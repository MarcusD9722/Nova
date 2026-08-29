"""The one place that answers "is this done?" (Stage 14 §7).

Before Stage 14 the answer came from three lines inside the project builder,
duplicated in two code paths, consulting whether the program started. Anything
that wanted to claim completion could assign the string.

This service replaces that with a shape where there is nothing to assign:

    record the request        ->  `record_request`
    record what would prove it ->  `set_criteria`
    record what was observed   ->  `record_verdict`
    ask what state it is in    ->  `evaluate`   (derived, every time)

`evaluate` reads durable rows and the implementation on disk and calls the pure
derivation in `core.completion`. It holds no cache and keeps no state field, so
a stale answer is not possible: there is nowhere for one to live.

WHAT THIS DELIBERATELY DOES NOT DO

It does not generate criteria from the finished code. Criteria are recorded
against the user's durable request before implementation, and each carries the
span of that request it came from. An acceptance contract derived from the
artifact can only ever conclude that the artifact does what it does — which is
the circularity that let a calculator with no subtraction be `complete`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from core.completion import (
    Criterion, Evidence, FAILED, HUMAN_PENDING, INCONCLUSIVE, PASSED, WAIVED,
    Verdict, derive_state,
)
from core.completion_artifacts import has_implementation, implementation_digest
from core.logging_setup import get_logger

logger = get_logger(__name__)

#: Verdicts a caller may record. `pending` is not here — pending is the absence
#: of evidence, and writing a row to say nothing happened would make "nothing
#: was checked" indistinguishable from "something was checked and said
#: nothing", which is the distinction the whole stage rests on.
RECORDABLE = frozenset({PASSED, FAILED, HUMAN_PENDING, WAIVED, INCONCLUSIVE})


class CompletionService:
    """Derives project completion state from durable criteria and evidence."""

    def __init__(self, *, memory: Any, projects_dir: Path) -> None:
        self._memory = memory
        self._projects_dir = Path(projects_dir)

    def project_path(self, slug: str) -> Path:
        return self._projects_dir / slug

    # ── recording ───────────────────────────────────────────────────────────

    async def record_request(self, *, slug: str, request_text: str,
                             source: str = "user", note: str = "") -> int:
        """Record the user's own words. Returns the new requirement revision.

        Every call is a new revision. A correction IS a new revision, and that
        is what makes evidence gathered for the old one stop counting.
        """
        rev = await self._memory.record_requirement(
            project_name=slug, request_text=request_text, source=source, note=note)
        logger.info("completion_request_recorded", project=slug, revision=rev)
        return rev

    async def set_criteria(self, *, slug: str, revision: int,
                           criteria: Sequence[dict[str, Any]]) -> list[str]:
        """Record the acceptance criteria for a requirement revision.

        Each entry needs `text` and `origin_quote`. The quote is not decoration:
        it is what makes a criterion traceable to the request instead of to
        somebody's impression of it, and a criterion without one is refused.
        """
        ids: list[str] = []
        for spec in criteria:
            text = str(spec.get("text") or "").strip()
            quote = str(spec.get("origin_quote") or "").strip()
            if not text:
                continue
            if not quote:
                raise ValueError(
                    f"criterion {text!r} has no origin_quote: a criterion that "
                    "cannot point at the request is not an acceptance criterion")
            ids.append(await self._memory.add_acceptance_criterion(
                project_name=slug, revision=int(revision), text=text,
                origin_quote=quote, source=str(spec.get("source") or "user"),
                required=bool(spec.get("required", True)),
                verify_kind=str(spec.get("verify_kind") or "machine")))
        logger.info("completion_criteria_recorded", project=slug,
                    revision=revision, count=len(ids))
        return ids

    async def carry_forward(self, *, slug: str, from_revision: int,
                            to_revision: int,
                            drop_criterion_ids: Sequence[str] = (),
                            drop_reason: str = "") -> list[str]:
        """Move an unchanged acceptance contract onto a new revision.

        A correction usually changes PART of what was asked. The criteria that
        did not change still stand, and re-recording them by hand at every
        revision is how one quietly goes missing. Anything genuinely retired
        must be named in `drop_criterion_ids` — an explicit, attributable act,
        which is the only way a criterion may leave the contract.

        Evidence is NOT carried forward. It was gathered under the old revision
        and has to be re-established, which is exactly what "the requirements
        changed" should mean.
        """
        old = await self._memory.list_acceptance_criteria(
            project_name=slug, revision=int(from_revision))
        drop = set(drop_criterion_ids)
        moved: list[str] = []
        for c in old:
            if c["criterion_id"] in drop:
                await self._memory.supersede_acceptance_criterion(
                    criterion_id=c["criterion_id"], by_revision=int(to_revision),
                    reason=drop_reason or "retired by a later requirement")
                continue
            moved.append(await self._memory.add_acceptance_criterion(
                project_name=slug, revision=int(to_revision), text=c["text"],
                origin_quote=c["origin_quote"], source=c["source"],
                required=bool(c["required"]), verify_kind=c["verify_kind"],
                carried_from=c["criterion_id"]))
            await self._memory.supersede_acceptance_criterion(
                criterion_id=c["criterion_id"], by_revision=int(to_revision),
                reason="carried forward to a later requirement revision")
        return moved

    async def record_verdict(self, *, slug: str, criterion_id: str, verdict: str,
                             detail: str = "", error: str = "",
                             task_id: str | None = None,
                             generation: int | None = None,
                             attempt: int | None = None) -> str:
        """Record one observation, stamped with what it actually examined.

        The revision and artifact digest are read HERE rather than accepted from
        the caller, so evidence cannot be attributed to a revision or an
        implementation the check did not run against.
        """
        if verdict not in RECORDABLE:
            raise ValueError(f"{verdict!r} is not a recordable verdict; "
                             f"expected one of {sorted(RECORDABLE)}")
        req = await self._memory.current_requirement(project_name=slug)
        if req is None:
            raise ValueError(f"no requirement recorded for {slug!r}: there is "
                             "nothing for this evidence to be about")
        digest = implementation_digest(self.project_path(slug))
        return await self._memory.record_acceptance_evidence(
            criterion_id=criterion_id, project_name=slug,
            revision=int(req["revision"]), artifact_digest=digest,
            verdict=verdict, detail=detail, error=error, task_id=task_id,
            generation=generation, attempt=attempt)

    # ── deriving ────────────────────────────────────────────────────────────

    async def evaluate(self, *, slug: str, legacy_status: str = "") -> Verdict:
        """What state this project is in NOW. Derived, never stored."""
        req = await self._memory.current_requirement(project_name=slug)
        revision = int(req["revision"]) if req else 0
        rows = await self._memory.list_acceptance_criteria(
            project_name=slug, revision=revision) if req else []
        criteria = [
            Criterion(criterion_id=r["criterion_id"], text=r["text"],
                      origin_quote=r["origin_quote"], source=r["source"],
                      required=bool(r["required"]), verify_kind=r["verify_kind"],
                      revision=int(r["revision"]),
                      carried_from=str(r.get("carried_from") or ""))
            for r in rows
        ]
        ev_rows = await self._memory.list_acceptance_evidence(
            project_name=slug) if req else []
        evidence = [
            Evidence(criterion_id=e["criterion_id"], verdict=e["verdict"],
                     revision=int(e["revision"]), artifact_digest=e["artifact_digest"],
                     detail=e["detail"], error=e["error"], created_at=e["created_at"],
                     task_id=e["task_id"], generation=e["generation"],
                     attempt=e["attempt"])
            for e in ev_rows
        ]
        path = self.project_path(slug)
        return derive_state(
            has_requirement=req is not None,
            criteria=criteria, evidence=evidence, revision=revision,
            artifact_digest=implementation_digest(path),
            has_implementation=has_implementation(path),
            legacy_status=legacy_status,
        )
