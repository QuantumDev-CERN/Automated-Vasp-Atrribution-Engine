"""M24 live smoke: scam/ransomware feed ingestion.

Two parts:

1. Live fetch (no API key needed): download both verified feeds —
   ScamSniffer blacklist/all.json and the Ransomwhere /export API —
   validate their shapes, and assert sane minimum counts
   (ScamSniffer >= 4000 addresses, Ransomwhere >= 10000 records, the
   counts observed when the feeds were verified on 2026-09-28).
   Transient network errors SKIP instead of failing.

2. Fixture round-trip (offline): from_fixture() parses the vendored
   sample and lookups resolve.

Run: uv run python scripts/smoke_m24.py
"""
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.intel.threat_feeds import (
    RANSOMWHERE_URL, SCAMSNIFTER_URL, ThreatFeedList, from_fixture, merge,
    parse_ransomwhere, parse_scamsniffer,
)


class SectionSkip(Exception):
    pass


def _transient(message: str) -> bool:
    m = message.lower()
    return any(
        h in m
        for h in ("429", "502", "503", "504", "timeout", "timed out",
                  "connecterror", "connection reset", "temporarily unavailable",
                  "rate limit", "urlopen error")
    )


def _fetch(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "vasp-engine/1.0"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


def section_live_feeds() -> None:
    try:
        ss_data = _fetch(SCAMSNIFTER_URL)
    except Exception as e:  # noqa: BLE001
        raise SectionSkip(f"m24: scamsniffer fetch failed: {e}")
    ss_records, ss_skipped = parse_scamsniffer(ss_data)
    print(f"[live] scamsniffer: {len(ss_records)} addresses "
          f"({ss_skipped} malformed skipped)")
    assert len(ss_records) >= 4000, "scamsniffer feed shrank unexpectedly"

    try:
        rw_data = _fetch(RANSOMWHERE_URL)
    except Exception as e:  # noqa: BLE001
        raise SectionSkip(f"m24: ransomwhere fetch failed: {e}")
    rw_records, rw_skipped = parse_ransomwhere(rw_data)
    print(f"[live] ransomwhere: {len(rw_records)} records "
          f"({rw_skipped} malformed skipped)")
    assert len(rw_records) >= 10000, "ransomwhere feed shrank unexpectedly"

    feeds = merge(ThreatFeedList(records=tuple(ss_records),
                                 skipped=ss_skipped),
                  ThreatFeedList(records=tuple(rw_records),
                                 skipped=rw_skipped))
    probe = ss_records[0].address
    assert feeds.lookup(probe), "live lookup failed"
    assert feeds.lookup(probe.upper()), "case-insensitive lookup failed"
    print(f"[live] merged {len(feeds.records)} records; "
          f"lookup OK for {probe[:12]}…")


def section_fixture() -> None:
    feeds = from_fixture()
    assert len(feeds.records) == 24, len(feeds.records)
    assert feeds.lookup(feeds.records[0].address)
    assert feeds.lookup(feeds.records[0].address.upper())
    btc = [r for r in feeds.records if r.chain == "bitcoin"]
    assert btc and all(r.label for r in btc)
    print("[fixture] 24 real entries parsed; lookups resolve")


def main() -> int:
    ok = True
    try:
        section_live_feeds()
        print("PASS: live threat-feed fetch + parse")
    except SectionSkip as s:
        print(f"SKIP: {s}")
    except AssertionError as e:
        print(f"FAIL: {e}")
        ok = False
    except Exception as e:  # noqa: BLE001
        if _transient(str(e)):
            print(f"SKIP (transient): {e}")
        else:
            print(f"FAIL: {e}")
            ok = False
    try:
        section_fixture()
        print("PASS: fixture round-trip")
    except Exception as e:  # noqa: BLE001
        print(f"FAIL: {e}")
        ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
