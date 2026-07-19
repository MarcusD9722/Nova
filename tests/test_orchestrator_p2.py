"""Phase 2: ModelRouter (2.4). More agents/orchestrator checks append here."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.orchestrator.model_router import ROLES, ModelHandle, ModelRouter, parse_role_map

_fail = False


def check(cond, label):
    global _fail
    status = "OK  " if cond else "FAIL"
    if not cond:
        _fail = True
    print(f"  {status} {label}")


class _FakeLLM:
    def __init__(self, tag):
        self.tag = tag


def _router_checks():
    prim = _FakeLLM("primary")
    sem = asyncio.Semaphore(1)

    # single(): one model, every role resolves to it, shares the one semaphore
    r = ModelRouter.single(prim, sem)
    check(all(r.for_role(role).runtime is prim for role in ROLES), "single(): every role on the primary model")
    check(all(r.for_role(role).semaphore is sem for role in ROLES), "single(): every role shares the one GPU semaphore")
    check(r.describe() == {role: "primary" for role in ROLES}, "describe() maps all roles to primary")

    # Two handles: a remapped role gets the OTHER model AND its own semaphore
    # (this is what lets it run concurrently once the hardware exists).
    sec = _FakeLLM("secondary")
    sem2 = asyncio.Semaphore(1)
    handles = {
        "primary": ModelHandle("primary", prim, sem),
        "secondary": ModelHandle("secondary", sec, sem2),
    }
    r2 = ModelRouter(handles, default="primary", role_map={"coder": "secondary", "planner": "secondary"})
    check(r2.for_role("coder").runtime is sec, "remapped role uses the second model")
    check(r2.for_role("coder").semaphore is sem2, "remapped role uses the second model's OWN semaphore")
    check(r2.for_role("chat").runtime is prim, "unmapped role stays on default")
    check(r2.describe()["coder"] == "secondary" and r2.describe()["chat"] == "primary", "describe reflects the remap")

    # Unknown handle in a role map is dropped (never routes into the void)
    r3 = ModelRouter(handles, default="primary", role_map={"coder": "ghost"})
    check(r3.for_role("coder").name == "primary", "role mapped to an unregistered model falls back to default")

    # A bad default is a hard error (config bug worth failing on)
    try:
        ModelRouter(handles, default="nope")
        check(False, "bad default should raise")
    except ValueError:
        check(True, "unknown default handle raises")

    # parse_role_map: config string -> dict, ignoring junk + unknown roles
    check(parse_role_map("coder=secondary, planner=secondary") == {"coder": "secondary", "planner": "secondary"},
          "parse_role_map parses a normal config")
    check(parse_role_map("") == {}, "empty config -> no remaps")
    check(parse_role_map("garbage,notarole=x,coder=secondary") == {"coder": "secondary"},
          "parse_role_map drops malformed + unknown-role entries")


# ── Tool-loop executor (2.1) ──────────────────────────────────────────────────

class _ScriptedLLM:
    """Returns canned JSON decisions in order; records prompts for inspection."""
    def __init__(self, replies):
        self._replies = list(replies)
        self.prompts = []

    async def chat(self, messages, **kw):
        self.prompts.append(messages[0]["content"])
        return self._replies.pop(0) if self._replies else '{"action": "respond"}'


async def _async_checks():
    from core.orchestrator.agent import Agent, ToolLoopExecutor
    from core.tool_router import ToolRouter

    calls = []

    async def weather(args):
        calls.append(("weather.current", args))
        return {"temp": 72}

    async def fetch(args):
        calls.append(("web.fetch", args))
        return {"text": "page"}

    async def boom(args):
        raise RuntimeError("kaboom")

    tools = {"weather.current": weather, "web.fetch": fetch, "flaky.tool": boom}
    descs = {"weather.current": "live weather", "web.fetch": "fetch a url", "flaky.tool": "breaks"}
    router = ToolRouter(tools, descs)

    def executor_with(replies):
        return ToolLoopExecutor(models=ModelRouter.single(_ScriptedLLM(replies), asyncio.Semaphore(1)), tool_router=router)

    agent = Agent(name="chat", step_budget=6)

    # respond immediately -> no tools
    calls.clear()
    res = await executor_with(['{"action": "respond"}']).run(agent=agent, user_text="hi", grounding="{}")
    check(res == [], "respond-immediately runs no tools")

    # one tool then respond -> single observation, correct shape
    calls.clear()
    res = await executor_with([
        '{"action": "tool", "tool": "weather.current", "args": {"city": "Austin"}}',
        '{"action": "respond"}',
    ]).run(agent=agent, user_text="weather?", grounding="{}")
    check(len(res) == 1 and res[0]["tool"] == "weather.current" and res[0]["ok"], "single tool call recorded")
    check(res[0]["result"] == {"temp": 72} and calls == [("weather.current", {"city": "Austin"})], "tool actually invoked with args")

    # chain two tools
    calls.clear()
    res = await executor_with([
        '{"action": "tool", "tool": "weather.current", "args": {}}',
        '{"tool": "web.fetch", "args": {"url": "x"}}',  # lenient: no action wrapper
        '{"action": "respond"}',
    ]).run(agent=agent, user_text="chain", grounding="{}")
    check([r["tool"] for r in res] == ["weather.current", "web.fetch"], "tools chain in order (lenient parse works)")

    # a tool that keeps failing stops after 2 failures (guard), never budget-spins
    calls.clear()
    ex = executor_with(['{"action": "tool", "tool": "flaky.tool", "args": {}}'] * 6)
    res = await ex.run(agent=agent, user_text="fail", grounding="{}")
    check(sum(1 for r in res if not r["ok"]) == 2, f"failing tool stops after 2 attempts (got {len(res)})")

    # step budget caps a model that never says respond
    calls.clear()
    ex = executor_with(['{"action": "tool", "tool": "weather.current", "args": {}}'] * 20)
    res = await ex.run(agent=Agent(name="chat", step_budget=3), user_text="loop", grounding="{}")
    check(len(res) == 3, f"step budget caps the loop (got {len(res)})")

    # the decision prompt still contains the load-bearing rules (verbatim move)
    llm = _ScriptedLLM(['{"action": "respond"}'])
    ex = ToolLoopExecutor(models=ModelRouter.single(llm, asyncio.Semaphore(1)), tool_router=router)
    await ex.decide(agent=agent, user_text="hi", grounding="{}", tool_results=[])
    p = llm.prompts[0]
    check("You are the agent brain for Nova" in p and "Reply ONLY with JSON" in p, "decision prompt preserved")
    check("weather.current: live weather" in p, "tool catalog rendered into the prompt")

    # allowlist / skip filtering
    llm2 = _ScriptedLLM(['{"action": "respond"}'])
    ex2 = ToolLoopExecutor(models=ModelRouter.single(llm2, asyncio.Semaphore(1)), tool_router=router)
    await ex2.decide(agent=Agent(name="x", tool_allowlist=frozenset({"weather.current"})),
                     user_text="hi", grounding="{}", tool_results=[])
    # Check the CATALOG lines specifically (web.fetch also appears in the fixed
    # rules text as an example, so a whole-prompt check would false-fail).
    p2 = llm2.prompts[0]
    check("- weather.current: live weather" in p2 and "- web.fetch: fetch a url" not in p2,
          "allowlist filters the tool catalog")


def main():
    _router_checks()
    asyncio.run(_async_checks())
    print("\nRESULT:", "FAILURES" if _fail else "ALL PASS")
    sys.exit(1 if _fail else 0)


main()
