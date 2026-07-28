import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../../api";
import {
  AI_STATUS_POLL_INTERVAL_MS,
  aiSummaryIsInProgress,
} from "../../backoffice/utils/ai-extraction";
import { formatLibraryDate } from "../../backoffice/utils/formatters";
import {
  hasSourceTextQualityIssue,
  hasSupersededReparseVersion,
} from "../../backoffice/utils/resume-source-quality";
import { canPreviewInline } from "../../backoffice/utils/resume-file";
import type {
  ResumeReviewDetail,
  ResumeScore,
  ResumeSummary,
} from "../../types";
import type { CandidateDrawerProps } from "./CandidateDrawer";
import type {
  CandidateDrawerTab,
  SelectedResume,
} from "./candidate-drawer-types";

type ToastKind = "success" | "error";

interface UseCandidateDrawerControllerOptions {
  formatError: (error: unknown) => string;
  notify: (kind: ToastKind, message: string) => void;
  onLibraryChanged: () => void;
}

export interface OpenResumeInput {
  candidateId: string;
  candidateName: string;
  resumeId: string;
}

export function useCandidateDrawerController({
  formatError,
  notify,
  onLibraryChanged,
}: UseCandidateDrawerControllerOptions) {
  const [selectedResume, setSelectedResume] = useState<SelectedResume | null>(
    null,
  );
  const [review, setReview] = useState<ResumeReviewDetail | null>(null);
  const [reviewLoading, setReviewLoading] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [drawerTab, setDrawerTab] = useState<CandidateDrawerTab>("original");
  const [pdfUrl, setPdfUrl] = useState<string | null>(null);
  const [pdfLoading, setPdfLoading] = useState(false);
  const [pdfDownloadLoading, setPdfDownloadLoading] = useState(false);
  const [pdfError, setPdfError] = useState<string | null>(null);
  const [summaries, setSummaries] = useState<ResumeSummary[]>([]);
  const [summaryLoading, setSummaryLoading] = useState(false);
  const [drawerScores, setDrawerScores] = useState<ResumeScore[]>([]);
  const [drawerScoreLoading, setDrawerScoreLoading] = useState(false);
  const [drawerScoreError, setDrawerScoreError] = useState<string | null>(null);
  const [reparsingSource, setReparsingSource] = useState(false);
  const [enrichingFacts, setEnrichingFacts] = useState(false);
  const reviewRequestRef = useRef(0);
  const summaryRequestRef = useRef(0);
  const drawerScoreRequestRef = useRef(0);
  const originalFileRequestRef = useRef(0);
  const originalFileRevokeRef = useRef<(() => void) | null>(null);
  const selectedResumeId = selectedResume?.resumeId ?? null;

  const releaseOriginalFile = useCallback((clearError = true) => {
    originalFileRequestRef.current += 1;
    originalFileRevokeRef.current?.();
    originalFileRevokeRef.current = null;
    setPdfUrl(null);
    setPdfLoading(false);
    if (clearError) setPdfError(null);
  }, []);

  const refreshReview = useCallback(
    async (resumeId: string) => {
      const requestId = ++reviewRequestRef.current;
      setReviewLoading(true);
      try {
        const detail = await api.getReview(resumeId);
        if (requestId === reviewRequestRef.current) {
          setReview(detail);
          setSelectedResume((current) => {
            if (!current || current.resumeId !== detail.resume_id)
              return current;
            return {
              ...current,
              candidateName:
                detail.candidate_display_name?.trim() || "未命名候选人",
            };
          });
        }
      } catch (error) {
        if (requestId === reviewRequestRef.current) {
          setReview(null);
          notify("error", formatError(error));
        }
      } finally {
        if (requestId === reviewRequestRef.current) setReviewLoading(false);
      }
    },
    [formatError, notify],
  );

  const loadSummaries = useCallback(
    async (resumeId: string) => {
      const requestId = ++summaryRequestRef.current;
      setSummaryLoading(true);
      try {
        const response = await api.listSummaries(resumeId);
        if (requestId === summaryRequestRef.current) setSummaries(response);
      } catch (error) {
        if (requestId === summaryRequestRef.current) {
          setSummaries([]);
          notify("error", formatError(error));
        }
      } finally {
        if (requestId === summaryRequestRef.current) setSummaryLoading(false);
      }
    },
    [formatError, notify],
  );

  const loadDrawerScores = useCallback(
    async (resumeId: string) => {
      const requestId = ++drawerScoreRequestRef.current;
      setDrawerScoreLoading(true);
      setDrawerScoreError(null);
      try {
        const response = await api.listScores(resumeId);
        if (requestId === drawerScoreRequestRef.current) {
          setDrawerScores(response);
        }
      } catch (error) {
        if (requestId === drawerScoreRequestRef.current) {
          setDrawerScores([]);
          setDrawerScoreError(formatError(error));
        }
      } finally {
        if (requestId === drawerScoreRequestRef.current) {
          setDrawerScoreLoading(false);
        }
      }
    },
    [formatError],
  );


  const openResume = useCallback(
    (
      { candidateId, candidateName, resumeId }: OpenResumeInput,
      tab: CandidateDrawerTab = "summary",
    ) => {
      summaryRequestRef.current += 1;
      setReview(null);
      setSummaries([]);
      setSelectedResume({ resumeId, candidateId, candidateName });
      setDrawerTab(tab);
      setDrawerOpen(true);
      void refreshReview(resumeId);
    },
    [refreshReview],
  );

  const closeDrawer = useCallback(() => {
    setDrawerOpen(false);
  }, []);

  const resetDrawer = useCallback(() => {
    setSelectedResume(null);
    setDrawerOpen(false);
  }, []);

  useEffect(() => {
    if (
      !drawerOpen ||
      drawerTab !== "summary" ||
      !selectedResumeId ||
      !review ||
      review.resume_id !== selectedResumeId
    )
      return;
    if (hasSourceTextQualityIssue(review.quality_flags)) {
      setSummaries([]);
      return;
    }
    void loadSummaries(selectedResumeId);
  }, [drawerOpen, drawerTab, loadSummaries, review, selectedResumeId]);

  useEffect(() => {
    if (
      !drawerOpen ||
      drawerTab !== "summary" ||
      !selectedResumeId ||
      !review ||
      review.resume_id !== selectedResumeId ||
      hasSourceTextQualityIssue(review.quality_flags) ||
      hasSupersededReparseVersion(review.quality_flags) ||
      !aiSummaryIsInProgress(review.ai_summary_status)
    ) {
      return undefined;
    }
    const interval = window.setInterval(() => {
      void refreshReview(selectedResumeId);
    }, AI_STATUS_POLL_INTERVAL_MS);
    return () => window.clearInterval(interval);
  }, [drawerOpen, drawerTab, refreshReview, review, selectedResumeId]);

  useEffect(() => {
    if (
      !drawerOpen ||
      drawerTab !== "score" ||
      !selectedResumeId ||
      !review ||
      review.resume_id !== selectedResumeId
    ) {
      return;
    }
    if (
      hasSourceTextQualityIssue(review.quality_flags) ||
      hasSupersededReparseVersion(review.quality_flags)
    ) {
      setDrawerScores([]);
      setDrawerScoreError(null);
      return;
    }
    void loadDrawerScores(selectedResumeId);
  }, [drawerOpen, drawerTab, loadDrawerScores, review, selectedResumeId]);

  useEffect(() => {
    drawerScoreRequestRef.current += 1;
    setSummaries([]);
    setDrawerScores([]);
    setDrawerScoreError(null);
    setDrawerScoreLoading(false);
  }, [selectedResumeId]);

  /**
   * Keep protected originals scoped to the active original-file tab. Opening
   * that tab creates a fresh, audited view grant; switching away, closing the
   * drawer, or selecting another resume invalidates the local object URL and
   * any in-flight request.
   */
  useEffect(() => {
    releaseOriginalFile();
  }, [drawerOpen, drawerTab, releaseOriginalFile, selectedResumeId]);

  useEffect(
    () => () => {
      originalFileRequestRef.current += 1;
      originalFileRevokeRef.current?.();
      originalFileRevokeRef.current = null;
    },
    [],
  );


  const previewOriginalFile = useCallback(async () => {
    if (!selectedResumeId || pdfLoading) return;
    const requestId = ++originalFileRequestRef.current;
    originalFileRevokeRef.current?.();
    originalFileRevokeRef.current = null;
    setPdfUrl(null);
    setPdfError(null);
    setPdfLoading(true);
    try {
      const access = await api.requestResumeOriginalFileAccess(
        selectedResumeId,
        "view",
      );
      const resource = await api.getAuthorizedFileObjectUrl(access.access_url);
      if (requestId !== originalFileRequestRef.current) {
        resource.revoke();
        return;
      }
      originalFileRevokeRef.current = resource.revoke;
      setPdfUrl(resource.url);
    } catch (error) {
      if (requestId === originalFileRequestRef.current) {
        setPdfError(formatError(error));
      }
    } finally {
      if (requestId === originalFileRequestRef.current) setPdfLoading(false);
    }
  }, [formatError, pdfLoading, selectedResumeId]);

  useEffect(() => {
    if (
      !drawerOpen ||
      drawerTab !== "original" ||
      !selectedResumeId ||
      !review ||
      review.resume_id !== selectedResumeId ||
      !canPreviewInline(review.original_filename) ||
      pdfUrl ||
      pdfLoading ||
      pdfError
    ) {
      return;
    }
    // Opening the original-file tab is an intentional view action. Request a
    // short-lived, audited grant and render the protected object URL directly.
    void previewOriginalFile();
  }, [
    drawerOpen,
    drawerTab,
    pdfError,
    pdfLoading,
    pdfUrl,
    previewOriginalFile,
    review,
    selectedResumeId,
  ]);

  const downloadOriginalFile = useCallback(async () => {
    if (!selectedResumeId || pdfDownloadLoading) return;
    setPdfDownloadLoading(true);
    try {
      const access = await api.requestResumeOriginalFileAccess(
        selectedResumeId,
        "download",
      );
      const blob = await api.getAuthorizedFileBlob(access.access_url);
      const downloadUrl = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = downloadUrl;
      link.download = review?.original_filename || "resume";
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(downloadUrl), 0);
      notify("success", "已开始下载原始文件，系统已记录本次访问。");
    } catch (error) {
      notify("error", formatError(error));
    } finally {
      setPdfDownloadLoading(false);
    }
  }, [
    formatError,
    notify,
    pdfDownloadLoading,
    review?.original_filename,
    selectedResumeId,
  ]);


  const generateSummary = async () => {
    if (!selectedResumeId) {
      notify("error", "请先从筛选结果中打开一份简历。");
      return;
    }
    setSummaryLoading(true);
    try {
      const summary = await api.generateSummary(selectedResumeId);
      setSummaries((current) => [
        summary,
        ...current
          .filter((item) => item.summary_id !== summary.summary_id)
          .map((item) => ({ ...item, is_current: false })),
      ]);
      onLibraryChanged();
      void refreshReview(selectedResumeId);
      notify("success", "AI 简历总结已生成。");
    } catch (error) {
      notify("error", formatError(error));
    } finally {
      setSummaryLoading(false);
    }
  };

  const createManualSummary = async (
    summaryId: string,
    content: Record<string, string>,
  ) => {
    try {
      const summary = await api.createManualSummaryVersion(summaryId, {
        content,
      });
      setSummaries((current) => [
        summary,
        ...current
          .filter((item) => item.summary_id !== summary.summary_id)
          .map((item) => ({ ...item, is_current: false })),
      ]);
      onLibraryChanged();
      notify("success", "人工总结已保存为新的可追溯版本。");
    } catch (error) {
      notify("error", formatError(error));
      throw error;
    }
  };

  const reparseSelectedSource = useCallback(async () => {
    if (!selectedResumeId || reparsingSource) return;
    setReparsingSource(true);
    try {
      const parsed = await api.reparseSource(selectedResumeId);
      summaryRequestRef.current += 1;
      setReview(null);
      setSummaries([]);
      setSelectedResume((current) => ({
        resumeId: parsed.resume_id,
        candidateId: parsed.candidate_id,
        candidateName:
          parsed.candidate_display_name?.trim() ||
          current?.candidateName ||
          "未命名候选人",
      }));
      setDrawerTab("original");
      onLibraryChanged();
      await refreshReview(parsed.resume_id);
      notify(
        "success",
        "已创建新的解析版本，正在基于原件重新提取。原版本会保留，不会被覆盖。",
      );
    } catch (error) {
      notify("error", formatError(error));
    } finally {
      setReparsingSource(false);
    }
  }, [
    formatError,
    notify,
    onLibraryChanged,
    refreshReview,
    reparsingSource,
    selectedResumeId,
  ]);

  const enrichSelectedFacts = useCallback(async () => {
    if (!selectedResumeId || enrichingFacts) return;
    setEnrichingFacts(true);
    try {
      await api.enrichFilterFacts(selectedResumeId);
      await refreshReview(selectedResumeId);
      notify("success", "已提交高级筛选事实补充任务，旧事实会保留。完成后可刷新查看。");
    } catch (error) {
      notify("error", formatError(error));
    } finally {
      setEnrichingFacts(false);
    }
  }, [enrichingFacts, formatError, notify, refreshReview, selectedResumeId]);

  const deleteSelectedResumeData = useCallback(
    async (): Promise<void> => {
      if (!selectedResumeId) throw new Error("resume_not_found");
      try {
        const response = await api.deleteResumeCandidateData(selectedResumeId, {
          reason: "other",
          other_note: "simple_resume_delete",
        });
        releaseOriginalFile();
        summaryRequestRef.current += 1;
        setReview(null);
        setSummaries([]);
        setSelectedResume(null);
        setDrawerOpen(false);
        onLibraryChanged();
        notify(
          "success",
          `当前简历版本已移出工作台，可在 ${formatLibraryDate(response.recovery_deadline_at)} 前恢复。`,
        );
      } catch (error) {
        notify("error", formatError(error));
        throw error;
      }
    },
    [formatError, notify, onLibraryChanged, releaseOriginalFile, selectedResumeId],
  );


  const candidateDrawerProps: Omit<
    CandidateDrawerProps,
    "canManageCandidateData" | "languageCredentialOptions"
  > = {
    candidate: selectedResume,
    drawerTab,
    enrichingFacts,
    isOpen: drawerOpen,
    onClose: closeDrawer,
    onCreateManualSummary: createManualSummary,
    onDeleteResume: deleteSelectedResumeData,
    onDownloadOriginal: downloadOriginalFile,
    onEnrichFacts: () => void enrichSelectedFacts(),
    onGenerateSummary: () => void generateSummary(),
    onNotify: notify,
    onPreviewOriginal: () => void previewOriginalFile(),
    onRefreshScores: () => {
      if (selectedResumeId) void loadDrawerScores(selectedResumeId);
    },
    onReparseSource: () => void reparseSelectedSource(),
    onTabChange: setDrawerTab,
    pdfDownloadLoading,
    pdfError,
    pdfLoading,
    pdfUrl,
    reparsingSource,
    review,
    reviewLoading,
    scoreError: drawerScoreError,
    scoreLoading: drawerScoreLoading,
    scores: drawerScores,
    summaries,
    summaryLoading,
  };

  return {
    candidateDrawerProps,
    closeDrawer,
    isOpen: drawerOpen,
    openResume,
    resetDrawer,
    selectedResumeId,
  };
}
