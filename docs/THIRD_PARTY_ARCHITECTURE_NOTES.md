# Third-party architecture notes

What Nova looked at, what it took, and under which licence. Written so that a
future licensing question has a factual answer rather than a reconstruction.

Licences were checked live via the GitHub API at the time of the work, not
assumed from documentation.

---

## Summary

| Repository | HEAD inspected | Licence | Source reused in Nova? |
|---|---|---|---|
| `InterGenJLU/jarvis` | `39acdf6346f6c8497c3b368a6fdecef00fd6405b` | MIT | **No** — concepts only |
| `isair/jarvis` | `d22ed8b975792842dc09e49861f31a39cbb302a6` | Custom "Jarvis AI Assistant License" | **No** — deliberately excluded |
| `nazirlouis/ada_v2` | `d005af742fc5c604074b8b92bd9a223d7fca7447` | MIT | **No** — concepts only, future work |

**No source code from any of these repositories was copied into Nova.** Every
module written in this round is original work against Nova's own architecture.
There is therefore no third-party copyright notice to carry, and Nova's
licensing position is unchanged.

---

## `isair/jarvis` — why it is excluded, not merely credited

This is the one that required a decision rather than a note.

Its LICENSE is not a standard non-commercial licence. It is a custom "Jarvis AI
Assistant License" with two operative terms:

1. **Non-commercial.** "Commercial use of the Software requires a separate
   commercial license from the copyright holder."
2. **Share-alike.** "Any derivative works are also licensed under these same
   terms."

Term 1 alone could be worked around by not shipping the code commercially.
Term 2 cannot. A share-alike clause means that anything qualifying as a
derivative work carries the same licence forward — so a Nova module that was
recognisably derived from that source could pull Nova's own licensing toward
terms Nova cannot accept, given the stated requirement that Nova remain
structurally capable of future commercial use.

**Decision:** no source from `isair/jarvis` enters Nova, and inspection was
deliberately limited to the repository's public description and
architecture-level concepts rather than a close reading of implementation files.
This is a stricter position than copyright law strictly requires — general
engineering ideas are not protectable, only their expression — but the margin is
cheap and the downside is not.

The ideas below are general and were implemented from first principles against
Nova's own data structures. They are noted for intellectual honesty about where
the *problem framing* came from, not as an admission of derivation:

| Concept | Nova's implementation | Relationship |
|---|---|---|
| Gate expensive recall behind a cheap check | `memory/recall_gate.py` | Independent. Nova's gate is a pure-string, fail-open decision over its own `WorkingContext`, with an explicit asymmetry argument (a wrong skip loses memory, a wrong recall costs milliseconds) and an AST test proving it cannot reach a model. |
| Distinguish assistant echo from user speech | `core/voice/echo.py` | Independent. Nova's version is a three-way verdict (ECHO / USER / MIXED) with token-level prefix alignment against `TurnRegistry.recent_spoken()`, tuned to salvage the user's real suffix. |
| Preselect tools instead of prompting with all of them | `core/tools/selector.py` | Independent. Nova's is a three-tier selector over its existing `ToolRouter.describe_tools()`, with content-hash-keyed vector caching and mean-centred cosine scoring. |
| Keep a small hot window of live conversational state | `memory/working_context.py` | Independent. Deliberately not a vector store; a bounded per-conversation record. |

---

## `InterGenJLU/jarvis` — MIT, concepts adopted, no code copied

MIT would have permitted source reuse with a preserved notice. None was needed:
the useful part was the *shape* of the idea, and Nova's storage and event
architecture are different enough that copying would have cost more than it
saved.

| Concept | Nova's implementation | Notes |
|---|---|---|
| Interaction artifact cache | `memory/artifacts.py` | Nova's artifacts are addressable result sets with 1-based child positions, trust classes and freshness classes. Deliberately **not** a second database: SQLite remains authoritative, artifacts live hot in memory, and only compact summaries persist through the normal fact path. |
| Persistent event-driven voice workers | `services/tts_worker.py`, `services/tts_client.py` | Nova's worker exists for a different reason — CUDA context isolation from llama.cpp — and its microphone path is Electron/browser, so InterGen's worker model was not transplanted. |
| Streaming audio engineering | `core/voice/chunker.py` | Nova already had sentence-streamed TTS. This round improved the splitter rather than replacing the stream. |

Because no MIT-licensed source was copied, no `LICENSE` fragment or copyright
notice from InterGenJLU is required in Nova. If any file is later copied
verbatim, its MIT notice must be preserved and recorded here.

---

## `nazirlouis/ada_v2` — MIT, future work

Inspected for CAD, printer and spatial-UI patterns. Nothing implemented this
round. See `docs/FUTURE_CAD_INTEGRATION.md` for how a CAD capability would sit
on the artifact model built here.

MIT, so source reuse is available later if it turns out to be worthwhile; that
would require a notice and an entry in this file.

---

## Rules for future rounds

1. Check the licence at the current HEAD before reading implementation files
   with intent to reuse. Licences change.
2. `isair/jarvis`: architecture-level inspection only, no source reuse, unless
   the licence changes or a separate commercial licence is obtained.
3. Any verbatim or lightly-adapted MIT source must (a) preserve its copyright
   and licence notice in-file and (b) be listed in the table above with the file
   path and upstream SHA.
4. When in doubt, reimplement. Nova's architecture differs enough that a native
   implementation is usually shorter than an adaptation anyway.
