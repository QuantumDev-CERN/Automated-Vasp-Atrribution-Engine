"""Create remaining watch records without live API ticks.

The full seed_phases.py hangs on slow indexer API calls during watch
baseline ticks. This script creates only the watch records (idempotent)
so the 18-watch target is met. Baseline ticks can be done later via the
watchlist API.
"""
import asyncio
import sys

sys.path.insert(0, ".")
from dotenv import load_dotenv

load_dotenv()


async def main() -> int:
    from api.core.config import settings
    from engine.store import init_store
    from engine.store.base import WatchIn
    from scripts.seed_demo import WATCHES, CASES

    store = await init_store(settings)

    # Build case_id map.
    cases, _ = await store.list_cases(limit=1000)
    case_ids = {c.fir_number: c.id for c in cases}

    existing = await store.list_watches(active_only=False)
    known = {(w.address.lower(), w.chain) for w in existing}
    print(f"existing watches: {len(existing)}")

    n_created = 0
    for spec in WATCHES:
        key = (spec["address"].lower(), spec["chain"])
        if key in known:
            continue
        rec = await store.add_watch(WatchIn(
            address=spec["address"], chain=spec["chain"],
            label=spec["label"], classification=spec["classification"],
            case_id=case_ids.get(spec["case_eval_id"] or ""),
            created_by="evaluation-seed"))
        print(f"[watch] created (no ticks): {spec['label']}")
        n_created += 1

    print(f"created {n_created} watch records")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
