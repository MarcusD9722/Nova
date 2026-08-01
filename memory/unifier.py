from __future__ import annotations

import asyncio

import aiosqlite
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from core.dates import parse_month_day
from core.digital_twin import DigitalTwinInputs, derive_profile
from core.executive import ExecutiveContext, recommend
from core.experiments import compare_variants
from core.skills import detect_repeated_workflow, workflow_parameters
from core.event_bus import BUS, clip
from core.logging_setup import get_logger
from core.settings import get_bool
from memory.backends.chroma_backend import ChromaMemoryBackend
from memory.backends.diskcache_backend import DiskCacheBackend
from memory.backends.json_backend import JsonAuditBackend
from memory.backends.sqlite_backend import SQLiteMemoryBackend
from memory.graph import Edge, GraphStore, build_timeline, extract_turn_edges, fact_edge, person_relation_edge
from memory.provenance import classify_default, normalize_status, observed_at_write
from memory.schemas import FactRecord, MemoryHit
from memory.thoughts import ThoughtStore
from memory.world_model import WorldModel


logger = get_logger(__name__)

IGNORE_TURN_KINDS = {"turn", "turn_user", "turn_assistant"}

# Behavioral "lessons" — durable guidance Nova learns from Marcus's corrections
# and preferences — are stored as facts under this entity and applied to future
# replies. Kept separate from ordinary facts so they can be listed/injected as a
# group. See core/runtime.py (capture + injection) and the reflection pass.
LESSON_ENTITY = "lesson"


def _lesson_topic_slug(topic: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (topic or "").strip().lower()).strip("-")
    return s[:48] or "general"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _staleness_factor(created_at: Any, last_reinforced_at: Any, half_life_days: float = 90.0) -> float:
    """Recency multiplier in [0.85, 1.0] (Phase 1.3). Uses the NEWER of
    created/reinforced timestamps, so a re-mentioned memory stays fresh.
    Deliberately gentle: decay re-ranks, it never buries or deletes."""
    import math

    newest = None
    for raw in (last_reinforced_at, created_at):
        if not raw:
            continue
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if newest is None or dt > newest:
            newest = dt
    if newest is None:
        return 1.0
    age_days = max(0.0, (_now() - newest).total_seconds() / 86400.0)
    return 0.85 + 0.15 * math.exp(-age_days / half_life_days)


class MemoryUnifier:
    # Attributes that hold exactly ONE current value: a new write supersedes
    # (deletes) older rows instead of accumulating contradictory facts that
    # search would then surface side by side ("mother = Tara" AND "= Sara").
    _SINGLETON_USER_ATTRS = {"name", "location", "spouse", "mother", "father", "children_type"}
    _SINGLETON_PROJECT_ATTRS = {"status", "summary", "brief", "next_steps", "last_worked"}

    def __init__(self, memory_dir: Path, *, enable_chroma: bool = True):
        self._dir = memory_dir
        self._sqlite = SQLiteMemoryBackend(memory_dir / "sqlite" / "nova.sqlite3")
        self._diskcache = DiskCacheBackend(memory_dir / "diskcache")
        self._chroma = ChromaMemoryBackend(memory_dir / "chroma") if enable_chroma else None
        self._json = JsonAuditBackend(memory_dir / "json")
        self._write_lock = asyncio.Lock()
        self._init_lock = asyncio.Lock()
        self._initialized = False
        self._chroma_degraded_logged = False
        # Bumped on every long-term write/delete; part of the search cache key
        # so fresh facts are recallable immediately instead of after the TTL.
        self._search_gen = 0
        # Knowledge graph (Phase 1.1) — same SQLite file, own table.
        self._graph = GraphStore(memory_dir / "sqlite" / "nova.sqlite3")
        # Semantic world model (Phase 4 / #11) — same SQLite file, own table.
        self._world = WorldModel(memory_dir / "sqlite" / "nova.sqlite3")
        # Persistent internal thoughts (Phase 4 / #6) — same SQLite file, own table.
        self._thoughts = ThoughtStore(memory_dir / "sqlite" / "nova.sqlite3")
        self._known_names_cache: tuple[float, list[str]] | None = None
        # Executive recommendations cache (Phase 5 / #1) — recomputed at most
        # every few minutes so the per-turn grounding hook stays cheap.
        self._exec_cache: tuple[float, list[dict[str, Any]]] | None = None
        # Optional LLM query expander (U3), wired by the runtime which owns the
        # model. None = pure deterministic term extraction, exactly as before.
        self._query_expander: Any | None = None
        # Optional LLM phrasing helper (U4). None = hardcoded templates.
        self._expression: Any | None = None

    def set_query_expander(self, expander: Any | None) -> None:
        """Install an `async (query, terms) -> list[str]` term expander."""
        self._query_expander = expander

    def set_expression(self, expression: Any | None) -> None:
        """Install the U4 Expression helper (LLM phrasing / naming / signal
        reading). None = the deterministic templates, exactly as before."""
        self._expression = expression

    @property
    def graph(self) -> GraphStore:
        return self._graph

    @property
    def world(self) -> WorldModel:
        return self._world

    @property
    def thoughts(self) -> ThoughtStore:
        return self._thoughts

    def _is_singleton_fact(self, entity: str, attribute: str) -> bool:
        ent = (entity or "").strip().lower()
        attr = (attribute or "").strip().lower()
        if ent == "user":
            return attr in self._SINGLETON_USER_ATTRS
        if ent == "projects":
            return attr == "last_active"
        if ent.startswith("project:"):
            return attr in self._SINGLETON_PROJECT_ATTRS
        if ent.startswith("conversation:"):
            # ":digest" facts are keyed by day (attribute=date-slug): one digest
            # per day, latest wins, but different days ACCUMULATE (so history
            # isn't destroyed). The plain rolling "summary" stays singleton.
            if ent.endswith(":digest"):
                return True
            if ent.endswith(":story"):
                return attr == "state"  # one running story bible, latest wins
            return attr == "summary"
        if ent == "mood":
            return True  # one mood reading per day (attribute=date), days accumulate
        if ent == "wellbeing":
            return True  # one wellbeing reading per day (attribute=date), days accumulate
        if ent == "interest_focus":
            return True  # one focus snapshot per week (attribute=week-slug), weeks accumulate
        if ent == "self_eval":
            return True  # one self-eval snapshot per day (attribute=date), days accumulate
        if ent == "session":
            return True  # bookkeeping facts (last_active, *_nudged_at, habit_suggested:*) — one current value each
        if ent == "executive":
            return True  # one last-surfaced timestamp per recommendation key (throttle bookkeeping)
        if ent.startswith("plan:"):
            return attr == "tree"  # one current plan tree per goal, latest wins
        if ent in ("research_topic", "research_checked"):
            return True  # one entry / one last-checked stamp per topic slug
        if ent.startswith("agent:"):
            return True  # one current value per specialist stat (confidence/consulted/helpful)
        if ent == "experiment":
            return True  # one definition per experiment id (attribute = id)
        if ent.startswith("exptrials:"):
            return True  # one accumulating trials blob per experiment
        if ent == "skill":
            return True  # one current definition per learned skill id
        if ent == "activity":
            return True  # one rolling activity log (attribute = "log")
        return False

    async def _fact_ids_for(self, entity: str, attribute: str, value: str | None = None) -> list[str]:
        sql = "SELECT id FROM facts WHERE entity = ? AND attribute = ?"
        params: list[Any] = [entity, attribute]
        if value is not None:
            sql += " AND value = ?"
            params.append(value)
        async with aiosqlite.connect(self._sqlite._db_path) as db:  # type: ignore[attr-defined]
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, tuple(params)) as cur:
                rows = await cur.fetchall()
        return [str(r["id"]) for r in rows]

    async def _chroma_upsert_safe(self, *, doc_id: str, text: str, metadata: dict[str, Any]) -> None:
        """Best-effort semantic index write.

        SQLite is the source of truth; a Chroma failure (e.g. a store written
        by an incompatible chromadb version) must never break memory ingest.
        """
        if self._chroma is None:
            return
        try:
            await self._chroma.upsert_text(doc_id=doc_id, text=text, metadata=metadata)
        except Exception as e:  # noqa: BLE001
            if not self._chroma_degraded_logged:
                self._chroma_degraded_logged = True
                logger.warning("chroma_unavailable_semantic_index_degraded", error=str(e)[:300])
                BUS.publish(
                    "system.warning",
                    {"component": "memory.semantic_index", "error": clip(e, 200), "impact": "semantic recall degraded; facts still saved"},
                )
            else:
                logger.debug("chroma_upsert_failed", error=str(e)[:200])

    async def initialize(self) -> None:
        if self._initialized:
            return
        async with self._init_lock:
            if self._initialized:
                return
            await self._sqlite.initialize()
            await self._json.initialize()

            # Best-effort: ensure semantic index exists after cold start.
            try:
                await self._ensure_semantic_index()
            except Exception as e:  # noqa: BLE001
                logger.debug("semantic_index_ensure_failed", error=str(e))

            self._initialized = True

    async def _ensure_semantic_index(self) -> None:
        """Ensure Chroma has at least the stable facts/people/events.

        SQLite is the source-of-truth. If Chroma is empty (e.g., deleted), rebuild it.
        """
        if self._chroma is None:
            return
        sqlite_counts = await self._sqlite.count_records()
        try:
            chroma_count = await self._chroma.count()
        except Exception:
            chroma_count = 0

        # If there is nothing stable to index, don't touch Chroma.
        stable_total = int(sqlite_counts.get("facts", 0)) + int(sqlite_counts.get("people", 0)) + int(sqlite_counts.get("events", 0))
        if stable_total <= 0:
            return

        # Only rebuild when Chroma looks empty.
        if chroma_count <= 0:
            await self.rebuild_semantic_index()

    async def rebuild_semantic_index(self) -> dict[str, int]:
        """Rebuild Chroma documents from SQLite facts/people/events."""
        if self._chroma is None:
            return {"facts": 0, "people": 0, "events": 0}
        await self._sqlite.initialize()
        await self._json.initialize()

        await self._chroma.reset()

        facts = await self._sqlite.all_facts(limit=None)
        people = await self._sqlite.all_people(limit=None)
        events = await self._sqlite.all_events(limit=None)

        # Upsert in small batches to avoid large thread payloads.
        fact_n = 0
        for row in facts:
            fact_n += 1
            await self._chroma.upsert_text(
                doc_id=str(row["id"]),
                text=f"FACT {row['entity']} {row['attribute']} = {row['value']}",
                metadata={
                    "kind": "fact",
                    "entity": str(row["entity"]),
                    "attribute": str(row["attribute"]),
                    "created_at": str(row.get("created_at") or ""),
                },
            )

        person_n = 0
        for row in people:
            person_n += 1
            await self._chroma.upsert_text(
                doc_id=str(row["id"]),
                text=f"PERSON {row['name']} {row['attributes_json']}",
                metadata={"kind": "person", "name": str(row["name"]), "created_at": str(row.get("created_at") or "")},
            )

        event_n = 0
        for row in events:
            event_n += 1
            await self._chroma.upsert_text(
                doc_id=str(row["id"]),
                text=f"EVENT {row['date']}: {row['note']}",
                metadata={"kind": "event", "date": str(row["date"]), "created_at": str(row.get("created_at") or "")},
            )

        await self._json.append_snapshot(
            {"kind": "semantic_index_rebuild", "facts": fact_n, "people": person_n, "events": event_n, "ts": _now().isoformat()}
        )
        return {"facts": fact_n, "people": person_n, "events": event_n}

    async def ingest_turn(self, conversation_id: UUID, role: str, content: str) -> UUID:
        await self.initialize()
        turn_id = uuid4()
        created_at = _now().isoformat()

        # Index substantive turns semantically so anything said is recallable
        # later (not just what got distilled into a structured fact). Skip short
        # greetings/acks to keep the index signal-rich. Best-effort; never blocks.
        index_turn = (
            os.getenv("NOVA_INDEX_TURNS", "1").strip().lower() not in {"0", "false", "no", "off"}
            and len((content or "").strip()) >= 25
        )

        async with self._write_lock:
            writes = [
                self._sqlite.add_turn(
                    turn_id=turn_id,
                    conversation_id=conversation_id,
                    role=role,
                    content=content,
                    created_at_iso=created_at,
                ),
                self._json.append_audit(
                    {
                        "kind": "turn",
                        "id": str(turn_id),
                        "conversation_id": str(conversation_id),
                        "role": role,
                        "content": content,
                        "created_at": created_at,
                    }
                ),
            ]
            if index_turn and self._chroma is not None:
                speaker = "Marcus" if role == "user" else "Nova"
                writes.append(
                    self._chroma_upsert_safe(
                        doc_id=f"turn:{turn_id}",
                        text=f"{speaker} said: {content}",
                        metadata={"kind": "turn", "role": role, "created_at": created_at,
                                  "conversation_id": str(conversation_id)},
                    )
                )
            await asyncio.gather(*writes)

        if index_turn:
            self._search_gen += 1
        BUS.publish("memory.write", {"kind": "turn", "role": role, "source": "conversation"})

        # Phase 1.1: observe co-mentions in this turn as graph edges (people
        # mentioned together; people tied to the active project). Cheap and
        # deterministic; failures never block ingest.
        try:
            known = await self.known_person_names()
            active_project = None
            try:
                lp = await self.get_latest_fact(entity="projects", attribute="last_active")
                active_project = lp.value.strip() if lp and lp.value else None
            except Exception:
                pass
            for edge in extract_turn_edges(content, known, active_project):
                await self._graph.upsert_edge(edge)
        except Exception as e:  # noqa: BLE001
            logger.debug("graph_turn_extract_failed", error=str(e)[:160])

        return turn_id

    async def known_person_names(self) -> list[str]:
        """Names Nova knows as people: the people table plus family/friend
        fact values. Cached briefly — called on every ingested turn."""
        import time as _time

        cached = self._known_names_cache
        if cached and (_time.monotonic() - cached[0]) < 60:
            return cached[1]
        names: set[str] = set()
        try:
            for row in await self._sqlite.all_people(limit=None):
                n = str(row.get("name") or "").strip()
                if n:
                    names.add(n)
        except Exception:
            pass
        try:
            for attr in ("child", "spouse", "mother", "father", "sibling", "cousin", "friend", "pet"):
                for rec in await self.get_facts(entity="user", attribute=attr, limit=30, newest_first=False):
                    n = (rec.value or "").split("|", 1)[0].strip()
                    # Guard against junk values (past extraction bugs stored
                    # sentences as child names); real names are short.
                    if n and len(n) <= 32 and len(n.split()) <= 3:
                        names.add(n)
        except Exception:
            pass
        result = sorted(names)
        self._known_names_cache = (_time.monotonic(), result)
        return result

    async def related(self, key: str, *, limit: int = 20) -> dict[str, Any]:
        """Graph neighbors (1- and 2-hop) for a person/project/topic key."""
        await self.initialize()
        return await self._graph.related(key, limit=limit)

    async def timeline(self, *, about: str | None = None, days: int = 14, limit: int = 40) -> list[dict[str, Any]]:
        """Time-ordered view of what happened (events, digests, reminders,
        new facts), optionally filtered to one person/project/topic."""
        await self.initialize()
        return await build_timeline(self._sqlite, about=about, days=days, limit=limit)

    # ── KG 2.0 (Phase 4): path, subgraph, discovery, arbitrary links ─────────

    async def graph_path(self, a: str, b: str, *, max_depth: int = 4) -> list[dict[str, Any]]:
        """Shortest connection path between two nodes — 'how are X and Y related'."""
        await self.initialize()
        return await self._graph.path_between(a, b, max_depth=max_depth)

    async def graph_subgraph(self, key: str, *, depth: int = 2, limit: int = 60) -> dict[str, Any]:
        """Bounded neighborhood around a node for visualization/reasoning."""
        await self.initialize()
        return await self._graph.subgraph(key, depth=depth, limit=limit)

    async def discover_graph_associations(self, *, min_shared: int = 2, max_new: int = 40) -> dict[str, int]:
        """Run one automatic relationship-discovery pass over the graph."""
        await self.initialize()
        return await self._graph.discover_associations(min_shared=min_shared, max_new=max_new)

    async def link(
        self, src_kind: str, src_key: str, predicate: str, dst_kind: str, dst_key: str, *, confidence: float = 0.7
    ) -> bool:
        """Record an explicit typed edge between any two nodes — the universal
        relationship engine (#8) that supports node kinds beyond person/project
        (movie, book, hardware, software, idea, location, ...). Returns False if
        the edge was degenerate (empty/self)."""
        await self.initialize()
        pred = re.sub(r"[^a-z0-9]+", "_", (predicate or "").strip().lower()).strip("_")[:40] or "related_to"
        edge = Edge((src_kind or "topic").strip().lower(), src_key, pred, (dst_kind or "topic").strip().lower(), dst_key)
        if not edge.src_key.strip() or not edge.dst_key.strip():
            return False
        try:
            await self._graph.upsert_edge(edge, confidence=confidence)
            self._search_gen += 1
            return True
        except Exception as e:  # noqa: BLE001
            logger.debug("graph_link_failed", error=str(e)[:160])
            return False

    # ── Semantic world model (Phase 4 / #11) ─────────────────────────────────

    async def world_learn(self, subject: str, predicate: str, obj: str, *, source: str, confidence: float = 0.6) -> bool:
        """Record a general world fact with source attribution. Refuses
        unsourced facts (returns False) — world knowledge is never stored as an
        assumption."""
        await self.initialize()
        if not get_bool("NOVA_WORLD_MODEL"):
            return False
        return await self._world.upsert(subject, predicate, obj, confidence=confidence, source=source)

    async def world_recall(self, subject: str) -> dict[str, Any]:
        """What Nova knows about a subject from the world model, plus whether it's
        fresh enough to answer without re-searching the web."""
        await self.initialize()
        if not get_bool("NOVA_WORLD_MODEL"):
            return {"subject": subject, "facts": [], "fresh": False, "enabled": False}
        facts = await self._world.query_subject(subject)
        return {
            "subject": subject,
            "facts": facts,
            "fresh": await self._world.is_fresh(subject),
            "enabled": True,
        }

    async def world_search(self, term: str, *, limit: int = 30) -> list[dict[str, Any]]:
        await self.initialize()
        if not get_bool("NOVA_WORLD_MODEL"):
            return []
        return await self._world.search(term, limit=limit)

    async def remember_web_finding(self, topic: str, summary: str, url: str, *, confidence: float = 0.55) -> bool:
        """Fold a web finding into the world model with the URL as source, so a
        repeat question can be answered without searching again."""
        await self.initialize()
        if not get_bool("NOVA_WORLD_MODEL"):
            return False
        return await self._world.upsert(topic, "summary", summary, confidence=confidence, source=url or "web")

    # ── Persistent internal thoughts (Phase 4 / #6) ──────────────────────────

    async def note_thought(self, kind: str, content: str, *, topic: str = "general") -> str:
        """Record one of Nova's private internal thoughts. Gated by the flag;
        returns '' when disabled or empty."""
        await self.initialize()
        if not get_bool("NOVA_INTERNAL_THOUGHTS"):
            return ""
        return await self._thoughts.add(kind, topic, content)

    async def recall_thoughts(self, *, topic: str | None = None, kind: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        """Surface Nova's internal thoughts — only ever called on explicit
        request (a tool/endpoint), never injected into ordinary replies."""
        await self.initialize()
        if not get_bool("NOVA_INTERNAL_THOUGHTS"):
            return []
        if kind:
            return await self._thoughts.list(kind=kind, status="open", limit=limit)
        return await self._thoughts.recall(topic=topic, limit=limit)

    async def resolve_thought(self, thought_id: str, *, status: str = "resolved") -> None:
        await self.initialize()
        await self._thoughts.resolve(thought_id, status=status)

    # ── Personal digital twin (Phase 5 / #4) ─────────────────────────────────

    async def digital_twin_profile(self) -> dict:
        """Gather Marcus's working-pattern signals and derive his digital-twin
        profile. All signals are ones Nova already records; the twin predicts
        patterns, never impersonates. Gated by NOVA_DIGITAL_TWIN."""
        await self.initialize()
        if not get_bool("NOVA_DIGITAL_TWIN"):
            return {"enabled": False}

        # ── Six independent signal fetches, run CONCURRENTLY (U1) ────────────
        # Each helper owns its try/except and returns a safe default, preserving
        # the previous "a failed signal degrades that field only" behavior.

        async def _turn_hours() -> list[int]:
            # Local hour-of-day for recent turns (created_at is UTC).
            try:
                hours: list[int] = []
                for row in await self._sqlite.recent_turns(conversation_id=None, limit=300):
                    dt = self._parse_dt(row.get("created_at"))
                    if dt is not None:
                        hours.append(dt.astimezone().hour)
                return hours
            except Exception:
                return []

        async def _trends() -> tuple[str, str]:
            try:
                return await asyncio.gather(
                    self.recent_mood_trend(days=5),
                    self.recent_wellbeing_trend(days=7),
                )
            except Exception:
                return "", ""

        async def _interests() -> list[str]:
            try:
                return [r.value for r in await self.get_facts(entity="interest_focus", limit=6, newest_first=True) if r.value]
            except Exception:
                return []

        async def _lessons_recent() -> int:
            try:
                cut = _now() - timedelta(days=14)
                return sum(
                    1 for r in await self.get_facts(entity=LESSON_ENTITY, limit=60, newest_first=True)
                    if r.created_at >= cut
                )
            except Exception:
                return 0

        async def _reminder_punctuality() -> tuple[int, int]:
            on_time = behind = 0
            try:
                for r in await self.list_reminders(status="fired", limit=200):
                    if str(r.get("title") or "").startswith("__nova_"):
                        continue
                    due, fired = self._parse_dt(r.get("due_at")), self._parse_dt(r.get("updated_at"))
                    if due and fired:
                        if (fired - due).total_seconds() > 3600:
                            behind += 1
                        else:
                            on_time += 1
            except Exception:
                pass
            return on_time, behind

        async def _goal_progress() -> tuple[int, int]:
            active = stalled = 0
            try:
                stale_cut = _now() - timedelta(days=7)
                for g in await self.list_goals(limit=100):
                    if str(g.get("status") or "") != "active":
                        continue
                    active += 1
                    up = self._parse_dt(g.get("updated_at"))
                    if up is None or up < stale_cut:
                        stalled += 1
            except Exception:
                pass
            return active, stalled

        turn_hours, (mood, wellbeing), interests, lessons_recent, (ontime, late), (goals_active, goals_stalled) = (
            await asyncio.gather(
                _turn_hours(), _trends(), _interests(), _lessons_recent(),
                _reminder_punctuality(), _goal_progress(),
            )
        )

        profile = derive_profile(DigitalTwinInputs(
            turn_hours=turn_hours, mood_trend=mood, wellbeing_trend=wellbeing,
            interests=interests, lessons_recent=lessons_recent,
            reminders_ontime=ontime, reminders_late=late,
            goals_active=goals_active, goals_stalled=goals_stalled,
        ))

        # U4: the derived stress level comes from substring-matching a fixed word
        # list, so "I'm fine, just been a lot lately" reads as low. When a model
        # is wired in, let it read the same evidence instead — keeping the
        # deterministic value as the fallback and the basis string honest.
        if profile.get("enough_data") and self._expression is not None and getattr(self._expression, "available", False):
            evidence = f"{mood} {wellbeing}".strip()
            if evidence:
                try:
                    read = await self._expression.read_signal(
                        evidence, labels=["low", "some", "elevated"],
                        fallback=profile["stress_level"]["value"],
                    )
                    if read != profile["stress_level"]["value"]:
                        profile["stress_level"] = {
                            "value": read,
                            "basis": f"read from the mood/wellbeing signal: {evidence[:120]}",
                        }
                except Exception:
                    pass  # keep the deterministic read

        profile["enabled"] = True
        return profile

    # ── Executive intelligence (Phase 5 / #1) ────────────────────────────────

    async def _gather_executive(self) -> list[dict[str, Any]]:
        """Collect live signals and produce confidence-gated recommendations.
        Cached ~5 min so the per-turn grounding hook doesn't re-scan every turn."""
        import time as _time
        if self._exec_cache and (_time.monotonic() - self._exec_cache[0]) < 300:
            return self._exec_cache[1]

        now = _now()
        now_local = datetime.now().astimezone()

        # ── Four independent signal fetches, run CONCURRENTLY (U1) ───────────

        async def _reminders() -> tuple[list[str], list[dict[str, Any]]]:
            late: list[str] = []
            soon: list[dict[str, Any]] = []
            try:
                for r in await self.list_reminders(status="pending", limit=100):
                    title = str(r.get("title") or "")
                    if title.startswith("__nova_"):
                        continue
                    due = self._parse_dt(r.get("due_at"))
                    if not due:
                        continue
                    delta_h = (due - now).total_seconds() / 3600.0
                    if delta_h < 0:
                        late.append(title)
                    elif delta_h <= 24:
                        soon.append({"label": title, "hours_until": max(0.0, delta_h)})
            except Exception:
                pass
            return late, soon

        async def _important_dates() -> list[dict[str, Any]]:
            try:
                return [
                    {"name": u["name"], "label": u["label"], "days_until": u["days_until"]}
                    for u in await self.list_people_with_upcoming_dates(within_days=3)
                ]
            except Exception:
                return []

        async def _goals() -> tuple[list[str], list[str]]:
            active: list[str] = []
            stalled: list[str] = []
            try:
                stale_cut = now - timedelta(days=7)
                for g in await self.list_goals(limit=100):
                    if str(g.get("status") or "") != "active":
                        continue
                    title = str(g.get("title") or "")
                    active.append(title)
                    up = self._parse_dt(g.get("updated_at"))
                    if up is None or up < stale_cut:
                        stalled.append(title)
            except Exception:
                pass
            return active, stalled

        async def _twin_signals() -> tuple[str | None, float, str]:
            try:
                prof = await self.digital_twin_profile()
                if prof.get("enough_data"):
                    return (
                        prof["peak_period"]["value"],
                        float(prof["procrastination_likelihood"]["value"]),
                        prof["stress_level"]["value"],
                    )
            except Exception:
                pass
            return None, 0.0, "low"

        (overdue, upcoming), upcoming_dates, (active_goals, stalled_goals), (peak, procrastination, stress) = (
            await asyncio.gather(_reminders(), _important_dates(), _goals(), _twin_signals())
        )

        recs = recommend(ExecutiveContext(
            now_hour=now_local.hour, overdue=overdue, upcoming=upcoming,
            upcoming_dates=upcoming_dates, active_goals=active_goals, stalled_goals=stalled_goals,
            peak_period=peak, procrastination=procrastination, stress_level=stress, weather=None,
        ))
        self._exec_cache = (_time.monotonic(), recs)
        return recs

    async def _phrase_recommendations(self, recs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """U4: make the WORDING fluid while leaving the decision untouched.

        Detection, ranking and the confidence gate above are what keep the
        executive layer from being annoying — those stay deterministic. Only the
        message text is rephrased, and only when a model is wired in; the
        original template survives any failure."""
        if not recs or self._expression is None or not getattr(self._expression, "available", False):
            return recs
        out: list[dict[str, Any]] = []
        for r in recs:
            phrased = dict(r)
            try:
                phrased["message"] = await self._expression.rephrase(
                    r["message"], context=f"proactive nudge about {r.get('kind', 'something')}",
                )
            except Exception:
                pass  # keep the template
            out.append(phrased)
        return out

    async def executive_recommendations(self, *, throttle: bool = False, throttle_hours: float = 6.0) -> list[dict[str, Any]]:
        """Proactive, confidence-gated recommendations (#1). With throttle=True
        (the unprompted grounding path) recently-surfaced items are suppressed and
        the returned ones marked, so Nova never repeats the same nudge; without it
        (an explicit /executive or executive.brief request) everything shows."""
        await self.initialize()
        if not get_bool("NOVA_EXECUTIVE"):
            return []
        recs = await self._gather_executive()
        recs = await self._phrase_recommendations(recs)
        if not throttle:
            return recs
        out: list[dict[str, Any]] = []
        for r in recs:
            attr = f"surfaced:{r['key']}"[:120]
            last = await self.get_latest_fact(entity="executive", attribute=attr)
            if last and last.value:
                last_dt = self._parse_dt(last.value)
                if last_dt and (_now() - last_dt).total_seconds() < throttle_hours * 3600:
                    continue
            out.append(r)
            await self.add_fact(entity="executive", attribute=attr, value=_now().isoformat(), confidence=1.0)
        return out

    # ── Long-term goal planning (Phase 5 / #3) ───────────────────────────────

    async def save_plan(self, goal_id: str, plan: dict[str, Any]) -> None:
        """Persist a goal's plan tree as a JSON fact (one current tree per goal)."""
        await self.initialize()
        await self.add_fact(entity=f"plan:{goal_id}", attribute="tree",
                            value=json.dumps(plan, ensure_ascii=False)[:20000], confidence=1.0)

    async def load_plan(self, goal_id: str) -> dict[str, Any] | None:
        await self.initialize()
        rec = await self.get_latest_fact(entity=f"plan:{goal_id}", attribute="tree")
        if not rec or not rec.value:
            return None
        try:
            return json.loads(rec.value)
        except Exception:
            return None

    async def advance_plan(self, goal_id: str) -> dict[str, Any] | None:
        """Roll a plan forward (missed recurring items → next occurrence; open
        milestones past target → at_risk) and persist. Returns the roll summary."""
        from core.goal_planner import roll_forward
        plan = await self.load_plan(goal_id)
        if plan is None:
            return None
        result = roll_forward(plan)
        await self.save_plan(goal_id, result["plan"])
        return {"rolled": result["rolled"], "at_risk": result["at_risk"], "overdue": result["overdue"]}

    # ── Autonomous research topics (Phase 5 / #9) ────────────────────────────

    @staticmethod
    def _research_slug(topic: str) -> str:
        return re.sub(r"[^a-z0-9]+", "-", (topic or "").strip().lower()).strip("-")[:60]

    async def track_research_topic(self, topic: str) -> bool:
        """Add an ongoing research topic. Findings accrue into the world model
        with citations; nothing is fabricated."""
        await self.initialize()
        topic = (topic or "").strip()
        slug = self._research_slug(topic)
        if not slug:
            return False
        await self.add_fact(entity="research_topic", attribute=slug, value=topic[:200], confidence=1.0)
        return True

    async def untrack_research_topic(self, topic: str) -> int:
        await self.initialize()
        slug = self._research_slug(topic)
        res = await self.purge_facts(entity="research_topic", attribute=slug, dry_run=False)
        await self.purge_facts(entity="research_checked", attribute=slug, dry_run=False)
        return int(res.get("deleted") or 0)

    async def list_research_topics(self) -> list[dict[str, Any]]:
        await self.initialize()
        topics = await self.get_facts(entity="research_topic", limit=100, newest_first=True)
        checked = {r.attribute: r.value for r in await self.get_facts(entity="research_checked", limit=200)}
        return [{"topic": t.value, "slug": t.attribute, "last_checked": checked.get(t.attribute)} for t in topics]

    async def next_research_topic(self) -> str | None:
        """The tracked topic least-recently checked (never-checked first) — the
        one an autonomous-research cycle should look at next."""
        topics = await self.list_research_topics()
        if not topics:
            return None
        topics.sort(key=lambda t: t.get("last_checked") or "")
        return topics[0]["topic"]

    async def mark_research_checked(self, topic: str) -> None:
        await self.initialize()
        slug = self._research_slug(topic)
        if slug:
            await self.add_fact(entity="research_checked", attribute=slug, value=_now().isoformat(), confidence=1.0)

    async def research_findings(self, topic: str) -> list[dict[str, Any]]:
        """What Nova has learned about a research topic — the sourced world-model
        entries (every finding carries its citation)."""
        await self.initialize()
        return await self._world.query_subject(topic)

    # ── Persistent agent society state (Phase 6 / #5) ─────────────────────────

    async def agent_state(self, agent_id: str) -> dict[str, Any]:
        """A specialist's durable state: confidence, times consulted (experience),
        and how often it was helpful. Defaults for a never-consulted agent."""
        await self.initialize()
        ent = f"agent:{agent_id}"

        async def _num(attr: str, default: float) -> float:
            rec = await self.get_latest_fact(entity=ent, attribute=attr)
            try:
                return float(rec.value) if rec and rec.value else default
            except (TypeError, ValueError):
                return default

        consulted = int(await _num("consulted", 0))
        helpful = int(await _num("helpful", 0))
        confidence = await _num("confidence", 0.5)
        return {
            "agent_id": agent_id,
            "confidence": round(confidence, 3),
            "consulted": consulted,
            "helpful": helpful,
            "experience": "seasoned" if consulted >= 20 else "practiced" if consulted >= 5 else "new",
        }

    async def record_consultation(self, agent_id: str, *, helpful: bool | None = None) -> None:
        """Log that a specialist participated. Experience always increments;
        confidence nudges up/down only when there's an explicit helpfulness
        signal (clamped, so it drifts slowly and never hits certainty)."""
        await self.initialize()
        ent = f"agent:{agent_id}"
        state = await self.agent_state(agent_id)
        await self.add_fact(entity=ent, attribute="consulted", value=str(state["consulted"] + 1), confidence=1.0)
        if helpful is True:
            await self.add_fact(entity=ent, attribute="helpful", value=str(state["helpful"] + 1), confidence=1.0)
            new_conf = min(0.95, state["confidence"] + 0.03)
            await self.add_fact(entity=ent, attribute="confidence", value=str(round(new_conf, 3)), confidence=1.0)
        elif helpful is False:
            new_conf = max(0.1, state["confidence"] - 0.03)
            await self.add_fact(entity=ent, attribute="confidence", value=str(round(new_conf, 3)), confidence=1.0)

    async def agent_remember(self, agent_id: str, note: str, *, topic: str = "general") -> None:
        """A specialist's own internal memory / learning history (accumulates)."""
        await self.initialize()
        note = (note or "").strip()
        if not note:
            return
        slug = re.sub(r"[^a-z0-9]+", "-", topic.lower()).strip("-")[:40] or "general"
        await self.add_fact(entity=f"agentmem:{agent_id}", attribute=slug, value=note[:400], confidence=0.7)

    async def agent_recall(self, agent_id: str, *, topic: str | None = None, limit: int = 10) -> list[str]:
        """A specialist's stored memory notes, newest first."""
        await self.initialize()
        rows = await self.get_facts(entity=f"agentmem:{agent_id}", limit=max(1, int(limit) * 2), newest_first=True)
        if topic:
            t = topic.lower()
            rows = [r for r in rows if t in (r.attribute or "").lower() or t in (r.value or "").lower()]
        return [r.value for r in rows[:limit] if r.value]

    # ── Autonomous experimentation (Phase 7 / #15) ───────────────────────────

    async def record_experiment(self, name: str, hypothesis: str = "") -> str | None:
        """Register an experiment. Returns its id, or None when disabled/invalid."""
        await self.initialize()
        if not get_bool("NOVA_EXPERIMENTS"):
            return None
        name = (name or "").strip()
        if not name:
            return None
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:32] or "exp"
        exp_id = f"{slug}-{uuid4().hex[:6]}"
        defn = {"id": exp_id, "name": name[:200], "hypothesis": hypothesis[:400],
                "created_at": _now().isoformat(), "status": "open"}
        await self.add_fact(entity="experiment", attribute=exp_id, value=json.dumps(defn), confidence=1.0)
        return exp_id

    async def add_experiment_trial(self, exp_id: str, variant: str, metrics: dict[str, Any]) -> bool:
        await self.initialize()
        if not get_bool("NOVA_EXPERIMENTS"):
            return False
        variant = (variant or "").strip()
        if not exp_id or not variant or not isinstance(metrics, dict):
            return False
        rec = await self.get_latest_fact(entity=f"exptrials:{exp_id}", attribute="data")
        try:
            trials = json.loads(rec.value) if rec and rec.value else []
        except Exception:
            trials = []
        clean = {k: float(v) for k, v in metrics.items() if isinstance(v, (int, float))}
        trials.append({"variant": variant, "metrics": clean})
        await self.add_fact(entity=f"exptrials:{exp_id}", attribute="data",
                            value=json.dumps(trials)[:20000], confidence=1.0)
        return True

    async def analyze_experiment(self, exp_id: str) -> dict[str, Any] | None:
        """Compare an experiment's variants into a RECOMMENDATION (never applied)."""
        await self.initialize()
        rec = await self.get_latest_fact(entity=f"exptrials:{exp_id}", attribute="data")
        if not rec or not rec.value:
            return {"verdict": "inconclusive", "reason": "no trials recorded", "ranking": [], "requires_approval": True}
        try:
            trials = json.loads(rec.value)
        except Exception:
            trials = []
        return compare_variants(trials)

    async def list_experiments(self) -> list[dict[str, Any]]:
        await self.initialize()
        out: list[dict[str, Any]] = []
        for r in await self.get_facts(entity="experiment", limit=100, newest_first=True):
            try:
                defn = json.loads(r.value)
            except Exception:
                continue
            trials_rec = await self.get_latest_fact(entity=f"exptrials:{defn['id']}", attribute="data")
            try:
                n_trials = len(json.loads(trials_rec.value)) if trials_rec and trials_rec.value else 0
            except Exception:
                n_trials = 0
            out.append({**defn, "trials": n_trials})
        return out

    # ── Activity log + autonomous skill learning (Phase 8 / #2) ──────────────

    async def log_activity(self, event: str, *, cap: int = 300) -> None:
        """Append a coarse activity token to the rolling log the skill learner
        watches for repeated workflows. Kept small and capped."""
        await self.initialize()
        event = (event or "").strip()[:120]
        if not event:
            return
        rec = await self.get_latest_fact(entity="activity", attribute="log")
        try:
            log = json.loads(rec.value) if rec and rec.value else []
        except Exception:
            log = []
        log.append(event)
        log = log[-cap:]
        await self.add_fact(entity="activity", attribute="log", value=json.dumps(log)[:20000], confidence=1.0)

    async def recent_activity(self, limit: int = 100) -> list[str]:
        await self.initialize()
        rec = await self.get_latest_fact(entity="activity", attribute="log")
        try:
            log = json.loads(rec.value) if rec and rec.value else []
        except Exception:
            log = []
        return log[-int(limit):]

    async def detect_learnable_workflow(self, *, min_repeats: int = 3) -> dict[str, Any] | None:
        """A repeated workflow in the activity log worth OFFERING to learn — or
        None. Never learns on its own; this only surfaces a candidate.

        U4: detection stays deterministic (it must be — it decides *whether* to
        interrupt), but the model proposes a human NAME for the workflow so the
        offer reads like "want me to learn 'Invoice Filing'?" instead of echoing
        raw step tokens."""
        found = detect_repeated_workflow(await self.recent_activity(limit=300), min_repeats=min_repeats)
        if not found:
            return None
        if self._expression is not None and getattr(self._expression, "available", False):
            try:
                found["suggested_name"] = await self._expression.name_for(
                    found["steps"], fallback=" then ".join(found["steps"][:2]),
                )
            except Exception:
                pass
        return found

    async def learn_skill(self, name: str, steps: list[str]) -> str | None:
        """Store an approved workflow as a learned skill (v1)."""
        await self.initialize()
        name = (name or "").strip()
        steps = [str(s).strip() for s in (steps or []) if str(s).strip()]
        if not name or not steps:
            return None
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:32] or "skill"
        skill_id = f"{slug}-{uuid4().hex[:6]}"
        skill = {"id": skill_id, "name": name[:120], "steps": steps, "version": 1, "versions": [],
                 "parameters": workflow_parameters(steps), "created_at": _now().isoformat(),
                 "updated_at": _now().isoformat()}
        await self.add_fact(entity="skill", attribute=skill_id, value=json.dumps(skill)[:20000], confidence=1.0)
        return skill_id

    async def get_skill(self, skill_id: str) -> dict[str, Any] | None:
        await self.initialize()
        rec = await self.get_latest_fact(entity="skill", attribute=skill_id)
        if not rec or not rec.value:
            return None
        try:
            return json.loads(rec.value)
        except Exception:
            return None

    async def list_skills(self) -> list[dict[str, Any]]:
        await self.initialize()
        out = []
        for r in await self.get_facts(entity="skill", limit=100, newest_first=True):
            try:
                s = json.loads(r.value)
                out.append({"id": s["id"], "name": s["name"], "steps": len(s["steps"]),
                            "version": s["version"], "parameters": s.get("parameters", [])})
            except Exception:
                continue
        return out

    async def update_skill(self, skill_id: str, steps: list[str]) -> dict[str, Any] | None:
        """Edit a skill's steps, bumping the version and keeping prior versions."""
        skill = await self.get_skill(skill_id)
        if skill is None:
            return None
        steps = [str(s).strip() for s in (steps or []) if str(s).strip()]
        if not steps:
            return None
        skill.setdefault("versions", []).append({"version": skill["version"], "steps": skill["steps"]})
        skill["version"] += 1
        skill["steps"] = steps
        skill["parameters"] = workflow_parameters(steps)
        skill["updated_at"] = _now().isoformat()
        await self.add_fact(entity="skill", attribute=skill_id, value=json.dumps(skill)[:20000], confidence=1.0)
        return skill

    async def branch_skill(self, skill_id: str, new_name: str) -> str | None:
        """Fork a skill into a new independent one (variant workflow)."""
        skill = await self.get_skill(skill_id)
        if skill is None:
            return None
        return await self.learn_skill(new_name, skill["steps"])

    async def delete_skill(self, skill_id: str) -> bool:
        res = await self.purge_facts(entity="skill", attribute=skill_id, dry_run=False)
        return int(res.get("deleted") or 0) > 0

    async def add_fact(
        self,
        entity: str,
        attribute: str,
        value: str,
        confidence: float = 0.7,
        *,
        source: str | None = None,
        evidence: str | None = None,
        verification_status: str | None = None,
    ) -> UUID:

        # ---- write guard: prevent storing non-name tokens as relationship values ----
        deny_by_attr = {
            "spouse": {"name", "names", "spouse", "wife", "husband", "partner"},
            "child": {"name", "names", "child", "children", "kid", "kids", "son", "sons", "daughter", "daughters"},
            "parent": {"name", "names", "parent", "parents", "mom", "mother", "dad", "father"},
            "mother": {"name", "names", "mom", "mother"},
            "father": {"name", "names", "dad", "father"},
            "sibling": {"name", "names", "sibling", "siblings", "brother", "brothers", "sister", "sisters"},
            "cousin": {"name", "names", "cousin", "cousins"},
            "friend": {"name", "names", "friend", "friends", "buddy", "buddies"},
            "pet": {"name", "names", "pet", "pets", "dog", "dogs", "cat", "cats"},
            "coworker": {"name", "names", "coworker", "coworkers", "colleague", "colleagues"},
        }
        attr_key = (attribute or "").strip().lower()
        v_raw = str(value).strip() if value is not None else ""
        v_norm = v_raw.lower()
        if attr_key in deny_by_attr:
            if (not v_raw) or (v_norm in deny_by_attr[attr_key]) or (len(v_raw) < 2):
                logger.debug("fact_rejected_denylist", entity=entity, attribute=attribute, value=value)
                return uuid4()  # preserve return type while skipping write
            if not any(ch.isalpha() for ch in v_raw):
                logger.debug("fact_rejected_denylist", entity=entity, attribute=attribute, value=value)
                return uuid4()

        await self.initialize()
        fact_id = uuid4()
        created_at = _now().isoformat()

        # Provenance (#19): resolve where this fact came from and how trustworthy
        # it is. When the flag is off we store NULLs (columns stay harmless). When
        # a caller doesn't specify, classify_default encodes what's genuinely known
        # about the write path — never a guess about the fact's truth. last_confirmed_at
        # starts at created_at only for directly-observed statuses; assumptions
        # (inferred/unverified) stay unconfirmed (NULL) so they can never masquerade
        # as settled facts.
        prov_source = prov_status = prov_evidence = prov_confirmed = None
        if get_bool("NOVA_MEMORY_PROVENANCE"):
            d_source, d_status = classify_default(entity, attribute)
            prov_source = source or d_source
            prov_status = normalize_status(verification_status or d_status)
            prov_evidence = evidence
            prov_confirmed = created_at if observed_at_write(prov_status) else None

        async with self._write_lock:
            # Supersede stale values for single-valued attributes; skip exact
            # duplicates for list-valued ones (child, friend, note, ...).
            stale_ids: list[str] = []
            if self._is_singleton_fact(entity, attribute):
                try:
                    stale_ids = await self._fact_ids_for(entity, attribute)
                    if stale_ids:
                        await self._sqlite.delete_facts_by_ids(stale_ids)
                except Exception as e:  # noqa: BLE001
                    logger.debug("fact_supersede_failed", entity=entity, attribute=attribute, error=str(e)[:200])
                    stale_ids = []
            else:
                try:
                    dupes = await self._fact_ids_for(entity, attribute, str(value))
                    if dupes:
                        # Phase 1.3: a re-mention is signal, not noise — touch
                        # last_reinforced_at and nudge confidence so this fact
                        # resists decay, instead of silently dropping the write.
                        try:
                            await self._sqlite.reinforce_fact(dupes[0])
                            self._search_gen += 1
                        except Exception:
                            pass
                        return UUID(dupes[0])  # already stored; nothing to write
                except Exception:
                    pass

            tasks = [
                self._sqlite.add_fact(
                    fact_id,
                    entity=entity,
                    attribute=attribute,
                    value=value,
                    confidence=confidence,
                    source=prov_source,
                    evidence=prov_evidence,
                    verification_status=prov_status,
                    last_confirmed_at=prov_confirmed,
                ),
                self._json.append_audit(
                    {
                        "kind": "fact",
                        "id": str(fact_id),
                        "entity": entity,
                        "attribute": attribute,
                        "value": value,
                        "confidence": confidence,
                        "created_at": created_at,
                        "source": prov_source,
                        "verification_status": prov_status,
                    }
                ),
                self._diskcache.set(
                    f"fact:{fact_id}",
                    {"entity": entity, "attribute": attribute, "value": value, "confidence": confidence, "created_at": created_at},
                    ttl_s=86400,
                ),
            ]
            if self._chroma is not None:
                tasks.append(
                    self._chroma_upsert_safe(
                        doc_id=str(fact_id),
                        text=f"FACT {entity} {attribute} = {value}",
                        metadata={"kind": "fact", "entity": entity, "attribute": attribute, "created_at": created_at},
                    )
                )
            await asyncio.gather(*tasks)

        self._search_gen += 1
        if stale_ids and self._chroma is not None:
            try:
                await self._chroma.delete_ids(stale_ids)
            except Exception:
                pass

        BUS.publish(
            "memory.write",
            {"kind": "fact", "entity": entity, "attribute": attribute, "value": clip(value, 120), "source": "long_term",
             "superseded": len(stale_ids), "verification": prov_status},
        )

        # Phase 1.1: a relationship-shaped fact also becomes a graph edge
        # (user.child=Liam -> Liam child_of user). Best-effort, never blocks.
        try:
            edge = fact_edge(entity, attribute, str(value))
            if edge is not None:
                await self._graph.upsert_edge(edge, confidence=min(0.9, confidence))
        except Exception as e:  # noqa: BLE001
            logger.debug("graph_fact_edge_failed", error=str(e)[:160])

        return fact_id

    async def upsert_person(self, name: str, attributes: dict[str, str]) -> UUID:
        await self.initialize()
        person_id = uuid4()
        created_at = _now().isoformat()

        # Merge with whatever's already known about this person — a SQL upsert
        # replaces the whole attributes_json blob, so writing just {"birthday":
        # ...} later would otherwise silently erase an earlier {"relation":
        # "son"} write for the same name.
        existing = await self._sqlite.get_person_by_name(name)
        merged = dict(attributes)
        if existing:
            try:
                prior = json.loads(existing.get("attributes_json") or "{}")
                if isinstance(prior, dict):
                    merged = {**prior, **attributes}
            except Exception:
                pass
        attributes_json = json.dumps(merged, ensure_ascii=False, sort_keys=True)

        async with self._write_lock:
            tasks = [
                self._sqlite.upsert_person(person_id, name=name, attributes_json=attributes_json),
                self._json.append_audit(
                    {
                        "kind": "person",
                        "id": str(person_id),
                        "name": name,
                        "attributes": attributes,
                        "created_at": created_at,
                    }
                ),
                self._diskcache.set(f"person:{name.lower()}", merged, ttl_s=86400),
            ]
            if self._chroma is not None:
                tasks.append(
                    self._chroma_upsert_safe(
                        doc_id=str(person_id),
                        text=f"PERSON {name} {attributes_json}",
                        metadata={"kind": "person", "name": name, "created_at": created_at},
                    )
                )
            await asyncio.gather(*tasks)

        self._search_gen += 1
        BUS.publish("memory.write", {"kind": "person", "name": clip(name, 80), "source": "long_term"})

        # Phase 1.1: a stored relation ("son", "coworker") becomes a graph edge.
        try:
            edge = person_relation_edge(name, merged)
            if edge is not None:
                await self._graph.upsert_edge(edge, confidence=0.8)
        except Exception as e:  # noqa: BLE001
            logger.debug("graph_person_edge_failed", error=str(e)[:160])

        return person_id

    # Attribute keys treated as an important recurring date (birthdays,
    # anniversaries, ...) when found in a person's attributes_json.
    _IMPORTANT_DATE_KEYS = ("birthday", "anniversary", "bday")

    @staticmethod
    def _important_dates_for(attributes: dict[str, Any]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for key, value in attributes.items():
            key_l = str(key).lower()
            is_date_key = key_l in MemoryUnifier._IMPORTANT_DATE_KEYS or key_l.endswith("_date")
            if not is_date_key:
                continue
            md = parse_month_day(str(value))
            if md is None:
                continue
            out.append({"label": key_l, "month": md[0], "day": md[1], "raw": str(value)})
        return out

    async def recall_person(self, name: str) -> dict[str, Any] | None:
        """Everything known about a person: attributes, when they were last
        mentioned in conversation, and any upcoming important dates."""
        await self.initialize()
        row = await self._sqlite.get_person_by_name(name)
        if not row:
            return None
        try:
            attributes = json.loads(row.get("attributes_json") or "{}")
            if not isinstance(attributes, dict):
                attributes = {}
        except Exception:
            attributes = {}

        last_mentioned: str | None = None
        try:
            turns = await self._sqlite.search_turns(term=name, limit=1)
            if turns:
                last_mentioned = str(turns[0]["created_at"])
        except Exception:
            pass

        return {
            "name": row["name"],
            "attributes": attributes,
            "last_mentioned": last_mentioned,
            "important_dates": self._important_dates_for(attributes),
        }

    async def list_people_with_upcoming_dates(self, within_days: int = 7) -> list[dict[str, Any]]:
        """People with a birthday/anniversary landing within the next N days
        (today inclusive), soonest first. Used to surface a gentle reminder —
        never guesses a date that wasn't actually stored."""
        await self.initialize()
        today = _now().date()
        out: list[dict[str, Any]] = []
        for row in await self._sqlite.all_people(limit=None):
            try:
                attributes = json.loads(row.get("attributes_json") or "{}")
                if not isinstance(attributes, dict):
                    continue
            except Exception:
                continue
            for d in self._important_dates_for(attributes):
                # Compare month/day only — the stored year (if any) isn't the
                # occurrence year; a birthday recurs every year.
                try:
                    this_year = today.replace(month=d["month"], day=d["day"], year=today.year)
                    occurrence = this_year if this_year >= today else this_year.replace(year=today.year + 1)
                except ValueError:
                    continue  # e.g. Feb 29 landing on a non-leap year — skip rather than guess
                days_until = (occurrence - today).days
                if 0 <= days_until <= within_days:
                    out.append({
                        "name": row["name"], "label": d["label"], "month": d["month"], "day": d["day"],
                        "occurrence": occurrence.isoformat(), "days_until": days_until,
                    })
        out.sort(key=lambda x: x["days_until"])
        return out

    # --- Long-horizon interest drift (MR1) ------------------------------------

    async def record_interest_focus(self, topic: str, week: str | None = None) -> None:
        """One interest-focus snapshot per ISO week (singleton per week; weeks
        accumulate so drift over months can be read back later)."""
        topic = (topic or "").strip()
        if not topic:
            return
        week = week or _now().strftime("%G-W%V")
        await self.add_fact(entity="interest_focus", attribute=week, value=topic[:200], confidence=0.5)

    async def recent_interest_drift(self, weeks: int = 6) -> str:
        """A short natural-language line noting how focus has shifted over the
        last few weeks, or '' if there's not enough history yet."""
        rows = await self.get_facts(entity="interest_focus", limit=weeks, newest_first=True)
        topics = [r.value for r in rows if r.value]
        if len(topics) < 2:
            return ""
        newest, oldest = topics[0], topics[-1]
        if newest.strip().lower() == oldest.strip().lower():
            return ""
        return f"A few weeks ago Marcus was focused on {oldest}; more recently it's been {newest}."

    async def add_event(self, date: str, note: str) -> UUID:
        await self.initialize()
        event_id = uuid4()
        created_at = _now().isoformat()

        async with self._write_lock:
            tasks = [
                self._sqlite.add_event(event_id, date=date, note=note),
                self._json.append_audit(
                    {
                        "kind": "event",
                        "id": str(event_id),
                        "date": date,
                        "note": note,
                        "created_at": created_at,
                    }
                ),
                self._diskcache.set(
                    f"event:{event_id}",
                    {"date": date, "note": note, "created_at": created_at},
                    ttl_s=86400,
                ),
            ]
            if self._chroma is not None:
                tasks.append(
                    self._chroma_upsert_safe(
                        doc_id=str(event_id),
                        text=f"EVENT {date}: {note}",
                        metadata={"kind": "event", "date": date, "created_at": created_at},
                    )
                )
            await asyncio.gather(*tasks)

        self._search_gen += 1
        BUS.publish("memory.write", {"kind": "event", "date": clip(date, 40), "note": clip(note, 120), "source": "long_term"})
        return event_id

    @staticmethod
    def _parse_dt(raw: Any) -> "datetime | None":
        if isinstance(raw, datetime):
            return raw
        if isinstance(raw, str) and raw.strip():
            try:
                return datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except Exception:
                return None
        return None

    def _fact_from_row(self, row: dict[str, Any]) -> FactRecord:
        created_dt = self._parse_dt(row.get("created_at")) or _now()
        return FactRecord(
            id=UUID(str(row["id"])),
            entity=str(row["entity"]),
            attribute=str(row["attribute"]),
            value=str(row["value"]),
            confidence=float(row.get("confidence", 0.7)),
            created_at=created_dt,
            source=(row.get("source") or None),
            evidence=(row.get("evidence") or None),
            verification_status=normalize_status(row.get("verification_status")),
            last_confirmed_at=self._parse_dt(row.get("last_confirmed_at")),
        )

    async def get_facts(
        self,
        entity: str,
        attribute: str | None = None,
        limit: int = 25,
        newest_first: bool = True,
    ) -> list[FactRecord]:
        """Deterministic fact retrieval (NOT semantic search)."""
        await self.initialize()
        ent = (entity or "").strip()
        attr = (attribute or "").strip() if attribute else None
        order = "DESC" if newest_first else "ASC"

        where = "WHERE entity = ?"
        params: list[Any] = [ent]
        if attr:
            where += " AND attribute = ?"
            params.append(attr)

        sql = (
            "SELECT id, entity, attribute, value, confidence, created_at, "
            "source, evidence, verification_status, last_confirmed_at "
            f"FROM facts {where} ORDER BY created_at {order} LIMIT ?"
        )
        params.append(int(limit))

        async with aiosqlite.connect(self._sqlite._db_path) as db:  # type: ignore[attr-defined]
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, tuple(params)) as cur:
                rows = await cur.fetchall()

        return [self._fact_from_row(dict(r)) for r in rows]

    async def get_latest_fact(self, entity: str, attribute: str) -> FactRecord | None:
        hits = await self.get_facts(entity=entity, attribute=attribute, limit=1, newest_first=True)
        return hits[0] if hits else None

    # ── Provenance (#19): confirm / re-verify ────────────────────────────────

    async def confirm_fact(self, fact_id: str, *, evidence: str | None = None) -> None:
        """Re-verify a fact against a fresh observation: stamp last_confirmed_at
        and promote its status to 'confirmed'. Turns an assumption into a checked
        fact only when there's genuine evidence — never silently."""
        await self.initialize()
        async with self._write_lock:
            await self._sqlite.confirm_fact(fact_id, evidence=evidence)
        self._search_gen += 1

    async def facts_needing_reverification(self, *, older_than_days: float = 180.0, limit: int = 50) -> list[dict[str, Any]]:
        """Directly-observed facts whose last confirmation is older than
        `older_than_days` — the queue a future re-verification pass would work.
        Read-only; assumptions are excluded (already flagged, not stale)."""
        await self.initialize()
        before = (_now() - timedelta(days=float(older_than_days))).isoformat()
        return await self._sqlite.facts_needing_reverification(before_iso=before, limit=limit)

    # ── Behavioral lessons (self-learning) ───────────────────────────────────

    async def add_lesson(self, lesson: str, topic: str = "general", confidence: float = 0.9) -> UUID:
        """Store a durable behavioral lesson (a correction or preference).

        Lessons are facts under LESSON_ENTITY; exact duplicates are skipped by
        add_fact's list-attribute dedup, so repeating the same instruction won't
        pile up.
        """
        text = (lesson or "").strip()
        if not text:
            return uuid4()
        return await self.add_fact(
            entity=LESSON_ENTITY, attribute=_lesson_topic_slug(topic), value=text[:400], confidence=confidence
        )

    async def get_lessons(self, limit: int = 12, newest_first: bool = True) -> list[str]:
        """Return recent lesson texts (deduped, order preserved)."""
        rows = await self.get_facts(entity=LESSON_ENTITY, limit=max(1, int(limit) * 2), newest_first=newest_first)
        out: list[str] = []
        seen: set[str] = set()
        for r in rows:
            v = (r.value or "").strip()
            k = v.lower()
            if v and k not in seen:
                seen.add(k)
                out.append(v)
            if len(out) >= limit:
                break
        return out

    async def lesson_records(self, limit: int = 50) -> list[dict[str, Any]]:
        """Lessons for the UI panel (id/topic/text/created_at)."""
        rows = await self.get_facts(entity=LESSON_ENTITY, limit=limit, newest_first=True)
        return [
            {"id": str(r.id), "topic": r.attribute, "text": r.value, "created_at": r.created_at.isoformat()}
            for r in rows
        ]

    async def consolidate_lessons(self, *, similarity: float = 0.87) -> int:
        """Merge near-duplicate lessons (Phase 1.4). The reflection loop keeps
        re-learning variations of the same instruction ("don't start new
        projects when debugging" × 8); collapse each cluster to its newest
        phrasing, deterministically — no LLM call. Returns lessons removed."""
        import difflib

        rows = await self.get_facts(entity=LESSON_ENTITY, limit=300, newest_first=True)
        kept: list[tuple[str, str]] = []  # (normalized full text, lead clause), newest first
        to_delete: list[str] = []         # exact values to purge

        def _norm(s: str) -> str:
            # Lowercase, strip punctuation, and crudely singularize so
            # "projects/ones" vs "project/one" phrasings compare equal.
            s = re.sub(r"[^a-z0-9 ]+", "", (s or "").lower()).strip()
            return " ".join(w.rstrip("s") if len(w) > 3 else w for w in s.split())

        def _lead(n: str) -> str:
            # The reflection loop's duplicates share an opening clause and
            # differ in tacked-on tails ("...; fix the current issue"), which
            # sinks whole-string similarity — compare the lead too. Thresholds
            # tuned against the real duplicate corpus: within-cluster lead
            # ratios run 0.75-1.0, distinct lessons max at ~0.51.
            return " ".join(n.split()[:8])

        for rec in rows:  # newest first, so the first of each cluster survives
            text = (rec.value or "").strip()
            if not text:
                continue
            n = _norm(text)
            lead = _lead(n)
            dup = any(
                difflib.SequenceMatcher(None, n, k_full).ratio() >= similarity
                or difflib.SequenceMatcher(None, lead, k_lead).ratio() >= 0.72
                for k_full, k_lead in kept
            )
            if dup:
                to_delete.append(text)
            else:
                kept.append((n, lead))

        if not to_delete:
            return 0
        result = await self.purge_facts(
            entity=LESSON_ENTITY, attribute=None, value_in=to_delete, value_ilike=None,
            dry_run=False, limit=len(to_delete) + 10,
        )
        removed = int(result.get("deleted") or 0)
        if removed:
            logger.info("lessons_consolidated", removed=removed, kept=len(kept))
            BUS.publish("memory.lessons_consolidated", {"removed": removed, "kept": len(kept)})
        return removed

    # --- Mood tracking (M1: emotional presence) ------------------------------

    async def record_mood(self, label: str, day: str | None = None) -> None:
        """One mood reading per calendar day (singleton per day; days
        accumulate so a trend can be read back later)."""
        day = day or _now().strftime("%Y-%m-%d")
        await self.add_fact(entity="mood", attribute=day, value=label, confidence=0.6)

    async def recent_mood_trend(self, days: int = 3) -> str:
        """A short natural-language line summarizing the last few days'
        detected mood, or '' if there's nothing recent to say. Never guesses —
        only reflects what was actually detected."""
        rows = await self.get_facts(entity="mood", limit=days, newest_first=True)
        if not rows:
            return ""
        labels = [r.value for r in rows if r.value]
        if not labels:
            return ""
        if len(labels) == 1:
            return f"Marcus seemed {labels[0]} recently."
        if len(set(labels)) == 1:
            return f"Marcus has seemed {labels[0]} the last {len(labels)} days."
        return f"Marcus's mood recently: {', '.join(labels)} (most recent first)."

    # --- Wellbeing awareness (WB1) --------------------------------------------

    async def record_wellbeing_signal(self, label: str, day: str | None = None) -> None:
        """One wellbeing reading per calendar day (singleton per day; days
        accumulate). Only meaningful signals get written — an ordinary day
        records nothing, same discipline as record_mood."""
        day = day or _now().strftime("%Y-%m-%d")
        await self.add_fact(entity="wellbeing", attribute=day, value=label, confidence=0.5)

    async def recent_wellbeing_trend(self, days: int = 5) -> str:
        """A short, gentle natural-language line if a wellbeing pattern shows
        up across recent days — e.g. several late nights in a row. Returns ''
        when there's nothing worth a mention (including: already mentioned
        recently, see should_nudge_wellbeing)."""
        rows = await self.get_facts(entity="wellbeing", limit=days, newest_first=True)
        labels = [r.value for r in rows if r.value]
        if len(labels) < 2:
            return ""
        if labels[0] == "late_night" and labels.count("late_night") >= 2:
            return f"Marcus has been up late {labels.count('late_night')} of the last {len(labels)} days he talked to you."
        return ""

    async def should_nudge_wellbeing(self, *, min_gap_days: int = 3) -> bool:
        """Guard so a wellbeing observation surfaces once, gently — not every
        turn. True if she hasn't nudged about it in the last `min_gap_days`."""
        last = await self.get_latest_fact(entity="session", attribute="wellbeing_nudged_at")
        if not last or not last.value:
            return True
        try:
            last_dt = datetime.fromisoformat(last.value)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
        except Exception:
            return True
        return (_now() - last_dt).days >= min_gap_days

    async def mark_wellbeing_nudged(self) -> None:
        await self.add_fact(entity="session", attribute="wellbeing_nudged_at", value=_now().isoformat(), confidence=1.0)

    # --- Habit & pattern learning (HP1) ----------------------------------------

    async def log_tool_usage(self, tool_name: str) -> None:
        await self.initialize()
        await self._sqlite.log_tool_usage(tool_name)

    async def distinct_logged_tools(self, window_days: int = 14) -> list[str]:
        await self.initialize()
        since = (_now() - timedelta(days=window_days)).isoformat()
        return await self._sqlite.distinct_logged_tools(since)

    async def detect_habit(
        self, tool_name: str, *, window_days: int = 14, min_distinct_days: int = 4, hour_window: int = 2
    ) -> dict[str, Any] | None:
        """A tool called in roughly the same hour-of-day window on several
        distinct recent days — e.g. weather.current most mornings. Returns
        None unless there's a genuine repeated pattern (never guesses from a
        handful of calls)."""
        since = (_now() - timedelta(days=window_days)).isoformat()
        timestamps = await self._sqlite.tool_usage_since(tool_name, since)
        if len(timestamps) < min_distinct_days:
            return None

        by_hour: dict[int, set[str]] = {}
        for ts in timestamps:
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except Exception:
                continue
            by_hour.setdefault(dt.hour, set()).add(dt.date().isoformat())

        best_hour, best_days = None, set()
        for hour, days in by_hour.items():
            # Merge this hour with its neighbors within hour_window so "7am"
            # and "8am" count as the same loose morning window.
            combined: set[str] = set(days)
            for h2, days2 in by_hour.items():
                if h2 != hour and abs(h2 - hour) <= hour_window:
                    combined |= days2
            if len(combined) > len(best_days):
                best_hour, best_days = hour, combined

        if best_hour is None or len(best_days) < min_distinct_days:
            return None
        return {"tool": tool_name, "hour": best_hour, "distinct_days": len(best_days)}

    async def should_suggest_habit(self, tool_name: str) -> bool:
        existing = await self.get_latest_fact(entity="session", attribute=f"habit_suggested:{tool_name}")
        return existing is None

    async def mark_habit_suggested(self, tool_name: str) -> None:
        await self.add_fact(
            entity="session", attribute=f"habit_suggested:{tool_name}", value=_now().isoformat(), confidence=1.0
        )

    # --- Continuity across gaps (CG1) ------------------------------------------

    async def check_and_mark_session_gap(self) -> "timedelta | None":
        """Gap since the last recorded activity across any conversation
        (measured from BEFORE this call updates it), or None on the very
        first-ever session. Always advances last_active to now — call this
        once per turn so the next gap stays accurate."""
        await self.initialize()
        prior = await self.get_latest_fact(entity="session", attribute="last_active")
        gap: timedelta | None = None
        if prior and prior.value:
            try:
                prior_dt = datetime.fromisoformat(prior.value)
                if prior_dt.tzinfo is None:
                    prior_dt = prior_dt.replace(tzinfo=timezone.utc)
                gap = _now() - prior_dt
            except Exception:
                gap = None
        await self.add_fact(entity="session", attribute="last_active", value=_now().isoformat(), confidence=1.0)
        return gap

    async def build_catchup_summary(self, since_iso: str) -> str:
        """What changed since `since_iso` — newly indexed documents, fired
        reminders, goal progress, mood/wellbeing trend. Sections are omitted
        honestly when there's nothing to report; never invented."""
        parts: list[str] = []

        try:
            docs = await self.list_indexed_documents(limit=200)
            new_docs = [d for d in docs if str(d.get("indexed_at") or "") >= since_iso]
            if new_docs:
                parts.append(f"I indexed {len(new_docs)} new file(s)")
        except Exception:
            pass

        try:
            rems = await self.list_reminders(status="fired", limit=100)
            fired = [
                r for r in rems
                if str(r.get("updated_at") or "") >= since_iso and not str(r.get("title") or "").startswith("__nova_")
            ]
            if fired:
                parts.append(f"{len(fired)} reminder(s) fired")
        except Exception:
            pass

        try:
            goals = await self.list_goals(limit=50)
            updated = [g for g in goals if str(g.get("updated_at") or "") >= since_iso]
            if updated:
                titles = ", ".join(str(g.get("title") or "") for g in updated[:3])
                parts.append(f"progress on: {titles}")
        except Exception:
            pass

        for trend_fn in (self.recent_mood_trend, self.recent_wellbeing_trend):
            try:
                trend = await trend_fn()
                if trend:
                    parts.append(trend.rstrip("."))
            except Exception:
                pass

        if not parts:
            return ""
        return "Since you last talked, " + "; ".join(parts) + "."

    async def recall_conversation(
        self,
        *,
        term: str | None = None,
        since_iso: str | None = None,
        until_iso: str | None = None,
        limit: int = 30,
    ) -> list[dict[str, Any]]:
        """Keyword/date-range recall over the full turn history (any age)."""
        await self.initialize()
        rows = await self._sqlite.search_turns(
            term=(term or None), since_iso=since_iso, until_iso=until_iso, limit=int(limit)
        )
        return [
            {
                "role": str(r.get("role")),
                "speaker": "Marcus" if str(r.get("role")) == "user" else "Nova",
                "content": str(r.get("content") or ""),
                "created_at": str(r.get("created_at") or ""),
            }
            for r in rows
        ]

    # --- Reminders / scheduling ---------------------------------------------

    async def create_reminder(
        self, *, title: str, details: str = "", due_at_iso: str, recurrence: str = "none"
    ) -> UUID:
        await self.initialize()
        rid = uuid4()
        async with self._write_lock:
            await self._sqlite.create_reminder(
                reminder_id=rid, title=title, details=details or title,
                due_at_iso=due_at_iso, recurrence=recurrence,
            )
        BUS.publish("reminder.created", {"reminder_id": str(rid), "title": clip(title, 120), "due_at": due_at_iso})
        return rid

    async def list_reminders(self, *, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        await self.initialize()
        return await self._sqlite.list_reminders(status=status, limit=limit)

    async def due_reminders(self, *, limit: int = 20) -> list[dict[str, Any]]:
        await self.initialize()
        return await self._sqlite.due_reminders(now_iso=_now().isoformat(), limit=limit)

    async def reschedule_reminder(self, *, reminder_id: str, next_due_at_iso: str) -> None:
        await self.initialize()
        async with self._write_lock:
            await self._sqlite.reschedule_reminder(reminder_id=reminder_id, next_due_at_iso=next_due_at_iso)

    async def complete_reminder(self, *, reminder_id: str) -> None:
        await self.initialize()
        async with self._write_lock:
            await self._sqlite.set_reminder_status(reminder_id=reminder_id, status="fired")

    async def cancel_reminder(self, *, reminder_id: str) -> None:
        await self.initialize()
        async with self._write_lock:
            await self._sqlite.set_reminder_status(reminder_id=reminder_id, status="cancelled")
        BUS.publish("reminder.cancelled", {"reminder_id": reminder_id})

    # --- Local file / photo recall (indexed documents) ----------------------

    async def document_needs_indexing(self, path: str, mtime: float) -> bool:
        """True if this file was never indexed, or has changed since (mtime
        differs) — used to skip re-indexing unchanged files on repeat scans."""
        await self.initialize()
        existing = await self._sqlite.get_document_mtime(path)
        return existing is None or abs(existing - mtime) > 1e-6

    @staticmethod
    def _chunk_text(text: str, chunk_size: int = 1000, overlap: int = 150) -> list[str]:
        """Split into overlapping chunks so a semantic query can retrieve just
        the relevant passage(s) of a long file instead of only its first
        ~1500 chars. Overlap avoids splitting a relevant sentence across two
        chunks that neither fully contains."""
        text = (text or "").strip()
        if not text:
            return []
        if len(text) <= chunk_size:
            return [text]
        chunks: list[str] = []
        start = 0
        while start < len(text):
            end = min(start + chunk_size, len(text))
            chunks.append(text[start:end])
            if end >= len(text):
                break
            start = end - overlap
        return chunks

    async def index_document(self, *, path: str, excerpt: str, mtime: float) -> None:
        await self.initialize()
        chunks = self._chunk_text(excerpt)
        async with self._write_lock:
            await self._sqlite.upsert_document(path=path, excerpt=excerpt, mtime=mtime)

            # Drop any stale chunk-ids left over from a previous, longer
            # version of this file before writing the fresh set — Chroma
            # upserts by id, so a shrinking file would otherwise leave orphan
            # entries behind.
            prior_count = await self._sqlite.document_chunk_count(path)
            if self._chroma is not None and prior_count > len(chunks):
                stale_ids = [f"doc:{path}#{i}" for i in range(len(chunks), prior_count)]
                try:
                    await self._chroma.delete_ids(stale_ids)
                except Exception:
                    pass

            await self._sqlite.replace_document_chunks(path, chunks)

            if self._chroma is not None:
                name = Path(path).name
                for i, chunk in enumerate(chunks):
                    await self._chroma_upsert_safe(
                        doc_id=f"doc:{path}#{i}",
                        text=f"FILE {name} (part {i + 1}/{len(chunks)}): {chunk}",
                        metadata={"kind": "document", "path": path, "chunk_index": i, "created_at": _now().isoformat()},
                    )
        self._search_gen += 1
        BUS.publish("memory.write", {"kind": "document", "path": clip(path, 200), "source": "file_index"})

    async def list_indexed_documents(self, *, limit: int = 200) -> list[dict[str, Any]]:
        await self.initialize()
        return await self._sqlite.list_documents(limit=limit)

    async def search_document_chunks_broad(self, topic: str, limit: int = 20) -> list[dict[str, Any]]:
        """Broader multi-chunk retrieval across indexed documents for
        memory.synthesize — deliberately NOT capped to one chunk per file
        (that cap is for the general-purpose search() used by memory.recall,
        which needs to stay uncluttered by a single long document)."""
        await self.initialize()
        results: list[dict[str, Any]] = []
        seen: set[str] = set()

        if self._chroma is not None:
            try:
                chroma_hits = await self._chroma.query(topic, limit=limit * 3)
            except Exception:
                chroma_hits = []
            for ch in chroma_hits:
                meta = ch.get("metadata") or {}
                if str(meta.get("kind", "")).lower() != "document":
                    continue
                doc_id = str(ch.get("id"))
                if doc_id in seen:
                    continue
                seen.add(doc_id)
                dist = float(ch.get("distance", 1.0))
                results.append({
                    "path": str(meta.get("path", "")),
                    "chunk_index": meta.get("chunk_index"),
                    "text": str(ch.get("text", "")),
                    "score": max(0.0, 1.0 - dist),
                })

        if len(results) < limit:
            # Keyword fallback so this still works if chroma is degraded — a
            # single LIKE '%whole phrase%' rarely matches natural text, so
            # split into terms and search each individually (same approach
            # as the main search() method's SQLite fallback).
            terms = [t for t in re.findall(r"[a-z0-9']+", topic.lower()) if len(t) >= 3]
            for term in terms[:8] or [topic]:
                rows = await self._sqlite.search_document_chunks(term, limit=limit)
                for row in rows:
                    key = f"{row['path']}#{row['chunk_index']}"
                    if key in seen:
                        continue
                    seen.add(key)
                    results.append({
                        "path": row["path"], "chunk_index": row["chunk_index"], "text": row["text"], "score": 0.5,
                    })

        results.sort(key=lambda r: r["score"], reverse=True)
        return results[:limit]

    async def recent_turns_text(self, limit: int = 30) -> str:
        """Recent conversation turns (across conversations) as a transcript —
        used by the reflection pass to distill behavioral lessons."""
        await self.initialize()
        rows = await self._sqlite.recent_turns(conversation_id=None, limit=int(limit))
        rows = list(reversed(rows))  # chronological
        lines: list[str] = []
        for r in rows:
            role = "Marcus" if str(r.get("role")) == "user" else "Nova"
            content = str(r.get("content") or "").strip()
            if content:
                lines.append(f"{role}: {content}")
        return "\n".join(lines).strip()

    async def purge_facts(
        self,
        entity: str,
        attribute: str | None = None,
        value_in: list[str] | None = None,
        value_ilike: str | None = None,
        dry_run: bool = True,
        limit: int = 5000,
    ) -> dict[str, Any]:
        """Delete facts matching simple filters (SQLite only)."""
        await self.initialize()
        ent = (entity or "").strip()
        attr = (attribute or "").strip() if attribute else None
        vin = [v.strip() for v in (value_in or []) if isinstance(v, str) and v.strip()]
        vilike = (value_ilike or "").strip() or None

        where = ["entity = ?"]
        params: list[Any] = [ent]
        if attr:
            where.append("attribute = ?")
            params.append(attr)
        if vin:
            placeholders = ",".join(["?"] * len(vin))
            where.append(f"value IN ({placeholders})")
            params.extend(vin)
        if vilike:
            where.append("value LIKE ?")
            params.append(vilike.replace("*", "%"))

        where_sql = " AND ".join(where)
        select_sql = f"SELECT id FROM facts WHERE {where_sql} LIMIT ?"

        async with aiosqlite.connect(self._sqlite._db_path) as db:  # type: ignore[attr-defined]
            db.row_factory = aiosqlite.Row
            async with db.execute(select_sql, tuple(params) + (int(limit),)) as cur:
                id_rows = await cur.fetchall()
            ids = [str(r["id"]) for r in id_rows]
            matched = len(ids)
            deleted = 0
            if not dry_run and matched:
                id_placeholders = ",".join(["?"] * len(ids))
                await db.execute(f"DELETE FROM facts WHERE id IN ({id_placeholders})", tuple(ids))
                await db.commit()
                deleted = matched

        # Purged facts must vanish from semantic recall too, not just SQLite.
        if deleted:
            self._search_gen += 1
            if self._chroma is not None:
                try:
                    await self._chroma.delete_ids(ids)
                except Exception:
                    pass

        return {
            "entity": ent,
            "attribute": attr,
            "value_in": vin,
            "value_ilike": vilike,
            "dry_run": bool(dry_run),
            "matched": matched,
            "deleted": deleted,
            "ids": ids,
        }

    async def search(self, q: str, conversation_id: UUID | None = None, limit: int = 12) -> list[MemoryHit]:
        await self.initialize()
        q = (q or "").strip()
        if not q:
            return []

        # Normalize query into searchable terms (helps simple LIKE-based backends).
        q_norm = q.lower().replace("’", "'")
        raw_terms = [t for t in re.findall(r"[a-z0-9']+", q_norm) if len(t) >= 3]

        # Strip possessive suffixes (e.g. "mom's" -> "mom")
        terms: list[str] = []
        for t in raw_terms:
            if t.endswith("'s") and len(t) > 2:
                t = t[:-2]
            terms.append(t)

        # Keep a few high-signal short terms — WHOLE WORD only. A plain
        # substring check (`"ai" in q_norm`) false-positives on "hawaii",
        # "chairs", "explain", "again", etc., silently adding "ai" as an extra
        # broad search term that LIKE-matches huge numbers of unrelated facts
        # (any row containing "ai" anywhere) at a fixed high score, drowning
        # out genuinely relevant results. Same failure mode for "id" (avoid,
        # said, provide, decide, ...) — even more common as a substring.
        if re.search(r"\bai\b", q_norm):
            terms.append("ai")
        if re.search(r"\bid\b", q_norm):
            terms.append("id")

        # De-duplicate while preserving order.
        seen: set[str] = set()
        terms = [t for t in terms if not (t in seen or seen.add(t))]

        # Lightweight synonym expansion for common relationship queries.
        synonyms = {
            "mom": ["mother"],
            "mother": ["mom"],
            "dad": ["father"],
            "father": ["dad"],
        }
        expanded: list[str] = []
        for t in terms:
            expanded.append(t)
            expanded.extend(synonyms.get(t, []))

        seen2: set[str] = set()
        terms = [t for t in expanded if not (t in seen2 or seen2.add(t))]

        # U3: let the model widen the term set when one is wired in. The
        # hardcoded synonym dict above only knows mom/dad; an LLM expansion
        # catches "spouse"->"wife/partner", "car"->"vehicle", etc. The expander
        # UNIONS with the deterministic terms (never removes them) and any
        # failure leaves `terms` exactly as the heuristic produced it.
        if self._query_expander is not None:
            try:
                widened = await self._query_expander(q, terms)
                if widened:
                    terms = list(dict.fromkeys([*terms, *widened]))[:12]
            except Exception as e:  # noqa: BLE001
                logger.debug("query_expansion_failed", error=str(e)[:160])

        BUS.publish("memory.search", {"query": clip(q, 120)})

        cache_key = f"search:{self._search_gen}:{conversation_id}:{'|'.join(terms) or q_norm}:{limit}"
        cached = await self._diskcache.get(cache_key)
        if isinstance(cached, list) and cached:
            return [MemoryHit.model_validate(x) for x in cached]

        # ── Gather signals CONCURRENTLY (U1) ─────────────────────────────────
        # recent turns, the per-term LIKE searches, and the semantic (Chroma)
        # query are mutually independent. Previously this ran up to 1 + 32 + 1
        # awaits strictly one at a time on every recall; now the whole fan-out
        # is a single round-trip. Result ordering is preserved exactly (gather
        # returns in argument order), so ranking/dedup downstream is unchanged.

        async def _term_batch(t: str) -> tuple[list, list, list, list]:
            return await asyncio.gather(
                self._sqlite.search_facts(t, limit=12),
                self._sqlite.search_people(t, limit=8),
                self._sqlite.search_events(t, limit=8),
                self._sqlite.search_documents(t, limit=8),
            )

        async def _keyword_rows() -> tuple[list, list, list, list]:
            if terms:
                facts_a: list[dict[str, Any]] = []
                people_a: list[dict[str, Any]] = []
                events_a: list[dict[str, Any]] = []
                docs_a: list[dict[str, Any]] = []
                for f_rows, p_rows, e_rows, d_rows in await asyncio.gather(*(_term_batch(t) for t in terms[:8])):
                    facts_a.extend(f_rows)
                    people_a.extend(p_rows)
                    events_a.extend(e_rows)
                    docs_a.extend(d_rows)
                return facts_a, people_a, events_a, docs_a
            return await _term_batch(q)

        async def _semantic_hits() -> list[dict[str, Any]]:
            # Semantic recall is optional; do not fail the request.
            if self._chroma is None:
                return []
            try:
                return await self._chroma.query(q, limit=limit)
            except Exception as e:
                logger.debug("chroma_query_failed", error=str(e))
                return []

        recent, keyword_rows, chroma_hits = await asyncio.gather(
            self._sqlite.recent_turns(conversation_id=conversation_id, limit=60),
            _keyword_rows(),
            _semantic_hits(),
        )
        fact_rows, people_rows, event_rows, document_rows = keyword_rows

        hits: list[MemoryHit] = []

        # --- Recent turns (STRICT FILTERING) ---
        # Only include recent turns for longer, contextual queries; this prevents
        # irrelevant recent chatter (e.g., e2e "hello" spam) from polluting the prompt.
        if len(q_norm) >= 8 and terms:
            now = _now()
            recent_terms = set(terms)
            for row in recent:
                content_l = (row.get("content") or "").lower()

                # Require strong lexical overlap.
                if not any(t in content_l for t in recent_terms):
                    continue

                try:
                    created_at = datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00"))
                except Exception:
                    continue
                # Tolerate legacy naive timestamps (assume UTC) so mixing them
                # with timezone-aware ones never crashes the search.
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=timezone.utc)
                age_s = max(1.0, (now - created_at).total_seconds())

                # Hard cap: ignore turns older than 2 hours.
                if age_s > 7200:
                    continue

                recency_score = 1.0 / (1.0 + age_s / 1800.0)
                hits.append(
                    MemoryHit(
                        id=row["id"],
                        kind="turn",
                        text=f"{row['role']}: {row['content']}",
                        score=0.15 * recency_score,
                        provenance={"backend": "sqlite", "table": "turns", "conversation_id": row["conversation_id"]},
                    )
                )

        # Facts (highest priority for identity and stable attributes).
        # Phase 1.3: free-form notes decay gently with staleness (floor 0.85×,
        # never deleted); a re-mention refreshes them via last_reinforced_at.
        # Identity facts (user.*), lessons, and project state stay undecayed —
        # being old doesn't make your name less true, and sinking them would
        # falsely trip the low-confidence hedge (CH1).
        for row in fact_rows:
            text = f"FACT {row['entity']} {row['attribute']} = {row['value']}"
            base = 0.95
            if str(row.get("entity") or "").strip().lower() == "note":
                base *= _staleness_factor(row.get("created_at"), row.get("last_reinforced_at"))
            fact_prov = {"backend": "sqlite", "table": "facts"}
            # #19: carry the trust label into recall so consumers (and the CH1
            # hedge) can tell a settled fact from an assumption. Only attach when
            # actually recorded — legacy rows stay unlabeled rather than pretend.
            vstatus = normalize_status(row.get("verification_status"))
            if row.get("verification_status"):
                fact_prov["verification"] = vstatus
                if row.get("source"):
                    fact_prov["source"] = str(row["source"])
            hits.append(
                MemoryHit(
                    id=row["id"],
                    kind="fact",
                    text=text,
                    score=base,
                    provenance=fact_prov,
                )
            )

        # People (very high priority)
        for row in people_rows:
            text = f"PERSON {row['name']} {row['attributes_json']}"
            hits.append(
                MemoryHit(
                    id=row["id"],
                    kind="person",
                    text=text,
                    score=0.90,
                    provenance={"backend": "sqlite", "table": "people"},
                )
            )

        # Events (decay with age like notes — an event from months ago is
        # still recallable, just outranked by fresher context)
        for row in event_rows:
            text = f"EVENT {row['date']}: {row['note']}"
            hits.append(
                MemoryHit(
                    id=row["id"],
                    kind="event",
                    text=text,
                    score=0.60 * _staleness_factor(row.get("created_at"), None),
                    provenance={"backend": "sqlite", "table": "events"},
                )
            )

        # Indexed local files/photos (memory.index_folder) — keyword fallback so
        # they're recallable even when Chroma is unavailable/degraded.
        for row in document_rows:
            name = Path(str(row["path"])).name
            text = f"FILE {name}: {row['excerpt']}"
            hits.append(
                MemoryHit(
                    id=f"doc:{row['path']}",
                    kind="document",
                    text=text,
                    score=0.80,
                    provenance={"backend": "sqlite", "table": "documents", "path": str(row["path"])},
                )
            )

        # Chroma: distance is cosine distance; convert to similarity-ish. Turns
        # ARE now indexed and searchable (that's how "recall anything we talked
        # about" works) — but they sit in a lower score bucket so a structured
        # fact still outranks a passing remark on the same topic.
        # Documents are now chunked (DS1) — a long file can contribute several
        # chroma entries. For this general-purpose search (used by
        # memory.recall), keep only the best-scoring chunk per file so one
        # long document can't crowd out everything else; memory.synthesize
        # queries chroma directly and isn't subject to this cap.
        best_doc_chunk: dict[str, MemoryHit] = {}
        for ch in chroma_hits:
            meta = ch.get("metadata") or {}
            kind = str(meta.get("kind", "chroma")).lower()
            dist = float(ch.get("distance", 1.0))
            sim = max(0.0, 1.0 - dist)
            weight = 0.55 if kind.startswith("turn") else 0.85
            hit = MemoryHit(
                id=str(ch.get("id")),
                kind=kind or "chroma",
                text=str(ch.get("text", "")),
                score=weight * sim,
                provenance={"backend": "chroma", **{mk: str(mv) for mk, mv in meta.items()}},
            )
            if kind == "document" and meta.get("path"):
                doc_path = str(meta["path"])
                prev = best_doc_chunk.get(doc_path)
                if prev is None or hit.score > prev.score:
                    best_doc_chunk[doc_path] = hit
            else:
                hits.append(hit)
        hits.extend(best_doc_chunk.values())

        # Merge by id taking max score
        merged: dict[str, MemoryHit] = {}
        for h in hits:
            prev = merged.get(h.id)
            if prev is None or h.score > prev.score:
                merged[h.id] = h

        ranked = sorted(merged.values(), key=lambda x: x.score, reverse=True)[: int(limit)]

        await self._diskcache.set(cache_key, [r.model_dump() for r in ranked], ttl_s=120)
        # NOTE: previously every search() appended its full result set to
        # snapshots.jsonl — the fastest-growing file in the system, with no code
        # ever reading it back. Dropped: search runs on every chat turn.

        return ranked


    # ---------------- ChatGPT-like autonomy task queue (new contract) ----------------

    async def enqueue_task(
        self,
        *,
        title: str,
        details: str,
        priority: int = 3,
        project_name: str = "temp",
        initiated_by_user: bool = True,
        conversation_id: UUID | None = None,
        run_after_iso: str | None = None,
    ) -> UUID:
        """Enqueue a background autonomy task.

        Contract required by the runtime's Autonomy Supervisor:
        - title/details/priority are user-facing task descriptors
        - claim_next_task() will return these fields
        """
        await self.initialize()
        tid = uuid4()
        async with self._write_lock:
            await self._sqlite.enqueue_autonomy_task(
                task_id=tid,
                conversation_id=str(conversation_id) if conversation_id else None,
                project_name=project_name,
                title=title,
                details=details,
                priority=int(priority),
                initiated_by_user=bool(initiated_by_user),
                run_after_iso=run_after_iso,
            )
        BUS.publish("task.created", {"task_id": str(tid), "title": clip(title, 120), "project": project_name})
        return tid

    async def claim_next_task(self) -> dict[str, Any] | None:
        await self.initialize()
        return await self._sqlite.claim_next_autonomy_task()

    async def list_tasks(self, *, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """List background autonomy tasks (newest first) for the Tasks UI panel."""
        await self.initialize()
        return await self._sqlite.list_autonomy_tasks(status=status, limit=limit)

    async def recent_memory(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """Recent long-term memory records (facts/people/events) for the Memory UI panel."""
        await self.initialize()
        # all_* order ascending; fetch everything (stable records only, stays small)
        # and slice the newest after sorting.
        facts = await self._sqlite.all_facts(limit=None)
        people = await self._sqlite.all_people(limit=None)
        events = await self._sqlite.all_events(limit=None)

        items: list[dict[str, Any]] = []
        for row in facts:
            items.append(
                {
                    "id": str(row.get("id")),
                    "kind": "fact",
                    "source": "long_term",
                    "text": f"{row.get('entity')} · {row.get('attribute')} = {row.get('value')}",
                    "created_at": str(row.get("created_at") or ""),
                    # #19: expose provenance to the Memory panel — assumptions
                    # should read differently from settled facts.
                    "verification": normalize_status(row.get("verification_status")),
                    "provenance_source": (row.get("source") or None),
                    "last_confirmed_at": (row.get("last_confirmed_at") or None),
                }
            )
        for row in people:
            items.append(
                {
                    "id": str(row.get("id")),
                    "kind": "person",
                    "source": "long_term",
                    "text": str(row.get("name") or ""),
                    "created_at": str(row.get("created_at") or ""),
                }
            )
        for row in events:
            items.append(
                {
                    "id": str(row.get("id")),
                    "kind": "event",
                    "source": "long_term",
                    "text": f"{row.get('date')}: {row.get('note')}",
                    "created_at": str(row.get("created_at") or ""),
                }
            )

        items.sort(key=lambda x: x.get("created_at") or "", reverse=True)
        return items[:limit]

    async def mark_task_done(self, *, task_id: str, result: dict[str, Any] | None = None) -> None:
        await self.initialize()
        async with self._write_lock:
            await self._sqlite.complete_autonomy_task(task_id=task_id, status="done", result=result or {}, error="")
        BUS.publish("task.completed", {"task_id": str(task_id), "status": "done"})

    async def mark_task_failed(self, *, task_id: str, error: str, result: dict[str, Any] | None = None) -> None:
        await self.initialize()
        async with self._write_lock:
            await self._sqlite.complete_autonomy_task(task_id=task_id, status="failed", result=result or {}, error=error)
        BUS.publish("task.updated", {"task_id": str(task_id), "status": "failed", "error": clip(error, 160)})

    async def bump_task_attempt(self, *, task_id: str, attempts: int, run_after_iso: str, error: str) -> None:
        await self.initialize()
        async with self._write_lock:
            await self._sqlite.bump_autonomy_task_attempt(task_id=task_id, attempts=int(attempts), run_after_iso=run_after_iso, error=error)

    async def cancel_pending_background_work(self) -> dict[str, int]:
        await self.initialize()
        async with self._write_lock:
            return await self._sqlite.cancel_pending_background_work()


    # ---------------- Agentic goal/task/proposal wrappers ----------------

    async def create_goal(
        self,
        *,
        project_name: str,
        title: str,
        objective: str,
        success_criteria: str = "",
        status: str = "active",
        priority: int = 50,
    ) -> UUID:
        await self.initialize()
        gid = uuid4()
        async with self._write_lock:
            await self._sqlite.create_goal(
                goal_id=gid,
                project_name=project_name,
                title=title,
                objective=objective,
                success_criteria=success_criteria or "",
                status=status,
                priority=priority,
            )
        return gid

    async def update_goal_status(self, *, goal_id: UUID, status: str) -> None:
        await self.initialize()
        async with self._write_lock:
            await self._sqlite.update_goal_status(goal_id=goal_id, status=status)

    async def list_goals(self, *, project_name: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        await self.initialize()
        return await self._sqlite.list_goals(project_name=project_name, limit=limit)

    async def enqueue_goal_task(
        self,
        *,
        goal_id: UUID,
        project_name: str,
        tool_name: str,
        args: dict[str, Any] | None = None,
        run_after_iso: str | None = None,
    ) -> UUID:
        await self.initialize()
        tid = uuid4()
        async with self._write_lock:
            await self._sqlite.enqueue_task(
                task_id=tid,
                goal_id=goal_id,
                project_name=project_name,
                tool_name=tool_name,
                args=args or {},
                run_after_iso=run_after_iso,
            )
        return tid

    async def claim_next_goal_task(self) -> dict[str, Any] | None:
        await self.initialize()
        return await self._sqlite.claim_next_task()

    async def complete_goal_task(self, *, task_id: str, status: str, result: dict[str, Any] | None = None, error: str = "") -> None:
        await self.initialize()
        async with self._write_lock:
            await self._sqlite.complete_task(task_id=task_id, status=status, result=result, error=error)

    async def bump_goal_task_attempt(self, *, task_id: str, attempts: int, run_after_iso: str, error: str) -> None:
        await self.initialize()
        async with self._write_lock:
            await self._sqlite.bump_task_attempt(task_id=task_id, attempts=attempts, run_after_iso=run_after_iso, error=error)

    async def list_goal_tasks(self, *, goal_id: str | None = None, project_name: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        await self.initialize()
        return await self._sqlite.list_tasks(goal_id=goal_id, project_name=project_name, limit=limit)

    async def create_proposal(
        self,
        *,
        goal_id: UUID,
        project_name: str,
        suggestion: str,
        rationale: str = "",
        status: str = "pending",
    ) -> UUID:
        await self.initialize()
        pid = uuid4()
        async with self._write_lock:
            await self._sqlite.create_proposal(
                proposal_id=pid,
                goal_id=goal_id,
                project_name=project_name,
                suggestion=suggestion,
                rationale=rationale or "",
                status=status,
            )
        return pid

    async def latest_pending_proposal(self, *, project_name: str) -> dict[str, Any] | None:
        await self.initialize()
        return await self._sqlite.latest_pending_proposal(project_name=project_name)

    async def set_proposal_status(self, *, proposal_id: str, status: str) -> None:
        await self.initialize()
        async with self._write_lock:
            await self._sqlite.set_proposal_status(proposal_id=proposal_id, status=status)

    async def add_progress_event(self, *, goal_id: UUID, project_name: str, kind: str, message: str) -> None:
        await self.initialize()
        async with self._write_lock:
            await self._sqlite.add_progress_event(event_id=uuid4(), goal_id=goal_id, project_name=project_name, kind=kind, message=message)

    async def fetch_unacked_progress(self, *, project_name: str, limit: int = 10) -> list[dict[str, Any]]:
        await self.initialize()
        return await self._sqlite.fetch_unacked_progress(project_name=project_name, limit=limit)
