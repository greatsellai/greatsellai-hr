import { useCallback, useEffect, useMemo, useState } from "react";
import { Icon } from "../../icons";
import { adminApi, adminErrorMessage } from "../admin-api";
import {
  AdminError,
  AdminLoading,
  AdminPageHeader,
  AdminStatus,
  formatDate,
  numberFormat,
} from "../AdminComponents";
import type { PlatformRuntimeOverview, RequestState, RuntimeLiveness } from "../admin-types";

const workerLabels: Record<string, string> = {
  background: "后台任务 Worker",
};

const queueLabels: Record<string, string> = {
  document_extraction: "文档解析",
  ai_extraction: "AI 提取",
  resume_summary: "自动总结",
  jd_match_item: "JD 匹配",
  resume_score_item: "批量评分",
  mailbox_background: "邮箱同步",
  transactional_email: "事务邮件",
  workspace_feedback_reward: "反馈奖励",
};

const livenessLabel: Record<RuntimeLiveness, string> = {
  live: "在线",
  stale: "心跳超时",
  stopped: "已停止",
  missing: "未上报",
};

function queueLabel(queueKey: string) {
  return queueLabels[queueKey] ?? queueKey;
}

function workerLabel(workerKind: string) {
  return workerLabels[workerKind] ?? workerKind;
}

function oldestPendingLabel(value: string | null) {
  if (!value) return "暂无待处理任务";
  const age = Math.max(0, Math.floor((Date.now() - new Date(value).getTime()) / 60_000));
  if (age < 1) return "刚刚进入队列";
  if (age < 60) return `等待约 ${age} 分钟`;
  return `等待约 ${Math.floor(age / 60)} 小时 ${age % 60} 分钟`;
}

function percentage(numerator: number, denominator: number) {
  if (denominator <= 0) return "—";
  return `${((numerator / denominator) * 100).toFixed(1)}%`;
}

export function AdminRuntimePage() {
  const [state, setState] = useState<RequestState>("loading");
  const [error, setError] = useState("");
  const [runtime, setRuntime] = useState<PlatformRuntimeOverview | null>(null);

  const load = useCallback(async () => {
    setState("loading");
    setError("");
    try {
      setRuntime(await adminApi.getRuntimeOverview());
      setState("ready");
    } catch (loadError) {
      setError(adminErrorMessage(loadError));
      setState("error");
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const attentionQueues = useMemo(() => runtime?.queues.filter((queue) => (
    queue.failed_count > 0 || queue.queued_count > 0
  )) ?? [], [runtime]);
  const outstandingTasks = useMemo(() => runtime?.queues.reduce((total, queue) => (
    total + queue.queued_count + queue.running_count
  ), 0) ?? 0, [runtime]);

  return (
    <section className="admin-page-frame" aria-labelledby="admin-runtime-title">
      <AdminPageHeader
        actions={<button className="button" onClick={() => void load()} type="button"><Icon name="refresh" size={16} />刷新诊断</button>}
        description="查看服务就绪、Worker 心跳与任务队列。所有信息均为不含候选人内容的运行元数据。"
        title="运行诊断"
      />

      {state === "loading" && <div className="admin-panel"><AdminLoading label="正在读取运行状态…" /></div>}
      {state === "error" && <div className="admin-panel"><AdminError message={error} onRetry={() => void load()} /></div>}
      {state === "ready" && runtime && (
        <>
          <div className="admin-metric-strip admin-runtime-metric-strip" aria-label="运行关键状态">
            <article>
              <span>服务就绪</span>
              <strong><AdminStatus label="已就绪" status="ready" /></strong>
              <small>数据库与当前应用实例可响应</small>
            </article>
            <article>
              <span>Worker</span>
              <strong><AdminStatus label={livenessLabel[runtime.worker_liveness]} status={runtime.worker_liveness} /></strong>
              <small>在线 {runtime.live_worker_process_count} / 已配置 {runtime.configured_worker_concurrency}，心跳阈值 {Math.max(1, Math.round(runtime.worker_stale_after_seconds / 60))} 分钟</small>
            </article>
            <article>
              <span>处理中任务</span>
              <strong>{numberFormat(outstandingTasks)}</strong>
              <small>{attentionQueues.length ? `${numberFormat(attentionQueues.length)} 个队列需要关注` : "当前没有积压任务"}</small>
            </article>
          </div>

          <section className="admin-panel admin-runtime-panel" aria-labelledby="runtime-worker-title">
            <div className="admin-section-heading">
              <div><h2 id="runtime-worker-title">后台 Worker</h2><p>心跳超时表示后台进程可能未运行，或无法完成一个处理循环。</p></div>
              <span className="admin-generated-at">更新时间：{formatDate(runtime.generated_at, true)}</span>
            </div>
            <div className="admin-runtime-table-wrap">
              <table className="admin-runtime-table">
                <thead><tr><th scope="col">组件</th><th scope="col">状态</th><th scope="col">最近心跳</th><th scope="col">最近完成循环</th><th scope="col">安全错误码</th></tr></thead>
                <tbody>
                  {runtime.workers.map((worker) => (
                    <tr key={worker.worker_kind}>
                      <th scope="row">{workerLabel(worker.worker_kind)}</th>
                      <td><AdminStatus label={livenessLabel[worker.liveness]} status={worker.liveness} /></td>
                      <td>{formatDate(worker.last_seen_at, true)}</td>
                      <td>{formatDate(worker.last_cycle_completed_at, true)}</td>
                      <td><code>{worker.last_error_code || "—"}</code></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>

          <section className="admin-panel admin-runtime-panel" aria-labelledby="runtime-queue-title">
            <div className="admin-section-heading">
              <div><h2 id="runtime-queue-title">任务队列</h2><p>失败和积压可在这里先定位到任务类别，再凭诊断编号在安全事件查询中定位详情。</p></div>
            </div>
            <div className="admin-runtime-table-wrap">
              <table className="admin-runtime-table">
                <thead><tr><th scope="col">队列</th><th scope="col">排队中</th><th scope="col">运行中</th><th scope="col">失败</th><th scope="col">积压最久</th></tr></thead>
                <tbody>
                  {runtime.queues.map((queue) => (
                    <tr key={queue.queue_key}>
                      <th scope="row">{queueLabel(queue.queue_key)}</th>
                      <td>{numberFormat(queue.queued_count)}</td>
                      <td>{numberFormat(queue.running_count)}</td>
                      <td><AdminStatus label={numberFormat(queue.failed_count)} status={queue.failed_count ? "failed" : "success"} /></td>
                      <td>{oldestPendingLabel(queue.oldest_pending_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>

          <section className="admin-panel admin-runtime-panel" aria-labelledby="runtime-ocr-title">
            <div className="admin-section-heading">
              <div>
                <h2 id="runtime-ocr-title">腾讯 OCR 使用统计</h2>
                <p>按日期汇总解析次数和页数，不保存候选人、简历、文件名或原文。统计从功能上线后开始累计。</p>
              </div>
              <span className="admin-generated-at">
                {runtime.ocr_usage.recorded_from ? `统计起始：${formatDate(runtime.ocr_usage.recorded_from)}` : "暂未记录 OCR 统计"}
              </span>
            </div>
            <div className="admin-metric-strip" aria-label="OCR 关键指标">
              <article>
                <span>OCR 尝试率</span>
                <strong>{percentage(runtime.ocr_usage.ocr_attempted_page_count, runtime.ocr_usage.total_source_pages)}</strong>
                <small>尝试 {numberFormat(runtime.ocr_usage.ocr_attempted_page_count)} / 来源 {numberFormat(runtime.ocr_usage.total_source_pages)} 页</small>
              </article>
              <article>
                <span>OCR 成功率</span>
                <strong>{percentage(runtime.ocr_usage.ocr_successful_page_count, runtime.ocr_usage.ocr_attempted_page_count)}</strong>
                <small>成功 {numberFormat(runtime.ocr_usage.ocr_successful_page_count)} / 尝试 {numberFormat(runtime.ocr_usage.ocr_attempted_page_count)} 页</small>
              </article>
              <article>
                <span>结果采用率</span>
                <strong>{percentage(runtime.ocr_usage.ocr_selected_page_count, runtime.ocr_usage.ocr_successful_page_count)}</strong>
                <small>采用 {numberFormat(runtime.ocr_usage.ocr_selected_page_count)} / 成功 {numberFormat(runtime.ocr_usage.ocr_successful_page_count)} 页</small>
              </article>
            </div>
            <div className="admin-runtime-table-wrap">
              <table className="admin-runtime-table">
                <thead><tr><th scope="col">纳入统计的解析</th><th scope="col">完成</th><th scope="col">失败</th><th scope="col">尝试 OCR 的文档</th><th scope="col">成功 OCR 的文档</th><th scope="col">采用 OCR 的文档</th><th scope="col">OCR 失败页</th></tr></thead>
                <tbody>
                  <tr>
                    <th scope="row">{numberFormat(runtime.ocr_usage.document_count)}</th>
                    <td>{numberFormat(runtime.ocr_usage.completed_document_count)}</td>
                    <td>{numberFormat(runtime.ocr_usage.failed_document_count)}</td>
                    <td>{numberFormat(runtime.ocr_usage.ocr_attempted_document_count)}</td>
                    <td>{numberFormat(runtime.ocr_usage.ocr_successful_document_count)}</td>
                    <td>{numberFormat(runtime.ocr_usage.ocr_selected_document_count)}</td>
                    <td>{numberFormat(runtime.ocr_usage.ocr_failed_page_count)}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </section>

          <section className="admin-panel admin-runtime-panel" aria-labelledby="runtime-failure-title">
            <div className="admin-section-heading">
              <div><h2 id="runtime-failure-title">最近失败事件</h2><p>只保留错误码和运行时间，不保留异常原文或候选人资料。</p></div>
            </div>
            {runtime.recent_failures.length ? (
              <ul className="admin-runtime-failure-list">
                {runtime.recent_failures.map((failure, index) => (
                  <li key={`${failure.queue_key}-${failure.occurred_at}-${index}`}>
                    <span className="admin-attention-icon is-error"><Icon name="activity" size={18} /></span>
                    <span><strong>{queueLabel(failure.queue_key)}</strong><small><code>{failure.error_code}</code>{failure.attempt_count ? ` · 第 ${failure.attempt_count} 次尝试` : ""}</small></span>
                    <time dateTime={failure.occurred_at}>{formatDate(failure.occurred_at, true)}</time>
                  </li>
                ))}
              </ul>
            ) : <p className="admin-inline-empty">最近没有需要处理的失败事件。</p>}
          </section>
        </>
      )}
    </section>
  );
}
