import { Icon } from "../../../icons";
import type { MailboxConfig } from "../../../types";
import { BackofficeButton } from "../../../backoffice/ui/BackofficeButton";
import { TableSkeleton } from "../../../backoffice/ui/TableSkeleton";
import {
  mailboxAuthenticationModeLabel,
  mailboxChannelStatus,
  mailboxChannelStatusClass,
  mailboxProviderDisplayName,
} from "../mailbox-model";

interface MailboxChannelListProps {
  isCreating: boolean;
  loading: boolean;
  mailboxes: MailboxConfig[];
  selectedMailboxId: string | null;
  onCreate: () => void;
  onSelect: (config: MailboxConfig) => void;
}

/**
 * Channel navigation is deliberately its own component. It remains above the
 * detail area on desktop as well as narrow screens, so this does not recreate
 * the old left/right split settings layout.
 */
export function MailboxChannelList({
  isCreating,
  loading,
  mailboxes,
  selectedMailboxId,
  onCreate,
  onSelect,
}: MailboxChannelListProps) {
  return (
    <section className="panel mailbox-channel-panel" aria-label="收件通道">
      <div className="panel-heading mailbox-channel-heading">
        <div>
          <h2>收件通道</h2>
          <p>每个通道独立保存首次入库范围与同步状态。</p>
        </div>
        <span className="tiny-badge">{mailboxes.length}</span>
      </div>
      {loading ? <TableSkeleton /> : mailboxes.length ? (
        <div className="mailbox-channel-list">
          {mailboxes.map((config) => {
            const selected = !isCreating && config.mailbox_id === selectedMailboxId;
            return (
              <button
                aria-pressed={selected}
                className={`mailbox-channel-row${selected ? " is-selected" : ""}`}
                key={config.mailbox_id}
                onClick={() => onSelect(config)}
                type="button"
              >
                <span className="mailbox-channel-copy">
                  <strong>{config.display_name}</strong>
                  <span className="mailbox-channel-provider">
                    {mailboxProviderDisplayName(config)} · {mailboxAuthenticationModeLabel(config.authentication_mode)}
                  </span>
                  <span>{config.email_address || "尚未配置接收邮箱"}</span>
                </span>
                <span className={`status-pill${mailboxChannelStatusClass(config)}`}>
                  {mailboxChannelStatus(config)}
                </span>
              </button>
            );
          })}
        </div>
      ) : (
        <div className="mailbox-channel-empty">
          <span className="empty-glyph"><Icon name="inbox" size={20} /></span>
          <strong>还没有收件通道</strong>
          <span>新建时可选择从现在开始，或回溯指定天数的简历附件。</span>
        </div>
      )}
      <BackofficeButton
        className="mailbox-add-channel"
        icon={<Icon name="plus" size={16} />}
        onClick={onCreate}
      >
        新建收件通道
      </BackofficeButton>
    </section>
  );
}
