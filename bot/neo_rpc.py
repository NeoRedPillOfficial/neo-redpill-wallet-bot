"""Minimal read-only Neo N3 JSON-RPC client.

Uses three standard methods:
  getnep17balances    (RpcNep17Tracker plugin)  current token balances of an address
  getnep17transfers   (RpcNep17Tracker plugin)  transfers of an address in a time range
  invokefunction                                 read a token's symbol / decimals

The tracker methods only work on nodes that run the RpcNep17Tracker plugin.
Point NEO_N3_RPC at a node that has it (a public one or your own).
"""
import base64
from dataclasses import dataclass

import aiohttp

from . import tls


class RpcError(Exception):
    pass


def normalize_hash(value: str) -> str:
    """Lower-case with a 0x prefix, so hashes compare reliably."""
    value = value.strip().lower()
    return value if value.startswith("0x") else "0x" + value


@dataclass(frozen=True)
class Transfer:
    tx_hash: str
    asset: str
    amount: int  # smallest unit
    from_address: str | None  # None for mints
    timestamp_ms: int
    notify_index: int


# ----------------------------------------------------------------- pure parsers
def parse_balances(result: dict) -> dict[str, int]:
    return {
        normalize_hash(b["assethash"]): int(b["amount"]) for b in result.get("balance", [])
    }


def parse_received(result: dict) -> list[Transfer]:
    return [
        Transfer(
            tx_hash=normalize_hash(r["txhash"]),
            asset=normalize_hash(r["assethash"]),
            amount=int(r["amount"]),
            from_address=r.get("transferaddress") or None,
            timestamp_ms=int(r["timestamp"]),
            notify_index=int(r.get("transfernotifyindex", 0)),
        )
        for r in result.get("received", [])
    ]


def _stack_item(result: dict) -> dict:
    if result.get("state") != "HALT" or not result.get("stack"):
        raise RpcError(f"contract call failed: {result.get('exception') or result.get('state')}")
    return result["stack"][0]


def parse_tx_outcome(result: dict, expect_true: bool = True) -> tuple[bool, str]:
    """From a getapplicationlog result: (succeeded, detail).

    Succeeded means the VM halted normally and, when `expect_true` (token transfers
    return true on success), the call returned true. Calls that return nothing, such as
    the bridge's depositNative, leave a null on the stack: use expect_true=False.
    """
    executions = result.get("executions") or []
    if not executions:
        return False, "no execution found"
    ex = executions[0]
    if ex.get("vmstate") != "HALT":
        return False, ex.get("exception") or ex.get("vmstate") or "execution faulted"
    stack = ex.get("stack") or []
    if stack and expect_true:
        item = stack[0]
        value = item.get("value")
        returned_true = value is True or (item.get("type") == "Integer" and str(value) == "1")
        if not returned_true:
            return False, "the token contract refused the transfer"
    return True, ""


def parse_decimals(result: dict) -> int:
    item = _stack_item(result)
    if item.get("type") != "Integer":
        raise RpcError("decimals() did not return an integer")
    value = int(item["value"])
    if not 0 <= value <= 36:
        raise RpcError(f"implausible decimals value {value}")
    return value


def parse_symbol(result: dict) -> str:
    item = _stack_item(result)
    if item.get("type") != "ByteString":
        raise RpcError("symbol() did not return a string")
    return base64.b64decode(item["value"]).decode("utf-8")


# ----------------------------------------------------------------------- client
class NeoRpc:
    def __init__(self, url: str, timeout: float = 15.0):
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
            raise RpcError(f"{method}: {data['error']}")
        return data["result"]

    async def get_balances(self, address: str) -> dict[str, int]:
        """Token contract hash -> balance in smallest units."""
        return parse_balances(await self.call("getnep17balances", [address]))

    async def get_received(self, address: str, start_ms: int, end_ms: int) -> list[Transfer]:
        result = await self.call("getnep17transfers", [address, start_ms, end_ms])
        return parse_received(result)

    async def get_tx_outcome(self, tx_hash: str, expect_true: bool = True) -> tuple[bool, str] | None:
        """None while the transaction is not on-chain yet, else (succeeded, detail).

        Needs a node with the ApplicationLogs plugin.
        """
        try:
            result = await self.call("getapplicationlog", [tx_hash])
        except RpcError as exc:
            if "unknown" in str(exc).lower():
                return None
            raise
        return parse_tx_outcome(result, expect_true)

    async def get_token_info(self, contract: str) -> tuple[str, int]:
        """Return (symbol, decimals) read from the token contract."""
        symbol = parse_symbol(await self.call("invokefunction", [contract, "symbol", []]))
        decimals = parse_decimals(await self.call("invokefunction", [contract, "decimals", []]))
        return symbol, decimals
