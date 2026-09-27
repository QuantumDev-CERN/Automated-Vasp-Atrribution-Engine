"""M17 tests: common-input clustering with CoinJoin guards.

All fixtures are synthetic CanonicalTx objects — no network. Live wiring is
covered by scripts/smoke_m17.py.
"""
from datetime import datetime, timezone

from engine.adapters.base import (
    Asset,
    AssetKind,
    CanonicalTx,
    Chain,
    FlowParty,
)
from engine.clustering import (
    cluster_coinput_transactions,
    detect_coinjoin,
)
from engine.graph import TxGraph

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


def _coinjoin_tx(tx_hash="cj1", n_in=8, n_eq_out=12, denom=10_000_000):
    """Wasabi-shaped synthetic CoinJoin: many inputs, many equal outputs."""
    inputs = [(f"cj_in_{i}", denom + 1000) for i in range(n_in)]
    outputs = [(f"cj_out_{i}", denom) for i in range(n_eq_out)]
    outputs += [("coordinator_fee", 5000), ("change_dust", 1234)]
    return _tx(tx_hash, Chain.BITCOIN, _BTC, inputs, outputs)


# ------------------------------------------------------- CoinJoin detection


def test_detect_coinjoin_fires_on_equal_output_anonymity_set():
    ev = detect_coinjoin(_coinjoin_tx())
    assert ev.is_coinjoin
    assert ev.max_equal_outputs == 12
    assert any("anonymity set" in r for r in ev.reasons)


def test_detect_coinjoin_fires_on_fan_in_fan_out():
    tx = _tx(
        "cj2", Chain.BITCOIN, _BTC,
        [(f"in_{i}", 50_000) for i in range(10)],
        # 10 outputs, only 2 distinct values -> coordinated construction
        [(f"out_{i}", 40_000) for i in range(6)]
        + [(f"outx_{i}", 30_000) for i in range(4)],
    )
    ev = detect_coinjoin(tx)
    assert ev.is_coinjoin
    assert any("fan-in/fan-out" in r for r in ev.reasons)


def test_detect_coinjoin_ignores_ordinary_payment():
    tx = _tx("pay1", Chain.BITCOIN, _BTC,
             [("alice", 100_000)],
             [("merchant", 90_000), ("alice_change", 9_000)])
    ev = detect_coinjoin(tx)
    assert not ev.is_coinjoin
    assert ev.reasons == ()


def test_detect_coinjoin_ignores_small_equal_outputs():
    # 3 equal outputs is below the default threshold of 5 -> not flagged.
    tx = _tx("pay2", Chain.BITCOIN, _BTC,
             [("alice", 100_000), ("bob", 100_000)],
             [("x", 50_000), ("y", 50_000), ("z", 50_000), ("w", 40_000)])
    ev = detect_coinjoin(tx)
    assert not ev.is_coinjoin


def test_detect_coinjoin_thresholds_are_tunable():
    tx = _tx("pay3", Chain.BITCOIN, _BTC,
             [("a", 100_000), ("b", 100_000), ("c", 100_000)],
             [("x", 50_000), ("y", 50_000), ("z", 50_000), ("w", 40_000)])
    assert not detect_coinjoin(tx).is_coinjoin
    ev = detect_coinjoin(tx, min_equal_outputs=3)
    assert ev.is_coinjoin


# ------------------------------------------------------- co-input clustering


def test_cluster_merges_inputs_of_one_tx():
    r = cluster_coinput_transactions([
        _tx("t1", Chain.BITCOIN, _BTC,
            [("a", 60), ("b", 40)], [("c", 90)]),
    ])
    assert r.clusters == {"a": "a", "b": "a"}
    assert r.members == {"a": frozenset({"a", "b"})}
    assert r.txs_merged == 1
    assert r.excluded == []


def test_cluster_merges_transitively_across_txs():
    r = cluster_coinput_transactions([
        _tx("t1", Chain.BITCOIN, _BTC, [("a", 60), ("b", 40)], [("c", 90)]),
        _tx("t2", Chain.BITCOIN, _BTC, [("b", 90), ("d", 10)], [("e", 95)]),
    ])
    assert set(r.members) == {"a"}
    assert r.members["a"] == frozenset({"a", "b", "d"})
    assert r.cluster_of("d") == "a"
    assert r.cluster_size("d") == 3


def test_cluster_skips_single_input_and_account_model_txs():
    r = cluster_coinput_transactions([
        _tx("t1", Chain.BITCOIN, _BTC, [("a", 100)], [("b", 90)]),
        _tx("t2", Chain.ETHEREUM, _ETH, [("x", 10)], [("y", 10)]),
    ])
    assert r.clusters == {}
    assert r.members == {}
    assert r.txs_merged == 0
    assert r.cluster_of("a") is None
    assert r.cluster_size("a") == 1


def test_cluster_ignores_coinbase_pseudo_input():
    r = cluster_coinput_transactions([
        _tx("t1", Chain.BITCOIN, _BTC,
            [("coinbase", 0), ("miner", 100)], [("a", 60), ("b", 40)]),
    ])
    # only one real input -> nothing to merge, and "coinbase" is not clustered
    assert r.clusters == {}
    assert "coinbase" not in r.clusters


def test_coinjoin_guard_blocks_merge_and_records_evidence():
    cj = _coinjoin_tx("cj1")
    r = cluster_coinput_transactions([
        _tx("t1", Chain.BITCOIN, _BTC, [("a", 60), ("b", 40)], [("c", 90)]),
        cj,
    ])
    # the ordinary tx still merges...
    assert r.clusters == {"a": "a", "b": "a"}
    # ...but none of the CoinJoin's inputs join any cluster
    for p in cj.inputs:
        assert p.address not in r.clusters
    assert len(r.excluded) == 1
    assert r.excluded[0].tx_hash == "cj1"
    assert r.excluded[0].is_coinjoin
    assert r.txs_seen == 2
    assert r.txs_merged == 1


def test_coinjoin_inputs_do_not_bridge_into_honest_cluster():
    # Adversarial: CoinJoin reuses "b", which is honestly clustered with "a".
    # The guard must prevent X/Y from being pulled into a's cluster via b.
    r = cluster_coinput_transactions([
        _tx("t1", Chain.BITCOIN, _BTC, [("a", 60), ("b", 40)], [("c", 90)]),
        _tx("cj9", Chain.BITCOIN, _BTC,
            [("b", 10_001_000), ("x", 10_001_000), ("y", 10_001_000)],
            [(f"o{i}", 10_000_000) for i in range(6)]),
    ])
    assert r.members == {"a": frozenset({"a", "b"})}
    assert "x" not in r.clusters and "y" not in r.clusters


def test_cluster_ids_are_deterministic():
    txs = [
        _tx("t2", Chain.BITCOIN, _BTC, [("b", 90), ("d", 10)], [("e", 95)]),
        _tx("t1", Chain.BITCOIN, _BTC, [("a", 60), ("b", 40)], [("c", 90)]),
    ]
    r1 = cluster_coinput_transactions(txs)
    r2 = cluster_coinput_transactions(list(reversed(txs)))
    assert r1.clusters == r2.clusters
    assert r1.members.keys() == r2.members.keys()


# ------------------------------------------------------- graph annotation seam


def test_graph_annotate_clusters():
    g = TxGraph.build([
        _tx("t1", Chain.BITCOIN, _BTC, [("a", 60), ("b", 40)], [("c", 90)]),
        _tx("t2", Chain.BITCOIN, _BTC, [("c", 90)], [("d", 80)]),
    ])
    result = cluster_coinput_transactions(list(g.txs.values()))
    n = g.annotate_clusters(result)
    assert n == 2
    assert g.cluster_id("a") == "a"
    assert g.cluster_id("b") == "a"
    assert g.cluster_id("c") is None  # single-input tx: unclustered
    assert g.cluster_id("ghost") is None
