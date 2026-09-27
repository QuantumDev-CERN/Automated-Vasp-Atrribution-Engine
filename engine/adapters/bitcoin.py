"""Bitcoin adapter via mempool.space (free, no key).

Why not Blockchair: its keyless tier answers HTTP 430 ("IP temporarily
blacklisted") from shared egress IPs, so it can't be relied on without a
key. mempool.space needs no key and returns full transactions —
GET /api/address/{address}/txs gives vin (with prevout) + vout, newest first.
"""
from typing import Any, Optional

from .base import (
    Asset,
    AssetKind,
    CanonicalTx,
    Chain,
    ChainAdapter,
    FlowParty,
    RateLimiter,
)

_MEMPOOL = "https://mempool.space/api"
_PAGE_SIZE = 25  # mempool.space fixed page size for /address/{addr}/txs
_BTC = Asset(kind=AssetKind.NATIVE, chain=Chain.BITCOIN, symbol="BTC", decimals=8)


class BitcoinAdapter(ChainAdapter):
    """UTXO normalization: every input/output becomes a FlowParty.

    Values are satoshis (int) as strings. Coinbase inputs are labeled
    "coinbase" with value = sum of outputs. Outputs with no address
    (OP_RETURN / nonstandard) are skipped — they have no addressable party.
    """

    chain = Chain.BITCOIN

    def __init__(self, timeout: float = 30.0):
        super().__init__(timeout=timeout)
        self._limiter = RateLimiter(0.5)  # stay polite on the free endpoint

    async def get_transactions(
        self, address: str, limit: int = 25
    ) -> list[CanonicalTx]:
        txs: list[CanonicalTx] = []
        after_txid: Optional[str] = None
        while len(txs) < limit:
            params = {"after_txid": after_txid} if after_txid else None
            page = await self._get_json(
                f"{_MEMPOOL}/address/{address}/txs", params=params
            )
            if not page:
                break
            for raw in page:
                tx = self._normalize(raw)
                if tx is not None:
                    txs.append(tx)
                    if len(txs) >= limit:
                        break
            if len(page) < _PAGE_SIZE:  # last page
                break
            after_txid = page[-1]["txid"]
        return txs[:limit]

    def _normalize(self, raw: dict[str, Any]) -> Optional[CanonicalTx]:
        outputs: list[FlowParty] = []
        total_out = 0
        for vout in raw.get("vout", []):
            addr = vout.get("scriptpubkey_address")
            if not addr:
                continue
            value = int(vout.get("value") or 0)
            total_out += value
            outputs.append(FlowParty(address=addr, value=str(value)))

        inputs: list[FlowParty] = []
        for vin in raw.get("vin", []):
            if vin.get("is_coinbase"):
                inputs.append(FlowParty(address="coinbase", value=str(total_out)))
                continue
            prev = vin.get("prevout") or {}
            addr = prev.get("scriptpubkey_address")
            if not addr:
                continue
            inputs.append(
                FlowParty(address=addr, value=str(int(prev.get("value") or 0)))
            )

        if not inputs and not outputs:
            return None
        status = raw.get("status") or {}
        return CanonicalTx(
            tx_hash=raw["txid"],
            chain=self.chain,
            block_number=status.get("block_height"),
            block_time=self._ts(status.get("block_time")),
            inputs=inputs,
            outputs=outputs,
            asset=_BTC,
            fee=str(int(raw.get("fee") or 0)),
            raw={"vsize": raw.get("vsize"), "weight": raw.get("weight")},
        )

    async def health_check(self) -> bool:
        try:
            height = await self._get_json(f"{_MEMPOOL}/blocks/tip/height")
            return isinstance(height, int)
        except Exception:  # noqa: BLE001
            return False
