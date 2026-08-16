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

#: Candidate grids. Coarse enough to stay deterministic and explainable, fine
#: enough to matter: 0.01 is well below the run-to-run spread of a real voice.
THRESHOLD_GRID = [round(0.30 + 0.01 * i, 2) for i in range(61)]      # .30 … .90
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
        fit.reason = (
            f"no threshold gives 0 false accepts and >= "
            f"{MIN_GENUINE_ACCEPT_RATE:.0%} acceptance: genuine p05 "
            f"{fit.genuine_p05:.3f} vs impostor max {fit.impostor_max:.3f} "
            f"(separation {fit.separation:+.3f}) — the distributions overlap"
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
