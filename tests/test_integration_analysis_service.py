from __future__ import annotations

from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event
from types import SimpleNamespace
from uuid import uuid4
import unicodedata

import pytest
from sqlalchemy import event, func, select

from app.integration_analysis_schemas import (
    IntegrationAnalysisCandidateReference,
    IntegrationAnalysisPrepare,
    IntegrationAnalysisObservation,
)
from app.integration_schemas import IntegrationGrantCreate, IntegrationPolicyPatch
from app.models import (
    Candidate,
    IntegrationIdempotencyRecord,
    IntegrationAnalysisReference,
    IntegrationAnalysisReport,
    IntegrationRequestLease,
    Resume,
)
from app.services.identity_service import AuthPrincipal
from app.services.integration_analysis_service import (
    IntegrationAnalysisError,
    confirm_pending_analysis_draft,
    discard_pending_analysis_draft,
    get_pending_analysis_draft,
    get_analysis_draft,
    list_analysis_drafts,
    required_save_scopes,
    prepare_analysis_draft,
)
from app.services.integration_auth_service import (
    IntegrationAccessError,
    authenticate_integration_token,
)
from app.services.integration_management_service import (
    create_integration_grant,
    revoke_integration_grant,
    update_integration_policy,
)
from app.services.integration_read_service import execute_integration_read
from app.services.integration_retention_service import cleanup_expired_integration_records
from app.services.integration_auth_service import integration_now
from app.tenant_scope import set_organization_context
from test_integration_auth_helpers import make_context, named_auth
from test_integration_limits import integration_postgres_url
from test_integration_read_service import seed_read_candidates

ALL_SCOPES = [
    "candidates:read",
    "jobs:read",
    "assessments:read",
    "evidence:read",
    "analyses:read",
    "analyses:write",
]


def make_analysis_context(tmp_path, *, database_url=None):
    database_options = {"database_url": database_url} if database_url else {}
    context = make_context(
        tmp_path,
        **database_options,
        integrations_analysis_enabled=True,
        integrations_mcp_enabled=True,
    )
    with context.database.session_factory() as session:
        update_integration_policy(
            session,
            named_auth(session, context),
            settings=context.settings,
            payload=IntegrationPolicyPatch(enabled=True, allowed_scopes=ALL_SCOPES),
        )
        issued = create_integration_grant(
            session,
            named_auth(session, context),
            settings=context.settings,
            payload=IntegrationGrantCreate(
                name="Synthetic analysis connection",
                scopes=ALL_SCOPES,
            ),
        )
        mcp_issued = create_integration_grant(
            session,
            named_auth(session, context),
            settings=context.settings,
            payload=IntegrationGrantCreate(
                name="Synthetic MCP analysis connection",
                audience="mcp",
                scopes=ALL_SCOPES,
            ),
        )
        principal = authenticate_integration_token(
            session,
            token=issued.token,
            audience="rest",
            settings=context.settings,
            required_scopes=ALL_SCOPES,
        )
        ids = seed_read_candidates(session, context.organization_id)
    context.analysis_token = issued.token
    context.analysis_mcp_token = mcp_issued.token
    context.analysis_principal = principal
    context.ids = ids
    return context


def analysis_payload(
    context,
    *,
    key=None,
    title="Warehouse automation draft",
):
    return IntegrationAnalysisPrepare(
        idempotency_key=key or uuid4(),
        title=title,
        candidates=[
            IntegrationAnalysisCandidateReference(
                candidate_id=context.ids["alpha"],
                resume_id=context.alpha_profile.resume_id,
                fact_snapshot_id=context.alpha_profile.fact_snapshot_id,
                facts_version=context.alpha_profile.facts_version,
                fact_ids=["fact-alpha-skill"],
                source_block_ids=["alpha-main"],
            )
        ],
        inferences=[
            IntegrationAnalysisObservation(
                candidate_id=context.ids["alpha"],
                text="The candidate may fit a Python automation role.",
            )
        ],
        questions_to_verify=[
            IntegrationAnalysisObservation(
                candidate_id=context.ids["alpha"],
                text="Verify production ownership in a recruiter interview.",
            )
        ],
    )


def confirm_prepared(session, context, prepared):
    browser_principal = named_auth(session, context)
    pending = get_pending_analysis_draft(
        session, browser_principal, report_id=prepared.id,
    )
    return confirm_pending_analysis_draft(
        session,
        browser_principal,
        report_id=prepared.id,
        version=pending.version,
        payload_sha256=pending.payload_sha256,
        settings=context.settings,
    )


@pytest.fixture()
def analysis_context(tmp_path):
    context = make_analysis_context(tmp_path)
    from app.services.integration_read_service import get_candidate_profile

    with context.database.session_factory() as session:
        authenticate_integration_token(
            session,
            token=context.analysis_token,
            audience="rest",
            settings=context.settings,
            required_scopes=("candidates:read",),
        )
        context.alpha_profile = get_candidate_profile(
            session,
            candidate_id=context.ids["alpha"],
        )
    return context


def _principal(session, context):
    return authenticate_integration_token(
        session,
        token=context.analysis_token,
        audience="rest",
        settings=context.settings,
        required_scopes=ALL_SCOPES,
    )


def execute_prepare(session, context, principal, payload):
    scoped = replace(principal, required_scopes=required_save_scopes(payload))
    candidate_ids = tuple(str(item.candidate_id) for item in payload.candidates)
    return execute_integration_read(
        session,
        principal=scoped,
        settings=context.settings,
        action="integration.analysis.prepare",
        resource_type="analysis_confirmation",
        read=lambda: prepare_analysis_draft(session, scoped, payload=payload),
        resource_ids=lambda value: (value.id,),
        candidate_ids=lambda _value: candidate_ids,
    )


def test_saved_detail_contains_only_selected_server_facts(analysis_context):
    with analysis_context.database.session_factory() as session:
        principal = _principal(session, analysis_context)
        prepared = prepare_analysis_draft(
            session,
            principal,
            payload=analysis_payload(analysis_context),
        )
        assert prepared.status == "awaiting_confirmation"
        assert list_analysis_drafts(session, principal, settings=analysis_context.settings).items == []
        pending = get_pending_analysis_draft(
            session, named_auth(session, analysis_context), report_id=prepared.id,
        )
        assert pending.source_status == "current"
        saved = confirm_prepared(session, analysis_context, prepared)

    with analysis_context.database.session_factory() as session:
        principal = _principal(session, analysis_context)
        listing = list_analysis_drafts(
            session,
            principal,
            settings=analysis_context.settings,
        )
        detail = get_analysis_draft(session, principal, report_id=saved.id)
        stored = session.scalar(
            select(IntegrationAnalysisReport).where(
                IntegrationAnalysisReport.id == saved.id
            )
        )

    assert [item.id for item in listing.items] == [saved.id]
    assert detail.kind == "external_ai_draft"
    assert detail.decision_authority == "recruiting_team"
    assert detail.source_status == "current"
    assert detail.referenced_facts[0].facts.is_985_211 is None
    assert detail.referenced_facts[0].facts.education == []
    assert detail.referenced_facts[0].facts.experiences == []
    assert [fact.fact_id for fact in detail.referenced_facts[0].facts.skills] == [
        "fact-alpha-skill"
    ]
    assert detail.referenced_facts[0].facts.skills[0].evidence_source_block_ids == [
        "alpha-main"
    ]
    assert set(stored.content_json) == {
        "schema_version",
        "inferences",
        "questions_to_verify",
    }
    assert "verified" not in str(stored.content_json).lower()


def test_idempotency_replays_pending_and_confirmed_states_without_client_confirmation_flag(analysis_context):
    key = uuid4()
    payload = analysis_payload(analysis_context, key=key)
    with analysis_context.database.session_factory() as session:
        principal = _principal(session, analysis_context)
        first = execute_prepare(session, analysis_context, principal, payload)
        replay = execute_prepare(session, analysis_context, principal, payload)
        assert replay.id == first.id
        assert replay.replayed is True
        assert replay.status == "awaiting_confirmation"
        with pytest.raises(
            IntegrationAnalysisError, match="integration_idempotency_conflict"
        ):
            execute_prepare(
                session, analysis_context, principal,
                analysis_payload(
                    analysis_context,
                    key=key,
                    title="Different request",
                ),
            )

        confirmed = confirm_prepared(session, analysis_context, first)
        assert confirmed.version == 1
        replay_after_confirmation = execute_prepare(
            session, analysis_context, principal, payload,
        )
        assert replay_after_confirmation.status == "saved"
        assert replay_after_confirmation.version == confirmed.version


def test_idempotency_replays_discarded_terminal_receipt_through_finalizer(analysis_context):
    payload = analysis_payload(analysis_context)
    with analysis_context.database.session_factory() as session:
        principal = _principal(session, analysis_context)
        prepared = execute_prepare(session, analysis_context, principal, payload)
        discard_pending_analysis_draft(
            session,
            named_auth(session, analysis_context),
            report_id=prepared.id,
            version=prepared.version,
        )
        replay = execute_prepare(session, analysis_context, principal, payload)
        assert replay.id == prepared.id
        assert replay.status == "expired"
        assert replay.replayed is True
        assert session.scalar(select(IntegrationIdempotencyRecord).where(
            IntegrationIdempotencyRecord.report_id == prepared.id,
        )) is not None


def test_idempotency_replays_expired_scrubbed_receipt_for_full_24_hours(analysis_context):
    payload = analysis_payload(analysis_context)
    current = integration_now()
    with analysis_context.database.session_factory() as session:
        principal = _principal(session, analysis_context)
        prepared = execute_prepare(session, analysis_context, principal, payload)
        report = session.get(IntegrationAnalysisReport, prepared.id)
        report.expires_at = current - timedelta(seconds=1)
        session.commit()

    cleanup_expired_integration_records(analysis_context.database, now=current)

    with analysis_context.database.session_factory() as session:
        principal = _principal(session, analysis_context)
        replay = execute_prepare(session, analysis_context, principal, payload)
        report = session.get(IntegrationAnalysisReport, prepared.id)
        assert replay.id == prepared.id
        assert replay.status == "expired"
        assert replay.replayed is True
        assert report is not None
        assert report.content_json == {}
        assert report.invalidation_reason == "retention_expired"
        assert session.scalar(select(IntegrationIdempotencyRecord).where(
            IntegrationIdempotencyRecord.report_id == prepared.id,
        )) is not None
        assert session.scalar(select(IntegrationAnalysisReference).where(
            IntegrationAnalysisReference.report_id == prepared.id,
        )) is None

    cleanup_expired_integration_records(
        analysis_context.database, now=current + timedelta(hours=25),
    )
    with analysis_context.database.session_factory() as session:
        set_organization_context(session, analysis_context.organization_id)
        assert session.scalar(select(IntegrationIdempotencyRecord).where(
            IntegrationIdempotencyRecord.report_id == prepared.id,
        )) is None
        assert session.get(IntegrationAnalysisReport, prepared.id) is None


def test_terminal_idempotency_replay_still_fails_after_grant_revocation(analysis_context):
    payload = analysis_payload(analysis_context)
    with analysis_context.database.session_factory() as session:
        principal = _principal(session, analysis_context)
        prepared = execute_prepare(session, analysis_context, principal, payload)
        discard_pending_analysis_draft(
            session,
            named_auth(session, analysis_context),
            report_id=prepared.id,
            version=prepared.version,
        )
        stale_principal = principal

    with analysis_context.database.session_factory() as session:
        revoke_integration_grant(
            session,
            named_auth(session, analysis_context),
            grant_id=stale_principal.grant_id,
        )

    with analysis_context.database.session_factory() as session:
        set_organization_context(session, analysis_context.organization_id)
        with pytest.raises(IntegrationAccessError, match="integration_invalid_token"):
            execute_prepare(session, analysis_context, stale_principal, payload)


def test_retry_replays_committed_receipt_when_source_validation_loses_race(
    analysis_context, monkeypatch,
):
    payload = analysis_payload(analysis_context)
    with analysis_context.database.session_factory() as session:
        prepared = execute_prepare(
            session, analysis_context, _principal(session, analysis_context), payload,
        )

    from app.services import integration_analysis_service as analysis_service

    original_find = analysis_service._find_idempotency
    initial_lookup = True

    def miss_initial_lookup(session, principal, **kwargs):
        nonlocal initial_lookup
        if initial_lookup:
            initial_lookup = False
            return None
        return original_find(session, principal, **kwargs)

    def source_changed_after_winner(session, principal, payload):
        raise IntegrationAnalysisError("integration_source_version_conflict", 409)

    monkeypatch.setattr(analysis_service, "_find_idempotency", miss_initial_lookup)
    monkeypatch.setattr(analysis_service, "_lock_and_validate_sources", source_changed_after_winner)
    with analysis_context.database.session_factory() as session:
        replay = execute_prepare(
            session, analysis_context, _principal(session, analysis_context), payload,
        )

    assert replay.id == prepared.id
    assert replay.status == "awaiting_confirmation"
    assert replay.replayed is True


def test_postgresql_pending_confirmation_limit_is_atomic(
    tmp_path, integration_postgres_url, monkeypatch,
):
    """SQLite cannot prove row-lock serialization; this runs only on the
    dedicated local PostgreSQL fixture and is skipped when it is unavailable.
    """

    context = make_analysis_context(tmp_path, database_url=integration_postgres_url)
    from app.services.integration_read_service import get_candidate_profile

    try:
        with context.database.session_factory() as session:
            authenticate_integration_token(
                session,
                token=context.analysis_token,
                audience="rest",
                settings=context.settings,
                required_scopes=("candidates:read",),
            )
            context.alpha_profile = get_candidate_profile(
                session, candidate_id=context.ids["alpha"],
            )
            beta_profile = get_candidate_profile(session, candidate_id=context.ids["beta"])
            principal = _principal(session, context)
            for index in range(9):
                execute_prepare(
                    session,
                    context,
                    principal,
                    analysis_payload(context, title=f"Queued draft {index}"),
                )

        barrier = Barrier(2)
        from app.services import integration_analysis_service as analysis_service
        original_queue_lock = analysis_service._lock_pending_confirmation_queue

        def synchronized_queue_lock(session, principal):
            # Both requests must own distinct source locks before contending
            # on the shared identity lock; alpha/alpha would mask the bug.
            barrier.wait(timeout=10)
            return original_queue_lock(session, principal)

        monkeypatch.setattr(analysis_service, "_lock_pending_confirmation_queue", synchronized_queue_lock)

        def attempt(index):
            with context.database.session_factory() as session:
                principal = _principal(session, context)
                payload = analysis_payload(
                    context, title=f"Concurrent queued draft {index}",
                )
                if index:
                    payload.candidates = [IntegrationAnalysisCandidateReference(
                        candidate_id=context.ids["beta"], resume_id=beta_profile.resume_id,
                        fact_snapshot_id=beta_profile.fact_snapshot_id, facts_version=beta_profile.facts_version,
                        fact_ids=["fact-beta-skill"], source_block_ids=["beta-main"],
                    )]
                    for observation in payload.inferences + payload.questions_to_verify:
                        observation.candidate_id = payload.candidates[0].candidate_id
                try:
                    execute_prepare(session, context, principal, payload)
                    return "ok"
                except IntegrationAnalysisError as error:
                    return error.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, range(2)))
        assert results.count("ok") == 1, results
        assert results.count("integration_confirmation_queue_full") == 1, results

        with context.database.session_factory() as session:
            set_organization_context(session, context.organization_id)
            assert session.scalar(select(func.count()).select_from(
                IntegrationAnalysisReport,
            ).where(
                IntegrationAnalysisReport.organization_id == context.organization_id,
                IntegrationAnalysisReport.confirmed_at.is_(None),
                IntegrationAnalysisReport.invalidated_at.is_(None),
            )) == 10
    finally:
        context.database.dispose()


def test_postgresql_same_key_waiter_replays_after_queue_reaches_limit(
    tmp_path, integration_postgres_url, monkeypatch,
):
    context = make_analysis_context(tmp_path, database_url=integration_postgres_url)
    from app.services import integration_analysis_service as analysis_service
    from app.services.integration_read_service import get_candidate_profile

    try:
        with context.database.session_factory() as session:
            principal = _principal(session, context)
            context.alpha_profile = get_candidate_profile(session, candidate_id=context.ids["alpha"])
            for index in range(9):
                execute_prepare(session, context, principal, analysis_payload(context, title=f"Queued draft {index}"))

        barrier = Barrier(2)
        original_source_lock = analysis_service._lock_and_validate_sources

        def synchronized_source_lock(session, principal, payload):
            # Force both initial idempotency reads to miss before either
            # request obtains the common candidate lock.
            barrier.wait(timeout=10)
            return original_source_lock(session, principal, payload)

        monkeypatch.setattr(analysis_service, "_lock_and_validate_sources", synchronized_source_lock)
        payload = analysis_payload(context)

        def attempt(_):
            with context.database.session_factory() as session:
                return execute_prepare(session, context, _principal(session, context), payload)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, range(2)))
        assert results[0].id == results[1].id
        assert sorted(result.replayed for result in results) == [False, True]
        assert all(result.status == "awaiting_confirmation" for result in results)
    finally:
        context.database.dispose()


def test_postgresql_same_key_different_sources_conflicts_after_identity_wait(
    tmp_path, integration_postgres_url, monkeypatch,
):
    context = make_analysis_context(tmp_path, database_url=integration_postgres_url)
    from app.services import integration_analysis_service as analysis_service
    from app.services.integration_read_service import get_candidate_profile

    try:
        with context.database.session_factory() as session:
            context.alpha_profile = get_candidate_profile(
                session, candidate_id=context.ids["alpha"],
            )
            beta_profile = get_candidate_profile(
                session, candidate_id=context.ids["beta"],
            )

        first_payload = analysis_payload(context)
        second_payload = analysis_payload(
            context, key=first_payload.idempotency_key,
            title="Different source proposal",
        )
        second_payload.candidates = [IntegrationAnalysisCandidateReference(
            candidate_id=context.ids["beta"],
            resume_id=beta_profile.resume_id,
            fact_snapshot_id=beta_profile.fact_snapshot_id,
            facts_version=beta_profile.facts_version,
            fact_ids=["fact-beta-skill"],
            source_block_ids=["beta-main"],
        )]
        for observation in second_payload.inferences + second_payload.questions_to_verify:
            observation.candidate_id = context.ids["beta"]

        barrier = Barrier(2)
        original_source_lock = analysis_service._lock_and_validate_sources

        def synchronize_after_initial_miss(session, principal, payload):
            barrier.wait(timeout=10)
            return original_source_lock(session, principal, payload)

        monkeypatch.setattr(
            analysis_service, "_lock_and_validate_sources", synchronize_after_initial_miss,
        )

        def attempt(payload):
            with context.database.session_factory() as session:
                try:
                    result = execute_prepare(
                        session, context, _principal(session, context), payload,
                    )
                    return result.status
                except IntegrationAnalysisError as error:
                    return error.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, (first_payload, second_payload)))
        assert results.count("awaiting_confirmation") == 1, results
        assert results.count("integration_idempotency_conflict") == 1, results
    finally:
        context.database.dispose()


def test_expired_idempotency_key_can_be_reused_before_worker_cleanup(analysis_context):
    payload = analysis_payload(analysis_context)
    with analysis_context.database.session_factory() as session:
        principal = _principal(session, analysis_context)
        original = execute_prepare(session, analysis_context, principal, payload)
        confirm_prepared(session, analysis_context, original)
        record = session.scalar(select(IntegrationIdempotencyRecord).where(
            IntegrationIdempotencyRecord.report_id == original.id,
        ))
        record.expires_at = integration_now() - timedelta(seconds=1)
        session.commit()
        replacement = execute_prepare(session, analysis_context, principal, payload)
        assert replacement.id != original.id
        assert replacement.replayed is False
        assert replacement.status == "awaiting_confirmation"
        assert session.get(IntegrationAnalysisReport, original.id).confirmed_at is not None


def test_postgresql_confirm_and_discard_share_report_before_authority_order(
    tmp_path, integration_postgres_url, monkeypatch,
):
    context = make_analysis_context(tmp_path, database_url=integration_postgres_url)
    from app.services import integration_limit_service as limit_service
    from app.services.integration_read_service import get_candidate_profile

    try:
        with context.database.session_factory() as session:
            principal = _principal(session, context)
            context.alpha_profile = get_candidate_profile(session, candidate_id=context.ids["alpha"])
            prepared = execute_prepare(session, context, principal, analysis_payload(context))

        confirmation_has_report = Event()
        discard_requested_report = Event()
        original_source_lock = limit_service.lock_integration_read_sources

        def pause_confirmation_with_report_lock(session, provenance, **kwargs):
            original_source_lock(session, provenance, **kwargs)
            confirmation_has_report.set()
            assert discard_requested_report.wait(timeout=10)

        monkeypatch.setattr(limit_service, "lock_integration_read_sources", pause_confirmation_with_report_lock)

        def confirm():
            with context.database.session_factory() as session:
                return confirm_prepared(session, context, prepared)

        def discard():
            assert confirmation_has_report.wait(timeout=10)
            with context.database.session_factory() as session:
                browser_principal = named_auth(session, context)

                def signal_report_lock(execution):
                    statement = execution.statement
                    if (execution.is_select and getattr(statement, "_for_update_arg", None) is not None
                            and any(item.get("entity") is IntegrationAnalysisReport
                                for item in getattr(statement, "column_descriptions", ()))):
                        discard_requested_report.set()

                event.listen(session, "do_orm_execute", signal_report_lock)
                try:
                    discard_pending_analysis_draft(session, browser_principal,
                        report_id=prepared.id, version=prepared.version)
                    return "discarded"
                except IntegrationAnalysisError as error:
                    return error.code
                finally:
                    event.remove(session, "do_orm_execute", signal_report_lock)

        with ThreadPoolExecutor(max_workers=2) as pool:
            confirmation = pool.submit(confirm)
            discarded = pool.submit(discard)
            assert confirmation.result(timeout=20).status == "saved"
            assert discarded.result(timeout=20) == "integration_resource_not_found"
    finally:
        context.database.dispose()


def test_retention_makes_progress_past_live_receipt_tombstones(analysis_context):
    current = integration_now()
    report_ids = []
    with analysis_context.database.session_factory() as session:
        principal = _principal(session, analysis_context)
        for index in range(101):
            report = IntegrationAnalysisReport(
                id=f"00000000-0000-0000-0000-{index:012d}",
                organization_id=principal.organization_id, owner_user_id=principal.user_id,
                membership_id=principal.membership_id, source_grant_id=principal.grant_id,
                source_credential_id=principal.credential_id, source_audience=principal.audience,
                source_auth_session_version=principal.auth_session_version,
                title="Synthetic expired pending content", content_json={"inferences": ["Synthetic body"]},
                created_at=current - timedelta(minutes=16), expires_at=current - timedelta(minutes=1),
            )
            session.add(report)
            session.flush()
            report_ids.append(report.id)
            session.add(IntegrationIdempotencyRecord(
                organization_id=principal.organization_id, user_id=principal.user_id,
                membership_id=principal.membership_id, grant_id=principal.grant_id,
                audience=principal.audience, operation="prepare_analysis_draft",
                key_digest=f"{index:064x}", request_digest="a" * 64,
                report_id=report.id, report_version=1, expires_at=current + timedelta(hours=23),
            ))
        session.commit()

    cleanup_expired_integration_records(analysis_context.database, now=current)
    cleanup_expired_integration_records(analysis_context.database, now=current)
    with analysis_context.database.session_factory() as session:
        set_organization_context(session, analysis_context.organization_id)
        reports = session.scalars(select(IntegrationAnalysisReport).where(
            IntegrationAnalysisReport.id.in_(report_ids),
        )).all()
        assert len(reports) == 101
        assert all(report.content_json == {} and report.invalidated_at is not None for report in reports)
        assert session.scalar(select(func.count()).select_from(IntegrationIdempotencyRecord)) == 101

    cleanup_expired_integration_records(analysis_context.database, now=current + timedelta(hours=24))
    cleanup_expired_integration_records(analysis_context.database, now=current + timedelta(hours=24))
    with analysis_context.database.session_factory() as session:
        set_organization_context(session, analysis_context.organization_id)
        assert session.scalar(select(func.count()).select_from(IntegrationAnalysisReport)) == 0
        assert session.scalar(select(func.count()).select_from(IntegrationIdempotencyRecord)) == 0


def test_unknown_fact_and_sensitive_draft_text_are_rejected(analysis_context):
    bad_fact = analysis_payload(analysis_context)
    bad_fact.candidates[0].fact_ids = ["invented-fact"]
    sensitive = analysis_payload(analysis_context, title="Alice Secret report")
    with analysis_context.database.session_factory() as session:
        principal = _principal(session, analysis_context)
        with pytest.raises(
            IntegrationAnalysisError, match="integration_fact_reference_not_found"
        ):
            prepare_analysis_draft(session, principal, payload=bad_fact)
        with pytest.raises(
            IntegrationAnalysisError, match="integration_sensitive_draft_not_supported"
        ):
            prepare_analysis_draft(session, principal, payload=sensitive)
        assert session.scalar(select(IntegrationAnalysisReport)) is None


def test_natural_chinese_draft_text_can_be_saved_and_read(analysis_context):
    title = "候选人分析：待复核（草稿）"
    inference = "候选人明确列出 Ｐｙｔｈｏｎ，仍需招聘团队结合岗位进行人工复核。"
    question = "是否有生产项目经验？请在面试中核实。"
    payload = analysis_payload(analysis_context, title=title)
    payload.inferences[0].text = inference
    payload.questions_to_verify[0].text = question
    with analysis_context.database.session_factory() as session:
        principal = _principal(session, analysis_context)
        prepared = prepare_analysis_draft(session, principal, payload=payload)
        pending = get_pending_analysis_draft(session, named_auth(session, analysis_context), report_id=prepared.id)
        assert pending.inferences[0].text == unicodedata.normalize("NFKC", inference)
        assert pending.questions_to_verify[0].text == unicodedata.normalize("NFKC", question)
        saved = confirm_prepared(session, analysis_context, prepared)
        detail = get_analysis_draft(session, principal, report_id=saved.id)
        assert detail.title == unicodedata.normalize("NFKC", title)
        assert detail.inferences[0].text == unicodedata.normalize("NFKC", inference)
        assert detail.questions_to_verify[0].text == unicodedata.normalize("NFKC", question)


@pytest.mark.parametrize("text", [
    "候选人具有 Python 经验，邮箱：synthetic@example.test",
    "候选人具有 Python 经验，ｅｍａｉｌ：synthetic@example.test",
    "候选人具有 Python 经验，姓名：合成测试人",
    "候选人具有 Py\u200bthon 经验。",
    "Ａｌｉｃｅ Ｓｅｃｒｅｔ 的分析草稿",
])
def test_unicode_draft_normalization_does_not_allow_private_content(analysis_context, text):
    payload = analysis_payload(analysis_context)
    payload.inferences[0].text = text
    with analysis_context.database.session_factory() as session:
        principal = _principal(session, analysis_context)
        with pytest.raises(
            IntegrationAnalysisError, match="integration_sensitive_draft_not_supported"
        ):
            prepare_analysis_draft(session, principal, payload=payload)
        assert session.scalar(select(IntegrationAnalysisReport)) is None


@pytest.mark.parametrize("stored_name,draft_name", [
    ("Alice\u200b Secret", "Alice Secret"),
    ("A\u200b\u0301lice Secret", "Álice Secret"),
])
def test_unicode_formatting_in_stored_name_cannot_bypass_draft_privacy(analysis_context, stored_name, draft_name):
    payload = analysis_payload(analysis_context)
    payload.inferences[0].text = f"{draft_name} may fit the Python role."
    with analysis_context.database.session_factory() as session:
        principal = _principal(session, analysis_context)
        candidate = session.get(Candidate, analysis_context.ids["alpha"])
        candidate.display_name = stored_name
        session.commit()
        with pytest.raises(
            IntegrationAnalysisError, match="integration_sensitive_draft_not_supported"
        ):
            prepare_analysis_draft(session, principal, payload=payload)
        assert session.scalar(select(IntegrationAnalysisReport)) is None


def test_owner_isolation_source_change_and_lifecycle_change(analysis_context):
    with analysis_context.database.session_factory() as session:
        principal = _principal(session, analysis_context)
        prepared = prepare_analysis_draft(
            session,
            principal,
            payload=analysis_payload(analysis_context),
        )
        saved = confirm_prepared(session, analysis_context, prepared)
        session.commit()
        other_auth = AuthPrincipal(
            user=SimpleNamespace(id=str(uuid4())),
            membership=SimpleNamespace(id=str(uuid4()), role="admin"),
            organization=principal.auth.organization,
            plan=principal.auth.plan,
        )
        other = replace(
            principal,
            auth=other_auth,
            grant_id=str(uuid4()),
            credential_id=str(uuid4()),
        )
        assert (
            list_analysis_drafts(
                session,
                other,
                settings=analysis_context.settings,
            ).items
            == []
        )
        with pytest.raises(
            IntegrationAnalysisError, match="integration_resource_not_found"
        ):
            get_analysis_draft(session, other, report_id=saved.id)

        resume = session.get(Resume, analysis_context.alpha_profile.resume_id)
        resume.facts_version = 2
        session.commit()
        changed = get_analysis_draft(session, principal, report_id=saved.id)
        assert changed.source_status == "source_changed"
        assert changed.referenced_facts[0].facts_version == 1

        resume.lifecycle_version += 1
        session.commit()
        with pytest.raises(
            IntegrationAnalysisError, match="integration_resource_not_found"
        ):
            get_analysis_draft(session, principal, report_id=saved.id)


def test_analysis_cursor_is_bound_to_owner_and_query(analysis_context):
    with analysis_context.database.session_factory() as session:
        principal = _principal(session, analysis_context)
        for title in ("First private draft", "Second private draft"):
            prepared = prepare_analysis_draft(
                session,
                principal,
                payload=analysis_payload(analysis_context, title=title),
            )
            confirm_prepared(session, analysis_context, prepared)
        first = list_analysis_drafts(
            session,
            principal,
            settings=analysis_context.settings,
            limit=1,
        )
        assert first.next_cursor is not None
        second = list_analysis_drafts(
            session,
            principal,
            settings=analysis_context.settings,
            limit=1,
            cursor=first.next_cursor,
        )
        assert len(second.items) == 1
        other_auth = AuthPrincipal(
            user=SimpleNamespace(id=str(uuid4())),
            membership=SimpleNamespace(id=str(uuid4()), role="admin"),
            organization=principal.auth.organization,
            plan=principal.auth.plan,
        )
        other = replace(principal, auth=other_auth, grant_id=str(uuid4()))
        for owner, limit in ((other, 1), (principal, 2)):
            with pytest.raises(
                IntegrationAnalysisError, match="integration_invalid_cursor"
            ):
                list_analysis_drafts(
                    session,
                    owner,
                    settings=analysis_context.settings,
                    limit=limit,
                    cursor=first.next_cursor,
                )


def test_audit_failure_rolls_back_draft_and_releases_lease(
    analysis_context, monkeypatch
):
    from app.services import integration_limit_service

    calls = 0
    real_record = integration_limit_service.record_integration_audit

    def fail_second_audit(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("synthetic audit outage")
        return real_record(*args, **kwargs)

    monkeypatch.setattr(
        integration_limit_service,
        "record_integration_audit",
        fail_second_audit,
    )
    payload = analysis_payload(analysis_context)
    with analysis_context.database.session_factory() as session:
        principal = _principal(session, analysis_context)
        principal = replace(
            principal,
            required_scopes=required_save_scopes(payload),
        )
        with pytest.raises(
            IntegrationAccessError, match="integration_audit_unavailable"
        ):
            execute_integration_read(
                session,
                principal=principal,
                settings=analysis_context.settings,
                action="integration.analysis.prepare",
                resource_type="analysis_confirmation",
                read=lambda: prepare_analysis_draft(
                    session,
                    principal,
                    payload=payload,
                ),
                resource_ids=lambda value: (value.id,),
                candidate_ids=lambda _value: (analysis_context.ids["alpha"],),
            )

    with analysis_context.database.session_factory() as session:
        _principal(session, analysis_context)
        assert session.scalar(select(IntegrationAnalysisReport)) is None
        leases = session.scalars(select(IntegrationRequestLease)).all()
        assert len(leases) == 1
        assert leases[0].released_at is not None
