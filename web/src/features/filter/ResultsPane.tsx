import { useEffect, useMemo, useState, type KeyboardEvent } from "react";
import { api } from "../../api";
import { BackofficeButton } from "../../backoffice/ui/BackofficeButton";
import { BackofficeSelect } from "../../backoffice/ui/BackofficeSelect";
import { TableSkeleton } from "../../backoffice/ui/TableSkeleton";
import { Icon } from "../../icons";
import type {
  CandidateSearchDisplayFieldKey,
  CandidateSearchItem,
  CandidateSearchResponse,
  DegreeLevel,
  InstitutionClassification,
  ScoreTemplate,
} from "../../types";
import type { CandidateDrawerTab } from "../candidate-drawer/candidate-drawer-types";
import {
  candidateIsPriority,
  candidateNeedsReview,
  candidatePendingReasons,
  candidateScoreConfidencePresentation,
} from "./candidate-workbench-model";
import {
  degreeLabels,
  formatDuration,
  formatMaximumRankPercent,
  formatMinimumAcademicScore,
  institutionClassificationLabel,
  institutionClassificationLabels,
  sortInstitutionClassifications,
  type FilterDraft,
} from "./filter-model";
import { hasActiveGraduationFilter } from "./filter-search-model";

type CandidateWorkbenchTab = "all" | "priority" | "review" | "favorites";

interface ResultDisplayColumn {
  key: CandidateSearchDisplayFieldKey;
  label: string;
}

function activeResultDisplayColumns(draft: FilterDraft): ResultDisplayColumn[] {
  const columns: ResultDisplayColumn[] = [];
  const add = (key: CandidateSearchDisplayFieldKey, label: string) => {
    if (!columns.some((column) => column.key === key)) columns.push({ key, label });
  };

  if (hasActiveGraduationFilter(draft)) add("graduation", "毕业时间");
  if (draft.minAcademicScorePercent > 0 || draft.maxRankPercent > 0) {
    add("academic_performance", "学业表现");
  }
  if (draft.keywords.length) add("keywords", "关键词命中");
  return columns;
}

function resultDisplayValueLabel(key: CandidateSearchDisplayFieldKey, value: string): string {
  const normalized = value.trim();
  if (!normalized) return "";
  if (key === "institution_classifications") {
    return institutionClassificationLabels[normalized as InstitutionClassification] ?? normalized;
  }
  if (key === "highest_degree" || key === "education_degree") {
    return degreeLabels[normalized as DegreeLevel] ?? normalized;
  }
  if (key === "employment_months" || key === "employment_or_internship_months") {
    const months = Number(normalized);
    return Number.isFinite(months) ? formatDuration(months) : normalized;
  }
  return normalized;
}

function resultDisplayValues(item: CandidateSearchItem, key: CandidateSearchDisplayFieldKey): string[] {
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
  label: string;
}) {
  const values = resultDisplayValues(item, fieldKey);
  if (!values.length) return <span className="candidate-meta result-display-empty">—</span>;
  return (
    <div aria-label={`${label}：${values.join("；")}`} className="result-display-values" title={values.join("；")}>
      {values.slice(0, 2).map((value) => <span className="result-display-value" key={value}>{value}</span>)}
      {values.length > 2 && <span className="result-display-more">+{values.length - 2} 项</span>}
    </div>
  );
}

function CandidateEducationCell({ item }: { item: CandidateSearchItem }) {
  const classifications = sortInstitutionClassifications(item.institution_classifications);
  const hasEducation = Boolean(item.highest_degree || item.education_school || item.education_major || classifications.length);
  if (!hasEducation) return <span className="candidate-meta">待核验</span>;
  return (
    <div className="candidate-profile-cell candidate-education-cell">
      <div className="candidate-profile-primary">
        {item.highest_degree && <span className="degree-label">{degreeLabels[item.highest_degree]}</span>}
        <span className="candidate-profile-title">{item.education_school || "学校待核验"}</span>
      </div>
      {item.education_major && <span className="candidate-meta">{item.education_major}</span>}
      {classifications.length > 0 && (
        <div className="institution-classification-tags">
          {classifications.map((classification) => (
            <span className="tag" key={classification}>{institutionClassificationLabel(classification)}</span>
          ))}
        </div>
      )}
    </div>
  );
}

function CandidateExperienceCell({ item }: { item: CandidateSearchItem }) {
  const role = [item.latest_experience_title, item.latest_experience_organization]
    .filter(Boolean)
    .join(" · ");
  const totalTenureMonths = Math.max(
    item.employment_or_internship_months ?? 0,
    item.employment_months ?? 0,
  );
  if (!totalTenureMonths && !role) return <span className="candidate-meta">待核验</span>;
  return (
    <div className="candidate-profile-cell">
      <span className="candidate-profile-title">
        {totalTenureMonths ? `${formatDuration(totalTenureMonths)} 工作年限` : "工作年限待核验"}
      </span>
      {role && <span className="candidate-meta">{role}</span>}
    </div>
  );
}

function CandidateSkillHighlights({ item }: { item: CandidateSearchItem }) {
  const skills = item.skill_highlights ?? [];
  if (!skills.length) return <span className="candidate-meta">—</span>;
  return (
    <div aria-label={`核心技能：${skills.join("；")}`} className="candidate-skill-highlights" title={skills.join("；")}>
      {skills.slice(0, 3).map((skill) => <span className="tag" key={skill}>{skill}</span>)}
      {skills.length > 3 && <span className="candidate-skills-more">+{skills.length - 3}</span>}
    </div>
  );
}

function compactFilterValue(values: readonly string[], limit = 2): string {
  const uniqueValues = [...new Set(values.map((value) => value.trim()).filter(Boolean))];
  if (uniqueValues.length <= limit) return uniqueValues.join("、");
  return `${uniqueValues.slice(0, limit).join("、")} 等 ${uniqueValues.length} 项`;
}

function appliedFilterLabels(draft: FilterDraft): string[] {
  const labels: string[] = [];
  const add = (label: string, value: string) => {
    const normalized = value.trim();
    if (normalized) labels.push(`${label}：${normalized}`);
  };
  const institutions = sortInstitutionClassifications(draft.institutionClassifications);
  if (institutions.length) add("院校", compactFilterValue(institutions.map(institutionClassificationLabel)));
  if (draft.degrees.length) add("最高学历", compactFilterValue(draft.degrees.map((degree) => degreeLabels[degree])));
  if (draft.minEmploymentOrInternshipMonths > 0) add("工作年限", `至少 ${formatDuration(draft.minEmploymentOrInternshipMonths)}`);
  const academic = [
    draft.minAcademicScorePercent > 0 ? formatMinimumAcademicScore(draft.minAcademicScorePercent) : null,
    draft.maxRankPercent > 0 ? formatMaximumRankPercent(draft.maxRankPercent) : null,
  ].filter((value): value is string => Boolean(value));
  if (academic.length) add("学业表现", academic.join(" · "));
  if (hasActiveGraduationFilter(draft)) {
    const status = draft.graduationStatus === "fresh" ? "应届" : "往届";
    const window = draft.graduationStatus === "fresh"
      ? `${draft.freshGraduateStartMonth} 至 ${draft.freshGraduateEndMonth}`
      : `早于 ${draft.freshGraduateStartMonth}`;
    add("毕业状态", `${status}（${window}）`);
  }
  if (draft.keywords.length) add("关键词", compactFilterValue(draft.keywords, 3));
  return labels;
}

function tabItems(items: CandidateSearchItem[], tab: CandidateWorkbenchTab): CandidateSearchItem[] {
  if (tab === "priority") return items.filter(candidateIsPriority);
  if (tab === "review") return items.filter(candidateNeedsReview);
  if (tab === "favorites") return items.filter((item) => item.is_favorited);
  return items;
}

export function ResultsPane({
  appliedDraft,
  filtersOpen,
  search,
  searching,
  selectedCandidateId,
  onSelectCandidate,
  onOpenCandidate,
  onScoreTemplateChange,
  onLoadMore,
  onReset,
  onRefineWithAgent,
  onUpload,
  onFavoriteChanged,
  onToggleFilters,
  scoreTemplateId,
  scoreTemplates,
}: {
  appliedDraft: FilterDraft;
  filtersOpen: boolean;
  search: CandidateSearchResponse;
  searching: boolean;
  selectedCandidateId: string | null;
  onSelectCandidate: (candidate: CandidateSearchItem | null) => void;
  onOpenCandidate: (item: CandidateSearchItem, tab?: CandidateDrawerTab) => void;
  onScoreTemplateChange: (templateId: string | null) => void;
  onLoadMore: () => void;
  onReset: () => void;
  onRefineWithAgent: () => void;
  onUpload: () => void;
  onFavoriteChanged?: () => void;
  onToggleFilters: () => void;
  scoreTemplateId: string | null;
  scoreTemplates: ScoreTemplate[];
}) {
  const [activeTab, setActiveTab] = useState<CandidateWorkbenchTab>("all");
  const [favoriteOverrides, setFavoriteOverrides] = useState<Record<string, boolean>>({});
  const [favoriteActionCandidateId, setFavoriteActionCandidateId] = useState<string | null>(null);
  const [favoriteError, setFavoriteError] = useState<string | null>(null);
  const displayColumns = activeResultDisplayColumns(appliedDraft);
  const appliedFilters = appliedFilterLabels(appliedDraft);
  const visibleAppliedFilters = appliedFilters.slice(0, 5);
  const hiddenAppliedFilterCount = appliedFilters.length - visibleAppliedFilters.length;
  const searchItems = useMemo(
    () => search.items.map((item) => ({
      ...item,
      is_favorited: favoriteOverrides[item.candidate_id] ?? item.is_favorited,
    })),
    [favoriteOverrides, search.items],
  );
  const visibleItems = useMemo(() => tabItems(searchItems, activeTab), [activeTab, searchItems]);

  useEffect(() => {
    if (!visibleItems.length) {
      onSelectCandidate(null);
      return;
    }
    if (!visibleItems.some((item) => item.candidate_id === selectedCandidateId)) {
      onSelectCandidate(visibleItems[0]);
    }
  }, [onSelectCandidate, selectedCandidateId, visibleItems]);

  useEffect(() => {
    setFavoriteOverrides({});
  }, [search]);

  const toggleFavorite = async (item: CandidateSearchItem) => {
    if (favoriteActionCandidateId === item.candidate_id) return;
    const wasFavorited = favoriteOverrides[item.candidate_id] ?? item.is_favorited;
    setFavoriteActionCandidateId(item.candidate_id);
    setFavoriteError(null);
    try {
      const nextState = wasFavorited
        ? (await api.unfavoriteCandidate(item.candidate_id), false)
        : (await api.favoriteCandidate(item.candidate_id)).is_favorited;
      setFavoriteOverrides((current) => ({ ...current, [item.candidate_id]: nextState }));
      onFavoriteChanged?.();
    } catch {
      setFavoriteError("更新收藏失败，请稍后重试。");
    } finally {
      setFavoriteActionCandidateId(null);
    }
  };

  const selectTab = (tab: CandidateWorkbenchTab) => {
    setActiveTab(tab);
  };

  const onRowKeyDown = (event: KeyboardEvent<HTMLTableRowElement>, item: CandidateSearchItem) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    event.preventDefault();
    onSelectCandidate(item);
  };

  return (
    <section className="results-pane" aria-label="候选人结果">
      <header className="results-header">
        <div className="results-summary">
          <h1>候选人库</h1>
          <p>{searching ? "正在更新候选人结果" : `${search.total_count} 位候选人，按综合评分排序`}</p>
        </div>
        <div className="results-toolbar">
          <BackofficeButton
            aria-controls="candidate-first-pass-filters"
            aria-expanded={filtersOpen}
            className="results-filter-toggle"
            icon={<Icon name="filter" size={16} />}
            onClick={onToggleFilters}
          >
            初筛
          </BackofficeButton>
          <div className="score-sort-control">
            <span>评分口径</span>
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
          <BackofficeButton
            ariaLabel={`交给 Agent 精筛当前 ${search.total_count} 位候选人`}
            className="results-agent-refine"
            disabled={searching || search.total_count === 0}
            icon={<Icon name="spark" size={16} />}
            onClick={onRefineWithAgent}
          >
            交给 Agent 精筛
          </BackofficeButton>
          <BackofficeButton
            ariaLabel="上传简历"
            icon={<Icon name="upload" size={16} />}
            onClick={onUpload}
            tone="primary"
          >
            导入简历
          </BackofficeButton>
        </div>
        {favoriteError && <p className="results-favorite-error" role="alert">{favoriteError}</p>}
      </header>

      <div className="candidate-workbench-tabs" role="tablist" aria-label="候选人视图">
        {([
          ["all", "全部"],
          ["priority", "优先查看"],
          ["review", "建议核验"],
          ["favorites", "收藏"],
        ] as Array<[CandidateWorkbenchTab, string]>).map(([tab, label]) => (
          <button
            aria-selected={activeTab === tab}
            className={activeTab === tab ? "is-active" : ""}
            key={tab}
            onClick={() => selectTab(tab)}
            role="tab"
            type="button"
          >
            {label}
          </button>
        ))}
        <span className="candidate-workbench-sort">综合评分 · 高到低</span>
      </div>

      {appliedFilters.length > 0 && (
        <div className="applied-filter-bar" aria-label="已应用的筛选条件">
          <div className="applied-filter-list">
            {visibleAppliedFilters.map((label) => <span className="applied-filter-chip" key={label} title={label}>{label}</span>)}
            {hiddenAppliedFilterCount > 0 && <span className="applied-filter-chip applied-filter-chip-more">+{hiddenAppliedFilterCount}</span>}
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

      <div aria-label="候选人结果，可横向滚动查看筛选字段" className="table-scroll" role="region" tabIndex={0}>
        {searching && !search.items.length ? (
          <TableSkeleton />
        ) : visibleItems.length ? (
          <table className="candidate-table candidate-workbench-table">
            <thead>
              <tr>
                <th scope="col">候选人 / AI 摘要</th>
                <th scope="col">学历 / 院校</th>
                <th scope="col">经历</th>
                <th scope="col">核心技能</th>
                {displayColumns.map((column) => <th className="result-display-column" key={column.key} scope="col">{column.label}</th>)}
                <th scope="col">综合评分</th>
                <th scope="col">可信度</th>
                <th scope="col">待确认</th>
                <th aria-label="查看详情" scope="col" />
              </tr>
            </thead>
            <tbody>
              {visibleItems.map((item) => {
                const scoreConfidence = candidateScoreConfidencePresentation(item.score_confidence);
                const pending = candidatePendingReasons(item);
                const isFavorited = item.is_favorited;
                const favoriteUpdating = favoriteActionCandidateId === item.candidate_id;
                const selected = selectedCandidateId === item.candidate_id;
                return (
                  <tr
                    aria-selected={selected}
                    className={selected ? "is-selected" : ""}
                    key={item.resume_id}
                    onClick={() => onSelectCandidate(item)}
                    onKeyDown={(event) => onRowKeyDown(event, item)}
                    tabIndex={0}
                  >
                    <td className="candidate-result-cell">
                      <div className="candidate-person">
                        <div>
                          <span className="candidate-name">{item.display_name?.trim() || "未命名候选人"}</span>
                          {item.summary_preview && <span className="candidate-summary-preview" title={item.summary_preview}>{item.summary_preview}</span>}
                        </div>
                        <button
                          aria-busy={favoriteUpdating}
                          aria-label={isFavorited ? `取消收藏 ${item.display_name?.trim() || "未命名候选人"}` : `收藏 ${item.display_name?.trim() || "未命名候选人"}`}
                          aria-pressed={isFavorited}
                          className={`candidate-row-favorite${isFavorited ? " is-favorited" : ""}`}
                          disabled={favoriteUpdating}
                          onClick={(event) => {
                            event.stopPropagation();
                            void toggleFavorite(item);
                          }}
                          type="button"
                        >
                          {favoriteUpdating ? <i className="spinner" /> : <Icon name="bookmark" size={14} />}
                        </button>
                      </div>
                    </td>
                    <td><CandidateEducationCell item={item} /></td>
                    <td><CandidateExperienceCell item={item} /></td>
                    <td><CandidateSkillHighlights item={item} /></td>
                    {displayColumns.map((column) => (
                      <td className="result-display-cell" key={column.key}>
                        <ResultDisplayValues fieldKey={column.key} item={item} label={column.label} />
                      </td>
                    ))}
                    <td className="candidate-score-cell">
                      {item.score_total !== null ? (
                        <button
                          aria-label={`查看 ${item.display_name?.trim() || "候选人"} 的评分详情`}
                          className="candidate-score-link"
                          onClick={(event) => {
                            event.stopPropagation();
                            onOpenCandidate(item, "score");
                          }}
                          type="button"
                        >
                          <strong>{item.score_total.toFixed(1)}</strong>
                          <span>/ 100</span>
                        </button>
                      ) : <span className="library-empty-copy">尚未评分</span>}
                    </td>
                    <td>
                      <span className={`score-confidence is-${scoreConfidence.tone}`}>{scoreConfidence.label}</span>
                    </td>
                    <td className="candidate-pending-cell">
                      {pending.length ? <span>{pending.length} 项</span> : <span className="candidate-meta">无</span>}
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
              <span className="empty-glyph"><Icon name={activeTab === "favorites" ? "bookmark" : "search"} size={24} /></span>
              <h2>{activeTab === "favorites" ? "当前结果中没有收藏候选人" : "没有符合条件的候选人"}</h2>
              <p>调整初步筛选，或交给 Agent 根据岗位画像进一步精筛。</p>
              {activeTab !== "all" && (
                <button className="button button-ghost" onClick={() => selectTab("all")} type="button">查看全部候选人</button>
              )}
            </div>
          </div>
        )}
      </div>

      <footer className="results-footer">
        <span>{searching ? <span className="loading-line"><i className="spinner" />正在查询候选人…</span> : `显示 ${visibleItems.length} / ${search.total_count} 位候选人`}</span>
        {search.next_cursor && activeTab === "all" && (
          <button className="button button-ghost" disabled={searching} onClick={onLoadMore} type="button">
            加载更多 <Icon name="arrow-right" size={16} />
          </button>
        )}
      </footer>
    </section>
  );
}
