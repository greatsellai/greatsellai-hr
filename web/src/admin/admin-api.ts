import type { AuthSession } from "../types";
import type {
  AiModelProfileCreateInput,
  AiModelProfile,
  AiProviderProfileCreateInput,
  AiProviderProfile,
  AiRoutePolicyPublishInput,
  AiRoutePolicy,
  AiRoutePolicyVersion,
  AiRunUsage,
  AiUsageAggregate,
  AiUsageQuery,
  AiUsageTrendBucket,
  AiUsageTrendQuery,
  AuditQuery,
  OrganizationQuery,
  PlatformAuditPage,
  PlatformDashboard,
  PlatformOrganizationDetail,
  PlatformOrganizationPage,
  PlatformOrganizationUpdate,
  PlatformUserDetail,
  PlatformUserPage,
  ProductPlan,
  ProductPlanUpdate,
  UserQuery,
} from "./admin-types";

export class AdminApiError extends Error {
  readonly status: number;
  readonly code: string | null;
  readonly requestId: string | null;

  constructor(status: number, message: string, code: string | null, requestId: string | null) {
    super(message);
    this.name = "AdminApiError";
    this.status = status;
    this.code = code;
    this.requestId = requestId;
  }
}

function apiBaseUrl() {
  const compatibilityBase = "/greatsellhr";
  const { pathname } = window.location;
  return pathname === compatibilityBase || pathname.startsWith(`${compatibilityBase}/`)
    ? `${compatibilityBase}/v1`
    : "/v1";
}

function queryString(values: object) {
  const params = new URLSearchParams();
  Object.entries(values as Record<string, string | number | boolean | undefined>).forEach(([key, value]) => {
    if (value === undefined || value === "") return;
    params.set(key, String(value));
  });
  const query = params.toString();
  return query ? `?${query}` : "";
}

async function responsePayload(response: Response): Promise<unknown> {
  const contentType = response.headers.get("content-type") ?? "";
  if (contentType.includes("application/json")) {
    try {
      return await response.json();
    } catch {
      return null;
    }
  }
  try {
    return await response.text();
  } catch {
    return null;
  }
}

function extractError(payload: unknown) {
  if (typeof payload === "string" && payload.trim()) {
    return { message: payload.trim(), code: null };
  }
  if (payload && typeof payload === "object") {
    const candidate = payload as Record<string, unknown>;
    const detail = candidate.detail;
    if (typeof detail === "string") return { message: detail, code: detail };
    if (Array.isArray(detail)) {
      const first = detail[0];
      if (first && typeof first === "object") {
        const issue = first as Record<string, unknown>;
        const rawMessage = typeof issue.msg === "string" ? issue.msg : "请求参数不符合要求。";
        const valueErrorPrefix = "Value error, ";
        const code = rawMessage.startsWith(valueErrorPrefix) ? rawMessage.slice(valueErrorPrefix.length) : null;
        return { message: code || rawMessage, code };
      }
    }
    if (detail && typeof detail === "object") {
      const detailObject = detail as Record<string, unknown>;
      const message = typeof detailObject.message === "string" ? detailObject.message : "请求未完成";
      const code = typeof detailObject.code === "string" ? detailObject.code : null;
      return { message, code };
    }
  }
  return { message: "请求未完成，请稍后重试。", code: null };
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  if (init.body !== undefined) headers.set("Content-Type", "application/json");
  const response = await fetch(`${apiBaseUrl()}${path}`, {
    ...init,
    headers,
    credentials: "same-origin",
  });
  if (!response.ok) {
    const payload = await responsePayload(response);
    const error = extractError(payload);
    throw new AdminApiError(
      response.status,
      error.message,
      error.code,
      response.headers.get("x-request-id"),
    );
  }
  if (response.status === 204) return undefined as T;
  return (await responsePayload(response)) as T;
}

function body(value: unknown) {
  return JSON.stringify(value);
}

export const adminApi = {
  getSession: () => request<AuthSession>("/auth/session"),
  logout: () => request<void>("/auth/logout", { method: "POST" }),
  getDashboard: () => request<PlatformDashboard>("/platform/dashboard"),
  listOrganizations: (query: OrganizationQuery = {}) =>
    request<PlatformOrganizationPage>(`/platform/organizations${queryString(query)}`),
  getOrganization: (organizationId: string) =>
    request<PlatformOrganizationDetail>(`/platform/organizations/${encodeURIComponent(organizationId)}`),
  updateOrganization: (organizationId: string, update: PlatformOrganizationUpdate) =>
    request<PlatformOrganizationDetail>(`/platform/organizations/${encodeURIComponent(organizationId)}`, {
      method: "PATCH",
      body: body(update),
    }),
  listUsers: (query: UserQuery = {}) =>
    request<PlatformUserPage>(`/platform/users${queryString(query)}`),
  getUser: (userId: string) =>
    request<PlatformUserDetail>(`/platform/users/${encodeURIComponent(userId)}`),
  updateUser: (userId: string, isActive: boolean, reason: string) =>
    request<PlatformUserDetail>(`/platform/users/${encodeURIComponent(userId)}`, {
      method: "PATCH",
      body: body({ is_active: isActive, reason }),
    }),
  listAuditEvents: (query: AuditQuery = {}) =>
    request<PlatformAuditPage>(`/platform/audit-events${queryString(query)}`),
  listPlans: () => request<ProductPlan[]>("/platform/plans"),
  updatePlan: (planCode: string, update: ProductPlanUpdate) =>
    request<ProductPlan>(`/platform/plans/${encodeURIComponent(planCode)}`, {
      method: "PUT",
      body: body(update),
    }),
  listAiRuns: (query: AiUsageQuery = {}) =>
    request<AiRunUsage[]>(`/platform/ai/usage/runs${queryString(query)}`),
  listAiUsage: (query: AiUsageQuery = {}) =>
    request<AiUsageAggregate[]>(`/platform/ai/usage/summary${queryString(query)}`),
  listAiUsageTrend: (query: AiUsageTrendQuery = {}) =>
    request<AiUsageTrendBucket[]>(`/platform/ai/usage/trend${queryString(query)}`),
  listAiProviders: () => request<AiProviderProfile[]>("/platform/ai/providers"),
  createAiProvider: (payload: AiProviderProfileCreateInput) =>
    request<AiProviderProfile>("/platform/ai/providers", { method: "POST", body: body(payload) }),
  listAiModels: () => request<AiModelProfile[]>("/platform/ai/models"),
  createAiModel: (payload: AiModelProfileCreateInput) =>
    request<AiModelProfile>("/platform/ai/models", { method: "POST", body: body(payload) }),
  listAiRoutes: () => request<AiRoutePolicy[]>("/platform/ai/routes"),
  listAiRouteVersions: (feature: string) =>
    request<AiRoutePolicyVersion[]>(`/platform/ai/routes/${encodeURIComponent(feature)}/versions`),
  publishAiRoute: (feature: string, payload: AiRoutePolicyPublishInput) =>
    request<AiRoutePolicyVersion>(`/platform/ai/routes/${encodeURIComponent(feature)}`, {
      method: "PUT",
      body: body(payload),
    }),
};

export function adminErrorMessage(error: unknown) {
  if (error instanceof AdminApiError) {
    const messages: Record<string, string> = {
      platform_admin_required: "当前账号没有平台管理权限。",
      platform_organization_not_found: "没有找到这个工作区，它可能已被移除。",
      platform_user_not_found: "没有找到这个用户。",
      platform_plan_not_found: "没有找到这个套餐。",
      product_plan_not_found: "没有找到这个套餐。",
      default_trial_plan_required: "必须至少保留一个已启用、可注册的默认试用套餐。",
      database_conflict: "数据刚刚发生变化，请刷新后重试。",
      platform_organization_confirmation_required: "请输入当前工作区名称确认这项高风险变更。",
      platform_admin_self_deactivation_forbidden: "不能停用当前登录的平台管理员账号。",
      platform_admin_deactivation_forbidden: "平台管理员账号受到保护，请先移除平台权限。",
      ai_provider_not_found: "没有找到这个模型服务，请刷新后重试。",
      ai_provider_slug_exists: "这个模型服务标识已经存在，请换一个标识。",
      ai_model_slug_exists: "这个模型标识已经存在，请换一个标识。",
      ai_model_not_found: "没有找到这个模型，请刷新资源后重试。",
      ai_route_policy_not_found: "没有找到这个路由策略。",
      ai_route_model_not_found: "路由中有模型不存在，请刷新后重试。",
      ai_route_model_unavailable: "路由中的模型未启用或能力不满足当前功能。",
      ai_route_provider_unavailable: "路由中的模型服务未启用或尚未就绪。",
      ai_route_credential_not_configured: "路由中的模型服务尚未就绪；请完成服务端配置并刷新状态后再发布。",
      unsupported_ai_feature: "这个 AI 功能暂不支持发布路由。",
      ai_gateway_configuration_conflict: "配置与当前数据冲突，请刷新后重新发布。",
      invalid_ai_config_slug: "配置标识仅支持小写字母、数字、点、下划线和连字符，且长度至少为 2 位。",
      ai_endpoint_url_must_be_https: "模型服务地址必须使用 HTTPS。",
      ai_endpoint_url_must_not_include_userinfo: "模型服务地址不能包含账号或密码。",
      ai_endpoint_url_host_not_allowed: "模型服务地址不能指向本机或内网地址。",
      ai_endpoint_url_must_not_include_fragment: "模型服务地址不能包含 URL 片段。",
      invalid_currency: "币种必须是三位大写字母，例如 CNY。",
      price_effective_to_must_follow_effective_from: "价格结束时间必须晚于生效时间。",
      duplicate_route_model_target: "同一个模型不能在同一路由中重复出现。",
      duplicate_ai_fallback_category: "同一种回退原因不能重复选择。",
      value_must_not_be_blank: "请填写必填信息，不能只输入空格。",
      platform_audit_reason_required: "请填写本次变更原因，便于平台审计。",
    };
    const message = (error.code && messages[error.code]) || error.message;
    return error.requestId ? `${message} 请求编号：${error.requestId}` : message;
  }
  return error instanceof Error ? error.message : "请求未完成，请稍后重试。";
}
