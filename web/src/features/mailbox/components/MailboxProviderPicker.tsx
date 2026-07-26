import { Icon } from "../../../icons";
import type { MailboxProvider } from "../../../types";
import { TableSkeleton } from "../../../backoffice/ui/TableSkeleton";

interface MailboxProviderPickerProps {
  disabled?: boolean;
  loading: boolean;
  onChange: (provider: MailboxProvider) => void;
  providers: MailboxProvider[];
  value: string;
}

function authenticationLabel(provider: MailboxProvider): string {
  return provider.authentication_mode === "oauth2" ? "网页授权" : "专用授权码";
}

/**
 * New channels can only select deployment-reviewed providers. The visual
 * selection deliberately uses buttons instead of a free-text endpoint field,
 * so no browser state can become an arbitrary IMAP destination.
 */
export function MailboxProviderPicker({
  disabled = false,
  loading,
  onChange,
  providers,
  value,
}: MailboxProviderPickerProps) {
  if (loading) {
    return <div className="mailbox-provider-loading"><TableSkeleton /></div>;
  }

  if (!providers.length) {
    return (
      <p className="mailbox-provider-empty" role="alert">
        暂时无法读取可接入的邮箱服务商，请刷新后重试。
      </p>
    );
  }

  return (
    <div aria-label="邮箱服务商" className="mailbox-provider-options" role="radiogroup">
      {providers.map((provider) => {
        const selected = value === provider.provider_key;
        const unavailable = !provider.available;
        return (
          <button
            aria-checked={selected}
            aria-describedby={`mailbox-provider-${provider.provider_key}-hint`}
            className={`mailbox-provider-option${selected ? " is-selected" : ""}${unavailable ? " is-unavailable" : ""}`}
            disabled={disabled || unavailable}
            key={provider.provider_key}
            onClick={() => onChange(provider)}
            role="radio"
            type="button"
          >
            <span className="mailbox-provider-option-heading">
              <span className="mailbox-provider-option-mark"><Icon name="inbox" size={16} /></span>
              <span>
                <strong>{provider.display_name}</strong>
                <small>{authenticationLabel(provider)}</small>
              </span>
            </span>
            <span className={`status-pill${unavailable ? " is-warning" : ""}`}>
              {unavailable ? "当前未启用" : selected ? "已选择" : "可连接"}
            </span>
            <span className="sr-only" id={`mailbox-provider-${provider.provider_key}-hint`}>
              {unavailable ? "当前部署尚未启用该服务商。" : provider.help_text}
            </span>
          </button>
        );
      })}
    </div>
  );
}
