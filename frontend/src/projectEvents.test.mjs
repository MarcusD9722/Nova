/**
 * Stage 15 — the UI must not go silent when a build fails.
 *
 * Asserts the decision itself, with payloads shaped exactly as
 * `core/completion_events.py` publishes them. The Python side asserts that the
 * backend really sends these fields; this side asserts what the UI does with
 * them. Neither half is enough alone — the defect lived precisely in the gap.
 *
 * Run:  node src/projectEvents.test.mjs   (from frontend/)
 */

import { PROJECT_REPORT_TYPES, completedReport, unfinishedReport } from "./projectEvents.js";

let failed = 0;
function check(cond, label) {
  const status = cond ? "OK  " : "FAIL";
  if (!cond) failed += 1;
  console.log(`  ${status} ${label}`);
}

// The payload a real failing build produces: announce() spreads `extra`, and
// the builder's extra carries mode="build".
const failingBuild = {
  seq: 41,
  type: "project.state_changed",
  data: {
    project: "calc",
    previous: "scaffolded",
    current: "failing",
    state: "failing",
    revision: 1,
    reason: "build finished",
    state_reason: "2 required criterion/criteria are currently failing",
    outstanding: [],
    failing: ["adds two numbers", "subtracts two numbers"],
    contract: "auto",
    mode: "build",
    summary: "a calculator",
  },
};

console.log("\nStage 15 — a build that finished without completing");
{
  const r = unfinishedReport(failingBuild) || { text: "", speak: "", state: "" };
  check(unfinishedReport(failingBuild) !== null, "produces a report at all");
  check(/isn't finished/.test(r.text), "says it is not finished");
  check(r.text.includes("failing"), "names the state");
  check(r.text.includes("adds two numbers"), "names what is not working");
  check(r.text.includes("2 required criterion"),
        "explains WHY, from state_reason");
  check(!/build finished/i.test(r.text),
        "and does not print the announcement's occasion as the explanation");
  check(r.text.includes("subtracts two numbers"), "including the second one");
  check(!/✅/.test(r.text), "and does not congratulate anybody");
  check(/isn't finished/.test(r.speak), "the spoken line is honest too");
  check(r.state === "failing", "and carries the state for the caller");
}

console.log("\nmid-build transitions stay quiet");
{
  const midway = {
    seq: 7,
    type: "project.state_changed",
    data: { project: "calc", current: "scaffolded", state: "scaffolded" },
  };
  check(unfinishedReport(midway) === null,
        "a transition with no `mode` is not a finish report");

  const partway = {
    seq: 8,
    type: "project.state_changed",
    data: { project: "calc", current: "partially_implemented", mode: "build" },
  };
  check(unfinishedReport(partway) !== null,
        "but a builder-announced partial finish IS reported");
}

console.log("\nsuccess is left to project.completed");
{
  const done = {
    seq: 9,
    type: "project.state_changed",
    data: { project: "calc", current: "complete", state: "complete", mode: "build" },
  };
  check(unfinishedReport(done) === null,
        "a completed build produces no warning here");
  check(unfinishedReport({ seq: 10, type: "project.completed", data: {} }) === null,
        "and project.completed is not this function's business");
}

console.log("\nthe hook listens for the right things");
{
  check(PROJECT_REPORT_TYPES.includes("project.state_changed"),
        "project.state_changed is subscribed");
  check(PROJECT_REPORT_TYPES.includes("project.completed"),
        "project.completed still is");
  check(PROJECT_REPORT_TYPES.includes("project.error"),
        "and so is project.error");
}

console.log("\na completed build reports success, and only success");
{
  const built = {
    seq: 50,
    type: "project.completed",
    data: {
      project: "calc", state: "complete", mode: "build",
      summary: "a calculator", files: ["main.py"], run: "python main.py",
      suggestions: ["add memory"],
    },
  };
  const r = completedReport(built) || { text: "", speak: "" };
  check(completedReport(built) !== null, "produces a report");
  check(/✅/.test(r.text), "says it is built");
  check(r.text.includes("main.py"), "lists the files");
  check(r.text.includes("python main.py"), "says how to run it");
  check(r.text.includes("add memory"), "and passes on the suggestions");

  // `project.completed` carries no `status` — verified against a real build's
  // payload — so the old "needs attention" branches were unreachable. A stray
  // one must not resurrect them.
  const withStatus = {
    seq: 51,
    type: "project.completed",
    data: { ...built.data, status: "needs attention" },
  };
  const r2 = completedReport(withStatus) || { text: "" };
  check(r2.text === r.text,
        "a stray `status` field cannot turn a completion into a warning");

  const improved = {
    seq: 52,
    type: "project.completed",
    data: { project: "calc", state: "complete", mode: "improve",
            summary: "made it faster" },
  };
  const r3 = completedReport(improved) || { text: "" };
  check(/could NOT verify/i.test(r3.text),
        "an improvement says what it could not verify");
  check(!/✅/.test(r3.text), "and does not claim a clean build");

  check(completedReport({ seq: 53, type: "project.state_changed", data: {} }) === null,
        "and it ignores state_changed, which unfinishedReport owns");
}

// ── the state matrix the review asked for ──────────────────────────────────
//
// Every completion state the evaluator can hold, plus the four ways an event
// stream misbehaves. The property under test is one-directional and is the
// whole point: the UI may under-claim, never over-claim. It must never present
// a success the authoritative evaluator does not support.

function stateChanged(state, extra = {}) {
  return {
    seq: 100,
    type: "project.state_changed",
    data: {
      project: "calc", current: state, state, revision: 1, mode: "build",
      reason: "build finished",
      state_reason: `the evaluator says ${state}`,
      ...extra,
    },
  };
}

const SUCCESS = /✅|is built/;

console.log("\nevery completion state, and what the UI may claim");
{
  for (const state of ["idea", "planned", "scaffolded",
                       "partially_implemented", "failing", "passing"]) {
    const r = unfinishedReport(stateChanged(state));
    check(r !== null, `${state}: produces a report`);
    check(r !== null && !SUCCESS.test(r.text),
          `${state}: does NOT claim success`);
    check(r !== null && r.text.includes(state),
          `${state}: names the state it is in`);
  }

  // `passing` is the trap: everything checkable passes, and it is still not
  // complete, because a person has not confirmed what only a person can.
  const passing = unfinishedReport(stateChanged("passing", {
    outstanding: ["looks right on a phone"],
  }));
  check(passing !== null && /isn't finished/.test(passing.text),
        "passing: says it is not finished, however well it is going");
  check(passing !== null && passing.text.includes("looks right on a phone"),
        "passing: and names what is still owed");

  // COMPLETE is the only state that may claim success, and only through the
  // event that means it.
  check(unfinishedReport(stateChanged("complete")) === null,
        "complete: produces no warning here");
  const done = completedReport({
    seq: 101, type: "project.completed",
    data: { project: "calc", state: "complete", mode: "build" },
  });
  check(done !== null && SUCCESS.test(done.text),
        "complete: and project.completed is where success is claimed");
}

console.log("\nstale, duplicate, missing, and post-restart events");
{
  // STALE: an old event still says complete while the project has moved on.
  // The UI cannot know this from the event alone -- which is exactly why the
  // hook filters on a seq watermark rather than trusting arrival order. What
  // this file can assert is that a stale `state_changed` for a non-complete
  // state never becomes a success claim.
  const stale = unfinishedReport(stateChanged("failing", { seq: 1 }));
  check(stale !== null && !SUCCESS.test(stale.text),
        "stale: an old failing event still reads as a failure");

  // DUPLICATE: the same event twice must produce the same message, so the
  // hook's seq filter is the only thing that has to dedupe, and it cannot be
  // confused by a differing render.
  const a = unfinishedReport(stateChanged("failing"));
  const b = unfinishedReport(stateChanged("failing"));
  check(a.text === b.text && a.speak === b.speak,
        "duplicate: identical payloads render identically");

  // MISSING: no event at all. Nothing is claimed, which is the honest failure
  // mode -- silence about a success is safe, silence about a failure is the
  // bug this file exists for and is covered by the backend contract test.
  check(unfinishedReport(null) === null && completedReport(null) === null,
        "missing: no event produces no message");
  check(unfinishedReport({ type: "project.state_changed" }) === null,
        "missing: an event with no data produces no message");

  // AFTER RESTART: a fresh process re-announces nothing (the durable ledger),
  // but a replayed event can still arrive at a reconnecting UI. A replayed
  // NON-complete state must stay non-complete.
  const replayed = unfinishedReport(stateChanged("failing", { previous: null }));
  check(replayed !== null && !SUCCESS.test(replayed.text),
        "after restart: a replayed failing event is still a failure");
}

console.log("\nthe UI can never invent a success");
{
  // The one-directional property, stated as one assertion over everything a
  // state_changed event can carry.
  const invented = ["idea", "planned", "scaffolded", "partially_implemented",
                    "failing", "passing"]
    .map((s) => unfinishedReport(stateChanged(s, {
      // every field that might tempt a renderer into optimism
      run_note: "Run check passed (main.py).",
      test_note: "Logic tests passed.",
      summary: "a finished calculator",
      files: ["main.py"],
    })))
    .filter((r) => r && SUCCESS.test(r.text));
  check(invented.length === 0,
        `no non-complete state renders as success, even with passing run and `
        + `test notes attached (${invented.length} did)`);
}

console.log(failed ? `\nRESULT: FAILURES (${failed})` : "\nRESULT: ALL PASS");
process.exit(failed ? 1 : 0);
