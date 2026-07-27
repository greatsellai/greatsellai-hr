import type {
  CandidateSearchRequest,
  CandidateSearchItem,
  JobMatch,
  ResumeLibraryItem,
  ScoreTemplate,
} from "../../types";
import { ResumeLibraryPage } from "../library/ResumeLibraryPage";
import { FilterWorkspace } from "../filter/FilterWorkspace";
import { type useCandidateSearchController } from "../filter/useCandidateSearchController";
import { ScoreWorkspace } from "../scoring/ScoreWorkspace";
import { MatchWorkspace } from "../job-match/MatchWorkspace";
import { UploadPage } from "../upload/UploadPage";
import { WorkspaceSettingsPage } from "../workspace-settings/WorkspaceSettingsPage";
import type { CandidateDrawerTab } from "../candidate-drawer/candidate-drawer-types";
import type {
  WorkspaceNavigationView,
  WorkspaceSettingsSection,
  WorkspaceView,
} from "./workspace-navigation-types";

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
  feedback: {
    formatError: (error: unknown) => string;
    notify: (kind: ToastKind, message: string) => void;
  };
  filter: FilterWorkspaceController;
  library: {
    refreshToken: number;
    selectedResumeId: string | null;
  };
  navigation: {
    navigateToView: (view: WorkspaceNavigationView) => void;
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
  view: WorkspaceView;
  onLibraryChanged: () => void;
  onOpenCandidate: (
    item: CandidateSearchItem,
    tab?: CandidateDrawerTab,
  ) => void;
  onOpenLibraryResume: (item: ResumeLibraryItem) => void;
  onOpenMatchedResume: (match: JobMatch) => void;
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
  feedback,
  filter,
  library,
  navigation,
  permissions,
  settingsSection,
  view,
  onLibraryChanged,
  onOpenCandidate,
  onOpenLibraryResume,
  onOpenMatchedResume,
  onRefineWithAgent,
  onScoreCreated,
  onTemplateCreated,
  onUploadedResume,
}: WorkspaceViewRouterProps) {
  return (
    <>
      {view === "library" && (
        <ResumeLibraryPage
          formatError={feedback.formatError}
          refreshToken={library.refreshToken}
          selectedResumeId={library.selectedResumeId}
          onOpenResume={onOpenLibraryResume}
          onUpload={() => navigation.navigateToView("upload")}
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
      {view === "match" && (
        <MatchWorkspace
          canGenerateAiJd={permissions.canGenerateAiJd}
          formatError={feedback.formatError}
          notify={feedback.notify}
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
    </>
  );
}
