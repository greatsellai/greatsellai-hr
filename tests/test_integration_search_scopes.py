"""Both external transports honor optional assessment access on search rows."""
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from starlette.routing import Mount

from app.integration_mcp import create_integration_mcp_app, integration_mcp_lifespan
from app.integration_router import router
from app.integration_schemas import IntegrationGrantCreate
from app.models import Resume, ResumeFactSnapshot, ResumeScore, ScoreTemplate
from app.services.integration_management_service import create_integration_grant
from app.tenant_scope import set_organization_context
from test_integration_auth_helpers import make_context, named_auth
from test_integration_read_service import seed_read_candidates


@pytest.mark.parametrize("audience", ["rest", "mcp"])
@pytest.mark.parametrize("assessments", [False, True])
def test_search_scores_require_explicit_assessment_scope(tmp_path, audience, assessments):
    context = make_context(tmp_path, integrations_mcp_enabled=True)
    try:
        with context.database.session_factory() as session:
            auth = named_auth(session, context)
            set_organization_context(session, context.organization_id)
            ids = seed_read_candidates(session, context.organization_id)
            resume = session.scalar(select(Resume).where(Resume.candidate_id == ids["alpha"]))
            snapshot = session.scalar(select(ResumeFactSnapshot).where(ResumeFactSnapshot.resume_id == resume.id))
            template = ScoreTemplate(name="Synthetic scope test", version=1)
            session.add(template)
            session.flush()
            session.add(ResumeScore(resume_id=resume.id, fact_snapshot_id=snapshot.id,
                template_id=template.id, facts_version=1, template_version=1,
                total_score=87.25, status="succeeded"))
            session.commit()
            issued = create_integration_grant(session, auth, settings=context.settings,
                payload=IntegrationGrantCreate(name="Narrow synthetic search", audience=audience,
                    scopes=["candidates:read"] + (["assessments:read"] if assessments else [])))

        host = FastAPI()
        if audience == "rest":
            host.state.database, host.state.settings = context.database, context.settings
            host.include_router(router)
        else:
            server, mcp_app = create_integration_mcp_app(database=context.database, settings=context.settings)

            @asynccontextmanager
            async def lifespan(_app):
                async with integration_mcp_lifespan(server):
                    yield

            host.router.lifespan_context = lifespan
            host.router.routes.append(Mount("/", app=mcp_app))

        with TestClient(host) as client:
            headers = {"Authorization": f"Bearer {issued.token}"}
            if audience == "rest":
                response = client.post("/v1/integrations/candidates/search", headers=headers, json={})
                assert response.status_code == 200
                result = response.json()
            else:
                headers["Accept"] = "application/json, text/event-stream"
                response = client.post("/v1/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1,
                    "method": "tools/call", "params": {"name": "search_candidates", "arguments": {"request": {}}}})
                assert response.status_code == 200
                assert response.json()["result"]["isError"] is False
                result = response.json()["result"]["structuredContent"]

        assert result["total_count"] == 3
        by_id = {item["candidate_id"]: item for item in result["items"]}
        scored = by_id[ids["alpha"]]
        if assessments:
            assert (scored["score_total"], scored["score_status"]) == (87.25, "succeeded")
            assert "score_total" not in scored["omitted_fields"]
            assert "score_status" not in scored["omitted_fields"]
        else:
            assert scored["score_total"] is None and scored["score_status"] is None
            # Both scored and unscored candidates give the same omission signal.
            for item in by_id.values():
                assert item["score_total"] is None and item["score_status"] is None
                assert {"score_total", "score_status"} <= set(item["omitted_fields"])
    finally:
        context.database.dispose()

