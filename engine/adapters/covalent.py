"""Covalent (GoldRush) adapter — multi-chain EVM fallback.

Etherscan's free tier only covers Ethereum mainnet, so BSC and Polygon go
through Covalent. One key, chain switched via path parameter.
"""
from datetime import datetime
from typing import Any, Optional

from engine.adapters.base import (
    AdapterError,
    Asset,
    AssetKind,
    CanonicalTx,
    Chain,
    ChainAdapter,
    FlowParty,
)

_CHAIN_NAMES: dict[Chain, str] = {
    Chain.ETHEREUM: "eth-mainnet",
    Chain.BSC: "bsc-mainnet",
    Chain.POLYGON: "matic-mainnet",
}

_NATIVE_SYMBOLS: dict[Chain, str] = {
    Chain.ETHEREUM: "ETH",
    Chain.BSC: "BNB",
    Chain.POLYGON: "POL",
}

_BASE_URL = "https://api.covalenthq.com/v1"


class CovalentAdapter(ChainAdapter):
    """GoldRush unified API adapter for one EVM chain."""

    def __init__(self, chain: Chain, api_key: str = "", **kwargs: Any):
        if chain not in _CHAIN_NAMES:
            raise ValueError(f"Covalent adapter: unsupported chain {chain}")
        self.chain = chain
        kwargs.setdefault("timeout", 60.0)  # hot addresses can be slow
        super().__init__(api_key=api_key, **kwargs)
        self._limiter = type(self._limiter)(min_interval=0.35)  # free-tier courtesy

    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    async def _items(self, address: str, limit: int) -> list[dict]:
        data = await self._get_json(
            f"{_BASE_URL}/{_CHAIN_NAMES[self.chain]}/address/{address}/transactions_v3/",
            params={"page-size": min(limit, 100)},
            headers=self._auth(),
        )
        if not isinstance(data, dict):
            raise AdapterError("Covalent: unexpected response shape")
        if data.get("error"):
            raise AdapterError(f"Covalent: {data.get('error_message')}")
        items = (data.get("data") or {}).get("items") or []
        return items[:limit]

    # -- ChainAdapter ------------------------------------------------------
    async def get_transactions(
        self, address: str, limit: int = 100
    ) -> list[CanonicalTx]:
        out = []
        for item in await self._items(address, limit):
            if not item.get("successful", True):
                continue
            to_addr = item.get("to_address") or ""
            out.append(
                CanonicalTx(
                    tx_hash=item.get("tx_hash", ""),
                    chain=self.chain,
                    block_number=item.get("block_height"),
                    block_time=self._parse_time(item.get("block_signed_at")),
                    inputs=[FlowParty(
                        address=item.get("from_address", ""),
                        value=str(item.get("value", "0")),
                    )],
                    outputs=[FlowParty(address=to_addr, value=str(item.get("value", "0")))]
                    if to_addr else [],
                    asset=Asset(
                        kind=AssetKind.NATIVE,
                        chain=self.chain,
                        symbol=_NATIVE_SYMBOLS[self.chain],
                        decimals=18,
                    ),
                    fee=str(item.get("gas_spent", 0) or 0),
                    raw={},
                )
            )
        return out

    async def get_token_transfers(
        self, address: str, limit: int = 100
    ) -> list[CanonicalTx]:
        out = []
        for item in await self._items(address, limit):
            for ev in item.get("log_events") or []:
                decoded = ev.get("decoded") or {}
                if str(decoded.get("name", "")).lower() != "transfer":
                    continue
                params = {str(p.get("name", "")).lower(): p.get("value")
                          for p in decoded.get("params") or []}
                if not all(k in params for k in ("from", "to", "value")):
                    continue
                out.append(
                    CanonicalTx(
                        tx_hash=item.get("tx_hash", ""),
                        chain=self.chain,
                        block_number=item.get("block_height"),
                        block_time=self._parse_time(item.get("block_signed_at")),
                        inputs=[FlowParty(address=str(params["from"]),
                                          value=str(params["value"]))],
                        outputs=[FlowParty(address=str(params["to"]),
                                           value=str(params["value"]))],
                        asset=Asset(
                            kind=AssetKind.TOKEN,
                            chain=self.chain,
                            symbol=ev.get("sender_contract_ticker_symbol"),
                            contract=ev.get("sender_address"),
                            decimals=self._int(ev.get("sender_contract_decimals")),
                        ),
                        raw={},
                    )
                )
                if len(out) >= limit:
                    return out
        return out

    async def health_check(self) -> bool:
        try:
            await self._items("0x000000000000000000000000000000000000dEaD", 1)
            return True
        except AdapterError:
            return False

    @staticmethod
    def _parse_time(value: Any) -> Optional[datetime]:
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _int(value: Any) -> Optional[int]:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
