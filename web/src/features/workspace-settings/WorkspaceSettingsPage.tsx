import type { KeyboardEvent } from "react";
import { Icon, type IconName } from "../../icons";
import { CandidateDataLifecyclePage } from "../candidate-data/CandidateDataLifecyclePage";
import { MailboxPage } from "../mailbox/MailboxPage";
import type { WorkspaceSettingsSection } from "../workspace-shell/workspace-navigation-types";
import "./workspace-settings.css";

export type { WorkspaceSettingsSection } from "../workspace-shell/workspace-navigation-types";

export function WorkspaceSettingsPage({
  activeSection,
  canManageCandidateData,
  canManageMailbox,
  formatError,
  notify,
  onImported,
  onOpenLibrary,
  onSelectSection,
  role,
}: {
  activeSection: WorkspaceSettingsSection;
  canManageCandidateData: boolean;
  canManageMailbox: boolean;
  formatError: (error: unknown) => string;
  notify: (kind: "success" | "error", message: string) => void;
  onImported: () => void;
  onOpenLibrary: () => void;
  onSelectSection: (section: WorkspaceSettingsSection) => void;
  role: "admin" | "recruiter" | null;
}) {
  const sections: Array<{
    id: WorkspaceSettingsSection;
    label: string;
    description: string;
    scope: string;
    guardrail: string;
    icon: IconName;
  }> = [];

  if (canManageMailbox) {
    sections.push({
      id: "mailbox",
      label: "收件邮箱",
      description: "管理收件通道、同步和附件入库保留。",
      scope: "收件通道",
      guardrail: "OAuth 授权、同步队列和附件保留",
      icon: "inbox",
    });
  }
  if (canManageCandidateData) {
    sections.push({
      id: "data",
      label: "候选人数据与保留",
      description: "管理资料保留、导出、删除和访问记录。",
      scope: "数据治理",
      guardrail: "留存、导出、删除恢复和访问审计",
      icon: "gear",
    });
  }

  const currentSection = sections.some((section) => section.id === activeSection)
    ? activeSection
    : sections[0]?.id;
  if (!currentSection) return null;
  const currentSectionMeta = sections.find((section) => section.id === currentSection)!;

  const handleTabKeyDown = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    const lastIndex = sections.length - 1;
    let nextIndex: number | null = null;
    if (event.key === "ArrowRight") nextIndex = index === lastIndex ? 0 : index + 1;
    if (event.key === "ArrowLeft") nextIndex = index === 0 ? lastIndex : index - 1;
    if (event.key === "Home") nextIndex = 0;
    if (event.key === "End") nextIndex = lastIndex;
    if (nextIndex === null) return;

    event.preventDefault();
    const nextSection = sections[nextIndex];
    if (!nextSection) return;
    document.getElementById(`settings-tab-${nextSection.id}`)?.focus();
  };

  return (
    <div className="page-frame settings-page">
      <header className="page-heading settings-heading">
        <div className="settings-heading-main">
          <span aria-hidden="true" className="settings-heading-icon"><Icon name="gear" size={19} /></span>
          <div>
            <h1>设置</h1>
            <p>管理当前工作区的收件通道、候选人资料留存和访问规则。</p>
          </div>
        </div>
        <div aria-label="当前设置权限" className="settings-heading-meta">
          {role && <span className="status-pill">{role === "admin" ? "管理员" : "招聘成员"}</span>}
          <span className="settings-heading-scope">变更仅作用于当前工作区</span>
        </div>
      </header>
      <div className="settings-layout">
        <nav aria-label="设置分类" className="settings-navigation">
          <p className="settings-navigation-label">工作区设置</p>
          <div className="settings-navigation-intro">
            <div>
              <h2>选择管理内容</h2>
              <p>先进入当前要处理的任务，其他记录和规则不会干扰操作。</p>
            </div>
            <span className="settings-navigation-current">{currentSectionMeta.scope}</span>
          </div>
          <div aria-orientation="horizontal" className="settings-navigation-list" role="tablist">
            {sections.map((section, index) => {
              const selected = section.id === currentSection;
              return (
                <button
                  aria-controls={`settings-panel-${section.id}`}
                  aria-describedby={`settings-tab-description-${section.id}`}
                  aria-label={section.label}
                  aria-selected={selected}
                  className={`settings-navigation-item${selected ? " is-active" : ""}`}
                  id={`settings-tab-${section.id}`}
                  key={section.id}
                  onKeyDown={(event) => handleTabKeyDown(event, index)}
                  onClick={() => onSelectSection(section.id)}
                  role="tab"
                  type="button"
                >
                  <span aria-hidden="true" className="settings-navigation-icon"><Icon name={section.icon} size={18} /></span>
                  <span className="settings-navigation-copy">
                    <strong>{section.label}</strong>
                    <small id={`settings-tab-description-${section.id}`}>{section.description}</small>
                  </span>
                  <span aria-hidden="true" className="settings-navigation-scope">{selected ? "当前" : section.scope}</span>
                </button>
              );
            })}
          </div>
          <p className="settings-navigation-guardrail">{currentSectionMeta.guardrail}</p>
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
          ) : (
            <CandidateDataLifecyclePage
              embedded
              formatError={formatError}
              notify={notify}
              onOpenLibrary={onOpenLibrary}
            />
          )}
        </section>
      </div>
    </div>
  );
}
