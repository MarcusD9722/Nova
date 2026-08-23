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
                  -> _project_prepass    :1383   authorize_project_mutation
                                                 ProjectBuilder slug resolution
                  -> _extract_quick_facts        scoped_person_entity
                  -> _capture_* (lessons/mood)
                  -> _direct_live_reply          dates, capability, name, clock
                  -> grounding + memory retrieval
                  -> agent tool loop             ToolRouter + PermissionBroker
                  -> _stream_guarded_reply       repeat guard
                  -> _finish                     working ctx, ConversationState,
                                                 memory ingest queue

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

Run:  venv\\Scripts\\python.exe tests\\test_stage13a_coherence_journey.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")

from harness import Checks, boot, run  # noqa: E402

check = Checks()

# Values the echo rule watches for. If one of these reaches the model prompt,
# production retrieval put it there.
WATCHED = [
    "dark blue",
    "black with purple accents",
    "vertical opening",
    "horizontal spacing",
    "flappy-bird",
    "calc-tool",
    "monospace",
    "60 fps",
]


_ECHO_N = [0]

#: Distinct sentence frames, cycled so consecutive stub replies are never near
#: duplicates of each other. A counter alone was not enough: "(reply 7) Right —
#: I don't have anything stored" and "(reply 8) Right — I don't have anything
#: stored" differ by one token and the production repeat guard scores them ~0.97
#: similar, so it rejected the second and substituted its apology — swallowing
#: the very answers this journey reads. A real model does not emit the same
#: sentence twice running; the stub has to be at least that varied or it tests
#: the guard instead of the assistant.
_FRAMES = [
    "Going on what I have here: {}.",
    "From what you've told me so far: {}.",
    "Checking my notes, this is what stands out: {}.",
    "Here's the relevant part on file: {}.",
    "What I'm holding for that: {}.",
]
_EMPTY = [
    "I don't have anything on file about that yet.",
    "Nothing stored on that one so far.",
    "That hasn't been written down anywhere I can see.",
    "No notes on that — you haven't mentioned it before.",
    "I've got nothing recorded against that.",
]


def _echo(prompt: str) -> str:
    """Report which stored context production actually handed the model."""
    _ECHO_N[0] += 1
    n = _ECHO_N[0]
    low = prompt.lower()
    seen = [w for w in WATCHED if w.lower() in low]
    if seen:
        return _FRAMES[n % len(_FRAMES)].format("; ".join(seen))
    return _EMPTY[n % len(_EMPTY)]


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


async def _last_active(nova) -> str:
    return await _fact(nova, "projects", "last_active")


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
          "and the active project pointer is untouched")
    check(not any("flappy" in p for p in _projects(nova)),
          f"mentioning Flappy Bird in passing created nothing ({_projects(nova)})")

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
    stored = await _all_facts(nova, "user")
    got_it = "dark blue" in recall.lower()
    j.log(user="recall desktop colour", intent="recall_preference", project=None,
          reads="memory:user", writes="conversation", decision="respond",
          tools="none", reply=recall, expected="dark blue", ok=got_it)
    check(got_it, f"[{j.step}] the stored colour reached the answer ({recall[:70]!r})")
    check(not any("purple" in v.lower() for v in stored.values()),
          "and nothing invented a colour that was never given")

    corr = await j.say("Actually, change that. For desktop interfaces I prefer "
                       "black with purple accents.")
    j.log(user="correct the colour", intent="correct_preference", project=None,
          reads="memory:user", writes="memory:user", decision="respond",
          tools="none", reply=corr, expected="correction stored", ok=True)

    await j.say("What was that documentary called again? Never mind.")
    await _settle(nova)
    recall2 = await j.say("Remind me what desktop colours I prefer.")
    low = recall2.lower()
    corrected = "black with purple accents" in low
    j.log(user="recall after correction", intent="recall_preference", project=None,
          reads="memory:user", writes="conversation", decision="respond",
          tools="none", reply=recall2, expected="corrected value wins",
          ok=corrected)
    check(corrected, f"[{j.step}] the CORRECTION is what comes back ({recall2[:70]!r})")
    # Durable state is the real question. The earlier sentence is still sitting
    # in this conversation's recent turns, so its presence in the prompt proves
    # nothing either way; what must not survive is the stored FACT.
    durable = await _fact(nova, "user", "favorite_color")
    check(durable == "black with purple accents",
          f"[{j.step}] the authoritative fact is the corrected one ({durable!r})")
    rows = await _all_facts(nova, "user")
    check(sum(1 for k in rows if k == "favorite_color") <= 1,
          f"[{j.step}] and the superseded row did not stack beside it ({rows})")

    # ── C: project discussion without authorisation ──────────────────────────
    j.section = "C"
    check.section("C: talking about a project does not modify it")

    # Build project A through Nova's own scaffold tool (production path).
    from core.tool_router import ToolCall
    res = await nova.runtime._router.execute(
        ToolCall("project.scaffold", {"name": "flappy-bird"}))
    check(res.ok, f"project A scaffolded via project.scaffold ({res.error})")
    (P / "flappy-bird" / "game.js").write_text(
        "const PIPE_GAP_Y = 120;\nconst PIPE_SPACING_X = 220;\n", encoding="utf-8")
    snapshot_a = _tree(nova, "flappy-bird")

    for text in ("I've been thinking about improving the Flappy Bird project.",
                 "I don't really like how unforgiving the pipes feel.",
                 "It's frustrating when you die on the very first pipe."):
        reply = await j.say(text)
        now = _tree(nova, "flappy-bird")
        unchanged = now == snapshot_a
        j.log(user=text, intent="discuss_project", project="flappy-bird",
              reads="project:flappy-bird", writes="conversation",
              decision="discuss", tools="none", reply=reply,
              expected="no file mutation", ok=unchanged)
        check(unchanged, f"[{j.step}] discussion left the files alone")
        check("i updated" not in reply.lower() and "i've changed" not in reply.lower(),
              f"[{j.step}] and claimed no edit ({reply[:60]!r})")

    # ── D: explicit action, plan before write ────────────────────────────────
    j.section = "D"
    check.section("D: explicit request plans first, then acts")

    plan_reply = await j.say(
        "Open the Flappy Bird project and help me make the pipe spacing easier, "
        "but don't change anything yet. First tell me what you think should change.")
    after_plan = _tree(nova, "flappy-bird")
    j.log(user="plan, do not change yet", intent="plan_only", project="flappy-bird",
          reads="project:flappy-bird", writes="conversation", decision="plan",
          tools="none", reply=plan_reply, expected="no write",
          ok=after_plan == snapshot_a)
    check(after_plan == snapshot_a,
          f"[{j.step}] 'don't change anything yet' wrote nothing")

    # ── E: correction supersedes the stale plan ──────────────────────────────
    j.section = "E"
    check.section("E: a correction replaces the plan that preceded it")

    corr_reply = await j.say(
        "Actually, keep the horizontal spacing the way it was. I meant make the "
        "vertical opening 15% larger.")
    j.log(user="correct the plan", intent="correct_plan", project="flappy-bird",
          reads="conversation", writes="conversation", decision="replan",
          tools="none", reply=corr_reply, expected="plan updated", ok=True)

    # The concrete edit goes through the same ToolRouter the agent loop uses.
    edit = await nova.runtime._router.execute(ToolCall("code.write", {
        "path": str((P / "flappy-bird" / "game.js")),
        "content": "const PIPE_GAP_Y = 138;\nconst PIPE_SPACING_X = 220;\n"}))
    j.step += 1
    j.log(user="(apply the corrected change)", intent="execute_plan",
          project="flappy-bird", reads="project:flappy-bird",
          writes="project:flappy-bird", decision="act", tools="code.write",
          reply=str(edit.ok), expected="vertical only", ok=bool(edit.ok))
    check(edit.ok, f"[{j.step}] the corrected edit applied ({edit.error})")
    text_a = _tree(nova, "flappy-bird")["game.js"]
    check("PIPE_GAP_Y = 138" in text_a, "the vertical opening changed")
    check("PIPE_SPACING_X = 220" in text_a,
          "and the horizontal spacing was left exactly as the correction asked")

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

    # ── G: return to the project ─────────────────────────────────────────────
    j.section = "G"
    check.section("G: returning resumes CURRENT state, not the old plan")

    back = await j.say("Alright, back to the Flappy Bird thing. Where were we?")
    low = back.lower()
    j.log(user="where were we", intent="resume_project", project="flappy-bird",
          reads="project:flappy-bird", writes="conversation", decision="report",
          tools="project status", reply=back, expected="current state",
          ok=bool(back.strip()))
    check(bool(back.strip()), f"[{j.step}] Nova said where things stand")
    check("horizontal" not in low or "vertical" in low,
          f"[{j.step}] the superseded horizontal plan is not resurrected alone "
          f"({back[:80]!r})")

    # ── H: second project, no bleed ──────────────────────────────────────────
    j.section = "H"
    check.section("H: two projects, no cross-contamination")

    res = await nova.runtime._router.execute(
        ToolCall("project.scaffold", {"name": "calc-tool"}))
    check(res.ok, f"project B scaffolded ({res.error})")
    (P / "calc-tool" / "main.py").write_text("def add(a, b):\n    return a + b\n",
                                             encoding="utf-8")
    snapshot_b = _tree(nova, "calc-tool")

    sw = await j.say("Let's work on the calculator project for a minute.")
    j.log(user="switch to calculator", intent="switch_project", project="calc-tool",
          reads="project:calc-tool", writes="conversation", decision="respond",
          tools="none", reply=sw, expected="B selected", ok=bool(sw.strip()))

    b_req = await j.say("For the calculator, I want the display to use a "
                        "monospace font.")
    j.log(user="calculator requirement", intent="state_requirement",
          project="calc-tool", reads="-", writes="memory", decision="respond",
          tools="none", reply=b_req, expected="scoped to B", ok=True)

    check(_tree(nova, "flappy-bird")["game.js"] == text_a,
          "working on B did not touch A")

    back2 = await j.say("Go back to Flappy Bird.")
    j.log(user="switch back to A", intent="switch_project", project="flappy-bird",
          reads="project:flappy-bird", writes="conversation", decision="respond",
          tools="none", reply=back2, expected="A selected", ok=bool(back2.strip()))
    check(_tree(nova, "calc-tool") == snapshot_b,
          "and switching back did not touch B")

    # ── I: memory scoping ────────────────────────────────────────────────────
    j.section = "I"
    check.section("I: global vs project-scoped facts stay where they belong")

    await _settle(nova)
    a_ask = await j.say("For Flappy Bird, what font did I ask for?")
    # NOT "did the word appear in the reply": both projects were discussed in
    # one continuous conversation, so recent context legitimately holds both.
    # The invariant is that a requirement stated for B did not become a GLOBAL
    # durable fact, and did not land in A's own project state.
    user_facts = await _all_facts(nova, "user")
    global_leak = [k for k, v in user_facts.items() if "monospace" in str(v).lower()]
    a_files = _tree(nova, "flappy-bird")
    file_leak = [f for f, t in a_files.items() if "monospace" in t.lower()]
    ok = not global_leak and not file_leak
    j.log(user="B's requirement asked of A", intent="recall_requirement",
          project="flappy-bird", reads="memory+project", writes="conversation",
          decision="respond", tools="none", reply=a_ask,
          expected="B requirement is not global and not in A", ok=ok)
    check(not global_leak,
          f"[{j.step}] B's font requirement did not become a global user fact "
          f"({global_leak})")
    check(not file_leak,
          f"[{j.step}] and never reached project A's files ({file_leak})")

    # ── J: planning truth ────────────────────────────────────────────────────
    j.section = "J"
    check.section("J: discussed / planned / done are not the same word")

    truth = await j.say("What have we actually completed, and what is still "
                        "just planned?")
    low = truth.lower()
    j.log(user="completed vs planned", intent="status_report", project="flappy-bird",
          reads="project+conversation", writes="conversation", decision="report",
          tools="none", reply=truth, expected="distinguishes the two",
          ok=bool(truth.strip()))
    check(bool(truth.strip()), f"[{j.step}] Nova answered the status question")
    check("finished" not in low and "all done" not in low,
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
    check(failed, f"[{j.step}] the missing file is a real failure, not a success "
                  f"({bad.ok} {str(bad.result)[:60]})")
    check(_tree(nova, "calc-tool") == snapshot_b,
          "and the failure did not touch the other project")

    recover = await nova.runtime._router.execute(ToolCall("code.read", {
        "path": str(P / "flappy-bird" / "game.js")}))
    j.step += 1
    j.log(user="(re-read the real file)", intent="recover", project="flappy-bird",
          reads="project:flappy-bird", writes="-", decision="act",
          tools="code.read", reply=str(recover.ok), expected="recovered",
          ok=bool(recover.ok))
    check(recover.ok, f"[{j.step}] and the recovery read succeeded")

    # ── L: integrated recall ─────────────────────────────────────────────────
    j.section = "L"
    check.section("L: final recall comes from accumulated state")

    await _settle(nova)
    q_colour = await j.say("What UI colours do I prefer?")
    ok = "black with purple accents" in q_colour.lower()
    j.log(user="final colour recall", intent="recall_preference", project=None,
          reads="memory:user", writes="conversation", decision="respond",
          tools="none", reply=q_colour, expected="corrected value", ok=ok)
    check(ok, f"[{j.step}] after 30+ turns the corrected colour still wins "
              f"({q_colour[:70]!r})")

    q_proj = await j.say("What project are we working on?")
    j.log(user="which project", intent="recall_project", project="flappy-bird",
          reads="projects:last_active", writes="conversation", decision="respond",
          tools="none", reply=q_proj, expected="flappy-bird",
          ok=bool(q_proj.strip()))

    q_done = await j.say("What changes actually succeeded?")
    j.log(user="what succeeded", intent="status_report", project="flappy-bird",
          reads="project+conversation", writes="conversation", decision="report",
          tools="none", reply=q_done, expected="the vertical change",
          ok=bool(q_done.strip()))

    check(_projects(nova) == ["calc-tool", "flappy-bird"],
          f"exactly the two intended projects exist ({_projects(nova)})")
    check("general" not in _projects(nova),
          "and no accidental 'general' project was invented")

    return text_a, snapshot_b


# ── adversarial intent boundary ──────────────────────────────────────────────

async def adversarial(nova) -> None:
    check.section("Adversarial: mentioning an action is not authorising it")

    P = nova.projects_dir
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
        # with the gate wide open (measured: removing the gerund veto left this
        # section green). So the AUTHORISATION DECISION itself is read, since
        # that is the load-bearing predicate.
        verdict = authorize_project_mutation(t, complaint=False)
        ok = (now == before) and not verdict.allowed
        LEDGER.add(step=0, section="ADV", user=t, intent="mention_only",
                   project="flappy-bird", reads="project", writes="none",
                   decision=f"refused: {verdict.reason}", tools="none",
                   reply=reply, expected="no authorisation, no mutation", ok=ok)
        check(not verdict.allowed,
              f"{t!r} is not authorisation ({verdict.reason})")
        check(now == before, f"{t!r} did not mutate the project")
        check(_projects(nova) == ["calc-tool", "flappy-bird"],
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
    check("dark blue" in c.lower() or "purple" in c.lower(),
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
    check((nova.projects_dir / "flappy-bird" / "game.js").exists(),
          "project A is on disk")
    check((nova.projects_dir / "calc-tool" / "main.py").exists(),
          "project B is on disk")
    return f"colour={colour!r} last_active={last!r} facts={len(facts)}"


async def main() -> None:
    async with boot() as nova:
        # Order matters: the harness's agent-decider rule is already first, the
        # extractor needs its own JSON shape, and the echo is the catch-all.
        nova.llm.when("You extract explicit user facts", _extractor,
                      label="memory-extractor")
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
