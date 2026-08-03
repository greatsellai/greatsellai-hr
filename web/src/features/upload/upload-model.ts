import type { ResumeDetail, ResumeUploadResponse } from "../../types";
import { hasSourceTextQualityIssue } from "../../backoffice/utils/resume-source-quality";

export type UploadStatus =
  | "queued"
  | "uploading"
  | "extracting"
  | "success"
  | "attention"
  | "error";

export interface UploadQueueItem {
  id: string;
  file: File;
  status: UploadStatus;
  idempotencyKey: string;
  response?: ResumeUploadResponse;
  error?: string;
  retryable?: boolean;
}

/** Keep each imported resume on a predictable, serial persistence path. */
export const BATCH_UPLOAD_CONCURRENCY = 1;
export const MAX_BATCH_FILES = 100;

export function uploadStatusFromResponse(
  response: ResumeUploadResponse,
): UploadStatus {
  if (
    response.extraction_status === "failed" ||
    hasSourceTextQualityIssue(response.quality_flags)
  ) {
    return "attention";
  }
  if (response.ai_extraction_status === "completed") return "success";
  if (
    response.ai_extraction_status === "needs_attention" ||
    response.ai_extraction_status === "unavailable"
  ) {
    return "attention";
  }
  return "extracting";
}

export function withLatestAiExtractionStatus(
  uploaded: ResumeUploadResponse,
  detail: ResumeDetail,
): ResumeUploadResponse {
  return {
    ...uploaded,
    candidate_display_name: detail.candidate_display_name,
    extraction_status: detail.extraction_status,
    ai_extraction_status: detail.ai_extraction_status,
    ai_extraction_error: detail.ai_extraction_error,
    candidate_name_extraction_status: detail.candidate_name_extraction_status,
    candidate_name_extraction_error: detail.candidate_name_extraction_error,
    ai_summary_status: detail.ai_summary_status,
    ai_summary_error: detail.ai_summary_error,
    source_page_count: detail.source_page_count,
    parsed_page_count: detail.parsed_page_count,
    quality_flags: detail.quality_flags,
  };
}

export function fileFingerprint(file: File): string {
  return `${file.name.toLocaleLowerCase()}-${file.size}-${file.lastModified}`;
}

export function createUploadIdempotencyKey(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `upload-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}
