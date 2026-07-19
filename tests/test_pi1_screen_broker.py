import asyncio
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from core.screen_broker import ScreenCaptureBroker

_fail = False


def check(cond, label):
    global _fail
    status = "OK  " if cond else "FAIL"
    if not cond:
        _fail = True
    print(f"  {status} {label}")


async def main():
    broker = ScreenCaptureBroker()

    # Approve path
    rid, fut = broker.new_request()
    ok = broker.resolve(rid, {"approved": True, "text": "A code editor with an error highlighted."})
    check(ok is True, "resolve() returns True for a pending request")
    result = await asyncio.wait_for(fut, timeout=1.0)
    check(result["approved"] is True, "future resolves with approved=True")
    check("error" in result["text"], "future carries the analysis text")

    # Decline path
    rid2, fut2 = broker.new_request()
    broker.resolve(rid2, {"approved": False})
    result2 = await asyncio.wait_for(fut2, timeout=1.0)
    check(result2["approved"] is False, "decline resolves with approved=False")

    # Resolving twice / unknown id -> False, no crash
    check(broker.resolve(rid, {"approved": True}) is False, "resolving an already-resolved request returns False")
    check(broker.resolve("nonexistent", {"approved": True}) is False, "resolving an unknown request_id returns False")

    # Timeout path (simulated directly, matching the tool's asyncio.wait_for(..., timeout=30.0))
    rid3, fut3 = broker.new_request()
    try:
        await asyncio.wait_for(fut3, timeout=0.1)
        check(False, "should have timed out")
    except (TimeoutError, asyncio.TimeoutError):
        check(True, "unresolved request times out as expected")
        broker.cancel(rid3)
    check(broker.resolve(rid3, {"approved": True}) is False, "a cancelled request can't be resolved late")

    print("\nRESULT:", "FAILURES" if _fail else "ALL PASS")
    sys.exit(1 if _fail else 0)


asyncio.run(main())
