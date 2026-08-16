from __future__ import annotations

"""Open-set matching, and the quality rules that decide what may be enrolled.

WHY NOT argmax
--------------
`argmax(scores)` always names somebody. Point a stranger at a registry with one
enrolled speaker and the stranger IS that speaker, at whatever score falls out.
Open-set identification has to be able to answer "nobody here", which means a
threshold — and a threshold alone is still not enough:

    Marcus .81   Alice .79     -> passes a .75 threshold, means nothing.
                                  Two people score alike; that is AMBIGUOUS.
    Marcus .86   Alice .31     -> passes, and the runner-up is nowhere near.
                                  That is evidence.

So the decision uses the top score, the threshold, AND the margin to the
second-best. Five outcomes, deliberately distinct, because "I don't know" and
"nobody" and "the model isn't running" are different things a caller may want to
handle differently:

    known | unknown | ambiguous | too_short | unavailable

THRESHOLDS ARE PROVISIONAL
--------------------------
The numbers below are starting points chosen from the measured separation on
this machine (same-speaker 0.78, cross-speaker 0.47 on the fixtures available
offline). They are NOT calibrated: real calibration needs Marcus's genuine score
distribution and a real impostor's, which requires humans and is what
`tests/live_speaker_id_harness.md` exists to collect.

Until then the bias is deliberate: a guest wrongly accepted as Marcus is a much
worse failure than an honest `unknown`, because the first silently writes a
stranger's facts into Marcus's memory and the second just asks.
"""

import os
from dataclasses import dataclass, field
from typing import Any, Iterable

import numpy as np

from core.speaker.registry import SpeakerProfile

#: Cosine similarity a match must clear to be considered at all. PROVISIONAL.
DEFAULT_THRESHOLD = 0.55

#: How far ahead of the runner-up the winner must be. Below this the two
#: candidates are not distinguishable and Nova says so instead of picking.
#: PROVISIONAL.
DEFAULT_MARGIN = 0.10

STATUS_KNOWN = "known"
STATUS_UNKNOWN = "unknown"
STATUS_AMBIGUOUS = "ambiguous"
STATUS_TOO_SHORT = "too_short"
STATUS_UNAVAILABLE = "unavailable"


def threshold() -> float:
    try:
        return float(os.getenv("NOVA_SPEAKER_THRESHOLD", "").strip() or DEFAULT_THRESHOLD)
    except ValueError:
        return DEFAULT_THRESHOLD


def margin() -> float:
    try:
        return float(os.getenv("NOVA_SPEAKER_MARGIN", "").strip() or DEFAULT_MARGIN)
    except ValueError:
        return DEFAULT_MARGIN


@dataclass
class SpeakerMatch:
    """What Nova concluded about who just spoke. Never an authorisation."""

    status: str = STATUS_UNAVAILABLE
    profile_id: str | None = None
    display_name: str | None = None
    similarity: float | None = None
    second_best_similarity: float | None = None
    second_best_name: str | None = None
    threshold: float | None = None
    #: Where `threshold` came from: "profile" (calibrated) or "default"
    #: (global/env fallback). Diagnostics only — never reaches a prompt.
    threshold_source: str | None = None
    margin: float | None = None
    second_best_profile_id: str | None = None
    reason: str = ""
    #: Was speaker identification actually ATTEMPTED for this turn? (V3 P5.1a)
    #:
    #: This is the difference between two things that both look like "no
    #: identity" and must never be conflated once attribution is wired:
    #:
    #:   attempted=False  the feature is OFF. Legacy Nova. There is no speaker
    #:                    question being asked, and no unverified-voice state.
    #:   attempted=True   the feature is ON and Nova tried. Whatever came back —
    #:                    including `unavailable` — is a real backend-derived
    #:                    outcome for a real voice command.
    #:
    #: Absence of metadata must never silently read as "Marcus". `attempted`
    #: is what keeps "we could not tell" distinguishable from "nobody asked".
    attempted: bool = False

    @property
    def is_known(self) -> bool:
        return self.status == STATUS_KNOWN and bool(self.profile_id)

    def for_response(self, *, model_id: str) -> dict[str, Any]:
        """The shape /stt returns. Contains no embedding, ever."""
        return {
            "status": self.status,
            # Structured diagnostics: WHY there is no identity is actionable
            # ("empty_transcript" vs "disabled" vs "no embedding"), and it
            # exposes nothing biometric.
            "reason": self.reason or None,
            "attempted": self.attempted,
            "profile_id": self.profile_id,
            "display_name": self.display_name,
            "similarity": round(self.similarity, 4) if self.similarity is not None else None,
            "second_best_similarity": (round(self.second_best_similarity, 4)
                                       if self.second_best_similarity is not None else None),
            "threshold": self.threshold,
            "threshold_source": self.threshold_source,
            "second_best_profile_id": self.second_best_profile_id,
            "model_id": model_id,
        }

    def for_prompt(self) -> str:
        """Compact identity for conversational context — no biometric scores.

        The model needs to know who it is talking to, not how confident a cosine
        similarity was. Scores belong in diagnostics.
        """
        if self.status == STATUS_KNOWN and self.display_name:
            return f"Speaking: {self.display_name}"
        if self.status == STATUS_AMBIGUOUS:
            return "Speaking: someone Nova cannot identify with confidence"
        if self.status == STATUS_UNKNOWN:
            return "Speaking: an unrecognised voice (not an enrolled speaker)"
        return ""


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    """Both sides are stored L2-normalised, so this is a dot product."""
    if a is None or b is None or a.size != b.size:
        return -1.0
    na, nb = float(np.linalg.norm(a)), float(np.linalg.norm(b))
    if na < 1e-9 or nb < 1e-9:
        return -1.0
    return float(np.dot(a, b) / (na * nb))


def match(embedding: np.ndarray | None, profiles: Iterable[SpeakerProfile],
          *, thresh: float | None = None, min_margin: float | None = None) -> SpeakerMatch:
    """Score against every compatible profile and decide honestly."""
    thresh = threshold() if thresh is None else thresh
    min_margin = margin() if min_margin is None else min_margin

    if embedding is None:
        return SpeakerMatch(status=STATUS_UNAVAILABLE, reason="no embedding")

    scored: list[tuple[float, SpeakerProfile]] = []
    for p in profiles:
        # An incompatible profile is skipped, not scored badly. Comparing across
        # models would produce a number, and the number would be meaningless.
        if not p.compatible:
            continue
        scored.append((cosine(embedding, p.centroid), p))

    if not scored:
        return SpeakerMatch(status=STATUS_UNKNOWN, threshold=thresh, margin=min_margin,
                            reason="no enrolled profiles for this model")

    scored.sort(key=lambda kv: kv[0], reverse=True)
    top_score, top = scored[0]
    second_score, second = (scored[1] if len(scored) > 1 else (None, None))

    # A per-profile threshold wins if calibration produced one.
    effective = top.threshold if top.threshold is not None else thresh

    result = SpeakerMatch(
        similarity=top_score, second_best_similarity=second_score,
        second_best_name=second.display_name if second else None,
        second_best_profile_id=second.profile_id if second else None,
        # The EFFECTIVE threshold, not the global fallback (V3 P5.2). Reporting
        # the fallback while deciding on a calibrated per-profile value made the
        # diagnostic describe a decision that was never made.
        threshold=effective,
        threshold_source=("profile" if top.threshold is not None else "default"),
        margin=min_margin,
    )

    if top_score < effective:
        result.status = STATUS_UNKNOWN
        result.reason = f"best {top_score:.3f} below threshold {effective:.3f}"
        return result

    if second_score is not None and (top_score - second_score) < min_margin:
        # Passed the bar, but so did someone else, close behind. Naming one of
        # them would be a coin toss wearing a confidence score.
        result.status = STATUS_AMBIGUOUS
        result.reason = (f"{top.display_name} {top_score:.3f} vs "
                         f"{second.display_name} {second_score:.3f} "
                         f"within margin {min_margin:.3f}")
        return result

    result.status = STATUS_KNOWN
    result.profile_id = top.profile_id
    result.display_name = top.display_name
    result.reason = f"{top_score:.3f} clears {effective:.3f}"
    return result


# ── enrollment quality ───────────────────────────────────────────────────────

#: An enrollment sample must carry this much audio to be worth embedding.
MIN_SAMPLE_S = 1.5
#: RMS below this is a silent or near-silent room, not speech.
MIN_RMS = 0.008
#: Above this fraction of samples at full scale, the mic was clipping.
MAX_CLIP_FRACTION = 0.02
#: How alike enrollment samples must be to each other. Below this the person
#: recorded inconsistently, or two different people recorded, and a centroid
#: built from them would match neither well.
MIN_CONSISTENCY = 0.55
#: A single sample this far below the others is an outlier and is rejected.
OUTLIER_DROP = 0.35


@dataclass
class SampleCheck:
    ok: bool
    reason: str = ""
    rms: float = 0.0
    duration_s: float = 0.0
    clipped: float = 0.0


def check_sample(audio: np.ndarray, sample_rate: int) -> SampleCheck:
    """Reject unusable enrollment audio before it ever reaches the model.

    Cheap, deterministic signal checks — no VAD model, no LLM. A profile built
    from silence would still produce a centroid, and that centroid would match
    silence.
    """
    x = np.asarray(audio, dtype=np.float32)
    if x.ndim > 1:
        x = x.mean(axis=1)
    if x.size == 0 or not np.isfinite(x).all():
        return SampleCheck(False, "empty or malformed audio")
    dur = x.size / max(sample_rate, 1)
    rms = float(np.sqrt(np.mean(np.square(x))))
    clipped = float(np.mean(np.abs(x) >= 0.999))
    if dur < MIN_SAMPLE_S:
        return SampleCheck(False, f"too short ({dur:.2f}s, need {MIN_SAMPLE_S}s)",
                           rms, dur, clipped)
    if rms < MIN_RMS:
        return SampleCheck(False, f"too quiet (rms {rms:.4f})", rms, dur, clipped)
    if clipped > MAX_CLIP_FRACTION:
        return SampleCheck(False, f"clipping ({clipped * 100:.1f}% at full scale)",
                           rms, dur, clipped)
    return SampleCheck(True, "ok", rms, dur, clipped)


@dataclass
class EnrollmentResult:
    ok: bool
    reason: str = ""
    centroid: np.ndarray | None = None
    kept: list[np.ndarray] = field(default_factory=list)
    dropped: list[int] = field(default_factory=list)
    consistency: float | None = None
    pairwise: list[float] = field(default_factory=list)


def build_profile_embedding(embeddings: list[np.ndarray],
                            *, min_samples: int = 3) -> EnrollmentResult:
    """Turn several enrollment embeddings into one profile representation.

    A centroid of many samples, not one arbitrary utterance: a single recording
    encodes whatever Marcus's voice was doing in those three seconds, and the
    match then depends on him repeating that mood.

    Consistency is checked with leave-one-out similarity — each sample against
    the centroid of the others — which catches the sample that does not belong
    without being fooled by its own contribution to the mean.
    """
    vecs = [np.asarray(e, dtype=np.float32) for e in embeddings if e is not None]
    if len(vecs) < min_samples:
        return EnrollmentResult(False, f"need at least {min_samples} usable samples, got {len(vecs)}")

    def centroid_of(items: list[np.ndarray]) -> np.ndarray:
        c = np.mean(np.stack(items), axis=0)
        n = float(np.linalg.norm(c))
        return (c / n).astype(np.float32) if n > 1e-9 else c.astype(np.float32)

    loo = []
    for i in range(len(vecs)):
        others = vecs[:i] + vecs[i + 1:]
        loo.append(cosine(vecs[i], centroid_of(others)))

    best = max(loo) if loo else 0.0
    dropped = [i for i, s in enumerate(loo) if s < best - OUTLIER_DROP]
    kept = [v for i, v in enumerate(vecs) if i not in dropped]

    if len(kept) < min_samples:
        return EnrollmentResult(
            False,
            f"samples disagree too much — {len(dropped)} outlier(s) of {len(vecs)}; "
            "re-record in one sitting, in the same place",
            dropped=dropped, pairwise=loo)

    kept_loo = [s for i, s in enumerate(loo) if i not in dropped]
    consistency = float(np.mean(kept_loo)) if kept_loo else 0.0
    if consistency < MIN_CONSISTENCY:
        return EnrollmentResult(
            False,
            f"enrollment is inconsistent (mean self-similarity {consistency:.2f}, "
            f"need {MIN_CONSISTENCY:.2f}) — re-record with steady, natural speech",
            dropped=dropped, consistency=consistency, pairwise=loo)

    return EnrollmentResult(True, "ok", centroid=centroid_of(kept), kept=kept,
                            dropped=dropped, consistency=consistency, pairwise=loo)
