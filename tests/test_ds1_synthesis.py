import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from memory.unifier import MemoryUnifier

_fail = False


def check(cond, label):
    global _fail
    status = "OK  " if cond else "FAIL"
    if not cond:
        _fail = True
    print(f"  {status} {label}")


async def main():
    # _chunk_text unit checks
    short = MemoryUnifier._chunk_text("hello world")
    check(short == ["hello world"], f"short text -> single chunk (got {short})")

    long_text = "A" * 2500
    chunks = MemoryUnifier._chunk_text(long_text, chunk_size=1000, overlap=150)
    check(len(chunks) == 3, f"2500 chars @1000/150 overlap -> 3 chunks (got {len(chunks)})")
    check(all(len(c) <= 1000 for c in chunks), "no chunk exceeds chunk_size")
    # overlap sanity: end of chunk 0 should reappear at start of chunk 1
    check(chunks[0][-150:] == chunks[1][:150], "consecutive chunks overlap by the configured amount")

    empty = MemoryUnifier._chunk_text("")
    check(empty == [], "empty text -> no chunks")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        mem = MemoryUnifier(Path(td), enable_chroma=False)
        await mem.initialize()

        # Index two files with distinct content, each long enough to chunk.
        doc_a = str(Path(td) / "notes_a.txt")
        doc_b = str(Path(td) / "notes_b.txt")
        text_a = ("The garden project uses raised cedar beds. " * 40) + "Watering schedule is every other morning."
        text_b = ("The garden project also needs a drip irrigation line. " * 40) + "Budget is around $300."
        await mem.index_document(path=doc_a, excerpt=text_a, mtime=1.0)
        await mem.index_document(path=doc_b, excerpt=text_b, mtime=1.0)

        # document_chunks table populated
        count_a = await mem._sqlite.document_chunk_count(doc_a)
        check(count_a >= 2, f"long doc_a produced multiple chunks (got {count_a})")

        # Broad synthesis search should surface chunks from BOTH files for a shared topic
        results = await mem.search_document_chunks_broad("garden project budget watering", limit=20)
        paths = {r["path"] for r in results}
        check(doc_a in paths and doc_b in paths, f"synthesis search pulls from both files (got paths={paths})")

        # Re-indexing with SHORTER content should shrink chunk count (stale chunk cleanup)
        await mem.index_document(path=doc_a, excerpt="short update.", mtime=2.0)
        count_a_after = await mem._sqlite.document_chunk_count(doc_a)
        check(count_a_after == 1, f"re-indexing with shorter text shrinks chunk count (got {count_a_after})")

        # General search() should still return at most ONE hit per document path
        # (the per-file cap protecting memory.recall from chunk-crowding).
        general_hits = await mem.search(q="garden project", limit=20)
        doc_hit_paths = [h.provenance.get("path") for h in general_hits if h.kind == "document"]
        check(len(doc_hit_paths) == len(set(doc_hit_paths)), f"search() dedups to <=1 hit per document path (got {doc_hit_paths})")

    print("\nRESULT:", "FAILURES" if _fail else "ALL PASS")
    sys.exit(1 if _fail else 0)


asyncio.run(main())
