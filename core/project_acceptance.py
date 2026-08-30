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
import subprocess
import sys
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
- Exit code 0 means the criterion holds; non-zero means it does not.
- Print one short line saying what you observed.
- Pure logic only: never open a window, sleep, play audio, or read input.
- Standard library only.
- If this criterion CANNOT be decided by running code without a live GUI,
  network or human eye, reply with EXACTLY: CANNOT_CHECK

Reply with ONLY the script in one fenced code block, or the bare word
CANNOT_CHECK.
"""


def _fenced(raw: str) -> str:
    """The largest fenced block, or the whole reply if there is no fence."""
    blocks = re.findall(r"```(?:[a-zA-Z0-9_+-]*)\n(.*?)```", raw or "", re.DOTALL)
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


def _run_script(path: Path, rel: str) -> tuple[bool, str]:
    """(passed, output). Passing is exit code 0.

    The check lives under `.nova/checks/` so that writing it does not disturb
    the artifact fence, and Python puts the SCRIPT's directory on `sys.path`,
    not the working directory — so the project itself has to be put there
    explicitly or every check dies with ModuleNotFoundError and gets filed as
    "could not decide". A whole contract's worth of criteria would sit
    permanently unproven for a reason that has nothing to do with the code.
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
        return False, "the check hung and was stopped — it decided nothing"
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return proc.returncode == 0, out[-600:]


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
    if not body or "CANNOT_CHECK" in body.upper()[:60]:
        return INCONCLUSIVE, ("no runnable check could be written for this "
                              "criterion; it remains unproven")
    script = _fenced(body)
    if len(script) < 20:
        return INCONCLUSIVE, "the generated check came back empty"

    # Nova's own file: declared as scaffolding so writing it does not move the
    # artifact fence the verdict is about to be stamped with.
    rel = f".nova/checks/check_{abs(hash(criterion['text'])) % 10_000_000}.py"
    target = path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(script, encoding="utf-8")
    declare_scaffold([rel])

    passed, output = await asyncio.to_thread(_run_script, path, rel)
    if passed:
        return PASSED, f"check passed: {output[:200] or 'exit 0'}"
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
    if "SyntaxError" in output and rel.replace("/", "\\") in output:
        return INCONCLUSIVE, f"the generated check did not parse: {output[:160]}"
    return FAILED, f"check failed: {output[:300] or 'non-zero exit'}"
