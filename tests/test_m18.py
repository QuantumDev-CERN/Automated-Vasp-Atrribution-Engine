"""M18 tests: swap-service hot-wallet registry, classification, traversal
terminal, risk signal, and report rendering.

All synthetic — no network. Live behavior is covered by
scripts/smoke_m18.py.
"""
import re

from engine.adapters.base import (
    Asset,
    AssetKind,
    CanonicalTx,
    Chain,
    FlowParty,
)
from engine.classifier.hops import HopKind, classify_edge, classify_graph
from engine.graph.builder import TxGraph
from engine.knowledge.swap_services import (
    ROLE_CONFIDENCE,
    SWAP_SERVICE_ADDRESSES,
    SwapServiceInfo,
    swap_service_for,
)
from engine.scoring.risk import score_risk
from engine.traversal.engine import SwapDeposit, traverse

ETH = Chain.ETHEREUM
BSC = Chain.BSC

USER = "0xUser000000000000000000000000000000000001"

# Registry spot-checks (full list asserted structurally below)
CN_HW2 = "0xA96Be652A08D9905F15B7FbE2255708709BeCD09"      # changenow hot-wallet
CN_HW4 = "0xEbA88149813BEc1cCcccFDb0daCEFaaa5DE94cB1"      # changenow hot-wallet
CN_DEP = "0x3f3Ee0a9cAC2d01DB44001eca3E8382fbe40207B"      # changenow deposit
FF_HW2 = "0x4E5B2e1dc63F6b91cb6Cd759936495434C7e972F"      # fixedfloat hot-wallet
FF_DEP = "0xc2cd4b1e89FE561702b72FF9329303A2DD0dc225"      # fixedfloat deposit
SS_ATTR = "0x7bacd3e83522f484bc5128ea93bf7290f1f1b9e5"     # simpleswap attributed
CN_EOA = "0xe2d60CFE3cF8B2079C7DF0144c5b28C03469775C"      # same key: ETH attributed, BSC hot-wallet
FF_BSC_HW = "0x4727250679294802377dD6cA6541B8E459077c95"   # fixedfloat BSC hot-wallet


def _native(symbol="ETH"):
    return Asset(kind=AssetKind.NATIVE, chain=ETH, symbol=symbol, decimals=18)


def _tx(tx_hash, asset, src, dst, value):
    return CanonicalTx(
        tx_hash=tx_hash, chain=asset.chain, asset=asset,
        inputs=[FlowParty(address=src, value=str(value))],
        outputs=[FlowParty(address=dst, value=str(value))],
    )


def _classify(tx):
    g = TxGraph.build([tx])
    cls = classify_graph(g)
    return cls, g


# ---------------------------------------------------------------- registry

def test_registry_addresses_are_valid_20_byte_evm():
    seen = set()
    assert len(SWAP_SERVICE_ADDRESSES) >= 12, "registry seed must hold the curated entries"
    for (chain, addr), info in SWAP_SERVICE_ADDRESSES.items():
        assert re.fullmatch(r"0x[0-9a-f]{40}", addr), f"malformed: {addr}"
        assert addr == info.address.lower(), "lookup key must match entry"
        key = (chain, addr)
        assert key not in seen, f"duplicate registry entry: {key}"
        seen.add(key)
        # round-trip through the public lookup
        assert swap_service_for(info.chain, info.address) is info
        assert swap_service_for(info.chain, info.address.upper()) is info


def test_registry_roles_confidence_and_provenance():
    for info in SWAP_SERVICE_ADDRESSES.values():
        assert info.role in ROLE_CONFIDENCE, f"bad role: {info.role}"
        assert info.confidence == ROLE_CONFIDENCE[info.role]
        assert 0 < info.confidence <= 1
        assert info.name in ("changenow", "fixedfloat", "simpleswap")
        assert info.source, "every entry needs a named source"
        assert info.source_url.startswith("https://"), "source must be a page URL"
        assert info.verified, "every entry needs an on-chain verification note"
        assert info.label, "every entry keeps the explorer's label text"


def test_registry_chain_scoping():
    # FixedFloat's Ethereum hot wallet 2 is a different key from its BSC
    # hot wallet — lookups must not leak across chains.
    assert swap_service_for(ETH, FF_HW2) is not None
    assert swap_service_for(BSC, FF_HW2) is None
    assert swap_service_for(BSC, FF_BSC_HW) is not None
    assert swap_service_for(ETH, FF_BSC_HW) is None
    # The ChangeNOW EOA is deliberately registered on both chains.
    assert swap_service_for(ETH, CN_EOA) is not None
    assert swap_service_for(BSC, CN_EOA) is not None
    assert swap_service_for(ETH, CN_EOA).role == "attributed"
    assert swap_service_for(BSC, CN_EOA).role == "hot-wallet"
    # Unknown addresses and chains stay clean.
    assert swap_service_for(ETH, USER) is None
    assert swap_service_for(Chain.TRON, CN_HW2) is None


# ---------------------------------------------------------------- classifier

def test_classifier_swap_service_deposit():
    tx = _tx("0xsw1", _native(), USER, CN_HW2, 10**18)
    cls, _ = _classify(tx)
    c = next(iter(cls.values()))
    assert c.kind == HopKind.SWAP_SERVICE
    assert c.confidence == 0.90  # hot-wallet tier
    assert c.details["swap_service"] == "changenow"
    assert c.details["role"] == "hot-wallet"


def test_classifier_tier_confidence():
    for addr, want in ((FF_DEP, 0.85), (SS_ATTR, 0.75)):
        tx = _tx("0xsw-" + addr[:6], _native(), USER, addr, 10**18)
        cls, _ = _classify(tx)
        c = next(iter(cls.values()))
        assert c.kind == HopKind.SWAP_SERVICE, addr
        assert c.confidence == want, addr


def test_classifier_registry_beats_sweep_heuristic():
    # 5 inputs -> 1 output into a swap-service address: the registry hit
    # must win over the sweep heuristic (same rule as mixer/bridge).
    tx = CanonicalTx(
        tx_hash="0xsw2", chain=ETH, asset=_native(),
        inputs=[FlowParty(address=f"0xIn{i:040d}", value="200000000000000000")
                for i in range(5)],
        outputs=[FlowParty(address=CN_HW4, value="1000000000000000000")],
    )
    cls, _ = _classify(tx)
    kinds = {c.kind for c in cls.values() if c.details.get("tx_hash") == "0xsw2"}
    assert HopKind.SWAP_SERVICE in kinds
    assert HopKind.SWEEP_CANDIDATE not in kinds


def test_classifier_service_as_source_is_not_swap_service():
    # Payout direction (service -> user) is not classified: only deposits
    # into the service are the custodial-swap fingerprint.
    tx = _tx("0xsw3", _native(), CN_HW2, USER, 10**18)
    cls, _ = _classify(tx)
    c = next(iter(cls.values()))
    assert c.kind != HopKind.SWAP_SERVICE
    assert c.kind == HopKind.DIRECT_TRANSFER


def test_classifier_unknown_address_unaffected():
    tx = _tx("0xsw4", _native(), USER,
             "0xDead000000000000000000000000000000000002", 10**18)
    cls, _ = _classify(tx)
    c = next(iter(cls.values()))
    assert c.kind == HopKind.DIRECT_TRANSFER


# ---------------------------------------------------------------- traversal

def test_traversal_swap_service_stops_and_records():
    tx = _tx("0xsw5", _native(), USER, FF_HW2, 2 * 10**18)
    g = TxGraph.build([tx])
    r = traverse(g, USER)
    assert any(t.reason == "swap-service" for t in r.terminals)
    assert not any(t.reason.startswith("unhandled-hop") for t in r.terminals)
    assert len(r.swap_deposits) == 1
    d = r.swap_deposits[0]
    assert isinstance(d, SwapDeposit)
    assert d.service == "fixedfloat" and d.role == "hot-wallet"
    assert d.address == USER and d.value == str(2 * 10**18)
    assert d.chain == "ethereum" and d.tx_hash == "0xsw5"


def test_traversal_swap_service_is_terminal_not_dead_end():
    # The hop before the service is walked; nothing is walked past it.
    tx1 = _tx("0xsw6a", _native(), USER, "0xMid0000000000000000000000000000000000003",
              10**18)
    tx2 = _tx("0xsw6b", _native(), "0xMid0000000000000000000000000000000000003",
              CN_DEP, 10**18)
    g = TxGraph.build([tx1, tx2])
    r = traverse(g, USER)
    terminal = next(t for t in r.terminals if t.reason == "swap-service")
    assert terminal.address == CN_DEP
    assert len(r.swap_deposits) == 1
    assert r.swap_deposits[0].service == "changenow"


# ---------------------------------------------------------------- scoring

def test_risk_signal_swap_service_terminal():
    tx = _tx("0xsw7", _native(), USER, SS_ATTR, 10**18)
    g = TxGraph.build([tx])
    r = traverse(g, USER)
    risk = score_risk(r.visited, terminal_reason="swap-service")
    sig = next(s for s in risk.signals if s.name == "swap-service")
    assert sig.points == 25
    assert risk.total >= 25


def test_confidence_discount_applies_to_swap_service():
    from types import SimpleNamespace

    from engine.scoring.confidence import KIND_DISCOUNT, score_attribution
    assert KIND_DISCOUNT["swap-service"] == 0.60
    visited = [
        SimpleNamespace(address=USER, via_kind=None, via_confidence=None),
        SimpleNamespace(address=CN_HW2, via_kind="swap-service",
                        via_confidence=0.90),
    ]
    s = score_attribution(visited, terminal_reason="swap-service")
    assert s.overall == 0.90 * 0.60
