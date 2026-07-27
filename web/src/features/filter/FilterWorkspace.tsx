import { FilterPanel } from "./FilterPanel";
import { ResultsPane } from "./ResultsPane";
import { draftToSearchRequest } from "./filter-search-model";
import type { FilterDraft } from "./filter-model";
import type {
  CandidateSearchRequest,
  CandidateSearchItem,
  CandidateSearchResponse,
  FilterOptions,
  ScoreTemplate,
} from "../../types";
import "./filter-workspace.css";

export function FilterWorkspace({
  appliedDraft,
  draft,
  filterOptions,
  onDraftChange,
  search,
  searching,
  selectedResumeId,
  onReset,
  onRefineWithAgent,
  onOpenCandidate,
  onScoreTemplateChange,
  onLoadMore,
  onUpload,
  scoreTemplateId,
  scoreTemplates,
}: {
  appliedDraft: FilterDraft;
  draft: FilterDraft;
  filterOptions: FilterOptions;
  onDraftChange: (draft: FilterDraft, timing?: "immediate" | "debounced") => void;
  search: CandidateSearchResponse;
  searching: boolean;
  selectedResumeId: string | null;
  onReset: () => void;
  onRefineWithAgent: (filter: CandidateSearchRequest, totalCount: number) => void;
  onOpenCandidate: (item: CandidateSearchItem, tab?: "score") => void;
  onScoreTemplateChange: (templateId: string | null) => void;
  onLoadMore: () => void;
  onUpload: () => void;
  scoreTemplateId: string | null;
  scoreTemplates: ScoreTemplate[];
}) {
  return (
    <div className="filter-workspace">
      <FilterPanel
        draft={draft}
        filterOptions={filterOptions}
        onDraftChange={onDraftChange}
        onReset={onReset}
      />
      <ResultsPane
        appliedDraft={appliedDraft}
        onLoadMore={onLoadMore}
        onOpenCandidate={onOpenCandidate}
        onReset={onReset}
        onRefineWithAgent={() => {
          const { cursor: _cursor, limit: _limit, score_template_id: _scoreTemplateId, ...filter } =
            draftToSearchRequest(appliedDraft);
          onRefineWithAgent(filter, search.total_count);
        }}
        onScoreTemplateChange={onScoreTemplateChange}
        onUpload={onUpload}
        search={search}
        searching={searching}
        selectedResumeId={selectedResumeId}
        scoreTemplateId={scoreTemplateId}
        scoreTemplates={scoreTemplates}
      />
    </div>
  );
}
