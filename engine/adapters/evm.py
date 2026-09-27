"""EVM adapter — Ethereum, BNB Chain, Polygon via the Etherscan V2 unified API.

One API key covers all EVM chains: api.etherscan.io/v2/api?chainid=<id>.

NOTE: the free Etherscan tier only serves Ethereum mainnet ("Free API access
is not supported for this chain" on BSC/Polygon). BSC and Polygon are covered
by the Covalent adapter instead — see engine/adapters/covalent.py.
"""
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

_CHAIN_IDS: dict[Chain, int] = {
    Chain.ETHEREUM: 1,
    Chain.BSC: 56,
    Chain.POLYGON: 137,
}

_NATIVE_SYMBOLS: dict[Chain, str] = {
    Chain.ETHEREUM: "ETH",
    Chain.BSC: "BNB",
    Chain.POLYGON: "POL",
}

_BASE_URL = "https://api.etherscan.io/v2/api"


class EvmAdapter(ChainAdapter):
    """Etherscan V2 adapter for one EVM chain."""

    def __init__(self, chain: Chain, api_key: str = "", **kwargs: Any):
        if chain not in _CHAIN_IDS:
            raise ValueError(f"not an EVM chain: {chain}")
        self.chain = chain
        super().__init__(api_key=api_key, **kwargs)

    # -- raw API -----------------------------------------------------------
    async def _api(self, module: str, action: str, address: str, limit: int) -> list[dict]:
        data = await self._get_json(
            _BASE_URL,
            params={
                "chainid": _CHAIN_IDS[self.chain],
                "module": module,
                "action": action,
                "address": address,
                "startblock": 0,
                "endblock": 99999999,
                "page": 1,
                "offset": limit,
                "sort": "desc",
                "apikey": self.api_key,
            },
        )
        if not isinstance(data, dict) or data.get("status") != "1":
            # "0" with "No transactions found" is a valid empty result
            msg = data.get("message", "") if isinstance(data, dict) else ""
            result = data.get("result", "") if isinstance(data, dict) else ""
            if "no transactions found" in str(result).lower():
                return []
            raise AdapterError(f"Etherscan V2 error: {msg} / {result}")
        return data.get("result", [])

    # -- ChainAdapter ------------------------------------------------------
    async def get_transactions(
        self, address: str, limit: int = 100
    ) -> list[CanonicalTx]:
        raw = await self._api("account", "txlist", address, limit)
        out = []
        for tx in raw:
            if tx.get("isError") == "1":
                continue  # failed txs move no value
            out.append(self._normalize_native(tx))
        return out

    async def get_token_transfers(
        self, address: str, limit: int = 100
    ) -> list[CanonicalTx]:
        raw = await self._api("account", "tokentx", address, limit)
        return [self._normalize_token(tx) for tx in raw]

    async def health_check(self) -> bool:
        try:
            await self._api("account", "txlist",
                            "0x0000000000000000000000000000000000000000", 1)
            return True
        except AdapterError:
            return False

    async def get_transaction_receipt(self, tx_hash: str) -> dict:
        """Full tx receipt (incl. logs) via the Etherscan proxy module.

        Used by engine/decoding for exact Swap-event decoding. Not part of
        the default normalization path — decoding is opt-in post-processing.
        """
        data = await self._get_json(
            _BASE_URL,
            params={
                "chainid": _CHAIN_IDS[self.chain],
                "module": "proxy",
                "action": "eth_getTransactionReceipt",
                "txhash": tx_hash,
                "apikey": self.api_key,
            },
        )
        if not isinstance(data, dict) or not data.get("result"):
            raise AdapterError(f"no receipt for {tx_hash}: {data}")
        return data["result"]

    async def get_code(self, address: str) -> str:
        """Runtime bytecode at `address` via eth_getCode (proxy module).

        Used by engine/decoding/proxies for deposit-proxy detection. Not part
        of the default normalization path — decoding is opt-in
        post-processing. NOTE: the free Etherscan tier only serves Ethereum
        for the proxy module; BSC/Polygon raise AdapterError here.
        """
        data = await self._get_json(
            _BASE_URL,
            params={
                "chainid": _CHAIN_IDS[self.chain],
                "module": "proxy",
                "action": "eth_getCode",
                "address": address,
                "tag": "latest",
                "apikey": self.api_key,
            },
        )
        if not isinstance(data, dict) or "result" not in data:
            raise AdapterError(f"no code for {address}: {data}")
        return data["result"]

    # -- normalization -----------------------------------------------------
    def _normalize_native(self, tx: dict) -> CanonicalTx:
        sender = tx.get("from", "")
        receiver = tx.get("to", "")
        return CanonicalTx(
            tx_hash=tx.get("hash", ""),
            chain=self.chain,
            block_number=self._int(tx.get("blockNumber")),
            block_time=self._ts(tx.get("timeStamp")),
            inputs=[FlowParty(address=sender, value=tx.get("value", "0"))],
            outputs=[FlowParty(address=receiver, value=tx.get("value", "0"))],
            asset=Asset(
                kind=AssetKind.NATIVE,
                chain=self.chain,
                symbol=_NATIVE_SYMBOLS[self.chain],
                decimals=18,
            ),
            fee=self._fee(tx),
            raw={"input": (tx.get("input") or "")[:10]},  # keep only selector
        )

    def _normalize_token(self, tx: dict) -> CanonicalTx:
        return CanonicalTx(
            tx_hash=tx.get("hash", ""),
            chain=self.chain,
            block_number=self._int(tx.get("blockNumber")),
            block_time=self._ts(tx.get("timeStamp")),
            inputs=[FlowParty(address=tx.get("from", ""), value=tx.get("value", "0"))],
            outputs=[FlowParty(address=tx.get("to", ""), value=tx.get("value", "0"))],
            asset=Asset(
                kind=AssetKind.TOKEN,
                chain=self.chain,
                symbol=tx.get("tokenSymbol"),
                contract=tx.get("contractAddress"),
                decimals=self._int(tx.get("tokenDecimal")),
            ),
            raw={},
        )

    @staticmethod
    def _int(value: Any) -> Optional[int]:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _fee(self, tx: dict) -> Optional[str]:
        try:
            return str(int(tx.get("gasUsed", 0)) * int(tx.get("gasPrice", 0)))
        except (TypeError, ValueError):
            return None
