import { useCallback, useEffect, useState } from "react";
import type {
  WorkspaceNavigationView,
  WorkspaceSettingsSection,
  WorkspaceView,
} from "./workspace-navigation-types";
import {
  isRemovedRecruitingWorkflowRoute,
  workspaceHashForView,
  workspaceRouteFromHash,
  type WorkspaceRouteParams,
} from "./workspace-navigation";

export type {
  WorkspaceNavigationView,
  WorkspaceSettingsSection,
  WorkspaceView,
} from "./workspace-navigation-types";

function settingsSectionFromHash(
  hash: string,
): WorkspaceSettingsSection | null {
  const value = hash
    .replace(/^#/, "")
    .trim()
    .replace(/^\/+|\/+$/g, "")
    .toLowerCase();

  if (value === "settings/mailbox" || value === "inbox") return "mailbox";
  if (value === "settings/data" || value === "data") return "data";
  return null;
}

function settingsHash(section: WorkspaceSettingsSection): string {
  return `#settings/${section}`;
}

function feedbackHash(): string {
  return "#feedback";
}

function sameRouteParams(
  left: WorkspaceRouteParams,
  right: WorkspaceRouteParams,
): boolean {
  return left.createJob === right.createJob &&
    left.jobVersionId === right.jobVersionId;
}

interface UseWorkspaceNavigationOptions {
  canManageCandidateData: boolean;
  canManageMailbox: boolean;
  hasSession: boolean;
}

/**
 * Keeps the workspace route, settings tab and browser hash in one place.
 * Permission decisions stay server-owned; this controller only redirects a
 * person away from a settings section that their current session cannot use.
 */
export function useWorkspaceNavigation({
  canManageCandidateData,
  canManageMailbox,
  hasSession,
}: UseWorkspaceNavigationOptions) {
  const [view, setView] = useState<WorkspaceView>(() =>
    workspaceRouteFromHash(window.location.hash)?.view ??
    (window.location.hash.replace(/^#/, "").trim().toLowerCase() === "feedback"
      ? "feedback"
      : settingsSectionFromHash(window.location.hash)
        ? "settings"
        : "workbench"),
  );
  const [settingsSection, setSettingsSection] =
    useState<WorkspaceSettingsSection>(
      () => settingsSectionFromHash(window.location.hash) ?? "mailbox",
    );
  const [routeParams, setRouteParams] = useState<WorkspaceRouteParams>(
    () => {
      const route = workspaceRouteFromHash(window.location.hash);
      return route
        ? {
            createJob: route.createJob,
            jobVersionId: route.jobVersionId,
          }
        : {};
    },
  );
  const canManageSettings = canManageMailbox || canManageCandidateData;

  const updateHash = useCallback(
    (nextHash: string, replace = false) => {
      if (window.location.hash === nextHash) return;
      const nextLocation = `${window.location.pathname}${window.location.search}${nextHash}`;
      if (replace) {
        window.history.replaceState(window.history.state, "", nextLocation);
      } else {
        window.history.pushState(window.history.state, "", nextLocation);
      }
    },
    [],
  );

  const updateSettingsHash = useCallback(
    (section: WorkspaceSettingsSection | null, replace = false) => {
      updateHash(section ? settingsHash(section) : "", replace);
    },
    [updateHash],
  );

  const navigateToView = useCallback(
    (nextView: WorkspaceNavigationView, nextRoute: WorkspaceRouteParams = {}) => {
      setView((current) => current === nextView ? current : nextView);
      setRouteParams((current) => sameRouteParams(current, nextRoute) ? current : nextRoute);
      updateHash(workspaceHashForView(nextView, nextRoute));
    },
    [updateHash],
  );

  const openSettings = useCallback(
    (section: WorkspaceSettingsSection) => {
      setSettingsSection(section);
      setView("settings");
      setRouteParams({});
      updateSettingsHash(section);
    },
    [updateSettingsHash],
  );

  const openFeedback = useCallback(() => {
    setView("feedback");
    setRouteParams({});
    const nextHash = feedbackHash();
    if (window.location.hash === nextHash) return;
    window.history.pushState(
      window.history.state,
      "",
      `${window.location.pathname}${window.location.search}${nextHash}`,
    );
  }, []);

  useEffect(() => {
    const syncSettingsFromHash = () => {
      const route = workspaceRouteFromHash(window.location.hash);
      if (route) {
        setView(route.view);
        setRouteParams({
          createJob: route.createJob,
          jobVersionId: route.jobVersionId,
        });
        return;
      }
      if (window.location.hash.replace(/^#/, "").trim().toLowerCase() === "feedback") {
        setView("feedback");
        return;
      }
      const section = settingsSectionFromHash(window.location.hash);
      if (!section) {
        // A browser Back action from a hash-routed view returns to the clean
        // workspace URL, whose canonical screen is the recruitment workbench.
        setView("workbench");
        setRouteParams({});
        if (isRemovedRecruitingWorkflowRoute(window.location.hash)) {
          updateHash(workspaceHashForView("workbench"), true);
        }
        return;
      }
      setSettingsSection(section);
      setView("settings");
      setRouteParams({});
      updateSettingsHash(section, true);
    };

    syncSettingsFromHash();
    window.addEventListener("hashchange", syncSettingsFromHash);
    window.addEventListener("popstate", syncSettingsFromHash);
    return () => {
      window.removeEventListener("hashchange", syncSettingsFromHash);
      window.removeEventListener("popstate", syncSettingsFromHash);
    };
  }, [updateSettingsHash]);

  useEffect(() => {
    if (!hasSession || view !== "settings") return;
    const sectionAllowed =
      (settingsSection === "mailbox" && canManageMailbox) ||
      (settingsSection === "data" && canManageCandidateData);
    if (sectionAllowed) return;

    const fallbackSection = canManageMailbox
      ? "mailbox"
      : canManageCandidateData
        ? "data"
        : null;
    if (!fallbackSection) {
      setView("library");
      updateSettingsHash(null, true);
      return;
    }
    setSettingsSection(fallbackSection);
    updateSettingsHash(fallbackSection, true);
  }, [
    canManageCandidateData,
    canManageMailbox,
    hasSession,
    settingsSection,
    updateSettingsHash,
    view,
  ]);

  return {
    canManageSettings,
    navigateToView,
    openFeedback,
    openSettings,
    settingsSection,
    routeParams,
    view,
  };
}
