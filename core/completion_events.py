"""Saying what happened, in words that mean it (Stage 14 §8).

`project.completed` used to fire whenever a build finished, carrying a `status`
field that might say `needs attention`. Every in-repo consumer happened to read
that field, so nothing was visibly broken — but the contract permitted a
consumer not to look, and an event named `completed` that fires for a program
which crashes on every run is a trap laid for the next person who writes one.

So:

  project.state_changed      every transition, with previous, current, the
                             requirement revision, and why
  project.validation_passed  one criterion was demonstrated
  project.validation_failed  one criterion was refuted
  project.completed          ONLY on an actual transition into COMPLETE

IDEMPOTENCE IS DURABLE, AND THAT WAS MEASURED RATHER THAN ASSUMED.

The first version kept the ledger in process memory. Within one process it
worked. Across a restart it did not, and the reproduction is worth stating
because the reasoning that produced the bug sounded fine:

    process 1, real transition to COMPLETE   -> 1 event   (correct)
    process 2, fresh, same durable root      -> 1 event   (wrong)
    process 3, fresh again                   -> 1 event   (wrong)

A new process starts with an empty dict, so `previous` is "" and `"" !=
"complete"` publishes. Exactly-once has to be scoped to the TRANSITION, and a
transition outlives the process that observed it — so the ledger has to as
well. It is now a durable row per (project, revision), claimed in a single
guarded statement.

What that deliberately still allows: COMPLETE at R1, then R2 falling to
PARTIAL, then R2 reaching COMPLETE, announces twice — because those are two
real transitions. And FAILING -> COMPLETE announces, because repairing
something is news.
"""

from __future__ import annotations

from typing import Any

from core.completion import COMPLETE, Verdict
from core.event_bus import BUS, clip
from core.logging_setup import get_logger

logger = get_logger(__name__)


class CompletionAnnouncer:
    """Emits completion events, once per real transition, across restarts."""

    def __init__(self, *, memory: Any | None = None) -> None:
        #: Durable ledger. Without it this class is correct only until the
        #: process ends, which is the defect that produced the table.
        self._memory = memory

    async def announce(self, *, slug: str, verdict: Verdict,
                       reason: str = "",
                       extra: dict[str, Any] | None = None) -> str:
        """Publish whatever this transition warrants. Returns the state.

        Nothing is published when the state has not moved since the last
        announcement for this project and revision — in this process or any
        earlier one.
        """
        previous = await self._claim(slug, verdict)
        if previous is None:
            return verdict.state

        payload = {
            "project": slug,
            "previous": previous or None,
            "current": verdict.state,
            "state": verdict.state,
            "revision": verdict.revision,
            "reason": reason or (verdict.reasons[0] if verdict.reasons else ""),
            "outstanding": [s.criterion.text for s in verdict.outstanding][:5],
            "failing": [s.criterion.text for s in verdict.failing][:5],
            "contract": verdict.seal_mode,
            **(extra or {}),
        }
        BUS.publish("project.state_changed", payload)

        # `project.completed` means one thing: the project became complete.
        if verdict.state == COMPLETE:
            BUS.publish("project.completed", {
                "project": slug,
                "revision": verdict.revision,
                "state": COMPLETE,
                "previous": previous or None,
                "criteria": [s.criterion.text for s in verdict.criteria][:10],
                "contract": verdict.seal_mode,
                **{k: v for k, v in (extra or {}).items() if k != "contract"},
            })
            logger.info("project_completed", project=slug,
                        revision=verdict.revision, contract=verdict.seal_mode)
        return verdict.state

    async def _claim(self, slug: str, verdict: Verdict) -> str | None:
        """The previous announced state, or None when nothing moved."""
        if self._memory is None:
            # No store to remember with. Announcing is the safer failure: a
            # missed transition is invisible, a duplicated one is merely noisy,
            # and callers that pass no memory are tests of the payload rather
            # than of the ledger.
            return ""
        return await self._memory.claim_state_announcement(
            project_name=slug, revision=int(verdict.revision),
            state=verdict.state)

    @staticmethod
    def criterion_result(*, slug: str, criterion_id: str, criterion_text: str,
                         verdict: str, revision: int, detail: str = "") -> None:
        """One criterion was decided. Named so a listener knows WHICH."""
        name = ("project.validation_passed" if verdict == "passed"
                else "project.validation_failed" if verdict == "failed"
                else "project.validation_inconclusive")
        BUS.publish(name, {"project": slug, "criterion_id": criterion_id,
                           "criterion": clip(criterion_text, 160),
                           "revision": revision, "detail": clip(detail, 200)})
