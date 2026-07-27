import {
  useEffect,
  useRef,
  useState,
  type ChangeEvent,
  type DragEvent,
} from "react";
import { api, isApiError } from "../../api";
import { Icon } from "../../icons";
import type { ResumeDetail } from "../../types";
import {
  AI_STATUS_POLL_INTERVAL_MS,
  aiExtractionIsInProgress,
} from "../../backoffice/utils/ai-extraction";
import { formatFileSize } from "../../backoffice/utils/formatters";
import {
  isSupportedResumeFile,
  resumeFileTypeLabel,
} from "../../backoffice/utils/resume-file";
import {
  BATCH_UPLOAD_CONCURRENCY,
  createUploadIdempotencyKey,
  fileFingerprint,
  MAX_BATCH_FILES,
  type UploadQueueItem,
  uploadStatusFromResponse,
  withLatestAiExtractionStatus,
} from "./upload-model";
import "./upload.css";

function isRetryableUploadError(error: unknown): boolean {
  if (!isApiError(error)) return true;
  return error.status === 408 || error.status === 429 || error.status >= 500;
}

export function UploadPage({
  formatError,
  notify,
  onComplete,
}: {
  formatError: (error: unknown) => string;
  notify: (kind: "success" | "error", message: string) => void;
  onComplete: (resumeId: string, candidateId: string) => void;
}) {
  const [uploads, setUploads] = useState<UploadQueueItem[]>([]);
  const [dragging, setDragging] = useState(false);
  const [uploading, setUploading] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const uploadLockRef = useRef(false);
  const dragDepthRef = useRef(0);

  const queuedUploads = uploads.filter((item) => item.status === "queued");
  const failedUploads = uploads.filter((item) => item.status === "error");
  const retryableFailedUploads = failedUploads.filter(
    (item) => item.retryable !== false,
  );
  const completedUploads = uploads.filter((item) => item.status === "success");
  const resolvedUploads = uploads.filter(
    (item) => item.status === "success" || item.status === "attention",
  );
  const attentionUploads = uploads.filter(
    (item) => item.status === "attention",
  );
  const extractingUploads = uploads.filter(
    (item) => item.status === "extracting",
  );

  const updateUpload = (uploadId: string, patch: Partial<UploadQueueItem>) => {
    setUploads((current) =>
      current.map((item) =>
        item.id === uploadId ? { ...item, ...patch } : item,
      ),
    );
  };

  useEffect(() => {
    const resumeIds = uploads
      .filter(
        (item) =>
          item.response &&
          aiExtractionIsInProgress(item.response.ai_extraction_status),
      )
      .map((item) => item.response!.resume_id);
    if (!resumeIds.length) return undefined;

    let cancelled = false;
    const refreshAiStatuses = async () => {
      const details = await Promise.all(
        resumeIds.map(async (resumeId) => {
          try {
            return await api.getResume(resumeId);
          } catch {
            // A transient polling failure must not turn a saved resume into an
            // upload failure. The worker will continue independently.
            return null;
          }
        }),
      );
      if (cancelled) return;
      const byResumeId = new Map(
        details
          .filter((detail): detail is ResumeDetail => detail !== null)
          .map((detail) => [detail.resume_id, detail]),
      );
      if (!byResumeId.size) return;
      setUploads((current) =>
        current.map((item) => {
          if (!item.response) return item;
          const detail = byResumeId.get(item.response.resume_id);
          if (!detail) return item;
          const response = withLatestAiExtractionStatus(item.response, detail);
          const status = uploadStatusFromResponse(response);
          if (
            item.status === status &&
            item.response.extraction_status === response.extraction_status &&
            item.response.ai_extraction_status ===
              response.ai_extraction_status &&
            item.response.ai_extraction_error === response.ai_extraction_error
          )
            return item;
          return { ...item, response, status, error: undefined };
        }),
      );
    };

    void refreshAiStatuses();
    const interval = window.setInterval(() => {
      void refreshAiStatuses();
    }, AI_STATUS_POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, [uploads]);

  const addFiles = (selectedFiles: FileList | File[]) => {
    if (uploading) return;
    const incoming = Array.from(selectedFiles);
    if (!incoming.length) return;

    const supportedFiles = incoming.filter(isSupportedResumeFile);
    const knownFiles = new Set(
      uploads.map((item) => fileFingerprint(item.file)),
    );
    const uniqueFiles = supportedFiles.filter((file) => {
      const fingerprint = fileFingerprint(file);
      if (knownFiles.has(fingerprint)) return false;
      knownFiles.add(fingerprint);
      return true;
    });
    const remainingSlots = Math.max(0, MAX_BATCH_FILES - uploads.length);
    const acceptedFiles = uniqueFiles.slice(0, remainingSlots);
    const invalidCount = incoming.length - supportedFiles.length;
    const duplicateCount = supportedFiles.length - uniqueFiles.length;
    const capacityCount = uniqueFiles.length - acceptedFiles.length;

    if (!acceptedFiles.length) {
      const reason = invalidCount
        ? "所选文件不在支持格式内。"
        : duplicateCount
          ? "这些简历已在当前队列中。"
          : `一次最多处理 ${MAX_BATCH_FILES} 份简历。`;
      notify("error", `没有加入新文件：${reason}`);
      return;
    }

    const timestamp = Date.now();
    setUploads((current) => [
      ...current,
      ...acceptedFiles.map((file, index) => ({
        id: `upload-${timestamp}-${index}-${fileFingerprint(file)}`,
        file,
        status: "queued" as const,
        idempotencyKey: createUploadIdempotencyKey(),
      })),
    ]);
    notify(
      "success",
      acceptedFiles.length === 1
        ? "已加入 1 份简历，等待上传。"
        : `已加入 ${acceptedFiles.length} 份简历，等待上传。`,
    );

    const exclusions: string[] = [];
    if (invalidCount) exclusions.push(`${invalidCount} 个不支持的文件`);
    if (duplicateCount) exclusions.push(`${duplicateCount} 份重复简历`);
    if (capacityCount) exclusions.push(`${capacityCount} 份超过本次上限`);
    if (exclusions.length)
      notify("error", `未加入：${exclusions.join("、")}。`);
  };

  const handleInputChange = (event: ChangeEvent<HTMLInputElement>) => {
    if (event.currentTarget.files) addFiles(event.currentTarget.files);
    event.currentTarget.value = "";
  };

  const handleDragEnter = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    if (uploading) return;
    dragDepthRef.current += 1;
    setDragging(true);
  };

  const handleDragLeave = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    if (uploading) return;
    dragDepthRef.current = Math.max(0, dragDepthRef.current - 1);
    if (!dragDepthRef.current) setDragging(false);
  };

  const handleDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    dragDepthRef.current = 0;
    setDragging(false);
    if (!uploading) addFiles(event.dataTransfer.files);
  };

  const runUploads = async (targets: UploadQueueItem[]) => {
    if (!targets.length) {
      notify("error", "请先选择至少一份简历。");
      return;
    }
    if (uploadLockRef.current) return;

    uploadLockRef.current = true;
    setUploading(true);
    let nextIndex = 0;
    let succeeded = 0;
    let failed = 0;

    const worker = async () => {
      while (nextIndex < targets.length) {
        const item = targets[nextIndex++];
        updateUpload(item.id, {
          status: "uploading",
          error: undefined,
          retryable: undefined,
        });
        try {
          const response = await api.uploadResume(item.file, {
            idempotencyKey: item.idempotencyKey,
          });
          succeeded += 1;
          updateUpload(item.id, {
            status: uploadStatusFromResponse(response),
            response,
            error: undefined,
          });
        } catch (error) {
          failed += 1;
          updateUpload(item.id, {
            status: "error",
            error: formatError(error),
            retryable: isRetryableUploadError(error),
          });
        }
      }
    };

    try {
      await Promise.all(
        Array.from(
          { length: Math.min(BATCH_UPLOAD_CONCURRENCY, targets.length) },
          worker,
        ),
      );
    } finally {
      uploadLockRef.current = false;
      setUploading(false);
    }

    if (succeeded) {
      notify(
        "success",
        succeeded === 1
          ? "简历已保存，AI 正在提取候选人姓名和结构化事实。"
          : `${succeeded} 份简历已保存，AI 正在按队列提取候选人姓名和结构化事实。`,
      );
    }
    if (failed) {
      notify(
        "error",
        failed === 1
          ? "1 份简历上传失败。请查看原因后重试。"
          : `${failed} 份简历上传失败。其余文件未受影响。`,
      );
    }
  };

  const openSuccessfulUpload = (item: UploadQueueItem) => {
    if (!item.response) return;
    onComplete(item.response.resume_id, item.response.candidate_id);
  };

  const statusText = (item: UploadQueueItem): string => {
    if (item.status === "queued") return "等待上传";
    if (item.status === "uploading") return "正在保存原件并提取文字";
    if (item.status === "extracting") {
      return item.response?.ai_extraction_status === "running"
        ? "AI 正在提取候选人姓名、教育、经历和技能"
        : "原件已保存，AI 正在排队提取候选人姓名和结构化事实";
    }
    if (item.status === "attention") {
      if (
        item.response?.extraction_status === "failed" ||
        !item.response?.parsed_page_count
      ) {
        return "原件已保存，但未读取到可用文字，暂不能 AI 提取";
      }
      if (item.response?.ai_extraction_status === "unavailable") {
        return "原件和文字已保存，等待服务器配置 AI 服务";
      }
      return "原件和文字已保存，但 AI 提取需要处理；可查看原件并重新上传。";
    }
    if (item.status === "success") {
      return item.response?.quality_flags.length
        ? "AI 已提取并启用，存在解析提示"
        : "AI 已提取并已进入筛选库";
    }
    return item.error || "上传没有完成，请重试。";
  };

  return (
    <div className="page-frame upload-workspace">
      <header className="page-heading">
        <div>
          <h1>批量上传简历</h1>
          <p>
            上传后自动入库并进入 AI 处理，完成后可在简历库查看、筛选、评分和匹配岗位。
          </p>
        </div>
      </header>
      <div className="page-layout">
        <section className="panel">
          <div className="panel-heading">
            <div>
              <h2>添加候选人简历</h2>
            </div>
          </div>
          <div className="form-grid">
            <div className="span-full">
              <div
                aria-busy={uploading}
                aria-describedby="upload-dropzone-help"
                aria-label="批量简历上传区域"
                className={`dropzone${dragging ? " is-dragging" : ""}${uploading ? " is-disabled" : ""}`}
                onDragEnter={handleDragEnter}
                onDragLeave={handleDragLeave}
                onDragOver={(event) => event.preventDefault()}
                onDrop={handleDrop}
              >
                <input
                  accept=".pdf,.doc,.docx,.png,.jpg,.jpeg,.xls,.xlsx,.html,.htm,application/pdf,application/msword,application/vnd.openxmlformats-officedocument.wordprocessingml.document,application/vnd.ms-excel,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,image/png,image/jpeg,text/html"
                  disabled={uploading}
                  multiple
                  onChange={handleInputChange}
                  ref={inputRef}
                  tabIndex={-1}
                  type="file"
                />
                <div className="dropzone-inner">
                  <span className="dropzone-icon">
                    <Icon name="upload" size={25} />
                  </span>
                  <h2>
                    {uploading
                      ? `正在保存并入队：已完成 ${completedUploads.length} / ${uploads.length}`
                      : extractingUploads.length
                        ? `AI 正在处理 ${extractingUploads.length} 份简历`
                        : uploads.length
                          ? `已加入 ${uploads.length} 份简历`
                          : "拖入简历，或点击选择文件"}
                  </h2>
                  <p id="upload-dropzone-help">
                    支持 PDF、Word、图片、Excel 和 HTML。
                  </p>
                  <button
                    className="button"
                    disabled={uploading}
                    onClick={() => inputRef.current?.click()}
                    type="button"
                  >
                    选择简历文件
                  </button>
                </div>
              </div>
              {uploads.length > 0 && (
                <div className="upload-queue">
                  <div className="upload-queue-header" aria-live="polite">
                    <div>
                      <strong>上传队列</strong>
                      <span>
                        {uploads.length} 份文件 · AI 处理中{" "}
                        {extractingUploads.length} · 已启用{" "}
                        {completedUploads.length} · 需处理{" "}
                        {attentionUploads.length} · 失败 {failedUploads.length}
                      </span>
                    </div>
                    {resolvedUploads.length > 0 && (
                      <button
                        className="text-button"
                        disabled={uploading}
                        onClick={() =>
                          setUploads((current) =>
                            current.filter(
                              (item) =>
                                item.status !== "success" &&
                                item.status !== "attention",
                            ),
                          )
                        }
                        type="button"
                      >
                        清除已完成
                      </button>
                    )}
                  </div>
                  <ul>
                    {uploads.map((item) => (
                      <li
                        className={`upload-file-card is-${item.status}`}
                        key={item.id}
                        role={item.status === "error" ? "alert" : undefined}
                      >
                        <Icon name="document" size={22} />
                        <div className="upload-file-main">
                          <strong title={item.file.name}>
                            {item.file.name}
                          </strong>
                          <span>
                            {formatFileSize(item.file.size)} · {resumeFileTypeLabel(item.file.name)} ·{" "}
                            {statusText(item)}
                          </span>
                        </div>
                        <div className="upload-row-actions">
                          {(item.status === "uploading" ||
                            item.status === "extracting") && (
                            <i
                              aria-label={
                                item.status === "uploading"
                                  ? "正在上传并解析"
                                  : "AI 正在提取"
                              }
                              className="spinner"
                            />
                          )}
                          {item.status === "error" &&
                            item.retryable !== false && (
                              <button
                                className="button button-ghost upload-row-button"
                                disabled={uploading}
                                onClick={() => void runUploads([item])}
                                type="button"
                              >
                                重新上传
                              </button>
                            )}
                          {item.response &&
                            item.status !== "queued" &&
                            item.status !== "uploading" && (
                              <button
                                className="button button-ghost upload-row-button"
                                onClick={() => openSuccessfulUpload(item)}
                                type="button"
                              >
                                {item.status === "extracting"
                                  ? "查看状态"
                                  : "查看简历"}
                              </button>
                            )}
                          {item.status !== "uploading" && (
                            <button
                              aria-label={`移除 ${item.file.name}`}
                              className="icon-button"
                              disabled={uploading}
                              onClick={() =>
                                setUploads((current) =>
                                  current.filter(
                                    (entry) => entry.id !== item.id,
                                  ),
                                )
                              }
                              type="button"
                            >
                              <Icon name="close" size={16} />
                            </button>
                          )}
                        </div>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          </div>
          <div className="review-actions upload-actions">
            {(uploading || queuedUploads.length > 0) && (
              <button
                className="button button-primary"
                disabled={!queuedUploads.length || uploading}
                onClick={() => void runUploads(queuedUploads)}
                type="button"
              >
                {uploading ? (
                  <>
                    <i className="spinner" />
                    正在按队列上传…
                  </>
                ) : (
                  <>
                    <Icon name="upload" size={16} />
                    上传 {queuedUploads.length} 份并自动提取
                  </>
                )}
              </button>
            )}
            {retryableFailedUploads.length > 0 && (
              <button
                className="button"
                disabled={uploading}
                onClick={() => void runUploads(retryableFailedUploads)}
                type="button"
              >
                重新上传失败项（{retryableFailedUploads.length}）
              </button>
            )}
          </div>
        </section>
      </div>
    </div>
  );
}
