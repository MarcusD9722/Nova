from __future__ import annotations

"""Tool selector — put 3-8 plausible tools in front of the model, not 49.

`ToolLoopExecutor` embeds every registered tool's name and description in
*every* `decide()` prompt, and `decide()` runs up to `step_budget` (6) times per
turn. With 49 tools registered in core/tooling.py alone, that is
`O(all_tools x steps)` prompt tokens spent every turn on capabilities the turn
will never touch — and it gets linearly worse with each plugin, MCP server or
CAD tool added later.

So a preselection stage runs first, in three tiers ordered by cost:

  1. **Deterministic.** "what time is it" needs no reasoning to route. A small
     set of high-confidence patterns, each of which either fires or abstains.
  2. **Semantic.** Cosine similarity between the turn and cached tool-capability
     vectors. Tool descriptions are static, so their vectors are computed once
     per (tool, content-hash, embedding-model) and reused forever after. Only
     the query is embedded per turn.
  3. **Ambiguity fallback.** When the semantic scores are flat, widen rather
     than guess. There is a hook for a small LLM router, but it is optional and
     off by default — the previous latency work established that an extra model
     call per turn is expensive, and a slightly longer candidate list is much
     cheaper than another generation.

Two invariants, both enforced by tests:

  * **Recall beats precision.** Excluding the tool the turn needed is a silent
    capability loss and is far worse than including one extra candidate. Every
    ambiguous path widens the list.
  * **Fail open.** Any failure anywhere — no embedding model, a bad cache, an
    exception — returns the full tool list. A broken selector must degrade to
    today's behaviour, never to a crippled assistant.

The selector never touches permissions. Candidates flow to the model, the
model's choice flows to `ToolRouter`, and `ToolRouter` remains the only place
that decides whether a call is allowed.
"""

import hashlib
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

from core.logging_setup import get_logger

logger = get_logger(__name__)

DEFAULT_MAX_CANDIDATES = int(os.getenv("NOVA_TOOL_CANDIDATES", "8").strip() or "8")

#: Always offered, regardless of the query. These are the tools whose absence
#: would be silently wrong — memory in particular, because a turn that should
#: have saved something and did not leaves no trace of the omission.
CORE_TOOLS = ("memory.remember", "memory.recall")

#: Deterministic routes. Each entry is (compiled pattern, tools it implies).
#: Conservative by construction: a pattern that is not certain does not belong
#: here, because a wrong deterministic hit skips the semantic stage entirely.
_RULES: tuple[tuple[re.Pattern[str], tuple[str, ...]], ...] = (
    (re.compile(r'\b(what(?:\'s| is)? the )?(time|clock)\b.*\?|\bwhat time is it\b', re.I),
     ("time.now",)),
    (re.compile(r'\b(weather|forecast|temperature|rain|snow|jacket|umbrella)\b', re.I),
     ("weather.current", "weather.forecast")),
    (re.compile(r'\bset (?:a |an )?(timer|alarm|reminder)\b|\bremind me\b', re.I),
     ("reminder.create",)),
    (re.compile(r'\b(remember|note) that\b|\bdon\'t forget\b', re.I),
     ("memory.remember",)),
    (re.compile(r'\b(what do you remember|what did i tell you|do you remember)\b', re.I),
     ("memory.recall",)),
    (re.compile(r'\b(my )?reminders\b|\bwhat.{0,12}reminders\b', re.I),
     ("reminder.create", "memory.recall")),
    (re.compile(r'\b(directions?|how (?:long|far)|drive|route|traffic|navigate)\b.*'
                r'\b(to|from)\b', re.I),
     ("maps.directions",)),
    (re.compile(r'\b(search|google|look up|find out|latest news)\b', re.I),
     ("web.search",)),
    (re.compile(r'\b(build|create|start|make|scaffold)\b.{0,24}\b(new |another )'
                r'.{0,24}\b(app|project|game|site|website|tool|script|program)\b', re.I),
     ("project.start_build",)),
    (re.compile(r'\b(your own|yourself|nova\'s own)\b.{0,20}\b(source|code)\b'
                r'|\byour source code\b', re.I),
     ("self.read_code", "self.list_code", "self.propose_change")),
)

#: Turns that need no tool at all. Allowlist; anything unrecognised is not
#: treated as conversational.
_NO_TOOL = re.compile(
    r'^\s*(?:'
    r'(?:hey|hi|hello|good (?:morning|afternoon|evening|night)|thanks|thank you|'
    r'how are you|what\'s up|tell me a joke|goodnight|bye|cheers|ok|okay|nice|cool)'
    r'[\s,!.?]*)+$',
    re.I,
)

_TOKEN = re.compile(r"[a-z0-9]+")


@dataclass
class Selection:
    tools: list[str]
    stage: str                      # deterministic | semantic | widened | all
    reason: str = ""
    scores: dict[str, float] = field(default_factory=dict)
    elapsed_ms: float = 0.0

    def __contains__(self, name: object) -> bool:
        return name in self.tools

    def __len__(self) -> int:
        return len(self.tools)


class ToolEmbeddingCache:
    """Vectors for tool capability text, keyed by content and model identity.

    A tool's description changes only when someone edits it, so re-embedding
    every tool on every turn (the naive implementation) is pure waste. The key
    includes a hash of the text and the embedding model's identity, so a changed
    description or a swapped model invalidates exactly the affected entries and
    nothing else.
    """

    def __init__(self, embed: Callable[[list[str]], list[list[float]]] | None = None,
                 model_id: str | None = None, *, enabled: bool = True) -> None:
        self._embed = embed
        #: False pins the selector to lexical ranking. Distinct from "the model
        #: is not loaded yet" (transient) and from "the backend raised"
        #: (fail open) — this is a deliberate configuration.
        self._enabled = enabled
        self._model_id = model_id or os.getenv("NOVA_EMBED_MODEL", "BAAI/bge-small-en-v1.5")
        self._vectors: dict[str, list[float]] = {}   # cache key -> vector
        self._keys: dict[str, str] = {}              # tool name -> cache key
        self.hits = 0
        self.misses = 0

    def _key(self, name: str, text: str) -> str:
        digest = hashlib.sha256(f"{text}\x00{self._model_id}".encode("utf-8")).hexdigest()[:32]
        return f"{name}:{digest}"

    def _embedder(self) -> Callable[[list[str]], list[list[float]]] | None:
        """The embedding function, but only if it costs nothing to get.

        Deliberately uses `embedding_loaded()` rather than
        `embedding_available()`. The latter *loads* bge-small on first call,
        which measured 7.3 s on this machine and blew the turn-latency budget in
        tests/test_it_chat_pipeline.py — selection sits in front of every turn,
        so it must never be the thing that pays for a model load. Instead the
        load is kicked off in the background and this turn ranks lexically,
        which widens the candidate list and so costs precision, never recall.
        """
        if not self._enabled:
            return None
        if self._embed is not None:
            return self._embed
        try:
            from memory.embeddings import embed_texts, embedding_loaded, warm_in_background

            if not embedding_loaded():
                warm_in_background()
                return None
            return embed_texts
        except Exception:  # noqa: BLE001
            return None

    def sync(self, descriptions: dict[str, str]) -> int:
        """Ensure every tool has a current vector. Returns how many were computed."""
        embed = self._embedder()
        if embed is None:
            return 0

        pending: list[tuple[str, str]] = []
        for name, desc in descriptions.items():
            text = f"{name}: {desc}".strip()
            key = self._key(name, text)
            if key in self._vectors:
                self._keys[name] = key
                self.hits += 1
                continue
            pending.append((name, text))

        if not pending:
            return 0

        vectors = embed([t for _, t in pending])
        for (name, text), vec in zip(pending, vectors):
            key = self._key(name, text)
            self._vectors[key] = vec
            self._keys[name] = key
            self.misses += 1

        # Drop vectors for tools that no longer exist or were re-described.
        live = set(self._keys.values())
        for key in [k for k in self._vectors if k not in live]:
            self._vectors.pop(key, None)
        return len(pending)

    def vector(self, name: str) -> list[float] | None:
        key = self._keys.get(name)
        return self._vectors.get(key) if key else None

    def embed_query(self, text: str) -> list[float] | None:
        embed = self._embedder()
        if embed is None:
            return None
        try:
            return embed([text])[0]
        except Exception:  # noqa: BLE001
            return None

    def stats(self) -> dict[str, Any]:
        return {"cached_vectors": len(self._vectors), "tools": len(self._keys),
                "hits": self.hits, "misses": self.misses, "model": self._model_id}


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    # memory/embeddings.py returns normalised vectors, so the dot product is
    # already cosine. Normalising again would be wasted work per tool per turn.
    return float(dot)


#: Words that carry no routing signal. Without this filter a query like "look at
#: your own source and tell me how memory works" scores every memory.* tool as
#: highly as self.read_code, because "look", "tell" and "how" appear in half the
#: descriptions — a measured recall failure before this was added.
_NOISE = frozenset("""
a an the and or but if then than that this these those is are was was were be been am
do does did doing have has had having i you he she it we they me him her us them my your
his its our their to of in on at by for with about from as so not no yes what which who
when where why how can could will would should may might must please just now also very
really tell show give get make take see look need want know
""".split())


def _lexical_score(query: str, name: str, desc: str) -> float:
    """Cheap fallback when no embedding model is available.

    Not a replacement for semantic matching — it exists so that losing the
    embedding model degrades the *ranking* rather than removing selection
    entirely. Tool names are weighted above descriptions because they are
    written to be read ("self.read_code" says what it does), while descriptions
    share a lot of generic vocabulary with each other.
    """
    q = {w for w in _TOKEN.findall(query.lower()) if w not in _NOISE and len(w) > 2}
    if not q:
        return 0.0

    name_words = {w for w in _TOKEN.findall(name.lower()) if len(w) > 2}
    desc_words = {w for w in _TOKEN.findall(desc.lower()) if w not in _NOISE and len(w) > 2}
    if not (name_words or desc_words):
        return 0.0

    score = len(q & name_words) * 2.0 + len(q & desc_words) * 1.0
    return score / (2.0 * len(q))


class ToolSelector:
    """Narrow the tool list before the agent loop reasons over it."""

    def __init__(
        self,
        *,
        max_candidates: int = DEFAULT_MAX_CANDIDATES,
        cache: ToolEmbeddingCache | None = None,
        core_tools: Iterable[str] = CORE_TOOLS,
        # Scores are mean-centred (see _score), so 0.0 means "exactly average
        # relevance". The floor sits just below that: a tool must be at least
        # about as relevant as the median to be offered.
        semantic_floor: float = -0.01,
        ambiguity_margin: float = 0.04,
        router_llm: Callable[[str, list[str]], list[str]] | None = None,
    ) -> None:
        self.max_candidates = max_candidates
        self.cache = cache or ToolEmbeddingCache()
        self.core_tools = tuple(core_tools)
        self.semantic_floor = semantic_floor
        self.ambiguity_margin = ambiguity_margin
        #: Optional stage-3 router. Off by default: an extra model call on every
        #: turn is exactly the cost the previous latency round removed.
        self.router_llm = router_llm
        self.stats = {"deterministic": 0, "semantic": 0, "widened": 0, "all": 0, "failed_open": 0}
        #: Whether the last scoring pass had real vectors. Recorded rather than
        #: assumed, so a machine where bge-small failed to load takes the wider,
        #: recall-safe path instead of trusting word overlap.
        self._has_embeddings = False

    def select(self, query: str, descriptions: dict[str, str], *,
               context: str = "") -> Selection:
        """Return the tools worth showing the model for this turn."""
        started = time.perf_counter()
        all_tools = sorted(descriptions)
        try:
            selection = self._select(query, descriptions, context=context)
        except Exception as e:  # noqa: BLE001
            # A broken selector must degrade to today's behaviour, not to a
            # crippled assistant.
            logger.warning("tool_selector_failed_open", error=str(e)[:200])
            self.stats["failed_open"] += 1
            selection = Selection(tools=all_tools, stage="all",
                                  reason=f"selector error, showing everything: {type(e).__name__}")
        selection.elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)
        self.stats[selection.stage] = self.stats.get(selection.stage, 0) + 1
        return selection

    def _select(self, query: str, descriptions: dict[str, str], *, context: str) -> Selection:
        known = set(descriptions)
        q = (query or "").strip()
        if not q:
            return Selection(tools=sorted(known), stage="all", reason="empty query")

        # ── Stage 1: deterministic ───────────────────────────────────────────
        if _NO_TOOL.match(q):
            # Still offer the core set. "Good morning, remember I'm off today"
            # is social AND a memory write, and dropping memory here would lose
            # it silently.
            picked = [t for t in self.core_tools if t in known]
            return Selection(tools=picked, stage="deterministic",
                             reason="purely conversational turn")

        hits: list[str] = []
        for pattern, tools in _RULES:
            if pattern.search(q):
                hits.extend(t for t in tools if t in known)
        if hits:
            picked = self._finalise(hits, known)
            return Selection(tools=picked, stage="deterministic",
                             reason="matched a high-confidence intent pattern")

        # ── Stage 2: semantic ────────────────────────────────────────────────
        scores = self._score(q, descriptions, context=context)
        if not scores:
            return Selection(tools=sorted(known), stage="all",
                             reason="no ranking signal available")

        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        top = ranked[0][1]

        if top <= 0.0:
            # Nothing scored above nothing: the ranking has no opinion at all,
            # so every ordering is arbitrary and cutting to the "top" 8 would be
            # picking tools at random. No signal means no basis to exclude.
            return Selection(tools=sorted(known), stage="all",
                             reason="ranking found no signal; showing everything")

        # Without an embedding model the ranking is lexical only, and lexical
        # ranking is not confident enough to justify a tight cut. Take the top
        # `max_candidates` outright rather than trusting a floor calibrated for
        # cosine scores — still an 80%+ reduction, without betting recall on
        # word overlap.
        if not self._has_embeddings:
            picked = self._finalise([n for n, _ in ranked[: self.max_candidates]], known)
            return Selection(tools=picked, stage="widened", scores=dict(ranked[:12]),
                             reason="no embedding model; lexical ranking widened for recall")

        above = [name for name, s in ranked if s >= self.semantic_floor]
        if not above:
            # Nothing looked relevant. That is not evidence no tool is needed —
            # it is evidence the ranking did not understand the turn, so widen.
            picked = self._finalise([n for n, _ in ranked[: self.max_candidates]], known)
            return Selection(tools=picked, stage="widened", scores=dict(ranked[:12]),
                             reason="no tool scored above the floor; widened rather than guessed")

        # A flat distribution near the top means the ranking is not confident.
        contenders = [n for n, s in ranked if top - s <= self.ambiguity_margin]
        if len(contenders) > max(3, self.max_candidates // 2):
            if self.router_llm is not None:
                try:
                    chosen = [t for t in self.router_llm(q, contenders) if t in known]
                    if chosen:
                        return Selection(tools=self._finalise(chosen, known), stage="semantic",
                                         scores=dict(ranked[:12]),
                                         reason="LLM router broke a tie")
                except Exception as e:  # noqa: BLE001
                    logger.debug("tool_router_llm_failed", error=str(e)[:160])
            picked = self._finalise([n for n, _ in ranked[: self.max_candidates]], known)
            return Selection(tools=picked, stage="widened", scores=dict(ranked[:12]),
                             reason=f"{len(contenders)} tools scored within the margin")

        picked = self._finalise(above[: self.max_candidates], known)
        return Selection(tools=picked, stage="semantic", scores=dict(ranked[:12]),
                         reason=f"top score {top:.3f}")

    def _score(self, query: str, descriptions: dict[str, str], *, context: str) -> dict[str, float]:
        text = f"{context}\n{query}".strip() if context else query
        self.cache.sync(descriptions)
        qvec = self.cache.embed_query(text)
        self._has_embeddings = qvec is not None

        lexical = {name: _lexical_score(text, name, desc) for name, desc in descriptions.items()}
        if qvec is None:
            return lexical

        raw = {name: (_cosine(qvec, self.cache.vector(name) or []) if self.cache.vector(name) else 0.0)
               for name in descriptions}

        # Centre the cosine scores. bge-small puts almost every short English
        # string in a narrow high band (~0.6-0.9 against anything), so the
        # ABSOLUTE similarity is nearly uninformative while the RELATIVE
        # position is not. Subtracting the mean turns "everything looks 0.75"
        # into a usable ranking signal; without this, a query about Nova's own
        # source code ranks six memory tools above self.read_code purely on
        # baseline similarity.
        mean = sum(raw.values()) / len(raw) if raw else 0.0

        # Lexical agreement lifts the centred score rather than replacing it:
        # tool names are written to be readable, so a name-word match is real
        # evidence, but it must not outvote a strong semantic match on its own.
        return {name: (raw[name] - mean) + 0.5 * lexical[name] for name in descriptions}

    def _finalise(self, picked: Sequence[str], known: set[str]) -> list[str]:
        """Add the core tools and de-duplicate, preserving rank order."""
        out: list[str] = []
        for name in list(picked) + [t for t in self.core_tools if t in known]:
            if name in known and name not in out:
                out.append(name)
        return out

    def describe(self) -> dict[str, Any]:
        return {"max_candidates": self.max_candidates, "stages": dict(self.stats),
                "embedding_cache": self.cache.stats(),
                "llm_router": self.router_llm is not None}
