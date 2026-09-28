"""M19: OTC/hawala terminus detection + persistent registry.

Master plan, section 2 (typology table): when funds exit the on-chain
world entirely (cash payout off-chain), naive tracing reports a "failed"
dead-end. The counter-technique is to recognize the collection pattern —
many small disparate deposits, no further movement — and label the
terminal explicitly as "OTC/hawala terminus — no further on-chain trail
expected", not a failed trace.

Two halves:

1. Heuristic detection (`assess_otc_pattern` / `detect_otc_termini`).
   A dead-end terminal whose recent on-chain history shows at least
   MIN_DISTINCT_SENDERS distinct depositors and zero outbound transfers
   is relabeled from "dead-end" to "otc-hawala-terminus". Deliberately
   conservative: only dead-end terminals are candidates (a sweep,
   bridge, mixer or swap-service terminal already has a better
   explanation), and the thresholds are structural, not value-based —
   a hawala collector can receive any size, so "small" is not gated on
   amounts. The discriminating features are disparate depositors +
   no onward movement.

2. Persistent growing registry. The tag IS the registry: the pipeline
   already tags every terminal address with its reason in the graph
   store (M8), so confirmed terminus wallets accumulate under the
   OTC_TERMINUS_TAG tag automatically, and future traces short-circuit
   on repeat addresses via `address_tags` before spending any indexer
   quota. Same reusable-intelligence pattern as exchange clustering.

Honesty notes:
- This is a behavioral heuristic, not identity attribution. It says
  "this wallet behaves like an OTC/hawala collection point", never
  "this wallet belongs to person X".
- Tags are advisory and append-only; a wallet that later moves funds
  on-chain keeps its historical tag. The M13 feedback loop is the
  place where an officer confirms or refutes a terminus call.
- No curated seed list: unlike swap services (M18), there is no
  reliable public labeling of OTC-desk wallets, and fabricating one
  would be worse than starting the registry empty and growing it
  from observed behavior, which is what the master plan specifies.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # pragma: no cover
    from ..adapters.base import CanonicalTx, ChainAdapter

# Tag under which confirmed terminus wallets persist in the graph store.
# The registry is the set of addresses carrying this tag.
OTC_TERMINUS_TAG = "otc-hawala-terminus"

# Minimum distinct depositors for the collection pattern. Ten unrelated
# counterparties paying into one wallet that never forwards on-chain is
# far outside normal personal-wallet behavior; below this the pattern
# is too easily mimicked by ordinary fan-in (e.g. an unswept deposit
# address) to label.
MIN_DISTINCT_SENDERS = 10

# Newest-first transactions examined per candidate address. Bounds
# indexer cost; recent behavior is what determines "terminus".
HISTORY_WINDOW = 50

# Chains whose addresses are hex and case-insensitive.
_EVM_CHAINS = frozenset({"ethereum", "bsc", "polygon"})


def _norm(address: str, chain: str) -> str:
    return address.lower() if chain in _EVM_CHAINS else address


@dataclass(frozen=True)
class OtcAssessment:
    """Outcome of the OTC/hawala pattern check for one address."""

    address: str
    is_terminus: bool
    distinct_senders: int
    inbound_count: int
    outbound_count: int
    detail: str  # one-line evidence summary for logs/reports


def assess_otc_pattern(
    address: str,
    txs: list["CanonicalTx"],
    chain: str,
) -> OtcAssessment:
    """Pure structural check: many disparate depositors, no onward movement.

    `txs` is the address's recent history, newest first (native + token
    transfers). A transaction counts as inbound when the address appears
    in its outputs, outbound when it appears in its inputs. Self-transfers
    are excluded from the depositor set but still count as movement.
    """
    norm = _norm(address, chain)
    senders: set[str] = set()
    inbound = 0
    outbound = 0
    for tx in txs:
        in_addrs = {_norm(p.address, chain) for p in tx.inputs}
        out_addrs = {_norm(p.address, chain) for p in tx.outputs}
        if norm in out_addrs:
            inbound += 1
            senders.update(a for a in in_addrs if a != norm)
        if norm in in_addrs:
            outbound += 1
    distinct = len(senders)
    is_terminus = (
        distinct >= MIN_DISTINCT_SENDERS
        and inbound >= MIN_DISTINCT_SENDERS
        and outbound == 0
    )
    detail = (
        f"{distinct} distinct depositors, {inbound} inbound / "
        f"{outbound} outbound txs in last {len(txs)} — "
        f"{'OTC/hawala terminus pattern' if is_terminus else 'no terminus pattern'}"
    )
    return OtcAssessment(
        address=address,
        is_terminus=is_terminus,
        distinct_senders=distinct,
        inbound_count=inbound,
        outbound_count=outbound,
        detail=detail,
    )


async def detect_otc_termini(
    adapter: "ChainAdapter",
    chain: str,
    addresses: list[str],
    *,
    limit: int = HISTORY_WINDOW,
) -> dict[str, OtcAssessment]:
    """Fetch recent history for each candidate and return the ones that
    match the OTC/hawala collection pattern, keyed by address.

    A heuristic pass: an indexer failure on one address skips that
    address (never the whole trace).
    """
    from ..adapters.base import AdapterError

    out: dict[str, OtcAssessment] = {}
    for address in dict.fromkeys(addresses):
        try:
            txs = await adapter.get_transactions(address, limit=limit)
            txs = txs + await adapter.get_token_transfers(address, limit=limit)
        except AdapterError:
            continue
        assessment = assess_otc_pattern(address, txs, chain)
        if assessment.is_terminus:
            out[address] = assessment
    return out


async def registry_size(store, tag: str = OTC_TERMINUS_TAG) -> int:
    """How many wallets the persistent OTC/hawala registry holds."""
    return len(await store.addresses_with_tag(tag))


async def registry_contains(
    store, address: str, chain: str, tag: str = OTC_TERMINUS_TAG
) -> Optional[dict]:
    """The registry tag entry for an address, or None."""
    for entry in await store.address_tags(address, chain):
        if entry.get("tag") == tag:
            return entry
    return None
