import { useEffect, useRef } from "react";
import { PROJECT_REPORT_TYPES, completedReport, unfinishedReport } from "../projectEvents";

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
        PROJECT_REPORT_TYPES.includes(ev.type)
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
      if (ev.type === "project.state_changed") {
        // A build that stopped without completing. `project.completed` no
        // longer fires for these (Stage 14 narrowed it to real completions),
        // so without this the run ends in silence.
        const report = unfinishedReport(ev);
        if (report) {
          appendNovaMessage(`nova-project-${ev.seq}`, report.text);
          speakNotice(report.speak);
        }
      } else if (ev.type === "project.completed") {
        const report = completedReport(ev);
        if (report) {
          appendNovaMessage(`nova-project-${ev.seq}`, report.text);
          speakNotice(report.speak);
        }
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
