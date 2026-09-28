"""Structuring / smurfing risk signals (M23).

Structuring = breaking value into smaller pieces to evade reporting or
monitoring thresholds. The on-chain-observable shapes are:

1. fan-out burst: many similar-sized payouts from one address inside a
   short window (distribution to mules / structured withdrawals).
2. fan-in burst: many similar-sized deposits into one address inside a
   short window (collection from mules / structured deposits).
3. sub-threshold cluster: several transfers just below a round-number
   amount — the classic "just under the reporting line" signature.

Only native-asset transfers are analyzed: "round numbers" are only
meaningful in asset units, and token smallest-unit amounts carry no
round-number psychology. Analysis is strictly limited to transactions
already ingested (the traced graph) plus one bounded address-history
call for the terminal — we never score what we have not seen.

Thresholds are conservative by design. A legitimate batch payer
(payroll, mining pool, faucet) can also emit similar-sized bursts, so
findings are risk signals (points), not verdicts, and every reason
string states the raw counts behind it.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from ..adapters.base import AssetKind, CanonicalTx

# Native decimals per chain (for smallest-unit -> asset-unit conversion).
_NATIVE_DECIMALS = {
    "ethereum": 18, "bsc": 18, "polygon": 18,
    "tron": 6, "solana": 9, "bitcoin": 8,
}

WINDOW_HOURS = 24.0
MIN_BURST_COUNT = 8      # min similar-sized transfers for a fan finding
MAX_CV = 0.30            # max coefficient of variation ("similar-sized")
MIN_SUBTHRESHOLD_COUNT = 5
SUBTHRESHOLD_BAND = 0.85  # amounts in [0.85*R, R) count as "just below R"
ROUND_NUMBERS = (1, 10, 100, 1000, 10000)

POINTS_FAN = 15
POINTS_SUBTHRESHOLD = 10


@dataclass(frozen=True)
class StructuringFinding:
    pattern: str  # "fan-out-burst" | "fan-in-burst" | "sub-threshold"
    direction: str  # "out" | "in" | "both"
    address: str
    count: int
    window_hours: float = WINDOW_HOURS
    asset_symbol: str = ""
    amount_min: float = 0.0  # asset units
    amount_max: float = 0.0  # asset units
    amount_mean: float = 0.0  # asset units
    cv: float = 0.0
    round_number: Optional[float] = None  # sub-threshold only
    example_txs: tuple[str, ...] = ()
    points: int = 0


@dataclass
class _Leg:
    tx: CanonicalTx
    when: datetime
    amount_units: float  # native asset units


def _leg_amount(tx: CanonicalTx, address: str, direction: str) -> int:
    """Counterparty value on the leg, smallest units."""
    addr = address.lower()
    if direction == "out":
        parties = [p for p in tx.outputs if p.address.lower() != addr]
        pool = tx.outputs
    else:
        parties = [p for p in tx.inputs if p.address.lower() != addr]
        pool = tx.inputs
    total = sum(int(p.value or 0) for p in parties if p.value)
    if total == 0:  # EVM-style: value duplicated on the single in/out party
        total = sum(int(p.value or 0) for p in pool if p.value)
    return total


def _legs(txs: list[CanonicalTx], address: str,
          direction: str) -> list[_Leg]:
    addr = address.lower()
    out: list[_Leg] = []
    for tx in txs:
        if tx.block_time is None:
            continue
        if tx.asset.kind != AssetKind.NATIVE:
            continue
        sides = (tx.inputs if direction == "out" else tx.outputs)
        if not any(p.address.lower() == addr for p in sides):
            continue
        decimals = _NATIVE_DECIMALS.get(tx.asset.chain.value, 18)
        amount = _leg_amount(tx, address, direction) / (10 ** decimals)
        if amount <= 0:
            continue
        out.append(_Leg(tx=tx, when=tx.block_time, amount_units=amount))
    out.sort(key=lambda l: l.when)
    return out


def _best_window(legs: list[_Leg]) -> list[_Leg]:
    """Densest WINDOW_HOURS slice (sliding window, O(n^2), n bounded)."""
    best: list[_Leg] = []
    for i, start in enumerate(legs):
        window = [l for l in legs[i:]
                  if (l.when - start.when).total_seconds()
                  <= WINDOW_HOURS * 3600]
        if len(window) > len(best):
            best = window
    return best


def _cv(amounts: list[float]) -> float:
    mean = statistics.fmean(amounts)
    if mean == 0 or len(amounts) < 2:
        return 0.0
    return statistics.pstdev(amounts) / mean


def _describe(legs: list[_Leg], symbol: str) -> dict:
    amounts = [l.amount_units for l in legs]
    return {
        "amount_min": round(min(amounts), 6),
        "amount_max": round(max(amounts), 6),
        "amount_mean": round(statistics.fmean(amounts), 6),
        "cv": round(_cv(amounts), 4),
        "example_txs": tuple(l.tx.tx_hash for l in legs[:3]),
        "asset_symbol": symbol,
    }


def _fan_finding(legs: list[_Leg], address: str, direction: str,
                 symbol: str) -> Optional[StructuringFinding]:
    window = _best_window(legs)
    if len(window) < MIN_BURST_COUNT:
        return None
    amounts = [l.amount_units for l in window]
    cv = _cv(amounts)
    if cv > MAX_CV:
        return None
    pattern = "fan-out-burst" if direction == "out" else "fan-in-burst"
    return StructuringFinding(
        pattern=pattern, direction=direction, address=address,
        count=len(window), points=POINTS_FAN, **_describe(window, symbol))


def _sub_threshold_finding(legs: list[_Leg], address: str,
                           direction: str, symbol: str
                           ) -> Optional[StructuringFinding]:
    window = _best_window(legs)
    for rnd in ROUND_NUMBERS:
        near = [l for l in window
                if SUBTHRESHOLD_BAND * rnd <= l.amount_units < rnd]
        if len(near) >= MIN_SUBTHRESHOLD_COUNT:
            return StructuringFinding(
                pattern="sub-threshold", direction=direction,
                address=address, count=len(near),
                round_number=float(rnd), points=POINTS_SUBTHRESHOLD,
                **_describe(near, symbol))
    return None


def analyze_structuring(txs: list[CanonicalTx], address: str
                       ) -> list[StructuringFinding]:
    """Detect structuring shapes in an address's native transfers.

    Pure function over already-ingested transactions. Returns at most
    three findings (fan-out, fan-in, sub-threshold — the latter checked
    on the denser of the two legs).
    """
    findings: list[StructuringFinding] = []
    legs_out = _legs(txs, address, "out")
    legs_in = _legs(txs, address, "in")
    symbol = ""
    for legs in (legs_out, legs_in):
        if legs:
            symbol = legs[0].tx.asset.symbol or ""
            break

    fan_out = _fan_finding(legs_out, address, "out", symbol)
    if fan_out:
        findings.append(fan_out)
    fan_in = _fan_finding(legs_in, address, "in", symbol)
    if fan_in:
        findings.append(fan_in)

    dense, direction = ((legs_out, "out") if len(legs_out) >= len(legs_in)
                        else (legs_in, "in"))
    sub = _sub_threshold_finding(dense, address, direction, symbol)
    if sub:
        findings.append(sub)
    return findings
