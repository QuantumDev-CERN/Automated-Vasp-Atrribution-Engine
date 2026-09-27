"""Scoring engines (M6): path confidence + risk.

confidence.score_attribution() — composite path confidence: product of
  (classifier confidence x kind discount) per hop, adjusted for the
  terminal (sweep spikes it, mixer craters it, bridge barely dents it
  with an explicit destination).
risk.score_risk() — additive 0-100 signal model over observable hop
  classifications. OFAC/ransomware/scam-list proximity is deliberately
  NOT scored until the M7 dataset ingestion exists.
"""
from .confidence import (
    KIND_DISCOUNT,
    HopScore,
    AttributionScore,
    score_attribution,
)
from .risk import (
    RiskSignal,
    RiskScore,
    score_risk,
)

__all__ = [
    "KIND_DISCOUNT",
    "HopScore",
    "AttributionScore",
    "score_attribution",
    "RiskSignal",
    "RiskScore",
    "score_risk",
]
