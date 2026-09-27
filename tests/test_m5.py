"""M5 tests: VASP directory seed validity, routing, request templates."""
from datetime import date

import pytest

from engine.vasp import (
    VaspRecord,
    VASP_DIRECTORY,
    find_vasp,
    registered_vasps,
    LegalInstrument,
    CaseDetails,
    route_for,
    render_request,
    recommend_and_draft,
)


def _sample_case() -> CaseDetails:
    return CaseDetails(
        case_id="SAHYOG/2026/0042",
        agency="Cyber Cell, Delhi Police",
        officer="Insp. R. Sharma",
        wallets=("0xabc123", "bc1qxyz789"),
        tx_hashes=("0xdeadbeef",),
        date_from="2026-01-01",
        date_to="2026-09-27",
        suspected_offence="ransomware extortion (BNS s.308)",
    )


# ---------- seed validity ----------

def test_seed_records_are_well_formed():
    assert len(VASP_DIRECTORY) >= 20
    for v in VASP_DIRECTORY:
        assert v.name and v.name.strip()
        assert v.category in ("exchange", "swap-service", "issuer")
        assert v.domicile and v.domicile.strip()
        assert v.fiu_evidence and len(v.fiu_evidence) > 20, v.name
        assert v.travel_rule in ("unknown", "notabene", "sygna", "trp")
        date.fromisoformat(v.as_of)  # raises on malformed date
        assert isinstance(v.fiu_ind_registered, bool)


def test_seed_has_no_duplicate_names_or_aliases():
    seen: dict[str, str] = {}

    def norm(s: str) -> str:
        return "".join(c for c in s.lower() if c.isalnum())

    for v in VASP_DIRECTORY:
        record_keys = {norm(label) for label in (v.name, *v.aliases)}
        for key in record_keys:
            assert key not in seen, f"duplicate alias {key!r}"
            seen[key] = v.name


def test_travel_rule_is_honest_everywhere():
    # Participation is not publicly verifiable per-VASP; the seed must not
    # fabricate it. The field exists so the routing layer can cite it.
    assert all(v.travel_rule == "unknown" for v in VASP_DIRECTORY)


def test_pib_noncompliant_entities_present_and_unregistered():
    # PIB press release 09 SEP 2026 named these 15; all must be in the seed
    # and marked NOT FIU-IND registered.
    expected = ["weex", "blofin", "rezorex", "bitunix", "digifinex",
                "toobit", "xt.com", "latoken", "woo x", "pionex",
                "changenow", "simpleswap", "fixedfloat", "whitebit",
                "guardarian"]
    for name in expected:
        v = find_vasp(name)
        assert v is not None, f"{name} missing from seed"
        assert v.fiu_ind_registered is False, f"{name} wrongly registered"
        assert "PRID=2308131" in v.fiu_evidence or "PIB" in v.fiu_evidence


def test_registered_seed_has_domestic_and_offshore():
    reg = registered_vasps()
    assert len(reg) >= 10
    assert any(v.domicile == "IN" for v in reg)
    assert any(v.domicile != "IN" for v in reg)
    assert find_vasp("CoinDCX") in reg
    assert find_vasp("Binance") in reg  # offshore but registered


# ---------- lookup ----------

def test_lookup_case_insensitive_and_alias():
    assert find_vasp("coindcx").name == "CoinDCX"
    assert find_vasp("ChangeNOW").name == "ChangeNOW"
    assert find_vasp("change-now").name == "ChangeNOW"
    assert find_vasp("USDT").name == "Tether"
    assert find_vasp("no-such-vasp") is None


# ---------- routing ----------

def test_registered_domestic_routes_sahyog_pmla():
    rec = route_for(find_vasp("CoinDCX"))
    assert rec.instrument == LegalInstrument.SAHYOG_PMLA
    assert rec.escalations == ()
    assert not rec.parallel_fast_track


def test_registered_offshore_still_routes_sahyog_pmla():
    rec = route_for(find_vasp("Binance"))
    assert rec.instrument == LegalInstrument.SAHYOG_PMLA
    assert "offshore" in rec.rationale.lower()


def test_unregistered_foreign_routes_egmont_then_mlat():
    rec = route_for(find_vasp("WhiteBIT"))
    assert rec.instrument == LegalInstrument.EGMONT
    assert LegalInstrument.MLAT in rec.escalations
    assert "sahyog" in rec.rationale.lower()


def test_issuer_is_parallel_fast_track():
    rec = route_for(find_vasp("Tether"))
    assert rec.instrument == LegalInstrument.ISSUER_FREEZE
    assert rec.parallel_fast_track is True
    assert "parallel" in rec.rationale.lower()
    assert "never" in rec.rationale.lower()  # never a substitute


# ---------- templates ----------

@pytest.mark.parametrize("instrument", list(LegalInstrument))
def test_templates_render_with_no_unfilled_placeholders(instrument):
    vasp = find_vasp("CoinDCX")
    text = render_request(vasp, _sample_case(), instrument)
    assert "{" not in text and "}" not in text  # no leftover {placeholder}
    assert "SAHYOG/2026/0042" in text
    assert "0xabc123" in text
    assert text.startswith("DRAFT")


def test_issuer_template_carries_parallel_disclaimer():
    text = render_request(find_vasp("Tether"), _sample_case(),
                          LegalInstrument.ISSUER_FREEZE)
    assert "NOT a substitute" in text


# ---------- end-to-end helper ----------

def test_recommend_and_draft_end_to_end():
    result = recommend_and_draft("wazirx", _sample_case())
    assert result is not None
    rec, draft = result
    assert rec.instrument == LegalInstrument.SAHYOG_PMLA
    assert "WazirX" in draft


def test_recommend_and_draft_unknown_label_returns_none():
    assert recommend_and_draft("mystery-exchange", _sample_case()) is None
