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

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from core.completion import (
    Criterion, Evidence, FAILED, HUMAN_PENDING, INCONCLUSIVE, PASSED, WAIVED,
    Verdict, derive_state,
)
from core.completion_artifacts import (
    declare_scaffold, has_implementation, implementation_digest,
)
from core.completion_contract import is_span_of, uncovered_clauses
from core.logging_setup import get_logger

logger = get_logger(__name__)

#: Verdicts a MACHINE check may record. `pending` is not here — pending is the
#: absence of evidence, and a row saying nothing happened would make "nothing
#: was checked" indistinguishable from "something was checked and said
#: nothing". `waived` is not here either: accepting a criterion on a person's
#: behalf is not a machine's to do, and it has its own path.
RECORDABLE = frozenset({PASSED, FAILED, HUMAN_PENDING, INCONCLUSIVE})


@dataclass(frozen=True)
class CheckContext:
    """What a check was started against — captured BEFORE it runs.

    Evidence used to be stamped with whatever was true when the RESULT came
    back, which is a different moment from the one the check examined. A check
    that started against H1 and returned after the code became H2 was recorded
    as evidence about H2, so a pass earned against code that no longer existed
    certified code nobody had tested. Same for a correction landing mid-check:
    an R1 result was filed under R2.

    Immutable, and produced only by `begin_check`, so a caller cannot assemble
    one that claims to have examined something it did not.
    """

    slug: str
    criterion_id: str
    revision: int
    artifact_digest: str
    started_at: str


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
        req = await self._requirement_at(slug, revision)
        specs: list[dict[str, Any]] = []
        for spec in criteria:
            text = str(spec.get("text") or "").strip()
            quote = str(spec.get("origin_quote") or "").strip()
            if not text:
                continue
            if not quote:
                raise ValueError(
                    f"criterion {text!r} has no origin_quote: a criterion that "
                    "cannot point at the request is not an acceptance criterion")
            # The quote must be words the user actually wrote, at THIS revision.
            # Without this the trace is decoration: a criterion could cite
            # anything and still look sourced.
            if not is_span_of(quote, req["request_text"]):
                raise ValueError(
                    f"criterion {text!r} quotes {quote!r}, which is not a span "
                    f"of the request at revision {revision}: "
                    f"{req['request_text']!r}")
            specs.append({"text": text, "origin_quote": quote,
                          "source": str(spec.get("source") or "user"),
                          "required": bool(spec.get("required", True)),
                          "verify_kind": str(spec.get("verify_kind") or "machine"),
                          "carried_from": spec.get("carried_from")})
        # EVERY quote is validated before ANY row is written, and the write is
        # one transaction. A batch that aborts halfway used to leave a contract
        # that was neither the old one nor the new one, and the criteria that
        # never got written leave no trace of their own absence.
        ids = await self._memory.add_acceptance_criteria_batch(
            project_name=slug, revision=int(revision), specs=specs)
        logger.info("completion_criteria_recorded", project=slug,
                    revision=revision, count=len(ids))
        return ids

    async def _requirement_at(self, slug: str, revision: int) -> dict[str, Any]:
        req = await self._memory.current_requirement(project_name=slug)
        if req is None:
            raise ValueError(f"no requirement recorded for {slug!r}")
        if int(req["revision"]) != int(revision):
            raise ValueError(
                f"revision {revision} is not the current requirement for "
                f"{slug!r} (that is {req['revision']}); a contract may only be "
                "written for the requirement in force")
        return req

    async def seal_contract(self, *, slug: str, revision: int) -> list[str]:
        """Agree that these criteria are the WHOLE of what was asked.

        Refuses while any separately-asked-for clause of the request is quoted
        by no criterion. That is the check that stops an incomplete
        decomposition from completing: each criterion can be individually
        sound and the set can still miss half the request, and nothing about
        the criteria themselves reveals a capability that is simply absent.

        Returns the clauses it verified as covered.
        """
        req = await self._requirement_at(slug, revision)
        rows = await self._memory.list_acceptance_criteria(
            project_name=slug, revision=int(revision))
        if not rows:
            raise ValueError(
                f"cannot seal an empty contract for {slug!r} revision {revision}")
        missing = uncovered_clauses(req["request_text"],
                                    [r["origin_quote"] for r in rows])
        if missing:
            raise ValueError(
                f"the acceptance contract for {slug!r} revision {revision} does "
                f"not cover everything that was asked for. No criterion quotes: "
                + "; ".join(repr(m) for m in missing))
        await self._memory.seal_requirement(project_name=slug,
                                            revision=int(revision))
        logger.info("completion_contract_sealed", project=slug, revision=revision,
                    criteria=len(rows))
        return [r["origin_quote"] for r in rows]

    async def declare_scaffold(self, *, slug: str, paths: Sequence[str]) -> set[str]:
        """Record that Nova wrote these files as validation scaffolding.

        Scaffolding is excluded from the artifact fence by PROVENANCE. Guessing
        from a filename was wrong exactly when it mattered: a user who asks for
        a test runner owns `test_engine.py`, and excluding it made their own
        code invisible to the fence.
        """
        return declare_scaffold(self.project_path(slug), list(paths))

    async def begin_check(self, *, slug: str, criterion_id: str) -> CheckContext:
        """Capture what a check is about to examine, before it examines it."""
        req = await self._memory.current_requirement(project_name=slug)
        if req is None:
            raise ValueError(f"no requirement recorded for {slug!r}: there is "
                             "nothing for a check to be about")
        return CheckContext(
            slug=slug, criterion_id=criterion_id, revision=int(req["revision"]),
            artifact_digest=implementation_digest(self.project_path(slug)),
            started_at=datetime.now(timezone.utc).isoformat())

    async def carry_forward(self, *, slug: str, from_revision: int,
                            to_revision: int,
                            drop_criterion_ids: Sequence[str] = (),
                            drop_reason: str = "",
                            reanchor: dict[str, str] | None = None) -> list[str]:
        """Move an unchanged acceptance contract onto a new revision.

        A correction usually changes PART of what was asked. The criteria that
        did not change still stand, and re-recording them by hand at every
        revision is how one quietly goes missing. Anything genuinely retired
        must be named in `drop_criterion_ids` — an explicit, attributable act,
        which is the only way a criterion may leave the contract.

        Evidence is NOT carried forward. It was gathered under the old revision
        and has to be re-established, which is exactly what "the requirements
        changed" should mean.

        RE-ANCHORING. A criterion's quote must be a span of the request it
        belongs to, and the new revision is new words. When the user EXTENDED
        the request ("...and multiply"), the old quote is still there and the
        criterion carries unchanged. When they REPHRASED it ("just addition
        now"), it is not, and `reanchor` must supply the span of the new
        request this criterion now traces to. Guessing would be the same
        circularity in miniature: assuming a criterion still traces to a
        request nobody checked it against.
        """
        req = await self._requirement_at(slug, to_revision)
        anchors = dict(reanchor or {})
        old = await self._memory.list_acceptance_criteria(
            project_name=slug, revision=int(from_revision))
        drop = set(drop_criterion_ids)

        keep: list[dict[str, Any]] = []
        for c in old:
            if c["criterion_id"] in drop:
                continue
            quote = anchors.get(c["criterion_id"]) or c["origin_quote"]
            if not is_span_of(quote, req["request_text"]):
                raise ValueError(
                    f"criterion {c['text']!r} quotes {quote!r}, which is not a "
                    f"span of the request at revision {to_revision}. Pass "
                    f"reanchor={{{c['criterion_id']!r}: '<span of the new "
                    f"request>'}} to carry it forward, or drop it explicitly.")
            keep.append({"text": c["text"], "origin_quote": quote,
                         "source": c["source"], "required": bool(c["required"]),
                         "verify_kind": c["verify_kind"],
                         "carried_from": c["criterion_id"]})

        # Validate the whole batch before writing any of it, then write it in
        # one transaction, for the same reason set_criteria does.
        moved = await self._memory.add_acceptance_criteria_batch(
            project_name=slug, revision=int(to_revision), specs=keep)
        for c in old:
            reason = (drop_reason or "retired by a later requirement"
                      if c["criterion_id"] in drop
                      else "carried forward to a later requirement revision")
            await self._memory.supersede_acceptance_criterion(
                criterion_id=c["criterion_id"], by_revision=int(to_revision),
                reason=reason)
        return moved

    async def record_verdict(self, *, context: CheckContext, verdict: str,
                             detail: str = "", error: str = "",
                             task_id: str | None = None,
                             generation: int | None = None,
                             attempt: int | None = None) -> str:
        """Record one machine observation, stamped with what it EXAMINED.

        The revision and digest come from the context captured before the check
        ran, not from whatever is true now. That is the whole point: a result
        belongs to the state of the world it was produced against, and filing
        it under a later one is how a pass certifies code nobody tested.
        """
        if verdict not in RECORDABLE:
            if verdict == WAIVED:
                raise ValueError(
                    "a machine check cannot waive a criterion: accepting one "
                    "on a person's behalf is not a machine's to do. Use "
                    "record_human_decision, which requires naming who decided.")
            raise ValueError(f"{verdict!r} is not a recordable verdict; "
                             f"expected one of {sorted(RECORDABLE)}")
        return await self._memory.record_acceptance_evidence(
            criterion_id=context.criterion_id, project_name=context.slug,
            revision=context.revision, artifact_digest=context.artifact_digest,
            verdict=verdict, detail=detail, error=error, task_id=task_id,
            generation=generation, attempt=attempt)

    async def record_human_decision(self, *, slug: str, criterion_id: str,
                                    accepted: bool, actor: str,
                                    detail: str = "") -> str:
        """A person accepted or rejected a criterion. The ONLY path to a waiver.

        `actor` is required and recorded, so an acceptance can always be
        attributed to somebody. A decision is judged against what is on disk at
        the moment it is given — unlike a machine check, a person looked at the
        thing as it is now, so the current digest is the honest stamp.
        """
        who = str(actor or "").strip()
        if not who:
            raise ValueError("a human decision must name who made it")
        req = await self._memory.current_requirement(project_name=slug)
        if req is None:
            raise ValueError(f"no requirement recorded for {slug!r}")
        note = f"accepted by {who}" if accepted else f"rejected by {who}"
        return await self._memory.record_acceptance_evidence(
            criterion_id=criterion_id, project_name=slug,
            revision=int(req["revision"]),
            artifact_digest=implementation_digest(self.project_path(slug)),
            verdict=WAIVED if accepted else FAILED,
            detail=f"{note}{': ' + detail if detail else ''}", error="")

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
            contract_sealed=bool(req and req.get("sealed_at")),
            legacy_status=legacy_status,
        )
