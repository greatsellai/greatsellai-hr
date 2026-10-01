from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import make_url

from app.models import Candidate, IntegrationDailyCandidateAccess, IntegrationRequestLease
from app.services.integration_auth_service import (
    IntegrationAccessError, authenticate_integration_token, integration_now,
)
from app.services.integration_limit_service import (
    begin_integration_request, finalize_integration_read, release_integration_request,
)
from app.services.integration_management_service import revoke_integration_grant
from app.tenant_scope import clear_organization_context, set_organization_context
from test_integration_auth_helpers import make_context, named_auth


def principal(session, ctx):
    return authenticate_integration_token(session, token=ctx.token, audience="rest", settings=ctx.settings)


def add_candidates(ctx, count):
    with ctx.database.session_factory() as session:
        set_organization_context(session, ctx.organization_id)
        rows = [Candidate(display_name=f"Synthetic candidate {index}") for index in range(count)]
        session.add_all(rows)
        session.commit()
        return [row.id for row in rows]


def test_shared_minute_and_concurrency_limits_release_without_refund(tmp_path):
    ctx = make_context(tmp_path, integrations_grant_requests_per_minute=3, integrations_grant_concurrency=1)
    now = integration_now()
    with ctx.database.session_factory() as session:
        who = principal(session, ctx)
        lease = begin_integration_request(session, who, settings=ctx.settings, action="candidates.search", now=now)
        with pytest.raises(IntegrationAccessError, match="concurrency"):
            begin_integration_request(session, who, settings=ctx.settings, action="candidates.search", now=now)
        release_integration_request(session, who, lease_id=lease.id)
        for _ in range(2):
            lease = begin_integration_request(session, who, settings=ctx.settings, action="candidates.search", now=now)
            release_integration_request(session, who, lease_id=lease.id)
        with pytest.raises(IntegrationAccessError, match="rate_limit"):
            begin_integration_request(session, who, settings=ctx.settings, action="candidates.search", now=now)
    ctx.database.dispose()


def test_daily_distinct_includes_search_and_idempotently_counts_repeat(tmp_path):
    ctx = make_context(tmp_path, integrations_grant_daily_candidates=2)
    ids = add_candidates(ctx, 3)
    with ctx.database.session_factory() as session:
        who = principal(session, ctx)
        for batch in (ids[:2], ids[:2]):
            lease = begin_integration_request(session, who, settings=ctx.settings, action="candidates.search")
            finalize_integration_read(session, who, settings=ctx.settings, action="candidates.search",
                resource_type="candidate", resource_ids=batch, candidate_ids=batch, lease_id=lease.id)
        assert session.scalar(select(func.count()).select_from(IntegrationDailyCandidateAccess).where(IntegrationDailyCandidateAccess.scope_kind == "grant")) == 2
        lease = begin_integration_request(session, who, settings=ctx.settings, action="candidates.search")
        with pytest.raises(IntegrationAccessError, match="daily_candidate"):
            finalize_integration_read(session, who, settings=ctx.settings, action="candidates.search",
                resource_type="candidate", resource_ids=ids[2:], candidate_ids=ids[2:], lease_id=lease.id)
        release_integration_request(session, who, lease_id=lease.id)
    ctx.database.dispose()


def test_finalize_fails_closed_on_audit_failure_revocation_missing_scope_or_lease(tmp_path, monkeypatch):
    ctx = make_context(tmp_path)
    from app.services import integration_limit_service
    with ctx.database.session_factory() as session:
        who = principal(session, ctx)
        with pytest.raises(IntegrationAccessError, match="lease"):
            finalize_integration_read(session, who, settings=ctx.settings, action="candidates.search", resource_type="candidate")
        lease = begin_integration_request(session, who, settings=ctx.settings, action="candidates.search")
        with monkeypatch.context() as patch:
            patch.setattr(integration_limit_service, "record_integration_audit", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("synthetic failure")))
            with pytest.raises(IntegrationAccessError, match="audit_unavailable"):
                finalize_integration_read(session, who, settings=ctx.settings, action="candidates.search", resource_type="candidate", lease_id=lease.id)
        clear_organization_context(session)
        with pytest.raises(IntegrationAccessError, match="context_required"):
            finalize_integration_read(session, who, settings=ctx.settings, action="candidates.search", resource_type="candidate", lease_id=lease.id)
        set_organization_context(session, ctx.organization_id)
        with ctx.database.session_factory() as second:
            revoke_integration_grant(second, named_auth(second, ctx), grant_id=ctx.grant_id)
        with pytest.raises(IntegrationAccessError, match="invalid_token"):
            finalize_integration_read(session, who, settings=ctx.settings, action="candidates.search", resource_type="candidate", lease_id=lease.id)
        release_integration_request(session, who, lease_id=lease.id)
    ctx.database.dispose()


@pytest.fixture
def integration_postgres_url():
    base = os.getenv("INTEGRATION_TEST_POSTGRES_URL")
    if not base:
        pytest.skip("isolated PostgreSQL URL not configured")
    parsed = make_url(base)
    # This destructive-test fixture is intentionally restricted to the named
    # local synthetic DB. No arbitrary production URL may enable cleanup.
    if (parsed.host not in {"127.0.0.1", "::1", "localhost", "host.docker.internal"}
            or parsed.port not in {5432, 55432}
            or parsed.database not in {"integration_auth_test", "resume_v3_test"}):
        pytest.fail("integration PostgreSQL tests require the dedicated local synthetic database")
    import psycopg
    from psycopg import sql
    schema = "integration_auth_" + uuid4().hex
    connection = psycopg.connect(host=parsed.host, port=parsed.port, dbname=parsed.database,
        user=parsed.username, password=parsed.password, autocommit=True)
    connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    # A literal '=' is legal inside this query value and avoids ConfigParser's
    # percent interpolation in the existing Alembic environment.
    url = parsed.update_query_dict({"options": f"-csearch_path={schema}"}).render_as_string(hide_password=False).replace("%3D", "=")
    try:
        yield url
    finally:
        connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        connection.close()


def test_postgresql_replicas_never_overspend_rate_or_daily_distinct(tmp_path, integration_postgres_url):
    ctx = make_context(tmp_path, database_url=integration_postgres_url,
        integrations_grant_requests_per_minute=3, integrations_grant_concurrency=3,
        integrations_grant_daily_candidates=2)
    ids = add_candidates(ctx, 5)
    start = integration_now()
    barrier = Barrier(5)

    def attempt(index):
        with ctx.database.session_factory() as session:
            who = principal(session, ctx)
            session.commit()
            barrier.wait(timeout=10)
            lease = None
            try:
                lease = begin_integration_request(session, who, settings=ctx.settings, action="candidates.search", now=start)
                finalize_integration_read(session, who, settings=ctx.settings, action="candidates.search", resource_type="candidate",
                    resource_ids=[ids[index]], candidate_ids=[ids[index]], lease_id=lease.id, now=start)
                return "ok"
            except IntegrationAccessError as exc:
                return exc.code
            finally:
                if lease:
                    release_integration_request(session, who, lease_id=lease.id, now=start)

    try:
        with ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(attempt, range(5)))
        assert results.count("ok") == 2, results
        assert results.count("integration_daily_candidate_limit_exceeded") == 1, results
        assert results.count("integration_rate_limit_exceeded") == 2, results
        with ctx.database.session_factory() as session:
            set_organization_context(session, ctx.organization_id)
            assert session.scalar(select(func.count()).select_from(IntegrationDailyCandidateAccess).where(IntegrationDailyCandidateAccess.scope_kind == "grant")) == 2
    finally:
        ctx.database.dispose()


def test_postgresql_rotations_revoke_all_previous_tokens_and_revoke_blocks_finalize(tmp_path, integration_postgres_url):
    from app.integration_schemas import IntegrationCredentialRotate
    from app.services.integration_management_service import rotate_integration_credential
    ctx = make_context(tmp_path, database_url=integration_postgres_url)
    barrier = Barrier(2)

    def rotate(_):
        with ctx.database.session_factory() as session:
            auth = named_auth(session, ctx)
            session.commit()
            barrier.wait(timeout=10)
            return rotate_integration_credential(session, auth, settings=ctx.settings,
                grant_id=ctx.grant_id, payload=IntegrationCredentialRotate()).token

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            tokens = list(pool.map(rotate, range(2)))
        live = []
        with ctx.database.session_factory() as session:
            for token in [ctx.token, *tokens]:
                try:
                    live.append(authenticate_integration_token(session, token=token, audience="rest", settings=ctx.settings))
                except IntegrationAccessError:
                    pass
            assert len(live) == 1
            who = live[0]
            lease = begin_integration_request(session, who, settings=ctx.settings, action="candidates.search")
            with ctx.database.session_factory() as second:
                revoke_integration_grant(second, named_auth(second, ctx), grant_id=ctx.grant_id)
            with pytest.raises(IntegrationAccessError, match="invalid_token"):
                finalize_integration_read(session, who, settings=ctx.settings, action="candidates.search", resource_type="candidate", lease_id=lease.id)
            release_integration_request(session, who, lease_id=lease.id)
    finally:
        ctx.database.dispose()


def test_postgresql_concurrency_leases_are_shared_between_request_sessions(tmp_path, integration_postgres_url):
    ctx = make_context(tmp_path, database_url=integration_postgres_url)
    barrier = Barrier(5)

    def claim(_):
        with ctx.database.session_factory() as session:
            who = principal(session, ctx)
            session.commit()
            barrier.wait(timeout=10)
            try:
                lease = begin_integration_request(session, who, settings=ctx.settings, action="candidates.search")
                return lease.id
            except IntegrationAccessError as exc:
                assert exc.code == "integration_concurrency_limit_exceeded"
                return None

    try:
        with ThreadPoolExecutor(max_workers=5) as pool:
            leases = list(pool.map(claim, range(5)))
        assert len([lease for lease in leases if lease]) == 3
        with ctx.database.session_factory() as session:
            who = principal(session, ctx)
            for lease in leases:
                if lease:
                    release_integration_request(session, who, lease_id=lease)
    finally:
        ctx.database.dispose()

