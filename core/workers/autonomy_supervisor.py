from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from core.logging_setup import get_logger
from core.policy.autonomy_planner import AutonomyPlannerLLM
from core.tool_router import ToolCall, ToolRouter
from core.workers.lifecycle import stop_worker
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
            try:
                task = await self._memory.claim_next_task()
                if not task:
                    await asyncio.sleep(self._tick)
                    continue

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
                    tool_results: list[dict[str, object]] = []
                    for tc in plan.tool_calls:
                        call = ToolCall(name=tc.tool, args=tc.args)
                        res = await self._router.execute(call, timeout_s=30.0, retries=1)
                        tool_results.append({"tool": call.name, "ok": res.ok, "error": res.error, "result": res.result})
                    await self._memory.mark_task_done(task_id=task_id, result={"status": "tools_done", "tools": tool_results, **result_payload})
                    continue

                await self._memory.mark_task_done(task_id=task_id, result={"status": "unknown_action", **result_payload})

            except asyncio.CancelledError:
                return
            except Exception as e:  # noqa: BLE001
                logger.exception("autonomy_loop_error", error=str(e))
                await asyncio.sleep(self._tick)
