import { expect, test } from "@playwright/test";

import {
  RESUME_EXTRACTION_FAILED_RETRY_MESSAGE,
  SERVICE_UNAVAILABLE_RETRY_MESSAGE,
  resumeExtractionUserMessage,
  resumeExtractionStatusMessage,
} from "../src/resume-extraction-user-messages";

test.describe("简历提取普通用户文案", () => {
  test("不暴露提取质量错误码，并统一服务异常提示", () => {
    expect(
      resumeExtractionUserMessage("resume_source_text_unreliable", 409),
    ).toBe(RESUME_EXTRACTION_FAILED_RETRY_MESSAGE);
    expect(
      resumeExtractionUserMessage("resume_source_text_unavailable", 422),
    ).toBe(RESUME_EXTRACTION_FAILED_RETRY_MESSAGE);
    expect(
      resumeExtractionUserMessage(
        "resume_has_no_native_text_for_ai_extraction",
        409,
      ),
    ).toBe(RESUME_EXTRACTION_FAILED_RETRY_MESSAGE);
    expect(
      resumeExtractionUserMessage("unhandled_resume_service_failure", 500),
    ).toBe(SERVICE_UNAVAILABLE_RETRY_MESSAGE);
    expect(
      resumeExtractionUserMessage("tencent_ocr_request_failed", 409),
    ).toBe(SERVICE_UNAVAILABLE_RETRY_MESSAGE);
    expect(
      resumeExtractionUserMessage("office_conversion_timed_out", 409),
    ).toBe(SERVICE_UNAVAILABLE_RETRY_MESSAGE);
    expect(
      resumeExtractionStatusMessage("unreviewed_background_error"),
    ).toBe(SERVICE_UNAVAILABLE_RETRY_MESSAGE);
  });
});
