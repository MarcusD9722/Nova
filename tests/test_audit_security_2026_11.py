from __future__ import annotations

"""Regression suite for the SECURITY findings of the 2026-11-02 audit.

Every check here failed before the fix it guards. The findings are in
docs/AUDIT_2026-11-02.md; the short version of each is in its own section.

Why these were all green before: `tests/harness.py` substitutes the model, and
nothing drove these endpoints with an attacker's headers. The audit found them
by sending the exact request a sandboxed iframe sends, so that is what runs
here — `Origin: null` with `Sec-Fetch-Site: cross-site`, over the real ASGI app.
"""

import os
import sys
from pathlib import Path
from uuid import uuid4

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")

from harness import Checks, boot, run  # noqa: E402

check = Checks()

#: The headers a page in a sandboxed iframe on any website actually sends.
FOREIGN = {"Origin": "null", "Sec-Fetch-Site": "cross-site"}

PAYLOAD = (
    "import pathlib\n"
    "pathlib.Path(__file__).with_name('EXECUTED_BY_APPLY.txt').write_text('ran')\n"
)


async def test_dev_apply_cannot_execute_without_approval():
    check.section("C1: /dev/apply no longer executes code on nobody's authority")
    # `locked` so the refusal is immediate. The guarded path — where the broker
    # raises a request and WAITS for a human — is covered by
    # test_dev_apply_runs_only_after_a_human_approves below.
    async with boot(default_reply="ok", env={"NOVA_DEV_MODE": "1",
                                             "NOVA_PERMISSION_MODE": "locked"}) as nova:
        http = nova.http
        target = "core/audit_probe_c1.py"
        marker = nova.root / "core" / "EXECUTED_BY_APPLY.txt"
        (nova.root / "core").mkdir(parents=True, exist_ok=True)

        r = await http.post("/dev/propose", headers=FOREIGN,
                            json={"path": target, "new_content": PAYLOAD,
                                  "reason": "audit probe"})
        # Propose itself is now origin-checked. Either answer is safe; what
        # must never happen is an APPLY that runs.
        proposal_id = r.json().get("proposal_id") if r.status_code == 200 else None
        check(r.status_code in (200, 403),
              f"a foreign origin proposing gets an answer, not a crash ({r.status_code})")

        if proposal_id:
            applied = await http.post("/dev/apply", headers=FOREIGN,
                                      json={"proposal_id": proposal_id, "confirm": True})
            check(applied.status_code == 403,
                  f"and applying it is refused ({applied.status_code}: "
                  f"{str(applied.json().get('detail'))[:80]})")
            body = applied.json()
            check("applied" not in str(body.get("status") or ""),
                  "the response does not claim the change was applied")

        check(not marker.exists(),
              "the payload never ran — nothing wrote its marker file")
        check(not (nova.root / target).exists(),
              "and the file it would have executed was never written")


async def test_dev_apply_needs_the_broker_even_from_the_ui():
    check.section("C1: approval comes from the broker, not from the caller's own flag")
    async with boot(default_reply="ok", env={"NOVA_DEV_MODE": "1",
                                             "NOVA_PERMISSION_MODE": "locked"}) as nova:
        http = nova.http
        (nova.root / "core").mkdir(parents=True, exist_ok=True)
        (nova.root / "core" / "probe_c1b.py").write_text("X = 1\n", encoding="utf-8")

        r = await http.post("/dev/propose", json={"path": "core/probe_c1b.py",
                                                  "new_content": "X = 2\n"})
        check(r.status_code == 200, f"a same-origin proposal is accepted ({r.status_code})")
        pid = r.json()["proposal_id"]

        # `confirm=True` is the caller asserting the UI showed a diff. In
        # `locked` mode the broker refuses regardless, which is the point:
        # the flag is not the authority.
        applied = await http.post("/dev/apply", json={"proposal_id": pid, "confirm": True})
        check(applied.status_code == 403,
              f"apply is refused in locked mode despite confirm=true ({applied.status_code})")
        check((nova.root / "core" / "probe_c1b.py").read_text(encoding="utf-8") == "X = 1\n",
              "and the file on disk is untouched")

        audit = (await http.get("/permissions/audit")).json()
        caps = [e.get("capability") for e in audit.get("audit", [])]
        check("dev.apply" in caps, f"the refusal is in the permission audit trail ({caps[-3:]})")


async def test_dev_apply_runs_only_after_a_human_approves():
    check.section("C1: the guarded path raises a real request and waits for it")
    import asyncio

    async with boot(default_reply="ok", env={"NOVA_DEV_MODE": "1",
                                             "NOVA_PERMISSION_MODE": "guarded"}) as nova:
        http = nova.http
        # A non-Python file on purpose: `apply_proposal` ends by IMPORTING a .py
        # module it wrote, and in a temp root that import legitimately fails and
        # auto-reverts. What is under test here is the approval handshake, so
        # the target is chosen to leave the boot test out of it.
        src = nova.root / "docs" / "probe_c1c.md"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("X = 1\n", encoding="utf-8")
        pid = (await http.post("/dev/propose",
                               json={"path": "docs/probe_c1c.md",
                                     "new_content": "X = 2\n"})).json()["proposal_id"]

        broker = nova.runtime.permission_broker
        task = asyncio.create_task(http.post("/dev/apply",
                                             json={"proposal_id": pid, "confirm": True}))
        # The request must be WAITING, not already applied.
        waited = None
        for _ in range(100):
            await asyncio.sleep(0.05)
            live = [p for p in broker.pending() if p.get("capability") == "dev.apply"]
            if live:
                waited = live[0]
                break
        check(waited is not None, "apply is parked on a pending dev.apply request")
        check(src.read_text(encoding="utf-8") == "X = 1\n",
              "and nothing is written while it waits")
        if waited is not None:
            check(str(waited.get("details", {}).get("proposal_id")) == pid,
                  "the request names the proposal it is about")
            broker.resolve(str(waited["request_id"]), True)
        res = await task
        check(res.status_code == 200, f"approving lets it through ({res.status_code})")
        check(src.read_text(encoding="utf-8") == "X = 2\n",
              "and the approved change IS applied")


async def test_api_token_is_enforced_when_set():
    check.section("C1: with a token set, an unauthenticated caller gets nothing")
    token = "audit-token-" + uuid4().hex[:8]
    async with boot(default_reply="ok", env={"NOVA_API_TOKEN": token,
                                            "NOVA_DEV_MODE": "1"}) as nova:
        http = nova.http
        for path, payload in (("/dev/propose", {"path": "core/x.py", "new_content": "X=1\n"}),
                              ("/chat", {"message": "hello"}),
                              ("/permissions/resolve", {"request_id": "x", "approved": True})):
            r = await http.post(path, json=payload, headers=FOREIGN)
            check(r.status_code == 401, f"POST {path} without the token -> 401 ({r.status_code})")
        r = await http.get("/memory/recent", headers=FOREIGN)
        check(r.status_code == 401, f"GET /memory/recent without the token -> 401 ({r.status_code})")
        r = await http.get("/memory/recent", headers={**FOREIGN,
                                                      "Authorization": f"Bearer {token}"})
        check(r.status_code == 200, f"and 200 with it ({r.status_code})")


async def test_code_read_refuses_secrets():
    check.section("H10: code.read shares dev mode's secret deny-list")
    async with boot(default_reply="ok") as nova:
        (nova.root / ".env").write_text("NOVA_CLOUD_API_KEY=sk-AUDIT-SECRET\n", encoding="utf-8")
        from core.tool_router import ToolCall

        for path in (".env", str(nova.root / ".env"), "memory_data/sqlite/nova.sqlite3"):
            res = await nova.runtime.router.execute(ToolCall(name="code.read",
                                                             args={"path": path}), retries=0)
            body = str(res.result)
            check("AUDIT-SECRET" not in body,
                  f"code.read({path!r}) returns no secret material")
            check(res.result.get("error") == "refused_secret_path" if isinstance(res.result, dict) else False,
                  f"and says why it refused ({str(res.result)[:60]})")


async def test_locked_mode_means_read_only():
    check.section("H10: locked mode stops the actuators it always claimed to")
    async with boot(default_reply="ok", env={"NOVA_PERMISSION_MODE": "locked",
                                             "NOVA_ALLOW_SHELL": "1"}) as nova:
        from core.tool_router import ToolCall

        router = nova.runtime.router
        probe = nova.root / "shell_probe.txt"
        res = await router.execute(ToolCall(name="shell.exec",
                                            args={"cmd": f'echo hi > "{probe}"'}),
                                   retries=0)
        check(not (res.result or {}).get("ok"),
              f"shell.exec is refused in locked mode ({str(res.result)[:70]})")
        check(not probe.exists(), "and the command did not run")

        written = nova.root / "projects" / "locked_write.py"
        res = await router.execute(ToolCall(name="code.write",
                                            args={"path": str(written), "content": "X = 1\n"}),
                                   retries=0)
        check(not (res.result or {}).get("ok"),
              f"code.write is refused in locked mode ({str(res.result)[:70]})")
        check(not written.exists(), "and no file was written")

        # …while LOOKING is still allowed, in the mode documented as read-only.
        res = await router.execute(ToolCall(name="shell.exec",
                                            args={"cmd": "git --version"}), retries=0)
        check((res.result or {}).get("ok"), f"a read-only command still runs ({str(res.result)[:70]})")


async def test_shell_classification():
    check.section("H10: what counts as a read-only command")
    from core.tooling import build_tool_router  # noqa: F401  (module import side effects)
    import core.tooling as T

    # The classifier is a closure, so drive it the way production does: through
    # the capability each command asks the broker for. `locked` keeps the
    # refusal immediate — what is under test is which capability was asked for.
    async with boot(default_reply="ok", env={"NOVA_PERMISSION_MODE": "locked"}) as nova:
        from core.tool_router import ToolCall
        from core.permissions import tier_of

        check(tier_of("shell.read") == 0 and tier_of("shell.exec") == 2,
              "shell.read is READ and shell.exec is ADMIN")
        # A chained command must never classify as read-only, however harmless
        # its first token looks.
        probe = nova.root / "chained.txt"
        res = await nova.runtime.router.execute(
            ToolCall(name="shell.exec",
                     args={"cmd": f'git --version && echo hi > "{probe}"'}), retries=0)
        check((res.result or {}).get("capability") == "shell.exec",
              f"`git --version && echo ...` asks for shell.exec ({str(res.result)[:90]})")
        check(not probe.exists(), "and it did not run without approval")
        del T


async def test_agent_loop_cannot_call_a_skipped_tool():
    check.section("H10: the agent allowlist is enforced, not just displayed")
    async with boot(default_reply="ok") as nova:
        from core.orchestrator.agent import Agent, _DEFAULT_SKIP

        rt = nova.runtime
        executor = rt._tool_loop if hasattr(rt, "_tool_loop") else None
        if executor is None:  # name drift: find it by duck type
            for attr in dir(rt):
                v = getattr(rt, attr, None)
                if hasattr(v, "decide") and hasattr(v, "run"):
                    executor = v
                    break
        check(executor is not None, "found the tool-loop executor")
        agent = Agent(name="audit")
        check("code.write" in _DEFAULT_SKIP, "code.write is on the default skip list")
        allowed = set(executor._allowed(agent))
        check("code.write" not in allowed, "and so is absent from what the agent may call")

        # The model naming it anyway must be refused. ScriptedLLM replies with
        # whatever the rule says, so this is the real decide() path.
        nova.llm.when(lambda p: "agent brain for Nova" in p,
                      '{"action": "tool", "tool": "code.write", '
                      '"args": {"path": "core/evil.py", "content": "X=1"}}')
        decision = await executor.decide(agent=agent, user_text="write a file",
                                         grounding="", tool_results=[])
        check(decision is None,
              f"decide() refuses a skipped tool the model asked for ({decision})")
        check(not (nova.root / "core" / "evil.py").exists(), "and nothing was written")


async def test_unrecognised_voice_cannot_touch_a_project():
    check.section("H10: an unknown voice may not start or change a project")
    async with boot(default_reply="ok") as nova:
        from core.turn_identity import TurnIdentity, active_turn

        rt = nova.runtime
        pb = rt._project_builder
        d = nova.root / "projects" / "flappy-bird"
        d.mkdir(parents=True, exist_ok=True)
        (d / "PROJECT.md").write_text("# flappy-bird\n\n## Status\nbuilt\n", encoding="utf-8")
        (d / "main.py").write_text("X = 1\n", encoding="utf-8")
        await pb.select("flappy-bird")

        with active_turn(TurnIdentity.voice_unverified()):
            reply = await rt._project_prepass("Add a pause button to the game.", uuid4())
        check(not (reply or "").lower().startswith(("got it", "on it", "starting",
                                                     "resuming", "working")),
              f"the pre-pass does not accept the edit ({str(reply)[:60]!r})")
        check((d / "main.py").read_text(encoding="utf-8") == "X = 1\n",
              "and the project file is unchanged")

        # The owner is unaffected: same sentence, same project, accepted.
        with active_turn(TurnIdentity.typed()):
            owner_reply = await rt._project_prepass("Add a pause button to the game.", uuid4())
        check(owner_reply is not None,
              f"while Marcus's identical message IS acted on ({str(owner_reply)[:60]!r})")


async def main():
    await test_dev_apply_cannot_execute_without_approval()
    await test_dev_apply_needs_the_broker_even_from_the_ui()
    await test_dev_apply_runs_only_after_a_human_approves()
    await test_api_token_is_enforced_when_set()
    await test_code_read_refuses_secrets()
    await test_locked_mode_means_read_only()
    await test_shell_classification()
    await test_agent_loop_cannot_call_a_skipped_tool()
    await test_unrecognised_voice_cannot_touch_a_project()
    check.finish()


if __name__ == "__main__":
    run(main)
