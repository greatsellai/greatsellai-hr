import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, isApiError } from "../../api";
import type {
  RecruitingAgentContextBindInput,
  RecruitingAgentConversation,
  RecruitingAgentTurnInput,
} from "../../types";

type ConversationRestoreState = "loading" | "ready" | "failed";

const STORAGE_PREFIX = "greatsell-hr:recruiting-agent-conversation:v1";

function storageKey(scope: string): string {
  return `${STORAGE_PREFIX}:${encodeURIComponent(scope)}`;
}

function readStoredConversationId(scope: string): string | null {
  try {
    const value = window.sessionStorage.getItem(storageKey(scope));
    return value?.trim() || null;
  } catch {
    // Browsers can disable sessionStorage. The Agent still works for the
    // current page; it simply cannot restore the opaque work reference.
    return null;
  }
}

function writeStoredConversationId(scope: string, conversationId: string): void {
  try {
    window.sessionStorage.setItem(storageKey(scope), conversationId);
  } catch {
    // See readStoredConversationId: persistence is an enhancement, not a
    // prerequisite for a private server-side Agent work session.
  }
}

function removeStoredConversationId(scope: string): void {
  try {
    window.sessionStorage.removeItem(storageKey(scope));
  } catch {
    // Nothing to do when browser storage is unavailable.
  }
}

interface UseRecruitingAgentConversationOptions {
  /** Changes whenever the authenticated organization or user changes. */
  storageScope: string | null;
}

interface BindTalentSearchRunOptions {
  runId: string;
  jobVersionId: string | null;
}

interface BindTalentSearchProfileOptions {
  profileId: string;
  revisionId: string;
  jobVersionId: string | null;
}

/**
 * Keeps only an opaque server conversation reference in this browser tab.
 *
 * Chat messages, candidate IDs, candidate details, source text and resume
 * content deliberately remain outside sessionStorage. On reload the API
 * returns only a bounded, tenant- and owner-scoped visible transcript plus a
 * fresh work-state summary. The browser never submits history back to it.
 */
export function useRecruitingAgentConversation({
  storageScope,
}: UseRecruitingAgentConversationOptions) {
  const [conversation, setConversation] = useState<RecruitingAgentConversation | null>(null);
  const [restoreState, setRestoreState] = useState<ConversationRestoreState>("loading");
  const [restoreError, setRestoreError] = useState<string | null>(null);
  const restoreSequence = useRef(0);

  const persistConversation = useCallback((next: RecruitingAgentConversation) => {
    setConversation(next);
    setRestoreState("ready");
    setRestoreError(null);
    if (storageScope) writeStoredConversationId(storageScope, next.conversation_id);
  }, [storageScope]);

  const forgetConversation = useCallback(() => {
    if (storageScope) removeStoredConversationId(storageScope);
    setConversation(null);
    setRestoreState("ready");
    setRestoreError(null);
  }, [storageScope]);

  const restoreConversation = useCallback(async (conversationId?: string | null) => {
    const sequence = ++restoreSequence.current;
    if (!storageScope) {
      setConversation(null);
      setRestoreState("ready");
      setRestoreError(null);
      return null;
    }
    const id = conversationId?.trim() || readStoredConversationId(storageScope);
    if (!id) {
      setConversation(null);
      setRestoreState("ready");
      setRestoreError(null);
      return null;
    }
    setRestoreState("loading");
    setRestoreError(null);
    try {
      const restored = await api.getRecruitingAgentConversation(id);
      if (sequence !== restoreSequence.current) return null;
      persistConversation(restored);
      return restored;
    } catch (error) {
      if (sequence !== restoreSequence.current) return null;
      if (isApiError(error) && error.status === 404) {
        forgetConversation();
        return null;
      }
      setRestoreState("failed");
      setRestoreError("暂时无法恢复上次的助手工作范围。");
      throw error;
    }
  }, [forgetConversation, persistConversation, storageScope]);

  useEffect(() => {
    setConversation(null);
    void restoreConversation().catch(() => {
      // The compact status row exposes a retry; do not turn an unavailable
      // restore endpoint into an unhandled browser rejection.
    });
  }, [restoreConversation]);

  const buildTurnInput = useCallback((input: {
    message: string;
    jobVersionId: string | null;
  }): RecruitingAgentTurnInput => ({
    message: input.message,
    job_version_id: input.jobVersionId,
    ...(conversation
      ? {
        conversation_id: conversation.conversation_id,
        context_version: conversation.context_version,
      }
      : {}),
  }), [conversation]);

  const bindTalentSearchRun = useCallback(async ({
    runId,
    jobVersionId,
  }: BindTalentSearchRunOptions): Promise<RecruitingAgentConversation> => {
    const input: RecruitingAgentContextBindInput = {
      context_ref: { kind: "talent_search_run", run_id: runId },
      job_version_id: jobVersionId,
      ...(conversation
        ? {
          conversation_id: conversation.conversation_id,
          context_version: conversation.context_version,
        }
        : {}),
    };
    const bound = await api.bindRecruitingAgentContext(input);
    persistConversation(bound);
    return bound;
  }, [conversation, persistConversation]);

  const bindTalentSearchProfile = useCallback(async ({
    profileId,
    revisionId,
    jobVersionId,
  }: BindTalentSearchProfileOptions): Promise<RecruitingAgentConversation> => {
    const input: RecruitingAgentContextBindInput = {
      context_ref: {
        kind: "talent_search_profile",
        profile_id: profileId,
        revision_id: revisionId,
      },
      job_version_id: jobVersionId,
      ...(conversation
        ? {
          conversation_id: conversation.conversation_id,
          context_version: conversation.context_version,
        }
        : {}),
    };
    const bound = await api.bindRecruitingAgentContext(input);
    persistConversation(bound);
    return bound;
  }, [conversation, persistConversation]);

  const clearConversation = useCallback(async () => {
    if (!conversation) {
      forgetConversation();
      return;
    }
    try {
      await api.deleteRecruitingAgentConversation(conversation.conversation_id);
    } catch (error) {
      // A server-expired conversation is already cleared from the product
      // perspective. Avoid making the recruiter retry a no-op delete.
      if (!isApiError(error) || error.status !== 404) throw error;
    }
    forgetConversation();
  }, [conversation, forgetConversation]);

  return useMemo(() => ({
    adoptConversation: persistConversation,
    buildTurnInput,
    bindTalentSearchRun,
    bindTalentSearchProfile,
    clearConversation,
    conversation,
    forgetConversation,
    isRestoring: restoreState === "loading",
    restoreConversation,
    restoreError,
  }), [
    persistConversation,
    bindTalentSearchProfile,
    bindTalentSearchRun,
    buildTurnInput,
    clearConversation,
    conversation,
    forgetConversation,
    restoreConversation,
    restoreError,
    restoreState,
  ]);
}
