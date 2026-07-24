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
  type RefObject,
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
  CandidateSearchDisplayFieldKey,
  CandidateDataAuditEvent,
  CandidateDataDeletionBatch,
  CandidateDataDeletionReason,
  CandidateDataExport,
  CandidateDataRetentionCleanupRun,
  CandidateDataRetentionMode,
  CandidateDataRetentionPolicy,
  CandidateDataRetentionPreview,
  CandidateSearchItem,
  CandidateSearchRequest,
  CandidateSearchResponse,
  AwardLevel,
  DegreeLevel,
  ExperienceType,
  FilterOptions,
  InstitutionClassification,
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
  MailboxConfig,
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
  RecruitingAgentSearchSummary,
  RecruitingAgentTurn,
  SavedFilter,
  ScoreDimensionCreateInput,
  ScoreTemplate,
  TalentSearchHardFilters,
  TalentSearchProfile,
  TalentSearchProfileMatchResult,
  TalentSearchRun,
  TrialAccess,
} from "./types";
import { Icon, type IconName } from "./icons";
import { TableSkeleton } from "./backoffice/ui/TableSkeleton";
import { formatFileSize, formatLibraryDate } from "./backoffice/utils/formatters";
import { MailboxPage } from "./features/mailbox/MailboxPage";
import { mailboxImportErrorMessages } from "./features/mailbox/mailbox-model";

const AdminApp = lazy(() => import("./admin/AdminApp"));
const BackofficeUiProvider = lazy(() =>
  import("./backoffice/BackofficeUiProvider").then(({ BackofficeUiProvider: Provider }) => ({
    default: Provider,
  })),
);

type View = "library" | "filter" | "upload" | "score" | "match" | "settings";
type MainWorkspaceView = Exclude<View, "settings">;
type SettingsSection = "mailbox" | "data";
type DrawerTab = "original" | "summary" | "score" | "evidence";
type MatchMode = "all" | "any";
type KeywordMode = "broad" | "precise";
type ToastKind = "success" | "error";
type JobWorkspaceMode = "create" | "view";
type AuthRoute = "login" | "register" | "forgot-password" | "reset-password" | "verify-email";
type AppSurface =
  | { kind: "landing" }
  | { kind: "platform" }
  | { kind: "workspace"; authRoute: AuthRoute | null };

interface FilterDraft {
  minEmploymentMonths: number;
  minEmploymentOrInternshipMonths: number;
  degrees: DegreeLevel[];
  institutionClassifications: InstitutionClassification[];
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

interface TemplateDraftDimension extends ScoreDimensionCreateInput {
  id: string;
}

interface ScoreTemplatePreset {
  id: string;
  name: string;
  description: string;
  dimensions: ScoreDimensionCreateInput[];
}

const emptySearch: CandidateSearchResponse = {
  items: [],
  next_cursor: null,
  needs_review_count: 0,
  total_count: 0,
};

const fallbackRegistrationOffer: RegistrationOffer = {
  plan_code: "advanced",
  plan_name: "进阶版",
  trial_days: 30,
  llm_call_limit: 1000,
};

const wholeNumberFormatter = new Intl.NumberFormat("zh-CN", {
  maximumFractionDigits: 0,
});

function formatWholeNumber(value: number): string {
  return wholeNumberFormatter.format(Math.max(0, Math.trunc(value)));
}

function accountAvatarInitials(displayName: string | null): string | null {
  const normalizedName = displayName?.trim();
  if (!normalizedName) return null;

  const hanCharacters = Array.from(normalizedName).filter((character) =>
    /[\u3400-\u9fff]/.test(character),
  );
  if (hanCharacters.length > 0) return hanCharacters.slice(0, 2).join("");

  const words = normalizedName.split(/\s+/).filter(Boolean);
  const initials = words
    .slice(0, 2)
    .map((word) => Array.from(word)[0]?.toUpperCase() ?? "")
    .join("");
  return initials || null;
}

const defaultFilterDraft: FilterDraft = {
  minEmploymentMonths: 0,
  minEmploymentOrInternshipMonths: 0,
  degrees: [],
  institutionClassifications: [],
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

const institutionClassificationOptions: Array<{
  value: InstitutionClassification;
  label: string;
}> = [
  { value: "985", label: "985" },
  { value: "211", label: "211" },
  { value: "undergraduate", label: "本科" },
  { value: "associate", label: "大专" },
  { value: "secondary_vocational", label: "中专" },
  { value: "overseas", label: "海外院校" },
];

const institutionClassificationLabels: Record<InstitutionClassification, string> =
  Object.fromEntries(
    institutionClassificationOptions.map((option) => [option.value, option.label]),
  ) as Record<InstitutionClassification, string>;

const legacyInstitutionTierLabels: Record<InstitutionTier, string> = {
  ...institutionClassificationLabels,
  "211": "211",
  "985": "985",
  double_first_class: "双一流",
  key_undergraduate: "重本",
  first_tier: "一本",
  second_tier: "二本",
  regular_undergraduate: "普通本科",
  private_undergraduate: "民办本科",
  higher_vocational: "高职/高专",
  overseas: "海外院校",
};

/**
 * A small subset of historical tiers is semantically identical to a new
 * classification. Everything else must be reselected rather than widened.
 */
const legacyTierClassificationMap: Partial<
  Record<InstitutionTier, InstitutionClassification[]>
> = {
  "985": ["985"],
  // The product now defines 211 as 211-only. A legacy saved "211" condition
  // therefore adopts the explicit new meaning instead of silently widening
  // back to 985 candidates.
  "211": ["211"],
  regular_undergraduate: ["undergraduate"],
  higher_vocational: ["associate"],
  overseas: ["overseas"],
};

const fallbackFilterOptions: FilterOptions = {
  schema_version: "filter-options.v2.fallback",
  degrees: degreeOptions,
  institution_classifications: institutionClassificationOptions,
  institution_tiers: [],
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

const scoreTemplatePresets: ScoreTemplatePreset[] = [
  {
    id: "general-screening",
    name: "通用候选人初筛",
    description: "适用于尚未绑定具体 JD 的首轮简历筛选，只根据可验证的简历事实评分。",
    dimensions: [
      {
        label: "技能匹配",
        weight: 40,
        guidance: "重点看核心技能、工具与目标岗位场景的可验证匹配。",
      },
      {
        label: "经历深度",
        weight: 35,
        guidance: "重点看工作或实习职责范围、成果、复杂度与持续时间。",
      },
      {
        label: "教育与基础条件",
        weight: 25,
        guidance: "重点看明确记载的学历、专业及岗位必要的基础条件。",
      },
    ],
  },
  {
    id: "technical-screening",
    name: "技术岗位初筛",
    description: "适用于软件、算法、数据与 AI 等技术岗位，重点识别真实工程与项目实践。",
    dimensions: [
      {
        label: "核心技术匹配",
        weight: 40,
        guidance: "重点看岗位所需语言、框架、模型、工具和技术方向的明确记录。",
      },
      {
        label: "项目与工程实践",
        weight: 35,
        guidance: "重点看项目职责、技术方案、交付结果、复杂问题和工程规模。",
      },
      {
        label: "技术深度与成长证据",
        weight: 25,
        guidance: "重点看明确记载的性能优化、质量保障、研究、开源或持续技术投入。",
      },
    ],
  },
  {
    id: "business-screening",
    name: "销售与业务岗位初筛",
    description: "适用于销售、商务与业务拓展岗位，重点关注可验证的客户场景和业务成果。",
    dimensions: [
      {
        label: "行业与客户场景匹配",
        weight: 30,
        guidance: "重点看明确记载的行业、客户类型、产品或业务场景是否相关。",
      },
      {
        label: "业绩与目标达成",
        weight: 45,
        guidance: "重点看明确记载的营收、签约、增长、目标完成或其他业务结果。",
      },
      {
        label: "业务推进与协作",
        weight: 25,
        guidance: "重点看客户推进、跨团队协作、渠道或项目协调等可验证职责。",
      },
    ],
  },
];

const defaultScoreTemplatePreset = scoreTemplatePresets[0];
let templateDraftDimensionSequence = 0;

function createTemplateDraftDimensions(
  preset: Pick<ScoreTemplatePreset, "dimensions">,
): TemplateDraftDimension[] {
  return preset.dimensions.map((dimension) => ({
    ...dimension,
    id: `score-dimension-${Date.now()}-${++templateDraftDimensionSequence}`,
  }));
}

const navigation: Array<{ view: MainWorkspaceView; label: string; icon: IconName }> = [
  { view: "library", label: "简历库", icon: "folder" },
  { view: "filter", label: "筛选工作台", icon: "filter" },
  { view: "upload", label: "上传简历", icon: "upload" },
  { view: "score", label: "评分模板", icon: "layers" },
  { view: "match", label: "招聘详情", icon: "match" },
];

function settingsSectionFromHash(hash: string): SettingsSection | null {
  const value = hash
    .replace(/^#/, "")
    .trim()
    .replace(/^\/+|\/+$/g, "")
    .toLowerCase();

  if (value === "settings/mailbox" || value === "inbox") return "mailbox";
  if (value === "settings/data" || value === "data") return "data";
  return null;
}

function settingsHash(section: SettingsSection): string {
  return `#settings/${section}`;
}

const AUTO_FILTER_SEARCH_DELAY_MS = 350;

function freshDefaultFilter(): FilterDraft {
  return {
    ...defaultFilterDraft,
    degrees: [],
    institutionClassifications: [],
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

/**
 * The result table must describe the request that produced the rows, rather
 * than the controls a recruiter may be editing for their next search.  Keep a
 * shallow object copy plus copies of every mutable collection so the applied
 * request remains stable while the left-hand form changes.
 */
function snapshotFilterDraft(draft: FilterDraft): FilterDraft {
  return {
    ...draft,
    degrees: [...draft.degrees],
    institutionClassifications: [...draft.institutionClassifications],
    experienceTypes: [...draft.experienceTypes],
    experienceAwardLevels: [...draft.experienceAwardLevels],
    skills: [...draft.skills],
    skillCategories: [...draft.skillCategories],
    languageCredentials: [...draft.languageCredentials],
    languageScores: { ...draft.languageScores },
    scholarshipLevels: [...draft.scholarshipLevels],
    leadershipContexts: [...draft.leadershipContexts],
    leadershipRoles: [...draft.leadershipRoles],
    keywords: [...draft.keywords],
  };
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
      password_reset_invalid_or_expired: "这条重置链接无效、已过期或已被使用。请重新申请新的链接。",
      trial_expired: "试用期已结束。数据已保留，请联系 GreatSell AI 团队继续使用。",
      trial_llm_call_quota_exhausted:
        "本工作区的 1,000 次试用大模型调用已用完。数据仍会保留，请联系 GreatSell AI 团队继续使用。",
      organization_access_suspended: "当前工作区暂不可用，请联系 GreatSell AI 团队。",
      invalid_admin_token: "管理口令无效。请在右上角连接配置中更新后重试。",
      server_missing_admin_token: "服务器尚未配置管理口令，暂时无法访问。",
      deepseek_api_key_not_configured:
        "AI 服务尚未配置。请先在服务器环境变量中配置后重试。",
      talent_search_profile_not_found: "这份人才画像已不存在或无法访问。",
      talent_search_run_not_found: "这次找人记录已不存在或无法访问。",
      talent_search_profile_not_draft: "这份画像已不是可确认草案，请刷新后查看当前版本。",
      talent_search_profile_not_confirmed: "请先确认你当前看到的人才画像，再开始找人。",
      talent_search_profile_revision_not_current:
        "人才画像已在其他位置更新。请刷新并确认最新草案后再操作。",
      talent_search_profile_revision_superseded:
        "这版画像已被新的草案替代，请查看最新版本。",
      talent_search_profile_revision_missing: "人才画像版本不完整，请重新生成一版草案。",
      talent_search_profile_invalid_cursor: "候选人列表位置已失效，请点击刷新重新查看。",
      talent_search_profile_match_target_invalid:
        "这份画像的核验配置异常，请重新生成并确认后再找人。",
      talent_search_profile_provider_failed:
        "AI 人才画像暂时不可用，请稍后重新生成。",
      talent_search_profile_response_truncated:
        "本次需求较长，AI 未能完整生成画像。请精简后重试。",
      talent_search_profile_service_unavailable:
        "人才画像服务暂时不可用，请稍后重试。",
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
      candidate_data_file_access_purpose_invalid: "原件访问方式无效，请重新发起查看或下载。",
      candidate_data_file_access_not_found: "这次原件访问已失效，请重新发起查看或下载。",
      candidate_data_session_nonce_missing: "当前登录会话已更新，请刷新页面后重新操作。",
      candidate_data_deletion_batch_not_found: "这条删除记录已不存在或无法访问。",
      candidate_data_deletion_batch_not_restorable: "这条删除记录当前不能恢复。",
      candidate_data_recovery_window_closed: "恢复期限已结束，原始数据已进入清理流程。",
      candidate_data_retention_policy_invalid: "保留策略参数无效，请检查后重试。",
      candidate_data_retention_preview_stale: "预览结果已过期，请重新预览后再保存策略。",
      candidate_data_export_not_found: "这项导出任务已不存在或无法访问。",
      candidate_data_export_not_cancellable: "这项导出任务当前不能取消。",
      candidate_data_export_download_not_found: "导出文件不可用或已过期，请重新创建导出。",
      candidate_data_export_candidate_selection_invalid: "请选择一位或多位可访问的候选人后再导出。",
      candidate_data_export_snapshot_unavailable: "导出所需的候选人快照已不可用，请重新创建导出。",
      candidate_data_export_original_unavailable: "部分原始文件不可用，无法创建包含原件的导出。",
      candidate_data_export_original_bytes_exceeded: "原始文件总量超过本次导出上限，请改为不含原件导出。",
      ...mailboxImportErrorMessages,
      score_template_not_found: "评分模板不存在，请重新选择。",
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
  // HTML is accepted as an extraction source, never as browser-previewable
  // content. The API also forces it to an opaque attachment; keeping it out
  // of this branch prevents a future response-policy regression from turning
  // a candidate-controlled document into a same-origin preview.
  return [".pdf", ".png", ".jpg", ".jpeg"].includes(extension);
}

function fileFingerprint(file: File): string {
  return `${file.name.toLocaleLowerCase()}-${file.size}-${file.lastModified}`;
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

function formatDuration(months: number): string {
  if (months <= 0) return "0 个月";
  const years = Math.floor(months / 12);
  const rest = months % 12;
  return rest ? `${years} 年 ${rest} 个月` : `${years} 年`;
}

function formatMinimumDuration(months: number): string {
  return months <= 0 ? "不限" : formatDuration(months);
}

function resolvedInstitutionClassificationOptions(
  filterOptions: FilterOptions,
): Array<{ value: InstitutionClassification; label: string }> {
  const labels = new Map(
    filterOptions.institution_classifications?.map((option) => [
      option.value,
      option.label,
    ]),
  );
  return institutionClassificationOptions.map((option) => ({
    ...option,
    label: labels.get(option.value) || option.label,
  }));
}

function institutionClassificationLabel(
  classification: InstitutionClassification,
): string {
  return institutionClassificationLabels[classification];
}

function sortInstitutionClassifications(
  classifications: readonly InstitutionClassification[] | null | undefined,
): InstitutionClassification[] {
  const order = new Map(
    institutionClassificationOptions.map((option, index) => [option.value, index]),
  );
  return [...new Set(classifications ?? [])].sort(
    (left, right) => (order.get(left) ?? Number.MAX_SAFE_INTEGER) -
      (order.get(right) ?? Number.MAX_SAFE_INTEGER),
  );
}

function draftToSearchRequest(
  draft: FilterDraft,
  cursor: string | null = null,
  scoreTemplateId: string | null = null,
): CandidateSearchRequest {
  const request: CandidateSearchRequest = {
    schema_version: "candidate_filter.v2",
    limit: 50,
    cursor,
  };

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
    draft.institutionClassifications.length ||
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
        institution_classifications_any_of: draft.institutionClassifications,
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
  if (scoreTemplateId) request.score_template_id = scoreTemplateId;
  return request;
}

type SavedFilterDraftResult =
  | { draft: FilterDraft; error: null }
  | { draft: null; error: string };

function savedInstitutionClassifications(
  request: CandidateSearchRequest,
): { classifications: InstitutionClassification[]; error: string | null } {
  const education = request.education_any_of?.[0];
  const currentClassifications =
    education?.institution_classifications_any_of ?? [];
  const legacyTiers = education?.institution_tiers_any_of ?? [];

  if (request.is_985_211 === false) {
    return {
      classifications: [],
      error: "该历史筛选含有已下线的“非 985/211”条件，无法无损迁移。请重新设置院校类型后保存。",
    };
  }

  const unsupportedTiers = legacyTiers.filter(
    (tier) => !legacyTierClassificationMap[tier],
  );
  if (unsupportedTiers.length) {
    return {
      classifications: [],
      error: `该历史筛选包含已下线的院校层级（${unsupportedTiers
        .map((tier) => legacyInstitutionTierLabels[tier])
        .join("、")}），无法无损迁移。请重新设置院校类型后保存。`,
    };
  }

  if (currentClassifications.length) {
    if (legacyTiers.length) {
      return {
        classifications: [],
        error: "该历史筛选同时包含新旧院校条件，无法无损迁移。请重新设置院校类型后保存。",
      };
    }
    if (
      request.is_985_211 === true &&
      currentClassifications.some(
        (classification) => classification !== "985" && classification !== "211",
      )
    ) {
      return {
        classifications: [],
        error: "该历史筛选同时包含旧版 985/211 与其他院校条件，无法无损迁移。请重新设置院校类型后保存。",
      };
    }
    return {
      classifications: sortInstitutionClassifications(currentClassifications),
      error: null,
    };
  }

  if (request.is_985_211 === true && legacyTiers.some(
    (tier) => tier !== "985" && tier !== "211",
  )) {
    return {
      classifications: [],
      error: "该历史筛选同时包含旧版 985/211 与其他院校条件，无法无损迁移。请重新设置院校类型后保存。",
    };
  }

  // A saved tier and the old top-level flag were combined with AND. When a
  // tier is present it is therefore more specific than the old aggregate flag.
  const classifications = legacyTiers.length
    ? legacyTiers.flatMap((tier) => legacyTierClassificationMap[tier] ?? [])
    : request.is_985_211 === true
      ? (["985", "211"] as InstitutionClassification[])
      : [];
  return {
    classifications: sortInstitutionClassifications(classifications),
    error: null,
  };
}

function searchRequestToDraft(
  request: CandidateSearchRequest,
): SavedFilterDraftResult {
  const education = request.education_any_of?.[0];
  const experience = request.experience_any_of?.[0];
  const savedDegrees = request.highest_degree_in ?? education?.degree_in ?? [];
  const institutionMigration = savedInstitutionClassifications(request);
  if (institutionMigration.error) {
    return { draft: null, error: institutionMigration.error };
  }
  return {
    draft: {
      minEmploymentMonths: request.min_employment_months ?? 0,
      minEmploymentOrInternshipMonths:
        request.min_employment_or_internship_months ?? 0,
      degrees: savedDegrees.filter((degree) => degree !== "unknown"),
      institutionClassifications: institutionMigration.classifications,
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
    },
    error: null,
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
    if (["/login", "/register", "/forgot-password", "/reset-password"].includes(normalizedPath)) {
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
  if (normalized === "/reset-password") return "reset-password";
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

  return (
    <Suspense
      fallback={(
        <main className="login-page" aria-live="polite">
          <div className="login-panel login-redirect-panel">
            <i className="spinner" /> 正在打开招聘工作台…
          </div>
        </main>
      )}
    >
      <BackofficeUiProvider>
        <WorkspaceApp authRoute={surface.authRoute} />
      </BackofficeUiProvider>
    </Suspense>
  );
}

function WorkspaceApp({ authRoute }: { authRoute: AuthRoute | null }) {
  const [view, setView] = useState<View>(() =>
    settingsSectionFromHash(window.location.hash) ? "settings" : "library",
  );
  const [settingsSection, setSettingsSection] = useState<SettingsSection>(
    () => settingsSectionFromHash(window.location.hash) ?? "mailbox",
  );
  const [filterDraft, setFilterDraft] =
    useState<FilterDraft>(freshDefaultFilter);
  const [appliedFilter, setAppliedFilter] =
    useState<FilterDraft>(freshDefaultFilter);
  const [filterOptions, setFilterOptions] = useState<FilterOptions>(
    fallbackFilterOptions,
  );
  const [scoreTemplates, setScoreTemplates] = useState<ScoreTemplate[]>([]);
  const [scoreTemplateId, setScoreTemplateId] = useState<string | null>(null);
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
  const [pdfDownloadLoading, setPdfDownloadLoading] = useState(false);
  const [pdfError, setPdfError] = useState<string | null>(null);
  const [summaries, setSummaries] = useState<ResumeSummary[]>([]);
  const [summaryLoading, setSummaryLoading] = useState(false);
  const [drawerScores, setDrawerScores] = useState<ResumeScore[]>([]);
  const [drawerScoreLoading, setDrawerScoreLoading] = useState(false);
  const [drawerScoreError, setDrawerScoreError] = useState<string | null>(null);
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
  const filterDraftRef = useRef(filterDraft);
  const appliedFilterRef = useRef(appliedFilter);
  const scoreTemplateIdRef = useRef<string | null>(null);
  const searchRequestRef = useRef(0);
  const scheduledFilterSearchRef = useRef<number | null>(null);
  const reviewRequestRef = useRef(0);
  const summaryRequestRef = useRef(0);
  const drawerScoreRequestRef = useRef(0);
  const originalFileRequestRef = useRef(0);
  const originalFileRevokeRef = useRef<(() => void) | null>(null);
  const agentTriggerRef = useRef<HTMLButtonElement | null>(null);

  const canManageMailbox =
    authSession?.role === "admin" &&
    authSession.plan?.feature_flags.mailbox_import === true;
  const canManageCandidateData = authSession?.role === "admin";
  const canManageSettings = canManageMailbox || canManageCandidateData;
  const canGenerateAiJd =
    authSession?.role === "admin" &&
    authSession.plan?.feature_flags.ai_jd_generation === true;

  const updateSettingsHash = useCallback(
    (section: SettingsSection | null, replace = false) => {
      const nextHash = section ? settingsHash(section) : "";
      if (window.location.hash === nextHash) return;
      const nextLocation = `${window.location.pathname}${window.location.search}${nextHash}`;
      if (replace) {
        window.history.replaceState(window.history.state, "", nextLocation);
      } else {
        window.history.pushState(window.history.state, "", nextLocation);
      }
    },
    [],
  );

  const navigateToView = useCallback(
    (nextView: MainWorkspaceView) => {
      setView(nextView);
      updateSettingsHash(null);
    },
    [updateSettingsHash],
  );

  const openSettings = useCallback(
    (section: SettingsSection) => {
      setSettingsSection(section);
      setView("settings");
      updateSettingsHash(section);
    },
    [updateSettingsHash],
  );

  const closeAgent = useCallback(() => {
    setAgentOpen(false);
    window.requestAnimationFrame(() => agentTriggerRef.current?.focus());
  }, []);

  const replaceFilterDraft = useCallback((next: FilterDraft) => {
    filterDraftRef.current = next;
    setFilterDraft(next);
  }, []);

  const cancelScheduledFilterSearch = useCallback(() => {
    if (scheduledFilterSearchRef.current === null) return;
    window.clearTimeout(scheduledFilterSearchRef.current);
    scheduledFilterSearchRef.current = null;
  }, []);

  const replaceAppliedFilter = useCallback((next: FilterDraft) => {
    const snapshot = snapshotFilterDraft(next);
    appliedFilterRef.current = snapshot;
    setAppliedFilter(snapshot);
  }, []);

  const replaceScoreTemplateId = useCallback((next: string | null) => {
    scoreTemplateIdRef.current = next;
    setScoreTemplateId(next);
  }, []);

  const selectedResumeId = selectedResume?.resumeId ?? null;

  const releaseOriginalFile = useCallback((clearError = true) => {
    originalFileRequestRef.current += 1;
    originalFileRevokeRef.current?.();
    originalFileRevokeRef.current = null;
    setPdfUrl(null);
    setPdfLoading(false);
    if (clearError) setPdfError(null);
  }, []);

  useEffect(() => {
    const titles: Record<AuthRoute, string> = {
      login: "登录｜GreatSell AI 招聘工具",
      register: "免费试用｜GreatSell AI 招聘工具",
      "forgot-password": "找回密码｜GreatSell AI 招聘工具",
      "reset-password": "设置新密码｜GreatSell AI 招聘工具",
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

  const applyAuthSession = useCallback((session: AuthSession) => {
    setAuthSession(session);
    setAuthState(session.authenticated ? "authenticated" : "unauthenticated");
  }, []);

  // The verification email can be opened in another browser or device. The
  // registration tab keeps its own signed session, so polling the current
  // session is enough to learn that the user record was verified elsewhere.
  const refreshAuthSession = useCallback(async (): Promise<AuthSession | null> => {
    try {
      const session = await api.getAuthSession();
      applyAuthSession(session);
      return session;
    } catch {
      // A transient refresh failure must not log out a person who is simply
      // waiting for their verification email. The initial session bootstrap
      // below still handles a genuine unauthenticated start safely.
      return null;
    }
  }, [applyAuthSession]);

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
      selectedScoreTemplateId: string | null = scoreTemplateIdRef.current,
    ) => {
      const requestId = ++searchRequestRef.current;
      setSearching(true);
      try {
        const response = await api.searchCandidates(
          draftToSearchRequest(draft, cursor, selectedScoreTemplateId),
        );
        if (requestId !== searchRequestRef.current) return;
        setSearch((current) => ({
          ...response,
          items: append
            ? [...current.items, ...response.items]
            : response.items,
        }));
        if (!append) replaceAppliedFilter(draft);
      } catch (error) {
        if (requestId === searchRequestRef.current) {
          notify("error", humanizeError(error));
        }
      } finally {
        if (requestId === searchRequestRef.current) setSearching(false);
      }
    },
    [notify, replaceAppliedFilter],
  );

  const updateFilterDraft = useCallback(
    (next: FilterDraft, timing: "immediate" | "debounced" = "immediate") => {
      replaceFilterDraft(next);
      cancelScheduledFilterSearch();
      if (timing === "immediate") {
        void runSearch(next);
        return;
      }

      // Invalidate an in-flight result while the recruiter keeps typing, so
      // an older request cannot briefly present rows for stale conditions.
      searchRequestRef.current += 1;
      setSearching(true);
      scheduledFilterSearchRef.current = window.setTimeout(() => {
        scheduledFilterSearchRef.current = null;
        void runSearch(next);
      }, AUTO_FILTER_SEARCH_DELAY_MS);
    },
    [cancelScheduledFilterSearch, replaceFilterDraft, runSearch],
  );

  useEffect(
    () => () => cancelScheduledFilterSearch(),
    [cancelScheduledFilterSearch],
  );

  const registerScoreTemplate = useCallback(
    (template: ScoreTemplate) => {
      setScoreTemplates((current) => [
        template,
        ...current.filter((item) => item.template_id !== template.template_id),
      ]);
      replaceScoreTemplateId(template.template_id);
      void runSearch(
        appliedFilterRef.current,
        false,
        null,
        template.template_id,
      );
    },
    [replaceScoreTemplateId, runSearch],
  );

  const handleScoreCreated = useCallback(() => {
    refreshLibraryScores();
    void runSearch(appliedFilterRef.current);
  }, [refreshLibraryScores, runSearch]);

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

  const loadDrawerScores = useCallback(
    async (resumeId: string) => {
      const requestId = ++drawerScoreRequestRef.current;
      setDrawerScoreLoading(true);
      setDrawerScoreError(null);
      try {
        const response = await api.listScores(resumeId);
        if (requestId === drawerScoreRequestRef.current) {
          setDrawerScores(response);
        }
      } catch (error) {
        if (requestId === drawerScoreRequestRef.current) {
          setDrawerScores([]);
          setDrawerScoreError(humanizeError(error));
        }
      } finally {
        if (requestId === drawerScoreRequestRef.current) {
          setDrawerScoreLoading(false);
        }
      }
    },
    [],
  );

  useEffect(() => {
    void api
      .getAuthSession()
      .then((session) => {
        applyAuthSession(session);
      })
      .catch(() => {
        setAuthSession(null);
        setAuthState("unauthenticated");
      });
  }, [applyAuthSession]);

  // Large-model usage can change in a background worker, an Agent turn, or a
  // second browser tab. Refresh the small server-owned trial snapshot while
  // the workspace is open so the visible allowance does not drift for long.
  useEffect(() => {
    if (
      authState !== "authenticated" ||
      authRoute ||
      authSession?.email_verification_required ||
      authSession?.trial?.plan_status !== "trial"
    ) {
      return;
    }
    const refreshOnFocus = () => {
      if (document.visibilityState === "visible") void refreshAuthSession();
    };
    const intervalId = window.setInterval(refreshOnFocus, 60_000);
    window.addEventListener("focus", refreshOnFocus);
    return () => {
      window.clearInterval(intervalId);
      window.removeEventListener("focus", refreshOnFocus);
    };
  }, [
    authRoute,
    authSession?.email_verification_required,
    authSession?.trial?.plan_status,
    authState,
    refreshAuthSession,
  ]);

  useEffect(() => {
    if (
      authState !== "authenticated" ||
      authRoute ||
      authSession?.email_verification_required
    )
      return;
    void runSearch(defaultFilterDraft);
    void refreshSavedFilters();
    void api.getFilterOptions().then((options) => {
      setFilterOptions({
        ...fallbackFilterOptions,
        ...options,
        institution_classifications:
          options.institution_classifications?.length
            ? options.institution_classifications
            : fallbackFilterOptions.institution_classifications,
      });
    }).catch(() => {
      setFilterOptions(fallbackFilterOptions);
    });
    void api.listScoreTemplates().then((templates) => {
      setScoreTemplates(templates);
      const defaultTemplateId = templates[0]?.template_id ?? null;
      replaceScoreTemplateId(defaultTemplateId);
      if (defaultTemplateId) {
        void runSearch(
          appliedFilterRef.current,
          false,
          null,
          defaultTemplateId,
        );
      }
    }).catch(() => {
      setScoreTemplates([]);
      replaceScoreTemplateId(null);
    });
  }, [
    authRoute,
    authSession?.email_verification_required,
    authState,
    refreshSavedFilters,
    replaceScoreTemplateId,
    runSearch,
  ]);

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
    if (
      !drawerOpen ||
      drawerTab !== "score" ||
      !selectedResumeId ||
      !review ||
      review.resume_id !== selectedResumeId
    ) {
      return;
    }
    if (
      hasSourceTextQualityIssue(review.quality_flags) ||
      hasSupersededReparseVersion(review.quality_flags)
    ) {
      setDrawerScores([]);
      setDrawerScoreError(null);
      return;
    }
    void loadDrawerScores(selectedResumeId);
  }, [drawerOpen, drawerTab, loadDrawerScores, review, selectedResumeId]);

  useEffect(() => {
    drawerScoreRequestRef.current += 1;
    setSummaries([]);
    setDrawerScores([]);
    setDrawerScoreError(null);
    setDrawerScoreLoading(false);
  }, [selectedResumeId]);

  /**
   * Keep protected originals scoped to the active original-file tab. Opening
   * that tab creates a fresh, audited view grant; switching away, closing the
   * drawer, or selecting another resume invalidates the local object URL and
   * any in-flight request.
   */
  useEffect(() => {
    releaseOriginalFile();
  }, [drawerOpen, drawerTab, releaseOriginalFile, selectedResumeId]);

  useEffect(
    () => () => {
      originalFileRequestRef.current += 1;
      originalFileRevokeRef.current?.();
      originalFileRevokeRef.current = null;
    },
    [],
  );

  useEffect(() => {
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      if (agentOpen) {
        event.preventDefault();
        closeAgent();
        return;
      }
      setDrawerOpen(false);
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [agentOpen, closeAgent]);

  useEffect(() => {
    const syncSettingsFromHash = () => {
      const section = settingsSectionFromHash(window.location.hash);
      if (!section) {
        setView((current) => current === "settings" ? "library" : current);
        return;
      }
      setSettingsSection(section);
      setView("settings");
      updateSettingsHash(section, true);
    };

    syncSettingsFromHash();
    window.addEventListener("hashchange", syncSettingsFromHash);
    window.addEventListener("popstate", syncSettingsFromHash);
    return () => {
      window.removeEventListener("hashchange", syncSettingsFromHash);
      window.removeEventListener("popstate", syncSettingsFromHash);
    };
  }, [updateSettingsHash]);

  useEffect(() => {
    if (!authSession || view !== "settings") return;
    const sectionAllowed =
      (settingsSection === "mailbox" && canManageMailbox) ||
      (settingsSection === "data" && canManageCandidateData);
    if (sectionAllowed) return;

    const fallbackSection = canManageMailbox
      ? "mailbox"
      : canManageCandidateData
        ? "data"
        : null;
    if (!fallbackSection) {
      setView("library");
      updateSettingsHash(null, true);
      return;
    }
    setSettingsSection(fallbackSection);
    updateSettingsHash(fallbackSection, true);
  }, [
    authSession,
    canManageCandidateData,
    canManageMailbox,
    settingsSection,
    updateSettingsHash,
    view,
  ]);

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
      navigateToView("library");
      setLibraryRefreshToken((current) => current + 1);
      void refreshReview(resumeId);
    },
    [navigateToView, refreshReview],
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

  const previewOriginalFile = useCallback(async () => {
    if (!selectedResumeId || pdfLoading) return;
    const requestId = ++originalFileRequestRef.current;
    originalFileRevokeRef.current?.();
    originalFileRevokeRef.current = null;
    setPdfUrl(null);
    setPdfError(null);
    setPdfLoading(true);
    try {
      const access = await api.requestResumeOriginalFileAccess(
        selectedResumeId,
        "view",
      );
      const resource = await api.getAuthorizedFileObjectUrl(access.access_url);
      if (requestId !== originalFileRequestRef.current) {
        resource.revoke();
        return;
      }
      originalFileRevokeRef.current = resource.revoke;
      setPdfUrl(resource.url);
    } catch (error) {
      if (requestId === originalFileRequestRef.current) {
        setPdfError(humanizeError(error));
      }
    } finally {
      if (requestId === originalFileRequestRef.current) setPdfLoading(false);
    }
  }, [pdfLoading, selectedResumeId]);

  useEffect(() => {
    if (
      !drawerOpen ||
      drawerTab !== "original" ||
      !selectedResumeId ||
      !review ||
      review.resume_id !== selectedResumeId ||
      !canPreviewInline(review.original_filename) ||
      pdfUrl ||
      pdfLoading ||
      pdfError
    ) {
      return;
    }
    // Opening the original-file tab is an intentional view action. Request a
    // short-lived, audited grant and render the protected object URL directly.
    void previewOriginalFile();
  }, [
    drawerOpen,
    drawerTab,
    pdfError,
    pdfLoading,
    pdfUrl,
    previewOriginalFile,
    review,
    selectedResumeId,
  ]);

  const downloadOriginalFile = useCallback(async () => {
    if (!selectedResumeId || pdfDownloadLoading) return;
    setPdfDownloadLoading(true);
    try {
      const access = await api.requestResumeOriginalFileAccess(
        selectedResumeId,
        "download",
      );
      const blob = await api.getAuthorizedFileBlob(access.access_url);
      const downloadUrl = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = downloadUrl;
      link.download = review?.original_filename || "resume";
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(downloadUrl), 0);
      notify("success", "已开始下载原始文件，系统已记录本次访问。");
    } catch (error) {
      notify("error", humanizeError(error));
    } finally {
      setPdfDownloadLoading(false);
    }
  }, [notify, pdfDownloadLoading, review?.original_filename, selectedResumeId]);

  const resetFilter = async () => {
    cancelScheduledFilterSearch();
    const clean = freshDefaultFilter();
    replaceFilterDraft(clean);
    await runSearch(clean);
  };

  const changeScoreTemplate = useCallback(
    (nextTemplateId: string | null) => {
      replaceScoreTemplateId(nextTemplateId);
      void runSearch(
        appliedFilterRef.current,
        false,
        null,
        nextTemplateId,
      );
    },
    [replaceScoreTemplateId, runSearch],
  );

  const saveCurrentFilter = async (name: string) => {
    const normalized = name.trim();
    if (!normalized) {
      notify("error", "请为这组筛选条件填写一个名称。");
      return;
    }
    try {
      await api.createSavedFilter({
        name: normalized,
        filters: draftToSearchRequest(filterDraftRef.current),
      });
      await refreshSavedFilters();
      notify("success", `已保存“${normalized}”。`);
    } catch (error) {
      notify("error", humanizeError(error));
    }
  };

  const applySavedFilter = (filter: SavedFilter): boolean => {
    const result = searchRequestToDraft(filter.filters);
    if (!result.draft) {
      notify("error", result.error);
      return false;
    }
    cancelScheduledFilterSearch();
    replaceFilterDraft(result.draft);
    void runSearch(result.draft);
    return true;
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

  const deleteSelectedResumeData = useCallback(
    async (): Promise<void> => {
      if (!selectedResumeId) throw new Error("resume_not_found");
      try {
        const response = await api.deleteResumeCandidateData(selectedResumeId, {
          reason: "other",
          other_note: "simple_resume_delete",
        });
        releaseOriginalFile();
        summaryRequestRef.current += 1;
        setReview(null);
        setSummaries([]);
        setSelectedResume(null);
        setDrawerOpen(false);
        setLibraryRefreshToken((current) => current + 1);
        notify(
          "success",
          `当前简历版本已移出工作台，可在 ${formatLibraryDate(response.recovery_deadline_at)} 前恢复。`,
        );
      } catch (error) {
        notify("error", humanizeError(error));
        throw error;
      }
    },
    [notify, releaseOriginalFile, selectedResumeId],
  );

  const handleGlobalSearch = (event: ReactKeyboardEvent<HTMLInputElement>) => {
    if (event.key !== "Enter") return;
    const terms = globalQuery
      .split(/[、,，\s]+/)
      .map((term) => term.trim())
      .filter(Boolean);
    const next = { ...filterDraftRef.current, keywords: terms };
    cancelScheduledFilterSearch();
    replaceFilterDraft(next);
    navigateToView("filter");
    void runSearch(next);
  };

  const establishSession = (session: AuthSession) => {
    applyAuthSession(session);
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

  const completePasswordReset = async (token: string, password: string) => {
    setAuthError(null);
    setAuthLoading(true);
    try {
      await api.completePasswordReset({ token, password });
      return true;
    } catch (error) {
      setAuthError(humanizeError(error));
      return false;
    } finally {
      setAuthLoading(false);
    }
  };

  const completeEmailVerification = async (token: string) => {
    setAuthError(null);
    setAuthLoading(true);
    try {
      // Keep the verification-link tab on its explicit confirmation page.
      // The registration tab polls its own session and is the one that enters
      // the workspace after this server-side verification succeeds.
      const session = await api.completeEmailVerification(token);
      applyAuthSession(session);
      return session;
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

  // A recovery link may be opened in a browser that still has an unrelated
  // workspace session.  It must remain usable instead of silently dropping
  // the recipient into that workspace.
  if (authRoute === "reset-password") {
    return (
      <ResetPasswordPage
        error={authError}
        loading={authLoading}
        onComplete={completePasswordReset}
      />
    );
  }

  if (authRoute === "verify-email" || authSession?.email_verification_required) {
    return (
      <EmailVerificationPage
        error={authError}
        loading={authLoading}
        session={authSession}
        onComplete={completeEmailVerification}
        onRefreshSession={refreshAuthSession}
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
      <a className="skip-link" href="#main-content">跳到主要内容</a>
      <SideRail
        activeView={view}
        canManageSettings={canManageSettings}
        inert={drawerOpen || agentOpen}
        onChangeView={navigateToView}
        onOpenSettings={() => openSettings(canManageMailbox ? "mailbox" : "data")}
      />
      <div className="app-area" inert={drawerOpen || agentOpen}>
      <Topbar
        globalQuery={globalQuery}
        onGlobalQueryChange={setGlobalQuery}
        onGlobalSearchKeyDown={handleGlobalSearch}
        onOpenAgent={() => {
            setDrawerOpen(false);
            setAgentOpen(true);
          }}
          agentTriggerRef={agentTriggerRef}
          canManageSettings={canManageSettings}
          onAccountMenuOpen={() => {
            void refreshAuthSession();
          }}
          onLogout={() => void logout()}
          onNewUpload={() => navigateToView("upload")}
          onOpenSettings={() => openSettings(canManageMailbox ? "mailbox" : "data")}
          organizationName={authSession?.organization?.name ?? null}
          platformAdmin={authSession?.is_platform_admin ?? false}
          planName={authSession?.plan?.name ?? null}
          role={authSession?.role ?? null}
          trial={authSession?.trial ?? null}
          userDisplayName={authSession?.user?.display_name ?? null}
          userEmail={authSession?.user?.email ?? null}
        />
        <TrialStatusBanner trial={authSession?.trial ?? null} />
        <main className="main-content" id="main-content">
          {view === "library" && (
            <ResumeLibraryPage
              refreshToken={libraryRefreshToken}
              selectedResumeId={selectedResumeId}
              onOpenResume={openLibraryResume}
              onUpload={() => navigateToView("upload")}
            />
          )}
          {view === "filter" && (
            <FilterWorkspace
              appliedDraft={appliedFilter}
              draft={filterDraft}
              filterOptions={filterOptions}
              onDraftChange={updateFilterDraft}
              savedFilters={savedFilters}
              search={search}
              searching={searching}
              selectedResumeId={selectedResumeId}
              onReset={resetFilter}
              onSave={saveCurrentFilter}
              onApplySaved={applySavedFilter}
              onDeleteSaved={deleteSavedFilter}
              onOpenCandidate={openCandidate}
              onScoreTemplateChange={changeScoreTemplate}
              onLoadMore={() =>
                void runSearch(appliedFilterRef.current, true, search.next_cursor)
              }
              onUpload={() => navigateToView("upload")}
              scoreTemplateId={scoreTemplateId}
              scoreTemplates={scoreTemplates}
            />
          )}
          <div hidden={view !== "upload"}>
            <UploadPage onComplete={openUploadedResume} notify={notify} />
          </div>
          {view === "score" && (
            <ScorePage
              notify={notify}
              onScoreCreated={handleScoreCreated}
              onTemplateCreated={registerScoreTemplate}
            />
          )}
          {view === "match" && (
            <MatchPage
              canGenerateAiJd={canGenerateAiJd}
              notify={notify}
              onOpenMatchedResume={openMatchedResume}
            />
          )}
          {view === "settings" && canManageSettings && (
            <WorkspaceSettingsPage
              activeSection={settingsSection}
              canManageCandidateData={canManageCandidateData}
              canManageMailbox={canManageMailbox}
              notify={notify}
              onImported={() => setLibraryRefreshToken((current) => current + 1)}
              onOpenLibrary={() => navigateToView("library")}
              onSelectSection={openSettings}
              role={authSession?.role ?? null}
            />
          )}
        </main>
      </div>

      <div
        aria-hidden="true"
        className={`drawer-scrim${drawerOpen || agentOpen ? " is-open" : ""}`}
        onClick={() => {
          if (agentOpen) closeAgent();
          else setDrawerOpen(false);
        }}
      />
      <CandidateDrawer
        candidate={selectedResume}
        drawerTab={drawerTab}
        isOpen={drawerOpen}
        pdfError={pdfError}
        pdfDownloadLoading={pdfDownloadLoading}
        pdfLoading={pdfLoading}
        pdfUrl={pdfUrl}
        review={review}
        reviewLoading={reviewLoading}
        scoreError={drawerScoreError}
        scoreLoading={drawerScoreLoading}
        scores={drawerScores}
        summaries={summaries}
        summaryLoading={summaryLoading}
        onClose={() => setDrawerOpen(false)}
        onCreateManualSummary={createManualSummary}
        onDeleteResume={deleteSelectedResumeData}
        onDownloadOriginal={downloadOriginalFile}
        onGenerateSummary={() => void generateSummary()}
        onReparseSource={() => void reparseSelectedSource()}
        onEnrichFacts={() => void enrichSelectedFacts()}
        onPreviewOriginal={() => void previewOriginalFile()}
        onRefreshScores={() => {
          if (selectedResumeId) void loadDrawerScores(selectedResumeId);
        }}
        enrichingFacts={enrichingFacts}
        onTabChange={setDrawerTab}
        reparsingSource={reparsingSource}
        canManageCandidateData={canManageCandidateData}
      />
      <RecruitingAgentDrawer
        isOpen={agentOpen}
        onClose={closeAgent}
        onOpenMatchWorkspace={() => {
          setAgentOpen(false);
          navigateToView("match");
        }}
        onOpenScoreWorkspace={() => {
          setAgentOpen(false);
          navigateToView("score");
        }}
        onOpenMailboxSettings={() => {
          setAgentOpen(false);
          openSettings("mailbox");
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
        ? "30 天免费体验，含 1,000 次大模型调用"
        : `${registrationOffer.trial_days} 天${registrationOffer.plan_name}免费体验，含 ${formatWholeNumber(registrationOffer.llm_call_limit)} 次大模型调用`}
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
            <span>试用期内最多 {formatWholeNumber(registrationOffer.llm_call_limit)} 次大模型调用，简历提取、评分、JD 处理和招聘助手统一计入。</span>
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
  onRefreshSession,
  onResend,
}: {
  error: string | null;
  loading: boolean;
  session: AuthSession | null;
  onComplete: (token: string) => Promise<AuthSession | null>;
  onRefreshSession: () => Promise<AuthSession | null>;
  onResend: () => Promise<{ accepted: boolean; delivery_available: boolean } | null>;
}) {
  const token = new URLSearchParams(window.location.search).get("token");
  const completionStarted = useRef(false);
  const [verificationState, setVerificationState] = useState<
    "waiting" | "verifying" | "verified" | "failed"
  >(token ? "verifying" : "waiting");
  const [resendState, setResendState] = useState<"idle" | "sent" | "unavailable">("idle");
  const email = session?.user?.email ?? null;
  const canResend = Boolean(session?.authenticated && session.email_verification_required);
  const isWaitingForVerification = Boolean(
    !token && session?.authenticated && session.email_verification_required,
  );
  const verificationSucceeded = Boolean(token && verificationState === "verified");
  const verificationInProgress = Boolean(token && verificationState === "verifying");

  useEffect(() => {
    if (!token || completionStarted.current) return;
    completionStarted.current = true;
    setVerificationState("verifying");
    void onComplete(token).then((result) => {
      setVerificationState(
        result?.authenticated && !result.email_verification_required
          ? "verified"
          : "failed",
      );
    });
  }, [onComplete, token]);

  useEffect(() => {
    if (!isWaitingForVerification) return;

    let active = true;
    let refreshing = false;
    const refresh = async () => {
      if (refreshing) return;
      refreshing = true;
      try {
        await onRefreshSession();
      } finally {
        refreshing = false;
      }
    };
    const refreshOnFocus = () => {
      if (active) void refresh();
    };

    void refresh();
    const intervalId = window.setInterval(refresh, 3_000);
    window.addEventListener("focus", refreshOnFocus);
    return () => {
      active = false;
      window.clearInterval(intervalId);
      window.removeEventListener("focus", refreshOnFocus);
    };
  }, [isWaitingForVerification, onRefreshSession]);

  useEffect(() => {
    if (!token && session?.authenticated && !session.email_verification_required) {
      // This is the original registration tab. It is the only page that
      // automatically enters the workspace after a successful email check.
      window.location.replace(workspaceHref());
    }
  }, [session?.authenticated, session?.email_verification_required, token]);

  const maskedEmail = email
    ? email.replace(/^(.{1,2}).*(@.*)$/, "$1•••$2")
    : null;

  return (
    <AuthPageLayout
      description="验证工作邮箱后即可进入你的独立招聘工作区。候选人、简历、岗位和 AI 结论始终按工作区隔离。"
      eyebrow="账户验证"
      title={
        verificationSucceeded
          ? "邮箱验证成功"
          : token
            ? verificationInProgress
              ? "正在验证邮箱"
              : "邮箱验证未完成"
            : "请验证工作邮箱"
      }
    >
      <div aria-live="polite" className="auth-success-state">
        <span className="auth-success-icon">
          <Icon name={verificationSucceeded ? "check" : "inbox"} size={20} />
        </span>
        <h2>
          {verificationSucceeded
            ? "邮箱已验证"
            : token
              ? verificationInProgress
                ? "正在确认你的邮箱"
                : "验证链接未完成验证"
              : "请查收验证邮件"}
        </h2>
        {verificationSucceeded ? (
          <p>验证已经完成。请返回发起注册的页面，系统会自动进入工作台；你可以直接关闭此页面。</p>
        ) : token ? (
          <p>
            {loading || verificationInProgress
              ? "请稍候，正在安全地验证这条链接。"
              : "验证链接无效或已失效时，你可以登录后重新发送邮件。"}
          </p>
        ) : (
          <p>
            {maskedEmail
              ? `请查看 ${maskedEmail} 的收件箱，并在 24 小时内打开验证链接。`
              : "请登录注册邮箱后打开验证链接，完成后即可进入工作台。"}
          </p>
        )}
        {isWaitingForVerification && (
          <p className="auth-footer-copy" role="status">
            验证完成后，本页面会自动进入工作台。
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
        {token && !verificationSucceeded && !loading && (
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

function ResetPasswordPage({
  error,
  loading,
  onComplete,
}: {
  error: string | null;
  loading: boolean;
  onComplete: (token: string, password: string) => Promise<boolean>;
}) {
  const token = new URLSearchParams(window.location.search).get("token") ?? "";
  const [password, setPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [formError, setFormError] = useState<string | null>(null);
  const [completed, setCompleted] = useState(false);

  const submit = async () => {
    setFormError(null);
    if (!token) {
      setFormError("缺少重置链接。请重新申请一封重置邮件。");
      return;
    }
    if (password !== confirmation) {
      setFormError("两次输入的密码不一致，请重新确认。");
      return;
    }
    if (await onComplete(token, password)) {
      setCompleted(true);
    }
  };

  return (
    <AuthPageLayout
      description="设置新密码后，旧密码将立即失效。为安全起见，重置链接只能使用一次。"
      eyebrow="账户协助"
      title="设置新的登录密码"
    >
      {completed ? (
        <div aria-live="polite" className="auth-success-state">
          <span className="auth-success-icon"><Icon name="check" size={20} /></span>
          <h2>新密码已设置</h2>
          <p>请使用新密码登录你的招聘工作台。</p>
          <a className="button button-primary auth-submit" href={workspaceHref("/login")}>前往登录</a>
        </div>
      ) : (
        <form
          className="auth-form"
          onSubmit={(event) => {
            event.preventDefault();
            void submit();
          }}
        >
          <div className="field-stack">
            <label className="field-label" htmlFor="reset-password">新密码</label>
            <input
              autoComplete="new-password"
              className="field"
              id="reset-password"
              minLength={8}
              onChange={(event) => setPassword(event.target.value)}
              placeholder="至少 8 个字符"
              required
              type="password"
              value={password}
            />
          </div>
          <div className="field-stack">
            <label className="field-label" htmlFor="reset-password-confirmation">再次输入新密码</label>
            <input
              aria-describedby={formError || error ? "reset-password-error" : undefined}
              aria-invalid={Boolean(formError || error)}
              autoComplete="new-password"
              className="field"
              id="reset-password-confirmation"
              minLength={8}
              onChange={(event) => setConfirmation(event.target.value)}
              placeholder="请再次输入"
              required
              type="password"
              value={confirmation}
            />
          </div>
          {(formError || error) && <p className="auth-error" id="reset-password-error" role="alert">{formError || error}</p>}
          <button
            className="button button-primary auth-submit"
            disabled={loading || !token || password.length < 8 || !confirmation}
            type="submit"
          >
            {loading ? <><i className="spinner" />正在保存</> : "保存新密码"}
          </button>
          <p className="auth-footer-copy"><a href={workspaceHref("/forgot-password")}>重新申请重置链接</a></p>
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

function TrialStatusBanner({ trial }: { trial: TrialAccess | null }) {
  if (!trial) return null;
  const isExpired = trial.plan_status === "expired" || !trial.access_enabled;
  const isTrial = trial.plan_status === "trial";
  const llmCallRemaining =
    typeof trial.llm_call_remaining === "number"
      ? Math.max(0, trial.llm_call_remaining)
      : null;
  const isQuotaExhausted = isTrial && llmCallRemaining === 0;
  if (!isExpired && !isQuotaExhausted) return null;
  return (
    <section className={`trial-banner${isExpired ? " is-expired" : " is-quota-exhausted"}`} role="alert">
      <div>
        <strong>
          {isExpired ? "试用期已结束" : "试用 AI 调用额度已用完"}
        </strong>
        <p>
          {isExpired
            ? "你的工作区数据已保留。续费入口开放前，请联系 GreatSell AI 团队继续使用。"
            : "你仍可查看和管理已有数据。继续使用 AI 功能请联系 GreatSell AI 团队。"}
        </p>
      </div>
      <span>{isExpired ? "数据已保留" : "AI 调用已暂停"}</span>
    </section>
  );
}

function SideRail({
  activeView,
  canManageSettings,
  onChangeView,
  onOpenSettings,
  inert,
}: {
  activeView: View;
  canManageSettings: boolean;
  onChangeView: (view: MainWorkspaceView) => void;
  onOpenSettings: () => void;
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
        {canManageSettings && (
          <button
            aria-current={activeView === "settings" ? "page" : undefined}
            aria-label="设置"
            className={`rail-item${activeView === "settings" ? " is-active" : ""}`}
            onClick={onOpenSettings}
            type="button"
          >
            <Icon name="gear" size={18} />
            <span className="rail-tooltip">设置</span>
          </button>
        )}
      </div>
    </aside>
  );
}

function Topbar({
  globalQuery,
  onGlobalQueryChange,
  onGlobalSearchKeyDown,
  onOpenAgent,
  agentTriggerRef,
  canManageSettings,
  onAccountMenuOpen,
  onLogout,
  onNewUpload,
  onOpenSettings,
  organizationName,
  platformAdmin,
  planName,
  role,
  trial,
  userDisplayName,
  userEmail,
}: {
  globalQuery: string;
  onGlobalQueryChange: (value: string) => void;
  onGlobalSearchKeyDown: (event: ReactKeyboardEvent<HTMLInputElement>) => void;
  onOpenAgent: () => void;
  agentTriggerRef: RefObject<HTMLButtonElement | null>;
  canManageSettings: boolean;
  onAccountMenuOpen: () => void;
  onLogout: () => void;
  onNewUpload: () => void;
  onOpenSettings: () => void;
  organizationName: string | null;
  platformAdmin: boolean;
  planName: string | null;
  role: "admin" | "recruiter" | null;
  trial: TrialAccess | null;
  userDisplayName: string | null;
  userEmail: string | null;
}) {
  const trialDays = trial?.trial_days_remaining;
  const roleLabel = role === "admin" ? "管理员" : role === "recruiter" ? "招聘官" : null;
  const trialLabel =
    trial?.plan_status === "trial" && typeof trialDays === "number"
      ? `试用 ${Math.max(0, trialDays)} 天`
      : trial?.plan_status === "expired"
        ? "试用已到期"
        : null;
  const trialLlmCallRemaining =
    trial?.plan_status === "trial" && typeof trial.llm_call_remaining === "number"
      ? Math.max(0, trial.llm_call_remaining)
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
        <button
          aria-label="招聘助手"
          className="button button-agent"
          onClick={onOpenAgent}
          ref={agentTriggerRef}
          type="button"
        >
          <Icon name="spark" size={16} />
          <span className="topbar-action-label">招聘助手</span>
        </button>
        <button
          aria-label="上传简历"
          className="button button-ghost"
          onClick={onNewUpload}
          type="button"
        >
          <Icon name="upload" size={16} />
          <span className="topbar-action-label">上传简历</span>
        </button>
        <AccountMenu
          canManageSettings={canManageSettings}
          onOpen={onAccountMenuOpen}
          onOpenSettings={onOpenSettings}
          onLogout={onLogout}
          organizationName={organizationName}
          platformAdmin={platformAdmin}
          planName={planName}
          role={role}
          trial={trial}
          trialLlmCallRemaining={trialLlmCallRemaining}
          userDisplayName={userDisplayName}
          userEmail={userEmail}
        />
      </div>
    </header>
  );
}

function AccountMenu({
  canManageSettings,
  onOpen,
  onOpenSettings,
  onLogout,
  organizationName,
  platformAdmin,
  planName,
  role,
  trial,
  trialLlmCallRemaining,
  userDisplayName,
  userEmail,
}: {
  canManageSettings: boolean;
  onOpen: () => void;
  onOpenSettings: () => void;
  onLogout: () => void;
  organizationName: string | null;
  platformAdmin: boolean;
  planName: string | null;
  role: "admin" | "recruiter" | null;
  trial: TrialAccess | null;
  trialLlmCallRemaining: number | null;
  userDisplayName: string | null;
  userEmail: string | null;
}) {
  const [isOpen, setIsOpen] = useState(false);
  const [isPinned, setIsPinned] = useState(false);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const hoverCloseTimerRef = useRef<number | null>(null);
  const avatarInitials = accountAvatarInitials(userDisplayName);
  const roleLabel = role === "admin" ? "管理员" : role === "recruiter" ? "招聘官" : null;
  const displayName = userDisplayName?.trim() || "当前用户";
  const llmCallLimit =
    typeof trial?.llm_call_limit === "number" ? Math.max(0, trial.llm_call_limit) : null;
  const llmCallUsed =
    typeof trial?.llm_call_used === "number" ? Math.max(0, trial.llm_call_used) : null;
  const hasLlmCallUsage =
    llmCallLimit !== null &&
    llmCallUsed !== null &&
    trialLlmCallRemaining !== null;
  const trialDays =
    trial?.plan_status === "trial" && typeof trial.trial_days_remaining === "number"
      ? Math.max(0, trial.trial_days_remaining)
      : null;
  const triggerLabel = `账户菜单：${displayName}`;

  const cancelHoverClose = () => {
    if (hoverCloseTimerRef.current === null) return;
    window.clearTimeout(hoverCloseTimerRef.current);
    hoverCloseTimerRef.current = null;
  };

  const closeMenu = (restoreFocus = false) => {
    cancelHoverClose();
    setIsOpen(false);
    setIsPinned(false);
    if (restoreFocus) {
      window.requestAnimationFrame(() => triggerRef.current?.focus());
    }
  };

  const openMenu = () => {
    cancelHoverClose();
    if (!isOpen) onOpen();
    setIsOpen(true);
  };

  const scheduleHoverClose = () => {
    if (isPinned) return;
    cancelHoverClose();
    hoverCloseTimerRef.current = window.setTimeout(() => {
      hoverCloseTimerRef.current = null;
      setIsOpen(false);
      setIsPinned(false);
    }, 180);
  };

  useEffect(() => {
    if (!isOpen) return;

    const closeFromOutside = (event: PointerEvent) => {
      const target = event.target;
      if (target instanceof Node && rootRef.current?.contains(target)) return;
      cancelHoverClose();
      setIsOpen(false);
      setIsPinned(false);
    };
    const closeFromEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      cancelHoverClose();
      setIsOpen(false);
      setIsPinned(false);
      window.requestAnimationFrame(() => triggerRef.current?.focus());
    };

    document.addEventListener("pointerdown", closeFromOutside);
    document.addEventListener("keydown", closeFromEscape);
    return () => {
      document.removeEventListener("pointerdown", closeFromOutside);
      document.removeEventListener("keydown", closeFromEscape);
    };
  }, [isOpen]);

  useEffect(() => () => cancelHoverClose(), []);

  const toggleMenu = () => {
    if (!isOpen) {
      onOpen();
      setIsOpen(true);
      setIsPinned(true);
      return;
    }
    if (isPinned) {
      closeMenu(true);
      return;
    }
    setIsPinned(true);
  };

  return (
    <div
      className="account-menu"
      onBlur={(event) => {
        const nextFocused = event.relatedTarget;
        if (nextFocused instanceof Node && rootRef.current?.contains(nextFocused)) return;
        cancelHoverClose();
        setIsOpen(false);
        setIsPinned(false);
      }}
      onMouseEnter={openMenu}
      onMouseLeave={scheduleHoverClose}
      ref={rootRef}
    >
      <button
        aria-controls="account-menu-popover"
        aria-expanded={isOpen}
        aria-haspopup="dialog"
        aria-label={triggerLabel}
        className="account-menu-trigger"
        onClick={toggleMenu}
        ref={triggerRef}
        type="button"
      >
        <span className={`account-avatar${avatarInitials ? "" : " is-icon"}`} aria-hidden="true">
          {avatarInitials ?? <Icon name="user" size={17} />}
        </span>
        <Icon className="account-menu-chevron" name="chevron-down" size={15} />
      </button>
      {isOpen && (
        <section
          aria-label="账户菜单"
          className="account-menu-popover"
          id="account-menu-popover"
          role="dialog"
        >
          <div className="account-menu-profile">
            <span className={`account-avatar account-menu-profile-avatar${avatarInitials ? "" : " is-icon"}`} aria-hidden="true">
              {avatarInitials ?? <Icon name="user" size={18} />}
            </span>
            <div>
              <strong>{displayName}</strong>
              {userEmail && <p>{userEmail}</p>}
            </div>
          </div>
          {(organizationName || roleLabel || planName) && (
            <p className="account-menu-context">
              {[roleLabel, organizationName, planName].filter(Boolean).join(" · ")}
            </p>
          )}
          {trial?.plan_status === "trial" && (
            <section className="account-menu-allowance" aria-label="试用状态">
              <span>试用状态</span>
              {hasLlmCallUsage ? (
                <strong>
                  AI 调用已用 {formatWholeNumber(llmCallUsed)} / {formatWholeNumber(llmCallLimit)}，剩余 {formatWholeNumber(trialLlmCallRemaining)} 次
                </strong>
              ) : (
                <strong>AI 调用额度正在同步</strong>
              )}
              {trialDays !== null && <small>试用还剩 {trialDays} 天</small>}
            </section>
          )}
          {trial?.plan_status === "expired" && (
            <section className="account-menu-allowance is-expired" aria-label="试用状态">
              <span>试用状态</span>
              <strong>试用已到期</strong>
            </section>
          )}
          <div className="account-menu-actions">
            {canManageSettings && (
              <button
                className="account-menu-action"
                onClick={() => {
                  closeMenu();
                  onOpenSettings();
                }}
                type="button"
              >
                <Icon name="gear" size={16} />
                工作区设置
              </button>
            )}
            {platformAdmin && (
              <a className="account-menu-action" href={platformHref()} onClick={() => closeMenu()}>
                <Icon name="layers" size={16} />
                平台管理
              </a>
            )}
            <button
              className="account-menu-action is-danger"
              onClick={() => {
                closeMenu();
                onLogout();
              }}
              type="button"
            >
              <Icon name="arrow-right" size={16} />
              退出登录
            </button>
          </div>
        </section>
      )}
    </div>
  );
}

type AgentComposerContext = "assistant" | "new_profile" | "refine_profile";

interface AgentSendSnapshot {
  composerContext: AgentComposerContext;
  activeTalentProfile: {
    profileId: string;
    revisionId: string;
  } | null;
  jobVersionId: string;
}

interface AgentRetry {
  message: string;
  snapshot: AgentSendSnapshot;
}

interface AgentChatMessage {
  id: number;
  role: "assistant" | "user";
  content: string;
  candidates?: RecruitingAgentCandidate[];
  actions?: RecruitingAgentAction[];
  searchSummary?: RecruitingAgentSearchSummary | null;
  talentProfile?: TalentSearchProfile;
  talentRun?: TalentSearchRun;
  failure?: boolean;
  retry?: AgentRetry;
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

function AgentSearchSummaryPanel({
  summary,
}: {
  summary: RecruitingAgentSearchSummary;
}) {
  const hasVerificationSplit = summary.unconfirmed_count !== null;
  return (
    <section className="agent-search-summary" aria-label="候选人检索结果">
      <div className="agent-search-summary-heading">
        <span>检索结果</span>
        <small>已基于当前工作区简历完成检索</small>
      </div>
      <div className="agent-search-summary-metrics">
        <div>
          <strong>{summary.confirmed_count}</strong>
          <span>{hasVerificationSplit ? "已确认" : "符合条件"}</span>
        </div>
        {hasVerificationSplit && (
          <div className="is-unconfirmed">
            <strong>{summary.unconfirmed_count}</strong>
            <span>未确认</span>
          </div>
        )}
      </div>
      {summary.confirmation_basis && (
        <p className="agent-search-summary-note">{summary.confirmation_basis}</p>
      )}
      {summary.displayed_count < summary.confirmed_count && (
        <p className="agent-search-summary-note">
          当前展示前 {summary.displayed_count} 位候选人。
        </p>
      )}
    </section>
  );
}

function AgentCandidateCard({
  candidate,
  onOpen,
}: {
  candidate: RecruitingAgentCandidate;
  onOpen: () => void;
}) {
  const verificationEvidence = candidate.verification_evidence ?? [];
  const confirmationLabel =
    candidate.verification_status === "confirmed" ? "已确认" : "未确认";
  return (
    <article className="agent-candidate-card">
      <div className="agent-candidate-card-heading">
        <div>
          <strong>{candidate.display_name?.trim() || "未命名候选人"}</strong>
          <small>{candidate.detail}</small>
        </div>
        <div className="agent-candidate-card-actions">
          {candidate.score !== null && <b>{candidate.score.toFixed(1)}</b>}
          <button
            aria-label={`查看${candidate.display_name?.trim() || "候选人"}详情`}
            className="icon-button agent-candidate-open"
            onClick={onOpen}
            type="button"
          >
            <Icon name="chevron-right" size={16} />
          </button>
        </div>
      </div>
      {candidate.verification_status && (
        <div className="agent-verification">
          <span
            className={`agent-verification-status is-${candidate.verification_status}`}
          >
            {confirmationLabel}
          </span>
          {verificationEvidence.length ? (
            <ul className="agent-verification-evidence" aria-label="简历依据">
              {verificationEvidence.map((evidence) => (
                <li key={`${evidence.source}-${evidence.label}`}>
                  <span>
                    {evidence.source === "resume_text" ? "简历原文" : "已提取事实"}
                  </span>
                  {evidence.label}
                </li>
              ))}
            </ul>
          ) : (
            <small>简历未明确提及或当前信息无法识别。</small>
          )}
        </div>
      )}
    </article>
  );
}

function talentProfileHardFilterLabels(filters: TalentSearchHardFilters): string[] {
  const labels: string[] = [];
  if (filters.institution_classifications_any_of.length) {
    const institutionLabels: Record<InstitutionClassification, string> = {
      "985": "985",
      "211": "211",
      undergraduate: "本科院校",
      associate: "大专院校",
      secondary_vocational: "中专院校",
      overseas: "海外院校",
    };
    labels.push(
      `院校类型：${filters.institution_classifications_any_of
        .map((value) => institutionLabels[value])
        .join(" / ")}（任一）`,
    );
  }
  if (filters.education_degree_in.length) {
    labels.push(
      `教育经历：含${filters.education_degree_in.map((value) => degreeLabels[value]).join(" / ")}（任一）`,
    );
  }
  if (filters.highest_degree_in.length) {
    labels.push(
      `最高学历：${filters.highest_degree_in.map((value) => degreeLabels[value]).join(" / ")}（任一）`,
    );
  }
  if (filters.graduation_status !== "any") {
    const graduationLabel = filters.graduation_status === "fresh" ? "应届" : "往届";
    labels.push(
      `毕业：${graduationLabel}${filters.fresh_graduate_start_month && filters.fresh_graduate_end_month ? ` ${filters.fresh_graduate_start_month} 至 ${filters.fresh_graduate_end_month}` : ""}`,
    );
  }
  if (filters.min_employment_months !== null) {
    labels.push(`正式工作不少于 ${Math.round(filters.min_employment_months / 12 * 10) / 10} 年`);
  }
  if (filters.min_employment_or_internship_months !== null) {
    labels.push(`工作加实习不少于 ${Math.round(filters.min_employment_or_internship_months / 12 * 10) / 10} 年`);
  }
  if (filters.experience_types_all_of.length) {
    labels.push(
      `经历：${filters.experience_types_all_of
        .map((value) => experienceTypeOptions.find((item) => item.value === value)?.label || value)
        .join(" + ")}（全部）`,
    );
  }
  if (filters.skills_all_of.length) {
    labels.push(`精确技能：${filters.skills_all_of.join("、")}（全部）`);
  }
  if (filters.language_credentials_all_of.length) {
    labels.push(
      `证书：${filters.language_credentials_all_of
        .map((item) => item.custom_name_contains || item.credential_code.toUpperCase())
        .join("、")}（全部）`,
    );
  }
  return labels;
}

function profileCandidateAsAgentCandidate(item: CandidateSearchItem): RecruitingAgentCandidate {
  const experience = [item.latest_experience_organization, item.latest_experience_title]
    .filter(Boolean)
    .join(" · ");
  return {
    candidate_id: item.candidate_id,
    resume_id: item.resume_id,
    display_name: item.display_name,
    detail: experience || degreeLabels[item.highest_degree ?? "unknown"],
    // A library score and a profile-match score are different measurements.
    // Never show the former as an unlabeled number in the profile workflow.
    score: null,
    verification_status: null,
    verification_evidence: [],
  };
}

function talentProfileLaneLabel(lane: TalentSearchProfileMatchResult["match_lane"]): string {
  if (lane === "recommended") return "证据充分";
  if (lane === "pending") return "待核实";
  return "存在缺口";
}

function talentProfileOutcomeLabel(
  outcome: TalentSearchProfileMatchResult["requirement_results"][number]["outcome"],
): string {
  if (outcome === "met") return "已支持";
  if (outcome === "partial") return "部分支持";
  if (outcome === "unknown") return "待核实";
  return "存在缺口";
}

function TalentProfileMatchCard({
  match,
  onOpen,
}: {
  match: TalentSearchProfileMatchResult;
  onOpen: () => void;
}) {
  const confidence = match.match_confidence === null
    ? "—"
    : `${Math.round(match.match_confidence * 100)}%`;
  const needsVerification = match.requirement_results.filter(
    (item) => item.outcome === "unknown" || item.outcome === "partial",
  );
  return (
    <article className="talent-profile-match-card">
      <div className="talent-profile-match-heading">
        <div>
          <strong>{match.candidate_display_name?.trim() || "未命名候选人"}</strong>
          <small>{talentProfileLaneLabel(match.match_lane)}</small>
        </div>
        <div className="talent-profile-match-metrics" aria-label="画像匹配指标">
          <span><b>{match.match_score.toFixed(1)}</b>匹配度</span>
          <span><b>{confidence}</b>可信度</span>
          <button
            aria-label={`查看${match.candidate_display_name?.trim() || "候选人"}详情`}
            className="icon-button agent-candidate-open"
            onClick={onOpen}
            type="button"
          >
            <Icon name="chevron-right" size={16} />
          </button>
        </div>
      </div>
      {!!match.requirement_results.length && (
        <ul className="talent-profile-match-requirements" aria-label="画像核验依据">
          {match.requirement_results.map((item) => (
            <li key={item.requirement_id}>
              <span className={`is-${item.outcome}`}>{talentProfileOutcomeLabel(item.outcome)}</span>
              <div>
                <strong>{item.requirement_text}</strong>
                <small>{item.reason}</small>
              </div>
            </li>
          ))}
        </ul>
      )}
      {!!needsVerification.length && (
        <p className="talent-profile-match-note">
          待核实：{needsVerification.map((item) => item.requirement_text).join("；")}
        </p>
      )}
    </article>
  );
}

function TalentSearchRunPanel({
  run,
  onOpenCandidate,
  onRefresh,
  onLoadMore,
  onAdjustConditions,
  loading,
}: {
  run: TalentSearchRun;
  onOpenCandidate: (candidate: RecruitingAgentCandidate) => void;
  onRefresh: () => void;
  onLoadMore: () => void;
  onAdjustConditions: () => void;
  loading: boolean;
}) {
  const isProcessing = run.status === "queued" || run.status === "running";
  const isHardFilterRecall = run.result_mode === "hard_filter_recall";
  const statusLabel = isHardFilterRecall
    ? "硬筛已命中候选人"
    : (run.status === "queued"
      ? "已排队，等待 AI 核验"
      : run.status === "running"
        ? "正在依据简历事实核验"
        : run.status === "partial"
          ? "部分候选人的 AI 核验未完成"
          : "已完成依据简历事实的核验")
  const hasSemanticResults = run.match_results.length > 0;
  const shouldShowRecall = isHardFilterRecall || !hasSemanticResults;
  const appliedHardFilters = talentProfileHardFilterLabels(run.applied_hard_filters);
  const diagnostics = run.recall_diagnostics;
  return (
    <section className="talent-profile-run" aria-label="人才画像找人结果">
      <div className="talent-profile-run-heading">
        <div>
          <strong>{statusLabel}</strong>
          <small>
            严格召回 {run.total_recalled_count} 位候选人
            {isHardFilterRecall
              ? "；本次只有明确硬条件，无需 AI 语义核验。"
              : run.job_match_batch_id
                ? `；已完成 ${run.match_completed_count}/${run.match_total_count} 位 AI 核验。`
                : "；当前没有候选人进入 AI 核验。"}
          </small>
        </div>
        <button
          className="button button-ghost talent-profile-refresh"
          disabled={loading}
          onClick={onRefresh}
          type="button"
        >
          <Icon name="refresh" size={14} />刷新
        </button>
      </div>
      {(isProcessing || run.status === "partial") && (
        <p className="talent-profile-run-note">
          待核实不代表不符合，系统不会自动拒绝或录用候选人。
        </p>
      )}
      {hasSemanticResults && (
        <div className="talent-profile-match-list">
          {run.match_results.map((match) => (
            <TalentProfileMatchCard
              key={match.match_id}
              match={match}
              onOpen={() => onOpenCandidate({
                candidate_id: match.candidate_id,
                resume_id: match.resume_id,
                display_name: match.candidate_display_name,
                detail: "AI 人才画像核验结果",
                score: null,
                verification_status: null,
                verification_evidence: [],
              })}
            />
          ))}
        </div>
      )}
      {shouldShowRecall && !!run.candidate_recall.items.length && (
        <div className="agent-candidate-list">
          {run.candidate_recall.items.map((item) => {
            const candidate = profileCandidateAsAgentCandidate(item);
            return (
              <AgentCandidateCard
                candidate={candidate}
                key={candidate.resume_id}
                onOpen={() => onOpenCandidate(candidate)}
              />
            );
          })}
        </div>
      )}
      {!run.candidate_recall.items.length && !isProcessing && !hasSemanticResults && (
        <section className="talent-profile-zero-state" aria-label="零结果说明">
          <strong>
            {diagnostics?.eligible_resume_count === 0
              ? "当前工作区没有可筛选的简历"
              : "没有候选人同时满足本次严格条件"}
          </strong>
          {!!appliedHardFilters.length && (
            <div className="talent-profile-chips" aria-label="本次已应用条件">
              {appliedHardFilters.map((label) => <small key={label}>{label}</small>)}
            </div>
          )}
          {diagnostics && (
            <div className="talent-profile-recall-diagnostics">
              <p>
                可筛选简历 {diagnostics.eligible_resume_count} 份
                {diagnostics.needs_review_count > 0
                  ? `；另有 ${diagnostics.needs_review_count} 份待处理，未计入本次筛选。`
                  : "。"}
              </p>
              {!!diagnostics.steps.length && (
                <ol>
                  {diagnostics.steps.map((step) => (
                    <li key={step.key}>
                      <span>{step.label}</span>
                      <b>筛掉 {step.removed_count}，剩余 {step.remaining_count}</b>
                    </li>
                  ))}
                </ol>
              )}
            </div>
          )}
          <small>
            重点核验和优先项不会作为严格条件排除候选人；缺少简历证据会在后续核验中标为待核实。
          </small>
          <button
            className="button button-ghost talent-profile-adjust"
            disabled={loading}
            onClick={onAdjustConditions}
            type="button"
          >
            调整条件
          </button>
        </section>
      )}
      {!isProcessing && run.status === "partial" && !hasSemanticResults && (
        <p className="talent-profile-run-note">当前未生成可用的 AI 核验结论，请稍后刷新查看失败项。</p>
      )}
      {run.candidate_recall.next_cursor && shouldShowRecall && (
        <button className="button button-ghost talent-profile-load-more" disabled={loading} onClick={onLoadMore} type="button">
          加载更多已召回候选人
        </button>
      )}
    </section>
  );
}

function TalentSearchProfileCard({
  profile,
  run,
  onSupplement,
  onRegenerate,
  onConfirm,
  onStart,
  onRefreshRun,
  onLoadMoreRecall,
  onAdjustConditions,
  onOpenCandidate,
  loading,
}: {
  profile: TalentSearchProfile;
  run?: TalentSearchRun;
  onSupplement: () => void;
  onRegenerate: () => void;
  onConfirm: () => void;
  onStart: () => void;
  onRefreshRun: () => void;
  onLoadMoreRecall: () => void;
  onAdjustConditions: () => void;
  onOpenCandidate: (candidate: RecruitingAgentCandidate) => void;
  loading: boolean;
}) {
  const revision = profile.current_revision;
  const hardFilters = talentProfileHardFilterLabels(revision.hard_filters);
  const confirmed = profile.status === "confirmed" && revision.status === "confirmed";
  return (
    <section className="talent-profile-card" aria-label="AI 人才画像">
      <div className="talent-profile-card-heading">
        <div>
          <span>AI 人才画像</span>
          <strong>{revision.title}</strong>
        </div>
        <small className={`talent-profile-status is-${confirmed ? "confirmed" : "draft"}`}>
          {confirmed ? "已确认" : "待确认"}
        </small>
      </div>
      <p className="talent-profile-summary">{revision.summary}</p>
      <div className="talent-profile-meta">
        <span>{profile.source_type === "job" ? "来源：已保存 JD" : "来源：HR 描述"}</span>
        <span>版本 {revision.revision_number}</span>
      </div>
      {!!hardFilters.length && (
        <div className="talent-profile-section">
          <span>硬条件</span>
          <div className="talent-profile-chips">
            {hardFilters.map((label) => <small key={label}>{label}</small>)}
          </div>
          <small className="talent-profile-filter-note">
            院校类型内满足任一即可；它与学历、年限、经历、精确技能等其他硬条件同时生效。
          </small>
        </div>
      )}
      {!!revision.verification_requirements.length && (
        <div className="talent-profile-section">
          <span>重点核验</span>
          <ul className="talent-profile-requirements">
            {revision.verification_requirements.map((item) => (
              <li key={item.key}>
                <strong>{item.label}</strong>
                <small>{item.evidence_hint}</small>
              </li>
            ))}
          </ul>
        </div>
      )}
      {!!revision.preferred_requirements.length && (
        <div className="talent-profile-section">
          <span>优先项</span>
          <ul className="talent-profile-requirements">
            {revision.preferred_requirements.map((item) => (
              <li key={item.key}>
                <strong>{item.label}</strong>
                <small>{item.evidence_hint}</small>
              </li>
            ))}
          </ul>
        </div>
      )}
      {!!revision.aliases.length && (
        <div className="talent-profile-section">
          <span>可识别表达</span>
          <div className="talent-profile-chips is-muted">
            {revision.aliases.map((alias) => <small key={alias}>{alias}</small>)}
          </div>
        </div>
      )}
      {!!revision.clarifying_questions.length && !confirmed && (
        <div className="talent-profile-question">
          <Icon name="spark" size={14} />
          <span>{revision.clarifying_questions.join("；")}</span>
        </div>
      )}
      <div className="talent-profile-actions">
        <button
          className="button button-ghost"
          disabled={loading}
          onClick={onSupplement}
          type="button"
        >
          补充条件
        </button>
        {!confirmed && (
          <button
            className="button button-ghost"
            disabled={loading}
            onClick={onRegenerate}
            type="button"
          >
            <Icon name="refresh" size={14} />重新生成
          </button>
        )}
        {confirmed && !run ? (
          <button className="button button-primary" disabled={loading} onClick={onStart} type="button">
            <Icon name="match" size={15} />开始找人
          </button>
        ) : !confirmed ? (
          <button className="button button-primary" disabled={loading} onClick={onConfirm} type="button">
            <Icon name="check" size={15} />确认画像
          </button>
        ) : null}
      </div>
      {run && (
        <TalentSearchRunPanel
          loading={loading}
          onOpenCandidate={onOpenCandidate}
          onLoadMore={onLoadMoreRecall}
          onRefresh={onRefreshRun}
          onAdjustConditions={onAdjustConditions}
          run={run}
        />
      )}
    </section>
  );
}

function RecruitingAgentDrawer({
  isOpen,
  onClose,
  onOpenMatchWorkspace,
  onOpenScoreWorkspace,
  onOpenMailboxSettings,
  onOpenResume,
}: {
  isOpen: boolean;
  onClose: () => void;
  onOpenMatchWorkspace: () => void;
  onOpenScoreWorkspace: () => void;
  onOpenMailboxSettings: () => void;
  onOpenResume: (candidate: RecruitingAgentCandidate) => void;
}) {
  const [input, setInput] = useState("");
  const [jobs, setJobs] = useState<JobVersion[]>([]);
  const [jobVersionId, setJobVersionId] = useState("");
  const [composerContext, setComposerContext] = useState<AgentComposerContext>("assistant");
  const [activeTalentProfile, setActiveTalentProfile] = useState<{
    profileId: string;
    revisionId: string;
  } | null>(null);
  const [recentTalentProfiles, setRecentTalentProfiles] = useState<TalentSearchProfile[]>([]);
  const [loading, setLoading] = useState(false);
  const drawerRef = useRef<HTMLElement | null>(null);
  const closeButtonRef = useRef<HTMLButtonElement | null>(null);
  const composerInputRef = useRef<HTMLTextAreaElement | null>(null);
  const [messages, setMessages] = useState<AgentChatMessage[]>([
    {
      id: 1,
      role: "assistant",
      content:
        "我是招聘助手。可以在当前工作区筛选简历、处理 JD 匹配、查看排行榜，并按已有评分规则发起全量评分。需要发起一轮主动找人时，点击“新建人才画像”；我会先整理条件，等你确认后才开始找人。",
    },
  ]);

  useEffect(() => {
    if (!isOpen) return;
    void api
      .listConfirmedJobVersions()
      .then((items) => {
        const matchableJobs = items.filter(
          (item) => item.requirements.length > 0,
        );
        setJobs(items);
        setJobVersionId((current) =>
          current &&
          items.some((item) => item.job_version_id === current)
            ? current
            : (matchableJobs[0]?.job_version_id ?? items[0]?.job_version_id ?? ""),
        );
      })
      .catch(() => setJobs([]));
    void api
      .listTalentSearchProfiles()
      .then((response) => setRecentTalentProfiles(response.items))
      .catch(() => setRecentTalentProfiles([]));
  }, [isOpen]);

  useEffect(() => {
    if (!isOpen) return;
    const frame = window.requestAnimationFrame(() => closeButtonRef.current?.focus());
    return () => window.cancelAnimationFrame(frame);
  }, [isOpen]);

  const trapFocus = (event: ReactKeyboardEvent<HTMLElement>) => {
    if (event.key !== "Tab") return;
    const drawer = drawerRef.current;
    if (!drawer) return;
    const focusable = Array.from(
      drawer.querySelectorAll<HTMLElement>(
        'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
      ),
    ).filter((element) => !element.hidden && element.getAttribute("aria-hidden") !== "true");
    if (!focusable.length) {
      event.preventDefault();
      return;
    }
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };

  const addAssistantReply = (turn: RecruitingAgentTurn) => {
    setMessages((current) => [
      ...current,
      {
        id: Date.now() + 1,
        role: "assistant",
        content: turn.message,
        candidates: turn.candidates,
        actions: turn.actions,
        searchSummary: turn.search_summary,
      },
    ]);
    if (turn.job_version_id) setJobVersionId(turn.job_version_id);
  };

  const updateTalentProfileMessage = (
    profile: TalentSearchProfile,
    run?: TalentSearchRun,
  ) => {
    setMessages((current) => current.map((item) => (
      item.talentProfile?.profile_id === profile.profile_id
        ? { ...item, talentProfile: profile, talentRun: run }
        : item
    )));
  };

  const appendTalentProfileReply = (profile: TalentSearchProfile, content: string) => {
    setMessages((current) => [
      ...current,
      {
        id: Date.now() + 1,
        role: "assistant",
        content,
        talentProfile: profile,
      },
    ]);
  };

  const rememberTalentProfile = (profile: TalentSearchProfile) => {
    setRecentTalentProfiles((current) => [
      profile,
      ...current.filter((item) => item.profile_id !== profile.profile_id),
    ].slice(0, 12));
  };

  const addTalentProfileFailure = (error: unknown) => {
    setMessages((current) => [
      ...current,
      {
        id: Date.now() + 1,
        role: "assistant",
        content: humanizeError(error),
        failure: true,
      },
    ]);
  };

  const prepareTalentProfileRefinement = (profile: TalentSearchProfile) => {
    setActiveTalentProfile({
      profileId: profile.profile_id,
      revisionId: profile.current_revision.revision_id,
    });
    setComposerContext("refine_profile");
    setInput("");
    window.requestAnimationFrame(() => composerInputRef.current?.focus());
  };

  const startNewTalentProfile = () => {
    if (loading) return;
    setActiveTalentProfile(null);
    setComposerContext("new_profile");
    setInput("");
    window.requestAnimationFrame(() => composerInputRef.current?.focus());
  };

  const returnToAssistant = () => {
    setComposerContext("assistant");
    setInput("");
    window.requestAnimationFrame(() => composerInputRef.current?.focus());
  };

  const regenerateTalentProfile = async (profile: TalentSearchProfile) => {
    if (loading) return;
    setLoading(true);
    try {
      const next = await api.refineTalentSearchProfile(profile.profile_id, {
        revision_id: profile.current_revision.revision_id,
        message: "请保留原始招聘目标，重新梳理一版人才画像。删去不明确的硬条件，并给出需要 HR 核验的重点。",
      });
      setActiveTalentProfile({
        profileId: next.profile_id,
        revisionId: next.current_revision.revision_id,
      });
      rememberTalentProfile(next);
      setComposerContext("refine_profile");
      updateTalentProfileMessage(next);
    } catch (error) {
      addTalentProfileFailure(error);
    } finally {
      setLoading(false);
    }
  };

  const confirmTalentProfile = async (profile: TalentSearchProfile) => {
    if (loading) return;
    setLoading(true);
    try {
      const confirmed = await api.confirmTalentSearchProfile(profile.profile_id, {
        revision_id: profile.current_revision.revision_id,
      });
      setActiveTalentProfile({
        profileId: confirmed.profile_id,
        revisionId: confirmed.current_revision.revision_id,
      });
      rememberTalentProfile(confirmed);
      setComposerContext("assistant");
      updateTalentProfileMessage(confirmed);
    } catch (error) {
      addTalentProfileFailure(error);
    } finally {
      setLoading(false);
    }
  };

  const startTalentProfileSearch = async (profile: TalentSearchProfile) => {
    if (loading) return;
    setLoading(true);
    try {
      const run = await api.startTalentSearchProfileRun(profile.profile_id, {
        revision_id: profile.current_revision.revision_id,
        limit: 20,
      });
      setComposerContext("assistant");
      updateTalentProfileMessage(profile, run);
    } catch (error) {
      addTalentProfileFailure(error);
    } finally {
      setLoading(false);
    }
  };

  const refreshTalentProfileRun = async (profile: TalentSearchProfile, run: TalentSearchRun) => {
    if (loading) return;
    setLoading(true);
    try {
      const refreshed = await api.getTalentSearchProfileRun(profile.profile_id, run.run_id, { limit: 20 });
      updateTalentProfileMessage(profile, refreshed);
    } catch (error) {
      addTalentProfileFailure(error);
    } finally {
      setLoading(false);
    }
  };

  const loadMoreTalentProfileRecall = async (
    profile: TalentSearchProfile,
    run: TalentSearchRun,
  ) => {
    const cursor = run.candidate_recall.next_cursor;
    if (loading || !cursor) return;
    setLoading(true);
    try {
      const next = await api.getTalentSearchProfileRun(profile.profile_id, run.run_id, {
        limit: 20,
        cursor,
      });
      const seen = new Set(run.candidate_recall.items.map((item) => item.resume_id));
      updateTalentProfileMessage(profile, {
        ...next,
        candidate_recall: {
          ...next.candidate_recall,
          items: [
            ...run.candidate_recall.items,
            ...next.candidate_recall.items.filter((item) => !seen.has(item.resume_id)),
          ],
        },
      });
    } catch (error) {
      addTalentProfileFailure(error);
    } finally {
      setLoading(false);
    }
  };

  const resumeTalentProfile = async (profileId: string) => {
    if (loading) return;
    setLoading(true);
    try {
      const profile = await api.getTalentSearchProfile(profileId);
      setComposerContext(profile.status === "confirmed" ? "assistant" : "refine_profile");
      setActiveTalentProfile({
        profileId: profile.profile_id,
        revisionId: profile.current_revision.revision_id,
      });
      rememberTalentProfile(profile);
      appendTalentProfileReply(
        profile,
        profile.status === "confirmed"
          ? "已恢复这份已确认的人才画像。可查看本次找人结果，或补充条件后形成新草案。"
          : "已恢复这份人才画像草案。请确认，或继续补充条件。",
      );
    } catch (error) {
      addTalentProfileFailure(error);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (!isOpen || loading) return undefined;
    const pending = messages.find((item) => (
      item.talentProfile
      && item.talentRun
      && (item.talentRun.status === "queued" || item.talentRun.status === "running")
    ));
    if (!pending?.talentProfile || !pending.talentRun) return undefined;
    const timer = window.setTimeout(() => {
      void api.getTalentSearchProfileRun(
        pending.talentProfile!.profile_id,
        pending.talentRun!.run_id,
        { limit: 20 },
      ).then((refreshed) => {
        updateTalentProfileMessage(pending.talentProfile!, refreshed);
      }).catch(() => {
        // A transient poll failure should not flood the recruiter chat. The
        // visible refresh button remains available for an explicit retry.
      });
    }, 4_000);
    return () => window.clearTimeout(timer);
  }, [isOpen, loading, messages]);

  const send = async (
    raw: string,
    snapshot?: AgentSendSnapshot,
    options?: { clearComposer?: boolean },
  ) => {
    const message = raw.trim();
    if (!message || loading) return;
    const request = snapshot ?? {
      composerContext,
      activeTalentProfile,
      jobVersionId,
    };
    const isProfileWorkflow = request.composerContext === "new_profile"
      || (request.composerContext === "refine_profile" && request.activeTalentProfile !== null);
    const isRefinement = request.composerContext === "refine_profile"
      && request.activeTalentProfile !== null;
    if (options?.clearComposer !== false) setInput("");
    setMessages((current) => [
      ...current,
      { id: Date.now(), role: "user", content: message },
    ]);
    setLoading(true);
    try {
      if (isProfileWorkflow) {
        const profile = isRefinement && request.activeTalentProfile
          ? await api.refineTalentSearchProfile(request.activeTalentProfile.profileId, {
            revision_id: request.activeTalentProfile.revisionId,
            message,
          })
          : await api.generateTalentSearchProfile({
            message,
            job_version_id: request.jobVersionId || null,
          });
        setActiveTalentProfile({
          profileId: profile.profile_id,
          revisionId: profile.current_revision.revision_id,
        });
        rememberTalentProfile(profile);
        setComposerContext("refine_profile");
        appendTalentProfileReply(
          profile,
          isRefinement
            ? "我已根据你的补充更新人才画像。请确认，或继续补充条件。"
            : "我先整理了一版人才画像草稿。请看硬条件和重点核验项，还想补什么吗？确认后才会开始找人。",
        );
      } else {
        // Source-only JDs are intentionally usable as input for an AI talent
        // profile, but the existing conversational assistant only understands
        // confirmed, matchable JD versions. Do not turn selecting an original
        // publication into a generic server error in the normal chat mode.
        const selectedMatchableJob = jobs.some(
          (job) => job.job_version_id === request.jobVersionId && job.requirements.length > 0,
        );
        const turn = await api.runRecruitingAgentTurn({
          message,
          job_version_id: selectedMatchableJob ? request.jobVersionId : null,
        });
        addAssistantReply(turn);
      }
    } catch (error) {
      const failureMessage = isProfileWorkflow ? humanizeError(error) : humanizeAgentError(error);
      setMessages((current) => [
        ...current,
        {
          id: Date.now() + 1,
          role: "assistant",
          content: failureMessage,
          failure: true,
          retry: isRetryableAgentError(error) ? { message, snapshot: request } : undefined,
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
      inert={!isOpen}
      onKeyDown={trapFocus}
      ref={drawerRef}
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
        <button
          aria-label="关闭招聘助手"
          className="icon-button"
          onClick={onClose}
          ref={closeButtonRef}
          type="button"
        >
          <Icon name="close" size={18} />
        </button>
      </header>
      <div className="agent-context">
        <div className="agent-context-actions">
          <button
            className="button button-ghost agent-new-profile-button"
            disabled={loading}
            onClick={startNewTalentProfile}
            type="button"
          >
            <Icon name="spark" size={14} />新建人才画像
          </button>
        </div>
        {composerContext !== "assistant" && (
          <div className="agent-profile-context" role="status">
            <span>
              {composerContext === "new_profile"
                ? "正在新建人才画像：先给出可确认草案，不会直接检索候选人。"
                : "正在补充当前人才画像：发送后会生成新草案，不会直接检索候选人。"}
            </span>
            <button className="text-button" onClick={returnToAssistant} type="button">
              返回助手
            </button>
          </div>
        )}
        <div className="select-wrap">
          <label className="sr-only" htmlFor="agent-job-version">关联 JD</label>
          <select
            className="select-field"
            id="agent-job-version"
            onChange={(event) => setJobVersionId(event.target.value)}
            value={jobVersionId}
          >
            <option value="">不关联 JD</option>
            {jobs.map((item) => (
              <option key={item.job_version_id} value={item.job_version_id}>
                {item.title} · v{item.version}{item.requirements.length ? "" : " · 原版"}
              </option>
            ))}
          </select>
          <Icon name="chevron-down" size={15} />
        </div>
        {!!recentTalentProfiles.length && (
          <div className="agent-profile-history" aria-label="继续已保存的人才画像">
            <span>继续已保存画像</span>
            <div>
              {recentTalentProfiles.slice(0, 4).map((profile) => (
                <button
                  className="button button-ghost"
                  disabled={loading}
                  key={profile.profile_id}
                  onClick={() => void resumeTalentProfile(profile.profile_id)}
                  type="button"
                >
                  {profile.current_revision.title}
                  <small>{profile.status === "confirmed" ? "已确认" : "草案"}</small>
                </button>
              ))}
            </div>
          </div>
        )}
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
            {item.retry && (
              <div className="agent-retry-row">
                <button
                  className="button button-ghost agent-retry-button"
                  disabled={loading}
                  onClick={() => void send(
                    item.retry!.message,
                    item.retry!.snapshot,
                    { clearComposer: false },
                  )}
                  type="button"
                >
                  <Icon name="refresh" size={15} />
                  重新发送
                </button>
              </div>
            )}
            {item.searchSummary && <AgentSearchSummaryPanel summary={item.searchSummary} />}
            {item.talentProfile && (
              <TalentSearchProfileCard
                loading={loading}
                onConfirm={() => void confirmTalentProfile(item.talentProfile!)}
                onOpenCandidate={onOpenResume}
                onLoadMoreRecall={() => {
                  if (item.talentRun) {
                    void loadMoreTalentProfileRecall(item.talentProfile!, item.talentRun);
                  }
                }}
                onRefreshRun={() => {
                  if (item.talentRun) {
                    void refreshTalentProfileRun(item.talentProfile!, item.talentRun);
                  }
                }}
                onRegenerate={() => void regenerateTalentProfile(item.talentProfile!)}
                onStart={() => void startTalentProfileSearch(item.talentProfile!)}
                onSupplement={() => prepareTalentProfileRefinement(item.talentProfile!)}
                onAdjustConditions={() => prepareTalentProfileRefinement(item.talentProfile!)}
                profile={item.talentProfile}
                run={item.talentRun}
              />
            )}
            {!!item.candidates?.length && (
              <div className="agent-candidate-list">
                {item.candidates.map((candidate) => (
                  <AgentCandidateCard
                    key={candidate.resume_id}
                    candidate={candidate}
                    onOpen={() => onOpenResume(candidate)}
                  />
                ))}
              </div>
            )}
            {item.actions?.some((action) => action.action === "open_match_workspace") && (
              <button className="button button-ghost agent-workspace-button" onClick={onOpenMatchWorkspace} type="button">
                <Icon name="match" size={15} />
                打开 JD 匹配工作区
              </button>
            )}
            {item.actions?.some((action) => action.action === "open_score_workspace") && (
              <button className="button button-ghost agent-workspace-button" onClick={onOpenScoreWorkspace} type="button">
                <Icon name="layers" size={15} />
                打开评分工作台
              </button>
            )}
            {item.actions?.some((action) => action.action === "open_mailbox_workspace") && (
              <button className="button button-ghost agent-workspace-button" onClick={onOpenMailboxSettings} type="button">
                <Icon name="inbox" size={15} />
                打开收件邮箱设置
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
        {composerContext !== "assistant" && (
          <p className="agent-profile-context-note">
            {composerContext === "new_profile"
              ? "我会先整理可确认的人才画像；发送后不会直接检索候选人。"
              : "这条消息会更新当前人才画像；发送后不会直接检索候选人。"}
          </p>
        )}
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
            placeholder={composerContext === "new_profile"
              ? "描述你想找的人，例如：需要有 LangChain 项目经验的本科毕业工程师"
              : composerContext === "refine_profile"
                ? "补充或调整条件，例如：正式工作和实习都要有，项目中重点看 RAG 落地"
                : "例如：找 985 或 211 院校、3 年以上 Python 的候选人；或点击“新建人才画像”发起一轮找人"}
            ref={composerInputRef}
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
  appliedDraft,
  draft,
  filterOptions,
  onDraftChange,
  savedFilters,
  search,
  searching,
  selectedResumeId,
  onReset,
  onSave,
  onApplySaved,
  onDeleteSaved,
  onOpenCandidate,
  onScoreTemplateChange,
  onLoadMore,
  onUpload,
  scoreTemplateId,
  scoreTemplates,
}: {
  appliedDraft: FilterDraft;
  draft: FilterDraft;
  filterOptions: FilterOptions;
  onDraftChange: (draft: FilterDraft, timing?: "immediate" | "debounced") => void;
  savedFilters: SavedFilter[];
  search: CandidateSearchResponse;
  searching: boolean;
  selectedResumeId: string | null;
  onReset: () => void;
  onSave: (name: string) => Promise<void>;
  onApplySaved: (filter: SavedFilter) => boolean;
  onDeleteSaved: (filter: SavedFilter) => Promise<void>;
  onOpenCandidate: (item: CandidateSearchItem, tab?: DrawerTab) => void;
  onScoreTemplateChange: (templateId: string | null) => void;
  onLoadMore: () => void;
  onUpload: () => void;
  scoreTemplateId: string | null;
  scoreTemplates: ScoreTemplate[];
}) {
  return (
    <div className="filter-workspace">
      <FilterPanel
        draft={draft}
        filterOptions={filterOptions}
        onApplySaved={onApplySaved}
        onDeleteSaved={onDeleteSaved}
        onDraftChange={onDraftChange}
        onReset={onReset}
        onSave={onSave}
        savedFilters={savedFilters}
      />
      <ResultsPane
        appliedDraft={appliedDraft}
        onLoadMore={onLoadMore}
        onOpenCandidate={onOpenCandidate}
        onScoreTemplateChange={onScoreTemplateChange}
        onUpload={onUpload}
        search={search}
        searching={searching}
        selectedResumeId={selectedResumeId}
        scoreTemplateId={scoreTemplateId}
        scoreTemplates={scoreTemplates}
      />
    </div>
  );
}

function FilterPanel({
  draft,
  filterOptions,
  onDraftChange,
  savedFilters,
  onReset,
  onSave,
  onApplySaved,
  onDeleteSaved,
}: {
  draft: FilterDraft;
  filterOptions: FilterOptions;
  onDraftChange: (draft: FilterDraft, timing?: "immediate" | "debounced") => void;
  savedFilters: SavedFilter[];
  onReset: () => void;
  onSave: (name: string) => Promise<void>;
  onApplySaved: (filter: SavedFilter) => boolean;
  onDeleteSaved: (filter: SavedFilter) => Promise<void>;
}) {
  const [selectedSavedId, setSelectedSavedId] = useState("");
  const [saveName, setSaveName] = useState("");
  const [saving, setSaving] = useState(false);
  const [mobileFiltersOpen, setMobileFiltersOpen] = useState(false);
  const institutionClassifications = resolvedInstitutionClassificationOptions(filterOptions);

  const update = (patch: Partial<FilterDraft>) =>
    onDraftChange({ ...draft, ...patch });
  const updateAfterTyping = (patch: Partial<FilterDraft>) =>
    onDraftChange({ ...draft, ...patch }, "debounced");
  const applySaved = (id: string) => {
    setSelectedSavedId(id);
    const saved = savedFilters.find((item) => item.saved_filter_id === id);
    if (saved && !onApplySaved(saved)) setSelectedSavedId("");
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
    <aside
      aria-label="筛选条件"
      className={`filter-panel${mobileFiltersOpen ? " is-mobile-open" : ""}`}
    >
      <div className="filter-panel-header">
        <h2 className="filter-panel-title">筛选条件</h2>
        <div className="filter-panel-header-actions">
          <button
            aria-controls="filter-controls"
            aria-expanded={mobileFiltersOpen}
            className="text-button filter-mobile-toggle"
            onClick={() => setMobileFiltersOpen((current) => !current)}
            type="button"
          >
            <Icon name="filter" size={15} />
            {mobileFiltersOpen ? "收起筛选" : "展开筛选"}
          </button>
          <button
            className="text-button"
            onClick={() => void onReset()}
            type="button"
          >
            清空
          </button>
        </div>
      </div>
      <div className="filter-scroll" id="filter-controls">
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
            <span>任一满足</span>
          </div>
          <div className="field-stack">
            <span className="field-label">院校类型</span>
            <div className="choice-grid" aria-label="院校类型条件">
              {institutionClassifications.map((option) => (
                <label className="choice-row" key={option.value}>
                  <input
                    checked={draft.institutionClassifications.includes(option.value)}
                    onChange={() =>
                      update({
                        institutionClassifications: sortInstitutionClassifications(
                          draft.institutionClassifications.includes(option.value)
                            ? draft.institutionClassifications.filter(
                                (value) => value !== option.value,
                              )
                            : [
                                ...draft.institutionClassifications,
                                option.value,
                              ],
                        ),
                      })
                    }
                    type="checkbox"
                  />
                  {option.label}
                </label>
              ))}
            </div>
            <span className="field-hint">已选院校类型满足任一即可。</span>
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
          <span className="field-hint">已选最高学历满足任一即可。</span>
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
                  onChange={(event) => updateAfterTyping({ freshGraduateStartMonth: event.target.value })}
                  type="month"
                  value={draft.freshGraduateStartMonth}
                />
              </label>
              <label className="field-stack">
                <span className="field-label">应届窗口结束</span>
                <input
                  className="field"
                  onChange={(event) => updateAfterTyping({ freshGraduateEndMonth: event.target.value })}
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
              onChange={(event) => updateAfterTyping({ schoolName: event.target.value })}
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
                  onChange={(event) => updateAfterTyping({ minAverageScore: event.target.value })}
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
                  onChange={(event) => updateAfterTyping({ minGpaPercent: event.target.value })}
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
                  onChange={(event) => updateAfterTyping({ maxRankPosition: event.target.value })}
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
                  onChange={(event) => updateAfterTyping({ maxRankPercent: event.target.value })}
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
              onChange={(event) => updateAfterTyping({ major: event.target.value })}
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
                updateAfterTyping({
                  minEmploymentMonths: clampMonths(Number(event.target.value)),
                })
              }
              step="12"
              type="range"
              value={draft.minEmploymentMonths}
            />
            <div className="range-values">
              <span>{formatMinimumDuration(draft.minEmploymentMonths)}</span>
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
                updateAfterTyping({
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
              <span>{formatMinimumDuration(draft.minEmploymentOrInternshipMonths)}</span>
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
              onChange={(event) => updateAfterTyping({ experienceName: event.target.value })}
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
              onChange={(event) => updateAfterTyping({ company: event.target.value })}
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
              onChange={(event) => updateAfterTyping({ title: event.target.value })}
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
              onChange={(event) => updateAfterTyping({ experienceAwardResult: event.target.value })}
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
                        updateAfterTyping({
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
              onChange={(event) => updateAfterTyping({ customLanguageName: event.target.value })}
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
                onChange={(event) => updateAfterTyping({ scholarshipName: event.target.value })}
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

function InstitutionClassificationTags({
  classifications,
}: {
  classifications: readonly InstitutionClassification[] | null | undefined;
}) {
  const orderedClassifications = sortInstitutionClassifications(classifications);
  if (!orderedClassifications.length) {
    return <span className="candidate-meta">待核实</span>;
  }
  return (
    <div className="institution-classification-tags">
      {orderedClassifications.map((classification) => (
        <span className="tag" key={classification}>
          {institutionClassificationLabel(classification)}
        </span>
      ))}
    </div>
  );
}

interface ResultDisplayColumn {
  key: CandidateSearchDisplayFieldKey;
  label: string;
}

function activeResultDisplayColumns(draft: FilterDraft): ResultDisplayColumn[] {
  const columns: ResultDisplayColumn[] = [];
  const add = (key: CandidateSearchDisplayFieldKey, label: string) => {
    if (!columns.some((column) => column.key === key)) {
      columns.push({ key, label });
    }
  };

  if (draft.graduationStatus !== "any") add("graduation", "毕业时间");
  if (draft.minEmploymentMonths > 0) {
    add("employment_months", "正式工作年限");
  }
  if (draft.minEmploymentOrInternshipMonths > 0) {
    add("employment_or_internship_months", "工作 + 实习年限");
  }

  if (draft.schoolName.trim()) add("school", "学校");
  if (draft.major.trim()) add("major", "专业");
  if (
    draft.minAverageScore ||
    draft.minGpaPercent ||
    draft.maxRankPosition ||
    draft.maxRankPercent
  ) {
    add("academic_performance", "学业表现");
  }

  if (draft.experienceTypes.length) add("experience_type", "经历类型");
  if (draft.experienceName.trim()) add("experience_name", "经历名称");
  if (draft.company.trim()) add("organization", "公司 / 组织");
  if (draft.title.trim()) add("title", "职位");
  if (
    draft.experienceAwardLevels.length ||
    draft.experienceAwardResult.trim()
  ) {
    add("experience_award", "经历获奖");
  }

  if (draft.skills.length || draft.skillCategories.length) add("skills", "技能");
  if (
    draft.languageCredentials.some(
      (credential) =>
        credential !== "custom" || Boolean(draft.customLanguageName.trim()),
    )
  ) {
    add("language", "语言证书");
  }
  if (
    draft.scholarshipStatus !== "any" ||
    draft.scholarshipName.trim() ||
    draft.scholarshipLevels.length
  ) {
    add("scholarship", "奖学金");
  }
  if (
    draft.competitionStatus !== "any" ||
    draft.competitionAwardStatus !== "any"
  ) {
    add("competition", "竞赛");
  }
  if (draft.leadershipContexts.length || draft.leadershipRoles.length) {
    add("leadership", "领导经历");
  }
  if (draft.keywords.length) add("keywords", "关键词命中");

  return columns;
}

function resultDisplayValueLabel(
  key: CandidateSearchDisplayFieldKey,
  value: string,
): string {
  const normalized = value.trim();
  if (!normalized) return "";

  if (key === "institution_classifications") {
    return (
      institutionClassificationLabels[
        normalized as InstitutionClassification
      ] ?? normalized
    );
  }
  if (key === "highest_degree" || key === "education_degree") {
    return degreeLabels[normalized as DegreeLevel] ?? normalized;
  }
  if (key === "experience_type") {
    return (
      experienceTypeOptions.find((option) => option.value === normalized)
        ?.label ?? normalized
    );
  }
  if (
    key === "employment_months" ||
    key === "employment_or_internship_months"
  ) {
    const months = Number(normalized);
    return Number.isFinite(months) ? formatDuration(months) : normalized;
  }
  return normalized;
}

function resultDisplayValues(
  item: CandidateSearchItem,
  key: CandidateSearchDisplayFieldKey,
): string[] {
  const values = (item.display_fields ?? [])
    .filter((field) => field.key === key)
    .flatMap((field) => field.values)
    .map((value) => resultDisplayValueLabel(key, value))
    .filter(Boolean);
  return [...new Set(values)];
}

function ResultDisplayValues({
  item,
  fieldKey,
  label,
}: {
  item: CandidateSearchItem;
  fieldKey: CandidateSearchDisplayFieldKey;
  label?: string;
}) {
  const values = resultDisplayValues(item, fieldKey);
  if (!values.length) {
    return <span className="candidate-meta result-display-empty">—</span>;
  }

  return (
    <div
      aria-label={`${label ?? "筛选字段"}：${values.join("；")}`}
      className="result-display-values"
      title={values.join("；")}
    >
      {values.slice(0, 2).map((value) => (
        <span className="result-display-value" key={value}>
          {value}
        </span>
      ))}
      {values.length > 2 && (
        <span className="result-display-more">+{values.length - 2} 项</span>
      )}
    </div>
  );
}

function ResultColumnHeader({
  children,
  active = false,
}: {
  children: ReactNode;
  active?: boolean;
}) {
  return (
    <span className="result-column-heading">
      {children}
      {active && <span className="result-filter-indicator">已筛</span>}
    </span>
  );
}

function scoreConfidencePresentation(value: number | null): {
  label: string;
  tone: "grounded" | "partial" | "unknown";
} {
  if (value === null) {
    return { label: "依据待核实", tone: "unknown" };
  }
  if (value >= 80) {
    return { label: `可信度高 · ${value.toFixed(0)}%`, tone: "grounded" };
  }
  if (value >= 50) {
    return { label: `可信度中 · ${value.toFixed(0)}%`, tone: "partial" };
  }
  return { label: `待核实 · ${value.toFixed(0)}%`, tone: "unknown" };
}

function scoreStatusLabel(status: string | null): string | null {
  if (status === "overridden") return "含人工调整";
  if (status === "needs_review") return "建议复核";
  if (status === "succeeded") return null;
  return status ? "评分待更新" : null;
}

function CandidateEducationCell({ item }: { item: CandidateSearchItem }) {
  const hasEducation = Boolean(
    item.highest_degree ||
      item.education_school ||
      item.education_major ||
      item.institution_classifications.length,
  );
  if (!hasEducation) return <span className="candidate-meta">未识别</span>;
  return (
    <div className="candidate-profile-cell candidate-education-cell">
      <div className="candidate-profile-primary">
        {item.highest_degree && (
          <span className="degree-label">{degreeLabels[item.highest_degree]}</span>
        )}
        <span className="candidate-profile-title">
          {item.education_school || "学校信息待补充"}
        </span>
      </div>
      {item.education_major && (
        <span className="candidate-meta">{item.education_major}</span>
      )}
      <InstitutionClassificationTags
        classifications={item.institution_classifications}
      />
    </div>
  );
}

function CandidateExperienceCell({ item }: { item: CandidateSearchItem }) {
  const experienceType = experienceTypeOptions.find(
    (option) => option.value === item.latest_experience_type,
  )?.label;
  const role = [
    item.latest_experience_title,
    item.latest_experience_organization,
  ]
    .filter(Boolean)
    .join(" · ");
  const hasVerifiedEmployment = item.employment_months > 0;
  const hasAdditionalInternshipTenure =
    item.employment_or_internship_months > item.employment_months;
  return (
    <div className="candidate-profile-cell">
      <div className="candidate-profile-primary">
        <span
          aria-label={
            hasVerifiedEmployment
              ? `正式工作 ${formatDuration(item.employment_months)}`
              : "正式工作年限待核实"
          }
          className="candidate-profile-title"
          title="正式工作年限仅累计有明确工作类型、公司、职位和起止日期的工作经历；实习单独计入“工作 + 实习”。"
        >
          {hasVerifiedEmployment
            ? `${formatDuration(item.employment_months)} 正式工作`
            : "正式工作年限待核实"}
        </span>
      </div>
      {hasAdditionalInternshipTenure && (
        <span className="candidate-meta">
          工作 + 实习 {formatDuration(item.employment_or_internship_months)}
        </span>
      )}
      {role ? (
        <span className="candidate-meta">
          {experienceType ? `${experienceType} · ` : ""}
          {role}
        </span>
      ) : (
        <span className="candidate-meta">最近岗位信息待补充</span>
      )}
    </div>
  );
}

function CandidateSkillHighlights({ item }: { item: CandidateSearchItem }) {
  const skills = item.skill_highlights ?? [];
  if (!skills.length) return <span className="candidate-meta">未识别</span>;
  return (
    <div
      aria-label={`核心技能：${skills.join("；")}`}
      className="candidate-skill-highlights"
      title={skills.join("；")}
    >
      {skills.slice(0, 3).map((skill) => (
        <span className="tag" key={skill}>{skill}</span>
      ))}
      {skills.length > 3 && <span className="candidate-skills-more">+{skills.length - 3}</span>}
    </div>
  );
}

function ResultsPane({
  appliedDraft,
  search,
  searching,
  selectedResumeId,
  onOpenCandidate,
  onScoreTemplateChange,
  onLoadMore,
  onUpload,
  scoreTemplateId,
  scoreTemplates,
}: {
  appliedDraft: FilterDraft;
  search: CandidateSearchResponse;
  searching: boolean;
  selectedResumeId: string | null;
  onOpenCandidate: (item: CandidateSearchItem, tab?: DrawerTab) => void;
  onScoreTemplateChange: (templateId: string | null) => void;
  onLoadMore: () => void;
  onUpload: () => void;
  scoreTemplateId: string | null;
  scoreTemplates: ScoreTemplate[];
}) {
  const displayColumns = activeResultDisplayColumns(appliedDraft);
  const hasAppliedDisplayColumns = displayColumns.length > 0;
  const selectedScoreTemplate = scoreTemplates.find(
    (template) => template.template_id === scoreTemplateId,
  );
  const scoreOrderLabel = selectedScoreTemplate
    ? `按“${selectedScoreTemplate.name} · v${selectedScoreTemplate.version}”综合评分排序`
    : "未选择统一评分模板，按最近更新排序";

  return (
    <section className="results-pane" aria-label="候选人结果">
      <header className="results-header">
        <div className="results-summary">
          <h1>候选人结果</h1>
          <p>
            {search.items.length
              ? `当前已加载 ${search.items.length} 位候选人，${scoreOrderLabel}`
              : "仅显示已完成 AI 提取并启用的简历"}
          </p>
        </div>
        <div className="results-toolbar">
          <label className="score-sort-control">
            <span>评分口径</span>
            <span className="select-wrap">
              <select
                aria-label="综合评分排序规则"
                className="select-field"
                onChange={(event) => onScoreTemplateChange(event.target.value || null)}
                value={scoreTemplateId ?? ""}
              >
                <option value="">不按评分排序</option>
                {scoreTemplates.map((template) => (
                  <option key={template.template_id} value={template.template_id}>
                    {template.name} · v{template.version}
                  </option>
                ))}
              </select>
              <Icon name="chevron-down" size={15} />
            </span>
          </label>
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
      <div
        aria-label="候选人结果，可横向滚动查看筛选字段"
        className="table-scroll"
        role="region"
        tabIndex={0}
      >
        {searching && !search.items.length ? (
          <TableSkeleton />
        ) : search.items.length ? (
          <table
            className={`candidate-table${
              hasAppliedDisplayColumns ? " has-active-filter-columns" : ""
            }`}
          >
            <thead>
              <tr>
                <th scope="col">候选人</th>
                <th scope="col">学历 / 院校</th>
                <th scope="col">经历</th>
                <th scope="col">核心技能</th>
                {displayColumns.map((column) => (
                  <th className="result-display-column" key={column.key} scope="col">
                    <ResultColumnHeader active>{column.label}</ResultColumnHeader>
                  </th>
                ))}
                <th scope="col">综合评分</th>
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
                    if (
                      event.target instanceof HTMLElement &&
                      event.target.closest("button, a, input, select, textarea")
                    ) {
                      return;
                    }
                    if (event.key === "Enter" || event.key === " ") {
                      event.preventDefault();
                      onOpenCandidate(item);
                    }
                  }}
                  tabIndex={0}
                >
                  <td className="candidate-result-cell">
                    <div className="candidate-person">
                      <span className="candidate-name">
                        {item.display_name?.trim() || "未命名候选人"}
                      </span>
                    </div>
                  </td>
                  <td>
                    <CandidateEducationCell item={item} />
                  </td>
                  <td>
                    <CandidateExperienceCell item={item} />
                  </td>
                  <td><CandidateSkillHighlights item={item} /></td>
                  {displayColumns.map((column) => (
                    <td className="result-display-cell" key={column.key}>
                      <ResultDisplayValues
                        fieldKey={column.key}
                        item={item}
                        label={column.label}
                      />
                    </td>
                  ))}
                  <td className="candidate-score-cell">
                    {item.score_total !== null ? (
                      <div className="candidate-score-summary">
                        <button
                          aria-label={`查看 ${item.display_name ?? "候选人"} 的评分详情`}
                          className="library-score candidate-score-link"
                          onClick={(event) => {
                            event.stopPropagation();
                            onOpenCandidate(item, "score");
                          }}
                          type="button"
                        >
                          <strong>{item.score_total.toFixed(1)}</strong>
                          <span>/ 100</span>
                          {item.score_template_name && (
                            <small>{item.score_template_name}</small>
                          )}
                        </button>
                        <span
                          className={`score-confidence is-${scoreConfidencePresentation(item.score_confidence).tone}`}
                        >
                          {scoreConfidencePresentation(item.score_confidence).label}
                        </span>
                        {scoreStatusLabel(item.score_status) && (
                          <span className="candidate-score-status">
                            {scoreStatusLabel(item.score_status)}
                          </span>
                        )}
                      </div>
                    ) : (
                      <span className="library-empty-copy">尚未评分</span>
                    )}
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

function resumeLibraryScoreState(status: string | null): string {
  switch (status) {
    case "overridden":
      return "含人工调整";
    case "needs_review":
      return "建议复核";
    case "succeeded":
      return "AI 已完成";
    default:
      return "评分已生成";
  }
}

function ResumeLibraryPage({
  selectedResumeId,
  refreshToken,
  onOpenResume,
  onUpload,
}: {
  selectedResumeId: string | null;
  refreshToken: number;
  onOpenResume: (item: ResumeLibraryItem) => void;
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
  const pageOverview = items.reduce(
    (summary, item) => {
      const status = resumeLibraryStatus(item);
      summary[status.tone] += 1;
      if (status.tone === "ready" && item.score_total === null) {
        summary.unscored += 1;
      }
      return summary;
    },
    { ready: 0, progress: 0, attention: 0, waiting: 0, unscored: 0 },
  );
  const firstItemIndex = total ? (page - 1) * RESUME_LIBRARY_PAGE_SIZE + 1 : 0;
  const lastItemIndex = Math.min(page * RESUME_LIBRARY_PAGE_SIZE, total);

  return (
    <div className="page-frame resume-library-page">
      <header className="page-heading">
        <div>
          <h1>简历库</h1>
          <p>
            一眼查看入库进度、AI 总结和 AI 评分；打开后可继续查看原始文件与提取依据。
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

      {library && (
        <section aria-label="当前页面简历状态" className="library-queue-summary">
          <span className="library-queue-total"><strong>{total}</strong> 份已入库</span>
          <span className="library-queue-item is-ready">本页已启用 <strong>{pageOverview.ready}</strong></span>
          {(pageOverview.progress + pageOverview.waiting) > 0 && <span className="library-queue-item is-progress">处理中 <strong>{pageOverview.progress + pageOverview.waiting}</strong></span>}
          {pageOverview.attention > 0 && <span className="library-queue-item is-attention">需处理 <strong>{pageOverview.attention}</strong></span>}
          {pageOverview.unscored > 0 && <span className="library-queue-item">待评分 <strong>{pageOverview.unscored}</strong></span>}
        </section>
      )}

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
                  <th scope="col">候选人</th>
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
                      <td className="library-candidate-cell">
                        <div className="candidate-person">
                          <span className="candidate-name">
                            {item.display_name?.trim() || "未命名候选人"}
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
                            title={`${item.score_template_name ?? "评分模板"} · ${resumeLibraryScoreState(item.score_status)}`}
                          >
                            <strong>{item.score_total.toFixed(1)}</strong>
                            <span>/ 100</span>
                            <small>{item.score_template_name ?? "评分模板"} · {resumeLibraryScoreState(item.score_status)}</small>
                          </div>
                        ) : item.is_active ? (
                          <span className="library-empty-copy">
                            尚无通用评分
                          </span>
                        ) : (
                          <span className="library-empty-copy">
                            完成提取后可评分
                          </span>
                        )}
                      </td>
                      <td className="library-status-cell">
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
                        {status.tone === "progress" && (
                          <small>完成后会自动更新</small>
                        )}
                      </td>
                      <td>
                        <span className="candidate-meta">
                          {formatLibraryDate(item.created_at)}
                        </span>
                      </td>
                      <td className="library-open-cell">
                        <span aria-hidden="true" className="library-open-affordance">
                          查看 <Icon name="chevron-right" size={17} />
                        </span>
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
            total ? `显示第 ${firstItemIndex}–${lastItemIndex} 份，共 ${total} 份` : "共 0 份简历"
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

function candidateDataDeletionReasonLabel(reason: CandidateDataDeletionReason): string {
  return candidateDataDeletionReasonOptions.find((option) => option.value === reason)?.label
    ?? "系统保留策略";
}

function candidateDataExportStatusLabel(status: string): string {
  switch (status) {
    case "queued": return "等待导出";
    case "running": return "正在导出";
    case "completed": return "可下载";
    case "retryable_failed": return "等待重试";
    case "failed": return "导出失败";
    case "cancelled": return "已取消";
    case "revoked": return "已撤销";
    case "expired": return "已过期";
    default: return status;
  }
}

function candidateDataExportStatusClass(status: string): string {
  if (status === "completed") return "is-success";
  if (status === "failed" || status === "revoked" || status === "expired") return "is-error";
  if (status === "retryable_failed") return "is-warning";
  return "is-progress";
}

function candidateDataRetentionRunStatusLabel(status: string): string {
  switch (status) {
    case "completed": return "已完成";
    case "completed_with_errors": return "完成但有异常";
    case "failed": return "清理失败";
    case "running": return "正在处理";
    default: return status;
  }
}

function candidateDataAuditActionLabel(event: CandidateDataAuditEvent): string {
  const labels: Record<string, string> = {
    resume_original_view_authorized: "已授权查看原文件",
    resume_original_download_authorized: "已授权下载原文件",
    resume_delete_requested: "已请求删除当前简历",
    candidate_delete_requested: "已请求删除候选人资料",
    resume_restored: "已恢复简历",
    candidate_restored: "已恢复候选人资料",
    retention_policy_changed: "已更新保留策略",
    retention_cleanup_completed: "已执行到期清理",
    candidate_data_export_requested: "已创建资料导出",
    candidate_data_export_cancelled: "已取消资料导出",
    candidate_data_export_download_authorized: "已授权下载导出文件",
  };
  return labels[event.action] ?? event.action;
}

function CandidateDataLifecyclePage({
  notify,
  onOpenLibrary,
  embedded = false,
}: {
  notify: (kind: ToastKind, message: string) => void;
  onOpenLibrary: () => void;
  embedded?: boolean;
}) {
  const pageClassName = `candidate-data-page${embedded ? " is-embedded" : " page-frame"}`;
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [policy, setPolicy] = useState<CandidateDataRetentionPolicy | null>(null);
  const [retentionMode, setRetentionMode] = useState<CandidateDataRetentionMode>("manual");
  const [retentionDays, setRetentionDays] = useState("365");
  const [preview, setPreview] = useState<CandidateDataRetentionPreview | null>(null);
  const [runs, setRuns] = useState<CandidateDataRetentionCleanupRun[]>([]);
  const [deletions, setDeletions] = useState<CandidateDataDeletionBatch[]>([]);
  const [exports, setExports] = useState<CandidateDataExport[]>([]);
  const [auditEvents, setAuditEvents] = useState<CandidateDataAuditEvent[]>([]);
  const [savingPolicy, setSavingPolicy] = useState(false);
  const [previewing, setPreviewing] = useState(false);
  const [cleaning, setCleaning] = useState(false);
  const [restoringBatchId, setRestoringBatchId] = useState<string | null>(null);
  const [cancellingExportId, setCancellingExportId] = useState<string | null>(null);
  const [downloadingExportId, setDownloadingExportId] = useState<string | null>(null);

  const load = useCallback(async (showLoading = true) => {
    if (showLoading) setLoading(true);
    else setRefreshing(true);
    setError(null);
    try {
      const [nextPolicy, nextRuns, nextDeletions, nextExports, nextAuditEvents] = await Promise.all([
        api.getCandidateDataRetentionPolicy(),
        api.listCandidateDataRetentionCleanupRuns(),
        api.listCandidateDataDeletions(),
        api.listCandidateDataExports(),
        api.listCandidateDataAuditEvents(30),
      ]);
      setPolicy(nextPolicy);
      setRetentionMode(nextPolicy.mode);
      setRetentionDays(nextPolicy.retention_days ? String(nextPolicy.retention_days) : "365");
      setRuns(nextRuns.items);
      setDeletions(nextDeletions.items);
      setExports(nextExports.items);
      setAuditEvents(nextAuditEvents.items);
    } catch (loadError) {
      const message = humanizeError(loadError);
      setError(message);
      if (!showLoading) notify("error", message);
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, [notify]);

  useEffect(() => {
    void load();
  }, [load]);

  const normalizedRetentionDays = Number.parseInt(retentionDays, 10);
  const validRetentionDays = Number.isInteger(normalizedRetentionDays)
    && normalizedRetentionDays >= 30
    && normalizedRetentionDays <= 3650;
  const previewMatchesPolicy = Boolean(
    preview
    && policy
    && preview.retention_days === normalizedRetentionDays
    && preview.policy_version === policy.version,
  );

  const previewRetention = async () => {
    if (!validRetentionDays || previewing) return;
    setPreviewing(true);
    try {
      setPreview(await api.previewCandidateDataRetention(normalizedRetentionDays));
    } catch (previewError) {
      notify("error", humanizeError(previewError));
    } finally {
      setPreviewing(false);
    }
  };

  const saveRetentionPolicy = async () => {
    if (savingPolicy) return;
    if (retentionMode === "automatic" && (!validRetentionDays || !previewMatchesPolicy || !preview)) {
      notify("error", "请先预览当前天数对应的清理范围，再启用自动清理。");
      return;
    }
    setSavingPolicy(true);
    try {
      const saved = await api.updateCandidateDataRetentionPolicy(
        retentionMode === "automatic"
          ? {
            mode: "automatic",
            retention_days: normalizedRetentionDays,
            preview_token: preview!.preview_token,
          }
          : { mode: "manual" },
      );
      setPolicy(saved);
      setRetentionMode(saved.mode);
      setRetentionDays(saved.retention_days ? String(saved.retention_days) : "365");
      setPreview(null);
      notify("success", saved.mode === "automatic" ? "已启用候选人资料自动保留策略。" : "已改为手动保留，系统不会按期限自动删除候选人资料。");
      await load(false);
    } catch (saveError) {
      notify("error", humanizeError(saveError));
    } finally {
      setSavingPolicy(false);
    }
  };

  const runRetentionCleanup = async () => {
    if (cleaning) return;
    setCleaning(true);
    try {
      const run = await api.runCandidateDataRetentionCleanup();
      setRuns((current) => [run, ...current.filter((item) => item.run_id !== run.run_id)]);
      notify(
        "success",
        run.queued_count
          ? `已将 ${run.queued_count} 位到期候选人加入可恢复删除流程。`
          : "本次没有符合条件的候选人需要进入删除流程。",
      );
      await load(false);
    } catch (cleanupError) {
      notify("error", humanizeError(cleanupError));
    } finally {
      setCleaning(false);
    }
  };

  const restoreDeletion = async (deletion: CandidateDataDeletionBatch) => {
    if (restoringBatchId) return;
    setRestoringBatchId(deletion.deletion_batch_id);
    try {
      const restored = await api.restoreCandidateDataDeletion(deletion.deletion_batch_id);
      notify(
        "success",
        `已恢复 ${restored.restored_candidate_count} 位候选人和 ${restored.restored_resume_count} 份简历。`,
      );
      await load(false);
    } catch (restoreError) {
      notify("error", humanizeError(restoreError));
    } finally {
      setRestoringBatchId(null);
    }
  };

  const cancelExport = async (item: CandidateDataExport) => {
    if (cancellingExportId) return;
    setCancellingExportId(item.export_id);
    try {
      const updated = await api.cancelCandidateDataExport(item.export_id);
      setExports((current) => current.map((entry) => entry.export_id === updated.export_id ? updated : entry));
      notify("success", "导出任务已取消，已撤销其下载权限。");
      await load(false);
    } catch (cancelError) {
      notify("error", humanizeError(cancelError));
    } finally {
      setCancellingExportId(null);
    }
  };

  const downloadExport = async (item: CandidateDataExport) => {
    if (downloadingExportId || item.status !== "completed") return;
    setDownloadingExportId(item.export_id);
    try {
      const access = await api.requestCandidateDataExportDownload(item.export_id);
      const blob = await api.getAuthorizedFileBlob(access.access_url);
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `candidate-data-export-${item.export_id.slice(0, 8)}.zip`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 0);
      notify("success", "已开始下载导出文件，系统已记录本次访问。");
      await load(false);
    } catch (downloadError) {
      notify("error", humanizeError(downloadError));
    } finally {
      setDownloadingExportId(null);
    }
  };

  if (loading && !policy) {
    return <div className={pageClassName}><TableSkeleton /></div>;
  }

  return (
    <div className={pageClassName}>
      <header className="page-heading">
        <div>
          {embedded ? <h2>候选人数据与保留</h2> : <h1>数据保留与恢复</h1>}
          <p>在工作区内管理候选人资料的保留期限、可恢复删除、导出文件和原件访问记录。所有清理操作先进入恢复期，不会直接做出招聘结论。</p>
        </div>
        <div className="candidate-data-page-actions">
          <button className="button" disabled={refreshing} onClick={() => void load(false)} type="button">
            {refreshing ? <><i className="spinner" />正在刷新</> : <><Icon name="refresh" size={16} />刷新记录</>}
          </button>
          <button className="button button-ghost" onClick={onOpenLibrary} type="button">返回简历库</button>
        </div>
      </header>

      {error && <p className="library-error" role="status">{error}</p>}

      <div className="candidate-data-layout">
        <div className="candidate-data-main-column">
          <section className="panel candidate-data-retention-panel">
            <div className="panel-heading">
              <div>
                <h2>候选人资料保留策略</h2>
                <p>自动清理只处理到期且未被保留标记的候选人，先进入可恢复删除流程。</p>
              </div>
              <span className={`status-pill${retentionMode === "automatic" ? " is-warning" : ""}`}>{retentionMode === "automatic" ? "自动保留" : "手动保留"}</span>
            </div>
            <fieldset className="candidate-data-retention-form" disabled={savingPolicy || previewing}>
              <div className="candidate-data-retention-options" role="radiogroup" aria-label="候选人资料保留方式">
                <label className="choice-row candidate-data-retention-option">
                  <input checked={retentionMode === "manual"} name="candidate-data-retention-mode" onChange={() => { setRetentionMode("manual"); setPreview(null); }} type="radio" />
                  <span><strong>手动保留</strong><small>不会按期限自动删除候选人资料。</small></span>
                </label>
                <label className="choice-row candidate-data-retention-option">
                  <input checked={retentionMode === "automatic"} name="candidate-data-retention-mode" onChange={() => setRetentionMode("automatic")} type="radio" />
                  <span><strong>自动保留</strong><small>到期候选人进入可恢复删除流程，恢复期结束后才清理。</small></span>
                </label>
              </div>
              {retentionMode === "automatic" && (
                <label className="field-stack candidate-data-retention-days" htmlFor="candidate-data-retention-days">
                  <span className="field-label">资料保留天数</span>
                  <input className="field" id="candidate-data-retention-days" inputMode="numeric" max="3650" min="30" onChange={(event) => { setRetentionDays(event.target.value); setPreview(null); }} type="number" value={retentionDays} />
                  <span className="field-help">可设置 30 至 3650 天。保存前必须先预览本次影响范围。</span>
                </label>
              )}
            </fieldset>
            {retentionMode === "automatic" && preview && (
              <section className="candidate-data-retention-preview" aria-live="polite">
                <div>
                  <strong>{previewMatchesPolicy ? "当前预览可用于保存" : "预览已过期，请重新计算"}</strong>
                  <p>计算于 {formatLibraryDate(preview.calculated_at)}，不会删除任何数据。</p>
                </div>
                <div className="candidate-data-retention-preview-stats">
                  <span><strong>{preview.eligible_candidate_count}</strong> 位候选人可能到期</span>
                  <span><strong>{preview.eligible_resume_count}</strong> 份简历关联</span>
                  <span><strong>{preview.held_candidate_count}</strong> 位被保留标记跳过</span>
                </div>
              </section>
            )}
            <div className="review-actions candidate-data-retention-actions">
              {retentionMode === "automatic" && (
                <button className="button" disabled={!validRetentionDays || previewing} onClick={() => void previewRetention()} type="button">
                  {previewing ? <><i className="spinner" />正在预览</> : <><Icon name="search" size={16} />预览清理范围</>}
                </button>
              )}
              <button className="button button-primary" disabled={savingPolicy || (retentionMode === "automatic" && !previewMatchesPolicy)} onClick={() => void saveRetentionPolicy()} type="button">
                {savingPolicy ? <><i className="spinner" />正在保存</> : <><Icon name="check" size={16} />保存保留策略</>}
              </button>
              <button className="button button-danger-ghost" disabled={cleaning || policy?.mode !== "automatic"} onClick={() => void runRetentionCleanup()} type="button">
                {cleaning ? <><i className="spinner" />正在执行</> : "立即执行到期清理"}
              </button>
            </div>
          </section>

          <section className="panel candidate-data-recovery-panel">
            <div className="panel-heading">
              <div>
                <h2>可恢复删除</h2>
                <p>此处仅显示删除批次与数量，不重新展示已删除候选人的姓名或原始文件名。</p>
              </div>
              <span className="status-pill">{deletions.length} 条记录</span>
            </div>
            {deletions.length ? (
              <div className="table-scroll">
                <table className="candidate-table candidate-data-table">
                  <thead><tr><th scope="col">范围</th><th scope="col">原因</th><th scope="col">影响</th><th scope="col">恢复截止</th><th scope="col">状态</th><th scope="col" aria-label="恢复操作" /></tr></thead>
                  <tbody>
                    {deletions.map((item) => (
                      <tr key={item.deletion_batch_id}>
                        <td>{item.trigger_type === "manual_resume" ? "单份简历" : "候选人资料"}</td>
                        <td>{candidateDataDeletionReasonLabel(item.reason)}</td>
                        <td>{item.affected_candidate_count} 位候选人 · {item.affected_resume_count} 份简历</td>
                        <td>{formatLibraryDate(item.recovery_deadline_at)}</td>
                        <td><span className={`status-pill${item.restorable ? " is-warning" : ""}`}>{item.restorable ? "可恢复" : item.status === "restored" ? "已恢复" : "已进入清理"}</span></td>
                        <td>
                          {item.restorable && (
                            <button className="button button-ghost candidate-data-inline-action" disabled={restoringBatchId === item.deletion_batch_id} onClick={() => void restoreDeletion(item)} type="button">
                              {restoringBatchId === item.deletion_batch_id ? <><i className="spinner" />正在恢复</> : "恢复"}
                            </button>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : <CandidateDataEmptyState title="没有可恢复删除记录" description="从候选人抽屉删除资料后，恢复期限内的批次会显示在这里。" />}
          </section>

          <section className="panel candidate-data-export-panel">
            <div className="panel-heading">
              <div>
                <h2>资料导出</h2>
                <p>导出文件仅在到期前可下载。候选人资料被删除后，相关导出会立即撤销。</p>
              </div>
              <span className="status-pill">{exports.length} 项任务</span>
            </div>
            {exports.length ? (
              <div className="table-scroll">
                <table className="candidate-table candidate-data-table">
                  <thead><tr><th scope="col">内容</th><th scope="col">创建时间</th><th scope="col">有效期</th><th scope="col">状态</th><th scope="col" aria-label="导出操作" /></tr></thead>
                  <tbody>
                    {exports.map((item) => (
                      <tr key={item.export_id}>
                        <td>{item.item_count} 位候选人{item.include_originals ? " · 含原始文件" : " · 不含原始文件"}</td>
                        <td>{formatLibraryDate(item.requested_at)}</td>
                        <td>{item.expires_at ? formatLibraryDate(item.expires_at) : "—"}</td>
                        <td><span className={`status-pill ${candidateDataExportStatusClass(item.status)}`}>{candidateDataExportStatusLabel(item.status)}</span>{item.error_code && <small className="candidate-data-error-code">{item.error_code}</small>}</td>
                        <td className="candidate-data-export-actions">
                          {item.status === "completed" && <button className="button button-ghost candidate-data-inline-action" disabled={downloadingExportId === item.export_id} onClick={() => void downloadExport(item)} type="button">{downloadingExportId === item.export_id ? <><i className="spinner" />正在准备</> : <><Icon name="download" size={15} />下载</>}</button>}
                          {["queued", "running", "retryable_failed"].includes(item.status) && <button className="button button-ghost candidate-data-inline-action" disabled={cancellingExportId === item.export_id} onClick={() => void cancelExport(item)} type="button">{cancellingExportId === item.export_id ? <><i className="spinner" />正在取消</> : "取消"}</button>}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : <CandidateDataEmptyState title="还没有资料导出" description="在候选人抽屉中创建导出后，可在这里查看进度并下载。" />}
          </section>
        </div>

        <aside className="candidate-data-side-column">
          <section className="panel candidate-data-audit-panel">
            <div className="panel-heading">
              <div>
                <h2>访问与操作审计</h2>
                <p>查看、下载原件与导出文件均会记录在此。</p>
              </div>
            </div>
            {auditEvents.length ? (
              <ol className="candidate-data-audit-list">
                {auditEvents.map((event) => (
                  <li key={event.event_id}>
                    <strong>{candidateDataAuditActionLabel(event)}</strong>
                    <span>{formatLibraryDate(event.created_at)}</span>
                    {event.reason_code && <small>{candidateDataDeletionReasonLabel(event.reason_code as CandidateDataDeletionReason)}</small>}
                  </li>
                ))}
              </ol>
            ) : <CandidateDataEmptyState title="暂无审计记录" description="后续的原件访问、导出和删除操作会显示在这里。" />}
          </section>

          <section className="panel candidate-data-cleanup-history">
            <div className="panel-heading">
              <div><h2>到期清理记录</h2><p>系统只将符合策略的数据加入可恢复删除流程。</p></div>
            </div>
            {runs.length ? (
              <ol className="candidate-data-cleanup-list">
                {runs.map((run) => (
                  <li key={run.run_id}>
                    <div><strong>{candidateDataRetentionRunStatusLabel(run.status)}</strong><span>{formatLibraryDate(run.finished_at ?? run.started_at)}</span></div>
                    <small>扫描 {run.scanned_count}，加入删除 {run.queued_count}{run.skipped_hold_count ? `，保留跳过 ${run.skipped_hold_count}` : ""}{run.failed_count ? `，异常 ${run.failed_count}` : ""}</small>
                  </li>
                ))}
              </ol>
            ) : <CandidateDataEmptyState title="尚未执行到期清理" description="启用自动保留后，可先预览再手动执行首次清理。" />}
          </section>
        </aside>
      </div>
    </div>
  );
}

function CandidateDataEmptyState({
  title,
  description,
}: {
  title: string;
  description: string;
}) {
  return (
    <div className="candidate-data-empty">
      <strong>{title}</strong>
      <span>{description}</span>
    </div>
  );
}

function WorkspaceSettingsPage({
  activeSection,
  canManageCandidateData,
  canManageMailbox,
  notify,
  onImported,
  onOpenLibrary,
  onSelectSection,
  role,
}: {
  activeSection: SettingsSection;
  canManageCandidateData: boolean;
  canManageMailbox: boolean;
  notify: (kind: ToastKind, message: string) => void;
  onImported: () => void;
  onOpenLibrary: () => void;
  onSelectSection: (section: SettingsSection) => void;
  role: "admin" | "recruiter" | null;
}) {
  const sections: Array<{
    id: SettingsSection;
    label: string;
    description: string;
    icon: IconName;
  }> = [];

  if (canManageMailbox) {
    sections.push({
      id: "mailbox",
      label: "收件邮箱",
      description: "管理收件通道、同步和附件入库保留。",
      icon: "inbox",
    });
  }
  if (canManageCandidateData) {
    sections.push({
      id: "data",
      label: "候选人数据与保留",
      description: "管理资料保留、导出、删除和访问记录。",
      icon: "gear",
    });
  }

  const currentSection = sections.some((section) => section.id === activeSection)
    ? activeSection
    : sections[0]?.id;
  if (!currentSection) return null;

  return (
    <div className="page-frame settings-page">
      <header className="page-heading">
        <div>
          <h1>设置</h1>
          <p>管理当前工作区的收件通道，以及候选人资料的保留和访问规则。</p>
        </div>
      </header>
      <div className="settings-layout">
        <nav aria-label="设置分类" className="panel settings-navigation">
          <p className="settings-navigation-label">工作区设置</p>
          <div aria-orientation="vertical" className="settings-navigation-list" role="tablist">
            {sections.map((section) => {
              const selected = section.id === currentSection;
              return (
                <button
                  aria-controls={`settings-panel-${section.id}`}
                  aria-label={section.label}
                  aria-selected={selected}
                  className={`settings-navigation-item${selected ? " is-active" : ""}`}
                  id={`settings-tab-${section.id}`}
                  key={section.id}
                  onClick={() => onSelectSection(section.id)}
                  role="tab"
                  type="button"
                >
                  <span className="settings-navigation-icon"><Icon name={section.icon} size={17} /></span>
                  <span>
                    <strong>{section.label}</strong>
                    <small>{section.description}</small>
                  </span>
                </button>
              );
            })}
          </div>
        </nav>
        <section
          aria-labelledby={`settings-tab-${currentSection}`}
          className="settings-content"
          id={`settings-panel-${currentSection}`}
          role="tabpanel"
          tabIndex={-1}
        >
          {currentSection === "mailbox" ? (
            <MailboxPage
              embedded
              humanizeError={humanizeError}
              notify={notify}
              onImported={onImported}
              role={role}
            />
          ) : (
            <CandidateDataLifecyclePage
              embedded
              notify={notify}
              onOpenLibrary={onOpenLibrary}
            />
          )}
        </section>
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
  pdfDownloadLoading,
  pdfError,
  summaries,
  summaryLoading,
  scores,
  scoreLoading,
  scoreError,
  onGenerateSummary,
  onCreateManualSummary,
  onReparseSource,
  reparsingSource,
  onEnrichFacts,
  enrichingFacts,
  canManageCandidateData,
  onPreviewOriginal,
  onDownloadOriginal,
  onRefreshScores,
  onDeleteResume,
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
  pdfDownloadLoading: boolean;
  pdfError: string | null;
  summaries: ResumeSummary[];
  summaryLoading: boolean;
  scores: ResumeScore[];
  scoreLoading: boolean;
  scoreError: string | null;
  onGenerateSummary: () => void;
  onCreateManualSummary: (
    summaryId: string,
    content: Record<string, string>,
  ) => Promise<void>;
  onReparseSource: () => void;
  reparsingSource: boolean;
  onEnrichFacts: () => void;
  enrichingFacts: boolean;
  canManageCandidateData: boolean;
  onPreviewOriginal: () => void;
  onDownloadOriginal: () => void;
  onRefreshScores: () => void;
  onDeleteResume: () => Promise<void>;
}) {
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const [deleting, setDeleting] = useState(false);
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

  const deleteResume = async () => {
    if (
      !window.confirm(
        "删除当前简历？它会立即从工作台移除，并在恢复期内可恢复。",
      )
    ) {
      return;
    }
    setDeleting(true);
    try {
      await onDeleteResume();
    } catch {
      // The parent reports a user-facing error toast.
    } finally {
      setDeleting(false);
    }
  };

  return (
    <aside
      aria-hidden={!isOpen}
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
          {canManageCandidateData && candidate && (
            <button
              aria-busy={deleting}
              className="button button-danger-ghost resume-delete-button"
              disabled={deleting}
              onClick={() => void deleteResume()}
              type="button"
            >
              {deleting ? <><i className="spinner" />正在删除</> : "删除简历"}
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
              ["score", "评分详情"],
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
              downloadLoading={pdfDownloadLoading}
              onDownload={onDownloadOriginal}
              onPreview={onPreviewOriginal}
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
          ) : drawerTab === "score" ? (
            sourceTextIssue ? (
              <ScoreDetailsUnavailable
                busy={reparsingSource}
                onOpenEvidence={() => onTabChange("evidence")}
                onReparse={onReparseSource}
                reason="这份简历的提取文本疑似乱码。为避免误导，本版本的评分结论不会在这里展示。"
              />
            ) : supersededReparse ? (
              <ScoreDetailsUnavailable
                onOpenEvidence={() => onTabChange("evidence")}
                reason="候选人已有更新版本。为避免旧解析结果影响判断，请从当前版本查看评分。"
              />
            ) : (
              <CandidateScoreDetails
                error={scoreError}
                loading={scoreLoading}
                onRefresh={onRefreshScores}
                scores={scores}
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

function scoreEvidenceCoverage(score: ResumeScore): number | null {
  const weightedDimensions = score.dimension_scores.filter(
    (dimension) => dimension.weight > 0,
  );
  const totalWeight = weightedDimensions.reduce(
    (total, dimension) => total + dimension.weight,
    0,
  );
  if (!totalWeight) return null;
  const groundedWeight = weightedDimensions
    .filter((dimension) => dimension.evidence_state === "grounded")
    .reduce((total, dimension) => total + dimension.weight, 0);
  return Math.round((groundedWeight / totalWeight) * 100);
}

function scoreRecordLabel(score: ResumeScore): string {
  if (score.status === "overridden") return "含人工调整";
  if (score.status === "needs_review") return "建议复核";
  return "AI 评分";
}

function CandidateScoreDetails({
  scores,
  loading,
  error,
  onRefresh,
}: {
  scores: ResumeScore[];
  loading: boolean;
  error: string | null;
  onRefresh: () => void;
}) {
  const score = scores.find(
    (item) => item.is_current_facts_version && item.is_current_template_version,
  ) ?? scores.find((item) => item.is_current_facts_version) ?? scores[0] ?? null;
  const evidenceCoverage = score ? scoreEvidenceCoverage(score) : null;

  if (loading && !scores.length) {
    return <div className="drawer-score-details"><TableSkeleton /></div>;
  }
  if (error) {
    return (
      <div className="empty-state drawer-score-empty">
        <div className="empty-state-inner">
          <span className="empty-glyph"><Icon name="refresh" size={23} /></span>
          <h2>暂时无法读取评分详情</h2>
          <p>{error}</p>
          <button className="button" onClick={onRefresh} type="button">
            <Icon name="refresh" size={16} />重新加载
          </button>
        </div>
      </div>
    );
  }
  if (!score) {
    return (
      <div className="empty-state drawer-score-empty">
        <div className="empty-state-inner">
          <span className="empty-glyph"><Icon name="layers" size={23} /></span>
          <h2>尚未生成评分</h2>
          <p>请先在评分模板中批量生成通用评分。生成后，这里会展示每项得分、理由和简历依据。</p>
        </div>
      </div>
    );
  }

  return (
    <section aria-label="综合评分详情" className="drawer-score-details">
      <header className="drawer-score-heading">
        <div>
          <h3>评分详情</h3>
          <p>
            {score.template_name ?? "评分模板"} · 模板 v{score.template_version} · {formatLibraryDate(score.created_at)}
          </p>
        </div>
        <span className={`score-record-status is-${score.status}`}>
          {scoreRecordLabel(score)}
        </span>
      </header>

      <div className="drawer-score-overview">
        <div className="drawer-score-total">
          <span>综合评分</span>
          <strong>{score.total_score.toFixed(1)}<small>/ 100</small></strong>
        </div>
        <dl className="drawer-score-metrics">
          <div>
            <dt>计算方式</dt>
            <dd>最终分 ÷ 100 × 权重</dd>
          </div>
          <div>
            <dt>事实覆盖</dt>
            <dd>{evidenceCoverage === null ? "待核实" : `${evidenceCoverage}%`}</dd>
          </div>
          <div>
            <dt>当前版本</dt>
            <dd>{score.is_current_facts_version ? "是" : "否，请重新评分"}</dd>
          </div>
        </dl>
      </div>
      <p className="drawer-score-formula">
        总分 = Σ（每项最终分 ÷ 100 × 权重）。事实覆盖只表示简历中可验证依据的覆盖程度，不代表候选人能力高低。
      </p>

      <div className="drawer-score-dimensions">
        {score.dimension_scores.map((dimension) => {
          const manuallyAdjusted =
            dimension.manual_reason !== null ||
            dimension.final_raw_score !== dimension.ai_raw_score;
          return (
            <article className="drawer-score-dimension" key={dimension.key}>
              <div className="drawer-score-dimension-heading">
                <div>
                  <h4>{dimension.label}</h4>
                  <span className="drawer-score-dimension-score">
                    {dimension.final_raw_score.toFixed(0)} / 100
                  </span>
                </div>
                <div className="drawer-score-contribution">
                  <span>对总分贡献</span>
                  <strong>{dimension.final_weighted_score.toFixed(1)} 分</strong>
                </div>
              </div>
              <div className="drawer-score-dimension-meta">
                <span>AI 原始分 {dimension.ai_raw_score.toFixed(0)} / 100</span>
                <span>权重 {dimension.weight}%</span>
                <span className={`score-evidence-state is-${dimension.evidence_state}`}>
                  {dimension.evidence_state === "grounded" ? "已提供简历依据" : "证据不足"}
                </span>
                {manuallyAdjusted && <span className="score-manual-mark">已人工调整</span>}
              </div>

              <div className="drawer-score-section">
                <span>AI 评分理由</span>
                <p>{dimension.rationale || "信息不足，未提供可验证判断依据。"}</p>
              </div>
              <div className="drawer-score-section">
                <span>简历事实依据</span>
                {dimension.fact_evidence.length ? (
                  <ul className="drawer-score-facts">
                    {dimension.fact_evidence.map((fact) => (
                      <li key={fact.fact_id}>{fact.summary}</li>
                    ))}
                  </ul>
                ) : (
                  <p>当前维度没有足够的已验证简历依据。</p>
                )}
              </div>
              {dimension.uncertainties.length > 0 && (
                <div className="drawer-score-section is-uncertain">
                  <span>待确认项</span>
                  <ul className="drawer-score-facts">
                    {dimension.uncertainties.map((item) => <li key={item}>{item}</li>)}
                  </ul>
                </div>
              )}
              {dimension.manual_reason && (
                <div className="drawer-score-section is-manual">
                  <span>人工调整原因</span>
                  <p>{dimension.manual_reason}</p>
                </div>
              )}
            </article>
          );
        })}
      </div>

      {score.analysis.overall_summary && (
        <section className="drawer-score-analysis">
          <h4>AI 综合判断</h4>
          <p>{score.analysis.overall_summary}</p>
        </section>
      )}
      {score.analysis.risk_flags.length > 0 && (
        <section className="drawer-score-risks">
          <h4>待关注项</h4>
          <ul>
            {score.analysis.risk_flags.map((risk, index) => (
              <li key={`${risk.message}-${index}`}>
                <span>{risk.message}</span>
                {risk.fact_evidence.length > 0 && (
                  <small>依据：{risk.fact_evidence.map((fact) => fact.summary).join("；")}</small>
                )}
              </li>
            ))}
          </ul>
        </section>
      )}
    </section>
  );
}

function ScoreDetailsUnavailable({
  reason,
  busy = false,
  onOpenEvidence,
  onReparse,
}: {
  reason: string;
  busy?: boolean;
  onOpenEvidence: () => void;
  onReparse?: () => void;
}) {
  return (
    <div className="empty-state source-quality-blocked-summary">
      <div className="empty-state-inner">
        <span className="empty-glyph"><Icon name="layers" size={23} /></span>
        <h2>评分详情暂不可用</h2>
        <p>{reason}</p>
        <div className="source-quality-summary-actions">
          {onReparse && (
            <button
              className="button button-primary"
              disabled={busy}
              onClick={onReparse}
              type="button"
            >
              {busy ? <><i className="spinner" />正在创建</> : <><Icon name="refresh" size={16} />重新解析为新版本</>}
            </button>
          )}
          <button className="button button-ghost" onClick={onOpenEvidence} type="button">
            查看提取依据
          </button>
        </div>
      </div>
    </div>
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

const candidateDataDeletionReasonOptions: Array<{
  value: CandidateDataDeletionReason;
  label: string;
}> = [
  { value: "candidate_request", label: "候选人提出删除" },
  { value: "recruitment_closed", label: "招聘流程结束" },
  { value: "duplicate", label: "重复资料" },
  { value: "other", label: "其他原因" },
];

function OriginalDocumentTab({
  review,
  pdfUrl,
  loading,
  downloadLoading,
  error,
  onPreview,
  onDownload,
}: {
  review: ResumeReviewDetail | null;
  pdfUrl: string | null;
  loading: boolean;
  downloadLoading: boolean;
  error: string | null;
  onPreview: () => void;
  onDownload: () => void;
}) {
  const filename = review?.original_filename ?? "";
  const canPreview = canPreviewInline(filename);
  const isImage = [".png", ".jpg", ".jpeg"].includes(
    resumeFileExtension(filename),
  );
  return (
    <div className="pdf-viewer">
      <section className="original-file-access" aria-label="原文件访问">
        <div>
          <strong>原文件访问</strong>
          <p>打开此标签时会自动加载一次预览，并写入工作区访问审计。</p>
        </div>
        <div className="original-file-access-actions">
          {canPreview && (
            <button
              className="button button-primary"
              disabled={loading || !review}
              onClick={onPreview}
              type="button"
            >
              {loading ? (
                <><i className="spinner" />正在加载</>
              ) : (
                <><Icon name="document" size={16} />重新加载预览</>
              )}
            </button>
          )}
          <button
            className="button"
            disabled={downloadLoading || !review}
            onClick={onDownload}
            type="button"
          >
            {downloadLoading ? (
              <><i className="spinner" />正在准备</>
            ) : (
              <><Icon name="download" size={16} />下载原文件</>
            )}
          </button>
        </div>
      </section>
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
        ) : !canPreview && review ? (
          <div className="empty-state">
            <div className="empty-state-inner">
              <span className="empty-glyph"><Icon name="document" size={23} /></span>
              <h2>{resumeFileTypeLabel(filename)} 原件仅支持下载</h2>
              <p>浏览器不能安全预览此格式，请使用上方“下载原文件”查看。</p>
            </div>
          </div>
        ) : (
          <div className="empty-state original-file-idle">
            <div className="empty-state-inner">
              <span className="empty-glyph"><Icon name="document" size={23} /></span>
              <h2>正在准备原文件预览</h2>
              <p>本次预览会自动加载；如加载失败，可使用上方“重新加载预览”。关闭或切换简历后，本地预览会自动释放。</p>
            </div>
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
                  {item.institution_classification
                    ? ` · ${institutionClassificationLabel(item.institution_classification)}`
                    : ""}
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
              <span>
                {review.employment_months > 0
                  ? `正式工作 ${formatDuration(review.employment_months)}；`
                  : "正式工作年限待核实；"}
                工作 + 实习 {formatDuration(review.employment_or_internship_months)}。
              </span>
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
  notify,
  onScoreCreated,
  onTemplateCreated,
}: {
  notify: (kind: ToastKind, message: string) => void;
  onScoreCreated: () => void;
  onTemplateCreated: (template: ScoreTemplate) => void;
}) {
  const [templates, setTemplates] = useState<ScoreTemplate[]>([]);
  const [templateId, setTemplateId] = useState("");
  const [selectedPresetId, setSelectedPresetId] = useState<string | null>(
    defaultScoreTemplatePreset.id,
  );
  const [templateName, setTemplateName] = useState(defaultScoreTemplatePreset.name);
  const [templateDescription, setTemplateDescription] = useState(
    defaultScoreTemplatePreset.description,
  );
  const [dimensions, setDimensions] = useState<TemplateDraftDimension[]>(() =>
    createTemplateDraftDimensions(defaultScoreTemplatePreset),
  );
  const [loadingTemplates, setLoadingTemplates] = useState(false);
  const [savingTemplate, setSavingTemplate] = useState(false);
  const [startingScoreBatch, setStartingScoreBatch] = useState(false);
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

  const totalWeight = dimensions.reduce(
    (total, item) => total + Number(item.weight || 0),
    0,
  );
  const updateDimension = (
    id: string,
    patch: Partial<TemplateDraftDimension>,
  ) => {
    setSelectedPresetId(null);
    setDimensions((current) =>
      current.map((item) => (item.id === id ? { ...item, ...patch } : item)),
    );
  };
  const applyPreset = (preset: ScoreTemplatePreset) => {
    setSelectedPresetId(preset.id);
    setTemplateName(preset.name);
    setTemplateDescription(preset.description);
    setDimensions(createTemplateDraftDimensions(preset));
  };
  const saveTemplate = async () => {
    if (!templateName.trim()) {
      notify("error", "请填写评分模板名称。");
      return;
    }
    if (totalWeight !== 100) {
      notify("error", `评分权重当前为 ${totalWeight}，必须恰好为 100。`);
      return;
    }
    if (dimensions.some((item) => !item.label.trim())) {
      notify("error", "请为每个评分维度填写名称。");
      return;
    }
    const normalizedLabels = dimensions.map((item) =>
      item.label.trim().replace(/\s+/g, " ").toLowerCase(),
    );
    if (new Set(normalizedLabels).size !== normalizedLabels.length) {
      notify("error", "同一评分规则内不能使用重复的维度名称。");
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
      onTemplateCreated(created);
      notify("success", `评分模板“${created.name}”已创建。`);
    } catch (error) {
      notify("error", humanizeError(error));
    } finally {
      setSavingTemplate(false);
    }
  };
  const runAllScores = async () => {
    if (!templateId) {
      notify("error", "请先选择或创建一套评分模板。");
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
  }, [onScoreCreated, scoreBatch?.batch_id, scoreBatch?.status]);
  const scoreBatchIsRunning =
    scoreBatch?.status === "queued" || scoreBatch?.status === "running";

  return (
    <div className="page-frame">
      <header className="page-heading">
        <div>
          <h1>通用评分模板</h1>
          <p>在这里维护维度和权重；生成通用评分时会面向简历库批量执行，不会暗中选择某位候选人。</p>
        </div>
      </header>
      <div className="page-layout">
        <div>
          <section className="panel">
            <div className="panel-heading">
              <div>
                <h2>新建评分模板</h2>
                <p>权重总和必须为 100。模板用于简历库的通用评分；按 JD 的岗位评估请在招聘详情中发起。</p>
              </div>
              <button
                className="button"
                disabled={loadingTemplates}
                onClick={() => void loadTemplates()}
                type="button"
              >
                <Icon name="refresh" size={15} />
                刷新模板
              </button>
            </div>
            <section aria-labelledby="score-preset-heading" className="score-template-preset-section">
              <div className="score-template-preset-copy">
                <h3 id="score-preset-heading">从预置起点开始</h3>
                <p>选择后仍可修改规则名称、维度、权重和评分说明。</p>
              </div>
              <div className="score-template-presets" role="group" aria-label="评分规则预置起点">
                {scoreTemplatePresets.map((preset) => (
                  <button
                    aria-pressed={selectedPresetId === preset.id}
                    className={`score-template-preset${selectedPresetId === preset.id ? " is-selected" : ""}`}
                    key={preset.id}
                    onClick={() => applyPreset(preset)}
                    type="button"
                  >
                    <strong>{preset.name}</strong>
                    <span>{preset.dimensions.map((dimension) => dimension.label).join(" · ")}</span>
                  </button>
                ))}
              </div>
            </section>
            <div className="form-grid">
              <div className="field-stack span-full">
                <label className="field-label" htmlFor="template-name">
                  规则名称
                </label>
                <input
                  className="field"
                  id="template-name"
                  onChange={(event) => {
                    setSelectedPresetId(null);
                    setTemplateName(event.target.value);
                  }}
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
                  onChange={(event) => {
                    setSelectedPresetId(null);
                    setTemplateDescription(event.target.value);
                  }}
                  placeholder="说明此规则适用的岗位、评价重点或使用边界。"
                  value={templateDescription}
                />
              </div>
            </div>
            <div className="model-list">
              {dimensions.map((dimension) => (
                <div className="score-template-dimension" key={dimension.id}>
                  <div className="score-template-dimension-header">
                    <div className="dimension-field-stack">
                      <label
                        className="field-label"
                        htmlFor={`dimension-label-${dimension.id}`}
                      >
                        评分维度
                      </label>
                      <input
                        className="field"
                        id={`dimension-label-${dimension.id}`}
                        onChange={(event) =>
                          updateDimension(dimension.id, {
                            label: event.target.value,
                          })
                        }
                        placeholder="例如：技能匹配"
                        value={dimension.label}
                      />
                    </div>
                    <div className="dimension-field-stack dimension-weight-field">
                      <label
                        className="field-label"
                        htmlFor={`dimension-weight-${dimension.id}`}
                      >
                        权重（%）
                      </label>
                      <input
                        aria-describedby={`dimension-weight-hint-${dimension.id}`}
                        className="field"
                        id={`dimension-weight-${dimension.id}`}
                        max="100"
                        min="0"
                        onChange={(event) =>
                          updateDimension(dimension.id, {
                            weight: Number(event.target.value),
                          })
                        }
                        step="1"
                        type="number"
                        value={dimension.weight}
                      />
                      <span
                        className="dimension-numeric-hint"
                        id={`dimension-weight-hint-${dimension.id}`}
                      >
                        占总分
                      </span>
                    </div>
                    <button
                      aria-label={`删除 ${dimension.label}`}
                      className="icon-button"
                      disabled={dimensions.length <= 1}
                      onClick={() => {
                        setSelectedPresetId(null);
                        setDimensions((current) =>
                          current.filter((item) => item.id !== dimension.id),
                        );
                      }}
                      type="button"
                    >
                      <Icon name="close" size={16} />
                    </button>
                  </div>
                  <div className="dimension-guidance-stack">
                    <label
                      className="field-label"
                      htmlFor={`dimension-guidance-${dimension.id}`}
                    >
                      AI 评分说明（可选）
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
                </div>
              ))}
            </div>
            <div className="review-actions">
              <button
                className="button button-ghost"
                onClick={() => {
                  setSelectedPresetId(null);
                  setDimensions((current) => [
                    ...current,
                    {
                      id: `score-dimension-${Date.now()}-${++templateDraftDimensionSequence}`,
                      label: "新评分维度",
                      weight: 0,
                      guidance: "",
                    },
                  ]);
                }}
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
                    创建评分模板
                  </>
                )}
              </button>
            </div>
            <div className="weight-total">
              <span>已分配权重</span>
              <strong>{totalWeight} / 100%</strong>
            </div>
          </section>
          <section className="panel">
            <div className="panel-heading">
              <div>
                <h2>批量生成通用评分</h2>
                <p>按所选模板对当前工作区所有符合条件的启用简历评分。岗位 JD 匹配度请在招聘详情中运行。</p>
              </div>
            </div>
            <div className="field-stack">
              <label className="field-label" htmlFor="score-template">
                选择评分模板
              </label>
              <div className="select-wrap">
                <select
                  className="select-field"
                  id="score-template"
                  onChange={(event) => setTemplateId(event.target.value)}
                  value={templateId}
                >
                  <option value="">选择评分模板</option>
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
                    生成全部简历的通用评分
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
        </div>
        <aside className="panel">
          <div className="panel-heading">
            <div>
              <h2>现有模板</h2>
              <p>选择一套模板后，可对简历库批量生成通用评分。</p>
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
              <p className="candidate-meta">还没有可用评分模板。</p>
            )}
          </div>
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
  const scoreDimensionLabels = new Map(
    score.dimension_scores.map((dimension) => [dimension.key, dimension.label]),
  );

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
                    <strong>
                      {entry.dimension_key
                        ? scoreDimensionLabels.get(entry.dimension_key) ?? "评分维度"
                        : "评分维度"}
                    </strong>
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
  canGenerateAiJd,
  notify,
  onOpenMatchedResume,
}: {
  canGenerateAiJd: boolean;
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
    setMatchBatch(null);
    setBatchItems([]);
  };
  const beginNewJob = () => {
    resetJobAuthoring();
    setJobWorkspaceMode("create");
    setJobVersion(null);
    setVersioningJobId(null);
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
    if (!canGenerateAiJd) {
      notify("error", "当前套餐未开通 AI 生成 JD。你仍可直接发布原版 JD。");
      return;
    }
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
        `已将 ${response.total_count} 份简历加入岗位评估队列。`,
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
          <h1>招聘详情</h1>
          <p>
            {canGenerateAiJd
              ? "管理岗位 JD、匹配条件与候选人评估；启用后可直接对该岗位的全部可匹配简历运行评估。"
              : "可直接发布原版 JD；AI 生成 JD 与候选人匹配需要开通相应套餐。"}
          </p>
        </div>
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
                      placeholder={
                        canGenerateAiJd
                          ? "填写岗位需求后点击「AI 生成 JD」；已有完整 JD 可直接粘贴后点击「原版发布」。"
                          : "粘贴完整原版 JD 后点击「原版发布」，内容会按原样保存。"
                      }
                      value={jobBrief}
                    />
                    <p className="candidate-meta">
                      {canGenerateAiJd
                        ? "AI 生成 JD 会提取匹配条件，原版发布会按当前内容原样保存。"
                        : "原版发布不会调用 AI，内容会按当前输入原样保存。"}
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
                  {canGenerateAiJd && (
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
                  )}
                  {(!canGenerateAiJd || !generatedJobIsReady) && (
                    <button
                      className={`button${canGenerateAiJd ? "" : " button-primary"}`}
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
              <h2>岗位评估</h2>
              <p>
                {jobIsOriginal
                  ? "原版发布未生成匹配条件，因此不会调用 AI 匹配。"
                  : "根据当前 JD，对全部可匹配简历生成匹配度、可信度与待核实项。"}
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
              className="button button-primary"
              disabled={!jobCanMatch || loading}
              onClick={() => void runAllMatches()}
              type="button"
            >
              {loading ? (
                <>
                  <i className="spinner" />
                  正在创建评估任务…
                </>
              ) : (
                <>
                  <Icon name="match" size={16} />
                  开始岗位评分（全部可匹配简历）
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
  partial: "硬条件部分满足",
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
          <h2>岗位评估任务</h2>
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
          <h2>候选人评估结果</h2>
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
            <h2>尚未生成岗位评估</h2>
            <p>确认 JD 后，点击“开始岗位评分（全部可匹配简历）”即可在此查看排序结果。</p>
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
