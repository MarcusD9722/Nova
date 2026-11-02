from __future__ import annotations

"""The Windows platform adapter — the first thing that makes Phase 8 real.

Until now `ComputerControl` was constructed with `adapter=None`, so every
permitted action was an honest dry run. This module is the actuator that was
deliberately missing. It changes nothing about the gate: the broker still
decides, `NOVA_COMPUTER_CONTROL` still has to be on, and this adapter is only
ever reached after both have cleared.

Two layers, on purpose:

  `Win32Api`       the ONLY place a real syscall lives. Thin, no policy.
  `WindowsAdapter` translation + the adapter's own refusals. Takes an injectable
                   `api`, so the test suite exercises every path without
                   synthesizing one real keystroke into the machine running it.

Zero new dependencies — `ctypes` against user32/kernel32. `psutil` is present in
this venv but is NOT pinned in requirements.txt (it arrives transitively), so
process names come from `QueryFullProcessImageNameW` instead of depending on a
package that could vanish on a clean install.

What this adapter deliberately will NOT do
------------------------------------------
* **No screenshots, no pixels, no OCR.** `observe` reports window titles,
  geometry and process names. Reading the screen's CONTENT is a far larger
  privacy surface than enumerating its windows and nothing has asked for it.
* **No CRITICAL actions.** `computer.system_setting` and `computer.delete` raise
  `AdapterRefusal`. The broker already denies CRITICAL in `locked` and `guarded`,
  so only `trusted` mode could ever reach them — and an adapter that physically
  cannot change a system setting or delete a file is strictly safer than one
  that relies on a mode flag to stay harmless. Both were listed in
  `ACTION_CAPABILITY` as *capabilities*; neither was ever a requirement.
* **No forced process kill.** `close_app` posts WM_CLOSE, which an app may
  refuse or answer with a "save your work?" prompt. TerminateProcess would
  discard unsaved work without asking, which is not ours to do.

Input synthesis is blind by nature — it goes to whatever window has focus — so
three guards sit in front of it, and all three fail closed:

  1. a **deny-list** on the foreground window title (`NOVA_COMPUTER_DENY_WINDOWS`)
     covering password managers, terminals and security dialogs. Applied to every
     synthesized event, clicks and scrolls included: clicking "Allow" in a
     security prompt is worse than typing into one;
  2. an optional **focus assertion** — `details["window"]` must appear in the
     foreground title or nothing is sent, so a caller who knows where it is
     aiming can refuse to be mistargeted;
  3. **bounds and size caps** — coordinates must land on the virtual desktop and
     one `type` carries at most `MAX_TYPE_CHARS`, so a runaway loop cannot dump
     a novel into a focused document.

Every executed action reports which window actually received it, so the audit
trail records where the input landed and not merely that it was sent.
"""

import ctypes
import os
from ctypes import wintypes
from typing import Any

from core.computer_control import AdapterRefusal
from core.logging_setup import get_logger

logger = get_logger(__name__)

#: One `type` action's ceiling. Generous for a form field, far below "a model
#: looped and pasted its context into whatever had focus".
MAX_TYPE_CHARS = 2000

#: Default foreground windows that refuse synthesized input. Substring match,
#: case-insensitive. Deliberately broad: a false refusal costs one explanation,
#: a false accept types a password into a chat window.
DEFAULT_DENY_WINDOWS = (
    "1password", "bitwarden", "keepass", "lastpass", "dashlane", "nordpass",
    "windows security", "user account control", "credential",
    "sign in", "sign-in", "log in", "password",
    "command prompt", "powershell", "windows terminal", "cmd.exe",
    "administrator:",
)

# ── Win32 constants ───────────────────────────────────────────────────────────
_INPUT_MOUSE, _INPUT_KEYBOARD = 0, 1

_MOUSEEVENTF_MOVE = 0x0001
_MOUSEEVENTF_LEFTDOWN = 0x0002
_MOUSEEVENTF_LEFTUP = 0x0004
_MOUSEEVENTF_RIGHTDOWN = 0x0008
_MOUSEEVENTF_RIGHTUP = 0x0010
_MOUSEEVENTF_WHEEL = 0x0800
_MOUSEEVENTF_HWHEEL = 0x01000
_MOUSEEVENTF_VIRTUALDESK = 0x4000
_MOUSEEVENTF_ABSOLUTE = 0x8000
_WHEEL_DELTA = 120

_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_UNICODE = 0x0004

_SM_XVIRTUALSCREEN, _SM_YVIRTUALSCREEN = 76, 77
_SM_CXVIRTUALSCREEN, _SM_CYVIRTUALSCREEN = 78, 79
_SM_CMONITORS = 80

_CF_UNICODETEXT = 13
_GMEM_MOVEABLE = 0x0002
_WM_CLOSE = 0x0010
_SW_SHOWNORMAL = 1
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

#: Named keys for `key` actions. Anything not here falls back to VkKeyScanW, so
#: "ctrl+s" works without enumerating the alphabet.
VK_NAMES = {
    "ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12, "menu": 0x12,
    "win": 0x5B, "super": 0x5B, "cmd": 0x5B,
    "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
    "space": 0x20, "backspace": 0x08, "back": 0x08, "delete": 0x2E, "del": 0x2E,
    "insert": 0x2D, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pgup": 0x21, "pagedown": 0x22, "pgdn": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "capslock": 0x14, "printscreen": 0x2C, "apps": 0x5D,
}
VK_NAMES.update({f"f{i}": 0x6F + i for i in range(1, 25)})

#: Modifiers, in the order they are pressed (and the reverse of their release).
MODIFIERS = ("ctrl", "control", "shift", "alt", "menu", "win", "super", "cmd")

ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def deny_list() -> tuple[str, ...]:
    """Foreground titles that refuse input, from the environment or the default.

    An empty `NOVA_COMPUTER_DENY_WINDOWS` means "use the default", not "deny
    nothing" — an unset variable should never be the thing that widens what
    Nova may type into. Clearing it on purpose takes an explicit `none`.
    """
    raw = os.getenv("NOVA_COMPUTER_DENY_WINDOWS", "").strip()
    if not raw:
        return DEFAULT_DENY_WINDOWS
    if raw.lower() == "none":
        return ()
    return tuple(p.strip().lower() for p in raw.split(",") if p.strip())


class Win32Api:
    """The real syscalls, and nothing else.

    Every method here is one small Win32 operation with no policy attached, so
    that `WindowsAdapter` can be tested against a fake of this class and the
    only untested code is a handful of one-line bindings. The DLLs load in
    `__init__` rather than at import, so importing this module on a machine
    that is not Windows — or in a test that supplies its own `api` — does not
    raise.
    """

    def __init__(self) -> None:
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        u, k = self.user32, self.kernel32

        u.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
        u.SendInput.restype = wintypes.UINT
        u.GetSystemMetrics.argtypes = (ctypes.c_int,)
        u.GetSystemMetrics.restype = ctypes.c_int
        u.GetForegroundWindow.restype = wintypes.HWND
        u.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
        u.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
        u.IsWindowVisible.argtypes = (wintypes.HWND,)
        u.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
        u.VkKeyScanW.argtypes = (wintypes.WCHAR,)
        u.VkKeyScanW.restype = ctypes.c_short
        u.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT,
                                   wintypes.WPARAM, wintypes.LPARAM)
        u.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
        k.QueryFullProcessImageNameW.argtypes = (
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))

    # ── geometry ─────────────────────────────────────────────────────────────
    def screen_bounds(self) -> tuple[int, int, int, int]:
        g = self.user32.GetSystemMetrics
        return (g(_SM_XVIRTUALSCREEN), g(_SM_YVIRTUALSCREEN),
                g(_SM_CXVIRTUALSCREEN), g(_SM_CYVIRTUALSCREEN))

    def monitor_count(self) -> int:
        return int(self.user32.GetSystemMetrics(_SM_CMONITORS))

    def cursor_pos(self) -> tuple[int, int]:
        pt = wintypes.POINT()
        self.user32.GetCursorPos(ctypes.byref(pt))
        return int(pt.x), int(pt.y)

    # ── windows ──────────────────────────────────────────────────────────────
    def _title(self, hwnd: Any) -> str:
        n = int(self.user32.GetWindowTextLengthW(hwnd))
        if n <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(n + 1)
        self.user32.GetWindowTextW(hwnd, buf, n + 1)
        return buf.value

    def _pid(self, hwnd: Any) -> int:
        pid = wintypes.DWORD()
        self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value)

    def _rect(self, hwnd: Any) -> dict[str, int]:
        r = wintypes.RECT()
        if not self.user32.GetWindowRect(hwnd, ctypes.byref(r)):
            return {}
        return {"x": int(r.left), "y": int(r.top),
                "w": int(r.right - r.left), "h": int(r.bottom - r.top)}

    def process_name(self, pid: int) -> str:
        """Image name for a pid, or "" when it cannot be read.

        QUERY_LIMITED_INFORMATION is the least handle that answers this, and an
        elevated process legitimately refuses it — hence "" rather than a raise.
        """
        h = self.kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return ""
        try:
            size = wintypes.DWORD(260)
            buf = ctypes.create_unicode_buffer(size.value)
            if not self.kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return ""
            return os.path.basename(buf.value)
        finally:
            self.kernel32.CloseHandle(h)

    def foreground_window(self) -> dict[str, Any]:
        hwnd = self.user32.GetForegroundWindow()
        if not hwnd:
            return {}
        pid = self._pid(hwnd)
        return {"hwnd": int(hwnd), "title": self._title(hwnd), "pid": pid,
                "process": self.process_name(pid), "rect": self._rect(hwnd)}

    def visible_windows(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def _cb(hwnd, _lparam):
            if not self.user32.IsWindowVisible(hwnd):
                return True
            title = self._title(hwnd)
            if not title:
                return True
            pid = self._pid(hwnd)
            out.append({"hwnd": int(hwnd), "title": title, "pid": pid,
                        "process": self.process_name(pid), "rect": self._rect(hwnd)})
            return True

        self.user32.EnumWindows(proc(_cb), 0)
        return out

    # ── actuation ────────────────────────────────────────────────────────────
    def send(self, events: list[INPUT]) -> int:
        """Inject events. Returns how many the OS accepted.

        A short count is not an exception: the OS silently drops injected input
        for a foreground window on the secure desktop (UAC) or one running
        elevated above us. The caller compares and reports the shortfall rather
        than claiming a keystroke that never arrived.
        """
        if not events:
            return 0
        arr = (INPUT * len(events))(*events)
        return int(self.user32.SendInput(len(events), arr, ctypes.sizeof(INPUT)))

    def vk_for_char(self, ch: str) -> int:
        res = int(self.user32.VkKeyScanW(ch))
        return -1 if res == -1 else res & 0xFF

    def set_clipboard_text(self, text: str) -> None:
        u, k = self.user32, self.kernel32
        if not u.OpenClipboard(None):
            raise OSError("could not open the clipboard")
        try:
            u.EmptyClipboard()
            data = ctypes.create_unicode_buffer(text)
            nbytes = ctypes.sizeof(data)
            handle = k.GlobalAlloc(_GMEM_MOVEABLE, nbytes)
            if not handle:
                raise OSError("clipboard allocation failed")
            ptr = k.GlobalLock(handle)
            if not ptr:
                k.GlobalFree(handle)
                raise OSError("clipboard lock failed")
            try:
                ctypes.memmove(ptr, data, nbytes)
            finally:
                k.GlobalUnlock(handle)
            # Ownership of `handle` transfers to the clipboard on success — it
            # must NOT be freed here. On failure it is still ours.
            if not u.SetClipboardData(_CF_UNICODETEXT, handle):
                k.GlobalFree(handle)
                raise OSError("SetClipboardData failed")
        finally:
            u.CloseClipboard()

    def post_close(self, hwnd: int) -> bool:
        return bool(self.user32.PostMessageW(wintypes.HWND(hwnd), _WM_CLOSE, 0, 0))

    def shell_execute(self, target: str, args: str = "") -> int:
        return int(self.user32.ShellExecuteW(None, "open", target, args or None,
                                            None, _SW_SHOWNORMAL))


def _utf16_units(ch: str) -> tuple[int, ...]:
    """UTF-16 code units for one character.

    `KEYEVENTF_UNICODE` carries a 16-bit `wScan`, so anything above the BMP —
    an emoji, say — has to go as its surrogate pair or it is silently mangled.
    """
    cp = ord(ch)
    if cp <= 0xFFFF:
        return (cp,)
    cp -= 0x10000
    return (0xD800 + (cp >> 10), 0xDC00 + (cp & 0x3FF))


class WindowsAdapter:
    """Translate a permitted action into Win32 input, or refuse it.

    Construct with `api=` to drive a fake: every guard and every translation is
    reachable that way, which is why the test suite can prove the deny-list
    refuses without a password manager open, and prove `type` emits the right
    key events without typing into the window running the tests.
    """

    #: Actions this adapter will not perform at any permission tier. See the
    #: module docstring — the broker denies CRITICAL by default, and this makes
    #: the denial structural rather than a consequence of a mode setting.
    REFUSED_KINDS = {
        "system_setting": "change a Windows system setting",
        "delete": "delete a file or folder",
    }

    def __init__(self, *, api: Any | None = None) -> None:
        self._api = api if api is not None else Win32Api()

    # ── observation ──────────────────────────────────────────────────────────
    def observe(self, what: str = "windows") -> dict[str, Any]:
        """Window titles, geometry, processes and screen metrics. No pixels."""
        what = (what or "windows").strip().lower()

        if what in {"window", "windows"}:
            wins = self._api.visible_windows()
            return {"kind": "windows", "count": len(wins), "windows": wins[:40]}

        if what in {"foreground", "active", "focus", "focused"}:
            return {"kind": "foreground", "window": self._api.foreground_window() or None}

        if what in {"app", "apps", "process", "processes"}:
            seen: dict[str, int] = {}
            for w in self._api.visible_windows():
                name = w.get("process") or "?"
                seen[name] = seen.get(name, 0) + 1
            apps = [{"process": k, "windows": v}
                    for k, v in sorted(seen.items(), key=lambda kv: (-kv[1], kv[0]))]
            return {"kind": "apps", "count": len(apps), "apps": apps}

        if what in {"screen", "display", "displays", "monitors"}:
            x, y, w, h = self._api.screen_bounds()
            cx, cy = self._api.cursor_pos()
            return {"kind": "screen", "virtual": {"x": x, "y": y, "w": w, "h": h},
                    "monitors": self._api.monitor_count(),
                    "cursor": {"x": cx, "y": cy}}

        return {"kind": what, "unsupported": True,
                "supported": ["windows", "foreground", "apps", "screen"],
                "note": f"I don't know how to observe '{what}' — there is no "
                        f"screenshot or screen-content reading in this adapter."}

    # ── guards ───────────────────────────────────────────────────────────────
    def _guard_input(self, details: dict[str, Any]) -> dict[str, Any]:
        """Decide whether synthesized input may be sent right now.

        Returns the foreground window it will land in (so the caller can report
        it), or raises `AdapterRefusal`. Both checks fail closed: an unreadable
        foreground window is refused rather than treated as safe, because
        "I can't tell what has focus" is the worst moment to start typing.
        """
        fg = self._api.foreground_window() or {}
        title = str(fg.get("title") or "")
        if not title:
            raise AdapterRefusal(
                "I can't read which window has focus, so I won't send input into it.")

        low = title.lower()
        for pattern in deny_list():
            if pattern in low:
                raise AdapterRefusal(
                    f"The focused window ({title[:60]!r}) matches the protected "
                    f"pattern {pattern!r}, so I will not synthesize input into it.")

        want = str(details.get("window") or "").strip()
        if want and want.lower() not in low:
            raise AdapterRefusal(
                f"Focus check failed: expected a window matching {want!r} but "
                f"{title[:60]!r} has focus. Nothing was sent.")
        return fg

    def _point(self, target: str, details: dict[str, Any]) -> tuple[int, int]:
        """Resolve a click point, defaulting to where the cursor already is.

        Defaulting to the cursor matters: a click with no coordinates is then a
        click where Marcus last put the pointer, not a click at (0, 0).
        """
        raw_x, raw_y = details.get("x"), details.get("y")
        if raw_x is None and raw_y is None and target:
            parts = [p.strip() for p in str(target).replace(" ", ",").split(",") if p.strip()]
            if len(parts) >= 2:
                raw_x, raw_y = parts[0], parts[1]
        if raw_x is None or raw_y is None:
            return self._api.cursor_pos()
        try:
            x, y = int(float(raw_x)), int(float(raw_y))
        except (TypeError, ValueError) as exc:
            raise AdapterRefusal(f"Coordinates {raw_x!r},{raw_y!r} are not numbers.") from exc

        vx, vy, vw, vh = self._api.screen_bounds()
        if not (vx <= x < vx + vw and vy <= y < vy + vh):
            raise AdapterRefusal(
                f"({x}, {y}) is off the virtual desktop "
                f"({vx},{vy} {vw}x{vh}) — refusing rather than clamping to an edge.")
        return x, y

    # ── event builders ───────────────────────────────────────────────────────
    def _mouse(self, flags: int, *, x: int | None = None, y: int | None = None,
               data: int = 0) -> INPUT:
        ev = INPUT(type=_INPUT_MOUSE)
        if x is not None and y is not None:
            vx, vy, vw, vh = self._api.screen_bounds()
            ev.mi.dx = int(round((x - vx) * 65535 / max(vw - 1, 1)))
            ev.mi.dy = int(round((y - vy) * 65535 / max(vh - 1, 1)))
            flags |= _MOUSEEVENTF_ABSOLUTE | _MOUSEEVENTF_VIRTUALDESK
        ev.mi.mouseData = ctypes.c_uint32(data & 0xFFFFFFFF).value
        ev.mi.dwFlags = flags
        return ev

    def _vk(self, vk: int, *, up: bool = False) -> INPUT:
        ev = INPUT(type=_INPUT_KEYBOARD)
        ev.ki.wVk = vk
        ev.ki.dwFlags = _KEYEVENTF_KEYUP if up else 0
        return ev

    def _unit(self, unit: int, *, up: bool = False) -> INPUT:
        ev = INPUT(type=_INPUT_KEYBOARD)
        ev.ki.wVk = 0
        ev.ki.wScan = unit
        ev.ki.dwFlags = _KEYEVENTF_UNICODE | (_KEYEVENTF_KEYUP if up else 0)
        return ev

    def _text_events(self, text: str) -> list[INPUT]:
        out: list[INPUT] = []
        for ch in text:
            for unit in _utf16_units(ch):
                out.append(self._unit(unit))
                out.append(self._unit(unit, up=True))
        return out

    def _combo_events(self, combo: str) -> list[INPUT]:
        parts = [p for p in str(combo).strip().lower().replace(" ", "").split("+") if p]
        if not parts:
            raise AdapterRefusal("No key given.")
        mods = [p for p in parts[:-1] if p in MODIFIERS]
        unknown = [p for p in parts[:-1] if p not in MODIFIERS]
        if unknown:
            raise AdapterRefusal(
                f"{unknown!r} is not a modifier — write a combo as e.g. 'ctrl+shift+s'.")
        leaf = parts[-1]

        if leaf in VK_NAMES:
            vk = VK_NAMES[leaf]
        elif len(leaf) == 1:
            vk = self._api.vk_for_char(leaf)
            if vk == -1:
                raise AdapterRefusal(
                    f"{leaf!r} has no key on the current keyboard layout.")
        else:
            raise AdapterRefusal(
                f"Unknown key {leaf!r}. Known names include: "
                f"{', '.join(sorted(VK_NAMES)[:10])}")

        mod_vks = [VK_NAMES[m] for m in mods]
        out = [self._vk(v) for v in mod_vks]
        out.append(self._vk(vk))
        out.append(self._vk(vk, up=True))
        out.extend(self._vk(v, up=True) for v in reversed(mod_vks))
        return out

    def _send(self, events: list[INPUT], fg: dict[str, Any],
              **extra: Any) -> dict[str, Any]:
        """Inject and report honestly, including a partial injection."""
        sent = self._api.send(events)
        result: dict[str, Any] = {"events": len(events), "accepted": sent,
                                  "into": {"title": fg.get("title"),
                                           "process": fg.get("process"),
                                           "pid": fg.get("pid")},
                                  **extra}
        if sent != len(events):
            # Elevated or secure-desktop windows silently swallow injected input.
            result["partial"] = True
            result["note"] = (f"Only {sent} of {len(events)} events were accepted — "
                              f"the focused window is likely running elevated above "
                              f"Nova, so the action may have half-landed.")
            logger.warning("computer_input_partial", accepted=sent,
                           expected=len(events), process=str(fg.get("process"))[:60])
        return result

    # ── execution ────────────────────────────────────────────────────────────
    def execute(self, kind: str, target: str = "",
                details: dict[str, Any] | None = None) -> dict[str, Any]:
        """Perform one already-permitted action, or raise `AdapterRefusal`.

        By the time this is called the broker has cleared the action and
        `NOVA_COMPUTER_CONTROL` is on. Everything refused below is refused by
        this adapter on its own account, which is why it raises a distinct
        exception instead of returning something that reads like a result.
        """
        kind = (kind or "").strip().lower()
        details = dict(details or {})

        if kind in self.REFUSED_KINDS:
            raise AdapterRefusal(
                f"This adapter will not {self.REFUSED_KINDS[kind]}. That is a "
                f"deliberate omission, not a missing feature — see "
                f"core/computer_windows.py.")

        handler = getattr(self, f"_do_{kind}", None)
        if handler is None:
            raise AdapterRefusal(f"The Windows adapter has no handler for '{kind}'.")
        return handler(target, details)

    # ── input-synthesizing handlers ──────────────────────────────────────────
    def _do_click(self, target: str, details: dict[str, Any]) -> dict[str, Any]:
        return self._click(target, details, double=False)

    def _do_double_click(self, target: str, details: dict[str, Any]) -> dict[str, Any]:
        return self._click(target, details, double=True)

    def _click(self, target: str, details: dict[str, Any], *, double: bool) -> dict[str, Any]:
        button = str(details.get("button") or "left").strip().lower()
        if button not in {"left", "right"}:
            raise AdapterRefusal(f"Unknown mouse button {button!r} — use 'left' or 'right'.")
        # Resolve the point BEFORE the focus guard: a bad coordinate should be
        # reported as a bad coordinate, not as whatever window happens to be up.
        x, y = self._point(target, details)
        fg = self._guard_input(details)

        down = _MOUSEEVENTF_LEFTDOWN if button == "left" else _MOUSEEVENTF_RIGHTDOWN
        up = _MOUSEEVENTF_LEFTUP if button == "left" else _MOUSEEVENTF_RIGHTUP
        events = [self._mouse(_MOUSEEVENTF_MOVE, x=x, y=y)]
        for _ in range(2 if double else 1):
            events.append(self._mouse(down))
            events.append(self._mouse(up))
        return self._send(events, fg, at={"x": x, "y": y}, button=button,
                          double=double)

    def _do_type(self, target: str, details: dict[str, Any]) -> dict[str, Any]:
        text = details.get("text")
        if text is None:
            text = details.get("step", target)
        text = str(text or "")
        if not text:
            raise AdapterRefusal("Nothing to type.")
        if len(text) > MAX_TYPE_CHARS:
            raise AdapterRefusal(
                f"Refusing to type {len(text)} characters in one action "
                f"(cap is {MAX_TYPE_CHARS}). Split it or write a file instead.")
        fg = self._guard_input(details)
        return self._send(self._text_events(text), fg, chars=len(text))

    def _do_key(self, target: str, details: dict[str, Any]) -> dict[str, Any]:
        combo = str(details.get("key") or details.get("combo") or target or "").strip()
        if not combo:
            raise AdapterRefusal("No key combination given.")
        events = self._combo_events(combo)
        fg = self._guard_input(details)
        return self._send(events, fg, combo=combo)

    def _do_scroll(self, target: str, details: dict[str, Any]) -> dict[str, Any]:
        direction = str(details.get("direction") or target or "down").strip().lower()
        if direction not in {"up", "down", "left", "right"}:
            raise AdapterRefusal(
                f"Unknown scroll direction {direction!r} — use up/down/left/right.")
        try:
            clicks = int(details.get("amount", details.get("clicks", 3)))
        except (TypeError, ValueError) as exc:
            raise AdapterRefusal("Scroll amount must be a whole number.") from exc
        if not 1 <= clicks <= 30:
            raise AdapterRefusal(f"Scroll amount {clicks} is outside 1-30.")

        fg = self._guard_input(details)
        horizontal = direction in {"left", "right"}
        sign = -1 if direction in {"down", "left"} else 1
        flag = _MOUSEEVENTF_HWHEEL if horizontal else _MOUSEEVENTF_WHEEL
        events = [self._mouse(flag, data=sign * _WHEEL_DELTA) for _ in range(clicks)]
        return self._send(events, fg, direction=direction, clicks=clicks)

    def _do_write_clipboard(self, target: str, details: dict[str, Any]) -> dict[str, Any]:
        text = details.get("text")
        if text is None:
            text = target
        text = str(text or "")
        if not text:
            raise AdapterRefusal("Nothing to put on the clipboard.")
        # The clipboard is global state, not input into a window, so the focus
        # guard does not apply — but the deny-list does: a password manager in
        # front is a strong signal that whatever is being copied is not ours.
        fg = self._api.foreground_window() or {}
        low = str(fg.get("title") or "").lower()
        for pattern in deny_list():
            if pattern in low:
                raise AdapterRefusal(
                    f"{str(fg.get('title'))[:60]!r} is in front (matches "
                    f"{pattern!r}) — I will not overwrite the clipboard now.")
        self._api.set_clipboard_text(text)
        return {"clipboard_chars": len(text)}

    # ── non-input handlers ───────────────────────────────────────────────────
    def _do_launch_app(self, target: str, details: dict[str, Any]) -> dict[str, Any]:
        app = str(details.get("app") or target or "").strip().strip('"')
        if not app:
            raise AdapterRefusal("No application given.")
        args = str(details.get("args") or "")
        # ShellExecuteW takes a verb and a file, not a command line, so a shell
        # metacharacter here means the caller thinks it is writing shell — it
        # would not chain, it would simply fail confusingly.
        bad = [c for c in "&|;\n\r<>" if c in app or c in args]
        if bad:
            raise AdapterRefusal(
                f"{''.join(bad)!r} is not usable here — launch_app opens one "
                f"program, it is not a shell command line.")
        rc = self._api.shell_execute(app, args)
        # ShellExecuteW returns >32 on success; anything else is an error code.
        if rc <= 32:
            raise AdapterRefusal(f"Windows refused to launch {app!r} (code {rc}).")
        return {"launched": app, "args": args or None, "code": rc}

    def _do_close_app(self, target: str, details: dict[str, Any]) -> dict[str, Any]:
        want = str(details.get("window") or details.get("app") or target or "").strip()
        if not want:
            raise AdapterRefusal(
                "No window named. Closing every window is not a thing I will "
                "infer from an empty target.")
        low = want.lower()
        for pattern in deny_list():
            if pattern in low:
                raise AdapterRefusal(
                    f"{want!r} matches the protected pattern {pattern!r} — "
                    f"dismissing a window like that is not mine to do.")

        matches = [w for w in self._api.visible_windows()
                   if low in str(w.get("title") or "").lower()]
        if not matches:
            raise AdapterRefusal(f"No visible window matches {want!r}.")
        asked = [w for w in matches if self._api.post_close(int(w["hwnd"]))]
        # WM_CLOSE is a request. The app may show "save your work?" or ignore it
        # outright, so this reports what was ASKED, never what closed.
        return {"asked_to_close": [w.get("title") for w in asked],
                "matched": len(matches),
                "note": "WM_CLOSE was posted — an app may prompt to save or "
                        "decline. Nothing was force-killed."}

    def _do_file_write(self, target: str, details: dict[str, Any]) -> dict[str, Any]:
        from pathlib import Path

        raw = str(details.get("path") or target or "").strip().strip('"')
        if not raw:
            raise AdapterRefusal("No path given.")
        path = Path(raw)
        if not path.is_absolute():
            raise AdapterRefusal(
                f"{raw!r} is relative. Give an absolute path — resolving it "
                f"against whatever the working directory happens to be is how "
                f"files land somewhere nobody meant.")
        content = str(details.get("content") or details.get("text") or "")
        if len(content) > 1_000_000:
            raise AdapterRefusal(f"Refusing to write {len(content)} characters in one action.")
        if path.is_dir():
            raise AdapterRefusal(f"{path} is a directory.")
        if not path.parent.is_dir():
            raise AdapterRefusal(
                f"{path.parent} does not exist. I will not create a directory "
                f"tree as a side effect of writing a file.")
        existed = path.exists()
        if existed and not bool(details.get("overwrite")):
            raise AdapterRefusal(
                f"{path} already exists. Pass overwrite=true to replace it — "
                f"I will not silently destroy a file's contents.")
        path.write_text(content, encoding="utf-8")
        return {"path": str(path), "chars": len(content), "replaced": existed}
