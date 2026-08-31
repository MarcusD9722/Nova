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

import { PROJECT_REPORT_TYPES, unfinishedReport } from "./projectEvents.js";

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

console.log(failed ? `\nRESULT: FAILURES (${failed})` : "\nRESULT: ALL PASS");
process.exit(failed ? 1 : 0);
