"""Bridge contract registry (M4).

Deposit detection is counterparty-based: a transfer whose counterparty is
a known bridge contract is a bridge-lock (user -> bridge) or bridge-release
(bridge -> user) hop. Cross-chain correlation (deposit on A -> withdrawal
on B) lives in engine/decoding/correlation.py.

Address verification (2026-09-27):
- Stargate routers: the Ethereum router carries Etherscan's "Stargate
  Finance: Router" name tag; all three match the addresses published in
  Stargate/LayerZero integration docs and third-party integration guides.
- Wormhole TokenBridge: taken from the canonical MAINNET registry in the
  official wormhole-foundation/wormhole repo
  (sdk/js/src/utils/consts.ts), corroborated by ethereum/VERIFY.md in the
  same repo (Ethereum proxy address) and the `worm evm info` CLI output
  (BSC address). An earlier draft of this registry carried corrupted
  variants of both Wormhole addresses; the official repo is what caught them.
"""
from dataclasses import dataclass

from ..adapters.base import Chain


@dataclass(frozen=True)
class BridgeInfo:
    name: str       # "stargate" | "wormhole"
    chain: Chain
    contract: str   # display address
    model: str = "lock-mint"  # stargate pools / wormhole lock-mint


_BRIDGES: list[BridgeInfo] = [
    BridgeInfo("stargate", Chain.ETHEREUM,
               "0x8731d54E9D02c286767d56ac03e8037C07e01e98"),
    BridgeInfo("stargate", Chain.BSC,
               "0x4a364f8c717cAAD9A442737Eb7b8A55cc6cf18D8"),
    BridgeInfo("stargate", Chain.POLYGON,
               "0x45A01E4e04F14f7A4a6702c74187c5F6222033cd"),
    BridgeInfo("wormhole", Chain.ETHEREUM,
               "0x3ee18B2214AFF97000D974cf647E7C347E8fa585"),
    BridgeInfo("wormhole", Chain.BSC,
               "0xB6F6D86a8f9879A9c87f643768d9efc38c1Da6E7"),
]

# (chain.value, address.lower()) -> BridgeInfo
BRIDGE_CONTRACTS: dict[tuple[str, str], BridgeInfo] = {
    (b.chain.value, b.contract.lower()): b for b in _BRIDGES
}


def bridge_for(chain: Chain, address: str) -> BridgeInfo | None:
    """Bridge lookup — None when the address is not a known bridge contract."""
    return BRIDGE_CONTRACTS.get((chain.value, address.lower()))
