from __future__ import annotations

import contextlib
import json
import os
import re
import asyncio
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from core.code_ops import CodeOps
from core.llm_runtime import LLMRuntime
from core.logging_setup import get_logger
from core.planner import Planner
from core.project_manager import ProjectManager
from core.tool_router import ToolCall, ToolRouter
from memory.unifier import MemoryUnifier
from plugins.registry import REGISTRY

logger = get_logger(__name__)


_UI_ARTIFACT_MARKERS = (
    "last edited by",
    "copy",
    "output:",
)


def _is_ui_artifact(text: str) -> bool:
    if not text:
        return False
    low = text.lower()
    if "last edited by" in low and "nova" in low:
        return True
    if "output:" in low:
        return True
    if re.search(r"(?im)^\s*copy\s*$", text):
        return True
    if re.search(r"(?im)^\s*output:?\s*$", text):
        return True
    if re.search(r"(?im)^\s*(assistant|user):\s*", text):
        return True
    if "```" in text and any(m in low for m in _UI_ARTIFACT_MARKERS):
        return True
    return False


def _sanitize_user_text(text: str) -> str:
    """Remove known e2e/debug artifacts and accidental transcript pastes."""
    if not text:
        return ""
    t = text.strip()

    # Strip common test tags
    t = re.sub(r"#e2e\b", "", t, flags=re.IGNORECASE)
    t = re.sub(r"#hello\b", "", t, flags=re.IGNORECASE)

    # Remove UI/editor artifacts
    t = re.sub(r"\blast edited by\s+Nova\b", "", t, flags=re.IGNORECASE)

    # If the user accidentally pasted a transcript, keep only the last user question.
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    if any(ln.lower().startswith("assistant:") or ln.lower().startswith("user:") for ln in lines):
        last_user = ""
        for ln in lines:
            if ln.lower().startswith("user:"):
                last_user = ln.split(":", 1)[1].strip()
        if last_user:
            t = last_user

    # Drop repeated boilerplate directives often injected by tests
    banned = [
        "ignore the repetitive messages",
        "what's the real reason for your message",
        "last 4 messages were",
        "last 5 messages were",
        "hello from e2e",
        "current time:",
    ]
    low = t.lower()
    for b in banned:
        if b in low:
            t = re.sub(re.escape(b), "", t, flags=re.IGNORECASE).strip()
            low = t.lower()

    # Collapse whitespace
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _sanitize_any_text(text: str) -> str:
    """Sanitize assistant/memory text to avoid saving or injecting UI dumps."""
    if not text:
        return ""
    t = text.strip()
    t = re.sub(r"\blast edited by\s+Nova\b", "", t, flags=re.IGNORECASE)
    t = re.sub(r"(?im)^\s*(copy|output:?)\s*$", "", t)

    # Remove hashtag-style metadata/tags (e.g. "#assistant #Nova").
    # This prevents the model from persisting or re-injecting label spam.
    t = re.sub(r"(?:^|\s)#[A-Za-z0-9_]+", "", t)

    # If the content looks like a transcript paste, keep only the last assistant line.
    lines = [ln.rstrip() for ln in t.splitlines() if ln.strip()]
    if any(ln.strip().lower().startswith(("assistant:", "user:")) for ln in lines):
        last_assistant = None
        for ln in lines:
            if ln.strip().lower().startswith("assistant:"):
                last_assistant = ln.split(":", 1)[1].strip()
        if last_assistant:
            t = last_assistant
        else:
            t = lines[-1].strip()
        t = re.sub(r"(?im)^\s*(assistant|user):\s*", "", t).strip()

    # Collapse whitespace if it's not code
    if "\n" not in t and "```" not in t:
        t = re.sub(r"\s+", " ", t).strip()

    return t


def _user_wants_code(user_msg: str) -> bool:
    q = (user_msg or "").lower()
    triggers = (
        "code",
        "python",
        "powershell",
        "bash",
        "script",
        "snippet",
        "regex",
        "json",
        "yaml",
        "```",
    )
    return any(t in q for t in triggers)


def _postprocess_assistant(assistant: str, user_msg: str) -> str:
    """Final output guardrails: strip unintended code fences, prevent rambling loops."""
    a = (assistant or "").strip()
    if not a:
        return ""

    wants_code = _user_wants_code(user_msg)

    # If the user did not ask for code, drop fenced code blocks entirely.
    if not wants_code and "```" in a:
        a = re.sub(r"```[\s\S]*?```", "", a).strip()

    # If the model leaked internal orchestration text (e.g., tool dumps), strip it.
    if not wants_code:
        low = a.lower()
        leak_markers = (
            "tool results:",
            "tool result:",
            "respond to the user in natural language",
            "<tool_results>",
        )
        cut_at: int | None = None
        for lm in leak_markers:
            j = low.find(lm)
            if j != -1:
                cut_at = j if cut_at is None else min(cut_at, j)
        if cut_at is not None:
            a = a[:cut_at].strip()

    # If the model started echoing a multi-turn transcript, keep only the first response chunk.
    if not wants_code:
        # Cut off if it begins repeating prompts/questions.
        cut_markers = ["\nWhat is ", "\nWhat are ", "\nWho are ", "\nHow can "]
        for m in cut_markers:
            idx = a.find(m)
            if idx != -1 and idx > 0:
                a = a[:idx].strip()
                break

    # Enforce concise answers for short questions.
    if not wants_code and len((user_msg or "").strip()) <= 60:
        # Keep at most 2 sentences.
        parts = re.split(r"(?<=[.!?])\s+", a)
        a = " ".join([p for p in parts if p.strip()][:2]).strip()

    # Final whitespace cleanup
    a = re.sub(r"\s+\n", "\n", a)
    a = re.sub(r"\n{3,}", "\n\n", a)
    return a.strip()


def _wants_turn_recall(user_msg: str) -> bool:
    q = (user_msg or "").lower()
    triggers = (
        "what did i say",
        "what did we say",
        "what have we talked about",
        "what did we talk about",
        "what did we discuss",
        "what have we discussed",
        "what questions have i asked",
        "what did i ask",
        "summarize this conversation",
        "summarize our conversation",
        "earlier",
        "previous",
        "last time",
        "recap",
        "remind me",
        "as we discussed",
        "continue",
    )
    return any(t in q for t in triggers)


def _is_smalltalk_or_greeting(user_msg: str) -> bool:
    """Heuristic: detect greetings/chitchat so we don't spin up tool-planning for casual turns."""
    q = (user_msg or "").strip().lower()
    if not q:
        return True

    # Strip assistant name / punctuation
    q2 = re.sub(r"[^a-z0-9\s]", " ", q)
    q2 = re.sub(r"\s+", " ", q2).strip()

    # Very short messages are usually chit-chat (unless they are explicit commands).
    short = len(q2) <= 20

    greetings = {
        "hi", "hey", "hello", "yo", "sup", "what's up", "whats up", "good morning", "good afternoon", "good evening",
        "hey nova", "hi nova", "hello nova", "yo nova", "sup nova",
    }
    if q2 in greetings:
        return True

    # Common chitchat openers
    if short and any(q2.startswith(g) for g in ("hi ", "hey ", "hello ", "yo ", "sup ")):
        return True

    # If it contains a question mark or obvious task verbs, it's not smalltalk.
    if "?" in q:
        return False

    tasky = (
        "find ", "search ", "look up", "google ", "open ", "create ", "make ", "build ", "write ", "fix ", "debug ",
        "plan ", "schedule ", "remind ", "email ", "text ", "call ", "run ", "execute ", "scaffold ", "generate ",
        "summarize ", "analyze ", "compare ",
    )
    if any(t in q2 for t in tasky):
        return False

    return short


def _should_use_autonomy(user_msg: str) -> bool:
    """Gate the plan/act loop: only use autonomy when the user is asking for work."""
    q = (user_msg or "").strip()
    if not q:
        return False
    if _is_smalltalk_or_greeting(q):
        return False
    # For longer freeform questions, autonomy may help, but don't force it.
    return True



def _is_memory_confirm(msg: str) -> bool:
    q = (msg or "").strip().lower()
    # Short, explicit approvals.
    return q in {"yes", "y", "sure", "ok", "okay", "remember", "save", "save it", "remember that"} or any(
        p in q for p in ("yes, remember", "please remember", "go ahead and save", "store that", "remember this")
    )


def _is_memory_deny(msg: str) -> bool:
    q = (msg or "").strip().lower()
    return q in {"no", "n", "nope", "don't", "do not", "dont", "don't save", "do not save"} or any(
        p in q for p in ("don't remember", "do not remember", "please don't save", "no thanks")
    )


@dataclass
class _PendingFact:
    entity: str
    attribute: str
    value: str
    confidence: float
    # Human-friendly blurb used when asking the user to confirm.
    blurb: str


@dataclass
class ChatResponse:
    conversation_id: UUID
    assistant_text: str
    tool_calls: list[dict[str, Any]]


class Brain:

    # ----------------------------
    # Relationship extraction helpers
    # ----------------------------
    @staticmethod
    def _split_name_list(text: str) -> list[str]:
        """Split 'Jake, Sarah and Noel' into ['Jake','Sarah','Noel'] safely."""
        if not text:
            return []
        t = text.strip()
        # normalize separators
        t = re.sub(r"\s*(?:,|&|\band\b)\s*", "|", t, flags=re.IGNORECASE)
        parts = [p.strip(" .;:!\n\t") for p in t.split("|")]
        out: list[str] = []
        for p in parts:
            if not p:
                continue
            # Keep only plausible name tokens (allow spaces, hyphen, apostrophe)
            p = re.sub(r"[^A-Za-z\-'\s]", "", p).strip()
            if len(p) < 2:
                continue
            # Title-case for consistency (but preserve multi-part)
            out.append(" ".join([w.capitalize() for w in p.split() if w]))
        # de-dupe preserving order
        seen = set()
        dedup: list[str] = []
        for n in out:
            key = n.lower()
            if key in seen:
                continue
            seen.add(key)
            dedup.append(n)
        return dedup

    @staticmethod
    def _pet_value(name: str, species: str) -> str:
        s = (species or "").strip().lower()
        n = (name or "").strip()
        return f"{n}|{s}" if s else n

    def __init__(
        self,
        repo_root: Path,
        projects_dir: Path,
        memory: MemoryUnifier,
        llm: LLMRuntime,
    ) -> None:
        self._repo_root = repo_root
        self._memory = memory
        self._llm = llm
        self._planner = Planner()
        self._project_manager = ProjectManager(repo_root=repo_root, projects_dir=projects_dir)
        self._projects_dir = projects_dir
        self._code_ops = CodeOps(repo_root, extra_allowed_roots=[projects_dir])

        # Autonomy configuration
        self._autonomy_enabled = (os.getenv("NOVA_AUTONOMY", "1").strip().lower() not in {"0", "false", "no"})
        self._autonomy_max_steps = int(os.getenv("NOVA_AUTONOMY_MAX_STEPS", "12").strip() or "12")
        self._allow_shell = (os.getenv("NOVA_ALLOW_SHELL", "1").strip().lower() not in {"0", "false", "no"})
        self._allow_network_tools = (os.getenv("NOVA_ALLOW_NETWORK_TOOLS", "1").strip().lower() not in {"0", "false", "no"})
        self._memory_save_mode = (os.getenv("NOVA_MEMORY_SAVE_MODE", "all").strip().lower() or "all")

        # Per-conversation staged memory writes that require explicit user approval.
        # Keyed by conversation_id.
        self._pending_memory: dict[UUID, list[_PendingFact]] = {}

        # load plugins
        import plugins.init  # noqa: F401

        plugin_tools = {name: spec.fn for name, spec in REGISTRY.get_tools().items()}

        # Built-in tools (safe code ops + project ops)
        async def _scaffold_project(args: dict) -> dict:
            name = str(args.get("name") or "").strip()
            path = self._project_manager.scaffold_project(name)
            return {"project": name, "path": str(path)}

        async def _code_read(args: dict) -> dict:
            path = Path(str(args.get("path") or "")).expanduser()
            content = await self._code_ops.read_text(path)
            return {"path": str(path), "content": content}

        async def _code_write(args: dict) -> dict:
            path = Path(str(args.get("path") or "")).expanduser()
            content = str(args.get("content") or "")
            await self._code_ops.apply_patch_atomic(path, content)
            return {"path": str(path), "bytes": len(content.encode("utf-8"))}

        async def _memory_rebuild(args: dict) -> dict:  # noqa: ARG001
            res = await self._memory.rebuild_semantic_index()
            return {"rebuilt": res}

        def _looks_dangerous_cmd(cmd: str) -> bool:
            c = (cmd or "").strip().lower()
            # Guardrail denylist for obviously destructive commands.
            bad = [
                "rm -rf /",
                "rm -rf /*",
                "del /s",
                "rmdir /s",
                "format ",
                "shutdown",
                "reboot",
                "reg delete",
                "diskpart",
            ]
            return any(b in c for b in bad)

        async def _shell_exec(args: dict) -> dict:
            if not self._allow_shell:
                return {"ok": False, "error": "shell_disabled"}
            cmd = str(args.get("cmd") or args.get("command") or "").strip()
            timeout_s = float(args.get("timeout_s") or 45.0)
            if not cmd:
                return {"ok": False, "error": "missing_cmd"}
            if _looks_dangerous_cmd(cmd):
                return {"ok": False, "error": "refusing_dangerous_command", "cmd": cmd}

            proc = await asyncio.create_subprocess_shell(
                cmd,
                cwd=str(self._repo_root),
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
            # keep payload bounded
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
        }
        if self._allow_shell:
            tools["shell.exec"] = _shell_exec
        if self._allow_network_tools:
            tools.update(plugin_tools)

        self._router = ToolRouter({**tools})

        # Cached tool descriptions for the autonomous planner
        self._tool_descriptions: dict[str, str] = {
            "project.scaffold": "Create a new project under projects/. Args: {name}",
            "code.read": "Read a text file. Args: {path}",
            "code.write": "Write/replace a text file atomically. Args: {path, content}",
            "memory.rebuild_index": "Rebuild Chroma semantic index from SQLite durable records. Args: {}",
        }
        if self._allow_shell:
            self._tool_descriptions["shell.exec"] = "Run a shell command (guarded). Args: {cmd, timeout_s?}"
        for name, spec in REGISTRY.get_tools().items():
            self._tool_descriptions.setdefault(name, spec.description)

    async def _autonomous_tool_loop(
        self,
        *,
        user_message: str,
        conversation_id: UUID,
        memory_context: str,
        tool_calls: list[dict[str, Any]],
    ) -> tuple[str, list[dict[str, Any]]]:
        """LLM-driven plan/act loop that selects tools and executes them."""
        if not self._autonomy_enabled or not self._llm.model_loaded:
            return "", tool_calls

        tools_list = "\n".join([f"- {n}: {self._tool_descriptions.get(n, '')}" for n in self._router.list_tools()])

        history: list[str] = []
        for i in range(max(1, int(self._autonomy_max_steps))):
            prev = "\n".join(history[-6:])
            prompt = f"""You are Nova running in AUTONOMOUS MODE.

Rules:
- If the user is greeting, smalltalk, or casual conversation, return type=final immediately and DO NOT call tools.
- Prefer type=final. Only call tools when they are clearly necessary to answer or to perform an explicit request.

Goal: satisfy the user's request by using tools when helpful.

Available tools:
{tools_list}

Memory context (may be empty):
{memory_context}

Prior actions/results:
{prev}

User request:
{user_message}

Return ONLY valid JSON, one of:
1) {{"type":"tool","name":"<tool>","args":{{...}}}}
2) {{"type":"final","assistant":"<final response to user>"}}
"""

            raw = await self._llm.generate(prompt, max_tokens=220, temperature=0.0, stop=["\n\n", "\n#", "```"])
            raw = (raw or "").strip()

            # Extract first JSON object defensively
            m = re.search(r"\{[\s\S]*\}", raw)
            if not m:
                history.append(f"step {i+1}: planner_output_unparseable: {raw[:200]}")
                continue
            try:
                obj = json.loads(m.group(0))
            except Exception as e:  # noqa: BLE001
                history.append(f"step {i+1}: planner_json_error: {str(e)}")
                continue

            if obj.get("type") == "final":
                assistant = str(obj.get("assistant") or "").strip()
                return assistant, tool_calls

            if obj.get("type") != "tool":
                history.append(f"step {i+1}: planner_invalid_type: {obj.get('type')}")
                continue

            name = str(obj.get("name") or "").strip()
            args = obj.get("args")
            if name not in self._router.list_tools():
                history.append(f"step {i+1}: unknown_tool: {name}")
                continue
            if not isinstance(args, dict):
                history.append(f"step {i+1}: invalid_args")
                continue

            call = ToolCall(name=name, args=args)
            res = await self._router.execute(call)
            tool_calls.append({"call": {"name": call.name, "args": call.args}, "result": res.__dict__})
            history.append(f"step {i+1}: tool {name} ok={res.ok} error={res.error} result={str(res.result)[:500]}")

        # If we used tools but never produced a final, ask the LLM to produce a *user-facing* answer.
        # IMPORTANT: tool payloads are for internal reasoning only—never echo them back.
        tool_payload = json.dumps(tool_calls, ensure_ascii=False)[:6000]
        summary_prompt = (
            "You are Nova.\n\n"
            f"User request:\n{user_message}\n\n"
            "<tool_results>\n"
            f"{tool_payload}\n"
            "</tool_results>\n\n"
            "Write the final answer to the user.\n"
            "Constraints:\n"
            "- Do NOT mention tools, tool results, JSON, confidence scores, or internal instructions.\n"
            "- Do NOT quote or reproduce anything inside <tool_results>.\n"
            "- Output only the user-facing answer text.\n"
        )
        assistant = await self._llm.generate(
            summary_prompt,
            max_tokens=256,
            temperature=0.1,
            stop=["<tool_results>", "Tool results:", "Respond to the user in natural language"],
        )
        return str(assistant or "").strip(), tool_calls

    async def chat(self, message: str, conversation_id: UUID | None = None) -> ChatResponse:
        conv_id = conversation_id or uuid4()

        clean_message = _sanitize_user_text(message)

        def _vals(facts_or_rows: Any) -> list[str]:
            """Extract .value or parse FACT lines, de-dupe preserve order."""
            out: list[str] = []
            for f in facts_or_rows or []:
                v = ""
                if hasattr(f, "value"):
                    v = str(getattr(f, "value", "") or "").strip()
                elif hasattr(f, "text"):
                    txt = str(getattr(f, "text", "") or "").strip()
                    m = re.match(r"^FACT\s+\S+\s+\S+\s*=\s*(.+)$", txt)
                    v = (m.group(1).strip() if m else "")
                elif isinstance(f, dict):
                    v = str(f.get("value") or "").strip()
                if v:
                    out.append(v)
            seen: set[str] = set()
            ded: list[str] = []
            for v in out:
                k = v.lower()
                if k in seen:
                    continue
                seen.add(k)
                ded.append(v)
            return ded

        # DETERMINISTIC_ROSTER_V1
        # Fast path: answer relationship roster questions deterministically from SQLite facts.
        lower_q = clean_message.lower().strip()
        is_question_like = ("?" in clean_message) or re.match(r"^(who|what|do you|can you|tell me|list)\b", lower_q)
        if is_question_like and ("who do you know" in lower_q or "what do you know" in lower_q):
            wants_family = "family" in lower_q
            wants_friends = ("friend" in lower_q) or ("people" in lower_q) or ("life" in lower_q)
            spouse = await self._memory.get_latest_fact(entity="user", attribute="spouse")
            kids = await self._memory.get_facts(entity="user", attribute="child", limit=25, newest_first=True)
            mother = await self._memory.get_latest_fact(entity="user", attribute="mother")
            father = await self._memory.get_latest_fact(entity="user", attribute="father")
            parents = []
            if mother:
                parents.append(mother.value)
            if father:
                parents.append(father.value)
            siblings = await self._memory.get_facts(entity="user", attribute="sibling", limit=25, newest_first=True)
            cousins = await self._memory.get_facts(entity="user", attribute="cousin", limit=50, newest_first=True)
            pets = await self._memory.get_facts(entity="user", attribute="pet", limit=25, newest_first=True)
            friends = await self._memory.get_facts(entity="user", attribute="friend", limit=50, newest_first=True)

            parts=[]
            if spouse and spouse.value:
                parts.append(f"spouse: {spouse.value}")
            kid_vals = _vals(kids)
            if kid_vals:
                parts.append("children: " + ", ".join(kid_vals))
            if parents:
                parts.append("parents: " + ", ".join(parents))
            sib_vals = _vals(siblings)
            if sib_vals:
                parts.append("siblings: " + ", ".join(sib_vals))
            cous_vals = _vals(cousins)
            if cous_vals:
                parts.append("cousins: " + ", ".join(cous_vals))
            pet_vals = _vals(pets)
            if pet_vals:
                # show pets without species pipe if present
                pretty=[]
                for p in pet_vals:
                    pretty.append(p.split("|",1)[0] if "|" in p else p)
                parts.append("pets: " + ", ".join(pretty))
            if (not wants_family) and wants_friends:
                fr_vals = _vals(friends)
                if fr_vals:
                    parts.append("friends: " + ", ".join(fr_vals))

            if not parts:
                return ChatResponse(conversation_id=conv_id, assistant_text="I don’t have any family information saved yet.", tool_calls=[])
            if wants_family and (not wants_friends):
                return ChatResponse(conversation_id=conv_id, assistant_text="I know your " + "; ".join(parts) + ".", tool_calls=[])
            # broader
            fr_vals = _vals(friends)
            if wants_friends and fr_vals and ("friends:" not in " ".join(parts)):
                parts.append("friends: " + ", ".join(fr_vals))
            return ChatResponse(conversation_id=conv_id, assistant_text="Here’s who I know: " + "; ".join(parts) + ".", tool_calls=[])

        # DETERMINISTIC_CHILDREN_Q_V1
        if is_question_like and any(t in lower_q for t in ("son", "sons", "daughter", "daughters", "kid", "kids", "child", "children")):
            if ("name" in lower_q) or ("names" in lower_q):
                kid_vals = _vals(await self._memory.get_facts(entity="user", attribute="child", limit=25, newest_first=True))
                if not kid_vals:
                    return ChatResponse(conversation_id=conv_id, assistant_text="I don’t have your children’s names saved yet.", tool_calls=[])
                if len(kid_vals) == 1:
                    return ChatResponse(conversation_id=conv_id, assistant_text=f"Your child’s name is {kid_vals[0]}.", tool_calls=[])
                return ChatResponse(conversation_id=conv_id, assistant_text="Your children are: " + ", ".join(kid_vals) + ".", tool_calls=[])

        # DETERMINISTIC_PARENT_Q_V1
        if is_question_like and any(t in lower_q for t in ("mom", "mother", "dad", "father", "parents")) and ("name" in lower_q or "names" in lower_q):
            mother = await self._memory.get_latest_fact(entity="user", attribute="mother")
            father = await self._memory.get_latest_fact(entity="user", attribute="father")
            if ("mom" in lower_q or "mother" in lower_q) and mother and getattr(mother, "value", ""):
                return ChatResponse(conversation_id=conv_id, assistant_text=f"Your mom’s name is {mother.value}.", tool_calls=[])
            if ("dad" in lower_q or "father" in lower_q) and father and getattr(father, "value", ""):
                return ChatResponse(conversation_id=conv_id, assistant_text=f"Your dad’s name is {father.value}.", tool_calls=[])
            parents = [p for p in [getattr(mother, "value", ""), getattr(father, "value", "")] if p]
            if parents:
                return ChatResponse(conversation_id=conv_id, assistant_text="Your parents are: " + ", ".join(parents) + ".", tool_calls=[])
            return ChatResponse(conversation_id=conv_id, assistant_text="I don’t have your parents’ names saved yet.", tool_calls=[])

        # DETERMINISTIC_PET_Q_V1
        if is_question_like and any(t in lower_q for t in ("pet", "pets", "dog", "dogs", "cat", "cats")) and ("name" in lower_q or "names" in lower_q):
            pet_vals = _vals(await self._memory.get_facts(entity="user", attribute="pet", limit=25, newest_first=True))
            if not pet_vals:
                return ChatResponse(conversation_id=conv_id, assistant_text="I don’t have any pet names saved yet.", tool_calls=[])
            pretty = [(p.split("|", 1)[0] if "|" in p else p) for p in pet_vals]
            if len(pretty) == 1:
                return ChatResponse(conversation_id=conv_id, assistant_text=f"Your pet’s name is {pretty[0]}.", tool_calls=[])
            return ChatResponse(conversation_id=conv_id, assistant_text="Your pets are: " + ", ".join(pretty) + ".", tool_calls=[])

        # DETERMINISTIC_SPOUSE_Q_V1
        if is_question_like and any(t in lower_q for t in ("wife", "husband", "spouse")) and ("name" in lower_q or "names" in lower_q):
            spouse = await self._memory.get_latest_fact(entity="user", attribute="spouse")
            if spouse and getattr(spouse, "value", ""):
                return ChatResponse(conversation_id=conv_id, assistant_text=f"Your spouse’s name is {spouse.value}.", tool_calls=[])
            return ChatResponse(conversation_id=conv_id, assistant_text="I don’t have your spouse’s name saved yet.", tool_calls=[])

        # DETERMINISTIC_FRIEND_Q_V1
        if is_question_like and any(t in lower_q for t in ("friend", "friends")) and ("name" in lower_q or "names" in lower_q):
            fr_vals = _vals(await self._memory.get_facts(entity="user", attribute="friend", limit=50, newest_first=True))
            if not fr_vals:
                return ChatResponse(conversation_id=conv_id, assistant_text="I don’t have any friends’ names saved yet.", tool_calls=[])
            if len(fr_vals) == 1:
                return ChatResponse(conversation_id=conv_id, assistant_text=f"One friend I know is {fr_vals[0]}.", tool_calls=[])
            return ChatResponse(conversation_id=conv_id, assistant_text="Your friends are: " + ", ".join(fr_vals) + ".", tool_calls=[])
        # DETERMINISTIC_SIBLING_Q_V1
        # Answer direct sibling-name questions from deterministic facts (avoids reliance on semantic search).
        if is_question_like and ("brother" in lower_q or "sister" in lower_q or "sibling" in lower_q):
            if ("name" in lower_q) or ("names" in lower_q):
                sib_vals = _vals(await self._memory.get_facts(entity="user", attribute="sibling", limit=25, newest_first=True))
                if not sib_vals:
                    return ChatResponse(conversation_id=conv_id, assistant_text="I don’t have your sibling’s name saved yet.", tool_calls=[])
                if len(sib_vals) == 1:
                    if "brother" in lower_q:
                        return ChatResponse(conversation_id=conv_id, assistant_text=f"Your brother’s name is {sib_vals[0]}.", tool_calls=[])
                    if "sister" in lower_q:
                        return ChatResponse(conversation_id=conv_id, assistant_text=f"Your sister’s name is {sib_vals[0]}.", tool_calls=[])
                    return ChatResponse(conversation_id=conv_id, assistant_text=f"Your sibling’s name is {sib_vals[0]}.", tool_calls=[])
                return ChatResponse(conversation_id=conv_id, assistant_text="Your siblings are: " + ", ".join(sib_vals) + ".", tool_calls=[])


        if not clean_message:
            clean_message = (message or "").strip()

        await self._memory.ingest_turn(conv_id, role="user", content=clean_message)

        # Handle explicit memory approval/denial.
        pending = self._pending_memory.get(conv_id) or []
        if pending and _is_memory_confirm(clean_message):
            for pf in pending:
                await self._memory.add_fact(entity=pf.entity, attribute=pf.attribute, value=pf.value, confidence=pf.confidence)
            self._pending_memory.pop(conv_id, None)
            # If this message is purely an approval, acknowledge immediately to avoid LLM confusion.
            if len(clean_message) <= 24 and "?" not in clean_message:
                assistant = "Got it. I'll remember that for future chats."
                await self._memory.ingest_turn(conv_id, role="assistant", content=assistant)
                return ChatResponse(conversation_id=conv_id, assistant_text=assistant, tool_calls=[])
        elif pending and _is_memory_deny(clean_message):
            self._pending_memory.pop(conv_id, None)
            if len(clean_message) <= 24 and "?" not in clean_message:
                assistant = "Understood. I won't save that."
                await self._memory.ingest_turn(conv_id, role="assistant", content=assistant)
                return ChatResponse(conversation_id=conv_id, assistant_text=assistant, tool_calls=[])

        # Update memory with quick facts
        ask_to_confirm = await self._extract_facts(clean_message, conversation_id=conv_id)

        tool_calls: list[dict[str, Any]] = []

        # Always respect explicit /tool commands.
        maybe_tool = await self._maybe_route_tool(clean_message)
        if maybe_tool is not None:
            res = await self._router.execute(maybe_tool)
            tool_calls.append({"call": {"name": maybe_tool.name, "args": maybe_tool.args}, "result": res.__dict__})
        else:
            # Legacy deterministic planner (used when no LLM or autonomy disabled).
            if (not self._autonomy_enabled) or (not self._llm.model_loaded):
                plan = self._planner.plan(clean_message)
                for step in plan:
                    if step.action == "scaffold_project":
                        call = ToolCall(name="project.scaffold", args=step.args)
                        res = await self._router.execute(call)
                        tool_calls.append({"call": {"name": call.name, "args": call.args}, "result": res.__dict__})

        # Memory retrieval
        try:
            family_terms = ("family", "wife", "spouse", "husband", "son", "sons", "daughter", "children", "kids")
            if any(t in clean_message.lower() for t in family_terms):
                mem_hits = await self._memory.search(q="user", conversation_id=conv_id, limit=10)
            else:
                mem_hits = await self._memory.search(q=clean_message, conversation_id=conv_id, limit=10)
        except Exception as e:
            logger.exception("memory_search_failed", error=str(e))
            mem_hits = []

        if not self._llm.model_loaded:
            assistant = (
                "Model not loaded (no .gguf found under model/). "
                "I can still store memory and run tools. "
                "Place a GGUF under model/ to enable GPU-only inference."
            )
        else:
            include_turns = _wants_turn_recall(clean_message)
            context_lines: list[str] = []
            for h in mem_hits:
                if h.kind == "turn" and not include_turns:
                    continue
                if _is_ui_artifact(h.text):
                    continue
                safe = _sanitize_any_text(h.text)
                if not safe:
                    continue
                m_fact = re.match(r"^FACT\s+user\s+([a-zA-Z0-9_]+)\s*=\s*(.+)$", safe)
                if m_fact:
                    attr = m_fact.group(1).strip().lower()
                    val = m_fact.group(2).strip()
                    if attr == "name":
                        context_lines.append(f"User's name is {val}.")
                    elif attr == "spouse":
                        context_lines.append(f"User's spouse is {val}.")
                    elif attr == "child":
                        context_lines.append(f"User has a child named {val}.")
                    else:
                        context_lines.append(safe)
                else:
                    context_lines.append(safe)

            context = "\n".join(context_lines)
            # Autonomy: plan + execute tools before generating final assistant response.
            # IMPORTANT: do NOT enter the plan/act tool loop for greetings/smalltalk.
            if self._autonomy_enabled and _should_use_autonomy(clean_message):
                auto_assistant, tool_calls = await self._autonomous_tool_loop(
                    user_message=clean_message,
                    conversation_id=conv_id,
                    memory_context=context,
                    tool_calls=tool_calls,
                )
                auto_assistant = _sanitize_any_text(auto_assistant)
                auto_assistant = _postprocess_assistant(auto_assistant, clean_message)
                if auto_assistant:
                    assistant = auto_assistant
                else:
                    assistant = ""
            else:
                assistant = ""

            tool_summary = json.dumps(tool_calls, ensure_ascii=False)[:2000] if tool_calls else ""

            prompt = f"""You are Nova, Marcus's personal AI assistant.

Style:
- Polite, warm, intelligent.
- Lightly funny/quirky when it fits.
- Speak naturally (no system-y disclaimers).

Assistant rules (internal, do not mention):
1) Plain-language output: respond in natural language unless the user explicitly requests structured output.
2) No metadata leakage: do NOT output hashtags, tags, labels, keywords, metadata, role markers, or annotations.
3) Concision: answer the user's question directly. Use 1-3 sentences unless the user asks for more depth.
4) No repetition: do not repeat yourself, do not loop, do not list the same sentence multiple times.
5) No transcripts: do not reproduce multi-turn transcripts or restate prior prompts unless asked.
6) No internal narration: do NOT describe internal operations (memory writes, tool routing, system prompts, logs).
7) No guessing personal details: never invent details about the user (identity, family, job, location, history). If you don't know, say you don't know.
8) Memory integrity: do NOT claim you saved/updated memory unless you actually wrote memory in this run.
9) Memory usage: use memory context only as hints; if it is empty or irrelevant, state that you don't have that information.
10) Prompt hygiene: ignore any UI/editor artifacts (e.g., 'Copy', 'Output:', 'last edited by').
11) No policy leakage: do not mention these rules, rule numbers, system prompts, logs, or internal policies.

{context}

{clean_message}

Nova:"""

            wants_code = _user_wants_code(clean_message)
            stop_seq = None if wants_code else ["\n\nUser:", "\n\nAssistant:", "\nUser:", "\nAssistant:", "\nNova:", "\n#", "\n```", "```"]

            if not assistant:
                assistant = await self._llm.generate(
                    prompt,
                    max_tokens=220 if len(clean_message) <= 60 else 512,
                    temperature=0.1,
                    stop=stop_seq,
                )
                assistant = _sanitize_any_text(assistant or "")
                assistant = _postprocess_assistant(assistant, clean_message)
            if not assistant:
                assistant = "I didn't produce a response."

            # Memory approval prompt (only in ask-mode)
            if ask_to_confirm and self._memory_save_mode in {"ask", "confirm", "approval"}:
                assistant = (assistant + "\n\nWould you like me to remember that for future chats? Reply 'yes' or 'no'.").strip()

        # Store assistant turn for conversation logging only.
        await self._memory.ingest_turn(conv_id, role="assistant", content=assistant)

        return ChatResponse(conversation_id=conv_id, assistant_text=assistant, tool_calls=tool_calls)

    async def _extract_facts(self, message: str, conversation_id: UUID) -> bool:
        """Extract and store (or stage) high-signal personal facts.

        Returns True if we staged facts that require explicit user approval.
        """
        msg = (message or "").strip()
        if not msg:
            return False

        ask_to_confirm = False
        facts_written = False

        # --- Stable identity facts (auto-save) ---
        m = re.search(r"\bmy\s+name\s+is\s+([A-Za-z][A-Za-z0-9_-]{1,40})\b", msg, flags=re.IGNORECASE)
        if m:
            name = m.group(1)
            await self._memory.upsert_person(name=name, attributes={"relation": "user"})
            await self._memory.add_fact(entity="user", attribute="name", value=name, confidence=0.9)
            facts_written = True

        m2 = re.search(r"\bi\s+live\s+in\s+([A-Za-z][A-Za-z0-9 _-]{2,60})\b", msg, flags=re.IGNORECASE)
        if m2:
            loc = m2.group(1).strip()
            await self._memory.add_fact(entity="user", attribute="location", value=loc, confidence=0.75)
            facts_written = True

        # --- Family facts (require explicit approval) ---
        pending: list[_PendingFact] = []

        # Spouse
        m_spouse = re.search(
            r"\bmy\s+(wife|husband|spouse)\b(?:\s+name\s+is|\s+is|\s+named)?\s+([A-Za-z][A-Za-z0-9'_-]{1,40})\b",
            msg,
            flags=re.IGNORECASE,
        )
        if m_spouse:
            who = m_spouse.group(1).lower()
            spouse_name = m_spouse.group(2)
            pending.append(
                _PendingFact(
                    entity="user",
                    attribute="spouse",
                    value=spouse_name,
                    confidence=0.85,
                    blurb=f"Your {who}'s name is {spouse_name}.",
                )
            )

        # Children (simple named list extraction)
        m_kids = re.search(
            r"\b(?:i\s+have|my)\s+(?:two\s+|three\s+|four\s+|\d+\s+)?(sons|son|daughters|daughter|kids|children)\b(?:\s+(?:named|are|:))?\s+(.+)$",
            msg,
            flags=re.IGNORECASE,
        )
        extracted_child_names = False
        if m_kids:
            rel = m_kids.group(1).lower()
            tail = m_kids.group(2).strip()
            tail = re.sub(r"[.?!]+$", "", tail).strip()
            parts = re.split(r"\s*(?:,|\band\b|&|\+)\s*", tail)
            names = [p.strip() for p in parts if p.strip()]
            names = [n for n in names if re.fullmatch(r"[A-Za-z][A-Za-z0-9'_-]{1,40}", n)]
            if names:
                for n in names[:6]:
                    pending.append(
                        _PendingFact(
                            entity="user",
                            attribute="child",
                            value=n,
                            confidence=0.80,
                            blurb=f"You have a child named {n}.",
                        )
                    )
                extracted_child_names = True
                if rel in {"sons", "son"}:
                    pending.append(
                        _PendingFact(
                            entity="user",
                            attribute="children_type",
                            value="sons",
                            confidence=0.70,
                            blurb="You mentioned your children are sons.",
                        )
                    )
                elif rel in {"daughters", "daughter"}:
                    pending.append(
                        _PendingFact(
                            entity="user",
                            attribute="children_type",
                            value="daughters",
                            confidence=0.70,
                            blurb="You mentioned your children are daughters.",
                        )
                    )

        # Support: "I have two sons. Their names are Mateo and Liam."
        # If we see a child relationship mention earlier, allow a second sentence to provide names.
        if not extracted_child_names:
            rel_m = re.search(r"\b(sons|son|daughters|daughter|kids|children)\b", msg, flags=re.IGNORECASE)
            names_m = re.search(r"\btheir\s+names?\s+(?:are|is)\s+(.+)$", msg, flags=re.IGNORECASE)
            if rel_m and names_m:
                rel = rel_m.group(1).lower()
                tail = names_m.group(1).strip()
                tail = re.sub(r"[.?!]+$", "", tail).strip()
                parts = re.split(r"\s*(?:,|\band\b|&|\+)\s*", tail)
                names = [p.strip() for p in parts if p.strip()]
                names = [n for n in names if re.fullmatch(r"[A-Za-z][A-Za-z0-9'_-]{1,40}", n)]
                if names:
                    for n in names[:6]:
                        pending.append(
                            _PendingFact(
                                entity="user",
                                attribute="child",
                                value=n,
                                confidence=0.80,
                                blurb=f"You have a child named {n}.",
                            )
                        )
                    if rel in {"sons", "son"}:
                        pending.append(
                            _PendingFact(
                                entity="user",
                                attribute="children_type",
                                value="sons",
                                confidence=0.70,
                                blurb="You mentioned your children are sons.",
                            )
                        )
                    elif rel in {"daughters", "daughter"}:
                        pending.append(
                            _PendingFact(
                                entity="user",
                                attribute="children_type",
                                value="daughters",
                                confidence=0.70,
                                blurb="You mentioned your children are daughters.",
                            )
                        )

        if pending:
            if self._memory_save_mode in {"ask", "confirm", "approval"}:
                # Stage for explicit user approval.
                self._pending_memory.setdefault(conversation_id, []).extend(pending)
                ask_to_confirm = True
            else:
                # Auto-save (relationships or all)
                for p in pending:
                    await self._memory.add_fact(
                        entity=p.entity,
                        attribute=p.attribute,
                        value=p.value,
                        confidence=p.confidence,
                    )
                    facts_written = True
                ask_to_confirm = False

        # RELATIONSHIP_EXTRACT_V1
        parent_patterns = [
            (r"\bmy\s+mom\s+(?:is|=)\s+([A-Z][A-Za-z\-']+(?:\s+[A-Z][A-Za-z\-']+)*)", "mother"),
            (r"\bmy\s+mother\s+(?:is|=)\s+([A-Z][A-Za-z\-']+(?:\s+[A-Z][A-Za-z\-']+)*)", "mother"),
            (r"\bmy\s+dad\s+(?:is|=)\s+([A-Z][A-Za-z\-']+(?:\s+[A-Z][A-Za-z\-']+)*)", "father"),
            (r"\bmy\s+father\s+(?:is|=)\s+([A-Z][A-Za-z\-']+(?:\s+[A-Z][A-Za-z\-']+)*)", "father"),
            (r"\bmy\s+mom['’]s\s+name\s+is\s+([A-Z][A-Za-z\-']+(?:\s+[A-Z][A-Za-z\-']+)*)", "mother"),
            (r"\bmy\s+mother['’]s\s+name\s+is\s+([A-Z][A-Za-z\-']+(?:\s+[A-Z][A-Za-z\-']+)*)", "mother"),
            (r"\bmy\s+dad['’]s\s+name\s+is\s+([A-Z][A-Za-z\-']+(?:\s+[A-Z][A-Za-z\-']+)*)", "father"),
            (r"\bmy\s+father['’]s\s+name\s+is\s+([A-Z][A-Za-z\-']+(?:\s+[A-Z][A-Za-z\-']+)*)", "father"),
            (r"\bi\s+have\s+a\s+mom\s+and\s+her\s+name\s+is\s+([A-Z][A-Za-z\-']+(?:\s+[A-Z][A-Za-z\-']+)*)", "mother"),
            (r"\bi\s+have\s+a\s+dad\s+and\s+his\s+name\s+is\s+([A-Z][A-Za-z\-']+(?:\s+[A-Z][A-Za-z\-']+)*)", "father"),
        ]
        for pat, attr in parent_patterns:
            mm = re.search(pat, msg, flags=re.IGNORECASE)
            if mm:
                name = mm.group(1).strip()
                await self._memory.add_fact(entity="user", attribute=attr, value=name, confidence=0.90)
                facts_written = True

        list_patterns = [
            (r"\bmy\s+cousins?\s+(?:are|=)\s+([^\.\n]+)", "cousin"),
            (r"\bmy\s+friends?\s+(?:are|=)\s+([^\.\n]+)", "friend"),
            (r"\bmy\s+siblings?\s+(?:are|=)\s+([^\.\n]+)", "sibling"),
            (r"\bmy\s+brothers?\s+(?:are|=)\s+([^\.\n]+)", "sibling"),
            (r"\bmy\s+sisters?\s+(?:are|=)\s+([^\.\n]+)", "sibling"),
        ]
        for pat, attr in list_patterns:
            mm = re.search(pat, msg, flags=re.IGNORECASE)
            if mm:
                names = self._split_name_list(mm.group(1))
                for n in names:
                    await self._memory.add_fact(entity="user", attribute=attr, value=n, confidence=0.85)
                    facts_written = True

        singular_patterns = [
            (r"\bmy\s+cousin\s+([A-Z][A-Za-z\-']+(?:\s+[A-Z][A-Za-z\-']+)*)\b", "cousin"),
            (r"\bmy\s+friend\s+([A-Z][A-Za-z\-']+(?:\s+[A-Z][A-Za-z\-']+)*)\b", "friend"),
            (r"\bmy\s+brother\s+([A-Z][A-Za-z\-']+(?:\s+[A-Z][A-Za-z\-']+)*)\b", "sibling"),
            (r"\bmy\s+sister\s+([A-Z][A-Za-z\-']+(?:\s+[A-Z][A-Za-z\-']+)*)\b", "sibling"),
            (r"\bi\s+have\s+a\s+brother\s+(?:named|name\s+is|is)\s+([A-Z][A-Za-z\-']+(?:\s+[A-Z][A-Za-z\-']+)*)\b", "sibling"),
            (r"\bi\s+have\s+a\s+sister\s+(?:named|name\s+is|is)\s+([A-Z][A-Za-z\-']+(?:\s+[A-Z][A-Za-z\-']+)*)\b", "sibling"),
        ]
        for pat, attr in singular_patterns:
            mm = re.search(pat, msg, flags=re.IGNORECASE)
            if mm:
                n = mm.group(1).strip()
                await self._memory.add_fact(entity="user", attribute=attr, value=n, confidence=0.85)
                facts_written = True

        pet_patterns = [
            (r"\bmy\s+dog\s+(?:is|named)\s+([A-Z][A-Za-z\-']+)\b", "dog"),
            (r"\bi\s+have\s+a\s+dog\s+named\s+([A-Z][A-Za-z\-']+)\b", "dog"),
            (r"\bmy\s+cat\s+(?:is|named)\s+([A-Z][A-Za-z\-']+)\b", "cat"),
            (r"\bi\s+have\s+a\s+cat\s+named\s+([A-Z][A-Za-z\-']+)\b", "cat"),
        ]
        for pat, species in pet_patterns:
            mm = re.search(pat, msg, flags=re.IGNORECASE)
            if mm:
                pet_name = mm.group(1).strip()
                await self._memory.add_fact(
                    entity="user",
                    attribute="pet",
                    value=self._pet_value(pet_name, species),
                    confidence=0.90,
                )
                facts_written = True

        return bool(ask_to_confirm)

    async def _maybe_route_tool(self, message: str) -> ToolCall | None:
        msg = message.lower()
        if msg.strip().startswith("/tool "):
            parts = message.split(" ", 2)
            if len(parts) >= 2:
                name = parts[1].strip()
                args: dict[str, Any] = {}
                if len(parts) == 3 and parts[2].strip():
                    try:
                        args = json.loads(parts[2])
                        if not isinstance(args, dict):
                            args = {}
                    except Exception:
                        return None
                return ToolCall(name=name, args=args)

        if "weather" in msg:
            m = re.search(r"weather\s+in\s+([A-Za-z][A-Za-z0-9 _-]{2,60})", message, flags=re.IGNORECASE)
            if m:
                return ToolCall(name="weather.current", args={"city": m.group(1).strip()})

        return None
