import { useCallback, useEffect, useState } from "react";
import { api } from "../../api";
import { Icon } from "../../icons";
import { BackofficeButton } from "../../backoffice/ui/BackofficeButton";
import { BackofficeSelect } from "../../backoffice/ui/BackofficeSelect";
import { TableSkeleton } from "../../backoffice/ui/TableSkeleton";
import {
  AI_STATUS_POLL_INTERVAL_MS,
  aiExtractionIsInProgress,
  aiSummaryIsInProgress,
} from "../../backoffice/utils/ai-extraction";
import { formatLibraryDate } from "../../backoffice/utils/formatters";
import {
  hasSourceTextQualityIssue,
  hasSupersededReparseVersion,
} from "../../backoffice/utils/resume-source-quality";
import type {
  MailboxConfig,
  ResumeLibraryItem,
  ResumeLibraryResponse,
} from "../../types";
import "./resume-library.css";

const RESUME_LIBRARY_PAGE_SIZE = 50;

interface ResumeLibraryPageProps {
  formatError: (error: unknown) => string;
  onOpenResume: (item: ResumeLibraryItem) => void;
  onUpload: () => void;
  refreshToken: number;
  selectedResumeId: string | null;
}

function resumeLibraryStatus(item: ResumeLibraryItem): {
  label: string;
  tone: "ready" | "progress" | "attention" | "waiting";
} {
  if (hasSourceTextQualityIssue(item.quality_flags)) {
    return { label: "文本待校正", tone: "attention" };
  }
  if (hasSupersededReparseVersion(item.quality_flags)) {
    return { label: "当前版本已更新", tone: "attention" };
  }
  if (item.ai_extraction_status === "running") {
    return { label: "AI 提取中", tone: "progress" };
  }
  if (item.ai_extraction_status === "queued") {
    return { label: "等待 AI 提取", tone: "waiting" };
  }
  if (
    item.ai_extraction_status === "needs_attention" ||
    item.extraction_status === "failed"
  ) {
    return { label: "需要处理", tone: "attention" };
  }
  if (item.ai_extraction_status === "unavailable") {
    return { label: "等待 AI 服务", tone: "attention" };
  }
  if (item.is_active && item.extraction_status === "ready") {
    if (aiSummaryIsInProgress(item.ai_summary_status)) {
      return { label: "AI 总结生成中", tone: "progress" };
    }
    if (item.ai_summary_status === "failed") {
      return { label: "总结待重试", tone: "attention" };
    }
    if (item.ai_summary_status === "unavailable") {
      return { label: "总结暂不可用", tone: "attention" };
    }
    return { label: "已启用", tone: "ready" };
  }
  return { label: "等待启用", tone: "waiting" };
}

function summaryStatusLabel(item: ResumeLibraryItem): string {
  if (aiSummaryIsInProgress(item.ai_summary_status)) {
    return "AI 总结生成中";
  }
  if (item.ai_summary_status === "failed") {
    return "AI 总结生成失败，打开后可重试";
  }
  if (item.ai_summary_status === "unavailable") {
    return "AI 总结暂时不可用，稍后可重试";
  }
  if (item.ai_summary_status === "succeeded") {
    return "AI 总结已生成，正在加载";
  }
  return item.is_active
    ? "等待 AI 自动生成总结"
    : "候选人信息提取完成后自动生成";
}

function resumeLibraryScoreNotice(status: string | null): string | null {
  switch (status) {
    case "overridden":
      return "含人工调整";
    case "needs_review":
      return "建议复核";
    case "succeeded":
      return null;
    default:
      return "评分待更新";
  }
}

export function ResumeLibraryPage({
  formatError,
  selectedResumeId,
  refreshToken,
  onOpenResume,
  onUpload,
}: ResumeLibraryPageProps) {
  const [library, setLibrary] = useState<ResumeLibraryResponse | null>(null);
  const [mailboxSources, setMailboxSources] = useState<MailboxConfig[]>([]);
  const [sourceMailboxId, setSourceMailboxId] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const loadLibrary = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setLibrary(
        await api.listResumeLibrary(
          page,
          RESUME_LIBRARY_PAGE_SIZE,
          sourceMailboxId,
        ),
      );
    } catch (loadError) {
      setError(formatError(loadError));
    } finally {
      setLoading(false);
    }
  }, [formatError, page, sourceMailboxId]);

  useEffect(() => {
    void loadLibrary();
  }, [loadLibrary, refreshToken]);

  useEffect(() => {
    let cancelled = false;
    void api.listMailboxConfigs(true)
      .then((response) => {
        if (!cancelled) setMailboxSources(response.items);
      })
      // The library remains usable when the current plan cannot use mailbox
      // ingestion. In that case there is simply no source-specific filter.
      .catch(() => undefined);
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    if (
      !library?.items.some((item) =>
        aiExtractionIsInProgress(item.ai_extraction_status) ||
        aiSummaryIsInProgress(item.ai_summary_status),
      )
    ) {
      return undefined;
    }
    const interval = window.setInterval(() => {
      void loadLibrary();
    }, AI_STATUS_POLL_INTERVAL_MS);
    return () => window.clearInterval(interval);
  }, [library, loadLibrary]);

  const items = library?.items ?? [];
  const total = library?.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / RESUME_LIBRARY_PAGE_SIZE));
  const canPageBack = page > 1;
  const canPageForward = page < totalPages;
  const pageOverview = items.reduce(
    (summary, item) => {
      const status = resumeLibraryStatus(item);
      if (status.tone !== "ready") {
        summary[status.tone] += 1;
      }
      if (status.tone === "ready" && item.score_total === null) {
        summary.unscored += 1;
      }
      return summary;
    },
    { progress: 0, attention: 0, waiting: 0, unscored: 0 },
  );
  const firstItemIndex = total ? (page - 1) * RESUME_LIBRARY_PAGE_SIZE + 1 : 0;
  const lastItemIndex = Math.min(page * RESUME_LIBRARY_PAGE_SIZE, total);
  const mailboxOptions = [
    { label: "全部来源", value: "" },
    ...mailboxSources.map((mailbox) => ({
      label: mailbox.archived_at
        ? `${mailbox.display_name}（已归档）`
        : mailbox.display_name,
      value: mailbox.mailbox_id,
    })),
  ];

  return (
    <div className="page-frame resume-library-page">
      <header className="page-heading">
        <div>
          <h1>简历库</h1>
        </div>
        <div className="resume-library-actions">
          {mailboxSources.length ? (
            <div className="resume-library-source-filter">
              <label className="sr-only" id="resume-library-source-label">
                按收件通道筛选
              </label>
              <BackofficeSelect
                ariaLabelledBy="resume-library-source-label"
                id="resume-library-source"
                onChange={(mailboxId) => {
                  setPage(1);
                  setSourceMailboxId(mailboxId || null);
                }}
                options={mailboxOptions}
                value={sourceMailboxId ?? ""}
              />
            </div>
          ) : null}
          <BackofficeButton
            disabled={loading}
            icon={loading ? undefined : <Icon name="refresh" size={16} />}
            loading={loading}
            onClick={() => void loadLibrary()}
          >
            刷新
          </BackofficeButton>
          <BackofficeButton
            icon={<Icon name="upload" size={16} />}
            onClick={onUpload}
            tone="primary"
          >
            上传简历
          </BackofficeButton>
        </div>
      </header>

      {library &&
        pageOverview.progress +
          pageOverview.waiting +
          pageOverview.attention +
          pageOverview.unscored >
          0 && (
          <section aria-label="当前页面简历状态" className="library-queue-summary">
            {pageOverview.progress + pageOverview.waiting > 0 && (
              <span className="library-queue-item is-progress">
                处理中 <strong>{pageOverview.progress + pageOverview.waiting}</strong>
              </span>
            )}
            {pageOverview.attention > 0 && (
              <span className="library-queue-item is-attention">
                需处理 <strong>{pageOverview.attention}</strong>
              </span>
            )}
            {pageOverview.unscored > 0 && (
              <span className="library-queue-item">
                待评分 <strong>{pageOverview.unscored}</strong>
              </span>
            )}
          </section>
        )}

      {error && (
        <p className="library-error" role="status">
          {error}
        </p>
      )}

      <section aria-label="简历库列表" className="library-table-frame">
        {loading && !library ? (
          <TableSkeleton />
        ) : items.length ? (
          <div
            aria-label="简历库列表，可横向滚动查看全部字段"
            className="table-scroll"
            role="region"
            tabIndex={0}
          >
            <table className="candidate-table library-table">
              <thead>
                <tr>
                  <th scope="col">候选人</th>
                  <th scope="col">AI 总结</th>
                  <th scope="col">AI 评分</th>
                  <th scope="col">上传时间</th>
                  <th aria-label="查看简历" scope="col" />
                </tr>
              </thead>
              <tbody>
                {items.map((item) => {
                  const status = resumeLibraryStatus(item);
                  const sourceTextIssue = hasSourceTextQualityIssue(
                    item.quality_flags,
                  );
                  const supersededReparse = hasSupersededReparseVersion(
                    item.quality_flags,
                  );
                  const scoreNotice = resumeLibraryScoreNotice(
                    item.score_status,
                  );
                  return (
                    <tr
                      className={[
                        selectedResumeId === item.resume_id ? "is-selected" : "",
                        sourceTextIssue ? "has-source-quality-issue" : "",
                        supersededReparse ? "has-superseded-reparse" : "",
                      ]
                        .filter(Boolean)
                        .join(" ")}
                      key={item.resume_id}
                      onClick={() => onOpenResume(item)}
                    >
                      <td className="library-candidate-cell">
                        <div className="candidate-person">
                          <span className="candidate-name">
                            {item.display_name?.trim() || "未命名候选人"}
                          </span>
                          {status.tone !== "ready" && (
                            <span
                              className={`library-status is-${status.tone}`}
                              title={
                                sourceTextIssue
                                  ? "提取文本疑似乱码，请先重新解析原件。"
                                  : supersededReparse
                                    ? "候选人已有更新版本，此解析版本不会被启用。"
                                    : item.ai_extraction_error ?? undefined
                              }
                            >
                              {status.label}
                            </span>
                          )}
                          {item.source_mailbox_label && (
                            <span className="candidate-meta library-source-label">
                              邮箱 · {item.source_mailbox_label}
                            </span>
                          )}
                        </div>
                      </td>
                      <td className="library-summary-cell">
                        {sourceTextIssue ? (
                          <span className="library-quality-copy">
                            提取文本疑似乱码，暂不展示 AI 总结。
                          </span>
                        ) : supersededReparse ? (
                          <span className="library-quality-copy">
                            此解析版本已过期，不展示旧结论。
                          </span>
                        ) : item.summary_preview ? (
                          <p
                            className="library-summary-preview"
                            title={item.summary_preview}
                          >
                            {item.summary_preview}
                          </p>
                        ) : (
                          <span
                            className={`library-summary-status${
                              item.ai_summary_status === "failed" ||
                              item.ai_summary_status === "unavailable"
                                ? " is-attention"
                                : ""
                            }`}
                            title={item.ai_summary_error ?? undefined}
                          >
                            {aiSummaryIsInProgress(item.ai_summary_status) && (
                              <i aria-hidden="true" className="spinner" />
                            )}
                            {summaryStatusLabel(item)}
                          </span>
                        )}
                      </td>
                      <td>
                        {sourceTextIssue ? (
                          <span className="library-quality-copy">
                            请先打开并重新解析
                          </span>
                        ) : supersededReparse ? (
                          <span className="library-quality-copy">
                            请使用候选人的当前版本
                          </span>
                        ) : item.score_total !== null ? (
                          <div
                            className="library-score"
                            title={item.score_template_name ?? "评分模板"}
                          >
                            <strong>{item.score_total.toFixed(1)}</strong>
                            <span>/ 100</span>
                            {scoreNotice && <small>{scoreNotice}</small>}
                          </div>
                        ) : item.is_active ? (
                          <span className="library-empty-copy">
                            尚无通用评分
                          </span>
                        ) : (
                          <span className="library-empty-copy">
                            完成提取后可评分
                          </span>
                        )}
                      </td>
                      <td>
                        <span className="candidate-meta">
                          {formatLibraryDate(item.created_at)}
                        </span>
                      </td>
                      <td className="library-open-cell">
                        <button
                          aria-label={`查看 ${item.display_name?.trim() || "未命名候选人"} 的简历详情`}
                          className="library-open-affordance"
                          onClick={(event) => {
                            event.stopPropagation();
                            onOpenResume(item);
                          }}
                          type="button"
                        >
                          <Icon name="chevron-right" size={17} />
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="empty-state">
            <div className="empty-state-inner">
              <span className="empty-glyph">
                <Icon name="folder" size={24} />
              </span>
              <h2>简历库还是空的</h2>
              <p>
                上传简历后，它会立即出现在这里；AI 提取、总结和评分会逐步更新。
              </p>
              <BackofficeButton
                icon={<Icon name="upload" size={16} />}
                onClick={onUpload}
                tone="primary"
              >
                上传简历
              </BackofficeButton>
            </div>
          </div>
        )}
      </section>

      <footer className="library-table-footer">
        <span>
          {loading && library ? (
            <span className="loading-line">
              <i className="spinner" />
              正在更新简历库…
            </span>
          ) : (
            total ? `显示第 ${firstItemIndex}–${lastItemIndex} 份，共 ${total} 份` : "共 0 份简历"
          )}
        </span>
        {totalPages > 1 && (
          <div className="pagination">
            <BackofficeButton
              disabled={!canPageBack || loading}
              onClick={() => setPage((current) => current - 1)}
            >
              上一页
            </BackofficeButton>
            <span>
              第 {page} / {totalPages} 页
            </span>
            <BackofficeButton
              disabled={!canPageForward || loading}
              onClick={() => setPage((current) => current + 1)}
            >
              下一页
            </BackofficeButton>
          </div>
        )}
      </footer>
    </div>
  );
}
