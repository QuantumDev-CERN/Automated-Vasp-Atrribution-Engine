"""Hop classifier: tags each transfer edge (peel | sweep-candidate | direct-transfer)."""
from .hops import HopClassification, HopKind, classify_edge, classify_graph

__all__ = ["HopClassification", "HopKind", "classify_edge", "classify_graph"]
