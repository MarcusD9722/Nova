"""Full-audit coverage for core/conversation_state.py and core/file_extract.py.

Both sit on the live path and had no tests.

ConversationState is what Nova reads back as "Recent messages:" in every reply
prompt — if it silently drops a turn, she forgets the last thing said with no
error anywhere. file_extract feeds both chat attachments and the document
indexer, and its contract ("exactly one of excerpt/error is non-None") is what
stops a failed read from being indexed as empty content.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import zipfile
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import Checks

from core.conversation_state import ConversationState, ConversationStateStore, _norm_q
from core.file_extract import (
    MAX_CHARS, TEXT_SUFFIXES, decode_text_bytes, extract_excerpt, read_limited_bytes,
)
from memory.backends.diskcache_backend import DiskCacheBackend

check = Checks()


async def test_conversation_state(tmp: Path) -> None:
    check.section("ConversationState (de/serialization)")

    st = ConversationState.from_obj(None)
    check(st.last_user_messages == [] and st.last_mode is None, "None yields an empty state")
    check(ConversationState.from_obj("not json").last_user_messages == [], "garbage string yields empty state")
    check(ConversationState.from_obj(123).last_user_messages == [], "a non-dict yields empty state")
    check(ConversationState.from_obj('{"last_mode": "task"}').last_mode == "task",
          "a JSON string is parsed")

    st = ConversationState(["u"], ["a"], ["q?"], "chat")
    check(ConversationState.from_obj(st.to_json()).last_user_messages == ["u"], "round-trips through JSON")
    check(ConversationState.from_obj({"last_user_messages": [1, 2]}).last_user_messages == ["1", "2"],
          "non-string entries are coerced, not dropped")
    big = ConversationState.from_obj({"last_user_messages": [str(i) for i in range(200)]})
    check(len(big.last_user_messages) == 50, "an oversized history is capped at 50 on load")

    check(_norm_q('  "Hello  there?" ') == "hello there?", "_norm_q trims, lowercases and collapses space")
    check(_norm_q("“Smart quotes”") == "smart quotes", "_norm_q strips smart quotes")
    check(_norm_q(None) == "", "_norm_q handles None")

    check.section("ConversationStateStore")
    store = ConversationStateStore(DiskCacheBackend(tmp / "cs"), max_turns=3, followup_window=2)
    conv = uuid4()

    check((await store.load(conv)).last_user_messages == [], "an unknown conversation loads empty")
    check(await store.recent_chat_text(conv) == "", "an unknown conversation has no chat text")

    await store.record_turn(conversation_id=conv, user_message="hello",
                            assistant_reply="hi there", follow_up_question=None, mode="chat")
    text = await store.recent_chat_text(conv)
    check("User: hello" in text and "Assistant: hi there" in text, "a turn round-trips into the prompt text")
    check((await store.load(conv)).last_mode == "chat", "mode is recorded")

    for i in range(5):
        await store.record_turn(conversation_id=conv, user_message=f"m{i}",
                                assistant_reply=f"r{i}", follow_up_question=None, mode="chat")
    st = await store.load(conv)
    check(st.last_user_messages == ["m2", "m3", "m4"], f"history trims to max_turns, newest kept ({st.last_user_messages})")
    check(st.last_assistant_replies == ["r2", "r3", "r4"], "replies trim in lockstep with messages")

    text = await store.recent_chat_text(conv)
    check(text.index("User: m2") < text.index("User: m4"), "chat text is in chronological order")
    check(text.count("User:") == 3 and text.count("Assistant:") == 3, "user and assistant lines stay paired")

    # Two conversations must never bleed into each other.
    other = uuid4()
    await store.record_turn(conversation_id=other, user_message="different",
                            assistant_reply="reply", follow_up_question=None, mode="chat")
    check("different" not in await store.recent_chat_text(conv), "conversations are isolated")
    check("m4" not in await store.recent_chat_text(other), "isolation holds both ways")

    check.section("follow-up de-duplication")
    conv2 = uuid4()
    await store.record_turn(conversation_id=conv2, user_message="u", assistant_reply="a",
                            follow_up_question="How was work?", mode="chat")
    check(await store.was_followup_recent(conversation_id=conv2, question="How was work?") is True,
          "a just-asked follow-up is recognized")
    check(await store.was_followup_recent(conversation_id=conv2, question="  how was WORK?  ") is True,
          "recognition is case/whitespace insensitive")
    check(await store.was_followup_recent(conversation_id=conv2, question="Something else?") is False,
          "an unrelated question is not flagged")
    check(await store.was_followup_recent(conversation_id=conv2, question="") is False,
          "an empty question is never 'recent'")

    for q in ("Q1?", "Q2?", "Q3?"):
        await store.record_turn(conversation_id=conv2, user_message="u", assistant_reply="a",
                                follow_up_question=q, mode="chat")
    check(await store.was_followup_recent(conversation_id=conv2, question="How was work?") is False,
          "an old follow-up falls out of the window and may be reused")

    # Empty replies must not create phantom prompt lines.
    conv3 = uuid4()
    await store.record_turn(conversation_id=conv3, user_message="  ", assistant_reply="  ",
                            follow_up_question=None, mode=None)
    check(await store.recent_chat_text(conv3) == "", "blank turns produce no prompt lines")


async def test_state_store_concurrency(tmp: Path) -> None:
    """Regression: record_turn is read-modify-write. Unguarded, 10 concurrent
    calls kept ONE — nine turns vanished from Nova's short-term memory with no
    error anywhere. Reachable via a double-send or an overlapping voice+text
    turn, since /chat and /chat/stream are separate endpoints."""
    check.section("ConversationStateStore under concurrent writes")
    store = ConversationStateStore(DiskCacheBackend(tmp / "cs2"), max_turns=20, followup_window=8)
    conv = uuid4()
    await asyncio.gather(*(
        store.record_turn(conversation_id=conv, user_message=f"m{i}", assistant_reply=f"r{i}",
                          follow_up_question=None, mode="chat")
        for i in range(10)
    ))
    st = await store.load(conv)
    kept = len(st.last_user_messages)
    check(kept == 10, f"all 10 concurrent turns survive — none lost to the race ({kept}/10)")
    check(sorted(st.last_user_messages) == sorted(f"m{i}" for i in range(10)),
          "every distinct message is present exactly once")
    check(len(st.last_user_messages) == len(st.last_assistant_replies),
          "user and assistant lists stay the same length")


async def test_file_extract(tmp: Path) -> None:
    check.section("file_extract")

    txt = tmp / "note.txt"
    txt.write_text("hello world", encoding="utf-8")
    excerpt, err = extract_excerpt(txt)
    check(excerpt == "hello world" and err is None, "a plain text file is extracted")

    empty = tmp / "empty.txt"
    empty.write_text("   \n  ", encoding="utf-8")
    excerpt, err = extract_excerpt(empty)
    check(excerpt is None and err == "file is empty", "an empty file reports an error, not empty content")

    missing = tmp / "nope.txt"
    excerpt, err = extract_excerpt(missing)
    check(excerpt is None and err is not None, f"a missing file reports an error ({err})")

    weird = tmp / "thing.xyz"
    weird.write_text("data", encoding="utf-8")
    excerpt, err = extract_excerpt(weird)
    check(excerpt is None and "unsupported file type" in (err or ""), "an unsupported suffix is refused honestly")
    excerpt, err = extract_excerpt(weird, content_type="text/plain")
    check(excerpt == "data", "an explicit text/* content type overrides the suffix check")

    big = tmp / "big.txt"
    big.write_text("x" * (MAX_CHARS + 5000), encoding="utf-8")
    excerpt, err = extract_excerpt(big)
    check(excerpt.endswith("[truncated]"), "an oversized text file is truncated with a marker")
    check(len(excerpt) <= MAX_CHARS + 20, "truncation actually bounds the length")

    # The contract every caller relies on.
    for p in (txt, empty, missing, weird, big):
        e, r = extract_excerpt(p)
        check((e is None) != (r is None), f"exactly one of (excerpt, error) is set for {p.name}")

    check.section("file_extract — encodings")
    utf16 = tmp / "u16.txt"
    utf16.write_bytes("café au lait".encode("utf-16"))
    excerpt, err = extract_excerpt(utf16)
    check(excerpt is not None and "caf" in excerpt, f"a UTF-16 file decodes ({(excerpt or '')[:20]!r})")

    # A UTF-8 BOM must not survive into the excerpt: it would be indexed and
    # fed to the model as a stray ﻿ at the head of every such document.
    bom = tmp / "bom.txt"
    bom.write_bytes(b"\xef\xbb\xbfhello bom")
    excerpt, err = extract_excerpt(bom)
    check(excerpt == "hello bom", f"a UTF-8 BOM is stripped, not kept ({excerpt!r})")

    check(decode_text_bytes(b"plain") == "plain", "decode_text_bytes handles ascii")
    check(decode_text_bytes(b"") == "", "decode_text_bytes handles empty input")

    data, truncated = read_limited_bytes(big, max_bytes=10)
    check(len(data) == 10 and truncated is True, "read_limited_bytes caps and reports truncation")
    data, truncated = read_limited_bytes(txt, max_bytes=10_000)
    check(truncated is False, "a small file is not reported as truncated")

    check.section("file_extract — docx")
    docx = tmp / "doc.docx"
    with zipfile.ZipFile(docx, "w") as z:
        z.writestr("word/document.xml",
                   '<?xml version="1.0"?><w:document xmlns:w="urn:x"><w:body>'
                   '<w:p><w:r><w:t>First line</w:t></w:r></w:p>'
                   '<w:p><w:r><w:t>Second line</w:t></w:r></w:p>'
                   "</w:body></w:document>")
    excerpt, err = extract_excerpt(docx)
    check(err is None and "First line" in excerpt and "Second line" in excerpt, "a .docx extracts its paragraphs")

    notzip = tmp / "fake.docx"
    notzip.write_text("this is not a zip", encoding="utf-8")
    excerpt, err = extract_excerpt(notzip)
    check(excerpt is None and err == "invalid .docx file", "a non-zip .docx is reported honestly")

    nodoc = tmp / "nodoc.docx"
    with zipfile.ZipFile(nodoc, "w") as z:
        z.writestr("other.xml", "<x/>")
    excerpt, err = extract_excerpt(nodoc)
    check(excerpt is None and err == "missing document content", "a .docx without document.xml is reported honestly")

    check.section("file_extract — pdf")
    badpdf = tmp / "bad.pdf"
    badpdf.write_text("not really a pdf", encoding="utf-8")
    excerpt, err = extract_excerpt(badpdf)
    check(excerpt is None and err is not None, f"an invalid PDF reports an error ({err})")

    check(".py" in TEXT_SUFFIXES and ".md" in TEXT_SUFFIXES, "code and markdown count as text")


async def main() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tmp = Path(td)
        await test_conversation_state(tmp)
        await test_state_store_concurrency(tmp)
        await test_file_extract(tmp)
    check.finish()


asyncio.run(main())
