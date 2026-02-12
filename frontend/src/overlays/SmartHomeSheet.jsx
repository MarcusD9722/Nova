import React from "react";

export default function SmartHomeSheet() {
  return (
    <div className="space-y-3 text-white/80">
      <div className="text-sm text-white">Smart Home</div>
      <div className="text-xs text-white/60">
        Placeholder. Wire to your smart-home plugin endpoints (connect, list devices, toggle).
      </div>
      <div className="rounded-2xl border border-white/10 bg-white/5 p-3 text-xs text-white/60">
        Recommended: Kasa, Home Assistant, or Matter bridge with a normalized device schema.
      </div>
    </div>
  );
}
