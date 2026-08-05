"""Tenant-safe AI routing, durable call ledger, and provider execution.

Business services enter :func:`ai_gateway_execution` with the feature they
are performing.  They never choose a provider/model/endpoint/key.  The
gateway resolves a published platform route, writes an independent immutable
run/invocation ledger, then delegates the protocol call to a provider adapter.

The ledger intentionally keeps *metadata only*: hashes, version IDs, usage,
cost snapshots, status, and sanitized error categories.  Prompt bodies,
resume text, model output, request headers, and secrets remain in memory.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.ai import (
    ChatMessage,
    CompletionRequest,
    RouteAuthentication,
    RouteTarget,
    ToolCall,
    ToolChoice,
    ToolDefinition,
)
from app.ai.adapters import OpenAICompatibleAdapter
from app.ai.errors import ProviderError, ProviderErrorCategory
from app.config import AppSettings
from app.models import (
    AiModelPriceVersion,
    AiModelProfile,
    AiProviderProfile,
    AiRoutePolicy,
    AiRoutePolicyVersion,
    AiRun,
    ApiInvocation,
    utcnow,
)
from app.tenant_scope import clear_organization_context, organization_context_id, set_organization_context
from app.services.trial_quota_service import TrialQuotaError, reserve_trial_llm_call


LEGACY_RUNTIME_CREDENTIAL_REF = "legacy-runtime-credential"
LEGACY_RUNTIME_PROVIDER_SLUG = "legacy-runtime-openai-compatible"
LEGACY_RUNTIME_MODEL_SLUG = "legacy-runtime-default"
LEGACY_RUNTIME_PROVIDER_DISPLAY_NAME = "DeepSeek"
LEGACY_RUNTIME_MODEL_DISPLAY_NAME = "DeepSeek 默认模型"

_LEGACY_ROUTE_COPY: dict[str, tuple[str, str]] = {
    "resume_extract_rich": ("简历深度提取", "提取完整的候选人结构化信息。"),
    "resume_extract_core": ("简历核心信息提取", "提取筛选所需的核心字段。"),
    "candidate_name_backfill": ("候选人姓名补全", "基于简历原文补全可核验的姓名。"),
    "resume_score": ("简历评分", "根据岗位要求生成候选人评分。"),
    "score_template_optimize": (
        "评分模板优化",
        "将现有评分模板整理为更清晰、可复核的评分规则草案。",
    ),
    "resume_summary": ("简历总结", "生成候选人经历与亮点摘要。"),
    "jd_generate": ("JD 生成", "根据岗位需求生成职位描述。"),
    "jd_requirements_extract": ("JD 要求提取", "将职位描述整理为评估要求。"),
    "jd_match": ("JD 匹配", "分析候选人与岗位的匹配情况。"),
    "recruiting_agent_turn": ("招聘助手对话", "为招聘助手生成下一轮回复。"),
    "talent_search_profile": ("AI 人才画像", "根据招聘需求生成待 HR 确认的人才搜索画像。"),
    "resume_ocr_page": ("简历 OCR 识别", "识别扫描件或图片简历页面。"),
}

# The set is deliberately small and server-owned.  Platform administrators can
# publish different *routes* for these features, but a browser cannot invent an
# arbitrary provider action or use this API to turn the gateway into a proxy.
SUPPORTED_AI_FEATURES = frozenset(
    {
        "resume_extract_rich",
        "resume_extract_core",
        "candidate_name_backfill",
        "resume_score",
        "score_template_optimize",
        "resume_summary",
        "jd_generate",
        "jd_requirements_extract",
        "jd_match",
        "recruiting_agent_turn",
        "talent_search_profile",
        "resume_ocr_page",
    }
)

_MICROS_PER_CURRENCY_UNIT = Decimal("1000000")
_TOKENS_PER_PRICE_UNIT = Decimal("1000000")
_CURRENT_LEGACY_PAYLOAD_EXECUTOR: ContextVar[
    Callable[[Mapping[str, object]], dict[str, object]] | None
] = ContextVar("greatsell_ai_gateway_payload_executor", default=None)


class AiGatewayError(RuntimeError):
    """A stable, non-sensitive failure from route resolution or execution."""


@dataclass(frozen=True, slots=True)
class AiExecutionSpec:
    """Metadata for one business-level run, without request content."""

    feature: str
    business_ref_type: str
    business_ref_id: str
    actor_user_id: str | None = None
    service_kind: str = "llm"
    correlation_id: str | None = None
    prompt_revision: str | None = None
    contract_version: str | None = None
    pinned_route_policy_version_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("feature", "business_ref_type", "business_ref_id", "service_kind"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name}_required")
            object.__setattr__(self, name, value.strip())
        if self.feature not in SUPPORTED_AI_FEATURES:
            raise ValueError("unsupported_ai_feature")
        for name in (
            "actor_user_id",
            "correlation_id",
            "prompt_revision",
            "contract_version",
            "pinned_route_policy_version_id",
        ):
            value = getattr(self, name)
            if value is not None:
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f"{name}_invalid")
                object.__setattr__(self, name, value.strip())


@dataclass(slots=True)
class _ExecutionHandle:
    session: Session
    settings: AppSettings
    organization_id: str
    spec: AiExecutionSpec
    run_id: str
    route_policy_version: AiRoutePolicyVersion
    next_attempt_no: int = 0


def active_legacy_payload_executor() -> Callable[[Mapping[str, object]], dict[str, object]] | None:
    """Return the current in-memory gateway hook, if a service installed one.

    ``deepseek_provider`` uses this tiny compatibility seam while its prompts,
    strict tool schemas, and evidence validators stay untouched.  Direct calls
    outside a gateway context retain the old transport only during migration.
    """

    return _CURRENT_LEGACY_PAYLOAD_EXECUTOR.get()


def ai_gateway_credentials_configured(settings: AppSettings) -> bool:
    """Whether this process has at least one server-side AI credential path.

    Route resolution still validates that the selected profile references an
    available credential.  This lightweight check is only for existing queue
    code deciding whether an AI job should be queued or shown as unavailable.
    """

    return bool(settings.deepseek_api_key or settings.ai_provider_credentials)


def ai_provider_credential_configured(
    settings: AppSettings,
    credential_ref: str | None,
) -> bool:
    """Whether this process can resolve one non-secret provider reference.

    The platform control plane may expose this boolean to platform operators,
    but never the credential value or the full environment map.  Keeping the
    same resolver as actual Gateway execution prevents the UI from claiming a
    route is ready when API/worker calls would fail later.
    """

    return bool(_resolve_credential(settings, credential_ref))


def gateway_prompt_transport_arguments(settings: AppSettings) -> tuple[str, str, int]:
    """Return inert arguments required by pre-gateway prompt helpers.

    The existing domain prompt/schema functions still expose ``api_key`` and
    ``model`` parameters while their transport is being peeled away.  Every
    gateway-migrated call executes under :func:`ai_gateway_execution`, where
    ``deepseek_provider`` intercepts the payload before those values are ever
    used.  Keeping the compatibility values here prevents business code from
    selecting a provider credential or model during the transition.
    """

    return "", "gateway-managed", settings.deepseek_timeout_seconds


@contextmanager
def ai_gateway_execution(
    source_session: Session,
    *,
    settings: AppSettings,
    spec: AiExecutionSpec,
) -> Iterator[None]:
    """Install a per-run gateway context and finalize its durable ledger.

    It intentionally opens a separate session bound to the same engine.  This
    makes the external-call ledger durable even if the caller's later business
    transaction fails validation or rolls back.
    """

    organization_id = organization_context_id(source_session)
    _log_gateway_event(
        "ai_gateway_execution_start",
        workspace_id=organization_id,
        job_kind=spec.feature,
    )
    execution_started = time.perf_counter()
    ledger_session = _new_ledger_session(source_session, organization_id)
    handle: _ExecutionHandle | None = None
    token = None
    try:
        handle = _create_execution(ledger_session, settings=settings, organization_id=organization_id, spec=spec)
        token = _CURRENT_LEGACY_PAYLOAD_EXECUTOR.set(
            lambda payload: _execute_legacy_payload(handle, payload)
        )
        yield
    except BaseException as exc:
        if handle is not None:
            _finish_run(handle, status="failed", failure_code=_safe_failure_code(exc))
        raise
    else:
        if handle is not None:
            _finish_run(handle, status="succeeded", failure_code=None)
    finally:
        if token is not None:
            _CURRENT_LEGACY_PAYLOAD_EXECUTOR.reset(token)
        clear_organization_context(ledger_session)
        ledger_session.close()
        _log_gateway_event(
            "ai_gateway_execution_completed",
            workspace_id=organization_id,
            job_kind=spec.feature,
            duration_ms=int((time.perf_counter() - execution_started) * 1000),
        )


def resolve_active_route_policy_version_id(
    session: Session,
    *,
    settings: AppSettings,
    feature: str,
) -> str:
    """Return a route pin for a durable queue without running a provider call."""

    if feature not in SUPPORTED_AI_FEATURES:
        raise AiGatewayError("unsupported_ai_feature")
    # Queue creation commonly happens inside an uncommitted resume/JD
    # transaction.  Resolving on that same session keeps a newly bootstrapped
    # route visible to its FK without committing the caller's business data.
    version = _resolve_route_policy_version(
        session,
        settings=settings,
        feature=feature,
        pinned_id=None,
    )
    session.flush()
    return version.id


def _log_gateway_event(event: str, **fields: object) -> None:
    """Emit a safe, structured AI-gateway event for operational tracing."""

    try:
        from app.observability import configure_observability_logging, log_event
    except ModuleNotFoundError as exc:
        if exc.name == "app.observability":
            return
        raise
    configure_observability_logging()
    log_event(event, **fields)


def _new_ledger_session(source_session: Session, organization_id: str) -> Session:
    factory = sessionmaker(
        bind=source_session.get_bind(),
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )
    session = factory()
    set_organization_context(session, organization_id)
    # Eagerly bind a connection so a pool-exhaustion wait is observable in
    # structured logs instead of surfacing as an unexplained stall. Emit a
    # start marker before acquisition; if the pool cannot spare a connection
    # the done marker never arrives and the logs pinpoint the wait.
    _log_gateway_event(
        "ai_gateway_ledger_acquire_start",
        workspace_id=organization_id,
    )
    acquire_started = time.perf_counter()
    session.connection()
    _log_gateway_event(
        "ai_gateway_ledger_acquire_done",
        workspace_id=organization_id,
        duration_ms=int((time.perf_counter() - acquire_started) * 1000),
    )
    return session


def _create_execution(
    session: Session,
    *,
    settings: AppSettings,
    organization_id: str,
    spec: AiExecutionSpec,
) -> _ExecutionHandle:
    run = AiRun(
        organization_id=organization_id,
        actor_user_id=spec.actor_user_id,
        feature=spec.feature,
        service_kind=spec.service_kind,
        business_ref_type=spec.business_ref_type,
        business_ref_id=spec.business_ref_id,
        correlation_id=spec.correlation_id,
        prompt_revision=spec.prompt_revision,
        contract_version=spec.contract_version,
        status="starting",
        started_at=utcnow(),
        reporting_currency="CNY",
        cost_status="unavailable",
    )
    session.add(run)
    session.commit()
    try:
        version = _resolve_route_policy_version(
            session,
            settings=settings,
            feature=spec.feature,
            pinned_id=spec.pinned_route_policy_version_id,
        )
    except BaseException as exc:
        run.status = "failed"
        run.failure_code = _safe_failure_code(exc)
        run.finished_at = utcnow()
        session.commit()
        raise
    run.route_policy_version_id = version.id
    run.status = "running"
    session.commit()
    return _ExecutionHandle(
        session=session,
        settings=settings,
        organization_id=organization_id,
        spec=spec,
        run_id=run.id,
        route_policy_version=version,
    )


def _resolve_route_policy_version(
    session: Session,
    *,
    settings: AppSettings,
    feature: str,
    pinned_id: str | None,
) -> AiRoutePolicyVersion:
    policy = session.scalar(select(AiRoutePolicy).where(AiRoutePolicy.feature == feature))
    if policy is None:
        _bootstrap_legacy_route_if_available(
            session,
            settings=settings,
            feature=feature,
        )
        policy = session.scalar(select(AiRoutePolicy).where(AiRoutePolicy.feature == feature))
    if policy is None:
        raise AiGatewayError("ai_route_not_configured")

    if pinned_id is not None:
        version = session.scalar(
            select(AiRoutePolicyVersion).where(
                AiRoutePolicyVersion.id == pinned_id,
                AiRoutePolicyVersion.policy_id == policy.id,
                AiRoutePolicyVersion.status == "published",
            )
        )
        if version is None:
            raise AiGatewayError("ai_pinned_route_not_available")
        return version

    if not policy.enabled:
        raise AiGatewayError("ai_route_disabled")
    if not policy.active_version_id:
        raise AiGatewayError("ai_route_not_published")
    version = session.scalar(
        select(AiRoutePolicyVersion).where(
            AiRoutePolicyVersion.id == policy.active_version_id,
            AiRoutePolicyVersion.policy_id == policy.id,
            AiRoutePolicyVersion.status == "published",
        )
    )
    if version is None:
        raise AiGatewayError("ai_route_not_published")
    return version


def _bootstrap_legacy_route_if_available(
    session: Session,
    *,
    settings: AppSettings,
    feature: str,
) -> None:
    """Create exactly one compatibility route for pre-gateway deployments.

    It runs only when a requested feature has no policy at all.  Once a
    platform administrator publishes, disables, or deletes a policy, startup
    and ordinary calls never overwrite that decision.  New installations can
    instead create provider/model/route records through the platform API.
    """

    if not settings.deepseek_api_key or not settings.legacy_openai_compatible_endpoint:
        return

    # ``begin_nested`` flushes pending ORM state before opening its savepoint.
    # Flush caller-owned state explicitly outside every bootstrap race handler
    # so an unrelated integrity error can never be mistaken for a duplicate
    # provider/model/policy created by another process. This does not commit or
    # roll back the caller's outer business transaction.
    session.flush()
    provider = _get_or_create_legacy_provider(session, settings=settings)
    model = _get_or_create_legacy_model(session, settings=settings, provider=provider)
    _get_or_create_legacy_policy(session, feature=feature, model=model)


def _get_or_create_legacy_provider(
    session: Session,
    *,
    settings: AppSettings,
) -> AiProviderProfile:
    def lookup() -> AiProviderProfile | None:
        return session.scalar(
            select(AiProviderProfile).where(
                AiProviderProfile.slug == LEGACY_RUNTIME_PROVIDER_SLUG
            )
        )

    provider = lookup()
    if provider is not None:
        return provider
    try:
        with session.begin_nested():
            provider = AiProviderProfile(
                slug=LEGACY_RUNTIME_PROVIDER_SLUG,
                display_name=LEGACY_RUNTIME_PROVIDER_DISPLAY_NAME,
                driver="openai_compatible",
                base_url=settings.legacy_openai_compatible_endpoint,
                credential_ref=LEGACY_RUNTIME_CREDENTIAL_REF,
                request_defaults_json={"thinking": {"type": "disabled"}},
                enabled=True,
            )
            session.add(provider)
            session.flush([provider])
    except IntegrityError:
        provider = lookup()
        if provider is None:
            raise
    return provider


def _get_or_create_legacy_model(
    session: Session,
    *,
    settings: AppSettings,
    provider: AiProviderProfile,
) -> AiModelProfile:
    def lookup() -> AiModelProfile | None:
        return session.scalar(
            select(AiModelProfile).where(
                AiModelProfile.slug == LEGACY_RUNTIME_MODEL_SLUG
            )
        )

    model = lookup()
    if model is not None:
        return model
    try:
        with session.begin_nested():
            model = AiModelProfile(
                provider_profile_id=provider.id,
                slug=LEGACY_RUNTIME_MODEL_SLUG,
                display_name=LEGACY_RUNTIME_MODEL_DISPLAY_NAME,
                provider_model_id=settings.deepseek_model,
                capabilities_json={"chat": True, "tools": True, "json_schema": True},
                data_classification_json={"candidate_data_allowed": True},
                enabled=True,
            )
            session.add(model)
            session.flush([model])
    except IntegrityError:
        model = lookup()
        if model is None:
            raise
    return model


def _get_or_create_legacy_policy(
    session: Session,
    *,
    feature: str,
    model: AiModelProfile,
) -> AiRoutePolicy:
    def lookup() -> AiRoutePolicy | None:
        return session.scalar(
            select(AiRoutePolicy).where(AiRoutePolicy.feature == feature)
        )

    policy = lookup()
    if policy is not None:
        return policy
    display_name, description = _LEGACY_ROUTE_COPY.get(
        feature,
        (feature, "为历史部署创建的兼容路由。"),
    )
    try:
        with session.begin_nested():
            policy = AiRoutePolicy(
                feature=feature,
                display_name=display_name,
                description=description,
                enabled=True,
            )
            session.add(policy)
            session.flush([policy])
            version = AiRoutePolicyVersion(
                policy_id=policy.id,
                version=1,
                status="published",
                targets_json=[
                    {
                        "model_profile_id": model.id,
                        "max_attempts": 1,
                        "allow_fallback_on": [],
                    }
                ],
                retry_policy_json={},
                max_cost_guard_json={},
                prompt_revision=None,
                published_at=utcnow(),
            )
            session.add(version)
            session.flush([version])
            policy.active_version_id = version.id
            session.flush([policy])
    except IntegrityError:
        policy = lookup()
        if policy is None or policy.active_version_id is None:
            raise
    return policy


def _execute_legacy_payload(
    handle: _ExecutionHandle,
    payload: Mapping[str, object],
) -> dict[str, object]:
    request = _completion_request_from_legacy_payload(handle, payload)
    _record_input_fingerprint(handle, payload)
    return _execute_completion(handle, request)


def _completion_request_from_legacy_payload(
    handle: _ExecutionHandle,
    payload: Mapping[str, object],
) -> CompletionRequest:
    raw_messages = payload.get("messages")
    if not isinstance(raw_messages, list):
        raise AiGatewayError("ai_request_messages_invalid")
    messages: list[ChatMessage] = []
    for raw_message in raw_messages:
        if not isinstance(raw_message, Mapping):
            raise AiGatewayError("ai_request_messages_invalid")
        role = raw_message.get("role")
        content = raw_message.get("content")
        name = raw_message.get("name")
        tool_call_id = raw_message.get("tool_call_id")
        raw_tool_calls = raw_message.get("tool_calls", [])
        if (
            not isinstance(role, str)
            or (content is not None and not isinstance(content, str))
            or (name is not None and not isinstance(name, str))
            or (tool_call_id is not None and not isinstance(tool_call_id, str))
            or not isinstance(raw_tool_calls, list)
        ):
            raise AiGatewayError("ai_request_messages_invalid")
        tool_calls: list[ToolCall] = []
        for raw_call in raw_tool_calls:
            if not isinstance(raw_call, Mapping):
                raise AiGatewayError("ai_request_messages_invalid")
            function = raw_call.get("function")
            call_id = raw_call.get("id")
            if not isinstance(function, Mapping):
                raise AiGatewayError("ai_request_messages_invalid")
            function_name = function.get("name")
            arguments = function.get("arguments")
            if isinstance(arguments, Mapping):
                arguments = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
            if not isinstance(call_id, str) or not isinstance(function_name, str) or not isinstance(arguments, str):
                raise AiGatewayError("ai_request_messages_invalid")
            try:
                tool_calls.append(ToolCall(id=call_id, name=function_name, arguments=arguments))
            except ValueError as exc:
                raise AiGatewayError("ai_request_messages_invalid") from exc
        try:
            messages.append(
                ChatMessage(
                    role=role,
                    content=content,
                    name=name,
                    tool_call_id=tool_call_id,
                    tool_calls=tuple(tool_calls),
                )
            )
        except ValueError as exc:
            raise AiGatewayError("ai_request_messages_invalid") from exc

    raw_tools = payload.get("tools", [])
    if not isinstance(raw_tools, list):
        raise AiGatewayError("ai_request_tools_invalid")
    tools: list[ToolDefinition] = []
    strict_tools = False
    for raw_tool in raw_tools:
        if not isinstance(raw_tool, Mapping) or raw_tool.get("type") != "function":
            raise AiGatewayError("ai_request_tools_invalid")
        function = raw_tool.get("function")
        if not isinstance(function, Mapping):
            raise AiGatewayError("ai_request_tools_invalid")
        name = function.get("name")
        description = function.get("description")
        parameters = function.get("parameters")
        strict = function.get("strict")
        if not isinstance(name, str) or not isinstance(description, str) or not isinstance(parameters, Mapping):
            raise AiGatewayError("ai_request_tools_invalid")
        if strict is not None and not isinstance(strict, bool):
            raise AiGatewayError("ai_request_tools_invalid")
        strict_tools = strict_tools or strict is True
        try:
            tools.append(
                ToolDefinition(
                    name=name,
                    description=description,
                    parameters=parameters,
                    strict=strict,
                )
            )
        except ValueError as exc:
            raise AiGatewayError("ai_request_tools_invalid") from exc

    tool_choice = _tool_choice_from_legacy_payload(payload.get("tool_choice"), tools)
    max_tokens = payload.get("max_tokens")
    temperature = payload.get("temperature")
    if max_tokens is not None and (isinstance(max_tokens, bool) or not isinstance(max_tokens, int)):
        raise AiGatewayError("ai_request_max_tokens_invalid")
    if temperature is not None and (isinstance(temperature, bool) or not isinstance(temperature, (int, float))):
        raise AiGatewayError("ai_request_temperature_invalid")
    capabilities = {"chat"}
    if tools:
        capabilities.add("tools")
    if strict_tools:
        capabilities.add("json_schema")
    try:
        return CompletionRequest(
            feature=handle.spec.feature,
            organization_id=handle.organization_id,
            messages=tuple(messages),
            actor_user_id=handle.spec.actor_user_id,
            run_id=handle.run_id,
            business_ref_type=handle.spec.business_ref_type,
            business_ref_id=handle.spec.business_ref_id,
            prompt_revision_id=handle.spec.prompt_revision,
            contract_version=handle.spec.contract_version,
            tools=tuple(tools),
            tool_choice=tool_choice,
            required_capabilities=frozenset(capabilities),
            max_output_tokens=max_tokens,
            temperature=float(temperature) if temperature is not None else None,
        )
    except ValueError as exc:
        raise AiGatewayError("ai_request_invalid") from exc


def _tool_choice_from_legacy_payload(
    value: object,
    tools: list[ToolDefinition],
) -> ToolChoice | None:
    if value is None:
        return None
    if isinstance(value, str):
        if value not in {"auto", "none", "required"}:
            raise AiGatewayError("ai_request_tool_choice_invalid")
        return ToolChoice(mode=value)
    if not isinstance(value, Mapping):
        raise AiGatewayError("ai_request_tool_choice_invalid")
    if value.get("type") != "function":
        raise AiGatewayError("ai_request_tool_choice_invalid")
    function = value.get("function")
    if not isinstance(function, Mapping) or not isinstance(function.get("name"), str):
        raise AiGatewayError("ai_request_tool_choice_invalid")
    if function["name"] not in {tool.name for tool in tools}:
        raise AiGatewayError("ai_request_tool_choice_invalid")
    return ToolChoice.named(function["name"])


def _record_input_fingerprint(handle: _ExecutionHandle, payload: Mapping[str, object]) -> None:
    try:
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise AiGatewayError("ai_request_invalid") from exc
    secret = handle.settings.session_secret
    fingerprint = (
        hmac.new(secret.encode("utf-8"), encoded, hashlib.sha256).hexdigest()
        if secret
        else None
    )
    run = handle.session.get(AiRun, handle.run_id)
    if run is None:
        raise AiGatewayError("ai_run_not_found")
    # Keep the first request's snapshot. Agent turns can legitimately create
    # several completions under one run, and the run remains reproducible via
    # its prompt/contract version plus invocation sequence without persisting
    # any prompt body.
    if run.source_snapshot_hmac is None:
        run.source_snapshot_hmac = fingerprint
        run.input_size_bytes = len(encoded)
        handle.session.commit()


def _execute_completion(handle: _ExecutionHandle, request: CompletionRequest) -> dict[str, object]:
    targets = _validated_targets(handle.route_policy_version)
    previous_failed_invocation_id: str | None = None
    last_error: ProviderError | None = None
    for target_index, target_data in enumerate(targets):
        route, price = _resolve_route_target(
            handle.session,
            settings=handle.settings,
            target_data=target_data,
            required_capabilities=request.required_capabilities,
            max_output_tokens=request.max_output_tokens,
        )
        max_attempts = _target_max_attempts(target_data)
        for attempt_index in range(max_attempts):
            handle.next_attempt_no += 1
            adapter = _adapter_for_driver(route.driver)
            try:
                # Run all deterministic adapter work before recording an
                # external attempt or reserving trial quota.  A malformed
                # route/defaults payload must not consume an allowance.
                adapter.preflight(request, route)
            except ProviderError as exc:
                raise AiGatewayError(f"ai_provider_{exc.category.value}") from exc

            invocation = _start_invocation(
                handle,
                route=route,
                price=price,
                target_index=target_index,
                fallback_of_id=previous_failed_invocation_id,
            )
            started = time.perf_counter()
            try:
                result = adapter.complete(request, route)
            except ProviderError as exc:
                _finish_failed_invocation(
                    handle,
                    invocation,
                    error=exc,
                    latency_ms=_elapsed_ms(started),
                )
                previous_failed_invocation_id = invocation.id
                last_error = exc
                if exc.retryable and attempt_index + 1 < max_attempts:
                    continue
                if exc.fallback_eligible and _target_allows_fallback(
                    target_data,
                    category=exc.category,
                ):
                    break
                raise AiGatewayError(f"ai_provider_{exc.category.value}") from exc
            except BaseException as exc:
                _finish_unexpected_invocation_failure(
                    handle,
                    invocation,
                    latency_ms=_elapsed_ms(started),
                )
                raise AiGatewayError("ai_provider_execution_failed") from exc
            _finish_successful_invocation(
                handle,
                invocation,
                result_usage=result.usage,
                provider_request_id=result.provider_request_id,
                provider_model_id=result.model_id,
                http_status=result.raw_status_code,
                price=price,
                latency_ms=_elapsed_ms(started),
            )
            return dict(result.raw_response)
        # The next target is an explicit fallback, never an automatic
        # cheapest-model decision.
        continue
    if last_error is not None:
        raise AiGatewayError(f"ai_provider_{last_error.category.value}") from last_error
    raise AiGatewayError("ai_route_execution_unavailable")


def _validated_targets(version: AiRoutePolicyVersion) -> list[Mapping[str, object]]:
    raw_targets = version.targets_json
    if not isinstance(raw_targets, list) or not raw_targets:
        raise AiGatewayError("ai_route_targets_invalid")
    targets: list[Mapping[str, object]] = []
    for target in raw_targets:
        if not isinstance(target, Mapping) or not isinstance(target.get("model_profile_id"), str):
            raise AiGatewayError("ai_route_targets_invalid")
        targets.append(target)
    return targets


def _target_max_attempts(target: Mapping[str, object]) -> int:
    value = target.get("max_attempts", 1)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 3:
        raise AiGatewayError("ai_route_retry_policy_invalid")
    return value


def _target_allows_fallback(
    target: Mapping[str, object],
    *,
    category: ProviderErrorCategory,
) -> bool:
    """Return whether this exact target permits the next configured target.

    Missing policy data deliberately means no fallback.  That keeps route
    versions created before the allowlist field was introduced conservative:
    they may retry their current target, but never start a second billable
    provider request without an explicit platform-admin decision.
    """

    value = target.get("allow_fallback_on", [])
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise AiGatewayError("ai_route_fallback_policy_invalid")
    if len(value) != len(set(value)):
        raise AiGatewayError("ai_route_fallback_policy_invalid")
    allowed_categories = {
        ProviderErrorCategory.RATE_LIMITED.value,
        ProviderErrorCategory.QUOTA_EXHAUSTED.value,
        ProviderErrorCategory.TIMEOUT.value,
        ProviderErrorCategory.NETWORK.value,
        ProviderErrorCategory.PROVIDER_5XX.value,
    }
    if any(item not in allowed_categories for item in value):
        raise AiGatewayError("ai_route_fallback_policy_invalid")
    return category.value in value


def _resolve_route_target(
    session: Session,
    *,
    settings: AppSettings,
    target_data: Mapping[str, object],
    required_capabilities: frozenset[str],
    max_output_tokens: int | None,
) -> tuple[RouteTarget, AiModelPriceVersion | None]:
    model_id = target_data["model_profile_id"]
    assert isinstance(model_id, str)
    model = session.get(AiModelProfile, model_id)
    if model is None or not model.enabled or model.retired_at is not None:
        raise AiGatewayError("ai_route_model_unavailable")
    provider = session.get(AiProviderProfile, model.provider_profile_id)
    if provider is None or not provider.enabled or provider.retired_at is not None:
        raise AiGatewayError("ai_route_provider_unavailable")
    if not provider.base_url:
        raise AiGatewayError("ai_route_endpoint_missing")
    if not _model_supports(model, required_capabilities):
        raise AiGatewayError("ai_route_capability_missing")
    if (
        max_output_tokens is not None
        and model.max_output_tokens is not None
        and max_output_tokens > model.max_output_tokens
    ):
        raise AiGatewayError("ai_route_output_limit_exceeded")
    credential = _resolve_credential(settings, provider.credential_ref)
    if not credential:
        raise AiGatewayError("ai_route_credential_not_configured")
    request_defaults = provider.request_defaults_json
    if not isinstance(request_defaults, Mapping):
        raise AiGatewayError("ai_route_defaults_invalid")
    route = RouteTarget(
        id=f"{provider.id}:{model.id}",
        driver=provider.driver,
        provider_profile_id=provider.id,
        model_profile_id=model.id,
        endpoint_url=provider.base_url,
        provider_model_id=model.provider_model_id,
        timeout_seconds=settings.deepseek_timeout_seconds,
        credential=credential,
        authentication=RouteAuthentication(header_name="Authorization", value_prefix="Bearer "),
        request_defaults=request_defaults,
    )
    # Validate the driver before a started ledger invocation exists. An
    # unsupported local configuration has not made an external request and
    # must never be marked as potentially billable.
    _adapter_for_driver(route.driver)
    price = _active_price_version(session, model_id=model.id, at=utcnow())
    return route, price


def _model_supports(model: AiModelProfile, required: frozenset[str]) -> bool:
    raw = model.capabilities_json
    if isinstance(raw, Mapping):
        capabilities = {str(key) for key, enabled in raw.items() if enabled is True}
    elif isinstance(raw, list):
        capabilities = {item for item in raw if isinstance(item, str)}
    else:
        capabilities = set()
    return required.issubset(capabilities)


def _resolve_credential(settings: AppSettings, reference: str | None) -> str | None:
    if not reference:
        return None
    if reference == LEGACY_RUNTIME_CREDENTIAL_REF:
        return settings.deepseek_api_key
    return settings.ai_provider_credentials.get(reference)


def _active_price_version(
    session: Session,
    *,
    model_id: str,
    at: datetime,
) -> AiModelPriceVersion | None:
    return session.scalar(
        select(AiModelPriceVersion)
        .where(
            AiModelPriceVersion.model_profile_id == model_id,
            AiModelPriceVersion.is_active.is_(True),
            AiModelPriceVersion.effective_from <= at,
            or_(
                AiModelPriceVersion.effective_to.is_(None),
                AiModelPriceVersion.effective_to > at,
            ),
        )
        .order_by(AiModelPriceVersion.version.desc())
    )


def _adapter_for_driver(driver: str) -> OpenAICompatibleAdapter:
    if driver == "openai_compatible":
        return OpenAICompatibleAdapter()
    raise AiGatewayError("ai_provider_driver_not_supported")


def _start_invocation(
    handle: _ExecutionHandle,
    *,
    route: RouteTarget,
    price: AiModelPriceVersion | None,
    target_index: int,
    fallback_of_id: str | None,
) -> ApiInvocation:
    # This is the last point before an external provider request. Reserving
    # here, rather than in a feature endpoint, means every real model attempt
    # is covered: normal calls, Agent tool loops, retries, and fallbacks.  This
    # gateway is the model-provider boundary, so ``service_kind`` remains
    # reporting metadata and can never opt an invocation out of the allowance.
    # A future non-LLM OCR transport must use a separate transport boundary.
    try:
        reserve_trial_llm_call(
            handle.session,
            organization_id=handle.organization_id,
        )
    except TrialQuotaError as exc:
        raise AiGatewayError(str(exc)) from exc

    invocation = ApiInvocation(
        organization_id=handle.organization_id,
        ai_run_id=handle.run_id,
        attempt_no=handle.next_attempt_no,
        target_index=target_index,
        fallback_of_id=fallback_of_id,
        provider_profile_id=route.provider_profile_id,
        model_profile_id=route.model_profile_id,
        provider_driver=route.driver,
        provider_model_id=route.provider_model_id,
        status="started",
        may_have_billed=False,
        started_at=utcnow(),
        usage_source="unavailable",
        usage_details_json={},
        price_version_id=price.id if price is not None else None,
        price_snapshot_json=_price_snapshot(price),
        reporting_currency="CNY",
        fx_snapshot_json={},
        cost_source="unavailable",
    )
    handle.session.add(invocation)
    handle.session.commit()
    return invocation


def _finish_successful_invocation(
    handle: _ExecutionHandle,
    invocation: ApiInvocation,
    *,
    result_usage: Any,
    provider_request_id: str | None,
    provider_model_id: str,
    http_status: int,
    price: AiModelPriceVersion | None,
    latency_ms: int,
) -> None:
    now = utcnow()
    invocation = handle.session.get(ApiInvocation, invocation.id)
    if invocation is None:
        raise AiGatewayError("api_invocation_not_found")
    invocation.status = "succeeded"
    invocation.provider_request_id = provider_request_id
    invocation.provider_model_id = provider_model_id
    invocation.http_status = http_status
    invocation.completed_at = now
    invocation.latency_ms = latency_ms
    invocation.may_have_billed = False
    if result_usage is not None:
        invocation.input_tokens = result_usage.input_tokens
        invocation.cached_read_input_tokens = result_usage.cached_read_input_tokens
        invocation.cached_write_input_tokens = result_usage.cached_write_input_tokens
        invocation.output_tokens = result_usage.output_tokens
        invocation.reasoning_tokens = result_usage.reasoning_tokens
        invocation.image_units = result_usage.image_units
        invocation.page_units = result_usage.page_units
        invocation.request_units = result_usage.request_units
        invocation.usage_source = "provider"
        invocation.usage_details_json = {
            "provider_reported_total_tokens": result_usage.provider_reported_total_tokens,
        }
        cost = _calculate_price_snapshot_cost_micros(result_usage, price)
        if cost is not None and price is not None:
            invocation.calculated_cost_provider_micros = cost
            invocation.provider_currency = price.currency
            if price.currency == "CNY":
                invocation.reporting_cost_micros = cost
                invocation.cost_source = "price_snapshot"
            else:
                invocation.cost_source = "price_snapshot_unconverted"
    handle.session.commit()
    _refresh_run_cost(handle)


def _finish_failed_invocation(
    handle: _ExecutionHandle,
    invocation: ApiInvocation,
    *,
    error: ProviderError,
    latency_ms: int,
) -> None:
    persisted = handle.session.get(ApiInvocation, invocation.id)
    if persisted is None:
        raise AiGatewayError("api_invocation_not_found")
    persisted.status = "failed"
    persisted.error_category = error.category.value
    persisted.error_code = f"ai_provider_{error.category.value}"
    persisted.provider_request_id = error.provider_request_id
    persisted.http_status = error.http_status_code
    persisted.may_have_billed = error.may_have_billed
    persisted.completed_at = utcnow()
    persisted.latency_ms = latency_ms
    handle.session.commit()
    _refresh_run_cost(handle)


def _finish_unexpected_invocation_failure(
    handle: _ExecutionHandle,
    invocation: ApiInvocation,
    *,
    latency_ms: int,
) -> None:
    persisted = handle.session.get(ApiInvocation, invocation.id)
    if persisted is None:
        return
    persisted.status = "failed"
    persisted.error_category = "internal"
    persisted.error_code = "ai_provider_execution_failed"
    persisted.may_have_billed = True
    persisted.completed_at = utcnow()
    persisted.latency_ms = latency_ms
    handle.session.commit()
    _refresh_run_cost(handle)


def _price_snapshot(price: AiModelPriceVersion | None) -> dict[str, object]:
    if price is None:
        return {}
    def value(item: Decimal | None) -> str | None:
        return str(item) if item is not None else None

    return {
        "version": price.version,
        "currency": price.currency,
        "input_price_per_million": value(price.input_price_per_million),
        "cached_read_input_price_per_million": value(price.cached_read_input_price_per_million),
        "cached_write_input_price_per_million": value(price.cached_write_input_price_per_million),
        "output_price_per_million": value(price.output_price_per_million),
        "reasoning_price_per_million": value(price.reasoning_price_per_million),
        "request_price": value(price.request_price),
        "page_price": value(price.page_price),
        "source": price.source,
    }


def _calculate_price_snapshot_cost_micros(usage: Any, price: AiModelPriceVersion | None) -> int | None:
    if price is None:
        return None
    components: tuple[tuple[int, Decimal | None, bool], ...] = (
        (usage.input_tokens, price.input_price_per_million, True),
        (usage.cached_read_input_tokens, price.cached_read_input_price_per_million, True),
        (usage.cached_write_input_tokens, price.cached_write_input_price_per_million, True),
        (usage.output_tokens, price.output_price_per_million, True),
        (usage.reasoning_tokens, price.reasoning_price_per_million, True),
        (usage.page_units, price.page_price, False),
        (usage.image_units, None, False),
    )
    total = Decimal("0")
    for units, unit_price, per_million in components:
        if units <= 0:
            continue
        if unit_price is None:
            return None
        divisor = _TOKENS_PER_PRICE_UNIT if per_million else Decimal("1")
        total += Decimal(units) * unit_price / divisor
    # Request pricing is optional for token-metered models.  When configured,
    # it is added; when omitted it means zero rather than an unknown token cost.
    if price.request_price is not None and usage.request_units > 0:
        total += Decimal(usage.request_units) * price.request_price
    return int((total * _MICROS_PER_CURRENCY_UNIT).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _refresh_run_cost(handle: _ExecutionHandle) -> None:
    run = handle.session.get(AiRun, handle.run_id)
    if run is None:
        return
    invocations = list(
        handle.session.scalars(
            select(ApiInvocation).where(ApiInvocation.ai_run_id == handle.run_id)
        )
    )
    total_cost, cost_status = _summarize_run_cost(invocations)
    run.total_cost_reporting_micros = total_cost
    run.cost_status = cost_status
    handle.session.commit()


def _summarize_run_cost(
    invocations: list[ApiInvocation],
) -> tuple[int | None, str]:
    """Summarize only reporting-currency costs without hiding unknown calls."""

    known_costs = [item.reporting_cost_micros for item in invocations if item.reporting_cost_micros is not None]
    potentially_billed_unknown = any(
        item.may_have_billed and item.reporting_cost_micros is None for item in invocations
    )
    successful_or_started_unknown = any(
        item.reporting_cost_micros is None
        and item.status in {"started", "succeeded"}
        for item in invocations
    )
    if known_costs:
        return (
            sum(known_costs),
            "partial"
            if potentially_billed_unknown or successful_or_started_unknown
            else "known",
        )
    return (
        None,
        "partial" if potentially_billed_unknown else "unavailable",
    )


def _finish_run(handle: _ExecutionHandle, *, status: str, failure_code: str | None) -> None:
    run = handle.session.get(AiRun, handle.run_id)
    if run is None:
        return
    _refresh_run_cost(handle)
    run = handle.session.get(AiRun, handle.run_id)
    if run is None:
        return
    run.status = status
    run.failure_code = failure_code
    run.finished_at = utcnow()
    handle.session.commit()


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))


def _safe_failure_code(exc: BaseException) -> str:
    if isinstance(exc, AiGatewayError):
        return str(exc)[:128]
    if isinstance(exc, ProviderError):
        return f"ai_provider_{exc.category.value}"
    # Existing domain validators use stable snake-case failures.  Preserve a
    # bounded, non-sensitive token only; never store arbitrary exception text.
    text = str(exc)
    if text and len(text) <= 128 and all(ch.isalnum() or ch in {"_", "-"} for ch in text):
        return text
    return "ai_business_validation_failed"


__all__ = [
    "AiExecutionSpec",
    "AiGatewayError",
    "LEGACY_RUNTIME_CREDENTIAL_REF",
    "SUPPORTED_AI_FEATURES",
    "active_legacy_payload_executor",
    "ai_provider_credential_configured",
    "ai_gateway_credentials_configured",
    "ai_gateway_execution",
    "gateway_prompt_transport_arguments",
    "resolve_active_route_policy_version_id",
]
