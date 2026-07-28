from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm.exc import StaleDataError

from app.config import AppSettings
from app.database import Database
from app.main import create_app
from app.models import (
    Organization,
    RecruitingAgentCandidateSet,
    RecruitingAgentCandidateSetItem,
    RecruitingAgentConversation,
    RecruitingAgentConversationTurn,
    TalentSearchProfile,
    TalentSearchProfileRevision,
    TalentSearchRun,
    UserAccount,
    utcnow,
)
from app.services import recruiting_agent_service
from app.tenant_scope import (
    bypass_organization_scope,
    clear_organization_context,
    set_organization_context,
)
from test_resume_flow import (
    create_candidate,
    replace_page_evidence,
    upload_text_resume,
)


class _StopWorkerLoop(RuntimeError):
    """End one patched worker iteration after the cleanup hook is reached."""


def _new_database() -> Database:
    database = Database("sqlite://")
    database.create_all()
    return database


def _new_file_database(tmp_path: Path) -> Database:
    database_path = tmp_path / "agent-context-concurrency.sqlite3"
    database = Database(f"sqlite:///{database_path.as_posix()}")
    database.create_all()
    return database


def _seed_conversation(
    database: Database,
    *,
    label: str,
    expires_in: timedelta,
    item_count: int,
) -> tuple[str, str, tuple[str, ...]]:
    """Create private Agent state plus one bounded recruiter-visible turn."""

    with database.session_factory() as session:
        organization = Organization(name=f"Agent context retention {label}")
        owner = UserAccount(
            email=f"agent-context-retention-{label}@example.test",
            email_key=f"agent-context-retention-{label}@example.test",
            full_name="Retention test owner",
            password_hash="not-used-by-this-test",
            email_verified_at=utcnow(),
        )
        session.add_all((organization, owner))
        session.flush()

        set_organization_context(session, organization.id)
        try:
            conversation = RecruitingAgentConversation(
                owner_user_id=owner.id,
                expires_at=utcnow() + expires_in,
            )
            session.add(conversation)
            session.flush()
            candidate_set = RecruitingAgentCandidateSet(
                conversation_id=conversation.id,
                source_kind="agent_search",
                source_ref_id=None,
                expires_at=conversation.expires_at,
            )
            session.add(candidate_set)
            session.flush()
            item_ids = tuple(
                f"opaque-resume-{label}-{ordinal}"
                for ordinal in range(1, item_count + 1)
            )
            session.add_all(
                RecruitingAgentCandidateSetItem(
                    candidate_set_id=candidate_set.id,
                    resume_id=resume_id,
                    ordinal=ordinal,
                )
                for ordinal, resume_id in enumerate(item_ids, start=1)
            )
            session.add(
                RecruitingAgentConversationTurn(
                    conversation_id=conversation.id,
                    context_version=conversation.context_version,
                    user_message="Synthetic retention prompt.",
                    assistant_message="Synthetic retention reply.",
                )
            )
            conversation.active_candidate_set_id = candidate_set.id
            session.commit()
            return conversation.id, candidate_set.id, item_ids
        finally:
            clear_organization_context(session)


def _exists_anywhere(
    database: Database,
    model: type[RecruitingAgentConversation] | type[RecruitingAgentCandidateSet],
    identifier: str,
) -> bool:
    with database.session_factory() as session:
        with bypass_organization_scope(session):
            return (
                session.scalar(select(model.id).where(model.id == identifier))
                is not None
            )


def _item_count_anywhere(database: Database, candidate_set_id: str) -> int:
    with database.session_factory() as session:
        with bypass_organization_scope(session):
            return int(
                session.scalar(
                    select(func.count(RecruitingAgentCandidateSetItem.id)).where(
                        RecruitingAgentCandidateSetItem.candidate_set_id == candidate_set_id
                    )
                )
                or 0
            )


def _turn_count_anywhere(database: Database, conversation_id: str) -> int:
    with database.session_factory() as session:
        with bypass_organization_scope(session):
            return int(
                session.scalar(
                    select(func.count(RecruitingAgentConversationTurn.id)).where(
                        RecruitingAgentConversationTurn.conversation_id == conversation_id
                    )
                )
                or 0
            )


def _save_ready_agent_resume(client: TestClient) -> tuple[str, str]:
    """Create a synthetic ready resume usable by the Agent's normal search."""

    candidate_id = create_candidate(client)
    resume_id = upload_text_resume(client, candidate_id)
    replace_page_evidence(
        client,
        resume_id,
        (
            "\u6559\u80b2\u7ecf\u5386 \u5317\u4eac\u5927\u5b66 \u672c\u79d1\u3002"
            "\u5927\u5b66\u82f1\u8bed\u56db\u7ea7 CET-4 \u6210\u7ee9 520\u3002"
        ),
    )
    saved = client.put(
        f"/v1/resumes/{resume_id}/facts",
        json={
            "facts": {
                "schema_version": "resume_facts.v2",
                "education": [
                    {
                        "school_name_raw": "\u5317\u4eac\u5927\u5b66",
                        "degree": "bachelor",
                        "evidence_block_ids": ["page-001"],
                    }
                ],
            }
        },
    )
    assert saved.status_code == 200, saved.text
    return candidate_id, resume_id


def _register_verified_workspace(
    client: TestClient,
    *,
    organization_name: str,
    email: str,
) -> dict[str, object]:
    password = "agent-context-tenant-test-password"
    registered = client.post(
        "/v1/auth/register",
        json={
            "organization_name": organization_name,
            "full_name": "Agent context test admin",
            "email": email,
            "password": password,
        },
    )
    assert registered.status_code == 201, registered.text
    provider = client.app.state.transactional_email_provider
    delivery = next(
        item for item in reversed(provider.deliveries) if item.recipient == email
    )
    token = parse_qs(urlsplit(delivery.verification_url).query)["token"][0]
    verified = client.post("/v1/auth/email-verification/complete", json={"token": token})
    assert verified.status_code == 200, verified.text
    logged_in = client.post(
        "/v1/auth/login",
        json={"email": email, "password": password},
    )
    assert logged_in.status_code == 200, logged_in.text
    session = client.get("/v1/auth/session")
    assert session.status_code == 200, session.text
    return session.json()


def _seed_talent_search_run_for_workspace(
    client: TestClient,
    *,
    organization_id: str,
    recalled_resume_ids: tuple[str, ...] = (),
) -> str:
    """Seed one synthetic, server-owned RAG run in the specified workspace."""

    database = client.app.state.database
    with database.session_factory() as session:
        set_organization_context(session, organization_id)
        try:
            profile = TalentSearchProfile(
                title="Synthetic foreign profile",
                original_request="Synthetic test-only request.",
                status="confirmed",
                current_revision_number=1,
                confirmed_revision_number=1,
            )
            session.add(profile)
            session.flush()
            revision = TalentSearchProfileRevision(
                profile_id=profile.id,
                revision_number=1,
                status="confirmed",
                title="Synthetic foreign profile",
                summary="Synthetic test-only summary.",
                hard_filters={},
                verification_requirements=[],
                preferred_requirements=[],
                aliases=[],
                clarifying_questions=[],
                confirmed_at=utcnow(),
            )
            session.add(revision)
            session.flush()
            run = TalentSearchRun(
                profile_id=profile.id,
                revision_id=revision.id,
                status="completed",
                recalled_resume_ids=list(recalled_resume_ids),
                recall_diagnostics={},
                total_recalled_count=len(recalled_resume_ids),
            )
            session.add(run)
            session.commit()
            return run.id
        finally:
            clear_organization_context(session)


def test_talent_search_run_binds_agent_context_without_an_ai_turn(
    ai_client: TestClient,
    monkeypatch,
) -> None:
    """A visible talent-search result becomes a private scope without model cost."""

    _, resume_id = _save_ready_agent_resume(ai_client)
    workspace = ai_client.get("/v1/auth/session")
    assert workspace.status_code == 200, workspace.text
    organization_id = str(workspace.json()["organization"]["organization_id"])
    run_id = _seed_talent_search_run_for_workspace(
        ai_client,
        organization_id=organization_id,
        recalled_resume_ids=(resume_id,),
    )
    job = ai_client.post(
        "/v1/jobs",
        json={
            "title": "Context bind job",
            "jd_text": "Python service development.",
            "requirements": {"must_have": ["Python"], "preferred": []},
        },
    )
    assert job.status_code == 200, job.text

    def model_must_not_run(*args, **kwargs):
        raise AssertionError("binding an Agent context must not invoke the model")

    monkeypatch.setattr(
        "app.services.recruiting_agent_service._model_completion",
        model_must_not_run,
    )
    bound = ai_client.post(
        "/v1/recruiting-agent/conversations/context",
        json={
            "context_ref": {"kind": "talent_search_run", "run_id": run_id},
            "job_version_id": job.json()["job_version_id"],
        },
    )

    assert bound.status_code == 200, bound.text
    payload = bound.json()
    assert payload["active_context"]["candidate_set_source"] == "talent_search_run"
    assert payload["active_context"]["candidate_count"] == 1
    assert payload["active_context"]["active_job_version_id"] == job.json()["job_version_id"]

    restored = ai_client.get(
        f"/v1/recruiting-agent/conversations/{payload['conversation_id']}"
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["context_version"] == payload["context_version"]
    restored_context = restored.json()["active_context"]
    bound_context = payload["active_context"]
    for key in (
        "candidate_set_source",
        "candidate_count",
        "active_job_version_id",
        "active_job_title",
    ):
        assert restored_context[key] == bound_context[key]

    stale = ai_client.post(
        "/v1/recruiting-agent/conversations/context",
        json={
            "context_ref": {"kind": "talent_search_run", "run_id": run_id},
            "conversation_id": payload["conversation_id"],
            "context_version": payload["context_version"] - 1,
        },
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["detail"] == "agent_conversation_stale"

    missing_version = ai_client.post(
        "/v1/recruiting-agent/conversations/context",
        json={
            "context_ref": {"kind": "talent_search_run", "run_id": run_id},
            "conversation_id": payload["conversation_id"],
        },
    )
    assert missing_version.status_code == 422, missing_version.text


def test_historic_talent_search_run_clears_an_unrelated_active_profile(
    ai_client: TestClient,
) -> None:
    """A historic recall can scope candidates, but never a later refinement."""

    workspace = ai_client.get("/v1/auth/session")
    assert workspace.status_code == 200, workspace.text
    organization_id = str(workspace.json()["organization"]["organization_id"])
    historic_run_id = _seed_talent_search_run_for_workspace(
        ai_client,
        organization_id=organization_id,
    )
    database = ai_client.app.state.database
    with database.session_factory() as session:
        set_organization_context(session, organization_id)
        try:
            historic_run = session.scalar(
                select(TalentSearchRun).where(TalentSearchRun.id == historic_run_id)
            )
            assert historic_run is not None
            historic_profile = session.scalar(
                select(TalentSearchProfile).where(
                    TalentSearchProfile.id == historic_run.profile_id
                )
            )
            assert historic_profile is not None
            # The run is now from revision 1 while the profile has advanced to
            # revision 2.  It may still be viewed, but is no longer refinable.
            historic_profile.current_revision_number = 2
            historic_profile.status = "draft"
            session.add(
                TalentSearchProfileRevision(
                    profile_id=historic_profile.id,
                    revision_number=2,
                    status="draft",
                    title="Historic profile revision two",
                    summary="A newer draft replaces the run's revision.",
                    hard_filters={},
                    verification_requirements=[],
                    preferred_requirements=[],
                    aliases=[],
                    clarifying_questions=[],
                )
            )
            current_profile = TalentSearchProfile(
                title="Different active profile",
                original_request="A separate current draft.",
                status="draft",
                current_revision_number=1,
            )
            session.add(current_profile)
            session.flush()
            current_revision = TalentSearchProfileRevision(
                profile_id=current_profile.id,
                revision_number=1,
                status="draft",
                title="Different active profile",
                summary="This is not the historic run profile.",
                hard_filters={},
                verification_requirements=[],
                preferred_requirements=[],
                aliases=[],
                clarifying_questions=[],
            )
            session.add(current_revision)
            session.commit()
            current_profile_id = current_profile.id
            current_revision_id = current_revision.id
        finally:
            clear_organization_context(session)

    active_profile = ai_client.post(
        "/v1/recruiting-agent/conversations/context",
        json={
            "context_ref": {
                "kind": "talent_search_profile",
                "profile_id": current_profile_id,
                "revision_id": current_revision_id,
            },
        },
    )
    assert active_profile.status_code == 200, active_profile.text
    assert (
        active_profile.json()["active_context"]["active_talent_profile"]["profile_id"]
        == current_profile_id
    )

    historic_run = ai_client.post(
        "/v1/recruiting-agent/conversations/context",
        json={
            "context_ref": {
                "kind": "talent_search_run",
                "run_id": historic_run_id,
            },
            "conversation_id": active_profile.json()["conversation_id"],
            "context_version": active_profile.json()["context_version"],
        },
    )

    assert historic_run.status_code == 200, historic_run.text
    context = historic_run.json()["active_context"]
    assert context["candidate_set_source"] == "talent_search_run"
    assert context["active_talent_profile"] is None


def test_worker_purges_expired_agent_contexts_across_workspaces_and_cascades_items() -> None:
    """A tenant-agnostic worker must remove only expired private state.

    The two expired conversations intentionally live in separate workspaces.
    Their candidate-set membership is opaque, but must be deleted with the
    parent rather than retained as an orphaned reference.
    """

    database = _new_database()
    try:
        expired_a_id, expired_a_set_id, _ = _seed_conversation(
            database,
            label="expired-a",
            expires_in=timedelta(minutes=-1),
            item_count=2,
        )
        expired_b_id, expired_b_set_id, _ = _seed_conversation(
            database,
            label="expired-b",
            expires_in=timedelta(minutes=-1),
            item_count=1,
        )
        active_id, active_set_id, _ = _seed_conversation(
            database,
            label="active",
            expires_in=timedelta(hours=1),
            item_count=1,
        )

        # A bounded worker pass is allowed to remove one conversation at a
        # time, but it must be safe to invoke again without a tenant context.
        assert (
            recruiting_agent_service.purge_expired_recruiting_agent_conversations(
                database,
                limit=1,
            )
            == 1
        )
        assert (
            recruiting_agent_service.purge_expired_recruiting_agent_conversations(
                database,
                limit=1,
            )
            == 1
        )
        assert (
            recruiting_agent_service.purge_expired_recruiting_agent_conversations(
                database,
                limit=1,
            )
            == 0
        )

        assert not _exists_anywhere(
            database,
            RecruitingAgentConversation,
            expired_a_id,
        )
        assert not _exists_anywhere(
            database,
            RecruitingAgentConversation,
            expired_b_id,
        )
        assert not _exists_anywhere(
            database,
            RecruitingAgentCandidateSet,
            expired_a_set_id,
        )
        assert not _exists_anywhere(
            database,
            RecruitingAgentCandidateSet,
            expired_b_set_id,
        )
        assert _item_count_anywhere(database, expired_a_set_id) == 0
        assert _item_count_anywhere(database, expired_b_set_id) == 0
        assert _turn_count_anywhere(database, expired_a_id) == 0
        assert _turn_count_anywhere(database, expired_b_id) == 0

        # Expiry cleanup is not a global Agent-state wipe. The unexpired
        # conversation and its opaque scope remain usable.
        assert _exists_anywhere(
            database,
            RecruitingAgentConversation,
            active_id,
        )
        assert _exists_anywhere(
            database,
            RecruitingAgentCandidateSet,
            active_set_id,
        )
        assert _item_count_anywhere(database, active_set_id) == 1
        assert _turn_count_anywhere(database, active_id) == 1
    finally:
        database.dispose()


def test_worker_invokes_agent_context_expiry_cleanup(monkeypatch, tmp_path: Path) -> None:
    """The periodic worker loop reaches the Agent-state hard-delete hook."""

    from app import ai_extraction_worker

    database = _new_file_database(tmp_path)
    calls: list[tuple[object, object]] = []
    try:
        monkeypatch.setattr(
            ai_extraction_worker,
            "_create_worker_database",
            lambda settings: database,
        )
        for dependency_name in (
            "run_transactional_email_outbox_worker_once",
            "run_mailbox_background_job_worker_once",
                "run_document_extraction_worker_once",
                "run_ai_extraction_worker_once",
                "run_resume_summary_worker_once",
                "run_job_match_batch_worker_once",
            "run_resume_score_batch_worker_once",
            "enqueue_due_mailbox_sync_jobs",
            "cleanup_due_mailbox_retention",
            "run_due_candidate_data_retention_cleanup",
            "run_candidate_data_purge_worker_once",
            "run_candidate_data_export_worker_once",
            "cleanup_expired_candidate_data_exports",
        ):
            monkeypatch.setattr(
                ai_extraction_worker,
                dependency_name,
                lambda *args, **kwargs: False,
            )

        def stop_after_cleanup(*args, **kwargs):
            calls.append((args, kwargs))
            raise _StopWorkerLoop()

        monkeypatch.setattr(
            ai_extraction_worker,
            "purge_expired_recruiting_agent_conversations",
            stop_after_cleanup,
        )
        with pytest.raises(_StopWorkerLoop):
            ai_extraction_worker.run_forever(object())

        assert len(calls) == 1
        positional, keyword = calls[0]
        assert positional == (database,) or keyword.get("database") is database
    finally:
        database.dispose()


def test_expiry_purge_treats_a_stale_delete_as_already_handled(
    monkeypatch,
) -> None:
    """One competing explicit delete cannot abort cleanup for other workspaces."""

    database = _new_database()
    try:
        conversation_id, _, _ = _seed_conversation(
            database,
            label="stale-cleanup",
            expires_in=timedelta(minutes=-1),
            item_count=1,
        )
        original_commit = recruiting_agent_service.Session.commit
        stale_once = True

        def raise_stale_once(session):
            nonlocal stale_once
            if stale_once:
                stale_once = False
                raise StaleDataError("simulated concurrent delete")
            return original_commit(session)

        monkeypatch.setattr(
            recruiting_agent_service.Session,
            "commit",
            raise_stale_once,
        )

        assert (
            recruiting_agent_service.purge_expired_recruiting_agent_conversations(
                database,
                limit=1,
            )
            == 0
        )
        assert _exists_anywhere(
            database,
            RecruitingAgentConversation,
            conversation_id,
        )
    finally:
        database.dispose()


def test_replayed_context_version_rejects_a_second_state_advancing_agent_turn(
    ai_client: TestClient,
    monkeypatch,
) -> None:
    """A replayed browser snapshot cannot overwrite a committed transition.

    This deterministic request replay covers the API conflict contract. The
    database-level compare-and-swap implementation separately protects the
    truly simultaneous transaction case.
    """

    first_job = ai_client.post(
        "/v1/jobs",
        json={
            "title": "Context revision A",
            "jd_text": "Python service development.",
            "requirements": {"must_have": ["Python"], "preferred": []},
        },
    )
    second_job = ai_client.post(
        "/v1/jobs",
        json={
            "title": "Context revision B",
            "jd_text": "Distributed systems engineering.",
            "requirements": {"must_have": ["Distributed systems"], "preferred": []},
        },
    )
    assert first_job.status_code == 200, first_job.text
    assert second_job.status_code == 200, second_job.text

    completion_calls = 0

    def fake_completion(*args, **kwargs):
        nonlocal completion_calls
        completion_calls += 1
        return {"content": "\u5df2\u5904\u7406\u5f53\u524d JD\u3002"}

    monkeypatch.setattr(
        "app.services.recruiting_agent_service._model_completion",
        fake_completion,
    )

    initial_turn = ai_client.post(
        "/v1/recruiting-agent/turns",
        json={
            "message": "Use the first job.",
            "job_version_id": first_job.json()["job_version_id"],
        },
    )
    assert initial_turn.status_code == 200, initial_turn.text
    initial_payload = initial_turn.json()

    # Both requests represent the same browser snapshot. The first wins and
    # advances the selected-JD context; the second must fail before it reaches
    # the model, rather than overwriting the active work state.
    competing_turn = {
        "message": "Switch to the second job.",
        "conversation_id": initial_payload["conversation_id"],
        "context_version": initial_payload["context_version"],
        "job_version_id": second_job.json()["job_version_id"],
    }
    winning_turn = ai_client.post("/v1/recruiting-agent/turns", json=competing_turn)
    stale_turn = ai_client.post("/v1/recruiting-agent/turns", json=competing_turn)

    assert winning_turn.status_code == 200, winning_turn.text
    assert winning_turn.json()["context_version"] > initial_payload["context_version"]
    assert stale_turn.status_code == 409, stale_turn.text
    assert stale_turn.json()["detail"] == "agent_conversation_stale"
    assert completion_calls == 2


def test_database_row_version_rejects_parallel_conversation_overwrite(
    tmp_path: Path,
) -> None:
    """Independent database sessions cannot both persist one context version.

    This is the actual concurrent-write guard behind the API's stale-token
    contract. A file-backed SQLite database gives the two sessions independent
    connections, while the optimistic row version has the same expected
    behavior as the production database.
    """

    database = _new_file_database(tmp_path)
    try:
        conversation_id, _, _ = _seed_conversation(
            database,
            label="concurrent",
            expires_in=timedelta(hours=1),
            item_count=0,
        )
        with database.session_factory() as session:
            with bypass_organization_scope(session):
                organization_id = session.scalar(
                    select(RecruitingAgentConversation.organization_id).where(
                        RecruitingAgentConversation.id == conversation_id
                    )
                )
        assert isinstance(organization_id, str) and organization_id

        first_session = database.session_factory()
        second_session = database.session_factory()
        try:
            set_organization_context(first_session, organization_id)
            set_organization_context(second_session, organization_id)
            first_copy = first_session.scalar(
                select(RecruitingAgentConversation).where(
                    RecruitingAgentConversation.id == conversation_id
                )
            )
            second_copy = second_session.scalar(
                select(RecruitingAgentConversation).where(
                    RecruitingAgentConversation.id == conversation_id
                )
            )
            assert first_copy is not None
            assert second_copy is not None
            original_version = first_copy.context_version
            assert second_copy.context_version == original_version

            first_copy.context_version += 1
            first_session.commit()

            second_copy.context_version += 1
            with pytest.raises(StaleDataError):
                second_session.commit()
            second_session.rollback()
        finally:
            clear_organization_context(first_session)
            clear_organization_context(second_session)
            first_session.close()
            second_session.close()

        with database.session_factory() as session:
            with bypass_organization_scope(session):
                persisted = session.scalar(
                    select(RecruitingAgentConversation).where(
                        RecruitingAgentConversation.id == conversation_id
                    )
                )
                assert persisted is not None
                assert persisted.context_version == original_version + 1
    finally:
        database.dispose()


def test_existing_agent_conversation_requires_context_version_before_model(
    ai_client: TestClient,
    monkeypatch,
) -> None:
    """A caller cannot opt out of optimistic concurrency after creation."""

    def valid_completion(*args, **kwargs):
        return {"content": "\u5df2\u5904\u7406\u3002"}

    monkeypatch.setattr(
        "app.services.recruiting_agent_service._model_completion",
        valid_completion,
    )
    created = ai_client.post(
        "/v1/recruiting-agent/turns",
        json={"message": "Start an agent work session."},
    )
    assert created.status_code == 200, created.text

    def model_must_not_run(*args, **kwargs):
        raise AssertionError("missing context_version must fail before the model")

    monkeypatch.setattr(
        "app.services.recruiting_agent_service._model_completion",
        model_must_not_run,
    )
    rejected = ai_client.post(
        "/v1/recruiting-agent/turns",
        json={
            "message": "Try to continue without a concurrency token.",
            "conversation_id": created.json()["conversation_id"],
        },
    )

    assert rejected.status_code == 422, rejected.text


def test_deleted_resume_is_excluded_from_agent_context_ranking_and_batch(
    ai_client: TestClient,
    monkeypatch,
) -> None:
    """Logical candidate deletion wins over an older saved Agent scope."""

    candidate_id, resume_id = _save_ready_agent_resume(ai_client)
    job = ai_client.post(
        "/v1/jobs",
        json={
            "title": "Lifecycle context job",
            "jd_text": "Python service development.",
            "requirements": {"must_have": ["Python"], "preferred": []},
        },
    )
    assert job.status_code == 200, job.text

    initial_steps = [
        {
            "content": None,
            "tool_calls": [
                {
                    "id": "search-lifecycle-context",
                    "type": "function",
                    "function": {
                        "name": "search_candidates",
                        "arguments": (
                            '{"language_credentials_any_of":'
                            '[{"credential_code":"cet4"}]}'
                        ),
                    },
                }
            ],
        },
        {"content": "\u5df2\u4fdd\u5b58\u5f53\u524d\u7b5b\u9009\u8303\u56f4\u3002"},
    ]

    monkeypatch.setattr(
        "app.services.recruiting_agent_service._model_completion",
        lambda *args, **kwargs: initial_steps.pop(0),
    )
    first_turn = ai_client.post(
        "/v1/recruiting-agent/turns",
        json={
            "message": "Find CET-4 candidates.",
            "job_version_id": job.json()["job_version_id"],
        },
    )
    assert first_turn.status_code == 200, first_turn.text
    first_payload = first_turn.json()
    assert first_payload["active_context"]["candidate_count"] == 1

    deleted = ai_client.request(
        "DELETE",
        f"/v1/candidates/{candidate_id}",
        json={"reason": "duplicate"},
    )
    assert deleted.status_code == 202, deleted.text

    restored_context = ai_client.get(
        f"/v1/recruiting-agent/conversations/{first_payload['conversation_id']}"
    )
    assert restored_context.status_code == 200, restored_context.text
    assert restored_context.json()["active_context"]["candidate_count"] == 0

    def unexpected_scope_read(*args, **kwargs):
        raise AssertionError("a deleted resume must not reach scoped ranking")

    def unexpected_batch_enqueue(*args, **kwargs):
        raise AssertionError("a deleted resume must not queue a JD match batch")

    scoped_steps = [
        {
            "content": None,
            "tool_calls": [
                {
                    "id": "rank-deleted-scope",
                    "type": "function",
                    "function": {
                        "name": "get_current_job_ranking_from_active_context",
                        "arguments": "{}",
                    },
                },
                {
                    "id": "batch-deleted-scope",
                    "type": "function",
                    "function": {
                        "name": "start_current_job_match_for_active_context",
                        "arguments": "{}",
                    },
                },
            ],
        },
        {"content": "\u5f53\u524d\u8303\u56f4\u5df2\u66f4\u65b0\u3002"},
    ]
    monkeypatch.setattr(
        "app.services.recruiting_agent_service._model_completion",
        lambda *args, **kwargs: scoped_steps.pop(0),
    )
    monkeypatch.setattr(
        "app.services.recruiting_agent_service.list_job_version_matches",
        unexpected_scope_read,
    )
    monkeypatch.setattr(
        "app.services.recruiting_agent_service.enqueue_job_version_match_batch",
        unexpected_batch_enqueue,
    )
    scoped_turn = ai_client.post(
        "/v1/recruiting-agent/turns",
        json={
            "message": "Rank and match the saved candidates.",
            "conversation_id": first_payload["conversation_id"],
            "context_version": first_payload["context_version"],
        },
    )

    assert scoped_turn.status_code == 200, scoped_turn.text
    assert scoped_turn.json()["active_context"]["candidate_count"] == 0
    assert scoped_turn.json()["candidates"] == []
    assert scoped_turn.json()["batch_id"] is None
    assert resume_id not in str(scoped_turn.json())


def test_foreign_talent_search_context_is_rejected_before_agent_model(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """An opaque RAG run ID never crosses the authenticated workspace boundary."""

    settings = AppSettings(
        project_dir=tmp_path,
        data_dir=tmp_path / "data",
        upload_dir=tmp_path / "data" / "uploads",
        database_url="sqlite://",
        allow_unauthenticated=False,
        session_secret="agent-context-foreign-run-test-secret",
        deepseek_api_key="unit-test-key",
        deepseek_model="unit-test-model",
        min_text_chars_per_page=20,
        transactional_email_provider="test",
        public_app_url="http://testserver",
    )
    app = create_app(settings)
    with TestClient(app):
        client_a = TestClient(app)
        client_b = TestClient(app)
        try:
            session_a = _register_verified_workspace(
                client_a,
                organization_name="Agent context workspace A",
                email="agent-context-a@example.test",
            )
            session_b = _register_verified_workspace(
                client_b,
                organization_name="Agent context workspace B",
                email="agent-context-b@example.test",
            )
            foreign_run_id = _seed_talent_search_run_for_workspace(
                client_b,
                organization_id=str(session_b["organization"]["organization_id"]),
            )

            def model_must_not_run(*args, **kwargs):
                raise AssertionError("foreign context must fail before the model")

            monkeypatch.setattr(
                "app.services.recruiting_agent_service._model_completion",
                model_must_not_run,
            )
            rejected = client_a.post(
                "/v1/recruiting-agent/turns",
                json={
                    "message": "Use this talent-search result.",
                    "context_ref": {
                        "kind": "talent_search_run",
                        "run_id": foreign_run_id,
                    },
                },
            )

            assert rejected.status_code == 404, rejected.text
            assert rejected.json()["detail"] == "agent_context_reference_not_found"
            bind_rejected = client_a.post(
                "/v1/recruiting-agent/conversations/context",
                json={
                    "context_ref": {
                        "kind": "talent_search_run",
                        "run_id": foreign_run_id,
                    },
                },
            )
            assert bind_rejected.status_code == 404, bind_rejected.text
            assert bind_rejected.json()["detail"] == "agent_context_reference_not_found"
            assert str(session_a["organization"]["organization_id"]) != str(
                session_b["organization"]["organization_id"]
            )
        finally:
            client_a.close()
            client_b.close()
