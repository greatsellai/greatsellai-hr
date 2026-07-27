import { useState } from "react";
import { Icon } from "../../icons";
import {
  clampMonths,
  formatMinimumDuration,
  resolvedInstitutionClassificationOptions,
  sortInstitutionClassifications,
  type FilterDraft,
  type MatchMode,
} from "./filter-model";
import type {
  FilterOptions,
  PresenceStatus,
  SavedFilter,
} from "../../types";

export function FilterPanel({
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
        <details className="filter-match-rules">
          <summary>
            <span>匹配规则</span>
            <span aria-hidden="true" className="filter-match-rules-hint">
              查看条件如何组合
            </span>
          </summary>
          <div className="filter-match-rules-content">
            <p>未选择的条件不会限制候选人。</p>
            <ul>
              <li>
                院校类型和最高学历的多选项均按任一项匹配；学校、专业、成绩等条件会在同一条教育经历中核验。
              </li>
              <li>
                项目/竞赛名称、公司、职位和获奖条件会在同一条经历中联合匹配。
              </li>
              <li>
                英语证书支持常见别名归一，例如四级、英语四级、CET4、CET-4；已选证书按任一项匹配。
              </li>
            </ul>
          </div>
        </details>
        <section className="filter-section">
          <div className="filter-section-heading">
            <h3>已保存的筛选</h3>
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
          </div>
          <span className="sr-only" id="filter-rule-education">
            匹配规则：院校类型和最高学历的多选项均按任一项匹配；学校、专业、成绩等条件会在同一条教育经历中核验。
          </span>
          <div className="field-stack">
            <span className="field-label">院校类型</span>
            <div
              aria-describedby="filter-rule-education"
              aria-label="院校类型条件"
              className="choice-grid"
              role="group"
            >
              {institutionClassifications.map((option) => (
                <label className="choice-row" key={option.value}>
                  <input
                    aria-describedby="filter-rule-education"
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
          </div>
          <span className="field-label">最高学历</span>
          <div
            aria-describedby="filter-rule-education"
            aria-label="学历条件"
            className="choice-grid"
            role="group"
          >
            {filterOptions.degrees.map((option) => (
              <label className="choice-row" key={option.value}>
                <input
                  aria-describedby="filter-rule-education"
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
              aria-describedby="filter-rule-education"
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
                  aria-describedby="filter-rule-education"
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
                  aria-describedby="filter-rule-education"
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
                  aria-describedby="filter-rule-education"
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
                  aria-describedby="filter-rule-education"
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
              aria-describedby="filter-rule-education"
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
          </div>
          <span className="sr-only" id="filter-rule-experience">
            匹配规则：项目/竞赛名称、公司、职位和获奖条件会在同一条经历中联合匹配。
          </span>
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
            <div
              aria-describedby="filter-rule-experience"
              aria-label="经历类型条件"
              className="choice-grid"
              role="group"
            >
              {filterOptions.experience_types.map((option) => (
                <label className="choice-row" key={option.value}>
                  <input
                    aria-describedby="filter-rule-experience"
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
          </div>
          <div className="field-stack">
            <label className="field-label" htmlFor="experience-name">
              项目 / 竞赛 / 经历名称
            </label>
            <input
              aria-describedby="filter-rule-experience"
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
              aria-describedby="filter-rule-experience"
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
              aria-describedby="filter-rule-experience"
              className="field"
              id="role-name"
              onChange={(event) => updateAfterTyping({ title: event.target.value })}
              placeholder="例如：后端工程师"
              value={draft.title}
            />
          </div>
          <details className="advanced-filter">
            <summary>经历获奖情况（非必选）</summary>
            <div
              aria-describedby="filter-rule-experience"
              aria-label="经历获奖等级条件"
              className="choice-grid"
              role="group"
            >
              {filterOptions.award_levels.map((option) => (
                <label className="choice-row" key={option.value}>
                  <input
                    aria-describedby="filter-rule-experience"
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
              aria-describedby="filter-rule-experience"
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
          <span className="field-label">技能分类</span>
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
          </div>
          <span className="sr-only" id="filter-rule-language">
            匹配规则：英语证书支持常见别名归一；已选证书按任一项匹配。
          </span>
          <div className="credential-list">
            {filterOptions.language_credentials.map((option) => {
              const selected = draft.languageCredentials.includes(option.value);
              return (
                <div className="credential-row" key={option.value}>
                  <label className="choice-row">
                    <input
                      aria-describedby="filter-rule-language"
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
        </section>

        <section className="filter-section">
          <div className="filter-section-heading">
            <h3>奖学金与竞赛</h3>
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
