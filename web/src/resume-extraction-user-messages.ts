export const RESUME_EXTRACTION_FAILED_LABEL = "简历提取失败";
export const RESUME_EXTRACTION_FAILED_RETRY_MESSAGE =
  "简历提取失败。请重新上传清晰、完整的原文件后重试。";
export const SERVICE_UNAVAILABLE_RETRY_MESSAGE =
  "服务暂时不可用，请稍后重试。";

const RESUME_EXTRACTION_FAILURE_CODES = new Set([
  "document_open_failed",
  "document_page_limit_exceeded",
  "document_text_limit_exceeded",
  "html_open_failed",
  "invalid_document_signature",
  "resume_has_no_native_text_for_ai_extraction",
  "resume_source_text_unavailable",
  "resume_source_text_unreliable",
  "spreadsheet_cell_limit_exceeded",
  "spreadsheet_open_failed",
  "spreadsheet_row_limit_exceeded",
  "spreadsheet_sheet_limit_exceeded",
]);

const SERVICE_UNAVAILABLE_ERROR_PREFIXES = [
  "document_extraction_",
  "office_conversion_",
  "spreadsheet_conversion_",
  "tencent_ocr_",
];

/**
 * Keeps technical extraction codes out of ordinary recruiting-facing UI while
 * preserving them unchanged for API clients, audit logs, and operations.
 */
export function resumeExtractionUserMessage(
  errorCode: string | null | undefined,
  status?: number,
): string | null {
  const normalizedErrorCode = errorCode?.trim() ?? "";
  if (RESUME_EXTRACTION_FAILURE_CODES.has(normalizedErrorCode)) {
    return RESUME_EXTRACTION_FAILED_RETRY_MESSAGE;
  }
  if (
    SERVICE_UNAVAILABLE_ERROR_PREFIXES.some((prefix) =>
      normalizedErrorCode.startsWith(prefix),
    )
  ) {
    return SERVICE_UNAVAILABLE_RETRY_MESSAGE;
  }
  if (typeof status === "number" && status >= 500) {
    return SERVICE_UNAVAILABLE_RETRY_MESSAGE;
  }
  return null;
}

/** A background job error is never safe to render as an unreviewed raw code. */
export function resumeExtractionStatusMessage(
  errorCode: string | null | undefined,
): string {
  return (
    resumeExtractionUserMessage(errorCode) ??
    SERVICE_UNAVAILABLE_RETRY_MESSAGE
  );
}
