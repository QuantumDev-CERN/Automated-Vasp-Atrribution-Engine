"""VASP directory & routing engine (M5).

directory.py  - VaspRecord model + curated FIU-IND seed (public sources only).
routing.py    - legal-instrument mapping (SAHYOG/PMLA vs MLAT vs Egmont vs
                stablecoin-issuer fast-track) + per-instrument request templates.

"Nearest" VASP = first custodial + legally-addressable node, not fewest hops.
"""
from .directory import (
    VaspRecord,
    VASP_DIRECTORY,
    find_vasp,
    registered_vasps,
)
from .routing import (
    LegalInstrument,
    CaseDetails,
    RouteRecommendation,
    route_for,
    render_request,
    recommend_and_draft,
)

__all__ = [
    "VaspRecord",
    "VASP_DIRECTORY",
    "find_vasp",
    "registered_vasps",
    "LegalInstrument",
    "CaseDetails",
    "RouteRecommendation",
    "route_for",
    "render_request",
    "recommend_and_draft",
]
