import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../../api";
import type {
  AuthLoginInput,
  AuthRegistrationInput,
  AuthSession,
  AuthWorkspaceMembership,
} from "../../types";

export type WorkspaceAuthState =
  | "checking"
  | "authenticated"
  | "unauthenticated";

interface UseWorkspaceAuthOptions {
  authRoute: string | null;
  formatError: (error: unknown) => string;
  onLogoutCleanup: () => void;
  rootWorkspaceBasePath: string;
  workspaceHref: (path?: string) => string;
}

export function useWorkspaceAuth({
  authRoute,
  formatError,
  onLogoutCleanup,
  rootWorkspaceBasePath,
  workspaceHref,
}: UseWorkspaceAuthOptions) {
  const [authState, setAuthState] = useState<WorkspaceAuthState>("checking");
  const [authSession, setAuthSession] = useState<AuthSession | null>(null);
  const [authError, setAuthError] = useState<string | null>(null);
  const [authLoading, setAuthLoading] = useState(false);
  const [workspaceMemberships, setWorkspaceMemberships] = useState<AuthWorkspaceMembership[]>([]);
  const authSessionEpochRef = useRef(0);

  const applyAuthSession = useCallback((session: AuthSession) => {
    // A newer session state makes every older in-flight session refresh
    // obsolete. This matters most when a user signs out from the email
    // verification page while its short-interval poll is still in flight.
    authSessionEpochRef.current += 1;
    setAuthSession(session);
    setAuthState(session.authenticated ? "authenticated" : "unauthenticated");
  }, []);

  // The verification email can be opened in another browser or device. The
  // registration tab keeps its own signed session, so polling the current
  // session is enough to learn that the user record was verified elsewhere.
  const refreshAuthSession = useCallback(async (): Promise<AuthSession | null> => {
    const requestEpoch = authSessionEpochRef.current;
    try {
      const session = await api.getAuthSession();
      if (requestEpoch !== authSessionEpochRef.current) return null;
      applyAuthSession(session);
      return session;
    } catch {
      // A transient refresh failure must not log out a person who is simply
      // waiting for their verification email. The initial session bootstrap
      // below still handles a genuine unauthenticated start safely.
      return null;
    }
  }, [applyAuthSession]);

  const refreshWorkspaceMemberships = useCallback(async () => {
    try {
      const response = await api.listAuthWorkspaces();
      setWorkspaceMemberships(response.items);
      return response.items;
    } catch {
      // The server remains authoritative. A transient menu refresh failure
      // never changes the current authenticated workspace.
      return [];
    }
  }, []);

  useEffect(() => {
    let active = true;
    const requestEpoch = authSessionEpochRef.current;
    void api
      .getAuthSession()
      .then((session) => {
        if (!active || requestEpoch !== authSessionEpochRef.current) return;
        applyAuthSession(session);
      })
      .catch(() => {
        if (!active || requestEpoch !== authSessionEpochRef.current) return;
        setAuthSession(null);
        setAuthState("unauthenticated");
      });
    return () => {
      active = false;
    };
  }, [applyAuthSession]);

  useEffect(() => {
    if (authState !== "authenticated" || !authSession?.authenticated) {
      setWorkspaceMemberships([]);
      return;
    }
    void refreshWorkspaceMemberships();
  }, [authSession?.authenticated, authSession?.organization?.organization_id, authState, refreshWorkspaceMemberships]);

  // Large-model usage can change in a background worker, an Agent turn, or a
  // second browser tab. Refresh the small server-owned trial snapshot while
  // the workspace is open so the visible allowance does not drift for long.
  useEffect(() => {
    if (
      authState !== "authenticated" ||
      authRoute ||
      authSession?.email_verification_required ||
      authSession?.trial?.plan_status !== "trial"
    ) {
      return;
    }
    const refreshOnFocus = () => {
      if (document.visibilityState === "visible") void refreshAuthSession();
    };
    const intervalId = window.setInterval(refreshOnFocus, 60_000);
    window.addEventListener("focus", refreshOnFocus);
    return () => {
      window.clearInterval(intervalId);
      window.removeEventListener("focus", refreshOnFocus);
    };
  }, [
    authRoute,
    authSession?.email_verification_required,
    authSession?.trial?.plan_status,
    authState,
    refreshAuthSession,
  ]);

  const establishSession = useCallback(
    (session: AuthSession) => {
      applyAuthSession(session);
      if (session.authenticated) {
        const nextPath = new URLSearchParams(window.location.search).get("next");
        if (
          nextPath &&
          (nextPath === "/platform" ||
            nextPath.startsWith("/platform/") ||
            nextPath === `${rootWorkspaceBasePath}/platform` ||
            nextPath.startsWith(`${rootWorkspaceBasePath}/platform/`)) &&
          session.is_platform_admin
        ) {
          window.location.assign(nextPath);
          return session;
        }
        window.location.assign(
          workspaceHref(session.email_verification_required ? "/verify-email" : ""),
        );
      }
      return session;
    },
    [applyAuthSession, rootWorkspaceBasePath, workspaceHref],
  );

  const login = useCallback(
    async (input: AuthLoginInput) => {
      setAuthError(null);
      setAuthLoading(true);
      try {
        return establishSession(await api.login(input));
      } catch (error) {
        setAuthError(formatError(error));
        return null;
      } finally {
        setAuthLoading(false);
      }
    },
    [establishSession, formatError],
  );

  const register = useCallback(
    async (input: AuthRegistrationInput) => {
      setAuthError(null);
      setAuthLoading(true);
      try {
        return establishSession(await api.register(input));
      } catch (error) {
        setAuthError(formatError(error));
        return null;
      } finally {
        setAuthLoading(false);
      }
    },
    [establishSession, formatError],
  );

  const requestPasswordReset = useCallback(
    async (email: string) => {
      setAuthError(null);
      setAuthLoading(true);
      try {
        return await api.requestPasswordReset(email);
      } catch (error) {
        setAuthError(formatError(error));
        return null;
      } finally {
        setAuthLoading(false);
      }
    },
    [formatError],
  );

  const completePasswordReset = useCallback(
    async (token: string, password: string) => {
      setAuthError(null);
      setAuthLoading(true);
      try {
        await api.completePasswordReset({ token, password });
        return true;
      } catch (error) {
        setAuthError(formatError(error));
        return false;
      } finally {
        setAuthLoading(false);
      }
    },
    [formatError],
  );

  const completeEmailVerification = useCallback(
    async (token: string) => {
      setAuthError(null);
      setAuthLoading(true);
      try {
        // This response establishes the verification-link browser session.
        // EmailVerificationPage then enters that workspace, while the original
        // registration tab independently observes the verified server session.
        const session = await api.completeEmailVerification(token);
        applyAuthSession(session);
        return session;
      } catch (error) {
        setAuthError(formatError(error));
        return null;
      } finally {
        setAuthLoading(false);
      }
    },
    [applyAuthSession, formatError],
  );

  const resendEmailVerification = useCallback(async () => {
    setAuthError(null);
    setAuthLoading(true);
    try {
      return await api.resendEmailVerification();
    } catch (error) {
      setAuthError(formatError(error));
      return null;
    } finally {
      setAuthLoading(false);
    }
  }, [formatError]);

  const switchWorkspace = useCallback(
    async (membershipId: string) => {
      setAuthError(null);
      setAuthLoading(true);
      try {
        const session = await api.switchAuthWorkspace(membershipId);
        // All view-local candidate, filter, and Agent state belongs to the
        // previous tenant. Clear it before the full workspace reload.
        onLogoutCleanup();
        applyAuthSession(session);
        window.location.assign(workspaceHref());
        return true;
      } catch (error) {
        setAuthError(formatError(error));
        return false;
      } finally {
        setAuthLoading(false);
      }
    },
    [applyAuthSession, formatError, onLogoutCleanup, workspaceHref],
  );

  const logout = useCallback(async (): Promise<boolean> => {
    // Invalidate any session refresh that was started before the person chose
    // to leave. A delayed response must never resurrect this browser session.
    authSessionEpochRef.current += 1;
    setAuthError(null);
    setAuthLoading(true);
    try {
      await api.logout();
      onLogoutCleanup();
      setAuthSession(null);
      setWorkspaceMemberships([]);
      setAuthState("unauthenticated");
      window.location.assign(workspaceHref("/login"));
      return true;
    } catch (error) {
      // Do not clear the local session when the server did not acknowledge
      // the logout. This keeps the verification gate intact and lets the
      // page explain that the person can retry instead of appearing to leave.
      setAuthError(formatError(error));
      return false;
    } finally {
      setAuthLoading(false);
    }
  }, [formatError, onLogoutCleanup, workspaceHref]);

  return {
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
  };
}
