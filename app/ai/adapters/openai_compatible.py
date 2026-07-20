"""Adapter for the OpenAI-compatible chat-completions protocol.

The adapter knows only a protocol shape.  It contains no concrete provider,
model, base URL, credential name, or business prompt.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

from app.ai.adapters.base import CompletionAdapter
from app.ai.contracts import (
    ChatMessage,
    CompletionRequest,
    CompletionResult,
    GatewayContractError,
    NormalizedUsage,
    RouteTarget,
    ToolCall,
    ToolChoice,
    ToolDefinition,
)
from app.ai.errors import (
    ProviderConfigurationError,
    ProviderError,
    ProviderErrorCategory,
    ProviderResponseError,
)


_PROTECTED_DEFAULT_KEYS = frozenset(
    {
        "model",
        "messages",
        "tools",
        "tool_choice",
        "max_tokens",
        "max_output_tokens",
        "temperature",
        "stream",
    }
)
_REQUEST_ID_HEADER_NAMES = (
    "x-request-id",
    "request-id",
    "x-amzn-requestid",
    "x-correlation-id",
)


class OpenAICompatibleAdapter(CompletionAdapter):
    """Execute non-streaming chat completions through a resolved route."""

    driver = "openai_compatible"

    def __init__(self, *, opener: Callable[..., Any] | None = None) -> None:
        # Kept injectable so tests never make a real network request.  The
        # default is resolved at call time, which also keeps monkeypatching
        # urllib.request.urlopen straightforward.
        self._opener = opener

    def complete(
        self,
        request: CompletionRequest,
        route: RouteTarget,
    ) -> CompletionResult:
        self._validate_route(route)
        payload = _serialize_completion_request(request, route)
        headers = _request_headers(route)
        http_request = urllib.request.Request(
            route.endpoint_url,
            data=json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode(
                "utf-8"
            ),
            headers=headers,
            method="POST",
        )

        opener = self._opener or urllib.request.urlopen
        try:
            with opener(http_request, timeout=route.timeout_seconds) as response:
                raw_body = response.read()
                raw_response = json.loads(raw_body.decode("utf-8"))
                status_code = _response_status_code(response)
                provider_request_id = _provider_request_id(response.headers)
        except urllib.error.HTTPError as exc:
            raise _http_error_to_provider_error(exc) from exc
        except (TimeoutError, socket.timeout) as exc:
            raise ProviderError(
                ProviderErrorCategory.TIMEOUT,
                may_have_billed=True,
            ) from exc
        except urllib.error.URLError as exc:
            raise ProviderError(
                ProviderErrorCategory.NETWORK,
                may_have_billed=True,
            ) from exc
        except OSError as exc:
            raise ProviderError(
                ProviderErrorCategory.NETWORK,
                may_have_billed=True,
            ) from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderResponseError() from exc

        return _parse_completion_response(
            raw_response,
            status_code=status_code,
            provider_request_id=provider_request_id,
            fallback_model_id=route.provider_model_id,
        )

    def _validate_route(self, route: RouteTarget) -> None:
        if route.driver != self.driver:
            raise ProviderConfigurationError()
        if route.credential is None or route.authentication is None:
            raise ProviderConfigurationError()
        if any(key in _PROTECTED_DEFAULT_KEYS for key in route.request_defaults):
            raise ProviderConfigurationError()


def _serialize_completion_request(
    request: CompletionRequest,
    route: RouteTarget,
) -> dict[str, Any]:
    payload = dict(route.request_defaults)
    payload["model"] = route.provider_model_id
    payload["messages"] = [_serialize_message(message) for message in request.messages]
    if request.tools:
        payload["tools"] = [_serialize_tool(tool) for tool in request.tools]
    if request.tool_choice is not None:
        payload["tool_choice"] = _serialize_tool_choice(request.tool_choice)
    if request.max_output_tokens is not None:
        payload["max_tokens"] = request.max_output_tokens
    if request.temperature is not None:
        payload["temperature"] = request.temperature
    # Phase one intentionally supports only a single JSON response.  The
    # gateway needs a closed invocation record before any business validation.
    payload["stream"] = False
    return payload


def _serialize_message(message: ChatMessage) -> dict[str, Any]:
    payload: dict[str, Any] = {"role": message.role}
    if message.content is not None:
        payload["content"] = message.content
    if message.name is not None:
        payload["name"] = message.name
    if message.tool_call_id is not None:
        payload["tool_call_id"] = message.tool_call_id
    if message.tool_calls:
        payload["tool_calls"] = [_serialize_tool_call(call) for call in message.tool_calls]
    return payload


def _serialize_tool_call(call: ToolCall) -> dict[str, Any]:
    return {
        "id": call.id,
        "type": "function",
        "function": {"name": call.name, "arguments": call.arguments},
    }


def _serialize_tool(tool: ToolDefinition) -> dict[str, Any]:
    function: dict[str, Any] = {
        "name": tool.name,
        "description": tool.description,
        "parameters": dict(tool.parameters),
    }
    if tool.strict is not None:
        function["strict"] = tool.strict
    return {"type": "function", "function": function}


def _serialize_tool_choice(choice: ToolChoice) -> str | dict[str, Any]:
    if choice.mode == "named":
        assert choice.name is not None
        return {"type": "function", "function": {"name": choice.name}}
    return choice.mode


def _request_headers(route: RouteTarget) -> dict[str, str]:
    assert route.credential is not None
    assert route.authentication is not None
    headers = dict(route.headers)
    headers["Content-Type"] = "application/json"
    headers[route.authentication.header_name] = (
        f"{route.authentication.value_prefix}{route.credential}"
    )
    return headers


def _response_status_code(response: Any) -> int:
    getcode = getattr(response, "getcode", None)
    status_code = getcode() if callable(getcode) else getattr(response, "status", None)
    if isinstance(status_code, bool) or not isinstance(status_code, int):
        raise ProviderResponseError()
    return status_code


def _provider_request_id(headers: object) -> str | None:
    if not isinstance(headers, Mapping) and not hasattr(headers, "items"):
        return None
    try:
        items = headers.items()  # type: ignore[union-attr]
    except (AttributeError, TypeError):
        return None
    known_names = set(_REQUEST_ID_HEADER_NAMES)
    for name, value in items:
        if isinstance(name, str) and name.casefold() in known_names:
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _http_error_to_provider_error(exc: urllib.error.HTTPError) -> ProviderError:
    request_id = _provider_request_id(exc.headers)
    if exc.code in {401, 403}:
        category = ProviderErrorCategory.AUTH
    elif exc.code == 429:
        category = ProviderErrorCategory.RATE_LIMITED
    elif exc.code in {408, 504}:
        category = ProviderErrorCategory.TIMEOUT
    elif 500 <= exc.code <= 599:
        category = ProviderErrorCategory.PROVIDER_5XX
    else:
        category = ProviderErrorCategory.INVALID_REQUEST
    return ProviderError(
        category,
        may_have_billed=category is ProviderErrorCategory.TIMEOUT,
        http_status_code=exc.code,
        provider_request_id=request_id,
    )


def _parse_completion_response(
    raw_response: object,
    *,
    status_code: int,
    provider_request_id: str | None,
    fallback_model_id: str,
) -> CompletionResult:
    if not isinstance(raw_response, Mapping):
        raise ProviderResponseError(http_status_code=status_code, provider_request_id=provider_request_id)
    choices = raw_response.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
        raise ProviderResponseError(http_status_code=status_code, provider_request_id=provider_request_id)
    choice = choices[0]
    finish_reason = choice.get("finish_reason")
    if finish_reason is not None and not isinstance(finish_reason, str):
        raise ProviderResponseError(http_status_code=status_code, provider_request_id=provider_request_id)
    if finish_reason == "length":
        raise ProviderResponseError(
            category=ProviderErrorCategory.TRUNCATED,
            http_status_code=status_code,
            provider_request_id=provider_request_id,
        )
    message = choice.get("message")
    if not isinstance(message, Mapping):
        raise ProviderResponseError(http_status_code=status_code, provider_request_id=provider_request_id)
    content = message.get("content")
    if content is not None and not isinstance(content, str):
        raise ProviderResponseError(http_status_code=status_code, provider_request_id=provider_request_id)
    try:
        tool_calls = _parse_tool_calls(message.get("tool_calls", []))
        usage = _normalize_usage(raw_response.get("usage"))
    except (GatewayContractError, ValueError, TypeError) as exc:
        raise ProviderResponseError(
            http_status_code=status_code,
            provider_request_id=provider_request_id,
        ) from exc
    if content is None and not tool_calls:
        raise ProviderResponseError(http_status_code=status_code, provider_request_id=provider_request_id)
    model_id = raw_response.get("model")
    if not isinstance(model_id, str) or not model_id.strip():
        model_id = fallback_model_id
    response_id = raw_response.get("id")
    if not isinstance(response_id, str) or not response_id.strip():
        response_id = None
    return CompletionResult(
        content=content,
        tool_calls=tool_calls,
        finish_reason=finish_reason,
        provider_request_id=provider_request_id or response_id,
        usage=usage,
        raw_status_code=status_code,
        model_id=model_id,
        provider_response_id=response_id,
        raw_response=raw_response,
    )


def _parse_tool_calls(value: object) -> tuple[ToolCall, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError("provider_tool_calls_not_list")
    calls: list[ToolCall] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError("provider_tool_call_not_object")
        function = item.get("function")
        if not isinstance(function, Mapping):
            raise ValueError("provider_tool_call_function_missing")
        call_id = item.get("id")
        name = function.get("name")
        arguments = function.get("arguments")
        if isinstance(arguments, Mapping):
            arguments = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
        if not isinstance(call_id, str) or not isinstance(name, str) or not isinstance(arguments, str):
            raise ValueError("provider_tool_call_fields_invalid")
        calls.append(ToolCall(id=call_id, name=name, arguments=arguments))
    return tuple(calls)


def _usage_int(value: object, *, field_name: str) -> int:
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name}_invalid")
    return value


def _usage_mapping(value: object, *, field_name: str) -> Mapping[str, object]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name}_invalid")
    return value


def _normalize_usage(value: object) -> NormalizedUsage | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("usage_not_object")

    # Several OpenAI-compatible gateways emit ``usage: {}`` when metering is
    # unavailable.  Treat that as unknown rather than manufacturing one
    # request unit and a zero-cost result: a genuine zero is meaningful only
    # when the provider actually supplied at least one metering field.
    recognized_usage_keys = {
        "prompt_tokens",
        "input_tokens",
        "completion_tokens",
        "output_tokens",
        "total_tokens",
        "prompt_tokens_details",
        "input_tokens_details",
        "completion_tokens_details",
        "output_tokens_details",
        "cached_read_input_tokens",
        "cached_write_input_tokens",
        "reasoning_tokens",
        "image_units",
        "page_units",
        "request_units",
    }
    if not any(key in value for key in recognized_usage_keys):
        return None

    prompt_total = _usage_int(
        value.get("prompt_tokens", value.get("input_tokens")),
        field_name="prompt_tokens",
    )
    completion_total = _usage_int(
        value.get("completion_tokens", value.get("output_tokens")),
        field_name="completion_tokens",
    )
    prompt_details = _usage_mapping(
        value.get("prompt_tokens_details", value.get("input_tokens_details")),
        field_name="prompt_tokens_details",
    )
    completion_details = _usage_mapping(
        value.get("completion_tokens_details", value.get("output_tokens_details")),
        field_name="completion_tokens_details",
    )
    cached_read = _usage_int(
        prompt_details.get("cached_tokens", value.get("cached_read_input_tokens")),
        field_name="cached_read_input_tokens",
    )
    cached_write = _usage_int(
        prompt_details.get("cache_creation_tokens", value.get("cached_write_input_tokens")),
        field_name="cached_write_input_tokens",
    )
    reasoning = _usage_int(
        completion_details.get("reasoning_tokens", value.get("reasoning_tokens")),
        field_name="reasoning_tokens",
    )
    if cached_read + cached_write > prompt_total:
        raise ValueError("cached_tokens_exceed_prompt_tokens")
    if reasoning > completion_total:
        raise ValueError("reasoning_tokens_exceed_completion_tokens")
    reported_total_value = value.get("total_tokens")
    reported_total = (
        None
        if reported_total_value is None
        else _usage_int(reported_total_value, field_name="total_tokens")
    )
    # A provider may expose only ``total_tokens`` without separating input
    # from output. Those buckets have different prices, so manufacturing zero
    # input/output usage would turn a real paid call into a false known-zero
    # ledger entry. Keep the whole usage unknown until it is safely priceable.
    if reported_total and prompt_total == 0 and completion_total == 0:
        return None
    return NormalizedUsage(
        input_tokens=prompt_total - cached_read - cached_write,
        cached_read_input_tokens=cached_read,
        cached_write_input_tokens=cached_write,
        output_tokens=completion_total - reasoning,
        reasoning_tokens=reasoning,
        image_units=_usage_int(value.get("image_units"), field_name="image_units"),
        page_units=_usage_int(value.get("page_units"), field_name="page_units"),
        request_units=_usage_int(value.get("request_units", 1), field_name="request_units"),
        provider_reported_total_tokens=reported_total,
    )


__all__ = ["OpenAICompatibleAdapter"]
