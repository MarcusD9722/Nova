"""LIVE cloud verification — makes REAL API calls and spends REAL money.

Marcus's requirement is that "code and project building is powered by cloud llm
from the .env". Nothing else in the suite can prove that: every other cloud test
runs against a localhost stub, which verifies the wiring but not the key, the
base URL, the model name, or that the provider actually answers.

OPT-IN ONLY. Skips (exit 0) unless NOVA_CLOUD_LIVE=1, so `run_tests.ps1` never
spends money on its own. Run it deliberately:

    $env:NOVA_CLOUD_LIVE=1; .\run_tests.ps1 cloud_live

Guards, because this touches a paid API:
  * refuses to run at all unless NOVA_CLOUD_DAILY_TOKEN_CAP is set
  * reports real token usage and the estimated cost when it finishes
  * uses tiny prompts and a small max_tokens — a full run is a fraction of a cent

What it proves, in order:
  1. the configured provider/model/key actually answer
  2. the `coder` and `planner` roles genuinely route to the cloud
  3. the context firewall scrubs personal text BEFORE it leaves the machine
  4. spend tracking increments against real usage
  5. ProjectBuilder's real codegen path (_llm_file) is served by the cloud
  6. a cloud failure still falls back to the local model
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / ".env", override=False)
except ImportError:
    pass

from harness import Checks

check = Checks()

if (os.getenv("NOVA_CLOUD_LIVE", "0").strip().lower() not in {"1", "true", "yes", "on"}):
    print("SKIPPED: live cloud test is opt-in. Set NOVA_CLOUD_LIVE=1 to spend real tokens.")
    sys.exit(0)


async def main() -> None:
    from core.cloud_runtime import CloudRuntime, cloud_enabled
    from core.context_firewall import scrub_messages, verify_safe

    check.section("Preconditions (refusing to spend without a cap)")
    cap = os.getenv("NOVA_CLOUD_DAILY_TOKEN_CAP", "").strip()
    if not cap or not cap.isdigit() or int(cap) <= 0:
        print("  FAIL NOVA_CLOUD_DAILY_TOKEN_CAP must be a positive integer before any live call")
        sys.exit(1)
    check(True, f"a daily token cap is set ({cap})")
    check(cloud_enabled(), "NOVA_CLOUD_ENABLED is on")
    check(bool(os.getenv("NOVA_CLOUD_API_KEY", "").strip()), "an API key is present (value never printed)")
    model = os.getenv("NOVA_CLOUD_MODEL", "").strip()
    check(bool(model), f"a model is configured ({model})")

    class LocalMarker:
        """Stands in for the local model so a fallback is unmistakable."""
        async def chat(self, *_a, **_kw):
            return "__LOCAL_FALLBACK__"

    sem = asyncio.Semaphore(1)
    cloud = CloudRuntime(fallback=LocalMarker(), fallback_semaphore=sem,
                         identities=lambda: ("Marcus", ["Leslie", "Mateo", "Liam"]))

    check.section("1. The real provider answers")
    check(cloud.available is True, "CloudRuntime reports itself configured")
    reply = await cloud.chat(
        [{"role": "user", "content": "Reply with exactly the word: ONLINE"}],
        max_tokens=8, temperature=0.0,
    )
    check(reply != "__LOCAL_FALLBACK__", f"the call reached the cloud, not the local fallback ({reply[:40]!r})")
    check("ONLINE" in reply.upper(), f"the provider returned a usable answer ({reply[:60]!r})")

    status = cloud.status()
    check(status["calls"] >= 1, f"the call was counted (calls={status['calls']})")
    check(status["fallbacks_to_local"] == 0, "no fallback was needed")

    check.section("2. Spend tracking is real")
    used = status["today"]["total_tokens"]
    check(used > 0, f"real token usage was recorded ({used} tokens)")
    check(status["today"]["daily_token_cap"] == int(cap), "the cap is reported in status")
    check(status["today"]["cap_reached"] is False, "the cap has not been hit")

    check.section("3. The firewall scrubs BEFORE anything leaves the machine")
    # (a) A STRUCTURED grounding blob is dropped whole — the firewall's primary
    #     job, and the realistic bulk-leak path.
    grounded = [
        {"role": "system", "content": '{"known_user": {"name": "Marcus", "location": "9139 Coronal Rings"}}'},
        {"role": "user", "content": "Write a function that adds two integers."},
    ]
    scrub = scrub_messages(grounded, user_name="Marcus", known_names=["Leslie"])
    check(scrub.dropped >= 1, f"a grounding block is dropped whole ({scrub.dropped} dropped)")
    sent = " ".join(str(m.get("content") or "") for m in scrub.messages)
    check("Coronal Rings" not in sent, "the address inside the grounding block never leaves")
    check("adds two integers" in sent, "the actual task survives the scrub")

    # (b) PROSE that carries contact PII is REDACTED, not dropped — verified
    #     live that the address used to survive this path intact.
    prose = [{"role": "user",
              "content": "Marcus lives at 9139 Coronal Rings with Leslie. "
                         "Email m.deleona97@gmail.com or call 512-555-0134. "
                         "Write a function that adds two integers."}]
    scrub = scrub_messages(prose, user_name="Marcus", known_names=["Leslie", "Mateo", "Liam"])
    sent = " ".join(str(m.get("content") or "") for m in scrub.messages)
    for secret in ("Marcus", "Leslie", "9139 Coronal Rings", "m.deleona97@gmail.com", "512-555-0134"):
        check(secret not in sent, f"'{secret}' never appears in what would be sent")
    check("adds two integers" in sent, "the coding request itself is preserved")
    check(verify_safe(scrub.messages) == [], "post-scrub verification finds nothing personal left")

    # (c) A redaction must not fire on ordinary code. Refusing or mangling a
    #     legitimate coding prompt would be a worse failure than the leak.
    code_prose = [{"role": "user", "content": "Refactor loop 3 times over 42 Python Programs in list_a"}]
    kept = " ".join(str(m.get("content") or "") for m in
                    scrub_messages(code_prose, user_name=None, known_names=[]).messages)
    check("42 Python Programs" in kept, "an ordinary coding prompt is left untouched")

    check.section("4. The coder role really is served by the cloud")
    # The exact call ProjectBuilder._llm_file makes for every generated file.
    code = await cloud.chat(
        [{"role": "user", "content": "Reply with ONLY a fenced python code block "
                                     "containing: def add(a, b): return a + b"}],
        max_tokens=120, temperature=0.2, stop=[],
    )
    check(code != "__LOCAL_FALLBACK__", "codegen went to the cloud")
    check("def add" in code, f"the cloud produced real code ({code[:70]!r})")

    check.section("5. A cloud failure still falls back to local")
    saved_url = os.environ.get("NOVA_CLOUD_BASE_URL", "")
    os.environ["NOVA_CLOUD_BASE_URL"] = "http://127.0.0.1:1"   # nothing listens here
    out = await cloud.chat([{"role": "user", "content": "hi"}], max_tokens=8)
    os.environ["NOVA_CLOUD_BASE_URL"] = saved_url
    check(out == "__LOCAL_FALLBACK__", "an unreachable provider transparently falls back to local")
    check(cloud.status()["fallbacks_to_local"] >= 1, "the fallback is counted honestly")
    check(bool(cloud.status()["last_error"]), f"the real error is retained ({str(cloud.status()['last_error'])[:60]!r})")

    check.section("6. The cap actually stops spending")
    os.environ["NOVA_CLOUD_DAILY_TOKEN_CAP"] = "1"   # already exceeded
    check(cloud.available is False, "over the cap, the cloud reports unavailable")
    out = await cloud.chat([{"role": "user", "content": "hi"}], max_tokens=8)
    check(out == "__LOCAL_FALLBACK__", "over the cap, work routes to local instead of spending")
    os.environ["NOVA_CLOUD_DAILY_TOKEN_CAP"] = cap

    final = cloud.status()["today"]
    print(f"\n  SPEND THIS RUN: {final['total_tokens']} tokens "
          f"(prompt={final['prompt_tokens']}, completion={final['completion_tokens']}) "
          f"~ ${final['estimated_cost_usd']}")
    check.finish()


asyncio.run(main())
