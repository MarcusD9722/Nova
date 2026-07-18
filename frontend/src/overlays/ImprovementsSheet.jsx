import React, { useCallback, useEffect, useMemo, useState } from "react";
import { diffLines } from "diff";
import {
  Sparkles, RefreshCw, Power, Check, X, RotateCcw, GitPullRequest,
  GraduationCap, AlertTriangle, ShieldCheck, FolderGit2, ChevronDown, ChevronRight,
} from "lucide-react";

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
async function postJson(path, payload) {
  const res = await fetch(apiUrl(path), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {}),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw Object.assign(new Error(body?.detail || res.statusText), { status: res.status });
  return body;
}

// Legacy fallback: color the stored unified-diff string. Used only for
// proposals persisted before old_content existed.
function UnifiedDiffView({ diff }) {
  const lines = String(diff || "").split("\n");
  return (
    <pre className="mt-2 max-h-56 overflow-auto rounded-lg border border-nova-gold/10 bg-black/40 p-2 font-mono text-[11px] leading-relaxed">
      {lines.map((ln, i) => {
        let cls = "text-nova-gold/55";
        if (ln.startsWith("+") && !ln.startsWith("+++")) cls = "text-emerald-300";
        else if (ln.startsWith("-") && !ln.startsWith("---")) cls = "text-red-300";
        else if (ln.startsWith("@@")) cls = "text-nova-purple/90";
        return <div key={i} className={cls}>{ln || " "}</div>;
      })}
    </pre>
  );
}

// Real line diff computed from old+new content (WS-G). Long unchanged runs
// are collapsed to keep large files reviewable; added/removed lines always
// show in full with line numbers on both sides.
function RealDiffView({ oldContent, newContent }) {
  const rows = useMemo(() => {
    const parts = diffLines(String(oldContent ?? ""), String(newContent ?? ""));
    const out = [];
    let oldNo = 1;
    let newNo = 1;
    const CONTEXT = 3;
    for (const part of parts) {
      const lines = part.value.split("\n");
      if (lines[lines.length - 1] === "") lines.pop(); // trailing newline artifact
      if (part.added) {
        for (const ln of lines) out.push({ kind: "add", oldNo: null, newNo: newNo++, text: ln });
      } else if (part.removed) {
        for (const ln of lines) out.push({ kind: "del", oldNo: oldNo++, newNo: null, text: ln });
      } else if (lines.length > CONTEXT * 2 + 2) {
        for (let i = 0; i < CONTEXT; i++) out.push({ kind: "ctx", oldNo: oldNo++, newNo: newNo++, text: lines[i] });
        const hidden = lines.length - CONTEXT * 2;
        out.push({ kind: "skip", count: hidden });
        oldNo += hidden;
        newNo += hidden;
        for (let i = lines.length - CONTEXT; i < lines.length; i++) {
          out.push({ kind: "ctx", oldNo: oldNo++, newNo: newNo++, text: lines[i] });
        }
      } else {
        for (const ln of lines) out.push({ kind: "ctx", oldNo: oldNo++, newNo: newNo++, text: ln });
      }
    }
    return out;
  }, [oldContent, newContent]);

  const rowCls = {
    add: "bg-emerald-500/10 text-emerald-200",
    del: "bg-red-500/10 text-red-300",
    ctx: "text-nova-gold/55",
  };
  const marker = { add: "+", del: "-", ctx: " " };

  return (
    <div className="mt-2 max-h-72 overflow-auto rounded-lg border border-nova-gold/10 bg-black/40 font-mono text-[11px] leading-relaxed">
      {rows.map((r, i) =>
        r.kind === "skip" ? (
          <div key={i} className="border-y border-nova-gold/10 bg-black/30 px-2 py-0.5 text-center text-nova-gold/35">
            ··· {r.count} unchanged lines ···
          </div>
        ) : (
          <div key={i} className={["flex", rowCls[r.kind]].join(" ")}>
            <span className="w-9 shrink-0 select-none border-r border-nova-gold/10 pr-1 text-right text-nova-gold/30">
              {r.oldNo ?? ""}
            </span>
            <span className="w-9 shrink-0 select-none border-r border-nova-gold/10 pr-1 text-right text-nova-gold/30">
              {r.newNo ?? ""}
            </span>
            <span className="w-4 shrink-0 select-none text-center">{marker[r.kind]}</span>
            <span className="whitespace-pre-wrap break-all">{r.text || " "}</span>
          </div>
        )
      )}
    </div>
  );
}

function StatusPill({ status }) {
  const map = {
    pending: "border-amber-400/30 bg-amber-500/10 text-amber-200",
    applied: "border-emerald-400/30 bg-emerald-500/10 text-emerald-200",
    reverted: "border-red-400/30 bg-red-500/10 text-red-200",
    rejected: "border-nova-gold/20 bg-black/30 text-nova-gold/50",
  };
  return (
    <span className={["rounded-full border px-2 py-0.5 text-[10px] uppercase tracking-[0.14em]", map[status] || map.rejected].join(" ")}>
      {status}
    </span>
  );
}

export default function ImprovementsSheet() {
  const [tab, setTab] = useState("proposals");
  const [status, setStatus] = useState(null);
  const [proposals, setProposals] = useState([]);
  const [devOff, setDevOff] = useState(false);
  const [lessons, setLessons] = useState([]);
  const [errors, setErrors] = useState({ recent: [], recurring: [] });
  const [busy, setBusy] = useState("");
  const [note, setNote] = useState("");
  // WS-G: expanded proposal id -> full detail (old_content/new_content) from
  // GET /dev/proposals/{id}; the list endpoint stays lightweight.
  const [expandedId, setExpandedId] = useState(null);
  const [detail, setDetail] = useState(null);
  const [detailLoading, setDetailLoading] = useState(false);

  const toggleExpand = useCallback(async (p) => {
    if (expandedId === p.id) {
      setExpandedId(null);
      setDetail(null);
      return;
    }
    setExpandedId(p.id);
    setDetail(null);
    setDetailLoading(true);
    try {
      setDetail(await getJson(`/dev/proposals/${p.id}`));
    } catch {
      setDetail(null); // fall back to the stored unified diff string
    } finally {
      setDetailLoading(false);
    }
  }, [expandedId]);

  const loadStatus = useCallback(async () => {
    try { setStatus(await getJson("/autonomy/status")); } catch { setStatus(null); }
  }, []);

  const loadAll = useCallback(async () => {
    await loadStatus();
    try {
      const p = await getJson("/dev/proposals");
      setProposals(p.proposals || []);
      setDevOff(false);
    } catch (e) {
      if (e.status === 403) { setDevOff(true); setProposals([]); }
    }
    try { setLessons((await getJson("/memory/lessons")).lessons || []); } catch { /* ignore */ }
    try { setErrors(await getJson("/autonomy/errors")); } catch { /* ignore */ }
  }, [loadStatus]);

  useEffect(() => {
    loadAll();
    const id = setInterval(loadStatus, 5000);
    return () => clearInterval(id);
  }, [loadAll, loadStatus]);

  const act = async (fn, label) => {
    setBusy(label); setNote("");
    try { await fn(); await loadAll(); }
    catch (e) { setNote(String(e?.message || e)); }
    finally { setBusy(""); }
  };

  const toggleAutonomy = () =>
    act(async () => {
      const enabled = status?.enabled;
      await postJson(enabled ? "/autonomy/stop" : "/autonomy/start");
    }, "toggle");

  const pending = useMemo(() => proposals.filter((p) => p.status === "pending"), [proposals]);
  const enabled = Boolean(status?.enabled);

  const tabs = [
    { key: "proposals", label: "Proposals", icon: GitPullRequest, badge: pending.length || null },
    { key: "lessons", label: "Lessons", icon: GraduationCap, badge: lessons.length || null },
    { key: "errors", label: "Errors", icon: AlertTriangle, badge: (errors.recurring || []).length || null },
  ];

  return (
    <div className="space-y-3 text-nova-gold">
      {/* header */}
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="inline-flex items-center gap-2 text-sm text-nova-gold/80">
          <Sparkles size={16} /> Self-Improvement
          <span className="inline-flex items-center gap-1 rounded-full border border-nova-purple/25 bg-nova-purple/10 px-2 py-0.5 text-[10px] uppercase tracking-[0.14em] text-nova-purple/90">
            <ShieldCheck size={10} /> {status?.dev_mode ? "Dev mode on" : "Dev mode off"}
          </span>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={toggleAutonomy}
            disabled={busy === "toggle"}
            className={[
              "inline-flex items-center gap-1.5 rounded-xl border px-3 py-1.5 text-xs uppercase tracking-[0.14em]",
              enabled ? "border-emerald-400/30 bg-emerald-500/10 text-emerald-200" : "border-red-400/30 bg-red-500/10 text-red-200",
            ].join(" ")}
            title="Toggle the proactive self-improvement loop"
          >
            <Power size={13} /> Autonomy {enabled ? "On" : "Off"}
          </button>
          <button
            onClick={() => act(loadAll, "refresh")}
            className="rounded-xl border border-nova-gold/20 bg-black/25 px-3 py-1.5 text-nova-gold/70 hover:text-nova-gold"
            title="Refresh"
          >
            <RefreshCw size={14} className={busy === "refresh" ? "animate-spin" : ""} />
          </button>
        </div>
      </div>

      {note && (
        <div className="rounded-xl border border-red-400/20 bg-red-500/10 px-3 py-2 text-xs text-red-200">{note}</div>
      )}

      {/* tabs */}
      <div className="flex gap-1.5">
        {tabs.map((t) => {
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
              {t.badge ? <span className="rounded-full bg-nova-purple/30 px-1.5 text-[10px]">{t.badge}</span> : null}
            </button>
          );
        })}
      </div>

      {/* PROPOSALS */}
      {tab === "proposals" && (
        <div className="space-y-2">
          {devOff && (
            <div className="rounded-xl border border-amber-400/20 bg-amber-500/10 px-3 py-2 text-xs text-amber-100">
              Developer mode is off. Set <code>NOVA_DEV_MODE=1</code> in <code>.env</code> and restart to let Nova propose changes to her own code.
            </div>
          )}
          {!devOff && proposals.length === 0 && (
            <div className="py-6 text-center text-sm text-nova-gold/40">No proposals yet. When Nova wants to change her own code, it shows up here for your approval.</div>
          )}
          {proposals.map((p) => (
            <div key={p.id} className="rounded-2xl border border-nova-gold/10 bg-black/25 p-3">
              <div className="flex items-start justify-between gap-2">
                <button type="button" onClick={() => toggleExpand(p)} className="min-w-0 flex-1 text-left">
                  <div className="flex items-center gap-1.5">
                    {expandedId === p.id ? <ChevronDown size={12} className="shrink-0 text-nova-gold/50" /> : <ChevronRight size={12} className="shrink-0 text-nova-gold/50" />}
                    <span className="truncate font-mono text-xs text-nova-gold/90">{p.display_path || p.path}</span>
                    {p.project && (
                      <span className="inline-flex shrink-0 items-center gap-1 rounded-full border border-nova-purple/30 bg-nova-purple/10 px-1.5 py-0.5 text-[9px] uppercase tracking-[0.14em] text-nova-purple/90">
                        <FolderGit2 size={9} /> {p.project}
                      </span>
                    )}
                  </div>
                  {p.reason && <div className="mt-0.5 text-xs text-nova-gold/55">{p.reason}</div>}
                  <div className="mt-1 flex items-center gap-2">
                    <StatusPill status={p.status} />
                    <span className="text-[10px] uppercase tracking-[0.14em] text-nova-gold/40">{p.origin === "nova" ? "proposed by Nova" : "manual"}</span>
                    {p.project && <span className="text-[10px] uppercase tracking-[0.14em] text-amber-200/60">boot test skipped (external)</span>}
                  </div>
                  {p.boot_error && <div className="mt-1 text-[11px] text-red-300">Rolled back: {p.boot_error}</div>}
                </button>
                <div className="flex shrink-0 gap-1.5">
                  {p.status === "pending" && (
                    <>
                      <button onClick={() => act(() => postJson("/dev/apply", { proposal_id: p.id, confirm: true }), p.id)}
                        disabled={busy === p.id}
                        className="inline-flex items-center gap-1 rounded-lg border border-emerald-400/30 bg-emerald-500/10 px-2 py-1 text-xs text-emerald-200 hover:bg-emerald-500/20">
                        <Check size={12} /> Approve
                      </button>
                      <button onClick={() => act(() => postJson("/dev/reject", { proposal_id: p.id }), p.id)}
                        disabled={busy === p.id}
                        className="inline-flex items-center gap-1 rounded-lg border border-nova-gold/20 bg-black/30 px-2 py-1 text-xs text-nova-gold/70 hover:text-nova-gold">
                        <X size={12} /> Reject
                      </button>
                    </>
                  )}
                  {p.status === "applied" && p.has_backup && (
                    <button onClick={() => act(() => postJson("/dev/rollback", { proposal_id: p.id }), p.id)}
                      disabled={busy === p.id}
                      className="inline-flex items-center gap-1 rounded-lg border border-red-400/30 bg-red-500/10 px-2 py-1 text-xs text-red-200 hover:bg-red-500/20">
                      <RotateCcw size={12} /> Roll back
                    </button>
                  )}
                </div>
              </div>
              {expandedId === p.id && detailLoading && (
                <div className="mt-2 text-xs text-nova-gold/40">Loading diff…</div>
              )}
              {expandedId === p.id && !detailLoading && (
                detail && (detail.old_content || detail.new_content)
                  ? <RealDiffView oldContent={detail.old_content} newContent={detail.new_content} />
                  : (p.diff ? <UnifiedDiffView diff={p.diff} /> : null)
              )}
            </div>
          ))}
        </div>
      )}

      {/* LESSONS */}
      {tab === "lessons" && (
        <div className="space-y-2">
          {lessons.length === 0 && (
            <div className="py-6 text-center text-sm text-nova-gold/40">No lessons yet. Tell Nova a preference ("from now on…", "I prefer…") and she'll remember it here.</div>
          )}
          {lessons.map((l) => (
            <div key={l.id} className="rounded-xl border border-nova-gold/10 bg-black/25 px-3 py-2">
              <div className="text-sm text-nova-gold/90">{l.text}</div>
              <div className="mt-0.5 text-[10px] uppercase tracking-[0.14em] text-nova-gold/40">{l.topic} · {String(l.created_at).slice(0, 10)}</div>
            </div>
          ))}
        </div>
      )}

      {/* ERRORS */}
      {tab === "errors" && (
        <div className="space-y-3">
          <div>
            <div className="pb-1 text-[10px] uppercase tracking-[0.24em] text-nova-gold/50">Recurring (Nova may propose fixes)</div>
            {(errors.recurring || []).length === 0 ? (
              <div className="text-xs text-nova-gold/40">None — nothing recurring.</div>
            ) : (
              (errors.recurring || []).map((e, i) => (
                <div key={i} className="rounded-xl border border-amber-400/15 bg-amber-500/5 px-3 py-2 text-xs">
                  <span className="mr-2 rounded-full bg-amber-500/20 px-1.5 py-0.5 text-amber-200">×{e.count}</span>
                  <span className="text-nova-gold/80">{e.component}</span>
                  <div className="mt-0.5 text-nova-gold/55">{e.message}</div>
                </div>
              ))
            )}
          </div>
          <div>
            <div className="pb-1 text-[10px] uppercase tracking-[0.24em] text-nova-gold/50">Recent</div>
            {(errors.recent || []).length === 0 ? (
              <div className="text-xs text-nova-gold/40">No errors logged. 🎉</div>
            ) : (
              (errors.recent || []).slice(0, 30).map((e, i) => (
                <div key={i} className="rounded border border-nova-gold/5 bg-black/25 px-2 py-1 font-mono text-[11px]">
                  <span className="text-nova-purple/80">{String(e.ts || "").slice(11, 19)}</span>{" "}
                  <span className="text-nova-gold/80">{e.component}</span>{" "}
                  <span className="text-nova-gold/50">{String(e.message).slice(0, 120)}</span>
                </div>
              ))
            )}
          </div>
        </div>
      )}
    </div>
  );
}
