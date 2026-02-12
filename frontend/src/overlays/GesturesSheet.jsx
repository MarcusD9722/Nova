import React from "react";

export default function GesturesSheet({ enabled, status, tracker }) {
  return (
    <div className="space-y-3 text-white/80">
      <div className="text-xs text-white/60">Gestures: {enabled ? "Enabled" : "Disabled"} • Status: {status}</div>
      <div className="text-xs text-white/60">
        Tracker: {tracker?.status ?? "off"}
        {typeof tracker?.handsDetected === "number" ? ` • Hands: ${tracker.handsDetected}` : ""}
        {tracker?.pinch?.down ? " • Pinch: down" : ""}
      </div>
      <div className="rounded-2xl border border-white/10 bg-white/5 p-3 text-xs text-white/60">
        Pinch (thumb + index) to click at the on-screen cursor.
      </div>
    </div>
  );
}
