import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../../api";
import { Icon } from "../../icons";
import type { RecruitingJob, RecruitingStatus } from "../../types";
import "./recruiting.css";

type ToastKind = "success" | "error";

const statusLabel: Record<RecruitingStatus, string> = {
  draft: "草稿",
  open: "招聘中",
  paused: "暂停",
  closed: "已关闭",
};

function jobMeta(job: RecruitingJob): string {
  return [job.department, job.owner_display_name].filter(Boolean).join(" · ") || "未设置部门和负责人";
}

/**
 * The workbench intentionally uses only recruiting-core aggregates that are
 * already backed by the server.  It is an operational entry point, not a
 * decorative analytics dashboard with invented conversion data.
 */
export function RecruitingOverview({
  formatError,
  notify,
  onOpenAgent,
  onCreateJob,
  onOpenJobs,
  onOpenMatching,
}: {
  formatError: (error: unknown) => string;
  notify: (kind: ToastKind, message: string) => void;
  onOpenAgent: () => void;
  onCreateJob: () => void;
  onOpenJobs: () => void;
  onOpenMatching: () => void;
}) {
  const [jobs, setJobs] = useState<RecruitingJob[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await api.listRecruitingJobs();
      setJobs(response.items);
    } catch (loadError) {
      const message = formatError(loadError);
      setError(message);
      notify("error", message);
    } finally {
      setLoading(false);
    }
  }, [formatError, notify]);

  useEffect(() => {
    void load();
  }, [load]);

  const summary = useMemo(() => {
    const open = jobs.filter((job) => job.recruiting_status === "open").length;
    const paused = jobs.filter((job) => job.recruiting_status === "paused").length;
    const applications = jobs.reduce(
      (total, job) => total + Math.max(0, job.active_application_count),
      0,
    );
    return { applications, open, paused };
  }, [jobs]);

  const activeJobs = jobs
    .filter((job) => job.recruiting_status === "open" || job.recruiting_status === "paused")
    .slice(0, 8);

  return (
    <div className="page-frame recruiting-overview">
      <header className="page-heading recruiting-page-heading">
        <div>
          <h1>招聘工作台</h1>
          <p>从岗位 JD 到候选人匹配，所有操作只基于当前工作区的真实招聘记录。</p>
        </div>
        <div className="recruiting-overview-heading-actions">
          <button className="button button-ghost" onClick={onOpenAgent} type="button">
            <Icon name="spark" size={16} />招聘 Agent
          </button>
          <button className="button button-primary" onClick={onCreateJob} type="button">
            <Icon name="plus" size={16} />新建职位 JD
          </button>
        </div>
      </header>

      <section aria-label="当前招聘概览" className="recruiting-overview-strip">
        <div>
          <span>招聘中岗位</span>
          <strong>{summary.open}</strong>
        </div>
        <div>
          <span>流程中候选人</span>
          <strong>{summary.applications}</strong>
        </div>
        <div>
          <span>暂停岗位</span>
          <strong>{summary.paused}</strong>
        </div>
        <p>数字来自已保存岗位与当前应聘记录，不包含 AI 推测或自动决策。</p>
      </section>

      {error ? (
        <section className="empty-state recruiting-empty-state" role="alert">
          <div className="empty-state-inner">
            <span className="empty-glyph"><Icon name="refresh" size={23} /></span>
            <h2>暂时无法读取招聘工作台</h2>
            <p>{error}</p>
            <button className="button button-primary" disabled={loading} onClick={() => void load()} type="button">
              <Icon name="refresh" size={16} />重新加载
            </button>
          </div>
        </section>
      ) : loading ? (
        <section aria-busy="true" aria-label="正在加载招聘工作台" className="recruiting-overview-loading">
          <div className="skeleton recruiting-overview-loading-title" />
          <div className="skeleton recruiting-overview-loading-row" />
          <div className="skeleton recruiting-overview-loading-row" />
          <div className="skeleton recruiting-overview-loading-row" />
        </section>
      ) : !jobs.length ? (
        <section className="empty-state recruiting-empty-state">
          <div className="empty-state-inner">
            <span className="empty-glyph"><Icon name="briefcase" size={23} /></span>
            <h2>从第一个职位开始</h2>
            <p>先创建或发布岗位 JD，再从人才库加入候选人。</p>
            <button className="button button-primary" onClick={onCreateJob} type="button">
              <Icon name="plus" size={16} />创建职位 JD
            </button>
          </div>
        </section>
      ) : (
        <div className="recruiting-overview-layout">
          <section className="panel recruiting-overview-jobs">
            <div className="panel-heading">
              <div>
                <h2>当前岗位</h2>
                <p>优先显示招聘中和暂停中的岗位。</p>
              </div>
            </div>
            {activeJobs.length ? (
              <div className="table-scroll">
                <table className="candidate-table recruiting-overview-table">
                  <thead>
                    <tr>
                      <th scope="col">岗位</th>
                      <th scope="col">状态</th>
                      <th scope="col">负责人 / 部门</th>
                      <th scope="col">流程中候选人</th>
                    </tr>
                  </thead>
                  <tbody>
                    {activeJobs.map((job) => (
                      <tr key={job.job_id}>
                        <td>
                          <strong>{job.title}</strong>
                          <small>JD v{job.current_job_version_number ?? "—"}</small>
                        </td>
                        <td><span className={`recruiting-status-chip is-${job.recruiting_status}`}>{statusLabel[job.recruiting_status]}</span></td>
                        <td>{jobMeta(job)}</td>
                        <td>{job.active_application_count}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <div className="recruiting-overview-empty-row">
                暂无招聘中岗位。你仍可以在职位管理中查看草稿或已关闭岗位。
              </div>
            )}
          </section>

          <aside className="panel recruiting-overview-actions">
            <div className="panel-heading">
              <div>
                <h2>下一步</h2>
                <p>按招聘实际动作进入对应模块。</p>
              </div>
            </div>
            <button className="recruiting-overview-action" onClick={onOpenJobs} type="button">
              <Icon name="briefcase" size={18} />
              <span><strong>管理职位 JD</strong><small>创建、发布和查看岗位版本</small></span>
              <Icon name="chevron-right" size={16} />
            </button>
            <button className="recruiting-overview-action" onClick={onOpenMatching} type="button">
              <Icon name="match" size={18} />
              <span><strong>查看智能匹配</strong><small>按已确认 JD 审阅候选人依据</small></span>
              <Icon name="chevron-right" size={16} />
            </button>
          </aside>
        </div>
      )}
    </div>
  );
}
