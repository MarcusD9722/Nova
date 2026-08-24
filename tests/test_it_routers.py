"""INTEGRATION: backend/routers/memory_api.py and web_maps.py (no coverage before).

These two routers carry 27 of the backend's endpoints — everything the UI's
Memory, Tasks, Graph and Maps panels read — and nothing exercised them. Driven
through the real booted backend (tests/harness.py) rather than mocked, so the
router, STATE wiring, query validation and the memory layer underneath are all
the real thing.

Endpoints that reach a third party (web search/fetch, maps) are exercised with
credentials blanked by the harness: the point is that they fail HONESTLY with a
4xx/5xx rather than returning empty results that read as "there was nothing
there".
"""
from __future__ import annotations

import sys
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import Checks, boot, run

check = Checks()


async def main() -> None:
    async with boot(default_reply="Noted.") as nova:
        http = nova.http

        check.section("Memory read endpoints")
        # Seed through the real memory layer so the endpoints read real rows.
        await nova.memory.add_fact(entity="user", attribute="name", value="Marcus", confidence=0.95)
        await nova.memory.add_fact(entity="note", attribute="coffee", value="likes oat milk", confidence=0.8)
        await nova.memory.upsert_person(name="Leslie", attributes={"relation": "spouse"})
        conv = uuid4()
        await nova.memory.ingest_turn(conv, "user", "we talked about the pillow fort with Leslie")

        r = await http.get("/memory/recent?limit=10")
        check(r.status_code == 200, f"GET /memory/recent -> {r.status_code}")
        check(isinstance(r.json().get("items", r.json().get("recent")), list) or bool(r.json()),
              "recent returns a payload")

        r = await http.get("/memory/search?q=oat milk")
        check(r.status_code == 200, f"GET /memory/search -> {r.status_code}")
        check("oat milk" in str(r.json()), "search finds the seeded fact")

        r = await http.get("/memory/search")
        check(r.status_code == 422, f"search with no q is a validation error, not a 500 ({r.status_code})")

        r = await http.get("/memory/lessons?limit=5")
        check(r.status_code == 200, f"GET /memory/lessons -> {r.status_code}")

        check.section("Knowledge graph endpoints")
        r = await http.get("/memory/graph/stats")
        check(r.status_code == 200, f"GET /memory/graph/stats -> {r.status_code}")
        check(isinstance(r.json(), dict), "graph stats returns an object")

        r = await http.get("/memory/graph?key=leslie&limit=5")
        check(r.status_code == 200, f"GET /memory/graph -> {r.status_code}")

        r = await http.get("/memory/graph?key=")
        check(r.status_code == 422, "an empty graph key is rejected by validation")

        r = await http.get("/memory/graph/subgraph?key=leslie&depth=2")
        check(r.status_code == 200, f"GET /memory/graph/subgraph -> {r.status_code}")
        r = await http.get("/memory/graph/subgraph?key=leslie&depth=9")
        check(r.status_code == 422, "an out-of-range depth is rejected (bounded traversal)")

        r = await http.get("/memory/path?from=marcus&to=leslie")
        check(r.status_code == 200, f"GET /memory/path -> {r.status_code}")

        r = await http.get("/memory/timeline?days=7")
        check(r.status_code == 200, f"GET /memory/timeline -> {r.status_code}")
        r = await http.get("/memory/timeline?days=999")
        check(r.status_code == 422, "an unbounded timeline window is rejected")

        check.section("World model / thoughts / twin / executive")
        for path in ("/memory/world", "/thoughts", "/twin", "/executive", "/research",
                     "/skills", "/experiments", "/agents", "/tasks"):
            r = await http.get(path)
            check(r.status_code == 200, f"GET {path} -> {r.status_code}")

        r = await http.get("/tasks?status=nonexistent-status")
        check(r.status_code == 200, "an unknown task status filter returns empty, not an error")
        r = await http.get("/tasks?limit=9999")
        check(r.status_code == 422, "an over-large task limit is rejected")

        check.section("Answering a task that is waiting on a person")
        # A background task that asked a question used to be recorded `done`,
        # so there was nothing to answer and no endpoint to answer it with.
        tid = str(await nova.memory.enqueue_task(
            title="add a pause menu", details="step one",
            project_name="flappy-bird", initiated_by_user=True))
        claimed = await nova.memory.claim_next_task()
        check(claimed is not None and str(claimed.get("task_id")) == tid,
              f"the task was claimed ({str((claimed or {}).get('task_id'))[:8]})")
        parked = await nova.memory.mark_task_blocked(
            task_id=tid, question="Should the pause menu darken the screen?")
        check(parked is True, f"and parked as waiting ({parked})")

        r = await http.get("/tasks?status=blocked")
        listed = [t for t in r.json().get("tasks", [])
                  if str(t.get("task_id")) == tid]
        check(r.status_code == 200 and len(listed) == 1,
              f"GET /tasks?status=blocked lists it ({r.status_code}, {len(listed)})")

        r = await http.post(f"/tasks/{tid}/answer", json={"answer": "yes, darken it"})
        check(r.status_code == 200, f"POST /tasks/id/answer -> {r.status_code}")
        row = [t for t in (await http.get("/tasks?limit=50")).json().get("tasks", [])
               if str(t.get("task_id")) == tid]
        check(bool(row) and str(row[0].get("status")) == "queued",
              f"the task is runnable again ({(row or [{}])[0].get('status')!r})")
        check(bool(row) and "yes, darken it" in str(row[0].get("details") or ""),
              "and the answer is where the next plan will read it")

        r = await http.post(f"/tasks/{tid}/answer", json={"answer": "again"})
        check(r.status_code == 409,
              f"answering something that is not waiting is refused ({r.status_code})")
        r = await http.post(f"/tasks/{tid}/answer", json={"answer": "   "})
        check(r.status_code in (409, 422),
              f"an empty answer does not release it ({r.status_code})")

        check.section("Reminders CRUD (real scheduling)")
        r = await http.post("/reminders", json={"title": "Call the dentist", "when": "in 30 minutes"})
        check(r.status_code == 200, f"POST /reminders -> {r.status_code}")
        body = r.json()
        rid = body.get("reminder_id")
        check(bool(rid), "a reminder id is returned")
        check(bool(body.get("due_at")), f"a concrete due time was parsed ({body.get('due_at')})")

        r = await http.get("/reminders")
        check(r.status_code == 200 and "Call the dentist" in str(r.json()), "the reminder is listed")

        r = await http.post("/reminders", json={"title": "x", "when": "sometime maybe never"})
        check(r.status_code == 422, "an unparseable time is refused honestly, not silently dropped")
        r = await http.post("/reminders", json={"title": "  ", "when": "5pm"})
        check(r.status_code == 422, "a blank title is refused")

        r = await http.delete(f"/reminders/{rid}")
        check(r.status_code == 200, f"DELETE /reminders/{{id}} -> {r.status_code}")
        listed = str((await http.get("/reminders?status=scheduled")).json())
        check("Call the dentist" not in listed, "a cancelled reminder leaves the scheduled list")

        check.section("Memory purge is admin-gated (fails CLOSED)")
        # Regression: the guard returned silently when NOVA_ADMIN_TOKEN was
        # unset, so a real delete answered 200 with no authentication at all.
        # (dry_run defaults to True, so it took an explicit dry_run:false —
        # the endpoint was never one-stray-POST-wipes-everything.)
        before = len(await nova.memory.get_facts(entity="note"))
        r = await http.post("/memory/purge", json={"entity": "note", "dry_run": False})
        check(r.status_code == 403, f"a destructive purge with no token configured is refused ({r.status_code})")
        check("NOVA_ADMIN_TOKEN" in str(r.json()), "the refusal says exactly what to configure")
        after = len(await nova.memory.get_facts(entity="note"))
        check(after == before, f"nothing was actually deleted ({before} -> {after} facts)")

        # A dry run only reports, so it stays usable for inspection.
        r = await http.post("/memory/purge", json={"entity": "note", "dry_run": True})
        check(r.status_code == 200, f"a dry run is still allowed without a token ({r.status_code})")
        check(len(await nova.memory.get_facts(entity="note")) == before, "the dry run deleted nothing")

        check.section("Plugin execution endpoint")
        r = await http.post("/plugins/execute", json={"name": "system.time", "args": {}})
        check(r.status_code == 200, f"POST /plugins/execute (system.time) -> {r.status_code}")
        check("unix_timestamp" in str(r.json()), "the real tool result comes back")

        r = await http.post("/plugins/execute", json={"name": "no.such.tool", "args": {}})
        check(r.status_code >= 400 or "error" in str(r.json()).lower(),
              f"an unknown tool is reported as an error, not a silent success ({r.status_code})")

        check.section("Maps endpoints fail honestly with no API key")
        r = await http.get("/api/maps/key")
        check(r.status_code == 400, f"the key endpoint says it is unconfigured ({r.status_code})")
        check("GOOGLE_MAPS_API_KEY" in str(r.json()), "and names the missing key")

        for path in ("/api/maps/geocode?address=Austin",
                     "/api/maps/directions?origin=A&destination=B",
                     "/api/maps/nearby?query=coffee&lat=30.2&lng=-97.7"):
            r = await http.get(path)
            check(r.status_code >= 400, f"{path.split('?')[0]} errors rather than faking a result ({r.status_code})")

        check.section("Web endpoints validate their input")
        r = await http.get("/api/web/fetch?url=notaurl")
        check(r.status_code >= 400, f"a scheme-less URL is rejected ({r.status_code})")
        r = await http.get("/api/web/search")
        check(r.status_code == 422, "search with no query is a validation error")

    check.finish()


run(main)
