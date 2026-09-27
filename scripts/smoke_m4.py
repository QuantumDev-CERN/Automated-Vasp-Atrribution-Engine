"""M4 live smoke: DEX swap decoding on real chain data.

EVM: takes recent swapExactTokensForTokens calls to the Uniswap V2 router,
fetches the tx receipt, and proves BOTH decode layers on the same swap:
  1. event-log  — decode the Swap event (exact pool-level amounts)
  2. pairing    — rebuild ERC-20 Transfer legs from the receipt logs and
                  pair them into the same swap via transfer-pairing
Solana (best-effort): resolve a live SPL token account to its owner wallet.

Run: uv run python scripts/smoke_m4.py
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.adapters.base import AdapterError, Asset, AssetKind, CanonicalTx, Chain, FlowParty
from engine.decoding.swaps import decode_swap_receipts, detect_dex_swaps


class SectionSkip(Exception):
    """The external dependency returned nothing usable (free public
    endpoint flaky), not a code regression. Loud skip, not a pass."""

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"  # keccak("Transfer(address,address,uint256)") — verified on mainnet
SWAP_EXACT_TOKENS_SELECTOR = "0x38ed1739"  # swapExactTokensForTokens(address,uint256,...)
V2_ROUTER = "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D"


def load_env() -> None:
    for line in Path(".env").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def _transient(message: str) -> bool:
    m = message.lower()
    return any(
        h in m
        for h in ("429", "502", "503", "504", "timeout", "timed out",
                  "connecterror", "connection reset", "temporarily unavailable")
    )


def _u256(word: str) -> int:
    return int(word, 16)


def _addr_word(word: str) -> str:
    return "0x" + word[-40:]


def _transfer_legs(logs: list[dict], tx_hash: str) -> list[CanonicalTx]:
    """Rebuild ERC-20 Transfer legs from receipt logs (excl. mints/burns)."""
    legs = []
    for log in logs:
        topics = log.get("topics") or []
        if not topics or topics[0].lower() != TRANSFER_TOPIC:
            continue
        if len(topics) < 3:
            continue
        src, dst = _addr_word(topics[1]), _addr_word(topics[2])
        if "0x0000000000000000000000000000000000000000" in (src, dst):
            continue  # mint/burn: not a swap leg
        data = (log.get("data") or "0x")[2:]
        amount = str(_u256(data[:64])) if len(data) >= 64 else "0"
        legs.append(CanonicalTx(
            tx_hash=tx_hash, chain=Chain.ETHEREUM,
            inputs=[FlowParty(address=src, value=amount)],
            outputs=[FlowParty(address=dst, value=amount)],
            asset=Asset(kind=AssetKind.TOKEN, chain=Chain.ETHEREUM,
                        contract=log.get("address"), decimals=None),
        ))
    return legs


async def section_evm() -> None:
    from engine.adapters.evm import EvmAdapter

    load_env()
    api_key = os.environ.get("ETHERSCAN_API_KEY")
    if not api_key:
        raise SectionSkip(
            "ETHERSCAN_API_KEY not set — add it to .env to run this section")
    evm = EvmAdapter(Chain.ETHEREUM, api_key=api_key)

    # recent token->token swaps through the V2 router
    raw = await evm._api("account", "txlist", V2_ROUTER, 25)
    swaps = [t for t in raw
             if t.get("isError") == "0"
             and (t.get("input") or "").startswith(SWAP_EXACT_TOKENS_SELECTOR)]
    assert swaps, "no swapExactTokensForTokens txs in router txlist page"
    print(f"[evm] candidate swap txs: {len(swaps)}")

    decoded = paired = None
    used_hash = ""
    for cand in swaps[:5]:
        h = cand["hash"]
        receipt = await evm.get_transaction_receipt(h)
        logs = receipt.get("logs") or []
        trader = cand.get("from", "")
        events = decode_swap_receipts(logs, h, Chain.ETHEREUM, trader)
        legs = _transfer_legs(logs, h)
        print(f"[evm]   {h[:12]}… logs={len(logs)} swap_events={len(events)} "
              f"transfer_legs={len(legs)}")
        found = detect_dex_swaps(legs, prefer=trader)
        if not events or not found:
            continue
        # trader-facing ends must agree: first hop's in == paired in,
        # last hop's out == paired out (multi-hop routes)
        if (found[0].in_value == events[0].in_value
                and found[0].out_value == events[-1].out_value):
            decoded, paired = events[-1], found[0]
            used_hash = h
            break
    assert decoded, "no Swap event decoded from 5 candidate txs"
    assert paired, "transfer-pairing found no swap in 5 candidate receipts"

    print(f"[evm] tx {used_hash[:18]}…")
    print(f"[evm] event-log : in={decoded.in_value} "
          f"out={decoded.out_value} dex={decoded.dex}")
    print(f"[evm] pairing  : trader={paired.trader[:12]}… "
          f"in={paired.in_value} out={paired.out_value} "
          f"dex={paired.dex} conf={paired.confidence}")
    await evm.close()


async def section_solana_spl_owner() -> None:
    from engine.adapters.solana import SolanaAdapter, WSOL_MINT

    sol = SolanaAdapter()
    txs = []
    for attempt in range(3):
        txs = await sol.get_token_transfers(WSOL_MINT, limit=3)
        if txs:
            break
        print(f"[sol] SPL probe empty (attempt {attempt + 1}/3), retrying…")
        await asyncio.sleep(3)
    if not txs:
        raise SectionSkip(
            "solana-spl-owner: public RPC returned no parseable SPL "
            "transfers in 3 attempts — free endpoint flaky, not a code "
            "regression")
    owner = None
    used = ""
    for t in txs:  # a tx may bundle several tokens; take the first resolvable
        for party in t.inputs + t.outputs:
            owner = await sol.get_token_account_owner(party.address)
            if owner:
                used = party.address
                break
        if owner:
            break
    print(f"[sol] token account {used[:16]}… -> owner "
          f"{(owner[:16] + '…') if owner else 'UNRESOLVED'}")
    assert owner, "no SPL token account owner resolved from 3 transfers"
    await sol.close()


async def _run_section(name: str, fn) -> str:
    try:
        await fn()
    except SectionSkip as e:
        print(f"[{name}] SKIP — {e}")
        return "skip"
    except AdapterError as e:
        if _transient(str(e)):
            print(f"[{name}] SKIP — indexer throttled/unreachable: {e}")
            return "skip"
        raise
    print(f"[{name}] section passed")
    return "pass"


async def main() -> None:
    sections = [
        ("evm-dex-decode", section_evm),
        ("solana-spl-owner", section_solana_spl_owner),
    ]
    results = {name: await _run_section(name, fn) for name, fn in sections}
    passed = [n for n, s in results.items() if s == "pass"]
    skipped = [n for n, s in results.items() if s == "skip"]
    print(f"\nSMOKE {'OK' if not skipped else 'OK with skips'} — "
          f"{len(passed)} passed, {len(skipped)} skipped"
          + (f" ({', '.join(skipped)})" if skipped else ""))
    if len(skipped) == len(sections):
        sys.exit("SMOKE FAILED — every section skipped; check network connectivity")


if __name__ == "__main__":
    asyncio.run(main())
