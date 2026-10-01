"""Cross-transport contracts for the approved synthetic candidate queries.

The service, REST router and real MCP SDK adapter must expose the same ordered
candidate pages and the same privacy-minimized matching-evidence projection.
No model provider is involved in these tests.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import select
from starlette.routing import Mount

from app.integration_mcp import create_integration_mcp_app, integration_mcp_lifespan
from app.integration_read_schemas import IntegrationCandidateSearchRequest
from app.integration_router import router
from app.integration_schemas import IntegrationGrantCreate
from app.models import Resume, ResumeFactSnapshot, ResumeSkill, ResumeSourceBlock
from app.services.integration_auth_service import authenticate_integration_token
from app.services.integration_management_service import create_integration_grant
from app.services.integration_read_service import search_candidates
from test_integration_auth_helpers import make_context, named_auth
from test_integration_read_service import seed_read_candidates


@dataclass(frozen=True)
class QueryCase:
    name: str
    payload: dict[str, Any]
    expected_labels: frozenset[str]


QUERY_CASES = (
    QueryCase("all-current-candidates", {}, frozenset({"alpha", "beta", "gamma"})),
    QueryCase("known-985-or-211", {"is_985_211": True}, frozenset({"alpha"})),
    # gamma is unknown and must not be collapsed into the known false bucket.
    QueryCase("known-not-985-or-211", {"is_985_211": False}, frozenset({"beta"})),
    QueryCase(
        "highest-degree-bachelor",
        {"highest_degree_in": ["bachelor"]},
        frozenset({"alpha"}),
    ),
    QueryCase(
        "highest-degree-master",
        {"highest_degree_in": ["master"]},
        frozenset({"beta"}),
    ),
    QueryCase(
        "education-degree-associate",
        {"education_degree_in": ["associate"]},
        frozenset({"gamma"}),
    ),
    QueryCase(
        "institution-985",
        {"institution_classifications_any_of": ["985"]},
        frozenset({"alpha"}),
    ),
    QueryCase(
        "institution-overseas",
        {"institution_classifications_any_of": ["overseas"]},
        frozenset({"beta"}),
    ),
    QueryCase(
        "employment-at-least-20-months",
        {"min_employment_months": 20},
        frozenset({"alpha"}),
    ),
    QueryCase(
        "employment-or-internship-at-least-10-months",
        {"min_employment_or_internship_months": 10},
        frozenset({"alpha", "beta"}),
    ),
    QueryCase(
        "has-employment",
        {"experience_types_all_of": ["employment"]},
        frozenset({"alpha"}),
    ),
    QueryCase(
        "has-internship",
        {"experience_types_all_of": ["internship"]},
        frozenset({"alpha", "beta"}),
    ),
    QueryCase(
        "has-employment-and-internship",
        {"experience_types_all_of": ["employment", "internship"]},
        frozenset({"alpha"}),
    ),
    QueryCase(
        "software-skill-category",
        {"skill_categories_any_of": ["software"]},
        frozenset({"alpha"}),
    ),
    QueryCase("all-skills-python", {"skills_all_of": ["Python"]}, frozenset({"alpha"})),
    QueryCase("any-skill-figma", {"skills_any_of": ["Figma"]}, frozenset({"beta"})),
    QueryCase("language-cet6", {"language_credentials_any_of": ["cet6"]}, frozenset({"alpha"})),
    QueryCase("scholarship-present", {"scholarship_status": "present"}, frozenset({"alpha"})),
    QueryCase(
        "scholarship-unknown",
        {"scholarship_status": "unknown"},
        frozenset({"beta", "gamma"}),
    ),
    QueryCase(
        "competition-present",
        {"competition_status": "present"},
        frozenset({"alpha", "beta"}),
    ),
    QueryCase(
        "competition-award-present",
        {"competition_award_status": "present"},
        frozenset({"alpha"}),
    ),
    QueryCase(
        "any-of-985-or-figma",
        {
            "condition_match_mode": "any",
            "is_985_211": True,
            "skills_all_of": ["Figma"],
        },
        frozenset({"alpha", "beta"}),
    ),
    # Rust exists only on alpha's inactive historical resume. Current-version
    # search must not recall it or count alpha twice.
    QueryCase(
        "inactive-version-rust-is-not-current",
        {"skills_all_of": ["Rust"]},
        frozenset(),
    ),
)


def _rest_client(context) -> TestClient:
    app = FastAPI()
    app.state.database = context.database
    app.state.settings = context.settings
    app.include_router(router)
    return TestClient(app)


def _add_inactive_historical_resume(context) -> str:
    with context.database.session_factory() as session:
        authenticate_integration_token(
            session,
            token=context.token,
            audience="rest",
            settings=context.settings,
            required_scopes=("candidates:read",),
        )
        current = session.scalar(
            select(Resume).where(
                Resume.candidate_id == context.ids["alpha"],
                Resume.is_active.is_(True),
            )
        )
        assert current is not None
        historical = Resume(
            organization_id=context.organization_id,
            candidate_id=current.candidate_id,
            original_filename="synthetic-historical-alpha.pdf",
            storage_key=f"{context.organization_id}/synthetic-history-{uuid4()}.pdf",
            sha256="8" * 64,
            source_page_count=1,
            parsed_page_count=1,
            extraction_status="ready",
            quality_flags=[],
            parser_version="integration-query-consistency",
            is_active=False,
            is_985_211=False,
            highest_degree="master",
            employment_months=120,
            employment_or_internship_months=120,
            facts_version=1,
            raw_text="Historical Rust experience must not be searchable.",
            contact_details=[],
        )
        session.add(historical)
        session.flush()
        block_id = "alpha-historical-main"
        session.add_all(
            [
                ResumeSourceBlock(
                    resume_id=historical.id,
                    block_id=block_id,
                    page_no=1,
                    block_type="paragraph",
                    text="Historical Rust experience must not be searchable.",
                ),
                ResumeSkill(
                    resume_id=historical.id,
                    skill_key="rust",
                    skill_display="Rust",
                    skill_category="software",
                    evidence_block_ids=[block_id],
                ),
                ResumeFactSnapshot(
                    organization_id=context.organization_id,
                    resume_id=historical.id,
                    facts_version=1,
                    canonical_facts_json=json.dumps(
                        {
                            "schema_version": "resume_facts.v1",
                            "source_block_ids": [block_id],
                            "derived": {
                                "is_985_211": False,
                                "highest_degree": "master",
                                "employment_months": 120,
                                "employment_or_internship_months": 120,
                            },
                            "education": [],
                            "experiences": [],
                            "skills": [
                                {
                                    "fact_id": "fact-alpha-historical-rust",
                                    "skill_display": "Rust",
                                    "skill_category": "software",
                                    "evidence_block_ids": [block_id],
                                }
                            ],
                            "language_credentials": [],
                            "scholarships": [],
                        }
                    ),
                    facts_sha256="7" * 64,
                    source_block_ids=[block_id],
                    created_by="integration-query-consistency",
                ),
            ]
        )
        session.commit()
        return historical.id


@pytest.fixture(scope="module")
def query_context(tmp_path_factory):
    context = make_context(
        tmp_path_factory.mktemp("integration-query-consistency"),
        integrations_mcp_enabled=True,
    )
    with context.database.session_factory() as session:
        authenticate_integration_token(
            session,
            token=context.token,
            audience="rest",
            settings=context.settings,
            required_scopes=("candidates:read",),
        )
        context.ids = seed_read_candidates(session, context.organization_id)
        issued = create_integration_grant(
            session,
            named_auth(session, context),
            settings=context.settings,
            payload=IntegrationGrantCreate(
                name="Synthetic query consistency MCP",
                audience="mcp",
                scopes=["candidates:read"],
            ),
        )
        context.mcp_token = issued.token
        # Cross-transport equality requires equal authorization, not the default
        # REST grant (which also contains assessment access) versus narrow MCP.
        context.token = create_integration_grant(
            session, named_auth(session, context), settings=context.settings,
            payload=IntegrationGrantCreate(name="Synthetic query consistency REST",
                audience="rest", scopes=["candidates:read"]),
        ).token
    context.historical_resume_id = _add_inactive_historical_resume(context)
    with context.database.session_factory() as session:
        authenticate_integration_token(
            session,
            token=context.token,
            audience="rest",
            settings=context.settings,
            required_scopes=("candidates:read",),
        )
        context.current_sources = {
            candidate_id: {
                "resume_id": resume.id,
                "fact_snapshot_id": snapshot.id,
                "source_block_ids": frozenset(snapshot.source_block_ids or []),
            }
            for candidate_id, resume, snapshot in session.execute(
                select(Resume.candidate_id, Resume, ResumeFactSnapshot)
                .join(
                    ResumeFactSnapshot,
                    (ResumeFactSnapshot.resume_id == Resume.id)
                    & (ResumeFactSnapshot.facts_version == Resume.facts_version),
                )
                .where(Resume.is_active.is_(True))
            ).all()
        }
    yield context
    context.database.dispose()


def _page_signature(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "items": payload["items"],
        "total_count": payload["total_count"],
        "needs_review_count": payload["needs_review_count"],
        "has_next": payload["next_cursor"] is not None,
    }


def _flatten(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for page in pages for item in page["items"]]


def _service_pages(context, payload: dict[str, Any]) -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []
    cursor = None
    with context.database.session_factory() as session:
        principal = authenticate_integration_token(
            session,
            token=context.token,
            audience="rest",
            settings=context.settings,
            required_scopes=("candidates:read",),
        )
        while True:
            request = IntegrationCandidateSearchRequest(
                **payload,
                limit=1,
                cursor=cursor,
            )
            result = search_candidates(
                session,
                principal=principal,
                settings=context.settings,
                request=request,
            ).model_dump(mode="json")
            pages.append(result)
            cursor = result["next_cursor"]
            if cursor is None:
                return pages


def _rest_pages(context, payload: dict[str, Any]) -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []
    cursor = None
    with _rest_client(context) as client:
        while True:
            request = {**payload, "limit": 1}
            if cursor is not None:
                request["cursor"] = cursor
            response = client.post(
                "/v1/integrations/candidates/search",
                headers={"Authorization": f"Bearer {context.token}"},
                json=request,
            )
            assert response.status_code == 200, response.text
            result = response.json()
            pages.append(result)
            cursor = result["next_cursor"]
            if cursor is None:
                return pages


async def _mcp_pages_async(context, payload: dict[str, Any]) -> list[dict[str, Any]]:
    built = create_integration_mcp_app(
        database=context.database,
        settings=context.settings,
    )
    assert built is not None
    server, mcp_app = built

    @asynccontextmanager
    async def lifespan(_app):
        async with integration_mcp_lifespan(server):
            yield

    host = FastAPI(lifespan=lifespan)
    host.router.routes.append(Mount("/", app=mcp_app))
    pages: list[dict[str, Any]] = []
    cursor = None
    client = httpx2.AsyncClient(
        transport=httpx2.ASGITransport(host),
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {context.mcp_token}"},
    )
    async with (
        host.router.lifespan_context(host),
        client,
        streamable_http_client(
            "http://testserver/v1/mcp",
            http_client=client,
        ) as streams,
        ClientSession(*streams) as session,
    ):
        await session.initialize()
        while True:
            request = {**payload, "limit": 1}
            if cursor is not None:
                request["cursor"] = cursor
            response = await session.call_tool(
                "search_candidates",
                {"request": request},
            )
            assert response.is_error is False, response.content
            assert response.structured_content is not None
            result = response.structured_content
            pages.append(result)
            cursor = result["next_cursor"]
            if cursor is None:
                return pages


def _mcp_pages(context, payload: dict[str, Any]) -> list[dict[str, Any]]:
    return asyncio.run(_mcp_pages_async(context, payload))


@pytest.mark.parametrize(
    "case",
    QUERY_CASES,
    ids=[case.name for case in QUERY_CASES],
)
def test_standard_query_is_consistent_across_service_rest_and_mcp(
    query_context,
    case: QueryCase,
) -> None:
    service_pages = _service_pages(query_context, case.payload)
    rest_pages = _rest_pages(query_context, case.payload)
    mcp_pages = _mcp_pages(query_context, case.payload)

    # Cursor values are intentionally principal/audience-bound, so compare the
    # page boundary rather than requiring identical opaque signatures.
    service_signatures = [_page_signature(page) for page in service_pages]
    assert [_page_signature(page) for page in rest_pages] == service_signatures
    assert [_page_signature(page) for page in mcp_pages] == service_signatures

    items = _flatten(service_pages)
    candidate_ids = [item["candidate_id"] for item in items]
    expected_ids = {query_context.ids[label] for label in case.expected_labels}
    assert set(candidate_ids) == expected_ids
    assert len(candidate_ids) == len(set(candidate_ids))
    assert len(service_pages) == max(1, len(expected_ids))
    assert [page["next_cursor"] is not None for page in service_pages] == [
        index < len(service_pages) - 1 for index in range(len(service_pages))
    ]
    assert all(page["total_count"] == len(expected_ids) for page in service_pages)

    # A candidate with several resume versions is one person. Only the current
    # resume/snapshot may be counted or projected, and evidence ids must remain
    # inside that current immutable snapshot.
    assert all(
        item["resume_id"] != query_context.historical_resume_id for item in items
    )
    for item in items:
        source = query_context.current_sources[item["candidate_id"]]
        assert item["resume_id"] == source["resume_id"]
        assert item["fact_snapshot_id"] == source["fact_snapshot_id"]
        assert set(item["evidence_source_block_ids"]) <= source["source_block_ids"]

