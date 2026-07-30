import { Icon } from "../../icons";

export interface WorkspaceToastMessage {
  id: number;
  kind: "success" | "error";
  message: string;
}

export function CandidateRequired({
  title,
  description,
  actionLabel,
  onAction,
}: {
  title: string;
  description: string;
  actionLabel?: string;
  onAction?: () => void;
}) {
  return (
    <div className="page-frame">
      <section className="panel">
        <div className="empty-state">
          <div className="empty-state-inner">
            <span className="empty-glyph">
              <Icon name="user" size={23} />
            </span>
            <h2>{title}需要一份当前简历</h2>
            <p>{description}</p>
            {onAction && (
              <button
                className="button button-primary"
                onClick={onAction}
                type="button"
              >
                <Icon name="filter" size={16} />
                {actionLabel ?? "前往条件筛选"}
              </button>
            )}
          </div>
        </div>
      </section>
    </div>
  );
}

export function ToastRegion({
  toasts,
  onDismiss,
}: {
  toasts: WorkspaceToastMessage[];
  onDismiss: (id: number) => void;
}) {
  return (
    <div aria-live="polite" className="toast-region">
      {toasts.map((toast) => (
        <div className={`toast is-${toast.kind}`} key={toast.id} role="status">
          <Icon name={toast.kind === "success" ? "check" : "close"} size={18} />
          <span>{toast.message}</span>
          <button
            aria-label="关闭提示"
            className="icon-button"
            onClick={() => onDismiss(toast.id)}
            type="button"
          >
            <Icon name="close" size={14} />
          </button>
        </div>
      ))}
    </div>
  );
}
