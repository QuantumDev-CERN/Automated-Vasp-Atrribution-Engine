"""Tron adapter via TronGrid.

Covers native TRX transfers and TRC-20 token transfers (the USDT-TRC20
corridor is one of the highest-volume paths in Indian cases).
"""
import hashlib
from typing import Any, Optional

from engine.adapters.base import (
    Asset,
    AssetKind,
    CanonicalTx,
    Chain,
    ChainAdapter,
    FlowParty,
)

_BASE_URL = "https://api.trongrid.io"

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def tron_hex_to_base58(hex_addr: str) -> str:
    """41… hex (21 bytes) → base58check T… address. No external deps."""
    raw = bytes.fromhex(hex_addr.removeprefix("0x"))
    digest = hashlib.sha256(hashlib.sha256(raw).digest()).digest()[:4]
    num = int.from_bytes(raw + digest, "big")
    enc = ""
    while num > 0:
        num, rem = divmod(num, 58)
        enc = _B58[rem] + enc
    # leading zero bytes → '1's
    pad = 0
    for b in raw + digest:
        if b == 0:
            pad += 1
        else:
            break
    return "1" * pad + enc


def _to_base58(addr: Any) -> str:
    s = str(addr or "")
    if s.startswith("41") and len(s) == 42:
        try:
            return tron_hex_to_base58(s)
        except ValueError:
            return s
    return s


class TronAdapter(ChainAdapter):
    chain = Chain.TRON

    def _headers(self) -> dict[str, str]:
        return {"TRON-PRO-API-KEY": self.api_key} if self.api_key else {}

    # -- ChainAdapter ------------------------------------------------------
    async def get_transactions(
        self, address: str, limit: int = 100
    ) -> list[CanonicalTx]:
        data = await self._get_json(
            f"{_BASE_URL}/v1/accounts/{address}/transactions",
            params={
                "only_confirmed": "true",
                "limit": min(limit, 200),
                "order_by": "block_timestamp,desc",
            },
            headers=self._headers(),
        )
        out = []
        for tx in data.get("data", []) or []:
            norm = self._normalize_native(tx)
            if norm:
                out.append(norm)
        return out

    async def get_token_transfers(
        self, address: str, limit: int = 100
    ) -> list[CanonicalTx]:
        data = await self._get_json(
            f"{_BASE_URL}/v1/accounts/{address}/transactions/trc20",
            params={
                "only_confirmed": "true",
                "limit": min(limit, 200),
                "order_by": "block_timestamp,desc",
            },
            headers=self._headers(),
        )
        out = []
        for t in data.get("data", []) or []:
            info = t.get("token_info") or {}
            out.append(
                CanonicalTx(
                    tx_hash=t.get("transaction_id", ""),
                    chain=Chain.TRON,
                    block_number=self._int(t.get("block_number")),
                    block_time=self._ts_ms(t.get("block_timestamp")),
                    inputs=[FlowParty(address=t.get("from", ""), value=str(t.get("value", "0")))],
                    outputs=[FlowParty(address=t.get("to", ""), value=str(t.get("value", "0")))],
                    asset=Asset(
                        kind=AssetKind.TOKEN,
                        chain=Chain.TRON,
                        symbol=info.get("symbol"),
                        contract=info.get("address"),
                        decimals=self._int(info.get("decimals")),
                    ),
                    raw={},
                )
            )
        return out

    async def health_check(self) -> bool:
        try:
            data = await self._get_json(
                f"{_BASE_URL}/v1/accounts/TJCnKsPa7y5okkXvQAidZBzqx3QyQ6sxMW",
                headers=self._headers(),
            )
            return isinstance(data, dict)
        except Exception:  # noqa: BLE001
            return False

    # -- normalization -----------------------------------------------------
    def _normalize_native(self, tx: dict) -> Optional[CanonicalTx]:
        try:
            contracts = (tx.get("raw_data") or {}).get("contract") or []
            if not contracts:
                return None
            c = contracts[0]
            if c.get("type") != "TransferContract":
                return None  # TRX transfers only here; TRC-20 via get_token_transfers
            v = c.get("parameter", {}).get("value", {}) or {}
            amount_sun = str(v.get("amount", 0))
            return CanonicalTx(
                tx_hash=tx.get("txID", ""),
                chain=Chain.TRON,
                block_number=self._int(tx.get("blockNumber")),
                block_time=self._ts_ms(tx.get("block_timestamp")),
                inputs=[FlowParty(address=_to_base58(v.get("owner_address")), value=amount_sun)],
                outputs=[FlowParty(address=_to_base58(v.get("to_address")), value=amount_sun)],
                asset=Asset(kind=AssetKind.NATIVE, chain=Chain.TRON,
                            symbol="TRX", decimals=6),
                fee=str(tx.get("fee", 0) or 0),
                raw={},
            )
        except Exception:  # noqa: BLE001 — skip malformed txs
            return None

    @staticmethod
    def _int(value: Any) -> Optional[int]:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _ts_ms(value: Any) -> Any:
        try:
            return ChainAdapter._ts(int(value) // 1000)
        except (TypeError, ValueError):
            return None
