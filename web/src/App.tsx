import {
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useRef,
  useState,
  type CSSProperties,
  type ChangeEvent,
  type DragEvent,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
} from "react";
import ReactMarkdown, { defaultUrlTransform } from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  api,
  isApiError,
} from "./api";
import {
  LandingPage,
  ROOT_WORKSPACE_BASE_PATH,
} from "./landing";
import type {
  AiExtractionStatus,
  AuthLoginInput,
  AuthRegistrationInput,
  AuthSession,
  CandidateSearchItem,
  CandidateSearchRequest,
  CandidateSearchResponse,
  AwardLevel,
  DegreeLevel,
  ExperienceType,
  FilterOptions,
  InstitutionTier,
  LanguageCredentialCode,
  LeadershipContext,
  PresenceStatus,
  ScholarshipLevel,
  JobMatchBatch,
  JobMatchBatchItem,
  JobMatch,
  JobRequirements,
  JobVersion,
  MailboxBackgroundJob,
  MailboxBackgroundJobHistory,
  MailboxConfig,
  MailboxImportHistory,
  MailboxImportHistoryItem,
  MailboxRetentionOverview,
  MailboxRetentionPolicy,
  MailboxRetentionPreview,
  MailboxRetentionRun,
  MailboxRetentionRuns,
  ResumeDetail,
  ResumeLibraryItem,
  ResumeLibraryResponse,
  ResumeReviewDetail,
  ResumeScore,
  ResumeScoreBatch,
  ResumeScoreBatchItem,
  ResumeSummary,
  ResumeUploadResponse,
  RegistrationOffer,
  RecruitingAgentAction,
  RecruitingAgentCandidate,
  RecruitingAgentTurn,
  RecruitingAgentToolTrace,
  SavedFilter,
  ScoreDimensionInput,
  ScoreTemplate,
  TrialAccess,
} from "./types";
import { Icon, type IconName } from "./icons";

const AdminApp = lazy(() => import("./admin/AdminApp"));

type View = "library" | "filter" | "upload" | "inbox" | "score" | "match";
type DrawerTab = "original" | "summary" | "evidence";
type SchoolFilter = "any" | "yes" | "no";
type MatchMode = "all" | "any";
type KeywordMode = "broad" | "precise";
type ToastKind = "success" | "error";
type JobWorkspaceMode = "create" | "view";
type AuthRoute = "login" | "register" | "forgot-password" | "verify-email";
type AppSurface =
  | { kind: "landing" }
  | { kind: "platform" }
  | { kind: "workspace"; authRoute: AuthRoute | null };

interface FilterDraft {
  school: SchoolFilter;
  minEmploymentMonths: number;
  minEmploymentOrInternshipMonths: number;
  degrees: DegreeLevel[];
  institutionTiers: InstitutionTier[];
  graduationStatus: "any" | "fresh" | "previous";
  freshGraduateStartMonth: string;
  freshGraduateEndMonth: string;
  schoolName: string;
  major: string;
  minAverageScore: string;
  minGpaPercent: string;
  maxRankPosition: string;
  maxRankPercent: string;
  experienceTypes: ExperienceType[];
  experienceName: string;
  company: string;
  title: string;
  experienceAwardLevels: AwardLevel[];
  experienceAwardResult: string;
  skills: string[];
  skillCategories: string[];
  skillsMode: MatchMode;
  languageCredentials: LanguageCredentialCode[];
  languageScores: Partial<Record<LanguageCredentialCode, string>>;
  customLanguageName: string;
  scholarshipStatus: PresenceStatus;
  scholarshipName: string;
  scholarshipLevels: ScholarshipLevel[];
  competitionStatus: PresenceStatus;
  competitionAwardStatus: PresenceStatus;
  leadershipContexts: LeadershipContext[];
  leadershipRoles: string[];
  keywords: string[];
  keywordsMode: KeywordMode;
}

interface SelectedResume {
  resumeId: string;
  candidateId: string;
  candidateName: string;
}

interface ToastMessage {
  id: number;
  kind: ToastKind;
  message: string;
}

type UploadStatus =
  "queued" | "uploading" | "extracting" | "success" | "attention" | "error";

interface UploadQueueItem {
  id: string;
  file: File;
  status: UploadStatus;
  idempotencyKey: string;
  response?: ResumeUploadResponse;
  error?: string;
  retryable?: boolean;
}

interface TemplateDraftDimension extends ScoreDimensionInput {
  id: string;
}

const emptySearch: CandidateSearchResponse = {
  items: [],
  next_cursor: null,
  needs_review_count: 0,
};

const fallbackRegistrationOffer: RegistrationOffer = {
  plan_code: "advanced",
  plan_name: "进阶版",
  trial_days: 30,
};

const defaultFilterDraft: FilterDraft = {
  school: "any",
  minEmploymentMonths: 0,
  minEmploymentOrInternshipMonths: 0,
  degrees: [],
  institutionTiers: [],
  graduationStatus: "any",
  freshGraduateStartMonth: `${new Date().getFullYear()}-01`,
  freshGraduateEndMonth: `${new Date().getFullYear() + 1}-12`,
  schoolName: "",
  major: "",
  minAverageScore: "",
  minGpaPercent: "",
  maxRankPosition: "",
  maxRankPercent: "",
  experienceTypes: [],
  experienceName: "",
  company: "",
  title: "",
  experienceAwardLevels: [],
  experienceAwardResult: "",
  skills: [],
  skillCategories: [],
  skillsMode: "all",
  languageCredentials: [],
  languageScores: {},
  customLanguageName: "",
  scholarshipStatus: "any",
  scholarshipName: "",
  scholarshipLevels: [],
  competitionStatus: "any",
  competitionAwardStatus: "any",
  leadershipContexts: [],
  leadershipRoles: [],
  keywords: [],
  keywordsMode: "broad",
};

/**
 * Each supported resume file is normalized by the API. Keeping a queue avoids
 * competing writes while still letting a recruiter add a whole folder in one action.
 */
const BATCH_UPLOAD_CONCURRENCY = 1;
const MAX_BATCH_FILES = 100;
const AI_STATUS_POLL_INTERVAL_MS = 2_500;

function aiExtractionIsInProgress(
  status: AiExtractionStatus | undefined,
): boolean {
  return status === "queued" || status === "running";
}

function aiExtractionStatusLabel(status: AiExtractionStatus): string {
  switch (status) {
    case "queued":
      return "已排队";
    case "running":
      return "提取中";
    case "completed":
      return "已完成，已启用";
    case "needs_attention":
      return "需要处理";
    case "unavailable":
      return "等待服务配置";
  }
}

/**
 * These flags describe a source-text failure, not an ordinary extraction
 * caveat such as an unresolved school.  When present, conclusions generated
 * from that text must not be presented as reliable merely because an older
 * resume version was once activated.
 */
const SOURCE_TEXT_UNRELIABLE_FLAGS = new Set([
  "source_text_unreliable",
]);
const PAGE_SOURCE_TEXT_UNRELIABLE_FLAG = /^page_\d+_source_text_unreliable$/i;
const POSSIBLE_MOJIBAKE_FLAG = /^page_\d+_possible_mojibake$/i;
const REPARSE_SOURCE_SUPERSEDED_FLAG =
  "reparse_source_superseded_before_completion";

function hasSourceTextQualityIssue(
  qualityFlags: readonly string[] | null | undefined,
): boolean {
  return Boolean(
    qualityFlags?.some(
      (flag) =>
        SOURCE_TEXT_UNRELIABLE_FLAGS.has(flag) ||
        PAGE_SOURCE_TEXT_UNRELIABLE_FLAG.test(flag) ||
        POSSIBLE_MOJIBAKE_FLAG.test(flag),
    ),
  );
}

function hasSupersededReparseVersion(
  qualityFlags: readonly string[] | null | undefined,
): boolean {
  return Boolean(qualityFlags?.includes(REPARSE_SOURCE_SUPERSEDED_FLAG));
}

function uploadStatusFromResponse(
  response: ResumeUploadResponse,
): UploadStatus {
  if (response.extraction_status === "failed") return "attention";
  if (response.ai_extraction_status === "completed") return "success";
  if (
    response.ai_extraction_status === "needs_attention" ||
    response.ai_extraction_status === "unavailable"
  )
    return "attention";
  return "extracting";
}

function withLatestAiExtractionStatus(
  uploaded: ResumeUploadResponse,
  detail: ResumeDetail,
): ResumeUploadResponse {
  return {
    ...uploaded,
    extraction_status: detail.extraction_status,
    ai_extraction_status: detail.ai_extraction_status,
    ai_extraction_error: detail.ai_extraction_error,
    source_page_count: detail.source_page_count,
    parsed_page_count: detail.parsed_page_count,
    quality_flags: detail.quality_flags,
  };
}

const degreeOptions: Array<{ value: DegreeLevel; label: string }> = [
  { value: "doctor", label: "博士" },
  { value: "master", label: "硕士" },
  { value: "bachelor", label: "本科" },
  { value: "associate", label: "大专" },
  { value: "high_school", label: "高中" },
  { value: "vocational_or_below", label: "中专/职高及以下" },
];

const experienceTypeOptions: Array<{
  value: ExperienceType;
  label: string;
}> = [
  { value: "employment", label: "正式工作" },
  { value: "internship", label: "实习" },
  { value: "project", label: "项目" },
  { value: "research", label: "科研" },
  { value: "competition", label: "技能竞赛" },
  { value: "campus", label: "校内/学生组织" },
  { value: "club", label: "社团" },
  { value: "volunteer", label: "志愿活动/社会实践" },
  { value: "entrepreneurship", label: "创业" },
  { value: "training", label: "培训" },
];

const degreeLabels: Record<DegreeLevel, string> = {
  unknown: "未知",
  vocational_or_below: "中专/职高及以下",
  high_school: "高中",
  associate: "大专",
  bachelor: "本科",
  master: "硕士",
  doctor: "博士",
};

const fallbackFilterOptions: FilterOptions = {
  schema_version: "filter-options.v2.fallback",
  degrees: degreeOptions,
  institution_tiers: [
    { value: "211", label: "211" },
    { value: "985", label: "985" },
    { value: "double_first_class", label: "双一流" },
    { value: "key_undergraduate", label: "重本" },
    { value: "first_tier", label: "一本" },
    { value: "second_tier", label: "二本" },
    { value: "regular_undergraduate", label: "普通本科" },
    { value: "private_undergraduate", label: "民办本科" },
    { value: "higher_vocational", label: "高职/高专" },
    { value: "overseas", label: "海外院校" },
  ],
  experience_types: experienceTypeOptions,
  skill_categories: [
    { value: "software", label: "编程与开发" },
    { value: "data_ai", label: "数据与 AI" },
    { value: "product_project", label: "产品与项目" },
    { value: "design_content", label: "设计与内容" },
    { value: "marketing_ecommerce_operations", label: "市场、电商与运营" },
    { value: "sales_customer_service", label: "销售与客户服务" },
    { value: "supply_chain_logistics", label: "供应链与物流" },
    { value: "finance_legal_hr", label: "财务、法务与人力资源" },
    { value: "office_collaboration", label: "办公与协作工具" },
    { value: "industry_professional", label: "行业专业技能" },
  ],
  leadership_contexts: [
    { value: "class", label: "班级" },
    { value: "student_org", label: "学生会/校内组织" },
    { value: "club", label: "社团" },
    { value: "project_team", label: "项目组" },
    { value: "company", label: "公司" },
  ],
  award_levels: [
    { value: "national", label: "国家级" },
    { value: "provincial", label: "省级" },
    { value: "school", label: "校级" },
    { value: "department", label: "院系级" },
    { value: "other", label: "其他明确级别" },
  ],
  scholarship_levels: [
    { value: "national", label: "国家级" },
    { value: "provincial", label: "省级" },
    { value: "school", label: "校级" },
    { value: "department", label: "院系级" },
    { value: "enterprise", label: "企业/社会奖学金" },
    { value: "other", label: "其他明确级别" },
  ],
  language_credentials: [
    { value: "cet4", label: "大学英语四级（CET-4）" },
    { value: "cet6", label: "大学英语六级（CET-6）" },
    { value: "ielts", label: "雅思（IELTS）" },
    { value: "toefl", label: "托福（TOEFL）" },
    { value: "tem4", label: "英语专业四级（TEM-4）" },
    { value: "tem8", label: "英语专业八级（TEM-8）" },
    { value: "bec", label: "剑桥商务英语（BEC）" },
    { value: "toeic", label: "托业（TOEIC）" },
    { value: "custom", label: "其他英语证书（自定义填写）" },
  ],
  graduation_statuses: [
    { value: "any", label: "不限" },
    { value: "fresh", label: "应届" },
    { value: "previous", label: "往届" },
  ],
  presence_statuses: [
    { value: "any", label: "不限" },
    { value: "present", label: "有明确记录" },
    { value: "unknown", label: "未知" },
  ],
  keyword_modes: [
    { value: "broad", label: "泛匹配" },
    { value: "precise", label: "精准匹配" },
  ],
};

const defaultTemplateDimensions: TemplateDraftDimension[] = [
  {
    id: "skill_fit",
    key: "skill_fit",
    label: "技能匹配",
    weight: 40,
    guidance: "重点看核心技术栈、工具与岗位场景的可验证匹配。",
  },
  {
    id: "experience_depth",
    key: "experience_depth",
    label: "经历深度",
    weight: 35,
    guidance: "重点看工作年限、职责范围、成果与复杂度。",
  },
  {
    id: "education_basis",
    key: "education_basis",
    label: "教育背景",
    weight: 25,
    guidance: "重点看学历、专业及必要的院校条件。",
  },
];

const navigation: Array<{ view: View; label: string; icon: IconName }> = [
  { view: "library", label: "简历库", icon: "folder" },
  { view: "filter", label: "筛选工作台", icon: "filter" },
  { view: "upload", label: "上传简历", icon: "upload" },
  { view: "inbox", label: "邮箱入库", icon: "inbox" },
  { view: "score", label: "评分规则", icon: "layers" },
  { view: "match", label: "岗位匹配", icon: "match" },
];

const mailboxRetentionPolicies: Array<{
  value: MailboxRetentionPolicy;
  label: string;
  description: string;
}> = [
  {
    value: "minimal",
    label: "最小保留",
    description: "正文和成功附件副本不持久化；失败附件保留 7 天。",
  },
  {
    value: "standard",
    label: "标准保留",
    description: "正文保留 7 天；成功附件副本保留 24 小时；失败附件保留 30 天。",
  },
  {
    value: "audit",
    label: "审计保留",
    description: "正文保留 30 天；成功附件副本保留 7 天；失败附件保留 90 天。",
  },
];

function freshDefaultFilter(): FilterDraft {
  return {
    ...defaultFilterDraft,
    degrees: [],
    institutionTiers: [],
    experienceTypes: [],
    experienceAwardLevels: [],
    skills: [],
    skillCategories: [],
    languageCredentials: [],
    languageScores: {},
    scholarshipLevels: [],
    leadershipContexts: [],
    leadershipRoles: [],
    keywords: [],
  };
}

const mailboxImportErrorMessages: Record<string, string> = {
  mailbox_import_not_found: "这条附件记录已不存在或无法访问。",
  mailbox_import_not_retryable: "这份附件当前不能重新入库。",
  mailbox_import_retry_in_progress: "这份附件正在重新入库，请稍后刷新。",
  mailbox_import_retry_superseded: "这份附件已由更新的重试请求接管，请刷新后查看结果。",
  mailbox_background_job_failed: "后台任务暂时失败，系统会按队列策略再次尝试。",
  mailbox_background_job_lease_expired: "后台任务意外中断，系统正在重新安排处理。",
  mailbox_task_source_changed: "收件通道配置已变化，旧的同步任务已停止。",
  mailbox_config_archived: "该收件通道已归档，不能再同步新邮件。",
  mailbox_not_enabled: "该收件邮箱已暂停，请启用后重试。",
  mailbox_credentials_unavailable: "邮箱授权码无法读取，请重新保存后再同步。",
  mailbox_imap_host_not_allowed: "该 IMAP 地址不在当前部署允许的服务商范围内。",
  mailbox_imap_port_not_allowed: "只支持加密 IMAPS 的 993 端口。",
  mailbox_imap_address_not_allowed: "该 IMAP 地址解析到不安全网络，系统已拒绝连接。",
  mailbox_imap_dns_failed: "无法安全解析该 IMAP 地址，请检查服务商配置。",
  mailbox_imap_argument_invalid: "邮箱账号、文件夹或授权码包含 IMAP 不支持的字符，请重新配置。",
  mailbox_imap_response_line_too_large: "邮箱返回的数据行超过安全上限，系统已停止本次同步。",
  mailbox_connection_failed: "无法连接邮箱，请检查 IMAP 地址、端口和授权码。",
  mailbox_select_failed: "无法打开指定的邮箱文件夹。",
  mailbox_status_failed: "无法读取邮箱当前位置，请检查文件夹设置后重试。",
  mailbox_source_epoch_changed: "邮箱来源标识已变化，通道已暂停，请归档后新建。",
  mailbox_source_watermark_invalid: "邮箱 UID 水位线异常，通道已暂停，请归档后新建。",
  mailbox_message_too_large: "邮件超过系统可处理大小，已跳过且不会重复下载。",
  mailbox_message_headers_too_large: "邮件头超过系统可处理范围，已安全跳过。",
  mailbox_mime_structure_too_complex: "邮件 MIME 结构过于复杂，已安全跳过。",
  mailbox_attachment_count_exceeded: "邮件附件数量超过单封处理上限，已安全跳过。",
  mailbox_attachment_too_large: "邮件中的简历附件超过单个文件上限，已安全跳过。",
  mailbox_attachment_total_too_large: "邮件中的简历附件总量超过单封处理上限，已安全跳过。",
  mailbox_search_response_too_large: "邮箱待处理邮件范围过大，系统暂未展开扫描。",
  attachment_validation_failed: "附件未通过文件校验，请候选人重新发送。",
  attachment_text_extraction_failed: "附件文字提取失败，请候选人重新发送清晰原件。",
  attachment_import_failed: "附件暂时无法入库，请稍后重试。",
  attachment_message_unavailable: "原邮件或附件已无法获取，请候选人重新发送。",
  attachment_source_changed: "收件邮箱来源已变化，不能安全重试该附件。",
  attachment_source_unavailable: "原收件邮箱已不可用，不能重试该附件。",
  attachment_retry_interrupted: "上次重新入库被中断，可再次尝试。",
  attachment_content_claim_expired: "相同附件的处理未完成，现可重新入库。",
};

function mailboxImportErrorLabel(error: string | null): string {
  if (!error) return "附件处理没有完成，请稍后重试。";
  return mailboxImportErrorMessages[error] ?? "附件处理没有完成，请稍后重试。";
}

function mailboxImportStatusLabel(status: string, canRetry = false): string {
  switch (status) {
    case "imported":
      return "已入库";
    case "duplicate":
      return "已去重";
    case "deduplicating":
      return "等待去重";
    case "skipped":
      return "已跳过";
    case "retrying":
      return canRetry ? "可重新入库" : "正在重试";
    case "failed":
      return "处理失败";
    default:
      return "处理中";
  }
}

function mailboxBackgroundJobStatusLabel(job: MailboxBackgroundJob): string {
  if (job.status === "queued") return job.job_kind === "sync" ? "等待后台同步" : "等待后台重试";
  if (job.status === "running") return job.job_kind === "sync" ? "正在后台同步" : "正在后台重试";
  if (job.status === "completed") return "已完成";
  return "处理失败";
}

function mailboxBackgroundJobStatusClass(job: MailboxBackgroundJob): string {
  if (job.status === "completed") return "is-success";
  if (job.status === "failed") return "is-error";
  return "is-progress";
}

function mailboxRetentionPolicyLabel(policy: MailboxRetentionPolicy): string {
  return mailboxRetentionPolicies.find((option) => option.value === policy)?.label ?? "标准保留";
}

function mailboxRetentionRunStatusLabel(status: MailboxRetentionRun["status"]): string {
  switch (status) {
    case "queued":
      return "等待执行";
    case "running":
      return "正在清理";
    case "completed":
      return "已完成";
    case "completed_with_errors":
      return "完成但有异常";
    case "failed":
      return "清理失败";
  }
}

function mailboxRetentionRunStatusClass(status: MailboxRetentionRun["status"]): string {
  switch (status) {
    case "completed":
      return "is-success";
    case "completed_with_errors":
      return "is-warning";
    case "failed":
      return "is-error";
    default:
      return "is-progress";
  }
}

function mailboxRetentionRunErrorLabel(errorCode: string | null): string {
  if (!errorCode) return "";
  const labels: Record<string, string> = {
    retention_cleanup_interrupted: "清理任务被中断，可稍后重试。",
    retention_cleanup_storage_failed: "部分缓存副本暂时无法删除，系统会稍后重试。",
    retention_cleanup_retry_scheduled: "部分内容将按退避策略再次清理。",
    storage_delete_failed: "部分缓存副本暂时无法删除，系统会在下次任务中重试。",
  };
  return labels[errorCode] ?? "部分内容尚未清理完成，系统会保留安全记录后重试。";
}

function mailboxRetentionDueCount(summary: Pick<MailboxRetentionPreview, "expired_body_count" | "expired_attachment_copy_count" | "expired_failure_artifact_count">): number {
  return summary.expired_body_count
    + summary.expired_attachment_copy_count
    + summary.expired_failure_artifact_count;
}

function humanizeError(error: unknown): string {
  if (isApiError(error)) {
    const messages: Record<string, string> = {
      invalid_login_credentials: "邮箱或密码不正确，请重试。",
      email_already_registered: "该邮箱已注册，请直接登录或找回密码。",
      email_delivery_not_configured: "注册邮件服务正在配置中，请稍后再试。",
      registration_rate_limit_exceeded: "当前注册请求较多，请稍后再试。",
      email_verification_required: "请先完成工作邮箱验证后再进入工作台。",
      email_verification_invalid_or_expired: "这条验证链接无效或已过期，请重新发送验证邮件。",
      email_verification_account_mismatch: "当前浏览器已登录其他工作区，请先退出后再打开验证链接。",
      email_verification_resend_too_soon: "验证邮件刚刚发送，请稍候一分钟后再试。",
      email_verification_resend_limit_reached: "今天的验证邮件发送次数已达到上限，请明天再试。",
      invalid_registration_input: "请检查企业名称、姓名、邮箱和密码后重试。",
      password_too_short: "密码至少需要 8 个字符。",
      trial_expired: "试用期已结束。数据已保留，请联系 GreatSell AI 团队继续使用。",
      organization_access_suspended: "当前工作区暂不可用，请联系 GreatSell AI 团队。",
      invalid_admin_token: "管理口令无效。请在右上角连接配置中更新后重试。",
      server_missing_admin_token: "服务器尚未配置管理口令，暂时无法访问。",
      deepseek_api_key_not_configured:
        "AI 服务尚未配置。请先在服务器环境变量中配置后重试。",
      resume_has_no_native_text_for_ai_extraction:
        "这份简历没有足够的可提取文字，暂时不能由 AI 提取。",
      resume_source_text_unavailable:
        "这份简历没有可用的提取文字，暂时不能由 AI 提取。",
      resume_source_text_unreliable:
        "这份简历的提取文本待校正，暂不能用于筛选、评分、总结或 JD 匹配。",
      completed_resume_cannot_be_reextracted:
        "这份简历已启用，不能被后台 AI 任务覆盖。",
      resume_must_be_active_and_ready_for_source_reparse:
        "当前版本尚未准备完成，暂时不能创建新的解析版本。",
      source_resume_ai_extraction_already_running:
        "当前版本仍在 AI 提取中，请完成后再创建新的解析版本。",
      source_resume_reparse_already_running:
        "这份简历已经在创建新的解析版本，请稍后刷新。",
      resume_original_hash_mismatch:
        "原始文件校验未通过，暂时不能重新解析。请重新上传原件。",
      resume_original_file_not_found:
        "找不到这份简历的原始文件。请重新上传该文件。",
      content_type_not_supported: "仅支持 PDF、Word、图片、Excel 和 HTML 简历文件。",
      unsupported_document_type: "仅支持 PDF、Word、图片、Excel 和 HTML 简历文件。",
      file_too_large: "简历文件过大，请压缩后再上传。",
      empty_upload: "这份简历是空文件。请重新选择原始文件后上传。",
      not_a_pdf: "文件内容不是有效的 PDF。请重新导出后上传。",
      database_conflict: "该简历与正在处理的请求冲突。请稍后重试。",
      invalid_idempotency_key: "上传请求标识无效。请重新选择该简历后重试。",
      idempotency_key_reused_with_different_pdf:
        "该简历的重试标识已被其他文件使用。请重新选择文件后上传。",
      resume_not_found: "这份简历已不存在或无法访问。",
      mailbox_not_configured: "请先保存邮箱配置。",
      mailbox_config_not_found: "这个收件通道已不存在或无法访问。",
      mailbox_display_name_required: "请为这个收件通道填写名称。",
      mailbox_duplicate_display_name: "该收件通道名称已被使用，请换一个名称。",
      mailbox_source_identity_locked:
        "该通道已有附件记录，不能改为其他邮箱。请新建一个收件通道。",
      mailbox_legacy_endpoint_ambiguous: "当前工作区已有多个收件通道，请刷新页面后重试。",
      mailbox_sync_in_progress: "这个收件通道正在同步，请稍后刷新查看结果。",
      mailbox_sync_claim_failed: "这个收件通道暂时无法开始同步，请稍后重试。",
      mailbox_config_archived: "这个收件通道已归档，不能继续同步。",
      mailbox_source_epoch_changed:
        "邮箱来源标识已变化，系统已暂停该通道。请归档后新建收件通道。",
      mailbox_password_required: "首次配置需要填写邮箱授权码。",
      mailbox_credentials_unavailable: "邮箱授权码无法读取，请重新保存后再同步。",
      mailbox_connection_failed: "无法连接邮箱，请检查 IMAP 地址、端口和授权码。",
      mailbox_select_failed: "无法打开指定的邮箱文件夹。",
      mailbox_status_failed: "无法读取邮箱当前位置，请检查文件夹设置后重试。",
      mailbox_search_failed: "无法检索邮箱中的附件。",
      mailbox_sync_failed: "邮箱入库暂时异常，请稍后重试。",
      mailbox_retention_policy_invalid: "内容保留策略无效，请重新选择后保存。",
      mailbox_retention_run_not_found: "这条清理记录已不存在或无法访问。",
      organization_admin_required: "仅工作区管理员可以修改保留策略或执行清理。",
      ...mailboxImportErrorMessages,
      score_template_not_found: "评分规则不存在，请重新选择。",
      resume_score_batch_not_found: "评分任务不存在或已不可访问。",
      job_version_not_found: "岗位版本不存在，请重新创建。",
      jd_generation_response_truncated:
        "岗位需求较长，AI 未能完整生成 JD。请精简后重试。",
      jd_generation_provider_failed:
        "AI 生成 JD 暂时不可用，请稍后重试。",
      jd_generation_service_unavailable:
        "AI 生成 JD 服务暂时不可用，请稍后重试。",
      jd_requirements_response_truncated:
        "JD 较长，AI 未能完整整理匹配条件。请稍后重试。",
      jd_requirements_provider_failed:
        "AI 整理 JD 条件暂时不可用，请稍后重试。",
    };
    const message = messages[error.message];
    if (message) return message;
    if (error.status >= 500) return "服务暂时不可用，请稍后重试。";
    return `操作没有完成：${error.message}`;
  }
  return "操作没有完成。请检查网络后重试。";
}

function humanizeAgentError(error: unknown): string {
  if (isApiError(error)) {
    const messages: Record<string, string> = {
      agent_model_not_configured: "招聘助手尚未配置 AI 服务。",
      agent_model_timeout: "招聘助手响应超时，请稍后重试。",
      agent_model_network_error: "招聘助手暂时无法连接 AI 服务，请稍后重试。",
      agent_service_unavailable: "招聘助手暂时不可用，请稍后重试。",
      agent_model_invalid_response: "招聘助手暂时没有返回有效结果，请重新发送。",
      agent_model_empty_response: "招聘助手暂时没有返回有效结果，请重新发送。",
      agent_model_missing_final_answer: "招聘助手暂时没有完成回答，请重新发送。",
      agent_model_invalid_tool_calls: "招聘助手的工具调用异常，请重新发送。",
      agent_model_tool_loop_limit: "招聘助手本次处理步骤过多，请换一种说法后重试。",
    };
    if (messages[error.message]) return messages[error.message];
    if (error.message.startsWith("agent_model_http_")) {
      return error.message === "agent_model_http_429"
        ? "招聘助手请求过于频繁，请稍后重试。"
        : "招聘助手暂时不可用，请稍后重试。";
    }
  }
  return humanizeError(error);
}

function isRetryableAgentError(error: unknown): boolean {
  if (!isApiError(error)) return true;
  return error.status === 408 || error.status === 429 || error.status >= 500;
}

const SUPPORTED_RESUME_EXTENSIONS = new Set([
  ".pdf",
  ".doc",
  ".docx",
  ".png",
  ".jpg",
  ".jpeg",
  ".xls",
  ".xlsx",
  ".html",
  ".htm",
]);

function resumeFileExtension(filename: string): string {
  const dot = filename.lastIndexOf(".");
  return dot >= 0 ? filename.slice(dot).toLowerCase() : "";
}

function isSupportedResumeFile(file: File): boolean {
  return SUPPORTED_RESUME_EXTENSIONS.has(resumeFileExtension(file.name));
}

function resumeFileTypeLabel(filename: string): string {
  const extension = resumeFileExtension(filename);
  if (extension === ".pdf") return "PDF";
  if (extension === ".doc" || extension === ".docx") return "Word";
  if (extension === ".xls" || extension === ".xlsx") return "Excel";
  if (extension === ".png" || extension === ".jpg" || extension === ".jpeg") return "图片";
  if (extension === ".html" || extension === ".htm") return "HTML";
  return "文件";
}

function canPreviewInline(filename: string): boolean {
  const extension = resumeFileExtension(filename);
  return [".pdf", ".png", ".jpg", ".jpeg", ".html", ".htm"].includes(extension);
}

function fileFingerprint(file: File): string {
  return `${file.name.toLocaleLowerCase()}-${file.size}-${file.lastModified}`;
}

function formatFileSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.ceil(bytes / 1024))} KB`;
  return `${(bytes / 1024 / 1024).toFixed(2)} MB`;
}

function createUploadIdempotencyKey(): string {
  if (
    typeof crypto !== "undefined" &&
    typeof crypto.randomUUID === "function"
  ) {
    return crypto.randomUUID();
  }
  return `upload-${Date.now()}-${Math.random().toString(36).slice(2, 14)}`;
}

function isRetryableUploadError(error: unknown): boolean {
  if (!isApiError(error)) return true;
  return error.status === 408 || error.status === 429 || error.status >= 500;
}

function clampMonths(value: number): number {
  return Math.max(0, Math.min(240, Math.round(value / 12) * 12));
}

function formatMonths(months: number): string {
  if (months <= 0) return "未设门槛";
  const years = Math.floor(months / 12);
  const rest = months % 12;
  return rest ? `${years} 年 ${rest} 个月` : `${years} 年`;
}

function formatLibraryDate(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function draftToSearchRequest(
  draft: FilterDraft,
  cursor: string | null = null,
): CandidateSearchRequest {
  const request: CandidateSearchRequest = {
    schema_version: "candidate_filter.v2",
    limit: 50,
    cursor,
  };

  if (draft.school === "yes") request.is_985_211 = true;
  if (draft.school === "no") request.is_985_211 = false;
  if (draft.minEmploymentMonths > 0) {
    request.min_employment_months = draft.minEmploymentMonths;
  }
  if (draft.minEmploymentOrInternshipMonths > 0) {
    request.min_employment_or_internship_months =
      draft.minEmploymentOrInternshipMonths;
  }
  if (draft.degrees.length) request.highest_degree_in = draft.degrees;
  if (draft.graduationStatus !== "any") {
    request.graduation_status = draft.graduationStatus;
    request.fresh_graduate_start_month =
      draft.freshGraduateStartMonth || defaultFilterDraft.freshGraduateStartMonth;
    request.fresh_graduate_end_month =
      draft.freshGraduateEndMonth || defaultFilterDraft.freshGraduateEndMonth;
  }
  if (
    draft.institutionTiers.length ||
    draft.schoolName.trim() ||
    draft.major.trim() ||
    draft.minAverageScore ||
    draft.minGpaPercent ||
    draft.maxRankPosition ||
    draft.maxRankPercent
  ) {
    request.education_any_of = [
      {
        school_name_contains: draft.schoolName.trim()
          ? [draft.schoolName.trim()]
          : [],
        major_contains: draft.major.trim() ? [draft.major.trim()] : [],
        institution_tiers_any_of: draft.institutionTiers,
        min_average_score: draft.minAverageScore
          ? Number(draft.minAverageScore)
          : null,
        min_gpa_percent: draft.minGpaPercent
          ? Number(draft.minGpaPercent)
          : null,
        max_rank_position: draft.maxRankPosition
          ? Number(draft.maxRankPosition)
          : null,
        max_rank_percent: draft.maxRankPercent
          ? Number(draft.maxRankPercent)
          : null,
      },
    ];
  }
  if (
    draft.experienceTypes.length ||
    draft.experienceName.trim() ||
    draft.company.trim() ||
    draft.title.trim() ||
    draft.experienceAwardLevels.length ||
    draft.experienceAwardResult.trim()
  ) {
    request.experience_any_of = [
      {
        experience_types: draft.experienceTypes.length
          ? draft.experienceTypes
          : experienceTypeOptions.map((option) => option.value),
        experience_name_contains: draft.experienceName.trim()
          ? [draft.experienceName.trim()]
          : [],
        organization_name_contains: draft.company.trim()
          ? [draft.company.trim()]
          : [],
        title_contains: draft.title.trim() ? [draft.title.trim()] : [],
        award_levels_any_of: draft.experienceAwardLevels,
        award_result_contains: draft.experienceAwardResult.trim()
          ? [draft.experienceAwardResult.trim()]
          : [],
      },
    ];
  }
  if (draft.skillCategories.length) {
    request.skill_categories_any_of = draft.skillCategories;
  }
  if (draft.skills.length) {
    if (draft.skillsMode === "all") request.skills_all_of = draft.skills;
    else request.skills_any_of = draft.skills;
  }
  const validLanguageCredentials = draft.languageCredentials.filter(
    (code) => code !== "custom" || Boolean(draft.customLanguageName.trim()),
  );
  if (validLanguageCredentials.length) {
    request.language_credentials_any_of = validLanguageCredentials.map(
      (credential_code) => ({
        credential_code,
        custom_name_contains:
          credential_code === "custom"
            ? draft.customLanguageName.trim()
            : null,
        min_score: draft.languageScores[credential_code]
          ? Number(draft.languageScores[credential_code])
          : null,
      }),
    );
  }
  if (draft.scholarshipStatus !== "any" || draft.scholarshipName.trim()) {
    request.scholarship_status = draft.scholarshipStatus;
    request.scholarship_name_contains =
      draft.scholarshipStatus === "present" && draft.scholarshipName.trim()
      ? [draft.scholarshipName.trim()]
      : [];
    request.scholarship_levels_any_of =
      draft.scholarshipStatus === "present" ? draft.scholarshipLevels : [];
  }
  if (draft.competitionStatus !== "any") {
    request.competition_status = draft.competitionStatus;
  }
  if (draft.competitionAwardStatus !== "any") {
    request.competition_award_status = draft.competitionAwardStatus;
  }
  if (draft.leadershipContexts.length || draft.leadershipRoles.length) {
    request.leadership_any_of = [
      {
        contexts_any_of: draft.leadershipContexts,
        roles_any_of: draft.leadershipRoles,
      },
    ];
  }
  if (draft.keywords.length) {
    request.keywords = draft.keywords;
    request.keyword_match_mode = draft.keywordsMode;
  }
  return request;
}

function searchRequestToDraft(request: CandidateSearchRequest): FilterDraft {
  const education = request.education_any_of?.[0];
  const experience = request.experience_any_of?.[0];
  const savedDegrees = request.highest_degree_in ?? education?.degree_in ?? [];
  const savedInstitutionTiers = education?.institution_tiers_any_of ?? [];
  return {
    // V1's positive 985/211 switch maps visibly to 211 because every official
    // 985 entry is also tagged 211. The removed negative/unknown controls must
    // never survive as invisible conditions in the V2 editor.
    school: "any",
    minEmploymentMonths: request.min_employment_months ?? 0,
    minEmploymentOrInternshipMonths:
      request.min_employment_or_internship_months ?? 0,
    degrees: savedDegrees.filter((degree) => degree !== "unknown"),
    institutionTiers:
      savedInstitutionTiers.length || request.is_985_211 !== true
        ? savedInstitutionTiers
        : ["211"],
    graduationStatus: request.graduation_status ?? "any",
    freshGraduateStartMonth:
      request.fresh_graduate_start_month ?? defaultFilterDraft.freshGraduateStartMonth,
    freshGraduateEndMonth:
      request.fresh_graduate_end_month ?? defaultFilterDraft.freshGraduateEndMonth,
    schoolName: education?.school_name_contains?.[0] ?? "",
    major: education?.major_contains?.[0] ?? "",
    minAverageScore: education?.min_average_score?.toString() ?? "",
    minGpaPercent: education?.min_gpa_percent?.toString() ?? "",
    maxRankPosition: education?.max_rank_position?.toString() ?? "",
    maxRankPercent: education?.max_rank_percent?.toString() ?? "",
    experienceTypes: experience?.experience_types ?? [],
    experienceName: experience?.experience_name_contains?.[0] ?? "",
    company: experience?.organization_name_contains?.[0] ?? "",
    title: experience?.title_contains?.[0] ?? "",
    experienceAwardLevels: experience?.award_levels_any_of ?? [],
    experienceAwardResult: experience?.award_result_contains?.[0] ?? "",
    skills: request.skills_all_of ?? request.skills_any_of ?? [],
    skillCategories: request.skill_categories_any_of ?? [],
    skillsMode: request.skills_any_of?.length ? "any" : "all",
    languageCredentials:
      request.language_credentials_any_of?.map((item) => item.credential_code) ?? [],
    languageScores: Object.fromEntries(
      (request.language_credentials_any_of ?? [])
        .filter((item) => item.min_score != null)
        .map((item) => [item.credential_code, String(item.min_score)]),
    ),
    customLanguageName:
      request.language_credentials_any_of?.find(
        (item) => item.credential_code === "custom",
      )?.custom_name_contains ?? "",
    scholarshipStatus: request.scholarship_status ?? "any",
    scholarshipName: request.scholarship_name_contains?.[0] ?? "",
    scholarshipLevels: request.scholarship_levels_any_of ?? [],
    competitionStatus: request.competition_status ?? "any",
    competitionAwardStatus: request.competition_award_status ?? "any",
    leadershipContexts: request.leadership_any_of?.[0]?.contexts_any_of ?? [],
    leadershipRoles: request.leadership_any_of?.[0]?.roles_any_of ?? [],
    keywords: request.keywords ?? request.keywords_all_of ?? request.keywords_any_of ?? [],
    keywordsMode:
      request.keyword_match_mode ??
      (request.keywords_all_of?.length ? "precise" : "broad"),
  };
}

function isLocalDevelopmentHost(hostname: string) {
  return hostname === "localhost" || hostname === "127.0.0.1" || hostname === "::1";
}

function isRootMarketingHost(hostname: string) {
  return hostname === "greatsellai.net";
}

const HR_WORKSPACE_BASE_PATH = "/workspace";

function workspaceHref(path = "") {
  const normalizedPath = path && !path.startsWith("/") ? `/${path}` : path;
  const { hostname, pathname } = window.location;
  const isCompatibilityWorkspace =
    pathname === ROOT_WORKSPACE_BASE_PATH ||
    pathname.startsWith(`${ROOT_WORKSPACE_BASE_PATH}/`);

  if (isCompatibilityWorkspace) {
    return `${ROOT_WORKSPACE_BASE_PATH}${normalizedPath}` || ROOT_WORKSPACE_BASE_PATH;
  }

  // The public root site is intentionally a separate marketing surface. Its
  // calls to action always cross to the dedicated HR application origin, so
  // the root domain never needs to expose the authenticated HR API.
  if (window.location.hostname === "hr.greatsellai.net") {
    if (["/login", "/register", "/forgot-password"].includes(normalizedPath)) {
      return normalizedPath;
    }
    return `${HR_WORKSPACE_BASE_PATH}${normalizedPath}` || HR_WORKSPACE_BASE_PATH;
  }

  if (isRootMarketingHost(hostname)) {
    return `https://hr.greatsellai.net${normalizedPath || "/"}`;
  }

  return normalizedPath || "/";
}

function platformHref() {
  const { pathname } = window.location;
  return pathname === ROOT_WORKSPACE_BASE_PATH ||
    pathname.startsWith(`${ROOT_WORKSPACE_BASE_PATH}/`)
    ? `${ROOT_WORKSPACE_BASE_PATH}/platform`
    : "/platform";
}

function authRouteFromPath(pathname: string): AuthRoute | null {
  const normalized = pathname.replace(/\/+$/, "") || "/";
  if (normalized === "/login") return "login";
  if (normalized === "/register") return "register";
  if (normalized === "/forgot-password") return "forgot-password";
  if (normalized === "/verify-email") return "verify-email";
  return null;
}

function resolveAppSurface(): AppSurface {
  const { hostname, pathname } = window.location;
  const compatibilityPlatformPath = `${ROOT_WORKSPACE_BASE_PATH}/platform`;
  const isPrimaryPlatformPath =
    pathname === "/platform" || pathname.startsWith("/platform/");
  const isCompatibilityPlatformPath =
    pathname === compatibilityPlatformPath ||
    pathname.startsWith(`${compatibilityPlatformPath}/`);
  if (
    isCompatibilityPlatformPath ||
    (
      isPrimaryPlatformPath &&
      (hostname === "hr.greatsellai.net" || isLocalDevelopmentHost(hostname))
    )
  ) {
    return { kind: "platform" };
  }
  const isWorkspacePath =
    pathname === ROOT_WORKSPACE_BASE_PATH ||
    pathname.startsWith(`${ROOT_WORKSPACE_BASE_PATH}/`);

  if (isWorkspacePath) {
    const nestedPath = pathname.slice(ROOT_WORKSPACE_BASE_PATH.length).replace(/\/+$/, "") || "/";
    return { kind: "workspace", authRoute: authRouteFromPath(nestedPath) };
  }

  if (hostname === "hr.greatsellai.net") {
    if (pathname === "/" || pathname === "") return { kind: "landing" };
    const nestedPath = pathname.startsWith(HR_WORKSPACE_BASE_PATH)
      ? pathname.slice(HR_WORKSPACE_BASE_PATH.length)
      : pathname;
    return { kind: "workspace", authRoute: authRouteFromPath(nestedPath) };
  }

  if (isLocalDevelopmentHost(hostname)) {
    return { kind: "workspace", authRoute: authRouteFromPath(pathname) };
  }

  return { kind: "landing" };
}

function ExternalRedirect({ href }: { href: string }) {
  useEffect(() => {
    window.location.replace(href);
  }, [href]);

  return (
    <main className="login-page" aria-live="polite">
      <div className="login-panel login-redirect-panel">
        <i className="spinner" /> 正在前往登录系统…
      </div>
    </main>
  );
}

function App() {
  const [surface, setSurface] = useState<AppSurface>(resolveAppSurface);

  useEffect(() => {
    const syncSurface = () => setSurface(resolveAppSurface());
    window.addEventListener("popstate", syncSurface);
    return () => window.removeEventListener("popstate", syncSurface);
  }, []);

  if (surface.kind === "landing") {
    return (
      <LandingPage
        loginHref={workspaceHref("/login")}
        registerHref={workspaceHref("/register")}
      />
    );
  }

  if (surface.kind === "platform") {
    return (
      <Suspense
        fallback={(
          <main className="login-page" aria-live="polite">
            <div className="login-panel login-redirect-panel">
              <i className="spinner" /> 正在打开平台管理…
            </div>
          </main>
        )}
      >
        <AdminApp />
      </Suspense>
    );
  }

  return <WorkspaceApp authRoute={surface.authRoute} />;
}

function WorkspaceApp({ authRoute }: { authRoute: AuthRoute | null }) {
  const [view, setView] = useState<View>("library");
  const [filterDraft, setFilterDraft] =
    useState<FilterDraft>(freshDefaultFilter);
  const [filterOptions, setFilterOptions] = useState<FilterOptions>(
    fallbackFilterOptions,
  );
  const [search, setSearch] = useState<CandidateSearchResponse>(emptySearch);
  const [searching, setSearching] = useState(false);
  const [savedFilters, setSavedFilters] = useState<SavedFilter[]>([]);
  const [selectedResume, setSelectedResume] = useState<SelectedResume | null>(
    null,
  );
  const [review, setReview] = useState<ResumeReviewDetail | null>(null);
  const [reviewLoading, setReviewLoading] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [agentOpen, setAgentOpen] = useState(false);
  const [drawerTab, setDrawerTab] = useState<DrawerTab>("original");
  const [pdfUrl, setPdfUrl] = useState<string | null>(null);
  const [pdfLoading, setPdfLoading] = useState(false);
  const [pdfError, setPdfError] = useState<string | null>(null);
  const [summaries, setSummaries] = useState<ResumeSummary[]>([]);
  const [summaryLoading, setSummaryLoading] = useState(false);
  const [reparsingSource, setReparsingSource] = useState(false);
  const [enrichingFacts, setEnrichingFacts] = useState(false);
  const [libraryRefreshToken, setLibraryRefreshToken] = useState(0);
  const [toasts, setToasts] = useState<ToastMessage[]>([]);
  const [globalQuery, setGlobalQuery] = useState("");
  const [authState, setAuthState] = useState<
    "checking" | "authenticated" | "unauthenticated"
  >("checking");
  const [authSession, setAuthSession] = useState<AuthSession | null>(null);
  const [authError, setAuthError] = useState<string | null>(null);
  const [authLoading, setAuthLoading] = useState(false);
  const reviewRequestRef = useRef(0);
  const summaryRequestRef = useRef(0);

  const selectedResumeId = selectedResume?.resumeId ?? null;

  useEffect(() => {
    const titles: Record<AuthRoute, string> = {
      login: "登录｜GreatSell AI 招聘工具",
      register: "免费试用｜GreatSell AI 招聘工具",
      "forgot-password": "找回密码｜GreatSell AI 招聘工具",
      "verify-email": "验证邮箱｜GreatSell AI 招聘工具",
    };
    document.title = authRoute ? titles[authRoute] : "招聘工作台｜GreatSell AI";
  }, [authRoute]);

  const notify = useCallback((kind: ToastKind, message: string) => {
    const id = Date.now() + Math.round(Math.random() * 1000);
    setToasts((current) => [...current, { id, kind, message }]);
    window.setTimeout(() => {
      setToasts((current) => current.filter((toast) => toast.id !== id));
    }, 5200);
  }, []);

  const refreshLibraryScores = useCallback(() => {
    setLibraryRefreshToken((current) => current + 1);
  }, []);

  const refreshSavedFilters = useCallback(async () => {
    try {
      setSavedFilters(await api.listSavedFilters());
    } catch (error) {
      notify("error", humanizeError(error));
    }
  }, [notify]);

  const runSearch = useCallback(
    async (
      draft: FilterDraft,
      append = false,
      cursor: string | null = null,
    ) => {
      setSearching(true);
      try {
        const response = await api.searchCandidates(
          draftToSearchRequest(draft, cursor),
        );
        setSearch((current) => ({
          ...response,
          items: append
            ? [...current.items, ...response.items]
            : response.items,
        }));
      } catch (error) {
        notify("error", humanizeError(error));
      } finally {
        setSearching(false);
      }
    },
    [notify],
  );

  const refreshReview = useCallback(
    async (resumeId: string) => {
      const requestId = ++reviewRequestRef.current;
      setReviewLoading(true);
      try {
        const detail = await api.getReview(resumeId);
        if (requestId === reviewRequestRef.current) {
          setReview(detail);
          setSelectedResume((current) => {
            if (!current || current.resumeId !== detail.resume_id)
              return current;
            return {
              ...current,
              candidateName:
                detail.candidate_display_name?.trim() || "未命名候选人",
            };
          });
        }
      } catch (error) {
        if (requestId === reviewRequestRef.current) {
          setReview(null);
          notify("error", humanizeError(error));
        }
      } finally {
        if (requestId === reviewRequestRef.current) setReviewLoading(false);
      }
    },
    [notify],
  );

  const loadSummaries = useCallback(
    async (resumeId: string) => {
      const requestId = ++summaryRequestRef.current;
      setSummaryLoading(true);
      try {
        const response = await api.listSummaries(resumeId);
        if (requestId === summaryRequestRef.current) setSummaries(response);
      } catch (error) {
        if (requestId === summaryRequestRef.current) {
          setSummaries([]);
          notify("error", humanizeError(error));
        }
      } finally {
        if (requestId === summaryRequestRef.current) setSummaryLoading(false);
      }
    },
    [notify],
  );

  useEffect(() => {
    void api
      .getAuthSession()
      .then((session) => {
        setAuthSession(session);
        setAuthState(session.authenticated ? "authenticated" : "unauthenticated");
      })
      .catch(() => {
        setAuthSession(null);
        setAuthState("unauthenticated");
      });
  }, []);

  useEffect(() => {
    if (
      authState !== "authenticated" ||
      authRoute ||
      authSession?.email_verification_required
    )
      return;
    void runSearch(defaultFilterDraft);
    void refreshSavedFilters();
    void api.getFilterOptions().then(setFilterOptions).catch(() => {
      setFilterOptions(fallbackFilterOptions);
    });
  }, [authRoute, authSession?.email_verification_required, authState, refreshSavedFilters, runSearch]);

  useEffect(() => {
    if (
      authState === "authenticated" &&
      authRoute &&
      authRoute !== "verify-email" &&
      !authSession?.email_verification_required
    ) {
      window.location.replace(workspaceHref());
    }
  }, [authRoute, authSession?.email_verification_required, authState]);

  useEffect(() => {
    if (
      !drawerOpen ||
      drawerTab !== "summary" ||
      !selectedResumeId ||
      !review ||
      review.resume_id !== selectedResumeId
    )
      return;
    if (hasSourceTextQualityIssue(review.quality_flags)) {
      setSummaries([]);
      return;
    }
    void loadSummaries(selectedResumeId);
  }, [drawerOpen, drawerTab, loadSummaries, review, selectedResumeId]);

  useEffect(() => {
    setSummaries([]);
  }, [selectedResumeId]);

  useEffect(() => {
    if (!drawerOpen || drawerTab !== "original" || !selectedResumeId) {
      setPdfUrl(null);
      setPdfLoading(false);
      return;
    }
    let cancelled = false;
    let revoke: (() => void) | undefined;
    setPdfLoading(true);
    setPdfError(null);
    setPdfUrl(null);
    void api
      .getPdfObjectUrl(selectedResumeId)
      .then((resource) => {
        if (cancelled) {
          resource.revoke();
          return;
        }
        revoke = resource.revoke;
        setPdfUrl(resource.url);
      })
      .catch((error) => {
        if (!cancelled) setPdfError(humanizeError(error));
      })
      .finally(() => {
        if (!cancelled) setPdfLoading(false);
      });
    return () => {
      cancelled = true;
      revoke?.();
    };
  }, [drawerOpen, drawerTab, selectedResumeId]);

  useEffect(() => {
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      setDrawerOpen(false);
      setAgentOpen(false);
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, []);

  const openCandidate = useCallback(
    (item: CandidateSearchItem, tab: DrawerTab = "summary") => {
      summaryRequestRef.current += 1;
      setReview(null);
      setSummaries([]);
      setSelectedResume({
        resumeId: item.resume_id,
        candidateId: item.candidate_id,
        candidateName: item.display_name?.trim() || "未命名候选人",
      });
      setDrawerTab(tab);
      setDrawerOpen(true);
      void refreshReview(item.resume_id);
    },
    [refreshReview],
  );

  const openUploadedResume = useCallback(
    (resumeId: string, candidateId: string) => {
      summaryRequestRef.current += 1;
      setReview(null);
      setSummaries([]);
      setSelectedResume({
        resumeId,
        candidateId,
        candidateName: "未命名候选人",
      });
      setDrawerTab("summary");
      setDrawerOpen(true);
      setView("library");
      setLibraryRefreshToken((current) => current + 1);
      void refreshReview(resumeId);
    },
    [refreshReview],
  );

  const openLibraryResume = useCallback(
    (item: ResumeLibraryItem) => {
      summaryRequestRef.current += 1;
      setReview(null);
      setSummaries([]);
      setSelectedResume({
        resumeId: item.resume_id,
        candidateId: item.candidate_id,
        candidateName: item.display_name?.trim() || "未命名候选人",
      });
      setDrawerTab("summary");
      setDrawerOpen(true);
      void refreshReview(item.resume_id);
    },
    [refreshReview],
  );

  const openMatchedResume = useCallback(
    (match: JobMatch) => {
      summaryRequestRef.current += 1;
      setReview(null);
      setSummaries([]);
      setSelectedResume({
        resumeId: match.resume_id,
        candidateId: match.candidate_id,
        candidateName: match.candidate_display_name?.trim() || "未命名候选人",
      });
      setDrawerTab("summary");
      setDrawerOpen(true);
      void refreshReview(match.resume_id);
    },
    [refreshReview],
  );

  const openAgentResume = useCallback(
    (item: RecruitingAgentCandidate) => {
      summaryRequestRef.current += 1;
      setReview(null);
      setSummaries([]);
      setSelectedResume({
        resumeId: item.resume_id,
        candidateId: item.candidate_id,
        candidateName: item.display_name?.trim() || "未命名候选人",
      });
      setAgentOpen(false);
      setDrawerTab("summary");
      setDrawerOpen(true);
      void refreshReview(item.resume_id);
    },
    [refreshReview],
  );

  const scoreLibraryResume = useCallback((item: ResumeLibraryItem) => {
    summaryRequestRef.current += 1;
    setSelectedResume({
      resumeId: item.resume_id,
      candidateId: item.candidate_id,
      candidateName: item.display_name?.trim() || "未命名候选人",
    });
    setDrawerOpen(false);
    setView("score");
  }, []);

  const applyFilter = async () => {
    await runSearch(filterDraft);
  };

  const resetFilter = async () => {
    const clean = freshDefaultFilter();
    setFilterDraft(clean);
    await runSearch(clean);
  };

  const saveCurrentFilter = async (name: string) => {
    const normalized = name.trim();
    if (!normalized) {
      notify("error", "请为这组筛选条件填写一个名称。");
      return;
    }
    try {
      await api.createSavedFilter({
        name: normalized,
        filters: draftToSearchRequest(filterDraft),
      });
      await refreshSavedFilters();
      notify("success", `已保存“${normalized}”。`);
    } catch (error) {
      notify("error", humanizeError(error));
    }
  };

  const applySavedFilter = (filter: SavedFilter) => {
    const next = searchRequestToDraft(filter.filters);
    setFilterDraft(next);
    void runSearch(next);
  };

  const deleteSavedFilter = async (filter: SavedFilter) => {
    try {
      await api.deleteSavedFilter(filter.saved_filter_id);
      await refreshSavedFilters();
      notify("success", `已删除“${filter.name}”。`);
    } catch (error) {
      notify("error", humanizeError(error));
    }
  };

  const generateSummary = async () => {
    if (!selectedResumeId) {
      notify("error", "请先从筛选结果中打开一份简历。");
      return;
    }
    setSummaryLoading(true);
    try {
      const summary = await api.generateSummary(selectedResumeId);
      setSummaries((current) => [
        summary,
        ...current
          .filter((item) => item.summary_id !== summary.summary_id)
          .map((item) => ({ ...item, is_current: false })),
      ]);
      setLibraryRefreshToken((current) => current + 1);
      notify("success", "AI 简历总结已生成。");
    } catch (error) {
      notify("error", humanizeError(error));
    } finally {
      setSummaryLoading(false);
    }
  };

  const createManualSummary = async (
    summaryId: string,
    content: Record<string, string>,
  ) => {
    try {
      const summary = await api.createManualSummaryVersion(summaryId, {
        content,
      });
      setSummaries((current) => [
        summary,
        ...current
          .filter((item) => item.summary_id !== summary.summary_id)
          .map((item) => ({ ...item, is_current: false })),
      ]);
      setLibraryRefreshToken((current) => current + 1);
      notify("success", "人工总结已保存为新的可追溯版本。");
    } catch (error) {
      notify("error", humanizeError(error));
      throw error;
    }
  };

  const reparseSelectedSource = useCallback(async () => {
    if (!selectedResumeId || reparsingSource) return;
    setReparsingSource(true);
    try {
      const parsed = await api.reparseSource(selectedResumeId);
      summaryRequestRef.current += 1;
      setReview(null);
      setSummaries([]);
      setSelectedResume((current) => ({
        resumeId: parsed.resume_id,
        candidateId: parsed.candidate_id,
        candidateName:
          parsed.candidate_display_name?.trim() ||
          current?.candidateName ||
          "未命名候选人",
      }));
      setDrawerTab("original");
      setLibraryRefreshToken((current) => current + 1);
      await refreshReview(parsed.resume_id);
      notify(
        "success",
        "已创建新的解析版本，正在基于原件重新提取。原版本会保留，不会被覆盖。",
      );
    } catch (error) {
      notify("error", humanizeError(error));
    } finally {
      setReparsingSource(false);
    }
  }, [notify, refreshReview, reparsingSource, selectedResumeId]);

  const enrichSelectedFacts = useCallback(async () => {
    if (!selectedResumeId || enrichingFacts) return;
    setEnrichingFacts(true);
    try {
      await api.enrichFilterFacts(selectedResumeId);
      await refreshReview(selectedResumeId);
      notify("success", "已提交高级筛选事实补充任务，旧事实会保留。完成后可刷新查看。");
    } catch (error) {
      notify("error", humanizeError(error));
    } finally {
      setEnrichingFacts(false);
    }
  }, [enrichingFacts, notify, refreshReview, selectedResumeId]);

  const handleGlobalSearch = (event: ReactKeyboardEvent<HTMLInputElement>) => {
    if (event.key !== "Enter") return;
    const terms = globalQuery
      .split(/[、,，\s]+/)
      .map((term) => term.trim())
      .filter(Boolean);
    const next = { ...filterDraft, keywords: terms };
    setFilterDraft(next);
    setView("filter");
    void runSearch(next);
  };

  const establishSession = (session: AuthSession) => {
    setAuthSession(session);
    setAuthState(session.authenticated ? "authenticated" : "unauthenticated");
    if (session.authenticated) {
      const nextPath = new URLSearchParams(window.location.search).get("next");
      if (
        nextPath &&
        (
          nextPath === "/platform" ||
          nextPath.startsWith("/platform/") ||
          nextPath === `${ROOT_WORKSPACE_BASE_PATH}/platform` ||
          nextPath.startsWith(`${ROOT_WORKSPACE_BASE_PATH}/platform/`)
        ) &&
        session.is_platform_admin
      ) {
        window.location.assign(nextPath);
        return session;
      }
      window.location.assign(
        workspaceHref(session.email_verification_required ? "/verify-email" : ""),
      );
    }
    return session;
  };

  const login = async (input: AuthLoginInput | string) => {
    setAuthError(null);
    setAuthLoading(true);
    try {
      return establishSession(await api.login(input));
    } catch (error) {
      setAuthError(humanizeError(error));
      return null;
    } finally {
      setAuthLoading(false);
    }
  };

  const register = async (input: AuthRegistrationInput) => {
    setAuthError(null);
    setAuthLoading(true);
    try {
      return establishSession(await api.register(input));
    } catch (error) {
      setAuthError(humanizeError(error));
      return null;
    } finally {
      setAuthLoading(false);
    }
  };

  const requestPasswordReset = async (email: string) => {
    setAuthError(null);
    setAuthLoading(true);
    try {
      return await api.requestPasswordReset(email);
    } catch (error) {
      setAuthError(humanizeError(error));
      return null;
    } finally {
      setAuthLoading(false);
    }
  };

  const completeEmailVerification = async (token: string) => {
    setAuthError(null);
    setAuthLoading(true);
    try {
      return establishSession(await api.completeEmailVerification(token));
    } catch (error) {
      setAuthError(humanizeError(error));
      return null;
    } finally {
      setAuthLoading(false);
    }
  };

  const resendEmailVerification = async () => {
    setAuthError(null);
    setAuthLoading(true);
    try {
      return await api.resendEmailVerification();
    } catch (error) {
      setAuthError(humanizeError(error));
      return null;
    } finally {
      setAuthLoading(false);
    }
  };

  const logout = async () => {
    await api.logout();
    setSelectedResume(null);
    setDrawerOpen(false);
    setAuthSession(null);
    setAuthState("unauthenticated");
    window.location.assign(workspaceHref("/login"));
  };

  if (authState === "checking") {
    return (
      <main className="login-page" aria-live="polite">
        <div className="login-panel login-redirect-panel">
          <i className="spinner" /> 正在验证登录状态…
        </div>
      </main>
    );
  }

  if (authState !== "authenticated" && !authRoute) {
    return <ExternalRedirect href={workspaceHref("/login")} />;
  }

  if (authRoute === "verify-email" || authSession?.email_verification_required) {
    return (
      <EmailVerificationPage
        error={authError}
        loading={authLoading}
        session={authSession}
        onComplete={completeEmailVerification}
        onResend={resendEmailVerification}
      />
    );
  }

  if (authState !== "authenticated") {
    if (authRoute === "register") {
      return <RegistrationPage error={authError} loading={authLoading} onRegister={register} />;
    }
    if (authRoute === "forgot-password") {
      return <ForgotPasswordPage error={authError} loading={authLoading} onRequest={requestPasswordReset} />;
    }
    return (
      <LoginPage
        error={authError}
        loading={authLoading}
        onLogin={login}
      />
    );
  }

  return (
    <div className="app-shell">
      <SideRail activeView={view} inert={drawerOpen || agentOpen} onChangeView={setView} />
      <div className="app-area" inert={drawerOpen || agentOpen}>
        <Topbar
          globalQuery={globalQuery}
          onGlobalQueryChange={setGlobalQuery}
          onGlobalSearchKeyDown={handleGlobalSearch}
          onOpenAgent={() => {
            setDrawerOpen(false);
            setAgentOpen(true);
          }}
          onLogout={() => void logout()}
          onNewUpload={() => setView("upload")}
          organizationName={authSession?.organization?.name ?? null}
          platformAdmin={authSession?.is_platform_admin ?? false}
          planName={authSession?.plan?.name ?? null}
          role={authSession?.role ?? null}
          trial={authSession?.trial ?? null}
        />
        <TrialStatusBanner planName={authSession?.plan?.name ?? null} trial={authSession?.trial ?? null} />
        <main className="main-content" id="main-content">
          {view === "library" && (
            <ResumeLibraryPage
              refreshToken={libraryRefreshToken}
              selectedResumeId={selectedResumeId}
              onOpenResume={openLibraryResume}
              onScoreResume={scoreLibraryResume}
              onUpload={() => setView("upload")}
            />
          )}
          {view === "filter" && (
            <FilterWorkspace
              draft={filterDraft}
              filterOptions={filterOptions}
              onDraftChange={setFilterDraft}
              savedFilters={savedFilters}
              search={search}
              searching={searching}
              selectedResumeId={selectedResumeId}
              onApply={applyFilter}
              onReset={resetFilter}
              onSave={saveCurrentFilter}
              onApplySaved={applySavedFilter}
              onDeleteSaved={deleteSavedFilter}
              onOpenCandidate={openCandidate}
              onLoadMore={() =>
                void runSearch(filterDraft, true, search.next_cursor)
              }
              onUpload={() => setView("upload")}
            />
          )}
          <div hidden={view !== "upload"}>
            <UploadPage onComplete={openUploadedResume} notify={notify} />
          </div>
          {view === "inbox" && (
            <MailboxPage
              notify={notify}
              onImported={() => setLibraryRefreshToken((current) => current + 1)}
              role={authSession?.role ?? null}
            />
          )}
          {view === "score" && (
            <ScorePage
              selected={selectedResume}
              notify={notify}
              onScoreCreated={refreshLibraryScores}
            />
          )}
          {view === "match" && (
            <MatchPage
              selected={selectedResume}
              notify={notify}
              onOpenMatchedResume={openMatchedResume}
            />
          )}
        </main>
      </div>

      <div
        aria-hidden="true"
        className={`drawer-scrim${drawerOpen || agentOpen ? " is-open" : ""}`}
        onClick={() => {
          setDrawerOpen(false);
          setAgentOpen(false);
        }}
      />
      <CandidateDrawer
        candidate={selectedResume}
        drawerTab={drawerTab}
        isOpen={drawerOpen}
        pdfError={pdfError}
        pdfLoading={pdfLoading}
        pdfUrl={pdfUrl}
        review={review}
        reviewLoading={reviewLoading}
        summaries={summaries}
        summaryLoading={summaryLoading}
        onClose={() => setDrawerOpen(false)}
        onCreateManualSummary={createManualSummary}
        onGenerateSummary={() => void generateSummary()}
        onReparseSource={() => void reparseSelectedSource()}
        onEnrichFacts={() => void enrichSelectedFacts()}
        enrichingFacts={enrichingFacts}
        onTabChange={setDrawerTab}
        reparsingSource={reparsingSource}
      />
      <RecruitingAgentDrawer
        isOpen={agentOpen}
        mailboxToolsAvailable={
          authSession?.role === "admin" &&
          authSession?.plan?.feature_flags.mailbox_import === true
        }
        selectedResume={selectedResume}
        onClose={() => setAgentOpen(false)}
        onOpenMatchWorkspace={() => {
          setAgentOpen(false);
          setView("match");
        }}
        onOpenMailboxWorkspace={() => {
          setAgentOpen(false);
          setView("inbox");
        }}
        onOpenResume={openAgentResume}
      />

      <ToastRegion
        toasts={toasts}
        onDismiss={(id) =>
          setToasts((current) => current.filter((toast) => toast.id !== id))
        }
      />
    </div>
  );
}

function LoginPage({
  error,
  loading,
  onLogin,
}: {
  error: string | null;
  loading: boolean;
  onLogin: (input: AuthLoginInput | string) => Promise<AuthSession | null>;
}) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [legacyMode, setLegacyMode] = useState(false);
  const canSubmit = legacyMode ? Boolean(password) : Boolean(email.trim() && password);
  return (
    <AuthPageLayout
      description="进入只属于你所在团队的招聘工作区。候选人、岗位、评分和原始文件按工作区分别管理。"
      eyebrow="GREATSELL AI · 招聘工具"
      title="登录招聘工作台"
    >
      <form
        className="auth-form"
        onSubmit={(event) => {
          event.preventDefault();
          if (legacyMode && password) {
            void onLogin(password);
          } else if (email.trim() && password) {
            void onLogin({ email: email.trim(), password });
          }
        }}
      >
        {!legacyMode && (
          <div className="field-stack">
            <label className="field-label" htmlFor="login-email">
              工作邮箱
            </label>
            <input
              autoComplete="email"
              className="field"
              id="login-email"
              inputMode="email"
              onChange={(event) => setEmail(event.target.value)}
              placeholder="name@company.com"
              required
              type="email"
              value={email}
            />
          </div>
        )}
        <div className="field-stack">
          <div className="auth-field-heading">
            <label className="field-label" htmlFor={legacyMode ? "legacy-login-password" : "login-password"}>
              {legacyMode ? "旧管理口令" : "密码"}
            </label>
            {!legacyMode && <a className="auth-inline-link" href={workspaceHref("/forgot-password")}>忘记密码</a>}
          </div>
          <input
            autoComplete="current-password"
            className="field"
            id={legacyMode ? "legacy-login-password" : "login-password"}
            onChange={(event) => setPassword(event.target.value)}
            placeholder={legacyMode ? "输入旧管理口令" : "输入密码"}
            required
            type="password"
            value={password}
          />
        </div>
        {error && <p className="auth-error" role="alert">{error}</p>}
        <button
          className="button button-primary auth-submit"
          disabled={loading || !canSubmit}
          type="submit"
        >
          {loading ? <><i className="spinner" />正在登录</> : legacyMode ? "使用口令登录" : "登录工作台"}
        </button>
        <div className="auth-mode-row">
          <span>{legacyMode ? "正在使用旧版工作区兼容登录" : "旧版工作区管理员？"}</span>
          <button
            aria-pressed={legacyMode}
            className="auth-mode-switch"
            onClick={() => setLegacyMode((current) => !current)}
            type="button"
          >
            {legacyMode ? "改用邮箱登录" : "使用旧管理口令"}
          </button>
        </div>
        <p className="auth-footer-copy">
          还没有团队工作区？<a href={workspaceHref("/register")}>免费试用 30 天</a>
        </p>
        <p className="auth-legacy-note">
          旧管理口令仅用于迁移中的原工作区，本次提交后不会写入浏览器本地存储。
        </p>
      </form>
    </AuthPageLayout>
  );
}

function RegistrationPage({
  error,
  loading,
  onRegister,
}: {
  error: string | null;
  loading: boolean;
  onRegister: (input: AuthRegistrationInput) => Promise<AuthSession | null>;
}) {
  const [organizationName, setOrganizationName] = useState("");
  const [fullName, setFullName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [formError, setFormError] = useState<string | null>(null);
  const [submitted, setSubmitted] = useState(false);
  const [registrationOffer, setRegistrationOffer] = useState<RegistrationOffer>(
    fallbackRegistrationOffer,
  );
  const [offerLoading, setOfferLoading] = useState(true);

  useEffect(() => {
    let active = true;
    void api
      .getRegistrationOffer()
      .then((offer) => {
        if (active) setRegistrationOffer(offer);
      })
      // Public offer details are helpful, never a reason to block signup.
      .catch(() => undefined)
      .finally(() => {
        if (active) setOfferLoading(false);
      });
    return () => {
      active = false;
    };
  }, []);

  const submit = async () => {
    setFormError(null);
    if (password !== confirmation) {
      setFormError("两次输入的密码不一致，请重新确认。");
      return;
    }
    const session = await onRegister({
      organization_name: organizationName.trim(),
      full_name: fullName.trim(),
      email: email.trim(),
      password,
    });
    if (session?.email_verification_required) setSubmitted(true);
  };

  return (
    <AuthPageLayout
      description="注册后即可使用大卖数智 AI 招聘工作台。上传简历，快速筛选、统一评分并查看 JD 匹配依据，把时间留给真正需要你判断的人。"
      eyebrow={offerLoading
        ? "30 天免费体验"
        : `${registrationOffer.trial_days} 天${registrationOffer.plan_name}免费体验`}
      title="让招聘判断，从第一份简历开始更快"
      variant="registration"
    >
      {submitted ? (
        <div aria-live="polite" className="auth-success-state">
          <span className="auth-success-icon"><Icon name="check" size={20} /></span>
          <h2>验证邮箱，马上进入工作台</h2>
          <p>验证邮件已发送到你填写的工作邮箱。点击邮件中的链接，即可开始上传第一份简历。</p>
          <a className="button button-primary auth-submit" href={workspaceHref("/verify-email")}>我已完成邮箱验证</a>
        </div>
      ) : (
        <div className="auth-registration">
          <div className="auth-registration-heading">
            <p>免费创建团队工作台</p>
            <h2>开始 {registrationOffer.trial_days} 天{registrationOffer.plan_name}体验</h2>
            <span>完成邮箱验证后，即可上传第一份简历。</span>
          </div>
          <form
            className="auth-form auth-registration-form"
            onSubmit={(event) => {
              event.preventDefault();
              void submit();
            }}
          >
            <div className="auth-form-grid">
              <div className="field-stack auth-form-span-2">
                <label className="field-label" htmlFor="register-organization">公司 / 团队名称</label>
                <input autoComplete="organization" className="field" id="register-organization" onChange={(event) => setOrganizationName(event.target.value)} placeholder="例如：大卖数智 AI 部" required value={organizationName} />
              </div>
              <div className="field-stack">
                <label className="field-label" htmlFor="register-name">姓名</label>
                <input autoComplete="name" className="field" id="register-name" onChange={(event) => setFullName(event.target.value)} placeholder="请输入你的姓名" required value={fullName} />
              </div>
              <div className="field-stack">
                <label className="field-label" htmlFor="register-email">工作邮箱</label>
                <input autoComplete="email" className="field" id="register-email" inputMode="email" onChange={(event) => setEmail(event.target.value)} placeholder="name@company.com" required type="email" value={email} />
              </div>
              <div className="field-stack">
                <label className="field-label" htmlFor="register-password">设置登录密码</label>
                <input autoComplete="new-password" className="field" id="register-password" minLength={8} onChange={(event) => setPassword(event.target.value)} placeholder="至少 8 个字符" required type="password" value={password} />
              </div>
              <div className="field-stack">
                <label className="field-label" htmlFor="register-password-confirmation">再次输入密码</label>
                <input aria-describedby={formError ? "register-password-error" : undefined} aria-invalid={Boolean(formError)} autoComplete="new-password" className="field" id="register-password-confirmation" minLength={8} onChange={(event) => setConfirmation(event.target.value)} placeholder="请再次输入" required type="password" value={confirmation} />
              </div>
            </div>
            {(formError || error) && <p className="auth-error" id="register-password-error" role="alert">{formError || error}</p>}
            <p className="auth-consent">提交后，我们会向你的工作邮箱发送验证邮件。完成验证即可进入工作台。</p>
            <button className="button button-primary auth-submit" disabled={loading || !organizationName.trim() || !fullName.trim() || !email.trim() || password.length < 8 || !confirmation} type="submit">
              {loading ? <><i className="spinner" />正在创建工作台</> : `免费开启 ${registrationOffer.trial_days} 天体验`}
            </button>
            <p className="auth-footer-copy">已有团队账号？<a href={workspaceHref("/login")}>立即登录</a></p>
          </form>
        </div>
      )}
    </AuthPageLayout>
  );
}

function EmailVerificationPage({
  error,
  loading,
  session,
  onComplete,
  onResend,
}: {
  error: string | null;
  loading: boolean;
  session: AuthSession | null;
  onComplete: (token: string) => Promise<AuthSession | null>;
  onResend: () => Promise<{ accepted: boolean; delivery_available: boolean } | null>;
}) {
  const token = new URLSearchParams(window.location.search).get("token");
  const completionStarted = useRef(false);
  const [resendState, setResendState] = useState<"idle" | "sent" | "unavailable">("idle");
  const email = session?.user?.email ?? null;
  const canResend = Boolean(session?.authenticated && session.email_verification_required);

  useEffect(() => {
    if (!token || completionStarted.current) return;
    completionStarted.current = true;
    void onComplete(token);
  }, [onComplete, token]);

  const maskedEmail = email
    ? email.replace(/^(.{1,2}).*(@.*)$/, "$1•••$2")
    : null;

  return (
    <AuthPageLayout
      description="验证工作邮箱后即可进入你的独立招聘工作区。候选人、简历、岗位和 AI 结论始终按工作区隔离。"
      eyebrow="账户验证"
      title={token ? "正在验证邮箱" : "请验证工作邮箱"}
    >
      <div aria-live="polite" className="auth-success-state">
        <span className="auth-success-icon">
          <Icon name={token ? "check" : "inbox"} size={20} />
        </span>
        <h2>{token ? "正在确认你的邮箱" : "请查收验证邮件"}</h2>
        {token ? (
          <p>{loading ? "请稍候，正在安全地验证这条链接。" : "验证链接无效或已失效时，你可以登录后重新发送邮件。"}</p>
        ) : (
          <p>
            {maskedEmail
              ? `请查看 ${maskedEmail} 的收件箱，并在 24 小时内打开验证链接。`
              : "请登录注册邮箱后打开验证链接，完成后即可进入工作台。"}
          </p>
        )}
        {error && <p className="auth-error" role="alert">{error}</p>}
        {canResend && !token && (
          <button
            className="button button-primary auth-submit"
            disabled={loading || resendState === "sent"}
            onClick={() => {
              void onResend().then((result) => {
                if (result?.accepted) {
                  setResendState(result.delivery_available ? "sent" : "unavailable");
                }
              });
            }}
            type="button"
          >
            {loading ? <><i className="spinner" />正在发送</> : resendState === "sent" ? "验证邮件已重新发送" : "重新发送验证邮件"}
          </button>
        )}
        {resendState === "unavailable" && (
          <p className="auth-error" role="status">暂时无法发送验证邮件，请稍后重试。</p>
        )}
        {!canResend && !token && (
          <a className="button button-primary auth-submit" href={workspaceHref("/login")}>返回登录</a>
        )}
        {token && !loading && (
          <a className="button button-primary auth-submit" href={workspaceHref("/login")}>返回登录</a>
        )}
        <p className="auth-footer-copy">
          验证前不会开放候选人或简历数据访问。
        </p>
      </div>
    </AuthPageLayout>
  );
}

function ForgotPasswordPage({
  error,
  loading,
  onRequest,
}: {
  error: string | null;
  loading: boolean;
  onRequest: (email: string) => Promise<{ accepted: boolean; delivery_available: boolean } | null>;
}) {
  const [email, setEmail] = useState("");
  const [result, setResult] = useState<{ deliveryAvailable: boolean } | null>(null);

  return (
    <AuthPageLayout
      description="我们不会在此页面显示邮箱是否已注册。重置链接仅发送给有效且可用的账号。"
      eyebrow="账户协助"
      title="找回登录密码"
    >
      {result ? (
        <div aria-live="polite" className="auth-success-state">
          <span className="auth-success-icon"><Icon name={result.deliveryAvailable ? "check" : "user"} size={20} /></span>
          <h2>{result.deliveryAvailable ? "请查看邮箱" : "请联系管理员"}</h2>
          <p>{result.deliveryAvailable ? "若该邮箱对应可用账号，我们已发送重置密码的后续指引。" : "当前团队暂未启用邮件重置，请联系管理员协助重置密码。"}</p>
          <a className="button button-primary auth-submit" href={workspaceHref("/login")}>返回登录</a>
        </div>
      ) : (
        <form
          className="auth-form"
          onSubmit={(event) => {
            event.preventDefault();
            void onRequest(email.trim()).then((response) => {
              if (response?.accepted) setResult({ deliveryAvailable: response.delivery_available });
            });
          }}
        >
          <div className="field-stack">
            <label className="field-label" htmlFor="reset-email">工作邮箱</label>
            <input autoComplete="email" className="field" id="reset-email" inputMode="email" onChange={(event) => setEmail(event.target.value)} placeholder="name@company.com" required type="email" value={email} />
            <p className="field-help">为保护账户安全，提交后的提示不会披露该邮箱是否已注册。</p>
          </div>
          {error && <p className="auth-error" role="alert">{error}</p>}
          <button className="button button-primary auth-submit" disabled={loading || !email.trim()} type="submit">
            {loading ? <><i className="spinner" />正在提交</> : "获取重置指引"}
          </button>
          <p className="auth-footer-copy"><a href={workspaceHref("/login")}>返回登录</a></p>
        </form>
      )}
    </AuthPageLayout>
  );
}

function AuthPageLayout({
  children,
  description,
  eyebrow,
  title,
  variant = "default",
}: {
  children: ReactNode;
  description: string;
  eyebrow: string;
  title: string;
  variant?: "default" | "registration";
}) {
  const isRegistration = variant === "registration";
  return (
    <main className={`auth-page${isRegistration ? " auth-page-registration" : ""}`}>
      <div className="auth-shell">
        <section className="auth-introduction" aria-labelledby="auth-page-title">
          <a className="auth-brand" href={workspaceHref("/")} aria-label="大卖数智首页">
            <img alt="大卖数智 GreatSell AI" src="/brand/greatsell-logo-cn-white.png" />
          </a>
          <div aria-hidden="true" className="auth-mark" />
          <p className="auth-kicker">{eyebrow}</p>
          <h1 id="auth-page-title">{title}</h1>
          <p>{description}</p>
          <ul className="auth-assurance-list">
            {isRegistration ? (
              <>
                <li><Icon name="spark" size={17} /><span>免费体验进阶版已开放能力，先使用再决定</span></li>
                <li><Icon name="layers" size={17} /><span>简历、岗位与候选人资料仅限你的团队访问</span></li>
                <li><Icon name="user" size={17} /><span>AI 先整理判断依据，是否推进始终由 HR 决定</span></li>
              </>
            ) : (
              <>
                <li><Icon name="layers" size={17} /><span>团队资料集中管理，仅限已授权成员访问</span></li>
                <li><Icon name="briefcase" size={17} /><span>从筛选、评分到 JD 匹配，在同一个工作台完成</span></li>
                <li><Icon name="user" size={17} /><span>AI 提供判断依据，最终决定始终属于招聘团队</span></li>
              </>
            )}
          </ul>
        </section>
        <section className="auth-panel" aria-label={title}>
          {children}
        </section>
      </div>
    </main>
  );
}

function TrialStatusBanner({
  planName,
  trial,
}: {
  planName: string | null;
  trial: TrialAccess | null;
}) {
  if (!trial) return null;
  const isExpired = trial.plan_status === "expired" || !trial.access_enabled;
  const isTrial = trial.plan_status === "trial";
  if (!isExpired && !isTrial) return null;
  const days = trial.trial_days_remaining;
  const remaining = typeof days === "number" ? Math.max(0, days) : null;
  return (
    <section className={`trial-banner${isExpired ? " is-expired" : ""}`} role={isExpired ? "alert" : "status"}>
      <div>
        <strong>{isExpired ? "试用期已结束" : `免费试用还剩 ${remaining ?? "—"} 天`}</strong>
        <p>{isExpired ? "你的工作区数据已保留。续费入口开放前，请联系 GreatSell AI 团队继续使用。" : `${planName ?? "进阶版"}试用中，已实现功能可正常体验。`}</p>
      </div>
      <span>{isExpired ? "数据已保留" : "30 天试用"}</span>
    </section>
  );
}

function SideRail({
  activeView,
  onChangeView,
  inert,
}: {
  activeView: View;
  onChangeView: (view: View) => void;
  inert: boolean;
}) {
  return (
    <aside aria-label="主导航" className="side-rail" inert={inert}>
      <div aria-label="AI 简历筛选工作台" className="rail-mark" role="img" />
      <nav className="rail-nav">
        {navigation.map((item) => (
          <button
            aria-current={activeView === item.view ? "page" : undefined}
            aria-label={item.label}
            className={`rail-item${activeView === item.view ? " is-active" : ""}`}
            key={item.view}
            onClick={() => onChangeView(item.view)}
            type="button"
          >
            <Icon name={item.icon} size={19} />
            <span className="rail-tooltip">{item.label}</span>
          </button>
        ))}
      </nav>
      <div className="rail-bottom">
        <button aria-label="工作记录" className="rail-item" type="button">
          <Icon name="history" size={18} />
          <span className="rail-tooltip">工作记录</span>
        </button>
        <button aria-label="设置" className="rail-item" type="button">
          <Icon name="gear" size={18} />
          <span className="rail-tooltip">设置</span>
        </button>
      </div>
    </aside>
  );
}

function Topbar({
  globalQuery,
  onGlobalQueryChange,
  onGlobalSearchKeyDown,
  onOpenAgent,
  onLogout,
  onNewUpload,
  organizationName,
  platformAdmin,
  planName,
  role,
  trial,
}: {
  globalQuery: string;
  onGlobalQueryChange: (value: string) => void;
  onGlobalSearchKeyDown: (event: ReactKeyboardEvent<HTMLInputElement>) => void;
  onOpenAgent: () => void;
  onLogout: () => void;
  onNewUpload: () => void;
  organizationName: string | null;
  platformAdmin: boolean;
  planName: string | null;
  role: "admin" | "recruiter" | null;
  trial: TrialAccess | null;
}) {
  const trialDays = trial?.trial_days_remaining;
  const roleLabel = role === "admin" ? "管理员" : role === "recruiter" ? "招聘官" : null;
  const trialLabel =
    trial?.plan_status === "trial" && typeof trialDays === "number"
      ? `试用 ${Math.max(0, trialDays)} 天`
      : trial?.plan_status === "expired"
        ? "试用已到期"
        : null;
  return (
    <header className="topbar">
      <div className="topbar-title-wrap">
        <p className="topbar-title">
          AI 简历筛选 <span>/ 工作台</span>
        </p>
        {(organizationName || roleLabel || planName) && (
          <p className="topbar-workspace" title={organizationName ?? undefined}>
            <span>{organizationName || "我的工作区"}</span>
            {roleLabel && <small>{roleLabel}</small>}
            {planName && <small>{planName}</small>}
          </p>
        )}
      </div>
      <label className="topbar-search">
        <Icon name="search" size={17} />
        <span className="sr-only">全局检索简历关键词</span>
        <input
          onChange={(event) => onGlobalQueryChange(event.target.value)}
          onKeyDown={onGlobalSearchKeyDown}
          placeholder="输入技能或关键词，按 Enter 筛选"
          value={globalQuery}
        />
      </label>
      <div className="topbar-actions">
        {trialLabel && <span className={`topbar-trial${trial?.plan_status === "expired" ? " is-expired" : ""}`}>{trialLabel}</span>}
        {platformAdmin && <a className="button button-ghost" href={platformHref()}><Icon name="layers" size={16} />平台管理</a>}
        <button
          className="button button-agent"
          onClick={onOpenAgent}
          type="button"
        >
          <Icon name="spark" size={16} />
          招聘助手
        </button>
        <button
          className="button button-ghost"
          onClick={onNewUpload}
          type="button"
        >
          <Icon name="upload" size={16} />
          上传简历
        </button>
        <button
          className="button button-ghost"
          onClick={onLogout}
          type="button"
        >
          退出登录
        </button>
      </div>
    </header>
  );
}

interface AgentChatMessage {
  id: number;
  role: "assistant" | "user";
  content: string;
  candidates?: RecruitingAgentCandidate[];
  actions?: RecruitingAgentAction[];
  toolTrace?: RecruitingAgentToolTrace[];
  failure?: boolean;
  retryMessage?: string;
}

function agentMarkdownUrlTransform(url: string): string {
  const normalized = defaultUrlTransform(url);
  return /^(?:https?:|mailto:)/i.test(normalized) ? normalized : "";
}

function AgentMarkdown({ content }: { content: string }) {
  return (
    <div className="agent-markdown">
      <ReactMarkdown
        components={{
          a({ children, href, node: _node, ...props }) {
            if (!href) return <>{children}</>;
            return (
              <a
                {...props}
                href={href}
                rel="noopener noreferrer"
                target="_blank"
              >
                {children}
              </a>
            );
          },
        }}
        disallowedElements={["img"]}
        remarkPlugins={[remarkGfm]}
        skipHtml
        urlTransform={agentMarkdownUrlTransform}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}

function RecruitingAgentDrawer({
  isOpen,
  mailboxToolsAvailable,
  selectedResume,
  onClose,
  onOpenMatchWorkspace,
  onOpenMailboxWorkspace,
  onOpenResume,
}: {
  isOpen: boolean;
  mailboxToolsAvailable: boolean;
  selectedResume: SelectedResume | null;
  onClose: () => void;
  onOpenMatchWorkspace: () => void;
  onOpenMailboxWorkspace: () => void;
  onOpenResume: (candidate: RecruitingAgentCandidate) => void;
}) {
  const [input, setInput] = useState("");
  const [jobs, setJobs] = useState<JobVersion[]>([]);
  const [jobVersionId, setJobVersionId] = useState("");
  const [loading, setLoading] = useState(false);
  const [messages, setMessages] = useState<AgentChatMessage[]>([
    {
      id: 1,
      role: "assistant",
      content:
        "我是招聘助手。可以筛选简历、处理 JD 匹配和评分；已开通邮箱入库的工作区也可以查询收件状态，并按你的指令发起后台同步。",
    },
  ]);

  useEffect(() => {
    if (!isOpen) return;
    void api
      .listConfirmedJobVersions()
      .then((items) => {
        // Original-published JDs intentionally have no AI matching conditions.
        // Keep them out of the Agent selector so a matching tool cannot target
        // a version that is display-only.
        const matchableJobs = items.filter(
          (item) => item.requirements.length > 0,
        );
        setJobs(matchableJobs);
        setJobVersionId((current) =>
          current &&
          matchableJobs.some((item) => item.job_version_id === current)
            ? current
            : (matchableJobs[0]?.job_version_id ?? ""),
        );
      })
      .catch(() => setJobs([]));
  }, [isOpen]);

  const addAssistantReply = (turn: RecruitingAgentTurn) => {
    setMessages((current) => [
      ...current,
      {
        id: Date.now() + 1,
        role: "assistant",
        content: turn.message,
        candidates: turn.candidates,
        actions: turn.actions,
        toolTrace: turn.tool_trace,
      },
    ]);
    if (turn.job_version_id) setJobVersionId(turn.job_version_id);
  };

  const send = async (raw: string) => {
    const message = raw.trim();
    if (!message || loading) return;
    setInput("");
    setMessages((current) => [
      ...current,
      { id: Date.now(), role: "user", content: message },
    ]);
    setLoading(true);
    try {
      const turn = await api.runRecruitingAgentTurn({
        message,
        job_version_id: jobVersionId || null,
        resume_id: selectedResume?.resumeId ?? null,
      });
      addAssistantReply(turn);
    } catch (error) {
      const failureMessage = humanizeAgentError(error);
      setMessages((current) => [
        ...current,
        {
          id: Date.now() + 1,
          role: "assistant",
          content: failureMessage,
          failure: true,
          retryMessage: isRetryableAgentError(error) ? message : undefined,
        },
      ]);
    } finally {
      setLoading(false);
    }
  };

  return (
    <aside
      aria-label="招聘助手"
      aria-modal="true"
      className={`recruiting-agent-drawer${isOpen ? " is-open" : ""}`}
      role="dialog"
    >
      <header className="agent-header">
        <div className="agent-title-wrap">
          <span className="agent-mark"><Icon name="spark" size={17} /></span>
          <div>
            <h2>招聘助手</h2>
            <p>工具执行，结论可追溯</p>
          </div>
        </div>
        <button aria-label="关闭招聘助手" className="icon-button" onClick={onClose} type="button">
          <Icon name="close" size={18} />
        </button>
      </header>
      <div className="agent-context">
        <div className="select-wrap">
          <label className="sr-only" htmlFor="agent-job-version">当前 JD</label>
          <select
            className="select-field"
            id="agent-job-version"
            onChange={(event) => setJobVersionId(event.target.value)}
            value={jobVersionId}
          >
            <option value="">自动选择最近可匹配的 JD</option>
            {jobs.map((item) => (
              <option key={item.job_version_id} value={item.job_version_id}>
                {item.title} · v{item.version}
              </option>
            ))}
          </select>
          <Icon name="chevron-down" size={15} />
        </div>
        <span className="agent-context-note">
          {selectedResume ? `当前候选人：${selectedResume.candidateName}` : "未选择候选人"}
        </span>
      </div>
      <div className="agent-conversation" aria-live="polite">
        {messages.map((item) => (
          <article
            className={`agent-message is-${item.role}${item.failure ? " is-error" : ""}`}
            key={item.id}
          >
            {item.role === "assistant" ? (
              <AgentMarkdown content={item.content} />
            ) : (
              <p>{item.content}</p>
            )}
            {item.retryMessage && (
              <div className="agent-retry-row">
                <button
                  className="button button-ghost agent-retry-button"
                  disabled={loading}
                  onClick={() => void send(item.retryMessage!)}
                  type="button"
                >
                  <Icon name="refresh" size={15} />
                  重新发送
                </button>
              </div>
            )}
            {!!item.toolTrace?.length && (
              <div className="agent-tool-trace">
                {item.toolTrace.map((trace, index) => (
                  <span key={`${trace.tool}-${index}`}>已调用：{trace.summary}</span>
                ))}
              </div>
            )}
            {!!item.candidates?.length && (
              <div className="agent-candidate-list">
                {item.candidates.map((candidate) => (
                  <button
                    className="agent-candidate-row"
                    key={candidate.resume_id}
                    onClick={() => onOpenResume(candidate)}
                    type="button"
                  >
                    <span>
                      <strong>{candidate.display_name?.trim() || "未命名候选人"}</strong>
                      <small>{candidate.detail}</small>
                    </span>
                    {candidate.score !== null && <b>{candidate.score.toFixed(1)}</b>}
                    <Icon name="chevron-right" size={16} />
                  </button>
                ))}
              </div>
            )}
            {item.actions?.some((action) => action.action === "open_match_workspace") && (
              <button className="button button-ghost agent-workspace-button" onClick={onOpenMatchWorkspace} type="button">
                <Icon name="match" size={15} />
                打开 JD 匹配工作区
              </button>
            )}
            {item.actions?.some((action) => action.action === "open_mailbox_workspace") && (
              <button className="button button-ghost agent-workspace-button" onClick={onOpenMailboxWorkspace} type="button">
                <Icon name="inbox" size={15} />
                打开邮箱附件入库
              </button>
            )}
          </article>
        ))}
        {loading && (
          <article className="agent-message is-assistant agent-loading">
            <i className="spinner" /> 正在调用招聘工具…
          </article>
        )}
      </div>
      <div className="agent-composer">
        <div className="agent-suggestions" aria-label="常用提问">
          {[
            "找 985/211、3 年以上的候选人",
            "为当前 JD 批量匹配",
            "查看当前 JD 排行榜",
            "解释当前候选人的分数",
            ...(mailboxToolsAvailable ? ["查看收件邮箱状态", "同步全部收件邮箱"] : []),
          ].map((prompt) => (
            <button disabled={loading} key={prompt} onClick={() => void send(prompt)} type="button">
              {prompt}
            </button>
          ))}
        </div>
        <form
          className="agent-input-row"
          onSubmit={(event) => {
            event.preventDefault();
            void send(input);
          }}
        >
          <label className="sr-only" htmlFor="agent-message">向招聘助手提问</label>
          <textarea
            id="agent-message"
            onChange={(event) => setInput(event.target.value)}
            placeholder="例如：找 985/211、3 年以上 Python 的候选人"
            rows={2}
            value={input}
          />
          <button aria-label="发送提问" className="button button-primary" disabled={loading || !input.trim()} type="submit">
            <Icon name="arrow-right" size={17} />
          </button>
        </form>
      </div>
    </aside>
  );
}

function FilterWorkspace({
  draft,
  filterOptions,
  onDraftChange,
  savedFilters,
  search,
  searching,
  selectedResumeId,
  onApply,
  onReset,
  onSave,
  onApplySaved,
  onDeleteSaved,
  onOpenCandidate,
  onLoadMore,
  onUpload,
}: {
  draft: FilterDraft;
  filterOptions: FilterOptions;
  onDraftChange: (draft: FilterDraft) => void;
  savedFilters: SavedFilter[];
  search: CandidateSearchResponse;
  searching: boolean;
  selectedResumeId: string | null;
  onApply: () => void;
  onReset: () => void;
  onSave: (name: string) => Promise<void>;
  onApplySaved: (filter: SavedFilter) => void;
  onDeleteSaved: (filter: SavedFilter) => Promise<void>;
  onOpenCandidate: (item: CandidateSearchItem, tab?: DrawerTab) => void;
  onLoadMore: () => void;
  onUpload: () => void;
}) {
  return (
    <div className="filter-workspace">
      <FilterPanel
        draft={draft}
        filterOptions={filterOptions}
        onApply={onApply}
        onApplySaved={onApplySaved}
        onDeleteSaved={onDeleteSaved}
        onDraftChange={onDraftChange}
        onReset={onReset}
        onSave={onSave}
        savedFilters={savedFilters}
      />
      <ResultsPane
        onLoadMore={onLoadMore}
        onOpenCandidate={onOpenCandidate}
        onUpload={onUpload}
        search={search}
        searching={searching}
        selectedResumeId={selectedResumeId}
      />
    </div>
  );
}

function FilterPanel({
  draft,
  filterOptions,
  onDraftChange,
  savedFilters,
  onApply,
  onReset,
  onSave,
  onApplySaved,
  onDeleteSaved,
}: {
  draft: FilterDraft;
  filterOptions: FilterOptions;
  onDraftChange: (draft: FilterDraft) => void;
  savedFilters: SavedFilter[];
  onApply: () => void;
  onReset: () => void;
  onSave: (name: string) => Promise<void>;
  onApplySaved: (filter: SavedFilter) => void;
  onDeleteSaved: (filter: SavedFilter) => Promise<void>;
}) {
  const [selectedSavedId, setSelectedSavedId] = useState("");
  const [saveName, setSaveName] = useState("");
  const [saving, setSaving] = useState(false);

  const update = (patch: Partial<FilterDraft>) =>
    onDraftChange({ ...draft, ...patch });
  const applySaved = (id: string) => {
    setSelectedSavedId(id);
    const saved = savedFilters.find((item) => item.saved_filter_id === id);
    if (saved) onApplySaved(saved);
  };

  const save = async () => {
    setSaving(true);
    try {
      await onSave(saveName);
      setSaveName("");
    } finally {
      setSaving(false);
    }
  };

  return (
    <aside className="filter-panel" aria-label="筛选条件">
      <div className="filter-panel-header">
        <h2 className="filter-panel-title">筛选条件</h2>
        <button
          className="text-button"
          onClick={() => void onReset()}
          type="button"
        >
          清空
        </button>
      </div>
      <div className="filter-scroll">
        <section className="filter-section">
          <div className="filter-section-heading">
            <h3>已保存的筛选</h3>
            <span>{savedFilters.length} 组</span>
          </div>
          <div className="saved-filter-row">
            <div className="select-wrap" style={{ flex: 1 }}>
              <label className="sr-only" htmlFor="saved-filter">
                选择已保存的筛选
              </label>
              <select
                className="select-field"
                id="saved-filter"
                onChange={(event) => applySaved(event.target.value)}
                value={selectedSavedId}
              >
                <option value="">选择一组筛选</option>
                {savedFilters.map((item) => (
                  <option
                    key={item.saved_filter_id}
                    value={item.saved_filter_id}
                  >
                    {item.name}
                  </option>
                ))}
              </select>
              <Icon name="chevron-down" size={16} />
            </div>
            {selectedSavedId && (
              <button
                aria-label="删除当前保存的筛选"
                className="icon-button"
                onClick={() => {
                  const item = savedFilters.find(
                    (filter) => filter.saved_filter_id === selectedSavedId,
                  );
                  if (!item) return;
                  void onDeleteSaved(item).then(() => setSelectedSavedId(""));
                }}
                type="button"
              >
                <Icon name="close" size={16} />
              </button>
            )}
          </div>
          <div className="saved-filter-row">
            <label className="sr-only" htmlFor="save-filter-name">
              筛选名称
            </label>
            <input
              className="field"
              id="save-filter-name"
              maxLength={120}
              onChange={(event) => setSaveName(event.target.value)}
              placeholder="为当前条件命名"
              value={saveName}
            />
            <button
              className="button"
              disabled={saving}
              onClick={() => void save()}
              type="button"
            >
              保存
            </button>
          </div>
        </section>

        <section className="filter-section">
          <div className="filter-section-heading">
            <h3>学历与院校</h3>
            <span>精确筛选</span>
          </div>
          <span className="field-label">最高学历</span>
          <div className="choice-grid" aria-label="学历条件">
            {filterOptions.degrees.map((option) => (
              <label className="choice-row" key={option.value}>
                <input
                  checked={draft.degrees.includes(option.value)}
                  onChange={() =>
                    update({
                      degrees: draft.degrees.includes(option.value)
                        ? draft.degrees.filter(
                            (degree) => degree !== option.value,
                          )
                        : [...draft.degrees, option.value],
                    })
                  }
                  type="checkbox"
                />
                {option.label}
              </label>
            ))}
          </div>
          <span className="field-label">院校层级</span>
          <div className="choice-grid" aria-label="院校层级条件">
            {filterOptions.institution_tiers.map((option) => (
              <label className="choice-row" key={option.value}>
                <input
                  checked={draft.institutionTiers.includes(option.value)}
                  onChange={() =>
                    update({
                      institutionTiers: draft.institutionTiers.includes(option.value)
                        ? draft.institutionTiers.filter((value) => value !== option.value)
                        : [...draft.institutionTiers, option.value],
                    })
                  }
                  type="checkbox"
                />
                {option.label}
              </label>
            ))}
          </div>
          <div className="field-stack">
            <span className="field-label">应届状态</span>
            <div className="choice-grid choice-grid-inline" role="radiogroup">
              {filterOptions.graduation_statuses.map((option) => (
                <label className="choice-row" key={option.value}>
                  <input
                    checked={draft.graduationStatus === option.value}
                    name="graduation-status"
                    onChange={() => update({ graduationStatus: option.value })}
                    type="radio"
                  />
                  {option.label}
                </label>
              ))}
            </div>
          </div>
          {draft.graduationStatus !== "any" && (
            <div className="filter-inline-fields">
              <label className="field-stack">
                <span className="field-label">应届窗口开始</span>
                <input
                  className="field"
                  onChange={(event) => update({ freshGraduateStartMonth: event.target.value })}
                  type="month"
                  value={draft.freshGraduateStartMonth}
                />
              </label>
              <label className="field-stack">
                <span className="field-label">应届窗口结束</span>
                <input
                  className="field"
                  onChange={(event) => update({ freshGraduateEndMonth: event.target.value })}
                  type="month"
                  value={draft.freshGraduateEndMonth}
                />
              </label>
            </div>
          )}
          <div className="field-stack">
            <label className="field-label" htmlFor="school-name">
              院校名称
            </label>
            <input
              className="field"
              id="school-name"
              onChange={(event) => update({ schoolName: event.target.value })}
              placeholder="可填全称或简称，例如：北大"
              value={draft.schoolName}
            />
          </div>
          <details className="advanced-filter">
            <summary>成绩、绩点与排名（非必选）</summary>
            <div className="filter-inline-fields">
              <label className="field-stack">
                <span className="field-label">最低平均成绩</span>
                <input
                  className="field"
                  max="100"
                  min="0"
                  onChange={(event) => update({ minAverageScore: event.target.value })}
                  placeholder="例如：85"
                  type="number"
                  value={draft.minAverageScore}
                />
              </label>
              <label className="field-stack">
                <span className="field-label">最低绩点百分比</span>
                <input
                  className="field"
                  max="100"
                  min="0"
                  onChange={(event) => update({ minGpaPercent: event.target.value })}
                  placeholder="例如：85"
                  type="number"
                  value={draft.minGpaPercent}
                />
              </label>
              <label className="field-stack">
                <span className="field-label">专业名次不低于</span>
                <input
                  className="field"
                  min="1"
                  onChange={(event) => update({ maxRankPosition: event.target.value })}
                  placeholder="例如：10（前 10 名）"
                  type="number"
                  value={draft.maxRankPosition}
                />
              </label>
              <label className="field-stack">
                <span className="field-label">排名前百分比</span>
                <input
                  className="field"
                  max="100"
                  min="1"
                  onChange={(event) => update({ maxRankPercent: event.target.value })}
                  placeholder="例如：10"
                  type="number"
                  value={draft.maxRankPercent}
                />
              </label>
            </div>
            <span className="field-hint">
              只匹配简历中有明确成绩、绩点或排名证据的同一条教育经历。
            </span>
          </details>
          <div className="field-stack">
            <label className="field-label" htmlFor="major-name">
              专业方向
            </label>
            <input
              className="field"
              id="major-name"
              onChange={(event) => update({ major: event.target.value })}
              placeholder="例如：计算机科学"
              value={draft.major}
            />
          </div>
        </section>

        <section className="filter-section">
          <div className="filter-section-heading">
            <h3>经历类别</h3>
            <span>按同一条经历匹配</span>
          </div>
          <div className="field-stack">
            <label className="field-label" htmlFor="min-experience">
              最低正式工作年限
            </label>
            <input
              className="range-input"
              id="min-experience"
              max="240"
              min="0"
              onChange={(event) =>
                update({
                  minEmploymentMonths: clampMonths(Number(event.target.value)),
                })
              }
              step="12"
              type="range"
              value={draft.minEmploymentMonths}
            />
            <div className="range-values">
              <span>不限</span>
              <span>20 年</span>
            </div>
          </div>
          <div className="field-stack">
            <label className="field-label" htmlFor="min-work-internship">
              最低工作 + 实习年限
            </label>
            <input
              className="range-input"
              id="min-work-internship"
              max="240"
              min="0"
              onChange={(event) =>
                update({
                  minEmploymentOrInternshipMonths: clampMonths(
                    Number(event.target.value),
                  ),
                })
              }
              step="12"
              type="range"
              value={draft.minEmploymentOrInternshipMonths}
            />
            <div className="range-values">
              <span>{formatMonths(draft.minEmploymentOrInternshipMonths)}</span>
              <span>20 年</span>
            </div>
          </div>
          <div className="field-stack">
            <span className="field-label">经历类型</span>
            <div className="choice-grid" aria-label="经历类型条件">
              {filterOptions.experience_types.map((option) => (
                <label className="choice-row" key={option.value}>
                  <input
                    checked={draft.experienceTypes.includes(option.value)}
                    onChange={() =>
                      update({
                        experienceTypes: draft.experienceTypes.includes(
                          option.value,
                        )
                          ? draft.experienceTypes.filter(
                              (value) => value !== option.value,
                            )
                          : [...draft.experienceTypes, option.value],
                      })
                    }
                    type="checkbox"
                  />
                  {option.label}
                </label>
              ))}
            </div>
            <span className="field-hint">不选则不限经历类型。</span>
          </div>
          <div className="field-stack">
            <label className="field-label" htmlFor="experience-name">
              项目 / 竞赛 / 经历名称
            </label>
            <input
              className="field"
              id="experience-name"
              onChange={(event) => update({ experienceName: event.target.value })}
              placeholder="例如：全国大学生数学建模竞赛"
              value={draft.experienceName}
            />
          </div>
          <div className="field-stack">
            <label className="field-label" htmlFor="company-name">
              公司 / 组织
            </label>
            <input
              className="field"
              id="company-name"
              onChange={(event) => update({ company: event.target.value })}
              placeholder="例如：字节跳动"
              value={draft.company}
            />
          </div>
          <div className="field-stack">
            <label className="field-label" htmlFor="role-name">
              职位名称
            </label>
            <input
              className="field"
              id="role-name"
              onChange={(event) => update({ title: event.target.value })}
              placeholder="例如：后端工程师"
              value={draft.title}
            />
          </div>
          <details className="advanced-filter">
            <summary>经历获奖情况（非必选）</summary>
            <div className="choice-grid">
              {filterOptions.award_levels.map((option) => (
                <label className="choice-row" key={option.value}>
                  <input
                    checked={draft.experienceAwardLevels.includes(option.value)}
                    onChange={() =>
                      update({
                        experienceAwardLevels: draft.experienceAwardLevels.includes(option.value)
                          ? draft.experienceAwardLevels.filter((value) => value !== option.value)
                          : [...draft.experienceAwardLevels, option.value],
                      })
                    }
                    type="checkbox"
                  />
                  {option.label}
                </label>
              ))}
            </div>
            <input
              className="field"
              onChange={(event) => update({ experienceAwardResult: event.target.value })}
              placeholder="获奖结果，例如：一等奖"
              value={draft.experienceAwardResult}
            />
          </details>
        </section>

        <section className="filter-section">
          <div className="filter-section-heading">
            <h3>技能</h3>
            <span>支持全部或任一</span>
          </div>
          <div className="field-stack">
            <span className="field-label">技能匹配方式</span>
            <div className="choice-grid choice-grid-inline" role="radiogroup">
              {(
                [
                  ["all", "全部具备"],
                  ["any", "任一具备"],
                ] as Array<[MatchMode, string]>
              ).map(([value, label]) => (
                <label className="choice-row" key={value}>
                  <input
                    checked={draft.skillsMode === value}
                    name="skills-match-mode"
                    onChange={() => update({ skillsMode: value })}
                    type="radio"
                  />
                  {label}
                </label>
              ))}
            </div>
          </div>
          <span className="field-label">技能分类（非必选）</span>
          <div className="choice-grid">
            {filterOptions.skill_categories.map((option) => (
              <label className="choice-row" key={option.value}>
                <input
                  checked={draft.skillCategories.includes(option.value)}
                  onChange={() =>
                    update({
                      skillCategories: draft.skillCategories.includes(option.value)
                        ? draft.skillCategories.filter((value) => value !== option.value)
                        : [...draft.skillCategories, option.value],
                    })
                  }
                  type="checkbox"
                />
                {option.label}
              </label>
            ))}
          </div>
          <ChipInput
            label="核心技能"
            onChange={(skills) => update({ skills })}
            placeholder="输入技能后按 Enter"
            values={draft.skills}
          />
        </section>

        <section className="filter-section">
          <div className="filter-section-heading">
            <h3>英语能力</h3>
            <span>证书之间按 OR</span>
          </div>
          <div className="credential-list">
            {filterOptions.language_credentials.map((option) => {
              const selected = draft.languageCredentials.includes(option.value);
              return (
                <div className="credential-row" key={option.value}>
                  <label className="choice-row">
                    <input
                      checked={selected}
                      onChange={() =>
                        update({
                          languageCredentials: selected
                            ? draft.languageCredentials.filter((value) => value !== option.value)
                            : [...draft.languageCredentials, option.value],
                        })
                      }
                      type="checkbox"
                    />
                    {option.label}
                  </label>
                  {selected && option.value !== "custom" && (
                    <input
                      aria-label={`${option.label}最低分`}
                      className="field score-field"
                      min="0"
                      onChange={(event) =>
                        update({
                          languageScores: {
                            ...draft.languageScores,
                            [option.value]: event.target.value,
                          },
                        })
                      }
                      placeholder="最低分（可选）"
                      type="number"
                      value={draft.languageScores[option.value] ?? ""}
                    />
                  )}
                </div>
              );
            })}
          </div>
          {draft.languageCredentials.includes("custom") && (
            <input
              className="field"
              onChange={(event) => update({ customLanguageName: event.target.value })}
              placeholder="填写英语证书名称"
              value={draft.customLanguageName}
            />
          )}
          <span className="field-hint">
            “四级、英语四级、CET4、CET-4”等写法均匹配大学英语四级（CET-4）。
          </span>
        </section>

        <section className="filter-section">
          <div className="filter-section-heading">
            <h3>奖学金与竞赛</h3>
            <span>均为非必选</span>
          </div>
          <PresenceRadio
            label="奖学金"
            name="scholarship-status"
            options={filterOptions.presence_statuses}
            value={draft.scholarshipStatus}
            onChange={(scholarshipStatus) => update({ scholarshipStatus })}
          />
          {draft.scholarshipStatus === "present" && (
            <div className="field-stack">
              <div className="choice-grid">
                {filterOptions.scholarship_levels.map((option) => (
                  <label className="choice-row" key={option.value}>
                    <input
                      checked={draft.scholarshipLevels.includes(option.value)}
                      onChange={() =>
                        update({
                          scholarshipLevels: draft.scholarshipLevels.includes(option.value)
                            ? draft.scholarshipLevels.filter((value) => value !== option.value)
                            : [...draft.scholarshipLevels, option.value],
                        })
                      }
                      type="checkbox"
                    />
                    {option.label}
                  </label>
                ))}
              </div>
              <input
                className="field"
                onChange={(event) => update({ scholarshipName: event.target.value })}
                placeholder="奖学金名称（可选）"
                value={draft.scholarshipName}
              />
            </div>
          )}
          <PresenceRadio
            label="技能竞赛参赛记录"
            name="competition-status"
            options={filterOptions.presence_statuses}
            value={draft.competitionStatus}
            onChange={(competitionStatus) => update({ competitionStatus })}
          />
          <PresenceRadio
            label="技能竞赛获奖记录"
            name="competition-award-status"
            options={filterOptions.presence_statuses}
            value={draft.competitionAwardStatus}
            onChange={(competitionAwardStatus) => update({ competitionAwardStatus })}
          />
        </section>

        <section className="filter-section">
          <div className="filter-section-heading">
            <h3>管理与领导经历</h3>
            <span>非必选</span>
          </div>
          <div className="choice-grid">
            {filterOptions.leadership_contexts.map((option) => (
              <label className="choice-row" key={option.value}>
                <input
                  checked={draft.leadershipContexts.includes(option.value)}
                  onChange={() =>
                    update({
                      leadershipContexts: draft.leadershipContexts.includes(option.value)
                        ? draft.leadershipContexts.filter((item) => item !== option.value)
                        : [...draft.leadershipContexts, option.value],
                    })
                  }
                  type="checkbox"
                />
                {option.label}
              </label>
            ))}
          </div>
          <ChipInput
            label="角色名称"
            onChange={(leadershipRoles) => update({ leadershipRoles })}
            placeholder="例如：班干部、组长、主管、经理"
            values={draft.leadershipRoles}
          />
        </section>

        <section className="filter-section">
          <div className="filter-section-heading">
            <h3>自定义关键词</h3>
            <span>泛匹配或精准匹配</span>
          </div>
          <div className="field-stack">
            <span className="field-label">关键词匹配方式</span>
            <div className="choice-grid choice-grid-inline" role="radiogroup">
              {filterOptions.keyword_modes.map((option) => (
                <label className="choice-row" key={option.value}>
                  <input
                    checked={draft.keywordsMode === option.value}
                    name="keywords-match-mode"
                    onChange={() => update({ keywordsMode: option.value })}
                    type="radio"
                  />
                  {option.label}
                </label>
              ))}
            </div>
          </div>
          <ChipInput
            label="补充关键词"
            onChange={(keywords) => update({ keywords })}
            placeholder="输入关键词后按 Enter"
            values={draft.keywords}
          />
        </section>
      </div>
      <div className="filter-actions">
        <button
          className="button button-primary"
          onClick={() => void onApply()}
          type="button"
        >
          <Icon name="filter" size={16} />
          应用筛选条件
        </button>
        <button
          className="button button-ghost"
          onClick={() => void onReset()}
          type="button"
        >
          恢复默认条件
        </button>
      </div>
    </aside>
  );
}

function PresenceRadio({
  label,
  name,
  options,
  value,
  onChange,
}: {
  label: string;
  name: string;
  options: FilterOptions["presence_statuses"];
  value: PresenceStatus;
  onChange: (value: PresenceStatus) => void;
}) {
  return (
    <div className="field-stack">
      <span className="field-label">{label}</span>
      <div className="choice-grid choice-grid-inline" role="radiogroup">
        {options.map((option) => (
          <label className="choice-row" key={option.value}>
            <input
              checked={value === option.value}
              name={name}
              onChange={() => onChange(option.value)}
              type="radio"
            />
            {option.label}
          </label>
        ))}
      </div>
    </div>
  );
}

function ChipInput({
  label,
  values,
  onChange,
  placeholder,
}: {
  label: string;
  values: string[];
  onChange: (values: string[]) => void;
  placeholder: string;
}) {
  const [value, setValue] = useState("");
  const add = () => {
    const normalized = value.trim();
    if (
      !normalized ||
      values.some(
        (item) => item.toLocaleLowerCase() === normalized.toLocaleLowerCase(),
      )
    )
      return;
    onChange([...values, normalized]);
    setValue("");
  };
  return (
    <div className="field-stack">
      <label className="field-label">{label}</label>
      <div className="chip-input">
        {values.map((item) => (
          <span className="filter-chip" key={item}>
            {item}
            <button
              aria-label={`移除 ${item}`}
              onClick={() =>
                onChange(values.filter((valueItem) => valueItem !== item))
              }
              type="button"
            >
              <Icon name="close" size={12} />
            </button>
          </span>
        ))}
        <input
          onBlur={add}
          onChange={(event) => setValue(event.target.value)}
          onKeyDown={(event) => {
            if (
              event.key === "Enter" ||
              event.key === "," ||
              event.key === "，"
            ) {
              event.preventDefault();
              add();
            }
          }}
          placeholder={values.length ? "继续添加" : placeholder}
          value={value}
        />
      </div>
    </div>
  );
}

function ResultsPane({
  search,
  searching,
  selectedResumeId,
  onOpenCandidate,
  onLoadMore,
  onUpload,
}: {
  search: CandidateSearchResponse;
  searching: boolean;
  selectedResumeId: string | null;
  onOpenCandidate: (item: CandidateSearchItem, tab?: DrawerTab) => void;
  onLoadMore: () => void;
  onUpload: () => void;
}) {
  return (
    <section className="results-pane" aria-label="候选人结果">
      <header className="results-header">
        <div className="results-summary">
          <h1>候选人结果</h1>
          <p>
            {search.items.length
              ? `当前已加载 ${search.items.length} 位候选人`
              : "仅显示已完成 AI 提取并启用的简历"}
          </p>
        </div>
        <div className="results-toolbar">
          {search.needs_review_count > 0 && (
            <span className="status-pill">
              待处理 {search.needs_review_count}
            </span>
          )}
          <button className="button" onClick={onUpload} type="button">
            <Icon name="upload" size={16} />
            上传简历
          </button>
        </div>
      </header>
      <div className="table-scroll">
        {searching && !search.items.length ? (
          <TableSkeleton />
        ) : search.items.length ? (
          <table className="candidate-table">
            <thead>
              <tr>
                <th scope="col">候选人</th>
                <th scope="col">985 / 211</th>
                <th scope="col">最高学历</th>
                <th scope="col">正式工作年限</th>
                <th scope="col">AI 总结</th>
                <th scope="col">最近评分</th>
                <th scope="col">命中证据</th>
                <th scope="col">原件</th>
                <th scope="col" aria-label="查看详情" />
              </tr>
            </thead>
            <tbody>
              {search.items.map((item) => (
                <tr
                  aria-label={`打开 ${item.display_name ?? "未命名候选人"} 的简历详情`}
                  className={
                    selectedResumeId === item.resume_id ? "is-selected" : ""
                  }
                  key={item.resume_id}
                  onClick={() => onOpenCandidate(item)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" || event.key === " ") {
                      event.preventDefault();
                      onOpenCandidate(item);
                    }
                  }}
                  tabIndex={0}
                >
                  <td>
                    <div className="candidate-person">
                      <span className="candidate-name">
                        {item.display_name?.trim() || "未命名候选人"}
                      </span>
                      <span className="candidate-meta">
                        {item.original_filename}
                      </span>
                    </div>
                  </td>
                  <td>
                    {item.is_985_211 ? (
                      <span className="school-mark">
                        <Icon name="check" size={13} />是
                      </span>
                    ) : (
                      <span className="candidate-meta">否</span>
                    )}
                  </td>
                  <td>
                    <span className="degree-label">
                      {item.highest_degree
                        ? degreeLabels[item.highest_degree]
                        : "未知"}
                    </span>
                  </td>
                  <td>{formatMonths(item.employment_months)}</td>
                  <td className="library-summary-cell">
                    {item.summary_preview ? (
                      <p
                        className="library-summary-preview"
                        title={item.summary_preview}
                      >
                        {item.summary_preview}
                      </p>
                    ) : (
                      <span className="library-empty-copy">尚未生成</span>
                    )}
                  </td>
                  <td>
                    {item.score_total !== null ? (
                      <div
                        className="library-score"
                        title={item.score_template_name ?? undefined}
                      >
                        <strong>{item.score_total.toFixed(1)}</strong>
                        <span>/ 100</span>
                        {item.score_template_name && (
                          <small>{item.score_template_name}</small>
                        )}
                      </div>
                    ) : (
                      <span className="library-empty-copy">尚未评分</span>
                    )}
                  </td>
                  <td>
                    <div className="match-tags">
                      {item.matched_evidence.length ? (
                        item.matched_evidence
                          .slice(0, 3)
                          .map((evidence, index) => (
                            <span
                              className="tag"
                              key={`${evidence.filter_key}-${index}`}
                              title={evidence.label}
                            >
                              {evidence.label}
                            </span>
                          ))
                      ) : (
                        <span className="candidate-meta">无附加条件</span>
                      )}
                    </div>
                  </td>
                  <td>
                    <button
                      aria-label={`查看 ${item.display_name ?? "候选人"} 的原始文件`}
                      className="button button-ghost match-open-button"
                      onClick={(event) => {
                        event.stopPropagation();
                        onOpenCandidate(item, "original");
                      }}
                      type="button"
                    >
                      <Icon name="document" size={15} />
                      原件
                    </button>
                  </td>
                  <td>
                    <Icon name="chevron-right" size={18} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <div className="empty-state">
            <div className="empty-state-inner">
              <span className="empty-glyph">
                <Icon name="search" size={24} />
              </span>
              <h2>没有符合条件的已启用简历</h2>
              <p>
                调整筛选条件，或上传一份简历。AI
                提取完成后，它会自动进入筛选库。
              </p>
              <button
                className="button button-primary"
                onClick={onUpload}
                type="button"
              >
                <Icon name="upload" size={16} />
                上传简历
              </button>
            </div>
          </div>
        )}
      </div>
      <footer className="results-footer">
        <span>
          {searching ? (
            <span className="loading-line">
              <i className="spinner" />
              正在查询候选人…
            </span>
          ) : (
            `${search.items.length} 位候选人`
          )}
        </span>
        {search.next_cursor && (
          <button
            className="button button-ghost"
            disabled={searching}
            onClick={onLoadMore}
            type="button"
          >
            加载更多 <Icon name="arrow-right" size={16} />
          </button>
        )}
      </footer>
    </section>
  );
}

const RESUME_LIBRARY_PAGE_SIZE = 50;

function resumeLibraryStatus(item: ResumeLibraryItem): {
  label: string;
  tone: "ready" | "progress" | "attention" | "waiting";
} {
  if (hasSourceTextQualityIssue(item.quality_flags)) {
    return { label: "文本待校正", tone: "attention" };
  }
  if (hasSupersededReparseVersion(item.quality_flags)) {
    return { label: "当前版本已更新", tone: "attention" };
  }
  if (item.is_active && item.extraction_status === "ready") {
    return { label: "已启用", tone: "ready" };
  }
  if (item.ai_extraction_status === "running") {
    return { label: "AI 提取中", tone: "progress" };
  }
  if (item.ai_extraction_status === "queued") {
    return { label: "等待 AI 提取", tone: "waiting" };
  }
  if (
    item.ai_extraction_status === "needs_attention" ||
    item.extraction_status === "failed"
  ) {
    return { label: "需要处理", tone: "attention" };
  }
  if (item.ai_extraction_status === "unavailable") {
    return { label: "等待 AI 服务", tone: "attention" };
  }
  return { label: "等待启用", tone: "waiting" };
}

function ResumeLibraryPage({
  selectedResumeId,
  refreshToken,
  onOpenResume,
  onScoreResume,
  onUpload,
}: {
  selectedResumeId: string | null;
  refreshToken: number;
  onOpenResume: (item: ResumeLibraryItem) => void;
  onScoreResume: (item: ResumeLibraryItem) => void;
  onUpload: () => void;
}) {
  const [library, setLibrary] = useState<ResumeLibraryResponse | null>(null);
  const [mailboxSources, setMailboxSources] = useState<MailboxConfig[]>([]);
  const [sourceMailboxId, setSourceMailboxId] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const loadLibrary = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setLibrary(
        await api.listResumeLibrary(
          page,
          RESUME_LIBRARY_PAGE_SIZE,
          sourceMailboxId,
        ),
      );
    } catch (loadError) {
      setError(humanizeError(loadError));
    } finally {
      setLoading(false);
    }
  }, [page, sourceMailboxId]);

  useEffect(() => {
    void loadLibrary();
  }, [loadLibrary, refreshToken]);

  useEffect(() => {
    let cancelled = false;
    void api.listMailboxConfigs(true)
      .then((response) => {
        if (!cancelled) setMailboxSources(response.items);
      })
      // The library remains usable when the current plan cannot use mailbox
      // ingestion. In that case there is simply no source-specific filter.
      .catch(() => undefined);
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    if (
      !library?.items.some((item) =>
        aiExtractionIsInProgress(item.ai_extraction_status),
      )
    )
      return undefined;
    const interval = window.setInterval(() => {
      void loadLibrary();
    }, AI_STATUS_POLL_INTERVAL_MS);
    return () => window.clearInterval(interval);
  }, [library, loadLibrary]);

  const items = library?.items ?? [];
  const total = library?.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / RESUME_LIBRARY_PAGE_SIZE));
  const canPageBack = page > 1;
  const canPageForward = page < totalPages;

  return (
    <div className="page-frame resume-library-page">
      <header className="page-heading">
        <div>
          <h1>简历库</h1>
          <p>
            每份已上传的 PDF 都会保留在这里；查看 AI 总结、AI 评分和原始简历。
          </p>
        </div>
        <div className="resume-library-actions">
          {mailboxSources.length ? (
            <div className="resume-library-source-filter">
              <label className="sr-only" htmlFor="resume-library-source">
                按收件通道筛选
              </label>
              <div className="select-wrap">
                <select
                  aria-label="按收件通道筛选"
                  className="select-field"
                  id="resume-library-source"
                  onChange={(event) => {
                    setPage(1);
                    setSourceMailboxId(event.target.value || null);
                  }}
                  value={sourceMailboxId ?? ""}
                >
                  <option value="">全部来源</option>
                  {mailboxSources.map((mailbox) => (
                    <option key={mailbox.mailbox_id} value={mailbox.mailbox_id}>
                      {mailbox.archived_at
                        ? `${mailbox.display_name}（已归档）`
                        : mailbox.display_name}
                    </option>
                  ))}
                </select>
                <Icon name="chevron-down" size={16} />
              </div>
            </div>
          ) : null}
          <button
            className="button"
            disabled={loading}
            onClick={() => void loadLibrary()}
            type="button"
          >
            {loading ? (
              <i className="spinner" />
            ) : (
              <Icon name="refresh" size={16} />
            )}
            刷新
          </button>
          <button
            className="button button-primary"
            onClick={onUpload}
            type="button"
          >
            <Icon name="upload" size={16} />
            上传简历
          </button>
        </div>
      </header>

      {error && (
        <p className="library-error" role="status">
          {error}
        </p>
      )}

      <section aria-label="简历库列表" className="library-table-frame">
        {loading && !library ? (
          <TableSkeleton />
        ) : items.length ? (
          <div className="table-scroll">
            <table className="candidate-table library-table">
              <thead>
                <tr>
                  <th scope="col">候选人 / 原始文件</th>
                  <th scope="col">AI 总结</th>
                  <th scope="col">AI 评分</th>
                  <th scope="col">状态</th>
                  <th scope="col">上传时间</th>
                  <th aria-label="查看简历" scope="col" />
                </tr>
              </thead>
              <tbody>
                {items.map((item) => {
                  const status = resumeLibraryStatus(item);
                  const sourceTextIssue = hasSourceTextQualityIssue(
                    item.quality_flags,
                  );
                  const supersededReparse = hasSupersededReparseVersion(
                    item.quality_flags,
                  );
                  return (
                    <tr
                      aria-label={`打开 ${item.display_name?.trim() || "未命名候选人"} 的 AI 总结和原始简历`}
                      className={[
                        selectedResumeId === item.resume_id ? "is-selected" : "",
                        sourceTextIssue ? "has-source-quality-issue" : "",
                        supersededReparse ? "has-superseded-reparse" : "",
                      ]
                        .filter(Boolean)
                        .join(" ")}
                      key={item.resume_id}
                      onClick={() => onOpenResume(item)}
                      onKeyDown={(event) => {
                        if (event.key === "Enter" || event.key === " ") {
                          event.preventDefault();
                          onOpenResume(item);
                        }
                      }}
                      tabIndex={0}
                    >
                      <td>
                        <div className="candidate-person">
                          <span className="candidate-name">
                            {item.display_name?.trim() || "未命名候选人"}
                          </span>
                          <span
                            className="candidate-meta"
                            title={item.original_filename}
                          >
                            {item.original_filename}
                          </span>
                          <span className="candidate-meta library-source-label">
                            {item.source_mailbox_label
                              ? `邮箱 · ${item.source_mailbox_label}`
                              : "手动上传"}
                          </span>
                        </div>
                      </td>
                      <td className="library-summary-cell">
                        {sourceTextIssue ? (
                          <span className="library-quality-copy">
                            提取文本疑似乱码，暂不展示 AI 总结。
                          </span>
                        ) : supersededReparse ? (
                          <span className="library-quality-copy">
                            此解析版本已过期，不展示旧结论。
                          </span>
                        ) : item.summary_preview ? (
                          <p
                            className="library-summary-preview"
                            title={item.summary_preview}
                          >
                            {item.summary_preview}
                          </p>
                        ) : (
                          <span className="library-empty-copy">
                            {item.is_active
                              ? "尚未生成，打开后可生成"
                              : "完成提取后可生成"}
                          </span>
                        )}
                      </td>
                      <td>
                        {sourceTextIssue ? (
                          <span className="library-quality-copy">
                            请先打开并重新解析
                          </span>
                        ) : supersededReparse ? (
                          <span className="library-quality-copy">
                            请使用候选人的当前版本
                          </span>
                        ) : item.score_total !== null ? (
                          <div
                            className="library-score"
                            title={item.score_template_name ?? undefined}
                          >
                            <strong>{item.score_total.toFixed(1)}</strong>
                            <span>/ 100</span>
                            {item.score_template_name && (
                              <small>{item.score_template_name}</small>
                            )}
                          </div>
                        ) : item.is_active ? (
                          <button
                            className="text-button library-score-action"
                            onClick={(event) => {
                              event.stopPropagation();
                              onScoreResume(item);
                            }}
                            type="button"
                          >
                            去评分 <Icon name="arrow-right" size={14} />
                          </button>
                        ) : (
                          <span className="library-empty-copy">
                            完成提取后可评分
                          </span>
                        )}
                      </td>
                      <td>
                        <span
                          className={`library-status is-${status.tone}`}
                          title={
                            sourceTextIssue
                              ? "提取文本疑似乱码，请先重新解析原件。"
                              : supersededReparse
                                ? "候选人已有更新版本，此解析版本不会被启用。"
                                : item.ai_extraction_error ?? undefined
                          }
                        >
                          {status.label}
                        </span>
                      </td>
                      <td>
                        <span className="candidate-meta">
                          {formatLibraryDate(item.created_at)}
                        </span>
                      </td>
                      <td>
                        <Icon name="chevron-right" size={18} />
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="empty-state">
            <div className="empty-state-inner">
              <span className="empty-glyph">
                <Icon name="folder" size={24} />
              </span>
              <h2>简历库还是空的</h2>
              <p>
                上传简历后，它会立即出现在这里；AI 提取、总结和评分会逐步更新。
              </p>
              <button
                className="button button-primary"
                onClick={onUpload}
                type="button"
              >
                <Icon name="upload" size={16} />
                上传简历
              </button>
            </div>
          </div>
        )}
      </section>

      <footer className="library-table-footer">
        <span>
          {loading && library ? (
            <span className="loading-line">
              <i className="spinner" />
              正在更新简历库…
            </span>
          ) : (
            `共 ${total} 份简历`
          )}
        </span>
        {totalPages > 1 && (
          <div className="pagination">
            <button
              className="button button-ghost"
              disabled={!canPageBack || loading}
              onClick={() => setPage((current) => current - 1)}
              type="button"
            >
              上一页
            </button>
            <span>
              第 {page} / {totalPages} 页
            </span>
            <button
              className="button button-ghost"
              disabled={!canPageForward || loading}
              onClick={() => setPage((current) => current + 1)}
              type="button"
            >
              下一页
            </button>
          </div>
        )}
      </footer>
    </div>
  );
}

function TableSkeleton() {
  return (
    <div className="empty-state">
      <div className="empty-state-inner">
        <div
          className="skeleton"
          style={{ width: "3.75rem", height: "3.75rem", borderRadius: "50%" }}
        />
        <div className="skeleton" style={{ width: "13rem", height: "1rem" }} />
        <div
          className="skeleton"
          style={{ width: "18rem", height: "0.875rem" }}
        />
      </div>
    </div>
  );
}

function CandidateDrawer({
  candidate,
  review,
  reviewLoading,
  isOpen,
  drawerTab,
  onTabChange,
  onClose,
  pdfUrl,
  pdfLoading,
  pdfError,
  summaries,
  summaryLoading,
  onGenerateSummary,
  onCreateManualSummary,
  onReparseSource,
  reparsingSource,
  onEnrichFacts,
  enrichingFacts,
}: {
  candidate: SelectedResume | null;
  review: ResumeReviewDetail | null;
  reviewLoading: boolean;
  isOpen: boolean;
  drawerTab: DrawerTab;
  onTabChange: (tab: DrawerTab) => void;
  onClose: () => void;
  pdfUrl: string | null;
  pdfLoading: boolean;
  pdfError: string | null;
  summaries: ResumeSummary[];
  summaryLoading: boolean;
  onGenerateSummary: () => void;
  onCreateManualSummary: (
    summaryId: string,
    content: Record<string, string>,
  ) => Promise<void>;
  onReparseSource: () => void;
  reparsingSource: boolean;
  onEnrichFacts: () => void;
  enrichingFacts: boolean;
}) {
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const currentSummary =
    summaries.find((item) => item.is_current) ?? summaries[0] ?? null;
  const sourceTextIssue = hasSourceTextQualityIssue(review?.quality_flags);
  const supersededReparse = hasSupersededReparseVersion(review?.quality_flags);
  useEffect(() => {
    if (!isOpen) return;
    const frame = window.requestAnimationFrame(() =>
      closeButtonRef.current?.focus(),
    );
    return () => window.cancelAnimationFrame(frame);
  }, [isOpen]);
  const downloadPdf = () => {
    if (!pdfUrl || !review) return;
    const link = document.createElement("a");
    link.href = pdfUrl;
    link.download = review.original_filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
  };
  return (
    <aside
      aria-label={
        candidate ? `${candidate.candidateName} 的简历详情` : "简历详情"
      }
      aria-modal="true"
      className={`candidate-drawer${isOpen ? " is-open" : ""}${sourceTextIssue ? " has-source-quality-notice" : ""}`}
      inert={!isOpen}
      role="dialog"
    >
      <header className="drawer-header">
        <div className="drawer-title-wrap">
          <h2>
            {candidate?.candidateName ?? "候选人详情"}
            {sourceTextIssue ? (
              <span className="tiny-badge is-attention">文本待校正</span>
            ) : supersededReparse ? (
              <span className="tiny-badge is-attention">当前版本已更新</span>
            ) : review?.is_active ? (
              <span className="tiny-badge">已启用</span>
            ) : null}
          </h2>
          <p>{review ? review.original_filename : "正在读取简历详情…"}</p>
        </div>
        <div className="drawer-actions">
          {pdfUrl && drawerTab === "original" && (
            <button className="button" onClick={downloadPdf} type="button">
              <Icon name="download" size={16} />
              下载原始文件
            </button>
          )}
          <button
            aria-label="关闭简历详情"
            className="icon-button"
            onClick={onClose}
            ref={closeButtonRef}
            type="button"
          >
            <Icon name="close" size={19} />
          </button>
        </div>
      </header>
      {sourceTextIssue && (
        <SourceTextQualityNotice
          busy={reparsingSource}
          onReparse={onReparseSource}
        />
      )}
      {supersededReparse && !sourceTextIssue && <SupersededReparseNotice />}
      <div className="drawer-body">
        <div aria-label="详情标签" className="tabs" role="tablist">
          {(
            [
              ["original", "原始文件"],
              ["summary", "AI 总结"],
              ["evidence", "提取依据"],
            ] as Array<[DrawerTab, string]>
          ).map(([tab, label]) => (
            <button
              aria-selected={drawerTab === tab}
              className={`tab${drawerTab === tab ? " is-active" : ""}`}
              key={tab}
              onClick={() => onTabChange(tab)}
              role="tab"
              type="button"
            >
              {label}
            </button>
          ))}
        </div>
        <div className="drawer-content">
          {reviewLoading && !review ? (
            <TableSkeleton />
          ) : drawerTab === "original" ? (
            <OriginalDocumentTab
              error={pdfError}
              loading={pdfLoading}
              pdfUrl={pdfUrl}
              review={review}
            />
          ) : drawerTab === "summary" ? (
            sourceTextIssue ? (
              <SourceTextQualityBlockedSummary
                busy={reparsingSource}
                onOpenEvidence={() => onTabChange("evidence")}
                onReparse={onReparseSource}
              />
            ) : supersededReparse ? (
              <SupersededReparseBlockedSummary
                onOpenEvidence={() => onTabChange("evidence")}
              />
            ) : (
              <DrawerSummary
                currentSummary={currentSummary}
                loading={summaryLoading}
                onCreateManual={onCreateManualSummary}
                onGenerate={onGenerateSummary}
                onOpenEvidence={() => onTabChange("evidence")}
                summaries={summaries}
              />
            )
          ) : (
            <EvidenceTab
              enriching={enrichingFacts}
              loading={reviewLoading}
              onEnrich={onEnrichFacts}
              review={review}
            />
          )}
        </div>
      </div>
    </aside>
  );
}

function SourceTextQualityNotice({
  busy,
  onReparse,
}: {
  busy: boolean;
  onReparse: () => void;
}) {
  return (
    <section className="source-quality-notice" role="alert">
      <span aria-hidden="true" className="source-quality-notice-icon">
        <Icon name="document" size={18} />
      </span>
      <div className="source-quality-notice-copy">
        <strong>提取文本疑似乱码</strong>
        <p>
          当前版本的 AI 总结、评分和 JD 匹配不应作为筛选依据。请从原件创建新的解析版本，旧版本会保留供追溯。
        </p>
      </div>
      <button
        className="button source-quality-reparse-button"
        disabled={busy}
        onClick={onReparse}
        type="button"
      >
        {busy ? (
          <>
            <i className="spinner" />正在创建
          </>
        ) : (
          <>
            <Icon name="refresh" size={15} />重新解析为新版本
          </>
        )}
      </button>
    </section>
  );
}

function SourceTextQualityBlockedSummary({
  busy,
  onOpenEvidence,
  onReparse,
}: {
  busy: boolean;
  onOpenEvidence: () => void;
  onReparse: () => void;
}) {
  return (
    <div className="empty-state source-quality-blocked-summary">
      <div className="empty-state-inner">
        <span className="empty-glyph">
          <Icon name="document" size={23} />
        </span>
        <h2>AI 总结已暂停展示</h2>
        <p>
          这份简历的提取文本疑似乱码。为避免误导，本版本的 AI 结论不会在这里展示。
        </p>
        <div className="source-quality-summary-actions">
          <button
            className="button button-primary"
            disabled={busy}
            onClick={onReparse}
            type="button"
          >
            {busy ? (
              <>
                <i className="spinner" />正在创建
              </>
            ) : (
              <>
                <Icon name="refresh" size={16} />重新解析为新版本
              </>
            )}
          </button>
          <button className="button button-ghost" onClick={onOpenEvidence} type="button">
            查看提取依据
          </button>
        </div>
      </div>
    </div>
  );
}

function SupersededReparseNotice() {
  return (
    <section className="source-quality-notice source-quality-notice-stale" role="status">
      <span aria-hidden="true" className="source-quality-notice-icon">
        <Icon name="history" size={18} />
      </span>
      <div className="source-quality-notice-copy">
        <strong>当前版本已更新</strong>
        <p>
          这份重新解析版本完成前，候选人已有更新版本。系统没有让旧任务覆盖当前版本，请从候选人的当前版本继续处理。
        </p>
      </div>
    </section>
  );
}

function SupersededReparseBlockedSummary({
  onOpenEvidence,
}: {
  onOpenEvidence: () => void;
}) {
  return (
    <div className="empty-state source-quality-blocked-summary">
      <div className="empty-state-inner">
        <span className="empty-glyph">
          <Icon name="history" size={23} />
        </span>
        <h2>此解析版本未启用</h2>
        <p>
          候选人已有更新版本。为避免旧解析结果覆盖当前版本，本版本的 AI 结论不会在这里展示。
        </p>
        <div className="source-quality-summary-actions">
          <button className="button button-ghost" onClick={onOpenEvidence} type="button">
            查看提取依据
          </button>
        </div>
      </div>
    </div>
  );
}

function OriginalDocumentTab({
  review,
  pdfUrl,
  loading,
  error,
}: {
  review: ResumeReviewDetail | null;
  pdfUrl: string | null;
  loading: boolean;
  error: string | null;
}) {
  const pageCount = Math.min(review?.source_page_count ?? 1, 4);
  const filename = review?.original_filename ?? "";
  const canPreview = canPreviewInline(filename);
  const isImage = [".png", ".jpg", ".jpeg"].includes(
    resumeFileExtension(filename),
  );
  return (
    <div className="pdf-viewer">
      <div className="pdf-thumbnails" aria-label="原始文件页码">
        {Array.from({ length: pageCount }, (_, index) => (
          <div
            className={`pdf-thumb${index === 0 ? " is-current" : ""}`}
            key={index}
          >
            {index + 1}
          </div>
        ))}
      </div>
      <div className="pdf-canvas">
        {loading ? (
          <div className="pdf-loading">
            <span className="loading-line">
              <i className="spinner" />
              正在载入受保护的原始文件…
            </span>
          </div>
        ) : error ? (
          <div className="empty-state">
            <div className="empty-state-inner">
              <span className="empty-glyph">
                <Icon name="document" size={23} />
              </span>
              <h2>无法载入原始文件</h2>
              <p>{error}</p>
            </div>
          </div>
        ) : pdfUrl && canPreview ? (
          isImage ? (
            <img
              alt={filename ? `${filename} 原始图片` : "原始图片"}
              className="original-image-preview"
              src={pdfUrl}
            />
          ) : (
            <iframe
              sandbox={resumeFileExtension(filename) === ".html" || resumeFileExtension(filename) === ".htm" ? "" : undefined}
              src={pdfUrl}
              title={filename ? `${filename} 原始文件` : "原始文件"}
            />
          )
        ) : pdfUrl ? (
          <div className="empty-state">
            <div className="empty-state-inner">
              <span className="empty-glyph"><Icon name="document" size={23} /></span>
              <h2>{resumeFileTypeLabel(filename)} 原件可下载</h2>
              <p>浏览器不能安全预览此格式。请使用右上角“下载原始文件”查看。</p>
            </div>
          </div>
        ) : (
          <div className="pdf-loading">
            选择一份简历后会在这里显示原始文件。
          </div>
        )}
      </div>
    </div>
  );
}

function DrawerSummary({
  currentSummary,
  summaries,
  loading,
  onGenerate,
  onCreateManual,
  onOpenEvidence,
}: {
  currentSummary: ResumeSummary | null;
  summaries: ResumeSummary[];
  loading: boolean;
  onGenerate: () => void;
  onCreateManual: (
    summaryId: string,
    content: Record<string, string>,
  ) => Promise<void>;
  onOpenEvidence: () => void;
}) {
  const [selectedSummaryId, setSelectedSummaryId] = useState("");
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const selectedSummary =
    summaries.find((item) => item.summary_id === selectedSummaryId) ??
    currentSummary;

  useEffect(() => {
    if (!currentSummary) {
      setSelectedSummaryId("");
      setEditing(false);
      setDraft({});
      return;
    }
    setSelectedSummaryId(currentSummary.summary_id);
    setEditing(false);
    setDraft(summaryContentToDraft(currentSummary.content));
  }, [currentSummary?.summary_id]);

  if (loading) return <TableSkeleton />;
  if (!currentSummary) {
    return (
      <div className="empty-state">
        <div className="empty-state-inner">
          <span className="empty-glyph">
            <Icon name="spark" size={23} />
          </span>
          <h2>还没有 AI 总结</h2>
          <p>生成后会保存在这份简历中，之后可随时回看。</p>
          <button
            className="button button-primary"
            onClick={onGenerate}
            type="button"
          >
            <Icon name="spark" size={16} />
            生成 AI 总结
          </button>
        </div>
      </div>
    );
  }
  return (
    <div className="detail-summary">
      <div className="panel-heading">
        <div>
          <h2>{selectedSummary?.is_current ? "当前总结" : "历史总结"}</h2>
          <p>
            {selectedSummary?.source === "manual" ? "人工版本" : "AI 版本"} ·
            生成于 {selectedSummary ? formatLibraryDate(selectedSummary.created_at) : "—"}
          </p>
        </div>
        <div className="drawer-summary-actions">
          <button className="button" onClick={onGenerate} type="button">
            <Icon name="refresh" size={15} />
            重新生成
          </button>
          <button
            className="button button-ghost"
            onClick={() => {
              setDraft(summaryContentToDraft(selectedSummary?.content ?? {}));
              setEditing((current) => !current);
            }}
            type="button"
          >
            {editing ? "取消编辑" : "人工编辑"}
          </button>
        </div>
      </div>
      {summaries.length > 1 && (
        <div className="summary-history-control">
          <label className="field-label" htmlFor="summary-history">
            总结版本
          </label>
          <div className="select-wrap">
            <select
              className="select-field"
              id="summary-history"
              onChange={(event) => {
                const next = summaries.find(
                  (item) => item.summary_id === event.target.value,
                );
                if (!next) return;
                setSelectedSummaryId(next.summary_id);
                setDraft(summaryContentToDraft(next.content));
                setEditing(false);
              }}
              value={selectedSummary?.summary_id ?? ""}
            >
              {summaries.map((item) => (
                <option key={item.summary_id} value={item.summary_id}>
                  {item.is_current ? "当前 · " : "历史 · "}
                  {item.source === "manual" ? "人工" : "AI"} · {formatLibraryDate(item.created_at)}
                </option>
              ))}
            </select>
            <Icon name="chevron-down" size={16} />
          </div>
        </div>
      )}
      {editing && selectedSummary ? (
        <form
          className="summary-editor"
          onSubmit={(event) => {
            event.preventDefault();
            const content = Object.fromEntries(
              Object.entries(draft).filter(([, value]) => value.trim()),
            );
            if (!Object.keys(content).length) return;
            setSaving(true);
            void onCreateManual(selectedSummary.summary_id, content)
              .then(() => setEditing(false))
              .catch(() => undefined)
              .finally(() => setSaving(false));
          }}
        >
          {summarySectionOrder.map((key) => (
            <label className="field-stack" key={key}>
              <span className="field-label">{summarySectionLabels[key]}</span>
              <textarea
                className="textarea-field summary-editor-textarea"
                onChange={(event) =>
                  setDraft((current) => ({
                    ...current,
                    [key]: event.target.value,
                  }))
                }
                value={draft[key] ?? ""}
              />
            </label>
          ))}
          <div className="review-actions">
            <button
              className="button button-primary"
              disabled={saving}
              type="submit"
            >
              {saving ? <><i className="spinner" />正在保存</> : <><Icon name="check" size={16} />保存人工版本</>}
            </button>
          </div>
        </form>
      ) : selectedSummary ? (
        <SummaryContent
          content={selectedSummary.content}
          onOpenEvidence={onOpenEvidence}
        />
      ) : null}
    </div>
  );
}

const summarySectionLabels: Record<string, string> = {
  candidate_positioning: "候选人定位",
  education_background: "教育背景",
  work_and_internship: "工作与实习",
  core_skills: "核心技能",
  representative_projects: "代表项目",
  strengths: "优势亮点",
  verification_items: "建议核验",
};

const summarySectionOrder = Object.keys(summarySectionLabels);

function summaryContentToDraft(content: Record<string, unknown>): Record<string, string> {
  const sections = summarySections(content);
  return Object.fromEntries(
    summarySectionOrder.map((key) => [
      key,
      sections.find((section) => section.key === key)?.rendered ?? "",
    ]),
  );
}

function summaryFactIds(value: unknown): string[] {
  if (!value || typeof value !== "object" || Array.isArray(value)) return [];
  const rawFactIds = (value as Record<string, unknown>).fact_ids;
  return Array.isArray(rawFactIds)
    ? rawFactIds.filter((item): item is string => typeof item === "string")
    : [];
}

function summarySections(content: Record<string, unknown>) {
  const source =
    content.sections &&
    typeof content.sections === "object" &&
    !Array.isArray(content.sections)
      ? (content.sections as Record<string, unknown>)
      : content;
  return Object.entries(source)
    .filter(([key]) => key !== "schema_version")
    .flatMap(([key, value]) => {
      const rendered =
        typeof value === "string"
          ? value.trim()
          : value &&
              typeof value === "object" &&
              !Array.isArray(value) &&
              typeof (value as Record<string, unknown>).content === "string"
            ? ((value as Record<string, unknown>).content as string).trim()
            : "";
      return rendered
        ? [
            {
              key,
              label: summarySectionLabels[key] ?? key.replace(/_/g, " "),
              rendered,
              factIds: summaryFactIds(value),
            },
          ]
        : [];
    });
}

function SummaryContent({
  content,
  onOpenEvidence,
}: {
  content: Record<string, unknown>;
  onOpenEvidence?: () => void;
}) {
  const entries = summarySections(content);
  return (
    <article className="summary-card">
      {entries.length ? (
        <dl>
          {entries.flatMap((section) => [
            <dt key={`${section.key}-dt`}>{section.label}</dt>,
            <dd key={`${section.key}-dd`}>
              <p>{section.rendered}</p>
              {section.factIds.length > 0 && (
                <button
                  className="summary-evidence-link"
                  onClick={onOpenEvidence}
                  type="button"
                >
                  依据 {section.factIds.join("、")}
                </button>
              )}
            </dd>,
          ])}
        </dl>
      ) : (
        <p className="candidate-meta">AI 没有返回可展示的总结内容。</p>
      )}
    </article>
  );
}

function evidenceBlockLabel(ids: string[]): string {
  return ids.length ? `原文依据：${ids.join("、")}` : "未标注原文依据";
}

function EvidenceTab({
  review,
  loading,
  onEnrich,
  enriching,
}: {
  review: ResumeReviewDetail | null;
  loading: boolean;
  onEnrich: () => void;
  enriching: boolean;
}) {
  if (loading) return <TableSkeleton />;
  if (!review) {
    return (
      <div className="empty-state">
        <div className="empty-state-inner">
          <span className="empty-glyph"><Icon name="document" size={23} /></span>
          <h2>暂时无法读取提取依据</h2>
          <p>请稍后重新打开这份简历。</p>
        </div>
      </div>
    );
  }
  return (
    <div className="detail-review">
      <section className="content-section">
        <div className="panel-heading">
          <div>
            <h3>已提取的简历事实</h3>
            <p>历史简历可按需补充英语、成绩、奖项等 V2 事实。</p>
          </div>
          {review.is_active && (
            <button className="button" disabled={enriching} onClick={onEnrich} type="button">
              {enriching ? <><i className="spinner" />正在提交</> : "补充高级筛选事实"}
            </button>
          )}
        </div>
        <div className="detail-grid">
          <div className="fact-list">
            <div className="fact-row">
              <strong>教育经历</strong>
              {review.education.length ? review.education.map((item, index) => (
                <span key={`${item.school_name_raw}-${index}`}>
                  {item.school_name_raw} · {degreeLabels[item.degree]}
                  {item.major_raw ? ` · ${item.major_raw}` : ""}
                  {item.institution_tiers.length ? ` · ${item.institution_tiers.join("/")}` : ""}
                  {item.gpa_percent != null ? ` · GPA ${item.gpa_percent.toFixed(1)}%` : ""}
                  {item.rank_percent != null ? ` · 排名前 ${item.rank_percent.toFixed(1)}%` : ""}
                  {` · ${evidenceBlockLabel(item.evidence_block_ids)}`}
                </span>
              )) : <span>未提取到可验证教育经历</span>}
            </div>
            <div className="fact-row">
              <strong>核心技能</strong>
              {review.skills.length ? review.skills.map((item, index) => (
                <span key={`${item.skill_display}-${index}`}>
                  {item.skill_display} · {evidenceBlockLabel(item.evidence_block_ids)}
                </span>
              )) : <span>未提取到可验证技能</span>}
            </div>
            <div className="fact-row">
              <strong>英语能力</strong>
              {review.language_credentials.length ? review.language_credentials.map((item, index) => (
                <span key={`${item.credential_code}-${index}`}>
                  {fallbackFilterOptions.language_credentials.find(
                    (option) => option.value === item.credential_code,
                  )?.label ?? item.credential_name_raw}
                  {item.score != null ? ` · ${item.score}` : ""}
                  {` · ${evidenceBlockLabel(item.evidence_block_ids)}`}
                </span>
              )) : <span>未提取到明确英语证书记录</span>}
            </div>
            <div className="fact-row">
              <strong>奖学金</strong>
              {review.scholarships.length ? review.scholarships.map((item, index) => (
                <span key={`${item.scholarship_name_raw}-${index}`}>
                  {item.scholarship_name_raw} · {evidenceBlockLabel(item.evidence_block_ids)}
                </span>
              )) : <span>未提取到明确奖学金记录</span>}
            </div>
          </div>
          <div className="fact-list">
            <div className="fact-row">
              <strong>事实版本</strong>
              <span>v{review.facts_version}，仅当前版本用于筛选、评分与匹配。</span>
            </div>
            <div className="fact-row">
              <strong>年限统计</strong>
              <span>正式工作 {formatMonths(review.employment_months)}；工作 + 实习 {formatMonths(review.employment_or_internship_months)}。</span>
            </div>
          </div>
        </div>
      </section>
      <section className="content-section">
        <h3>经历与职责</h3>
        <div className="fact-list">
          {review.experiences.length ? review.experiences.map((item, index) => (
            <div className="fact-row fact-row-experience" key={`${item.experience_name_raw ?? item.title_raw ?? "experience"}-${index}`}>
              <strong>
                {item.organization_name_raw || item.experience_name_raw || "未命名经历"}
                {item.title_raw ? ` · ${item.title_raw}` : ""}
              </strong>
              <span>{item.experience_type} · {evidenceBlockLabel(item.evidence_block_ids)}</span>
              {(item.leadership_role || item.award_result_raw) && (
                <span>
                  {item.leadership_role ? `管理角色：${item.leadership_role}` : ""}
                  {item.leadership_role && item.award_result_raw ? " · " : ""}
                  {item.award_result_raw ? `获奖：${item.award_result_raw}` : ""}
                </span>
              )}
              {item.detail_items.length > 0 && (
                <ul className="fact-row-detail-list">
                  {item.detail_items.map((detail, detailIndex) => (
                    <li key={`${detail.detail_raw}-${detailIndex}`}>
                      <span>{detail.detail_raw}</span>
                      <small>{evidenceBlockLabel(detail.evidence_block_ids)}</small>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )) : <span className="candidate-meta">未提取到可验证经历。</span>}
        </div>
      </section>
      <section className="evidence-panel">
        <h3>原文证据块</h3>
        {review.source_blocks.map((block) => (
          <div className="evidence-item" key={block.block_id}>
            <b>{block.block_id} · 第 {block.page_no} 页</b>
            {block.text}
          </div>
        ))}
      </section>
    </div>
  );
}

interface MailboxDraft {
  displayName: string;
  imapHost: string;
  imapPort: string;
  emailAddress: string;
  mailbox: string;
  password: string;
  enabled: boolean;
}

function newMailboxDraft(): MailboxDraft {
  return {
    displayName: "",
    imapHost: "imap.feishu.cn",
    imapPort: "993",
    emailAddress: "",
    mailbox: "INBOX",
    password: "",
    enabled: true,
  };
}

function mailboxDraftFromConfig(config: MailboxConfig): MailboxDraft {
  return {
    displayName: config.display_name,
    imapHost: config.imap_host || "imap.feishu.cn",
    imapPort: String(config.imap_port || 993),
    emailAddress: config.email_address || "",
    mailbox: config.mailbox || "INBOX",
    password: "",
    enabled: config.enabled,
  };
}

function mailboxChannelStatus(config: MailboxConfig): string {
  if (config.active_sync_alert) return "需处理";
  if (config.archived_at) return "已归档";
  return config.enabled ? "已启用" : "已暂停";
}

function mailboxChannelStatusClass(config: MailboxConfig): string {
  if (config.active_sync_alert) return " is-error";
  if (config.archived_at) return "";
  return config.enabled ? " is-success" : " is-warning";
}

function mailboxSyncAlertTitle(config: MailboxConfig): string {
  const alert = config.active_sync_alert;
  if (!alert) return "";
  return alert.severity === "critical" ? "同步配置需要处理" : "同步持续失败";
}

function MailboxPage({
  notify,
  onImported,
  role,
}: {
  notify: (kind: ToastKind, message: string) => void;
  onImported: () => void;
  role: "admin" | "recruiter" | null;
}) {
  const [mailboxes, setMailboxes] = useState<MailboxConfig[]>([]);
  const [selectedMailboxId, setSelectedMailboxId] = useState<string | null>(null);
  const [historyFilterMailboxId, setHistoryFilterMailboxId] = useState<string | null>(null);
  const [history, setHistory] = useState<MailboxImportHistory | null>(null);
  const [draft, setDraft] = useState<MailboxDraft>(() => newMailboxDraft());
  const [isCreating, setIsCreating] = useState(true);
  const [retention, setRetention] = useState<MailboxRetentionOverview | null>(null);
  const [retentionRuns, setRetentionRuns] = useState<MailboxRetentionRuns | null>(null);
  const [mailboxJobs, setMailboxJobs] = useState<MailboxBackgroundJobHistory | null>(null);
  const [loading, setLoading] = useState(true);
  const [historyLoading, setHistoryLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [enqueuingMailboxId, setEnqueuingMailboxId] = useState<string | null>(null);
  const [enqueuingAll, setEnqueuingAll] = useState(false);
  const [archiving, setArchiving] = useState(false);
  const [enqueuingRetryImportId, setEnqueuingRetryImportId] = useState<string | null>(null);
  const [retentionSaving, setRetentionSaving] = useState(false);
  const [previewingRetention, setPreviewingRetention] = useState(false);
  const [cleaningRetention, setCleaningRetention] = useState(false);
  const [retentionPreview, setRetentionPreview] = useState<MailboxRetentionPreview | null>(null);
  const [retentionPolicy, setRetentionPolicy] = useState<MailboxRetentionPolicy>("standard");
  const retentionRequestRef = useRef(0);
  const mailboxJobPollInFlightRef = useRef(false);
  const manualMailboxJobIdsRef = useRef(new Set<string>());
  const handledMailboxJobIdsRef = useRef(new Set<string>());

  const selectedConfig = selectedMailboxId
    ? mailboxes.find((item) => item.mailbox_id === selectedMailboxId) ?? null
    : null;
  const selectedMailboxArchived = Boolean(selectedConfig?.archived_at);
  const activeMailboxJobs = (mailboxJobs?.items ?? []).filter(
    (job) => job.status === "queued" || job.status === "running",
  );
  const activeSyncMailboxIds = new Set(
    activeMailboxJobs
      .filter((job) => job.job_kind === "sync")
      .map((job) => job.mailbox_id),
  );
  const activeRetryImportIds = new Set(
    activeMailboxJobs
      .filter((job) => job.job_kind === "attachment_retry" && job.import_id)
      .flatMap((job) => job.import_id ? [job.import_id] : []),
  );
  const activeSyncAlerts = mailboxes.filter((item) => item.active_sync_alert);
  const selectedSyncJob = selectedMailboxId
    ? activeMailboxJobs.find(
      (job) => job.mailbox_id === selectedMailboxId && job.job_kind === "sync",
    ) ?? null
    : null;
  const selectedSyncInProgress = Boolean(
    selectedMailboxId
    && (activeSyncMailboxIds.has(selectedMailboxId) || enqueuingMailboxId === selectedMailboxId),
  );
  const canManageRetention = role === "admin";
  const retentionHasActiveRun = Boolean(retentionRuns?.items.some(
    (run) => run.status === "queued" || run.status === "running",
  ));
  const retentionPolicyChanged = Boolean(
    retention && retention.retention_policy !== retentionPolicy,
  );

  const loadRetentionActivity = useCallback(async (mailboxId: string, reset = false) => {
    const requestId = ++retentionRequestRef.current;
    if (reset) {
      setRetention(null);
      setRetentionRuns(null);
      setRetentionPreview(null);
      setRetentionPolicy("standard");
    }

    const [nextRetention, nextRuns] = await Promise.all([
      api.getMailboxRetention(mailboxId),
      api.listMailboxRetentionRuns(mailboxId),
    ]);
    if (retentionRequestRef.current !== requestId) return;
    setRetention(nextRetention);
    setRetentionPolicy(nextRetention.retention_policy);
    setRetentionRuns(nextRuns);
  }, []);

  const clearRetentionActivity = useCallback(() => {
    retentionRequestRef.current += 1;
    setRetention(null);
    setRetentionRuns(null);
    setRetentionPreview(null);
    setRetentionPolicy("standard");
  }, []);

  const selectMailbox = (config: MailboxConfig) => {
    setSelectedMailboxId(config.mailbox_id);
    setDraft(mailboxDraftFromConfig(config));
    setIsCreating(false);
  };

  const startCreatingMailbox = () => {
    setSelectedMailboxId(null);
    setDraft(newMailboxDraft());
    setIsCreating(true);
  };

  const updateDraft = <Key extends keyof MailboxDraft>(key: Key, value: MailboxDraft[Key]) => {
    setDraft((current) => ({ ...current, [key]: value }));
  };

  const applyMailboxList = (items: MailboxConfig[], preferredMailboxId?: string | null) => {
    setMailboxes(items);
    const desiredMailboxId = preferredMailboxId ?? selectedMailboxId;
    const nextConfig = items.find((item) => item.mailbox_id === desiredMailboxId)
      ?? items.find((item) => !item.archived_at)
      ?? items[0]
      ?? null;
    if (nextConfig) {
      selectMailbox(nextConfig);
    } else {
      startCreatingMailbox();
    }
  };

  const loadHistory = useCallback(async (mailboxId: string | null = historyFilterMailboxId) => {
    setHistoryLoading(true);
    try {
      setHistory(await api.listMailboxImports(mailboxId));
    } catch (error) {
      notify("error", humanizeError(error));
    } finally {
      setHistoryLoading(false);
    }
  }, [historyFilterMailboxId, notify]);

  const upsertMailboxJobs = useCallback((jobs: MailboxBackgroundJob[]) => {
    setMailboxJobs((current) => {
      const byId = new Map((current?.items ?? []).map((job) => [job.job_id, job]));
      for (const job of jobs) byId.set(job.job_id, job);
      const items = [...byId.values()].sort((left, right) => (
        right.requested_at.localeCompare(left.requested_at)
      ));
      return {
        items,
        total: Math.max(current?.total ?? 0, items.length),
      };
    });
  }, []);

  const refreshMailboxJobs = useCallback(async () => {
    if (mailboxJobPollInFlightRef.current) return;
    mailboxJobPollInFlightRef.current = true;
    try {
      const next = await api.listMailboxBackgroundJobs();
      setMailboxJobs(next);

      const terminalManualJobs = next.items.filter((job) => (
        manualMailboxJobIdsRef.current.has(job.job_id)
        && !handledMailboxJobIdsRef.current.has(job.job_id)
        && (job.status === "completed" || job.status === "failed")
      ));
      if (!terminalManualJobs.length) return;

      for (const job of terminalManualJobs) {
        handledMailboxJobIdsRef.current.add(job.job_id);
        const mailboxName = mailboxes.find((item) => item.mailbox_id === job.mailbox_id)?.display_name
          ?? "收件通道";
        if (job.status === "failed") {
          notify("error", mailboxImportErrorLabel(job.last_error));
          continue;
        }
        if (job.job_kind === "attachment_retry") {
          notify("success", "附件已在后台重新入库。");
          continue;
        }
        const summary = `“${mailboxName}”后台同步完成：入库 ${job.imported_count} 份，重复 ${job.duplicate_count} 份，跳过 ${job.skipped_count} 份。`;
        notify(
          job.failed_count ? "error" : "success",
          job.failed_count ? `${summary} ${job.failed_count} 份处理失败。` : summary,
        );
      }

      if (terminalManualJobs.some((job) => job.imported_count > 0)) onImported();
      void api.listMailboxConfigs(true).then((response) => setMailboxes(response.items)).catch(() => undefined);
      void loadHistory(historyFilterMailboxId);
      if (selectedMailboxId) {
        void loadRetentionActivity(selectedMailboxId).catch(() => undefined);
      }
    } finally {
      mailboxJobPollInFlightRef.current = false;
    }
  }, [historyFilterMailboxId, loadHistory, loadRetentionActivity, mailboxes, notify, onImported, selectedMailboxId]);

  const loadInitialData = useCallback(async () => {
    setLoading(true);
    setHistoryLoading(true);
    try {
      const [configResponse, historyResponse, jobsResponse] = await Promise.all([
        api.listMailboxConfigs(true),
        api.listMailboxImports(),
        api.listMailboxBackgroundJobs(),
      ]);
      applyMailboxList(configResponse.items);
      setHistory(historyResponse);
      setMailboxJobs(jobsResponse);
    } catch (error) {
      notify("error", humanizeError(error));
    } finally {
      setLoading(false);
      setHistoryLoading(false);
    }
  // The initial fetch intentionally runs once. Actions refresh only the data they change.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [notify]);

  useEffect(() => { void loadInitialData(); }, [loadInitialData]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      // Do not replace the current form draft while the recruiter is editing.
      // This refresh only brings scheduled-worker alert and health changes
      // into the channel list when no browser task is active.
      void api.listMailboxConfigs(true)
        .then((response) => setMailboxes(response.items))
        .catch(() => undefined);
    }, 30_000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    if (!selectedConfig?.configured) {
      clearRetentionActivity();
      return undefined;
    }

    let active = true;
    void loadRetentionActivity(selectedConfig.mailbox_id, true).catch((error) => {
      if (active) notify("error", humanizeError(error));
    });
    return () => {
      active = false;
    };
  }, [
    clearRetentionActivity,
    loadRetentionActivity,
    notify,
    selectedConfig?.configured,
    selectedConfig?.mailbox_id,
  ]);

  useEffect(() => {
    if (!selectedConfig?.configured || !retentionHasActiveRun) return undefined;
    const mailboxId = selectedConfig.mailbox_id;
    const timer = window.setInterval(() => {
      void loadRetentionActivity(mailboxId).catch(() => undefined);
    }, 5_000);
    return () => window.clearInterval(timer);
  }, [
    loadRetentionActivity,
    retentionHasActiveRun,
    selectedConfig?.configured,
    selectedConfig?.mailbox_id,
  ]);

  useEffect(() => {
    if (!activeMailboxJobs.length) return undefined;
    void refreshMailboxJobs();
    const timer = window.setInterval(() => {
      void refreshMailboxJobs();
    }, 2_000);
    return () => window.clearInterval(timer);
  }, [activeMailboxJobs.length, refreshMailboxJobs]);

  const saveMailbox = async () => {
    if (!draft.displayName.trim()) {
      notify("error", "请为这个收件通道填写名称。");
      return;
    }
    if (!draft.imapHost.trim() || !draft.emailAddress.trim()) {
      notify("error", "请填写 IMAP 地址和接收简历的邮箱。");
      return;
    }
    if (isCreating && !draft.password) {
      notify("error", "新通道首次保存需要填写邮箱授权码。");
      return;
    }
    if (!isCreating && !selectedConfig) {
      notify("error", "请先选择一个收件通道。");
      return;
    }
    if (!isCreating && selectedConfig?.archived_at) {
      notify("error", "归档通道仅保留历史与内容清理记录，不能再修改连接配置。");
      return;
    }

    setSaving(true);
    try {
      const connection = {
        display_name: draft.displayName.trim(),
        imap_host: draft.imapHost.trim(),
        imap_port: Number(draft.imapPort) || 993,
        email_address: draft.emailAddress.trim(),
        mailbox: draft.mailbox.trim() || "INBOX",
        enabled: draft.enabled,
      };
      const saved = isCreating
        ? await api.createMailboxConfig({ ...connection, password: draft.password })
        : await api.updateMailboxConfig(selectedConfig!.mailbox_id, {
          ...connection,
          ...(draft.password ? { password: draft.password } : {}),
        });
      setMailboxes((current) => [
        saved,
        ...current.filter((item) => item.mailbox_id !== saved.mailbox_id),
      ]);
      selectMailbox(saved);
      notify("success", isCreating ? "收件通道已创建，只会入库从现在起收到的附件。" : "收件通道已保存。");
    } catch (error) {
      notify("error", humanizeError(error));
    } finally {
      setSaving(false);
    }
  };

  const syncMailbox = async (config: MailboxConfig) => {
    if (
      !config.enabled
      || config.archived_at
      || enqueuingMailboxId === config.mailbox_id
      || activeSyncMailboxIds.has(config.mailbox_id)
    ) return;
    setEnqueuingMailboxId(config.mailbox_id);
    try {
      const job = await api.syncMailbox(config.mailbox_id);
      manualMailboxJobIdsRef.current.add(job.job_id);
      upsertMailboxJobs([job]);
      notify("success", job.deduplicated ? "该收件通道已有后台同步任务。" : "已加入后台同步队列。");
    } catch (error) {
      notify("error", humanizeError(error));
    } finally {
      setEnqueuingMailboxId(null);
    }
  };

  const syncAllMailboxes = async () => {
    if (enqueuingAll || !mailboxes.some((item) => item.enabled && !item.archived_at)) return;
    setEnqueuingAll(true);
    try {
      const result = await api.syncAllMailboxes();
      for (const job of result.items) manualMailboxJobIdsRef.current.add(job.job_id);
      upsertMailboxJobs(result.items);
      notify(
        "success",
        result.queued_count
          ? `${result.queued_count} 个收件通道已加入后台同步队列。`
          : "所有可用收件通道都已有后台同步任务。",
      );
    } catch (error) {
      notify("error", humanizeError(error));
    } finally {
      setEnqueuingAll(false);
    }
  };

  const archiveMailbox = async () => {
    if (!selectedConfig || archiving) return;
    if (!window.confirm(`归档“${selectedConfig.display_name}”？它将停止接收新附件，已有入库记录会保留。`)) return;
    setArchiving(true);
    try {
      const archived = await api.archiveMailbox(selectedConfig.mailbox_id);
      setMailboxes((current) => current.map((item) => (
        item.mailbox_id === archived.mailbox_id ? archived : item
      )));
      selectMailbox(archived);
      notify("success", "收件通道已归档，历史入库、内容保留与清理记录仍可查看。");
    } catch (error) {
      notify("error", humanizeError(error));
    } finally {
      setArchiving(false);
    }
  };

  const retryImport = async (item: MailboxImportHistoryItem) => {
    if (
      !item.can_retry
      || enqueuingRetryImportId === item.import_id
      || activeRetryImportIds.has(item.import_id)
    ) return;
    setEnqueuingRetryImportId(item.import_id);
    try {
      const job = await api.retryMailboxImport(item.import_id);
      manualMailboxJobIdsRef.current.add(job.job_id);
      upsertMailboxJobs([job]);
      notify("success", job.deduplicated ? "该附件已有后台重试任务。" : "已加入后台重新入库队列。");
    } catch (error) {
      notify("error", humanizeError(error));
    } finally {
      setEnqueuingRetryImportId(null);
    }
  };

  const saveRetentionPolicy = async () => {
    if (!selectedConfig?.configured) {
      notify("error", "请先保存这个收件通道，再设置内容保留策略。");
      return;
    }
    if (!canManageRetention) return;

    setRetentionSaving(true);
    try {
      const saved = await api.saveMailboxRetention(selectedConfig.mailbox_id, {
        retention_policy: retentionPolicy,
      });
      setRetention(saved);
      setRetentionPolicy(saved.retention_policy);
      setRetentionPreview(null);
      notify("success", "内容保留策略已保存，将在后续清理任务中生效。");
    } catch (error) {
      notify("error", humanizeError(error));
    } finally {
      setRetentionSaving(false);
    }
  };

  const previewRetentionCleanup = async () => {
    if (!selectedConfig?.configured || !canManageRetention) return;
    if (retentionPolicyChanged) {
      notify("error", "请先保存新的保留策略，再预览清理范围。");
      return;
    }

    setPreviewingRetention(true);
    try {
      const preview = await api.previewMailboxRetention(selectedConfig.mailbox_id);
      setRetentionPreview(preview);
    } catch (error) {
      notify("error", humanizeError(error));
    } finally {
      setPreviewingRetention(false);
    }
  };

  const startRetentionCleanup = async () => {
    if (!selectedConfig?.configured || !canManageRetention || !retentionPreview) return;
    if (mailboxRetentionDueCount(retentionPreview) <= 0) return;

    setCleaningRetention(true);
    try {
      const run = await api.cleanupMailboxRetention(selectedConfig.mailbox_id);
      setRetentionPreview(null);
      setRetentionRuns((current) => ({
        items: [
          run,
          ...(current?.items.filter((item) => item.run_id !== run.run_id) ?? []),
        ],
        total: Math.max(current?.total ?? 0, (current?.items.length ?? 0) + 1),
      }));
      notify(
        "success",
        run.status === "completed" ? "已完成该通道的过期内容清理。" : "已创建清理任务，状态会在下方自动更新。",
      );
      void loadRetentionActivity(selectedConfig.mailbox_id).catch(() => undefined);
    } catch (error) {
      notify("error", humanizeError(error));
    } finally {
      setCleaningRetention(false);
    }
  };

  const historySourceOptions = [
    ...mailboxes.map((item) => ({ mailboxId: item.mailbox_id, displayName: item.display_name })),
    ...(history?.items ?? [])
      .filter((item) => !mailboxes.some((mailbox) => mailbox.mailbox_id === item.mailbox_config_id))
      .map((item) => ({
        mailboxId: item.mailbox_config_id,
        displayName: item.mailbox_display_name || "已归档收件通道",
      })),
  ].filter((item, index, entries) => entries.findIndex((candidate) => candidate.mailboxId === item.mailboxId) === index);

  return (
    <div className="page-frame mailbox-page">
      <header className="page-heading">
        <div>
          <h1>邮箱附件入库</h1>
          <p>每个收件通道独立保存绑定位置和同步状态，只接收绑定之后到达的附件。</p>
        </div>
        <div className="mailbox-heading-actions">
          <button className="button" disabled={loading || saving || enqueuingAll} onClick={startCreatingMailbox} type="button">
            <Icon name="plus" size={16} />新建收件通道
          </button>
          <button
            className="button button-primary"
            disabled={loading || saving || enqueuingAll || !mailboxes.some((item) => item.enabled && !item.archived_at)}
            onClick={() => void syncAllMailboxes()}
            type="button"
          >
            {enqueuingAll ? <><i className="spinner" />正在加入队列</> : activeSyncMailboxIds.size ? <><i className="spinner" />后台同步中</> : <><Icon name="refresh" size={16} />同步全部</>}
          </button>
        </div>
      </header>

      {activeSyncAlerts.length > 0 && (
        <section aria-label="需要处理的邮箱同步异常" className="mailbox-sync-alert-list" role="alert">
          <div className="mailbox-sync-alert-list-heading">
            <div>
              <h2>需要处理的同步异常</h2>
              <p>这些通道的后台同步已连续失败。请检查连接配置后重新同步，成功后提示会自动恢复。</p>
            </div>
            <span className="status-pill is-error">{activeSyncAlerts.length} 个通道需处理</span>
          </div>
          <div className="mailbox-sync-alert-items">
            {activeSyncAlerts.map((config) => {
              const alert = config.active_sync_alert!;
              const canSync = config.enabled
                && !config.archived_at
                && enqueuingMailboxId !== config.mailbox_id
                && !activeSyncMailboxIds.has(config.mailbox_id);
              return (
                <div className="mailbox-sync-alert-item" key={config.mailbox_id}>
                  <div>
                    <strong>{config.display_name}</strong>
                    <span>{mailboxSyncAlertTitle(config)}，连续失败的后台同步任务 {alert.consecutive_failures} 次，最近一次 {formatLibraryDate(alert.last_failed_at)}。</span>
                    <small>{mailboxImportErrorLabel(alert.last_error_code)}</small>
                  </div>
                  <button
                    className="button button-danger-ghost"
                    disabled={!canSync}
                    onClick={() => void syncMailbox(config)}
                    type="button"
                  >
                    {enqueuingMailboxId === config.mailbox_id
                      ? <><i className="spinner" />正在加入队列</>
                      : activeSyncMailboxIds.has(config.mailbox_id)
                        ? <><i className="spinner" />后台同步中</>
                        : <><Icon name="refresh" size={16} />同步此通道</>}
                  </button>
                </div>
              );
            })}
          </div>
        </section>
      )}

      <div className="mailbox-workspace">
        <aside className="panel mailbox-channel-panel" aria-label="收件通道">
          <div className="panel-heading mailbox-channel-heading">
            <div><h2>收件通道</h2><p>各通道独立同步；同一工作区内的相同附件统一去重。</p></div>
            <span className="tiny-badge">{mailboxes.length}</span>
          </div>
          {loading ? <TableSkeleton /> : mailboxes.length ? (
            <div className="mailbox-channel-list">
              {mailboxes.map((config) => {
                const selected = !isCreating && config.mailbox_id === selectedMailboxId;
                return (
                  <button
                    aria-pressed={selected}
                    className={`mailbox-channel-row${selected ? " is-selected" : ""}`}
                    key={config.mailbox_id}
                    onClick={() => selectMailbox(config)}
                    type="button"
                  >
                    <span className="mailbox-channel-copy">
                      <strong>{config.display_name}</strong>
                      <span>{config.email_address || "尚未配置接收邮箱"}</span>
                    </span>
                    <span className={`status-pill${mailboxChannelStatusClass(config)}`}>{mailboxChannelStatus(config)}</span>
                  </button>
                );
              })}
            </div>
          ) : (
            <div className="mailbox-channel-empty">
              <span className="empty-glyph"><Icon name="inbox" size={20} /></span>
              <strong>还没有收件通道</strong>
              <span>新建后，系统从当前邮箱位置开始接收附件。</span>
            </div>
          )}
          <button className="button button-ghost mailbox-add-channel" onClick={startCreatingMailbox} type="button">
            <Icon name="plus" size={16} />新建收件通道
          </button>
        </aside>

        <div className="mailbox-detail">
          <div className="mailbox-detail-grid">
            <section className="panel">
              <div className="panel-heading">
                <div>
                  <h2>{isCreating ? "新建收件通道" : selectedConfig?.display_name || "收件通道"}</h2>
                  <p>{isCreating ? "保存时会记录当前邮箱位置，历史邮件不会入库。" : "授权码始终保持隐藏，留空则继续使用已保存的值。"}</p>
                </div>
                {selectedConfig && <span className={`status-pill${mailboxChannelStatusClass(selectedConfig)}`}>{mailboxChannelStatus(selectedConfig)}</span>}
              </div>
              {loading ? <TableSkeleton /> : (
                <div className="form-grid">
                  <div className="field-stack span-full">
                    <label className="field-label" htmlFor="mailbox-display-name">通道名称</label>
                    <input className="field" disabled={selectedMailboxArchived || selectedSyncInProgress} id="mailbox-display-name" maxLength={32} onChange={(event) => updateDraft("displayName", event.target.value)} placeholder="例如：招聘邮箱" value={draft.displayName} />
                  </div>
                  <div className="field-stack">
                    <label className="field-label" htmlFor="imap-host">IMAP 地址</label>
                    <input className="field" disabled={selectedMailboxArchived || selectedSyncInProgress} id="imap-host" onChange={(event) => updateDraft("imapHost", event.target.value)} value={draft.imapHost} />
                    <p className="field-help">仅可连接服务端已批准的 IMAPS 服务商地址。</p>
                  </div>
                  <div className="field-stack">
                    <label className="field-label" htmlFor="imap-port">端口</label>
                    <input className="field" disabled={selectedMailboxArchived || selectedSyncInProgress} id="imap-port" inputMode="numeric" onChange={(event) => updateDraft("imapPort", event.target.value)} value={draft.imapPort} />
                    <p className="field-help">系统仅接受加密 IMAPS 的 993 端口。</p>
                  </div>
                  <div className="field-stack span-full">
                    <label className="field-label" htmlFor="imap-address">接收简历的邮箱</label>
                    <input autoComplete="email" className="field" disabled={selectedMailboxArchived || selectedSyncInProgress} id="imap-address" onChange={(event) => updateDraft("emailAddress", event.target.value)} type="email" value={draft.emailAddress} />
                  </div>
                  <div className="field-stack">
                    <label className="field-label" htmlFor="imap-folder">邮箱文件夹</label>
                    <input className="field" disabled={selectedMailboxArchived || selectedSyncInProgress} id="imap-folder" onChange={(event) => updateDraft("mailbox", event.target.value)} value={draft.mailbox} />
                  </div>
                  <div className="field-stack">
                    <label className="field-label" htmlFor="imap-password">邮箱授权码</label>
                    <input
                      aria-describedby="imap-password-hint"
                      autoComplete="new-password"
                      className="field"
                      disabled={selectedMailboxArchived || selectedSyncInProgress}
                      id="imap-password"
                      onChange={(event) => updateDraft("password", event.target.value)}
                      placeholder={isCreating ? "首次保存必填" : "留空则保持原授权码"}
                      type="password"
                      value={draft.password}
                    />
                    <p className="field-help" id="imap-password-hint">授权码仅用于连接该收件通道，不会在页面中回显。</p>
                  </div>
                  <label className="choice-row span-full">
                    <input checked={draft.enabled} disabled={selectedMailboxArchived || selectedSyncInProgress} onChange={(event) => updateDraft("enabled", event.target.checked)} type="checkbox" />
                    启用后台定时同步
                  </label>
                </div>
              )}
              <div className="review-actions mailbox-form-actions">
                {!isCreating && selectedConfig && (
                  <button className="button button-ghost" disabled={archiving || saving || selectedSyncInProgress || !selectedConfig.enabled || Boolean(selectedConfig.archived_at)} onClick={() => void syncMailbox(selectedConfig)} type="button">
                    {enqueuingMailboxId === selectedConfig.mailbox_id ? <><i className="spinner" />正在加入队列</> : selectedSyncJob ? <><i className="spinner" />后台同步中</> : <><Icon name="refresh" size={16} />同步此通道</>}
                  </button>
                )}
                {!isCreating && selectedConfig && !selectedConfig.archived_at && (
                  <button className="button button-danger-ghost" disabled={archiving || saving || selectedSyncInProgress} onClick={() => void archiveMailbox()} type="button">
                    {archiving ? <><i className="spinner" />正在归档</> : "归档通道"}
                  </button>
                )}
                <button className="button button-primary" disabled={loading || saving || archiving || selectedSyncInProgress || (!isCreating && selectedMailboxArchived)} onClick={() => void saveMailbox()} type="button">
                  {saving ? <><i className="spinner" />正在保存</> : <><Icon name="check" size={16} />{isCreating ? "创建并开始接收" : selectedMailboxArchived ? "已归档" : "保存通道"}</>}
                </button>
              </div>
            </section>

            <aside className="panel mailbox-status-panel">
              <div className="panel-heading"><div><h2>同步与保留状态</h2><p>各通道独立同步与清理；同一工作区内已处理邮件或相同内容附件不会重复创建候选人，每封邮件仍保留一条处理记录。</p></div></div>
              {selectedConfig ? (
                <>
                  {selectedConfig.active_sync_alert && (
                    <section className="mailbox-sync-alert-detail" role="alert">
                      <div>
                        <strong>{mailboxSyncAlertTitle(selectedConfig)}</strong>
                        <span>连续失败的后台同步任务 {selectedConfig.active_sync_alert.consecutive_failures} 次，最近一次 {formatLibraryDate(selectedConfig.active_sync_alert.last_failed_at)}。</span>
                        <small>{mailboxImportErrorLabel(selectedConfig.active_sync_alert.last_error_code)}</small>
                      </div>
                      <button
                        className="button button-danger-ghost"
                        disabled={!selectedConfig.enabled || Boolean(selectedConfig.archived_at) || selectedSyncInProgress}
                        onClick={() => void syncMailbox(selectedConfig)}
                        type="button"
                      >
                        {selectedSyncInProgress ? <><i className="spinner" />后台同步中</> : <><Icon name="refresh" size={16} />立即同步</>}
                      </button>
                    </section>
                  )}
                <div className="fact-list">
                  <div className="fact-row"><strong>开始接收</strong><span>{selectedConfig.import_started_at ? formatLibraryDate(selectedConfig.import_started_at) : "正在初始化"}</span></div>
                  <div className="fact-row"><strong>最近同步</strong><span>{selectedConfig.last_synced_at ? formatLibraryDate(selectedConfig.last_synced_at) : "尚未同步"}</span></div>
                  {selectedSyncJob && <div className="fact-row"><strong>后台任务</strong><span className={`status-pill ${mailboxBackgroundJobStatusClass(selectedSyncJob)}`}>{mailboxBackgroundJobStatusLabel(selectedSyncJob)}</span></div>}
                  <div className="fact-row"><strong>附件处理记录</strong><span>{historyFilterMailboxId === selectedConfig.mailbox_id ? `${history?.total ?? 0} 条` : "可在下方按来源筛选"}</span></div>
                  <div className="fact-row"><strong>支持格式</strong><span>PDF、Word、图片、Excel、HTML</span></div>
                  {retention && <>
                    <div className="fact-row"><strong>当前保留</strong><span>{mailboxRetentionPolicyLabel(retention.retention_policy)}</span></div>
                    <div className="fact-row"><strong>缓存内容</strong><span>{retention.body_copy_count} 正文 · {retention.attachment_copy_count + retention.failure_artifact_count} 附件副本</span></div>
                    <div className="fact-row"><strong>缓存占用</strong><span>{formatFileSize(retention.cache_bytes)}</span></div>
                    <div className="fact-row"><strong>最早到期</strong><span>{retention.earliest_expires_at ? formatLibraryDate(retention.earliest_expires_at) : "暂无待清理内容"}</span></div>
                    <div className="fact-row"><strong>最近清理</strong><span>{retention.last_cleanup_at ? formatLibraryDate(retention.last_cleanup_at) : "尚未执行"}</span></div>
                    <div className="fact-row"><strong>下次清理</strong><span>{retention.next_cleanup_at ? formatLibraryDate(retention.next_cleanup_at) : "由系统定时安排"}</span></div>
                  </>}
                  {selectedConfig.last_sync_error && <div className="fact-row"><strong>最近异常</strong><span>{mailboxImportErrorLabel(selectedConfig.last_sync_error)}</span></div>}
                </div>
                </>
              ) : (
                <div className="mailbox-status-empty"><Icon name="history" size={19} /><span>保存后会显示这个通道的接收起点、最近同步时间和异常状态。</span></div>
              )}
            </aside>
          </div>

          <section className="panel mailbox-retention-panel">
            <div className="panel-heading">
              <div>
                <h2>内容保留</h2>
                <p>只清理当前通道的系统邮件正文与附件副本，不会删除源邮件或候选人原始简历。</p>
              </div>
              {retention && <span className="status-pill">{mailboxRetentionPolicyLabel(retention.retention_policy)}</span>}
            </div>
            {loading ? <TableSkeleton /> : !selectedConfig?.configured ? (
              <div className="mailbox-retention-empty">
                <strong>先保存收件通道</strong>
                <span>保存连接配置后，可为这个通道设置正文和附件副本的保留周期。</span>
              </div>
            ) : (
              <>
                {selectedMailboxArchived && <p className="mailbox-retention-notice">该通道已归档，不会接收新附件；已有内容仍按以下策略清理。</p>}
                <fieldset className="mailbox-retention-policy" disabled={!canManageRetention || retentionSaving}>
                  <legend className="field-label">内容保留档位</legend>
                  <div className="mailbox-retention-policy-options">
                    {mailboxRetentionPolicies.map((option) => (
                      <label className="choice-row mailbox-retention-option" key={option.value}>
                        <input
                          checked={retentionPolicy === option.value}
                          name="mailbox-retention-policy"
                          onChange={() => {
                            setRetentionPolicy(option.value);
                            setRetentionPreview(null);
                          }}
                          type="radio"
                        />
                        <span>
                          <strong>{option.label}</strong>
                          <small>{option.description}</small>
                        </span>
                      </label>
                    ))}
                  </div>
                </fieldset>
                {!canManageRetention && <p className="field-help">仅工作区管理员可以修改保留策略或执行清理。当前策略与清理统计仍可查看。</p>}
                <p className="mailbox-retention-notice">已删除的系统副本不可恢复。简历库中的候选人原始简历、AI 结论与邮箱服务商中的源邮件不受影响。</p>
                {canManageRetention && (
                  <div className="review-actions mailbox-retention-actions">
                    <button className="button button-primary" disabled={retentionSaving || !retention || !retentionPolicyChanged} onClick={() => void saveRetentionPolicy()} type="button">
                      {retentionSaving ? <><i className="spinner" />正在保存</> : <><Icon name="check" size={16} />保存保留策略</>}
                    </button>
                    <button className="button" disabled={!retention || previewingRetention || retentionSaving || retentionPolicyChanged || retentionHasActiveRun} onClick={() => void previewRetentionCleanup()} type="button">
                      {previewingRetention ? <><i className="spinner" />正在预览</> : <><Icon name="history" size={16} />预览已到期内容</>}
                    </button>
                  </div>
                )}
                {retentionPreview && (
                  <section aria-live="polite" className="mailbox-retention-preview">
                    <div className="mailbox-retention-preview-heading">
                      <div>
                        <h3>已到期内容预览</h3>
                        <p>以下系统副本将不可恢复地删除，不包含邮箱源邮件或候选人原始简历。</p>
                      </div>
                      <span className={`status-pill${mailboxRetentionDueCount(retentionPreview) ? " is-warning" : " is-success"}`}>
                        {mailboxRetentionDueCount(retentionPreview) ? `${mailboxRetentionDueCount(retentionPreview)} 项待清理` : "暂无待清理内容"}
                      </span>
                    </div>
                    <div className="mailbox-retention-preview-stats">
                      <div><strong>正文副本</strong><span>{retentionPreview.expired_body_count} 项</span></div>
                      <div><strong>成功与失败附件副本</strong><span>{retentionPreview.expired_attachment_copy_count + retentionPreview.expired_failure_artifact_count} 项</span></div>
                      <div><strong>预计释放</strong><span>{formatFileSize(retentionPreview.expired_bytes)}</span></div>
                      <div><strong>暂不清理</strong><span>{retentionPreview.skipped_count} 项</span></div>
                    </div>
                    {canManageRetention && mailboxRetentionDueCount(retentionPreview) > 0 && (
                      <div className="review-actions mailbox-retention-confirm-actions">
                        <button className="button button-danger-ghost" disabled={cleaningRetention || retentionHasActiveRun} onClick={() => void startRetentionCleanup()} type="button">
                          {cleaningRetention ? <><i className="spinner" />正在创建清理任务</> : "确认清理已到期内容"}
                        </button>
                      </div>
                    )}
                  </section>
                )}
              </>
            )}
          </section>

          <section className="panel mailbox-history">
            <div className="panel-heading mailbox-history-heading">
              <div><h2>附件入库记录</h2><p>每封新邮件保留一条附件处理记录；相同内容只关联既有入库结果，不展示邮件正文或候选人信息。</p></div>
              <div className="mailbox-history-filter">
                <label className="field-label" htmlFor="mailbox-history-filter">来源</label>
                <div className="select-wrap">
                  <select className="select-field" id="mailbox-history-filter" onChange={(event) => {
                    const mailboxId = event.target.value || null;
                    setHistoryFilterMailboxId(mailboxId);
                    void loadHistory(mailboxId);
                  }} value={historyFilterMailboxId ?? ""}>
                    <option value="">全部收件通道</option>
                    {historySourceOptions.map((item) => <option key={item.mailboxId} value={item.mailboxId}>{item.displayName}</option>)}
                  </select>
                  <Icon name="chevron-down" size={16} />
                </div>
              </div>
            </div>
            <span aria-live="polite" className="sr-only">{activeRetryImportIds.size ? "附件正在后台重新入库。" : activeSyncMailboxIds.size ? "收件通道正在后台同步。" : ""}</span>
            {historyLoading ? <TableSkeleton /> : history?.items.length ? (
              <div className="table-scroll">
                <table className="candidate-table mailbox-history-table">
                  <thead>
                    <tr>
                      <th scope="col">附件</th>
                      <th scope="col">来源</th>
                      <th scope="col">结果与原因</th>
                      <th scope="col">尝试</th>
                      <th scope="col">最后处理</th>
                      <th scope="col">操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {history.items.map((item) => {
                      const isRetrying = (item.status === "retrying" && !item.can_retry)
                        || activeRetryImportIds.has(item.import_id)
                        || enqueuingRetryImportId === item.import_id;
                      const statusClass = item.status === "imported" || item.status === "duplicate"
                        ? "is-success"
                        : item.status === "failed"
                          ? "is-error"
                          : item.status === "retrying" && item.can_retry
                            ? "is-warning"
                            : item.status === "retrying" || item.status === "deduplicating" || item.status === "processing"
                              ? "is-progress"
                              : "";
                      return (
                        <tr key={item.import_id}>
                          <th scope="row"><strong>{item.attachment_filename}</strong></th>
                          <td className="mailbox-source-cell">{item.mailbox_display_name || "已归档收件通道"}</td>
                          <td>
                            <span className={`status-pill mailbox-import-status ${statusClass}`}>{mailboxImportStatusLabel(item.status, item.can_retry)}</span>
                            {item.error && <small className="mailbox-import-error">{mailboxImportErrorLabel(item.error)}</small>}
                          </td>
                          <td className="mailbox-attempt-cell">{item.attempt_count} 次</td>
                          <td>{formatLibraryDate(item.last_attempted_at ?? item.created_at)}</td>
                          <td className="mailbox-action-cell">
                            {isRetrying ? (
                              <span className="mailbox-retry-pending"><i className="spinner" />正在重试</span>
                            ) : item.can_retry ? (
                              <button aria-label={`重新入库：${item.attachment_filename}`} className="button button-ghost upload-row-button mailbox-retry-button" disabled={activeRetryImportIds.has(item.import_id) || enqueuingRetryImportId === item.import_id} onClick={() => void retryImport(item)} type="button">
                                <Icon name="refresh" size={15} />重新入库
                              </button>
                            ) : <span className="candidate-meta">—</span>}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            ) : <div className="empty-state"><div className="empty-state-inner"><span className="empty-glyph"><Icon name="inbox" size={23} /></span><h2>还没有附件入库记录</h2><p>绑定后收到的附件会在这里显示，历史邮件不会入库。</p></div></div>}
          </section>

          <section className="panel mailbox-retention-history">
            <div className="panel-heading">
              <div>
                <h2>清理记录</h2>
                <p>仅保留安全统计与任务状态，不展示邮件正文、邮箱地址或附件内容。</p>
              </div>
              {retentionHasActiveRun && <span className="status-pill is-progress"><i className="spinner" />正在更新</span>}
            </div>
            <span aria-live="polite" className="sr-only">{retentionHasActiveRun ? "正在更新当前收件通道的内容清理任务状态。" : ""}</span>
            {loading ? <TableSkeleton /> : !selectedConfig?.configured ? (
              <div className="mailbox-retention-empty">
                <strong>尚未配置清理</strong>
                <span>保存收件通道后，系统会按该通道的保留策略自动清理过期副本。</span>
              </div>
            ) : retentionRuns?.items.length ? (
              <div className="table-scroll">
                <table className="candidate-table mailbox-retention-history-table">
                  <thead>
                    <tr>
                      <th scope="col">触发方式</th>
                      <th scope="col">保留策略</th>
                      <th scope="col">状态</th>
                      <th scope="col">扫描 / 清理</th>
                      <th scope="col">释放空间</th>
                      <th scope="col">处理时间</th>
                    </tr>
                  </thead>
                  <tbody>
                    {retentionRuns.items.map((run) => (
                      <tr key={run.run_id}>
                        <th scope="row">{run.trigger_type === "manual" ? "手动" : "定时"}</th>
                        <td>{mailboxRetentionPolicyLabel(run.retention_policy)}</td>
                        <td>
                          <span className={`status-pill ${mailboxRetentionRunStatusClass(run.status)}`}>{mailboxRetentionRunStatusLabel(run.status)}</span>
                          {run.error_code && <small className="mailbox-import-error">{mailboxRetentionRunErrorLabel(run.error_code)}</small>}
                        </td>
                        <td className="mailbox-retention-count-cell">{run.scanned_count} / {run.deleted_count}{run.skipped_count ? `，跳过 ${run.skipped_count}` : ""}{run.failed_count ? `，失败 ${run.failed_count}` : ""}</td>
                        <td className="mailbox-retention-count-cell">{formatFileSize(run.reclaimed_bytes)}</td>
                        <td>{formatLibraryDate(run.finished_at ?? run.started_at ?? "")}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <div className="mailbox-retention-empty">
                <strong>还没有清理记录</strong>
                <span>系统会每日检查当前通道的到期副本；管理员也可先预览后手动执行。</span>
              </div>
            )}
          </section>
        </div>
      </div>
    </div>
  );
}

function UploadPage({
  onComplete,
  notify,
}: {
  onComplete: (resumeId: string, candidateId: string) => void;
  notify: (kind: ToastKind, message: string) => void;
}) {
  const [uploads, setUploads] = useState<UploadQueueItem[]>([]);
  const [dragging, setDragging] = useState(false);
  const [uploading, setUploading] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const uploadLockRef = useRef(false);
  const dragDepthRef = useRef(0);

  const queuedUploads = uploads.filter((item) => item.status === "queued");
  const failedUploads = uploads.filter((item) => item.status === "error");
  const retryableFailedUploads = failedUploads.filter(
    (item) => item.retryable !== false,
  );
  const completedUploads = uploads.filter((item) => item.status === "success");
  const resolvedUploads = uploads.filter(
    (item) => item.status === "success" || item.status === "attention",
  );
  const attentionUploads = uploads.filter(
    (item) => item.status === "attention",
  );
  const extractingUploads = uploads.filter(
    (item) => item.status === "extracting",
  );

  const updateUpload = (uploadId: string, patch: Partial<UploadQueueItem>) => {
    setUploads((current) =>
      current.map((item) =>
        item.id === uploadId ? { ...item, ...patch } : item,
      ),
    );
  };

  useEffect(() => {
    const resumeIds = uploads
      .filter(
        (item) =>
          item.response &&
          aiExtractionIsInProgress(item.response.ai_extraction_status),
      )
      .map((item) => item.response!.resume_id);
    if (!resumeIds.length) return undefined;

    let cancelled = false;
    const refreshAiStatuses = async () => {
      const details = await Promise.all(
        resumeIds.map(async (resumeId) => {
          try {
            return await api.getResume(resumeId);
          } catch {
            // A transient polling failure must not turn a saved resume into an
            // upload failure. The worker will continue independently.
            return null;
          }
        }),
      );
      if (cancelled) return;
      const byResumeId = new Map(
        details
          .filter((detail): detail is ResumeDetail => detail !== null)
          .map((detail) => [detail.resume_id, detail]),
      );
      if (!byResumeId.size) return;
      setUploads((current) =>
        current.map((item) => {
          if (!item.response) return item;
          const detail = byResumeId.get(item.response.resume_id);
          if (!detail) return item;
          const response = withLatestAiExtractionStatus(item.response, detail);
          const status = uploadStatusFromResponse(response);
          if (
            item.status === status &&
            item.response.extraction_status === response.extraction_status &&
            item.response.ai_extraction_status ===
              response.ai_extraction_status &&
            item.response.ai_extraction_error === response.ai_extraction_error
          )
            return item;
          return { ...item, response, status, error: undefined };
        }),
      );
    };

    void refreshAiStatuses();
    const interval = window.setInterval(() => {
      void refreshAiStatuses();
    }, AI_STATUS_POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, [uploads]);

  const addFiles = (selectedFiles: FileList | File[]) => {
    if (uploading) return;
    const incoming = Array.from(selectedFiles);
    if (!incoming.length) return;

    const supportedFiles = incoming.filter(isSupportedResumeFile);
    const knownFiles = new Set(
      uploads.map((item) => fileFingerprint(item.file)),
    );
    const uniqueFiles = supportedFiles.filter((file) => {
      const fingerprint = fileFingerprint(file);
      if (knownFiles.has(fingerprint)) return false;
      knownFiles.add(fingerprint);
      return true;
    });
    const remainingSlots = Math.max(0, MAX_BATCH_FILES - uploads.length);
    const acceptedFiles = uniqueFiles.slice(0, remainingSlots);
    const invalidCount = incoming.length - supportedFiles.length;
    const duplicateCount = supportedFiles.length - uniqueFiles.length;
    const capacityCount = uniqueFiles.length - acceptedFiles.length;

    if (!acceptedFiles.length) {
      const reason = invalidCount
        ? "所选文件不在支持格式内。"
        : duplicateCount
          ? "这些简历已在当前队列中。"
          : `一次最多处理 ${MAX_BATCH_FILES} 份简历。`;
      notify("error", `没有加入新文件：${reason}`);
      return;
    }

    const timestamp = Date.now();
    setUploads((current) => [
      ...current,
      ...acceptedFiles.map((file, index) => ({
        id: `upload-${timestamp}-${index}-${fileFingerprint(file)}`,
        file,
        status: "queued" as const,
        idempotencyKey: createUploadIdempotencyKey(),
      })),
    ]);
    notify(
      "success",
      acceptedFiles.length === 1
        ? "已加入 1 份简历，等待上传。"
        : `已加入 ${acceptedFiles.length} 份简历，等待上传。`,
    );

    const exclusions: string[] = [];
    if (invalidCount) exclusions.push(`${invalidCount} 个不支持的文件`);
    if (duplicateCount) exclusions.push(`${duplicateCount} 份重复简历`);
    if (capacityCount) exclusions.push(`${capacityCount} 份超过本次上限`);
    if (exclusions.length)
      notify("error", `未加入：${exclusions.join("、")}。`);
  };

  const handleInputChange = (event: ChangeEvent<HTMLInputElement>) => {
    if (event.currentTarget.files) addFiles(event.currentTarget.files);
    event.currentTarget.value = "";
  };

  const handleDragEnter = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    if (uploading) return;
    dragDepthRef.current += 1;
    setDragging(true);
  };

  const handleDragLeave = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    if (uploading) return;
    dragDepthRef.current = Math.max(0, dragDepthRef.current - 1);
    if (!dragDepthRef.current) setDragging(false);
  };

  const handleDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    dragDepthRef.current = 0;
    setDragging(false);
    if (!uploading) addFiles(event.dataTransfer.files);
  };

  const runUploads = async (targets: UploadQueueItem[]) => {
    if (!targets.length) {
      notify("error", "请先选择至少一份简历。");
      return;
    }
    if (uploadLockRef.current) return;

    uploadLockRef.current = true;
    setUploading(true);
    let nextIndex = 0;
    let succeeded = 0;
    let failed = 0;

    const worker = async () => {
      while (nextIndex < targets.length) {
        const item = targets[nextIndex++];
        updateUpload(item.id, {
          status: "uploading",
          error: undefined,
          retryable: undefined,
        });
        try {
          const response = await api.uploadResume(item.file, {
            idempotencyKey: item.idempotencyKey,
          });
          succeeded += 1;
          updateUpload(item.id, {
            status: uploadStatusFromResponse(response),
            response,
            error: undefined,
          });
        } catch (error) {
          failed += 1;
          updateUpload(item.id, {
            status: "error",
            error: humanizeError(error),
            retryable: isRetryableUploadError(error),
          });
        }
      }
    };

    try {
      await Promise.all(
        Array.from(
          { length: Math.min(BATCH_UPLOAD_CONCURRENCY, targets.length) },
          worker,
        ),
      );
    } finally {
      uploadLockRef.current = false;
      setUploading(false);
    }

    if (succeeded) {
      notify(
        "success",
        succeeded === 1
          ? "简历已保存，AI 正在提取候选人姓名和结构化事实。"
          : `${succeeded} 份简历已保存，AI 正在按队列提取候选人姓名和结构化事实。`,
      );
    }
    if (failed) {
      notify(
        "error",
        failed === 1
          ? "1 份简历上传失败。请查看原因后重试。"
          : `${failed} 份简历上传失败。其余文件未受影响。`,
      );
    }
  };

  const openSuccessfulUpload = (item: UploadQueueItem) => {
    if (!item.response) return;
    onComplete(item.response.resume_id, item.response.candidate_id);
  };

  const statusText = (item: UploadQueueItem): string => {
    if (item.status === "queued") return "等待上传";
    if (item.status === "uploading") return "正在保存原件并提取文字";
    if (item.status === "extracting") {
      return item.response?.ai_extraction_status === "running"
        ? "AI 正在提取候选人姓名、教育、经历和技能"
        : "原件已保存，AI 正在排队提取候选人姓名和结构化事实";
    }
    if (item.status === "attention") {
      if (
        item.response?.extraction_status === "failed" ||
        !item.response?.parsed_page_count
      ) {
        return "原件已保存，但未读取到可用文字，暂不能 AI 提取";
      }
      if (item.response?.ai_extraction_status === "unavailable") {
        return "原件和文字已保存，等待服务器配置 AI 服务";
      }
      return "原件和文字已保存，但 AI 提取需要处理；可查看原件并重新上传。";
    }
    if (item.status === "success") {
      return item.response?.quality_flags.length
        ? "AI 已提取并启用，存在解析提示"
        : "AI 已提取并已进入筛选库";
    }
    return item.error || "上传没有完成，请重试。";
  };

  return (
    <div className="page-frame">
      <header className="page-heading">
        <div>
          <h1>批量上传简历</h1>
          <p>
            上传后会逐份保存原件、提取原生文字，并由 AI
            自动识别候选人姓名、教育、经历和技能。姓名无法可靠识别时，将保留为“未命名候选人”。
          </p>
        </div>
      </header>
      <div className="page-layout">
        <section className="panel">
          <div className="panel-heading">
            <div>
              <h2>添加候选人简历</h2>
              <p>
                可拖入多份简历或一次选择多个文件。支持 PDF、Word、图片、Excel 和 HTML。候选人姓名仅由 AI
                从简历原文识别，文件名只用于区分上传文件。
              </p>
            </div>
          </div>
          <div className="form-grid">
            <div className="span-full">
              <div
                aria-busy={uploading}
                aria-describedby="upload-dropzone-help"
                aria-label="批量简历上传区域"
                className={`dropzone${dragging ? " is-dragging" : ""}${uploading ? " is-disabled" : ""}`}
                onDragEnter={handleDragEnter}
                onDragLeave={handleDragLeave}
                onDragOver={(event) => event.preventDefault()}
                onDrop={handleDrop}
              >
                <input
                  accept=".pdf,.doc,.docx,.png,.jpg,.jpeg,.xls,.xlsx,.html,.htm,application/pdf,application/msword,application/vnd.openxmlformats-officedocument.wordprocessingml.document,application/vnd.ms-excel,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,image/png,image/jpeg,text/html"
                  disabled={uploading}
                  multiple
                  onChange={handleInputChange}
                  ref={inputRef}
                  tabIndex={-1}
                  type="file"
                />
                <div className="dropzone-inner">
                  <span className="dropzone-icon">
                    <Icon name="upload" size={25} />
                  </span>
                  <h2>
                    {uploading
                      ? `正在保存并入队：已完成 ${completedUploads.length} / ${uploads.length}`
                      : extractingUploads.length
                        ? `AI 正在处理 ${extractingUploads.length} 份简历`
                        : uploads.length
                          ? `已加入 ${uploads.length} 份简历`
                          : "拖入简历，或点击选择文件"}
                  </h2>
                  <p id="upload-dropzone-help">
                    支持 PDF、Word、图片、Excel 和 HTML。每份会单独校验、保存，并由 AI
                    从原文识别候选人姓名和结构化事实；姓名不清晰时不会使用文件名代替。
                  </p>
                  <button
                    className="button"
                    disabled={uploading}
                    onClick={() => inputRef.current?.click()}
                    type="button"
                  >
                    选择简历文件
                  </button>
                </div>
              </div>
              {uploads.length > 0 && (
                <div className="upload-queue">
                  <div className="upload-queue-header" aria-live="polite">
                    <div>
                      <strong>上传队列</strong>
                      <span>
                        {uploads.length} 份文件 · AI 处理中{" "}
                        {extractingUploads.length} · 已启用{" "}
                        {completedUploads.length} · 需处理{" "}
                        {attentionUploads.length} · 失败 {failedUploads.length}
                      </span>
                    </div>
                    {resolvedUploads.length > 0 && (
                      <button
                        className="text-button"
                        disabled={uploading}
                        onClick={() =>
                          setUploads((current) =>
                            current.filter(
                              (item) =>
                                item.status !== "success" &&
                                item.status !== "attention",
                            ),
                          )
                        }
                        type="button"
                      >
                        清除已完成
                      </button>
                    )}
                  </div>
                  <ul>
                    {uploads.map((item) => (
                      <li
                        className={`upload-file-card is-${item.status}`}
                        key={item.id}
                        role={item.status === "error" ? "alert" : undefined}
                      >
                        <Icon name="document" size={22} />
                        <div className="upload-file-main">
                          <strong title={item.file.name}>
                            {item.file.name}
                          </strong>
                          <span>
                            {formatFileSize(item.file.size)} · {resumeFileTypeLabel(item.file.name)} ·{" "}
                            {statusText(item)}
                          </span>
                        </div>
                        <div className="upload-row-actions">
                          {(item.status === "uploading" ||
                            item.status === "extracting") && (
                            <i
                              aria-label={
                                item.status === "uploading"
                                  ? "正在上传并解析"
                                  : "AI 正在提取"
                              }
                              className="spinner"
                            />
                          )}
                          {item.status === "error" &&
                            item.retryable !== false && (
                              <button
                                className="button button-ghost upload-row-button"
                                disabled={uploading}
                                onClick={() => void runUploads([item])}
                                type="button"
                              >
                                重新上传
                              </button>
                            )}
                          {item.response &&
                            item.status !== "queued" &&
                            item.status !== "uploading" && (
                              <button
                                className="button button-ghost upload-row-button"
                                onClick={() => openSuccessfulUpload(item)}
                                type="button"
                              >
                                {item.status === "extracting"
                                  ? "查看状态"
                                  : "查看简历"}
                              </button>
                            )}
                          {item.status !== "uploading" && (
                            <button
                              aria-label={`移除 ${item.file.name}`}
                              className="icon-button"
                              disabled={uploading}
                              onClick={() =>
                                setUploads((current) =>
                                  current.filter(
                                    (entry) => entry.id !== item.id,
                                  ),
                                )
                              }
                              type="button"
                            >
                              <Icon name="close" size={16} />
                            </button>
                          )}
                        </div>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          </div>
          <div className="review-actions upload-actions">
            {(uploading || queuedUploads.length > 0) && (
              <button
                className="button button-primary"
                disabled={!queuedUploads.length || uploading}
                onClick={() => void runUploads(queuedUploads)}
                type="button"
              >
                {uploading ? (
                  <>
                    <i className="spinner" />
                    正在按队列上传…
                  </>
                ) : (
                  <>
                    <Icon name="upload" size={16} />
                    上传 {queuedUploads.length} 份并自动提取
                  </>
                )}
              </button>
            )}
            {retryableFailedUploads.length > 0 && (
              <button
                className="button"
                disabled={uploading}
                onClick={() => void runUploads(retryableFailedUploads)}
                type="button"
              >
                重新上传失败项（{retryableFailedUploads.length}）
              </button>
            )}
          </div>
        </section>
        <aside className="panel">
          <div className="panel-heading">
            <div>
              <h2>批量处理路径</h2>
              <p>每一份简历独立处理，便于定位问题与补传。</p>
            </div>
          </div>
          <ol className="workflow-list">
            <li>
              <span className="workflow-step">1</span>
              <div>
                <strong>逐份保存原始文件</strong>
              <span>支持 PDF、Word、图片、Excel 和 HTML，文件质量会单独检查。</span>
              </div>
            </li>
            <li>
              <span className="workflow-step">2</span>
              <div>
                <strong>AI 识别姓名与结构化事实</strong>
                <span>
                  基于可提取的原文识别候选人姓名、教育、经历和技能；姓名不明确时保留为未命名候选人。
                </span>
              </div>
            </li>
            <li>
              <span className="workflow-step">3</span>
              <div>
                <strong>通过证据校验后自动启用</strong>
                <span>
                  AI 提取结果会直接进入筛选库；异常简历保留原件与失败状态。
                </span>
              </div>
            </li>
          </ol>
        </aside>
      </div>
    </div>
  );
}

function ScorePage({
  selected,
  notify,
  onScoreCreated,
}: {
  selected: SelectedResume | null;
  notify: (kind: ToastKind, message: string) => void;
  onScoreCreated: () => void;
}) {
  const [templates, setTemplates] = useState<ScoreTemplate[]>([]);
  const [templateId, setTemplateId] = useState("");
  const [templateName, setTemplateName] = useState("通用候选人评分");
  const [templateDescription, setTemplateDescription] = useState("");
  const [dimensions, setDimensions] = useState<TemplateDraftDimension[]>(() =>
    defaultTemplateDimensions.map((item) => ({ ...item })),
  );
  const [loadingTemplates, setLoadingTemplates] = useState(false);
  const [savingTemplate, setSavingTemplate] = useState(false);
  const [scoring, setScoring] = useState(false);
  const [startingScoreBatch, setStartingScoreBatch] = useState(false);
  const [score, setScore] = useState<ResumeScore | null>(null);
  const [scoreHistory, setScoreHistory] = useState<ResumeScore[]>([]);
  const [loadingScoreHistory, setLoadingScoreHistory] = useState(false);
  const [scoreBatch, setScoreBatch] = useState<ResumeScoreBatch | null>(null);
  const [scoreBatchItems, setScoreBatchItems] = useState<ResumeScoreBatchItem[]>([]);
  const [scoreBatchRefreshError, setScoreBatchRefreshError] = useState<string | null>(null);

  const loadTemplates = useCallback(async () => {
    setLoadingTemplates(true);
    try {
      const response = await api.listScoreTemplates();
      setTemplates(response);
      setTemplateId((current) => current || response[0]?.template_id || "");
    } catch (error) {
      notify("error", humanizeError(error));
    } finally {
      setLoadingTemplates(false);
    }
  }, [notify]);

  useEffect(() => {
    void loadTemplates();
  }, [loadTemplates]);

  const loadScoreHistory = useCallback(async () => {
    if (!selected) {
      setScore(null);
      setScoreHistory([]);
      return;
    }
    setLoadingScoreHistory(true);
    try {
      const history = await api.listScores(selected.resumeId);
      setScoreHistory(history);
      setScore((current) =>
        current && current.resume_id === selected.resumeId
          ? current
          : (history[0] ?? null),
      );
    } catch (error) {
      notify("error", humanizeError(error));
    } finally {
      setLoadingScoreHistory(false);
    }
  }, [notify, selected?.resumeId]);

  useEffect(() => {
    void loadScoreHistory();
  }, [loadScoreHistory]);

  const totalWeight = dimensions.reduce(
    (total, item) => total + Number(item.weight || 0),
    0,
  );
  const updateDimension = (
    id: string,
    patch: Partial<TemplateDraftDimension>,
  ) =>
    setDimensions((current) =>
      current.map((item) => (item.id === id ? { ...item, ...patch } : item)),
    );
  const saveTemplate = async () => {
    if (!templateName.trim()) {
      notify("error", "请填写评分规则名称。");
      return;
    }
    if (totalWeight !== 100) {
      notify("error", `评分权重当前为 ${totalWeight}，必须恰好为 100。`);
      return;
    }
    if (
      dimensions.some(
        (item) =>
          !/^[a-z][a-z0-9_]{1,63}$/.test(item.key) ||
          !item.label.trim(),
      )
    ) {
      notify("error", "每个维度都需要合法英文 key 和显示名称。");
      return;
    }
    setSavingTemplate(true);
    try {
      const created = await api.createScoreTemplate({
        name: templateName.trim(),
        description: templateDescription.trim() || undefined,
        dimensions: dimensions.map(({ id: _id, ...item }) => item),
      });
      setTemplates((current) => [created, ...current]);
      setTemplateId(created.template_id);
      notify("success", `评分规则“${created.name}”已创建。`);
    } catch (error) {
      notify("error", humanizeError(error));
    } finally {
      setSavingTemplate(false);
    }
  };
  const runScore = async () => {
    if (!selected) {
      notify("error", "请先在简历库打开一份简历。");
      return;
    }
    if (!templateId) {
      notify("error", "请先选择或创建一套评分规则。");
      return;
    }
    setScoring(true);
    try {
      const response = await api.createScore(selected.resumeId, {
        template_id: templateId,
      });
      setScore(response);
      setScoreHistory((current) => [
        response,
        ...current.filter((item) => item.score_id !== response.score_id),
      ]);
      onScoreCreated();
      notify("success", "AI 评分已完成。");
    } catch (error) {
      notify("error", humanizeError(error));
    } finally {
      setScoring(false);
    }
  };
  const runAllScores = async () => {
    if (!templateId) {
      notify("error", "请先选择或创建一套评分规则。");
      return;
    }
    setStartingScoreBatch(true);
    setScoreBatchRefreshError(null);
    try {
      const response = await api.enqueueAllResumeScores(templateId);
      setScoreBatch(response);
      setScoreBatchItems([]);
      const cachedNotice = response.cached_count
        ? `，其中 ${response.cached_count} 份复用当前评分`
        : "";
      notify(
        "success",
        `已将 ${response.total_count} 份简历加入评分队列${cachedNotice}。`,
      );
    } catch (error) {
      notify("error", humanizeError(error));
    } finally {
      setStartingScoreBatch(false);
    }
  };
  useEffect(() => {
    if (!scoreBatch) return;
    let cancelled = false;
    const refresh = async () => {
      try {
        const [next, items] = await Promise.all([
          api.getResumeScoreBatch(scoreBatch.batch_id),
          api.listResumeScoreBatchItems(scoreBatch.batch_id),
        ]);
        if (cancelled) return;
        const wasTerminal = ["completed", "partial"].includes(scoreBatch.status);
        const isTerminal = ["completed", "partial"].includes(next.status);
        setScoreBatch(next);
        setScoreBatchItems(items);
        setScoreBatchRefreshError(null);
        if (isTerminal && !wasTerminal) {
          onScoreCreated();
          void loadScoreHistory();
        }
      } catch {
        if (!cancelled) {
          setScoreBatchRefreshError("暂时无法更新进度，任务仍在服务端继续运行。");
        }
      }
    };
    void refresh();
    if (["completed", "partial"].includes(scoreBatch.status)) {
      return () => {
        cancelled = true;
      };
    }
    const timer = window.setInterval(() => void refresh(), 2000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [loadScoreHistory, onScoreCreated, scoreBatch?.batch_id, scoreBatch?.status]);
  const overrideDimension = async (
    scoreId: string,
    dimensionKey: string,
    rawScore: number,
    reason: string,
  ) => {
    try {
      const updated = await api.overrideScoreDimension(scoreId, dimensionKey, {
        raw_score: rawScore,
        reason,
      });
      setScore(updated);
      setScoreHistory((current) =>
        current.map((item) =>
          item.score_id === updated.score_id ? updated : item,
        ),
      );
      onScoreCreated();
      notify("success", "已保留人工调整和调整原因。");
    } catch (error) {
      notify("error", humanizeError(error));
      throw error;
    }
  };
  const scoreBatchIsRunning =
    scoreBatch?.status === "queued" || scoreBatch?.status === "running";

  return (
    <div className="page-frame">
      <header className="page-heading">
        <div>
          <h1>评分规则</h1>
          <p>你设定维度和权重；AI 会基于这份简历给出可追溯的评分结果。</p>
        </div>
        {selected ? (
          <span className="status-pill">
            当前简历：{selected.candidateName}
          </span>
        ) : (
          <span className="status-pill">尚未选择简历</span>
        )}
      </header>
      <div className="page-layout">
        <div>
          <section className="panel">
            <div className="panel-heading">
              <div>
                <h2>新建评分规则</h2>
                <p>权重总和必须为 100；建议将最影响岗位成功的条件权重拉高。</p>
              </div>
              <button
                className="button"
                disabled={loadingTemplates}
                onClick={() => void loadTemplates()}
                type="button"
              >
                <Icon name="refresh" size={15} />
                刷新规则
              </button>
            </div>
            <div className="form-grid">
              <div className="field-stack span-full">
                <label className="field-label" htmlFor="template-name">
                  规则名称
                </label>
                <input
                  className="field"
                  id="template-name"
                  onChange={(event) => setTemplateName(event.target.value)}
                  value={templateName}
                />
              </div>
              <div className="field-stack span-full">
                <label className="field-label" htmlFor="template-description">
                  评分说明（可选）
                </label>
                <textarea
                  className="textarea-field template-description-field"
                  id="template-description"
                  onChange={(event) => setTemplateDescription(event.target.value)}
                  placeholder="说明此规则适用的岗位、评价重点或使用边界。"
                  value={templateDescription}
                />
              </div>
            </div>
            <div className="model-list">
              {dimensions.map((dimension) => (
                <div className="model-row" key={dimension.id}>
                  <div className="dimension-main-fields">
                    <label
                      className="sr-only"
                      htmlFor={`dimension-label-${dimension.id}`}
                    >
                      维度名称
                    </label>
                    <input
                      className="field"
                      id={`dimension-label-${dimension.id}`}
                      onChange={(event) =>
                        updateDimension(dimension.id, {
                          label: event.target.value,
                        })
                      }
                      value={dimension.label}
                    />
                    <label
                      className="sr-only"
                      htmlFor={`dimension-key-${dimension.id}`}
                    >
                      维度 key
                    </label>
                    <input
                      className="field"
                      id={`dimension-key-${dimension.id}`}
                      onChange={(event) =>
                        updateDimension(dimension.id, {
                          key: event.target.value
                            .replace(/\s+/g, "_")
                            .toLowerCase(),
                        })
                      }
                      placeholder="english_key"
                      value={dimension.key}
                    />
                    <label
                      className="sr-only"
                      htmlFor={`dimension-guidance-${dimension.id}`}
                    >
                      评分指引
                    </label>
                    <textarea
                      className="textarea-field dimension-guidance-field"
                      id={`dimension-guidance-${dimension.id}`}
                      onChange={(event) =>
                        updateDimension(dimension.id, {
                          guidance: event.target.value,
                        })
                      }
                      placeholder="评分指引，例如重点核验可验证的技术深度与实际职责。"
                      value={dimension.guidance ?? ""}
                    />
                  </div>
                  <div className="dimension-numeric-fields">
                    <label
                      className="sr-only"
                      htmlFor={`dimension-weight-${dimension.id}`}
                    >
                      权重
                    </label>
                    <input
                      className="field"
                      id={`dimension-weight-${dimension.id}`}
                      max="100"
                      min="0"
                      onChange={(event) =>
                        updateDimension(dimension.id, {
                          weight: Number(event.target.value),
                        })
                      }
                      type="number"
                      value={dimension.weight}
                    />
                    <span className="dimension-numeric-hint">权重 % · 单项满分固定 100</span>
                  </div>
                  <button
                    aria-label={`删除 ${dimension.label}`}
                    className="icon-button"
                    disabled={dimensions.length <= 1}
                    onClick={() =>
                      setDimensions((current) =>
                        current.filter((item) => item.id !== dimension.id),
                      )
                    }
                    type="button"
                  >
                    <Icon name="close" size={16} />
                  </button>
                </div>
              ))}
            </div>
            <div className="review-actions">
              <button
                className="button button-ghost"
                onClick={() =>
                  setDimensions((current) => [
                    ...current,
                    {
                      id: `dimension-${Date.now()}`,
                      key: "new_dimension",
                      label: "新评分维度",
                      weight: 0,
                      guidance: "",
                    },
                  ])
                }
                type="button"
              >
                <Icon name="plus" size={15} />
                添加维度
              </button>
              <button
                className="button button-primary"
                disabled={savingTemplate}
                onClick={() => void saveTemplate()}
                type="button"
              >
                {savingTemplate ? (
                  <>
                    <i className="spinner" />
                    正在创建…
                  </>
                ) : (
                  <>
                    <Icon name="layers" size={16} />
                    创建评分规则
                  </>
                )}
              </button>
            </div>
            <div className="weight-total">
              <span>当前权重总和</span>
              <strong>{totalWeight} / 100</strong>
            </div>
          </section>
          <section className="panel">
            <div className="panel-heading">
              <div>
                <h2>运行 AI 评分</h2>
                <p>每次运行都会保留结果；更新规则后可重新评分。</p>
              </div>
            </div>
            <div className="field-stack">
              <label className="field-label" htmlFor="score-template">
                选择评分规则
              </label>
              <div className="select-wrap">
                <select
                  className="select-field"
                  id="score-template"
                  onChange={(event) => setTemplateId(event.target.value)}
                  value={templateId}
                >
                  <option value="">选择评分规则</option>
                  {templates.map((template) => (
                    <option
                      key={template.template_id}
                      value={template.template_id}
                    >
                      {template.name} · v{template.version}
                    </option>
                  ))}
                </select>
                <Icon name="chevron-down" size={16} />
              </div>
            </div>
            <div className="review-actions">
              <button
                className="button"
                disabled={!selected || !templateId || scoring || scoreBatchIsRunning}
                onClick={() => void runScore()}
                type="button"
              >
                {scoring ? (
                  <>
                    <i className="spinner" />
                    AI 正在评分…
                  </>
                ) : (
                  <>
                    <Icon name="spark" size={16} />
                    生成当前候选人评分
                  </>
                )}
              </button>
              <button
                className="button button-primary"
                disabled={!templateId || startingScoreBatch || scoreBatchIsRunning}
                onClick={() => void runAllScores()}
                type="button"
              >
                {startingScoreBatch || scoreBatchIsRunning ? (
                  <>
                    <i className="spinner" />
                    {startingScoreBatch ? "正在创建任务…" : "评分队列运行中…"}
                  </>
                ) : (
                  <>
                    <Icon name="layers" size={16} />
                    一键生成全部评分
                  </>
                )}
              </button>
            </div>
          </section>
          {scoreBatch && (
            <ScoreBatchDetails
              batch={scoreBatch}
              items={scoreBatchItems}
              refreshError={scoreBatchRefreshError}
            />
          )}
          {score && (
            <ScoreResult
              onOverride={overrideDimension}
              score={score}
            />
          )}
        </div>
        <aside className="panel">
          <div className="panel-heading">
            <div>
              <h2>现有规则</h2>
              <p>创建后可以作为任意候选人的评分基准。</p>
            </div>
          </div>
          <div className="fact-list">
            {templates.length ? (
              templates.map((template) => (
                <button
                  className="fact-row"
                  key={template.template_id}
                  onClick={() => setTemplateId(template.template_id)}
                  type="button"
                >
                  <strong>
                    {template.name} · v{template.version}
                  </strong>
                  <span>
                    {template.dimensions
                      .map((item) => `${item.label} ${item.weight}%`)
                      .join(" · ")}
                    {template.description ? ` · ${template.description}` : ""}
                  </span>
                </button>
              ))
            ) : (
              <p className="candidate-meta">还没有可用评分规则。</p>
            )}
          </div>
          {selected && (
            <section className="score-history-panel">
              <div className="panel-heading">
                <div>
                  <h2>评分历史</h2>
                  <p>每次 AI 评分和人工调整都会保留，旧结论不会被覆盖。</p>
                </div>
                <button
                  className="button button-ghost"
                  disabled={loadingScoreHistory}
                  onClick={() => void loadScoreHistory()}
                  type="button"
                >
                  {loadingScoreHistory ? <i className="spinner" /> : <Icon name="refresh" size={15} />}
                  刷新
                </button>
              </div>
              <div className="fact-list">
                {scoreHistory.length ? scoreHistory.map((item) => (
                  <button
                    className={`fact-row${score?.score_id === item.score_id ? " is-selected" : ""}`}
                    key={item.score_id}
                    onClick={() => setScore(item)}
                    type="button"
                  >
                    <strong>{item.total_score.toFixed(1)} / 100 · {item.status === "overridden" ? "含人工调整" : "AI 评分"}</strong>
                    <span>模板 v{item.template_version} · 事实 v{item.facts_version} · {formatLibraryDate(item.created_at)}</span>
                  </button>
                )) : <p className="candidate-meta">当前简历还没有评分记录。</p>}
              </div>
            </section>
          )}
        </aside>
      </div>
    </div>
  );
}

function ScoreResult({
  score,
  onOverride,
}: {
  score: ResumeScore;
  onOverride: (
    scoreId: string,
    dimensionKey: string,
    rawScore: number,
    reason: string,
  ) => Promise<void>;
}) {
  const scoreStyle = {
    "--score": Math.max(0, Math.min(100, score.total_score)),
  } as CSSProperties;
  const [editingDimensionKey, setEditingDimensionKey] = useState<string | null>(
    null,
  );
  const [draftRawScore, setDraftRawScore] = useState("");
  const [draftReason, setDraftReason] = useState("");
  const [savingOverride, setSavingOverride] = useState(false);
  const riskFlags = score.analysis.risk_flags ?? [];

  useEffect(() => {
    setEditingDimensionKey(null);
    setDraftRawScore("");
    setDraftReason("");
  }, [score.score_id]);

  const beginOverride = (dimension: ResumeScore["dimension_scores"][number]) => {
    setEditingDimensionKey(dimension.key);
    setDraftRawScore(String(dimension.final_raw_score));
    setDraftReason(dimension.manual_reason ?? "");
  };
  const saveOverride = async (
    dimension: ResumeScore["dimension_scores"][number],
  ) => {
    const rawScore = Number(draftRawScore);
    if (!Number.isFinite(rawScore) || rawScore < 0 || rawScore > 100) {
      return;
    }
    if (!draftReason.trim()) return;
    setSavingOverride(true);
    try {
      await onOverride(score.score_id, dimension.key, rawScore, draftReason.trim());
      setEditingDimensionKey(null);
    } catch {
      // The caller has already presented an actionable error message.
    } finally {
      setSavingOverride(false);
    }
  };
  return (
    <section className="panel">
      <div className="panel-heading">
        <div>
          <h2>本次评分</h2>
          <p>
            模板 v{score.template_version} · 事实 v{score.facts_version} · {score.status === "overridden" ? "含人工调整" : "AI 原始评分"}
            {!score.is_current_facts_version ? " · 简历事实已更新，请重新评分" : ""}
          </p>
        </div>
      </div>
      <div className="score-result">
        <div
          aria-label={`综合评分 ${score.total_score}`}
          className="score-number"
          data-value={score.total_score.toFixed(1)}
          style={scoreStyle}
        >
          <span>{score.total_score.toFixed(1)}</span>
        </div>
        <div className="score-dimension-list">
          {score.dimension_scores.map((dimension) => {
            const hasManualAdjustment =
              dimension.manual_reason !== null ||
              dimension.final_raw_score !== dimension.ai_raw_score;
            return (
              <div className="score-dimension-detail" key={dimension.key}>
                <div className="score-dimension">
                  <span>{dimension.label}</span>
                  <div className="score-bar">
                    <i
                      style={{
                        width: `${Math.max(0, Math.min(100, dimension.final_raw_score))}%`,
                      }}
                    />
                  </div>
                  <strong>{dimension.final_raw_score.toFixed(0)} / 100</strong>
                </div>
                <div className="score-dimension-meta">
                  <span>AI 原始分 {dimension.ai_raw_score.toFixed(0)} / 100 · 权重 {dimension.weight}%</span>
                  {hasManualAdjustment && <span className="score-manual-mark">人工调整后 {dimension.final_raw_score.toFixed(0)} / 100</span>}
                </div>
                <p className="score-dimension-rationale">{dimension.rationale || "信息不足，未提供可验证判断依据。"}</p>
                <div className="score-evidence-row">
                  <span>
                    {dimension.fact_evidence.length
                      ? `事实依据：${dimension.fact_evidence.map((fact) => fact.summary).join("；")}`
                      : "事实依据不足"}
                  </span>
                  {dimension.uncertainties.length > 0 && (
                    <span>待核实：{dimension.uncertainties.join("；")}</span>
                  )}
                </div>
                {dimension.manual_reason && (
                  <p className="score-manual-reason">人工调整原因：{dimension.manual_reason}</p>
                )}
                {editingDimensionKey === dimension.key ? (
                  <form
                    className="score-override-form"
                    onSubmit={(event) => {
                      event.preventDefault();
                      void saveOverride(dimension);
                    }}
                  >
                    <label className="field-stack">
                      <span className="field-label">人工原始分（0 至 100）</span>
                      <input
                        className="field"
                        max="100"
                        min="0"
                        onChange={(event) => setDraftRawScore(event.target.value)}
                        step="0.1"
                        type="number"
                        value={draftRawScore}
                      />
                    </label>
                    <label className="field-stack">
                      <span className="field-label">调整原因</span>
                      <textarea
                        className="textarea-field score-override-reason"
                        onChange={(event) => setDraftReason(event.target.value)}
                        placeholder="说明为什么需要调整此维度分数"
                        value={draftReason}
                      />
                    </label>
                    <div className="review-actions">
                      <button
                        className="button button-ghost"
                        disabled={savingOverride}
                        onClick={() => setEditingDimensionKey(null)}
                        type="button"
                      >
                        取消
                      </button>
                      <button
                        className="button button-primary"
                        disabled={
                          savingOverride ||
                          !draftReason.trim() ||
                          !Number.isFinite(Number(draftRawScore))
                        }
                        type="submit"
                      >
                        {savingOverride ? <><i className="spinner" />正在保存</> : <><Icon name="check" size={16} />保存人工调整</>}
                      </button>
                    </div>
                  </form>
                ) : (
                  <button
                    className="text-button score-override-button"
                    onClick={() => beginOverride(dimension)}
                    type="button"
                  >
                    人工调整此维度
                  </button>
                )}
              </div>
            );
          })}
          <div className="evidence-item">
            <b>AI 分析</b>
            {typeof score.analysis.overall_summary === "string"
              ? score.analysis.overall_summary
              : "评分已生成。请结合各维度依据完成判断。"}
          </div>
          {riskFlags.length > 0 && (
            <div className="score-risk-list">
              <b>待关注项</b>
              <ul>
                {riskFlags.map((item, index) => (
                  <li key={`${item.message}-${index}`}>
                    {item.message}
                    {item.fact_evidence.length > 0 && (
                      <small>
                        依据：{item.fact_evidence.map((fact) => fact.summary).join("；")}
                      </small>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {score.audit_trail.length > 0 && (
            <div className="score-audit-list">
              <b>人工调整记录</b>
              <ul>
                {score.audit_trail.map((entry) => (
                  <li key={entry.audit_id}>
                    <strong>{entry.dimension_key ?? "评分维度"}</strong>
                    <span>
                      {entry.previous_final_raw_score ?? "—"} → {entry.final_raw_score ?? "—"} · {entry.reason ?? "未填写原因"}
                    </span>
                    <small>{entry.actor} · {formatLibraryDate(entry.created_at)}</small>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      </div>
    </section>
  );
}

function ScoreBatchDetails({
  batch,
  items,
  refreshError,
}: {
  batch: ResumeScoreBatch;
  items: ResumeScoreBatchItem[];
  refreshError: string | null;
}) {
  const failed = items.filter((item) => item.status === "failed");
  const inProgress = items.filter(
    (item) => item.status === "queued" || item.status === "running",
  );
  const isTerminal = ["completed", "partial"].includes(batch.status);
  const statusLabel =
    batch.status === "partial"
      ? "部分完成"
      : batch.status === "completed"
        ? "已完成"
        : batch.status === "queued"
          ? "等待处理"
          : "运行中";
  return (
    <section className="panel match-batch-details score-batch-details" aria-live="polite">
      <div className="panel-heading">
        <div>
          <h2>批量评分任务</h2>
          <p>
            {batch.completed_count + batch.failed_count} / {batch.total_count} 已结束
            {batch.cached_count ? `，${batch.cached_count} 份已复用当前评分` : ""}
            {inProgress.length ? `，仍有 ${inProgress.length} 份在队列中` : ""}。
          </p>
        </div>
        <span className={`status-pill${batch.failed_count ? " is-warning" : ""}`}>
          {statusLabel}
        </span>
      </div>
      {refreshError && (
        <p className="library-error" role="alert">
          {refreshError}
        </p>
      )}
      {failed.length ? (
        <div className="table-scroll">
          <table className="candidate-table batch-failure-table">
            <thead>
              <tr>
                <th scope="col">候选人</th>
                <th scope="col">事实版本</th>
                <th scope="col">尝试次数</th>
                <th scope="col">失败原因</th>
              </tr>
            </thead>
            <tbody>
              {failed.map((item) => (
                <tr key={item.item_id}>
                  <td>{item.candidate_display_name?.trim() || "未命名候选人"}</td>
                  <td>v{item.facts_version}</td>
                  <td>{item.attempt_count}</td>
                  <td>{item.last_error || "未知错误"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : batch.failed_count ? (
        <p className="library-error">任务报告了失败项，正在读取具体原因。</p>
      ) : (
        <p className="candidate-meta">
          {isTerminal
            ? "本批简历均已完成评分。"
            : "评分在服务端队列中运行，当前页面可以继续处理其他工作。"}
        </p>
      )}
    </section>
  );
}

function MatchPage({
  selected,
  notify,
  onOpenMatchedResume,
}: {
  selected: SelectedResume | null;
  notify: (kind: ToastKind, message: string) => void;
  onOpenMatchedResume: (match: JobMatch) => void;
}) {
  const [title, setTitle] = useState("");
  const [jobBrief, setJobBrief] = useState("");
  const [jdText, setJdText] = useState("");
  const [editedGeneratedJd, setEditedGeneratedJd] = useState(false);
  const [generatedRequirements, setGeneratedRequirements] =
    useState<JobRequirements | null>(null);
  const [generationError, setGenerationError] = useState<string | null>(null);
  const [jobWorkspaceMode, setJobWorkspaceMode] =
    useState<JobWorkspaceMode>("create");
  const [jobVersion, setJobVersion] = useState<JobVersion | null>(null);
  const [versioningJobId, setVersioningJobId] = useState<string | null>(null);
  const [confirmedJobVersions, setConfirmedJobVersions] = useState<JobVersion[]>([]);
  const [loading, setLoading] = useState(false);
  const [match, setMatch] = useState<JobMatch | null>(null);
  const [matchBatch, setMatchBatch] = useState<JobMatchBatch | null>(null);
  const [batchItems, setBatchItems] = useState<JobMatchBatchItem[]>([]);
  const [jobMatches, setJobMatches] = useState<JobMatch[]>([]);
  const [matchesLoading, setMatchesLoading] = useState(false);
  const resetJobAuthoring = () => {
    setTitle("");
    setJobBrief("");
    setJdText("");
    setEditedGeneratedJd(false);
    setGeneratedRequirements(null);
    setGenerationError(null);
  };
  const selectJobVersion = (next: JobVersion) => {
    resetJobAuthoring();
    setJobWorkspaceMode("view");
    setJobVersion(next);
    setVersioningJobId(null);
    setMatch(null);
    setMatchBatch(null);
    setBatchItems([]);
    setBatchItems([]);
  };
  const beginNewJob = () => {
    resetJobAuthoring();
    setJobWorkspaceMode("create");
    setJobVersion(null);
    setVersioningJobId(null);
    setMatch(null);
    setMatchBatch(null);
    setBatchItems([]);
    setJobMatches([]);
  };
  const beginNextJobVersion = () => {
    if (!jobVersion) return;
    setJobWorkspaceMode("create");
    setVersioningJobId(jobVersion.job_id);
    setTitle(jobVersion.title);
    setJobBrief(jobVersion.raw_text);
    setJdText("");
    setEditedGeneratedJd(false);
    setGeneratedRequirements(null);
    setGenerationError(null);
    setMatch(null);
    setMatchBatch(null);
    setBatchItems([]);
  };
  const requirementsAreReady = (requirements: JobRequirements | null) =>
    Boolean(
      requirements &&
        ((requirements.must_have?.some((item) => item.trim()) ?? false) ||
          (requirements.preferred?.some((item) => item.trim()) ?? false)),
    );
  const updateGeneratedRequirement = (
    priority: "must_have" | "preferred",
    index: number,
    value: string,
  ) => {
    setGeneratedRequirements((current) => {
      const next = {
        must_have: [...(current?.must_have ?? [])],
        preferred: [...(current?.preferred ?? [])],
      };
      next[priority][index] = value;
      return next;
    });
  };
  const addGeneratedRequirement = (priority: "must_have" | "preferred") => {
    setGeneratedRequirements((current) => ({
      must_have: [...(current?.must_have ?? [])],
      preferred: [...(current?.preferred ?? [])],
      [priority]: [...(current?.[priority] ?? []), ""],
    }));
  };
  const removeGeneratedRequirement = (
    priority: "must_have" | "preferred",
    index: number,
  ) => {
    setGeneratedRequirements((current) => ({
      must_have: (current?.must_have ?? []).filter((_, itemIndex) => itemIndex !== (priority === "must_have" ? index : -1)),
      preferred: (current?.preferred ?? []).filter((_, itemIndex) => itemIndex !== (priority === "preferred" ? index : -1)),
    }));
  };
  const invalidateGeneratedRequirements = () => {
    if (!generatedRequirements) return;
    setGeneratedRequirements(null);
    setGenerationError("JD 已修改，请重新运行 AI 生成后再启用岗位。");
  };
  const generateJobDescription = async () => {
    const sourceText =
      (editedGeneratedJd && jdText.trim()) || jobBrief.trim();
    if (!title.trim() || !sourceText) {
      notify("error", "请填写岗位名称和岗位需求后再生成 JD。");
      return;
    }
    setGenerationError(null);
    setLoading(true);
    try {
      const generated = await api.generateJobDescription({
        title: title.trim(),
        brief: sourceText,
      });
      const generatedJd = generated.jd_text?.trim();
      if (!generatedJd) {
        throw new Error("AI 未返回可编辑的 JD");
      }
      setTitle(generated.title?.trim() || title.trim());
      setJdText(generatedJd);
      setEditedGeneratedJd(false);
      setGeneratedRequirements(generated.requirements ?? null);
      if (!requirementsAreReady(generated.requirements ?? null)) {
        setGenerationError(
          "AI 已生成 JD，但没有返回可用于匹配的条件。请补充岗位需求后重新生成。",
        );
        notify("error", "AI 未生成可用的匹配条件，请补充需求后重试。");
        return;
      }
      notify("success", "AI 已生成 JD 和匹配条件。确认内容后即可启用岗位。");
    } catch (error) {
      const message = humanizeError(error);
      setGenerationError(message);
      notify("error", message);
    } finally {
      setLoading(false);
    }
  };
  const enableJob = async () => {
    if (!title.trim() || !jdText.trim()) {
      notify("error", "请先生成或粘贴完整 JD。");
      return;
    }
    if (!requirementsAreReady(generatedRequirements)) {
      const message = "JD 已修改或匹配条件不完整，请先重新运行 AI 生成。";
      setGenerationError(message);
      notify("error", message);
      return;
    }
    setGenerationError(null);
    setLoading(true);
    try {
      const requirements = generatedRequirements
        ? {
            must_have: (generatedRequirements.must_have ?? [])
              .map((item) => item.trim())
              .filter(Boolean),
            preferred: (generatedRequirements.preferred ?? [])
              .map((item) => item.trim())
              .filter(Boolean),
          }
        : undefined;
      const payload = {
        title: title.trim(),
        jd_text: jdText.trim(),
        requirements,
      };
      const created = versioningJobId
        ? await api.createJobVersion(versioningJobId, payload)
        : await api.createJob(payload);
      if (created.status !== "confirmed") {
        const message =
          "岗位已保存，但服务尚未返回可启用版本。请重新生成 JD 后再试。";
        setGenerationError(message);
        notify("error", message);
        return;
      }
      setConfirmedJobVersions((current) => [
        created,
        ...current.filter((item) => item.job_version_id !== created.job_version_id),
      ]);
      selectJobVersion(created);
      notify("success", "岗位已启用，现在可以开始匹配简历。");
    } catch (error) {
      const message = humanizeError(error);
      setGenerationError(message);
      notify("error", message);
    } finally {
      setLoading(false);
    }
  };
  const publishOriginalJob = async () => {
    const originalSourceText =
      editedGeneratedJd && jdText.trim() ? jdText : jobBrief;
    if (!title.trim() || !originalSourceText.trim()) {
      notify("error", "请填写岗位名称和完整原版 JD 后再发布。");
      return;
    }
    setGenerationError(null);
    setLoading(true);
    try {
      const published = await api.publishOriginalJob({
        title: title.trim(),
        // This deliberately retains every valid character entered in the JD.
        // The endpoint performs validation without normalizing the source text.
        jd_text: originalSourceText,
      });
      setConfirmedJobVersions((current) => [
        published,
        ...current.filter(
          (item) => item.job_version_id !== published.job_version_id,
        ),
      ]);
      selectJobVersion(published);
      notify("success", "原版 JD 已发布，内容未经过 AI 处理。");
    } catch (error) {
      const message = humanizeError(error);
      setGenerationError(message);
      notify("error", message);
    } finally {
      setLoading(false);
    }
  };
  const runMatch = async () => {
    if (!selected) {
      notify("error", "请先在筛选工作台打开一份已启用简历。");
      return;
    }
    if (!jobVersion || jobVersion.status !== "confirmed") {
      notify("error", "请先启用岗位，再运行匹配。");
      return;
    }
    if (!jobVersion.requirements.length) {
      notify("error", "原版 JD 未生成匹配条件，不能运行 AI 匹配。");
      return;
    }
    setLoading(true);
    try {
      const response = await api.runJobMatch(selected.resumeId, {
        job_version_id: jobVersion.job_version_id,
      });
      setMatch(response);
      setJobMatches((current) => [
        response,
        ...current.filter((item) => item.resume_id !== response.resume_id),
      ]);
      notify("success", "岗位匹配已完成，结果已绑定岗位与简历的事实版本。");
    } catch (error) {
      notify("error", humanizeError(error));
    } finally {
      setLoading(false);
    }
  };
  const runAllMatches = async () => {
    if (!jobVersion || jobVersion.status !== "confirmed") {
      notify("error", "请先启用岗位，再批量匹配简历。");
      return;
    }
    if (!jobVersion.requirements.length) {
      notify("error", "原版 JD 未生成匹配条件，不能批量运行 AI 匹配。");
      return;
    }
    setLoading(true);
    try {
      const response = await api.enqueueAllJobMatches(
        jobVersion.job_version_id,
      );
      setMatchBatch(response);
      setBatchItems([]);
      notify(
        "success",
        `已将 ${response.total_count} 份简历加入岗位匹配队列。`,
      );
    } catch (error) {
      notify("error", humanizeError(error));
    } finally {
      setLoading(false);
    }
  };
  useEffect(() => {
    if (!matchBatch) return;
    let cancelled = false;
    const refresh = async () => {
      try {
        const [next, items] = await Promise.all([
          api.getJobMatchBatch(matchBatch.batch_id),
          api.listJobMatchBatchItems(matchBatch.batch_id),
        ]);
        if (!cancelled) {
          setMatchBatch(next);
          setBatchItems(items);
        }
      } catch {
        // Keep the last durable status visible; the next manual action can retry.
      }
    };
    void refresh();
    if (["completed", "partial"].includes(matchBatch.status)) {
      return () => {
        cancelled = true;
      };
    }
    const timer = window.setInterval(() => void refresh(), 2000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [matchBatch?.batch_id, matchBatch?.status]);
  useEffect(() => {
    let cancelled = false;
    void api
      .listConfirmedJobVersions()
      .then((versions) => {
        if (cancelled) return;
        setConfirmedJobVersions(versions);
        if (versions[0]) selectJobVersion(versions[0]);
      })
      .catch(() => {
        // A new workspace has no confirmed JD yet. The creation form remains usable.
      });
    return () => {
      cancelled = true;
    };
  }, []);
  useEffect(() => {
    if (
      !jobVersion ||
      jobVersion.status !== "confirmed" ||
      !jobVersion.requirements.length
    ) {
      setJobMatches([]);
      return;
    }
    let cancelled = false;
    setMatchesLoading(true);
    void api
      .listJobVersionMatches(jobVersion.job_version_id)
      .then((items) => {
        if (!cancelled) setJobMatches(items);
      })
      .catch((error) => {
        if (!cancelled) notify("error", humanizeError(error));
      })
      .finally(() => {
        if (!cancelled) setMatchesLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [jobVersion?.job_version_id, jobVersion?.status, matchBatch?.completed_count, notify]);
  const jobIsEnabled =
    jobWorkspaceMode === "view" && jobVersion?.status === "confirmed";
  const jobIsOriginal = Boolean(
    jobIsEnabled && jobVersion && jobVersion.requirements.length === 0,
  );
  const jobCanMatch = Boolean(
    jobIsEnabled && jobVersion && jobVersion.requirements.length > 0,
  );
  const generatedJobIsReady = requirementsAreReady(generatedRequirements);
  return (
    <div className="page-frame">
      <header className="page-heading">
        <div>
          <h1>岗位 JD 匹配</h1>
          <p>
            描述岗位需求，由 AI 生成可编辑 JD 和匹配条件；启用后即可对已核验的简历事实逐项比对。
          </p>
        </div>
        {selected ? (
          <span className="status-pill">候选人：{selected.candidateName}</span>
        ) : (
          <span className="status-pill">尚未选择候选人</span>
        )}
      </header>
      <div className="page-layout">
        <div>
          <section className="panel">
            {confirmedJobVersions.length > 0 && (
              <div className="jd-switcher">
                <div>
                  <span className="field-label">已保存的岗位 JD</span>
                  <p>切换后将显示该岗位自己的候选人匹配结果。</p>
                </div>
                <div className="select-wrap jd-switcher-select">
                  <select
                    aria-label="切换已保存的岗位 JD"
                    className="select-field"
                    onChange={(event) => {
                      if (!event.target.value) {
                        beginNewJob();
                        return;
                      }
                      const next = confirmedJobVersions.find(
                        (item) => item.job_version_id === event.target.value,
                      );
                      if (next) selectJobVersion(next);
                    }}
                    value={
                      jobWorkspaceMode === "view"
                        ? (jobVersion?.job_version_id ?? "")
                        : ""
                    }
                  >
                    <option value="">新建岗位 JD</option>
                    {confirmedJobVersions.map((item) => (
                      <option key={item.job_version_id} value={item.job_version_id}>
                        {item.title} · v{item.version}
                        {!item.requirements.length ? " · 原版" : ""}
                      </option>
                    ))}
                  </select>
                  <Icon name="chevron-down" size={15} />
                </div>
              </div>
            )}
            {jobWorkspaceMode === "view" && jobVersion ? (
              <div className="field-stack">
                <div className="panel-heading">
                  <div>
                    <h2>{jobVersion.title}</h2>
                    <p>
                      {jobIsOriginal
                        ? "原版内容已发布，未调用 AI，也不包含用于简历匹配的条件。"
                        : "已启用，当前匹配结果仅基于这份岗位 JD。"}
                    </p>
                  </div>
                  <div className="jd-view-actions">
                    <span className="status-pill">
                      {jobIsOriginal ? "原版已发布" : "已启用"}
                    </span>
                    <button
                      className="button button-ghost"
                      onClick={beginNextJobVersion}
                      type="button"
                    >
                      <Icon name="plus" size={15} />
                      基于此新建版本
                    </button>
                  </div>
                </div>
                <label className="field-label" htmlFor="active-job-text">
                  岗位 JD 原文
                </label>
                <textarea
                  aria-label="当前已启用岗位的 JD 原文"
                  className="textarea-field"
                  id="active-job-text"
                  readOnly
                  value={jobVersion.raw_text}
                />
              </div>
            ) : (
              <>
                {versioningJobId && (
                  <p className="version-context" role="status">
                    正在基于当前岗位创建新版本。原版本和已有匹配结果会完整保留。
                  </p>
                )}
                <div className="jd-steps">
                  <span className={`jd-step${jdText ? " is-done" : " is-current"}`}>
                    1 描述需求
                  </span>
                  <span
                    className={`jd-step${generatedJobIsReady ? " is-done" : jdText ? " is-current" : ""}`}
                  >
                    2 编辑 JD
                  </span>
                  <span className={`jd-step${generatedJobIsReady ? " is-current" : ""}`}>
                    3 启用匹配
                  </span>
                </div>
                <div className="form-grid">
                  <div className="field-stack span-full">
                    <label className="field-label" htmlFor="job-title">
                      岗位名称
                    </label>
                    <input
                      className="field"
                      id="job-title"
                      onChange={(event) => {
                        invalidateGeneratedRequirements();
                        setTitle(event.target.value);
                      }}
                      placeholder="例如：大模型应用架构师"
                      value={title}
                    />
                  </div>
                  <div className="field-stack span-full">
                    <label className="field-label" htmlFor="job-brief">
                      岗位需求或完整 JD
                    </label>
                    <textarea
                      className="textarea-field"
                      id="job-brief"
                      onChange={(event) => {
                        setJobBrief(event.target.value);
                        setGenerationError(null);
                        if (jdText) {
                          setJdText("");
                          setEditedGeneratedJd(false);
                          setGeneratedRequirements(null);
                        }
                      }}
                      placeholder="填写岗位需求后点击「AI 生成 JD」；已有完整 JD 可直接粘贴后点击「原版发布」。"
                      value={jobBrief}
                    />
                    <p className="candidate-meta">
                      AI 生成 JD 会提取匹配条件，原版发布会按当前内容原样保存。
                    </p>
                  </div>
                  {jdText && (
                    <div className="field-stack span-full">
                      <label className="field-label" htmlFor="job-text">
                        AI 生成的 JD
                      </label>
                      <textarea
                        className="textarea-field"
                        id="job-text"
                        onChange={(event) => {
                          invalidateGeneratedRequirements();
                          setEditedGeneratedJd(true);
                          setJdText(event.target.value);
                        }}
                        value={jdText}
                      />
                      <p className="candidate-meta">
                        可以直接编辑。编辑后请重新生成，以同步用于匹配的 AI 条件。
                      </p>
                    </div>
                  )}
                </div>
                {generationError && (
                  <p className="library-error" role="alert">
                    {generationError}
                  </p>
                )}
                <div className="review-actions">
                  <button
                    className={`button${generatedJobIsReady ? " button-ghost" : " button-primary"}`}
                    disabled={loading}
                    onClick={() => void generateJobDescription()}
                    type="button"
                  >
                    {loading ? (
                      <>
                        <i className="spinner" />
                        正在生成…
                      </>
                    ) : (
                      <>
                        <Icon name="spark" size={16} />
                        {jdText ? "重新生成 JD" : "生成 JD"}
                      </>
                    )}
                  </button>
                  {!generatedJobIsReady && (
                    <button
                      className="button"
                      disabled={loading}
                      onClick={() => void publishOriginalJob()}
                      type="button"
                    >
                      <Icon name="briefcase" size={16} />
                      原版发布
                    </button>
                  )}
                  {jdText && (
                    <button
                      className="button button-primary"
                      disabled={loading || !generatedJobIsReady}
                      onClick={() => void enableJob()}
                      type="button"
                    >
                      <Icon name="check" size={16} />
                      {versioningJobId ? "发布新版本" : "启用岗位"}
                    </button>
                  )}
                </div>
              </>
            )}
          </section>
          {jobWorkspaceMode === "create" && generatedJobIsReady && (
            <section className="panel">
              <div className="panel-heading">
                <div>
                  <h2>AI 识别的匹配条件</h2>
                  <p>可以直接修订条件与优先级，发布后会固化为该 JD 版本的匹配依据。</p>
                </div>
              </div>
              <div className="requirements-list">
                {(generatedRequirements?.must_have ?? []).map((requirement, index) => (
                  <div className="requirement-row" key={`must-${index}-${requirement}`}>
                    <span className="priority-must">必须</span>
                    <input
                      aria-label={`第 ${index + 1} 条必须条件`}
                      className="field requirement-input"
                      onChange={(event) => updateGeneratedRequirement("must_have", index, event.target.value)}
                      value={requirement}
                    />
                    <button
                      aria-label={`删除第 ${index + 1} 条必须条件`}
                      className="icon-button requirement-remove"
                      onClick={() => removeGeneratedRequirement("must_have", index)}
                      type="button"
                    >
                      <Icon name="close" size={15} />
                    </button>
                  </div>
                ))}
                {(generatedRequirements?.preferred ?? []).map((requirement, index) => (
                  <div className="requirement-row" key={`preferred-${index}-${requirement}`}>
                    <span className="priority-preferred">优先</span>
                    <input
                      aria-label={`第 ${index + 1} 条优先条件`}
                      className="field requirement-input"
                      onChange={(event) => updateGeneratedRequirement("preferred", index, event.target.value)}
                      value={requirement}
                    />
                    <button
                      aria-label={`删除第 ${index + 1} 条优先条件`}
                      className="icon-button requirement-remove"
                      onClick={() => removeGeneratedRequirement("preferred", index)}
                      type="button"
                    >
                      <Icon name="close" size={15} />
                    </button>
                  </div>
                ))}
              </div>
              <div className="requirement-actions">
                <button className="button button-ghost" onClick={() => addGeneratedRequirement("must_have")} type="button">
                  <Icon name="plus" size={15} /> 添加必须条件
                </button>
                <button className="button button-ghost" onClick={() => addGeneratedRequirement("preferred")} type="button">
                  <Icon name="plus" size={15} /> 添加优先条件
                </button>
              </div>
            </section>
          )}
          {jobIsOriginal && (
            <section className="panel">
              <div className="panel-heading">
                <div>
                  <h2>原版发布</h2>
                  <p>已按原文发布，未调用 AI，未生成用于简历匹配的条件。</p>
                </div>
              </div>
            </section>
          )}
          {jobCanMatch && jobVersion && (
            <section className="panel">
              <div className="panel-heading">
                <div>
                  <h2>当前岗位的匹配条件</h2>
                  <p>这些条件已随岗位启用，并用于当前的简历匹配。</p>
                </div>
              </div>
              <div className="requirements-list">
                {jobVersion.requirements.map((requirement) => (
                  <div className="requirement-row" key={requirement.requirement_id}>
                    <span
                      className={
                        requirement.priority === "must_have"
                          ? "priority-must"
                          : "priority-preferred"
                      }
                    >
                      {requirement.priority === "must_have" ? "必须" : "优先"}
                    </span>
                    <p>{requirement.raw_requirement}</p>
                    <span className="candidate-meta">{requirement.category}</span>
                  </div>
                ))}
              </div>
            </section>
          )}
          {match && <MatchResult match={match} />}
          {matchBatch && (
            <MatchBatchDetails batch={matchBatch} items={batchItems} />
          )}
          {jobCanMatch && (
            <MatchLeaderboard
              loading={matchesLoading}
              matches={jobMatches}
              onOpenResume={onOpenMatchedResume}
            />
          )}
        </div>
        <aside className="panel">
          <div className="panel-heading">
            <div>
              <h2>匹配操作</h2>
              <p>
                {jobIsOriginal
                  ? "原版发布未生成匹配条件，因此不会调用 AI 匹配。"
                  : "启用岗位后，可对当前候选人或全部简历运行 AI 匹配。"}
              </p>
            </div>
          </div>
          <div className="fact-list">
            <div className="fact-row">
              <strong>当前岗位</strong>
              <span>
                {jobIsEnabled && jobVersion
                  ? `${jobVersion.title} · v${jobVersion.version} · ${jobIsOriginal ? "原版已发布" : "已启用"}`
                  : "尚未启用"}
              </span>
            </div>
            <div className="fact-row">
              <strong>当前候选人</strong>
              <span>{selected?.candidateName ?? "请先从筛选结果打开"}</span>
            </div>
          </div>
          {matchBatch && (
            <div className="fact-row">
              <strong>AI 批量进度</strong>
              <span>
                {matchBatch.completed_count + matchBatch.failed_count} / {matchBatch.total_count}
                {matchBatch.failed_count ? ` · 失败 ${matchBatch.failed_count}` : ""}
              </span>
            </div>
          )}
          <div className="review-actions">
            <button
              className="button"
              disabled={!jobCanMatch || loading}
              onClick={() => void runAllMatches()}
              type="button"
            >
              <Icon name="spark" size={16} />
              批量匹配全部简历
            </button>
            <button
              className="button button-primary"
              disabled={
                !selected || !jobCanMatch || loading
              }
              onClick={() => void runMatch()}
              type="button"
            >
              {loading && jobCanMatch ? (
                <>
                  <i className="spinner" />
                  正在匹配…
                </>
              ) : (
                <>
                  <Icon name="match" size={16} />
                  运行岗位匹配
                </>
              )}
            </button>
          </div>
        </aside>
      </div>
    </div>
  );
}

type MatchLane = "recommended" | "pending" | "unmet";

const hardRequirementLabel: Record<string, string> = {
  pass: "硬条件通过",
  unmet: "硬条件未满足",
  information_insufficient: "硬条件待核实",
  not_applicable: "无硬条件",
};

function clampMatchPercent(value: number): number {
  return Math.max(0, Math.min(100, value));
}

/**
 * A completed match may have been created before the server returned the
 * evidence-normalized score. Keep old results readable while never presenting
 * their legacy, coverage-weighted total as a JD match percentage.
 */
function matchConfidence(match: JobMatch): number {
  const value = match.match_confidence ?? match.evidence_coverage ?? 0;
  return typeof value === "number" && Number.isFinite(value)
    ? clampMatchPercent(value)
    : 0;
}

function matchScore(match: JobMatch): number {
  if (
    typeof match.match_score === "number" &&
    Number.isFinite(match.match_score)
  ) {
    return clampMatchPercent(match.match_score);
  }

  const confidence = matchConfidence(match);
  if (!confidence || !Number.isFinite(match.total_score)) return 0;
  return clampMatchPercent((match.total_score / confidence) * 100);
}

function matchLane(match: JobMatch): MatchLane {
  if (
    match.match_lane === "recommended" ||
    match.match_lane === "pending" ||
    match.match_lane === "unmet"
  ) {
    return match.match_lane;
  }

  if (match.hard_requirement_status === "unmet") return "unmet";
  if (
    matchConfidence(match) >= 60 &&
    (match.hard_requirement_status === "pass" ||
      match.hard_requirement_status === "not_applicable")
  ) {
    return "recommended";
  }
  return "pending";
}

function compareMatchesByNewest(left: JobMatch, right: JobMatch): number {
  const leftTime = Date.parse(left.created_at);
  const rightTime = Date.parse(right.created_at);
  const timeDifference =
    (Number.isFinite(rightTime) ? rightTime : 0) -
    (Number.isFinite(leftTime) ? leftTime : 0);
  if (timeDifference) return timeDifference;
  return right.match_id.localeCompare(left.match_id);
}

function MatchResult({ match }: { match: JobMatch }) {
  const jdMatchScore = matchScore(match);
  const confidence = matchConfidence(match);
  const hardStatus = match.hard_requirement_status ?? "unknown";
  const scoreStyle = {
    "--score": jdMatchScore,
  } as CSSProperties;
  return (
    <section className="panel">
      <div className="panel-heading">
        <div>
          <h2>匹配结果</h2>
          <p>
            岗位版本 {match.job_version} · 简历事实版本 {match.facts_version} ·{" "}
            {hardRequirementLabel[hardStatus] ?? "待检查硬性要求"}
          </p>
        </div>
      </div>
      <div className="score-result match-result-layout">
        <div className="match-result-score-panel">
          <div
            aria-label={`JD 匹配度 ${jdMatchScore.toFixed(1)}%，匹配可信度 ${confidence.toFixed(1)}%`}
            className="score-number"
            data-value={`${jdMatchScore.toFixed(1)}%`}
            style={scoreStyle}
          >
            <span>{jdMatchScore.toFixed(1)}%</span>
          </div>
          <p className="match-result-score-label">JD 匹配度</p>
          <div className="match-result-confidence">
            <span>匹配可信度</span>
            <strong>{confidence.toFixed(1)}%</strong>
          </div>
          <span className={`match-hard-status is-${hardStatus}`}>
            {hardRequirementLabel[hardStatus] ?? "待确认"}
          </span>
        </div>
        <div className="requirements-list">
          {match.requirement_results.map((item) => (
            <div className="requirement-row" key={item.requirement_id}>
              <span className={`outcome-${item.outcome.replace("_", "-")}`}>
                {item.outcome === "met"
                  ? "已满足"
                  : item.outcome === "partial"
                    ? "部分满足"
                    : item.outcome === "not_met"
                      ? "未满足"
                      : "待确认"}
              </span>
              <p>
                <b>{item.requirement_text}</b>
                <br />
                {item.reason}
                {item.missing_or_uncertain
                  ? ` · ${item.missing_or_uncertain}`
                  : ""}
                <small className="match-fact-reference">
                  {item.fact_ids.length
                    ? `事实依据：${item.fact_ids.join("、")}`
                    : "未发现可验证的简历事实"}
                </small>
              </p>
              <span
                className={
                  item.priority === "must_have"
                    ? "priority-must"
                    : "priority-preferred"
                }
              >
                {item.priority === "must_have" ? "必须" : "优先"}
              </span>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

function MatchBatchDetails({
  batch,
  items,
}: {
  batch: JobMatchBatch;
  items: JobMatchBatchItem[];
}) {
  const failed = items.filter((item) => item.status === "failed");
  const inProgress = items.filter(
    (item) => item.status === "queued" || item.status === "running",
  );
  return (
    <section className="panel match-batch-details">
      <div className="panel-heading">
        <div>
          <h2>批量匹配任务</h2>
          <p>
            {batch.completed_count + batch.failed_count} / {batch.total_count} 已结束
            {inProgress.length ? `，仍有 ${inProgress.length} 份在队列中` : ""}。
          </p>
        </div>
        <span className={`status-pill${batch.failed_count ? " is-warning" : ""}`}>
          {batch.status === "partial" ? "部分完成" : batch.status === "completed" ? "已完成" : "运行中"}
        </span>
      </div>
      {failed.length ? (
        <div className="table-scroll">
          <table className="candidate-table batch-failure-table">
            <thead>
              <tr>
                <th scope="col">候选人</th>
                <th scope="col">事实版本</th>
                <th scope="col">尝试次数</th>
                <th scope="col">失败原因</th>
              </tr>
            </thead>
            <tbody>
              {failed.map((item) => (
                <tr key={item.item_id}>
                  <td>{item.candidate_display_name?.trim() || "未命名候选人"}</td>
                  <td>v{item.facts_version}</td>
                  <td>{item.attempt_count}</td>
                  <td>{item.last_error || "未知错误"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : batch.failed_count ? (
        <p className="library-error">任务报告了失败项，正在读取具体原因。</p>
      ) : (
        <p className="candidate-meta">
          {batch.status === "completed"
            ? "本批简历均已完成匹配。"
            : "失败项会在任务结束后显示具体原因。"}
        </p>
      )}
    </section>
  );
}

function MatchLeaderboard({
  matches,
  loading,
  onOpenResume,
}: {
  matches: JobMatch[];
  loading: boolean;
  onOpenResume: (match: JobMatch) => void;
}) {
  const [collapsedLanes, setCollapsedLanes] = useState<Record<MatchLane, boolean>>({
    recommended: false,
    pending: false,
    unmet: false,
  });
  const latestByResume = new Map<string, JobMatch>();
  const newestFirst = [...matches].sort(compareMatchesByNewest);
  for (const match of newestFirst) {
    if (!latestByResume.has(match.resume_id)) latestByResume.set(match.resume_id, match);
  }
  const ranked = [...latestByResume.values()].sort((left, right) => {
    const scoreDifference = matchScore(right) - matchScore(left);
    if (scoreDifference) return scoreDifference;
    const confidenceDifference = matchConfidence(right) - matchConfidence(left);
    if (confidenceDifference) return confidenceDifference;
    return compareMatchesByNewest(left, right);
  });
  const lanes: Record<MatchLane, JobMatch[]> = {
    recommended: [],
    pending: [],
    unmet: [],
  };
  for (const item of ranked) lanes[matchLane(item)].push(item);
  const laneDefinitions: Array<{
    key: MatchLane;
    title: string;
    description: string;
    empty: string;
    icon: IconName;
  }> = [
    {
      key: "recommended",
      title: "推荐候选人",
      description: "可信度 ≥ 60%，硬性条件已通过或不适用",
      empty: "暂无满足推荐条件的候选人。",
      icon: "check",
    },
    {
      key: "pending",
      title: "待核实候选人",
      description: "关键项待核实，或匹配可信度不足 60%",
      empty: "暂无需要补充核实的候选人。",
      icon: "match",
    },
    {
      key: "unmet",
      title: "明确不匹配",
      description: "至少一项硬性条件已有明确不满足的证据",
      empty: "暂无明确不满足硬性条件的候选人。",
      icon: "close",
    },
  ];
  return (
    <section className="panel match-leaderboard">
      <div className="panel-heading">
        <div>
          <h2>候选人匹配工作区</h2>
          <p>JD 匹配度仅按已确认信息计算，匹配可信度表示可验证条件的覆盖程度。</p>
        </div>
        <span className="status-pill">{ranked.length} 份已完成</span>
      </div>
      {loading ? (
        <div
          aria-busy="true"
          aria-label="正在加载候选人匹配结果"
          className="match-lanes match-lanes-loading"
        >
          {laneDefinitions.map((lane) => (
            <div className="match-lane" key={lane.key}>
              <div className="match-lane-heading">
                <div className="match-lane-title">
                  <span className="skeleton match-lane-icon-skeleton" />
                  <div>
                    <div className="skeleton match-lane-title-skeleton" />
                    <div className="skeleton match-lane-description-skeleton" />
                  </div>
                </div>
              </div>
              <div className="match-lane-skeleton-list">
                <span className="skeleton" />
                <span className="skeleton" />
                <span className="skeleton" />
              </div>
            </div>
          ))}
        </div>
      ) : ranked.length ? (
        <div className="match-lanes">
          {laneDefinitions.map((lane) => {
            const items = lanes[lane.key];
            const isCollapsed = collapsedLanes[lane.key];
            const laneContentId = `match-lane-${lane.key}-content`;
            return (
              <section
                aria-labelledby={`match-lane-${lane.key}-heading`}
                className={`match-lane is-${lane.key}`}
                key={lane.key}
              >
                <div className="match-lane-heading">
                  <div className="match-lane-title">
                    <span className="match-lane-icon">
                      <Icon name={lane.icon} size={16} />
                    </span>
                    <div>
                      <h3 id={`match-lane-${lane.key}-heading`}>{lane.title}</h3>
                      <p>{lane.description}</p>
                    </div>
                  </div>
                  <div className="match-lane-actions">
                    <span aria-label={`${lane.title} ${items.length} 份`} className="match-lane-count">
                      {items.length}
                    </span>
                    <button
                      aria-controls={laneContentId}
                      aria-expanded={!isCollapsed}
                      className="text-button match-lane-collapse"
                      onClick={() =>
                        setCollapsedLanes((current) => ({
                          ...current,
                          [lane.key]: !current[lane.key],
                        }))
                      }
                      type="button"
                    >
                      <span>{isCollapsed ? "展开" : "收起"}</span>
                      <Icon name="chevron-down" size={14} />
                    </button>
                  </div>
                </div>
                {!isCollapsed && (
                  <div
                    aria-live="polite"
                    className="match-lane-content"
                    id={laneContentId}
                  >
                    {items.length ? (
                      <ol className="match-candidate-list">
                        {items.map((item) => (
                          <li key={item.match_id}>
                            <MatchLaneCandidate
                              match={item}
                              onOpenResume={onOpenResume}
                            />
                          </li>
                        ))}
                      </ol>
                    ) : (
                      <p className="match-lane-empty">{lane.empty}</p>
                    )}
                  </div>
                )}
              </section>
            );
          })}
        </div>
      ) : (
        <div className="empty-state match-empty-state">
          <div className="empty-state-inner">
            <span className="empty-glyph"><Icon name="match" size={23} /></span>
            <h2>尚未生成匹配结果</h2>
            <p>确认 JD 后，点击“批量匹配全部简历”即可在此查看排序结果。</p>
          </div>
        </div>
      )}
    </section>
  );
}

function MatchLaneCandidate({
  match,
  onOpenResume,
}: {
  match: JobMatch;
  onOpenResume: (match: JobMatch) => void;
}) {
  const jdMatchScore = matchScore(match);
  const confidence = matchConfidence(match);
  const hardStatus = match.hard_requirement_status ?? "unknown";
  const met = match.requirement_results.filter(
    (result) => result.outcome === "met",
  ).length;
  const partial = match.requirement_results.filter(
    (result) => result.outcome === "partial",
  ).length;
  const unknown = match.requirement_results.filter(
    (result) => result.outcome === "unknown",
  ).length;
  return (
    <article className="match-candidate-card">
      <div className="match-candidate-heading">
        <div>
          <strong>{match.candidate_display_name?.trim() || "未命名候选人"}</strong>
          <small>简历事实 v{match.facts_version}</small>
        </div>
        <span className={`match-hard-status is-${hardStatus}`}>
          {hardRequirementLabel[hardStatus] ?? "待确认"}
        </span>
      </div>
      <dl className="match-candidate-metrics">
        <div>
          <dt>JD 匹配度</dt>
          <dd>{jdMatchScore.toFixed(1)}%</dd>
        </div>
        <div>
          <dt>匹配可信度</dt>
          <dd>{confidence.toFixed(1)}%</dd>
        </div>
      </dl>
      <div className="match-candidate-overview">
        <span>满足 {met}</span>
        <span>部分满足 {partial}</span>
        {unknown > 0 && <span>待核实 {unknown}</span>}
      </div>
      <button
        className="button button-ghost match-open-button"
        onClick={() => onOpenResume(match)}
        type="button"
      >
        <Icon name="document" size={15} />
        查看简历
      </button>
    </article>
  );
}

function CandidateRequired({
  title,
  description,
  actionLabel,
  onAction,
}: {
  title: string;
  description: string;
  actionLabel?: string;
  onAction?: () => void;
}) {
  return (
    <div className="page-frame">
      <section className="panel">
        <div className="empty-state">
          <div className="empty-state-inner">
            <span className="empty-glyph">
              <Icon name="user" size={23} />
            </span>
            <h2>{title}需要一份当前简历</h2>
            <p>{description}</p>
            {onAction && (
              <button
                className="button button-primary"
                onClick={onAction}
                type="button"
              >
                <Icon name="filter" size={16} />
                {actionLabel ?? "前往筛选工作台"}
              </button>
            )}
          </div>
        </div>
      </section>
    </div>
  );
}

function ToastRegion({
  toasts,
  onDismiss,
}: {
  toasts: ToastMessage[];
  onDismiss: (id: number) => void;
}) {
  return (
    <div aria-live="polite" className="toast-region">
      {toasts.map((toast) => (
        <div className={`toast is-${toast.kind}`} key={toast.id} role="status">
          <Icon name={toast.kind === "success" ? "check" : "close"} size={18} />
          <span>{toast.message}</span>
          <button
            aria-label="关闭提示"
            className="icon-button"
            onClick={() => onDismiss(toast.id)}
            type="button"
          >
            <Icon name="close" size={14} />
          </button>
        </div>
      ))}
    </div>
  );
}

export default App;
