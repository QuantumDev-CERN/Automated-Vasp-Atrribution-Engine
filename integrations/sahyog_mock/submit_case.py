"""Submit a test case to the mock SAHYOG server."""
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from api.core.config import settings  # noqa: E402

# SAHYOG_MOCK_URL from .env (dev default only when genuinely unset).
MOCK_URL = settings.sahyog_mock_url


def main() -> None:
    address = sys.argv[1] if len(sys.argv) > 1 else "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
    chain = sys.argv[2] if len(sys.argv) > 2 else "ethereum"
    r = httpx.post(
        f"{MOCK_URL}/sahyog/cases",
        json={
            "fir_number": "FIR/2026/DEMO001",
            "suspect_address": address,
            "chain": chain,
            "officer_id": "demo-officer",
        },
        timeout=10,
    )
    r.raise_for_status()
    print(r.json())


if __name__ == "__main__":
    main()
