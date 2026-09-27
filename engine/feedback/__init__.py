"""M13: confidence calibration from confirmed VASP-cooperation outcomes.

The raw confidence model (M6) is a product of heuristics — it has never
seen ground truth. Each time a cooperation request resolves, the officer
records whether the attributed VASP confirmed the wallet as theirs.
fit_calibration() turns those outcomes into a versioned, monotonic
calibration curve: predicted decile -> empirical confirmation rate,
shrunk toward the identity line when samples are thin.

Safety properties:
- No outcomes (or too few per bucket) -> the identity function: scores
  are unchanged until real evidence arrives.
- Monotonicity is enforced (PAVA): a higher predicted confidence can
  never calibrate to a lower value than a lower one.
- Models are versioned and immutable; historical reports keep the
  version they were generated with. Nothing is silently rewritten.
"""
from __future__ import annotations

from __future__ import annotations

from datetime import datetime

from ..store.base import CalibrationModelRec, FeedbackOutcomeRec

N_BUCKETS = 10
MIN_PER_BUCKET = 5   # fewer samples -> bucket stays on the identity line
SHRINK_K = 10        # shrinkage strength toward identity
BUCKET_CENTERS = tuple((i + 0.5) / N_BUCKETS for i in range(N_BUCKETS))


def _bucket_of(predicted: float) -> int:
    p = max(0.0, min(1.0, predicted))
    return min(N_BUCKETS - 1, int(p * N_BUCKETS))


def _pava(values: list[float], weights: list[float]) -> list[float]:
    """Pool-adjacent-violators: smallest non-decreasing adjustment of
    `values` under `weights`."""
    blocks: list[list] = []  # [indices, sum(w*v), sum(w)]
    for i, (v, w) in enumerate(zip(values, weights)):
        blocks.append([[i], v * w, w])
        while len(blocks) >= 2 and \
                blocks[-2][1] / blocks[-2][2] > \
                blocks[-1][1] / blocks[-1][2] + 1e-12:
            idx2, s2, w2 = blocks.pop()
            idx1, s1, w1 = blocks.pop()
            blocks.append([idx1 + idx2, s1 + s2, w1 + w2])
    result = [0.0] * len(values)
    for idxs, total, wsum in blocks:
        mean = total / wsum
        for i in idxs:
            result[i] = mean
    return result


def fit_calibration(
    outcomes: list[FeedbackOutcomeRec],
    *,
    version: str,
    created_by: str,
    created_at: datetime | None = None,
) -> CalibrationModelRec:
    """Fit one calibration model from confirmed/refuted outcomes.

    `inconclusive` outcomes carry no signal and are excluded.
    """
    from datetime import timezone

    usable = [o for o in outcomes if o.outcome in ("confirmed", "refuted")]
    counts = [0] * N_BUCKETS
    confirmed = [0] * N_BUCKETS
    for o in usable:
        b = _bucket_of(o.predicted_confidence)
        counts[b] += 1
        if o.outcome == "confirmed":
            confirmed[b] += 1

    raw_values: list[float] = []
    for i in range(N_BUCKETS):
        center = BUCKET_CENTERS[i]
        n = counts[i]
        if n < MIN_PER_BUCKET:
            raw_values.append(center)  # identity: not enough evidence
            continue
        empirical = confirmed[i] / n
        w = n / (n + SHRINK_K)
        raw_values.append(w * empirical + (1 - w) * center)

    weights = [c + 1 for c in counts]  # every bucket pulls, thin ones less
    values = _pava(raw_values, weights)
    values = [round(max(0.01, min(0.99, v)), 4) for v in values]

    return CalibrationModelRec(
        version=version,
        created_at=created_at or datetime.now(timezone.utc),
        created_by=created_by,
        n_outcomes=len(usable),
        bucket_values=tuple(values),
        bucket_counts=tuple(counts),
    )


def calibrate_confidence(
    model: CalibrationModelRec | None, raw: float,
) -> tuple[float, str | None]:
    """Map a raw confidence through the calibration curve.

    Returns (value, model version). With no model (or an empty one),
    returns the raw value unchanged — the identity.
    """
    raw = max(0.0, min(1.0, raw))
    if model is None or model.n_outcomes == 0:
        return round(raw, 4), None
    centers = BUCKET_CENTERS
    vals = model.bucket_values
    if raw <= centers[0]:
        return round(vals[0], 4), model.version
    if raw >= centers[-1]:
        return round(vals[-1], 4), model.version
    for i in range(N_BUCKETS - 1):
        if centers[i] <= raw <= centers[i + 1]:
            frac = ((raw - centers[i]) / (centers[i + 1] - centers[i]))
            v = vals[i] + frac * (vals[i + 1] - vals[i])
            return round(v, 4), model.version
    return round(raw, 4), model.version  # unreachable; defensive


def describe_model(model: CalibrationModelRec) -> dict:
    return {
        "version": model.version,
        "created_at": model.created_at.isoformat(),
        "created_by": model.created_by,
        "n_outcomes": model.n_outcomes,
        "buckets": [
            {"decile": f"{i / 10:.1f}-{(i + 1) / 10:.1f}",
             "calibrated": v, "samples": c}
            for i, (v, c) in enumerate(
                zip(model.bucket_values, model.bucket_counts))
        ],
    }
