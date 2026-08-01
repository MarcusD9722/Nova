import { useEffect, useRef } from "react";

// Bus-event side effects extracted verbatim from App.jsx (Phase 0.6 of
// docs/ROADMAP.md) — behavior unchanged. Each block tracks its own last-seen
// event seq and skips the initial replay batch so old events don't re-post
// into chat on every app start.
//
//   appendNovaMessage(id, text) — append a Nova chat bubble
//   speakNotice(text)           — speak via the TTS queue
//   onScreenLookRequest(req)    — surface the screen-capture confirm prompt

export default function useNovaBusEffects({ novaEvents, appendNovaMessage, speakNotice, onScreenLookRequest }) {
  // ── Project builder reports ────────────────────────────────────────────────
  const lastProjectEventSeqRef = useRef(null);
  useEffect(() => {
    if (!novaEvents.length) return;
    // First batch is a replay of past events — skip it so old completions
    // don't re-post into chat on every app start.
    if (lastProjectEventSeqRef.current === null) {
      lastProjectEventSeqRef.current = Math.max(...novaEvents.map((e) => e.seq || 0));
      return;
    }

    const fresh = novaEvents.filter(
      (ev) =>
        (ev.seq || 0) > lastProjectEventSeqRef.current &&
        (ev.type === "project.completed" || ev.type === "project.error")
    );
    if (!fresh.length) {
      const maxSeq = Math.max(...novaEvents.map((e) => e.seq || 0));
      if (maxSeq > lastProjectEventSeqRef.current) lastProjectEventSeqRef.current = maxSeq;
      return;
    }

    lastProjectEventSeqRef.current = Math.max(
      ...fresh.map((e) => e.seq || 0),
      lastProjectEventSeqRef.current
    );

    fresh.forEach((ev) => {
      const d = ev.data || {};
      if (ev.type === "project.completed") {
        const files = Array.isArray(d.files) && d.files.length ? d.files.join(", ") : "";
        const suggestions =
          Array.isArray(d.suggestions) && d.suggestions.length
            ? `\n\nSuggested improvements:\n${d.suggestions.map((s) => `• ${s}`).join("\n")}\n\nSay "implement those improvements" and I'll do it.`
            : "";
        // Be honest about how sure we are. The build/run check only confirms it
        // launches — it can't verify a visual/interactive feature actually works.
        const status = d.status || "complete";
        const isImprove = d.mode === "improve";
        const note = d.test_note && !/passed/i.test(d.test_note) ? d.test_note : (d.run_note && !/passed/i.test(d.run_note) ? d.run_note : "");
        let head;
        if (status === "needs attention") {
          head = `⚠️ I worked on "${d.project}" but it didn't fully check out${note ? ` — ${note}` : ""}.`;
        } else if (status === "needs review") {
          head = `🛠️ I updated "${d.project}"${note ? ` (${note})` : ""}. It runs, but please double-check it does what you wanted.`;
        } else if (isImprove) {
          // Honest framing: `summary` is written by the planner BEFORE the code
          // is generated, so it describes what was ATTEMPTED, not a confirmed
          // fix. The run check only proves the program starts without crashing —
          // it cannot tell a working game loop from one frozen on frame one.
          // Saying "Done — resolved X" here is how a silent no-op got reported
          // as a fix four times in a row.
          head = `🛠️ I changed "${d.project}". Attempted: ${d.summary || "see files below"}\n⚠️ I verified only that it starts without crashing — I could NOT verify the behavior you asked about. Please run it and tell me what actually happens.`;
        } else {
          head = `✅ Project "${d.project}" is built. ${d.summary || ""}\nGive it a try and let me know how it looks.`;
        }
        const text =
          head +
          (files ? `\nFiles: ${files}` : "") +
          (d.run ? `\nRun it with: ${d.run}` : "") +
          suggestions;
        appendNovaMessage(`nova-project-${ev.seq}`, text);
        speakNotice(`I finished working on ${String(d.project || "").replace(/-/g, " ")}. Give it a try.`);
      } else {
        appendNovaMessage(
          `nova-project-${ev.seq}`,
          `⚠️ Project "${d.project}" hit a problem: ${d.error || "unknown error"}. Ask me to try again and I'll take another pass.`
        );
      }
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [novaEvents]);

  // ── Reminders / briefings ─────────────────────────────────────────────────
  const lastReminderEventSeqRef = useRef(null);
  useEffect(() => {
    if (!novaEvents.length) return;
    // Same "skip the initial replay batch" pattern as project events, so old
    // reminders don't re-fire into chat every time the app starts.
    if (lastReminderEventSeqRef.current === null) {
      lastReminderEventSeqRef.current = Math.max(...novaEvents.map((e) => e.seq || 0));
      return;
    }

    const fresh = novaEvents.filter(
      (ev) => (ev.seq || 0) > lastReminderEventSeqRef.current && ev.type === "reminder.due"
    );
    if (!fresh.length) {
      const maxSeq = Math.max(...novaEvents.map((e) => e.seq || 0));
      if (maxSeq > lastReminderEventSeqRef.current) lastReminderEventSeqRef.current = maxSeq;
      return;
    }
    lastReminderEventSeqRef.current = Math.max(...fresh.map((e) => e.seq || 0), lastReminderEventSeqRef.current);

    fresh.forEach((ev) => {
      const d = ev.data || {};
      const message = String(d.message || d.title || "Reminder!").trim();
      const text = d.briefing ? message : `⏰ ${d.title || "Reminder"}: ${message}`;
      appendNovaMessage(`nova-reminder-${ev.seq}`, text);
      speakNotice(message);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [novaEvents]);

  // ── Agent-requested screen looks (vision.look_at_screen) ──────────────────
  // Surface a confirm prompt; nothing captures without the explicit click,
  // even if a periodic focus session is already running.
  const lastScreenReqSeqRef = useRef(null);
  useEffect(() => {
    if (!novaEvents.length) return;
    if (lastScreenReqSeqRef.current === null) {
      lastScreenReqSeqRef.current = Math.max(...novaEvents.map((e) => e.seq || 0));
      return;
    }
    const fresh = novaEvents.filter(
      (ev) => (ev.seq || 0) > lastScreenReqSeqRef.current && ev.type === "screen.capture_requested"
    );
    const maxSeq = Math.max(...novaEvents.map((e) => e.seq || 0));
    if (maxSeq > lastScreenReqSeqRef.current) lastScreenReqSeqRef.current = maxSeq;
    if (!fresh.length) return;
    const latest = fresh[fresh.length - 1];
    const d = latest.data || {};
    onScreenLookRequest({ requestId: String(d.request_id || ""), question: String(d.question || "") });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [novaEvents]);
}
