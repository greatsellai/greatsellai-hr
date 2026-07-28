import { useState } from "react";
import { Icon } from "../../icons";
import {
  clampMonths,
  formatMinimumDuration,
  resolvedInstitutionClassificationOptions,
  sortInstitutionClassifications,
  type FilterDraft,
} from "./filter-model";
import type { FilterOptions } from "../../types";

/**
 * The left rail deliberately stays conservative. It only performs structured,
 * source-grounded first-pass checks that the extraction model can compare directly;
 * nuanced requirements are handed to the Recruiting Agent with this result
 * set as its server-bound scope.
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
  const institutionClassifications = resolvedInstitutionClassificationOptions(
    filterOptions,
  );
  const initialExperienceTypes = filterOptions.experience_types.filter(
    (option) =>
      option.value === "employment" || option.value === "internship",
  );

  const update = (patch: Partial<FilterDraft>) =>
    onDraftChange({ ...draft, ...patch });
  const updateAfterTyping = (patch: Partial<FilterDraft>) =>
    onDraftChange({ ...draft, ...patch }, "debounced");

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
          <button className="text-button" onClick={onReset} type="button">
            清空
          </button>
        </div>
      </div>

      <div className="filter-scroll filter-scroll-basic" id="filter-controls">
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
            <h3>工作年限</h3>
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
            <div className="range-values" aria-live="polite">
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
            <div className="range-values" aria-live="polite">
              <span>
                {formatMinimumDuration(
                  draft.minEmploymentOrInternshipMonths,
                )}
              </span>
              <span>20 年</span>
            </div>
          </div>
        </section>

        <section className="filter-section">
          <div className="filter-section-heading">
            <h3>经历要求</h3>
          </div>
          <div aria-label="经历类型条件" className="choice-grid" role="group">
            {initialExperienceTypes.map((option) => (
              <label className="choice-row" key={option.value}>
                <input
                  checked={draft.experienceTypes.includes(option.value)}
                  onChange={() =>
                    update({
                      experienceTypes: draft.experienceTypes.includes(option.value)
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
        </section>
      </div>
    </aside>
  );
}
