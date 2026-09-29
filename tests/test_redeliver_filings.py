"""M33 tests: redeliver_filings retries failed SAHYOG deliveries.

Mirrors the worker's delivery semantics: one new filing row per
attempt, report webhook status updated on success, already-delivered
filings untouched.
"""
from types import SimpleNamespace
from uuid import uuid4

import pytest

from engine.delivery.webhook import DeliveryResult
from engine.store.base import FILING_DELIVERED, FILING_FAILED
from scripts.redeliver_filings import redeliver


def _delivery(ok):
    async def fake(report, case, job, **kwargs):
        return DeliveryResult(ok, 1, 200 if ok else 401,
                              error=None if ok else "receiver rejected",
                              ack_ref="MOCK-ABC" if ok else None)
    return fake


class FakeStore:
    def __init__(self, filings):
        self._filings = filings
        self.recorded = []
        self.webhook_status = {}

    async def list_filings(self, *, status=None, limit=50, offset=0):
        rows = [f for f in self._filings
                if status is None or f.status == status]
        return rows[:limit], len(rows)

    async def get_case(self, case_id):
        return SimpleNamespace(id=case_id, fir_number="EVAL/2026/0001")

    async def get_report(self, report_id):
        return SimpleNamespace(id=report_id)

    async def get_latest_job(self, case_id):
        return SimpleNamespace(id=uuid4(), address="0xabc",
                               chain="ethereum")

    async def record_filing(self, filing):
        self.recorded.append(filing)
        return filing

    async def set_webhook_status(self, report_id, status):
        self.webhook_status[report_id] = status


def _failed_filing():
    return SimpleNamespace(id=uuid4(), case_id=uuid4(),
                           report_id=uuid4(), status=FILING_FAILED)


_SETTINGS = SimpleNamespace(sahyog_mock_url="http://mock:8091",
                           engine_webhook_secret="s")


@pytest.mark.asyncio
async def test_redeliver_success_records_delivered(monkeypatch):
    import engine.delivery.webhook as webhook_mod
    monkeypatch.setattr(webhook_mod, "deliver_attribution",
                        _delivery(True))
    filing = _failed_filing()
    store = FakeStore([filing])

    delivered, still_failed = await redeliver(store, _SETTINGS)

    assert (delivered, still_failed) == (1, 0)
    assert len(store.recorded) == 1
    assert store.recorded[0].status == FILING_DELIVERED
    assert store.recorded[0].ack_ref == "MOCK-ABC"
    assert store.webhook_status[filing.report_id] == "delivered"


@pytest.mark.asyncio
async def test_redeliver_failure_records_failed(monkeypatch):
    import engine.delivery.webhook as webhook_mod
    monkeypatch.setattr(webhook_mod, "deliver_attribution",
                        _delivery(False))
    store = FakeStore([_failed_filing()])

    delivered, still_failed = await redeliver(store, _SETTINGS)

    assert (delivered, still_failed) == (0, 1)
    assert store.recorded[0].status == FILING_FAILED
    assert "receiver rejected" in store.recorded[0].error


@pytest.mark.asyncio
async def test_redeliver_dry_run_records_nothing(monkeypatch):
    import engine.delivery.webhook as webhook_mod
    monkeypatch.setattr(webhook_mod, "deliver_attribution",
                        _delivery(True))
    store = FakeStore([_failed_filing()])

    delivered, still_failed = await redeliver(store, _SETTINGS,
                                              dry_run=True)

    assert (delivered, still_failed) == (0, 0)
    assert store.recorded == []


@pytest.mark.asyncio
async def test_redeliver_ignores_delivered_filings(monkeypatch):
    import engine.delivery.webhook as webhook_mod
    monkeypatch.setattr(webhook_mod, "deliver_attribution",
                        _delivery(True))
    already = _failed_filing()
    already.status = FILING_DELIVERED
    store = FakeStore([already])

    delivered, still_failed = await redeliver(store, _SETTINGS)

    assert (delivered, still_failed) == (0, 0)
    assert store.recorded == []
