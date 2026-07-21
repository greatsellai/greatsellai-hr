"""Platform-only management of AI providers, models, prices, and routes."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import AppSettings
from app.models import (
    AiModelPriceVersion,
    AiModelProfile,
    AiProviderProfile,
    AiRoutePolicy,
    AiRoutePolicyVersion,
    utcnow,
)
from app.schemas import (
    AiModelPriceVersionCreate,
    AiModelPriceVersionResponse,
    AiModelProfileCreate,
    AiModelProfileResponse,
    AiProviderProfileCreate,
    AiProviderProfileResponse,
    AiRoutePolicyPublish,
    AiRoutePolicyResponse,
    AiRoutePolicyVersionResponse,
    AiRouteTargetInput,
)
from app.services.ai_gateway_service import (
    SUPPORTED_AI_FEATURES,
    ai_provider_credential_configured,
)


class AiGatewayConfigurationError(RuntimeError):
    """Stable control-plane failures, safe for platform API responses."""


def list_provider_profiles(
    session: Session,
    *,
    settings: AppSettings,
) -> list[AiProviderProfileResponse]:
    profiles = list(session.scalars(select(AiProviderProfile).order_by(AiProviderProfile.slug)))
    return [_provider_response(profile, settings=settings) for profile in profiles]


def create_provider_profile(
    session: Session,
    *,
    payload: AiProviderProfileCreate,
    settings: AppSettings,
) -> AiProviderProfileResponse:
    if session.scalar(select(AiProviderProfile.id).where(AiProviderProfile.slug == payload.slug)):
        raise AiGatewayConfigurationError("ai_provider_slug_exists")
    profile = AiProviderProfile(
        slug=payload.slug,
        display_name=payload.display_name,
        driver=payload.driver,
        base_url=payload.endpoint_url,
        credential_ref=payload.credential_ref,
        request_defaults_json=dict(payload.request_defaults),
        enabled=payload.is_enabled,
    )
    session.add(profile)
    session.flush()
    return _provider_response(profile, settings=settings)


def list_model_profiles(session: Session) -> list[AiModelProfileResponse]:
    rows = session.execute(
        select(AiModelProfile, AiProviderProfile.slug)
        .join(AiProviderProfile, AiProviderProfile.id == AiModelProfile.provider_profile_id)
        .order_by(AiModelProfile.slug)
    )
    return [_model_response(model, provider_slug) for model, provider_slug in rows]


def create_model_profile(
    session: Session,
    *,
    payload: AiModelProfileCreate,
) -> AiModelProfileResponse:
    provider = session.scalar(
        select(AiProviderProfile).where(AiProviderProfile.slug == payload.provider_slug)
    )
    if provider is None:
        raise AiGatewayConfigurationError("ai_provider_not_found")
    if session.scalar(select(AiModelProfile.id).where(AiModelProfile.slug == payload.slug)):
        raise AiGatewayConfigurationError("ai_model_slug_exists")
    model = AiModelProfile(
        provider_profile_id=provider.id,
        slug=payload.slug,
        display_name=payload.display_name,
        provider_model_id=payload.provider_model_id,
        capabilities_json={capability: True for capability in payload.capabilities},
        context_window=payload.context_window_tokens,
        max_output_tokens=payload.max_output_tokens,
        data_classification_json={"candidate_data_allowed": True},
        enabled=payload.is_enabled,
    )
    session.add(model)
    session.flush()
    return _model_response(model, provider.slug)


def list_model_price_versions(session: Session) -> list[AiModelPriceVersionResponse]:
    rows = session.execute(
        select(AiModelPriceVersion, AiModelProfile.slug)
        .join(AiModelProfile, AiModelProfile.id == AiModelPriceVersion.model_profile_id)
        .order_by(AiModelProfile.slug, AiModelPriceVersion.version.desc())
    )
    return [_price_response(version, model_slug) for version, model_slug in rows]


def create_model_price_version(
    session: Session,
    *,
    payload: AiModelPriceVersionCreate,
    created_by_user_id: str,
) -> AiModelPriceVersionResponse:
    model = session.scalar(select(AiModelProfile).where(AiModelProfile.slug == payload.model_slug))
    if model is None:
        raise AiGatewayConfigurationError("ai_model_not_found")
    current_version = session.scalar(
        select(func.max(AiModelPriceVersion.version)).where(
            AiModelPriceVersion.model_profile_id == model.id
        )
    )
    price = AiModelPriceVersion(
        model_profile_id=model.id,
        version=int(current_version or 0) + 1,
        currency=payload.currency,
        effective_from=payload.effective_from,
        effective_to=payload.effective_to,
        input_price_per_million=payload.input_per_million,
        cached_read_input_price_per_million=payload.cached_read_input_per_million,
        cached_write_input_price_per_million=payload.cached_write_input_per_million,
        output_price_per_million=payload.output_per_million,
        reasoning_price_per_million=payload.reasoning_per_million,
        request_price=payload.request_unit_price,
        page_price=payload.page_unit_price,
        source=payload.source,
        is_active=payload.is_active,
        created_by_user_id=created_by_user_id,
    )
    session.add(price)
    session.flush()
    return _price_response(price, model.slug)


def list_route_policies(session: Session) -> list[AiRoutePolicyResponse]:
    policies = list(session.scalars(select(AiRoutePolicy).order_by(AiRoutePolicy.feature)))
    return [_route_policy_response(policy) for policy in policies]


def list_route_policy_versions(
    session: Session,
    *,
    feature: str,
) -> list[AiRoutePolicyVersionResponse]:
    policy = session.scalar(select(AiRoutePolicy).where(AiRoutePolicy.feature == feature))
    if policy is None:
        raise AiGatewayConfigurationError("ai_route_policy_not_found")
    versions = list(
        session.scalars(
            select(AiRoutePolicyVersion)
            .where(AiRoutePolicyVersion.policy_id == policy.id)
            .order_by(AiRoutePolicyVersion.version.desc())
        )
    )
    return [_route_version_response(session, policy, version) for version in versions]


def publish_route_policy(
    session: Session,
    *,
    feature: str,
    payload: AiRoutePolicyPublish,
    published_by_user_id: str,
    settings: AppSettings,
) -> AiRoutePolicyVersionResponse:
    if feature not in SUPPORTED_AI_FEATURES:
        raise AiGatewayConfigurationError("unsupported_ai_feature")
    models = _models_for_route_targets(session, payload.targets, settings=settings)
    policy = session.scalar(select(AiRoutePolicy).where(AiRoutePolicy.feature == feature))
    if policy is None:
        policy = AiRoutePolicy(
            feature=feature,
            display_name=payload.display_name,
            description=payload.description,
            enabled=True,
        )
        session.add(policy)
        session.flush()
    else:
        policy.display_name = payload.display_name
        policy.description = payload.description
        policy.enabled = True
    current_version = session.scalar(
        select(func.max(AiRoutePolicyVersion.version)).where(
            AiRoutePolicyVersion.policy_id == policy.id
        )
    )
    version = AiRoutePolicyVersion(
        policy_id=policy.id,
        version=int(current_version or 0) + 1,
        status="published",
        targets_json=[
            {
                "model_profile_id": model.id,
                "max_attempts": target.max_attempts,
                "allow_fallback_on": list(target.allow_fallback_on),
            }
            for target, model in zip(payload.targets, models, strict=True)
        ],
        retry_policy_json={},
        max_cost_guard_json={},
        prompt_revision=payload.prompt_revision,
        published_by_user_id=published_by_user_id,
        published_at=utcnow(),
        supersedes_version_id=policy.active_version_id,
    )
    session.add(version)
    session.flush()
    policy.active_version_id = version.id
    session.flush()
    return _route_version_response(session, policy, version)


def _models_for_route_targets(
    session: Session,
    targets: Iterable[AiRouteTargetInput],
    *,
    settings: AppSettings,
) -> list[AiModelProfile]:
    models: list[AiModelProfile] = []
    for target in targets:
        model = session.scalar(select(AiModelProfile).where(AiModelProfile.slug == target.model_slug))
        if model is None:
            raise AiGatewayConfigurationError("ai_route_model_not_found")
        if not model.enabled or model.retired_at is not None:
            raise AiGatewayConfigurationError("ai_route_model_unavailable")
        provider = session.get(AiProviderProfile, model.provider_profile_id)
        if provider is None or not provider.enabled or provider.retired_at is not None:
            raise AiGatewayConfigurationError("ai_route_provider_unavailable")
        if not ai_provider_credential_configured(settings, provider.credential_ref):
            raise AiGatewayConfigurationError("ai_route_credential_not_configured")
        models.append(model)
    return models


def _provider_response(
    profile: AiProviderProfile,
    *,
    settings: AppSettings,
) -> AiProviderProfileResponse:
    return AiProviderProfileResponse(
        provider_id=profile.id,
        slug=profile.slug,
        display_name=profile.display_name,
        driver=profile.driver,
        endpoint_url=profile.base_url or "",
        credential_ref=profile.credential_ref or "",
        credential_configured=ai_provider_credential_configured(
            settings,
            profile.credential_ref,
        ),
        request_defaults=dict(profile.request_defaults_json or {}),
        is_enabled=profile.enabled,
        created_at=profile.created_at,
        updated_at=profile.updated_at,
    )


def _model_response(model: AiModelProfile, provider_slug: str) -> AiModelProfileResponse:
    raw_capabilities = model.capabilities_json if isinstance(model.capabilities_json, dict) else {}
    return AiModelProfileResponse(
        model_id=model.id,
        slug=model.slug,
        provider_id=model.provider_profile_id,
        provider_slug=provider_slug,
        display_name=model.display_name,
        provider_model_id=model.provider_model_id,
        capabilities=[key for key, value in raw_capabilities.items() if value is True],
        context_window_tokens=model.context_window,
        max_output_tokens=model.max_output_tokens,
        is_enabled=model.enabled,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def _price_response(
    price: AiModelPriceVersion,
    model_slug: str,
) -> AiModelPriceVersionResponse:
    return AiModelPriceVersionResponse(
        price_version_id=price.id,
        model_id=price.model_profile_id,
        model_slug=model_slug,
        currency=price.currency,
        effective_from=price.effective_from,
        effective_to=price.effective_to,
        input_per_million=price.input_price_per_million,
        cached_read_input_per_million=price.cached_read_input_price_per_million,
        cached_write_input_per_million=price.cached_write_input_price_per_million,
        output_per_million=price.output_price_per_million,
        reasoning_per_million=price.reasoning_price_per_million,
        request_unit_price=price.request_price,
        page_unit_price=price.page_price,
        source=price.source,
        is_active=price.is_active,
        created_at=price.created_at,
    )


def _route_policy_response(policy: AiRoutePolicy) -> AiRoutePolicyResponse:
    active_version = policy.active_version
    return AiRoutePolicyResponse(
        policy_id=policy.id,
        feature=policy.feature,
        display_name=policy.display_name,
        description=policy.description,
        current_version=active_version.version if active_version is not None else None,
        is_enabled=policy.enabled,
        updated_at=policy.updated_at,
    )


def _route_version_response(
    session: Session,
    policy: AiRoutePolicy,
    version: AiRoutePolicyVersion,
) -> AiRoutePolicyVersionResponse:
    model_ids = [
        target.get("model_profile_id")
        for target in version.targets_json
        if isinstance(target, dict) and isinstance(target.get("model_profile_id"), str)
    ]
    rows = list(
        session.execute(
            select(AiModelProfile.id, AiModelProfile.slug).where(AiModelProfile.id.in_(model_ids))
        )
    ) if model_ids else []
    slug_by_id = {model_id: slug for model_id, slug in rows}
    targets: list[AiRouteTargetInput] = []
    for target in version.targets_json:
        if not isinstance(target, dict):
            continue
        model_id = target.get("model_profile_id")
        max_attempts = target.get("max_attempts", 1)
        allow_fallback_on = target.get("allow_fallback_on", [])
        if (
            not isinstance(model_id, str)
            or not isinstance(max_attempts, int)
            or not isinstance(allow_fallback_on, list)
            or any(not isinstance(category, str) for category in allow_fallback_on)
        ):
            continue
        model_slug = slug_by_id.get(model_id)
        if model_slug is not None:
            targets.append(
                AiRouteTargetInput(
                    model_slug=model_slug,
                    max_attempts=max_attempts,
                    allow_fallback_on=allow_fallback_on,
                )
            )
    return AiRoutePolicyVersionResponse(
        route_policy_version_id=version.id,
        policy_id=policy.id,
        feature=policy.feature,
        version=version.version,
        targets=targets,
        prompt_revision=version.prompt_revision,
        published_at=version.published_at or version.created_at,
        published_by_user_id=version.published_by_user_id,
    )


def commit_or_raise_configuration_conflict(session: Session) -> None:
    """Commit a platform change with a stable response for concurrent edits."""

    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise AiGatewayConfigurationError("ai_gateway_configuration_conflict") from exc


__all__ = [
    "AiGatewayConfigurationError",
    "commit_or_raise_configuration_conflict",
    "create_model_price_version",
    "create_model_profile",
    "create_provider_profile",
    "list_model_price_versions",
    "list_model_profiles",
    "list_provider_profiles",
    "list_route_policies",
    "list_route_policy_versions",
    "publish_route_policy",
]
