import {
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { api } from "../../api";
import { Icon } from "../../icons";
import type {
  AuthLoginInput,
  AuthRegistrationInput,
  AuthSession,
  RegistrationOffer,
} from "../../types";

type WorkspaceHref = (path?: string) => string;

const fallbackRegistrationOffer: RegistrationOffer = {
  plan_code: "advanced",
  plan_name: "进阶版",
  trial_days: 30,
  llm_call_limit: 1000,
};

const wholeNumberFormatter = new Intl.NumberFormat("zh-CN", {
  maximumFractionDigits: 0,
});

function formatWholeNumber(value: number): string {
  return wholeNumberFormatter.format(Math.max(0, Math.trunc(value)));
}

export function LoginPage({
  error,
  loading,
  onLogin,
  workspaceHref,
}: {
  error: string | null;
  loading: boolean;
  onLogin: (input: AuthLoginInput) => Promise<AuthSession | null>;
  workspaceHref: WorkspaceHref;
}) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const canSubmit = Boolean(email.trim() && password);
  return (
    <AuthPageLayout
      description="进入只属于你所在团队的招聘工作区。候选人、岗位、评分和原始文件按工作区分别管理。"
      eyebrow="大卖智聘｜AI 招聘决策工作台"
      title="登录大卖智聘"
      workspaceHref={workspaceHref}
    >
      <form
        className="auth-form"
        onSubmit={(event) => {
          event.preventDefault();
          if (email.trim() && password) {
            void onLogin({ email: email.trim(), password });
          }
        }}
      >
        <div className="field-stack">
          <label className="field-label" htmlFor="login-email">
            工作邮箱
          </label>
          <input
            autoComplete="email"
            className="field"
            id="login-email"
            inputMode="email"
            onChange={(event) => setEmail(event.target.value)}
            placeholder="name@company.com"
            required
            type="email"
            value={email}
          />
        </div>
        <div className="field-stack">
          <div className="auth-field-heading">
            <label className="field-label" htmlFor="login-password">密码</label>
            <a className="auth-inline-link" href={workspaceHref("/forgot-password")}>忘记密码</a>
          </div>
          <input
            autoComplete="current-password"
            className="field"
            id="login-password"
            onChange={(event) => setPassword(event.target.value)}
            placeholder="输入密码"
            required
            type="password"
            value={password}
          />
        </div>
        {error && <p className="auth-error" role="alert">{error}</p>}
        <button
          className="button button-primary auth-submit"
          disabled={loading || !canSubmit}
          type="submit"
        >
          {loading ? <><i className="spinner" />正在登录</> : "登录工作台"}
        </button>
        <p className="auth-footer-copy">
          还没有团队工作区？<a href={workspaceHref("/register")}>免费试用 30 天</a>
        </p>
      </form>
    </AuthPageLayout>
  );
}

export function RegistrationPage({
  error,
  loading,
  onRegister,
  workspaceHref,
}: {
  error: string | null;
  loading: boolean;
  onRegister: (input: AuthRegistrationInput) => Promise<AuthSession | null>;
  workspaceHref: WorkspaceHref;
}) {
  const [organizationName, setOrganizationName] = useState("");
  const [fullName, setFullName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [formError, setFormError] = useState<string | null>(null);
  const [submitted, setSubmitted] = useState(false);
  const [registrationOffer, setRegistrationOffer] = useState<RegistrationOffer>(
    fallbackRegistrationOffer,
  );
  const [offerLoading, setOfferLoading] = useState(true);

  useEffect(() => {
    let active = true;
    void api
      .getRegistrationOffer()
      .then((offer) => {
        if (active) setRegistrationOffer(offer);
      })
      // Public offer details are helpful, never a reason to block signup.
      .catch(() => undefined)
      .finally(() => {
        if (active) setOfferLoading(false);
      });
    return () => {
      active = false;
    };
  }, []);

  const submit = async () => {
    setFormError(null);
    if (password !== confirmation) {
      setFormError("两次输入的密码不一致，请重新确认。");
      return;
    }
    const session = await onRegister({
      organization_name: organizationName.trim(),
      full_name: fullName.trim(),
      email: email.trim(),
      password,
    });
    if (session?.email_verification_required) setSubmitted(true);
  };

  return (
    <AuthPageLayout
      description="注册后即可使用大卖智聘。上传简历，快速筛选、统一评分并查看 JD 匹配依据，把时间留给真正需要你判断的人。"
      eyebrow={offerLoading
        ? "30 天免费体验，含 1,000 次大模型调用"
        : `${registrationOffer.trial_days} 天${registrationOffer.plan_name}免费体验，含 ${formatWholeNumber(registrationOffer.llm_call_limit)} 次大模型调用`}
      title="让招聘判断，从第一份简历开始更快"
      variant="registration"
      workspaceHref={workspaceHref}
    >
      {submitted ? (
        <div aria-live="polite" className="auth-success-state">
          <span className="auth-success-icon"><Icon name="check" size={20} /></span>
          <h2>验证邮箱，马上进入工作台</h2>
          <p>验证邮件已发送到你填写的工作邮箱。点击邮件中的链接，即可开始上传第一份简历。</p>
          <a className="button button-primary auth-submit" href={workspaceHref("/verify-email")}>我已完成邮箱验证</a>
        </div>
      ) : (
        <div className="auth-registration">
          <div className="auth-registration-heading">
            <p>免费创建团队工作台</p>
            <h2>开始 {registrationOffer.trial_days} 天{registrationOffer.plan_name}体验</h2>
            <span>试用期内最多 {formatWholeNumber(registrationOffer.llm_call_limit)} 次大模型调用，简历提取、评分、JD 处理和招聘助手统一计入。</span>
          </div>
          <form
            className="auth-form auth-registration-form"
            onSubmit={(event) => {
              event.preventDefault();
              void submit();
            }}
          >
            <div className="auth-form-grid">
              <div className="field-stack auth-form-span-2">
                <label className="field-label" htmlFor="register-organization">公司 / 团队名称</label>
                <input autoComplete="organization" className="field" id="register-organization" onChange={(event) => setOrganizationName(event.target.value)} placeholder="例如：大卖数智 AI 部" required value={organizationName} />
              </div>
              <div className="field-stack">
                <label className="field-label" htmlFor="register-name">姓名</label>
                <input autoComplete="name" className="field" id="register-name" onChange={(event) => setFullName(event.target.value)} placeholder="请输入你的姓名" required value={fullName} />
              </div>
              <div className="field-stack">
                <label className="field-label" htmlFor="register-email">工作邮箱</label>
                <input autoComplete="email" className="field" id="register-email" inputMode="email" onChange={(event) => setEmail(event.target.value)} placeholder="name@company.com" required type="email" value={email} />
              </div>
              <div className="field-stack">
                <label className="field-label" htmlFor="register-password">设置登录密码</label>
                <input autoComplete="new-password" className="field" id="register-password" minLength={8} onChange={(event) => setPassword(event.target.value)} placeholder="至少 8 个字符" required type="password" value={password} />
              </div>
              <div className="field-stack">
                <label className="field-label" htmlFor="register-password-confirmation">再次输入密码</label>
                <input aria-describedby={formError ? "register-password-error" : undefined} aria-invalid={Boolean(formError)} autoComplete="new-password" className="field" id="register-password-confirmation" minLength={8} onChange={(event) => setConfirmation(event.target.value)} placeholder="请再次输入" required type="password" value={confirmation} />
              </div>
            </div>
            {(formError || error) && <p className="auth-error" id="register-password-error" role="alert">{formError || error}</p>}
            <p className="auth-consent">提交后，我们会向你的工作邮箱发送验证邮件。完成验证即可进入工作台。</p>
            <button className="button button-primary auth-submit" disabled={loading || !organizationName.trim() || !fullName.trim() || !email.trim() || password.length < 8 || !confirmation} type="submit">
              {loading ? <><i className="spinner" />正在创建工作台</> : `免费开启 ${registrationOffer.trial_days} 天体验`}
            </button>
            <p className="auth-footer-copy">已有团队账号？<a href={workspaceHref("/login")}>立即登录</a></p>
          </form>
        </div>
      )}
    </AuthPageLayout>
  );
}

export function EmailVerificationPage({
  error,
  loading,
  session,
  onComplete,
  onRefreshSession,
  onResend,
  workspaceHref,
}: {
  error: string | null;
  loading: boolean;
  session: AuthSession | null;
  onComplete: (token: string) => Promise<AuthSession | null>;
  onRefreshSession: () => Promise<AuthSession | null>;
  onResend: () => Promise<{ accepted: boolean; delivery_available: boolean } | null>;
  workspaceHref: WorkspaceHref;
}) {
  const token = new URLSearchParams(window.location.search).get("token");
  const completionStarted = useRef(false);
  const [verificationState, setVerificationState] = useState<
    "waiting" | "verifying" | "verified" | "failed"
  >(token ? "verifying" : "waiting");
  const [resendState, setResendState] = useState<"idle" | "sent" | "unavailable">("idle");
  const email = session?.user?.email ?? null;
  const canResend = Boolean(session?.authenticated && session.email_verification_required);
  const isWaitingForVerification = Boolean(
    !token && session?.authenticated && session.email_verification_required,
  );
  const verificationSucceeded = Boolean(token && verificationState === "verified");
  const verificationInProgress = Boolean(token && verificationState === "verifying");
  const shouldEnterWorkspace = Boolean(
    session?.authenticated &&
      !session.email_verification_required &&
      (!token || verificationSucceeded),
  );

  useEffect(() => {
    if (!token || completionStarted.current) return;
    completionStarted.current = true;
    setVerificationState("verifying");
    void onComplete(token).then((result) => {
      setVerificationState(
        result?.authenticated && !result.email_verification_required
          ? "verified"
          : "failed",
      );
    });
  }, [onComplete, token]);

  useEffect(() => {
    if (!isWaitingForVerification) return;

    let active = true;
    let refreshing = false;
    const refresh = async () => {
      if (refreshing) return;
      refreshing = true;
      try {
        await onRefreshSession();
      } finally {
        refreshing = false;
      }
    };
    const refreshOnFocus = () => {
      if (active) void refresh();
    };

    void refresh();
    const intervalId = window.setInterval(refresh, 3_000);
    window.addEventListener("focus", refreshOnFocus);
    return () => {
      active = false;
      window.clearInterval(intervalId);
      window.removeEventListener("focus", refreshOnFocus);
    };
  }, [isWaitingForVerification, onRefreshSession]);

  useEffect(() => {
    if (!shouldEnterWorkspace) return;
    // The original registration tab observes the verified server session via
    // polling. The email-link tab owns a newly established verified session.
    // Both tabs should land in the workspace once their own session is ready.
    window.location.replace(workspaceHref());
  }, [shouldEnterWorkspace, workspaceHref]);

  const maskedEmail = email
    ? email.replace(/^(.{1,2}).*(@.*)$/, "$1•••$2")
    : null;

  return (
    <AuthPageLayout
      description="验证工作邮箱后即可进入你的独立招聘工作区。候选人、简历、岗位和 AI 结论始终按工作区隔离。"
      eyebrow="账户验证"
      title={
        verificationSucceeded
          ? "邮箱验证成功"
          : token
            ? verificationInProgress
              ? "正在验证邮箱"
              : "邮箱验证未完成"
            : "请验证工作邮箱"
      }
      workspaceHref={workspaceHref}
    >
      <div aria-live="polite" className="auth-success-state">
        <span className="auth-success-icon">
          <Icon name={verificationSucceeded ? "check" : "inbox"} size={20} />
        </span>
        <h2>
          {verificationSucceeded
            ? "邮箱已验证"
            : token
              ? verificationInProgress
                ? "正在确认你的邮箱"
                : "验证链接未完成验证"
              : "请查收验证邮件"}
        </h2>
        {verificationSucceeded ? (
          <p>验证已经完成，正在进入工作台。</p>
        ) : token ? (
          <p>
            {loading || verificationInProgress
              ? "请稍候，正在安全地验证这条链接。"
              : "验证链接无效或已失效时，你可以登录后重新发送邮件。"}
          </p>
        ) : (
          <p>
            {maskedEmail
              ? `请查看 ${maskedEmail} 的收件箱，并在 24 小时内打开验证链接。`
              : "请登录注册邮箱后打开验证链接，完成后即可进入工作台。"}
          </p>
        )}
        {isWaitingForVerification && (
          <p className="auth-footer-copy" role="status">
            验证完成后，本页面会自动进入工作台。
          </p>
        )}
        {error && <p className="auth-error" role="alert">{error}</p>}
        {canResend && !token && (
          <button
            className="button button-primary auth-submit"
            disabled={loading || resendState === "sent"}
            onClick={() => {
              void onResend().then((result) => {
                if (result?.accepted) {
                  setResendState(result.delivery_available ? "sent" : "unavailable");
                }
              });
            }}
            type="button"
          >
            {loading ? <><i className="spinner" />正在发送</> : resendState === "sent" ? "验证邮件已重新发送" : "重新发送验证邮件"}
          </button>
        )}
        {resendState === "unavailable" && (
          <p className="auth-error" role="status">暂时无法发送验证邮件，请稍后重试。</p>
        )}
        {!canResend && !token && (
          <a className="button button-primary auth-submit" href={workspaceHref("/login")}>返回登录</a>
        )}
        {token && !verificationSucceeded && !loading && (
          <a className="button button-primary auth-submit" href={workspaceHref("/login")}>返回登录</a>
        )}
        <p className="auth-footer-copy">
          验证前不会开放候选人或简历数据访问。
        </p>
      </div>
    </AuthPageLayout>
  );
}

export function ForgotPasswordPage({
  error,
  loading,
  onRequest,
  workspaceHref,
}: {
  error: string | null;
  loading: boolean;
  onRequest: (email: string) => Promise<{ accepted: boolean; delivery_available: boolean } | null>;
  workspaceHref: WorkspaceHref;
}) {
  const [email, setEmail] = useState("");
  const [result, setResult] = useState<{ deliveryAvailable: boolean } | null>(null);

  return (
    <AuthPageLayout
      description="我们不会在此页面显示邮箱是否已注册。重置链接仅发送给有效且可用的账号。"
      eyebrow="账户协助"
      title="找回登录密码"
      workspaceHref={workspaceHref}
    >
      {result ? (
        <div aria-live="polite" className="auth-success-state">
          <span className="auth-success-icon"><Icon name={result.deliveryAvailable ? "check" : "user"} size={20} /></span>
          <h2>{result.deliveryAvailable ? "请查看邮箱" : "请联系管理员"}</h2>
          <p>{result.deliveryAvailable ? "若该邮箱对应可用账号，我们已发送重置密码的后续指引。" : "当前团队暂未启用邮件重置，请联系管理员协助重置密码。"}</p>
          <a className="button button-primary auth-submit" href={workspaceHref("/login")}>返回登录</a>
        </div>
      ) : (
        <form
          className="auth-form"
          onSubmit={(event) => {
            event.preventDefault();
            void onRequest(email.trim()).then((response) => {
              if (response?.accepted) setResult({ deliveryAvailable: response.delivery_available });
            });
          }}
        >
          <div className="field-stack">
            <label className="field-label" htmlFor="reset-email">工作邮箱</label>
            <input autoComplete="email" className="field" id="reset-email" inputMode="email" onChange={(event) => setEmail(event.target.value)} placeholder="name@company.com" required type="email" value={email} />
            <p className="field-help">为保护账户安全，提交后的提示不会披露该邮箱是否已注册。</p>
          </div>
          {error && <p className="auth-error" role="alert">{error}</p>}
          <button className="button button-primary auth-submit" disabled={loading || !email.trim()} type="submit">
            {loading ? <><i className="spinner" />正在提交</> : "获取重置指引"}
          </button>
          <p className="auth-footer-copy"><a href={workspaceHref("/login")}>返回登录</a></p>
        </form>
      )}
    </AuthPageLayout>
  );
}

export function ResetPasswordPage({
  error,
  loading,
  onComplete,
  workspaceHref,
}: {
  error: string | null;
  loading: boolean;
  onComplete: (token: string, password: string) => Promise<boolean>;
  workspaceHref: WorkspaceHref;
}) {
  const token = new URLSearchParams(window.location.search).get("token") ?? "";
  const [password, setPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [formError, setFormError] = useState<string | null>(null);
  const [completed, setCompleted] = useState(false);

  const submit = async () => {
    setFormError(null);
    if (!token) {
      setFormError("缺少重置链接。请重新申请一封重置邮件。");
      return;
    }
    if (password !== confirmation) {
      setFormError("两次输入的密码不一致，请重新确认。");
      return;
    }
    if (await onComplete(token, password)) {
      setCompleted(true);
    }
  };

  return (
    <AuthPageLayout
      description="设置新密码后，旧密码将立即失效。为安全起见，重置链接只能使用一次。"
      eyebrow="账户协助"
      title="设置新的登录密码"
      workspaceHref={workspaceHref}
    >
      {completed ? (
        <div aria-live="polite" className="auth-success-state">
          <span className="auth-success-icon"><Icon name="check" size={20} /></span>
          <h2>新密码已设置</h2>
          <p>请使用新密码登录你的招聘工作台。</p>
          <a className="button button-primary auth-submit" href={workspaceHref("/login")}>前往登录</a>
        </div>
      ) : (
        <form
          className="auth-form"
          onSubmit={(event) => {
            event.preventDefault();
            void submit();
          }}
        >
          <div className="field-stack">
            <label className="field-label" htmlFor="reset-password">新密码</label>
            <input
              autoComplete="new-password"
              className="field"
              id="reset-password"
              minLength={8}
              onChange={(event) => setPassword(event.target.value)}
              placeholder="至少 8 个字符"
              required
              type="password"
              value={password}
            />
          </div>
          <div className="field-stack">
            <label className="field-label" htmlFor="reset-password-confirmation">再次输入新密码</label>
            <input
              aria-describedby={formError || error ? "reset-password-error" : undefined}
              aria-invalid={Boolean(formError || error)}
              autoComplete="new-password"
              className="field"
              id="reset-password-confirmation"
              minLength={8}
              onChange={(event) => setConfirmation(event.target.value)}
              placeholder="请再次输入"
              required
              type="password"
              value={confirmation}
            />
          </div>
          {(formError || error) && <p className="auth-error" id="reset-password-error" role="alert">{formError || error}</p>}
          <button
            className="button button-primary auth-submit"
            disabled={loading || !token || password.length < 8 || !confirmation}
            type="submit"
          >
            {loading ? <><i className="spinner" />正在保存</> : "保存新密码"}
          </button>
          <p className="auth-footer-copy"><a href={workspaceHref("/forgot-password")}>重新申请重置链接</a></p>
        </form>
      )}
    </AuthPageLayout>
  );
}

function AuthPageLayout({
  children,
  description,
  eyebrow,
  title,
  variant = "default",
  workspaceHref,
}: {
  children: ReactNode;
  description: string;
  eyebrow: string;
  title: string;
  variant?: "default" | "registration";
  workspaceHref: WorkspaceHref;
}) {
  const isRegistration = variant === "registration";
  return (
    <main className={`auth-page${isRegistration ? " auth-page-registration" : ""}`}>
      <div className="auth-shell">
        <section className="auth-introduction" aria-labelledby="auth-page-title">
          <a className="auth-brand" href={workspaceHref("/")} aria-label="大卖数智首页">
            <img alt="大卖数智 GreatSell AI" src="/brand/greatsell-logo-cn-white.png" />
          </a>
          <div aria-hidden="true" className="auth-mark" />
          <p className="auth-kicker">{eyebrow}</p>
          <h1 id="auth-page-title">{title}</h1>
          <p>{description}</p>
          <ul className="auth-assurance-list">
            {isRegistration ? (
              <>
                <li><Icon name="spark" size={17} /><span>免费体验进阶版已开放能力，先使用再决定</span></li>
                <li><Icon name="layers" size={17} /><span>简历、岗位与候选人资料仅限你的团队访问</span></li>
                <li><Icon name="user" size={17} /><span>AI 先整理判断依据，是否推进始终由 HR 决定</span></li>
              </>
            ) : (
              <>
                <li><Icon name="layers" size={17} /><span>团队资料集中管理，仅限已授权成员访问</span></li>
                <li><Icon name="briefcase" size={17} /><span>从简历筛选、AI 评分到 JD 匹配，在大卖智聘统一完成</span></li>
                <li><Icon name="user" size={17} /><span>AI 提供判断依据，最终决定始终属于招聘团队</span></li>
              </>
            )}
          </ul>
        </section>
        <section className="auth-panel" aria-label={title}>
          {children}
        </section>
      </div>
    </main>
  );
}
