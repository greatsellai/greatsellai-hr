"""Strict integration-management wire contracts; never serialize ORM objects."""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

IntegrationScope = Literal["candidates:read", "jobs:read", "assessments:read", "evidence:read", "analyses:read", "analyses:write"]


class IntegrationSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class IntegrationGrantCreate(IntegrationSchema):
    name: str = Field(min_length=1, max_length=80)
    audience: Literal["rest", "mcp"] = "rest"
    scopes: list[IntegrationScope] = Field(default_factory=lambda: ["candidates:read", "jobs:read", "assessments:read"], min_length=1, max_length=6)
    expires_in_days: int = Field(default=30, ge=1, le=90, strict=True)

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        if any(ord(char) < 32 for char in value):
            raise ValueError("control_characters_not_allowed")
        return value


class IntegrationCredentialRotate(IntegrationSchema):
    expires_in_days: int = Field(default=30, ge=1, le=90, strict=True)


class IntegrationPolicyPatch(IntegrationSchema):
    enabled: bool
    allowed_scopes: list[IntegrationScope] = Field(max_length=6)


class IntegrationGrantSummary(IntegrationSchema):
    id: str
    name: str
    kind: Literal["pat", "oauth"] = "pat"
    audience: Literal["rest", "mcp"]
    scopes: list[IntegrationScope]
    status: Literal["active", "expired", "revoked", "blocked"]
    token_prefix: str
    created_at: datetime
    expires_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None


class IntegrationIssuedCredential(IntegrationSchema):
    grant: IntegrationGrantSummary
    token: str = Field(repr=False)


class IntegrationUser(IntegrationSchema):
    id: str
    display_name: str
    email: str


class IntegrationWorkspace(IntegrationSchema):
    organization_id: str
    name: str
    enabled: bool
    allowed_scopes: list[IntegrationScope]
    available_scopes: list[IntegrationScope]


class IntegrationFeatures(IntegrationSchema):
    api: bool
    mcp: bool
    analyses: bool
    oauth: bool


class IntegrationEndpoints(IntegrationSchema):
    api_base_url: str
    mcp_url: str


class IntegrationPermissions(IntegrationSchema):
    can_create: bool
    can_admin: bool
    can_revoke_own: bool


class IntegrationSettingsResponse(IntegrationSchema):
    user: IntegrationUser
    workspace: IntegrationWorkspace
    features: IntegrationFeatures
    endpoints: IntegrationEndpoints
    permissions: IntegrationPermissions
    csrf_token: str = Field(repr=False)
    grants: list[IntegrationGrantSummary]
    logout_revokes_connections: bool = True


class IntegrationGrantList(IntegrationSchema):
    items: list[IntegrationGrantSummary]


class IntegrationActivity(IntegrationSchema):
    id: str
    grant_id: str | None
    action: str
    resource_type: str
    resource_count: int
    candidate_count: int
    result: str
    reason_code: str | None
    created_at: datetime


class IntegrationActivityList(IntegrationSchema):
    items: list[IntegrationActivity]
    next_cursor: str | None
