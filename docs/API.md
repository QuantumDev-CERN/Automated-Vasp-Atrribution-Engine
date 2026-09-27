# API contract

Full interactive docs at `/docs` when the API runs. This file tracks the
contract the future frontend will build against (fleshed out in M7).

## Planned endpoints (M7)

- `POST /cases` → `{case_id, job_id}` — submit suspect wallet, async
- `GET /cases/{case_id}` → case + trace status
- `GET /cases/{case_id}/attribution` → attribution result, path, confidence, risk
- `GET /cases/{case_id}/report` → investigation-ready report (JSON/PDF)
- `POST /webhooks/sahyog` — SAHYOG pushes case updates here
- `GET /vasps` / `GET /vasps/{id}` — VASP directory
- `POST /watchlist` — subscribe to address alerts (phase 2)
