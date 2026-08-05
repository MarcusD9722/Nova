from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from core.logging_setup import get_logger
from core.policy._json_extract import extract_first_json_object
from core.tool_router import ToolCall, ToolRouter
from core.workers.lifecycle import log_worker_error, stop_worker
from core.llm_runtime import LLMRuntime
from memory.unifier import MemoryUnifier

logger = get_logger(__name__)


@dataclass
class SupervisorConfig:
    tick_seconds: float = 1.0
    task_timeout_s: float = 30.0
    max_retries: int = 2
    # Hard budget of executed steps per goal. Each "tool" decision re-enqueues
    # a new __decide__ step, so without a cap a goal the model never declares
    # "final" loops forever (one LLM call per second).
    max_steps_per_goal: int = 24


class AgentSupervisor:
    """
    Background supervisor for Nova's hybrid near-autonomous behavior.

    Design goals:
    - Deterministic, bounded loop (no infinite LLM self-chat).
    - Durable task queue (SQLite) with retry + backoff.
    - Only claims completion when backed by tool results.
    """

    def __init__(
        self,
        *,
        memory: MemoryUnifier,
        llm: LLMRuntime,
        router: ToolRouter,
        tool_descriptions: dict[str, str],
        cfg: SupervisorConfig | None = None,
    ) -> None:
        self._memory = memory
        self._llm = llm
        self._router = router
        self._tool_descriptions = dict(tool_descriptions)
        self._cfg = cfg or SupervisorConfig()
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run_loop())
        logger.info("agent_supervisor_started")

    async def stop(self) -> None:
        self._stop.set()
        await stop_worker(self._task, name="agent-supervisor")

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    def _iso(self, dt: datetime) -> str:
        return dt.isoformat()

    async def _enqueue_decide(self, *, goal_id: UUID, project_name: str) -> None:
        await self._memory.enqueue_goal_task(
            goal_id=goal_id,
            project_name=project_name,
            tool_name="__decide__",
            args={},
        )

    async def _decide_next(self, *, goal_id: str, project_name: str) -> dict[str, Any]:
        """
        Ask the LLM for the next step.

        Output contract (strict JSON):
        - {"type":"tool","name":"<tool_name>","args":{...}}
        - {"type":"final","message":"..."}
        - {"type":"question","message":"..."}  # needs user input; pauses goal
        """
        tools_list = "\n".join([f"- {n}: {self._tool_descriptions.get(n, '')}" for n in self._router.list_tools()])
        recent_tasks = await self._memory.list_goal_tasks(goal_id=goal_id, limit=12)
        # Compact history for the model
        history_lines: list[str] = []
        for t in reversed(recent_tasks):
            status = t.get("status", "")
            tool = t.get("tool_name", "")
            err = (t.get("last_error") or "").strip()
            if status in ("done", "failed"):
                if err:
                    history_lines.append(f"{status.upper()} {tool}: {err[:200]}")
                else:
                    history_lines.append(f"{status.upper()} {tool}")
        history = "\n".join(history_lines[-8:])

        # Pull the goal details
        goals = await self._memory.list_goals(project_name=project_name, limit=25)
        g = next((x for x in goals if x.get("goal_id") == goal_id), None)

        objective = (g.get("objective") if g else "") or ""
        success = (g.get("success_criteria") if g else "") or ""

        prompt = f"""You are Nova's background supervisor. You must output ONLY valid JSON.

Goal:
- objective: {objective}
- success_criteria: {success}

Recent execution history:
{history}

Available tools:
{tools_list}

Rules:
- If you need user input/approval to proceed, output type=question.
- If the goal appears complete, output type=final with a short completion message.
- Otherwise, output type=tool with the single best next tool call.
- Never claim a tool result you did not execute.

Return JSON only.
"""
        raw = await self._llm.chat(
            [{"role": "user", "content": prompt}],
            max_tokens=768,  # background reasoning + the JSON decision
            temperature=0.2,
            stop=["\n\nUser:", "\n\nAssistant:"],
            thinking=True,
        )
        # Reuse the balanced-brace extractor (handles nested braces / trailing
        # prose) instead of a greedy `\{.*\}` that breaks on either.
        data = extract_first_json_object((raw or "").strip())
        if not isinstance(data, dict) or "type" not in data:
            raise ValueError("Invalid decide JSON")
        return data

    async def _run_loop(self) -> None:
        await self._memory.initialize()
        while not self._stop.is_set():
            claimed_id: str | None = None
            try:
                task = await self._memory.claim_next_goal_task()
                if not task:
                    await asyncio.sleep(self._cfg.tick_seconds)
                    continue

                task_id = str(task["task_id"])
                # claim_next_goal_task marks it running. Anything raising below
                # (an unknown decision type, a tool name the model left blank,
                # schema drift) used to fall through to the outer handler,
                # which logged and slept but never released the claim — the
                # goal stalled forever with nothing reporting it.
                claimed_id = task_id
                goal_id = str(task["goal_id"])
                project_name = str(task["project_name"])
                tool_name = str(task["tool_name"])
                args_json = str(task.get("args_json") or "{}")
                attempts = int(task.get("attempts") or 0)
                try:
                    args = json.loads(args_json) if args_json else {}
                except Exception:
                    args = {}

                if tool_name == "__decide__":
                    # Enforce the per-goal step budget before spending an LLM call.
                    done_steps = await self._memory.list_goal_tasks(goal_id=goal_id, limit=self._cfg.max_steps_per_goal + 4)
                    executed = sum(1 for t in done_steps if t.get("status") in ("done", "failed"))
                    if executed >= self._cfg.max_steps_per_goal:
                        msg = (
                            f"Goal paused after {executed} steps without completion. "
                            "Review progress and resume or cancel it."
                        )
                        await self._memory.update_goal_status(goal_id=UUID(goal_id), status="paused")
                        await self._memory.complete_goal_task(task_id=task_id, status="done", result={"paused": msg})
                        await self._memory.add_progress_event(goal_id=UUID(goal_id), project_name=project_name, kind="paused", message=msg)
                        logger.info("agent_goal_step_budget_hit", goal_id=goal_id, steps=executed)
                        continue

                    try:
                        decision = await self._decide_next(goal_id=goal_id, project_name=project_name)
                    except Exception as decide_err:  # noqa: BLE001
                        # A malformed decision must not leave the claimed task
                        # stuck forever (silent stalled goal). Fail it explicitly
                        # so the loop moves on and the goal can be retried/paused.
                        logger.warning("agent_decide_failed", goal_id=goal_id, error=str(decide_err)[:200])
                        await self._memory.complete_goal_task(task_id=task_id, status="failed", result={}, error=str(decide_err)[:300])
                        await self._memory.add_progress_event(
                            goal_id=UUID(goal_id), project_name=project_name, kind="error",
                            message=f"Could not decide next step: {str(decide_err)[:160]}",
                        )
                        continue
                    t = decision.get("type")
                    if t == "tool":
                        name = str(decision.get("name", "")).strip()
                        if not name:
                            raise ValueError("Decision missing tool name")
                        await self._memory.enqueue_goal_task(goal_id=UUID(goal_id), project_name=project_name, tool_name=name, args=decision.get("args") or {})
                        # enqueue next decision step
                        await self._enqueue_decide(goal_id=UUID(goal_id), project_name=project_name)
                        await self._memory.complete_goal_task(task_id=task_id, status="done", result={"decision": decision})
                        await self._memory.add_progress_event(goal_id=UUID(goal_id), project_name=project_name, kind="plan", message=f"Next: {name}")
                        continue

                    if t == "question":
                        msg = str(decision.get("message", "")).strip() or "I need your input to proceed."
                        # create a proposal to get approval/clarification
                        await self._memory.create_proposal(goal_id=UUID(goal_id), project_name=project_name, suggestion=msg, rationale="Needed to proceed with the active goal.")
                        await self._memory.update_goal_status(goal_id=UUID(goal_id), status="paused")
                        await self._memory.complete_goal_task(task_id=task_id, status="done", result={"question": msg})
                        await self._memory.add_progress_event(goal_id=UUID(goal_id), project_name=project_name, kind="question", message=msg)
                        continue

                    if t == "final":
                        msg = str(decision.get("message", "")).strip() or "Completed."
                        await self._memory.update_goal_status(goal_id=UUID(goal_id), status="completed")
                        await self._memory.complete_goal_task(task_id=task_id, status="done", result={"final": msg})
                        await self._memory.add_progress_event(goal_id=UUID(goal_id), project_name=project_name, kind="complete", message=msg)
                        continue

                    raise ValueError(f"Unknown decision type: {t}")

                # Execute a real tool call.
                #
                # ToolRouter.execute() NEVER raises — it catches everything and
                # returns ToolResult(ok=False). This used to be wrapped in a
                # try/except, which made the entire retry-with-backoff branch
                # DEAD CODE: max_retries never applied, bump_goal_task_attempt
                # was never called, and a tool that failed was recorded as
                # status "done". The goal's own history then read "DONE
                # <tool>", so the next __decide__ step was told the step had
                # succeeded. Failure is signalled by `ok`, so that is what is
                # checked. The except is kept for genuinely unexpected errors.
                call = ToolCall(name=tool_name, args=args)
                try:
                    result = await self._router.execute(call, timeout_s=self._cfg.task_timeout_s, retries=1)
                    failure = None if result.ok else (result.error or "tool reported failure")
                except Exception as e:  # noqa: BLE001 — router contract says this shouldn't happen
                    result, failure = None, str(e)

                if failure is not None:
                    attempts += 1
                    if attempts <= self._cfg.max_retries:
                        backoff = timedelta(seconds=min(60, 2 ** attempts))
                        await self._memory.bump_goal_task_attempt(
                            task_id=task_id,
                            attempts=attempts,
                            run_after_iso=self._iso(self._now() + backoff),
                            error=failure,
                        )
                        await self._memory.add_progress_event(goal_id=UUID(goal_id), project_name=project_name, kind="retry", message=f"{tool_name} failed, retrying ({attempts}/{self._cfg.max_retries}): {failure}")
                    else:
                        await self._memory.complete_goal_task(task_id=task_id, status="failed", result={}, error=failure)
                        await self._memory.add_progress_event(goal_id=UUID(goal_id), project_name=project_name, kind="error", message=f"{tool_name} failed: {failure}")
                    continue

                # Mark tool task done
                await self._memory.complete_goal_task(task_id=task_id, status="done", result={"ok": True, "data": result.result}, error="")
                await self._memory.add_progress_event(goal_id=UUID(goal_id), project_name=project_name, kind="tool", message=f"{tool_name} completed")
            except Exception as e:
                # Schema drift during upgrades can cause sqlite OperationalError spam; back off harder.
                msg = str(e)
                # log_worker_error, not logger.exception: on a cp1252 console
                # structlog's traceback renderer raised UnicodeEncodeError from
                # inside this very handler and killed the supervisor outright.
                log_worker_error(logger, "agent_supervisor_loop_error", e, task_id=claimed_id or "-")
                if claimed_id:
                    try:
                        await self._memory.complete_goal_task(
                            task_id=claimed_id, status="failed", result={}, error=msg[:300])
                    except Exception as e2:  # noqa: BLE001
                        log_worker_error(logger, "agent_task_release_failed", e2, task_id=claimed_id)
                if "no such column" in msg and "sqlite" in msg.lower():
                    await asyncio.sleep(max(5.0, self._cfg.tick_seconds * 5))
                else:
                    await asyncio.sleep(max(1.0, self._cfg.tick_seconds))
