import {
  useEffect,
  useMemo,
  useState,
  type CSSProperties,
} from "react";
import { api } from "../../api";
import type {
  JobMatch,
  JobMatchBatch,
  JobMatchBatchItem,
  JobMatchRequirementResult,
  JobRequirements,
  JobVersion,
} from "../../types";
import { Icon } from "../../icons";
import { BackofficeButton } from "../../backoffice/ui/BackofficeButton";
import { BackofficeProgress } from "../../backoffice/ui/BackofficeProgress";
import { BackofficeSelect } from "../../backoffice/ui/BackofficeSelect";
import { useJobMatchBatchPolling } from "./useJobMatchBatchPolling";
import "./job-match.css";

type ToastKind = "success" | "error";
type JobWorkspaceMode = "create" | "view";
export type MatchWorkspaceSurface = "jobs" | "matching";

/**
 * The matching surface intentionally selects a precise immutable JD version.
 * Job management has a different task: one selector item must represent one
 * recruiting Job, with its latest confirmed version shown by default.
 */
function latestConfirmedVersionPerJob(versions: JobVersion[]): JobVersion[] {
  const latestByJobId = new Map<string, JobVersion>();
  for (const version of versions) {
    const current = latestByJobId.get(version.job_id);
    if (!current || version.version > current.version) {
      latestByJobId.set(version.job_id, version);
    }
  }
  return [...latestByJobId.values()];
}

function jobManagementLabel(version: JobVersion, versionCount: number): string {
  const labels = [version.title, `最新 v${version.version}`];
  if (versionCount > 1) labels.push(`${versionCount} 个版本`);
  if (!version.requirements.length) labels.push("原版");
  return labels.join(" · ");
}

export function MatchWorkspace({
  canGenerateAiJd,
  createNewJob = false,
  formatError,
  initialJobVersionId,
  mode = "jobs",
  notify,
  onCreateNewJob,
  onInvalidJobVersion,
  onJobVersionChange,
  onOpenJobManagement,
  onOpenMatching,
  onOpenMatchedResume,
}: {
  canGenerateAiJd: boolean;
  /** A routed, explicit new-JD action must not fall back to the first saved JD. */
  createNewJob?: boolean;
  formatError: (error: unknown) => string;
  initialJobVersionId?: string;
  /** Jobs owns JD authoring; matching only consumes a confirmed JD version. */
  mode?: MatchWorkspaceSurface;
  notify: (kind: ToastKind, message: string) => void;
  onCreateNewJob?: () => void;
  onInvalidJobVersion?: () => void;
  onJobVersionChange?: (jobVersionId: string) => void;
  onOpenJobManagement?: () => void;
  onOpenMatching?: (jobVersionId: string) => void;
  onOpenMatchedResume: (match: JobMatch) => void;
}) {
  const [title, setTitle] = useState("");
  const [jobBrief, setJobBrief] = useState("");
  const [jdText, setJdText] = useState("");
  const [editedGeneratedJd, setEditedGeneratedJd] = useState(false);
  const [generatedRequirements, setGeneratedRequirements] =
    useState<JobRequirements | null>(null);
  const [generationError, setGenerationError] = useState<string | null>(null);
  const [jobWorkspaceMode, setJobWorkspaceMode] =
    useState<JobWorkspaceMode>("create");
  const [jobVersion, setJobVersion] = useState<JobVersion | null>(null);
  const [versioningJobId, setVersioningJobId] = useState<string | null>(null);
  const [confirmedJobVersions, setConfirmedJobVersions] = useState<JobVersion[]>([]);
  const [loading, setLoading] = useState(false);
  const [matchBatch, setMatchBatch] = useState<JobMatchBatch | null>(null);
  const [batchItems, setBatchItems] = useState<JobMatchBatchItem[]>([]);
  const [jobMatches, setJobMatches] = useState<JobMatch[]>([]);
  const [matchesLoading, setMatchesLoading] = useState(false);
  const latestConfirmedJobs = useMemo(
    () => latestConfirmedVersionPerJob(confirmedJobVersions),
    [confirmedJobVersions],
  );
  const confirmedVersionsForCurrentJob = useMemo(
    () => jobVersion?.job_id
      ? confirmedJobVersions
        .filter((item) => item.job_id === jobVersion.job_id)
        .sort((left, right) => right.version - left.version)
      : [],
    [confirmedJobVersions, jobVersion?.job_id],
  );
  const confirmedVersionCounts = useMemo(() => {
    const counts = new Map<string, number>();
    for (const version of confirmedJobVersions) {
      counts.set(version.job_id, (counts.get(version.job_id) ?? 0) + 1);
    }
    return counts;
  }, [confirmedJobVersions]);
  const resetJobAuthoring = () => {
    setTitle("");
    setJobBrief("");
    setJdText("");
    setEditedGeneratedJd(false);
    setGeneratedRequirements(null);
    setGenerationError(null);
  };
  const selectJobVersion = (next: JobVersion, syncRoute = true) => {
    resetJobAuthoring();
    setJobWorkspaceMode("view");
    setJobVersion(next);
    setVersioningJobId(null);
    setMatchBatch(null);
    setBatchItems([]);
    if (syncRoute) onJobVersionChange?.(next.job_version_id);
  };
  const enterNewJobDraft = () => {
    resetJobAuthoring();
    setJobWorkspaceMode("create");
    setJobVersion(null);
    setVersioningJobId(null);
    setMatchBatch(null);
    setBatchItems([]);
    setJobMatches([]);
  };
  const beginNewJob = () => {
    enterNewJobDraft();
    onCreateNewJob?.();
  };
  const beginNextJobVersion = () => {
    if (!jobVersion) return;
    setJobWorkspaceMode("create");
    setVersioningJobId(jobVersion.job_id);
    setTitle(jobVersion.title);
    setJobBrief(jobVersion.raw_text);
    setJdText("");
    setEditedGeneratedJd(false);
    setGeneratedRequirements(null);
    setGenerationError(null);
    setMatchBatch(null);
    setBatchItems([]);
  };
  const requirementsAreReady = (requirements: JobRequirements | null) =>
    Boolean(
      requirements &&
        ((requirements.must_have?.some((item) => item.trim()) ?? false) ||
          (requirements.preferred?.some((item) => item.trim()) ?? false)),
    );
  const updateGeneratedRequirement = (
    priority: "must_have" | "preferred",
    index: number,
    value: string,
  ) => {
    setGeneratedRequirements((current) => {
      const next = {
        must_have: [...(current?.must_have ?? [])],
        preferred: [...(current?.preferred ?? [])],
      };
      next[priority][index] = value;
      return next;
    });
  };
  const addGeneratedRequirement = (priority: "must_have" | "preferred") => {
    setGeneratedRequirements((current) => ({
      must_have: [...(current?.must_have ?? [])],
      preferred: [...(current?.preferred ?? [])],
      [priority]: [...(current?.[priority] ?? []), ""],
    }));
  };
  const removeGeneratedRequirement = (
    priority: "must_have" | "preferred",
    index: number,
  ) => {
    setGeneratedRequirements((current) => ({
      must_have: (current?.must_have ?? []).filter((_, itemIndex) => itemIndex !== (priority === "must_have" ? index : -1)),
      preferred: (current?.preferred ?? []).filter((_, itemIndex) => itemIndex !== (priority === "preferred" ? index : -1)),
    }));
  };
  const invalidateGeneratedRequirements = () => {
    if (!generatedRequirements) return;
    setGeneratedRequirements(null);
    setGenerationError("JD 已修改，请重新运行 AI 生成后再启用岗位。");
  };
  const generateJobDescription = async () => {
    if (!canGenerateAiJd) {
      notify("error", "当前套餐未开通 AI 生成 JD。你仍可直接发布原版 JD。");
      return;
    }
    const sourceText =
      (editedGeneratedJd && jdText.trim()) || jobBrief.trim();
    if (!title.trim() || !sourceText) {
      notify("error", "请填写岗位名称和岗位需求后再生成 JD。");
      return;
    }
    setGenerationError(null);
    setLoading(true);
    try {
      const generated = await api.generateJobDescription({
        title: title.trim(),
        brief: sourceText,
      });
      const generatedJd = generated.jd_text?.trim();
      if (!generatedJd) {
        throw new Error("AI 未返回可编辑的 JD");
      }
      setTitle(generated.title?.trim() || title.trim());
      setJdText(generatedJd);
      setEditedGeneratedJd(false);
      setGeneratedRequirements(generated.requirements ?? null);
      if (!requirementsAreReady(generated.requirements ?? null)) {
        setGenerationError(
          "AI 已生成 JD，但没有返回可用于匹配的条件。请补充岗位需求后重新生成。",
        );
        notify("error", "AI 未生成可用的匹配条件，请补充需求后重试。");
        return;
      }
      notify("success", "AI 已生成 JD 和匹配条件。确认内容后即可启用岗位。");
    } catch (error) {
      const message = formatError(error);
      setGenerationError(message);
      notify("error", message);
    } finally {
      setLoading(false);
    }
  };
  const enableJob = async () => {
    if (!title.trim() || !jdText.trim()) {
      notify("error", "请先生成或粘贴完整 JD。");
      return;
    }
    if (!requirementsAreReady(generatedRequirements)) {
      const message = "JD 已修改或匹配条件不完整，请先重新运行 AI 生成。";
      setGenerationError(message);
      notify("error", message);
      return;
    }
    setGenerationError(null);
    setLoading(true);
    try {
      const requirements = generatedRequirements
        ? {
            must_have: (generatedRequirements.must_have ?? [])
              .map((item) => item.trim())
              .filter(Boolean),
            preferred: (generatedRequirements.preferred ?? [])
              .map((item) => item.trim())
              .filter(Boolean),
          }
        : undefined;
      const payload = {
        title: title.trim(),
        jd_text: jdText.trim(),
        requirements,
      };
      const created = versioningJobId
        ? await api.createJobVersion(versioningJobId, payload)
        : await api.createJob(payload);
      if (created.status !== "confirmed") {
        const message =
          "岗位已保存，但服务尚未返回可启用版本。请重新生成 JD 后再试。";
        setGenerationError(message);
        notify("error", message);
        return;
      }
      setConfirmedJobVersions((current) => [
        created,
        ...current.filter((item) => item.job_version_id !== created.job_version_id),
      ]);
      selectJobVersion(created);
      notify("success", "岗位已启用，现在可以开始匹配简历。");
    } catch (error) {
      const message = formatError(error);
      setGenerationError(message);
      notify("error", message);
    } finally {
      setLoading(false);
    }
  };
  const publishOriginalJob = async () => {
    if (!title.trim() || !jobBrief.trim()) {
      notify("error", "请填写岗位名称和完整原版 JD 后再发布。");
      return;
    }
    setGenerationError(null);
    setLoading(true);
    try {
      const payload = {
        title: title.trim(),
        // This deliberately retains every valid character entered in the JD.
        // The endpoint performs validation without normalizing the source text.
        jd_text: jobBrief,
      };
      const published = versioningJobId
        ? await api.publishOriginalJobVersion(versioningJobId, payload)
        : await api.publishOriginalJob(payload);
      setConfirmedJobVersions((current) => [
        published,
        ...current.filter(
          (item) => item.job_version_id !== published.job_version_id,
        ),
      ]);
      selectJobVersion(published);
      notify("success", "原版 JD 已发布，内容未经过 AI 处理。");
    } catch (error) {
      const message = formatError(error);
      setGenerationError(message);
      notify("error", message);
    } finally {
      setLoading(false);
    }
  };
  const runAllMatches = async () => {
    if (!jobVersion || jobVersion.status !== "confirmed") {
      notify("error", "请先启用岗位，再批量匹配简历。");
      return;
    }
    if (!jobVersion.requirements.length) {
      notify("error", "原版 JD 未生成匹配条件，不能批量运行 AI 匹配。");
      return;
    }
    setLoading(true);
    try {
      const response = await api.enqueueAllJobMatches(
        jobVersion.job_version_id,
      );
      setMatchBatch(response);
      setBatchItems([]);
      notify(
        "success",
        `已将 ${response.total_count} 份简历加入岗位评估队列。`,
      );
    } catch (error) {
      notify("error", formatError(error));
    } finally {
      setLoading(false);
    }
  };
  useJobMatchBatchPolling({
    batchId: matchBatch?.batch_id,
    onBatch: setMatchBatch,
    onItems: setBatchItems,
  });
  useEffect(() => {
    let cancelled = false;
    void api
      .listConfirmedJobVersions()
      .then((versions) => {
        if (cancelled) return;
        setConfirmedJobVersions(versions);
        if (mode === "jobs" && createNewJob) {
          enterNewJobDraft();
          return;
        }
        const defaultVersions = mode === "jobs"
          ? latestConfirmedVersionPerJob(versions)
          : versions;
        const initial = initialJobVersionId
          ? versions.find((item) => item.job_version_id === initialJobVersionId)
          : defaultVersions[0];
        if (initialJobVersionId && !initial) {
          notify("error", "该岗位 JD 不存在或无权访问，已回到可访问的岗位。");
          onInvalidJobVersion?.();
          return;
        }
        if (initial) selectJobVersion(initial, false);
      })
      .catch(() => {
        // A new workspace has no confirmed JD yet. The creation form remains usable.
      });
    return () => {
      cancelled = true;
    };
  }, [createNewJob, initialJobVersionId, mode, notify, onInvalidJobVersion]);
  useEffect(() => {
    if (
      mode !== "matching" ||
      !jobVersion ||
      jobVersion.status !== "confirmed" ||
      !jobVersion.requirements.length
    ) {
      setJobMatches([]);
      return;
    }
    let cancelled = false;
    setMatchesLoading(true);
    void api
      .listJobVersionMatches(jobVersion.job_version_id)
      .then((items) => {
        if (!cancelled) setJobMatches(items);
      })
      .catch((error) => {
        if (!cancelled) notify("error", formatError(error));
      })
      .finally(() => {
        if (!cancelled) setMatchesLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [jobVersion?.job_version_id, jobVersion?.status, matchBatch?.completed_count, mode, notify]);
  const isJobManagement = mode === "jobs";
  const isMatching = mode === "matching";
  const jobIsEnabled =
    jobWorkspaceMode === "view" && jobVersion?.status === "confirmed";
  const jobIsOriginal = Boolean(
    jobIsEnabled && jobVersion && jobVersion.requirements.length === 0,
  );
  const jobCanMatch = Boolean(
    jobIsEnabled && jobVersion && jobVersion.requirements.length > 0,
  );
  const generatedJobIsReady = requirementsAreReady(generatedRequirements);
  return (
    <div className={`page-frame job-match-workspace is-${mode}`}>
      <header className="page-heading">
        <div>
          <h1>{isJobManagement ? "职位管理" : "智能匹配"}</h1>
          <p>
            {isJobManagement
              ? (canGenerateAiJd
                ? "创建、发布和维护岗位 JD 版本。确认后的版本可用于候选人匹配。"
                : "可直接发布原版 JD；AI 生成 JD 需要开通相应套餐。")
              : "选择已确认的岗位 JD，查看候选人匹配依据。"}
          </p>
        </div>
      </header>
      <div className="page-layout">
        <div>
          <section className="panel">
            {confirmedJobVersions.length > 0 && (
              <div className="jd-switcher">
                <div>
                  <span className="field-label">
                    {isJobManagement ? "已保存的岗位" : "已保存的岗位 JD"}
                  </span>
                  <p>
                    {isJobManagement
                      ? "每个岗位只显示一条，进入后可查看和切换其 JD 版本。"
                      : "切换后将显示该岗位 JD 对应的候选人匹配结果。"}
                  </p>
                </div>
                <div className="jd-switcher-select">
                  <BackofficeSelect
                    ariaLabel={isJobManagement ? "切换已保存的岗位" : "选择用于智能匹配的岗位 JD"}
                    id="saved-job-selector"
                    onChange={(value) => {
                      if (!value) {
                        if (isJobManagement) beginNewJob();
                        return;
                      }
                      const next = isJobManagement
                        ? latestConfirmedJobs.find((item) => item.job_id === value)
                        : confirmedJobVersions.find((item) => item.job_version_id === value);
                      if (next) selectJobVersion(next);
                    }}
                    options={[
                      ...(isJobManagement ? [{ label: "新建岗位 JD", value: "" }] : []),
                      ...(isJobManagement ? latestConfirmedJobs : confirmedJobVersions).map((item) => ({
                        label: isJobManagement
                          ? jobManagementLabel(item, confirmedVersionCounts.get(item.job_id) ?? 1)
                          : `${item.title} · v${item.version}${!item.requirements.length ? " · 原版" : ""}`,
                        value: isJobManagement ? item.job_id : item.job_version_id,
                      })),
                    ]}
                    value={
                      isJobManagement && versioningJobId
                        ? versioningJobId
                        : jobWorkspaceMode === "view"
                        ? (isJobManagement
                          ? (jobVersion?.job_id ?? "")
                          : (jobVersion?.job_version_id ?? ""))
                        : ""
                    }
                  />
                </div>
              </div>
            )}
            {jobWorkspaceMode === "view" && jobVersion ? (
              <div className="field-stack">
                <div className="panel-heading">
                  <div>
                    <h2>{jobVersion.title}</h2>
                    <p>
                      {jobIsOriginal
                        ? "原版内容已发布，未调用 AI，也不包含用于简历匹配的条件。"
                        : "已启用，当前匹配结果仅基于这份岗位 JD。"}
                    </p>
                  </div>
                  <div className="jd-view-actions">
                    <span className="status-pill">
                      {jobIsOriginal ? "原版已发布" : "已启用"}
                    </span>
                    {isJobManagement && (
                      <button
                        className="button button-ghost"
                        onClick={beginNextJobVersion}
                        type="button"
                      >
                        <Icon name="plus" size={15} />
                        基于此新建版本
                      </button>
                    )}
                    {isJobManagement && !jobIsOriginal && onOpenMatching && (
                      <button
                        className="button button-primary"
                        onClick={() => onOpenMatching(jobVersion.job_version_id)}
                        type="button"
                      >
                        <Icon name="match" size={15} />查看智能匹配
                      </button>
                    )}
                  </div>
                </div>
                {isJobManagement && confirmedVersionsForCurrentJob.length > 1 && (
                  <div className="job-version-picker">
                    <div>
                      <span className="field-label">JD 版本</span>
                      <p>当前岗位保留 {confirmedVersionsForCurrentJob.length} 个已发布版本。</p>
                    </div>
                    <BackofficeSelect
                      ariaLabel="切换当前岗位的 JD 版本"
                      id="saved-job-version-selector"
                      onChange={(value) => {
                        const next = confirmedVersionsForCurrentJob.find(
                          (item) => item.job_version_id === value,
                        );
                        if (next) selectJobVersion(next);
                      }}
                      options={confirmedVersionsForCurrentJob.map((item) => ({
                        label: `v${item.version}${!item.requirements.length ? " · 原版" : ""}`,
                        value: item.job_version_id,
                      }))}
                      value={jobVersion.job_version_id}
                    />
                  </div>
                )}
                <label className="field-label" htmlFor="active-job-text">
                  岗位 JD 原文
                </label>
                <textarea
                  aria-label="当前已启用岗位的 JD 原文"
                  className="textarea-field job-jd-original-field"
                  id="active-job-text"
                  readOnly
                  rows={12}
                  value={jobVersion.raw_text}
                />
              </div>
            ) : isJobManagement ? (
              <>
                {versioningJobId && (
                  <p className="version-context" role="status">
                    正在基于当前岗位创建新版本。原版本和已有匹配结果会完整保留。
                  </p>
                )}
                <div className="jd-publish-guide" role="note">
                  <Icon aria-hidden="true" name="briefcase" size={18} />
                  <div>
                    <strong>发布方式</strong>
                    <span>
                      原样发布只保存下方输入，不调用 AI；AI 版会生成可编辑的 JD 和匹配条件。
                    </span>
                  </div>
                </div>
                <div className="form-grid">
                  <div className="field-stack span-full">
                    <label className="field-label" htmlFor="job-title">
                      岗位名称
                    </label>
                    <input
                      className="field"
                      id="job-title"
                      onChange={(event) => {
                        invalidateGeneratedRequirements();
                        setTitle(event.target.value);
                      }}
                      placeholder="例如：大模型应用架构师"
                      value={title}
                    />
                  </div>
                  <div className="field-stack span-full">
                    <label className="field-label" htmlFor="job-brief">
                      岗位需求或完整 JD
                    </label>
                    <textarea
                      className="textarea-field"
                      id="job-brief"
                      onChange={(event) => {
                        setJobBrief(event.target.value);
                        setGenerationError(null);
                        if (jdText) {
                          setJdText("");
                          setEditedGeneratedJd(false);
                          setGeneratedRequirements(null);
                        }
                      }}
                      placeholder={
                        canGenerateAiJd
                          ? "填写岗位需求后点击「AI 生成 JD」；已有完整 JD 可直接粘贴后点击「原版发布」。"
                          : "粘贴完整原版 JD 后点击「原版发布」，内容会按原样保存。"
                      }
                      value={jobBrief}
                    />
                    <p className="candidate-meta">
                      {canGenerateAiJd
                        ? "AI 生成 JD 会提取匹配条件；原样发布始终只保存这里的输入，不会使用 AI 生成内容。"
                        : "原版发布不会调用 AI，内容会按当前输入原样保存。"}
                    </p>
                  </div>
                  {jdText && (
                    <div className="field-stack span-full">
                      <label className="field-label" htmlFor="job-text">
                        AI 生成的 JD
                      </label>
                      <textarea
                        className="textarea-field"
                        id="job-text"
                        onChange={(event) => {
                          invalidateGeneratedRequirements();
                          setEditedGeneratedJd(true);
                          setJdText(event.target.value);
                        }}
                        value={jdText}
                      />
                      <p className="candidate-meta">
                        可以直接编辑。编辑后请重新生成，以同步用于匹配的 AI 条件。
                      </p>
                    </div>
                  )}
                </div>
                {generationError && (
                  <p className="library-error" role="alert">
                    {generationError}
                  </p>
                )}
                <div className="review-actions jd-authoring-actions">
                  <BackofficeButton
                    disabled={loading}
                    icon={<Icon name="briefcase" size={16} />}
                    onClick={() => void publishOriginalJob()}
                    tone={canGenerateAiJd ? "default" : "primary"}
                  >
                    {versioningJobId ? "发布原版新版本" : "原样发布 JD"}
                  </BackofficeButton>
                  {canGenerateAiJd && (
                    <BackofficeButton
                      disabled={loading}
                      icon={<Icon name="spark" size={16} />}
                      onClick={() => void generateJobDescription()}
                      tone="primary"
                    >
                      {jdText ? "重新生成 AI JD" : "AI 生成 JD"}
                    </BackofficeButton>
                  )}
                  {jdText && (
                    <BackofficeButton
                      disabled={loading || !generatedJobIsReady}
                      icon={<Icon name="check" size={16} />}
                      onClick={() => void enableJob()}
                      tone="primary"
                    >
                      {versioningJobId ? "发布新版本" : "启用岗位"}
                    </BackofficeButton>
                  )}
                </div>
              </>
            ) : (
              <div className="empty-state job-match-selection-empty">
                <div className="empty-state-inner">
                  <span className="empty-glyph"><Icon name="briefcase" size={23} /></span>
                  <h2>先发布一个岗位 JD</h2>
                  <p>智能匹配只使用已确认的岗位版本，不会在这里创建或修改 JD。</p>
                  {onOpenJobManagement && (
                    <button className="button button-primary" onClick={onOpenJobManagement} type="button">
                      <Icon name="briefcase" size={16} />前往职位管理
                    </button>
                  )}
                </div>
              </div>
            )}
          </section>
          {isJobManagement && jobWorkspaceMode === "create" && generatedJobIsReady && (
            <section className="panel">
              <div className="panel-heading">
                <div>
                  <h2>AI 识别的匹配条件</h2>
                  <p>可以直接修订条件与优先级，发布后会固化为该 JD 版本的匹配依据。</p>
                </div>
              </div>
              <div className="requirements-list">
                {(generatedRequirements?.must_have ?? []).map((requirement, index) => (
                  <div className="requirement-row" key={`must-${index}-${requirement}`}>
                    <span className="priority-must">必须</span>
                    <input
                      aria-label={`第 ${index + 1} 条必备条件`}
                      className="field requirement-input"
                      onChange={(event) => updateGeneratedRequirement("must_have", index, event.target.value)}
                      value={requirement}
                    />
                    <button
                      aria-label={`删除第 ${index + 1} 条必备条件`}
                      className="icon-button requirement-remove"
                      onClick={() => removeGeneratedRequirement("must_have", index)}
                      type="button"
                    >
                      <Icon name="close" size={15} />
                    </button>
                  </div>
                ))}
                {(generatedRequirements?.preferred ?? []).map((requirement, index) => (
                  <div className="requirement-row" key={`preferred-${index}-${requirement}`}>
                    <span className="priority-preferred">优先</span>
                    <input
                      aria-label={`第 ${index + 1} 条优先条件`}
                      className="field requirement-input"
                      onChange={(event) => updateGeneratedRequirement("preferred", index, event.target.value)}
                      value={requirement}
                    />
                    <button
                      aria-label={`删除第 ${index + 1} 条优先条件`}
                      className="icon-button requirement-remove"
                      onClick={() => removeGeneratedRequirement("preferred", index)}
                      type="button"
                    >
                      <Icon name="close" size={15} />
                    </button>
                  </div>
                ))}
              </div>
              <div className="requirement-actions">
                <button className="button button-ghost" onClick={() => addGeneratedRequirement("must_have")} type="button">
                  <Icon name="plus" size={15} /> 添加必备条件
                </button>
                <button className="button button-ghost" onClick={() => addGeneratedRequirement("preferred")} type="button">
                  <Icon name="plus" size={15} /> 添加优先条件
                </button>
              </div>
            </section>
          )}
          {jobIsOriginal && (
            <section className="panel">
              <div className="panel-heading">
                <div>
                  <h2>原版发布</h2>
                  <p>已按原文发布，未调用 AI，未生成用于简历匹配的条件。</p>
                </div>
              </div>
            </section>
          )}
          {isMatching && jobCanMatch && jobVersion && (
            <section className="panel">
              <div className="panel-heading">
                <div>
                  <h2>当前岗位的匹配条件</h2>
                  <p>这些条件已随岗位启用，并用于当前的简历匹配。</p>
                </div>
              </div>
              <div className="requirements-list">
                {jobVersion.requirements.map((requirement) => (
                  <div className="requirement-row" key={requirement.requirement_id}>
                    <span
                      className={
                        requirement.priority === "must_have"
                          ? "priority-must"
                          : "priority-preferred"
                      }
                    >
                      {requirement.priority === "must_have" ? "必须" : "优先"}
                    </span>
                    <p>{requirement.raw_requirement}</p>
                    <span className="candidate-meta">{requirement.category}</span>
                  </div>
                ))}
              </div>
              <div className="requirement-actions match-run-actions">
                <button
                  className="button button-primary"
                  disabled={!jobCanMatch || loading}
                  onClick={() => void runAllMatches()}
                  type="button"
                >
                  {loading ? (
                    <>
                      <i className="spinner" />
                      正在创建评估任务…
                    </>
                  ) : (
                    <>
                      <Icon name="match" size={16} />
                      开始岗位评分（全部可匹配简历）
                    </>
                  )}
                </button>
              </div>
            </section>
          )}
          {isMatching && matchBatch && (
            <MatchBatchDetails batch={matchBatch} items={batchItems} />
          )}
          {isMatching && jobCanMatch && (
            <MatchLeaderboard
              loading={matchesLoading}
              matches={jobMatches}
              onOpenResume={onOpenMatchedResume}
            />
          )}
        </div>
      </div>
    </div>
  );
}

type MatchLane = "recommended" | "pending" | "unmet";

const hardRequirementLabel: Record<string, string> = {
  partial: "硬条件部分满足",
  pass: "硬条件通过",
  unmet: "硬条件未满足",
  information_insufficient: "硬条件待核实",
  not_applicable: "无硬条件",
};

function clampMatchPercent(value: number): number {
  return Math.max(0, Math.min(100, value));
}

/**
 * The server derives the match degree as the weighted total over ALL JD
 * requirements (max 100). For a match created before that was true, fall
 * back to the raw weighted total rather than re-normalizing by coverage.
 */
function matchScore(match: JobMatch): number {
  if (
    typeof match.match_score === "number" &&
    Number.isFinite(match.match_score)
  ) {
    return clampMatchPercent(match.match_score);
  }
  return clampMatchPercent(
    typeof match.total_score === "number" && Number.isFinite(match.total_score)
      ? match.total_score
      : 0,
  );
}

function matchLane(match: JobMatch): MatchLane {
  if (
    match.match_lane === "recommended" ||
    match.match_lane === "pending" ||
    match.match_lane === "unmet"
  ) {
    return match.match_lane;
  }

  if (match.hard_requirement_status === "unmet") return "unmet";
  if (
    match.hard_requirement_status === "pass" ||
    match.hard_requirement_status === "not_applicable"
  ) {
    return "recommended";
  }
  return "pending";
}

function compareMatchesByNewest(left: JobMatch, right: JobMatch): number {
  const leftTime = Date.parse(left.created_at);
  const rightTime = Date.parse(right.created_at);
  const timeDifference =
    (Number.isFinite(rightTime) ? rightTime : 0) -
    (Number.isFinite(leftTime) ? leftTime : 0);
  if (timeDifference) return timeDifference;
  return right.match_id.localeCompare(left.match_id);
}

function MatchResult({ match }: { match: JobMatch }) {
  const jdMatchScore = matchScore(match);
  const hardStatus = match.hard_requirement_status ?? "unknown";
  const scoreStyle = {
    "--score": jdMatchScore,
  } as CSSProperties;
  return (
    <section className="panel">
      <div className="panel-heading">
        <div>
          <h2>匹配结果</h2>
          <p>
            岗位版本 {match.job_version} · 简历事实版本 {match.facts_version} ·{" "}
            {hardRequirementLabel[hardStatus] ?? "硬条件待确认"}
          </p>
        </div>
      </div>
      <div className="score-result match-result-layout">
        <div className="match-result-score-panel">
          <div
            aria-label={`JD 匹配度 ${jdMatchScore.toFixed(1)}%`}
            className="score-number"
            data-value={`${jdMatchScore.toFixed(1)}%`}
            style={scoreStyle}
          >
            <span>{jdMatchScore.toFixed(1)}%</span>
          </div>
          <p className="match-result-score-label">JD 匹配度</p>
          <span className={`match-hard-status is-${hardStatus}`}>
            {hardRequirementLabel[hardStatus] ?? "待确认"}
          </span>
        </div>
        <div className="requirements-list">
          {match.requirement_results.map((item) => (
            <div className="requirement-row" key={item.requirement_id}>
              <span className={`outcome-${item.outcome.replace("_", "-")}`}>
                {item.outcome === "met"
                  ? "已满足"
                  : item.outcome === "partial"
                    ? "部分满足"
                    : item.outcome === "not_met"
                      ? "未满足"
                      : "未提及"}
              </span>
              <p>
                <b>{item.requirement_text}</b>
                <br />
                {item.reason}
                {item.missing_or_uncertain
                  ? ` · ${item.missing_or_uncertain}`
                  : ""}
                <small className="match-fact-reference">
                  {item.fact_ids.length
                    ? `事实依据：${item.fact_ids.join("、")}`
                    : "未发现可验证的简历事实"}
                </small>
              </p>
              <span
                className={
                  item.priority === "must_have"
                    ? "priority-must"
                    : "priority-preferred"
                }
              >
                {item.priority === "must_have" ? "必须" : "优先"}
              </span>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

function MatchBatchDetails({
  batch,
  items,
}: {
  batch: JobMatchBatch;
  items: JobMatchBatchItem[];
}) {
  const failed = items.filter((item) => item.status === "failed");
  const settledCount = Math.min(
    batch.total_count,
    batch.completed_count + batch.failed_count,
  );
  const inProgressCount = Math.max(0, batch.total_count - settledCount);
  const progressPercent = batch.total_count
    ? Math.round((settledCount / batch.total_count) * 100)
    : 100;
  const progressText = batch.total_count
    ? `已处理 ${settledCount} / ${batch.total_count}${inProgressCount ? `，剩余 ${inProgressCount} 份` : ""}`
    : "没有符合条件的简历，无需处理";
  const batchStatus = batch.status === "partial"
    ? { label: "部分完成", tone: "is-warning" }
    : batch.status === "completed"
      ? { label: "已完成", tone: "is-success" }
      : batch.status === "queued"
        ? { label: "排队中", tone: "is-progress" }
        : { label: "运行中", tone: "is-progress" };
  return (
    <section className="panel match-batch-details">
      <div className="panel-heading">
        <div>
          <h2>岗位评估任务</h2>
          <p>{progressText}</p>
        </div>
        <span className={`status-pill ${batchStatus.tone}`}>{batchStatus.label}</span>
      </div>
      <div className="match-batch-progress">
        <span className="match-batch-progress-label">处理进度</span>
        <BackofficeProgress
          aria-label="岗位评估进度"
          aria-valuetext={progressText}
          orbitStroke="var(--surface-muted)"
          percent={progressPercent}
          showInfo
          size="large"
          stroke="var(--blue)"
        />
      </div>
      {failed.length ? (
        <div className="table-scroll">
          <table className="candidate-table batch-failure-table">
            <thead>
              <tr>
                <th scope="col">候选人</th>
                <th scope="col">事实版本</th>
                <th scope="col">尝试次数</th>
                <th scope="col">失败原因</th>
              </tr>
            </thead>
            <tbody>
              {failed.map((item) => (
                <tr key={item.item_id}>
                  <td>{item.candidate_display_name?.trim() || "未命名候选人"}</td>
                  <td>v{item.facts_version}</td>
                  <td>{item.attempt_count}</td>
                  <td>{item.last_error || "未知错误"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : batch.failed_count ? (
        <p className="library-error">任务报告了失败项，正在读取具体原因。</p>
      ) : (
        <p className="candidate-meta">
          {batch.status === "completed"
            ? "本批简历均已完成匹配。"
            : "失败项会在任务结束后显示具体原因。"}
        </p>
      )}
    </section>
  );
}

const matchLaneLabel: Record<MatchLane, string> = {
  recommended: "推荐",
  pending: "待核实",
  unmet: "不匹配",
};

const matchLaneOrder: Record<MatchLane, number> = {
  recommended: 0,
  pending: 1,
  unmet: 2,
};

function MatchLeaderboard({
  matches,
  loading,
  onOpenResume,
}: {
  matches: JobMatch[];
  loading: boolean;
  onOpenResume: (match: JobMatch) => void;
}) {
  const latestByResume = new Map<string, JobMatch>();
  const newestFirst = [...matches].sort(compareMatchesByNewest);
  for (const match of newestFirst) {
    if (!latestByResume.has(match.resume_id)) latestByResume.set(match.resume_id, match);
  }
  const ranked = [...latestByResume.values()].sort((left, right) => {
    const laneDifference = matchLaneOrder[matchLane(left)] - matchLaneOrder[matchLane(right)];
    if (laneDifference) return laneDifference;
    const scoreDifference = matchScore(right) - matchScore(left);
    if (scoreDifference) return scoreDifference;
    const coverageDifference = (right.evidence_coverage ?? 0) - (left.evidence_coverage ?? 0);
    if (coverageDifference) return coverageDifference;
    return compareMatchesByNewest(left, right);
  });
  return (
    <section className="panel match-leaderboard">
      <div className="panel-heading">
        <div>
          <h2>候选人评估结果</h2>
          <p>匹配度按 JD 全部要求加权计算（硬条件权重更高）；简历未提及的要求按未满足计。</p>
        </div>
        <span className="status-pill">{ranked.length} 份已完成</span>
      </div>
      {loading ? (
        <div
          aria-busy="true"
          aria-label="正在加载候选人匹配结果"
          className="match-results-loading"
        >
          <span className="skeleton match-results-loading-card" />
          <span className="skeleton match-results-loading-card" />
          <span className="skeleton match-results-loading-card" />
        </div>
      ) : ranked.length ? (
        <div className="match-table-wrap">
          <table className="match-table">
            <thead>
              <tr>
                <th scope="col">排名</th>
                <th scope="col">候选人</th>
                <th scope="col">分档</th>
                <th scope="col">硬条件</th>
                <th scope="col">匹配度</th>
                <th scope="col" className="match-open-column">操作</th>
              </tr>
            </thead>
            <tbody>
              {ranked.map((item, index) => (
                <MatchLaneCandidate
                  key={item.match_id}
                  lane={matchLane(item)}
                  match={item}
                  rank={index + 1}
                  onOpenResume={onOpenResume}
                />
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="empty-state match-empty-state">
          <div className="empty-state-inner">
            <span className="empty-glyph"><Icon name="match" size={23} /></span>
            <h2>尚未生成岗位评估</h2>
            <p>确认 JD 后，点击“开始岗位评分（全部可匹配简历）”即可在此查看排序结果。</p>
          </div>
        </div>
      )}
    </section>
  );
}

function matchOutcomeLabel(outcome: JobMatchRequirementResult["outcome"]): string {
  if (outcome === "met") return "已满足";
  if (outcome === "partial") return "部分满足";
  if (outcome === "not_met") return "未满足";
  return "未提及";
}

function factReferenceText(item: JobMatchRequirementResult): string {
  const readable = item.fact_evidence?.length
    ? item.fact_evidence.map((fact) => fact.summary).join("、")
    : "";
  const cited = readable || item.fact_ids.join("、");
  return cited ? `事实依据：${cited}` : "未发现可验证的简历事实";
}

function MatchLaneCandidate({
  match,
  lane,
  rank,
  onOpenResume,
}: {
  match: JobMatch;
  lane: MatchLane;
  rank: number;
  onOpenResume: (match: JobMatch) => void;
}) {
  const jdMatchScore = matchScore(match);
  const hardStatus = match.hard_requirement_status ?? "unknown";
  const met = match.requirement_results.filter(
    (result) => result.outcome === "met",
  ).length;
  const partial = match.requirement_results.filter(
    (result) => result.outcome === "partial",
  ).length;
  const unknown = match.requirement_results.filter(
    (result) => result.outcome === "unknown",
  ).length;
  const [expanded, setExpanded] = useState(false);
  const openResume = () => onOpenResume(match);
  return (
    <>
      <tr className="match-candidate-row" onClick={openResume}>
        <td className="match-rank">
          <button
            aria-expanded={expanded}
            aria-label={expanded ? "收起匹配证据明细" : "展开匹配证据明细"}
            className="button button-ghost match-expand-button"
            onClick={(event) => {
              event.stopPropagation();
              setExpanded((current) => !current);
            }}
            type="button"
          >
            <Icon name={expanded ? "chevron-down" : "chevron-right"} size={15} />
          </button>
          <span className="match-rank-number">{rank}</span>
        </td>
        <td>
          <strong>{match.candidate_display_name?.trim() || "未命名候选人"}</strong>
          <small>
            满足 {met} · 部分满足 {partial}
            {unknown > 0 && <> · 未提及 {unknown}</>}
          </small>
        </td>
        <td>
          <span className={`match-lane-tag is-${lane}`}>{matchLaneLabel[lane]}</span>
        </td>
        <td>
          <span className={`match-hard-status is-${hardStatus}`}>
            {hardRequirementLabel[hardStatus] ?? "待确认"}
          </span>
        </td>
        <td className="match-score">{jdMatchScore.toFixed(1)}%</td>
        <td>
          <button
            className="button button-ghost match-open-button"
            onClick={(event) => {
              event.stopPropagation();
              openResume();
            }}
            type="button"
          >
            <Icon name="document" size={15} />
            查看简历
          </button>
        </td>
      </tr>
      {expanded && (
        <tr className="match-detail-row">
          <td colSpan={6}>
            <div className="match-detail-inner">
              <h3 className="match-detail-title">逐条匹配依据</h3>
              {match.requirement_results.map((item) => (
                <div className="requirement-row" key={item.requirement_id}>
                  <span className={`outcome-${item.outcome.replace("_", "-")}`}>
                    {matchOutcomeLabel(item.outcome)}
                  </span>
                  <p>
                    <b>{item.requirement_text}</b>
                    <br />
                    {item.reason}
                    {item.missing_or_uncertain
                      ? ` · 待确认：${item.missing_or_uncertain}`
                      : ""}
                    <small className="match-fact-reference">
                      {factReferenceText(item)}
                    </small>
                  </p>
                  <span
                    className={
                      item.priority === "must_have"
                        ? "priority-must"
                        : "priority-preferred"
                    }
                  >
                    {item.priority === "must_have" ? "必须" : "优先"}
                  </span>
                </div>
              ))}
            </div>
          </td>
        </tr>
      )}
    </>
  );
}
