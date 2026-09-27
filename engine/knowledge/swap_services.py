"""Swap-service hot-wallet registry (M18).

Curated, publicly-attributed addresses belonging to instant (non-KYC)
crypto swap services — ChangeNOW, FixedFloat, SimpleSwap. A deposit into
one of these addresses is the on-chain fingerprint of a custodial swap:
value leaves the user's control and reappears later, on any chain, with
no on-chain linkage the tracer can follow. That is why the traversal
engine stops at these addresses (terminal "swap-service") and records
the deposit as a cross-chain lead for M21 correlation work.

Scope is deliberately narrow: deterministic, labeled addresses only.
Heuristic "looks like a swap service" detection is out of scope — a
false merge here would attribute an innocent user's funds to a service
flow, which is worse than a miss.

Address verification (2026-09-27): every address below was checked
on-chain the day it was curated —
  * Ethereum entries: eth_getCode == 0x (EOA, as expected for service
    hot wallets) and eth_getTransactionCount > 0 (live, has sent
    transactions), via the Etherscan V2 proxy API — except where noted.
  * BSC entries: same checks via the public BSC RPC
    (https://bsc-dataseed.binance.org/).
Service attribution comes from the explorer's public label (named
source per entry); the on-chain check confirms the address exists,
is an EOA, and is active. It does not independently prove ownership —
the label is the attribution claim, and each entry cites it.

Tiers reflect label strength:
  hot-wallet  (0.90) — explorer label explicitly says "Hot Wallet".
  deposit     (0.85) — explorer label marks it a service deposit
                       address, observed forwarding into a Tier-1 wallet.
  attributed  (0.75) — explorer labels the address with the service
                       name but states no role.

Caveats:
  * Services rotate hot wallets; entries carry as-of dates and the
    registry is a seed, not exhaustive coverage. Absence from this
    registry proves nothing.
  * 0xe2d60cfe…776c is the same EOA key on Ethereum and BSC (key reuse
    across EVM chains). BscScan labels it "ChangeNOW: Hot Wallet"
    (Tier 1); Etherscan labels it only "ChangeNOW 18" (Tier 3), and on
    Ethereum it had never sent a transaction as of 2026-09-27
    (receive-only: one 5.02 USDC inbound observed).
  * ChangeNOW "Hot Wallet 2" (0xa96be652…) had only 4 outbound
    transactions at verification — a young or rotated wallet; the
    explicit label is the attribution, activity level is not a filter.
  * FixedFloat deposit addresses (0xc2cd4b1e…, 0xfa1c172e…, 0x7a128903…)
    each show exactly 1 outbound transaction: the forward into
    "FixedFloat: Hot Wallet 2", matching the classic per-user deposit
    pattern.
  * The 2024 FixedFloat hacker addresses are deliberately excluded —
    attacker-controlled, not service-controlled.
  * No usable public labels were found for Exolix, StealthEX, Godex,
    LetsExchange, SwapSpace, or ChangeHero on 2026-09-27, nor for any
    of these services on Tron or Bitcoin — so the registry covers
    Ethereum and BSC only. Do not fabricate entries for them.
"""
from dataclasses import dataclass

from ..adapters.base import Chain

#: classification confidence per address role (label strength)
ROLE_CONFIDENCE = {
    "hot-wallet": 0.90,
    "deposit": 0.85,
    "attributed": 0.75,
}


@dataclass(frozen=True)
class SwapServiceInfo:
    name: str          # "changenow" | "fixedfloat" | "simpleswap"
    chain: Chain
    address: str       # display address (checksummed as shown by source)
    role: str          # "hot-wallet" | "deposit" | "attributed"
    label: str         # the explorer's public label text, verbatim
    source: str        # named source, e.g. "Etherscan public label"
    source_url: str    # page where the label was observed
    verified: str      # on-chain check performed + date
    as_of: str = "2026-09-27"

    @property
    def confidence(self) -> float:
        return ROLE_CONFIDENCE[self.role]


_SWAP_SERVICES: list[SwapServiceInfo] = [
    # --- Tier 1: explicit "Hot Wallet" labels -------------------------
    SwapServiceInfo(
        "changenow", Chain.ETHEREUM,
        "0xA96Be652A08D9905F15B7FbE2255708709BeCD09",
        "hot-wallet", "ChangeNOW: Hot Wallet 2",
        "Etherscan public label",
        "https://etherscan.io/address/0xa96be652a08d9905f15b7fbe2255708709becd09",
        "EOA (no code), 4 outbound txs via Etherscan V2 proxy 2026-09-27",
    ),
    SwapServiceInfo(
        "changenow", Chain.ETHEREUM,
        "0xEbA88149813BEc1cCcccFDb0daCEFaaa5DE94cB1",
        "hot-wallet", "ChangeNOW: Hot Wallet 4",
        "Etherscan public label",
        "https://etherscan.io/address/0xeba88149813bec1cccccfdb0dacefaaa5de94cb1",
        "EOA (no code), 1312307 outbound txs via Etherscan V2 proxy 2026-09-27",
    ),
    SwapServiceInfo(
        "fixedfloat", Chain.ETHEREUM,
        "0x4E5B2e1dc63F6b91cb6Cd759936495434C7e972F",
        "hot-wallet", "FixedFloat: Hot Wallet 2",
        "Etherscan public label",
        "https://etherscan.io/address/0x4e5b2e1dc63f6b91cb6cd759936495434c7e972f",
        "EOA (no code), 2011956 outbound txs via Etherscan V2 proxy 2026-09-27",
    ),
    SwapServiceInfo(
        "fixedfloat", Chain.BSC,
        "0x4727250679294802377dD6cA6541B8E459077c95",
        "hot-wallet", "FixedFloat: Hot Wallet",
        "BscScan public label (re-verified live 2026-09-27: label, "
        "$4.17M portfolio, txs seconds old)",
        "https://bscscan.com/address/0x4727250679294802377dd6ca6541b8e459077c95",
        "EOA (no code), 2010604 outbound txs via public BSC RPC 2026-09-27",
    ),
    SwapServiceInfo(
        "changenow", Chain.BSC,
        "0xe2d60CFE3cF8B2079C7DF0144c5b28C03469775C",
        "hot-wallet", "ChangeNOW: Hot Wallet",
        "BscScan public label (re-verified live 2026-09-27: label, "
        "$79.7k portfolio, txs minutes old)",
        "https://bscscan.com/address/0xe2d60cfe3cf8b2079c7df0144c5b28c03469775c",
        "EOA (no code), 507469 outbound txs via public BSC RPC 2026-09-27",
    ),
    # --- Tier 2: deposit addresses observed forwarding to Tier 1 ------
    SwapServiceInfo(
        "changenow", Chain.ETHEREUM,
        "0x3f3Ee0a9cAC2d01DB44001eca3E8382fbe40207B",
        "deposit", "ChangeNOW Dep: 0x3f3...07B",
        "Etherscan public label",
        "https://etherscan.io/address/0x3f3Ee0a9cAC2d01DB44001eca3E8382fbe40207B",
        "EOA (no code), 79252 outbound txs via Etherscan V2 proxy 2026-09-27",
    ),
    SwapServiceInfo(
        "fixedfloat", Chain.ETHEREUM,
        "0xc2cd4b1e89FE561702b72FF9329303A2DD0dc225",
        "deposit", "FixedFloat Dep: 0xc2c...225",
        "Etherscan public label (page shows 0.015 ETH in, 0.014475 ETH "
        "forwarded to FixedFloat: Hot Wallet 2)",
        "https://etherscan.io/address/0xc2cd4b1e89FE561702b72FF9329303A2DD0dc225",
        "EOA (no code), 1 outbound tx (the forward) via Etherscan V2 proxy "
        "2026-09-27",
    ),
    SwapServiceInfo(
        "fixedfloat", Chain.ETHEREUM,
        "0xfA1c172E5F8A7dbdE93AC3bcB02ab164eCFa6a2B",
        "deposit", "FixedFloat Dep: 0xfa1...a2b",
        "Etherscan public label (page shows 0.04 ETH in, 0.03999874 ETH "
        "forwarded to FixedFloat: Hot Wallet 2)",
        "https://etherscan.io/address/0xfa1c172e5f8a7dbde93ac3bcb02ab164ecfa6a2b",
        "EOA (no code), 1 outbound tx (the forward) via Etherscan V2 proxy "
        "2026-09-27",
    ),
    SwapServiceInfo(
        "fixedfloat", Chain.ETHEREUM,
        "0x7A128903FbF43cF2b6121994c850a0103fcb5247",
        "deposit", "FixedFloat Dep: 0x7a1...247",
        "Etherscan public label (page shows 42 ETH in, 42 ETH forwarded "
        "to FixedFloat: Hot Wallet 2)",
        "https://etherscan.io/address/0x7A128903FbF43cF2b6121994c850a0103fcb5247",
        "EOA (no code), 1 outbound tx (the forward) via Etherscan V2 proxy "
        "2026-09-27",
    ),
    # --- Tier 3: service-attributed, role unstated --------------------
    SwapServiceInfo(
        "changenow", Chain.ETHEREUM,
        "0x59855c07cdd4924609df4f5f175da3188699932f",
        "attributed", "ChangeNOW 21",
        "Etherscan public label",
        "https://etherscan.io/address/0x59855c07cdd4924609df4f5f175da3188699932f",
        "EOA (no code), 70535 outbound txs via Etherscan V2 proxy 2026-09-27",
    ),
    SwapServiceInfo(
        "simpleswap", Chain.ETHEREUM,
        "0x7bacd3e83522f484bc5128ea93bf7290f1f1b9e5",
        "attributed", "SimpleSwap 7",
        "Etherscan public label (classification: Exchange)",
        "https://etherscan.io/address/0x7bacd3e83522f484bc5128ea93bf7290f1f1b9e5",
        "EOA (no code), 54996 outbound txs via Etherscan V2 proxy 2026-09-27",
    ),
    SwapServiceInfo(
        "changenow", Chain.ETHEREUM,
        "0xe2d60CFE3cF8B2079C7DF0144c5b28C03469775C",
        "attributed", "ChangeNOW 18",
        "Etherscan public label (same EOA key as the BscScan-labeled "
        "ChangeNOW hot wallet above)",
        "https://etherscan.io/address/0xe2d60CFE3cF8B2079C7DF0144c5b28C03469775C",
        "EOA (no code), 0 outbound txs on Ethereum as of 2026-09-27 "
        "(receive-only; 5.02 USDC inbound observed) via Etherscan V2",
    ),
]

# (chain.value, address.lower()) -> SwapServiceInfo
SWAP_SERVICE_ADDRESSES: dict[tuple[str, str], SwapServiceInfo] = {
    (s.chain.value, s.address.lower()): s for s in _SWAP_SERVICES
}


def swap_service_for(chain: Chain, address: str) -> SwapServiceInfo | None:
    """Swap-service lookup — None when the address is not a known one."""
    return SWAP_SERVICE_ADDRESSES.get((chain.value, address.lower()))
