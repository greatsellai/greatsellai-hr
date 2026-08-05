import { useEffect } from "react";
import { api } from "../../api";
import type { JobMatchBatch, JobMatchBatchItem } from "../../types";

const ACTIVE_POLL_INTERVAL_MS = 2_000;
const MAX_RETRY_DELAY_MS = 16_000;

function isTerminalBatchStatus(status: string): boolean {
  return status === "completed" || status === "partial";
}

function retryDelay(failureCount: number): number {
  return Math.min(
    ACTIVE_POLL_INTERVAL_MS * 2 ** Math.min(failureCount, 3),
    MAX_RETRY_DELAY_MS,
  );
}

/**
 * Keeps one durable JD-match batch current without stacking network requests.
 *
 * The old interval fetched both the batch and every item on every tick. The
 * batch already carries progress counts, so active work only needs one status
 * request. Failure details are fetched when their count changes, or when a
 * terminal batch needs them for its report.
 */
export function useJobMatchBatchPolling({
  batchId,
  onBatch,
  onItems,
}: {
  batchId: string | null | undefined;
  onBatch: (batch: JobMatchBatch) => void;
  onItems: (items: JobMatchBatchItem[]) => void;
}) {
  useEffect(() => {
    if (!batchId) return;
    const activeBatchId = batchId;

    let cancelled = false;
    let requestInFlight = false;
    let refreshRequested = false;
    let retryCount = 0;
    let itemDetailRetryCount = 0;
    let lastSyncedFailedCount = -1;
    let lastSyncedTerminalStatus: string | null = null;
    let timer: ReturnType<typeof window.setTimeout> | null = null;

    const clearTimer = () => {
      if (timer === null) return;
      window.clearTimeout(timer);
      timer = null;
    };

    const schedule = (delay: number, refresh: () => Promise<void>) => {
      if (cancelled || document.visibilityState !== "visible") return;
      clearTimer();
      timer = window.setTimeout(() => {
        timer = null;
        void refresh();
      }, delay);
    };

    async function retryTerminalFailureItems(batch: JobMatchBatch): Promise<void> {
      if (cancelled || document.visibilityState !== "visible") return;
      if (requestInFlight) {
        refreshRequested = true;
        return;
      }

      requestInFlight = true;
      try {
        const items = await api.listJobMatchBatchItems(activeBatchId);
        if (cancelled || document.visibilityState !== "visible") return;

        itemDetailRetryCount = 0;
        lastSyncedFailedCount = batch.failed_count;
        lastSyncedTerminalStatus = batch.status;
        onItems(items);
      } catch {
        itemDetailRetryCount += 1;
        schedule(
          retryDelay(itemDetailRetryCount),
          () => retryTerminalFailureItems(batch),
        );
      } finally {
        requestInFlight = false;
        if (
          refreshRequested
          && !cancelled
          && document.visibilityState === "visible"
        ) {
          refreshRequested = false;
          void refresh();
        }
      }
    }

    const refresh = async () => {
      if (cancelled || document.visibilityState !== "visible") return;
      if (requestInFlight) {
        refreshRequested = true;
        return;
      }

      requestInFlight = true;
      let nextBatch: JobMatchBatch | null = null;
      let statusRequestFailed = false;

      try {
        const next = await api.getJobMatchBatch(activeBatchId);
        if (cancelled || document.visibilityState !== "visible") return;

        nextBatch = next;
        retryCount = 0;
        onBatch(next);

        const isTerminal = isTerminalBatchStatus(next.status);
        const shouldSyncFailureItems = next.failed_count > 0 && (
          next.failed_count !== lastSyncedFailedCount
          || (isTerminal && next.status !== lastSyncedTerminalStatus)
        );

        if (next.failed_count === 0 && lastSyncedFailedCount !== 0) {
          lastSyncedFailedCount = 0;
          lastSyncedTerminalStatus = isTerminal ? next.status : null;
          onItems([]);
        } else if (shouldSyncFailureItems) {
          try {
            const items = await api.listJobMatchBatchItems(activeBatchId);
            if (!cancelled && document.visibilityState === "visible") {
              itemDetailRetryCount = 0;
              lastSyncedFailedCount = next.failed_count;
              lastSyncedTerminalStatus = isTerminal ? next.status : null;
              onItems(items);
            }
          } catch {
            // Running batches retry from their next status refresh. A terminal
            // failure report must keep retrying without restarting status polls.
            if (isTerminal) {
              itemDetailRetryCount += 1;
              schedule(
                retryDelay(itemDetailRetryCount),
                () => retryTerminalFailureItems(next),
              );
            }
          }
        }
      } catch {
        statusRequestFailed = true;
        retryCount += 1;
      } finally {
        requestInFlight = false;
        if (cancelled || document.visibilityState !== "visible") return;

        if (refreshRequested) {
          refreshRequested = false;
          void refresh();
          return;
        }

        if (statusRequestFailed) {
          schedule(retryDelay(retryCount), refresh);
          return;
        }

        if (nextBatch && !isTerminalBatchStatus(nextBatch.status)) {
          schedule(ACTIVE_POLL_INTERVAL_MS, refresh);
        }
      }
    };

    const handleVisibilityChange = () => {
      if (document.visibilityState !== "visible") {
        clearTimer();
        return;
      }
      void refresh();
    };

    document.addEventListener("visibilitychange", handleVisibilityChange);
    void refresh();

    return () => {
      cancelled = true;
      clearTimer();
      document.removeEventListener("visibilitychange", handleVisibilityChange);
    };
  }, [batchId, onBatch, onItems]);
}
