from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from core.logging_setup import get_logger
from core.policy.autonomy_planner import AutonomyPlannerLLM
from core.tool_router import ToolCall, ToolRouter
from core.workers.lifecycle import log_worker_error, stop_worker
from memory.unifier import MemoryUnifier


logger = get_logger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class AutonomySupervisorWorker:
    def __init__(
        self,
        *,
        memory: MemoryUnifier,
        planner: AutonomyPlannerLLM,
        router: ToolRouter,
        tick_seconds: float = 5.0,
    ) -> None:
        self._memory = memory
        self._planner = planner
        self._router = router
        self._tick = float(tick_seconds)
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run())
        logger.info("autonomy_supervisor_started")

    async def stop(self) -> None:
        self._stop.set()
        await stop_worker(self._task, name="autonomy-supervisor")

    async def _run(self) -> None:
        await self._memory.initialize()
        while not self._stop.is_set():
            claimed_id: str | None = None
            try:
                task = await self._memory.claim_next_task()
                if not task:
                    await asyncio.sleep(self._tick)
                    continue
                # Remember what we hold: claim_next_task marks it running, so
                # anything that throws below would otherwise leave it running
                # FOREVER — never finished, never retried, and invisible until
                # the next boot clears stale background work.
                claimed_id = str(task.get("task_id"))

                # Pace backlog processing: without this, a queue of stale tasks
                # drains at full speed — one GPU LLM call per task, back to back.
                await asyncio.sleep(1.0)

                task_id = str(task.get("task_id"))
                title = str(task.get("title") or "")
                details = str(task.get("details") or "")
                project_name = str(task.get("project_name") or "temp")
                initiated_by_user = bool(task.get("initiated_by_user"))

                # Light memory context
                mem_hits = await self._memory.search(q=title + " " + details, conversation_id=None, limit=8)
                mem_ctx = "\n".join([h.text for h in mem_hits])

                plan = await self._planner.plan(
                    title=title,
                    details=details,
                    memory_context=mem_ctx,
                    available_tools=self._router.list_tools(),
                )

                result_payload: dict[str, object] = {"action": plan.action, "reason": plan.reason}

                if plan.action == "idle":
                    # "Nothing to do" and "I could not read the plan" are
                    # both `idle` — the planner falls back to it on an
                    # unparseable or invalid response, labelling the reason.
                    # Recording the second one as `done` tells the user
                    # their task finished when no plan was ever produced,
                    # and the label lives in result_json, which
                    # list_autonomy_tasks does not return.
                    degraded = str(plan.reason or "").startswith("planner_")
                    if degraded:
                        await self._memory.mark_task_failed(
                            task_id=task_id,
                            error=f"planner produced no usable plan ({plan.reason})",
                            result={"status": "planner_failed", **result_payload})
                    else:
                        await self._memory.mark_task_done(task_id=task_id, result={"status": "idle", **result_payload})
                    continue

                if plan.action == "enqueue_task":
                    # Depth guard: only user-initiated tasks may spawn subtasks.
                    # Planner-spawned tasks that try to enqueue more would create
                    # a self-feeding loop (observed: thousands of chained tasks
                    # hammering the GPU at boot).
                    if not initiated_by_user:
                        logger.info("autonomy_subtask_fanout_blocked", task=title[:80])
                        await self._memory.mark_task_done(task_id=task_id, result={"status": "fanout_blocked", **result_payload})
                        continue
                    for nt in (plan.new_tasks or [])[:3]:
                        try:
                            await self._memory.enqueue_task(
                                title=str(nt.get("title") or ""),
                                details=str(nt.get("details") or ""),
                                priority=int(nt.get("priority") or 3),
                                project_name=project_name,
                                initiated_by_user=False,
                            )
                        except Exception:
                            continue
                    await self._memory.mark_task_done(task_id=task_id, result={"status": "enqueued", **result_payload})
                    continue

                if plan.action == "ask_user":
                    # Store as progress event; chat can surface on demand.
                    msg = (plan.message_to_user or "").strip()
                    if msg:
                        await self._memory.add_fact(entity=f"project:{project_name}", attribute="autonomy_note", value=msg, confidence=0.6)
                    await self._memory.mark_task_done(task_id=task_id, result={"status": "needs_user", "message": msg, **result_payload})
                    continue

                if plan.action == "tool":
                    # An ORDERED plan. If step B fails, step C must not run: the
                    # loop used to execute every call regardless and then mark the
                    # task `tools_done`, so a plan whose second step failed was
                    # reported as finished and its third step ran on a
                    # precondition that never held.
                    tool_results: list[dict[str, object]] = []
                    blocked: dict[str, object] | None = None
                    for tc in plan.tool_calls:
                        call = ToolCall(name=tc.tool, args=tc.args)
                        res = await self._router.execute(call, timeout_s=30.0, retries=1)
                        tool_results.append({"tool": call.name, "ok": res.ok, "error": res.error, "result": res.result})
                        if not res.ok:
                            blocked = {"tool": call.name,
                                       "error": res.error or "tool reported failure"}
                            break

                    if blocked is not None:
                        remaining = [tc.tool for tc in plan.tool_calls][len(tool_results):]
                        # FAILED, not done. Every terminal path here used to call
                        # mark_task_done, which writes status='done', clears
                        # last_error and publishes task.completed. The honest
                        # outcome survived only inside result_json — and
                        # list_autonomy_tasks does not even return that column,
                        # so "what failed?" had nothing to read and
                        # list_tasks(status='failed') was always empty.
                        await self._memory.mark_task_failed(
                            task_id=task_id,
                            error=str(blocked["error"] or "tool reported failure"),
                            result={"status": "tools_blocked", "tools": tool_results,
                                    "failed_tool": blocked["tool"],
                                    "error": blocked["error"],
                                    "not_attempted": remaining, **result_payload})
                        continue

                    await self._memory.mark_task_done(task_id=task_id, result={"status": "tools_done", "tools": tool_results, **result_payload})
                    continue

                # An action this loop does not implement means nothing ran. That
                # is an unknown outcome, and recording it as success is exactly
                # the "unknown becomes success" failure.
                await self._memory.mark_task_failed(
                    task_id=task_id,
                    error=f"planner returned an action this worker cannot run: {plan.action!r}",
                    result={"status": "unknown_action", **result_payload})

            except asyncio.CancelledError:
                return
            except Exception as e:  # noqa: BLE001
                log_worker_error(logger, "autonomy_loop_error", e, task_id=claimed_id or "-")
                # Release the claim honestly so the task shows as failed rather
                # than sitting in 'running' for the rest of the session. It has
                # to be mark_task_FAILED: the previous call wrote 'done' with an
                # empty last_error and announced task.completed, so a crash was
                # indistinguishable from success on every surface that reads the
                # row.
                if claimed_id:
                    try:
                        await self._memory.mark_task_failed(
                            task_id=claimed_id,
                            error=str(e)[:300],
                            result={"status": "failed", "error": str(e)[:300]},
                        )
                    except Exception as e2:  # noqa: BLE001
                        log_worker_error(logger, "autonomy_task_release_failed", e2, task_id=claimed_id)
                await asyncio.sleep(self._tick)
