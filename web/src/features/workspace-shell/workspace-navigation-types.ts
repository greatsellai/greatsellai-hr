export type WorkspaceNavigationView =
  | "workbench"
  | "agent"
  | "jobs"
  | "library"
  | "favorites"
  | "filter"
  | "upload"
  | "score"
  | "match";

/**
 * Feedback is intentionally reached from the account menu rather than the
 * recruitment side rail. It is a short product-research task, not a primary
 * recruiting workflow.
 */
export type WorkspaceView = WorkspaceNavigationView | "settings" | "feedback";

export type WorkspaceSettingsSection = "mailbox" | "data";
