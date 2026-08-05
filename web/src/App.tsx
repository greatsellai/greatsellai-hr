import {
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { isApiError } from "./api";
import {
  resumeExtractionUserMessage,
  SERVICE_UNAVAILABLE_RETRY_MESSAGE,
} from "./resume-extraction-user-messages";
import {
  LandingPage,
  ROOT_WORKSPACE_BASE_PATH,
} from "./landing";
import type {
  CandidateSearchRequest,
  CandidateSearchItem,
  JobMatch,
  ResumeLibraryItem,
  RecruitingAgentCandidate,
  RecruitingAgentFilterScopeRequest,
} from "./types";
import { mailboxImportErrorMessages } from "./features/mailbox/mailbox-model";
import { CandidateDrawer } from "./features/candidate-drawer/CandidateDrawer";
import { useCandidateSearchController } from "./features/filter/useCandidateSearchController";
import {
  SideRail,
  Topbar,
  TrialStatusBanner,
} from "./features/workspace-shell/WorkspaceChrome";
import { useWorkspaceNavigation } from "./features/workspace-shell/useWorkspaceNavigation";
import { WorkspaceViewRouter } from "./features/workspace-shell/WorkspaceViewRouter";
import { ToastRegion } from "./features/workspace-shell/WorkspaceFeedback";
import { useWorkspaceAuth } from "./features/auth/useWorkspaceAuth";
import { useCandidateDrawerController } from "./features/candidate-drawer/useCandidateDrawerController";
import {
  EmailVerificationPage,
  ForgotPasswordPage,
  LoginPage,
  RegistrationPage,
  ResetPasswordPage,
} from "./features/auth/AuthPages";
import type {
  CandidateDrawerTab as DrawerTab,
} from "./features/candidate-drawer/candidate-drawer-types";

const AdminApp = lazy(() => import("./admin/AdminApp"));
const BackofficeUiProvider = lazy(() =>
  import("./backoffice/BackofficeUiProvider").then(({ BackofficeUiProvider: Provider }) => ({
    default: Provider,
  })),
);

type ToastKind = "success" | "error";
type AuthRoute = "login" | "register" | "forgot-password" | "reset-password" | "verify-email";
type AppSurface =
  | { kind: "landing" }
  | { kind: "platform" }
  | { kind: "workspace"; authRoute: AuthRoute | null };

interface ToastMessage {
  id: number;
  kind: ToastKind;
  message: string;
}

function humanizeError(error: unknown): string {
  if (isApiError(error)) {
    const resumeExtractionMessage = resumeExtractionUserMessage(
      error.message,
      error.status,
    );
    if (resumeExtractionMessage) return resumeExtractionMessage;
    const messages: Record<string, string> = {
      invalid_login_credentials: "邮箱或密码不正确，请重试。",
      email_already_registered: "该邮箱已注册，请直接登录或找回密码。",
      email_delivery_not_configured: "注册邮件服务正在配置中，请稍后再试。",
      registration_rate_limit_exceeded: "当前注册请求较多，请稍后再试。",
      email_verification_required: "请先完成工作邮箱验证后再进入工作台。",
      email_verification_invalid_or_expired: "这条验证链接无效或已过期，请重新发送验证邮件。",
      email_verification_account_mismatch: "当前浏览器已登录其他工作区，请先退出后再打开验证链接。",
      email_verification_resend_too_soon: "验证邮件刚刚发送，请稍候一分钟后再试。",
      email_verification_resend_limit_reached: "今天的验证邮件发送次数已达到上限，请明天再试。",
      invalid_registration_input: "请检查企业名称、姓名、邮箱和密码后重试。",
      password_too_short: "密码至少需要 8 个字符。",
      password_reset_invalid_or_expired: "这条重置链接无效、已过期或已被使用。请重新申请新的链接。",
      trial_expired: "试用期已结束。数据已保留，请联系 GreatSell AI 团队继续使用。",
      trial_llm_call_quota_exhausted:
        "本工作区的 1,000 次试用大模型调用已用完。数据仍会保留，请联系 GreatSell AI 团队继续使用。",
      organization_access_suspended: "当前工作区暂不可用，请联系 GreatSell AI 团队。",
      deepseek_api_key_not_configured:
        "AI 服务尚未配置。请先在服务器环境变量中配置后重试。",
      talent_search_profile_not_found: "这份人才画像已不存在或无法访问。",
      talent_search_run_not_found: "这次找人记录已不存在或无法访问。",
      talent_search_profile_not_draft: "这份画像已不是可确认草案，请刷新后查看当前版本。",
      talent_search_profile_not_confirmed: "请先确认你当前看到的人才画像，再开始找人。",
      talent_search_profile_revision_not_current:
        "人才画像已在其他位置更新。请刷新并确认最新草案后再操作。",
      talent_search_profile_revision_superseded:
        "这版画像已被新的草案替代，请查看最新版本。",
      talent_search_profile_revision_missing: "人才画像版本不完整，请重新生成一版草案。",
      talent_search_profile_invalid_cursor: "候选人列表位置已失效，请点击刷新重新查看。",
      talent_search_profile_match_target_invalid:
        "这份画像的核验配置异常，请重新生成并确认后再找人。",
      talent_search_profile_provider_failed:
        "AI 人才画像暂时不可用，请稍后重新生成。",
      talent_search_profile_response_truncated:
        "本次需求较长，AI 未能完整生成画像。请精简后重试。",
      talent_search_profile_service_unavailable:
        "人才画像服务暂时不可用，请稍后重试。",
      completed_resume_cannot_be_reextracted:
        "这份简历已启用，不能被后台 AI 任务覆盖。",
      resume_must_be_active_and_ready_for_source_reparse:
        "当前版本尚未准备完成，暂时不能创建新的解析版本。",
      source_resume_ai_extraction_already_running:
        "当前版本仍在 AI 提取中，请完成后再创建新的解析版本。",
      source_resume_reparse_already_running:
        "这份简历已经在创建新的解析版本，请稍后刷新。",
      resume_original_hash_mismatch:
        "原始文件校验未通过，暂时不能重新解析。请重新上传原件。",
      resume_original_file_not_found:
        "找不到这份简历的原始文件。请重新上传该文件。",
      content_type_not_supported: "仅支持 PDF、Word、图片、Excel 和 HTML 简历文件。",
      unsupported_document_type: "仅支持 PDF、Word、图片、Excel 和 HTML 简历文件。",
      file_too_large: "简历文件过大，请压缩后再上传。",
      empty_upload: "这份简历是空文件。请重新选择原始文件后上传。",
      not_a_pdf: "文件内容不是有效的 PDF。请重新导出后上传。",
      database_conflict: "该简历与正在处理的请求冲突。请稍后重试。",
      invalid_idempotency_key: "上传请求标识无效。请重新选择该简历后重试。",
      idempotency_key_reused_with_different_pdf:
        "该简历的重试标识已被其他文件使用。请重新选择文件后上传。",
      workspace_feedback_cooldown:
        "当前工作区刚提交过问卷，请在页面提示的时间后再提交。",
      workspace_feedback_idempotency_key_required:
        "问卷提交标识无效，请刷新页面后重试。",
      invalid_workspace_feedback_idempotency_key:
        "问卷提交标识无效，请刷新页面后重试。",
      workspace_feedback_idempotency_key_reused:
        "这次问卷提交已被其他内容使用，请刷新后重新填写。",
      workspace_feedback_use_case_required:
        "请完整填写四个问卷问题后再提交。",
      workspace_feedback_intended_outcome_required:
        "请完整填写四个问卷问题后再提交。",
      workspace_feedback_friction_required:
        "请完整填写四个问卷问题后再提交。",
      workspace_feedback_desired_change_required:
        "请完整填写四个问卷问题后再提交。",
      workspace_feedback_contact_phone_required: "请填写联系电话后再提交。",
      workspace_feedback_contact_phone_invalid: "请填写有效的联系电话。",
      workspace_feedback_answer_too_long:
        "单项回答请控制在 4,000 个字符以内。",
      workspace_feedback_too_many_attachments:
        "最多可附加 5 张图片。",
      workspace_feedback_attachment_type_invalid:
        "截图仅支持 PNG、JPG 或 WebP 图片。",
      workspace_feedback_attachment_too_large:
        "单张截图不能超过 10 MB。",
      workspace_feedback_attachment_size_invalid:
        "单张截图不能超过 10 MB。",
      resume_not_found: "这份简历已不存在或无法访问。",
      candidate_not_found: "这位候选人已不存在或无法访问。",
      candidate_favorite_update_conflict: "收藏状态刚刚发生变化，请刷新后重试。",
      mailbox_not_configured: "请先保存邮箱配置。",
      mailbox_config_not_found: "这个收件通道已不存在或无法访问。",
      mailbox_display_name_required: "请为这个收件通道填写名称。",
      mailbox_duplicate_display_name: "该收件通道名称已被使用，请换一个名称。",
      mailbox_source_identity_locked:
        "该通道已有附件记录，不能改为其他邮箱。请新建一个收件通道。",
      mailbox_legacy_endpoint_ambiguous: "当前工作区已有多个收件通道，请刷新页面后重试。",
      mailbox_sync_in_progress: "这个收件通道正在同步，请稍后刷新查看结果。",
      mailbox_sync_claim_failed: "这个收件通道暂时无法开始同步，请稍后重试。",
      mailbox_config_archived: "这个收件通道已归档，不能继续同步。",
      mailbox_source_epoch_changed:
        "邮箱来源标识已变化，系统已暂停该通道。请归档后新建收件通道。",
      mailbox_password_required: "首次配置需要填写邮箱授权码。",
      mailbox_provider_required: "请先选择一个已启用的邮箱服务商。",
      mailbox_provider_not_supported: "该邮箱服务商当前不受支持，请选择列表中的服务商。",
      mailbox_provider_not_available: "该邮箱服务商尚未在当前部署启用，请联系部署管理员。",
      mailbox_provider_endpoint_mismatch: "该服务商的固定连接配置不匹配，请重新选择服务商。",
      mailbox_provider_change_requires_new_connection:
        "服务商不能在原通道中切换，请新建一个收件通道。",
      mailbox_provider_oauth_not_supported: "这个收件通道不支持网页授权。",
      mailbox_provider_authentication_transition_requires_oauth:
        "这个通道的授权方式不能直接切换，请新建一个收件通道。",
      mailbox_oauth_connection_required: "请先完成邮箱网页授权后再同步。",
      mailbox_oauth_not_configured: "该邮箱服务商尚未在当前部署启用，请联系部署管理员。",
      mailbox_oauth_callback_origin_invalid:
        "当前访问入口无法完成授权回调，请从正式招聘工作台重新发起授权。",
      mailbox_oauth_reauthorization_required:
        "邮箱授权已失效，请重新授权。历史入库记录不会受影响。",
      mailbox_oauth_token_exchange_failed: "邮箱授权服务暂时不可用，请稍后重新授权。",
      mailbox_credentials_key_not_configured: "邮箱凭据服务尚未配置，请联系部署管理员。",
      mailbox_credentials_unavailable: "邮箱授权码无法读取，请重新保存后再同步。",
      mailbox_connection_failed: "无法连接邮箱，请检查 IMAP 地址、端口和授权码。",
      mailbox_select_failed: "无法打开收件箱，请检查邮箱服务商和授权状态。",
      mailbox_status_failed: "无法读取收件箱状态，请检查邮箱服务商和授权状态后重试。",
      mailbox_search_failed: "无法检索邮箱中的附件。",
      mailbox_sync_failed: "邮箱入库暂时异常，请稍后重试。",
      mailbox_retention_policy_invalid: "内容保留策略无效，请重新选择后保存。",
      mailbox_retention_run_not_found: "这条清理记录已不存在或无法访问。",
      organization_admin_required: "仅工作区管理员可以修改保留策略或执行清理。",
      candidate_data_file_access_purpose_invalid: "原件访问方式无效，请重新发起查看或下载。",
      candidate_data_file_access_not_found: "这次原件访问已失效，请重新发起查看或下载。",
      candidate_data_session_nonce_missing: "当前登录会话已更新，请刷新页面后重新操作。",
      candidate_data_deletion_batch_not_found: "这条删除记录已不存在或无法访问。",
      candidate_data_deletion_batch_not_restorable: "这条删除记录当前不能恢复。",
      candidate_data_recovery_window_closed: "恢复期限已结束，原始数据已进入清理流程。",
      candidate_data_retention_policy_invalid: "保留策略参数无效，请检查后重试。",
      candidate_data_retention_preview_stale: "预览结果已过期，请重新预览后再保存策略。",
      candidate_data_export_not_found: "这项导出任务已不存在或无法访问。",
      candidate_data_export_not_cancellable: "这项导出任务当前不能取消。",
      candidate_data_export_download_not_found: "导出文件不可用或已过期，请重新创建导出。",
      candidate_data_export_candidate_selection_invalid: "请选择一位或多位可访问的候选人后再导出。",
      candidate_data_export_snapshot_unavailable: "导出所需的候选人快照已不可用，请重新创建导出。",
      candidate_data_export_original_unavailable: "部分原始文件不可用，无法创建包含原件的导出。",
      candidate_data_export_original_bytes_exceeded: "原始文件总量超过本次导出上限，请改为不含原件导出。",
      sensitive_candidate_keyword_not_supported:
        "匹配关键词不能包含年龄、性别或其他受保护个人属性。请改为岗位技能、经历或项目关键词。",
      ...mailboxImportErrorMessages,
      score_template_not_found: "评分模板不存在，请重新选择。",
      resume_score_batch_not_found: "评分任务不存在或已不可访问。",
      job_version_not_found: "岗位版本不存在，请重新创建。",
      jd_generation_response_truncated:
        "岗位需求较长，AI 未能完整生成 JD。请精简后重试。",
      jd_generation_provider_failed:
        "AI 生成 JD 暂时不可用，请稍后重试。",
      jd_generation_service_unavailable:
        "AI 生成 JD 服务暂时不可用，请稍后重试。",
      jd_requirements_response_truncated:
        "JD 较长，AI 未能完整整理匹配条件。请稍后重试。",
      jd_requirements_provider_failed:
        "AI 整理 JD 条件暂时不可用，请稍后重试。",
    };
    const message = messages[error.message];
    if (message) return message;
    if (error.status >= 500) return SERVICE_UNAVAILABLE_RETRY_MESSAGE;
    return `操作没有完成：${error.message}`;
  }
  return "操作没有完成。请检查网络后重试。";
}

function isLocalDevelopmentHost(hostname: string) {
  return hostname === "localhost" || hostname === "127.0.0.1" || hostname === "::1";
}

function isRootMarketingHost(hostname: string) {
  return hostname === "greatsellai.net";
}

const CANONICAL_HR_ORIGIN = "https://hr.greatsellai.cn";

function isHrApplicationHost(hostname: string) {
  return (
    hostname === "hr.greatsellai.cn" ||
    hostname === "hr.greatsellai.net" ||
    hostname === "staging.hr.greatsellai.net"
  );
}

const HR_WORKSPACE_BASE_PATH = "/workspace";

function workspaceHref(path = "") {
  const normalizedPath = path && !path.startsWith("/") ? `/${path}` : path;
  const { hostname, pathname } = window.location;
  const isCompatibilityWorkspace =
    pathname === ROOT_WORKSPACE_BASE_PATH ||
    pathname.startsWith(`${ROOT_WORKSPACE_BASE_PATH}/`);

  if (isCompatibilityWorkspace) {
    return `${ROOT_WORKSPACE_BASE_PATH}${normalizedPath}` || ROOT_WORKSPACE_BASE_PATH;
  }

  // The public root site is intentionally a separate marketing surface. Its
  // calls to action always cross to the dedicated HR application origin, so
  // the root domain never needs to expose the authenticated HR API.
  if (isHrApplicationHost(hostname)) {
    if (["/login", "/register", "/forgot-password", "/reset-password"].includes(normalizedPath)) {
      return normalizedPath;
    }
    return `${HR_WORKSPACE_BASE_PATH}${normalizedPath}` || HR_WORKSPACE_BASE_PATH;
  }

  if (isRootMarketingHost(hostname)) {
    return `${CANONICAL_HR_ORIGIN}${normalizedPath || "/"}`;
  }

  return normalizedPath || "/";
}

function platformHref() {
  const { pathname } = window.location;
  return pathname === ROOT_WORKSPACE_BASE_PATH ||
    pathname.startsWith(`${ROOT_WORKSPACE_BASE_PATH}/`)
    ? `${ROOT_WORKSPACE_BASE_PATH}/platform`
    : "/platform";
}

function authRouteFromPath(pathname: string): AuthRoute | null {
  const normalized = pathname.replace(/\/+$/, "") || "/";
  if (normalized === "/login") return "login";
  if (normalized === "/register") return "register";
  if (normalized === "/forgot-password") return "forgot-password";
  if (normalized === "/reset-password") return "reset-password";
  if (normalized === "/verify-email") return "verify-email";
  return null;
}

function resolveAppSurface(): AppSurface {
  const { hostname, pathname } = window.location;
  const compatibilityPlatformPath = `${ROOT_WORKSPACE_BASE_PATH}/platform`;
  const isPrimaryPlatformPath =
    pathname === "/platform" || pathname.startsWith("/platform/");
  const isCompatibilityPlatformPath =
    pathname === compatibilityPlatformPath ||
    pathname.startsWith(`${compatibilityPlatformPath}/`);
  if (
    isCompatibilityPlatformPath ||
    (
      isPrimaryPlatformPath &&
      (isHrApplicationHost(hostname) || isLocalDevelopmentHost(hostname))
    )
  ) {
    return { kind: "platform" };
  }
  const isWorkspacePath =
    pathname === ROOT_WORKSPACE_BASE_PATH ||
    pathname.startsWith(`${ROOT_WORKSPACE_BASE_PATH}/`);

  if (isWorkspacePath) {
    const nestedPath = pathname.slice(ROOT_WORKSPACE_BASE_PATH.length).replace(/\/+$/, "") || "/";
    return { kind: "workspace", authRoute: authRouteFromPath(nestedPath) };
  }

  if (isHrApplicationHost(hostname)) {
    if (pathname === "/" || pathname === "") return { kind: "landing" };
    const nestedPath = pathname.startsWith(HR_WORKSPACE_BASE_PATH)
      ? pathname.slice(HR_WORKSPACE_BASE_PATH.length)
      : pathname;
    return { kind: "workspace", authRoute: authRouteFromPath(nestedPath) };
  }

  if (isLocalDevelopmentHost(hostname)) {
    return { kind: "workspace", authRoute: authRouteFromPath(pathname) };
  }

  return { kind: "landing" };
}

function ExternalRedirect({ href }: { href: string }) {
  useEffect(() => {
    window.location.replace(href);
  }, [href]);

  return (
    <main className="login-page" aria-live="polite">
      <div className="login-panel login-redirect-panel">
        <i className="spinner" /> 正在前往登录系统…
      </div>
    </main>
  );
}

function App() {
  const [surface, setSurface] = useState<AppSurface>(resolveAppSurface);

  useEffect(() => {
    const syncSurface = () => setSurface(resolveAppSurface());
    window.addEventListener("popstate", syncSurface);
    return () => window.removeEventListener("popstate", syncSurface);
  }, []);

  if (surface.kind === "landing") {
    return (
      <LandingPage
        loginHref={workspaceHref("/login")}
        registerHref={workspaceHref("/register")}
      />
    );
  }

  if (surface.kind === "platform") {
    return (
      <Suspense
        fallback={(
          <main className="login-page" aria-live="polite">
            <div className="login-panel login-redirect-panel">
              <i className="spinner" /> 正在打开平台管理…
            </div>
          </main>
        )}
      >
        <AdminApp />
      </Suspense>
    );
  }

  return (
    <Suspense
      fallback={(
        <main className="login-page" aria-live="polite">
          <div className="login-panel login-redirect-panel">
            <i className="spinner" /> 正在打开招聘工作台…
          </div>
        </main>
      )}
    >
      <BackofficeUiProvider>
        <WorkspaceApp authRoute={surface.authRoute} />
      </BackofficeUiProvider>
    </Suspense>
  );
}

function WorkspaceApp({ authRoute }: { authRoute: AuthRoute | null }) {
  const [pendingAgentFilterScope, setPendingAgentFilterScope] =
    useState<RecruitingAgentFilterScopeRequest | null>(null);
  const agentScopeRequestIdRef = useRef(0);
  const [libraryRefreshToken, setLibraryRefreshToken] = useState(0);
  const [favoriteRefreshToken, setFavoriteRefreshToken] = useState(0);
  const [toasts, setToasts] = useState<ToastMessage[]>([]);

  const notify = useCallback((kind: ToastKind, message: string) => {
    const id = Date.now() + Math.round(Math.random() * 1000);
    setToasts((current) => [...current, { id, kind, message }]);
    window.setTimeout(() => {
      setToasts((current) => current.filter((toast) => toast.id !== id));
    }, 5200);
  }, []);

  const refreshLibraryScores = useCallback(() => {
    setLibraryRefreshToken((current) => current + 1);
  }, []);

  // Favorites appear in the drawer, library, and existing filter results.
  // The drawer is initialized before the filter hook, so a token lets the
  // later effect refresh that result set without violating hook ordering.
  const refreshCandidateFavorites = useCallback(() => {
    refreshLibraryScores();
    setFavoriteRefreshToken((current) => current + 1);
  }, [refreshLibraryScores]);

  const {
    candidateDrawerProps,
    closeDrawer,
    isOpen: drawerOpen,
    openResume,
    resetDrawer,
    selectedResumeId,
  } = useCandidateDrawerController({
    formatError: humanizeError,
    notify,
    onLibraryChanged: refreshLibraryScores,
    onFavoriteChanged: refreshCandidateFavorites,
  });

  const clearWorkspaceAfterLogout = useCallback(() => {
    resetDrawer();
    setPendingAgentFilterScope(null);
  }, [resetDrawer]);
  const {
    authError,
    authLoading,
    authSession,
    authState,
    completeEmailVerification,
    completePasswordReset,
    login,
    logout,
    refreshAuthSession,
    register,
    requestPasswordReset,
    resendEmailVerification,
    switchWorkspace,
    workspaceMemberships,
  } = useWorkspaceAuth({
    authRoute,
    formatError: humanizeError,
    onLogoutCleanup: clearWorkspaceAfterLogout,
    rootWorkspaceBasePath: ROOT_WORKSPACE_BASE_PATH,
    workspaceHref,
  });

  const {
    appliedFilter,
    changeScoreTemplate,
    filterDraft,
    filterOptions,
    loadMore,
    refreshCurrentResults,
    registerScoreTemplate,
    resetFilter,
    scoreTemplateId,
    scoreTemplates,
    search,
    searching,
    updateFilterDraft,
  } = useCandidateSearchController({
    enabled:
      authState === "authenticated" &&
      !authRoute &&
      !authSession?.email_verification_required,
    formatError: humanizeError,
    notify,
  });

  const handleScoreCreated = useCallback(() => {
    refreshLibraryScores();
    refreshCurrentResults();
  }, [refreshCurrentResults, refreshLibraryScores]);

  useEffect(() => {
    if (favoriteRefreshToken === 0) return;
    refreshCurrentResults();
  }, [favoriteRefreshToken, refreshCurrentResults]);

  const canManageMailbox =
    authSession?.role === "admin" &&
    authSession.plan?.feature_flags.mailbox_import === true;
  const canManageCandidateData = authSession?.role === "admin";
  const {
    canManageSettings,
    navigateToView,
    openFeedback,
    openSettings,
    routeParams,
    settingsSection,
    view,
  } = useWorkspaceNavigation({
    canManageCandidateData,
    canManageMailbox,
    hasSession: Boolean(authSession),
  });
  const canGenerateAiJd =
    authSession?.role === "admin" &&
    authSession.plan?.feature_flags.ai_jd_generation === true;

  const openRecruitingAgent = useCallback(() => {
    closeDrawer();
    navigateToView("agent");
  }, [closeDrawer, navigateToView]);

  const openAgentWithFilterScope = useCallback(
    (filter: CandidateSearchRequest, totalCount: number) => {
      closeDrawer();
      setPendingAgentFilterScope({
        filter,
        request_id: ++agentScopeRequestIdRef.current,
        total_count: totalCount,
      });
      navigateToView("agent");
    },
    [closeDrawer, navigateToView],
  );

  const completeAgentFilterScopeHandoff = useCallback((requestId: number) => {
    setPendingAgentFilterScope((current) =>
      current?.request_id === requestId ? null : current,
    );
  }, []);

  useEffect(() => {
    const titles: Record<AuthRoute, string> = {
      login: "登录大卖智聘｜大卖数智",
      register: "免费试用大卖智聘｜大卖数智",
      "forgot-password": "找回密码｜大卖智聘",
      "reset-password": "设置新密码｜大卖智聘",
      "verify-email": "验证邮箱｜大卖智聘",
    };
    document.title = authRoute
      ? titles[authRoute]
      : view === "agent"
        ? "招聘 Agent｜大卖智聘"
        : "大卖智聘｜AI 招聘决策工作台";
  }, [authRoute, view]);

  useEffect(() => {
    if (
      authState === "authenticated" &&
      authRoute &&
      authRoute !== "verify-email" &&
      !authSession?.email_verification_required
    ) {
      window.location.replace(workspaceHref());
    }
  }, [authRoute, authSession?.email_verification_required, authState]);

  useEffect(() => {
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      closeDrawer();
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [closeDrawer]);

  const openCandidate = useCallback(
    (item: CandidateSearchItem, tab: DrawerTab = "summary") => {
      openResume({
        resumeId: item.resume_id,
        candidateId: item.candidate_id,
        candidateName: item.display_name?.trim() || "未命名候选人",
      }, tab);
    },
    [openResume],
  );

  const openUploadedResume = useCallback(
    (resumeId: string, candidateId: string) => {
      openResume({
        resumeId,
        candidateId,
        candidateName: "未命名候选人",
      });
      navigateToView("library");
      refreshLibraryScores();
    },
    [navigateToView, openResume, refreshLibraryScores],
  );

  const openLibraryResume = useCallback(
    (item: ResumeLibraryItem) => {
      openResume({
        resumeId: item.resume_id,
        candidateId: item.candidate_id,
        candidateName: item.display_name?.trim() || "未命名候选人",
      });
    },
    [openResume],
  );

  const openFavoriteResume = useCallback(
    (resumeId: string, candidateId: string, candidateName: string | null) => {
      openResume({
        resumeId,
        candidateId,
        candidateName: candidateName?.trim() || "未命名候选人",
      });
    },
    [openResume],
  );

  const openMatchedResume = useCallback(
    (match: JobMatch) => {
      openResume({
        resumeId: match.resume_id,
        candidateId: match.candidate_id,
        candidateName: match.candidate_display_name?.trim() || "未命名候选人",
      });
    },
    [openResume],
  );

  const openAgentResume = useCallback(
    (item: RecruitingAgentCandidate) => {
      openResume({
        resumeId: item.resume_id,
        candidateId: item.candidate_id,
        candidateName: item.display_name?.trim() || "未命名候选人",
      });
    },
    [openResume],
  );

  if (authState === "checking") {
    return (
      <main className="login-page" aria-live="polite">
        <div className="login-panel login-redirect-panel">
          <i className="spinner" /> 正在验证登录状态…
        </div>
      </main>
    );
  }

  if (authState !== "authenticated" && !authRoute) {
    return <ExternalRedirect href={workspaceHref("/login")} />;
  }

  // A recovery link may be opened in a browser that still has an unrelated
  // workspace session.  It must remain usable instead of silently dropping
  // the recipient into that workspace.
  if (authRoute === "reset-password") {
    return (
      <ResetPasswordPage
        error={authError}
        loading={authLoading}
        onComplete={completePasswordReset}
        workspaceHref={workspaceHref}
      />
    );
  }

  if (authRoute === "verify-email" || authSession?.email_verification_required) {
    return (
      <EmailVerificationPage
        error={authError}
        loading={authLoading}
        session={authSession}
        onComplete={completeEmailVerification}
        onExit={logout}
        onRefreshSession={refreshAuthSession}
        onResend={resendEmailVerification}
        workspaceHref={workspaceHref}
      />
    );
  }

  if (authState !== "authenticated") {
    if (authRoute === "register") {
      return (
        <RegistrationPage
          error={authError}
          loading={authLoading}
          onRegister={register}
          workspaceHref={workspaceHref}
        />
      );
    }
    if (authRoute === "forgot-password") {
      return (
        <ForgotPasswordPage
          error={authError}
          loading={authLoading}
          onRequest={requestPasswordReset}
          workspaceHref={workspaceHref}
        />
      );
    }
    return (
      <LoginPage
        error={authError}
        loading={authLoading}
        onLogin={login}
        workspaceHref={workspaceHref}
      />
    );
  }

  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">跳到主要内容</a>
      <SideRail
        activeView={view}
        canManageSettings={canManageSettings}
        inert={drawerOpen}
        onChangeView={navigateToView}
        onOpenSettings={() => openSettings(canManageMailbox ? "mailbox" : "data")}
      />
      <div className="app-area" inert={drawerOpen}>
        <Topbar
          onOpenAgent={openRecruitingAgent}
          onOpenFeedback={() => {
            closeDrawer();
            openFeedback();
          }}
          canManageSettings={canManageSettings}
          onAccountMenuOpen={() => {
            void refreshAuthSession();
          }}
          onLogout={() => void logout()}
          onNewUpload={() => navigateToView("upload")}
          onOpenSettings={() => openSettings(canManageMailbox ? "mailbox" : "data")}
          onSwitchWorkspace={(membershipId) => void switchWorkspace(membershipId)}
          organizationId={authSession?.organization?.organization_id ?? null}
          organizationName={authSession?.organization?.name ?? null}
          platformAdmin={authSession?.is_platform_admin ?? false}
          platformAdminHref={platformHref()}
          planName={authSession?.plan?.name ?? null}
          role={authSession?.role ?? null}
          trial={authSession?.trial ?? null}
          userDisplayName={authSession?.user?.display_name ?? null}
          userEmail={authSession?.user?.email ?? null}
          workspaceMemberships={workspaceMemberships}
        />
        <TrialStatusBanner trial={authSession?.trial ?? null} />
        <main className="main-content" id="main-content">
          <WorkspaceViewRouter
            agent={{
              conversationStorageScope:
                authSession?.organization?.organization_id && authSession.user?.user_id
                  ? `${authSession.organization.organization_id}:${authSession.user.user_id}`
                  : null,
              pendingFilterScope: pendingAgentFilterScope,
              onPendingFilterScopeHandled: completeAgentFilterScopeHandoff,
              onOpenMatchWorkspace: () => navigateToView("match"),
              onOpenScoreWorkspace: () => navigateToView("score"),
              onOpenMailboxSettings: () => openSettings("mailbox"),
              onOpenResume: openAgentResume,
            }}
            feedback={{
              formatError: humanizeError,
              notify,
              onRewardGranted: () => {
                void refreshAuthSession();
              },
            }}
            filter={{
              appliedFilter,
              changeScoreTemplate,
              filterDraft,
              filterOptions,
              loadMore,
              resetFilter,
              scoreTemplateId,
              scoreTemplates,
              search,
              searching,
              updateFilterDraft,
            }}
            library={{
              refreshToken: libraryRefreshToken,
              selectedResumeId,
            }}
            navigation={{ navigateToView, openSettings }}
            permissions={{
              canGenerateAiJd,
              canManageCandidateData,
              canManageMailbox,
              canManageSettings,
              role: authSession?.role ?? null,
            }}
            routeParams={routeParams}
            settingsSection={settingsSection}
            view={view}
            onFavoriteChanged={refreshCandidateFavorites}
            onLibraryChanged={refreshLibraryScores}
            onOpenCandidate={openCandidate}
            onOpenFavoriteResume={openFavoriteResume}
            onOpenLibraryResume={openLibraryResume}
            onOpenMatchedResume={openMatchedResume}
            onOpenRecruitingAgent={openRecruitingAgent}
            onRefineWithAgent={openAgentWithFilterScope}
            onScoreCreated={handleScoreCreated}
            onTemplateCreated={registerScoreTemplate}
            onUploadedResume={openUploadedResume}
          />
        </main>
      </div>

      <div
        aria-hidden="true"
        className={`drawer-scrim${drawerOpen ? " is-open" : ""}`}
        onClick={closeDrawer}
      />
      <CandidateDrawer
        {...candidateDrawerProps}
        languageCredentialOptions={filterOptions.language_credentials}
        canManageCandidateData={canManageCandidateData}
      />
      <ToastRegion
        toasts={toasts}
        onDismiss={(id) =>
          setToasts((current) => current.filter((toast) => toast.id !== id))
        }
      />
    </div>
  );
}


export default App;
