"""An isolated ASGI launcher for browser regression tests.

The browser still talks to the real FastAPI application and SQLite persistence
layer.  This module only provides deterministic local fixtures and an in-memory
transactional delivery capture; it is intentionally not reachable from a
production entry point, Docker image, or deployment configuration.
"""
from __future__ import annotations

import hashlib
import os
import secrets
from pathlib import Path
from typing import Literal

from cryptography.fernet import Fernet
from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import AppSettings
from app.database import get_session
from app.main import create_app
from app.models import (
    JobMatch,
    JobMatchRequirementResult,
    Resume,
    ResumeFactSnapshot,
    ResumeSourceBlock,
)
from app.schemas import JobCreate, JobRequirements, ResumeFactsSaveRequest
from app.services.identity_service import AuthPrincipal, principal_from_session
from app.services.job_service import create_job
from app.services import mailbox_import_service
from app.services.resume_service import create_candidate, save_facts
from app.services.transactional_email import TestTransactionalEmailProvider
from app.services.transactional_email_outbox_service import (
    run_transactional_email_outbox_worker_once,
)
from app.tenant_scope import set_organization_context


PROJECT_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = Path(
    os.getenv("E2E_DATA_DIR", str(PROJECT_DIR / "data" / "e2e-playwright"))
).resolve()
PUBLIC_APP_URL = os.getenv("E2E_PUBLIC_APP_URL", "http://127.0.0.1:5174")
E2E_CONTROL_TOKEN = os.getenv("E2E_CONTROL_TOKEN", "local-playwright-control")

# The non-secret placeholder merely allows durable AI *queue creation*. The
# e2e launcher never starts an AI worker and never invokes an external model.
settings = AppSettings(
    project_dir=PROJECT_DIR,
    data_dir=DATA_DIR,
    upload_dir=DATA_DIR / "uploads",
    database_url=f"sqlite:///{(DATA_DIR / 'e2e.sqlite3').as_posix()}",
    session_secret="e2e-local-session-secret-not-for-production",
    allow_unauthenticated=False,
    transactional_email_provider="test",
    public_app_url=PUBLIC_APP_URL,
    deepseek_api_key="e2e-local-placeholder-not-a-provider-key",
    legacy_openai_compatible_endpoint="https://e2e.invalid/v1/chat/completions",
    email_credentials_key=Fernet.generate_key().decode("ascii"),
    mailbox_imap_allowed_hosts=("imap.feishu.cn",),
    registration_rate_limit_global_limit=200,
    registration_rate_limit_client_limit=200,
    registration_rate_limit_email_limit=20,
)


def _e2e_initial_mailbox_watermark(**_: object) -> tuple[int, int]:
    """Keep browser tests offline while preserving the normal create flow.

    Production mailbox creation validates and records the remote mailbox's
    UID watermark before it accepts a channel.  The E2E app deliberately
    replaces only that network boundary with a deterministic value; queueing
    and every HTTP/API permission check still run through the real service.
    """

    return (1, 1)


# This module is imported only by ``web/e2e/start-api.mjs``.  Rebinding the
# service-local helper here keeps IMAP out of the test process without adding a
# test bypass to any production endpoint or deployment configuration.
mailbox_import_service._read_initial_mailbox_watermark = _e2e_initial_mailbox_watermark

app = create_app(settings)


def _current_e2e_principal(request: Request, session: Session) -> AuthPrincipal:
    """Resolve the browser's own member before exposing any fixture data."""

    principal = principal_from_session(session, request.session)
    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="authentication_required",
        )
    set_organization_context(session, principal.organization_id)
    return principal


def _require_e2e_control_token(
    request: Request,
    token: str | None = Header(default=None, alias="X-E2E-Control-Token"),
) -> None:
    """Protect the unauthenticated reset-delivery lookup in this test app.

    Password recovery deliberately works after logout, so a normal session is
    unavailable when Playwright needs its locally captured action URL.  This
    guard exists only in the loopback-only E2E launcher; it is not part of the
    production application or an application route.
    """

    # ``start-api.mjs`` binds Uvicorn to loopback.  Recheck the peer here so a
    # manual ``0.0.0.0`` launch cannot expose locally captured reset links just
    # because the test-only default control token is known in the repository.
    if (
        request.client is None
        or request.client.host not in {"127.0.0.1", "::1"}
        or not secrets.compare_digest(token or "", E2E_CONTROL_TOKEN)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="e2e_control_forbidden",
        )


@app.get("/__e2e__/deliveries")
def list_e2e_deliveries(
    request: Request,
    session: Session = Depends(get_session),
) -> dict[str, list[dict[str, str | int]]]:
    """Expose only this signed-in user's captured local verification links."""

    principal = _current_e2e_principal(request, session)
    provider = request.app.state.transactional_email_provider
    if not isinstance(provider, TestTransactionalEmailProvider):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="e2e_delivery_capture_unavailable",
        )
    return {
        "deliveries": [
            {
                "recipient": delivery.recipient,
                "verification_url": delivery.verification_url,
                "expires_minutes": delivery.expires_minutes,
            }
            for delivery in provider.deliveries
            if delivery.recipient.casefold() == principal.user.email.casefold()
        ]
    }


@app.get("/__e2e__/password-reset-deliveries")
def list_e2e_password_reset_deliveries(
    recipient: str,
    request: Request,
    _: None = Depends(_require_e2e_control_token),
) -> dict[str, list[dict[str, str | int]]]:
    """Return this local test recipient's captured reset links only.

    The actual password-reset request is still submitted by the browser via
    the normal public API.  This adapter exists solely to let the browser test
    follow the generated one-time URL without sending a real email.
    """

    provider = request.app.state.transactional_email_provider
    if not isinstance(provider, TestTransactionalEmailProvider):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="e2e_delivery_capture_unavailable",
        )
    # Password recovery now leaves HTTP before provider I/O.  The loopback-only
    # control route advances one real outbox job against the same test provider
    # so the browser can follow the generated one-time URL without starting a
    # second process or adding a production test bypass.
    run_transactional_email_outbox_worker_once(
        request.app.state.database,
        settings=request.app.state.settings,
        worker_id="e2e-password-reset-outbox",
        provider=provider,
    )
    recipient_key = recipient.strip().casefold()
    return {
        "deliveries": [
            {
                "recipient": delivery.recipient,
                "reset_url": delivery.reset_url,
                "expires_minutes": delivery.expires_minutes,
            }
            for delivery in provider.password_reset_deliveries
            if delivery.recipient.casefold() == recipient_key
        ]
    }


def _seed_ready_resume(
    session: Session,
    *,
    organization_id: str,
    candidate_name: str,
    sequence: int,
) -> Resume:
    """Create one source-grounded, searchable candidate through domain code."""

    candidate = create_candidate(session, display_name=None)
    source_text = (
        f"{candidate_name}\n"
        "清华大学 计算机科学 本科\n"
        "Python 后端经验 分布式系统\n"
        "负责服务端开发与系统设计。"
    )
    resume = Resume(
        candidate_id=candidate.id,
        original_filename=f"e2e-fixture-{sequence}.pdf",
        storage_key=f"e2e-fixture-{candidate.id}.pdf",
        sha256=hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
        source_page_count=1,
        parsed_page_count=1,
        extraction_status="text_ready",
        quality_flags=[],
        parser_version="e2e-fixture",
        raw_text=source_text,
    )
    session.add(resume)
    session.flush()
    if resume.organization_id != organization_id:
        raise RuntimeError("e2e_fixture_workspace_stamp_failed")
    session.add(
        ResumeSourceBlock(
            resume_id=resume.id,
            block_id="page-001",
            page_no=1,
            block_type="page_text",
            text=source_text,
        )
    )
    session.flush()
    save_facts(
        session,
        resume_id=resume.id,
        request=ResumeFactsSaveRequest.model_validate(
            {
                "facts": {
                    "schema_version": "resume_facts.v2",
                    "candidate_name_raw": candidate_name,
                    "candidate_name_evidence_block_ids": ["page-001"],
                    "education": [
                        {
                            "school_name_raw": "清华大学",
                            "degree": "bachelor",
                            "major_raw": "计算机科学",
                            "evidence_block_ids": ["page-001"],
                        }
                    ],
                    "skills": [
                        {
                            "skill_display": "Python",
                            "skill_category": "software",
                            "evidence_block_ids": ["page-001"],
                        }
                    ],
                }
            }
        ),
        created_by="e2e-fixture",
        force_pending_review=True,
        auto_activate=True,
    )
    session.flush()
    return resume


def _seed_match(
    session: Session,
    *,
    resume: Resume,
    job_id: str,
    job_version_id: str,
    requirements: list[object],
    lane: Literal["recommended", "pending", "unmet"],
) -> None:
    snapshot = session.scalar(
        select(ResumeFactSnapshot).where(
            ResumeFactSnapshot.resume_id == resume.id,
            ResumeFactSnapshot.facts_version == resume.facts_version,
        )
    )
    if snapshot is None:
        raise RuntimeError("e2e_fixture_snapshot_missing")

    hard_status = {
        "recommended": "pass",
        "pending": "information_insufficient",
        "unmet": "unmet",
    }[lane]
    evidence_coverage = {
        "recommended": 0.9,
        "pending": 0.4,
        "unmet": 0.8,
    }[lane]
    total_score = {
        "recommended": 78.0,
        "pending": 26.0,
        "unmet": 12.0,
    }[lane]
    job_match = JobMatch(
        job_id=job_id,
        job_version_id=job_version_id,
        resume_id=resume.id,
        fact_snapshot_id=snapshot.id,
        facts_version=resume.facts_version,
        job_version=1,
        total_score=total_score,
        must_have_passed=(True if hard_status == "pass" else False if hard_status == "unmet" else None),
        evidence_coverage=evidence_coverage,
        hard_requirement_status=hard_status,
        analysis={
            "schema_version": "e2e_match.v1",
            "needs_human_review": lane == "pending",
            "decision": "advisory_only",
        },
        status="needs_review" if lane == "pending" else "succeeded",
        model_name="e2e-fixture",
    )
    session.add(job_match)
    session.flush()
    for requirement in requirements:
        priority = getattr(requirement, "priority")
        outcome = (
            "met"
            if lane == "recommended"
            else "unknown"
            if lane == "pending" and priority == "must_have"
            else "not_met"
            if lane == "unmet" and priority == "must_have"
            else "partial"
        )
        session.add(
            JobMatchRequirementResult(
                job_match_id=job_match.id,
                requirement_id=getattr(requirement, "id"),
                outcome=outcome,
                reason="E2E fixture: only verifies presentation and lane boundaries.",
                fact_ids=["skill-001"],
                missing_or_uncertain=("E2E fixture needs a human check" if outcome == "unknown" else None),
                score_contribution=0.0,
            )
        )
    session.flush()


@app.post("/__e2e__/fixture/seed")
def seed_e2e_workspace_fixture(
    request: Request,
    session: Session = Depends(get_session),
) -> dict[str, object]:
    """Seed local-only candidate/JD data after an actual browser login.

    The production HTTP routes remain responsible for all reads and user
    actions. This endpoint only avoids model/provider calls when browser tests
    need stable, source-grounded input data for score queues and JD lanes.
    """

    principal = _current_e2e_principal(request, session)
    if not principal.email_verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="email_verification_required",
        )

    resumes = [
        _seed_ready_resume(
            session,
            organization_id=principal.organization_id,
            candidate_name=name,
            sequence=index,
        )
        for index, name in enumerate(
            ("E2E 推荐候选人", "E2E 待核实候选人", "E2E 不匹配候选人"),
            start=1,
        )
    ]
    job = create_job(
        session,
        payload=JobCreate(
            title="E2E 后端工程师",
            jd_text="必须掌握 Python\n具备后端经验",
            requirements=JobRequirements(
                must_have=["Python"],
                preferred=["后端经验"],
            ),
        ),
    )
    session.flush()

    # Load the returned immutable version through the normal tenant-aware ORM
    # path. Each result below is then served by the ordinary JD-match API.
    from app.models import JobVersion

    job_version = session.scalar(
        select(JobVersion).where(JobVersion.id == job.job_version_id)
    )
    if job_version is None:
        raise RuntimeError("e2e_fixture_job_version_missing")
    requirements = list(job_version.requirements)
    for resume, lane in zip(
        resumes,
        ("recommended", "pending", "unmet"),
        strict=True,
    ):
        _seed_match(
            session,
            resume=resume,
            job_id=job_version.job_id,
            job_version_id=job_version.id,
            requirements=requirements,
            lane=lane,
        )
    session.commit()
    return {
        "resume_ids": [resume.id for resume in resumes],
        "job_version_id": job_version.id,
    }
