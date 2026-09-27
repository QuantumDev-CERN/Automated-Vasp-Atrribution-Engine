"""DEX swap decoding (M4, master plan section 6).

Two layers, cheapest first:

1. transfer-pairing — pure adapter data, no extra RPC. A swap is visible
   as one tx where the trader sends token A out and receives token B in
   (native leg included: ETH out / token in). Pairing is conservative: it
   only fires on an unambiguous 1-out/1-in per trader per tx, and gains
   confidence when a counterparty is a known DEX router.

2. event-log — exact decode of Uniswap V2/V3 Swap events from the tx
   receipt (needs EvmAdapter.get_transaction_receipt). Gives exact
   in/out amounts at the pool level; token contracts are attached when
   the caller supplies the pool's token0/token1.

Both produce DexSwap and annotate the involved CanonicalTx objects via
tx.dex_swap, which the hop classifier turns into dex-swap edges.
"""
from __future__ import annotations

from typing import Optional

from ..adapters.base import AssetKind, CanonicalTx, Chain, DexSwap
from ..knowledge.dex import SWAP_TOPIC_V2, SWAP_TOPIC_V3, dex_for

_PAIR_BASE_CONF = 0.75
_PAIR_ROUTER_BONUS = 0.15


def _asset_id(tx: CanonicalTx) -> tuple[Optional[str], Optional[str]]:
    """(contract-or-None, symbol) for one transfer leg."""
    if tx.asset.kind == AssetKind.NATIVE:
        return None, tx.asset.symbol
    return tx.asset.contract, tx.asset.symbol


def detect_dex_swaps(
    txs: list[CanonicalTx], prefer: Optional[str] = None
) -> list[DexSwap]:
    """Pair opposite-direction transfers of the same tx into DexSwaps.

    Annotates each involved tx with .dex_swap and returns the swaps.
    One swap per leg-pair: the two candidate perspectives (trader vs
    pool/router) describe the same economic hop, so the venue is never
    the trader — a known DEX router perspective is dropped, and any
    remaining tie is broken deterministically by address sort (documented
    arbitrary choice; classification is unaffected since both legs touch
    both addresses either way). Pass prefer=<case subject> to resolve the
    tie toward the address under investigation.

    Ambiguous txs (anything but exactly 1 out-leg + 1 in-leg per
    perspective) are skipped — a wrong pairing is worse than a miss.
    """
    by_hash: dict[str, list[CanonicalTx]] = {}
    for tx in txs:
        by_hash.setdefault(tx.tx_hash, []).append(tx)

    swaps: list[DexSwap] = []
    for tx_hash, legs in by_hash.items():
        if len(legs) < 2:
            continue
        chain = legs[0].chain
        out_by: dict[str, list[CanonicalTx]] = {}
        in_by: dict[str, list[CanonicalTx]] = {}
        for leg in legs:
            if not leg.inputs or not leg.outputs:
                continue
            src, dst = leg.inputs[0].address, leg.outputs[0].address
            if src == dst:
                continue
            out_by.setdefault(src, []).append(leg)
            in_by.setdefault(dst, []).append(leg)

        # (id(out_leg), id(in_leg)) as an ORDER-INSENSITIVE pair ->
        # candidate trader perspectives. Both sides of a swap (trader vs
        # pool/router) describe the same economic hop, so they must
        # collapse to one DexSwap.
        cands: dict[tuple[int, int], list[str]] = {}
        leg_of: dict[int, CanonicalTx] = {}
        for trader in set(out_by) & set(in_by):
            if dex_for(chain, trader) is not None:
                continue  # the venue is not the trader
            outs = [l for l in out_by[trader] if l.inputs[0].address == trader]
            ins = [l for l in in_by[trader] if l.outputs[0].address == trader]
            if len(outs) != 1 or len(ins) != 1:
                continue  # ambiguous — don't guess
            o_leg, i_leg = outs[0], ins[0]
            if _asset_id(o_leg) == _asset_id(i_leg):
                continue  # same asset both ways: not a swap
            key = tuple(sorted((id(o_leg), id(i_leg))))
            leg_of[id(o_leg)] = o_leg
            leg_of[id(i_leg)] = i_leg
            cands.setdefault(key, []).append(trader)

        for (a_id, b_id), traders in cands.items():
            first, second = leg_of[a_id], leg_of[b_id]
            # the out-leg is the one whose sender is the chosen trader
            non_router = [t for t in traders if dex_for(chain, t) is None]
            pool = non_router or traders
            trader = prefer if prefer in pool else sorted(pool)[0]
            o_leg = first if first.inputs[0].address == trader else second
            i_leg = second if o_leg is first else first
            out_c, out_s = _asset_id(o_leg)
            in_c, in_s = _asset_id(i_leg)

            router: Optional[str] = None
            dex_name: Optional[str] = "unknown"
            conf = _PAIR_BASE_CONF
            for leg in (o_leg, i_leg):
                for party in (leg.inputs[0].address, leg.outputs[0].address):
                    hit = dex_for(chain, party)
                    if hit and party.lower() != trader.lower():
                        router, dex_name = party, hit.name
                        conf = min(0.95, conf + _PAIR_ROUTER_BONUS)
                        break
                if router:
                    break

            swap = DexSwap(
                tx_hash=tx_hash,
                chain=chain,
                trader=trader,
                router=router,
                dex=dex_name,
                in_contract=out_c,   # trader's out-leg = swap input
                in_symbol=out_s,
                in_value=o_leg.inputs[0].value,
                out_contract=in_c,   # trader's in-leg = swap output
                out_symbol=in_s,
                out_value=i_leg.outputs[0].value,
                method="transfer-pairing",
                confidence=round(conf, 2),
            )
            o_leg.dex_swap = swap
            i_leg.dex_swap = swap
            swaps.append(swap)
    return swaps


# ----------------------------------------------------------------------
# event-log decoding


def _u256(word: str) -> int:
    return int(word, 16)


def _i256(word: str) -> int:
    v = int(word, 16)
    return v - 2**256 if v >= 2**255 else v


def _addr_word(word: str) -> str:
    return "0x" + word[-40:]


def _decode_swap_log(
    log: dict,
    tx_hash: str,
    chain: Chain,
    trader: str,
    token0: Optional[str] = None,
    token1: Optional[str] = None,
) -> Optional[DexSwap]:
    """Decode one receipt log if it is a Uniswap V2/V3 Swap event."""
    topics = log.get("topics") or []
    if not topics:
        return None
    topic0 = topics[0].lower()
    data = (log.get("data") or "0x")[2:]
    words = [data[i:i + 64] for i in range(0, len(data), 64)]
    pool = log.get("address", "")

    if topic0 == SWAP_TOPIC_V2.lower() and len(words) >= 4 and len(topics) >= 3:
        # Swap(sender, amount0In, amount1In, amount0Out, amount1Out, to)
        a0_in, a1_in, a0_out, a1_out = (_u256(w) for w in words[:4])
        sender = _addr_word(topics[1])
        if (a0_in and a0_out) or (a1_in and a1_out):
            return None  # malformed: same token in and out
        if a0_in and a1_out:
            in_t, out_t, in_v, out_v = token0, token1, a0_in, a1_out
        elif a1_in and a0_out:
            in_t, out_t, in_v, out_v = token1, token0, a1_in, a0_out
        else:
            return None  # no value moved — not a swap
        return DexSwap(
            tx_hash=tx_hash, chain=chain, trader=trader or sender,
            router=None, dex="uniswap-v2",
            in_contract=in_t, out_contract=out_t,
            in_value=str(in_v), out_value=str(out_v),
            method="event-log", confidence=0.95,
            # pool + event parties ride along in raw via the caller
        )

    if topic0 == SWAP_TOPIC_V3.lower() and len(words) >= 5 and len(topics) >= 3:
        # Swap(sender, recipient, amount0, amount1, sqrtPriceX96, liquidity, tick)
        a0, a1 = _i256(words[0]), _i256(words[1])
        sender = _addr_word(topics[1])
        if a0 > 0 and a1 < 0:
            in_t, out_t, in_v, out_v = token0, token1, a0, -a1
        elif a1 > 0 and a0 < 0:
            in_t, out_t, in_v, out_v = token1, token0, a1, -a0
        else:
            return None
        return DexSwap(
            tx_hash=tx_hash, chain=chain, trader=trader or sender,
            router=None, dex="uniswap-v3",
            in_contract=in_t, out_contract=out_t,
            in_value=str(in_v), out_value=str(out_v),
            method="event-log", confidence=0.95,
        )
    return None


def decode_swap_receipt(
    logs: list[dict],
    tx_hash: str,
    chain: Chain,
    trader: str,
    token0: Optional[str] = None,
    token1: Optional[str] = None,
) -> Optional[DexSwap]:
    """Decode Uniswap V2/V3 Swap events from a tx receipt's logs.

    token0/token1 (the pool's tokens) are not in the event — pass them when
    known so the swap carries contracts, not just amounts. Returns the
    first Swap event found; multi-pool routes decode pool-by-pool, so the
    caller can invoke per pool with the right tokens.
    """
    for log in logs:
        swap = _decode_swap_log(log, tx_hash, chain, trader, token0, token1)
        if swap:
            return swap
    return None


def decode_swap_receipts(
    logs: list[dict],
    tx_hash: str,
    chain: Chain,
    trader: str,
) -> list[DexSwap]:
    """Decode every V2/V3 Swap event in a receipt, in log order.

    Multi-hop routes emit one Swap event per pool; the trader-facing ends
    are swaps[0].in_value (what the trader gave) and swaps[-1].out_value
    (what the trader got).
    """
    swaps = []
    for log in logs:
        swap = _decode_swap_log(log, tx_hash, chain, trader)
        if swap:
            swaps.append(swap)
    return swaps
