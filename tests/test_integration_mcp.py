from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import replace
from unittest.mock import Mock

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from starlette.routing import Mount

from app import integration_mcp
from app.integration_mcp import create_integration_mcp_app, integration_mcp_lifespan
from app.integration_schemas import IntegrationGrantCreate
from app.services.integration_management_service import create_integration_grant
from app.services.integration_auth_service import DEFAULT_INTEGRATION_SCOPES, INTEGRATION_SCOPES, IntegrationAccessError
from test_integration_auth_helpers import make_context, named_auth
from test_integration_oauth import issue, oauth_browser


def _mcp_context(tmp_path):
    context = make_context(tmp_path, integrations_mcp_enabled=True)
    with context.database.session_factory() as session:
        issued = create_integration_grant(
            session,
            named_auth(session, context),
            settings=context.settings,
            payload=IntegrationGrantCreate(
                name="Synthetic MCP connection",
                audience="mcp",
                scopes=["candidates:read", "jobs:read", "assessments:read"],
            ),
        )
    context.mcp_token = issued.token
    return context


def test_mcp_factory_is_hidden_until_enabled(tmp_path):
    context = make_context(tmp_path)
    assert (
        create_integration_mcp_app(
            database=context.database,
            settings=context.settings,
        )
        is None
    )
    assert (
        create_integration_mcp_app(
            database=context.database,
            settings=replace(
                context.settings,
                integrations_enabled=False,
                integrations_mcp_enabled=True,
            ),
        )
        is None
    )


def test_mcp_lists_exact_read_tools_and_enforces_audience(tmp_path):
    context = _mcp_context(tmp_path)
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
            tools = await session.list_tools()
            connection = await session.call_tool("get_connection_info", {})
            rest_token_response = await httpx2.AsyncClient(
                transport=httpx2.ASGITransport(host),
                base_url="http://testserver",
            ).post(
                "/v1/mcp",
                headers={"Authorization": f"Bearer {context.token}"},
                json={},
            )
        return tools, connection, rest_token_response

    tools, connection, rest_token_response = asyncio.run(exercise())
    assert {tool.name for tool in tools.tools} == {
        "get_connection_info",
        "get_filter_options",
        "search_candidates",
        "get_candidate_profile",
        "get_candidate_evidence",
        "get_candidate_assessments",
        "list_jobs",
        "get_job_requirements",
    }
    assert connection.is_error is False
    assert connection.structured_content["audience"] == "mcp"
    assert rest_token_response.status_code == 401


@pytest.mark.parametrize("analysis_enabled", [False, True])
def test_supported_scopes_are_advertised_without_requiring_them_for_mcp_initialization(tmp_path, analysis_enabled, monkeypatch):
    # Alembic fileConfig can disable existing SDK loggers in a full-suite run.
    # Keep that condition explicit: authorization evidence must not be log text.
    monkeypatch.setattr(logging.getLogger("mcp.server.mcpserver.server"), "disabled", True)
    context = make_context(tmp_path, integrations_mcp_enabled=True, integrations_oauth_enabled=True,
        integrations_analysis_enabled=analysis_enabled)
    try:
        with oauth_browser(context) as client:
            _, token, _ = issue(client)
            authorization_metadata = client.get("/.well-known/oauth-authorization-server").json()
        # This OAuth token contains only candidates:read, not all advertised scopes.
        assert token["scope"] == "candidates:read"
        server, mcp_app = create_integration_mcp_app(database=context.database, settings=context.settings)
        assert server.settings.auth.required_scopes == []
        metadata_path = "/.well-known/oauth-protected-resource/v1/mcp"
        assert sum(route.path == metadata_path for route in mcp_app.routes) == 1

        @asynccontextmanager
        async def lifespan(_app):
            async with integration_mcp_lifespan(server):
                yield

        host = FastAPI(lifespan=lifespan)
        host.router.routes.append(Mount("/", app=mcp_app))
        with TestClient(host) as client:
            metadata = client.get(metadata_path)
            assert metadata.status_code == 200
            body = metadata.json()
            assert body["resource"] == "http://testserver/v1/mcp"
            assert body["authorization_servers"] == [authorization_metadata["issuer"]] == ["http://testserver/"]
            expected = INTEGRATION_SCOPES if analysis_enabled else DEFAULT_INTEGRATION_SCOPES | {"evidence:read"}
            assert body["scopes_supported"] == authorization_metadata["scopes_supported"] == sorted(expected)
            assert client.options(metadata_path, headers={"Origin": "https://synthetic.example.test",
                "Access-Control-Request-Method": "GET"}).status_code == 200
            assert client.post("/v1/mcp", json={}).status_code == 401
            headers = {"Authorization": f"Bearer {token['access_token']}", "Accept": "application/json, text/event-stream"}
            initialized = client.post("/v1/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1,
                "method": "initialize", "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                "clientInfo": {"name": "synthetic-scope-test", "version": "1"}}})
            assert initialized.status_code == 200
            assert "result" in initialized.json()
            connected = client.post("/v1/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2,
                "method": "tools/call", "params": {"name": "get_connection_info", "arguments": {}}})
            assert connected.status_code == 200
            assert connected.json()["result"]["structuredContent"]["scopes"] == ["candidates:read"]
            authenticate = integration_mcp.authenticate_integration_token
            denials = []

            def observe_authentication(*args, **kwargs):
                try:
                    return authenticate(*args, **kwargs)
                except IntegrationAccessError as error:
                    denials.append((frozenset(kwargs.get("required_scopes", ())), error.code, error.status_code))
                    raise

            read_executor = Mock(wraps=integration_mcp.execute_integration_read)
            monkeypatch.setattr(integration_mcp, "authenticate_integration_token", observe_authentication)
            monkeypatch.setattr(integration_mcp, "execute_integration_read", read_executor)
            # Discovery must not confer advertised rights through the tool layer.
            forbidden_tools = [("list_jobs", {}, {"jobs:read"}),
                ("get_candidate_evidence", {"candidate_id": "synthetic-unread-candidate",
                    "request": {"source_block_ids": ["p1-b1"]}}, {"candidates:read", "evidence:read"})]
            if analysis_enabled:
                forbidden_tools.append(("get_analysis_draft", {"report_id": "synthetic-unread-draft"}, {"analyses:read"}))
            for name, arguments, required_scopes in forbidden_tools:
                denials.clear()
                response = client.post("/v1/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 3,
                    "method": "tools/call", "params": {"name": name, "arguments": arguments}})
                assert response.status_code == 200
                result = response.json()["result"]
                assert result["isError"] is True
                assert "structuredContent" not in result
                # Observe the real authorization failure before SDK error masking,
                # without substituting an auth result or reading business data.
                assert denials == [(frozenset(required_scopes), "integration_scope_forbidden", 403)]
                read_executor.assert_not_called()
    finally:
        context.database.dispose()

