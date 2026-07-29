import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ChangeEvent,
  type FormEvent,
} from "react";
import { api } from "../../api";
import { Icon } from "../../icons";
import type {
  WorkspaceFeedback,
  WorkspaceFeedbackHistory,
} from "../../types";
import "./workspace-feedback.css";

type LoadState = "loading" | "ready" | "error";

const MAX_ATTACHMENTS = 5;

function formatDateTime(value: string | null): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function cooldownMessage(nextSubmissionAt: string | null): string | null {
  if (!nextSubmissionAt) return null;
  const next = new Date(nextSubmissionAt);
  if (Number.isNaN(next.getTime()) || next.getTime() <= Date.now()) return null;
  return `下一次可提交意见的时间：${formatDateTime(nextSubmissionAt)}`;
}

function rewardStatusCopy(item: WorkspaceFeedback): { label: string; tone: string } {
  switch (item.reward_status) {
    case "granted":
      return {
        label: `已到账，当前工作区已增加 ${item.reward_call_count} 次 AI 调用`,
        tone: "is-granted",
      };
    case "running":
      return { label: "系统审核通过，正在发放当前工作区额度", tone: "is-pending" };
    default:
      return { label: `系统审核中，审核通过后发放 ${item.reward_call_count} 次 AI 调用额度`, tone: "is-pending" };
  }
}

function createIdempotencyKey(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `feedback-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function AttachmentChooser({
  files,
  onChange,
  onRemove,
}: {
  files: File[];
  onChange: (event: ChangeEvent<HTMLInputElement>) => void;
  onRemove: (index: number) => void;
}) {
  const inputRef = useRef<HTMLInputElement | null>(null);
  return (
    <div className="feedback-attachment-field">
      <div className="feedback-attachment-heading">
        <div>
          <label className="field-label" htmlFor="feedback-attachments">补充截图或手写说明图（可选）</label>
          <p className="field-help">支持 PNG、JPG、WebP，最多 {MAX_ATTACHMENTS} 张。</p>
        </div>
        <button
          className="button button-ghost feedback-attachment-button"
          disabled={files.length >= MAX_ATTACHMENTS}
          onClick={() => inputRef.current?.click()}
          type="button"
        >
          <Icon name="upload" size={16} /> 添加图片
        </button>
      </div>
      <input
        accept="image/png,image/jpeg,image/webp,.png,.jpg,.jpeg,.webp"
        aria-label="上传反馈截图或手写说明图"
        className="feedback-file-input"
        id="feedback-attachments"
        multiple
        onChange={onChange}
        ref={inputRef}
        type="file"
      />
      {files.length > 0 && (
        <ul className="feedback-selected-attachments" aria-label="已选择的反馈图片">
          {files.map((file, index) => (
            <li key={`${file.name}-${file.size}-${index}`}>
              <Icon name="document" size={16} />
              <span title={file.name}>{file.name}</span>
              <small>{Math.max(1, Math.ceil(file.size / 1024))} KB</small>
              <button aria-label={`移除 ${file.name}`} onClick={() => onRemove(index)} type="button">
                <Icon name="close" size={14} />
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function FeedbackHistory({ items }: { items: WorkspaceFeedback[] }) {
  if (!items.length) {
    return (
      <div className="feedback-empty-history">
        <Icon name="history" size={19} />
        <div>
          <strong>还没有提交记录</strong>
          <p>第一次反馈提交后，奖励状态会显示在这里。</p>
        </div>
      </div>
    );
  }

  return (
    <ol className="feedback-history-list">
      {items.map((item) => {
        const status = rewardStatusCopy(item);
        return (
          <li key={item.feedback_id}>
            <header>
              <div>
                <strong>使用体验反馈</strong>
                <time dateTime={item.created_at}>{formatDateTime(item.created_at)}</time>
              </div>
              <span className={`feedback-reward-status ${status.tone}`}>{status.label}</span>
            </header>
            <details>
              <summary>查看填写内容</summary>
              <dl>
                <div><dt>使用场景</dt><dd>{item.use_case}</dd></div>
                <div><dt>原本想完成什么</dt><dd>{item.intended_outcome}</dd></div>
                <div><dt>最不顺的地方</dt><dd>{item.friction}</dd></div>
                <div><dt>希望怎样改</dt><dd>{item.desired_change}</dd></div>
              </dl>
              {item.attachments.length > 0 && (
                <div className="feedback-history-attachments">
                  <span>补充图片</span>
                  {item.attachments.map((attachment) => (
                    <a
                      href={api.workspaceFeedbackAttachmentUrl(item.feedback_id, attachment.attachment_id)}
                      key={attachment.attachment_id}
                      rel="noreferrer"
                      target="_blank"
                    >
                      <Icon name="document" size={14} /> {attachment.original_filename}
                    </a>
                  ))}
                </div>
              )}
            </details>
          </li>
        );
      })}
    </ol>
  );
}

export function WorkspaceFeedbackPage({
  formatError,
  notify,
  onRewardGranted,
}: {
  formatError: (error: unknown) => string;
  notify: (kind: "success" | "error", message: string) => void;
  onRewardGranted: () => void;
}) {
  const [history, setHistory] = useState<WorkspaceFeedbackHistory>({
    items: [],
    next_submission_at: null,
  });
  const [loadState, setLoadState] = useState<LoadState>("loading");
  const [loadError, setLoadError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [useCase, setUseCase] = useState("");
  const [intendedOutcome, setIntendedOutcome] = useState("");
  const [friction, setFriction] = useState("");
  const [desiredChange, setDesiredChange] = useState("");
  const [contactPhone, setContactPhone] = useState("");
  const [attachments, setAttachments] = useState<File[]>([]);
  const idempotencyKeyRef = useRef<string | null>(null);
  const seenGrantedIdsRef = useRef<Set<string>>(new Set());

  const load = useCallback(async () => {
    setLoadState("loading");
    setLoadError("");
    try {
      const next = await api.listWorkspaceFeedback();
      setHistory(next);
      setLoadState("ready");
      const newlyGranted = next.items.filter(
        (item) => item.reward_status === "granted" && !seenGrantedIdsRef.current.has(item.feedback_id),
      );
      newlyGranted.forEach((item) => seenGrantedIdsRef.current.add(item.feedback_id));
      if (newlyGranted.length > 0) {
        onRewardGranted();
      }
    } catch (error) {
      setLoadError(formatError(error));
      setLoadState("error");
    }
  }, [formatError, onRewardGranted]);

  useEffect(() => { void load(); }, [load]);

  useEffect(() => {
    const refresh = () => {
      if (document.visibilityState === "visible") void load();
    };
    const intervalId = window.setInterval(refresh, 60_000);
    window.addEventListener("focus", refresh);
    return () => {
      window.clearInterval(intervalId);
      window.removeEventListener("focus", refresh);
    };
  }, [load]);

  const cooldown = useMemo(
    () => cooldownMessage(history.next_submission_at),
    [history.next_submission_at],
  );

  const handleAttachmentChange = (event: ChangeEvent<HTMLInputElement>) => {
    const selected = Array.from(event.target.files ?? []);
    if (!selected.length) return;
    setAttachments((current) => [...current, ...selected].slice(0, MAX_ATTACHMENTS));
    event.target.value = "";
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (submitting || cooldown) return;
    const idempotencyKey = idempotencyKeyRef.current ?? createIdempotencyKey();
    idempotencyKeyRef.current = idempotencyKey;
    setSubmitting(true);
    try {
      const next = await api.submitWorkspaceFeedback({
        use_case: useCase.trim(),
        intended_outcome: intendedOutcome.trim(),
        friction: friction.trim(),
        desired_change: desiredChange.trim(),
        contact_phone: contactPhone.trim(),
        attachments,
        idempotency_key: idempotencyKey,
      });
      setHistory(next);
      setUseCase("");
      setIntendedOutcome("");
      setFriction("");
      setDesiredChange("");
      setContactPhone("");
      setAttachments([]);
      idempotencyKeyRef.current = null;
      notify("success", "意见已提交，系统审核通过后将向当前工作区发放 500 次 AI 调用额度。");
    } catch (error) {
      notify("error", formatError(error));
      void load();
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <section className="page-frame feedback-page" aria-labelledby="feedback-page-title">
      <header className="page-heading feedback-page-heading">
        <div>
          <h1 id="feedback-page-title">提交宝贵意见</h1>
          <p>写下真实使用体验。系统审核通过后，当前工作区将获得 500 次 AI 调用额度。</p>
        </div>
        <span className="feedback-reward-note">每 8 小时可提交一次</span>
      </header>

      <div className="feedback-page-layout">
        <form className="panel feedback-form" onSubmit={submit}>
          <div className="feedback-form-heading">
            <div>
              <h2>这一次的使用体验</h2>
              <p>四个问题都请填写。每次聚焦一个具体场景或问题，会更方便我们理解。</p>
            </div>
          </div>
          {cooldown && <p className="feedback-cooldown" role="status"><Icon name="history" size={16} />{cooldown}</p>}
          <div className="feedback-question-list">
            <label>
              <span className="field-label">联系电话</span>
              <input
                autoComplete="tel"
                className="field"
                inputMode="tel"
                maxLength={32}
                onChange={(event) => setContactPhone(event.target.value)}
                placeholder="例如：138 0013 8000"
                required
                type="tel"
                value={contactPhone}
              />
              <span className="field-hint">仅供平台管理员在必要时跟进意见使用，不会在当前工作区公开。</span>
            </label>
            <label>
              <span className="field-label">你这次主要怎样使用 GreatSell AI？</span>
              <textarea className="textarea-field" maxLength={2000} onChange={(event) => setUseCase(event.target.value)} placeholder="例如：上传一批简历后，用筛选工作台找有 Agent 项目经验的候选人。" required rows={3} value={useCase} />
            </label>
            <label>
              <span className="field-label">你当时想完成什么？</span>
              <textarea className="textarea-field" maxLength={2000} onChange={(event) => setIntendedOutcome(event.target.value)} placeholder="描述你期望看到的结果或下一步动作。" required rows={3} value={intendedOutcome} />
            </label>
            <label>
              <span className="field-label">使用过程中哪里最不顺？</span>
              <textarea className="textarea-field" maxLength={2000} onChange={(event) => setFriction(event.target.value)} placeholder="请写下具体卡住、看不懂、结果不符合预期或操作麻烦的地方。" required rows={4} value={friction} />
            </label>
            <label>
              <span className="field-label">你希望我们具体怎样改？</span>
              <textarea className="textarea-field" maxLength={2000} onChange={(event) => setDesiredChange(event.target.value)} placeholder="可以描述你理想中的流程、页面或结果。" required rows={4} value={desiredChange} />
            </label>
          </div>
          <AttachmentChooser
            files={attachments}
            onChange={handleAttachmentChange}
            onRemove={(index) => setAttachments((current) => current.filter((_, currentIndex) => currentIndex !== index))}
          />
          <footer className="feedback-form-footer">
            <p>额度属于当前工作区，所有成员共享使用。</p>
            <button className="button button-primary" disabled={submitting || Boolean(cooldown)} type="submit">
              {submitting ? <><i className="spinner" />正在提交</> : <><Icon name="document" size={16} />提交意见</>}
            </button>
          </footer>
        </form>

        <aside className="panel feedback-history-panel" aria-labelledby="feedback-history-title">
          <header>
            <div>
              <h2 id="feedback-history-title">我的提交记录</h2>
              <p>奖励状态由服务端自动更新。</p>
            </div>
            <button aria-label="刷新问卷记录" className="icon-button" disabled={loadState === "loading"} onClick={() => void load()} type="button"><Icon name="refresh" size={16} /></button>
          </header>
          {loadState === "loading" && <div className="feedback-history-loading"><i className="spinner" />正在读取记录…</div>}
          {loadState === "error" && <div className="feedback-history-error" role="alert"><p>{loadError}</p><button className="button" onClick={() => void load()} type="button">重新加载</button></div>}
          {loadState === "ready" && <FeedbackHistory items={history.items} />}
        </aside>
      </div>
    </section>
  );
}
