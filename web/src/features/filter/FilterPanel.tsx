import { type KeyboardEvent, useState } from "react";
import { Icon } from "../../icons";
import {
  clampPercentage,
  clampMonths,
  formatMaximumRankPercent,
  formatMinimumAcademicScore,
  formatMinimumDuration,
  MAX_TENURE_MONTHS,
  resolvedInstitutionClassificationOptions,
  sortInstitutionClassifications,
  type FilterDraft,
} from "./filter-model";
import type { FilterOptions } from "../../types";

/**
 * The left rail deliberately stays conservative. It only performs direct,
 * source-grounded first-pass checks; nuanced requirements are handed to the
 * Recruiting Agent with this result set as its server-bound scope.
 */
export function FilterPanel({
  draft,
  filterOptions,
  onDraftChange,
  onReset,
}: {
  draft: FilterDraft;
  filterOptions: FilterOptions;
  onDraftChange: (draft: FilterDraft, timing?: "immediate" | "debounced") => void;
  onReset: () => void;
}) {
  const [mobileFiltersOpen, setMobileFiltersOpen] = useState(false);
  const [keywordInput, setKeywordInput] = useState("");
  const institutionClassifications = resolvedInstitutionClassificationOptions(
    filterOptions,
  );

  const update = (patch: Partial<FilterDraft>) =>
    onDraftChange({ ...draft, ...patch });
  const updateAfterTyping = (patch: Partial<FilterDraft>) =>
    onDraftChange({ ...draft, ...patch }, "debounced");

  const addKeywords = (rawValue: string) => {
    const additions = rawValue
      .split(/[，,、;；\n]+/)
      .map((value) => value.trim())
      .filter(Boolean);
    if (!additions.length) return;

    const knownKeys = new Set(
      draft.keywords.map((keyword) => keyword.toLocaleLowerCase()),
    );
    const nextKeywords = [...draft.keywords];
    for (const keyword of additions) {
      const key = keyword.toLocaleLowerCase();
      if (knownKeys.has(key) || nextKeywords.length >= 10) continue;
      knownKeys.add(key);
      nextKeywords.push(keyword);
    }
    setKeywordInput("");
    if (nextKeywords.length !== draft.keywords.length) {
      update({ keywords: nextKeywords });
    }
  };

  const handleKeywordKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.nativeEvent.isComposing) return;
    if (event.key !== "Enter" && event.key !== "," && event.key !== "，") {
      return;
    }
    event.preventDefault();
    addKeywords(event.currentTarget.value);
  };

  const updateGraduationStartMonth = (startMonth: string) => {
    if (!startMonth) return;
    update({
      freshGraduateStartMonth: startMonth,
      freshGraduateEndMonth:
        !draft.freshGraduateEndMonth ||
        draft.freshGraduateEndMonth < startMonth
          ? startMonth
          : draft.freshGraduateEndMonth,
    });
  };

  const updateGraduationEndMonth = (endMonth: string) => {
    if (!endMonth) return;
    update({
      freshGraduateStartMonth:
        !draft.freshGraduateStartMonth ||
        endMonth < draft.freshGraduateStartMonth
          ? endMonth
          : draft.freshGraduateStartMonth,
      freshGraduateEndMonth: endMonth,
    });
  };

  return (
    <aside
      aria-label="初筛条件"
      className={`filter-panel${mobileFiltersOpen ? " is-mobile-open" : ""}`}
    >
      <div className="filter-panel-header">
        <h2 className="filter-panel-title">初筛条件</h2>
        <div className="filter-panel-header-actions">
          <button
            aria-controls="filter-controls"
            aria-expanded={mobileFiltersOpen}
            className="text-button filter-mobile-toggle"
            onClick={() => setMobileFiltersOpen((current) => !current)}
            type="button"
          >
            <Icon name="filter" size={15} />
            {mobileFiltersOpen ? "收起" : "展开"}
          </button>
          <button
            className="text-button"
            onClick={() => {
              setKeywordInput("");
              onReset();
            }}
            type="button"
          >
            清空
          </button>
        </div>
      </div>

      <div className="filter-scroll filter-scroll-basic" id="filter-controls">
        <section className="filter-section filter-condition-mode">
          <div className="filter-section-heading">
            <h3>全局匹配方式</h3>
          </div>
          <div
            aria-describedby="condition-match-mode-note"
            aria-label="全局匹配方式"
            className="choice-grid"
            role="radiogroup"
          >
            <label className="choice-row choice-row-detail">
              <input
                checked={draft.conditionMatchMode === "all"}
                name="condition-match-mode"
                onChange={() => update({ conditionMatchMode: "all" })}
                type="radio"
              />
              <span className="choice-row-copy">
                <strong>精确匹配</strong>
                <small>全部已设条件均需满足</small>
              </span>
            </label>
            <label className="choice-row choice-row-detail">
              <input
                checked={draft.conditionMatchMode === "any"}
                name="condition-match-mode"
                onChange={() => update({ conditionMatchMode: "any" })}
                type="radio"
              />
              <span className="choice-row-copy">
                <strong>模糊匹配</strong>
                <small>满足任一条件即可显示</small>
              </span>
            </label>
          </div>
          <p className="filter-field-note" id="condition-match-mode-note">
            不改变同一条件内的规则。模糊匹配会列出每位候选人未满足和待核实的条件。
          </p>
        </section>

        <section className="filter-section">
          <div className="filter-section-heading">
            <h3>院校等级</h3>
          </div>
          <div aria-label="院校等级条件" className="choice-grid" role="group">
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
                          : [...draft.institutionClassifications, option.value],
                      ),
                    })
                  }
                  type="checkbox"
                />
                {option.label}
              </label>
            ))}
          </div>
          <div className="field-stack">
            <span className="field-label">最高学历</span>
            <div aria-label="最高学历条件" className="choice-grid" role="group">
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
          </div>
        </section>

        <section className="filter-section">
          <div className="filter-section-heading">
            <h3>学业表现</h3>
          </div>
          <div className="field-stack">
            <label className="field-label" htmlFor="min-academic-score">
              最低成绩 / GPA（百分制）
            </label>
            <input
              aria-describedby="min-academic-score-note"
              aria-valuetext={formatMinimumAcademicScore(
                draft.minAcademicScorePercent,
              )}
              className="range-input"
              id="min-academic-score"
              max="100"
              min="0"
              onChange={(event) =>
                updateAfterTyping({
                  minAcademicScorePercent: clampPercentage(
                    Number(event.target.value),
                  ),
                })
              }
              step="1"
              type="range"
              value={draft.minAcademicScorePercent}
            />
            <div className="range-values" aria-live="polite">
              <span>{formatMinimumAcademicScore(draft.minAcademicScorePercent)}</span>
              <span>100 分</span>
            </div>
            <p className="filter-field-note" id="min-academic-score-note">
              平均分或标准化 GPA 达到门槛即可命中，仅使用简历原文明确写出的分数。
            </p>
          </div>
          <div className="field-stack">
            <label className="field-label" htmlFor="max-rank-percent">
              成绩排名前
            </label>
            <input
              aria-describedby="max-rank-percent-note"
              aria-valuetext={formatMaximumRankPercent(draft.maxRankPercent)}
              className="range-input"
              id="max-rank-percent"
              max="100"
              min="0"
              onChange={(event) =>
                updateAfterTyping({
                  maxRankPercent: clampPercentage(Number(event.target.value)),
                })
              }
              step="1"
              type="range"
              value={draft.maxRankPercent}
            />
            <div className="range-values" aria-live="polite">
              <span>{formatMaximumRankPercent(draft.maxRankPercent)}</span>
              <span>排名前 100%（需有排名）</span>
            </div>
            <p className="filter-field-note" id="max-rank-percent-note">
              仅匹配简历明确给出的名次和总人数。选择 100% 时仍只返回有明确排名记录的简历；当前不区分专业、班级或院系的排名范围。
            </p>
          </div>
        </section>

        <section className="filter-section">
          <div className="filter-section-heading">
            <h3>毕业状态</h3>
          </div>
          <div aria-label="毕业状态" className="choice-grid" role="radiogroup">
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
          {draft.graduationStatus !== "any" && (
            <div className="field-stack">
              <span className="field-label">毕业时间窗口</span>
              <div className="filter-inline-fields">
                <label className="field-stack" htmlFor="fresh-graduate-start-month">
                  <span className="field-label">起始月</span>
                  <input
                    id="fresh-graduate-start-month"
                    onChange={(event) =>
                      updateGraduationStartMonth(event.target.value)
                    }
                    type="month"
                    value={draft.freshGraduateStartMonth}
                  />
                </label>
                <label className="field-stack" htmlFor="fresh-graduate-end-month">
                  <span className="field-label">结束月</span>
                  <input
                    id="fresh-graduate-end-month"
                    onChange={(event) =>
                      updateGraduationEndMonth(event.target.value)
                    }
                    type="month"
                    value={draft.freshGraduateEndMonth}
                  />
                </label>
              </div>
              <p className="filter-field-note">
                应届：最高学历毕业时间在此区间；往届：毕业时间早于起始月。
              </p>
            </div>
          )}
        </section>

        <section className="filter-section">
          <div className="filter-section-heading">
            <h3>工作年限</h3>
          </div>
          <div className="field-stack">
            <label className="field-label" htmlFor="min-experience">
              最低工作年限
            </label>
            <input
              aria-valuetext={formatMinimumDuration(
                draft.minEmploymentOrInternshipMonths,
              )}
              className="range-input"
              id="min-experience"
              max={MAX_TENURE_MONTHS}
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
            <div className="range-values" aria-live="polite">
              <span>
                {formatMinimumDuration(draft.minEmploymentOrInternshipMonths)}
              </span>
              <span>20 年及以上</span>
            </div>
          </div>
        </section>

        <section className="filter-section">
          <div className="filter-section-heading">
            <h3>匹配关键词</h3>
          </div>
          <div className="field-stack">
            <label className="field-label" htmlFor="candidate-keywords">
              岗位相关关键词
            </label>
            <div className="chip-input">
              {draft.keywords.map((keyword) => (
                <button
                  aria-label={`移除关键词 ${keyword}`}
                  className="filter-keyword-chip"
                  key={keyword}
                  onClick={() =>
                    update({
                      keywords: draft.keywords.filter((value) => value !== keyword),
                    })
                  }
                  type="button"
                >
                  <span>{keyword}</span>
                  <Icon name="close" size={12} />
                </button>
              ))}
              <input
                aria-describedby="candidate-keywords-note"
                aria-label="添加匹配关键词"
                id="candidate-keywords"
                maxLength={120}
                onChange={(event) => setKeywordInput(event.target.value)}
                onKeyDown={handleKeywordKeyDown}
                placeholder={
                  draft.keywords.length ? "继续添加关键词" : "例如 Python、售前方案"
                }
                value={keywordInput}
              />
            </div>
            <p className="filter-field-note" id="candidate-keywords-note">
              输入后按 Enter 添加，最多 10 个；仅支持岗位相关的技能、经历或项目关键词。
            </p>
          </div>
          {draft.keywords.length > 0 && (
            <div className="field-stack">
              <span className="field-label">匹配方式</span>
              <div
                aria-label="关键词匹配方式"
                className="choice-grid"
                role="radiogroup"
              >
                {filterOptions.keyword_modes.map((option) => (
                  <label className="choice-row" key={option.value}>
                    <input
                      checked={draft.keywordsMode === option.value}
                      name="keyword-match-mode"
                      onChange={() => update({ keywordsMode: option.value })}
                      type="radio"
                    />
                    {option.label}
                  </label>
                ))}
              </div>
            </div>
          )}
        </section>
      </div>
    </aside>
  );
}
