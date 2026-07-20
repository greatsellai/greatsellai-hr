import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import { Icon } from "../../icons";
import { adminApi, adminErrorMessage } from "../admin-api";
import type {
  PlatformOrganizationDetail,
  PlatformOrganizationSummary,
  PlatformPlanStatus,
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

const PAGE_SIZE = 30;

function initialSearch() {
  return new URLSearchParams(window.location.search).get("search")?.trim() || "";
}

function toDateTimeLocal(value: string | null) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 16);
}

function toIsoDate(value: string) {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date.toISOString();
}

function OrganizationDetailPanel({
  organizationId,
  plans,
  onUpdated,
}: {
  organizationId: string | null;
  plans: ProductPlan[];
  onUpdated: (organization: PlatformOrganizationDetail) => void;
}) {
  const [state, setState] = useState<RequestState>("idle");
  const [detail, setDetail] = useState<PlatformOrganizationDetail | null>(null);
  const [error, setError] = useState("");
  const [name, setName] = useState("");
  const [planCode, setPlanCode] = useState("");
  const [planStatus, setPlanStatus] = useState<PlatformPlanStatus>("trial");
  const [trialEndsAt, setTrialEndsAt] = useState("");
  const [reason, setReason] = useState("");
  const [confirmationName, setConfirmationName] = useState("");
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState("");

  const load = useCallback(async () => {
    if (!organizationId) {
      setDetail(null);
      setState("idle");
      return;
    }
    setState("loading");
    setError("");
    setNotice("");
    try {
      const next = await adminApi.getOrganization(organizationId);
      setDetail(next);
      setName(next.name);
      setPlanCode(next.plan_code || "");
      setPlanStatus(next.plan_status);
      setTrialEndsAt(toDateTimeLocal(next.trial_ends_at));
      setReason("");
      setConfirmationName("");
      setState("ready");
    } catch (loadError) {
      setError(adminErrorMessage(loadError));
      setState("error");
    }
  }, [organizationId]);

  useEffect(() => { void load(); }, [load]);

  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (!detail || !reason.trim()) return;
    setSaving(true);
    setError("");
    setNotice("");
    try {
      const nextTrialEndsAt = toIsoDate(trialEndsAt);
      const currentTrialTime = detail.trial_ends_at ? new Date(detail.trial_ends_at).getTime() : null;
      const nextTrialTime = nextTrialEndsAt ? new Date(nextTrialEndsAt).getTime() : null;
      const currentTrialMinute = currentTrialTime === null ? null : Math.floor(currentTrialTime / 60_000);
      const nextTrialMinute = nextTrialTime === null ? null : Math.floor(nextTrialTime / 60_000);
      const requiresConfirmation = (
        (["expired", "suspended"].includes(planStatus) && planStatus !== detail.plan_status) ||
        (nextTrialMinute !== null && currentTrialMinute !== null && nextTrialMinute < currentTrialMinute)
      );
      if (requiresConfirmation && confirmationName.trim() !== detail.name) {
        setError(`请输入工作区名称“${detail.name}”确认高风险变更。`);
        return;
      }
      const update: Parameters<typeof adminApi.updateOrganization>[1] = { reason: reason.trim() };
      if (name.trim() !== detail.name) update.name = name.trim();
      if (planCode !== (detail.plan_code || "")) update.plan_code = planCode;
      if (planStatus !== detail.plan_status && planStatus !== "legacy") update.plan_status = planStatus;
      if (nextTrialMinute !== currentTrialMinute) update.trial_ends_at = nextTrialEndsAt;
      if (requiresConfirmation) update.confirmation_name = confirmationName.trim();
      if (Object.keys(update).length === 1) {
        setError("没有检测到需要保存的工作区变更。");
        return;
      }
      const updated = await adminApi.updateOrganization(detail.organization_id, update);
      setDetail(updated);
      setName(updated.name);
      setPlanCode(updated.plan_code || "");
      setPlanStatus(updated.plan_status);
      setTrialEndsAt(toDateTimeLocal(updated.trial_ends_at));
      setReason("");
      setConfirmationName("");
      setNotice("工作区设置已保存，变更已写入操作审计。 ");
      onUpdated(updated);
    } catch (saveError) {
      setError(adminErrorMessage(saveError));
    } finally {
      setSaving(false);
    }
  };

  if (!organizationId) {
    return (
      <aside className="admin-detail-pane">
        <AdminEmpty description="从左侧列表选择一个工作区，查看套餐、试用、成员和用量。" title="选择工作区" />
      </aside>
    );
  }
  if (state === "loading") return <aside className="admin-detail-pane"><AdminLoading label="正在读取工作区详情…" /></aside>;
  if (state === "error") return <aside className="admin-detail-pane"><AdminError message={error} onRetry={() => void load()} /></aside>;
  if (!detail) return null;

  const inactiveMembers = detail.members.filter((member) => !member.is_active || !member.user_is_active).length;
  const currentTrialTime = detail.trial_ends_at ? new Date(detail.trial_ends_at).getTime() : null;
  const draftTrialEndsAt = toIsoDate(trialEndsAt);
  const draftTrialTime = draftTrialEndsAt ? new Date(draftTrialEndsAt).getTime() : null;
  const currentTrialMinute = currentTrialTime === null ? null : Math.floor(currentTrialTime / 60_000);
  const draftTrialMinute = draftTrialTime === null ? null : Math.floor(draftTrialTime / 60_000);
  const requiresConfirmation = (
    (["expired", "suspended"].includes(planStatus) && planStatus !== detail.plan_status) ||
    (draftTrialMinute !== null && currentTrialMinute !== null && draftTrialMinute < currentTrialMinute)
  );
  return (
    <aside className="admin-detail-pane" aria-labelledby="organization-detail-title">
      <header className="admin-detail-header">
        <div>
          <span className="admin-detail-label">工作区详情</span>
          <h2 id="organization-detail-title">{detail.name}</h2>
          <p title={detail.organization_id}>{shortId(detail.organization_id)}</p>
        </div>
        <AdminStatus status={detail.plan_status} />
      </header>

      <dl className="admin-detail-metrics">
        <div><dt>简历</dt><dd>{numberFormat(detail.resume_count)}</dd></div>
        <div><dt>岗位</dt><dd>{numberFormat(detail.job_count)}</dd></div>
        <div><dt>邮箱</dt><dd>{numberFormat(detail.mailbox_count)}</dd></div>
        <div><dt>AI 运行</dt><dd>{numberFormat(detail.ai_run_count)}</dd></div>
      </dl>

      <section className="admin-detail-section">
        <div className="admin-detail-section-heading"><h3>当前方案</h3></div>
        <dl className="admin-fact-list">
          <div><dt>套餐</dt><dd>{detail.plan_name || detail.plan_code || "未分配"}</dd></div>
          <div><dt>试用开始</dt><dd>{formatDate(detail.trial_started_at)}</dd></div>
          <div><dt>试用截止</dt><dd>{formatDate(detail.trial_ends_at)}</dd></div>
          <div><dt>成员</dt><dd>{detail.active_member_count} / {detail.member_count} 活跃</dd></div>
          <div><dt>创建时间</dt><dd>{formatDate(detail.created_at, true)}</dd></div>
        </dl>
      </section>

      <details className="admin-management-section">
        <summary><span><Icon name="gear" size={17} />管理工作区</span><Icon name="chevron-down" size={17} /></summary>
        <form className="admin-management-form" onSubmit={(event) => void save(event)}>
          <label>
            <span>工作区名称</span>
            <input className="field" maxLength={200} onChange={(event) => setName(event.target.value)} required value={name} />
          </label>
          <div className="admin-form-grid">
            <label>
              <span>套餐</span>
              <select className="select-field" onChange={(event) => setPlanCode(event.target.value)} required value={planCode}>
                <option value="" disabled>选择套餐</option>
                {plans.map((plan) => <option key={plan.code} value={plan.code}>{plan.name}</option>)}
              </select>
            </label>
            <label>
              <span>访问状态</span>
              <select className="select-field" onChange={(event) => setPlanStatus(event.target.value as PlatformPlanStatus)} value={planStatus}>
                <option value="trial">试用中</option>
                <option value="active">正常</option>
                <option value="expired">已到期</option>
                <option value="suspended">已暂停</option>
                <option disabled={detail.plan_status !== "legacy"} value="legacy">兼容模式（仅历史工作区）</option>
              </select>
            </label>
          </div>
          <label>
            <span>试用截止时间</span>
            <input className="field" onChange={(event) => setTrialEndsAt(event.target.value)} type="datetime-local" value={trialEndsAt} />
          </label>
          {planStatus === "suspended" && <p className="admin-form-warning">暂停后，该工作区成员将无法继续使用招聘功能，数据仍会保留。</p>}
          {requiresConfirmation && <label>
            <span>输入工作区名称确认</span>
            <input
              className="field"
              onChange={(event) => setConfirmationName(event.target.value)}
              placeholder={detail.name}
              required
              value={confirmationName}
            />
          </label>}
          <label>
            <span>变更原因</span>
            <textarea
              aria-describedby="organization-reason-help"
              className="textarea-field"
              maxLength={500}
              onChange={(event) => setReason(event.target.value)}
              placeholder="例如：客户已确认续费，延长试用至合同生效日"
              required
              rows={3}
              value={reason}
            />
          </label>
          <p className="admin-field-help" id="organization-reason-help">原因会和修改前后的数据一起进入操作审计。</p>
          {error && <p className="admin-form-error" role="alert">{error}</p>}
          {notice && <p className="admin-form-success" role="status">{notice}</p>}
          <button className="button button-primary" disabled={saving || !name.trim() || !planCode || !reason.trim() || (requiresConfirmation && confirmationName.trim() !== detail.name)} type="submit">
            {saving ? <><i className="spinner" />正在保存</> : "保存工作区设置"}
          </button>
        </form>
      </details>

      <section className="admin-detail-section">
        <div className="admin-detail-section-heading">
          <h3>成员</h3>
          {inactiveMembers > 0 && <span>{inactiveMembers} 个停用身份</span>}
        </div>
        {detail.members.length ? (
          <ul className="admin-member-list">
            {detail.members.map((member) => (
              <li key={member.membership_id}>
                <span className="admin-avatar" aria-hidden="true">{member.full_name.trim().slice(0, 1) || "用"}</span>
                <span><strong>{member.full_name || "未命名用户"}</strong><small>{member.email}</small></span>
                <span><small>{member.role === "admin" ? "管理员" : "招聘官"}</small><AdminStatus status={member.is_active && member.user_is_active ? "active" : "inactive"} /></span>
              </li>
            ))}
          </ul>
        ) : <p className="admin-inline-empty">这个工作区还没有成员。</p>}
      </section>
    </aside>
  );
}

export function AdminOrganizationsPage() {
  const [state, setState] = useState<RequestState>("loading");
  const [error, setError] = useState("");
  const [items, setItems] = useState<PlatformOrganizationSummary[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [plans, setPlans] = useState<ProductPlan[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [searchDraft, setSearchDraft] = useState(initialSearch);
  const [search, setSearch] = useState(initialSearch);
  const [planCode, setPlanCode] = useState("");
  const [planStatus, setPlanStatus] = useState<PlatformPlanStatus | "">("");

  const load = useCallback(async () => {
    setState("loading");
    setError("");
    try {
      const page = await adminApi.listOrganizations({
        search: search || undefined,
        plan_code: planCode || undefined,
        plan_status: planStatus,
        limit: PAGE_SIZE,
        offset,
      });
      setItems(page.items);
      setTotal(page.total);
      setSelectedId((current) => current && page.items.some((item) => item.organization_id === current)
        ? current
        : page.items[0]?.organization_id ?? null);
      setState("ready");
    } catch (loadError) {
      setError(adminErrorMessage(loadError));
      setState("error");
    }
  }, [offset, planCode, planStatus, search]);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    void adminApi.listPlans().then(setPlans).catch(() => setPlans([]));
  }, []);

  useEffect(() => {
    const syncSearch = () => {
      const next = initialSearch();
      setSearchDraft(next);
      setSearch(next);
      setOffset(0);
    };
    window.addEventListener("popstate", syncSearch);
    return () => window.removeEventListener("popstate", syncSearch);
  }, []);

  const selected = useMemo(() => items.find((item) => item.organization_id === selectedId) ?? null, [items, selectedId]);

  const applyFilters = (event: FormEvent) => {
    event.preventDefault();
    setOffset(0);
    setSearch(searchDraft.trim());
  };

  const clearFilters = () => {
    setSearchDraft("");
    setSearch("");
    setPlanCode("");
    setPlanStatus("");
    setOffset(0);
    window.history.replaceState(null, "", window.location.pathname);
  };

  const handleUpdated = (updated: PlatformOrganizationDetail) => {
    setItems((current) => current.map((item) => item.organization_id === updated.organization_id ? updated : item));
  };

  return (
    <section className="admin-page-frame admin-page-frame-wide" aria-labelledby="admin-organizations-title">
      <AdminPageHeader
        actions={<button className="button" onClick={() => void load()} type="button"><Icon name="refresh" size={16} />刷新列表</button>}
        description="管理每个客户工作区的套餐、试用期限、访问状态和成员身份。"
        title="工作区"
      />

      <form className="admin-filter-bar" onSubmit={applyFilters}>
        <label className="admin-search-field">
          <span className="sr-only">搜索工作区</span>
          <Icon name="search" size={16} />
          <input onChange={(event) => setSearchDraft(event.target.value)} placeholder="搜索名称或工作区 ID" value={searchDraft} />
        </label>
        <label>
          <span className="sr-only">按套餐筛选</span>
          <select className="select-field" onChange={(event) => { setPlanCode(event.target.value); setOffset(0); }} value={planCode}>
            <option value="">全部套餐</option>
            {plans.map((plan) => <option key={plan.code} value={plan.code}>{plan.name}</option>)}
          </select>
        </label>
        <label>
          <span className="sr-only">按状态筛选</span>
          <select className="select-field" onChange={(event) => { setPlanStatus(event.target.value as PlatformPlanStatus | ""); setOffset(0); }} value={planStatus}>
            <option value="">全部状态</option>
            <option value="trial">试用中</option>
            <option value="active">正常</option>
            <option value="expired">已到期</option>
            <option value="suspended">已暂停</option>
            <option value="legacy">兼容模式</option>
          </select>
        </label>
        <button className="button button-primary" type="submit">筛选工作区</button>
        {(search || planCode || planStatus) && <button className="button button-ghost" onClick={clearFilters} type="button">清除条件</button>}
      </form>

      <div className="admin-master-detail">
        <section className="admin-list-pane" aria-label="工作区列表">
          <div className="admin-list-summary">
            <span>工作区</span>
            <strong>{numberFormat(total)}</strong>
          </div>
          {state === "loading" && <AdminLoading label="正在读取工作区…" />}
          {state === "error" && <AdminError message={error} onRetry={() => void load()} />}
          {state === "ready" && items.length === 0 && (
            <AdminEmpty
              action={(search || planCode || planStatus) ? <button className="button" onClick={clearFilters} type="button">清除筛选条件</button> : undefined}
              description={(search || planCode || planStatus) ? "没有工作区符合当前条件。" : "有客户完成注册后，工作区会显示在这里。"}
              title="没有找到工作区"
            />
          )}
          {state === "ready" && items.length > 0 && (
            <div className="admin-workspace-list">
              {items.map((organization) => (
                <button
                  aria-current={selectedId === organization.organization_id ? "true" : undefined}
                  className={`admin-workspace-row${selectedId === organization.organization_id ? " is-selected" : ""}`}
                  key={organization.organization_id}
                  onClick={() => setSelectedId(organization.organization_id)}
                  type="button"
                >
                  <span className="admin-workspace-primary">
                    <strong>{organization.name}</strong>
                    <small title={organization.organization_id}>{shortId(organization.organization_id)} · {organization.plan_name || organization.plan_code || "未分配套餐"}</small>
                  </span>
                  <span className="admin-workspace-members"><strong>{organization.active_member_count}</strong><small>/ {organization.member_count} 成员</small></span>
                  <span className="admin-workspace-trial"><small>试用截止</small><strong>{formatDate(organization.trial_ends_at)}</strong></span>
                  <AdminStatus status={organization.plan_status} />
                  <Icon name="chevron-right" size={17} />
                </button>
              ))}
            </div>
          )}
          {state === "ready" && items.length > 0 && <AdminPagination limit={PAGE_SIZE} offset={offset} onChange={setOffset} total={total} />}
        </section>
        <OrganizationDetailPanel organizationId={selected?.organization_id ?? null} onUpdated={handleUpdated} plans={plans} />
      </div>
    </section>
  );
}
