import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { Icon } from "../../icons";
import { AdminApiError, adminApi, adminErrorMessage } from "../admin-api";
import {
  AdminStatus,
  formatDate,
} from "../AdminComponents";
import type {
  AiModelCapability,
  AiModelProfile,
  AiModelProfileCreateInput,
  AiProviderProfile,
  AiProviderProfileCreateInput,
  AiRouteFallbackCategory,
  AiRoutePolicy,
  AiRoutePolicyPublishInput,
  AiRoutePolicyVersion,
} from "../admin-types";

type ConfigurationSection = "provider" | "model" | "route";

type RouteTargetDraft = {
  model_slug: string;
  max_attempts: number;
  allow_fallback_on: AiRouteFallbackCategory[];
};

type RouteDraft = {
  display_name: string;
  description: string;
  prompt_revision: string;
  reason: string;
  targets: RouteTargetDraft[];
};

const routeFeatures = [
  { value: "resume_extract_rich", label: "简历深度提取", detail: "提取完整的候选人结构化信息" },
  { value: "resume_extract_core", label: "简历核心提取", detail: "提取筛选所需的核心字段" },
  { value: "candidate_name_backfill", label: "候选人姓名补全", detail: "基于原始简历补全可靠姓名" },
  { value: "resume_score", label: "简历评分", detail: "按岗位设置的权重生成评分" },
  { value: "resume_summary", label: "简历总结", detail: "生成候选人概览与经历摘要" },
  { value: "jd_generate", label: "JD 生成", detail: "根据需求撰写岗位 JD" },
  { value: "jd_requirements_extract", label: "JD 要求提取", detail: "将岗位 JD 归一为评分要求" },
  { value: "jd_match", label: "JD 匹配", detail: "分析候选人与岗位的匹配度" },
  { value: "recruiting_agent_turn", label: "招聘 Agent 对话", detail: "为招聘助手生成下一轮回复" },
  { value: "resume_ocr_page", label: "简历 OCR", detail: "识别扫描件或图片简历页面" },
] as const;

type RouteFeature = (typeof routeFeatures)[number]["value"];

const fallbackCategories: Array<{ value: AiRouteFallbackCategory; label: string }> = [
  { value: "rate_limited", label: "限流" },
  { value: "quota_exhausted", label: "额度耗尽" },
  { value: "timeout", label: "超时" },
  { value: "network", label: "网络异常" },
  { value: "provider_5xx", label: "服务端 5xx" },
];

const fallbackCategoryValues = new Set<AiRouteFallbackCategory>(
  fallbackCategories.map((category) => category.value),
);

type RouteTargetSummary = {
  model_slug: string;
  max_attempts: number;
  allow_fallback_on: string[];
};

type RoutePublicationReview = {
  feature: RouteFeature;
  featureLabel: string;
  currentPolicy: AiRoutePolicy | null;
  currentVersion: AiRoutePolicyVersion | null;
  currentVersionNumber: number | null;
  nextVersion: number;
  payload: AiRoutePolicyPublishInput;
  changes: string[];
};

const structuredRouteFeatures = new Set([
  "resume_extract_rich",
  "resume_extract_core",
  "candidate_name_backfill",
  "resume_score",
  "resume_summary",
  "jd_generate",
  "jd_requirements_extract",
  "jd_match",
]);

function routeCapabilityRequirements(feature: string): AiModelCapability[] {
  if (structuredRouteFeatures.has(feature)) return ["chat", "tools", "json_schema"];
  if (feature === "recruiting_agent_turn") return ["chat", "tools"];
  return ["chat"];
}

function supportsRouteCapabilities(model: AiModelProfile, capabilities: AiModelCapability[]) {
  return capabilities.every((capability) => model.capabilities.includes(capability));
}

function optionalPositiveInteger(value: string, label: string) {
  const normalized = value.trim();
  if (!normalized) return undefined;
  const number = Number(normalized);
  if (!Number.isInteger(number) || number < 1) throw new Error(`${label}必须是大于 0 的整数。`);
  return number;
}

function reasonValue(value: string) {
  const normalized = value.trim();
  if (!normalized) throw new Error("请填写本次变更原因，便于平台审计。");
  return normalized;
}

function initialRouteTarget(modelSlug = ""): RouteTargetDraft {
  return { model_slug: modelSlug, max_attempts: 1, allow_fallback_on: [] };
}

function latestRouteVersion(versions: AiRoutePolicyVersion[]) {
  return versions.reduce<AiRoutePolicyVersion | null>((latest, version) => (
    !latest || version.version > latest.version ? version : latest
  ), null);
}

function routeDraftFromPublishedConfiguration(
  featureLabel: string,
  policy: AiRoutePolicy | null,
  version: AiRoutePolicyVersion | null,
  defaultModelSlug: string,
): RouteDraft {
  const targets = version?.targets.length
    ? version.targets.map((target) => ({
        model_slug: target.model_slug,
        max_attempts: Math.min(3, Math.max(1, target.max_attempts)),
        allow_fallback_on: target.allow_fallback_on.filter(
          (category): category is AiRouteFallbackCategory => fallbackCategoryValues.has(category as AiRouteFallbackCategory),
        ),
      }))
    : [initialRouteTarget(defaultModelSlug)];
  return {
    display_name: policy?.display_name ?? featureLabel,
    description: policy?.description ?? "",
    prompt_revision: version?.prompt_revision ?? "",
    reason: "",
    targets,
  };
}

function routeModelChain(targets: RouteTargetSummary[]) {
  return targets.length ? targets.map((target) => target.model_slug).join(" → ") : "未配置";
}

function routeRetrySummary(targets: RouteTargetSummary[]) {
  return targets.length
    ? targets.map((target) => `${target.model_slug}：${target.max_attempts} 次`).join("；")
    : "未配置";
}

function routeFallbackSummary(targets: RouteTargetSummary[]) {
  const summaries = targets.flatMap((target) => {
    if (!target.allow_fallback_on.length) return [];
    const labels = target.allow_fallback_on.map((category) => (
      fallbackCategories.find((item) => item.value === category)?.label ?? category
    ));
    return [`${target.model_slug}：${labels.join("、")}`];
  });
  return summaries.length ? summaries.join("；") : "不回退";
}

function normalizedTargetFingerprint(targets: RouteTargetSummary[], field: "max_attempts" | "allow_fallback_on") {
  return JSON.stringify(targets.map((target) => (
    field === "max_attempts"
      ? [target.model_slug, target.max_attempts]
      : [target.model_slug, [...target.allow_fallback_on].sort()]
  )));
}

function routePublicationChanges(
  policy: AiRoutePolicy | null,
  version: AiRoutePolicyVersion | null,
  payload: AiRoutePolicyPublishInput,
) {
  if (!version) return ["这是该功能的首个路由版本，发布后新任务将开始使用这套配置。"];
  const changes: string[] = [];
  if ((policy?.display_name ?? "") !== payload.display_name) changes.push("路由显示名称已修改。");
  if ((policy?.description ?? "") !== (payload.description ?? "")) changes.push("用途说明已修改。");
  if (routeModelChain(version.targets) !== routeModelChain(payload.targets)) changes.push("模型调用链将发生变化。");
  if (normalizedTargetFingerprint(version.targets, "max_attempts") !== normalizedTargetFingerprint(payload.targets, "max_attempts")) {
    changes.push("至少一个模型的最大尝试次数将发生变化。");
  }
  if (normalizedTargetFingerprint(version.targets, "allow_fallback_on") !== normalizedTargetFingerprint(payload.targets, "allow_fallback_on")) {
    changes.push("失败回退条件将发生变化。");
  }
  if ((version.prompt_revision ?? "") !== (payload.prompt_revision ?? "")) changes.push("提示词版本将发生变化。");
  return changes.length ? changes : ["配置内容与当前版本一致，确认后仍会生成一个新的不可变版本。"];
}

export function AdminAiConfigurationPanel({
  providers,
  models,
  routes,
  onChanged,
}: {
  providers: AiProviderProfile[];
  models: AiModelProfile[];
  routes: AiRoutePolicy[];
  onChanged: () => Promise<void>;
}) {
  const [section, setSection] = useState<ConfigurationSection>("provider");
  const [saving, setSaving] = useState<ConfigurationSection | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const enabledProviders = useMemo(() => providers.filter((provider) => provider.is_enabled), [providers]);
  const routableProviderCount = useMemo(
    () => providers.filter((provider) => provider.is_enabled && provider.credential_configured).length,
    [providers],
  );

  const [providerDraft, setProviderDraft] = useState({
    display_name: "",
    slug: "",
    endpoint_url: "",
    credential_ref: "",
    is_enabled: true,
    reason: "",
  });
  const [modelDraft, setModelDraft] = useState({
    display_name: "",
    slug: "",
    provider_slug: "",
    provider_model_id: "",
    chat: true,
    tools: true,
    json_schema: true,
    context_window_tokens: "",
    max_output_tokens: "",
    is_enabled: true,
    reason: "",
  });
  const [routeFeature, setRouteFeature] = useState<RouteFeature>("resume_extract_rich");
  const routeCapabilities = useMemo(() => routeCapabilityRequirements(routeFeature), [routeFeature]);
  const routeModels = useMemo(() => models.filter((model) => (
    model.is_enabled
    && providers.some((provider) => (
      provider.slug === model.provider_slug
      && provider.is_enabled
      && provider.credential_configured
    ))
    && supportsRouteCapabilities(model, routeCapabilities)
  )), [models, providers, routeCapabilities]);
  const [routeDraft, setRouteDraft] = useState<RouteDraft>({
    display_name: routeFeatures[0].label,
    description: "",
    prompt_revision: "",
    reason: "",
    targets: [initialRouteTarget()],
  });
  const [routeVersions, setRouteVersions] = useState<AiRoutePolicyVersion[]>([]);
  const [routeVersionsState, setRouteVersionsState] = useState<"idle" | "loading" | "error" | "ready">("idle");
  const [routeVersionsError, setRouteVersionsError] = useState("");
  const [routeReview, setRouteReview] = useState<RoutePublicationReview | null>(null);
  const routeDraftDirtyRef = useRef(false);
  const routePublishingRef = useRef(false);
  const routeVersionRequestRef = useRef(0);
  const routeReviewHeadingRef = useRef<HTMLHeadingElement>(null);
  const routeImpactButtonRef = useRef<HTMLButtonElement>(null);

  const selectedRouteFeature = routeFeatures.find((feature) => feature.value === routeFeature) ?? routeFeatures[0];
  const currentRoute = routes.find((route) => route.feature === routeFeature) ?? null;
  const activeRouteVersion = latestRouteVersion(routeVersions);
  const routeCapabilitiesLabel = routeCapabilities.join("、");

  useEffect(() => {
    if (!modelDraft.provider_slug && enabledProviders[0]) {
      setModelDraft((draft) => ({ ...draft, provider_slug: enabledProviders[0].slug }));
    }
  }, [enabledProviders, modelDraft.provider_slug]);

  const loadRouteVersions = useCallback(async () => {
    if (section !== "route") return;
    const requestId = ++routeVersionRequestRef.current;
    setRouteVersionsState("loading");
    setRouteVersionsError("");
    try {
      let versions: AiRoutePolicyVersion[];
      try {
        versions = await adminApi.listAiRouteVersions(routeFeature);
      } catch (requestError) {
        if (requestError instanceof AdminApiError && requestError.code === "ai_route_policy_not_found") {
          versions = [];
        } else {
          throw requestError;
        }
      }
      if (requestId !== routeVersionRequestRef.current) return;
      setRouteVersions(versions);
      if (!routeDraftDirtyRef.current) {
        const policy = routes.find((route) => route.feature === routeFeature) ?? null;
        const version = latestRouteVersion(versions);
        const featureLabel = routeFeatures.find((feature) => feature.value === routeFeature)?.label ?? routeFeature;
        setRouteDraft(routeDraftFromPublishedConfiguration(featureLabel, policy, version, routeModels[0]?.slug ?? ""));
      }
      setRouteVersionsState("ready");
    } catch (requestError) {
      if (requestId !== routeVersionRequestRef.current) return;
      setRouteVersionsError(adminErrorMessage(requestError));
      setRouteVersionsState("error");
    }
  }, [routeFeature, routeModels, routes, section]);

  useEffect(() => {
    if (section !== "route") return undefined;
    void loadRouteVersions();
    return () => { routeVersionRequestRef.current += 1; };
  }, [loadRouteVersions, section]);

  useEffect(() => {
    if (routeReview) routeReviewHeadingRef.current?.focus();
  }, [routeReview]);

  const beginSubmission = (nextSection: ConfigurationSection) => {
    setSaving(nextSection);
    setError("");
    setNotice("");
  };

  const finishSubmission = async (message: string) => {
    setNotice(message);
    try {
      await onChanged();
    } catch (refreshError) {
      setError(`配置已保存，但资源列表刷新失败：${adminErrorMessage(refreshError)}`);
    } finally {
      setSaving(null);
    }
  };

  const failSubmission = (submissionError: unknown) => {
    setSaving(null);
    setError(adminErrorMessage(submissionError));
  };

  const submitProvider = async (event: FormEvent) => {
    event.preventDefault();
    beginSubmission("provider");
    try {
      const payload: AiProviderProfileCreateInput = {
        display_name: providerDraft.display_name.trim(),
        slug: providerDraft.slug.trim().toLowerCase(),
        driver: "openai_compatible",
        endpoint_url: providerDraft.endpoint_url.trim(),
        credential_ref: providerDraft.credential_ref.trim(),
        request_defaults: {},
        is_enabled: providerDraft.is_enabled,
        reason: reasonValue(providerDraft.reason),
      };
      const created = await adminApi.createAiProvider(payload);
      setProviderDraft({ display_name: "", slug: "", endpoint_url: "", credential_ref: "", is_enabled: true, reason: "" });
      await finishSubmission(`Provider「${created.display_name}」已创建。`);
    } catch (submissionError) {
      failSubmission(submissionError);
    }
  };

  const submitModel = async (event: FormEvent) => {
    event.preventDefault();
    beginSubmission("model");
    try {
      const capabilities: AiModelCapability[] = ["chat"];
      if (modelDraft.tools) capabilities.push("tools");
      if (modelDraft.json_schema) capabilities.push("json_schema");
      const payload: AiModelProfileCreateInput = {
        display_name: modelDraft.display_name.trim(),
        slug: modelDraft.slug.trim().toLowerCase(),
        provider_slug: modelDraft.provider_slug,
        provider_model_id: modelDraft.provider_model_id.trim(),
        capabilities,
        context_window_tokens: optionalPositiveInteger(modelDraft.context_window_tokens, "上下文窗口"),
        max_output_tokens: optionalPositiveInteger(modelDraft.max_output_tokens, "最大输出"),
        is_enabled: modelDraft.is_enabled,
        reason: reasonValue(modelDraft.reason),
      };
      const created = await adminApi.createAiModel(payload);
      setModelDraft({
        display_name: "", slug: "", provider_slug: enabledProviders[0]?.slug ?? "", provider_model_id: "", chat: true, tools: true, json_schema: true,
        context_window_tokens: "", max_output_tokens: "", is_enabled: true, reason: "",
      });
      await finishSubmission(`模型「${created.display_name}」已创建。`);
    } catch (submissionError) {
      failSubmission(submissionError);
    }
  };

  const prepareRouteReview = (event: FormEvent) => {
    event.preventDefault();
    setError("");
    setNotice("");
    try {
      if (routeVersionsState !== "ready") throw new Error("当前路由版本尚未加载完成，请稍后再检查发布影响。");
      if (routeDraft.targets.some((target) => !target.model_slug)) throw new Error("请为每个路由目标选择一个模型。");
      if (new Set(routeDraft.targets.map((target) => target.model_slug)).size !== routeDraft.targets.length) throw new Error("同一个模型只能在路由中出现一次。");
      const availableModelSlugs = new Set(routeModels.map((model) => model.slug));
      if (routeDraft.targets.some((target) => !availableModelSlugs.has(target.model_slug))) {
        throw new Error("当前草稿包含已停用或能力不匹配的模型，请重新选择后再发布。");
      }
      const displayName = routeDraft.display_name.trim();
      if (!displayName) throw new Error("请填写路由显示名称。");
      const payload: AiRoutePolicyPublishInput = {
        display_name: displayName,
        ...(routeDraft.description.trim() ? { description: routeDraft.description.trim() } : {}),
        ...(routeDraft.prompt_revision.trim() ? { prompt_revision: routeDraft.prompt_revision.trim() } : {}),
        targets: routeDraft.targets.map((target) => ({
          model_slug: target.model_slug,
          max_attempts: target.max_attempts,
          allow_fallback_on: target.allow_fallback_on,
        })),
        reason: reasonValue(routeDraft.reason),
      };
      const latestVersionNumber = Math.max(
        currentRoute?.current_version ?? 0,
        ...routeVersions.map((version) => version.version),
      );
      setRouteReview({
        feature: routeFeature,
        featureLabel: selectedRouteFeature.label,
        currentPolicy: currentRoute,
        currentVersion: activeRouteVersion,
        currentVersionNumber: activeRouteVersion?.version ?? currentRoute?.current_version ?? null,
        nextVersion: latestVersionNumber + 1,
        payload,
        changes: routePublicationChanges(currentRoute, activeRouteVersion, payload),
      });
    } catch (submissionError) {
      setError(adminErrorMessage(submissionError));
    }
  };

  const confirmRoutePublication = async () => {
    if (!routeReview || routePublishingRef.current) return;
    const liveVersionNumber = activeRouteVersion?.version ?? currentRoute?.current_version ?? null;
    if (
      routeVersionsState !== "ready"
      || routeReview.feature !== routeFeature
      || routeReview.currentVersionNumber !== liveVersionNumber
    ) {
      setRouteReview(null);
      setError("当前路由版本已发生变化，请重新检查发布影响后再确认。");
      return;
    }
    routePublishingRef.current = true;
    beginSubmission("route");
    try {
      const published = await adminApi.publishAiRoute(routeReview.feature, routeReview.payload);
      setRouteVersions((versions) => [published, ...versions.filter((item) => item.route_policy_version_id !== published.route_policy_version_id)]);
      setRouteDraft((draft) => ({ ...draft, reason: "" }));
      routeDraftDirtyRef.current = false;
      setRouteReview(null);
      await finishSubmission(`已发布「${routeReview.featureLabel}」路由版本 v${published.version}。`);
    } catch (submissionError) {
      failSubmission(submissionError);
    } finally {
      routePublishingRef.current = false;
    }
  };

  const updateRouteDraft = (updater: (draft: RouteDraft) => RouteDraft) => {
    routeDraftDirtyRef.current = true;
    setRouteReview(null);
    setRouteDraft(updater);
    setError("");
    setNotice("");
  };

  const selectRouteFeature = (nextFeature: RouteFeature) => {
    const nextLabel = routeFeatures.find((feature) => feature.value === nextFeature)?.label ?? "";
    const nextCapabilities = routeCapabilityRequirements(nextFeature);
    const nextModel = models.find((model) => (
      model.is_enabled
      && providers.some((provider) => (
        provider.slug === model.provider_slug
        && provider.is_enabled
        && provider.credential_configured
      ))
      && supportsRouteCapabilities(model, nextCapabilities)
    ));
    setRouteDraft({
      display_name: nextLabel,
      description: "",
      prompt_revision: "",
      reason: "",
      targets: [initialRouteTarget(nextModel?.slug ?? "")],
    });
    routeDraftDirtyRef.current = false;
    routeVersionRequestRef.current += 1;
    setRouteVersions([]);
    setRouteVersionsState("loading");
    setRouteVersionsError("");
    setRouteReview(null);
    setRouteFeature(nextFeature);
    setError("");
    setNotice("");
  };

  const updateRouteTarget = (index: number, update: Partial<RouteTargetDraft>) => {
    updateRouteDraft((draft) => ({
      ...draft,
      targets: draft.targets.map((target, targetIndex) => targetIndex === index ? { ...target, ...update } : target),
    }));
  };

  const toggleFallbackCategory = (index: number, category: AiRouteFallbackCategory, checked: boolean) => {
    const existing = routeDraft.targets[index]?.allow_fallback_on ?? [];
    updateRouteTarget(index, {
      allow_fallback_on: checked ? [...existing, category] : existing.filter((value) => value !== category),
    });
  };

  const addRouteTarget = () => {
    const used = new Set(routeDraft.targets.map((target) => target.model_slug));
    const nextModel = routeModels.find((model) => !used.has(model.slug))?.slug ?? "";
    updateRouteDraft((draft) => ({ ...draft, targets: [...draft.targets, initialRouteTarget(nextModel)] }));
  };

  const returnToRouteDraft = () => {
    setRouteReview(null);
    window.requestAnimationFrame(() => routeImpactButtonRef.current?.focus());
  };

  return (
    <div className="admin-ai-config-layout">
      <aside className="admin-panel admin-ai-config-guide" aria-label="配置发布说明">
        <span className="admin-ai-config-kicker"><Icon name="gear" size={16} />平台 AI 控制面</span>
        <h2>按顺序配置，再发布路由</h2>
        <ol>
          <li>创建 Provider，只填写服务端的凭据引用。</li>
          <li>为 Provider 注册可路由的模型。</li>
          <li>为每个 AI 功能发布一条可追溯的路由版本。</li>
        </ol>
        <p>不会在浏览器中录入、保存或回显 API Key。请先在服务器环境中配置对应的凭据引用。</p>
        <dl>
          <div><dt>Provider</dt><dd>{providers.length} 个</dd></div>
          <div><dt>可路由 Provider</dt><dd>{routableProviderCount} / {providers.length}</dd></div>
          <div><dt>模型</dt><dd>{models.length} 个</dd></div>
          <div><dt>已发布路由</dt><dd>{routes.length} 项</dd></div>
        </dl>
      </aside>

      <section className="admin-panel admin-ai-config-panel" aria-labelledby="admin-ai-config-title">
        <header className="admin-section-heading">
          <div>
            <h2 id="admin-ai-config-title">配置与发布</h2>
            <p>创建记录后不可直接覆盖；模型与路由变更都会留下可审计版本。</p>
          </div>
        </header>
        <div className="admin-segmented admin-ai-config-tabs" aria-label="AI 配置操作">
          {([['provider', '1 Provider'], ['model', '2 模型'], ['route', '3 发布路由']] as Array<[ConfigurationSection, string]>).map(([value, label]) => (
            <button aria-pressed={section === value} key={value} onClick={() => { setSection(value); setError(""); setNotice(""); }} type="button">{label}</button>
          ))}
        </div>

        {section === "provider" && <form className="admin-management-form admin-ai-config-form" onSubmit={submitProvider}>
          <div className="admin-ai-form-heading"><div><h3>创建 Provider</h3><p>目前支持 OpenAI 兼容接口。凭据由服务器托管，此处仅登记引用名。</p></div><AdminStatus status="enabled" label="可创建" /></div>
          <div className="admin-form-grid">
            <label><span>显示名称</span><input className="field" maxLength={120} onChange={(event) => setProviderDraft({ ...providerDraft, display_name: event.target.value })} placeholder="例如：DeepSeek 生产接口" required value={providerDraft.display_name} /></label>
            <label><span>配置标识</span><input autoCapitalize="none" className="field" maxLength={64} onChange={(event) => setProviderDraft({ ...providerDraft, slug: event.target.value })} pattern="[A-Za-z0-9][A-Za-z0-9._-]{1,63}" placeholder="例如：deepseek-prod" required value={providerDraft.slug} /></label>
            <label className="admin-ai-field-wide"><span>完整 Chat Completions HTTPS 端点</span><input autoCapitalize="none" className="field" maxLength={1000} onChange={(event) => setProviderDraft({ ...providerDraft, endpoint_url: event.target.value })} placeholder="https://api.example.com/v1/chat/completions" required type="url" value={providerDraft.endpoint_url} /><small>系统会直接向此 URL 发送请求；仅允许公网 HTTPS 地址，不支持本机、内网地址或 URL 中携带认证信息。</small></label>
            <label className="admin-ai-field-wide"><span>服务端凭据引用</span><input autoCapitalize="none" className="field" maxLength={120} onChange={(event) => setProviderDraft({ ...providerDraft, credential_ref: event.target.value })} placeholder="例如：deepseek-production" required value={providerDraft.credential_ref} /><small>填写部署环境已经配置好的引用名，不是 API Key，也不会在管理端显示密钥。</small></label>
          </div>
          <fieldset className="admin-toggle-group"><legend>Provider 状态</legend><label><input checked={providerDraft.is_enabled} onChange={(event) => setProviderDraft({ ...providerDraft, is_enabled: event.target.checked })} type="checkbox" /><span><strong>创建后立即启用</strong><small>启用后才可以被模型和路由使用。</small></span></label></fieldset>
          <label className="admin-reason-field"><span>变更原因</span><textarea className="textarea-field" maxLength={500} onChange={(event) => setProviderDraft({ ...providerDraft, reason: event.target.value })} placeholder="例如：新增备用模型服务商" required rows={3} value={providerDraft.reason} /></label>
          <p className="admin-form-warning">高级请求参数暂不在浏览器配置，避免把鉴权或运行参数误写入持久化配置。</p>
          {!!providers.length && <section className="admin-ai-provider-runtime" aria-label="Provider 运行时凭据状态">
            <div>
              <strong>已登记 Provider</strong>
              <p>此状态只校验当前 API 进程能否解析引用，不显示密钥，也不替代上游连通性测试。</p>
            </div>
            <ul className="admin-simple-list">
              {providers.map((provider) => (
                <li key={provider.provider_id}>
                  <span><strong>{provider.display_name}</strong><small>{provider.slug} · {provider.credential_ref}</small></span>
                  <AdminStatus
                    status={provider.credential_configured ? "verified" : "warning"}
                    label={provider.credential_configured ? "运行时凭据已配置" : "运行时凭据未配置"}
                  />
                </li>
              ))}
            </ul>
          </section>}
          {error && <p className="admin-form-error" role="alert">{error}</p>}{notice && <p className="admin-form-success" role="status">{notice}</p>}
          <div className="admin-form-actions"><button className="button button-primary" disabled={saving === "provider"} type="submit">{saving === "provider" ? <><i className="spinner" />正在创建</> : <><Icon name="plus" size={16} />创建 Provider</>}</button></div>
        </form>}

        {section === "model" && <form className="admin-management-form admin-ai-config-form" onSubmit={submitModel}>
          <div className="admin-ai-form-heading"><div><h3>配置模型</h3><p>模型归属于一个已启用的 Provider，并声明当前路由可以依赖的能力。</p></div><span className="admin-ai-step-count">{enabledProviders.length} 个已启用 Provider</span></div>
          {!enabledProviders.length && <p className="admin-form-warning">请先创建并启用至少一个 Provider，才能配置模型。</p>}
          <div className="admin-form-grid">
            <label><span>所属 Provider</span><select className="select-field" disabled={!enabledProviders.length} onChange={(event) => setModelDraft({ ...modelDraft, provider_slug: event.target.value })} required value={modelDraft.provider_slug}>{!enabledProviders.length && <option value="">暂无可用 Provider</option>}{enabledProviders.map((provider) => <option key={provider.provider_id} value={provider.slug}>{provider.display_name} · {provider.slug}</option>)}</select></label>
            <label><span>显示名称</span><input className="field" maxLength={120} onChange={(event) => setModelDraft({ ...modelDraft, display_name: event.target.value })} placeholder="例如：DeepSeek Chat" required value={modelDraft.display_name} /></label>
            <label><span>模型配置标识</span><input autoCapitalize="none" className="field" maxLength={64} onChange={(event) => setModelDraft({ ...modelDraft, slug: event.target.value })} pattern="[A-Za-z0-9][A-Za-z0-9._-]{1,63}" placeholder="例如：deepseek-chat" required value={modelDraft.slug} /></label>
            <label><span>Provider 模型 ID</span><input autoCapitalize="none" className="field" maxLength={255} onChange={(event) => setModelDraft({ ...modelDraft, provider_model_id: event.target.value })} placeholder="例如：deepseek-chat" required value={modelDraft.provider_model_id} /></label>
            <label><span>上下文窗口（可选）</span><input className="field" max="20000000" min="1" onChange={(event) => setModelDraft({ ...modelDraft, context_window_tokens: event.target.value })} placeholder="例如：128000" type="number" value={modelDraft.context_window_tokens} /></label>
            <label><span>最大输出（可选）</span><input className="field" max="2000000" min="1" onChange={(event) => setModelDraft({ ...modelDraft, max_output_tokens: event.target.value })} placeholder="例如：8192" type="number" value={modelDraft.max_output_tokens} /></label>
          </div>
          <fieldset className="admin-toggle-group"><legend>模型能力</legend>
            <label><input checked readOnly type="checkbox" /><span><strong>对话（chat）</strong><small>当前所有招聘功能都需要基础对话能力。</small></span></label>
            <label><input checked={modelDraft.tools} onChange={(event) => setModelDraft({ ...modelDraft, tools: event.target.checked, json_schema: event.target.checked ? modelDraft.json_schema : false })} type="checkbox" /><span><strong>工具调用（tools）</strong><small>支持结构化工具与函数调用。</small></span></label>
            <label><input checked={modelDraft.json_schema} onChange={(event) => setModelDraft({ ...modelDraft, json_schema: event.target.checked, tools: event.target.checked ? true : modelDraft.tools })} type="checkbox" /><span><strong>JSON Schema</strong><small>提取、评分和匹配等结构化结果建议启用。</small></span></label>
            <label><input checked={modelDraft.is_enabled} onChange={(event) => setModelDraft({ ...modelDraft, is_enabled: event.target.checked })} type="checkbox" /><span><strong>创建后立即启用</strong><small>禁用的模型无法被新路由发布使用。</small></span></label>
          </fieldset>
          <label className="admin-reason-field"><span>变更原因</span><textarea className="textarea-field" maxLength={500} onChange={(event) => setModelDraft({ ...modelDraft, reason: event.target.value })} placeholder="例如：新增主用结构化提取模型" required rows={3} value={modelDraft.reason} /></label>
          {error && <p className="admin-form-error" role="alert">{error}</p>}{notice && <p className="admin-form-success" role="status">{notice}</p>}
          <div className="admin-form-actions"><button className="button button-primary" disabled={saving === "model" || !enabledProviders.length} type="submit">{saving === "model" ? <><i className="spinner" />正在创建</> : <><Icon name="plus" size={16} />创建模型</>}</button></div>
        </form>}

        {section === "route" && <form aria-busy={routeVersionsState === "loading" || saving === "route"} className="admin-management-form admin-ai-config-form" onSubmit={prepareRouteReview}>
          <div className="admin-ai-form-heading"><div><h3>发布功能路由</h3><p>按顺序设置主模型与失败回退。每次发布都会生成不可变的新版本，不会静默改写旧版本。</p></div>{currentRoute && <AdminStatus status={currentRoute.is_enabled ? "enabled" : "disabled"} label={`当前 v${activeRouteVersion?.version ?? currentRoute.current_version ?? "—"}`} />}</div>
          {(routeVersionsState === "idle" || routeVersionsState === "loading") && <p className="admin-ai-route-load-state" role="status"><i aria-hidden="true" className="spinner" />正在加载当前路由版本，加载完成前不能发布。</p>}
          {routeVersionsState === "error" && <div className="admin-ai-route-load-error" role="alert"><span>当前路由版本加载失败：{routeVersionsError}</span><button className="button" onClick={() => void loadRouteVersions()} type="button">重新加载</button></div>}

          {!routeReview && <>
            {!routeModels.length && <p className="admin-form-warning">当前功能需要 {routeCapabilitiesLabel} 能力。请先创建并启用兼容模型，并让对应 Provider 的运行时凭据显示为“已配置”，才能发布路由。</p>}
            <div className="admin-form-grid">
              <label><span>AI 功能</span><select className="select-field" onChange={(event) => selectRouteFeature(event.target.value as RouteFeature)} value={routeFeature}>{routeFeatures.map((feature) => <option key={feature.value} value={feature.value}>{feature.label}</option>)}</select><small>{selectedRouteFeature.detail}</small></label>
              <label><span>路由显示名称</span><input className="field" maxLength={120} onChange={(event) => updateRouteDraft((draft) => ({ ...draft, display_name: event.target.value }))} required value={routeDraft.display_name} /></label>
              <label className="admin-ai-field-wide"><span>用途说明（可选）</span><textarea className="textarea-field" maxLength={1000} onChange={(event) => updateRouteDraft((draft) => ({ ...draft, description: event.target.value }))} placeholder="说明该路由的质量、速度或稳定性取舍" rows={3} value={routeDraft.description} /></label>
              <label><span>提示词版本（可选）</span><input className="field" maxLength={120} onChange={(event) => updateRouteDraft((draft) => ({ ...draft, prompt_revision: event.target.value }))} placeholder="例如：resume-extract-v3" value={routeDraft.prompt_revision} /></label>
            </div>
            <fieldset className="admin-ai-route-targets"><legend>路由目标</legend>
              <p>首个目标是主模型；仅在勾选的错误类型出现时，才按顺序尝试后续模型。</p>
              {routeDraft.targets.map((target, index) => <div className="admin-ai-route-target" key={`route-target-${index}`}>
                <div className="admin-ai-route-target-heading"><strong>{index === 0 ? "主模型" : `回退模型 ${index}`}</strong>{routeDraft.targets.length > 1 && <button className="button button-ghost" onClick={() => updateRouteDraft((draft) => ({ ...draft, targets: draft.targets.filter((_, targetIndex) => targetIndex !== index) }))} type="button">移除</button>}</div>
                <div className="admin-form-grid">
                  <label><span>模型</span><select className="select-field" disabled={!routeModels.length} onChange={(event) => updateRouteTarget(index, { model_slug: event.target.value })} required value={target.model_slug}>{!routeModels.length && <option value="">暂无兼容模型</option>}{target.model_slug && !routeModels.some((model) => model.slug === target.model_slug) && <option value={target.model_slug}>{target.model_slug} · 当前不可用</option>}{routeModels.map((model) => <option key={model.model_id} value={model.slug}>{model.display_name} · {model.slug}</option>)}</select><small>仅显示具备 {routeCapabilitiesLabel} 能力、已启用且运行时凭据已配置的模型。</small></label>
                  <label><span>最大尝试次数</span><select className="select-field" onChange={(event) => updateRouteTarget(index, { max_attempts: Number(event.target.value) })} value={target.max_attempts}><option value={1}>1 次</option><option value={2}>2 次</option><option value={3}>3 次</option></select></label>
                </div>
                {index < routeDraft.targets.length - 1 && <fieldset className="admin-ai-fallback-options"><legend>允许回退的原因</legend>{fallbackCategories.map((category) => <label key={category.value}><input checked={target.allow_fallback_on.includes(category.value)} onChange={(event) => toggleFallbackCategory(index, category.value, event.target.checked)} type="checkbox" /><span>{category.label}</span></label>)}</fieldset>}
              </div>)}
              <div className="admin-ai-route-target-actions"><button className="button" disabled={routeDraft.targets.length >= 4 || routeModels.length <= routeDraft.targets.length} onClick={addRouteTarget} type="button"><Icon name="plus" size={16} />添加回退模型</button><small>最多 4 个目标；同一模型不可重复。</small></div>
            </fieldset>
            <label className="admin-reason-field"><span>发布原因</span><textarea className="textarea-field" maxLength={500} onChange={(event) => updateRouteDraft((draft) => ({ ...draft, reason: event.target.value }))} placeholder="例如：主模型升级，保留原模型应对限流与超时" required rows={3} value={routeDraft.reason} /></label>
          </>}

          {routeReview && <section aria-labelledby="admin-ai-route-review-title" className="admin-ai-route-review">
            <header>
              <div><span>发布前检查</span><h4 id="admin-ai-route-review-title" ref={routeReviewHeadingRef} tabIndex={-1}>确认「{routeReview.featureLabel}」新版本</h4></div>
              <p>下列内容是本次发布的固定快照。确认后将生成新版本，并将其设为当前发布版本。</p>
            </header>
            <div className="admin-ai-route-review-table-wrap">
              <table className="admin-ai-route-review-table">
                <thead><tr><th scope="col">检查项</th><th scope="col">当前版本</th><th scope="col">新版本</th></tr></thead>
                <tbody>
                  <tr><th scope="row">版本</th><td>{routeReview.currentVersionNumber === null ? "未发布" : `v${routeReview.currentVersionNumber}`}</td><td><strong>v{routeReview.nextVersion}</strong></td></tr>
                  <tr><th scope="row">显示名称</th><td>{routeReview.currentPolicy?.display_name ?? "未设置"}</td><td>{routeReview.payload.display_name}</td></tr>
                  <tr><th scope="row">用途说明</th><td>{routeReview.currentPolicy?.description || "未设置"}</td><td>{routeReview.payload.description || "未设置"}</td></tr>
                  <tr><th scope="row">模型链</th><td>{routeReview.currentVersion ? routeModelChain(routeReview.currentVersion.targets) : "未配置"}</td><td><strong>{routeModelChain(routeReview.payload.targets)}</strong></td></tr>
                  <tr><th scope="row">最大尝试</th><td>{routeReview.currentVersion ? routeRetrySummary(routeReview.currentVersion.targets) : "未配置"}</td><td>{routeRetrySummary(routeReview.payload.targets)}</td></tr>
                  <tr><th scope="row">回退条件</th><td>{routeReview.currentVersion ? routeFallbackSummary(routeReview.currentVersion.targets) : "未配置"}</td><td>{routeFallbackSummary(routeReview.payload.targets)}</td></tr>
                  <tr><th scope="row">提示词版本</th><td>{routeReview.currentVersion?.prompt_revision || "未设置"}</td><td>{routeReview.payload.prompt_revision || "未设置"}</td></tr>
                </tbody>
              </table>
            </div>
            <dl className="admin-ai-route-review-reason"><div><dt>发布原因</dt><dd>{routeReview.payload.reason}</dd></div></dl>
            <section aria-labelledby="admin-ai-route-change-title" className="admin-ai-route-change-warning">
              <h5 id="admin-ai-route-change-title">变化警示</h5>
              <ul>{routeReview.changes.map((change) => <li key={change}>{change}</li>)}</ul>
              <p>已发布的历史版本不会被改写。请确认模型顺序、重试和回退条件与变更原因一致。</p>
            </section>
          </section>}

          {error && <p className="admin-form-error" role="alert">{error}</p>}{notice && <p className="admin-form-success" role="status">{notice}</p>}
          {!routeReview && <div className="admin-form-actions"><button className="button button-primary" disabled={saving === "route" || !routeModels.length || routeVersionsState !== "ready"} ref={routeImpactButtonRef} type="submit"><Icon name="spark" size={16} />检查发布影响</button></div>}
          {routeReview && <div className="admin-form-actions"><button className="button" disabled={saving === "route"} onClick={returnToRouteDraft} type="button">返回修改</button><button className="button button-primary" disabled={saving === "route" || routeVersionsState !== "ready"} onClick={() => void confirmRoutePublication()} type="button">{saving === "route" ? <><i className="spinner" />正在发布</> : <><Icon name="spark" size={16} />确认发布新版本</>}</button></div>}

          <section className="admin-ai-route-history" aria-live="polite">
            <div><h4>「{selectedRouteFeature.label}」发布历史</h4><p>{routeVersionsState === "idle" || routeVersionsState === "loading" ? "正在读取版本历史…" : routeVersionsState === "error" ? "版本历史暂不可用，请重新加载。" : "仅展示版本和路由目标，不展示任何候选人内容。"}</p></div>
            {routeVersionsState === "ready" && !routeVersions.length && <p className="admin-field-help">尚未发布过这个功能的路由。</p>}
            {routeVersionsState === "ready" && !!routeVersions.length && <ol>{routeVersions.map((version) => <li key={version.route_policy_version_id}><span><strong>v{version.version}</strong><small>{version.targets.map((target) => target.model_slug).join(" → ")}</small></span><time dateTime={version.published_at}>{formatDate(version.published_at, true)}</time></li>)}</ol>}
          </section>
        </form>}
      </section>
    </div>
  );
}
