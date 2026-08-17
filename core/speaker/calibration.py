from __future__ import annotations

"""Turning provisional speaker thresholds into measured ones (V3 P5.2).

P5 shipped 0.55 / 0.10 as *provisional* numbers derived from offline fixtures,
and `status()` has said `threshold_calibrated: false` ever since. This module is
how that becomes true — and the one thing it must never do is make the dashboard
green by lowering a bar.

THE ASYMMETRY THAT DRIVES EVERY CHOICE HERE
-------------------------------------------
The two failure modes are not equally bad.

    false REJECT   Nova says "I don't recognise you" to Marcus.
                   Annoying. He repeats himself.
    false ACCEPT   Nova files a stranger's words into Marcus's memory, or reads
                   his private facts to them.

The entire P5.1 boundary exists to prevent the second one. So the fit optimises
for **zero observed false accepts first**, and only then for genuine acceptance.
`unknown` and `ambiguous` are *successful* outcomes of a system that is unsure —
they are counted as rejects, never as errors.

If the measured distributions cannot deliver both zero false accepts and the
required genuine acceptance, calibration FAILS and says so. A threshold that
only looks good because it was fitted loosely is worse than an honest
provisional one, because it carries a claim.

NOTHING BIOMETRIC IS STORED HERE
--------------------------------
Trials carry a score and a label. The calibration record carries thresholds,
margins and aggregate counts. No embeddings, no centroids, no audio, no
transcripts — the same rule the profile store follows.
"""

import json
import math
import statistics
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

import aiosqlite

from core.logging_setup import get_logger
from core.speaker.backend import EMBEDDING_DIM, MODEL_ID, MODEL_REVISION

logger = get_logger(__name__)

#: Bump when the meaning of a stored calibration changes, so an old record is
#: recognised as stale rather than silently reused under new semantics.
PROTOCOL_VERSION = 1

#: Acceptance bars. Deliberately expressed as constants with names, so a future
#: change to either is a visible decision rather than a tweaked literal.
MIN_GENUINE_ACCEPT_RATE = 0.90
MAX_FALSE_ACCEPTS = 0

# ── the candidate grid, and why it starts where it does ──────────────────────
#
# The scores being fitted are TRUE COSINE similarities between L2-normalised
# ECAPA embeddings (`matcher.cosine`), so the metric is bounded [-1, 1] — not
# [0, 1], and emphatically not [0.30, 0.90].
#
# The first real human calibration run failed on exactly that. Marcus measured
# genuine p05 0.2405 against impostor max 0.2001 — positive separation, a valid
# threshold sitting near 0.21-0.25 — and the fitter reported "the distributions
# overlap" because it never evaluated a candidate below 0.30. The floor was an
# unexamined guess carried over from synthetic fixtures whose scores happened to
# be high; real speech is not obliged to agree with it.
#
# LOWER BOUND 0.01 — the first STRICTLY POSITIVE grid point, and this one IS
# principled rather than a guess. A cosine threshold at or below zero cannot
# express an identity claim: it admits vectors with no directional agreement
# with the centroid at all, which for 192-d unit vectors is roughly half of
# everything. The empirical bars below ("no impostor in THIS sample cleared it")
# cannot protect against that, because the sample is 12-23 utterances and the
# runtime population is every voice that ever speaks.
#
# The floor is 0.01 and not 0.00 deliberately. With 0.00 on the grid the fitter
# could SELECT exactly zero whenever every observed impostor happened to score
# negative and >=90% of genuine samples cleared zero — a configuration that
# satisfies both bars on paper while asserting an identity claim the policy
# above says is meaningless. That is the exact fail-open this floor exists to
# prevent, so zero is excluded from the candidate set rather than merely
# discouraged in a comment.
#
# If no strictly positive threshold satisfies both bars, the honest answer is
# that this pair of voices is not separable by a bar worth asserting — so the
# fit fails closed and says which boundary it hit.
#
# UPPER BOUND 1.00 rather than 0.90, for the same reason the floor moved: 0.90
# would reject a genuinely tight speaker whose impostor max sat above it.
#
# 0.01 resolution is kept: well below the run-to-run spread of a real voice, and
# it keeps the search deterministic and explainable.
THRESHOLD_MIN, THRESHOLD_MAX = 0.01, 1.00
THRESHOLD_GRID = [round(THRESHOLD_MIN + 0.01 * i, 2)
                  for i in range(int(round((THRESHOLD_MAX - THRESHOLD_MIN) / 0.01)) + 1)]

#: Margin candidates. The ceiling is exercised, not binding: the first human run
#: fitted 0.29, meaning 0.30 was evaluated and rejected.
MARGIN_GRID = [round(0.02 + 0.01 * i, 2) for i in range(29)]         # .02 … .30


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Trial:
    """One labelled measurement. No audio, no embedding — a score and a truth."""

    truth: str                      # profile_id of the human who actually spoke
    top_profile_id: str | None      # who the matcher ranked first
    top_score: float
    second_score: float | None = None
    second_profile_id: str | None = None
    status: str = ""                # known | unknown | ambiguous | too_short | …
    condition: str = "normal"       # normal | quiet | loud | near | far
    phase: str = "A"                # A = owner-only, B = two-profile, V = validation


@dataclass
class ProfileFit:
    profile_id: str
    display_name: str = ""
    threshold: float | None = None
    genuine_n: int = 0
    impostor_n: int = 0
    genuine_accept_rate: float = 0.0
    false_accepts: int = 0
    genuine_min: float | None = None
    genuine_p05: float | None = None
    genuine_median: float | None = None
    impostor_max: float | None = None
    impostor_median: float | None = None
    separation: float | None = None      # genuine_p05 - impostor_max
    #: The highest threshold that still accepts MIN_GENUINE_ACCEPT_RATE of this
    #: speaker's genuine scores. This — not `genuine_p05` — is the real ceiling
    #: the decision is made against, so a failure can say which side it fell on.
    accept_ceiling: float | None = None
    ok: bool = False
    reason: str = ""


@dataclass
class CalibrationResult:
    ok: bool = False
    reason: str = ""
    margin: float | None = None
    margin_correct_rate: float = 0.0
    margin_wrong_person: int = 0
    profiles: list[ProfileFit] = field(default_factory=list)
    protocol_version: int = PROTOCOL_VERSION
    model_id: str = MODEL_ID
    model_revision: str = MODEL_REVISION

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["profiles"] = [asdict(p) if not isinstance(p, dict) else p for p in self.profiles]
        return d


def _pct(values: Sequence[float], q: float) -> float | None:
    """Lower-tail percentile, nearest-rank. Small n, so no interpolation games."""
    if not values:
        return None
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round(q * (len(s) - 1)))))
    return float(s[k])


def fit_profile_threshold(profile_id: str, display_name: str,
                          genuine: Sequence[float],
                          impostor: Sequence[float]) -> ProfileFit:
    """Choose the threshold for one enrolled human.

    Walks the grid from strictest to most permissive and takes the FIRST value
    that admits zero impostors and enough genuine speech. Strictest-first is the
    point: among candidates that satisfy both bars, the tightest is the one that
    leaves the most headroom for a voice the fit has never seen.
    """
    fit = ProfileFit(profile_id=profile_id, display_name=display_name,
                     genuine_n=len(genuine), impostor_n=len(impostor))
    if len(genuine) < 5:
        fit.reason = f"only {len(genuine)} genuine trials; need at least 5"
        return fit
    if not impostor:
        # Without impostor scores there is no evidence about false accepts, and
        # a threshold fitted on genuine speech alone is a guess wearing numbers.
        fit.reason = "no impostor trials — cannot bound false accepts"
        return fit

    fit.genuine_min = float(min(genuine))
    fit.genuine_p05 = _pct(genuine, 0.05)
    fit.genuine_median = float(statistics.median(genuine))
    fit.impostor_max = float(max(impostor))
    fit.impostor_median = float(statistics.median(impostor))
    fit.separation = round(float(fit.genuine_p05) - float(fit.impostor_max), 4)

    # How many genuine scores must clear the bar, and therefore the highest bar
    # that can still clear that many. `ceil` because 28.8 of 32 means 29.
    need = math.ceil(MIN_GENUINE_ACCEPT_RATE * len(genuine))
    desc = sorted(genuine, reverse=True)
    fit.accept_ceiling = float(desc[min(need, len(desc)) - 1]) if need >= 1 else None

    best: float | None = None
    for cand in reversed(THRESHOLD_GRID):          # strictest first
        fa = sum(1 for s in impostor if s >= cand)
        acc = sum(1 for s in genuine if s >= cand) / len(genuine)
        if fa <= MAX_FALSE_ACCEPTS and acc >= MIN_GENUINE_ACCEPT_RATE:
            best = cand
            fit.false_accepts = fa
            fit.genuine_accept_rate = round(acc, 4)
            break

    if best is None:
        # WHY it failed, derived from the data — never inferred from `best is
        # None`. The old message asserted "the distributions overlap" for every
        # failure, which was actively misleading on the first human run: the
        # distributions were separated by +0.04 and the real fault was the
        # search floor. A diagnostic that names the wrong cause sends a person
        # to re-record forty utterances that were fine.
        #
        # `accept_ceiling` is the highest threshold that still accepts the
        # required share of genuine speech; any threshold above `impostor_max`
        # admits no impostor. So a valid real-valued threshold exists precisely
        # when accept_ceiling > impostor_max.
        ceiling = fit.accept_ceiling
        imax = float(fit.impostor_max)
        if ceiling is None or ceiling <= imax:
            fit.reason = (
                f"no threshold gives 0 false accepts and >= "
                f"{MIN_GENUINE_ACCEPT_RATE:.0%} acceptance: accepting "
                f"{MIN_GENUINE_ACCEPT_RATE:.0%} of this speaker needs a "
                f"threshold at or below {ceiling:.4f}, but an impostor reached "
                f"{imax:.4f} — the distributions genuinely overlap, so any bar "
                f"loose enough to admit them admits somebody else too"
            )
        elif ceiling < THRESHOLD_MIN:
            # A threshold WOULD satisfy both bars, but only at or below zero —
            # every impostor in this sample happened to score negative. Refusing
            # is the policy, not a limitation, and the message must not blame
            # the search range for a deliberate safety decision.
            fit.reason = (
                f"accepting {MIN_GENUINE_ACCEPT_RATE:.0%} of this speaker needs a "
                f"threshold at or below {ceiling:.4f}, which is under the minimum "
                f"{THRESHOLD_MIN:.2f} this system will assert. A bar that low "
                f"admits voices with no directional agreement at all; the "
                f"{len(impostor)} impostor samples here all scored below it, but "
                f"that is a property of this sample, not of every voice that will "
                f"ever speak. Refusing to calibrate rather than fail open"
            )
        elif not any(imax < c <= ceiling for c in THRESHOLD_GRID):
            # A valid threshold exists in the data but not on the grid. After
            # widening the range to the metric's real bounds this should only be
            # reachable when the usable window is narrower than one 0.01 step.
            fit.reason = (
                f"a valid threshold exists in ({imax:.4f}, {ceiling:.4f}] but no "
                f"candidate falls inside it — the search range "
                f"[{THRESHOLD_MIN:.2f}, {THRESHOLD_MAX:.2f}] at 0.01 resolution "
                f"cannot express it. This is a FITTER limitation, not a property "
                f"of the voices"
            )
        else:  # pragma: no cover - defensive; the loop should have found it
            fit.reason = (
                f"internal: a candidate in ({imax:.4f}, {ceiling:.4f}] satisfies "
                f"both bars but the search did not select it"
            )
        return fit

    fit.threshold = best
    fit.ok = True
    fit.reason = (f"{best:.2f}: {fit.genuine_accept_rate:.0%} genuine accepted, "
                  f"0 impostor accepted")
    return fit


def fit_margin(trials: Iterable[Trial],
               thresholds: dict[str, float] | None = None
               ) -> tuple[float | None, float, int, str]:
    """Choose the top-vs-runner-up margin from two-profile labelled trials.

    Returns (margin, correct_rate, wrong_person_count, reason).

    Simulates the REAL classifier, not the gap alone (V3 P5.2 closure): a trial
    only counts as a correct KNOWN when the top score clears that profile's
    proposed threshold *and* the gap clears the candidate margin. Scoring on the
    gap by itself credited trials the live matcher would have returned as
    `unknown`, so a margin could show >=90% while the shipped classifier did
    not.

    Naming the WRONG person is the failure this exists to prevent, so a
    candidate producing even one is rejected outright — `ambiguous` is a better
    answer than a confident mistake.
    """
    thresholds = thresholds or {}
    rows = [t for t in trials if t.second_score is not None and t.top_profile_id]
    if len(rows) < 8:
        return (None, 0.0, 0, f"only {len(rows)} two-profile trials; need at least 8")

    by_truth: dict[str, list[Trial]] = {}
    for t in rows:
        by_truth.setdefault(t.truth, []).append(t)
    if len(by_truth) < 2:
        return (None, 0.0, 0, "two-profile trials cover only one speaker")

    best: tuple[float, float] | None = None
    detail = ""
    for cand in reversed(MARGIN_GRID):             # most conservative first
        wrong = unknown = ambiguous = 0
        per_speaker_ok = True
        rates: list[float] = []
        for truth, group in by_truth.items():
            correct = 0
            for t in group:
                top_pid = t.top_profile_id
                thr = thresholds.get(top_pid or "", None)
                # 1. does the top score clear ITS profile's threshold?
                if thr is not None and float(t.top_score) < float(thr):
                    unknown += 1
                    continue
                # 2. does the gap clear the candidate margin?
                gap = float(t.top_score) - float(t.second_score or 0.0)
                if gap < cand:
                    ambiguous += 1
                    continue
                if top_pid == truth:
                    correct += 1
                else:
                    wrong += 1
            rate = correct / len(group) if group else 0.0
            rates.append(rate)
            if rate < MIN_GENUINE_ACCEPT_RATE:
                per_speaker_ok = False
        if wrong == 0 and per_speaker_ok:
            best = (cand, min(rates))
            detail = (f"{cand:.2f}: 0 wrong-person calls, "
                      f">= {min(rates):.0%} correct for every speaker "
                      f"({unknown} unknown, {ambiguous} ambiguous)")
            break

    if best is None:
        return (None, 0.0, 0,
                "no margin gives 0 wrong-person calls with >= "
                f"{MIN_GENUINE_ACCEPT_RATE:.0%} correct per speaker")
    return (best[0], round(best[1], 4), 0, detail)


def calibrate(trials: Sequence[Trial],
              names: dict[str, str] | None = None) -> CalibrationResult:
    """Fit per-profile thresholds and one shared margin from labelled trials."""
    names = names or {}
    res = CalibrationResult()

    enrolled = sorted({t.truth for t in trials if t.truth})
    if len(enrolled) < 2:
        res.reason = f"need two real humans; trials cover {len(enrolled)}"
        return res

    for pid in enrolled:
        # Every score this person's OWN profile earned while they were speaking
        # — top or runner-up. Dropping a trial because the true speaker ranked
        # second discards exactly the hard cases the threshold exists to handle,
        # and biases the genuine distribution upward (V3 P5.2 closure).
        genuine = []
        for t in trials:
            if t.truth != pid:
                continue
            if t.top_profile_id == pid:
                genuine.append(t.top_score)
            elif t.second_profile_id == pid and t.second_score is not None:
                genuine.append(t.second_score)
        # Impostor evidence: every score this profile earned while SOMEBODY
        # ELSE was speaking — whether it ranked top or runner-up.
        #
        # Top-only is not enough, and missing that is a real gap: with two
        # enrolled people, a guest speaking makes Marcus the RUNNER-UP, never
        # the top match. Collecting only top scores leaves the second profile
        # with no impostor evidence at all and calibration refuses to fit it —
        # correctly, but for the wrong reason. The runner-up score is exactly
        # the number that would have to clear this profile's threshold for a
        # false accept, so it belongs in the bound.
        impostor = []
        for t in trials:
            if t.truth == pid:
                continue
            if t.top_profile_id == pid:
                impostor.append(t.top_score)
            elif t.second_profile_id == pid and t.second_score is not None:
                impostor.append(t.second_score)
        res.profiles.append(
            fit_profile_threshold(pid, names.get(pid, ""), genuine, impostor))

    # The margin is fitted against the thresholds that will actually ship.
    fitted = {p.profile_id: p.threshold for p in res.profiles
              if p.ok and p.threshold is not None}
    margin, rate, wrong, why = fit_margin(
        [t for t in trials if t.phase == "B"], fitted)
    res.margin, res.margin_correct_rate, res.margin_wrong_person = margin, rate, wrong

    bad = [p for p in res.profiles if not p.ok]
    if bad:
        res.reason = "; ".join(f"{p.display_name or p.profile_id}: {p.reason}" for p in bad)
        return res
    if margin is None:
        res.reason = why
        return res

    res.ok = True
    res.reason = why
    return res


# ── persistence ──────────────────────────────────────────────────────────────

CALIBRATION_DDL = [
    """
    CREATE TABLE IF NOT EXISTS speaker_calibration (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        protocol_version INTEGER NOT NULL,
        model_id TEXT NOT NULL,
        model_revision TEXT NOT NULL,
        embedding_dim INTEGER NOT NULL,
        margin REAL NOT NULL,
        profile_ids TEXT NOT NULL DEFAULT '[]',
        metrics TEXT NOT NULL DEFAULT '{}',
        calibrated_at TEXT NOT NULL
    );
    """,
]


@dataclass
class CalibrationRecord:
    margin: float
    profile_ids: list[str]
    metrics: dict[str, Any]
    model_id: str = MODEL_ID
    model_revision: str = MODEL_REVISION
    embedding_dim: int = EMBEDDING_DIM
    protocol_version: int = PROTOCOL_VERSION
    calibrated_at: str = field(default_factory=_now)

    @property
    def valid_for_build(self) -> bool:
        """Does this calibration still describe the model we actually load?

        A model or revision change invalidates it automatically. Scores from a
        different encoder are not comparable, so a threshold fitted on the old
        one is not merely stale — it is meaningless.
        """
        return (self.model_id == MODEL_ID
                and self.model_revision == MODEL_REVISION
                and self.embedding_dim == EMBEDDING_DIM
                and self.protocol_version == PROTOCOL_VERSION)


class CalibrationStore:
    """One row. Calibration is a property of this build, not a history."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)

    @asynccontextmanager
    async def _conn(self):
        async with aiosqlite.connect(str(self._db_path)) as db:
            db.row_factory = aiosqlite.Row
            yield db

    async def initialize(self) -> None:
        async with self._conn() as db:
            for sql in CALIBRATION_DDL:
                await db.execute(sql)
            await db.commit()

    async def save(self, rec: CalibrationRecord) -> None:
        await self.initialize()
        async with self._conn() as db:
            await db.execute(
                """INSERT OR REPLACE INTO speaker_calibration
                   (id, protocol_version, model_id, model_revision, embedding_dim,
                    margin, profile_ids, metrics, calibrated_at)
                   VALUES (1,?,?,?,?,?,?,?,?)""",
                (rec.protocol_version, rec.model_id, rec.model_revision,
                 rec.embedding_dim, float(rec.margin),
                 json.dumps(list(rec.profile_ids)),
                 json.dumps(rec.metrics, default=str), rec.calibrated_at),
            )
            await db.commit()
        logger.info("speaker_calibration_saved", margin=rec.margin,
                    profiles=len(rec.profile_ids))

    async def load(self) -> CalibrationRecord | None:
        await self.initialize()
        async with self._conn() as db:
            async with db.execute("SELECT * FROM speaker_calibration WHERE id=1") as cur:
                row = await cur.fetchone()
        if row is None:
            return None
        try:
            return CalibrationRecord(
                margin=float(row["margin"]),
                profile_ids=json.loads(row["profile_ids"] or "[]"),
                metrics=json.loads(row["metrics"] or "{}"),
                model_id=row["model_id"], model_revision=row["model_revision"],
                embedding_dim=int(row["embedding_dim"]),
                protocol_version=int(row["protocol_version"]),
                calibrated_at=row["calibrated_at"],
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("speaker_calibration_unreadable", error=str(e)[:160])
            return None

    async def clear(self) -> None:
        await self.initialize()
        async with self._conn() as db:
            await db.execute("DELETE FROM speaker_calibration WHERE id=1")
            await db.commit()


async def apply_atomically(db_path: Path, *, thresholds: dict[str, float],
                           record: "CalibrationRecord",
                           _fail_between: Any = None) -> None:
    """Write every fitted threshold AND the calibration record, or NOTHING.

    `SpeakerRegistry` and `CalibrationStore` share one SQLite file, so this is a
    single connection and a single commit rather than several independently
    committed writes with best-effort compensation.

    That distinction is not pedantic. The dangerous case is REPLACING a valid
    calibration: if the thresholds are written, the new record fails, and the
    rollback of one threshold also fails, the runtime is left holding the OLD
    record — which still matches the current profile ids — alongside MIXED
    threshold values. Every profile would then be judged by a number nobody
    fitted, and `threshold_calibrated` would say true. Compensation cannot
    guarantee its way out of that; a transaction can simply not enter it.

    A missing profile row is an error, not a silent skip: it means the
    population changed under us, which is exactly the defect this whole change
    exists to stop.

    `_fail_between` is a test seam — a callable invoked inside the transaction
    after the threshold updates and before the record write, so a suite can fail
    the second half and assert nothing survived. It is never passed in
    production.
    """
    expected = set(thresholds)
    # isolation_level=None disables Python's implicit transaction handling, so
    # BEGIN IMMEDIATE / COMMIT below are the only transaction boundaries.
    async with aiosqlite.connect(str(db_path), isolation_level=None) as db:
        db.row_factory = aiosqlite.Row
        # DDL first, outside the unit of work: CREATE TABLE commits implicitly
        # in SQLite and would otherwise end the transaction early.
        for sql in CALIBRATION_DDL:
            await db.execute(sql)
        try:
            # IMMEDIATE takes the write lock now, so the population cannot change
            # between the check below and the commit.
            await db.execute("BEGIN IMMEDIATE")

            # THE POPULATION, RE-READ UNDER THE LOCK. The router validated a
            # snapshot taken before this call; a profile enrolled in between
            # would leave the record naming {M,G} while the registry holds
            # {M,G,H}, and the runtime would immediately report it as not
            # covering. Narrow race, same invariant.
            #
            # The CANONICAL compatibility rule is applied here in Python rather
            # than approximated in SQL — `SpeakerProfile.compatible` also checks
            # the centroid's dimension, and a weaker second definition is
            # exactly the kind of drift this whole change exists to prevent.
            async with db.execute(
                "SELECT profile_id, model_id, model_revision, embedding_dim, "
                "centroid FROM speaker_profiles") as cur:
                rows = await cur.fetchall()
            live = set()
            for r in rows:
                try:
                    cen = json.loads(r["centroid"] or "[]")
                except Exception:  # noqa: BLE001
                    cen = []
                if (r["model_id"] == MODEL_ID
                        and r["model_revision"] == MODEL_REVISION
                        and int(r["embedding_dim"]) == EMBEDDING_DIM
                        and len(cen) == EMBEDDING_DIM):
                    live.add(r["profile_id"])
            if live != expected:
                raise RuntimeError(
                    f"the enrolled population changed during apply: "
                    f"expected {sorted(expected)}, found {sorted(live)}")

            for pid, value in thresholds.items():
                cur = await db.execute(
                    "UPDATE speaker_profiles SET threshold=?, updated_at=? "
                    "WHERE profile_id=?", (float(value), _now(), str(pid)))
                if cur.rowcount != 1:
                    raise RuntimeError(
                        f"profile {pid} is no longer in the registry — the "
                        f"enrolled population changed during apply")
            if _fail_between is not None:
                _fail_between()
            await db.execute(
                """INSERT OR REPLACE INTO speaker_calibration
                   (id, protocol_version, model_id, model_revision, embedding_dim,
                    margin, profile_ids, metrics, calibrated_at)
                   VALUES (1,?,?,?,?,?,?,?,?)""",
                (record.protocol_version, record.model_id, record.model_revision,
                 record.embedding_dim, float(record.margin),
                 json.dumps(list(record.profile_ids)),
                 json.dumps(record.metrics, default=str), record.calibrated_at),
            )
            await db.execute("COMMIT")
        except Exception:
            try:
                await db.execute("ROLLBACK")
            except Exception:  # noqa: BLE001
                pass
            raise
    logger.info("speaker_calibration_applied_atomically",
                profiles=len(thresholds), margin=record.margin)


# ── the ONE effective policy (V3 P5.2 closure) ───────────────────────────────

SOURCE_ENV = "env override"
SOURCE_CALIBRATED = "calibrated"
SOURCE_DEFAULT = "provisional default"


@dataclass
class EffectivePolicy:
    """The thresholds and margin a decision will ACTUALLY use, plus their source.

    Resolved once and handed to `match()`, rather than each layer deciding for
    itself. Two defects made that necessary:

      * the matcher preferred a stored per-profile threshold over an explicit
        `NOVA_SPEAKER_THRESHOLD`, inverting the documented precedence;
      * a profile keeps `threshold` in SQLite after its calibration record has
        gone stale, so a number nobody stands behind kept deciding.

    So a stored threshold is INERT unless a valid calibration currently covers
    the whole compatible population. The row may stay — it is useful history —
    but it does not vote.
    """

    per_profile: dict[str, float] = field(default_factory=dict)
    default_threshold: float = 0.55
    margin: float = 0.10
    threshold_source: str = SOURCE_DEFAULT
    margin_source: str = SOURCE_DEFAULT
    calibrated: bool = False

    def threshold_for(self, profile_id: str | None) -> float:
        if self.threshold_source == SOURCE_CALIBRATED and profile_id:
            return float(self.per_profile.get(profile_id, self.default_threshold))
        return float(self.default_threshold)


def calibration_covers(rec: "CalibrationRecord | None", profiles: Sequence[Any]) -> bool:
    """Does this calibration describe the population Nova has RIGHT NOW?

    EXACT SET EQUALITY, not containment (V3 P5.2 final closure):

        set(rec.profile_ids) == {every current compatible profile id}

    and every one of them carries a fitted threshold.

    Containment was wrong in the direction that matters. A fit over
    {Marcus, Guest} still "covered" a population of {Marcus} alone, so deleting
    the guest left Marcus judged by a threshold whose entire justification was
    the impostor evidence that guest provided. The fit's false-accept bound came
    from a voice that is no longer enrolled — the number survived the evidence
    for it.

    This is also the backstop for a failed clear: if `_invalidate_calibration()`
    cannot delete the row (disk error, locked database), the stale row is still
    read on the next boot. Set equality makes it INERT rather than trusted, so
    the failure mode of the delete is a fall back to provisional defaults rather
    than a silent stale claim. Fail closed.
    """
    if rec is None or not rec.valid_for_build:
        return False
    compatible = [p for p in profiles if getattr(p, "compatible", False)]
    if not compatible:
        return False
    if set(rec.profile_ids) != {p.profile_id for p in compatible}:
        return False
    return all(p.threshold is not None for p in compatible)


def resolve_policy(profiles: Sequence[Any], rec: "CalibrationRecord | None",
                   *, env_threshold: float | None, env_margin: float | None,
                   default_threshold: float, default_margin: float) -> EffectivePolicy:
    """env override -> valid calibration -> provisional default. In that order."""
    covers = calibration_covers(rec, profiles)

    if env_threshold is not None:
        pol_thresh, thresh_src, per = default_threshold, SOURCE_ENV, {}
        pol_thresh = float(env_threshold)
    elif covers:
        pol_thresh, thresh_src = default_threshold, SOURCE_CALIBRATED
        per = {p.profile_id: float(p.threshold) for p in profiles
               if getattr(p, "compatible", False) and p.threshold is not None}
    else:
        pol_thresh, thresh_src, per = float(default_threshold), SOURCE_DEFAULT, {}

    if env_margin is not None:
        margin_val, margin_src = float(env_margin), SOURCE_ENV
    elif covers and rec is not None:
        margin_val, margin_src = float(rec.margin), SOURCE_CALIBRATED
    else:
        margin_val, margin_src = float(default_margin), SOURCE_DEFAULT

    return EffectivePolicy(per_profile=per, default_threshold=pol_thresh,
                           margin=margin_val, threshold_source=thresh_src,
                           margin_source=margin_src, calibrated=bool(covers))
