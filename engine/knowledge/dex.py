"""DEX router registry + Swap event signatures (M4).

Address verification (2026-09-27):
- Uniswap V2 Router02 (ethereum): canonical address, corroborated by
  multiple independent integration references.
- Uniswap V3 SwapRouter + SwapRouter02 (ethereum): verified ON-CHAIN by
  calling each contract's ``factory()`` view function through the Etherscan
  V2 ``proxy/eth_call`` endpoint; both return the Uniswap V3 factory
  (0x1F98431c8aD98523631AE4a59f267346ea31F984).
- PancakeSwap V2 router (bsc): verified ON-CHAIN 2026-09-27 — called
  ``factory()`` through the public BSC JSON-RPC endpoint; it returns
  0xca143ce32fe78f1f7019d7d551a6402fc5350c73, the PancakeSwap V2 factory
  (PancakeSwap's own docs, router-v2 page, corroborate the router address).
- PancakeSwap V3 Smart Router (bsc): verified ON-CHAIN 2026-09-27 —
  ``factory()`` returns 0x0BFbCF9fa4f9C56B0F40a671Ad40E0805A091865, which
  independent integration references (SubQuery docs, bnbchain-skills,
  whal-e-bnb) all name as the PancakeSwap V3 factory. PancakeSwap V3 is a
  Uniswap V3 fork, so kind="v3" selects the V3 Swap event layout.
- QuickSwap V2 router (polygon): QuickSwap's official docs (V2 router02
  page) plus the PolygonScan "QuickSwap V2: Router" name tag.

Swap topic hashes: keccak of the event signatures, verified against live
mainnet receipts 2026-09-27 (an earlier draft misremembered both hashes;
the live smoke caught it).
"""
from dataclasses import dataclass

from ..adapters.base import Chain

# keccak("Swap(address,uint256,uint256,uint256,uint256,address)")
# verified against mainnet receipts 2026-09-27 (was misremembered before;
# the live smoke caught it)
SWAP_TOPIC_V2 = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"
# keccak("Swap(address,address,int256,int256,uint160,uint128,int24)")
# verified against mainnet receipts 2026-09-27 (was misremembered before;
# the live smoke caught it)
SWAP_TOPIC_V3 = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"


@dataclass(frozen=True)
class DexInfo:
    name: str       # "uniswap-v2"
    chain: Chain
    router: str     # display (checksummed-ish) address
    kind: str       # "v2" | "v3"  — AMM family, selects the Swap event layout


_DEXES: list[DexInfo] = [
    DexInfo("uniswap-v2", Chain.ETHEREUM,
            "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D", "v2"),
    DexInfo("uniswap-v3", Chain.ETHEREUM,
            "0xE592427A0AEce92De3Edee1F18E0157C05861564", "v3"),
    DexInfo("uniswap-v3-router02", Chain.ETHEREUM,
            "0x68b3465833fb72A70ecDF485E0e4C7Bd8665Fc45", "v3"),
    DexInfo("pancakeswap-v2", Chain.BSC,
            "0x10ED43C718714eb63d5aA57B78B54704E256024E", "v2"),
    DexInfo("pancakeswap-smart-router", Chain.BSC,
            "0x13f4EA83D0bd40E75C8222255bc855a974568Dd4", "v3"),
    DexInfo("quickswap-v2", Chain.POLYGON,
            "0xa5e0829caced8ffdd4de3c43696c57f7d7a678ff", "v2"),
]

# (chain.value, address.lower()) -> DexInfo
DEX_ROUTERS: dict[tuple[str, str], DexInfo] = {
    (d.chain.value, d.router.lower()): d for d in _DEXES
}


def dex_for(chain: Chain, address: str) -> DexInfo | None:
    """Router lookup — None when the address is not a known DEX router."""
    return DEX_ROUTERS.get((chain.value, address.lower()))
