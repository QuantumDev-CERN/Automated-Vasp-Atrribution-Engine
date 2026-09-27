"""VASP directory seed (M5).

Curated from PUBLIC sources only — every record carries its evidence
citation and an as-of date. Nothing here is scraped from private
directories, and nothing is guessed:

- FIU-IND registration status comes from press/industry reporting of the
  FIU-IND registry (method: web search 2026-09-27, cross-checked across
  multiple outlets). "54 VDASPs registered as of mid-2026" per
  cryptotimes.io (2026-09-16); domestic list corroborated by khelja.in /
  asianetnews (2026-09).
- The 15 NON-COMPLIANT offshore entities are taken verbatim from the
  official PIB press release of 09 SEP 2026 (PRID=2308131): notices under
  PMLA Section 13 for operating without complying with PMLA provisions.
  These are explicitly marked fiu_ind_registered=False.
- travel_rule is "unknown" for every entry. The field exists because the
  master reference requires it (Notabene/Sygna/TRP participation
  determines which data channel a disclosure request can cite), but
  participation is not publicly verifiable per-VASP, so we do NOT
  fabricate values. Populate from the Travel Rule protocol directories
  when available.
- compliance_contact is None throughout: populate from each VASP's
  published compliance/grievance page. Not scraped for this seed.

Domicile labels: "IN" = India-domiciled; two-letter codes where the
entity name makes it unambiguous (WhiteBIT's "UAB" = Lithuania);
"offshore" where the operator is multi-jurisdiction or the domicile is
not publicly pinned down. Routing only distinguishes IN vs non-IN plus
the FIU-IND flag, so coarse labels are sufficient and honest.
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class VaspRecord:
    name: str                      # canonical trade name
    legal_entity: str | None       # entity name as stated in public sources
    aliases: tuple[str, ...]       # lookup aliases (trade-name variants)
    category: str                  # "exchange" | "swap-service" | "issuer"
    domicile: str                  # "IN" | "US" | "LT" | "offshore" ...
    fiu_ind_registered: bool       # FIU-IND Reporting Entity (PMLA) status
    fiu_evidence: str              # source citation for the registration claim
    travel_rule: str = "unknown"   # "unknown" | "notabene" | "sygna" | "trp"
    compliance_contact: str | None = None
    as_of: str = "2026-09-27"      # YYYY-MM-DD
    notes: str = ""


def _r(name, legal_entity, aliases, category, domicile, registered,
       evidence, notes=""):
    return VaspRecord(
        name=name,
        legal_entity=legal_entity,
        aliases=tuple(aliases),
        category=category,
        domicile=domicile,
        fiu_ind_registered=registered,
        fiu_evidence=evidence,
        notes=notes,
    )


_REG_EVIDENCE_DOMESTIC = (
    "Industry reporting of FIU-IND registry (khelja.in / asianetnews, "
    "Sep 2026; cryptotimes.io 2026-09-16: 54 VDASPs registered as of "
    "mid-2026). Method: web search 2026-09-27, cross-checked outlets."
)
_REG_EVIDENCE_OFFSHORE = (
    "cryptotimes.io 2026-09-16 (54 VDASPs registered as of mid-2026, "
    "incl. named offshore platforms); datawallet.com 2026 listing. "
    "Method: web search 2026-09-27, cross-checked outlets."
)
_NONCOMPLIANT_EVIDENCE = (
    "PIB press release 09 SEP 2026 (PRID=2308131): FIU-IND issued PMLA "
    "Section 13 non-compliance notices; entities found operating without "
    "complying with PMLA provisions in India."
)

VASP_DIRECTORY: list[VaspRecord] = [
    # ---- FIU-IND registered, India-domiciled ----
    _r("CoinDCX", None, ("coindcx",), "exchange", "IN", True,
       _REG_EVIDENCE_DOMESTIC),
    _r("CoinSwitch", None, ("coinswitch", "coinswitch kuber"), "exchange",
       "IN", True, _REG_EVIDENCE_DOMESTIC),
    _r("WazirX", None, ("wazirx",), "exchange", "IN", True,
       _REG_EVIDENCE_DOMESTIC),
    _r("ZebPay", None, ("zebpay",), "exchange", "IN", True,
       _REG_EVIDENCE_DOMESTIC),
    _r("Mudrex", None, ("mudrex",), "exchange", "IN", True,
       _REG_EVIDENCE_DOMESTIC),
    _r("Giottus", None, ("giottus",), "exchange", "IN", True,
       _REG_EVIDENCE_DOMESTIC),
    _r("Unocoin", None, ("unocoin", "uno coin"), "exchange", "IN", True,
       _REG_EVIDENCE_DOMESTIC),
    _r("Bitbns", None, ("bitbns",), "exchange", "IN", True,
       _REG_EVIDENCE_DOMESTIC),
    _r("Delta Exchange", None, ("delta exchange", "delta"), "exchange",
       "IN", True, _REG_EVIDENCE_DOMESTIC,
       notes="Derivatives-focused platform."),
    _r("SunCrypto", None, ("suncrypto", "sun crypto"), "exchange", "IN",
       True, _REG_EVIDENCE_DOMESTIC),

    # ---- FIU-IND registered, offshore ----
    _r("Binance", None, ("binance",), "exchange", "offshore", True,
       _REG_EVIDENCE_OFFSHORE,
       notes="Multi-jurisdiction operator; FIU-IND registered 2024."),
    _r("KuCoin", None, ("kucoin",), "exchange", "offshore", True,
       _REG_EVIDENCE_OFFSHORE),
    _r("Bybit", None, ("bybit",), "exchange", "offshore", True,
       _REG_EVIDENCE_OFFSHORE),
    _r("Coinbase", None, ("coinbase",), "exchange", "US", True,
       _REG_EVIDENCE_OFFSHORE),
    _r("Bitget", None, ("bitget",), "exchange", "offshore", True,
       _REG_EVIDENCE_OFFSHORE),

    # ---- PIB 09-SEP-2026 non-compliant list (NOT registered) ----
    _r("Weex", "Weex International Exchange LTD", ("weex",), "exchange",
       "offshore", False, _NONCOMPLIANT_EVIDENCE),
    _r("Blofin", "BLF Global Limited", ("blofin",), "exchange",
       "offshore", False, _NONCOMPLIANT_EVIDENCE),
    _r("Rezorex", "RezorEx", ("rezorex",), "exchange", "offshore", False,
       _NONCOMPLIANT_EVIDENCE),
    _r("Bitunix", "Bitunix LLC", ("bitunix",), "exchange", "offshore",
       False, _NONCOMPLIANT_EVIDENCE),
    _r("DigiFinex", "DigiFinex Ltd", ("digifinex",), "exchange",
       "offshore", False, _NONCOMPLIANT_EVIDENCE),
    _r("Toobit", "Hopeful Technology Co. Ltd.", ("toobit",), "exchange",
       "offshore", False, _NONCOMPLIANT_EVIDENCE),
    _r("XT.com", "Fibtc Ltd / XT TECHNICAL PTE. LTD.", ("xt.com", "xt"),
       "exchange", "offshore", False, _NONCOMPLIANT_EVIDENCE),
    _r("Latoken", "LAtrade Ltd", ("latoken", "la token"), "exchange",
       "offshore", False, _NONCOMPLIANT_EVIDENCE),
    _r("WOO X", "Wootech Limited", ("woo x", "woox", "woo"),
       "exchange", "offshore", False, _NONCOMPLIANT_EVIDENCE),
    _r("Pionex", "Marketa Trading Inc.", ("pionex",), "exchange",
       "offshore", False, _NONCOMPLIANT_EVIDENCE),
    _r("ChangeNOW", "CHN Group LLC",
       ("changenow", "change now", "change-now"), "swap-service",
       "offshore", False, _NONCOMPLIANT_EVIDENCE,
       notes="Non-custodial swap service; also relevant to M4 "
             "swap-service terminal labels."),
    _r("SimpleSwap", "SimpleSwap LTD", ("simpleswap", "simple swap"),
       "swap-service", "offshore", False, _NONCOMPLIANT_EVIDENCE),
    _r("FixedFloat", "FFGX Group LLC",
       ("fixedfloat", "fixed float"), "swap-service", "offshore", False,
       _NONCOMPLIANT_EVIDENCE),
    _r("WhiteBIT", "UAB Clear White Technologies", ("whitebit", "white bit"),
       "exchange", "LT", False, _NONCOMPLIANT_EVIDENCE,
       notes="UAB = Lithuanian private limited company."),
    _r("Guardarian", "FinSeven CZ", ("guardarian",), "swap-service",
       "offshore", False, _NONCOMPLIANT_EVIDENCE),

    # ---- Stablecoin issuers: parallel fast-track route (M5 routing) ----
    _r("Tether", "Tether Holdings Limited", ("tether", "usdt"),
       "issuer", "offshore", False,
       "Issuer freeze route is contractual/direct with the issuer, not a "
       "PMLA registration matter; included for the parallel fast-track.",
       notes="USDT issuer. Blacklist requests go to Tether directly."),
    _r("Circle", "Circle Internet Financial, LLC", ("circle", "usdc"),
       "issuer", "US", False,
       "Issuer freeze route is contractual/direct with the issuer, not a "
       "PMLA registration matter; included for the parallel fast-track.",
       notes="USDC issuer. Blacklist requests go to Circle directly."),
]


def _normalize(label: str) -> str:
    """Lowercase, strip everything but alphanumerics: 'ChangeNOW' -> 'changenow'."""
    return "".join(c for c in label.lower() if c.isalnum())


# normalized alias -> record (names included as their own alias)
_ALIAS_INDEX: dict[str, VaspRecord] = {}
for _v in VASP_DIRECTORY:
    _seen: set[str] = set()
    for _a in (_v.name, *_v.aliases):
        _key = _normalize(_a)
        if _key in _seen:
            continue  # name that duplicates its own alias
        _seen.add(_key)
        if _key in _ALIAS_INDEX:
            raise ValueError(f"duplicate VASP alias in seed: {_a!r}")
        _ALIAS_INDEX[_key] = _v


def find_vasp(label: str) -> VaspRecord | None:
    """Directory lookup by trade name or alias — None when not in the seed."""
    return _ALIAS_INDEX.get(_normalize(label))


def registered_vasps() -> list[VaspRecord]:
    """All FIU-IND-registered entries (directly actionable via SAHYOG/PMLA)."""
    return [v for v in VASP_DIRECTORY if v.fiu_ind_registered]
