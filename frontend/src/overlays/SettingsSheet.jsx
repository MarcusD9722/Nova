import React from "react";

export default function SettingsSheet() {
  return (
    <div className="space-y-4 text-nova-gold">
      <div className="text-sm">Settings</div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        <div className="rounded-2xl border border-nova-gold/15 bg-black/20 p-3">
          <div className="text-[11px] uppercase tracking-widest text-nova-gold/70">Voice</div>
          <div className="mt-2 text-xs text-nova-gold/70">Wake word, mic device, STT/TTS engine, voice id/name.</div>
        </div>
        <div className="rounded-2xl border border-nova-gold/15 bg-black/20 p-3">
          <div className="text-[11px] uppercase tracking-widest text-nova-gold/70">Model</div>
          <div className="mt-2 text-xs text-nova-gold/70">Current model, context size, temperature, GPU layers.</div>
        </div>
      </div>
    </div>
  );
}
