import {
  useCallback,
  useEffect,
  useMemo,
  useState,
} from "react";
import { api } from "../../api";
import { Icon } from "../../icons";
import type {
  JobApplication,
  JobApplicationDetail,
  JobApplicationStageTransition,
  RecruitingJob,
  RecruitingMember,
  RecruitingStatus,
  RecruitingWorkflow,
  RecruitingWorkflowStage,
  RecruitingWorkflowVersion,
} from "../../types";
import { BackofficeSelect } from "../../backoffice/ui/BackofficeSelect";
import "./recruiting.css";

type ToastKind = "success" | "error";
type WorkflowStageType = "active" | "hired" | "rejected";
type ApplicationAction = "advance" | "return" | "reject" | "hire";

interface WorkflowStageDraft {
  stage_key: string;
  name: string;
  stage_type: WorkflowStageType;
}

interface WorkflowVersionReference {
  workflow: RecruitingWorkflow;
  version: RecruitingWorkflowVersion;
}

interface WorkflowBoardGroup {
  workflowVersionId: string;
  stages: RecruitingWorkflowStage[];
}

const recruitingStatusOptions: Array<{ value: RecruitingStatus; label: string }> = [
  { value: "draft", label: "草稿" },
  { value: "open", label: "招聘中" },
  { value: "paused", label: "暂停" },
  { value: "closed", label: "已关闭" },
];

const recruitingStatusLabels: Record<RecruitingStatus, string> = Object.fromEntries(
  recruitingStatusOptions.map((option) => [option.value, option.label]),
) as Record<RecruitingStatus, string>;

const applicationStatusLabels: Record<JobApplication["status"], string> = {
  active: "进行中",
  hired: "已录用",
  rejected: "已淘汰",
  withdrawn: "已撤回",
};

const stageTypeLabels: Record<WorkflowStageType, string> = {
  active: "进行中",
  hired: "录用结果",
  rejected: "淘汰结果",
};

function defaultWorkflowStages(): WorkflowStageDraft[] {
  return [
    { stage_key: "pending_screen", name: "待筛选", stage_type: "active" },
    { stage_key: "initial_screen", name: "初筛", stage_type: "active" },
    { stage_key: "interview", name: "面试", stage_type: "active" },
    { stage_key: "final_interview", name: "复试", stage_type: "active" },
    { stage_key: "offer", name: "Offer", stage_type: "active" },
    { stage_key: "hired", name: "已录用", stage_type: "hired" },
    { stage_key: "rejected", name: "已淘汰", stage_type: "rejected" },
  ];
}

function statusClass(status: RecruitingStatus): string {
  if (status === "open") return "is-open";
  if (status === "paused") return "is-paused";
  if (status === "closed") return "is-closed";
  return "is-draft";
}

function terminalClass(stageType: WorkflowStageType): string {
  if (stageType === "hired") return "is-hired";
  if (stageType === "rejected") return "is-rejected";
  return "is-active";
}

function formatDateTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function workflowVersionLabel(reference: WorkflowVersionReference): string {
  return `${reference.workflow.name} · v${reference.version.version}`;
}

function applicationTransitionLabel(transition: JobApplicationStageTransition): string {
  const source = transition.from_stage_name ?? "加入岗位";
  return `${source} → ${transition.to_stage_name}`;
}

export interface RecruitingWorkspaceProps {
  formatError: (error: unknown) => string;
  initialJobId?: string;
  notify: (kind: ToastKind, message: string) => void;
  onCreateJob: () => void;
  onInvalidJobSelection?: () => void;
  onOpenCandidate?: (application: JobApplication) => void;
  onJobSelectionChange?: (jobId: string | null) => void;
}

/**
 * A recruiter-owned workbench for the existing Job aggregate. The component
 * only invokes explicit human transition endpoints. It deliberately has no
 * AI action or automatic decision path.
 */
export function RecruitingWorkspace({
  formatError,
  initialJobId,
  notify,
  onCreateJob,
  onInvalidJobSelection,
  onOpenCandidate,
  onJobSelectionChange,
}: RecruitingWorkspaceProps) {
  const [jobs, setJobs] = useState<RecruitingJob[]>([]);
  const [workflows, setWorkflows] = useState<RecruitingWorkflow[]>([]);
  const [members, setMembers] = useState<RecruitingMember[]>([]);
  const [selectedJobId, setSelectedJobId] = useState(initialJobId ?? "");
  const [selectedJob, setSelectedJob] = useState<RecruitingJob | null>(null);
  const [applications, setApplications] = useState<JobApplication[]>([]);
  const [workspaceLoading, setWorkspaceLoading] = useState(true);
  const [selectedJobLoading, setSelectedJobLoading] = useState(false);
  const [workspaceError, setWorkspaceError] = useState<string | null>(null);
  const [settingsSaving, setSettingsSaving] = useState(false);
  const [workflowSaving, setWorkflowSaving] = useState(false);
  const [movingApplicationId, setMovingApplicationId] = useState<string | null>(null);
  const [transitionNotes, setTransitionNotes] = useState<Record<string, string>>({});
  const [applicationDetails, setApplicationDetails] = useState<
    Record<string, JobApplicationDetail | undefined>
  >({});
  const [historyLoadingId, setHistoryLoadingId] = useState<string | null>(null);
  const [historyOpenId, setHistoryOpenId] = useState<string | null>(null);
  const [workflowEditorOpen, setWorkflowEditorOpen] = useState(false);
  const [workflowEditorId, setWorkflowEditorId] = useState("");
  const [workflowName, setWorkflowName] = useState("");
  const [workflowStageList, setWorkflowStageList] = useState<WorkflowStageDraft[]>(
    defaultWorkflowStages,
  );
  const [jobDraft, setJobDraft] = useState({
    recruiting_status: "draft" as RecruitingStatus,
    department: "",
    owner_user_id: "",
    hc_total: "1",
    recruiting_workflow_version_id: "",
  });

  const selectJob = useCallback((jobId: string) => {
    setSelectedJobId(jobId);
    onJobSelectionChange?.(jobId || null);
  }, [onJobSelectionChange]);

  const workflowReferences = useMemo<WorkflowVersionReference[]>(
    () => workflows.flatMap((workflow) =>
      workflow.versions.map((version) => ({ workflow, version })),
    ),
    [workflows],
  );
  const publishedWorkflowReferences = useMemo(
    () => workflowReferences.filter((reference) => reference.version.status === "published"),
    [workflowReferences],
  );
  const selectedWorkflowReference = useMemo(
    () => workflowReferences.find(
      (reference) => reference.version.workflow_version_id === selectedJob?.recruiting_workflow_version_id,
    ) ?? null,
    [selectedJob?.recruiting_workflow_version_id, workflowReferences],
  );

  const loadWorkspace = useCallback(async () => {
    setWorkspaceLoading(true);
    setWorkspaceError(null);
    try {
      const [nextJobs, nextWorkflows, nextMembers] = await Promise.all([
        api.listRecruitingJobs(),
        api.listRecruitingWorkflows(),
        api.listRecruitingMembers(),
      ]);
      setJobs(nextJobs.items);
      setWorkflows(nextWorkflows);
      setMembers(nextMembers);
      if (initialJobId && !nextJobs.items.some((item) => item.job_id === initialJobId)) {
        notify("error", "该招聘岗位不存在或无权访问，已回到可访问的岗位。");
        onInvalidJobSelection?.();
      }
      setSelectedJobId((current) =>
        nextJobs.items.some((item) => item.job_id === current)
          ? current
          : (nextJobs.items[0]?.job_id ?? ""),
      );
    } catch (error) {
      const message = formatError(error);
      setWorkspaceError(message);
    } finally {
      setWorkspaceLoading(false);
    }
  }, [formatError, initialJobId, notify, onInvalidJobSelection]);

  const loadSelectedJob = useCallback(async (jobId: string) => {
    if (!jobId) {
      setSelectedJob(null);
      setApplications([]);
      return;
    }
    setSelectedJobLoading(true);
    try {
      const [nextJob, nextApplications] = await Promise.all([
        api.getRecruitingJob(jobId),
        api.listJobApplications(jobId),
      ]);
      setSelectedJob(nextJob);
      setApplications(nextApplications.items);
    } catch (error) {
      notify("error", formatError(error));
    } finally {
      setSelectedJobLoading(false);
    }
  }, [formatError, notify]);

  useEffect(() => {
    void loadWorkspace();
  }, [loadWorkspace]);

  useEffect(() => {
    void loadSelectedJob(selectedJobId);
  }, [loadSelectedJob, selectedJobId]);

  useEffect(() => {
    if (initialJobId) {
      if (initialJobId === selectedJobId) return;
      if (!jobs.some((job) => job.job_id === initialJobId)) return;
      setSelectedJobId(initialJobId);
      return;
    }
    const defaultJobId = jobs[0]?.job_id ?? "";
    if (defaultJobId && defaultJobId !== selectedJobId) {
      setSelectedJobId(defaultJobId);
    }
  }, [initialJobId, jobs, selectedJobId]);

  useEffect(() => {
    if (!selectedJob) return;
    setJobDraft({
      recruiting_status: selectedJob.recruiting_status,
      department: selectedJob.department ?? "",
      owner_user_id: selectedJob.owner_user_id ?? "",
      hc_total: String(selectedJob.hc_total),
      recruiting_workflow_version_id: selectedJob.recruiting_workflow_version_id ?? "",
    });
  }, [selectedJob]);

  useEffect(() => {
    setApplicationDetails({});
    setHistoryOpenId(null);
  }, [selectedJobId]);

  const workflowReferenceByVersionId = useMemo(
    () => new Map(
      workflowReferences.map((reference) => [
        reference.version.workflow_version_id,
        reference,
      ]),
    ),
    [workflowReferences],
  );
  const workflowBoardGroups = useMemo<WorkflowBoardGroup[]>(() => {
    const applicationsByWorkflowVersion = new Map<string, JobApplication[]>();
    for (const application of applications) {
      const versionApplications = applicationsByWorkflowVersion.get(application.workflow_version_id) ?? [];
      versionApplications.push(application);
      applicationsByWorkflowVersion.set(application.workflow_version_id, versionApplications);
    }

    const groups: WorkflowBoardGroup[] = [];
    const addGroup = (
      workflowVersionId: string,
      fallbackApplications: JobApplication[] = [],
    ) => {
      if (groups.some((group) => group.workflowVersionId === workflowVersionId)) return;
      const reference = workflowReferenceByVersionId.get(workflowVersionId);
      const knownStages = reference
        ? [...reference.version.stages].sort((left, right) => left.sort_order - right.sort_order)
        : [];
      const seenStageIds = new Set(knownStages.map((stage) => stage.stage_id));
      const fallbackStages = fallbackApplications.flatMap((application) => {
        if (seenStageIds.has(application.current_stage_id)) return [];
        seenStageIds.add(application.current_stage_id);
        return [{
          stage_id: application.current_stage_id,
          workflow_version_id: application.workflow_version_id,
          stage_key: application.current_stage_key,
          name: application.current_stage_name,
          stage_type: application.current_stage_type,
          sort_order: application.current_stage_sort_order,
        } satisfies RecruitingWorkflowStage];
      });
      groups.push({
        workflowVersionId,
        stages: [...knownStages, ...fallbackStages].sort(
          (left, right) => left.sort_order - right.sort_order,
        ),
      });
    };

    if (selectedWorkflowReference) {
      addGroup(selectedWorkflowReference.version.workflow_version_id);
    }
    for (const [workflowVersionId, versionApplications] of applicationsByWorkflowVersion) {
      addGroup(workflowVersionId, versionApplications);
    }
    return groups;
  }, [applications, selectedWorkflowReference, workflowReferenceByVersionId]);
  const activeStagesByWorkflowVersion = useMemo(
    () => new Map(
      workflowBoardGroups.map((group) => [
        group.workflowVersionId,
        group.stages.filter((stage) => stage.stage_type === "active"),
      ]),
    ),
    [workflowBoardGroups],
  );
  const boardStages = useMemo(
    () => workflowBoardGroups.flatMap((group) => group.stages),
    [workflowBoardGroups],
  );

  const applicationByStageId = useMemo(() => {
    const grouped = new Map<string, JobApplication[]>();
    for (const application of applications) {
      const stageApplications = grouped.get(application.current_stage_id) ?? [];
      stageApplications.push(application);
      grouped.set(application.current_stage_id, stageApplications);
    }
    return grouped;
  }, [applications]);

  const resetWorkflowEditor = useCallback((reference?: WorkflowVersionReference | null) => {
    setWorkflowEditorId(reference?.workflow.workflow_id ?? "");
    setWorkflowName(reference?.workflow.name ?? "");
    setWorkflowStageList(reference
      ? [...reference.version.stages]
        .sort((left, right) => left.sort_order - right.sort_order)
        .map((stage) => ({
          stage_key: stage.stage_key,
          name: stage.name,
          stage_type: stage.stage_type,
        }))
      : defaultWorkflowStages());
  }, []);

  const openWorkflowEditor = () => {
    resetWorkflowEditor(selectedWorkflowReference);
    setWorkflowEditorOpen(true);
  };

  const openNewWorkflowEditor = () => {
    resetWorkflowEditor(null);
    setWorkflowEditorOpen(true);
  };

  const refreshCurrentJob = async () => {
    await Promise.all([
      loadWorkspace(),
      selectedJobId ? loadSelectedJob(selectedJobId) : Promise.resolve(),
    ]);
  };

  const saveJobSettings = async () => {
    if (!selectedJob) return;
    const normalizedHc = Number.parseInt(jobDraft.hc_total, 10);
    if (!Number.isInteger(normalizedHc) || normalizedHc < 1) {
      notify("error", "HC 必须是大于 0 的整数。");
      return;
    }
    setSettingsSaving(true);
    try {
      await api.updateRecruitingJob(selectedJob.job_id, {
        recruiting_status: jobDraft.recruiting_status,
        department: jobDraft.department.trim() || null,
        owner_user_id: jobDraft.owner_user_id || null,
        hc_total: normalizedHc,
        recruiting_workflow_version_id: jobDraft.recruiting_workflow_version_id || null,
      });
      await refreshCurrentJob();
      notify("success", "岗位设置已保存。");
    } catch (error) {
      notify("error", formatError(error));
    } finally {
      setSettingsSaving(false);
    }
  };

  const createWorkflowStage = () => {
    setWorkflowStageList((current) => {
      const activeCount = current.filter((stage) => stage.stage_type === "active").length;
      const terminalIndex = current.findIndex((stage) => stage.stage_type !== "active");
      const nextStage: WorkflowStageDraft = {
        stage_key: `stage_${Date.now().toString(36)}`,
        name: `阶段 ${activeCount + 1}`,
        stage_type: "active",
      };
      if (terminalIndex < 0) return [...current, nextStage];
      return [
        ...current.slice(0, terminalIndex),
        nextStage,
        ...current.slice(terminalIndex),
      ];
    });
  };

  const moveWorkflowStage = (index: number, offset: -1 | 1) => {
    setWorkflowStageList((current) => {
      const nextIndex = index + offset;
      if (
        nextIndex < 0 ||
        nextIndex >= current.length ||
        current[index]?.stage_type !== "active" ||
        current[nextIndex]?.stage_type !== "active"
      ) return current;
      const next = [...current];
      [next[index], next[nextIndex]] = [next[nextIndex], next[index]];
      return next;
    });
  };

  const removeWorkflowStage = (index: number) => {
    setWorkflowStageList((current) => {
      const target = current[index];
      if (!target || target.stage_type !== "active") return current;
      if (current.filter((stage) => stage.stage_type === "active").length <= 1) {
        notify("error", "流程至少保留一个进行中阶段。");
        return current;
      }
      return current.filter((_, stageIndex) => stageIndex !== index);
    });
  };

  const updateWorkflowStageName = (index: number, name: string) => {
    setWorkflowStageList((current) => current.map((stage, stageIndex) => (
      stageIndex === index ? { ...stage, name } : stage
    )));
  };

  const publishWorkflow = async () => {
    if (!selectedJob) return;
    const normalizedStages = workflowStageList.map((stage, index) => ({
      ...stage,
      name: stage.name.trim(),
      sort_order: (index + 1) * 10,
    }));
    if (normalizedStages.some((stage) => !stage.name)) {
      notify("error", "请填写每个阶段的名称。");
      return;
    }
    if (!workflowEditorId && !workflowName.trim()) {
      notify("error", "请填写流程名称。");
      return;
    }
    setWorkflowSaving(true);
    try {
      let publishedVersion: RecruitingWorkflowVersion | null = null;
      if (workflowEditorId) {
        const draftVersion = await api.createRecruitingWorkflowVersion(workflowEditorId, {
          stages: normalizedStages,
        });
        publishedVersion = await api.publishRecruitingWorkflowVersion(
          draftVersion.workflow_version_id,
        );
      } else {
        const workflow = await api.createRecruitingWorkflow({
          name: workflowName.trim(),
          stages: normalizedStages,
        });
        publishedVersion = workflow.versions.find((version) => version.status === "published") ?? null;
      }
      if (!publishedVersion) throw new Error("流程版本未发布");
      await api.updateRecruitingJob(selectedJob.job_id, {
        recruiting_workflow_version_id: publishedVersion.workflow_version_id,
      });
      setWorkflowEditorOpen(false);
      await refreshCurrentJob();
      notify("success", "流程已发布并应用到当前岗位。");
    } catch (error) {
      notify("error", formatError(error));
    } finally {
      setWorkflowSaving(false);
    }
  };

  const showApplicationHistory = async (applicationId: string) => {
    if (historyOpenId === applicationId) {
      setHistoryOpenId(null);
      return;
    }
    setHistoryOpenId(applicationId);
    if (applicationDetails[applicationId]) return;
    setHistoryLoadingId(applicationId);
    try {
      const detail = await api.getJobApplication(applicationId);
      setApplicationDetails((current) => ({ ...current, [applicationId]: detail }));
    } catch (error) {
      setHistoryOpenId(null);
      notify("error", formatError(error));
    } finally {
      setHistoryLoadingId(null);
    }
  };

  const transitionApplication = async (
    application: JobApplication,
    action: ApplicationAction,
  ) => {
    if (!selectedJob || application.status !== "active") return;
    const actionLabel = {
      advance: "推进",
      return: "退回",
      reject: "淘汰",
      hire: "录用",
    }[action];
    if (
      (action === "reject" || action === "hire") &&
      !window.confirm(`确认${actionLabel}「${application.candidate_display_name ?? "该候选人"}」吗？`)
    ) return;

    const payload = {
      expected_state_version: application.state_version,
      note: transitionNotes[application.application_id]?.trim() || null,
    };
    setMovingApplicationId(application.application_id);
    try {
      if (action === "advance") {
        await api.advanceJobApplication(application.application_id, payload);
      } else if (action === "return") {
        await api.returnJobApplication(application.application_id, payload);
      } else if (action === "reject") {
        await api.rejectJobApplication(application.application_id, payload);
      } else {
        await api.hireJobApplication(application.application_id, payload);
      }
      setTransitionNotes((current) => ({ ...current, [application.application_id]: "" }));
      setApplicationDetails((current) => ({ ...current, [application.application_id]: undefined }));
      await refreshCurrentJob();
      notify("success", `已${actionLabel}候选人。`);
    } catch (error) {
      notify("error", formatError(error));
      await loadSelectedJob(selectedJob.job_id);
    } finally {
      setMovingApplicationId(null);
    }
  };

  if (workspaceLoading) {
    return (
      <div className="page-frame recruiting-workspace">
        <div className="recruiting-loading" role="status">
          <i className="spinner" />正在加载招聘流程
        </div>
      </div>
    );
  }

  if (workspaceError) {
    return (
      <div className="page-frame recruiting-workspace">
        <section className="empty-state recruiting-empty-state" role="alert">
          <div className="empty-state-inner">
            <span className="empty-glyph"><Icon name="briefcase" size={23} /></span>
            <h1>无法加载招聘流程</h1>
            <p>{workspaceError}</p>
            <button className="button button-primary" onClick={() => void loadWorkspace()} type="button">
              <Icon name="refresh" size={16} />重新加载
            </button>
          </div>
        </section>
      </div>
    );
  }

  if (!jobs.length) {
    return (
      <div className="page-frame recruiting-workspace">
        <header className="page-heading recruiting-page-heading">
          <div><h1>招聘流程</h1></div>
        </header>
        <section className="empty-state recruiting-empty-state">
          <div className="empty-state-inner">
            <span className="empty-glyph"><Icon name="briefcase" size={23} /></span>
            <h2>还没有岗位</h2>
            <p>创建并发布 JD 后，即可在这里配置流程和管理候选人。</p>
            <button className="button button-primary" onClick={onCreateJob} type="button">
              <Icon name="plus" size={16} />新建岗位 JD
            </button>
          </div>
        </section>
      </div>
    );
  }

  return (
    <div className="page-frame recruiting-workspace">
      <header className="page-heading recruiting-page-heading">
        <div>
          <h1>招聘流程</h1>
          <p>查看岗位看板、人工流转和可追溯的阶段历史。</p>
        </div>
        <button className="button button-primary" onClick={onCreateJob} type="button">
          <Icon name="plus" size={16} />新建岗位 JD
        </button>
      </header>

      <div className="recruiting-layout">
        <aside aria-label="岗位列表" className="recruiting-job-rail">
          <div className="recruiting-job-rail-heading">
            <strong>岗位</strong>
            <span>{jobs.length}</span>
          </div>
          <div className="recruiting-job-list">
            {jobs.map((job) => {
              const isSelected = job.job_id === selectedJobId;
              return (
                <button
                  aria-current={isSelected ? "page" : undefined}
                  className={`recruiting-job-item${isSelected ? " is-selected" : ""}`}
                  key={job.job_id}
                  onClick={() => selectJob(job.job_id)}
                  type="button"
                >
                  <span className="recruiting-job-item-title">{job.title}</span>
                  <span className="recruiting-job-item-meta">
                    <span className={`recruiting-status-chip ${statusClass(job.recruiting_status)}`}>
                      {recruitingStatusLabels[job.recruiting_status]}
                    </span>
                    <span>{job.active_application_count} 人</span>
                  </span>
                  <span className="recruiting-job-item-detail">
                    {[job.department, job.owner_display_name].filter(Boolean).join(" · ") || "未设置"}
                  </span>
                </button>
              );
            })}
          </div>
        </aside>

        <main className="recruiting-main" aria-busy={selectedJobLoading}>
          {selectedJob ? (
            <>
              <section className="recruiting-job-summary">
                <div>
                  <div className="recruiting-job-title-row">
                    <h2>{selectedJob.title}</h2>
                    <span className={`recruiting-status-chip ${statusClass(selectedJob.recruiting_status)}`}>
                      {recruitingStatusLabels[selectedJob.recruiting_status]}
                    </span>
                  </div>
                  <p>
                    HC {selectedJob.hc_total}
                    <span aria-hidden="true"> · </span>
                    在招 {selectedJob.active_application_count} 人
                    {selectedJob.department ? <><span aria-hidden="true"> · </span>{selectedJob.department}</> : null}
                  </p>
                </div>
                {selectedJobLoading && <i className="spinner" aria-label="正在刷新岗位" />}
              </section>

              <section className="recruiting-stage-summary" aria-label="当前招聘流程">
                {boardStages.length ? boardStages.map((stage, index) => (
                  <div className={`recruiting-stage-step ${terminalClass(stage.stage_type)}`} key={stage.stage_id}>
                    <span>{index + 1}</span>
                    <strong>{stage.name}</strong>
                  </div>
                )) : (
                  <span className="candidate-meta">尚未配置流程。</span>
                )}
              </section>

              <section aria-label="候选人应聘阶段" className="recruiting-stage-board">
                {boardStages.map((stage) => {
                  const stageApplications = applicationByStageId.get(stage.stage_id) ?? [];
                  return (
                    <article className={`recruiting-stage-lane ${terminalClass(stage.stage_type)}`} key={stage.stage_id}>
                      <header>
                        <div>
                          <h3>{stage.name}</h3>
                          <span>{stageApplications.length}</span>
                        </div>
                        <small>{stageTypeLabels[stage.stage_type]}</small>
                      </header>
                      <div className="recruiting-application-list">
                        {stageApplications.length ? stageApplications.map((application) => (
                          <ApplicationCard
                            activeStages={activeStagesByWorkflowVersion.get(application.workflow_version_id) ?? []}
                            application={application}
                            detail={applicationDetails[application.application_id]}
                            historyLoading={historyLoadingId === application.application_id}
                            historyOpen={historyOpenId === application.application_id}
                            key={application.application_id}
                            moving={movingApplicationId === application.application_id}
                            note={transitionNotes[application.application_id] ?? ""}
                            onAction={transitionApplication}
                            onHistory={() => void showApplicationHistory(application.application_id)}
                            onNoteChange={(note) => setTransitionNotes((current) => ({
                              ...current,
                              [application.application_id]: note,
                            }))}
                            onOpenCandidate={onOpenCandidate}
                          />
                        )) : (
                          <p className="recruiting-stage-empty">暂无候选人</p>
                        )}
                      </div>
                    </article>
                  );
                })}
              </section>
            </>
          ) : null}
        </main>

        <aside className="recruiting-settings-column">
          {selectedJob && (
            <section className="recruiting-settings">
              <div className="recruiting-section-heading">
                <h2>岗位设置</h2>
              </div>
              <div className="recruiting-settings-fields">
                <label className="field-stack">
                  <span className="field-label">招聘状态</span>
                  <BackofficeSelect
                    ariaLabel="招聘状态"
                    onChange={(value) => setJobDraft((current) => ({
                      ...current,
                      recruiting_status: value as RecruitingStatus,
                    }))}
                    options={recruitingStatusOptions}
                    value={jobDraft.recruiting_status}
                  />
                </label>
                <label className="field-stack">
                  <span className="field-label">部门</span>
                  <input
                    className="field"
                    onChange={(event) => setJobDraft((current) => ({
                      ...current,
                      department: event.target.value,
                    }))}
                    placeholder="例如：研发中心"
                    value={jobDraft.department}
                  />
                </label>
                <label className="field-stack">
                  <span className="field-label">负责人</span>
                  <BackofficeSelect
                    ariaLabel="岗位负责人"
                    onChange={(value) => setJobDraft((current) => ({
                      ...current,
                      owner_user_id: value,
                    }))}
                    options={[
                      { label: "暂不指定", value: "" },
                      ...members.map((member) => ({
                        label: `${member.display_name} · ${member.role === "admin" ? "管理员" : "招聘官"}`,
                        value: member.user_id,
                      })),
                    ]}
                    value={jobDraft.owner_user_id}
                  />
                </label>
                <label className="field-stack">
                  <span className="field-label">HC</span>
                  <input
                    className="field recruiting-hc-field"
                    min="1"
                    onChange={(event) => setJobDraft((current) => ({
                      ...current,
                      hc_total: event.target.value,
                    }))}
                    type="number"
                    value={jobDraft.hc_total}
                  />
                </label>
                <label className="field-stack">
                  <span className="field-label">流程版本</span>
                  <BackofficeSelect
                    ariaLabel="招聘流程版本"
                    onChange={(value) => setJobDraft((current) => ({
                      ...current,
                      recruiting_workflow_version_id: value,
                    }))}
                    options={[
                      { label: "暂不设置", value: "" },
                      ...publishedWorkflowReferences.map((reference) => ({
                        label: workflowVersionLabel(reference),
                        value: reference.version.workflow_version_id,
                      })),
                    ]}
                    value={jobDraft.recruiting_workflow_version_id}
                  />
                </label>
              </div>
              <button
                className="button button-primary recruiting-settings-save"
                disabled={settingsSaving}
                onClick={() => void saveJobSettings()}
                type="button"
              >
                {settingsSaving ? <><i className="spinner" />正在保存</> : <><Icon name="check" size={16} />保存设置</>}
              </button>
            </section>
          )}

          <section className="recruiting-workflow-panel">
            <div className="recruiting-section-heading">
              <div>
                <h2>招聘流程</h2>
                {selectedWorkflowReference && <p>{workflowVersionLabel(selectedWorkflowReference)}</p>}
              </div>
              {selectedWorkflowReference && (
                <button className="text-button" onClick={openWorkflowEditor} type="button">
                  新版本
                </button>
              )}
            </div>
            <button
              aria-expanded={workflowEditorOpen}
              className="button button-primary recruiting-workflow-create-action"
              onClick={openNewWorkflowEditor}
              type="button"
            >
              <Icon name="plus" size={16} />
              新增招聘流程
            </button>
            {selectedWorkflowReference ? (
              <ol className="recruiting-workflow-list">
                {[...selectedWorkflowReference.version.stages]
                  .sort((left, right) => left.sort_order - right.sort_order)
                  .map((stage) => (
                    <li className={terminalClass(stage.stage_type)} key={stage.stage_id}>
                      <span>{stage.name}</span>
                      <small>{stageTypeLabels[stage.stage_type]}</small>
                    </li>
                  ))}
              </ol>
            ) : (
              <p className="recruiting-workflow-empty">当前岗位尚未绑定流程。</p>
            )}

            {workflowEditorOpen && (
              <form
                className="recruiting-workflow-editor"
                onSubmit={(event) => {
                  event.preventDefault();
                  void publishWorkflow();
                }}
              >
                <label className="field-stack">
                  <span className="field-label">{workflowEditorId ? "复用流程" : "流程名称"}</span>
                  {workflowEditorId ? (
                    <BackofficeSelect
                      ariaLabel="选择已有招聘流程"
                      onChange={(value) => {
                        const reference = workflowReferences.find(
                          (item) => item.workflow.workflow_id === value && item.version.status === "published",
                        ) ?? null;
                        resetWorkflowEditor(reference);
                      }}
                      options={[
                        { label: "新建流程", value: "" },
                        ...workflows.map((workflow) => ({ label: workflow.name, value: workflow.workflow_id })),
                      ]}
                      value={workflowEditorId}
                    />
                  ) : (
                    <input
                      className="field"
                      onChange={(event) => setWorkflowName(event.target.value)}
                      placeholder="例如：技术岗位流程"
                      value={workflowName}
                    />
                  )}
                </label>
                <div className="recruiting-workflow-stage-editor" aria-label="流程阶段">
                  {workflowStageList.map((stage, index) => {
                    const canMoveUp = index > 0 && workflowStageList[index - 1]?.stage_type === "active";
                    const canMoveDown = index < workflowStageList.length - 1 && workflowStageList[index + 1]?.stage_type === "active";
                    const editable = stage.stage_type === "active";
                    return (
                      <div className={`recruiting-stage-edit-row ${terminalClass(stage.stage_type)}`} key={stage.stage_key}>
                        <span className="recruiting-stage-edit-order">{index + 1}</span>
                        <input
                          aria-label={`第 ${index + 1} 个阶段`}
                          className="field"
                          onChange={(event) => updateWorkflowStageName(index, event.target.value)}
                          value={stage.name}
                        />
                        <span>{stageTypeLabels[stage.stage_type]}</span>
                        {editable ? (
                          <div className="recruiting-stage-edit-actions">
                            <button aria-label="上移阶段" className="text-button" disabled={!canMoveUp} onClick={() => moveWorkflowStage(index, -1)} type="button">上移</button>
                            <button aria-label="下移阶段" className="text-button" disabled={!canMoveDown} onClick={() => moveWorkflowStage(index, 1)} type="button">下移</button>
                            <button aria-label="删除阶段" className="text-button is-danger" onClick={() => removeWorkflowStage(index)} type="button">删除</button>
                          </div>
                        ) : <span className="recruiting-stage-locked">固定</span>}
                      </div>
                    );
                  })}
                </div>
                <div className="recruiting-workflow-editor-actions">
                  <button className="button button-ghost" onClick={createWorkflowStage} type="button">
                    <Icon name="plus" size={15} />添加阶段
                  </button>
                  <button className="button" onClick={() => setWorkflowEditorOpen(false)} type="button">取消</button>
                  <button className="button button-primary" disabled={workflowSaving} type="submit">
                    {workflowSaving ? <><i className="spinner" />正在发布</> : "发布并应用"}
                  </button>
                </div>
              </form>
            )}
          </section>
        </aside>
      </div>
    </div>
  );
}

function ApplicationCard({
  activeStages,
  application,
  detail,
  historyLoading,
  historyOpen,
  moving,
  note,
  onAction,
  onHistory,
  onNoteChange,
  onOpenCandidate,
}: {
  activeStages: RecruitingWorkflowStage[];
  application: JobApplication;
  detail: JobApplicationDetail | undefined;
  historyLoading: boolean;
  historyOpen: boolean;
  moving: boolean;
  note: string;
  onAction: (application: JobApplication, action: ApplicationAction) => void;
  onHistory: () => void;
  onNoteChange: (note: string) => void;
  onOpenCandidate?: (application: JobApplication) => void;
}) {
  const currentActiveIndex = activeStages.findIndex(
    (stage) => stage.stage_id === application.current_stage_id,
  );
  const isActive = application.status === "active";
  const canReturn = isActive && currentActiveIndex > 0;
  const canAdvance = isActive && currentActiveIndex >= 0 && currentActiveIndex < activeStages.length - 1;
  const canHire = isActive && currentActiveIndex === activeStages.length - 1;
  const transitions = detail?.stage_transitions ?? [];

  return (
    <section className="recruiting-application-card">
      <div className="recruiting-application-card-title">
        <strong>{application.candidate_display_name ?? "未命名候选人"}</strong>
        <span>{application.round_number > 1 ? `第 ${application.round_number} 轮` : applicationStatusLabels[application.status]}</span>
      </div>
      <div className="recruiting-application-card-meta">
        <span>JD v{application.job_version_number}</span>
        <span>流程 v{application.workflow_version_number}</span>
        <span>简历事实 v{application.resume_facts_version}</span>
      </div>
      {onOpenCandidate && (
        <button
          className="text-button recruiting-candidate-open"
          onClick={() => onOpenCandidate(application)}
          type="button"
        >
          <Icon name="document" size={14} />查看候选人
        </button>
      )}
      {isActive && (
        <>
          <input
            aria-label={`${application.candidate_display_name ?? "候选人"}的流转备注`}
            className="field recruiting-transition-note"
            onChange={(event) => onNoteChange(event.target.value)}
            placeholder="流转备注（可选）"
            value={note}
          />
          <div className="recruiting-application-actions">
            <button className="button button-ghost" disabled={moving || !canReturn} onClick={() => onAction(application, "return")} type="button">退回</button>
            <button className="button button-ghost" disabled={moving || !canAdvance} onClick={() => onAction(application, "advance")} type="button">推进</button>
            <button className="button button-danger-ghost" disabled={moving} onClick={() => onAction(application, "reject")} type="button">淘汰</button>
            {canHire && <button className="button button-primary" disabled={moving} onClick={() => onAction(application, "hire")} type="button">录用</button>}
          </div>
        </>
      )}
      <button
        aria-expanded={historyOpen}
        className="text-button recruiting-history-toggle"
        onClick={onHistory}
        type="button"
      >
        <Icon name="history" size={14} />流转记录
      </button>
      {historyOpen && (
        <ol className="recruiting-transition-history">
          {historyLoading ? (
            <li className="recruiting-history-loading"><i className="spinner" />正在加载</li>
          ) : transitions.map((transition) => (
            <li key={transition.transition_id}>
              <strong>{applicationTransitionLabel(transition)}</strong>
              <span>{formatDateTime(transition.created_at)}</span>
              {transition.note && <p>{transition.note}</p>}
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
