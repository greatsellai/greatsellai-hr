import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../../api";
import { Icon } from "../../icons";
import { BackofficeButton } from "../../backoffice/ui/BackofficeButton";
import { BackofficeInput } from "../../backoffice/ui/BackofficeInput";
import { BackofficeSelect } from "../../backoffice/ui/BackofficeSelect";
import { TableSkeleton } from "../../backoffice/ui/TableSkeleton";
import { formatFileSize, formatLibraryDate } from "../../backoffice/utils/formatters";
import type {
  MailboxBackgroundJob,
  MailboxBackgroundJobHistory,
  MailboxConfig,
  MailboxImportHistory,
  MailboxImportHistoryItem,
  MailboxProvider,
  MailboxRetentionOverview,
  MailboxRetentionPolicy,
  MailboxRetentionPreview,
  MailboxRetentionRun,
  MailboxRetentionRuns,
} from "../../types";
import { MailboxChannelList } from "./components/MailboxChannelList";
import { MailboxProviderPicker } from "./components/MailboxProviderPicker";
import {
  mailboxAuthenticationModeLabel,
  mailboxBackgroundJobStatusClass,
  mailboxBackgroundJobStatusLabel,
  mailboxCanSync,
  mailboxChannelStatus,
  mailboxChannelStatusClass,
  mailboxDraftFromConfig,
  mailboxDraftIsDirty,
  mailboxImportErrorLabel,
  mailboxImportStatusLabel,
  mailboxRetentionDueCount,
  mailboxRetentionPolicies,
  mailboxRetentionPolicyLabel,
  mailboxRetentionRunErrorLabel,
  mailboxRetentionRunStatusClass,
  mailboxRetentionRunStatusLabel,
  mailboxProviderDisplayName,
  mailboxRequiresAuthorization,
  mailboxSyncAlertTitle,
  newMailboxDraft,
  type MailboxDraft,
} from "./mailbox-model";
import "./mailbox.css";

export type MailboxToastKind = "success" | "error";

interface MailboxPageProps {
  embedded?: boolean;
  humanizeError: (error: unknown) => string;
  notify: (kind: MailboxToastKind, message: string) => void;
  onImported: () => void;
  role: "admin" | "recruiter" | null;
}

export function MailboxPage({
  notify,
  onImported,
  role,
  humanizeError,
  embedded = false,
}: MailboxPageProps) {
  const pageClassName = `mailbox-page${embedded ? " is-embedded" : " page-frame"}`;
  const [mailboxes, setMailboxes] = useState<MailboxConfig[]>([]);
  const [providers, setProviders] = useState<MailboxProvider[]>([]);
  const [selectedMailboxId, setSelectedMailboxId] = useState<string | null>(null);
  const [historyFilterMailboxId, setHistoryFilterMailboxId] = useState<string | null>(null);
  const [history, setHistory] = useState<MailboxImportHistory | null>(null);
  const [draft, setDraft] = useState<MailboxDraft>(() => newMailboxDraft());
  const [isCreating, setIsCreating] = useState(true);
  const [isEditingConnection, setIsEditingConnection] = useState(false);
  const [retention, setRetention] = useState<MailboxRetentionOverview | null>(null);
  const [retentionRuns, setRetentionRuns] = useState<MailboxRetentionRuns | null>(null);
  const [mailboxJobs, setMailboxJobs] = useState<MailboxBackgroundJobHistory | null>(null);
  const [loading, setLoading] = useState(true);
  const [providersLoading, setProvidersLoading] = useState(true);
  const [historyLoading, setHistoryLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [authorizing, setAuthorizing] = useState(false);
  const [reauthorizingMailboxId, setReauthorizingMailboxId] = useState<string | null>(null);
  const [enqueuingMailboxId, setEnqueuingMailboxId] = useState<string | null>(null);
  const [enqueuingAll, setEnqueuingAll] = useState(false);
  const [archiving, setArchiving] = useState(false);
  const [enqueuingRetryImportId, setEnqueuingRetryImportId] = useState<string | null>(null);
  const [retentionSaving, setRetentionSaving] = useState(false);
  const [previewingRetention, setPreviewingRetention] = useState(false);
  const [cleaningRetention, setCleaningRetention] = useState(false);
  const [retentionPreview, setRetentionPreview] = useState<MailboxRetentionPreview | null>(null);
  const [retentionPolicy, setRetentionPolicy] = useState<MailboxRetentionPolicy>("standard");
  const retentionRequestRef = useRef(0);
  const historyRequestRef = useRef(0);
  const mailboxJobPollInFlightRef = useRef(false);
  const manualMailboxJobIdsRef = useRef(new Set<string>());
  const handledMailboxJobIdsRef = useRef(new Set<string>());
  const handledOauthReturnRef = useRef(false);

  const selectedConfig = selectedMailboxId
    ? mailboxes.find((item) => item.mailbox_id === selectedMailboxId) ?? null
    : null;
  const draftProvider = providers.find((item) => item.provider_key === draft.providerKey) ?? null;
  const selectedMailboxRequiresAuthorization = Boolean(
    selectedConfig && mailboxRequiresAuthorization(selectedConfig),
  );
  const selectedMailboxCanSync = Boolean(selectedConfig && mailboxCanSync(selectedConfig));
  const selectedMailboxArchived = Boolean(selectedConfig?.archived_at);
  const activeMailboxJobs = (mailboxJobs?.items ?? []).filter(
    (job) => job.status === "queued" || job.status === "running",
  );
  const activeSyncMailboxIds = new Set(
    activeMailboxJobs
      .filter((job) => job.job_kind === "sync")
      .map((job) => job.mailbox_id),
  );
  const activeRetryImportIds = new Set(
    activeMailboxJobs
      .filter((job) => job.job_kind === "attachment_retry" && job.import_id)
      .flatMap((job) => job.import_id ? [job.import_id] : []),
  );
  const activeSyncAlerts = mailboxes.filter((item) => item.active_sync_alert);
  const selectedSyncJob = selectedMailboxId
    ? activeMailboxJobs.find(
      (job) => job.mailbox_id === selectedMailboxId && job.job_kind === "sync",
    ) ?? null
    : null;
  const selectedSyncInProgress = Boolean(
    selectedMailboxId
    && (activeSyncMailboxIds.has(selectedMailboxId) || enqueuingMailboxId === selectedMailboxId),
  );
  const canManageRetention = role === "admin";
  const retentionHasActiveRun = Boolean(retentionRuns?.items.some(
    (run) => run.status === "queued" || run.status === "running",
  ));
  const retentionPolicyChanged = Boolean(
    retention && retention.retention_policy !== retentionPolicy,
  );

  const loadRetentionActivity = useCallback(async (mailboxId: string, reset = false) => {
    const requestId = ++retentionRequestRef.current;
    if (reset) {
      setRetention(null);
      setRetentionRuns(null);
      setRetentionPreview(null);
      setRetentionPolicy("standard");
    }

    const [nextRetention, nextRuns] = await Promise.all([
      api.getMailboxRetention(mailboxId),
      api.listMailboxRetentionRuns(mailboxId),
    ]);
    if (retentionRequestRef.current !== requestId) return;
    setRetention(nextRetention);
    setRetentionPolicy(nextRetention.retention_policy);
    setRetentionRuns(nextRuns);
  }, []);

  const clearRetentionActivity = useCallback(() => {
    retentionRequestRef.current += 1;
    setRetention(null);
    setRetentionRuns(null);
    setRetentionPreview(null);
    setRetentionPolicy("standard");
  }, []);

  const loadHistory = useCallback(async (mailboxId: string | null = historyFilterMailboxId) => {
    const requestId = ++historyRequestRef.current;
    setHistoryLoading(true);
    try {
      const nextHistory = await api.listMailboxImports(mailboxId);
      if (historyRequestRef.current !== requestId) return;
      setHistory(nextHistory);
    } catch (error) {
      if (historyRequestRef.current === requestId) {
        notify("error", humanizeError(error));
      }
    } finally {
      if (historyRequestRef.current === requestId) {
        setHistoryLoading(false);
      }
    }
  }, [historyFilterMailboxId, humanizeError, notify]);

  const confirmDiscardMailboxDraft = () => {
    if (!mailboxDraftIsDirty(draft, selectedConfig, isCreating)) return true;
    return window.confirm("尚未保存的收件通道设置会丢失，仍要离开吗？");
  };

  const selectMailbox = (config: MailboxConfig, force = false) => {
    if (!force && !confirmDiscardMailboxDraft()) return false;
    setSelectedMailboxId(config.mailbox_id);
    setDraft(mailboxDraftFromConfig(config));
    setIsCreating(false);
    setIsEditingConnection(false);
    setHistoryFilterMailboxId(config.mailbox_id);
    void loadHistory(config.mailbox_id);
    return true;
  };

  const startCreatingMailbox = (force = false) => {
    if (!force && !confirmDiscardMailboxDraft()) return false;
    setSelectedMailboxId(null);
    setDraft(newMailboxDraft());
    setIsCreating(true);
    setIsEditingConnection(false);
    return true;
  };

  const updateDraft = <Key extends keyof MailboxDraft>(key: Key, value: MailboxDraft[Key]) => {
    setDraft((current) => ({ ...current, [key]: value }));
  };

  const selectProvider = (provider: MailboxProvider) => {
    setDraft((current) => ({
      ...current,
      providerKey: provider.provider_key,
      mailbox: provider.default_mailbox,
      password: "",
    }));
  };

  const applyMailboxList = (items: MailboxConfig[], preferredMailboxId?: string | null) => {
    setMailboxes(items);
    const desiredMailboxId = preferredMailboxId ?? selectedMailboxId;
    const nextConfig = items.find((item) => item.mailbox_id === desiredMailboxId)
      ?? items.find((item) => !item.archived_at)
      ?? items[0]
      ?? null;
    if (nextConfig) {
      selectMailbox(nextConfig, true);
    } else {
      startCreatingMailbox(true);
    }
  };

  const upsertMailboxJobs = useCallback((jobs: MailboxBackgroundJob[]) => {
    setMailboxJobs((current) => {
      const byId = new Map((current?.items ?? []).map((job) => [job.job_id, job]));
      for (const job of jobs) byId.set(job.job_id, job);
      const items = [...byId.values()].sort((left, right) => (
        right.requested_at.localeCompare(left.requested_at)
      ));
      return {
        items,
        total: Math.max(current?.total ?? 0, items.length),
      };
    });
  }, []);

  const refreshMailboxJobs = useCallback(async () => {
    if (mailboxJobPollInFlightRef.current) return;
    mailboxJobPollInFlightRef.current = true;
    try {
      const next = await api.listMailboxBackgroundJobs();
      setMailboxJobs(next);

      const terminalManualJobs = next.items.filter((job) => (
        manualMailboxJobIdsRef.current.has(job.job_id)
        && !handledMailboxJobIdsRef.current.has(job.job_id)
        && (job.status === "completed" || job.status === "failed")
      ));
      if (!terminalManualJobs.length) return;

      for (const job of terminalManualJobs) {
        handledMailboxJobIdsRef.current.add(job.job_id);
        const mailboxName = mailboxes.find((item) => item.mailbox_id === job.mailbox_id)?.display_name
          ?? "收件通道";
        if (job.status === "failed") {
          notify("error", mailboxImportErrorLabel(job.last_error));
          continue;
        }
        if (job.job_kind === "attachment_retry") {
          notify("success", "附件已在后台重新入库。");
          continue;
        }
        const summary = `“${mailboxName}”后台同步完成：入库 ${job.imported_count} 份，重复 ${job.duplicate_count} 份，跳过 ${job.skipped_count} 份。`;
        notify(
          job.failed_count ? "error" : "success",
          job.failed_count ? `${summary} ${job.failed_count} 份处理失败。` : summary,
        );
      }

      if (terminalManualJobs.some((job) => job.imported_count > 0)) onImported();
      void api.listMailboxConfigs(true).then((response) => setMailboxes(response.items)).catch(() => undefined);
      void loadHistory(historyFilterMailboxId);
      if (selectedMailboxId) {
        void loadRetentionActivity(selectedMailboxId).catch(() => undefined);
      }
    } finally {
      mailboxJobPollInFlightRef.current = false;
    }
  }, [historyFilterMailboxId, loadHistory, loadRetentionActivity, mailboxes, notify, onImported, selectedMailboxId]);

  const loadInitialData = useCallback(async () => {
    setLoading(true);
    setProvidersLoading(true);
    setHistoryLoading(true);
    try {
      const [configResponse, providerResponse, historyResponse, jobsResponse] = await Promise.all([
        api.listMailboxConfigs(true),
        api.listMailboxProviders(),
        api.listMailboxImports(),
        api.listMailboxBackgroundJobs(),
      ]);
      applyMailboxList(configResponse.items);
      setProviders(providerResponse.items);
      setHistory(historyResponse);
      setMailboxJobs(jobsResponse);
    } catch (error) {
      notify("error", humanizeError(error));
    } finally {
      setLoading(false);
      setProvidersLoading(false);
      setHistoryLoading(false);
    }
  // The initial fetch intentionally runs once. Actions refresh only the data they change.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [humanizeError, notify]);

  useEffect(() => { void loadInitialData(); }, [loadInitialData]);

  useEffect(() => {
    if (loading || handledOauthReturnRef.current) return;
    const query = new URLSearchParams(window.location.search);
    const outcome = query.get("mailbox_oauth");
    if (outcome !== "connected" && outcome !== "failed") return;

    handledOauthReturnRef.current = true;
    const providerKey = query.get("mailbox_provider");
    query.delete("mailbox_oauth");
    query.delete("mailbox_provider");
    const nextSearch = query.toString();
    window.history.replaceState(
      window.history.state,
      "",
      `${window.location.pathname}${nextSearch ? `?${nextSearch}` : ""}${window.location.hash}`,
    );

    if (outcome === "failed") {
      notify("error", "邮箱授权没有完成。你可以检查服务商设置后重新发起授权。");
      return;
    }

    void api.listMailboxConfigs(true).then((response) => {
      setMailboxes(response.items);
      const connectedMailbox = providerKey
        ? response.items.find((item) => item.provider_key === providerKey && !item.archived_at)
        : null;
      if (connectedMailbox) selectMailbox(connectedMailbox, true);
      const providerName = providers.find((item) => item.provider_key === providerKey)?.display_name ?? "邮箱服务商";
      notify("success", `已连接 ${providerName}，系统只会接收绑定之后到达的附件。`);
    }).catch((error) => {
      notify("error", humanizeError(error));
    });
  }, [humanizeError, loading, notify, providers]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      // Do not replace the current form draft while the recruiter is editing.
      // This refresh only brings scheduled-worker alert and health changes
      // into the channel list when no browser task is active.
      void api.listMailboxConfigs(true)
        .then((response) => setMailboxes(response.items))
        .catch(() => undefined);
    }, 30_000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    if (!selectedConfig?.configured) {
      clearRetentionActivity();
      return undefined;
    }

    let active = true;
    void loadRetentionActivity(selectedConfig.mailbox_id, true).catch((error) => {
      if (active) notify("error", humanizeError(error));
    });
    return () => {
      active = false;
    };
  }, [
    clearRetentionActivity,
    humanizeError,
    loadRetentionActivity,
    notify,
    selectedConfig?.configured,
    selectedConfig?.mailbox_id,
  ]);

  useEffect(() => {
    if (!selectedConfig?.configured || !retentionHasActiveRun) return undefined;
    const mailboxId = selectedConfig.mailbox_id;
    const timer = window.setInterval(() => {
      void loadRetentionActivity(mailboxId).catch(() => undefined);
    }, 5_000);
    return () => window.clearInterval(timer);
  }, [
    loadRetentionActivity,
    retentionHasActiveRun,
    selectedConfig?.configured,
    selectedConfig?.mailbox_id,
  ]);

  useEffect(() => {
    if (!activeMailboxJobs.length) return undefined;
    void refreshMailboxJobs();
    const timer = window.setInterval(() => {
      void refreshMailboxJobs();
    }, 2_000);
    return () => window.clearInterval(timer);
  }, [activeMailboxJobs.length, refreshMailboxJobs]);

  const saveMailbox = async () => {
    if (!draft.displayName.trim()) {
      notify("error", "请为这个收件通道填写名称。");
      return;
    }
    if (!draft.emailAddress.trim()) {
      notify("error", "请填写接收简历的邮箱。");
      return;
    }
    if (isCreating && !draftProvider) {
      notify("error", "请先选择邮箱服务商。");
      return;
    }
    if (isCreating && !draftProvider?.available) {
      notify("error", "该邮箱服务商尚未在当前部署启用，请联系部署管理员。");
      return;
    }
    if (isCreating && draftProvider?.authentication_mode === "oauth2") {
      notify("error", "此服务商需要通过网页授权连接，请点击下方授权按钮。");
      return;
    }
    if (isCreating && !draft.password) {
      notify("error", "新通道首次保存需要填写邮箱专用授权码。");
      return;
    }
    if (!isCreating && !selectedConfig) {
      notify("error", "请先选择一个收件通道。");
      return;
    }
    if (!isCreating && selectedConfig?.archived_at) {
      notify("error", "归档通道仅保留历史与内容清理记录，不能再修改连接配置。");
      return;
    }

    setSaving(true);
    try {
      const saved = isCreating
        ? await api.createMailboxConfig({
          display_name: draft.displayName.trim(),
          provider_key: draftProvider!.provider_key,
          email_address: draft.emailAddress.trim(),
          mailbox: draft.mailbox.trim() || draftProvider!.default_mailbox,
          password: draft.password,
          enabled: draft.enabled,
        })
        : await api.updateMailboxConfig(selectedConfig!.mailbox_id, {
          display_name: draft.displayName.trim(),
          enabled: draft.enabled,
          ...(draft.password ? { password: draft.password } : {}),
        });
      setMailboxes((current) => [
        saved,
        ...current.filter((item) => item.mailbox_id !== saved.mailbox_id),
      ]);
      selectMailbox(saved, true);
      notify("success", isCreating ? "收件通道已创建，只会入库从现在起收到的附件。" : "收件通道已保存。");
    } catch (error) {
      notify("error", humanizeError(error));
    } finally {
      setSaving(false);
    }
  };

  const startMailboxOAuth = async () => {
    if (!draftProvider || draftProvider.authentication_mode !== "oauth2") {
      notify("error", "请先选择支持网页授权的邮箱服务商。");
      return;
    }
    if (!draftProvider.available) {
      notify("error", "该邮箱服务商尚未在当前部署启用，请联系部署管理员。");
      return;
    }
    if (!draft.displayName.trim()) {
      notify("error", "请为这个收件通道填写名称。");
      return;
    }
    if (!draft.emailAddress.trim()) {
      notify("error", "请填写接收简历的邮箱。");
      return;
    }

    setAuthorizing(true);
    try {
      const result = await api.startMailboxOAuth({
        provider_key: draftProvider.provider_key,
        display_name: draft.displayName.trim(),
        email_address: draft.emailAddress.trim(),
        mailbox: draft.mailbox.trim() || draftProvider.default_mailbox,
      });
      window.location.assign(result.authorization_url);
    } catch (error) {
      notify("error", humanizeError(error));
      setAuthorizing(false);
    }
  };

  const reauthorizeMailbox = async (config: MailboxConfig) => {
    if (
      config.authentication_mode !== "oauth2"
      || config.archived_at
      || reauthorizingMailboxId === config.mailbox_id
    ) return;

    setReauthorizingMailboxId(config.mailbox_id);
    try {
      const result = await api.reauthorizeMailboxOAuth(config.mailbox_id);
      window.location.assign(result.authorization_url);
    } catch (error) {
      notify("error", humanizeError(error));
      setReauthorizingMailboxId(null);
    }
  };

  const syncMailbox = async (config: MailboxConfig) => {
    if (
      !mailboxCanSync(config)
      || enqueuingMailboxId === config.mailbox_id
      || activeSyncMailboxIds.has(config.mailbox_id)
    ) return;
    setEnqueuingMailboxId(config.mailbox_id);
    try {
      const job = await api.syncMailbox(config.mailbox_id);
      manualMailboxJobIdsRef.current.add(job.job_id);
      upsertMailboxJobs([job]);
      notify("success", job.deduplicated ? "该收件通道已有后台同步任务。" : "已加入后台同步队列。");
    } catch (error) {
      notify("error", humanizeError(error));
    } finally {
      setEnqueuingMailboxId(null);
    }
  };

  const syncAllMailboxes = async () => {
    if (enqueuingAll || !mailboxes.some(mailboxCanSync)) return;
    setEnqueuingAll(true);
    try {
      const result = await api.syncAllMailboxes();
      for (const job of result.items) manualMailboxJobIdsRef.current.add(job.job_id);
      upsertMailboxJobs(result.items);
      notify(
        "success",
        result.queued_count
          ? `${result.queued_count} 个收件通道已加入后台同步队列。`
          : "所有可用收件通道都已有后台同步任务。",
      );
    } catch (error) {
      notify("error", humanizeError(error));
    } finally {
      setEnqueuingAll(false);
    }
  };

  const archiveMailbox = async () => {
    if (!selectedConfig || archiving) return;
    if (!window.confirm(`归档“${selectedConfig.display_name}”？它将停止接收新附件，已有入库记录会保留。`)) return;
    setArchiving(true);
    try {
      const archived = await api.archiveMailbox(selectedConfig.mailbox_id);
      setMailboxes((current) => current.map((item) => (
        item.mailbox_id === archived.mailbox_id ? archived : item
      )));
      selectMailbox(archived, true);
      notify("success", "收件通道已归档，历史入库、内容保留与清理记录仍可查看。");
    } catch (error) {
      notify("error", humanizeError(error));
    } finally {
      setArchiving(false);
    }
  };

  const retryImport = async (item: MailboxImportHistoryItem) => {
    if (
      !item.can_retry
      || enqueuingRetryImportId === item.import_id
      || activeRetryImportIds.has(item.import_id)
    ) return;
    setEnqueuingRetryImportId(item.import_id);
    try {
      const job = await api.retryMailboxImport(item.import_id);
      manualMailboxJobIdsRef.current.add(job.job_id);
      upsertMailboxJobs([job]);
      notify("success", job.deduplicated ? "该附件已有后台重试任务。" : "已加入后台重新入库队列。");
    } catch (error) {
      notify("error", humanizeError(error));
    } finally {
      setEnqueuingRetryImportId(null);
    }
  };

  const saveRetentionPolicy = async () => {
    if (!selectedConfig?.configured) {
      notify("error", "请先保存这个收件通道，再设置内容保留策略。");
      return;
    }
    if (!canManageRetention) return;

    setRetentionSaving(true);
    try {
      const saved = await api.saveMailboxRetention(selectedConfig.mailbox_id, {
        retention_policy: retentionPolicy,
      });
      setRetention(saved);
      setRetentionPolicy(saved.retention_policy);
      setRetentionPreview(null);
      notify("success", "内容保留策略已保存，将在后续清理任务中生效。");
    } catch (error) {
      notify("error", humanizeError(error));
    } finally {
      setRetentionSaving(false);
    }
  };

  const previewRetentionCleanup = async () => {
    if (!selectedConfig?.configured || !canManageRetention) return;
    if (retentionPolicyChanged) {
      notify("error", "请先保存新的保留策略，再预览清理范围。");
      return;
    }

    setPreviewingRetention(true);
    try {
      const preview = await api.previewMailboxRetention(selectedConfig.mailbox_id);
      setRetentionPreview(preview);
    } catch (error) {
      notify("error", humanizeError(error));
    } finally {
      setPreviewingRetention(false);
    }
  };

  const startRetentionCleanup = async () => {
    if (!selectedConfig?.configured || !canManageRetention || !retentionPreview) return;
    if (mailboxRetentionDueCount(retentionPreview) <= 0) return;

    setCleaningRetention(true);
    try {
      const run = await api.cleanupMailboxRetention(selectedConfig.mailbox_id);
      setRetentionPreview(null);
      setRetentionRuns((current) => ({
        items: [
          run,
          ...(current?.items.filter((item) => item.run_id !== run.run_id) ?? []),
        ],
        total: Math.max(current?.total ?? 0, (current?.items.length ?? 0) + 1),
      }));
      notify(
        "success",
        run.status === "completed" ? "已完成该通道的过期内容清理。" : "已创建清理任务，状态会在下方自动更新。",
      );
      void loadRetentionActivity(selectedConfig.mailbox_id).catch(() => undefined);
    } catch (error) {
      notify("error", humanizeError(error));
    } finally {
      setCleaningRetention(false);
    }
  };

  const historySourceOptions = [
    ...mailboxes.map((item) => ({ mailboxId: item.mailbox_id, displayName: item.display_name })),
    ...(history?.items ?? [])
      .filter((item) => !mailboxes.some((mailbox) => mailbox.mailbox_id === item.mailbox_config_id))
      .map((item) => ({
        mailboxId: item.mailbox_config_id,
        displayName: item.mailbox_display_name || "已归档收件通道",
      })),
  ].filter((item, index, entries) => entries.findIndex((candidate) => candidate.mailboxId === item.mailboxId) === index);
  const hasMailboxChannels = mailboxes.length > 0;
  const showMailboxCreation = !loading && isCreating;
  const mailboxCreationTitle = hasMailboxChannels ? "新建收件通道" : "绑定招聘收件邮箱";
  const mailboxCreationKicker = hasMailboxChannels ? "新建通道" : "首次接入";
  const mailboxCreationDescription = hasMailboxChannels
    ? "从空白配置开始。保存时会记录当前邮箱位置，历史邮件不会入库。"
    : "保存时会记录当前邮箱位置。只有此刻之后到达的附件会进入简历库，历史邮件不会入库。";
  const showMailboxOverview = Boolean(selectedConfig && !isCreating && !isEditingConnection);
  const formUsesOAuth = isCreating
    ? draftProvider?.authentication_mode === "oauth2"
    : selectedConfig?.authentication_mode === "oauth2";
  const formProviderName = isCreating
    ? draftProvider?.display_name ?? "尚未选择"
    : selectedConfig ? mailboxProviderDisplayName(selectedConfig) : "已配置 IMAP 邮箱";
  const formCredentialLabel = draftProvider?.credential_label
    ?? (selectedConfig?.authentication_mode === "app_password" ? "邮箱授权码" : "邮箱授权");

  const mailboxConnectionFields = (
    <div className="mailbox-connection-form">
      <section className="mailbox-form-section" aria-labelledby="mailbox-provider-heading">
        <div className="mailbox-form-section-heading">
          <div>
            <h3 id="mailbox-provider-heading">邮箱服务商</h3>
            <p>系统固定使用经过审核的加密连接，不开放自定义服务器地址或端口。</p>
          </div>
        </div>
        {isCreating ? (
          <>
            <MailboxProviderPicker
              disabled={saving || authorizing}
              loading={providersLoading}
              onChange={selectProvider}
              providers={providers}
              value={draft.providerKey}
            />
            {draftProvider && (
              <p className={`mailbox-provider-help${draftProvider.available ? "" : " is-warning"}`} role={draftProvider.available ? undefined : "alert"}>
                <Icon name={draftProvider.available ? "check" : "activity"} size={15} />
                <span>{draftProvider.available ? draftProvider.help_text : "该服务商尚未在当前部署启用，请联系部署管理员完成配置。"}</span>
              </p>
            )}
          </>
        ) : (
          <div className="mailbox-provider-locked">
            <div>
              <strong>{formProviderName}</strong>
              <span>{mailboxAuthenticationModeLabel(selectedConfig?.authentication_mode ?? null)}</span>
            </div>
            <span className={`status-pill${selectedConfig?.authorization_status === "connected" ? " is-success" : selectedConfig?.authorization_status === "reauthorization_required" ? " is-error" : " is-warning"}`}>
              {selectedConfig?.authorization_status === "connected"
                ? "已连接"
                : selectedConfig?.authorization_status === "reauthorization_required"
                  ? "需重新授权"
                  : selectedConfig?.authorization_status === "unavailable"
                    ? "服务未启用"
                    : "待连接"}
            </span>
          </div>
        )}
      </section>

      <section className="mailbox-form-section" aria-labelledby="mailbox-identity-heading">
        <div className="mailbox-form-section-heading">
          <div>
            <h3 id="mailbox-identity-heading">收件身份</h3>
            <p>用于区分简历来源；不会向候选人发送邮件。</p>
          </div>
        </div>
        <div className="form-grid mailbox-form-grid">
          <div className="field-stack">
            <label className="field-label" htmlFor="mailbox-display-name">通道名称</label>
            <BackofficeInput
              disabled={selectedMailboxArchived || selectedSyncInProgress}
              id="mailbox-display-name"
              maxLength={32}
              onChange={(value) => updateDraft("displayName", value)}
              placeholder="例如：招聘邮箱"
              value={draft.displayName}
            />
          </div>
          <div className="field-stack">
            <label className="field-label" htmlFor="imap-address">接收简历的邮箱</label>
            <BackofficeInput
              autoComplete="email"
              disabled={!isCreating || selectedMailboxArchived || selectedSyncInProgress || authorizing}
              id="imap-address"
              onChange={(value) => updateDraft("emailAddress", value)}
              type="email"
              value={draft.emailAddress}
            />
            {!isCreating && <p className="field-help">接收邮箱与服务商属于这个通道的来源身份。需要换邮箱时，请新建收件通道。</p>}
          </div>
        </div>
      </section>

      <section className="mailbox-form-section" aria-labelledby="mailbox-connection-heading">
        <div className="mailbox-form-section-heading">
          <div>
            <h3 id="mailbox-connection-heading">连接与同步</h3>
            <p>{formUsesOAuth ? "授权将在服务商页面完成，系统不会收集网页登录密码。" : "请使用服务商生成的专用授权码或客户端密码。"}</p>
          </div>
        </div>
        <div className="form-grid mailbox-form-grid">
          <div className="field-stack">
            <label className="field-label" htmlFor="imap-folder">邮箱文件夹</label>
            <BackofficeInput
              disabled={!isCreating || selectedMailboxArchived || selectedSyncInProgress || authorizing}
              id="imap-folder"
              onChange={(value) => updateDraft("mailbox", value)}
              value={draft.mailbox}
            />
            {!isCreating && <p className="field-help">文件夹会保持当前来源位置，不能直接改写。</p>}
          </div>
          {isCreating && !draftProvider ? (
            <p className="mailbox-connection-pending span-full">先选择一个可用的邮箱服务商，再填写连接所需信息。</p>
          ) : formUsesOAuth ? (
            <div className="mailbox-oauth-explainer span-full">
              <span className="mailbox-oauth-explainer-icon"><Icon name="arrow-right" size={16} /></span>
              <div>
                <strong>{formProviderName} 网页授权</strong>
                <p>点击授权后会前往服务商登录页，完成后自动回到这里。系统只保存服务端加密的授权凭据。</p>
              </div>
            </div>
          ) : (
            <div className="field-stack">
              <label className="field-label" htmlFor="imap-password">{formCredentialLabel}</label>
              <BackofficeInput
                aria-describedby="imap-password-hint"
                autoComplete="new-password"
                disabled={selectedMailboxArchived || selectedSyncInProgress || authorizing || (isCreating && !draftProvider?.available)}
                id="imap-password"
                onChange={(value) => updateDraft("password", value)}
                placeholder={isCreating ? "首次连接必填" : "留空则保持原授权码"}
                type="password"
                value={draft.password}
              />
              <p className="field-help" id="imap-password-hint">{isCreating ? "只用于连接当前通道，不会在页面中回显。" : "更新后只替换服务端加密保存的授权码，不改变收件起点。"}</p>
            </div>
          )}
          <label className="choice-row span-full mailbox-sync-toggle">
            <input checked={draft.enabled} disabled={selectedMailboxArchived || selectedSyncInProgress || authorizing} onChange={(event) => updateDraft("enabled", event.target.checked)} type="checkbox" />
            <span>
              <strong>启用后台定时同步</strong>
              <small>你也可以在保存后随时手动同步这个通道。</small>
            </span>
          </label>
        </div>
      </section>
    </div>
  );

  const mailboxFormActions = (
    <div className="review-actions mailbox-form-actions">
      {isCreating && hasMailboxChannels && (
        <BackofficeButton
          disabled={saving || archiving}
          icon={<Icon name="arrow-left" size={16} />}
          onClick={() => {
            const fallback = mailboxes.find((item) => !item.archived_at) ?? mailboxes[0];
            if (fallback) selectMailbox(fallback);
          }}
        >
          取消新建
        </BackofficeButton>
      )}
      {!isCreating && selectedConfig && isEditingConnection && (
        <BackofficeButton
          disabled={saving || archiving || selectedSyncInProgress}
          icon={<Icon name="arrow-left" size={16} />}
          onClick={() => selectMailbox(selectedConfig)}
        >
          返回概览
        </BackofficeButton>
      )}
      {!isCreating && selectedConfig && (
        selectedMailboxRequiresAuthorization ? (
          <BackofficeButton
            disabled={archiving || saving || selectedMailboxArchived}
            icon={<Icon name="arrow-right" size={16} />}
            loading={reauthorizingMailboxId === selectedConfig.mailbox_id}
            onClick={() => void reauthorizeMailbox(selectedConfig)}
            tone="primary"
          >
            {reauthorizingMailboxId === selectedConfig.mailbox_id ? "正在前往授权" : "重新授权"}
          </BackofficeButton>
        ) : (
          <BackofficeButton
            disabled={archiving || saving || selectedSyncInProgress || !selectedMailboxCanSync}
            icon={selectedSyncJob ? <i className="spinner" /> : <Icon name="refresh" size={16} />}
            loading={enqueuingMailboxId === selectedConfig.mailbox_id}
            onClick={() => void syncMailbox(selectedConfig)}
          >
            {enqueuingMailboxId === selectedConfig.mailbox_id ? "正在加入队列" : selectedSyncJob ? "后台同步中" : "同步此通道"}
          </BackofficeButton>
        )
      )}
      {!isCreating && selectedConfig && !selectedConfig.archived_at && (
        <BackofficeButton
          disabled={archiving || saving || selectedSyncInProgress}
          loading={archiving}
          onClick={() => void archiveMailbox()}
          tone="danger"
        >
          {archiving ? "正在归档" : "归档通道"}
        </BackofficeButton>
      )}
      {isCreating && formUsesOAuth ? (
        <BackofficeButton
          disabled={loading || saving || archiving || authorizing || !draftProvider?.available}
          icon={authorizing ? undefined : <Icon name="arrow-right" size={16} />}
          loading={authorizing}
          onClick={() => void startMailboxOAuth()}
          tone="primary"
        >
          {authorizing ? "正在前往授权" : `前往 ${draftProvider?.display_name ?? "服务商"} 授权`}
        </BackofficeButton>
      ) : (
        <BackofficeButton
          disabled={loading || saving || archiving || selectedSyncInProgress || authorizing || (isCreating && (!draftProvider || !draftProvider.available)) || (!isCreating && selectedMailboxArchived)}
          icon={saving ? undefined : <Icon name="check" size={16} />}
          loading={saving}
          onClick={() => void saveMailbox()}
          tone={!isCreating && selectedMailboxRequiresAuthorization ? "default" : "primary"}
        >
          {saving ? "正在保存" : isCreating ? "创建并开始接收" : selectedMailboxArchived ? "已归档" : "保存通道"}
        </BackofficeButton>
      )}
    </div>
  );

  const mailboxOperationalOverview = selectedConfig && (
    <section className="panel mailbox-operation-overview" aria-label={`${selectedConfig.display_name} 收件概览`}>
      <div className="mailbox-operation-heading">
        <div className="mailbox-operation-identity">
          <span className="mailbox-operation-icon"><Icon name="inbox" size={20} /></span>
          <div>
            <div className="mailbox-operation-title-row">
              <h2>{selectedConfig.display_name}</h2>
              <span className={`status-pill${mailboxChannelStatusClass(selectedConfig)}`}>{mailboxChannelStatus(selectedConfig)}</span>
            </div>
            <p>{mailboxProviderDisplayName(selectedConfig)} · {selectedConfig.email_address || "尚未配置接收邮箱"} · {selectedConfig.mailbox || "INBOX"}</p>
          </div>
        </div>
        <div className="mailbox-operation-actions">
          {selectedMailboxRequiresAuthorization ? (
            <BackofficeButton
              disabled={Boolean(selectedConfig.archived_at)}
              icon={<Icon name="arrow-right" size={16} />}
              loading={reauthorizingMailboxId === selectedConfig.mailbox_id}
              onClick={() => void reauthorizeMailbox(selectedConfig)}
              tone="primary"
            >
              {reauthorizingMailboxId === selectedConfig.mailbox_id ? "正在前往授权" : "重新授权"}
            </BackofficeButton>
          ) : (
            <BackofficeButton
              disabled={!selectedMailboxCanSync || selectedSyncInProgress}
              icon={selectedSyncInProgress ? undefined : <Icon name="refresh" size={16} />}
              loading={selectedSyncInProgress}
              onClick={() => void syncMailbox(selectedConfig)}
              tone="primary"
            >
              {selectedSyncInProgress ? "后台同步中" : "同步此通道"}
            </BackofficeButton>
          )}
          <BackofficeButton
            disabled={Boolean(selectedConfig.archived_at) || selectedSyncInProgress}
            icon={<Icon name="gear" size={16} />}
            onClick={() => setIsEditingConnection(true)}
          >
            编辑连接
          </BackofficeButton>
          {!selectedConfig.archived_at && (
            <BackofficeButton
              disabled={archiving || selectedSyncInProgress}
              loading={archiving}
              onClick={() => void archiveMailbox()}
              tone="danger"
            >
              {archiving ? "正在归档" : "归档通道"}
            </BackofficeButton>
          )}
        </div>
      </div>

      <div className="mailbox-operation-facts">
        <div>
          <span>连接方式</span>
          <strong>{mailboxAuthenticationModeLabel(selectedConfig.authentication_mode)}</strong>
        </div>
        <div>
          <span>开始接收</span>
          <strong>{selectedConfig.import_started_at ? formatLibraryDate(selectedConfig.import_started_at) : "正在初始化"}</strong>
        </div>
        <div>
          <span>最近同步</span>
          <strong>{selectedConfig.last_synced_at ? formatLibraryDate(selectedConfig.last_synced_at) : "尚未同步"}</strong>
        </div>
        <div>
          <span>后台同步</span>
          <strong>{selectedSyncJob ? mailboxBackgroundJobStatusLabel(selectedSyncJob) : selectedMailboxCanSync ? "已启用" : selectedConfig.enabled ? "等待授权" : "已暂停"}</strong>
        </div>
      </div>

      {selectedConfig.authentication_mode === "oauth2" && selectedConfig.authorization_status === "reauthorization_required" && (
        <div className="mailbox-operation-alert" role="alert">
          <Icon name="activity" size={16} />
          <span>邮箱授权已失效。重新授权后会恢复原通道的同步，历史入库记录与收件起点不会改变。</span>
        </div>
      )}
      {selectedConfig.authorization_status === "unavailable" && (
        <div className="mailbox-operation-alert is-warning" role="alert">
          <Icon name="activity" size={16} />
          <span>该服务商当前未在部署环境启用。系统不会尝试同步，请联系部署管理员完成配置。</span>
        </div>
      )}
      {selectedConfig.active_sync_alert && (
        <div className="mailbox-operation-alert" role="alert">
          <Icon name="activity" size={16} />
          <span>{mailboxSyncAlertTitle(selectedConfig)}，连续失败 {selectedConfig.active_sync_alert.consecutive_failures} 次。{mailboxImportErrorLabel(selectedConfig.active_sync_alert.last_error_code)}</span>
        </div>
      )}
    </section>
  );

  return (
    <div className={pageClassName}>
      <header className="page-heading">
        <div>
          {embedded ? <h2>收件邮箱</h2> : <h1>邮箱附件入库</h1>}
          <p>连接招聘邮箱后，系统只接收绑定之后到达的附件。</p>
        </div>
        {hasMailboxChannels && !isCreating && (
          <div className="mailbox-heading-actions">
            <BackofficeButton
              disabled={loading || saving || enqueuingAll}
              icon={<Icon name="plus" size={16} />}
              onClick={() => void startCreatingMailbox()}
            >
              新建收件通道
            </BackofficeButton>
            <BackofficeButton
              disabled={loading || saving || enqueuingAll || !mailboxes.some(mailboxCanSync)}
              icon={activeSyncMailboxIds.size ? <i className="spinner" /> : <Icon name="refresh" size={16} />}
              loading={enqueuingAll}
              onClick={() => void syncAllMailboxes()}
              tone="primary"
            >
              {enqueuingAll ? "正在加入队列" : activeSyncMailboxIds.size ? "后台同步中" : "同步全部"}
            </BackofficeButton>
          </div>
        )}
      </header>

      {!isCreating && activeSyncAlerts.length > 0 && (
        <section aria-label="需要处理的邮箱同步异常" className="mailbox-sync-alert-list" role="alert">
          <div className="mailbox-sync-alert-list-heading">
            <div>
              <h2>需要处理的同步异常</h2>
              <p>这些通道的后台同步已连续失败。请检查连接配置后重新同步，成功后提示会自动恢复。</p>
            </div>
            <span className="status-pill is-error">{activeSyncAlerts.length} 个通道需处理</span>
          </div>
          <div className="mailbox-sync-alert-items">
            {activeSyncAlerts.map((config) => {
              const alert = config.active_sync_alert!;
              const canSync = mailboxCanSync(config)
                && enqueuingMailboxId !== config.mailbox_id
                && !activeSyncMailboxIds.has(config.mailbox_id);
              return (
              <div className="mailbox-sync-alert-item" key={config.mailbox_id}>
                <div>
                  <strong>{config.display_name}</strong>
                    <span>{config.authorization_status === "reauthorization_required" ? "邮箱授权已失效，重新授权后会恢复同步。" : `${mailboxSyncAlertTitle(config)}，连续失败的后台同步任务 ${alert.consecutive_failures} 次，最近一次 ${formatLibraryDate(alert.last_failed_at)}。`}</span>
                    <small>{mailboxImportErrorLabel(alert.last_error_code)}</small>
                  </div>
                  {mailboxRequiresAuthorization(config) ? (
                    <BackofficeButton
                      disabled={Boolean(config.archived_at)}
                      icon={<Icon name="arrow-right" size={16} />}
                      loading={reauthorizingMailboxId === config.mailbox_id}
                      onClick={() => void reauthorizeMailbox(config)}
                      tone="primary"
                    >
                      {reauthorizingMailboxId === config.mailbox_id ? "正在前往授权" : "重新授权"}
                    </BackofficeButton>
                  ) : (
                    <BackofficeButton
                      disabled={!canSync}
                      icon={activeSyncMailboxIds.has(config.mailbox_id) ? undefined : <Icon name="refresh" size={16} />}
                      loading={enqueuingMailboxId === config.mailbox_id || activeSyncMailboxIds.has(config.mailbox_id)}
                      onClick={() => void syncMailbox(config)}
                      tone="danger"
                    >
                      {enqueuingMailboxId === config.mailbox_id
                        ? "正在加入队列"
                        : activeSyncMailboxIds.has(config.mailbox_id)
                          ? "后台同步中"
                          : "同步此通道"}
                    </BackofficeButton>
                  )}
                </div>
              );
            })}
          </div>
        </section>
      )}

      {showMailboxCreation ? (
        <section className="mailbox-setup-shell" aria-label={mailboxCreationTitle}>
          <section className="panel mailbox-setup-form-panel">
            <div className="mailbox-setup-heading">
              <span className="mailbox-setup-kicker"><Icon name="inbox" size={16} />{mailboxCreationKicker}</span>
              <h2>{mailboxCreationTitle}</h2>
              <p>{mailboxCreationDescription}</p>
            </div>
            {mailboxConnectionFields}
            {mailboxFormActions}
          </section>

          <aside className="panel mailbox-setup-aside">
            <div className="mailbox-setup-aside-heading">
              <h2>接入后如何工作</h2>
              <p>{hasMailboxChannels ? "先完成这个新通道的连接，已有通道和处理记录不会带入。" : "连接配置、同步状态和处理记录都只属于当前工作区。"}</p>
            </div>
            <ol className="mailbox-setup-steps">
              <li>
                <span>1</span>
                <div><strong>保存连接</strong><p>系统记录当前收件位置，不回扫已有邮件。</p></div>
              </li>
              <li>
                <span>2</span>
                <div><strong>后台同步</strong><p>按计划检查新附件，也可以随时手动触发。</p></div>
              </li>
              <li>
                <span>3</span>
                <div><strong>附件入库</strong><p>支持 PDF、Word、图片、Excel 和 HTML，处理结果会留在本页。</p></div>
              </li>
            </ol>
            <p className="mailbox-setup-footnote"><Icon name="check" size={15} />{hasMailboxChannels ? "创建完成后，再查看这个通道的同步、入库和保留信息。" : "连接完成后，可在这里查看入库记录、同步异常和内容保留策略。"}</p>
          </aside>
        </section>
      ) : (
      <div className="mailbox-workspace">
        <MailboxChannelList
          isCreating={isCreating}
          loading={loading}
          mailboxes={mailboxes}
          onCreate={() => void startCreatingMailbox()}
          onSelect={selectMailbox}
          selectedMailboxId={selectedMailboxId}
        />

        <div className={`mailbox-detail${showMailboxOverview ? " is-overview" : ""}`}>
          {showMailboxOverview ? mailboxOperationalOverview : (
          <div className="mailbox-detail-grid">
            <section className="panel mailbox-config-panel">
              <div className="panel-heading">
                <div>
                  <h2>{isCreating ? "新建收件通道" : "收件通道设置"}</h2>
                  <p>{isCreating ? "保存时会记录当前邮箱位置，历史邮件不会入库。" : "授权码始终保持隐藏；留空则继续使用已保存的值。"}</p>
                </div>
                {selectedConfig && <span className={`status-pill${mailboxChannelStatusClass(selectedConfig)}`}>{mailboxChannelStatus(selectedConfig)}</span>}
              </div>
              {loading ? <TableSkeleton /> : mailboxConnectionFields}
              {mailboxFormActions}
            </section>

            <aside className="panel mailbox-status-panel">
              <div className="panel-heading"><div><h2>运行状态</h2><p>同步、异常和内容保留都按当前通道独立管理；相同内容附件不会重复创建候选人。</p></div></div>
              {selectedConfig ? (
                <>
                  {selectedConfig.active_sync_alert && (
                    <section className="mailbox-sync-alert-detail" role="alert">
                      <div>
                        <strong>{mailboxSyncAlertTitle(selectedConfig)}</strong>
                        <span>连续失败的后台同步任务 {selectedConfig.active_sync_alert.consecutive_failures} 次，最近一次 {formatLibraryDate(selectedConfig.active_sync_alert.last_failed_at)}。</span>
                        <small>{mailboxImportErrorLabel(selectedConfig.active_sync_alert.last_error_code)}</small>
                      </div>
                      <BackofficeButton
                        disabled={!selectedConfig.enabled || Boolean(selectedConfig.archived_at) || selectedSyncInProgress}
                        icon={selectedSyncInProgress ? undefined : <Icon name="refresh" size={16} />}
                        loading={selectedSyncInProgress}
                        onClick={() => void syncMailbox(selectedConfig)}
                        tone="danger"
                      >
                        {selectedSyncInProgress ? "后台同步中" : "立即同步"}
                      </BackofficeButton>
                    </section>
                  )}
                <div className="fact-list">
                  <div className="fact-row"><strong>开始接收</strong><span>{selectedConfig.import_started_at ? formatLibraryDate(selectedConfig.import_started_at) : "正在初始化"}</span></div>
                  <div className="fact-row"><strong>最近同步</strong><span>{selectedConfig.last_synced_at ? formatLibraryDate(selectedConfig.last_synced_at) : "尚未同步"}</span></div>
                  {selectedSyncJob && <div className="fact-row"><strong>后台任务</strong><span className={`status-pill ${mailboxBackgroundJobStatusClass(selectedSyncJob)}`}>{mailboxBackgroundJobStatusLabel(selectedSyncJob)}</span></div>}
                  <div className="fact-row"><strong>附件处理记录</strong><span>{historyFilterMailboxId === selectedConfig.mailbox_id ? `${history?.total ?? 0} 条` : "可在下方按来源筛选"}</span></div>
                  <div className="fact-row"><strong>支持格式</strong><span>PDF、Word、图片、Excel、HTML</span></div>
                  {retention && <>
                    <div className="fact-row"><strong>当前保留</strong><span>{mailboxRetentionPolicyLabel(retention.retention_policy)}</span></div>
                    <div className="fact-row"><strong>缓存内容</strong><span>{retention.body_copy_count} 正文 · {retention.attachment_copy_count + retention.failure_artifact_count} 附件副本</span></div>
                    <div className="fact-row"><strong>缓存占用</strong><span>{formatFileSize(retention.cache_bytes)}</span></div>
                    <div className="fact-row"><strong>最早到期</strong><span>{retention.earliest_expires_at ? formatLibraryDate(retention.earliest_expires_at) : "暂无待清理内容"}</span></div>
                    <div className="fact-row"><strong>最近清理</strong><span>{retention.last_cleanup_at ? formatLibraryDate(retention.last_cleanup_at) : "尚未执行"}</span></div>
                    <div className="fact-row"><strong>下次清理</strong><span>{retention.next_cleanup_at ? formatLibraryDate(retention.next_cleanup_at) : "由系统定时安排"}</span></div>
                  </>}
                  {selectedConfig.last_sync_error && <div className="fact-row"><strong>最近异常</strong><span>{mailboxImportErrorLabel(selectedConfig.last_sync_error)}</span></div>}
                </div>
                </>
              ) : (
                <div className="mailbox-status-empty"><Icon name="history" size={19} /><span>保存后会显示这个通道的接收起点、最近同步时间和异常状态。</span></div>
              )}
            </aside>
          </div>
          )}

          <details className="panel mailbox-retention-panel">
            <summary className="panel-heading mailbox-disclosure-heading">
              <div>
                <h2>内容保留</h2>
                <p>只清理当前通道的系统邮件正文与附件副本，不会删除源邮件或候选人原始简历。</p>
              </div>
              {retention && <span className="status-pill">{mailboxRetentionPolicyLabel(retention.retention_policy)}</span>}
            </summary>
            {loading ? <TableSkeleton /> : !selectedConfig?.configured ? (
              <div className="mailbox-retention-empty">
                <strong>先保存收件通道</strong>
                <span>保存连接配置后，可为这个通道设置正文和附件副本的保留周期。</span>
              </div>
            ) : (
              <>
                {selectedMailboxArchived && <p className="mailbox-retention-notice">该通道已归档，不会接收新附件；已有内容仍按以下策略清理。</p>}
                <fieldset className="mailbox-retention-policy" disabled={!canManageRetention || retentionSaving}>
                  <legend className="field-label">内容保留档位</legend>
                  <div className="mailbox-retention-policy-options">
                    {mailboxRetentionPolicies.map((option) => (
                      <label className="choice-row mailbox-retention-option" key={option.value}>
                        <input
                          checked={retentionPolicy === option.value}
                          name="mailbox-retention-policy"
                          onChange={() => {
                            setRetentionPolicy(option.value);
                            setRetentionPreview(null);
                          }}
                          type="radio"
                        />
                        <span>
                          <strong>{option.label}</strong>
                          <small>{option.description}</small>
                        </span>
                      </label>
                    ))}
                  </div>
                </fieldset>
                {!canManageRetention && <p className="field-help">仅工作区管理员可以修改保留策略或执行清理。当前策略与清理统计仍可查看。</p>}
                <p className="mailbox-retention-notice">已删除的系统副本不可恢复。简历库中的候选人原始简历、AI 结论与邮箱服务商中的源邮件不受影响。</p>
                {canManageRetention && (
                  <div className="review-actions mailbox-retention-actions">
                    <BackofficeButton
                      disabled={retentionSaving || !retention || !retentionPolicyChanged}
                      icon={retentionSaving ? undefined : <Icon name="check" size={16} />}
                      loading={retentionSaving}
                      onClick={() => void saveRetentionPolicy()}
                      tone="primary"
                    >
                      {retentionSaving ? "正在保存" : "保存保留策略"}
                    </BackofficeButton>
                    <BackofficeButton
                      disabled={!retention || previewingRetention || retentionSaving || retentionPolicyChanged || retentionHasActiveRun}
                      icon={previewingRetention ? undefined : <Icon name="history" size={16} />}
                      loading={previewingRetention}
                      onClick={() => void previewRetentionCleanup()}
                    >
                      {previewingRetention ? "正在预览" : "预览已到期内容"}
                    </BackofficeButton>
                  </div>
                )}
                {retentionPreview && (
                  <section aria-live="polite" className="mailbox-retention-preview">
                    <div className="mailbox-retention-preview-heading">
                      <div>
                        <h3>已到期内容预览</h3>
                        <p>以下系统副本将不可恢复地删除，不包含邮箱源邮件或候选人原始简历。</p>
                      </div>
                      <span className={`status-pill${mailboxRetentionDueCount(retentionPreview) ? " is-warning" : " is-success"}`}>
                        {mailboxRetentionDueCount(retentionPreview) ? `${mailboxRetentionDueCount(retentionPreview)} 项待清理` : "暂无待清理内容"}
                      </span>
                    </div>
                    <div className="mailbox-retention-preview-stats">
                      <div><strong>正文副本</strong><span>{retentionPreview.expired_body_count} 项</span></div>
                      <div><strong>成功与失败附件副本</strong><span>{retentionPreview.expired_attachment_copy_count + retentionPreview.expired_failure_artifact_count} 项</span></div>
                      <div><strong>预计释放</strong><span>{formatFileSize(retentionPreview.expired_bytes)}</span></div>
                      <div><strong>暂不清理</strong><span>{retentionPreview.skipped_count} 项</span></div>
                    </div>
                    {canManageRetention && mailboxRetentionDueCount(retentionPreview) > 0 && (
                      <div className="review-actions mailbox-retention-confirm-actions">
                        <BackofficeButton
                          disabled={cleaningRetention || retentionHasActiveRun}
                          loading={cleaningRetention}
                          onClick={() => void startRetentionCleanup()}
                          tone="danger"
                        >
                          {cleaningRetention ? "正在创建清理任务" : "确认清理已到期内容"}
                        </BackofficeButton>
                      </div>
                    )}
                  </section>
                )}
              </>
            )}
          </details>

          <section className="panel mailbox-history">
            <div className="panel-heading mailbox-history-heading">
              <div><h2>附件入库记录</h2><p>每封新邮件保留一条附件处理记录；相同内容只关联既有入库结果，不展示邮件正文或候选人信息。</p></div>
              <div className="mailbox-history-filter">
                <label className="field-label" htmlFor="mailbox-history-filter" id="mailbox-history-filter-label">来源</label>
                <BackofficeSelect
                  ariaLabelledBy="mailbox-history-filter-label"
                  id="mailbox-history-filter"
                  onChange={(mailboxId) => {
                    const nextMailboxId = mailboxId || null;
                    setHistoryFilterMailboxId(nextMailboxId);
                    void loadHistory(nextMailboxId);
                  }}
                  options={[
                    { label: "全部收件通道", value: "" },
                    ...historySourceOptions.map((item) => ({
                      label: item.displayName,
                      value: item.mailboxId,
                    })),
                  ]}
                  value={historyFilterMailboxId ?? ""}
                />
              </div>
            </div>
            <span aria-live="polite" className="sr-only">{activeRetryImportIds.size ? "附件正在后台重新入库。" : activeSyncMailboxIds.size ? "收件通道正在后台同步。" : ""}</span>
            {historyLoading ? <TableSkeleton /> : history?.items.length ? (
              <div className="table-scroll">
                <table className="candidate-table mailbox-history-table">
                  <thead>
                    <tr>
                      <th scope="col">附件</th>
                      <th scope="col">来源</th>
                      <th scope="col">结果与原因</th>
                      <th scope="col">尝试</th>
                      <th scope="col">最后处理</th>
                      <th scope="col">操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {history.items.map((item) => {
                      const isRetrying = (item.status === "retrying" && !item.can_retry)
                        || activeRetryImportIds.has(item.import_id)
                        || enqueuingRetryImportId === item.import_id;
                      const statusClass = item.status === "imported" || item.status === "duplicate"
                        ? "is-success"
                        : item.status === "failed"
                          ? "is-error"
                          : item.status === "retrying" && item.can_retry
                            ? "is-warning"
                            : item.status === "retrying" || item.status === "deduplicating" || item.status === "processing"
                              ? "is-progress"
                              : "";
                      return (
                        <tr key={item.import_id}>
                          <th scope="row"><strong>{item.attachment_filename}</strong></th>
                          <td className="mailbox-source-cell">{item.mailbox_display_name || "已归档收件通道"}</td>
                          <td>
                            <span className={`status-pill mailbox-import-status ${statusClass}`}>{mailboxImportStatusLabel(item.status, item.can_retry)}</span>
                            {item.error && <small className="mailbox-import-error">{mailboxImportErrorLabel(item.error)}</small>}
                          </td>
                          <td className="mailbox-attempt-cell">{item.attempt_count} 次</td>
                          <td>{formatLibraryDate(item.last_attempted_at ?? item.created_at)}</td>
                          <td className="mailbox-action-cell">
                            {isRetrying ? (
                              <span className="mailbox-retry-pending"><i className="spinner" />正在重试</span>
                            ) : item.can_retry ? (
                              <BackofficeButton
                                ariaLabel={`重新入库：${item.attachment_filename}`}
                                className="upload-row-button mailbox-retry-button"
                                disabled={activeRetryImportIds.has(item.import_id) || enqueuingRetryImportId === item.import_id}
                                icon={<Icon name="refresh" size={15} />}
                                onClick={() => void retryImport(item)}
                              >
                                重新入库
                              </BackofficeButton>
                            ) : <span className="candidate-meta">—</span>}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            ) : <div className="mailbox-history-empty"><span className="empty-glyph"><Icon name="inbox" size={21} /></span><div><h3>还没有附件入库记录</h3><p>绑定后收到的附件会显示在这里，历史邮件不会入库。</p></div></div>}
          </section>

          <details className="panel mailbox-retention-history">
            <summary className="panel-heading mailbox-disclosure-heading">
              <div>
                <h2>清理记录</h2>
                <p>仅保留安全统计与任务状态，不展示邮件正文、邮箱地址或附件内容。</p>
              </div>
              {retentionHasActiveRun && <span className="status-pill is-progress"><i className="spinner" />正在更新</span>}
            </summary>
            <span aria-live="polite" className="sr-only">{retentionHasActiveRun ? "正在更新当前收件通道的内容清理任务状态。" : ""}</span>
            {loading ? <TableSkeleton /> : !selectedConfig?.configured ? (
              <div className="mailbox-retention-empty">
                <strong>尚未配置清理</strong>
                <span>保存收件通道后，系统会按该通道的保留策略自动清理过期副本。</span>
              </div>
            ) : retentionRuns?.items.length ? (
              <div className="table-scroll">
                <table className="candidate-table mailbox-retention-history-table">
                  <thead>
                    <tr>
                      <th scope="col">触发方式</th>
                      <th scope="col">保留策略</th>
                      <th scope="col">状态</th>
                      <th scope="col">扫描 / 清理</th>
                      <th scope="col">释放空间</th>
                      <th scope="col">处理时间</th>
                    </tr>
                  </thead>
                  <tbody>
                    {retentionRuns.items.map((run) => (
                      <tr key={run.run_id}>
                        <th scope="row">{run.trigger_type === "manual" ? "手动" : "定时"}</th>
                        <td>{mailboxRetentionPolicyLabel(run.retention_policy)}</td>
                        <td>
                          <span className={`status-pill ${mailboxRetentionRunStatusClass(run.status)}`}>{mailboxRetentionRunStatusLabel(run.status)}</span>
                          {run.error_code && <small className="mailbox-import-error">{mailboxRetentionRunErrorLabel(run.error_code)}</small>}
                        </td>
                        <td className="mailbox-retention-count-cell">{run.scanned_count} / {run.deleted_count}{run.skipped_count ? `，跳过 ${run.skipped_count}` : ""}{run.failed_count ? `，失败 ${run.failed_count}` : ""}</td>
                        <td className="mailbox-retention-count-cell">{formatFileSize(run.reclaimed_bytes)}</td>
                        <td>{formatLibraryDate(run.finished_at ?? run.started_at ?? "")}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <div className="mailbox-retention-empty">
                <strong>还没有清理记录</strong>
                <span>系统会每日检查当前通道的到期副本；管理员也可先预览后手动执行。</span>
              </div>
            )}
          </details>
        </div>
      </div>
      )}
    </div>
  );
}
