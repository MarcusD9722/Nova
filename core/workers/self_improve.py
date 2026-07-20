from __future__ import annotations

"""Nova's proactive, bounded self-improvement loop.

Two jobs, both paced and killable:
1. CAPTURE — subscribe to the event bus and record real errors into the
   ErrorLog so recurring problems become visible.
2. IMPROVE — on a slow timer (and only while autonomy is enabled), take ONE
   bounded action per cycle:
     - self-correct: for a recurring error, read the relevant code and file a
       fix PROPOSAL for Marcus (never auto-applied to her own source);
     - reflect: distill durable behavioral lessons from recent conversation.

Safety (do not weaken):
- Enabled by NOVA_AUTONOMY (default on) and flippable live via set_enabled()
  (the /autonomy/stop endpoint + UI toggle use this). stop() cancels everything.
- Changes to Nova's OWN code are ALWAYS proposals — apply stays human-gated.
- One action per cycle, long sleep between cycles, in-process de-dup of handled
  error signatures — nothing self-spawns or hammers the GPU.
"""

import asyncio
import json
import os
import re
from datetime import datetime, timezone

from core.dev_mode import DevMode, DevModeError, dev_mode_enabled
from core.error_log import ErrorLog, error_message, is_error_event
from core.event_bus import BUS, clip
from core.llm_runtime import LLMRuntime
from core.logging_setup import get_logger
from core.orchestrator.benchmarks import compute_benchmark_report
from core.orchestrator.internal_state import InternalStateInputs, derive_internal_state
from core.orchestrator.metrics import MetricsCollector
from core.policy._json_extract import extract_first_json_object
from memory.unifier import MemoryUnifier

logger = get_logger(__name__)


def _internal_state_enabled() -> bool:
    return os.getenv("NOVA_INTERNAL_STATE", "1").strip().lower() not in {"0", "false", "no", "off"}


def _self_benchmark_enabled() -> bool:
    return os.getenv("NOVA_SELF_BENCHMARK", "1").strip().lower() not in {"0", "false", "no", "off"}

_CODE_FENCE_RE = re.compile(r"```(?:[a-zA-Z0-9+\-_.]*)\n(.*?)```", re.DOTALL)
_FILE_IN_TRACE_RE = re.compile(r'File "([^"]+\.py)"|([A-Za-z0-9_./\\]+\.py)')


def _enabled_from_env() -> bool:
    return os.getenv("NOVA_AUTONOMY", "1").strip().lower() not in {"0", "false", "no", "off"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SelfImproveWorker:
    def __init__(
        self,
        *,
        memory: MemoryUnifier,
        llm: LLMRuntime,
        llm_semaphore: asyncio.Semaphore,
        dev_mode: DevMode,
        error_log: ErrorLog,
        state_store,
        interval_s: float | None = None,
    ) -> None:
        self._memory = memory
        self._llm = llm
        self._sem = llm_semaphore
        self._dev = dev_mode
        self._errors = error_log
        self._state = state_store
        self._interval = float(interval_s if interval_s is not None else os.getenv("NOVA_SELF_IMPROVE_INTERVAL_S", "1800") or "1800")
        self._enabled = _enabled_from_env()
        self._stop = asyncio.Event()
        self._capture_task: asyncio.Task[None] | None = None
        self._improve_task: asyncio.Task[None] | None = None
        self._bus_q = None
        self._handled_sigs: set[str] = set()
        self._last_reflect_turn = 0
        self._metrics = MetricsCollector()  # Phase 2.5: fed in the capture loop
        self._last_eval_day = ""

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._improve_task and not self._improve_task.done():
            return
        self._stop.clear()
        self._bus_q = BUS.subscribe()
        self._capture_task = asyncio.create_task(self._capture_loop())
        self._improve_task = asyncio.create_task(self._improve_loop())
        logger.info("self_improve_started", enabled=self._enabled, interval_s=self._interval)

    async def stop(self) -> None:
        self._stop.set()
        if self._bus_q is not None:
            BUS.unsubscribe(self._bus_q)
        for t in (self._capture_task, self._improve_task):
            if t:
                t.cancel()
                try:
                    await asyncio.wait_for(t, timeout=5.0)
                except Exception:
                    pass

    def set_enabled(self, value: bool) -> None:
        self._enabled = bool(value)
        BUS.publish("autonomy.state", {"enabled": self._enabled})
        logger.info("self_improve_enabled_changed", enabled=self._enabled)

    def status(self) -> dict:
        return {
            "enabled": self._enabled,
            "interval_s": self._interval,
            "handled_error_signatures": len(self._handled_sigs),
            "dev_mode": dev_mode_enabled(),
        }

    def metrics(self) -> dict:
        """Live self-eval snapshot for /autonomy/metrics (Phase 2.5)."""
        return self._metrics.snapshot()

    async def internal_state(self) -> dict:
        """Nova's current internal operational state (#12): reasoning/operational
        metrics derived from live telemetry — never simulated feelings. Combines
        the self-eval snapshot with live background-work counts."""
        if not _internal_state_enabled():
            return {"enabled": False}
        queued = running = active_goals = 0
        try:
            queued = len(await self._memory.list_tasks(status="queued", limit=200))
            running = len(await self._memory.list_tasks(status="running", limit=200))
            active_goals = len([g for g in await self._memory.list_goals(limit=200)
                                if str(g.get("status") or "") == "active"])
        except Exception as e:  # noqa: BLE001
            logger.debug("internal_state_counts_failed", error=str(e)[:160])
        state = derive_internal_state(InternalStateInputs(
            snapshot=self._metrics.snapshot(),
            queued_tasks=queued,
            running_tasks=running,
            active_goals=active_goals,
        ))
        state["enabled"] = True
        return state

    def operating_hints(self) -> list[str]:
        """Cheap, synchronous operating hints from the in-memory metrics snapshot
        (no DB) — safe to call on every chat turn from the grounding layer. The
        task-count-driven workload term is approximated as 0 here (conservative:
        fewer hints, never more); the full picture lives in internal_state()."""
        if not _internal_state_enabled():
            return []
        try:
            state = derive_internal_state(InternalStateInputs(snapshot=self._metrics.snapshot()))
            return state.get("operating_hints") or []
        except Exception:
            return []

    async def benchmark_report(self, *, days: int = 30) -> dict:
        """Self-benchmark (#14): trends + regressions over the daily self_eval
        history. Read-only, no LLM call — pure computation over facts already
        written by the self-eval snapshot."""
        if not _self_benchmark_enabled():
            return {"enabled": False}
        snaps: list[dict] = []
        try:
            rows = await self._memory.get_facts(entity="self_eval", limit=int(days), newest_first=True)
            for r in rows:
                try:
                    snaps.append(json.loads(r.value))
                except Exception:
                    continue
        except Exception as e:  # noqa: BLE001
            logger.debug("benchmark_read_failed", error=str(e)[:160])
        report = compute_benchmark_report(snaps)
        report["enabled"] = True
        return report

    # ── capture loop ─────────────────────────────────────────────────────────

    async def _capture_loop(self) -> None:
        assert self._bus_q is not None
        while not self._stop.is_set():
            try:
                event = await asyncio.wait_for(self._bus_q.get(), timeout=1.0)
            except (TimeoutError, asyncio.TimeoutError):
                continue
            except asyncio.CancelledError:
                return
            except Exception:
                continue
            try:
                etype = getattr(event, "type", "")
                data = getattr(event, "data", {}) or {}
                # Phase 2.5: feed self-eval metrics (latency, tool failures,
                # empty replies) — observes chat.*/tool.*/vision.* only.
                self._metrics.observe(etype, getattr(event, "ts", ""), data)
                # Don't log our own autonomy chatter as errors.
                if etype.startswith("autonomy.") or etype.startswith("dev."):
                    continue
                if is_error_event(etype, data):
                    await self._errors.record(component=etype, message=error_message(etype, data), context=data)
                # HP1: every real tool invocation, for later usage-pattern detection.
                if etype == "tool.started":
                    tool_name = str(data.get("tool") or "").strip()
                    if tool_name:
                        await self._memory.log_tool_usage(tool_name)
            except Exception:
                continue

    # ── improve loop ─────────────────────────────────────────────────────────

    async def _improve_loop(self) -> None:
        await self._memory.initialize()
        # Small initial delay so startup noise settles before the first cycle.
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=min(60.0, self._interval))
        except (TimeoutError, asyncio.TimeoutError):
            pass
        while not self._stop.is_set():
            try:
                if self._enabled:
                    await self._improve_cycle()
            except asyncio.CancelledError:
                return
            except Exception as e:  # noqa: BLE001
                logger.debug("self_improve_cycle_error", error=str(e)[:200])
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except (TimeoutError, asyncio.TimeoutError):
                pass

    async def _improve_cycle(self) -> None:
        """One bounded action per cycle: prefer fixing a recurring error, else reflect."""
        BUS.publish("autonomy.cycle", {"ts": _now_iso()})

        # 1) Self-correction — a recurring, unhandled error.
        recurring = await self._errors.recurring(min_count=2, limit=5)
        for err in recurring:
            if err["signature"] in self._handled_sigs:
                continue
            handled = await self._self_correct(err)
            self._handled_sigs.add(err["signature"])
            if handled:
                return  # one action per cycle

        # 2) Reflection — distill lessons from recent conversation.
        await self._reflect()

        # 3) Consolidation (Phase 1.4) — merge near-duplicate lessons the
        # reflection pass keeps re-learning. Deterministic, no LLM call.
        try:
            await self._memory.consolidate_lessons()
        except Exception as e:  # noqa: BLE001
            logger.debug("lesson_consolidation_failed", error=str(e)[:160])

        # 3b) Knowledge-graph association discovery (Phase 4 / #8) — infer
        # low-confidence links between nodes that share several neighbors.
        # Deterministic, no LLM call; bounded work per cycle.
        try:
            found = await self._memory.discover_graph_associations()
            if found.get("discovered"):
                BUS.publish("memory.graph_discovery", {"discovered": found["discovered"]})
                logger.info("graph_associations_discovered", count=found["discovered"])
        except Exception as e:  # noqa: BLE001
            logger.debug("graph_discovery_failed", error=str(e)[:160])

        # 4) Self-eval (Phase 2.5) — once per UTC day, snapshot the metrics
        # into a durable fact and announce it on the bus.
        try:
            snap = self._metrics.snapshot()
            if snap["day"] != self._last_eval_day and snap["turns"] > 0:
                self._last_eval_day = snap["day"]
                # #12: capture the derived internal state alongside raw metrics so
                # confidence/uncertainty/workload/etc. can be trended over time.
                try:
                    snap["internal_state"] = await self.internal_state()
                except Exception:
                    pass
                await self._memory.add_fact(
                    entity="self_eval", attribute=snap["day"], value=json.dumps(snap), confidence=1.0
                )
                BUS.publish("autonomy.self_eval", {"day": snap["day"], "turns": snap["turns"],
                            "tool_failure_rate": snap["tool_failure_rate"],
                            "avg_latency_s": snap["reply_latency_s"]["avg"]})
                logger.info("self_eval_recorded", day=snap["day"], turns=snap["turns"])
                # #14: once per day, refresh the benchmark and surface regressions
                # (paced by the daily gate above — never spammy).
                try:
                    if _self_benchmark_enabled():
                        report = await self.benchmark_report()
                        if report.get("regressions"):
                            labels = [r["label"] for r in report["regressions"]]
                            BUS.publish("autonomy.benchmark_regression", {"day": snap["day"], "regressions": labels})
                            logger.info("benchmark_regressions", day=snap["day"], count=len(report["regressions"]))
                            # #6: a real, measured regression becomes a persistent
                            # "improvement" thought to revisit — deterministic, honest.
                            try:
                                await self._memory.note_thought(
                                    "improvement",
                                    f"Benchmark regression on {', '.join(labels)} (as of {snap['day']}) — worth investigating.",
                                    topic="self-benchmark",
                                )
                            except Exception:
                                pass
                except Exception as e:  # noqa: BLE001
                    logger.debug("benchmark_check_failed", error=str(e)[:160])
        except Exception as e:  # noqa: BLE001
            logger.debug("self_eval_failed", error=str(e)[:160])

    # ── self-correction (error -> diagnose -> propose) ───────────────────────

    async def _self_correct(self, err: dict) -> bool:
        if not dev_mode_enabled():
            # Can't file a code proposal without dev mode; surface a note instead.
            BUS.publish("autonomy.note", {
                "kind": "error_needs_dev_mode",
                "message": clip(f"Recurring error I could try to fix if developer mode were on: {err.get('message','')}", 200),
            })
            return False

        target = self._guess_file(err.get("message", ""), err.get("component", ""))
        if not target:
            return False
        try:
            info = await asyncio.to_thread(self._dev.read_file, target)
        except DevModeError:
            return False
        current = str(info.get("content") or "")
        if not current:
            return False

        BUS.publish("autonomy.self_correct", {"file": target, "error": clip(err.get("message", ""), 160)})
        prompt = (
            "You are Nova diagnosing a recurring error in your OWN code and proposing a fix.\n"
            f"Recurring error (seen {err.get('count')}x): {err.get('message','')[:800]}\n\n"
            f"File `{target}`:\n```python\n{current[:8000]}\n```\n\n"
            "If this file is the cause, return the COMPLETE corrected file. Change as little as "
            "possible; keep all existing behavior; do not add imports it lacks. If this file is NOT "
            "the cause, reply with exactly: NOT_THIS_FILE.\n"
            "Reply with ONLY the corrected file in one fenced code block (or NOT_THIS_FILE)."
        )
        async with self._sem:
            raw = await self._llm.chat([{"role": "user", "content": prompt}], max_tokens=4000, temperature=0.1, stop=[], thinking=True)
        raw = (raw or "").strip()
        if "NOT_THIS_FILE" in raw[:60] or not raw:
            return False
        blocks = _CODE_FENCE_RE.findall(raw)
        new_content = (max(blocks, key=len).strip() + "\n") if blocks else ""
        if len(new_content) < max(40, len(current) * 0.4):
            return False  # implausibly short; don't propose a gutted file
        if new_content.strip() == current.strip():
            return False
        try:
            compile(new_content, target, "exec")
        except Exception:
            return False  # never propose code that won't compile
        try:
            p = await asyncio.to_thread(
                self._dev.propose_change, target, new_content,
                f"Auto-diagnosed fix for a recurring error: {err.get('message','')[:160]}", "nova",
            )
            BUS.publish("autonomy.proposal", {"proposal_id": p.id, "file": target, "reason": "self_correction"})
            logger.info("self_correct_proposed", proposal_id=p.id, file=target)
            return True
        except DevModeError:
            return False

    def _guess_file(self, message: str, component: str) -> str | None:
        """Best-effort: pull a repo .py path out of a traceback/message."""
        for m in _FILE_IN_TRACE_RE.finditer(message or ""):
            cand = m.group(1) or m.group(2)
            if not cand:
                continue
            name = cand.replace("\\", "/")
            # Prefer repo-relative paths; skip site-packages/stdlib.
            if "site-packages" in name or "/lib/" in name.lower():
                continue
            idx = name.lower().find("/nova/")
            if idx >= 0:
                name = name[idx + len("/nova/"):]
            return name
        # component like "memory.semantic_index" isn't a path; give up gracefully.
        return None

    # ── reflection (distill lessons) ─────────────────────────────────────────

    async def _reflect(self) -> None:
        try:
            transcript = await self._recent_transcript()
        except Exception:
            transcript = ""
        if not transcript or len(transcript) < 200:
            return
        BUS.publish("autonomy.reflect", {"chars": len(transcript)})
        prompt = (
            "You are Nova reflecting on a recent conversation with Marcus to learn how to work with him "
            "better. Extract only DURABLE preferences or corrections he expressed about how you should "
            "behave (tone, format, what to do or avoid). Ignore one-off task content and facts. Also name, "
            "in a few words, what he's mainly been focused on/interested in during this conversation (a "
            "project, hobby, topic) — leave it empty if nothing stands out.\n\n"
            f"Conversation:\n{transcript[:4000]}\n\n"
            'Reply ONLY with JSON: {"lessons": ["short imperative lesson", ...], "interest_focus": "short '
            'phrase or empty string"} (0-3 lessons; empty array if none).'
        )
        async with self._sem:
            raw = await self._llm.chat([{"role": "user", "content": prompt}], max_tokens=500, temperature=0.2, thinking=True)
        obj = extract_first_json_object(raw or "") or {}
        lessons = obj.get("lessons") if isinstance(obj, dict) else None
        if isinstance(lessons, list):
            for l in lessons[:3]:
                text = str(l).strip()
                if 6 <= len(text) <= 300:
                    try:
                        await self._memory.add_lesson(text, topic="reflection")
                        BUS.publish("autonomy.lesson", {"lesson": clip(text, 160)})
                    except Exception:
                        continue

        focus = str(obj.get("interest_focus") or "").strip() if isinstance(obj, dict) else ""
        if 3 <= len(focus) <= 200:
            try:
                await self._memory.record_interest_focus(focus)
            except Exception:
                pass

    async def _recent_transcript(self) -> str:
        try:
            return await self._memory.recent_turns_text(limit=30)
        except Exception:
            return ""
