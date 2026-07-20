import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
} from "react";
import { Icon, type IconName } from "../icons";
import type { AuthSession } from "../types";
import { adminApi, adminErrorMessage } from "./admin-api";
import type { AdminView } from "./admin-types";
import { AdminOverviewPage } from "./pages/AdminOverviewPage";
import { AdminOrganizationsPage } from "./pages/AdminOrganizationsPage";
import {
  AdminAiPage,
  AdminAuditPage,
  AdminPlansPage,
  AdminUsersPage,
} from "./pages/AdminSecondaryPages";
import "./admin.css";

const navigation: Array<{
  view: AdminView;
  label: string;
  description: string;
  icon: IconName;
  group: "平台" | "客户" | "运营" | "治理";
}> = [
  { view: "overview", label: "平台概览", description: "状态与待处理事项", icon: "activity", group: "平台" },
  { view: "organizations", label: "工作区", description: "套餐、试用与成员", icon: "briefcase", group: "客户" },
  { view: "users", label: "用户", description: "身份与账号状态", icon: "user", group: "客户" },
  { view: "plans", label: "套餐与试用", description: "价格与功能范围", icon: "layers", group: "运营" },
  { view: "ai", label: "AI 运营", description: "运行、用量与配置", icon: "spark", group: "运营" },
  { view: "audit", label: "操作审计", description: "平台变更记录", icon: "history", group: "治理" },
];

const platformBasePath = window.location.pathname === "/greatsellhr/platform" || window.location.pathname.startsWith("/greatsellhr/platform/")
  ? "/greatsellhr/platform"
  : "/platform";
const workspacePath = platformBasePath.startsWith("/greatsellhr") ? "/greatsellhr" : "/workspace";
const loginPath = platformBasePath.startsWith("/greatsellhr") ? "/greatsellhr/login" : "/login";
const viewPaths: Record<AdminView, string> = {
  overview: platformBasePath,
  organizations: `${platformBasePath}/organizations`,
  users: `${platformBasePath}/users`,
  plans: `${platformBasePath}/plans`,
  ai: `${platformBasePath}/ai`,
  audit: `${platformBasePath}/audit`,
};

function viewFromPath(pathname = window.location.pathname): AdminView {
  const segment = pathname.slice(platformBasePath.length).split("/").filter(Boolean)[0];
  if (segment === "organizations") return "organizations";
  if (segment === "users") return "users";
  if (segment === "plans") return "plans";
  if (segment === "ai") return "ai";
  if (segment === "audit") return "audit";
  return "overview";
}

function AdminBootState({ message }: { message: string }) {
  return (
    <main className="admin-gate" aria-live="polite">
      <div className="admin-gate-mark" aria-hidden="true" />
      <div>
        <h1>GreatSell AI 平台管理</h1>
        <p><i className="spinner" /> {message}</p>
      </div>
    </main>
  );
}

function AdminDenied({ session }: { session: AuthSession }) {
  const displayName = session.user?.display_name || session.user?.email || "当前账号";
  return (
    <main className="admin-gate admin-gate-denied">
      <div className="admin-gate-mark is-denied" aria-hidden="true">
        <Icon name="close" size={22} />
      </div>
      <div>
        <span className="admin-gate-label">平台权限</span>
        <h1>此账号不能访问平台管理</h1>
        <p>{displayName} 已登录，但没有平台管理员权限。你仍可返回自己的招聘工作区。</p>
        <div className="admin-gate-actions">
          <a className="button button-primary" href={workspacePath}>返回招聘工作台</a>
          <a className="button" href={`${loginPath}?next=${encodeURIComponent(platformBasePath)}`}>更换账号</a>
        </div>
      </div>
    </main>
  );
}

function AdminLoadError({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <main className="admin-gate" role="alert">
      <div className="admin-gate-mark is-denied" aria-hidden="true">
        <Icon name="refresh" size={22} />
      </div>
      <div>
        <h1>暂时无法验证平台权限</h1>
        <p>{message}</p>
        <button className="button button-primary" onClick={onRetry} type="button">重新验证</button>
      </div>
    </main>
  );
}

export default function AdminApp() {
  const [sessionState, setSessionState] = useState<"checking" | "ready" | "error">("checking");
  const [session, setSession] = useState<AuthSession | null>(null);
  const [sessionError, setSessionError] = useState("");
  const [view, setView] = useState<AdminView>(viewFromPath);
  const [navOpen, setNavOpen] = useState(false);
  const [mobileNavigation, setMobileNavigation] = useState(false);
  const [globalSearch, setGlobalSearch] = useState("");
  const menuButtonRef = useRef<HTMLButtonElement>(null);
  const sidebarRef = useRef<HTMLElement>(null);

  const currentNavigation = useMemo(
    () => navigation.find((item) => item.view === view) ?? navigation[0],
    [view],
  );
  const isLocalEnvironment = ["localhost", "127.0.0.1", "::1"].includes(window.location.hostname);

  const loadSession = () => {
    setSessionState("checking");
    setSessionError("");
    void adminApi.getSession()
      .then((nextSession) => {
        setSession(nextSession);
        setSessionState("ready");
        if (!nextSession.authenticated) {
          const next = encodeURIComponent(window.location.pathname + window.location.search);
          window.location.replace(`${loginPath}?next=${next}`);
        }
      })
      .catch((error) => {
        setSession(null);
        setSessionError(adminErrorMessage(error));
        setSessionState("error");
      });
  };

  useEffect(loadSession, []);

  useEffect(() => {
    const media = window.matchMedia("(max-width: 56rem)");
    const sync = () => {
      setMobileNavigation(media.matches);
      if (!media.matches) setNavOpen(false);
    };
    sync();
    media.addEventListener("change", sync);
    return () => media.removeEventListener("change", sync);
  }, []);

  useEffect(() => {
    if (!navOpen || !mobileNavigation) return;
    window.setTimeout(() => sidebarRef.current?.querySelector<HTMLButtonElement>("button")?.focus(), 0);
  }, [mobileNavigation, navOpen]);

  useEffect(() => {
    const syncView = () => setView(viewFromPath());
    window.addEventListener("popstate", syncView);
    return () => window.removeEventListener("popstate", syncView);
  }, []);

  useEffect(() => {
    document.title = `${currentNavigation.label}｜GreatSell AI 平台管理`;
  }, [currentNavigation.label]);

  useEffect(() => {
    if (!navOpen) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      setNavOpen(false);
      window.setTimeout(() => menuButtonRef.current?.focus(), 0);
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [navOpen]);

  const navigate = (nextView: AdminView) => {
    if (nextView !== view) {
      window.history.pushState(null, "", viewPaths[nextView]);
      setView(nextView);
    }
    setNavOpen(false);
  };

  const submitGlobalSearch = (event: FormEvent) => {
    event.preventDefault();
    const query = globalSearch.trim();
    const targetView: AdminView = view === "users" || query.includes("@") ? "users" : "organizations";
    const target = query
      ? `${viewPaths[targetView]}?search=${encodeURIComponent(query)}`
      : viewPaths[targetView];
    window.history.pushState(null, "", target);
    setView(targetView);
    window.dispatchEvent(new PopStateEvent("popstate"));
    setNavOpen(false);
  };

  const logout = async () => {
    try {
      await adminApi.logout();
    } finally {
      window.location.assign(`${loginPath}?next=${encodeURIComponent(platformBasePath)}`);
    }
  };

  if (sessionState === "checking") return <AdminBootState message="正在验证平台身份…" />;
  if (sessionState === "error") return <AdminLoadError message={sessionError} onRetry={loadSession} />;
  if (!session?.authenticated) return <AdminBootState message="正在前往登录…" />;
  if (!session.is_platform_admin) return <AdminDenied session={session} />;

  return (
    <div className={`admin-shell${navOpen ? " is-nav-open" : ""}`}>
      <a className="skip-link" href="#admin-main">跳到主要内容</a>
      <div
        aria-hidden="true"
        className="admin-nav-scrim"
        onClick={() => setNavOpen(false)}
      />
      <aside
        aria-hidden={mobileNavigation && !navOpen ? true : undefined}
        aria-label="平台管理导航"
        className="admin-sidebar"
        inert={mobileNavigation && !navOpen}
        ref={sidebarRef}
      >
        <div className="admin-brand">
          <span className="admin-brand-copy"><strong>GreatSell AI</strong><small>平台管理</small></span>
        </div>
        <nav className="admin-navigation">
          {(["平台", "客户", "运营", "治理"] as const).map((group) => (
            <div className="admin-nav-group" key={group}>
              <p>{group}</p>
              {navigation.filter((item) => item.group === group).map((item) => (
                <button
                  aria-current={view === item.view ? "page" : undefined}
                  aria-label={item.label}
                  className={`admin-nav-item${view === item.view ? " is-active" : ""}`}
                  key={item.view}
                  onClick={() => navigate(item.view)}
                  type="button"
                >
                  <Icon name={item.icon} size={18} />
                  <span><strong>{item.label}</strong><small>{item.description}</small></span>
                </button>
              ))}
            </div>
          ))}
        </nav>
        <div className="admin-sidebar-footer">
          <a href={workspacePath}><Icon name="arrow-left" size={17} /><span>返回招聘工作台</span></a>
        </div>
      </aside>

      <div className="admin-area" inert={navOpen}>
        <header className="admin-topbar">
          <button
            aria-expanded={navOpen}
            aria-label={navOpen ? "关闭平台导航" : "打开平台导航"}
            className="icon-button admin-menu-button"
            onClick={() => setNavOpen(true)}
            ref={menuButtonRef}
            type="button"
          >
            <Icon name="layers" size={19} />
          </button>
          <div className="admin-topbar-title">
            <span>平台管理</span>
            <strong>{currentNavigation.label}</strong>
          </div>
          <form className="admin-global-search" onSubmit={submitGlobalSearch} role="search">
            <Icon name="search" size={17} />
            <label className="sr-only" htmlFor="admin-global-search">搜索工作区或用户</label>
            <input
              id="admin-global-search"
              onChange={(event) => setGlobalSearch(event.target.value)}
              placeholder="搜索工作区、邮箱或 ID"
              value={globalSearch}
            />
          </form>
          <span className={`admin-environment${isLocalEnvironment ? " is-local" : ""}`}><i />{isLocalEnvironment ? "本地环境" : "生产环境"}</span>
          <div className="admin-identity">
            <span>{session.user?.display_name || session.user?.email}</span>
            <small>平台管理员</small>
          </div>
          <button className="button button-ghost admin-logout" onClick={() => void logout()} type="button">退出</button>
        </header>
        <main className="admin-main" id="admin-main">
          {view === "overview" ? (
            <AdminOverviewPage onNavigate={navigate} />
          ) : view === "organizations" ? (
            <AdminOrganizationsPage />
          ) : view === "users" ? (
            <AdminUsersPage />
          ) : view === "plans" ? (
            <AdminPlansPage />
          ) : view === "ai" ? (
            <AdminAiPage />
          ) : (
            <AdminAuditPage />
          )}
        </main>
      </div>
    </div>
  );
}
