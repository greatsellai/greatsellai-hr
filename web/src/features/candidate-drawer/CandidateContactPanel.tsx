import type { ResumeContact } from "../../types";

type ToastKind = "success" | "error";

interface CandidateContactPanelProps {
  contacts: ResumeContact[];
  onNotify: (kind: ToastKind, message: string) => void;
}

async function copyContactValue(value: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(value);
      return;
    } catch {
      // Some embedded browsers expose Clipboard API but deny writes. Use the
      // controlled fallback before reporting a user-visible copy failure.
    }
  }

  const fallback = document.createElement("textarea");
  fallback.value = value;
  fallback.setAttribute("aria-hidden", "true");
  fallback.setAttribute("readonly", "");
  fallback.tabIndex = -1;
  fallback.style.position = "fixed";
  fallback.style.opacity = "0";
  fallback.style.pointerEvents = "none";
  document.body.appendChild(fallback);
  fallback.select();
  const copied = document.execCommand("copy");
  fallback.remove();
  if (!copied) throw new Error("clipboard_unavailable");
}

export function CandidateContactPanel({
  contacts,
  onNotify,
}: CandidateContactPanelProps) {
  const orderedContacts = [...contacts].sort(
    (left, right) =>
      (left.kind === "phone" ? 0 : 1) - (right.kind === "phone" ? 0 : 1),
  );

  const copyContact = async (contact: ResumeContact) => {
    const label = contact.kind === "phone" ? "电话" : "邮箱";
    try {
      await copyContactValue(contact.value);
      onNotify("success", `${label}已复制。`);
    } catch {
      onNotify("error", "无法复制，请手动选择联系方式。");
    }
  };

  return (
    <section aria-labelledby="candidate-contact-heading" className="drawer-contact-summary">
      <div className="drawer-contact-summary-heading">
        <h3 id="candidate-contact-heading">联系方式</h3>
        <p>仅从简历原文提取，不参与筛选、评分、JD 匹配或招聘助手。</p>
      </div>
      <dl className="drawer-contact-list">
        {orderedContacts.map((contact, index) => {
          const label = contact.kind === "phone" ? "电话" : "邮箱";
          return (
            <div className="drawer-contact-item" key={`${contact.kind}-${contact.value}-${index}`}>
              <dt>{label}</dt>
              <dd>
                <span>{contact.value}</span>
                <button
                  aria-label={`复制${label}`}
                  className="button button-ghost drawer-contact-copy"
                  onClick={() => void copyContact(contact)}
                  type="button"
                >
                  复制
                </button>
              </dd>
            </div>
          );
        })}
      </dl>
    </section>
  );
}
