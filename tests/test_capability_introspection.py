"""Nova's answer to "what can you do?" comes from the runtime, not from memory.

Live, she dramatically underreported herself while `RuntimeManager` and
`ToolRouter` already held the full registry: `_build_grounding_context` put the
tool list into `context["available_tools"]` and `_grounding_to_natural` rendered
only a smart-home flag, so the response model never saw the inventory.

Run:  venv\\Scripts\\python.exe tests\\test_capability_introspection.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")

from harness import Checks, run  # noqa: E402

from core.capability_report import summarize_capabilities  # noqa: E402

check = Checks()


def _tmp():
    return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)


class _NoLLM:
    gpu_status = type("S", (), {"status": "stub"})()

    async def initialize(self):
        return None

    async def chat(self, *a, **k):
        raise AssertionError("introspection must not need a generation")


async def _runtime(td: str):
    from core.runtime import RuntimeManager
    from core.tooling import build_tool_router
    from memory.unifier import MemoryUnifier

    root = Path(td)
    projects = root / "projects"
    projects.mkdir(parents=True, exist_ok=True)
    mem_dir = root / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    m = MemoryUnifier(mem_dir, enable_chroma=False)
    await m.initialize()
    router = build_tool_router(repo_root=root, projects_dir=projects, memory=m)
    rt = RuntimeManager(repo_root=root, projects_dir=projects, memory=m,
                        llm=_NoLLM(), router=router, memory_dir=mem_dir)
    return rt, m, router


async def test_the_answer_covers_what_is_registered():
    check.section("Phase 3: the answer is grounded in the real registry")

    with _tmp() as td:
        rt, m, router = await _runtime(td)
        names = router.list_tools()
        check(len(names) > 20, f"the real router has a substantial registry ({len(names)})")

        for question in ("What can you do?", "What are you capable of?",
                         "What features do you have?"):
            reply = rt._capability_reply(question)
            check(reply is not None and len(reply) > 40,
                  f"{question!r} -> a real answer ({str(reply)[:60]!r})")

        reply = rt._capability_reply("What are you capable of?") or ""
        report = rt._capability_report()
        registered = {c.key for c in report.usable()}
        # The major families the real build registers must be spoken about.
        for expected in ("memory", "projects", "code_understanding"):
            check(expected in registered,
                  f"{expected} is registered in this build ({sorted(registered)})")
            label = next(c.label for c in report.capabilities if c.key == expected)
            check(label in reply, f"and appears in the answer ({label!r})")


async def test_nothing_unregistered_is_claimed():
    check.section("Phase 3: an unregistered capability is never claimed")

    # A deliberately tiny registry: only memory tools exist.
    report = summarize_capabilities(["memory.recall", "memory.remember"])
    usable = {c.key for c in report.usable()}
    check(usable == {"memory"}, f"only memory is usable ({sorted(usable)})")
    sentence = report.sentence()
    for absent in ("smart-home", "email", "controlling this computer",
                   "searching and reading the web"):
        check(absent not in sentence,
              f"{absent!r} is not claimed when unregistered ({sentence[:70]})")

    check(summarize_capabilities([]).sentence().startswith("Right now I have no tools"),
          "an empty registry says so plainly")


async def test_disabled_is_reported_as_disabled():
    check.section("Phase 3: built-but-switched-off is not 'available'")

    tools = ["shell.exec", "web.search", "self.propose_change", "memory.recall"]

    prev = {k: os.environ.get(k) for k in
            ("NOVA_ALLOW_SHELL", "NOVA_ALLOW_NETWORK_TOOLS", "NOVA_DEV_MODE")}
    try:
        os.environ["NOVA_ALLOW_SHELL"] = "0"
        os.environ["NOVA_ALLOW_NETWORK_TOOLS"] = "0"
        os.environ["NOVA_DEV_MODE"] = "0"
        report = summarize_capabilities(tools)
        states = {c.key: c.state for c in report.capabilities if c.tools}
        check(states.get("computer_control") == "disabled",
              f"shell off -> disabled ({states.get('computer_control')})")
        check(states.get("research") == "disabled",
              f"network off -> disabled ({states.get('research')})")
        check(states.get("self_inspection") == "disabled",
              f"dev mode off -> disabled ({states.get('self_inspection')})")
        check(states.get("memory") == "available",
              f"and memory is unaffected ({states.get('memory')})")

        sentence = report.sentence()
        check("switched off" in sentence,
              f"the answer says they are switched off ({sentence[:110]})")
        check("Right now I can help with: remembering" in sentence,
              f"and only offers what is usable ({sentence[:80]})")

        os.environ["NOVA_ALLOW_SHELL"] = "1"
        # Permission alone is NOT enough any more: without an execution backend
        # the category is dry_run_only. Asserting "available" here was exactly
        # the defect independent review found — ComputerControl ships with
        # adapter=None, so nothing could ever run.
        again = summarize_capabilities(tools, {"computer_can_execute": False})
        check(again.state_of("computer_control") == "dry_run_only",
              f"permitted but with no adapter -> dry_run_only "
              f"({again.state_of('computer_control')})")
        with_backend = summarize_capabilities(tools, {"computer_can_execute": True})
        check(with_backend.state_of("computer_control") == "available",
              "and only a real execution backend makes it available")
    finally:
        for k, v in prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


async def test_the_registry_can_grow_without_edits():
    check.section("Phase 3: a new tool needs no hardcoded answer")

    base = summarize_capabilities(["memory.recall"])
    grown = summarize_capabilities(["memory.recall", "memory.timeline",
                                    "memory.brand_new_thing"])
    mem = next(c for c in grown.capabilities if c.key == "memory")
    check("memory.brand_new_thing" in mem.tools,
          f"a newly registered tool joins its category ({mem.tools})")
    check(grown.total_tools == 3 and base.total_tools == 1,
          "and the count follows the registry")


async def test_ordinary_turns_are_not_bloated():
    check.section("Phase 3: only introspection carries the inventory")

    with _tmp() as td:
        rt, m, router = await _runtime(td)
        names = router.list_tools()

        asked = await rt._build_grounding_context(
            user_text="What are you capable of?", user_name="Test",
            available_tools=names)
        ordinary = await rt._build_grounding_context(
            user_text="She's enjoying preparing everything.", user_name="Test",
            available_tools=names)

        import json
        check("capability_summary" in json.loads(asked),
              "an introspection question gets the summary")
        check("capability_summary" not in json.loads(ordinary),
              "an ordinary message does not")

        natural = rt._grounding_to_natural(json.loads(asked))
        check("cover:" in natural,
              f"and the summary is actually RENDERED into the prompt ({natural[:90]!r})")
        plain = rt._grounding_to_natural(json.loads(ordinary))
        check("cover:" not in plain,
              f"while an ordinary turn's prompt stays clean ({plain[:60]!r})")


async def test_smart_home_behaviour_is_unchanged():
    check.section("Phase 3: the existing smart-home flag still works")

    import json

    with _tmp() as td:
        rt, m, router = await _runtime(td)
        ctx = json.loads(await rt._build_grounding_context(
            user_text="turn on the lights", user_name="Test",
            available_tools=["memory.recall", "smart_home.lights"]))
        check(ctx["capabilities"]["smart_home_control"] == "available",
              f"smart home present -> available ({ctx['capabilities']})")
        ctx = json.loads(await rt._build_grounding_context(
            user_text="turn on the lights", user_name="Test",
            available_tools=["memory.recall"]))
        check(ctx["capabilities"]["smart_home_control"] == "unavailable",
              f"absent -> unavailable ({ctx['capabilities']})")


# ── CORRECTION 5: registered is not operational ─────────────────────────────
async def test_registered_but_unexecutable_is_not_claimed():
    """`ComputerControl(adapter=None)` cannot act, so Nova must not say she can.

    Independent review found the report calling computer control "available"
    whenever NOVA_ALLOW_SHELL was on — while production constructs
    ComputerControl with no platform adapter, so `can_execute()` is false and
    every action is a dry run. Permission and an execution backend are separate
    axes; both must hold.
    """
    check.section("C5: no adapter means no claim of computer control")

    from core.capability_report import summarize_capabilities

    tools = ["computer.observe", "computer.act", "memory.recall"]
    prev = os.environ.get("NOVA_ALLOW_SHELL")
    try:
        os.environ["NOVA_ALLOW_SHELL"] = "1"        # permitted...

        no_adapter = summarize_capabilities(tools, {"computer_can_execute": False})
        check(no_adapter.state_of("computer_control") == "dry_run_only",
              f"...but with no adapter it is dry_run_only "
              f"({no_adapter.state_of('computer_control')})")
        check("controlling this computer" not in
              ", ".join(c.label for c in no_adapter.usable()),
              "and it is NOT listed among what she can do now")
        check("dry runs only" in no_adapter.sentence(),
              f"the answer says why ({no_adapter.sentence()[-90:]!r})")

        with_adapter = summarize_capabilities(tools, {"computer_can_execute": True})
        check(with_adapter.state_of("computer_control") == "available",
              f"a working adapter flips it to available "
              f"({with_adapter.state_of('computer_control')})")
        check("controlling this computer" in with_adapter.sentence(),
              "and then it IS offered")

        os.environ["NOVA_ALLOW_SHELL"] = "0"
        off = summarize_capabilities(tools, {"computer_can_execute": True})
        check(off.state_of("computer_control") == "disabled",
              f"permission still wins when switched off "
              f"({off.state_of('computer_control')})")
    finally:
        if prev is None:
            os.environ.pop("NOVA_ALLOW_SHELL", None)
        else:
            os.environ["NOVA_ALLOW_SHELL"] = prev


async def test_an_unconfigured_integration_is_not_claimed():
    check.section("C5: a registered integration without credentials")

    from core.capability_report import summarize_capabilities

    tools = ["discord.send", "memory.recall"]
    prev = os.environ.get("DISCORD_BOT_TOKEN")
    try:
        os.environ.pop("DISCORD_BOT_TOKEN", None)
        report = summarize_capabilities(tools)
        check(report.state_of("communication") == "needs_setup",
              f"no token -> needs_setup ({report.state_of('communication')})")
        check("email, calendar and messaging" not in
              ", ".join(c.label for c in report.usable()),
              "and messaging is not offered as available")
        check("not connected yet" in report.sentence(),
              f"the answer says it is not connected ({report.sentence()[-80:]!r})")

        os.environ["DISCORD_BOT_TOKEN"] = "x" * 12
        connected = summarize_capabilities(tools)
        check(connected.state_of("communication") == "available",
              f"with the token present it becomes available "
              f"({connected.state_of('communication')})")
    finally:
        if prev is None:
            os.environ.pop("DISCORD_BOT_TOKEN", None)
        else:
            os.environ["DISCORD_BOT_TOKEN"] = prev


async def test_the_real_runtime_probes_its_own_adapter():
    """Through the REAL RuntimeManager, not a hand-made probe dict."""
    check.section("C5: the production report measures the real ComputerControl")

    with _tmp() as td:
        rt, m, router = await _runtime(td)
        check(rt._computer.available is False,
              f"production ComputerControl cannot execute "
              f"({rt._computer.available})")

        state = rt._capability_report().state_of("computer_control")
        check(state != "available", f"so the report does not claim it ({state})")

        answer = rt._capability_reply("What are you capable of?") or ""
        check("Right now I can help with" in answer, "an answer is produced")
        can_half = answer.split("Built but not usable")[0]
        check("controlling this computer" not in can_half,
              f"and does not offer computer control ({can_half[-70:]!r})")

        class _Adapter:
            def observe(self, what):
                return {"windows": []}

            def execute(self, kind, target, details):
                return {"ok": True}

        prev_shell = os.environ.get("NOVA_ALLOW_SHELL")
        prev_cc = os.environ.get("NOVA_COMPUTER_CONTROL")
        try:
            os.environ["NOVA_ALLOW_SHELL"] = "1"
            os.environ["NOVA_COMPUTER_CONTROL"] = "1"
            rt._computer._adapter = _Adapter()
            if rt._computer.available:
                check(rt._capability_report().state_of("computer_control") == "available",
                      "with a working adapter installed it becomes available")
            else:
                check(rt._capability_report().state_of("computer_control") != "available",
                      "execution still gated by configuration — and still not claimed")
        finally:
            rt._computer._adapter = None
            for k, v in (("NOVA_ALLOW_SHELL", prev_shell),
                         ("NOVA_COMPUTER_CONTROL", prev_cc)):
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


async def main():
    await test_the_answer_covers_what_is_registered()
    await test_nothing_unregistered_is_claimed()
    await test_disabled_is_reported_as_disabled()
    await test_the_registry_can_grow_without_edits()
    await test_ordinary_turns_are_not_bloated()
    await test_smart_home_behaviour_is_unchanged()
    await test_registered_but_unexecutable_is_not_claimed()
    await test_an_unconfigured_integration_is_not_claimed()
    await test_the_real_runtime_probes_its_own_adapter()
    check.finish()


if __name__ == "__main__":
    run(main)
