"""Turning a request into an acceptance contract, and proving it (Stage 14).

Two jobs the project builder needs and should not be doing inline:

  DECOMPOSE  the user's durable request into acceptance criteria, each quoting
             the span of the request it came from — done BEFORE any code
             exists, because a contract derived from finished code can only
             conclude that the code does what the code does.

  VALIDATE   each criterion SEPARATELY, so that what a check proves is the
             thing it actually tested. The old builder ran one launch check and
             one batch of generated tests and let the pair stand for the whole
             request; a program that starts proves that a program starts, and
             a calculator that cannot subtract started perfectly.

WHAT DOES NOT EARN CREDIT

A check that cannot be tied to a named criterion is diagnostic. It goes in the
log, where it is useful, and it earns nothing, because "some tests passed" is
not an answer to "does it subtract?". A criterion nobody could write a check
for stays unproven and says so.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from core.completion import FAILED, INCONCLUSIVE, PASSED
from core.completion_contract import is_span_of, uncovered_clauses
from core.logging_setup import get_logger

logger = get_logger(__name__)

#: How long one criterion check may run. A check that hangs is a check that
#: failed to decide, and saying so beats waiting for it.
CHECK_TIMEOUT_S = 25.0

_DECOMPOSE_PROMPT = """You are writing the ACCEPTANCE CRITERIA for a piece of work.

The person asked for exactly this:

{request}

Break that request into the smallest set of separately checkable statements
that, if ALL were demonstrated, would mean the request was satisfied.

Rules:
- Every criterion must quote the EXACT span of the request it comes from,
  copied character-for-character from the request above. Do not paraphrase the
  quote; do not invent words that are not in the request.
- Cover everything that was asked for. If the request asks for two things,
  produce at least two criteria.
- A criterion must be a statement about OBSERVABLE BEHAVIOUR ("adds two
  numbers and returns their sum"), not about code structure ("has an add
  function"). What matters is what the program does, not how it is written.
- Mark verify_kind "human" only for things a machine genuinely cannot judge
  (looks, feel, taste). Anything checkable by running code is "machine".

Reply ONLY with JSON:
{{"criteria": [{{"text": "...", "origin_quote": "...", "verify_kind": "machine"}}]}}
"""

_CHECK_PROMPT = """Write a Python script that decides ONE question about a program.

The question — the acceptance criterion — is:

    {criterion}

It comes from this request: {request}

The program's files are:

{listing}

Here is the entry point (`{entry}`):
```python
{code}
```

Write a script that imports from `{module}` and determines whether the
criterion above IS ACTUALLY TRUE of this program's behaviour.

Rules:
- Test THE CRITERION, nothing else. Do not test unrelated behaviour.
- Print EXACTLY ONE verdict line as the LAST thing the script prints:
      PASS: <what you observed>     the criterion holds
      FAIL: <what you observed>     it does not
      CANNOT_CHECK: <why>           running code cannot decide it
  and exit 0 after PASS or CANNOT_CHECK, non-zero after FAIL.
- The verdict is read from that line. A script that exits 0 without printing
  one decides nothing and is recorded as unproven, so always print it.
- Pure logic only: never open a window, sleep, play audio, or read input.
- Do not change the project's own files. Write only to a temporary directory
  you create, and never alongside the program's source.
- Standard library only.
- If this criterion CANNOT be decided by running code without a live GUI,
  network or human eye, reply with EXACTLY: CANNOT_CHECK

Reply with ONLY the script in one fenced code block, or the bare word
CANNOT_CHECK.
"""

#: The verdict line the script is asked to print, read from its OUTPUT rather
#: than inferred from an exit code. The exit code alone made a script that
#: printed "CANNOT_CHECK" and exited 0 a PASS (audit, H18).
_VERDICT_RE = re.compile(r"^\s*(PASS|FAIL|CANNOT[_ ]CHECK)\b\s*:?\s*(.*)$",
                         re.IGNORECASE | re.MULTILINE)


def _declared_verdict(output: str) -> tuple[str, str] | None:
    """The LAST verdict line a check printed, if it printed one."""
    matches = list(_VERDICT_RE.finditer(output or ""))
    if not matches:
        return None
    m = matches[-1]
    word = m.group(1).upper().replace(" ", "_")
    detail = (m.group(2) or "").strip()
    if word == "PASS":
        return PASSED, detail
    if word == "FAIL":
        return FAILED, detail
    return INCONCLUSIVE, detail


#: Project files a check must never be able to disturb. Everything else is
#: copied, because a check may legitimately read the project's data files.
_UNCOPIED = {".git", ".venv", "venv", "__pycache__", "node_modules", ".nova"}


def _copy_for_checking(path: Path, dest: Path) -> None:
    """Copy a project into `dest` so a check cannot touch the real thing.

    Checks used to run with `cwd` set to the project directory. A check that
    wrote `tasks.json` or `input.txt` — which is what a check for "tasks
    persist between runs" naturally does — changed the implementation digest,
    and every verdict already recorded went stale with "the implementation
    changed after this was checked". That is why nothing ever reached
    `complete` (audit, H18).
    """
    def _ignore(_dir: str, names: list[str]) -> set[str]:
        return {n for n in names if n in _UNCOPIED}

    shutil.copytree(path, dest, ignore=_ignore, dirs_exist_ok=True)


#: "CANNOT_CHECK" as a word, anywhere in the reply.
_CANNOT_CHECK_RE = re.compile(r"\bCANNOT[_ ]CHECK\b", re.IGNORECASE)


def _fenced_blocks(raw: str) -> list[str]:
    """Every fenced code block in a reply, in order."""
    return re.findall(r"```(?:[a-zA-Z0-9_+-]*)\n(.*?)```", raw or "", re.DOTALL)


def _fenced(raw: str) -> str:
    """The largest fenced block, or the whole reply if there is no fence."""
    blocks = _fenced_blocks(raw)
    if blocks:
        return max(blocks, key=len).strip()
    return (raw or "").strip()


async def derive_criteria(*, request: str, ask_json) -> list[dict[str, Any]]:
    """Decompose a request into criteria. Only well-formed ones survive.

    A criterion whose quote is not genuinely in the request is dropped here
    rather than passed on to be rejected by the service: the model produced
    something untraceable, and the honest response is to not have it, not to
    argue with it.
    """
    obj = await ask_json(_DECOMPOSE_PROMPT.format(request=request))
    out: list[dict[str, Any]] = []
    for spec in (obj or {}).get("criteria", []) or []:
        if not isinstance(spec, dict):
            continue
        text = str(spec.get("text") or "").strip()
        quote = str(spec.get("origin_quote") or "").strip()
        if not text or not quote:
            continue
        if not is_span_of(quote, request):
            logger.info("acceptance_quote_rejected", quote=quote[:60])
            continue
        kind = "human" if str(spec.get("verify_kind")).lower() == "human" else "machine"
        out.append({"text": text, "origin_quote": quote, "source": "plan",
                    "required": True, "verify_kind": kind})
    return out


def coverage_gaps(request: str, criteria: list[dict[str, Any]]) -> list[str]:
    """Parts of the request no criterion quotes."""
    return uncovered_clauses(request, [c["origin_quote"] for c in criteria])


def _run_script(path: Path, rel: str) -> tuple[bool, str, bool]:
    """(exit_zero, output, timed_out).

    `path` is a THROWAWAY COPY of the project, not the project — see
    `_copy_for_checking`. Python puts the SCRIPT's directory on `sys.path`,
    not the working directory, so the project root is put there explicitly or
    every check dies with ModuleNotFoundError and gets filed as "could not
    decide": a whole contract's worth of criteria permanently unproven for a
    reason that has nothing to do with the code.
    """
    import os as _os

    env = dict(_os.environ)
    env["PYTHONPATH"] = str(path) + _os.pathsep + env.get("PYTHONPATH", "")
    try:
        proc = subprocess.run(
            [sys.executable, rel], cwd=str(path), capture_output=True, text=True,
            timeout=CHECK_TIMEOUT_S, stdin=subprocess.DEVNULL, env=env,
            encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return False, "the check hung and was stopped — it decided nothing", True
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return proc.returncode == 0, out[-1200:], False


#: Exceptions that are the check DOING ITS JOB, wherever they are raised. An
#: assertion that fails is a finding about the program; so is a check that
#: exits non-zero on purpose.
_VERDICT_EXCEPTIONS = ("AssertionError", "SystemExit")


def _crashed_in_the_check(output: str, rel: str) -> bool:
    """Did the CHECK blow up, rather than find something about the program?

    A check's own bug is not evidence against the app, and four of the six
    checks written for `todo-cli` were its own bugs — a NameError, an
    uncommented English sentence, `-m main.py` from an empty directory, and a
    `chdir` away from the file it then looked for. All four were recorded as
    the app failing (audit, H18).

    Two questions, and both have to be answered: WHERE it raised, and WHAT.
    The deepest frame says where; if that is the generated check and no
    project frame sits below it, the line that broke was the check's own. But
    `assert main.add(2, 2) == 5` also raises on the check's own line, and that
    is the entire point of a check — so an AssertionError is a finding, not a
    crash, and only the other exceptions mean the check broke itself.
    """
    if "Traceback (most recent call last)" not in output:
        return False
    if any(f"{exc}:" in output or output.rstrip().endswith(exc)
           for exc in _VERDICT_EXCEPTIONS):
        return False
    names = {rel, rel.replace("/", "\\")}
    frames = re.findall(r'^\s*File "([^"]+)", line \d+', output, re.MULTILINE)
    if not frames:
        # A SyntaxError in the generated file reports the file without a
        # Traceback frame list.
        return any(n in output for n in names)
    deepest = frames[-1]
    return any(deepest.endswith(n) or n in deepest for n in names)


async def check_criterion(*, path: Path, entry: str, module: str, listing: str,
                          code: str, criterion: dict[str, Any], request: str,
                          ask_file, declare_scaffold) -> tuple[str, str]:
    """Decide ONE criterion. Returns (verdict, detail).

    The verdict is `passed`, `failed` or `inconclusive`. Inconclusive means the
    check could not be written or could not decide — which is information, and
    is not credit.
    """
    raw = await ask_file(_CHECK_PROMPT.format(
        criterion=criterion["text"], request=request, listing=listing,
        entry=entry, code=code[:6000], module=module))
    body = (raw or "").strip()
    # A REFUSAL TO CHECK, read from the whole reply rather than its first 60
    # characters. That window meant a model that explained itself before
    # refusing — "this is about what a person sees, so: CANNOT_CHECK" — had its
    # prose written out as Python and run; it died of SyntaxError, which landed
    # as inconclusive by luck, and one syntactically valid sentence away from
    # exit 0 and a recorded PASS (audit, H18).
    #
    # A reply with a code fence IS a check, even if it mentions the word, so
    # the fence is what distinguishes the two.
    if not body or (_CANNOT_CHECK_RE.search(body) and not _fenced_blocks(body)):
        return INCONCLUSIVE, ("no runnable check could be written for this "
                              "criterion; it remains unproven")
    script = _fenced(body)
    if len(script) < 20:
        return INCONCLUSIVE, "the generated check came back empty"

    # RUN IT ON A COPY. A check that writes a data file in the project
    # directory changed the implementation digest and staled every verdict
    # already recorded, including its own (audit, H18). The copy also means
    # nothing has to be written into the project at all, so the artifact fence
    # is not touched and there is no scaffolding to declare.
    rel = f"nova_check_{abs(hash(criterion['text'])) % 10_000_000}.py"
    tmp_root = Path(tempfile.mkdtemp(prefix="nova-accept-"))
    sandbox = tmp_root / "project"
    try:
        try:
            await asyncio.to_thread(_copy_for_checking, path, sandbox)
        except OSError as e:
            return INCONCLUSIVE, f"the project could not be copied to check it: {e}"
        (sandbox / rel).write_text(script, encoding="utf-8")

        exit_zero, output, timed_out = await asyncio.to_thread(
            _run_script, sandbox, rel)
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    if timed_out:
        return INCONCLUSIVE, output

    # The check's own crash is not a finding about the program.
    if _crashed_in_the_check(output, rel):
        return INCONCLUSIVE, f"the check itself crashed, so it decided nothing: {output[-250:]}"

    # THE VERDICT THE CHECK DECLARED, in preference to its exit code. "Exit 0
    # means it holds" turned every script that ran to completion without
    # asserting anything into a pass, including one that passed on any output
    # whose first line lacked "Error:".
    declared = _declared_verdict(output)
    if declared is not None:
        verdict, said = declared
        if verdict == PASSED and not exit_zero:
            # It printed PASS and then failed. Believe the exit code and say so.
            return FAILED, (f"the check printed PASS but exited non-zero, so it "
                            f"contradicts itself: {output[-250:]}")
        prefix = {PASSED: "check passed", FAILED: "check failed",
                  INCONCLUSIVE: "the check could not decide"}[verdict]
        return verdict, f"{prefix}: {said or output[-200:] or 'no detail'}"

    # A check that cannot find the MODULE has failed to run, and that is not a
    # refutation. But a check that cannot import a NAME from a module that
    # loaded fine has found exactly what it went looking for: the thing the
    # criterion is about does not exist. Collapsing the two would file every
    # missing feature as "could not decide", which is how "no tests were
    # applicable" became a pass in the first place.
    if "ModuleNotFoundError" in output:
        return INCONCLUSIVE, f"the check could not run: {output[:200]}"
    if "ImportError" in output and "cannot import name" in output:
        return FAILED, f"the program does not provide it: {output[:250]}"
    if "ImportError" in output:
        return INCONCLUSIVE, f"the check could not run: {output[:200]}"
    if "SyntaxError" in output and any(
            n in output for n in (rel, rel.replace("/", "\\"))):
        return INCONCLUSIVE, f"the generated check did not parse: {output[:160]}"
    if exit_zero:
        return PASSED, ("MUTANT: exit 0 is a pass; "
                              f"it proves nothing: {output[-200:] or 'no output'}")
    return FAILED, f"check failed: {output[:300] or 'non-zero exit'}"
