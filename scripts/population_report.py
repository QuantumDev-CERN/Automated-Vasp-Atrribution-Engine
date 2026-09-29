"""Population quality report: what the engine actually traced.

Read-only. For every EVAL case shows the transactions ingested, the
graph topology (addresses), hop count, terminal classification and
methodology — so "66 cases seeded" can be audited as "N cases with
real transaction topology" rather than taken on faith.

Usage:
    uv run python scripts/population_report.py
"""
import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _row(case_id, chain, subject, method, txs, addrs, hops, terminal,
         risk, conf):
    subj = subject[:18] + "..." if len(subject) > 21 else subject
    term = (terminal or "-")[:22]
    return (f"{case_id:<16} {chain:<8} {subj:<21} {method:<6} "
            f"{txs:>6} {addrs:>6} {hops:>4} {term:<22} "
            f"{str(risk):>4} {conf if conf is not None else '-':>5}")


async def collect_population(store, graph_store):
    """Per-case rows + summary dict. Shared by this script and the seed's
    end-of-run summary."""
    cases, _total = await store.list_cases(limit=500)
    eval_cases = sorted(
        (c for c in cases if (c.fir_number or "").startswith("EVAL/")),
        key=lambda c: c.fir_number)

    rows = []
    summary = {
        "cases": len(eval_cases), "reports": 0, "no_report": 0,
        "topology": 0, "feed": 0, "empty_dead_end": 0,
        "total_txs": 0, "max_hops": 0,
        "terminals": {}, "chains": {},
    }
    for case in eval_cases:
        report = await store.get_report_by_case(case.id)
        stats = await graph_store.case_stats(str(case.id))
        txs = (stats or {}).get("transactions", 0)
        addrs = (stats or {}).get("addresses", 0)
        summary["chains"][case.chain] = \
            summary["chains"].get(case.chain, 0) + 1
        if report is None:
            summary["no_report"] += 1
            rows.append({"line": _row(
                case.fir_number, case.chain, case.suspect_address,
                "?", txs, addrs, "-", "-", "-", "-") + "   <-- NO REPORT",
                "flag": "no_report"})
            continue
        summary["reports"] += 1
        method = ("feed"
                  if report.terminal_reason == "threat-feed-direct"
                  else "engine")
        hops = report.hop_count if report.hop_count is not None else 0
        summary["max_hops"] = max(summary["max_hops"], hops)
        summary["total_txs"] += txs
        term = report.terminal_reason or "?"
        summary["terminals"][term] = summary["terminals"].get(term, 0) + 1
        flag = ""
        if method == "feed":
            summary["feed"] += 1
        elif txs > 0:
            summary["topology"] += 1
        elif hops == 0 and report.terminal_reason == "dead-end":
            summary["empty_dead_end"] += 1
            flag = ("   <-- EMPTY DEAD-END "
                    "(audit: genuine or failed trace?)")
        rows.append({"line": _row(
            case.fir_number, case.chain, case.suspect_address, method,
            txs, addrs, hops, report.terminal_reason,
            report.risk_score, report.confidence) + flag,
            "flag": "empty" if flag else ""})
    return rows, summary


def format_summary(s) -> str:
    return (
        f"cases: {s['cases']}  reports: {s['reports']}  "
        f"no report: {s['no_report']}\n"
        f"engine-traced with transaction topology: {s['topology']}\n"
        f"feed-attributed (no engine trace): {s['feed']}\n"
        f"empty dead-ends (0 txs, audit these): {s['empty_dead_end']}\n"
        f"total transactions ingested: {s['total_txs']}  "
        f"max hops: {s['max_hops']}\n"
        "terminals: " + ", ".join(
            f"{k}={v}" for k, v in sorted(s["terminals"].items())) + "\n"
        "chains: " + ", ".join(
            f"{k}={v}" for k, v in sorted(s["chains"].items())))


async def _amain() -> int:
    from api.core.config import settings
    from engine.graph import get_graph_store
    from engine.store import init_store
    from engine.store.memory import MemoryStore

    store = await init_store(settings)
    if isinstance(store, MemoryStore):
        print("REFUSING: memory store is process-local — start Postgres "
              "(docker compose up -d).")
        return 2
    graph_store = get_graph_store()

    rows, summary = await collect_population(store, graph_store)

    header = (f"{'case':<16} {'chain':<8} {'subject':<21} {'method':<6} "
              f"{'txs':>6} {'addrs':>6} {'hops':>4} {'terminal':<22} "
              f"{'risk':>4} {'conf':>5}")
    print(header)
    print("-" * len(header))
    for r in rows:
        print(r["line"])
    print("-" * len(header))
    print(format_summary(summary), end="")

    engine = getattr(store, "engine", None)
    if engine is not None:
        await engine.dispose()
    await graph_store.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.parse_args()
    return asyncio.run(_amain())


if __name__ == "__main__":
    raise SystemExit(main())
