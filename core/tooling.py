from __future__ import annotations

import asyncio
import contextlib
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from core.code_ops import CodeOps
from core.dates import parse_date_range, parse_reminder_time
from core.dev_mode import DevMode, DevModeError
from core.file_extract import extract_excerpt as _extract_file_excerpt
from core.project_manager import ProjectManager
from core.tool_router import ToolRouter
from core.logging_setup import get_logger
from memory.unifier import MemoryUnifier
from plugins.registry import REGISTRY


logger = get_logger(__name__)


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

    plugin_tools = {name: spec.fn for name, spec in REGISTRY.get_tools().items()}
    plugin_descriptions = {name: spec.description for name, spec in REGISTRY.get_tools().items()}

    async def _scaffold_project(args: dict[str, Any]) -> dict[str, Any]:
        name = str(args.get("name") or "").strip()
        path = project_manager.scaffold_project(name)
        return {"project": name, "path": str(path)}

    async def _code_read(args: dict[str, Any]) -> dict[str, Any]:
        path = Path(str(args.get("path") or "")).expanduser()
        content = await code_ops.read_text(path)
        return {"path": str(path), "content": content}

    async def _code_write(args: dict[str, Any]) -> dict[str, Any]:
        path = Path(str(args.get("path") or "")).expanduser()
        content = str(args.get("content") or "")
        await code_ops.apply_patch_atomic(path, content)
        return {"path": str(path), "bytes": len(content.encode("utf-8"))}

    async def _memory_rebuild(_: dict[str, Any]) -> dict[str, Any]:
        res = await memory.rebuild_semantic_index()
        return {"rebuilt": res}

    def _slug_topic(t: str) -> str:
        t = re.sub(r"[^a-z0-9]+", "_", (t or "").strip().lower()).strip("_")
        return t[:48] or "general"

    async def _memory_remember(args: dict[str, Any]) -> dict[str, Any]:
        fact = str(args.get("fact") or args.get("value") or args.get("note") or "").strip()
        topic = _slug_topic(str(args.get("topic") or args.get("attribute") or ""))
        if not fact:
            return {"ok": False, "error": "missing_fact"}
        # The user explicitly asked Nova to remember this — record it as stated
        # (#19), the highest-trust provenance, so recall never hedges on it later.
        await memory.add_fact(
            entity="note", attribute=topic, value=fact[:400], confidence=0.9,
            source="user", verification_status="stated",
        )
        return {"ok": True, "saved": fact[:400], "topic": topic}

    async def _memory_recall(args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query") or args.get("q") or "").strip()
        if not query:
            return {"ok": False, "error": "missing_query"}
        # Date-range recall: "what did we talk about last Tuesday" -> pull the
        # actual turns from that day (semantic search alone can't do temporal).
        rng = parse_date_range(query)
        if rng:
            since, until = rng
            rows = await memory.recall_conversation(
                since_iso=since.isoformat(), until_iso=until.isoformat(), limit=40
            )
            rows.reverse()  # chronological reads better than newest-first
            lines = [f"{r['speaker']} ({r['created_at'][:16].replace('T', ' ')}): {r['content']}" for r in rows]
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
        await memory.add_lesson(lesson, topic=topic)
        return {"ok": True, "learned": lesson[:200], "topic": topic}

    async def _memory_remember_person(args: dict[str, Any]) -> dict[str, Any]:
        name = str(args.get("name") or "").strip()
        if not name:
            return {"ok": False, "error": "missing_name"}
        # Freeform attributes: relation, how_met, works_at, notes, etc.
        attrs = args.get("attributes")
        if not isinstance(attrs, dict):
            attrs = {k: str(v) for k, v in args.items() if k not in {"name", "attributes"} and v}
        attrs = {str(k): str(v)[:300] for k, v in (attrs or {}).items() if str(v).strip()}
        await memory.upsert_person(name=name, attributes=attrs)
        return {"ok": True, "person": name, "attributes": attrs}

    async def _memory_remember_event(args: dict[str, Any]) -> dict[str, Any]:
        note = str(args.get("note") or args.get("event") or args.get("what") or "").strip()
        date = str(args.get("date") or args.get("when") or "").strip()
        if not note:
            return {"ok": False, "error": "missing_note"}
        await memory.add_event(date=date or "unspecified", note=note[:400])
        return {"ok": True, "event": note[:200], "date": date or "unspecified"}

    async def _memory_recall_person(args: dict[str, Any]) -> dict[str, Any]:
        name = str(args.get("name") or "").strip()
        if not name:
            return {"ok": False, "error": "missing_name"}
        person = await memory.recall_person(name)
        if person is None:
            return {"ok": False, "error": "not_found", "name": name}
        return {"ok": True, **person}

    async def _memory_related(args: dict[str, Any]) -> dict[str, Any]:
        key = str(args.get("name") or args.get("key") or args.get("topic") or "").strip()
        if not key:
            return {"ok": False, "error": "missing_name"}
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
        entries = await memory.timeline(about=about, days=days)
        if not entries:
            return {"ok": True, "entries": [], "note": f"Nothing recorded in the last {days} day(s)" + (f" about '{about}'" if about else "") + "."}
        return {"ok": True, "days": days, "about": about, "entries": entries}

    async def _memory_path(args: dict[str, Any]) -> dict[str, Any]:
        a = str(args.get("from") or args.get("a") or args.get("source") or "").strip()
        b = str(args.get("to") or args.get("b") or args.get("target") or "").strip()
        if not a or not b:
            return {"ok": False, "error": "missing_endpoints", "note": "need both 'from' and 'to'"}
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
        tid = await memory.note_thought(kind, content, topic=topic)
        if not tid:
            return {"ok": False, "error": "thoughts_disabled"}
        return {"ok": True, "noted": content[:200], "kind": kind}

    async def _thoughts_recall(args: dict[str, Any]) -> dict[str, Any]:
        topic = str(args.get("topic") or args.get("about") or "").strip() or None
        kind = str(args.get("kind") or "").strip() or None
        thoughts = await memory.recall_thoughts(topic=topic, kind=kind)
        if not thoughts:
            return {"ok": True, "thoughts": [], "note": "No open internal thoughts recorded" + (f" about '{topic}'" if topic else "") + "."}
        return {"ok": True, "thoughts": [{"kind": t["kind"], "topic": t["topic"], "content": t["content"]} for t in thoughts]}

    async def _twin_profile(args: dict[str, Any]) -> dict[str, Any]:
        profile = await memory.digital_twin_profile()
        if not profile.get("enabled", True):
            return {"ok": False, "error": "digital_twin_disabled"}
        if not profile.get("enough_data"):
            return {"ok": True, "enough_data": False, "note": profile.get("note", "Still learning your patterns.")}
        return {"ok": True, **profile}

    async def _executive_brief(args: dict[str, Any]) -> dict[str, Any]:
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
        plan = build_plan(vision, horizon_days=horizon, milestones=milestones, items=items)
        await memory.save_plan(goal_id, plan)
        return {"ok": True, "goal_id": goal_id, "milestones": len(plan["milestones"]),
                "items": len(plan["items"]), "progress": progress(plan)}

    async def _plan_status(args: dict[str, Any]) -> dict[str, Any]:
        from core.goal_planner import progress
        goal_id = str(args.get("goal_id") or args.get("goal") or args.get("id") or "").strip()
        if not goal_id:
            return {"ok": False, "error": "missing_goal_id"}
        plan = await memory.load_plan(goal_id)
        if plan is None:
            return {"ok": False, "error": "no_plan", "note": f"No plan saved for goal '{goal_id}' yet."}
        return {"ok": True, "goal_id": goal_id, "vision": plan.get("vision"),
                "progress": progress(plan), "milestones": plan.get("milestones", []), "items": plan.get("items", [])}

    async def _plan_advance(args: dict[str, Any]) -> dict[str, Any]:
        goal_id = str(args.get("goal_id") or args.get("goal") or args.get("id") or "").strip()
        if not goal_id:
            return {"ok": False, "error": "missing_goal_id"}
        summary = await memory.advance_plan(goal_id)
        if summary is None:
            return {"ok": False, "error": "no_plan"}
        return {"ok": True, "goal_id": goal_id, **summary}

    async def _research_track(args: dict[str, Any]) -> dict[str, Any]:
        topic = str(args.get("topic") or args.get("subject") or "").strip()
        if not topic:
            return {"ok": False, "error": "missing_topic"}
        if await memory.track_research_topic(topic):
            return {"ok": True, "tracking": topic,
                    "note": "I'll keep an eye on this and save findings (with sources) as I learn them. Enable NOVA_RESEARCH for automatic periodic updates."}
        return {"ok": False, "error": "invalid_topic"}

    async def _research_list(args: dict[str, Any]) -> dict[str, Any]:
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

        max_files = max(1, min(int(args.get("max_files") or 200), 500))
        scanned = indexed = skipped_unchanged = failed = 0
        errors: list[str] = []

        for p in folder.rglob("*"):
            if scanned >= max_files:
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
                continue

            if not await memory.document_needs_indexing(str(p), stat.st_mtime):
                skipped_unchanged += 1
                continue

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

        more = scanned >= max_files
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
        parsed = parse_reminder_time(when_text)
        if parsed is None:
            return {"ok": False, "error": "could_not_parse_time",
                     "note": f"Could not understand the time '{when_text}'. Ask the user to be more specific (e.g. '5pm', 'tomorrow at 9am', 'every morning at 8')."}
        due_at, recurrence = parsed
        rid = await memory.create_reminder(
            title=title, details=str(args.get("details") or title), due_at_iso=due_at.isoformat(), recurrence=recurrence,
        )
        return {"ok": True, "reminder_id": str(rid), "title": title, "due_at": due_at.isoformat(), "recurrence": recurrence}

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
        timeout_s = float(args.get("timeout_s") or 45.0)
        if not cmd:
            return {"ok": False, "error": "missing_cmd"}
        if _looks_dangerous_cmd(cmd):
            return {"ok": False, "error": "refusing_dangerous_command", "cmd": cmd}

        proc = await asyncio.create_subprocess_shell(
            cmd,
            cwd=str(repo_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except TimeoutError:
            with contextlib.suppress(Exception):
                proc.kill()
            return {"ok": False, "error": "timeout", "cmd": cmd, "timeout_s": timeout_s}

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
        "goal.create": _goal_create,
        "reminder.create": _reminder_create,
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
        "memory.remember": "Save a fact or note to permanent long-term memory when the user asks you to remember something. args: {fact, topic?}",
        "memory.recall": "Search permanent long-term memory for previously saved facts and notes. args: {query}",
        "memory.learn_lesson": ("Save a durable lesson about how Marcus wants you to behave (a correction or "
                                 "preference) so you apply it in future replies. args: {lesson, topic?}"),
        "memory.remember_person": ("Save someone Marcus mentions (a friend, coworker, family member) so you can "
                                    "recall who they are later. ALWAYS use this (not memory.remember) whenever he "
                                    "gives you a birthday or anniversary for someone — put it in attributes as "
                                    "birthday/anniversary (e.g. 'April 12') so you can remind him before it comes "
                                    "up. args: {name, attributes?:{relation, how_met, works_at, birthday, anniversary, notes}}"),
        "memory.remember_event": ("Save a dated life event Marcus mentions (a trip, appointment, milestone). "
                                   "args: {note, date?}"),
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
        "thoughts.note": ("Record one of your own private internal thoughts to revisit later — an idea, an "
                           "unresolved question, a potential improvement, an interesting discovery, a future plan. "
                           "These persist across sessions and are private. kinds: idea|question|unresolved|"
                           "improvement|discovery|failed_experiment|future_plan. args: {content, kind?, topic?}"),
        "thoughts.recall": ("Surface your own internal thoughts — use ONLY when Marcus asks what you've been "
                             "thinking about / pondering / your ideas. Never volunteer these unprompted. "
                             "args: {topic?, kind?}"),
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
        "plan.status": ("Show a goal's plan and progress (milestones/items done, % complete, at-risk, overdue). "
                         "args: {goal_id}"),
        "plan.advance": ("Roll a plan forward: missed recurring items move to their next occurrence instead of "
                          "becoming overdue, and open milestones past their target date are flagged at-risk. Run "
                          "this when catching up on a goal. args: {goal_id}"),
        "research.track": ("Start tracking a research topic Marcus wants Nova to follow over time (AI, GPUs, "
                            "snowboarding, a framework, ...). Findings accrue into the world model with sources. "
                            "args: {topic}"),
        "research.list": ("List the research topics Nova is currently tracking and when each was last checked. args: {}"),
        "research.findings": ("Show what Nova has learned about a tracked research topic — each finding with its "
                               "source citation. args: {topic}"),
        "memory.index_folder": ("Index a folder's files/photos into memory so Marcus can later ask about them "
                                 "('where's that PDF about the mortgage', 'photos from the beach trip'). "
                                 "args: {path, max_files?}. Skips files already indexed and unchanged."),
        "image.generate": ("Generate an image from a text description on the local image-generation GPU. "
                           "args: {prompt, width?, height?}. If the service isn't running or the second GPU "
                           "isn't installed yet, this honestly reports that — never claim an image was made "
                           "if the tool result says ok:false."),
        "video.generate": "Not implemented yet — always returns unavailable. Offer image.generate instead.",
        "reminder.create": ("Schedule a reminder or recurring check-in. args: {title, when, details?}. "
                            "'when' examples: '5pm', 'in 20 minutes', 'tomorrow at 9am', 'every morning at 8', "
                            "'every weekday at 7:30am', 'every monday at noon'. If the user didn't give a clear "
                            "time, ask them — never guess one."),
        "goal.create": ("Start tracking a real multi-step objective that spans multiple sessions (e.g. 'help me "
                        "learn Spanish', 'plan the Hawaii trip'). You'll advance it in the background and report "
                        "progress. Use for ongoing goals, NOT one-shot tasks. args: {objective, title?, success_criteria?, project?}"),
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
    # Expose the shared self-editing surface so the /dev/* endpoints operate on
    # the same proposal store Nova writes to via self.propose_change.
    router.dev_mode = dev_mode  # type: ignore[attr-defined]
    return router
