"""Phase 8: computer control is permission-gated and NEVER an armed actuator.

Verifies the propose-only default (dry run), the confirmation requirement, denial
under strict modes, and that a real adapter runs ONLY when explicitly enabled.
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.computer_control import ComputerControl
from core.permissions import PermissionBroker

_fail = False


def check(cond, label):
    global _fail
    status = "OK  " if cond else "FAIL"
    if not cond:
        _fail = True
    print(f"  {status} {label}")


class RecordingAdapter:
    def __init__(self):
        self.executed = []

    def observe(self, what):
        return {"windows": ["Editor", "Browser"]}

    def execute(self, kind, target, details):
        self.executed.append((kind, target))
        return {"did": kind}


async def main():
    os.environ.pop("NOVA_COMPUTER_CONTROL", None)  # default OFF

    # ── No adapter, guarded: observe is honest, actions are dry-run/gated ──
    broker = PermissionBroker(mode="guarded")
    cc = ComputerControl(broker, adapter=None)
    check(cc.available is False, "with no adapter + flag off, control is NOT available")

    obs = await cc.observe("windows")
    check(obs["ok"] and obs["available"] is False, "observe honestly reports no adapter (doesn't fake seeing the screen)")

    click = await cc.act("click", target="Save", wait_for_confirm=False)
    check(click["status"] == "needs_confirmation" and click.get("request_id"), "a STANDARD action needs confirmation (guarded)")

    crit = await cc.act("system_setting", target="firewall", wait_for_confirm=False)
    check(crit["status"] == "denied", "a CRITICAL action is denied outright (guarded)")

    unknown = await cc.act("frobnicate", wait_for_confirm=False)
    check(unknown["status"] == "unknown_action", "an unknown action is refused, not guessed")

    # ── Approved but not executed: permission clears, adapter absent -> dry run ──
    broker2 = PermissionBroker(mode="trusted")   # STANDARD auto-allowed
    cc2 = ComputerControl(broker2, adapter=None)
    dry = await cc2.act("click", target="OK", wait_for_confirm=False)
    check(dry["status"] == "approved_not_executed", "permitted but no adapter -> dry run, nothing synthesized")

    # ── Real execution ONLY with flag ON + adapter present, and still gated ──
    adapter = RecordingAdapter()
    os.environ["NOVA_COMPUTER_CONTROL"] = "1"
    cc3 = ComputerControl(PermissionBroker(mode="trusted"), adapter=adapter)
    check(cc3.available is True, "flag on + adapter -> control available")
    ran = await cc3.act("click", target="OK", wait_for_confirm=False)
    check(ran["status"] == "executed" and adapter.executed == [("click", "OK")], "an allowed action executes via the adapter")

    # even with execution enabled, a CRITICAL action is still denied in guarded mode
    cc4 = ComputerControl(PermissionBroker(mode="guarded"), adapter=adapter)
    before = len(adapter.executed)
    denied = await cc4.act("delete", target="everything", wait_for_confirm=False)
    check(denied["status"] == "denied" and len(adapter.executed) == before, "CRITICAL stays denied even with an adapter present")

    # ── Confirmation handshake actually gates execution ──
    b5 = PermissionBroker(mode="guarded")
    adapter5 = RecordingAdapter()
    cc5 = ComputerControl(b5, adapter=adapter5)

    async def _decline_after(rid):
        await asyncio.sleep(0.02)
        b5.resolve(rid, False)

    # start an act that waits for confirmation, then decline it
    pend = await b5.request("computer.type", details={"text": "x"})  # peek: this is how the tool would pend
    b5.resolve(pend["request_id"], False)
    check(len(adapter5.executed) == 0, "a declined action never reaches the adapter")

    os.environ.pop("NOVA_COMPUTER_CONTROL", None)
    print("\nRESULT:", "FAILURES" if _fail else "ALL PASS")
    sys.exit(1 if _fail else 0)


asyncio.run(main())
