"""Bulk evaluation seed (M27).

Populates the console with REAL data through the REAL backend: 16
evaluation cases traced by the actual pipeline (worker.trace_wallet —
the same function the arq worker runs) plus 10 watchlist subscriptions
with a baseline check cycle each. The API then serves everything to
the frontend; nothing is fabricated, no frontend fixtures.

Every case is explicitly labeled EVALUATION — fir_number
EVAL/2026/NNNN, notes carrying the public source + provenance and the
disclaimer "not a real investigation". Threat-feed hits, risk scores,
terminals and filings all come out of the engine.

Idempotent: stable evaluation identifiers. Re-running skips cases that
already have a report and watches that already exist; a case whose
latest trace failed is retried with a fresh job.

Infra: Postgres is REQUIRED (a memory-store seed would be invisible
to the API — the script refuses instead of pretending). Neo4j is
used when reachable; otherwise graph pages stay empty and the script
says so loudly. Indexer API keys come from .env like everything else.

Run from the repository root:
    uv run python scripts/seed_demo.py --yes
    uv run python scripts/seed_demo.py --dry-run   # show the plan only
    SEED_DEMO=1 uv run python scripts/seed_demo.py # env opt-in
    uv run python scripts/seed_demo.py --yes --cases EVAL/2026/0001,EVAL/2026/0009
"""
import argparse
import asyncio
import os
import sys
import time
from pathlib import Path
from uuid import UUID

REPO_ROOT = Path(__file__).resolve().parent.parent
os.chdir(REPO_ROOT)  # Pydantic's .env path is relative to the CWD
sys.path.insert(0, str(REPO_ROOT))

EVAL = ("EVALUATION SEED — not a real investigation, not a real FIR. "
        "Subject is a public address from a public threat-intel feed, "
        "traced by the engine for demonstration.")

# ---------------------------------------------------------------- cases
# source: exact public provenance for each subject. 8 ScamSniffer ETH
# phishing/scam addresses (snapshot data/threat_feeds/scamsniffer-*.json,
# live history verified 2026-09-28), 6 Ransomwhere BTC ransomware-payment
# addresses (snapshot data/threat_feeds/ransomwhere-*.json, activity
# verified via mempool.space 2026-09-28), 2 real Tornado Cash 1 ETH pool
# depositors (selector 0xb214faa5 verified empirically 2026-09-28 —
# never seed the pool itself: it traces to a dead end).
CASES: list[dict] = [
    dict(eval_id="EVAL/2026/0001", address="0xbf28eb4e017e472f9f6c11efabcdcb97f5761707",
         chain="ethereum", label="ScamSniffer phishing/scam address",
         source="ScamSniffer scam-database blacklist/all.json snapshot 2026-09-28"),
    dict(eval_id="EVAL/2026/0002", address="0x22af344920a302ae1edfc8bfd10a44d395723e46",
         chain="ethereum", label="ScamSniffer phishing/scam address",
         source="ScamSniffer scam-database blacklist/all.json snapshot 2026-09-28"),
    dict(eval_id="EVAL/2026/0003", address="0x000000009b0e0800549dba4210a24eabbc93f9ef",
         chain="ethereum", label="ScamSniffer phishing/scam address",
         source="ScamSniffer scam-database blacklist/all.json snapshot 2026-09-28"),
    dict(eval_id="EVAL/2026/0004", address="0xe5100c85dc221758d9ceba53cc0e097ccb836ed0",
         chain="ethereum", label="ScamSniffer phishing/scam address",
         source="ScamSniffer scam-database blacklist/all.json snapshot 2026-09-28"),
    dict(eval_id="EVAL/2026/0005", address="0x211bda5034fa0f01948f91671d77e814c4ceab59",
         chain="ethereum", label="ScamSniffer phishing/scam address",
         source="ScamSniffer scam-database blacklist/all.json snapshot 2026-09-28"),
    dict(eval_id="EVAL/2026/0006", address="0x1d5071048370df50839c8879cdf5144ace4b3b3b",
         chain="ethereum", label="ScamSniffer phishing/scam address",
         source="ScamSniffer scam-database blacklist/all.json snapshot 2026-09-28"),
    dict(eval_id="EVAL/2026/0007", address="0xe52331cac0cec1d6b7f0032500fb6aa52c9a3ae6",
         chain="ethereum", label="ScamSniffer phishing/scam address",
         source="ScamSniffer scam-database blacklist/all.json snapshot 2026-09-28"),
    dict(eval_id="EVAL/2026/0008", address="0x4f44bd9853737e37668a91963e1b20a0ef52e30c",
         chain="ethereum", label="ScamSniffer phishing/scam address",
         source="ScamSniffer scam-database blacklist/all.json snapshot 2026-09-28"),
    dict(eval_id="EVAL/2026/0009", address="17TMc2UkVRSga2yYvuxSD9Q1XyB2EPRjTF",
         chain="bitcoin", label="Ransomwhere: Netwalker/Mailto ransomware payment",
         source="Ransomwhere api.ransomwhe.re/export snapshot 2026-09-28 (crowdsourced)"),
    dict(eval_id="EVAL/2026/0010", address="1DUBrMcH9T13oFSa59jxtFDM5eWTP8v2yc",
         chain="bitcoin", label="Ransomwhere: Ako ransomware payment",
         source="Ransomwhere api.ransomwhe.re/export snapshot 2026-09-28 (crowdsourced)"),
    dict(eval_id="EVAL/2026/0011", address="1FEsU4nL3WBG4YWmzR9BwKtdn9ALqobWJ3",
         chain="bitcoin", label="Ransomwhere: HC6/HC7 ransomware payment",
         source="Ransomwhere api.ransomwhe.re/export snapshot 2026-09-28 (crowdsourced)"),
    dict(eval_id="EVAL/2026/0012", address="1Bq6UcakLLKHQihttwQQjaoSHFci9wGTHu",
         chain="bitcoin", label="Ransomwhere: Locky ransomware payment",
         source="Ransomwhere api.ransomwhe.re/export snapshot 2026-09-28 (crowdsourced)"),
    dict(eval_id="EVAL/2026/0013", address="1BgSZRc3bPD8JfhrcXLSyLqBxaLT8UyTqZ",
         chain="bitcoin", label="Ransomwhere: Locky ransomware payment",
         source="Ransomwhere api.ransomwhe.re/export snapshot 2026-09-28 (crowdsourced)"),
    dict(eval_id="EVAL/2026/0014", address="1GXNfLB4my7F2Xck9Xhy3x6SnVkgMGoZFQ",
         chain="bitcoin", label="Ransomwhere: Locky ransomware payment",
         source="Ransomwhere api.ransomwhe.re/export snapshot 2026-09-28 (crowdsourced)"),
    dict(eval_id="EVAL/2026/0015", address="0xb97ef6609fb8b61611c1bcdb476a01af229cd617",
         chain="ethereum", label="Tornado Cash 1 ETH pool depositor (real deposit tx)",
         source="derived on-chain 2026-09-28: deposit selector 0xb214faa5 into "
                "0x47CE0C6eD5B0Ce3d3A51fdb1C52DC66a7c3c2936"),
    dict(eval_id="EVAL/2026/0016", address="0x9f5b7528dd288f4058672e9f6134e43857a009c3",
         chain="ethereum", label="Tornado Cash 1 ETH pool depositor (real deposit tx)",
         source="derived on-chain 2026-09-28: deposit selector 0xb214faa5 into "
                "0x47CE0C6eD5B0Ce3d3A51fdb1C52DC66a7c3c2936"),
]

# --------------------------------------------------------------- watches
# classification is operator-set vocabulary (suspect|terminal|
# counterparty|vasp|general) — never inferred by the engine.
WATCHES: list[dict] = [
    dict(address="0xbf28eb4e017e472f9f6c11efabcdcb97f5761707", chain="ethereum",
         label="eval: ScamSniffer suspect", classification="suspect",
         case_eval_id="EVAL/2026/0001"),
    dict(address="0x22af344920a302ae1edfc8bfd10a44d395723e46", chain="ethereum",
         label="eval: ScamSniffer suspect", classification="suspect",
         case_eval_id="EVAL/2026/0002"),
    dict(address="0xe5100c85dc221758d9ceba53cc0e097ccb836ed0", chain="ethereum",
         label="eval: ScamSniffer suspect", classification="suspect",
         case_eval_id="EVAL/2026/0004"),
    dict(address="17TMc2UkVRSga2yYvuxSD9Q1XyB2EPRjTF", chain="bitcoin",
         label="eval: Netwalker ransom wallet", classification="suspect",
         case_eval_id="EVAL/2026/0009"),
    dict(address="1DUBrMcH9T13oFSa59jxtFDM5eWTP8v2yc", chain="bitcoin",
         label="eval: Ako ransom wallet", classification="suspect",
         case_eval_id="EVAL/2026/0010"),
    dict(address="1FEsU4nL3WBG4YWmzR9BwKtdn9ALqobWJ3", chain="bitcoin",
         label="eval: HC6/HC7 ransom wallet", classification="suspect",
         case_eval_id="EVAL/2026/0011"),
    dict(address="0xb97ef6609fb8b61611c1bcdb476a01af229cd617", chain="ethereum",
         label="eval: Tornado 1 ETH depositor", classification="suspect",
         case_eval_id="EVAL/2026/0015"),
    dict(address="0x9f5b7528dd288f4058672e9f6134e43857a009c3", chain="ethereum",
         label="eval: Tornado 1 ETH depositor", classification="suspect",
         case_eval_id="EVAL/2026/0016"),
    dict(address="0xA96Be652A08D9905F15B7FbE2255708709BeCD09", chain="ethereum",
         label="eval: ChangeNOW Hot Wallet 2 (Etherscan label)",
         classification="counterparty", case_eval_id=None),
    dict(address="0x4E5B2e1dc63F6b91cb6Cd759936495434C7e972F", chain="ethereum",
         label="eval: FixedFloat Hot Wallet 2 (Etherscan label)",
         classification="counterparty", case_eval_id=None),
]

MAX_CONCURRENCY = 3
MAX_ATTEMPTS = 3


async def _find_case(store, eval_id):
    recs, _ = await store.list_cases(search=eval_id, limit=50)
    for r in recs:
        if r.fir_number == eval_id:
            return r
    return None


async def _seed_case(ctx, spec, sem):
    from worker import trace_wallet

    store = ctx["store"]
    async with sem:
        case = await _find_case(store, spec["eval_id"])
        if case is None:
            from engine.store.base import CaseIn
            case = await store.create_case(CaseIn(
                fir_number=spec["eval_id"],
                suspect_address=spec["address"],
                chain=spec["chain"],
                officer_id="evaluation-seed",
                notes=f"{spec['label']}. Source: {spec['source']}. {EVAL}"))
            print(f"[{spec['eval_id']}] case created", flush=True)
        else:
            print(f"[{spec['eval_id']}] case exists", flush=True)

        if await store.get_report_by_case(case.id) is not None:
            print(f"[{spec['eval_id']}] report exists — skipping trace",
                  flush=True)
            return ("skipped", spec["eval_id"])

        last_err = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            job = await store.create_job(case.id, spec["address"],
                                         spec["chain"])
            try:
                out = await trace_wallet(
                    ctx, job_id=str(job.id), case_id=str(case.id),
                    address=spec["address"], chain=spec["chain"])
                report = await store.get_report(UUID(out["report_id"]))
                print(f"[{spec['eval_id']}] traced: risk={report.risk_score} "
                      f"({report.risk_level}) conf={report.confidence} "
                      f"terminal={report.terminal_reason} "
                      f"webhook_ok={out['webhook_ok']}", flush=True)
                return ("traced", spec["eval_id"])
            except Exception as exc:  # noqa: BLE001 — retried, then reported
                last_err = f"{type(exc).__name__}: {exc}"
                print(f"[{spec['eval_id']}] attempt {attempt} failed: "
                      f"{last_err}", flush=True)
                await asyncio.sleep(10 * attempt)
        print(f"[{spec['eval_id']}] FAILED after {MAX_ATTEMPTS} attempts: "
              f"{last_err}", flush=True)
        return ("failed", spec["eval_id"])


async def _seed_watches(ctx, case_ids: dict[str, UUID]):
    from engine.jobs.pipeline import make_adapter
    from engine.store.base import WatchIn
    from engine.watch.watcher import process_watch
    from api.core.config import settings

    store = ctx["store"]
    existing = await store.list_watches(active_only=False)
    known = {(w.address.lower(), w.chain) for w in existing}
    for spec in WATCHES:
        key = (spec["address"].lower(), spec["chain"])
        if key in known:
            print(f"[watch] exists: {spec['label']}", flush=True)
            continue
        rec = await store.add_watch(WatchIn(
            address=spec["address"], chain=spec["chain"],
            label=spec["label"], classification=spec["classification"],
            case_id=case_ids.get(spec["case_eval_id"] or ""),
            created_by="evaluation-seed"))
        # one baseline check so the detail page has real check history
        # (first tick learns the baseline and alerts on nothing)
        try:
            out = await process_watch(
                store, rec, adapter_factory=make_adapter,
                alert_url=(settings.sahyog_mock_url.rstrip("/")
                           + "/sahyog/webhook/watch-alert"),
                secret=settings.engine_webhook_secret)
            print(f"[watch] created + baseline: {spec['label']} "
                  f"(baseline={out['baseline']})", flush=True)
        except Exception as exc:  # noqa: BLE001 — watch exists regardless
            print(f"[watch] created, baseline check failed: {spec['label']}: "
                  f"{type(exc).__name__}: {exc}", flush=True)


async def _amain(args) -> int:
    from api.core.config import settings
    from engine.graph import get_graph_store
    from engine.store import init_store
    from engine.store.memory import MemoryStore
    from worker import _load_sanctions, _load_threat_feeds

    wanted = set(args.cases.split(",")) if args.cases else None
    specs = [c for c in CASES if wanted is None or c["eval_id"] in wanted]
    if not specs:
        print("no cases match --cases filter")
        return 1

    if args.dry_run:
        print("plan (dry run — nothing written, no infra needed):")
        for c in specs:
            print(f"  {c['eval_id']}  {c['chain']:9s} {c['address']}  "
                  f"{c['label']}")
        for w in WATCHES:
            print(f"  watch   {w['chain']:9s} {w['address']}  {w['label']}")
        return 0

    t0 = time.time()
    store = await init_store(settings)
    if isinstance(store, MemoryStore):
        print("REFUSING: the store backend is memory (process-local) — a "
              "seed written here would be invisible to the API. Start "
              "Postgres (docker compose up -d) or set STORE_BACKEND=postgres "
              "with DATABASE_URL pointing at a live database.")
        return 2
    print(f"store: {type(store).__name__} (shared with the API)")

    graph_store = get_graph_store()
    print(f"graph store: {graph_store.backend}"
          + ("" if graph_store.backend == "neo4j"
             else " — graph pages will stay EMPTY until Neo4j is up"))

    ctx = {
        "settings": settings,
        "store": store,
        "sanctions": _load_sanctions(settings),
        "threat_feeds": _load_threat_feeds(),
        "graph_store": graph_store,
    }

    sem = asyncio.Semaphore(MAX_CONCURRENCY)
    results = await asyncio.gather(
        *(_seed_case(ctx, c, sem) for c in specs))

    case_ids: dict[str, UUID] = {}
    for c in specs:
        case = await _find_case(store, c["eval_id"])
        if case is not None:
            case_ids[c["eval_id"]] = case.id
    await _seed_watches(ctx, case_ids)

    n_traced = sum(1 for s, _ in results if s == "traced")
    n_skipped = sum(1 for s, _ in results if s == "skipped")
    n_failed = sum(1 for s, _ in results if s == "failed")
    print(f"done in {time.time() - t0:.0f}s: {n_traced} traced, "
          f"{n_skipped} skipped (already seeded), {n_failed} failed")

    engine = getattr(store, "engine", None)
    if engine is not None:
        await engine.dispose()
    await graph_store.close()
    return 1 if n_failed else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--yes", action="store_true",
                    help="actually write to the database")
    ap.add_argument("--dry-run", action="store_true",
                    help="show the plan without writing anything")
    ap.add_argument("--cases", default="",
                    help="comma-separated eval IDs to seed "
                         "(default: all 16)")
    args = ap.parse_args()
    if not args.yes and not args.dry_run \
            and os.environ.get("SEED_DEMO") != "1":
        print(__doc__)
        print("refusing: this writes to your database — pass --yes, "
              "--dry-run, or SEED_DEMO=1")
        return 2
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
