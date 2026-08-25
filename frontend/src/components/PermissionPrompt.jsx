import React, { useEffect, useMemo, useRef, useState } from "react";
import { ShieldAlert, Check, X, Clock } from "lucide-react";

function apiUrl(path) {
  try {
    const base = window.__NOVA_API_BASE || "http://localhost:8008";
    return `${base}${path}`;
  } catch {
    return `http://localhost:8008${path}`;
  }
}

/**
 * The approval surface for permission-gated capabilities.
 *
 * Nova has had `permission.requested` / `permission.expired` on the bus and a
 * `POST /permissions/resolve` endpoint for some time, and nothing in the
 * frontend consumed any of it. So an admin-tier action — `project.delete` —
 * asked for approval that could not be given, waited its full 120 seconds, and
 * timed out. From the user's side Nova simply hung and then did nothing.
 *
 * Two rules this component exists to keep:
 *
 *  1. It never reports success on its own say-so. The backend answers with
 *     `applied`, which is whether the click actually did anything; `approved`
 *     only echoes what was clicked. A request that expired a moment before the
 *     click returns applied=false, and that is shown as "no longer waiting",
 *     never as done.
 *  2. An expired request loses its buttons. `permission.expired` disables the
 *     controls and marks the card, so there is nothing left to click that could
 *     imply an action still might run.
 */
export default function PermissionPrompt({ liveEvents = [] }) {
  const [requests, setRequests] = useState([]);
  const seen = useRef(new Set());

  useEffect(() => {
    if (!Array.isArray(liveEvents) || !liveEvents.length) return;
    for (const ev of liveEvents) {
      const type = String(ev?.type || "");
      const data = ev?.data || {};
      const id = String(data.request_id || "");
      if (!id) continue;

      if (type === "permission.requested") {
        if (seen.current.has(id)) continue;
        seen.current.add(id);
        setRequests((prev) => [
          ...prev,
          {
            id,
            capability: String(data.capability || "an action"),
            tier: String(data.tier || ""),
            details: data.details || {},
            state: "pending",
            note: "",
          },
        ]);
      } else if (type === "permission.expired") {
        setRequests((prev) =>
          prev.map((r) =>
            r.id === id && r.state === "pending"
              ? {
                  ...r,
                  state: "expired",
                  note:
                    String(data.outcome || "expired") === "timeout"
                      ? "Timed out — nothing was done."
                      : "No longer waiting — nothing was done.",
                }
              : r
          )
        );
      }
    }
  }, [liveEvents]);

  const answer = async (id, approved) => {
    setRequests((prev) =>
      prev.map((r) => (r.id === id ? { ...r, state: "sending" } : r))
    );
    try {
      const res = await fetch(apiUrl("/permissions/resolve"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ request_id: id, approved: !!approved }),
      });
      const body = await res.json().catch(() => ({}));
      // `applied` is the truth. `approved` only echoes the click.
      const applied = res.ok && body && body.applied === true;
      setRequests((prev) =>
        prev.map((r) =>
          r.id === id
            ? {
                ...r,
                state: applied ? (approved ? "approved" : "denied") : "stale",
                note: applied
                  ? approved
                    ? "Approved — running it now."
                    : "Denied — nothing was done."
                  : String(
                      (body && body.note) ||
                        "That request was no longer waiting — nothing was done."
                    ),
              }
            : r
        )
      );
    } catch (err) {
      setRequests((prev) =>
        prev.map((r) =>
          r.id === id
            ? { ...r, state: "stale", note: String(err?.message || err) }
            : r
        )
      );
    }
  };

  const dismiss = (id) =>
    setRequests((prev) => prev.filter((r) => r.id !== id));

  const live = useMemo(
    () => requests.filter((r) => r.state !== "dismissed"),
    [requests]
  );
  if (!live.length) return null;

  return (
    <div className="fixed inset-x-0 bottom-6 z-[95] flex flex-col items-center gap-2 px-4">
      {live.map((r) => {
        const target =
          String(r.details?.project || r.details?.target || r.details?.path || "");
        const recoverable = r.details?.recoverable;
        const closed = ["expired", "approved", "denied", "stale"].includes(r.state);
        return (
          <div
            key={r.id}
            role="alertdialog"
            aria-label={`Approval needed: ${r.capability}`}
            className="w-full max-w-lg rounded-2xl border border-amber-400/40 bg-black/80 backdrop-blur px-4 py-3 shadow-xl"
          >
            <div className="flex items-start gap-3">
              <ShieldAlert size={18} className="mt-0.5 text-amber-300 shrink-0" />
              <div className="min-w-0 flex-1">
                <div className="text-sm text-amber-100 font-medium">
                  Nova needs your approval
                </div>
                <div className="mt-1 text-sm text-nova-gold/90 break-words">
                  <span className="font-mono text-[12px]">{r.capability}</span>
                  {target ? (
                    <>
                      {" on "}
                      <span className="font-medium">{target}</span>
                    </>
                  ) : null}
                </div>
                <div className="mt-1 flex flex-wrap items-center gap-2 text-[10px] uppercase tracking-[0.14em] text-nova-gold/50">
                  {r.tier ? (
                    <span className="rounded-full border border-nova-gold/20 px-2 py-0.5">
                      {r.tier} tier
                    </span>
                  ) : null}
                  <span
                    className={
                      recoverable === false
                        ? "rounded-full border border-red-400/40 text-red-200 px-2 py-0.5"
                        : "rounded-full border border-nova-gold/20 px-2 py-0.5"
                    }
                  >
                    {recoverable === false
                      ? "permanent — cannot be undone"
                      : "recoverable — goes to trash"}
                  </span>
                </div>

                {r.note ? (
                  <div className="mt-2 text-[12px] text-nova-gold/70 break-words">
                    {r.note}
                  </div>
                ) : null}
              </div>

              {closed ? (
                <button
                  type="button"
                  onClick={() => dismiss(r.id)}
                  className="rounded-lg px-2 py-1 border border-nova-gold/20 text-[11px] text-nova-gold/70 hover:text-nova-gold"
                >
                  Dismiss
                </button>
              ) : (
                <div className="flex items-center gap-2 shrink-0">
                  <button
                    type="button"
                    disabled={r.state === "sending"}
                    onClick={() => answer(r.id, false)}
                    className="inline-flex items-center gap-1 rounded-lg px-3 py-1.5 border border-nova-gold/25 bg-black/30 text-[12px] text-nova-gold/80 hover:text-nova-gold disabled:opacity-40"
                  >
                    <X size={12} />
                    Deny
                  </button>
                  <button
                    type="button"
                    disabled={r.state === "sending"}
                    onClick={() => answer(r.id, true)}
                    className="inline-flex items-center gap-1 rounded-lg px-3 py-1.5 border border-amber-400/40 bg-amber-500/15 text-[12px] text-amber-100 hover:bg-amber-500/25 disabled:opacity-40"
                  >
                    <Check size={12} />
                    Approve
                  </button>
                </div>
              )}
            </div>

            {r.state === "expired" ? (
              <div className="mt-2 inline-flex items-center gap-1 text-[11px] text-nova-gold/50">
                <Clock size={11} />
                This request is no longer waiting.
              </div>
            ) : null}
          </div>
        );
      })}
    </div>
  );
}
