import React, { useEffect, useMemo, useRef, useState, lazy, Suspense } from "react";

import TopBar from "./components/TopBar";
import BottomDock from "./components/BottomDock";
import OverlayHost from "./components/OverlayHost";
import LeftSidebar from "./components/LeftSidebar";
import Waveform from "./components/Waveform";
import HologramStage from "./components/HologramStage";

import useMicLevel from "./hooks/useMicLevel";
import useCamera from "./hooks/useCamera";
import useHandTracking from "./hooks/useHandTracking";
import useNovaEvents from "./hooks/useNovaEvents";
import useFocusSession from "./hooks/useFocusSession";
import useNovaBusEffects from "./hooks/useNovaBusEffects";

import { useWakeNova } from "./voice/useWakeNova";
import { acquireMicStreamHandle, duckPlayback, playAudioUrl, prefetchAudioBlob, recordVoiceActivityFromStreamToBlob, recordVoiceActivityToBlob, restorePlayback, stopActiveAudio, transcribeBlob, transcribeBlobDetailed, unlockAudioContext, voiceTurnIdOf } from "./voice/recorder";
import { buildFallbackBody, buildStreamBody, unverifiedVoiceOrigin, voiceOrigin } from "./voice/turnOrigin";
import { CouplingEstimator, newAttempt, summarize, watchForSpeechOverPlayback } from "./voice/bargeIn";

import SettingsSheet from "./overlays/SettingsSheet";
import CameraSheet from "./overlays/CameraSheet";
import MapsSheet from "./overlays/SmartHomeSheet";
import WebSheet from "./overlays/WebSheet";
import MemorySheet from "./overlays/MemorySheet";
import TasksSheet from "./overlays/TasksSheet";
import SystemSheet from "./overlays/SystemSheet";
import ImprovementsSheet from "./overlays/ImprovementsSheet";
import ScreenVisionSheet from "./overlays/ScreenVisionSheet";
import GraphSheet from "./overlays/GraphSheet";

const AnimatedBackground = lazy(() => import("./components/AnimatedBackground"));
const NovaHologramAvatar = lazy(() => import("./components/NovaHologramAvatar"));
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

const HAND_CALIBRATION_KEY = "nova.handCalibration.v1";
const LOCATION_REQUIRED_MESSAGE_RE = /\b(?:from\s+here|from\s+my\s+location|near\s+me|around\s+me|nearest\b|closest\b|directions?\s+to\b|how\s+do\s+i\s+get\s+to\b|how\s+long\s+will\s+it\s+take(?:\s+to\s+get)?\s+to\b|(?:get|go|drive|walk|navigate|head)\s+to\b)\s/i;
const HAND_CALIBRATION_STEPS = [
  {
    key: "center",
    title: "Center",
    detail: "Hold your index finger where you want the cursor to feel centered, then stay still and capture.",
  },
  {
    key: "left",
    title: "Left Edge",
    detail: "Reach to the furthest comfortable left position you want to use for cursor control.",
  },
  {
    key: "right",
    title: "Right Edge",
    detail: "Reach to the furthest comfortable right position you want to use for cursor control.",
  },
  {
    key: "top",
    title: "Top Edge",
    detail: "Lift your hand to the highest comfortable cursor position.",
  },
  {
    key: "bottom",
    title: "Bottom Edge",
    detail: "Lower your hand to the lowest comfortable cursor position.",
  },
  {
    key: "open",
    title: "Open Pinch",
    detail: "Relax your thumb and index finger fully open, then capture that resting gap.",
  },
  {
    key: "closed",
    title: "Closed Pinch",
    detail: "Pinch firmly the way you want clicks and drags to engage, then capture.",
  },
];

function loadSavedHandCalibration() {
  try {
    const raw = window.localStorage.getItem(HAND_CALIBRATION_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object" ? parsed : null;
  } catch {
    return null;
  }
}

function averageCalibrationSamples(samples) {
  const list = Array.isArray(samples) ? samples.filter(Boolean) : [];
  if (!list.length) return null;

  const sum = list.reduce(
    (acc, item) => ({
      rawX: acc.rawX + Number(item.rawX || 0),
      rawY: acc.rawY + Number(item.rawY || 0),
      pinchRatio: acc.pinchRatio + Number(item.pinchRatio || 0),
    }),
    { rawX: 0, rawY: 0, pinchRatio: 0 }
  );

  return {
    rawX: sum.rawX / list.length,
    rawY: sum.rawY / list.length,
    pinchRatio: sum.pinchRatio / list.length,
    sampleCount: list.length,
  };
}

function buildHandCalibration(samples) {
  const center = samples?.center;
  const left = samples?.left;
  const right = samples?.right;
  const top = samples?.top;
  const bottom = samples?.bottom;
  const open = samples?.open;
  const closed = samples?.closed;

  if (!center || !left || !right || !top || !bottom || !open || !closed) {
    return null;
  }

  const mirrorX = left.rawX > right.rawX;
  const leftSampleX = mirrorX ? right.rawX : left.rawX;
  const rightSampleX = mirrorX ? left.rawX : right.rawX;

  const leftBound = Math.max(0, Math.min(leftSampleX, center.rawX - 0.02));
  const rightBound = Math.min(1, Math.max(rightSampleX, center.rawX + 0.02));
  const topBound = Math.max(0, Math.min(top.rawY, center.rawY - 0.02));
  const bottomBound = Math.min(1, Math.max(bottom.rawY, center.rawY + 0.02));

  const xRange = Math.max(0.08, rightBound - leftBound);
  const yRange = Math.max(0.08, bottomBound - topBound);
  const xPad = Math.min(0.06, xRange * 0.08);
  const yPad = Math.min(0.06, yRange * 0.08);

  const openRatio = Math.max(open.pinchRatio, closed.pinchRatio + 0.08);
  const closedRatio = Math.min(closed.pinchRatio, openRatio - 0.08);
  const pinchSpan = Math.max(0.08, openRatio - closedRatio);

  return {
    version: 1,
    createdAt: new Date().toISOString(),
    mirrorX,
    cursorBounds: {
      left: Math.max(0, leftBound - xPad),
      right: Math.min(1, rightBound + xPad),
      top: Math.max(0, topBound - yPad),
      bottom: Math.min(1, bottomBound + yPad),
      centerX: center.rawX,
      centerY: center.rawY,
    },
    pinch: {
      open: openRatio,
      closed: closedRatio,
      pressThreshold: closedRatio + pinchSpan * 0.34,
      releaseThreshold: closedRatio + pinchSpan * 0.62,
    },
    samples,
  };
}

export default function App() {
  // ===== Chat state =====
  const [messages, setMessages] = useState([]);
  const [conversationId, setConversationId] = useState(null);
  // Mirrored into a ref: the voice session loop is a single long-lived async
  // function, so reading the state variable there would pin whatever value it
  // had when the loop started.
  const conversationIdRef = useRef(null);
  useEffect(() => { conversationIdRef.current = conversationId; }, [conversationId]);

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
  // U8: current project-build stage ("writing: main.py"), or "" when idle.
  const [buildProgress, setBuildProgress] = useState("");
  // The status poller below runs on an interval with empty deps, so it would
  // capture buildProgress at mount. Mirror it into a ref (same pattern as
  // thinkingRef) so the interval always reads the current stage.
  const buildProgressRef = useRef("");
  useEffect(() => {
    buildProgressRef.current = buildProgress;
  }, [buildProgress]);
  const thinkingRef = useRef(false);
  useEffect(() => {
    thinkingRef.current = thinking;
  }, [thinking]);

  // ===== Layout state =====
  const [chatOpen, setChatOpen] = useState(true);
  const [activeSection, setActiveSection] = useState("home");
  const [activeOverlay, setActiveOverlay] = useState(null); // "settings"|"camera"|"maps"|"web"|null
  const [mapsRoutePreload, setMapsRoutePreload] = useState(null);
  const [currentLocation, setCurrentLocation] = useState(null);
  const [locationStatus, setLocationStatus] = useState("idle"); // idle | locating | ready | denied | unavailable | manual
  const [locationNote, setLocationNote] = useState("");

  // ===== Subsystem state =====
  const [micMuted, setMicMuted] = useState(true);
  const [gesturesOn, setGesturesOn] = useState(false);
  // PI1: opt-in periodic screen glances — off by default; toggling this ON
  // IS the explicit action that permits periodic capture (see useFocusSession).
  const [focusSessionOn, setFocusSessionOn] = useState(false);
  // PI1: pending agent-requested screen look, waiting on the user's confirm click.
  const [screenLookRequest, setScreenLookRequest] = useState(null); // {requestId, question} | null
  const [desktopControlAvailable, setDesktopControlAvailable] = useState(false);
  const [desktopControlEnabled, setDesktopControlEnabled] = useState(false);
  const [handCalibration, setHandCalibration] = useState(() => loadSavedHandCalibration());
  const [calibrationSession, setCalibrationSession] = useState(null);
  const [micRequesting, setMicRequesting] = useState(false);

  const [micStream, setMicStream] = useState(null);

  const [ttsPlaying, setTtsPlaying] = useState(false);
  const ttsPlayingRef = useRef(false);
  const [wakePulse, setWakePulse] = useState(false);
  const wakePulseTimerRef = useRef(null);

  const [voiceSessionActive, setVoiceSessionActive] = useState(false);
  const voiceSessionRef = useRef(false);
  const sessionCtlRef = useRef({ token: 0 });
  useEffect(() => {
    voiceSessionRef.current = voiceSessionActive;
  }, [voiceSessionActive]);

  const setVoiceSession = (active) => {
    voiceSessionRef.current = !!active;
    setVoiceSessionActive(!!active);
  };

  const camera = useCamera();
  const micLevel = useMicLevel({ enabled: !micMuted, muted: micMuted, stream: micStream });

  // Live backend event stream (thinking / tools / memory / vision / web states)
  const { connected: eventsConnected, events: novaEvents, activity: novaActivity } = useNovaEvents();

  const handTrackingEnabled = camera.enabled && (gesturesOn || Boolean(calibrationSession?.active));
  const hand = useHandTracking({ enabled: handTrackingEnabled, stream: camera.stream, calibration: handCalibration });
  const cameraOverlayAutoGesturesRef = useRef(false);
  const desktopMoveAtRef = useRef(0);
  const desktopMoveInFlightRef = useRef(false);
  const domPressRef = useRef({ active: false, target: null, startX: 0, startY: 0, moved: false, pressedAt: 0 });
  const latestHandSampleRef = useRef({ status: "off", rawCursor: { x: 0, y: 0, visible: false }, pinchRatio: 1 });

  // ===== Voice pipeline state =====
  const [voiceStatus, setVoiceStatus] = useState("idle"); // idle | wake | listening | transcribing | speaking | error
  const capturingRef = useRef(false);

  // Strict voice state machine (for wake reliability + debuggability)
  const [voicePhase, setVoicePhase] = useState("IDLE_LISTENING"); // IDLE_LISTENING | ARMED | CAPTURING_COMMAND | RESPONDING
  const phaseRef = useRef("IDLE_LISTENING");
  const wakeResumeAtRef = useRef(0);
  const transcribeDoneAtRef = useRef(0);
  const resumeTimerRef = useRef(null);

  const requestCurrentLocation = async ({ silent = false } = {}) => {
    if (!navigator?.geolocation) {
      setLocationStatus("unavailable");
      setLocationNote("Location services are not available in this build.");
      return null;
    }

    setLocationStatus("locating");
    if (!silent) setLocationNote("Requesting your current location...");

    return await new Promise((resolve) => {
      navigator.geolocation.getCurrentPosition(
        (position) => {
          const next = {
            lat: Number(position.coords.latitude),
            lng: Number(position.coords.longitude),
            accuracy_m: Number(position.coords.accuracy || 0),
          };
          setCurrentLocation(next);
          setLocationStatus("ready");
          setLocationNote(
            next.accuracy_m ? `Using your current location (accuracy about ${Math.round(next.accuracy_m)} meters).` : "Using your current location."
          );
          resolve(next);
        },
        (error) => {
          setCurrentLocation(null);
          if (error?.code === 1) {
            setLocationStatus("denied");
            setLocationNote("Location permission was denied. You can type an address manually below.");
          } else {
            setLocationStatus("unavailable");
            setLocationNote("Could not get your current location. You can type an address manually below.");
          }
          resolve(null);
        },
        { enableHighAccuracy: true, timeout: 12000, maximumAge: 300000 }
      );
    });
  };

  useEffect(() => {
    requestCurrentLocation({ silent: true });
  }, []);

  const setManualLocation = (nextLocation, label = "") => {
    setCurrentLocation(nextLocation);
    setLocationStatus("manual");
    setLocationNote(label ? `Using typed location: ${label}` : "Using typed location.");
  };

  const messageNeedsCurrentLocation = (text) => LOCATION_REQUIRED_MESSAGE_RE.test(String(text || "").trim());

  const locationForMessage = async (text) => {
    if (currentLocation || !messageNeedsCurrentLocation(text)) return currentLocation;
    return await requestCurrentLocation({ silent: false });
  };

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
      if (voiceSessionRef.current) return;
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

  const showSystemMessage = (text) => {
    try {
      if (window.__NOVA_SHOW_SYSTEM_CHAT === true) {
        addSystem(text);
      }
    } catch {}
  };

  const normalize = (s) =>
    String(s || "")
      .toLowerCase()
      .replace(/[\u2019']/g, "'")
      .replace(/[^a-z0-9\s']/g, " ")
      .replace(/\s+/g, " ")
      .trim();

  const isSessionStopPhrase = (raw) => {
    const t = normalize(raw);
    if (!t) return false;
    // Any mention of these words ends the engaged session.
    return /\b(standby|disengage)\b/.test(t);
  };

  // Barge-in: short "stop"-style phrases silence Nova without ending the session.
  const isInterruptPhrase = (raw) => {
    const t = normalize(raw);
    if (!t || t.split(" ").length > 4) return false;
    return /\b(stop( talking)?|be quiet|quiet|shush|shut up|silence|enough)\b/.test(t);
  };

  const resolveDomTarget = (x, y) => document.elementFromPoint(x, y) || document.body;

  const pointerTypeForMouse = (type) => {
    if (type === "mousedown") return "pointerdown";
    if (type === "mouseup") return "pointerup";
    return "pointermove";
  };

  const dispatchSyntheticMouse = (target, type, init) => {
    if (!target?.dispatchEvent) return;
    try {
      target.dispatchEvent(new MouseEvent(type, init));
    } catch {}
    if (typeof PointerEvent !== "undefined") {
      try {
        target.dispatchEvent(new PointerEvent(pointerTypeForMouse(type), {
          ...init,
          pointerId: 1,
          pointerType: "mouse",
          isPrimary: true,
        }));
      } catch {}
    }
  };

  const releaseDomPress = (x, y) => {
    const press = domPressRef.current;
    if (!press.active) return;

    const target = resolveDomTarget(x, y);
    const init = {
      bubbles: true,
      cancelable: true,
      clientX: x,
      clientY: y,
      view: window,
      button: 0,
      buttons: 0,
    };

    dispatchSyntheticMouse(document, "mouseup", init);
    if (target && target !== document) {
      dispatchSyntheticMouse(target, "mouseup", init);
    }

    const quickTap = !press.moved && Date.now() - press.pressedAt < 450;
    if (quickTap && target) {
      dispatchSyntheticMouse(target, "click", init);
    }

    domPressRef.current = { active: false, target: null, startX: 0, startY: 0, moved: false, pressedAt: 0 };
  };

  // ===== True barge-in (brief §13) =====
  //
  // OFF by default, deliberately. The server side is complete and tested
  // (turn cancellation + echo suppression), but this half cannot be validated
  // without a live microphone talking over live playback — and getting it wrong
  // degrades a voice loop that currently works. Enable to try it:
  //
  //     localStorage.setItem("nova.bargeIn", "1")   // then reload
  //
  // What changes when it is on: the microphone stays open while Nova speaks,
  // instead of the loop waiting for her to finish before listening again.
  const bargeInEnabled = () => {
    try {
      return window.localStorage.getItem("nova.bargeIn") === "1";
    } catch {
      return false;
    }
  };

  // ===== Barge-in telemetry (live acceptance harness) =====
  const bargeAttemptsRef = useRef([]);
  const couplingRef = useRef(new CouplingEstimator());

  const harnessOn = () => {
    try { return window.localStorage.getItem("nova.bargeInHarness") === "1"; }
    catch { return false; }
  };

  useEffect(() => {
    window.novaBargeIn = {
      report() {
        const s = summarize(bargeAttemptsRef.current);
        const text = [
          "=== Nova barge-in acceptance run ===",
          `attempts recorded        : ${s.total}`,
          `successful interrupts    : ${s.successes}`,
          `missed interrupts        : ${s.missed}`,
          `FALSE self-interrupts    : ${s.falseSelfInterrupts}`,
          `echoes correctly rejected: ${s.echoesCorrectlyRejected}`,
          `mixed speech salvaged    : ${s.mixedSalvaged}`,
          `stale audio leaks        : ${s.staleAudioLeaks}`,
          `median stop latency      : ${s.medianStopMs ?? "n/a"} ms`,
          `P90 stop latency         : ${s.p90StopMs ?? "n/a"} ms`,
          `median interrupt->reply  : ${s.medianReplyMs ?? "n/a"} ms`,
          `median measured coupling : ${s.medianCoupling ?? "n/a"}`,
        ].join("\n");
        console.log(text);
        try {
          navigator.clipboard?.writeText(
            text + "\n\n" + JSON.stringify(bargeAttemptsRef.current, null, 2));
        } catch {}
        return s;
      },
      attempts: () => bargeAttemptsRef.current,
      coupling: () => couplingRef.current.value(),
      reset() {
        bargeAttemptsRef.current = [];
        couplingRef.current.reset();
        console.log("[barge-in] harness reset");
      },
    };
    if (harnessOn()) {
      console.log("[barge-in] harness armed — 20 attempts. "
        + "Run window.novaBargeIn.report() when done.");
    }
    return () => { try { delete window.novaBargeIn; } catch {} };
  }, []);

  const watchForBargeIn = async (replyDone, myToken) => {
    let replyFinished = false;
    const settle = () => { replyFinished = true; };
    replyDone.then(settle, settle);

    const attempt = newAttempt(bargeAttemptsRef.current.length + 1);
    attempt.playbackStartedAt = performance.now();

    // ── Stage 1: acoustic gate ──────────────────────────────────────────────
    // Fires on levels alone, so it only DUCKS Nova. Nothing is cancelled until
    // the backend has classified the transcript — that asymmetry is what makes
    // a fast, fallible detector acceptable here.
    let stopWatch = () => {};
    const analyser = micKeepaliveRef.current?.analyser || null;
    if (analyser) {
      stopWatch = watchForSpeechOverPlayback(analyser, (ev) => {
        attempt.speechDetectedAt = ev.at;
        attempt.micLevel = ev.micLevel;
        attempt.ttsRms = ev.ttsRms;
        attempt.coupling = ev.coupling;
        if (duckPlayback(0.05)) attempt.playbackDuckedAt = performance.now();
      }, {}, couplingRef.current);
    }

    const finish = (outcome) => {
      stopWatch();
      attempt.outcome = outcome;
      if (harnessOn()) {
        bargeAttemptsRef.current.push(attempt);
        console.log(`[barge-in] attempt ${attempt.attempt}: ${outcome}`, attempt);
      }
    };

    try {
      while (voiceSessionRef.current && sessionCtlRef.current.token === myToken) {
        if (replyFinished && !ttsPlayingRef.current) {
          // Nova finished uninterrupted. If the gate ducked her anyway, that is
          // a false self-interrupt and is recorded as one rather than passing
          // silently as a clean run.
          restorePlayback();
          finish(attempt.playbackDuckedAt ? "false-self-interrupt" : "missed");
          return null;
        }

        let heard = "";
        try {
          const blob = await captureCommandBlob({ debugTag: "bargein", maxMs: 4000 });
          // Deliberately NO speaker classification. This capture is mixed
          // audio — the human plus Nova's own playback — so embedding it would
          // be classifying Nova's voice as often as anyone's.
          heard = await transcribeBlob(blob, { url: apiUrl("/stt"), debugTag: "bargein" });
        } catch {
          heard = "";
        }
        if (!heard?.trim()) {
          // A duck with no intelligible speech behind it was a false trigger.
          // Give Nova her volume back rather than leaving her quiet.
          if (attempt.playbackDuckedAt) restorePlayback();
          continue;
        }

        attempt.transcriptAt = performance.now();
        attempt.transcript = heard;

        // ── Stage 2: the backend decides ECHO / USER / MIXED ────────────────
        const verdict = await interruptActiveTurn(heard);
        attempt.classification = verdict?.classification ?? null;
        attempt.turnCancelled = verdict?.interrupted ?? null;

        if (verdict?.interrupted) {
          attempt.salvagedText = verdict.text || heard;
          stopActiveAudio();          // confirmed — now it is safe to be final
          finish("success");
          return String(attempt.salvagedText).trim();
        }

        // Pure echo. Nova was ducked on suspicion and is owed her volume back.
        restorePlayback();
        if (attempt.playbackDuckedAt) {
          finish("echo-rejected");
          return null;
        }
      }
      restorePlayback();
      finish(attempt.playbackDuckedAt ? "false-self-interrupt" : "missed");
      return null;
    } catch (e) {
      restorePlayback();
      finish("missed");
      throw e;
    }
  };

  const waitForResponseToFinish = async ({ maxMs = 45_000 } = {}) => {
    const t0 = Date.now();
    while (Date.now() - t0 < maxMs) {
      if (!thinkingRef.current && !ttsPlayingRef.current) return;
      await new Promise((r) => setTimeout(r, 120));
    }
  };

  const captureCommandBlob = async ({ debugTag = "command", maxMs = 8000 } = {}) => {
    const keep = micKeepaliveRef.current;
    if (keep?.stream) {
      return recordVoiceActivityFromStreamToBlob(keep.stream, {
        maxMs,
        minSpeechMs: 220,
        trailingSilenceMs: 850,
        speechThreshold: 0.02,
        startTimeoutMs: 4500,
        timesliceMs: 200,
        debugTag,
      });
    }
    return recordVoiceActivityToBlob({
      maxMs,
      minSpeechMs: 220,
      trailingSilenceMs: 850,
      speechThreshold: 0.02,
      startTimeoutMs: 4500,
      timesliceMs: 200,
      debugTag,
    });
  };

  // WS-H: an engaged voice session auto-disengages after this much silence,
  // so she isn't left hot-mic'd indefinitely if Marcus walks away. He can
  // always re-engage with the wake word.
  const SESSION_IDLE_TIMEOUT_MS = 90_000;

  const runVoiceSessionLoop = async () => {
    // Session loop: capture -> transcribe -> stop word? -> send -> wait -> repeat
    const myToken = (sessionCtlRef.current.token = sessionCtlRef.current.token + 1);
    setVoiceSession(true);
    showSystemMessage("Engaged listening. Say 'standby' or 'disengage' to stop.");
    let lastHeardAt = Date.now();

    try {
      while (micUnmutedRef.current && voiceSessionRef.current && sessionCtlRef.current.token === myToken) {
        if (capturingRef.current) {
          await new Promise((r) => setTimeout(r, 120));
          continue;
        }

        capturingRef.current = true;
        try {
          setPhase("CAPTURING_COMMAND", { reason: "session" });
          setVoiceStatus("listening");

          clearTtsQueue();
          const blob = await captureCommandBlob({ debugTag: "session", maxMs: 8000 });

          setVoiceStatus("transcribing");
          // Each session command gets its OWN classification and its own
          // one-use handle. A session is not proof the same human is still
          // talking: Marcus can start one and Alice can answer the next turn.
          const stt = await transcribeBlobDetailed(blob, {
            url: apiUrl("/stt"), speaker: true, debugTag: "session",
          });
          const text = stt.text;
          if (!text?.trim()) {
            if (Date.now() - lastHeardAt > SESSION_IDLE_TIMEOUT_MS) {
              clearTtsQueue();
              addSystem("Haven't heard anything for a bit — standing by. Say 'Hey Nova' when you need me.");
              setVoiceSession(false);
              setVoiceStatus("idle");
              setPhase("IDLE_LISTENING", { reason: "session_idle_timeout" });
              wakeResumeAtRef.current = Date.now();
              try { startWake?.(); } catch {}
              break;
            }
            setVoiceStatus("idle");
            await new Promise((r) => setTimeout(r, 200));
            continue;
          }
          lastHeardAt = Date.now();

          if (isInterruptPhrase(text)) {
            // Cancel on the server too, so the sentences Nova had queued for
            // this reply are never synthesised. The transcript goes with it:
            // the backend runs echo suppression first, so Nova hearing her own
            // voice through the speakers cannot cancel her own turn.
            await interruptActiveTurn(text);
            setVoiceStatus("idle");
            continue;
          }

          if (isSessionStopPhrase(text)) {
            clearTtsQueue();
            addSystem("Standing by.");
            setVoiceSession(false);
            setVoiceStatus("idle");
            setPhase("IDLE_LISTENING", { reason: "session_stop_word" });
            wakeResumeAtRef.current = Date.now();
            try { startWake?.(); } catch {}
            break;
          }

          // Send as normal chat message
          setVoiceStatus("speaking");
          if (bargeInEnabled()) {
            // True barge-in: keep the microphone open WHILE Nova speaks, so
            // talking over her interrupts without needing a stop phrase.
            // Everything heard here goes through the backend's echo suppression
            // first, because the mic is picking up Nova's own voice through the
            // speakers at the same time.
            const replyDone = sendMessage(text, [], voiceOrigin(voiceTurnIdOf(stt)));
            setPhase("RESPONDING", { reason: "session_sent" });
            const interruption = await watchForBargeIn(replyDone, myToken);
            if (interruption) {
              // Answer the salvaged words immediately rather than dropping
              // back to a pause the speaker would have to talk into again.
              //
              // UNVERIFIED on purpose: this text came from MIXED audio (a human
              // plus Nova's own output through the speakers), which is never
              // speaker-classified. Reusing the handle from the command Nova
              // was answering would attribute one person's interruption to
              // whoever spoke before them.
              lastHeardAt = Date.now();
              setVoiceStatus("speaking");
              await sendMessage(interruption, [], unverifiedVoiceOrigin());
              await waitForResponseToFinish();
            }
            setPhase("CAPTURING_COMMAND", { reason: "session_next" });
          } else {
            await sendMessage(text, [], voiceOrigin(voiceTurnIdOf(stt)));
            if (!ttsPlayingRef.current) setVoiceStatus("idle");
            setPhase("RESPONDING", { reason: "session_sent" });
            await waitForResponseToFinish();
            setPhase("CAPTURING_COMMAND", { reason: "session_next" });
          }
        } catch (e) {
          console.warn(e);
          setVoiceStatus("error");
          addSystem("Voice session error.");
          await new Promise((r) => setTimeout(r, 600));
          setVoiceStatus("idle");
        } finally {
          capturingRef.current = false;
        }
      }
    } finally {
      // If the loop exits without an explicit stop phrase, return to wake listening.
      if (micUnmutedRef.current && !voiceSessionRef.current) {
        wakeResumeAtRef.current = Date.now();
        scheduleWakeResumeCheck();
      }
    }
  };

  // Ensure window.__NOVA_API_BASE exists for Electron prod
  useEffect(() => {
    try {
      if (!window.__NOVA_API_BASE) window.__NOVA_API_BASE = "http://localhost:8008";
    } catch {}
  }, []);

  useEffect(() => {
    latestHandSampleRef.current = {
      status: hand?.status || "off",
      rawCursor: hand?.rawCursor || { x: 0, y: 0, visible: false },
      pinchRatio: Number(hand?.telemetry?.pinchRatio ?? 1),
    };
  }, [hand?.status, hand?.rawCursor, hand?.telemetry?.pinchRatio]);

  useEffect(() => {
    try {
      if (handCalibration) {
        window.localStorage.setItem(HAND_CALIBRATION_KEY, JSON.stringify(handCalibration));
      } else {
        window.localStorage.removeItem(HAND_CALIBRATION_KEY);
      }
    } catch {}
  }, [handCalibration]);

  useEffect(() => {
    if (!handCalibration || handCalibration.mirrorX !== undefined) return;
    const rebuilt = buildHandCalibration(handCalibration.samples || {});
    if (rebuilt) {
      setHandCalibration(rebuilt);
    }
  }, [handCalibration]);

  useEffect(() => {
    let cancelled = false;

    async function loadDesktopGestureStatus() {
      try {
        const status = await window.novaDesktop?.desktopGestureStatus?.();
        if (!cancelled) {
          const supported = Boolean(status?.supported);
          setDesktopControlAvailable(supported);
          if (!supported) setDesktopControlEnabled(false);
        }
      } catch {
        if (!cancelled) {
          setDesktopControlAvailable(false);
          setDesktopControlEnabled(false);
        }
      }
    }

    loadDesktopGestureStatus();
    return () => {
      cancelled = true;
    };
  }, []);

  const startHandCalibration = async ({ cameraReady = false } = {}) => {
    if (!camera.enabled && !cameraReady) {
      try {
        await camera.start();
        addSystem("Camera on (required for calibration).");
      } catch (e) {
        console.warn(e);
        addSystem("Calibration needs camera permission.");
        return;
      }
    }

    if (desktopControlEnabled && hand?.pinch?.down) {
      Promise.resolve(
        window.novaDesktop?.mouseUpSystemCursor?.({
          x: hand?.cursor?.x ?? 0.5,
          y: hand?.cursor?.y ?? 0.5,
        })
      ).catch(() => {});
      setDesktopControlEnabled(false);
    }

    if (domPressRef.current.active) {
      const x = Math.round((hand?.cursor?.x ?? 0.5) * window.innerWidth);
      const y = Math.round((hand?.cursor?.y ?? 0.5) * window.innerHeight);
      releaseDomPress(x, y);
    }

    setCalibrationSession({
      active: true,
      stepIndex: 0,
      capturing: false,
      samples: {},
      error: "",
      lastCompletedAt: null,
    });
    addSystem("Hand calibration started.");
  };

  const cancelHandCalibration = () => {
    setCalibrationSession(null);
    addSystem("Hand calibration canceled.");
  };

  const resetHandCalibration = () => {
    setHandCalibration(null);
    addSystem("Hand calibration reset.");
  };

  const captureHandCalibrationStep = async () => {
    if (!calibrationSession?.active || calibrationSession.capturing) return;

    const step = HAND_CALIBRATION_STEPS[calibrationSession.stepIndex];
    if (!step) return;

    const before = latestHandSampleRef.current;
    if (before.status !== "ready" || !before.rawCursor?.visible) {
      setCalibrationSession((prev) => prev ? { ...prev, error: "Show one hand clearly to the camera before capturing." } : prev);
      return;
    }

    setCalibrationSession((prev) => prev ? { ...prev, capturing: true, error: "" } : prev);

    const startedAt = Date.now();
    const samples = [];
    while (Date.now() - startedAt < 900) {
      const sample = latestHandSampleRef.current;
      if (sample.status === "ready" && sample.rawCursor?.visible) {
        samples.push({
          rawX: Number(sample.rawCursor.x ?? 0),
          rawY: Number(sample.rawCursor.y ?? 0),
          pinchRatio: Number(sample.pinchRatio ?? 1),
        });
      }
      // eslint-disable-next-line no-await-in-loop
      await new Promise((resolve) => window.setTimeout(resolve, 35));
    }

    const averaged = averageCalibrationSamples(samples);
    if (!averaged) {
      setCalibrationSession((prev) => prev ? { ...prev, capturing: false, error: "Could not sample your hand. Hold still and try again." } : prev);
      return;
    }

    const nextSamples = { ...(calibrationSession.samples || {}), [step.key]: averaged };
    const nextIndex = calibrationSession.stepIndex + 1;

    if (nextIndex >= HAND_CALIBRATION_STEPS.length) {
      const built = buildHandCalibration(nextSamples);
      if (!built) {
        setCalibrationSession((prev) => prev ? { ...prev, capturing: false, error: "Calibration data was incomplete. Restart and try again." } : prev);
        return;
      }

      setHandCalibration(built);
      setCalibrationSession({
        active: false,
        stepIndex: HAND_CALIBRATION_STEPS.length,
        capturing: false,
        samples: nextSamples,
        error: "",
        lastCompletedAt: built.createdAt,
      });
      addSystem("Hand calibration saved.");
      return;
    }

    setCalibrationSession((prev) => prev ? {
      ...prev,
      capturing: false,
      samples: nextSamples,
      stepIndex: nextIndex,
      error: "",
    } : prev);
  };

  // ===== Chat streaming helpers =====
  const beginTtsPlayback = () => {
    ttsPlayingRef.current = true;
    setTtsPlaying(true);
    setVoiceStatus("speaking");
  };
  const endTtsPlayback = () => {
    ttsPlayingRef.current = false;
    setTtsPlaying(false);
    // Only fall back to idle if we're not actively capturing/listening for the
    // next voice command — otherwise this would clobber that state and the
    // avatar/jaw-drive fallback would never see it latch on "speaking" forever.
    if (!capturingRef.current) setVoiceStatus("idle");
  };

  // ===== Sequential TTS queue (backend streams one audio clip per sentence) =====
  const ttsQueueRef = useRef([]);
  const ttsBusyRef = useRef(false);

  const clearTtsQueue = () => {
    ttsQueueRef.current = [];
    ttsBusyRef.current = false;
    try { stopActiveAudio(); } catch {}
    endTtsPlayback();
  };

  // Server-side turn id for the reply currently streaming (from the SSE `meta`
  // event). Needed to cancel a turn on the BACKEND, not just locally.
  const activeTurnIdRef = useRef(null);

  // Stopping playback locally is only half an interruption: without telling the
  // server, it keeps synthesising every remaining sentence of the abandoned
  // reply and keeps emitting `tts` events for it. This cancels the turn at the
  // source, so queued sentences are never synthesised and any clip already in
  // flight is discarded instead of played over the next answer.
  const interruptActiveTurn = async (transcript = "") => {
    const turnId = activeTurnIdRef.current;
    clearTtsQueue();
    if (!turnId && !conversationIdRef.current) return null;
    activeTurnIdRef.current = null;
    try {
      const res = await fetch(apiUrl("/voice/interrupt"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ...(turnId ? { turn_id: turnId } : {}),
          ...(conversationIdRef.current ? { conversation_id: conversationIdRef.current } : {}),
          ...(transcript ? { transcript } : {}),
          reason: "user_interrupt",
        }),
      });
      if (!res.ok) return null;
      return await res.json();
    } catch {
      // A failed interrupt must never break the session loop — playback is
      // already stopped locally, which is the part the user can hear.
      return null;
    }
  };

  const pumpTtsQueue = () => {
    if (ttsBusyRef.current) return;
    const next = ttsQueueRef.current.shift();
    if (!next) {
      endTtsPlayback();
      return;
    }
    ttsBusyRef.current = true;
    beginTtsPlayback();
    const advance = () => {
      ttsBusyRef.current = false;
      if (ttsQueueRef.current.length) pumpTtsQueue();
      else endTtsPlayback();
    };
    playAudioUrl(next.url, {
      debugTag: "tts",
      preloadedBlob: next.blobPromise,
      onEnded: advance,
      onError: () => {},
    }).catch(() => advance());
  };

  const enqueueTts = (url) => {
    // Start fetching+decoding this clip's audio the moment it's enqueued
    // instead of waiting for the previous clip to finish — by the time
    // playback advances here, the blob is usually already resolved, which
    // removes the audible dead-air between sentences.
    ttsQueueRef.current.push({ url, blobPromise: prefetchAudioBlob(url) });
    pumpTtsQueue();
  };

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
  const attachImage = (imageUrl, prompt) => {
    setMessages((prev) => {
      const next = [...prev];
      const last = next[next.length - 1];
      const entry = { url: imageUrl, prompt };
      if (last && last.sender === "nova") {
        next[next.length - 1] = { ...last, images: [...(last.images || []), entry] };
      } else {
        next.push({ id: `nova-image-${Date.now()}`, sender: "nova", text: "", images: [entry] });
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

  async function nonStreamingFallback(text, files = [], origin = null) {
    const resolvedLocation = await locationForMessage(text);
    // Same origin as the stream attempt. If the stream already redeemed the
    // handle, the backend resolves this retry as UNVERIFIED — correct and safe.
    // Dropping the origin to make the retry "work" would promote it to typed
    // owner, which is the one outcome that must never happen (V3 P5.1e).
    const resp = await fetch(apiUrl("/chat"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(buildFallbackBody({
        text,
        attachments: files,
        conversationId,
        location: resolvedLocation,
        origin,
      })),
    });
    if (!resp.ok) {
      const raw = await resp.text();
      try {
        const obj = JSON.parse(raw);
        const detail = obj?.detail || obj?.error || "";
        if (detail) throw new Error(String(detail));
      } catch {}
      throw new Error(raw || `HTTP ${resp.status}`);
    }
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
    clearTtsQueue();
    setThinking(false);
    finalizeReply();
    addSystem("Stopped.");
  };

  // Main sendMessage for ChatPanel
  const sendMessage = async (text, files = [], origin = null) => {
    const cleanText = (text || "").trim();
    const attachments = Array.isArray(files) ? files.filter(Boolean) : [];
    if (!cleanText && !attachments.length) return;

    const resolvedLocation = await locationForMessage(cleanText);

    const userId = `user-${Date.now()}`;
    const replyId = `nova-${Date.now()}`;
    setMessages((prev) => [...prev, { id: userId, sender: "user", text: cleanText, files: attachments }]);
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
        body: JSON.stringify(buildStreamBody({
          text: cleanText,
          attachments,
          conversationId,
          location: resolvedLocation,
          origin,
        })),
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
                  // Server-side turn identity. Cancelling by id is what stops
                  // synthesis of sentences that have not been spoken yet.
                  if (piece?.turn_id) activeTurnIdRef.current = String(piece.turn_id);
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
                    // Sentence clips arrive as the reply streams; queue them in order.
                    enqueueTts(apiUrl(String(aurl)));
                  }
                  continue;
                }
                if (currentEvent === "tts_error") {
                  addSystem(`TTS error: ${piece?.error || "unknown"}`);
                  continue;
                }
                if (currentEvent === "action") {
                  try {
                    if (piece?.type === "open_overlay" && piece?.overlay) {
                      if (piece.overlay === "maps" && piece.map_payload) {
                        setMapsRoutePreload(piece.map_payload);
                      }
                      setActiveOverlay(piece.overlay);
                    }
                    if (piece?.type === "image_generated" && piece?.image_url) {
                      attachImage(piece.image_url, piece.prompt || "");
                    }
                  } catch {}
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
              if (piece?.turn_id) activeTurnIdRef.current = String(piece.turn_id);
            }
            if (currentEvent === "tts") {
              const aurl = piece?.audio_url;
              if (aurl) enqueueTts(apiUrl(String(aurl)));
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
        const reply = await nonStreamingFallback(cleanText, attachments, origin);
        return reply;
      }
    } catch (err) {
      if (ctl.signal.aborted) return;
      console.error("stream error:", err);
      try {
        const reply = await nonStreamingFallback(cleanText, attachments, origin);
        return reply;
      } catch (e2) {
        const msg = (e2 && e2.message) ? String(e2.message) : "Sorry — I hit a connection error.";
        setReply(msg, true);
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
      showSystemMessage("Listening…");

      clearTtsQueue();
      const blob = await captureCommandBlob({ debugTag: "capture", maxMs: 8000 });
      setVoiceStatus("transcribing");
      showSystemMessage("Transcribing…");

      // Transcription finished gate for wake resumption.
      transcribeDoneAtRef.current = 0;

      // Clean command audio: the one place speaker identification belongs.
      // ONE upload, ONE decode, ONE Whisper pass, at most one embedding — the
      // handle rides back on this same response rather than costing a second
      // transcription (V3 P5.1e).
      const stt = await transcribeBlobDetailed(blob, {
        url: apiUrl("/stt"), speaker: true, debugTag: "capture",
      });
      const text = stt.text;
      transcribeDoneAtRef.current = Date.now();

      if (!text?.trim()) {
        showSystemMessage("No speech detected.");
        setVoiceStatus("idle");
        setPhase("IDLE_LISTENING", { reason: "empty_transcript" });
        return;
      }

      setPhase("RESPONDING");
      // We'll stay in "speaking" while actual TTS audio is playing.
      setVoiceStatus("speaking");
      showSystemMessage(`You said: ${text}`);

      // Do not restart wake until transcription is done AND we either:
      // - wait a short cooldown (default), or
      // - TTS playback begins (handled in sendMessage SSE tts event).
      const cooldownMs = 4000;
      wakeResumeAtRef.current = Date.now() + cooldownMs;

      // Send message and then speak last assistant reply. A missing handle
      // still travels as VOICE — never as typed — so it resolves unverified.
      await sendMessage(text, [], voiceOrigin(voiceTurnIdOf(stt)));

      // If TTS is not playing, return to idle immediately.
      if (!ttsPlayingRef.current) {
        setVoiceStatus("idle");
      }
      setPhase("IDLE_LISTENING", { reason: "response_complete" });
    } catch (e) {
      console.warn(e);
      setVoiceStatus("error");
      showSystemMessage("Voice error.");
      setTimeout(() => setVoiceStatus("idle"), 1200);
    } finally {
      capturingRef.current = false;

      // Resume wake only when allowed by the state machine + cooldown.
      if (micKeepaliveRef.current && micUnmutedRef.current && !voiceSessionRef.current) {
        setPhase("IDLE_LISTENING", { reason: "capture_finally" });
        scheduleWakeResumeCheck();
      }
    }
  };

  const { startWake, stopWake } = useWakeNova(() => {
    // IDLE_LISTENING -> ARMED
    setPhase("ARMED");
    setVoiceStatus("wake");
    try {
      if (wakePulseTimerRef.current) window.clearTimeout(wakePulseTimerRef.current);
    } catch {}
    setWakePulse(true);
    wakePulseTimerRef.current = window.setTimeout(() => setWakePulse(false), 1200);
    showSystemMessage("Wake word detected (Hey Nova).");
    try { clearTtsQueue(); } catch {}
    // Stop wake while we enter engaged session.
    try { stopWake?.(); } catch {}
    // Ensure wake won't restart while engaged.
    wakeResumeAtRef.current = Date.now() + 60_000;
    setPhase("ARMED", { reason: "wake_engage" });
    // Start engaged listening session.
    setTimeout(() => runVoiceSessionLoop(), 120);
  }, "Hey Nova", {
    onStatus: (msg) => {
      try {
        // Avoid spamming: keep these short and system-only.
        showSystemMessage(String(msg));
      } catch {}
    },
  });

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

  useEffect(() => {
    if (calibrationSession?.active) return;

    const ready = gesturesOn && camera.enabled && hand?.status === "ready" && hand?.cursor?.visible;

    if (!ready) {
      if (domPressRef.current.active) {
        const lastX = Math.round((hand?.cursor?.x ?? 0.5) * window.innerWidth);
        const lastY = Math.round((hand?.cursor?.y ?? 0.5) * window.innerHeight);
        releaseDomPress(lastX, lastY);
      }
      if (desktopControlEnabled && hand?.pinch?.down) {
        Promise.resolve(
          window.novaDesktop?.mouseUpSystemCursor?.({
            x: hand?.cursor?.x ?? 0.5,
            y: hand?.cursor?.y ?? 0.5,
          })
        ).catch(() => {});
      }
      return;
    }

    const normX = hand.cursor.x ?? 0;
    const normY = hand.cursor.y ?? 0;
    const x = Math.round(normX * window.innerWidth);
    const y = Math.round(normY * window.innerHeight);

    if (desktopControlEnabled) {
      const now = Date.now();
      if (now - desktopMoveAtRef.current >= 16 && !desktopMoveInFlightRef.current) {
        desktopMoveAtRef.current = now;
        desktopMoveInFlightRef.current = true;
        Promise.resolve(
          window.novaDesktop?.moveSystemCursor?.({ x: normX, y: normY })
        ).finally(() => {
          desktopMoveInFlightRef.current = false;
        });
      }

      if (hand?.pinch?.justPressed) {
        Promise.resolve(
          window.novaDesktop?.mouseDownSystemCursor?.({ x: normX, y: normY })
        ).catch(() => {});
      }

      if (hand?.pinch?.justReleased) {
        Promise.resolve(
          window.novaDesktop?.mouseUpSystemCursor?.({ x: normX, y: normY })
        ).catch(() => {});
      }
      return;
    }

    const buttons = domPressRef.current.active ? 1 : 0;
    const moveInit = {
      bubbles: true,
      cancelable: true,
      clientX: x,
      clientY: y,
      view: window,
      button: 0,
      buttons,
    };

    dispatchSyntheticMouse(document, "mousemove", moveInit);
    const hoverTarget = resolveDomTarget(x, y);
    if (hoverTarget && hoverTarget !== document) {
      dispatchSyntheticMouse(hoverTarget, "mousemove", moveInit);
    }

    if (domPressRef.current.active) {
      const moved = Math.hypot(x - domPressRef.current.startX, y - domPressRef.current.startY);
      if (moved > 8) domPressRef.current.moved = true;
    }

    if (hand?.pinch?.justPressed && !domPressRef.current.active) {
      const target = hoverTarget;
      domPressRef.current = {
        active: true,
        target,
        startX: x,
        startY: y,
        moved: false,
        pressedAt: Date.now(),
      };
      const downInit = {
        bubbles: true,
        cancelable: true,
        clientX: x,
        clientY: y,
        view: window,
        button: 0,
        buttons: 1,
      };
      dispatchSyntheticMouse(document, "mousedown", downInit);
      if (target && target !== document) {
        dispatchSyntheticMouse(target, "mousedown", downInit);
      }
      return;
    }

    if (hand?.pinch?.justReleased) {
      releaseDomPress(x, y);
    }
  }, [
    gesturesOn,
    camera.enabled,
    desktopControlEnabled,
    hand?.status,
    hand?.cursor?.visible,
    hand?.cursor?.x,
    hand?.cursor?.y,
    hand?.pinch?.down,
    hand?.pinch?.justPressed,
    hand?.pinch?.justReleased,
    calibrationSession?.active,
  ]);

  // Dock actions
  const onToggleMic = async () => {
    // User gesture entrypoint: request permission here.
    if (micMuted) {
      setMicRequesting(true);
      showSystemMessage("Requesting microphone permission…");
      try {
        // Acquire and keep the stream open while unmuted.
        if (!micKeepaliveRef.current) {
          // If permission prompt hangs, at least show UI feedback.
          micKeepaliveRef.current = await Promise.race([
            acquireMicStreamHandle({ debugTag: "toggle" }),
            new Promise((_, rej) => setTimeout(() => rej(new Error("mic_permission_timeout")), 12000)),
          ]);
        }

        // Ensure AudioContext is unlocked while we still have a user gesture.
        try { await unlockAudioContext(); } catch {}

        try {
          setMicStream(micKeepaliveRef.current?.stream || null);
        } catch {
          setMicStream(null);
        }

        setMicMuted(false);
        showSystemMessage("Mic unmuted.");
        // Start wake from the same user gesture, but still honor state machine.
        setPhase("IDLE_LISTENING", { reason: "toggle_unmute" });
        wakeResumeAtRef.current = Date.now();
        try { startWake?.(); } catch {}
        scheduleWakeResumeCheck();
      } catch (e) {
        console.warn(e);
        showSystemMessage("Mic permission denied or unavailable.");
        setMicMuted(true);
        try {
          micKeepaliveRef.current?.release?.();
        } catch {}
        micKeepaliveRef.current = null;
      } finally {
        setMicRequesting(false);
      }
      return;
    }

    // Muting
    setMicMuted(true);
    showSystemMessage("Mic muted.");
    try { stopWake?.(); } catch {}
    setVoiceSession(false);
    // bump token so any session loop exits quickly
    sessionCtlRef.current.token = sessionCtlRef.current.token + 1;
    setPhase("IDLE_LISTENING", { reason: "toggle_mute" });
    try { micKeepaliveRef.current?.release?.(); } catch {}
    micKeepaliveRef.current = null;
    setMicStream(null);
    setMicRequesting(false);
  };
  const onToggleCameraPower = async () => {
    try {
      if (!camera.enabled) {
        await camera.start();
        addSystem("Camera on.");
        return;
      }

      await camera.stop();
      if (calibrationSession?.active) {
        setCalibrationSession(null);
        addSystem("Hand calibration canceled.");
      }
      if (gesturesOn) {
        setGesturesOn(false);
        cameraOverlayAutoGesturesRef.current = false;
        addSystem("Hand tracking disabled.");
      }
      addSystem("Camera off.");
      setActiveOverlay((cur) => (cur === "camera" ? null : cur));
    } catch (e) {
      console.warn(e);
      addSystem("Camera error.");
    }
  };

  const onToggleCameraOverlay = async () => {
    const opening = activeOverlay !== "camera";

    if (!opening) {
      setActiveOverlay(null);
      if (cameraOverlayAutoGesturesRef.current && gesturesOn) {
        setGesturesOn(false);
        addSystem("Hand tracking disabled.");
      }
      cameraOverlayAutoGesturesRef.current = false;
      return;
    }

    setActiveOverlay("camera");

    if (!camera.enabled) {
      try {
        await camera.start();
        addSystem("Camera on (required for hand tracking).");
      } catch (e) {
        console.warn(e);
        addSystem("Gestures need camera permission.");
        return;
      }
    }

    if (!handCalibration) {
      cameraOverlayAutoGesturesRef.current = false;
      await startHandCalibration({ cameraReady: true });
      return;
    }

    if (!gesturesOn) {
      setGesturesOn(true);
      cameraOverlayAutoGesturesRef.current = true;
      addSystem("Hand tracking linked to camera window.");
    } else {
      cameraOverlayAutoGesturesRef.current = false;
    }
  };

  const onCloseCameraOverlay = () => {
    setActiveOverlay(null);
    if (calibrationSession?.active) {
      setCalibrationSession(null);
      addSystem("Hand calibration canceled.");
    }
    if (cameraOverlayAutoGesturesRef.current && gesturesOn) {
      setGesturesOn(false);
      addSystem("Hand tracking disabled.");
    }
    cameraOverlayAutoGesturesRef.current = false;
  };

  const onToggleGestures = async () => {
    const next = !gesturesOn;
    cameraOverlayAutoGesturesRef.current = false;
    if (next && !camera.enabled) {
      try {
        await camera.start();
        addSystem("Camera on (required for gestures).");
      } catch (e) {
        console.warn(e);
        addSystem("Gestures need camera permission.");
        return;
      }
    }
    if (!next) {
      if (domPressRef.current.active) {
        const x = Math.round((hand?.cursor?.x ?? 0.5) * window.innerWidth);
        const y = Math.round((hand?.cursor?.y ?? 0.5) * window.innerHeight);
        releaseDomPress(x, y);
      }
      if (desktopControlEnabled && hand?.pinch?.down) {
        Promise.resolve(
          window.novaDesktop?.mouseUpSystemCursor?.({
            x: hand?.cursor?.x ?? 0.5,
            y: hand?.cursor?.y ?? 0.5,
          })
        ).catch(() => {});
      }
    }
    setGesturesOn(next);
    addSystem(next ? "Gestures enabled." : "Gestures disabled.");
  };
  const onToggleDesktopControl = async () => {
    if (!desktopControlAvailable) {
      addSystem("Desktop hand control is unavailable here.");
      return;
    }
    const next = !desktopControlEnabled;

    if (next && domPressRef.current.active) {
      const x = Math.round((hand?.cursor?.x ?? 0.5) * window.innerWidth);
      const y = Math.round((hand?.cursor?.y ?? 0.5) * window.innerHeight);
      releaseDomPress(x, y);
    }

    if (!next && hand?.pinch?.down) {
      Promise.resolve(
        window.novaDesktop?.mouseUpSystemCursor?.({
          x: hand?.cursor?.x ?? 0.5,
          y: hand?.cursor?.y ?? 0.5,
        })
      ).catch(() => {});
    }

    setDesktopControlEnabled(next);
    addSystem(next ? "Desktop hand control enabled." : "Desktop hand control disabled.");
  };
  const onOpenOverlay = (key) => {
    setActiveOverlay((cur) => (cur === key ? null : key));
  };

  const onToggleChat = () => setChatOpen((v) => !v);

  const onSelectSection = (key) => {
    setActiveSection(key);
    if (key === "chat") {
      setChatOpen(true);
      return;
    }
    if (key === "settings") {
      setActiveOverlay("settings");
      return;
    }
    if (key === "memory") {
      setActiveOverlay("memory");
      return;
    }
    if (key === "graph") {
      setActiveOverlay("graph");
      return;
    }
    if (key === "system") {
      setActiveOverlay("system");
      return;
    }
    if (key === "tasks") {
      setActiveOverlay("tasks");
      return;
    }
    if (key === "improvements") {
      setActiveOverlay("improvements");
      return;
    }
    if (key === "home") {
      setActiveOverlay(null);
    }
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

  const [coreStatus, setCoreStatus] = useState({
    status: "offline",
    statusText: "Booting",
    model: "Loading",
    contextLength: "8192 tokens",
    gpu: "Unknown",
    temperature: "--",
    tokenUsage: "—",
  });

  useEffect(() => {
    let canceled = false;

    const updateStatus = async () => {
      try {
        const resp = await fetch(apiUrl("/status"));
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const status = await resp.json();
        if (canceled) return;

        const modelName = status?.model?.name || "No model";
        const gpuActive = Boolean(status?.model?.enforcement?.active);
        const gpu = status?.gpu || {};
        const ctxTokens = Number(status?.model?.context_tokens || 0);
        const usage = status?.model?.usage || {};
        const avgTokens = Number(usage?.avg_reply_tokens || 0);

        setCoreStatus((prev) => ({
          ...prev,
          status: gpuActive ? "online" : "offline",
          statusText: buildProgressRef.current || (gpuActive ? "Online" : "Idle"),
          model: modelName,
          contextLength: ctxTokens ? `${ctxTokens.toLocaleString()} tokens` : "Unknown",
          gpu: gpu?.available && gpu?.name ? gpu.name : String(status?.model?.enforcement?.status || "unknown").replace(/_/g, " "),
          temperature: gpu?.available && gpu?.temperature_c != null ? `${gpu.temperature_c}°C` : "--",
          vram:
            gpu?.available && gpu?.vram_total_mb
              ? `${Math.round((gpu.vram_used_mb || 0) / 1024 * 10) / 10} / ${Math.round(gpu.vram_total_mb / 1024 * 10) / 10} GB`
              : null,
          tokenUsage: avgTokens > 0 ? `${Math.round(avgTokens)} avg/call` : "—",
        }));
      } catch {
        if (!canceled) {
          setCoreStatus((prev) => ({
            ...prev,
            status: "offline",
            statusText: "Offline",
            gpu: "Unavailable",
            temperature: "--",
          }));
        }
      }
    };

    updateStatus();
    const id = setInterval(updateStatus, 6000);
    return () => {
      canceled = true;
      clearInterval(id);
    };
  }, []);

  // Token usage comes from /status (real per-reply averages tracked by the
  // LLM runtime) — no client-side estimation.

  // ===== Project builder reports (from the live event stream) =====
  const speakNotice = async (text) => {
    try {
      const resp = await fetch(apiUrl("/speak"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
      });
      if (!resp.ok) return;
      const blob = await resp.blob();
      enqueueTts(URL.createObjectURL(blob));
    } catch {}
  };

  // U8: live build progress. `project.progress` events already fired for every
  // stage but nothing rendered them, so a multi-minute build looked like total
  // silence — which is what made the flappy-bird session so opaque. Show the
  // current stage in the status line, and clear it when the build ends.
  useEffect(() => {
    if (!novaEvents?.length) return;
    let latest = null;
    for (const ev of novaEvents) {
      if (ev.type === "project.progress" || ev.type === "project.completed" || ev.type === "project.error") {
        latest = ev;
      }
    }
    if (!latest) return;

    if (latest.type !== "project.progress") {
      setBuildProgress("");   // build finished (or failed) — stop showing a stage
      return;
    }
    const d = latest.data || {};
    const stage = String(d.stage || "").replace(/_/g, " ").trim();
    if (!stage) return;
    setBuildProgress(d.file ? `${stage}: ${d.file}` : stage);
  }, [novaEvents]);

  // Bus-event side effects (project reports, reminders, screen-look
  // requests) live in hooks/useNovaBusEffects.js — extracted in Phase 0.6.
  useNovaBusEffects({
    novaEvents,
    appendNovaMessage: (id, text) => setMessages((prev) => [...prev, { id, sender: "nova", text }]),
    speakNotice,
    onScreenLookRequest: setScreenLookRequest,
  });


  // PI1: opt-in periodic screen glances. Best-effort — a failed/empty glance
  // just waits for the next interval, never surfaces an error to the user.
  useFocusSession({
    enabled: focusSessionOn,
    apiBase: apiBase(),
    onNotable: (text) => {
      setMessages((prev) => [...prev, { id: `nova-focus-${Date.now()}`, sender: "nova", text: `👀 ${text}` }]);
      speakNotice(text);
    },
  });


  async function respondToScreenLookRequest(approved) {
    const req = screenLookRequest;
    if (!req) return;
    setScreenLookRequest(null);
    if (!approved) {
      try {
        await fetch(apiUrl("/vision/screen_capture_result"), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ request_id: req.requestId, approved: false }),
        });
      } catch {}
      return;
    }
    try {
      if (!window.novaDesktop?.captureScreen) {
        await fetch(apiUrl("/vision/screen_capture_result"), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ request_id: req.requestId, approved: true, error: "desktop_capture_unavailable" }),
        });
        return;
      }
      const capture = await window.novaDesktop.captureScreen();
      if (!capture?.ok || !capture?.dataUrl) {
        await fetch(apiUrl("/vision/screen_capture_result"), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ request_id: req.requestId, approved: true, error: capture?.error || "capture_failed" }),
        });
        return;
      }
      const blob = await (await fetch(capture.dataUrl)).blob();
      const fd = new FormData();
      fd.append("file", blob, "screen.png");
      const q = req.question || "Describe what's on screen.";
      const resp = await fetch(apiUrl(`/vision/analyze?question=${encodeURIComponent(q)}`), { method: "POST", body: fd });
      const data = resp.ok ? await resp.json() : null;
      await fetch(apiUrl("/vision/screen_capture_result"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          request_id: req.requestId, approved: true,
          text: data?.text || "", error: resp.ok ? "" : "vision_analyze_failed",
        }),
      });
    } catch (e) {
      try {
        await fetch(apiUrl("/vision/screen_capture_result"), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ request_id: req.requestId, approved: true, error: String(e?.message || e) }),
        });
      } catch {}
    }
  }

  // Orb state mapping: voice first, then live backend activity states.
  const orbState = useMemo(() => {
    if (voiceStatus === "wake" || voiceStatus === "listening" || voiceStatus === "transcribing") return "listening";
    if (voiceStatus === "speaking" || ttsPlaying) return "speaking";
    if (novaActivity.vision) return "vision";
    if (novaActivity.web) return "searching";
    if (novaActivity.tool) return "working";
    if (thinking || novaActivity.thinking) return "thinking";
    if (novaActivity.memory) return "memory";
    return "idle";
  }, [voiceStatus, thinking, ttsPlaying, novaActivity]);

  const currentCalibrationStep = calibrationSession?.active
    ? HAND_CALIBRATION_STEPS[calibrationSession.stepIndex] || null
    : null;

  return (
    <div className="relative w-screen h-screen overflow-hidden text-nova-gold font-space">
      <div className="absolute inset-0 -z-10 pointer-events-none">
        <Suspense fallback={null}>
          <AnimatedBackground />
        </Suspense>
      </div>

      <TopBar
        version="v3"
        project={coreStatus.status === "online" ? "SYSTEM ONLINE" : "SYSTEM OFFLINE"}
        systemOnline={coreStatus.status === "online"}
        micMuted={micMuted}
        micLevel={micLevel}
        micRequesting={micRequesting}
        voiceStatus={voiceStatus}
        voicePhase={voicePhase}
        voiceSessionActive={voiceSessionActive}
        ttsPlaying={ttsPlaying}
        wakePulse={wakePulse}
        timeText={timeText}
        activity={novaActivity}
        eventsConnected={eventsConnected}
      />

      <main className="absolute inset-0 pt-14 pb-28 px-3 lg:px-5 z-20">
        <div className="grid grid-cols-1 lg:grid-cols-[280px_minmax(0,1fr)_420px] gap-4 h-full min-h-0">
          <div className="hidden lg:block min-h-0">
            <LeftSidebar activeSection={activeSection} onSelect={onSelectSection} coreStatus={coreStatus} />
          </div>

          <section className="nova-center-stage-panel">
            <HologramStage
              state={orbState}
              avatar={
                <Suspense fallback={<div className="nova-hologram-loading">Loading avatar...</div>}>
                  <NovaHologramAvatar
                    state={orbState}
                    size={Math.min(
                      510,
                      typeof window !== "undefined"
                        ? Math.min(Math.max(300, window.innerWidth * 0.36), window.innerHeight * 0.62)
                        : 510
                    )}
                    micLevel={micLevel}
                    ttsPlaying={ttsPlaying}
                  />
                </Suspense>
              }
              waveform={
                <Waveform
                  mode={voiceStatus === "speaking" ? "ring" : "bars"}
                  mediaStream={micMuted ? null : micStream}
                  height={54}
                  theme={{
                    primary: "#8B5CF6",
                    secondary: "#A78BFA",
                    glow: "#F5C542",
                  }}
                />
              }
            />
          </section>

          <section
            className={[
              "min-h-0 transition-all duration-300",
              chatOpen ? "opacity-100" : "opacity-0 pointer-events-none lg:opacity-45",
            ].join(" ")}
          >
            <div className="h-full">
              <Suspense fallback={<div className="h-full hud-panel grid place-items-center text-nova-gold/70 text-sm">Loading chat...</div>}>
                <ChatPanel
                  messages={messages}
                  onSendMessage={sendMessage}
                  onStop={stopStream}
                  isAssistantThinking={thinking}
                />
              </Suspense>
            </div>
          </section>
        </div>
      </main>

      <BottomDock
        chatOpen={chatOpen}
        micMuted={micMuted}
        cameraOn={camera.enabled}
        gesturesOn={gesturesOn}
        focusSessionOn={focusSessionOn}
        activeOverlay={activeOverlay}
        onToggleChat={onToggleChat}
        onToggleMic={onToggleMic}
        onToggleCameraPower={onToggleCameraPower}
        onToggleCameraOverlay={onToggleCameraOverlay}
        onToggleGestures={onToggleGestures}
        onToggleFocusSession={() => setFocusSessionOn((v) => !v)}
        onOpenOverlay={onOpenOverlay}
        voiceStatus={voiceStatus}
        ttsPlaying={ttsPlaying}
      />

      {/* PI1: agent-requested screen look — requires an explicit click, never silent */}
      {screenLookRequest ? (
        <div className="fixed top-6 left-1/2 -translate-x-1/2 z-[80] rounded-2xl border border-nova-gold/25 bg-black/70 backdrop-blur-xl px-5 py-4 shadow-[0_10px_30px_rgba(0,0,0,0.5)] max-w-md">
          <div className="text-sm text-nova-gold">Nova wants to look at your screen{screenLookRequest.question ? ` — ${screenLookRequest.question}` : ""}.</div>
          <div className="mt-3 flex gap-2 justify-end">
            <button
              type="button"
              onClick={() => respondToScreenLookRequest(false)}
              className="rounded-xl border border-nova-gold/20 px-3 py-1.5 text-sm text-nova-gold/80 hover:bg-black/25"
            >
              Decline
            </button>
            <button
              type="button"
              onClick={() => respondToScreenLookRequest(true)}
              className="rounded-xl bg-nova-gold/20 border border-nova-gold/30 px-3 py-1.5 text-sm text-nova-gold hover:bg-nova-gold/30"
            >
              Capture
            </button>
          </div>
        </div>
      ) : null}

      {/* Gesture cursor overlay */}
      {gesturesOn && (
        <div className="fixed left-6 bottom-28 z-[60] pointer-events-none">
          <div className="rounded-2xl border border-nova-gold/15 bg-black/45 px-4 py-3 shadow-[0_10px_30px_rgba(0,0,0,0.45)] backdrop-blur-xl">
            <div className="text-[10px] uppercase tracking-[0.24em] text-nova-gold/55">Hand Control</div>
            <div className="mt-1 text-sm text-nova-gold">
              {hand?.status === "ready" ? "Tracking" : hand?.status === "no_hands" ? "Looking for a hand" : hand?.status || "off"}
            </div>
            <div className="mt-1 text-xs text-nova-gold/70">
              {desktopControlEnabled ? "Targeting desktop cursor" : "Targeting Nova window"}
            </div>
            <div className="mt-1 text-xs text-nova-gold/55">
              Hands: {Number.isFinite(hand?.handsDetected) ? hand.handsDetected : 0} • Pinch: {hand?.pinch?.down ? "down" : "open"}
            </div>
            <div className="mt-1 text-xs text-nova-gold/55">
              Hold: {Math.round((hand?.pinch?.holdMs ?? 0) / 10) * 10}ms • Strength: {Math.round((hand?.pinch?.strength ?? 0) * 100)}%
            </div>
          </div>
        </div>
      )}

      {gesturesOn && camera.enabled && hand?.cursor?.visible && (
        <div className="fixed inset-0 z-[60] pointer-events-none">
          <div
            className={[
              "absolute -translate-x-1/2 -translate-y-1/2",
              "w-5 h-5 rounded-full",
              "border border-nova-gold/80",
              hand?.pinch?.down ? "bg-nova-gold/40 scale-110" : "bg-nova-purple/20",
            ].join(" ")}
            style={{
              left: `${(hand.cursor.x ?? 0) * 100}%`,
              top: `${(hand.cursor.y ?? 0) * 100}%`,
            }}
          >
            <div className="absolute left-1/2 top-7 -translate-x-1/2 whitespace-nowrap rounded-full border border-nova-gold/15 bg-black/55 px-2 py-1 text-[10px] uppercase tracking-[0.18em] text-nova-gold/80">
              {desktopControlEnabled ? "Desktop" : "Nova"}
            </div>
          </div>
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
        onClose={onCloseCameraOverlay}
      >
        <CameraSheet
          stream={camera.stream}
          status={camera.status}
          gesturesOn={gesturesOn}
          onToggleGestures={onToggleGestures}
          handStatus={hand?.status}
          handsDetected={hand?.handsDetected}
          pinchHoldMs={hand?.pinch?.holdMs}
          pinchStrength={hand?.pinch?.strength}
          rawCursor={hand?.rawCursor}
          rawPinchRatio={hand?.telemetry?.pinchRatio}
          calibrationSession={calibrationSession}
          calibrationProfile={handCalibration}
          currentCalibrationStep={currentCalibrationStep}
          onStartCalibration={startHandCalibration}
          onCaptureCalibrationStep={captureHandCalibrationStep}
          onCancelCalibration={cancelHandCalibration}
          onResetCalibration={resetHandCalibration}
          desktopControlEnabled={desktopControlEnabled}
          desktopControlAvailable={desktopControlAvailable}
          onToggleDesktopControl={onToggleDesktopControl}
        />
      </OverlayHost>

      <OverlayHost
        open={activeOverlay === "maps"}
        title="Maps & Directions"
        onClose={() => setActiveOverlay(null)}
      >
        <MapsSheet
          routePreload={mapsRoutePreload}
          currentLocation={currentLocation}
          locationStatus={locationStatus}
          locationNote={locationNote}
          onRequestCurrentLocation={requestCurrentLocation}
          onManualLocationSet={setManualLocation}
        />
      </OverlayHost>

      <OverlayHost
        open={activeOverlay === "web"}
        title="Web Search"
        onClose={() => setActiveOverlay(null)}
      >
        <WebSheet />
      </OverlayHost>

      <OverlayHost
        open={activeOverlay === "memory"}
        title="Memory"
        onClose={() => setActiveOverlay(null)}
      >
        <MemorySheet liveEvents={novaEvents} />
      </OverlayHost>

      <OverlayHost
        open={activeOverlay === "tasks"}
        title="Tasks"
        onClose={() => setActiveOverlay(null)}
      >
        <TasksSheet liveEvents={novaEvents} />
      </OverlayHost>

      <OverlayHost
        open={activeOverlay === "system"}
        title="System"
        onClose={() => setActiveOverlay(null)}
      >
        <SystemSheet liveEvents={novaEvents} eventsConnected={eventsConnected} />
      </OverlayHost>

      <OverlayHost
        open={activeOverlay === "improvements"}
        title="Self-Improvement"
        onClose={() => setActiveOverlay(null)}
      >
        <ImprovementsSheet />
      </OverlayHost>

      <OverlayHost
        open={activeOverlay === "screenvision"}
        title="Screen Vision"
        onClose={() => setActiveOverlay(null)}
      >
        <ScreenVisionSheet />
      </OverlayHost>

      <OverlayHost
        open={activeOverlay === "graph"}
        title="Knowledge Graph"
        onClose={() => setActiveOverlay(null)}
      >
        <GraphSheet />
      </OverlayHost>
    </div>
  );
}
