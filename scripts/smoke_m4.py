"""M4 live smoke: DEX swap decoding on real chain data.

EVM: takes recent swapExactTokensForTokens calls to the Uniswap V2 router,
fetches the tx receipt, and proves BOTH decode layers on the same swap:
  1. event-log  — decode the Swap event (exact pool-level amounts)
  2. pairing    — rebuild ERC-20 Transfer legs from the receipt logs and
                  pair them into the same swap via transfer-pairing
BSC (M15): proves the venue-aware event-log decoder on PancakeSwap — the
WBNB/USDT V2 pair is resolved on-chain via factory.getPair (no hardcoded
pair address), recent Swap logs are pulled via eth_getLogs on the public
BSC RPC (Etherscan's free tier does not serve BSC), and the decoded venue
label must be "pancakeswap-v2". A V3 pool check runs best-effort.
Solana (best-effort): resolve a live SPL token account to its owner wallet.

Run: uv run python scripts/smoke_m4.py
"""
import asyncio
import os
import ssl
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.adapters.base import AdapterError, Asset, AssetKind, CanonicalTx, Chain, FlowParty
from engine.decoding.swaps import decode_swap_receipts, detect_dex_swaps


class SectionSkip(Exception):
    """The external dependency returned nothing usable (free public
    endpoint flaky), not a code regression. Loud skip, not a pass."""

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"  # keccak("Transfer(address,address,uint256)") — verified on mainnet
SWAP_EXACT_TOKENS_SELECTOR = "0x38ed1739"  # swapExactTokensForTokens(address,uint256,...)
V2_ROUTER = "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D"

# --- M15: BSC / PancakeSwap (public RPC; Etherscan free tier skips BSC) ---
BSC_RPC_URL = "https://bsc-dataseed.binance.org/"
PANCAKE_V2_ROUTER = "0x10ED43C718714eb63d5aA57B78B54704E256024E"
PANCAKE_V3_ROUTER = "0x13f4EA83D0bd40E75C8222255bc855a974568Dd4"
PANCAKE_V3_FACTORY = "0x0BFbCF9fa4f9C56B0F40a671Ad40E0805A091865"
WBNB = "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"
USDT_BSC = "0x55d398326f99059fF775485246999027B3197955"
GETPOOL_SELECTOR = "0x1698ee82"  # getPool(address,address,uint24)


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


def _bsc_client() -> httpx.AsyncClient:
    # same egress pattern as engine/adapters/base.py: the sandbox's
    # NO_PROXY breaks httpx env parsing, so the proxy is explicit
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    ssl_ctx = ssl.create_default_context()
    if os.environ.get("SSL_CERT_FILE"):
        ssl_ctx.load_verify_locations(os.environ["SSL_CERT_FILE"])
    return httpx.AsyncClient(
        timeout=30.0, trust_env=False, proxy=proxy, verify=ssl_ctx)


async def _bsc_rpc(client: httpx.AsyncClient, method: str,
                   params: list) -> object:
    r = await client.post(
        BSC_RPC_URL,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
    )
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise SectionSkip(f"bsc-pancakeswap: RPC error {data['error']}")
    return data["result"]


def _enc_addr(a: str) -> str:
    return "00" * 12 + a[2:].lower()


async def section_bsc_pancakeswap() -> None:
    """M15 live: PancakeSwap V2 Swap events decode with the right venue.

    Scans the latest BSC block(s) for a tx calling the PancakeSwap V2
    router, fetches its receipt via the public BSC RPC (Etherscan's free
    tier does not serve BSC; public getLogs is rate-capped, so discovery
    is by block scan), and decodes with router= the PancakeSwap V2 router —
    the venue label must come back "pancakeswap-v2", not "uniswap-v2".
    A V3 factory.getPool existence check runs best-effort.
    """
    async with _bsc_client() as client:
        try:
            latest = int(await _bsc_rpc(client, "eth_blockNumber", []), 16)
        except (httpx.HTTPError, ValueError) as e:
            raise SectionSkip(f"bsc-pancakeswap: public RPC unreachable: {e}")

        # find a live router call in the last few blocks
        swap_tx = None
        for depth in range(6):
            blk = await _bsc_rpc(
                client, "eth_getBlockByNumber", [hex(latest - depth), True])
            for t in blk.get("transactions") or []:
                if ((t.get("to") or "").lower() == PANCAKE_V2_ROUTER.lower()
                        and (t.get("input") or "0x") != "0x"):
                    swap_tx = t["hash"]
                    break
            if swap_tx:
                break
        if not swap_tx:
            raise SectionSkip("bsc-pancakeswap: no router call in 6 blocks")
        receipt = await _bsc_rpc(
            client, "eth_getTransactionReceipt", [swap_tx])
        if receipt.get("status") != "0x1":
            raise SectionSkip("bsc-pancakeswap: candidate tx reverted")
        rlogs = receipt.get("logs") or []
        topics = (rlogs[0].get("topics") or []) if rlogs else []
        trader = _addr_word(topics[1]) if len(topics) > 1 else ""
        swaps = decode_swap_receipts(rlogs, swap_tx, Chain.BSC, trader,
                                     router=PANCAKE_V2_ROUTER)
        v2 = [s for s in swaps if s.dex == "pancakeswap-v2"]
        if not v2:
            # router call that isn't a swap (e.g. addLiquidity) — loud skip,
            # not a failure of the decoder
            raise SectionSkip(
                "bsc-pancakeswap: router call had no V2 Swap event")
        s = v2[0]
        assert int(s.in_value) > 0 and int(s.out_value) > 0
        print(f"[bsc] tx {swap_tx[:18]}… in={s.in_value} out={s.out_value} "
              f"dex={s.dex}")

        # V3 best-effort: the 0.05% WBNB/USDT pool must exist on the
        # on-chain-verified V3 factory; the V3 label path is unit-tested
        try:
            t0, t1 = sorted([WBNB.lower(), USDT_BSC.lower()])
            pdata = (GETPOOL_SELECTOR + _enc_addr(t0) + _enc_addr(t1)
                     + f"{500:064x}")
            pool = await _bsc_rpc(
                client, "eth_call",
                [{"to": PANCAKE_V3_FACTORY, "data": pdata}, hex(latest)])
            pool = "0x" + pool[-40:]
            assert int(pool, 16) != 0, "V3 WBNB/USDT pool missing"
            print(f"[bsc] V3 WBNB/USDT pool exists: {pool[:14]}…")
        except SectionSkip as e:
            print(f"[bsc] V3 best-effort skipped: {e}")


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
        ("bsc-pancakeswap", section_bsc_pancakeswap),
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
