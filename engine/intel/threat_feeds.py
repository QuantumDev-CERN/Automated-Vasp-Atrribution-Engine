"""Scam + ransomware feed ingestion (M24).

Two live sources, both fetched and shape-verified on 2026-09-28:

1. ScamSniffer scam-database — community scam/phishing/drainer blacklist
   Raw feed:
   https://raw.githubusercontent.com/scamsniffer/scam-database/main/blacklist/all.json
   Shape: {"address": [...], "domains": [...], "combined": [...]}
   IMPORTANT: blacklist/address.json is FROZEN since 2024-02-28 — never
   use it; all.json is the live file (pushed daily). 4,607 entries were
   observed on 2026-09-28, of which 9 were malformed and are skipped by
   the parser (counted, not silently dropped).

2. Ransomwhere — crowdsourced ransomware-payment dataset (Jack Cable)
   API: https://api.ransomwhe.re/export
   Shape: {"result": [{"address", "blockchain": "bitcoin", "family",
                       "transactions": [...], ...}]}
   11,186 BTC records observed on 2026-09-28. Crowdsourced: per the
   publisher's own FAQ, accuracy is not independently verified — hits
   are investigative leads, and the reason strings say so.

Two ingestion paths (mirroring M7 sanctions):
- from_fixture(): vendored sample of REAL entries with provenance
  (engine/intel/fixtures/threat_feeds_sample.json).
- parse_scamsniffer_file / parse_ransomwhere_file: full ingestion from
  a downloaded copy of the feed (see scripts/refresh_threat_feeds.py).

Nothing here is fabricated: unknown shapes raise, malformed entries are
counted as skipped, and every record carries its source URL.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

SCAMSNIFTER_URL = ("https://raw.githubusercontent.com/scamsniffer/"
                   "scam-database/main/blacklist/all.json")
RANSOMWHERE_URL = "https://api.ransomwhe.re/export"

_EVM_RE = re.compile(r"0x[0-9a-fA-F]{40}")
_BTC_RE = re.compile(r"^(bc1[qpzry9x8gf2tvdw0s3jn54khce6mua7l]{11,71}|"
                     r"[13][a-km-zA-HJ-NP-Z1-9]{25,34})$")


@dataclass(frozen=True)
class ThreatRecord:
    address: str      # normalized: EVM lowercase, BTC as published
    chain: str        # "evm" | "bitcoin"
    category: str     # "scam" | "ransomware"
    source: str       # "scamsniffer" | "ransomwhere"
    label: str        # ransomware family, or "" for scam entries
    reference: str    # feed URL the record was extracted from


@dataclass
class ThreatFeedList:
    """Lookup index over ingested threat-feed records."""
    records: tuple[ThreatRecord, ...] = ()
    skipped: int = 0  # malformed entries seen during ingestion

    def __post_init__(self) -> None:
        index: dict[str, list[ThreatRecord]] = {}
        for r in self.records:
            key = r.address.lower() if r.chain == "evm" else r.address
            index.setdefault(key, []).append(r)
        self._index = index

    def lookup(self, address: str) -> tuple[ThreatRecord, ...]:
        hits = self._index.get(address.lower())
        if hits:
            return tuple(hits)
        return tuple(self._index.get(address, ()))


def parse_scamsniffer(data: dict) -> tuple[list[ThreatRecord], int]:
    """Parse ScamSniffer all.json. Returns (records, skipped)."""
    if not isinstance(data, dict) or not isinstance(
            data.get("address"), list):
        raise ValueError("scamsniffer: expected {\"address\": [...]} — "
                         "feed shape changed?")
    records: list[ThreatRecord] = []
    skipped = 0
    for entry in data["address"]:
        if isinstance(entry, str) and _EVM_RE.fullmatch(entry.strip()):
            records.append(ThreatRecord(
                address=entry.strip().lower(), chain="evm",
                category="scam", source="scamsniffer", label="",
                reference=SCAMSNIFTER_URL))
        else:
            skipped += 1
    return records, skipped


def parse_ransomwhere(data: dict) -> tuple[list[ThreatRecord], int]:
    """Parse Ransomwhere /export. Returns (records, skipped)."""
    if not isinstance(data, dict) or not isinstance(
            data.get("result"), list):
        raise ValueError("ransomwhere: expected {\"result\": [...]} — "
                         "feed shape changed?")
    records: list[ThreatRecord] = []
    skipped = 0
    for entry in data["result"]:
        addr = entry.get("address") if isinstance(entry, dict) else None
        chain = entry.get("blockchain") if isinstance(entry, dict) else None
        if (isinstance(addr, str) and chain == "bitcoin"
                and _BTC_RE.match(addr)):
            records.append(ThreatRecord(
                address=addr, chain="bitcoin", category="ransomware",
                source="ransomwhere",
                label=str(entry.get("family") or "unlabeled"),
                reference=RANSOMWHERE_URL))
        else:
            skipped += 1
    return records, skipped


def parse_scamsniffer_file(path: str | Path) -> ThreatFeedList:
    records, skipped = parse_scamsniffer(
        json.loads(Path(path).read_text()))
    return ThreatFeedList(records=tuple(records), skipped=skipped)


def parse_ransomwhere_file(path: str | Path) -> ThreatFeedList:
    records, skipped = parse_ransomwhere(
        json.loads(Path(path).read_text()))
    return ThreatFeedList(records=tuple(records), skipped=skipped)


def merge(*lists: ThreatFeedList) -> ThreatFeedList:
    records: list[ThreatRecord] = []
    skipped = 0
    for lst in lists:
        records.extend(lst.records)
        skipped += lst.skipped
    return ThreatFeedList(records=tuple(records), skipped=skipped)


def load_snapshots(data_dir: str | Path = "data/threat_feeds"
                   ) -> ThreatFeedList | None:
    """Load the newest ScamSniffer + Ransomwhere snapshots written by
    scripts/refresh_threat_feeds.py. Returns None when no snapshot
    exists (the pipeline then skips feed checks — it never invents
    feed data). Raises on a corrupt snapshot: a half-written feed
    must fail loudly, not silently degrade."""
    data_dir = Path(data_dir)
    lists: list[ThreatFeedList] = []
    for prefix, parser in (("scamsniffer-", parse_scamsniffer_file),
                           ("ransomwhere-", parse_ransomwhere_file)):
        paths = sorted(data_dir.glob(f"{prefix}*.json"))
        if not paths:
            continue
        lists.append(parser(paths[-1]))  # newest dated snapshot
    if not lists:
        return None
    return merge(*lists)


def from_fixture(
        path: Optional[str | Path] = None) -> ThreatFeedList:
    """Vendored sample of REAL entries (with provenance in _meta)."""
    path = path or Path(__file__).parent / "fixtures" / \
        "threat_feeds_sample.json"
    data = json.loads(Path(path).read_text())
    records: list[ThreatRecord] = []
    for addr in data.get("scamsniffer", []):
        records.append(ThreatRecord(
            address=addr.lower(), chain="evm", category="scam",
            source="scamsniffer", label="",
            reference=data["_meta"]["scamsniffer_url"]))
    for entry in data.get("ransomwhere", []):
        records.append(ThreatRecord(
            address=entry["address"], chain="bitcoin",
            category="ransomware", source="ransomwhere",
            label=entry.get("family") or "unlabeled",
            reference=data["_meta"]["ransomwhere_url"]))
    return ThreatFeedList(records=tuple(records))
