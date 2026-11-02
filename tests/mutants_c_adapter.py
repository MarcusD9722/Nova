"""The Windows-adapter mutation campaign, M301 onwards — the guards.

The adapter's value is almost entirely in what it REFUSES. A suite that drives
it through a fake can pass while every refusal has quietly stopped working, so
each guard is deleted here in turn and the suite has to notice. A guard no
mutant can kill is a guard that is not being tested, whatever the green count
says.

Two guards are singled out because they are the ones whose absence would be
invisible in normal use:

  * M301 — the observation gate. Without it, installing the adapter silently
    turns window enumeration on, because `computer.observe` is READ tier and
    auto-allowed in every policy mode. Nothing would error; Nova would simply
    start being able to read every window title.
  * M315 — the execution gate in `available`. Without it, the adapter alone
    arms the actuator and `NOVA_COMPUTER_CONTROL` stops meaning anything.

A mutant counts as KILLED only when it compiles, reaches the production path it
targets, and a NAMED assertion fails for the reason the mutant describes.

Usage:
    venv\\Scripts\\python.exe tests\\mutants_c_adapter.py            # all
    venv\\Scripts\\python.exe tests\\mutants_c_adapter.py M301 M304  # some
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY = REPO / "venv" / "Scripts" / "python.exe"

ADAPTER = "tests/test_computer_windows_adapter.py"
FROZEN_P8 = "tests/test_computer_control_p8.py"
#: The P5.2 contract pins `adapter_installed is False` on a stock boot.
P52_CONTRACT = "tests/test_speaker_api_contract_v52.py"
ALL_SUITES = [ADAPTER, FROZEN_P8, P52_CONTRACT]

CW = "core/computer_windows.py"
CC = "core/computer_control.py"
RT = "core/runtime.py"

#: (id, description, file, old, new, suites)
MUTANTS: list[tuple] = [
    ("M301", "observation is live the moment an adapter exists", CC,
     '    return os.getenv("NOVA_COMPUTER_OBSERVE", "0").strip().lower() in _TRUTHY',
     '    return True',
     [ADAPTER]),

    ("M302", "an unset deny-list variable means deny nothing", CW,
     '    if not raw:\n        return DEFAULT_DENY_WINDOWS',
     '    if not raw:\n        return ()',
     [ADAPTER]),

    ("M303", "the focus assertion is ignored", CW,
     '        want = str(details.get("window") or "").strip()',
     '        want = ""',
     [ADAPTER]),

    ("M304", "coordinates are not bounds-checked", CW,
     '        if not (vx <= x < vx + vw and vy <= y < vy + vh):',
     '        if False:',
     [ADAPTER]),

    ("M305", "CRITICAL kinds are no longer structurally refused", CW,
     '    REFUSED_KINDS = {\n        "system_setting": "change a Windows system setting",\n'
     '        "delete": "delete a file or folder",\n    }',
     '    REFUSED_KINDS: dict[str, str] = {}',
     [ADAPTER]),

    ("M306", "a refusal is reported as an execution error", CC,
     '            return {"ok": False, "status": "refused_by_adapter",\n'
     '                    "action": {"kind": kind, **details}, "note": str(e)[:300]}',
     '            return {"ok": False, "status": "execution_error",\n'
     '                    "action": {"kind": kind, **details}, "error": str(e)[:300]}',
     [ADAPTER]),

    ("M307", "file_write overwrites an existing file silently", CW,
     '        if existed and not bool(details.get("overwrite")):',
     '        if False:',
     [ADAPTER]),

    ("M308", "an unreadable foreground window is treated as safe", CW,
     '        if not title:\n            raise AdapterRefusal(\n'
     '                "I can\'t read which window has focus, so I won\'t send input into it.")',
     '        if False:\n            raise AdapterRefusal("unreachable")',
     [ADAPTER]),

    ("M309", "an astral character is truncated instead of surrogate-paired", CW,
     '    cp = ord(ch)\n    if cp <= 0xFFFF:\n        return (cp,)',
     '    cp = ord(ch)\n    if True:\n        return (cp & 0xFFFF,)',
     [ADAPTER]),

    ("M310", "the runtime does not actually hold the adapter", RT,
     '        self._computer = ComputerControl(self._permission_broker,\n'
     '                                         adapter=default_adapter())',
     '        self._computer = ComputerControl(self._permission_broker, adapter=None)',
     [ADAPTER]),

    ("M311", "the typing length cap is gone", CW,
     '        if len(text) > MAX_TYPE_CHARS:',
     '        if False:',
     [ADAPTER]),

    ("M312", "launch_app accepts shell metacharacters", CW,
     '        bad = [c for c in "&|;\\n\\r<>" if c in app or c in args]',
     '        bad: list[str] = []',
     [ADAPTER]),

    ("M313", "close_app will dismiss a protected window", CW,
     '        low = want.lower()\n        for pattern in deny_list():\n'
     '            if pattern in low:\n                raise AdapterRefusal(\n'
     '                    f"{want!r} matches the protected pattern {pattern!r} — "\n'
     '                    f"dismissing a window like that is not mine to do.")',
     '        low = want.lower()',
     [ADAPTER]),

    ("M314", "a partial injection is reported as a clean success", CW,
     '        if sent != len(events):',
     '        if False:',
     [ADAPTER]),

    ("M315", "an adapter alone arms the actuator", CC,
     '        return execution_enabled() and self._adapter is not None',
     '        return self._adapter is not None',
     [ADAPTER, FROZEN_P8]),

    ("M316", "the clipboard ignores what is in front of it", CW,
     '        fg = self._api.foreground_window() or {}\n'
     '        low = str(fg.get("title") or "").lower()\n'
     '        for pattern in deny_list():\n'
     '            if pattern in low:\n                raise AdapterRefusal(\n'
     '                    f"{str(fg.get(\'title\'))[:60]!r} is in front (matches "\n'
     '                    f"{pattern!r}) — I will not overwrite the clipboard now.")',
     '        pass',
     [ADAPTER]),

    ("M317", "file_write creates any missing directory tree", CW,
     '        if not path.parent.is_dir():',
     '        if False:',
     [ADAPTER]),

    ("M318", "a relative file_write path is resolved against the cwd", CW,
     '        if not path.is_absolute():',
     '        if False:',
     [ADAPTER]),

    # The bug wiring an adapter would have turned loose: a learned skill's
    # steps are free-text descriptions, so executing them as typed input dumps
    # prose into whatever window has focus.
    ("M319", "skill.run types its step descriptions for real", RT,
     '                    wait_for_confirm=False, dry_run=True)})',
     '                    wait_for_confirm=False)})',
     [ADAPTER]),

    ("M320", "dry_run is accepted and then ignored", CC,
     '        if dry_run or not self.available:',
     '        if not self.available:',
     [ADAPTER]),

    # The regression the full gate caught. Installing an adapter on a stock
    # boot makes `adapter_installed` true at /speaker/permission-probe, which
    # tests/test_speaker_api_contract_v52.py asserts is false. Both suites are
    # listed because this belongs caught here, not only by the P5.2 contract.
    ("M321", "an actuator is installed even though neither flag asked", CC,
     '    if not (execution_enabled() or observation_enabled()):\n        return None',
     '    if False:\n        return None',
     [ADAPTER, P52_CONTRACT]),
]


def run_suite(path: str, timeout: float = 900) -> tuple[bool, str]:
    env = dict(os.environ)
    env.setdefault("NOVA_IT_WATCHDOG_S", "900")
    # The suite's own labels contain em dashes. Without this the child
    # writes cp1252, we decode it as utf-8, every dash comes back as
    # U+FFFD, and that cannot be printed to a cp1252 console. The first
    # run died on M303 for exactly this reason.
    env["PYTHONIOENCODING"] = "utf-8"
    # The gates are what is under test, so the mutant run must not inherit a
    # shell that happens to have either one switched on.
    for k in ("NOVA_COMPUTER_CONTROL", "NOVA_COMPUTER_OBSERVE",
              "NOVA_COMPUTER_DENY_WINDOWS"):
        env.pop(k, None)
    r = subprocess.run([str(PY), path], cwd=str(REPO), capture_output=True,
                       text=True, encoding="utf-8", errors="replace",
                       timeout=timeout, env=env)
    out = (r.stdout or "") + (r.stderr or "")
    return ("RESULT: ALL PASS" in out), out


def _safe(text: str) -> str:
    """Printable on a cp1252 console, whatever the suite wrote."""
    return str(text).encode("ascii", "replace").decode("ascii")


def classify(out: str) -> str | None:
    if "SyntaxError" in out or "IndentationError" in out:
        return "INVALID (did not compile)"
    if "NameError" in out or "ImportError" in out:
        return "INVALID (import/name error)"
    return None


def main() -> int:
    wanted = {a for a in sys.argv[1:] if a.startswith("M")}
    killed, survived, invalid = [], [], []
    started = time.time()

    for mid, desc, rel, old, new, suites in MUTANTS:
        if wanted and mid not in wanted:
            continue
        p = REPO / rel
        src = io.open(p, encoding="utf-8").read()
        if old not in src:
            print(_safe(f"{mid}: ANCHOR MISSING in {rel} -> WITHDRAWN"), flush=True)
            invalid.append((mid, "anchor missing"))
            continue
        io.open(p, "w", encoding="utf-8", newline="\n").write(src.replace(old, new, 1))
        try:
            verdict = None
            for suite in suites:
                ok, out = run_suite(suite)
                bad = classify(out)
                if bad:
                    verdict = ("INVALID", bad)
                    break
                if not ok:
                    fails = [ln.strip() for ln in out.splitlines()
                             if ln.strip().startswith("FAIL")]
                    verdict = ("KILLED", f"{Path(suite).stem}: "
                                         f"{fails[0][:88] if fails else 'failed'}")
                    break
            if verdict is None:
                for suite in ALL_SUITES:
                    if suite in suites:
                        continue
                    ok, out = run_suite(suite)
                    if classify(out):
                        continue
                    if not ok:
                        fails = [ln.strip() for ln in out.splitlines()
                                 if ln.strip().startswith("FAIL")]
                        verdict = ("KILLED (wider)",
                                   f"{Path(suite).stem}: "
                                   f"{fails[0][:88] if fails else 'failed'}")
                        break
            if verdict is None:
                verdict = ("SURVIVED", "no suite noticed — investigate")
        finally:
            io.open(p, "w", encoding="utf-8", newline="\n").write(src)

        state, detail = verdict
        print(_safe(f"{mid}: {state} — {desc}\n      {detail}"), flush=True)
        if state.startswith("KILLED"):
            killed.append(mid)
        elif state == "SURVIVED":
            survived.append((mid, desc))
        else:
            invalid.append((mid, detail))

    print("\n" + "=" * 70)
    print(f"killed: {len(killed)}   survived: {len(survived)}   "
          f"invalid/withdrawn: {len(invalid)}   "
          f"({(time.time() - started) / 60:.1f} min)")
    for mid, desc in survived:
        print(_safe(f"  SURVIVED {mid}: {desc}"))
    for mid, why in invalid:
        print(_safe(f"  WITHDRAWN {mid}: {why}"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
