"""Offline verification of memory supersession/dedup + project builder imports."""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))


async def main() -> int:
    # Import checks (would catch syntax errors in every edited module)
    import core.project_builder  # noqa: F401
    import core.tooling  # noqa: F401
    import core.runtime  # noqa: F401
    import core.policy.memory_extractor  # noqa: F401
    from memory.unifier import MemoryUnifier
    print("imports OK")

    failures: list[str] = []
    # diskcache keeps cache.db open on Windows — don't fail on cleanup
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        mem = MemoryUnifier(Path(td), enable_chroma=False)
        await mem.initialize()

        # 1) Singleton supersession: location corrected -> only newest survives
        await mem.add_fact(entity="user", attribute="location", value="Dallas", confidence=0.9)
        await mem.add_fact(entity="user", attribute="location", value="Austin", confidence=0.9)
        rows = await mem.get_facts(entity="user", attribute="location", limit=10)
        if len(rows) == 1 and rows[0].value == "Austin":
            print("supersession (user.location) OK")
        else:
            failures.append(f"user.location rows={[(r.value) for r in rows]}")

        # 2) Project status: three writes -> one current row
        for s in ("building", "complete", "needs attention"):
            await mem.add_fact(entity="project:snake", attribute="status", value=s, confidence=0.95)
        rows = await mem.get_facts(entity="project:snake", attribute="status", limit=10)
        if len(rows) == 1 and rows[0].value == "needs attention":
            print("supersession (project status) OK")
        else:
            failures.append(f"project status rows={[(r.value) for r in rows]}")

        # 3) List attr dedup: same child twice -> one row; different child -> two
        await mem.add_fact(entity="user", attribute="child", value="Emma", confidence=0.8)
        await mem.add_fact(entity="user", attribute="child", value="Emma", confidence=0.8)
        await mem.add_fact(entity="user", attribute="child", value="Liam", confidence=0.8)
        rows = await mem.get_facts(entity="user", attribute="child", limit=10)
        vals = sorted(r.value for r in rows)
        if vals == ["Emma", "Liam"]:
            print("list dedup (user.child) OK")
        else:
            failures.append(f"child rows={vals}")

        # 4) Conversation summary supersession
        await mem.add_fact(entity="conversation:abc", attribute="summary", value="old", confidence=0.75)
        await mem.add_fact(entity="conversation:abc", attribute="summary", value="new", confidence=0.75)
        rows = await mem.get_facts(entity="conversation:abc", attribute="summary", limit=10)
        if len(rows) == 1 and rows[0].value == "new":
            print("supersession (conversation summary) OK")
        else:
            failures.append(f"summary rows={[(r.value) for r in rows]}")

        # 5) Search cache invalidation: fact added after a search is visible now
        hits0 = await mem.search(q="favorite color preference", limit=10)
        await mem.add_fact(entity="note", attribute="favorite_color", value="Marcus's favorite color is teal", confidence=0.9)
        hits1 = await mem.search(q="favorite color preference", limit=10)
        found = any("teal" in h.text for h in hits1)
        if found:
            print(f"search cache invalidation OK (before={len(hits0)} hits, after finds teal)")
        else:
            failures.append(f"teal not found; hits={[h.text for h in hits1]}")

        # 6) purge still works and denylist guard intact
        res = await mem.purge_facts(entity="user", attribute="child", dry_run=False)
        if res["deleted"] == 2:
            print("purge OK")
        else:
            failures.append(f"purge={res}")

    # 7) Project builder helpers
    from core.project_builder import ProjectBuilder, _python_stack_note, _MISSING_MODULE_RE, _FILE_TOKENS
    note = _python_stack_note()
    assert "tkinter" in note, note  # pygame absent on this machine
    m = _MISSING_MODULE_RE.search("Traceback...\nModuleNotFoundError: No module named 'pygame'")
    assert m and m.group(1) == "pygame"
    assert _FILE_TOKENS == 3000
    assert ProjectBuilder._looks_like_failed_generation("", "real content " * 20) is True
    print(f"project builder helpers OK (stack note: {note[:60]}...)")

    # 8) memory tools registered
    from core.tooling import build_tool_router
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td2:
        mem2 = MemoryUnifier(Path(td2), enable_chroma=False)
        router = build_tool_router(repo_root=Path(td2), projects_dir=Path(td2), memory=mem2)
        names = router.list_tools()
        assert "memory.remember" in names and "memory.recall" in names, names
        from core.tool_router import ToolCall
        r1 = await router.execute(ToolCall(name="memory.remember", args={"fact": "the wifi password is on the fridge", "topic": "WiFi Password"}), timeout_s=10, retries=0)
        assert r1.ok and r1.result["ok"], r1
        r2 = await router.execute(ToolCall(name="memory.recall", args={"query": "wifi password"}), timeout_s=10, retries=0)
        assert r2.ok and any("fridge" in t for t in r2.result["results"]), r2.result
        print("memory.remember / memory.recall OK")

    if failures:
        print("FAILURES:")
        for f in failures:
            print(" -", f)
        return 1
    print("ALL OFFLINE CHECKS PASSED")
    return 0


sys.exit(asyncio.run(main()))
