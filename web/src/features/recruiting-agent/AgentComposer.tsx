import {
  lazy,
  Suspense,
  useEffect,
  useId,
  useImperativeHandle,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
  type RefObject,
} from "react";
import type SemiAIChatInputInstance from "@douyinfe/semi-ui-19/lib/es/aiChatInput";
import type {
  Content,
  MessageContent,
  Reference,
} from "@douyinfe/semi-ui-19/lib/es/aiChatInput/interface";
import { IconAILoading } from "@douyinfe/semi-icons";
import { Icon } from "../../icons";

const SemiAIChatInput = lazy(
  () => import("@douyinfe/semi-ui-19/lib/es/aiChatInput"),
);

export type AgentReferenceKind =
  | "candidate"
  | "job"
  | "filter"
  | "talent_profile";

export interface AgentReference {
  /** Kept only in React state. Active references are always server-issued. */
  referenceId: string;
  kind: AgentReferenceKind;
  label: string;
  description?: string;
}

export interface AgentComposerHandle {
  focus: () => void;
}

const referenceKindLabel: Record<AgentReferenceKind, string> = {
  candidate: "候选人",
  job: "关联 JD",
  filter: "当前筛选",
  talent_profile: "人才画像",
};

interface AgentComposerProps {
  value: string;
  references: AgentReference[];
  availableReferences: AgentReference[];
  disabled: boolean;
  generating: boolean;
  inputRef: RefObject<AgentComposerHandle | null>;
  onChange: (value: string) => void;
  onRemoveReference: (referenceId: string) => void;
  onSelectReference: (reference: AgentReference) => void;
  onSubmit: (message: string) => void;
}

function referenceMatchesQuery(reference: AgentReference, query: string): boolean {
  const normalized = query.trim().toLocaleLowerCase();
  if (!normalized) return true;
  return [
    reference.label,
    reference.description,
    referenceKindLabel[reference.kind],
  ].some((item) => item?.toLocaleLowerCase().includes(normalized));
}

function referenceIcon(reference: Pick<AgentReference, "kind">): "user" | "briefcase" | "filter" | "spark" {
  if (reference.kind === "candidate") return "user";
  if (reference.kind === "job") return "briefcase";
  if (reference.kind === "filter") return "filter";
  return "spark";
}

function contentToPlainText(contents: Content[] | undefined): string {
  return (contents ?? []).reduce((message, item) => (
    item.type === "text" && typeof item.text === "string"
      ? `${message}${item.text}`
      : message
  ), "");
}

function editorHtml(value: string): string {
  if (!value) return "";
  return value
    .split("\n")
    .map((line) => `<p>${line
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")}</p>`)
    .join("");
}

export function AgentComposer({
  value,
  references,
  availableReferences,
  disabled,
  generating,
  inputRef,
  onChange,
  onRemoveReference,
  onSelectReference,
  onSubmit,
}: AgentComposerProps) {
  const menuId = useId();
  const aiInputRef = useRef<SemiAIChatInputInstance | null>(null);
  const inputHostRef = useRef<HTMLDivElement | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);
  const lastEditorValueRef = useRef(value);
  const [menuOpen, setMenuOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [activeIndex, setActiveIndex] = useState(0);
  const [menuStyle, setMenuStyle] = useState<CSSProperties>({});
  const inputLocked = disabled || generating;

  const selectedIds = useMemo(
    () => new Set(references.map((reference) => reference.referenceId)),
    [references],
  );
  const matches = useMemo(
    () => availableReferences.filter((reference) => (
      !selectedIds.has(reference.referenceId) && referenceMatchesQuery(reference, query)
    )),
    [availableReferences, query, selectedIds],
  );
  const activeOptionId = matches[activeIndex]
    ? `${menuId}-option-${activeIndex}`
    : undefined;
  const inputReferences = useMemo<Reference[]>(() => references.map((reference) => ({
    id: reference.referenceId,
    type: "text",
    content: reference.label,
    kind: reference.kind,
  })), [references]);

  useImperativeHandle(inputRef, () => ({
    focus: () => {
      if (!inputLocked) aiInputRef.current?.focusEditor(0);
    },
  }), [inputLocked]);

  useEffect(() => {
    const host = inputHostRef.current;
    if (!host) return undefined;
    const applyEditorAccessibility = () => {
      const editor = host.querySelector<HTMLElement>("[contenteditable]");
      if (!editor) return;
      editor.setAttribute("aria-label", "向招聘助手提问");
      editor.setAttribute("aria-multiline", "true");
      editor.setAttribute("role", "textbox");
      editor.setAttribute("aria-disabled", String(inputLocked));
      editor.setAttribute("contenteditable", String(!inputLocked));
      aiInputRef.current?.getEditor()?.setEditable(!inputLocked);
      host.querySelector<HTMLButtonElement>(".semi-aiChatInput-footer-action-send")
        ?.setAttribute("aria-label", "发送提问");
    };
    applyEditorAccessibility();
    const observer = new MutationObserver(applyEditorAccessibility);
    observer.observe(host, { childList: true, subtree: true });
    return () => observer.disconnect();
  }, [inputLocked]);

  useEffect(() => {
    if (value === lastEditorValueRef.current) return;
    lastEditorValueRef.current = value;
    aiInputRef.current?.setContent(editorHtml(value));
  }, [value]);

  const positionMenu = () => {
    const trigger = triggerRef.current;
    if (!trigger) return;
    const rect = trigger.getBoundingClientRect();
    const width = Math.min(22 * 16, window.innerWidth - 24);
    const left = Math.min(
      Math.max(12, rect.left),
      Math.max(12, window.innerWidth - width - 12),
    );
    setMenuStyle({
      position: "fixed",
      width,
      left,
      bottom: Math.max(12, window.innerHeight - rect.top + 12),
    });
  };

  const closeMenu = (restoreFocus = false) => {
    setMenuOpen(false);
    setQuery("");
    setActiveIndex(0);
    if (restoreFocus) window.requestAnimationFrame(() => aiInputRef.current?.focusEditor(0));
  };

  const openMenu = () => {
    if (disabled || generating) return;
    positionMenu();
    setMenuOpen(true);
    setActiveIndex(0);
  };

  useEffect(() => {
    if (!menuOpen) return undefined;
    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target as Node;
      if (
        !menuRef.current?.contains(target)
        && !triggerRef.current?.contains(target)
      ) {
        closeMenu();
      }
    };
    const reposition = () => positionMenu();
    const handleEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      closeMenu(true);
    };
    document.addEventListener("pointerdown", handlePointerDown);
    document.addEventListener("keydown", handleEscape, true);
    window.addEventListener("resize", reposition);
    window.addEventListener("scroll", reposition, true);
    return () => {
      document.removeEventListener("pointerdown", handlePointerDown);
      document.removeEventListener("keydown", handleEscape, true);
      window.removeEventListener("resize", reposition);
      window.removeEventListener("scroll", reposition, true);
    };
  }, [menuOpen]);

  useEffect(() => {
    if (activeIndex >= matches.length) setActiveIndex(Math.max(0, matches.length - 1));
  }, [activeIndex, matches.length]);

  const selectReference = (reference: AgentReference) => {
    onSelectReference(reference);
    closeMenu(true);
  };

  const handleMenuKeyDown = (event: ReactKeyboardEvent<HTMLInputElement>) => {
    if (event.key === "Escape") {
      event.preventDefault();
      closeMenu(true);
      return;
    }
    if (event.key === "ArrowDown" && matches.length) {
      event.preventDefault();
      setActiveIndex((current) => (current + 1) % matches.length);
      return;
    }
    if (event.key === "ArrowUp" && matches.length) {
      event.preventDefault();
      setActiveIndex((current) => (current - 1 + matches.length) % matches.length);
      return;
    }
    if (event.key === "Enter" && !event.nativeEvent.isComposing) {
      const active = matches[activeIndex];
      if (active) {
        event.preventDefault();
        selectReference(active);
      }
    }
  };

  const handleInputKeyDownCapture = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (inputLocked) {
      if (event.key !== "Tab") {
        event.preventDefault();
        event.stopPropagation();
      }
      return;
    }
    if (event.nativeEvent.isComposing) {
      event.stopPropagation();
      return;
    }
    if (event.key === "@") {
      event.preventDefault();
      event.stopPropagation();
      openMenu();
    }
  };

  const handleMessageSend = ({ inputContents }: MessageContent) => {
    const message = contentToPlainText(inputContents).trim();
    if (!message || disabled || generating) return;
    aiInputRef.current?.setContent("");
    lastEditorValueRef.current = "";
    onChange("");
    onSubmit(message);
  };

  return (
    <div className="agent-composer" data-testid="agent-composer">
      <div
        aria-disabled={inputLocked}
        className="agent-composer-input-host"
        onBeforeInputCapture={(event) => {
          if (inputLocked) event.preventDefault();
        }}
        onKeyDownCapture={handleInputKeyDownCapture}
        onPasteCapture={(event) => {
          if (inputLocked) event.preventDefault();
        }}
        ref={inputHostRef}
      >
        <Suspense
          fallback={<div aria-busy="true" className="agent-ai-chat-input-fallback">正在加载输入框</div>}
        >
          <SemiAIChatInput
            canSend={inputLocked ? false : undefined}
            className={`agent-ai-chat-input${inputLocked ? " is-pending" : ""}`}
            clearContentOnGenerating={false}
            generating={generating}
            immediatelyRender={false}
            onContentChange={(contents) => {
              const nextValue = contentToPlainText(contents);
              // AIChatInput reports its initial empty document while mounting.
              // It is already represented by the controlled value, so avoid a
              // redundant parent state update during the child mount.
              if (nextValue === lastEditorValueRef.current) return;
              lastEditorValueRef.current = nextValue;
              onChange(nextValue);
            }}
            onMessageSend={handleMessageSend}
            placeholder="描述要查找、比较或核验的内容，输入 @ 引用相关资料"
            ref={aiInputRef}
            references={inputReferences}
            renderActionArea={({ className, menuItem }) => (
              <div className={`${className} agent-ai-chat-input-actions`}>
                <button
                  aria-controls={`${menuId}-list`}
                  aria-expanded={menuOpen}
                  aria-haspopup="listbox"
                  aria-label="添加引用"
                  className="agent-reference-trigger"
                  disabled={disabled || generating}
                  onClick={openMenu}
                  ref={triggerRef}
                  type="button"
                >
                  @
                </button>
                {generating ? (
                  <span aria-live="polite" className="agent-ai-input-generating">
                    <IconAILoading aria-hidden="true" size="small" />
                    <span>生成中</span>
                  </span>
                ) : (
                  <>
                    <span className="agent-composer-keyboard-hint" aria-hidden="true">Enter 发送</span>
                    {menuItem}
                  </>
                )}
              </div>
            )}
            renderReference={(reference) => {
              const kind = reference.kind as AgentReferenceKind;
              return (
                <div className="agent-ai-reference" key={reference.id}>
                  <Icon name={referenceIcon({ kind })} size={14} />
                  <span>{typeof reference.content === "string" ? reference.content : "已引用资料"}</span>
                  <button
                    aria-label={`移除引用：${typeof reference.content === "string" ? reference.content : "已引用资料"}`}
                    disabled={disabled || generating}
                    onClick={() => onRemoveReference(reference.id)}
                    type="button"
                  >
                    <Icon name="close" size={12} />
                  </button>
                </div>
              );
            }}
            round
            sendHotKey="enter"
            showUploadButton={false}
            showUploadFile={false}
          />
        </Suspense>
      </div>
      {menuOpen && (
        <div
          className="agent-reference-menu"
          id={menuId}
          ref={menuRef}
          style={menuStyle}
        >
          <div className="agent-reference-menu-search">
            <Icon name="search" size={15} />
            <input
              aria-activedescendant={activeOptionId}
              aria-autocomplete="list"
              aria-controls={`${menuId}-list`}
              aria-expanded="true"
              aria-haspopup="listbox"
              aria-label="搜索要引用的资料"
              autoFocus
              onChange={(event) => {
                setQuery(event.target.value);
                setActiveIndex(0);
              }}
              onKeyDown={handleMenuKeyDown}
              placeholder="搜索可引用内容"
              role="combobox"
              value={query}
            />
            <button aria-label="关闭引用菜单" onClick={() => closeMenu(true)} type="button">
              <Icon name="close" size={14} />
            </button>
          </div>
          <div
            aria-label="选择要引用的资料"
            className="agent-reference-menu-list"
            id={`${menuId}-list`}
            role="listbox"
          >
            {matches.length ? matches.map((reference, index) => (
              <button
                aria-selected={index === activeIndex}
                className={index === activeIndex ? "is-active" : undefined}
                id={`${menuId}-option-${index}`}
                key={reference.referenceId}
                onMouseEnter={() => setActiveIndex(index)}
                onMouseDown={(event) => event.preventDefault()}
                onClick={() => selectReference(reference)}
                role="option"
                type="button"
              >
                <Icon name={referenceIcon(reference)} size={16} />
                <span>
                  <strong>{reference.label}</strong>
                  {reference.description && <small>{reference.description}</small>}
                </span>
                <em>{referenceKindLabel[reference.kind]}</em>
              </button>
            )) : (
              <p className="agent-reference-menu-empty">当前没有可引用的内容。</p>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
