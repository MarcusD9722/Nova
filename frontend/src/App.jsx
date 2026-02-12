import React, { useEffect, useMemo, useRef, useState, lazy, Suspense } from "react";

import TopBar from "./components/TopBar";
import BottomDock from "./components/BottomDock";
import OverlayHost from "./components/OverlayHost";

import useMicLevel from "./hooks/useMicLevel";
import useCamera from "./hooks/useCamera";
import useHandTracking from "./hooks/useHandTracking";

import { useWakeNova } from "./voice/useWakeNova";
import { acquireMicStreamHandle, playAudioUrl, recordFromStreamToBlob, recordOnceToBlob, transcribeBlob } from "./voice/recorder";

import SettingsSheet from "./overlays/SettingsSheet";
import CameraSheet from "./overlays/CameraSheet";
import GesturesSheet from "./overlays/GesturesSheet";
import SmartHomeSheet from "./overlays/SmartHomeSheet";
import PrinterSheet from "./overlays/PrinterSheet";
import WebSheet from "./overlays/WebSheet";

const AnimatedBackground = lazy(() => import("./components/AnimatedBackground"));
const NovaOrb3D = lazy(() => import("./components/NovaOrb3D"));
const ChatPanel = lazy(() => import("./components/ChatPanel"));

function apiBase() {
  // Dev (Vite) proxy: use relative
  try {
    if (import.meta?.env?.DEV) return "";
  } catch {}
  // Electron prod: window.location.origin becomes "null"
  try {
    const fromEnv = import.meta?.env?.VITE_API_BASE ? String(import.meta.env.VITE_API_BASE) : "";
    if (fromEnv) return fromEnv.replace(/\/$/, "");
  } catch {}
  try {
    const fromWindow = window.__NOVA_API_BASE ? String(window.__NOVA_API_BASE) : "";
    if (fromWindow) return fromWindow.replace(/\/$/, "");
  } catch {}
  return "http://localhost:8008";
}
function apiUrl(path) {
  const b = apiBase();
  if (!b) return path; // dev proxy
  return `${b}${path}`;
}

export default function App() {
  // ===== Chat state =====
  const [messages, setMessages] = useState([]);
  const [conversationId, setConversationId] = useState(null);

  // Persist conversation ID across app restarts
  const CONV_STORAGE_KEY = "nova.conversation_id";
  useEffect(() => {
    try {
      const saved = window.localStorage.getItem(CONV_STORAGE_KEY);
      if (saved) setConversationId(saved);
    } catch {}
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    try {
      if (conversationId) window.localStorage.setItem(CONV_STORAGE_KEY, String(conversationId));
    } catch {}
  }, [conversationId]);
  const [thinking, setThinking] = useState(false);

  // ===== Layout state =====
  const [activeOverlay, setActiveOverlay] = useState(null); // "settings"|"camera"|"gestures"|"smarthome"|"printer"|"web"|null

  // ===== Subsystem state =====
  const [micMuted, setMicMuted] = useState(true);
  const [gesturesOn, setGesturesOn] = useState(false);

  const camera = useCamera();
  const micLevel = useMicLevel({ enabled: !micMuted, muted: micMuted });

  const hand = useHandTracking({ enabled: gesturesOn && camera.enabled, stream: camera.stream });

  // ===== Voice pipeline state =====
  const [voiceStatus, setVoiceStatus] = useState("idle"); // idle | wake | listening | transcribing | speaking | error
  const capturingRef = useRef(false);

  // Strict voice state machine (for wake reliability + debuggability)
  const [voicePhase, setVoicePhase] = useState("IDLE_LISTENING"); // IDLE_LISTENING | ARMED | CAPTURING_COMMAND | RESPONDING
  const phaseRef = useRef("IDLE_LISTENING");
  const wakeResumeAtRef = useRef(0);
  const transcribeDoneAtRef = useRef(0);
  const resumeTimerRef = useRef(null);

  const setPhase = (next, meta = {}) => {
    const prev = phaseRef.current;
    if (prev === next) return;
    phaseRef.current = next;
    setVoicePhase(next);
    // eslint-disable-next-line no-console
    console.log(`[voice:phase] ${prev} -> ${next}`, meta);
  };

  const scheduleWakeResumeCheck = () => {
    try {
      if (resumeTimerRef.current) window.clearTimeout(resumeTimerRef.current);
    } catch {}
    resumeTimerRef.current = window.setTimeout(() => {
      if (!micUnmutedRef.current) return;
      if (capturingRef.current) return;
      const now = Date.now();
      if (wakeResumeAtRef.current && now < wakeResumeAtRef.current) {
        scheduleWakeResumeCheck();
        return;
      }
      // Only resume when we're truly idle.
      if (phaseRef.current !== "IDLE_LISTENING") return;
      try { startWake?.(); } catch {}
    }, 150);
  };

  const voiceDebug = useMemo(() => {
    try {
      if (window.__NOVA_VOICE_DEBUG) return true;
    } catch {}
    try {
      return window.localStorage?.getItem("novaVoiceDebug") === "1";
    } catch {}
    return false;
  }, []);

  // Keep mic open after user grants permission (while unmuted)
  const micKeepaliveRef = useRef(null);
  const micUnmutedRef = useRef(false);
  useEffect(() => {
    micUnmutedRef.current = !micMuted;
  }, [micMuted]);

  // Keep a handle to the in-flight stream so we can Stop/Abort
  const streamCtlRef = useRef(null);

  const addSystem = (text) => {
    const id = `sys-${Date.now()}`;
    setMessages((prev) => [...prev, { id, sender: "system", text }]);
  };

  // Ensure window.__NOVA_API_BASE exists for Electron prod
  useEffect(() => {
    try {
      if (!window.__NOVA_API_BASE) window.__NOVA_API_BASE = "http://localhost:8008";
    } catch {}
  }, []);

  // ===== Chat streaming helpers =====
  const setReply = (text, isError = false) => {
    setMessages((prev) => {
      const next = [...prev];
      const last = next[next.length - 1];
      if (last && last.sender === "nova" && last.streaming) {
        next[next.length - 1] = { ...last, text, error: isError ? "Error" : undefined };
      } else {
        next.push({ id: `nova-${Date.now()}`, sender: "nova", text, streaming: true });
      }
      return next;
    });
  };
  const updateReply = (append) => {
    setMessages((prev) => {
      const next = [...prev];
      const last = next[next.length - 1];
      if (last && last.sender === "nova" && last.streaming) {
        next[next.length - 1] = { ...last, text: (last.text || "") + append };
      } else {
        next.push({ id: `nova-${Date.now()}`, sender: "nova", text: append, streaming: true });
      }
      return next;
    });
  };
  const finalizeReply = () => {
    setMessages((prev) => {
      const next = [...prev];
      const last = next[next.length - 1];
      if (last && last.sender === "nova" && last.streaming) {
        next[next.length - 1] = { ...last, streaming: false };
      }
      return next;
    });
  };

  async function nonStreamingFallback(text) {
    const resp = await fetch(apiUrl("/chat"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text, ...(conversationId ? { conversation_id: conversationId } : {}) }),
    });
    if (!resp.ok) throw new Error(await resp.text());
    const data = await resp.json();
    const reply = data?.assistant ?? data?.response ?? data?.text ?? "";
    setReply(reply);
    finalizeReply();
    return reply;
  }

  // Stop button from ChatPanel
  const stopStream = () => {
    try {
      streamCtlRef.current?.abort?.();
    } catch {}
    streamCtlRef.current = null;
    setThinking(false);
    finalizeReply();
    addSystem("Stopped.");
  };

  // Main sendMessage for ChatPanel
  const sendMessage = async (text) => {
    const userId = `user-${Date.now()}`;
    const replyId = `nova-${Date.now()}`;
    setMessages((prev) => [...prev, { id: userId, sender: "user", text }]);
    setMessages((prev) => [...prev, { id: replyId, sender: "nova", text: "", streaming: true }]);
    setThinking(true);

    let assistantText = "";
    let currentEvent = "message";

    // Abort any previous stream before starting a new one
    if (streamCtlRef.current) {
      try { streamCtlRef.current.abort(); } catch {}
    }
    const ctl = new AbortController();
    streamCtlRef.current = ctl;

    try {
      const resp = await fetch(apiUrl("/chat/stream"), {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Accept": "text/event-stream, text/plain",
        },
        body: JSON.stringify({
          system_prompt: "You are Nova.",
          msg: text,
          hint: "",
          speak: true,
          ...(conversationId ? { conversation_id: conversationId } : {}),
        }),
        signal: ctl.signal,
      });

      if (resp.ok && resp.body) {
        const reader = resp.body.getReader();
        const decoder = new TextDecoder("utf-8");
        let buffer = "";
        let gotAny = false;

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          gotAny = true;
          buffer += decoder.decode(value, { stream: true });

          const lines = buffer.split(/\r?\n/);
          buffer = lines.pop() ?? "";

          for (const line of lines) {
            if (!line) continue;
            if (line.startsWith("event:")) {
              currentEvent = line.slice(6).trim() || "message";
              continue;
            }

            if (line.startsWith("data:")) {
              const payload = line.slice(5).trim();
              if (!payload) continue;
              try {
                const piece = JSON.parse(payload);
                if (currentEvent === "meta") {
                  const cid = piece?.conversation_id;
                  if (cid) setConversationId(String(cid));
                  continue;
                }
                if (currentEvent === "tts") {
                  const aurl = piece?.audio_url;
                  if (aurl) {
                    // Allow wake to resume as soon as TTS starts (if we were waiting).
                    const now = Date.now();
                    if (transcribeDoneAtRef.current && now >= transcribeDoneAtRef.current) {
                      wakeResumeAtRef.current = Math.min(wakeResumeAtRef.current || now, now);
                      scheduleWakeResumeCheck();
                    }
                    try {
                      await playAudioUrl(String(aurl), { debugTag: "tts" });
                    } catch {}
                  }
                  continue;
                }
                if (currentEvent === "tts_error") {
                  if (voiceDebug) addSystem(`TTS error: ${piece?.error || "unknown"}`);
                  continue;
                }

                const token = piece?.content ?? "";
                if (token) {
                  assistantText += token;
                  updateReply(token);
                }
              } catch {
                // Treat as plain text only for normal message events

                if (currentEvent === "message") {
                  assistantText += payload;
                  updateReply(payload);
                }
              }
            } else {
              if (currentEvent === "message") {
                assistantText += line;
                updateReply(line);
              }
            }
          }
        }

        if (buffer) {
          try {
            const piece = JSON.parse(buffer);
            if (currentEvent === "meta") {
              const cid = piece?.conversation_id;
              if (cid) setConversationId(String(cid));
            }
            if (currentEvent === "tts") {
              const aurl = piece?.audio_url;
              if (aurl) {
                try {
                  await playAudioUrl(String(aurl), { debugTag: "tts" });
                } catch {}
              }
            } else if (currentEvent === "message") {
              const token = piece?.content ?? "";
              if (token) {
                assistantText += token;
                updateReply(token);
              }
            }
          } catch {
            if (currentEvent === "message") {
              assistantText += buffer;
              updateReply(buffer);
            }
          }
        }

        finalizeReply();

        return assistantText;
      } else {
        const reply = await nonStreamingFallback(text);
        return reply;
      }
    } catch (err) {
      if (ctl.signal.aborted) return;
      console.error("stream error:", err);
      try {
        const reply = await nonStreamingFallback(text);
        return reply;
      } catch {
        setReply("Sorry — I hit a connection error.", true);
        finalizeReply();
      }
    } finally {
      setThinking(false);
      if (streamCtlRef.current === ctl) streamCtlRef.current = null;
    }
  };

  // ===== Wake word -> record -> transcribe -> send -> speak =====
  const captureAndSend = async () => {
    if (capturingRef.current) return;
    capturingRef.current = true;

    try {
      setPhase("CAPTURING_COMMAND");
      setVoiceStatus("listening");
      addSystem("Listening…");

      // Prefer the keepalive stream so we don't create competing MediaRecorder sessions.
      let blob;
      const keep = micKeepaliveRef.current;
      if (keep?.stream) {
        blob = await recordFromStreamToBlob(keep.stream, { maxMs: 8000, timesliceMs: 250, debugTag: "capture" });
      } else {
        blob = await recordOnceToBlob({ seconds: 8, debugTag: "capture" });
      }
      setVoiceStatus("transcribing");
      addSystem("Transcribing…");

      // Transcription finished gate for wake resumption.
      transcribeDoneAtRef.current = 0;

      const text = await transcribeBlob(blob, apiUrl("/stt"));
      transcribeDoneAtRef.current = Date.now();

      if (!text?.trim()) {
        addSystem("No speech detected.");
        setVoiceStatus("idle");
        setPhase("IDLE_LISTENING", { reason: "empty_transcript" });
        return;
      }

      setPhase("RESPONDING");
      setVoiceStatus("speaking");
      addSystem(`You said: ${text}`);

      // Do not restart wake until transcription is done AND we either:
      // - wait a short cooldown (default), or
      // - TTS playback begins (handled in sendMessage SSE tts event).
      const cooldownMs = 4000;
      wakeResumeAtRef.current = Date.now() + cooldownMs;

      // Send message and then speak last assistant reply
      await sendMessage(text);
      setVoiceStatus("idle");
      setPhase("IDLE_LISTENING", { reason: "response_complete" });
    } catch (e) {
      console.warn(e);
      setVoiceStatus("error");
      addSystem("Voice error.");
      setTimeout(() => setVoiceStatus("idle"), 1200);
    } finally {
      capturingRef.current = false;

      // Resume wake only when allowed by the state machine + cooldown.
      if (micKeepaliveRef.current && micUnmutedRef.current) {
        setPhase("IDLE_LISTENING", { reason: "capture_finally" });
        scheduleWakeResumeCheck();
      }
    }
  };

  const { startWake, stopWake } = useWakeNova(() => {
    // IDLE_LISTENING -> ARMED
    setPhase("ARMED");
    setVoiceStatus("wake");
    if (voiceDebug) addSystem("Wake word detected.");
    // Stop wake while we record the command (prevents concurrent MediaRecorder usage).
    try { stopWake?.(); } catch {}
    // Ensure wake won't restart until we explicitly allow it.
    wakeResumeAtRef.current = Date.now() + 60_000;
    // brief delay for UX then capture
    setTimeout(() => captureAndSend(), 120);
  }, "hey nova");

  // Enable wake listening when not muted
  useEffect(() => {
    if (micMuted) {
      try { stopWake?.(); } catch {}
      return;
    }
    // Enter idle listening when unmuted.
    setPhase("IDLE_LISTENING", { reason: "mic_unmuted" });
    wakeResumeAtRef.current = Date.now();
    scheduleWakeResumeCheck();
  }, [micMuted, startWake, stopWake]);

  // gestures status
  const gesturesStatus = useMemo(() => {
    if (!gesturesOn) return "off";
    if (!camera.enabled) return "needs camera";
    if (hand.status === "loading") return "starting";
    if (hand.status === "error") return "error";
    return "ready";
  }, [gesturesOn, camera.enabled, hand.status]);

  // Pinch-to-click
  useEffect(() => {
    if (!gesturesOn || !camera.enabled) return;
    if (hand?.status !== "ready") return;
    if (!hand?.cursor?.visible) return;
    if (!hand?.pinch?.justPressed) return;

    const x = Math.round((hand.cursor.x ?? 0) * window.innerWidth);
    const y = Math.round((hand.cursor.y ?? 0) * window.innerHeight);
    const el = document.elementFromPoint(x, y);
    if (!el) return;

    try {
      el.dispatchEvent(
        new MouseEvent("click", {
          bubbles: true,
          cancelable: true,
          clientX: x,
          clientY: y,
          view: window,
        })
      );
    } catch {}
  }, [gesturesOn, camera.enabled, hand?.status, hand?.cursor?.visible, hand?.cursor?.x, hand?.cursor?.y, hand?.pinch?.justPressed]);

  // Dock actions
  const onToggleMic = async () => {
    // User gesture entrypoint: request permission here.
    if (micMuted) {
      try {
        // Acquire and keep the stream open while unmuted.
        if (!micKeepaliveRef.current) {
          micKeepaliveRef.current = await acquireMicStreamHandle({ debugTag: "toggle" });
        }
        setMicMuted(false);
        addSystem("Mic unmuted.");
        // Start wake from the same user gesture, but still honor state machine.
        setPhase("IDLE_LISTENING", { reason: "toggle_unmute" });
        wakeResumeAtRef.current = Date.now();
        scheduleWakeResumeCheck();
      } catch (e) {
        console.warn(e);
        addSystem("Mic permission denied or unavailable.");
        setMicMuted(true);
        try {
          micKeepaliveRef.current?.release?.();
        } catch {}
        micKeepaliveRef.current = null;
      }
      return;
    }

    // Muting
    setMicMuted(true);
    addSystem("Mic muted.");
    try { stopWake?.(); } catch {}
    setPhase("IDLE_LISTENING", { reason: "toggle_mute" });
    try { micKeepaliveRef.current?.release?.(); } catch {}
    micKeepaliveRef.current = null;
  };
  const onToggleCamera = async () => {
    try {
      if (!camera.enabled) {
        await camera.start();
        addSystem("Camera on.");
      } else {
        await camera.stop();
        addSystem("Camera off.");
      }
    } catch (e) {
      console.warn(e);
      addSystem("Camera error.");
    }
  };
  const onToggleGestures = () => {
    setGesturesOn((v) => {
      const next = !v;
      if (next && !camera.enabled) addSystem("Gestures enabled (camera required).");
      else addSystem(next ? "Gestures enabled." : "Gestures disabled.");
      return next;
    });
  };
  const onOpenOverlay = (key) => {
    setActiveOverlay((cur) => (cur === key ? null : key));
  };

  // Time text for top bar
  const [timeText, setTimeText] = useState("");
  useEffect(() => {
    const tick = () =>
      setTimeText(
        new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
      );
    tick();
    const id = setInterval(tick, 15_000);
    return () => clearInterval(id);
  }, []);

  // Orb state mapping
  const orbState = useMemo(() => {
    if (voiceStatus === "wake" || voiceStatus === "listening" || voiceStatus === "transcribing") return "listening";
    if (voiceStatus === "speaking") return "speaking";
    if (thinking) return "thinking";
    return "idle";
  }, [voiceStatus, thinking]);

  return (
    <div className="relative w-screen h-screen overflow-hidden text-zinc-100">
      {/* Background */}
      <div className="absolute inset-0 -z-10 pointer-events-none">
        <Suspense fallback={null}>
          <AnimatedBackground />
        </Suspense>
      </div>

      {/* Top bar */}
      <TopBar
        version="v2"
        project="PROJECT: TEMP"
        micMuted={micMuted}
        micLevel={micLevel}
        timeText={timeText}
      />

      {/* Center stage */}
      <div className="relative z-10 w-full h-full pt-16 pb-24 flex justify-center">
        <div className="w-[min(980px,94vw)] flex flex-col items-center gap-4">
          {/* Home card */}
          <div className="w-full max-w-[620px] rounded-3xl border border-cyan-500/25 bg-black/25 backdrop-blur-2xl shadow-[0_16px_50px_rgba(0,0,0,0.35)] px-4 py-3">
            <div className="flex items-center justify-between text-xs text-white/60">
              <div>Status: {micMuted ? "Muted" : "Listening"} • {camera.enabled ? "Cam On" : "Cam Off"} • Gestures: {gesturesOn ? "On" : "Off"}</div>
              <div className="text-white/50">Voice: {voiceStatus} • {voicePhase}</div>
            </div>

            <div className="mt-2 grid place-items-center">
              <div style={{ width: 420, height: 420 }} className="max-w-full">
                <Suspense fallback={<div className="w-full h-full grid place-items-center text-white/60 text-sm">Loading orb…</div>}>
                  <NovaOrb3D bloom={false} showText state={orbState} size={420} />
                </Suspense>
              </div>
            </div>
          </div>

          {/* Chat card (always visible) */}
          <div className="w-full flex-1 min-h-0">
            <div className="h-[min(520px,52vh)]">
              <Suspense fallback={<div className="w-full h-full grid place-items-center text-white/60 text-sm">Loading chat…</div>}>
                <ChatPanel
                  messages={messages}
                  onSendMessage={sendMessage}
                  onStop={stopStream}
                  isAssistantThinking={thinking}
                />
              </Suspense>
            </div>
          </div>
        </div>
      </div>

      {/* Bottom dock */}
      <BottomDock
        micMuted={micMuted}
        cameraOn={camera.enabled}
        gesturesOn={gesturesOn}
        activeOverlay={activeOverlay}
        onToggleMic={onToggleMic}
        onToggleCamera={onToggleCamera}
        onToggleGestures={onToggleGestures}
        onOpenOverlay={onOpenOverlay}
      />

      {/* Gesture cursor overlay */}
      {gesturesOn && camera.enabled && hand?.cursor?.visible && (
        <div className="fixed inset-0 z-[60] pointer-events-none">
          <div
            className={[
              "absolute -translate-x-1/2 -translate-y-1/2",
              "w-4 h-4 rounded-full",
              "border border-white/70",
              hand?.pinch?.down ? "bg-white/40" : "bg-white/10",
            ].join(" ")}
            style={{
              left: `${(hand.cursor.x ?? 0) * 100}%`,
              top: `${(hand.cursor.y ?? 0) * 100}%`,
            }}
          />
        </div>
      )}

      {/* Bottom-sheet overlays */}
      <OverlayHost
        open={activeOverlay === "settings"}
        title="Settings"
        onClose={() => setActiveOverlay(null)}
      >
        <SettingsSheet />
      </OverlayHost>

      <OverlayHost
        open={activeOverlay === "camera"}
        title={`Camera • ${camera.status}`}
        onClose={() => setActiveOverlay(null)}
      >
        <CameraSheet stream={camera.stream} status={camera.status} />
      </OverlayHost>

      <OverlayHost
        open={activeOverlay === "gestures"}
        title="Gestures"
        onClose={() => setActiveOverlay(null)}
      >
        <GesturesSheet enabled={gesturesOn} status={gesturesStatus} tracker={hand} />
      </OverlayHost>

      <OverlayHost
        open={activeOverlay === "smarthome"}
        title="Smart Home"
        onClose={() => setActiveOverlay(null)}
      >
        <SmartHomeSheet />
      </OverlayHost>

      <OverlayHost
        open={activeOverlay === "printer"}
        title="3D Printer"
        onClose={() => setActiveOverlay(null)}
      >
        <PrinterSheet />
      </OverlayHost>

      <OverlayHost
        open={activeOverlay === "web"}
        title="Web Search"
        onClose={() => setActiveOverlay(null)}
      >
        <WebSheet />
      </OverlayHost>
    </div>
  );
}
