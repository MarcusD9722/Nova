# Computer control — how it is gated, and how to turn it on

Phase 8 shipped a permission gate and, on purpose, no actuator: `ComputerControl`
ran with `adapter=None`, so a permitted action was an honest dry run. The Windows
adapter (`core/computer_windows.py`, 2026-11-02) is that missing actuator.

**A stock boot behaves exactly as it did before.** Both flags default off. The
adapter is attached, so `NOVA_COMPUTER_CONTROL` finally means something, but
until you set it a permitted action still returns `approved_not_executed`.

---

## The two flags, and why there are two

| Flag | Default | What it allows |
|---|---|---|
| `NOVA_COMPUTER_OBSERVE` | `0` | Reading **which windows exist** — titles, geometry, process names. Never pixels. |
| `NOVA_COMPUTER_CONTROL` | `0` | Actually performing a permitted action — clicks, keystrokes, scrolling, launching. |

They are separate because `computer.observe` sits at **READ** tier, and READ is
auto-allowed in *every* policy mode — `locked` included. So without its own
flag, the mere act of installing an adapter would have switched silent window
enumeration on, with no confirmation and no mode that stopped it.

That matters more than it sounds. A window title is rarely neutral: it names the
document you have open, the person you are messaging, the account you are looking
at. Enumerating titles is a weaker capability than typing, but it is not a lesser
*decision*, so it gets its own switch.

Each flag is independently load-bearing, and the suite proves it both ways:
turning on execution does not turn on observation, and turning on observation
does not arm the actuator.

### Turning it on

Both live in `.env`:

```bash
NOVA_COMPUTER_OBSERVE=1
```

```bash
NOVA_COMPUTER_CONTROL=1
```

`NOVA_PERMISSION_MODE` still decides what needs your approval. On the default
`guarded`, STANDARD and ADMIN actions wait for an explicit confirmation and
CRITICAL is denied outright — the flag above does not change that, it only
decides whether an **already-approved** action reaches the machine.

---

## What happens to one action

```
  tool call  →  PermissionBroker  →  NOVA_COMPUTER_CONTROL  →  adapter guards  →  SendInput
                (tier + mode +        (off ⇒ dry run)           (deny-list,
                 your confirmation)                              focus, bounds)
```

Four independent gates. Every one of them can stop the action on its own, and
the result says which did:

| Status | Meaning |
|---|---|
| `denied` | The broker refused — tier plus policy mode. |
| `needs_confirmation` | Waiting on you; carries a `request_id`. |
| `not_approved` | You declined, or the window timed out. |
| `approved_not_executed` | Permitted, but `NOVA_COMPUTER_CONTROL` is off. A dry run. |
| `refused_by_adapter` | The adapter declined on its own account. |
| `executed` | It happened. The result names the window it landed in. |

`refused_by_adapter` is deliberately distinct from `execution_error`. "I will not
do this" and "I tried and it broke" are different answers, and collapsing them
would make a working safety guard look like a bug.

---

## The three guards in front of input synthesis

Synthesized input is blind — it goes to whatever window has focus. All three
guards fail closed.

**1. The foreground deny-list.** If the focused window's title matches a
protected pattern, nothing is sent. The built-in list covers password managers,
terminals and security dialogs; see `DEFAULT_DENY_WINDOWS`.

It applies to **clicks and scrolls as well as keystrokes**, which is the less
obvious half: a stray click landing on "Allow" in a consent dialog is worse than
a stray keystroke landing in one.

Override it with a comma-separated `NOVA_COMPUTER_DENY_WINDOWS`:

```bash
NOVA_COMPUTER_DENY_WINDOWS=my-bank,Payroll,KeePass
```

An **empty or unset** value means "use the built-in list", not "deny nothing" —
an unset variable should never be the thing that widens what Nova may type into.
Disabling the guard takes an explicit `NOVA_COMPUTER_DENY_WINDOWS=none`, and is
not advised.

**2. The focus assertion.** A caller that knows where it is aiming can pass
`details["window"]`; the foreground title must contain it or nothing is sent.
This is how a step refuses to be mistargeted rather than typing into whatever
drifted into focus.

**3. Bounds and caps.** Coordinates must land on the virtual desktop — an
off-screen point is refused, never clamped to an edge. One `type` carries at most
`MAX_TYPE_CHARS` (2,000), so a runaway loop cannot dump a context into a focused
document. A click with no coordinates uses the **current cursor position**, not
`(0, 0)`.

Every executed action reports the window that received it (title, process, pid),
so the audit trail records where input landed and not merely that it was sent.

---

## What this adapter will not do

Not missing features — deliberate omissions, each enforced in code rather than
left to a setting.

- **No screenshots, no pixels, no OCR, no screen-content reading.** `observe`
  supports `windows`, `foreground`, `apps` and `screen`. Anything else returns
  `unsupported`. Reading what is *in* a window is a far larger privacy surface
  than knowing the window is open.
- **No CRITICAL actions.** `computer.system_setting` and `computer.delete` raise
  `AdapterRefusal` at every tier, `trusted` included. The broker already denies
  CRITICAL under `locked` and `guarded`; making the adapter structurally
  incapable means a mode change cannot quietly grant it.
- **No forced process kill.** `close_app` posts `WM_CLOSE`, which an app may
  refuse or answer with "save your work?". It reports what it **asked**, never
  what closed. `TerminateProcess` would discard unsaved work without asking.
- **No directory creation as a side effect.** `file_write` requires an absolute
  path with an existing parent, and refuses to overwrite an existing file unless
  `overwrite: true` is passed explicitly.
- **No shell.** `launch_app` opens one program through `ShellExecuteW` and
  refuses `& | ; < >` and newlines — it is not a command line.

### `skill.run` evaluates; it does not type

A learned skill's steps are free-text descriptions — `detect_repeated_workflow`
builds them from an activity log and `render_steps` only fills in
`{placeholders}`, so a step reads like "open the browser". `skill.run` reports
the per-step permission decision and executes nothing.

That used to be true only by accident: it called `act("type", target=step)`, which
was harmless while no adapter existed but would have typed each description into
the focused window once one was wired. `act` now takes an explicit `dry_run`,
which `skill.run` passes, so the guarantee holds however the flags are set.
Running a learned skill for real needs a step → action translator, which does
not exist yet; guessing at one would be inventing intent.

Voice identity is unchanged by all of this: it supplies identity and context
only, and never bypasses the broker or authorizes an action on its own.

---

## Verifying it

```bash
venv\Scripts\python.exe tests\test_computer_windows_adapter.py
```

The suite drives a fake API, so it synthesizes **no real input** — a suite that
actually typed would be injecting keystrokes into whatever window the runner
left focused. Two sections guard against that fake proving nothing: C9 checks the
real `Win32Api` (struct sizes, resolved signatures, live read-only calls) and C10
boots the real runtime to confirm the adapter is genuinely attached in
production.

```bash
venv\Scripts\python.exe tests\mutants_c_adapter.py
```

20 mutants delete each guard in turn — the observation gate, the deny-list, the
focus assertion, the bounds check, the CRITICAL refusal, the overwrite guard, the
surrogate-pair split, the runtime wiring, `skill.run`'s dry run — and the suite
has to notice each one. All 20 are killed by a named assertion. A guard no
mutant can kill is a guard that is not being tested, whatever the green count
says.

### What was verified live

`SendInput` really reaches the OS: a `capslock` toggle (a key that cannot land in
a text field) was accepted 2/2, observed flipping through `GetKeyState`, and
restored. In the same run, with the foreground window added to the deny-list, the
identical action returned `refused_by_adapter` and `capslock` did not move; and
with a focus assertion naming a window that was not in front, likewise. The
guards are load-bearing in the real path, not only against the fake.

---

## Other platforms

`default_adapter()` returns `None` off Windows, so `NOVA_COMPUTER_CONTROL` stays
a no-op there and actions remain dry runs. The import is lazy and
platform-checked because `core/computer_windows.py` pulls in `ctypes.wintypes`,
which raises outright on Linux and macOS — a module-level import would break
every non-Windows boot.

A macOS or Linux adapter needs only the two methods in
`core/computer_control.py`'s contract (`observe`, `execute`), either of which may
raise `AdapterRefusal`. The guard design above is worth copying; none of it is
Windows-specific except the syscalls.
