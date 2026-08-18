from __future__ import annotations

"""Agent spec + tool-loop executor (Phase 2.1 of docs/ROADMAP.md).

An Agent is a *bundle*, not a process: a name, which model role decides its
steps, which tools it may call, and a step budget. The ToolLoopExecutor runs
the reason→act→observe loop for a given agent — this is the exact loop that
lived inline in RuntimeManager.chat_turn_stream, moved here verbatim so the
default chat path behaves identically while the loop becomes reusable (deep
mode's Executor stage in 2.3 reuses it).
"""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from core.logging_setup import get_logger
from core.orchestrator.model_router import ModelRouter
from core.policy._json_extract import extract_first_json_object
from core.tool_router import ToolCall, ToolRouter

if TYPE_CHECKING:  # selection is optional; never imported at runtime from here
    from core.tools.selector import ToolSelector

logger = get_logger(__name__)

# Tools the free agent loop never calls directly: code.write is gated behind
# the project builder (sandboxed writes), the other two are internal/setup.
_DEFAULT_SKIP = frozenset({"code.write", "memory.rebuild_index", "project.scaffold"})


@dataclass(frozen=True)
class Agent:
    name: str
    model_role: str = "decider"
    # None = every router tool (minus skip); a set restricts to those names.
    tool_allowlist: frozenset[str] | None = None
    skip_tools: frozenset[str] = _DEFAULT_SKIP
    step_budget: int = 6
    decision_max_tokens: int = 900
    temperature: float = 0.1
    # Extra guidance appended after the standard rules (deep mode's Executor
    # passes the plan here); empty for the default chat agent.
    extra_instructions: str = ""


class ToolLoopExecutor:
    """Runs an Agent's reason→act→observe loop and returns the observations."""

    def __init__(self, *, models: ModelRouter, tool_router: ToolRouter,
                 selector: "ToolSelector | None" = None) -> None:
        self._models = models
        self._router = tool_router
        #: Optional preselection stage (core/tools/selector.py). Without it the
        #: agent sees every allowed tool on every step — the pre-V2 behaviour,
        #: which remains the fallback whenever selection is unavailable.
        self._selector = selector
        self.last_selection = None

    def tool_catalog(self, agent: Agent, user_text: str = "") -> str:
        """Public: the tool list an agent would see (for the Planner)."""
        return self._tool_catalog(agent, user_text)

    def _allowed(self, agent: Agent) -> dict[str, str]:
        return {
            name: desc
            for name, desc in self._router.describe_tools().items()
            if desc and name not in agent.skip_tools
            and (agent.tool_allowlist is None or name in agent.tool_allowlist)
        }

    def _tool_catalog(self, agent: Agent, user_text: str = "", context: str = "") -> str:
        allowed = self._allowed(agent)

        # Preselect, so the prompt carries a handful of plausible tools instead
        # of the whole registry, repeated on every step of the loop.
        if self._selector is not None and user_text.strip():
            selection = self._selector.select(user_text, allowed, context=context)
            self.last_selection = selection
            if selection.tools:
                allowed = {name: allowed[name] for name in selection.tools if name in allowed}

        return "\n".join(f"- {name}: {desc}" for name, desc in allowed.items())

    async def decide(self, *, agent: Agent, user_text: str, grounding: str,
                     tool_results: list[dict[str, Any]]) -> dict[str, Any] | None:
        """One reason→act decision: returns {"tool","args"} or None to respond.

        Runs with native thinking enabled — the model reasons in the background
        about what it has learned so far and what step comes next; only the
        final JSON decision comes out (think blocks are stripped in the LLM
        runtime).
        """
        results_text = ""
        if tool_results:
            results_text = "Observations from tools you already called this turn (in order):\n" + "\n".join(
                f"- {r['tool']}: {'OK ' + str(r['result'])[:700] if r['ok'] else 'FAILED ' + str(r['error'])[:200]}"
                for r in tool_results
            )
        prompt = (
            "You are the agent brain for Nova, a local AI assistant. Work the user's "
            "request step by step: decide whether you need ONE more tool call, or "
            "whether you have enough to answer.\n\n"
            f"Available tools:\n{self._tool_catalog(agent, user_text, grounding)}\n\n"
            f"Context: {grounding}\n{results_text}\n"
            f"User message: {user_text}\n\n"
            'Reply ONLY with JSON. Next tool call: {"action": "tool", "tool": "<name>", "args": {...}}. '
            'Ready to answer: {"action": "respond"}.\n'
            "Rules:\n"
            "- Chain tools when the task needs it: e.g. web.search first, then web.fetch on the most "
            "relevant result URL; or read a file, then act on its contents.\n"
            "- Call tools ONLY for live/current/external data or real actions (weather, places, web, "
            "Discord, files, shell, projects, memory). Never for general knowledge, math, opinions, or chat.\n"
            "- project.start_build creates a brand-new project — only call it when the user clearly asks to "
            "start/create/build something new. Questions or discussion about an existing project (even ones "
            "asking for improvement ideas) are NOT a reason to call it; respond in chat instead, or use "
            "project.improve only if they clearly asked you to make a change.\n"
            "- When the user asks you to remember something, save it with memory.remember. When they ask "
            "what you remember about a topic, check memory.recall first.\n"
            "- To inspect or improve your OWN code, use self.read_code / self.list_code, and self.propose_change "
            "to suggest an edit. Proposals are NOT applied automatically — Marcus reviews and approves them.\n"
            "- Each observation above is real; trust it. Don't repeat a call that already succeeded.\n"
            "- If a tool failed twice, stop and respond — explain what failed.\n"
            "- When the observations already answer the user, respond."
            + (f"\n{agent.extra_instructions}" if agent.extra_instructions else "")
        )
        handle = self._models.for_role(agent.model_role)
        async with handle.semaphore:
            raw = await handle.runtime.chat(
                [{"role": "user", "content": prompt}],
                max_tokens=agent.decision_max_tokens,  # room for background reasoning + the JSON decision
                temperature=agent.temperature,
                thinking=True,
            )
        obj = extract_first_json_object(raw or "")
        logger.debug("tool_decision", raw=(raw or "")[:200], parsed=bool(obj))
        if not obj:
            return None

        # Lenient parse: small models put the tool name in "action" or skip
        # the wrapper entirely ({"tool": "web.search", ...}).
        known = set(self._router.list_tools())
        action = str(obj.get("action") or "").strip()
        tool = str(obj.get("tool") or "").strip()
        if action == "respond":
            return None
        if action == "tool" and tool in known:
            pass
        elif action in known:
            tool = action
        elif tool in known:
            pass
        else:
            return None

        args = obj.get("args") if isinstance(obj.get("args"), dict) else {}
        return {"tool": tool, "args": args}

    async def run(self, *, agent: Agent, user_text: str, grounding: str) -> list[dict[str, Any]]:
        """The reason→act→observe loop. Returns the ordered observations
        (each {"tool","ok","result","error"}). A tool that fails twice is dead
        for this run; the loop stops after agent.step_budget rounds."""
        tool_results: list[dict[str, Any]] = []
        failure_counts: dict[str, int] = {}
        for _ in range(agent.step_budget):
            try:
                decision = await self.decide(
                    agent=agent, user_text=user_text, grounding=grounding, tool_results=tool_results
                )
            except Exception as e:  # noqa: BLE001
                logger.debug("tool_decider_failed", error=str(e))
                decision = None
            if decision is None:
                break

            # Hard guard mirroring the prompt rule: a tool that failed twice is
            # dead for this turn — force the loop to wrap up and answer.
            if failure_counts.get(decision["tool"], 0) >= 2:
                break

            call = ToolCall(name=decision["tool"], args=decision["args"])
            # No timeout here on purpose. The router holds the authoritative
            # budget per tool; a number typed at this generic call site cannot
            # know that `project.delete` waits on a human, and the 25.0 that used
            # to sit here silently cancelled that approval handshake.
            res = await self._router.execute(call, retries=0)
            # `args` travels with the observation so downstream can record what
            # was actually asked — artifact provenance needs the query, not just
            # the answer (memory/artifacts.py::capture_tool_result).
            tool_results.append({"tool": call.name, "args": call.args, "ok": res.ok,
                                 "result": res.result, "error": res.error})
            if not res.ok:
                failure_counts[call.name] = failure_counts.get(call.name, 0) + 1
        return tool_results
