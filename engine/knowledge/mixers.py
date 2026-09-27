"""Mixer registry (M4).

Tornado Cash ETH pools on Ethereum mainnet — the canonical fixed-
denomination pools. Sanctioned by OFAC (Aug 2022, delisted Mar 2025); the
registry exists so the tracer can *detect* deposits into the anonymity set,
which is exactly what an investigator needs to see.

Scope is deliberately narrow: deterministic pool addresses only. Heuristic
"looks like a mixer" detection is out of scope — false positives there are
worse than misses.

Address verification (2026-09-27): each address below was verified ON-CHAIN
by calling the pool contract's ``denomination()`` view function through the
Etherscan V2 ``proxy/eth_call`` endpoint and confirming the returned value
matches the pool's denomination (0.1 / 1 / 10 / 100 ETH). An earlier draft
of this registry carried corrupted variants of these addresses; the
on-chain calls are what caught them.
"""
from dataclasses import dataclass

from ..adapters.base import Chain


@dataclass(frozen=True)
class MixerInfo:
    name: str          # "tornado-cash"
    chain: Chain
    pool: str          # display address
    denomination: str   # fixed pool size, smallest units ("100000000000000000" = 0.1 ETH)


_MIXERS: list[MixerInfo] = [
    # denomination() -> 0.1 ETH, verified on-chain 2026-09-27
    MixerInfo("tornado-cash", Chain.ETHEREUM,
              "0x12D66f87A04A9E220743712cE6d9bB1B5616B8Fc", "100000000000000000"),
    # denomination() -> 1 ETH, verified on-chain 2026-09-27
    MixerInfo("tornado-cash", Chain.ETHEREUM,
              "0x47CE0C6eD5B0Ce3d3A51fdb1C52DC66a7c3c2936", "1000000000000000000"),
    # denomination() -> 10 ETH, verified on-chain 2026-09-27
    MixerInfo("tornado-cash", Chain.ETHEREUM,
              "0x910Cbd523D972eb0a6f4cAe4618aD62622b39DbF", "10000000000000000000"),
    # denomination() -> 100 ETH, verified on-chain 2026-09-27
    MixerInfo("tornado-cash", Chain.ETHEREUM,
              "0xA160cdAB225685dA1d56aa342Ad8841c3b53f291", "100000000000000000000"),
]

# (chain.value, address.lower()) -> MixerInfo
MIXER_POOLS: dict[tuple[str, str], MixerInfo] = {
    (m.chain.value, m.pool.lower()): m for m in _MIXERS
}


def mixer_for(chain: Chain, address: str) -> MixerInfo | None:
    """Mixer-pool lookup — None when the address is not a known pool."""
    return MIXER_POOLS.get((chain.value, address.lower()))
