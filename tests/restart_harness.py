"""Run Nova in a SEPARATE process, against a durable store that outlives it.

WHY THIS EXISTS

`runtime.stop()` then `runtime.start()` on the same object proves very little
about a restart. The Python globals are still there, the workers are the same
objects, the caches are warm, the conversation context is intact, and anything
reconstructed "from durable state" may in fact have been reconstructed from
itself.

Stage 13C's question is what survives when all of that is genuinely gone. So a
scenario here runs in a fresh interpreter, is handed nothing but a directory
path, and communicates only through JSON on stdout. The parent process holds no
handle on anything the child made except the files it left behind.

TWO WAYS TO END A PROCESS, and they are different facts:

    run_step(...)                    a clean shutdown - the app's own stop path
                                     runs and everything drains.
    run_step(..., expect_crash=True) the scenario calls CRASH() at an exact
                                     point. `os._exit` skips every finally
                                     block, atexit hook and buffer flush, so
                                     nothing drains and whatever reached the
                                     database before that line is all that
                                     survives.
    crash_step(..., at=3.0)          killed by the parent after N seconds. Only
                                     for boundaries a scenario cannot name;
                                     prefer CRASH(), which is placed rather
                                     than timed.

USAGE

    step = run_step(root, '''
        goal = await mem.create_goal(project_name="p", title="t",
                                     objective="o", success_criteria="c")
        emit({"goal": str(goal)})
    ''')
    assert step["goal"]

The scenario body is executed with `mem` (a live MemoryUnifier on the shared
store) and `emit(dict)` already in scope. With `full=True` it also gets `nova`
(the booted backend, including `nova.http` for real /chat turns).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY = REPO / "venv" / "Scripts" / "python.exe"

#: Everything the child needs to find the SAME durable state as its siblings.
#: Deliberately explicit rather than inherited: a child that silently picked up
#: the parent's environment could pass while reading a different database.
def child_env(root: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.update({
        "PYTHONIOENCODING": "utf-8",
        "NOVA_LOG_LEVEL": "ERROR",
        "NOVA_REPO_ROOT": str(root),
        "NOVA_PROJECTS_DIR": str(root / "projects"),
        "NOVA_MEMORY_DIR": str(root / "memory_data"),
        "NOVA_VOICE_DIR": str(root / "voices"),
        "NOVA_MODEL_PATH": str(root / "model" / "harness-stub.gguf"),
        "NOVA_AUTONOMY": "0",
        "NOVA_RESEARCH": "0",
        "NOVA_TTS_PREWARM": "0",
        "NOVA_DEV_MODE": "1",
        "NOVA_IT_WATCHDOG_S": "300",
    })
    return env


def prepare_root(root: Path) -> Path:
    root = Path(root)
    for sub in ("model", "projects", "memory_data", "voices"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    stub = root / "model" / "harness-stub.gguf"
    if not stub.exists():
        stub.write_bytes(b"")
    return root


_PREAMBLE_MEM = '''
import asyncio, json, os, sys
from pathlib import Path
REPO = Path(r"{repo}")
sys.path.insert(0, str(REPO)); sys.path.insert(0, str(REPO / "tests"))
_OUT = []
def emit(d):
    _OUT.append(dict(d))
def CRASH():
    """Die exactly here, the way a killed process dies.

    `os._exit` skips every finally block, atexit hook, buffer flush and
    shutdown path. Nothing drains. Whatever reached the database before this
    call is all that survives - which is the whole point of a crash window, and
    is far more precise than killing the child after N seconds and hoping it
    was in the right place.
    """
    sys.stdout.flush()
    os._exit(9)
from memory.unifier import MemoryUnifier

async def _main():
    mem = MemoryUnifier(Path(r"{root}") / "memory_data", enable_chroma=False)
    await mem.initialize()
{body}
asyncio.run(_main())
print("@@JSON@@" + json.dumps(_OUT))
'''

_PREAMBLE_FULL = '''
import asyncio, json, os, sys
from pathlib import Path
REPO = Path(r"{repo}")
sys.path.insert(0, str(REPO)); sys.path.insert(0, str(REPO / "tests"))
_OUT = []
def emit(d):
    _OUT.append(dict(d))
def CRASH():
    """Die exactly here. See the note in the memory-only preamble."""
    sys.stdout.flush()
    os._exit(9)
from harness import boot

async def _main():
    async with boot(default_reply="Sure.", root=Path(r"{root}")) as nova:
        mem = nova.memory
{body}
asyncio.run(_main())
print("@@JSON@@" + json.dumps(_OUT))
'''


class StepFailed(RuntimeError):
    pass


def run_step(root: Path, body: str, *, full: bool = False,
             kill_after: float | None = None,
             expect_crash: bool = False,
             timeout: float = 240.0) -> list[dict]:
    """Run one scenario in a fresh interpreter. Returns what it emitted.

    `kill_after` terminates the child that many seconds in, WITHOUT letting it
    shut down: no drain, no shutdown hook, no last commit. Whatever reached the
    database before that instant is all that survives, which is the point.
    """
    root = prepare_root(root)
    template = _PREAMBLE_FULL if full else _PREAMBLE_MEM
    src = template.format(repo=str(REPO), root=str(root),
                          body=textwrap.indent(textwrap.dedent(body),
                                               "        " if full else "    "))
    proc = subprocess.Popen(
        [str(PY), "-c", src], cwd=str(REPO), env=child_env(root),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        encoding="utf-8", errors="replace")

    if kill_after is not None:
        deadline = time.time() + kill_after
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            time.sleep(0.05)
        if proc.poll() is None:
            proc.kill()          # no shutdown path runs. This is a crash.
        try:
            proc.communicate(timeout=30)
        except Exception:
            pass
        return []

    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise StepFailed(f"child timed out after {timeout}s")
    if expect_crash:
        # A placed CRASH() is a non-zero exit BY DESIGN. Exiting cleanly means
        # the boundary was never reached, which would make the whole window
        # vacuous - so that is the failure, not the crash.
        if proc.returncode == 0:
            raise StepFailed("scenario was expected to CRASH and exited cleanly")
        return []
    if proc.returncode != 0:
        raise StepFailed(f"child exited {proc.returncode}\n{err[-2500:]}")
    for line in (out or "").splitlines():
        if line.startswith("@@JSON@@"):
            return json.loads(line[len("@@JSON@@"):])
    raise StepFailed(f"child emitted no result\nSTDOUT:\n{out[-1500:]}\n"
                     f"STDERR:\n{err[-1500:]}")


def crash_step(root: Path, body: str, *, at: float, full: bool = False) -> None:
    """Start a scenario and kill it mid-flight. Emits nothing by definition."""
    run_step(root, body, full=full, kill_after=at)


def scenario(*parts: str) -> str:
    """Join scenario fragments that were written at different indentations.

    Each piece is dedented on its own before joining. Concatenating a
    flush-left constant with a block indented for readability inside a test
    leaves `textwrap.dedent` no common prefix to remove, and the child dies on
    an IndentationError that says nothing about the property under test.
    """
    cleaned = [textwrap.dedent(part).strip("\n") for part in parts]
    return "\n".join(cleaned) + "\n"


def one(rows: list[dict], key: str, default=None):
    """The first emitted value for `key`. Emissions are keyed, never indexed."""
    for r in rows:
        if key in r:
            return r[key]
    return default
