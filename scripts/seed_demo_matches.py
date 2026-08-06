"""Seed clearly-labeled demo JD-match data into a local dev database.

Usage (from repo root):

    python scripts/seed_demo_matches.py

Creates one demo job (“高级后端工程师（演示）”) with a confirmed JD version
and six demo candidates spread across the three match lanes, so the flat
JD-match leaderboard has real rows to render without any AI credentials.
Idempotent: re-running clears the previous demo job and demo resumes first.

Every row is marked with a “demo” provenance (filename prefix, model_name,
analysis.schema_version) so it can never be mistaken for extracted data.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

# Force the repo's `app` package to win over any stale editable install that
# maps `app` to a temp snapshot directory (see __editable__.resume_screening_v3).
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from sqlalchemy import select

from app.database import Database
from app.models import (
    Candidate,
    Job,
    JobMatch,
    JobMatchRequirementResult,
    JobVersion,
    Resume,
    ResumeFactSnapshot,
    ResumeSourceBlock,
)
from app.schemas import JobCreate, JobRequirements, ResumeFactsSaveRequest
from app.services.job_service import create_job
from app.services.resume_service import create_candidate, save_facts
from app.tenant_scope import LEGACY_ORGANIZATION_ID, set_organization_context

PROJECT_DIR = Path(__file__).resolve().parents[1]
DATABASE_URL = f"sqlite:///{(PROJECT_DIR / 'data' / 'resume_v3.db').as_posix()}"

DEMO_JOB_TITLE = "高级后端工程师（演示）"

# name, lane, total_score, evidence_coverage, hard_requirement_status
# The JD-match leaderboard shows total_score directly as the match % (it is
# already the weighted total over all JD requirements, max 100); the
# evidence_coverage column is persisted audit data on the 0–100 scale.
# Under the current semantics “没写就当作不会”, the pending lane only holds
# candidates whose hard requirement is *partially* evidenced (partial), so
# those two demo rows use that status rather than information_insufficient.
DEMO_CANDIDATES = [
    ("陈晓东", "recommended", 88.0, 95, "pass"),
    ("林思远", "recommended", 72.0, 90, "pass"),
    ("周明华", "pending", 30.0, 45, "partial"),
    ("吴佳怡", "pending", 22.0, 35, "partial"),
    ("郑志强", "unmet", 15.0, 80, "unmet"),
    ("王建国", "unmet", 8.0, 70, "unmet"),
]

DEMO_MUST_HAVE = ["Python", "后端服务开发经验", "分布式系统"]
DEMO_PREFERRED = ["高并发", "微服务"]


def _seed_resume(
    session,
    *,
    organization_id: str,
    candidate_name: str,
    sequence: int,
) -> Resume:
    candidate = create_candidate(session, display_name=None)
    source_text = (
        f"{candidate_name}\n"
        "清华大学 计算机科学 本科 2018-09 至 2022-06\n"
        "平均成绩 88 分，GPA 3.6/4.0。\n"
        "Python 后端经验 分布式系统\n"
        "工作经历：2020-07 至 2026-06，某科技公司，后端服务研发，后端工程师，负责服务端开发与系统设计。\n"
    )
    resume = Resume(
        candidate_id=candidate.id,
        original_filename=f"demo-{candidate_name}-{sequence}.pdf",
        storage_key=f"demo-{candidate.id}.pdf",
        sha256=hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
        source_page_count=1,
        parsed_page_count=1,
        extraction_status="text_ready",
        quality_flags=[],
        parser_version="demo-seed",
        raw_text=source_text,
        contact_details=[],
    )
    session.add(resume)
    session.flush()
    if resume.organization_id != organization_id:
        raise RuntimeError("demo_seed_workspace_stamp_failed")
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
                            "start_month": "2018-09",
                            "end_month": "2022-06",
                            "average_score": 88,
                            "gpa_value": 3.6,
                            "gpa_scale": 4.0,
                            "evidence_block_ids": ["page-001"],
                        }
                    ],
                    "experiences": [
                        {
                            "experience_type": "employment",
                            "experience_name_raw": "后端服务研发",
                            "organization_name_raw": "某科技公司",
                            "title_raw": "后端工程师",
                            "start_month": "2020-07",
                            "end_month": "2026-06",
                            "evidence_block_ids": ["page-001"],
                            "classification_evidence_block_ids": ["page-001"],
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
        created_by="demo-seed",
        force_pending_review=True,
        auto_activate=True,
    )
    session.flush()
    return resume


def _seed_match(
    session,
    *,
    resume: Resume,
    job_version: JobVersion,
    lane: str,
    total_score: float,
    coverage: float,
    hard_status: str,
) -> None:
    snapshot = session.scalar(
        select(ResumeFactSnapshot).where(
            ResumeFactSnapshot.resume_id == resume.id,
            ResumeFactSnapshot.facts_version == resume.facts_version,
        )
    )
    if snapshot is None:
        raise RuntimeError("demo_seed_snapshot_missing")

    job_match = JobMatch(
        job_id=job_version.job_id,
        job_version_id=job_version.id,
        resume_id=resume.id,
        fact_snapshot_id=snapshot.id,
        facts_version=resume.facts_version,
        job_version=job_version.version,
        total_score=total_score,
        must_have_passed=(
            True if hard_status == "pass" else False if hard_status == "unmet" else None
        ),
        evidence_coverage=coverage,
        hard_requirement_status=hard_status,
        analysis={
            "schema_version": "demo_seed.v1",
            "needs_human_review": lane == "pending",
            "decision": "advisory_only",
        },
        status="needs_review" if lane == "pending" else "succeeded",
        model_name="demo-seed",
    )
    session.add(job_match)
    session.flush()
    for requirement in job_version.requirements:
        priority = requirement.priority
        outcome = (
            "met"  # recommended：硬条件全满足
            if lane == "recommended"
            else "partial"  # pending：硬条件部分满足（部分证据）
            if lane == "pending"
            else "not_met"  # unmet：硬条件未满足
            if lane == "unmet" and priority == "must_have"
            else "partial"  # unmet 的优先项给部分分
        )
        session.add(
            JobMatchRequirementResult(
                job_match_id=job_match.id,
                requirement_id=requirement.id,
                outcome=outcome,
                reason="演示数据：仅用于本地界面预览。",
                fact_ids=["skill-001"],
                missing_or_uncertain=(
                    "演示：需人工核实" if outcome == "unknown" else None
                ),
                score_contribution=0.0,
            )
        )
    session.flush()


def _clear_demo_rows(session) -> None:
    job = session.scalar(select(Job).where(Job.title == DEMO_JOB_TITLE))
    if job is not None:
        session.delete(job)
    for resume in list(
        session.scalars(select(Resume).where(Resume.original_filename.like("demo-%")))
    ):
        session.delete(resume)
    session.flush()
    # Remove any candidate left with no resumes (a demo candidate owns exactly
    # one resume). Never touch a candidate that still has other resumes.
    for candidate in list(session.scalars(select(Candidate))):
        if not candidate.resumes:
            session.delete(candidate)
    session.flush()


def main() -> None:
    database = Database(DATABASE_URL)
    with database.session_factory() as session:
        set_organization_context(session, LEGACY_ORGANIZATION_ID)
        _clear_demo_rows(session)

        job = create_job(
            session,
            payload=JobCreate(
                title=DEMO_JOB_TITLE,
                jd_text="必须掌握 Python。具备后端服务开发经验。熟悉分布式系统。优先考虑高并发、微服务经验。",
                requirements=JobRequirements(
                    must_have=DEMO_MUST_HAVE,
                    preferred=DEMO_PREFERRED,
                ),
            ),
        )
        session.flush()
        job_version = session.scalar(
            select(JobVersion).where(JobVersion.id == job.job_version_id)
        )
        if job_version is None:
            raise RuntimeError("demo_seed_job_version_missing")
        if job_version.status != "confirmed" or not job_version.requirements:
            raise RuntimeError("demo_seed_job_version_not_matchable")

        for index, (name, lane, total, coverage, hard_status) in enumerate(
            DEMO_CANDIDATES,
            start=1,
        ):
            resume = _seed_resume(
                session,
                organization_id=LEGACY_ORGANIZATION_ID,
                candidate_name=name,
                sequence=index,
            )
            _seed_match(
                session,
                resume=resume,
                job_version=job_version,
                lane=lane,
                total_score=total,
                coverage=coverage,
                hard_status=hard_status,
            )

        session.commit()
        print(
            f"已种入演示数据：{len(DEMO_CANDIDATES)} 个候选人 · 岗位「{DEMO_JOB_TITLE}」\n"
            f"    job_version_id = {job_version.id}\n"
        )
        for name, lane, total, coverage, hard_status in DEMO_CANDIDATES:
            print(
                f"    {name:<5} {lane:<14} 匹配度 {total:5.1f}%  "
                f"证据覆盖 {coverage:3.0f}%  ({hard_status})"
            )


if __name__ == "__main__":
    main()
