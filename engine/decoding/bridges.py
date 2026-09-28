"""Bridge calldata destination parsing (M21).

When funds lock into a bridge, the lock transaction's calldata usually
names the destination chain and recipient explicitly. Parsing it turns a
correlation-only cross-chain lead into an explicit destination — which
is exactly what the M6 ``bridge_explicit_destination`` confidence path
was built for, and it powers the M21 destination-chain continuation.

Only decoders with a stable, well-documented ABI are implemented:

* Wormhole TokenBridge ``transferTokens`` — canonical signature from the
  official wormhole-foundation/wormhole repo
  (evm/src/contracts/bridge/interfaces/ITokenBridge.sol)::

      transferTokens(address token, uint256 amount, uint16 recipientChain,
                     bytes32 recipient, uint256 arbiterFee, uint32 nonce)

  ``recipientChain`` is a Wormhole chain id (2 = Ethereum, 4 = BSC,
  5 = Polygon, 1 = Solana, ...); ``recipient`` is a Wormhole universal
  32-byte address (12 zero bytes + 20-byte EVM address on EVM chains,
  32-byte pubkey on Solana).

* Stargate's ``swap()`` carries the destination in a dynamic ``bytes``
  ``_to`` parameter behind a tuple — deliberately NOT hand-decoded here
  (too easy to get subtly wrong). Stargate locks keep the M4
  correlation-only treatment until a verified decoder lands.

The parser never raises: garbage in -> None out. No curated
coordinator lists, no network calls.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..adapters.base import CanonicalTx
from ..knowledge.bridges import bridge_for
from ..knowledge.proxies import keccak256

# keccak256("transferTokens(address,uint256,uint16,bytes32,uint256,uint32)")[:4]
_TRANSFER_TOKENS_SELECTOR = keccak256(
    b"transferTokens(address,uint256,uint16,bytes32,uint256,uint32)"
)[:4]

# Wormhole chain id -> our Chain value. Ids we know but cannot trace
# (avalanche, fantom, arbitrum, optimism, base) are parsed with
# dest_chain=None rather than guessed.
_WORMHOLE_CHAIN_IDS: dict[int, Optional[str]] = {
    1: "solana",
    2: "ethereum",
    4: "bsc",
    5: "polygon",
    6: None,    # avalanche — no adapter
    10: None,   # fantom — no adapter
    23: None,   # arbitrum — no adapter
    30: None,   # optimism — no adapter
    32: None,   # base — no adapter
}

_WORD = 32
_SELECTOR_LEN = 4
_EXPECTED_LEN = _SELECTOR_LEN + 6 * _WORD  # selector + 6 ABI words


@dataclass(frozen=True)
class BridgeDestination:
    """Explicitly parsed bridge destination (M21)."""
    bridge: str            # "wormhole"
    tx_hash: str
    wormhole_chain_id: int
    dest_chain: Optional[str]   # our chain value; None when unsupported
    dest_address: str           # 0x-address on EVM chains, hex otherwise
    note: str


def _word(data: bytes, i: int) -> bytes:
    return data[_SELECTOR_LEN + i * _WORD:_SELECTOR_LEN + (i + 1) * _WORD]


def _evm_address(word32: bytes) -> Optional[str]:
    if word32[:12] == b"\x00" * 12:
        return "0x" + word32[12:].hex()
    return None


def parse_bridge_destination(tx: CanonicalTx) -> Optional[BridgeDestination]:
    """Parse an explicit destination from a bridge-lock tx's calldata.

    Returns None when the tx does not touch a known Wormhole TokenBridge,
    the calldata is not a transferTokens call, or anything is malformed.
    """
    counterparties = {p.address for p in tx.inputs + tx.outputs}
    touches_wormhole = False
    for addr in counterparties:
        b = bridge_for(tx.chain, addr)
        if b is not None and b.name == "wormhole":
            touches_wormhole = True
            break
    if not touches_wormhole:
        return None

    raw = (tx.raw.get("input") or "")
    if raw.startswith(("0x", "0X")):
        raw = raw[2:]
    try:
        data = bytes.fromhex(raw)
    except ValueError:
        return None
    if len(data) < _EXPECTED_LEN:
        return None
    if data[:_SELECTOR_LEN] != _TRANSFER_TOKENS_SELECTOR:
        return None

    try:
        recipient_chain = int.from_bytes(_word(data, 2)[30:32], "big")
        recipient = _word(data, 3)
    except Exception:
        return None

    dest_chain = _WORMHOLE_CHAIN_IDS.get(recipient_chain)
    evm = _evm_address(recipient)
    if evm is not None:
        dest_address = evm
    else:
        dest_address = recipient.hex()  # e.g. Solana 32-byte pubkey

    if recipient_chain not in _WORMHOLE_CHAIN_IDS:
        note = (f"unknown wormhole chain id {recipient_chain}: parsed "
                f"recipient but cannot map the chain")
    elif dest_chain is None:
        note = (f"wormhole chain id {recipient_chain}: no adapter for the "
                f"destination chain — correlation only")
    else:
        note = f"explicit destination: {dest_chain}:{dest_address[:12]}…"
    return BridgeDestination(
        bridge="wormhole",
        tx_hash=tx.tx_hash,
        wormhole_chain_id=recipient_chain,
        dest_chain=dest_chain,
        dest_address=dest_address,
        note=note,
    )
