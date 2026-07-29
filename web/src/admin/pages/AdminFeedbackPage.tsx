import { useCallback, useEffect, useMemo, useState } from "react";
import { Icon } from "../../icons";
import {
  adminApi,
  adminErrorMessage,
  workspaceFeedbackAttachmentUrl,
} from "../admin-api";
import type {
  PlatformWorkspaceFeedback,
  RequestState,
  WorkspaceFeedbackRewardStatus,
} from "../admin-types";
import {
  AdminEmpty,
  AdminError,
  AdminLoading,
  AdminPageHeader,
  AdminPagination,
  AdminStatus,
  formatDate,
  numberFormat,
  shortId,
} from "../AdminComponents";

const PAGE_SIZE = 30;

const rewardPresentation: Record<WorkspaceFeedbackRewardStatus, { label: string; status: string }> = {
  queued: { label: "系统审核中", status: "queued" },
  running: { label: "发放中", status: "running" },
  granted: { label: "已发放", status: "succeeded" },
};

function readableResponse(value: string | null | undefined) {
  return value?.trim() || "未填写";
}

function formatBytes(value: number) {
  if (!Number.isFinite(value) || value <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  const index = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1);
  const amount = value / 1024 ** index;
  return `${amount >= 10 || index === 0 ? amount.toFixed(0) : amount.toFixed(1)} ${units[index]}`;
}

function FeedbackDetail({ item }: { item: PlatformWorkspaceFeedback | null }) {
  if (!item) {
    return (
      <aside className="admin-detail-pane admin-feedback-detail">
        <AdminEmpty
          description="从左侧列表选择一条反馈，查看提交内容、附件和自动奖励状态。"
          title="选择反馈"
        />
      </aside>
    );
  }

  const reward = rewardPresentation[item.reward_status];
  return (
    <aside className="admin-detail-pane admin-feedback-detail" aria-labelledby="admin-feedback-detail-title">
      <header className="admin-detail-header">
        <div>
          <span className="admin-detail-label">用户反馈</span>
          <h2 id="admin-feedback-detail-title">{item.organization_name || "未命名工作区"}</h2>
          <p>{item.submitter_name || "未命名用户"} · {item.submitter_email || "未提供邮箱"}</p>
        </div>
        <AdminStatus label={reward.label} status={reward.status} />
      </header>

      <dl className="admin-detail-metrics admin-feedback-metrics">
        <div><dt>提交时间</dt><dd>{formatDate(item.created_at, true)}</dd></div>
        <div><dt>奖励调用</dt><dd>{numberFormat(Math.max(0, item.reward_call_count || 0))}</dd></div>
        <div><dt>待发放时间</dt><dd>{formatDate(item.reward_due_at, true)}</dd></div>
        <div><dt>发放时间</dt><dd>{formatDate(item.reward_granted_at, true)}</dd></div>
      </dl>

      <section className="admin-detail-section admin-feedback-identifiers" aria-label="提交关联信息">
        <dl className="admin-fact-list">
          <div><dt>工作区 ID</dt><dd title={item.organization_id}>{shortId(item.organization_id)}</dd></div>
          <div><dt>提交人 ID</dt><dd title={item.submitted_by_user_id}>{shortId(item.submitted_by_user_id)}</dd></div>
          <div><dt>联系电话</dt><dd>{readableResponse(item.contact_phone)}</dd></div>
          <div><dt>反馈 ID</dt><dd title={item.feedback_id}>{shortId(item.feedback_id)}</dd></div>
        </dl>
      </section>

      <section className="admin-detail-section admin-feedback-response" aria-labelledby="admin-feedback-response-title">
        <div className="admin-detail-section-heading">
          <div>
            <h3 id="admin-feedback-response-title">原始反馈</h3>
            <p>仅平台管理员可见，不会写入操作审计。</p>
          </div>
        </div>
        <dl className="admin-feedback-answer-list">
          <div><dt>当前使用场景</dt><dd>{readableResponse(item.use_case)}</dd></div>
          <div><dt>希望达成的结果</dt><dd>{readableResponse(item.intended_outcome)}</dd></div>
          <div><dt>遇到的阻碍</dt><dd>{readableResponse(item.friction)}</dd></div>
          <div><dt>希望改进的内容</dt><dd>{readableResponse(item.desired_change)}</dd></div>
        </dl>
      </section>

      <section className="admin-detail-section admin-feedback-attachments" aria-labelledby="admin-feedback-attachments-title">
        <div className="admin-detail-section-heading">
          <div>
            <h3 id="admin-feedback-attachments-title">附件</h3>
            <p>{item.attachments.length ? "附件通过平台受保护接口打开。" : "这条反馈没有附加文件。"}</p>
          </div>
        </div>
        {!!item.attachments.length && (
          <ul className="admin-feedback-attachment-list">
            {item.attachments.map((attachment) => (
              <li key={attachment.attachment_id}>
                <span className="admin-feedback-attachment-icon" aria-hidden="true"><Icon name="document" size={17} /></span>
                <span>
                  <strong title={attachment.original_filename}>{attachment.original_filename || "未命名附件"}</strong>
                  <small>{attachment.content_type || "未知格式"} · {formatBytes(attachment.size_bytes)}</small>
                </span>
                <a
                  className="button button-ghost admin-feedback-attachment-link"
                  href={workspaceFeedbackAttachmentUrl(item.feedback_id, attachment.attachment_id)}
                  rel="noopener noreferrer"
                  target="_blank"
                >
                  <Icon name="download" size={15} />打开附件
                </a>
              </li>
            ))}
          </ul>
        )}
      </section>
    </aside>
  );
}

export function AdminFeedbackPage() {
  const [state, setState] = useState<RequestState>("loading");
  const [error, setError] = useState("");
  const [items, setItems] = useState<PlatformWorkspaceFeedback[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [selectedFeedbackId, setSelectedFeedbackId] = useState<string | null>(null);

  const load = useCallback(async () => {
    setState("loading");
    setError("");
    try {
      const page = await adminApi.listWorkspaceFeedback({ limit: PAGE_SIZE, offset });
      setItems(page.items);
      setTotal(page.total);
      setSelectedFeedbackId((current) => (
        current && page.items.some((item) => item.feedback_id === current)
          ? current
          : page.items[0]?.feedback_id ?? null
      ));
      setState("ready");
    } catch (loadError) {
      setError(adminErrorMessage(loadError));
      setState("error");
    }
  }, [offset]);

  useEffect(() => { void load(); }, [load]);

  const selected = useMemo(
    () => items.find((item) => item.feedback_id === selectedFeedbackId) ?? null,
    [items, selectedFeedbackId],
  );

  return (
    <section className="admin-page-frame admin-page-frame-wide" aria-labelledby="admin-feedback-title">
      <AdminPageHeader
        actions={<button className="button" onClick={() => void load()} type="button"><Icon name="refresh" size={16} />刷新反馈</button>}
        description="查看工作区提交的原始反馈、受保护附件和自动奖励处理状态。此页只读。"
        title="用户反馈"
      />

      <div className="admin-master-detail admin-feedback-layout">
        <section className="admin-list-pane" aria-label="用户反馈列表">
          <div className="admin-list-summary"><span>反馈</span><strong>{numberFormat(total)}</strong></div>
          {state === "loading" && <AdminLoading label="正在读取用户反馈…" />}
          {state === "error" && <AdminError message={error} onRetry={() => void load()} />}
          {state === "ready" && !items.length && <AdminEmpty description="当前还没有已提交的用户反馈。" title="没有反馈" />}
          {state === "ready" && !!items.length && (
            <div className="admin-feedback-list">
              {items.map((item) => {
                const reward = rewardPresentation[item.reward_status];
                return (
                  <button
                    aria-current={selectedFeedbackId === item.feedback_id ? "true" : undefined}
                    className={`admin-feedback-row${selectedFeedbackId === item.feedback_id ? " is-selected" : ""}`}
                    key={item.feedback_id}
                    onClick={() => setSelectedFeedbackId(item.feedback_id)}
                    type="button"
                  >
                    <span className="admin-feedback-row-primary">
                      <strong>{item.organization_name || "未命名工作区"}</strong>
                      <small>{item.submitter_name || "未命名用户"} · {item.submitter_email || "未提供邮箱"}</small>
                    </span>
                    <span className="admin-feedback-row-context">
                      <strong>{readableResponse(item.use_case)}</strong>
                      <small>{item.attachments.length ? `${item.attachments.length} 个附件` : "无附件"}</small>
                    </span>
                    <AdminStatus label={reward.label} status={reward.status} />
                    <Icon name="chevron-right" size={17} />
                  </button>
                );
              })}
            </div>
          )}
          {state === "ready" && !!items.length && (
            <AdminPagination limit={PAGE_SIZE} offset={offset} onChange={setOffset} total={total} />
          )}
        </section>
        <FeedbackDetail item={selected} />
      </div>
    </section>
  );
}
