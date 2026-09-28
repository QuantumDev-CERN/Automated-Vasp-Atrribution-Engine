"""Refresh the M24 threat-feed snapshots (ScamSniffer + Ransomwhere).

Downloads both feeds, validates their shapes (unknown shapes fail
loudly instead of ingesting garbage), and writes dated snapshots to
data/threat_feeds/ (gitignored). The engine loads them via
engine.intel.threat_feeds.load_snapshots; if no snapshot exists, the
pipeline skips feed checks (it never invents feed data).

Run: uv run python scripts/refresh_threat_feeds.py
"""
import json
import sys
import urllib.request
from datetime import date, timezone, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.intel.threat_feeds import (
    RANSOMWHERE_URL, SCAMSNIFTER_URL, merge, parse_ransomwhere,
    parse_ransomwhere_file, parse_scamsniffer, parse_scamsniffer_file,
)

DATA_DIR = Path("data/threat_feeds")


def _fetch(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "vasp-engine/1.0"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()

    print(f"fetching ScamSniffer ({SCAMSNIFTER_URL}) ...")
    ss_data = _fetch(SCAMSNIFTER_URL)
    ss_records, ss_skipped = parse_scamsniffer(ss_data)
    ss_path = DATA_DIR / f"scamsniffer-{today}.json"
    ss_path.write_text(json.dumps(ss_data))
    print(f"  {len(ss_records)} addresses, {ss_skipped} malformed skipped "
          f"-> {ss_path} ({ss_path.stat().st_size // 1024} KB)")

    print(f"fetching Ransomwhere ({RANSOMWHERE_URL}) ...")
    rw_data = _fetch(RANSOMWHERE_URL)
    rw_records, rw_skipped = parse_ransomwhere(rw_data)
    rw_path = DATA_DIR / f"ransomwhere-{today}.json"
    rw_path.write_text(json.dumps(rw_data))
    print(f"  {len(rw_records)} addresses, {rw_skipped} malformed skipped "
          f"-> {rw_path} ({rw_path.stat().st_size // 1024} KB)")

    # Round-trip check: the parsers must read back exactly what they
    # parsed from the live payloads.
    feeds = merge(parse_scamsniffer_file(ss_path),
                  parse_ransomwhere_file(rw_path))
    assert len(feeds.records) == len(ss_records) + len(rw_records)
    probe = ss_records[0].address
    assert feeds.lookup(probe), "round-trip lookup failed"
    assert feeds.lookup(probe.upper()), "case-insensitive lookup failed"
    print(f"OK: {len(feeds.records)} total records, "
          f"lookup verified for {probe[:12]}…")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
