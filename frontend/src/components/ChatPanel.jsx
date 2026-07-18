import React, { useRef, useEffect, useState, useCallback, useMemo } from "react";
import { GlassButton, GlassInput, GlassPanel, GlowCard, NeonDivider, StatusBadge } from "../ui";

// API base resolution:
// - Dev (Vite): keep relative URLs so Vite proxy works
// - Electron prod (file://): window.location.origin becomes "null", so use VITE_API_BASE or window.__NOVA_API_BASE
const API_BASE = (() => {
  try {
    if (import.meta?.env?.DEV) return "";
  } catch {}
  try {
    const fromEnv = import.meta?.env?.VITE_API_BASE ? String(import.meta.env.VITE_API_BASE) : "";
    if (fromEnv) return fromEnv.replace(/\/$/, "");
  } catch {}
  try {
    const w = window;
    const fromWindow = w && w.__NOVA_API_BASE ? String(w.__NOVA_API_BASE) : "";
    if (fromWindow) return fromWindow.replace(/\/$/, "");
  } catch {}
  // Final fallback: backend default (safe for Electron) rather than window.location.origin ("null" under file://)
  return "http://localhost:8008";
})();
async function uploadToServer(files) {
  const fd = new FormData();
  for (const f of files) fd.append("files", f);
  const resp = await fetch(`${API_BASE}/file-upload`, { method: "POST", body: fd });
  if (!resp.ok) throw new Error(await resp.text());
  const data = await resp.json();
  return data.files || [];
}

export default function ChatPanel({
  messages = [],
  onSendMessage,
  onFileUpload,
  onStop,
  isAssistantThinking = false, // removed onRetry
}) {
  // Refs & state
  const inputRef = useRef(null);
  const listRef = useRef(null);
  const endRef = useRef(null);
  const fileInputRef = useRef(null);
  const textAreaRef = useRef(null);

  const [input, setInput] = useState("");
  const [attached, setAttached] = useState([]); // pending uploaded attachments
  const [isDraggingOver, setIsDraggingOver] = useState(false);
  const [autoScroll, setAutoScroll] = useState(true);

  // Fallback handlers so component "just works"
  const _sendMessage =
    onSendMessage ||
    ((text, files) => {
      alert(
        "Sent! (demo fallback): " +
          text +
          (files?.length ? ` [${files.length} file(s)]` : "")
      );
    });
  const _onFileUpload =
    onFileUpload ||
    (async (files) => {
      return uploadToServer(files);
    });

  // Focus on mount
  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  // Autoscroll to latest
  const scrollToEnd = useCallback((behavior = "smooth") => {
    endRef.current?.scrollIntoView({ behavior, block: "end" });
  }, []);
  useEffect(() => {
    if (autoScroll) scrollToEnd(messages.length < 5 ? "auto" : "smooth");
  }, [messages, autoScroll, scrollToEnd]);

  // Track manual scrolling
  const handleScroll = useCallback(() => {
    const el = listRef.current;
    if (!el) return;
    const threshold = 48;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < threshold;
    setAutoScroll(atBottom);
  }, []);

  // Textarea auto-resize
  const autoSize = useCallback(() => {
    const ta = textAreaRef.current;
    if (!ta) return;
    ta.style.height = "0px";
    const h = Math.min(220, ta.scrollHeight);
    ta.style.height = h + "px";
  }, []);
  useEffect(() => autoSize(), [input, autoSize]);

  // Keyboard handling
  const handleKeyDown = (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      if (input.trim() || attached.length) handleSubmit();
      return;
    }
    // ArrowUp to recall last user message when input empty
    if (e.key === "ArrowUp" && !input.trim()) {
      const lastUser = [...messages].reverse().find((m) => m.sender === "user");
      if (lastUser?.text) setInput(lastUser.text);
    }
  };

  // Send
  const handleSubmit = () => {
    const text = input.trim();
    if (!text && attached.length === 0) return;
    _sendMessage(text, attached);
    setInput("");
    setAttached([]);
    if (fileInputRef.current) fileInputRef.current.value = "";
    setTimeout(() => scrollToEnd("auto"), 0);
  };

  // Files: input change
  const handleFileChange = async (e) => {
    if (e.target.files && e.target.files.length) {
      const files = Array.from(e.target.files);
      try {
        const uploaded = await _onFileUpload(files);
        setAttached((prev) => [...prev, ...uploaded]);
      } finally {
        e.target.value = "";
      }
    }
  };

  // Files: paste
  const handlePaste = useCallback(
    async (e) => {
      if (e.clipboardData?.files?.length) {
        e.preventDefault();
        const files = Array.from(e.clipboardData.files);
        const uploaded = await _onFileUpload(files);
        setAttached((prev) => [...prev, ...uploaded]);
      }
    },
    [_onFileUpload]
  );
  useEffect(() => {
    const el = inputRef.current;
    if (!el) return;
    el.addEventListener("paste", handlePaste);
    return () => el.removeEventListener("paste", handlePaste);
  }, [handlePaste]);

  // Files: drag & drop
  const onDragEnter = (e) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDraggingOver(true);
  };
  const onDragOver = (e) => {
    e.preventDefault();
    e.stopPropagation();
  };
  const onDragLeave = (e) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDraggingOver(false);
  };
  const onDrop = async (e) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDraggingOver(false);
    const files = Array.from(e.dataTransfer.files || []);
    if (files.length) {
      const uploaded = await _onFileUpload(files);
      setAttached((prev) => [...prev, ...uploaded]);
    }
  };

  // Render text: code fences + linkify + streaming caret
  const renderMessageText = useCallback((text, streaming) => {
    const parts = [];
    const fence = /```([\w+-]*)\n([\s\S]*?)```/g;
    let lastIndex = 0;
    let m;
    while ((m = fence.exec(text)) !== null) {
      const [full, lang, code] = m;
      if (m.index > lastIndex) {
        parts.push(linkify(text.slice(lastIndex, m.index)));
      }
      parts.push(<CodeBlock key={`code-${m.index}`} code={code} lang={lang || ""} />);
      lastIndex = m.index + full.length;
    }
    if (lastIndex < text.length) {
      parts.push(linkify(text.slice(lastIndex)));
    }
    if (streaming) {
      parts.push(
        <span
          key="caret"
          className="inline-block w-2 h-4 align-bottom animate-pulse bg-nova-gold ml-1 rounded-[2px]"
        />
      );
    }
    return <>{parts}</>;
  }, []);

  const isStreaming =
    isAssistantThinking ||
    (!!messages.length && messages[messages.length - 1]?.streaming);

  const modelMissing = useMemo(() => {
    try {
      for (let i = messages.length - 1; i >= 0; i--) {
        const m = messages[i];
        if (!m || m.sender !== "nova") continue;
        const t = String(m.text || "");
        if (t.includes("No GGUF model found") || t.includes("*.gguf") || t.includes("NOVA_MODEL_PATH")) return true;
        // only consider the most recent nova message
        break;
      }
    } catch {}
    return false;
  }, [messages]);

  const canOpenModelFolder = useMemo(() => {
    try {
      return typeof window !== "undefined" && typeof window.novaDesktop?.openModelFolder === "function";
    } catch {}
    return false;
  }, []);

  const formatTimestamp = useCallback((msg) => {
    if (msg?.timestamp) {
      const d = new Date(msg.timestamp);
      if (!Number.isNaN(d.getTime())) return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    }
    if (typeof msg?.id === "string") {
      const m = msg.id.match(/-(\d{10,})$/);
      if (m) {
        const d = new Date(Number(m[1]));
        if (!Number.isNaN(d.getTime())) return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
      }
    }
    return "";
  }, []);

  return (
    <GlassPanel
      as="form"
      variant="strong"
      neon
      glow="purple"
      className="nova-chat-panel"
      style={{ boxSizing: "border-box" }}
      onSubmit={(e) => {
        e.preventDefault();
        handleSubmit();
      }}
      autoComplete="off"
      onDragEnter={onDragEnter}
      onDragOver={onDragOver}
      onDragLeave={onDragLeave}
      onDrop={onDrop}
      aria-label="Chat panel"
    >
      {/* Drag overlay */}
      {isDraggingOver && (
        <div className="nova-chat-drop-zone">
          <div>Drop files to attach</div>
        </div>
      )}

      <span className="nova-chat-edge-accent" aria-hidden="true" />

      <div className="nova-chat-header">
        <div className="nova-chat-identity">
          <span className="nova-chat-signal" aria-hidden="true">
            <i /><i /><i /><i /><i />
          </span>
          <div>
            <div className="nova-chat-eyebrow">Holographic channel</div>
            <div className="nova-chat-title">Nova Conversation</div>
          </div>
        </div>
        <StatusBadge
          status={isAssistantThinking || isStreaming ? "warning" : "online"}
          pulse={isAssistantThinking || isStreaming}
          label={isAssistantThinking ? "Thinking" : isStreaming ? "Streaming" : "Online"}
          className="nova-chat-status"
        />
      </div>

      <NeonDivider tone="mixed" className="nova-chat-divider" />

      {modelMissing && (
        <GlowCard as="div" tone="gold" className="nova-chat-model-warning">
          <div className="nova-chat-model-warning-content">
            <div className="nova-chat-model-warning-copy">
              <div className="nova-chat-model-warning-title">Model not found</div>
              <div className="nova-chat-model-warning-detail">
                Drop a <span className="font-mono">*.gguf</span> into <span className="font-mono">%APPDATA%\\Nova\\model</span>
              </div>
            </div>
            {canOpenModelFolder && (
              <GlassButton
                type="button"
                variant="gold"
                className="nova-chat-model-button"
                onClick={() => {
                  try {
                    window.novaDesktop.openModelFolder();
                  } catch {}
                }}
                title="Open model folder"
              >
                Open folder
              </GlassButton>
            )}
          </div>
        </GlowCard>
      )}

      {/* Messages list */}
      <div
        ref={listRef}
        className="nova-chat-messages"
        onScroll={handleScroll}
      >
        {messages.length === 0 && (
          <div className="nova-chat-empty" aria-hidden="true">
            <span className="nova-chat-empty-orbit" />
            <div className="nova-chat-empty-wordmark">NOVA</div>
            <div className="nova-chat-empty-copy">Conversation channel ready</div>
          </div>
        )}

        {messages.map((msg, i) => {
          const isSystem = msg.sender === "system";
          const isUser = msg.sender === "user";
          if (isSystem) {
            return (
              <div key={msg.id ?? i} className="nova-chat-system-row">
                <span className="nova-chat-system-message">
                  {msg.text}
                </span>
              </div>
            );
          }
          return (
            <div key={msg.id ?? i} className={`nova-chat-message-row ${isUser ? "nova-chat-message-row--user" : "nova-chat-message-row--nova"}`}>
              <Bubble isUser={isUser} streaming={!!msg.streaming}>
                <div className="nova-chat-message-body">
                  {renderMessageText(msg.text || "", msg.streaming)}
                </div>

                <div className="nova-chat-message-meta">
                  {isUser ? "You" : "Nova"} {formatTimestamp(msg)}
                </div>

                {!!msg.files?.length && (
                  <div className="nova-chat-file-list">
                    {msg.files.map((f, idx) => (
                      <span
                        key={idx}
                        className="nova-chat-file-chip"
                        title={f.name}
                      >
                        📎 {truncate(f.name, 28)}
                        {f.url ? (
                          <a
                            href={API_BASE ? `${API_BASE}${f.url}` : f.url}
                            target="_blank"
                            rel="noreferrer"
                            className="nova-chat-link"
                          >
                            open
                          </a>
                        ) : null}
                      </span>
                    ))}
                  </div>
                )}

                {!!msg.images?.length && (
                  <div className="nova-chat-image-list">
                    {msg.images.map((img, idx) => (
                      <a
                        key={idx}
                        href={img.url}
                        target="_blank"
                        rel="noreferrer"
                        title={img.prompt || "Generated image"}
                      >
                        <img
                          src={img.url}
                          alt={img.prompt || "Generated image"}
                          className="nova-chat-generated-image"
                        />
                      </a>
                    ))}
                  </div>
                )}

                {msg.error && (
                  <div className="nova-chat-message-error">⚠ {msg.error}</div>
                )}
              </Bubble>
            </div>
          );
        })}

        {isAssistantThinking && (
          <div className="nova-chat-thinking-row">
            <span className="nova-chat-thinking">
              <TypingDots />
              <span>thinking…</span>
            </span>
          </div>
        )}

        <div ref={endRef} />
      </div>

      {!autoScroll && (
        <GlassButton
          type="button"
          variant="ghost"
          onClick={() => {
            setAutoScroll(true);
            scrollToEnd("smooth");
          }}
          className="nova-chat-jump"
          aria-label="Jump to latest messages"
        >
          Jump to latest ↓
        </GlassButton>
      )}

      {!!attached.length && (
        <div className="nova-chat-attachment-tray">
          {attached.map((f, i) => (
            <span
              key={i}
              className="nova-chat-file-chip"
            >
              📎 {truncate(f.name, 28)}
              {f.url ? (
                <a
                  href={API_BASE ? `${API_BASE}${f.url}` : f.url}
                  target="_blank"
                  rel="noreferrer"
                  className="nova-chat-link"
                >
                  open
                </a>
              ) : null}
              <button
                type="button"
                className="nova-chat-file-remove"
                onClick={() =>
                  setAttached((prev) => prev.filter((_, idx) => idx !== i))
                }
                aria-label={`Remove ${f.name}`}
                title="Remove"
              >
                ✕
              </button>
            </span>
          ))}
        </div>
      )}

      <GlassPanel variant="subtle" className="nova-chat-composer">
        <GlassInput
          as="textarea"
          ref={(el) => {
            inputRef.current = el;
            textAreaRef.current = el;
          }}
          className="nova-chat-input"
          placeholder="Type a message…"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          aria-label="Message input"
        />

        <GlassButton
          as="label"
          variant="ghost"
          className="nova-chat-file-button"
          title="Upload files"
        >
          <input
            ref={fileInputRef}
            type="file"
            className="hidden"
            onChange={handleFileChange}
            multiple
          />
          <svg
            className="w-5 h-5 mr-1"
            fill="none"
            stroke="currentColor"
            strokeWidth={2}
            viewBox="0 0 24 24"
            aria-hidden="true"
          >
            <path strokeLinecap="round" strokeLinejoin="round" d="M12 4v16m8-8H4" />
          </svg>
          <span className="hidden sm:inline">File</span>
        </GlassButton>

        {isStreaming && (
          <GlassButton
            type="button"
            variant="ghost"
            onClick={() => onStop?.()}
            className="nova-chat-stop-button"
            title="Stop generating"
          >
            Stop
          </GlassButton>
        )}

        <GlassButton
          type="submit"
          variant="gold"
          className="nova-chat-send-button"
          title="Send (Enter)"
        >
          Send
        </GlassButton>
      </GlassPanel>
    </GlassPanel>
  );
}

/* ---------- Bubble (futuristic skin) ---------- */

function Bubble({ isUser, streaming, children }) {
  return (
    <div className="nova-chat-bubble-wrap">
      <GlowCard
        as="div"
        tone={isUser ? "gold" : "purple"}
        className={[
          "nova-chat-bubble",
          isUser ? "nova-chat-bubble--user" : "nova-chat-bubble--nova",
          streaming && "nova-chat-bubble--streaming",
        ].filter(Boolean).join(" ")}
      >
        {children}
      </GlowCard>
    </div>
  );
}

/* ---------- Small helpers & subcomponents ---------- */

function truncate(s, n) {
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

function TypingDots() {
  return (
    <span aria-hidden className="nova-chat-typing-dots">
      <span />
      <span />
      <span />
    </span>
  );
}

function CodeBlock({ code, lang }) {
  const [copied, setCopied] = useState(false);
  const doCopy = async () => {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      setTimeout(() => setCopied(false), 900);
    } catch {}
  };
  return (
    <div className="nova-chat-code group">
      <pre>
        <div className="nova-chat-code-language">{lang || "code"}</div>
        <code className="whitespace-pre">{code}</code>
      </pre>
      <GlassButton
        type="button"
        variant="ghost"
        onClick={doCopy}
        className="nova-chat-code-copy"
        title="Copy"
      >
        {copied ? "Copied" : "Copy"}
      </GlassButton>
    </div>
  );
}

// Convert plain text URLs to <a> tags
function linkify(chunk) {
  const urlRe = /((https?:\/\/|www\.)[^\s<]+)/g;
  const nodes = [];
  let last = 0;
  let m;
  while ((m = urlRe.exec(chunk)) !== null) {
    if (m.index > last) nodes.push(chunk.slice(last, m.index));
    let href = m[0];
    if (href.startsWith("www.")) href = "https://" + href;
    nodes.push(
      <a
        key={`u-${m.index}`}
        href={href}
        target="_blank"
        rel="noreferrer"
        className="nova-chat-link"
      >
        {m[0]}
      </a>
    );
    last = m.index + m[0].length;
  }
  if (last < chunk.length) nodes.push(chunk.slice(last));
  return <>{nodes}</>;
}
