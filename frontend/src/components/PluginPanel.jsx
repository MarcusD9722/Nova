import React from "react";

export default function PluginPanel({ children }) {
  return (
    <div
      className="relative flex flex-col w-full h-full min-w-0 min-h-0
      bg-gradient-to-br from-[#14152f]/85 via-[#192040]/90 to-[#2c1139]/80
      backdrop-blur-2xl border border-fuchsia-500/30 rounded-3xl
      shadow-[0_8px_32px_0_rgba(240,36,242,0.19)]
      before:absolute before:inset-0 before:rounded-3xl before:opacity-70 before:pointer-events-none
      before:bg-gradient-to-br before:from-fuchsia-500/15 before:to-cyan-300/10
      after:absolute after:inset-0 after:rounded-3xl after:pointer-events-none after:ring-2 after:ring-fuchsia-400/20
      "
      style={{ boxSizing: "border-box" }}
    >
      {/* Neon top border */}
      <div className="absolute top-0 left-1/2 -translate-x-1/2 w-4/5 h-1.5 bg-gradient-to-r from-fuchsia-400/40 via-cyan-400/20 to-fuchsia-400/40 blur-lg opacity-80 rounded-b-3xl pointer-events-none" />
      <div className="flex-1 p-3">
        {children}
      </div>
    </div>
  );
}
