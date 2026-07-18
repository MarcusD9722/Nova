import React, { useEffect, useMemo, useState } from "react";
import { ListTodo, RefreshCw, CircleDot, CheckCircle2, XCircle, Loader2 } from "lucide-react";

function apiUrl(path) {
  try {
    const base = window.__NOVA_API_BASE || "http://localhost:8008";
    return `${base}${path}`;
  } catch {
    return `http://localhost:8008${path}`;
  }
}

const STATUS_META = {
  queued: { icon: CircleDot, cls: "text-nova-gold/70 border-nova-gold/25 bg-black/25", label: "Queued" },
  running: { icon: Loader2, cls: "text-nova-purple border-nova-purple/40 bg-nova-purple/10", label: "Running" },
  done: { icon: CheckCircle2, cls: "text-emerald-300 border-emerald-400/25 bg-emerald-500/10", label: "Done" },
  failed: { icon: XCircle, cls: "text-red-300 border-red-400/25 bg-red-500/10", label: "Failed" },
};

function StatusChip({ status }) {
  const meta = STATUS_META[status] || STATUS_META.queued;
  const Icon = meta.icon;
  return (
    <span className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10px] uppercase tracking-[0.14em] ${meta.cls}`}>
      <Icon size={10} className={status === "running" ? "animate-spin" : ""} />
      {meta.label}
    </span>
  );
}

export default function TasksSheet({ liveEvents = [] }) {
  const [tasks, setTasks] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const load = async () => {
    setLoading(true);
    setError("");
    try {
      const res = await fetch(apiUrl("/tasks?limit=50"));
      if (!res.ok) throw new Error(await res.text());
      const data = await res.json();
      setTasks(Array.isArray(data?.tasks) ? data.tasks : []);
    } catch (err) {
      setError(String(err?.message || err));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
  }, []);

  // Refresh when task events arrive.
  const taskEventCount = useMemo(
    () => liveEvents.filter((ev) => String(ev?.type || "").startsWith("task.")).length,
    [liveEvents]
  );
  useEffect(() => {
    if (taskEventCount > 0) load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [taskEventCount]);

  const grouped = useMemo(() => {
    const list = tasks || [];
    const active = list.filter((t) => t.status === "queued" || t.status === "running");
    const finished = list.filter((t) => t.status !== "queued" && t.status !== "running");
    return { active, finished };
  }, [tasks]);

  const renderTask = (t) => (
    <div key={t.task_id} className="rounded-xl border border-nova-gold/10 bg-black/25 px-3 py-2">
      <div className="flex items-center justify-between gap-2">
        <div className="text-sm text-nova-gold/90 font-medium break-words">{t.title || "(untitled task)"}</div>
        <StatusChip status={t.status} />
      </div>
      {t.details ? <div className="mt-1 text-xs text-nova-gold/60 break-words">{t.details}</div> : null}
      <div className="mt-1 flex items-center gap-3 text-[10px] text-nova-gold/40">
        <span>project: {t.project_name || "temp"}</span>
        {t.attempts ? <span>attempts: {t.attempts}</span> : null}
        <span>{t.updated_at ? new Date(t.updated_at).toLocaleString() : ""}</span>
      </div>
      {t.last_error ? (
        <div className="mt-1 rounded-lg border border-red-400/20 bg-red-500/10 px-2 py-1 text-[11px] text-red-200 break-words">
          {t.last_error}
        </div>
      ) : null}
    </div>
  );

  return (
    <div className="space-y-3 text-nova-gold h-full flex flex-col">
      <div className="flex items-center justify-between">
        <div className="inline-flex items-center gap-2 text-sm text-nova-gold/80">
          <ListTodo size={16} />
          Background tasks Nova is tracking
        </div>
        <button
          type="button"
          onClick={load}
          className="rounded-xl px-3 py-2 bg-black/25 border border-nova-gold/20 text-nova-gold/70 hover:text-nova-gold hover:bg-black/35 text-sm"
          title="Refresh"
        >
          <RefreshCw size={14} className={loading ? "animate-spin" : ""} />
        </button>
      </div>

      {error && (
        <div className="rounded-xl border border-red-400/20 bg-red-500/10 px-3 py-2 text-xs text-red-200">{error}</div>
      )}

      <div className="flex-1 min-h-0 overflow-y-auto space-y-4 pr-1">
        <div className="space-y-2">
          <div className="text-[10px] uppercase tracking-[0.24em] text-nova-gold/50">Active ({grouped.active.length})</div>
          {grouped.active.length ? (
            grouped.active.map(renderTask)
          ) : (
            <div className="text-sm text-nova-gold/50 rounded-xl border border-nova-gold/10 bg-black/20 px-3 py-4 text-center">
              No active tasks. Ask Nova to work on something in the background.
            </div>
          )}
        </div>

        <div className="space-y-2">
          <div className="text-[10px] uppercase tracking-[0.24em] text-nova-gold/50">History ({grouped.finished.length})</div>
          {grouped.finished.length ? (
            grouped.finished.map(renderTask)
          ) : (
            <div className="text-xs text-nova-gold/40 px-3 py-2">No completed tasks yet.</div>
          )}
        </div>
      </div>
    </div>
  );
}
