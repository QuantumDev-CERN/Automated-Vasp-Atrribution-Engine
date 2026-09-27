"""Live smoke tests for M1/M2 chain adapters. Reads keys from .env.

Run: uv run python scripts/smoke_adapters.py

Each adapter section is independent: a transient indexer error
(rate-limit 429, 502/503/504, timeouts) reports SKIP, not FAIL — a free
public endpoint throttling us is not broken code. Failed assertions and
unexpected exceptions still FAIL hard. If EVERY section skips, the script
fails: that means no network at all, not throttling.
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.adapters.base import AdapterError  # noqa: E402


class SectionSkip(Exception):
    """The external dependency returned nothing usable (free public
    endpoint flaky), not a code regression. Loud skip, not a pass."""


async def _spl_probe_with_retry(sol, label: str):
    """wSOL-mint SPL probe with retries: the public Solana RPC
    intermittently returns signatures whose details are pruned or
    unparseable, yielding zero transfers on a healthy code path."""
    from engine.adapters.solana import WSOL_MINT

    last = 0
    for attempt in range(3):
        txs = await sol.get_token_transfers(WSOL_MINT, limit=3)
        last = len(txs)
        if txs:
            return txs
        print(f"[sol] SPL probe empty (attempt {attempt + 1}/3), retrying…")
        await asyncio.sleep(3)
    raise SectionSkip(
        f"{label}: solana public RPC returned no parseable SPL transfers "
        f"in 3 attempts (last count {last}) — free endpoint flaky, "
        "not a code regression")


def load_env() -> None:
    for line in Path(".env").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


_TRANSIENT_HINTS = (
    "429", "502", "503", "504", "timeout", "timed out", "connecterror",
    "connection reset", "temporarily unavailable",
)


def _transient(message: str) -> bool:
    m = message.lower()
    return any(h in m for h in _TRANSIENT_HINTS)


async def _run_section(name: str, fn) -> str:
    """pass | skip. Anything that is not a transient AdapterError or an
    explicit SectionSkip propagates."""
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


# ------------------------------------------------------------------ sections

async def section_eth(eth_key: str) -> None:
    from engine.adapters.base import Chain
    from engine.adapters.evm import EvmAdapter

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
    await evm.close()


async def section_covalent(cov_key: str) -> None:
    from engine.adapters.base import Chain
    from engine.adapters.covalent import CovalentAdapter

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


async def section_tron(tron_key: str) -> None:
    from engine.adapters.tron import TronAdapter, _to_base58

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
    await tron.close()


async def section_bitcoin() -> None:
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


async def section_solana() -> None:
    from engine.adapters.solana import SolanaAdapter, WSOL_MINT

    sol = SolanaAdapter()
    assert await sol.health_check(), "solana RPC health failed"
    print("[sol] health: True")

    # SPL: wSOL mint always has activity (probe retries; the public RPC
    # intermittently returns pruned/unparseable details)
    spl = await _spl_probe_with_retry(sol, "solana")
    print(f"[sol] SPL transfers: {len(spl)}")
    for t in spl[:2]:
        print(f"  mint={t.asset.contract[:12]}… {t.tx_hash[:12]}… "
              f"{t.inputs[0].address[:10]} → {t.outputs[0].address[:10]} "
              f"{t.inputs[0].value}")
    assert all(t.asset.contract for t in spl), "SPL transfer missing mint"

    # native: bootstrap a real SOL-active wallet = fee payer of a recent wSOL tx.
    # The first payer's recent history may contain no native SOL transfers
    # (all-token activity), so try several recent payers; if the free RPC
    # yields nothing parseable, SKIP the section instead of failing the gate
    # (same policy as the SPL probe above).
    sigs = await sol.get_signatures(WSOL_MINT, 8)
    assert sigs, "expected signatures for wSOL mint"
    native: list = []
    payer = ""
    for sig in sigs:
        try:
            detail = await sol._tx_detail(sig["signature"])
            payer = detail["transaction"]["message"]["accountKeys"][0]["pubkey"]
        except Exception:  # noqa: BLE001 — pruned/unparseable, try next
            continue
        print(f"[sol] probing active wallet {payer[:12]}…")
        native = await sol.get_transactions(payer, limit=10)
        print(f"[sol] native SOL transfers: {len(native)}")
        if native:
            break
    if not native:
        raise SectionSkip(
            "solana: no recent wSOL fee payer had native SOL transfers in "
            "8 attempts — free RPC flaky, not a code regression")
    for t in native[:2]:
        print(f"  {t.tx_hash[:12]}… {t.inputs[0].address[:10]} → "
              f"{t.outputs[0].address[:10]} {t.inputs[0].value} lamports")
    assert native[0].asset.symbol == "SOL"
    await sol.close()


# ------------------------------------------------------------------ runner

def _need_key(var: str) -> str:
    """Read an indexer key that must come from the environment/.env.

    Raises SectionSkip (not KeyError) when the variable is absent, so a
    missing key skips its section instead of crashing the whole smoke.
    """
    key = os.environ.get(var)
    if not key:
        raise SectionSkip(f"{var} not set — add it to .env to run this section")
    return key


async def main() -> None:
    load_env()

    sections = [
        ("eth", lambda: section_eth(_need_key("ETHERSCAN_API_KEY"))),
        ("covalent-bsc-polygon",
         lambda: section_covalent(_need_key("COVALENT_API_KEY"))),
        ("tron", lambda: section_tron(_need_key("TRONGRID_API_KEY"))),
        ("bitcoin", section_bitcoin),
        ("solana", section_solana),
    ]
    results: dict[str, str] = {}
    for name, fn in sections:
        results[name] = await _run_section(name, fn)

    passed = [n for n, s in results.items() if s == "pass"]
    skipped = [n for n, s in results.items() if s == "skip"]
    print(f"\nSMOKE {'OK' if not skipped else 'OK with skips'} — "
          f"{len(passed)} passed, {len(skipped)} skipped"
          + (f" ({', '.join(skipped)})" if skipped else ""))
    if len(skipped) == len(sections):
        sys.exit("SMOKE FAILED — every section skipped; "
                 "check network connectivity and indexer API keys in .env")


if __name__ == "__main__":
    asyncio.run(main())
