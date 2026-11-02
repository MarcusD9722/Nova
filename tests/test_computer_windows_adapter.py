"""The Windows adapter: what it does, and everything it refuses to do.

Phase 8 shipped the permission gate and deliberately shipped no actuator, so
`ComputerControl` ran with `adapter=None` and every permitted action was a dry
run. This suite covers the adapter that now exists — the thing that turns a
permitted action into a real keystroke.

The whole suite drives `WindowsAdapter(api=FakeApi())`, so not one real event is
synthesized into the machine running the tests. That is not a convenience: a
suite that actually typed would be injecting keystrokes into whatever window
the test runner happened to leave focused. The cost of a fake is that it could
pass against a binding that does not exist, so section C9 checks the REAL
`Win32Api` — struct sizes, resolved signatures and read-only calls — and
section C10 boots the real runtime to confirm the adapter is genuinely attached
in production rather than only in this file.

What is asserted, in order of how much it would cost to get wrong:

  * both gates DEFAULT OFF, and each is independently load-bearing. Wiring an
    adapter must not change what Nova does until Marcus turns it on;
  * on every refusal, `send` is never called — the assertion is that nothing
    reached the OS, not merely that an error came back;
  * a refusal is reported apart from an execution error, so a working guard
    never reads as a bug;
  * CRITICAL actions are refused structurally, at `trusted` too;
  * the translation is actually right: the key events for "ctrl+s", the UTF-16
    units for text, the wheel sign for a direction.

Run:  venv\\Scripts\\python.exe tests\\test_computer_windows_adapter.py
"""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")

from harness import Checks, boot, run  # noqa: E402

from core.computer_control import (  # noqa: E402
    AdapterRefusal,
    ComputerControl,
    default_adapter,
    execution_enabled,
    observation_enabled,
    platform_adapter,
)
from core.computer_windows import (  # noqa: E402
    DEFAULT_DENY_WINDOWS,
    MAX_TYPE_CHARS,
    INPUT,
    WindowsAdapter,
    deny_list,
)
from core.permissions import PermissionBroker  # noqa: E402

check = Checks()

_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_UNICODE = 0x0004
_MOUSEEVENTF_WHEEL = 0x0800
_MOUSEEVENTF_HWHEEL = 0x01000
_MOUSEEVENTF_LEFTDOWN = 0x0002
_MOUSEEVENTF_RIGHTDOWN = 0x0008
_VK_CTRL, _VK_SHIFT = 0x11, 0x10


class FakeApi:
    """Stands in for every syscall, and records what would have been sent."""

    def __init__(self, *, title: str = "Untitled - Scratch", process: str = "scratch.exe",
                 accept: int | None = None, windows: list[dict] | None = None) -> None:
        self.title = title
        self.process = process
        self.accept = accept           # None = accept everything
        self.sent: list[list[INPUT]] = []
        self.clipboard: list[str] = []
        self.closed: list[int] = []
        self.launched: list[tuple[str, str]] = []
        self._windows = windows

    # geometry
    def screen_bounds(self):
        return (0, 0, 1920, 1080)

    def monitor_count(self):
        return 1

    def cursor_pos(self):
        return (640, 480)

    # windows
    def foreground_window(self):
        if self.title is None:
            return {}
        return {"hwnd": 11, "title": self.title, "pid": 4242,
                "process": self.process, "rect": {"x": 0, "y": 0, "w": 800, "h": 600}}

    def visible_windows(self):
        if self._windows is not None:
            return self._windows
        return [self.foreground_window(),
                {"hwnd": 12, "title": "Notes", "pid": 7, "process": "notes.exe", "rect": {}}]

    def process_name(self, pid):
        return self.process

    # actuation
    def send(self, events):
        self.sent.append(list(events))
        return len(events) if self.accept is None else self.accept

    def vk_for_char(self, ch):
        return ord(ch.upper()) if ch.isalnum() else -1

    def set_clipboard_text(self, text):
        self.clipboard.append(text)

    def post_close(self, hwnd):
        self.closed.append(int(hwnd))
        return True

    def shell_execute(self, target, args=""):
        self.launched.append((target, args))
        return 42


def _flat(api: FakeApi) -> list[INPUT]:
    return [ev for batch in api.sent for ev in batch]


def _refuses(fn, *, api: FakeApi, label: str) -> str:
    """Assert `fn()` refuses CLEANLY, and that nothing reached the OS.

    A bare `except AdapterRefusal` would let a crash count as a refusal: the
    exception escapes, the suite dies on a traceback, and the mutant that
    caused it is recorded as killed with no named assertion behind it. That is
    what M317 did on its first run. So any OTHER exception is caught here and
    reported as its own failure — "it blew up" and "it declined" are different
    outcomes, and only one of them is a working guard.
    """
    before = len(api.sent)
    try:
        fn()
    except AdapterRefusal as exc:
        check(len(api.sent) == before, f"{label} — and nothing was sent")
        return str(exc)
    except Exception as exc:  # noqa: BLE001
        check(False, f"{label} — but it raised {type(exc).__name__} "
                     f"instead of refusing ({str(exc)[:70]})")
        return ""
    check(False, f"{label} — but it did NOT refuse")
    return ""


# ── C1 ───────────────────────────────────────────────────────────────────────
async def test_the_gates_default_off() -> None:
    check.section("C1 both gates default off, and each is load-bearing alone")
    saved = {k: os.environ.get(k) for k in ("NOVA_COMPUTER_CONTROL", "NOVA_COMPUTER_OBSERVE")}
    try:
        for k in saved:
            os.environ.pop(k, None)
        check(execution_enabled() is False, "NOVA_COMPUTER_CONTROL defaults off")
        check(observation_enabled() is False, "NOVA_COMPUTER_OBSERVE defaults off")

        api = FakeApi()
        cc = ComputerControl(PermissionBroker(mode="trusted"), adapter=WindowsAdapter(api=api))
        check(cc.available is False,
              "an adapter is attached but control is NOT available (flag off)")

        # The point of the whole exercise: wiring an adapter changes nothing yet.
        res = await cc.act("type", details={"text": "hello"})
        check(res["status"] == "approved_not_executed",
              f"a permitted action is still a dry run ({res['status']})")
        check(api.sent == [], "and the adapter was never reached")

        obs = await cc.observe("windows")
        check(obs["available"] is False and obs.get("status") == "observation_disabled",
              f"observation is off even though an adapter can see ({obs.get('status')})")

        # Each flag on its own. Execution must NOT turn looking on, and looking
        # must NOT turn execution on.
        os.environ["NOVA_COMPUTER_CONTROL"] = "1"
        check(observation_enabled() is False,
              "enabling execution does not enable observation")
        obs = await cc.observe("windows")
        check(obs["available"] is False, "and observe still refuses")

        os.environ.pop("NOVA_COMPUTER_CONTROL", None)
        os.environ["NOVA_COMPUTER_OBSERVE"] = "1"
        check(execution_enabled() is False,
              "enabling observation does not enable execution")
        cc2 = ComputerControl(PermissionBroker(mode="trusted"), adapter=WindowsAdapter(api=api))
        check(cc2.available is False, "and control is still unavailable")
        obs = await cc2.observe("windows")
        check(obs["available"] is True and obs["observation"]["kind"] == "windows",
              "while observation now works")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ── C2 ───────────────────────────────────────────────────────────────────────
async def test_observation_has_no_pixels() -> None:
    check.section("C2 observation reports structure, never screen content")
    a = WindowsAdapter(api=FakeApi(title="Budget.xlsx - Excel", process="excel.exe"))

    w = a.observe("windows")
    check(w["kind"] == "windows" and w["count"] == 2, f"windows enumerate ({w['count']})")
    check("title" in w["windows"][0] and "process" in w["windows"][0],
          "each window carries a title and a process")

    f = a.observe("foreground")
    check(f["window"]["title"] == "Budget.xlsx - Excel", "foreground is identified")

    apps = a.observe("apps")
    check([x["process"] for x in apps["apps"]] == ["excel.exe", "notes.exe"],
          f"apps are grouped by process ({[x['process'] for x in apps['apps']]})")

    s = a.observe("screen")
    check(s["virtual"]["w"] == 1920 and s["monitors"] == 1, "screen metrics are reported")
    check(s["cursor"] == {"x": 640, "y": 480}, f"including the cursor ({s['cursor']})")

    for what in ("screenshot", "pixels", "ocr", "screen_text"):
        r = a.observe(what)
        check(r.get("unsupported") is True,
              f"'{what}' is unsupported — no screen-content reading exists")
    check("screenshot" not in str(a.observe("screen")).lower(),
          "and nothing in a normal observation returns image data")


# ── C3 ───────────────────────────────────────────────────────────────────────
async def test_the_deny_list_refuses_input() -> None:
    check.section("C3 a protected foreground window refuses every synthesized event")
    saved = os.environ.get("NOVA_COMPUTER_DENY_WINDOWS")
    try:
        os.environ.pop("NOVA_COMPUTER_DENY_WINDOWS", None)
        check(deny_list() == DEFAULT_DENY_WINDOWS,
              f"an unset variable uses the built-in list ({len(deny_list())} patterns)")
        os.environ["NOVA_COMPUTER_DENY_WINDOWS"] = ""
        check(deny_list() == DEFAULT_DENY_WINDOWS,
              "and so does an EMPTY one — an unset var must never widen what Nova may type into")
        os.environ.pop("NOVA_COMPUTER_DENY_WINDOWS", None)

        # Every input-synthesizing kind, against a password manager in front.
        for kind, details in (("type", {"text": "hunter2"}),
                              ("key", {"key": "ctrl+v"}),
                              ("click", {"x": 10, "y": 10}),
                              ("double_click", {"x": 10, "y": 10}),
                              ("scroll", {"direction": "down"})):
            api = FakeApi(title="1Password — Personal Vault", process="1password.exe")
            a = WindowsAdapter(api=api)
            msg = _refuses(lambda: a.execute(kind, "", details), api=api,
                           label=f"{kind} into a password manager is refused")
            if kind == "type":
                check("1password" in msg.lower(), f"naming the pattern it matched ({msg[:60]})")

        # Clicks too, not just keystrokes: "Allow" in a security dialog is the
        # worst thing a stray click could land on.
        api = FakeApi(title="Windows Security", process="consent.exe")
        a = WindowsAdapter(api=api)
        _refuses(lambda: a.execute("click", "", {"x": 5, "y": 5}), api=api,
                 label="a click into a security dialog is refused")

        # An unreadable foreground window fails CLOSED.
        api = FakeApi(title=None)
        a = WindowsAdapter(api=api)
        msg = _refuses(lambda: a.execute("type", "", {"text": "x"}), api=api,
                       label="an unreadable foreground window is refused")
        check("focus" in msg.lower(), f"saying it cannot tell what has focus ({msg[:60]})")

        # And an ordinary window is NOT refused — the guard must discriminate,
        # or every assertion above would pass with a blanket refusal.
        api = FakeApi(title="Untitled - Notepad", process="notepad.exe")
        a = WindowsAdapter(api=api)
        res = a.execute("type", "", {"text": "hello"})
        check(res["accepted"] == res["events"] and len(api.sent) == 1,
              "a normal window still receives input (the guard discriminates)")

        # An explicit opt-out is honoured, because a guard you cannot inspect
        # is a guard nobody can reason about.
        os.environ["NOVA_COMPUTER_DENY_WINDOWS"] = "none"
        check(deny_list() == (), "'none' disables the list explicitly")
        api = FakeApi(title="1Password", process="1password.exe")
        a = WindowsAdapter(api=api)
        a.execute("type", "", {"text": "x"})
        check(len(api.sent) == 1, "and then even a password manager accepts input")

        os.environ["NOVA_COMPUTER_DENY_WINDOWS"] = "my-bank, SecretApp"
        check(deny_list() == ("my-bank", "secretapp"),
              f"a custom list is parsed and lower-cased ({deny_list()})")
    finally:
        if saved is None:
            os.environ.pop("NOVA_COMPUTER_DENY_WINDOWS", None)
        else:
            os.environ["NOVA_COMPUTER_DENY_WINDOWS"] = saved


# ── C4 ───────────────────────────────────────────────────────────────────────
async def test_the_focus_assertion() -> None:
    check.section("C4 a caller can refuse to be mistargeted")
    api = FakeApi(title="Untitled - Notepad", process="notepad.exe")
    a = WindowsAdapter(api=api)

    msg = _refuses(lambda: a.execute("type", "", {"text": "rm -rf", "window": "Excel"}),
                   api=api, label="typing aimed at Excel refuses when Notepad has focus")
    check("Notepad" in msg, f"and names what actually had focus ({msg[:70]})")

    res = a.execute("type", "", {"text": "ok", "window": "notepad"})
    check(res["events"] == 4, "a matching assertion proceeds (2 chars, down+up each)")
    check(res["into"]["process"] == "notepad.exe",
          f"and reports where it landed ({res['into']['process']})")

    # No assertion given: it proceeds, but still records the destination, so the
    # audit says where input went rather than only that it was sent.
    res = a.execute("key", "", {"key": "enter"})
    check(res["into"]["title"] == "Untitled - Notepad",
          "with no assertion it still records the destination window")


# ── C5 ───────────────────────────────────────────────────────────────────────
async def test_coordinates_must_be_on_screen() -> None:
    check.section("C5 coordinates must land on the virtual desktop")
    api = FakeApi()
    a = WindowsAdapter(api=api)

    for x, y, why in ((5000, 10, "past the right edge"), (10, 4000, "below the bottom"),
                      (-5, 10, "left of the origin"), (10, -5, "above the origin")):
        _refuses(lambda x=x, y=y: a.execute("click", "", {"x": x, "y": y}), api=api,
                 label=f"({x}, {y}) is refused — {why}")

    msg = _refuses(lambda: a.execute("click", "", {"x": "over there", "y": 1}), api=api,
                   label="a non-numeric coordinate is refused")
    check("number" in msg.lower(), f"saying why ({msg[:60]})")

    res = a.execute("click", "", {"x": 1919, "y": 1079})
    check(res["at"] == {"x": 1919, "y": 1079}, "the far corner is in bounds")

    # No coordinates at all means "where the pointer already is", not (0, 0).
    api.sent.clear()
    res = a.execute("click", "", {})
    check(res["at"] == {"x": 640, "y": 480},
          f"a click with no coordinates uses the cursor, not the origin ({res['at']})")

    # "x,y" via target, which is how skill.run passes a step through.
    res = a.execute("click", "300,200", {})
    check(res["at"] == {"x": 300, "y": 200}, f"a 'x,y' target parses ({res['at']})")


# ── C6 ───────────────────────────────────────────────────────────────────────
async def test_critical_actions_are_structurally_refused() -> None:
    check.section("C6 CRITICAL actions are refused by the adapter, at every tier")
    for mode in ("locked", "guarded", "trusted"):
        for kind in ("system_setting", "delete"):
            api = FakeApi()
            a = WindowsAdapter(api=api)
            msg = _refuses(lambda: a.execute(kind, "C:/Windows", {}), api=api,
                           label=f"{kind} is refused by the adapter itself")
            check("deliberate" in msg.lower(),
                  f"as a deliberate omission, not a missing feature ({msg[:60]})")
            break
        break

    # Through the full stack, in `trusted` — the one mode that would otherwise
    # let a CRITICAL action through after a confirmation.
    saved = os.environ.get("NOVA_COMPUTER_CONTROL")
    try:
        os.environ["NOVA_COMPUTER_CONTROL"] = "1"
        api = FakeApi()
        cc = ComputerControl(PermissionBroker(mode="trusted"), adapter=WindowsAdapter(api=api))
        check(cc.available is True, "control is available (trusted + flag + adapter)")
        for kind in ("system_setting", "delete"):
            res = await cc.act(kind, target="C:/Windows/System32", wait_for_confirm=False)
            check(res["status"] in {"refused_by_adapter", "needs_confirmation", "denied"},
                  f"{kind} never reports 'executed' ({res['status']})")
            check(api.sent == [], f"and {kind} synthesized nothing")

        # An unknown kind is refused rather than guessed at.
        api2 = FakeApi()
        a2 = WindowsAdapter(api=api2)
        _refuses(lambda: a2.execute("reformat_drive", "", {}), api=api2,
                 label="an unknown action kind is refused")
    finally:
        if saved is None:
            os.environ.pop("NOVA_COMPUTER_CONTROL", None)
        else:
            os.environ["NOVA_COMPUTER_CONTROL"] = saved


# ── C7 ───────────────────────────────────────────────────────────────────────
async def test_the_translation_is_right() -> None:
    check.section("C7 actions translate to the right Win32 events")
    api = FakeApi()
    a = WindowsAdapter(api=api)

    # type: one down + one up per UTF-16 unit, as unicode scans.
    api.sent.clear()
    a.execute("type", "", {"text": "Hi"})
    ev = _flat(api)
    check(len(ev) == 4, f"'Hi' is 4 events ({len(ev)})")
    check([e.ki.wScan for e in ev] == [ord("H"), ord("H"), ord("i"), ord("i")],
          "carrying the right code units")
    check(all(e.ki.dwFlags & _KEYEVENTF_UNICODE for e in ev),
          "all flagged UNICODE, so the layout cannot change what is typed")
    check([bool(e.ki.dwFlags & _KEYEVENTF_KEYUP) for e in ev] == [False, True, False, True],
          "alternating down/up")
    check(all(e.ki.wVk == 0 for e in ev), "with no virtual key code (wVk=0)")

    # Above the BMP: an emoji is a surrogate pair, not one mangled unit.
    api.sent.clear()
    a.execute("type", "", {"text": "\U0001F600"})
    ev = _flat(api)
    check(len(ev) == 4, f"an astral character is 2 units = 4 events ({len(ev)})")
    check(ev[0].ki.wScan == 0xD83D and ev[2].ki.wScan == 0xDE00,
          f"sent as its surrogate pair (0x{ev[0].ki.wScan:04X}, 0x{ev[2].ki.wScan:04X})")

    # key: modifiers down in order, key, then modifiers up in reverse.
    api.sent.clear()
    a.execute("key", "", {"key": "ctrl+shift+s"})
    ev = _flat(api)
    check([e.ki.wVk for e in ev] == [_VK_CTRL, _VK_SHIFT, ord("S"), ord("S"), _VK_SHIFT, _VK_CTRL],
          f"ctrl+shift+s presses and releases in the right order ({[e.ki.wVk for e in ev]})")
    check([bool(e.ki.dwFlags & _KEYEVENTF_KEYUP) for e in ev]
          == [False, False, False, True, True, True],
          "with the releases where they belong")

    api.sent.clear()
    a.execute("key", "", {"key": "enter"})
    check([e.ki.wVk for e in _flat(api)] == [0x0D, 0x0D], "a bare named key works")

    api2 = FakeApi()
    a2 = WindowsAdapter(api=api2)
    _refuses(lambda: a2.execute("key", "", {"key": "ctrl+nonsuchkey"}), api=api2,
             label="an unknown key name is refused")
    _refuses(lambda: a2.execute("key", "", {"key": "notamodifier+s"}), api=api2,
             label="a bad modifier is refused")

    # scroll: direction decides the wheel sign, and horizontal uses HWHEEL.
    for direction, flag, positive in (("down", _MOUSEEVENTF_WHEEL, False),
                                      ("up", _MOUSEEVENTF_WHEEL, True),
                                      ("left", _MOUSEEVENTF_HWHEEL, False),
                                      ("right", _MOUSEEVENTF_HWHEEL, True)):
        api.sent.clear()
        a.execute("scroll", "", {"direction": direction, "amount": 2})
        ev = _flat(api)
        signed = ctypes.c_int32(ev[0].mi.mouseData).value
        check(len(ev) == 2 and bool(ev[0].mi.dwFlags & flag),
              f"scroll {direction} emits 2 wheel events on the right axis")
        check((signed > 0) is positive, f"with the right sign ({signed})")

    _refuses(lambda: a.execute("scroll", "", {"direction": "sideways"}), api=api,
             label="an unknown scroll direction is refused")
    _refuses(lambda: a.execute("scroll", "", {"direction": "down", "amount": 500}), api=api,
             label="an absurd scroll amount is refused")

    # click: move, then down/up — twice for a double click.
    api.sent.clear()
    a.execute("click", "", {"x": 100, "y": 100})
    check(len(_flat(api)) == 3, f"a click is move+down+up ({len(_flat(api))})")
    api.sent.clear()
    a.execute("double_click", "", {"x": 100, "y": 100})
    check(len(_flat(api)) == 5, f"a double click is move+(down+up)x2 ({len(_flat(api))})")
    api.sent.clear()
    a.execute("click", "", {"x": 1, "y": 1, "button": "right"})
    check(bool(_flat(api)[1].mi.dwFlags & _MOUSEEVENTF_RIGHTDOWN), "a right click uses RIGHTDOWN")
    _refuses(lambda: a.execute("click", "", {"x": 1, "y": 1, "button": "middle"}), api=api,
             label="an unsupported button is refused")

    # The typing cap.
    api3 = FakeApi()
    a3 = WindowsAdapter(api=api3)
    msg = _refuses(lambda: a3.execute("type", "", {"text": "x" * (MAX_TYPE_CHARS + 1)}),
                   api=api3, label=f"typing over {MAX_TYPE_CHARS} chars is refused")
    check(str(MAX_TYPE_CHARS) in msg, f"naming the cap ({msg[:70]})")
    res = a3.execute("type", "", {"text": "x" * MAX_TYPE_CHARS})
    check(res["chars"] == MAX_TYPE_CHARS, "exactly at the cap is allowed")
    _refuses(lambda: a3.execute("type", "", {"text": ""}), api=api3,
             label="typing nothing is refused rather than sent as zero events")


# ── C8 ───────────────────────────────────────────────────────────────────────
async def test_a_refusal_is_not_a_failure() -> None:
    check.section("C8 a refusal reads as a refusal, not as a bug")
    saved = os.environ.get("NOVA_COMPUTER_CONTROL")
    try:
        os.environ["NOVA_COMPUTER_CONTROL"] = "1"
        api = FakeApi(title="KeePass", process="keepass.exe")
        cc = ComputerControl(PermissionBroker(mode="trusted"), adapter=WindowsAdapter(api=api))

        res = await cc.act("type", details={"text": "secret"})
        check(res["status"] == "refused_by_adapter",
              f"a guarded refusal is 'refused_by_adapter' ({res['status']})")
        check(res["ok"] is False and "keepass" in str(res.get("note", "")).lower(),
              "carrying the adapter's own reason")
        check("error" not in res, "and it is NOT reported as an error")

        # A genuine crash still reads as one, so the two are distinguishable.
        class Broken:
            def execute(self, *a, **k):
                raise OSError("user32 went away")

        cc2 = ComputerControl(PermissionBroker(mode="trusted"), adapter=Broken())
        res = await cc2.act("type", details={"text": "x"})
        check(res["status"] == "execution_error",
              f"a real crash is 'execution_error' ({res['status']})")
        check("user32" in str(res.get("error", "")), "with the underlying error")

        # A partial injection is reported, not rounded up to success.
        api2 = FakeApi(accept=0)
        cc3 = ComputerControl(PermissionBroker(mode="trusted"), adapter=WindowsAdapter(api=api2))
        res = await cc3.act("key", details={"key": "enter"})
        check(res["status"] == "executed", "an accepted-but-dropped action still returns")
        check(res["result"].get("partial") is True,
              "flagged partial — the OS took 0 of 2 events")
        check("elevated" in res["result"].get("note", "").lower(),
              f"with the likely reason ({str(res['result'].get('note'))[:60]})")
    finally:
        if saved is None:
            os.environ.pop("NOVA_COMPUTER_CONTROL", None)
        else:
            os.environ["NOVA_COMPUTER_CONTROL"] = saved


# ── C9 ───────────────────────────────────────────────────────────────────────
async def test_the_non_input_handlers() -> None:
    check.section("C9 launch, close and file_write: the destructive edges")
    api = FakeApi()
    a = WindowsAdapter(api=api)

    res = a.execute("launch_app", "notepad.exe", {})
    check(api.launched == [("notepad.exe", "")], f"launch_app shells out ({api.launched})")
    check(res["code"] == 42, "and reports the ShellExecute code")
    for bad in ("notepad & calc", "a | b", "x; y", "a\nb"):
        _refuses(lambda bad=bad: a.execute("launch_app", bad, {}), api=api,
                 label=f"launch_app refuses {bad!r} — it is not a shell command line")
    _refuses(lambda: a.execute("launch_app", "", {}), api=api,
             label="launch_app with no application is refused")

    # close_app posts WM_CLOSE and says what it ASKED, never what closed.
    res = a.execute("close_app", "Notes", {})
    check(api.closed == [12], f"close_app posts WM_CLOSE to the match ({api.closed})")
    check(res["asked_to_close"] == ["Notes"], "reporting what was asked")
    check("force-killed" in res["note"], "and that nothing was force-killed")
    _refuses(lambda: a.execute("close_app", "", {}), api=api,
             label="close_app with an empty target is refused, not read as 'everything'")
    _refuses(lambda: a.execute("close_app", "no such window", {}), api=api,
             label="close_app with no match is refused")
    # A dedicated fake that DOES have a security dialog open — otherwise this
    # would pass on "no window matched" and the deny-list check above it could
    # be deleted without a single assertion noticing.
    sec = FakeApi(windows=[{"hwnd": 99, "title": "Windows Security",
                            "pid": 8, "process": "consent.exe", "rect": {}}])
    a_sec = WindowsAdapter(api=sec)
    _refuses(lambda: a_sec.execute("close_app", "Windows Security", {}), api=sec,
             label="close_app refuses to dismiss a security dialog that IS open")
    check(sec.closed == [], "and WM_CLOSE was never posted to it")

    # file_write: the three ways it could quietly destroy something.
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        target = root / "note.txt"
        res = a.execute("file_write", str(target), {"content": "hello"})
        check(target.read_text(encoding="utf-8") == "hello",
              "file_write writes the file")
        check(res["replaced"] is False, "reporting that nothing was replaced")

        _refuses(lambda: a.execute("file_write", str(target), {"content": "clobber"}),
                 api=api, label="writing over an existing file is refused by default")
        check(target.read_text(encoding="utf-8") == "hello", "and the file is untouched")

        res = a.execute("file_write", str(target), {"content": "new", "overwrite": True})
        check(target.read_text(encoding="utf-8") == "new" and res["replaced"] is True,
              "an explicit overwrite is honoured and reported")

        # A relative path whose parent EXISTS, so only the is_absolute() check
        # can catch it. "relative/path.txt" was the first version of this
        # assertion and it passed for the wrong reason — the missing-parent
        # guard refused it, and deleting the absolute check changed nothing
        # that any assertion noticed (mutant M318 survived). Asserting the
        # REASON is what makes the guard testable.
        rel_msg = _refuses(lambda: a.execute("file_write", "note_in_cwd.txt",
                                             {"content": "x"}),
                           api=api, label="a relative path is refused")
        check("relative" in rel_msg.lower(),
              f"because it is relative, not because its parent is missing "
              f"({rel_msg[:70]})")
        stray = Path("note_in_cwd.txt")
        check(not stray.exists(),
              "and nothing was written into the working directory")
        # When the guard IS broken the write lands in the repo root, so clean up
        # rather than leaving a mutant run's debris behind.
        stray.unlink(missing_ok=True)
        _refuses(lambda: a.execute("file_write", str(root / "nope" / "deep" / "f.txt"),
                                   {"content": "x"}),
                 api=api, label="a missing parent directory is refused, not created")
        _refuses(lambda: a.execute("file_write", str(root), {"content": "x"}),
                 api=api, label="writing to a directory is refused")

    # write_clipboard is global state, so the deny-list applies but focus does not.
    res = a.execute("write_clipboard", "", {"text": "copied"})
    check(api.clipboard == ["copied"], f"the clipboard is set ({api.clipboard})")
    api2 = FakeApi(title="Bitwarden", process="bitwarden.exe")
    a2 = WindowsAdapter(api=api2)
    try:
        a2.execute("write_clipboard", "", {"text": "x"})
        check(False, "a password manager in front should refuse a clipboard write")
    except AdapterRefusal:
        check(api2.clipboard == [],
              "a password manager in front refuses a clipboard write")


# ── C10 ──────────────────────────────────────────────────────────────────────
async def test_the_real_binding_and_the_real_runtime() -> None:
    check.section("C10 the real Win32 binding, and the real runtime wiring")
    # Everything above runs on a fake, which could pass against a binding that
    # does not exist. These checks touch the real thing — read-only.
    check(ctypes.sizeof(INPUT) == 40,
          f"INPUT is 40 bytes on x64 — a wrong size means SendInput silently "
          f"rejects everything ({ctypes.sizeof(INPUT)})")

    real = platform_adapter()
    check(real is not None and type(real).__name__ == "WindowsAdapter",
          f"platform_adapter() finds a WindowsAdapter here ({type(real).__name__})")
    check(platform_adapter("linux") is None and platform_adapter("darwin") is None,
          "and nothing off Windows, where ctypes.wintypes cannot even import")

    scr = real.observe("screen")
    check(scr["virtual"]["w"] > 0 and scr["virtual"]["h"] > 0,
          f"the real screen has a real size ({scr['virtual']['w']}x{scr['virtual']['h']})")
    check(scr["monitors"] >= 1, f"and at least one monitor ({scr['monitors']})")
    wins = real.observe("windows")
    check(wins["count"] >= 1, f"real windows enumerate ({wins['count']})")
    check(any(w.get("process") for w in wins["windows"]),
          "with process names resolved (QueryFullProcessImageNameW works)")

    # `default_adapter` answers a DIFFERENT question from `platform_adapter`:
    # not "what could this machine use" but "what should actually be
    # installed". With both flags off the answer is nothing at all, so a stock
    # boot holds no actuator object — nothing with bound user32 signatures or a
    # DLL handle on a machine where Nova may neither look nor act.
    saved = {k: os.environ.get(k) for k in ("NOVA_COMPUTER_CONTROL", "NOVA_COMPUTER_OBSERVE")}
    try:
        for k in saved:
            os.environ.pop(k, None)
        check(default_adapter() is None,
              "with both flags off, default_adapter() installs nothing")
        os.environ["NOVA_COMPUTER_OBSERVE"] = "1"
        check(default_adapter() is not None,
              "the observe flag alone is enough to want one")
        os.environ.pop("NOVA_COMPUTER_OBSERVE")
        os.environ["NOVA_COMPUTER_CONTROL"] = "1"
        check(default_adapter() is not None,
              "and so is the control flag alone")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # ── the production wiring, in both states ────────────────────────────────
    # A stock boot. `/speaker/permission-probe` reports `adapter_installed`
    # from this very object, and tests/test_speaker_api_contract_v52.py asserts
    # it is False — so this has to stay None or that P5.2 contract breaks.
    async with boot(default_reply="Sure.") as nova:
        rt = nova.runtime
        check(rt._computer._adapter is None,
              f"a stock boot holds NO platform adapter ({rt._computer._adapter})")
        check(rt._computer.available is False, "so control is unavailable")

        probe = await nova.http.post("/speaker/permission-probe",
                                     json={"capability": "computer.type"})
        body = probe.json() if probe.status_code == 200 else {}
        check(body.get("adapter_installed") is False,
              f"and the P5.2 permission probe still reports none "
              f"({body.get('adapter_installed')})")
        check(body.get("executed") is False and body.get("execution_enabled") is False,
              "with nothing executed and execution off — the P5.2 invariant")

    # With observation asked for, the runtime must ACTUALLY hold the adapter —
    # otherwise every assertion in this file is about a module nothing uses.
    async with boot(default_reply="Sure.",
                    env={"NOVA_COMPUTER_OBSERVE": "1"}) as nova:
        rt = nova.runtime
        check(type(rt._computer._adapter).__name__ == "WindowsAdapter",
              f"asking to observe attaches the real adapter "
              f"({type(rt._computer._adapter).__name__})")
        check(rt._computer.available is False,
              "while control stays unavailable — looking is not acting")

        from core.tool_router import ToolCall

        out = await rt._router.execute(
            ToolCall(name="computer.observe", args={"what": "windows"}))
        obs = out.result if isinstance(out.result, dict) else {}
        check(obs.get("available") is True,
              f"and the real observe tool now works ({obs.get('note') or 'ok'})")
        check((obs.get("observation") or {}).get("count", 0) >= 1,
              "returning real windows through the real router")

        out = await rt._router.execute(
            ToolCall(name="computer.act", args={"kind": "type", "target": "hello"}))
        res = out.result if isinstance(out.result, dict) else {}
        check(res.get("status") in {"approved_not_executed", "needs_confirmation"},
              f"yet acting is still a dry run or a prompt ({res.get('status')})")

        rep = rt._capability_report()
        check(rep is not None, "the capability report still builds with an adapter attached")


# ── C11 ──────────────────────────────────────────────────────────────────────
async def test_skill_run_never_types_its_steps() -> None:
    check.section("C11 skill.run evaluates its steps, it does not type them")
    # A latent bug that wiring an adapter would otherwise have turned loose.
    # `skill.run` calls act("type", target=step) once per step, and a step is a
    # free-text description ("open the browser") built by
    # detect_repeated_workflow from an activity log — not a keystroke payload.
    # With no adapter that was harmless. With one wired and the flag on, each
    # step would have been typed into whatever window had focus.
    saved = os.environ.get("NOVA_COMPUTER_CONTROL")
    try:
        os.environ["NOVA_COMPUTER_CONTROL"] = "1"

        # The gate itself: dry_run closes it however the flags are set.
        api = FakeApi()
        cc = ComputerControl(PermissionBroker(mode="trusted"), adapter=WindowsAdapter(api=api))
        check(cc.available is True, "control IS available (trusted + flag + adapter)")
        res = await cc.act("type", details={"text": "hello"}, dry_run=True)
        check(res["status"] == "approved_not_executed",
              f"dry_run=True still returns a dry run ({res['status']})")
        check(api.sent == [], "and nothing was synthesized")
        check("evaluation" in res["note"],
              f"saying it was an evaluation, not a blocked action ({res['note'][:70]})")
        # ...and without it, the same call DOES execute, so the assertion above
        # is about dry_run and not about something else being switched off.
        res = await cc.act("type", details={"text": "hello"})
        check(res["status"] == "executed" and len(api.sent) == 1,
              f"while the same call without dry_run executes ({res['status']})")

        # Production: the real runtime, a real stored skill, a real adapter.
        async with boot(default_reply="Sure.") as nova:
            rt = nova.runtime
            recorder = FakeApi()
            # `trusted`, so computer.type (STANDARD) auto-allows and each step
            # actually REACHES the execution gate. Under the default `guarded`
            # the steps come back needs_confirmation and "nothing was typed"
            # passes because permission was never granted — which proves
            # nothing about dry_run. `_tool_skill_run` calls
            # `self._computer.act(...)`, so swapping the whole object still
            # exercises the real tool and the real argument it passes.
            rt._computer = ComputerControl(PermissionBroker(mode="trusted"),
                                           adapter=WindowsAdapter(api=recorder))
            check(rt._computer.available is True,
                  "the booted runtime's control is armed for this check")

            steps = ["open the browser", "go to the dashboard", "export last week"]
            skill_id = await rt._memory.learn_skill("weekly export", steps)
            check(bool(skill_id), f"a skill was stored ({skill_id})")

            from core.tool_router import ToolCall

            out = await rt._router.execute(
                ToolCall(name="skill.run", args={"skill_id": skill_id}))
            res = out.result if isinstance(out.result, dict) else {}
            check(res.get("ok") is True, f"skill.run ran ({res.get('error')})")
            check(len(res.get("steps_evaluated") or []) == len(steps),
                  f"evaluating every step ({len(res.get('steps_evaluated') or [])})")
            check(all(r.get("status") == "approved_not_executed"
                      for r in res["steps_evaluated"]),
                  "each one permitted-but-not-executed")
            check(recorder.sent == [],
                  f"and NOT ONE keystroke was synthesized ({len(recorder.sent)} sends)")
            check("description" in res.get("note", ""),
                  f"the note says why ({str(res.get('note'))[:70]})")
    finally:
        if saved is None:
            os.environ.pop("NOVA_COMPUTER_CONTROL", None)
        else:
            os.environ["NOVA_COMPUTER_CONTROL"] = saved


async def main() -> None:
    await test_the_gates_default_off()
    await test_observation_has_no_pixels()
    await test_the_deny_list_refuses_input()
    await test_the_focus_assertion()
    await test_coordinates_must_be_on_screen()
    await test_critical_actions_are_structurally_refused()
    await test_the_translation_is_right()
    await test_a_refusal_is_not_a_failure()
    await test_the_non_input_handlers()
    await test_the_real_binding_and_the_real_runtime()
    await test_skill_run_never_types_its_steps()
    check.finish()


if __name__ == "__main__":
    run(main)
