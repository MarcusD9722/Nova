import React, { useRef, useEffect, useState, useCallback, useMemo } from "react";

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
  const [attached, setAttached] = useState([]); // pending File[]
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
      const uploaded = await uploadToServer(files);
      setAttached((prev) => [...prev, ...uploaded]);
      return uploaded;
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
          className="inline-block w-2 h-4 align-bottom animate-pulse bg-cyan-300/80 ml-1 rounded-[2px]"
        />
      );
    }
    return <>{parts}</>;
  }, []);

  const isStreaming =
    isAssistantThinking ||
    (!!messages.length && messages[messages.length - 1]?.streaming);

  return (
    <form
      className="relative flex flex-col w-full h-full min-w-0 min-h-0
      bg-gradient-to-br from-[#13142a]/80 via-[#262b41]/70 to-[#181928]/90
      backdrop-blur-2xl border border-cyan-500/30 rounded-3xl
      shadow-[0_8px_32px_0_rgba(12,16,56,0.36)]
      before:absolute before:inset-0 before:rounded-3xl before:opacity-60 before:pointer-events-none
      before:bg-gradient-to-br before:from-cyan-400/20 before:to-fuchsia-500/10
      after:absolute after:inset-0 after:rounded-3xl after:pointer-events-none after:ring-2 after:ring-cyan-400/20"
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
        <div className="absolute inset-0 z-20 rounded-3xl bg-cyan-400/10 ring-2 ring-cyan-300/40 grid place-items-center pointer-events-none">
          <div className="text-cyan-100 font-medium">Drop files to attach</div>
        </div>
      )}

      {/* Neon top glow */}
      <div className="absolute top-0 left-1/2 -translate-x-1/2 w-4/5 h-1.5 bg-gradient-to-r from-cyan-400/40 via-fuchsia-400/20 to-cyan-400/40 blur-lg opacity-80 rounded-b-3xl pointer-events-none" />

      {/* Messages list */}
      <div
        ref={listRef}
        className="flex-1 overflow-y-auto mb-2 min-h-0 pt-1 px-1"
        onScroll={handleScroll}
      >
        {messages.map((msg, i) => {
          const isSystem = msg.sender === "system";
          const isUser = msg.sender === "user";
          if (isSystem) {
            return (
              <div key={msg.id ?? i} className="my-2 text-center">
                <span className="inline-block text-[11px] px-2 py-1 rounded-full bg-white/5 border border-white/10 text-white/60">
                  {msg.text}
                </span>
              </div>
            );
          }
          return (
            <div key={msg.id ?? i} className={`mb-1 ${isUser ? "text-right" : "text-left"}`}>
              <Bubble isUser={isUser} streaming={!!msg.streaming}>
                <div className="whitespace-pre-wrap break-words leading-relaxed">
                  {renderMessageText(msg.text || "", msg.streaming)}
                </div>

                {!!msg.files?.length && (
                  <div className="mt-2 flex flex-wrap gap-2">
                    {msg.files.map((f, idx) => (
                      <span
                        key={idx}
                        className="text-xs px-2 py-1 rounded-lg border border-white/10 bg-black/20 hover:bg-black/30"
                        title={f.name}
                      >
                        📎 {truncate(f.name, 28)}
                        {f.url ? (
                          <a
                            href={API_BASE ? `${API_BASE}${f.url}` : f.url}
                            target="_blank"
                            rel="noreferrer"
                            className="underline ml-1"
                          >
                            open
                          </a>
                        ) : null}
                      </span>
                    ))}
                  </div>
                )}

                {msg.error && (
                  <div className="mt-1 text-[11px] text-red-300/80">⚠ {msg.error}</div>
                )}
              </Bubble>
            </div>
          );
        })}

        {isAssistantThinking && (
          <div className="mb-1 text-left">
            <span className="inline-flex items-center gap-1 rounded-2xl px-3 py-1.5 bg-white/10 border border-cyan-200/10 text-cyan-200">
              <TypingDots />
              <span className="text-xs opacity-80">thinking…</span>
            </span>
          </div>
        )}

        <div ref={endRef} />
      </div>

      {!autoScroll && (
        <button
          type="button"
          onClick={() => {
            setAutoScroll(true);
            scrollToEnd("smooth");
          }}
          className="absolute bottom-24 right-4 z-10 text-xs px-2 py-1 rounded-md bg-black/40 border border-white/10 text-white/80 hover:text-white hover:bg-black/60"
          aria-label="Jump to latest messages"
        >
          Jump to latest ↓
        </button>
      )}

      {!!attached.length && (
        <div className="mx-1 mb-1 flex flex-wrap gap-2">
          {attached.map((f, i) => (
            <span
              key={i}
              className="text-xs px-2 py-1 rounded-lg border border-white/10 bg-black/30 text-white/80 flex items-center gap-1"
            >
              📎 {truncate(f.name, 28)}
              {f.url ? (
                <a
                  href={API_BASE ? `${API_BASE}${f.url}` : f.url}
                  target="_blank"
                  rel="noreferrer"
                  className="underline ml-1"
                >
                  open
                </a>
              ) : null}
              <button
                type="button"
                className="ml-1 text-white/50 hover:text-white"
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

      <div className="flex gap-2 mt-auto p-1">
        <textarea
          ref={(el) => {
            inputRef.current = el;
            textAreaRef.current = el;
          }}
          className="flex-1 rounded-xl px-4 py-2 bg-black/30 border border-cyan-600/30 outline-none text-cyan-100
          placeholder:text-cyan-200/60 font-mono shadow focus:ring-2 focus:ring-cyan-500/40 transition
          max-h-[220px] resize-none"
          placeholder="Type a message…"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          aria-label="Message input"
        />

        <label
          className="cursor-pointer bg-gradient-to-br from-cyan-500 to-fuchsia-500 text-white rounded-xl px-3 py-2 shadow-lg
          hover:scale-105 transition-all duration-100 flex items-center"
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
        </label>

        {isStreaming && (
          <button
            type="button"
            onClick={() => onStop?.()}
            className="rounded-xl px-3 py-2 bg-black/40 border border-white/10 text-white/80 hover:text-white"
            title="Stop generating"
          >
            Stop
          </button>
        )}

        <button
          type="submit"
          className="rounded-xl px-4 py-2 bg-gradient-to-br from-cyan-500 to-fuchsia-500 text-white shadow-lg
          hover:scale-105 hover:bg-cyan-600/90 transition-all duration-100"
          title="Send (Enter)"
        >
          Send
        </button>
      </div>
    </form>
  );
}

/* ---------- Bubble (futuristic skin) ---------- */

function Bubble({ isUser, streaming, children }) {
  const side = isUser ? "items-end" : "items-start";
  const tail = isUser ? "bubble-tail-right" : "bubble-tail-left";
  const roleSkin = isUser ? "bubble-user glow-breathe-user" : "bubble-assistant glow-breathe-assistant";
  const streamBoost = streaming ? "glow-stream" : "";

  return (
    <div className={`inline-block max-w-[85%] ${side}`}>
      <div
        className={[
          "bubble-base nova-glass nova-border nova-scan nova-sheen nova-float",
          roleSkin,
          tail,
          streamBoost,
        ].join(" ")}
      >
        {children}
      </div>
    </div>
  );
}

/* ---------- Small helpers & subcomponents ---------- */

function truncate(s, n) {
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

function TypingDots() {
  return (
    <span aria-hidden className="inline-flex gap-1 px-1">
      <span className="w-1.5 h-1.5 bg-cyan-300/80 rounded-full animate-bounce [animation-delay:-0.2s]" />
      <span className="w-1.5 h-1.5 bg-cyan-300/80 rounded-full animate-bounce" />
      <span className="w-1.5 h-1.5 bg-cyan-300/80 rounded-full animate-bounce [animation-delay:0.2s]" />
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
    <div className="group relative my-2">
      <pre className="rounded-xl border border-white/10 bg-black/40 px-3 py-2 overflow-auto text-[12.5px] leading-6">
        <div className="text-[11px] opacity-60 mb-1">{lang || "code"}</div>
        <code className="whitespace-pre">{code}</code>
      </pre>
      <button
        type="button"
        onClick={doCopy}
        className="absolute top-2 right-2 text-xs px-2 py-1 rounded-md bg-black/40 border border-white/10 text-white/70 hover:text-white opacity-0 group-hover:opacity-100 transition"
        title="Copy"
      >
        {copied ? "Copied" : "Copy"}
      </button>
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
        className="underline decoration-cyan-400/60 hover:decoration-cyan-300"
      >
        {m[0]}
      </a>
    );
    last = m.index + m[0].length;
  }
  if (last < chunk.length) nodes.push(chunk.slice(last));
  return <>{nodes}</>;
}
