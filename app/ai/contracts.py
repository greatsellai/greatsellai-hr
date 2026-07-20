"""Stable, provider-neutral contracts for the AI gateway.

The boundary here is deliberate: business code can describe *what* it needs
from an AI operation, but cannot select a provider, model, endpoint, or
credential.  Those connection details only appear after the gateway resolves
an approved :class:`RouteTarget`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from ipaddress import ip_address
from types import MappingProxyType
from typing import Any, Literal, TypeAlias
from urllib.parse import urlsplit


JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class GatewayContractError(ValueError):
    """Raised when a gateway boundary object is malformed before transport."""


def validate_external_https_endpoint(value: object, *, field_name: str) -> str:
    """Accept only non-local HTTPS provider endpoints.

    A provider profile can ultimately receive a Bearer credential and a
    candidate-derived prompt.  Even platform-owned configuration must not
    turn the gateway into an SSRF primitive for loopback, link-local, or
    literal private addresses.  DNS allowlisting belongs at the deployment
    egress layer; this contract rejects every unsafe literal target before a
    transport object can be created.
    """

    endpoint = _required_text(value, field_name=field_name)
    try:
        parsed = urlsplit(endpoint)
        hostname = parsed.hostname
    except ValueError as exc:
        raise GatewayContractError(f"{field_name}_invalid") from exc
    if parsed.scheme != "https":
        raise GatewayContractError(f"{field_name}_must_be_https")
    if parsed.username is not None or parsed.password is not None:
        raise GatewayContractError(f"{field_name}_must_not_include_userinfo")
    if hostname is None or not hostname.strip():
        raise GatewayContractError(f"{field_name}_host_required")
    normalized_host = hostname.strip().rstrip(".").casefold()
    if normalized_host in {"localhost", "localhost.localdomain"}:
        raise GatewayContractError(f"{field_name}_host_not_allowed")
    try:
        address = ip_address(normalized_host)
    except ValueError:
        address = None
    if address is not None and (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    ):
        raise GatewayContractError(f"{field_name}_host_not_allowed")
    if parsed.fragment:
        raise GatewayContractError(f"{field_name}_must_not_include_fragment")
    return endpoint


def _required_text(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GatewayContractError(f"{field_name}_must_be_non_empty")
    return value.strip()


def _optional_text(value: object, *, field_name: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, field_name=field_name)


def _json_snapshot(value: object, *, field_name: str) -> JsonValue:
    """Validate JSON compatibility and detach contract data from caller state."""

    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        decoded = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise GatewayContractError(f"{field_name}_must_be_json_serializable") from exc
    return decoded


def _json_mapping_snapshot(value: Mapping[str, object], *, field_name: str) -> Mapping[str, JsonValue]:
    snapshot = _json_snapshot(value, field_name=field_name)
    if not isinstance(snapshot, dict):
        raise GatewayContractError(f"{field_name}_must_be_object")
    return MappingProxyType(snapshot)


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A provider-neutral function/tool invocation.

    ``arguments`` is intentionally preserved as JSON text.  The business
    service owns schema validation, so the transport layer never turns a
    malformed model argument into trusted application data.
    """

    id: str
    name: str
    arguments: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _required_text(self.id, field_name="tool_call_id"))
        object.__setattr__(self, "name", _required_text(self.name, field_name="tool_call_name"))
        if not isinstance(self.arguments, str):
            raise GatewayContractError("tool_call_arguments_must_be_text")


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """A canonical chat message accepted by all text-generation adapters."""

    role: Literal["system", "developer", "user", "assistant", "tool"]
    content: str | None = None
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()

    def __post_init__(self) -> None:
        allowed_roles = {"system", "developer", "user", "assistant", "tool"}
        if self.role not in allowed_roles:
            raise GatewayContractError("message_role_not_supported")
        if self.content is not None and not isinstance(self.content, str):
            raise GatewayContractError("message_content_must_be_text")
        if self.content is None and not self.tool_calls:
            raise GatewayContractError("message_content_or_tool_calls_required")
        object.__setattr__(self, "name", _optional_text(self.name, field_name="message_name"))
        object.__setattr__(
            self,
            "tool_call_id",
            _optional_text(self.tool_call_id, field_name="message_tool_call_id"),
        )
        calls = tuple(self.tool_calls)
        if any(not isinstance(call, ToolCall) for call in calls):
            raise GatewayContractError("message_tool_calls_must_be_tool_calls")
        object.__setattr__(self, "tool_calls", calls)
        if calls and self.role != "assistant":
            raise GatewayContractError("message_tool_calls_require_assistant_role")
        if self.tool_call_id is not None and self.role != "tool":
            raise GatewayContractError("message_tool_call_id_requires_tool_role")
        if self.role == "tool" and self.tool_call_id is None:
            raise GatewayContractError("tool_message_requires_tool_call_id")


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """A canonical function definition supplied by a business feature."""

    name: str
    description: str
    parameters: Mapping[str, object]
    strict: bool | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _required_text(self.name, field_name="tool_name"))
        object.__setattr__(
            self,
            "description",
            _required_text(self.description, field_name="tool_description"),
        )
        if not isinstance(self.parameters, Mapping):
            raise GatewayContractError("tool_parameters_must_be_object")
        object.__setattr__(
            self,
            "parameters",
            _json_mapping_snapshot(self.parameters, field_name="tool_parameters"),
        )
        if self.strict is not None and not isinstance(self.strict, bool):
            raise GatewayContractError("tool_strict_must_be_boolean")


@dataclass(frozen=True, slots=True)
class ToolChoice:
    """A provider-neutral selection policy for function tools."""

    mode: Literal["auto", "none", "required", "named"] = "auto"
    name: str | None = None

    def __post_init__(self) -> None:
        allowed_modes = {"auto", "none", "required", "named"}
        if self.mode not in allowed_modes:
            raise GatewayContractError("tool_choice_mode_not_supported")
        object.__setattr__(self, "name", _optional_text(self.name, field_name="tool_choice_name"))
        if self.mode == "named" and self.name is None:
            raise GatewayContractError("named_tool_choice_requires_name")
        if self.mode != "named" and self.name is not None:
            raise GatewayContractError("tool_choice_name_requires_named_mode")

    @classmethod
    def named(cls, name: str) -> "ToolChoice":
        return cls(mode="named", name=name)


@dataclass(frozen=True, slots=True)
class CompletionRequest:
    """Business intent for one AI completion.

    This class intentionally has no vendor, endpoint, credential, model, or
    route-preference field.  It is safe for business services and workers to
    construct, but its messages/tools must not be persisted into the cost
    ledger or ordinary logs.
    """

    feature: str
    organization_id: str
    messages: tuple[ChatMessage, ...]
    actor_user_id: str | None = None
    run_id: str | None = None
    business_ref_type: str | None = None
    business_ref_id: str | None = None
    prompt_revision_id: str | None = None
    contract_version: str | None = None
    tools: tuple[ToolDefinition, ...] = ()
    tool_choice: ToolChoice | None = None
    required_capabilities: frozenset[str] = frozenset()
    max_output_tokens: int | None = None
    temperature: float | None = None
    metadata: Mapping[str, object] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "feature", _required_text(self.feature, field_name="feature"))
        object.__setattr__(
            self,
            "organization_id",
            _required_text(self.organization_id, field_name="organization_id"),
        )
        for field_name in (
            "actor_user_id",
            "run_id",
            "business_ref_type",
            "business_ref_id",
            "prompt_revision_id",
            "contract_version",
        ):
            object.__setattr__(
                self,
                field_name,
                _optional_text(getattr(self, field_name), field_name=field_name),
            )

        messages = tuple(self.messages)
        if not messages:
            raise GatewayContractError("completion_messages_required")
        if any(not isinstance(message, ChatMessage) for message in messages):
            raise GatewayContractError("completion_messages_must_be_chat_messages")
        object.__setattr__(self, "messages", messages)

        tools = tuple(self.tools)
        if any(not isinstance(tool, ToolDefinition) for tool in tools):
            raise GatewayContractError("completion_tools_must_be_tool_definitions")
        if len({tool.name for tool in tools}) != len(tools):
            raise GatewayContractError("completion_tool_names_must_be_unique")
        object.__setattr__(self, "tools", tools)
        if self.tool_choice is not None and not isinstance(self.tool_choice, ToolChoice):
            raise GatewayContractError("completion_tool_choice_must_be_tool_choice")
        if self.tool_choice is not None and self.tool_choice.mode == "named":
            assert self.tool_choice.name is not None
            if self.tool_choice.name not in {tool.name for tool in tools}:
                raise GatewayContractError("completion_named_tool_not_declared")
        if self.tool_choice is not None and not tools:
            raise GatewayContractError("completion_tool_choice_requires_tools")

        capabilities = frozenset(self.required_capabilities)
        if any(
            not isinstance(capability, str) or not capability.strip()
            for capability in capabilities
        ):
            raise GatewayContractError("required_capabilities_must_be_non_empty_strings")
        object.__setattr__(self, "required_capabilities", capabilities)

        if self.max_output_tokens is not None:
            if isinstance(self.max_output_tokens, bool) or self.max_output_tokens <= 0:
                raise GatewayContractError("max_output_tokens_must_be_positive")
        if self.temperature is not None:
            if isinstance(self.temperature, bool) or not isinstance(self.temperature, (int, float)):
                raise GatewayContractError("temperature_must_be_number")
            if not 0 <= float(self.temperature) <= 2:
                raise GatewayContractError("temperature_out_of_range")
            object.__setattr__(self, "temperature", float(self.temperature))
        if not isinstance(self.metadata, Mapping):
            raise GatewayContractError("completion_metadata_must_be_object")
        object.__setattr__(
            self,
            "metadata",
            _json_mapping_snapshot(self.metadata, field_name="completion_metadata"),
        )


@dataclass(frozen=True, slots=True)
class RouteAuthentication:
    """How a resolved route injects its already-resolved credential."""

    header_name: str
    value_prefix: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "header_name",
            _required_text(self.header_name, field_name="auth_header_name"),
        )
        if not isinstance(self.value_prefix, str):
            raise GatewayContractError("auth_value_prefix_must_be_text")


@dataclass(frozen=True, slots=True)
class RouteTarget:
    """A fully resolved, server-side target for one provider request.

    The gateway is the only layer allowed to create this object.  Credentials
    are intentionally hidden from ``repr`` and comparisons so accidental
    logging and test diffs do not expose them.
    """

    id: str
    driver: str
    provider_profile_id: str
    model_profile_id: str
    endpoint_url: str
    provider_model_id: str
    timeout_seconds: float
    credential: str | None = field(default=None, repr=False, compare=False)
    authentication: RouteAuthentication | None = None
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)
    request_defaults: Mapping[str, object] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        for field_name in (
            "id",
            "driver",
            "provider_profile_id",
            "model_profile_id",
            "endpoint_url",
            "provider_model_id",
        ):
            object.__setattr__(
                self,
                field_name,
                _required_text(getattr(self, field_name), field_name=field_name),
            )
        object.__setattr__(
            self,
            "endpoint_url",
            validate_external_https_endpoint(
                self.endpoint_url,
                field_name="route_endpoint_url",
            ),
        )
        if isinstance(self.timeout_seconds, bool) or not isinstance(
            self.timeout_seconds,
            (int, float),
        ):
            raise GatewayContractError("route_timeout_seconds_must_be_number")
        if float(self.timeout_seconds) <= 0:
            raise GatewayContractError("route_timeout_seconds_must_be_positive")
        object.__setattr__(self, "timeout_seconds", float(self.timeout_seconds))
        if self.credential is not None:
            _required_text(self.credential, field_name="route_credential")
            if self.authentication is None:
                raise GatewayContractError("route_credential_requires_authentication")
        if self.authentication is not None and not isinstance(
            self.authentication,
            RouteAuthentication,
        ):
            raise GatewayContractError("route_authentication_invalid")
        if not isinstance(self.headers, Mapping):
            raise GatewayContractError("route_headers_must_be_object")
        normalized_headers: dict[str, str] = {}
        for header_name, header_value in self.headers.items():
            normalized_name = _required_text(header_name, field_name="route_header_name")
            if normalized_name.casefold() in {"authorization", "content-type"}:
                raise GatewayContractError("route_headers_must_not_override_transport_headers")
            if not isinstance(header_value, str):
                raise GatewayContractError("route_header_value_must_be_text")
            normalized_headers[normalized_name] = header_value
        object.__setattr__(self, "headers", MappingProxyType(normalized_headers))
        if not isinstance(self.request_defaults, Mapping):
            raise GatewayContractError("route_request_defaults_must_be_object")
        object.__setattr__(
            self,
            "request_defaults",
            _json_mapping_snapshot(self.request_defaults, field_name="route_request_defaults"),
        )


@dataclass(frozen=True, slots=True)
class NormalizedUsage:
    """Disjoint metering buckets for a completed external AI request.

    ``input_tokens`` and ``output_tokens`` exclude cache and reasoning token
    quantities when a provider reports those as subsets.  This prevents later
    price calculation from charging the same token twice.
    """

    input_tokens: int = 0
    cached_read_input_tokens: int = 0
    cached_write_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    image_units: int = 0
    page_units: int = 0
    request_units: int = 0
    provider_reported_total_tokens: int | None = None

    def __post_init__(self) -> None:
        integer_fields = (
            "input_tokens",
            "cached_read_input_tokens",
            "cached_write_input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "image_units",
            "page_units",
            "request_units",
        )
        for field_name in integer_fields:
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise GatewayContractError(f"{field_name}_must_be_non_negative_integer")
        if self.provider_reported_total_tokens is not None:
            value = self.provider_reported_total_tokens
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise GatewayContractError(
                    "provider_reported_total_tokens_must_be_non_negative_integer"
                )

    @property
    def metered_token_total(self) -> int:
        return (
            self.input_tokens
            + self.cached_read_input_tokens
            + self.cached_write_input_tokens
            + self.output_tokens
            + self.reasoning_tokens
        )


@dataclass(frozen=True, slots=True)
class CompletionResult:
    """Canonical successful response returned by a protocol adapter.

    ``raw_response`` is an in-memory handoff for the immediately following
    domain validator only.  Gateway ledger code must never persist it.
    """

    content: str | None
    tool_calls: tuple[ToolCall, ...]
    finish_reason: str | None
    provider_request_id: str | None
    usage: NormalizedUsage | None
    raw_status_code: int
    model_id: str
    provider_response_id: str | None = None
    raw_response: Mapping[str, object] = field(
        default_factory=dict,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if self.content is not None and not isinstance(self.content, str):
            raise GatewayContractError("completion_result_content_must_be_text")
        calls = tuple(self.tool_calls)
        if any(not isinstance(call, ToolCall) for call in calls):
            raise GatewayContractError("completion_result_tool_calls_must_be_tool_calls")
        object.__setattr__(self, "tool_calls", calls)
        if self.content is None and not calls:
            raise GatewayContractError("completion_result_content_or_tool_calls_required")
        object.__setattr__(
            self,
            "finish_reason",
            _optional_text(self.finish_reason, field_name="completion_finish_reason"),
        )
        object.__setattr__(
            self,
            "provider_request_id",
            _optional_text(self.provider_request_id, field_name="provider_request_id"),
        )
        object.__setattr__(
            self,
            "provider_response_id",
            _optional_text(self.provider_response_id, field_name="provider_response_id"),
        )
        if self.usage is not None and not isinstance(self.usage, NormalizedUsage):
            raise GatewayContractError("completion_result_usage_must_be_normalized_usage")
        if isinstance(self.raw_status_code, bool) or not isinstance(self.raw_status_code, int):
            raise GatewayContractError("completion_result_status_code_must_be_integer")
        if not 100 <= self.raw_status_code <= 599:
            raise GatewayContractError("completion_result_status_code_invalid")
        object.__setattr__(self, "model_id", _required_text(self.model_id, field_name="result_model_id"))
        if not isinstance(self.raw_response, Mapping):
            raise GatewayContractError("completion_result_raw_response_must_be_object")
        object.__setattr__(
            self,
            "raw_response",
            _json_mapping_snapshot(self.raw_response, field_name="completion_result_raw_response"),
        )


__all__ = [
    "ChatMessage",
    "CompletionRequest",
    "CompletionResult",
    "GatewayContractError",
    "JsonValue",
    "NormalizedUsage",
    "RouteAuthentication",
    "RouteTarget",
    "ToolCall",
    "ToolChoice",
    "ToolDefinition",
    "validate_external_https_endpoint",
]
