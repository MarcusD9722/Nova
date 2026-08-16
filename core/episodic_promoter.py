from __future__ import annotations

"""One place that decides what becomes durable memory (V3 P4.2).

P4 wrote the promotion rules. P4.1 wired exactly one caller to them — the hot
artifact store — so a tool result became an episode and nothing else did. The
other four signals `worth_remembering()` accepts (`user_selected`,
`is_correction`, `is_failure`, `is_decision`) were reachable only from unit
tests: the live hook passed `user_text`, `tool` and `result_items` and let the
rest default to False.

The fix is deliberately not "pass more booleans". Each of those signals already
exists somewhere in production, computed by something that had better evidence
than a guess:

    selection    ArtifactStore.resolve() already determined WHICH item
                 "the second one" means, deterministically, on the turn path.
    correction   MemoryUnifier publishes `memory.corrected` (with was/now) and
                 the ingest worker publishes `memory.superseded` after its
                 contradiction judgement. Fact memory already did this work.
    failure      ErrorLog already normalises errors into recurrence signatures,
                 and ToolRouter already swallows transients that succeed on
                 retry. `project.error` is already a material, user-visible
                 event.
    project      ProjectBuilder already publishes started / completed / error,
                 distinct from the `project.progress` ticks.

So this module is a router over evidence that exists, not a second detector.
Nothing here calls a model, and nothing here writes to SQLite: every path ends
at `submit()`, which is the P4.1 queue, drained by the P4.1 worker. That keeps
D7's real guarantee — one promotion policy, one durable write path — while
dropping its accidental assumption that every event is artifact-backed.

Two things a correction is NOT:

    FACT      Marcus's GPU is an RTX 5080.          (fact memory owns this)
    EPISODE   On the 14th Marcus corrected his GPU from a 3080 to a 5080.

Collapsing them would either turn the fact table into a transcript or lose the
date the belief changed.
"""

import asyncio
import hashlib
import time
from collections import OrderedDict
from contextlib import suppress
from datetime import datetime, timezone
from typing import Any, Callable

from core.error_log import ErrorLog, error_message, is_error_event
from core.event_bus import BUS
from core.events import EpisodicPersistEvent
from contextlib import nullcontext as _no_scope

from core.turn_identity import (OWNER_ENTITY, active_turn, current_identity,
                                current_identity_or_none)


def _speaker_name() -> str:
    """How a user-originated episode names whoever did the thing.

    "Marcus chose the WD Gold" is exactly right until Alice can produce a real
    turn, at which point it is a fabricated quote about the wrong person. An
    unattributed turn gets neutral wording rather than a guess — Nova does not
    manufacture Marcus merely because an event lacks attribution, and off the
    turn path entirely she says nobody's name (V3 P5.1e.1).
    """
    ident = current_identity_or_none()
    if ident is None:
        return "Nova"
    if ident.is_owner:
        return "Marcus"
    if ident.is_known_other and ident.display_name:
        return ident.display_name
    return "The user"


def _correction_subject(entity: str, attribute: str) -> str:
    """"favorite_color", not "speaker:p-alice favorite_color".

    Strips the SPEAKER'S OWN personal root — structurally, from turn identity,
    not by pattern-matching entity strings. That distinction matters: a blind
    `replace("speaker:", "")` would also rewrite a subject that legitimately
    mentions a different speaker, turning "Alice corrected Bob's note" into
    something Nova never observed.

    Provenance keeps the true entity; this only affects the sentence.
    """
    ent = str(entity or "").strip()
    attr = str(attribute or "").strip()
    ident = current_identity_or_none()
    own = ident.memory_entity if ident is not None else OWNER_ENTITY
    if own and ent.lower() == str(own).lower():
        return attr                       # their own root: just the attribute
    if own and ent.lower().startswith(str(own).lower() + ":"):
        return f"{ent[len(str(own)) + 1:]} {attr}".strip()   # their sub-namespace
    if ent.lower() == OWNER_ENTITY:
        return attr                       # legacy "user name" -> "name"
    return f"{ent} {attr}".strip()

from core.logging_setup import get_logger
from core.workers.lifecycle import log_worker_error, stop_worker
from memory.episodes import (EP_CORRECTION, EP_FAILURE, EP_MCP_RESULT, EP_PROJECT,
                             EP_SELECTION, EP_TOOL_RESULT)
from memory.episodic_recall import importance_for, worth_remembering

logger = get_logger(__name__)

#: How many times one error signature must recur before it is worth
#: remembering forever.
#:
#: Not 1. `ToolRouter.execute` already retries once and only publishes
#: `tool.error` after every attempt failed, so a transient that recovered never
#: reaches here at all — meaning a single event is already "failed for real,
#: once". That is still not a durable life event; a flaky endpoint would fill
#: memory with identical entries. Three occurrences of the SAME normalised
#: signature is a pattern, and it is the same threshold shape the
#: self-improvement loop uses to decide something is worth a fix proposal.
#:
#: Material project failures bypass this entirely: `project.error` is
#: user-visible and project-relevant on the first occurrence.
FAILURE_RECURRENCE = 3

#: Bounded signature counter. Memory of "how often has this failed" must not
#: itself become a leak in a process that runs for weeks.
_MAX_SIGNATURES = 512

#: Bus prefixes that never produce episodes.
#:
#: `autonomy.` / `dev.` are Nova talking to herself. `permission.` is a refusal
#: working correctly — a denied capability is policy, not failure. `project.` is
#: handled explicitly below, because `project.progress` fires many times per
#: build and only started/completed/error are milestones.
_IGNORED_PREFIXES = ("autonomy.", "dev.", "permission.", "chat.", "voice.",
                     "tts.", "stt.", "memory.recall_gate")


def _digest(*parts: Any) -> str:
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:16]


class EpisodicPromoter:
    """Decides. Never writes.

    `submit` is injected rather than the worker being owned here, so this stays
    pure policy and there remains exactly one thing in the process that writes
    episodes.
    """

    def __init__(self, *, submit: Callable[[EpisodicPersistEvent], bool],
                 enabled: bool = True) -> None:
        self._submit = submit
        self._enabled = bool(enabled)
        self._signatures: "OrderedDict[str, int]" = OrderedDict()
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._queue: "asyncio.Queue[Any] | None" = None
        self.stats: dict[str, int] = {
            "artifact": 0, "selection": 0, "correction": 0, "failure": 0,
            "project": 0, "rejected": 0, "errors": 0, "undrained": 0,
        }

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Subscribe to the bus. Off the turn path by construction: publishers
        fire and forget, and this drains in its own task."""
        if not self._enabled or (self._task and not self._task.done()):
            return
        self._stop.clear()
        self._queue = BUS.subscribe()
        self._task = asyncio.create_task(self._run())
        logger.info("episodic_promoter_started")

    async def stop(self) -> None:
        """Drain before stopping. P4.2 shipped this the wrong way round.

        The original set the stop flag first, and `_run` checks it at the TOP of
        the loop — so everything still queued was discarded. Measured: 12 events
        queued, 1 processed. At system level it usually survived anyway, because
        the worker that stops earlier in the sequence yields for long enough that
        the promoter drains by coincidence. Surviving by coincidence is not a
        durability guarantee, and it does not hold for events published by the
        producers that stop AFTER this one.

        The order below is the fix, and each step exists for a reason:
        """
        q = self._queue
        # 1. Unsubscribe FIRST, so the queue stops growing while we drain it.
        #    A drain that races new arrivals has no defined end.
        if q is not None:
            BUS.unsubscribe(q)
        # 2. Drain what was already accepted. Synchronous — `on_bus_event` does
        #    no I/O — which means the consumer task cannot interleave and there
        #    is no race to reason about.
        if q is not None:
            self._drain_remaining(q)
        # 3. Only now stop the consumer.
        self._stop.set()
        await stop_worker(self._task, name="episodic-promoter")
        self._queue = None

    def _drain_remaining(self, q: "asyncio.Queue[Any]", *,
                         max_events: int = 5000, budget_s: float = 5.0) -> int:
        """Process everything still queued, bounded, reporting what it could not.

        Deliberately NOT `queue.join()`. join() waits on `task_done()` accounting
        that must be exactly right on every path — including rejected and
        malformed events — and gets it wrong exactly once to hang shutdown
        forever. Draining here needs no accounting to be correct and cannot
        deadlock: it takes what is there and stops.
        """
        drained = 0
        deadline = time.monotonic() + budget_s
        while drained < max_events:
            try:
                event = q.get_nowait()
            except asyncio.QueueEmpty:
                break
            except Exception:  # noqa: BLE001
                break
            try:
                self.on_bus_event(getattr(event, "type", ""),
                                  getattr(event, "data", {}) or {},
                                  getattr(event, "ts", ""),
                                  identity=getattr(event, "identity", None))
                drained += 1
            except Exception as e:  # noqa: BLE001
                self.stats["errors"] += 1
                log_worker_error(logger, "episodic_promoter_drain_failed", e)
            finally:
                # Keep the accounting honest even though nothing joins on it —
                # a queue left with unfinished tasks is a trap for whoever adds
                # a join() later.
                with suppress(ValueError):
                    q.task_done()
            if time.monotonic() > deadline:
                break

        pending = q.qsize()
        if pending:
            # Never claim a drain finished when it did not.
            self.stats["undrained"] += pending
            logger.warning("episodic_promoter_drain_incomplete",
                           drained=drained, pending=pending)
        elif drained:
            logger.info("episodic_promoter_drained", events=drained)
        return drained

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=0.5)
            except (TimeoutError, asyncio.TimeoutError):
                continue
            except asyncio.CancelledError:
                return
            except Exception:
                continue
            try:
                self.on_bus_event(getattr(event, "type", ""),
                                  getattr(event, "data", {}) or {},
                                  getattr(event, "ts", ""),
                                  identity=getattr(event, "identity", None))
            except Exception as e:  # noqa: BLE001
                self.stats["errors"] += 1
                log_worker_error(logger, "episodic_promoter_failed", e)
            finally:
                with suppress(ValueError):
                    self._queue.task_done()

    # ── artifact-backed events (the P4.1 path, unchanged in behaviour) ───────

    def note_artifact(self, artifact: Any, children: list, *,
                      user_text: str = "", project: str | None = None) -> None:
        if not self._enabled:
            return
        tool = artifact.source_tool or ""
        promote, why = worth_remembering(
            user_text=user_text, tool=tool, result_items=len(children))
        if not promote:
            self.stats["rejected"] += 1
            return
        kind = EP_MCP_RESULT if tool.startswith("mcp:") else EP_TOOL_RESULT
        if self._submit(EpisodicPersistEvent(
            identity=current_identity_or_none(),
            conversation_id=artifact.conversation_id,
            turn_id=artifact.turn_id,
            timestamp=datetime.now(timezone.utc),
            artifact=artifact,
            children=list(children),
            user_text=user_text,
            reason=why,
            kind=kind,
            project=project,
            importance=importance_for(kind, has_items=bool(children)),
        )):
            self.stats["artifact"] += 1

    # ── selection ────────────────────────────────────────────────────────────

    def note_selection(self, *, selected: Any, parent: Any, items: list,
                       conversation_id: str, turn_id: str, user_text: str,
                       project: str | None = None) -> None:
        """Marcus picked one of the things Nova showed him.

        Called only when BOTH halves of the evidence are present: the hot
        resolver already decided which artifact, and the wording expressed a
        choice rather than a question. No model is asked to rediscover either.

        The result set is NOT copied. The selection is its own small episode
        pointing at the artifact, and the original result-set episode is
        reinforced through P4's access mechanism — so "which drive did I
        choose" and "what drives did we look at" stay different questions with
        different answers.
        """
        if not self._enabled or selected is None:
            return
        # Identity is the SELECTED ARTIFACT. Saying "yeah, that one" three times
        # about the same drive is one decision, not three.
        ep_id = f"sel-{selected.artifact_id}"
        title = getattr(selected, "title", "") or selected.summary
        where = f" from {parent.summary}" if parent is not None else ""
        provenance = {
            "artifact_id": selected.artifact_id,
            "parent_id": selected.parent_id or (parent.artifact_id if parent else None),
            "item_index": selected.item_index,
            "source_tool": selected.source_tool,
            "said": (user_text or "")[:200],
            "turn_id": turn_id,
        }
        if self._submit(EpisodicPersistEvent(
            identity=current_identity_or_none(),
            conversation_id=conversation_id,
            turn_id=turn_id,
            timestamp=datetime.now(timezone.utc),
            # Deliberately NOT `artifact=selected`. That row already exists —
            # it was written as a child of the result set — and re-persisting it
            # would rewrite its `episode_id` to point at this selection instead
            # of the set it belongs to. The choice REFERENCES the artifact; it
            # does not own it. Its identity, position, trust and freshness are
            # carried explicitly below.
            artifact=None,
            episode_id=ep_id,
            trust=selected.trust,
            freshness=selected.freshness,
            source_tool=selected.source_tool or None,
            summary=f"{_speaker_name()} chose {title}{where}",
            entities=[title] + [str(getattr(i, "title", "")) for i in items[:4]
                                if getattr(i, "title", "") and i is not selected],
            user_text=user_text,
            reason="user selected or referenced this",
            kind=EP_SELECTION,
            project=project,
            importance=importance_for(EP_SELECTION),
            provenance=provenance,
            outcome=f"chose {title}",
            # Credit the result set rather than duplicating it.
            reinforce=[f"ep-{selected.parent_id}"] if selected.parent_id else [],
            # Changing your mind within one comparison replaces the earlier
            # choice. Scoped to THIS result set: a live choice from an unrelated
            # comparison must not be retired by it.
            supersede_scope=selected.parent_id or None,
        )):
            self.stats["selection"] += 1

    # ── bus-sourced events ───────────────────────────────────────────────────

    def on_bus_event(self, event_type: str, data: dict, ts: str = "",
                     identity: Any = None) -> None:
        """Route one published event. Synchronous, deterministic, no I/O.

        `identity` is the snapshot the bus took at PUBLISH time. This method
        runs on the promoter's own draining task, so `current_identity()` here
        would be the typed default and would file a guest's correction as
        Marcus's — the D12 lesson, one layer down (V3 P5.1e).
        """
        if not self._enabled or not event_type:
            return
        etype = str(event_type)
        if any(etype.startswith(p) for p in _IGNORED_PREFIXES):
            return

        with active_turn(identity) if identity is not None else _no_scope():
            self._route(etype, data, ts)

    def _route(self, etype: str, data: dict, ts: str) -> None:
        if etype == "memory.corrected":
            self._note_correction(data, ts, explicit=True)
        elif etype == "memory.superseded":
            self._note_correction(data, ts, explicit=False)
        elif etype in ("project.started", "project.completed"):
            self._note_project(etype, data, ts)
        elif etype == "project.error":
            self._note_project_failure(data, ts)
        elif etype.startswith("project."):
            return  # project.progress and friends: internal ticks, never events
        elif is_error_event(etype, data):
            self._note_failure(etype, data, ts)

    def _note_correction(self, data: dict, ts: str, *, explicit: bool) -> None:
        """Nova learned that something she believed was wrong.

        Fact memory has already done the hard part — decided that a
        contradiction occurred and retired the old value. This records that it
        HAPPENED, and when, which the resulting fact cannot say about itself.
        """
        if explicit:
            entity = str(data.get("entity") or "").strip()
            attribute = str(data.get("attribute") or "").strip()
            was = str(data.get("was") or "").strip()
            now = str(data.get("now") or "").strip()
            if not (entity and attribute and now):
                return
            # No change is not a correction: re-stating the same value supersedes
            # a row without anything having been wrong.
            if was and was.lower() == now.lower():
                self.stats["rejected"] += 1
                return
            subject = _correction_subject(entity, attribute)
            who = _speaker_name()
            summary = (f"{who} corrected {subject}: {was} -> {now}" if was
                       else f"{who} corrected {subject} to {now}")
            ep_id = f"corr-{_digest(entity, attribute, now)}"
            entities = [e for e in (entity, attribute, was, now) if e]
            provenance = {"entity": entity, "attribute": attribute,
                          "was": was or None, "now": now, "signal": "memory.corrected"}
            outcome = f"{subject} is now {now}"
        else:
            because = str(data.get("because") or "").strip()
            retired = int(data.get("retired") or 0)
            if not because or retired <= 0:
                return
            summary = f"Nova retired {retired} outdated belief(s) after Marcus said: {because}"
            ep_id = f"corr-{_digest(because, retired)}"
            entities = [because[:80]]
            provenance = {"retired": retired, "because": because,
                          "signal": "memory.superseded"}
            outcome = None

        if self._submit(EpisodicPersistEvent(
            identity=current_identity_or_none(),
            conversation_id="", turn_id="", timestamp=datetime.now(timezone.utc),
            episode_id=ep_id, summary=summary, entities=entities,
            reason="user corrected a belief", kind=EP_CORRECTION,
            importance=importance_for(EP_CORRECTION),
            provenance={**provenance, "at": ts},
            outcome=outcome,
        )):
            self.stats["correction"] += 1

    def _note_project(self, etype: str, data: dict, ts: str) -> None:
        slug = str(data.get("project") or "").strip()
        if not slug:
            return
        started = etype.endswith("started")
        mode = str(data.get("mode") or "").strip()
        if started:
            what = "started improving" if mode == "improve" else "started building"
            detail = str(data.get("brief") or "")[:200]
            outcome = None
        else:
            status = str(data.get("status") or "ok").strip()
            what = f"finished {slug} ({status})"
            detail = str(data.get("summary") or "")[:200]
            outcome = status
        summary = (f"Nova {what} {slug}" if started else f"Nova {what}")
        if detail:
            summary += f": {detail}"
        if self._submit(EpisodicPersistEvent(
            identity=current_identity_or_none(),
            conversation_id="", turn_id="", timestamp=datetime.now(timezone.utc),
            # A build can legitimately happen many times for one project, so
            # identity includes the moment. The bus timestamp is fixed at
            # publish, so a redelivery of the same event is idempotent.
            episode_id=f"proj-{slug}-{'start' if started else 'end'}-{_digest(ts)}",
            summary=summary,
            entities=[slug] + ([str(f) for f in (data.get("files") or [])[:4]]),
            reason="project milestone", kind=EP_PROJECT, project=slug,
            importance=importance_for(EP_PROJECT),
            provenance={"project": slug, "event": etype, "at": ts,
                        "status": data.get("status"), "mode": mode or None,
                        "test_note": str(data.get("test_note") or "")[:200] or None},
            outcome=outcome,
        )):
            self.stats["project"] += 1

    def _note_project_failure(self, data: dict, ts: str) -> None:
        """A build that failed is material on the FIRST occurrence.

        Unlike a generic error it is user-visible, project-scoped, and the thing
        Marcus will ask about next time ("why did that build fail?"). It does
        not wait for recurrence.
        """
        slug = str(data.get("project") or "").strip()
        error = str(data.get("error") or "").strip()
        if not (slug and error):
            return
        if self._submit(EpisodicPersistEvent(
            identity=current_identity_or_none(),
            conversation_id="", turn_id="", timestamp=datetime.now(timezone.utc),
            episode_id=f"fail-proj-{slug}-{_digest(ErrorLog.signature(slug, error))}",
            summary=f"Building {slug} failed: {error[:180]}",
            entities=[slug, error[:80]],
            reason="failure worth not repeating", kind=EP_FAILURE, project=slug,
            importance=importance_for(EP_FAILURE),
            provenance={"project": slug, "error": error[:400], "event": "project.error",
                        "at": ts, "signature": ErrorLog.signature(slug, error)},
            outcome="failed",
        )):
            self.stats["failure"] += 1

    def _note_failure(self, etype: str, data: dict, ts: str) -> None:
        """A generic error, promoted only once it has become a PATTERN.

        Reuses `ErrorLog.signature` rather than re-normalising: if the two
        disagreed about what "the same error" means, the self-improvement loop
        and episodic memory would tell Marcus different stories about the same
        fault.
        """
        message = error_message(etype, data)
        if not message:
            return
        signature = ErrorLog.signature(etype, message)

        count = self._signatures.get(signature, 0) + 1
        self._signatures[signature] = count
        self._signatures.move_to_end(signature)
        while len(self._signatures) > _MAX_SIGNATURES:
            self._signatures.popitem(last=False)

        if count < FAILURE_RECURRENCE:
            self.stats["rejected"] += 1
            return
        # Identity is the SIGNATURE, so the fourth and fiftieth occurrence
        # update one episode instead of adding forty-seven.
        if self._submit(EpisodicPersistEvent(
            identity=current_identity_or_none(),
            conversation_id="", turn_id="", timestamp=datetime.now(timezone.utc),
            episode_id=f"fail-{_digest(signature)}",
            summary=f"Recurring failure in {etype} ({count}x): {str(message)[:160]}",
            entities=[etype, str(message)[:80]],
            reason="failure worth not repeating", kind=EP_FAILURE,
            importance=importance_for(EP_FAILURE),
            provenance={"event": etype, "signature": signature, "count": count,
                        "at": ts, "tool": data.get("tool")},
            outcome="recurring",
        )):
            self.stats["failure"] += 1

    # ── observability ────────────────────────────────────────────────────────

    def status(self) -> dict[str, Any]:
        return {**self.stats, "tracked_signatures": len(self._signatures),
                "listening": bool(self._task and not self._task.done())}
