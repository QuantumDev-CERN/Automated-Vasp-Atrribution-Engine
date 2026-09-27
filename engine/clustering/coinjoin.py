"""CoinJoin detection guards (M17, master plan section 6).

Common-input-ownership clustering assumes every input of a UTXO transaction
is controlled by one entity. CoinJoin transactions (Wasabi, Whirlpool,
JoinMarket) deliberately violate that assumption: they combine inputs from
many unrelated parties into one transaction. Unioning across a CoinJoin
would falsely attribute innocent parties' funds to the suspect — the worst
possible failure for an evidentiary tool.

So the guard runs BEFORE any merge: a flagged transaction's inputs are never
unioned, and the flag is kept as auditable evidence.

Detection is structural on purpose — no curated coordinator addresses, so it
keeps working as coordinators rotate:

  1. equal-output anonymity set: the largest group of outputs carrying an
     identical value. ``min_equal_outputs`` (default 5) identical outputs
     with ``min_inputs`` (default 3) inputs is the classic CoinJoin shape
     (Wasabi rounds emit dozens of identical-denomination outputs).
  2. fan-in/fan-out with low value diversity: ``min_fan`` (default 10)
     inputs AND ``min_fan`` outputs but at most ``max_distinct_values``
     (default 3) distinct output values — a coordinated multi-party
     construction even when denominations are mixed.

Thresholds are conservative by design: a missed cluster costs recall, a
false merge costs a false accusation.

Known limitation (documented, not solved): PayJoin (BIP-78) looks like an
ordinary 2-in/2-out payment and will not trip these heuristics, so CIOH can
over-merge there. That residual risk is disclosed rather than hidden.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from ..adapters.base import CanonicalTx

_COINBASE = "coinbase"  # pseudo-input label used by the Bitcoin adapter


@dataclass(frozen=True)
class CoinJoinEvidence:
    """Why a transaction was (or was not) treated as a CoinJoin."""

    tx_hash: str
    is_coinjoin: bool
    reasons: tuple[str, ...] = ()
    input_count: int = 0
    output_count: int = 0
    max_equal_outputs: int = 0
    distinct_output_values: int = 0


def _spend_inputs(tx: CanonicalTx) -> list[str]:
    """Input addresses, excluding the coinbase pseudo-input."""
    return [p.address for p in tx.inputs if p.address != _COINBASE]


def _output_values(tx: CanonicalTx) -> list[int]:
    values: list[int] = []
    for p in tx.outputs:
        try:
            values.append(int(p.value))
        except (TypeError, ValueError):
            continue
    return values


def detect_coinjoin(
    tx: CanonicalTx,
    *,
    min_equal_outputs: int = 5,
    min_inputs: int = 3,
    min_fan: int = 10,
    max_distinct_values: int = 3,
) -> CoinJoinEvidence:
    """Decide whether ``tx`` looks like a CoinJoin.

    Never raises on malformed data: unparseable values are ignored, and a
    transaction with nothing to judge is reported as not-a-CoinJoin.
    """
    inputs = _spend_inputs(tx)
    values = _output_values(tx)
    counts = Counter(values)
    max_equal = max(counts.values()) if counts else 0
    distinct = len(counts)

    reasons: list[str] = []
    if len(inputs) >= min_inputs and max_equal >= min_equal_outputs:
        reasons.append(
            f"equal-output anonymity set: {max_equal} outputs of identical "
            f"value across {len(inputs)} inputs"
        )
    if (
        len(inputs) >= min_fan
        and len(values) >= min_fan
        and distinct <= max_distinct_values
    ):
        reasons.append(
            f"coordinated fan-in/fan-out: {len(inputs)} inputs, "
            f"{len(values)} outputs, only {distinct} distinct values"
        )

    return CoinJoinEvidence(
        tx_hash=tx.tx_hash,
        is_coinjoin=bool(reasons),
        reasons=tuple(reasons),
        input_count=len(inputs),
        output_count=len(values),
        max_equal_outputs=max_equal,
        distinct_output_values=distinct,
    )
