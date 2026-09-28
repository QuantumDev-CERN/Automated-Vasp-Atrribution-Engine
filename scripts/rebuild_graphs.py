"""Rebuild case graphs from report hop paths.

Parses the FUND-FLOW PATH section of each report_text and builds a
linear TxGraph (subject -> hop1 -> hop2 -> ... -> terminal). This is
the ACTUAL path the engine traced — not the full transaction graph
with un-traversed branches, but the real path, honestly labeled.

For EVAL/2026/0047 (feed-attributed, 0 hops): subject node only.
"""
import asyncio
import re
import sys

sys.path.insert(0, ".")
from dotenv import load_dotenv

load_dotenv()

GRAPH_FILE = "data/graph_store.json"

# Matches: "1 | 14ped4Kn2JvfxeKNJGRU1h2j6iQzRRTy8J | peel | 0.95 x 0.9 = 0.855"
HOP_RE = re.compile(r"^\s*(\d+)\s*\|\s*([A-Za-z0-9]+)\s*\|\s*([a-z\-]+)\s*\|")


def parse_path(report_text: str) -> list[tuple[str, str]]:
    """Extract [(address, kind)] from the FUND-FLOW PATH section."""
    hops = []
    in_path = False
    for line in report_text.split("\n"):
        if "FUND-FLOW PATH" in line:
            in_path = True
            continue
        if in_path:
            if line.strip().startswith("4.") or "TERMINAL ASSESSMENT" in line:
                break
            m = HOP_RE.match(line)
            if m:
                hops.append((m.group(2), m.group(3)))
    return hops


async def main() -> int:
    from api.core.config import settings
    from engine.graph.builder import TxGraph
    from engine.graph.memory_store import MemoryGraphStore
    from engine.store import init_store
    from sqlalchemy import select
    from engine.store import models

    store = await init_store(settings)
    graph_store = MemoryGraphStore.load_from_file(GRAPH_FILE)
    if graph_store is None:
        graph_store = MemoryGraphStore()

    async with store._sessions() as s:
        q = select(models.ReportRecord, models.Case).join(
            models.Case, models.ReportRecord.case_id == models.Case.id)
        rows = (await s.execute(q)).all()

    n_built = 0
    for rec, case in rows:
        hops = parse_path(rec.report_text)
        # Build linear graph: subject -> hop1 -> ... -> terminal
        g = TxGraph()
        # Subject node
        g.g.add_node(case.suspect_address, chains={case.chain},
                     first_seen=None, labels={"subject"})
        prev = case.suspect_address
        for addr, kind in hops:
            if addr not in g.g:
                g.g.add_node(addr, chains={case.chain},
                             first_seen=None, labels={kind})
            else:
                g.g.nodes[addr]["labels"].add(kind)
            # Add a directed edge (hop). Use a synthetic tx id.
            g.g.add_edge(prev, addr, key=f"hop:{prev[:8]}->{addr[:8]}",
                         kind=kind)
            prev = addr
        # Terminal node gets its tag label
        if rec.terminal_address and rec.terminal_reason:
            # Terminal is the last hop (or subject if no hops)
            term = hops[-1][0] if hops else case.suspect_address
            if term in g.g.nodes:
                g.g.nodes[term]["labels"].add(rec.terminal_reason)

        meta = {
            "eval_id": case.fir_number,
            "subject": case.suspect_address,
            "chain": case.chain,
            "terminal": rec.terminal_address,
            "terminal_reason": rec.terminal_reason,
            "risk_score": rec.risk_score,
            "risk_level": rec.risk_level,
            "confidence": rec.confidence,
            "hop_count": rec.hop_count,
            "reconstructed_from": "report_text FUND-FLOW PATH "
                                  "(linear path only, not full tx graph)",
        }
        await graph_store.save_case_subgraph(
            str(case.id), g, meta)
        n_built += 1
        if n_built % 10 == 0:
            print(f"  built {n_built}/{len(rows)}...", flush=True)

    graph_store.save_to_file(GRAPH_FILE)
    print(f"built {n_built} case graphs → {GRAPH_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
