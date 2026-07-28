import {
  useEffect,
  useRef,
  useState,
} from "react";
import { Icon, type IconName } from "../../icons";
import { BackofficeButton } from "../../backoffice/ui/BackofficeButton";
import type { TrialAccess } from "../../types";
import type {
  WorkspaceNavigationView,
  WorkspaceView,
} from "./workspace-navigation-types";

export type { WorkspaceNavigationView } from "./workspace-navigation-types";

const navigation: Array<{
  view: WorkspaceNavigationView;
  label: string;
  icon: IconName;
}> = [
  { view: "library", label: "简历库", icon: "folder" },
  { view: "filter", label: "筛选工作台", icon: "filter" },
  { view: "upload", label: "上传简历", icon: "upload" },
  { view: "score", label: "评分模板", icon: "layers" },
  { view: "match", label: "招聘详情", icon: "match" },
];

function formatWholeNumber(value: number): string {
  return new Intl.NumberFormat("zh-CN", {
    maximumFractionDigits: 0,
  }).format(Math.max(0, Math.trunc(value)));
}

function accountAvatarInitials(displayName: string | null): string | null {
  const normalizedName = displayName?.trim();
  if (!normalizedName) return null;

  const hanCharacters = Array.from(normalizedName).filter((character) =>
    /[\u3400-\u9fff]/.test(character),
  );
  if (hanCharacters.length > 0) return hanCharacters.slice(0, 2).join("");

  const words = normalizedName.split(/\s+/).filter(Boolean);
  const initials = words
    .slice(0, 2)
    .map((word) => Array.from(word)[0]?.toUpperCase() ?? "")
    .join("");
  return initials || null;
}

export function TrialStatusBanner({ trial }: { trial: TrialAccess | null }) {
  if (!trial) return null;
  const isExpired = trial.plan_status === "expired" || !trial.access_enabled;
  const isTrial = trial.plan_status === "trial";
  const llmCallRemaining =
    typeof trial.llm_call_remaining === "number"
      ? Math.max(0, trial.llm_call_remaining)
      : null;
  const isQuotaExhausted = isTrial && llmCallRemaining === 0;
  if (!isExpired && !isQuotaExhausted) return null;
  return (
    <section className={`trial-banner${isExpired ? " is-expired" : " is-quota-exhausted"}`} role="alert">
      <div>
        <strong>
          {isExpired ? "试用期已结束" : "试用 AI 调用额度已用完"}
        </strong>
        <p>
          {isExpired
            ? "你的工作区数据已保留。续费入口开放前，请联系 GreatSell AI 团队继续使用。"
            : "你仍可查看和管理已有数据。继续使用 AI 功能请联系 GreatSell AI 团队。"}
        </p>
      </div>
      <span>{isExpired ? "数据已保留" : "AI 调用已暂停"}</span>
    </section>
  );
}

export function SideRail({
  activeView,
  canManageSettings,
  onChangeView,
  onOpenSettings,
  inert,
}: {
  activeView: WorkspaceView;
  canManageSettings: boolean;
  onChangeView: (view: WorkspaceNavigationView) => void;
  onOpenSettings: () => void;
  inert: boolean;
}) {
  return (
    <aside aria-label="主导航" className="side-rail" inert={inert}>
      <div className="rail-mark">
        <img
          alt="大卖数智"
          className="rail-brand-logo"
          src="/brand/greatsell-logo-cn-white.png"
        />
        <p className="rail-brand-tagline">让每一次招聘决策，都拥有 AI 驱动的判断能力。</p>
        <img
          alt="大卖数智"
          className="rail-brand-symbol"
          src="/brand/greatsell-logo-symbol-red.png"
        />
      </div>
      <nav className="rail-nav">
        {navigation.map((item) => (
            <button
              aria-current={activeView === item.view ? "page" : undefined}
              aria-label={item.label}
              className={`rail-item${activeView === item.view ? " is-active" : ""}`}
              key={item.view}
              onClick={() => onChangeView(item.view)}
              type="button"
            >
              <Icon name={item.icon} size={19} />
              <span className="rail-label">{item.label}</span>
              <span className="rail-tooltip">{item.label}</span>
            </button>
          ))}
      </nav>
      <div className="rail-bottom">
        {canManageSettings && (
          <button
            aria-current={activeView === "settings" ? "page" : undefined}
            aria-label="设置"
            className={`rail-item${activeView === "settings" ? " is-active" : ""}`}
            onClick={onOpenSettings}
            type="button"
          >
            <Icon name="gear" size={18} />
            <span className="rail-label">设置</span>
            <span className="rail-tooltip">设置</span>
          </button>
        )}
      </div>
    </aside>
  );
}

export function Topbar({
  onOpenAgent,
  canManageSettings,
  onAccountMenuOpen,
  onLogout,
  onNewUpload,
  onOpenSettings,
  organizationName,
  platformAdmin,
  platformAdminHref,
  planName,
  role,
  trial,
  userDisplayName,
  userEmail,
}: {
  onOpenAgent: () => void;
  canManageSettings: boolean;
  onAccountMenuOpen: () => void;
  onLogout: () => void;
  onNewUpload: () => void;
  onOpenSettings: () => void;
  organizationName: string | null;
  platformAdmin: boolean;
  platformAdminHref: string;
  planName: string | null;
  role: "admin" | "recruiter" | null;
  trial: TrialAccess | null;
  userDisplayName: string | null;
  userEmail: string | null;
}) {
  const trialDays = trial?.trial_days_remaining;
  const roleLabel = role === "admin" ? "管理员" : role === "recruiter" ? "招聘官" : null;
  const trialLabel =
    trial?.plan_status === "trial" && typeof trialDays === "number"
      ? `试用 ${Math.max(0, trialDays)} 天`
      : trial?.plan_status === "expired"
        ? "试用已到期"
        : null;
  const trialLlmCallRemaining =
    trial?.plan_status === "trial" && typeof trial.llm_call_remaining === "number"
      ? Math.max(0, trial.llm_call_remaining)
      : null;
  return (
    <header className="topbar">
      <div className="topbar-title-wrap">
        <p className="topbar-title">
          AI 简历筛选 <span>/ 工作台</span>
        </p>
        {(organizationName || roleLabel || planName) && (
          <p className="topbar-workspace" title={organizationName ?? undefined}>
            <span>{organizationName || "我的工作区"}</span>
            {roleLabel && <small>{roleLabel}</small>}
            {planName && <small>{planName}</small>}
          </p>
        )}
      </div>
      <div className="topbar-actions">
        {trialLabel && <span className={`topbar-trial${trial?.plan_status === "expired" ? " is-expired" : ""}`}>{trialLabel}</span>}
        <BackofficeButton
          ariaLabel="招聘助手"
          className="backoffice-agent-button"
          icon={<Icon name="spark" size={16} />}
          id="recruiting-agent-trigger"
          onClick={onOpenAgent}
          tone="primary"
        >
          <span className="topbar-action-label">招聘助手</span>
        </BackofficeButton>
        <BackofficeButton
          aria-label="上传简历"
          icon={<Icon name="upload" size={16} />}
          onClick={onNewUpload}
        >
          <span className="topbar-action-label">上传简历</span>
        </BackofficeButton>
        <AccountMenu
          canManageSettings={canManageSettings}
          onOpen={onAccountMenuOpen}
          onOpenSettings={onOpenSettings}
          onLogout={onLogout}
          organizationName={organizationName}
          platformAdmin={platformAdmin}
          platformAdminHref={platformAdminHref}
          planName={planName}
          role={role}
          trial={trial}
          trialLlmCallRemaining={trialLlmCallRemaining}
          userDisplayName={userDisplayName}
          userEmail={userEmail}
        />
      </div>
    </header>
  );
}

function AccountMenu({
  canManageSettings,
  onOpen,
  onOpenSettings,
  onLogout,
  organizationName,
  platformAdmin,
  platformAdminHref,
  planName,
  role,
  trial,
  trialLlmCallRemaining,
  userDisplayName,
  userEmail,
}: {
  canManageSettings: boolean;
  onOpen: () => void;
  onOpenSettings: () => void;
  onLogout: () => void;
  organizationName: string | null;
  platformAdmin: boolean;
  platformAdminHref: string;
  planName: string | null;
  role: "admin" | "recruiter" | null;
  trial: TrialAccess | null;
  trialLlmCallRemaining: number | null;
  userDisplayName: string | null;
  userEmail: string | null;
}) {
  const [isOpen, setIsOpen] = useState(false);
  const [isPinned, setIsPinned] = useState(false);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const hoverCloseTimerRef = useRef<number | null>(null);
  const avatarInitials = accountAvatarInitials(userDisplayName);
  const roleLabel = role === "admin" ? "管理员" : role === "recruiter" ? "招聘官" : null;
  const displayName = userDisplayName?.trim() || "当前用户";
  const llmCallLimit =
    typeof trial?.llm_call_limit === "number" ? Math.max(0, trial.llm_call_limit) : null;
  const llmCallUsed =
    typeof trial?.llm_call_used === "number" ? Math.max(0, trial.llm_call_used) : null;
  const hasLlmCallUsage =
    llmCallLimit !== null &&
    llmCallUsed !== null &&
    trialLlmCallRemaining !== null;
  const trialDays =
    trial?.plan_status === "trial" && typeof trial.trial_days_remaining === "number"
      ? Math.max(0, trial.trial_days_remaining)
      : null;
  const triggerLabel = `账户菜单：${displayName}`;

  const cancelHoverClose = () => {
    if (hoverCloseTimerRef.current === null) return;
    window.clearTimeout(hoverCloseTimerRef.current);
    hoverCloseTimerRef.current = null;
  };

  const closeMenu = (restoreFocus = false) => {
    cancelHoverClose();
    setIsOpen(false);
    setIsPinned(false);
    if (restoreFocus) {
      window.requestAnimationFrame(() => triggerRef.current?.focus());
    }
  };

  const openMenu = () => {
    cancelHoverClose();
    if (!isOpen) onOpen();
    setIsOpen(true);
  };

  const scheduleHoverClose = () => {
    if (isPinned) return;
    cancelHoverClose();
    hoverCloseTimerRef.current = window.setTimeout(() => {
      hoverCloseTimerRef.current = null;
      setIsOpen(false);
      setIsPinned(false);
    }, 180);
  };

  useEffect(() => {
    if (!isOpen) return;

    const closeFromOutside = (event: PointerEvent) => {
      const target = event.target;
      if (target instanceof Node && rootRef.current?.contains(target)) return;
      cancelHoverClose();
      setIsOpen(false);
      setIsPinned(false);
    };
    const closeFromEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      cancelHoverClose();
      setIsOpen(false);
      setIsPinned(false);
      window.requestAnimationFrame(() => triggerRef.current?.focus());
    };

    document.addEventListener("pointerdown", closeFromOutside);
    document.addEventListener("keydown", closeFromEscape);
    return () => {
      document.removeEventListener("pointerdown", closeFromOutside);
      document.removeEventListener("keydown", closeFromEscape);
    };
  }, [isOpen]);

  useEffect(() => () => cancelHoverClose(), []);

  const toggleMenu = () => {
    if (!isOpen) {
      onOpen();
      setIsOpen(true);
      setIsPinned(true);
      return;
    }
    if (isPinned) {
      closeMenu(true);
      return;
    }
    setIsPinned(true);
  };

  return (
    <div
      className="account-menu"
      onBlur={(event) => {
        const nextFocused = event.relatedTarget;
        if (nextFocused instanceof Node && rootRef.current?.contains(nextFocused)) return;
        cancelHoverClose();
        setIsOpen(false);
        setIsPinned(false);
      }}
      onMouseEnter={openMenu}
      onMouseLeave={scheduleHoverClose}
      ref={rootRef}
    >
      <button
        aria-controls="account-menu-popover"
        aria-expanded={isOpen}
        aria-haspopup="dialog"
        aria-label={triggerLabel}
        className="account-menu-trigger"
        onClick={toggleMenu}
        ref={triggerRef}
        type="button"
      >
        <span className={`account-avatar${avatarInitials ? "" : " is-icon"}`} aria-hidden="true">
          {avatarInitials ?? <Icon name="user" size={17} />}
        </span>
        <Icon className="account-menu-chevron" name="chevron-down" size={15} />
      </button>
      {isOpen && (
        <section
          aria-label="账户菜单"
          className="account-menu-popover"
          id="account-menu-popover"
          role="dialog"
        >
          <div className="account-menu-profile">
            <span className={`account-avatar account-menu-profile-avatar${avatarInitials ? "" : " is-icon"}`} aria-hidden="true">
              {avatarInitials ?? <Icon name="user" size={18} />}
            </span>
            <div>
              <strong>{displayName}</strong>
              {userEmail && <p>{userEmail}</p>}
            </div>
          </div>
          {(organizationName || roleLabel || planName) && (
            <p className="account-menu-context">
              {[roleLabel, organizationName, planName].filter(Boolean).join(" · ")}
            </p>
          )}
          {trial?.plan_status === "trial" && (
            <section className="account-menu-allowance" aria-label="试用状态">
              <span>试用状态</span>
              {hasLlmCallUsage ? (
                <strong>
                  AI 调用已用 {formatWholeNumber(llmCallUsed)} / {formatWholeNumber(llmCallLimit)}，剩余 {formatWholeNumber(trialLlmCallRemaining)} 次
                </strong>
              ) : (
                <strong>AI 调用额度正在同步</strong>
              )}
              {trialDays !== null && <small>试用还剩 {trialDays} 天</small>}
            </section>
          )}
          {trial?.plan_status === "expired" && (
            <section className="account-menu-allowance is-expired" aria-label="试用状态">
              <span>试用状态</span>
              <strong>试用已到期</strong>
            </section>
          )}
          <div className="account-menu-actions">
            {canManageSettings && (
              <button
                className="account-menu-action"
                onClick={() => {
                  closeMenu();
                  onOpenSettings();
                }}
                type="button"
              >
                <Icon name="gear" size={16} />
                工作区设置
              </button>
            )}
            {platformAdmin && (
              <a className="account-menu-action" href={platformAdminHref} onClick={() => closeMenu()}>
                <Icon name="layers" size={16} />
                平台管理
              </a>
            )}
            <button
              className="account-menu-action is-danger"
              onClick={() => {
                closeMenu();
                onLogout();
              }}
              type="button"
            >
              <Icon name="arrow-right" size={16} />
              退出登录
            </button>
          </div>
        </section>
      )}
    </div>
  );
}
