"""M32 regression test: collect_population must unpack the
(tuple[list[CaseRec], int]) return of store.list_cases.

M30 shipped `cases = await store.list_cases(...)` and iterated the
tuple itself — the first element is a list, so every seed run crashed
its end-of-run summary with 'list' object has no attribute
'fir_number'. This test pins the fix against MemoryStore (same
contract as Postgres).
"""
import pytest

from engine.store.base import CaseIn
from engine.store.memory import MemoryStore
from scripts.population_report import collect_population, format_summary


class _StubGraphStore:
    async def case_stats(self, case_id: str):
        return {"transactions": 12, "addresses": 7}


@pytest.mark.asyncio
async def test_collect_population_unpacks_list_cases_tuple():
    store = MemoryStore()
    await store.create_case(CaseIn(
        fir_number="EVAL/2026/0001", suspect_address="0xabc",
        chain="ethereum", officer_id="t"))
    await store.create_case(CaseIn(
        fir_number="FIR/2026/9999", suspect_address="0xdef",
        chain="ethereum", officer_id="t"))

    rows, summary = await collect_population(store, _StubGraphStore())

    # Only the EVAL case is counted; the non-EVAL case is ignored.
    assert summary["cases"] == 1
    assert summary["no_report"] == 1  # no report saved for it
    assert len(rows) == 1
    assert "EVAL/2026/0001" in rows[0]["line"]
    # format_summary must render without raising.
    text = format_summary(summary)
    assert "cases: 1" in text
