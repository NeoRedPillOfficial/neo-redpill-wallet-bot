"""Minimal Solana JSON-RPC client (asynchronous, plain HTTP).

Only the calls the bot needs. Addresses and signatures are base58 text.
"""
import base64

import aiohttp

from . import solana_keys as keys
from . import tls

TOKEN_PROGRAM_B58 = keys.b58encode(keys.TOKEN_PROGRAM)


class SolRpcError(Exception):
    pass


class SolRpc:
    def __init__(self, url: str, timeout: float = 20.0):
        self.url = url
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def call(self, method: str, params: list | None = None):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self._timeout, connector=aiohttp.TCPConnector(ssl=tls.context())
            )
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}
        async with self._session.post(self.url, json=payload) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)
        if data.get("error"):
            error = data["error"]
            detail = error.get("message", str(error)) if isinstance(error, dict) else str(error)
            extra = error.get("data", {}).get("err") if isinstance(error, dict) and isinstance(error.get("data"), dict) else None
            raise SolRpcError(f"{detail}" + (f" ({extra})" if extra else ""))
        return data["result"]

    # ------------------------------------------------------------------ reading
    async def genesis_hash(self) -> str:
        return await self.call("getGenesisHash")

    async def get_balance(self, address: str) -> int:
        return int((await self.call("getBalance", [address, {"commitment": "confirmed"}]))["value"])

    async def token_balances(self, owner: str) -> dict[str, int]:
        """mint -> total raw amount, over all of the owner's token accounts (classic Token program)."""
        result = await self.call(
            "getTokenAccountsByOwner",
            [owner, {"programId": TOKEN_PROGRAM_B58}, {"encoding": "jsonParsed", "commitment": "confirmed"}],
        )
        totals: dict[str, int] = {}
        for item in result["value"]:
            info = item["account"]["data"]["parsed"]["info"]
            totals[info["mint"]] = totals.get(info["mint"], 0) + int(info["tokenAmount"]["amount"])
        return totals

    async def token_accounts(self, owner: str, mint: str) -> list[tuple[str, int]]:
        """[(token account address, raw amount)] for one mint."""
        result = await self.call(
            "getTokenAccountsByOwner",
            [owner, {"mint": mint}, {"encoding": "jsonParsed", "commitment": "confirmed"}],
        )
        return [
            (item["pubkey"], int(item["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"]))
            for item in result["value"]
        ]

    async def get_account(self, address: str) -> dict | None:
        """{'owner': str, 'lamports': int, 'data': bytes}, or None if the account doesn't exist."""
        result = await self.call("getAccountInfo", [address, {"encoding": "base64", "commitment": "confirmed"}])
        value = result.get("value")
        if value is None:
            return None
        return {
            "owner": value["owner"],
            "lamports": int(value["lamports"]),
            "data": base64.b64decode(value["data"][0]),
        }

    async def mint_info(self, mint: str) -> tuple[int, str]:
        """(decimals, owning program) of a token mint. Raises SolRpcError if it isn't a mint."""
        result = await self.call("getAccountInfo", [mint, {"encoding": "jsonParsed", "commitment": "confirmed"}])
        value = result.get("value")
        if value is None:
            raise SolRpcError("no such account")
        parsed = value["data"].get("parsed") if isinstance(value.get("data"), dict) else None
        if not parsed or parsed.get("type") != "mint":
            raise SolRpcError("not a token mint")
        return int(parsed["info"]["decimals"]), value["owner"]

    async def min_balance_for_rent(self, size: int) -> int:
        return int(await self.call("getMinimumBalanceForRentExemption", [size]))

    async def latest_blockhash(self) -> tuple[bytes, int]:
        value = (await self.call("getLatestBlockhash", [{"commitment": "confirmed"}]))["value"]
        return keys.b58decode(value["blockhash"]), int(value["lastValidBlockHeight"])

    async def block_height(self) -> int:
        return int(await self.call("getBlockHeight", [{"commitment": "confirmed"}]))

    # ------------------------------------------------------------------ sending
    async def send_transaction(self, transaction: bytes) -> str:
        """Broadcast. The node runs the transaction once first (preflight) and refuses it if it would fail."""
        return await self.call(
            "sendTransaction",
            [
                base64.b64encode(transaction).decode(),
                {"encoding": "base64", "skipPreflight": False, "preflightCommitment": "confirmed", "maxRetries": 5},
            ],
        )

    async def get_signature_status(self, signature: str) -> dict | None:
        result = await self.call("getSignatureStatuses", [[signature], {"searchTransactionHistory": True}])
        return result["value"][0]

    # ------------------------------------------------------------------ history
    async def signatures_since(self, address: str, until: str | None, page: int = 100, max_pages: int = 5) -> list[dict]:
        """Confirmed signatures touching `address`, newest first, stopping at `until`."""
        collected: list[dict] = []
        before = None
        for _ in range(max_pages):
            options: dict = {"limit": page, "commitment": "confirmed"}
            if until:
                options["until"] = until
            if before:
                options["before"] = before
            batch = await self.call("getSignaturesForAddress", [address, options])
            collected += batch
            if len(batch) < page:
                break
            before = batch[-1]["signature"]
        return collected

    async def get_transaction(self, signature: str) -> dict | None:
        return await self.call(
            "getTransaction",
            [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0, "commitment": "confirmed"}],
        )
