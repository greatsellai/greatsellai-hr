import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../../api";
import type {
  CandidateSearchResponse,
  FilterOptions,
  SavedFilter,
  ScoreTemplate,
} from "../../types";
import type { FilterDraft } from "./filter-model";
import {
  draftToSearchRequest,
  emptyCandidateSearch,
  fallbackFilterOptions,
  freshDefaultFilter,
  searchRequestToDraft,
  snapshotFilterDraft,
} from "./filter-search-model";

const AUTO_FILTER_SEARCH_DELAY_MS = 350;

type ToastKind = "success" | "error";
export type FilterSearchTiming = "immediate" | "debounced";

interface UseCandidateSearchControllerOptions {
  enabled: boolean;
  formatError: (error: unknown) => string;
  notify: (kind: ToastKind, message: string) => void;
}

/**
 * Owns the result set and the editable query that powers the filtering
 * workbench. The workspace shell remains responsible for navigation, opening
 * a candidate, and keeping the library in sync after a scoring job finishes.
 */
export function useCandidateSearchController({
  enabled,
  formatError,
  notify,
}: UseCandidateSearchControllerOptions) {
  const [filterDraft, setFilterDraft] = useState<FilterDraft>(
    freshDefaultFilter,
  );
  const [appliedFilter, setAppliedFilter] = useState<FilterDraft>(
    freshDefaultFilter,
  );
  const [filterOptions, setFilterOptions] = useState<FilterOptions>(
    fallbackFilterOptions,
  );
  const [scoreTemplates, setScoreTemplates] = useState<ScoreTemplate[]>([]);
  const [scoreTemplateId, setScoreTemplateId] = useState<string | null>(null);
  const [search, setSearch] = useState<CandidateSearchResponse>(
    emptyCandidateSearch,
  );
  const [searching, setSearching] = useState(false);
  const [savedFilters, setSavedFilters] = useState<SavedFilter[]>([]);
  const filterDraftRef = useRef(filterDraft);
  const appliedFilterRef = useRef(appliedFilter);
  const scoreTemplateIdRef = useRef<string | null>(null);
  const searchRequestRef = useRef(0);
  const scheduledFilterSearchRef = useRef<number | null>(null);

  const replaceFilterDraft = useCallback((next: FilterDraft) => {
    filterDraftRef.current = next;
    setFilterDraft(next);
  }, []);

  const cancelScheduledFilterSearch = useCallback(() => {
    if (scheduledFilterSearchRef.current === null) return;
    window.clearTimeout(scheduledFilterSearchRef.current);
    scheduledFilterSearchRef.current = null;
  }, []);

  const replaceAppliedFilter = useCallback((next: FilterDraft) => {
    const snapshot = snapshotFilterDraft(next);
    appliedFilterRef.current = snapshot;
    setAppliedFilter(snapshot);
  }, []);

  const replaceScoreTemplateId = useCallback((next: string | null) => {
    scoreTemplateIdRef.current = next;
    setScoreTemplateId(next);
  }, []);

  const refreshSavedFilters = useCallback(async () => {
    try {
      setSavedFilters(await api.listSavedFilters());
    } catch (error) {
      notify("error", formatError(error));
    }
  }, [formatError, notify]);

  const runSearch = useCallback(
    async (
      draft: FilterDraft,
      append = false,
      cursor: string | null = null,
      selectedScoreTemplateId: string | null = scoreTemplateIdRef.current,
    ) => {
      const requestId = ++searchRequestRef.current;
      setSearching(true);
      try {
        const response = await api.searchCandidates(
          draftToSearchRequest(draft, cursor, selectedScoreTemplateId),
        );
        if (requestId !== searchRequestRef.current) return;
        setSearch((current) => ({
          ...response,
          items: append
            ? [...current.items, ...response.items]
            : response.items,
        }));
        if (!append) replaceAppliedFilter(draft);
      } catch (error) {
        if (requestId === searchRequestRef.current) {
          notify("error", formatError(error));
        }
      } finally {
        if (requestId === searchRequestRef.current) setSearching(false);
      }
    },
    [formatError, notify, replaceAppliedFilter],
  );

  const updateFilterDraft = useCallback(
    (next: FilterDraft, timing: FilterSearchTiming = "immediate") => {
      replaceFilterDraft(next);
      cancelScheduledFilterSearch();
      if (timing === "immediate") {
        void runSearch(next);
        return;
      }

      // Invalidate an in-flight result while the recruiter keeps typing, so
      // an older request cannot briefly present rows for stale conditions.
      searchRequestRef.current += 1;
      setSearching(true);
      scheduledFilterSearchRef.current = window.setTimeout(() => {
        scheduledFilterSearchRef.current = null;
        void runSearch(next);
      }, AUTO_FILTER_SEARCH_DELAY_MS);
    },
    [cancelScheduledFilterSearch, replaceFilterDraft, runSearch],
  );

  useEffect(
    () => () => cancelScheduledFilterSearch(),
    [cancelScheduledFilterSearch],
  );

  const registerScoreTemplate = useCallback(
    (template: ScoreTemplate) => {
      setScoreTemplates((current) => [
        template,
        ...current.filter((item) => item.template_id !== template.template_id),
      ]);
      replaceScoreTemplateId(template.template_id);
      void runSearch(
        appliedFilterRef.current,
        false,
        null,
        template.template_id,
      );
    },
    [replaceScoreTemplateId, runSearch],
  );

  useEffect(() => {
    if (!enabled) return;

    // Keep the current startup sequence intact: immediately show the default
    // result set, then refresh with the default score template when one exists.
    void runSearch(freshDefaultFilter());
    void refreshSavedFilters();
    void api
      .getFilterOptions()
      .then((options) => {
        setFilterOptions({
          ...fallbackFilterOptions,
          ...options,
          institution_classifications: options.institution_classifications?.length
            ? options.institution_classifications
            : fallbackFilterOptions.institution_classifications,
        });
      })
      .catch(() => {
        setFilterOptions(fallbackFilterOptions);
      });
    void api
      .listScoreTemplates()
      .then((templates) => {
        setScoreTemplates(templates);
        const defaultTemplateId = templates[0]?.template_id ?? null;
        replaceScoreTemplateId(defaultTemplateId);
        if (defaultTemplateId) {
          void runSearch(
            appliedFilterRef.current,
            false,
            null,
            defaultTemplateId,
          );
        }
      })
      .catch(() => {
        setScoreTemplates([]);
        replaceScoreTemplateId(null);
      });
  }, [enabled, refreshSavedFilters, replaceScoreTemplateId, runSearch]);

  const resetFilter = useCallback(async () => {
    cancelScheduledFilterSearch();
    const clean = freshDefaultFilter();
    replaceFilterDraft(clean);
    await runSearch(clean);
  }, [cancelScheduledFilterSearch, replaceFilterDraft, runSearch]);

  const changeScoreTemplate = useCallback(
    (nextTemplateId: string | null) => {
      replaceScoreTemplateId(nextTemplateId);
      void runSearch(appliedFilterRef.current, false, null, nextTemplateId);
    },
    [replaceScoreTemplateId, runSearch],
  );

  const saveCurrentFilter = useCallback(
    async (name: string) => {
      const normalized = name.trim();
      if (!normalized) {
        notify("error", "请为这组筛选条件填写一个名称。");
        return;
      }
      try {
        await api.createSavedFilter({
          name: normalized,
          filters: draftToSearchRequest(filterDraftRef.current),
        });
        await refreshSavedFilters();
        notify("success", `已保存“${normalized}”。`);
      } catch (error) {
        notify("error", formatError(error));
      }
    },
    [formatError, notify, refreshSavedFilters],
  );

  const applySavedFilter = useCallback(
    (filter: SavedFilter): boolean => {
      const result = searchRequestToDraft(filter.filters);
      if (!result.draft) {
        notify("error", result.error);
        return false;
      }
      cancelScheduledFilterSearch();
      replaceFilterDraft(result.draft);
      void runSearch(result.draft);
      return true;
    },
    [cancelScheduledFilterSearch, notify, replaceFilterDraft, runSearch],
  );

  const deleteSavedFilter = useCallback(
    async (filter: SavedFilter) => {
      try {
        await api.deleteSavedFilter(filter.saved_filter_id);
        await refreshSavedFilters();
        notify("success", `已删除“${filter.name}”。`);
      } catch (error) {
        notify("error", formatError(error));
      }
    },
    [formatError, notify, refreshSavedFilters],
  );

  const loadMore = useCallback(() => {
    void runSearch(appliedFilterRef.current, true, search.next_cursor);
  }, [runSearch, search.next_cursor]);

  const refreshCurrentResults = useCallback(() => {
    void runSearch(appliedFilterRef.current);
  }, [runSearch]);

  return {
    appliedFilter,
    applySavedFilter,
    changeScoreTemplate,
    deleteSavedFilter,
    filterDraft,
    filterOptions,
    loadMore,
    refreshCurrentResults,
    registerScoreTemplate,
    resetFilter,
    savedFilters,
    scoreTemplateId,
    scoreTemplates,
    search,
    searching,
    saveCurrentFilter,
    updateFilterDraft,
  };
}
