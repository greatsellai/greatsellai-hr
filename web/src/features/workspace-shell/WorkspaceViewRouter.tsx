import type {
  CandidateSearchRequest,
  CandidateSearchItem,
  JobMatch,
  RecruitingAgentCandidate,
  RecruitingAgentFilterScopeRequest,
  ResumeLibraryItem,
  ScoreTemplate,
} from "../../types";
import { ResumeLibraryPage } from "../library/ResumeLibraryPage";
import { FavoriteCandidatesPage } from "../favorites/FavoriteCandidatesPage";
import { FilterWorkspace } from "../filter/FilterWorkspace";
import { type useCandidateSearchController } from "../filter/useCandidateSearchController";
import { ScoreWorkspace } from "../scoring/ScoreWorkspace";
import { MatchWorkspace } from "../job-match/MatchWorkspace";
import { RecruitingOverview } from "../recruiting/RecruitingOverview";
import { UploadPage } from "../upload/UploadPage";
import { WorkspaceSettingsPage } from "../workspace-settings/WorkspaceSettingsPage";
import { WorkspaceFeedbackPage } from "../workspace-feedback/WorkspaceFeedbackPage";
import { RecruitingAgentPage } from "../recruiting-agent/RecruitingAgentPage";
import type { CandidateDrawerTab } from "../candidate-drawer/candidate-drawer-types";
import { useCallback } from "react";
import type {
  WorkspaceNavigationView,
  WorkspaceSettingsSection,
  WorkspaceView,
} from "./workspace-navigation-types";
import type { WorkspaceRouteParams } from "./workspace-navigation";

type CandidateSearchController = ReturnType<typeof useCandidateSearchController>;

type FilterWorkspaceController = Pick<
  CandidateSearchController,
  | "appliedFilter"
  | "changeScoreTemplate"
  | "filterDraft"
  | "filterOptions"
  | "loadMore"
  | "resetFilter"
  | "scoreTemplateId"
  | "scoreTemplates"
  | "search"
  | "searching"
  | "updateFilterDraft"
>;

type ToastKind = "success" | "error";

export interface WorkspaceViewRouterProps {
  agent: {
    conversationStorageScope: string | null;
    pendingFilterScope: RecruitingAgentFilterScopeRequest | null;
    onPendingFilterScopeHandled: (requestId: number) => void;
    onOpenMailboxSettings: () => void;
    onOpenMatchWorkspace: () => void;
    onOpenResume: (candidate: RecruitingAgentCandidate) => void;
    onOpenScoreWorkspace: () => void;
  };
  feedback: {
    formatError: (error: unknown) => string;
    notify: (kind: ToastKind, message: string) => void;
    onRewardGranted: () => void;
  };
  filter: FilterWorkspaceController;
  library: {
    refreshToken: number;
    selectedResumeId: string | null;
  };
  navigation: {
    navigateToView: (view: WorkspaceNavigationView, route?: WorkspaceRouteParams) => void;
    openSettings: (section: WorkspaceSettingsSection) => void;
  };
  permissions: {
    canGenerateAiJd: boolean;
    canManageCandidateData: boolean;
    canManageMailbox: boolean;
    canManageSettings: boolean;
    role: "admin" | "recruiter" | null;
  };
  settingsSection: WorkspaceSettingsSection;
  routeParams: WorkspaceRouteParams;
  view: WorkspaceView;
  onLibraryChanged: () => void;
  /** Refresh every current-user favorite projection after a bookmark change. */
  onFavoriteChanged: () => void;
  /**
   * Favorites are candidate-level, so the existing resume-library callback
   * cannot describe an arbitrary historical version without fabricating a
   * ResumeLibraryItem. App wires this directly to the shared drawer opener.
   */
  onOpenFavoriteResume?: (
    resumeId: string,
    candidateId: string,
    candidateName: string | null,
  ) => void;
  onOpenCandidate: (
    item: CandidateSearchItem,
    tab?: CandidateDrawerTab,
  ) => void;
  onOpenLibraryResume: (item: ResumeLibraryItem) => void;
  onOpenMatchedResume: (match: JobMatch) => void;
  onOpenRecruitingAgent: () => void;
  onRefineWithAgent: (filter: CandidateSearchRequest, totalCount: number) => void;
  onScoreCreated: () => void;
  onTemplateCreated: (template: ScoreTemplate) => void;
  onUploadedResume: (resumeId: string, candidateId: string) => void;
}

/**
 * The authenticated workspace's page switcher. It deliberately owns no
 * route, auth, query, or drawer state: App keeps those controllers and this
 * component only connects each feature page to their stable callbacks.
 */
export function WorkspaceViewRouter({
  agent,
  feedback,
  filter,
  library,
  navigation,
  permissions,
  settingsSection,
  routeParams,
  view,
  onLibraryChanged,
  onFavoriteChanged,
  onOpenFavoriteResume,
  onOpenCandidate,
  onOpenLibraryResume,
  onOpenMatchedResume,
  onOpenRecruitingAgent,
  onRefineWithAgent,
  onScoreCreated,
  onTemplateCreated,
  onUploadedResume,
}: WorkspaceViewRouterProps) {
  const clearJobRoute = useCallback(
    () => navigation.navigateToView("jobs"),
    [navigation.navigateToView],
  );
  const clearMatchingRoute = useCallback(
    () => navigation.navigateToView("match"),
    [navigation.navigateToView],
  );
  return (
    <>
      <div className="recruiting-agent-view" hidden={view !== "agent"}>
        <RecruitingAgentPage
          active={view === "agent"}
          conversationStorageScope={agent.conversationStorageScope}
          formatError={feedback.formatError}
          onOpenMailboxSettings={agent.onOpenMailboxSettings}
          onOpenMatchWorkspace={agent.onOpenMatchWorkspace}
          onOpenResume={agent.onOpenResume}
          onOpenScoreWorkspace={agent.onOpenScoreWorkspace}
          onPendingFilterScopeHandled={agent.onPendingFilterScopeHandled}
          pendingFilterScope={agent.pendingFilterScope}
        />
      </div>
      {view === "workbench" && (
        <RecruitingOverview
          formatError={feedback.formatError}
          notify={feedback.notify}
          onOpenAgent={onOpenRecruitingAgent}
          onCreateJob={() => navigation.navigateToView("jobs", { createJob: true })}
          onOpenJobs={() => navigation.navigateToView("jobs")}
          onOpenMatching={() => navigation.navigateToView("match")}
        />
      )}
      {view === "library" && (
        <ResumeLibraryPage
          formatError={feedback.formatError}
          refreshToken={library.refreshToken}
          selectedResumeId={library.selectedResumeId}
          onFavoriteChanged={onFavoriteChanged}
          onOpenResume={onOpenLibraryResume}
          onUpload={() => navigation.navigateToView("upload")}
        />
      )}
      {view === "favorites" && (
        <FavoriteCandidatesPage
          formatError={feedback.formatError}
          onFavoritesChanged={onFavoriteChanged}
          onGoToFilter={() => navigation.navigateToView("filter")}
          onOpenResume={onOpenFavoriteResume}
          refreshToken={library.refreshToken}
        />
      )}
      {view === "filter" && (
        <FilterWorkspace
          appliedDraft={filter.appliedFilter}
          draft={filter.filterDraft}
          filterOptions={filter.filterOptions}
          onDraftChange={filter.updateFilterDraft}
          search={filter.search}
          searching={filter.searching}
          selectedResumeId={library.selectedResumeId}
          onFavoriteChanged={onFavoriteChanged}
          onReset={filter.resetFilter}
          onRefineWithAgent={onRefineWithAgent}
          onOpenCandidate={onOpenCandidate}
          onScoreTemplateChange={filter.changeScoreTemplate}
          onLoadMore={filter.loadMore}
          onUpload={() => navigation.navigateToView("upload")}
          scoreTemplateId={filter.scoreTemplateId}
          scoreTemplates={filter.scoreTemplates}
        />
      )}
      <div hidden={view !== "upload"}>
        <UploadPage
          formatError={feedback.formatError}
          notify={feedback.notify}
          onComplete={onUploadedResume}
        />
      </div>
      {view === "score" && (
        <ScoreWorkspace
          formatError={feedback.formatError}
          notify={feedback.notify}
          onScoreCreated={onScoreCreated}
          onTemplateCreated={onTemplateCreated}
        />
      )}
      {view === "jobs" && (
        <MatchWorkspace
          canGenerateAiJd={permissions.canGenerateAiJd}
          createNewJob={routeParams.createJob}
          formatError={feedback.formatError}
          mode="jobs"
          notify={feedback.notify}
          initialJobVersionId={routeParams.jobVersionId}
          onCreateNewJob={() => navigation.navigateToView("jobs", { createJob: true })}
          onInvalidJobVersion={clearJobRoute}
          onJobVersionChange={(jobVersionId) => navigation.navigateToView(
            "jobs",
            jobVersionId ? { jobVersionId } : {},
          )}
          onOpenMatching={(jobVersionId) => navigation.navigateToView("match", { jobVersionId })}
          onOpenMatchedResume={onOpenMatchedResume}
        />
      )}
      {view === "match" && (
        <MatchWorkspace
          canGenerateAiJd={permissions.canGenerateAiJd}
          formatError={feedback.formatError}
          mode="matching"
          notify={feedback.notify}
          initialJobVersionId={routeParams.jobVersionId}
          onInvalidJobVersion={clearMatchingRoute}
          onJobVersionChange={(jobVersionId) => navigation.navigateToView(
            "match",
            jobVersionId ? { jobVersionId } : {},
          )}
          onOpenJobManagement={() => navigation.navigateToView("jobs")}
          onOpenMatchedResume={onOpenMatchedResume}
        />
      )}
      {view === "settings" && permissions.canManageSettings && (
        <WorkspaceSettingsPage
          activeSection={settingsSection}
          canManageCandidateData={permissions.canManageCandidateData}
          canManageMailbox={permissions.canManageMailbox}
          formatError={feedback.formatError}
          notify={feedback.notify}
          onImported={onLibraryChanged}
          onOpenLibrary={() => navigation.navigateToView("library")}
          onSelectSection={navigation.openSettings}
          role={permissions.role}
        />
      )}
      {view === "feedback" && (
        <WorkspaceFeedbackPage
          formatError={feedback.formatError}
          notify={feedback.notify}
          onRewardGranted={feedback.onRewardGranted}
        />
      )}
    </>
  );
}
