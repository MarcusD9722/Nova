"""STAGE 13A — one long conversation, one continuous assistant.

Not a unit suite. This drives the REAL production turn path, over the same
HTTP endpoint the frontend calls, for a single conversation of >=30 meaningful
turns, and asks whether Nova stays one coherent assistant across general chat,
personal memory, corrections, two projects, planning, interruption, failure and
recovery.

PRODUCTION FLOW EXERCISED (nothing here is re-implemented)

    POST /chat                          backend/app.py:1537
      -> _compose_chat_message
      -> _resolve_turn_identity          core/turn_identity.py
      -> Brain.chat                      core/brain.py:96
         -> RuntimeManager.chat_turn     core/runtime.py:1350
            -> chat_turn_stream          :1506   active_turn() + GATE.turn()
               -> _chat_turn_stream      :1533
                  -> _project_prepass    :1383   project_intent: mutate /
                                                 select / read-current,
                                                 pending-plan carry,
                                                 ProjectBuilder slug resolution
                  -> _extract_quick_facts        scoped_person_entity
                  -> _capture_* (lessons/mood)
                  -> _direct_live_reply          dates, capability, name, clock
                  -> grounding + memory retrieval
                  -> agent tool loop             ToolRouter + PermissionBroker
                  -> _stream_guarded_reply       repeat guard
                  -> _finish                     working ctx, ConversationState,
                                                 memory ingest queue

    and, on the approval turn only:

      _project_prepass -> ProjectBuilder.improve
                            -> plan call     ("improving an existing project")
                            -> per-file call ("Return the COMPLETE improved file")
                            -> write into the project directory

WHAT IS SUBSTITUTED, and why that does not hollow out the test

Only the model boundary. `ScriptedLLM` stands in for the 9B GGUF (loading it
per suite would take minutes, need the GPU exclusively, and make replies
non-deterministic), and Chroma is off because SQLite is the declared source of
truth. Orchestration, memory, project resolution, permissions, routing and
persistence are all production code.

The catch-all model rule is deliberately an ECHO of what production retrieval
actually put in front of the model. So when a recall assertion passes, it is
evidence that the fact reached the prompt — not evidence that the test scripted
the answer it wanted. Asserting on a hardcoded scripted reply would prove
nothing, which is the trap this stage is meant to avoid.

The same discipline governs the edit itself. The two `improve` rules do return
a file, but the numbers in it are READ OUT OF the prompt production handed them.
If the corrected requirement had not survived plan -> correction -> approval ->
orchestration, a different number would be on disk. This journey never calls
`code.write`; the decision to edit, the choice of project, the choice of file
and the write are all production's.

Run:  venv\\Scripts\\python.exe tests\\test_stage13a_coherence_journey.py
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path
from uuid import uuid4

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")

from harness import Checks, boot, run  # noqa: E402

check = Checks()

# Values that must never reach a reply by accident. `_topic` scrubs every one of
# them out of the question digest, so the ONLY way one of these can appear in a
# stub reply is if production retrieval put it in the prompt's memory block.
WATCHED = [
    "dark blue",
    "black with purple accents",
    "vertical opening",
    "horizontal spacing",
    "flappy-bird",
    "calculator",
    "monospace",
    "pause menu",
    "60 fps",
]


_ECHO_N = [0]

#: Where production puts what it RETRIEVED, as opposed to what is merely in the
#: transcript. Verified against a live prompt dump:
#:
#:     Who you're talking to: ...
#:     Things you remember:
#:     FACT user favorite_color = dark blue
#:     Recent messages:
#:     User: ...
#:     Assistant: ...
#:
#:     IMPORTANT: Reply with ONLY what you'd actually say ... reply directly.
#:     <the current user message>
_MEM_HEADER = "Things you remember:"
_MEM_END = ("Recent messages:", "IMPORTANT: Reply with ONLY")
_ASK_MARKER = "just say your reply directly."

#: Distinct sentence frames, cycled so stub replies are never near duplicates.
#: A counter alone was not enough: "(reply 7) Right — I don't have anything
#: stored" and "(reply 8) Right — I don't have anything stored" differ by one
#: token and the repeat guard scores them ~0.97 similar, so it rejected the
#: second and served its apology instead — swallowing the very answers this
#: journey reads. A real model does not emit the same sentence twice running.
_FRAMES = [
    "Going on what I have here —",
    "From what you've told me so far —",
    "Checking my notes —",
    "Here's the relevant part on file —",
    "What I'm holding for that —",
]
_EMPTY = [
    "I don't have anything on file about that yet.",
    "Nothing stored on that one so far.",
    "That hasn't been written down anywhere I can see.",
    "No notes on that — you haven't mentioned it before.",
    "I've got nothing recorded against that.",
]


def _retrieved(prompt: str) -> list[str]:
    """The memory production RETRIEVED for this turn, verbatim.

    Deliberately not "anything matching a watched word anywhere in the prompt".
    By turn 30 the transcript alone contains every value the journey ever
    mentioned, so echoing the whole prompt would let a recall assertion pass on
    conversation history even if memory retrieval were completely broken. This
    reads only the block production filled from the memory store.
    """
    i = prompt.find(_MEM_HEADER)
    if i < 0:
        return []
    block = prompt[i + len(_MEM_HEADER):]
    cut = min((block.find(m) for m in _MEM_END if block.find(m) >= 0),
              default=-1)
    if cut >= 0:
        block = block[:cut]
    out = []
    for line in block.splitlines():
        line = line.strip()
        if not line:
            continue
        out.append(line[5:].strip() if line.startswith("FACT ") else line)
    return out


def _topic(prompt: str) -> str:
    """A short, DE-FACTED digest of the question, for reply variety only.

    Every watched value is scrubbed out of it, so a recall assertion can never
    be satisfied by the stub quoting the question back. The only route from a
    remembered value into a reply is `_retrieved`.
    """
    i = prompt.rfind(_ASK_MARKER)
    q = prompt[i + len(_ASK_MARKER):] if i >= 0 else prompt[-160:]
    for w in WATCHED:
        q = re.sub(re.escape(w), "...", q, flags=re.IGNORECASE)
    return " ".join(q.split()[:9])


def _echo(prompt: str) -> str:
    """Report what production retrieved, phrased around the current question.

    Two jobs. It makes recall assertions evidence about RETRIEVAL rather than
    about scripting, and it varies with the question the way a real reply does
    — which matters, because a stub that answers every turn with the same
    sentence trips the production repeat guard and the journey ends up testing
    the guard instead of the assistant.
    """
    _ECHO_N[0] += 1
    n = _ECHO_N[0]
    facts = _retrieved(prompt)
    topic = _topic(prompt)
    if facts:
        return f"{_FRAMES[n % len(_FRAMES)]} {'; '.join(facts)}. (on: {topic})"
    return f"{_EMPTY[n % len(_EMPTY)]} (on: {topic})"


#: Words carrying no retrieval signal. The real prompt asks for "no stopwords".
_STOP = {
    "what", "which", "the", "a", "an", "did", "i", "you", "me", "my", "is",
    "are", "was", "were", "do", "does", "tell", "say", "said", "about", "for",
    "to", "of", "and", "that", "this", "it", "again", "remind", "we", "on",
    "in", "like", "prefer", "have", "has", "had", "just", "also", "still",
    "your", "yours", "there", "then", "how", "why", "when", "who", "am",
}

#: Cheap synonym expansion, matching what the real prompt explicitly asks for
#: ("include obvious synonyms").
_SYNONYMS = {
    "colour": ["color", "colors", "colours", "favorite_color"],
    "colours": ["color", "colors", "colour", "favorite_color"],
    "color": ["colour", "colours", "colors", "favorite_color"],
    "colors": ["colour", "colours", "color", "favorite_color"],
    "font": ["typeface", "monospace"],
}


def _search_terms(prompt: str) -> str:
    """Stand in for the memory SEARCH-TERM extractor — a separate model call.

    This one has to be scripted or the whole recall path is quietly crippled:
    production asks for `{"terms": [...]}` and hands the result to the memory
    store as the query. When the catch-all echo answered it with prose instead,
    retrieval fell back to something far weaker and the corrected colour stopped
    coming back — which showed up as three "memory" failures that were really
    this harness's fault. Class B, and worth the comment: an unscripted JSON
    boundary does not fail loudly, it just degrades the thing under test.
    """
    m = re.search(r"^Question:\s*(.+)$", prompt, re.M)
    q = (m.group(1) if m else "").lower()
    terms: list[str] = []
    for w in re.findall(r"[a-z0-9_\-]+", q):
        if len(w) < 3 or w in _STOP:
            continue
        terms.append(w)
        terms.extend(_SYNONYMS.get(w, ()))
    seen, out = set(), []
    for t in terms:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return json.dumps({"terms": out[:10]})


def _extractor(prompt: str) -> str:
    """Stand in for the memory extractor, which is itself an LLM call.

    Production owns validation, scoping, singleton superseding and persistence;
    the model's only job is turning prose into candidate facts, so that is all
    this supplies. It reads the prompt for the statement actually made rather
    than emitting a fixed answer, so a turn that never reached the extractor
    produces nothing.
    """
    low = prompt.lower()
    facts = []
    if "black with purple accents" in low:
        facts.append({"entity": "user", "attribute": "favorite_color",
                      "value": "black with purple accents", "confidence": 0.95,
                      "persist": True})
    elif "dark blue" in low:
        facts.append({"entity": "user", "attribute": "favorite_color",
                      "value": "dark blue", "confidence": 0.95, "persist": True})
    return json.dumps({"facts": facts})


#: Every prompt the two `improve` stages were handed, in order. These are the
#: receipts section E reads: they show what production actually carried into
#: the edit, and when.
IMPROVE_PROMPTS: list[tuple[str, str]] = []


def _improve_planner(prompt: str) -> str:
    """Stage 1 of ProjectBuilder.improve — which files to touch."""
    IMPROVE_PROMPTS.append(("plan", prompt))
    return json.dumps({
        "changes": [{"path": "game.js", "what": "adjust the pipe geometry"}],
        "summary": "adjust pipe geometry as requested",
    })


def _improve_file(prompt: str) -> str:
    """Stage 2 — the complete file, DERIVED FROM the instruction handed down.

    Deliberately not a constant. The vertical gap only becomes 138 if the
    CORRECTION reached the edit; the horizontal spacing only stays 220 if the
    value the correction protected reached it too. A stale, uncorrected plan
    produces 120/999 and section E fails.
    """
    IMPROVE_PROMPTS.append(("file", prompt))
    low = prompt.lower()
    gap = 138 if "vertical opening" in low else 120
    spacing = 220 if "keep the horizontal" in low else 999
    return ("```javascript\n"
            f"const PIPE_GAP_Y = {gap};\n"
            f"const PIPE_SPACING_X = {spacing};\n"
            "```")


async def _settle(nova, *, budget_s: float = 20.0) -> None:
    """Let asynchronous memory ingestion finish before reading storage.

    `_finish` QUEUES the turn for a background worker; the durable fact is not
    written by the time /chat returns. Asserting immediately reads an empty
    store and looks exactly like a memory defect, so the journey waits at the
    points where it inspects storage or asks Nova to recall.
    """
    worker = getattr(nova.runtime, "_memory_worker", None)
    if worker is not None:
        try:
            await worker._drain_queue_for_shutdown(budget_s=budget_s)
        except Exception:
            pass
    for _ in range(20):
        await asyncio.sleep(0.05)


# ── ledger ───────────────────────────────────────────────────────────────────

class Ledger:
    """Structured execution evidence. No reasoning traces, only observations."""

    def __init__(self) -> None:
        self.rows: list[dict] = []

    def add(self, *, step: int, section: str, user: str, intent: str,
            project: str | None, reads: str, writes: str, decision: str,
            tools: str, reply: str, expected: str, ok: bool) -> None:
        self.rows.append(dict(
            step=step, section=section, user=user[:70], intent=intent,
            project=project or "-", reads=reads, writes=writes,
            decision=decision, tools=tools, reply=(reply or "")[:70],
            expected=expected, result="PASS" if ok else "FAIL"))

    def find(self, intent: str) -> list[dict]:
        return [r for r in self.rows if r["intent"] == intent]

    def dump(self, path: Path) -> None:
        path.write_text(json.dumps(self.rows, indent=2), encoding="utf-8")

    def render(self) -> str:
        out = []
        for r in self.rows:
            out.append(f"{r['step']:>3} [{r['section']}] {r['result']:<4} "
                       f"proj={r['project']:<14} {r['intent']:<22} {r['user']}")
        return "\n".join(out)


LEDGER = Ledger()


# ── state probes (authoritative storage, not prose) ──────────────────────────

async def _fact(nova, entity: str, attribute: str) -> str:
    f = await nova.memory.get_latest_fact(entity=entity, attribute=attribute)
    return str(getattr(f, "value", "") or "") if f else ""


async def _all_facts(nova, entity: str) -> dict:
    rows = await nova.memory.get_facts(entity=entity, limit=100)
    return {r.attribute: str(r.value) for r in rows}


def _projects(nova) -> list[str]:
    d = nova.projects_dir
    return sorted(p.name for p in d.iterdir() if p.is_dir()) if d.exists() else []


def _tree(nova, slug: str) -> dict[str, str]:
    root = nova.projects_dir / slug
    if not root.exists():
        return {}
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            try:
                out[str(p.relative_to(root)).replace("\\", "/")] = p.read_text(
                    encoding="utf-8", errors="replace")
            except Exception:
                out[str(p.relative_to(root)).replace("\\", "/")] = "<binary>"
    return out


def _pending(nova, cid: str, slug: str) -> str:
    """The proposal pending for ONE project in ONE conversation.

    Project-scoped: a plan described for A is not reachable from B, which is
    what stops "switch to B, okay make that change" running A's plan on B.
    """
    return nova.runtime._pending_plan.get(cid, {}).get(slug, "")


async def _last_active(nova) -> str:
    """The AUTHORITATIVE current project, read from storage — not from prose."""
    return await nova.runtime._project_builder.last_active() or ""


# ── the conversation ─────────────────────────────────────────────────────────

class Journey:
    def __init__(self, nova) -> None:
        self.nova = nova
        self.cid = str(uuid4())
        self.step = 0
        self.section = "A"

    async def say(self, text: str) -> str:
        """One real turn through POST /chat — the endpoint the frontend uses."""
        self.step += 1
        r = await self.nova.http.post(
            "/chat", json={"message": text, "conversation_id": self.cid})
        assert r.status_code == 200, f"/chat {r.status_code}: {r.text[:200]}"
        body = r.json()
        self.cid = body["conversation_id"]
        return str(body.get("assistant") or "")

    def log(self, *, user, intent, project, reads, writes, decision, tools,
            reply, expected, ok) -> None:
        LEDGER.add(step=self.step, section=self.section, user=user, intent=intent,
                   project=project, reads=reads, writes=writes, decision=decision,
                   tools=tools, reply=reply, expected=expected, ok=ok)


async def journey(nova) -> None:
    j = Journey(nova)
    P = nova.projects_dir
    pb = nova.runtime._project_builder
    from core.tool_router import ToolCall

    # ── A: general conversation must not touch projects ──────────────────────
    j.section = "A"
    check.section("A: ordinary conversation changes nothing")

    before_projects = _projects(nova)
    before_active = await _last_active(nova)

    for text in ("Hey Nova, how's it going?",
                 "I've been working on a lot of stuff lately.",
                 "I think my Flappy Bird project is finally starting to look decent."):
        reply = await j.say(text)
        after = _projects(nova)
        ok = (after == before_projects)
        j.log(user=text, intent="smalltalk", project=None, reads="conversation",
              writes="conversation", decision="respond", tools="none",
              reply=reply, expected="no project created", ok=ok)
        check(ok, f"[{j.step}] no project appeared from small talk ({after})")
        check(bool(reply.strip()), f"[{j.step}] Nova answered ({reply[:40]!r})")

    check(await _last_active(nova) == before_active,
          "and the current-project pointer is untouched")

    # ── B: durable personal memory + correction ──────────────────────────────
    j.section = "B"
    check.section("B: a personal fact is stored, recalled, then corrected")

    reply = await j.say("My favorite color for desktop themes is dark blue.")
    j.log(user="favorite color = dark blue", intent="state_preference", project=None,
          reads="-", writes="memory:user", decision="respond", tools="none",
          reply=reply, expected="stored", ok=True)

    for filler in ("Anyway, the weather's been miserable this week.",
                   "I watched a documentary about deep sea vents last night.",
                   "Do you ever get bored running in the background?"):
        r = await j.say(filler)
        j.log(user=filler, intent="smalltalk", project=None, reads="conversation",
              writes="conversation", decision="respond", tools="none", reply=r,
              expected="normal answer", ok=bool(r.strip()))

    await _settle(nova)
    recall = await j.say("What color did I tell you I like for desktop themes?")
    got_it = "dark blue" in recall.lower()
    j.log(user="recall desktop colour", intent="recall_preference", project=None,
          reads="memory:user", writes="conversation", decision="respond",
          tools="none", reply=recall, expected="dark blue", ok=got_it)
    check(got_it, f"[{j.step}] the stored colour reached the answer ({recall[:70]!r})")
    check(await _fact(nova, "user", "favorite_color") == "dark blue",
          "and the durable fact says so too")

    corr = await j.say("Actually, change that. For desktop interfaces I prefer "
                       "black with purple accents.")
    j.log(user="correct the colour", intent="correct_preference", project=None,
          reads="memory:user", writes="memory:user", decision="respond",
          tools="none", reply=corr, expected="correction stored", ok=True)

    await j.say("What was that documentary called again? Never mind.")
    await _settle(nova)
    recall2 = await j.say("Remind me what desktop colours I prefer.")
    corrected = "black with purple accents" in recall2.lower()
    j.log(user="recall after correction", intent="recall_preference", project=None,
          reads="memory:user", writes="conversation", decision="respond",
          tools="none", reply=recall2, expected="corrected value wins",
          ok=corrected)
    check(corrected, f"[{j.step}] the CORRECTION is what comes back ({recall2[:70]!r})")
    durable = await _fact(nova, "user", "favorite_color")
    check(durable == "black with purple accents",
          f"[{j.step}] the authoritative fact is the corrected one ({durable!r})")

    # ── C: project discussion without authorisation ──────────────────────────
    j.section = "C"
    check.section("C: talking about a project does not modify it")

    for name in ("flappy-bird", "calculator"):
        res = await nova.runtime._router.execute(
            ToolCall("project.scaffold", {"name": name}))
        check(res.ok, f"{name} scaffolded via project.scaffold ({res.error})")

    # ONE project existence contract: what the scaffolding tool created is what
    # conversation, status, selection and resume can all see. Before the
    # contract was unified, `list_projects()` required a PROJECT.md the
    # scaffolder never wrote, so a freshly scaffolded project was invisible to
    # every conversational path.
    check(sorted(pb.list_projects()) == ["calculator", "flappy-bird"],
          f"scaffolded projects are conversationally addressable "
          f"({pb.list_projects()})")

    (P / "flappy-bird" / "game.js").write_text(
        "const PIPE_GAP_Y = 120;\nconst PIPE_SPACING_X = 220;\n", encoding="utf-8")
    (P / "calculator" / "main.py").write_text("def add(a, b):\n    return a + b\n",
                                              encoding="utf-8")
    snapshot_a = _tree(nova, "flappy-bird")
    snapshot_b = _tree(nova, "calculator")

    for text in ("I've been thinking about improving the Flappy Bird project.",
                 "I don't really like how unforgiving the pipes feel.",
                 "It's frustrating when you die on the very first pipe."):
        reply = await j.say(text)
        unchanged = _tree(nova, "flappy-bird") == snapshot_a
        j.log(user=text, intent="discuss_project", project="flappy-bird",
              reads="project:flappy-bird", writes="conversation",
              decision="discuss", tools="none", reply=reply,
              expected="no file mutation", ok=unchanged)
        check(unchanged, f"[{j.step}] discussion left the files alone")
        check("i updated" not in reply.lower(),
              f"[{j.step}] and claimed no edit ({reply[:60]!r})")

    # ── D: selection, then plan without executing ────────────────────────────
    j.section = "D"
    check.section("D: select a project, then plan before acting")

    sel = await j.say("Open Flappy Bird.")
    active = await _last_active(nova)
    j.log(user="select flappy-bird", intent="select_project", project=active,
          reads="projects", writes="projects:last_active", decision="select",
          tools="none", reply=sel, expected="flappy-bird is current",
          ok=active == "flappy-bird")
    check(active == "flappy-bird",
          f"[{j.step}] selection set the AUTHORITATIVE pointer ({active!r})")
    check("flappy-bird" in sel.lower(),
          f"[{j.step}] and Nova said which project ({sel[:60]!r})")
    check(_tree(nova, "flappy-bird") == snapshot_a,
          f"[{j.step}] selecting is not mutating — nothing inside it changed")

    plan = await j.say("Help me make the pipe spacing easier, but don't change "
                       "anything yet. First tell me what you think should change.")
    pending = _pending(nova, j.cid, "flappy-bird")
    j.log(user="plan, do not change yet", intent="plan_only", project="flappy-bird",
          reads="project:flappy-bird", writes="pending_plan", decision="plan",
          tools="none", reply=plan, expected="plan recorded, no write",
          ok=bool(pending) and _tree(nova, "flappy-bird") == snapshot_a)
    check(_tree(nova, "flappy-bird") == snapshot_a,
          f"[{j.step}] 'don't change anything yet' wrote nothing")
    check("pipe spacing" in pending.lower(),
          f"[{j.step}] the described change is CARRIED, not discarded "
          f"({pending[:60]!r})")
    check(not IMPROVE_PROMPTS,
          f"[{j.step}] and no edit orchestration ran "
          f"({[k for k, _ in IMPROVE_PROMPTS]})")

    # ── E: correction supersedes, then approval executes ─────────────────────
    j.section = "E"
    check.section("E: plan -> correction -> approval -> execution, all via /chat")

    corr = await j.say("Actually, keep the horizontal spacing the way it was. "
                       "I meant make the vertical opening 15% larger.")
    pending = _pending(nova, j.cid, "flappy-bird")
    j.log(user="correct the plan", intent="correct_plan", project="flappy-bird",
          reads="pending_plan", writes="pending_plan", decision="replan",
          tools="none", reply=corr, expected="plan replaced",
          ok="vertical opening" in pending.lower())
    check("vertical opening" in pending.lower(),
          f"[{j.step}] the correction REPLACED the plan ({pending[:70]!r})")
    check("don't change anything yet" not in pending.lower(),
          f"[{j.step}] and the superseded wording did not survive")
    check(_tree(nova, "flappy-bird") == snapshot_a,
          f"[{j.step}] a correction is still not an instruction to act")
    check(not IMPROVE_PROMPTS, f"[{j.step}] still nothing executed")

    approve = await j.say("Okay, make that change.")
    for _ in range(400):
        await asyncio.sleep(0.05)
        if IMPROVE_PROMPTS and IMPROVE_PROMPTS[-1][0] == "file" \
                and "PIPE_GAP_Y" in _tree(nova, "flappy-bird").get("game.js", ""):
            break
    text_a = _tree(nova, "flappy-bird")["game.js"]
    stages = [k for k, _ in IMPROVE_PROMPTS]
    j.log(user="approve", intent="approve_plan", project="flappy-bird",
          reads="pending_plan+project", writes="project:flappy-bird",
          decision="act", tools="ProjectBuilder.improve -> file write",
          reply=approve, expected="corrected change applied",
          ok="PIPE_GAP_Y = 138" in text_a)

    # Production's own orchestration ran — this journey never called code.write.
    check(stages[:2] == ["plan", "file"],
          f"[{j.step}] the two-stage improve orchestration ran ({stages})")
    plan_prompt = next(p for k, p in IMPROVE_PROMPTS if k == "plan")
    file_prompt = next(p for k, p in IMPROVE_PROMPTS if k == "file")
    check("game.js" in plan_prompt,
          f"[{j.step}] planning saw real project state, not only the request")
    check("vertical opening" in file_prompt.lower(),
          f"[{j.step}] the CORRECTED requirement reached the edit")
    check("keep the horizontal" in file_prompt.lower(),
          f"[{j.step}] including the part the correction protected")
    check("okay, make that change" in file_prompt.lower(),
          f"[{j.step}] and the approval turn is what carried it")

    check("PIPE_GAP_Y = 138" in text_a,
          f"[{j.step}] the CORRECTED change executed ({text_a.splitlines()[:1]})")
    check("PIPE_SPACING_X = 220" in text_a,
          f"[{j.step}] the horizontal value the correction protected is untouched")
    check("999" not in text_a,
          f"[{j.step}] and the superseded plan never ran")
    check(not _pending(nova, j.cid, "flappy-bird"),
          f"[{j.step}] the approved plan was consumed, not left to re-fire")
    check(_tree(nova, "calculator") == snapshot_b,
          f"[{j.step}] the other project was not touched")
    check(await _last_active(nova) == "flappy-bird",
          f"[{j.step}] and the edit landed in the current project")

    # ── F: interruption ──────────────────────────────────────────────────────
    j.section = "F"
    check.section("F: unrelated questions do not disturb the project")

    ram = await j.say("By the way, what's the difference between RAM and VRAM?")
    j.log(user="RAM vs VRAM", intent="general_question", project=None,
          reads="-", writes="conversation", decision="respond", tools="none",
          reply=ram, expected="normal answer", ok=bool(ram.strip()))
    check(bool(ram.strip()), f"[{j.step}] a general question still answers")

    await _settle(nova)
    colours = await j.say("Also remind me what desktop colours I said I prefer.")
    ok = "black with purple accents" in colours.lower()
    j.log(user="recall colours mid-project", intent="recall_preference",
          project=None, reads="memory:user", writes="conversation",
          decision="respond", tools="none", reply=colours,
          expected="corrected value", ok=ok)
    check(ok, f"[{j.step}] personal memory still resolves while a project is open")

    for chat in ("My coffee machine finally died this morning.",
                 "I might just start drinking tea instead."):
        r = await j.say(chat)
        j.log(user=chat, intent="smalltalk", project=None, reads="conversation",
              writes="conversation", decision="respond", tools="none", reply=r,
              expected="normal answer", ok=bool(r.strip()))

    check(_tree(nova, "flappy-bird")["game.js"] == text_a,
          "the interruption changed nothing in the project")
    check(await _last_active(nova) == "flappy-bird",
          "and small talk did not move the current project")

    # ── G: return to the project ─────────────────────────────────────────────
    j.section = "G"
    check.section("G: returning resumes CURRENT state")

    back = await j.say("Alright, back to the Flappy Bird thing. Where were we?")
    j.log(user="where were we", intent="resume_project", project="flappy-bird",
          reads="project:flappy-bird", writes="conversation", decision="report",
          tools="project status", reply=back, expected="current state",
          ok="flappy-bird" in back.lower())
    check("flappy-bird" in back.lower(),
          f"[{j.step}] Nova named the project she resumed ({back[:60]!r})")
    check(_tree(nova, "flappy-bird")["game.js"] == text_a,
          f"[{j.step}] and resuming re-ran nothing")

    # ── H: second project, authoritative switching ───────────────────────────
    j.section = "H"
    check.section("H: A -> B -> A is authoritative, not just conversational")

    sw = await j.say("Let's work on the calculator project.")
    now_b = await _last_active(nova)
    j.log(user="switch to calculator", intent="select_project", project=now_b,
          reads="projects", writes="projects:last_active", decision="select",
          tools="none", reply=sw, expected="calculator is current",
          ok=now_b == "calculator")
    check(now_b == "calculator",
          f"[{j.step}] the current project moved to B ({now_b!r})")
    check(_tree(nova, "flappy-bird")["game.js"] == text_a,
          f"[{j.step}] selecting B changed nothing in A")
    check(_tree(nova, "calculator") == snapshot_b,
          f"[{j.step}] and selecting B did not modify B either")

    b_req = await j.say("For the calculator, I want the display to use a "
                        "monospace font.")
    j.log(user="calculator requirement", intent="state_requirement",
          project="calculator", reads="-", writes="conversation",
          decision="respond", tools="none", reply=b_req,
          expected="scoped to B", ok=True)

    mid = await j.say("Which one are we on right now?")
    mid_ok = "calculator" in mid.lower()
    j.log(user="which one are we on", intent="recall_project", project="calculator",
          reads="projects:last_active", writes="conversation", decision="respond",
          tools="none", reply=mid, expected="calculator", ok=mid_ok)
    check(mid_ok, f"[{j.step}] mid-switch, Nova reports B ({mid[:60]!r})")

    back2 = await j.say("Go back to Flappy Bird.")
    now_a = await _last_active(nova)
    j.log(user="switch back to A", intent="select_project", project=now_a,
          reads="projects", writes="projects:last_active", decision="select",
          tools="none", reply=back2, expected="flappy-bird is current",
          ok=now_a == "flappy-bird")
    check(now_a == "flappy-bird", f"[{j.step}] and back to A ({now_a!r})")
    check(_tree(nova, "calculator") == snapshot_b,
          f"[{j.step}] B survived the round trip untouched")

    # A casual MENTION of the other project is not a switch.
    mention = await j.say("The calculator was fiddly to get right, honestly.")
    after_mention = await _last_active(nova)
    j.log(user="casual mention of B", intent="mention_project", project=after_mention,
          reads="conversation", writes="conversation", decision="respond",
          tools="none", reply=mention, expected="current project unchanged",
          ok=after_mention == "flappy-bird")
    check(after_mention == "flappy-bird",
          f"[{j.step}] merely naming B did not switch to it ({after_mention!r})")

    # ── I: memory scoping ────────────────────────────────────────────────────
    j.section = "I"
    check.section("I: a requirement for B is not a global fact")

    await _settle(nova)
    a_ask = await j.say("For Flappy Bird, what font did I ask for?")
    user_facts = await _all_facts(nova, "user")
    # Honest note: `global_leak` is partly stub-influenced, because the memory
    # extractor is itself a model call and this harness's extractor emits no
    # font fact. The load-bearing claim is `file_leak` — production never wrote
    # B's requirement into A — plus A's code being byte-identical to what E made.
    global_leak = [k for k, v in user_facts.items() if "monospace" in str(v).lower()]
    file_leak = [f for f, t in _tree(nova, "flappy-bird").items()
                 if "monospace" in t.lower()]
    j.log(user="B's requirement asked of A", intent="recall_requirement",
          project="flappy-bird", reads="memory+project", writes="conversation",
          decision="respond", tools="none", reply=a_ask,
          expected="not global, not in A", ok=not global_leak and not file_leak)
    check(not global_leak,
          f"[{j.step}] B's font requirement did not become a global fact "
          f"({global_leak})")
    check(not file_leak, f"[{j.step}] and never reached A's files ({file_leak})")
    check(_tree(nova, "flappy-bird")["game.js"] == text_a,
          f"[{j.step}] A's code is still exactly what E produced")

    # ── J: planning truth, and work that stays planned ───────────────────────
    j.section = "J"
    check.section("J: discussed / planned / done are not the same word")

    later = await j.say("I'd like you to add a pause menu later on, but don't build it yet.")
    still_planned = _pending(nova, j.cid, "flappy-bird")
    j.log(user="plan a pause menu, not now", intent="plan_only",
          project="flappy-bird", reads="project", writes="pending_plan",
          decision="plan", tools="none", reply=later,
          expected="recorded, not executed",
          ok="pause menu" in still_planned.lower())
    check("pause menu" in still_planned.lower(),
          f"[{j.step}] the pause menu is held as PLANNED ({still_planned[:60]!r})")
    check(_tree(nova, "flappy-bird")["game.js"] == text_a,
          f"[{j.step}] and nothing was built for it")
    check([k for k, _ in IMPROVE_PROMPTS].count("file") == 1,
          f"[{j.step}] exactly one edit has ever executed "
          f"({[k for k, _ in IMPROVE_PROMPTS]})")

    truth = await j.say("What have we actually completed, and what is still "
                        "just planned?")
    low = truth.lower()
    j.log(user="completed vs planned", intent="status_report", project="flappy-bird",
          reads="project+conversation", writes="conversation", decision="report",
          tools="none", reply=truth, expected="does not claim completion",
          ok=bool(truth.strip()))
    check(bool(truth.strip()), f"[{j.step}] Nova answered the status question")
    check("all done" not in low,
          f"[{j.step}] and did not declare the project finished ({truth[:70]!r})")

    # ── K: a real failure, honestly reported ─────────────────────────────────
    j.section = "K"
    check.section("K: a tool failure is reported, not fabricated into success")

    bad = await nova.runtime._router.execute(
        ToolCall("code.read", {"path": str(P / "flappy-bird" / "does-not-exist.js")}))
    j.step += 1
    failed = (not bad.ok) or (isinstance(bad.result, dict)
                              and bad.result.get("ok") is False)
    j.log(user="(read a missing file)", intent="tool_failure", project="flappy-bird",
          reads="project:flappy-bird", writes="-", decision="act",
          tools="code.read", reply=str(bad.error or bad.result)[:60],
          expected="failure reported", ok=failed)
    check(failed, f"[{j.step}] the missing file is a real failure ({bad.ok})")
    check("does-not-exist.js" not in _tree(nova, "flappy-bird"),
          "and the failed read did not conjure the file into existence")
    check(_tree(nova, "calculator") == snapshot_b,
          "and the failure did not touch the other project")

    recover = await nova.runtime._router.execute(ToolCall("code.read", {
        "path": str(P / "flappy-bird" / "game.js")}))
    j.step += 1
    recovered = bool(recover.ok) and "PIPE_GAP_Y = 138" in str(recover.result)
    j.log(user="(re-read the real file)", intent="recover", project="flappy-bird",
          reads="project:flappy-bird", writes="-", decision="act",
          tools="code.read", reply=str(recover.ok), expected="recovered",
          ok=recovered)
    check(recovered, f"[{j.step}] recovery read the real, corrected content back")

    # ── L: integrated recall, concrete ───────────────────────────────────────
    j.section = "L"
    check.section("L: final recall is concrete and matches authoritative state")

    await _settle(nova)

    # 1. corrected personal preference
    q_colour = await j.say("What UI colours do I prefer?")
    ok = "black with purple accents" in q_colour.lower()
    j.log(user="final colour recall", intent="recall_preference", project=None,
          reads="memory:user", writes="conversation", decision="respond",
          tools="none", reply=q_colour, expected="corrected value", ok=ok)
    check(ok, f"[{j.step}] the corrected colour still wins ({q_colour[:70]!r})")

    # 2. the current project — prose AND authoritative state, and they agree
    q_proj = await j.say("What project are we working on?")
    named = "flappy-bird" in q_proj.lower()
    j.log(user="which project", intent="recall_project", project="flappy-bird",
          reads="projects:last_active", writes="conversation", decision="respond",
          tools="none", reply=q_proj, expected="flappy-bird", ok=named)
    check(named,
          f"[{j.step}] Nova names the actual current project ({q_proj[:70]!r})")
    check(await _last_active(nova) == "flappy-bird",
          f"[{j.step}] and it matches authoritative state")
    check("calculator" not in q_proj.lower(),
          f"[{j.step}] and not the other one ({q_proj[:70]!r})")

    # 3 + 4. the corrected requirement, and the change that actually succeeded
    final = _tree(nova, "flappy-bird")["game.js"]
    check("PIPE_GAP_Y = 138" in final,
          "the change that succeeded is still the corrected one")
    check("PIPE_SPACING_X = 220" in final,
          "the value the correction protected is still protected")
    check("999" not in final, "and the superseded plan is nowhere on disk")

    # 5. work that is still only planned
    check("pause menu" in _pending(nova, j.cid, "flappy-bird").lower(),
          "the pause menu is still planned, not built")
    check(not any("pause" in t.lower() for t in _tree(nova, "flappy-bird").values()),
          "and no pause-menu code exists")

    # 6. the controlled failure and its recovery are both on the record
    fails = LEDGER.find("tool_failure")
    recs = LEDGER.find("recover")
    check(len(fails) == 1 and fails[0]["result"] == "PASS",
          f"the failure is recorded as a handled failure ({fails})")
    check(len(recs) == 1 and recs[0]["result"] == "PASS",
          f"and so is the recovery ({recs})")

    check(sorted(_projects(nova)) == ["calculator", "flappy-bird"],
          f"exactly the two intended projects exist ({_projects(nova)})")
    check("general" not in _projects(nova),
          "and no accidental 'general' project was invented")


# ── adversarial intent boundary ──────────────────────────────────────────────

async def adversarial(nova) -> None:
    check.section("Adversarial: mentioning an action is not authorising it")

    before = _tree(nova, "flappy-bird")
    cid = str(uuid4())

    async def say(t: str) -> str:
        r = await nova.http.post("/chat", json={"message": t, "conversation_id": cid})
        assert r.status_code == 200, r.text[:200]
        return str(r.json().get("assistant") or "")

    non_authorising = [
        "I've been improving Nova.",
        "I need to fix my game sometime.",
        "You should see the project I made.",
        "I wish the menu looked better.",
        "I was thinking about deleting the old project.",
        "Deleting the old project was probably a bad idea.",
        "Don't change anything.",
        "What would you change?",
        "Tell me how you'd improve it.",
        "Actually don't.",
    ]
    from core.project_intent import authorize_project_mutation

    for t in non_authorising:
        reply = await say(t)
        now = _tree(nova, "flappy-bird")
        # Two separate claims. "Nothing changed" can pass for the wrong reason
        # — a fixture where no mutation was reachable anyway would look clean
        # with the gate wide open (measured: removing the grammatical
        # restriction left this section green). So the AUTHORISATION DECISION
        # itself is read, since that is the load-bearing predicate.
        verdict = authorize_project_mutation(t, complaint=False)
        ok = (now == before) and not verdict.allowed
        LEDGER.add(step=0, section="ADV", user=t, intent="mention_only",
                   project="flappy-bird", reads="project", writes="none",
                   decision=f"refused: {verdict.reason}", tools="none",
                   reply=reply, expected="no authorisation, no mutation", ok=ok)
        check(not verdict.allowed,
              f"{t!r} is not authorisation ({verdict.reason})")
        check(now == before, f"{t!r} did not mutate the project")
        check(_projects(nova) == ["calculator", "flappy-bird"],
              f"{t!r} created no project ({_projects(nova)})")


# ── repetition / stale-turn ──────────────────────────────────────────────────

async def repetition(nova) -> None:
    check.section("Repetition: no stale answer is replayed for a new turn")

    cid = str(uuid4())

    async def say(t: str) -> str:
        r = await nova.http.post("/chat", json={"message": t, "conversation_id": cid})
        assert r.status_code == 200, r.text[:200]
        return str(r.json().get("assistant") or "")

    a = await say("What are you capable of?")
    b = await say("The pipes in that game still feel a bit tight to me.")
    check(a.strip() != b.strip() or not a.strip(),
          f"a different question gets a different answer ({b[:50]!r})")

    await _settle(nova)
    c = await say("Remind me what desktop colours I prefer.")
    check("purple" in c.lower(),
          f"a related follow-up is answered freshly ({c[:60]!r})")

    d1 = await say("Explain what RAID 5 is.")
    d2 = await say("Explain what RAID 5 is.")
    check(bool(d1.strip()) and bool(d2.strip()),
          "the identical question twice is answered both times, not suppressed")


# ── persistence note ─────────────────────────────────────────────────────────

async def persistence_probe(nova) -> str:
    """What survives a restart is asserted at the STORAGE layer here.

    A full process restart is deferred: `boot()` owns one temp root and one
    `_startup()`/`_shutdown()` pair, so restarting inside a single journey would
    tear down the very SQLite file the journey is asserting against. Faking it
    by re-reading the same live objects would prove nothing, so this stage
    instead verifies that the durable facts are in durable storage — the thing a
    restart would actually reload — and the real restart journey belongs in
    final E2E.
    """
    check.section("Persistence: the durable half is genuinely in durable storage")

    await _settle(nova)

    colour = await _fact(nova, "user", "favorite_color")
    facts = await _all_facts(nova, "user")
    last = await _last_active(nova)

    check((nova.root / "memory_data").exists(), "the memory store is on disk")
    check(bool(facts), f"user facts are persisted ({sorted(facts)[:4]})")
    check(colour == "black with purple accents",
          f"the corrected preference is the durable one ({colour!r})")
    # The current project is a durable FACT, not process state — which is what
    # makes "which project are we on" survive a restart at all.
    check(last == "flappy-bird",
          f"the current project is durable, not in-memory ({last!r})")
    check(await _fact(nova, "projects", "last_active") == "flappy-bird",
          "and it is stored where a restart would read it back")
    check((nova.projects_dir / "flappy-bird" / "game.js").exists(),
          "project A is on disk")
    check((nova.projects_dir / "calculator" / "main.py").exists(),
          "project B is on disk")
    return f"colour={colour!r} last_active={last!r} facts={len(facts)}"


async def main() -> None:
    async with boot() as nova:
        # Order matters: the harness's agent-decider rule is already first, the
        # extractor and the two improve stages each need their own shape, and
        # the echo is the catch-all.
        nova.llm.when("Extract the key search terms", _search_terms,
                      label="memory-search-terms")
        nova.llm.when("You extract explicit user facts", _extractor,
                      label="memory-extractor")
        nova.llm.when("You are Nova improving an existing project", _improve_planner,
                      label="improve-planner")
        nova.llm.when("Return the COMPLETE improved file", _improve_file,
                      label="improve-file")
        nova.llm.when(lambda _p: True, _echo, label="echo-grounding")

        await journey(nova)
        await adversarial(nova)
        await repetition(nova)
        note = await persistence_probe(nova)

        out = REPO / "tests" / "_stage13a_ledger.json"
        LEDGER.dump(out)
        print()
        print("── LEDGER " + "─" * 60)
        print(LEDGER.render())
        print(f"\nsteps recorded: {len(LEDGER.rows)}")
        print(f"persistence: {note}")
        print(f"ledger written: {out}")
    check.finish()


if __name__ == "__main__":
    run(main)
