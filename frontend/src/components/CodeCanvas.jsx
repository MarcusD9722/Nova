import React, { useState } from "react";

export default function CodeCanvas({ code: codeProp = "", onChange, language = "python", onRun }) {
  const [code, setCode] = useState(codeProp);

  // Fallback if no onRun given
  const _onRun =
    onRun ||
    (() => alert("Run/Save clicked! (No onRun prop passed to CodeCanvas)"));

  // Always sync local state and parent
  const handleCodeChange = (v) => {
    setCode(v);
    onChange?.(v);
  };

  return (
    <div
      className="relative flex flex-col w-full h-full min-w-0 min-h-0
      bg-gradient-to-br from-[#161f34]/80 via-[#1b2247]/90 to-[#101422]/95
      backdrop-blur-2xl border border-purple-600/30 rounded-3xl
      shadow-[0_8px_32px_0_rgba(59,23,109,0.29)]
      before:absolute before:inset-0 before:rounded-3xl before:opacity-70 before:pointer-events-none
      before:bg-gradient-to-tr before:from-purple-500/20 before:to-cyan-300/10
      after:absolute after:inset-0 after:rounded-3xl after:pointer-events-none after:ring-2 after:ring-purple-400/20"
      style={{ boxSizing: "border-box" }}
    >
      {/* Glowing label */}
      <label className="text-xs font-bold mb-1 mt-2 ml-2 text-cyan-300 drop-shadow-glow uppercase tracking-wider">
        Live Code Editor
      </label>
      <textarea
        className="flex-1 resize-none w-full h-full rounded-2xl bg-zinc-900/70 text-green-300 font-mono p-2
        border border-purple-400/20 shadow-[0_2px_16px_0_rgba(126,56,255,0.13)]
        focus:ring-2 focus:ring-fuchsia-400/30 transition"
        value={code}
        onChange={e => handleCodeChange(e.target.value)}
        spellCheck={false}
        style={{ minHeight: 0, minWidth: 0, boxSizing: "border-box" }}
      />
      <div className="flex justify-between items-center px-2 pb-2 mt-1">
        <span className="text-xs opacity-70 text-purple-300">Language: {language}</span>
        <button
          type="button"
          onClick={() => _onRun(code)}
          className="ml-2 rounded-xl px-4 py-1.5 bg-gradient-to-br from-purple-500 to-cyan-500 text-white shadow-lg
            hover:scale-105 hover:bg-fuchsia-600/90 transition-all duration-100 font-bold"
        >
          Run/Save
        </button>
      </div>
    </div>
  );
}
