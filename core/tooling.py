from __future__ import annotations

import asyncio
import contextlib
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.code_ops import CodeOps
from core.dates import parse_date_range, parse_reminder_time
from core.dev_mode import DevMode, DevModeError, secret_path_reason
from core.file_extract import extract_excerpt as _extract_file_excerpt
from core.permissions import HUMAN_DECISION_TIMEOUT_S
from core.project_manager import ProjectManager
from core.tool_router import (PERMISSION_EXECUTION_ALLOWANCE_S,
                              PERMISSION_TOOL_TIMEOUT_S, ToolRouter)
from core.logging_setup import get_logger
from memory.unifier import MemoryUnifier
from plugins.registry import REGISTRY


logger = get_logger(__name__)

#: The longest a single shell command may run. The router's declared budget for
#: `shell.exec` is built from this plus the approval window, so the two numbers
#: cannot drift into the state where cancellation beats the process's own
#: timeout and nothing kills it.
_SHELL_MAX_TIMEOUT_S = 120.0


def _local_stamp(created_at: Any) -> str:
    """A stored UTC timestamp, shown in Marcus's own time.

    The model was handed raw UTC and read it as wall-clock time — which is how
    a bookkeeping stamp of 17:57 became "your dentist appointment at 5:00 PM"
    (audit 2026-11-02, H6/H7). Anything unparseable is passed through rather
    than guessed at.
    """
    raw = str(created_at or "")
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return raw[:16].replace("T", " ")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime("%Y-%m-%d %H:%M")


async def _kill_tree(proc: Any) -> None:
    """Kill a shell command AND whatever it started.

    `proc.kill()` on Windows kills the `cmd.exe` wrapper and leaves its children
    running, so a cancelled `shell.exec` reported "cancelled" while the command
    it launched carried on (audit 2026-11-02, M3). `taskkill /T` takes the tree.
    """
    pid = getattr(proc, "pid", None)
    if pid and os.name == "nt":
        try:
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/T", "/F", "/PID", str(pid),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await asyncio.wait_for(killer.wait(), timeout=10)
            return
        except Exception as e:  # noqa: BLE001
            logger.warning("taskkill_failed", pid=pid, error=str(e)[:120])
    with contextlib.suppress(Exception):
        proc.kill()


def build_tool_router(*, repo_root: Path, projects_dir: Path, memory: MemoryUnifier) -> ToolRouter:
    """Build Nova's ToolRouter from built-ins + plugins.

    Deterministic wiring only; selection remains LLM-driven in policy/planner.
    """
    allow_shell = (os.getenv("NOVA_ALLOW_SHELL", "1").strip().lower() not in {"0", "false", "no"})
    allow_network_tools = (os.getenv("NOVA_ALLOW_NETWORK_TOOLS", "1").strip().lower() not in {"0", "false", "no"})

    project_manager = ProjectManager(repo_root=repo_root, projects_dir=projects_dir)
    code_ops = CodeOps(repo_root, extra_allowed_roots=[projects_dir])
    # One shared guarded self-editing surface. Nova drives read/propose through
    # the self.* tools; the /dev/* HTTP endpoints reuse this same instance (see
    # backend/app.py::_dev_mode) so proposals stay consistent. Everything here
    # honors NOVA_DEV_MODE and the .env/.git/secrets deny-list in core/dev_mode.
    dev_mode = DevMode(repo_root=repo_root, projects_dir=projects_dir)

    # Load plugins (side-effects: register tools)
    with contextlib.suppress(Exception):
        import plugins.init  # noqa: F401

    # ── The permission gate for actuators (audit 2026-11-02, C1/H10) ─────────
    #
    # core/permissions.py describes itself as the gate EVERY actuator passes,
    # and three of them did not pass it: `shell.exec` ran a command,
    # `code.write` wrote a source file and `discord.send` sent a message with
    # nothing asked and nothing recorded — including in `locked` mode, which is
    # documented as read-only.
    #
    # The broker is owned by RuntimeManager, which is built AFTER the router, so
    # it is attached to the router afterwards (the same way `router.dev_mode`
    # already is). These closures read it at CALL time, by which point boot has
    # finished.

    async def _gate(capability: str, details: dict[str, Any]) -> dict[str, Any] | None:
        """Ask permission. None means proceed; a dict is the refusal to return.

        A MISSING broker fails CLOSED. An actuator with nothing deciding for it
        is precisely what this gate exists to prevent, and treating absence as
        permission is how the hole would come back the first time some caller
        built a router on its own.
        """
        broker = getattr(router, "permission_broker", None)
        if broker is None:
            logger.warning("actuator_without_broker", capability=capability)
            return {"ok": False, "status": "denied", "error": "no_permission_broker",
                    "note": ("Nothing is available to approve that right now, so I "
                             "didn't run it.")}
        decision = await broker.request(capability, details=details)
        verdict = decision.get("decision")
        if verdict == "allowed":
            return None
        if verdict == "denied":
            return {"ok": False, "status": "denied", "capability": capability,
                    "reason": decision.get("reason"),
                    "note": f"{capability} isn't permitted in '{broker.mode}' mode, so I didn't run it."}
        # needs_confirmation: wait on the broker's own window. The tool declares
        # PERMISSION_TOOL_TIMEOUT_S so the agent loop does not amputate it.
        request_id = decision["request_id"]
        if not await broker.await_decision(request_id):
            outcome = broker.settled_as(request_id) or "not_approved"
            return {"ok": False, "status": "not_approved", "outcome": outcome,
                    "capability": capability,
                    "note": ("You didn't approve it (declined or timed out) — "
                             "nothing ran.")}
        return None

    #: Shell commands that only LOOK. First token only, and only when the line
    #: carries no shell metacharacter, so there is nothing chained onto the end.
    #: An allowlist rather than a deny-list on purpose: a command this does not
    #: recognise is treated as capable of changing the machine, which is the
    #: direction whose mistakes are cheap.
    _READ_ONLY_CMDS = frozenset({
        "ls", "dir", "pwd", "cat", "type", "head", "tail", "wc", "findstr",
        "grep", "rg", "tree", "where", "which", "whoami", "hostname", "date",
        "echo", "printenv", "uname", "ver", "systeminfo", "nvidia-smi", "df",
        "du", "free", "ps", "tasklist", "netstat", "stat", "file", "diff", "fc",
        "uniq", "cut", "python", "py", "python3", "pip", "npm", "git",
    })
    #: For commands that both read and write depending on the subcommand, the
    #: subcommands that only READ. Anything else drops to `shell.exec`.
    _VERSION_FLAGS = frozenset({"--version", "-v", "-V", "--help", "-h", "version"})
    _READ_ONLY_SUBCMDS = {
        "git": frozenset({"log", "status", "diff", "show", "branch", "remote",
                          "describe", "rev-parse", "ls-files", "config", "blame",
                          "shortlog", "tag", "stash", "ls-remote", "cat-file"})
                | _VERSION_FLAGS,
        "pip": frozenset({"list", "show", "freeze", "check"}) | _VERSION_FLAGS,
        "npm": frozenset({"ls", "list", "view", "outdated"}) | _VERSION_FLAGS,
    }
    _SHELL_META_RE = re.compile(r"[;&|><`$\n]|\$\(")

    def _shell_capability(cmd: str) -> str:
        """`shell.read` for a command that provably only looks, else `shell.exec`."""
        text = (cmd or "").strip()
        if not text or _SHELL_META_RE.search(text):
            return "shell.exec"
        parts = text.split()
        head = Path(parts[0].strip('"\'')).name.lower()
        head = head[:-4] if head.endswith(".exe") else head
        if head not in _READ_ONLY_CMDS:
            return "shell.exec"
        allowed_subs = _READ_ONLY_SUBCMDS.get(head)
        if allowed_subs is not None:
            sub = parts[1].lower() if len(parts) > 1 else ""
            if sub not in allowed_subs:
                return "shell.exec"
        # `python -c ...` and `pytest` run arbitrary code from somewhere. A
        # version probe does not.
        if head in {"python", "py", "python3"} and not (
                len(parts) == 2 and parts[1] in {"--version", "-V", "-VV"}):
            return "shell.exec"
        return "shell.read"

    def _scope_guarded(name: str, fn: Any) -> Any:
        """Wrap an owner-private plugin so a non-owner is refused BEFORE it runs.

        V3 P5.1d.4. Plugins reach connected accounts belonging to Marcus's
        installation — his Gmail, his primary Calendar, his Discord bot — and
        there is no per-speaker account mapping. Measured on `71fc0eb`: a guest
        and an unknown speaker both received his unread mail, his calendar
        events with locations, his Discord history, and could create a Gmail
        draft and send a Discord message.

        The guard is deliberately the OUTERMOST layer, so a refused call does
        no OAuth token retrieval, no HTTP request, and produces no external
        side effect. Refusing after the token fetch would still have touched
        his credentials.

        This routes DATA. It is not authorisation: PermissionBroker is
        untouched, and a recognised owner gains nothing he did not already have.
        """
        async def guarded(args: dict[str, Any]) -> Any:
            from core.turn_identity import current_identity

            ident = current_identity()
            if not ident.is_owner:
                who = ident.display_name if ident.is_known_other else None
                return {
                    "ok": False, "error": "scoped_unavailable", "scope": "connected_account",
                    "tool": name,
                    "detail": ("That's connected to Marcus's own account, so I can't "
                               "reach it from this conversation."),
                    "speaker": who or "unidentified speaker",
                }
            return await fn(args)

        return guarded

    #: Plugin tools that reach OUT of this machine on Marcus's behalf, and so
    #: need his approval rather than only his data scope. Sending a message as
    #: someone is not something to do unasked (audit 2026-11-02, H10).
    #: Plugins the broker decides before anything leaves the box.
    #:
    #: EMPTY, deliberately. `discord.send` was put here during the audit fixes
    #: and taken back out: in the default `guarded` mode a STANDARD capability
    #: needs a click, so every Discord message Nova sent would wait for one —
    #: a change to what Marcus built, which no finding asked for. What the
    #: findings DID ask for is covered without it: the tool stays owner-scoped
    #: (`data_scope="owner_private"`), and `AgentSupervisor._NEVER_UNATTENDED`
    #: keeps it away from the unattended planner, which is M1.
    #:
    #: The machinery below is kept because it is the right shape for anything
    #: that should ask first; it just has nothing in it today.
    _BROKERED_PLUGINS: dict[str, str] = {}

    def _broker_guarded(name: str, capability: str, fn: Any) -> Any:
        """Wrap a plugin so the broker decides before anything leaves the box."""

        async def _guarded(args: dict[str, Any]) -> dict[str, Any]:
            details = {"tool": name}
            for key in ("to", "channel", "channel_id", "subject"):
                if args.get(key):
                    details[key] = str(args[key])[:120]
            body = str(args.get("content") or args.get("message") or args.get("text") or "")
            if body:
                details["preview"] = body[:160]
            refused = await _gate(capability, details)
            if refused is not None:
                return refused
            return await fn(args)

        return _guarded

    _plugin_specs = REGISTRY.get_tools()

    def _wrap_plugin(name: str, spec: Any) -> Any:
        fn = spec.fn
        cap = _BROKERED_PLUGINS.get(name)
        if cap is not None:
            fn = _broker_guarded(name, cap, fn)
        # Scope stays OUTERMOST: a speaker who may not touch Marcus's accounts
        # is refused before a permission request is even raised, so no approval
        # prompt appears for a call that was never going to run.
        if spec.data_scope == "owner_private":
            fn = _scope_guarded(name, fn)
        return fn

    plugin_tools = {name: _wrap_plugin(name, spec) for name, spec in _plugin_specs.items()}
    plugin_descriptions = {name: spec.description for name, spec in _plugin_specs.items()}

    async def _scaffold_project(args: dict[str, Any]) -> dict[str, Any]:
        name = str(args.get("name") or "").strip()
        path = project_manager.scaffold_project(name)
        return {"project": name, "path": str(path)}

    async def _code_read(args: dict[str, Any]) -> dict[str, Any]:
        path = Path(str(args.get("path") or "")).expanduser()
        # The SAME secret rule dev mode uses (core/dev_mode.secret_path_reason).
        # This tool had none, so it read `.env` — API key and all — into the
        # model's prompt, for any speaker (audit 2026-11-02, H10).
        reason = secret_path_reason(path)
        if reason is not None:
            return {"ok": False, "error": "refused_secret_path", "path": str(path),
                    "note": f"I won't read that: {reason}."}
        content = await code_ops.read_text(path)
        return {"path": str(path), "content": content}

    async def _code_write(args: dict[str, Any]) -> dict[str, Any]:
        path = Path(str(args.get("path") or "")).expanduser()
        content = str(args.get("content") or "")
        reason = secret_path_reason(path)
        if reason is not None:
            return {"ok": False, "error": "refused_secret_path", "path": str(path),
                    "note": f"I won't write that: {reason}."}
        # Writing source outside the propose -> review -> apply flow needs a
        # human. Measured: the free agent loop called this and wrote a file into
        # core/ with no proposal and no approval.
        refused = await _gate("code.write", {"path": str(path),
                                             "bytes": len(content.encode("utf-8"))})
        if refused is not None:
            return refused
        await code_ops.apply_patch_atomic(path, content)
        return {"path": str(path), "bytes": len(content.encode("utf-8"))}

    async def _memory_rebuild(_: dict[str, Any]) -> dict[str, Any]:
        res = await memory.rebuild_semantic_index()
        return {"rebuilt": res}

    def _slug_topic(t: str) -> str:
        t = re.sub(r"[^a-z0-9]+", "_", (t or "").strip().lower()).strip("_")
        return t[:48] or "general"

    # ── Personal-data scope for the DIRECT tool surface (V3 P5.1d.2) ─────────
    #
    # P5.1d/d.1 scoped the paths Nova takes on her own: grounding, semantic
    # search, quick-fact capture, the background extractor. The tools the MODEL
    # calls were still global, so the whole boundary could be stepped around by
    # emitting a tool call — measured: a guest overwrote another speaker's fact,
    # added people and events to Marcus's stores, mutated his relationship
    # graph, and read his private thoughts.
    #
    # These helpers route DATA. They never touch PermissionBroker: a guest may
    # still call every tool, and gets the identical permission decision Marcus
    # gets. What changes is whose data the call can reach.

    def _owner_only(what: str, *, detail: str) -> dict[str, Any] | None:
        """Refuse a Marcus-personal operation for anyone else. None = proceed.

        Used where the underlying store has no per-person ownership yet (the
        `people` table, `events`, the knowledge graph, the digital twin). Those
        are Marcus's, modelled as his, and inventing a parallel store for guests
        inside a corrective patch would be a worse outcome than a clear refusal.
        Fail closed now; scoped support can come later without a migration.
        """
        from core.turn_identity import current_identity

        ident = current_identity()
        if ident.is_owner:
            return None
        who = ident.display_name if ident.is_known_other else None
        return {"ok": False, "error": "scoped_unavailable", "scope": what,
                "detail": detail,
                "speaker": who or "unidentified speaker"}

    async def _memory_remember(args: dict[str, Any]) -> dict[str, Any]:
        from core.turn_identity import current_identity, remap_entity_for

        fact = str(args.get("fact") or args.get("value") or args.get("note") or "").strip()
        topic = _slug_topic(str(args.get("topic") or args.get("attribute") or ""))
        if not fact:
            return {"ok": False, "error": "missing_fact"}

        # AN ATTRIBUTE-SHAPED TOPIC GOES WHERE ATTRIBUTES GO (audit, M18).
        # "remember that I live in Austin" with topic="location" was written to
        # `note.location`, while the extractor writes the same thing to
        # `user.location`. Only the `user` row is a singleton, so it is
        # superseded on a move and the `note` row stays forever: after moving
        # from Dallas to Austin, BOTH were in memory and both came back on
        # recall. One namespace per kind of fact, so supersession can work.
        namespace = "note"
        if topic in MemoryUnifier._SINGLETON_USER_ATTRS:
            namespace = "user"

        # A personal namespace, not a shared one — a guest saying "remember my
        # locker code" was writing into the same bucket as Marcus.
        entity = remap_entity_for(namespace, current_identity())
        if entity is None:
            return {"ok": False, "error": "unverified_speaker", "saved": None,
                    "detail": ("I can't save that to memory when I'm not sure "
                               "who I'm speaking with.")}
        # The user explicitly asked Nova to remember this — record it as stated
        # (#19), the highest-trust provenance, so recall never hedges on it later.
        await memory.add_fact(
            entity=entity, attribute=topic, value=fact[:400], confidence=0.9,
            source="user", verification_status="stated",
        )
        # A note written under the OLD routing for the same attribute would now
        # sit beside the superseding one forever, so it is retired here. Only
        # the speaker's own notes, and only the one attribute.
        superseded = 0
        if namespace == "user":
            stale_note = remap_entity_for("note", current_identity())
            if stale_note:
                try:
                    old = await memory.get_facts(entity=stale_note,
                                                 attribute=topic, limit=25)
                    ids = [str(f.id) for f in old if getattr(f, "id", None)]
                    if ids:
                        superseded = await memory.supersede_facts(
                            old_ids=ids,
                            reason=f"re-routed to {entity}.{topic} (audit M18)")
                except Exception as exc:  # noqa: BLE001
                    logger.warning("stale_note_retire_failed", attribute=topic,
                                   error=str(exc)[:160])
        return {"ok": True, "saved": fact[:400], "topic": topic,
                "stored_as": f"{entity}.{topic}",
                **({"replaced_notes": superseded} if superseded else {})}

    async def _memory_correct(args: dict[str, Any]) -> dict[str, Any]:
        """Correct a remembered fact — scoped to whoever is actually speaking.

        The default entity is `user`, which means Marcus. Once a guest can
        speak, "no, my favourite colour is red" would supersede HIS fact, and
        the model cannot be relied on to pick a safe entity — it does not know
        who is in the room, and asking it to would make a safety boundary
        probabilistic. So it is enforced here, from turn identity (V3 P5.1).

        P5.1 protected only the DEFAULT `user` case. Every other entity string
        went straight through to `correct_fact`, so the guard was one argument
        wide: measured on `d1ec5a9`, Alice calling this with
        `entity="speaker:p-bob"` changed Bob's stored favourite colour from blue
        to red. Routing now goes through `resolve_write_target`, which refuses
        another speaker's namespace outright and nests anything else under the
        current speaker (V3 P5.1d.2).
        """
        from core.turn_identity import current_identity, resolve_write_target

        ident = current_identity()
        requested = str(args.get("entity") or "user").strip()
        entity, why = resolve_write_target(requested, ident)
        if entity is None:
            if why == "other_speaker":
                return {"ok": False, "error": "not_your_memory",
                        "detail": ("That's someone else's memory — I can only "
                                   "correct things about you.")}
            if why == "shared_write_refused":
                return {"ok": False, "error": "shared_memory_readonly",
                        "detail": ("That's shared knowledge rather than a "
                                   "personal fact; I won't rewrite it here.")}
            # Unverified voice: refuse rather than write somewhere wrong.
            # Returning a clear non-persisting result lets Nova say so.
            return {"ok": False, "error": "unverified_speaker",
                    "detail": ("I can't update a personal fact when I'm not "
                               "sure who I'm speaking with.")}
        attribute = str(args.get("attribute") or args.get("topic") or "").strip()
        new_value = str(args.get("value") or args.get("new_value") or args.get("correction") or "").strip()
        old_value = str(args.get("old_value") or "").strip() or None
        if not attribute or not new_value:
            return {"ok": False, "error": "missing_attribute_or_value"}
        return await memory.correct_fact(entity, attribute, new_value, old_value=old_value)

    async def _memory_forget(args: dict[str, Any]) -> dict[str, Any]:
        """Delete something the speaker asks to be forgotten.

        This tool did not exist (audit 2026-11-02, C4). "Please forget my locker
        code" reached `memory.correct`, which needs a replacement value, failed
        with `missing_attribute_or_value` — and the reply said "I've wiped that
        locker code from my memory" anyway. The next turn recited it.

        The result names what was removed, so the reply can state the truth
        instead of asserting it.
        """
        what = str(args.get("what") or args.get("query") or args.get("topic")
                   or args.get("value") or "").strip()
        if not what:
            return {"ok": False, "error": "missing_what",
                    "detail": "Say which thing to forget."}
        dry = bool(args.get("dry_run") or args.get("preview"))
        return await memory.forget(what, dry_run=dry)

    async def _memory_recall(args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query") or args.get("q") or "").strip()
        if not query:
            return {"ok": False, "error": "missing_query"}
        # V3 P5.1d: the tool obeys the same read policy as grounding. A privacy
        # boundary enforced only in the prompt is exactly one tool call wide,
        # and the model can make that call. Measured: an unknown speaker
        # retrieved a private owner fact through this tool.
        #
        # P5.1d.1: that fix over-corrected. It refused an unverified speaker
        # outright, before any search — stricter than the stated policy, which
        # says shared world/system/capability knowledge is readable by anyone.
        # Nova ended up unable to answer "where is the Eiffel Tower" to someone
        # she simply had not met. Generic recall now delegates to the ONE entity
        # filter inside memory.search() rather than keeping a second, divergent
        # copy of the policy here.
        from core.turn_identity import current_identity

        _ident = current_identity()

        # Date-range recall: "what did we talk about last Tuesday" -> pull the
        # actual turns from that day (semantic search alone can't do temporal).
        # Durable conversation history is personal in every case, so this branch
        # keeps an explicit gate: recall_conversation scopes rows to the
        # speaker, and somebody Nova cannot identify has no history at all.
        rng = parse_date_range(query)
        if rng and _ident.is_unverified:
            return {"ok": False, "error": "unverified_speaker", "results": [],
                    "detail": ("I don't recognise who I'm speaking with, so I "
                               "can't look back through our conversations.")}
        if rng:
            since, until = rng
            # LOCAL BOUNDS, UTC COMPARISON. `parse_date_range` works in local
            # wall-clock time, which is right — "yesterday" means Marcus's
            # yesterday — but `turns.created_at` is UTC and the two were compared
            # as strings. At UTC-6 that asked for the wrong 24 hours: "what did
            # we talk about yesterday" found the morning and afternoon, MISSED
            # everything after 6pm, and wrongly included the previous evening
            # (audit 2026-11-02, H7).
            since_utc = since.astimezone(timezone.utc)
            until_utc = until.astimezone(timezone.utc)
            rows = await memory.recall_conversation(
                since_iso=since_utc.isoformat(), until_iso=until_utc.isoformat(), limit=80
            )
            rows.reverse()  # chronological reads better than newest-first
            lines = [f"{r['speaker']} ({_local_stamp(r['created_at'])}): {r['content']}" for r in rows]
            return {
                "ok": True,
                "when": f"{since.date()} to {until.date()}",
                "results": lines or [f"No conversation found between {since.date()} and {until.date()}."],
            }
        # Otherwise semantic search — which now includes indexed conversation
        # turns, so anything said (not just structured facts) is recallable.
        hits = await memory.search(q=query, conversation_id=None, limit=8)
        best_score = max((h.score for h in hits), default=0.0)
        # Facts/people/documents score >=0.80; anything weaker is a fuzzy match
        # (a passing remark, a stale/low-similarity hit) worth hedging on rather
        # than stating as settled fact.
        confidence = "high" if best_score >= 0.80 else "low"
        # #19: a high score isn't enough — if the top hit is an ASSUMPTION Nova
        # inferred (not something stated/observed), hedge anyway. Never present
        # an inference as a settled fact.
        top = max(hits, key=lambda h: h.score, default=None)
        verification = top.provenance.get("verification") if top else None
        if verification in {"inferred", "contradicted"}:
            confidence = "low"
        out: dict[str, Any] = {"ok": True, "results": [h.text for h in hits], "confidence": confidence}
        if verification:
            out["verification"] = verification
        return out

    async def _memory_learn_lesson(args: dict[str, Any]) -> dict[str, Any]:
        lesson = str(args.get("lesson") or args.get("text") or args.get("value") or "").strip()
        topic = str(args.get("topic") or "general").strip()
        if not lesson:
            return {"ok": False, "error": "missing_lesson"}
        # add_lesson is speaker-scoped, and returns without writing for an
        # unverified speaker. Reporting "learned" there would be a lie the model
        # then repeats aloud, so say what actually happened.
        from core.turn_identity import current_identity

        if current_identity().is_unverified:
            return {"ok": False, "error": "unverified_speaker", "learned": None,
                    "detail": ("I can't store that as a standing instruction "
                               "without knowing who I'm speaking with.")}
        await memory.add_lesson(lesson, topic=topic)
        return {"ok": True, "learned": lesson[:200], "topic": topic}

    async def _memory_remember_person(args: dict[str, Any]) -> dict[str, Any]:
        """Save someone the speaker mentions.

        The `people` table and its graph edges are Marcus's social map, so a
        guest does not write there (measured: they could). They get the
        fact-backed representation the canonical hierarchy already provides —
        `speaker:<id>:person:<key>` — which needs no new store and no migration.

        The routing rule lives in `scoped_person_entity` so this tool, the
        recall tool, deterministic age capture and structured date lookup all
        share one implementation.
        """
        from core.turn_identity import scoped_person_entity

        name = str(args.get("name") or "").strip()
        if not name:
            return {"ok": False, "error": "missing_name"}
        # Freeform attributes: relation, how_met, works_at, notes, etc.
        attrs = args.get("attributes")
        if not isinstance(attrs, dict):
            attrs = {k: str(v) for k, v in args.items() if k not in {"name", "attributes"} and v}
        attrs = {str(k): str(v)[:300] for k, v in (attrs or {}).items() if str(v).strip()}

        scope = scoped_person_entity(name)
        if scope.is_global_people:
            await memory.upsert_person(name=name, attributes=attrs)
            return {"ok": True, "person": name, "attributes": attrs}
        if scope.refused:
            return {"ok": False, "error": "unverified_speaker", "person": None,
                    "detail": ("I can't save someone to memory without knowing "
                               "who's telling me about them.")}
        root = scope.entity or ""
        await memory.add_fact(entity=root, attribute="name", value=name[:200],
                              confidence=0.9, source="user",
                              verification_status="stated")
        for k, v in attrs.items():
            await memory.add_fact(entity=root, attribute=_slug_topic(k), value=v,
                                  confidence=0.85, source="user",
                                  verification_status="stated")
        return {"ok": True, "person": name, "attributes": attrs}

    async def _memory_remember_event(args: dict[str, Any]) -> dict[str, Any]:
        note = str(args.get("note") or args.get("event") or args.get("what") or "").strip()
        date = str(args.get("date") or args.get("when") or "").strip()
        if not note:
            return {"ok": False, "error": "missing_note"}
        # The events table IS Marcus's timeline — his trips, appointments,
        # milestones. A guest speaking must not be able to put "surgery on the
        # 4th" onto it, so this fails closed until events carry ownership.
        refused = _owner_only("events", detail=(
            "I only keep the calendar of events for Marcus, so I won't add that "
            "to his timeline."))
        if refused is not None:
            return refused
        await memory.add_event(date=date or "unspecified", note=note[:400])
        return {"ok": True, "event": note[:200], "date": date or "unspecified"}

    async def _memory_recall_person(args: dict[str, Any]) -> dict[str, Any]:
        from core.turn_identity import scoped_person_entity

        name = str(args.get("name") or "").strip()
        if not name:
            return {"ok": False, "error": "missing_name"}
        scope = scoped_person_entity(name)
        if scope.is_global_people:
            person = await memory.recall_person(name)
            if person is None:
                return {"ok": False, "error": "not_found", "name": name}
            return {"ok": True, **person}
        if scope.refused:
            return {"ok": False, "error": "unverified_speaker", "name": name,
                    "detail": ("I don't know who I'm speaking with, so I can't "
                               "look anyone up.")}
        # A known guest reads only the people THEY told Nova about.
        rows = await memory.get_facts(entity=scope.entity or "", limit=40)
        if not rows:
            return {"ok": False, "error": "not_found", "name": name}
        return {"ok": True, "name": name,
                "attributes": {r.attribute: r.value for r in rows if r.attribute != "name"}}

    async def _memory_related(args: dict[str, Any]) -> dict[str, Any]:
        key = str(args.get("name") or args.get("key") or args.get("topic") or "").strip()
        if not key:
            return {"ok": False, "error": "missing_name"}
        # P5.1d made automatic edge extraction owner-only because the graph is
        # Marcus's social map. The direct tools have to agree, or the same data
        # is one tool call away.
        refused = _owner_only("graph", detail=(
            "The relationship map I keep is Marcus's, so I can't look through "
            "it for someone else."))
        if refused is not None:
            return refused
        result = await memory.related(key)
        if not result["neighbors"] and not result["two_hop"]:
            return {"ok": False, "error": "no_connections_recorded", "key": key,
                    "note": "Nothing linked to this in the knowledge graph yet — connections build up as things get mentioned together."}
        return {"ok": True, **result}

    async def _memory_timeline(args: dict[str, Any]) -> dict[str, Any]:
        about = str(args.get("about") or args.get("topic") or "").strip() or None
        try:
            days = max(1, min(int(args.get("days") or 14), 120))
        except (TypeError, ValueError):
            days = 14
        # A timeline aggregates events, conversation digests, fired reminders
        # and more. Scoping one of those sources would not make the composite
        # safe, and a partially-scoped history is worse than none — so it fails
        # closed for anyone but the owner until every source carries ownership.
        refused = _owner_only("timeline", detail=(
            "The history I keep — events, past conversations, reminders — is "
            "Marcus's, so I can't lay it out for someone else."))
        if refused is not None:
            return refused
        entries = await memory.timeline(about=about, days=days)
        if not entries:
            return {"ok": True, "entries": [], "note": f"Nothing recorded in the last {days} day(s)" + (f" about '{about}'" if about else "") + "."}
        return {"ok": True, "days": days, "about": about, "entries": entries}

    async def _memory_path(args: dict[str, Any]) -> dict[str, Any]:
        a = str(args.get("from") or args.get("a") or args.get("source") or "").strip()
        b = str(args.get("to") or args.get("b") or args.get("target") or "").strip()
        if not a or not b:
            return {"ok": False, "error": "missing_endpoints", "note": "need both 'from' and 'to'"}
        refused = _owner_only("graph", detail=(
            "The relationship map I keep is Marcus's, so I can't trace "
            "connections through it for someone else."))
        if refused is not None:
            return refused
        path = await memory.graph_path(a, b)
        if not path:
            return {"ok": True, "connected": False, "from": a, "to": b,
                    "note": f"No recorded connection between '{a}' and '{b}' in the knowledge graph."}
        hops = [{"key": h["key"], "via": h.get("predicate")} for h in path]
        return {"ok": True, "connected": True, "from": a, "to": b, "steps": len(path) - 1, "path": hops}

    async def _memory_link(args: dict[str, Any]) -> dict[str, Any]:
        src = str(args.get("from") or args.get("a") or args.get("source") or "").strip()
        dst = str(args.get("to") or args.get("b") or args.get("target") or "").strip()
        predicate = str(args.get("predicate") or args.get("relation") or args.get("as") or "related_to").strip()
        src_kind = str(args.get("from_kind") or args.get("source_kind") or "topic").strip()
        dst_kind = str(args.get("to_kind") or args.get("target_kind") or "topic").strip()
        if not src or not dst:
            return {"ok": False, "error": "missing_endpoints", "note": "need both 'from' and 'to'"}
        refused = _owner_only("graph", detail=(
            "The relationship map I keep is Marcus's, so I won't add "
            "connections to it from this conversation."))
        if refused is not None:
            return refused
        ok = await memory.link(src_kind, src, predicate, dst_kind, dst)
        if not ok:
            return {"ok": False, "error": "invalid_edge"}
        return {"ok": True, "linked": f"{src} —{predicate}→ {dst}"}

    async def _world_recall(args: dict[str, Any]) -> dict[str, Any]:
        subject = str(args.get("subject") or args.get("topic") or args.get("about") or args.get("query") or "").strip()
        if not subject:
            return {"ok": False, "error": "missing_subject"}
        result = await memory.world_recall(subject)
        if not result.get("enabled"):
            return {"ok": False, "error": "world_model_disabled"}
        if not result["facts"]:
            return {"ok": True, "subject": subject, "known": False,
                    "note": f"Nothing recorded about '{subject}' in the world model yet — search the web, then save what you learn with world.learn."}
        return {"ok": True, "subject": subject, "known": True, "fresh": result["fresh"], "facts": result["facts"]}

    async def _world_learn(args: dict[str, Any]) -> dict[str, Any]:
        subject = str(args.get("subject") or args.get("topic") or "").strip()
        predicate = str(args.get("predicate") or args.get("relation") or "is").strip()
        obj = str(args.get("object") or args.get("value") or args.get("fact") or "").strip()
        source = str(args.get("source") or args.get("url") or "").strip()
        if not subject or not obj:
            return {"ok": False, "error": "missing_subject_or_object"}
        if not source:
            return {"ok": False, "error": "missing_source",
                    "note": "World facts require a source (a URL or where you learned it) — never store general knowledge unsourced."}
        ok = await memory.world_learn(subject, predicate, obj, source=source)
        if not ok:
            return {"ok": False, "error": "not_stored"}
        return {"ok": True, "learned": f"{subject} {predicate} {obj}", "source": source}

    async def _thoughts_note(args: dict[str, Any]) -> dict[str, Any]:
        content = str(args.get("content") or args.get("thought") or args.get("text") or "").strip()
        kind = str(args.get("kind") or "note").strip()
        topic = str(args.get("topic") or "general").strip()
        if not content:
            return {"ok": False, "error": "missing_content"}
        # The ThoughtStore is Nova's private notebook about Marcus. P5.1d.2
        # made thoughts.recall owner-only but left this writer open, so a
        # guest's text still landed in the store he later reads back
        # (measured). A one-sided gate is not a gate.
        refused = _owner_only("thoughts", detail=(
            "Those notes are my own thinking about Marcus and his work, so I keep them to that."))
        if refused is not None:
            return refused
        tid = await memory.note_thought(kind, content, topic=topic)
        if not tid:
            return {"ok": False, "error": "thoughts_disabled"}
        return {"ok": True, "noted": content[:200], "kind": kind}

    async def _thoughts_recall(args: dict[str, Any]) -> dict[str, Any]:
        topic = str(args.get("topic") or args.get("about") or "").strip() or None
        kind = str(args.get("kind") or "").strip() or None
        # Nova's private working notes are about Marcus and his life — measured
        # returning "I worry about the deadline" verbatim to an unknown speaker.
        refused = _owner_only("thoughts", detail=(
            "Those are my own notes about Marcus, so they're not mine to share."))
        if refused is not None:
            return refused
        thoughts = await memory.recall_thoughts(topic=topic, kind=kind)
        if not thoughts:
            return {"ok": True, "thoughts": [], "note": "No open internal thoughts recorded" + (f" about '{topic}'" if topic else "") + "."}
        return {"ok": True, "thoughts": [{"kind": t["kind"], "topic": t["topic"], "content": t["content"]} for t in thoughts]}

    async def _twin_profile(args: dict[str, Any]) -> dict[str, Any]:
        # Marcus's work hours, focus periods and routine — a behavioural
        # profile of one specific person, built from his activity.
        refused = _owner_only("twin", detail=(
            "That's Marcus's working-pattern profile, so it isn't something I "
            "can hand to someone else."))
        if refused is not None:
            return refused
        profile = await memory.digital_twin_profile()
        if not profile.get("enabled", True):
            return {"ok": False, "error": "digital_twin_disabled"}
        if not profile.get("enough_data"):
            return {"ok": True, "enough_data": False, "note": profile.get("note", "Still learning your patterns.")}
        return {"ok": True, **profile}

    async def _executive_brief(args: dict[str, Any]) -> dict[str, Any]:
        # Looming deadlines, stalled goals, timing nudges — all Marcus's.
        refused = _owner_only("executive_brief", detail=(
            "That briefing is about Marcus's deadlines and goals, so it's not "
            "something I can go through with someone else."))
        if refused is not None:
            return refused
        recs = await memory.executive_recommendations(throttle=False)
        if not recs:
            return {"ok": True, "recommendations": [], "note": "Nothing worth flagging right now — no looming deadlines, stalled goals, or timing nudges."}
        return {"ok": True, "recommendations": [
            {"message": r["message"], "why": r["rationale"], "confidence": r["confidence"]} for r in recs
        ]}

    async def _plan_save(args: dict[str, Any]) -> dict[str, Any]:
        from core.goal_planner import build_plan, progress
        goal_id = str(args.get("goal_id") or args.get("goal") or args.get("id") or "").strip()
        vision = str(args.get("vision") or args.get("objective") or "").strip()
        if not goal_id or not vision:
            return {"ok": False, "error": "missing_goal_id_or_vision"}
        milestones = args.get("milestones") if isinstance(args.get("milestones"), list) else []
        items = args.get("items") if isinstance(args.get("items"), list) else []
        try:
            horizon = int(args.get("horizon_days") or 90)
        except (TypeError, ValueError):
            horizon = 90
        # One global plan store, no ownership column. Measured: a guest
        # overwrote the owner's saved plan outright.
        refused = _owner_only("plans", detail=(
            "Long-term plans here are Marcus\u2019s, so I won\u2019t write one from this conversation."))
        if refused is not None:
            return refused
        plan = build_plan(vision, horizon_days=horizon, milestones=milestones, items=items)
        await memory.save_plan(goal_id, plan)
        return {"ok": True, "goal_id": goal_id, "milestones": len(plan["milestones"]),
                "items": len(plan["items"]), "progress": progress(plan)}

    async def _plan_status(args: dict[str, Any]) -> dict[str, Any]:
        from core.goal_planner import progress
        goal_id = str(args.get("goal_id") or args.get("goal") or args.get("id") or "").strip()
        if not goal_id:
            return {"ok": False, "error": "missing_goal_id"}
        refused = _owner_only("plans", detail=(
            "That plan is Marcus\u2019s, so it isn\u2019t mine to walk through with someone else."))
        if refused is not None:
            return refused
        plan = await memory.load_plan(goal_id)
        if plan is None:
            return {"ok": False, "error": "no_plan", "note": f"No plan saved for goal '{goal_id}' yet."}
        return {"ok": True, "goal_id": goal_id, "vision": plan.get("vision"),
                "progress": progress(plan), "milestones": plan.get("milestones", []), "items": plan.get("items", [])}

    async def _plan_advance(args: dict[str, Any]) -> dict[str, Any]:
        goal_id = str(args.get("goal_id") or args.get("goal") or args.get("id") or "").strip()
        if not goal_id:
            return {"ok": False, "error": "missing_goal_id"}
        refused = _owner_only("plans", detail=(
            "That plan is Marcus\u2019s, so I won\u2019t move it forward from here."))
        if refused is not None:
            return refused
        summary = await memory.advance_plan(goal_id)
        if summary is None:
            return {"ok": False, "error": "no_plan"}
        return {"ok": True, "goal_id": goal_id, **summary}

    async def _research_track(args: dict[str, Any]) -> dict[str, Any]:
        topic = str(args.get("topic") or args.get("subject") or "").strip()
        if not topic:
            return {"ok": False, "error": "missing_topic"}
        # The tracking REGISTRY is what Marcus asked Nova to follow - his
        # workflow state. The findings it produces are sourced world facts
        # and stay shared; see research.findings.
        refused = _owner_only("research_registry", detail=(
            "The list of topics I follow is Marcus\u2019s, so I won\u2019t add to it from this conversation."))
        if refused is not None:
            return refused
        if await memory.track_research_topic(topic):
            return {"ok": True, "tracking": topic,
                    "note": "I'll keep an eye on this and save findings (with sources) as I learn them. Enable NOVA_RESEARCH for automatic periodic updates."}
        return {"ok": False, "error": "invalid_topic"}

    async def _research_list(args: dict[str, Any]) -> dict[str, Any]:
        refused = _owner_only("research_registry", detail=(
            "The topics I keep an eye on are Marcus\u2019s, so that list isn\u2019t mine to share."))
        if refused is not None:
            return refused
        topics = await memory.list_research_topics()
        return {"ok": True, "topics": topics}

    async def _research_findings(args: dict[str, Any]) -> dict[str, Any]:
        topic = str(args.get("topic") or args.get("subject") or "").strip()
        if not topic:
            return {"ok": False, "error": "missing_topic"}
        findings = await memory.research_findings(topic)
        if not findings:
            return {"ok": True, "topic": topic, "findings": [], "note": f"No findings recorded for '{topic}' yet."}
        return {"ok": True, "topic": topic, "findings": [
            {"summary": f["object"], "source": f.get("source"), "confidence": f.get("confidence")} for f in findings
        ]}

    async def _agents_roster(args: dict[str, Any]) -> dict[str, Any]:
        from core.orchestrator.society import roster as _roster
        out = []
        for spec in _roster():
            state = await memory.agent_state(spec["id"])
            out.append({**spec, "consulted": state["consulted"], "confidence": state["confidence"],
                        "experience": state["experience"]})
        return {"ok": True, "specialists": out}

    async def _agent_recall(args: dict[str, Any]) -> dict[str, Any]:
        agent_id = str(args.get("agent_id") or args.get("id") or args.get("specialist") or "").strip()
        topic = str(args.get("topic") or "").strip() or None
        if not agent_id:
            return {"ok": False, "error": "missing_agent_id"}
        # Classified owner-private from evidence, not from the word 'agent':
        # agent_remember has no production caller, and the only note ever
        # written through it - in tests/test_society_p6.py - is
        # 'Marcus prefers primary sources over blog posts.' The store's
        # designed content is his preferences. agents.roster stays shared:
        # it returns specialist specs and counters, never user content.
        refused = _owner_only("agent_memory", detail=(
            "Those are a specialist\u2019s notes about working with Marcus, so I keep them to that."))
        if refused is not None:
            return refused
        notes = await memory.agent_recall(agent_id, topic=topic)
        return {"ok": True, "agent_id": agent_id, "notes": notes}

    async def _experiment_record(args: dict[str, Any]) -> dict[str, Any]:
        name = str(args.get("name") or args.get("experiment") or "").strip()
        hypothesis = str(args.get("hypothesis") or "").strip()
        if not name:
            return {"ok": False, "error": "missing_name"}
        exp_id = await memory.record_experiment(name, hypothesis)
        if not exp_id:
            return {"ok": False, "error": "experiments_disabled"}
        return {"ok": True, "experiment_id": exp_id}

    async def _experiment_trial(args: dict[str, Any]) -> dict[str, Any]:
        exp_id = str(args.get("experiment_id") or args.get("experiment") or args.get("id") or "").strip()
        variant = str(args.get("variant") or "").strip()
        metrics = args.get("metrics") if isinstance(args.get("metrics"), dict) else {}
        if not exp_id or not variant:
            return {"ok": False, "error": "missing_experiment_or_variant"}
        if await memory.add_experiment_trial(exp_id, variant, metrics):
            return {"ok": True, "recorded": {"variant": variant, "metrics": metrics}}
        return {"ok": False, "error": "not_recorded"}

    async def _experiment_analyze(args: dict[str, Any]) -> dict[str, Any]:
        exp_id = str(args.get("experiment_id") or args.get("experiment") or args.get("id") or "").strip()
        if not exp_id:
            return {"ok": False, "error": "missing_experiment_id"}
        result = await memory.analyze_experiment(exp_id)
        if result is None:
            return {"ok": False, "error": "not_found"}
        # Reinforce the safety contract in the tool response itself.
        return {"ok": True, "experiment_id": exp_id, **result,
                "reminder": "This is a recommendation only — adopting a variant needs your explicit approval; nothing is applied automatically."}

    async def _experiment_list(args: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "experiments": await memory.list_experiments()}

    async def _skill_detect(args: dict[str, Any]) -> dict[str, Any]:
        # detect reads his repeated tool-usage history directly.
        refused = _owner_only("skills", detail=(
            "Learned workflows come from Marcus\u2019s own repeated work, so that\u2019s his to look at."))
        if refused is not None:
            return refused
        candidate = await memory.detect_learnable_workflow()
        if not candidate:
            return {"ok": True, "found": False, "note": "No repeated workflow detected yet — patterns build up as you work."}
        return {"ok": True, "found": True, "workflow": candidate,
                "suggestion": f"I noticed you've repeated this {candidate['occurrences']} times. Want me to learn it as a skill?"}

    async def _skill_learn(args: dict[str, Any]) -> dict[str, Any]:
        name = str(args.get("name") or "").strip()
        steps = args.get("steps") if isinstance(args.get("steps"), list) else []
        if not name or not steps:
            return {"ok": False, "error": "missing_name_or_steps"}
        refused = _owner_only("skills", detail=(
            "The skills I\u2019ve learned are Marcus\u2019s workflows, so I won\u2019t add to them from here."))
        if refused is not None:
            return refused
        skill_id = await memory.learn_skill(name, [str(s) for s in steps])
        if not skill_id:
            return {"ok": False, "error": "not_learned"}
        return {"ok": True, "skill_id": skill_id, "learned": name}

    #: Every learned skill is distilled from Marcus's own repeated work, and the
    #: store has no ownership column. The mutators matter most: measured on
    #: `641f499`, a guest updated a skill's steps to "HACKED" and then DELETED
    #: it. A half-scoped guest skill store is explicitly out of scope here.
    _SKILL_SCOPE_DETAIL = ("The skills I’ve learned are Marcus’s own workflows, "
                           "so they aren’t mine to go through or change here.")

    async def _skill_list(args: dict[str, Any]) -> dict[str, Any]:
        refused = _owner_only("skills", detail=_SKILL_SCOPE_DETAIL)
        if refused is not None:
            return refused
        return {"ok": True, "skills": await memory.list_skills()}

    async def _skill_get(args: dict[str, Any]) -> dict[str, Any]:
        refused = _owner_only("skills", detail=_SKILL_SCOPE_DETAIL)
        if refused is not None:
            return refused
        skill = await memory.get_skill(str(args.get("skill_id") or args.get("id") or "").strip())
        if skill is None:
            return {"ok": False, "error": "not_found"}
        return {"ok": True, **skill}

    async def _skill_update(args: dict[str, Any]) -> dict[str, Any]:
        skill_id = str(args.get("skill_id") or args.get("id") or "").strip()
        steps = args.get("steps") if isinstance(args.get("steps"), list) else []
        refused = _owner_only("skills", detail=_SKILL_SCOPE_DETAIL)
        if refused is not None:
            return refused
        updated = await memory.update_skill(skill_id, [str(s) for s in steps])
        if updated is None:
            return {"ok": False, "error": "not_found_or_empty"}
        return {"ok": True, "skill_id": skill_id, "version": updated["version"]}

    async def _skill_branch(args: dict[str, Any]) -> dict[str, Any]:
        skill_id = str(args.get("skill_id") or args.get("id") or "").strip()
        new_name = str(args.get("new_name") or args.get("name") or "").strip()
        if not new_name:
            return {"ok": False, "error": "missing_new_name"}
        refused = _owner_only("skills", detail=_SKILL_SCOPE_DETAIL)
        if refused is not None:
            return refused
        new_id = await memory.branch_skill(skill_id, new_name)
        if not new_id:
            return {"ok": False, "error": "not_found"}
        return {"ok": True, "new_skill_id": new_id}

    async def _skill_delete(args: dict[str, Any]) -> dict[str, Any]:
        skill_id = str(args.get("skill_id") or args.get("id") or "").strip()
        refused = _owner_only("skills", detail=_SKILL_SCOPE_DETAIL)
        if refused is not None:
            return refused
        return {"ok": await memory.delete_skill(skill_id), "skill_id": skill_id}

    _INDEX_SKIP_DIR_NAMES = {
        "__pycache__", "node_modules", ".git", ".venv", "venv",
        "$recycle.bin", "system volume information",
    }
    _INDEX_SYSTEM_PREFIXES = ("c:\\windows", "c:\\program files", "c:\\programdata")
    _INDEX_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic", ".tiff"}

    async def _memory_index_folder(args: dict[str, Any]) -> dict[str, Any]:
        """Index a folder's files/photos into semantic memory so they become
        recallable ('where's that PDF about the mortgage'). Bounded per call;
        skips files already indexed and unchanged. Photos are indexed by
        filename/folder/date (fast) rather than a per-image vision call, which
        would make indexing a large photo folder take many minutes."""
        raw_path = str(args.get("path") or args.get("folder") or "").strip()
        if not raw_path:
            return {"ok": False, "error": "missing_path"}
        try:
            folder = Path(raw_path).expanduser().resolve()
        except Exception:
            return {"ok": False, "error": "invalid_path"}
        if not folder.exists() or not folder.is_dir():
            return {"ok": False, "error": "not_a_directory", "path": str(folder)}
        if str(folder).lower().startswith(_INDEX_SYSTEM_PREFIXES):
            return {"ok": False, "error": "refusing_to_index_system_directory"}

        # Indexed documents become part of the filesystem memory that semantic
        # search treats as Marcus's, and the document store has no ownership
        # column. Measured: a guest's folder was indexed straight into it.
        refused = _owner_only("documents", detail=(
            "The files I’ve indexed are Marcus’s, so I won’t add a folder to "
            "that index from this conversation."))
        if refused is not None:
            return refused

        max_files = max(1, min(int(args.get("max_files") or 200), 500))
        scanned = indexed = skipped_unchanged = failed = 0
        errors: list[str] = []

        # THE CAP COUNTS WORK, NOT FILES LOOKED AT (audit, M4). It used to
        # count every file scanned, including the ones already indexed and
        # unchanged — so a 25-file folder with a cap of 10 indexed 10, then
        # spent calls 2, 3 and 4 re-scanning the same first 10, skipping them
        # as unchanged, hitting the cap and reporting "call again to continue".
        # It never progressed past file 10, and the note said it would.
        #
        # `attempted` is what the cap bounds: a file that was indexed, failed,
        # or could not be stat'ed. Skipping an unchanged file is free, so it
        # does not consume the budget and the next call reaches new files.
        attempted = 0
        for p in folder.rglob("*"):
            if attempted >= max_files:
                break
            if not p.is_file():
                continue
            if any(part.lower() in _INDEX_SKIP_DIR_NAMES for part in p.parts):
                continue
            scanned += 1
            try:
                stat = p.stat()
            except Exception:
                failed += 1
                attempted += 1
                continue

            if not await memory.document_needs_indexing(str(p), stat.st_mtime):
                skipped_unchanged += 1
                continue
            attempted += 1

            suffix = p.suffix.lower()
            if suffix in _INDEX_IMAGE_SUFFIXES:
                mod_date = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d")
                excerpt = f"Photo: {p.name} in folder '{p.parent.name}'. Modified {mod_date}."
            else:
                excerpt, err = _extract_file_excerpt(p)
                if excerpt is None:
                    failed += 1
                    if len(errors) < 10:
                        errors.append(f"{p.name}: {err}")
                    continue

            await memory.index_document(path=str(p), excerpt=excerpt, mtime=stat.st_mtime)
            indexed += 1

        # "More remaining" means the cap stopped it mid-folder. A run that
        # reached the end has nothing left, however many files it skipped.
        more = attempted >= max_files
        return {
            "ok": True, "folder": str(folder), "scanned": scanned, "indexed": indexed,
            "skipped_unchanged": skipped_unchanged, "failed": failed,
            "more_files_remaining": more,
            "note": ("Reached the per-call file cap — call memory.index_folder again on the same "
                     "folder to continue, or use goal.create to track indexing a large folder over time.")
                    if more else "",
            "errors": errors,
        }

    async def _reminder_create(args: dict[str, Any]) -> dict[str, Any]:
        """'Remind me at 5pm to X' / 'check on me every morning at 8'."""
        title = str(args.get("title") or args.get("what") or "").strip()
        when_text = str(args.get("when") or args.get("time") or "").strip()
        if not title:
            return {"ok": False, "error": "missing_title"}
        if not when_text:
            return {"ok": False, "error": "missing_when", "note": "Ask the user when — never guess a time."}
        # Reminders fire at Marcus, through his notifications, on his schedule.
        # A guest scheduling one is writing into his day.
        refused = _owner_only("reminders", detail=(
            "Reminders here go to Marcus, so I shouldn't put something on his "
            "schedule from this conversation."))
        if refused is not None:
            return refused
        parsed = parse_reminder_time(when_text)
        if parsed is None:
            return {"ok": False, "error": "could_not_parse_time",
                     "note": f"Could not understand the time '{when_text}'. Ask the user to be more specific (e.g. '5pm', 'tomorrow at 9am', 'every morning at 8')."}
        due_at, recurrence = parsed
        rid = await memory.create_reminder(
            title=title, details=str(args.get("details") or title), due_at_iso=due_at.isoformat(), recurrence=recurrence,
        )
        return {"ok": True, "reminder_id": str(rid), "title": title, "due_at": due_at.isoformat(), "recurrence": recurrence}

    async def _reminder_list(args: dict[str, Any]) -> dict[str, Any]:
        """What reminders are actually set.

        THERE WAS NO WAY TO ASK (audit 2026-11-02, H6). "Do I have any reminders
        coming up?" had no tool to call, so the decision step said, correctly,
        "I don't have access to a tool that can list your existing reminders" —
        and that honest answer was discarded by the reply step, which invented
        "You've got a dentist appointment coming up at 5:00 PM today" out of an
        internal throttle timestamp. A REST endpoint existed; chat could not
        reach it.

        Times come back in Marcus's own timezone, because handing the model a
        UTC stamp is how 17:57 became "5:00 PM".
        """
        refused = _owner_only("reminders", detail=(
            "The reminders here are Marcus's, so I can't list them from this "
            "conversation."))
        if refused is not None:
            return refused
        status = str(args.get("status") or "pending").strip().lower() or "pending"
        try:
            limit = max(1, min(int(args.get("limit") or 20), 50))
        except (TypeError, ValueError):
            limit = 20
        # Fetched WIDE and filtered BEFORE the limit. The limit used to apply
        # first, so Nova's own bookkeeping reminders (birthday heads-ups, habit
        # prompts, the briefing), which sort among his by due time, used up the
        # slots: his were cut off under "These are the only reminders that
        # exist", and with enough of hers the answer was "Nothing is
        # scheduled" while things were (layer 2, 2026-11-06).
        rows = await memory.list_reminders(status=(None if status == "all" else status),
                                           limit=1000)
        items = []
        for r in rows:
            title = str(r.get("title") or "")
            # Nova's own bookkeeping reminders (birthday heads-ups, habit
            # prompts, the morning briefing) are not things Marcus set.
            if title.startswith("__nova_"):
                continue
            items.append({
                "reminder_id": str(r.get("reminder_id") or ""),
                "title": title,
                "when": _local_stamp(r.get("due_at")),
                "recurrence": str(r.get("recurrence") or "none"),
                "status": str(r.get("status") or ""),
            })
        if not items:
            return {"ok": True, "reminders": [], "count": 0,
                    "note": "Nothing is scheduled. Say that plainly — do not "
                            "describe a reminder that is not in this list."}
        total = len(items)
        if total > limit:
            items = items[:limit]
            return {"ok": True, "count": len(items), "total": total, "reminders": items,
                    "note": (f"These are the first {len(items)} of {total} reminders. "
                             "Say that there are more; never present these as all of them.")}
        return {"ok": True, "count": len(items), "reminders": items,
                "note": "These are the only reminders that exist."}

    async def _reminder_cancel(args: dict[str, Any]) -> dict[str, Any]:
        """Cancel one reminder by id. Reports whether it actually existed."""
        refused = _owner_only("reminders", detail=(
            "Those reminders are Marcus's, so I won't cancel one from here."))
        if refused is not None:
            return refused
        rid = str(args.get("reminder_id") or args.get("id") or "").strip()
        if not rid:
            return {"ok": False, "error": "missing_reminder_id",
                    "note": "Call reminder.list first and use the id from it."}
        cancelled = await memory.cancel_reminder(reminder_id=rid)
        if not cancelled:
            return {"ok": False, "error": "no_such_reminder", "reminder_id": rid,
                    "note": "Nothing was cancelled — there is no pending reminder with that id."}
        return {"ok": True, "cancelled": True, "reminder_id": rid}

    _IMAGEGEN_PORT = int(os.getenv("NOVA_IMAGEGEN_PORT", "8801").strip() or "8801")
    _IMAGEGEN_BASE = f"http://127.0.0.1:{_IMAGEGEN_PORT}"

    async def _imagegen_health() -> dict[str, Any] | None:
        """None = service unreachable (not running). A dict = it responded."""
        import httpx
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(3.0)) as client:
                r = await client.get(f"{_IMAGEGEN_BASE}/health")
                r.raise_for_status()
                return r.json()
        except Exception:
            return None

    async def _image_generate(args: dict[str, Any]) -> dict[str, Any]:
        """Generate an image on the second-GPU imagegen service. Honest about
        availability at every step — never fakes success or falls back to the
        main GPU running the LLM."""
        prompt = str(args.get("prompt") or args.get("description") or "").strip()
        if not prompt:
            return {"ok": False, "error": "missing_prompt"}

        health = await _imagegen_health()
        if health is None:
            return {
                "ok": False, "error": "service_not_running",
                "note": ("The image-generation service isn't running. It needs to be started separately "
                         "(see tools/imagegen/README.md) — tell Marcus, don't claim an image was made."),
            }
        if not health.get("gpu_available"):
            return {
                "ok": False, "error": "second_gpu_not_detected",
                "note": ("The second GPU for image generation isn't installed/detected yet. Tell Marcus "
                         "plainly that this needs the second GPU (e.g. his RTX 3080) to be plugged in — "
                         "don't claim an image was made."),
            }

        import httpx
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
                r = await client.post(
                    f"{_IMAGEGEN_BASE}/generate/image",
                    json={"prompt": prompt, "width": int(args.get("width") or 768), "height": int(args.get("height") or 768)},
                )
                r.raise_for_status()
                data = r.json()
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": "generation_failed", "note": str(e)[:300]}

        return {"ok": True, "prompt": prompt, "image_data_url": data.get("image_data_url"),
                "seconds": data.get("seconds"), "note": "Image generated successfully."}

    async def _video_generate(args: dict[str, Any]) -> dict[str, Any]:
        """Video generation is intentionally not built yet (see
        tools/imagegen/README.md) — honest 'not available' response, matching
        the approved phased plan, never a fake success."""
        return {
            "ok": False, "error": "not_implemented",
            "note": "Video generation isn't built yet — only image generation is available so far. Offer an image instead.",
        }

    async def _goal_create(args: dict[str, Any]) -> dict[str, Any]:
        """Track a multi-step, multi-session objective. The background
        AgentSupervisor advances it autonomously (bounded to 24 steps/goal)."""
        title = str(args.get("title") or "").strip()
        objective = str(args.get("objective") or args.get("goal") or title).strip()
        success = str(args.get("success_criteria") or args.get("done_when") or "").strip()
        project = str(args.get("project") or "general").strip() or "general"
        if not objective:
            return {"ok": False, "error": "missing_objective"}
        # A goal is a row in Marcus's goal table AND an enqueued __decide__ task
        # the AgentSupervisor will act on autonomously, across sessions, when
        # nobody is present. Measured: a guest and an unknown speaker each
        # created both. That is owner-state contamination plus unattended work
        # started by someone Nova may not even be able to name.
        refused = _owner_only("goals", detail=(
            "Long-running goals here are Marcus’s, and I work on them in the "
            "background for him, so I won’t start one from this conversation."))
        if refused is not None:
            return refused
        gid = await memory.create_goal(
            project_name=project, title=(title or objective[:60]), objective=objective, success_criteria=success
        )
        # Kick off the supervisor loop for this goal (it claims __decide__ tasks).
        await memory.enqueue_goal_task(goal_id=gid, project_name=project, tool_name="__decide__", args={})
        return {"ok": True, "goal_id": str(gid), "title": title or objective[:60], "project": project,
                "note": "I'll work on this in the background across sessions and report progress."}

    # ── Guarded self-editing (Nova inspecting/improving her OWN code) ──────────

    def _project_arg(args: dict[str, Any]) -> str:
        return str(args.get("project") or "").strip().lower()

    def _self_err(e: Exception, args: dict[str, Any]) -> str:
        """Make path errors self-correcting for the agent loop: a small model
        routinely forgets the 'project' arg, so name the registered projects
        it probably meant instead of leaving a dead-end 'file not found'."""
        err = str(e)
        if not _project_arg(args):
            try:
                roots = dev_mode.list_external_roots()
                if roots:
                    names = ", ".join(r["project"] for r in roots)
                    err += (f" — If this file belongs to one of Marcus's registered external projects, "
                            f"retry the SAME call with project set. Registered projects: {names}.")
            except Exception:
                pass
        return err

    async def _self_list_code(args: dict[str, Any]) -> dict[str, Any]:
        subdir = str(args.get("subdir") or args.get("path") or "").strip()
        try:
            files = await asyncio.to_thread(
                lambda: dev_mode.list_files(subdir, int(args.get("limit") or 400), project=_project_arg(args))
            )
            return {"ok": True, "files": files}
        except DevModeError as e:
            return {"ok": False, "error": _self_err(e, args)}

    async def _self_read_code(args: dict[str, Any]) -> dict[str, Any]:
        path = str(args.get("path") or "").strip()
        if not path:
            return {"ok": False, "error": "missing_path"}
        try:
            result = await asyncio.to_thread(lambda: dev_mode.read_file(path, project=_project_arg(args)))
            return {"ok": True, **result}
        except DevModeError as e:
            return {"ok": False, "error": _self_err(e, args)}

    async def _self_propose_change(args: dict[str, Any]) -> dict[str, Any]:
        path = str(args.get("path") or "").strip()
        new_content = args.get("new_content")
        reason = str(args.get("reason") or "").strip()
        if not path or new_content is None:
            return {"ok": False, "error": "missing_path_or_new_content"}
        try:
            p = await asyncio.to_thread(
                lambda: dev_mode.propose_change(path, str(new_content), reason, "nova", project=_project_arg(args))
            )
            return {
                "ok": True, "proposal_id": p.id, "path": p.path, "status": p.status,
                "note": ("Proposal created for Marcus to review and approve. It is NOT applied "
                         "automatically — nothing changes until he approves it in the UI."),
            }
        except DevModeError as e:
            return {"ok": False, "error": _self_err(e, args)}

    async def _self_register_project(args: dict[str, Any]) -> dict[str, Any]:
        name = str(args.get("name") or "").strip()
        path = str(args.get("path") or args.get("folder") or "").strip()
        if not name or not path:
            return {"ok": False, "error": "missing_name_or_path"}
        try:
            return {"ok": True, **await asyncio.to_thread(dev_mode.register_external_root, name, path)}
        except DevModeError as e:
            return {"ok": False, "error": str(e)}

    # Blocklists are never complete, but the old one missed the obvious Windows
    # and pipe-to-shell bypasses (Remove-Item -Recurse -Force, curl … | sh,
    # iwr … | iex). Since shell.exec is reachable from the autonomous agent loop
    # and prompt-injected content, refuse the destructive/remote-exec families.
    _DANGEROUS_CMD_RE = re.compile(
        r"(?:"
        r"\brm\s+-[rf]{1,2}\b|"                              # rm -rf / -fr
        r"\bremove-item\b(?=.*-(?:recurse|force)\b)|"        # Remove-Item -Recurse/-Force
        r"\b(?:rmdir|rd)\s+/s\b|\bdel\s+/[sfq]\b|"           # rmdir /s, del /s /f /q
        r"\bformat\s+[a-z]:|\bdiskpart\b|\bmkfs\b|\bdd\s+if=|"  # disk wipe
        r"\bshutdown\b|\breboot\b|\bstop-computer\b|\brestart-computer\b|\bhalt\b|"
        r"\breg\s+(?:delete|add)\b|\bbcdedit\b|\bvssadmin\s+delete\b|\bcipher\s+/w\b|"
        r"\bnet\s+user\b|\bnet\s+localgroup\b|\btaskkill\b|"
        r"\|\s*(?:sh|bash|zsh|iex|invoke-expression|python|powershell|cmd)\b|"  # pipe to interpreter
        r"\b(?:invoke-expression|iex)\b|"
        r"\b(?:curl|wget|iwr|invoke-webrequest|downloadstring|bitsadmin)\b.*\|\s*(?:sh|bash|iex|invoke-expression)\b|"
        r":\(\)\s*\{|"                                        # bash fork bomb :(){
        r"\battrib\b.*\bsystem32\b|>\s*/dev/sd"
        r")",
        re.IGNORECASE,
    )

    def _looks_dangerous_cmd(cmd: str) -> bool:
        return bool(_DANGEROUS_CMD_RE.search(cmd or ""))

    async def _shell_exec(args: dict[str, Any]) -> dict[str, Any]:
        if not allow_shell:
            return {"ok": False, "error": "shell_disabled"}
        cmd = str(args.get("cmd") or args.get("command") or "").strip()
        # Capped, and the cap is part of the budget the router declares below.
        # An uncapped inner timeout could outlive the router's window, and the
        # cancellation that followed skipped the kill (audit, M3).
        timeout_s = max(1.0, min(float(args.get("timeout_s") or 45.0), _SHELL_MAX_TIMEOUT_S))
        if not cmd:
            return {"ok": False, "error": "missing_cmd"}
        if _looks_dangerous_cmd(cmd):
            return {"ok": False, "error": "refusing_dangerous_command", "cmd": cmd}

        # Permission, by what the command can DO. A command on the read-only
        # allowlist asks for `shell.read` (allowed in every mode, `locked`
        # included — looking at the machine is reading); anything else asks for
        # `shell.exec`, which needs Marcus in guarded mode and is refused in
        # locked. Before this, `locked` mode ran whatever it was handed.
        capability = _shell_capability(cmd)
        refused = await _gate(capability, {"cmd": cmd[:400]})
        if refused is not None:
            return {**refused, "cmd": cmd, "capability": capability}

        proc = await asyncio.create_subprocess_shell(
            cmd,
            cwd=str(repo_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except TimeoutError:
            await _kill_tree(proc)
            return {"ok": False, "error": "timeout", "cmd": cmd, "timeout_s": timeout_s}
        except asyncio.CancelledError:
            # The router's budget fired, or the turn was abandoned. `except
            # TimeoutError` never saw this, so the process was left running
            # while the tool reported "cancelled" — a command still changing
            # the machine after Nova said it had stopped (audit, M3).
            await _kill_tree(proc)
            raise

        stdout = (out_b or b"").decode("utf-8", errors="ignore")
        stderr = (err_b or b"").decode("utf-8", errors="ignore")
        if len(stdout) > 20000:
            stdout = stdout[:20000] + "\n...[truncated]"
        if len(stderr) > 20000:
            stderr = stderr[:20000] + "\n...[truncated]"

        return {"ok": True, "cmd": cmd, "exit_code": int(proc.returncode or 0), "stdout": stdout, "stderr": stderr}

    tools: dict[str, Any] = {
        "project.scaffold": _scaffold_project,
        "code.read": _code_read,
        "code.write": _code_write,
        "memory.rebuild_index": _memory_rebuild,
        "memory.remember": _memory_remember,
        "memory.recall": _memory_recall,
        "memory.correct": _memory_correct,
        "memory.forget": _memory_forget,
        "memory.learn_lesson": _memory_learn_lesson,
        "memory.remember_person": _memory_remember_person,
        "memory.remember_event": _memory_remember_event,
        "memory.recall_person": _memory_recall_person,
        "memory.related": _memory_related,
        "memory.timeline": _memory_timeline,
        "memory.path": _memory_path,
        "memory.link": _memory_link,
        "world.recall": _world_recall,
        "world.learn": _world_learn,
        "thoughts.note": _thoughts_note,
        "thoughts.recall": _thoughts_recall,
        "twin.profile": _twin_profile,
        "executive.brief": _executive_brief,
        "plan.save": _plan_save,
        "plan.status": _plan_status,
        "plan.advance": _plan_advance,
        "research.track": _research_track,
        "research.list": _research_list,
        "research.findings": _research_findings,
        "agents.roster": _agents_roster,
        "agent.recall": _agent_recall,
        "experiment.record": _experiment_record,
        "experiment.trial": _experiment_trial,
        "experiment.analyze": _experiment_analyze,
        "experiment.list": _experiment_list,
        "skill.detect": _skill_detect,
        "skill.learn": _skill_learn,
        "skill.list": _skill_list,
        "skill.get": _skill_get,
        "skill.update": _skill_update,
        "skill.branch": _skill_branch,
        "skill.delete": _skill_delete,
        "goal.create": _goal_create,
        "reminder.create": _reminder_create,
        "reminder.list": _reminder_list,
        "reminder.cancel": _reminder_cancel,
        "memory.index_folder": _memory_index_folder,
        "image.generate": _image_generate,
        "video.generate": _video_generate,
        "self.list_code": _self_list_code,
        "self.read_code": _self_read_code,
        "self.propose_change": _self_propose_change,
        "self.register_project": _self_register_project,
    }
    descriptions: dict[str, str] = {
        "project.scaffold": "Create an empty project folder. args: {name}",
        "code.read": "Read a text file inside the repo or projects workspace. args: {path}",
        "code.write": "Write a text file inside the repo or projects workspace. args: {path, content}",
        "memory.rebuild_index": "Rebuild the semantic memory index from the primary store. args: {}",
        "memory.remember": "Save a fact or note to permanent long-term memory when the current speaker asks you to remember something. It is filed under whoever is speaking. args: {fact, topic?}",
        "memory.recall": "Search permanent long-term memory for previously saved facts and notes. args: {query}",
        "memory.forget": ("DELETE something the speaker asks you to forget or stop storing (\"forget my locker code\", \"don't keep that\"). Only ever their own memory, and only facts matching every word of what they named. The result lists exactly what was removed: say THAT, and never claim a deletion the result does not show. args: {what, dry_run?}"),
        "memory.correct": ("Fix something you remembered WRONG when the current speaker corrects you ('no, her birthday is the 14th'). This SUPERSEDES the old value instead of storing a second contradictory fact. Corrections apply to the speaker's own memory; you cannot use this to change what is stored about anyone else. args: {attribute, value, entity?, old_value?}"),
        "memory.learn_lesson": ("Save a durable lesson about how the current speaker wants you to behave (a "
                                 "correction or preference) so you apply it in future replies with them. "
                                 "args: {lesson, topic?}"),
        "memory.remember_person": ("Save someone this person mentions (a friend, coworker, family member) so you "
                                    "can recall who they are later. ALWAYS use this (not memory.remember) whenever "
                                    "they give you a birthday or anniversary for someone — put it in attributes as "
                                    "birthday/anniversary (e.g. 'April 12') so you can mention it before it comes "
                                    "up. args: {name, attributes?:{relation, how_met, works_at, birthday, anniversary, notes}}"),
        "memory.remember_event": ("Save a dated life event the speaker mentions (a trip, appointment, "
                                   "milestone). args: {note, date?}"),
        "memory.recall_person": ("Look up everything remembered about a specific person by name — attributes, "
                                  "when they were last mentioned, and any upcoming birthdays/anniversaries. "
                                  "args: {name}"),
        "memory.related": ("How a person/project/topic connects to everything else Nova knows (knowledge-graph "
                            "neighbors, direct and one step out). Use for 'how is X connected to Y' or 'what do "
                            "you associate with X'. args: {name}"),
        "memory.timeline": ("Time-ordered view of what actually happened — events, conversation digests, fired "
                             "reminders, new facts — optionally about one person/project/topic. Use for 'what "
                             "happened this week' / 'catch me up on X'. args: {about?, days?}"),
        "memory.path": ("Find the shortest connection path between two things in the knowledge graph — the "
                         "'how are X and Y related' answer. Returns the chain of hops or reports no connection. "
                         "args: {from, to}"),
        "memory.link": ("Explicitly record a relationship between any two things (people, projects, files, ideas, "
                         "movies, hardware, software, locations, ...). Use when Marcus states a connection worth "
                         "remembering. args: {from, to, predicate, from_kind?, to_kind?}"),
        "world.recall": ("Check Nova's semantic world model for what she already knows about a general topic "
                          "(technologies, companies, concepts — NOT personal facts). ALWAYS try this BEFORE a web "
                          "search; if it returns fresh known facts you can answer without searching. args: {subject}"),
        "world.learn": ("Save a durable GENERAL-knowledge fact to the world model with its source, so you don't "
                         "have to re-search it later. Use after learning something from the web. A source is "
                         "REQUIRED — never store world knowledge unsourced. args: {subject, predicate, object, source}"),
        "thoughts.note": ("Record one of your own private internal thoughts about your work with Marcus, "
                           "to revisit later — an idea, an unresolved question, a potential improvement, an "
                           "interesting discovery, a future plan. This notebook is his; there is no per-guest "
                           "one, so it is unavailable when someone else is speaking. kinds: idea|question|"
                           "unresolved|improvement|discovery|failed_experiment|future_plan. "
                           "args: {content, kind?, topic?}"),
        "thoughts.recall": ("Surface your own internal thoughts — use ONLY when the speaker asks what "
                             "you've been thinking about / pondering / your ideas. Never volunteer these "
                             "unprompted. args: {topic?, kind?}"),
        "twin.profile": ("Get Marcus's working-pattern profile (preferred work hours, peak focus period, "
                          "procrastination likelihood, interests) derived from his recorded activity. Use when he "
                          "asks about his own habits/productivity or when timing a suggestion. Predicts patterns; "
                          "never claims to read his mind. args: {}"),
        "executive.brief": ("Get Nova's current proactive recommendations — looming deadlines, stalled goals, focus "
                             "windows, break suggestions — synthesized from goals/reminders/habits/patterns. Use when "
                             "Marcus asks 'what should I focus on', 'what's on my plate', 'brief me', or you want to "
                             "proactively help him prioritize. Each item includes why + a confidence. args: {}"),
        "plan.save": ("Save a long-term plan for a goal: a vision plus milestones (with target_date + risk) and "
                       "dated action items (cadence once|daily|weekly|monthly). Compose the plan yourself from the "
                       "goal, then persist it here. args: {goal_id, vision, milestones:[{title,target_date,risk}], "
                       "items:[{title,cadence,due,milestone_id?}], horizon_days?}"),
        "plan.status": ("Show a goal's plan and progress (milestones/items done, % complete, at-risk, "
                         "overdue). Plans belong to Marcus \u2014 there is no per-guest plan store, so this "
                         "is unavailable when someone else is speaking. args: {goal_id}"),
        "plan.advance": ("Roll a plan forward: missed recurring items move to their next occurrence instead of "
                          "becoming overdue, and open milestones past their target date are flagged at-risk. Run "
                          "this when catching up on a goal. args: {goal_id}"),
        "research.track": ("Start tracking a research topic Marcus wants Nova to follow over time (AI, GPUs, "
                            "snowboarding, a framework, ...). Findings accrue into the world model with "
                            "sources. The tracking list is Marcus\u2019s own, so this is unavailable when "
                            "someone else is speaking. args: {topic}"),
        "research.list": ("List the research topics Nova is tracking for Marcus and when each was last "
                          "checked. This registry is his; the FINDINGS it produces are sourced public "
                          "facts and stay available to anyone via research.findings. args: {}"),
        "research.findings": ("Show what Nova has learned about a tracked research topic — each finding with its "
                               "source citation. args: {topic}"),
        "agents.roster": ("List Nova's council of specialist agents (Chief Engineer, Research Scientist, "
                           "Psychologist, coaches, ...) with how experienced/confident each has become. Use when "
                           "Marcus asks who's on the team or about a specialist. args: {}"),
        "agent.recall": ("Recall a specific specialist's accumulated notes about working with Marcus \u2014 "
                          "these can hold his preferences and context, so they are his. Use agents.roster "
                          "for the neutral list of specialists. args: {agent_id, topic?}"),
        "experiment.record": ("Start a safe A/B experiment to compare approaches (prompt variants, retrieval, "
                               "ranking, scheduling, ...). args: {name, hypothesis?}"),
        "experiment.trial": ("Log one trial's measured metrics for a variant (accuracy, reliability, latency_s, "
                              "resource). args: {experiment_id, variant, metrics:{...}}"),
        "experiment.analyze": ("Compare an experiment's variants and get a ranked RECOMMENDATION. It never applies a "
                                "change — adopting a variant is always your call. args: {experiment_id}"),
        "experiment.list": ("List recorded experiments and their trial counts. args: {}"),
        "skill.detect": ("Check whether Marcus has repeated a multi-step workflow enough to be worth "
                          "learning, by reading his own tool-usage history. If so, OFFER to learn it "
                          "(never auto-learn). args: {}"),
        "skill.learn": ("Save an approved repeated workflow as a reusable skill (after Marcus says yes). Steps may "
                         "contain {parameters}. args: {name, steps:[...]}"),
        "skill.list": ("List the workflow skills Nova has learned from Marcus\u2019s own repeated work. "
                        "These are his workflows \u2014 there is no per-guest skill store. args: {}"),
        "skill.get": ("Show a learned skill's steps, version, and parameters. args: {skill_id}"),
        "skill.update": ("Edit a skill's steps (bumps its version, keeps history). args: {skill_id, steps:[...]}"),
        "skill.branch": ("Fork a skill into a new variant workflow. args: {skill_id, new_name}"),
        "skill.delete": ("Delete a learned skill. args: {skill_id}"),
        "memory.index_folder": ("Index a folder's files/photos into Marcus\u2019s document memory so he can later "
                                 "ask about them ('where's that PDF about the mortgage', 'photos from the beach "
                                 "trip'). The index is his, so this is unavailable when someone else is "
                                 "speaking. args: {path, max_files?}. Skips files already indexed and unchanged."),
        "image.generate": ("Generate an image from a text description on the local image-generation GPU. "
                           "args: {prompt, width?, height?}. If the service isn't running or the second GPU "
                           "isn't installed yet, this honestly reports that — never claim an image was made "
                           "if the tool result says ok:false."),
        "video.generate": "Not implemented yet — always returns unavailable. Offer image.generate instead.",
        "reminder.list": ("List the reminders that ACTUALLY exist, with their times in Marcus's timezone. Call this before answering anything about upcoming reminders: there was no such tool, and the answer got invented from an internal timestamp. If the list comes back empty, say nothing is scheduled. args: {status?, limit?}"),
        "reminder.cancel": ("Cancel one reminder by its id, from reminder.list. Reports whether a reminder actually existed; never say you cancelled one unless ok is true. args: {reminder_id}"),
        "reminder.create": ("Schedule a reminder or recurring check-in. args: {title, when, details?}. "
                            "'when' examples: '5pm', 'in 20 minutes', 'tomorrow at 9am', 'every morning at 8', "
                            "'every weekday at 7:30am', 'every monday at noon'. If the user didn't give a clear "
                            "time, ask them — never guess one."),
        "goal.create": ("Start tracking a real multi-step objective that spans multiple sessions (e.g. 'help me "
                        "learn Spanish', 'plan the Hawaii trip'). You'll advance it in the background and report "
                        "progress. These are Marcus\u2019s goals and run unattended for him, so this is "
                        "unavailable when someone else is speaking. Use for ongoing goals, NOT one-shot "
                        "tasks. args: {objective, title?, success_criteria?, project?}"),
        "self.list_code": ("List source files — Nova's OWN codebase by default, or one of Marcus's registered "
                            "external projects when 'project' is given. args: {subdir?, limit?, project?}. Requires developer mode."),
        "self.read_code": ("Read a source file — Nova's OWN code by default, or a registered external project's "
                            "file when 'project' is given. args: {path, project?}. Requires developer mode."),
        "self.propose_change": ("Propose a code edit — to Nova's OWN source, or to a registered external project "
                                 "when 'project' is given. Creates a proposal for Marcus to review and approve — it is "
                                 "NOT applied automatically. External-project applies are syntax-checked and reversible "
                                 "but skip Nova's boot test (say so if asked). args: {path, new_content, reason, project?}. "
                                 "Requires developer mode."),
        "self.register_project": ("Register one of Marcus's OTHER project folders (outside Nova's own code) for "
                                   "guarded editing, when he asks you to work on a project at a specific path. After "
                                   "registering, use self.list_code/read_code/propose_change with project=<name>. "
                                   "args: {name, path}. Requires developer mode."),
    }
    if allow_shell:
        tools["shell.exec"] = _shell_exec
        descriptions["shell.exec"] = "Run a shell command in the repo root (guarded). args: {cmd, timeout_s}"
    if allow_network_tools:
        tools.update(plugin_tools)
        descriptions.update(plugin_descriptions)

    router = ToolRouter(tools, descriptions=descriptions)

    # RETRY SAFETY. The router refuses to re-invoke a tool automatically unless it
    # is declared safe, because a timeout says a call did not FINISH — never that
    # it did not happen. `agent_supervisor` and `autonomy_supervisor` run
    # arbitrary tools with `retries=1`, so without this a timed-out
    # `project.delete`, `code.write` or `shell.exec` was simply run a second time.
    #
    # Only READ-ONLY tools go on this list. Anything that writes a file, spends
    # money, sends something, or changes memory is deliberately absent: losing one
    # flaky read costs a retry, and repeating one write can cost the work.
    for _read_only in (
        "memory.recall", "memory.recall_person", "memory.related", "memory.path",
        "memory.timeline", "code.read", "self.read_code", "self.list_code",
        "project.status", "project.trash", "plan.status", "research.list",
        "research.findings", "experiment.list", "experiment.analyze",
        "reminder.list",
        "agents.roster", "agent.recall", "executive.brief", "skill.detect",
        # PLUGIN READS (audit, M2). These were absent, so the FIRST failure
        # fenced the tool for the rest of the turn: one 404 from `web.fetch`
        # meant a second URL was never tried, while the agent prompt told the
        # model "if a tool failed twice, stop" — advice it could not follow
        # because there was never a second attempt. Each of these only reads a
        # public or read-scoped source; nothing here sends, writes or spends.
        # `discord.send` and `email.draft_reply` are deliberately absent.
        "web.search", "web.fetch", "weather.current", "system.time",
        "maps.geocode", "maps.place_search", "maps.places_nearby",
        "maps.directions", "calendar.today", "calendar.upcoming",
        "email.recent", "discord.read",
    ):
        if _read_only in tools:
            router.set_retry_safe(_read_only, True)
    # PERMISSION BUDGETS. These three can block on Marcus clicking Approve, so
    # they declare the approval window plus room to actually do the work. The
    # agent loop's generic 25s would otherwise abandon the call while the button
    # was still live — the defect PERMISSION_TOOL_TIMEOUT_S exists to prevent,
    # which the newly-gated tools would have walked straight back into.
    for _gated, _budget in (
        ("shell.exec", HUMAN_DECISION_TIMEOUT_S + _SHELL_MAX_TIMEOUT_S
                       + PERMISSION_EXECUTION_ALLOWANCE_S),
        ("code.write", PERMISSION_TOOL_TIMEOUT_S),
        ("discord.send", PERMISSION_TOOL_TIMEOUT_S),
    ):
        if _gated in tools:
            router.set_timeout(_gated, _budget)
    # Expose the shared self-editing surface so the /dev/* endpoints operate on
    # the same proposal store Nova writes to via self.propose_change.
    router.dev_mode = dev_mode  # type: ignore[attr-defined]
    return router
