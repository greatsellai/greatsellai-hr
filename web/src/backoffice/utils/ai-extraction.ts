import type { AiSummaryStatus } from "../../types";

/** Shared polling cadence for workbench surfaces that show AI extraction state. */
export const AI_STATUS_POLL_INTERVAL_MS = 2_500;

export function aiExtractionIsInProgress(
  status: string | null | undefined,
): boolean {
  return status === "queued" || status === "running";
}

/** The automatic summary is still waiting for, or using, the AI worker. */
export function aiSummaryIsInProgress(
  status: AiSummaryStatus | undefined,
): boolean {
  return status === "queued" || status === "running";
}
