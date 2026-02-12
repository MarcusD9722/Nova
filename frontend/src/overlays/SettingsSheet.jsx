import React from "react";

export default function SettingsSheet() {
  return (
    <div className="space-y-4 text-white/80">
      <div className="text-sm text-white">Settings</div>
      <div className="text-xs text-white/60">
        This sheet is wired into the fixed-layout shell. Next step is to connect these controls to your backend config
        (model selection, voice, hotkeys, API base, etc.).
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        <div className="rounded-2xl border border-white/10 bg-white/5 p-3">
          <div className="text-[11px] uppercase tracking-widest text-cyan-200/70">Voice</div>
          <div className="mt-2 text-xs text-white/60">Wake word, mic device, STT/TTS engine, voice id/name.</div>
        </div>
        <div className="rounded-2xl border border-white/10 bg-white/5 p-3">
          <div className="text-[11px] uppercase tracking-widest text-cyan-200/70">Model</div>
          <div className="mt-2 text-xs text-white/60">Current model, context size, temperature, GPU layers.</div>
        </div>
      </div>
    </div>
  );
}
