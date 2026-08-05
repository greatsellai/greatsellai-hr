import type { IconName } from "../../icons";
import type { WorkspaceNavigationView } from "./workspace-navigation-types";

export interface WorkspaceNavigationViewItem {
  kind: "view";
  view: WorkspaceNavigationView;
  label: string;
  icon: IconName;
}

export interface WorkspaceNavigationGroup {
  id: string;
  label: string;
  items: WorkspaceNavigationViewItem[];
}

/** Resource identifiers that are safe to carry in a browser route. */
export interface WorkspaceRouteParams {
  createJob?: boolean;
  jobVersionId?: string;
}

export interface WorkspaceRoute extends WorkspaceRouteParams {
  view: WorkspaceNavigationView;
}

/**
 * The shell, routes, permissions and actions all read the same navigation
 * model.  Keeping it here stops labels such as "招聘详情" from becoming a
 * second, slightly different information architecture in the sidebar.
 */
export const workspaceNavigationGroups: WorkspaceNavigationGroup[] = [
  {
    id: "overview",
    label: "工作台",
    items: [
      { kind: "view", view: "workbench", label: "工作台", icon: "activity" },
      { kind: "view", view: "agent", label: "招聘 Agent", icon: "spark" },
    ],
  },
  {
    id: "recruiting",
    label: "招聘",
    items: [
      { kind: "view", view: "jobs", label: "职位管理", icon: "briefcase" },
      { kind: "view", view: "match", label: "智能匹配", icon: "match" },
    ],
  },
  {
    id: "talent",
    label: "人才",
    items: [
      { kind: "view", view: "library", label: "人才库", icon: "folder" },
      { kind: "view", view: "favorites", label: "我的收藏", icon: "bookmark" },
      { kind: "view", view: "filter", label: "条件筛选", icon: "filter" },
      { kind: "view", view: "upload", label: "上传简历", icon: "upload" },
    ],
  },
  {
    id: "evaluation",
    label: "评估",
    items: [
      { kind: "view", view: "score", label: "评分模板", icon: "layers" },
    ],
  },
];

const hashByView: Record<WorkspaceNavigationView, string> = {
  workbench: "#workbench",
  agent: "#agent",
  jobs: "#jobs",
  library: "#library",
  favorites: "#favorites",
  filter: "#filter",
  upload: "#upload",
  score: "#score",
  match: "#matching",
};

const viewByRoute: Record<string, WorkspaceNavigationView> = {
  workbench: "workbench",
  dashboard: "workbench",
  agent: "agent",
  "recruiting-agent": "agent",
  jobs: "jobs",
  positions: "jobs",
  library: "library",
  talent: "library",
  favorites: "favorites",
  filter: "filter",
  upload: "upload",
  score: "score",
  matching: "match",
  match: "match",
};

function normalizedHashRoute(hash: string): string {
  return hash
    .replace(/^#/, "")
    .trim()
    .split("?", 1)[0]
    .replace(/^\/+|\/+$/g, "")
    .toLowerCase();
}

/**
 * These hashes belonged to the removed recruitment-process board. Keep this
 * migration marker so previously shared URLs can be redirected to the
 * workbench instead of leaving people on a stale hash.
 */
export function isRemovedRecruitingWorkflowRoute(hash: string): boolean {
  const route = normalizedHashRoute(hash);
  return route === "workflow" || route === "recruiting";
}

function readRouteParam(params: URLSearchParams, key: string): string | undefined {
  const value = params.get(key)?.trim();
  return value || undefined;
}

export function workspaceRouteFromHash(hash: string): WorkspaceRoute | null {
  const view = viewByRoute[normalizedHashRoute(hash)];
  if (!view) return null;
  const queryIndex = hash.indexOf("?");
  const params = new URLSearchParams(queryIndex >= 0 ? hash.slice(queryIndex + 1) : "");
  return {
    view,
    createJob: params.get("new") === "1",
    jobVersionId: readRouteParam(params, "jobVersion"),
  };
}

export function workspaceViewFromHash(hash: string): WorkspaceNavigationView | null {
  return workspaceRouteFromHash(hash)?.view ?? null;
}

export function workspaceHashForView(
  view: WorkspaceNavigationView,
  route: WorkspaceRouteParams = {},
): string {
  const params = new URLSearchParams();
  if (route.createJob) params.set("new", "1");
  if (route.jobVersionId) params.set("jobVersion", route.jobVersionId);
  const query = params.toString();
  return `${hashByView[view]}${query ? `?${query}` : ""}`;
}
