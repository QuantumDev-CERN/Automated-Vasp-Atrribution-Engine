"""Run the post-trace seed phases: watches, users, feedback.

Standalone so it doesn't re-enter the case/trace logic.
"""
import asyncio
import sys
import time

sys.path.insert(0, ".")
from dotenv import load_dotenv

load_dotenv()


async def main() -> int:
    from uuid import UUID
    from api.core.config import settings
    from engine.store import init_store
    from scripts.seed_demo import (
        _seed_watches, _seed_users, _seed_feedback, CASES,
    )
    from worker import _load_sanctions, _load_threat_feeds
    from engine.graph.memory_store import MemoryGraphStore

    t0 = time.time()
    store = await init_store(settings)
    print(f"store: {type(store).__name__}")

    # Build case_ids map (eval_id -> UUID).
    case_ids: dict[str, UUID] = {}
    cases, _ = await store.list_cases(limit=100)
    for c in CASES:
        match = next((x for x in cases if x.fir_number == c["eval_id"]),
                     None)
        if match is not None:
            case_ids[c["eval_id"]] = match.id
    print(f"found {len(case_ids)} cases")

    ctx = {
        "settings": settings,
        "store": store,
        "sanctions": _load_sanctions(settings),
        "threat_feeds": _load_threat_feeds(),
        "graph_store": MemoryGraphStore(),
    }

    await _seed_watches(ctx, case_ids)
    await _seed_users(ctx)
    await _seed_feedback(ctx, case_ids)

    print(f"done in {time.time() - t0:.0f}s")
    engine = getattr(store, "engine", None)
    if engine is not None:
        await engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
