"""U8: test-first repair reports what was VERIFIED, never what was assumed.

This is the structural answer to the flappy-bird failure: Nova claimed a fix
four times while the bug remained, because the run check proves a program
STARTS and a frozen window starts perfectly. A check that reproduces the bug —
failing before, passing after — is the only thing that can tell the difference.
"""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.project_builder import ProjectBuilder

_fail = False


def check(cond, label):
    global _fail
    status = "OK  " if cond else "FAIL"
    if not cond:
        _fail = True
    print(f"  {status} {label}")


async def main():
    S = ProjectBuilder.summarize_repair

    # ── The four honest outcomes ──
    verified = S(reproduced=True, passed_after=True)
    check("verified" in verified.lower() and "not assumed" in verified.lower(),
          "reproduced-then-passing is reported as VERIFIED")

    still_broken = S(reproduced=True, passed_after=False)
    check("could NOT" in still_broken and "still there" in still_broken,
          "reproduced-but-still-failing admits the bug is NOT fixed")
    check("fixed" not in still_broken.replace("haven't fixed", ""),
          "a failed repair never claims a fix")

    bad_check = S(reproduced=False, passed_after=True)
    check("PASSED before" in bad_check and "unverified" in bad_check,
          "a check that passed BEFORE the fix is called out as not capturing the problem")

    no_check = S(reproduced=None, passed_after=None)
    check("NOT verified" in no_check, "no check written -> explicitly unverified, asks Marcus to run it")

    # None of the outcomes may use the language that caused the original failure.
    for label, text in [("verified", verified), ("still broken", still_broken),
                        ("bad check", bad_check), ("no check", no_check)]:
        low = text.lower()
        check("resolved the" not in low and "stabilized" not in low,
              f"'{label}' avoids the false-success wording ('resolved'/'stabilized')")

    # ── run_repro_check: real pass/fail/timeout behavior ──
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        path = Path(td)
        pb = ProjectBuilder(projects_dir=path, llm=None, llm_semaphore=asyncio.Semaphore(1), memory=None)

        (path / "pass.py").write_text("print('all good')\nimport sys; sys.exit(0)\n", encoding="utf-8")
        ok, out = await pb.run_repro_check(path, "pass.py")
        check(ok is True and "all good" in out, "a passing check reports pass + its output")

        (path / "fail.py").write_text("print('bug present')\nimport sys; sys.exit(1)\n", encoding="utf-8")
        ok, out = await pb.run_repro_check(path, "fail.py")
        check(ok is False and "bug present" in out, "a failing check reports fail + its output")

        (path / "boom.py").write_text("raise RuntimeError('kaboom')\n", encoding="utf-8")
        ok, out = await pb.run_repro_check(path, "boom.py")
        check(ok is False and "kaboom" in out, "a crashing check counts as failure, with the traceback")

        (path / "hang.py").write_text("import time; time.sleep(30)\n", encoding="utf-8")
        ok, out = await pb.run_repro_check(path, "hang.py", timeout_s=0.5)
        check(ok is False and "timed out" in out, "a hanging check is a FAILURE, not a pass (the freeze case)")

    print("\nRESULT:", "FAILURES" if _fail else "ALL PASS")
    sys.exit(1 if _fail else 0)


asyncio.run(main())
