from __future__ import annotations

import json
from typing import get_args

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app import integration_router
from app.integration_read_schemas import (
    InstitutionClassification,
    IntegrationCandidateEvidenceRequest,
    IntegrationCandidateSearchRequest,
    LanguageCredentialCode,
    SkillCategory,
)
from app.integration_router import router as external_router
from app.models import IntegrationAuditEvent
from app.services import integration_analysis_service, integration_read_service
from app.services.integration_auth_service import authenticate_integration_token
from app.services.integration_read_service import (
    IntegrationReadError,
    get_candidate_profile,
    get_filter_options,
    search_candidates,
)
from app.tenant_scope import set_organization_context
from test_integration_analysis_api import _browser_client, _csrf_headers, _external_client
from test_integration_analysis_service import analysis_payload, make_analysis_context
from test_integration_auth_helpers import make_context


def test_external_filter_dto_is_the_current_bounded_public_contract():
    classifications = (
        "985",
        "211",
        "undergraduate",
        "associate",
        "secondary_vocational",
        "overseas",
    )
    skill_categories = (
        "software",
        "data_ai",
        "product_project",
        "design_content",
        "marketing_ecommerce_operations",
        "sales_customer_service",
        "supply_chain_logistics",
        "finance_legal_hr",
        "office_collaboration",
        "industry_professional",
    )
    language_credentials = (
        "cet4",
        "cet6",
        "ielts",
        "toefl",
        "tem4",
        "tem8",
        "bec",
        "toeic",
    )

    assert get_args(InstitutionClassification) == classifications
    assert get_args(SkillCategory) == skill_categories
    assert get_args(LanguageCredentialCode) == language_credentials

    request = IntegrationCandidateSearchRequest(
        institution_classifications_any_of=list(classifications),
        skill_categories_any_of=list(skill_categories),
        language_credentials_any_of=list(language_credentials),
    )
    assert tuple(request.institution_classifications_any_of) == classifications
    assert tuple(request.skill_categories_any_of) == skill_categories

    options = get_filter_options()
    assert tuple(item.value for item in options.institution_classifications) == classifications
    assert tuple(item.value for item in options.skill_categories) == skill_categories
    assert tuple(item.value for item in options.language_credentials) == language_credentials
    option_values = {
        item.value
        for group in (
            options.institution_classifications,
            options.skill_categories,
            options.language_credentials,
        )
        for item in group
    }
    assert "double_first_class" not in option_values
    assert "custom" not in option_values

    for payload in (
        {"institution_classifications_any_of": ["double_first_class"]},
        {"institution_classifications_any_of": ["985"] * 7},
        {"skill_categories_any_of": ["engineering"]},
        {"language_credentials_any_of": ["custom"]},
    ):
        with pytest.raises(ValidationError):
            IntegrationCandidateSearchRequest(**payload)


def test_evidence_request_is_bounded_deduplicated_and_closed():
    allowed = [f"synthetic-block-{index}" for index in range(20)]
    assert IntegrationCandidateEvidenceRequest(
        source_block_ids=allowed
    ).source_block_ids == allowed

    for payload in (
        {"source_block_ids": []},
        {"source_block_ids": allowed + ["synthetic-block-20"]},
        {"source_block_ids": ["synthetic-block", "synthetic-block"]},
        {"source_block_ids": ["synthetic-block"], "include_original": True},
    ):
        with pytest.raises(ValidationError):
            IntegrationCandidateEvidenceRequest(**payload)


def test_browser_filter_validation_error_is_mapped_without_echoing_input(
    tmp_path, monkeypatch
):
    context = make_context(tmp_path)
    with context.database.session_factory() as session:
        principal = authenticate_integration_token(
            session,
            token=context.token,
            audience="rest",
            settings=context.settings,
            required_scopes=("candidates:read",),
        )
        validation_error = ValidationError.from_exception_data(
            "CandidateSearchRequest",
            [
                {
                    "type": "missing",
                    "loc": ("synthetic-private-filter",),
                    "input": {},
                }
            ],
        )

        def reject_browser_contract(**_kwargs):
            raise validation_error

        monkeypatch.setattr(
            integration_read_service,
            "CandidateSearchRequest",
            reject_browser_contract,
        )
        with pytest.raises(IntegrationReadError) as raised:
            search_candidates(
                session,
                principal=principal,
                settings=context.settings,
                request=IntegrationCandidateSearchRequest(
                    keywords_all_of=["synthetic-private-filter"]
                ),
            )

    assert raised.value.code == "integration_invalid_filter"
    assert raised.value.status_code == 422
    assert "synthetic-private-filter" not in str(raised.value)


def _rest_client(context) -> TestClient:
    app = FastAPI()
    app.state.database = context.database
    app.state.settings = context.settings
    app.include_router(external_router)
    return TestClient(app)


def test_rest_unexpected_operation_failure_returns_only_fixed_code(
    tmp_path, monkeypatch, caplog
):
    context = make_context(tmp_path)
    private_marker = "synthetic-bound-draft-text"

    def fail_read(*_args, **_kwargs):
        raise RuntimeError(private_marker)

    monkeypatch.setattr(integration_router, "execute_integration_read", fail_read)
    with _rest_client(context) as client:
        response = client.get(
            "/v1/integrations/connection",
            headers={"Authorization": f"Bearer {context.token}"},
        )

    assert response.status_code == 503
    assert response.json() == {
        "detail": {"code": "integration_operation_unavailable"}
    }
    assert private_marker not in response.text
    assert private_marker not in caplog.text
    assert response.headers["cache-control"] == "no-store"


def test_rest_auth_storage_failure_returns_only_fixed_code(
    tmp_path, monkeypatch, caplog
):
    context = make_context(tmp_path)
    private_marker = "synthetic-bound-auth-value"

    def fail_auth(*_args, **_kwargs):
        raise SQLAlchemyError(private_marker)

    monkeypatch.setattr(
        integration_router,
        "authenticate_integration_token",
        fail_auth,
    )
    with _rest_client(context) as client:
        response = client.get(
            "/v1/integrations/connection",
            headers={"Authorization": f"Bearer {context.token}"},
        )

    assert response.status_code == 503
    assert response.json() == {
        "detail": {"code": "integration_storage_unavailable"}
    }
    assert private_marker not in response.text
    assert private_marker not in caplog.text
    assert response.headers["cache-control"] == "no-store"


def _saved_browser_draft(tmp_path):
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
    with _external_client(context) as client:
        response = client.post(
            "/v1/integrations/analysis-reports",
            headers={"Authorization": f"Bearer {context.analysis_token}"},
            json=analysis_payload(context).model_dump(mode="json"),
        )
    assert response.status_code == 202
    with _browser_client(context) as client:
        pending = client.get(f"/v1/integration-settings/analysis-reports/pending/{response.json()['id']}")
        confirmation = client.post(
            f"/v1/integration-settings/analysis-reports/{response.json()['id']}/confirm",
            headers=_csrf_headers(context),
            json={"version": pending.json()["version"], "payload_sha256": pending.json()["payload_sha256"]},
        )
    assert pending.status_code == 200
    assert confirmation.status_code == 200
    return context, response.json()["id"]


def test_browser_private_draft_reads_write_body_free_audits(tmp_path):
    context, report_id = _saved_browser_draft(tmp_path)
    with _browser_client(context) as client:
        listing = client.get("/v1/integration-settings/analysis-reports")
        detail = client.get(
            f"/v1/integration-settings/analysis-reports/{report_id}"
        )

    assert listing.status_code == 200
    assert detail.status_code == 200
    with context.database.session_factory() as session:
        set_organization_context(session, context.organization_id)
        audits = session.scalars(
            select(IntegrationAuditEvent).where(
                IntegrationAuditEvent.action.in_(
                    (
                        "integration.analysis.browser_list",
                        "integration.analysis.browser_read",
                    )
                )
            )
        ).all()

    assert {audit.action for audit in audits} == {
        "integration.analysis.browser_list",
        "integration.analysis.browser_read",
    }
    assert all(audit.resource_count == 1 for audit in audits)
    assert all(audit.candidate_count == 1 for audit in audits)
    assert all(audit.reason_code is None for audit in audits)


def test_browser_private_draft_is_not_returned_when_audit_fails(
    tmp_path, monkeypatch, caplog
):
    context, _report_id = _saved_browser_draft(tmp_path)
    private_marker = "synthetic-private-draft-body"

    def fail_audit(*_args, **_kwargs):
        raise RuntimeError(private_marker)

    monkeypatch.setattr(
        integration_analysis_service,
        "record_integration_audit",
        fail_audit,
    )
    with _browser_client(context) as client:
        response = client.get("/v1/integration-settings/analysis-reports")

    assert response.status_code == 503
    assert response.json() == {"detail": "integration_storage_unavailable"}
    assert private_marker not in response.text
    assert private_marker not in caplog.text
