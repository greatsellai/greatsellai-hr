import { BackofficeButton } from "../../backoffice/ui/BackofficeButton";
import { BackofficeSelect } from "../../backoffice/ui/BackofficeSelect";
import { TableSkeleton } from "../../backoffice/ui/TableSkeleton";
import { Icon } from "../../icons";
import {
  degreeLabels,
  experienceTypeOptions,
  formatDuration,
  institutionClassificationLabel,
  institutionClassificationLabels,
  sortInstitutionClassifications,
  type FilterDraft,
} from "./filter-model";
import type {
  CandidateSearchDisplayFieldKey,
  CandidateSearchItem,
  CandidateSearchResponse,
  DegreeLevel,
  InstitutionClassification,
  ScoreTemplate,
} from "../../types";

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

  if (draft.minEmploymentMonths > 0) {
    add("employment_months", "正式工作年限");
  }
  if (draft.experienceTypes.includes("internship")) {
    add("experience_type", "实习经历");
  }

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

function scoreConfidencePresentation(value: number | null): {
  label: string;
  tone: "grounded" | "partial" | "unknown";
} | null {
  if (value === null) {
    return { label: "待核实", tone: "unknown" };
  }
  if (value >= 80) {
    return { label: `可信度 ${value.toFixed(0)}%`, tone: "grounded" };
  }
  if (value >= 50) {
    return { label: `可信度 ${value.toFixed(0)}%`, tone: "partial" };
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
  if (!hasEducation) return <span className="candidate-meta">待核实</span>;
  return (
    <div className="candidate-profile-cell candidate-education-cell">
      <div className="candidate-profile-primary">
        {item.highest_degree && (
          <span className="degree-label">{degreeLabels[item.highest_degree]}</span>
        )}
        <span className="candidate-profile-title">
          {item.education_school || "学校待核实"}
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
  if (!hasVerifiedEmployment && !role) {
    return <span className="candidate-meta">待核实</span>;
  }
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
            : "工作年限待核实"}
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
      ) : null}
    </div>
  );
}

function CandidateSkillHighlights({ item }: { item: CandidateSearchItem }) {
  const skills = item.skill_highlights ?? [];
  if (!skills.length) return <span className="candidate-meta">—</span>;
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

function compactFilterValue(values: readonly string[], limit = 2): string {
  const uniqueValues = [
    ...new Set(values.map((value) => value.trim()).filter(Boolean)),
  ];
  if (uniqueValues.length <= limit) return uniqueValues.join("、");
  return `${uniqueValues.slice(0, limit).join("、")} 等 ${uniqueValues.length} 项`;
}

function appliedFilterLabels(draft: FilterDraft): string[] {
  const labels: string[] = [];
  const add = (label: string, value: string) => {
    const normalizedValue = value.trim();
    if (normalizedValue) labels.push(`${label}：${normalizedValue}`);
  };

  const institutionClassifications = draft.institutionClassifications.filter(
    (classification) => classification === "985" || classification === "211",
  );
  if (institutionClassifications.length) {
    add(
      "院校",
      compactFilterValue(
        sortInstitutionClassifications(institutionClassifications).map(
          institutionClassificationLabel,
        ),
      ),
    );
  }
  if (draft.minEmploymentMonths > 0) {
    add("正式工作", `至少 ${formatDuration(draft.minEmploymentMonths)}`);
  }
  if (draft.experienceTypes.includes("internship")) add("实习", "有实习经历");

  return labels;
}

export function ResultsPane({
  appliedDraft,
  search,
  searching,
  selectedResumeId,
  onOpenCandidate,
  onScoreTemplateChange,
  onLoadMore,
  onReset,
  onRefineWithAgent,
  onUpload,
  scoreTemplateId,
  scoreTemplates,
}: {
  appliedDraft: FilterDraft;
  search: CandidateSearchResponse;
  searching: boolean;
  selectedResumeId: string | null;
  onOpenCandidate: (item: CandidateSearchItem, tab?: "score") => void;
  onScoreTemplateChange: (templateId: string | null) => void;
  onLoadMore: () => void;
  onReset: () => void;
  onRefineWithAgent: () => void;
  onUpload: () => void;
  scoreTemplateId: string | null;
  scoreTemplates: ScoreTemplate[];
}) {
  const displayColumns = activeResultDisplayColumns(appliedDraft);
  const hasAppliedDisplayColumns = displayColumns.length > 0;
  const appliedFilters = appliedFilterLabels(appliedDraft);
  const visibleAppliedFilters = appliedFilters.slice(0, 4);
  const hiddenAppliedFilterCount =
    appliedFilters.length - visibleAppliedFilters.length;

  return (
    <section className="results-pane" aria-label="候选人结果">
      <header className="results-header">
        <div className="results-summary">
          <h1>候选人</h1>
        </div>
        <div className="results-toolbar">
          <BackofficeButton
            ariaLabel={`交给 Agent 精筛当前 ${search.total_count} 位候选人`}
            className="results-agent-refine"
            disabled={searching || search.total_count === 0}
            icon={<Icon name="spark" size={16} />}
            onClick={onRefineWithAgent}
            tone="primary"
          >
            交给 Agent 精筛当前 {search.total_count} 人
          </BackofficeButton>
          <div className="score-sort-control">
            <BackofficeSelect
              ariaLabel="评分口径"
              className="score-sort-select"
              onChange={(templateId) => onScoreTemplateChange(templateId || null)}
              options={[
                { label: "不按评分排序", value: "" },
                ...scoreTemplates.map((template) => ({
                  label: `${template.name} · v${template.version}`,
                  value: template.template_id,
                })),
              ]}
              value={scoreTemplateId ?? ""}
            />
          </div>
          {search.needs_review_count > 0 && (
            <span className="status-pill">
              待处理 {search.needs_review_count}
            </span>
          )}
          <BackofficeButton
            icon={<Icon name="upload" size={16} />}
            onClick={onUpload}
          >
            上传简历
          </BackofficeButton>
        </div>
      </header>
      {appliedFilters.length > 0 && (
        <div className="applied-filter-bar" aria-label="已应用的筛选条件">
          <div className="applied-filter-list">
            {visibleAppliedFilters.map((label) => (
              <span className="applied-filter-chip" key={label} title={label}>
                {label}
              </span>
            ))}
            {hiddenAppliedFilterCount > 0 && (
              <span
                className="applied-filter-chip applied-filter-chip-more"
                title={`另有 ${hiddenAppliedFilterCount} 项已应用条件`}
              >
                +{hiddenAppliedFilterCount}
              </span>
            )}
          </div>
          <BackofficeButton
            ariaLabel="清空筛选条件"
            className="applied-filter-clear"
            icon={<Icon name="close" size={14} />}
            onClick={onReset}
          >
            清空条件
          </BackofficeButton>
        </div>
      )}
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
                    {column.label}
                  </th>
                ))}
                <th scope="col">综合评分</th>
                <th scope="col" aria-label="查看详情" />
              </tr>
            </thead>
            <tbody>
              {search.items.map((item) => {
                const scoreConfidence = scoreConfidencePresentation(
                  item.score_confidence,
                );
                const scoreStatus = scoreStatusLabel(item.score_status);
                return (
                  <tr
                    className={
                      selectedResumeId === item.resume_id ? "is-selected" : ""
                    }
                    key={item.resume_id}
                    onClick={() => onOpenCandidate(item)}
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
                          </button>
                          {scoreConfidence && (
                            <span
                              className={`score-confidence is-${scoreConfidence.tone}`}
                            >
                              {scoreConfidence.label}
                            </span>
                          )}
                          {scoreStatus && (
                            <span className="candidate-score-status">
                              {scoreStatus}
                            </span>
                          )}
                        </div>
                      ) : (
                        <span className="library-empty-copy">尚未评分</span>
                      )}
                    </td>
                    <td className="candidate-open-cell">
                      <button
                        aria-label={`查看 ${item.display_name?.trim() || "未命名候选人"} 的简历详情`}
                        className="candidate-open-action"
                        onClick={(event) => {
                          event.stopPropagation();
                          onOpenCandidate(item);
                        }}
                        type="button"
                      >
                        <Icon name="chevron-right" size={17} />
                      </button>
                    </td>
                  </tr>
                );
              })}
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
