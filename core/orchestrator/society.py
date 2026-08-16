from __future__ import annotations

"""Persistent agent society (Goal #5, Phase 6).

Replaces the ephemeral reason→act chain with a roster of durable SPECIALISTS —
Chief Engineer, Research Scientist, Psychologist, Fitness Coach, ... — each with
a lasting specialization, reasoning style, and (persisted elsewhere) its own
memory, confidence, and experience. The Executive picks who participates; they
contribute in turn and a lead synthesizes.

Honest constraint (do not paper over): Nova runs ONE 9B model on ONE GPU,
serialized. So a "society" here is durable persona+memory+confidence bundles that
deliberate TURN BY TURN (sequential LLM calls), not concurrent minds. Genuine
parallel debate arrives with a second model/GPU — and ModelRouter (P2.4) already
makes that a config change. Selection and state are pure/deterministic and fully
tested; the LLM deliberation is the thin orchestration on top.
"""

from dataclasses import dataclass, field
import re

from core.logging_setup import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class Specialist:
    id: str
    name: str
    specialization: str
    reasoning_style: str
    persona: str            # system guidance for this specialist's contribution
    keywords: frozenset[str]  # routing signal (lowercase stems)
    coordinator: bool = False


# The roster. Keywords are matched as whole words against the query for routing.
SPECIALISTS: dict[str, Specialist] = {s.id: s for s in [
    Specialist(
        "chief_executive", "Chief Executive", "prioritization, coordination, decisions",
        "decisive, big-picture, weighs tradeoffs and picks a direction",
        "You coordinate the other specialists and make the final call. Be concise and decisive; "
        "surface the key tradeoff and recommend one path.",
        frozenset({"prioritize", "priority", "decide", "decision", "plan", "strategy", "coordinate", "tradeoff", "should"}),
        coordinator=True,
    ),
    Specialist(
        "chief_engineer", "Chief Engineer", "systems, infrastructure, debugging, reliability",
        "rigorous and systematic; reasons from failure modes and first principles",
        "You are a senior systems engineer. Focus on correctness, reliability, and concrete debugging steps.",
        frozenset({"bug", "error", "crash", "debug", "server", "infrastructure", "performance", "gpu", "memory", "deploy", "system"}),
    ),
    Specialist(
        "software_architect", "Software Architect", "software design, architecture, tradeoffs",
        "structured and pattern-oriented; thinks in interfaces and long-term maintainability",
        "You are a software architect. Focus on structure, boundaries, and design tradeoffs; avoid over-engineering.",
        frozenset({"architecture", "design", "refactor", "module", "interface", "pattern", "scalable", "structure", "api", "code"}),
    ),
    Specialist(
        "research_scientist", "Research Scientist", "research, analysis, evidence",
        "careful and evidence-driven; separates what's known from what's speculation, cites sources",
        "You are a research scientist. Ground claims in evidence, flag uncertainty, and never fabricate findings.",
        frozenset({"research", "study", "evidence", "paper", "data", "analyze", "compare", "why", "how", "science", "learn"}),
    ),
    Specialist(
        "creative_director", "Creative Director", "design, writing, aesthetics, ideas",
        "imaginative and taste-driven; generates and shapes ideas",
        "You are a creative director. Offer fresh, concrete creative directions and sharpen the aesthetic.",
        frozenset({"design", "creative", "idea", "brainstorm", "name", "logo", "story", "write", "aesthetic", "art", "video", "image"}),
    ),
    Specialist(
        "psychologist", "Psychologist", "wellbeing, motivation, stress, relationships",
        "empathetic and careful; supportive without overstepping or diagnosing",
        "You support wellbeing and motivation. Be warm and practical; never diagnose or replace professional care.",
        frozenset({"stress", "stressed", "anxious", "motivation", "burnout", "tired", "relationship", "feel", "overwhelmed", "wellbeing", "sleep"}),
    ),
    Specialist(
        "fitness_coach", "Fitness Coach", "exercise, health, training",
        "motivating and practical; gives actionable, safe guidance",
        "You are a fitness coach. Give practical, safe training guidance; defer to a doctor for medical issues.",
        frozenset({"workout", "exercise", "gym", "training", "fitness", "run", "lift", "diet", "muscle", "cardio", "health"}),
    ),
    Specialist(
        "snowboard_coach", "Snowboard Coach", "snowboarding technique and progression",
        "encouraging and technical; breaks skills into progressions",
        "You are a snowboard coach. Break technique into clear progressions and safety notes.",
        frozenset({"snowboard", "snowboarding", "carve", "carving", "slope", "powder", "mountain", "ride", "board", "trick", "park"}),
    ),
    Specialist(
        "media_curator", "Media Curator", "movies, shows, music recommendations",
        "knowledgeable and taste-aware; matches recommendations to preferences",
        "You curate media. Recommend based on stated tastes; explain why each pick fits.",
        frozenset({"movie", "film", "show", "series", "watch", "music", "song", "album", "recommend", "playlist", "stream"}),
    ),
    Specialist(
        "financial_planner", "Financial Planner", "budgeting, saving, financial education",
        "prudent and educational; general planning only",
        "You give GENERAL budgeting and financial-education guidance only. You are NOT a licensed advisor: never give "
        "personalized investment advice or recommend specific trades/securities — say so and suggest a professional.",
        frozenset({"budget", "save", "saving", "spending", "money", "finance", "financial", "debt", "expense", "retirement", "income"}),
    ),
    Specialist(
        "security_specialist", "Security Specialist", "security, privacy, threats",
        "cautious and defensive; threat-models and prefers least-privilege",
        "You are a security specialist. Threat-model, prefer least-privilege and defensive measures; flag risks plainly.",
        frozenset({"security", "secure", "privacy", "password", "token", "vulnerability", "threat", "encrypt", "attack", "auth", "leak"}),
    ),
]}

COORDINATOR_ID = "chief_executive"


def _stems(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    # crude singularization so "bugs"/"movies" match "bug"/"movie"
    return {w[:-1] if len(w) > 3 and w.endswith("s") else w for w in words}


def _addressee() -> str:
    """Who the council's synthesis is being written for (V3 P5.1d.4).

    Owner keeps the original wording exactly. A known guest is named. Anyone
    unrecognised gets neutral phrasing rather than a guess — and in no case is
    a non-owner described as Marcus.
    """
    try:
        from core.turn_identity import current_identity
    except Exception:  # noqa: BLE001
        return "Marcus"
    ident = current_identity()
    if ident.is_owner:
        return "Marcus"
    if ident.is_known_other and ident.display_name:
        return ident.display_name
    return "the current speaker"


def select_specialists(query: str, *, max_participants: int = 3) -> list[str]:
    """Executive routing (deterministic core): score each specialist by keyword
    overlap with the query, return the top matches (ids). The coordinator is
    added when 2+ specialists engage (someone has to synthesize). Falls back to
    the coordinator alone when nothing matches."""
    q = _stems(query)
    scored: list[tuple[int, str]] = []
    for sid, spec in SPECIALISTS.items():
        if spec.coordinator:
            continue
        hits = len({k[:-1] if len(k) > 3 and k.endswith("s") else k for k in spec.keywords} & q)
        if hits > 0:
            scored.append((hits, sid))
    scored.sort(key=lambda x: (-x[0], x[1]))
    chosen = [sid for _, sid in scored[:max(1, max_participants)]]

    if not chosen:
        return [COORDINATOR_ID]
    if len(chosen) >= 2 and COORDINATOR_ID not in chosen:
        chosen.append(COORDINATOR_ID)  # coordinator synthesizes a multi-specialist council
    return chosen


async def select_specialists_smart(
    query: str, *, understanding=None, max_participants: int = 3
) -> list[str]:
    """LLM-routed specialist selection (U3), falling back to the deterministic
    keyword scoring. Keyword overlap misses cross-domain requests — "I keep
    putting off my training" scores Fitness Coach on 'training' and never sees
    the procrastination angle the Psychologist owns. The model reads intent;
    the keyword path remains the safety net."""
    keyword_pick = select_specialists(query, max_participants=max_participants)
    if understanding is None or not getattr(understanding, "available", False):
        return keyword_pick

    options = {
        s.id: f"{s.name} — {s.specialization}"
        for s in SPECIALISTS.values() if not s.coordinator
    }
    picked = await understanding.rank(
        query, options=options, fallback=[], limit=max_participants,
        instruction="You are routing a request to the right specialists on an assistant's team.",
    )
    if not picked:
        return keyword_pick
    if len(picked) >= 2 and COORDINATOR_ID not in picked:
        picked.append(COORDINATOR_ID)  # someone must synthesize a multi-voice council
    return picked


def roster() -> list[dict]:
    return [
        {"id": s.id, "name": s.name, "specialization": s.specialization,
         "reasoning_style": s.reasoning_style, "coordinator": s.coordinator}
        for s in SPECIALISTS.values()
    ]


class AgentSociety:
    """Turn-based council. Selection + state are deterministic (tested); the LLM
    contributions are the thin orchestration. GPU-serialized via the semaphore —
    contributions happen one at a time, honestly reflecting the single model."""

    def __init__(self, *, memory, llm, llm_semaphore, understanding=None) -> None:
        self._memory = memory
        self._llm = llm
        self._sem = llm_semaphore
        self._understanding = understanding

    async def _one_call(self, prompt: str, *, max_tokens: int) -> str:
        try:
            async with self._sem:
                out = await self._llm.chat(
                    [{"role": "user", "content": prompt}], max_tokens=max_tokens, temperature=0.3, thinking=False
                )
            return (out or "").strip()
        except Exception as e:  # noqa: BLE001
            logger.debug("society_call_failed", error=str(e)[:160])
            return ""

    async def deliberate(self, query: str, *, context: str = "", max_participants: int = 3) -> dict:
        """Executive selects participants → each contributes in turn → the
        coordinator synthesizes (when 2+ engaged) → experience is recorded."""
        ids = await select_specialists_smart(
            query, understanding=self._understanding, max_participants=max_participants,
        )
        specs = [SPECIALISTS[i] for i in ids]
        contributors = [s for s in specs if not s.coordinator]
        coordinator = next((s for s in specs if s.coordinator), None)
        # When nothing matched, the coordinator answers directly as the sole voice.
        if not contributors and coordinator:
            contributors, coordinator = [coordinator], None

        # A specialist's prior notes are accumulated observations about working
        # with Marcus — his preferences, his context. Injecting them into the
        # prompt would put that content into an answer given to whoever is in
        # the room, which is how `agent.recall` being owner-only gets undone one
        # layer down (V3 P5.1d.3). The council still deliberates for a guest; it
        # just does so without his private context.
        try:
            from core.turn_identity import current_identity
            _notes_ok = current_identity().is_owner
        except Exception:  # noqa: BLE001
            _notes_ok = True

        contributions: list[dict] = []
        for spec in contributors:
            notes = await self._memory.agent_recall(spec.id, limit=3) if _notes_ok else []
            note_line = ("\nYour prior notes on similar topics: " + " | ".join(notes)) if notes else ""
            prompt = (
                f"You are Nova's {spec.name} ({spec.specialization}). Reasoning style: {spec.reasoning_style}. "
                f"{spec.persona}{note_line}\n\nQuestion: {query}\n"
                + (f"Context: {context}\n" if context else "")
                + "Give your specialist perspective in 2-4 sentences. Be direct; contribute only your angle."
            )
            text = await self._one_call(prompt, max_tokens=220)
            if text:
                contributions.append({"agent": spec.name, "id": spec.id, "text": text})
            await self._memory.record_consultation(spec.id)

        synthesis = ""
        used_coordinator = False
        if coordinator and len(contributions) >= 2:
            joined = "\n".join(f"- {c['agent']}: {c['text']}" for c in contributions)
            # Who the answer is FOR. Hardcoding "Marcus" told the coordinator a
            # guest's question was his — measured on `71fc0eb`, every speaker's
            # synthesis prompt said "answer for Marcus". Suppressing the private
            # notes (above) was not enough on its own: the addressee is a second,
            # independent way the same prompt asserts the wrong person.
            #
            # Server-side from TurnIdentity, never a client-supplied name.
            prompt = (
                f"You are Nova's {coordinator.name}. {coordinator.persona}\n\n"
                f"Question: {query}\nSpecialist input:\n{joined}\n\n"
                f"Synthesize this into one clear, cohesive answer for {_addressee()} — "
                "not a list of who said what. "
                "Resolve any disagreement and recommend a direction."
            )
            synthesis = await self._one_call(prompt, max_tokens=400)
            await self._memory.record_consultation(coordinator.id)
            used_coordinator = bool(synthesis)
        elif contributions:
            synthesis = contributions[0]["text"]

        participants = [c["agent"] for c in contributions]
        if used_coordinator and coordinator:
            participants.append(coordinator.name)
        return {"participants": participants, "contributions": contributions, "synthesis": synthesis}
