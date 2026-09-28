"""Bounded live smoke for the M27 evaluation seed (scripts/seed_demo.py).

Runs 2 evaluation cases through the REAL pipeline (worker.trace_wallet
with live indexers) plus 2 watch baselines, against process-local
stores — no Postgres/Neo4j needed. Validates the whole seed path:
idempotent case creation, report outcome fields, filing record, graph
meta with hop classifications, watch + baseline check.

Wired into regression --live. Run: uv run python scripts/smoke_m27.py
"""
import asyncio
import os
import sys
from pathlib import Path
from uuid import UUID

REPO_ROOT = Path(__file__).resolve().parent.parent
os.chdir(REPO_ROOT)
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

# 2 subjects, both known-active from the 2026-09-28 verification pass.
CASES = [
    dict(eval_id="EVAL/SMOKE/0001",
         address="0xbf28eb4e017e472f9f6c11efabcdcb97f5761707",
         chain="ethereum", label="ScamSniffer phishing/scam address",
         source="ScamSniffer blacklist/all.json snapshot 2026-09-28"),
    dict(eval_id="EVAL/SMOKE/0002",
         address="17TMc2UkVRSga2yYvuxSD9Q1XyB2EPRjTF",
         chain="bitcoin", label="Ransomwhere: Netwalker/Mailto",
         source="Ransomwhere export snapshot 2026-09-28"),
]
WATCHES = [
    dict(address="0xbf28eb4e017e472f9f6c11efabcdcb97f5761707",
         chain="ethereum", label="smoke: ScamSniffer suspect",
         classification="suspect", case_eval_id="EVAL/SMOKE/0001"),
    dict(address="17TMc2UkVRSga2yYvuxSD9Q1XyB2EPRjTF",
         chain="bitcoin", label="smoke: Netwalker ransom wallet",
         classification="suspect", case_eval_id="EVAL/SMOKE/0002"),
]


async def main() -> int:
    import seed_demo
    from api.core.config import settings
    from engine.graph.memory_store import MemoryGraphStore
    from engine.store.memory import MemoryStore
    from worker import _load_sanctions, _load_threat_feeds

    seed_demo.CASES[:] = CASES
    seed_demo.WATCHES[:] = WATCHES

    store = MemoryStore()
    graph_store = MemoryGraphStore()
    ctx = {"settings": settings, "store": store,
           "sanctions": _load_sanctions(settings),
           "threat_feeds": _load_threat_feeds(),
           "graph_store": graph_store}

    sem = asyncio.Semaphore(2)
    results = await asyncio.gather(
        *(seed_demo._seed_case(ctx, c, sem) for c in CASES))
    assert all(s == "traced" for s, _ in results), results
    print("cases traced:", results, flush=True)

    # re-run one case: must be idempotent (report exists -> skip)
    again = await seed_demo._seed_case(ctx, CASES[0], sem)
    assert again[0] == "skipped", again
    print("idempotency OK: second run skipped", flush=True)

    case_ids: dict[str, UUID] = {}
    for c in CASES:
        case = await seed_demo._find_case(store, c["eval_id"])
        assert case is not None
        case_ids[c["eval_id"]] = case.id

        # M26 outcome fields are populated from the real pipeline
        report = await store.get_report_by_case(case.id)
        assert report is not None
        assert report.risk_score is not None, "risk_score missing"
        assert report.confidence is not None, "confidence missing"
        assert report.terminal_reason, "terminal_reason missing"
        assert report.hop_count and report.hop_count > 0
        print(f"[{c['eval_id']}] risk={report.risk_score} "
              f"({report.risk_level}) conf={report.confidence:.2f} "
              f"terminal={report.terminal_reason} hops={report.hop_count}",
              flush=True)

        # filing recorded for the delivery attempt
        filings, total = await store.list_filings(limit=50)
        mine = [f for f in filings if f.case_id == case.id]
        assert mine, "no filing recorded"
        print(f"[{c['eval_id']}] filing: {mine[0].status} "
              f"(attempts={mine[0].attempts})", flush=True)

        # graph meta carries the classified hop path
        meta = await graph_store.case_meta(str(case.id))
        assert meta and meta.get("hops"), "graph meta hops missing"
        kinds = {h.get("kind") for h in meta["hops"]}
        print(f"[{c['eval_id']}] graph meta hops={len(meta['hops'])} "
              f"kinds={sorted(k for k in kinds if k)}", flush=True)

    await seed_demo._seed_watches(ctx, case_ids)
    watches = await store.list_watches(active_only=False)
    assert len(watches) == 2, [w.label for w in watches]
    for w in watches:
        checks = await store.list_watch_checks(w.id, limit=10)
        assert checks and checks[0].baseline, "baseline check missing"
        assert w.classification == "suspect"
    print("watches + baseline checks OK", flush=True)

    print("smoke_m27: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
