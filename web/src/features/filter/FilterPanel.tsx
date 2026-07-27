import { useState } from "react";
import { Icon } from "../../icons";
import {
  formatMinimumDuration,
  resolvedInstitutionClassificationOptions,
  sortInstitutionClassifications,
  type FilterDraft,
} from "./filter-model";
import type { FilterOptions } from "../../types";

const formalWorkDurationOptions = [0, 12, 24, 36, 60, 96];

/**
 * The left rail deliberately stays conservative. It only performs the three
 * factual first-pass checks that the extraction model can compare directly;
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
  ).filter((option) => option.value === "985" || option.value === "211");
  const internshipRequired = draft.experienceTypes.includes("internship");

  const update = (patch: Partial<FilterDraft>) =>
    onDraftChange({ ...draft, ...patch });

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
        </section>

        <section className="filter-section">
          <div className="filter-section-heading">
            <h3>正式工作</h3>
          </div>
          <label className="field-stack" htmlFor="min-formal-work">
            <span className="field-label">最低年限</span>
            <div className="select-wrap">
              <select
                className="select-field"
                id="min-formal-work"
                onChange={(event) =>
                  update({ minEmploymentMonths: Number(event.target.value) })
                }
                value={draft.minEmploymentMonths}
              >
                {formalWorkDurationOptions.map((months) => (
                  <option key={months} value={months}>
                    {formatMinimumDuration(months)}
                  </option>
                ))}
              </select>
              <Icon name="chevron-down" size={16} />
            </div>
          </label>
        </section>

        <section className="filter-section">
          <div className="filter-section-heading">
            <h3>实习经历</h3>
          </div>
          <label className="choice-row choice-row-single">
            <input
              checked={internshipRequired}
              onChange={() =>
                update({
                  experienceTypes: internshipRequired ? [] : ["internship"],
                })
              }
              type="checkbox"
            />
            要求有实习经历
          </label>
        </section>
      </div>
    </aside>
  );
}
