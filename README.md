# VASP Attribution Engine

Automated attribution of unknown cryptocurrency wallets to the nearest
legally-addressable VASP. Built from `AUTOMATED-VASP-ATTRIBUTION-MASTER-REFERENCE.md`.

## Quickstart

```bash
cp .env.example .env   # fill in indexer API keys
docker compose up -d
# API: http://localhost:8000/docs   Mock SAHYOG: http://localhost:8091
```

Local dev without docker:

```bash
pip install -e ".[dev]"
uvicorn api.main:app --reload
python integrations/sahyog_mock/submit_case.py <address> <chain>
```

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
