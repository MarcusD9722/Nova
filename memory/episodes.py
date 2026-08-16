from __future__ import annotations

"""WARM tier: what happened, durably.

Nova already remembers what is TRUE (facts, in `facts`) and what is on screen
(hot artifacts, in memory/artifacts.py). What she had no representation for is
what HAPPENED — and the three are genuinely different:

    FACT      Marcus owns an RTX 5080.
    EPISODE   On 2026-07-28 Marcus compared water-cooling vs overclocking it.
    ARTIFACT  The benchmark output that comparison was based on.

Collapsing them loses information in both directions: a fact cannot say when or
why it was learned, and an episode flattened into a fact ("Marcus compared
cooling options") loses the evidence chain that makes it checkable.

The warm record is deliberately small. Its job is to let Nova decide whether an
old episode is worth looking at WITHOUT loading the evidence — summary,
entities, project, trust, freshness, importance. Cold hydration happens only
after something has been shortlisted.

Three invariants that are load-bearing elsewhere and are enforced here:

  * **Trust never launders.** An artifact stored as UNTRUSTED_EXTERNAL comes
    back UNTRUSTED_EXTERNAL after persistence, restart and retrieval. There is
    no path in this module that upgrades a trust class.
  * **Freshness survives.** A stored price stays a stored price with a
    timestamp, not a current one. `stale_fields()` on the rehydrated artifact
    behaves exactly as it did in hot memory.
  * **Provenance survives.** MCP server, remote tool, arguments and schema hash
    persist as structured provenance, not flattened into prose.
"""

import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import aiosqlite
from contextlib import asynccontextmanager

from core.logging_setup import get_logger
from memory.artifacts import Artifact, FRESH_SESSION, TRUST_TOOL_RESULT
from memory.cold_store import ColdStore

logger = get_logger(__name__)

# Episode kinds. Deliberately a small closed-ish vocabulary: an open string
# field would drift into a hundred near-synonyms within a month.
EP_TOOL_RESULT = "tool_result"
EP_MCP_RESULT = "mcp_result"
EP_DECISION = "decision"
EP_PROJECT = "project_event"
EP_ACTION = "action"
EP_FAILURE = "failure"
EP_PREFERENCE = "preference"
EP_CONVERSATION = "conversation"
#: V3 P4.2. A SELECTION is what Marcus chose out of something Nova showed him;
#: a CORRECTION is Nova learning that what she believed was wrong. Both are
#: events, which is why neither is a fact: "Marcus prefers WD Gold" is a fact,
#: "Marcus chose the WD Gold from a three-drive comparison on the 14th" is not.
EP_SELECTION = "selection"
EP_CORRECTION = "correction"

#: Kinds a cleanup job may never delete by age. Quietly forgetting a choice or
#: a correction is the same class of failure as forgetting a decision.
PROTECTED_KINDS = (EP_DECISION, EP_PROJECT, EP_PREFERENCE, EP_SELECTION, EP_CORRECTION)

#: Payload above this size goes to cold storage instead of the warm row.
WARM_PAYLOAD_LIMIT = 2000

#: Position of `cold_ref` in the artifact row tuple built by `_artifact_row`.
#: Named rather than inlined because a column added before it would otherwise
#: silently start copying the wrong value onto episodes.
_COLD_REF_COL = 16


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _loads(raw: Any, default):
    if raw in (None, ""):
        return default
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return default


@dataclass
class Episode:
    """One durable thing that happened."""

    id: str
    kind: str
    summary: str
    entities: list[str] = field(default_factory=list)
    conversation_id: str | None = None
    project: str | None = None
    source_tool: str | None = None
    trust: str = TRUST_TOOL_RESULT
    freshness: str = FRESH_SESSION
    provenance: dict[str, Any] = field(default_factory=dict)
    outcome: str | None = None
    importance: float = 0.5
    access_count: int = 0
    last_accessed_at: str | None = None
    superseded_by: str | None = None
    created_at: str = field(default_factory=_now_iso)
    #: Whose episode this is (V3 P5.1e). `user` for Marcus, `speaker:<id>` for a
    #: known guest, `unverified` when Nova could not attribute the turn, and
    #: `system` for things that happened without a human saying them. Structured
    #: on purpose: read scoping must never be decided by parsing summary prose.
    speaker_entity: str = "user"
    speaker_label: str = ""
    input_source: str = "typed"

    @classmethod
    def from_row(cls, row) -> "Episode":
        return cls(
            id=row["id"], kind=row["kind"], summary=row["summary"],
            entities=_loads(row["entities"], []),
            conversation_id=row["conversation_id"], project=row["project"],
            source_tool=row["source_tool"], trust=row["trust"], freshness=row["freshness"],
            provenance=_loads(row["provenance"], {}), outcome=row["outcome"],
            importance=float(row["importance"] or 0.0),
            access_count=int(row["access_count"] or 0),
            last_accessed_at=row["last_accessed_at"],
            speaker_entity=_row_get(row, "speaker_entity", "user"),
            speaker_label=_row_get(row, "speaker_label", ""),
            input_source=_row_get(row, "input_source", "typed"),
            superseded_by=row["superseded_by"], created_at=row["created_at"],
        )

    def age_days(self, now: float | None = None) -> float:
        try:
            created = datetime.fromisoformat(self.created_at.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return 0.0
        ref = datetime.fromtimestamp(now, tz=timezone.utc) if now else datetime.now(timezone.utc)
        return max(0.0, (ref - created).total_seconds() / 86400.0)


@dataclass
class Decision:
    """An architectural decision, with the reasoning that produced it.

    Exists so that "why is it built this way" is answerable from memory rather
    than by re-deriving intent from the implementation — which is how a good
    decision gets quietly undone.
    """

    id: str
    title: str
    decision: str
    rationale: str = ""
    evidence: list[str] = field(default_factory=list)
    alternatives: list[str] = field(default_factory=list)
    subsystem: str | None = None
    status: str = "active"          # active | superseded
    supersedes: str | None = None
    superseded_by: str | None = None
    source_refs: list[str] = field(default_factory=list)
    constraints: str = ""           # e.g. "model-specific; re-measure on a swap"
    decided_at: str = field(default_factory=_now_iso)
    created_at: str = field(default_factory=_now_iso)

    @classmethod
    def from_row(cls, row) -> "Decision":
        return cls(
            id=row["id"], title=row["title"], decision=row["decision"],
            rationale=row["rationale"], evidence=_loads(row["evidence"], []),
            alternatives=_loads(row["alternatives"], []), subsystem=row["subsystem"],
            status=row["status"], supersedes=row["supersedes"],
            superseded_by=row["superseded_by"], source_refs=_loads(row["source_refs"], []),
            constraints=row["constraints"] or "", decided_at=row["decided_at"],
            created_at=row["created_at"],
        )


def _row_get(row, key: str, default):
    """Read a column that may be absent from an older row or a partial SELECT."""
    try:
        val = row[key]
    except (IndexError, KeyError):
        return default
    return default if val is None else val


class EpisodicStore:
    """Warm persistence over Nova's existing SQLite database.

    Same file, same transaction semantics, same backup story as facts. A second
    database would have bought nothing except another thing to keep consistent.
    """

    def __init__(self, db_path: Path, cold: ColdStore | None = None) -> None:
        self._db_path = Path(db_path)
        self.cold = cold or ColdStore(self._db_path.parent)

    @asynccontextmanager
    async def _conn(self):
        """One short-lived connection per operation.

        Must be used as `async with self._conn() as db`, NOT
        `async with await ...`: an aiosqlite Connection is both awaitable and an
        async context manager, so doing both starts its worker thread twice and
        raises "threads can only be started once".
        """
        async with aiosqlite.connect(str(self._db_path)) as db:
            db.row_factory = aiosqlite.Row
            yield db

    # ── episodes ─────────────────────────────────────────────────────────────

    _EPISODE_SQL = """INSERT OR REPLACE INTO episodes
                   (id, kind, summary, entities, conversation_id, project, source_tool,
                    trust, freshness, provenance, outcome, importance, access_count,
                    last_accessed_at, superseded_by, created_at,
                    speaker_entity, speaker_label, input_source)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""

    def _episode_row(self, ep: Episode) -> tuple:
        return (ep.id, ep.kind, ep.summary, json.dumps(ep.entities), ep.conversation_id,
                ep.project, ep.source_tool, ep.trust, ep.freshness,
                json.dumps(ep.provenance, default=str), ep.outcome, ep.importance,
                ep.access_count, ep.last_accessed_at, ep.superseded_by, ep.created_at,
                ep.speaker_entity or "user", ep.speaker_label or "",
                ep.input_source or "typed")

    async def record_episode(self, ep: Episode) -> str:
        async with self._conn() as db:
            await db.execute(self._EPISODE_SQL, self._episode_row(ep))
            await db.commit()
        return ep.id

    async def record_happening(self, ep: Episode, parent: Artifact | None = None,
                               children: Iterable[Artifact] = ()) -> int:
        """Write one thing that happened — episode and its evidence — atomically.

        The episode and its result set are a single fact about the world, and
        splitting them across transactions creates a state that is worse than
        either: an episode whose `provenance.artifact_id` points at rows that a
        crash prevented from ever existing. Retrieval would then rank it, offer
        it, and resolve an ordinal against nothing.

        Returns the number of rows written.
        """
        rows = []
        if parent is not None:
            rows.append(self._artifact_row(parent, episode_id=ep.id))
            rows.extend(self._artifact_row(c, episode_id=ep.id) for c in children)

        # Carry any cold digest up onto the WARM row. Retrieval decides whether
        # to hydrate by looking at the episode — it has not loaded the artifacts
        # at that point, and loading them to find out whether there is anything
        # worth loading would defeat the tiering. Without this the evidence is
        # on disk, correctly written, and permanently unreachable: measured on
        # the real path, cold hydration never fired once.
        cold_refs = [r[_COLD_REF_COL] for r in rows if r[_COLD_REF_COL]]
        if cold_refs and not ep.provenance.get("cold_ref"):
            ep.provenance = {**ep.provenance, "cold_ref": cold_refs[0],
                             "cold_refs": cold_refs[:8]}

        async with self._conn() as db:
            await db.execute(self._EPISODE_SQL, self._episode_row(ep))
            if rows:
                await db.executemany(self._ARTIFACT_SQL, rows)
            await db.commit()
        return 1 + len(rows)

    async def get_episode(self, episode_id: str) -> Episode | None:
        async with self._conn() as db:
            async with db.execute("SELECT * FROM episodes WHERE id = ?", (episode_id,)) as cur:
                row = await cur.fetchone()
        return Episode.from_row(row) if row else None

    async def recent_episodes(self, *, limit: int = 20, project: str | None = None,
                              kind: str | None = None) -> list[Episode]:
        sql = "SELECT * FROM episodes WHERE superseded_by IS NULL"
        params: list[Any] = []
        if project:
            sql += " AND project = ?"
            params.append(project)
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(int(limit))
        async with self._conn() as db:
            async with db.execute(sql, params) as cur:
                rows = await cur.fetchall()
        return [Episode.from_row(r) for r in rows]

    async def search_episodes(self, terms: Iterable[str], *, limit: int = 60,
                              project: str | None = None,
                              include_superseded: bool = False) -> list["Episode"]:
        """Candidates drawn by RELEVANCE, not recency.

        The obvious implementation — take the N most recent episodes and rank
        them — silently cannot answer "what did we decide about the server last
        month?" once a few hundred newer episodes exist. The relevant record is
        simply never a candidate.

        So matching happens in SQL against summary and entities, and recency is
        only a tie-breaker within the matches. Still bounded: LIKE over a few
        thousand small rows is cheap, and the result set is capped.
        """
        words = [w for w in {str(t).strip().lower() for t in terms} if len(w) > 2]
        if not words:
            return []
        # Prefix match so "drive" finds "drives"; the caller has already stemmed.
        words = words[:12]
        clauses = " OR ".join(["(LOWER(summary) LIKE ? OR LOWER(entities) LIKE ?)"] * len(words))
        params: list[Any] = []
        for w in words:
            params.extend([f"%{w}%", f"%{w}%"])
        # Replaced decisions are excluded by default: a choice that was changed
        # must not compete with the one that changed it. `include_superseded`
        # is the narrow opt-in for "what did I originally pick?".
        scope = "1=1" if include_superseded else "superseded_by IS NULL"
        sql = (f"SELECT * FROM episodes WHERE {scope} AND ({clauses})")
        if project:
            sql += " AND project = ?"
            params.append(project)
        sql += " ORDER BY importance DESC, created_at DESC LIMIT ?"
        params.append(int(limit))
        async with self._conn() as db:
            async with db.execute(sql, params) as cur:
                rows = await cur.fetchall()
        return [Episode.from_row(r) for r in rows]

    async def supersede_selections(self, *, parent_id: str,
                                   keep_episode_id: str) -> int:
        """Changing your mind replaces the earlier choice; it does not erase it.

        Scope is the RESULT SET (`parent_id`), not the artifact type. Choosing a
        drive and choosing a monitor are two live decisions; choosing a drive and
        then a different drive from the SAME comparison is one decision that
        changed. Anything wider would have the monitor silently retire the drive.

        The old episode is marked, never deleted — `superseded_by` is what makes
        "what did I originally pick, before I changed my mind?" answerable at
        all. Normal retrieval already filters `superseded_by IS NULL`, so the
        replaced choice stops competing with the current one without vanishing.
        """
        if not parent_id or not keep_episode_id:
            return 0
        async with self._conn() as db:
            cur = await db.execute(
                """UPDATE episodes SET superseded_by = ?
                   WHERE kind = ? AND id != ? AND superseded_by IS NULL
                     AND provenance LIKE ?""",
                (keep_episode_id, EP_SELECTION, keep_episode_id,
                 f'%"parent_id": "{parent_id}"%'))
            await db.commit()
            return cur.rowcount or 0

    async def touch_episodes(self, episode_ids: Iterable[str]) -> None:
        """Reinforce several episodes in ONE transaction.

        Retrieval touches its whole shortlist; doing that one connection at a
        time put a measurable write cost on the read path.
        """
        ids = [str(i) for i in episode_ids if i]
        if not ids:
            return
        now = _now_iso()
        async with self._conn() as db:
            await db.executemany(
                "UPDATE episodes SET access_count = access_count + 1, last_accessed_at = ? "
                "WHERE id = ?", [(now, i) for i in ids])
            await db.commit()

    async def touch_episode(self, episode_id: str) -> None:
        """Access reinforcement — an episode Marcus keeps returning to matters."""
        async with self._conn() as db:
            await db.execute(
                "UPDATE episodes SET access_count = access_count + 1, last_accessed_at = ? "
                "WHERE id = ?", (_now_iso(), episode_id))
            await db.commit()

    # ── artifacts ────────────────────────────────────────────────────────────

    _ARTIFACT_SQL = """INSERT OR REPLACE INTO artifacts
                   (id, conversation_id, turn_id, episode_id, parent_id, item_index,
                    artifact_type, summary, payload, source_tool, trust, freshness,
                    provenance, importance, access_count, last_accessed_at, cold_ref,
                    active, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""

    def _artifact_row(self, art: Artifact, *, episode_id: str | None,
                      cold_payload: Any = None) -> tuple:
        """Build one artifact's row, sending heavy evidence cold on the way.

        Separated from the write so a result set can go into a single
        transaction. Cold storage is plain file IO and deliberately happens
        outside the database transaction — a blob write must not hold a SQLite
        lock open.

        Trust and freshness are copied verbatim. There is deliberately no
        parameter here that could change them: laundering an untrusted artifact
        into a trusted one must not be possible by calling this differently.
        """
        payload = dict(art.payload or {})
        cold_ref = None

        blob = cold_payload if cold_payload is not None else None
        encoded = json.dumps(payload, ensure_ascii=False, default=str)
        if blob is None and len(encoded) > WARM_PAYLOAD_LIMIT:
            # The payload itself is the heavy evidence: keep a trimmed version
            # warm for relevance decisions and push the full thing cold.
            blob = payload
            payload = {"_truncated": True,
                       **{k: str(v)[:200] for k, v in list(payload.items())[:8]}}

        if blob is not None:
            ref = self.cold.put(blob)
            if ref:
                cold_ref = ref["digest"]

        return (art.artifact_id, art.conversation_id, art.turn_id, episode_id,
                art.parent_id, art.item_index, art.artifact_type, art.summary,
                json.dumps(payload, ensure_ascii=False, default=str), art.source_tool,
                art.trust, art.freshness,
                json.dumps(art.provenance, default=str), art.importance,
                art.access_count, art.last_accessed,
                cold_ref, 1 if art.active else 0,
                datetime.fromtimestamp(art.created_at, tz=timezone.utc).isoformat())

    async def persist_artifact(self, art: Artifact, *, episode_id: str | None = None,
                               cold_payload: Any = None) -> str:
        """Persist one artifact. Large evidence goes cold; the row stays small."""
        row = self._artifact_row(art, episode_id=episode_id, cold_payload=cold_payload)
        async with self._conn() as db:
            await db.execute(self._ARTIFACT_SQL, row)
            await db.commit()
        return art.artifact_id

    async def persist_result_set(self, parent: Artifact, children: Iterable[Artifact],
                                 *, episode_id: str | None = None) -> int:
        """A result set is ONE write, not one per item.

        It used to be a loop over `persist_artifact`, which opened a fresh
        connection and committed separately for every row. Measured on the real
        promotion path that made a four-item set cost ~147 ms — four connection
        setups and four fsyncs to store one thing that happened. It is also the
        wrong transaction boundary: a crash midway through left a result set
        with some of its items, and a half-persisted ordered set is worse than
        none, because "the second one" would then quietly mean something else.
        """
        rows = [self._artifact_row(parent, episode_id=episode_id)]
        rows.extend(self._artifact_row(c, episode_id=episode_id) for c in children)
        async with self._conn() as db:
            await db.executemany(self._ARTIFACT_SQL, rows)
            await db.commit()
        return len(rows)

    async def load_artifact(self, artifact_id: str, *, hydrate: bool = False) -> Artifact | None:
        async with self._conn() as db:
            async with db.execute("SELECT * FROM artifacts WHERE id = ?", (artifact_id,)) as cur:
                row = await cur.fetchone()
        return self._artifact_from_row(row, hydrate=hydrate) if row else None

    async def load_children(self, parent_id: str, *, hydrate: bool = False) -> list[Artifact]:
        """Children in their ORIGINAL order — this is what makes 'the second
        one' answerable after a restart."""
        async with self._conn() as db:
            async with db.execute(
                "SELECT * FROM artifacts WHERE parent_id = ? ORDER BY item_index ASC",
                (parent_id,)) as cur:
                rows = await cur.fetchall()
        return [self._artifact_from_row(r, hydrate=hydrate) for r in rows]

    async def result_sets_for_conversation(self, conversation_id: str,
                                           *, limit: int = 10) -> list[Artifact]:
        async with self._conn() as db:
            async with db.execute(
                """SELECT * FROM artifacts
                   WHERE conversation_id = ? AND artifact_type = 'result_set'
                   ORDER BY created_at DESC LIMIT ?""",
                (str(conversation_id), int(limit))) as cur:
                rows = await cur.fetchall()
        return [self._artifact_from_row(r) for r in rows]

    def _artifact_from_row(self, row, *, hydrate: bool = False) -> Artifact:
        payload = _loads(row["payload"], {})
        if hydrate and row["cold_ref"]:
            full = self.cold.get(row["cold_ref"])
            if isinstance(full, dict):
                payload = full
            elif full is not None:
                payload = {"evidence": full}
            # A missing cold payload is NOT an error: the warm row is
            # self-sufficient and the caller keeps the trimmed payload.

        try:
            created = datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00"))
            created_ts = created.timestamp()
        except (ValueError, AttributeError):
            created_ts = time.time()

        return Artifact(
            artifact_id=row["id"], conversation_id=row["conversation_id"],
            turn_id=row["turn_id"] or "", artifact_type=row["artifact_type"],
            summary=row["summary"], payload=payload, source_tool=row["source_tool"] or "",
            parent_id=row["parent_id"], item_index=row["item_index"],
            # Written back exactly as stored. This is the trust-persistence
            # invariant in one line.
            trust=row["trust"], freshness=row["freshness"],
            provenance=_loads(row["provenance"], {}),
            created_at=created_ts, access_count=int(row["access_count"] or 0),
            last_accessed=row["last_accessed_at"], importance=float(row["importance"] or 0.5),
            active=bool(row["active"]),
        )

    # ── decisions ────────────────────────────────────────────────────────────

    async def record_decision(self, dec: Decision) -> str:
        async with self._conn() as db:
            await db.execute(
                """INSERT OR REPLACE INTO decisions
                   (id, title, decision, rationale, evidence, alternatives, subsystem,
                    status, supersedes, superseded_by, source_refs, constraints,
                    decided_at, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (dec.id, dec.title, dec.decision, dec.rationale,
                 json.dumps(dec.evidence), json.dumps(dec.alternatives), dec.subsystem,
                 dec.status, dec.supersedes, dec.superseded_by,
                 json.dumps(dec.source_refs), dec.constraints, dec.decided_at,
                 dec.created_at),
            )
            # Supersession marks the old decision, it does NOT delete it. The
            # history of why something changed is exactly the thing worth
            # keeping.
            if dec.supersedes:
                await db.execute(
                    "UPDATE decisions SET status = 'superseded', superseded_by = ? WHERE id = ?",
                    (dec.id, dec.supersedes))
            await db.commit()
        return dec.id

    async def get_decision(self, decision_id: str) -> Decision | None:
        async with self._conn() as db:
            async with db.execute("SELECT * FROM decisions WHERE id = ?", (decision_id,)) as cur:
                row = await cur.fetchone()
        return Decision.from_row(row) if row else None

    async def search_decisions(self, query: str, *, limit: int = 5,
                               include_superseded: bool = False) -> list[Decision]:
        """Find the decision behind a 'why is it like this' question.

        Lexical over title/decision/rationale/constraints. Deliberately simple:
        decisions number in the tens, not the thousands, and an embedding lookup
        here would add a model dependency to a question that word overlap
        answers well.
        """
        sql = "SELECT * FROM decisions"
        if not include_superseded:
            sql += " WHERE status = 'active'"
        async with self._conn() as db:
            async with db.execute(sql) as cur:
                rows = await cur.fetchall()

        terms = {t for t in _tokens(query) if len(t) > 2}
        if not terms:
            return []
        scored = []
        for row in rows:
            dec = Decision.from_row(row)
            hay = _tokens(f"{dec.title} {dec.decision} {dec.rationale} "
                          f"{dec.constraints} {dec.subsystem or ''}")
            overlap = len(terms & hay)
            if overlap:
                scored.append((overlap / len(terms), dec))
        scored.sort(key=lambda kv: kv[0], reverse=True)
        return [d for _s, d in scored[:limit]]

    async def all_decisions(self, *, include_superseded: bool = True) -> list[Decision]:
        sql = "SELECT * FROM decisions"
        if not include_superseded:
            sql += " WHERE status = 'active'"
        sql += " ORDER BY decided_at DESC"
        async with self._conn() as db:
            async with db.execute(sql) as cur:
                rows = await cur.fetchall()
        return [Decision.from_row(r) for r in rows]

    # ── stats / lifecycle ────────────────────────────────────────────────────

    async def stats(self) -> dict[str, Any]:
        async with self._conn() as db:
            out = {}
            for table in ("episodes", "artifacts", "decisions", "cold_evidence"):
                async with db.execute(f"SELECT COUNT(*) FROM {table}") as cur:
                    out[table] = (await cur.fetchone())[0]
        out["cold"] = self.cold.stats()
        return out

    async def prune_episodes(self, *, older_than_days: float = 180.0,
                             max_importance: float = 0.3,
                             keep_kinds: Iterable[str] = PROTECTED_KINDS) -> int:
        """Conservative pruning of low-value old episodes.

        Deliberately narrow: only unimportant, old, never-revisited episodes of
        kinds that are not decisions, project events or preferences. Nova must
        never quietly forget a decision or a stated preference because a
        cleanup job ran.
        """
        cutoff = datetime.now(timezone.utc).timestamp() - older_than_days * 86400
        cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
        placeholders = ",".join("?" for _ in keep_kinds)
        async with self._conn() as db:
            cur = await db.execute(
                f"""DELETE FROM episodes
                    WHERE created_at < ?
                      AND importance <= ?
                      AND access_count = 0
                      AND kind NOT IN ({placeholders})""",
                (cutoff_iso, max_importance, *keep_kinds))
            await db.commit()
            return cur.rowcount or 0


def _tokens(text: str) -> set[str]:
    import re
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))
