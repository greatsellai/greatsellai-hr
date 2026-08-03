import type { CandidateSearchItem } from "../../types";

export type CandidateScoreConfidenceTone = "grounded" | "partial" | "unknown";

export function candidateScoreConfidencePresentation(value: number | null): {
  label: string;
  tone: CandidateScoreConfidenceTone;
} {
  if (value === null) return { label: "待核实", tone: "unknown" };
  if (value >= 80) return { label: `可信度 ${value.toFixed(0)}%`, tone: "grounded" };
  if (value >= 50) return { label: `可信度 ${value.toFixed(0)}%`, tone: "partial" };
  return { label: `待核实 · ${value.toFixed(0)}%`, tone: "unknown" };
}

/**
 * This is intentionally conservative: every item is derived from a missing
 * source-grounded field or an existing score lifecycle status. The workbench
 * never treats a missing fact as a negative candidate attribute.
 */
export function candidatePendingReasons(item: CandidateSearchItem): string[] {
  const reasons: string[] = [];
  if (!item.education_school || !item.highest_degree) {
    reasons.push("教育背景信息不完整");
  }
  if ((item.employment_or_internship_months ?? 0) <= 0) {
    reasons.push("经历起止时间待核验");
  }
  if (item.score_total === null) {
    reasons.push("尚未按当前评分口径完成评分");
  } else if (item.score_status === "needs_review") {
    reasons.push("评分结果建议人工复核");
  } else if (item.score_confidence === null || item.score_confidence < 80) {
    reasons.push("评分依据仍有待核验事实");
  }
  return reasons;
}

export function candidateIsPriority(item: CandidateSearchItem): boolean {
  return (
    item.score_total !== null &&
    item.score_total >= 80 &&
    item.score_confidence !== null &&
    item.score_confidence >= 80
  );
}

export function candidateNeedsReview(item: CandidateSearchItem): boolean {
  return candidatePendingReasons(item).length > 0;
}
