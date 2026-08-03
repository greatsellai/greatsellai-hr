import { useEffect, useRef, useState } from "react";
import ReactMarkdown, { defaultUrlTransform } from "react-markdown";
import remarkGfm from "remark-gfm";
import type {
  CandidateResumeVersionPreview,
  FilterOptions,
  AiSummaryStatus,
  ResumeReviewDetail,
  ResumeScore,
  ResumeSummary,
} from "../../types";
import { Icon } from "../../icons";
import { RESUME_EXTRACTION_FAILED_LABEL } from "../../resume-extraction-user-messages";
import { TableSkeleton } from "../../backoffice/ui/TableSkeleton";
import { formatLibraryDate } from "../../backoffice/utils/formatters";
import {
  hasSourceTextQualityIssue,
  hasSupersededReparseVersion,
} from "../../backoffice/utils/resume-source-quality";
import {
  canPreviewInline,
  resumeFileExtension,
  resumeFileTypeLabel,
} from "../../backoffice/utils/resume-file";
import {
  degreeLabels,
  formatDuration,
  institutionClassificationLabel,
} from "../filter/filter-model";
import type {
  CandidateDrawerTab,
  SelectedResume,
} from "./candidate-drawer-types";
import { CandidateContactPanel } from "./CandidateContactPanel";
import { CandidateRecruitingPanel } from "./CandidateRecruitingPanel";
import "./candidate-drawer.css";

export interface CandidateDrawerProps {
  candidate: SelectedResume | null;
  review: ResumeReviewDetail | null;
  reviewLoading: boolean;
  isOpen: boolean;
  drawerTab: CandidateDrawerTab;
  onTabChange: (tab: CandidateDrawerTab) => void;
  onClose: () => void;
  pdfUrl: string | null;
  pdfLoading: boolean;
  pdfDownloadLoading: boolean;
  pdfError: string | null;
  summaries: ResumeSummary[];
  summaryLoading: boolean;
  scores: ResumeScore[];
  languageCredentialOptions: FilterOptions["language_credentials"];
  scoreLoading: boolean;
  scoreError: string | null;
  onGenerateSummary: () => void;
  onCreateManualSummary: (
    summaryId: string,
    content: Record<string, string>,
  ) => Promise<void>;
  onReparseSource: () => void;
  reparsingSource: boolean;
  onEnrichFacts: () => void;
  enrichingFacts: boolean;
  canManageCandidateData: boolean;
  onPreviewOriginal: () => void;
  onDownloadOriginal: () => void;
  onRefreshScores: () => void;
  onDeleteResume: () => Promise<void>;
  formatError: (error: unknown) => string;
  onNotify: (kind: "success" | "error", message: string) => void;
  /** Candidate-level versions are metadata-only, never duplicate AI data. */
  resumeVersions: CandidateResumeVersionPreview[];
  resumeVersionsLoading: boolean;
  onSelectResumeVersion: (resumeId: string) => void;
  favoriteLoading: boolean;
  onToggleFavorite: () => void;
}

export function CandidateDrawer({
  candidate,
  review,
  reviewLoading,
  isOpen,
  drawerTab,
  onTabChange,
  onClose,
  pdfUrl,
  pdfLoading,
  pdfDownloadLoading,
  pdfError,
  summaries,
  summaryLoading,
  scores,
  languageCredentialOptions,
  scoreLoading,
  scoreError,
  onGenerateSummary,
  onCreateManualSummary,
  onReparseSource,
  reparsingSource,
  onEnrichFacts,
  enrichingFacts,
  canManageCandidateData,
  onPreviewOriginal,
  onDownloadOriginal,
  onRefreshScores,
  onDeleteResume,
  formatError,
  onNotify,
  resumeVersions,
  resumeVersionsLoading,
  onSelectResumeVersion,
  favoriteLoading,
  onToggleFavorite,
}: CandidateDrawerProps) {
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const [deleting, setDeleting] = useState(false);
  const currentSummary =
    summaries.find((item) => item.is_current) ?? summaries[0] ?? null;
  const sourceTextIssue = hasSourceTextQualityIssue(review?.quality_flags);
  const supersededReparse = hasSupersededReparseVersion(review?.quality_flags);
  const selectedResumeVersion = candidate
    ? resumeVersions.find((item) => item.resume_id === candidate.resumeId) ?? {
        resume_id: candidate.resumeId,
        original_filename: review?.original_filename ?? "当前简历",
        created_at: "",
        extraction_status: review?.extraction_status ?? "",
        is_active: review?.is_active ?? false,
      }
    : null;
  const availableResumeVersions = selectedResumeVersion
    ? [
        selectedResumeVersion,
        ...resumeVersions.filter(
          (item) => item.resume_id !== selectedResumeVersion.resume_id,
        ),
      ]
    : resumeVersions;
  useEffect(() => {
    if (!isOpen) return;
    const frame = window.requestAnimationFrame(() =>
      closeButtonRef.current?.focus(),
    );
    return () => window.cancelAnimationFrame(frame);
  }, [isOpen]);

  const deleteResume = async () => {
    if (
      !window.confirm(
        "删除当前简历？它会立即从工作台移除，并在恢复期内可恢复。",
      )
    ) {
      return;
    }
    setDeleting(true);
    try {
      await onDeleteResume();
    } catch {
      // The parent reports a user-facing error toast.
    } finally {
      setDeleting(false);
    }
  };

  return (
    <aside
      aria-hidden={!isOpen}
      aria-label={
        candidate ? `${candidate.candidateName} 的简历详情` : "简历详情"
      }
      aria-modal="true"
      className={`candidate-drawer${isOpen ? " is-open" : ""}${sourceTextIssue ? " has-source-quality-notice" : ""}`}
      inert={!isOpen}
      role="dialog"
    >
      <header className="drawer-header">
        <div className="drawer-title-wrap">
          <h2>
            {candidate?.candidateName ?? "候选人详情"}
            {sourceTextIssue ? (
              <span className="tiny-badge is-attention">{RESUME_EXTRACTION_FAILED_LABEL}</span>
            ) : supersededReparse ? (
              <span className="tiny-badge is-attention">当前版本已更新</span>
            ) : review?.is_active ? (
              <span className="tiny-badge">已启用</span>
            ) : null}
          </h2>
          {candidate && (
            <div className="drawer-version-control">
              <label htmlFor="candidate-drawer-resume-version">简历版本</label>
              <select
                aria-busy={resumeVersionsLoading}
                aria-describedby="candidate-drawer-resume-version-note"
                disabled={resumeVersionsLoading || reviewLoading}
                id="candidate-drawer-resume-version"
                onChange={(event) => onSelectResumeVersion(event.target.value)}
                value={candidate.resumeId}
              >
                {availableResumeVersions.map((version) => (
                  <option key={version.resume_id} value={version.resume_id}>
                    {`${version.is_active ? "当前版本 · " : ""}${version.original_filename}${version.created_at ? ` · ${formatLibraryDate(version.created_at)}` : ""}`}
                  </option>
                ))}
              </select>
              <span id="candidate-drawer-resume-version-note">
                {resumeVersionsLoading
                  ? "正在加载版本"
                  : `${availableResumeVersions.length} 个简历版本`}
              </span>
            </div>
          )}
        </div>
        <div className="drawer-actions">
          {candidate && (
            <button
              aria-busy={favoriteLoading}
              aria-label={
                review?.is_favorited
                  ? `取消收藏候选人 ${candidate.candidateName}`
                  : `收藏候选人 ${candidate.candidateName}`
              }
              aria-pressed={review?.is_favorited ?? false}
              className={`button drawer-favorite-button${review?.is_favorited ? " is-favorited" : ""}`}
              disabled={!review || favoriteLoading}
              onClick={onToggleFavorite}
              type="button"
            >
              {favoriteLoading ? (
                <i className="spinner" />
              ) : (
                <Icon name="bookmark" size={16} />
              )}
              {review?.is_favorited ? "取消收藏" : "收藏候选人"}
            </button>
          )}
          {canManageCandidateData && candidate && (
            <button
              aria-busy={deleting}
              className="button button-danger-ghost resume-delete-button"
              disabled={deleting}
              onClick={() => void deleteResume()}
              type="button"
            >
              {deleting ? <><i className="spinner" />正在删除</> : "删除简历"}
            </button>
          )}
          <button
            aria-label="关闭简历详情"
            className="icon-button"
            onClick={onClose}
            ref={closeButtonRef}
            type="button"
          >
            <Icon name="close" size={19} />
          </button>
        </div>
      </header>
      {sourceTextIssue && (
        <SourceTextQualityNotice
          busy={reparsingSource}
          onReparse={onReparseSource}
        />
      )}
      {supersededReparse && !sourceTextIssue && <SupersededReparseNotice />}
      <div className="drawer-body">
        <div className="drawer-navigation">
          {review?.contacts.length ? (
            <CandidateContactPanel contacts={review.contacts} onNotify={onNotify} />
          ) : null}
          <div aria-label="详情标签" className="tabs" role="tablist">
            {(
              [
                ["original", "原始文件"],
                ["summary", "AI 总结"],
                ["score", "评分详情"],
                ["evidence", "提取依据"],
                ["applications", "应聘记录"],
              ] as Array<[CandidateDrawerTab, string]>
            ).map(([tab, label]) => (
              <button
                aria-controls={`candidate-drawer-panel-${tab}`}
                aria-selected={drawerTab === tab}
                className={`tab${drawerTab === tab ? " is-active" : ""}`}
                id={`candidate-drawer-tab-${tab}`}
                key={tab}
                onClick={() => onTabChange(tab)}
                role="tab"
                type="button"
              >
                {label}
              </button>
            ))}
          </div>
        </div>
        <div
          aria-labelledby={`candidate-drawer-tab-${drawerTab}`}
          className="drawer-content"
          id={`candidate-drawer-panel-${drawerTab}`}
          role="tabpanel"
        >
          {drawerTab === "applications" ? (
            <CandidateRecruitingPanel
              candidateId={candidate?.candidateId ?? null}
              formatError={formatError}
              notify={onNotify}
            />
          ) : reviewLoading && !review ? (
            <TableSkeleton />
          ) : drawerTab === "original" ? (
            <OriginalDocumentTab
              error={pdfError}
              loading={pdfLoading}
              downloadLoading={pdfDownloadLoading}
              onDownload={onDownloadOriginal}
              onPreview={onPreviewOriginal}
              pdfUrl={pdfUrl}
              review={review}
            />
          ) : drawerTab === "summary" ? (
            sourceTextIssue ? (
              <SourceTextQualityBlockedSummary
                busy={reparsingSource}
                onOpenEvidence={() => onTabChange("evidence")}
                onReparse={onReparseSource}
              />
            ) : supersededReparse ? (
              <SupersededReparseBlockedSummary
                onOpenEvidence={() => onTabChange("evidence")}
              />
            ) : (
              <DrawerSummary
                currentSummary={currentSummary}
                loading={summaryLoading}
                onCreateManual={onCreateManualSummary}
                onRetry={onGenerateSummary}
                onOpenEvidence={() => onTabChange("evidence")}
                summaryError={review?.ai_summary_error ?? null}
                summaryStatus={review?.ai_summary_status ?? null}
                summaries={summaries}
              />
            )
          ) : drawerTab === "score" ? (
            sourceTextIssue ? (
              <ScoreDetailsUnavailable
                busy={reparsingSource}
                onOpenEvidence={() => onTabChange("evidence")}
                onReparse={onReparseSource}
                reason={`${RESUME_EXTRACTION_FAILED_LABEL}。当前版本不会展示评分结论，请重新解析原件后重试。`}
              />
            ) : supersededReparse ? (
              <ScoreDetailsUnavailable
                onOpenEvidence={() => onTabChange("evidence")}
                reason="候选人已有更新版本。为避免旧解析结果影响判断，请从当前版本查看评分。"
              />
            ) : (
              <CandidateScoreDetails
                error={scoreError}
                loading={scoreLoading}
                onRefresh={onRefreshScores}
                scores={scores}
              />
            )
          ) : (
            <EvidenceTab
              enriching={enrichingFacts}
              languageCredentialOptions={languageCredentialOptions}
              loading={reviewLoading}
              onEnrich={onEnrichFacts}
              review={review}
            />
          )}
        </div>
      </div>
    </aside>
  );
}

function scoreEvidenceCoverage(score: ResumeScore): number | null {
  const weightedDimensions = score.dimension_scores.filter(
    (dimension) => dimension.weight > 0,
  );
  const totalWeight = weightedDimensions.reduce(
    (total, dimension) => total + dimension.weight,
    0,
  );
  if (!totalWeight) return null;
  const groundedWeight = weightedDimensions
    .filter((dimension) => dimension.evidence_state === "grounded")
    .reduce((total, dimension) => total + dimension.weight, 0);
  return Math.round((groundedWeight / totalWeight) * 100);
}

function scoreRecordLabel(score: ResumeScore): string | null {
  if (!score.is_current_facts_version) return "版本待更新";
  if (score.status === "overridden") return "含人工调整";
  if (score.status === "needs_review") return "建议复核";
  return null;
}

function CandidateScoreDetails({
  scores,
  loading,
  error,
  onRefresh,
}: {
  scores: ResumeScore[];
  loading: boolean;
  error: string | null;
  onRefresh: () => void;
}) {
  const score = scores.find(
    (item) => item.is_current_facts_version && item.is_current_template_version,
  ) ?? scores.find((item) => item.is_current_facts_version) ?? scores[0] ?? null;
  const evidenceCoverage = score ? scoreEvidenceCoverage(score) : null;
  const scoreStatus = score ? scoreRecordLabel(score) : null;

  if (loading && !scores.length) {
    return <div className="drawer-score-details"><TableSkeleton /></div>;
  }
  if (error) {
    return (
      <div className="empty-state drawer-score-empty">
        <div className="empty-state-inner">
          <span className="empty-glyph"><Icon name="refresh" size={23} /></span>
          <h2>暂时无法读取评分详情</h2>
          <p>{error}</p>
          <button className="button" onClick={onRefresh} type="button">
            <Icon name="refresh" size={16} />重新加载
          </button>
        </div>
      </div>
    );
  }
  if (!score) {
    return (
      <div className="empty-state drawer-score-empty">
        <div className="empty-state-inner">
          <span className="empty-glyph"><Icon name="layers" size={23} /></span>
          <h2>尚未生成评分</h2>
          <p>请先在评分模板中批量生成通用评分。生成后，这里会展示每项得分、理由和简历依据。</p>
        </div>
      </div>
    );
  }

  return (
    <section aria-label="综合评分详情" className="drawer-score-details">
      <header className="drawer-score-heading">
        <div>
          <h3
            title={`${score.template_name ?? "评分模板"} · 模板 v${score.template_version} · ${formatLibraryDate(score.created_at)}`}
          >
            {score.template_name ?? "评分详情"}
          </h3>
        </div>
        {scoreStatus && (
          <span
            className={`score-record-status is-${score.status}${!score.is_current_facts_version ? " is-stale" : ""}`}
          >
            {scoreStatus}
          </span>
        )}
      </header>

      <div className="drawer-score-overview">
        <div className="drawer-score-total">
          <span>综合评分</span>
          <strong>{score.total_score.toFixed(1)}<small>/ 100</small></strong>
        </div>
        <dl className="drawer-score-metrics">
          <div>
            <dt>事实覆盖</dt>
            <dd>{evidenceCoverage === null ? "待核实" : `${evidenceCoverage}%`}</dd>
          </div>
        </dl>
      </div>

      <div className="drawer-score-dimensions">
        {score.dimension_scores.map((dimension) => {
          const manuallyAdjusted =
            dimension.manual_reason !== null ||
            dimension.final_raw_score !== dimension.ai_raw_score;
          return (
            <article className="drawer-score-dimension" key={dimension.key}>
              <div className="drawer-score-dimension-heading">
                <div>
                  <h4>{dimension.label}</h4>
                  <span className="drawer-score-dimension-score">
                    {dimension.final_raw_score.toFixed(0)} / 100
                  </span>
                </div>
                <div className="drawer-score-contribution">
                  <span>对总分贡献</span>
                  <strong>{dimension.final_weighted_score.toFixed(1)} 分</strong>
                </div>
              </div>
              <div className="drawer-score-dimension-meta">
                <span>权重 {dimension.weight}%</span>
                {dimension.evidence_state !== "grounded" && (
                  <span
                    className={`score-evidence-state is-${dimension.evidence_state}`}
                  >
                    证据不足
                  </span>
                )}
                {manuallyAdjusted && (
                  <span className="score-manual-mark">
                    人工调整，原始分 {dimension.ai_raw_score.toFixed(0)}
                  </span>
                )}
              </div>

              <div className="drawer-score-section">
                <span>AI 判断</span>
                <p>{dimension.rationale || "信息不足，未提供可验证判断依据。"}</p>
              </div>
              <div className="drawer-score-section">
                <span>简历事实</span>
                {dimension.fact_evidence.length ? (
                  <ul className="drawer-score-facts">
                    {dimension.fact_evidence.map((fact) => (
                      <li key={fact.fact_id}>{fact.summary}</li>
                    ))}
                  </ul>
                ) : (
                  <p>当前维度没有足够的已验证简历依据。</p>
                )}
              </div>
              {dimension.uncertainties.length > 0 && (
                <div className="drawer-score-section is-uncertain">
                  <span>待确认项</span>
                  <ul className="drawer-score-facts">
                    {dimension.uncertainties.map((item) => <li key={item}>{item}</li>)}
                  </ul>
                </div>
              )}
              {dimension.manual_reason && (
                <div className="drawer-score-section is-manual">
                  <span>人工调整原因</span>
                  <p>{dimension.manual_reason}</p>
                </div>
              )}
            </article>
          );
        })}
      </div>

      {score.analysis.overall_summary && (
        <section className="drawer-score-analysis">
          <h4>AI 综合判断</h4>
          <p>{score.analysis.overall_summary}</p>
        </section>
      )}
      {score.analysis.risk_flags.length > 0 && (
        <section className="drawer-score-risks">
          <h4>待关注项</h4>
          <ul>
            {score.analysis.risk_flags.map((risk, index) => (
              <li key={`${risk.message}-${index}`}>
                <span>{risk.message}</span>
                {risk.fact_evidence.length > 0 && (
                  <small>依据：{risk.fact_evidence.map((fact) => fact.summary).join("；")}</small>
                )}
              </li>
            ))}
          </ul>
        </section>
      )}
    </section>
  );
}

function ScoreDetailsUnavailable({
  reason,
  busy = false,
  onOpenEvidence,
  onReparse,
}: {
  reason: string;
  busy?: boolean;
  onOpenEvidence: () => void;
  onReparse?: () => void;
}) {
  return (
    <div className="empty-state source-quality-blocked-summary">
      <div className="empty-state-inner">
        <span className="empty-glyph"><Icon name="layers" size={23} /></span>
        <h2>评分详情暂不可用</h2>
        <p>{reason}</p>
        <div className="source-quality-summary-actions">
          {onReparse && (
            <button
              className="button button-primary"
              disabled={busy}
              onClick={onReparse}
              type="button"
            >
              {busy ? <><i className="spinner" />正在创建</> : <><Icon name="refresh" size={16} />重新解析为新版本</>}
            </button>
          )}
          <button className="button button-ghost" onClick={onOpenEvidence} type="button">
            查看提取依据
          </button>
        </div>
      </div>
    </div>
  );
}

function SourceTextQualityNotice({
  busy,
  onReparse,
}: {
  busy: boolean;
  onReparse: () => void;
}) {
  return (
    <section className="source-quality-notice" role="alert">
      <span aria-hidden="true" className="source-quality-notice-icon">
        <Icon name="document" size={18} />
      </span>
      <div className="source-quality-notice-copy">
        <strong>{RESUME_EXTRACTION_FAILED_LABEL}</strong>
        <p>
          系统未能从当前原件中提取可用信息。请重新解析原件后重试，旧版本会保留供追溯。
        </p>
      </div>
      <button
        className="button source-quality-reparse-button"
        disabled={busy}
        onClick={onReparse}
        type="button"
      >
        {busy ? (
          <>
            <i className="spinner" />正在创建
          </>
        ) : (
          <>
            <Icon name="refresh" size={15} />重新解析为新版本
          </>
        )}
      </button>
    </section>
  );
}

function SourceTextQualityBlockedSummary({
  busy,
  onOpenEvidence,
  onReparse,
}: {
  busy: boolean;
  onOpenEvidence: () => void;
  onReparse: () => void;
}) {
  return (
    <div className="empty-state source-quality-blocked-summary">
      <div className="empty-state-inner">
        <span className="empty-glyph">
          <Icon name="document" size={23} />
        </span>
        <h2>{RESUME_EXTRACTION_FAILED_LABEL}</h2>
        <p>
          当前版本不会展示 AI 结论。请重新解析原件后重试。
        </p>
        <div className="source-quality-summary-actions">
          <button
            className="button button-primary"
            disabled={busy}
            onClick={onReparse}
            type="button"
          >
            {busy ? (
              <>
                <i className="spinner" />正在创建
              </>
            ) : (
              <>
                <Icon name="refresh" size={16} />重新解析为新版本
              </>
            )}
          </button>
          <button className="button button-ghost" onClick={onOpenEvidence} type="button">
            查看提取依据
          </button>
        </div>
      </div>
    </div>
  );
}

function SupersededReparseNotice() {
  return (
    <section className="source-quality-notice source-quality-notice-stale" role="status">
      <span aria-hidden="true" className="source-quality-notice-icon">
        <Icon name="history" size={18} />
      </span>
      <div className="source-quality-notice-copy">
        <strong>当前版本已更新</strong>
        <p>
          这份重新解析版本完成前，候选人已有更新版本。系统没有让旧任务覆盖当前版本，请从候选人的当前版本继续处理。
        </p>
      </div>
    </section>
  );
}

function SupersededReparseBlockedSummary({
  onOpenEvidence,
}: {
  onOpenEvidence: () => void;
}) {
  return (
    <div className="empty-state source-quality-blocked-summary">
      <div className="empty-state-inner">
        <span className="empty-glyph">
          <Icon name="history" size={23} />
        </span>
        <h2>此解析版本未启用</h2>
        <p>
          候选人已有更新版本。为避免旧解析结果覆盖当前版本，本版本的 AI 结论不会在这里展示。
        </p>
        <div className="source-quality-summary-actions">
          <button className="button button-ghost" onClick={onOpenEvidence} type="button">
            查看提取依据
          </button>
        </div>
      </div>
    </div>
  );
}

function OriginalDocumentTab({
  review,
  pdfUrl,
  loading,
  downloadLoading,
  error,
  onPreview,
  onDownload,
}: {
  review: ResumeReviewDetail | null;
  pdfUrl: string | null;
  loading: boolean;
  downloadLoading: boolean;
  error: string | null;
  onPreview: () => void;
  onDownload: () => void;
}) {
  const filename = review?.original_filename ?? "";
  const canPreview = canPreviewInline(filename);
  const isImage = [".png", ".jpg", ".jpeg"].includes(
    resumeFileExtension(filename),
  );
  return (
    <div className="pdf-viewer">
      <section className="original-file-access" aria-label="原文件访问">
        <div>
          <strong>原文件访问</strong>
          <p>打开此标签时会自动加载一次预览，并写入工作区访问审计。</p>
        </div>
        <div className="original-file-access-actions">
          {canPreview && (
            <button
              className="button button-primary"
              disabled={loading || !review}
              onClick={onPreview}
              type="button"
            >
              {loading ? (
                <><i className="spinner" />正在加载</>
              ) : (
                <><Icon name="document" size={16} />重新加载预览</>
              )}
            </button>
          )}
          <button
            className="button"
            disabled={downloadLoading || !review}
            onClick={onDownload}
            type="button"
          >
            {downloadLoading ? (
              <><i className="spinner" />正在准备</>
            ) : (
              <><Icon name="download" size={16} />下载原文件</>
            )}
          </button>
        </div>
      </section>
      <div className="pdf-canvas">
        {loading ? (
          <div className="pdf-loading">
            <span className="loading-line">
              <i className="spinner" />
              正在载入受保护的原始文件…
            </span>
          </div>
        ) : error ? (
          <div className="empty-state">
            <div className="empty-state-inner">
              <span className="empty-glyph">
                <Icon name="document" size={23} />
              </span>
              <h2>无法载入原始文件</h2>
              <p>{error}</p>
            </div>
          </div>
        ) : pdfUrl && canPreview ? (
          isImage ? (
            <img
              alt={filename ? `${filename} 原始图片` : "原始图片"}
              className="original-image-preview"
              src={pdfUrl}
            />
          ) : (
            <iframe
              sandbox={resumeFileExtension(filename) === ".html" || resumeFileExtension(filename) === ".htm" ? "" : undefined}
              src={pdfUrl}
              title={filename ? `${filename} 原始文件` : "原始文件"}
            />
          )
        ) : !canPreview && review ? (
          <div className="empty-state">
            <div className="empty-state-inner">
              <span className="empty-glyph"><Icon name="document" size={23} /></span>
              <h2>{resumeFileTypeLabel(filename)} 原件仅支持下载</h2>
              <p>浏览器不能安全预览此格式，请使用上方“下载原文件”查看。</p>
            </div>
          </div>
        ) : (
          <div className="empty-state original-file-idle">
            <div className="empty-state-inner">
              <span className="empty-glyph"><Icon name="document" size={23} /></span>
              <h2>正在准备原文件预览</h2>
              <p>本次预览会自动加载；如加载失败，可使用上方“重新加载预览”。关闭或切换简历后，本地预览会自动释放。</p>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function DrawerSummary({
  currentSummary,
  summaries,
  loading,
  onRetry,
  onCreateManual,
  onOpenEvidence,
  summaryError,
  summaryStatus,
}: {
  currentSummary: ResumeSummary | null;
  summaries: ResumeSummary[];
  loading: boolean;
  onRetry: () => void;
  onCreateManual: (
    summaryId: string,
    content: Record<string, string>,
  ) => Promise<void>;
  onOpenEvidence: () => void;
  summaryError: string | null;
  summaryStatus: AiSummaryStatus;
}) {
  const [selectedSummaryId, setSelectedSummaryId] = useState("");
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const selectedSummary =
    summaries.find((item) => item.summary_id === selectedSummaryId) ??
    currentSummary;

  useEffect(() => {
    if (!currentSummary) {
      setSelectedSummaryId("");
      setEditing(false);
      setDraft({});
      return;
    }
    setSelectedSummaryId(currentSummary.summary_id);
    setEditing(false);
    setDraft(summaryContentToDraft(currentSummary.content));
  }, [currentSummary?.summary_id]);

  if (loading) return <TableSkeleton />;
  if (!currentSummary) {
    const generationInProgress =
      summaryStatus === "queued" ||
      summaryStatus === "running";
    const summaryReadyToLoad = summaryStatus === "succeeded";
    const retryable =
      summaryStatus === "failed" || summaryStatus === "unavailable";
    return (
      <div className="empty-state">
        <div className="empty-state-inner">
          <span className="empty-glyph">
            {generationInProgress || summaryReadyToLoad
              ? <i className="spinner" />
              : <Icon name="spark" size={23} />}
          </span>
          <h2>
            {generationInProgress
              ? "AI 总结生成中"
              : summaryReadyToLoad
                ? "AI 总结正在加载"
              : retryable
                ? summaryStatus === "unavailable"
                  ? "AI 总结暂时不可用"
                  : "AI 总结暂未生成"
                : "等待 AI 自动生成总结"}
          </h2>
          <p>
            {generationInProgress
              ? "系统会在候选人信息提取完成后自动生成，并在完成后显示在这里。"
              : summaryReadyToLoad
                ? "AI 已完成生成，正在同步可展示的总结内容。"
              : retryable
                ? summaryError?.trim() || "本次自动生成未完成，你可以重新尝试。"
                : "系统会在候选人信息提取完成后自动生成，无需手动操作。"}
          </p>
          {retryable && (
            <button
              className="button button-primary"
              onClick={onRetry}
              type="button"
            >
              <Icon name="refresh" size={16} />
              重试生成
            </button>
          )}
        </div>
      </div>
    );
  }
  return (
    <div className="detail-summary">
      <div className="panel-heading">
        <div>
          <h2>{selectedSummary?.is_current ? "当前总结" : "历史总结"}</h2>
          <p>
            {selectedSummary?.source === "manual" ? "人工版本" : "AI 版本"} ·
            生成于 {selectedSummary ? formatLibraryDate(selectedSummary.created_at) : "—"}
          </p>
        </div>
        <div className="drawer-summary-actions">
          <button className="button" onClick={onRetry} type="button">
            <Icon name="refresh" size={15} />
            重新生成
          </button>
          <button
            className="button button-ghost"
            onClick={() => {
              setDraft(summaryContentToDraft(selectedSummary?.content ?? {}));
              setEditing((current) => !current);
            }}
            type="button"
          >
            {editing ? "取消编辑" : "人工编辑"}
          </button>
        </div>
      </div>
      {summaries.length > 1 && (
        <div className="summary-history-control">
          <label className="field-label" htmlFor="summary-history">
            总结版本
          </label>
          <div className="select-wrap">
            <select
              className="select-field"
              id="summary-history"
              onChange={(event) => {
                const next = summaries.find(
                  (item) => item.summary_id === event.target.value,
                );
                if (!next) return;
                setSelectedSummaryId(next.summary_id);
                setDraft(summaryContentToDraft(next.content));
                setEditing(false);
              }}
              value={selectedSummary?.summary_id ?? ""}
            >
              {summaries.map((item) => (
                <option key={item.summary_id} value={item.summary_id}>
                  {item.is_current ? "当前 · " : "历史 · "}
                  {item.source === "manual" ? "人工" : "AI"} · {formatLibraryDate(item.created_at)}
                </option>
              ))}
            </select>
            <Icon name="chevron-down" size={16} />
          </div>
        </div>
      )}
      {editing && selectedSummary ? (
        <form
          className="summary-editor"
          onSubmit={(event) => {
            event.preventDefault();
            const content = Object.fromEntries(
              Object.entries(draft).filter(([, value]) => value.trim()),
            );
            if (!Object.keys(content).length) return;
            setSaving(true);
            void onCreateManual(selectedSummary.summary_id, content)
              .then(() => setEditing(false))
              .catch(() => undefined)
              .finally(() => setSaving(false));
          }}
        >
          {summarySectionOrder.map((key) => (
            <label className="field-stack" key={key}>
              <span className="field-label">{summarySectionLabels[key]}</span>
              <textarea
                className="textarea-field summary-editor-textarea"
                onChange={(event) =>
                  setDraft((current) => ({
                    ...current,
                    [key]: event.target.value,
                  }))
                }
                value={draft[key] ?? ""}
              />
            </label>
          ))}
          <div className="review-actions">
            <button
              className="button button-primary"
              disabled={saving}
              type="submit"
            >
              {saving ? <><i className="spinner" />正在保存</> : <><Icon name="check" size={16} />保存人工版本</>}
            </button>
          </div>
        </form>
      ) : selectedSummary ? (
        <SummaryContent
          content={selectedSummary.content}
          onOpenEvidence={onOpenEvidence}
        />
      ) : null}
    </div>
  );
}

const summarySectionLabels: Record<string, string> = {
  candidate_positioning: "候选人定位",
  education_background: "教育背景",
  work_and_internship: "工作与实习",
  core_skills: "核心技能",
  representative_projects: "代表项目",
  strengths: "优势亮点",
  verification_items: "建议核验",
};

const summarySectionOrder = Object.keys(summarySectionLabels);

function summaryContentToDraft(content: Record<string, unknown>): Record<string, string> {
  const sections = summarySections(content);
  return Object.fromEntries(
    summarySectionOrder.map((key) => [
      key,
      sections.find((section) => section.key === key)?.rendered ?? "",
    ]),
  );
}

function summaryFactIds(value: unknown): string[] {
  if (!value || typeof value !== "object" || Array.isArray(value)) return [];
  const rawFactIds = (value as Record<string, unknown>).fact_ids;
  return Array.isArray(rawFactIds)
    ? rawFactIds.filter((item): item is string => typeof item === "string")
    : [];
}

function summarySections(content: Record<string, unknown>) {
  const source =
    content.sections &&
    typeof content.sections === "object" &&
    !Array.isArray(content.sections)
      ? (content.sections as Record<string, unknown>)
      : content;
  return Object.entries(source)
    .filter(([key]) => key !== "schema_version")
    .flatMap(([key, value]) => {
      const rendered =
        typeof value === "string"
          ? value.trim()
          : value &&
              typeof value === "object" &&
              !Array.isArray(value) &&
              typeof (value as Record<string, unknown>).content === "string"
            ? ((value as Record<string, unknown>).content as string).trim()
            : "";
      return rendered
        ? [
            {
              key,
              label: summarySectionLabels[key] ?? key.replace(/_/g, " "),
              rendered,
              factIds: summaryFactIds(value),
            },
          ]
        : [];
    });
}

function SummaryContent({
  content,
  onOpenEvidence,
}: {
  content: Record<string, unknown>;
  onOpenEvidence?: () => void;
}) {
  const entries = summarySections(content);
  return (
    <article className="summary-card">
      {entries.length ? (
        <dl>
          {entries.flatMap((section) => [
            <dt key={`${section.key}-dt`}>{section.label}</dt>,
            <dd key={`${section.key}-dd`}>
              <p>{section.rendered}</p>
              {section.factIds.length > 0 && (
                <button
                  className="summary-evidence-link"
                  onClick={onOpenEvidence}
                  type="button"
                >
                  依据 {section.factIds.join("、")}
                </button>
              )}
            </dd>,
          ])}
        </dl>
      ) : (
        <p className="candidate-meta">AI 没有返回可展示的总结内容。</p>
      )}
    </article>
  );
}

function evidenceBlockLabel(ids: string[]): string {
  return ids.length ? `原文依据：${ids.join("、")}` : "未标注原文依据";
}

function EvidenceTab({
  review,
  loading,
  onEnrich,
  enriching,
  languageCredentialOptions,
}: {
  review: ResumeReviewDetail | null;
  loading: boolean;
  onEnrich: () => void;
  enriching: boolean;
  languageCredentialOptions: FilterOptions["language_credentials"];
}) {
  if (loading) return <TableSkeleton />;
  if (!review) {
    return (
      <div className="empty-state">
        <div className="empty-state-inner">
          <span className="empty-glyph"><Icon name="document" size={23} /></span>
          <h2>暂时无法读取提取依据</h2>
          <p>请稍后重新打开这份简历。</p>
        </div>
      </div>
    );
  }
  return (
    <div className="detail-review">
      <section className="content-section">
        <div className="panel-heading">
          <div>
            <h3>已提取的简历事实</h3>
            <p>历史简历可按需补充英语、成绩、奖项等 V2 事实。</p>
          </div>
          {review.is_active && (
            <button className="button" disabled={enriching} onClick={onEnrich} type="button">
              {enriching ? <><i className="spinner" />正在提交</> : "补充高级筛选事实"}
            </button>
          )}
        </div>
        <div className="detail-grid">
          <div className="fact-list">
            <div className="fact-row">
              <strong>教育经历</strong>
              {review.education.length ? review.education.map((item, index) => (
                <span key={`${item.school_name_raw}-${index}`}>
                  {item.school_name_raw} · {degreeLabels[item.degree]}
                  {item.major_raw ? ` · ${item.major_raw}` : ""}
                  {item.institution_classification
                    ? ` · ${institutionClassificationLabel(item.institution_classification)}`
                    : ""}
                  {item.gpa_percent != null ? ` · GPA ${item.gpa_percent.toFixed(1)}%` : ""}
                  {item.rank_percent != null ? ` · 排名前 ${item.rank_percent.toFixed(1)}%` : ""}
                  {` · ${evidenceBlockLabel(item.evidence_block_ids)}`}
                </span>
              )) : <span>未提取到可验证教育经历</span>}
            </div>
            <div className="fact-row">
              <strong>核心技能</strong>
              {review.skills.length ? review.skills.map((item, index) => (
                <span key={`${item.skill_display}-${index}`}>
                  {item.skill_display} · {evidenceBlockLabel(item.evidence_block_ids)}
                </span>
              )) : <span>未提取到可验证技能</span>}
            </div>
            <div className="fact-row">
              <strong>英语能力</strong>
              {review.language_credentials.length ? review.language_credentials.map((item, index) => (
                <span key={`${item.credential_code}-${index}`}>
                  {languageCredentialOptions.find(
                    (option) => option.value === item.credential_code,
                  )?.label ?? item.credential_name_raw}
                  {item.score != null ? ` · ${item.score}` : ""}
                  {` · ${evidenceBlockLabel(item.evidence_block_ids)}`}
                </span>
              )) : <span>未提取到明确英语证书记录</span>}
            </div>
            <div className="fact-row">
              <strong>奖学金</strong>
              {review.scholarships.length ? review.scholarships.map((item, index) => (
                <span key={`${item.scholarship_name_raw}-${index}`}>
                  {item.scholarship_name_raw} · {evidenceBlockLabel(item.evidence_block_ids)}
                </span>
              )) : <span>未提取到明确奖学金记录</span>}
            </div>
          </div>
          <div className="fact-list">
            <div className="fact-row">
              <strong>事实版本</strong>
              <span>v{review.facts_version}，仅当前版本用于筛选、评分与匹配。</span>
            </div>
            <div className="fact-row">
              <strong>年限统计</strong>
              <span>
                工作年限 {formatDuration(review.employment_or_internship_months)}。
              </span>
            </div>
          </div>
        </div>
      </section>
      <section className="content-section">
        <h3>经历与职责</h3>
        <div className="fact-list">
          {review.experiences.length ? review.experiences.map((item, index) => (
            <div className="fact-row fact-row-experience" key={`${item.experience_name_raw ?? item.title_raw ?? "experience"}-${index}`}>
              <strong>
                {item.organization_name_raw || item.experience_name_raw || "未命名经历"}
                {item.title_raw ? ` · ${item.title_raw}` : ""}
              </strong>
              <span>{item.experience_type} · {evidenceBlockLabel(item.evidence_block_ids)}</span>
              {(item.leadership_role || item.award_result_raw) && (
                <span>
                  {item.leadership_role ? `管理角色：${item.leadership_role}` : ""}
                  {item.leadership_role && item.award_result_raw ? " · " : ""}
                  {item.award_result_raw ? `获奖：${item.award_result_raw}` : ""}
                </span>
              )}
              {item.detail_items.length > 0 && (
                <ul className="fact-row-detail-list">
                  {item.detail_items.map((detail, detailIndex) => (
                    <li key={`${detail.detail_raw}-${detailIndex}`}>
                      <span>{detail.detail_raw}</span>
                      <small>{evidenceBlockLabel(detail.evidence_block_ids)}</small>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )) : <span className="candidate-meta">未提取到可验证经历。</span>}
        </div>
      </section>
      <section className="evidence-panel">
        <h3>原文证据块</h3>
        {review.source_blocks.map((block) => (
          <div className="evidence-item" key={block.block_id}>
            <b>{block.block_id} · 第 {block.page_no} 页</b>
            {block.text}
          </div>
        ))}
      </section>
    </div>
  );
}
