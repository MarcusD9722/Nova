import React, { useCallback, useEffect, useState } from "react";
import { Share2, Clock3, Search, User, FolderGit2, FileText, CalendarDays, Bell, MessageSquare, Database } from "lucide-react";

// Knowledge-graph explorer + timeline (Phase 1.5 of docs/ROADMAP.md).
// Deliberately a NAVIGABLE LIST explorer, not a canvas force-graph: at this
// scale, clickable neighbor chips are more usable than a physics toy, and it
// keeps the bundle dependency-free. A visual graph can layer on later.

function apiUrl(path) {
  try {
    const base = window.__NOVA_API_BASE || "http://localhost:8008";
    return `${base}${path}`;
  } catch {
    return `http://localhost:8008${path}`;
  }
}

async function getJson(path) {
  const res = await fetch(apiUrl(path));
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw Object.assign(new Error(body?.detail || res.statusText), { status: res.status });
  return body;
}

const KIND_ICONS = { person: User, project: FolderGit2, document: FileText, topic: Database };
const TIMELINE_ICONS = { event: CalendarDays, conversation: MessageSquare, reminder: Bell, fact: Database };

function KindChip({ kind, keyName, meta, onClick }) {
  const Icon = KIND_ICONS[kind] || Database;
  return (
    <button
      type="button"
      onClick={onClick}
      title={`Explore ${keyName}`}
      className="inline-flex items-center gap-1.5 rounded-xl border border-nova-gold/20 bg-black/25 px-2.5 py-1.5 text-xs text-nova-gold/85 hover:border-nova-gold/50 hover:bg-nova-gold/10"
    >
      <Icon size={12} className="text-nova-purple/90" />
      <span className="font-mono">{keyName}</span>
      {meta ? <span className="text-[10px] uppercase tracking-[0.12em] text-nova-gold/45">{meta}</span> : null}
    </button>
  );
}

export default function GraphSheet() {
  const [tab, setTab] = useState("explore");
  const [stats, setStats] = useState(null);
  const [query, setQuery] = useState("");
  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");

  const [days, setDays] = useState(7);
  const [about, setAbout] = useState("");
  const [timeline, setTimeline] = useState([]);

  const loadStats = useCallback(async () => {
    try { setStats(await getJson("/memory/graph/stats")); } catch { setStats(null); }
  }, []);

  // Mount fetch can race a just-booting backend, so retry on a slow interval
  // until stats land, and refresh alongside every user action below.
  useEffect(() => {
    loadStats();
    const id = setInterval(loadStats, 15000);
    return () => clearInterval(id);
  }, [loadStats]);

  const explore = useCallback(async (key) => {
    const k = (key || "").trim();
    if (!k) return;
    setBusy(true); setNote(""); setQuery(k);
    loadStats();
    try {
      const data = await getJson(`/memory/graph?key=${encodeURIComponent(k)}`);
      setResult(data);
      if (!data.neighbors?.length && !data.two_hop?.length) {
        setNote(`Nothing linked to "${k}" yet — connections build up as things get mentioned together in conversation.`);
      }
    } catch (e) {
      setResult(null); setNote(String(e?.message || e));
    } finally { setBusy(false); }
  }, [loadStats]);

  const loadTimeline = useCallback(async (d = days, a = about) => {
    setBusy(true); setNote("");
    try {
      const q = `/memory/timeline?days=${d}` + (a.trim() ? `&about=${encodeURIComponent(a.trim())}` : "");
      const data = await getJson(q);
      setTimeline(data.entries || []);
      if (!(data.entries || []).length) setNote(`Nothing recorded in the last ${d} day(s)${a.trim() ? ` about "${a.trim()}"` : ""}.`);
    } catch (e) {
      setTimeline([]); setNote(String(e?.message || e));
    } finally { setBusy(false); }
  }, [days, about]);

  useEffect(() => { if (tab === "timeline") loadTimeline(); /* eslint-disable-line react-hooks/exhaustive-deps */ }, [tab]);

  return (
    <div className="space-y-3 text-nova-gold">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="inline-flex items-center gap-2 text-sm text-nova-gold/80">
          <Share2 size={16} /> Knowledge Graph
          {stats ? (
            <span className="rounded-full border border-nova-purple/25 bg-nova-purple/10 px-2 py-0.5 text-[10px] uppercase tracking-[0.14em] text-nova-purple/90">
              {stats.edges} connection{stats.edges === 1 ? "" : "s"}
            </span>
          ) : null}
        </div>
        <div className="flex gap-1.5">
          {[{ key: "explore", label: "Explorer", icon: Search }, { key: "timeline", label: "Timeline", icon: Clock3 }].map((t) => {
            const Icon = t.icon;
            const active = tab === t.key;
            return (
              <button
                key={t.key}
                onClick={() => setTab(t.key)}
                className={[
                  "inline-flex items-center gap-1.5 rounded-xl border px-3 py-1.5 text-xs",
                  active ? "border-nova-gold/40 bg-nova-gold/10 text-nova-gold" : "border-nova-gold/15 bg-black/25 text-nova-gold/60 hover:text-nova-gold/90",
                ].join(" ")}
              >
                <Icon size={13} /> {t.label}
              </button>
            );
          })}
        </div>
      </div>

      {tab === "explore" && (
        <div className="space-y-3">
          <form
            onSubmit={(e) => { e.preventDefault(); explore(query); }}
            className="flex gap-2"
          >
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              className="flex-1 rounded-xl bg-black/25 border border-nova-gold/15 px-3 py-2 text-sm text-nova-gold outline-none placeholder:text-nova-gold/45"
              placeholder="Explore a person, project, or topic… (e.g. Mateo)"
            />
            <button
              type="submit"
              disabled={busy || !query.trim()}
              className="rounded-xl px-4 py-2 bg-black/25 border border-nova-gold/30 text-nova-gold hover:bg-black/35 text-sm disabled:opacity-50"
            >
              {busy ? "…" : "Explore"}
            </button>
          </form>

          {result && (result.neighbors?.length || result.two_hop?.length) ? (
            <div className="space-y-3">
              <div className="rounded-2xl border border-nova-gold/10 bg-black/25 p-3">
                <div className="pb-2 text-[10px] uppercase tracking-[0.24em] text-nova-gold/50">
                  Directly connected to <span className="font-mono text-nova-gold/90">{result.key}</span>
                </div>
                <div className="flex flex-wrap gap-2">
                  {result.neighbors.map((n, i) => (
                    <KindChip
                      key={`${n.key}-${i}`}
                      kind={n.kind}
                      keyName={n.key}
                      meta={`${n.predicate.replace(/_/g, " ")} ·×${Math.round(n.weight)}`}
                      onClick={() => explore(n.key)}
                    />
                  ))}
                </div>
              </div>
              {result.two_hop?.length ? (
                <div className="rounded-2xl border border-nova-gold/10 bg-black/25 p-3">
                  <div className="pb-2 text-[10px] uppercase tracking-[0.24em] text-nova-gold/50">One step out</div>
                  <div className="flex flex-wrap gap-2">
                    {result.two_hop.map((t, i) => (
                      <KindChip
                        key={`${t.key}-${i}`}
                        kind={t.kind}
                        keyName={t.key}
                        meta={`via ${t.via}`}
                        onClick={() => explore(t.key)}
                      />
                    ))}
                  </div>
                </div>
              ) : null}
            </div>
          ) : null}
        </div>
      )}

      {tab === "timeline" && (
        <div className="space-y-3">
          <form onSubmit={(e) => { e.preventDefault(); loadTimeline(); }} className="flex flex-wrap gap-2 items-center">
            <div className="flex gap-1">
              {[3, 7, 14, 30].map((d) => (
                <button
                  key={d}
                  type="button"
                  onClick={() => { setDays(d); loadTimeline(d, about); }}
                  className={[
                    "rounded-lg px-2.5 py-1 text-xs border",
                    days === d ? "border-nova-gold/60 bg-nova-gold/10 text-nova-gold" : "border-nova-gold/15 bg-black/20 text-nova-gold/55 hover:text-nova-gold",
                  ].join(" ")}
                >
                  {d}d
                </button>
              ))}
            </div>
            <input
              value={about}
              onChange={(e) => setAbout(e.target.value)}
              className="flex-1 min-w-[160px] rounded-xl bg-black/25 border border-nova-gold/15 px-3 py-1.5 text-sm text-nova-gold outline-none placeholder:text-nova-gold/45"
              placeholder="Filter: person / project / topic (optional)"
            />
            <button
              type="submit"
              disabled={busy}
              className="rounded-xl px-3 py-1.5 bg-black/25 border border-nova-gold/30 text-nova-gold hover:bg-black/35 text-sm disabled:opacity-50"
            >
              {busy ? "…" : "Refresh"}
            </button>
          </form>

          <div className="space-y-1.5">
            {timeline.map((e, i) => {
              const Icon = TIMELINE_ICONS[e.kind] || Database;
              const when = String(e.when || "").replace("T", " ").slice(0, 16);
              return (
                <div key={i} className="flex items-start gap-2 rounded-xl border border-nova-gold/10 bg-black/25 px-3 py-2">
                  <Icon size={13} className="mt-0.5 shrink-0 text-nova-purple/90" />
                  <div className="min-w-0">
                    <div className="text-[10px] uppercase tracking-[0.14em] text-nova-gold/40">{when} · {e.kind}</div>
                    <div className="text-sm text-nova-gold/90 break-words">{e.text}</div>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {note && <div className="rounded-xl border border-nova-gold/15 bg-black/25 px-3 py-2 text-xs text-nova-gold/60">{note}</div>}
    </div>
  );
}
