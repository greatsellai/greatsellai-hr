"""Closed consent DTOs. Client-provided display text is not trusted UI markup."""
from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from app.integration_schemas import IntegrationSchema, IntegrationScope


class OAuthConsentClient(IntegrationSchema):
    name: str
    redirect_origin: str


class OAuthConsentWorkspace(IntegrationSchema):
    organization_id: str
    name: str


class OAuthConsentResponse(IntegrationSchema):
    request_id: str
    client: OAuthConsentClient
    workspace: OAuthConsentWorkspace
    audience: Literal["rest", "mcp"]
    scopes: list[IntegrationScope]
    available_scopes: list[IntegrationScope]
    default_scopes: list[IntegrationScope]
    expires_at: datetime
    csrf_token: str = Field(repr=False)


class OAuthConsentDecision(IntegrationSchema):
    approve: bool = Field(strict=True)
    approved_scopes: list[IntegrationScope] | None = Field(default=None, max_length=6, strict=True)

    @model_validator(mode="before")
    @classmethod
    def ignore_scopes_when_denied(cls, value):
        if isinstance(value, dict) and value.get("approve") is False:
            return {**value, "approved_scopes": None}
        return value

    @model_validator(mode="after")
    def reject_duplicate_scopes(self):
        if self.approved_scopes is not None and len(self.approved_scopes) != len(set(self.approved_scopes)):
            raise ValueError("duplicate_approved_scopes")
        return self


class OAuthConsentRedirect(IntegrationSchema):
    redirect_url: str = Field(repr=False)


class OAuthRegistrationResponse(IntegrationSchema):
    client_id: str
    client_name: str
    redirect_uris: list[str]
    token_endpoint_auth_method: Literal["none"]
    grant_types: list[str]
    response_types: list[str]
    client_id_issued_at: int


class OAuthTokenResponse(IntegrationSchema):
    access_token: str = Field(repr=False)
    token_type: Literal["Bearer"]
    expires_in: int
    refresh_token: str = Field(repr=False)
    scope: str


class OAuthErrorResponse(IntegrationSchema):
    error: str
    error_description: str | None = None


class OAuthMetadata(IntegrationSchema):
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str
    revocation_endpoint: str
    response_types_supported: list[str]
    grant_types_supported: list[str]
    token_endpoint_auth_methods_supported: list[str]
    revocation_endpoint_auth_methods_supported: list[str]
    code_challenge_methods_supported: list[str]
    authorization_response_iss_parameter_supported: bool
    scopes_supported: list[str]
