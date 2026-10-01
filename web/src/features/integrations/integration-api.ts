/**
 * Same-origin client for the external-connection settings screen.
 *
 * It deliberately never stores or sends bearer credentials. The only secret
 * returned by this API is kept by the settings panel in React memory and is
 * discarded when that panel unmounts.
 */
export type IntegrationAudience = "rest" | "mcp";
export type IntegrationGrantStatus = "active" | "expired" | "revoked" | "blocked";
export type IntegrationScope =
  | "analyses:read"
  | "analyses:write"
  | "assessments:read"
  | "candidates:read"
  | "evidence:read"
  | "jobs:read";

export interface IntegrationGrantSummary {
  id: string;
  name: string;
  audience: IntegrationAudience;
  scopes: string[];
  status: IntegrationGrantStatus;
  token_prefix: string;
  created_at: string;
  expires_at: string;
  last_used_at: string | null;
  revoked_at: string | null;
  kind?: "pat" | "oauth";
}

export interface IntegrationSettings {
  user: { id: string; display_name: string | null; email: string | null };
  workspace: {
    organization_id: string;
    name: string;
    enabled: boolean;
    allowed_scopes: string[];
    available_scopes?: string[];
  };
  features: { api: boolean; mcp: boolean; analyses: boolean; oauth: boolean };
  endpoints: { api_base_url: string; mcp_url: string };
  logout_revokes_connections?: boolean;
  permissions: { can_create: boolean; can_admin: boolean; can_revoke_own: boolean };
  csrf_token: string;
  grants: IntegrationGrantSummary[];
}

export interface IntegrationGrantCreated {
  grant: IntegrationGrantSummary;
  /** Deliberately transient. Callers must not persist this token. */
  token: string;
}

export interface IntegrationGrantCreateInput {
  name: string;
  audience: IntegrationAudience;
  scopes: string[];
  expires_in_days: number;
}

export interface IntegrationActivity {
  id: string;
  grant_id: string | null;
  action: string;
  resource_type: string;
  resource_count: number;
  candidate_count: number;
  result: string;
  reason_code: string | null;
  created_at: string;
}

export interface IntegrationActivityList {
  items: IntegrationActivity[];
  next_cursor: string | null;
}

export interface IntegrationAnalysisCandidateLink {
  candidate_id: string;
  candidate_code: string;
  resume_id: string;
  fact_snapshot_id: string;
  facts_version: number;
}

export interface IntegrationAnalysisDraftSummary {
  id: string;
  kind: "external_ai_draft";
  title: string;
  version: number;
  source_status: "current" | "source_changed";
  candidates: IntegrationAnalysisCandidateLink[];
  job: { job_id: string; job_version_id: string } | null;
  created_at: string;
  updated_at: string;
  expires_at: string;
}

export interface IntegrationAnalysisObservation {
  candidate_id: string;
  text: string;
}

export interface IntegrationAnalysisReferencedFacts {
  candidate_id: string;
  candidate_code: string;
  resume_id: string;
  fact_snapshot_id: string;
  facts_version: number;
  facts: {
    highest_degree: string | null;
    employment_months: number | null;
    employment_or_internship_months: number | null;
    education: Array<{ school: string | null; degree: string | null; major: string | null }>;
    experiences: Array<{ organization: string | null; title: string | null; experience_name: string | null }>;
    skills: Array<{ skill: string }>;
  };
}

export interface IntegrationAnalysisDraftDetail extends IntegrationAnalysisDraftSummary {
  referenced_facts: IntegrationAnalysisReferencedFacts[];
  inferences: IntegrationAnalysisObservation[];
  questions_to_verify: IntegrationAnalysisObservation[];
  decision_authority: "recruiting_team";
}

export interface IntegrationAnalysisDraftList {
  items: IntegrationAnalysisDraftSummary[];
  next_cursor: string | null;
}

export interface IntegrationAnalysisPendingSummary {
  id: string;
  title: string;
  version: number;
  candidates: IntegrationAnalysisCandidateLink[];
  job: { job_id: string; job_version_id: string } | null;
  job_title: string | null;
  source_connection_name: string;
  created_at: string;
  expires_at: string;
}

export interface IntegrationAnalysisPendingDetail extends IntegrationAnalysisPendingSummary {
  payload_sha256: string;
  source_status: "current" | "source_changed";
  referenced_facts: IntegrationAnalysisReferencedFacts[];
  inferences: IntegrationAnalysisObservation[];
  questions_to_verify: IntegrationAnalysisObservation[];
}

export interface IntegrationAnalysisPendingList {
  items: IntegrationAnalysisPendingSummary[];
}

/** Same-origin browser-consent payload. It deliberately contains no token. */
export interface IntegrationOAuthConsent {
  request_id: string;
  client: { name: string; redirect_origin: string };
  workspace: { organization_id: string; name: string };
  audience: IntegrationAudience;
  scopes: IntegrationScope[];
  available_scopes: IntegrationScope[];
  default_scopes: IntegrationScope[];
  expires_at: string;
  csrf_token: string;
}

export interface IntegrationOAuthConsentResult {
  /** Server-validated OAuth redirect. It is not an API or bearer-token URL. */
  redirect_url: string;
}

export class IntegrationApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string | null,
    message: string,
    readonly payload: unknown,
    readonly requestId: string | null,
  ) {
    super(message);
    this.name = "IntegrationApiError";
  }
}

export function isIntegrationApiError(error: unknown): error is IntegrationApiError {
  return error instanceof IntegrationApiError;
}

function defaultApiBaseUrl(): string {
  if (typeof window === "undefined") return "/v1";
  const compatibilityBase = "/greatsellhr";
  return window.location.pathname === compatibilityBase || window.location.pathname.startsWith(`${compatibilityBase}/`)
    ? `${compatibilityBase}/v1`
    : "/v1";
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

async function responsePayload(response: Response): Promise<unknown> {
  if ((response.headers.get("content-type") || "").includes("application/json")) {
    try { return await response.json(); } catch { return null; }
  }
  try { return await response.text(); } catch { return null; }
}

function errorDetail(payload: unknown): { code: string | null; message: string } {
  const object = asRecord(payload);
  const detail = object?.detail;
  if (typeof detail === "string") return { code: detail, message: detail };
  if (typeof object?.code === "string") {
    return { code: object.code, message: typeof object.message === "string" ? object.message : object.code };
  }
  if (typeof object?.message === "string") return { code: null, message: object.message };
  if (typeof payload === "string" && payload.trim()) return { code: null, message: payload };
  return { code: null, message: "integration_request_failed" };
}

function endpoint(path: string): string {
  return `${defaultApiBaseUrl()}/integration-settings${path}`;
}

async function request<T>(
  path: string,
  init: Omit<RequestInit, "body"> & { body?: unknown; csrfToken?: string } = {},
): Promise<T> {
  const { body, csrfToken, headers: suppliedHeaders, ...rest } = init;
  const headers = new Headers(suppliedHeaders);
  headers.set("Accept", "application/json");
  if (body !== undefined) headers.set("Content-Type", "application/json");
  if (csrfToken) headers.set("X-CSRF-Token", csrfToken);

  const response = await fetch(endpoint(path), {
    ...rest,
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: "no-store",
    credentials: "same-origin",
    headers,
  });
  if (!response.ok) {
    const payload = await responsePayload(response);
    const detail = errorDetail(payload);
    throw new IntegrationApiError(
      response.status,
      detail.code,
      detail.message,
      payload,
      response.headers.get("x-request-id"),
    );
  }
  if (response.status === 204) return undefined as T;
  return (await responsePayload(response)) as T;
}

function normalizeGrantList(payload: IntegrationGrantSummary[] | { items: IntegrationGrantSummary[] }): IntegrationGrantSummary[] {
  return Array.isArray(payload) ? payload : payload.items;
}

/** Same-origin client; it never sends bearer credentials or persists tokens. */
export const integrationApi = {
  getSettings: (signal?: AbortSignal): Promise<IntegrationSettings> => request<IntegrationSettings>("", { signal }),
  createGrant: (input: IntegrationGrantCreateInput, csrfToken: string): Promise<IntegrationGrantCreated> =>
    request<IntegrationGrantCreated>("/grants", { method: "POST", body: input, csrfToken }),
  rotateGrant: (id: string, expiresInDays: number, csrfToken: string): Promise<IntegrationGrantCreated> =>
    request<IntegrationGrantCreated>(`/grants/${encodeURIComponent(id)}/rotate`, {
      method: "POST", body: { expires_in_days: expiresInDays }, csrfToken,
    }),
  revokeOwnGrant: (id: string, csrfToken: string): Promise<void> =>
    request<void>(`/grants/${encodeURIComponent(id)}`, { method: "DELETE", csrfToken }),
  updateWorkspacePolicy: (
    input: Pick<IntegrationSettings["workspace"], "enabled" | "allowed_scopes">,
    csrfToken: string,
  ): Promise<IntegrationSettings["workspace"]> =>
    request<IntegrationSettings["workspace"]>("/workspace", { method: "PATCH", body: input, csrfToken }),
  listWorkspaceGrants: async (signal?: AbortSignal): Promise<IntegrationGrantSummary[]> =>
    normalizeGrantList(await request<IntegrationGrantSummary[] | { items: IntegrationGrantSummary[] }>("/workspace/grants", { signal })),
  revokeWorkspaceGrant: (id: string, csrfToken: string): Promise<void> =>
    request<void>(`/workspace/grants/${encodeURIComponent(id)}`, { method: "DELETE", csrfToken }),
  listActivity: (limit = 50, cursor?: string | null, signal?: AbortSignal): Promise<IntegrationActivityList> => {
    const query = new URLSearchParams({ limit: String(Math.min(100, Math.max(1, limit))) });
    if (cursor) query.set("cursor", cursor);
    return request<IntegrationActivityList>(`/activity?${query.toString()}`, { signal });
  },
  listAnalysisReports: (limit = 20, cursor?: string | null, signal?: AbortSignal): Promise<IntegrationAnalysisDraftList> => {
    const query = new URLSearchParams({ limit: String(Math.min(100, Math.max(1, limit))) });
    if (cursor) query.set("cursor", cursor);
    return request<IntegrationAnalysisDraftList>(`/analysis-reports?${query.toString()}`, { signal });
  },
  getAnalysisReport: (id: string, signal?: AbortSignal): Promise<IntegrationAnalysisDraftDetail> =>
    request<IntegrationAnalysisDraftDetail>(`/analysis-reports/${encodeURIComponent(id)}`, { signal }),
  listPendingAnalysisReports: (signal?: AbortSignal): Promise<IntegrationAnalysisPendingList> =>
    request<IntegrationAnalysisPendingList>("/analysis-reports/pending?limit=10", { signal }),
  getPendingAnalysisReport: (id: string, signal?: AbortSignal): Promise<IntegrationAnalysisPendingDetail> =>
    request<IntegrationAnalysisPendingDetail>(`/analysis-reports/pending/${encodeURIComponent(id)}`, { signal }),
  confirmPendingAnalysisReport: (
    id: string,
    input: { version: number; payload_sha256: string },
    csrfToken: string,
  ): Promise<{ id: string; version: number; confirmed_at: string; status: "saved" }> =>
    request(`/analysis-reports/${encodeURIComponent(id)}/confirm`, {
      method: "POST", body: input, csrfToken,
    }),
  discardPendingAnalysisReport: (id: string, version: number, csrfToken: string): Promise<void> =>
    request<void>(`/analysis-reports/${encodeURIComponent(id)}/discard`, {
      method: "POST", body: { version }, csrfToken,
    }),
  getOAuthConsent: (requestId: string, signal?: AbortSignal): Promise<IntegrationOAuthConsent> =>
    request<IntegrationOAuthConsent>(`/oauth/consents/${encodeURIComponent(requestId)}`, { signal }),
  submitOAuthConsent: (
    requestId: string,
    decision: { approve: boolean; approved_scopes?: IntegrationScope[] | null },
    csrfToken: string,
  ): Promise<IntegrationOAuthConsentResult> =>
    request<IntegrationOAuthConsentResult>(`/oauth/consents/${encodeURIComponent(requestId)}`, {
      method: "POST", body: decision, csrfToken,
    }),
};

export function integrationErrorMessage(error: unknown): string {
  if (!isIntegrationApiError(error)) return "连接服务暂时不可用，请稍后重试。";
  const withRequestId = (message: string) => error.requestId ? `${message}（请求编号：${error.requestId}）` : message;
  switch (error.status) {
    case 401: return withRequestId("登录状态已失效，请重新登录后继续。");
    case 402: return withRequestId("当前套餐或工作区状态不支持此连接操作。");
    case 403: return withRequestId("你没有执行此连接操作的权限。");
    case 404: return withRequestId("当前工作区尚未启用该连接服务。");
    case 409: return withRequestId("该操作与当前连接状态冲突，请刷新后重试。");
    case 429: return withRequestId("操作过于频繁，请稍后重试。");
    default: return withRequestId("操作未完成，请稍后重试。");
  }
}
