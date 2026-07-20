import { useCallback, useEffect, useMemo, useState } from "react";
import { Icon } from "../../icons";
import { adminApi, adminErrorMessage } from "../admin-api";
import type {
  AdminView,
  AiRunUsage,
  PlatformDashboard,
  PlatformOrganizationSummary,
  RequestState,
} from "../admin-types";
import {
  AdminError,
  AdminLoading,
  AdminPageHeader,
  AdminStatus,
  currencyFromMicros,
  formatDate,
  numberFormat,
  shortId,
} from "../AdminComponents";

export function AdminOverviewPage({ onNavigate }: { onNavigate: (view: AdminView) => void }) {
  const [state, setState] = useState<RequestState>("loading");
  const [error, setError] = useState("");
  const [dashboard, setDashboard] = useState<PlatformDashboard | null>(null);
  const [organizations, setOrganizations] = useState<PlatformOrganizationSummary[]>([]);
  const [runs, setRuns] = useState<AiRunUsage[]>([]);

  const load = useCallback(async () => {
    setState("loading");
    setError("");
    try {
      const [nextDashboard, organizationPage, nextRuns] = await Promise.all([
        adminApi.getDashboard(),
        adminApi.listOrganizations({ limit: 50, offset: 0 }),
        adminApi.listAiRuns({ limit: 50, offset: 0 }),
      ]);
      setDashboard(nextDashboard);
      setOrganizations(organizationPage.items);
      setRuns(nextRuns);
      setState("ready");
    } catch (loadError) {
      setError(adminErrorMessage(loadError));
      setState("error");
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const failedRuns = useMemo(
    () => runs.filter((run) => ["failed", "error", "unavailable"].includes(run.status.toLowerCase())).slice(0, 6),
    [runs],
  );
  const expiringOrganizations = useMemo(() => {
    const now = Date.now();
    const sevenDays = now + 7 * 24 * 60 * 60 * 1000;
    return organizations.filter((item) => {
      if (item.plan_status !== "trial" || !item.trial_ends_at) return false;
      const end = new Date(item.trial_ends_at).getTime();
      return end >= now && end <= sevenDays;
    }).sort((left, right) =>
      new Date(left.trial_ends_at || 0).getTime() - new Date(right.trial_ends_at || 0).getTime(),
    ).slice(0, 5);
  }, [organizations]);

  return (
    <section className="admin-page-frame" aria-labelledby="admin-overview-title">
      <AdminPageHeader
        actions={<button className="button" onClick={() => void load()} type="button"><Icon name="refresh" size={16} />刷新数据</button>}
        description="先处理异常和临期事项，再查看平台规模与 AI 成本。"
        title="平台概览"
      />
      {state === "loading" && <div className="admin-panel"><AdminLoading label="正在汇总平台状态…" /></div>}
      {state === "error" && <div className="admin-panel"><AdminError message={error} onRetry={() => void load()} /></div>}
      {state === "ready" && dashboard && (
        <>
          <div className="admin-metric-strip" aria-label="平台关键指标">
            <article>
              <span>工作区</span>
              <strong>{numberFormat(dashboard.organizations_total)}</strong>
              <small>正常 {numberFormat(dashboard.organizations_by_status.active ?? 0)}，试用 {numberFormat(dashboard.organizations_by_status.trial ?? 0)}</small>
            </article>
            <article>
              <span>活跃用户</span>
              <strong>{numberFormat(dashboard.users_active)}</strong>
              <small>共 {numberFormat(dashboard.users_total)} 个账号，已验证 {numberFormat(dashboard.users_verified)}</small>
            </article>
            <article>
              <span>AI 运行</span>
              <strong>{numberFormat(dashboard.ai_runs_total)}</strong>
              <small>成功 {numberFormat(dashboard.ai_runs_succeeded)}，失败 {numberFormat(dashboard.ai_runs_failed)}</small>
            </article>
            <article>
              <span>累计 AI 成本</span>
              <strong>{currencyFromMicros(dashboard.ai_cost_cny_micros)}</strong>
              <small>{dashboard.ai_cost_unavailable_runs ? `${dashboard.ai_cost_unavailable_runs} 次运行待核算` : "费用记录完整"}</small>
            </article>
          </div>

          <div className="admin-overview-grid">
            <section className="admin-panel admin-attention-panel" aria-labelledby="attention-title">
              <div className="admin-section-heading">
                <div><h2 id="attention-title">待处理事项</h2><p>只展示需要平台管理员介入的状态。</p></div>
                <span>{dashboard.trials_expiring_within_7_days + dashboard.ai_runs_failed} 项信号</span>
              </div>
              <div className="admin-attention-list">
                <button onClick={() => onNavigate("organizations")} type="button">
                  <span className="admin-attention-icon is-warning"><Icon name="briefcase" size={18} /></span>
                  <span><strong>7 天内到期的试用</strong><small>检查是否续期、升级或到期保留数据。</small></span>
                  <b>{numberFormat(dashboard.trials_expiring_within_7_days)}</b>
                  <Icon name="chevron-right" size={17} />
                </button>
                <button onClick={() => onNavigate("ai")} type="button">
                  <span className="admin-attention-icon is-error"><Icon name="activity" size={18} /></span>
                  <span><strong>失败的 AI 运行</strong><small>按工作区和功能定位失败来源。</small></span>
                  <b>{numberFormat(dashboard.ai_runs_failed)}</b>
                  <Icon name="chevron-right" size={17} />
                </button>
                <button onClick={() => onNavigate("organizations")} type="button">
                  <span className="admin-attention-icon"><Icon name="inbox" size={18} /></span>
                  <span><strong>邮箱接入规模</strong><small>当前平台已连接的简历收件邮箱。</small></span>
                  <b>{numberFormat(dashboard.mailboxes_total)}</b>
                  <Icon name="chevron-right" size={17} />
                </button>
                {dashboard.ai_cost_unavailable_runs > 0 && (
                  <button onClick={() => onNavigate("ai")} type="button">
                    <span className="admin-attention-icon is-warning"><Icon name="history" size={18} /></span>
                    <span><strong>费用待核算</strong><small>模型价格缺失或运行只记录了部分成本。</small></span>
                    <b>{numberFormat(dashboard.ai_cost_unavailable_runs)}</b>
                    <Icon name="chevron-right" size={17} />
                  </button>
                )}
              </div>
            </section>

            <section className="admin-panel admin-inventory-panel" aria-labelledby="inventory-title">
              <div className="admin-section-heading"><div><h2 id="inventory-title">业务资产</h2><p>仅展示数量，不暴露候选人内容。</p></div></div>
              <dl>
                <div><dt>简历</dt><dd>{numberFormat(dashboard.resumes_total)}</dd></div>
                <div><dt>岗位 JD</dt><dd>{numberFormat(dashboard.jobs_total)}</dd></div>
                <div><dt>接入邮箱</dt><dd>{numberFormat(dashboard.mailboxes_total)}</dd></div>
                <div><dt>暂停工作区</dt><dd>{numberFormat(dashboard.organizations_by_status.suspended ?? 0)}</dd></div>
              </dl>
              <p className="admin-generated-at">汇总时间：{formatDate(dashboard.generated_at, true)}</p>
            </section>
          </div>

          <div className="admin-overview-grid admin-overview-grid-secondary">
            <section className="admin-panel" aria-labelledby="expiring-title">
              <div className="admin-section-heading">
                <div><h2 id="expiring-title">临期工作区</h2><p>当前页可确认的 7 天内到期试用。</p></div>
                <button className="text-button" onClick={() => onNavigate("organizations")} type="button">查看全部</button>
              </div>
              {expiringOrganizations.length ? (
                <ul className="admin-compact-list">
                  {expiringOrganizations.map((organization) => (
                    <li key={organization.organization_id}>
                      <span><strong>{organization.name}</strong><small>{organization.plan_name || organization.plan_code || "未分配套餐"}</small></span>
                      <span><AdminStatus status="expiring" label={formatDate(organization.trial_ends_at)} /></span>
                    </li>
                  ))}
                </ul>
              ) : <p className="admin-inline-empty">当前列表中没有 7 天内到期的工作区。</p>}
            </section>
            <section className="admin-panel" aria-labelledby="failed-run-title">
              <div className="admin-section-heading">
                <div><h2 id="failed-run-title">最近失败运行</h2><p>运行记录不包含简历、Prompt 或模型输出。</p></div>
                <button className="text-button" onClick={() => onNavigate("ai")} type="button">查看运行记录</button>
              </div>
              {failedRuns.length ? (
                <ul className="admin-compact-list">
                  {failedRuns.map((run) => (
                    <li key={run.run_id}>
                      <span><strong>{run.feature}</strong><small>{shortId(run.organization_id)} · {formatDate(run.started_at, true)}</small></span>
                      <AdminStatus status={run.status} />
                    </li>
                  ))}
                </ul>
              ) : <p className="admin-inline-empty">最近拉取的运行中没有失败记录。</p>}
            </section>
          </div>
        </>
      )}
    </section>
  );
}
