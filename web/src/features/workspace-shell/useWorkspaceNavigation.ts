import { useCallback, useEffect, useState } from "react";
import type {
  WorkspaceNavigationView,
  WorkspaceSettingsSection,
  WorkspaceView,
} from "./workspace-navigation-types";

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
    window.location.hash.replace(/^#/, "").trim().toLowerCase() === "feedback"
      ? "feedback"
      : settingsSectionFromHash(window.location.hash)
        ? "settings"
        : "library",
  );
  const [settingsSection, setSettingsSection] =
    useState<WorkspaceSettingsSection>(
      () => settingsSectionFromHash(window.location.hash) ?? "mailbox",
    );
  const canManageSettings = canManageMailbox || canManageCandidateData;

  const updateSettingsHash = useCallback(
    (section: WorkspaceSettingsSection | null, replace = false) => {
      const nextHash = section ? settingsHash(section) : "";
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

  const navigateToView = useCallback(
    (nextView: WorkspaceNavigationView) => {
      setView(nextView);
      updateSettingsHash(null);
    },
    [updateSettingsHash],
  );

  const openSettings = useCallback(
    (section: WorkspaceSettingsSection) => {
      setSettingsSection(section);
      setView("settings");
      updateSettingsHash(section);
    },
    [updateSettingsHash],
  );

  const openFeedback = useCallback(() => {
    setView("feedback");
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
      if (window.location.hash.replace(/^#/, "").trim().toLowerCase() === "feedback") {
        setView("feedback");
        return;
      }
      const section = settingsSectionFromHash(window.location.hash);
      if (!section) {
        setView((current) => (current === "settings" ? "library" : current));
        return;
      }
      setSettingsSection(section);
      setView("settings");
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
    view,
  };
}
