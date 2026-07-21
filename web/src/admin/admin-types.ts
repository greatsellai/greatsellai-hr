import type { AuthSession, PlanStatus } from "../types";

export type PlatformPlanStatus = PlanStatus | "legacy";

export type AdminView =
  | "overview"
  | "organizations"
  | "users"
  | "plans"
  | "ai"
  | "audit";

export type RequestState = "idle" | "loading" | "ready" | "error";

export interface PlatformSession extends AuthSession {
  is_platform_admin: boolean;
}

export interface PlatformDashboard {
  generated_at: string;
  organizations_total: number;
  organizations_by_status: Partial<Record<PlatformPlanStatus, number>>;
  trials_expiring_within_7_days: number;
  users_total: number;
  users_active: number;
  users_verified: number;
  resumes_total: number;
  jobs_total: number;
  mailboxes_total: number;
  ai_runs_total: number;
  ai_runs_succeeded: number;
  ai_runs_failed: number;
  ai_cost_cny_micros: number;
  ai_cost_unavailable_runs: number;
}

export interface PlatformOrganizationSummary {
  organization_id: string;
  name: string;
  plan_id: string | null;
  plan_code: string | null;
  plan_name: string | null;
  plan_status: PlatformPlanStatus;
  trial_started_at: string | null;
  trial_ends_at: string | null;
  member_count: number;
  active_member_count: number;
  created_at: string;
  updated_at: string;
}

export interface PlatformOrganizationMember {
  membership_id: string;
  user_id: string;
  full_name: string;
  email: string;
  role: "admin" | "recruiter" | string;
  is_active: boolean;
  user_is_active: boolean;
  email_verified: boolean;
  last_login_at: string | null;
  joined_at: string;
}

export interface PlatformOrganizationDetail extends PlatformOrganizationSummary {
  resume_count: number;
  job_count: number;
  mailbox_count: number;
  ai_run_count: number;
  members: PlatformOrganizationMember[];
}

export interface PlatformOrganizationPage {
  items: PlatformOrganizationSummary[];
  total: number;
  limit: number;
  offset: number;
}

export interface OrganizationQuery {
  search?: string;
  plan_code?: string;
  plan_status?: PlatformPlanStatus | "";
  limit?: number;
  offset?: number;
}

export interface PlatformOrganizationUpdate {
  name?: string;
  plan_code?: string;
  plan_status?: Exclude<PlatformPlanStatus, "legacy">;
  trial_ends_at?: string | null;
  confirmation_name?: string;
  reason: string;
}

export interface PlatformUserSummary {
  user_id: string;
  full_name: string;
  email: string;
  is_active: boolean;
  is_platform_admin: boolean;
  email_verified: boolean;
  last_login_at: string | null;
  created_at: string;
  membership_count: number;
}

export interface PlatformUserMembership {
  membership_id: string;
  organization_id: string;
  organization_name: string;
  role: "admin" | "recruiter" | string;
  is_active: boolean;
  joined_at: string;
}

export interface PlatformUserDetail extends PlatformUserSummary {
  memberships: PlatformUserMembership[];
}

export interface PlatformUserPage {
  items: PlatformUserSummary[];
  total: number;
  limit: number;
  offset: number;
}

export interface UserQuery {
  search?: string;
  is_active?: boolean | "";
  limit?: number;
  offset?: number;
}

export interface PlatformAuditEvent {
  audit_id: string;
  actor_user_id: string | null;
  action: string;
  target_type: string;
  target_id: string | null;
  organization_id: string | null;
  reason: string | null;
  before_state: Record<string, unknown> | null;
  after_state: Record<string, unknown> | null;
  request_id: string | null;
  created_at: string;
}

export interface PlatformAuditPage {
  items: PlatformAuditEvent[];
  total: number;
  limit: number;
  offset: number;
}

export interface AuditQuery {
  actor_user_id?: string;
  action?: string;
  target_type?: string;
  organization_id?: string;
  created_at_from?: string;
  created_at_to?: string;
  limit?: number;
  offset?: number;
}

export interface ProductPlan {
  plan_id: string;
  code: string;
  name: string;
  monthly_price_cents: number;
  trial_days: number;
  feature_flags: Record<string, boolean>;
  is_active: boolean;
  is_available_for_signup: boolean;
  is_default_trial: boolean;
  sort_order: number;
}

export interface ProductPlanUpdate {
  name?: string;
  monthly_price_cents?: number;
  trial_days?: number;
  feature_flags?: Record<string, boolean>;
  is_active?: boolean;
  is_available_for_signup?: boolean;
  is_default_trial?: boolean;
  sort_order?: number;
  reason?: string;
}

export interface AiProviderProfile {
  provider_id: string;
  slug: string;
  display_name: string;
  driver: string;
  endpoint_url: string;
  credential_ref: string;
  request_defaults: Record<string, unknown>;
  is_enabled: boolean;
  created_at: string;
  updated_at: string;
}

/** Platform-only, non-secret provider connection settings. */
export interface AiProviderProfileCreateInput {
  slug: string;
  display_name: string;
  driver: "openai_compatible";
  endpoint_url: string;
  /** A server-side credential reference, never an API key. */
  credential_ref: string;
  request_defaults: Record<string, unknown>;
  is_enabled: boolean;
  reason?: string;
}

export interface AiModelProfile {
  model_id: string;
  slug: string;
  provider_id: string;
  provider_slug: string;
  display_name: string;
  provider_model_id: string;
  capabilities: string[];
  context_window_tokens: number | null;
  max_output_tokens: number | null;
  is_enabled: boolean;
  created_at: string;
  updated_at: string;
}

export type AiModelCapability = "chat" | "tools" | "json_schema";

export interface AiModelProfileCreateInput {
  slug: string;
  provider_slug: string;
  display_name: string;
  provider_model_id: string;
  capabilities: AiModelCapability[];
  context_window_tokens?: number;
  max_output_tokens?: number;
  is_enabled: boolean;
  reason?: string;
}

export interface AiRouteTarget {
  model_slug: string;
  max_attempts: number;
  allow_fallback_on: string[];
}

export type AiRouteFallbackCategory =
  | "rate_limited"
  | "quota_exhausted"
  | "timeout"
  | "network"
  | "provider_5xx";

export interface AiRoutePolicyPublishInput {
  display_name: string;
  description?: string;
  targets: AiRouteTarget[];
  prompt_revision?: string;
  reason?: string;
}

export interface AiRoutePolicy {
  policy_id: string;
  feature: string;
  display_name: string;
  description: string | null;
  current_version: number | null;
  is_enabled: boolean;
  updated_at: string;
}

export interface AiRoutePolicyVersion {
  route_policy_version_id: string;
  policy_id: string;
  feature: string;
  version: number;
  targets: AiRouteTarget[];
  prompt_revision: string | null;
  published_at: string;
  published_by_user_id: string | null;
}

export interface AiModelPriceVersion {
  price_version_id: string;
  model_id: string;
  model_slug: string;
  currency: string;
  effective_from: string;
  effective_to: string | null;
  input_per_million: string | number | null;
  cached_read_input_per_million: string | number | null;
  cached_write_input_per_million: string | number | null;
  output_per_million: string | number | null;
  reasoning_per_million: string | number | null;
  request_unit_price: string | number | null;
  page_unit_price: string | number | null;
  source: string;
  is_active: boolean;
  created_at: string;
}

export interface AiModelPriceVersionCreateInput {
  model_slug: string;
  currency: string;
  effective_from: string;
  effective_to?: string;
  input_per_million?: string;
  cached_read_input_per_million?: string;
  cached_write_input_per_million?: string;
  output_per_million?: string;
  reasoning_per_million?: string;
  request_unit_price?: string;
  page_unit_price?: string;
  source: string;
  is_active: boolean;
  reason?: string;
}

export interface AiRunUsage {
  run_id: string;
  organization_id: string;
  feature: string;
  service_kind: string;
  status: string;
  started_at: string | null;
  finished_at: string | null;
  total_cost_cny_micros: number | null;
  cost_status: string;
  invocation_count: number;
  potentially_billed_invocation_count: number;
}

export interface AiUsageAggregate {
  organization_id: string;
  feature: string;
  model_slug: string;
  invocation_count: number;
  costed_invocation_count: number;
  unavailable_cost_invocation_count: number;
  potentially_billed_invocation_count: number;
  reported_cost_cny_micros: number;
  known_run_count: number;
  partial_run_count: number;
  unavailable_run_count: number;
}

export interface AiUsageQuery {
  organization_id?: string;
  feature?: string;
  started_at_from?: string;
  started_at_to?: string;
  limit?: number;
  offset?: number;
}
