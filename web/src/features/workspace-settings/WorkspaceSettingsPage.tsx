import { Icon, type IconName } from "../../icons";
import { CandidateDataLifecyclePage } from "../candidate-data/CandidateDataLifecyclePage";
import { MailboxPage } from "../mailbox/MailboxPage";
import { LegacyWorkspaceAdoptionPanel } from "./LegacyWorkspaceAdoptionPanel";
import type { WorkspaceSettingsSection } from "../workspace-shell/workspace-navigation-types";
import "./workspace-settings.css";

export type { WorkspaceSettingsSection } from "../workspace-shell/workspace-navigation-types";

export function WorkspaceSettingsPage({
  activeSection,
  canAdoptLegacyWorkspace,
  canManageCandidateData,
  canManageMailbox,
  formatError,
  notify,
  onAdoptLegacyWorkspace,
  onImported,
  onOpenLibrary,
  onSelectSection,
  role,
}: {
  activeSection: WorkspaceSettingsSection;
  canAdoptLegacyWorkspace: boolean;
  canManageCandidateData: boolean;
  canManageMailbox: boolean;
  formatError: (error: unknown) => string;
  notify: (kind: "success" | "error", message: string) => void;
  onAdoptLegacyWorkspace: (legacyAdminPassword: string) => Promise<void>;
  onImported: () => void;
  onOpenLibrary: () => void;
  onSelectSection: (section: WorkspaceSettingsSection) => void;
  role: "admin" | "recruiter" | null;
}) {
  const sections: Array<{
    id: WorkspaceSettingsSection;
    label: string;
    description: string;
    icon: IconName;
  }> = [];

  if (canManageMailbox) {
    sections.push({
      id: "mailbox",
      label: "收件邮箱",
      description: "管理收件通道、同步和附件入库保留。",
      icon: "inbox",
    });
  }
  if (canManageCandidateData) {
    sections.push({
      id: "data",
      label: "候选人数据与保留",
      description: "管理资料保留、导出、删除和访问记录。",
      icon: "gear",
    });
  }
  if (canAdoptLegacyWorkspace) {
    sections.push({
      id: "account",
      label: "账户接管",
      description: "把历史工作区交给当前账号。",
      icon: "user",
    });
  }

  const currentSection = sections.some((section) => section.id === activeSection)
    ? activeSection
    : sections[0]?.id;
  if (!currentSection) return null;

  return (
    <div className="page-frame settings-page">
      <header className="page-heading">
        <div>
          <h1>设置</h1>
          <p>管理当前工作区的收件通道，以及候选人资料的保留和访问规则。</p>
        </div>
      </header>
      <div className="settings-layout">
        <nav aria-label="设置分类" className="settings-navigation">
          <p className="settings-navigation-label">工作区设置</p>
          <div aria-orientation="horizontal" className="settings-navigation-list" role="tablist">
            {sections.map((section) => {
              const selected = section.id === currentSection;
              return (
                <button
                  aria-controls={`settings-panel-${section.id}`}
                  aria-label={section.label}
                  aria-selected={selected}
                  className={`settings-navigation-item${selected ? " is-active" : ""}`}
                  id={`settings-tab-${section.id}`}
                  key={section.id}
                  onClick={() => onSelectSection(section.id)}
                  role="tab"
                  type="button"
                >
                  <span className="settings-navigation-icon"><Icon name={section.icon} size={17} /></span>
                  <span>
                    <strong>{section.label}</strong>
                    <small>{section.description}</small>
                  </span>
                </button>
              );
            })}
          </div>
        </nav>
        <section
          aria-labelledby={`settings-tab-${currentSection}`}
          className="settings-content"
          id={`settings-panel-${currentSection}`}
          role="tabpanel"
          tabIndex={-1}
        >
          {currentSection === "mailbox" ? (
            <MailboxPage
              embedded
              humanizeError={formatError}
              notify={notify}
              onImported={onImported}
              role={role}
            />
          ) : currentSection === "data" ? (
            <CandidateDataLifecyclePage
              embedded
              formatError={formatError}
              notify={notify}
              onOpenLibrary={onOpenLibrary}
            />
          ) : (
            <LegacyWorkspaceAdoptionPanel
              formatError={formatError}
              onAdopt={onAdoptLegacyWorkspace}
            />
          )}
        </section>
      </div>
    </div>
  );
}
