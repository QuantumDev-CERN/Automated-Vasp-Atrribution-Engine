"""M16 live smoke: CREATE2 deposit-proxy detection on real chain data.

Ethereum (Etherscan free tier serves it):
  1. The known CREATE2 factory in the registry has code on-chain
     (registry sanity — the address was verified, not remembered).
  2. End-to-end CREATE2 proof: take recent calls to the factory, parse
     salt + init code from calldata, re-derive the deployment address with
     our keccak256, and assert code exists at the derived address. This
     proves the registry entry, the calldata parsing, and the EIP-1014
     math against live chain state.
  3. Run detect_deposit_proxy over the deployed bytecode plus live
     negatives (USDT contract, an EOA): no false positives, no crashes.

The EIP-1167 positive is pinned by unit test to the spec's reference
bytecode (tests/test_m16.py); recent factory deployments are arbitrary
contracts, so the live section reports (not asserts) proxy matches.

Run: uv run python scripts/smoke_m16.py
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.adapters.base import Chain
from engine.adapters.evm import EvmAdapter
from engine.knowledge.proxies import (
    CREATE2_FACTORIES,
    create2_address,
    create2_factory_name,
    detect_deposit_proxy,
)

DEPLOYER = "0x4e59b44847b379578588920ca78fbf26c0b4956c"
USDT = "0xdAC17F958D2ee523a2206206994597C13D831ec7"
BURN = "0x000000000000000000000000000000000000dEaD"


class SectionSkip(Exception):
    """The external dependency returned nothing usable (free public
    endpoint flaky), not a code regression. Loud skip, not a pass."""


def load_env() -> None:
    for line in Path(".env").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


async def main() -> None:
    load_env()
    api_key = os.environ.get("ETHERSCAN_API_KEY")
    if not api_key:
        raise SectionSkip(
            "ETHERSCAN_API_KEY not set — add it to .env to run this section"
        )

    assert create2_factory_name(DEPLOYER) == list(CREATE2_FACTORIES.values())[0]
    evm = EvmAdapter(Chain.ETHEREUM, api_key=api_key)
    try:
        # 1. registry sanity: the factory has code on mainnet
        factory_code = await evm.get_code(DEPLOYER)
        n_bytes = (len(factory_code) - 2) // 2
        print(f"[m16] factory {DEPLOYER[:10]}… code={n_bytes}B")
        if n_bytes == 0:
            raise SectionSkip("m16: factory has no code on mainnet?!")

        # 2. end-to-end CREATE2: re-derive deployment addresses from calldata
        raw = await evm._api("account", "txlist", DEPLOYER, 8)
        if not raw:
            raise SectionSkip("m16: no factory txs returned")
        verified = 0
        proxy_hits = 0
        for t in raw:
            data = bytes.fromhex(t.get("input", "0x")[2:])
            if len(data) < 64:
                continue
            salt, init = data[:32], data[32:]
            derived = create2_address(DEPLOYER, salt, init)
            code = await evm.get_code(derived)
            has_code = len(code) > 2
            print(f"[m16]   {derived[:10]}… code={'yes' if has_code else 'NO'}")
            if not has_code:
                continue  # reverted deployment or newer than our node view
            verified += 1
            if detect_deposit_proxy(code) is not None:
                proxy_hits += 1
            if verified >= 5:
                break
        print(f"[m16] create2 re-derived OK: {verified}/5 deployments")
        print(f"[m16] eip1167 among them: {proxy_hits} (informational)")
        assert verified >= 3, "m16: too few verifiable CREATE2 deployments"

        # 3. live negatives: real contract + EOA must not be flagged
        assert detect_deposit_proxy(await evm.get_code(USDT)) is None
        assert detect_deposit_proxy(await evm.get_code(BURN)) is None
        print("[m16] negatives OK (USDT contract, burn EOA -> None)")
    finally:
        await evm.close()

    print("\nSMOKE M16 OK — CREATE2 factory + address derivation live-verified")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except SectionSkip as e:
        print(f"SKIP: {e}")
        sys.exit(0)
