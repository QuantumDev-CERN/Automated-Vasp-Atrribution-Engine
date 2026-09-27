"""M4 tests: DEX decoding, bridge/mixer registries, SPL owner resolution,
correlation scoring, and traversal strategies for the new hop kinds.

All synthetic — no network. Live behavior is covered by scripts/smoke_m4.py.
"""
import asyncio
from datetime import datetime, timedelta, timezone

from engine.adapters.base import (
    Asset,
    AssetKind,
    CanonicalTx,
    Chain,
    FlowParty,
)
from engine.adapters.solana import SolanaAdapter
from engine.classifier.hops import HopKind, classify_graph
from engine.decoding.correlation import (
    Deposit,
    WithdrawalCandidate,
    rank_candidates,
    score_candidate,
)
from engine.decoding.swaps import decode_swap_receipt, detect_dex_swaps
from engine.graph.builder import TxGraph
from engine.knowledge.bridges import bridge_for
from engine.knowledge.dex import SWAP_TOPIC_V2, SWAP_TOPIC_V3, dex_for
from engine.knowledge.mixers import mixer_for
from engine.traversal.engine import traverse

ETH = Chain.ETHEREUM
TRADER = "0xTrader000000000000000000000000000000000001"
ROUTER_V2 = "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D"  # Uniswap V2
STARGATE = "0x8731d54E9D02c286767d56ac03e8037C07e01e98"
TC1 = "0x47CE0C6eD5B0Ce3d3A51fdb1C52DC66a7c3c2936"  # Tornado 1 ETH pool (denomination() verified on-chain 2026-09-27)
USDT = "0xdAC17F958D2ee523a2206206994597C13D831ec7"
WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"


def _tok(symbol, contract, decimals=18):
    return Asset(kind=AssetKind.TOKEN, chain=ETH, symbol=symbol,
                 contract=contract, decimals=decimals)


def _native(symbol="ETH"):
    return Asset(kind=AssetKind.NATIVE, chain=ETH, symbol=symbol, decimals=18)


def _tx(tx_hash, asset, src, dst, value):
    return CanonicalTx(
        tx_hash=tx_hash, chain=ETH, asset=asset,
        inputs=[FlowParty(address=src, value=str(value))],
        outputs=[FlowParty(address=dst, value=str(value))],
    )


# ---------------------------------------------------------------- registries

def test_dex_registry_hit():
    d = dex_for(ETH, ROUTER_V2)
    assert d is not None and d.name == "uniswap-v2" and d.kind == "v2"
    assert dex_for(ETH, TRADER) is None


def test_bridge_registry_hit():
    b = bridge_for(ETH, STARGATE)
    assert b is not None and b.name == "stargate"
    assert bridge_for(ETH, TRADER) is None


def test_mixer_registry_hit():
    m = mixer_for(ETH, TC1)
    assert m is not None and m.name == "tornado-cash"
    assert m.denomination == "1000000000000000000"
    assert mixer_for(ETH, TRADER) is None


# ---------------------------------------------------------------- pairing

def _swap_legs():
    h = "0xswap1"
    out_leg = _tx(h, _tok("USDT", USDT, 6), TRADER, ROUTER_V2, 1_000_000)
    in_leg = _tx(h, _tok("WETH", WETH), ROUTER_V2, TRADER, 500_000_000_000_000)
    return h, [out_leg, in_leg]


def test_pairing_basic():
    h, legs = _swap_legs()
    swaps = detect_dex_swaps(legs)
    assert len(swaps) == 1
    s = swaps[0]
    assert s.tx_hash == h and s.trader == TRADER
    assert s.in_symbol == "USDT" and s.in_contract == USDT
    assert s.out_symbol == "WETH" and s.out_contract == WETH
    assert s.dex == "uniswap-v2" and s.router == ROUTER_V2
    assert s.confidence >= 0.9  # router bonus applied
    assert legs[0].dex_swap is s and legs[1].dex_swap is s


def test_pairing_native_leg():
    h = "0xswap2"
    out_leg = _tx(h, _native(), TRADER, ROUTER_V2, 10**18)
    in_leg = _tx(h, _tok("WETH", WETH), ROUTER_V2, TRADER, 10**18)
    swaps = detect_dex_swaps([out_leg, in_leg])
    assert len(swaps) == 1
    s = swaps[0]
    assert s.in_contract is None and s.in_symbol == "ETH"
    assert s.out_symbol == "WETH"


def test_pairing_ambiguous_skipped():
    h = "0xswap3"
    legs = [
        _tx(h, _tok("USDT", USDT, 6), TRADER, ROUTER_V2, 100),
        _tx(h, _tok("WETH", WETH), ROUTER_V2, TRADER, 50),
        _tx(h, _tok("DAI", "0x6B175474E89094C44Da98b954EedeAC495271d0F"),
            "0xOther000000000000000000000000000000000002", TRADER, 70),
    ]
    assert detect_dex_swaps(legs) == []
    assert all(t.dex_swap is None for t in legs)


def test_pairing_same_asset_no_swap():
    h = "0xswap4"
    legs = [
        _tx(h, _tok("USDT", USDT, 6), TRADER, ROUTER_V2, 100),
        _tx(h, _tok("USDT", USDT, 6), ROUTER_V2, TRADER, 100),
    ]
    assert detect_dex_swaps(legs) == []


# ---------------------------------------------------------------- event logs

def _word(v: int) -> str:
    return "0x" + f"{v & (2**256 - 1):064x}"


def _addr_topic(a: str) -> str:
    return "0x" + "00" * 12 + a[2:].lower()


def test_decode_v2_swap_event():
    # token0 -> token1: 1000 token0 in, 900 token1 out
    logs = [{
        "address": "0xPool00000000000000000000000000000000000001",
        "topics": [SWAP_TOPIC_V2, _addr_topic(ROUTER_V2), _addr_topic(TRADER)],
        "data": "0x" + "".join(
            _word(v)[2:] for v in (1000, 0, 0, 900)),
    }]
    s = decode_swap_receipt(logs, "0xrx", ETH, TRADER,
                            token0=USDT, token1=WETH)
    assert s is not None
    assert s.method == "event-log" and s.dex == "uniswap-v2"
    assert s.in_contract == USDT and s.in_value == "1000"
    assert s.out_contract == WETH and s.out_value == "900"


def test_decode_v3_swap_event():
    # amount0 = -500 (out), amount1 = +450 (in) -> token1 in, token0 out
    logs = [{
        "address": "0xPool00000000000000000000000000000000000002",
        "topics": [SWAP_TOPIC_V3, _addr_topic(ROUTER_V2), _addr_topic(TRADER)],
        "data": "0x" + "".join(
            _word(v)[2:] for v in (2**256 - 500, 450, 0, 0, 0)),
    }]
    s = decode_swap_receipt(logs, "0xrx", ETH, TRADER,
                            token0=USDT, token1=WETH)
    assert s is not None and s.dex == "uniswap-v3"
    assert s.in_contract == WETH and s.in_value == "450"
    assert s.out_contract == USDT and s.out_value == "500"


def test_decode_no_swap_logs():
    logs = [{"address": "0xabc", "topics": [_word(1234)], "data": "0x"}]
    assert decode_swap_receipt(logs, "0xrx", ETH, TRADER) is None


# ---------------------------------------------------------------- classifier

def _classify(*txs):
    g = TxGraph.build(list(txs))
    return classify_graph(g), g


def test_classifier_dex_swap():
    _, legs = _swap_legs()
    detect_dex_swaps(legs)
    cls, _ = _classify(*legs)
    kinds = {c.kind for c in cls.values()}
    assert kinds == {HopKind.DEX_SWAP}
    c = next(iter(cls.values()))
    assert c.confidence >= 0.9
    assert c.details["dex"] == "uniswap-v2"
    assert c.details["in_symbol"] == "USDT"


def test_classifier_bridge_lock():
    tx = _tx("0xbr", _tok("USDC", "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", 6),
             TRADER, STARGATE, 5_000_000)
    cls, _ = _classify(tx)
    c = next(iter(cls.values()))
    assert c.kind == HopKind.BRIDGE_LOCK
    assert c.details["direction"] == "lock"
    assert c.details["bridge"] == "stargate"


def test_classifier_bridge_release():
    tx = _tx("0xbr2", _tok("USDC", "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", 6),
             STARGATE, TRADER, 4_990_000)
    cls, _ = _classify(tx)
    c = next(iter(cls.values()))
    assert c.kind == HopKind.BRIDGE_LOCK
    assert c.details["direction"] == "release"


def test_classifier_mixer_deposit():
    tx = _tx("0xmx", _native(), TRADER, TC1, 10**18)
    cls, _ = _classify(tx)
    c = next(iter(cls.values()))
    assert c.kind == HopKind.MIXER_DEPOSIT
    assert c.confidence == 0.95
    assert c.details["mixer"] == "tornado-cash"


def test_classifier_registry_beats_heuristic():
    # 5 inputs -> 1 output into a mixer pool: mixer wins over sweep
    tx = CanonicalTx(
        tx_hash="0xmx2", chain=ETH, asset=_native(),
        inputs=[FlowParty(address=f"0xIn{i:040d}", value="200000000000000000")
                for i in range(5)],
        outputs=[FlowParty(address=TC1, value="1000000000000000000")],
    )
    cls, _ = _classify(tx)
    kinds = {c.kind for c in cls.values() if c.details.get("tx_hash") == "0xmx2"}
    assert HopKind.MIXER_DEPOSIT in kinds


# ---------------------------------------------------------------- traversal

def test_traversal_dex_swap_continues():
    _, legs = _swap_legs()
    detect_dex_swaps(legs)
    g = TxGraph.build(legs)
    r = traverse(g, TRADER)
    kinds = {v.via_kind for v in r.visited if v.via_kind}
    assert HopKind.DEX_SWAP.value in kinds
    node = next(v for v in r.visited if v.via_kind == HopKind.DEX_SWAP.value)
    assert node.note == "USDT->WETH"
    assert not any(t.reason == "bridge-lock" for t in r.terminals)


def test_traversal_bridge_lock_stops():
    tx = _tx("0xbr", _tok("USDC", "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", 6),
             TRADER, STARGATE, 5_000_000)
    g = TxGraph.build([tx])
    r = traverse(g, TRADER)
    assert any(t.reason == "bridge-lock" for t in r.terminals)
    assert len(r.bridge_deposits) == 1
    d = r.bridge_deposits[0]
    assert d.bridge == "stargate" and d.direction == "lock"
    assert d.value == "5000000" and d.address == TRADER


def test_traversal_mixer_deposit_stops():
    tx = _tx("0xmx", _native(), TRADER, TC1, 10**18)
    g = TxGraph.build([tx])
    r = traverse(g, TRADER)
    assert any(t.reason == "mixer-deposit" for t in r.terminals)
    assert "mixer-depositor" in r.labels_applied.get(TRADER, [])


# ---------------------------------------------------------------- SPL owners

def test_spl_owner_resolution():
    TOKEN_ACCT = "TokenAcct1111111111111111111111111111111"
    OWNER = "Wallet11111111111111111111111111111111111"

    async def fake_post(payload):
        assert payload["method"] == "getAccountInfo"
        acct = payload["params"][0]
        if acct == TOKEN_ACCT:
            return {"result": {"value": {"data": {"parsed": {"info": {
                "mint": "So11111111111111111111111111111111111111112",
                "owner": OWNER,
                "tokenAmount": {"amount": "42", "decimals": 9},
            }}}}}}
        return {"result": {"value": None}}  # unresolvable

    async def main():
        sol = SolanaAdapter()
        sol._post_json = fake_post  # type: ignore[method-assign]
        owner = await sol.get_token_account_owner(TOKEN_ACCT)
        assert owner == OWNER
        assert await sol.get_token_account_owner("Nope11111111111111111111111111111111") is None

        from engine.adapters.base import Asset, AssetKind, Chain
        tx = CanonicalTx(
            tx_hash="sig", chain=Chain.SOLANA,
            asset=Asset(kind=AssetKind.TOKEN, chain=Chain.SOLANA,
                        contract="So11111111111111111111111111111111111111112",
                        decimals=9),
            inputs=[FlowParty(address="TokenAcct1111111111111111111111111111111",
                              value="42")],
            outputs=[FlowParty(address="TokenAcct2222222222222222222222222222222",
                               value="42")],
        )
        out = await sol.resolve_token_owners([tx])
        assert out[0].inputs[0].address == OWNER
        # unresolvable account keeps its address
        assert out[0].outputs[0].address == "TokenAcct2222222222222222222222222222222"
        assert tx.inputs[0].address.startswith("TokenAcct")  # original untouched
        await sol.close()

    asyncio.run(main())


# ---------------------------------------------------------------- correlation

def _dep():
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return Deposit(tx_hash="0xdep", chain="ethereum", address=TRADER,
                   asset_contract=USDT, asset_symbol="USDT",
                   value="5000000", block_time=t0, venue="stargate"), t0


def test_correlation_ranks_best_first():
    dep, t0 = _dep()
    cands = [
        WithdrawalCandidate("0xlate", "bsc", "0xR1", USDT, "USDT",
                            "4990000", t0 + timedelta(minutes=50)),
        WithdrawalCandidate("0xbest", "bsc", "0xR2", USDT, "USDT",
                            "4995000", t0 + timedelta(minutes=5)),
        WithdrawalCandidate("0xwrong-asset", "bsc", "0xR3", WETH, "WETH",
                            "4995000", t0 + timedelta(minutes=5)),
        WithdrawalCandidate("0xtoo-late", "bsc", "0xR4", USDT, "USDT",
                            "4995000", t0 + timedelta(hours=5)),
        WithdrawalCandidate("0xtoo-much", "bsc", "0xR5", USDT, "USDT",
                            "6000000", t0 + timedelta(minutes=5)),
    ]
    ranked = rank_candidates(dep, cands)
    assert [c.tx_hash for c, _ in ranked] == ["0xbest", "0xlate"]
    assert ranked[0][1] > ranked[1][1]


def test_correlation_none_when_impossible():
    dep, t0 = _dep()
    cand = WithdrawalCandidate("0xno", "bsc", "0xR", WETH, "WETH",
                               "4995000", t0 + timedelta(minutes=5))
    assert score_candidate(dep, cand) is None


# ------------------------------------------------------- registry hygiene

def test_registry_addresses_are_valid_20_byte_evm():
    """Every registry entry must be exactly 0x + 40 hex chars.

    This is the tripwire that would have caught the corrupted Tornado and
    Wormhole addresses from the pre-audit draft (one entry was 36 chars,
    others had transposed suffixes). It cannot catch a well-formed but
    wrong address — that needs the on-chain verification documented in
    each registry module's docstring.
    """
    import re
    from engine.knowledge import bridges, dex, mixers

    entries: list[tuple[str, str, str]] = []  # (registry, name, address)
    for m in mixers._MIXERS:
        entries.append(("mixer", m.name, m.pool))
    for b in bridges._BRIDGES:
        entries.append(("bridge", b.name, b.contract))
    for d in dex._DEXES:
        entries.append(("dex", d.name, d.router))

    assert entries, "registries must not be empty"
    bad = [f"{reg}/{name}: {addr!r}"
           for reg, name, addr in entries
           if not re.fullmatch(r"0x[0-9a-fA-F]{40}", addr)]
    assert not bad, f"malformed EVM addresses in registries: {bad}"

    # no duplicate (registry, chain, address) keys
    seen: set[tuple[str, str, str]] = set()
    dupes: list[str] = []
    for reg, mod, key in [
        ("mixer", mixers, mixers.MIXER_POOLS),
        ("bridge", bridges, bridges.BRIDGE_CONTRACTS),
        ("dex", dex, dex.DEX_ROUTERS),
    ]:
        for k in key:
            if (reg, *k) in seen:
                dupes.append(f"{reg}: {k}")
            seen.add((reg, *k))
    assert not dupes, f"duplicate registry entries: {dupes}"

    # mixer denominations are positive integers in smallest units
    for m in mixers._MIXERS:
        assert m.denomination.isdigit() and int(m.denomination) > 0, \
            f"bad denomination for {m.name} pool {m.pool}"

    # lookup round-trips: every registry entry resolves through its own API
    for m in mixers._MIXERS:
        assert mixer_for(m.chain, m.pool) is m
        assert mixer_for(m.chain, m.pool.upper()) is m  # case-insensitive
    for b in bridges._BRIDGES:
        assert bridge_for(b.chain, b.contract) is b
    for d in dex._DEXES:
        assert dex_for(d.chain, d.router) is d
        assert dex_for(d.chain, d.router).kind in ("v2", "v3")
