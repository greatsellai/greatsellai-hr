import { lazy, Suspense } from "react";
import { Icon, type IconName } from "../../icons";
import SemiNavigation from "@douyinfe/semi-ui-19/lib/es/navigation";
import SemiLayout from "@douyinfe/semi-ui-19/lib/es/layout";
import { CandidateDataLifecyclePage } from "../candidate-data/CandidateDataLifecyclePage";
import { MailboxPage } from "../mailbox/MailboxPage";
import { AiImportSettingsPanel } from "./AiImportSettingsPanel";
import { DisplayFieldsSettingsPanel } from "./DisplayFieldsSettingsPanel";
import { FilterSectionsSettingsPanel } from "./FilterSectionsSettingsPanel";
import { IntegrationSettingsPanel } from "../integrations/IntegrationSettingsPanel";
import type { WorkspaceSettingsSection } from "../workspace-shell/workspace-navigation-types";

const SemiTag = lazy(() => import("@douyinfe/semi-ui-19/lib/es/tag"));
const SemiTitle = lazy(() => import("@douyinfe/semi-ui-19/lib/es/typography/title"));
const SemiParagraph = lazy(() => import("@douyinfe/semi-ui-19/lib/es/typography/paragraph"));

export type { WorkspaceSettingsSection } from "../workspace-shell/workspace-navigation-types";

interface SettingsNavItem {
  key: WorkspaceSettingsSection;
  label: string;
  icon: IconName;
}

/**
 * The visible settings sections for the current session. Workspace-scoped
 * sections gate on permission; the per-user 筛选显示字段 preference is always
 * available. The installed Semi Navigation (2.101.1) has no `groupTitle` item
 * type, so the groups are a flat list rather than the plan's grouped sidebar.
 */
function settingsNavItems(
  canManageMailbox: boolean,
  canManageCandidateData: boolean,
  canManageAiImport: boolean,
): SettingsNavItem[] {
  const items: SettingsNavItem[] = [
    { key: "integrations", label: "API 与 AI 工具连接", icon: "spark" },
  ];
  if (canManageMailbox) {
    items.push({ key: "mailbox", label: "收件邮箱", icon: "inbox" });
  }
  if (canManageCandidateData) {
    items.push({ key: "data", label: "候选人数据与保留", icon: "gear" });
  }
  if (canManageAiImport) {
    items.push({ key: "ai-import", label: "AI 导入处理", icon: "spark" });
  }
  items.push({ key: "display-fields", label: "筛选显示字段", icon: "layers" });
  items.push({ key: "filter-sections", label: "初筛条件板块", icon: "filter" });
  return items;
}

export function WorkspaceSettingsPage({
  workspaceIdentityKey,
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
  /** Remount private integration state whenever the server identity changes. */
  workspaceIdentityKey: string | null;
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
  const items = settingsNavItems(
    canManageMailbox,
    canManageCandidateData,
    role === "admin",
  );
  const currentSection = items.some((item) => item.key === activeSection)
    ? activeSection
    : items[0].key;

  return (
    <div className="page-frame settings-page">
      <Suspense fallback={<p>加载设置…</p>}>
        <SemiLayout className="settings-layout">
          <SemiLayout.Header className="settings-header">
            <div className="settings-title">
              <SemiTitle heading={4}>设置</SemiTitle>
              {role && (
                <SemiTag color={role === "admin" ? "blue" : "grey"} size="small">
                  {role === "admin" ? "管理员" : "招聘成员"}
                </SemiTag>
              )}
            </div>
            <SemiParagraph type="tertiary">
              管理当前工作区的收件通道、候选人资料留存和访问规则。
            </SemiParagraph>
          </SemiLayout.Header>
          <SemiLayout className="settings-body">
            <SemiLayout.Sider aria-label="设置分类" className="settings-sider">
              <SemiNavigation
                onSelect={(data) =>
                  onSelectSection(data.itemKey as WorkspaceSettingsSection)
                }
                selectedKeys={[currentSection]}
                style={{ height: "100%" }}
              >
                {items.map((item) => (
                  <SemiNavigation.Item
                    icon={<Icon name={item.icon} size={16} />}
                    itemKey={item.key}
                    key={item.key}
                    text={item.label}
                  />
                ))}
              </SemiNavigation>
            </SemiLayout.Sider>
            <SemiLayout.Content className="settings-content">
              {currentSection === "integrations" && (
                workspaceIdentityKey
                  ? <IntegrationSettingsPanel identityKey={workspaceIdentityKey} key={workspaceIdentityKey} />
                  : <SemiParagraph>无法确认当前账户与工作区，请刷新页面后重试。</SemiParagraph>
              )}
              {currentSection === "mailbox" && (
                <MailboxPage
                  embedded
                  humanizeError={formatError}
                  notify={notify}
                  onImported={onImported}
                  role={role}
                />
              )}
              {currentSection === "data" && (
                <CandidateDataLifecyclePage
                  embedded
                  formatError={formatError}
                  notify={notify}
                  onOpenLibrary={onOpenLibrary}
                />
              )}
              {currentSection === "ai-import" && (
                <AiImportSettingsPanel formatError={formatError} notify={notify} />
              )}
              {currentSection === "display-fields" && (
                <DisplayFieldsSettingsPanel formatError={formatError} notify={notify} />
              )}
              {currentSection === "filter-sections" && (
                <FilterSectionsSettingsPanel formatError={formatError} notify={notify} />
              )}
            </SemiLayout.Content>
          </SemiLayout>
        </SemiLayout>
      </Suspense>
    </div>
  );
}
