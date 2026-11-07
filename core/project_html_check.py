"""Execute an HTML/JS project headlessly, or say plainly that nothing ran.

AUDIT 2026-11-02, H19. HTML projects were never executed: no run check, no
logic tests, no check scripts. `_verify_and_fix_runtime` and
`_generate_and_run_tests` both return None the moment there is no `.py` file,
and they returned it silently — so `serpent`'s PROJECT.md said "Complete
playable snake game... Use arrow keys or WASD" about a game whose reversal
guard was inverted (the snake could never turn), with WASD not implemented at
all. Nothing had looked.

What this does, in order of how much it proves:

1. **Parse.** Every inline `<script>` goes through `node --check`. A syntax
   error in generated JS is common and was previously invisible.
2. **Load.** The scripts run against a small DOM stub. A crash on load — a
   missing element, a typo'd global, a bad canvas call — is a real defect and
   is reported as one.
3. **Drive.** Whatever the page registered as a timer or animation frame is
   ticked, with keyboard events dispatched, and the stub records what the page
   drew. A page that draws nothing after sixty ticks, or throws while being
   driven, is reported.

What it does NOT do: judge whether the game is any good, or whether a rule is
right. The stub has no layout, no real canvas and no pixels. Every result says
what it actually checked, because the whole finding was a confident claim about
something nobody had run.

Node is required. Without it the result says the project was NOT executed,
which is the honest answer and the one the audit asked for.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from core.logging_setup import get_logger

logger = get_logger(__name__)

#: How long node may spend on one project.
SMOKE_TIMEOUT_S = float(os.getenv("NOVA_HTML_SMOKE_TIMEOUT_S", "30").strip() or "30")

#: How many animation/timer ticks to drive.
SMOKE_TICKS = int(os.getenv("NOVA_HTML_SMOKE_TICKS", "60").strip() or "60")

_SCRIPT_RE = re.compile(
    r"<script\b(?P<attrs>[^>]*)>(?P<body>.*?)</script>", re.IGNORECASE | re.DOTALL)
_SRC_RE = re.compile(r"""\bsrc\s*=\s*["']?([^"'\s>]+)""", re.IGNORECASE)
_TYPE_RE = re.compile(r"""\btype\s*=\s*["']?([^"'\s>]+)""", re.IGNORECASE)


@dataclass
class HtmlSmokeResult:
    """What was actually established. `ran` false means nothing executed."""

    ran: bool = False
    syntax_ok: bool = False
    loaded: bool = False
    ticks: int = 0
    drew: int = 0
    key_handlers: int = 0
    errors: list[str] = field(default_factory=list)
    skipped_reason: str = ""
    #: How the page's JavaScript was assembled, so the note can say what was
    #: and was not covered.
    inline_scripts: int = 0
    local_scripts: int = 0
    remote_scripts: int = 0

    @property
    def passed(self) -> bool:
        return self.ran and self.syntax_ok and self.loaded and not self.errors

    def note(self) -> str:
        """One honest line for PROJECT.md and the build log."""
        if not self.ran:
            return (f"Browser smoke test NOT run: {self.skipped_reason}. "
                    "Nothing has executed this project.")
        if not self.syntax_ok:
            return ("Browser smoke test FAILED to parse the page's JavaScript: "
                    + "; ".join(self.errors[:2]))
        if not self.loaded:
            return ("Browser smoke test: the page's JavaScript threw while "
                    "loading: " + "; ".join(self.errors[:2]))
        if self.errors:
            return (f"Browser smoke test: loaded, then threw while being driven "
                    f"({self.ticks} ticks): " + "; ".join(self.errors[:2]))
        drawn = f"{self.drew} draw call(s)" if self.drew else "NOTHING DRAWN"
        extra = "" if self.drew else (
            " — it loaded without error but never drew, so it may not be doing "
            "anything")
        keys = (f"{self.key_handlers} key handler(s)" if self.key_handlers
                else "NO keyboard handler, so nothing responds to a key press")
        uncovered = (f" {self.remote_scripts} script(s) loaded from the network "
                     "were not fetched, so they were not checked."
                     if self.remote_scripts else "")
        return (f"Browser smoke test passed: parsed, loaded, driven for "
                f"{self.ticks} ticks with {keys}, {drawn}{extra}. This checks "
                "that it RUNS, not that it plays correctly — no human has "
                f"played it.{uncovered}")


def node_executable() -> str | None:
    return shutil.which("node")


def _local_script(base: Path, src: str) -> Path | None:
    """The project file a `<script src=...>` points at, or None.

    None for anything off-machine (http, protocol-relative, data:) and for
    anything that escapes the project directory. Those are not part of what
    was built, and fetching them would make the result depend on a network.
    """
    s = (src or "").strip()
    if not s or re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", s) or s.startswith("//"):
        return None
    rel = s.split("?", 1)[0].split("#", 1)[0].lstrip("/")
    if not rel:
        return None
    try:
        target = (base / rel).resolve()
        root = base.resolve()
    except OSError:  # quiet-ok: a path that cannot be resolved is not part of the project; the note counts it as not checked
        return None
    # By path components, not string prefix: `../calculator/app.js` from a
    # project called `calc` shares the prefix and is not part of it.
    if not target.is_relative_to(root):
        return None
    return target if target.is_file() else None


def inline_scripts(html: str) -> tuple[list[str], list[str]]:
    """(inline script bodies, external src attributes) in document order."""
    bodies: list[str] = []
    external: list[str] = []
    for m in _SCRIPT_RE.finditer(html or ""):
        attrs = m.group("attrs") or ""
        src = _SRC_RE.search(attrs)
        if src:
            external.append(src.group(1))
            continue
        kind = (_TYPE_RE.search(attrs).group(1).lower()
                if _TYPE_RE.search(attrs) else "")
        if kind and kind not in {"text/javascript", "application/javascript",
                                 "module"}:
            continue  # a template or JSON block, not code
        body = (m.group("body") or "").strip()
        if body:
            bodies.append(body)
    return bodies, external


#: The DOM stub. Deliberately small and honest: it answers the calls a simple
#: canvas game makes, records what was drawn, and does not pretend to lay
#: anything out. Written as a file rather than a string argument so a quote in
#: the project's own code cannot break the command line.
_HARNESS_JS = r"""
'use strict';
const fs = require('fs');
const path = require('path');

const input = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const result = {loaded: false, ticks: 0, drew: 0, keyHandlers: 0, errors: []};

function note(e) {
  const msg = (e && e.stack) ? String(e.stack).split('\n').slice(0, 3).join(' | ')
                             : String(e);
  if (result.errors.length < 8) result.errors.push(msg.slice(0, 300));
}

// ── the stub ────────────────────────────────────────────────────────────────
let drew = 0;
const ctx = new Proxy({}, {
  get(_t, prop) {
    if (prop === 'canvas') return canvasEl;
    if (prop === 'measureText') return () => ({width: 10});
    if (prop === 'createLinearGradient' || prop === 'createRadialGradient') {
      return () => ({addColorStop() {}});
    }
    if (prop === 'getImageData') {
      return (x, y, w, h) => ({data: new Uint8ClampedArray(Math.max(1, w * h * 4))});
    }
    // Anything else is a drawing or state call.
    return (...a) => { drew++; return undefined; };
  },
  set() { return true; },
});

function makeElement(tag) {
  const el = {
    tagName: String(tag || 'div').toUpperCase(),
    style: {}, dataset: {}, children: [], childNodes: [],
    classList: {add() {}, remove() {}, toggle() {}, contains: () => false},
    width: 800, height: 600, value: '', textContent: '', innerHTML: '',
    clientWidth: 800, clientHeight: 600, offsetWidth: 800, offsetHeight: 600,
    scrollWidth: 800, scrollHeight: 600,
    getContext: () => ctx,
    getBoundingClientRect: () => ({left: 0, top: 0, right: 800, bottom: 600,
                                   width: 800, height: 600, x: 0, y: 0}),
    appendChild(c) { this.children.push(c); this.childNodes.push(c); return c; },
    removeChild(c) { return c; },
    insertBefore(c) { this.children.push(c); return c; },
    setAttribute(k, v) { this[k] = v; },
    getAttribute(k) { return this[k] === undefined ? null : this[k]; },
    removeAttribute() {},
    hasAttribute: () => false,
    focus() {}, blur() {}, click() { fire(el, 'click', {}); },
    addEventListener: (t, fn) => addListener(el, t, fn),
    removeEventListener() {},
    querySelector: (s) => makeElement('div'),
    querySelectorAll: () => [],
    cloneNode: () => makeElement(tag),
    remove() {},
    play: () => Promise.resolve(), pause() {}, load() {},
  };
  return el;
}

const listeners = new Map();
function addListener(target, type, fn) {
  if (typeof fn !== 'function') return;
  const key = target;
  if (!listeners.has(key)) listeners.set(key, new Map());
  const byType = listeners.get(key);
  if (!byType.has(type)) byType.set(type, []);
  byType.get(type).push(fn);
  if (type === 'keydown' || type === 'keyup' || type === 'keypress') {
    result.keyHandlers++;
  }
}
function fire(target, type, event) {
  const byType = listeners.get(target);
  const fns = byType && byType.get(type);
  if (!fns) return;
  for (const fn of fns.slice()) {
    try { fn(event); } catch (e) { note(e); }
  }
}

const canvasEl = makeElement('canvas');
const bodyEl = makeElement('body');

const timers = [];
const frames = [];

const documentStub = {
  body: bodyEl,
  documentElement: makeElement('html'),
  head: makeElement('head'),
  readyState: 'complete',
  title: '',
  getElementById: (id) => (String(id).toLowerCase().includes('canvas')
                           ? canvasEl : makeElement('div')),
  querySelector: (s) => (String(s).toLowerCase().includes('canvas')
                         ? canvasEl : makeElement('div')),
  querySelectorAll: () => [],
  getElementsByTagName: (t) => (String(t).toLowerCase() === 'canvas'
                                ? [canvasEl] : []),
  getElementsByClassName: () => [],
  createElement: (t) => (String(t).toLowerCase() === 'canvas'
                         ? makeElement('canvas') : makeElement(t)),
  createTextNode: (t) => ({textContent: t}),
  createDocumentFragment: () => makeElement('fragment'),
  addEventListener: (t, fn) => addListener(documentStub, t, fn),
  removeEventListener() {},
  appendChild: (c) => c,
  cookie: '',
  hidden: false,
  fonts: {ready: Promise.resolve(), load: () => Promise.resolve()},
};

const storage = (() => {
  const m = new Map();
  return {
    getItem: (k) => (m.has(String(k)) ? m.get(String(k)) : null),
    setItem: (k, v) => { m.set(String(k), String(v)); },
    removeItem: (k) => { m.delete(String(k)); },
    clear: () => m.clear(),
    key: (i) => Array.from(m.keys())[i] ?? null,
    get length() { return m.size; },
  };
})();

const windowStub = {
  document: documentStub,
  innerWidth: 800, innerHeight: 600, devicePixelRatio: 1,
  localStorage: storage, sessionStorage: storage,
  location: {href: 'http://localhost/', search: '', hash: '', reload() {}},
  navigator: {userAgent: 'nova-smoke', language: 'en-US', maxTouchPoints: 0},
  addEventListener: (t, fn) => addListener(windowStub, t, fn),
  removeEventListener() {},
  requestAnimationFrame: (fn) => { frames.push(fn); return frames.length; },
  cancelAnimationFrame: () => {},
  setTimeout: (fn, ms) => { timers.push({fn, ms: ms || 0, repeat: false}); return timers.length; },
  setInterval: (fn, ms) => { timers.push({fn, ms: ms || 0, repeat: true}); return timers.length; },
  clearTimeout: () => {}, clearInterval: () => {},
  alert() {}, confirm: () => true, prompt: () => null,
  getComputedStyle: () => ({getPropertyValue: () => ''}),
  matchMedia: () => ({matches: false, addEventListener() {}, addListener() {}}),
  performance: {now: () => Date.now()},
  AudioContext: function () {
    return {createOscillator: () => ({connect() {}, start() {}, stop() {},
                                      frequency: {value: 0}}),
            createGain: () => ({connect() {}, gain: {value: 0}}),
            destination: {}, close: () => Promise.resolve()};
  },
  Audio: function () { return makeElement('audio'); },
  Image: function () { return makeElement('img'); },
  fetch: () => Promise.reject(new Error('no network in the smoke test')),
};
windowStub.window = windowStub;
windowStub.globalThis = windowStub;
windowStub.self = windowStub;

// Publish the stub as globals, the way a browser does.
//
// Plain assignment is not enough: node defines `navigator` and `performance`
// as getter-only accessors, so `g.navigator = ...` throws TypeError and the
// whole harness died — which was then reported as the PAGE throwing. A
// harness's own failure must never be filed against the project.
const g = globalThis;
function defineGlobal(name, value) {
  try {
    Object.defineProperty(g, name, {value, writable: true, configurable: true,
                                    enumerable: false});
  } catch (e) {
    try { g[name] = value; } catch (e2) { /* leave node's own in place */ }
  }
}

for (const [name, value] of [
  ['window', windowStub],
  ['document', documentStub],
  ['localStorage', storage],
  ['sessionStorage', storage],
  ['navigator', windowStub.navigator],
  ['location', windowStub.location],
  ['alert', windowStub.alert],
  ['confirm', windowStub.confirm],
  ['prompt', windowStub.prompt],
  ['requestAnimationFrame', windowStub.requestAnimationFrame],
  ['cancelAnimationFrame', windowStub.cancelAnimationFrame],
  ['getComputedStyle', windowStub.getComputedStyle],
  ['matchMedia', windowStub.matchMedia],
  ['AudioContext', windowStub.AudioContext],
  ['webkitAudioContext', windowStub.AudioContext],
  ['Audio', windowStub.Audio],
  ['Image', windowStub.Image],
  ['fetch', windowStub.fetch],
  ['setTimeout', windowStub.setTimeout],
  ['setInterval', windowStub.setInterval],
  ['clearTimeout', windowStub.clearTimeout],
  ['clearInterval', windowStub.clearInterval],
]) {
  defineGlobal(name, value);
}
if (typeof g.performance === 'undefined') {
  defineGlobal('performance', windowStub.performance);
}
// `addEventListener` at top level is the window's.
defineGlobal('addEventListener', windowStub.addEventListener);
defineGlobal('removeEventListener', windowStub.removeEventListener);

// ── load ────────────────────────────────────────────────────────────────────
const vm = require('vm');
let loadFailed = false;
for (const [i, src] of input.scripts.entries()) {
  try {
    vm.runInThisContext(src, {filename: `inline-script-${i + 1}.js`,
                              timeout: 10000});
  } catch (e) {
    loadFailed = true;
    note(e);
    break;
  }
}
result.loaded = !loadFailed;

// Pages that wait for load/DOMContentLoaded before doing anything.
if (result.loaded) {
  for (const target of [documentStub, windowStub]) {
    for (const type of ['DOMContentLoaded', 'load']) {
      fire(target, type, {type});
    }
  }
}

// ── drive ───────────────────────────────────────────────────────────────────
const KEYS = [
  {key: 'ArrowRight', code: 'ArrowRight', keyCode: 39, which: 39},
  {key: 'ArrowDown', code: 'ArrowDown', keyCode: 40, which: 40},
  {key: 'ArrowLeft', code: 'ArrowLeft', keyCode: 37, which: 37},
  {key: 'ArrowUp', code: 'ArrowUp', keyCode: 38, which: 38},
  {key: ' ', code: 'Space', keyCode: 32, which: 32},
];

if (result.loaded) {
  for (let tick = 0; tick < input.ticks; tick++) {
    const k = KEYS[tick % KEYS.length];
    const ev = Object.assign({type: 'keydown', preventDefault() {},
                              stopPropagation() {}, repeat: false}, k);
    for (const target of [documentStub, windowStub, bodyEl]) {
      fire(target, 'keydown', ev);
    }
    for (const target of [documentStub, windowStub, bodyEl]) {
      fire(target, 'keyup', Object.assign({}, ev, {type: 'keyup'}));
    }

    const due = frames.splice(0, frames.length);
    for (const fn of due) {
      try { fn(16.7 * (tick + 1)); } catch (e) { note(e); }
    }
    const pending = timers.splice(0, timers.length);
    for (const t of pending) {
      try { t.fn(); } catch (e) { note(e); }
      if (t.repeat) timers.push(t);
    }
    result.ticks = tick + 1;
    if (result.errors.length >= 8) break;
  }
}

result.drew = drew;
process.stdout.write(JSON.stringify(result));
"""


def _node_check(node: str, script: str, workdir: Path, label: str) -> str | None:
    """`node --check` on one script body. Returns the error, or None."""
    f = workdir / f"{label}.js"
    f.write_text(script, encoding="utf-8")
    try:
        proc = subprocess.run([node, "--check", str(f)], capture_output=True,
                              text=True, timeout=SMOKE_TIMEOUT_S,
                              encoding="utf-8", errors="replace")
    except (subprocess.TimeoutExpired, OSError) as e:  # quiet-ok: returned as this script's parse error
        return f"{label}: could not be parsed ({type(e).__name__})"
    if proc.returncode == 0:
        return None
    err = ((proc.stderr or "") + (proc.stdout or "")).strip()
    return f"{label}: {err.splitlines()[0][:200] if err else 'syntax error'}"


def _smoke_sync(html_path: Path) -> HtmlSmokeResult:
    res = HtmlSmokeResult()
    node = node_executable()
    if node is None:
        res.skipped_reason = "node is not installed on this machine"
        return res
    try:
        html = html_path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:  # quiet-ok: the note says the page could not be read
        res.skipped_reason = f"the page could not be read ({e})"
        return res

    inline, external = inline_scripts(html)
    # A project's own .js files are part of the project, so they are loaded in
    # document order ahead of the inline code. Anything off-machine is not
    # fetched — a smoke test that reached the network would be testing a CDN.
    local, remote = [], []
    for src in external:
        resolved = _local_script(html_path.parent, src)
        (local if resolved is not None else remote).append(resolved or src)
    scripts = [p.read_text(encoding="utf-8", errors="replace") for p in local] + inline
    if not scripts:
        res.skipped_reason = (
            "the page has no JavaScript in the project to run"
            + (f" (it loads {len(remote)} script(s) from the network, which "
               "this check does not fetch)" if remote else ""))
        return res
    res.remote_scripts = len(remote)
    res.local_scripts = len(local)
    res.inline_scripts = len(inline)

    work = Path(tempfile.mkdtemp(prefix="nova-html-smoke-"))
    try:
        res.ran = True
        errors = [e for e in (_node_check(node, s, work, f"inline-script-{i+1}")
                              for i, s in enumerate(scripts)) if e]
        if errors:
            res.errors = errors
            return res
        res.syntax_ok = True

        (work / "harness.js").write_text(_HARNESS_JS, encoding="utf-8")
        payload = work / "input.json"
        payload.write_text(json.dumps({"scripts": scripts, "ticks": SMOKE_TICKS}),
                           encoding="utf-8")
        try:
            proc = subprocess.run(
                [node, str(work / "harness.js"), str(payload)],
                capture_output=True, text=True, timeout=SMOKE_TIMEOUT_S,
                cwd=str(work), stdin=subprocess.DEVNULL,
                encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired:  # quiet-ok: recorded as the page's error, which the note reports
            res.errors = ["the page did not finish 60 ticks within "
                          f"{SMOKE_TIMEOUT_S:g}s — it may be looping"]
            return res
        except OSError as e:  # quiet-ok: the note says node could not be started
            res.ran = False
            res.skipped_reason = f"node could not be started ({e})"
            return res

        raw = (proc.stdout or "").strip()
        try:
            data = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
        except (ValueError, json.JSONDecodeError):
            # THE HARNESS broke, not the page. Reporting this as the project
            # failing is the same misattribution H18 was about, so it is
            # recorded as "nothing ran" and the reason is kept.
            detail = ((proc.stderr or raw).strip()[:250] or "no output")
            logger.warning("html_smoke_harness_error", path=str(html_path),
                           detail=detail)
            res.ran = False
            res.syntax_ok = False
            res.skipped_reason = ("the smoke harness itself failed, so this "
                                  f"says nothing about the project ({detail})")
            return res
        res.loaded = bool(data.get("loaded"))
        res.ticks = int(data.get("ticks") or 0)
        res.drew = int(data.get("drew") or 0)
        res.key_handlers = int(data.get("keyHandlers") or 0)
        res.errors = [str(e)[:300] for e in (data.get("errors") or [])]
        return res
    finally:
        shutil.rmtree(work, ignore_errors=True)


async def smoke_test_html(html_path: Path) -> HtmlSmokeResult:
    """Run the smoke test off the event loop."""
    try:
        return await asyncio.to_thread(_smoke_sync, html_path)
    except Exception as e:  # noqa: BLE001
        logger.warning("html_smoke_failed", path=str(html_path), error=str(e)[:200])
        res = HtmlSmokeResult()
        res.skipped_reason = f"the smoke test could not run ({type(e).__name__})"
        return res
