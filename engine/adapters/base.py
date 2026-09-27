"""Canonical transaction schema + chain adapter interface.

Every chain adapter normalizes into CanonicalTx. The graph engine, hop
classifier, traversal engine, and scoring layers only ever see this shape —
they are fully chain-agnostic.
"""
import asyncio
import os
import ssl
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

import httpx
from pydantic import BaseModel, Field


class Chain(str, Enum):
    ETHEREUM = "ethereum"
    BSC = "bsc"
    POLYGON = "polygon"
    TRON = "tron"
    BITCOIN = "bitcoin"
    SOLANA = "solana"


class AssetKind(str, Enum):
    NATIVE = "native"  # ETH, BNB, TRX, BTC, SOL, MATIC/POL
    TOKEN = "token"    # ERC-20 / TRC-20 / SPL / BEP-20


class Asset(BaseModel):
    kind: AssetKind
    chain: Chain
    symbol: Optional[str] = None
    contract: Optional[str] = None
    decimals: Optional[int] = None


class FlowParty(BaseModel):
    """One side of a value flow. value is a smallest-unit integer as string
    (wei, satoshi, sun, lamports, token base units) — never float."""

    address: str
    value: str = "0"
    # UTXO script type when the adapter knows it (Bitcoin: p2pkh, p2sh,
    # p2wpkh, p2wsh, p2tr; normalized by the adapter). None = unknown or
    # not applicable (account-based chains). Used by the peel classifier's
    # script-type match factor (master plan section 2).
    script_type: Optional[str] = None
    # Deposit-proxy marker for EVM chains (M16): set by
    # decoding.proxies.annotate_deposit_proxies when the output address
    # looks like an exchange deposit proxy (eip1167-minimal-proxy |
    # create2-deployed | eip1167-via-create2). None = not a proxy or
    # not checked (annotation is opt-in enrichment).
    proxy_kind: Optional[str] = None


class DexSwap(BaseModel):
    """One DEX swap decoded from a transaction (M4).

    Produced by engine.decoding either from same-tx transfer pairing
    (method="transfer-pairing", works on adapter data alone) or from Swap
    event logs in the tx receipt (method="event-log", exact amounts).

    Values are smallest-unit integers as strings. *_contract is None for
    the native asset leg (use *_symbol, e.g. "ETH").
    """

    tx_hash: str
    chain: Chain
    trader: str
    router: Optional[str] = None
    dex: Optional[str] = None  # "uniswap-v2" | "uniswap-v3" | ... | "unknown"
    in_contract: Optional[str] = None
    in_symbol: Optional[str] = None
    in_value: str = "0"
    out_contract: Optional[str] = None
    out_symbol: Optional[str] = None
    out_value: str = "0"
    method: str = "transfer-pairing"
    confidence: float = 0.8


class CanonicalTx(BaseModel):
    tx_hash: str
    chain: Chain
    block_number: Optional[int] = None
    block_time: Optional[datetime] = None
    inputs: list[FlowParty] = Field(default_factory=list)
    outputs: list[FlowParty] = Field(default_factory=list)
    asset: Asset
    fee: Optional[str] = None
    raw: dict[str, Any] = Field(default_factory=dict)
    # M4: set by engine/decoding when this tx is (a leg of) a DEX swap.
    # None for every other tx — M1/M2/M3 code paths ignore it.
    dex_swap: Optional[DexSwap] = None


class AdapterError(Exception):
    """Raised when an indexer call fails after retries."""


class RateLimiter:
    """Minimum interval between calls — keeps us inside free-tier limits."""

    def __init__(self, min_interval: float = 0.25):
        self._min_interval = min_interval
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            delay = self._min_interval - (now - self._last)
            if delay > 0:
                await asyncio.sleep(delay)
            self._last = time.monotonic()


class ChainAdapter(ABC):
    """Interface every chain connector implements."""

    chain: Chain

    def __init__(self, api_key: str = "", timeout: float = 30.0):
        self.api_key = api_key
        # NOTE: trust_env=False + explicit proxy. This environment's NO_PROXY
        # contains "[::1]", which crashes httpx's proxy parsing, and direct
        # egress is blocked — outbound must go via the egress proxy.
        # The proxy does TLS interception; its CA is honored via SSL_CERT_FILE
        # (certifi's bundle doesn't include it, so build our own context).
        proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        ssl_ctx = ssl.create_default_context(
            cafile=os.environ.get("SSL_CERT_FILE") or None
        )
        self._client = httpx.AsyncClient(
            timeout=timeout, trust_env=False, proxy=proxy, verify=ssl_ctx
        )
        self._limiter = RateLimiter()

    @abstractmethod
    async def get_transactions(
        self, address: str, limit: int = 100
    ) -> list[CanonicalTx]:
        """Native-asset transfers involving address, newest first."""

    async def get_token_transfers(
        self, address: str, limit: int = 100
    ) -> list[CanonicalTx]:
        """Fungible-token transfers involving address. Default: unsupported."""
        return []

    async def health_check(self) -> bool:
        """True if the indexer is reachable and the key works."""
        return True

    async def _get_json(
        self,
        url: str,
        params: Optional[dict] = None,
        headers: Optional[dict] = None,
        retries: int = 3,
    ) -> Any:
        last_exc: Optional[Exception] = None
        for attempt in range(retries):
            await self._limiter.wait()
            try:
                r = await self._client.get(url, params=params, headers=headers)
                r.raise_for_status()
                return r.json()
            except Exception as exc:  # noqa: BLE001 — retried below
                last_exc = exc
                await asyncio.sleep(2**attempt)
        raise AdapterError(f"GET {url} failed after {retries} tries: {last_exc}")

    async def close(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _ts(value: Any) -> Optional[datetime]:
        try:
            return datetime.fromtimestamp(int(value), tz=timezone.utc)
        except (TypeError, ValueError):
            return None
