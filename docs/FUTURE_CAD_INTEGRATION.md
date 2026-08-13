# Future CAD integration

How a CAD capability would sit on the architecture built in the JARVIS V2 round.
**Nothing here is implemented.** This document exists so the artifact and voice
work does not have to be redone when CAD arrives.

Reference: `nazirlouis/ada_v2` at `d005af742fc5c604074b8b92bd9a223d7fca7447`
(MIT — source reuse permitted with a preserved notice; see
`docs/THIRD_PARTY_ARCHITECTURE_NOTES.md`). ADA's backend runs a CAD agent that
writes to project-specific output directories with timestamped filenames, plus a
printer agent handling slicer detection and print orchestration.

---

## The shape of the fit

A CAD model is exactly the thing `memory/artifacts.py` was built to hold: a
concrete object produced by a turn, referenced by later turns, with a position
in a set, provenance, and a meaningful notion of freshness.

```
User: "Design me a bracket for the server rack, 3 mm steel."
  → tool selector surfaces cad.* tools
  → cad.design runs, produces parametric source + STEP + STL + a preview render
  → an ARTIFACT of type `cad_model` is recorded against the turn
  → Nova speaks a summary; the viewer opens via an SSE `action` event

User: "Make the second mounting hole 2 mm wider."
  → ordinal resolution picks hole #2 from the child artifacts   <- already works
  → cad.revise produces version 2, parent_id -> version 1
  → the old version stays addressable, superseded not deleted
```

The parts that already exist are marked below.

---

## Artifact types to add

`memory/artifacts.py` takes `artifact_type` as a free string, so this needs no
schema change:

| Type | Payload | Freshness |
|---|---|---|
| `cad_model` | parametric source, dimensions, constraints, material, units | `STATIC` — geometry does not decay |
| `cad_export` | format (STEP/STL/3MF), path, byte size, checksum | `STATIC` |
| `cad_render` | preview image path, camera, resolution | `SLOW` — regenerate if the model changes |
| `cad_assembly` | child model ids, mates, bill of materials | `STATIC` |
| `print_job` | printer, slicer profile, estimated time and filament | `REALTIME` — a running job's state is live |

Existing fields that already carry their weight:

* `parent_id` — assembly → parts, and version N → version N−1
* `item_index` — "the second mounting hole", "the third part"
* `provenance` — the prompt and parameters that produced the geometry
* `trust` — `ASSISTANT_INFERENCE` for a dimension Nova chose versus
  `DIRECT_USER` for one Marcus specified. This distinction matters more in CAD
  than anywhere else: a hole position Nova inferred should be hedged when
  surfaced, and a tolerance Marcus stated should not be quietly changed.
* `stale_fields()` — a `print_job`'s ETA goes stale in minutes while its
  geometry never does. The per-field volatility model already handles this.

---

## Versioning

CAD is the case where "supersede, do not delete" is not optional — a revision
that turns out wrong must be recoverable.

This maps onto the same discipline Nova's memory already uses for fact
supersession (`docs/JARVIS_V2_AUDIT.md` §6): the old version stays
historically accountable, `active` goes false, `parent_id` chains the lineage.
No new mechanism needed.

---

## Tool selection

CAD is precisely the scenario that motivated the selector. A CAD-capable Nova
plausibly registers 20–40 additional tools (sketch, extrude, fillet, pattern,
constrain, measure, export, slice, print, …), which would have taken the
`decide()` catalogue from ~736 tokens to well over 1,200 — six times per turn.

With `core/tools/selector.py` in place:

* Deterministic patterns handle the unambiguous verbs ("slice this", "export as
  STL").
* Semantic ranking handles the rest, and the tool vectors are computed once at
  registration because CAD tool descriptions are static.
* Adding 40 tools costs 40 one-time embeddings and roughly nothing per turn.

The measured per-turn selector cost is 12.42 ms at 49 tools and is dominated by
embedding the query once, which does not scale with registry size.

---

## Voice

CAD replies are exactly the content `core/voice/speech_text.py` exists for.

```
DISPLAY:  **Bracket v2** — 120 × 45 × 3 mm, 4 × M6 holes, 1.8 mm fillet
SPOKEN:   Bracket version 2. 120 by 45 by 3 millimetres, four M6 holes,
          1.8 millimetre fillet.
```

Two additions needed:

1. Dimension expansion (`120 × 45 × 3 mm` → "120 by 45 by 3 millimetres").
   `_UNITS` in `speech_text.py` already has the mechanism; `mm`, `cm`, `in`,
   `°` and `×` need entries.
2. A length cap on spoken geometry. A 40-feature model must not be read out
   feature by feature; the voice summarises and the viewer shows the detail.
   This is the `display_text` / `spoken_text` split already in place.

Long CAD operations also want a progress event on the existing `BUS`, so the
frontend can show a spinner and Nova can say "still working on it" rather than
going silent for 30 seconds.

---

## Process isolation

CAD kernels and slicers are heavy native dependencies with their own crash
modes, and a segfault in a geometry kernel must not take Nova down.

The pattern is settled twice over: `tools/imagegen/service.py` (separate venv,
separate GPU, honest `/health`) and `services/tts_worker.py` (child process,
bounded IPC, crash recovery, per-turn cancellation).

A CAD service should follow the imagegen shape rather than the TTS shape — its
dependency stack is large and unrelated to Nova's, so a separate venv is worth
it, and its operations are long enough that HTTP is fine.

Cancellation matters: "actually, make it aluminium" mid-generation should abort
the running job. `services/tts_client.py` already demonstrates the pattern —
turn-scoped request ids, results discarded on cancellation.

---

## Printing

ADA's printer agent handles slicer detection and print orchestration. Two
constraints for Nova:

1. **Nothing requires owning a printer.** Design, preview and export must work
   standalone. `/status` should report `printer: not configured` honestly rather
   than hiding the capability, matching how `image.generate` already reports
   when the imagegen service is absent.
2. **Starting a print is a physical action.** It goes through
   `core/permissions.py` with explicit confirmation, like any other
   irreversible action. A tool selector surfacing `print.start` is not
   authorisation to run it — the selector never touches permissions, which is
   enforced by a test.

---

## Suggested order

1. Artifact types + versioning (no new infrastructure; unlocks references)
2. A read-only viewer fed by an SSE `action` event, like the existing maps overlay
3. `cad.design` / `cad.revise` in an isolated service
4. Voice: dimension expansion + geometry summarisation
5. Export formats
6. Slicing
7. Printing, behind permissions

Steps 1 and 2 are worth doing before any CAD kernel is chosen — they are where
the conversational continuity lives, and they are the parts that would otherwise
have to be retrofitted.
