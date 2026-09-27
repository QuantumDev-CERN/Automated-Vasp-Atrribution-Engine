"""M3 tests: graph builder, hop classifier, traversal engine.

All fixtures are synthetic CanonicalTx objects — no network. Live wiring is
covered by scripts/smoke_m3.py.
"""
from datetime import datetime, timezone

from engine.adapters.base import (
    Asset,
    AssetKind,
    CanonicalTx,
    Chain,
    FlowParty,
)
from engine.classifier import HopKind, classify_graph
from engine.graph import TxGraph
from engine.traversal import TraversalConfig, traverse

_BTC = Asset(kind=AssetKind.NATIVE, chain=Chain.BITCOIN, symbol="BTC", decimals=8)
_ETH = Asset(kind=AssetKind.NATIVE, chain=Chain.ETHEREUM, symbol="ETH", decimals=18)

_T0 = datetime(2024, 1, 1, tzinfo=timezone.utc)


def _tx(tx_hash, chain, asset, inputs, outputs):
    """inputs/outputs: list of (address, value_int)."""
    return CanonicalTx(
        tx_hash=tx_hash,
        chain=chain,
        block_time=_T0,
        inputs=[FlowParty(address=a, value=str(v)) for a, v in inputs],
        outputs=[FlowParty(address=a, value=str(v)) for a, v in outputs],
        asset=asset,
    )


# ------------------------------------------------------------------ builder

def test_builder_bipartite_expansion_and_coinbase_skip():
    g = TxGraph()
    n = g.add_tx(
        _tx("t1", Chain.BITCOIN, _BTC,
            [("coinbase", 0), ("miner1", 100)],
            [("alice", 60), ("bob", 40)])
    )
    assert n == 2  # coinbase pseudo-input produces no edges
    assert g.stats() == {"addresses": 3, "transfers": 2, "transactions": 1}
    assert {t for t, _, _ in g.out_edges("miner1")} == {"alice", "bob"}


def test_builder_node_chains_and_first_seen():
    g = TxGraph.build([
        _tx("t1", Chain.ETHEREUM, _ETH, [("a", 10)], [("b", 10)]),
    ])
    assert g.labels("a") == set()
    g.label("a", "suspect")
    assert g.labels("a") == {"suspect"}
    assert g.first_seen("b") == _T0


# ------------------------------------------------------------------ peel

def _peel_chain():
    return [
        _tx("peel1", Chain.BITCOIN, _BTC, [("A", 100_000)], [("B", 90_000), ("C", 9_000)]),
        _tx("peel2", Chain.BITCOIN, _BTC, [("B", 90_000)], [("D", 80_000), ("E", 9_000)]),
    ]


def test_peel_classification():
    g = TxGraph.build(_peel_chain())
    out = classify_graph(g)
    kinds = {(s, d): c.kind for (s, d, _), c in out.items()}
    assert kinds[("A", "B")] == HopKind.PEEL
    assert kinds[("A", "C")] == HopKind.DIRECT_TRANSFER
    assert kinds[("B", "D")] == HopKind.PEEL
    assert kinds[("B", "E")] == HopKind.DIRECT_TRANSFER
    assert out[("A", "B", "peel1:0->0")].confidence > 0.6


def test_peel_traversal_follows_change_branch():
    g = TxGraph.build(_peel_chain())
    r = traverse(g, "A")
    by_addr = {v.address: v for v in r.visited}
    assert set(by_addr) == {"A", "B", "C", "D", "E"}
    assert by_addr["D"].hop == 2 and not by_addr["D"].side_branch
    assert by_addr["C"].side_branch and by_addr["E"].side_branch
    assert by_addr["B"].via_kind == HopKind.PEEL.value


def test_no_peel_without_asymmetry():
    g = TxGraph.build([
        _tx("t1", Chain.BITCOIN, _BTC, [("A", 100_000)], [("B", 49_000), ("C", 49_000)]),
    ])
    out = classify_graph(g)
    assert all(c.kind == HopKind.DIRECT_TRANSFER for c in out.values())


def test_no_peel_when_change_address_reused():
    # B already seen elsewhere -> not fresh -> not peel-shaped
    g = TxGraph.build([
        _tx("t0", Chain.BITCOIN, _BTC, [("X", 50_000)], [("B", 49_000)]),
        _tx("t1", Chain.BITCOIN, _BTC, [("A", 100_000)], [("B", 90_000), ("C", 9_000)]),
    ])
    out = classify_graph(g)
    kinds = {(s, d): c.kind for (s, d, _), c in out.items() if s == "A"}
    assert kinds[("A", "B")] == HopKind.DIRECT_TRANSFER


def _tx_scripts(tx_hash, chain, asset, inputs, outputs):
    """inputs/outputs: list of (address, value_int, script_type_or_None)."""
    return CanonicalTx(
        tx_hash=tx_hash,
        chain=chain,
        block_time=_T0,
        inputs=[FlowParty(address=a, value=str(v), script_type=s)
                for a, v, s in inputs],
        outputs=[FlowParty(address=a, value=str(v), script_type=s)
                 for a, v, s in outputs],
        asset=asset,
    )


def test_peel_script_type_match_boosts_confidence():
    txs = [
        _tx_scripts("p1", Chain.BITCOIN, _BTC,
                    [("A", 100_000, "p2wpkh")],
                    [("B", 90_000, "p2wpkh"), ("C", 9_000, "p2pkh")]),
    ]
    g = TxGraph.build(txs)
    out = classify_graph(g)
    c = out[("A", "B", "p1:0->0")]
    assert c.kind == HopKind.PEEL
    assert c.details["script_match"] is True
    assert "script-type match" in c.reason

    # same shape, script types unknown -> lower confidence, still peel
    g2 = TxGraph.build([_tx("p1", Chain.BITCOIN, _BTC,
                            [("A", 100_000)], [("B", 90_000), ("C", 9_000)])])
    c2 = classify_graph(g2)[("A", "B", "p1:0->0")]
    assert c2.kind == HopKind.PEEL
    assert c2.details["script_match"] is False
    assert c.confidence > c2.confidence


def test_peel_script_type_mismatch_vetoes():
    # Peel-shaped, but the change candidate's script type differs from the
    # spent input's -> the peel hypothesis is rejected for both edges.
    g = TxGraph.build([
        _tx_scripts("p1", Chain.BITCOIN, _BTC,
                    [("A", 100_000, "p2wpkh")],
                    [("B", 90_000, "p2tr"), ("C", 9_000, "p2pkh")]),
    ])
    out = classify_graph(g)
    kinds = {(s, d): c.kind for (s, d, _), c in out.items()}
    assert kinds[("A", "B")] == HopKind.DIRECT_TRANSFER
    assert kinds[("A", "C")] == HopKind.DIRECT_TRANSFER
    c = out[("A", "B", "p1:0->0")]
    assert "script-type mismatch" in c.reason
    assert c.details["script_match"] is False
    assert not c.details.get("peel_payment", False)
    assert out[("A", "C", "p1:0->1")].details.get("peel_payment") is not True


def test_bitcoin_adapter_normalizes_script_types():
    from engine.adapters.bitcoin import BitcoinAdapter

    raw = {
        "txid": "abc123",
        "fee": 120,
        "status": {"block_height": 800000, "block_time": 1700000000},
        "vin": [{
            "is_coinbase": False,
            "prevout": {
                "scriptpubkey_address": "bc1qinput",
                "value": 100_000,
                "scriptpubkey_type": "v0_p2wpkh",
            },
        }],
        "vout": [
            {"scriptpubkey_address": "bc1qchange", "value": 90_000,
             "scriptpubkey_type": "v0_p2wpkh"},
            {"scriptpubkey_address": "1Payment", "value": 9_000,
             "scriptpubkey_type": "p2pkh"},
            {"scriptpubkey_address": "bc1ptr", "value": 880,
             "scriptpubkey_type": "v1_p2tr"},
            {"scriptpubkey": "6a046f706a656374", "value": 0,
             "scriptpubkey_type": "op_return"},
        ],
    }
    tx = BitcoinAdapter()._normalize(raw)
    assert tx is not None
    assert tx.inputs[0].script_type == "p2wpkh"
    by_addr = {p.address: p.script_type for p in tx.outputs}
    assert by_addr == {
        "bc1qchange": "p2wpkh",
        "1Payment": "p2pkh",
        "bc1ptr": "p2tr",
    }


# ------------------------------------------------------------------ sweep

def _sweep_tx():
    return _tx("sweep1", Chain.ETHEREUM, _ETH,
               [(f"dep{i}", 10_000) for i in range(6)],
               [("hotwallet", 59_000)])


def test_sweep_classification():
    g = TxGraph.build([_sweep_tx()])
    out = classify_graph(g)
    assert out
    assert all(c.kind == HopKind.SWEEP_CANDIDATE for c in out.values())
    assert all(c.confidence >= 0.8 for c in out.values())


def test_sweep_traversal_stops_and_back_labels():
    g = TxGraph.build([_sweep_tx()])
    r = traverse(g, "dep0")
    assert [t.address for t in r.terminals] == ["hotwallet"]
    assert r.terminals[0].reason == "sweep-consolidation"
    # consolidation wallet is NOT expanded further
    assert "hotwallet" not in {v.address for v in r.visited if v.hop > 1}
    # every deposit address back-labeled
    for i in range(6):
        assert f"dep{i}" in r.labels_applied
        assert "sweep-source" in r.labels_applied[f"dep{i}"]
        assert "sweep-source" in g.labels(f"dep{i}")


def test_no_sweep_below_input_threshold():
    g = TxGraph.build([
        _tx("t1", Chain.ETHEREUM, _ETH,
            [("a", 10), ("b", 10), ("c", 10)], [("d", 30)]),
    ])
    out = classify_graph(g)
    assert all(c.kind == HopKind.DIRECT_TRANSFER for c in out.values())


# ------------------------------------------------------------------ direct / limits

def test_direct_chain_traversal():
    g = TxGraph.build([
        _tx("t1", Chain.ETHEREUM, _ETH, [("A", 10)], [("B", 10)]),
        _tx("t2", Chain.ETHEREUM, _ETH, [("B", 10)], [("C", 10)]),
        _tx("t3", Chain.ETHEREUM, _ETH, [("C", 10)], [("D", 10)]),
    ])
    r = traverse(g, "A")
    assert [(v.address, v.hop) for v in r.visited] == [
        ("A", 0), ("B", 1), ("C", 2), ("D", 3)
    ]
    assert r.terminals and r.terminals[-1].reason == "dead-end"


def test_max_hops_terminal():
    g = TxGraph.build([
        _tx(f"t{i}", Chain.ETHEREUM, _ETH, [(f"n{i}", 10)], [(f"n{i+1}", 10)])
        for i in range(6)
    ])
    r = traverse(g, "n0", TraversalConfig(max_hops=2))
    assert {v.address for v in r.visited} == {"n0", "n1", "n2"}
    assert any(t.reason == "max-hops" for t in r.terminals)


def test_cycle_does_not_loop_forever():
    g = TxGraph.build([
        _tx("t1", Chain.ETHEREUM, _ETH, [("A", 10)], [("B", 10)]),
        _tx("t2", Chain.ETHEREUM, _ETH, [("B", 10)], [("A", 10)]),
    ])
    r = traverse(g, "A", TraversalConfig(max_hops=50))
    assert len(r.visited) == 2


def test_unknown_start_is_dead_end():
    g = TxGraph.build([
        _tx("t1", Chain.ETHEREUM, _ETH, [("A", 10)], [("B", 10)]),
    ])
    r = traverse(g, "ghost")
    assert r.terminals[0].reason == "dead-end"


def test_came_from_path_reconstruction():
    g = TxGraph.build(_peel_chain())
    r = traverse(g, "A")
    assert r.came_from["D"] == ("B", "peel2")
    assert r.came_from["B"] == ("A", "peel1")
