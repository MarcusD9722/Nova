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

IDEMPOTENCE. Completion is derived, and deriving it twice is normal — a status
read, a re-evaluation after a no-op edit, a replayed callback. None of those is
a new event. The emitter remembers the last state it announced per
(project, revision) and stays silent when nothing moved.
"""

from __future__ import annotations

from typing import Any

from core.completion import COMPLETE, Verdict
from core.event_bus import BUS, clip
from core.logging_setup import get_logger

logger = get_logger(__name__)


class CompletionAnnouncer:
    """Emits completion events, once per real transition.

    Held by the runtime rather than constructed per build, because "has this
    already been announced?" is a question about the process's history, not
    about one build's.
    """

    def __init__(self) -> None:
        #: (project, revision) -> the state last announced for it.
        self._announced: dict[tuple[str, int], str] = {}

    def reset(self, slug: str | None = None) -> None:
        if slug is None:
            self._announced.clear()
            return
        for key in [k for k in self._announced if k[0] == slug]:
            self._announced.pop(key, None)

    def last_announced(self, slug: str, revision: int) -> str:
        return self._announced.get((slug, int(revision)), "")

    def announce(self, *, slug: str, verdict: Verdict,
                 reason: str = "", extra: dict[str, Any] | None = None) -> str:
        """Publish whatever this transition warrants. Returns the state.

        Nothing is published when the state has not moved since the last
        announcement for this project and revision. A duplicate evaluation is
        not news.
        """
        key = (slug, int(verdict.revision))
        previous = self._announced.get(key, "")
        if previous == verdict.state:
            return verdict.state

        payload = {
            "project": slug,
            "previous": previous or None,
            "current": verdict.state,
            "revision": verdict.revision,
            "reason": reason or (verdict.reasons[0] if verdict.reasons else ""),
            "outstanding": [s.criterion.text for s in verdict.outstanding][:5],
            "failing": [s.criterion.text for s in verdict.failing][:5],
            **(extra or {}),
        }
        BUS.publish("project.state_changed", payload)

        # `project.completed` means one thing now: the project became complete.
        # It fires on the TRANSITION, so re-deriving COMPLETE ten times
        # announces it once.
        if verdict.state == COMPLETE:
            BUS.publish("project.completed", {
                "project": slug,
                "revision": verdict.revision,
                "state": COMPLETE,
                "criteria": [s.criterion.text for s in verdict.criteria][:10],
                "contract": (extra or {}).get("contract", ""),
                **{k: v for k, v in (extra or {}).items() if k != "contract"},
            })
            logger.info("project_completed", project=slug,
                        revision=verdict.revision)

        self._announced[key] = verdict.state
        return verdict.state

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
