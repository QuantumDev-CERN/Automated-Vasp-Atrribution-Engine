"""Solana adapter via the public JSON-RPC (free, no key).

The supplied Solscan Pro key is well-formed (header `token: <key>` is the
correct v2.0 auth) but its plan answers 401 "Please upgrade your api key
level" on every endpoint, so Solana runs on api.mainnet-beta.solana.com:
  getSignaturesForAddress -> getTransaction (jsonParsed, v0 support)

Native SOL = `system` transfer instructions; SPL = `spl-token`
transfer / transferChecked instructions. One CanonicalTx is emitted per
transfer instruction (a Solana tx can carry many).

Known M4 refinements: SPL source/destination are token *accounts*, not
wallets — owner resolution needs getAccountInfo per account. Plain
`spl-token transfer` (unlike transferChecked) carries no mint in `info`,
so the mint is resolved via getAccountInfo and cached per call.
"""
import asyncio
import itertools
from typing import Any, Optional

from .base import (
    AdapterError,
    Asset,
    AssetKind,
    CanonicalTx,
    Chain,
    ChainAdapter,
    FlowParty,
    RateLimiter,
)

RPC = "https://api.mainnet-beta.solana.com"
WSOL_MINT = "So11111111111111111111111111111111111111112"
_SOL = Asset(kind=AssetKind.NATIVE, chain=Chain.SOLANA, symbol="SOL", decimals=9)

_req_id = itertools.count(1)


class SolanaAdapter(ChainAdapter):
    chain = Chain.SOLANA
    RPC = RPC

    def __init__(self, timeout: float = 30.0):
        super().__init__(timeout=timeout)
        self._limiter = RateLimiter(0.5)  # public RPC: stay polite

    # ---------- low-level ----------

    async def _post_json(self, payload: Any) -> Any:
        last: Optional[Exception] = None
        for attempt in range(3):
            await self._limiter.wait()
            try:
                r = await self._client.post(self.RPC, json=payload)
                r.raise_for_status()
                body = r.json()
                if isinstance(body, dict) and body.get("error"):
                    raise AdapterError(f"solana RPC error: {body['error']}")
                return body
            except Exception as exc:  # noqa: BLE001 — retried below
                last = exc
                await asyncio.sleep(2**attempt)
        raise AdapterError(f"POST {self.RPC} failed after 3 tries: {last}")

    async def get_signatures(
        self, address: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Raw signature list for address, newest first (successful txs only)."""
        body = await self._post_json(
            {
                "jsonrpc": "2.0",
                "id": next(_req_id),
                "method": "getSignaturesForAddress",
                "params": [address, {"limit": min(limit, 1000)}],
            }
        )
        return [s for s in (body.get("result") or []) if not s.get("err")]

    async def _tx_detail(self, signature: str) -> Optional[dict[str, Any]]:
        body = await self._post_json(
            {
                "jsonrpc": "2.0",
                "id": next(_req_id),
                "method": "getTransaction",
                "params": [
                    signature,
                    {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1},
                ],
            }
        )
        return body.get("result")

    @staticmethod
    def _instructions(detail: dict[str, Any]):
        """Yield (index, program, parsed_type, info) for parsed instructions.

        Covers top-level AND inner instructions — DEX/swap programs (Jupiter
        etc.) execute SPL transfers as inner instructions of an outer call.
        """
        msg = (detail.get("transaction") or {}).get("message") or {}
        for idx, ix in enumerate(msg.get("instructions") or []):
            parsed = ix.get("parsed") or {}
            if isinstance(parsed, str):
                continue  # unparsed blob
            yield idx, ix.get("program"), parsed.get("type"), parsed.get("info") or {}
        meta = detail.get("meta") or {}
        for group in meta.get("innerInstructions") or []:
            for j, ix in enumerate(group.get("instructions") or []):
                parsed = ix.get("parsed") or {}
                if isinstance(parsed, str):
                    continue
                yield (
                    f"inner:{group.get('index')}:{j}",
                    ix.get("program"),
                    parsed.get("type"),
                    parsed.get("info") or {},
                )

    async def _mint_of(
        self, token_account: str, cache: dict[str, Optional[str]]
    ) -> Optional[str]:
        if token_account in cache:
            return cache[token_account]
        body = await self._post_json(
            {
                "jsonrpc": "2.0",
                "id": next(_req_id),
                "method": "getAccountInfo",
                "params": [token_account, {"encoding": "jsonParsed"}],
            }
        )
        mint: Optional[str] = None
        try:
            mint = body["result"]["value"]["data"]["parsed"]["info"].get("mint")
        except (TypeError, KeyError):
            pass
        cache[token_account] = mint
        return mint

    # ---------- ChainAdapter interface ----------

    async def get_transactions(
        self, address: str, limit: int = 10
    ) -> list[CanonicalTx]:
        """Native SOL transfers involving address (system transfer ix)."""
        out: list[CanonicalTx] = []
        for sig in await self.get_signatures(address, limit):
            detail = await self._tx_detail(sig["signature"])
            if not detail:
                continue
            fee = str(int((detail.get("meta") or {}).get("fee") or 0))
            for idx, program, typ, info in self._instructions(detail):
                if program != "system" or typ != "transfer":
                    continue
                src, dst = info.get("source"), info.get("destination")
                if address not in (src, dst):
                    continue
                lamports = str(int(info.get("lamports") or 0))
                out.append(
                    CanonicalTx(
                        tx_hash=sig["signature"],
                        chain=self.chain,
                        block_number=sig.get("slot"),
                        block_time=self._ts(sig.get("blockTime")),
                        inputs=[FlowParty(address=src, value=lamports)],
                        outputs=[FlowParty(address=dst, value=lamports)],
                        asset=_SOL,
                        fee=fee,
                        raw={"instruction_index": idx},
                    )
                )
                if len(out) >= limit:
                    return out
        return out

    async def get_token_transfers(
        self, address: str, limit: int = 10
    ) -> list[CanonicalTx]:
        """SPL transfers in txs involving address (token accounts as parties)."""
        out: list[CanonicalTx] = []
        mint_cache: dict[str, Optional[str]] = {}
        for sig in await self.get_signatures(address, limit * 2):
            detail = await self._tx_detail(sig["signature"])
            if not detail:
                continue
            for idx, program, typ, info in self._instructions(detail):
                if program != "spl-token" or typ not in ("transfer", "transferChecked"):
                    continue
                src, dst = info.get("source"), info.get("destination")
                mint = info.get("mint")
                token_amount = info.get("tokenAmount") or {}
                amount = token_amount.get("amount", info.get("amount"))
                decimals = token_amount.get("decimals")
                if not mint:
                    mint = await self._mint_of(src, mint_cache)
                if not mint or amount is None:
                    continue
                out.append(
                    CanonicalTx(
                        tx_hash=sig["signature"],
                        chain=self.chain,
                        block_number=sig.get("slot"),
                        block_time=self._ts(sig.get("blockTime")),
                        inputs=[FlowParty(address=src, value=str(amount))],
                        outputs=[FlowParty(address=dst, value=str(amount))],
                        asset=Asset(
                            kind=AssetKind.TOKEN,
                            chain=self.chain,
                            contract=mint,
                            decimals=decimals,
                        ),
                        raw={"instruction_index": idx},
                    )
                )
                if len(out) >= limit:
                    return out
        return out

    async def health_check(self) -> bool:
        try:
            body = await self._post_json(
                {"jsonrpc": "2.0", "id": next(_req_id), "method": "getHealth"}
            )
            return body.get("result") == "ok"
        except Exception:  # noqa: BLE001
            return False
