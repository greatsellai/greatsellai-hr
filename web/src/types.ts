/**
 * TypeScript representations of the public FastAPI contracts in app/schemas.py.
 * Dates stay as strings because the API serializes them as ISO-8601 text.
 */

export type DegreeLevel =
  | "unknown"
  | "vocational_or_below"
  | "high_school"
  | "associate"
  | "bachelor"
  | "master"
  | "doctor";

export type ExperienceType =
  | "employment"
  | "internship"
  | "project"
  | "research"
  | "competition"
  | "campus"
  | "club"
  | "volunteer"
  | "entrepreneurship"
  | "training"
  | "other"
  | "unknown";

export type JobRequirementPriority = "must_have" | "preferred";

export type JobRequirementCategory =
  | "skill"
  | "experience"
  | "education"
  | "major"
  | "keyword"
  | "other";

export type JobMatchOutcome = "met" | "partial" | "not_met" | "unknown";

/**
 * Server-owned lifecycle for the asynchronous AI fact extraction job.
 * This is deliberately separate from `extraction_status`, which describes
 * native-PDF parsing and the human-review lifecycle.
 */
export type AiExtractionStatus =
  | "queued"
  | "running"
  | "completed"
  | "needs_attention"
  | "unavailable";

export type JsonObject = Record<string, unknown>;

/** Identity attached to the current server-side workspace session. */
export interface AuthUser {
  user_id: string;
  display_name: string;
  email: string;
}

export interface AuthOrganization {
  organization_id: string;
  name: string;
}

export type MembershipRole = "admin" | "recruiter";

export type PlanStatus = "trial" | "active" | "expired" | "suspended";

export interface OrganizationPlan {
  code: "basic" | "advanced" | "professional" | string;
  name: string;
  feature_flags: Record<string, boolean>;
}

export interface TrialAccess {
  plan_status: PlanStatus;
  trial_started_at: string | null;
  trial_ends_at: string | null;
  /** Server-calculated whole calendar days, never computed from browser time. */
  trial_days_remaining: number | null;
  access_enabled: boolean;
}

export interface AuthSession {
  authenticated: boolean;
  login_required: boolean;
  is_platform_admin: boolean;
  email_verified: boolean;
  email_verification_required: boolean;
  user: AuthUser | null;
  organization: AuthOrganization | null;
  role: MembershipRole | null;
  plan: OrganizationPlan | null;
  trial: TrialAccess | null;
}

export interface AuthLoginInput {
  email: string;
  password: string;
}

export interface AuthRegistrationInput {
  organization_name: string;
  full_name: string;
  email: string;
  password: string;
}

/** Public, display-only onboarding offer. The server remains authoritative. */
export interface RegistrationOffer {
  plan_code: string;
  plan_name: string;
  trial_days: number;
}

export interface EmailVerificationResendResult {
  accepted: boolean;
  delivery_available: boolean;
}

/** Deliberately contains no account-existence or reset-token information. */
export interface PasswordResetRequestResult {
  accepted: boolean;
  delivery_available: boolean;
}

export interface CandidateCreateInput {
  display_name?: string | null;
}

export interface CandidateCreated {
  candidate_id: string;
}

export interface MailboxConfig {
  /** Stable ID of this named mailbox source. */
  mailbox_id: string;
  /** Human-readable source label, unique within the workspace. */
  display_name: string;
  configured: boolean;
  imap_host: string | null;
  imap_port: number | null;
  email_address: string | null;
  mailbox: string | null;
  enabled: boolean;
  /** Archived sources no longer receive new mail, but keep their import audit trail. */
  archived_at: string | null;
  password_configured: boolean;
  import_started_at: string | null;
  last_synced_at: string | null;
  last_sync_error: string | null;
  /** Present only when this channel has an unresolved terminal sync incident. */
  active_sync_alert: MailboxSyncAlertSummary | null;
}

export interface MailboxSyncAlertSummary {
  severity: "warning" | "critical";
  consecutive_failures: number;
  opened_at: string;
  last_failed_at: string;
  last_error_code: string;
}

export interface MailboxConfigCreate {
  display_name: string;
  imap_host: string;
  imap_port: number;
  email_address: string;
  mailbox: string;
  password: string;
  enabled: boolean;
}

/** PATCH payload. Leave the authorization code out to keep the saved value. */
export interface MailboxConfigPatch {
  display_name?: string;
  imap_host?: string;
  imap_port?: number;
  email_address?: string;
  mailbox?: string;
  password?: string;
  enabled?: boolean;
}

export interface MailboxConfigList {
  items: MailboxConfig[];
  total: number;
}

export type MailboxBackgroundJobKind = "sync" | "attachment_retry";
export type MailboxBackgroundJobTrigger = "manual" | "scheduled";
export type MailboxBackgroundJobStatus = "queued" | "running" | "completed" | "failed";

/** Safe, pollable state for IMAP work that runs in the worker process. */
export interface MailboxBackgroundJob {
  job_id: string;
  mailbox_id: string;
  job_kind: MailboxBackgroundJobKind;
  trigger_type: MailboxBackgroundJobTrigger;
  status: MailboxBackgroundJobStatus;
  import_id: string | null;
  attempt_count: number;
  max_attempts: number;
  imported_count: number;
  duplicate_count: number;
  skipped_count: number;
  failed_count: number;
  last_error: string | null;
  requested_at: string;
  started_at: string | null;
  completed_at: string | null;
  deduplicated: boolean;
}

export interface MailboxBackgroundJobHistory {
  items: MailboxBackgroundJob[];
  total: number;
}

export interface MailboxBackgroundJobBatch {
  items: MailboxBackgroundJob[];
  queued_count: number;
  deduplicated_count: number;
}

export type MailboxImportStatus =
  | "processing"
  | "deduplicating"
  | "imported"
  | "duplicate"
  | "skipped"
  | "failed"
  | "retrying";

export interface MailboxImportHistoryItem {
  import_id: string;
  mailbox_config_id: string;
  mailbox_display_name: string | null;
  attachment_filename: string;
  status: MailboxImportStatus;
  error: string | null;
  resume_id: string | null;
  attempt_count: number;
  last_attempted_at: string | null;
  can_retry: boolean;
  created_at: string;
}

export interface MailboxImportHistory {
  items: MailboxImportHistoryItem[];
  total: number;
}

export type MailboxRetentionPolicy = "minimal" | "standard" | "audit";

export interface MailboxRetentionOverview {
  configured: boolean;
  retention_policy: MailboxRetentionPolicy;
  body_copy_count: number;
  attachment_copy_count: number;
  failure_artifact_count: number;
  cache_bytes: number;
  expired_body_count: number;
  expired_attachment_copy_count: number;
  expired_failure_artifact_count: number;
  expired_bytes: number;
  earliest_expires_at: string | null;
  last_cleanup_at: string | null;
  next_cleanup_at: string | null;
}

export interface MailboxRetentionUpdate {
  retention_policy: MailboxRetentionPolicy;
}

export interface MailboxRetentionPreview {
  retention_policy: MailboxRetentionPolicy;
  expired_body_count: number;
  expired_attachment_copy_count: number;
  expired_failure_artifact_count: number;
  expired_bytes: number;
  skipped_count: number;
}

export type MailboxRetentionRunTrigger = "manual" | "scheduled";
export type MailboxRetentionRunStatus =
  | "queued"
  | "running"
  | "completed"
  | "completed_with_errors"
  | "failed";

export interface MailboxRetentionRun {
  run_id: string;
  trigger_type: MailboxRetentionRunTrigger;
  status: MailboxRetentionRunStatus;
  retention_policy: MailboxRetentionPolicy;
  started_at: string | null;
  finished_at: string | null;
  scanned_count: number;
  deleted_count: number;
  skipped_count: number;
  failed_count: number;
  reclaimed_bytes: number;
  next_cleanup_at: string | null;
  error_code: string | null;
}

export interface MailboxRetentionRuns {
  items: MailboxRetentionRun[];
  total: number;
}

export type RecruitingAgentIntent =
  | "search_candidates"
  | "run_job_matching"
  | "show_job_ranking"
  | "explain_candidate"
  | "score_current_candidate"
  | "show_mailbox_status"
  | "show_mailbox_imports"
  | "sync_mailbox"
  | "help";

export interface RecruitingAgentTurnInput {
  message: string;
  job_version_id?: string | null;
  resume_id?: string | null;
}

export interface RecruitingAgentCandidate {
  candidate_id: string;
  resume_id: string;
  display_name: string | null;
  detail: string;
  score: number | null;
}

export interface RecruitingAgentAction {
  action: "open_resume" | "open_match_workspace" | "open_mailbox_workspace";
  label: string;
  resume_id: string | null;
}

export interface RecruitingAgentToolTrace {
  tool: string;
  summary: string;
}

export interface RecruitingAgentTurn {
  message: string;
  intent: RecruitingAgentIntent;
  job_version_id: string | null;
  candidates: RecruitingAgentCandidate[];
  actions: RecruitingAgentAction[];
  tool_trace: RecruitingAgentToolTrace[];
  batch_id: string | null;
}

export interface ResumeUploadResponse {
  resume_id: string;
  candidate_id: string;
  /** null until AI finds a source-grounded candidate name. */
  candidate_display_name: string | null;
  extraction_status: string;
  ai_extraction_status: AiExtractionStatus;
  ai_extraction_error: string | null;
  source_page_count: number;
  parsed_page_count: number;
  quality_flags: string[];
}

export interface ResumeReviewQueueItem {
  resume_id: string;
  candidate_id: string;
  candidate_display_name: string | null;
  original_filename: string;
  extraction_status: string;
  ai_extraction_status: AiExtractionStatus;
  ai_extraction_error: string | null;
  quality_flags: string[];
  created_at: string;
}

export interface ResumeReviewQueueResponse {
  items: ResumeReviewQueueItem[];
  total: number;
  page: number;
  page_size: number;
}

export interface ResumeDetail {
  resume_id: string;
  candidate_id: string;
  candidate_display_name: string | null;
  extraction_status: string;
  ai_extraction_status: AiExtractionStatus;
  ai_extraction_error: string | null;
  is_active: boolean;
  retention_hold: boolean;
  /** null means the school decision still needs a reviewer. */
  is_985_211: boolean | null;
  highest_degree: DegreeLevel | null;
  employment_months: number;
  employment_or_internship_months: number;
  source_page_count: number;
  parsed_page_count: number;
  quality_flags: string[];
}

/**
 * A short-lived, session-bound link to an original candidate document.  The
 * browser requests one only after an explicit preview or download action so
 * the server can keep those two audit events distinct.
 */
export type CandidateDataFileAccessPurpose = "view" | "download";

export interface CandidateDataFileAccessRequest {
  purpose: CandidateDataFileAccessPurpose;
}

export interface CandidateDataFileAccessResponse {
  access_url: string;
  expires_at: string;
}

export type CandidateDataDeletionReason =
  | "candidate_request"
  | "recruitment_closed"
  | "duplicate"
  | "retention_expired"
  | "other";

export interface CandidateDataDeletionRequest {
  reason: CandidateDataDeletionReason;
  /** Used only to confirm an `other` deletion reason; the server does not persist it. */
  other_note?: string | null;
}

export interface CandidateDataDeletionResponse {
  deletion_batch_id: string;
  recovery_deadline_at: string;
  purge_after_at: string;
  affected_candidate_count: number;
  affected_resume_count: number;
}

export interface CandidateDataRestoreResponse {
  deletion_batch_id: string;
  restored_candidate_count: number;
  restored_resume_count: number;
  restored_at: string;
}

/** Metadata-only recovery item. It intentionally contains no candidate name or file name. */
export interface CandidateDataDeletionBatch {
  deletion_batch_id: string;
  trigger_type: string;
  reason: CandidateDataDeletionReason;
  status: string;
  recovery_deadline_at: string;
  purge_after_at: string;
  affected_candidate_count: number;
  affected_resume_count: number;
  restorable: boolean;
  restored_at: string | null;
  purged_at: string | null;
}

export interface CandidateDataDeletionBatchList {
  items: CandidateDataDeletionBatch[];
  total: number;
}

export type CandidateDataRetentionMode = "manual" | "automatic";

export interface CandidateDataRetentionPolicy {
  mode: CandidateDataRetentionMode;
  retention_days: number | null;
  version: number;
  updated_at: string;
}

export interface CandidateDataRetentionPreview {
  preview_token: string;
  policy_version: number;
  retention_days: number;
  eligible_candidate_count: number;
  eligible_resume_count: number;
  held_candidate_count: number;
  already_deleted_count: number;
  calculated_at: string;
}

export interface CandidateDataRetentionCleanupRun {
  run_id: string;
  trigger_type: "manual" | "scheduled";
  status: string;
  policy_version: number;
  retention_days: number | null;
  started_at: string;
  finished_at: string | null;
  scanned_count: number;
  queued_count: number;
  skipped_hold_count: number;
  failed_count: number;
  error_code: string | null;
}

export interface CandidateDataRetentionCleanupRunList {
  items: CandidateDataRetentionCleanupRun[];
  total: number;
}

export interface CandidateDataExportCreate {
  candidate_ids: string[];
  include_originals: boolean;
}

export interface CandidateDataExport {
  export_id: string;
  status: string;
  item_count: number;
  include_originals: boolean;
  requested_at: string;
  completed_at: string | null;
  expires_at: string | null;
  error_code: string | null;
}

export interface CandidateDataExportList {
  items: CandidateDataExport[];
  total: number;
}

/** Opaque audit metadata, deliberately without candidate content. */
export interface CandidateDataAuditEvent {
  event_id: string;
  actor_user_id: string | null;
  actor_kind: string;
  action: string;
  target_type: string;
  target_id: string;
  result: string;
  reason_code: string | null;
  created_at: string;
}

export interface CandidateDataAuditEventList {
  items: CandidateDataAuditEvent[];
  total: number;
}

export interface ResumeSourceBlock {
  block_id: string;
  page_no: number;
  block_type: string;
  text: string;
}

export interface ResumeEducation {
  school_name_raw: string;
  school_match_state: string;
  /**
   * Mutually exclusive, recruiter-facing school classification. It is kept
   * separate from `degree`, which describes the candidate's qualification.
   */
  institution_classification: InstitutionClassification | null;
  degree: DegreeLevel;
  major_raw: string | null;
  start_month: string | null;
  end_month: string | null;
  institution_tiers: InstitutionTier[];
  average_score: number | null;
  gpa_value: number | null;
  gpa_scale: number | null;
  gpa_percent: number | null;
  rank_position: number | null;
  rank_total: number | null;
  rank_percent: number | null;
  evidence_block_ids: string[];
}

export interface ResumeExperience {
  experience_type: ExperienceType;
  experience_name_raw: string | null;
  organization_name_raw: string | null;
  title_raw: string | null;
  start_month: string | null;
  end_month: string | null;
  is_current: boolean;
  evidence_block_ids: string[];
  classification_evidence_block_ids: string[];
  detail_items: ResumeExperienceDetailItem[];
  leadership_context: string | null;
  leadership_role: string | null;
  award_level: string | null;
  award_result_raw: string | null;
}

export interface ResumeExperienceDetailItem {
  detail_raw: string;
  evidence_block_ids: string[];
}

export interface ResumeSkill {
  skill_display: string;
  skill_category: string | null;
  evidence_block_ids: string[];
}

export interface ResumeLanguageCredential {
  credential_code: LanguageCredentialCode;
  credential_name_raw: string;
  score: number | null;
  passed: boolean | null;
  evidence_block_ids: string[];
}

export interface ResumeScholarship {
  scholarship_name_raw: string;
  scholarship_level: string | null;
  evidence_block_ids: string[];
}

export interface ResumeReviewAction {
  action: string;
  actor: string;
  note: string | null;
  created_at: string;
}

export interface ResumeReviewDetail extends ResumeDetail {
  original_filename: string;
  facts_version: number;
  source_blocks: ResumeSourceBlock[];
  education: ResumeEducation[];
  experiences: ResumeExperience[];
  skills: ResumeSkill[];
  language_credentials: ResumeLanguageCredential[];
  scholarships: ResumeScholarship[];
  review_actions: ResumeReviewAction[];
}

export interface EducationFactInput {
  school_name_raw: string;
  degree?: DegreeLevel;
  major_raw?: string | null;
  start_month?: string | null;
  end_month?: string | null;
  evidence_block_ids: string[];
}

export interface ExperienceFactInput {
  experience_type: ExperienceType;
  experience_name_raw?: string | null;
  organization_name_raw?: string | null;
  title_raw?: string | null;
  start_month?: string | null;
  end_month?: string | null;
  is_current?: boolean;
  evidence_block_ids: string[];
  classification_evidence_block_ids?: string[];
  detail_items?: ResumeExperienceDetailItem[];
}

export interface SkillFactInput {
  skill_display: string;
  evidence_block_ids: string[];
}

export interface ResumeFactsSubmission {
  schema_version?: "resume_facts.v1";
  education?: EducationFactInput[];
  experiences?: ExperienceFactInput[];
  skills?: SkillFactInput[];
}

export interface ResumeFactsSaveRequest {
  facts: ResumeFactsSubmission;
  complete_review?: boolean;
  review_note?: string | null;
  /** Only valid when complete_review is true. */
  is_985_211_override?: boolean | null;
}

export interface ResumeActivateRequest {
  note?: string | null;
}

export interface EducationFilter {
  degree_in?: DegreeLevel[];
  school_name_contains?: string[];
  major_contains?: string[];
  institution_classifications_any_of?: InstitutionClassification[];
  /**
   * Legacy V2 field. New UI writes `institution_classifications_any_of`.
   * It remains typed only so saved historical filters can be handled safely.
   */
  institution_tiers_any_of?: InstitutionTier[];
  min_average_score?: number | null;
  min_gpa_percent?: number | null;
  max_rank_position?: number | null;
  max_rank_percent?: number | null;
}

export interface ExperienceFilter {
  experience_types?: ExperienceType[];
  experience_name_contains?: string[];
  organization_name_contains?: string[];
  title_contains?: string[];
  leadership_contexts_any_of?: LeadershipContext[];
  leadership_roles_any_of?: string[];
  award_levels_any_of?: AwardLevel[];
  award_result_contains?: string[];
}

export type InstitutionTier =
  | "211" | "985" | "double_first_class" | "key_undergraduate"
  | "first_tier" | "second_tier" | "regular_undergraduate"
  | "private_undergraduate" | "higher_vocational" | "overseas"
  // Kept for legacy response compatibility. New UI writes the exact
  // `InstitutionClassification` field instead.
  | "undergraduate" | "associate" | "secondary_vocational";

/**
 * Stable, mutually exclusive labels used by the recruiter-facing table and
 * its quick filters. In particular, `211` means 211-only, not 985 + 211.
 */
export type InstitutionClassification =
  | "985"
  | "211"
  | "undergraduate"
  | "associate"
  | "secondary_vocational"
  | "overseas";

export type LanguageCredentialCode =
  | "cet4" | "cet6" | "ielts" | "toefl"
  | "tem4" | "tem8" | "bec" | "toeic" | "custom";

export type PresenceStatus = "any" | "present" | "unknown";
export type LeadershipContext = "class" | "student_org" | "club" | "project_team" | "company";
export type AwardLevel = "national" | "provincial" | "school" | "department" | "other";
export type ScholarshipLevel = AwardLevel | "enterprise";

export interface LanguageCredentialFilter {
  credential_code: LanguageCredentialCode;
  custom_name_contains?: string | null;
  min_score?: number | null;
}

export interface LeadershipFilter {
  contexts_any_of?: LeadershipContext[];
  roles_any_of?: string[];
}

export interface FilterOption<T extends string = string> {
  value: T;
  label: string;
}

export interface FilterOptions {
  schema_version: string;
  degrees: Array<FilterOption<DegreeLevel>>;
  institution_classifications: Array<FilterOption<InstitutionClassification>>;
  /** Legacy data kept for historical filter compatibility only. */
  institution_tiers: Array<FilterOption<InstitutionTier>>;
  experience_types: Array<FilterOption<ExperienceType>>;
  skill_categories: Array<FilterOption<string>>;
  leadership_contexts: Array<FilterOption<LeadershipContext>>;
  award_levels: Array<FilterOption<AwardLevel>>;
  scholarship_levels: Array<FilterOption<ScholarshipLevel>>;
  language_credentials: Array<FilterOption<LanguageCredentialCode>>;
  graduation_statuses: Array<FilterOption<"any" | "fresh" | "previous">>;
  presence_statuses: Array<FilterOption<PresenceStatus>>;
  keyword_modes: Array<FilterOption<"broad" | "precise">>;
}

export interface CandidateSearchRequest {
  schema_version?: "candidate_filter.v2";
  is_985_211?: boolean | null;
  highest_degree_in?: DegreeLevel[];
  graduation_status?: "any" | "fresh" | "previous";
  fresh_graduate_start_month?: string | null;
  fresh_graduate_end_month?: string | null;
  min_employment_months?: number | null;
  min_employment_or_internship_months?: number | null;
  education_any_of?: EducationFilter[];
  experience_any_of?: ExperienceFilter[];
  skill_categories_any_of?: string[];
  skills_all_of?: string[];
  skills_any_of?: string[];
  language_credentials_any_of?: LanguageCredentialFilter[];
  scholarship_status?: PresenceStatus;
  scholarship_levels_any_of?: ScholarshipLevel[];
  scholarship_name_contains?: string[];
  competition_status?: PresenceStatus;
  competition_award_status?: PresenceStatus;
  leadership_any_of?: LeadershipFilter[];
  keywords?: string[];
  keyword_match_mode?: "broad" | "precise";
  keywords_all_of?: string[];
  keywords_any_of?: string[];
  limit?: number;
  cursor?: string | null;
}

export interface CandidateSearchMatch {
  filter_key: string;
  label: string;
  fact_type:
    | "aggregate" | "education" | "experience" | "skill"
    | "language" | "scholarship" | "keyword";
  evidence_block_ids: string[];
}

export type CandidateSearchDisplayFieldKey =
  | "institution_classifications"
  | "highest_degree"
  | "education_degree"
  | "graduation"
  | "employment_months"
  | "employment_or_internship_months"
  | "school"
  | "major"
  | "academic_performance"
  | "experience_type"
  | "experience_name"
  | "organization"
  | "title"
  | "experience_award"
  | "skills"
  | "language"
  | "scholarship"
  | "competition"
  | "leadership"
  | "keywords";

export interface CandidateSearchDisplayField {
  key: CandidateSearchDisplayFieldKey;
  values: string[];
  evidence_block_ids: string[];
}

export interface CandidateSearchItem {
  candidate_id: string;
  display_name: string | null;
  resume_id: string;
  original_filename: string;
  is_985_211: boolean;
  institution_classifications: InstitutionClassification[];
  highest_degree: DegreeLevel | null;
  employment_months: number;
  employment_or_internship_months: number;
  summary_preview: string | null;
  score_total: number | null;
  score_template_name: string | null;
  display_fields: CandidateSearchDisplayField[];
  matched_filters: string[];
  matched_evidence: CandidateSearchMatch[];
}

export interface CandidateSearchResponse {
  items: CandidateSearchItem[];
  next_cursor: string | null;
  needs_review_count: number;
}

export interface ResumeLibraryItem {
  resume_id: string;
  candidate_id: string;
  display_name: string | null;
  original_filename: string;
  created_at: string;
  extraction_status: string;
  ai_extraction_status: AiExtractionStatus;
  ai_extraction_error: string | null;
  is_active: boolean;
  ingestion_source_type: string;
  source_mailbox_config_id: string | null;
  source_mailbox_label: string | null;
  /** Source extraction warnings. These take precedence over an old active state. */
  quality_flags: string[];
  summary_preview: string | null;
  summary_created_at: string | null;
  score_total: number | null;
  score_status: string | null;
  score_template_name: string | null;
  score_created_at: string | null;
}

export interface ResumeLibraryResponse {
  items: ResumeLibraryItem[];
  total: number;
  page: number;
  page_size: number;
}

export interface SavedFilterCreate {
  name: string;
  filters: CandidateSearchRequest;
}

export interface SavedFilter {
  saved_filter_id: string;
  name: string;
  filters: CandidateSearchRequest;
  created_at: string;
  updated_at: string;
}

export interface ScoreDimensionInput {
  key: string;
  label: string;
  weight: number;
  guidance?: string | null;
}

export interface ScoreTemplateCreate {
  name: string;
  description?: string | null;
  dimensions: ScoreDimensionInput[];
}

export interface ScoreTemplate {
  template_id: string;
  name: string;
  description: string | null;
  version: number;
  dimensions: ScoreDimensionInput[];
}

export interface ResumeScoreDimension {
  key: string;
  label: string;
  weight: number;
  ai_raw_score: number;
  final_raw_score: number;
  /** Final weighted contribution; kept for compatibility with earlier API responses. */
  weighted_score: number;
  ai_weighted_score: number;
  final_weighted_score: number;
  rationale: string;
  fact_ids: string[];
  fact_evidence: ResumeScoreFactEvidence[];
  evidence_state: "grounded" | "insufficient_information";
  uncertainties: string[];
  manual_reason: string | null;
  adjusted_at: string | null;
  manual_adjustment: ResumeScoreManualAdjustment | null;
}

export interface ResumeScoreFactEvidence {
  fact_id: string;
  fact_type: "education" | "experience" | "skill" | "unknown";
  summary: string;
  evidence_block_ids: string[];
}

export interface ResumeScoreManualAdjustment {
  raw_score: number;
  reason: string;
  actor: string;
  adjusted_at: string;
}

export interface ResumeScoreRiskFlag {
  message: string;
  fact_ids: string[];
  fact_evidence: ResumeScoreFactEvidence[];
}

export interface ResumeScoreAnalysis {
  schema_version: string | null;
  overall_summary: string;
  risk_flags: ResumeScoreRiskFlag[];
  needs_human_review: boolean;
}

export interface ResumeScoreAuditEntry {
  audit_id: string;
  action: string;
  actor: string;
  reason: string | null;
  dimension_key: string | null;
  ai_raw_score: number | null;
  previous_final_raw_score: number | null;
  final_raw_score: number | null;
  facts_version: number | null;
  template_version: number | null;
  created_at: string;
}

export interface ResumeScore {
  score_id: string;
  resume_id: string;
  fact_snapshot_id: string | null;
  template_id: string;
  template_name: string | null;
  template_description: string | null;
  facts_version: number;
  template_version: number;
  fact_snapshot_created_at: string | null;
  is_current_facts_version: boolean;
  is_current_template_version: boolean;
  total_score: number;
  ai_total_score: number | null;
  dimension_scores: ResumeScoreDimension[];
  analysis: ResumeScoreAnalysis;
  audit_trail: ResumeScoreAuditEntry[];
  status: string;
  model_name: string | null;
  created_at: string;
}

export interface ResumeScoreCreate {
  template_id: string;
}

export interface ResumeScoreBatch {
  batch_id: string;
  template_id: string;
  template_name: string | null;
  template_version: number;
  status: string;
  total_count: number;
  completed_count: number;
  failed_count: number;
  cached_count: number;
  requested_at: string;
  started_at: string | null;
  completed_at: string | null;
  last_error: string | null;
}

export interface ResumeScoreBatchItem {
  item_id: string;
  resume_id: string;
  candidate_id: string;
  candidate_display_name: string | null;
  facts_version: number;
  status: string;
  attempt_count: number;
  last_error: string | null;
  resume_score_id: string | null;
  was_cached: boolean;
  completed_at: string | null;
  updated_at: string;
}

export interface ResumeScoreOverride {
  raw_score: number;
  reason: string;
}

export interface ResumeSummary {
  summary_id: string;
  resume_id: string;
  fact_snapshot_id: string | null;
  facts_version: number;
  content: JsonObject;
  source: string;
  supersedes_id: string | null;
  is_current: boolean;
  status: string;
  model_name: string | null;
  created_at: string;
}

export interface ResumeSummaryManualCreate {
  content: Record<string, string>;
}

export interface JobRequirements {
  must_have?: string[];
  preferred?: string[];
}

/**
 * AI-generated role description used by the authoring flow before a role is
 * enabled for matching. Requirements are kept alongside the generated JD so
 * the final save can create a match-ready job in one request.
 */
export interface JobDescriptionGeneration {
  title: string;
  jd_text: string;
  requirements?: JobRequirements;
}

export interface JobDescriptionGenerateInput {
  title: string;
  brief: string;
}

/** A source JD published exactly as supplied, without any AI request. */
export interface OriginalJobPublishInput {
  title: string;
  jd_text: string;
}

export interface JobCreate {
  title: string;
  jd_text: string;
  requirements?: JobRequirements;
}

export interface JobClause {
  clause_id: string;
  ordinal: number;
  text: string;
}

export interface JobRequirementInput {
  requirement_key?: string | null;
  priority: JobRequirementPriority;
  category: JobRequirementCategory;
  raw_requirement: string;
  terms?: string[];
  minimum_months?: number | null;
  clause_ids: string[];
}

export interface JobRequirement {
  requirement_id: string;
  requirement_key: string;
  priority: JobRequirementPriority;
  category: JobRequirementCategory;
  raw_requirement: string;
  terms: string[];
  minimum_months: number | null;
  weight: number;
  clause_ids: string[];
  sort_order: number;
}

export interface JobVersion {
  job_version_id: string;
  job_id: string;
  version: number;
  title: string;
  raw_text: string;
  status: "draft" | "confirmed" | "archived";
  created_at: string;
  confirmed_at: string | null;
  clauses: JobClause[];
  requirements: JobRequirement[];
}

export interface JobVersionRequirementsUpdate {
  title: string;
  requirements?: JobRequirementInput[];
}

export interface JobMatchCreate {
  job_version_id: string;
}

export interface JobMatchBatch {
  batch_id: string;
  job_version_id: string;
  status: string;
  total_count: number;
  completed_count: number;
  failed_count: number;
  requested_at: string;
  started_at: string | null;
  completed_at: string | null;
  last_error: string | null;
}

export interface JobMatchBatchItem {
  item_id: string;
  resume_id: string;
  candidate_id: string;
  candidate_display_name: string | null;
  facts_version: number;
  status: string;
  attempt_count: number;
  last_error: string | null;
  job_match_id: string | null;
  completed_at: string | null;
  updated_at: string;
}

export interface JobMatchRequirementResult {
  requirement_id: string;
  requirement_key: string;
  priority: JobRequirementPriority;
  requirement_text: string;
  clause_ids: string[];
  outcome: JobMatchOutcome;
  reason: string;
  fact_ids: string[];
  missing_or_uncertain: string | null;
  score_contribution: number;
}

export interface JobMatch {
  match_id: string;
  job_id: string;
  job_version_id: string | null;
  resume_id: string;
  candidate_id: string;
  candidate_display_name: string | null;
  fact_snapshot_id: string | null;
  facts_version: number;
  job_version: number;
  /**
   * Evidence-normalized JD fit score. Older API responses only contain
   * `total_score`, so consumers must retain a backwards-compatible fallback.
   */
  match_score?: number | null;
  /** Percentage of weighted JD conditions with verifiable resume evidence. */
  match_confidence?: number | null;
  /** Server-provided screening lane when available. */
  match_lane?: "recommended" | "pending" | "unmet" | null;
  total_score: number;
  must_have_passed: boolean | null;
  evidence_coverage: number | null;
  hard_requirement_status: string | null;
  analysis: JsonObject;
  requirement_results: JobMatchRequirementResult[];
  status: string;
  model_name: string | null;
  created_at: string;
}
