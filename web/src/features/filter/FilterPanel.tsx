import { type KeyboardEvent, type KeyboardEventHandler, useState } from "react";
import { Icon } from "../../icons";
import {
  resolvedInstitutionClassificationOptions,
  sortInstitutionClassifications,
  type FilterDraft,
} from "./filter-model";
import type { FilterOptions, InstitutionClassification } from "../../types";

const experienceChoices = [
  { label: "不限", months: 0 },
  { label: "1 年+", months: 12 },
  { label: "3 年+", months: 36 },
  { label: "5 年+", months: 60 },
];

/**
 * The left rail only exposes direct, source-grounded first-pass checks. More
 * nuanced requirements intentionally move to the Recruiting Agent after the
 * recruiter narrows the result set.
 */
export function FilterPanel({
  draft,
  filterOptions,
  onDraftChange,
  onClose,
  onReset,
}: {
  draft: FilterDraft;
  filterOptions: FilterOptions;
  onDraftChange: (draft: FilterDraft, timing?: "immediate" | "debounced") => void;
  onClose: () => void;
  onReset: () => void;
}) {
  const [keywordInput, setKeywordInput] = useState("");
  const institutionClassifications = resolvedInstitutionClassificationOptions(
    filterOptions,
  );

  const update = (patch: Partial<FilterDraft>, timing: "immediate" | "debounced" = "immediate") =>
    onDraftChange({ ...draft, ...patch }, timing);

  const toggleInstitutionClassification = (value: InstitutionClassification) => {
    const next = draft.institutionClassifications.includes(value)
      ? draft.institutionClassifications.filter((item) => item !== value)
      : [...draft.institutionClassifications, value];
    update({ institutionClassifications: sortInstitutionClassifications(next) });
  };

  const toggleDegree = (value: FilterDraft["degrees"][number]) => {
    update({
      degrees: draft.degrees.includes(value)
        ? draft.degrees.filter((item) => item !== value)
        : [...draft.degrees, value],
    });
  };

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
    if (event.key !== "Enter" && event.key !== "," && event.key !== "，") return;
    event.preventDefault();
    addKeywords(event.currentTarget.value);
  };

  const handlePanelKeyDown: KeyboardEventHandler<HTMLElement> = (event) => {
    if (event.key === "Escape") onClose();
  };

  return (
    <aside
      aria-label="初筛条件"
      className="filter-panel"
      id="candidate-first-pass-filters"
      onKeyDown={handlePanelKeyDown}
    >
      <div className="filter-panel-header">
        <h2 className="filter-panel-title">初步筛选</h2>
        <div className="filter-panel-header-actions">
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
          <button
            aria-label="关闭初筛"
            className="filter-close-button"
            onClick={onClose}
            type="button"
          >
            <Icon name="close" size={16} />
          </button>
        </div>
      </div>

      <div className="filter-scroll filter-scroll-basic" id="filter-controls">
        <section className="filter-section">
          <h3>院校等级</h3>
          <div aria-label="院校等级条件" className="filter-option-grid" role="group">
            {institutionClassifications.map((option) => {
              const active = draft.institutionClassifications.includes(option.value);
              return (
                <button
                  aria-pressed={active}
                  className={`filter-option-button${active ? " is-selected" : ""}`}
                  key={option.value}
                  onClick={() => toggleInstitutionClassification(option.value)}
                  type="button"
                >
                  {option.label}
                </button>
              );
            })}
          </div>
        </section>

        <section className="filter-section">
          <h3>最高学历</h3>
          <div aria-label="最高学历条件" className="filter-option-grid" role="group">
            {filterOptions.degrees.map((option) => {
              const active = draft.degrees.includes(option.value);
              return (
                <button
                  aria-pressed={active}
                  className={`filter-option-button${active ? " is-selected" : ""}`}
                  key={option.value}
                  onClick={() => toggleDegree(option.value)}
                  type="button"
                >
                  {option.label}
                </button>
              );
            })}
          </div>
        </section>

        <section className="filter-section">
          <h3>毕业状态</h3>
          <div aria-label="毕业状态条件" className="filter-option-grid" role="group">
            {filterOptions.graduation_statuses.map((option) => {
              const active = draft.graduationStatus === option.value;
              return (
                <button
                  aria-pressed={active}
                  className={`filter-option-button${active ? " is-selected" : ""}`}
                  key={option.value}
                  onClick={() => update({ graduationStatus: option.value })}
                  type="button"
                >
                  {option.label}
                </button>
              );
            })}
          </div>
        </section>

        <section className="filter-section">
          <h3>工作年限</h3>
          <div aria-label="最低工作年限" className="filter-option-grid" role="group">
            {experienceChoices.map((option) => {
              const active = draft.minEmploymentOrInternshipMonths === option.months;
              return (
                <button
                  aria-pressed={active}
                  className={`filter-option-button${active ? " is-selected" : ""}`}
                  key={option.months}
                  onClick={() => update({ minEmploymentOrInternshipMonths: option.months })}
                  type="button"
                >
                  {option.label}
                </button>
              );
            })}
          </div>
        </section>

        <section className="filter-section">
          <h3>匹配关键词</h3>
          <div className="compact-chip-input">
            {draft.keywords.map((keyword) => (
              <button
                aria-label={`移除关键词 ${keyword}`}
                className="filter-keyword-chip"
                key={keyword}
                onClick={() => update({ keywords: draft.keywords.filter((value) => value !== keyword) })}
                type="button"
              >
                <span>{keyword}</span>
                <Icon name="close" size={12} />
              </button>
            ))}
            <input
              aria-label="添加匹配关键词"
              maxLength={120}
              onChange={(event) => setKeywordInput(event.target.value)}
              onKeyDown={handleKeywordKeyDown}
              placeholder={draft.keywords.length ? "继续添加" : "输入技能或关键词"}
              value={keywordInput}
            />
            <button
              aria-label="添加关键词"
              className="compact-chip-input-submit"
              disabled={!keywordInput.trim()}
              onClick={() => addKeywords(keywordInput)}
              type="button"
            >
              <Icon name="search" size={15} />
            </button>
          </div>
        </section>
      </div>
    </aside>
  );
}
