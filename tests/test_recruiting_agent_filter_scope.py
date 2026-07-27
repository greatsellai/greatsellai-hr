from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace

from sqlalchemy import select

from app.models import (
    JobMatchBatch,
    JobVersion,
    RecruitingAgentCandidateSet,
    RecruitingAgentCandidateSetItem,
    RecruitingAgentConversation,
    TalentSearchProfile,
    TalentSearchProfileRevision,
    utcnow,
)
from app.services import recruiting_agent_service, talent_search_profile_service
from app.tenant_scope import (
    bypass_organization_scope,
    clear_organization_context,
    set_organization_context,
)
from test_recruiting_agent import _install_agent_profile_provider_stub
from test_talent_search_profile_api import _save_ready_resume
from test_tenant_isolation import _register_and_login, workspace_clients


def _workspace_id(client) -> str:
    response = client.get("/v1/auth/session")
    assert response.status_code == 200, response.text
    return str(response.json()["organization"]["organization_id"])


def _seed_confirmed_profile_for_conversation(
    client,
    *,
    conversation_id: str,
    hard_filters: dict[str, object],
) -> tuple[str, str]:
    """Attach a minimal confirmed profile without storing any candidate text."""

    organization_id = _workspace_id(client)
    database = client.app.state.database
    with database.session_factory() as session:
        set_organization_context(session, organization_id)
        try:
            conversation = session.get(RecruitingAgentConversation, conversation_id)
            assert conversation is not None
            profile = TalentSearchProfile(
                title="Scoped profile fixture",
                original_request="Synthetic profile fixture.",
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
                title="Scoped profile fixture",
                summary="Synthetic profile fixture.",
                hard_filters=hard_filters,
                verification_requirements=[],
                preferred_requirements=[],
                aliases=[],
                clarifying_questions=[],
                confirmed_at=utcnow(),
            )
            session.add(revision)
            session.flush()
            conversation.active_talent_profile_id = profile.id
            conversation.active_talent_profile_revision_id = revision.id
            session.commit()
            return profile.id, revision.id
        finally:
            clear_organization_context(session)


def test_filter_scope_reconstructs_all_server_pages_without_model(
    ai_client,
    monkeypatch,
) -> None:
    """A browser page/cursor never becomes the frozen Agent membership."""

    calls: list[tuple[int, str | None, str | None]] = []

    def fake_search(_session, request, **_kwargs):
        calls.append((request.limit, request.cursor, request.score_template_id))
        if request.cursor is None:
            return SimpleNamespace(
                items=[
                    SimpleNamespace(resume_id="opaque-resume-1"),
                    SimpleNamespace(resume_id="opaque-resume-2"),
                ],
                next_cursor="server-page-2",
            )
        assert request.cursor == "server-page-2"
        return SimpleNamespace(
            items=[SimpleNamespace(resume_id="opaque-resume-3")],
            next_cursor=None,
        )

    monkeypatch.setattr(recruiting_agent_service, "search_candidates", fake_search)
    monkeypatch.setattr(
        recruiting_agent_service,
        "_model_completion",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("filter-scope binding must not invoke an AI model")
        ),
    )

    response = ai_client.post(
        "/v1/recruiting-agent/conversations/filter-scope",
        json={
            "filter": {
                "limit": 1,
                "cursor": "browser-page-that-must-be-ignored",
                "score_template_id": "stale-score-template",
            },
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert calls == [(100, None, None), (100, "server-page-2", None)]
    assert payload["active_context"]["candidate_set_source"] == "candidate_filter"
    database = ai_client.app.state.database
    with database.session_factory() as session:
        with bypass_organization_scope(session):
            conversation = session.get(
                RecruitingAgentConversation,
                payload["conversation_id"],
            )
            assert conversation is not None
            candidate_set = session.get(
                RecruitingAgentCandidateSet,
                conversation.active_candidate_set_id,
            )
            assert candidate_set is not None
            assert candidate_set.source_kind == "candidate_filter"
            stored_ids = list(
                session.scalars(
                    select(RecruitingAgentCandidateSetItem.resume_id)
                    .where(
                        RecruitingAgentCandidateSetItem.candidate_set_id
                        == candidate_set.id
                    )
                    .order_by(RecruitingAgentCandidateSetItem.ordinal)
                ).all()
            )
    assert stored_ids == ["opaque-resume-1", "opaque-resume-2", "opaque-resume-3"]


def test_scoped_profile_run_intersects_frozen_filter_and_never_reuses_global_run(
    ai_client,
    monkeypatch,
) -> None:
    """A confirmed profile recalls only the bound sidebar result, not all resumes."""

    global_resume_id = _save_ready_resume(
        ai_client,
        skills=["Python"],
        experience_types=["employment"],
    )
    scoped_resume_id = _save_ready_resume(
        ai_client,
        skills=["Python"],
        experience_types=["internship"],
    )
    bound = ai_client.post(
        "/v1/recruiting-agent/conversations/filter-scope",
        json={"filter": {"experience_types_all_of": ["internship"], "limit": 1}},
    )
    assert bound.status_code == 200, bound.text
    bound_payload = bound.json()
    assert bound_payload["active_context"]["candidate_count"] == 1

    _install_agent_profile_provider_stub(monkeypatch)
    monkeypatch.setattr(
        recruiting_agent_service,
        "_model_completion",
        lambda **_kwargs: {
            "content": None,
            "tool_calls": [
                {
                    "id": "draft-scoped-profile",
                    "type": "function",
                    "function": {
                        "name": "draft_talent_search_profile",
                        "arguments": json.dumps({}),
                    },
                }
            ],
        },
    )
    drafted = ai_client.post(
        "/v1/recruiting-agent/turns",
        json={
            "message": "请基于当前初筛结果做人才画像精筛。",
            "conversation_id": bound_payload["conversation_id"],
            "context_version": bound_payload["context_version"],
        },
    )
    assert drafted.status_code == 200, drafted.text
    draft_payload = drafted.json()
    assert draft_payload["active_context"]["candidate_set_source"] == "candidate_filter"
    assert draft_payload["active_context"]["candidate_count"] == 1
    profile = draft_payload["talent_profile"]
    assert profile is not None
    revision_id = profile["current_revision"]["revision_id"]
    confirmed = ai_client.post(
        f"/v1/talent-search-profiles/{profile['profile_id']}/confirm",
        json={"revision_id": revision_id},
    )
    assert confirmed.status_code == 200, confirmed.text
    # The drawer rebinds a profile after confirm/refine. That must retain the
    # current explicit filter scope instead of silently widening to all data.
    rebound = ai_client.post(
        "/v1/recruiting-agent/conversations/context",
        json={
            "context_ref": {
                "kind": "talent_search_profile",
                "profile_id": profile["profile_id"],
                "revision_id": revision_id,
            },
            "conversation_id": draft_payload["conversation_id"],
            "context_version": draft_payload["context_version"],
        },
    )
    assert rebound.status_code == 200, rebound.text
    rebound_payload = rebound.json()
    assert rebound_payload["active_context"]["candidate_set_source"] == "candidate_filter"
    assert rebound_payload["active_context"]["candidate_count"] == 1

    enqueued: list[list[str]] = []

    def fake_enqueue(session, *, job_version_id, resume_ids, **_kwargs):
        enqueued.append(list(resume_ids))
        job_version = session.get(JobVersion, job_version_id)
        assert job_version is not None
        batch = JobMatchBatch(
            organization_id=job_version.organization_id,
            job_version_id=job_version.id,
            total_count=len(resume_ids),
            completed_count=0,
            failed_count=0,
            status="queued",
            max_attempts=1,
        )
        session.add(batch)
        session.flush()
        return SimpleNamespace(
            batch_id=batch.id,
            status=batch.status,
        )

    monkeypatch.setattr(
        talent_search_profile_service,
        "enqueue_job_version_match_batch",
        fake_enqueue,
    )
    scoped = ai_client.post(
        f"/v1/recruiting-agent/conversations/talent-profiles/{profile['profile_id']}/runs",
        json={
            "revision_id": revision_id,
            "conversation_id": rebound_payload["conversation_id"],
            "context_version": rebound_payload["context_version"],
            "limit": 20,
        },
    )
    assert scoped.status_code == 200, scoped.text
    scoped_payload = scoped.json()
    assert scoped_payload["scope_kind"] == "candidate_filter"
    assert scoped_payload["scope_candidate_count"] == 1
    assert scoped_payload["candidate_recall"]["total_count"] == 1
    assert {
        item["resume_id"] for item in scoped_payload["candidate_recall"]["items"]
    } == {scoped_resume_id}
    assert scoped_payload["active_context"]["candidate_set_source"] == "talent_search_run"
    assert enqueued == [[scoped_resume_id]]

    global_run = ai_client.post(
        f"/v1/talent-search-profiles/{profile['profile_id']}/runs",
        json={"revision_id": revision_id, "limit": 20},
    )
    assert global_run.status_code == 200, global_run.text
    global_payload = global_run.json()
    assert global_payload["scope_kind"] == "global"
    assert global_payload["run_id"] != scoped_payload["run_id"]
    assert {
        item["resume_id"] for item in global_payload["candidate_recall"]["items"]
    } == {global_resume_id, scoped_resume_id}
    assert enqueued[0] == [scoped_resume_id]
    assert set(enqueued[1]) == {global_resume_id, scoped_resume_id}


def test_scoped_zero_result_diagnostics_are_limited_to_frozen_scope(
    ai_client,
) -> None:
    """A zero result explains the scoped funnel, never a workspace-wide one."""

    _save_ready_resume(
        ai_client,
        skills=["Python"],
        experience_types=["employment"],
    )
    _save_ready_resume(
        ai_client,
        skills=["Python"],
        experience_types=["internship"],
    )
    bound = ai_client.post(
        "/v1/recruiting-agent/conversations/filter-scope",
        json={"filter": {"experience_types_all_of": ["internship"]}},
    )
    assert bound.status_code == 200, bound.text
    bound_payload = bound.json()
    profile_id, revision_id = _seed_confirmed_profile_for_conversation(
        ai_client,
        conversation_id=bound_payload["conversation_id"],
        hard_filters={"skills_all_of": ["Rust"]},
    )

    scoped = ai_client.post(
        f"/v1/recruiting-agent/conversations/talent-profiles/{profile_id}/runs",
        json={
            "revision_id": revision_id,
            "conversation_id": bound_payload["conversation_id"],
            "context_version": bound_payload["context_version"],
        },
    )
    assert scoped.status_code == 200, scoped.text
    payload = scoped.json()
    assert payload["scope_kind"] == "candidate_filter"
    assert payload["total_recalled_count"] == 0
    diagnostics = payload["recall_diagnostics"]
    assert diagnostics is not None
    assert diagnostics["eligible_resume_count"] == 1
    assert diagnostics["strict_match_count"] == 0


def test_filter_scope_rejects_stale_cross_tenant_and_expired_scope(
    workspace_clients,
) -> None:
    """Private scopes remain owner/tenant/TTL/version bound before any recall."""

    client_a, client_b = workspace_clients
    _register_and_login(
        client_a,
        organization_name="Scoped filter tenant A",
        full_name="Scoped A",
        email="scoped-filter-a@example.test",
        password="tenant-test-password",
    )
    _register_and_login(
        client_b,
        organization_name="Scoped filter tenant B",
        full_name="Scoped B",
        email="scoped-filter-b@example.test",
        password="tenant-test-password",
    )
    bound = client_a.post(
        "/v1/recruiting-agent/conversations/filter-scope",
        json={"filter": {}},
    )
    assert bound.status_code == 200, bound.text
    payload = bound.json()

    cross_tenant = client_b.post(
        "/v1/recruiting-agent/conversations/filter-scope",
        json={
            "filter": {},
            "conversation_id": payload["conversation_id"],
            "context_version": payload["context_version"],
        },
    )
    assert cross_tenant.status_code == 404, cross_tenant.text
    assert cross_tenant.json()["detail"] == "agent_conversation_not_found"

    stale = client_a.post(
        "/v1/recruiting-agent/conversations/filter-scope",
        json={
            "filter": {},
            "conversation_id": payload["conversation_id"],
            "context_version": payload["context_version"] - 1,
        },
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["detail"] == "agent_conversation_stale"

    profile_id, revision_id = _seed_confirmed_profile_for_conversation(
        client_a,
        conversation_id=payload["conversation_id"],
        hard_filters={},
    )
    database = client_a.app.state.database
    with database.session_factory() as session:
        with bypass_organization_scope(session):
            conversation = session.get(
                RecruitingAgentConversation,
                payload["conversation_id"],
            )
            assert conversation is not None
            candidate_set = session.get(
                RecruitingAgentCandidateSet,
                conversation.active_candidate_set_id,
            )
            assert candidate_set is not None
            candidate_set.expires_at = utcnow() - timedelta(seconds=1)
            session.commit()

    expired = client_a.post(
        f"/v1/recruiting-agent/conversations/talent-profiles/{profile_id}/runs",
        json={
            "revision_id": revision_id,
            "conversation_id": payload["conversation_id"],
            "context_version": payload["context_version"],
        },
    )
    assert expired.status_code == 404, expired.text
    assert expired.json()["detail"] == "agent_filter_scope_not_found"
