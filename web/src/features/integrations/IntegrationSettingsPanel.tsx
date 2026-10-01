import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { BackofficeButton } from "../../backoffice/ui/BackofficeButton";
import { BackofficeInput } from "../../backoffice/ui/BackofficeInput";
import {
  integrationApi,
  integrationErrorMessage,
  isIntegrationApiError,
  type IntegrationActivity,
  type IntegrationAnalysisDraftDetail,
  type IntegrationAnalysisDraftSummary,
  type IntegrationAnalysisPendingDetail,
  type IntegrationAnalysisPendingSummary,
  type IntegrationAudience,
  type IntegrationGrantCreated,
  type IntegrationGrantSummary,
  type IntegrationSettings,
} from "./integration-api";

const SemiBanner = lazy(() => import("@douyinfe/semi-ui-19/lib/es/banner"));
const SemiCard = lazy(() => import("@douyinfe/semi-ui-19/lib/es/card"));
const SemiCheckboxGroup = lazy(() => import("@douyinfe/semi-ui-19/lib/es/checkbox/checkboxGroup"));
const SemiEmpty = lazy(() => import("@douyinfe/semi-ui-19/lib/es/empty"));
const SemiParagraph = lazy(() => import("@douyinfe/semi-ui-19/lib/es/typography/paragraph"));
const SemiSideSheet = lazy(() => import("@douyinfe/semi-ui-19/lib/es/sideSheet"));
const SemiSpace = lazy(() => import("@douyinfe/semi-ui-19/lib/es/space"));
const SemiSwitch = lazy(() => import("@douyinfe/semi-ui-19/lib/es/switch"));
const SemiTabPane = lazy(() => import("@douyinfe/semi-ui-19/lib/es/tabs/TabPane"));
const SemiTable = lazy(() => import("@douyinfe/semi-ui-19/lib/es/table"));
const SemiTabs = lazy(() => import("@douyinfe/semi-ui-19/lib/es/tabs"));
const SemiTag = lazy(() => import("@douyinfe/semi-ui-19/lib/es/tag"));
const SemiTitle = lazy(() => import("@douyinfe/semi-ui-19/lib/es/typography/title"));

const SCOPE_LABELS: Record<string, string> = {
  "candidates:read": "候选人资料（只读）",
  "jobs:read": "职位与 JD（只读）",
  "assessments:read": "评估结果（只读）",
  "evidence:read": "原文证据片段（只读，可能含个人信息）",
  "analyses:read": "我的分析草稿（只读）",
  "analyses:write": "我的分析草稿（保存）",
};
const DEFAULT_READ_SCOPES = ["candidates:read", "jobs:read", "assessments:read"];

interface GrantFormState {
  name: string;
  scopes: string[];
  expiresInDays: string;
}

function initialGrantForm(scopes: string[]): GrantFormState {
  return {
    name: "",
    scopes: DEFAULT_READ_SCOPES.filter((scope) => scopes.includes(scope)),
    expiresInDays: "30",
  };
}

function scopeLabel(scope: string): string {
  return SCOPE_LABELS[scope] || "其他已批准权限";
}

function formatDate(value: string | null): string {
  if (!value) return "从未使用";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { hour12: false });
}

type AnalysisDetailForReview = Pick<
  IntegrationAnalysisPendingDetail,
  "candidates" | "referenced_facts"
>;

/**
 * The server binds each external-AI observation to a candidate UUID.  Never
 * show that UUID as an identity substitute: resolve it to the privacy-safe
 * candidate code that is already present in the review payload instead.
 */
function candidateCodeForObservation(
  detail: AnalysisDetailForReview,
  candidateId: string,
): string {
  return detail.referenced_facts.find((profile) => profile.candidate_id === candidateId)?.candidate_code
    ?? detail.candidates.find((candidate) => candidate.candidate_id === candidateId)?.candidate_code
    ?? "候选人代号不可用";
}

/**
 * The browser contract already returns only selected evidence references. The
 * API type deliberately keeps fact projections compact, so collect these
 * known, non-PII identifiers locally rather than widening an API contract.
 */
function selectedEvidenceReferences(
  profile: IntegrationAnalysisPendingDetail["referenced_facts"][number],
): string[] {
  type EvidenceCarrier = { evidence_source_block_ids?: unknown };
  const evidence = new Set<string>();
  const collect = (value: EvidenceCarrier | null | undefined) => {
    if (!Array.isArray(value?.evidence_source_block_ids)) return;
    value.evidence_source_block_ids.forEach((reference) => {
      if (typeof reference === "string" && reference.trim()) evidence.add(reference);
    });
  };

  collect(profile as typeof profile & EvidenceCarrier);
  const facts = profile.facts as unknown as {
    education: EvidenceCarrier[];
    experiences: Array<EvidenceCarrier & { details?: EvidenceCarrier[] }>;
    skills: EvidenceCarrier[];
    language_credentials?: EvidenceCarrier[];
    scholarships?: EvidenceCarrier[];
  };
  facts.education.forEach(collect);
  facts.experiences.forEach((experience) => {
    collect(experience);
    experience.details?.forEach(collect);
  });
  facts.skills.forEach(collect);
  facts.language_credentials?.forEach(collect);
  facts.scholarships?.forEach(collect);
  return [...evidence].sort();
}

function AnalysisObservations({
  detail,
  emptyText,
  items,
}: {
  detail: AnalysisDetailForReview;
  emptyText: string;
  items: IntegrationAnalysisPendingDetail["inferences"];
}) {
  if (items.length === 0) return <SemiEmpty description={emptyText} />;
  return (
    <SemiSpace spacing="medium" vertical style={{ width: "100%" }}>
      {items.map((item, index) => {
        const candidateCode = candidateCodeForObservation(detail, item.candidate_id);
        return (
          <div data-candidate-code={candidateCode} key={`${item.candidate_id}-${index}`}>
            <SemiSpace spacing="medium" vertical style={{ width: "100%" }}>
              <SemiTag color="blue">候选人代号：{candidateCode}</SemiTag>
              <SemiParagraph style={{ margin: 0, whiteSpace: "pre-wrap" }}>{item.text}</SemiParagraph>
            </SemiSpace>
          </div>
        );
      })}
    </SemiSpace>
  );
}

function cleanExpiresInDays(value: string): number | null {
  if (!/^\d+$/.test(value.trim())) return null;
  const days = Number(value);
  return days >= 1 && days <= 90 ? days : null;
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

function settingsMatchIdentity(settings: IntegrationSettings, identityKey: string): boolean {
  return JSON.stringify([settings.user.id, settings.workspace.organization_id]) === identityKey;
}

function grantStatus(status: IntegrationGrantSummary["status"]) {
  const labels: Record<IntegrationGrantSummary["status"], string> = {
    active: "有效", expired: "已过期", revoked: "已撤销", blocked: "已停用",
  };
  const colors: Record<IntegrationGrantSummary["status"], "green" | "orange" | "grey" | "red"> = {
    active: "green", expired: "orange", revoked: "grey", blocked: "red",
  };
  return <SemiTag color={colors[status]}>{labels[status]}</SemiTag>;
}

function OneTimeToken({ created, onClear }: { created: IntegrationGrantCreated; onClear: () => void }) {
  return (
    <SemiBanner
      description={
        <SemiSpace spacing="medium" vertical style={{ width: "100%" }}>
          <SemiParagraph style={{ margin: 0 }}>
            请立即复制并妥善保存。关闭提示、刷新或离开本页后，令牌正文不会再次显示。
          </SemiParagraph>
          <BackofficeInput aria-label={`${created.grant.name} 的一次性令牌`} readOnly value={created.token} />
          <div><BackofficeButton onClick={onClear}>我已保存，关闭令牌</BackofficeButton></div>
        </SemiSpace>
      }
      title={`${created.grant.name} 已创建`}
      type="success"
    />
  );
}

function GrantTable({
  grants,
  busyId,
  canRotate,
  canRevoke,
  onRotate,
  onRevoke,
}: {
  grants: IntegrationGrantSummary[];
  busyId: string | null;
  canRotate: boolean;
  canRevoke: boolean;
  onRotate: (grant: IntegrationGrantSummary) => void;
  onRevoke: (grant: IntegrationGrantSummary) => void;
}) {
  if (grants.length === 0) return <SemiEmpty description="暂无连接记录。" />;
  const columns = [
    { title: "名称", dataIndex: "name", key: "name" },
    { title: "范围", dataIndex: "scopes", key: "scopes", render: (scopes: string[]) => scopes.map(scopeLabel).join("、") || "未授予" },
    {
      title: "连接方式", key: "kind",
      render: (_: unknown, grant: IntegrationGrantSummary) => grant.kind === "oauth" ? "OAuth 授权" : grant.token_prefix,
    },
    { title: "状态", dataIndex: "status", key: "status", render: grantStatus },
    { title: "到期", dataIndex: "expires_at", key: "expires_at", render: (value: string) => formatDate(value) },
    { title: "上次使用", dataIndex: "last_used_at", key: "last_used_at", render: formatDate },
    {
      title: "操作", key: "actions",
      render: (_: unknown, grant: IntegrationGrantSummary) => (
        <SemiSpace spacing="medium">
          {grant.kind !== "oauth" && grant.status === "active" && canRotate && (
            <BackofficeButton disabled={busyId === grant.id} loading={busyId === grant.id} onClick={() => onRotate(grant)}>轮换</BackofficeButton>
          )}
          {grant.status !== "revoked" && canRevoke && (
            <BackofficeButton disabled={busyId === grant.id} loading={busyId === grant.id} onClick={() => onRevoke(grant)} tone="danger">撤销</BackofficeButton>
          )}
        </SemiSpace>
      ),
    },
  ];
  return <SemiTable columns={columns} dataSource={grants} pagination={false} rowKey="id" scroll={{ x: 960 }} size="small" />;
}

function ConnectionGuide({
  audience,
  apiBaseUrl,
  mcpUrl,
  oauthAvailable,
}: {
  audience: IntegrationAudience;
  apiBaseUrl: string;
  mcpUrl: string;
  oauthAvailable: boolean;
}) {
  if (audience === "rest") {
    const curl = `export GREATSELL_API_TOKEN='gs_pat_<一次性令牌>'\n\ncurl --fail-with-body \\\n  -H "Authorization: Bearer $GREATSELL_API_TOKEN" \\\n  -H "Accept: application/json" \\\n  "${apiBaseUrl}/connection"`;
    return (
      <SemiCard title="API 调用示例">
        <SemiSpace spacing="medium" vertical style={{ width: "100%" }}>
          <SemiParagraph type="tertiary" style={{ margin: 0 }}>
            示例只使用环境变量占位符，不含可用密钥。请将令牌放入密码管理器或受控环境变量，不要提交到代码或发送到聊天。
          </SemiParagraph>
          <pre aria-label="API curl 示例" style={{ margin: 0, overflowWrap: "anywhere", whiteSpace: "pre-wrap" }}>{curl}</pre>
        </SemiSpace>
      </SemiCard>
    );
  }
  const config = `[mcp_servers.greatsell_hr_example]\nurl = "${mcpUrl}"\nbearer_token_env_var = "GREATSELL_MCP_TOKEN"`;
  return (
    <SemiSpace spacing="medium" vertical style={{ width: "100%" }}>
      <SemiCard title="MCP 服务地址">
        <SemiParagraph type="tertiary" style={{ margin: 0 }}>
          只将这个地址填写到客户端的 Streamable HTTP / MCP Server URL 字段：{mcpUrl}
        </SemiParagraph>
      </SemiCard>
      <SemiCard title="Codex 配置占位示例">
        <SemiSpace spacing="medium" vertical style={{ width: "100%" }}>
          <SemiParagraph type="tertiary" style={{ margin: 0 }}>
            这是字段示例，不是可直接导入的真实凭据；令牌应由客户端从受控环境变量读取。
          </SemiParagraph>
          <pre aria-label="Codex MCP 配置占位示例" style={{ margin: 0, overflowWrap: "anywhere", whiteSpace: "pre-wrap" }}>{config}</pre>
        </SemiSpace>
      </SemiCard>
      <SemiCard title="WorkBuddy 连接提示">
        <SemiParagraph type="tertiary" style={{ margin: 0 }}>
          在企业 Connector 管理中选择 MCP / Streamable HTTP，使用本页服务地址，并先以不含候选人正文的受控查询确认连通。
          {oauthAvailable ? " 如客户端提供 OAuth，请按其窗口打开大卖智聘的授权页，并核对应用、工作区和权限后再明确批准。" : " 当前以服务器实际开放的认证方式为准。"}
        </SemiParagraph>
      </SemiCard>
    </SemiSpace>
  );
}

function activityLabel(action: string): string {
  const labels: Record<string, string> = {
    "grant.created": "已创建连接授权", "grant.rotated": "已轮换连接授权", "grant.revoked": "已撤销连接授权",
    "grant.admin_revoked": "管理员已撤销连接授权", "workspace.policy_updated": "已更新工作区连接权限",
    "candidates.search": "已查询候选人", "candidates.get": "已读取候选人资料",
    "jobs.list": "已查询职位", "jobs.get": "已读取职位信息", "assessments.read": "已读取评估结果",
  };
  return labels[action] || "已完成一项授权操作";
}

function ActivityTable({ items }: { items: IntegrationActivity[] }) {
  if (items.length === 0) return <SemiEmpty description="最近 90 天没有你的连接访问记录。" />;
  const columns = [
    { title: "时间", dataIndex: "created_at", key: "created_at", render: (value: string) => formatDate(value) },
    { title: "操作", dataIndex: "action", key: "action", render: activityLabel },
    { title: "结果", dataIndex: "result", key: "result", render: (value: string) => value === "success" ? "已完成" : "已记录" },
    { title: "影响范围", key: "count", render: (_: unknown, item: IntegrationActivity) => item.candidate_count > 0 ? `候选人：${item.candidate_count}` : item.resource_count > 0 ? `项目：${item.resource_count}` : "—" },
  ];
  return <SemiTable columns={columns} dataSource={items} pagination={false} rowKey="id" scroll={{ x: 720 }} size="small" />;
}

function DraftDetail({
  detail,
  onClose,
  suspended,
}: {
  detail: IntegrationAnalysisDraftDetail | null;
  onClose: () => void;
  suspended: boolean;
}) {
  return (
    <SemiSideSheet
      aria-label="分析草稿详情"
      closable
      footer={<BackofficeButton onClick={onClose}>关闭</BackofficeButton>}
      onCancel={onClose}
      placement="right"
      title={detail?.title || "分析草稿详情"}
      visible={Boolean(detail) && !suspended}
      width={Math.min(720, typeof window === "undefined" ? 720 : window.innerWidth)}
    >
      {detail && (
        <SemiSpace spacing="medium" vertical style={{ width: "100%" }}>
          <SemiBanner description="这是外部 AI 工具整理的个人草稿，不是正式评分或自动招聘决定。请由招聘团队核验后作出最终判断。" title="需要团队复核" type="warning" />
          <SemiCard title="草稿信息">
            <SemiParagraph style={{ margin: 0 }}>版本：v{detail.version}；更新：{formatDate(detail.updated_at)}；保留至：{formatDate(detail.expires_at)}</SemiParagraph>
          </SemiCard>
          <SemiCard title="已选事实依据">
            <SemiSpace spacing="medium" vertical style={{ width: "100%" }}>
              {detail.referenced_facts.map((profile) => (
                <SemiParagraph key={profile.candidate_id} style={{ margin: 0 }}>
                  {profile.candidate_code}：{[
                    profile.facts.highest_degree ? `最高学历 ${profile.facts.highest_degree}` : null,
                    profile.facts.employment_months === null ? null : `工作经历 ${profile.facts.employment_months} 个月`,
                    profile.facts.skills.map((item) => item.skill).filter(Boolean).join("、") || null,
                  ].filter(Boolean).join("；") || "未保存可展示的已选事实"}
                </SemiParagraph>
              ))}
            </SemiSpace>
          </SemiCard>
          <SemiCard title="外部 AI 推断">
            <AnalysisObservations detail={detail} emptyText="这份草稿没有外部 AI 推断。" items={detail.inferences} />
          </SemiCard>
          <SemiCard title="待核验问题">
            <AnalysisObservations detail={detail} emptyText="这份草稿没有待核验问题。" items={detail.questions_to_verify} />
          </SemiCard>
        </SemiSpace>
      )}
    </SemiSideSheet>
  );
}

function PendingDraftReview({
  detail,
  busy,
  suspended,
  onClose,
  onConfirm,
  onDiscard,
}: {
  detail: IntegrationAnalysisPendingDetail | null;
  busy: boolean;
  suspended: boolean;
  onClose: () => void;
  onConfirm: () => void;
  onDiscard: () => void;
}) {
  return (
    <SemiSideSheet
      aria-label="核对并确认分析草稿"
      closable
      footer={(
        <SemiSpace>
          <BackofficeButton disabled={busy || suspended} onClick={onClose}>暂不处理</BackofficeButton>
          <BackofficeButton disabled={busy || suspended} loading={busy} onClick={onDiscard} tone="danger">丢弃</BackofficeButton>
          <BackofficeButton disabled={busy || suspended || detail?.source_status !== "current"} loading={busy} onClick={onConfirm} tone="primary">确认保存到我的分析记录</BackofficeButton>
        </SemiSpace>
      )}
      onCancel={onClose}
      placement="right"
      title={detail?.title || "核对待保存内容"}
      visible={Boolean(detail) && !suspended}
      width={Math.min(760, typeof window === "undefined" ? 760 : window.innerWidth)}
    >
      {detail && (
        <SemiSpace spacing="medium" vertical style={{ width: "100%" }}>
          <SemiBanner
            description="以下内容来自外部 AI 工具。只有你点击“确认保存”后，才会进入你的私有分析记录；它不是系统正式评分或招聘决定。"
            title="请先核对完整内容"
            type={detail.source_status === "current" ? "warning" : "danger"}
          />
          {detail.source_status !== "current" && <SemiBanner description="候选人事实或简历状态已变化。为避免确认过期分析，请重新读取资料并重新生成草稿。" title="来源已变化，暂不能保存" type="danger" />}
          <SemiCard title="本次请求">
            <SemiSpace spacing="medium" vertical style={{ width: "100%" }}>
              <SemiParagraph style={{ margin: 0 }}>连接：{detail.source_connection_name}</SemiParagraph>
              <SemiParagraph style={{ margin: 0 }}>候选人：{detail.candidates.map((candidate) => candidate.candidate_code).join("、")}</SemiParagraph>
              <SemiParagraph style={{ margin: 0 }}>关联职位：{detail.job_title || (detail.job ? "职位版本已关联" : "未关联职位")}</SemiParagraph>
              <SemiParagraph style={{ margin: 0 }}>岗位版本：{detail.job?.job_version_id || "未关联岗位版本"}</SemiParagraph>
              <SemiParagraph style={{ margin: 0 }}>待确认内容将在 {formatDate(detail.expires_at)} 过期。</SemiParagraph>
            </SemiSpace>
          </SemiCard>
          <SemiCard title="候选人事实、版本与证据引用">
            <SemiSpace spacing="medium" vertical style={{ width: "100%" }}>
              {detail.referenced_facts.map((profile) => (
                <div key={profile.candidate_id}>
                  <SemiParagraph style={{ margin: "0 0 8px" }}>{profile.candidate_code} · 已选择的结构化事实</SemiParagraph>
                  <SemiParagraph style={{ margin: "0 0 8px" }}>
                    简历版本引用：{profile.resume_id}；事实版本：v{profile.facts_version}；事实快照：{profile.fact_snapshot_id}
                  </SemiParagraph>
                  <SemiParagraph style={{ margin: "0 0 8px" }}>
                    引用证据：{selectedEvidenceReferences(profile).join("、") || "未选择可展示的证据引用"}
                  </SemiParagraph>
                  <pre style={{ margin: 0, overflowWrap: "anywhere", whiteSpace: "pre-wrap" }}>{JSON.stringify(profile.facts, null, 2)}</pre>
                </div>
              ))}
            </SemiSpace>
          </SemiCard>
          <SemiCard title="外部 AI 推断">
            <AnalysisObservations detail={detail} emptyText="没有外部 AI 推断。" items={detail.inferences} />
          </SemiCard>
          <SemiCard title="待核验问题">
            <AnalysisObservations detail={detail} emptyText="没有待核验问题。" items={detail.questions_to_verify} />
          </SemiCard>
        </SemiSpace>
      )}
    </SemiSideSheet>
  );
}

function AnalysisReports({
  enabled,
  identityKey,
  csrfToken,
  onAccessLost,
  suspended,
}: {
  enabled: boolean;
  identityKey: string;
  csrfToken: string;
  onAccessLost: () => void;
  suspended: boolean;
}) {
  const [items, setItems] = useState<IntegrationAnalysisDraftSummary[]>([]);
  const [pendingItems, setPendingItems] = useState<IntegrationAnalysisPendingSummary[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [detail, setDetail] = useState<IntegrationAnalysisDraftDetail | null>(null);
  const [pendingDetail, setPendingDetail] = useState<IntegrationAnalysisPendingDetail | null>(null);
  const [busyPendingId, setBusyPendingId] = useState<string | null>(null);
  const epochRef = useRef(0);
  const controllerRef = useRef<AbortController | null>(null);
  const suspendedRef = useRef(suspended);
  const initializedRef = useRef(false);
  const lastIdentityKeyRef = useRef(identityKey);

  const load = useCallback(async () => {
    if (suspendedRef.current) return;
    const epoch = epochRef.current;
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    setLoading(true);
    setError(null);
    try {
      const [savedResult, pendingResult] = await Promise.allSettled([
        integrationApi.listAnalysisReports(20, null, controller.signal),
        integrationApi.listPendingAnalysisReports(controller.signal),
      ]);
      if (epoch !== epochRef.current || suspendedRef.current) return;
      if (savedResult.status === "fulfilled") setItems(savedResult.value.items);
      else if (isIntegrationApiError(savedResult.reason) && savedResult.reason.status === 401) onAccessLost();
      if (pendingResult.status === "fulfilled") setPendingItems(pendingResult.value.items);
      else if (isIntegrationApiError(pendingResult.reason) && pendingResult.reason.status === 401) onAccessLost();
      const failures = [savedResult, pendingResult]
        .filter((result): result is PromiseRejectedResult => result.status === "rejected")
        .map((result) => integrationErrorMessage(result.reason));
      if (failures.length) setError([...new Set(failures)].join("；"));
    } finally {
      if (epoch === epochRef.current && !suspendedRef.current) setLoading(false);
    }
  }, [onAccessLost]);

  useEffect(() => {
    suspendedRef.current = suspended;
    if (suspended) {
      epochRef.current += 1;
      controllerRef.current?.abort();
      setLoading(false);
      return;
    }

    const identityChanged = initializedRef.current && lastIdentityKeyRef.current !== identityKey;
    if (!initializedRef.current || identityChanged) {
      initializedRef.current = true;
      lastIdentityKeyRef.current = identityKey;
      epochRef.current += 1;
      controllerRef.current?.abort();
      setItems([]);
      setPendingItems([]);
      setDetail(null);
      setPendingDetail(null);
      setError(null);
      setNotice(null);
    }
    if (enabled) void load();
    return () => {
      if (identityChanged) {
        epochRef.current += 1;
        controllerRef.current?.abort();
      }
    };
  }, [enabled, identityKey, load, suspended]);

  const openDetail = useCallback(async (id: string) => {
    if (suspendedRef.current) return;
    const epoch = epochRef.current;
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    setError(null);
    try {
      const next = await integrationApi.getAnalysisReport(id, controller.signal);
      if (epoch === epochRef.current && !suspendedRef.current) setDetail(next);
    } catch (requestError) {
      if (isAbortError(requestError) || epoch !== epochRef.current) return;
      if (isIntegrationApiError(requestError) && requestError.status === 401) onAccessLost();
      else setError(integrationErrorMessage(requestError));
    }
  }, [onAccessLost]);

  const openPendingDetail = useCallback(async (id: string) => {
    if (suspendedRef.current) return;
    const epoch = epochRef.current;
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    setError(null);
    try {
      const next = await integrationApi.getPendingAnalysisReport(id, controller.signal);
      if (epoch === epochRef.current && !suspendedRef.current) setPendingDetail(next);
    } catch (requestError) {
      if (isAbortError(requestError) || epoch !== epochRef.current) return;
      if (isIntegrationApiError(requestError) && requestError.status === 401) onAccessLost();
      else setError(integrationErrorMessage(requestError));
    }
  }, [onAccessLost]);

  const confirmPending = useCallback(async () => {
    if (!pendingDetail || suspended) return;
    const current = pendingDetail;
    setBusyPendingId(current.id);
    setError(null);
    try {
      await integrationApi.confirmPendingAnalysisReport(current.id, {
        version: current.version,
        payload_sha256: current.payload_sha256,
      }, csrfToken);
      if (suspendedRef.current) return;
      setPendingDetail(null);
      setNotice("已确认保存。草稿现已出现在你的分析记录中。 ");
      await load();
    } catch (requestError) {
      if (suspendedRef.current) return;
      if (isIntegrationApiError(requestError) && requestError.status === 401) onAccessLost();
      else setError(integrationErrorMessage(requestError));
    } finally {
      setBusyPendingId(null);
    }
  }, [csrfToken, load, onAccessLost, pendingDetail, suspended]);

  const discardPending = useCallback(async () => {
    if (!pendingDetail || suspended) return;
    const current = pendingDetail;
    setBusyPendingId(current.id);
    setError(null);
    try {
      await integrationApi.discardPendingAnalysisReport(current.id, current.version, csrfToken);
      if (suspendedRef.current) return;
      setPendingDetail(null);
      setNotice("已丢弃，待确认内容已清除。 ");
      await load();
    } catch (requestError) {
      if (suspendedRef.current) return;
      if (isIntegrationApiError(requestError) && requestError.status === 401) onAccessLost();
      else setError(integrationErrorMessage(requestError));
    } finally {
      setBusyPendingId(null);
    }
  }, [csrfToken, load, onAccessLost, pendingDetail, suspended]);

  if (!enabled) return <SemiCard title="我的分析记录"><SemiEmpty description="分析记录功能尚未启用。系统不会自动生成招聘决定、自动拒绝或自动录用候选人。" /></SemiCard>;
  const columns = [
    { title: "标题", dataIndex: "title", key: "title" },
    { title: "版本", dataIndex: "version", key: "version", render: (value: number) => `v${value}` },
    { title: "候选人代号", dataIndex: "candidates", key: "candidates", render: (candidates: IntegrationAnalysisDraftSummary["candidates"]) => candidates.map((candidate) => candidate.candidate_code).join("、") || "—" },
    { title: "更新时间", dataIndex: "updated_at", key: "updated_at", render: (value: string) => formatDate(value) },
    { title: "操作", key: "actions", render: (_: unknown, item: IntegrationAnalysisDraftSummary) => <BackofficeButton onClick={() => void openDetail(item.id)}>查看详情</BackofficeButton> },
  ];
  const pendingColumns = [
    { title: "待确认内容", dataIndex: "title", key: "title" },
    { title: "候选人", dataIndex: "candidates", key: "candidates", render: (candidates: IntegrationAnalysisPendingSummary["candidates"]) => candidates.map((candidate) => candidate.candidate_code).join("、") || "—" },
    { title: "来源连接", dataIndex: "source_connection_name", key: "source_connection_name" },
    { title: "过期时间", dataIndex: "expires_at", key: "expires_at", render: formatDate },
    { title: "操作", key: "actions", render: (_: unknown, item: IntegrationAnalysisPendingSummary) => <BackofficeButton onClick={() => void openPendingDetail(item.id)}>核对内容</BackofficeButton> },
  ];
  return (
    <SemiSpace spacing="medium" vertical style={{ width: "100%" }}>
      {notice && <SemiBanner description={notice} title="操作完成" type="success" />}
      {error && <SemiBanner description={error} title="部分连接记录暂不可用" type="danger" />}
      <SemiCard title="待你确认的外部 AI 草稿">
        <SemiSpace spacing="medium" vertical style={{ width: "100%" }}>
          <SemiParagraph type="tertiary" style={{ margin: 0 }}>外部工具不能直接保存分析结论。请先核对内容，再明确确认或丢弃；未处理的内容 15 分钟后失效。</SemiParagraph>
          {loading ? <p>加载待确认内容…</p> : pendingItems.length === 0 ? <SemiEmpty description="没有待确认的内容。" /> : <SemiTable columns={pendingColumns} dataSource={pendingItems} pagination={false} rowKey="id" scroll={{ x: 760 }} size="small" />}
        </SemiSpace>
      </SemiCard>
      <SemiCard title="我的分析记录">
        <SemiSpace spacing="medium" vertical style={{ width: "100%" }}>
          <SemiParagraph type="tertiary" style={{ margin: 0 }}>仅显示你在当前工作区创建或保存的外部 AI 分析草稿。草稿不是正式评分，最终招聘判断由团队作出。</SemiParagraph>
          {loading ? <p>加载分析记录…</p> : items.length === 0 ? <SemiEmpty description="暂无可展示的分析草稿。" /> : <SemiTable columns={columns} dataSource={items} pagination={false} rowKey="id" scroll={{ x: 760 }} size="small" />}
        </SemiSpace>
      </SemiCard>
      <DraftDetail detail={detail} onClose={() => setDetail(null)} suspended={suspended} />
      <PendingDraftReview
        busy={busyPendingId === pendingDetail?.id}
        detail={pendingDetail}
        onClose={() => setPendingDetail(null)}
        onConfirm={() => void confirmPending()}
        onDiscard={() => void discardPending()}
        suspended={suspended}
      />
    </SemiSpace>
  );
}

/**
 * The parent remounts this page on known identity changes. This component also
 * validates every refreshed server payload so a cross-tab cookie replacement
 * cannot leave a previous user's token, grants, activity, or drafts visible.
 */
export function IntegrationSettingsPanel({ identityKey }: { identityKey: string }) {
  const [settings, setSettings] = useState<IntegrationSettings | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [identityError, setIdentityError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [oneTimeToken, setOneTimeToken] = useState<IntegrationGrantCreated | null>(null);
  const [form, setForm] = useState<GrantFormState>(() => initialGrantForm([]));
  const [submitting, setSubmitting] = useState(false);
  const [busyGrantId, setBusyGrantId] = useState<string | null>(null);
  const [workspaceEnabled, setWorkspaceEnabled] = useState(false);
  const [workspaceScopes, setWorkspaceScopes] = useState<string[]>([]);
  const [workspaceGrants, setWorkspaceGrants] = useState<IntegrationGrantSummary[] | null>(null);
  const [activity, setActivity] = useState<IntegrationActivity[]>([]);
  const [identityRevalidating, setIdentityRevalidating] = useState(true);
  const requestEpochRef = useRef(0);
  const readControllersRef = useRef(new Set<AbortController>());
  const verifiedIdentityRef = useRef(false);

  const abortReads = useCallback(() => {
    readControllersRef.current.forEach((controller) => controller.abort());
    readControllersRef.current.clear();
  }, []);

  const clearPrivateState = useCallback(() => {
    verifiedIdentityRef.current = false;
    setSettings(null);
    setOneTimeToken(null);
    setNotice(null);
    setForm(initialGrantForm([]));
    setSubmitting(false);
    setBusyGrantId(null);
    setWorkspaceEnabled(false);
    setWorkspaceScopes([]);
    setWorkspaceGrants(null);
    setActivity([]);
  }, []);

  const invalidateIdentity = useCallback((message: string) => {
    requestEpochRef.current += 1;
    abortReads();
    clearPrivateState();
    setLoading(false);
    setIdentityRevalidating(false);
    setError(null);
    setIdentityError(message);
  }, [abortReads, clearPrivateState]);

  const handleRequestError = useCallback((requestError: unknown, epoch: number) => {
    if (isAbortError(requestError) || epoch !== requestEpochRef.current) return;
    if (isIntegrationApiError(requestError) && [401, 403].includes(requestError.status)) {
      invalidateIdentity("登录或工作区身份已在其他标签变化。为保护私有连接信息，当前内容已清空；请刷新并重新登录后继续。");
      return;
    }
    setError(integrationErrorMessage(requestError));
  }, [invalidateIdentity]);

  const load = useCallback(async () => {
    const epoch = requestEpochRef.current + 1;
    requestEpochRef.current = epoch;
    abortReads();
    const controller = new AbortController();
    readControllersRef.current.add(controller);
    // A cookie can be replaced by another tab without updating React auth
    // state. An already-verified panel remains mounted, but is hidden and
    // inert until the server proves that the same identity still owns it.
    // This retains a pending-confirmation review only for the same identity.
    if (!verifiedIdentityRef.current) clearPrivateState();
    // One-time tokens are intentionally never retained across a refresh.
    setOneTimeToken(null);
    setIdentityRevalidating(true);
    setLoading(true);
    setError(null);
    setIdentityError(null);
    try {
      const next = await integrationApi.getSettings(controller.signal);
      if (epoch !== requestEpochRef.current) return;
      if (!settingsMatchIdentity(next, identityKey)) {
        invalidateIdentity("登录或工作区身份已变化。为保护私有连接信息，当前内容已清空；请刷新当前页面后继续。");
        return;
      }
      verifiedIdentityRef.current = true;
      setSettings(next);
      setWorkspaceEnabled(next.workspace.enabled);
      setWorkspaceScopes(next.workspace.allowed_scopes);
      setForm((current) => current.scopes.length > 0 ? current : initialGrantForm(next.workspace.allowed_scopes));
    } catch (requestError) {
      if (isAbortError(requestError) || epoch !== requestEpochRef.current) return;
      if (isIntegrationApiError(requestError) && [401, 403].includes(requestError.status)) {
        invalidateIdentity("登录或工作区身份已在其他标签变化。为保护私有连接信息，当前内容已清空；请刷新并重新登录后继续。");
        return;
      }
      // Identity is unknown after an unsuccessful revalidation. Do not reveal
      // data that belonged to the previous cookie/session.
      clearPrivateState();
      setIdentityError(null);
      setError(integrationErrorMessage(requestError));
    } finally {
      readControllersRef.current.delete(controller);
      if (epoch === requestEpochRef.current) {
        setLoading(false);
        setIdentityRevalidating(false);
      }
    }
  }, [abortReads, clearPrivateState, handleRequestError, identityKey, invalidateIdentity]);

  const loadWorkspaceGrants = useCallback(async () => {
    if (!settings?.permissions.can_admin) return;
    const epoch = requestEpochRef.current;
    const controller = new AbortController();
    readControllersRef.current.add(controller);
    try {
      const grants = await integrationApi.listWorkspaceGrants(controller.signal);
      if (epoch === requestEpochRef.current) setWorkspaceGrants(grants);
    } catch (requestError) {
      handleRequestError(requestError, epoch);
    } finally {
      readControllersRef.current.delete(controller);
    }
  }, [handleRequestError, settings?.permissions.can_admin]);

  const loadActivity = useCallback(async () => {
    const epoch = requestEpochRef.current;
    const controller = new AbortController();
    readControllersRef.current.add(controller);
    try {
      const page = await integrationApi.listActivity(50, null, controller.signal);
      if (epoch === requestEpochRef.current) setActivity(page.items);
    } catch (requestError) {
      handleRequestError(requestError, epoch);
    } finally {
      readControllersRef.current.delete(controller);
    }
  }, [handleRequestError]);

  useEffect(() => {
    void load();
    return () => {
      requestEpochRef.current += 1;
      abortReads();
      // React disposes state on unmount; this also clears state before an
      // identity-key remount so a secret never survives the transition.
      clearPrivateState();
    };
  }, [abortReads, clearPrivateState, identityKey, load]);
  useEffect(() => { void loadWorkspaceGrants(); }, [loadWorkspaceGrants]);
  useEffect(() => { if (settings) void loadActivity(); }, [loadActivity, settings]);

  useEffect(() => {
    const refreshOnVisible = () => {
      if (document.visibilityState === "visible") void load();
    };
    window.addEventListener("focus", refreshOnVisible);
    document.addEventListener("visibilitychange", refreshOnVisible);
    return () => {
      window.removeEventListener("focus", refreshOnVisible);
      document.removeEventListener("visibilitychange", refreshOnVisible);
    };
  }, [load]);

  const disabledReason = useCallback((audience: IntegrationAudience) => {
    if (!settings?.workspace.enabled) return "管理员当前未为此工作区启用 API 与 AI 工具连接。";
    if (!settings.permissions.can_create) return "你没有创建新连接的权限。";
    if (audience === "rest" && !settings.features.api) return "当前工作区尚未开放 API 密钥。";
    if (audience === "mcp" && !settings.features.mcp) return "当前工作区尚未开放 MCP 连接。";
    return null;
  }, [settings]);

  const createGrant = async (audience: IntegrationAudience) => {
    if (!settings || loading || identityError) return;
    const epoch = requestEpochRef.current;
    const expiresInDays = cleanExpiresInDays(form.expiresInDays);
    if (!form.name.trim()) { setError("请填写连接名称。"); return; }
    if (form.scopes.length === 0) { setError("请至少选择一个授权范围。"); return; }
    if (!expiresInDays) { setError("有效期必须是 1 到 90 天之间的整数。"); return; }
    setSubmitting(true);
    setError(null);
    try {
      const created = await integrationApi.createGrant({ name: form.name.trim(), audience, scopes: form.scopes, expires_in_days: expiresInDays }, settings.csrf_token);
      if (epoch !== requestEpochRef.current) return;
      setOneTimeToken(created);
      setNotice("连接已创建。令牌正文只在当前页面内存中显示一次。");
      setForm(initialGrantForm(settings.workspace.allowed_scopes));
      setSettings((current) => current ? { ...current, grants: [...current.grants, created.grant] } : current);
    } catch (requestError) { handleRequestError(requestError, epoch); } finally { if (epoch === requestEpochRef.current) setSubmitting(false); }
  };

  const rotateGrant = async (grant: IntegrationGrantSummary) => {
    if (!settings || loading || identityError) return;
    const epoch = requestEpochRef.current;
    const expiresInDays = cleanExpiresInDays(form.expiresInDays);
    if (!expiresInDays) { setError("有效期必须是 1 到 90 天之间的整数。"); return; }
    setBusyGrantId(grant.id);
    try {
      const created = await integrationApi.rotateGrant(grant.id, expiresInDays, settings.csrf_token);
      if (epoch !== requestEpochRef.current) return;
      setOneTimeToken(created);
      setNotice("连接已轮换。旧令牌已失效，新的令牌只显示一次。");
      setSettings((current) => current ? {
        ...current,
        grants: current.grants.map((item) => item.id === grant.id ? created.grant : item),
      } : current);
    } catch (requestError) { handleRequestError(requestError, epoch); } finally { if (epoch === requestEpochRef.current) setBusyGrantId(null); }
  };

  const revokeGrant = async (grant: IntegrationGrantSummary, workspaceAction = false) => {
    if (!settings || loading || identityError || !window.confirm(`确认撤销“${grant.name}”？撤销后将停止后续访问。`)) return;
    const epoch = requestEpochRef.current;
    setBusyGrantId(grant.id);
    try {
      if (workspaceAction) await integrationApi.revokeWorkspaceGrant(grant.id, settings.csrf_token);
      else await integrationApi.revokeOwnGrant(grant.id, settings.csrf_token);
      if (epoch !== requestEpochRef.current) return;
      setNotice(`已撤销“${grant.name}”。此前发送到外部工具的数据无法追回。`);
      setBusyGrantId(null);
      await Promise.all([load(), loadWorkspaceGrants()]);
    } catch (requestError) { handleRequestError(requestError, epoch); } finally { if (epoch === requestEpochRef.current) setBusyGrantId(null); }
  };

  const saveWorkspacePolicy = async () => {
    if (!settings || loading || identityError) return;
    const epoch = requestEpochRef.current;
    setSubmitting(true);
    try {
      await integrationApi.updateWorkspacePolicy({ enabled: workspaceEnabled, allowed_scopes: workspaceScopes }, settings.csrf_token);
      if (epoch !== requestEpochRef.current) return;
      setNotice("工作区连接策略已保存。");
      setSubmitting(false);
      await load();
    } catch (requestError) { handleRequestError(requestError, epoch); } finally { if (epoch === requestEpochRef.current) setSubmitting(false); }
  };

  const grantsFor = (audience: IntegrationAudience) => settings?.grants.filter((grant) => grant.audience === audience) ?? [];
  const loseAnalysisAccess = useCallback(() => {
    invalidateIdentity("登录或工作区身份已失效。为保护私有分析草稿，当前内容已清空；请刷新并重新登录后继续。");
  }, [invalidateIdentity]);
  const scopeOptions = useMemo(() => (settings?.workspace.available_scopes ?? settings?.workspace.allowed_scopes ?? [])
    .map((scope) => ({ label: scopeLabel(scope), value: scope })), [settings?.workspace.allowed_scopes, settings?.workspace.available_scopes]);

  if (loading && !settings) return <p>正在重新核对登录身份与连接服务…</p>;
  if (!settings) return <SemiBanner description={identityError ?? error ?? "未能读取连接服务状态。"} title={identityError ? "需要刷新登录状态" : "连接服务不可用"} type="danger" />;

  const formFor = (audience: IntegrationAudience) => {
    const reason = disabledReason(audience);
    return (
      <SemiCard title={audience === "rest" ? "新建 API 密钥" : "新建 MCP 连接授权"}>
        <SemiSpace spacing="medium" vertical style={{ width: "100%" }}>
          {reason ? <SemiBanner description={reason} title="当前不可创建连接" type="info" /> : <>
            <BackofficeInput aria-label="连接名称" disabled={submitting || loading} onChange={(name) => setForm((current) => ({ ...current, name }))} placeholder="例如：招聘分析脚本" value={form.name} />
            <SemiCheckboxGroup aria-label="授权范围" direction="vertical" disabled={submitting || loading} onChange={(values) => setForm((current) => ({ ...current, scopes: values.map(String) }))} options={settings.workspace.allowed_scopes.map((scope) => ({ label: scopeLabel(scope), value: scope }))} value={form.scopes} />
            {form.scopes.includes("evidence:read") && (
              <SemiBanner
                description="原文片段会提供给所选外部 AI 服务。系统会清理常见联系方式和已知候选人姓名，但无法保证识别所有自由文本个人信息；启用前请确认符合本组织的数据处理要求。"
                title="原文片段可能含个人信息"
                type="warning"
              />
            )}
            <BackofficeInput aria-label="有效期（天）" disabled={submitting || loading} max="90" min="1" onChange={(expiresInDays) => setForm((current) => ({ ...current, expiresInDays }))} type="number" value={form.expiresInDays} />
            <SemiParagraph type="tertiary" style={{ margin: 0 }}>默认 30 天，最长 90 天。创建成功后只展示一次令牌。</SemiParagraph>
            <div><BackofficeButton disabled={loading} loading={submitting} onClick={() => void createGrant(audience)} tone="primary">创建并显示一次令牌</BackofficeButton></div>
          </>}
        </SemiSpace>
      </SemiCard>
    );
  };

  return (
    <div aria-busy={identityRevalidating || undefined}>
      {identityRevalidating && (
        <div
          aria-live="polite"
          role="status"
          style={{
            alignItems: "center",
            background: "rgba(255, 255, 255, 0.98)",
            display: "flex",
            inset: 0,
            justifyContent: "center",
            position: "fixed",
            zIndex: 2147483647,
          }}
        >
          正在重新确认登录身份与工作区权限…
        </div>
      )}
      <div
        aria-hidden={identityRevalidating || undefined}
        style={identityRevalidating ? { pointerEvents: "none", visibility: "hidden" } : undefined}
      >
    <Suspense fallback={<p>加载 API 与 AI 工具连接…</p>}>
      <header className="page-heading">
        <div>
          <SemiTitle heading={2} style={{ margin: 0 }}>API 与 AI 工具连接</SemiTitle>
          <SemiParagraph type="tertiary" style={{ margin: "6px 0 0" }}>
            当前账户：{settings.user.display_name || settings.user.email || "当前账户"}；当前公司/工作区：{settings.workspace.name}。每次访问均由服务端按当前权限和工作区策略复核。
          </SemiParagraph>
        </div>
      </header>
      <SemiSpace spacing="medium" vertical style={{ width: "100%" }}>
        {error && <SemiBanner description={error} title="操作未完成" type="danger" />}
        {notice && <SemiBanner description={notice} title="操作结果" type="success" />}
        {oneTimeToken && <OneTimeToken created={oneTimeToken} onClear={() => setOneTimeToken(null)} />}
        <SemiTabs defaultActiveKey="api-keys" type="button">
          <SemiTabPane itemKey="api-keys" tab="我的 API 密钥">
            <SemiSpace spacing="medium" vertical style={{ width: "100%" }}>
              {formFor("rest")}
              <SemiCard title="我的 API 密钥"><GrantTable busyId={busyGrantId} canRevoke={settings.permissions.can_revoke_own} canRotate={settings.permissions.can_create && settings.features.api} grants={grantsFor("rest")} onRevoke={(grant) => void revokeGrant(grant)} onRotate={(grant) => void rotateGrant(grant)} /></SemiCard>
              {settings.features.api && <ConnectionGuide apiBaseUrl={settings.endpoints.api_base_url} audience="rest" mcpUrl={settings.endpoints.mcp_url} oauthAvailable={settings.features.oauth} />}
            </SemiSpace>
          </SemiTabPane>
          <SemiTabPane itemKey="mcp" tab="我的 MCP 连接">
            <SemiSpace spacing="medium" vertical style={{ width: "100%" }}>
              {formFor("mcp")}
              {settings.features.mcp && settings.endpoints.mcp_url && <ConnectionGuide apiBaseUrl={settings.endpoints.api_base_url} audience="mcp" mcpUrl={settings.endpoints.mcp_url} oauthAvailable={settings.features.oauth} />}
              <SemiCard title="我的 MCP 连接"><GrantTable busyId={busyGrantId} canRevoke={settings.permissions.can_revoke_own} canRotate={settings.permissions.can_create && settings.features.mcp} grants={grantsFor("mcp")} onRevoke={(grant) => void revokeGrant(grant)} onRotate={(grant) => void rotateGrant(grant)} /></SemiCard>
            </SemiSpace>
          </SemiTabPane>
          <SemiTabPane itemKey="analysis" tab="我的分析记录"><AnalysisReports csrfToken={settings.csrf_token} enabled={settings.features.analyses} identityKey={identityKey} onAccessLost={loseAnalysisAccess} suspended={identityRevalidating} /></SemiTabPane>
          <SemiTabPane itemKey="activity" tab="访问记录与帮助">
            <SemiSpace spacing="medium" vertical style={{ width: "100%" }}>
              <SemiCard title="访问记录"><SemiParagraph type="tertiary">仅显示当前工作区最近 90 天的连接访问摘要，不包含候选人正文、资源名称或令牌内容。</SemiParagraph><ActivityTable items={activity} /></SemiCard>
              <SemiCard title="使用前须知"><SemiParagraph style={{ margin: 0 }}>外部 AI 工具会收到你主动授权并发送给它的数据。撤销会阻止未来访问，但不能追回撤销前已经发送的数据。</SemiParagraph></SemiCard>
            </SemiSpace>
          </SemiTabPane>
          {settings.permissions.can_admin && <SemiTabPane itemKey="workspace" tab="工作区访问设置">
            <SemiSpace spacing="medium" vertical style={{ width: "100%" }}>
              <SemiCard title="工作区启用与权限">
                <SemiSpace spacing="medium" vertical style={{ width: "100%" }}>
                  <SemiSwitch aria-label="启用工作区 API 与 AI 工具连接" checked={workspaceEnabled} disabled={submitting || loading} onChange={setWorkspaceEnabled} />
                  <SemiCheckboxGroup aria-label="工作区允许的授权范围" direction="vertical" disabled={submitting || loading} onChange={(values) => setWorkspaceScopes(values.map(String))} options={scopeOptions} value={workspaceScopes} />
                  <SemiParagraph type="tertiary" style={{ margin: 0 }}>停用后，现有连接停止访问；管理员可以撤销连接，但不能查看成员令牌正文。</SemiParagraph>
                  <div><BackofficeButton disabled={loading} loading={submitting} onClick={() => void saveWorkspacePolicy()} tone="primary">保存工作区策略</BackofficeButton></div>
                </SemiSpace>
              </SemiCard>
              <SemiCard title="工作区连接管理">{workspaceGrants === null ? <p>加载工作区连接…</p> : <GrantTable busyId={busyGrantId} canRevoke canRotate={false} grants={workspaceGrants} onRevoke={(grant) => void revokeGrant(grant, true)} onRotate={() => undefined} />}</SemiCard>
            </SemiSpace>
          </SemiTabPane>}
        </SemiTabs>
      </SemiSpace>
    </Suspense>
      </div>
    </div>
  );
}
