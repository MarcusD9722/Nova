import { useEffect, useMemo, useRef, useState } from "react";

import { FilesetResolver, HandLandmarker } from "@mediapipe/tasks-vision";

function clamp01(v) {
  if (Number.isNaN(v)) return 0;
  return Math.min(1, Math.max(0, v));
}

function dist(a, b) {
  const dx = (a?.x ?? 0) - (b?.x ?? 0);
  const dy = (a?.y ?? 0) - (b?.y ?? 0);
  return Math.hypot(dx, dy);
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

export default function useHandTracking({ enabled, stream }) {
  const [status, setStatus] = useState("off"); // off|loading|ready|no_hands|error
  const [handsDetected, setHandsDetected] = useState(0);

  const [cursor, setCursor] = useState({ x: 0, y: 0, visible: false });
  const [pinch, setPinch] = useState({ down: false, justPressed: false, justReleased: false });

  const videoRef = useRef(null);
  const rafRef = useRef(0);
  const landmarkerRef = useRef(null);
  const abortRef = useRef(false);

  const config = useMemo(
    () => ({ wasmBase: resolveWasmBase(), modelPath: resolveModelPath() }),
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
      setPinch({ down: false, justPressed: false, justReleased: false });
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
          const handLandmarker = await HandLandmarker.createFromOptions(vision, {
            baseOptions: {
              modelAssetPath: config.modelPath,
              delegate: "GPU",
            },
            runningMode: "VIDEO",
            numHands: 1,
          });
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

          let results;
          try {
            results = lm.detectForVideo(video, performance.now());
          } catch {
            setStatus("error");
            rafRef.current = requestAnimationFrame(loop);
            return;
          }

          const landmarks = results?.landmarks?.[0] || null;
          if (!landmarks || landmarks.length < 9) {
            setHandsDetected(0);
            setCursor((c) => (c.visible ? { ...c, visible: false } : c));
            setStatus("no_hands");
            if (lastPinchDown) {
              lastPinchDown = false;
              setPinch({ down: false, justPressed: false, justReleased: true });
            } else {
              setPinch((p) => (p.justPressed || p.justReleased ? { ...p, justPressed: false, justReleased: false } : p));
            }
            rafRef.current = requestAnimationFrame(loop);
            return;
          }

          setHandsDetected(1);
          setStatus("ready");

          const indexTip = landmarks[8];
          const thumbTip = landmarks[4];
          const wrist = landmarks[0];
          const midMcp = landmarks[9] || landmarks[5];

          const x = clamp01(indexTip?.x ?? 0.5);
          const y = clamp01(indexTip?.y ?? 0.5);

          setCursor({ x, y, visible: true });

          const scale = Math.max(1e-6, dist(wrist, midMcp));
          const pinchRatio = dist(indexTip, thumbTip) / scale;

          // Simple hysteresis + cooldown to avoid chatter.
          const pinchDown = lastPinchDown ? pinchRatio < 0.45 : pinchRatio < 0.35;

          const now = Date.now();
          const canPress = now - lastPressAt > 350;

          const justPressed = !lastPinchDown && pinchDown && canPress;
          const justReleased = lastPinchDown && !pinchDown;

          if (justPressed) lastPressAt = now;
          lastPinchDown = pinchDown;

          setPinch({ down: pinchDown, justPressed, justReleased });

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
  }, [enabled, stream, config.wasmBase, config.modelPath]);

  return {
    status,
    handsDetected,
    cursor, // normalized 0..1
    pinch,
  };
}
