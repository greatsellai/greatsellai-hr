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
  MailboxRetentionRuns,
  MailboxSourceTagRule,
  SourceTag,
  SourceTagRuleMatchKind,
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
  mailboxInitialSyncLookbackLabel,
  mailboxInitialSyncLookbackOptions,
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

interface SourceTagRuleDraft {
  sourceTagId: string;
  matchKind: SourceTagRuleMatchKind;
  matchValue: string;
  priority: string;
}

const sourceTagRuleMatchOptions: Array<{
  value: SourceTagRuleMatchKind;
  label: string;
}> = [
  { value: "sender_domain", label: "发件域名" },
  { value: "sender_address", label: "发件地址" },
  { value: "subject_keyword", label: "主题关键词" },
];

function newSourceTagRuleDraft(sourceTagId = ""): SourceTagRuleDraft {
  return {
    sourceTagId,
    matchKind: "sender_domain",
    matchValue: "",
    priority: "100",
  };
}

function sourceTagRuleMatchLabel(kind: SourceTagRuleMatchKind): string {
  return sourceTagRuleMatchOptions.find((option) => option.value === kind)?.label
    ?? "匹配条件";
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
  const [sourceTags, setSourceTags] = useState<SourceTag[]>([]);
  const [sourceTagRules, setSourceTagRules] = useState<MailboxSourceTagRule[]>([]);
  const [sourceTagRulesLoading, setSourceTagRulesLoading] = useState(false);
  const [sourceTagRulesError, setSourceTagRulesError] = useState<string | null>(null);
  const [sourceTagRuleDraft, setSourceTagRuleDraft] = useState<SourceTagRuleDraft>(() => newSourceTagRuleDraft());
  const [editingSourceTagRuleId, setEditingSourceTagRuleId] = useState<string | null>(null);
  const [savingSourceTagRule, setSavingSourceTagRule] = useState(false);
  const [disablingSourceTagRuleId, setDisablingSourceTagRuleId] = useState<string | null>(null);
  const [creatingSourceTag, setCreatingSourceTag] = useState(false);
  const [newSourceTagName, setNewSourceTagName] = useState("");
  const [savingSourceTag, setSavingSourceTag] = useState(false);
  const retentionRequestRef = useRef(0);
  const historyRequestRef = useRef(0);
  const sourceTagRulesRequestRef = useRef(0);
  const mailboxJobPollInFlightRef = useRef(false);
  const manualMailboxJobIdsRef = useRef(new Set<string>());
  const handledMailboxJobIdsRef = useRef(new Set<string>());
  const handledOauthReturnRef = useRef(false);

  const selectedConfig = selectedMailboxId
    ? mailboxes.find((item) => item.mailbox_id === selectedMailboxId) ?? null
    : null;
  const draftProvider = providers.find((item) => item.provider_key === draft.providerKey) ?? null;
  const draftProviderAllowsCustomEndpoint = draftProvider?.allows_custom_endpoint === true;
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
  const selectedInitialSyncLookbackDays = selectedConfig?.initial_sync_lookback_days ?? 0;
  const selectedInitialImportStatus = selectedInitialSyncLookbackDays === 0
    ? "不导入历史邮件"
    : selectedConfig?.initial_backfill_completed_at
      ? "已完成"
      : !selectedConfig?.enabled
        ? "已暂停"
        : selectedMailboxRequiresAuthorization
          ? "等待授权"
          : selectedSyncJob
            ? "后台导入中"
            : "等待后台导入";
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

  const clearSourceTagRules = useCallback(() => {
    sourceTagRulesRequestRef.current += 1;
    setSourceTagRules([]);
    setSourceTagRulesError(null);
    setSourceTagRulesLoading(false);
    setEditingSourceTagRuleId(null);
    setSourceTagRuleDraft(newSourceTagRuleDraft());
    setCreatingSourceTag(false);
    setNewSourceTagName("");
  }, []);

  const loadSourceTagRules = useCallback(async (mailboxId: string) => {
    const requestId = ++sourceTagRulesRequestRef.current;
    setSourceTagRulesLoading(true);
    setSourceTagRulesError(null);
    try {
      const [nextTags, nextRules] = await Promise.all([
        api.listSourceTags(),
        api.listMailboxSourceTagRules(mailboxId),
      ]);
      if (requestId !== sourceTagRulesRequestRef.current) return;
      setSourceTags(nextTags);
      setSourceTagRules(nextRules);
      setSourceTagRuleDraft((current) => (
        current.sourceTagId || !nextTags.some((tag) => tag.source_tag_id === current.sourceTagId)
          ? newSourceTagRuleDraft(nextTags.find((tag) => tag.enabled)?.source_tag_id ?? "")
          : current
      ));
    } catch (error) {
      if (requestId !== sourceTagRulesRequestRef.current) return;
      setSourceTagRulesError(humanizeError(error));
    } finally {
      if (requestId === sourceTagRulesRequestRef.current) {
        setSourceTagRulesLoading(false);
      }
    }
  }, [humanizeError]);

  const confirmDiscardMailboxDraft = () => {
    if (!mailboxDraftIsDirty(draft, selectedConfig, isCreating)) return true;
    return window.confirm("尚未保存的收件通道设置会丢失，仍要离开吗？");
  };

  const selectMailbox = (config: MailboxConfig, force = false) => {
    if (!force && !confirmDiscardMailboxDraft()) return false;
    clearSourceTagRules();
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
    clearSourceTagRules();
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
      imapHost: "",
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
      const lookbackDays = connectedMailbox?.initial_sync_lookback_days ?? 0;
      const importMessage = lookbackDays > 0
        ? `后台将导入${mailboxInitialSyncLookbackLabel(lookbackDays)}的附件，后续只接收新邮件。`
        : "不导入历史邮件，后续只接收新邮件。";
      notify("success", `已连接 ${providerName}，${importMessage}`);
      if (lookbackDays > 0 && connectedMailbox?.enabled) {
        void api.listMailboxBackgroundJobs().then(setMailboxJobs).catch(() => undefined);
      }
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
    if (!selectedConfig?.configured) {
      clearSourceTagRules();
      return undefined;
    }
    void loadSourceTagRules(selectedConfig.mailbox_id);
    return () => {
      sourceTagRulesRequestRef.current += 1;
    };
  }, [clearSourceTagRules, loadSourceTagRules, selectedConfig?.configured, selectedConfig?.mailbox_id]);

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
    if (isCreating && draftProviderAllowsCustomEndpoint && !draft.imapHost.trim()) {
      notify("error", "请填写 IMAP 服务器域名。");
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
          ...(draftProviderAllowsCustomEndpoint
            ? { imap_host: draft.imapHost.trim(), imap_port: 993 }
            : {}),
          email_address: draft.emailAddress.trim(),
          password: draft.password,
          enabled: draft.enabled,
          initial_sync_lookback_days: draft.initialSyncLookbackDays,
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
      if (isCreating && draft.initialSyncLookbackDays > 0 && draft.enabled) {
        void api.listMailboxBackgroundJobs().then(setMailboxJobs).catch(() => undefined);
      }
      const initialImportMessage = draft.initialSyncLookbackDays > 0
        ? draft.enabled
          ? `将在后台导入${mailboxInitialSyncLookbackLabel(draft.initialSyncLookbackDays)}的附件，后续只接收新邮件。`
          : `已保存${mailboxInitialSyncLookbackLabel(draft.initialSyncLookbackDays)}的范围；启用同步或手动同步后才会导入。`
        : "不导入历史邮件，后续只接收新邮件。";
      notify("success", isCreating ? `收件通道已创建，${initialImportMessage}` : "收件通道已保存。");
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
        initial_sync_lookback_days: draft.initialSyncLookbackDays,
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

  const updateSourceTagRuleDraft = <Key extends keyof SourceTagRuleDraft>(
    key: Key,
    value: SourceTagRuleDraft[Key],
  ) => {
    setSourceTagRuleDraft((current) => ({ ...current, [key]: value }));
  };

  const editSourceTagRule = (rule: MailboxSourceTagRule) => {
    setEditingSourceTagRuleId(rule.rule_id);
    setCreatingSourceTag(false);
    setNewSourceTagName("");
    setSourceTagRuleDraft({
      sourceTagId: rule.source_tag.source_tag_id,
      matchKind: rule.match_kind,
      matchValue: rule.match_value,
      priority: String(rule.priority),
    });
  };

  const cancelSourceTagRuleEdit = () => {
    setEditingSourceTagRuleId(null);
    setSourceTagRuleDraft(
      newSourceTagRuleDraft(sourceTags.find((tag) => tag.enabled)?.source_tag_id ?? ""),
    );
  };

  const createSourceTag = async () => {
    const displayName = newSourceTagName.trim();
    if (!displayName) {
      notify("error", "请填写投递渠道名称。");
      return;
    }
    setSavingSourceTag(true);
    try {
      const created = await api.createSourceTag({ display_name: displayName });
      setSourceTags((current) => [...current, created].sort((left, right) => (
        left.sort_order - right.sort_order || left.display_name.localeCompare(right.display_name, "zh-Hans-CN")
      )));
      updateSourceTagRuleDraft("sourceTagId", created.source_tag_id);
      setCreatingSourceTag(false);
      setNewSourceTagName("");
      notify("success", `已创建投递渠道“${created.display_name}”。`);
    } catch (error) {
      notify("error", humanizeError(error));
    } finally {
      setSavingSourceTag(false);
    }
  };

  const saveSourceTagRule = async () => {
    if (!selectedConfig?.configured) {
      notify("error", "请先保存收件通道，再设置投递渠道规则。");
      return;
    }
    const matchValue = sourceTagRuleDraft.matchValue.trim();
    const priority = Number(sourceTagRuleDraft.priority);
    if (!sourceTagRuleDraft.sourceTagId) {
      notify("error", "请选择投递渠道，或先新建一个渠道标签。");
      return;
    }
    if (!matchValue) {
      notify("error", "请填写规则匹配值。");
      return;
    }
    if (!Number.isInteger(priority) || priority < 0 || priority > 10_000) {
      notify("error", "优先级请填写 0 到 10000 的整数。");
      return;
    }

    setSavingSourceTagRule(true);
    try {
      const input = {
        source_tag_id: sourceTagRuleDraft.sourceTagId,
        match_kind: sourceTagRuleDraft.matchKind,
        match_value: matchValue,
        priority,
        enabled: true,
      };
      const saved = editingSourceTagRuleId
        ? await api.updateMailboxSourceTagRule(
          selectedConfig.mailbox_id,
          editingSourceTagRuleId,
          input,
        )
        : await api.createMailboxSourceTagRule(selectedConfig.mailbox_id, input);
      setSourceTagRules((current) => [
        saved,
        ...current.filter((rule) => rule.rule_id !== saved.rule_id),
      ].sort((left, right) => (
        left.priority - right.priority || left.created_at.localeCompare(right.created_at)
      )));
      setEditingSourceTagRuleId(null);
      setSourceTagRuleDraft(newSourceTagRuleDraft(saved.source_tag.source_tag_id));
      notify("success", editingSourceTagRuleId ? "投递渠道规则已保存。" : "投递渠道规则已添加。");
    } catch (error) {
      notify("error", humanizeError(error));
    } finally {
      setSavingSourceTagRule(false);
    }
  };

  const disableSourceTagRule = async (rule: MailboxSourceTagRule) => {
    if (!selectedConfig?.configured || !rule.enabled || disablingSourceTagRuleId === rule.rule_id) return;
    if (!window.confirm(`停用“${rule.source_tag.display_name}”的这条投递渠道规则？后续邮件将不再按此规则标记。`)) return;
    setDisablingSourceTagRuleId(rule.rule_id);
    try {
      await api.disableMailboxSourceTagRule(selectedConfig.mailbox_id, rule.rule_id);
      setSourceTagRules((current) => current.map((item) => (
        item.rule_id === rule.rule_id ? { ...item, enabled: false } : item
      )));
      if (editingSourceTagRuleId === rule.rule_id) cancelSourceTagRuleEdit();
      notify("success", "投递渠道规则已停用，已有简历的历史标签会保留。" );
    } catch (error) {
      notify("error", humanizeError(error));
    } finally {
      setDisablingSourceTagRuleId(null);
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
    ? "从空白配置开始。连接后会按选定的首次范围导入附件，完成后只接收新邮件。"
    : "选择首次范围后，系统会在后台导入对应时间内的附件；完成后只接收新邮件。";
  const showMailboxOverview = Boolean(selectedConfig && !isCreating && !isEditingConnection);
  const formUsesOAuth = isCreating
    ? draftProvider?.authentication_mode === "oauth2"
    : selectedConfig?.authentication_mode === "oauth2";
  const formUsesCustomEndpoint = isCreating && draftProviderAllowsCustomEndpoint;
  const formProviderName = isCreating
    ? draftProvider?.display_name ?? "尚未选择"
    : selectedConfig ? mailboxProviderDisplayName(selectedConfig) : "已配置 IMAP 邮箱";
  const formCredentialLabel = formUsesCustomEndpoint
    ? "专用授权码或客户端密码"
    : (
      draftProvider?.credential_label
      ?? (selectedConfig?.authentication_mode === "app_password" ? "邮箱授权码" : "邮箱授权")
    );
  const formProviderDescription = !isCreating
    ? "服务商和收件来源已固定。需要改用其他邮箱或服务器时，请新建收件通道。"
    : !draftProvider
      ? "选择常用服务商，或使用通用 IMAP 手动填写服务器域名。"
      : formUsesCustomEndpoint
        ? "仅支持 SSL/TLS 加密的 IMAPS 连接。服务器域名会在保存时进行安全校验。"
        : "服务器地址、端口和加密方式已按所选服务商预填。";
  const savedGenericEndpoint = selectedConfig?.provider_key === "generic_imap" && selectedConfig.imap_host
    ? ` · ${selectedConfig.imap_host}:${selectedConfig.imap_port ?? 993}`
    : "";

  const mailboxConnectionFields = (
    <div className="mailbox-connection-form">
      <section className="mailbox-form-section" aria-labelledby="mailbox-provider-heading">
        <div className="mailbox-form-section-heading">
          <div>
            <h3 id="mailbox-provider-heading">邮箱服务商</h3>
            <p>{formProviderDescription}</p>
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
              <span>{mailboxAuthenticationModeLabel(selectedConfig?.authentication_mode ?? null)}{savedGenericEndpoint}</span>
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

      {isCreating && (
        <section className="mailbox-form-section" aria-labelledby="mailbox-initial-sync-heading">
          <div className="mailbox-form-section-heading">
            <div>
              <h3 id="mailbox-initial-sync-heading">首次入库范围</h3>
              <p>只在首次绑定时生效，创建后不能修改。</p>
            </div>
          </div>
          <div className="form-grid mailbox-form-grid">
            <div className="field-stack span-full">
              <label
                className="field-label"
                htmlFor="initial-sync-lookback-days"
                id="initial-sync-lookback-days-label"
              >
                导入历史邮件
              </label>
              <BackofficeSelect
                ariaDescribedBy="initial-sync-lookback-days-hint"
                ariaLabelledBy="initial-sync-lookback-days-label"
                id="initial-sync-lookback-days"
                disabled={saving || authorizing}
                onChange={(value) => {
                  const days = Number(value);
                  if (!Number.isInteger(days)) return;
                  updateDraft("initialSyncLookbackDays", days);
                }}
                options={mailboxInitialSyncLookbackOptions}
                value={String(draft.initialSyncLookbackDays)}
              />
              <p className="field-help" id="initial-sync-lookback-days-hint">
                系统只会导入这个时间范围内的简历附件；首次完成后，后续同步始终只接收新邮件。
              </p>
            </div>
          </div>
        </section>
      )}

      <section className="mailbox-form-section" aria-labelledby="mailbox-connection-heading">
        <div className="mailbox-form-section-heading">
          <div>
            <h3 id="mailbox-connection-heading">连接与同步</h3>
            <p>系统只同步收件箱（INBOX）。{formUsesOAuth ? "授权将在服务商页面完成，系统不会收集网页登录密码。" : "请使用服务商生成的专用授权码或客户端密码。"}</p>
          </div>
        </div>
        <div className="form-grid mailbox-form-grid">
          {isCreating && formUsesCustomEndpoint && (
            <>
              <div className="field-stack span-full">
                <label className="field-label" htmlFor="imap-host">IMAP 服务器域名</label>
                <BackofficeInput
                  aria-describedby="imap-host-hint"
                  autoComplete="off"
                  disabled={saving || authorizing || !draftProvider?.available}
                  id="imap-host"
                  onChange={(value) => updateDraft("imapHost", value)}
                  placeholder="例如：imap.example.com"
                  spellCheck={false}
                  value={draft.imapHost}
                />
                <p className="field-help" id="imap-host-hint">
                  仅填写服务器域名，不要填写 https://、路径或端口。保存时会校验连接目标。
                </p>
              </div>
              <div className="mailbox-imaps-security span-full" role="note">
                <span className="mailbox-imaps-security-icon"><Icon name="check" size={16} /></span>
                <div>
                  <strong>加密连接已固定</strong>
                  <p>SSL/TLS（IMAPS）· 端口 993</p>
                </div>
              </div>
            </>
          )}
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
            <div className="field-stack span-full">
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

  const selectableSourceTags = sourceTags.filter(
    (tag) => tag.enabled || tag.source_tag_id === sourceTagRuleDraft.sourceTagId,
  );
  const activeSourceTagRuleCount = sourceTagRules.filter((rule) => rule.enabled).length;
  const mailboxSourceTagRulesPanel = (
    <details className="panel mailbox-source-tag-rules">
      <summary className="panel-heading mailbox-disclosure-heading">
        <div>
          <h2>投递渠道规则</h2>
          <p>标记后续邮件的投递来源，可用于筛选。</p>
        </div>
        {selectedConfig?.configured && (
          <span className="status-pill">
            {activeSourceTagRuleCount ? `${activeSourceTagRuleCount} 条已启用` : "未设置"}
          </span>
        )}
      </summary>
      {sourceTagRulesLoading ? (
        <TableSkeleton />
      ) : !selectedConfig?.configured ? (
        <div className="mailbox-source-tag-empty">
          <strong>先保存收件通道</strong>
          <span>保存后可为这个通道设置后续邮件的投递渠道识别规则。</span>
        </div>
      ) : (
        <>
          <p className="mailbox-source-tag-note">
            只匹配后续邮件，不保存实际邮件头；已有标签不会因改规则而改变。
          </p>
          {sourceTagRulesError && (
            <div className="mailbox-source-tag-error" role="alert">
              <span>{sourceTagRulesError}</span>
              <BackofficeButton
                disabled={sourceTagRulesLoading}
                icon={<Icon name="refresh" size={15} />}
                onClick={() => void loadSourceTagRules(selectedConfig.mailbox_id)}
              >
                重试
              </BackofficeButton>
            </div>
          )}
          {sourceTagRules.length > 0 ? (
            <div className="mailbox-source-tag-rule-list">
              {sourceTagRules.map((rule) => {
                const editing = editingSourceTagRuleId === rule.rule_id;
                const disabling = disablingSourceTagRuleId === rule.rule_id;
                return (
                  <div
                    className={`mailbox-source-tag-rule${rule.enabled ? "" : " is-disabled"}${editing ? " is-editing" : ""}`}
                    key={rule.rule_id}
                  >
                    <div className="mailbox-source-tag-rule-copy">
                      <div className="mailbox-source-tag-rule-title">
                        <span className="tag">{rule.source_tag.display_name}</span>
                        <span className={`status-pill${rule.enabled ? " is-success" : ""}`}>
                          {rule.enabled ? "已启用" : "已停用"}
                        </span>
                      </div>
                      <span>
                        {sourceTagRuleMatchLabel(rule.match_kind)} · {rule.match_value}
                        {rule.priority !== 100 ? ` · 优先级 ${rule.priority}` : ""}
                      </span>
                    </div>
                    <div className="mailbox-source-tag-rule-actions">
                      <BackofficeButton
                        disabled={savingSourceTagRule || disabling || !rule.enabled}
                        onClick={() => editSourceTagRule(rule)}
                      >
                        编辑
                      </BackofficeButton>
                      {rule.enabled && (
                        <BackofficeButton
                          disabled={savingSourceTagRule || disabling}
                          loading={disabling}
                          onClick={() => void disableSourceTagRule(rule)}
                          tone="danger"
                        >
                          停用
                        </BackofficeButton>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          ) : !sourceTagRulesError ? (
            <div className="mailbox-source-tag-empty">
              <strong>还没有投递渠道规则</strong>
              <span>可先用下方规则把后续投递标记为招聘平台、内推或其他来源。</span>
            </div>
          ) : null}

          <section aria-label={editingSourceTagRuleId ? "编辑投递渠道规则" : "添加投递渠道规则"} className="mailbox-source-tag-rule-editor">
            <div className="mailbox-source-tag-rule-editor-heading">
              <div>
                <h3>{editingSourceTagRuleId ? "编辑规则" : "添加规则"}</h3>
                <p>命中后为附件保留投递渠道标签。</p>
              </div>
              {editingSourceTagRuleId && (
                <BackofficeButton disabled={savingSourceTagRule} onClick={cancelSourceTagRuleEdit}>
                  取消编辑
                </BackofficeButton>
              )}
            </div>
            <div className="form-grid mailbox-source-tag-rule-fields">
              <div className="field-stack">
                <label className="field-label" htmlFor="mailbox-source-tag" id="mailbox-source-tag-label">投递渠道</label>
                {selectableSourceTags.length ? (
                  <BackofficeSelect
                    ariaLabelledBy="mailbox-source-tag-label"
                    disabled={savingSourceTagRule || savingSourceTag}
                    id="mailbox-source-tag"
                    onChange={(value) => updateSourceTagRuleDraft("sourceTagId", value)}
                    options={selectableSourceTags.map((tag) => ({
                      label: tag.enabled ? tag.display_name : `${tag.display_name}（已停用）`,
                      value: tag.source_tag_id,
                    }))}
                    value={sourceTagRuleDraft.sourceTagId}
                  />
                ) : (
                  <div className="mailbox-source-tag-empty-inline">先新建一个投递渠道标签。</div>
                )}
                <div className="mailbox-source-tag-create-toggle">
                  <button
                    className="text-button"
                    disabled={savingSourceTag || savingSourceTagRule}
                    onClick={() => setCreatingSourceTag((current) => !current)}
                    type="button"
                  >
                    {creatingSourceTag ? "收起新建标签" : "新建投递渠道标签"}
                  </button>
                </div>
                {creatingSourceTag && (
                  <div className="mailbox-source-tag-create">
                    <BackofficeInput
                      disabled={savingSourceTag}
                      maxLength={64}
                      onChange={setNewSourceTagName}
                      placeholder="例如：员工内推"
                      value={newSourceTagName}
                    />
                    <BackofficeButton
                      disabled={savingSourceTag || !newSourceTagName.trim()}
                      loading={savingSourceTag}
                      onClick={() => void createSourceTag()}
                    >
                      创建标签
                    </BackofficeButton>
                  </div>
                )}
              </div>
              <div className="field-stack">
                <label className="field-label" htmlFor="mailbox-source-tag-match-kind" id="mailbox-source-tag-match-kind-label">匹配字段</label>
                <BackofficeSelect
                  ariaLabelledBy="mailbox-source-tag-match-kind-label"
                  disabled={savingSourceTagRule}
                  id="mailbox-source-tag-match-kind"
                  onChange={(value) => updateSourceTagRuleDraft("matchKind", value as SourceTagRuleMatchKind)}
                  options={sourceTagRuleMatchOptions}
                  value={sourceTagRuleDraft.matchKind}
                />
              </div>
              <div className="field-stack">
                <label className="field-label" htmlFor="mailbox-source-tag-match-value">匹配值</label>
                <BackofficeInput
                  disabled={savingSourceTagRule}
                  id="mailbox-source-tag-match-value"
                  maxLength={255}
                  onChange={(value) => updateSourceTagRuleDraft("matchValue", value)}
                  placeholder={
                    sourceTagRuleDraft.matchKind === "sender_domain"
                      ? "例如：example.com"
                      : sourceTagRuleDraft.matchKind === "sender_address"
                        ? "例如：no-reply@example.com"
                        : "例如：候选人简历"
                  }
                  value={sourceTagRuleDraft.matchValue}
                />
              </div>
              <div className="field-stack">
                <label className="field-label" htmlFor="mailbox-source-tag-priority">优先级</label>
                <BackofficeInput
                  disabled={savingSourceTagRule}
                  id="mailbox-source-tag-priority"
                  inputMode="numeric"
                  maxLength={5}
                  onChange={(value) => updateSourceTagRuleDraft("priority", value.replace(/[^0-9]/g, ""))}
                  value={sourceTagRuleDraft.priority}
                />
                <p className="field-help">数值越小越优先，默认 100。</p>
              </div>
            </div>
            <div className="review-actions mailbox-source-tag-rule-editor-actions">
              <BackofficeButton
                disabled={savingSourceTagRule || savingSourceTag || !sourceTagRuleDraft.sourceTagId}
                icon={savingSourceTagRule ? undefined : <Icon name="check" size={16} />}
                loading={savingSourceTagRule}
                onClick={() => void saveSourceTagRule()}
                tone="primary"
              >
                {savingSourceTagRule ? "正在保存" : editingSourceTagRuleId ? "保存规则" : "添加规则"}
              </BackofficeButton>
            </div>
          </section>
        </>
      )}
    </details>
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
            <p>{mailboxProviderDisplayName(selectedConfig)} · {selectedConfig.email_address || "尚未配置接收邮箱"}</p>
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
          <span>首次范围</span>
          <strong>{mailboxInitialSyncLookbackLabel(selectedInitialSyncLookbackDays)}</strong>
        </div>
        <div>
          <span>最近同步</span>
          <strong>{selectedConfig.last_synced_at ? formatLibraryDate(selectedConfig.last_synced_at) : "尚未同步"}</strong>
        </div>
        <div>
          <span>{selectedInitialSyncLookbackDays > 0 ? "首次导入" : "后台同步"}</span>
          <strong>{selectedInitialSyncLookbackDays > 0 ? selectedInitialImportStatus : selectedSyncJob ? mailboxBackgroundJobStatusLabel(selectedSyncJob) : selectedMailboxCanSync ? "已启用" : selectedConfig.enabled ? "等待授权" : "已暂停"}</strong>
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
          <p>连接招聘邮箱后，可按首次范围导入历史附件，后续持续接收新邮件。</p>
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
                <div><strong>保存连接</strong><p>系统按首次范围建立入库起点，范围外的历史邮件不会扫描。</p></div>
              </li>
              <li>
                <span>2</span>
                <div><strong>后台同步</strong><p>如选择历史范围，先在后台导入对应附件；随后按计划检查新邮件。</p></div>
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
                  <p>{isCreating ? "选择首次范围后，系统会在后台导入对应时间内的附件；之后只接收新邮件。" : "首次范围已固定；授权码始终保持隐藏，留空则继续使用已保存的值。"}</p>
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

          {mailboxSourceTagRulesPanel}

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
                          <td className="mailbox-source-cell">
                            <div className="mailbox-import-source">
                              <strong>{item.mailbox_display_name || "已归档收件通道"}</strong>
                              {item.source_tags.length > 0 && (
                                <div
                                  aria-label={`投递渠道：${item.source_tags.map((tag) => tag.display_name).join("、")}`}
                                  className="mailbox-import-source-tags"
                                  title={`投递渠道：${item.source_tags.map((tag) => tag.display_name).join("、")}`}
                                >
                                  {item.source_tags.slice(0, 2).map((tag) => (
                                    <span className="tag" key={tag.source_tag_id}>{tag.display_name}</span>
                                  ))}
                                  {item.source_tags.length > 2 && <span>+{item.source_tags.length - 2}</span>}
                                </div>
                              )}
                            </div>
                          </td>
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
            ) : <div className="mailbox-history-empty"><span className="empty-glyph"><Icon name="inbox" size={21} /></span><div><h3>还没有附件入库记录</h3><p>首次范围内和后续收到的简历附件，都会显示在这里。</p></div></div>}
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
