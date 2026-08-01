from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from core.agent_supervisor import AgentSupervisor, SupervisorConfig
from core.conversation_state import ConversationStateStore
from core.event_bus import BUS
from core.events import MemoryIngestEvent, SummarizeHintEvent
from core.logging_setup import get_logger
from core.planner import Planner
from core.policy.autonomy_planner import AutonomyPlannerLLM
from core.policy.chat_decider import ChatDecider
from core.policy.memory_extractor import MemoryExtractorLLM
from core.policy.summarizer import SummarizerLLM
from core.policy.storyteller import StorytellerLLM, is_story_request, story_system_prompt
from core.mood import detect_mood_signal
from core.policy._json_extract import extract_first_json_object
from core.intent import is_question
from core.project_builder import (
    ProjectBuilder,
    BUILD_ACTION_RE,
    CONTINUATION_COMPLAINT_RE,
    IMPLEMENT_SUGG_RE,
    IMPROVE_WORDS_RE,
    NAME_RE,
    NEEDS_NAME,
    RESUME_WORDS_RE,
    STATUS_WORDS_RE,
)
from core.orchestrator.agent import Agent, ToolLoopExecutor
from core.orchestrator.deep_mode import DeepPipeline, is_deep_request
from core.orchestrator.model_router import ModelHandle, ModelRouter, parse_role_map
from core.cloud_runtime import CloudRuntime, cloud_enabled
from core.response_composer import ResponseComposer
from core.screen_broker import ScreenCaptureBroker
from core.tool_router import ToolCall, ToolRouter
from core.llm_runtime import LLMRuntime
from core.workers.autonomy_supervisor import AutonomySupervisorWorker
from core.workers.memory_ingest import MemoryIngestWorker
from core.workers.self_improve import SelfImproveWorker
from core.workers.reminder_worker import ReminderWorker
from core.workers.research_worker import ResearchWorker
from core.orchestrator.society import AgentSociety
from core import code_intel
from core.permissions import PermissionBroker
from core.computer_control import ComputerControl
from core.error_log import ErrorLog
from memory.backends.diskcache_backend import DiskCacheBackend
from memory.unifier import MemoryUnifier


logger = get_logger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Tool-assist helpers ──────────────────────────────────────────────────────

_TIME_QUERY_RE = re.compile(
    r"\b(?:what\s+time\s+is\s+it|what(?:'s|\s+is)\s+the\s+time|current\s+time|tell\s+me\s+the\s+time)\b",
    re.IGNORECASE,
)
_DATE_QUERY_RE = re.compile(
    r"\b(?:what\s+day\s+is\s+it|what\s+date\s+is\s+it|what(?:'s|\s+is)\s+the\s+date|today'?s\s+date)\b",
    re.IGNORECASE,
)

_WEATHER_CITY_RE = re.compile(
    r'\bweather\b[^.!?]{0,60}?\b(?:in|for|at)\b\s*([A-Za-z][A-Za-z\s]{1,28}?)(?:\?|$|\s+(?:today|tonight|tomorrow|this\b))',
    re.IGNORECASE,
)
_WEATHER_CITY_LEAD_RE = re.compile(
    r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s+weather\b',
)
_DIRS_KEYWORD_RE = re.compile(
    r'\b(?:directions?|route|navigate|how\s+(?:do\s+i\s+)?(?:get|go)|how\s+far)\b',
    re.IGNORECASE,
)
_DIRS_FROM_TO_RE = re.compile(
    r'\bfrom\b\s+(.+?)\s+\bto\b\s+(.+?)(?:\s+\bby\b\s+(\w+))?(?:\?|$)',
    re.IGNORECASE,
)
_FROM_HERE_RE = re.compile(r"\b(?:from\s+here|from\s+my\s+location|near\s+me|around\s+me)\b", re.IGNORECASE)
_NEAREST_QUERY_RE = re.compile(r"\b(?:nearest|closest)\s+(.+?)(?:\?|$)", re.IGNORECASE)
_PLACE_LOOKUP_PATTERNS = [
    re.compile(r"\bwhere\s+is\b\s+(.+?)(?:\?|$)", re.IGNORECASE),
    re.compile(r"\bwhere's\b\s+(.+?)(?:\?|$)", re.IGNORECASE),
    re.compile(r"\bfind\b\s+(.+?)(?:\?|$)", re.IGNORECASE),
    re.compile(r"\blocate\b\s+(.+?)(?:\?|$)", re.IGNORECASE),
    re.compile(r"\bshow\s+me\b\s+(.+?)(?:\?|$)", re.IGNORECASE),
]
_TO_DESTINATION_PATTERNS = [
    re.compile(r"\bhow\s+do\s+i\s+get\s+to\b\s+(.+?)(?:\s+\bfrom\s+here\b|\?|$)", re.IGNORECASE),
    re.compile(r"\bdirections?\s+to\b\s+(.+?)(?:\s+\bfrom\s+here\b|\?|$)", re.IGNORECASE),
    re.compile(r"\bhow\s+long\s+will\s+it\s+take(?:\s+to\s+get)?\s+to\b\s+(.+?)(?:\s+\bfrom\s+here\b|\?|$)", re.IGNORECASE),
    re.compile(r"\b(?:get|go|drive|walk|navigate|head)\s+to\b\s+(.+?)(?:\s+\bfrom\s+here\b|\?|$)", re.IGNORECASE),
]
_NAME_QUERY_RE = re.compile(
    r"\b(?:do\s+you\s+know\s+my\s+name|what\s+is\s+my\s+name|who\s+am\s+i)\b",
    re.IGNORECASE,
)
_NAME_STATEMENT_PATTERNS = [
    re.compile(r"\bmy\s+name\s+is\s+([A-Z][A-Za-z'\-]+(?:\s+[A-Z][A-Za-z'\-]+){0,3})\b"),
    re.compile(r"\bcall\s+me\s+([A-Z][A-Za-z'\-]+(?:\s+[A-Z][A-Za-z'\-]+){0,3})\b"),
]


def _extract_weather_city(text: str) -> str | None:
    m = _WEATHER_CITY_RE.search(text)
    if m:
        return m.group(1).strip().rstrip(" ,.")
    m = _WEATHER_CITY_LEAD_RE.search(text)
    if m:
        return m.group(1).strip()
    return None


def _extract_directions(text: str) -> tuple[str, str, str] | None:
    if not _DIRS_KEYWORD_RE.search(text):
        return None
    m = _DIRS_FROM_TO_RE.search(text)
    if not m:
        return None
    origin = m.group(1).strip().rstrip(" ,.")
    dest = m.group(2).strip().rstrip(" ,.")
    mode_raw = (m.group(3) or "driving").strip().lower()
    valid = {"driving", "walking", "bicycling", "transit"}
    mode = mode_raw if mode_raw in valid else "driving"
    # Guard against overly long/garbage extractions
    if len(origin.split()) > 6 or len(dest.split()) > 6:
        return None
    return origin, dest, mode


def _extract_nearest_query(text: str) -> str | None:
    match = _NEAREST_QUERY_RE.search(text)
    if not match:
        return None
    query = match.group(1).strip(" .,!?")
    query = re.sub(r"^(?:the|a|an)\s+", "", query, flags=re.IGNORECASE)
    query = re.sub(r"\b(?:to\s+me|near\s+me|around\s+me|from\s+here)\b.*$", "", query, flags=re.IGNORECASE).strip(" .,!?")
    return query or None


def _extract_place_lookup_query(text: str) -> str | None:
    for pattern in _PLACE_LOOKUP_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        query = match.group(1).strip(" .,!?")
        query = re.sub(r"\b(?:nearby|near\s+me|around\s+me|from\s+here)\b.*$", "", query, flags=re.IGNORECASE).strip(" .,!?")
        if not query:
            return None
        lowered = query.lower()
        if lowered.startswith(("the nearest ", "nearest ", "the closest ", "closest ")):
            return None
        if lowered in {"it", "this", "that", "there"}:
            return None
        return query
    return None


def _extract_destination_from_here(text: str) -> str | None:
    for pattern in _TO_DESTINATION_PATTERNS:
        match = pattern.search(text)
        if match:
            destination = match.group(1).strip(" .,!?")
            return destination or None
    return None


def _current_coords_text(current_location: dict[str, Any] | None) -> str | None:
    if not current_location:
        return None
    try:
        lat = float(current_location.get("lat"))
        lng = float(current_location.get("lng"))
    except Exception:
        return None
    return f"{lat},{lng}"


_USE_DEVICE_LOCATION_RE = re.compile(
    r"\b(?:use\s+my\s+(?:current\s+)?location|my\s+(?:current\s+)?location|from\s+here|near\s+me|around\s+me|current\s+location|where\s+i\s+am)\b",
    re.IGNORECASE,
)
_LOCATION_ANSWER_ABORT = {"no", "nope", "nevermind", "never mind", "cancel", "forget it", "stop", "no thanks"}


def _wants_device_location(text: str) -> bool:
    return bool(_USE_DEVICE_LOCATION_RE.search(text or ""))


_READ_STEPS_RE = re.compile(
    r"\b(?:read|say|tell\s+me|give\s+me|what\s+are)\b.{0,30}\b(?:steps|directions|turn[\s-]?by[\s-]?turn|route|them)\b",
    re.IGNORECASE,
)


def _looks_like_location_answer(text: str) -> bool:
    """Heuristic: is this short message plausibly the user answering 'where are
    you?' with a place/address, rather than a new question or command? Lenient
    — geocoding rejects genuine garbage, so we only bail on clear non-answers."""
    t = (text or "").strip().lower()
    if not t or t in _LOCATION_ANSWER_ABORT:
        return False
    if re.match(r"^(what|who|when|why|how|can you|could you|please|make|build|create|remind|set|play|open the|show me the)\b", t):
        return False
    return len(t.split()) <= 12


def _looks_like_place_search_term(text: str) -> bool:
    candidate = (text or "").strip()
    if not candidate or len(candidate.split()) > 6:
        return False
    if re.search(r"\d", candidate):
        return False
    return True


def _looks_like_time_query(text: str) -> bool:
    return bool(_TIME_QUERY_RE.search(text) or _DATE_QUERY_RE.search(text))


def _looks_like_name_query(text: str) -> bool:
    return bool(_NAME_QUERY_RE.search(text or ""))


def _extract_user_name(text: str) -> str | None:
    raw = (text or "").strip()
    if not raw:
        return None
    for pattern in _NAME_STATEMENT_PATTERNS:
        match = pattern.search(raw)
        if not match:
            continue
        candidate = re.sub(r"\s+", " ", match.group(1).strip(" .,!?:;\t\n"))
        if not candidate:
            continue
        words = [word for word in candidate.split(" ") if word]
        if not words or len(words) > 4:
            continue
        if any(word.lower() in {"nova", "sorry", "actually", "no"} for word in words):
            continue
        return " ".join(word[:1].upper() + word[1:] for word in words)
    return None


def _format_clock_reply(text: str) -> str:
    now_local = datetime.now().astimezone()
    hour = now_local.strftime("%I").lstrip("0") or "0"
    minute_ampm = now_local.strftime("%M %p")
    time_text = f"{hour}:{minute_ampm}"
    tz_text = (now_local.tzname() or "").strip()
    date_text = now_local.strftime("%A, %B %d, %Y")

    wants_time = bool(_TIME_QUERY_RE.search(text))
    wants_date = bool(_DATE_QUERY_RE.search(text))

    if wants_time and wants_date:
        return f"It is {time_text}{(' ' + tz_text) if tz_text else ''} on {date_text}."
    if wants_date:
        return f"Today is {date_text}."
    return f"It is {time_text}{(' ' + tz_text) if tz_text else ''}."


def _format_number(value: Any) -> str:
    try:
        num = float(value)
    except Exception:
        return str(value)
    if num.is_integer():
        return str(int(num))
    return f"{num:.1f}"


def _format_weather_reply(city: str, payload: dict[str, Any]) -> str:
    try:
        temp = str(int(round(float(payload.get("temp")))))
    except Exception:
        temp = _format_number(payload.get("temp"))
    humidity = payload.get("humidity")
    description = str(payload.get("description") or "current conditions unavailable").strip()
    if humidity in (None, ""):
        return f"Right now in {city}, it is {temp} degrees Fahrenheit with {description}."
    return f"Right now in {city}, it is {temp} degrees Fahrenheit with {description}. Humidity is {humidity}%."


def _format_directions_reply(payload: dict[str, Any]) -> str:
    origin = str(payload.get("origin") or "the starting point").strip()
    destination = str(payload.get("destination") or "the destination").strip()
    distance = str(payload.get("distance") or "unknown distance").strip()
    duration = str(payload.get("duration") or "unknown travel time").strip()
    mode = str(payload.get("mode") or "driving").strip().lower()
    base = f"From {origin} to {destination}, it is about {distance} and takes around {duration} by {mode}."
    if payload.get("steps"):
        base += " I've got the full route on the map — want me to read the turn-by-turn, or scan the QR to send it to your phone?"
    return base


@dataclass
class ChatTurnResult:
    conversation_id: UUID
    assistant_text: str
    tool_calls: list[dict[str, Any]]


class RuntimeManager:
    """Owns queues, workers, and the shared LLM semaphore."""

    def __init__(
        self,
        *,
        repo_root: Path,
        projects_dir: Path,
        memory: MemoryUnifier,
        llm: LLMRuntime,
        router: ToolRouter,
        memory_dir: Path,
    ) -> None:
        self._repo_root = repo_root
        self._projects_dir = projects_dir
        self._memory = memory
        self._llm = llm
        self._router = router

        self._llm_sem = asyncio.Semaphore(1)

        # ModelRouter (Phase 2.4 + U2). The local model on its single GPU
        # semaphore is the DEFAULT for every role — chat, memory and decisions
        # stay local, always. When cloud is configured, a SECOND handle is
        # registered with its OWN semaphore (remote calls don't contend for the
        # GPU), and `coder`/`planner` default to it. An explicit NOVA_MODEL_ROLES
        # entry always wins, so routing stays fully user-controlled.
        self._identity_cache: tuple[str | None, list[str]] = (None, [])
        self._cloud = CloudRuntime(fallback=self._llm, identities=lambda: self._identity_cache)

        handles = {"primary": ModelHandle(name="primary", runtime=self._llm, semaphore=self._llm_sem)}
        role_map = parse_role_map(os.getenv("NOVA_MODEL_ROLES", ""))
        if cloud_enabled():
            try:
                concurrency = max(1, int(os.getenv("NOVA_CLOUD_CONCURRENCY", "4") or 4))
            except ValueError:
                concurrency = 4
            handles["cloud"] = ModelHandle(
                name="cloud", runtime=self._cloud, semaphore=asyncio.Semaphore(concurrency),
            )
            for _role in ("coder", "planner"):
                role_map.setdefault(_role, "cloud")
            logger.info("cloud_roles_enabled", provider=self._cloud.provider,
                        model=self._cloud.model or "(unset)", concurrency=concurrency)

        self._models = ModelRouter(handles, default="primary", role_map=role_map)
        # Orchestrator (Phase 2.1): the reason→act→observe loop runs here. The
        # default chat agent reproduces the previous inline loop exactly.
        self._tool_loop = ToolLoopExecutor(models=self._models, tool_router=router)
        self._chat_agent = Agent(name="chat", step_budget=self._TOOL_LOOP_MAX)
        # Deep mode (Phase 2.3): Planner + Critic bookends around the loop,
        # opt-in only (is_deep_request), so normal chat stays one-pass fast.
        self._deep = DeepPipeline(self._models)

        recent_turns = int(os.getenv("NOVA_RECENT_CHAT_TURNS", "20").strip() or "20")
        followup_window = int(os.getenv("NOVA_FOLLOWUP_WINDOW", "10").strip() or "10")

        # State store persisted via diskcache
        self._state_store = ConversationStateStore(
            DiskCacheBackend(memory_dir / "diskcache"),
            max_turns=recent_turns,
            followup_window=followup_window,
        )

        # Policies
        self._decider = ChatDecider(llm, llm_semaphore=self._llm_sem)
        self._extractor = MemoryExtractorLLM(llm, llm_semaphore=self._llm_sem)
        self._summarizer = SummarizerLLM(llm, llm_semaphore=self._llm_sem)
        self._storyteller = StorytellerLLM(llm, llm_semaphore=self._llm_sem)
        self._autonomy_planner = AutonomyPlannerLLM(llm, llm_semaphore=self._llm_sem)
        self._planner = Planner()

        # Composer
        self._composer = ResponseComposer(state_store=self._state_store, llm=llm, llm_semaphore=self._llm_sem)

        # Queues
        self._memory_ingest_q: asyncio.Queue[MemoryIngestEvent] = asyncio.Queue(maxsize=200)
        self._summarize_q: asyncio.Queue[SummarizeHintEvent] = asyncio.Queue(maxsize=50)

        # Workers
        summary_every_n = int(os.getenv("NOVA_SUMMARY_EVERY_N", "8").strip() or "8")
        self._memory_worker = MemoryIngestWorker(
            memory=self._memory,
            extractor=self._extractor,
            summarizer=self._summarizer,
            state=self._state_store,
            queue=self._memory_ingest_q,
            summarize_queue=self._summarize_q,
            summary_every_n=summary_every_n,
        )

        tick = float(os.getenv("NOVA_AUTONOMY_TICK_S", "5").strip() or "5")
        self._autonomy_worker = AutonomySupervisorWorker(
            memory=self._memory,
            planner=self._autonomy_planner,
            router=self._router,
            tick_seconds=tick,
        )

        # Autonomous project builder (builds real projects in projects_dir).
        self._project_builder = ProjectBuilder(
            projects_dir=projects_dir,
            llm=llm,
            llm_semaphore=self._llm_sem,
            memory=memory,
        )

        async def _tool_project_start(args: dict[str, Any]) -> dict[str, Any]:
            name = str(args.get("name") or "").strip()
            brief = str(args.get("brief") or args.get("description") or name).strip()
            if not name:
                return {"started": False, "error": "missing name"}
            # The LLM tool-calling loop picks names independently of the regex
            # pre-pass and has no memory of existing projects — if the name it
            # chose textually overlaps one we already have (e.g. "gravity
            # runner" vs. existing "gravity-run"), treat this as an improve
            # request instead of silently creating a near-duplicate project.
            # Deliberately NOT falling back to last_active() here: unlike the
            # regex pre-pass, this tool is only invoked when the LLM already
            # decided the user wants a genuinely new project, so guessing
            # "they meant the old one" would misfire on real new requests.
            existing = self._project_builder.known_slug_in_text(name)
            if existing:
                return await self._project_builder.improve(slug=existing, instructions=brief)
            return await self._project_builder.start(name=name, brief=brief)

        async def _tool_project_status(args: dict[str, Any]) -> dict[str, Any]:
            name = str(args.get("name") or "").strip()
            if not name:
                return {"projects": self._project_builder.list_projects()}
            return {"project": name, "status": self._project_builder.status_text(name)}

        async def _tool_project_improve(args: dict[str, Any]) -> dict[str, Any]:
            name = str(args.get("name") or "").strip()
            instructions = str(args.get("instructions") or "").strip()
            if not name or not instructions:
                return {"started": False, "error": "missing name or instructions"}
            return await self._project_builder.improve(slug=name, instructions=instructions)

        self._router.register("project.start_build", _tool_project_start, "Start building a brand-new software project. Only call this when the user explicitly asks to start/create/build a NEW project — never for questions, discussion, or feedback about an existing one (use project.improve or plain chat for those). args: {name, brief}")
        self._router.register("project.status", _tool_project_status, "Get build status / where we left off on a project. args: {name?}")
        self._router.register("project.improve", _tool_project_improve, "Improve an existing project with instructions. args: {name, instructions}")

        async def _tool_memory_synthesize(args: dict[str, Any]) -> dict[str, Any]:
            """DS1: cross-document synthesis, distinct from memory.recall (which
            stays a fast single-fact lookup). Pulls multiple relevant chunks —
            possibly from several files — and asks the LLM to synthesize an
            answer citing which file(s) each point comes from."""
            topic = str(args.get("topic") or args.get("query") or "").strip()
            if not topic:
                return {"ok": False, "error": "missing_topic"}
            chunks = await self._memory.search_document_chunks_broad(topic, limit=18)
            if not chunks:
                return {
                    "ok": False, "error": "no_indexed_documents_match",
                    "note": "No indexed files matched this topic. Index a folder first with memory.index_folder, "
                            "or try different wording.",
                }
            by_file: dict[str, list[str]] = {}
            for c in chunks:
                name = Path(str(c["path"])).name
                by_file.setdefault(name, []).append(str(c["text"]))
            context_blob = "\n\n".join(
                f"--- {name} ---\n" + "\n...\n".join(texts[:4]) for name, texts in by_file.items()
            )
            prompt = (
                f"Synthesize what these indexed documents say about: {topic}\n\n"
                f"{context_blob[:8000]}\n\n"
                "Write a focused answer citing which file(s) each point comes from. If the documents don't "
                "actually address the topic, say so honestly rather than padding with unrelated content."
            )
            async with self._llm_sem:
                answer = await self._llm.chat(
                    [{"role": "user", "content": prompt}], max_tokens=700, temperature=0.3, thinking=True
                )
            return {"ok": True, "answer": (answer or "").strip(), "files": list(by_file.keys())}

        self._router.register(
            "memory.synthesize", _tool_memory_synthesize,
            "Synthesize an answer about a topic by pulling and combining relevant passages across ALL indexed "
            "files (not just one) — use this for 'summarize everything about X' style questions, instead of "
            "memory.recall which only returns a fast single-fact match. args: {topic}",
        )

        # Goal-based supervisor: handles structured goals with proposals and tool chains.
        agent_tick = float(os.getenv("NOVA_AGENT_TICK_S", "1").strip() or "1")
        self._agent_supervisor = AgentSupervisor(
            memory=self._memory,
            llm=self._llm,
            router=self._router,
            tool_descriptions={name: "" for name in self._router.list_tools()},
            cfg=SupervisorConfig(tick_seconds=agent_tick),
        )

        # Self-improvement: captures her own errors, and on a slow killable timer
        # either files a fix PROPOSAL for a recurring error or reflects to learn
        # a lesson. Shares the router's guarded DevMode so proposals stay in one
        # store; only ever proposes changes to her own code.
        self._error_log = ErrorLog(memory_dir / "errors.json")
        dev_mode = getattr(router, "dev_mode", None)
        if dev_mode is None:
            from core.dev_mode import DevMode
            dev_mode = DevMode(repo_root=repo_root, projects_dir=projects_dir)
        self._dev_mode = dev_mode
        self._self_improve = SelfImproveWorker(
            memory=self._memory,
            llm=self._llm,
            llm_semaphore=self._llm_sem,
            dev_mode=dev_mode,
            error_log=self._error_log,
            state_store=self._state_store,
        )

        # Real user-facing scheduling ("remind me at 5pm") + a spoken morning
        # briefing — distinct from internal worker pacing.
        self._reminder_worker = ReminderWorker(memory=self._memory, router=self._router)

        # Autonomous research (Phase 5 / #9) — OFF by default (NOVA_RESEARCH),
        # self-gates in start(); keeps tracked topics fresh into the world model.
        self._research_worker = ResearchWorker(
            memory=self._memory, llm=self._llm, llm_semaphore=self._llm_sem, router=self._router,
        )

        # PI1: lets the agent ask to look at the screen mid-conversation —
        # still requires the user's confirm click frontend-side (see
        # vision.look_at_screen below), never a silent capture.
        self._screen_broker = ScreenCaptureBroker()

        async def _tool_vision_look_at_screen(args: dict[str, Any]) -> dict[str, Any]:
            question = str(args.get("question") or "What's on the screen right now?").strip()
            request_id, fut = self._screen_broker.new_request()
            BUS.publish("screen.capture_requested", {"request_id": request_id, "question": question})
            try:
                result = await asyncio.wait_for(fut, timeout=30.0)
            except (TimeoutError, asyncio.TimeoutError):
                self._screen_broker.cancel(request_id)
                return {
                    "ok": False, "error": "no_response",
                    "note": "Marcus didn't respond to the screen-look request in time — it needs his confirm "
                            "click in the app. Don't assume what's on screen; ask him to describe it instead.",
                }
            if not result.get("approved"):
                return {"ok": False, "error": "declined", "note": "Marcus declined to let you look at the screen this time."}
            text = str(result.get("text") or "").strip()
            if not text:
                return {"ok": False, "error": str(result.get("error") or "capture_failed")}
            return {"ok": True, "text": text}

        self._router.register(
            "vision.look_at_screen", _tool_vision_look_at_screen,
            "Ask to look at Marcus's screen right now (e.g. to help debug something visible on it). Requires his "
            "explicit confirm click in the app first — never silent. Only call this when it's clearly relevant to "
            "what he's asking. args: {question?}",
        )

        # Persistent agent society (Phase 6 / #5): a council of durable specialists
        # the Executive routes. Registered here where the LLM + semaphore live.
        self._society = AgentSociety(memory=self._memory, llm=self._llm, llm_semaphore=self._llm_sem)

        async def _tool_society_consult(args: dict[str, Any]) -> dict[str, Any]:
            if (os.getenv("NOVA_AGENT_SOCIETY", "1").strip() or "1").lower() in {"0", "false", "no", "off"}:
                return {"ok": False, "error": "agent_society_disabled"}
            question = str(args.get("question") or args.get("query") or "").strip()
            if not question:
                return {"ok": False, "error": "missing_question"}
            context = str(args.get("context") or "").strip()
            result = await self._society.deliberate(question, context=context)
            if not result.get("synthesis"):
                return {"ok": False, "error": "no_synthesis", "note": "The council couldn't produce an answer this time."}
            return {
                "ok": True,
                "participants": result["participants"],
                "answer": result["synthesis"],
                "perspectives": [{"specialist": c["agent"], "view": c["text"]} for c in result["contributions"]],
            }

        self._router.register(
            "society.consult", _tool_society_consult,
            "Convene Nova's council of specialists (Chief Engineer, Research Scientist, Psychologist, Fitness/"
            "Snowboard Coach, Media Curator, Financial Planner, Security Specialist, ...) for a question that "
            "genuinely benefits from multiple expert angles or a cross-domain decision. The Executive picks who "
            "weighs in and synthesizes their views. Slower than a normal reply (several model calls) — use only "
            "when the depth is worth it. args: {question, context?}",
        )

        # Continuous codebase understanding (Phase 7 / #10 + #18). Registered here
        # where repo_root / projects_dir / dev_mode resolve a project to a path.
        self._code_index_cache: dict[str, tuple[float, dict[str, Any]]] = {}

        def _resolve_project_root(name: str) -> Path | None:
            name = (name or "").strip().lower()
            if name in ("", "self", "nova", "repo", "."):
                return self._repo_root
            try:
                for entry in self._dev_mode.list_external_roots():
                    if entry["project"].lower() == name:
                        return Path(entry["path"])
            except Exception:
                pass
            cand = self._projects_dir / name
            return cand if cand.is_dir() else None

        def _get_index(root: Path) -> dict[str, Any]:
            import time as _t
            key = str(root)
            cached = self._code_index_cache.get(key)
            if cached and (_t.monotonic() - cached[0]) < 120:
                return cached[1]
            idx = code_intel.index_project(root)
            self._code_index_cache[key] = (_t.monotonic(), idx)
            return idx

        async def _tool_code_index(args: dict[str, Any]) -> dict[str, Any]:
            root = _resolve_project_root(str(args.get("project") or ""))
            if root is None:
                return {"ok": False, "error": "unknown_project", "note": "Register it first with self.register_project, or use 'self' for Nova's own code."}
            idx = _get_index(root)
            if not idx.get("exists"):
                return {"ok": False, "error": "not_a_directory", "path": str(root)}
            return {"ok": True, "project": str(root), "architecture": code_intel.architecture_summary(idx),
                    "health": code_intel.health_score(idx)}

        async def _tool_code_symbols(args: dict[str, Any]) -> dict[str, Any]:
            root = _resolve_project_root(str(args.get("project") or ""))
            if root is None:
                return {"ok": False, "error": "unknown_project"}
            q = str(args.get("query") or args.get("name") or "").strip().lower()
            idx = _get_index(root)
            hits = [{"symbol": s, "files": locs} for s, locs in idx.get("symbols", {}).items()
                    if not q or q in s.lower()]
            hits.sort(key=lambda h: h["symbol"].lower())
            return {"ok": True, "count": len(hits), "symbols": hits[:60]}

        async def _tool_code_impact(args: dict[str, Any]) -> dict[str, Any]:
            root = _resolve_project_root(str(args.get("project") or ""))
            if root is None:
                return {"ok": False, "error": "unknown_project"}
            symbol = str(args.get("symbol") or args.get("name") or "").strip()
            if not symbol:
                return {"ok": False, "error": "missing_symbol"}
            return {"ok": True, **code_intel.impact_of(_get_index(root), symbol)}

        async def _tool_code_health(args: dict[str, Any]) -> dict[str, Any]:
            root = _resolve_project_root(str(args.get("project") or ""))
            if root is None:
                return {"ok": False, "error": "unknown_project"}
            idx = _get_index(root)
            debt = code_intel.tech_debt(idx)
            return {"ok": True, "health": code_intel.health_score(idx),
                    "debt_summary": debt["by_severity"], "top_debt": debt["items"][:12]}

        async def _tool_code_security(args: dict[str, Any]) -> dict[str, Any]:
            root = _resolve_project_root(str(args.get("project") or ""))
            if root is None:
                return {"ok": False, "error": "unknown_project"}
            scan = code_intel.security_scan(root)
            return {"ok": True, "by_severity": scan["by_severity"], "findings": scan["findings"][:25],
                    "disclaimer": scan["disclaimer"]}

        # ── Computer control (Phase 8) — permission-gated, propose-only ──────
        # The broker is the single gate; no platform adapter ships, so actions
        # are dry-run/unavailable by default (see core/computer_control.py).
        self._permission_broker = PermissionBroker(
            mode=os.getenv("NOVA_PERMISSION_MODE", "guarded"),
            audit_path=(memory_dir / "permission_audit.jsonl"),
        )
        self._computer = ComputerControl(self._permission_broker, adapter=None)

        async def _tool_computer_observe(args: dict[str, Any]) -> dict[str, Any]:
            return await self._computer.observe(str(args.get("what") or "windows"))

        async def _tool_computer_act(args: dict[str, Any]) -> dict[str, Any]:
            kind = str(args.get("kind") or args.get("action") or "").strip()
            if not kind:
                return {"ok": False, "error": "missing_kind"}
            details = args.get("details") if isinstance(args.get("details"), dict) else {}
            # Non-blocking: return the permission decision (needs_confirmation with a
            # request_id) rather than hanging the turn waiting for approval.
            return await self._computer.act(kind, target=str(args.get("target") or ""),
                                            details=details, wait_for_confirm=False)

        async def _tool_skill_run(args: dict[str, Any]) -> dict[str, Any]:
            skill_id = str(args.get("skill_id") or args.get("id") or "").strip()
            skill = await self._memory.get_skill(skill_id) if skill_id else None
            if not skill:
                return {"ok": False, "error": "unknown_skill"}
            from core.skills import render_steps
            params = args.get("params") if isinstance(args.get("params"), dict) else {}
            steps = render_steps(skill["steps"], params)
            # Every step is re-checked through the permission gate — a "learned"
            # skill is never a bypass. With no adapter, each step is a dry run.
            results = []
            for step in steps:
                results.append({"step": step, **await self._computer.act("type", target=step,
                                                                          details={"step": step}, wait_for_confirm=False)})
            return {"ok": True, "skill": skill["name"], "steps_evaluated": results,
                    "note": "Each step is permission-checked individually; nothing executed without an enabled adapter."}

        self._router.register("computer.observe", _tool_computer_observe,
            "Observe the computer (list windows/apps, read state) — read-only. Honestly reports when no observation "
            "adapter is installed. args: {what?}")
        self._router.register("computer.act", _tool_computer_act,
            "Propose a computer action (click/type/scroll/launch_app/...). It is permission-gated: standard/admin "
            "actions need Marcus's explicit approval, and NOTHING executes unless computer control is enabled with a "
            "platform adapter (off by default — otherwise a dry run). args: {kind, target?, details?}")
        self._router.register("skill.run", _tool_skill_run,
            "Run a learned workflow skill by id, substituting {params}. Every step is re-checked through the "
            "permission gate; a learned skill is never a bypass. args: {skill_id, params?}")

        for _name, _fn, _desc in [
            ("code.index", _tool_code_index,
             "Get a structural understanding of a project: architecture outline (languages, top dirs, largest files, "
             "dependencies, entry points) + a health score. Use 'self' for Nova's own code. args: {project?}"),
            ("code.symbols", _tool_code_symbols,
             "Search a project's classes/functions by name and see which files define them. args: {project?, query}"),
            ("code.impact", _tool_code_impact,
             "Impact analysis BEFORE editing: what references a symbol (who uses it / imports its module) and how "
             "wide the blast radius is. args: {project?, symbol}"),
            ("code.health", _tool_code_health,
             "Project health score + ranked technical-debt report (long files, undocumented API, TODOs, missing "
             "tests, syntax errors). args: {project?}"),
            ("code.security", _tool_code_security,
             "Defensive security scan of a registered project's own code: flags risky patterns (eval/exec, shell=True, "
             "hardcoded secrets, disabled TLS, weak hashes) for human review. args: {project?}"),
        ]:
            self._router.register(_name, _fn, _desc)

    @property
    def router(self) -> ToolRouter:
        return self._router

    @property
    def models(self) -> ModelRouter:
        return self._models

    @property
    def error_log(self) -> ErrorLog:
        return self._error_log

    @property
    def self_improve(self) -> SelfImproveWorker:
        return self._self_improve

    @property
    def dev_mode(self):
        return self._dev_mode

    @property
    def reminder_worker(self) -> ReminderWorker:
        return self._reminder_worker

    @property
    def state_store(self) -> ConversationStateStore:
        return self._state_store

    @property
    def screen_broker(self) -> ScreenCaptureBroker:
        return self._screen_broker

    @property
    def society(self) -> AgentSociety:
        return self._society

    @property
    def permission_broker(self) -> PermissionBroker:
        return self._permission_broker

    @property
    def cloud(self) -> CloudRuntime:
        return self._cloud

    def model_routing(self) -> dict[str, Any]:
        """Which model serves each role, plus cloud state — for /status so it's
        always plain which roles are remote and which stay on this machine."""
        return {"roles": self._models.describe(), "cloud": self._cloud.status()}

    def start(self) -> None:
        self._memory_worker.start()
        # Self-improvement starts regardless: its error CAPTURE is passive/safe,
        # and its active IMPROVE loop is gated by its own live-toggleable flag
        # (initialized from NOVA_AUTONOMY) so /autonomy/stop works without a
        # restart.
        self._self_improve.start()
        # Reminders/scheduling are a direct user-facing feature (not autonomy),
        # so they start regardless of NOVA_AUTONOMY.
        self._reminder_worker.start()
        # Autonomous research self-gates on NOVA_RESEARCH (off by default).
        self._research_worker.start()
        # NOVA_AUTONOMY=0 disables the background task/goal workers entirely
        # (chat, memory, tools, and the project builder keep working).
        if (os.getenv("NOVA_AUTONOMY", "1").strip() or "1").lower() in {"0", "false", "no", "off"}:
            logger.info("autonomy_workers_disabled")
            return
        self._autonomy_worker.start()
        self._agent_supervisor.start()

    async def stop(self) -> None:
        await self._memory_worker.stop()
        await self._self_improve.stop()
        await self._reminder_worker.stop()
        await self._research_worker.stop()
        await self._autonomy_worker.stop()
        await self._agent_supervisor.stop()

    async def chat_turn(
        self,
        *,
        user_text: str,
        conversation_id: UUID,
        user_name: str | None = None,
        project_name: str = "temp",
        current_location: dict[str, Any] | None = None,
    ) -> ChatTurnResult:
        # ONE production pipeline. /chat (non-streaming) and /chat/stream now
        # share the exact same pre-passes, grounding, prompt, and tool loop —
        # previously they were separate implementations and only the streaming
        # one (the path the UI uses) got fixes. This aggregates the stream.
        full: list[str] = []
        done_full = ""
        tool_calls: list[dict[str, Any]] = []
        async for ev in self.chat_turn_stream(
            user_text=user_text,
            conversation_id=conversation_id,
            user_name=user_name,
            project_name=project_name,
            current_location=current_location,
        ):
            etype = ev.get("type")
            if etype == "token":
                full.append(str(ev.get("text") or ""))
            elif etype == "done":
                done_full = str(ev.get("full_text") or "")
                tool_calls = ev.get("tool_calls") or tool_calls
        reply = (done_full or "".join(full)).strip()
        return ChatTurnResult(conversation_id=conversation_id, assistant_text=reply, tool_calls=tool_calls)

    async def _project_prepass(self, text: str) -> str | None:
        """Detect project build/status/resume/improve intents and act on them."""
        t = (text or "").strip()
        if not t:
            return None
        pb = self._project_builder

        # Plain questions ("what improvements could we make to X?", "is X a
        # good game?", "what should we add next?") are asking Nova to DISCUSS,
        # not to act. Computed up front so every branch below treats questions
        # consistently and none of them can turn into an autonomous build/
        # improve task just because the sentence happens to contain a verb
        # like "make"/"add". Uses the shared intent classifier, which is robust
        # to preamble ("I meant what...") and to a missing "?" on WH-questions —
        # the exact gap that spawned the junk "what-other-improvements-…" slug.
        looks_like_question = is_question(t)

        # "implement those suggestions" (uses last active project when unnamed)
        if IMPLEMENT_SUGG_RE.search(t):
            slug = pb.known_slug_in_text(t) or await pb.last_active()
            if slug:
                res = await pb.improve(
                    slug=slug,
                    instructions="Implement the unchecked items under 'Next steps / suggestions' in PROJECT.md.",
                )
                if res.get("started"):
                    return f"On it. I'm implementing the suggested improvements for {slug} now — I'll report when finished."
                return f"I couldn't start improvements on {slug}: {res.get('reason', 'unknown')}."

        # Mentions of a known project + status/resume/improve intent. In natural
        # conversation the project name is usually only stated once, so fall
        # back to the last-active project when a continuation/feature-request
        # intent is detected but the name wasn't repeated — unless the message
        # looks like it's naming a brand new project ("called X"/"named X").
        slug = pb.known_slug_in_text(t)
        direct_mention = slug is not None
        is_complaint = bool(CONTINUATION_COMPLAINT_RE.search(t))
        has_continuation_intent = bool(
            STATUS_WORDS_RE.search(t) or RESUME_WORDS_RE.search(t) or IMPROVE_WORDS_RE.search(t)
            or BUILD_ACTION_RE.search(t) or is_complaint
        )
        if slug is None and has_continuation_intent and not looks_like_question and not NAME_RE.search(t):
            slug = await pb.last_active()
        if slug:
            if STATUS_WORDS_RE.search(t):
                return pb.status_text(slug)
            if RESUME_WORDS_RE.search(t):
                if pb.is_building(slug):
                    return f"I'm already working on {slug} — I'll report when it's done."
                res = await pb.improve(
                    slug=slug,
                    instructions="Continue from the 'Next steps / suggestions' in PROJECT.md and finish anything incomplete.",
                )
                if res.get("started"):
                    return f"Resuming {slug} from where we left off. I'll report when finished."
                return f"I couldn't resume {slug}: {res.get('reason', 'unknown')}."
            # Any non-question message that names/points at a known project and
            # carries a work signal (improve/build verb, complaint, or just a
            # direct mention with an instruction) works on it. Questions about a
            # project ("is flappybird a good game?", "what could we add?") are
            # for DISCUSSION — the whole condition is gated behind the question
            # check so a question can never kick off an autonomous change.
            if not looks_like_question and (
                IMPROVE_WORDS_RE.search(t) or BUILD_ACTION_RE.search(t) or is_complaint or direct_mention
            ):
                if pb.is_building(slug):
                    return f"I'm still working on {slug} — I'll report the moment it's done."
                res = await pb.improve(slug=slug, instructions=t)
                if res.get("started"):
                    reopen = "taking another pass at" if is_complaint else "working on those improvements to"
                    return f"Got it — {reopen} {slug} now. I'll report when it's finished."
                return f"I couldn't start on {slug}: {res.get('reason', 'unknown')}."
            # A known/active project was referenced but this message isn't a
            # work request (most likely a plain question about it) — stop here
            # rather than falling through to "new project" detection below,
            # which would risk turning the question itself into a brand new
            # project name/slug.
            return None

        # New build request ("make a snake game called Serpent"). Never from a
        # question — "What other improvements can we make to the flappy-bird
        # game?" incidentally contains a verb ("make") + object ("game") but is
        # asking Nova to discuss ideas, not to scaffold a new project out of the
        # whole sentence.
        if not looks_like_question:
            req = ProjectBuilder.extract_start_request(t)
            if req is not None:
                name, brief = req
                if name == NEEDS_NAME:
                    # A build was clearly requested but no name was given. Ask
                    # instead of inventing a name from the sentence.
                    return "Happy to build that for you. What should I call the project?"
                res = await pb.start(name=name, brief=brief)
                if res.get("started"):
                    return (
                        f"Starting project \"{res['project']}\" in the projects folder. "
                        "I'm planning and building it now — I'll report here when it's finished."
                    )
                if res.get("reason") == "already building":
                    return f"I'm already building {res['project']} — I'll report when it's done."

        return None

    # ── Streaming chat with native function calling ─────────────────────────

    # Max reason→act→observe rounds per chat turn (env-tunable). The loop
    # itself lives in core/orchestrator/agent.py (Phase 2.1); this is the
    # default chat agent's step budget.
    _TOOL_LOOP_MAX = int(os.getenv("NOVA_AGENT_MAX_STEPS", "6").strip() or "6")

    async def chat_turn_stream(
        self,
        *,
        user_text: str,
        conversation_id: UUID,
        user_name: str | None = None,
        project_name: str = "temp",
        current_location: dict[str, Any] | None = None,
    ):
        """Async generator for streamed chat.

        Yields dicts:
          {"type": "token", "text": str}          — response text delta
          {"type": "done", "full_text": str, "tool_calls": [...]}
        Deterministic pre-passes yield their full reply as a single token.
        """
        await self._memory.initialize()
        clean_user = (user_text or "").strip()

        async def _finish(reply: str, tool_calls: list[dict[str, Any]], mode: str = "chat") -> None:
            await self._state_store.record_turn(
                conversation_id=conversation_id,
                user_message=clean_user,
                assistant_reply=reply,
                follow_up_question=None,
                mode=mode,
            )
            try:
                await self._memory_ingest_q.put(
                    MemoryIngestEvent(
                        conversation_id=conversation_id,
                        user_message=clean_user,
                        assistant_message=reply,
                        timestamp=_now(),
                        policy_memory_facts=[],
                    )
                )
            except Exception:
                pass

        # ── Deterministic pre-passes (instant, no LLM) ──────────────────────
        project_reply = await self._project_prepass(clean_user)
        if project_reply is not None:
            yield {"type": "token", "text": project_reply}
            await _finish(project_reply, [], mode="task")
            yield {"type": "done", "full_text": project_reply, "tool_calls": []}
            return

        try:
            await self._extract_quick_facts(clean_user)
        except Exception:
            pass
        try:
            await self._capture_lessons(clean_user)
        except Exception:
            pass
        try:
            await self._capture_mood(clean_user)
        except Exception:
            pass
        try:
            await self._capture_wellbeing()
        except Exception:
            pass

        direct = await self._direct_live_reply(clean_user, current_location=current_location)
        if direct is not None:
            reply, tool_calls, mode = direct
            yield {"type": "token", "text": reply}
            await _finish(reply, tool_calls, mode=mode)
            yield {"type": "done", "full_text": reply, "tool_calls": tool_calls}
            return

        # ── Context assembly ────────────────────────────────────────────────
        if user_name is None:
            try:
                f = await self._memory.get_latest_fact(entity="user", attribute="name")
                if f and f.value.strip():
                    user_name = f.value.strip()
            except Exception:
                pass

        # ── Storytelling mode ───────────────────────────────────────────────
        # A real narrative branch: craft-focused prompt, generous budget, and a
        # persistent "story bible" so a story continues across turns/sessions.
        story_state = ""
        try:
            sf = await self._memory.get_latest_fact(entity=f"conversation:{conversation_id}:story", attribute="state")
            if sf and sf.value.strip():
                story_state = sf.value.strip()
        except Exception:
            pass
        if is_story_request(clean_user, story_active=bool(story_state)):
            messages = [
                {"role": "system", "content": story_system_prompt(user_name, story_state)},
                {"role": "user", "content": clean_user},
            ]
            story_budget = int(os.getenv("NOVA_STORY_TOKENS", "1200").strip() or "1200")
            parts: list[str] = []
            async with self._llm_sem:
                async for token in self._llm.chat_stream(
                    messages, max_tokens=story_budget, temperature=0.7, thinking=True
                ):
                    parts.append(token)
                    yield {"type": "token", "text": token}
            story_reply = "".join(parts).strip()
            if not story_reply:
                async with self._llm_sem:
                    retry = await self._llm.chat(messages, max_tokens=story_budget, temperature=0.7, thinking=True)
                story_reply = (retry or "").strip()
                if story_reply:
                    yield {"type": "token", "text": story_reply}
            story_reply = story_reply or "I lost my thread there — want me to start the story again?"
            await _finish(story_reply, [], mode="story")
            # Update the running story bible for continuity (best-effort).
            try:
                new_state = await self._storyteller.update_state(
                    prior_state=story_state,
                    latest_exchange=f"{user_name or 'Reader'}: {clean_user}\nStory: {story_reply}",
                )
                if new_state.strip():
                    await self._memory.add_fact(
                        entity=f"conversation:{conversation_id}:story", attribute="state",
                        value=new_state[:2000], confidence=0.8,
                    )
            except Exception:
                pass
            yield {"type": "done", "full_text": story_reply, "tool_calls": []}
            return

        mem_hits = await self._memory.search(q=clean_user, conversation_id=conversation_id, limit=8)
        # Lessons are stored under the "lesson" entity and can surface as search
        # hits too; keep them out of the general memory block so they only appear
        # in the dedicated "lessons" section below.
        stable_mem = "\n".join(h.text for h in mem_hits if h.kind != "turn" and not str(h.text).startswith("FACT lesson "))
        grounding = await self._build_grounding_context(
            user_text=clean_user, user_name=user_name, available_tools=self._router.list_tools(),
            conversation_id=conversation_id,
        )
        try:
            lessons = await self._memory.get_lessons(limit=10)
        except Exception:
            lessons = []

        conversation_summary = ""
        try:
            summary_fact = await self._memory.get_latest_fact(entity=f"conversation:{conversation_id}", attribute="summary")
            if summary_fact and summary_fact.value.strip():
                conversation_summary = summary_fact.value.strip()
        except Exception:
            pass
        recent_chat = await self._state_store.recent_chat_text(conversation_id)

        # ── Deep mode (Phase 2.3): opt-in Planner → Executor → Critic ──
        # Explicit-request only; normal chat skips all of this and stays fast.
        deep_mode = is_deep_request(clean_user)
        deep_plan = ""
        loop_agent = self._chat_agent
        if deep_mode:
            BUS.publish("agent.stage", {"stage": "planner"})
            try:
                deep_plan = await self._deep.plan(
                    user_text=clean_user, grounding=grounding,
                    tool_catalog=self._tool_loop.tool_catalog(self._chat_agent),
                )
            except Exception as e:  # noqa: BLE001
                logger.debug("deep_plan_failed", error=str(e)[:160])
            if deep_plan:
                loop_agent = Agent(
                    name="chat-deep", step_budget=self._chat_agent.step_budget,
                    extra_instructions=f"A planner drafted this plan for the task — follow it:\n{deep_plan}",
                )
            BUS.publish("agent.stage", {"stage": "executor"})

        # ── Agent loop: reason → act → observe (core/orchestrator/agent.py) ──
        tool_results = await self._tool_loop.run(
            agent=loop_agent, user_text=clean_user, grounding=grounding
        )

        # ── Streamed final response ─────────────────────────────────────────
        tools_context = ""
        if tool_results:
            tools_context = "\nLive tool results (trust these over your own knowledge):\n" + "\n".join(
                f"- {r['tool']}: {''.join([str(r['result'])[:600]]) if r['ok'] else 'FAILED: ' + str(r['error'])[:200]}"
                for r in tool_results
            )

        try:
            grounding_text = self._grounding_to_natural(json.loads(grounding))
        except Exception:
            grounding_text = ""

        system_prompt = (
            "You are Nova — Marcus's AI companion and assistant. You're not a corporate help desk; "
            "you're a warm, sharp presence who genuinely knows Marcus and enjoys talking with him. "
            "You can be a real friend to talk to AND get real work done.\n\n"
            "How you talk:\n"
            "- Talk like a real person in a genuine conversation. React to what Marcus actually said "
            "FIRST, before anything else.\n"
            "- When he shares something about his life — his day, his kids Mateo and Liam, his wife "
            "Leslie, how he's feeling — respond to THAT like someone who cares: with warmth, real "
            "interest, a little personality. Ask about it. Don't jump to 'what do you need'.\n"
            "- It's good to start conversation and be curious about his life, not just wait for orders.\n"
            "- For actual tasks, be crisp and get to work — but you're still allowed to have a personality.\n"
            "- Never use help-desk filler ('How can I help you', 'What do you need', 'I'm here to assist', "
            "'Is there anything else'). Just talk like a person.\n"
            "- Never reuse a phrase or sentence shape you already used earlier in the conversation.\n"
            "- Keep it conversational — a sentence or a few. Go longer only when the moment truly calls for it.\n"
            "- Never invent tool results or reasons. If a tool failed, say what ACTUALLY failed (e.g. the exact "
            "error) — never guess a cause or invent a blocker you're not sure about.\n"
            "- If a memory.recall result comes back with confidence: low, that's a fuzzy/weak match — say so "
            "honestly ('I think...', 'I'm not totally sure, but...') instead of stating it as settled fact. A "
            "confidence: high result you can state plainly.\n"
            "- Building or improving a PROJECT in the projects folder does NOT require developer mode; that "
            "happens automatically. Developer mode is only for editing your OWN source code. Never tell Marcus "
            "to enable developer mode for a project task.\n"
            "- Don't claim a feature works if you only wrote code for it. For anything visual or interactive you "
            "can't fully test, say you added it and ask him to try it, rather than declaring it done.\n\n"
            + (f"Who you're talking to: {grounding_text}\n" if grounding_text else "")
            + (
                "Lessons you've learned from Marcus — apply these unless he says otherwise:\n"
                + "\n".join(f"- {l}" for l in lessons) + "\n"
                if lessons else ""
            )
            + (f"Earlier in this conversation: {conversation_summary}\n" if conversation_summary else "")
            + (f"Things you remember:\n{stable_mem}\n" if stable_mem else "")
            + (f"Recent messages:\n{recent_chat}\n" if recent_chat else "")
            + tools_context
            + "\n\nIMPORTANT: Reply with ONLY what you'd actually say to Marcus out loud. Do NOT write "
            "any analysis, planning, notes, or a reasoning/<think> block — just say your reply directly."
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": clean_user},
        ]

        # ── Deep mode reply: draft → Critic → one optional revision ──────────
        # Not streamed (the draft may be revised); the final lands as one chunk.
        if deep_mode:
            reply_budget = int(os.getenv("NOVA_MAX_TOKENS", "1536").strip() or "1536")
            chat_model = self._models.for_role("chat")
            async with chat_model.semaphore:
                draft = await chat_model.runtime.chat(
                    messages, max_tokens=reply_budget, temperature=0.4, thinking=True
                )
            draft = (draft or "").strip()
            BUS.publish("agent.stage", {"stage": "critic"})
            verdict, notes = "approve", ""
            try:
                verdict, notes = await self._deep.critique(
                    user_text=clean_user, plan=deep_plan, draft=draft, tool_results=tool_results
                )
            except Exception as e:  # noqa: BLE001
                logger.debug("deep_critique_failed", error=str(e)[:160])
            reply = draft
            if verdict == "revise" and notes:
                BUS.publish("agent.stage", {"stage": "revise", "notes": notes[:160]})
                revise_msgs = messages + [
                    {"role": "assistant", "content": draft},
                    {"role": "user", "content": (
                        f"Before sending, a reviewer flagged: {notes}. Fix those issues while keeping your "
                        "natural voice and staying honest about anything uncertain or that failed. Reply with "
                        "ONLY the corrected message.")},
                ]
                async with chat_model.semaphore:
                    revised = await chat_model.runtime.chat(
                        revise_msgs, max_tokens=reply_budget, temperature=0.4, thinking=True
                    )
                if (revised or "").strip():
                    reply = revised.strip()
            if not reply:
                reply = "Sorry — I came up empty on that one."
            yield {"type": "token", "text": reply}
            await _finish(reply, tool_results, mode="deep")
            yield {"type": "done", "full_text": reply, "tool_calls": tool_results}
            return

        # This reasoning model ignores the '/no_think' switch and reasons anyway;
        # forcing it there produces a long unclosed <think> that overflows the
        # token budget and gets stripped to nothing ("came up empty"). Instead we
        # pass thinking=True (no '/no_think') and rely on the direct-reply
        # instruction above to keep the hidden reasoning to a line or two. Proven
        # far more reliable in practice.
        reply_budget = int(os.getenv("NOVA_MAX_TOKENS", "1536").strip() or "1536")
        chat_model = self._models.for_role("chat")
        full: list[str] = []
        async with chat_model.semaphore:
            async for token in chat_model.runtime.chat_stream(
                messages, max_tokens=reply_budget, temperature=0.4, thinking=True
            ):
                full.append(token)
                yield {"type": "token", "text": token}

        reply = "".join(full).strip()
        if not reply:
            # Rare overflow (the model reasoned past the budget without closing
            # the think block). The non-streaming chat() retries internally; give
            # it extra room and an explicit terse-answer nudge so it lands.
            salvage = messages + [{
                "role": "user",
                "content": "Reply now in one or two warm, natural sentences. No analysis, no reasoning — just talk.",
            }]
            async with self._llm_sem:
                retry = await self._llm.chat(
                    salvage, max_tokens=512, temperature=0.4, thinking=True
                )
            reply = (retry or "").strip()
            if reply:
                yield {"type": "token", "text": reply}
        if not reply:
            reply = "Sorry — I came up empty on that one."
        await _finish(reply, tool_results)
        yield {"type": "done", "full_text": reply, "tool_calls": tool_results}

    @staticmethod
    def _dedup_vals(vals: list[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for v in vals or []:
            vv = (v or "").strip()
            if not vv:
                continue
            key = vv.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(vv)
        return out

    # ── Maps: "always ask" location flow (WS-C) ─────────────────────────────
    # Per Marcus's choice, Nova never silently assumes his location for a
    # nearby/route request — she asks (or takes an explicit "use my current
    # location"), remembers the pending request, and completes it once he
    # answers. State is a short-lived session fact so it survives the turn
    # boundary without touching the conversation-state plumbing.

    async def _set_pending_map(self, data: dict[str, Any]) -> None:
        payload = {**data, "ts": _now().isoformat()}
        await self._memory.add_fact(
            entity="session", attribute="pending_map_request", value=json.dumps(payload), confidence=1.0
        )

    async def _get_pending_map(self) -> dict[str, Any] | None:
        f = await self._memory.get_latest_fact(entity="session", attribute="pending_map_request")
        if not f or not f.value:
            return None
        try:
            data = json.loads(f.value)
            ts = datetime.fromisoformat(str(data.get("ts", "")))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except Exception:
            return None
        if (_now() - ts).total_seconds() > 300:  # expire stale pending requests (5 min)
            return None
        return data

    async def _clear_pending_map(self) -> None:
        await self._memory.add_fact(
            entity="session", attribute="pending_map_request", value="", confidence=1.0
        )

    async def _run_nearby(self, query: str, lat: Any, lng: Any) -> tuple[str, list[dict[str, Any]], str]:
        call = ToolCall(name="maps.places_nearby", args={"query": query, "lat": lat, "lng": lng, "limit": 6})
        res = await self._router.execute(call, timeout_s=15.0, retries=0)
        tool_calls = [{"tool": call.name, "ok": res.ok, "error": res.error, "result": res.result}]
        if res.ok and isinstance(res.result, dict) and res.result.get("places"):
            top = res.result["places"][0]
            name = str(top.get("name") or "that place").strip()
            address = str(top.get("address") or "").strip()
            dist = top.get("distance_meters")
            miles = f" ({round(float(dist) / 1609.344, 1)} mi away)" if isinstance(dist, (int, float)) else ""
            where = f" at {address}" if address else ""
            return (
                f"Closest {query}: {name}{where}{miles}. I put the options on the map — tap one and I'll route you there.",
                tool_calls, "smalltalk",
            )
        return f"I couldn't find any {query} near there right now.", tool_calls, "smalltalk"

    async def _run_route(self, origin: str, destination: str, mode: str = "driving") -> tuple[str, list[dict[str, Any]], str]:
        call = ToolCall(name="maps.directions", args={"origin": origin, "destination": destination, "mode": mode})
        res = await self._router.execute(call, timeout_s=15.0, retries=0)
        tool_calls = [{"tool": call.name, "ok": res.ok, "error": res.error, "result": res.result}]
        if res.ok and isinstance(res.result, dict) and res.result.get("status") == "OK":
            await self._save_last_route(res.result)
            return _format_directions_reply(res.result), tool_calls, "smalltalk"
        return f"I couldn't pull directions to {destination} right now.", tool_calls, "smalltalk"

    async def _save_last_route(self, route: dict[str, Any]) -> None:
        """Remember the last route's steps so Nova can read the turn-by-turn
        aloud on request (WS-D desktop narration)."""
        steps = [
            str(s.get("instruction") or "").strip()
            for s in (route.get("steps") or [])
            if str(s.get("instruction") or "").strip()
        ]
        if not steps:
            return
        payload = {"destination": route.get("destination"), "steps": steps[:25], "ts": _now().isoformat()}
        await self._memory.add_fact(entity="session", attribute="last_route", value=json.dumps(payload), confidence=1.0)

    async def _get_last_route(self) -> dict[str, Any] | None:
        f = await self._memory.get_latest_fact(entity="session", attribute="last_route")
        if not f or not f.value:
            return None
        try:
            data = json.loads(f.value)
            ts = datetime.fromisoformat(str(data.get("ts", "")))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except Exception:
            return None
        if (_now() - ts).total_seconds() > 1800:  # 30 min freshness
            return None
        return data

    async def _dispatch_pending_map(
        self, pending: dict[str, Any], *, lat: Any, lng: Any, coords_text: str
    ) -> tuple[str, list[dict[str, Any]], str] | None:
        kind = pending.get("kind")
        if kind == "nearest":
            return await self._run_nearby(str(pending.get("query") or ""), lat, lng)
        if kind == "route":
            return await self._run_route(coords_text, str(pending.get("dest") or ""), str(pending.get("mode") or "driving"))
        return None

    async def _resolve_pending_map(
        self, pending: dict[str, Any], text: str, current_location: dict[str, Any] | None
    ) -> tuple[str, list[dict[str, Any]], str] | None:
        # (a) explicit opt-in to device location
        if _wants_device_location(text):
            if current_location is None:
                return ("I don't have a device location right now — what's your address or city?", [], "smalltalk")
            await self._clear_pending_map()
            return await self._dispatch_pending_map(
                pending, lat=current_location.get("lat"), lng=current_location.get("lng"),
                coords_text=_current_coords_text(current_location) or "",
            )
        # (b) not a location answer -> abandon the pending request, let normal handling take over
        if not _looks_like_location_answer(text):
            await self._clear_pending_map()
            return None
        # (c) treat the message as a stated location -> geocode it
        geo = await self._router.execute(ToolCall(name="maps.geocode", args={"address": text}), timeout_s=15.0, retries=0)
        loc = (geo.result or {}).get("location") if (geo.ok and isinstance(geo.result, dict)) else None
        if not loc or loc.get("lat") is None or loc.get("lng") is None:
            return (f"I couldn't find '{text}' on the map — can you give me a city or a full address?", [], "smalltalk")
        await self._clear_pending_map()
        return await self._dispatch_pending_map(
            pending, lat=loc["lat"], lng=loc["lng"], coords_text=f"{loc['lat']},{loc['lng']}"
        )

    async def _direct_live_reply(
        self,
        user_text: str,
        *,
        current_location: dict[str, Any] | None = None,
    ) -> tuple[str, list[dict[str, Any]], str] | None:
        text = (user_text or "").strip()
        if not text:
            return None

        coords_text = _current_coords_text(current_location)

        # Resolve a pending "where are you?" from a prior maps request first.
        pending_map = await self._get_pending_map()
        if pending_map is not None:
            resolved = await self._resolve_pending_map(pending_map, text, current_location)
            if resolved is not None:
                return resolved

        # "Read me the turn-by-turn" for the most recent route (WS-D narration).
        if _READ_STEPS_RE.search(text):
            last = await self._get_last_route()
            if last and last.get("steps"):
                steps = last["steps"]
                dest = str(last.get("destination") or "your destination").strip()
                spoken = " ".join(f"Step {i + 1}: {s}." for i, s in enumerate(steps[:12]))
                more = f" That's the first 12 of {len(steps)} steps." if len(steps) > 12 else ""
                return f"Here's the route to {dest}. {spoken}{more}", [], "smalltalk"

        if _looks_like_name_query(text):
            name_fact = await self._memory.get_latest_fact(entity="user", attribute="name")
            if name_fact and name_fact.value.strip():
                return f"Yes. Your name is {name_fact.value.strip()}.", [], "smalltalk"
            return "I don't know your name yet. Tell me your name and I'll remember it.", [], "smalltalk"

        stated_name = _extract_user_name(text)
        if stated_name:
            return f"Okay. I'll remember your name as {stated_name}.", [], "smalltalk"

        if _looks_like_time_query(text):
            return _format_clock_reply(text), [], "smalltalk"

        if "weather" in text.lower():
            city = _extract_weather_city(text)
            if city:
                call = ToolCall(name="weather.current", args={"city": city, "units": "imperial"})
                res = await self._router.execute(call, timeout_s=10.0, retries=0)
                tool_calls = [{"tool": call.name, "ok": res.ok, "error": res.error, "result": res.result}]
                if res.ok and isinstance(res.result, dict):
                    return _format_weather_reply(city, res.result), tool_calls, "smalltalk"
                return f"I could not pull the live weather for {city} right now.", tool_calls, "smalltalk"

        nearest_query = _extract_nearest_query(text)
        if nearest_query is not None:
            # Always ask where to search from (never silently assume the device
            # location) unless Marcus explicitly opts into it.
            if _wants_device_location(text) and current_location is not None:
                return await self._run_nearby(nearest_query, current_location.get("lat"), current_location.get("lng"))
            await self._set_pending_map({"kind": "nearest", "query": nearest_query})
            return (
                f"Sure — where should I look for the nearest {nearest_query}? Give me a city or address, "
                "or say 'use my current location'.",
                [], "smalltalk",
            )

        place_query = _extract_place_lookup_query(text)
        if place_query is not None:
            tool_calls: list[dict[str, Any]] = []

            place_call = ToolCall(name="maps.place_search", args={"query": place_query, "limit": 6})
            place_res = await self._router.execute(place_call, timeout_s=15.0, retries=0)
            tool_calls.append({"tool": place_call.name, "ok": place_res.ok, "error": place_res.error, "result": place_res.result})
            if place_res.ok and isinstance(place_res.result, dict) and place_res.result.get("places"):
                top = place_res.result["places"][0]
                name = str(top.get("name") or place_query).strip()
                address = str(top.get("address") or "").strip()
                if address:
                    return f"I found {name} at {address}. I opened it on the map.", tool_calls, "smalltalk"
                return f"I found {name}. I opened it on the map.", tool_calls, "smalltalk"

            geocode_call = ToolCall(name="maps.geocode", args={"address": place_query})
            geocode_res = await self._router.execute(geocode_call, timeout_s=15.0, retries=0)
            tool_calls.append({"tool": geocode_call.name, "ok": geocode_res.ok, "error": geocode_res.error, "result": geocode_res.result})
            if geocode_res.ok and isinstance(geocode_res.result, dict) and geocode_res.result.get("formatted_address"):
                address = str(geocode_res.result.get("formatted_address") or place_query).strip()
                return f"I found {place_query} at {address}. I opened it on the map.", tool_calls, "smalltalk"

            return f"I could not find a map result for {place_query} right now.", tool_calls, "smalltalk"

        local_destination = _extract_destination_from_here(text)
        if local_destination is not None or (_FROM_HERE_RE.search(text) and " to " in text.lower()):
            destination = local_destination or text
            # Always ask where he's starting from (never silently assume the
            # device location) unless he explicitly opts into it.
            if _wants_device_location(text) and current_location is not None and coords_text is not None:
                return await self._run_route(coords_text, destination)
            await self._set_pending_map({"kind": "route", "dest": destination, "mode": "driving"})
            return (
                f"Happy to route you to {destination} — where are you starting from? A city or address, "
                "or say 'use my current location'.",
                [], "smalltalk",
            )

        dirs = _extract_directions(text)
        if dirs:
            origin, destination, mode = dirs
            return await self._run_route(origin, destination, mode)

        return None

    async def _try_tool_assist(self, user_text: str) -> tuple[str, list[dict]]:
        """Pre-call live tools (weather, directions) so the LLM gets real data."""
        snippets: list[str] = []
        results: list[dict] = []

        # ── Weather ──────────────────────────────────────────────────────────
        if "weather" in user_text.lower():
            city = _extract_weather_city(user_text)
            if city:
                call = ToolCall(name="weather.current", args={"city": city, "units": "imperial"})
                res = await self._router.execute(call, timeout_s=10.0, retries=0)
                results.append({"tool": call.name, "ok": res.ok, "error": res.error, "result": res.result})
                if res.ok and res.result:
                    r = res.result
                    snippets.append(
                        f'live_weather: {{"city": "{city}", "condition": "{r.get("description","")}", '
                        f'"temp_f": {r.get("temp")}, "feels_like_f": {r.get("feels_like")}, '
                        f'"humidity_pct": {r.get("humidity")}}}'
                    )

        # ── Directions ───────────────────────────────────────────────────────
        dirs = _extract_directions(user_text)
        if dirs:
            origin, destination, mode = dirs
            call = ToolCall(name="maps.directions", args={"origin": origin, "destination": destination, "mode": mode})
            res = await self._router.execute(call, timeout_s=15.0, retries=0)
            results.append({"tool": call.name, "ok": res.ok, "error": res.error, "result": res.result})
            if res.ok and res.result:
                r = res.result
                snippets.append(
                    f'live_directions: {{"origin": "{origin}", "destination": "{destination}", '
                    f'"mode": "{mode}", "distance": "{r.get("distance","")}", '
                    f'"duration": "{r.get("duration","")}", "steps_count": {len(r.get("steps", []))}}}'
                )

        return "\n".join(snippets), results

    async def _build_grounding_context(
        self,
        *,
        user_text: str,
        user_name: str | None,
        available_tools: list[str],
        conversation_id: UUID | None = None,
    ) -> str:
        del user_text
        context: dict[str, Any] = {
            "known_user": {},
            "known_family": {},
            "known_people": {},
            "capabilities": {},
            "available_tools": [],
        }

        if (user_name or "").strip():
            context["known_user"]["name"] = (user_name or "").strip()

        # ── Independent read-only signals, fetched CONCURRENTLY (U1) ─────────
        # These five blocks hit different tables and don't depend on each other,
        # so they run in one round-trip instead of ~12 sequential awaits — on
        # every single turn. Each helper keeps its own try/except and returns
        # None on failure, preserving the previous "one bad signal never breaks
        # the turn" semantics exactly (including: if any family read fails, the
        # whole family block is skipped, as before).
        #
        # The write-involving blocks BELOW (wellbeing nudge, session gap,
        # executive throttle) stay sequential on purpose — they mutate state and
        # are order-dependent.

        async def _load_family() -> dict[str, Any] | None:
            try:
                mother, father, spouse, children, siblings, cousins, friends, pets = await asyncio.gather(
                    self._memory.get_latest_fact(entity="user", attribute="mother"),
                    self._memory.get_latest_fact(entity="user", attribute="father"),
                    self._memory.get_latest_fact(entity="user", attribute="spouse"),
                    self._memory.get_facts(entity="user", attribute="child", limit=25, newest_first=False),
                    self._memory.get_facts(entity="user", attribute="sibling", limit=25, newest_first=False),
                    self._memory.get_facts(entity="user", attribute="cousin", limit=25, newest_first=False),
                    self._memory.get_facts(entity="user", attribute="friend", limit=25, newest_first=False),
                    self._memory.get_facts(entity="user", attribute="pet", limit=25, newest_first=False),
                )
            except Exception:
                return None

            family: dict[str, Any] = {}
            people: dict[str, Any] = {}
            if mother and mother.value.strip():
                family["mother"] = mother.value.strip()
            if father and father.value.strip():
                family["father"] = father.value.strip()
            if spouse and spouse.value.strip():
                family["spouse"] = spouse.value.strip()

            child_vals = self._dedup_vals([c.value for c in children])
            if child_vals:
                family["children"] = child_vals

            sibling_vals = self._dedup_vals([c.value for c in siblings])
            if sibling_vals:
                family["siblings"] = sibling_vals

            cousin_vals = self._dedup_vals([c.value for c in cousins])
            if cousin_vals:
                family["cousins"] = cousin_vals

            friend_vals = self._dedup_vals([c.value for c in friends])
            if friend_vals:
                people["friends"] = friend_vals

            pet_vals = self._dedup_vals([c.value for c in pets])
            if pet_vals:
                people["pets"] = [(p.split("|", 1)[0] if "|" in p else p) for p in pet_vals]
            return {"family": family, "people": people}

        # Current focus — which project we're working on and what else exists.
        # Without this, the free-form agent tool loop (_decide_tool) decides
        # whether to start a NEW project with zero awareness one is already
        # active, which is how a follow-up got turned into a duplicate/junk
        # project. Give the model the same "what are we on" context the regex
        # pre-pass uses.
        async def _load_focus() -> dict[str, Any] | None:
            try:
                pb = self._project_builder
                known_projects = pb.list_projects()
                active = await pb.last_active()
                focus: dict[str, Any] = {}
                if active:
                    focus["active_project"] = active
                if known_projects:
                    focus["known_projects"] = known_projects[:20]
                return focus or None
            except Exception:
                return None

        # Recent mood trend (M1) — a coarse, honestly-labeled signal so replies
        # can be a little warmer/more attentive when it's been a rough
        # stretch, without claiming deep insight into how Marcus feels.
        async def _load_mood() -> str | None:
            try:
                return (await self._memory.recent_mood_trend(days=3)) or None
            except Exception:
                return None

        # Upcoming birthdays/anniversaries (MR1) — same window as the
        # reminder-worker's proactive nudge, so if it comes up naturally in
        # conversation she already has it, without re-announcing every turn.
        async def _load_upcoming_dates() -> list[dict[str, Any]] | None:
            try:
                upcoming = await self._memory.list_people_with_upcoming_dates(within_days=3)
                if not upcoming:
                    return None
                return [
                    {"name": u["name"], "label": u["label"], "days_until": u["days_until"]} for u in upcoming[:5]
                ]
            except Exception:
                return None

        # Long-horizon interest drift (MR1) — distilled periodically by the
        # self-improve reflection cycle, not computed fresh every turn.
        async def _load_drift() -> str | None:
            try:
                return (await self._memory.recent_interest_drift(weeks=6)) or None
            except Exception:
                return None

        relations, focus_ctx, mood_trend, upcoming_dates, drift_line = await asyncio.gather(
            _load_family(), _load_focus(), _load_mood(), _load_upcoming_dates(), _load_drift(),
        )

        if relations:
            context["known_family"].update(relations["family"])
            context["known_people"].update(relations["people"])
            # Feed the cloud context firewall's redaction list for free — these
            # names were just fetched anyway, so nothing extra is queried.
            names: list[str] = []
            for value in list(relations["family"].values()) + list(relations["people"].values()):
                names.extend(value if isinstance(value, list) else [value])
            self._identity_cache = (
                (user_name or "").strip() or None,
                [n for n in names if isinstance(n, str) and n.strip()],
            )
        if focus_ctx:
            context["current_focus"] = focus_ctx
        if mood_trend:
            context["recent_mood"] = mood_trend
        if upcoming_dates:
            context["relationship_reminders"] = upcoming_dates
        if drift_line:
            context["interest_drift"] = drift_line

        # Wellbeing trend (WB1) — gentler than mood: only surfaced once every
        # few days (should_nudge_wellbeing), never every single turn.
        try:
            if await self._memory.should_nudge_wellbeing():
                wb_trend = await self._memory.recent_wellbeing_trend(days=5)
                if wb_trend:
                    context["wellbeing_trend"] = wb_trend
                    await self._memory.mark_wellbeing_nudged()
        except Exception:
            pass

        # Continuity across gaps (CG1) — "catch me up" on the first turn of a
        # genuinely new conversation, if enough time has passed since the last
        # one. `check_and_mark_session_gap` always advances last_active (so it
        # must be called every turn to stay accurate); the summary is only
        # surfaced when this is a conversation_id with no prior turns recorded
        # AND the gap clears the threshold — never mid-conversation.
        try:
            is_new_conversation = True
            if conversation_id is not None:
                conv_state = await self._state_store.load(conversation_id)
                is_new_conversation = not conv_state.last_user_messages
            gap = await self._memory.check_and_mark_session_gap()
            if is_new_conversation and gap is not None and gap.total_seconds() >= 6 * 3600:
                since_iso = (_now() - gap).isoformat()
                catchup = await self._memory.build_catchup_summary(since_iso)
                if catchup:
                    context["catchup_summary"] = catchup
        except Exception:
            pass

        # Executive intelligence (#1) — at most a couple of proactive, confidence-
        # gated recommendations (looming deadline, focus window, take a break),
        # throttled so the same nudge never repeats. These are opportunities to be
        # helpful if they fit naturally, NOT a script to recite.
        try:
            exec_recs = await self._memory.executive_recommendations(throttle=True)
            if exec_recs:
                context["executive_recommendations"] = [r["message"] for r in exec_recs[:2]]
        except Exception:
            pass

        # Internal operational state (#12) — advisory operating hints derived
        # from live telemetry (uncertainty / workload / energy / confidence).
        # Present ONLY when a threshold is crossed, so most turns add nothing.
        # These influence HOW Nova responds (hedge, stay concise, ask a question),
        # never WHAT is true. Cheap (in-memory snapshot, no DB read).
        try:
            op_hints = self._self_improve.operating_hints()
            if op_hints:
                context["operating_state"] = op_hints
        except Exception:
            pass

        tool_names = sorted({str(t).strip() for t in (available_tools or []) if str(t).strip()})
        if tool_names:
            context["available_tools"] = tool_names

        smart_home_tools = [t for t in tool_names if ("smart" in t.lower() or "home" in t.lower())]
        if smart_home_tools:
            context["capabilities"]["smart_home_control"] = "available"
        else:
            context["capabilities"]["smart_home_control"] = "unavailable"

        # Inject current local date/time so Nova never has to guess.
        now_local = datetime.now().astimezone()
        context["current_datetime"] = {
            "date": now_local.strftime("%A, %B %d, %Y"),
            "time": now_local.strftime("%I:%M %p"),
            "timezone": str(now_local.tzinfo),
        }

        return json.dumps(context, ensure_ascii=True)

    @staticmethod
    def _grounding_to_natural(context: dict[str, Any]) -> str:
        """Render the grounding context dict as a short natural-language line
        instead of a raw JSON dump, so it reads as ambient context rather than
        a data report."""
        parts: list[str] = []

        known_user = context.get("known_user") or {}
        if known_user.get("name"):
            parts.append(f"the user's name is {known_user['name']}")

        family = context.get("known_family") or {}
        fam_bits: list[str] = []
        if family.get("mother"):
            fam_bits.append(f"mother {family['mother']}")
        if family.get("father"):
            fam_bits.append(f"father {family['father']}")
        if family.get("spouse"):
            fam_bits.append(f"spouse {family['spouse']}")
        if family.get("children"):
            fam_bits.append("children " + ", ".join(family["children"]))
        if family.get("siblings"):
            fam_bits.append("siblings " + ", ".join(family["siblings"]))
        if family.get("cousins"):
            fam_bits.append("cousins " + ", ".join(family["cousins"]))
        if fam_bits:
            parts.append("known family: " + "; ".join(fam_bits))

        people = context.get("known_people") or {}
        if people.get("friends"):
            parts.append("friends: " + ", ".join(people["friends"]))
        if people.get("pets"):
            parts.append("pets: " + ", ".join(people["pets"]))

        focus = context.get("current_focus") or {}
        if focus.get("active_project"):
            parts.append(
                f"you are currently working with the user on the '{focus['active_project']}' project — "
                "if they refer to 'it', 'the game', 'the project', or ask to change/add/fix something "
                "without naming a project, they almost certainly mean this one; do NOT start a new project for that"
            )
        if focus.get("known_projects"):
            parts.append("existing projects: " + ", ".join(focus["known_projects"]))

        if context.get("recent_mood"):
            mood_line = str(context["recent_mood"]).rstrip(".")
            parts.append(
                mood_line + " — let that inform your warmth and attentiveness where it's naturally "
                "relevant, without mentioning it out of nowhere or making a big deal of it"
            )

        relationship_reminders = context.get("relationship_reminders") or []
        if relationship_reminders:
            bits = []
            for r in relationship_reminders:
                when = "today" if r.get("days_until") == 0 else f"in {r.get('days_until')} day(s)"
                bits.append(f"{r.get('name')}'s {r.get('label')} is {when}")
            parts.append("upcoming: " + "; ".join(bits))

        if context.get("interest_drift"):
            parts.append(str(context["interest_drift"]).rstrip("."))

        if context.get("wellbeing_trend"):
            wb_line = str(context["wellbeing_trend"]).rstrip(".")
            parts.append(
                wb_line + " — mention this gently, once, only if it feels natural; don't lecture or make it a big deal"
            )

        if context.get("catchup_summary"):
            parts.append(
                str(context["catchup_summary"]).rstrip(".")
                + " — this is the start of a new conversation after a while away; naturally open with something "
                "like this instead of a generic greeting, but keep it brief and conversational, not a bulleted report"
            )

        op_state = context.get("operating_state") or []
        if op_state:
            parts.append(
                "internal operating note (guides HOW you respond, not what's true; never mention it aloud): "
                + " ".join(str(h) for h in op_state)
            )

        exec_recs = context.get("executive_recommendations") or []
        if exec_recs:
            parts.append(
                "proactive context you MAY raise if it fits naturally (don't force it, don't list all of it): "
                + " | ".join(str(r) for r in exec_recs)
            )

        caps = context.get("capabilities") or {}
        if caps.get("smart_home_control") == "available":
            parts.append("smart home control is available")
        elif caps.get("smart_home_control") == "unavailable":
            parts.append("smart home control is not connected")

        now = context.get("current_datetime") or {}
        if now:
            parts.append(f"right now it's {now.get('time')} on {now.get('date')} ({now.get('timezone')})")

        if not parts:
            return ""
        return "; ".join(parts) + "."

    @staticmethod
    def _natural_join(vals: list[str]) -> str:
        items = [str(v).strip() for v in vals if str(v).strip()]
        if not items:
            return ""
        if len(items) == 1:
            return items[0]
        if len(items) == 2:
            return items[0] + " and " + items[1]
        return ", ".join(items[:-1]) + ", and " + items[-1]

    @staticmethod
    def _split_name_list(text: str) -> list[str]:
        if not text:
            return []
        t = text.strip()
        t = re.sub(r"\s*(?:,|&|\band\b)\s*", "|", t, flags=re.IGNORECASE)
        parts = [p.strip(" .;:!\n\t") for p in t.split("|")]
        out: list[str] = []
        for p in parts:
            if not p:
                continue
            p = re.sub(r"[^A-Za-z\-\'\s]", "", p).strip()
            if len(p) < 2:
                continue
            out.append(" ".join([w.capitalize() for w in p.split() if w]))
        seen: set[str] = set()
        ded: list[str] = []
        for n in out:
            k = n.lower()
            if k in seen:
                continue
            seen.add(k)
            ded.append(n)
        return ded

    @staticmethod
    def _pet_value(name: str, species: str) -> str:
        s = (species or "").strip().lower()
        n = (name or "").strip()
        return f"{n}|{s}" if s else n

    async def _replace_user_name_fact(self, name: str) -> None:
        clean_name = (name or "").strip()
        if not clean_name:
            return
        try:
            await self._memory.purge_facts(entity="user", attribute="name", dry_run=False)
        except Exception:
            pass
        await self._memory.add_fact(entity="user", attribute="name", value=clean_name, confidence=0.98)

    async def _extract_quick_facts(self, message: str) -> None:
        msg = (message or "").strip()
        if not msg:
            return

        user_name = _extract_user_name(msg)
        if user_name:
            await self._replace_user_name_fact(user_name)

        # Location
        m_loc = re.search(r"\bi\s+live\s+in\s+([A-Za-z][A-Za-z0-9 _\-]{2,60})\b", msg, flags=re.IGNORECASE)
        if m_loc:
            loc = m_loc.group(1).strip(" .!?\t\n")
            if loc:
                await self._memory.add_fact(entity="user", attribute="location", value=loc, confidence=0.75)

        # Spouse
        m_sp = re.search(
            r"\bmy\s+(wife|husband|spouse)\b(?:\s+name\s+is|\s+is|\s+named)?\s+([A-Z][A-Za-z0-9'\-_]{1,40})\b",
            msg,
            flags=re.IGNORECASE,
        )
        if m_sp:
            spouse_name = m_sp.group(2).strip()
            if spouse_name:
                await self._memory.add_fact(entity="user", attribute="spouse", value=spouse_name, confidence=0.85)

        # Parents
        parent_patterns = [
            (r"\bmy\s+mom\s+(?:is|=|named)\s+([A-Z][A-Za-z\-']+(?:\s+[A-Z][A-Za-z\-']+)*)", "mother"),
            (r"\bmy\s+mother\s+(?:is|=|named)\s+([A-Z][A-Za-z\-']+(?:\s+[A-Z][A-Za-z\-']+)*)", "mother"),
            (r"\bmy\s+dad\s+(?:is|=|named)\s+([A-Z][A-Za-z\-']+(?:\s+[A-Z][A-Za-z\-']+)*)", "father"),
            (r"\bmy\s+father\s+(?:is|=|named)\s+([A-Z][A-Za-z\-']+(?:\s+[A-Z][A-Za-z\-']+)*)", "father"),
            (r"\bmy\s+mom['’]s\s+name\s+is\s+([A-Z][A-Za-z\-']+(?:\s+[A-Z][A-Za-z\-']+)*)", "mother"),
            (r"\bmy\s+mother['’]s\s+name\s+is\s+([A-Z][A-Za-z\-']+(?:\s+[A-Z][A-Za-z\-']+)*)", "mother"),
            (r"\bmy\s+dad['’]s\s+name\s+is\s+([A-Z][A-Za-z\-']+(?:\s+[A-Z][A-Za-z\-']+)*)", "father"),
            (r"\bmy\s+father['’]s\s+name\s+is\s+([A-Z][A-Za-z\-']+(?:\s+[A-Z][A-Za-z\-']+)*)", "father"),
        ]
        for pat, attr in parent_patterns:
            mm = re.search(pat, msg, flags=re.IGNORECASE)
            if mm:
                name = mm.group(1).strip()
                if name:
                    await self._memory.add_fact(entity="user", attribute=attr, value=name, confidence=0.9)

        # Children list
        m_kids = re.search(
            r"\b(?:i\s+have|my)\s+(?:two\s+|three\s+|four\s+|\d+\s+)?(sons|son|daughters|daughter|kids|children)\b(?:\s+(?:named|are|:))?\s+(.+)$",
            msg,
            flags=re.IGNORECASE,
        )
        if m_kids:
            rel = m_kids.group(1).lower()
            tail = re.sub(r"[.?!]+$", "", m_kids.group(2).strip()).strip()
            for n in self._split_name_list(tail)[:6]:
                await self._memory.add_fact(entity="user", attribute="child", value=n, confidence=0.8)
            if rel in {"sons", "son"}:
                await self._memory.add_fact(entity="user", attribute="children_type", value="sons", confidence=0.7)
            elif rel in {"daughters", "daughter"}:
                await self._memory.add_fact(entity="user", attribute="children_type", value="daughters", confidence=0.7)

        # Siblings / cousins / friends lists (simple)
        list_patterns = [
            (r"\bmy\s+(?:siblings|brothers|sisters)\b(?:\s+(?:are|:|named))?\s+(.+)$", "sibling", 0.85),
            (r"\bmy\s+cousins?\b(?:\s+(?:are|:|named))?\s+(.+)$", "cousin", 0.85),
            (r"\bmy\s+friends?\b(?:\s+(?:are|:|named))?\s+(.+)$", "friend", 0.8),
        ]
        for pat, attr, conf in list_patterns:
            mm = re.search(pat, msg, flags=re.IGNORECASE)
            if mm:
                tail = re.sub(r"[.?!]+$", "", mm.group(1).strip()).strip()
                for n in self._split_name_list(tail)[:10]:
                    await self._memory.add_fact(entity="user", attribute=attr, value=n, confidence=conf)

        # Pets
        m_pet = re.search(r"\bmy\s+(dog|cat|pet)\b(?:\s+(?:is|named))\s+([A-Z][A-Za-z\-']+(?:\s+[A-Z][A-Za-z\-']+)*)\b", msg, flags=re.IGNORECASE)
        if m_pet:
            species = m_pet.group(1).strip().lower()
            name = m_pet.group(2).strip()
            if name:
                await self._memory.add_fact(entity="user", attribute="pet", value=self._pet_value(name, species), confidence=0.85)

    # Directive/preference/correction phrasings that mean "learn this and apply it
    # going forward". Kept deliberately explicit so ordinary chatter isn't captured.
    _LESSON_PATTERNS = [
        re.compile(r"\b(?:from now on|going forward|in the future|next time)\b", re.IGNORECASE),
        re.compile(r"\bi (?:prefer|'d rather|would rather|really like|like it when|don't like it when|hate it when)\b", re.IGNORECASE),
        re.compile(r"^\s*(?:please\s+)?(?:always|never)\b", re.IGNORECASE),
        re.compile(r"\b(?:make sure (?:to|you)|remember to|be sure to)\b", re.IGNORECASE),
        re.compile(r"^\s*(?:please\s+)?(?:stop|don'?t)\b", re.IGNORECASE),
        re.compile(r"^\s*no,?\s+(?:that'?s|thats|it'?s)\s+(?:wrong|not right|incorrect)\b", re.IGNORECASE),
        re.compile(r"\bi (?:told|asked) you (?:to|not to)\b", re.IGNORECASE),
    ]
    # Questions and build/status requests shouldn't be mistaken for lessons.
    _LESSON_SKIP = re.compile(r"\?\s*$|^\s*(?:what|why|how|when|who|which|can you|could you|make (?:a|me|an)|build|create)\b", re.IGNORECASE)

    async def _capture_lessons(self, message: str) -> None:
        """Detect an explicit correction/preference and store it as a durable
        lesson so it shapes future replies. Conservative by design."""
        msg = (message or "").strip()
        if len(msg) < 6 or len(msg) > 400:
            return
        if self._LESSON_SKIP.search(msg):
            return
        if not any(p.search(msg) for p in self._LESSON_PATTERNS):
            return
        try:
            await self._memory.add_lesson(msg, topic="preference")
            BUS.publish("memory.lesson_learned", {"lesson": msg[:160]})
        except Exception:
            pass

    async def _capture_mood(self, message: str) -> None:
        """Detect a coarse mood signal (heuristic, not an LLM call — see
        core/mood.py) and record it. One reading per day; only writes when a
        clear signal is present, never guesses on ordinary messages."""
        label = detect_mood_signal(message)
        if label is None:
            return
        try:
            await self._memory.record_mood(label)
            BUS.publish("mood.detected", {"mood": label})
        except Exception:
            pass

    async def _capture_wellbeing(self) -> None:
        """Coarse usage-time signal (WB1) — no LLM call, just a clock check.
        Only records when the signal is actually meaningful (talking late at
        night), same discipline as mood: never guesses on an ordinary day."""
        hour = datetime.now().astimezone().hour
        if 0 <= hour < 5:
            try:
                await self._memory.record_wellbeing_signal("late_night")
            except Exception:
                pass
