import React, { useEffect, useMemo, useState } from "react";
import { Brain, Database, Search, User, CalendarDays, RefreshCw } from "lucide-react";

function apiUrl(path) {
  try {
    const base = window.__NOVA_API_BASE || "http://localhost:8008";
    return `${base}${path}`;
  } catch {
    return `http://localhost:8008${path}`;
  }
}

const KIND_META = {
  fact: { icon: Database, label: "Fact" },
  person: { icon: User, label: "Person" },
  event: { icon: CalendarDays, label: "Event" },
  turn: { icon: Brain, label: "Turn" },
};

function KindChip({ kind }) {
  const meta = KIND_META[kind] || { icon: Brain, label: kind || "?" };
  const Icon = meta.icon;
  return (
    <span className="inline-flex items-center gap-1 rounded-full border border-nova-gold/20 bg-black/30 px-2 py-0.5 text-[10px] uppercase tracking-[0.14em] text-nova-gold/70">
      <Icon size={10} />
      {meta.label}
    </span>
  );
}

export default function MemorySheet({ liveEvents = [] }) {
  const [items, setItems] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const [searchResults, setSearchResults] = useState(null);
  const [searching, setSearching] = useState(false);

  const loadRecent = async () => {
    setLoading(true);
    setError("");
    try {
      const res = await fetch(apiUrl("/memory/recent?limit=50"));
      if (!res.ok) throw new Error(await res.text());
      const data = await res.json();
      setItems(Array.isArray(data?.items) ? data.items : []);
    } catch (err) {
      setError(String(err?.message || err));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadRecent();
  }, []);

  async function handleSearch(e) {
    e?.preventDefault();
    const q = query.trim();
    if (!q) {
      setSearchResults(null);
      return;
    }
    setSearching(true);
    setError("");
    try {
      const res = await fetch(apiUrl(`/memory/search?q=${encodeURIComponent(q)}`));
      if (!res.ok) throw new Error(await res.text());
      const data = await res.json();
      setSearchResults(Array.isArray(data?.results) ? data.results : []);
    } catch (err) {
      setError(String(err?.message || err));
    } finally {
      setSearching(false);
    }
  }

  const memoryActivity = useMemo(
    () =>
      liveEvents
        .filter((ev) => String(ev?.type || "").startsWith("memory.") || String(ev?.type || "").startsWith("task."))
        .slice(-12)
        .reverse(),
    [liveEvents]
  );

  return (
    <div className="space-y-3 text-nova-gold h-full flex flex-col">
      <form onSubmit={handleSearch} className="flex gap-2">
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          className="flex-1 rounded-xl px-3 py-2 bg-black/25 border border-nova-gold/15 text-nova-gold outline-none placeholder:text-nova-gold/45 text-sm"
          placeholder="Search Nova's memory…"
        />
        <button
          type="submit"
          disabled={searching}
          className="rounded-xl px-4 py-2 bg-black/25 border border-nova-gold/30 text-nova-gold hover:bg-black/35 text-sm disabled:opacity-50 inline-flex items-center gap-2"
        >
          <Search size={14} />
          {searching ? "…" : "Search"}
        </button>
        <button
          type="button"
          onClick={loadRecent}
          title="Refresh"
          className="rounded-xl px-3 py-2 bg-black/25 border border-nova-gold/20 text-nova-gold/70 hover:text-nova-gold hover:bg-black/35 text-sm"
        >
          <RefreshCw size={14} className={loading ? "animate-spin" : ""} />
        </button>
      </form>

      {error && (
        <div className="rounded-xl border border-red-400/20 bg-red-500/10 px-3 py-2 text-xs text-red-200">{error}</div>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-[minmax(0,1fr)_260px] gap-3 flex-1 min-h-0">
        <div className="min-h-0 overflow-y-auto rounded-2xl border border-nova-gold/10 bg-black/20 p-3 space-y-2">
          <div className="text-[10px] uppercase tracking-[0.24em] text-nova-gold/50 pb-1">
            {searchResults ? `Search results (${searchResults.length})` : "What Nova remembers"}
          </div>

          {searchResults ? (
            searchResults.length ? (
              searchResults.map((hit) => (
                <div key={hit.id} className="rounded-xl border border-nova-gold/10 bg-black/25 px-3 py-2">
                  <div className="flex items-center gap-2">
                    <KindChip kind={hit.kind} />
                    <span className="text-[10px] text-nova-gold/40">score {Number(hit.score || 0).toFixed(2)}</span>
                  </div>
                  <div className="mt-1 text-sm text-nova-gold/90 break-words">{hit.text}</div>
                </div>
              ))
            ) : (
              <div className="text-sm text-nova-gold/50 py-6 text-center">No memories matched that search.</div>
            )
          ) : items === null ? (
            <div className="text-sm text-nova-gold/50 py-6 text-center">{loading ? "Loading memory…" : "Memory unavailable."}</div>
          ) : items.length ? (
            items.map((item) => (
              <div key={item.id} className="rounded-xl border border-nova-gold/10 bg-black/25 px-3 py-2">
                <div className="flex items-center gap-2">
                  <KindChip kind={item.kind} />
                  <span className="text-[10px] text-nova-gold/40">
                    {item.created_at ? new Date(item.created_at).toLocaleString() : ""}
                  </span>
                </div>
                <div className="mt-1 text-sm text-nova-gold/90 break-words">{item.text}</div>
              </div>
            ))
          ) : (
            <div className="text-sm text-nova-gold/50 py-6 text-center">
              Nova has no long-term memories yet. Tell her about yourself in chat.
            </div>
          )}
        </div>

        <div className="hidden lg:flex min-h-0 flex-col rounded-2xl border border-nova-gold/10 bg-black/20 p-3">
          <div className="text-[10px] uppercase tracking-[0.24em] text-nova-gold/50 pb-2">Live memory activity</div>
          <div className="flex-1 min-h-0 overflow-y-auto space-y-1.5">
            {memoryActivity.length ? (
              memoryActivity.map((ev) => (
                <div key={ev.seq} className="rounded-lg border border-nova-purple/20 bg-nova-purple/10 px-2 py-1.5">
                  <div className="text-[10px] uppercase tracking-[0.16em] text-nova-gold/60">{ev.type}</div>
                  <div className="text-xs text-nova-gold/80 break-words">
                    {ev?.data?.value || ev?.data?.query || ev?.data?.title || ev?.data?.kind || ""}
                  </div>
                </div>
              ))
            ) : (
              <div className="text-xs text-nova-gold/40 pt-4 text-center">
                Memory reads/writes will appear here live.
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
