import React, { useMemo } from "react";

function Meter({ level = 0, bars = 32 }) {
  const arr = useMemo(() => Array.from({ length: bars }, (_, i) => i), [bars]);
  return (
    <div className="flex items-end gap-[2px] h-4">
      {arr.map((i) => {
        const t = (i / (bars - 1)) * Math.PI;
        const shape = Math.sin(t); // bell
        const h = 3 + Math.round(13 * Math.min(1, level * (0.35 + shape)));
        return (
          <div
            key={i}
            className="w-[3px] rounded-sm bg-cyan-300/80"
            style={{ height: `${h}px`, opacity: 0.35 + 0.65 * shape }}
          />
        );
      })}
    </div>
  );
}

export default function TopBar({
  version = "v2",
  project = "PROJECT: TEMP",
  micMuted = false,
  micLevel = 0,
  timeText = "",
}) {
  return (
    <div className="fixed top-0 left-0 right-0 z-30 h-12 flex items-center px-4 border-b border-white/5 bg-black/35 backdrop-blur-xl">
      <div className="flex items-center gap-3">
        <div className="text-cyan-200 font-semibold tracking-[0.35em] text-sm">NOVA</div>
        <div className="text-[10px] text-white/40 border border-white/10 rounded-md px-2 py-0.5">
          {version}
        </div>
      </div>

      <div className="flex-1 flex justify-center">
        <div className="flex items-center gap-2">
          <Meter level={micMuted ? 0 : micLevel} />
          <div className="text-[10px] text-white/45 uppercase tracking-widest">
            {micMuted ? "Muted" : "Listening"}
          </div>
        </div>
      </div>

      <div className="flex items-center gap-3">
        <div className="text-[11px] text-white/45">{timeText}</div>
        <div className="text-[10px] text-white/40 border border-white/10 rounded-md px-2 py-0.5">
          {project}
        </div>
      </div>
    </div>
  );
}
