"""Sanctions intel (M7).

OFAC SDN crypto-address ingestion + lookup. Two paths:

1. from_fixture() — small vendored sample of REAL OFAC-listed addresses
   (engine/intel/fixtures/ofac_crypto_sample.json), extracted 2026-09-27
   from the official SDN Advanced XML. Used by tests and default runs.
2. parse_ofac_advanced_xml(path) — full ingestion from a downloaded copy
   of https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/
   exports/SDN_ADVANCED.xml (127MB). Streaming parse; ~1k crypto
   addresses. Run on demand / on a refresh schedule, not in tests.

Lookup normalizes EVM addresses to lowercase (OFAC lists checksummed
mixed-case); non-EVM addresses match exactly.
"""
import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

_NS = ("https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/"
       "exports/ADVANCED_XML")


def _q(tag: str) -> str:
    return f"{{{_NS}}}{tag}"


# FeatureTypeID -> currency code, from the OFAC FeatureTypeValues table
CRYPTO_FEATURE_TYPES = {
    "344": "XBT", "345": "ETH", "444": "XMR", "566": "LTC", "686": "ZEC",
    "687": "DASH", "688": "BTG", "689": "ETC", "706": "BSV", "726": "BCH",
    "746": "XVG", "887": "USDT", "907": "XRP", "989": "BNB", "992": "TRX",
    "996": "DOGE", "998": "USDC", "1007": "ARB", "1008": "BSC",
    "1167": "SOL",
}

SOURCE = "OFAC SDN Advanced XML"
_FIXTURE = Path(__file__).parent / "fixtures" / "ofac_crypto_sample.json"


@dataclass(frozen=True)
class SanctionsEntry:
    address: str      # as listed
    name: str         # sanctioned party name
    currency: str     # XBT | ETH | TRX | ...
    source: str = SOURCE


def parse_ofac_advanced_xml(path: str | Path) -> dict[str, SanctionsEntry]:
    """Stream-parse a downloaded SDN_ADVANCED.xml into address entries."""
    # Pass 1: identity ID -> primary latin name
    names: dict[str, str] = {}
    for _ev, el in ET.iterparse(str(path), events=("end",)):
        if el.tag != _q("Identity"):
            continue
        iid = el.get("ID")
        name = None
        for alias in el.findall(_q("Alias")):
            if alias.get("Primary") != "true":
                continue
            for dn in alias.findall(_q("DocumentedName")):
                if dn.get("DocNameStatusID") != "1":
                    continue
                parts = [np.text or ""
                         for dnp in dn.findall(_q("DocumentedNamePart"))
                         for np in dnp.findall(_q("NamePartValue"))
                         if np.get("ScriptID") == "215" and np.text]
                if not parts:
                    parts = [np.text or ""
                             for dnp in dn.findall(_q("DocumentedNamePart"))
                             for np in dnp.findall(_q("NamePartValue"))
                             if np.text]
                if parts:
                    name = " ".join(parts)
                    break
            if name:
                break
        if name:
            names[iid] = name
        el.clear()

    # Pass 2: crypto Feature entries -> addresses
    entries: dict[str, SanctionsEntry] = {}
    for _ev, el in ET.iterparse(str(path), events=("end",)):
        if el.tag != _q("Feature"):
            continue
        if el.get("FeatureTypeID") not in CRYPTO_FEATURE_TYPES:
            continue
        addr = iid = None
        for vd in el.iter(_q("VersionDetail")):
            if vd.get("DetailTypeID") == "1432" and vd.text:
                addr = vd.text.strip()
        for ir in el.iter(_q("IdentityReference")):
            iid = ir.get("IdentityID")
        if addr:
            entries[addr] = SanctionsEntry(
                address=addr,
                name=names.get(iid, "unknown"),
                currency=CRYPTO_FEATURE_TYPES[el.get("FeatureTypeID")])
        el.clear()
    return entries


def _normalize(address: str) -> str:
    address = address.strip()
    # EVM-style 0x addresses: case-insensitive
    if address.startswith(("0x", "0X")) and len(address) == 42:
        return address.lower()
    return address


class SanctionsList:
    """Lookup table over sanctioned crypto addresses."""

    def __init__(self, entries: dict[str, SanctionsEntry]) -> None:
        self._by_addr = {_normalize(a): e for a, e in entries.items()}

    @classmethod
    def from_fixture(cls) -> "SanctionsList":
        raw = json.loads(_FIXTURE.read_text())
        return cls({addr: SanctionsEntry(address=addr, **meta)
                    for addr, meta in raw["entries"].items()})

    @classmethod
    def from_ofac_xml(cls, path: str | Path) -> "SanctionsList":
        return cls(parse_ofac_advanced_xml(path))

    def lookup(self, address: str) -> SanctionsEntry | None:
        return self._by_addr.get(_normalize(address))

    def __len__(self) -> int:
        return len(self._by_addr)
