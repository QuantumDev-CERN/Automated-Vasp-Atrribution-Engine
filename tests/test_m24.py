"""M24 tests: scam/ransomware feed ingestion.

Parsers are tested against the REAL feed shapes (verified live
2026-09-28). The fixture holds real extracted entries with provenance.
"""
import pytest

from engine.adapters.base import (
    Asset, AssetKind, CanonicalTx, Chain, ChainAdapter, FlowParty,
)
from engine.intel.threat_feeds import (
    RANSOMWHERE_URL, SCAMSNIFTER_URL, ThreatFeedList, ThreatRecord,
    from_fixture, merge, parse_ransomwhere, parse_scamsniffer,
)
from engine.jobs import PipelineDeps, run_trace_pipeline
from engine.scoring.risk import score_risk
from engine.vasp import CaseDetails

_SS_SAMPLE = {"address": [
    "0x7538fd1e30d8e7771105d470fe8d65b6ab0da93f",
    "0xE6B39Db1DC73F0e591EaEdDDA1F8798DABf836D0",  # checksum case
    "null",                                          # malformed
    "0x7a3799013d8b4fc06cc51bdbf6ec3116496086cfm",  # malformed (41 hex)
    "00x401b88454094731e7d10a8ebace5f3ed07ab3ebf",  # malformed
]}

_RW_SAMPLE = {"result": [
    {"address": "17TMc2UkVRSga2yYvuxSD9Q1XyB2EPRjTF",
     "blockchain": "bitcoin", "family": "Netwalker (Mailto)"},
    {"address": "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
     "blockchain": "bitcoin", "family": None},
    {"address": "not-an-address", "blockchain": "bitcoin",
     "family": "Locky"},
    {"address": "17TMc2UkVRSga2yYvuxSD9Q1XyB2EPRjTF",
     "blockchain": "ethereum", "family": "Locky"},  # wrong chain
]}


def test_parse_scamsniffer_sample():
    records, skipped = parse_scamsniffer(_SS_SAMPLE)
    assert len(records) == 2
    assert skipped == 3
    assert records[0].address == \
        "0x7538fd1e30d8e7771105d470fe8d65b6ab0da93f"
    assert records[1].address == records[1].address.lower()
    assert all(r.chain == "evm" and r.category == "scam"
               and r.source == "scamsniffer" for r in records)
    assert all(r.reference == SCAMSNIFTER_URL for r in records)


def test_parse_scamsniffer_rejects_wrong_shape():
    # The frozen blacklist/address.json trap: an unexpected shape must
    # fail loudly, never ingest as an empty (or wrong) list.
    with pytest.raises(ValueError):
        parse_scamsniffer({"addresses": ["0x1234"]})
    with pytest.raises(ValueError):
        parse_scamsniffer(["0x1234"])


def test_parse_ransomwhere_sample():
    records, skipped = parse_ransomwhere(_RW_SAMPLE)
    assert len(records) == 2
    assert skipped == 2
    assert records[0].label == "Netwalker (Mailto)"
    assert records[1].label == "unlabeled"  # null family
    assert all(r.chain == "bitcoin" and r.category == "ransomware"
               and r.source == "ransomwhere" for r in records)
    assert all(r.reference == RANSOMWHERE_URL for r in records)


def test_parse_ransomwhere_rejects_wrong_shape():
    with pytest.raises(ValueError):
        parse_ransomwhere({"results": []})
    with pytest.raises(ValueError):
        parse_ransomwhere([])


def test_from_fixture_has_real_entries_with_provenance():
    feeds = from_fixture()
    assert len(feeds.records) == 24  # 12 scam + 12 ransomware
    assert feeds.lookup(feeds.records[0].address)
    assert "scamsniffer" in feeds.records[0].reference
    assert "ransomwhe.re" in feeds.records[12].reference


def test_lookup_case_insensitive_evm():
    feeds = ThreatFeedList(records=(ThreatRecord(
        address="0xabc0000000000000000000000000000000000001",
        chain="evm", category="scam", source="scamsniffer", label="",
        reference=SCAMSNIFTER_URL),))
    assert feeds.lookup("0xABC0000000000000000000000000000000000001")
    assert feeds.lookup("0xabc0000000000000000000000000000000000001")
    assert feeds.lookup("0xdead000000000000000000000000000000000001") == ()


def test_lookup_btc_exact():
    addr = "17TMc2UkVRSga2yYvuxSD9Q1XyB2EPRjTF"
    feeds = ThreatFeedList(records=(ThreatRecord(
        address=addr, chain="bitcoin", category="ransomware",
        source="ransomwhere", label="Locky", reference=RANSOMWHERE_URL),))
    assert feeds.lookup(addr)
    assert feeds.lookup("1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa") == ()


def test_merge_combines_lists():
    a = ThreatFeedList(records=(ThreatRecord(
        address="0xabc0000000000000000000000000000000000001",
        chain="evm", category="scam", source="scamsniffer", label="",
        reference=SCAMSNIFTER_URL),), skipped=3)
    b = ThreatFeedList(records=(ThreatRecord(
        address="17TMc2UkVRSga2yYvuxSD9Q1XyB2EPRjTF", chain="bitcoin",
        category="ransomware", source="ransomwhere", label="Locky",
        reference=RANSOMWHERE_URL),), skipped=1)
    merged = merge(a, b)
    assert len(merged.records) == 2
    assert merged.skipped == 4


def test_score_risk_threat_signals():
    scam = ThreatRecord(
        address="0xabc0000000000000000000000000000000000001",
        chain="evm", category="scam", source="scamsniffer", label="",
        reference=SCAMSNIFTER_URL)
    rw1 = ThreatRecord(
        address="17TMc2UkVRSga2yYvuxSD9Q1XyB2EPRjTF", chain="bitcoin",
        category="ransomware", source="ransomwhere", label="Locky",
        reference=RANSOMWHERE_URL)
    rw2 = ThreatRecord(
        address="17TMc2UkVRSga2yYvuxSD9Q1XyB2EPRjTF", chain="bitcoin",
        category="ransomware", source="ransomwhere", label="Conti",
        reference=RANSOMWHERE_URL)
    hits = (("0xabc0000000000000000000000000000000000001", scam),
            ("17TMc2UkVRSga2yYvuxSD9Q1XyB2EPRjTF", rw1),
            ("17TMc2UkVRSga2yYvuxSD9Q1XyB2EPRjTF", rw2))
    risk = score_risk([], threat_hits=hits)
    names = [s.name for s in risk.signals]
    # One signal per address even with two ransomware records.
    assert names.count("ransomware-direct-hit") == 1
    assert names.count("scam-direct-hit") == 1
    by_name = {s.name: s for s in risk.signals}
    assert by_name["ransomware-direct-hit"].points == 40
    assert by_name["scam-direct-hit"].points == 25
    assert "Locky" in by_name["ransomware-direct-hit"].reason
    assert "Conti" in by_name["ransomware-direct-hit"].reason
    assert risk.total == 65


# ---------- pipeline ----------

_ETH = Asset(kind=AssetKind.NATIVE, chain=Chain.ETHEREUM, symbol="ETH")


class StubAdapter(ChainAdapter):
    def __init__(self, by_address):
        super().__init__()
        self.chain = Chain.ETHEREUM
        self._by_address = by_address

    async def get_transactions(self, address, limit=100):
        return [t for t in self._by_address.get(address, [])
                if any(p.address == address for p in t.inputs + t.outputs)]

    async def get_token_transfers(self, address, limit=100):
        return []

    async def health_check(self):
        return True


@pytest.mark.asyncio
async def test_pipeline_feed_hit_becomes_risk_signal():
    feeds = from_fixture()
    listed = feeds.records[0].address  # real ScamSniffer entry
    tx = CanonicalTx(
        tx_hash="s1", chain=Chain.ETHEREUM, asset=_ETH, block_time=None,
        inputs=[FlowParty(address="0xSubject",
                          value="1000000000000000000")],
        outputs=[FlowParty(address=listed,
                           value="1000000000000000000")])
    adapter = StubAdapter({"0xSubject": [tx], listed: [tx]})
    deps = PipelineDeps(adapter_factory=lambda c: adapter,
                        threat_feeds=feeds)
    case = CaseDetails(
        case_id="FIR/2026/001234", agency="Cyber Cell", officer="Insp. X",
        wallets=("0xSubject",), tx_hashes=("s1",),
        date_from="2026-01-01", date_to="2026-09-28",
        suspected_offence="test")
    result = await run_trace_pipeline("0xSubject", "ethereum", case, deps)

    names = [s.name for s in result.risk.signals]
    assert "scam-direct-hit" in names
    assert "scam-direct-hit (+25)" in result.report.text
    assert len(result.threat_hits) == 1
    assert result.threat_hits[0][0] == listed
