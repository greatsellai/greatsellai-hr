import type {
  CandidateSearchRequest,
  CandidateSearchResponse,
  DegreeLevel,
  EducationFilter,
  FilterOptions,
  InstitutionClassification,
  InstitutionTier,
} from "../../types";
import {
  experienceTypeOptions,
  clampPercentage,
  institutionClassificationLabels,
  institutionClassificationOptions,
  sortInstitutionClassifications,
  type FilterDraft,
} from "./filter-model";

export const emptyCandidateSearch: CandidateSearchResponse = {
  items: [],
  next_cursor: null,
  needs_review_count: 0,
  total_count: 0,
};

const defaultFilterDraft: FilterDraft = {
  conditionMatchMode: "all",
  minEmploymentOrInternshipMonths: 0,
  degrees: [],
  institutionClassifications: [],
  graduationStatus: "any",
  freshGraduateStartMonth: `${new Date().getFullYear()}-01`,
  freshGraduateEndMonth: `${new Date().getFullYear() + 1}-12`,
  schoolName: "",
  major: "",
  minAcademicScorePercent: 0,
  minAverageScore: "",
  minGpaPercent: "",
  maxRankPosition: "",
  maxRankPercent: 0,
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

const MONTH_VALUE_PATTERN = /^\d{4}-(0[1-9]|1[0-2])$/;

export function hasActiveGraduationFilter(
  draft: Pick<
    FilterDraft,
    | "graduationStatus"
    | "freshGraduateStartMonth"
    | "freshGraduateEndMonth"
  >,
): boolean {
  return (
    draft.graduationStatus !== "any" &&
    MONTH_VALUE_PATTERN.test(draft.freshGraduateStartMonth) &&
    MONTH_VALUE_PATTERN.test(draft.freshGraduateEndMonth) &&
    draft.freshGraduateStartMonth <= draft.freshGraduateEndMonth
  );
}

const degreeOptions: Array<{ value: DegreeLevel; label: string }> = [
  { value: "doctor", label: "博士" },
  { value: "master", label: "硕士" },
  { value: "bachelor", label: "本科" },
  { value: "associate", label: "大专" },
  { value: "high_school", label: "高中" },
  { value: "vocational_or_below", label: "中专/职高及以下" },
];

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

export const fallbackFilterOptions: FilterOptions = {
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
    { value: "broad", label: "任一命中" },
    { value: "precise", label: "全部命中" },
  ],
};

export function freshDefaultFilter(): FilterDraft {
  return {
    ...defaultFilterDraft,
    degrees: [],
    institutionClassifications: [],
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
 * than the controls a recruiter may be editing for their next search. Keep a
 * shallow object copy plus copies of every mutable collection so the applied
 * request remains stable while the left-hand form changes.
 */
export function snapshotFilterDraft(draft: FilterDraft): FilterDraft {
  return {
    ...draft,
    degrees: [...draft.degrees],
    institutionClassifications: [...draft.institutionClassifications],
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

export function draftToSearchRequest(
  draft: FilterDraft,
  cursor: string | null = null,
  scoreTemplateId: string | null = null,
): CandidateSearchRequest {
  const request: CandidateSearchRequest = {
    schema_version: "candidate_filter.v2",
    limit: 50,
    cursor,
  };
  if (draft.conditionMatchMode === "any") {
    request.condition_match_mode = "any";
  }
  const institutionClassifications = sortInstitutionClassifications(
    draft.institutionClassifications,
  );

  if (draft.minEmploymentOrInternshipMonths > 0) {
    request.min_employment_or_internship_months =
      draft.minEmploymentOrInternshipMonths;
  }
  if (hasActiveGraduationFilter(draft)) {
    request.graduation_status = draft.graduationStatus;
    request.fresh_graduate_start_month = draft.freshGraduateStartMonth;
    request.fresh_graduate_end_month = draft.freshGraduateEndMonth;
  }
  if (draft.degrees.length) request.highest_degree_in = draft.degrees;
  const educationFilter: EducationFilter = {};
  if (institutionClassifications.length) {
    educationFilter.institution_classifications_any_of = institutionClassifications;
  }
  if (draft.minAcademicScorePercent > 0) {
    educationFilter.min_academic_score_percent = draft.minAcademicScorePercent;
  }
  if (draft.maxRankPercent > 0) {
    educationFilter.max_rank_percent = draft.maxRankPercent;
  }
  if (Object.keys(educationFilter).length) {
    request.education_any_of = [educationFilter];
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
      error:
        "该历史筛选含有已下线的“非 985/211”条件，无法无损迁移。请重新设置院校类型后保存。",
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
        error:
          "该历史筛选同时包含新旧院校条件，无法无损迁移。请重新设置院校类型后保存。",
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
        error:
          "该历史筛选同时包含旧版 985/211 与其他院校条件，无法无损迁移。请重新设置院校类型后保存。",
      };
    }
    return {
      classifications: sortInstitutionClassifications(currentClassifications),
      error: null,
    };
  }

  if (
    request.is_985_211 === true &&
    legacyTiers.some((tier) => tier !== "985" && tier !== "211")
  ) {
    return {
      classifications: [],
      error:
        "该历史筛选同时包含旧版 985/211 与其他院校条件，无法无损迁移。请重新设置院校类型后保存。",
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

export function searchRequestToDraft(
  request: CandidateSearchRequest,
): SavedFilterDraftResult {
  const institutionMigration = savedInstitutionClassifications(request);
  if (institutionMigration.error) {
    return { draft: null, error: institutionMigration.error };
  }
  const savedDegrees =
    request.highest_degree_in ?? request.education_any_of?.[0]?.degree_in ?? [];
  const savedEducation = request.education_any_of?.[0];
  const defaults = freshDefaultFilter();
  const savedKeywords = request.keywords?.length
    ? request.keywords
    : request.keywords_all_of?.length
      ? request.keywords_all_of
      : request.keywords_any_of ?? [];
  const keywordMode = request.keywords?.length
    ? (request.keyword_match_mode ?? "broad")
    : request.keywords_all_of?.length
      ? "precise"
      : "broad";
  const graduationDraft = {
    graduationStatus: request.graduation_status ?? "any",
    freshGraduateStartMonth:
      request.fresh_graduate_start_month ?? defaults.freshGraduateStartMonth,
    freshGraduateEndMonth:
      request.fresh_graduate_end_month ?? defaults.freshGraduateEndMonth,
  } as const;
  return {
    draft: {
      ...defaults,
      conditionMatchMode: request.condition_match_mode ?? "all",
      // Historical saved filters can have a formal-work threshold. The current
      // first-pass UI deliberately uses one combined tenure threshold instead.
      minEmploymentOrInternshipMonths: Math.max(
        request.min_employment_or_internship_months ?? 0,
        request.min_employment_months ?? 0,
      ),
      degrees: savedDegrees.filter((degree) => degree !== "unknown"),
      institutionClassifications: institutionMigration.classifications,
      graduationStatus: hasActiveGraduationFilter(graduationDraft)
        ? graduationDraft.graduationStatus
        : "any",
      freshGraduateStartMonth: graduationDraft.freshGraduateStartMonth,
      freshGraduateEndMonth: graduationDraft.freshGraduateEndMonth,
      minAcademicScorePercent: clampPercentage(
        savedEducation?.min_academic_score_percent ?? 0,
      ),
      maxRankPercent: clampPercentage(savedEducation?.max_rank_percent ?? 0),
      keywords: [...new Set(savedKeywords)],
      keywordsMode: keywordMode,
    },
    error: null,
  };
}
