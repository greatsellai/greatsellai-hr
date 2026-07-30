import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../../api";
import { BackofficeButton } from "../../backoffice/ui/BackofficeButton";
import { TableSkeleton } from "../../backoffice/ui/TableSkeleton";
import { formatLibraryDate } from "../../backoffice/utils/formatters";
import { Icon } from "../../icons";
import type {
  CandidateFavoriteListResponse,
  CandidateResumeVersionPreview,
  FavoriteCandidateItem,
} from "../../types";
import "./favorite-candidates.css";

const FAVORITE_CANDIDATES_PAGE_SIZE = 50;

interface FavoriteCandidatesPageProps {
  formatError: (error: unknown) => string;
  onFavoritesChanged?: () => void;
  onGoToFilter: () => void;
  onOpenResume?: (
    resumeId: string,
    candidateId: string,
    candidateName: string | null,
  ) => void;
  refreshToken: number;
}

function candidateName(item: FavoriteCandidateItem): string {
  return item.display_name?.trim() || "未命名候选人";
}

function currentResumeVersion(
  item: FavoriteCandidateItem,
): CandidateResumeVersionPreview | null {
  return (
    item.resume_versions.find(
      (version) => version.resume_id === item.current_resume_id,
    ) ?? item.resume_versions.find((version) => version.is_active) ??
    item.resume_versions[0] ??
    null
  );
}

/**
 * A private, candidate-level reading list.  The API groups all live resume
 * versions under one candidate, so this page never duplicates source files,
 * scores, summaries, or AI output just to create a favorite.
 */
export function FavoriteCandidatesPage({
  formatError,
  onFavoritesChanged,
  onGoToFilter,
  onOpenResume,
  refreshToken,
}: FavoriteCandidatesPageProps) {
  const [response, setResponse] = useState<CandidateFavoriteListResponse | null>(
    null,
  );
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [removingCandidateId, setRemovingCandidateId] = useState<string | null>(
    null,
  );
  const latestRequestIdRef = useRef(0);

  const loadFavorites = useCallback(async () => {
    const requestId = ++latestRequestIdRef.current;
    setLoading(true);
    setError(null);
    try {
      const next = await api.listCandidateFavorites(
        page,
        FAVORITE_CANDIDATES_PAGE_SIZE,
      );
      if (latestRequestIdRef.current === requestId) {
        setResponse(next);
      }
    } catch (loadError) {
      if (latestRequestIdRef.current === requestId) {
        setError(formatError(loadError));
      }
    } finally {
      if (latestRequestIdRef.current === requestId) {
        setLoading(false);
      }
    }
  }, [formatError, page]);

  useEffect(() => {
    void loadFavorites();
  }, [loadFavorites, refreshToken]);

  const items = response?.items ?? [];
  const total = response?.total ?? 0;
  const totalPages = Math.max(
    1,
    Math.ceil(total / FAVORITE_CANDIDATES_PAGE_SIZE),
  );
  const firstItemIndex = total
    ? (page - 1) * FAVORITE_CANDIDATES_PAGE_SIZE + 1
    : 0;
  const lastItemIndex = Math.min(page * FAVORITE_CANDIDATES_PAGE_SIZE, total);

  const pageItems = useMemo(
    () =>
      items.map((item) => ({
        item,
        currentVersion: currentResumeVersion(item),
      })),
    [items],
  );

  const removeFavorite = async (item: FavoriteCandidateItem) => {
    if (removingCandidateId) return;

    setRemovingCandidateId(item.candidate_id);
    setError(null);
    // A pending read must not restore a row that was just removed locally.
    latestRequestIdRef.current += 1;
    setLoading(false);
    try {
      await api.unfavoriteCandidate(item.candidate_id);
      const nextItems = items.filter(
        (candidate) => candidate.candidate_id !== item.candidate_id,
      );
      const nextTotal = Math.max(0, total - 1);
      setResponse((current) =>
        current
          ? {
              ...current,
              items: current.items.filter(
                (candidate) => candidate.candidate_id !== item.candidate_id,
              ),
              total: Math.max(0, current.total - 1),
            }
          : current,
      );
      onFavoritesChanged?.();

      if (!nextItems.length && nextTotal > 0 && page > 1) {
        setPage((current) => Math.max(1, current - 1));
      }
    } catch (removeError) {
      setError(formatError(removeError));
    } finally {
      setRemovingCandidateId(null);
    }
  };

  return (
    <section
      aria-labelledby="favorite-candidates-page-title"
      className="page-frame favorite-candidates-page"
    >
      <header className="page-heading favorite-candidates-heading">
        <div>
          <h1 id="favorite-candidates-page-title">我的收藏</h1>
          <p>
            仅当前登录账号在此工作区的收藏，其他成员不可见。
          </p>
        </div>
        <BackofficeButton
          disabled={loading}
          icon={loading ? undefined : <Icon name="refresh" size={16} />}
          loading={loading}
          onClick={() => void loadFavorites()}
        >
          刷新
        </BackofficeButton>
      </header>

      {error && (
        <p className="library-error" role="alert">
          {error}
        </p>
      )}

      <section
        aria-label="我的收藏候选人列表"
        className="favorite-candidates-table-frame"
      >
        {loading && !response ? (
          <TableSkeleton />
        ) : pageItems.length ? (
          <div
            aria-label="我的收藏候选人列表，可横向滚动查看全部字段"
            className="table-scroll"
            role="region"
            tabIndex={0}
          >
            <table className="candidate-table favorite-candidates-table">
              <thead>
                <tr>
                  <th scope="col">候选人</th>
                  <th scope="col">当前简历版本</th>
                  <th scope="col">版本数量</th>
                  <th scope="col">收藏时间</th>
                  <th scope="col">操作</th>
                </tr>
              </thead>
              <tbody>
                {pageItems.map(({ item, currentVersion }) => {
                  const name = candidateName(item);
                  const canOpen = Boolean(currentVersion && onOpenResume);
                  const removing = removingCandidateId === item.candidate_id;
                  return (
                    <tr key={item.candidate_id}>
                      <td>
                        <div className="candidate-person">
                          <strong className="candidate-name">{name}</strong>
                          <span className="candidate-meta">
                            收藏的是候选人，所有简历版本保留在同一详情中。
                          </span>
                        </div>
                      </td>
                      <td>
                        {currentVersion ? (
                          <div className="favorite-current-version">
                            <strong title={currentVersion.original_filename}>
                              {currentVersion.original_filename}
                            </strong>
                            <span>
                              {currentVersion.is_active ? "当前启用版本" : "可查看版本"}
                            </span>
                          </div>
                        ) : (
                          <span className="favorite-version-missing">暂无可查看简历</span>
                        )}
                      </td>
                      <td>
                        <span className="favorite-version-count">
                          {item.resume_versions.length} 个版本
                        </span>
                      </td>
                      <td>
                        <time
                          className="candidate-meta"
                          dateTime={item.favorited_at}
                        >
                          {formatLibraryDate(item.favorited_at)}
                        </time>
                      </td>
                      <td>
                        <div className="favorite-row-actions">
                          <button
                            aria-label={`查看 ${name} 的简历详情`}
                            className="favorite-row-action"
                            disabled={!canOpen}
                            onClick={() => {
                              if (!currentVersion || !onOpenResume) return;
                              onOpenResume(
                                currentVersion.resume_id,
                                item.candidate_id,
                                item.display_name,
                              );
                            }}
                            type="button"
                          >
                            查看简历 <Icon name="chevron-right" size={15} />
                          </button>
                          <button
                            aria-label={`取消收藏 ${name}`}
                            className="favorite-row-action is-unfavorite"
                            disabled={removing}
                            onClick={() => void removeFavorite(item)}
                            type="button"
                          >
                            {removing ? <><i className="spinner" />正在取消</> : "取消收藏"}
                          </button>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="empty-state favorite-candidates-empty">
            <div className="empty-state-inner">
              <span className="empty-glyph">
                <Icon name="bookmark" size={24} />
              </span>
              <h2>还没有收藏候选人</h2>
              <p>
                在条件筛选或候选人详情中收藏后，会仅显示在当前账号的这个工作区。
              </p>
              <BackofficeButton
                icon={<Icon name="filter" size={16} />}
                onClick={onGoToFilter}
                tone="primary"
              >
                前往条件筛选
              </BackofficeButton>
            </div>
          </div>
        )}
      </section>

      <footer className="favorite-candidates-footer">
        <span>
          {loading && response ? (
            <span className="loading-line">
              <i className="spinner" /> 正在更新收藏…
            </span>
          ) : total ? (
            `显示第 ${firstItemIndex}–${lastItemIndex} 位，共 ${total} 位候选人`
          ) : (
            "共 0 位候选人"
          )}
        </span>
        {totalPages > 1 && (
          <div className="pagination">
            <BackofficeButton
              disabled={page <= 1 || loading}
              onClick={() => setPage((current) => Math.max(1, current - 1))}
            >
              上一页
            </BackofficeButton>
            <span>
              第 {page} / {totalPages} 页
            </span>
            <BackofficeButton
              disabled={page >= totalPages || loading}
              onClick={() => setPage((current) => current + 1)}
            >
              下一页
            </BackofficeButton>
          </div>
        )}
      </footer>
    </section>
  );
}
