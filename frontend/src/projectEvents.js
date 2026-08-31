/**
 * What the UI should say when the backend finishes working on a project.
 *
 * Extracted as a pure function because the decision is a CONTRACT with the
 * backend, not a rendering detail, and a contract that can only be exercised
 * by mounting React is a contract nobody tests.
 *
 * THE BUG THIS EXISTS FOR. `project.completed` used to fire whenever a build
 * finished, carrying a `status` that might say "needs attention", and this UI
 * printed "⚠️ I worked on X but it didn't fully check out". Stage 14 narrowed
 * that event so it fires ONLY on a real transition into COMPLETE — which is
 * correct, and which nothing here was told about. Measured on a real build
 * whose acceptance criteria failed:
 *
 *     backend state            : failing
 *     events published         : project.progress x9
 *                                project.state_changed x1
 *                                project.validation_failed x2
 *     events the UI listens for: project.progress only
 *
 * So the build ended, the backend knew it had failed, and the user was told
 * nothing at all. Two individually correct pieces, one silence.
 *
 * `project.state_changed` fires on EVERY transition, including ones in the
 * middle of a build, so a report is produced only when the payload carries
 * `mode` — which the builder sets on the announcement it makes when it has
 * finished ("build" or "improve").
 */

/** Event types that can produce a chat message about a project. */
export const PROJECT_REPORT_TYPES = [
  "project.completed",
  "project.error",
  "project.state_changed",
];

/** States that mean "this is finished and it is fine". */
const GOOD = "complete";

function list(values, limit) {
  const arr = Array.isArray(values) ? values.filter(Boolean) : [];
  return arr.slice(0, limit).join(", ");
}

/**
 * A report for a build/improve run that finished WITHOUT completing.
 *
 * Returns null for anything else — including every mid-build transition, and
 * including the successful case, which `project.completed` already covers.
 */
export function unfinishedReport(ev) {
  if (!ev || ev.type !== "project.state_changed") return null;
  const d = ev.data || {};
  // Only announcements the BUILDER made when it stopped working. Without this
  // the UI would narrate every intermediate state of every build.
  if (!d.mode) return null;
  const state = String(d.current || d.state || "");
  if (!state || state === GOOD) return null;

  const project = String(d.project || "the project");
  const failing = list(d.failing, 3);
  const outstanding = list(d.outstanding, 3);
  // `reason` says why the announcement happened ("build finished"); it is the
  // OCCASION, not the explanation. `state_reason` is why the state is what it
  // is. Prefer the explanation and never print the occasion as though it were
  // one.
  const why = String(d.state_reason || "").trim();

  const lines = [`⚠️ I worked on "${project}", but it isn't finished — it's ${state}.`];
  if (why) lines.push(why.charAt(0).toUpperCase() + why.slice(1) + ".");
  if (failing) lines.push(`Not working: ${failing}`);
  else if (outstanding) lines.push(`Still unproven: ${outstanding}`);
  lines.push("Tell me to keep going and I'll take another pass.");

  return {
    text: lines.join("\n"),
    speak: `I worked on ${project.replace(/-/g, " ")}, but it isn't finished yet.`,
    state,
  };
}
