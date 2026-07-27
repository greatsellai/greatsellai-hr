import {
  useCallback,
  useEffect,
  useState,
  type CSSProperties,
} from "react";
import { api } from "../../api";
import type {
  ResumeScore,
  ResumeScoreBatch,
  ResumeScoreBatchItem,
  ScoreTemplate,
} from "../../types";
import { Icon } from "../../icons";
import { formatLibraryDate } from "../../backoffice/utils/formatters";
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
      notify("error", "同一评分规则内不能使用重复的维度名称。");
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
      onTemplateCreated(created);
      notify("success", `评分模板“${created.name}”已创建。`);
    } catch (error) {
      notify("error", formatError(error));
    } finally {
      setSavingTemplate(false);
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
                <p>权重总和必须为 100。模板用于简历库的通用评分；按 JD 的岗位评估请在招聘详情中发起。</p>
              </div>
              <BackofficeButton
                icon={<Icon name="refresh" size={15} />}
                loading={loadingTemplates}
                onClick={() => void loadTemplates()}
              >
                刷新模板
              </BackofficeButton>
            </div>
            <section aria-labelledby="score-preset-heading" className="score-template-preset-section">
              <div className="score-template-preset-copy">
                <h3 id="score-preset-heading">从预置起点开始</h3>
                <p>选择后仍可修改规则名称、维度、权重和评分说明。</p>
              </div>
              <div className="score-template-presets" role="group" aria-label="评分规则预置起点">
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
                  规则名称
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
                <p>按所选模板对当前工作区所有符合条件的启用简历评分。岗位 JD 匹配度请在招聘详情中运行。</p>
              </div>
            </div>
            <div className="field-stack">
              <label className="field-label" id="score-template-label">
                选择评分模板
              </label>
              <BackofficeSelect
                ariaLabelledBy="score-template-label"
                id="score-template"
                onChange={setTemplateId}
                options={templates.map((template) => ({
                  label: `${template.name} · v${template.version}`,
                  value: template.template_id,
                }))}
                placeholder="选择评分模板"
                value={templateId}
              />
            </div>
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
                  className="fact-row"
                  key={template.template_id}
                  onClick={() => setTemplateId(template.template_id)}
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

function ScoreResult({
  score,
  onOverride,
}: {
  score: ResumeScore;
  onOverride: (
    scoreId: string,
    dimensionKey: string,
    rawScore: number,
    reason: string,
  ) => Promise<void>;
}) {
  const scoreStyle = {
    "--score": Math.max(0, Math.min(100, score.total_score)),
  } as CSSProperties;
  const [editingDimensionKey, setEditingDimensionKey] = useState<string | null>(
    null,
  );
  const [draftRawScore, setDraftRawScore] = useState("");
  const [draftReason, setDraftReason] = useState("");
  const [savingOverride, setSavingOverride] = useState(false);
  const riskFlags = score.analysis.risk_flags ?? [];
  const scoreDimensionLabels = new Map(
    score.dimension_scores.map((dimension) => [dimension.key, dimension.label]),
  );

  useEffect(() => {
    setEditingDimensionKey(null);
    setDraftRawScore("");
    setDraftReason("");
  }, [score.score_id]);

  const beginOverride = (dimension: ResumeScore["dimension_scores"][number]) => {
    setEditingDimensionKey(dimension.key);
    setDraftRawScore(String(dimension.final_raw_score));
    setDraftReason(dimension.manual_reason ?? "");
  };
  const saveOverride = async (
    dimension: ResumeScore["dimension_scores"][number],
  ) => {
    const rawScore = Number(draftRawScore);
    if (!Number.isFinite(rawScore) || rawScore < 0 || rawScore > 100) {
      return;
    }
    if (!draftReason.trim()) return;
    setSavingOverride(true);
    try {
      await onOverride(score.score_id, dimension.key, rawScore, draftReason.trim());
      setEditingDimensionKey(null);
    } catch {
      // The caller has already presented an actionable error message.
    } finally {
      setSavingOverride(false);
    }
  };
  return (
    <section className="panel">
      <div className="panel-heading">
        <div>
          <h2>本次评分</h2>
          <p>
            模板 v{score.template_version} · 事实 v{score.facts_version} · {score.status === "overridden" ? "含人工调整" : "AI 原始评分"}
            {!score.is_current_facts_version ? " · 简历事实已更新，请重新评分" : ""}
          </p>
        </div>
      </div>
      <div className="score-result">
        <div
          aria-label={`综合评分 ${score.total_score}`}
          className="score-number"
          data-value={score.total_score.toFixed(1)}
          style={scoreStyle}
        >
          <span>{score.total_score.toFixed(1)}</span>
        </div>
        <div className="score-dimension-list">
          {score.dimension_scores.map((dimension) => {
            const hasManualAdjustment =
              dimension.manual_reason !== null ||
              dimension.final_raw_score !== dimension.ai_raw_score;
            return (
              <div className="score-dimension-detail" key={dimension.key}>
                <div className="score-dimension">
                  <span>{dimension.label}</span>
                  <div className="score-bar">
                    <i
                      style={{
                        width: `${Math.max(0, Math.min(100, dimension.final_raw_score))}%`,
                      }}
                    />
                  </div>
                  <strong>{dimension.final_raw_score.toFixed(0)} / 100</strong>
                </div>
                <div className="score-dimension-meta">
                  <span>AI 原始分 {dimension.ai_raw_score.toFixed(0)} / 100 · 权重 {dimension.weight}%</span>
                  {hasManualAdjustment && <span className="score-manual-mark">人工调整后 {dimension.final_raw_score.toFixed(0)} / 100</span>}
                </div>
                <p className="score-dimension-rationale">{dimension.rationale || "信息不足，未提供可验证判断依据。"}</p>
                <div className="score-evidence-row">
                  <span>
                    {dimension.fact_evidence.length
                      ? `事实依据：${dimension.fact_evidence.map((fact) => fact.summary).join("；")}`
                      : "事实依据不足"}
                  </span>
                  {dimension.uncertainties.length > 0 && (
                    <span>待核实：{dimension.uncertainties.join("；")}</span>
                  )}
                </div>
                {dimension.manual_reason && (
                  <p className="score-manual-reason">人工调整原因：{dimension.manual_reason}</p>
                )}
                {editingDimensionKey === dimension.key ? (
                  <form
                    className="score-override-form"
                    onSubmit={(event) => {
                      event.preventDefault();
                      void saveOverride(dimension);
                    }}
                  >
                    <label className="field-stack">
                      <span className="field-label">人工原始分（0 至 100）</span>
                      <input
                        className="field"
                        max="100"
                        min="0"
                        onChange={(event) => setDraftRawScore(event.target.value)}
                        step="0.1"
                        type="number"
                        value={draftRawScore}
                      />
                    </label>
                    <label className="field-stack">
                      <span className="field-label">调整原因</span>
                      <textarea
                        className="textarea-field score-override-reason"
                        onChange={(event) => setDraftReason(event.target.value)}
                        placeholder="说明为什么需要调整此维度分数"
                        value={draftReason}
                      />
                    </label>
                    <div className="review-actions">
                      <button
                        className="button button-ghost"
                        disabled={savingOverride}
                        onClick={() => setEditingDimensionKey(null)}
                        type="button"
                      >
                        取消
                      </button>
                      <button
                        className="button button-primary"
                        disabled={
                          savingOverride ||
                          !draftReason.trim() ||
                          !Number.isFinite(Number(draftRawScore))
                        }
                        type="submit"
                      >
                        {savingOverride ? <><i className="spinner" />正在保存</> : <><Icon name="check" size={16} />保存人工调整</>}
                      </button>
                    </div>
                  </form>
                ) : (
                  <button
                    className="text-button score-override-button"
                    onClick={() => beginOverride(dimension)}
                    type="button"
                  >
                    人工调整此维度
                  </button>
                )}
              </div>
            );
          })}
          <div className="evidence-item">
            <b>AI 分析</b>
            {typeof score.analysis.overall_summary === "string"
              ? score.analysis.overall_summary
              : "评分已生成。请结合各维度依据完成判断。"}
          </div>
          {riskFlags.length > 0 && (
            <div className="score-risk-list">
              <b>待关注项</b>
              <ul>
                {riskFlags.map((item, index) => (
                  <li key={`${item.message}-${index}`}>
                    {item.message}
                    {item.fact_evidence.length > 0 && (
                      <small>
                        依据：{item.fact_evidence.map((fact) => fact.summary).join("；")}
                      </small>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {score.audit_trail.length > 0 && (
            <div className="score-audit-list">
              <b>人工调整记录</b>
              <ul>
                {score.audit_trail.map((entry) => (
                  <li key={entry.audit_id}>
                    <strong>
                      {entry.dimension_key
                        ? scoreDimensionLabels.get(entry.dimension_key) ?? "评分维度"
                        : "评分维度"}
                    </strong>
                    <span>
                      {entry.previous_final_raw_score ?? "—"} → {entry.final_raw_score ?? "—"} · {entry.reason ?? "未填写原因"}
                    </span>
                    <small>{entry.actor} · {formatLibraryDate(entry.created_at)}</small>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      </div>
    </section>
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
