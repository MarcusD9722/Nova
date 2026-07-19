"""WS2 verification: self.* tools registered, dev-mode gated, functional."""
import asyncio, os, sys, tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from core.tooling import build_tool_router  # noqa: E402
from core.tool_router import ToolCall  # noqa: E402
from memory.unifier import MemoryUnifier  # noqa: E402

fails = []
def check(c, m):
    print(("  OK  " if c else " FAIL ") + m)
    if not c: fails.append(m)

async def main():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        mem = MemoryUnifier(Path(td) / "mem", enable_chroma=False)
        # Build router WITH dev mode OFF first
        os.environ["NOVA_DEV_MODE"] = "0"
        router = build_tool_router(repo_root=REPO, projects_dir=REPO / "projects", memory=mem)

        names = set(router.list_tools())
        check({"self.list_code", "self.read_code", "self.propose_change"} <= names, "self.* tools registered")
        check(getattr(router, "dev_mode", None) is not None, "router.dev_mode attached")

        # Gated: with dev mode OFF, tools return an error, not data
        r = await router.execute(ToolCall(name="self.read_code", args={"path": "core/dev_mode.py"}), timeout_s=15, retries=0)
        gated = r.ok and isinstance(r.result, dict) and not r.result.get("ok") and "disabled" in str(r.result.get("error", "")).lower()
        check(gated, "self.read_code refused when dev mode disabled")

        # Now enable dev mode and re-test (same instance honors env at call time)
        os.environ["NOVA_DEV_MODE"] = "1"
        r2 = await router.execute(ToolCall(name="self.read_code", args={"path": "core/runtime.py"}), timeout_s=15, retries=0)
        ok_read = r2.ok and r2.result.get("ok") and "chat_turn_stream" in r2.result.get("content", "")
        check(ok_read, "self.read_code returns her real source when enabled")

        r3 = await router.execute(ToolCall(name="self.list_code", args={"subdir": "core", "limit": 20}), timeout_s=15, retries=0)
        ok_list = r3.ok and r3.result.get("ok") and any("dev_mode.py" in str(f.get("path")) for f in r3.result.get("files", []))
        check(ok_list, "self.list_code lists her core files")

        # propose_change on a throwaway file, then confirm it appears in the shared store
        sbx = REPO / "_ws2_sbx_tmp.py"
        sbx.write_text("x = 1\n", encoding="utf-8")
        try:
            r4 = await router.execute(ToolCall(name="self.propose_change",
                    args={"path": str(sbx), "new_content": "x = 2\n", "reason": "ws2 test"}), timeout_s=15, retries=0)
            ok_prop = r4.ok and r4.result.get("ok") and r4.result.get("proposal_id") and "NOT applied" in r4.result.get("note", "")
            check(ok_prop, "self.propose_change creates a proposal (not auto-applied)")
            check(sbx.read_text().strip() == "x = 1", "file NOT modified by proposing (proposal-only)")
            # shared store: same dev_mode sees it
            pid = r4.result.get("proposal_id")
            seen = any(p["id"] == pid for p in router.dev_mode.list_proposals())
            check(seen, "proposal visible in the shared dev_mode store")
            # SECURITY via tool: proposing to .env is refused
            r5 = await router.execute(ToolCall(name="self.propose_change",
                    args={"path": str(REPO / ".env"), "new_content": "SECRET=x", "reason": "bad"}), timeout_s=15, retries=0)
            denied = r5.ok and not r5.result.get("ok")
            check(denied, "self.propose_change refuses .env")
        finally:
            sbx.unlink(missing_ok=True)
            # clean the ws2 proposal
            for jf in (REPO / ".nova_dev" / "proposals").glob("*.json"):
                try:
                    import json
                    if "_ws2_sbx_tmp" in json.loads(jf.read_text()).get("path", ""):
                        jf.unlink()
                except Exception:
                    pass

    print("\nRESULT:", "ALL PASS" if not fails else f"{len(fails)} FAILURES")
    return 1 if fails else 0

sys.exit(asyncio.run(main()))
