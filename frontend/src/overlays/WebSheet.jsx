import React, { useState } from "react";

export default function WebSheet({ onSearch }) {
  const [q, setQ] = useState("");
  return (
    <div className="space-y-3 text-white/80">
      <div className="text-sm text-white">Web Search</div>
      <div className="flex gap-2">
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          className="flex-1 rounded-xl px-3 py-2 bg-black/30 border border-white/10 text-white/80 outline-none"
          placeholder="Search…"
        />
        <button
          className="rounded-xl px-4 py-2 bg-gradient-to-br from-cyan-500 to-fuchsia-500 text-white"
          onClick={() => onSearch?.(q)}
          type="button"
        >
          Search
        </button>
      </div>
      <div className="text-xs text-white/60">
        Next step: call a backend search plugin and render results with “Send to Nova” context injection.
      </div>
    </div>
  );
}
