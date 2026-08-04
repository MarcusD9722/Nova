from __future__ import annotations

"""Autonomous research worker (Goal #9, Phase 5).

Keeps ongoing research topics fresh: on a slow, killable timer it takes the
least-recently-checked tracked topic, searches the web, summarizes what the
results actually say, and folds that into the semantic world model (#11) WITH
the source URL as a citation. Findings are never fabricated — only what a real
source returned, and nothing is stored without a citation.

OFF by default (`NOVA_RESEARCH=0`) because it makes network + model calls; opt in
explicitly. `run_cycle()` is a single bounded pass so it can also be triggered
on demand and reasoned about in isolation.
"""

import asyncio
import os

from core.event_bus import BUS, clip
from core.llm_runtime import LLMRuntime
from core.logging_setup import get_logger
from core.tool_router import ToolCall, ToolRouter
from core.workers.lifecycle import stop_worker
from memory.unifier import MemoryUnifier

logger = get_logger(__name__)


def research_enabled() -> bool:
    return os.getenv("NOVA_RESEARCH", "0").strip().lower() in {"1", "true", "yes", "on"}


class ResearchWorker:
    def __init__(self, *, memory: MemoryUnifier, llm: LLMRuntime, llm_semaphore, router: ToolRouter,
                 interval_s: float | None = None) -> None:
        self._memory = memory
        self._llm = llm
        self._sem = llm_semaphore
        self._router = router
        self._interval = float(interval_s if interval_s is not None else os.getenv("NOVA_RESEARCH_INTERVAL_S", "3600") or "3600")
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._enabled = research_enabled()

    def start(self) -> None:
        if not self._enabled:
            logger.info("research_worker_disabled")  # opt-in; makes network calls
            return
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop())
        logger.info("research_worker_started", interval_s=self._interval)

    async def stop(self) -> None:
        self._stop.set()
        await stop_worker(self._task, name="research")

    async def _loop(self) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=min(120.0, self._interval))
        except (TimeoutError, asyncio.TimeoutError):
            pass
        while not self._stop.is_set():
            try:
                await self.run_cycle()
            except asyncio.CancelledError:
                return
            except Exception as e:  # noqa: BLE001
                logger.debug("research_cycle_error", error=str(e)[:200])
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except (TimeoutError, asyncio.TimeoutError):
                pass

    async def run_cycle(self) -> dict:
        """One bounded research pass over the next-due topic."""
        topic = await self._memory.next_research_topic()
        if not topic:
            return {"researched": None}

        res = await self._router.execute(ToolCall(name="web.search", args={"query": topic}), timeout_s=30.0, retries=0)
        results = res.result.get("results") if (res.ok and isinstance(res.result, dict)) else None
        if not results:
            await self._memory.mark_research_checked(topic)
            return {"researched": topic, "ok": False, "reason": "no_results"}

        top = results[:5]
        url = ""
        lines: list[str] = []
        for r in top:
            u = str(r.get("url") or r.get("href") or "")
            url = url or u
            body = str(r.get("snippet") or r.get("body") or r.get("description") or "")
            lines.append(f"- {r.get('title') or ''}: {body}")
        prompt = (
            "Summarize what these search results say about the topic in 2-3 factual sentences. "
            "State ONLY what the results support; do not add outside claims or speculation.\n"
            f"Topic: {topic}\nResults:\n" + "\n".join(lines) + "\nSummary:"
        )
        summary = ""
        try:
            async with self._sem:
                summary = (await self._llm.chat(
                    [{"role": "user", "content": prompt}], max_tokens=220, temperature=0.2, thinking=False
                ) or "").strip()
        except Exception as e:  # noqa: BLE001
            logger.debug("research_summarize_failed", error=str(e)[:160])

        stored = False
        if summary:
            stored = await self._memory.remember_web_finding(topic, summary, url or "web")
            BUS.publish("research.finding", {"topic": clip(topic, 80), "source": clip(url or "web", 120)})
            logger.info("research_finding_stored", topic=clip(topic, 60))
        await self._memory.mark_research_checked(topic)
        return {"researched": topic, "ok": stored, "source": url or "web"}
