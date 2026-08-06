import { useCallback, useEffect, useRef, useState, type KeyboardEvent } from "react";
import { api } from "../../api";
import type {
  CandidateDataAuditEvent,
  CandidateDataDeletionBatch,
  CandidateDataDeletionReason,
  CandidateDataExport,
  CandidateDataRetentionCleanupRun,
  CandidateDataRetentionMode,
  CandidateDataRetentionPolicy,
  CandidateDataRetentionPreview,
} from "../../types";
import { Icon } from "../../icons";
import { TableSkeleton } from "../../backoffice/ui/TableSkeleton";
import { formatLibraryDate } from "../../backoffice/utils/formatters";
import "./candidate-data.css";

type ToastKind = "success" | "error";
type CandidateDataEmbeddedSection = "retention" | "activity";

const candidateDataEmbeddedSections: Array<{
  id: CandidateDataEmbeddedSection;
  label: string;
  icon: "gear" | "history";
}> = [
  { id: "retention", label: "保留策略", icon: "gear" },
  { id: "activity", label: "操作与记录", icon: "history" },
];

const candidateDataDeletionReasonOptions: Array<{
  value: CandidateDataDeletionReason;
  label: string;
}> = [
  { value: "candidate_request", label: "候选人提出删除" },
  { value: "recruitment_closed", label: "招聘流程结束" },
  { value: "duplicate", label: "重复资料" },
  { value: "other", label: "其他原因" },
];


function candidateDataDeletionReasonLabel(reason: CandidateDataDeletionReason): string {
  return candidateDataDeletionReasonOptions.find((option) => option.value === reason)?.label
    ?? "系统保留策略";
}

function candidateDataExportStatusLabel(status: string): string {
  switch (status) {
    case "queued": return "等待导出";
    case "running": return "正在导出";
    case "completed": return "可下载";
    case "retryable_failed": return "等待重试";
    case "failed": return "导出失败";
    case "cancelled": return "已取消";
    case "revoked": return "已撤销";
    case "expired": return "已过期";
    default: return status;
  }
}

function candidateDataExportStatusClass(status: string): string {
  if (status === "completed") return "is-success";
  if (status === "failed" || status === "revoked" || status === "expired") return "is-error";
  if (status === "retryable_failed") return "is-warning";
  return "is-progress";
}

function candidateDataRetentionRunStatusLabel(status: string): string {
  switch (status) {
    case "completed": return "已完成";
    case "completed_with_errors": return "完成但有异常";
    case "failed": return "清理失败";
    case "running": return "正在处理";
    default: return status;
  }
}

function candidateDataAuditActionLabel(event: CandidateDataAuditEvent): string {
  const labels: Record<string, string> = {
    resume_original_view_authorized: "已授权查看原文件",
    resume_original_download_authorized: "已授权下载原文件",
    resume_delete_requested: "已请求删除当前简历",
    candidate_delete_requested: "已请求删除候选人资料",
    resume_restored: "已恢复简历",
    candidate_restored: "已恢复候选人资料",
    retention_policy_changed: "已更新保留策略",
    retention_cleanup_completed: "已执行到期清理",
    candidate_data_export_requested: "已创建资料导出",
    candidate_data_export_cancelled: "已取消资料导出",
    candidate_data_export_download_authorized: "已授权下载导出文件",
  };
  return labels[event.action] ?? event.action;
}

export function CandidateDataLifecyclePage({
  formatError,
  notify,
  onOpenLibrary,
  embedded = false,
}: {
  formatError: (error: unknown) => string;
  notify: (kind: ToastKind, message: string) => void;
  onOpenLibrary: () => void;
  embedded?: boolean;
}) {
  const pageClassName = `candidate-data-page${embedded ? " is-embedded" : " page-frame"}`;
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [policy, setPolicy] = useState<CandidateDataRetentionPolicy | null>(null);
  const [retentionMode, setRetentionMode] = useState<CandidateDataRetentionMode>("manual");
  const [retentionDays, setRetentionDays] = useState("365");
  const [preview, setPreview] = useState<CandidateDataRetentionPreview | null>(null);
  const [runs, setRuns] = useState<CandidateDataRetentionCleanupRun[]>([]);
  const [deletions, setDeletions] = useState<CandidateDataDeletionBatch[]>([]);
  const [exports, setExports] = useState<CandidateDataExport[]>([]);
  const [auditEvents, setAuditEvents] = useState<CandidateDataAuditEvent[]>([]);
  const [savingPolicy, setSavingPolicy] = useState(false);
  const [previewing, setPreviewing] = useState(false);
  const [cleaning, setCleaning] = useState(false);
  const [restoringBatchId, setRestoringBatchId] = useState<string | null>(null);
  const [cancellingExportId, setCancellingExportId] = useState<string | null>(null);
  const [downloadingExportId, setDownloadingExportId] = useState<string | null>(null);
  const [embeddedSection, setEmbeddedSection] = useState<CandidateDataEmbeddedSection>("retention");
  const embeddedTabRefs = useRef<Array<HTMLButtonElement | null>>([]);

  const load = useCallback(async (showLoading = true) => {
    if (showLoading) setLoading(true);
    else setRefreshing(true);
    setError(null);
    try {
      const [nextPolicy, nextRuns, nextDeletions, nextExports, nextAuditEvents] = await Promise.all([
        api.getCandidateDataRetentionPolicy(),
        api.listCandidateDataRetentionCleanupRuns(),
        api.listCandidateDataDeletions(),
        api.listCandidateDataExports(),
        api.listCandidateDataAuditEvents(30),
      ]);
      setPolicy(nextPolicy);
      setRetentionMode(nextPolicy.mode);
      setRetentionDays(nextPolicy.retention_days ? String(nextPolicy.retention_days) : "365");
      setRuns(nextRuns.items);
      setDeletions(nextDeletions.items);
      setExports(nextExports.items);
      setAuditEvents(nextAuditEvents.items);
    } catch (loadError) {
      const message = formatError(loadError);
      setError(message);
      if (!showLoading) notify("error", message);
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, [formatError, notify]);

  useEffect(() => {
    void load();
  }, [load]);

  const normalizedRetentionDays = Number.parseInt(retentionDays, 10);
  const validRetentionDays = Number.isInteger(normalizedRetentionDays)
    && normalizedRetentionDays >= 30
    && normalizedRetentionDays <= 3650;
  const previewMatchesPolicy = Boolean(
    preview
    && policy
    && preview.retention_days === normalizedRetentionDays
    && preview.policy_version === policy.version,
  );

  const previewRetention = async () => {
    if (!validRetentionDays || previewing) return;
    setPreviewing(true);
    try {
      setPreview(await api.previewCandidateDataRetention(normalizedRetentionDays));
    } catch (previewError) {
      notify("error", formatError(previewError));
    } finally {
      setPreviewing(false);
    }
  };

  const saveRetentionPolicy = async () => {
    if (savingPolicy) return;
    if (retentionMode === "automatic" && (!validRetentionDays || !previewMatchesPolicy || !preview)) {
      notify("error", "请先预览当前天数对应的清理范围，再启用自动清理。");
      return;
    }
    setSavingPolicy(true);
    try {
      const saved = await api.updateCandidateDataRetentionPolicy(
        retentionMode === "automatic"
          ? {
            mode: "automatic",
            retention_days: normalizedRetentionDays,
            preview_token: preview!.preview_token,
          }
          : { mode: "manual" },
      );
      setPolicy(saved);
      setRetentionMode(saved.mode);
      setRetentionDays(saved.retention_days ? String(saved.retention_days) : "365");
      setPreview(null);
      notify("success", saved.mode === "automatic" ? "已启用候选人资料自动保留策略。" : "已改为手动保留，系统不会按期限自动删除候选人资料。");
      await load(false);
    } catch (saveError) {
      notify("error", formatError(saveError));
    } finally {
      setSavingPolicy(false);
    }
  };

  const runRetentionCleanup = async () => {
    if (cleaning) return;
    setCleaning(true);
    try {
      const run = await api.runCandidateDataRetentionCleanup();
      setRuns((current) => [run, ...current.filter((item) => item.run_id !== run.run_id)]);
      notify(
        "success",
        run.queued_count
          ? `已将 ${run.queued_count} 位到期候选人加入可恢复删除流程。`
          : "本次没有符合条件的候选人需要进入删除流程。",
      );
      await load(false);
    } catch (cleanupError) {
      notify("error", formatError(cleanupError));
    } finally {
      setCleaning(false);
    }
  };

  const restoreDeletion = async (deletion: CandidateDataDeletionBatch) => {
    if (restoringBatchId) return;
    setRestoringBatchId(deletion.deletion_batch_id);
    try {
      const restored = await api.restoreCandidateDataDeletion(deletion.deletion_batch_id);
      notify(
        "success",
        `已恢复 ${restored.restored_candidate_count} 位候选人和 ${restored.restored_resume_count} 份简历。`,
      );
      await load(false);
    } catch (restoreError) {
      notify("error", formatError(restoreError));
    } finally {
      setRestoringBatchId(null);
    }
  };

  const cancelExport = async (item: CandidateDataExport) => {
    if (cancellingExportId) return;
    setCancellingExportId(item.export_id);
    try {
      const updated = await api.cancelCandidateDataExport(item.export_id);
      setExports((current) => current.map((entry) => entry.export_id === updated.export_id ? updated : entry));
      notify("success", "导出任务已取消，已撤销其下载权限。");
      await load(false);
    } catch (cancelError) {
      notify("error", formatError(cancelError));
    } finally {
      setCancellingExportId(null);
    }
  };

  const downloadExport = async (item: CandidateDataExport) => {
    if (downloadingExportId || item.status !== "completed") return;
    setDownloadingExportId(item.export_id);
    try {
      const access = await api.requestCandidateDataExportDownload(item.export_id);
      const blob = await api.getAuthorizedFileBlob(access.access_url);
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `candidate-data-export-${item.export_id.slice(0, 8)}.zip`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 0);
      notify("success", "已开始下载导出文件，系统已记录本次访问。");
      await load(false);
    } catch (downloadError) {
      notify("error", formatError(downloadError));
    } finally {
      setDownloadingExportId(null);
    }
  };

  const handleEmbeddedTabKeyDown = (
    event: KeyboardEvent<HTMLButtonElement>,
    currentIndex: number,
  ) => {
    let nextIndex = currentIndex;

    switch (event.key) {
      case "ArrowRight":
      case "ArrowDown":
        nextIndex = (currentIndex + 1) % candidateDataEmbeddedSections.length;
        break;
      case "ArrowLeft":
      case "ArrowUp":
        nextIndex = (currentIndex - 1 + candidateDataEmbeddedSections.length)
          % candidateDataEmbeddedSections.length;
        break;
      case "Home":
        nextIndex = 0;
        break;
      case "End":
        nextIndex = candidateDataEmbeddedSections.length - 1;
        break;
      default:
        return;
    }

    event.preventDefault();
    setEmbeddedSection(candidateDataEmbeddedSections[nextIndex].id);
    embeddedTabRefs.current[nextIndex]?.focus();
  };

  if (loading && !policy) {
    return <div className={pageClassName}><TableSkeleton /></div>;
  }

  return (
    <div className={pageClassName}>
      <header className="page-heading">
        <div>
          {embedded ? <h2>候选人数据与保留</h2> : <h1>数据保留与恢复</h1>}
          <p>
            {embedded
              ? "先设置候选人资料的保留策略；可在“操作与记录”中处理可恢复删除、导出和审计记录。"
              : "在工作区内管理候选人资料的保留期限、可恢复删除、导出文件和原文件访问记录。所有清理操作都会先进入恢复期，不会立即永久清除数据。"}
          </p>
        </div>
        <div className="candidate-data-page-actions">
          <button className="button" disabled={refreshing} onClick={() => void load(false)} type="button">
            {refreshing ? <><i className="spinner" />正在刷新</> : <><Icon name="refresh" size={16} />刷新记录</>}
          </button>
          <button className="button button-ghost" onClick={onOpenLibrary} type="button">返回简历库</button>
        </div>
      </header>

      {error && <p className="library-error" role="status">{error}</p>}

      {embedded && (
        <nav aria-label="候选人数据管理" className="candidate-data-task-navigation">
          <div aria-orientation="horizontal" className="candidate-data-task-navigation-list" role="tablist">
            {candidateDataEmbeddedSections.map((section, index) => {
              const selected = section.id === embeddedSection;
              return (
                <button
                  aria-controls="candidate-data-task-content"
                  aria-selected={selected}
                  className={`candidate-data-task-navigation-item${selected ? " is-active" : ""}`}
                  id={`candidate-data-tab-${section.id}`}
                  key={section.id}
                  onClick={() => setEmbeddedSection(section.id)}
                  onKeyDown={(event) => handleEmbeddedTabKeyDown(event, index)}
                  ref={(element) => { embeddedTabRefs.current[index] = element; }}
                  role="tab"
                  tabIndex={selected ? 0 : -1}
                  type="button"
                >
                  <Icon name={section.icon} size={16} />
                  <span>{section.label}</span>
                </button>
              );
            })}
          </div>
        </nav>
      )}

      <div
        aria-labelledby={embedded ? `candidate-data-tab-${embeddedSection}` : undefined}
        className={`candidate-data-layout${embedded ? ` is-${embeddedSection}-view` : ""}`}
        id={embedded ? "candidate-data-task-content" : undefined}
        role={embedded ? "tabpanel" : undefined}
        tabIndex={embedded ? -1 : undefined}
      >
        <div className="candidate-data-main-column">
          <section className="panel candidate-data-retention-panel" hidden={embedded && embeddedSection !== "retention"}>
            <div className="panel-heading">
              <div>
                <h2>候选人资料保留策略</h2>
                <p>自动清理只处理到期且未被保留标记的候选人，先进入可恢复删除流程。</p>
              </div>
              <span className={`status-pill${retentionMode === "automatic" ? " is-warning" : ""}`}>{retentionMode === "automatic" ? "自动保留" : "手动保留"}</span>
            </div>
            <fieldset className="candidate-data-retention-form" disabled={savingPolicy || previewing}>
              <div className="candidate-data-retention-options" role="radiogroup" aria-label="候选人资料保留方式">
                <label className="choice-row candidate-data-retention-option">
                  <input checked={retentionMode === "manual"} name="candidate-data-retention-mode" onChange={() => { setRetentionMode("manual"); setPreview(null); }} type="radio" />
                  <span><strong>手动保留</strong><small>不会按期限自动删除候选人资料。</small></span>
                </label>
                <label className="choice-row candidate-data-retention-option">
                  <input checked={retentionMode === "automatic"} name="candidate-data-retention-mode" onChange={() => setRetentionMode("automatic")} type="radio" />
                  <span><strong>自动保留</strong><small>到期候选人进入可恢复删除流程，恢复期结束后才清理。</small></span>
                </label>
              </div>
              {retentionMode === "automatic" && (
                <label className="field-stack candidate-data-retention-days" htmlFor="candidate-data-retention-days">
                  <span className="field-label">资料保留天数</span>
                  <input className="field" id="candidate-data-retention-days" inputMode="numeric" max="3650" min="30" onChange={(event) => { setRetentionDays(event.target.value); setPreview(null); }} type="number" value={retentionDays} />
                  <span className="field-help">可设置 30 至 3650 天。保存前必须先预览本次影响范围。</span>
                </label>
              )}
            </fieldset>
            {retentionMode === "automatic" && preview && (
              <section className="candidate-data-retention-preview" aria-live="polite">
                <div>
                  <strong>{previewMatchesPolicy ? "当前预览可用于保存" : "预览已过期，请重新计算"}</strong>
                  <p>计算于 {formatLibraryDate(preview.calculated_at)}，不会删除任何数据。</p>
                </div>
                <div className="candidate-data-retention-preview-stats">
                  <span><strong>{preview.eligible_candidate_count}</strong> 位候选人可能到期</span>
                  <span>关联 <strong>{preview.eligible_resume_count}</strong> 份简历</span>
                  <span><strong>{preview.held_candidate_count}</strong> 位被保留标记跳过</span>
                </div>
              </section>
            )}
            <div className="review-actions candidate-data-retention-actions">
              {retentionMode === "automatic" && (
                <button className="button" disabled={!validRetentionDays || previewing} onClick={() => void previewRetention()} type="button">
                  {previewing ? <><i className="spinner" />正在预览</> : <><Icon name="search" size={16} />预览清理范围</>}
                </button>
              )}
              <button className="button button-primary" disabled={savingPolicy || (retentionMode === "automatic" && !previewMatchesPolicy)} onClick={() => void saveRetentionPolicy()} type="button">
                {savingPolicy ? <><i className="spinner" />正在保存</> : <><Icon name="check" size={16} />保存保留策略</>}
              </button>
              <button className="button button-danger-ghost" disabled={cleaning || policy?.mode !== "automatic"} onClick={() => void runRetentionCleanup()} type="button">
                {cleaning ? <><i className="spinner" />正在执行</> : "立即执行到期清理"}
              </button>
            </div>
          </section>

          <section className="panel candidate-data-recovery-panel" hidden={embedded && embeddedSection !== "activity"}>
            <div className="panel-heading">
              <div>
                <h2>可恢复删除</h2>
                <p>此处仅显示删除批次与数量，不重新展示已删除候选人的姓名或原文件名。</p>
              </div>
              <span className="status-pill">{deletions.length} 条记录</span>
            </div>
            {deletions.length ? (
              <div className="table-scroll">
                <table className="candidate-table candidate-data-table">
                  <thead><tr><th scope="col">范围</th><th scope="col">原因</th><th scope="col">影响</th><th scope="col">恢复截止</th><th scope="col">状态</th><th scope="col" aria-label="恢复操作" /></tr></thead>
                  <tbody>
                    {deletions.map((item) => (
                      <tr key={item.deletion_batch_id}>
                        <td>{item.trigger_type === "manual_resume" ? "单份简历" : "候选人资料"}</td>
                        <td>{candidateDataDeletionReasonLabel(item.reason)}</td>
                        <td>{item.affected_candidate_count} 位候选人 · {item.affected_resume_count} 份简历</td>
                        <td>{formatLibraryDate(item.recovery_deadline_at)}</td>
                        <td><span className={`status-pill${item.restorable ? " is-warning" : ""}`}>{item.restorable ? "可恢复" : item.status === "restored" ? "已恢复" : "已进入清理"}</span></td>
                        <td>
                          {item.restorable && (
                            <button className="button button-ghost candidate-data-inline-action" disabled={restoringBatchId === item.deletion_batch_id} onClick={() => void restoreDeletion(item)} type="button">
                              {restoringBatchId === item.deletion_batch_id ? <><i className="spinner" />正在恢复</> : "恢复"}
                            </button>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : <CandidateDataEmptyState title="没有可恢复删除记录" description="从候选人抽屉删除资料后，恢复期限内的批次会显示在这里。" />}
          </section>

          <section className="panel candidate-data-export-panel" hidden={embedded && embeddedSection !== "activity"}>
            <div className="panel-heading">
              <div>
                <h2>资料导出</h2>
                <p>导出文件仅在到期前可下载。候选人资料被删除后，相关导出会立即撤销。</p>
              </div>
              <span className="status-pill">{exports.length} 项任务</span>
            </div>
            {exports.length ? (
              <div className="table-scroll">
                <table className="candidate-table candidate-data-table">
                  <thead><tr><th scope="col">内容</th><th scope="col">创建时间</th><th scope="col">有效期</th><th scope="col">状态</th><th scope="col" aria-label="导出操作" /></tr></thead>
                  <tbody>
                    {exports.map((item) => (
                      <tr key={item.export_id}>
                        <td>{item.item_count} 位候选人{item.include_originals ? " · 含原文件" : " · 不含原文件"}</td>
                        <td>{formatLibraryDate(item.requested_at)}</td>
                        <td>{item.expires_at ? formatLibraryDate(item.expires_at) : "—"}</td>
                        <td><span className={`status-pill ${candidateDataExportStatusClass(item.status)}`}>{candidateDataExportStatusLabel(item.status)}</span>{item.error_code && <small className="candidate-data-error-code">{item.error_code}</small>}</td>
                        <td className="candidate-data-export-actions">
                          {item.status === "completed" && <button className="button button-ghost candidate-data-inline-action" disabled={downloadingExportId === item.export_id} onClick={() => void downloadExport(item)} type="button">{downloadingExportId === item.export_id ? <><i className="spinner" />正在准备</> : <><Icon name="download" size={15} />下载</>}</button>}
                          {["queued", "running", "retryable_failed"].includes(item.status) && <button className="button button-ghost candidate-data-inline-action" disabled={cancellingExportId === item.export_id} onClick={() => void cancelExport(item)} type="button">{cancellingExportId === item.export_id ? <><i className="spinner" />正在取消</> : "取消"}</button>}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : <CandidateDataEmptyState title="还没有资料导出" description="在候选人抽屉中创建导出后，可在这里查看进度并下载。" />}
          </section>
        </div>

        <aside className="candidate-data-side-column" hidden={embedded && embeddedSection !== "activity"}>
          <section className="panel candidate-data-audit-panel">
            <div className="panel-heading">
              <div>
                <h2>访问与操作审计</h2>
                <p>查看、下载原文件与导出文件均会记录在此。</p>
              </div>
            </div>
            {auditEvents.length ? (
              <ol className="candidate-data-audit-list">
                {auditEvents.map((event) => (
                  <li key={event.event_id}>
                    <strong>{candidateDataAuditActionLabel(event)}</strong>
                    <span>{formatLibraryDate(event.created_at)}</span>
                    {event.reason_code && <small>{candidateDataDeletionReasonLabel(event.reason_code as CandidateDataDeletionReason)}</small>}
                  </li>
                ))}
              </ol>
            ) : <CandidateDataEmptyState title="暂无审计记录" description="后续的原文件访问、导出和删除操作会显示在这里。" />}
          </section>

          <section className="panel candidate-data-cleanup-history">
            <div className="panel-heading">
              <div><h2>到期清理记录</h2><p>系统只将符合策略的数据加入可恢复删除流程。</p></div>
            </div>
            {runs.length ? (
              <ol className="candidate-data-cleanup-list">
                {runs.map((run) => (
                  <li key={run.run_id}>
                    <div><strong>{candidateDataRetentionRunStatusLabel(run.status)}</strong><span>{formatLibraryDate(run.finished_at ?? run.started_at)}</span></div>
                    <small>扫描 {run.scanned_count}，加入删除 {run.queued_count}{run.skipped_hold_count ? `，保留跳过 ${run.skipped_hold_count}` : ""}{run.failed_count ? `，异常 ${run.failed_count}` : ""}</small>
                  </li>
                ))}
              </ol>
            ) : <CandidateDataEmptyState title="尚未执行到期清理" description="启用自动保留后，可先预览再手动执行首次清理。" />}
          </section>
        </aside>
      </div>
    </div>
  );
}

function CandidateDataEmptyState({
  title,
  description,
}: {
  title: string;
  description: string;
}) {
  return (
    <div className="candidate-data-empty">
      <strong>{title}</strong>
      <span>{description}</span>
    </div>
  );
}
