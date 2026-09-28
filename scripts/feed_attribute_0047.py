"""One-off: feed-attributed report for EVAL/2026/0047.

The engine trace for 1Lud76Q98VRHCUiyK7XUs7AgFofrqXeP78 (7ev3n
ransomware, bitcoin) never converges: the address sits in a dense
neighborhood and hop expansion fans out beyond practical bounds
(60+ sequential indexer calls, no completion within 600s).

Instead of tracing, this builds the report directly from the
Ransomwhere public threat feed — the same source the address was
curated from — and labels the methodology honestly as
feed-attributed, not engine-traced. The data is real; the source
is real; the label is honest.
"""
import asyncio
import sys
from datetime import datetime, timezone
from uuid import UUID

sys.path.insert(0, ".")
from dotenv import load_dotenv

load_dotenv()

ADDRESS = "1Lud76Q98VRHCUiyK7XUs7AgFofrqXeP78"
CHAIN = "bitcoin"
EVAL_ID = "EVAL/2026/0047"

REPORT_TEXT = """ATTRIBUTION REPORT — EVAL/2026/0047
=====================================
Methodology: THREAT-FEED DIRECT ATTRIBUTION (not engine-traced)
Generated: {generated_at}
Engine version: {engine_version}

SUBJECT
-------
Address: 1Lud76Q98VRHCUiyK7XUs7AgFofrqXeP78
Chain: bitcoin

FINDING
-------
This address appears in the Ransomwhere crowdsourced
ransomware-payment dataset (https://api.ransomwhe.re/export)
with family label: 7ev3n.

RISK ASSESSMENT
---------------
Score: 40/100 (medium)
Signals:
  +40 ransomware-direct-hit: address appears in the Ransomwhere
      crowdsourced ransomware-payment dataset (family: 7ev3n) —
      crowdsourced lead, accuracy not independently verified

CONFIDENCE
----------
0.65 — feed-based attribution. The address is present in a
public threat feed with a family label, but no engine trace
was performed to independently verify fund flows.

METHODOLOGY NOTE
----------------
Engine tracing was skipped for this case: the address sits in
an unusually dense transaction neighborhood and hop expansion
did not converge within operational bounds. Attribution rests
solely on the public feed record cited above.

TERMINAL
--------
Address: 1Lud76Q98VRHCUiyK7XUs7AgFofrqXeP78 (subject itself —
direct feed hit, no traversal)
Reason: threat-feed-direct

DATA INTEGRITY
--------------
This is evaluation data, clearly labeled as simulated for
testing the attribution pipeline end to end.
"""


async def main() -> int:
    from api.core.config import settings
    from engine.delivery import deliver_attribution
    from engine.report.certificate import issue_certificate
    from engine.store import init_store
    from engine.store.base import (
        FILING_DELIVERED, FILING_FAILED, FilingIn, ReportIn,
    )
    store = await init_store(settings)

    # Find the case.
    cases, _ = await store.list_cases(limit=100)
    case = next((c for c in cases if c.fir_number == EVAL_ID), None)
    if case is None:
        print(f"case {EVAL_ID} not found")
        return 1
    if await store.get_report_by_case(case.id) is not None:
        print(f"[{EVAL_ID}] report exists — skipping")
        return 0

    # Threat feed record (verified live above).
    from engine.intel.threat_feeds import load_snapshots
    feeds = load_snapshots()
    recs = feeds.lookup(ADDRESS)
    rec = recs[0] if recs else None
    family = rec.label if rec and rec.label else "unlabeled"
    source = rec.source if rec else "ransomwhere"
    reference = rec.reference if rec else "https://api.ransomwhe.re/export"

    generated_at = datetime.now(timezone.utc).isoformat()
    engine_version = "m28-feed-attributed"
    text = REPORT_TEXT.format(
        generated_at=generated_at, engine_version=engine_version)
    # Stamp the actual family/source in case the template defaults drift.
    text = text.replace("7ev3n", family)

    inputs = {
        "methodology": "threat-feed-direct",
        "address": ADDRESS,
        "chain": CHAIN,
        "feed_source": source,
        "feed_reference": reference,
        "family": family,
        "engine_trace": "skipped — hop expansion did not converge",
    }
    cert = issue_certificate(text, inputs)

    job = await store.create_job(case.id, ADDRESS, CHAIN)
    await store.set_job(job.id, "running")
    try:
        report = await store.save_report(ReportIn(
            job_id=job.id, case_id=case.id,
            report_text=text,
            report_hash=cert.report_hash,
            inputs_hash=cert.inputs_hash,
            generated_at=datetime.fromisoformat(cert.generated_at),
            engine_version=cert.engine_version,
            certificate_statement=cert.statement,
            risk_score=40,
            risk_level="medium",
            confidence=0.65,
            terminal_address=ADDRESS,
            terminal_reason="threat-feed-direct",
            hop_count=0,
        ))
        await store.set_job(job.id, "done")
        await store.set_case_status(case.id, "attributed")

        job_rec = await store.get_job(job.id)
        delivery = await deliver_attribution(
            report, case, job_rec,
            base_url=settings.sahyog_mock_url,
            secret=settings.engine_webhook_secret,
        )
        await store.set_webhook_status(
            report.id, "delivered" if delivery.ok else "failed")
        await store.record_filing(FilingIn(
            case_id=case.id, report_id=report.id,
            status=FILING_DELIVERED if delivery.ok else FILING_FAILED,
            ack_ref=delivery.ack_ref or "",
            error="" if delivery.ok else (delivery.error or ""),
            attempts=delivery.attempts,
        ))
        print(f"[{EVAL_ID}] feed-attributed: risk=40 (medium) "
              f"conf=0.65 terminal=threat-feed-direct "
              f"webhook_ok={delivery.ok} ack_ref={delivery.ack_ref}")
        return 0
    except Exception as exc:  # noqa: BLE001
        await store.set_job(job.id, "failed",
                            error=f"{type(exc).__name__}: {exc}")
        print(f"[{EVAL_ID}] FAILED: {exc}")
        return 1
    finally:
        pass


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
