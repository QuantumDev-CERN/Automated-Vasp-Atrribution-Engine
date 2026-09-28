"""Rebuild graph-store tags from Postgres reports.

Reads every report's terminal_address + terminal_reason and tags the
address in a file-backed MemoryGraphStore. This populates the intel
ranked feed without re-running traces — the tags come from real
engine output already stored in Postgres.
"""
import asyncio
import sys

sys.path.insert(0, ".")
from dotenv import load_dotenv

load_dotenv()

GRAPH_FILE = "data/graph_store.json"


async def main() -> int:
    from api.core.config import settings
    from engine.graph.memory_store import MemoryGraphStore
    from engine.store import init_store

    store = await init_store(settings)

    # Load existing file if present (don't clobber re-traced graphs).
    graph_store = MemoryGraphStore.load_from_file(GRAPH_FILE)
    if graph_store is None:
        graph_store = MemoryGraphStore()
        print("no existing graph file — starting fresh")
    else:
        print(f"loaded existing graph file: "
              f"{len(graph_store.to_dict()['cases'])} cases")

    # Pull all cases and their reports via public APIs.
    cases, total = await store.list_cases(limit=1000)
    print(f"found {len(cases)} cases (total {total})")

    n_tagged = 0
    for case in cases:
        # Use the fixed get_report_by_case (queries latest report directly).
        report = await store.get_report_by_case(case.id)
        if not report:
            continue
        if not report.terminal_address or not report.terminal_reason:
            continue
        await graph_store.tag_address(
            report.terminal_address, case.chain,
            report.terminal_reason,
            source=f"report:{report.id}", case_id=str(case.id))
        # Also populate the address->case index (normally done by
        # save_case_subgraph, which we bypass here).
        key = (case.chain, report.terminal_address)
        graph_store._addr_cases.setdefault(key, set()).add(str(case.id))
        n_tagged += 1

    graph_store.save_to_file(GRAPH_FILE)
    print(f"tagged {n_tagged} terminal addresses → {GRAPH_FILE}")

    # Verify the intel feed would see them.
    from collections import Counter
    tags = Counter()
    for (chain, addr), entries in graph_store._tags.items():
        for e in entries:
            tags[e["tag"]] += 1
    print("tag distribution:", dict(tags))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
