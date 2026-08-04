"""Integration-test harness: boots the REAL Nova backend (U10).

Every other suite in `tests/` exercises one module against fakes. That is why a
routing misroute and a CUDA crash both walked through a fully green 46-suite
run: nothing booted the thing Marcus actually talks to. This harness closes
that gap — it runs `backend.app`'s own startup handler, so a test drives the
same code path as a live turn.

WHAT IS REAL HERE (do not stub these — they are the point):
  * `backend.app._startup()` itself, and `_shutdown()`
  * MemoryUnifier on SQLite (the source of truth), in a temp dir
  * the full ToolRouter incl. plugin registration
  * RuntimeManager: pre-passes, grounding, agent tool loop, ModelRouter,
    CloudRuntime + its GPU-semaphore fallback, ProjectBuilder
  * every background worker `brain.start()` starts
  * the FastAPI routes, driven over ASGI via `nova.http`

WHAT IS SUBSTITUTED, and the honest reason for each:
  * **Model weights → `ScriptedLLM`.** Loading a 9B GGUF per suite would take
    minutes, need the GPU exclusively (Nova may be running), and make replies
    non-deterministic. The stub keeps the *interface* — `chat`, `chat_stream`,
    `generate`, semaphore discipline — so wiring, ordering and concurrency are
    all still under test. Reply *quality* is not, and no test here claims it.
  * **Chroma semantic index → off.** `ChromaMemoryBackend` loads a real
    embedding transformer onto CUDA on first query. SQLite is the declared
    source of truth (ARCHITECTURE.md §1.4) and the index is a rebuildable
    cache, so these tests assert against SQLite and skip the cache.
  * **Credentials → blanked.** Maps/weather/Discord/cloud keys are emptied
    before `backend.app` loads `.env`, so no test can reach the network with
    Marcus's real keys. Tools stay registered and fail honestly instead.

Nothing else is faked. If a test passes here and the behavior is still wrong
live, the gap is one of the three items above — say so out loud rather than
widening this list.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import sys
import tempfile
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterable

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# ── check() helper, matching the convention every other suite uses ───────────

class Checks:
    """Collects pass/fail lines and owns the process exit code."""

    def __init__(self) -> None:
        self.failed = False

    def __call__(self, cond: Any, label: str) -> bool:
        ok = bool(cond)
        if not ok:
            self.failed = True
        print(f"  {'OK  ' if ok else 'FAIL'} {label}")
        return ok

    def section(self, title: str) -> None:
        print(f"\n{title}")

    def finish(self) -> None:
        print("\nRESULT:", "FAILURES" if self.failed else "ALL PASS")
        sys.exit(1 if self.failed else 0)


# ── The scripted model ───────────────────────────────────────────────────────

@dataclass
class _Rule:
    match: Callable[[str], bool]
    reply: Callable[[str], str]
    label: str


class ScriptedLLM:
    """An `LLMRuntime`-shaped model whose replies are chosen by prompt content.

    Tests declare rules ("when the prompt looks like the planner's, reply with
    this JSON"); everything unmatched gets `default_reply`. It also records
    every prompt and — critically for the CUDA regression — the high-water mark
    of CONCURRENT calls, which is what a shared llama.cpp context cannot
    survive going above 1.
    """

    #: set on construction so a harness can hand the live instance to the test
    latest: "ScriptedLLM | None" = None

    def __init__(self, model_path: Path | None = None, context_tokens: int = 8192) -> None:
        self.model_path = model_path
        self.context_tokens = int(context_tokens)
        self.rules: list[_Rule] = []
        self.default_reply = "Sure — I'm here."
        self.prompts: list[str] = []
        self.call_delay = 0.0
        self.concurrent = 0
        self.max_concurrent = 0
        self._lock = threading.Lock()
        ScriptedLLM.latest = self

    # -- scripting API --------------------------------------------------------

    def when(self, needle: str | Callable[[str], bool], reply: str | Callable[[str], str],
             *, label: str = "") -> "ScriptedLLM":
        """Reply with `reply` when the outgoing prompt matches `needle`.

        `needle` is a case-insensitive substring or a predicate; `reply` is a
        string or a function of the prompt. Rules are tried in the order added.
        """
        if callable(needle):
            match = needle
        else:
            lowered = str(needle).lower()

            def match(text: str, _n: str = lowered) -> bool:
                return _n in text.lower()

        fn = reply if callable(reply) else (lambda _t, _r=reply: str(_r))
        self.rules.append(_Rule(match=match, reply=fn, label=label or str(needle)[:40]))
        return self

    def prompts_matching(self, needle: str) -> list[str]:
        n = needle.lower()
        return [p for p in self.prompts if n in p.lower()]

    def reset_calls(self) -> None:
        self.prompts.clear()
        self.max_concurrent = 0

    # -- internals ------------------------------------------------------------

    @staticmethod
    def _flatten(messages: list[dict[str, Any]]) -> str:
        parts: list[str] = []
        for m in messages or []:
            content = m.get("content")
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):  # multimodal shape
                parts.extend(str(c.get("text", "")) for c in content if isinstance(c, dict))
        return "\n".join(parts)

    def _resolve(self, prompt: str) -> str:
        for rule in self.rules:
            try:
                if rule.match(prompt):
                    return rule.reply(prompt)
            except Exception:  # a broken rule must not look like a model failure
                raise
        return self.default_reply

    @contextlib.asynccontextmanager
    async def _tracked(self):
        with self._lock:
            self.concurrent += 1
            self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            if self.call_delay:
                await asyncio.sleep(self.call_delay)
            yield
        finally:
            with self._lock:
                self.concurrent -= 1

    # -- LLMRuntime surface ---------------------------------------------------

    async def initialize(self) -> None:
        return None

    @property
    def model_loaded(self) -> bool:
        return True

    @property
    def gpu_status(self):
        from core.llm_runtime import GpuStatus

        return GpuStatus(required=False, active=False, status="scripted_stub",
                         details="Integration harness: no weights are loaded.")

    @property
    def vision_status(self) -> dict[str, Any]:
        return {"enabled": False, "reason": "Integration harness runs without a vision model.",
                "mmproj_path": None}

    @property
    def usage_stats(self) -> dict[str, Any]:
        return {"replies": len(self.prompts), "last_prompt_tokens": 0,
                "last_reply_tokens": 0, "avg_reply_tokens": 0.0}

    async def chat(self, messages: list[dict[str, Any]], max_tokens: int = 512,
                   temperature: float = 0.2, stop: list[str] | None = None,
                   thinking: bool = False) -> str:
        prompt = self._flatten(messages)
        async with self._tracked():
            self.prompts.append(prompt)
            return self._resolve(prompt)

    async def chat_stream(self, messages: list[dict[str, Any]], max_tokens: int = 512,
                          temperature: float = 0.2, stop: list[str] | None = None,
                          thinking: bool = False):
        prompt = self._flatten(messages)
        async with self._tracked():
            self.prompts.append(prompt)
            text = self._resolve(prompt)
        # Chunked like a real stream so consumers that assemble deltas are tested.
        for i in range(0, len(text), 24):
            yield text[i:i + 24]

    async def generate(self, prompt: str, max_tokens: int = 256, temperature: float = 0.1,
                       stop: list[str] | None = None, **_: Any) -> str:
        return await self.chat([{"role": "user", "content": prompt}], max_tokens=max_tokens,
                               temperature=temperature, stop=stop)

    async def vision_analyze(self, *_a: Any, **_kw: Any) -> str:
        raise RuntimeError("Integration harness runs without a vision model.")


#: The agent tool loop asks this on every turn; answering "respond" keeps a
#: plain chat turn one-pass, exactly like a well-behaved decision would.
AGENT_DECIDER_MARKER = "agent brain for Nova"
RESPOND_NOW = '{"action": "respond"}'


# ── A local stand-in for the cloud provider ──────────────────────────────────

class CloudStub:
    """A real HTTP server on localhost that speaks the OpenAI chat shape.

    The handoff requires these tests to run without a paid API key. Pointing
    `NOVA_CLOUD_BASE_URL` at this makes the cloud path genuinely exercised —
    including the failure modes that force a fallback onto the local GPU —
    with zero outbound traffic.
    """

    def __init__(self, *, status: int = 200, reply: str = "cloud says hi",
                 latency_s: float = 0.0) -> None:
        self.status = int(status)
        self.reply = reply
        self.latency_s = float(latency_s)
        self.requests: list[dict[str, Any]] = []
        #: high-water mark of requests in flight — proves the cloud handle's
        #: several permits really do overlap (the local GPU's one must not)
        self.max_concurrent = 0
        self._inflight = 0
        self._counter_lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        assert self._server is not None, "CloudStub is not started"
        return f"http://127.0.0.1:{self._server.server_port}/v1"

    def start(self) -> "CloudStub":
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                with stub._counter_lock:
                    stub._inflight += 1
                    stub.max_concurrent = max(stub.max_concurrent, stub._inflight)
                try:
                    self._respond()
                finally:
                    with stub._counter_lock:
                        stub._inflight -= 1

            def _respond(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    stub.requests.append(json.loads(raw.decode("utf-8")))
                except Exception:
                    stub.requests.append({"_unparsed": raw[:200].decode("utf-8", "replace")})
                if stub.latency_s:
                    import time as _time

                    _time.sleep(stub.latency_s)
                body = json.dumps({
                    "choices": [{"message": {"role": "assistant", "content": stub.reply}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                }).encode("utf-8")
                if stub.status >= 400:
                    body = json.dumps({"error": {"message": "stubbed failure"}}).encode("utf-8")
                self.send_response(stub.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_a):  # keep the test output clean
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None


# ── Booting ──────────────────────────────────────────────────────────────────

#: Anything that could reach the network with Marcus's real credentials. These
#: are set BEFORE backend.app imports .env (which uses override=False), so the
#: blanks win.
_CREDENTIALS = (
    "GOOGLE_MAPS_API_KEY", "OPENWEATHER_API_KEY",
    "DISCORD_BOT_TOKEN", "DISCORD_CHANNEL_ID", "NOVA_CLOUD_API_KEY",
)


def _base_env(root: Path) -> dict[str, str]:
    stub_model = root / "model" / "harness-stub.gguf"
    return {
        # A real .gguf path is required by the /chat model gate; ScriptedLLM
        # replaces the loader, so the file's contents are never read.
        "NOVA_MODEL_PATH": str(stub_model),
        "NOVA_REPO_ROOT": str(root),
        "NOVA_PROJECTS_DIR": str(root / "projects"),
        "NOVA_MEMORY_DIR": str(root / "memory_data"),
        "NOVA_VOICE_DIR": str(root / "voices"),
        "NOVA_MMPROJ_PATH": "",
        "NOVA_TTS_PREWARM": "0",
        "NOVA_LOG_LEVEL": "ERROR",
        # Background timers off by default: tests that want worker load create
        # it deterministically rather than waiting on a clock.
        "NOVA_AUTONOMY": "0",
        "NOVA_RESEARCH": "0",
        "NOVA_SELF_BENCHMARK": "0",
        "NOVA_AGENT_SOCIETY": "0",
        "NOVA_CLOUD_ENABLED": "0",
        "NOVA_CLOUD_BASE_URL": "",
        "NOVA_CLOUD_MODEL": "",
        "NOVA_DEV_MODE": "0",
        "NOVA_RESUME_BACKGROUND_TASKS": "0",
    }


@dataclass
class Nova:
    """Handles on a booted backend."""

    root: Path
    llm: ScriptedLLM
    app: Any
    state: Any
    http: Any  # httpx.AsyncClient bound to the ASGI app

    @property
    def runtime(self):
        return self.state.runtime

    @property
    def brain(self):
        return self.state.brain

    @property
    def memory(self):
        return self.state.memory

    @property
    def projects_dir(self) -> Path:
        return self.root / "projects"

    async def say(self, text: str, *, conversation_id=None, current_location=None):
        """One real turn through Brain → RuntimeManager (what /chat calls)."""
        return await self.brain.chat(text, conversation_id=conversation_id,
                                     current_location=current_location)


@contextlib.asynccontextmanager
async def boot(*, env: dict[str, str] | None = None,
               rules: Iterable[tuple[Any, Any]] = (),
               default_reply: str | None = None):
    """Boot the real backend into a throwaway root; tear it down after.

    `env` overrides harness defaults (see `_base_env`). `rules` is sugar for
    `ScriptedLLM.when` pairs. Everything is restored on exit.
    """
    root = Path(tempfile.mkdtemp(prefix="nova-it-"))
    for sub in ("model", "projects", "memory_data", "voices"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    (root / "model" / "harness-stub.gguf").write_bytes(b"")

    saved = dict(os.environ)
    os.environ.update(_base_env(root))
    for key in _CREDENTIALS:
        os.environ[key] = ""
    os.environ.update(env or {})

    # Imported only now, so the environment above wins over .env (override=False).
    import backend.app as app_module
    from memory.unifier import MemoryUnifier as _RealUnifier

    class _NoChromaUnifier(_RealUnifier):
        def __init__(self, memory_dir, *, enable_chroma: bool = True):  # noqa: ARG002
            super().__init__(memory_dir, enable_chroma=False)

    real_llm_cls = app_module.LLMRuntime
    real_unifier = app_module.MemoryUnifier
    app_module.LLMRuntime = ScriptedLLM
    app_module.MemoryUnifier = _NoChromaUnifier

    import httpx

    nova: Nova | None = None
    try:
        await app_module._startup()
        llm = ScriptedLLM.latest
        assert llm is not None, "ScriptedLLM was not constructed — startup wiring changed"
        llm.when(AGENT_DECIDER_MARKER, RESPOND_NOW, label="agent-loop: respond")
        for needle, reply in rules:
            llm.when(needle, reply)
        if default_reply is not None:
            llm.default_reply = default_reply

        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_module.app),
            base_url="http://nova.test",
            timeout=60.0,
        )
        nova = Nova(root=root, llm=llm, app=app_module.app, state=app_module.STATE, http=client)
        yield nova
    finally:
        if nova is not None:
            with contextlib.suppress(Exception):
                await nova.http.aclose()
        with contextlib.suppress(Exception):
            await app_module._shutdown()
        app_module.LLMRuntime = real_llm_cls
        app_module.MemoryUnifier = real_unifier
        os.environ.clear()
        os.environ.update(saved)
        # Windows holds the SQLite file briefly after close; losing the temp
        # dir is not a test failure.
        shutil.rmtree(root, ignore_errors=True)


def run(main: Callable[[], Any]) -> None:
    """Run an async test body (suites are plain scripts, not pytest).

    A faulthandler watchdog is armed for the whole process and deliberately
    never cancelled. These suites boot real servers, threads and background
    workers, and a suite that HANGS is worse than one that fails: it stalls
    run_tests.ps1 indefinitely with nothing to go on — including a hang during
    interpreter shutdown, after the test body has already passed. When the
    watchdog fires it dumps every thread's stack and exits non-zero, which
    turns a silent stall into a diagnosable failure.
    """
    import faulthandler

    try:
        limit = float(os.getenv("NOVA_IT_WATCHDOG_S", "").strip() or 180.0)
    except ValueError:
        limit = 180.0
    if limit > 0:
        faulthandler.dump_traceback_later(limit, exit=True)
    asyncio.run(main())
