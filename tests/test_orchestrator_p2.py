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


def main():
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

    print("\nRESULT:", "FAILURES" if _fail else "ALL PASS")
    sys.exit(1 if _fail else 0)


main()
