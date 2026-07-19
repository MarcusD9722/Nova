import { useCallback, useEffect, useRef, useState } from "react";

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

function preferredVideoConstraints() {
  if (isElectronRuntime() && isWindowsRuntime()) {
    return {
      facingMode: "user",
      width: { ideal: 640, max: 960 },
      height: { ideal: 360, max: 540 },
      frameRate: { ideal: 24, max: 30 },
    };
  }

  return {
    facingMode: "user",
    width: { ideal: 960, max: 1280 },
    height: { ideal: 540, max: 720 },
    frameRate: { ideal: 30, max: 30 },
  };
}

export default function useCamera() {
  const [enabled, setEnabled] = useState(false);
  const [status, setStatus] = useState("off"); // off|starting|on|error
  const streamRef = useRef(null);

  const start = useCallback(async () => {
    setStatus("starting");
    try {
      const attempts = [
        { video: preferredVideoConstraints(), audio: false },
        {
          video: {
            facingMode: "user",
            width: { ideal: 640, max: 640 },
            height: { ideal: 480, max: 480 },
            frameRate: { ideal: 24, max: 30 },
          },
          audio: false,
        },
        { video: true, audio: false },
      ];

      let stream = null;
      let lastError = null;
      for (const constraints of attempts) {
        try {
          stream = await navigator.mediaDevices.getUserMedia(constraints);
          break;
        } catch (error) {
          lastError = error;
        }
      }
      if (!stream) throw lastError || new Error("camera_unavailable");

      try {
        const track = stream.getVideoTracks?.()[0];
        if (track) {
          track.contentHint = "motion";
        }
      } catch {}
      streamRef.current = stream;
      setEnabled(true);
      setStatus("on");
      return stream;
    } catch (e) {
      setEnabled(false);
      setStatus("error");
      return null;
    }
  }, []);

  const stop = useCallback(() => {
    const s = streamRef.current;
    if (s) {
      try { s.getTracks().forEach(t => t.stop()); } catch {}
    }
    streamRef.current = null;
    setEnabled(false);
    setStatus("off");
  }, []);

  // cleanup on unmount
  useEffect(() => () => stop(), [stop]);

  return { enabled, status, stream: streamRef.current, start, stop, setEnabled };
}
