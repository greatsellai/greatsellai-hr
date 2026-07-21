import type {
  AuthLoginInput,
  AuthRegistrationInput,
  AuthSession,
  CandidateCreateInput,
  CandidateCreated,
  CandidateDataAuditEventList,
  CandidateDataDeletionBatchList,
  CandidateDataDeletionRequest,
  CandidateDataDeletionResponse,
  CandidateDataExport,
  CandidateDataExportCreate,
  CandidateDataExportList,
  CandidateDataFileAccessPurpose,
  CandidateDataFileAccessResponse,
  CandidateDataRestoreResponse,
  CandidateDataRetentionCleanupRun,
  CandidateDataRetentionCleanupRunList,
  CandidateDataRetentionMode,
  CandidateDataRetentionPolicy,
  CandidateDataRetentionPreview,
  CandidateSearchRequest,
  CandidateSearchResponse,
  FilterOptions,
  JobCreate,
  JobDescriptionGenerateInput,
  JobDescriptionGeneration,
  JobMatchBatch,
  JobMatchBatchItem,
  JobMatch,
  JobMatchCreate,
  JobVersion,
  JobVersionRequirementsUpdate,
  MailboxConfig,
  MailboxConfigCreate,
  MailboxConfigList,
  MailboxConfigPatch,
  MailboxBackgroundJob,
  MailboxBackgroundJobBatch,
  MailboxBackgroundJobHistory,
  MailboxImportHistory,
  MailboxRetentionOverview,
  MailboxRetentionPreview,
  MailboxRetentionRun,
  MailboxRetentionRuns,
  MailboxRetentionUpdate,
  EmailVerificationResendResult,
  OriginalJobPublishInput,
  PasswordResetRequestResult,
  RegistrationOffer,
  ResumeActivateRequest,
  ResumeDetail,
  ResumeFactsSaveRequest,
  ResumeLibraryResponse,
  ResumeReviewDetail,
  ResumeReviewQueueResponse,
  ResumeScore,
  ResumeScoreBatch,
  ResumeScoreBatchItem,
  ResumeScoreCreate,
  ResumeScoreOverride,
  ResumeSummary,
  ResumeSummaryManualCreate,
  ResumeUploadResponse,
  RecruitingAgentTurn,
  RecruitingAgentTurnInput,
  SavedFilter,
  SavedFilterCreate,
  ScoreTemplate,
  ScoreTemplateCreate,
} from "./types";

export * from "./types";

export interface ApiClientOptions {
  /** Defaults to the current entry's same-origin API prefix. */
  baseUrl?: string;
}

export class ApiError extends Error {
  readonly status: number;
  readonly payload: unknown;

  constructor(status: number, message: string, payload: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.payload = payload;
  }
}

export function isApiError(error: unknown): error is ApiError {
  return error instanceof ApiError;
}

function defaultApiBaseUrl(): string {
  if (typeof window === "undefined") return "/v1";
  const compatibilityBase = "/greatsellhr";
  const { pathname } = window.location;
  return pathname === compatibilityBase || pathname.startsWith(`${compatibilityBase}/`)
    ? `${compatibilityBase}/v1`
    : "/v1";
}

function normalizeBaseUrl(baseUrl: string | undefined): string {
  const normalized = (baseUrl?.trim() || defaultApiBaseUrl()).replace(/\/+$/, "");
  return normalized || "/v1";
}

function endpoint(baseUrl: string, path: string): string {
  return `${baseUrl}${path.startsWith("/") ? path : `/${path}`}`;
}

function resourcePath(value: string): string {
  return encodeURIComponent(value);
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

async function responsePayload(response: Response): Promise<unknown> {
  const contentType = response.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    try {
      return await response.json();
    } catch {
      return null;
    }
  }

  try {
    return await response.text();
  } catch {
    return null;
  }
}

function errorMessage(status: number, payload: unknown): string {
  if (isPlainObject(payload) && typeof payload.detail === "string") {
    return payload.detail;
  }
  if (typeof payload === "string" && payload.trim()) {
    return payload;
  }
  return `Request failed with status ${status}`;
}

interface JsonRequestOptions extends Omit<RequestInit, "body"> {
  body?: unknown;
}

export interface ResumePdfObjectUrl {
  url: string;
  revoke: () => void;
}

/** A typed same-origin client authenticated exclusively by the session cookie. */
export function createApiClient(options: ApiClientOptions = {}) {
  const baseUrl = normalizeBaseUrl(options.baseUrl);

  function headers(initial?: HeadersInit): Headers {
    return new Headers(initial);
  }

  async function request<T>(path: string, init: JsonRequestOptions = {}): Promise<T> {
    const { body, headers: initialHeaders, ...requestInit } = init;
    const requestHeaders = headers(initialHeaders);
    let requestBody: BodyInit | undefined;

    if (body !== undefined) {
      requestHeaders.set("Content-Type", "application/json");
      requestBody = JSON.stringify(body);
    }
    requestHeaders.set("Accept", "application/json");

    const response = await fetch(endpoint(baseUrl, path), {
      ...requestInit,
      headers: requestHeaders,
      body: requestBody,
      credentials: "same-origin",
    });
    if (!response.ok) {
      const payload = await responsePayload(response);
      throw new ApiError(response.status, errorMessage(response.status, payload), payload);
    }
    if (response.status === 204) {
      return undefined as T;
    }
    return (await responsePayload(response)) as T;
  }

  async function requestForm<T>(path: string, formData: FormData, init: Omit<RequestInit, "body"> = {}): Promise<T> {
    const requestHeaders = headers(init.headers);
    requestHeaders.set("Accept", "application/json");
    const response = await fetch(endpoint(baseUrl, path), {
      ...init,
      headers: requestHeaders,
      body: formData,
      credentials: "same-origin",
    });
    if (!response.ok) {
      const payload = await responsePayload(response);
      throw new ApiError(response.status, errorMessage(response.status, payload), payload);
    }
    return (await responsePayload(response)) as T;
  }

  async function requestBlob(path: string, init: Omit<RequestInit, "body"> = {}): Promise<Blob> {
    const response = await fetch(endpoint(baseUrl, path), {
      ...init,
      headers: headers(init.headers),
      credentials: "same-origin",
    });
    if (!response.ok) {
      const payload = await responsePayload(response);
      throw new ApiError(response.status, errorMessage(response.status, payload), payload);
    }
    return response.blob();
  }

  /**
   * File-access URLs are generated by the API after an explicit user action.
   * They are same-origin, opaque and may need the compatibility-path prefix
   * when the workspace is mounted below `/greatsellhr`.
   */
  function authorizedFileEndpoint(accessUrl: string): string {
    const normalized = accessUrl.trim();
    if (!normalized.startsWith("/") || normalized.startsWith("//")) {
      throw new ApiError(422, "candidate_data_file_access_url_invalid", null);
    }
    if (normalized === "/v1" || normalized.startsWith("/v1/")) {
      const compatibilityPrefix = baseUrl.endsWith("/v1")
        ? baseUrl.slice(0, -3)
        : "";
      return `${compatibilityPrefix}${normalized}`;
    }
    return normalized;
  }

  async function requestAuthorizedFileBlob(
    accessUrl: string,
    init: Omit<RequestInit, "body"> = {},
  ): Promise<Blob> {
    const response = await fetch(authorizedFileEndpoint(accessUrl), {
      ...init,
      headers: headers(init.headers),
      credentials: "same-origin",
    });
    if (!response.ok) {
      const payload = await responsePayload(response);
      throw new ApiError(response.status, errorMessage(response.status, payload), payload);
    }
    return response.blob();
  }

  return {
    getAuthSession(): Promise<AuthSession> {
      return request<AuthSession>("/auth/session");
    },

    login(input: AuthLoginInput | string): Promise<AuthSession> {
      return request<AuthSession>("/auth/login", {
        method: "POST",
        // Keep the legacy password-only call shape available until the
        // server-side legacy workspace migration is complete.
        body: typeof input === "string" ? { password: input } : input,
      });
    },

    register(input: AuthRegistrationInput): Promise<AuthSession> {
      return request<AuthSession>("/auth/register", { method: "POST", body: input });
    },

    completeEmailVerification(token: string): Promise<AuthSession> {
      return request<AuthSession>("/auth/email-verification/complete", {
        method: "POST",
        body: { token },
      });
    },

    resendEmailVerification(): Promise<EmailVerificationResendResult> {
      return request<EmailVerificationResendResult>("/auth/email-verification/resend", {
        method: "POST",
      });
    },

    getRegistrationOffer(): Promise<RegistrationOffer> {
      return request<RegistrationOffer>("/auth/registration-offer");
    },

    requestPasswordReset(email: string): Promise<PasswordResetRequestResult> {
      return request<PasswordResetRequestResult>("/auth/password-reset/request", {
        method: "POST",
        body: { email },
      });
    },

    logout(): Promise<void> {
      return request<void>("/auth/logout", { method: "POST" });
    },

    listMailboxConfigs(includeArchived = false): Promise<MailboxConfigList> {
      const query = includeArchived ? "?include_archived=true" : "";
      return request<MailboxConfigList>(`/mailboxes${query}`);
    },

    getMailboxConfig(mailboxId: string): Promise<MailboxConfig> {
      return request<MailboxConfig>(`/mailboxes/${resourcePath(mailboxId)}`);
    },

    createMailboxConfig(input: MailboxConfigCreate): Promise<MailboxConfig> {
      return request<MailboxConfig>("/mailboxes", { method: "POST", body: input });
    },

    updateMailboxConfig(mailboxId: string, input: MailboxConfigPatch): Promise<MailboxConfig> {
      return request<MailboxConfig>(`/mailboxes/${resourcePath(mailboxId)}`, {
        method: "PATCH",
        body: input,
      });
    },

    syncMailbox(mailboxId: string): Promise<MailboxBackgroundJob> {
      return request<MailboxBackgroundJob>(`/mailboxes/${resourcePath(mailboxId)}/sync`, { method: "POST" });
    },

    syncAllMailboxes(): Promise<MailboxBackgroundJobBatch> {
      return request<MailboxBackgroundJobBatch>("/mailboxes/sync", { method: "POST" });
    },

    archiveMailbox(mailboxId: string): Promise<MailboxConfig> {
      return request<MailboxConfig>(`/mailboxes/${resourcePath(mailboxId)}/archive`, { method: "POST" });
    },

    listMailboxImports(mailboxId?: string | null): Promise<MailboxImportHistory> {
      const query = mailboxId ? `?${new URLSearchParams({ mailbox_id: mailboxId }).toString()}` : "";
      return request<MailboxImportHistory>(`/mailbox-imports${query}`);
    },

    retryMailboxImport(importId: string): Promise<MailboxBackgroundJob> {
      return request<MailboxBackgroundJob>(
        `/mailbox/imports/${encodeURIComponent(importId)}/retry`,
        { method: "POST" },
      );
    },

    listMailboxBackgroundJobs(mailboxId?: string | null): Promise<MailboxBackgroundJobHistory> {
      const query = mailboxId ? `?${new URLSearchParams({ mailbox_id: mailboxId }).toString()}` : "";
      return request<MailboxBackgroundJobHistory>(`/mailbox/tasks${query}`);
    },

    getMailboxBackgroundJob(jobId: string): Promise<MailboxBackgroundJob> {
      return request<MailboxBackgroundJob>(`/mailbox/tasks/${resourcePath(jobId)}`);
    },

    getMailboxRetention(mailboxId: string): Promise<MailboxRetentionOverview> {
      return request<MailboxRetentionOverview>(`/mailboxes/${resourcePath(mailboxId)}/retention`);
    },

    saveMailboxRetention(mailboxId: string, input: MailboxRetentionUpdate): Promise<MailboxRetentionOverview> {
      return request<MailboxRetentionOverview>(`/mailboxes/${resourcePath(mailboxId)}/retention`, { method: "PUT", body: input });
    },

    previewMailboxRetention(mailboxId: string): Promise<MailboxRetentionPreview> {
      return request<MailboxRetentionPreview>(`/mailboxes/${resourcePath(mailboxId)}/retention/preview`, { method: "POST" });
    },

    cleanupMailboxRetention(mailboxId: string): Promise<MailboxRetentionRun> {
      return request<MailboxRetentionRun>(`/mailboxes/${resourcePath(mailboxId)}/retention/cleanup`, { method: "POST" });
    },

    listMailboxRetentionRuns(mailboxId: string): Promise<MailboxRetentionRuns> {
      return request<MailboxRetentionRuns>(`/mailboxes/${resourcePath(mailboxId)}/retention/runs`);
    },

    runRecruitingAgentTurn(input: RecruitingAgentTurnInput): Promise<RecruitingAgentTurn> {
      return request<RecruitingAgentTurn>("/recruiting-agent/turns", {
        method: "POST",
        body: input,
      });
    },

    async health(): Promise<{ status: string }> {
      const response = await fetch(baseUrl === "/v1" ? "/health" : `${baseUrl.replace(/\/v1$/, "")}/health`, {
        headers: headers(),
      });
      if (!response.ok) {
        const payload = await responsePayload(response);
        throw new ApiError(response.status, errorMessage(response.status, payload), payload);
      }
      return (await responsePayload(response)) as { status: string };
    },

    createCandidate(input: CandidateCreateInput = {}): Promise<CandidateCreated> {
      return request<CandidateCreated>("/candidates", { method: "POST", body: input });
    },

    uploadResume(
      file: File,
      options: { idempotencyKey?: string | null } = {},
    ): Promise<ResumeUploadResponse> {
      const formData = new FormData();
      formData.set("file", file);
      const idempotencyKey = options.idempotencyKey?.trim();
      const requestHeaders = new Headers();
      if (idempotencyKey) requestHeaders.set("Idempotency-Key", idempotencyKey);
      return requestForm<ResumeUploadResponse>("/resumes/upload", formData, {
        method: "POST",
        headers: requestHeaders,
      });
    },

    uploadResumeForCandidate(candidateId: string, file: File): Promise<ResumeUploadResponse> {
      const formData = new FormData();
      formData.set("file", file);
      return requestForm<ResumeUploadResponse>(
        `/candidates/${resourcePath(candidateId)}/resumes`,
        formData,
        { method: "POST" },
      );
    },

    getResume(resumeId: string): Promise<ResumeDetail> {
      return request<ResumeDetail>(`/resumes/${resourcePath(resumeId)}`);
    },

    getReview(resumeId: string): Promise<ResumeReviewDetail> {
      return request<ResumeReviewDetail>(`/resumes/${resourcePath(resumeId)}/review`);
    },

    listReviewQueue(page = 1, pageSize = 25): Promise<ResumeReviewQueueResponse> {
      const query = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
      return request<ResumeReviewQueueResponse>(`/resumes/review-queue?${query.toString()}`);
    },

    requestResumeOriginalFileAccess(
      resumeId: string,
      purpose: CandidateDataFileAccessPurpose,
    ): Promise<CandidateDataFileAccessResponse> {
      return request<CandidateDataFileAccessResponse>(
        `/resumes/${resourcePath(resumeId)}/file-access`,
        { method: "POST", body: { purpose } },
      );
    },

    getAuthorizedFileBlob(accessUrl: string): Promise<Blob> {
      return requestAuthorizedFileBlob(accessUrl);
    },

    async getAuthorizedFileObjectUrl(
      accessUrl: string,
    ): Promise<ResumePdfObjectUrl> {
      const url = URL.createObjectURL(await requestAuthorizedFileBlob(accessUrl));
      return { url, revoke: () => URL.revokeObjectURL(url) };
    },

    deleteResumeCandidateData(
      resumeId: string,
      input: CandidateDataDeletionRequest,
    ): Promise<CandidateDataDeletionResponse> {
      return request<CandidateDataDeletionResponse>(
        `/resumes/${resourcePath(resumeId)}`,
        { method: "DELETE", body: input },
      );
    },

    deleteCandidateData(
      candidateId: string,
      input: CandidateDataDeletionRequest,
    ): Promise<CandidateDataDeletionResponse> {
      return request<CandidateDataDeletionResponse>(
        `/candidates/${resourcePath(candidateId)}`,
        { method: "DELETE", body: input },
      );
    },

    listCandidateDataDeletions(limit = 50): Promise<CandidateDataDeletionBatchList> {
      const query = new URLSearchParams({ limit: String(limit) });
      return request<CandidateDataDeletionBatchList>(
        `/candidate-data/deletions?${query.toString()}`,
      );
    },

    restoreCandidateDataDeletion(
      deletionBatchId: string,
    ): Promise<CandidateDataRestoreResponse> {
      return request<CandidateDataRestoreResponse>(
        `/candidate-data/deletions/${resourcePath(deletionBatchId)}/restore`,
        { method: "POST" },
      );
    },

    setCandidateRetentionHold(candidateId: string, retentionHold: boolean): Promise<void> {
      return request<void>(
        `/candidates/${resourcePath(candidateId)}/retention-hold`,
        { method: "PUT", body: { retention_hold: retentionHold } },
      );
    },

    getCandidateDataRetentionPolicy(): Promise<CandidateDataRetentionPolicy> {
      return request<CandidateDataRetentionPolicy>("/candidate-data/retention");
    },

    previewCandidateDataRetention(retentionDays: number): Promise<CandidateDataRetentionPreview> {
      return request<CandidateDataRetentionPreview>(
        "/candidate-data/retention/preview",
        { method: "POST", body: { retention_days: retentionDays } },
      );
    },

    updateCandidateDataRetentionPolicy(input: {
      mode: CandidateDataRetentionMode;
      retention_days?: number;
      preview_token?: string;
    }): Promise<CandidateDataRetentionPolicy> {
      return request<CandidateDataRetentionPolicy>(
        "/candidate-data/retention",
        { method: "PUT", body: input },
      );
    },

    runCandidateDataRetentionCleanup(): Promise<CandidateDataRetentionCleanupRun> {
      return request<CandidateDataRetentionCleanupRun>(
        "/candidate-data/retention/cleanup",
        { method: "POST" },
      );
    },

    listCandidateDataRetentionCleanupRuns(
      limit = 20,
    ): Promise<CandidateDataRetentionCleanupRunList> {
      const query = new URLSearchParams({ limit: String(limit) });
      return request<CandidateDataRetentionCleanupRunList>(
        `/candidate-data/retention/runs?${query.toString()}`,
      );
    },

    listCandidateDataAuditEvents(limit = 100): Promise<CandidateDataAuditEventList> {
      const query = new URLSearchParams({ limit: String(limit) });
      return request<CandidateDataAuditEventList>(
        `/candidate-data/audit-events?${query.toString()}`,
      );
    },

    createCandidateDataExport(
      input: CandidateDataExportCreate,
    ): Promise<CandidateDataExport> {
      return request<CandidateDataExport>("/candidate-data-exports", {
        method: "POST",
        body: input,
      });
    },

    listCandidateDataExports(limit = 50): Promise<CandidateDataExportList> {
      const query = new URLSearchParams({ limit: String(limit) });
      return request<CandidateDataExportList>(
        `/candidate-data-exports?${query.toString()}`,
      );
    },

    getCandidateDataExport(exportId: string): Promise<CandidateDataExport> {
      return request<CandidateDataExport>(
        `/candidate-data-exports/${resourcePath(exportId)}`,
      );
    },

    cancelCandidateDataExport(exportId: string): Promise<CandidateDataExport> {
      return request<CandidateDataExport>(
        `/candidate-data-exports/${resourcePath(exportId)}`,
        { method: "DELETE" },
      );
    },

    requestCandidateDataExportDownload(
      exportId: string,
    ): Promise<CandidateDataFileAccessResponse> {
      return request<CandidateDataFileAccessResponse>(
        `/candidate-data-exports/${resourcePath(exportId)}/download-access`,
        { method: "POST" },
      );
    },

    extractFacts(resumeId: string): Promise<ResumeDetail> {
      return request<ResumeDetail>(`/resumes/${resourcePath(resumeId)}/extract-facts`, { method: "POST" });
    },

    /** Queue a durable AI re-extraction. The server worker owns the model call. */
    queueAiExtraction(resumeId: string): Promise<ResumeDetail> {
      return request<ResumeDetail>(`/resumes/${resourcePath(resumeId)}/queue-ai-extraction`, { method: "POST" });
    },

    /**
     * Creates a fresh, inactive parse version from the immutable original file.
     * The server deliberately keeps the previous version intact for auditability.
     */
    reparseSource(resumeId: string): Promise<ResumeDetail> {
      return request<ResumeDetail>(`/resumes/${resourcePath(resumeId)}/reparse-source`, { method: "POST" });
    },

    saveFacts(resumeId: string, input: ResumeFactsSaveRequest): Promise<ResumeDetail> {
      return request<ResumeDetail>(`/resumes/${resourcePath(resumeId)}/facts`, {
        method: "PUT",
        body: input,
      });
    },

    activateResume(resumeId: string, input: ResumeActivateRequest = {}): Promise<ResumeDetail> {
      return request<ResumeDetail>(`/resumes/${resourcePath(resumeId)}/activate`, {
        method: "POST",
        body: input,
      });
    },

    searchCandidates(input: CandidateSearchRequest = {}): Promise<CandidateSearchResponse> {
      return request<CandidateSearchResponse>("/candidates/search", { method: "POST", body: input });
    },

    getFilterOptions(): Promise<FilterOptions> {
      return request<FilterOptions>("/filter-options");
    },

    enrichFilterFacts(resumeId: string): Promise<ResumeDetail> {
      return request<ResumeDetail>(`/resumes/${resourcePath(resumeId)}/enrich-filter-facts`, { method: "POST" });
    },

    listResumeLibrary(
      page = 1,
      pageSize = 50,
      mailboxId?: string | null,
    ): Promise<ResumeLibraryResponse> {
      const query = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
      if (mailboxId) query.set("mailbox_id", mailboxId);
      return request<ResumeLibraryResponse>(`/resume-library?${query.toString()}`);
    },

    listSavedFilters(): Promise<SavedFilter[]> {
      return request<SavedFilter[]>("/saved-filters");
    },

    createSavedFilter(input: SavedFilterCreate): Promise<SavedFilter> {
      return request<SavedFilter>("/saved-filters", { method: "POST", body: input });
    },

    deleteSavedFilter(savedFilterId: string): Promise<void> {
      return request<void>(`/saved-filters/${resourcePath(savedFilterId)}`, { method: "DELETE" });
    },

    listScoreTemplates(): Promise<ScoreTemplate[]> {
      return request<ScoreTemplate[]>("/score-templates");
    },

    createScoreTemplate(input: ScoreTemplateCreate): Promise<ScoreTemplate> {
      return request<ScoreTemplate>("/score-templates", { method: "POST", body: input });
    },

    createScore(resumeId: string, input: ResumeScoreCreate): Promise<ResumeScore> {
      return request<ResumeScore>(`/resumes/${resourcePath(resumeId)}/scores`, {
        method: "POST",
        body: input,
      });
    },

    enqueueAllResumeScores(templateId: string): Promise<ResumeScoreBatch> {
      return request<ResumeScoreBatch>(
        `/score-templates/${resourcePath(templateId)}/score-all`,
        { method: "POST" },
      );
    },

    getResumeScoreBatch(batchId: string): Promise<ResumeScoreBatch> {
      return request<ResumeScoreBatch>(
        `/resume-score-batches/${resourcePath(batchId)}`,
      );
    },

    listResumeScoreBatchItems(batchId: string): Promise<ResumeScoreBatchItem[]> {
      return request<ResumeScoreBatchItem[]>(
        `/resume-score-batches/${resourcePath(batchId)}/items`,
      );
    },

    getScore(scoreId: string): Promise<ResumeScore> {
      return request<ResumeScore>(`/resume-scores/${resourcePath(scoreId)}`);
    },

    listScores(resumeId: string): Promise<ResumeScore[]> {
      return request<ResumeScore[]>(`/resumes/${resourcePath(resumeId)}/scores`);
    },

    overrideScoreDimension(scoreId: string, dimensionKey: string, input: ResumeScoreOverride): Promise<ResumeScore> {
      return request<ResumeScore>(
        `/resume-scores/${resourcePath(scoreId)}/dimensions/${resourcePath(dimensionKey)}/override`,
        { method: "POST", body: input },
      );
    },

    generateSummary(resumeId: string): Promise<ResumeSummary> {
      return request<ResumeSummary>(`/resumes/${resourcePath(resumeId)}/summaries`, { method: "POST" });
    },

    getSummary(summaryId: string): Promise<ResumeSummary> {
      return request<ResumeSummary>(`/resume-summaries/${resourcePath(summaryId)}`);
    },

    listSummaries(resumeId: string): Promise<ResumeSummary[]> {
      return request<ResumeSummary[]>(`/resumes/${resourcePath(resumeId)}/summaries`);
    },

    createManualSummaryVersion(summaryId: string, input: ResumeSummaryManualCreate): Promise<ResumeSummary> {
      return request<ResumeSummary>(`/resume-summaries/${resourcePath(summaryId)}/manual-versions`, {
        method: "POST",
        body: input,
      });
    },

    createJob(input: JobCreate): Promise<JobVersion> {
      return request<JobVersion>("/jobs", { method: "POST", body: input });
    },

    generateJobDescription(
      input: JobDescriptionGenerateInput,
    ): Promise<JobDescriptionGeneration> {
      return request<JobDescriptionGeneration>("/jobs/generate-jd", {
        method: "POST",
        body: input,
      });
    },

    publishOriginalJob(input: OriginalJobPublishInput): Promise<JobVersion> {
      return request<JobVersion>("/jobs/publish-original", {
        method: "POST",
        body: input,
      });
    },

    createJobVersion(jobId: string, input: JobCreate): Promise<JobVersion> {
      return request<JobVersion>(`/jobs/${resourcePath(jobId)}/versions`, { method: "POST", body: input });
    },

    listJobVersions(jobId: string): Promise<JobVersion[]> {
      return request<JobVersion[]>(`/jobs/${resourcePath(jobId)}/versions`);
    },

    getJobVersion(jobVersionId: string): Promise<JobVersion> {
      return request<JobVersion>(`/job-versions/${resourcePath(jobVersionId)}`);
    },

    extractJobRequirements(jobVersionId: string): Promise<JobVersion> {
      return request<JobVersion>(`/job-versions/${resourcePath(jobVersionId)}/extract`, { method: "POST" });
    },

    updateJobRequirements(jobVersionId: string, input: JobVersionRequirementsUpdate): Promise<JobVersion> {
      return request<JobVersion>(`/job-versions/${resourcePath(jobVersionId)}/requirements`, {
        method: "PUT",
        body: input,
      });
    },

    confirmJobVersion(jobVersionId: string): Promise<JobVersion> {
      return request<JobVersion>(`/job-versions/${resourcePath(jobVersionId)}/confirm`, { method: "POST" });
    },

    enqueueAllJobMatches(jobVersionId: string): Promise<JobMatchBatch> {
      return request<JobMatchBatch>(`/job-versions/${resourcePath(jobVersionId)}/match-all`, { method: "POST" });
    },

    getJobMatchBatch(batchId: string): Promise<JobMatchBatch> {
      return request<JobMatchBatch>(`/job-match-batches/${resourcePath(batchId)}`);
    },

    listJobMatchBatchItems(batchId: string): Promise<JobMatchBatchItem[]> {
      return request<JobMatchBatchItem[]>(
        `/job-match-batches/${resourcePath(batchId)}/items`,
      );
    },

    runJobMatch(resumeId: string, input: JobMatchCreate): Promise<JobMatch> {
      return request<JobMatch>(`/resumes/${resourcePath(resumeId)}/job-matches`, {
        method: "POST",
        body: input,
      });
    },

    getJobMatch(matchId: string): Promise<JobMatch> {
      return request<JobMatch>(`/job-matches/${resourcePath(matchId)}`);
    },

    getLatestConfirmedJobVersion(): Promise<JobVersion> {
      return request<JobVersion>("/jobs/latest-confirmed-version");
    },

    listConfirmedJobVersions(): Promise<JobVersion[]> {
      return request<JobVersion[]>("/jobs/confirmed-versions");
    },

    listJobMatches(resumeId: string): Promise<JobMatch[]> {
      return request<JobMatch[]>(`/resumes/${resourcePath(resumeId)}/job-matches`);
    },

    listJobVersionMatches(jobVersionId: string): Promise<JobMatch[]> {
      return request<JobMatch[]>(`/job-versions/${resourcePath(jobVersionId)}/matches`);
    },
  };
}

/** The application singleton chooses `/v1` or `/greatsellhr/v1` by entry path. */
export const api = createApiClient();
