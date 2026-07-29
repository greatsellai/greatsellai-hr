import type {
  AwardLevel,
  DegreeLevel,
  ExperienceType,
  FilterOptions,
  InstitutionClassification,
  LanguageCredentialCode,
  LeadershipContext,
  PresenceStatus,
  ScholarshipLevel,
} from "../../types";

export type MatchMode = "all" | "any";
export type KeywordMode = "broad" | "precise";

/**
 * The editable state for the candidate filtering workbench.  The search
 * request remains owned by the workspace shell, while this shape is shared by
 * the filter UI, saved-filter migration, and result-column presentation.
 */
export interface FilterDraft {
  minEmploymentOrInternshipMonths: number;
  degrees: DegreeLevel[];
  institutionClassifications: InstitutionClassification[];
  graduationStatus: "any" | "fresh" | "previous";
  freshGraduateStartMonth: string;
  freshGraduateEndMonth: string;
  schoolName: string;
  major: string;
  minAcademicScorePercent: number;
  minAverageScore: string;
  minGpaPercent: string;
  maxRankPosition: string;
  maxRankPercent: number;
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

export const experienceTypeOptions: Array<{
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

export const degreeLabels: Record<DegreeLevel, string> = {
  unknown: "未知",
  vocational_or_below: "中专/职高及以下",
  high_school: "高中",
  associate: "大专",
  bachelor: "本科",
  master: "硕士",
  doctor: "博士",
};

export const institutionClassificationOptions: Array<{
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

export const institutionClassificationLabels: Record<
  InstitutionClassification,
  string
> = Object.fromEntries(
  institutionClassificationOptions.map((option) => [option.value, option.label]),
) as Record<InstitutionClassification, string>;

export function clampMonths(value: number): number {
  return Math.max(0, Math.min(240, Math.round(value / 12) * 12));
}

export function clampPercentage(value: number): number {
  if (!Number.isFinite(value)) return 0;
  return Math.max(0, Math.min(100, Math.round(value)));
}

export function formatDuration(months: number): string {
  if (months <= 0) return "0 个月";
  const years = Math.floor(months / 12);
  const rest = months % 12;
  return rest ? `${years} 年 ${rest} 个月` : `${years} 年`;
}

export function formatMinimumDuration(months: number): string {
  return months <= 0 ? "不限" : formatDuration(months);
}

export function formatMinimumAcademicScore(percent: number): string {
  return percent <= 0 ? "不限" : `不低于 ${percent} 分`;
}

export function formatMaximumRankPercent(percent: number): string {
  if (percent <= 0) return "不限";
  if (percent === 100) return "排名前 100%（仅有排名记录）";
  return `排名前 ${percent}%`;
}

export function resolvedInstitutionClassificationOptions(
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

export function institutionClassificationLabel(
  classification: InstitutionClassification,
): string {
  return institutionClassificationLabels[classification];
}

export function sortInstitutionClassifications(
  classifications: readonly InstitutionClassification[] | null | undefined,
): InstitutionClassification[] {
  const order = new Map(
    institutionClassificationOptions.map((option, index) => [option.value, index]),
  );
  return [...new Set(classifications ?? [])].sort(
    (left, right) =>
      (order.get(left) ?? Number.MAX_SAFE_INTEGER) -
      (order.get(right) ?? Number.MAX_SAFE_INTEGER),
  );
}
