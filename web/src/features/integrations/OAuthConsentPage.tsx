import { lazy, Suspense, useCallback, useEffect, useRef, useState } from "react";
import { BackofficeButton } from "../../backoffice/ui/BackofficeButton";
import { api } from "../../api";
import {
  integrationApi,
  integrationErrorMessage,
  type IntegrationOAuthConsent,
  type IntegrationScope,
} from "./integration-api";

const SemiBanner = lazy(() => import("@douyinfe/semi-ui-19/lib/es/banner"));
const SemiCard = lazy(() => import("@douyinfe/semi-ui-19/lib/es/card"));
const SemiCheckbox = lazy(() => import("@douyinfe/semi-ui-19/lib/es/checkbox/checkbox"));
const SemiEmpty = lazy(() => import("@douyinfe/semi-ui-19/lib/es/empty"));
const SemiParagraph = lazy(() => import("@douyinfe/semi-ui-19/lib/es/typography/paragraph"));
const SemiSpace = lazy(() => import("@douyinfe/semi-ui-19/lib/es/space"));
const SemiTitle = lazy(() => import("@douyinfe/semi-ui-19/lib/es/typography/title"));

const SCOPE_LABELS: Record<IntegrationScope, string> = {
  "candidates:read": "候选人资料（只读）",
  "jobs:read": "职位与 JD（只读）",
  "assessments:read": "评估结果（只读）",
  "evidence:read": "原文依据（只读）",
  "analyses:read": "我的分析草稿（只读）",
  "analyses:write": "我的分析草稿（保存）",
};
const KNOWN_SCOPES = new Set<IntegrationScope>(Object.keys(SCOPE_LABELS) as IntegrationScope[]);
const BASE_READ_SCOPES = new Set<IntegrationScope>(["candidates:read", "jobs:read", "assessments:read"]);
const METADATA_ERROR = "授权信息不完整或权限范围异常，无法安全继续。请返回外部工具后重新发起连接。";

function formatDate(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { hour12: false });
}

function isScopeList(value: unknown): value is IntegrationScope[] {
  return Array.isArray(value) && value.every((scope) => typeof scope === "string" && KNOWN_SCOPES.has(scope as IntegrationScope)) &&
    new Set(value).size === value.length && value.every((scope, index) => index === 0 || value[index - 1] <= scope);
}

function validConsentMetadata(consent: IntegrationOAuthConsent): boolean {
  if (!isScopeList(consent.scopes) || !isScopeList(consent.available_scopes) || !isScopeList(consent.default_scopes)) return false;
  const requested = new Set(consent.scopes);
  const available = new Set(consent.available_scopes);
  return consent.available_scopes.every((scope) => requested.has(scope)) &&
    consent.default_scopes.every((scope) => available.has(scope));
}

function isLoopbackHost(hostname: string): boolean {
  return hostname === "localhost" || hostname === "127.0.0.1" || hostname === "[::1]" || hostname === "::1";
}

/**
 * Defense in depth for the server's registered redirect validation. Browser
 * code only follows the exact origin presented by the server-bound consent;
 * production redirects must be HTTPS, with HTTP restricted to loopback tools.
 */
function safeConsentRedirect(value: string, consent: IntegrationOAuthConsent, approve: boolean): string | null {
  try {
    const target = new URL(value);
    const expected = new URL(consent.client.redirect_origin);
    if (target.origin !== expected.origin || target.username || target.password || target.hash) return null;
    if (target.protocol !== "https:" && !(target.protocol === "http:" && isLoopbackHost(target.hostname))) return null;
    const code = target.searchParams.get("code");
    if (approve && !code) return null;
    if (!approve && code) return null;
    return target.href;
  } catch {
    return null;
  }
}

export function OAuthConsentPage({
  requestId,
  accountEmail,
  userId,
  workspaceId,
}: {
  requestId: string;
  accountEmail: string | null;
  userId: string | null;
  workspaceId: string | null;
}) {
  const [consent, setConsent] = useState<IntegrationOAuthConsent | null>(null);
  const [selectedScopes, setSelectedScopes] = useState<IntegrationScope[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const epochRef = useRef(0);

  useEffect(() => {
    const epoch = epochRef.current + 1;
    epochRef.current = epoch;
    const controller = new AbortController();
    setConsent(null);
    setSelectedScopes([]);
    setError(null);
    setSubmitting(false);
    void Promise.all([integrationApi.getOAuthConsent(requestId, controller.signal), api.getAuthSession()]).then(
      ([response, session]) => {
        if (epoch !== epochRef.current) return;
        if (!session.authenticated || session.user?.user_id !== userId || session.organization?.organization_id !== workspaceId ||
            !validConsentMetadata(response) || !workspaceId || response.workspace.organization_id !== workspaceId) {
          setError(METADATA_ERROR);
          return;
        }
        setConsent(response);
        setSelectedScopes(response.default_scopes);
      },
      (requestError) => {
        if (epoch === epochRef.current && requestError instanceof DOMException && requestError.name === "AbortError") return;
        if (epoch === epochRef.current) setError(integrationErrorMessage(requestError));
      },
    );
    return () => {
      controller.abort();
      epochRef.current += 1;
      setConsent(null);
      setSelectedScopes([]);
    };
  }, [requestId, userId, workspaceId]);

  const selectScope = useCallback((scope: IntegrationScope, checked: boolean) => {
    setSelectedScopes((current) => (checked ? [...new Set([...current, scope])] : current.filter((value) => value !== scope)).sort());
    setError(null);
  }, []);

  const decide = useCallback(async (approve: boolean) => {
    const epoch = epochRef.current;
    if (!consent || submitting) return;
    if (approve && selectedScopes.length === 0) {
      setError("请至少选择一项访问权限，或拒绝本次授权请求。");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const result = await integrationApi.submitOAuthConsent(
        consent.request_id,
        { approve, approved_scopes: approve ? [...selectedScopes].sort() : null },
        consent.csrf_token,
      );
      if (epoch !== epochRef.current) return;
      const redirect = safeConsentRedirect(result.redirect_url, consent, approve);
      if (!redirect) {
        setError(approve ? "授权结果无法安全返回外部工具，请重新发起连接。" : "拒绝结果异常：返回地址包含授权码或不受信任地址，未继续跳转。");
        return;
      }
      window.location.assign(redirect);
    } catch (requestError) {
      if (epoch === epochRef.current) setError(integrationErrorMessage(requestError));
    } finally {
      if (epoch === epochRef.current) setSubmitting(false);
    }
  }, [consent, selectedScopes, submitting]);

  return (
    <Suspense fallback={<p>正在读取授权请求…</p>}>
      <main className="login-page" aria-live="polite">
        <div className="login-panel" style={{ maxWidth: 720 }}>
          <SemiSpace spacing="medium" vertical style={{ width: "100%" }}>
            <div>
              <SemiTitle heading={2}>授权外部工具</SemiTitle>
              <SemiParagraph type="tertiary">请确认本次连接要访问的公司、工具和数据范围。</SemiParagraph>
            </div>
            {error && <SemiBanner description={error} title="授权未完成" type="danger" />}
            {!consent && !error && <SemiEmpty description="正在读取授权请求…" />}
            {consent && <>
              <SemiBanner description="批准后，外部 AI 工具会收到你主动允许其读取的数据。请只在贵司批准的工具中继续；拒绝会结束本次请求，已发送给外部工具的数据无法追回。" title="候选资料外部访问提示" type="warning" />
              <SemiCard title="本次授权">
                <SemiSpace spacing="medium" vertical style={{ width: "100%" }}>
                  <SemiParagraph style={{ margin: 0 }}>当前账号：{accountEmail || "当前登录账号"}</SemiParagraph>
                  <SemiParagraph style={{ margin: 0 }}>第三方应用：{consent.client.name}</SemiParagraph>
                  <SemiParagraph type="tertiary" style={{ margin: 0 }}>应用名称由客户端自报，不代表大卖智聘对其官方认证。</SemiParagraph>
                  <SemiParagraph style={{ margin: 0 }}>回调来源：{consent.client.redirect_origin}</SemiParagraph>
                  <SemiParagraph style={{ margin: 0 }}>当前公司/工作区：{consent.workspace.name}</SemiParagraph>
                  <SemiParagraph style={{ margin: 0 }}>连接类型：{consent.audience === "mcp" ? "MCP" : "API"}；请求有效至：{formatDate(consent.expires_at)}</SemiParagraph>
                </SemiSpace>
              </SemiCard>
              <SemiCard title="选择允许的访问范围">
                <SemiSpace spacing="medium" vertical style={{ width: "100%" }}>
                  <SemiParagraph style={{ margin: 0 }}>默认仅勾选基础只读权限。原文依据和个人分析草稿等可选权限，只有在你明确勾选后才会授权。</SemiParagraph>
                  {consent.scopes.map((scope) => {
                    const available = consent.available_scopes.includes(scope);
                    return <div key={scope}>
                      <SemiCheckbox checked={selectedScopes.includes(scope)} disabled={!available || submitting} onChange={(event) => selectScope(scope, Boolean(event.target.checked))}>{SCOPE_LABELS[scope]}</SemiCheckbox>
                      <SemiParagraph type="tertiary" style={{ margin: "4px 0 0 28px" }}>
                        {!available ? "当前公司政策或已启用功能未开放这项权限。" : BASE_READ_SCOPES.has(scope) ? "基础只读权限，已按安全默认值勾选；你仍可取消。" : "可选权限，仅在你明确勾选后授权。"}
                      </SemiParagraph>
                    </div>;
                  })}
                </SemiSpace>
              </SemiCard>
              <SemiSpace spacing="medium">
                <BackofficeButton disabled={submitting || selectedScopes.length === 0} loading={submitting} onClick={() => void decide(true)} tone="primary">批准并返回工具</BackofficeButton>
                <BackofficeButton disabled={submitting} onClick={() => void decide(false)} tone="danger">拒绝并返回工具</BackofficeButton>
              </SemiSpace>
            </>}
          </SemiSpace>
        </div>
      </main>
    </Suspense>
  );
}
