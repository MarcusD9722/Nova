import React, { useEffect, useMemo, useState } from "react";
import { ListTodo, RefreshCw, CircleDot, CheckCircle2, XCircle, Loader2, HelpCircle, Ban, History, CircleHelp } from "lucide-react";

function apiUrl(path) {
  try {
    const base = window.__NOVA_API_BASE || "http://localhost:8008";
    return `${base}${path}`;
  } catch {
    return `http://localhost:8008${path}`;
  }
}

// Every status the backend can write has to be here. The fallback below is
// `queued`, so a status this map does not know is shown as "Queued" -- which
// says the task will run on its own. `cancelled` was already being mislabelled
// that way, and `blocked` (waiting for an answer) would have been too.
const STATUS_META = {
  queued: { icon: CircleDot, cls: "text-nova-gold/70 border-nova-gold/25 bg-black/25", label: "Queued" },
  running: { icon: Loader2, cls: "text-nova-purple border-nova-purple/40 bg-nova-purple/10", label: "Running" },
  blocked: { icon: HelpCircle, cls: "text-amber-300 border-amber-400/30 bg-amber-500/10", label: "Waiting for you" },
  done: { icon: CheckCircle2, cls: "text-emerald-300 border-emerald-400/25 bg-emerald-500/10", label: "Done" },
  failed: { icon: XCircle, cls: "text-red-300 border-red-400/25 bg-red-500/10", label: "Failed" },
  cancelled: { icon: Ban, cls: "text-nova-gold/40 border-nova-gold/15 bg-black/25", label: "Cancelled" },
  superseded: { icon: History, cls: "text-sky-300/80 border-sky-400/25 bg-sky-500/10", label: "Superseded" },
};

// What happened to the WORK, which is a different question from where the row
// is in its life. A superseded step may have succeeded; an interrupted one may
// have done anything at all. Showing only the status hid both.
const OUTCOME_META = {
  succeeded: { cls: "text-emerald-300/80 border-emerald-400/20", label: "work succeeded" },
  failed: { cls: "text-red-300/80 border-red-400/20", label: "work failed" },
  unknown: { cls: "text-amber-300/90 border-amber-400/30", label: "outcome unknown" },
  never_started: { cls: "text-nova-gold/45 border-nova-gold/15", label: "never ran" },
};

function OutcomeChip({ status, outcome }) {
  const meta = OUTCOME_META[outcome];
  // Only worth showing when it says something the status does not: a plain
  // done/succeeded or failed/failed pairing is noise.
  const redundant =
    (status === "done" && outcome === "succeeded") ||
    (status === "failed" && outcome === "failed") ||
    (status === "cancelled" && outcome === "never_started");
  if (!meta || redundant) return null;
  return (
    <span className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10px] uppercase tracking-[0.14em] ${meta.cls}`}>
      <CircleHelp size={10} />
      {meta.label}
    </span>
  );
}

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

function AnswerBox({ question, onSubmit }) {
  const [text, setText] = useState("");
  return (
    <div className="mt-1 rounded-lg border border-amber-400/25 bg-amber-500/10 px-2 py-2 space-y-2">
      <div className="text-[11px] text-amber-100 break-words">{question}</div>
      <div className="flex items-center gap-2">
        <input
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              onSubmit(text);
              setText("");
            }
          }}
          placeholder="Answer, and Nova picks it back up"
          className="flex-1 min-w-0 rounded-lg bg-black/30 border border-amber-400/20 px-2 py-1 text-[11px] text-nova-gold placeholder:text-nova-gold/30"
        />
        <button
          type="button"
          onClick={() => {
            onSubmit(text);
            setText("");
          }}
          className="rounded-lg px-2 py-1 border border-amber-400/30 bg-amber-500/10 text-[11px] text-amber-100 hover:bg-amber-500/20"
        >
          Send
        </button>
      </div>
    </div>
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

  // A task waiting on an answer is NOT history: it is the one thing here that
  // needs the user. It gets its own group at the top rather than being filed
  // with the finished work, which is exactly what the backend used to do to it.
  const grouped = useMemo(() => {
    const list = tasks || [];
    const waiting = list.filter((t) => t.status === "blocked");
    const active = list.filter((t) => t.status === "queued" || t.status === "running");
    const finished = list.filter(
      (t) => t.status !== "queued" && t.status !== "running" && t.status !== "blocked"
    );
    return { waiting, active, finished };
  }, [tasks]);

  const answer = async (taskId, text) => {
    const trimmed = String(text || "").trim();
    if (!trimmed) return;
    setError("");
    try {
      const res = await fetch(apiUrl(`/tasks/${encodeURIComponent(taskId)}/answer`), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ answer: trimmed }),
      });
      if (!res.ok) throw new Error(await res.text());
      await load();
    } catch (err) {
      setError(String(err?.message || err));
    }
  };

  const renderTask = (t) => (
    <div key={t.task_id} className="rounded-xl border border-nova-gold/10 bg-black/25 px-3 py-2">
      <div className="flex items-center justify-between gap-2">
        <div className="text-sm text-nova-gold/90 font-medium break-words">{t.title || "(untitled task)"}</div>
        <div className="flex items-center gap-1.5 shrink-0">
          <OutcomeChip status={t.status} outcome={t.outcome} />
          <StatusChip status={t.status} />
        </div>
      </div>
      {t.details ? <div className="mt-1 text-xs text-nova-gold/60 break-words">{t.details}</div> : null}
      <div className="mt-1 flex items-center gap-3 text-[10px] text-nova-gold/40">
        <span>project: {t.project_name || "temp"}</span>
        {t.attempts ? <span>attempts: {t.attempts}</span> : null}
        <span>{t.updated_at ? new Date(t.updated_at).toLocaleString() : ""}</span>
      </div>
      {t.last_error ? (
        t.status === "blocked" ? (
          <AnswerBox question={t.last_error} onSubmit={(text) => answer(t.task_id, text)} />
        ) : (
          <div className="mt-1 rounded-lg border border-red-400/20 bg-red-500/10 px-2 py-1 text-[11px] text-red-200 break-words">
            {t.last_error}
          </div>
        )
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
        {grouped.waiting.length ? (
          <div className="space-y-2">
            <div className="text-[10px] uppercase tracking-[0.24em] text-amber-300/70">
              Waiting for you ({grouped.waiting.length})
            </div>
            {grouped.waiting.map(renderTask)}
          </div>
        ) : null}

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
