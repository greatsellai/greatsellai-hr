import type { AuthSession } from "../types";
import type {
  AiModelPriceVersion,
  AiModelProfile,
  AiProviderProfile,
  AiRoutePolicy,
  AiRunUsage,
  AiUsageAggregate,
  AiUsageQuery,
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
  listAiProviders: () => request<AiProviderProfile[]>("/platform/ai/providers"),
  listAiModels: () => request<AiModelProfile[]>("/platform/ai/models"),
  listAiRoutes: () => request<AiRoutePolicy[]>("/platform/ai/routes"),
  listAiPrices: () => request<AiModelPriceVersion[]>("/platform/ai/model-prices"),
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
    };
    const message = (error.code && messages[error.code]) || error.message;
    return error.requestId ? `${message} 请求编号：${error.requestId}` : message;
  }
  return error instanceof Error ? error.message : "请求未完成，请稍后重试。";
}
