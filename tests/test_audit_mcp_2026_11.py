"""Audit 2026-11-02, H13: MCP against the REAL PermissionBroker.

`McpManager._check_permission` asked for `verdict["allowed"]`. The broker has
never returned that key — it returns `{"decision": allowed | denied |
needs_confirmation}` — so every MCP call was refused in every mode, read-only
tools included, and every needs_confirmation request was left pending and
resolvable long after the caller had gone.

tests/test_mcp_v3.py passed throughout, because its fake broker returned the
made-up shape. So this suite uses `core.permissions.PermissionBroker` itself,
and asserts the one thing a fake can never check: that the two sides agree
about what a verdict looks like.

Every MCP capability is ADMIN by design (core/mcp/registry.py:_permission_for
keeps the namespaced id so a policy can allowlist one specifically; unknown
capabilities default to ADMIN). Under the mode table that means an
un-allowlisted remote tool is DENIED in locked mode and needs confirmation in
both guarded and trusted -- trusted auto-runs up to STANDARD, not ADMIN. A
server's own readOnlyHint deliberately does not lower its tier, so "it only
reads" is not a reason to skip the click; an operator allowlisting the exact
capability in CAPABILITY_TIERS is.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")

from core.mcp.manager import McpManager  # noqa: E402
from core.mcp.session import ServerConfig  # noqa: E402
from core import permissions  # noqa: E402
from core.permissions import PermissionBroker  # noqa: E402
from harness import Checks  # noqa: E402
from memory.artifacts import ArtifactStore  # noqa: E402

check = Checks()

FAKE = REPO / "tests" / "fake_mcp_server.py"
READ_TOOL = "mcp:docs:search_docs"
WRITE_TOOL = "mcp:docs:delete_everything"


def cfg(server_id: str = "docs", mode: str = "normal") -> ServerConfig:
    return ServerConfig(server_id=server_id, command=sys.executable,
                        args=[str(FAKE)], env={"NOVA_FAKE_MCP_MODE": mode})


def _broker(mode: str) -> PermissionBroker:
    """A real broker, with its audit log in a temp file."""
    tmp = Path(tempfile.mkdtemp(prefix="nova_mcp_perm_")) / "audit.jsonl"
    return PermissionBroker(mode=mode, audit_path=tmp)


async def _manager(broker: PermissionBroker) -> McpManager:
    mgr = McpManager(permission_broker=broker, artifact_store=ArtifactStore())
    await mgr.add_server(cfg())
    return mgr


async def test_an_allowlisted_capability_runs_with_no_click() -> None:
    """The regression in one line: a real broker saying yes must mean yes.

    An MCP capability reaches "allowed" only when an operator has put that
    exact id in CAPABILITY_TIERS, which is the one path where the broker
    returns {"decision": "allowed"} for MCP -- and the path the missing key
    made unreachable. Patched here and restored, so the shipped policy is
    unchanged.
    """
    check.section("an allowlisted MCP capability: allowed, and it runs")
    permissions.CAPABILITY_TIERS[READ_TOOL] = permissions.READ
    try:
        broker = _broker("guarded")  # the DEFAULT mode, not a permissive one
        verdict = await broker.request(READ_TOOL)
        check(verdict.get("decision") == "allowed",
              f"an allowlisted READ capability is allowed outright ({verdict})")

        mgr = await _manager(_broker("guarded"))
        try:
            res = await mgr.call(READ_TOOL, {"q": "nova"},
                                 conversation_id="c", turn_id="t")
            check(res.get("ok") is True and not res.get("denied"),
                  f"and the call goes through ({str(res)[:80]})")
            check("result" in res and res.get("server") == "docs",
                  "coming back with the server's own result")
            # Before the fix this was the whole story: allowed by policy,
            # refused by a key that does not exist.
            check("permission" not in str(res.get("error") or "").lower(),
                  "no phantom permission error from a mismatched verdict shape")
        finally:
            await mgr.close()
    finally:
        permissions.CAPABILITY_TIERS.pop(READ_TOOL, None)
    check(permissions.tier_of(READ_TOOL) == permissions.ADMIN,
          "the shipped policy is restored: MCP is ADMIN again")


async def test_trusted_mode_still_asks_about_a_remote_tool() -> None:
    """Trusted is not a bypass: ADMIN is above its auto_max."""
    check.section("trusted mode: a remote tool still needs a click")
    broker = _broker("trusted")
    verdict = await broker.request(READ_TOOL)
    check(verdict.get("decision") == "needs_confirmation",
          f"even read-only-hinted MCP asks in trusted mode ({verdict.get('decision')!r})")
    check(verdict.get("request_id"), "and hands back a request to resolve")
    broker.resolve(str(verdict.get("request_id")), False)


async def test_locked_mode_denies_without_leaving_anything_pending() -> None:
    check.section("locked mode: denied, and nothing left to click later")
    broker = _broker("locked")
    mgr = await _manager(broker)
    try:
        res = await mgr.call(WRITE_TOOL, {"repo": "nova"}, conversation_id="c", turn_id="t")
        check(not res.get("ok") and res.get("denied") is True,
              "a destructive MCP tool is refused in locked mode")
        check("denied" in str(res.get("error") or "").lower(),
              f"and says it was denied ({str(res.get('error'))[:70]!r})")
        check(not broker.pending(),
              f"no request is left pending ({len(broker.pending())})")
    finally:
        await mgr.close()


async def test_guarded_mode_asks_and_an_approval_runs_it() -> None:
    """needs_confirmation has to be WAITED on, not read as a refusal."""
    check.section("guarded mode: it asks, and Approve runs the tool")
    broker = _broker("guarded")
    mgr = await _manager(broker)
    try:
        call = asyncio.create_task(
            mgr.call(WRITE_TOOL, {"repo": "nova"}, conversation_id="c", turn_id="t"))

        approved = False
        for _ in range(100):  # up to ~2s for the request to appear
            await asyncio.sleep(0.02)
            pend = broker.pending()
            if pend:
                rid = pend[0].get("request_id") or pend[0].get("id")
                check(str(pend[0].get("capability")) == WRITE_TOOL,
                      f"the pending request names the namespaced capability "
                      f"({pend[0].get('capability')!r})")
                approved = broker.resolve(str(rid), True)
                break
        check(approved, "guarded mode produced a request to approve "
                        "(it used to refuse outright and leave it pending)")

        res = await asyncio.wait_for(call, timeout=30)
        check(res.get("ok") is True,
              f"approving it runs the tool ({str(res)[:80]})")
        check(not broker.pending(),
              f"and nothing stays pending afterwards ({len(broker.pending())})")
    finally:
        await mgr.close()


async def test_guarded_mode_rejection_refuses_and_clears() -> None:
    check.section("guarded mode: Decline refuses, and clears the request")
    broker = _broker("guarded")
    mgr = await _manager(broker)
    try:
        call = asyncio.create_task(
            mgr.call(WRITE_TOOL, {"repo": "nova"}, conversation_id="c", turn_id="t"))
        rejected = False
        for _ in range(100):
            await asyncio.sleep(0.02)
            pend = broker.pending()
            if pend:
                rid = str(pend[0].get("request_id") or pend[0].get("id"))
                rejected = broker.resolve(rid, False)
                break
        check(rejected, "the request was there to decline")
        res = await asyncio.wait_for(call, timeout=30)
        check(not res.get("ok") and res.get("denied") is True,
              f"declining refuses the call ({str(res.get('error'))[:70]!r})")
        check("approve" in str(res.get("error") or "").lower()
              or "not approve" in str(res.get("error") or "").lower(),
              "and says it was not approved, rather than 'denied by policy'")
        check(not broker.pending(),
              f"the pending request is gone ({len(broker.pending())})")
    finally:
        await mgr.close()


async def test_an_unrecognised_verdict_fails_closed() -> None:
    """A broker change must not be able to turn into an accidental allow."""
    check.section("an unknown verdict shape is a refusal, not permission")

    class StrangeBroker:
        mode = "guarded"

        async def request(self, capability, details=None):
            return {"allowed": True}  # the shape this bug assumed

        def pending(self):
            return []

    mgr = McpManager(permission_broker=StrangeBroker(), artifact_store=ArtifactStore())
    await mgr.add_server(cfg())
    try:
        res = await mgr.call(WRITE_TOOL, {"repo": "nova"})
        check(not res.get("ok"),
              f"{{'allowed': True}} is NOT permission ({str(res.get('error'))[:70]!r})")
        check("unrecognised" in str(res.get("error") or "").lower(),
              "and the refusal names the mismatch instead of inventing a reason")
    finally:
        await mgr.close()


async def test_the_verdict_keys_are_the_brokers_own() -> None:
    """Pin the contract both sides depend on, so neither drifts silently."""
    check.section("the verdict contract, asserted against the broker itself")
    # A capability that IS allowlisted, to pin the "allowed" verdict's shape.
    allowed = await _broker("trusted").request("shell.read")
    check(set(allowed) == {"decision"} and allowed["decision"] == "allowed",
          f"allowed -> {allowed}")
    check("allowed" not in allowed,
          "there is no 'allowed' KEY — that was the bug, in one assertion")

    denied = await _broker("locked").request("mcp:docs:delete_everything")
    check(denied.get("decision") == "denied" and "reason" in denied,
          f"locked -> {denied}")

    guarded = _broker("guarded")
    confirm = await guarded.request("mcp:docs:delete_everything")
    check(confirm.get("decision") == "needs_confirmation" and confirm.get("request_id"),
          f"guarded -> {{'decision': {confirm.get('decision')!r}, "
          f"'request_id': {bool(confirm.get('request_id'))}}}")
    guarded.resolve(str(confirm["request_id"]), False)


async def main() -> None:
    await test_the_verdict_keys_are_the_brokers_own()
    await test_an_allowlisted_capability_runs_with_no_click()
    await test_trusted_mode_still_asks_about_a_remote_tool()
    await test_locked_mode_denies_without_leaving_anything_pending()
    await test_guarded_mode_asks_and_an_approval_runs_it()
    await test_guarded_mode_rejection_refuses_and_clears()
    await test_an_unrecognised_verdict_fails_closed()
    check.finish()


if __name__ == "__main__":
    asyncio.run(main())
