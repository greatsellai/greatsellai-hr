import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import { Icon } from "../../icons";
import { adminApi, adminErrorMessage } from "../admin-api";
import type {
  AiModelProfile,
  AiProviderProfile,
  AiRoutePolicy,
  AiRunUsage,
  AiUsageAggregate,
  AiUsageTrendBucket,
  AiUsageTrendGranularity,
  PlatformAuditEvent,
  PlatformUserDetail,
  PlatformUserSummary,
  ProductPlan,
  RequestState,
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
import { AdminAiConfigurationPanel } from "./AdminAiConfigurationPanel";
import { TokenUsageTrendChart } from "../TokenUsageTrendChart";
import {
  aiFeatureFilterOptions,
  featureDisplayName,
  modelDisplayName,
  providerDisplayName,
  routeDisplayDescription,
  routeDisplayName,
  serviceKindDisplayName,
} from "../ai-display";

const PAGE_SIZE = 30;

const PLAN_FEATURE_LABELS: Record<string, string> = {
  resume_library: "简历库",
  candidate_filtering: "候选人筛选",
  ai_scoring: "AI 简历评分",
  ai_summary: "AI 简历总结",
  jd_matching: "JD 匹配",
  recruiting_agent: "招聘智能助手",
  mailbox_import: "邮箱简历入库",
  ai_jd_generation: "AI 生成 JD",
  interview_questions: "面试题生成",
  interview_records: "面试记录",
  hrbp_reference: "HRBP 面试结论参考",
};

function planFeatureLabel(feature: string) {
  return PLAN_FEATURE_LABELS[feature] ?? feature;
}

function querySearch() {
  return new URLSearchParams(window.location.search).get("search")?.trim() || "";
}

function UserDetailPane({
  userId,
  onUpdated,
}: {
  userId: string | null;
  onUpdated: (user: PlatformUserDetail) => void;
}) {
  const [state, setState] = useState<RequestState>("idle");
  const [detail, setDetail] = useState<PlatformUserDetail | null>(null);
  const [error, setError] = useState("");
  const [reason, setReason] = useState("");
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState("");

  const load = useCallback(async () => {
    if (!userId) {
      setState("idle");
      setDetail(null);
      return;
    }
    setState("loading");
    setError("");
    setNotice("");
    try {
      setDetail(await adminApi.getUser(userId));
      setReason("");
      setState("ready");
    } catch (loadError) {
      setError(adminErrorMessage(loadError));
      setState("error");
    }
  }, [userId]);

  useEffect(() => { void load(); }, [load]);

  const toggleUser = async () => {
    if (!detail || !reason.trim()) return;
    setSaving(true);
    setError("");
    setNotice("");
    try {
      const updated = await adminApi.updateUser(detail.user_id, !detail.is_active, reason.trim());
      setDetail(updated);
      setReason("");
      setNotice(updated.is_active ? "账号已恢复使用，操作已写入审计。" : "账号已停用，现有会话将按服务端策略失效。 ");
      onUpdated(updated);
    } catch (saveError) {
      setError(adminErrorMessage(saveError));
    } finally {
      setSaving(false);
    }
  };

  if (!userId) return <aside className="admin-detail-pane"><AdminEmpty description="从列表选择用户，查看身份和所属工作区。" title="选择用户" /></aside>;
  if (state === "loading") return <aside className="admin-detail-pane"><AdminLoading label="正在读取用户详情…" /></aside>;
  if (state === "error") return <aside className="admin-detail-pane"><AdminError message={error} onRetry={() => void load()} /></aside>;
  if (!detail) return null;

  const protectedPlatformAdmin = detail.is_platform_admin && detail.is_active;

  return (
    <aside className="admin-detail-pane" aria-labelledby="admin-user-detail-title">
      <header className="admin-detail-header">
        <div>
          <span className="admin-detail-label">用户详情</span>
          <h2 id="admin-user-detail-title">{detail.full_name || "未命名用户"}</h2>
          <p>{detail.email}</p>
        </div>
        <AdminStatus status={detail.is_active ? "active" : "inactive"} />
      </header>
      <section className="admin-detail-section">
        <dl className="admin-fact-list">
          <div><dt>邮箱验证</dt><dd><AdminStatus status={detail.email_verified ? "verified" : "pending"} label={detail.email_verified ? "已验证" : "未验证"} /></dd></div>
          <div><dt>平台权限</dt><dd>{detail.is_platform_admin ? "平台管理员" : "普通用户"}</dd></div>
          <div><dt>最近登录</dt><dd>{formatDate(detail.last_login_at, true)}</dd></div>
          <div><dt>注册时间</dt><dd>{formatDate(detail.created_at, true)}</dd></div>
          <div><dt>用户 ID</dt><dd title={detail.user_id}>{shortId(detail.user_id)}</dd></div>
        </dl>
      </section>
      <section className="admin-detail-section">
        <div className="admin-detail-section-heading"><h3>所属工作区</h3><span>{detail.memberships.length} 个身份</span></div>
        {detail.memberships.length ? (
          <ul className="admin-simple-list">
            {detail.memberships.map((membership) => (
              <li key={membership.membership_id}>
                <span><strong>{membership.organization_name}</strong><small>{membership.role === "admin" ? "工作区管理员" : "招聘官"} · {formatDate(membership.joined_at)}</small></span>
                <AdminStatus status={membership.is_active ? "active" : "inactive"} />
              </li>
            ))}
          </ul>
        ) : <p className="admin-inline-empty">此用户没有工作区身份。</p>}
      </section>
      <section className="admin-detail-section admin-account-action">
        <div className="admin-detail-section-heading"><h3>{protectedPlatformAdmin ? "受保护账号" : detail.is_active ? "停用账号" : "恢复账号"}</h3>{protectedPlatformAdmin && <AdminStatus label="平台管理员" status="warning" />}</div>
        <p>{protectedPlatformAdmin ? "平台管理员不能从这里直接停用。请先完成平台权限交接并移除平台权限。" : detail.is_active ? "停用后用户不能继续登录，工作区和历史数据不会删除。" : "恢复后用户可以重新登录其有效工作区。"}</p>
        {!protectedPlatformAdmin && <label>
          <span>操作原因</span>
          <textarea className="textarea-field" maxLength={500} onChange={(event) => setReason(event.target.value)} placeholder="填写客服工单、客户请求或内部处置原因" rows={3} value={reason} />
        </label>}
        {error && <p className="admin-form-error" role="alert">{error}</p>}
        {notice && <p className="admin-form-success" role="status">{notice}</p>}
        <button
          className={`button${detail.is_active ? " button-danger-ghost" : " button-primary"}`}
          disabled={protectedPlatformAdmin || saving || !reason.trim()}
          onClick={() => void toggleUser()}
          type="button"
        >{protectedPlatformAdmin ? "平台管理员受保护" : saving ? <><i className="spinner" />正在处理</> : detail.is_active ? "停用这个账号" : "恢复这个账号"}</button>
      </section>
    </aside>
  );
}

export function AdminUsersPage() {
  const [state, setState] = useState<RequestState>("loading");
  const [error, setError] = useState("");
  const [items, setItems] = useState<PlatformUserSummary[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [searchDraft, setSearchDraft] = useState(querySearch);
  const [search, setSearch] = useState(querySearch);
  const [activeFilter, setActiveFilter] = useState<"" | "true" | "false">("");

  const load = useCallback(async () => {
    setState("loading");
    setError("");
    try {
      const page = await adminApi.listUsers({
        search: search || undefined,
        is_active: activeFilter === "" ? "" : activeFilter === "true",
        limit: PAGE_SIZE,
        offset,
      });
      setItems(page.items);
      setTotal(page.total);
      setSelectedId((current) => current && page.items.some((item) => item.user_id === current) ? current : page.items[0]?.user_id ?? null);
      setState("ready");
    } catch (loadError) {
      setError(adminErrorMessage(loadError));
      setState("error");
    }
  }, [activeFilter, offset, search]);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    const sync = () => { const next = querySearch(); setSearchDraft(next); setSearch(next); setOffset(0); };
    window.addEventListener("popstate", sync);
    return () => window.removeEventListener("popstate", sync);
  }, []);

  const apply = (event: FormEvent) => { event.preventDefault(); setOffset(0); setSearch(searchDraft.trim()); };
  const clear = () => { setSearchDraft(""); setSearch(""); setActiveFilter(""); setOffset(0); window.history.replaceState(null, "", window.location.pathname); };

  return (
    <section className="admin-page-frame admin-page-frame-wide" aria-labelledby="admin-users-title">
      <AdminPageHeader actions={<button className="button" onClick={() => void load()} type="button"><Icon name="refresh" size={16} />刷新列表</button>} description="查看注册身份、邮箱验证、所属工作区并处理账号启停。" title="用户" />
      <form className="admin-filter-bar" onSubmit={apply}>
        <label className="admin-search-field"><span className="sr-only">搜索用户</span><Icon name="search" size={16} /><input onChange={(event) => setSearchDraft(event.target.value)} placeholder="搜索姓名、邮箱或用户 ID" value={searchDraft} /></label>
        <label><span className="sr-only">账号状态</span><select className="select-field" onChange={(event) => { setActiveFilter(event.target.value as typeof activeFilter); setOffset(0); }} value={activeFilter}><option value="">全部状态</option><option value="true">正常</option><option value="false">已停用</option></select></label>
        <button className="button button-primary" type="submit">筛选用户</button>
        {(search || activeFilter) && <button className="button button-ghost" onClick={clear} type="button">清除条件</button>}
      </form>
      <div className="admin-master-detail">
        <section className="admin-list-pane" aria-label="用户列表">
          <div className="admin-list-summary"><span>用户</span><strong>{numberFormat(total)}</strong></div>
          {state === "loading" && <AdminLoading label="正在读取用户…" />}
          {state === "error" && <AdminError message={error} onRetry={() => void load()} />}
          {state === "ready" && !items.length && <AdminEmpty action={(search || activeFilter) ? <button className="button" onClick={clear} type="button">清除筛选条件</button> : undefined} description="没有用户符合当前条件。" title="没有找到用户" />}
          {state === "ready" && !!items.length && <div className="admin-user-list">{items.map((user) => (
            <button aria-current={selectedId === user.user_id ? "true" : undefined} className={`admin-user-row${selectedId === user.user_id ? " is-selected" : ""}`} key={user.user_id} onClick={() => setSelectedId(user.user_id)} type="button">
              <span className="admin-avatar" aria-hidden="true">{user.full_name.trim().slice(0, 1) || "用"}</span>
              <span><strong>{user.full_name || "未命名用户"}</strong><small>{user.email}</small></span>
              <span><strong>{user.membership_count}</strong><small>工作区</small></span>
              <AdminStatus status={user.is_active ? "active" : "inactive"} />
              <Icon name="chevron-right" size={17} />
            </button>
          ))}</div>}
          {state === "ready" && !!items.length && <AdminPagination limit={PAGE_SIZE} offset={offset} onChange={setOffset} total={total} />}
        </section>
        <UserDetailPane onUpdated={(updated) => setItems((current) => current.map((item) => item.user_id === updated.user_id ? updated : item))} userId={selectedId} />
      </div>
    </section>
  );
}

export function AdminPlansPage() {
  const [state, setState] = useState<RequestState>("loading");
  const [error, setError] = useState("");
  const [plans, setPlans] = useState<ProductPlan[]>([]);
  const [selectedCode, setSelectedCode] = useState<string | null>(null);
  const [draft, setDraft] = useState<ProductPlan | null>(null);
  const [priceYuan, setPriceYuan] = useState("");
  const [reason, setReason] = useState("");
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState("");

  const load = useCallback(async () => {
    setState("loading");
    setError("");
    try {
      const next = (await adminApi.listPlans()).sort((a, b) => a.sort_order - b.sort_order);
      setPlans(next);
      setSelectedCode((current) => current && next.some((plan) => plan.code === current) ? current : next[0]?.code ?? null);
      setState("ready");
    } catch (loadError) {
      setError(adminErrorMessage(loadError));
      setState("error");
    }
  }, []);

  useEffect(() => { void load(); }, [load]);
  const selected = useMemo(() => plans.find((plan) => plan.code === selectedCode) ?? null, [plans, selectedCode]);
  useEffect(() => {
    setDraft(selected ? { ...selected, feature_flags: { ...selected.feature_flags } } : null);
    setPriceYuan(selected ? (selected.monthly_price_cents / 100).toFixed(2) : "");
    setReason("");
    setNotice("");
  }, [selected]);

  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (!draft || !reason.trim()) return;
    setSaving(true);
    setError("");
    setNotice("");
    try {
      const updated = await adminApi.updatePlan(draft.code, {
        name: draft.name.trim(),
        monthly_price_cents: Math.round(Number(priceYuan) * 100),
        trial_days: draft.trial_days,
        feature_flags: draft.feature_flags,
        is_active: draft.is_active,
        is_available_for_signup: draft.is_available_for_signup,
        is_default_trial: draft.is_default_trial,
        sort_order: draft.sort_order,
        reason: reason.trim(),
      });
      setPlans((current) => current.map((plan) => plan.code === updated.code ? updated : plan).sort((a, b) => a.sort_order - b.sort_order));
      setDraft({ ...updated, feature_flags: { ...updated.feature_flags } });
      setPriceYuan((updated.monthly_price_cents / 100).toFixed(2));
      setReason("");
      setNotice("套餐设置已保存，新的注册和工作区授权将使用这份配置。 ");
    } catch (saveError) {
      setError(adminErrorMessage(saveError));
    } finally {
      setSaving(false);
    }
  };

  return (
    <section className="admin-page-frame" aria-labelledby="admin-plans-title">
      <AdminPageHeader actions={<button className="button" onClick={() => void load()} type="button"><Icon name="refresh" size={16} />刷新套餐</button>} description="管理价格、默认试用期、注册可见性和版本功能范围。" title="套餐与试用" />
      {state === "loading" && <div className="admin-panel"><AdminLoading label="正在读取套餐…" /></div>}
      {state === "error" && <div className="admin-panel"><AdminError message={error} onRetry={() => void load()} /></div>}
      {state === "ready" && !plans.length && <div className="admin-panel"><AdminEmpty description="系统还没有可配置的产品套餐。" title="没有套餐" /></div>}
      {state === "ready" && !!plans.length && (
        <div className="admin-plan-layout">
          <nav aria-label="套餐列表" className="admin-plan-list">{plans.map((plan) => (
            <button aria-current={selectedCode === plan.code ? "page" : undefined} className={selectedCode === plan.code ? "is-selected" : ""} key={plan.code} onClick={() => setSelectedCode(plan.code)} type="button">
              <span><strong>{plan.name}</strong><small>{plan.code} · ¥{(plan.monthly_price_cents / 100).toFixed(2)} / 月</small></span>
              <AdminStatus status={plan.is_active ? "active" : "inactive"} />
            </button>
          ))}</nav>
          {draft && <form className="admin-panel admin-plan-editor" onSubmit={(event) => void save(event)}>
            <div className="admin-section-heading"><div><h2>编辑 {draft.name}</h2><p>保存后立即成为服务端权威配置。</p></div><span>{draft.code}</span></div>
            <div className="admin-form-grid">
              <label><span>套餐名称</span><input className="field" maxLength={120} onChange={(event) => setDraft({ ...draft, name: event.target.value })} required value={draft.name} /></label>
              <label><span>月价（人民币元）</span><input className="field" min="0" onChange={(event) => setPriceYuan(event.target.value)} required step="0.01" type="number" value={priceYuan} /></label>
              <label><span>默认试用天数</span><input className="field" max="365" min="0" onChange={(event) => setDraft({ ...draft, trial_days: Number(event.target.value) })} required type="number" value={draft.trial_days} /></label>
              <label><span>显示顺序</span><input className="field" max="1000" min="0" onChange={(event) => setDraft({ ...draft, sort_order: Number(event.target.value) })} required type="number" value={draft.sort_order} /></label>
            </div>
            <fieldset className="admin-toggle-group"><legend>可用范围</legend>
              <label><input checked={draft.is_active} onChange={(event) => setDraft({ ...draft, is_active: event.target.checked })} type="checkbox" /><span><strong>启用套餐</strong><small>允许既有工作区继续使用。</small></span></label>
              <label><input checked={draft.is_available_for_signup} onChange={(event) => setDraft({ ...draft, is_available_for_signup: event.target.checked })} type="checkbox" /><span><strong>开放注册选择</strong><small>新用户可以在注册流程获得此套餐。</small></span></label>
              <label><input checked={draft.is_default_trial} onChange={(event) => setDraft({ ...draft, is_default_trial: event.target.checked })} type="checkbox" /><span><strong>默认试用套餐</strong><small>注册成功后的默认体验版本。</small></span></label>
            </fieldset>
            <fieldset className="admin-feature-flags"><legend>功能范围</legend>
              {Object.keys(draft.feature_flags).length ? Object.entries(draft.feature_flags).sort(([a], [b]) => a.localeCompare(b)).map(([flag, enabled]) => (
                <label key={flag}><input checked={enabled} onChange={(event) => setDraft({ ...draft, feature_flags: { ...draft.feature_flags, [flag]: event.target.checked } })} type="checkbox" /><span>{planFeatureLabel(flag)}</span></label>
              )) : <p>此套餐没有单独的功能开关。</p>}
            </fieldset>
            <label className="admin-reason-field"><span>变更原因</span><textarea className="textarea-field" maxLength={500} onChange={(event) => setReason(event.target.value)} placeholder="填写定价调整、发布计划或客户政策依据" required rows={3} value={reason} /></label>
            {error && <p className="admin-form-error" role="alert">{error}</p>}
            {notice && <p className="admin-form-success" role="status">{notice}</p>}
            <div className="admin-form-actions"><button className="button" onClick={() => { if (!selected) return; setDraft({ ...selected, feature_flags: { ...selected.feature_flags } }); setPriceYuan((selected.monthly_price_cents / 100).toFixed(2)); setReason(""); setNotice(""); setError(""); }} type="button">撤销未保存修改</button><button className="button button-primary" disabled={saving || !draft.name.trim() || !reason.trim() || !Number.isFinite(Number(priceYuan))} type="submit">{saving ? <><i className="spinner" />正在保存</> : "保存套餐设置"}</button></div>
          </form>}
        </div>
      )}
    </section>
  );
}

type AiTab = "runs" | "usage" | "resources" | "configure";
const featureName = featureDisplayName;

function DataTableEmpty({ description }: { description: string }) {
  return <AdminEmpty description={description} title="当前没有记录" />;
}

function TokenUsageCell({
  invocationCount,
  tokenUsageInvocationCount,
  totalTokens,
  inputTokens,
  cachedReadInputTokens,
  cachedWriteInputTokens,
  outputTokens,
  reasoningTokens,
}: {
  invocationCount: number;
  tokenUsageInvocationCount: number;
  totalTokens: number;
  inputTokens?: number;
  cachedReadInputTokens?: number;
  cachedWriteInputTokens?: number;
  outputTokens?: number;
  reasoningTokens?: number;
}) {
  const hasProviderUsage = tokenUsageInvocationCount > 0;
  const isPartial = hasProviderUsage && tokenUsageInvocationCount < invocationCount;
  const hasBreakdown = inputTokens !== undefined && outputTokens !== undefined;
  const inputTotal = (inputTokens ?? 0) + (cachedReadInputTokens ?? 0) + (cachedWriteInputTokens ?? 0);
  const outputTotal = (outputTokens ?? 0) + (reasoningTokens ?? 0);
  const title = hasProviderUsage && hasBreakdown
    ? [
      `总计 ${numberFormat(totalTokens)} Token`,
      `输入（含缓存） ${numberFormat(inputTotal)} Token`,
      `输出及推理 ${numberFormat(outputTotal)} Token`,
      cachedReadInputTokens ? `缓存读取 ${numberFormat(cachedReadInputTokens)} Token` : "",
      cachedWriteInputTokens ? `缓存写入 ${numberFormat(cachedWriteInputTokens)} Token` : "",
      reasoningTokens ? `推理 ${numberFormat(reasoningTokens)} Token` : "",
    ].filter(Boolean).join("\n")
    : undefined;

  return (
    <span title={title}>
      <strong>{hasProviderUsage ? numberFormat(totalTokens) : "未返回"}</strong>
      <small>{hasProviderUsage ? `已返回 ${numberFormat(tokenUsageInvocationCount)}/${numberFormat(invocationCount)} 次${isPartial ? "，部分统计" : ""}` : "模型未提供用量"}</small>
      {hasProviderUsage && hasBreakdown && <small>输入 {numberFormat(inputTotal)} · 输出 {numberFormat(outputTotal)}</small>}
    </span>
  );
}

const DEFAULT_TOKEN_USAGE_RANGE_DAYS = 1;
const DEFAULT_TOKEN_TREND_GRANULARITY: AiUsageTrendGranularity = "hour";
const MAX_TOKEN_TREND_RANGE_DAYS: Record<AiUsageTrendGranularity, number> = {
  hour: 31,
  day: 90,
};

function localDateInputValue(date: Date) {
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 10);
}

function defaultTokenUsageRange() {
  const end = new Date();
  const start = new Date(end);
  start.setDate(start.getDate() - (DEFAULT_TOKEN_USAGE_RANGE_DAYS - 1));
  return { start: localDateInputValue(start), end: localDateInputValue(end) };
}

function localDayBoundaryIso(value: string, boundary: "start" | "end") {
  if (!value) return undefined;
  const time = boundary === "start" ? "00:00:00.000" : "23:59:59.999";
  const date = new Date(`${value}T${time}`);
  return Number.isNaN(date.getTime()) ? undefined : date.toISOString();
}

function tokenUsageRangeLabel(start: string, end: string) {
  if (!start && !end) return "全部时间";
  return `${start || "最早记录"} 至 ${end || "今天"}`;
}

type ModelScope = {
  providerSlug: string;
  modelSlug: string;
};

const MODEL_SCOPE_SEPARATOR = "::";

function parseModelScope(value: string): ModelScope | null {
  if (!value) return null;
  const [providerSlug, modelSlug, extra] = value.split(MODEL_SCOPE_SEPARATOR);
  if (!providerSlug || !modelSlug || extra !== undefined) return null;
  return { providerSlug, modelSlug };
}

function usageRangeDays(start: string, end: string) {
  const startTime = new Date(`${start}T00:00:00`).getTime();
  const endTime = new Date(`${end}T23:59:59.999`).getTime();
  if (Number.isNaN(startTime) || Number.isNaN(endTime)) return 0;
  return Math.floor((endTime - startTime) / (24 * 60 * 60 * 1_000)) + 1;
}

function browserTimeZone() {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}

export function AdminAiPage() {
  const initialUsageRange = useMemo(() => defaultTokenUsageRange(), []);
  const timeZone = useMemo(() => browserTimeZone(), []);
  const [state, setState] = useState<RequestState>("loading");
  const [error, setError] = useState("");
  const [filterError, setFilterError] = useState("");
  const [tab, setTab] = useState<AiTab>("runs");
  const [organizationDraft, setOrganizationDraft] = useState("");
  const [featureDraft, setFeatureDraft] = useState("");
  const [usageStartDraft, setUsageStartDraft] = useState(initialUsageRange.start);
  const [usageEndDraft, setUsageEndDraft] = useState(initialUsageRange.end);
  const [usageTrendGranularityDraft, setUsageTrendGranularityDraft] = useState<AiUsageTrendGranularity>(DEFAULT_TOKEN_TREND_GRANULARITY);
  const [modelScopeDraft, setModelScopeDraft] = useState("");
  const [organizationId, setOrganizationId] = useState("");
  const [feature, setFeature] = useState("");
  const [usageStart, setUsageStart] = useState(initialUsageRange.start);
  const [usageEnd, setUsageEnd] = useState(initialUsageRange.end);
  const [usageTrendGranularity, setUsageTrendGranularity] = useState<AiUsageTrendGranularity>(DEFAULT_TOKEN_TREND_GRANULARITY);
  const [modelScope, setModelScope] = useState("");
  const [runs, setRuns] = useState<AiRunUsage[]>([]);
  const [usage, setUsage] = useState<AiUsageAggregate[]>([]);
  const [usageTrend, setUsageTrend] = useState<AiUsageTrendBucket[]>([]);
  const [usageTrendState, setUsageTrendState] = useState<RequestState>("loading");
  const [usageTrendError, setUsageTrendError] = useState("");
  const [providers, setProviders] = useState<AiProviderProfile[]>([]);
  const [models, setModels] = useState<AiModelProfile[]>([]);
  const [routes, setRoutes] = useState<AiRoutePolicy[]>([]);

  const refreshData = useCallback(async () => {
    const selectedModelScope = parseModelScope(modelScope);
    const baseQuery = {
      organization_id: organizationId || undefined,
      feature: feature || undefined,
      limit: 100,
      offset: 0,
    };
    const usageQuery = {
      ...baseQuery,
      provider_slug: selectedModelScope?.providerSlug,
      model_slug: selectedModelScope?.modelSlug,
      started_at_from: localDayBoundaryIso(usageStart, "start"),
      started_at_to: localDayBoundaryIso(usageEnd, "end"),
    };
    const [nextRuns, nextUsage, nextProviders, nextModels, nextRoutes] = await Promise.all([
      adminApi.listAiRuns(baseQuery),
      adminApi.listAiUsage(usageQuery),
      adminApi.listAiProviders(),
      adminApi.listAiModels(),
      adminApi.listAiRoutes(),
    ]);
    setRuns(nextRuns);
    setUsage(nextUsage);
    setProviders(nextProviders);
    setModels(nextModels);
    setRoutes(nextRoutes);
  }, [feature, modelScope, organizationId, usageEnd, usageStart]);

  const refreshUsageTrend = useCallback(async () => {
    const selectedModelScope = parseModelScope(modelScope);
    setUsageTrendState("loading");
    setUsageTrendError("");
    try {
      const nextUsageTrend = await adminApi.listAiUsageTrend({
        organization_id: organizationId || undefined,
        feature: feature || undefined,
        provider_slug: selectedModelScope?.providerSlug,
        model_slug: selectedModelScope?.modelSlug,
        started_at_from: localDayBoundaryIso(usageStart, "start"),
        started_at_to: localDayBoundaryIso(usageEnd, "end"),
        granularity: usageTrendGranularity,
        time_zone: timeZone,
      });
      setUsageTrend(nextUsageTrend);
      setUsageTrendState("ready");
    } catch (trendError) {
      setUsageTrend([]);
      setUsageTrendError(adminErrorMessage(trendError));
      setUsageTrendState("error");
    }
  }, [feature, modelScope, organizationId, timeZone, usageEnd, usageStart, usageTrendGranularity]);

  const load = useCallback(async () => {
    setState("loading");
    setError("");
    try {
      await refreshData();
      if (tab === "usage") await refreshUsageTrend();
      setState("ready");
    } catch (loadError) {
      setError(adminErrorMessage(loadError));
      setState("error");
    }
  }, [refreshData, refreshUsageTrend, tab]);

  useEffect(() => { void load(); }, [load]);

  const providerNames = useMemo(
    () => new Map(providers.map((provider) => [provider.slug, providerDisplayName(provider)])),
    [providers],
  );
  const modelNames = useMemo(
    () => new Map(models.map((model) => [model.slug, modelDisplayName(model)])),
    [models],
  );
  const selectedModelScope = useMemo(() => parseModelScope(modelScope), [modelScope]);
  const selectedModel = useMemo(() => {
    if (!selectedModelScope) return null;
    return models.find((model) => (
      model.provider_slug === selectedModelScope.providerSlug
      && model.slug === selectedModelScope.modelSlug
    )) ?? null;
  }, [models, selectedModelScope]);
  const modelScopeOptions = useMemo(() => [...models].sort((left, right) => (
    `${left.provider_slug}/${modelDisplayName(left)}`.localeCompare(`${right.provider_slug}/${modelDisplayName(right)}`, "zh-CN")
  )), [models]);
  const featureFilterOptions = useMemo(() => {
    const options = new Map(aiFeatureFilterOptions.map((item) => [item.value, item.label]));
    routes.forEach((route) => options.set(route.feature, routeDisplayName(route)));
    if (featureDraft && !options.has(featureDraft)) options.set(featureDraft, "当前自定义功能");
    return [...options].map(([value, label]) => ({ value, label }));
  }, [featureDraft, routes]);
  const tokenUsageScopeLabel = selectedModelScope
    ? selectedModel
      ? `${providerNames.get(selectedModelScope.providerSlug) ?? "模型服务"} · ${modelDisplayName(selectedModel)}`
      : "已归档模型"
    : "全部模型";
  const tokenSummary = useMemo(() => usage.reduce((summary, item) => ({
    invocationCount: summary.invocationCount + item.invocation_count,
    tokenUsageInvocationCount: summary.tokenUsageInvocationCount + item.token_usage_invocation_count,
    inputTokens: summary.inputTokens + item.input_tokens,
    cachedReadTokens: summary.cachedReadTokens + item.cached_read_input_tokens,
    cachedWriteTokens: summary.cachedWriteTokens + item.cached_write_input_tokens,
    outputTokens: summary.outputTokens + item.output_tokens + item.reasoning_tokens,
    totalTokens: summary.totalTokens + item.total_tokens,
  }), {
    invocationCount: 0,
    tokenUsageInvocationCount: 0,
    inputTokens: 0,
    cachedReadTokens: 0,
    cachedWriteTokens: 0,
    outputTokens: 0,
    totalTokens: 0,
  }), [usage]);
  const modelCount = useMemo(
    () => new Set(usage.map((item) => `${item.provider_slug}/${item.model_slug}`)).size,
    [usage],
  );
  const isDefaultUsageRange = (
    usageStart === initialUsageRange.start
    && usageEnd === initialUsageRange.end
  );
  const hasActiveFilters = Boolean(
    organizationId
    || feature
    || (tab === "usage" && (modelScope || !isDefaultUsageRange || usageTrendGranularity !== DEFAULT_TOKEN_TREND_GRANULARITY)),
  );
  const usageRange = tokenUsageRangeLabel(usageStart, usageEnd);

  const apply = (event: FormEvent) => {
    event.preventDefault();
    if (tab === "usage" && (!usageStartDraft || !usageEndDraft)) {
      setFilterError("趋势图需要同时选择开始和结束日期。");
      return;
    }
    if (usageStartDraft && usageEndDraft && usageStartDraft > usageEndDraft) {
      setFilterError("结束日期不能早于开始日期。");
      return;
    }
    const maxTrendRangeDays = MAX_TOKEN_TREND_RANGE_DAYS[usageTrendGranularityDraft];
    if (tab === "usage" && usageRangeDays(usageStartDraft, usageEndDraft) > maxTrendRangeDays) {
      setFilterError(`按${usageTrendGranularityDraft === "hour" ? "小时" : "天"}最多支持 ${maxTrendRangeDays} 天，请缩小日期范围或切换粒度。`);
      return;
    }
    setFilterError("");
    setOrganizationId(organizationDraft.trim());
    setFeature(featureDraft.trim());
    setUsageStart(usageStartDraft);
    setUsageEnd(usageEndDraft);
    setUsageTrendGranularity(usageTrendGranularityDraft);
    setModelScope(modelScopeDraft);
  };

  const clearFilters = () => {
    setFilterError("");
    setOrganizationDraft("");
    setFeatureDraft("");
    setUsageStartDraft(initialUsageRange.start);
    setUsageEndDraft(initialUsageRange.end);
    setUsageTrendGranularityDraft(DEFAULT_TOKEN_TREND_GRANULARITY);
    setModelScopeDraft("");
    setOrganizationId("");
    setFeature("");
    setUsageStart(initialUsageRange.start);
    setUsageEnd(initialUsageRange.end);
    setUsageTrendGranularity(DEFAULT_TOKEN_TREND_GRANULARITY);
    setModelScope("");
  };

  return (
    <section className="admin-page-frame admin-page-frame-wide" aria-labelledby="admin-ai-title">
      <AdminPageHeader
        actions={<button className="button" onClick={() => void load()} type="button"><Icon name="refresh" size={16} />刷新 AI 数据</button>}
        description="查看平台 AI 调用、Token 使用和模型服务状态。"
        title="AI 运营"
      />
      <div className="admin-segmented" aria-label="AI 运营视图">
        {([['runs', '运行记录'], ['usage', 'Token 用量'], ['resources', '模型与功能'], ['configure', '配置与发布']] as Array<[AiTab, string]>).map(([value, label]) => (
          <button aria-pressed={tab === value} key={value} onClick={() => setTab(value)} type="button">{label}</button>
        ))}
      </div>
      {(tab === "runs" || tab === "usage") && (
        <form className="admin-filter-bar" onSubmit={apply}>
          <label className="admin-search-field"><span className="sr-only">工作区 ID</span><Icon name="briefcase" size={16} /><input onChange={(event) => setOrganizationDraft(event.target.value)} placeholder="工作区 ID" value={organizationDraft} /></label>
          <label className="admin-model-scope-field"><span>AI 功能</span><select className="select-field" onChange={(event) => setFeatureDraft(event.target.value)} value={featureDraft}><option value="">全部功能</option>{featureFilterOptions.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}</select></label>
          {tab === "usage" && <>
            <label className="admin-date-field"><span>开始日期</span><input className="field" onChange={(event) => { setUsageStartDraft(event.target.value); setFilterError(""); }} type="date" value={usageStartDraft} /></label>
            <label className="admin-date-field"><span>结束日期</span><input className="field" onChange={(event) => { setUsageEndDraft(event.target.value); setFilterError(""); }} type="date" value={usageEndDraft} /></label>
            <fieldset aria-describedby="admin-token-trend-granularity-note" className="admin-token-trend-granularity">
              <legend>趋势粒度</legend>
              <div aria-label="趋势粒度" className="admin-segmented" role="group">
                <button aria-pressed={usageTrendGranularityDraft === "hour"} onClick={() => { setUsageTrendGranularityDraft("hour"); setFilterError(""); }} type="button">按小时</button>
                <button aria-pressed={usageTrendGranularityDraft === "day"} onClick={() => { setUsageTrendGranularityDraft("day"); setFilterError(""); }} type="button">按天</button>
              </div>
              <small id="admin-token-trend-granularity-note">按小时最多 31 天，按天最多 90 天。</small>
            </fieldset>
            <label className="admin-model-scope-field"><span>模型</span><select className="select-field" onChange={(event) => setModelScopeDraft(event.target.value)} value={modelScopeDraft}><option value="">全部模型</option>{modelScopeDraft && !modelScopeOptions.some((model) => `${model.provider_slug}${MODEL_SCOPE_SEPARATOR}${model.slug}` === modelScopeDraft) && <option value={modelScopeDraft}>已归档模型</option>}{modelScopeOptions.map((model) => <option key={model.model_id} value={`${model.provider_slug}${MODEL_SCOPE_SEPARATOR}${model.slug}`}>{providerNames.get(model.provider_slug) ?? "模型服务"} · {modelDisplayName(model)}</option>)}</select></label>
          </>}
          <button className="button button-primary" type="submit">{tab === "usage" ? "查看 Token" : "筛选记录"}</button>
          {hasActiveFilters && <button className="button button-ghost" onClick={clearFilters} type="button">清除条件</button>}
          {filterError && <p className="admin-form-error admin-filter-error" role="alert">{filterError}</p>}
        </form>
      )}
      {state === "loading" && <div className="admin-panel"><AdminLoading label="正在汇总 AI 运行数据…" /></div>}
      {state === "error" && <div className="admin-panel"><AdminError message={error} onRetry={() => void load()} /></div>}
      {state === "ready" && tab === "runs" && (
        <div className="admin-table-panel">
          <div className="admin-table-note"><span>最近 {runs.length} 条运行</span><small>不含提示词、简历或模型输出；模型 Token 请在「Token 用量」按模型服务和模型查看。</small></div>
          {runs.length ? <div className="admin-data-table-scroll"><table className="admin-data-table"><thead><tr><th>开始时间</th><th>工作区</th><th>功能</th><th>服务</th><th>状态</th><th>调用</th></tr></thead><tbody>{runs.map((run) => <tr key={run.run_id}><td>{formatDate(run.started_at, true)}</td><td title={run.organization_id}>{shortId(run.organization_id)}</td><td>{featureName(run.feature)}</td><td>{serviceKindDisplayName(run.service_kind)}</td><td><AdminStatus status={run.status} /></td><td>{numberFormat(run.invocation_count)}</td></tr>)}</tbody></table></div> : <DataTableEmpty description="当前筛选范围内没有 AI 运行记录。" />}
        </div>
      )}
      {state === "ready" && tab === "usage" && (
        <>
          <section aria-label="当前 Token 统计区间" className="admin-token-summary">
            <div><span>统计区间</span><strong>{usageRange}</strong><small>{usageTrendGranularity === "hour" ? "按小时统计" : "按天统计"}</small></div>
            <div><span>已用 Token</span><strong>{numberFormat(tokenSummary.totalTokens)} Token</strong><small>输入 {numberFormat(tokenSummary.inputTokens)} · 输出 {numberFormat(tokenSummary.outputTokens)} · 缓存 {numberFormat(tokenSummary.cachedReadTokens + tokenSummary.cachedWriteTokens)}</small></div>
            <div><span>已统计调用</span><strong>{numberFormat(tokenSummary.tokenUsageInvocationCount)} / {numberFormat(tokenSummary.invocationCount)} 次</strong><small>{selectedModelScope ? tokenUsageScopeLabel : `${numberFormat(modelCount)} 个模型`}</small></div>
          </section>
          {usageTrendState === "loading" && <section className="admin-token-trend-panel"><AdminLoading label="正在汇总 Token 趋势…" /></section>}
          {usageTrendState === "error" && <section className="admin-token-trend-panel"><AdminError message={usageTrendError} onRetry={() => void refreshUsageTrend()} /></section>}
          {usageTrendState === "ready" && <TokenUsageTrendChart buckets={usageTrend} granularity={usageTrendGranularity} rangeEnd={usageEnd} rangeLabel={usageRange} rangeStart={usageStart} scopeLabel={tokenUsageScopeLabel} />}
          <div className="admin-table-panel">
            <div className="admin-table-note"><span>按工作区、功能与模型拆分</span><small>仅统计模型实际返回的 Token。</small></div>
            {usage.length ? <div className="admin-data-table-scroll"><table className="admin-data-table admin-token-usage-table"><thead><tr><th>工作区</th><th>功能</th><th>模型服务</th><th>模型</th><th>调用</th><th title="仅统计模型实际返回 usage 的调用">Token</th></tr></thead><tbody>{usage.map((item, index) => <tr key={`${item.organization_id}-${item.feature}-${item.provider_slug}-${item.model_slug}-${index}`}><td title={item.organization_id}>{shortId(item.organization_id)}</td><td>{featureName(item.feature)}</td><td><strong>{providerNames.get(item.provider_slug) ?? "模型服务"}</strong></td><td><strong>{modelNames.get(item.model_slug) ?? "已归档模型"}</strong></td><td>{numberFormat(item.invocation_count)}</td><td><TokenUsageCell cachedReadInputTokens={item.cached_read_input_tokens} cachedWriteInputTokens={item.cached_write_input_tokens} inputTokens={item.input_tokens} invocationCount={item.invocation_count} outputTokens={item.output_tokens} reasoningTokens={item.reasoning_tokens} tokenUsageInvocationCount={item.token_usage_invocation_count} totalTokens={item.total_tokens} /></td></tr>)}</tbody></table></div> : <DataTableEmpty description="当前时间区间内没有可汇总的模型 Token 用量。" />}
          </div>
        </>
      )}
      {state === "ready" && tab === "resources" && <div className="admin-resource-stack">
        <section className="admin-table-panel">
          <div className="admin-table-note"><span>模型服务</span><small>{providers.length} 个已接入服务</small></div>
          {providers.length ? <ul className="admin-resource-list">{providers.map((item) => <li key={item.provider_id}><span><strong>{providerDisplayName(item)}</strong><small>{item.credential_configured ? "服务端密钥已配置" : "等待服务端密钥配置"}</small></span><AdminStatus status={item.is_enabled && item.credential_configured ? "verified" : item.is_enabled ? "warning" : "disabled"} label={item.is_enabled && item.credential_configured ? "已连接" : item.is_enabled ? "待配置" : "已停用"} /></li>)}</ul> : <DataTableEmpty description="尚未接入模型服务。" />}
        </section>
        <section className="admin-table-panel">
          <div className="admin-table-note"><span>可用模型</span><small>{models.length} 个模型</small></div>
          {models.length ? <ul className="admin-resource-list">{models.map((item) => <li key={item.model_id}><span><strong>{modelDisplayName(item)}</strong><small>模型服务：{providerNames.get(item.provider_slug) ?? "模型服务"}</small></span><AdminStatus status={item.is_enabled ? "enabled" : "disabled"} label={item.is_enabled ? "可用" : "已停用"} /></li>)}</ul> : <DataTableEmpty description="尚未配置模型。" />}
        </section>
        <section className="admin-table-panel">
          <div className="admin-table-note"><span>功能调用规则</span><small>{routes.length} 项功能</small></div>
          {routes.length ? <ul className="admin-resource-list">{routes.map((item) => <li key={item.policy_id}><span><strong>{routeDisplayName(item)}</strong><small>{routeDisplayDescription(item)} · 当前版本 {item.current_version ?? "—"}</small></span><AdminStatus status={item.is_enabled ? "enabled" : "disabled"} label={item.is_enabled ? "已启用" : "已停用"} /></li>)}</ul> : <DataTableEmpty description="尚未发布功能调用规则。" />}
        </section>
      </div>}
      {state === "ready" && tab === "configure" && <AdminAiConfigurationPanel models={models} onChanged={refreshData} providers={providers} routes={routes} />}
    </section>
  );
}

function auditActionName(action: string) {
  const names: Record<string,string> = { "organization.updated": "修改工作区", "organization.plan_assigned": "调整工作区套餐", "user.activation_changed": "修改用户状态", "product_plan.updated": "修改套餐", "ai_route.published": "发布 AI 规则", "ai_provider.created": "接入模型服务", "ai_model.created": "创建模型", "ai_model_price.created": "创建模型价格" };
  return names[action] || action.replaceAll("_", " ");
}

function statePreview(value: Record<string, unknown> | null) {
  return value ? JSON.stringify(value, null, 2) : "无";
}

export function AdminAuditPage() {
  const [state, setState] = useState<RequestState>("loading");
  const [error, setError] = useState("");
  const [items, setItems] = useState<PlatformAuditEvent[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [organizationDraft, setOrganizationDraft] = useState("");
  const [actionDraft, setActionDraft] = useState("");
  const [organizationId, setOrganizationId] = useState("");
  const [action, setAction] = useState("");

  const load = useCallback(async () => {
    setState("loading"); setError("");
    try { const page = await adminApi.listAuditEvents({ organization_id: organizationId || undefined, action: action || undefined, limit: PAGE_SIZE, offset }); setItems(page.items); setTotal(page.total); setState("ready"); }
    catch (loadError) { setError(adminErrorMessage(loadError)); setState("error"); }
  }, [action, offset, organizationId]);
  useEffect(() => { void load(); }, [load]);
  const apply = (event: FormEvent) => { event.preventDefault(); setOffset(0); setOrganizationId(organizationDraft.trim()); setAction(actionDraft.trim()); };

  return (
    <section className="admin-page-frame admin-page-frame-wide" aria-labelledby="admin-audit-title">
      <AdminPageHeader actions={<button className="button" onClick={() => void load()} type="button"><Icon name="refresh" size={16} />刷新审计</button>} description="追踪跨工作区的平台变更，核对操作原因和前后状态。" title="操作审计" />
      <form className="admin-filter-bar" onSubmit={apply}><label className="admin-search-field"><span className="sr-only">工作区 ID</span><Icon name="briefcase" size={16} /><input onChange={(event) => setOrganizationDraft(event.target.value)} placeholder="工作区 ID" value={organizationDraft} /></label><label className="admin-search-field"><span className="sr-only">操作名称</span><Icon name="history" size={16} /><input onChange={(event) => setActionDraft(event.target.value)} placeholder="操作代码，例如 organization.updated" value={actionDraft} /></label><button className="button button-primary" type="submit">筛选审计</button>{(organizationId || action) && <button className="button button-ghost" onClick={() => { setOrganizationDraft(""); setActionDraft(""); setOrganizationId(""); setAction(""); setOffset(0); }} type="button">清除条件</button>}</form>
      {state === "loading" && <div className="admin-panel"><AdminLoading label="正在读取操作审计…" /></div>}
      {state === "error" && <div className="admin-panel"><AdminError message={error} onRetry={() => void load()} /></div>}
      {state === "ready" && !items.length && <div className="admin-panel"><AdminEmpty description="当前筛选范围内没有平台变更记录。" title="没有审计记录" /></div>}
      {state === "ready" && !!items.length && <div className="admin-table-panel"><div className="admin-data-table-scroll"><table className="admin-data-table admin-audit-table"><thead><tr><th>时间</th><th>操作人</th><th>操作</th><th>目标</th><th>工作区</th><th>原因</th><th>变更</th></tr></thead><tbody>{items.map((item) => <tr key={item.audit_id}><td>{formatDate(item.created_at,true)}</td><td title={item.actor_user_id || undefined}>{shortId(item.actor_user_id)}</td><td>{auditActionName(item.action)}</td><td><strong>{item.target_type}</strong><small title={item.target_id || undefined}>{shortId(item.target_id)}</small></td><td title={item.organization_id || undefined}>{shortId(item.organization_id)}</td><td className="admin-audit-reason">{item.reason || "未填写"}</td><td><details className="admin-audit-change"><summary>查看变更</summary><div><section><h3>修改前</h3><pre>{statePreview(item.before_state)}</pre></section><section><h3>修改后</h3><pre>{statePreview(item.after_state)}</pre></section><p>请求编号：{item.request_id || "—"}</p></div></details></td></tr>)}</tbody></table></div><AdminPagination limit={PAGE_SIZE} offset={offset} onChange={setOffset} total={total} /></div>}
    </section>
  );
}
