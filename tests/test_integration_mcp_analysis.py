from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import timedelta

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from starlette.routing import Mount
from sqlalchemy import event, func, select
from sqlalchemy.exc import StatementError

from app.models import IntegrationAnalysisReport, IntegrationRequestLease
from app.integration_mcp import create_integration_mcp_app, integration_mcp_lifespan
from app.services.integration_auth_service import authenticate_integration_token, integration_now
from app.services.integration_read_service import get_candidate_profile
from app.services.integration_retention_service import cleanup_expired_integration_records
from app.tenant_scope import set_organization_context
from test_integration_analysis_api import _browser_client, _csrf_headers
from test_integration_analysis_service import analysis_payload, make_analysis_context


def test_analysis_tools_are_enabled_and_return_receipt_only(tmp_path):
    context = make_analysis_context(tmp_path)
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
    payload = analysis_payload(context)
    built = create_integration_mcp_app(
        database=context.database, settings=context.settings
    )
    assert built is not None
    server, mcp_app = built

    @asynccontextmanager
    async def lifespan(_app):
        async with integration_mcp_lifespan(server):
            yield

    host = FastAPI(lifespan=lifespan)
    host.router.routes.append(Mount("/", app=mcp_app))

    async def exercise():
        client = httpx2.AsyncClient(
            transport=httpx2.ASGITransport(host),
            base_url="http://testserver",
            headers={"Authorization": f"Bearer {context.analysis_mcp_token}"},
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
            tools = await session.list_tools()
            prepared = await session.call_tool(
                "prepare_analysis_draft",
                {"request": payload.model_dump(mode="json")},
            )
            pending_detail = await session.call_tool(
                "get_analysis_draft",
                {"report_id": prepared.structured_content["id"]},
            )
        return tools, prepared, pending_detail

    tools, prepared, pending_detail = asyncio.run(exercise())
    assert len(tools.tools) == 11
    assert {
        "prepare_analysis_draft",
        "list_analysis_drafts",
        "get_analysis_draft",
    } <= {tool.name for tool in tools.tools}
    assert prepared.is_error is False
    assert prepared.structured_content["status"] == "awaiting_confirmation"
    assert pending_detail.is_error is True

    with _browser_client(context) as browser:
        pending = browser.get(
            f"/v1/integration-settings/analysis-reports/pending/{prepared.structured_content['id']}"
        )
        assert pending.status_code == 200, pending.text
        confirmed = browser.post(
            f"/v1/integration-settings/analysis-reports/{prepared.structured_content['id']}/confirm",
            headers=_csrf_headers(context),
            json={"version": pending.json()["version"], "payload_sha256": pending.json()["payload_sha256"]},
        )
    assert confirmed.status_code == 200, confirmed.text

    second_server, second_mcp_app = create_integration_mcp_app(
        database=context.database, settings=context.settings
    )
    @asynccontextmanager
    async def second_lifespan(_app):
        async with integration_mcp_lifespan(second_server):
            yield

    second_host = FastAPI(lifespan=second_lifespan)
    second_host.router.routes.append(Mount("/", app=second_mcp_app))

    async def read_confirmed_draft():
        client = httpx2.AsyncClient(
            transport=httpx2.ASGITransport(second_host),
            base_url="http://testserver",
            headers={"Authorization": f"Bearer {context.analysis_mcp_token}"},
        )
        async with (
            second_host.router.lifespan_context(second_host),
            client,
            streamable_http_client("http://testserver/v1/mcp", http_client=client) as streams,
            ClientSession(*streams) as session,
        ):
            await session.initialize()
            return await session.call_tool(
                "get_analysis_draft", {"report_id": prepared.structured_content["id"]}
            )

    detail = asyncio.run(read_confirmed_draft())
    assert detail.is_error is False
    assert detail.structured_content["referenced_facts"][0]["facts"]["education"] == []


@pytest.mark.parametrize("terminal_state", ["saved", "expired", "discarded"])
def test_mcp_replays_terminal_receipts_through_transport_and_finalizer(tmp_path, terminal_state):
    context = make_analysis_context(tmp_path)
    try:
        with context.database.session_factory() as session:
            authenticate_integration_token(session, token=context.analysis_token,
                audience="rest", settings=context.settings)
            context.alpha_profile = get_candidate_profile(session, candidate_id=context.ids["alpha"])
        payload = analysis_payload(context).model_dump(mode="json")
        server, mcp_app = create_integration_mcp_app(database=context.database, settings=context.settings)

        @asynccontextmanager
        async def lifespan(_app):
            async with integration_mcp_lifespan(server):
                yield

        host = FastAPI(lifespan=lifespan)
        host.router.routes.append(Mount("/", app=mcp_app))
        with TestClient(host) as client, _browser_client(context) as browser:
            def prepare():
                response = client.post("/v1/mcp", headers={
                    "Authorization": f"Bearer {context.analysis_mcp_token}",
                    "Accept": "application/json, text/event-stream",
                }, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": "prepare_analysis_draft", "arguments": {"request": payload}}})
                assert response.status_code == 200, response.text
                result = response.json()["result"]
                assert result.get("isError", False) is False, result
                return result["structuredContent"]

            receipt = prepare()
            report_id = receipt["id"]
            if terminal_state == "saved":
                detail = browser.get(f"/v1/integration-settings/analysis-reports/pending/{report_id}")
                assert detail.status_code == 200
                response = browser.post(f"/v1/integration-settings/analysis-reports/{report_id}/confirm",
                    headers=_csrf_headers(context), json={"version": detail.json()["version"],
                        "payload_sha256": detail.json()["payload_sha256"]})
                assert response.status_code == 200
            elif terminal_state == "discarded":
                response = browser.post(f"/v1/integration-settings/analysis-reports/{report_id}/discard",
                    headers=_csrf_headers(context), json={"version": receipt["version"]})
                assert response.status_code == 204
            else:
                with context.database.session_factory() as session:
                    set_organization_context(session, context.organization_id)
                    session.get(IntegrationAnalysisReport, report_id).expires_at = integration_now() - timedelta(seconds=1)
                    session.commit()
                cleanup_expired_integration_records(context.database)
            replay = prepare()
            assert replay["id"] == report_id and replay["version"] == receipt["version"]
            assert replay["replayed"] is True
            assert replay["status"] == ("saved" if terminal_state == "saved" else "expired")
            assert set(replay) == {"id", "version", "status", "expires_at", "replayed"}
    finally:
        context.database.dispose()


@pytest.mark.parametrize("failure", ["sql_parameters", "driver_message"])
def test_failed_draft_does_not_emit_private_exception_material_to_sdk_or_root_logs(tmp_path, monkeypatch, caplog, failure):
    context = make_analysis_context(tmp_path)
    try:
        with context.database.session_factory() as session:
            authenticate_integration_token(session, token=context.analysis_token,
                audience="rest", settings=context.settings)
            context.alpha_profile = get_candidate_profile(session, candidate_id=context.ids["alpha"])
        title = "SyntheticPrivateTitleCanary731"
        body = "SyntheticPrivateBodyCanary942"
        payload = analysis_payload(context, title=title)
        payload.inferences[0].text = body
        server, mcp_app = create_integration_mcp_app(database=context.database, settings=context.settings)

        @asynccontextmanager
        async def lifespan(_app):
            async with integration_mcp_lifespan(server):
                yield

        host = FastAPI(lifespan=lifespan)
        host.router.routes.append(Mount("/", app=mcp_app))
        # Explicitly enable SDK/root capture: Alembic can disable existing SDK
        # loggers elsewhere in the suite; an empty log is not a privacy proof.
        sdk_logger = logging.getLogger("mcp.server.mcpserver.server")
        monkeypatch.setattr(sdk_logger, "disabled", False)
        monkeypatch.setattr(sdk_logger, "propagate", True)
        caplog.set_level(logging.INFO)
        caplog.set_level(logging.INFO, logger=sdk_logger.name)

        def inject_failure(connection, cursor, statement, parameters, ctx, executemany):
            if statement.lstrip().upper().startswith("INSERT INTO INTEGRATION_ANALYSIS_REPORTS"):
                if failure == "sql_parameters":
                    error = StatementError("Synthetic database failure", statement, parameters, RuntimeError("Synthetic driver failure"))
                    assert title in str(error) and body in str(error)
                    raise error
                raise RuntimeError(f"Synthetic driver failure containing {title} {body}")

        event.listen(context.database.engine, "before_cursor_execute", inject_failure)
        try:
            with TestClient(host) as client:
                headers = {"Authorization": f"Bearer {context.analysis_mcp_token}", "Accept": "application/json, text/event-stream"}
                response = client.post("/v1/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1,
                    "method": "tools/call", "params": {"name": "prepare_analysis_draft",
                        "arguments": {"request": payload.model_dump(mode="json")}}})
        finally:
            event.remove(context.database.engine, "before_cursor_execute", inject_failure)
        assert response.status_code == 200
        result = response.json()["result"]
        assert result["isError"] is True and "structuredContent" not in result
        for canary in (title, body):
            assert canary not in response.text and canary not in caplog.text
        assert result["content"][0]["text"] == "Error executing tool prepare_analysis_draft: integration_operation_unavailable"
        assert any(record.name == sdk_logger.name and "integration_operation_unavailable" in record.getMessage()
            for record in caplog.records)
        with context.database.session_factory() as session:
            set_organization_context(session, context.organization_id)
            assert session.scalar(select(func.count()).select_from(IntegrationAnalysisReport)) == 0
            assert all(lease.released_at is not None for lease in session.scalars(select(IntegrationRequestLease)).all())
    finally:
        context.database.dispose()
