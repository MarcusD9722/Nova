/**
 * What the UI should say when the backend finishes working on a project.
 *
 * Extracted as pure functions because the decision is a CONTRACT with the
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
 */

/** Event types that can produce a chat message about a project. */
export const PROJECT_REPORT_TYPES = [
  "project.completed",
  "project.error",
  "project.state_changed",
];

/** The state that means "finished, and fine". */
const GOOD = "complete";

function list(values, limit) {
  const arr = Array.isArray(values) ? values.filter(Boolean) : [];
  return arr.slice(0, limit).join(", ");
}

/** Files, how to run it, and any suggestions — shared by both reports. */
function tail(d) {
  const files = list(d.files, 12);
  const suggestions =
    Array.isArray(d.suggestions) && d.suggestions.length
      ? "\n\nSuggested improvements:\n" +
        d.suggestions.map((x) => `• ${x}`).join("\n") +
        '\n\nSay "implement those improvements" and I\'ll do it.'
      : "";
  return (
    (files ? `\nFiles: ${files}` : "") +
    (d.run ? `\nRun it with: ${d.run}` : "") +
    suggestions
  );
}

/**
 * A report for a build/improve run that finished WITHOUT completing.
 *
 * `project.state_changed` fires on EVERY transition, including ones in the
 * middle of a build, so a report is produced only when the payload carries
 * `mode` — which the builder sets on the announcement it makes when it has
 * stopped ("build" or "improve"). Returns null for everything else, including
 * the successful case, which `project.completed` owns.
 */
export function unfinishedReport(ev) {
  if (!ev || ev.type !== "project.state_changed") return null;
  const d = ev.data || {};
  if (!d.mode) return null;
  const state = String(d.current || d.state || "");
  if (!state || state === GOOD) return null;

  const project = String(d.project || "the project");
  const failing = list(d.failing, 3);
  const outstanding = list(d.outstanding, 3);
  // `reason` says why the announcement happened ("build finished"); it is the
  // OCCASION, not the explanation. `state_reason` is why the state is what it
  // is. Print the explanation; never print the occasion as though it were one.
  const why = String(d.state_reason || "").trim();

  const lines = [
    `⚠️ I worked on "${project}", but it isn't finished — it's ${state}.`,
  ];
  if (why) lines.push(why.charAt(0).toUpperCase() + why.slice(1) + ".");
  if (failing) lines.push(`Not working: ${failing}`);
  else if (outstanding) lines.push(`Still unproven: ${outstanding}`);
  lines.push("Tell me to keep going and I'll take another pass.");

  return {
    text: lines.join("\n") + tail(d),
    speak: `I worked on ${project.replace(/-/g, " ")}, but it isn't finished yet.`,
    state,
  };
}

/**
 * The message for a project that really did complete.
 *
 * `project.completed` carries no `status` field — verified against a real
 * build's payload — so the "needs attention" and "needs review" branches this
 * logic used to have were unreachable. They were worse than dead: they made
 * the file look as though it warned about unfinished work, which is exactly
 * the impression under which a build failing in silence went unnoticed. The
 * warning path is `unfinishedReport`; this one is only ever about success.
 */
export function completedReport(ev) {
  if (!ev || ev.type !== "project.completed") return null;
  const d = ev.data || {};
  const project = String(d.project || "the project");

  // Honest framing for an improvement: `summary` is written by the planner
  // BEFORE the code is generated, so it describes what was ATTEMPTED. The run
  // check only proves the program starts without crashing — it cannot tell a
  // working game loop from one frozen on frame one. Saying "Done — resolved X"
  // here is how a silent no-op got reported as a fix four times in a row.
  const head =
    d.mode === "improve"
      ? `🛠️ I changed "${project}". Attempted: ${d.summary || "see files below"}\n⚠️ I verified only that it starts without crashing — I could NOT verify the behavior you asked about. Please run it and tell me what actually happens.`
      : `✅ Project "${project}" is built. ${d.summary || ""}\nGive it a try and let me know how it looks.`;

  return {
    text: head + tail(d),
    speak: `I finished working on ${project.replace(/-/g, " ")}. Give it a try.`,
  };
}
