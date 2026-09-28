"""Seed evaluation users and feedback (no watch API calls)."""
import asyncio
import sys

sys.path.insert(0, ".")
from dotenv import load_dotenv

load_dotenv()


async def main() -> int:
    from api.core.config import settings
    from engine.store import init_store
    from scripts.seed_demo import _seed_users, _seed_feedback

    store = await init_store(settings)
    ctx = {"store": store, "settings": settings}

    # Build case_id map.
    cases, _ = await store.list_cases(limit=1000)
    case_ids = {c.fir_number: c.id for c in cases}

    print("seeding users...")
    await _seed_users(ctx)
    print("seeding feedback...")
    await _seed_feedback(ctx, case_ids)
    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
