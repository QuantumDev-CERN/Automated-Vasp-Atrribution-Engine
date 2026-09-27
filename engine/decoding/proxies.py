"""Deposit-proxy annotation (M16): opt-in post-processing that marks EVM
output addresses which look like exchange deposit proxies.

Like DEX receipt decoding (M4), this is enrichment, not part of the base
adapter path: the caller supplies a `get_code` callable (EvmAdapter.get_code
in production, a stub in tests) so the core stays network-free and unit
testable.

Deposit proxies matter for sweeps: when a sweep-consolidation hop is later
observed, traversal back-labels proxy inputs as "deposit-proxy" instead of
leaving them as anonymous sweep sources.
"""
from typing import Callable, Optional

from engine.adapters.base import CanonicalTx, Chain
from engine.knowledge.proxies import ProxyInfo, detect_deposit_proxy

_EVM_CHAINS = {Chain.ETHEREUM, Chain.BSC, Chain.POLYGON}


def annotate_deposit_proxies(
    chain: Chain,
    txs: list[CanonicalTx],
    get_code: Callable[[str], str],
    get_deployer: Optional[Callable[[str], Optional[str]]] = None,
) -> int:
    """Mark EVM output addresses that are deposit-proxy candidates.

    Sets `FlowParty.proxy_kind` on matching outputs. Each distinct address is
    probed once (cached). Returns the number of parties annotated.
    Non-EVM chains are a no-op returning 0.
    """
    if chain not in _EVM_CHAINS:
        return 0
    code_cache: dict[str, str] = {}
    annotated = 0
    for tx in txs:
        for party in tx.outputs:
            addr = party.address.lower()
            if party.proxy_kind is not None:
                continue  # already annotated upstream
            if addr not in code_cache:
                code_cache[addr] = get_code(party.address)
            deployer = get_deployer(party.address) if get_deployer else None
            info: Optional[ProxyInfo] = detect_deposit_proxy(
                code_cache[addr], deployer
            )
            if info is not None:
                party.proxy_kind = info.kind
                annotated += 1
    return annotated
