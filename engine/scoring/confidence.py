"""Path-based confidence scoring (M6).

Composite, not single-hop: overall attribution confidence is the product
over the traced path of (classifier confidence x kind discount), then
adjusted for how the trail terminated — exactly the discounting model in
the master reference (section 4, item 6):

  mixer hop craters confidence; bridge w/ explicit destination barely
  dents it; confirmed sweep spikes it.

Inputs are the traversal engine's own outputs (VisitedNode.via_kind /
via_confidence, Terminal.reason), so scoring stays honest about what the
engine actually observed. No invented signals.
"""
from dataclasses import dataclass, field

from ..feedback import calibrate_confidence
from ..store.base import CalibrationModelRec

# Per-hop-kind discount factors. Rationale per kind:
KIND_DISCOUNT: dict[str, float] = {
    # plain value movement, no heuristic involved
    "direct-transfer": 1.00,
    # Swap event decoded on-chain: same trader, new asset (M4)
    "dex-swap": 0.95,
    # change-branch heuristic can misfire on ambiguous 2-output txs (M3)
    "peel": 0.90,
    # terminal hop; the boost comes from the sweep-consolidation terminal
    "sweep-candidate": 1.00,
    # value left the chain. Correlation-only linkage is weaker than an
    # explicitly parsed destination address (M4 correlation.py).
    "bridge-lock": 0.85,
    # funds entered the anonymity set: linkage is probabilistic (M4)
    "mixer-deposit": 0.40,
    # CoinJoin anonymity set: deterministic linkage is broken like a
    # mixer, but denominations stay visible on-chain, leaving room for
    # probabilistic clustering — slightly less punitive than 0.40 (M20)
    "coinjoin": 0.50,
    # custodial swap, no on-chain linkage proof (M4 terminal label)
    "swap-service": 0.60,
}

_DEFAULT_CLASSIFIER_CONFIDENCE = 0.70  # via_confidence missing (defensive)
_SWEEP_TERMINAL_FLOOR = 0.80           # confirmed sweep spikes confidence
_BRIDGE_EXPLICIT_DESTINATION_DISCOUNT = 0.95  # barely dents it


@dataclass(frozen=True)
class HopScore:
    address: str
    kind: str
    classifier_confidence: float
    discount: float
    cumulative: float


@dataclass(frozen=True)
class AttributionScore:
    hops: tuple[HopScore, ...]
    overall: float            # 0..1
    terminal_reason: str | None
    notes: tuple[str, ...] = ()
    calibration_version: str | None = None  # M13: e.g. "cal-3"; None = raw


def score_attribution(visited: list,
                      terminal_reason: str | None = None,
                      bridge_explicit_destination: bool = False,
                      calibration: CalibrationModelRec | None = None,
                      ) -> AttributionScore:
    """Score one traced path.

    visited: TraversalResult.visited (hop 0 = subject, no via_kind).
    terminal_reason: Terminal.reason string, if the trail terminated.
    bridge_explicit_destination: True when the bridge hop carried an
        explicitly parsed destination address (strong correlation).
    calibration: M13 CalibrationModelRec — maps the final overall through
        the empirical curve. None (or an empty model) leaves the score
        unchanged.
    """
    hops: list[HopScore] = []
    cumulative = 1.0
    notes: list[str] = []

    for node in visited:
        kind = getattr(node, "via_kind", None)
        if not kind:  # hop 0: the subject itself, nothing to discount
            continue
        conf = getattr(node, "via_confidence", None)
        if conf is None:
            conf = _DEFAULT_CLASSIFIER_CONFIDENCE
            notes.append(f"hop to {node.address[:10]}…: classifier "
                         "confidence missing, assumed 0.70")
        discount = KIND_DISCOUNT.get(kind, 0.70)
        if kind == "bridge-lock" and bridge_explicit_destination:
            discount = _BRIDGE_EXPLICIT_DESTINATION_DISCOUNT
        cumulative *= conf * discount
        hops.append(HopScore(
            address=node.address,
            kind=kind,
            classifier_confidence=round(conf, 4),
            discount=discount,
            cumulative=round(cumulative, 4),
        ))

    overall = cumulative
    if terminal_reason == "sweep-consolidation" and overall < _SWEEP_TERMINAL_FLOOR:
        notes.append("confirmed sweep at consolidation wallet: attribution "
                     "confidence raised to 0.80 floor")
        overall = _SWEEP_TERMINAL_FLOOR
    if terminal_reason == "mixer-deposit":
        notes.append("funds entered the mixer anonymity set: attribution "
                     "is probabilistic, not deterministic")
    if terminal_reason == "coinjoin":
        notes.append("funds entered a CoinJoin anonymity set: deterministic "
                     "unmixing is not claimed — attribution past this point "
                     "is probabilistic, not deterministic")
    if terminal_reason == "bridge-lock":
        notes.append("value left the chain at a bridge: cross-chain "
                     "continuation needs correlation, not proof")
    if terminal_reason == "otc-hawala-terminus":
        notes.append("funds appear to have exited the on-chain world at an "
                     "OTC/hawala collection wallet: no further on-chain "
                     "trail is expected — a recognized terminus, not a "
                     "failed trace")
    if terminal_reason == "max-hops":
        notes.append("trail truncated at max-hops: confidence understates "
                     "a longer path")

    # M13: empirical calibration is the LAST step — outcomes are recorded
    # against the final reported number, so the curve must map it.
    calibration_version: str | None = None
    if calibration is not None:
        calibrated, calibration_version = calibrate_confidence(
            calibration, overall)
        if calibration_version is not None:
            notes.append(
                f"confidence calibrated by {calibration_version} "
                f"(empirical VASP-confirmation curve): "
                f"{overall:.4f} -> {calibrated:.4f}")
            overall = calibrated

    return AttributionScore(
        hops=tuple(hops),
        overall=round(max(0.0, min(1.0, overall)), 4),
        terminal_reason=terminal_reason,
        notes=tuple(notes),
        calibration_version=calibration_version,
    )
