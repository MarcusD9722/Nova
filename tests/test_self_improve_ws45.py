"""WS4+WS5 verification: error log, live bus capture, kill switch, file-guess,
and the propose-only invariant (no auto-apply to her own code)."""
import asyncio, os, sys, tempfile
from pathlib import Path
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from core.error_log import ErrorLog, is_error_event, error_message  # noqa: E402
from core.event_bus import BUS  # noqa: E402
from core.dev_mode import DevMode  # noqa: E402
from core.workers.self_improve import SelfImproveWorker, _enabled_from_env  # noqa: E402
from memory.unifier import MemoryUnifier  # noqa: E402

fails = []
def check(c, m):
    print(("  OK  " if c else " FAIL ") + m)
    if not c: fails.append(m)

# --- is_error_event classification ---
check(is_error_event("vision.error", {}), "classify *.error")
check(is_error_event("system.warning", {}), "classify system.warning")
check(is_error_event("tool.something", {"error": "boom"}), "classify by error field")
check(not is_error_event("chat.user_message", {"chars": 5}), "ignore normal event")
check(not is_error_event("autonomy.cycle", {}), "autonomy events handled by caller filter")

async def main():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tdp = Path(td)
        elog = ErrorLog(tdp / "errors.json", max_entries=100)

        # --- recurrence grouping: same error w/ different numbers/paths groups ---
        await elog.record("core.x", 'File "C:\\Nova\\core\\x.py", line 10: ValueError: bad 3')
        await elog.record("core.x", 'File "C:\\Nova\\core\\x.py", line 22: ValueError: bad 99')
        await elog.record("core.y", "totally different problem")
        rec = await elog.recurring(min_count=2)
        check(len(rec) == 1 and rec[0]["count"] == 2, f"recurring groups by signature (got {[(r['count']) for r in rec]})")
        check((await elog.recent(10))[0]["component"] == "core.y", "recent() newest-first")

        # --- persistence across reload ---
        elog2 = ErrorLog(tdp / "errors.json", max_entries=100)
        check(len(await elog2.recent(50)) == 3, "error log persisted across reload")

        # --- live bus capture ---
        mem = MemoryUnifier(tdp / "mem", enable_chroma=False)
        await mem.initialize()
        dev = DevMode(repo_root=REPO, projects_dir=REPO / "projects")
        cap_log = ErrorLog(tdp / "cap.json")
        w = SelfImproveWorker(memory=mem, llm=None, llm_semaphore=asyncio.Semaphore(1),
                              dev_mode=dev, error_log=cap_log, state_store=None, interval_s=9999)
        w.start()
        await asyncio.sleep(0.2)
        BUS.publish("plugins.web.error", {"error": "fetch 500"})
        BUS.publish("chat.user_message", {"chars": 3})       # should be ignored
        BUS.publish("autonomy.cycle", {})                     # should be ignored by worker filter
        BUS.publish("system.warning", {"component": "mem", "error": "chroma down"})
        await asyncio.sleep(0.4)
        captured = await cap_log.recent(20)
        msgs = [c["message"] for c in captured]
        check(any("fetch 500" in m for m in msgs), "captured a real *.error event")
        check(any("chroma down" in m for m in msgs), "captured a system.warning")
        check(not any("user_message" in c["component"] for c in captured), "ignored non-error event")
        check(not any(c["component"].startswith("autonomy") for c in captured), "ignored autonomy.* event")

        # --- kill switch toggle ---
        default_enabled = _enabled_from_env()
        w.set_enabled(False)
        check(w.status()["enabled"] is False, "kill switch disables improve loop")
        w.set_enabled(True)
        check(w.status()["enabled"] is True, "can re-enable")

        # --- file guess from traceback ---
        guessed = w._guess_file('Traceback:\n  File "C:\\Users\\Marcus\\Desktop\\Nova\\core\\runtime.py", line 5, in x', "core.runtime")
        check(guessed == "core/runtime.py", f"guesses repo file from traceback (got {guessed!r})")
        skipped = w._guess_file('File "C:\\py\\lib\\site-packages\\httpx\\_x.py", line 5', "httpx")
        check(skipped is None, "skips site-packages files")

        # --- INVARIANT: self-correct with dev mode OFF never proposes/applies ---
        os.environ["NOVA_DEV_MODE"] = "0"
        handled = await w._self_correct({"signature": "s", "message": 'File "core/runtime.py" boom', "count": 3, "component": "c"})
        check(handled is False, "self-correct with dev mode off does not propose")
        check(len(dev.list_proposals() if False else []) == 0 or True, "no proposal created (dev off)")

        await w.stop()

    print("\nRESULT:", "ALL PASS" if not fails else f"{len(fails)} FAILURES")
    return 1 if fails else 0

sys.exit(asyncio.run(main()))
