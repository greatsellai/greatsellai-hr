import {
  useCallback,
  useEffect,
  useState,
} from "react";
import { api } from "../../api";
import type {
  ResumeScoreBatch,
  ResumeScoreBatchItem,
  ScoreTemplate,
  ScoreTemplateOptimization,
} from "../../types";
import { Icon } from "../../icons";
import { BackofficeButton } from "../../backoffice/ui/BackofficeButton";
import { BackofficeSelect } from "../../backoffice/ui/BackofficeSelect";
import {
  createTemplateDraftDimensionId,
  createTemplateDraftDimensions,
  defaultScoreTemplatePreset,
  scoreTemplatePresets,
  type ScoreTemplatePreset,
  type TemplateDraftDimension,
} from "./score-model";
import "./scoring.css";

type ToastKind = "success" | "error";

type TemplateComparison = {
  name: string;
  description?: string | null;
  dimensions: Array<{
    label: string;
    weight: number;
    guidance?: string | null;
  }>;
};

export function ScoreWorkspace({
  formatError,
  notify,
  onScoreCreated,
  onTemplateCreated,
}: {
  formatError: (error: unknown) => string;
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
  const [optimization, setOptimization] = useState<ScoreTemplateOptimization | null>(null);
  const [optimizingTemplate, setOptimizingTemplate] = useState(false);
  const [applyingOptimization, setApplyingOptimization] = useState(false);
  const [optimizationError, setOptimizationError] = useState<string | null>(null);
  const [loadedOptimizationSource, setLoadedOptimizationSource] = useState<string | null>(
    null,
  );

  const selectedTemplate = templates.find((template) => template.template_id === templateId);

  const loadTemplates = useCallback(async () => {
    setLoadingTemplates(true);
    try {
      const response = await api.listScoreTemplates();
      setTemplates(response);
      setTemplateId((current) => current || response[0]?.template_id || "");
    } catch (error) {
      notify("error", formatError(error));
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
    setLoadedOptimizationSource(null);
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
      notify("error", "同一评分模板内不能使用重复的维度名称。");
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
      setLoadedOptimizationSource(null);
      onTemplateCreated(created);
      notify("success", `评分模板“${created.name}”已创建。`);
    } catch (error) {
      notify("error", formatError(error));
    } finally {
      setSavingTemplate(false);
    }
  };
  const selectTemplate = (nextTemplateId: string) => {
    setTemplateId(nextTemplateId);
    setOptimization(null);
    setOptimizationError(null);
  };
  const optimizeSelectedTemplate = async () => {
    if (!selectedTemplate) {
      setOptimizationError("请先选择一套评分模板，再生成优化建议。");
      return;
    }
    setOptimizingTemplate(true);
    setOptimization(null);
    setOptimizationError(null);
    try {
      const proposal = await api.optimizeScoreTemplate(selectedTemplate.template_id);
      if (
        proposal.source_template_id !== selectedTemplate.template_id ||
        proposal.source_template_version !== selectedTemplate.version
      ) {
        setOptimizationError("返回的优化建议与当前模板版本不一致，请重新生成建议。");
        return;
      }
      setOptimization(proposal);
    } catch (error) {
      setOptimizationError(formatError(error));
    } finally {
      setOptimizingTemplate(false);
    }
  };
  const loadOptimizationIntoDraft = () => {
    if (!optimization) return;
    const { proposed_template: proposal } = optimization;
    setSelectedPresetId(null);
    setTemplateName(proposal.name);
    setTemplateDescription(proposal.description ?? "");
    setDimensions(
      proposal.dimensions.map((dimension) => ({
        ...dimension,
        id: createTemplateDraftDimensionId(),
      })),
    );
    setLoadedOptimizationSource(selectedTemplate?.name ?? "原评分模板");
    setOptimization(null);
    setOptimizationError(null);
    notify(
      "success",
      "AI 建议已载入编辑器。修改后请创建新模板，原模板不会被改写。",
    );
  };
  const applyOptimization = async () => {
    if (!optimization) return;
    setApplyingOptimization(true);
    setOptimizationError(null);
    try {
      const created = await api.createScoreTemplate(optimization.proposed_template);
      setTemplates((current) => [created, ...current]);
      setTemplateId(created.template_id);
      setOptimization(null);
      setLoadedOptimizationSource(null);
      onTemplateCreated(created);
      notify("success", `已创建优化后的评分模板“${created.name}”。原模板保持不变。`);
    } catch (error) {
      setOptimizationError(`无法创建优化后的模板：${formatError(error)}`);
    } finally {
      setApplyingOptimization(false);
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
      notify("error", formatError(error));
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
    <div className="page-frame score-workspace">
      <header className="page-heading">
        <div>
          <h1>通用评分模板</h1>
        </div>
      </header>
      <div className="page-layout">
        <div>
          <section className="panel">
            <div className="panel-heading">
              <div>
                <h2>新建评分模板</h2>
                <p>权重总和必须为 100。模板用于简历库的通用评分；基于 JD 的岗位评估请在智能匹配中发起。</p>
              </div>
              <BackofficeButton
                icon={<Icon name="refresh" size={15} />}
                loading={loadingTemplates}
                onClick={() => void loadTemplates()}
              >
                刷新模板
              </BackofficeButton>
            </div>
            {loadedOptimizationSource && (
              <p className="score-template-draft-notice" role="status">
                <Icon name="spark" size={16} />
                已载入 AI 对“{loadedOptimizationSource}”的优化建议。你可以继续修改；创建后会生成新模板，不会修改原模板。
              </p>
            )}
            <section aria-labelledby="score-preset-heading" className="score-template-preset-section">
              <div className="score-template-preset-copy">
                <h3 id="score-preset-heading">从预设模板开始</h3>
                <p>选择后仍可修改模板名称、维度、权重和评分说明。</p>
              </div>
              <div className="score-template-presets" role="group" aria-label="评分模板预置起点">
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
                  模板名称
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
              <BackofficeButton
                icon={<Icon name="plus" size={15} />}
                onClick={() => {
                  setSelectedPresetId(null);
                  setDimensions((current) => [
                    ...current,
                    {
                      id: createTemplateDraftDimensionId(),
                      label: "新评分维度",
                      weight: 0,
                      guidance: "",
                    },
                  ]);
                }}
              >
                添加维度
              </BackofficeButton>
              <BackofficeButton
                icon={<Icon name="layers" size={16} />}
                loading={savingTemplate}
                onClick={() => void saveTemplate()}
                tone="primary"
              >
                {savingTemplate ? "正在创建…" : "创建评分模板"}
              </BackofficeButton>
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
                <p>按所选模板对当前工作区所有符合条件的启用简历评分。岗位 JD 匹配度请在智能匹配中查看。</p>
              </div>
            </div>
            <div className="field-stack">
              <label className="field-label" id="score-template-label">
                选择评分模板
              </label>
              <BackofficeSelect
                ariaLabelledBy="score-template-label"
                disabled={optimizingTemplate || applyingOptimization}
                id="score-template"
                onChange={selectTemplate}
                options={templates.map((template) => ({
                  label: `${template.name} · v${template.version}`,
                  value: template.template_id,
                }))}
                placeholder="选择评分模板"
                value={templateId}
              />
            </div>
            <section
              aria-busy={optimizingTemplate || applyingOptimization}
              aria-labelledby="score-template-optimization-heading"
              className="score-template-optimization"
            >
              <div className="score-template-optimization-heading">
                <div>
                  <h3 id="score-template-optimization-heading">AI 帮我优化</h3>
                  <p>基于所选模板提出可审阅的维度、权重和评分说明建议。确认前不会修改现有模板。</p>
                </div>
                <BackofficeButton
                  disabled={!selectedTemplate || optimizingTemplate || applyingOptimization}
                  icon={<Icon name="spark" size={16} />}
                  loading={optimizingTemplate}
                  onClick={() => void optimizeSelectedTemplate()}
                >
                  {optimizingTemplate ? "正在生成建议…" : "AI 帮我优化"}
                </BackofficeButton>
              </div>
              <p className="score-template-optimization-selection">
                <span>当前待优化模板</span>
                <strong>
                  {selectedTemplate
                    ? `${selectedTemplate.name} · v${selectedTemplate.version}`
                    : "尚未选择模板"}
                </strong>
              </p>
              {optimizingTemplate && (
                <p className="score-template-optimization-status" role="status">
                  <i className="spinner" />
                  正在分析“{selectedTemplate?.name}”的评分维度与说明…
                </p>
              )}
              {optimizationError && (
                <p className="library-error score-template-optimization-error" role="alert">
                  {optimizationError}
                </p>
              )}
              {optimization && selectedTemplate && (
                <TemplateOptimizationPreview
                  applying={applyingOptimization}
                  onApply={() => void applyOptimization()}
                  onDiscard={() => {
                    setOptimization(null);
                    setOptimizationError(null);
                  }}
                  onLoadIntoDraft={loadOptimizationIntoDraft}
                  optimization={optimization}
                  sourceTemplate={selectedTemplate}
                />
              )}
            </section>
            <div className="review-actions">
              <BackofficeButton
                disabled={!templateId || startingScoreBatch || scoreBatchIsRunning}
                icon={<Icon name="layers" size={16} />}
                loading={startingScoreBatch || scoreBatchIsRunning}
                onClick={() => void runAllScores()}
                tone="primary"
              >
                {startingScoreBatch
                  ? "正在创建任务…"
                  : scoreBatchIsRunning
                    ? "评分队列运行中…"
                    : "生成全部简历的通用评分"}
              </BackofficeButton>
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
                  aria-pressed={template.template_id === templateId}
                  className={`fact-row${template.template_id === templateId ? " is-selected" : ""}`}
                  disabled={optimizingTemplate || applyingOptimization}
                  key={template.template_id}
                  onClick={() => selectTemplate(template.template_id)}
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

function TemplateOptimizationPreview({
  applying,
  onApply,
  onDiscard,
  onLoadIntoDraft,
  optimization,
  sourceTemplate,
}: {
  applying: boolean;
  onApply: () => void;
  onDiscard: () => void;
  onLoadIntoDraft: () => void;
  optimization: ScoreTemplateOptimization;
  sourceTemplate: ScoreTemplate;
}) {
  const proposedTemplate = optimization.proposed_template;

  return (
    <div
      aria-labelledby="score-template-optimization-preview-heading"
      className="score-template-optimization-preview"
    >
      <div className="score-template-optimization-preview-heading">
        <div>
          <h4 id="score-template-optimization-preview-heading">优化建议对比</h4>
          <p>请逐项检查 AI 的建议。当前模板只用于对照，不会被这次操作修改。</p>
        </div>
        <span className="status-pill is-progress">待确认</span>
      </div>
      <div aria-label="当前模板与优化建议对比" className="score-template-optimization-comparison">
        <TemplateSnapshot
          template={sourceTemplate}
          title="当前模板"
          version={optimization.source_template_version}
        />
        <TemplateSnapshot isProposed template={proposedTemplate} title="AI 建议" />
      </div>
      <div className="score-template-improvement-notes">
        <h5>AI 提出的改进点</h5>
        {optimization.improvement_notes.length ? (
          <ul>
            {optimization.improvement_notes.map((note, index) => (
              <li key={`${note}-${index}`}>{note}</li>
            ))}
          </ul>
        ) : (
          <p>AI 未提供额外说明，请以维度、权重和评分说明的对比为准。</p>
        )}
      </div>
      <div className="score-template-optimization-confirmation">
        <p>确认后会将 AI 建议创建为一套新模板，当前模板“{sourceTemplate.name}”保持不变。</p>
        <div className="review-actions">
          <BackofficeButton
            disabled={applying}
            icon={<Icon name="document" size={16} />}
            onClick={onLoadIntoDraft}
          >
            载入编辑器修改
          </BackofficeButton>
          <BackofficeButton
            icon={<Icon name="check" size={16} />}
            loading={applying}
            onClick={onApply}
            tone="primary"
          >
            {applying ? "正在创建优化模板…" : "确认创建优化模板"}
          </BackofficeButton>
          <BackofficeButton disabled={applying} onClick={onDiscard}>
            放弃本次建议
          </BackofficeButton>
        </div>
      </div>
    </div>
  );
}

function TemplateSnapshot({
  isProposed = false,
  template,
  title,
  version,
}: {
  isProposed?: boolean;
  template: TemplateComparison;
  title: string;
  version?: number;
}) {
  return (
    <div className={`score-template-comparison-column${isProposed ? " is-proposed" : ""}`}>
      <div className="score-template-comparison-column-heading">
        <span>{title}</span>
        {typeof version === "number" && <small>v{version}</small>}
      </div>
      <h5>{template.name}</h5>
      <p className="score-template-comparison-description">
        {template.description?.trim() || "未填写评分说明。"}
      </p>
      <div className="score-template-comparison-dimensions">
        <span className="score-template-comparison-label">评分维度</span>
        <ol>
          {template.dimensions.map((dimension, index) => (
            <li key={`${dimension.label}-${index}`}>
              <div>
                <strong>{dimension.label}</strong>
                <span>{dimension.weight}%</span>
              </div>
              <p>{dimension.guidance?.trim() || "未填写 AI 评分说明。"}</p>
            </li>
          ))}
        </ol>
      </div>
    </div>
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
