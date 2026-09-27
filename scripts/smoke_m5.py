"""M5 smoke: VASP directory loads, routing decides, templates render.

Offline — no indexer keys needed. Exercises the full M5 path:
seed -> lookup -> legal-instrument routing -> drafted request.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.vasp import (
    VASP_DIRECTORY,
    find_vasp,
    registered_vasps,
    LegalInstrument,
    CaseDetails,
    recommend_and_draft,
)

CASE = CaseDetails(
    case_id="SAHYOG/2026/0042",
    agency="Cyber Cell, Delhi Police",
    officer="Insp. R. Sharma",
    wallets=("0xabc123",),
    tx_hashes=("0xdeadbeef",),
    date_from="2026-01-01",
    date_to="2026-09-27",
    suspected_offence="ransomware extortion",
)


def main() -> int:
    print(f"directory entries: {len(VASP_DIRECTORY)}")
    print(f"FIU-IND registered: {len(registered_vasps())}")

    # one of each route class
    for label, want in [
        ("CoinDCX", LegalInstrument.SAHYOG_PMLA),
        ("Binance", LegalInstrument.SAHYOG_PMLA),
        ("WhiteBIT", LegalInstrument.EGMONT),
        ("ChangeNOW", LegalInstrument.EGMONT),
        ("Tether", LegalInstrument.ISSUER_FREEZE),
    ]:
        result = recommend_and_draft(label, CASE)
        assert result is not None, f"no route for {label}"
        rec, draft = result
        assert rec.instrument == want, f"{label}: {rec.instrument} != {want}"
        assert "{" not in draft, f"{label}: unfilled placeholder"
        print(f"  {label:10s} -> {rec.instrument.value}"
              f"{' (parallel fast-track)' if rec.parallel_fast_track else ''}")

    assert recommend_and_draft("no-such-vasp", CASE) is None
    print("M5 smoke OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
