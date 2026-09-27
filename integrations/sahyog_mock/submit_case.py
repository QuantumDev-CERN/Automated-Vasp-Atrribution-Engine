"""Submit a test case to the mock SAHYOG server."""
import sys

import httpx

MOCK_URL = "http://localhost:8091"


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
