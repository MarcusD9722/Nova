# Nova V3 P5 — Local speaker identity

Nova can tell who is speaking, locally, without shipping a voice anywhere.

**This document describes what is built and what is not.** P5 as specified is a
large phase.

| | |
|---|---|
| **P5 part 1** | the measured backend decision, the identification subsystem, `/stt` integration |
| **P5.1** | four pre-flight defects in part 1, each reproduced before being fixed |
| **P5.1a** | two remaining unverified-voice holes, both reproduced first |
| **P5.1b** | the same invariant when the speaker *service itself* fails |
| **P5.1 main** | live turn carriage, write isolation, read privacy, correction enforcement |
| **P5.1c** | audit only — nine remaining failures reproduced against `78cba4d`, nothing fixed |
| **P5.1d** | every backend read and write path made speaker-safe, including the ones that run after the turn ends |
| **P5.1d.1** | read side effects, delimiter-exact namespaces, durable turn attribution, guest lessons applied |
| **P5.1d.2** | the direct tool surface — the boundary the model could step around by emitting a tool call |
| **P5.1d.3 (this pass)** | the full persistent-state inventory: durable stores that are not called "memory" |

The scope table at the end says exactly what remains, and nothing here is
reported as finished when it is not.

---

## P5.1 — four defects part 1 shipped

Each was verified against the code before anything changed. All four reproduced.

**1. The model revision was metadata-only.** Part 1 stamped
`0f99f2d0…` into every profile and used it to decide compatibility, while
`from_hparams` was called with no revision at all — so Nova loaded whatever HEAD
happened to be and asserted a commit she had never requested. Had upstream
republished the weights, she would have compared new embeddings against old
centroids, confidently, forever. Now pinned through SpeechBrain's supported
`fetch_config=FetchConfig(revision=…)`, using the *same constant* profiles are
stamped with, and `status()` reports `revision_pinned` as an observation of a
real load rather than a claim.

**2. Ordinary identification had no quality gate.** Enrollment rejected silence,
clipping and fragments; `identify()` rejected nothing but length, so a
long-enough stretch of near-silence was embedded and scored against profiles
like any other audio. An empty room could return a match.

`command_quality()` now gates before the model — and deliberately at a *lower*
bar than enrollment. Enrollment can demand 1.5 s of clean speech because it
happens once and can ask for a retake; a real command is often "stop" or "yes",
and rejecting those to guard against silence would break normal use to fix a
problem only silence causes. Measured: 1.1 s / 1.5 s / 3.0 s commands accepted,
a 0.4 s fragment `too_short`, digital and near-silence refused, quiet-but-real
speech accepted.

**3. Voice-turn redemption was replayable.** `redeem_voice_turn` returned the
match and left the handle in the cache, so one captured id could assert the same
identity on every later turn within its 300 s TTL. Redemption now consumes the
handle: one `/stt` classification backs exactly one chat turn.

An identity *failure* still mints a handle — `unknown`, `ambiguous` and
`too_short` redeem back as themselves. **`unavailable` did not**, which P5.1's
report described as though it did; that gap is fixed in P5.1a below.

**4. The privacy setting did nothing.** `NOVA_SPEAKER_KEEP_AUDIO` promised
control over raw recordings that were never written, and the expression guarding
the derived embeddings read `keep_audio() or True` — so the flag changed
nothing, in either direction. A privacy setting that does nothing is worse than
no setting, because someone will rely on it. Removed rather than given a meaning
it never had. Raw audio is never retained; the derived embeddings are, and a
test asserts no audio file of any format is written during enrollment.

---

## P5.1a — two remaining ways a voice turn could lose its state

Both reproduced on `d9ecc5a` before anything changed.

### Hole 1: `unavailable` was the one outcome with no handle

`issue_voice_turn` refused `status == unavailable`, so four outcomes carried
structured evidence that a voice command had happened and a classifier failure
carried none. Once attribution is wired, **"no speaker metadata" is exactly the
state that would be read as typed-Marcus** — so the failure case is the one that
most needs the handle.

The fix is a distinction the code did not previously have. `SpeakerMatch` now
records whether identification was **attempted**:

| | meaning | handle? |
|---|---|---|
| `attempted=False` | the feature is **off**. Legacy Nova. No speaker question is being asked. | no |
| `attempted=True` | the feature is **on** and Nova tried. Every outcome — including `unavailable` — is a real backend-derived voice-turn result. | yes |

This is what keeps *"we could not tell"* distinguishable from *"nobody asked"*.
Removing the `unavailable` check alone would have made `NOVA_SPEAKER_ID=0` mint
handles and quietly turn every legacy voice turn into an unverified guest turn —
the opposite mistake, and just as wrong.

**Handle semantics, all outcomes:**

| outcome | handle | single-use | expires |
|---|---|---|---|
| `known` | yes | yes | yes |
| `unknown` | yes | yes | yes |
| `ambiguous` | yes | yes | yes |
| `too_short` | yes | yes | yes |
| `unavailable` (attempted) | **yes** | yes | yes |
| disabled (`attempted=False`) | **no** | — | — |

### Hole 2: an empty transcript was still classified

`/stt` called the speaker path whenever `identify_speaker` was set, with no
reference to `result.empty`. A buffer can carry enough energy to clear
`command_quality()` while Whisper returns nothing — so **background noise could
come back as a `known` speaker for a turn containing no words**.

An empty result now short-circuits the model entirely:

* **zero** embedding calls — asserted by counting them, not by timing
* status `unavailable`, reason `empty_transcript`
* no profile, no similarity, never `known`
* still `attempted=True` with a handle, so the turn keeps its unverified voice
  state rather than looking like typed input

No second decode, no second upload, no second Whisper call. The guard is about
the *transcript*, not the audio: the identical buffer treated as a real
utterance still classifies, exactly once.

`SpeakerInfo` gained `reason` and `attempted` — structured diagnostics that
distinguish `empty_transcript` from `disabled` from `no embedding`, and expose
nothing biometric.

### Call counts, asserted

| path | ffmpeg | Whisper | ECAPA |
|---|---|---|---|
| voice command | 1 | 1 | 1 |
| empty transcript | 1 | 1 | **0** |
| wake chunk | 1 | 1 | **0** |
| typed chat | 0 | 0 | **0** |
| speaker disabled | 1 | 1 | **0** (no model load) |

---

## P5.1b — when the subsystem itself fails

P5.1a made every *classification* outcome preserve its voice-turn state. It did
not cover the case where there is nothing to classify with. Reproduced on
`335d31a`, feature **enabled**, real command:

| situation | status | attempted | handle |
|---|---|---|---|
| `SpeakerService` cannot be constructed | `unavailable` | **False** | none |
| unexpected exception in the helper | `unavailable` | **False** | none |
| `NOVA_SPEAKER_ID=0` (disabled) | `unavailable` | False | none |

The first two were byte-for-byte the third in every field a consumer would read.
Only the `reason` string differed — and no attribution logic should ever have to
parse prose to decide whether personal memory may be written.

### The final semantics

```
disabled      nobody asked a speaker question.  Legacy Nova, typed semantics.
unavailable   the question WAS asked and could not be answered.
              Unverified voice — personal memory must not be written to Marcus.
```

`attempted` is the discriminator, and it is a boolean rather than a string
comparison. **A subsystem that fails must not be able to erase the evidence that
it was supposed to run**, because "no speaker metadata" is exactly what would
later read as typed-Marcus. Failures fail closed, toward *unverified*, never
toward an identity.

### Architecture: one cache, moved — not a second one

The handle cache lived inside `SpeakerService`, so it died with it. It has no
dependency on ECAPA, on SQLite, or on the registry — it is a dict of small
metadata records, and its placement was the whole bug.

`core/speaker/voice_turns.py` now owns it, process-wide, and `SpeakerService`
delegates. That is the same single cache in a place where it survives its
consumer, rather than a fallback cache with parallel semantics to keep in sync.
`core/voice/turn.py`'s `TurnRegistry` was deliberately not reused: it governs
live execution turns, cancellation and barge-in, with a different lifetime, and
coupling barge-in cancellation to speaker metadata would be a worse trade.

Every handle keeps its contract: opaque, backend-derived, single-use, expiring,
bounded, metadata-only, never authorisation.

### Behaviour now

| situation | status | attempted | handle |
|---|---|---|---|
| service cannot be constructed | `unavailable` | **True** | **yes** |
| unexpected exception | `unavailable` | **True** | **yes** |
| disabled | `unavailable` | False | no |
| disabled **and** service missing | `unavailable` | False | no |

The last row matters: a service failure while the feature is off is still legacy
Nova, not a guest turn.

No additional decode, upload, Whisper call or embedding. The failure paths reach
the model zero times.

---

## P5.1 main body — identity now changes what Nova does

The failure this exists to prevent, stated plainly:

> A guest says *"my name is Alex"* and Nova rewrites **Marcus's** name.

Until now `entity="user"` meant Marcus, because only Marcus could speak.

### The attribution matrix

| turn | personal-memory entity |
|---|---|
| typed | `user` (legacy) |
| voice, speaker ID **disabled** | `user` (legacy — nobody asked) |
| voice, known profile with role `owner` | `user` |
| voice, known **non-owner** | `speaker:<profile_id>` |
| voice, unknown | **none** |
| voice, ambiguous | **none** |
| voice, too_short | **none** |
| voice, unavailable | **none** |
| voice, handle missing / invalid / expired / **replayed** | **none** |

`memory_entity` returns `None` for anything Nova could not attribute, and **None
is never substituted for a default**. Every failure lands on the safe side.

`stored_role` comes from the durable enrolled profile, never the request — a
client asserting `role=owner`, or a profile merely *named* "Marcus", does not get
the owner namespace.

### Two boundaries, not one

**WRITE.** `_extract_quick_facts` and the name-replacement helper consult turn
identity before every write. Name replacement is gated separately because it
*purges* first: a guest reaching it with the owner entity would have deleted
Marcus's name outright.

**READ.** Blocking writes alone would be half a boundary — reciting Marcus's
family, spouse and children to whoever is standing there leaks his profile just
as thoroughly. Grounding loads his personal context only for a turn that is
actually his. A known guest gets **their own** stored profile instead; an
unrecognised speaker gets none. Shared context (date, tools, capabilities) stays
for everyone, because it is not personal.

### `memory.correct` is enforced in code

The tool defaults to `entity="user"`. A guest saying *"no, my GPU is a 4090"*
would have superseded his. The model is **not** asked to pick a safe entity — it
does not know who is in the room, and making a safety boundary probabilistic is
the mistake. Personal corrections are redirected to the speaker's own namespace,
and an unverified speaker is refused with a result Nova can explain. Corrections
about explicitly named third parties are untouched.

### Carriage and concurrency

Identity is scoped in `chat_turn_stream`'s wrapper — one choke point, so every
early return inside (project prepass, direct replies, storytelling, error paths)
keeps it. A `ContextVar` rather than an attribute: concurrent turns each keep
their own view, verified by a test running four speakers in parallel, and the
`finally` guarantees background work never inherits a stale human.

### Still not authentication

Every speaker — typed, owner, guest, unknown — receives the **identical**
`PermissionBroker` decision, asserted by test. `evaluate()` takes no identity
argument. Owner role is a memory-routing label, not authority.

---

## P5.1c/d — the read paths, and everything that runs after the turn

P5.1 made the *synchronous* turn safe. An audit (P5.1c) then reproduced nine
remaining failures against `78cba4d` before a line was changed. Two conclusions
were uncomfortable enough to be worth stating plainly.

**Blocking writes was only half the boundary.** Three independent read paths
handed Marcus's private data to an unrecognised speaker: `_direct_live_reply`
answering "what is my name?" with his name, `memory.search` surfacing a private
fact into the prompt, and — the one that matters architecturally — the
`memory.recall` **tool** returning it when the model asked. A privacy boundary
enforced only in grounding is one tool call wide, and the model can make that
call. So the policy now lives in `core/turn_identity.py` (`may_read_entity`) and
every reader consults it, including the filter inside `MemoryUnifier.search()`.

**The write that mattered most happened after the speaker left.** `P5.1` scoped
the live turn with a `ContextVar`. A `ContextVar` does not cross a queue. The
background extractor — the path that writes the *durable* facts, seconds to
minutes later, on a worker task that never entered `active_turn` — read the typed
default and concluded every guest was Marcus. `MemoryIngestEvent` now carries an
identity **snapshot** taken where the turn ran, and the worker re-enters it
explicitly. A test asserts the worker ignores whatever identity is ambient while
the backlog drains.

### What changed

| Path | Before | Now |
|---|---|---|
| `_direct_live_reply` "what is my name?" | Marcus's name, to anyone | speaker's own name, or "I don't recognise your voice" |
| Production system prompt | Marcus's persona and family names to a stranger | `addressee()` wired; owner-only wording withheld |
| `memory.search` | every fact, to anyone | `may_read_entity` filter; cache key includes the speaker |
| `memory.recall` tool | bypassed the boundary entirely | refuses an unverified speaker; no date-range history for guests |
| `noticed_patterns` (insights) | reached any speaker | owner only |
| `current_focus` (active project) | reached any speaker | owner only |
| lessons / mood / wellbeing | one global entity | `lesson:speaker:<id>` etc.; unverified writes nothing |
| `MemoryIngestEvent` | no identity | identity snapshot, re-entered by the worker |
| extractor + policy facts | `entity="user"` always | `remap_entity_for`; unverified is discarded, not redirected |
| reconciliation prompt | "Marcus just told Nova…" | named from the turn's speaker |
| indexed conversation turns | `"Marcus said: …"` for everyone | speaker's label + `speaker_entity` metadata |
| relationship graph edges | drawn from any speaker's turn | owner only |
| `speaker:<id>` facts | no singleton or decay semantics | same person-quality memory Marcus gets |

### The read policy

`may_read_entity` is a positive allow-list, not a deny-list — and matching is
delimiter-exact, see P5.1d.1 below. Shared entities
(`world`, `system`, `capability`) are readable by anyone;
the owner reads everything; a known guest reads their own namespace and shared
knowledge; an unverified speaker gets shared knowledge only. Anything not
positively recognised is refused, so a personal entity added in a later phase is
private by default rather than public by oversight.

`note` is deliberately **not** shared. It is free-form and routinely holds
personal material — "not stored under `user`" is not the same as "public".

Neither are `project:` / `projects`. They were, briefly, on the theory that a
project is collaborative context. Measuring the grounding signals settled it:
what Marcus is building, and the names of everything he has built, is a personal
detail a stranger in the room has no claim on.

### The grounding signals, measured one sentinel at a time

Reading the gate was not enough. A unique sentinel was seeded into each personal
signal and grounding was built three times:

| | owner | known guest | unknown |
|---|---|---|---|
| before | all 6 present | **insights, active project** | **insights, active project** |
| after | all 6 present | none | none |

`_load_family` and `_load_profile` were already gated; `_load_insights` and
`_load_focus` never had a gate at all. Insights are generalisations Nova drew
about Marcus across many episodes — an inference about him is as personal as a
fact about him. The owner column is asserted too, so a "fix" that simply deletes
the feature for everybody cannot pass.

### The cache was the second half of the search fix

The first version of the search filter did nothing. `search()` returns early on a
disk-cache hit, above the filter, and the cache key had no speaker component —
so Marcus's cached result set was served verbatim to the next guest. Fixed by
putting the scope in the key *and* re-filtering cached results, because a key
alone still trusts whatever was stored under it.

---

## P5.1d.1 — the four things P5.1d still got wrong

All reproduced against `62672cf` before anything changed.

### A read you are not allowed to make left a trace

The scope filter ran *after* reinforcement and after the cache write:

```
rank → reinforce → cache → filter        (P5.1d)
rank → filter → reinforce → cache        (now)
```

Measured: an unknown speaker searching for Marcus's private fact took its
`access_count` from **0 → 1** and stamped `last_accessed_at`. The content never
reached them — but the read still made his memory of it stronger, which is both
a side channel and a corruption of the signal reinforcement exists to carry.
A denied hit now produces **zero** read-triggered writes.

### The allow-list matched on substring

`is_shared_entity` used `startswith`, so an allow-list of three roots quietly
admitted `worldsecret`, `world_private`, `system_personal`, `capability_notes`
and anything else beginning with those letters. An allow-list that matches on
substring is not an allow-list.

`under_root(entity, root)` is now exact: `root` itself, or `root:` and below.
The same helper backs `is_shared_entity`, `entity_belongs_to_speaker` and
`may_read_entity`, so the three cannot drift apart.

### The speaker namespace model

P5.1d put a guest's child namespaces *beside* their root
(`lesson:speaker:p-alice`) while `may_read_entity` allowed only the exact root —
so Alice's own lessons, mood and wellbeing were unreadable by Alice. One
canonical hierarchy replaces it:

```
speaker:<id>                    their root         (peer of `user`)
speaker:<id>:lesson             what they asked Nova to do differently
speaker:<id>:mood
speaker:<id>:wellbeing
speaker:<id>:session
speaker:<id>:person:<x>         someone THEY know
```

Read policy is then one containment check, centralised in
`entity_belongs_to_speaker`. A known speaker reads their root and everything
below it, plus shared knowledge — never the owner's, never another speaker's.
`speaker:p-alice2` is not inside `speaker:p-alice`, which is exactly why this
cannot be a prefix match. The older beside-the-root form is still recognised on
read, so nothing already written is stranded.

### Person-quality memory was claimed, not delivered

`_default_salience("speaker:p-alice", "name", .9)` returned **0.45** where the
owner's returned **1.00**. Three separate rules (salience, decay, singleton) had
each grown their own idea of what a speaker entity was, and prefix-matching
`speaker:` in the decay rule made *every* guest fact permanent — which is not
parity either, just a different wrong answer.

`personal_tail()` normalises once (`speaker:p-alice` → `user`,
`speaker:p-alice:note` → `note`, `speaker:p-alice:person:sarah` →
`person:sarah`) and the owner's existing rules apply unchanged. Parity is now a
property of the namespace rather than three rules that have to agree.

| | owner | known speaker |
|---|---|---|
| core identity (`name`, `spouse`, …) | 1.00, never decays, singleton | identical |
| an acquaintance's details | 0.70, decays, not singleton | identical |
| a passing note | 0.20, decays | identical |

Parity, not promotion: a guest's hobby does not become a permanent identity
fact, and their acquaintance's location does not supersede.

### Turn attribution was not durable

The speaker label lived only in Chroma metadata. The durable SQLite row — which
is what date-range recall actually reads — could not tell Marcus's sentences
from a guest's, so `recall_conversation` had to refuse guests wholesale, and
Alice could not recall her own history.

`turns` gained four columns via in-place `ALTER TABLE`, no rebuild:

| column | default | why |
|---|---|---|
| `speaker_entity` | `'user'` | whose history this row belongs to |
| `speaker_label` | `''` | how to name them when reading it back |
| `input_source` | `'typed'` | typed vs voice |
| `speaker_status` | `''` | known / unknown / … |

No embeddings, no similarity, no audio — the same rule the profile store
follows. Every pre-migration row predates speaker identity and *was* Marcus, so
the defaults are the correct answer rather than a guess, and his history reads
back exactly as before.

### Date-range recall matrix

| speaker | "what did we talk about last Tuesday" |
|---|---|
| owner | his own durable history, legacy behaviour, legacy rows included |
| known guest | **their own** history only |
| unverified | refused — there is no history belonging to nobody |

This is the narrower reading of D11 on purpose. D11 lets the owner see a guest's
stored *facts*; "what did **we** talk about" is a question about a shared
thread, and answering it by merging two transcripts would put words in Marcus's
mouth rather than merely show him data.

### `memory.recall` matrix

P5.1d refused an unverified speaker outright, before any search — stricter than
the stated policy, and it left Nova unable to say where the Eiffel Tower is to
someone she simply had not met. Generic recall now delegates to the one entity
filter inside `search()` instead of keeping a second copy of the policy.

| | shared (`world`/`system`/`capability`) | own personal | others' personal | date-range history |
|---|---|---|---|---|
| owner | yes | yes | yes (D11) | own |
| known guest | yes | yes | no | own |
| unverified | **yes** | — | no | no |

### A guest's corrections now actually apply

Lessons were stored per speaker and then injected only `if _ident.is_owner`, so
Nova would be told "stop doing that", write it down, and carry on doing it. Both
the storage and the prompt are now speaker-scoped, with the guest block headed
`Lessons you've learned from <name> … They are <name>'s preferences, not
Marcus's` — never presented as things learned from Marcus.

### Latency

Filtering earlier costs the owner nothing, and fixed a regression it would
otherwise have introduced for guests. `may_read_entity` is 2.4 µs.

| | median | p90 |
|---|---|---|
| owner, typed | 0.70 ms | 0.96 ms |
| guest, first cut of this change | 47.20 ms | 89.90 ms |
| guest, after the cache fix | 0.61 ms | 0.78 ms |

The middle row is the one worth keeping: caching the *allowed* view means a
guest's view is legitimately empty sometimes, and the early return treated empty
as a cache miss — so every such search re-ran the whole fan-out. A miss is
`None`; an empty list is a real answer and is cached as one.

---

## P5.1d.2 — the tool surface the model can reach

Everything up to here scoped the paths **Nova** takes on her own: grounding,
semantic search, quick-fact capture, the background extractor. The tools the
**model** calls were still global. So the whole boundary could be stepped around
by emitting a tool call — which is not a hypothetical, it is the ordinary way
the model interacts with memory.

All four speakers were exercised against the real `ToolRouter` on `d1ec5a9`.
Everything below reproduced.

| tool | before | now |
|---|---|---|
| `memory.remember` | every speaker wrote to the one global `note` | owner `note`; guest `speaker:<id>:note`; unverified refused |
| `memory.correct` | Alice passing `entity="speaker:p-bob"` **changed Bob's fact** | another speaker's namespace is refused; anything else nests under the caller |
| `memory.remember_person` | guests wrote into Marcus's `people` table | owner unchanged; guest fact-backed at `speaker:<id>:person:<key>`; unverified refused |
| `memory.recall_person` | returned Marcus's people to anyone | owner unchanged; guest reads only their own |
| `memory.remember_event` | guests added to Marcus's timeline | owner-only |
| `memory.timeline` | returned Marcus's history to anyone | owner-only |
| `memory.link` / `related` / `path` | guests read **and mutated** Marcus's graph | owner-only |
| `thoughts.recall` | handed Nova's private notes about Marcus to a stranger | owner-only |
| `twin.profile` | Marcus's behavioural profile, to anyone | owner-only |
| `executive.brief` | his deadlines and stalled goals, to anyone | owner-only |
| `reminder.create` | a guest could put items on his schedule | owner-only |
| `memory.learn_lesson` | reported success for an unverified speaker who wrote nothing | honest refusal |
| `world.recall` / `world.learn` | shared | **unchanged — still shared** |

### The one that mattered most

`memory.correct` was guarded — but only on the *default* entity. P5.1 remapped
`user` / `me` / `myself` and let every other string through to `correct_fact`.
A guard one argument wide is not a guard:

```
Alice: memory.correct(entity="speaker:p-bob", favorite_color="red")
   →   Bob's stored colour changed from blue to red
```

`resolve_write_target()` now answers "where may this speaker write an entity the
model named", and it is a **refusal**, not a remap, when the entity names
someone else's root. Nesting it under Alice would have invented
`speaker:p-alice:speaker:p-bob`, which reads like a claim about Bob and belongs
to nobody.

### Scope, not permission

Every one of these is **data routing**. `PermissionBroker` is untouched: a guest
may still call every tool and gets the identical decision Marcus gets, asserted
across three capabilities and five identities. What changes is whose data the
call reaches. Speaker identity is still not authentication.

### Where this fails closed, and why

`people`, `events`, the knowledge graph and the digital twin are modelled as
Marcus's, with no per-person ownership column. Inventing a parallel store for
guests inside a corrective patch would be a worse outcome than a clear refusal —
a half-built second memory system is harder to remove than a gap. Those return
`scoped_unavailable` with a sentence Nova can say out loud, and scoped support
can land later without a migration.

`memory.remember_person` is the exception, because the canonical hierarchy
already had a home for it: `speaker:<id>:person:<key>` is ordinary facts, no new
store, and a guest can read their own people back.

`memory.timeline` fails closed rather than partially scoping. A timeline
aggregates events, conversation digests and reminders; scoping one source would
not make the composite safe, and a partially-scoped history is worse than none
because it looks complete.

### Legacy namespace compatibility, tightened

P5.1d.1's compatibility rule matched `endswith(":" + own)`, which read
`speaker:p-bob:lesson:speaker:p-alice` as **Alice's** — Bob's namespace with her
name appended. That is the same substring-for-structure mistake `under_root`
exists to prevent, smuggled back in through the compatibility path. The rule now
matches only the four exact shapes P5.1d could actually write
(`lesson|mood|wellbeing|session:speaker:<id>`).

### Tool descriptions

The descriptions are shown to the model and said things like *"when Marcus
corrects you"*, *"someone Marcus mentions"*. Those become wrong the moment Alice
is speaking, and a description that misdescribes the situation invites the model
to act on the wrong assumption. They now say "the current speaker" / "this
person". Execution remains the boundary — the descriptions are guidance, not
enforcement, and no speaker name is ever placed in a client-provided argument.

### Latency

The scope checks are not measurable against the I/O they guard.

| | |
|---|---|
| `resolve_write_target` (owner path) | 0.19 µs |
| `resolve_write_target` (guest, nested) | 3.24 µs |
| `entity_belongs_to_speaker` | 0.45 µs |
| `memory.remember` end to end (owner) | 45.99 ms median |

---

## P5.1d.3 — the state that isn't called "memory"

P5.1d.2 fixed the tools named `memory.*`. The failure mode left over: **a tool
bypasses speaker privacy because the durable state it touches has a different
name.** So this pass inventoried every registered built-in from its code, not
its name, and classified each one.

### The classification

| category | meaning | tools |
|---|---|---|
| **speaker-scoped** | real `speaker:<id>` representation | `memory.remember`, `.recall`, `.correct`, `.learn_lesson`, `.remember_person`, `.recall_person` |
| **owner-private** | Marcus's store, no ownership column → fail closed | `memory.remember_event`, `.timeline`, `.related`, `.path`, `.link`, `.index_folder`, `.synthesize`, `thoughts.note`, `.recall`, `twin.profile`, `executive.brief`, `reminder.create`, `plan.save/status/advance`, `goal.create`, `research.track/list`, `skill.detect/learn/list/get/update/branch/delete/run`, `agent.recall` |
| **shared / system** | no per-person meaning | `world.recall/learn`, `research.findings`, `agents.roster`, `experiment.*`, `memory.rebuild_index`, `society.consult` |
| **capability** | PermissionBroker / dev mode decides, never the voice | `code.*`, `project.*`, `self.*`, `shell.exec`, `computer.*`, `vision.look_at_screen` |
| **ephemeral** | no durable state | `image.generate`, `video.generate` |

`tests/test_speaker_persistent_state_v51d3.py` asserts this table covers the
live registry exactly. **A tool added later without a classification fails the
suite** — which is the actual point: the previous three passes each found
something the one before had missed by omission rather than by error.

### What the inventory caught

Reproduced on `641f499`:

| | before |
|---|---|
| `thoughts.note` | P5.1d.2 closed the *reader* and left the *writer* — a guest's text still landed in the store Marcus reads back |
| `plan.*` | a guest read the owner's plan **and overwrote it** |
| `goal.create` | a guest and an unknown speaker each created a goal row **plus an enqueued `__decide__` task** — unattended background work started by someone Nova cannot name |
| `skill.*` | a guest listed, fetched, updated to `"HACKED"`, branched and **deleted** the owner's learned skill |
| `memory.index_folder` | a guest's folder was indexed straight into the owner document store |
| `research.track/list` | a guest read and added to the owner's tracking registry |
| `agent.recall` | specialist notes about Marcus returned to anyone |

And two the completeness check itself surfaced, registered by `RuntimeManager`
rather than `core/tooling.py`:

* **`memory.synthesize`** reads across the owner's indexed filesystem. Gating
  the writer and leaving this reader open would have achieved nothing.
* **`skill.run`** reads *and reveals* a learned workflow's steps.

### The one that was a layer down

`agent.recall` being owner-only is not enough on its own: `AgentSociety` injects
those same notes into **every specialist prompt**, so a guest could have
received Marcus's accumulated context inside a deliberation answer without ever
calling the tool. The notes are now omitted for a non-owner; the council still
deliberates for them, and the owner's is unchanged.

This is the shape of bug the whole sub-phase keeps finding: the boundary is
correct at the surface and absent one layer beneath it.

### Two classifications argued from evidence, not names

**`agent.recall` → owner-private.** `agent_remember` has *no production caller*.
The only note ever written through it in the tree — in
`tests/test_society_p6.py` — is *"Marcus prefers primary sources over blog
posts."* The store's designed content is his preferences, so it is his.
`agents.roster` stays shared: specs and counters, never note content.

**`research.findings` → shared, while `research.track`/`research.list` are
owner-private.** The distinction is real and worth keeping: the *registry* is
what Marcus asked Nova to follow — his workflow state. The *findings* are
sourced world facts, returned as `{summary, source, confidence}` with no
requester, timing, priority or rationale. A test asserts findings carry no other
keys, so the split cannot silently erode.

### Deliberately left alone

`experiment.*` is Nova's own A/B testing of prompt variants and ranking — the
meaning of an experiment does not change with who is speaking, so restricting it
would be exactly the over-correction the brief warned against.

Capability tools stay governed by `PermissionBroker` and developer mode.
`vision.look_at_screen` reads Marcus's screen but holds no durable state and
already requires broker consent per call; gating it on voice would convert a
consent prompt into an identity check. **Speaker identity is still not
authentication**, and none of these decisions makes it one.

### Latency

The gate is a property read on a `ContextVar`: **0.17 µs**. Owner tool calls are
dominated by their own I/O (`skill.list` 17 ms, `thoughts.recall` 16 ms,
`research.list` 60 ms median) — five orders of magnitude above the check.

### What the owner can still see

A guest's facts are visible to Marcus. This is his machine and his memory, and
the threat this phase addresses is a guest reaching *his* data, not the reverse.
Stated here, and asserted by test, so it stays a decision rather than becoming a
surprise.

---

## The one rule that outranks the rest

**Speaker identity is not authentication.**

A voice match may personalise a reply and attribute a memory. It may never grant
a capability that `PermissionBroker` would otherwise confirm or deny. Marcus
recognised at 0.99 similarity still gets asked before a destructive action.

This is enforced structurally, not by convention:

* `SpeakerService` exposes no method shaped like authorisation — no `allow`,
  `permit`, `grant`, `authorise`. A test asserts that.
* `permissions.evaluate()` takes a capability and a mode. There is no parameter a
  speaker match could be threaded through even if someone wanted to. A test
  asserts that too.
* Unknown capabilities still default to `ADMIN`.

The reason is simple: a voice is trivially recordable and increasingly
synthesisable. **P5 makes no anti-spoofing claim of any kind** — it does not
detect a recording of Marcus, a cloned voice, or a replay. Speaker verification
and liveness detection are different problems, and conflating them would build a
security boundary out of something that is not one.

Where identity *does* matter is a correctness and privacy boundary: an
unrecognised speaker must not have their statements written into Marcus's
personal memory. That is not the same claim as "Marcus's data is secure because
Nova knows his voice", and this document does not make the second one.

---

## Model choice — measured, not assumed

`speechbrain/spkrec-ecapa-voxceleb` (ECAPA-TDNN).

| | |
|---|---|
| revision | `0f99f2d0ebe89ac095bcc5903c4dd8f72b367286` |
| parameters | 20.8 M |
| on disk | 89 MB |
| embedding dimension | 192 |
| load time | 4.5 s cold (with download), 0.2 s warm |
| RSS | +222 MB CPU / +126 MB CUDA |

**Dependency impact was checked before installing anything.** The hard
constraint is that Nova's working Torch/CUDA/XTTS/STT stack must not be
disturbed to satisfy a speaker library, so `pip install --dry-run speechbrain`
came first:

```
Would install HyperPyYAML-1.2.3 ruamel.yaml-0.18.17
              ruamel.yaml.clib-0.2.15 sentencepiece-0.2.2 speechbrain-1.1.0
```

torch 2.11.0+cu128, torchaudio 2.11.0 and numpy 1.26.4 all reported *already
satisfied*. No downgrade, no CUDA change. A candidate that had demanded a torch
downgrade would have been rejected on that evidence alone — which is the whole
reason the dry run happened before the install.

**NVIDIA NeMo TitaNet was not benchmarked.** `nemo_toolkit` pulls a large
dependency tree, and the brief is explicit that a huge tree must not be imported
merely to complete a comparison. ECAPA met every requirement with a five-package
footprint, so the second candidate was not worth the risk to a working
environment. That is a decision made on cost, and it is recorded rather than
presented as a finished comparison.

## CPU, deliberately — and this is the measurement that decided it

| | 3 s of audio |
|---|---|
| CPU | **41–58 ms** |
| CUDA | 5.7 ms (+90 MB VRAM) |
| **CPU, while the GPU is saturated** | **46 ms — unchanged** |

CUDA is ~7× faster in isolation, and that is not the deciding number.

V3 P1 measured what a third CUDA consumer costs on this machine while the 9B
model generates: **whisper +185%, XTTS +209%**. Speaker ID would be that third
consumer, on the card that D1 records aborting the backend when pushed. Against
that, the CPU path is *provably indifferent* to GPU load — 57.9 ms idle, 46.3 ms
with the GPU pinned by a matmul loop — and 41 ms sits inside an `/stt` request
that already spends hundreds of milliseconds on ffmpeg and Whisper.

So CPU is the choice, not a fallback. `NOVA_SPEAKER_DEVICE=cuda` exists for
anyone who wants to re-measure the trade on different hardware.

## Does it actually separate voices?

Measured on the fixtures available offline:

| pair | cosine |
|---|---:|
| real human voice, segment 1 vs segment 2 | **+0.784** |
| synthetic voice A vs A′ | +0.979 |
| real human vs synthetic B | −0.034 |
| real human vs synthetic C | −0.045 |
| synthetic B vs synthetic C | +0.472 |

`min(same) = 0.784` versus `max(different) = 0.472`. Separated, with margin.

**What this does not prove:** that Nova recognises Marcus. The "real human voice"
is one speaker (the XTTS reference in `voices/nova.wav`), and the contrasts are
synthetic — a controlled contrast, not a second person. Real accuracy needs real
people, which is what `tests/live_speaker_id_harness.md` collects.

---

## Architecture

```
/stt upload
   │
   ├── ffmpeg  ──►  mono float32 PCM @ 16 kHz     ← decoded ONCE
   │                      │
   │                      ├──►  faster-whisper  ──►  text
   │                      │
   │                      └──►  ECAPA (CPU)     ──►  192-d embedding   [opt-in]
   │                                                      │
   │                                            registry ─┴─ matcher
   │                                                      │
   └──────────────────────────────────────────►  SpeakerInfo + voice_turn_id
```

One decode. The PCM Whisper already receives is handed back from the ASR thread
and reused — a second `ffmpeg` invocation, or a second upload of the same audio,
would double the most expensive part of the path to recompute bytes already in
memory.

Responsibilities stay separate so `/stt` never grows enrollment logic:

| module | job |
|---|---|
| `core/speaker/backend.py` | PCM → normalised embedding; lazy load; never raises |
| `core/speaker/registry.py` | durable profiles, model-compatibility, delete |
| `core/speaker/matcher.py` | open-set decision + enrollment quality rules |
| `core/speaker/service.py` | the façade the rest of Nova talks to |

## Opt-in per request — the wake loop stays free

Nova's fallback wake system calls `/stt` continuously on short chunks while
waiting for "Hey Nova". Embedding every one of those would burn CPU forever to
identify the speaker of a word that is about to be discarded.

So speaker identification is **opt-in per request**: `POST /stt` with
`speaker=true`. Default off.

* wake chunk → transcribe only, **zero** embedding calls
* real command → transcribe **and** identify
* typed chat → no speaker inference at all

## Open-set matching — why not `argmax`

`argmax(scores)` always names somebody. Point a stranger at a registry with one
enrolled speaker and the stranger *is* that speaker, at whatever score falls out.

The decision uses the top score, a threshold, **and** the margin to the
runner-up:

| | |
|---|---|
| Marcus .81, Alice .79 | passes a threshold and means nothing → **ambiguous** |
| Marcus .86, Alice .31 | passes, runner-up nowhere near → **known** |

Five outcomes, deliberately distinct, because "I don't know", "nobody I know"
and "the model isn't running" are different things a caller may want to handle
differently:

`known` · `unknown` · `ambiguous` · `too_short` · `unavailable`

**Thresholds are provisional** — 0.55 similarity, 0.10 margin, chosen from the
separation measured above. They are starting points for the live harness, not
calibrated values, and `SpeakerService.status()` reports
`threshold_calibrated: false` rather than implying otherwise.

The bias is deliberate and should survive calibration: **a guest wrongly
accepted as Marcus is much worse than an honest `unknown`.**

## Enrollment

Several samples, not one. A single recording encodes whatever the voice was
doing in those three seconds, and matching then depends on repeating that mood.

Rejected before the model ever sees them: silence, clipping, too short, empty,
malformed. Cheap deterministic signal checks — no VAD model, no LLM.

Consistency is then checked with **leave-one-out similarity**: each sample
against the centroid of the others, which catches the sample that does not belong
without being fooled by its own contribution to the mean. Obvious outliers are
dropped; a profile whose samples fundamentally disagree is refused with an
explanation written for the person recording it.

The profile stores a normalised centroid plus the individual embeddings, so a
future recalibration need not ask for six new recordings.

## Storage and privacy

Profiles live in Nova's existing SQLite database — the same reasoning D5 gave for
episodes.

**Raw enrollment audio is not stored.** Samples are embedded and discarded.
`NOVA_SPEAKER_KEEP_AUDIO=1` exists for someone deliberately building a
calibration set and is off by default: a voice recording is among the most
personal things Nova could hold, and P5 does not need it after enrollment.

Deleting a profile removes its embeddings. That is what a delete is for.

Nothing exposes an embedding: `describe()` returns metadata, `/stt` returns
metadata, `/status` returns counters. Tests assert it.

## Model-version compatibility

Every profile records `model_id`, `model_revision` and `embedding_dim`.
Embeddings from different models do not share a vector space, so comparing them
is not "slightly less accurate" — it is meaningless, and it would be meaningless
*confidently*. A profile whose model no longer matches reports
`needs_reenrollment` and is **skipped entirely** during matching rather than
scored badly.

## Integrity: the client cannot claim to be Marcus

The browser must not be able to send `"speaker": "Marcus"` and be believed.
Identity is derived on the backend, so it stays there:

1. `/stt` classifies and mints a short-lived `voice_turn_id`.
2. The frontend carries that opaque handle.
3. The backend resolves identity from a bounded, expiring cache.

TTL 300 s, at most 256 entries, expired handles refused, invented handles
resolve to nothing. **This is not authentication** — it protects the integrity of
speaker metadata and grants nothing.

## Failure behaviour

Speaker ID is enrichment and may never break voice interaction. Whisper
succeeding while the request returns HTTP 500 because an optional biometric
model misbehaved would be a self-inflicted outage.

| failure | result |
|---|---|
| model will not load | `status: unavailable`, transcription unaffected |
| embedding raises | `unavailable`, counted |
| corrupt profile | skipped, other profiles still matched |
| audio too short | `too_short` — distinct from `unknown` |
| `NOVA_SPEAKER_ID=0` | no model load, no embedding, `/stt` exactly as before |

## Barge-in is untouched

P0's acoustic barge-in and echo classification were **not modified**. Barge-in
audio can contain Marcus, Nova's own TTS leakage, or both — a different input
distribution from a clean command, and letting it label Nova's TTS as a guest (or
reject Marcus) would corrupt identity for no gain. Speaker ID applies to command
audio; the clean utterance after playback stops is the one worth classifying.

**Wake behaviour is also unchanged.** Anybody may still wake Nova. Restricting
the wake word to Marcus is not a P5 default and should not be considered before
the live harness produces real false-reject numbers.

---

## Scope: what is built, and what is not

Reported honestly rather than as a completed phase.

### Built and tested

* measured model + device decision (the gating architectural work)
* `core/speaker/` — backend, registry, matcher, enrollment, service
* durable profiles with model-compatibility gating; delete removes embeddings
* open-set matching with threshold + margin; five distinct outcomes
* enrollment quality gates and leave-one-out consistency
* `/stt` additive `speaker` field, opt-in per request, **single decode**
* `voice_turn_id` integrity handle: bounded, expiring, unforgeable
* failure isolation and `NOVA_SPEAKER_ID=0`
* 11 test groups (`tests/test_speaker_id_v5.py`), all passing
* `tests/live_speaker_id_harness.md`

### Fixed in P5.1

* model revision genuinely pinned at load, not just recorded
* command-audio quality gate — silence can no longer become `known`
* one-time voice-turn redemption
* the dead `NOVA_SPEAKER_KEEP_AUDIO` setting removed

### Fixed in P5.1a

* every *attempted* outcome preserves a voice-turn handle, `unavailable` included
* disabled mode stays legacy — it mints nothing and loads no model
* an empty transcript can never become `known`, and costs zero embedding calls

### Fixed in P5.1b

* a `SpeakerService` that cannot be constructed still yields an unverified
  *voice* turn, not a disabled-looking one
* the same for any unexpected fault in the `/stt` speaker helper
* the handle cache moved out of `SpeakerService` so it outlives it

### Fixed in P5.1 (main body)

* `TurnIdentity` + `active_turn`, carried through `chat_turn_stream`
* the attribution matrix, enforced in code
* `memory.correct` routed by identity rather than asked for by prompt
* speaker-scoped grounding and quick-fact capture

### Fixed in P5.1d

* every backend **read** path scoped: direct replies, production prompt,
  `memory.search` (and its cache), the `memory.recall` tool
* the personal grounding signals gated — including `noticed_patterns` and
  `current_focus`, which had no gate at all
* lessons, mood and wellbeing given per-speaker namespaces
* `MemoryIngestEvent` carries an identity snapshot; the background extractor,
  policy facts and reconciliation all route through `remap_entity_for`
* conversation turns indexed with the real speaker and a `speaker_entity` tag
* relationship-graph edges restricted to the owner
* `speaker:<id>` given the same singleton and no-decay semantics as `user`

### NOT built — do not assume these work

* **Frontend wiring (`A` in the audit).** This is the one that decides whether
  any of the above runs in the live app. `/stt` returns speaker metadata and
  `/chat` accepts a `voice_turn_id`, but the frontend still collapses the STT
  response to a string, never requests `speaker=true`, and sends no handle. So a
  **live voice turn today resolves to typed/owner semantics** — Nova behaves
  exactly as she did before P5. Deferred to P5.1e deliberately: the backend had
  to be safe before the frontend started asserting real identities.
* **Enrollment HTTP endpoints and UX.** Enrollment is programmatic only.
* **Episodic speaker provenance.** Episodes and artifacts carry no speaker.
* **The P5 pipeline benchmark** (`/stt` before/after, parallel execution).

The ordering is fail-closed rather than half-open: with no handle the backend
resolves to legacy owner semantics, which is what Nova already did, and the
guest-safety machinery is exercised only once something actually asserts a
guest. It is not, however, protection that is currently *running*.

## Live validation

**Unvalidated.** No accuracy figure for Marcus exists and none may be quoted
until `tests/live_speaker_id_harness.md` has been run with Marcus and at least
one other real person.

Separately, **P0 live barge-in acceptance is still pending a human run.** P5 did
not touch barge-in and does not change that.
