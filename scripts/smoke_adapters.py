"""Live smoke tests for M1/M2 chain adapters. Reads keys from .env.

Run: uv run python scripts/smoke_adapters.py
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def load_env() -> None:
    for line in Path(".env").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


async def main() -> None:
    from engine.adapters.base import Chain
    from engine.adapters.evm import EvmAdapter
    from engine.adapters.tron import TronAdapter, _to_base58

    load_env()
    eth_key = os.environ["ETHERSCAN_API_KEY"]
    tron_key = os.environ["TRONGRID_API_KEY"]

    # ---- EVM: Ethereum ----
    evm = EvmAdapter(Chain.ETHEREUM, api_key=eth_key)
    vitalik = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
    txs = await evm.get_transactions(vitalik, limit=5)
    print(f"[eth] native txs: {len(txs)}")
    for t in txs[:2]:
        print(f"  {t.tx_hash[:12]}… {t.inputs[0].address[:10]} → "
              f"{t.outputs[0].address[:10]} {t.inputs[0].value} wei @ {t.block_time}")
    toks = await evm.get_token_transfers(vitalik, limit=3)
    print(f"[eth] token transfers: {len(toks)}")
    for t in toks[:2]:
        print(f"  {t.asset.symbol} {t.tx_hash[:12]}… "
              f"{t.inputs[0].address[:10]} → {t.outputs[0].address[:10]}")
    assert txs, "expected ETH transactions for vitalik.eth"

    # ---- EVM: BSC + Polygon via Covalent (Etherscan free tier is ETH-only) ----
    from engine.adapters.covalent import CovalentAdapter
    cov_key = os.environ["COVALENT_API_KEY"]
    vitalik = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
    for chain in (Chain.BSC, Chain.POLYGON):
        a = CovalentAdapter(chain, api_key=cov_key)
        assert await a.health_check(), f"{chain.value} covalent health failed"
        txs = await a.get_transactions(vitalik, limit=5)
        print(f"[{chain.value}] covalent native txs: {len(txs)}")
        if txs:  # parse path is shared code; BSC proves it, Polygon proves reachability
            t = txs[0]
            print(f"  {t.tx_hash[:12]}… {t.asset.symbol} "
                  f"{t.inputs[0].address[:10]} → {t.outputs[0].address[:10] if t.outputs else '—'}")
            toks = await a.get_token_transfers(vitalik, limit=5)
            print(f"[{chain.value}] covalent token transfers: {len(toks)}")
            if toks:
                print(f"  {toks[0].asset.symbol} {toks[0].tx_hash[:12]}… "
                      f"{toks[0].inputs[0].address[:10]} → {toks[0].outputs[0].address[:10]}")
        await a.close()
    # BSC must have real data (parse validation); Polygon reachability is enough
    bsc = CovalentAdapter(Chain.BSC, api_key=cov_key)
    assert await bsc.get_transactions(vitalik, limit=3), "expected BSC txs"
    await bsc.close()

    # ---- Tron ----
    tron = TronAdapter(api_key=tron_key)
    assert await tron.health_check(), "trongrid health check failed"
    print("[tron] health: True")

    # bootstrap a real active address from USDT Transfer events (no guessing)
    events = await tron._get_json(
        "https://api.trongrid.io/v1/contracts/"
        "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t/events",
        params={"event_name": "Transfer", "only_confirmed": "true",
                "limit": 5, "order_by": "block_timestamp,desc"},
        headers=tron._headers(),
    )
    ev_list = (events.get("data") or [])
    print(f"[tron] USDT Transfer events: {len(ev_list)}")
    assert ev_list, "expected USDT transfer events"
    active = _to_base58(ev_list[0]["result"]["to"])  # events use 0x-hex; normalize
    print(f"[tron] probing active address {active[:12]}…")
    assert active.startswith("T"), f"address not normalized: {active}"

    trc20 = await tron.get_token_transfers(active, limit=5)
    print(f"[tron] TRC-20 transfers: {len(trc20)}")
    for t in trc20[:2]:
        print(f"  {t.asset.symbol} {t.tx_hash[:12]}… "
              f"{t.inputs[0].address[:10]} → {t.outputs[0].address[:10]}")
    assert trc20, "expected TRC-20 transfers"

    native = await tron.get_transactions(active, limit=50)
    trx_only = [t for t in native if t.asset.symbol == "TRX"]
    print(f"[tron] native TransferContract txs (of {len(native)} scanned): {len(trx_only)}")
    for t in trx_only[:2]:
        assert t.inputs[0].address.startswith("T"), f"hex leak: {t.inputs[0].address}"
        print(f"  TRX {t.tx_hash[:12]}… {t.inputs[0].address[:10]} → "
              f"{t.outputs[0].address[:10]} {t.inputs[0].value} sun")

    # hex→base58 sanity on a synthetic vector (round-trip shape, not a guess)
    assert _to_base58("41" + "00" * 20).startswith("T")
    assert _to_base58("TXYZ") == "TXYZ"

    # ---- Bitcoin (mempool.space; Blockchair keyless is IP-blacklisted → 430) ----
    from engine.adapters.bitcoin import BitcoinAdapter
    btc = BitcoinAdapter()
    assert await btc.health_check(), "mempool.space health failed"
    genesis = "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"
    btxs = await btc.get_transactions(genesis, limit=5)
    print(f"[btc] txs: {len(btxs)}")
    for t in btxs[:2]:
        print(f"  {t.tx_hash[:12]}… in={len(t.inputs)} out={len(t.outputs)} "
              f"block={t.block_number} fee={t.fee} sat")
    assert btxs, "expected BTC txs"
    t0 = btxs[0]
    assert t0.asset.symbol == "BTC" and t0.asset.decimals == 8
    assert all(p.value.isdigit() for p in t0.inputs + t0.outputs), "non-int sat value"
    await btc.close()

    # ---- Solana (public RPC; Solscan key plan 401s every v2.0 endpoint) ----
    from engine.adapters.solana import SolanaAdapter, WSOL_MINT
    sol = SolanaAdapter()
    assert await sol.health_check(), "solana RPC health failed"
    print("[sol] health: True")

    # SPL: wSOL mint always has activity
    spl = await sol.get_token_transfers(WSOL_MINT, limit=3)
    print(f"[sol] SPL transfers: {len(spl)}")
    for t in spl[:2]:
        print(f"  mint={t.asset.contract[:12]}… {t.tx_hash[:12]}… "
              f"{t.inputs[0].address[:10]} → {t.outputs[0].address[:10]} "
              f"{t.inputs[0].value}")
    assert spl, "expected SPL transfers"
    assert all(t.asset.contract for t in spl), "SPL transfer missing mint"

    # native: bootstrap a real SOL-active wallet = fee payer of a recent wSOL tx
    sigs = await sol.get_signatures(WSOL_MINT, 3)
    assert sigs, "expected signatures for wSOL mint"
    detail = await sol._tx_detail(sigs[0]["signature"])
    payer = detail["transaction"]["message"]["accountKeys"][0]["pubkey"]
    print(f"[sol] probing active wallet {payer[:12]}…")
    native = await sol.get_transactions(payer, limit=25)
    print(f"[sol] native SOL transfers: {len(native)}")
    for t in native[:2]:
        print(f"  {t.tx_hash[:12]}… {t.inputs[0].address[:10]} → "
              f"{t.outputs[0].address[:10]} {t.inputs[0].value} lamports")
    assert native, "expected native SOL transfers"
    assert native[0].asset.symbol == "SOL"
    await sol.close()

    await evm.close()
    await tron.close()
    print("\nSMOKE OK — EVM (eth/bsc/polygon) + Tron + Bitcoin + Solana live.")


if __name__ == "__main__":
    asyncio.run(main())
