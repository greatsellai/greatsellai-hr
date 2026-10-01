from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update

from app.models import (
    Candidate,
    IntegrationAnalysisReference,
    IntegrationAnalysisReport,
    Resume,
    ResumeFactSnapshot,
)
from app.services.candidate_data_lifecycle_service import delete_candidate, delete_resume, restore_deletion_batch
from app.services.integration_auth_service import integration_now
from app.services.integration_retention_service import cleanup_expired_integration_records, invalidate_analysis_reports_for_sources
from app.tenant_scope import set_organization_context
from test_integration_auth_helpers import make_context


def seed_report(ctx):
    """Create one synthetic provenance chain without depending on migration tests."""

    with ctx.database.session_factory() as session:
        set_organization_context(session, ctx.organization_id)
        candidate = Candidate(display_name="Synthetic provenance candidate")
        session.add(candidate)
        session.flush()
        resume = Resume(
            candidate_id=candidate.id,
            original_filename="synthetic.pdf",
            storage_key=f"synthetic/{uuid4()}.pdf",
            sha256="a" * 64,
            source_page_count=1,
            parsed_page_count=1,
            extraction_status="ready",
            parser_version="synthetic",
            facts_version=1,
        )
        session.add(resume)
        session.flush()
        snapshot = ResumeFactSnapshot(
            resume_id=resume.id,
            facts_version=1,
            canonical_facts_json="{}",
            facts_sha256="b" * 64,
            source_block_ids=["synthetic-block"],
            created_by="synthetic-test",
        )
        report = IntegrationAnalysisReport(
            owner_user_id=ctx.user_id,
            membership_id=ctx.membership_id,
            source_grant_id=ctx.grant_id,
            title="Synthetic private draft",
            content_json={"facts": []},
            confirmed_at=integration_now(),
        )
        session.add_all([snapshot, report])
        session.commit()
        return report.id, candidate.id, resume.id, snapshot.id


def reference(ctx, ids):
    report_id, candidate_id, resume_id, snapshot_id = ids
    return IntegrationAnalysisReference(
        organization_id=ctx.organization_id,
        report_id=report_id,
        candidate_id=candidate_id,
        resume_id=resume_id,
        fact_snapshot_id=snapshot_id,
        facts_version=1,
        candidate_lifecycle_version=1,
        resume_lifecycle_version=1,
        fact_ids=["synthetic-fact"],
        source_block_ids=["synthetic-block"],
    )


@pytest.mark.parametrize("kind", ["candidate", "resume"])
def test_delete_scrubs_entire_private_draft_and_restore_does_not_restore_it(tmp_path, kind):
    ctx = make_context(tmp_path)
    ids = seed_report(ctx)
    with ctx.database.session_factory() as session:
        set_organization_context(session, ctx.organization_id)
        session.add(reference(ctx, ids))
        report = session.get(IntegrationAnalysisReport, ids[0])
        report.title = "Synthetic private candidate title"
        report.content_json = {"inferences": ["Synthetic retained copy"]}
        session.commit()
        common = dict(settings=ctx.settings, actor_user_id=ctx.user_id, reason="candidate_request", private_note=None)
        if kind == "candidate":
            result = delete_candidate(session, candidate_id=ids[1], **common)
        else:
            result = delete_resume(session, resume_id=ids[2], **common)
        session.commit()
        session.expire_all()
        report = session.get(IntegrationAnalysisReport, ids[0])
        assert report.invalidated_at is not None
        assert report.content_json == {} and report.title == "已失效的分析草稿"
        assert report.version == 2
        assert session.scalar(select(func.count()).select_from(IntegrationAnalysisReference)) == 0
        # The fixture creates a recoverable local original only inside tmp_path.
        from app.models import Resume
        resume = session.scalar(select(Resume).where(Resume.id == ids[2]).execution_options(include_deleted_candidate_data=True))
        path = ctx.settings.upload_dir / resume.storage_key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic-only-original")
        restore_deletion_batch(session, deletion_batch_id=result.deletion_batch_id, actor_user_id=ctx.user_id)
        session.commit()
        session.expire_all()
        report = session.get(IntegrationAnalysisReport, ids[0])
        assert report.invalidated_at is not None and report.content_json == {}
        assert session.scalar(select(func.count()).select_from(IntegrationAnalysisReference)) == 0
    ctx.database.dispose()


def test_internal_invalidation_requires_matching_workspace_context(tmp_path):
    ctx = make_context(tmp_path)
    ids = seed_report(ctx)
    with ctx.database.session_factory() as session:
        set_organization_context(session, ctx.organization_id)
        session.add(reference(ctx, ids))
        session.commit()
        with pytest.raises(ValueError, match="context_required"):
            invalidate_analysis_reports_for_sources(session, organization_id="other-workspace", candidate_ids=[ids[1]])
        assert session.get(IntegrationAnalysisReport, ids[0]).invalidated_at is None
    ctx.database.dispose()


def test_retention_expires_only_due_reports_even_when_feature_off(tmp_path):
    ctx = make_context(tmp_path)
    expired = seed_report(ctx)
    current = seed_report(ctx)
    now = integration_now()
    with ctx.database.session_factory() as session:
        set_organization_context(session, ctx.organization_id)
        session.add_all([reference(ctx, expired), reference(ctx, current)])
        session.execute(update(IntegrationAnalysisReport).where(IntegrationAnalysisReport.id == expired[0])
            .values(expires_at=now - timedelta(seconds=1)))
        session.commit()
    assert cleanup_expired_integration_records(ctx.database, now=now) >= 1
    with ctx.database.session_factory() as session:
        set_organization_context(session, ctx.organization_id)
        assert session.get(IntegrationAnalysisReport, expired[0]) is None
        assert session.get(IntegrationAnalysisReport, current[0]) is not None
        assert session.scalar(select(func.count()).select_from(IntegrationAnalysisReference)) == 1
    ctx.database.dispose()
