import { useCallback, useEffect, useState } from "react";
import { FilterPanel } from "./FilterPanel";
import { ResultsPane } from "./ResultsPane";
import { CandidateInspector } from "./CandidateInspector";
import { draftToSearchRequest } from "./filter-search-model";
import type { FilterDraft } from "./filter-model";
import type {
  CandidateSearchRequest,
  CandidateSearchItem,
  CandidateSearchResponse,
  FilterOptions,
  ScoreTemplate,
} from "../../types";
import type { CandidateDrawerTab } from "../candidate-drawer/candidate-drawer-types";
import "./filter-workspace.css";
import "./candidate-inspector.css";

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
  onFavoriteChanged,
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
  onOpenCandidate: (item: CandidateSearchItem, tab?: CandidateDrawerTab) => void;
  onScoreTemplateChange: (templateId: string | null) => void;
  onLoadMore: () => void;
  onFavoriteChanged?: () => void;
  onUpload: () => void;
  scoreTemplateId: string | null;
  scoreTemplates: ScoreTemplate[];
}) {
  const [selectedCandidate, setSelectedCandidate] = useState<CandidateSearchItem | null>(null);
  const [filtersOpen, setFiltersOpen] = useState(false);

  const selectCandidate = useCallback((candidate: CandidateSearchItem | null) => {
    setSelectedCandidate(candidate);
  }, []);

  useEffect(() => {
    if (!selectedResumeId) return;
    const matchingCandidate = search.items.find((item) => item.resume_id === selectedResumeId);
    if (matchingCandidate) setSelectedCandidate(matchingCandidate);
  }, [search.items, selectedResumeId]);

  return (
    <div className="filter-workspace">
      {filtersOpen && (
        <>
          <button
            aria-label="关闭初筛"
            className="filter-panel-backdrop"
            onClick={() => setFiltersOpen(false)}
            tabIndex={-1}
            type="button"
          />
          <FilterPanel
            draft={draft}
            filterOptions={filterOptions}
            onClose={() => setFiltersOpen(false)}
            onDraftChange={onDraftChange}
            onReset={onReset}
          />
        </>
      )}
      <ResultsPane
        appliedDraft={appliedDraft}
        filtersOpen={filtersOpen}
        onLoadMore={onLoadMore}
        onFavoriteChanged={onFavoriteChanged}
        onOpenCandidate={onOpenCandidate}
        onSelectCandidate={selectCandidate}
        onReset={onReset}
        onRefineWithAgent={() => {
          const { cursor: _cursor, limit: _limit, score_template_id: _scoreTemplateId, ...filter } =
            draftToSearchRequest(appliedDraft);
          onRefineWithAgent(filter, search.total_count);
        }}
        onScoreTemplateChange={onScoreTemplateChange}
        onToggleFilters={() => setFiltersOpen((current) => !current)}
        onUpload={onUpload}
        search={search}
        searching={searching}
        scoreTemplateId={scoreTemplateId}
        scoreTemplates={scoreTemplates}
        selectedCandidateId={selectedCandidate?.candidate_id ?? null}
      />
      <CandidateInspector
        candidate={selectedCandidate}
        onFavoriteChanged={onFavoriteChanged}
        onOpenCandidate={onOpenCandidate}
      />
    </div>
  );
}
