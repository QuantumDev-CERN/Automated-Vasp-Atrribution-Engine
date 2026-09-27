"""Bridge / mixer withdrawal correlation (M4, master plan section 6).

Given a known deposit (bridge-lock or mixer-deposit hop), score candidate
withdrawal txs by temporal proximity + amount match. This is the
*scoring* layer — pure functions over caller-supplied candidates. Live
cross-chain candidate discovery (indexer scans on the destination chain)
is M7 worker territory; the scoring math is what M4 pins down.

Conventions:
- amounts are smallest-unit ints as strings; compare as ints
- block_time may be None — then temporal scoring is skipped (amount only)
- returns None when the candidate is outside the window/tolerance:
  "no link" is a valid answer and must be representable
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class Deposit:
    tx_hash: str
    chain: str
    address: str        # depositor
    asset_contract: Optional[str]  # None = native
    asset_symbol: Optional[str]
    value: str          # smallest units
    block_time: Optional[datetime]
    venue: str          # bridge/mixer name, e.g. "stargate", "tornado-cash"


@dataclass
class WithdrawalCandidate:
    tx_hash: str
    chain: str
    address: str        # recipient
    asset_contract: Optional[str]
    asset_symbol: Optional[str]
    value: str
    block_time: Optional[datetime]


def _norm_contract(c: Optional[str]) -> str:
    return (c or "").lower()


def score_candidate(
    deposit: Deposit,
    cand: WithdrawalCandidate,
    time_window_s: int = 3600,
    amount_tolerance: float = 0.03,
) -> Optional[float]:
    """0..1 link score, or None when the candidate cannot match.

    Asset identity must match (same contract, or both native with the same
    symbol). Amount must be within tolerance (bridges/mixers take fees, so
    the withdrawal is usually slightly *less*). Time must fall inside the
    window after the deposit. Score blends amount closeness (2/3) and
    temporal proximity (1/3).
    """
    if _norm_contract(deposit.asset_contract) != _norm_contract(cand.asset_contract):
        return None
    if not deposit.asset_contract and deposit.asset_symbol != cand.asset_symbol:
        return None  # native legs: symbols must agree too

    try:
        dep_v, cand_v = int(deposit.value), int(cand.value)
    except (TypeError, ValueError):
        return None
    if dep_v <= 0 or cand_v <= 0:
        return None
    # withdrawal should not exceed the deposit (plus tolerance for noise)
    if cand_v > dep_v * (1 + amount_tolerance):
        return None
    amount_score = 1.0 - min(1.0, abs(dep_v - cand_v) / (dep_v * amount_tolerance))

    time_score = 0.5  # neutral when either side lacks a timestamp
    if deposit.block_time and cand.block_time:
        dt = (cand.block_time - deposit.block_time).total_seconds()
        if dt < 0 or dt > time_window_s:
            return None
        time_score = 1.0 - dt / time_window_s

    return round(2 / 3 * amount_score + 1 / 3 * time_score, 3)


def rank_candidates(
    deposit: Deposit,
    candidates: list[WithdrawalCandidate],
    time_window_s: int = 3600,
    amount_tolerance: float = 0.03,
) -> list[tuple[WithdrawalCandidate, float]]:
    """Candidates that can match, best score first."""
    scored = []
    for cand in candidates:
        s = score_candidate(deposit, cand, time_window_s, amount_tolerance)
        if s is not None:
            scored.append((cand, s))
    scored.sort(key=lambda t: t[1], reverse=True)
    return scored
