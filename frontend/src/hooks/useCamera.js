import { useCallback, useEffect, useRef, useState } from "react";

export default function useCamera() {
  const [enabled, setEnabled] = useState(false);
  const [status, setStatus] = useState("off"); // off|starting|on|error
  const streamRef = useRef(null);

  const start = useCallback(async () => {
    setStatus("starting");
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ video: true, audio: false });
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
