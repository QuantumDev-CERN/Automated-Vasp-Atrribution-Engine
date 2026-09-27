# VASP Attribution Engine

Automated attribution of unknown cryptocurrency wallets to the nearest
legally-addressable VASP. Built from `AUTOMATED-VASP-ATTRIBUTION-MASTER-REFERENCE.md`.

## Quickstart

```bash
cp .env.example .env   # fill in indexer API keys
docker compose up -d
# API: http://localhost:8000/docs   Mock SAHYOG: http://localhost:8091
```

Local dev without docker (API + worker fall back to in-memory store/queue):

```bash
uv sync --extra dev
uv run python integrations/sahyog_mock/submit_case.py <address> <chain>
# or: .venv/bin/python scripts/smoke_adapters.py
```

M7 async flow with docker (Redis queue + Postgres persistence + worker):

```bash
docker compose up -d          # postgres, redis, api, worker, sahyog-mock
# submit a case ->  POST http://localhost:8000/cases
# start a trace ->  POST http://localhost:8000/jobs/trace   (202 + job_id)
# poll status   ->  GET  http://localhost:8000/jobs/{job_id}
# read report   ->  GET  http://localhost:8000/reports/{report_id}
# the worker pushes the finished attribution to the mock SAHYOG webhook
# (see mock inbox: GET http://localhost:8091/sahyog/webhooks)
```

Env knobs (`.env`): `STORE_BACKEND=auto|postgres|memory`,
`QUEUE_BACKEND=auto|redis|memory`, `SANCTIONS_TABLE_PATH` (full OFAC
SDN Advanced XML; empty = vendored fixture sample).

## Layout

- `api/` — FastAPI service (intake, jobs, VASP directory, reports)
- `engine/` — chain-agnostic tracing engine (adapters → graph → classify → traverse → score)
- `worker/` — arq background jobs
- `vasp_directory/` — FIU-IND seed data + legal routing
- `reports/` — report generator + evidentiary certificates
- `integrations/sahyog_mock/` — mock SAHYOG portal until real API access
- `docs/` — architecture + API contract

## Milestones

M0 scaffold → M1 EVM+Tron adapters → M2 BTC+Solana → M3 graph/classifier/traversal →
M4 swaps/bridges/mixers → M5 VASP directory → M6 scoring/reports → M7 async+webhooks
