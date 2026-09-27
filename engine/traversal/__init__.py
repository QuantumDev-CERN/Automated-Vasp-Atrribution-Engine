"""Traversal engine: typology-aware walk over the classified fund-flow graph."""
from .engine import (
    TraversalConfig,
    TraversalResult,
    Terminal,
    VisitedNode,
    traverse,
)

__all__ = [
    "TraversalConfig",
    "TraversalResult",
    "Terminal",
    "VisitedNode",
    "traverse",
]
