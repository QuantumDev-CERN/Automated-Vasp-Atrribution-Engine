"""M21 tests: bridge calldata destination parsing + destination-chain
continuation.

Wormhole TokenBridge transferTokens calldata is decoded into an
explicit (chain, address) destination; the pipeline then runs one
bounded follow-up trace on the destination chain. All offline.
"""
import pytest

from Crypto.Hash import keccak

from engine.adapters.base import (
    Asset, AssetKind, CanonicalTx, Chain, ChainAdapter, FlowParty,
)
from engine.adapters.evm import EvmAdapter
from engine.classifier.hops import HopKind, classify_graph
from engine.decoding.bridges import parse_bridge_destination
from engine.graph.builder import TxGraph
from engine.jobs import PipelineDeps, run_trace_pipeline
from engine.vasp import CaseDetails

_ETH = Asset(kind=AssetKind.NATIVE, chain=Chain.ETHEREUM, symbol="ETH")
_BNB = Asset(kind=AssetKind.NATIVE, chain=Chain.BSC, symbol="BNB")

WORMHOLE_ETH = "0x3ee18B2214AFF97000D974cf647E7C347E8fa585"
STARGATE_ETH = "0x8731d54E9D02c286767d56ac03e8037C07e01e98"


def _selector() -> bytes:
    k = keccak.new(digest_bits=256)
    k.update(b"transferTokens(address,uint256,uint16,bytes32,uint256,uint32)")
    return k.digest()[:4]


def _wormhole_calldata(recipient_chain: int, recipient: bytes) -> str:
    def word(b: bytes) -> bytes:
        return b.rjust(32, b"\x00")
    token = word(bytes.fromhex("c02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"))
    amount = word((10**18).to_bytes(32, "big")[-32:])
    chain_w = word(recipient_chain.to_bytes(2, "big"))
    arbiter = word(b"\x00")
    nonce = word((1).to_bytes(4, "big"))
    return "0x" + (_selector() + token + amount + chain_w + recipient
                   + arbiter + nonce).hex()


def _evm_recipient(addr: str) -> bytes:
    return b"\x00" * 12 + bytes.fromhex(addr[2:])


def _lock_tx(tx_hash, sender, calldata):
    return CanonicalTx(
        tx_hash=tx_hash, chain=Chain.ETHEREUM, asset=_ETH,
        inputs=[FlowParty(address=sender, value="0")],
        outputs=[FlowParty(address=WORMHOLE_ETH, value="0")],
        raw={"input": calldata})


def _kinds_of(graph, tx_hash):
    out = classify_graph(graph)
    return {c.kind for (s, d, k), c in out.items()
            if k.startswith(tx_hash + ":")}


def _details_of(graph, tx_hash):
    out = classify_graph(graph)
    for (s, d, k), c in out.items():
        if k.startswith(tx_hash + ":"):
            return c.details
    raise AssertionError("no classification")


# ---------- decoder ----------

def test_wormhole_calldata_decodes_bsc_destination():
    dest = "0x" + "ab" * 20
    tx = _lock_tx("lock1", "0xDepositor", _wormhole_calldata(4, _evm_recipient(dest)))
    d = parse_bridge_destination(tx)
    assert d is not None
    assert d.bridge == "wormhole"
    assert d.wormhole_chain_id == 4
    assert d.dest_chain == "bsc"
    assert d.dest_address == dest.lower()


def test_wormhole_unknown_chain_id_parsed_but_unmapped():
    dest = "0x" + "cd" * 20
    tx = _lock_tx("lock2", "0xDepositor",
                  _wormhole_calldata(9999, _evm_recipient(dest)))
    d = parse_bridge_destination(tx)
    assert d is not None
    assert d.wormhole_chain_id == 9999
    assert d.dest_chain is None
    assert d.dest_address == dest.lower()
    assert "unknown wormhole chain id" in d.note


def test_wormhole_solana_recipient_is_32byte_pubkey():
    pubkey = bytes(range(32))
    tx = _lock_tx("lock3", "0xDepositor", _wormhole_calldata(1, pubkey))
    d = parse_bridge_destination(tx)
    assert d is not None
    assert d.dest_chain == "solana"
    assert d.dest_address == pubkey.hex()


def test_wrong_selector_returns_none():
    tx = _lock_tx("lock4", "0xDepositor", "0xdeadbeef" + "00" * 192)
    assert parse_bridge_destination(tx) is None


def test_short_calldata_returns_none():
    tx = _lock_tx("lock5", "0xDepositor", "0x1234")
    assert parse_bridge_destination(tx) is None


def test_non_bridge_counterparty_returns_none():
    dest = "0x" + "ab" * 20
    tx = CanonicalTx(
        tx_hash="lock6", chain=Chain.ETHEREUM, asset=_ETH,
        inputs=[FlowParty(address="0xDepositor", value="0")],
        outputs=[FlowParty(address="0xSomeRandomContract", value="0")],
        raw={"input": _wormhole_calldata(4, _evm_recipient(dest))})
    assert parse_bridge_destination(tx) is None


# ---------- classifier ----------

def test_classifier_bridge_lock_carries_destination():
    dest = "0x" + "ab" * 20
    g = TxGraph.build([_lock_tx(
        "lock7", "0xDepositor", _wormhole_calldata(4, _evm_recipient(dest)))])
    assert _kinds_of(g, "lock7") == {HopKind.BRIDGE_LOCK}
    det = _details_of(g, "lock7")
    assert det["dest_chain"] == "bsc"
    assert det["dest_address"] == dest.lower()


def test_stargate_lock_has_no_parsed_destination():
    # Deliberate scope boundary (M21): Stargate's swap() is not hand-decoded.
    # The lock still classifies, but stays correlation-only.
    tx = CanonicalTx(
        tx_hash="sg1", chain=Chain.ETHEREUM, asset=_ETH,
        inputs=[FlowParty(address="0xDepositor", value="0")],
        outputs=[FlowParty(address=STARGATE_ETH, value="0")],
        raw={"input": "0x" + "ee" * 200})
    g = TxGraph.build([tx])
    assert _kinds_of(g, "sg1") == {HopKind.BRIDGE_LOCK}
    assert _details_of(g, "sg1").get("dest_address") is None


# ---------- adapter calldata retention ----------

def test_adapter_keeps_full_calldata_for_bridge_txs():
    adapter = EvmAdapter(Chain.ETHEREUM)
    calldata = _wormhole_calldata(4, _evm_recipient("0x" + "ab" * 20))
    raw = {"hash": "h", "from": "0xDepositor", "to": WORMHOLE_ETH,
           "value": "0", "input": calldata,
           "blockNumber": "1", "timeStamp": "1",
           "gasUsed": "1", "gasPrice": "1"}
    assert adapter._normalize_native(raw).raw["input"] == calldata


def test_adapter_truncates_calldata_for_non_bridge_txs():
    adapter = EvmAdapter(Chain.ETHEREUM)
    calldata = _wormhole_calldata(4, _evm_recipient("0x" + "ab" * 20))
    raw = {"hash": "h", "from": "0xDepositor", "to": "0xSomewhereElse",
           "value": "0", "input": calldata,
           "blockNumber": "1", "timeStamp": "1",
           "gasUsed": "1", "gasPrice": "1"}
    assert adapter._normalize_native(raw).raw["input"] == calldata[:10]


# ---------- pipeline continuation ----------

class StubAdapter(ChainAdapter):
    def __init__(self, chain, txs):
        super().__init__()
        self.chain = chain
        self._txs = txs

    async def get_transactions(self, address, limit=100):
        return [t for t in self._txs
                if any(p.address == address for p in t.inputs + t.outputs)]

    async def get_token_transfers(self, address, limit=100):
        return []

    async def health_check(self):
        return True


def _case(cid="FIR/2026/001234"):
    return CaseDetails(
        case_id=cid, agency="Cyber Cell", officer="Insp. X",
        wallets=("0xDepositor",), tx_hashes=("lock1",),
        date_from="2026-01-01", date_to="2026-09-28",
        suspected_offence="test")


def _bsc_tx(tx_hash, sender, receiver):
    return CanonicalTx(
        tx_hash=tx_hash, chain=Chain.BSC, asset=_BNB,
        inputs=[FlowParty(address=sender, value="100")],
        outputs=[FlowParty(address=receiver, value="100")])


@pytest.mark.asyncio
async def test_pipeline_continues_on_destination_chain():
    dest = "0x" + "ab" * 20
    eth_txs = [_lock_tx("lock1", "0xDepositor",
                        _wormhole_calldata(4, _evm_recipient(dest)))]
    bsc_txs = [_bsc_tx("b1", dest.lower(), "0xFinal")]
    stubs = {"ethereum": StubAdapter(Chain.ETHEREUM, eth_txs),
             "bsc": StubAdapter(Chain.BSC, bsc_txs)}
    deps = PipelineDeps(adapter_factory=lambda c: stubs[c])
    result = await run_trace_pipeline("0xDepositor", "ethereum", _case(), deps)

    assert result.terminal_reason == "bridge-lock"
    assert len(result.cross_chain) == 1
    cont = result.cross_chain[0]
    assert cont.dest_chain == "bsc"
    assert cont.dest_address == dest.lower()
    assert cont.terminal_reason == "dead-end"
    assert cont.terminal_address == "0xFinal"
    assert "10. CROSS-CHAIN CONTINUATION" in result.report.text
    assert "bsc:0xababababab" in result.report.text
    assert any("explicitly parsed destination" in n
               for n in result.attribution.notes)


@pytest.mark.asyncio
async def test_continuation_does_not_recurse_back():
    # BSC side locks straight back to Ethereum: depth guard must stop the
    # ping-pong after exactly one continuation.
    dest = "0x" + "ab" * 20
    eth_txs = [_lock_tx("lock1", "0xDepositor",
                        _wormhole_calldata(4, _evm_recipient(dest)))]
    back = CanonicalTx(
        tx_hash="b2", chain=Chain.BSC, asset=_BNB,
        inputs=[FlowParty(address=dest.lower(), value="0")],
        outputs=[FlowParty(address="0xB6F6D86a8f9879A9c87f643768d9efc38c1Da6E7",
                           value="0")],
        raw={"input": _wormhole_calldata(2, _evm_recipient("0x" + "de" * 20))})
    stubs = {"ethereum": StubAdapter(Chain.ETHEREUM, eth_txs),
             "bsc": StubAdapter(Chain.BSC, [back])}
    deps = PipelineDeps(adapter_factory=lambda c: stubs[c])
    result = await run_trace_pipeline("0xDepositor", "ethereum", _case(), deps)
    assert len(result.cross_chain) == 1
    assert result.cross_chain[0].terminal_reason == "bridge-lock"


@pytest.mark.asyncio
async def test_continuation_failure_does_not_fail_primary():
    dest = "0x" + "ab" * 20
    eth_txs = [_lock_tx("lock1", "0xDepositor",
                        _wormhole_calldata(4, _evm_recipient(dest)))]

    def factory(c):
        if c == "bsc":
            raise RuntimeError("indexer down")
        return StubAdapter(Chain.ETHEREUM, eth_txs)

    deps = PipelineDeps(adapter_factory=factory)
    result = await run_trace_pipeline("0xDepositor", "ethereum", _case(), deps)
    assert result.terminal_reason == "bridge-lock"
    assert result.cross_chain == ()


@pytest.mark.asyncio
async def test_no_continuation_without_parsed_destination():
    tx = CanonicalTx(
        tx_hash="sg2", chain=Chain.ETHEREUM, asset=_ETH,
        inputs=[FlowParty(address="0xDepositor", value="0")],
        outputs=[FlowParty(address=STARGATE_ETH, value="0")],
        raw={"input": "0x" + "ee" * 200})
    stubs = {"ethereum": StubAdapter(Chain.ETHEREUM, [tx])}
    deps = PipelineDeps(adapter_factory=lambda c: stubs[c])
    result = await run_trace_pipeline("0xDepositor", "ethereum", _case(), deps)
    assert result.terminal_reason == "bridge-lock"
    assert result.cross_chain == ()
    assert "10. CROSS-CHAIN CONTINUATION\n  (none)" in result.report.text
