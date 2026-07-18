import { useEffect, useMemo, useRef, useState } from "react";

import { FilesetResolver, HandLandmarker } from "@mediapipe/tasks-vision";

function isElectronRuntime() {
  try {
    if (window?.process?.versions?.electron) return true;
  } catch {}
  try {
    return String(navigator?.userAgent || "").toLowerCase().includes(" electron/");
  } catch {}
  return false;
}

function isWindowsRuntime() {
  try {
    return String(window?.novaDesktop?.platform || navigator?.platform || "").toLowerCase().includes("win");
  } catch {}
  return false;
}

function clamp01(v) {
  if (Number.isNaN(v)) return 0;
  return Math.min(1, Math.max(0, v));
}

function dist(a, b) {
  const dx = (a?.x ?? 0) - (b?.x ?? 0);
  const dy = (a?.y ?? 0) - (b?.y ?? 0);
  return Math.hypot(dx, dy);
}

function lerp(a, b, t) {
  return a + (b - a) * t;
}

function normalizeRange(value, min, max) {
  const lo = Number(min);
  const hi = Number(max);
  if (!Number.isFinite(lo) || !Number.isFinite(hi) || Math.abs(hi - lo) < 1e-4) {
    return clamp01(value);
  }
  return clamp01((value - lo) / (hi - lo));
}

function resolveWasmBase() {
  try {
    const fromEnv = import.meta?.env?.VITE_MEDIAPIPE_WASM_BASE;
    if (fromEnv) return String(fromEnv).replace(/\/$/, "");
  } catch {}
  return "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.21/wasm";
}

function resolveModelPath() {
  // You can override this in `.env` if you later choose to ship the model locally.
  try {
    const fromEnv = import.meta?.env?.VITE_MEDIAPIPE_HAND_MODEL_PATH;
    if (fromEnv) return String(fromEnv);
  } catch {}
  return "https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task";
}

function resolveDelegate() {
  try {
    const fromEnv = import.meta?.env?.VITE_MEDIAPIPE_HAND_DELEGATE;
    if (fromEnv) return String(fromEnv).toUpperCase() === "CPU" ? "CPU" : "GPU";
  } catch {}
  return isElectronRuntime() && isWindowsRuntime() ? "CPU" : "GPU";
}

async function createHandLandmarker(vision, modelPath, delegate) {
  const create = (nextDelegate) => HandLandmarker.createFromOptions(vision, {
    baseOptions: {
      modelAssetPath: modelPath,
      delegate: nextDelegate,
    },
    runningMode: "VIDEO",
    numHands: 1,
  });

  try {
    return await create(delegate);
  } catch (error) {
    if (delegate === "GPU") {
      return await create("CPU");
    }
    throw error;
  }
}

export default function useHandTracking({ enabled, stream, calibration = null }) {
  const [status, setStatus] = useState("off"); // off|loading|ready|no_hands|error
  const [handsDetected, setHandsDetected] = useState(0);

  const [cursor, setCursor] = useState({ x: 0, y: 0, visible: false });
  const [pinch, setPinch] = useState({ down: false, justPressed: false, justReleased: false, holdMs: 0, strength: 0 });

  const videoRef = useRef(null);
  const rafRef = useRef(0);
  const landmarkerRef = useRef(null);
  const abortRef = useRef(false);
  const filteredCursorRef = useRef({ x: 0.5, y: 0.5, visible: false });
  const rawCursorRef = useRef({ x: 0.5, y: 0.5, visible: false });
  const pinchRatioRef = useRef(1);
  const pinchDownAtRef = useRef(0);
  const lastSampleAtRef = useRef(0);

  const config = useMemo(
    () => ({ wasmBase: resolveWasmBase(), modelPath: resolveModelPath(), delegate: resolveDelegate() }),
    []
  );

  useEffect(() => {
    abortRef.current = false;
    return () => {
      abortRef.current = true;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;

    const stopLoop = () => {
      try {
        if (rafRef.current) cancelAnimationFrame(rafRef.current);
      } catch {}
      rafRef.current = 0;
    };

    const teardown = () => {
      stopLoop();
      try {
        const v = videoRef.current;
        if (v) {
          try {
            v.pause();
          } catch {}
          try {
            v.srcObject = null;
          } catch {}
        }
      } catch {}

      try {
        landmarkerRef.current?.close?.();
      } catch {}
      landmarkerRef.current = null;

      setHandsDetected(0);
      setCursor({ x: 0, y: 0, visible: false });
      filteredCursorRef.current = { x: 0.5, y: 0.5, visible: false };
      rawCursorRef.current = { x: 0.5, y: 0.5, visible: false };
      pinchRatioRef.current = 1;
      pinchDownAtRef.current = 0;
      lastSampleAtRef.current = 0;
      setPinch({ down: false, justPressed: false, justReleased: false, holdMs: 0, strength: 0 });
    };

    if (!enabled || !stream) {
      setStatus("off");
      teardown();
      return () => {};
    }

    (async () => {
      setStatus("loading");

      try {
        if (!videoRef.current) {
          const v = document.createElement("video");
          v.muted = true;
          v.playsInline = true;
          v.autoplay = true;
          videoRef.current = v;
        }

        const v = videoRef.current;
        try {
          v.srcObject = stream;
        } catch {
          setStatus("error");
          return;
        }

        try {
          await v.play();
        } catch {
          // In some environments autoplay can be blocked.
          setStatus("error");
          return;
        }

        if (!landmarkerRef.current) {
          const vision = await FilesetResolver.forVisionTasks(config.wasmBase);
          const handLandmarker = await createHandLandmarker(vision, config.modelPath, config.delegate);
          landmarkerRef.current = handLandmarker;
        }

        if (cancelled || abortRef.current) return;

        let lastPinchDown = false;
        let lastPressAt = 0;

        const loop = () => {
          if (cancelled || abortRef.current) return;

          const lm = landmarkerRef.current;
          const video = videoRef.current;
          if (!lm || !video) {
            setStatus("loading");
            rafRef.current = requestAnimationFrame(loop);
            return;
          }

          const frameTs = performance.now();

          let results;
          try {
            results = lm.detectForVideo(video, frameTs);
          } catch {
            setStatus("error");
            rafRef.current = requestAnimationFrame(loop);
            return;
          }

          const landmarks = results?.landmarks?.[0] || null;
          if (!landmarks || landmarks.length < 9) {
            setHandsDetected(0);
            setCursor((c) => (c.visible ? { ...c, visible: false } : c));
            rawCursorRef.current = { x: 0.5, y: 0.5, visible: false };
            setStatus("no_hands");
            if (lastPinchDown) {
              lastPinchDown = false;
              pinchDownAtRef.current = 0;
              setPinch({ down: false, justPressed: false, justReleased: true, holdMs: 0, strength: 0 });
            } else {
              setPinch((p) => (p.justPressed || p.justReleased ? { ...p, justPressed: false, justReleased: false } : p));
            }
            rafRef.current = requestAnimationFrame(loop);
            return;
          }

          setHandsDetected(1);
          setStatus("ready");

          const indexTip = landmarks[8];
          const indexPip = landmarks[6] || landmarks[5] || landmarks[8];
          const thumbTip = landmarks[4];
          const wrist = landmarks[0];
          const midMcp = landmarks[9] || landmarks[5];

          const baseX = clamp01(((indexTip?.x ?? 0.5) * 0.78) + ((indexPip?.x ?? 0.5) * 0.22));
          const rawY = clamp01(((indexTip?.y ?? 0.5) * 0.78) + ((indexPip?.y ?? 0.5) * 0.22));
          const mirrorX = calibration?.mirrorX ?? true;
          const rawX = mirrorX ? clamp01(1 - baseX) : baseX;
          rawCursorRef.current = { x: rawX, y: rawY, visible: true };

          const mappedX = calibration?.cursorBounds
            ? normalizeRange(rawX, calibration.cursorBounds.left, calibration.cursorBounds.right)
            : rawX;
          const mappedY = calibration?.cursorBounds
            ? normalizeRange(rawY, calibration.cursorBounds.top, calibration.cursorBounds.bottom)
            : rawY;

          const prevCursor = filteredCursorRef.current;
          const delta = Math.hypot(mappedX - prevCursor.x, mappedY - prevCursor.y);
          const dt = Math.max(1 / 120, (frameTs - (lastSampleAtRef.current || frameTs)) / 1000);
          lastSampleAtRef.current = frameTs;
          const speed = delta / Math.max(dt, 1e-3);
          const deadZone = lastPinchDown ? 0.0012 : 0.0018;
          const alpha = !prevCursor.visible
            ? 1
            : Math.min(
                0.7,
                Math.max(
                  lastPinchDown ? 0.1 : 0.12,
                  (lastPinchDown ? 0.08 : 0.1) + Math.min(0.5, speed * 0.012)
                )
              );

          const x = clamp01(delta < deadZone ? prevCursor.x : lerp(prevCursor.x, mappedX, alpha));
          const y = clamp01(delta < deadZone ? prevCursor.y : lerp(prevCursor.y, mappedY, alpha));

          filteredCursorRef.current = { x, y, visible: true };

          setCursor({ x, y, visible: true });

          const scale = Math.max(1e-6, dist(wrist, midMcp));
          const rawPinchRatio = dist(indexTip, thumbTip) / scale;
          const pinchRatio = pinchRatioRef.current * 0.72 + rawPinchRatio * 0.28;
          pinchRatioRef.current = pinchRatio;

          const openPinch = Number(calibration?.pinch?.open);
          const closedPinch = Number(calibration?.pinch?.closed);
          const pressThreshold = Number.isFinite(Number(calibration?.pinch?.pressThreshold))
            ? Number(calibration?.pinch?.pressThreshold)
            : 0.34;
          const releaseThreshold = Number.isFinite(Number(calibration?.pinch?.releaseThreshold))
            ? Number(calibration?.pinch?.releaseThreshold)
            : 0.5;
          const pinchStrength = Number.isFinite(openPinch) && Number.isFinite(closedPinch) && openPinch > closedPinch
            ? clamp01((openPinch - pinchRatio) / Math.max(1e-4, openPinch - closedPinch))
            : clamp01((0.65 - pinchRatio) / 0.35);

          // Wider hysteresis keeps drag locked during small finger jitter.
          const pinchDown = lastPinchDown ? pinchRatio < releaseThreshold : pinchRatio < pressThreshold;

          const now = Date.now();
          const canPress = now - lastPressAt > 350;

          const justPressed = !lastPinchDown && pinchDown && canPress;
          const justReleased = lastPinchDown && !pinchDown;

          if (justPressed) {
            lastPressAt = now;
            pinchDownAtRef.current = now;
          }
          if (justReleased) {
            pinchDownAtRef.current = 0;
          }
          lastPinchDown = pinchDown;

          setPinch({
            down: pinchDown,
            justPressed,
            justReleased,
            holdMs: pinchDown && pinchDownAtRef.current ? now - pinchDownAtRef.current : 0,
            strength: pinchStrength,
          });

          rafRef.current = requestAnimationFrame(loop);
        };

        rafRef.current = requestAnimationFrame(loop);
      } catch {
        setStatus("error");
      }
    })();

    return () => {
      cancelled = true;
      teardown();
    };
  }, [enabled, stream, config.wasmBase, config.modelPath, config.delegate, calibration]);

  return {
    status,
    handsDetected,
    cursor, // normalized 0..1
    pinch,
    rawCursor: rawCursorRef.current?.visible
      ? { x: rawCursorRef.current.x, y: rawCursorRef.current.y, visible: rawCursorRef.current.visible }
      : { x: 0, y: 0, visible: false },
    telemetry: {
      pinchRatio: pinchRatioRef.current,
      calibrationApplied: Boolean(calibration?.cursorBounds || calibration?.pinch),
    },
  };
}
