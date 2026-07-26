import type {
  MailboxAuthenticationMode,
  MailboxBackgroundJob,
  MailboxConfig,
  MailboxRetentionPolicy,
  MailboxRetentionPreview,
  MailboxRetentionRun,
} from "../../types";

export interface MailboxDraft {
  displayName: string;
  providerKey: string;
  emailAddress: string;
  mailbox: string;
  password: string;
  enabled: boolean;
}

export function newMailboxDraft(): MailboxDraft {
  return {
    displayName: "",
    providerKey: "",
    emailAddress: "",
    mailbox: "INBOX",
    password: "",
    enabled: true,
  };
}

export function mailboxDraftFromConfig(config: MailboxConfig): MailboxDraft {
  return {
    displayName: config.display_name,
    providerKey: config.provider_key || "",
    emailAddress: config.email_address || "",
    mailbox: config.mailbox || "INBOX",
    password: "",
    enabled: config.enabled,
  };
}

export function mailboxDraftIsDirty(
  draft: MailboxDraft,
  config: MailboxConfig | null,
  isCreating: boolean,
): boolean {
  const baseline = isCreating || !config
    ? newMailboxDraft()
    : mailboxDraftFromConfig(config);

  return (
    draft.displayName !== baseline.displayName
    || draft.providerKey !== baseline.providerKey
    || draft.emailAddress !== baseline.emailAddress
    || draft.mailbox !== baseline.mailbox
    || draft.enabled !== baseline.enabled
    || Boolean(draft.password.trim())
  );
}

export function mailboxAuthenticationModeLabel(mode: MailboxAuthenticationMode | null): string {
  return mode === "oauth2" ? "OAuth 授权" : "授权码连接";
}

export function mailboxProviderDisplayName(config: MailboxConfig): string {
  return config.provider_display_name || "已配置 IMAP 邮箱";
}

export function mailboxRequiresAuthorization(config: MailboxConfig): boolean {
  return config.authentication_mode === "oauth2"
    && (config.authorization_status === "not_connected" || config.authorization_status === "reauthorization_required");
}

export function mailboxCanSync(config: MailboxConfig): boolean {
  return config.enabled
    && !config.archived_at
    && config.authorization_status === "connected";
}

export function mailboxChannelStatus(config: MailboxConfig): string {
  if (config.archived_at) return "已归档";
  if (config.authorization_status === "reauthorization_required") return "需重新授权";
  if (config.authorization_status === "unavailable") return "服务未启用";
  if (config.authorization_status === "not_connected") return "待连接";
  if (config.active_sync_alert) return "需处理";
  return config.enabled ? "已启用" : "已暂停";
}

export function mailboxChannelStatusClass(config: MailboxConfig): string {
  if (config.archived_at) return "";
  if (config.authorization_status === "reauthorization_required") return " is-error";
  if (config.authorization_status === "unavailable" || config.authorization_status === "not_connected") return " is-warning";
  if (config.active_sync_alert) return " is-error";
  return config.enabled ? " is-success" : " is-warning";
}

export function mailboxSyncAlertTitle(config: MailboxConfig): string {
  const alert = config.active_sync_alert;
  if (!alert) return "";
  return alert.severity === "critical" ? "同步配置需要处理" : "同步持续失败";
}

export const mailboxRetentionPolicies: Array<{
  value: MailboxRetentionPolicy;
  label: string;
  description: string;
}> = [
  {
    value: "minimal",
    label: "最小保留",
    description: "正文和成功附件副本不持久化；失败附件保留 7 天。",
  },
  {
    value: "standard",
    label: "标准保留",
    description: "正文保留 7 天；成功附件副本保留 24 小时；失败附件保留 30 天。",
  },
  {
    value: "audit",
    label: "审计保留",
    description: "正文保留 30 天；成功附件副本保留 7 天；失败附件保留 90 天。",
  },
];

export const mailboxImportErrorMessages: Record<string, string> = {
  mailbox_import_not_found: "这条附件记录已不存在或无法访问。",
  mailbox_import_not_retryable: "这份附件当前不能重新入库。",
  mailbox_import_retry_in_progress: "这份附件正在重新入库，请稍后刷新。",
  mailbox_import_retry_superseded: "这份附件已由更新的重试请求接管，请刷新后查看结果。",
  mailbox_background_job_failed: "后台任务暂时失败，系统会按队列策略再次尝试。",
  mailbox_background_job_lease_expired: "后台任务意外中断，系统正在重新安排处理。",
  mailbox_task_source_changed: "收件通道配置已变化，旧的同步任务已停止。",
  mailbox_config_archived: "该收件通道已归档，不能再同步新邮件。",
  mailbox_not_enabled: "该收件邮箱已暂停，请启用后重试。",
  mailbox_credentials_unavailable: "邮箱授权码无法读取，请重新保存后再同步。",
  mailbox_imap_host_not_allowed: "该 IMAP 地址不在当前部署允许的服务商范围内。",
  mailbox_imap_port_not_allowed: "只支持加密 IMAPS 的 993 端口。",
  mailbox_imap_address_not_allowed: "该 IMAP 地址解析到不安全网络，系统已拒绝连接。",
  mailbox_imap_dns_failed: "无法安全解析该 IMAP 地址，请检查服务商配置。",
  mailbox_imap_argument_invalid: "邮箱账号、文件夹或授权码包含 IMAP 不支持的字符，请重新配置。",
  mailbox_imap_response_line_too_large: "邮箱返回的数据行超过安全上限，系统已停止本次同步。",
  mailbox_connection_failed: "无法连接邮箱，请检查 IMAP 地址、端口和授权码。",
  mailbox_select_failed: "无法打开指定的邮箱文件夹。",
  mailbox_status_failed: "无法读取邮箱当前位置，请检查文件夹设置后重试。",
  mailbox_source_epoch_changed: "邮箱来源标识已变化，通道已暂停，请归档后新建。",
  mailbox_source_watermark_invalid: "邮箱 UID 水位线异常，通道已暂停，请归档后新建。",
  mailbox_message_too_large: "邮件超过系统可处理大小，已跳过且不会重复下载。",
  mailbox_message_headers_too_large: "邮件头超过系统可处理范围，已安全跳过。",
  mailbox_mime_structure_too_complex: "邮件 MIME 结构过于复杂，已安全跳过。",
  mailbox_attachment_count_exceeded: "邮件附件数量超过单封处理上限，已安全跳过。",
  mailbox_attachment_too_large: "邮件中的简历附件超过单个文件上限，已安全跳过。",
  mailbox_attachment_total_too_large: "邮件中的简历附件总量超过单封处理上限，已安全跳过。",
  mailbox_search_response_too_large: "邮箱待处理邮件范围过大，系统暂未展开扫描。",
  attachment_validation_failed: "附件未通过文件校验，请候选人重新发送。",
  attachment_text_extraction_failed: "附件文字提取失败，请候选人重新发送清晰原件。",
  attachment_import_failed: "附件暂时无法入库，请稍后重试。",
  attachment_message_unavailable: "原邮件或附件已无法获取，请候选人重新发送。",
  attachment_source_changed: "收件邮箱来源已变化，不能安全重试该附件。",
  attachment_source_unavailable: "原收件邮箱已不可用，不能重试该附件。",
  attachment_retry_interrupted: "上次重新入库被中断，可再次尝试。",
  attachment_content_claim_expired: "相同附件的处理未完成，现可重新入库。",
};

export function mailboxImportErrorLabel(error: string | null): string {
  if (!error) return "附件处理没有完成，请稍后重试。";
  return mailboxImportErrorMessages[error] ?? "附件处理没有完成，请稍后重试。";
}

export function mailboxImportStatusLabel(status: string, canRetry = false): string {
  switch (status) {
    case "imported":
      return "已入库";
    case "duplicate":
      return "已去重";
    case "deduplicating":
      return "等待去重";
    case "skipped":
      return "已跳过";
    case "retrying":
      return canRetry ? "可重新入库" : "正在重试";
    case "failed":
      return "处理失败";
    default:
      return "处理中";
  }
}

export function mailboxBackgroundJobStatusLabel(job: MailboxBackgroundJob): string {
  if (job.status === "queued") return job.job_kind === "sync" ? "等待后台同步" : "等待后台重试";
  if (job.status === "running") return job.job_kind === "sync" ? "正在后台同步" : "正在后台重试";
  if (job.status === "completed") return "已完成";
  return "处理失败";
}

export function mailboxBackgroundJobStatusClass(job: MailboxBackgroundJob): string {
  if (job.status === "completed") return "is-success";
  if (job.status === "failed") return "is-error";
  return "is-progress";
}

export function mailboxRetentionPolicyLabel(policy: MailboxRetentionPolicy): string {
  return mailboxRetentionPolicies.find((option) => option.value === policy)?.label ?? "标准保留";
}

export function mailboxRetentionRunStatusLabel(status: MailboxRetentionRun["status"]): string {
  switch (status) {
    case "queued":
      return "等待执行";
    case "running":
      return "正在清理";
    case "completed":
      return "已完成";
    case "completed_with_errors":
      return "完成但有异常";
    case "failed":
      return "清理失败";
  }
}

export function mailboxRetentionRunStatusClass(status: MailboxRetentionRun["status"]): string {
  switch (status) {
    case "completed":
      return "is-success";
    case "completed_with_errors":
      return "is-warning";
    case "failed":
      return "is-error";
    default:
      return "is-progress";
  }
}

export function mailboxRetentionRunErrorLabel(errorCode: string | null): string {
  if (!errorCode) return "";
  const labels: Record<string, string> = {
    retention_cleanup_interrupted: "清理任务被中断，可稍后重试。",
    retention_cleanup_storage_failed: "部分缓存副本暂时无法删除，系统会稍后重试。",
    retention_cleanup_retry_scheduled: "部分内容将按退避策略再次清理。",
    storage_delete_failed: "部分缓存副本暂时无法删除，系统会在下次任务中重试。",
  };
  return labels[errorCode] ?? "部分内容尚未清理完成，系统会保留安全记录后重试。";
}

export function mailboxRetentionDueCount(
  summary: Pick<
    MailboxRetentionPreview,
    "expired_body_count" | "expired_attachment_copy_count" | "expired_failure_artifact_count"
  >,
): number {
  return summary.expired_body_count
    + summary.expired_attachment_copy_count
    + summary.expired_failure_artifact_count;
}
