import { useEffect, useRef } from "react";

// PI1: opt-in periodic screen glances. Only runs while `enabled` is true —
// the toggle itself IS the explicit action; nothing captures before the user
// turns this on, and turning it off (one click) stops it immediately.
const INTERVAL_MS = 10 * 60 * 1000; // ~10 min, matches the plan's cadence
const NOTABLE_QUESTION =
  "Is there anything here that looks like the user is stuck (a visible error, a stuck/hung state, a crash) " +
  "or has clearly changed since a normal working state? If nothing stands out, reply with exactly: NOTHING_NOTABLE. " +
  "Otherwise briefly describe what looks notable.";

async function dataUrlToBlob(dataUrl) {
  const res = await fetch(dataUrl);
  return res.blob();
}

export function useFocusSession({ enabled, apiBase, onNotable }) {
  const onNotableRef = useRef(onNotable);
  onNotableRef.current = onNotable;

  useEffect(() => {
    if (!enabled) return undefined;
    if (typeof window === "undefined" || !window.novaDesktop?.captureScreen) return undefined;

    let cancelled = false;

    async function glance() {
      try {
        const capture = await window.novaDesktop.captureScreen();
        if (cancelled || !capture?.ok || !capture?.dataUrl) return;

        const blob = await dataUrlToBlob(capture.dataUrl);
        const fd = new FormData();
        fd.append("file", blob, "screen.png");

        const resp = await fetch(`${apiBase}/vision/analyze?question=${encodeURIComponent(NOTABLE_QUESTION)}`, {
          method: "POST",
          body: fd,
        });
        if (cancelled || !resp.ok) return;
        const data = await resp.json();
        const text = String(data?.text || "").trim();
        if (!text || text.toUpperCase().startsWith("NOTHING_NOTABLE")) return;
        onNotableRef.current?.(text);
      } catch {
        // Best-effort; a failed glance just waits for the next interval.
      }
    }

    const id = setInterval(glance, INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [enabled, apiBase]);
}

export default useFocusSession;
