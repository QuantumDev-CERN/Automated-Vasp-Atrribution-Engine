"""M16: CREATE2 deposit-proxy detection.

- keccak256 self-validation (empty-string + Transfer-topic vectors)
- EIP-1014 CREATE2 address derivation
- EIP-1167 minimal-proxy bytecode recognition
- known CREATE2 factory registry
- annotate_deposit_proxies enrichment
- sweep back-labeling tags proxy inputs as deposit-proxy
"""
from engine.adapters.base import Asset, AssetKind, CanonicalTx, Chain, FlowParty
from engine.classifier.hops import HopKind, classify_graph
from engine.decoding.proxies import annotate_deposit_proxies
from engine.graph.builder import TxGraph
from engine.knowledge.proxies import (
    CREATE2_FACTORIES,
    EIP1167_RUNTIME_LEN,
    create2_address,
    create2_factory_name,
    detect_deposit_proxy,
    eip1167_implementation,
    keccak256,
)
from engine.traversal.engine import traverse

IMPL = "0x" + "be" * 20  # 20-byte implementation address
EIP1167_CODE = (
    "0x363d3d373d3d3d363d73" + IMPL[2:] + "5af43d82803e903d91602b57fd5bf3"
)
assert len(bytes.fromhex(EIP1167_CODE[2:])) == 45  # fail fast, not in a test
DEPLOYER = "0x4e59b44847b379578588920ca78fbf26c0b4956c"
USDT = "0xdac17f958d2ee523a2206206994597c13d831ec7"
EOA = "0x0000000000000000000000000000000000000000"


def _p(addr, value=1_000):
    return FlowParty(address=addr, value=str(value))


def _tx(hash_, inputs, outputs):
    return CanonicalTx(
        tx_hash=hash_,
        chain=Chain.ETHEREUM,
        asset=Asset(kind=AssetKind.NATIVE, chain=Chain.ETHEREUM,
                   symbol="ETH", decimals=18),
        inputs=[_p(a, v) for a, v in inputs],
        outputs=[_p(a, v) for a, v in outputs],
    )


# ------------------------------------------------------------- keccak256

def test_keccak256_empty_string_vector():
    assert keccak256(b"").hex() == (
        "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7b"
        "fad8045d85a470"
    )


def test_keccak256_transfer_topic_vector():
    # cross-validates the hardcoded topic in engine/knowledge/dex.py
    assert keccak256(b"Transfer(address,address,uint256)").hex() == (
        "ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
    )


# ------------------------------------------------------- CREATE2 address

def test_create2_address_is_deterministic_and_well_formed():
    salt = b"\x01" * 32
    init = b"\x60\x60"
    a1 = create2_address(DEPLOYER, salt, init)
    a2 = create2_address(DEPLOYER, salt, init)
    assert a1 == a2
    assert a1.startswith("0x") and len(a1) == 42
    bytes.fromhex(a1[2:])  # valid hex


def test_create2_address_sensitive_to_salt_and_init_code():
    a = create2_address(DEPLOYER, b"\x01" * 32, b"\x60\x60")
    assert create2_address(DEPLOYER, b"\x02" * 32, b"\x60\x60") != a
    assert create2_address(DEPLOYER, b"\x01" * 32, b"\x60\x61") != a


def test_create2_address_rejects_bad_salt():
    try:
        create2_address(DEPLOYER, b"\x01" * 31, b"\x60")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for 31-byte salt")


# ------------------------------------------------------------- EIP-1167

def test_eip1167_template_length():
    assert len(bytes.fromhex(EIP1167_CODE[2:])) == EIP1167_RUNTIME_LEN


def test_eip1167_positive_extracts_implementation():
    assert eip1167_implementation(EIP1167_CODE) == IMPL


def test_eip1167_rejects_non_proxies():
    assert eip1167_implementation("0x") is None  # EOA
    assert eip1167_implementation("0x" + "60" * 200) is None  # long code
    assert eip1167_implementation(EIP1167_CODE[:-2]) is None  # truncated
    bad_suffix = EIP1167_CODE[:-4] + "0000"
    assert eip1167_implementation(bad_suffix) is None
    assert eip1167_implementation("0xZZ") is None  # non-hex


# -------------------------------------------------------------- registry

def test_create2_factory_registry_valid():
    assert len(CREATE2_FACTORIES) >= 1
    for addr, name in CREATE2_FACTORIES.items():
        assert addr.startswith("0x") and len(addr) == 42
        bytes.fromhex(addr[2:])
        assert name  # every entry has a name


def test_create2_factory_lookup_case_insensitive():
    assert create2_factory_name(DEPLOYER) == "arachnid-deterministic-deployment-proxy"
    assert create2_factory_name(DEPLOYER.upper()) == "arachnid-deterministic-deployment-proxy"
    assert create2_factory_name(USDT) is None


# ------------------------------------------------------- combined detect

def test_detect_proxy_code_only():
    info = detect_deposit_proxy(EIP1167_CODE)
    assert info is not None and info.kind == "eip1167-minimal-proxy"
    assert info.implementation == IMPL


def test_detect_proxy_factory_only():
    info = detect_deposit_proxy("0x" + "60" * 100, deployer=DEPLOYER)
    assert info is not None and info.kind == "create2-deployed"
    assert info.factory == "arachnid-deterministic-deployment-proxy"


def test_detect_proxy_both_signals():
    info = detect_deposit_proxy(EIP1167_CODE, deployer=DEPLOYER)
    assert info is not None and info.kind == "eip1167-via-create2"


def test_detect_proxy_neither():
    assert detect_deposit_proxy("0x") is None
    assert detect_deposit_proxy("0x" + "60" * 100, deployer=USDT) is None


# -------------------------------------------------------------- annotate

def test_annotate_marks_proxy_outputs_and_caches():
    calls = []

    def get_code(addr):
        calls.append(addr)
        return EIP1167_CODE if addr == "0xproxy1" else "0x"

    txs = [
        _tx("t1", [("0xuser", 1_000)], [("0xproxy1", 1_000)]),
        _tx("t2", [("0xuser", 500)], [("0xproxy1", 500), ("0xeoa2", 500)]),
    ]
    n = annotate_deposit_proxies(Chain.ETHEREUM, txs, get_code)
    assert n == 2  # two party occurrences (one per tx); code fetched once
    assert txs[0].outputs[0].proxy_kind == "eip1167-minimal-proxy"
    assert txs[1].outputs[0].proxy_kind == "eip1167-minimal-proxy"
    assert txs[1].outputs[1].proxy_kind is None
    assert sorted(set(calls)) == ["0xeoa2", "0xproxy1"]  # one probe per address


def test_annotate_noop_on_non_evm_chain():
    def get_code(addr):  # pragma: no cover - must not be called
        raise AssertionError("must not probe on bitcoin")

    txs = [_tx("t1", [("a", 1)], [("b", 1)])]
    assert annotate_deposit_proxies(Chain.BITCOIN, txs, get_code) == 0


def test_annotate_respects_preexisting_kind():
    tx = _tx("t1", [("0xuser", 1_000)], [("0xproxy1", 1_000)])
    tx.outputs[0].proxy_kind = "create2-deployed"

    def get_code(addr):  # pragma: no cover - must not be called
        raise AssertionError("already annotated")

    assert annotate_deposit_proxies(Chain.ETHEREUM, [tx], get_code) == 0


# ------------------------------------------------- sweep back-labeling

def _sweep_with_proxy():
    inputs = [(f"dep{i}", 10_000) for i in range(5)] + [("0xproxy1", 10_000)]
    return _tx("sweep1", inputs, [("hotwallet", 59_000)])


def test_sweep_back_label_tags_proxy_inputs():
    g = TxGraph.build([_sweep_with_proxy()])
    out = classify_graph(g)
    assert all(c.kind == HopKind.SWEEP_CANDIDATE for c in out.values())

    # annotate the proxy input before traversal (as the decoder would)
    tx = g.tx("sweep1")
    for party in tx.inputs:
        if party.address == "0xproxy1":
            party.proxy_kind = "eip1167-minimal-proxy"

    r = traverse(g, "dep0")
    assert r.terminals[0].reason == "sweep-consolidation"
    assert "sweep-source" in r.labels_applied["0xproxy1"]
    assert "deposit-proxy" in r.labels_applied["0xproxy1"]
    assert "deposit-proxy" in g.labels("0xproxy1")
    # ordinary deposit addresses keep only the sweep-source label
    assert "deposit-proxy" not in r.labels_applied["dep0"]


def test_flowparty_proxy_kind_defaults_none():
    assert FlowParty(address="0xabc", value="1").proxy_kind is None
