"""Normal synthetic lifecycle races, isolated PostgreSQL only; no external I/O."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from threading import Event

import pytest
import httpx2
from fastapi import FastAPI
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from starlette.routing import Mount

from app.integration_mcp import create_integration_mcp_app, integration_mcp_lifespan
from app.integration_schemas import IntegrationGrantCreate, IntegrationPolicyPatch
from app.models import Candidate, IntegrationGrant, IntegrationRequestLease, Resume
from app.schemas import ResumeFactsSaveRequest, ResumeFactsSubmission
from app.services import integration_limit_service as limits
from app.services.candidate_data_lifecycle_service import delete_resume
from app.services.identity_service import revoke_user_auth_sessions
from app.services.integration_auth_service import IntegrationAccessError, authenticate_integration_token
from app.services.integration_management_service import create_integration_grant, update_integration_policy
from app.services.integration_read_service import get_candidate_profile
from app.services.resume_service import save_facts
from app.tenant_scope import set_organization_context
from test_integration_analysis_api import _browser_client, _csrf_headers, _external_client
from test_integration_analysis_service import ALL_SCOPES, analysis_payload
from test_integration_auth_helpers import make_context, named_auth
from test_integration_limits import integration_postgres_url  # noqa: F401
from test_integration_read_service import seed_read_candidates


@pytest.fixture
def lifecycle_context(tmp_path, integration_postgres_url):
    context = make_context(tmp_path, database_url=integration_postgres_url,
        integrations_analysis_enabled=True, integrations_mcp_enabled=True)
    try:
        with context.database.session_factory() as session:
            update_integration_policy(session, named_auth(session, context), settings=context.settings,
                payload=IntegrationPolicyPatch(enabled=True, allowed_scopes=ALL_SCOPES))
            issued = create_integration_grant(session, named_auth(session, context), settings=context.settings,
                payload=IntegrationGrantCreate(name="Synthetic lifecycle connection", scopes=ALL_SCOPES))
            context.analysis_token = issued.token
            context.analysis_grant_id = issued.grant.id
            authenticate_integration_token(session, token=issued.token, audience="rest", settings=context.settings)
            context.ids = seed_read_candidates(session, context.organization_id)
            context.alpha_profile = get_candidate_profile(session, candidate_id=context.ids["alpha"])
            context.beta_profile = get_candidate_profile(session, candidate_id=context.ids["beta"])
        yield context
    finally:
        context.database.dispose()


def _mutation(context, operation):
    with context.database.session_factory() as session:
        set_organization_context(session, context.organization_id)
        session.execute(text("SET LOCAL statement_timeout = '8s'"))
        result = operation(session)
        session.commit()
        return result


def _delete(context, resume_id=None):
    return _mutation(context, lambda session: delete_resume(session, settings=context.settings,
        resume_id=resume_id or context.alpha_profile.resume_id, actor_user_id=context.user_id,
        reason="candidate_request", private_note=None))


def _facts_request():
    return ResumeFactsSaveRequest(facts=ResumeFactsSubmission(skills=[
        {"skill_display": "measurable results", "evidence_block_ids": ["alpha-main"]},
    ]))


def _after_dto(monkeypatch, operation, *, browser=False):
    """Run a separate committed transaction after materialization, before fence."""
    name = "lock_integration_read_sources" if browser else "finalize_integration_read"
    original = getattr(limits, name)
    called = []

    def hooked(*args, **kwargs):
        if not called:
            called.append(True)
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(operation).result(timeout=10)
        return original(*args, **kwargs)

    monkeypatch.setattr(limits, name, hooked)
    return called


def _read(client, context, kind):
    root = f"/v1/integrations/candidates/{context.ids['alpha']}"
    headers = {"Authorization": f"Bearer {context.analysis_token}"}
    if kind == "search":
        return client.post("/v1/integrations/candidates/search", headers=headers,
            json={"skills_all_of": ["Python"]})
    if kind == "evidence":
        return client.post(root + "/evidence", headers=headers, json={"source_block_ids": ["alpha-main"]})
    return client.get(root + ("/assessments" if kind == "assessments" else ""), headers=headers)


def _assert_leases_released(context):
    with context.database.session_factory() as session:
        set_organization_context(session, context.organization_id)
        rows = session.scalars(select(IntegrationRequestLease)).all()
        assert rows and all(row.released_at is not None for row in rows)


def _save(client, context, payload=None):
    response = client.post("/v1/integrations/analysis-reports",
        headers={"Authorization": f"Bearer {context.analysis_token}"},
        json=(payload or analysis_payload(context)).model_dump(mode="json"))
    assert response.status_code == 202, response.text
    with _browser_client(context) as browser:
        pending = browser.get(f"/v1/integration-settings/analysis-reports/pending/{response.json()['id']}")
        assert pending.status_code == 200, pending.text
        confirmed = browser.post(
            f"/v1/integration-settings/analysis-reports/{response.json()['id']}/confirm",
            headers=_csrf_headers(context),
            json={"version": pending.json()["version"], "payload_sha256": pending.json()["payload_sha256"]},
        )
    assert confirmed.status_code == 200, confirmed.text
    return response.json()


@pytest.mark.parametrize("kind", ["search", "profile", "evidence", "assessments"])
def test_deleted_resume_between_dto_and_finalize_never_returns_old_data(lifecycle_context, monkeypatch, kind):
    context = lifecycle_context
    called = _after_dto(monkeypatch, lambda: _delete(context))
    with _external_client(context) as client:
        response = _read(client, context, kind)
    assert called
    assert response.status_code in {404, 409}, response.text
    assert "Python" not in response.text and "alpha-main" not in response.text
    with context.database.session_factory() as session:
        set_organization_context(session, context.organization_id)
        assert session.get(Candidate, context.ids["alpha"]) is not None
        assert session.get(Resume, context.alpha_profile.resume_id) is None
    _assert_leases_released(context)


def test_mcp_deleted_source_is_an_error_without_old_structured_data(lifecycle_context, monkeypatch):
    context = lifecycle_context
    with context.database.session_factory() as session:
        issued = create_integration_grant(session, named_auth(session, context), settings=context.settings,
            payload=IntegrationGrantCreate(name="Synthetic lifecycle MCP", audience="mcp", scopes=ALL_SCOPES))
    server, mcp_app = create_integration_mcp_app(database=context.database, settings=context.settings)

    @asynccontextmanager
    async def lifespan(_app):
        async with integration_mcp_lifespan(server):
            yield

    host = FastAPI(lifespan=lifespan)
    host.router.routes.append(Mount("/", app=mcp_app))
    called = _after_dto(monkeypatch, lambda: _delete(context))

    async def exercise():
        client = httpx2.AsyncClient(transport=httpx2.ASGITransport(host), base_url="http://testserver",
            headers={"Authorization": f"Bearer {issued.token}"})
        async with (host.router.lifespan_context(host), client,
            streamable_http_client("http://testserver/v1/mcp", http_client=client) as streams,
            ClientSession(*streams) as session):
            await session.initialize()
            return await session.call_tool("get_candidate_profile", {"candidate_id": context.ids["alpha"]})

    result = asyncio.run(exercise())
    assert called and result.is_error is True
    assert result.structured_content is None
    assert "Python" not in str(result.content)
    _assert_leases_released(context)


@pytest.mark.parametrize("browser", [False, True])
@pytest.mark.parametrize("detail", [False, True])
def test_draft_source_deletion_after_materialization_is_not_returned(lifecycle_context, monkeypatch, browser, detail):
    context = lifecycle_context
    with _external_client(context) as client:
        saved = _save(client, context)
    called = _after_dto(monkeypatch, lambda: _delete(context), browser=browser)
    prefix = "/v1/integration-settings" if browser else "/v1/integrations"
    path = prefix + "/analysis-reports" + (f"/{saved['id']}" if detail else "")
    with (_browser_client(context) if browser else _external_client(context)) as client:
        response = client.get(path, headers={} if browser else {"Authorization": f"Bearer {context.analysis_token}"})
    assert called
    assert response.status_code in {404, 409}, response.text
    assert "Warehouse automation" not in response.text
    _assert_leases_released(context)


def test_draft_version_is_fenced_after_materialization(lifecycle_context, monkeypatch):
    context = lifecycle_context
    with _external_client(context) as client:
        proposed = client.post("/v1/integrations/analysis-reports",
            headers={"Authorization": f"Bearer {context.analysis_token}"},
            json=analysis_payload(context).model_dump(mode="json"))
    assert proposed.status_code == 202
    with _browser_client(context) as client:
        pending = client.get(f"/v1/integration-settings/analysis-reports/pending/{proposed.json()['id']}")
        assert pending.status_code == 200

        def change_source():
            _mutation(context, lambda session: setattr(
                session.get(Resume, context.alpha_profile.resume_id), "facts_version", 2,
            ))

        called = _after_dto(monkeypatch, change_source, browser=True)
        response = client.post(
            f"/v1/integration-settings/analysis-reports/{proposed.json()['id']}/confirm",
            headers=_csrf_headers(context),
            json={"version": pending.json()["version"], "payload_sha256": pending.json()["payload_sha256"]},
        )
    assert called
    assert response.status_code == 409, response.text
    assert "Warehouse automation" not in response.text
    with _external_client(context) as client:
        invisible = client.get(f"/v1/integrations/analysis-reports/{proposed.json()['id']}",
            headers={"Authorization": f"Bearer {context.analysis_token}"})
    assert invisible.status_code == 404
    _assert_leases_released(context)


def test_fact_write_after_dto_requires_a_fresh_read(lifecycle_context, monkeypatch):
    context = lifecycle_context
    called = _after_dto(monkeypatch, lambda: _mutation(context, lambda session: save_facts(session,
        resume_id=context.alpha_profile.resume_id,
        request=_facts_request())))
    with _external_client(context) as client:
        response = _read(client, context, "profile")
    assert called
    assert response.status_code == 409, response.text
    assert "Python" not in response.text
    _assert_leases_released(context)


@pytest.mark.parametrize("change,status", [("session", 401), ("membership", 401), ("policy", 403)])
def test_browser_authority_after_dto_is_rechecked(lifecycle_context, monkeypatch, change, status):
    context = lifecycle_context
    with _external_client(context) as client:
        saved = _save(client, context)
    def revoke(session):
        auth = named_auth(session, context)
        if change == "session":
            revoke_user_auth_sessions(session, principal=auth)
        elif change == "membership":
            auth.membership.is_active = False
        else:
            update_integration_policy(session, auth, settings=context.settings,
                payload=IntegrationPolicyPatch(enabled=False, allowed_scopes=ALL_SCOPES))

    called = _after_dto(monkeypatch, lambda: _mutation(context, revoke), browser=True)
    with _browser_client(context) as client:
        response = client.get(f"/v1/integration-settings/analysis-reports/{saved['id']}")
    assert called
    assert response.status_code == status, response.text
    assert "Warehouse automation" not in response.text


def test_pending_confirmation_rechecks_original_connection_after_materialization(lifecycle_context, monkeypatch):
    context = lifecycle_context
    with _external_client(context) as client:
        proposed = client.post("/v1/integrations/analysis-reports",
            headers={"Authorization": f"Bearer {context.analysis_token}"},
            json=analysis_payload(context).model_dump(mode="json"))
    assert proposed.status_code == 202
    with _browser_client(context) as client:
        pending = client.get(f"/v1/integration-settings/analysis-reports/pending/{proposed.json()['id']}")
        assert pending.status_code == 200

        def revoke_source_grant():
            def revoke(session):
                grant = session.get(IntegrationGrant, context.analysis_grant_id)
                grant.revoked_at = datetime.now(timezone.utc)
            _mutation(context, revoke)

        called = _after_dto(monkeypatch, revoke_source_grant, browser=True)
        response = client.post(
            f"/v1/integration-settings/analysis-reports/{proposed.json()['id']}/confirm",
            headers=_csrf_headers(context),
            json={"version": pending.json()["version"], "payload_sha256": pending.json()["payload_sha256"]},
        )
    assert called
    assert response.status_code == 401, response.text
    with _external_client(context) as client:
        invisible = client.get(f"/v1/integrations/analysis-reports/{proposed.json()['id']}",
            headers={"Authorization": f"Bearer {context.analysis_token}"})
    assert invisible.status_code == 401
    _assert_leases_released(context)


def test_read_fence_blocks_same_source_delete_but_not_unreturned_candidate(lifecycle_context, monkeypatch):
    context = lifecycle_context
    original = limits.lock_integration_read_sources
    entered, finished = Event(), Event()
    futures = []

    def delete_alpha():
        entered.set()
        try:
            return _delete(context)
        finally:
            finished.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        def fenced(*args, **kwargs):
            original(*args, **kwargs)
            futures.append(pool.submit(delete_alpha))
            assert entered.wait(2)
            assert not finished.wait(0.15), "source delete crossed a held read fence"
            # Search inspected beta too, but returns only alpha for this filter.
            pool.submit(_delete, context, context.beta_profile.resume_id).result(timeout=5)

        monkeypatch.setattr(limits, "lock_integration_read_sources", fenced)
        with _external_client(context) as client:
            response = _read(client, context, "search")
        assert response.status_code == 200, response.text
        assert [item["candidate_id"] for item in response.json()["items"]] == [context.ids["alpha"]]
        futures[0].result(timeout=10)
    assert finished.is_set()
    _assert_leases_released(context)


@pytest.mark.parametrize("draft", [False, True])
def test_existing_resume_first_writer_and_candidate_first_reader_do_not_deadlock(lifecycle_context, monkeypatch, draft):
    context = lifecycle_context
    resume_locked, continue_writer = Event(), Event()

    def writer():
        def after_lock(_resume):
            resume_locked.set()
            assert continue_writer.wait(5)
        return _mutation(context, lambda session: save_facts(session,
            resume_id=context.alpha_profile.resume_id,
            request=_facts_request(), _after_resume_lock=after_lock))

    # Capture the exact PG NOWAIT error through the normal HTTP wrapper. The
    # writer remains paused holding Resume, while the request holds Candidate.
    payload = analysis_payload(context)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(writer)
        assert resume_locked.wait(5)
        try:
            with _external_client(context) as client:
                response = (client.post("/v1/integrations/analysis-reports",
                    headers={"Authorization": f"Bearer {context.analysis_token}"},
                    json=payload.model_dump(mode="json")) if draft else _read(client, context, "profile"))
            assert response.status_code == 409, response.text
            assert response.json()["detail"]["code"] == "integration_source_busy"
            _assert_leases_released(context)
        finally:
            continue_writer.set()
        future.result(timeout=10)


def test_draft_busy_retry_preserves_idempotency_and_releases_lease(lifecycle_context):
    context = lifecycle_context
    payload = analysis_payload(context)
    with context.database.session_factory() as holder:
        set_organization_context(holder, context.organization_id)
        holder.scalar(select(Resume).where(Resume.id == context.alpha_profile.resume_id).with_for_update())
        with _external_client(context) as client:
            busy = client.post("/v1/integrations/analysis-reports",
                headers={"Authorization": f"Bearer {context.analysis_token}"}, json=payload.model_dump(mode="json"))
        assert busy.status_code == 409, busy.text
        holder.rollback()
    _assert_leases_released(context)
    with _external_client(context) as client:
        saved = _save(client, context, payload)
        replay = _save(client, context, payload)
    assert saved["id"] == replay["id"] and saved["version"] == replay["version"] == 1
    assert saved["replayed"] is False and replay["replayed"] is True
    _assert_leases_released(context)


def test_only_postgresql_nowait_error_is_mapped_to_retryable_conflict():
    class SyntheticDriverError(Exception):
        def __init__(self, sqlstate):
            self.sqlstate = sqlstate

    with pytest.raises(IntegrationAccessError, match="integration_source_busy"):
        with limits.integration_source_lock_conflict():
            raise OperationalError("synthetic", {}, SyntheticDriverError("55P03"))
    for state in ("40P01", "08006", None):
        with pytest.raises(OperationalError):
            with limits.integration_source_lock_conflict():
                raise OperationalError("synthetic", {}, SyntheticDriverError(state))
