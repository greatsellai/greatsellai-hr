import {
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { IconAILoading } from "@douyinfe/semi-icons";
import { api, isApiError } from "../../api";
import type {
  CandidateSearchItem,
  InstitutionClassification,
  JobVersion,
  RecruitingAgentAction,
  RecruitingAgentCandidate,
  RecruitingAgentConversationTurn,
  RecruitingAgentFilterScopeRequest,
  RecruitingAgentSearchSummary,
  RecruitingAgentToolTrace,
  RecruitingAgentTurn,
  TalentSearchHardFilters,
  TalentSearchProfile,
  TalentSearchProfileMatchResult,
  TalentSearchRun,
} from "../../types";
import { Icon } from "../../icons";
import { degreeLabels, experienceTypeOptions } from "../filter/filter-model";
import {
  AgentCandidateCard,
  AgentMarkdown,
  AgentSearchSummaryPanel,
} from "./AgentMessagePresentation";
import {
  AgentComposer,
  type AgentComposerHandle,
  type AgentReference,
} from "./AgentComposer";
import { useRecruitingAgentConversation } from "./useRecruitingAgentConversation";
import "./recruiting-agent.css";

function humanizeAgentError(
  error: unknown,
  formatError: (error: unknown) => string,
): string {
  if (isApiError(error)) {
    const messages: Record<string, string> = {
      agent_model_not_configured: "招聘 Agent 尚未配置 AI 服务。",
      agent_model_timeout: "招聘 Agent 响应超时，请稍后重试。",
      agent_model_network_error: "招聘 Agent 暂时无法连接 AI 服务，请稍后重试。",
      agent_model_unavailable: "招聘 Agent 所用 AI 服务暂时不可用，请稍后重试。",
      agent_model_request_rejected: "招聘 Agent 当前配置暂时无法处理这类请求，请联系工作区管理员。",
      agent_service_unavailable: "招聘 Agent 暂时不可用，请稍后重试。",
      agent_talent_profile_unavailable: "人才画像暂时无法生成，请稍后重试。",
      agent_model_invalid_response: "招聘 Agent 暂时没有返回有效结果，请重新发送。",
      agent_model_empty_response: "招聘 Agent 暂时没有返回有效结果，请重新发送。",
      agent_model_missing_final_answer: "招聘 Agent 暂时没有完成回答，请重新发送。",
      agent_model_invalid_tool_calls: "招聘 Agent 的工具调用异常，请重新发送。",
      agent_model_tool_loop_limit: "招聘 Agent 本次处理步骤过多，请换一种说法后重试。",
      agent_conversation_stale: "当前工作范围已在另一页面更新，请重新发送这个问题。",
      agent_conversation_not_found: "上次的招聘 Agent 工作范围已失效，请重新发送这个问题。",
      agent_context_reference_not_found: "本次人才画像结果已不可用，请重新开始找人。",
      agent_filter_scope_not_found: "当前初筛范围已失效，请回到筛选结果后重新交给招聘 Agent。",
      agent_filter_scope_expired: "当前初筛范围已失效，请回到筛选结果后重新交给招聘 Agent。",
      agent_filter_scope_invalid: "当前初筛条件暂时无法冻结，请调整后重试。",
      agent_filter_scope_pagination_invalid: "当前初筛结果暂时无法完整读取，请稍后重试。",
      candidate_filter_scope_not_found: "当前初筛范围已失效，请回到筛选结果后重新交给招聘 Agent。",
    };
    if (messages[error.message]) return messages[error.message];
    if (error.message.startsWith("agent_model_http_")) {
      return error.message === "agent_model_http_429"
        ? "招聘 Agent 请求过于频繁，请稍后重试。"
        : "招聘 Agent 暂时不可用，请稍后重试。";
    }
    if (error.status >= 500) {
      return "招聘 Agent 暂时不可用，请稍后重试。";
    }
  }
  if (
    error instanceof Error &&
    /(?:internal[ _-]?server[ _-]?error|internal_server_error)/i.test(
      error.message,
    )
  ) {
    return "招聘 Agent 暂时不可用，请稍后重试。";
  }
  return formatError(error);
}

function isRetryableAgentError(error: unknown): boolean {
  if (!isApiError(error)) return true;
  if (error.status === 408 || error.status === 429) return true;
  return new Set([
    "agent_model_timeout",
    "agent_model_network_error",
    "agent_model_unavailable",
    "agent_service_unavailable",
    "agent_talent_profile_unavailable",
  ]).has(error.message);
}

interface AgentSendSnapshot {
  jobVersionId: string;
}

interface AgentRetry {
  message: string;
  snapshot: AgentSendSnapshot;
}

interface AgentSendOptions {
  clearComposer?: boolean;
  retryFailureId?: AgentChatMessage["id"];
}

interface AgentChatMessage {
  id: number | string;
  role: "assistant" | "user";
  content: string;
  candidates?: RecruitingAgentCandidate[];
  actions?: RecruitingAgentAction[];
  searchSummary?: RecruitingAgentSearchSummary | null;
  talentProfile?: TalentSearchProfile;
  talentRun?: TalentSearchRun;
  /** Safe server-authored summaries of tools completed for this reply. */
  toolTrace?: RecruitingAgentToolTrace[];
  failure?: boolean;
  retry?: AgentRetry;
}

function initialAgentMessages(): AgentChatMessage[] {
  return [
    {
      id: 1,
      role: "assistant",
      content:
        "基于当前授权范围筛选、比较、核验。输入需求，或 @ 引用 JD、筛选范围、人才画像。",
    },
  ];
}

function restoredAgentMessages(
  history: RecruitingAgentConversationTurn[],
): AgentChatMessage[] {
  return history.flatMap((turn) => [
    {
      id: `restored-${turn.context_version}-user`,
      role: "user" as const,
      content: turn.user_message,
    },
    {
      id: `restored-${turn.context_version}-assistant`,
      role: "assistant" as const,
      content: turn.assistant_message,
      toolTrace: turn.tool_trace ?? [],
    },
  ]);
}

function agentContextSourceLabel(
  source: "agent_search" | "candidate_filter" | "candidate" | "talent_search_run" | null,
): string {
  if (source === "agent_search") return "助手筛选结果";
  if (source === "candidate_filter") return "初筛结果";
  if (source === "candidate") return "已引用候选人";
  if (source === "talent_search_run") return "人才画像找人结果";
  return "尚未设置候选范围";
}

/**
 * Deliberately renders only server-verified tool summaries. This is useful
 * recruiter-facing process transparency, not model chain-of-thought, hidden
 * prompts, or raw tool payloads.
 */
function AgentExecutionTrace({ trace }: { trace: RecruitingAgentToolTrace[] }) {
  if (!trace.length) return null;

  return (
    <details className="agent-execution-trace">
      <summary>
        <span>
          <Icon name="history" size={14} />
          本轮处理过程
        </span>
        <small>已完成 {trace.length} 项操作</small>
        <Icon aria-hidden="true" name="chevron-down" size={14} />
      </summary>
      <div className="agent-execution-trace-body">
        <p>仅展示已执行的工具与结果摘要，不包含模型内部推理。</p>
        <ol>
          {trace.map((entry, index) => (
            <li key={`${entry.tool}-${index}`}>
              <strong>{entry.tool}</strong>
              <span>{entry.summary}</span>
            </li>
          ))}
        </ol>
      </div>
    </details>
  );
}

function talentProfileHardFilterLabels(filters: TalentSearchHardFilters): string[] {
  const labels: string[] = [];
  if (filters.institution_classifications_any_of.length) {
    const institutionLabels: Record<InstitutionClassification, string> = {
      "985": "985",
      "211": "211",
      undergraduate: "本科院校",
      associate: "大专院校",
      secondary_vocational: "中专院校",
      overseas: "海外院校",
    };
    labels.push(
      `院校类型：${filters.institution_classifications_any_of
        .map((value) => institutionLabels[value])
        .join(" / ")}（任一）`,
    );
  }
  if (filters.education_degree_in.length) {
    labels.push(
      `教育经历：含${filters.education_degree_in.map((value) => degreeLabels[value]).join(" / ")}（任一）`,
    );
  }
  if (filters.highest_degree_in.length) {
    labels.push(
      `最高学历：${filters.highest_degree_in.map((value) => degreeLabels[value]).join(" / ")}（任一）`,
    );
  }
  if (filters.graduation_status !== "any") {
    const graduationLabel = filters.graduation_status === "fresh" ? "应届" : "往届";
    labels.push(
      `毕业：${graduationLabel}${filters.fresh_graduate_start_month && filters.fresh_graduate_end_month ? ` ${filters.fresh_graduate_start_month} 至 ${filters.fresh_graduate_end_month}` : ""}`,
    );
  }
  if (filters.min_employment_or_internship_months !== null) {
    labels.push(`工作年限不少于 ${Math.round(filters.min_employment_or_internship_months / 12 * 10) / 10} 年`);
  }
  if (filters.experience_types_all_of.length) {
    labels.push(
      `经历：${filters.experience_types_all_of
        .map((value) => experienceTypeOptions.find((item) => item.value === value)?.label || value)
        .join(" + ")}（全部）`,
    );
  }
  if (filters.skills_all_of.length) {
    labels.push(`精确技能：${filters.skills_all_of.join("、")}（全部）`);
  }
  if (filters.language_credentials_all_of.length) {
    labels.push(
      `证书：${filters.language_credentials_all_of
        .map((item) => item.custom_name_contains || item.credential_code.toUpperCase())
        .join("、")}（全部）`,
    );
  }
  return labels;
}

function profileCandidateAsAgentCandidate(item: CandidateSearchItem): RecruitingAgentCandidate {
  const experience = [item.latest_experience_organization, item.latest_experience_title]
    .filter(Boolean)
    .join(" · ");
  return {
    candidate_id: item.candidate_id,
    resume_id: item.resume_id,
    display_name: item.display_name,
    detail: experience || degreeLabels[item.highest_degree ?? "unknown"],
    // A library score and a profile-match score are different measurements.
    // Never show the former as an unlabeled number in the profile workflow.
    score: null,
    verification_status: null,
    verification_evidence: [],
  };
}

function talentProfileLaneLabel(lane: TalentSearchProfileMatchResult["match_lane"]): string {
  if (lane === "recommended") return "证据充分";
  if (lane === "pending") return "待核实";
  return "存在缺口";
}

function talentProfileOutcomeLabel(
  outcome: TalentSearchProfileMatchResult["requirement_results"][number]["outcome"],
): string {
  if (outcome === "met") return "已支持";
  if (outcome === "partial") return "部分支持";
  if (outcome === "unknown") return "待核实";
  return "存在缺口";
}

function TalentProfileMatchCard({
  match,
  onOpen,
}: {
  match: TalentSearchProfileMatchResult;
  onOpen: () => void;
}) {
  const confidence = match.match_confidence === null
    ? "—"
    : `${Math.round(match.match_confidence * 100)}%`;
  const needsVerification = match.requirement_results.filter(
    (item) => item.outcome === "unknown" || item.outcome === "partial",
  );
  return (
    <article className="talent-profile-match-card">
      <div className="talent-profile-match-heading">
        <div>
          <strong>{match.candidate_display_name?.trim() || "未命名候选人"}</strong>
          <small>{talentProfileLaneLabel(match.match_lane)}</small>
        </div>
        <div className="talent-profile-match-metrics" aria-label="画像匹配指标">
          <span><b>{match.match_score.toFixed(1)}</b>匹配度</span>
          <span><b>{confidence}</b>可信度</span>
          <button
            aria-label={`查看${match.candidate_display_name?.trim() || "候选人"}详情`}
            className="icon-button agent-candidate-open"
            onClick={onOpen}
            type="button"
          >
            <Icon name="chevron-right" size={16} />
          </button>
        </div>
      </div>
      {!!match.requirement_results.length && (
        <ul className="talent-profile-match-requirements" aria-label="画像核验依据">
          {match.requirement_results.map((item) => (
            <li key={item.requirement_id}>
              <span className={`is-${item.outcome}`}>{talentProfileOutcomeLabel(item.outcome)}</span>
              <div>
                <strong>{item.requirement_text}</strong>
                <small>{item.reason}</small>
              </div>
            </li>
          ))}
        </ul>
      )}
      {!!needsVerification.length && (
        <p className="talent-profile-match-note">
          待核实：{needsVerification.map((item) => item.requirement_text).join("；")}
        </p>
      )}
    </article>
  );
}

function TalentSearchRunPanel({
  run,
  onOpenCandidate,
  onReferenceCandidate,
  onUseAsAgentContext,
  onRefresh,
  onLoadMore,
  onAdjustConditions,
  loading,
}: {
  run: TalentSearchRun;
  onOpenCandidate: (candidate: RecruitingAgentCandidate) => void;
  onReferenceCandidate?: (candidate: RecruitingAgentCandidate) => void;
  onUseAsAgentContext: () => void;
  onRefresh: () => void;
  onLoadMore: () => void;
  onAdjustConditions: () => void;
  loading: boolean;
}) {
  const isProcessing = run.status === "queued" || run.status === "running";
  const isHardFilterRecall = run.result_mode === "hard_filter_recall";
  const statusLabel = isHardFilterRecall
    ? "硬条件筛选已命中候选人"
    : (run.status === "queued"
      ? "已排队，等待 AI 核验"
      : run.status === "running"
        ? "正在依据简历事实核验"
        : run.status === "partial"
          ? "部分候选人的 AI 核验未完成"
          : "已完成依据简历事实的核验")
  const hasSemanticResults = run.match_results.length > 0;
  const shouldShowRecall = isHardFilterRecall || !hasSemanticResults;
  const appliedHardFilters = talentProfileHardFilterLabels(run.applied_hard_filters);
  const diagnostics = run.recall_diagnostics;
  return (
    <section className="talent-profile-run" aria-label="人才画像找人结果">
      <div className="talent-profile-run-heading">
        <div>
          <strong>{statusLabel}</strong>
          <small>
            严格召回 {run.total_recalled_count} 位候选人
            {isHardFilterRecall
              ? "；本次仅应用了明确的硬条件，无需 AI 语义核验。"
              : run.job_match_batch_id
                ? `；已完成 ${run.match_completed_count}/${run.match_total_count} 位 AI 核验。`
                : "；当前没有候选人进入 AI 核验。"}
          </small>
        </div>
        <div className="talent-profile-run-actions">
          <button
            className="button button-ghost talent-profile-refresh"
            disabled={loading}
            onClick={onRefresh}
            type="button"
          >
            <Icon name="refresh" size={14} />刷新
          </button>
          {!isProcessing && (
            <button
              aria-label="将本次人才画像结果设为助手工作范围"
              className="button button-ghost talent-profile-use-context"
              disabled={loading}
              onClick={onUseAsAgentContext}
              type="button"
            >
              <Icon name="spark" size={14} />在助手中继续
            </button>
          )}
        </div>
      </div>
      {(isProcessing || run.status === "partial") && (
        <p className="talent-profile-run-note">
          待核实不代表不符合，系统不会自动拒绝或录用候选人。
        </p>
      )}
      {hasSemanticResults && (
        <div className="talent-profile-match-list">
          {run.match_results.map((match) => (
            <TalentProfileMatchCard
              key={match.match_id}
              match={match}
              onOpen={() => onOpenCandidate({
                candidate_id: match.candidate_id,
                resume_id: match.resume_id,
                display_name: match.candidate_display_name,
                detail: "AI 人才画像核验结果",
                score: null,
                verification_status: null,
                verification_evidence: [],
              })}
            />
          ))}
        </div>
      )}
      {shouldShowRecall && !!run.candidate_recall.items.length && (
        <div className="agent-candidate-list">
          {run.candidate_recall.items.map((item) => {
            const candidate = profileCandidateAsAgentCandidate(item);
            return (
              <AgentCandidateCard
                candidate={candidate}
                key={candidate.resume_id}
                onOpen={() => onOpenCandidate(candidate)}
                onReference={onReferenceCandidate ? () => onReferenceCandidate(candidate) : undefined}
                referenceDisabled={loading}
              />
            );
          })}
        </div>
      )}
      {!run.candidate_recall.items.length && !isProcessing && !hasSemanticResults && (
        <section className="talent-profile-zero-state" aria-label="零结果说明">
          <strong>
            {diagnostics?.eligible_resume_count === 0
              ? "当前工作区没有可筛选的简历"
              : "没有候选人同时满足本次严格条件"}
          </strong>
          {!!appliedHardFilters.length && (
            <div className="talent-profile-chips" aria-label="本次已应用条件">
              {appliedHardFilters.map((label) => <small key={label}>{label}</small>)}
            </div>
          )}
          {diagnostics && (
            <div className="talent-profile-recall-diagnostics">
              <p>
                可筛选简历 {diagnostics.eligible_resume_count} 份
                {diagnostics.needs_review_count > 0
                  ? `；另有 ${diagnostics.needs_review_count} 份待处理，未计入本次筛选。`
                  : "。"}
              </p>
              {!!diagnostics.steps.length && (
                <ol>
                  {diagnostics.steps.map((step) => (
                    <li key={step.key}>
                      <span>{step.label}</span>
                      <b>筛掉 {step.removed_count}，剩余 {step.remaining_count}</b>
                    </li>
                  ))}
                </ol>
              )}
            </div>
          )}
          <small>
            重点核验和优先项不会作为严格条件排除候选人；缺少简历证据会在后续核验中标为待核实。
          </small>
          <button
            className="button button-ghost talent-profile-adjust"
            disabled={loading}
            onClick={onAdjustConditions}
            type="button"
          >
            调整条件
          </button>
        </section>
      )}
      {!isProcessing && run.status === "partial" && !hasSemanticResults && (
        <p className="talent-profile-run-note">当前未生成可用的 AI 核验结论，请稍后刷新查看失败项。</p>
      )}
      {run.candidate_recall.next_cursor && shouldShowRecall && (
        <button className="button button-ghost talent-profile-load-more" disabled={loading} onClick={onLoadMore} type="button">
          加载更多已召回候选人
        </button>
      )}
    </section>
  );
}

function TalentSearchProfileCard({
  profile,
  run,
  onSupplement,
  onCondense,
  onConfirm,
  onStart,
  onUseAsAgentContext,
  onRefreshRun,
  onLoadMoreRecall,
  onAdjustConditions,
  onOpenCandidate,
  onReferenceCandidate,
  loading,
  startLabel = "开始找人",
}: {
  profile: TalentSearchProfile;
  run?: TalentSearchRun;
  onSupplement: () => void;
  onCondense: () => void;
  onConfirm: () => void;
  onStart: () => void;
  onUseAsAgentContext: () => void;
  onRefreshRun: () => void;
  onLoadMoreRecall: () => void;
  onAdjustConditions: () => void;
  onOpenCandidate: (candidate: RecruitingAgentCandidate) => void;
  onReferenceCandidate?: (candidate: RecruitingAgentCandidate) => void;
  loading: boolean;
  startLabel?: string;
}) {
  const revision = profile.current_revision;
  const hardFilters = talentProfileHardFilterLabels(revision.hard_filters);
  const confirmed = profile.status === "confirmed" && revision.status === "confirmed";
  return (
    <section className="talent-profile-card" aria-label="AI 人才画像">
      <div className="talent-profile-card-heading">
        <div>
          <span>AI 人才画像</span>
          <strong>{revision.title}</strong>
        </div>
        <small className={`talent-profile-status is-${confirmed ? "confirmed" : "draft"}`}>
          {confirmed ? "已确认" : "待确认"}
        </small>
      </div>
      <p className="talent-profile-summary">{revision.summary}</p>
      <div className="talent-profile-meta">
        <span>{profile.source_type === "job" ? "来源：已保存 JD" : "来源：HR 描述"}</span>
        <span>版本 {revision.revision_number}</span>
      </div>
      {!!hardFilters.length && (
        <div className="talent-profile-section">
          <span>硬条件</span>
          <div className="talent-profile-chips">
            {hardFilters.map((label) => <small key={label}>{label}</small>)}
          </div>
          <small className="talent-profile-filter-note">
            院校类型满足其中一项即可；该条件与学历、年限、经历、精确技能等其他硬条件同时生效。
          </small>
        </div>
      )}
      {!!revision.verification_requirements.length && (
        <div className="talent-profile-section">
          <span>重点核验</span>
          <ul className="talent-profile-requirements">
            {revision.verification_requirements.map((item) => (
              <li key={item.key}>
                <strong>{item.label}</strong>
                <small>{item.evidence_hint}</small>
              </li>
            ))}
          </ul>
        </div>
      )}
      {!!revision.preferred_requirements.length && (
        <div className="talent-profile-section">
          <span>优先项</span>
          <ul className="talent-profile-requirements">
            {revision.preferred_requirements.map((item) => (
              <li key={item.key}>
                <strong>{item.label}</strong>
                <small>{item.evidence_hint}</small>
              </li>
            ))}
          </ul>
        </div>
      )}
      {!!revision.aliases.length && (
        <div className="talent-profile-section">
          <span>其他叫法</span>
          <div className="talent-profile-chips is-muted">
            {revision.aliases.map((alias) => <small key={alias}>{alias}</small>)}
          </div>
        </div>
      )}
      {!!revision.clarifying_questions.length && !confirmed && (
        <div className="talent-profile-question">
          <Icon name="spark" size={14} />
          <span>{revision.clarifying_questions.join("；")}</span>
        </div>
      )}
      <div className="talent-profile-actions">
        <button
          className="button button-ghost"
          disabled={loading}
          onClick={onSupplement}
          type="button"
        >
          补充条件
        </button>
        <button
          className="button button-ghost"
          disabled={loading}
          onClick={onCondense}
          type="button"
        >
          <Icon name="refresh" size={14} />精简画像
        </button>
        {confirmed && !run ? (
          <button className="button button-primary" disabled={loading} onClick={onStart} type="button">
            <Icon name="match" size={15} />{startLabel}
          </button>
        ) : !confirmed ? (
          <button className="button button-primary" disabled={loading} onClick={onConfirm} type="button">
            <Icon name="check" size={15} />确认画像
          </button>
        ) : null}
      </div>
      {run && (
        <TalentSearchRunPanel
          loading={loading}
          onOpenCandidate={onOpenCandidate}
          onReferenceCandidate={onReferenceCandidate}
          onUseAsAgentContext={onUseAsAgentContext}
          onLoadMore={onLoadMoreRecall}
          onRefresh={onRefreshRun}
          onAdjustConditions={onAdjustConditions}
          run={run}
        />
      )}
    </section>
  );
}

export function RecruitingAgentPage({
  formatError,
  active,
  conversationStorageScope,
  pendingFilterScope,
  onPendingFilterScopeHandled,
  onOpenMatchWorkspace,
  onOpenScoreWorkspace,
  onOpenMailboxSettings,
  onOpenResume,
}: {
  formatError: (error: unknown) => string;
  /** The route is visible. The component stays mounted to preserve an active chat. */
  active: boolean;
  conversationStorageScope: string | null;
  pendingFilterScope: RecruitingAgentFilterScopeRequest | null;
  onPendingFilterScopeHandled: (requestId: number) => void;
  onOpenMatchWorkspace: () => void;
  onOpenScoreWorkspace: () => void;
  onOpenMailboxSettings: () => void;
  onOpenResume: (candidate: RecruitingAgentCandidate) => void;
}) {
  const [input, setInput] = useState("");
  const [jobs, setJobs] = useState<JobVersion[]>([]);
  const [jobVersionId, setJobVersionId] = useState("");
  const [recentTalentProfiles, setRecentTalentProfiles] = useState<TalentSearchProfile[]>([]);
  const [loading, setLoading] = useState(false);
  const [isGenerating, setIsGenerating] = useState(false);
  const [bindingScopeRequestId, setBindingScopeRequestId] = useState<number | null>(null);
  const [scopeBindingError, setScopeBindingError] = useState<string | null>(null);
  const [referenceError, setReferenceError] = useState<string | null>(null);
  const {
    adoptConversation,
    bindCandidateScope,
    bindFilterScope,
    bindJobVersion,
    bindTalentSearchProfile,
    bindTalentSearchRun,
    buildTurnInput,
    clearContext,
    clearConversation,
    conversation,
    forgetConversation,
    isRestoring,
    restoreConversation,
    restoreError,
  } = useRecruitingAgentConversation({ storageScope: conversationStorageScope });
  const pageHeadingRef = useRef<HTMLHeadingElement | null>(null);
  const composerInputRef = useRef<AgentComposerHandle | null>(null);
  const restoredTalentProfileKeyRef = useRef<string | null>(null);
  const restoredChatHistoryConversationIdRef = useRef<string | null>(null);
  const autoBoundScopeRequestIdRef = useRef<number | null>(null);
  const completedScopeRequestIdRef = useRef<number | null>(null);
  const [messages, setMessages] = useState<AgentChatMessage[]>(initialAgentMessages);
  const isBindingScope = bindingScopeRequestId !== null;
  const interactionPending = loading || isRestoring || isBindingScope;

  useEffect(() => {
    restoredChatHistoryConversationIdRef.current = null;
    restoredTalentProfileKeyRef.current = null;
    autoBoundScopeRequestIdRef.current = null;
    completedScopeRequestIdRef.current = null;
    setMessages(initialAgentMessages());
    setInput("");
    setJobs([]);
    setJobVersionId("");
    setRecentTalentProfiles([]);
    setBindingScopeRequestId(null);
    setScopeBindingError(null);
    setReferenceError(null);
  }, [conversationStorageScope]);

  useEffect(() => {
    if (!active) return;
    void api
      .listConfirmedJobVersions()
      .then((items) => {
        const matchableJobs = items.filter(
          (item) => item.requirements.length > 0,
        );
        setJobs(items);
        setJobVersionId((current) =>
          current &&
          items.some((item) => item.job_version_id === current)
            ? current
            : (matchableJobs[0]?.job_version_id ?? items[0]?.job_version_id ?? ""),
        );
      })
      .catch(() => setJobs([]));
    void api
      .listTalentSearchProfiles()
      .then((response) => setRecentTalentProfiles(response.items))
      .catch(() => setRecentTalentProfiles([]));
  }, [active]);

  useEffect(() => {
    const restoredJobVersionId = conversation?.active_context.active_job_version_id;
    if (
      restoredJobVersionId
      && jobs.some((item) => item.job_version_id === restoredJobVersionId)
    ) {
      setJobVersionId(restoredJobVersionId);
    }
  }, [conversation?.active_context.active_job_version_id, jobs]);

  useEffect(() => {
    if (!active || !conversation) return;
    if (restoredChatHistoryConversationIdRef.current === conversation.conversation_id) {
      return;
    }
    restoredChatHistoryConversationIdRef.current = conversation.conversation_id;
    const restored = conversation.chat_history ?? [];
    if (restored.length) setMessages(restoredAgentMessages(restored));
  }, [
    conversation?.chat_history,
    conversation?.conversation_id,
    active,
  ]);

  useEffect(() => {
    const activeProfile = conversation?.active_context.active_talent_profile;
    if (!active || !activeProfile) {
      if (!activeProfile) restoredTalentProfileKeyRef.current = null;
      return;
    }
    const key = `${activeProfile.profile_id}:${activeProfile.revision_id}`;
    if (restoredTalentProfileKeyRef.current === key) return;
    // A reload restores only the safe opaque conversation reference. Fetch
    // the already-authorized profile again for its readable card, never from
    // browser-stored chat history or a browser-provided profile identifier.
    restoredTalentProfileKeyRef.current = key;
    void api.getTalentSearchProfile(activeProfile.profile_id).then((profile) => {
      if (profile.current_revision.revision_id !== activeProfile.revision_id) {
        restoredTalentProfileKeyRef.current = null;
        return;
      }
      setRecentTalentProfiles((current) => [
        profile,
        ...current.filter((item) => item.profile_id !== profile.profile_id),
      ].slice(0, 12));
      setMessages((current) => (
        current.some((item) => (
          item.talentProfile?.profile_id === profile.profile_id
          && item.talentProfile.current_revision.revision_id === profile.current_revision.revision_id
        ))
          ? current
          : [
            ...current,
            {
              id: Date.now() + 1,
              role: "assistant",
              content: profile.status === "confirmed"
                ? "已恢复当前已确认的人才画像。需要开始找人时，请点击“开始找人”。"
                : "已恢复当前人才画像草案。可直接补充条件，或确认后开始找人。",
              talentProfile: profile,
            },
          ]
      ));
    }).catch(() => {
      restoredTalentProfileKeyRef.current = null;
    });
  }, [
    conversation?.active_context.active_talent_profile?.profile_id,
    conversation?.active_context.active_talent_profile?.revision_id,
    active,
  ]);

  useEffect(() => {
    if (!active) return;
    const frame = window.requestAnimationFrame(() => pageHeadingRef.current?.focus());
    return () => window.cancelAnimationFrame(frame);
  }, [active]);

  const addAssistantReply = (turn: RecruitingAgentTurn) => {
    // This response is already rendered locally with cards/actions. Avoid
    // replacing it with the text-only transcript that is returned for reload.
    restoredChatHistoryConversationIdRef.current = turn.conversation_id;
    adoptConversation(turn);
    if (turn.talent_profile) {
      restoredTalentProfileKeyRef.current = `${turn.talent_profile.profile_id}:${turn.talent_profile.current_revision.revision_id}`;
      setRecentTalentProfiles((current) => [
        turn.talent_profile!,
        ...current.filter((item) => item.profile_id !== turn.talent_profile!.profile_id),
      ].slice(0, 12));
    }
    setMessages((current) => [
      ...current,
      {
        id: Date.now() + 1,
        role: "assistant",
        content: turn.message,
        candidates: turn.candidates,
        actions: turn.actions,
        searchSummary: turn.search_summary,
        talentProfile: turn.talent_profile ?? undefined,
        toolTrace: turn.tool_trace,
      },
    ]);
    if (turn.job_version_id) setJobVersionId(turn.job_version_id);
  };

  const updateTalentProfileMessage = (
    profile: TalentSearchProfile,
    run?: TalentSearchRun,
  ) => {
    setMessages((current) => current.map((item) => (
      item.talentProfile?.profile_id === profile.profile_id
        && item.talentProfile.current_revision.revision_id === profile.current_revision.revision_id
        ? { ...item, talentProfile: profile, talentRun: run }
        : item
    )));
  };

  const appendTalentProfileReply = (profile: TalentSearchProfile, content: string) => {
    restoredTalentProfileKeyRef.current = `${profile.profile_id}:${profile.current_revision.revision_id}`;
    setMessages((current) => [
      ...current,
      {
        id: Date.now() + 1,
        role: "assistant",
        content,
        talentProfile: profile,
      },
    ]);
  };

  const rememberTalentProfile = (profile: TalentSearchProfile) => {
    setRecentTalentProfiles((current) => [
      profile,
      ...current.filter((item) => item.profile_id !== profile.profile_id),
    ].slice(0, 12));
  };

  const addTalentProfileFailure = (error: unknown) => {
    setMessages((current) => [
      ...current,
      {
        id: Date.now() + 1,
        role: "assistant",
        content: humanizeAgentError(error, formatError),
        failure: true,
      },
    ]);
  };

  const focusTalentProfileComposer = () => {
    setInput("");
    window.requestAnimationFrame(() => composerInputRef.current?.focus());
  };

  const condenseTalentProfile = async (profile: TalentSearchProfile) => {
    if (interactionPending) return;
    setLoading(true);
    try {
      const next = await api.refineTalentSearchProfile(profile.profile_id, {
        revision_id: profile.current_revision.revision_id,
        message: "请精简当前人才画像：保留原始招聘目标和已明确硬条件；合并重复内容，删除模糊或非必要的要求；不要新增或放宽任何条件；将摘要和核验重点写得更短。",
      });
      await bindTalentSearchProfile({
        profileId: next.profile_id,
        revisionId: next.current_revision.revision_id,
        jobVersionId,
      });
      rememberTalentProfile(next);
      appendTalentProfileReply(
        next,
        `已精简人才画像，已生成待确认的第 ${next.current_revision.revision_number} 版。`,
      );
    } catch (error) {
      addTalentProfileFailure(error);
    } finally {
      setLoading(false);
    }
  };

  const confirmTalentProfile = async (profile: TalentSearchProfile) => {
    if (interactionPending) return;
    setLoading(true);
    try {
      const confirmed = await api.confirmTalentSearchProfile(profile.profile_id, {
        revision_id: profile.current_revision.revision_id,
      });
      await bindTalentSearchProfile({
        profileId: confirmed.profile_id,
        revisionId: confirmed.current_revision.revision_id,
        jobVersionId,
      });
      rememberTalentProfile(confirmed);
      updateTalentProfileMessage(confirmed);
    } catch (error) {
      addTalentProfileFailure(error);
    } finally {
      setLoading(false);
    }
  };

  const startTalentProfileSearch = async (profile: TalentSearchProfile) => {
    if (interactionPending) return;
    setLoading(true);
    try {
      const run = conversation?.active_context.candidate_set_source === "candidate_filter"
        ? await api.startRecruitingAgentScopedTalentProfileRun(profile.profile_id, {
            revision_id: profile.current_revision.revision_id,
            limit: 20,
            conversation_id: conversation.conversation_id,
            context_version: conversation.context_version,
          })
        : await api.startTalentSearchProfileRun(profile.profile_id, {
            revision_id: profile.current_revision.revision_id,
            limit: 20,
          });
      if (run.active_context && run.conversation_id && run.context_version != null) {
        adoptConversation({
          conversation_id: run.conversation_id,
          context_version: run.context_version,
          active_context: run.active_context,
          chat_history: conversation?.chat_history ?? [],
        });
      } else {
        await bindTalentSearchRun({
          runId: run.run_id,
          jobVersionId: matchableJobVersionId(jobVersionId),
        });
      }
      updateTalentProfileMessage(profile, run);
    } catch (error) {
      addTalentProfileFailure(error);
    } finally {
      setLoading(false);
    }
  };

  const refreshTalentProfileRun = async (profile: TalentSearchProfile, run: TalentSearchRun) => {
    if (interactionPending) return;
    setLoading(true);
    try {
      const refreshed = await api.getTalentSearchProfileRun(profile.profile_id, run.run_id, { limit: 20 });
      updateTalentProfileMessage(profile, refreshed);
    } catch (error) {
      addTalentProfileFailure(error);
    } finally {
      setLoading(false);
    }
  };

  const loadMoreTalentProfileRecall = async (
    profile: TalentSearchProfile,
    run: TalentSearchRun,
  ) => {
    const cursor = run.candidate_recall.next_cursor;
    if (interactionPending || !cursor) return;
    setLoading(true);
    try {
      const next = await api.getTalentSearchProfileRun(profile.profile_id, run.run_id, {
        limit: 20,
        cursor,
      });
      const seen = new Set(run.candidate_recall.items.map((item) => item.resume_id));
      updateTalentProfileMessage(profile, {
        ...next,
        candidate_recall: {
          ...next.candidate_recall,
          items: [
            ...run.candidate_recall.items,
            ...next.candidate_recall.items.filter((item) => !seen.has(item.resume_id)),
          ],
        },
      });
    } catch (error) {
      addTalentProfileFailure(error);
    } finally {
      setLoading(false);
    }
  };

  const resumeTalentProfile = async (profileId: string) => {
    if (interactionPending) return;
    setLoading(true);
    try {
      const profile = await api.getTalentSearchProfile(profileId);
      await bindTalentSearchProfile({
        profileId: profile.profile_id,
        revisionId: profile.current_revision.revision_id,
        jobVersionId,
      });
      rememberTalentProfile(profile);
      appendTalentProfileReply(
        profile,
        profile.status === "confirmed"
          ? "已恢复这份已确认的人才画像。需要开始找人时，请点击“开始找人”。"
          : "已恢复这份人才画像草案。可直接补充条件，或确认后开始找人。",
      );
    } catch (error) {
      addTalentProfileFailure(error);
    } finally {
      setLoading(false);
    }
  };

  const matchableJobVersionId = (selectedJobVersionId: string) => (
    jobs.some(
      (job) => job.job_version_id === selectedJobVersionId && job.requirements.length > 0,
    )
      ? selectedJobVersionId
      : null
  );

  const activeInputReferences = useMemo<AgentReference[]>(() => (
    conversation?.active_context.input_references?.map((reference) => ({
      referenceId: reference.reference_id,
      kind: reference.kind,
      label: reference.label,
    })) ?? []
  ), [conversation?.active_context.input_references]);

  const availableInputReferences = useMemo<AgentReference[]>(() => {
    const activeKinds = new Set(activeInputReferences.map((reference) => reference.kind));
    const options: AgentReference[] = [];
    if (!activeKinds.has("job")) {
      [...jobs]
        .sort((left, right) => (
          Number(right.job_version_id === jobVersionId) - Number(left.job_version_id === jobVersionId)
        ))
        .slice(0, 8)
        .forEach((job) => {
          options.push({
            referenceId: `job:${job.job_version_id}`,
            kind: "job",
            label: job.title,
            description: job.job_version_id === jobVersionId ? "当前选择的 JD" : "关联 JD",
          });
        });
    }
    if (!activeKinds.has("talent_profile")) {
      recentTalentProfiles.slice(0, 8).forEach((profile) => {
        options.push({
          referenceId: `profile:${profile.profile_id}:${profile.current_revision.revision_id}`,
          kind: "talent_profile",
          label: profile.current_revision.title,
          description: profile.status === "confirmed" ? "已确认的人才画像" : "人才画像草案",
        });
      });
    }
    if (!activeKinds.has("candidate")) {
      const seenCandidates = new Set<string>();
      messages.forEach((message) => {
        message.candidates?.forEach((candidate) => {
          if (seenCandidates.has(candidate.candidate_id)) return;
          seenCandidates.add(candidate.candidate_id);
          options.push({
            referenceId: `candidate:${candidate.candidate_id}`,
            kind: "candidate",
            label: candidate.display_name || "候选人",
            description: "来自当前对话结果",
          });
        });
      });
    }
    return options.slice(0, 24);
  }, [
    activeInputReferences,
    jobVersionId,
    jobs,
    messages,
    recentTalentProfiles,
  ]);

  const applyInputReference = async (reference: AgentReference) => {
    if (interactionPending || reference.kind === "filter") return;
    setReferenceError(null);
    setLoading(true);
    try {
      if (reference.kind === "candidate") {
        await bindCandidateScope({
          candidateId: reference.referenceId.slice("candidate:".length),
        });
      } else if (reference.kind === "job") {
        const nextJobVersionId = reference.referenceId.slice("job:".length);
        await bindJobVersion({ jobVersionId: nextJobVersionId });
        setJobVersionId(nextJobVersionId);
      } else {
        const profile = recentTalentProfiles.find((item) => (
          reference.referenceId === `profile:${item.profile_id}:${item.current_revision.revision_id}`
        ));
        if (!profile) return;
        await bindTalentSearchProfile({
          profileId: profile.profile_id,
          revisionId: profile.current_revision.revision_id,
          jobVersionId: matchableJobVersionId(jobVersionId),
        });
      }
      window.requestAnimationFrame(() => composerInputRef.current?.focus());
    } catch (error) {
      const recoveredMessage = await recoverAgentConversationError(error);
      setReferenceError(recoveredMessage ?? humanizeAgentError(error, formatError));
    } finally {
      setLoading(false);
    }
  };

  const applyCandidateReference = (candidate: RecruitingAgentCandidate) => {
    void applyInputReference({
      referenceId: `candidate:${candidate.candidate_id}`,
      kind: "candidate",
      label: candidate.display_name || "候选人",
      description: "来自当前对话结果",
    });
  };

  const removeInputReference = async (referenceId: string) => {
    const reference = activeInputReferences.find((item) => item.referenceId === referenceId);
    if (!reference || interactionPending) return;
    setReferenceError(null);
    setLoading(true);
    try {
      await clearContext(
        reference.kind === "job"
          ? "job"
          : reference.kind === "talent_profile"
            ? "talent_profile"
            : "candidate_scope",
      );
      if (reference.kind === "job") setJobVersionId("");
      window.requestAnimationFrame(() => composerInputRef.current?.focus());
    } catch (error) {
      const recoveredMessage = await recoverAgentConversationError(error);
      setReferenceError(recoveredMessage ?? humanizeAgentError(error, formatError));
    } finally {
      setLoading(false);
    }
  };

  const recoverAgentConversationError = async (error: unknown): Promise<string | null> => {
    if (!isApiError(error)) return null;
    if (error.status === 409 && error.message === "agent_conversation_stale") {
      try {
        restoredChatHistoryConversationIdRef.current = null;
        const restored = await restoreConversation(conversation?.conversation_id);
        return restored
          ? "当前工作范围已在另一页面更新，现已同步。请重新发送这个问题。"
          : "上次的招聘 Agent 工作范围已失效，请重新发送这个问题。";
      } catch {
        return "当前工作范围已在另一页面更新，但暂时无法同步。请稍后重试。";
      }
    }
    if (error.status === 404 && error.message === "agent_conversation_not_found") {
      forgetConversation();
      return "上次的招聘 Agent 工作范围已失效。您下一次提问会创建新的工作范围。";
    }
    return null;
  };

  const bindPendingFilterScope = async (
    scope: RecruitingAgentFilterScopeRequest | null = pendingFilterScope,
  ) => {
    if (!scope || isBindingScope) return;
    setBindingScopeRequestId(scope.request_id);
    setScopeBindingError(null);
    try {
      const bound = await bindFilterScope({
        filter: scope.filter,
        jobVersionId: matchableJobVersionId(jobVersionId),
      });
      completedScopeRequestIdRef.current = scope.request_id;
      onPendingFilterScopeHandled(scope.request_id);
      setMessages((current) => [
        ...current,
        {
          id: Date.now() + 1,
          role: "assistant",
          content: `初筛结果已就绪，共 ${bound.active_context.candidate_count} 位候选人。请描述希望重点核验的人才画像。`,
        },
      ]);
      window.requestAnimationFrame(() => composerInputRef.current?.focus());
    } catch (error) {
      const recoveredMessage = await recoverAgentConversationError(error);
      setScopeBindingError(recoveredMessage ?? humanizeAgentError(error, formatError));
    } finally {
      setBindingScopeRequestId(null);
    }
  };

  useEffect(() => {
    if (
      !active
      || !pendingFilterScope
      || isBindingScope
      || isRestoring
      || autoBoundScopeRequestIdRef.current === pendingFilterScope.request_id
      || completedScopeRequestIdRef.current === pendingFilterScope.request_id
    ) {
      return;
    }
    autoBoundScopeRequestIdRef.current = pendingFilterScope.request_id;
    void bindPendingFilterScope(pendingFilterScope);
  }, [
    isBindingScope,
    active,
    isRestoring,
    pendingFilterScope,
    pendingFilterScope?.request_id,
  ]);

  const useTalentSearchRunAsAgentContext = async (run: TalentSearchRun) => {
    if (interactionPending) return;
    setLoading(true);
    try {
      const bound = await bindTalentSearchRun({
        runId: run.run_id,
        jobVersionId: matchableJobVersionId(jobVersionId),
      });
      setMessages((current) => [
        ...current,
        {
          id: Date.now() + 1,
          role: "assistant",
          content: `已将本次人才画像结果设为当前工作范围（${bound.active_context.candidate_count} 位候选人）。接下来可直接让我在这批候选人中继续比较、排序或核验。`,
        },
      ]);
      window.requestAnimationFrame(() => composerInputRef.current?.focus());
    } catch (error) {
      const recoveredMessage = await recoverAgentConversationError(error);
      setMessages((current) => [
        ...current,
        {
          id: Date.now() + 1,
          role: "assistant",
          content: recoveredMessage ?? humanizeAgentError(error, formatError),
          failure: true,
        },
      ]);
    } finally {
      setLoading(false);
    }
  };

  const clearAgentConversation = async () => {
    if (interactionPending) return;
    setLoading(true);
    try {
      await clearConversation();
      setMessages(initialAgentMessages());
      restoredTalentProfileKeyRef.current = null;
      restoredChatHistoryConversationIdRef.current = null;
      setInput("");
      window.requestAnimationFrame(() => composerInputRef.current?.focus());
    } catch (error) {
      const recoveredMessage = await recoverAgentConversationError(error);
      setMessages((current) => [
        ...current,
        {
          id: Date.now() + 1,
          role: "assistant",
          content: recoveredMessage ?? humanizeAgentError(error, formatError),
          failure: true,
        },
      ]);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (!active || loading) return undefined;
    const pending = messages.find((item) => (
      item.talentProfile
      && item.talentRun
      && (item.talentRun.status === "queued" || item.talentRun.status === "running")
    ));
    if (!pending?.talentProfile || !pending.talentRun) return undefined;
    const timer = window.setTimeout(() => {
      void api.getTalentSearchProfileRun(
        pending.talentProfile!.profile_id,
        pending.talentRun!.run_id,
        { limit: 20 },
      ).then((refreshed) => {
        updateTalentProfileMessage(pending.talentProfile!, refreshed);
      }).catch(() => {
        // A transient poll failure should not flood the recruiter chat. The
        // visible refresh button remains available for an explicit retry.
      });
    }, 4_000);
    return () => window.clearTimeout(timer);
  }, [active, loading, messages]);

  const send = async (
    raw: string,
    snapshot?: AgentSendSnapshot,
    options?: AgentSendOptions,
  ) => {
    const message = raw.trim();
    if (!message || interactionPending) return;
    const request = snapshot ?? {
      jobVersionId,
    };
    if (options?.clearComposer !== false) setInput("");
    if (options?.retryFailureId === undefined) {
      setMessages((current) => [
        ...current,
        { id: Date.now(), role: "user", content: message },
      ]);
    } else {
      // Retrying is another attempt for the same user turn. Replacing the
      // failed reply keeps the conversation readable and prevents a single
      // question from looking like several duplicate recruiter messages.
      setMessages((current) => current.filter((item) => item.id !== options.retryFailureId));
    }
    setLoading(true);
    setIsGenerating(true);
    try {
      // The LangGraph Agent decides whether this is a normal workspace
      // question, a new confirmation-first profile, or a refinement of the
      // server-saved profile. The browser never chooses a profile mode or
      // submits a profile/revision identifier with a free-form message.
      const turn = await api.runRecruitingAgentTurn(buildTurnInput({
        message,
        jobVersionId: request.jobVersionId || null,
      }));
      addAssistantReply(turn);
    } catch (error) {
      const recoveredMessage = await recoverAgentConversationError(error);
      const failureMessage = recoveredMessage ?? humanizeAgentError(error, formatError);
      const canRetryAfterExpiredConversation = isApiError(error)
        && error.status === 404
        && error.message === "agent_conversation_not_found";
      setMessages((current) => [
        ...current,
        {
          id: Date.now() + 1,
          role: "assistant",
          content: failureMessage,
          failure: true,
          retry: !recoveredMessage && isRetryableAgentError(error)
            ? { message, snapshot: request }
            : canRetryAfterExpiredConversation
              ? { message, snapshot: request }
              : undefined,
        },
      ]);
    } finally {
      setIsGenerating(false);
      setLoading(false);
    }
  };

  return (
    <section
      aria-labelledby="recruiting-agent-title"
      className="recruiting-agent-page"
      data-testid="recruiting-agent-page"
    >
      <header className="agent-header">
        <div className="agent-title-wrap">
          <span className="agent-mark"><Icon name="spark" size={17} /></span>
          <div>
            <h1 id="recruiting-agent-title" ref={pageHeadingRef} tabIndex={-1}>招聘 Agent</h1>
          </div>
        </div>
        <div className="agent-header-actions">
          <div className="select-wrap agent-job-select">
            <label className="sr-only" htmlFor="agent-job-version">关联 JD</label>
            <select
              className="select-field"
              disabled={interactionPending}
              id="agent-job-version"
              onChange={(event) => setJobVersionId(event.target.value)}
              value={jobVersionId}
            >
              <option value="">不关联 JD</option>
              {jobs.map((item) => (
                <option key={item.job_version_id} value={item.job_version_id}>
                  {item.title} · v{item.version}{item.requirements.length ? "" : " · 原版"}
                </option>
              ))}
            </select>
            <Icon name="chevron-down" size={15} />
          </div>
          <button
            className="button button-ghost agent-new-conversation"
            disabled={interactionPending || !conversation}
            onClick={() => void clearAgentConversation()}
            type="button"
          >
            <Icon name="refresh" size={15} />
            新对话
          </button>
        </div>
      </header>
      <div className="agent-context">
        <div
          className={`agent-work-context${restoreError || scopeBindingError ? " is-error" : ""}`}
          role="status"
        >
          {isBindingScope ? (
            <span>正在接收初筛结果…</span>
          ) : scopeBindingError ? (
            <>
              <span>{scopeBindingError}</span>
              <button
                className="text-button"
                onClick={() => void bindPendingFilterScope(pendingFilterScope)}
                type="button"
              >
                重新尝试
              </button>
            </>
          ) : isRestoring ? (
            <span>正在恢复上次的助手工作范围…</span>
          ) : restoreError ? (
            <>
              <span>{restoreError}</span>
              <button
                className="text-button"
                disabled={loading}
                onClick={() => {
                  restoredChatHistoryConversationIdRef.current = null;
                  void restoreConversation().catch(() => undefined);
                }}
                type="button"
              >
                重新连接
              </button>
            </>
          ) : conversation ? (
            <div className="agent-work-context-main">
              <span>当前范围</span>
              <strong>
                {agentContextSourceLabel(conversation.active_context.candidate_set_source)}
                {` · ${conversation.active_context.candidate_count} 人`}
              </strong>
              {conversation.active_context.active_job_title && (
                <small>{conversation.active_context.active_job_title}</small>
              )}
              {conversation.active_context.active_talent_profile && (
                <small>{conversation.active_context.active_talent_profile.title}</small>
              )}
            </div>
          ) : (
            <span>描述你想找的人，系统会先整理可确认的条件。</span>
          )}
        </div>
        {!!recentTalentProfiles.length && (
          <details className="agent-profile-history">
            <summary>
              <span>已保存画像</span>
              <small>{recentTalentProfiles.length}</small>
              <Icon name="chevron-down" size={14} />
            </summary>
            <div>
              {recentTalentProfiles.slice(0, 4).map((profile) => (
                <button
                  className="button button-ghost"
                  disabled={interactionPending}
                  key={profile.profile_id}
                  onClick={() => void resumeTalentProfile(profile.profile_id)}
                  type="button"
                >
                  {profile.current_revision.title}
                  <small>{profile.status === "confirmed" ? "已确认" : "草案"}</small>
                </button>
              ))}
            </div>
          </details>
        )}
      </div>
      <div className="agent-conversation" aria-live="polite">
        {messages.map((item) => (
          <div className={`agent-message-row is-${item.role}`} key={item.id}>
            <article className={`agent-message is-${item.role}${item.failure ? " is-error" : ""}`}>
            {item.role === "assistant" ? (
              <AgentMarkdown content={item.content} />
            ) : (
              <p>{item.content}</p>
            )}
            {!!item.toolTrace?.length && <AgentExecutionTrace trace={item.toolTrace} />}
            {item.retry && (
              <div className="agent-retry-row">
                <button
                  className="button button-ghost agent-retry-button"
                  disabled={interactionPending}
                  onClick={() => void send(
                    item.retry!.message,
                    item.retry!.snapshot,
                    { clearComposer: false, retryFailureId: item.id },
                  )}
                  type="button"
                >
                  <Icon name="refresh" size={15} />
                  重新发送
                </button>
              </div>
            )}
            {item.searchSummary && <AgentSearchSummaryPanel summary={item.searchSummary} />}
            {item.talentProfile && (
              <TalentSearchProfileCard
                loading={interactionPending}
                onConfirm={() => void confirmTalentProfile(item.talentProfile!)}
                onOpenCandidate={onOpenResume}
                onReferenceCandidate={applyCandidateReference}
                onLoadMoreRecall={() => {
                  if (item.talentRun) {
                    void loadMoreTalentProfileRecall(item.talentProfile!, item.talentRun);
                  }
                }}
                onRefreshRun={() => {
                  if (item.talentRun) {
                    void refreshTalentProfileRun(item.talentProfile!, item.talentRun);
                  }
                }}
                onCondense={() => void condenseTalentProfile(item.talentProfile!)}
                onStart={() => void startTalentProfileSearch(item.talentProfile!)}
                onUseAsAgentContext={() => {
                  if (item.talentRun) {
                    void useTalentSearchRunAsAgentContext(item.talentRun);
                  }
                }}
                onSupplement={focusTalentProfileComposer}
                onAdjustConditions={focusTalentProfileComposer}
                profile={item.talentProfile}
                run={item.talentRun}
                startLabel={
                  conversation?.active_context.candidate_set_source === "candidate_filter"
                    ? `在当前 ${conversation.active_context.candidate_count} 人中精筛`
                    : undefined
                }
              />
            )}
            {!!item.candidates?.length && (
              <div className="agent-candidate-list">
                {item.candidates.map((candidate) => (
                  <AgentCandidateCard
                    key={candidate.resume_id}
                    candidate={candidate}
                    onOpen={() => onOpenResume(candidate)}
                    onReference={() => applyCandidateReference(candidate)}
                    referenceDisabled={interactionPending}
                  />
                ))}
              </div>
            )}
            {item.actions?.some((action) => action.action === "open_match_workspace") && (
              <button className="button button-ghost agent-workspace-button" onClick={onOpenMatchWorkspace} type="button">
                <Icon name="match" size={15} />
                打开 JD 匹配工作区
              </button>
            )}
            {item.actions?.some((action) => action.action === "open_score_workspace") && (
              <button className="button button-ghost agent-workspace-button" onClick={onOpenScoreWorkspace} type="button">
                <Icon name="layers" size={15} />
                打开评分工作台
              </button>
            )}
            {item.actions?.some((action) => action.action === "open_mailbox_workspace") && (
              <button className="button button-ghost agent-workspace-button" onClick={onOpenMailboxSettings} type="button">
                <Icon name="inbox" size={15} />
                打开收件邮箱设置
              </button>
            )}
            </article>
          </div>
        ))}
        {isGenerating && (
          <div className="agent-message-row is-assistant is-loading">
            <article
              aria-atomic="true"
              aria-live="polite"
              className="agent-message is-assistant agent-loading"
              data-testid="agent-generating-status"
              role="status"
            >
              <div className="agent-generating-status">
                <IconAILoading aria-hidden="true" size="large" />
                <span>AI 正在生成</span>
              </div>
            </article>
          </div>
        )}
      </div>
      <AgentComposer
        availableReferences={availableInputReferences}
        disabled={interactionPending}
        generating={isGenerating}
        inputRef={composerInputRef}
        onChange={setInput}
        onRemoveReference={(referenceId) => void removeInputReference(referenceId)}
        onSelectReference={(reference) => void applyInputReference(reference)}
        onSubmit={(message) => void send(message)}
        references={activeInputReferences}
        value={input}
      />
      {referenceError && <p className="agent-reference-status" role="status">{referenceError}</p>}
    </section>
  );
}
