"""The approval endpoint is the last link in the permission chain.

Every tier, every confirmation prompt and every audit line in
`core/permissions.py` exists so that a person decides. The thing that carries
that decision is one HTTP call — `POST /permissions/resolve` — and on a default
config (no NOVA_API_TOKEN) it answered anyone who could reach the port.

What this pins:

  * a request from an origin Nova does not serve is REFUSED, before the broker
    is consulted at all. CORS is not enough on its own: it stops a disallowed
    origin READING a response, not the request EXECUTING;
  * the clients that exist still work — no Origin at all (curl, the CLI, a
    local process) and the `null` origin that Electron and a file:// harness
    send both reach the handler;
  * the boot warning the middleware has promised in a comment since it was
    written now actually exists, and says which endpoint is exposed.

Run:  venv\\Scripts\\python.exe tests\\test_api_hardening.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("NOVA_LOG_LEVEL", "ERROR")

from harness import Checks, boot, run  # noqa: E402

check = Checks()

#: A request id that was never issued. A handler that REACHES the broker says
#: so ("resolved": false); one that refuses first never gets there.
UNKNOWN_ID = "0000deadbeef"


async def test_a_foreign_origin_cannot_approve() -> None:
    check.section("E1 an approval from an origin Nova does not serve is refused")
    async with boot(default_reply="Sure.") as nova:
        body = {"request_id": UNKNOWN_ID, "approved": True}

        r = await nova.http.post("/permissions/resolve", json=body,
                                 headers={"Origin": "https://evil.example"})
        check(r.status_code == 403,
              f"a foreign origin is refused ({r.status_code})")
        check("origin" in r.text.lower(),
              f"and told why ({r.text[:90]})")

        # The legitimate callers, unchanged.
        r = await nova.http.post("/permissions/resolve", json=body)
        check(r.status_code == 200,
              f"no Origin at all still reaches the handler ({r.status_code})")
        check(r.json().get("resolved") is False,
              f"and the broker answered it ({r.json()})")

        r = await nova.http.post("/permissions/resolve", json=body,
                                 headers={"Origin": "null"})
        check(r.status_code == 200,
              f"`null` — Electron and the file:// harness — still works "
              f"({r.status_code})")

        r = await nova.http.post("/permissions/resolve", json=body,
                                 headers={"Origin": "http://localhost:5173"})
        check(r.status_code == 200,
              f"and so does the Vite dev server ({r.status_code})")


async def test_a_foreign_origin_cannot_approve_a_real_request() -> None:
    check.section("E2 refusal happens before the broker, on a REAL pending ask")
    async with boot(default_reply="Sure.") as nova:
        broker = nova.runtime.permission_broker
        raised = await broker.request("project.delete",
                                      details={"project": "alpha",
                                               "name": "alpha"})
        rid = str(raised.get("request_id") or "")
        check(raised.get("decision") == "needs_confirmation" and rid,
              f"a real request is pending a human ({raised.get('decision')})")

        r = await nova.http.post("/permissions/resolve",
                                 json={"request_id": rid, "approved": True},
                                 headers={"Origin": "https://evil.example"})
        check(r.status_code == 403,
              f"the foreign approval is refused ({r.status_code})")
        # The ask is STILL waiting: nothing was approved, nothing was settled.
        pend = [str(p["request_id"]) for p in broker.pending()]
        check(pend == [rid],
              f"and the request is still waiting for a person ({pend})")
        check(broker.settled_as(rid) == "",
              f"with nothing recorded against it ({broker.settled_as(rid)!r})")

        # LIVENESS: the same call from a legitimate caller does resolve it, so
        # the refusal above is the Origin check and not a broken endpoint.
        r = await nova.http.post("/permissions/resolve",
                                 json={"request_id": rid, "approved": False},
                                 headers={"Origin": "null"})
        check(r.status_code == 200 and r.json().get("resolved") is True,
              f"a legitimate caller resolves it normally ({r.json()})")
        check(broker.settled_as(rid) == "rejected",
              f"and the decision is recorded ({broker.settled_as(rid)})")
        check(broker.pending() == [],
              f"with nothing left pending ({broker.pending()})")


async def test_the_promised_boot_warning_exists() -> None:
    check.section("E3 an unauthenticated port says so at boot")
    from backend.app import api_exposure_note

    saved = os.environ.get("NOVA_API_TOKEN")
    try:
        os.environ["NOVA_API_TOKEN"] = ""
        note = api_exposure_note()
        check(note is not None, "with no token set, there IS a warning")
        check("NOVA_API_TOKEN" in (note or ""),
              f"naming the setting that closes it ({str(note)[:60]})")
        check("/permissions/resolve" in (note or ""),
              f"and the endpoint that matters ({str(note)[:90]})")

        os.environ["NOVA_API_TOKEN"] = "a-real-token"
        check(api_exposure_note() is None,
              f"with a token set, there is nothing to warn about "
              f"({api_exposure_note()})")
    finally:
        if saved is None:
            os.environ.pop("NOVA_API_TOKEN", None)
        else:
            os.environ["NOVA_API_TOKEN"] = saved


async def test_cors_is_not_credentialed() -> None:
    check.section("E4 CORS does not advertise credentials it does not use")
    import backend.app as app_module

    cors = [m for m in app_module.app.user_middleware
            if "CORS" in str(m)]
    check(bool(cors), f"the CORS middleware is installed ({len(cors)})")
    opts = getattr(cors[0], "kwargs", {}) or {}
    check(opts.get("allow_credentials") is False,
          f"and credentialed CORS is off ({opts.get('allow_credentials')})")
    check("null" in (opts.get("allow_origins") or []),
          f"while the origin Electron needs is still allowed "
          f"({opts.get('allow_origins')})")


async def main() -> None:
    await test_a_foreign_origin_cannot_approve()
    await test_a_foreign_origin_cannot_approve_a_real_request()
    await test_the_promised_boot_warning_exists()
    await test_cors_is_not_credentialed()
    check.finish()


if __name__ == "__main__":
    run(main)
