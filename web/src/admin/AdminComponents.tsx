import type { ReactNode } from "react";
import { Icon } from "../icons";

export type AdminNoticeKind = "success" | "error" | "info";

export function AdminPageHeader({
  title,
  description,
  actions,
}: {
  title: string;
  description: string;
  actions?: ReactNode;
}) {
  return (
    <header className="admin-page-heading">
      <div>
        <p className="admin-breadcrumb">平台管理 / {title}</p>
        <h1>{title}</h1>
        <p>{description}</p>
      </div>
      {actions && <div className="admin-heading-actions">{actions}</div>}
    </header>
  );
}

export function AdminStatus({ status, label }: { status: string; label?: string }) {
  const normalized = status.trim().toLowerCase();
  const tone = ["active", "success", "succeeded", "verified", "completed", "enabled"].includes(normalized)
    ? "success"
    : ["trial", "running", "queued", "progress", "partial"].includes(normalized)
      ? "progress"
      : ["expired", "pending", "retrying", "warning", "expiring"].includes(normalized)
        ? "warning"
        : ["suspended", "failed", "error", "disabled", "inactive", "denied"].includes(normalized)
          ? "error"
          : "neutral";
  const labels: Record<string, string> = {
    active: "正常",
    trial: "试用中",
    expired: "已到期",
    suspended: "已暂停",
    succeeded: "成功",
    success: "成功",
    failed: "失败",
    running: "运行中",
    queued: "排队中",
    retrying: "重试中",
    verified: "已验证",
    pending: "待处理",
    enabled: "已启用",
    disabled: "已禁用",
    inactive: "已停用",
    denied: "已拒绝",
    legacy: "兼容模式",
  };
  return (
    <span className={`admin-status is-${tone}`}>
      <i aria-hidden="true" />
      {label || labels[normalized] || status}
    </span>
  );
}

export function AdminLoading({ label = "正在加载平台数据…" }: { label?: string }) {
  return (
    <div className="admin-state admin-state-loading" aria-live="polite">
      <div className="admin-loading-table" aria-hidden="true">
        {[0, 1, 2, 3].map((item) => <span className="skeleton" key={item} />)}
      </div>
      <p>{label}</p>
    </div>
  );
}

export function AdminError({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <div className="admin-state admin-state-error" role="alert">
      <span className="admin-state-icon"><Icon name="refresh" size={20} /></span>
      <div>
        <h2>数据没有加载完成</h2>
        <p>{message}</p>
      </div>
      <button className="button" onClick={onRetry} type="button">重新加载</button>
    </div>
  );
}

export function AdminEmpty({
  title,
  description,
  action,
}: {
  title: string;
  description: string;
  action?: ReactNode;
}) {
  return (
    <div className="admin-state admin-state-empty">
      <span className="admin-state-icon"><Icon name="folder" size={20} /></span>
      <div>
        <h2>{title}</h2>
        <p>{description}</p>
      </div>
      {action}
    </div>
  );
}

export function AdminPagination({
  offset,
  limit,
  total,
  onChange,
}: {
  offset: number;
  limit: number;
  total: number;
  onChange: (offset: number) => void;
}) {
  const page = Math.floor(offset / limit) + 1;
  const pages = Math.max(1, Math.ceil(total / limit));
  return (
    <div className="admin-pagination" aria-label="分页">
      <span>共 {numberFormat(total)} 项，第 {page} / {pages} 页</span>
      <div>
        <button
          aria-label="上一页"
          className="icon-button"
          disabled={offset <= 0}
          onClick={() => onChange(Math.max(0, offset - limit))}
          type="button"
        ><Icon name="arrow-left" size={17} /></button>
        <button
          aria-label="下一页"
          className="icon-button"
          disabled={offset + limit >= total}
          onClick={() => onChange(offset + limit)}
          type="button"
        ><Icon name="arrow-right" size={17} /></button>
      </div>
    </div>
  );
}

export function formatDate(value: string | null | undefined, includeTime = false) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat("zh-CN", includeTime
    ? { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }
    : { year: "numeric", month: "2-digit", day: "2-digit" }).format(date);
}

export function numberFormat(value: number) {
  return new Intl.NumberFormat("zh-CN").format(value);
}

export function shortId(value: string | null | undefined) {
  if (!value) return "—";
  return value.length > 14 ? `${value.slice(0, 8)}…${value.slice(-4)}` : value;
}
