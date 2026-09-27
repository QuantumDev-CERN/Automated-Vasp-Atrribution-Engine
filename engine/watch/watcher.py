"""M10: watchlist engine.

check_watch() polls one watched address and diffs the recent transaction
list against the hashes seen on previous ticks. The first tick only
establishes the baseline (no alert spam for history that predates the
subscription).

Direction is relative to the watched address; counterparties are
resolved against an optional vasp_resolver (same seam as the M7
pipeline) so movement straight into a known VASP is flagged.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional
from uuid import UUID

from .alerts import deliver_watch_alert

_SEEN_CAP = 200


def _same(a: str, b: str) -> bool:
    if a.startswith("0x") and b.startswith("0x"):
        return a.lower() == b.lower()
    return a == b


@dataclass
class WatchEvent:
    watch_id: UUID
    tx_hash: str
    direction: str  # in|out
    counterparty: str
    value: str
    asset: str
    vasp_hit: Optional[str] = None
    block_time: Optional[str] = None


@dataclass
class WatchCheck:
    watch_id: UUID
    baseline: bool  # first tick: baseline established, no events
    events: list[WatchEvent] = field(default_factory=list)
    seen_hashes: list[str] = field(default_factory=list)
    checked_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc))


async def check_watch(
    watch,
    adapter_factory: Callable[[str], object],
    *,
    vasp_resolver: Callable[[str], Optional[str]] = lambda a: None,
    limit: int = 25,
) -> WatchCheck:
    """Poll one watch; return new movements since the last tick."""
    adapter = adapter_factory(watch.chain)
    txs = await adapter.get_transactions(watch.address, limit=limit)

    seen = set(watch.seen_hashes or [])
    fresh = [t for t in txs if t.tx_hash not in seen]
    ordered_hashes = [t.tx_hash for t in txs]
    merged = ordered_hashes + [h for h in (watch.seen_hashes or [])
                               if h not in ordered_hashes]
    merged = merged[:_SEEN_CAP]

    if not watch.seen_hashes and watch.last_checked_at is None:
        # first tick: learn the baseline, alert on nothing
        return WatchCheck(watch_id=watch.id, baseline=True,
                          seen_hashes=merged)

    events: list[WatchEvent] = []
    for t in fresh:
        ins = [p for p in t.inputs if _same(p.address, watch.address)]
        outs = [p for p in t.outputs if _same(p.address, watch.address)]
        if ins and not outs:
            direction, mine, others = "out", ins, [
                p for p in t.outputs if not _same(p.address, watch.address)]
        elif outs:
            direction, mine, others = "in", outs, [
                p for p in t.inputs if not _same(p.address, watch.address)]
        else:
            continue  # address not a party (shouldn't happen)
        counterparty = others[0].address if others else ""
        vasp_hit = vasp_resolver(counterparty) if counterparty else None
        asset = t.asset.symbol or t.asset.contract or t.asset.kind.value
        events.append(WatchEvent(
            watch_id=watch.id, tx_hash=t.tx_hash, direction=direction,
            counterparty=counterparty,
            value=mine[0].value if mine else "",
            asset=asset, vasp_hit=vasp_hit,
            block_time=t.block_time.isoformat() if t.block_time else None,
        ))
    return WatchCheck(watch_id=watch.id, baseline=False, events=events,
                      seen_hashes=merged)


async def process_watch(
    store,
    watch,
    *,
    adapter_factory: Callable[[str], object],
    vasp_resolver: Callable[[str], Optional[str]] = lambda a: None,
    alert_url: str,
    secret: str,
) -> dict:
    """One full watch cycle: check, persist cursor, alert on movements.

    Shared by the worker cron tick and the manual API trigger.
    Returns {"baseline": bool, "events": [...], "delivered": [...]}.
    """
    import uuid
    from dataclasses import replace
    from datetime import datetime, timezone

    from ..store.base import AlertRec

    check = await check_watch(watch, adapter_factory,
                              vasp_resolver=vasp_resolver)
    await store.set_watch(
        watch.id, watch.status, seen_hashes=check.seen_hashes,
        last_checked_at=check.checked_at)
    delivered: list[dict] = []
    for event in check.events:
        view = replace(watch, alert_url=alert_url)
        result = await deliver_watch_alert(event, view, secret=secret)
        await store.record_alert(AlertRec(
            id=uuid.uuid4(), watch_id=watch.id, tx_hash=event.tx_hash,
            direction=event.direction, counterparty=event.counterparty,
            value=event.value, asset=event.asset, vasp_hit=event.vasp_hit,
            delivered=result.ok, created_at=datetime.now(timezone.utc)))
        delivered.append({"tx_hash": event.tx_hash,
                          "delivered": result.ok})
    return {
        "baseline": check.baseline,
        "events": [
            {"tx_hash": e.tx_hash, "direction": e.direction,
             "counterparty": e.counterparty, "value": e.value,
             "asset": e.asset, "vasp_hit": e.vasp_hit}
            for e in check.events
        ],
        "delivered": delivered,
    }
