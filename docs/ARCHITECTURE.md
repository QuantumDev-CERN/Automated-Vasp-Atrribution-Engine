# Architecture

Source of truth: `AUTOMATED-VASP-ATTRIBUTION-MASTER-REFERENCE.md` (kept with the user).

## Data flow

```
SAHYOG case ──▶ Intake API ──▶ arq job (redis) ──▶ Trace pipeline ──▶ webhook ──▶ SAHYOG
                                              │
                        ┌─────────────────────┼─────────────────────┐
                        ▼                     ▼                     ▼
                 Chain adapters        Graph + classifier      VASP directory
                 (canonical tx)        + traversal             + scoring
```

## Key design decisions

- **Canonical tx schema** (`engine/adapters/base.py`): every adapter normalizes
  into one shape. Graph engine, classifier, and scoring never touch chain-specific
  payloads.
- **Async jobs, not request/response**: traces and watchlist hits resolve over
  minutes/days; the API returns a job id immediately and pushes results via webhook.
- **Phase 1 graph**: networkx in-memory per trace job; case subgraphs persisted
  to Neo4j. Sharding/pruning is phase 2.
- **Confidence is path-based**: discounts multiply along the path; mixer hops
  crater it, explicit-destination bridges barely dent it, confirmed sweeps spike it.
- **"Nearest" VASP** = first custodial + legally-addressable node, not fewest hops.
