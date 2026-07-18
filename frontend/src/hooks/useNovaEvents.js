import { useEffect, useRef, useState, useCallback } from "react";
import { novaApiToken, novaApiBase } from "../lib/apiToken";

// Live connection to the backend event bus (/ws/events).
// Drives the UI's real activity states: thinking, tool use, memory access,
// vision, web search, TTS generation. Falls back to disconnected state (the
// UI shows offline) — never fakes activity.

function wsUrl() {
  const base = novaApiBase().replace(/^http/, "ws");
  // Browsers can't set WS headers, so the API token rides as a query param
  // (the backend checks it before accepting the socket).
  const token = novaApiToken();
  return base + "/ws/events" + (token ? `?token=${encodeURIComponent(token)}` : "");
}

const MAX_EVENTS = 100;
// One-shot events stay visible this long so the UI can show a pulse.
const FLASH_MS = 2600;

export default function useNovaEvents({ enabled = true } = {}) {
  const [connected, setConnected] = useState(false);
  const [events, setEvents] = useState([]);
  const [activity, setActivity] = useState({
    thinking: false,
    tool: null, // active tool name
    memory: null, // "read" | "write" | "search"
    vision: false,
    web: false,
    ttsGenerating: false,
    lastError: null,
  });

  const wsRef = useRef(null);
  const timersRef = useRef({});
  const closedRef = useRef(false);

  const flash = useCallback((key, value) => {
    setActivity((prev) => ({ ...prev, [key]: value }));
    try {
      if (timersRef.current[key]) window.clearTimeout(timersRef.current[key]);
    } catch {}
    timersRef.current[key] = window.setTimeout(() => {
      setActivity((prev) => ({ ...prev, [key]: key === "memory" || key === "tool" ? null : false }));
    }, FLASH_MS);
  }, []);

  const hold = useCallback((key, value) => {
    try {
      if (timersRef.current[key]) window.clearTimeout(timersRef.current[key]);
    } catch {}
    setActivity((prev) => ({ ...prev, [key]: value }));
  }, []);

  const applyEvent = useCallback(
    (ev) => {
      const type = String(ev?.type || "");
      const data = ev?.data || {};

      if (type === "chat.thinking_start") hold("thinking", true);
      else if (type === "chat.thinking_end" || type === "chat.assistant_done") hold("thinking", false);
      else if (type === "tool.started") hold("tool", String(data.tool || "tool"));
      else if (type === "tool.result") flash("tool", String(data.tool || "tool"));
      else if (type === "tool.error") {
        flash("tool", String(data.tool || "tool"));
        flash("lastError", `${data.tool || "tool"}: ${data.error || "failed"}`);
      } else if (type === "memory.write") flash("memory", "write");
      else if (type === "memory.search" || type === "memory.read") flash("memory", "read");
      else if (type === "vision.analysis_start") hold("vision", true);
      else if (type === "vision.analysis_done" || type === "vision.error") hold("vision", false);
      else if (type === "web.search_start" || type === "web.fetch_start") hold("web", true);
      else if (type.startsWith("web.") ) {
        if (type.endsWith("_done") || type.endsWith("_error")) hold("web", false);
      } else if (type === "tts.generate_start") hold("ttsGenerating", true);
      else if (type === "tts.generate_done") hold("ttsGenerating", false);
      else if (type === "project.started") hold("tool", `project:${data.project || "build"}`);
      else if (type === "project.progress") hold("tool", `project:${data.project || "build"} (${data.stage || "working"})`);
      else if (type === "project.completed" || type === "project.error") flash("tool", `project:${data.project || "build"}`);
      else if (type === "model.error" || type === "system.error") flash("lastError", String(data.error || type));
    },
    [flash, hold]
  );

  useEffect(() => {
    if (!enabled) return undefined;
    closedRef.current = false;
    let retryMs = 1000;
    let retryTimer = null;

    const connect = () => {
      if (closedRef.current) return;
      let ws;
      try {
        ws = new WebSocket(wsUrl());
      } catch {
        retryTimer = window.setTimeout(connect, retryMs);
        return;
      }
      wsRef.current = ws;

      ws.onopen = () => {
        retryMs = 1000;
        setConnected(true);
      };

      ws.onmessage = (msg) => {
        try {
          const ev = JSON.parse(msg.data);
          setEvents((prev) => {
            const next = [...prev, ev];
            return next.length > MAX_EVENTS ? next.slice(next.length - MAX_EVENTS) : next;
          });
          applyEvent(ev);
        } catch {}
      };

      ws.onclose = () => {
        setConnected(false);
        wsRef.current = null;
        if (!closedRef.current) {
          retryMs = Math.min(retryMs * 1.6, 10_000);
          retryTimer = window.setTimeout(connect, retryMs);
        }
      };

      ws.onerror = () => {
        try {
          ws.close();
        } catch {}
      };
    };

    connect();

    return () => {
      closedRef.current = true;
      try {
        if (retryTimer) window.clearTimeout(retryTimer);
      } catch {}
      try {
        wsRef.current?.close();
      } catch {}
      const timers = timersRef.current;
      Object.values(timers).forEach((t) => {
        try {
          window.clearTimeout(t);
        } catch {}
      });
    };
  }, [enabled, applyEvent]);

  return { connected, events, activity };
}
