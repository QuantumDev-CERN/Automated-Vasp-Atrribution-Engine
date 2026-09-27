# Mock SAHYOG

Until real SAHYOG API access exists, this stub plays both sides:

- `mock_server.py` — fake SAHYOG portal: accepts case submissions at
  `POST /sahyog/cases`, receives engine webhook callbacks at
  `POST /sahyog/webhook/attribution`.
- `submit_case.py` — `python submit_case.py <address> <chain>` to file a test case.

Our engine (M7) will mirror this contract on its own intake API and push
results to the webhook endpoint here.
