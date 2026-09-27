"""Curated on-chain registries: DEX routers, bridges, mixers.

Every entry was verified during M4 (2026-09-27): Tornado Cash pools and
Uniswap V3 routers by calling their on-chain view functions
(denomination(), factory()); Wormhole TokenBridge against the official
wormhole-foundation/wormhole repo registry; Stargate routers via Etherscan
name tags plus LayerZero/Stargate integration docs; PancakeSwap and
QuickSwap routers via their official docs. See each module's docstring for
the per-entry story. Registries are intentionally small and conservative —
a wrong registry entry mislabels hops, so every entry needs a source.
"""
