"""Minimal EVM JSON-RPC client (BNB Smart Chain, Neo X, any EVM chain) plus the
few ERC-20 calls the bot needs. Plain JSON-RPC over HTTP, no web3 dependency,
so there is no library version drift to worry about.
"""
import asyncio
import time
from dataclasses import dataclass

import logging

import aiohttp

from . import tls

log = logging.getLogger("walletbot.evm_rpc")

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
_SEL_BALANCE_OF = "0x70a08231"
_SEL_DECIMALS = "0x313ce567"
_SEL_SYMBOL = "0x95d89b41"
_SEL_NAME = "0x06fdde03"
_SEL_TRANSFER = "0xa9059cbb"


class EvmRpcError(Exception):
    pass


class LogsRestricted(EvmRpcError):
    """The node refuses open-ended eth_getLogs (needs a contract address, or says "limit exceeded")."""


# --------------------------------------------------------------- pure helpers
def _strip0x(value: str) -> str:
    return value[2:] if value[:2].lower() == "0x" else value


def pad_address(address: str) -> str:
    """Address -> 32-byte hex word (no 0x)."""
    return _strip0x(address).lower().rjust(64, "0")


def encode_balance_of(owner: str) -> str:
    return _SEL_BALANCE_OF + pad_address(owner)


def encode_transfer(to: str, amount: int) -> str:
    if not 0 <= amount < 2**256:
        raise ValueError("amount out of range")
    return _SEL_TRANSFER + pad_address(to) + format(amount, "064x")


def hex_to_int(value: str) -> int:
    digits = _strip0x(value)
    if not digits:
        raise EvmRpcError("empty result (not a contract, or wrong function)")
    return int(digits, 16)


def decode_abi_string(value: str) -> str:
    """Decode an eth_call result holding a string.

    Handles the standard dynamic string and the older bytes32 style some
    popular tokens (for example MKR) still use.
    """
    try:
        data = bytes.fromhex(_strip0x(value))
    except ValueError:
        raise EvmRpcError("result is not hex")
    if not data:
        raise EvmRpcError("empty result (not a contract, or wrong function)")
    try:
        if len(data) == 32:
            return data.rstrip(b"\x00").decode("utf-8")
        offset = int.from_bytes(data[:32], "big")
        if offset + 32 > len(data):
            raise EvmRpcError("malformed string result")
        length = int.from_bytes(data[offset : offset + 32], "big")
        body = data[offset + 32 : offset + 32 + length]
        if len(data) < 64 or len(body) != length:
            raise EvmRpcError("malformed string result")
        return body.decode("utf-8")
    except UnicodeDecodeError:
        raise EvmRpcError("string result is not valid text")


@dataclass(frozen=True)
class TokenTransferLog:
    token: str  # contract, lower case 0x...
    sender: str  # lower case 0x...
    recipient: str  # lower case 0x...
    amount: int
    tx_hash: str
    log_index: int
    block: int


def parse_transfer_log(entry: dict) -> TokenTransferLog | None:
    """ERC-20 Transfer has 3 topics and a 32-byte value. NFT transfers (4 topics)
    and anything malformed are ignored."""
    topics = entry.get("topics") or []
    data = _strip0x(entry.get("data") or "")
    if len(topics) != 3 or topics[0].lower() != TRANSFER_TOPIC or len(data) != 64:
        return None
    return TokenTransferLog(
        token=entry["address"].lower(),
        sender="0x" + topics[1][-40:].lower(),
        recipient="0x" + topics[2][-40:].lower(),
        amount=int(data, 16),
        tx_hash=entry["transactionHash"].lower(),
        log_index=hex_to_int(entry["logIndex"]),
        block=hex_to_int(entry["blockNumber"]),
    )


def input_mentions(input_hex: str, wallets: set[str]) -> bool:
    """Whether a transaction's call data contains one of our wallet addresses as an argument.

    `wallets` holds 40-character lower-case hex addresses without 0x. Call data is the 4-byte function
    selector followed by 32-byte words; an address argument is the last 20 bytes of a word. This covers
    transfer(to, amount), transferFrom(from, to, amount), swaps with a `to`, and multi-send lists.
    """
    body = _strip0x(input_hex or "").lower()[8:]
    for i in range(0, len(body) - 63, 64):
        if body[i + 24 : i + 64] in wallets:
            return True
    return False


def transfers_from_receipt(receipt: dict | None, wallets: set[str]) -> list["TokenTransferLog"]:
    """Token transfers to one of our wallets found in a mined transaction's receipt.

    `wallets` holds lower-case 0x addresses. A failed transaction (status 0) moved nothing.
    """
    if not receipt or receipt.get("status") not in ("0x1", 1, "1"):
        return []
    found = []
    for entry in receipt.get("logs") or []:
        if entry.get("removed"):
            continue
        try:
            parsed = parse_transfer_log(entry)
        except (KeyError, ValueError, EvmRpcError):
            continue
        if parsed is not None and parsed.recipient in wallets:
            found.append(parsed)
    return found


# --------------------------------------------------------------------- client
class EvmRpc:
    def __init__(self, url: str, timeout: float = 20.0):
        # One URL, or several separated by commas: when a call fails on the first, the next is tried.
        self.urls = [u.strip() for u in url.split(",") if u.strip()] or [url]
        self.url = self.urls[0]
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None
        # Some free RPC endpoints refuse eth_getLogs unless a contract address is given. Once we
        # learn that, we ask per known token contract instead (see get_transfer_logs).
        self.address_required = False
        # True once we have switched to reading blocks instead of asking for logs (see get_transfers_by_blocks).
        self.block_mode = False
        self._down_until: dict[str, float] = {}

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def _call_one(self, url: str, method: str, params: list | None):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self._timeout, connector=aiohttp.TCPConnector(ssl=tls.context())
            )
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}
        async with self._session.post(url, json=payload) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)
        if data.get("error"):
            raise EvmRpcError(f"{method}: {data['error']}")
        return data.get("result")

    async def call(self, method: str, params: list | None = None):
        if len(self.urls) == 1:
            return await self._call_one(self.urls[0], method, params)
        errors = []
        now = time.monotonic()
        # A URL whose connection just failed (403, timeout, ...) is skipped for a minute, so every call
        # doesn't first waste a request on it. If all of them are down we try them all anyway.
        order = [u for u in self.urls if self._down_until.get(u, 0.0) <= now] or list(self.urls)
        for url in order:
            try:
                return await self._call_one(url, method, params)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                host = url.split("//")[-1].split("/")[0]
                errors.append(f"{host}: {type(exc).__name__}: {exc}")
                if not isinstance(exc, EvmRpcError):  # the node itself did not answer (not a JSON-RPC refusal)
                    self._down_until[url] = now + 60.0
        raise EvmRpcError(" | ".join(errors))

    async def chain_id(self) -> int:
        return hex_to_int(await self.call("eth_chainId"))

    async def block_number(self) -> int:
        return hex_to_int(await self.call("eth_blockNumber"))

    async def get_balance(self, address: str, block: int | None = None) -> int:
        tag = hex(block) if block is not None else "latest"
        return hex_to_int(await self.call("eth_getBalance", [address, tag]))

    async def _eth_call(self, to: str, data: str) -> str:
        return await self.call("eth_call", [{"to": to, "data": data}, "latest"])

    async def erc20_balance(self, contract: str, owner: str) -> int:
        return hex_to_int(await self._eth_call(contract, encode_balance_of(owner)))

    async def token_info(self, contract: str) -> tuple[str, str, int]:
        """(symbol, name, decimals) of an ERC-20, read from the contract."""
        code = await self.call("eth_getCode", [contract, "latest"])
        if not _strip0x(code or ""):
            raise EvmRpcError("there is no contract at that address")
        decimals = hex_to_int(await self._eth_call(contract, _SEL_DECIMALS))
        if not 0 <= decimals <= 36:
            raise EvmRpcError("implausible decimals")
        symbol = decode_abi_string(await self._eth_call(contract, _SEL_SYMBOL))
        try:
            name = decode_abi_string(await self._eth_call(contract, _SEL_NAME))
        except EvmRpcError:
            name = symbol
        return symbol, name, decimals

    async def _logs(self, from_block, to_block, recipients, chunk, contracts):
        found: list[TokenTransferLog] = []
        address_filters = [None] if contracts is None else [contracts[j : j + 20] for j in range(0, len(contracts), 20)]
        for i in range(0, len(recipients), chunk):
            topics = ["0x" + pad_address(a) for a in recipients[i : i + chunk]]
            for addresses in address_filters:
                query = {"fromBlock": hex(from_block), "toBlock": hex(to_block),
                         "topics": [TRANSFER_TOPIC, None, topics]}
                if addresses is not None:
                    query["address"] = addresses
                entries = await self.call("eth_getLogs", [query])
                for entry in entries or []:
                    parsed = parse_transfer_log(entry)
                    if parsed is not None:
                        found.append(parsed)
        return found

    async def get_transfer_logs(
        self, from_block: int, to_block: int, recipients: list[str], chunk: int = 50,
        contracts: list[str] | None = None,
    ) -> list[TokenTransferLog]:
        """ERC-20 transfers TO any of the given addresses.

        Normally this asks for transfers of ANY token. Some free endpoints (publicnode, ...) refuse
        that and demand a contract address; then we switch, for good, to asking only about the token
        contracts we know (`contracts`), which means tokens the bot has never seen are not detected there.
        """
        if not self.address_required:
            try:
                return await self._logs(from_block, to_block, recipients, chunk, None)
            except EvmRpcError as exc:
                text = str(exc).lower()
                if not ("specify an address" in text or "-32701" in text
                        or "limit exceeded" in text or "-32005" in text):
                    raise
                self.address_required = True
                log.warning(
                    "%s only answers eth_getLogs for named contracts (or refuses open-ended queries): watching the known tokens only. "
                    "For full detection use an RPC provider that allows it (e.g. an Alchemy or Ankr key).",
                    self.url.split("//")[-1].split("/")[0],
                )
        if not contracts:
            return []
        return await self._split_logs(from_block, to_block, recipients, chunk, contracts)

    async def _split_logs(self, from_block, to_block, recipients, chunk, contracts, depth=0):
        """Like _logs, but if the endpoint says "limit exceeded" ask for half the block range at a time."""
        try:
            return await self._logs(from_block, to_block, recipients, chunk, contracts)
        except EvmRpcError as exc:
            text = str(exc).lower()
            if not ("limit exceeded" in text or "-32005" in text) or to_block - from_block < 50 or depth >= 5:
                raise
        await asyncio.sleep(0.3)
        mid = (from_block + to_block) // 2
        first = await self._split_logs(from_block, mid, recipients, chunk, contracts, depth + 1)
        second = await self._split_logs(mid + 1, to_block, recipients, chunk, contracts, depth + 1)
        return first + second

    async def get_open_logs(self, from_block: int, to_block: int, recipients: list[str], chunk: int = 50):
        """Transfers of ANY token to the given addresses, or LogsRestricted if this node won't answer that."""
        try:
            return await self._logs(from_block, to_block, recipients, chunk, None)
        except EvmRpcError as exc:
            text = str(exc).lower()
            if any(k in text for k in ("specify an address", "-32701", "limit exceeded", "-32005", "403", "forbidden", "archive")):
                raise LogsRestricted(str(exc)) from exc
            raise

    async def get_block(self, number: int) -> dict | None:
        return await self.call("eth_getBlockByNumber", [hex(number), True])

    async def get_transfers_by_blocks(
        self, from_block: int, to_block: int, wallets: list[str], concurrency: int = 6
    ) -> tuple[list[TokenTransferLog], int]:
        """Find token transfers to our wallets by reading blocks, for nodes that won't search logs.

        Works on every node and sees every token, listed or not. For each block we look at the call data
        of its transactions; only a transaction that names one of our wallets gets its receipt fetched,
        and the Transfer events in that receipt are what we report. Returns (transfers, last block fully
        scanned): the node may not have the newest blocks yet, in which case we stop before the gap.
        """
        wanted = {a.lower() for a in wallets}
        bare = {a[2:] for a in wanted}
        sem = asyncio.Semaphore(concurrency)

        async def one(number: int) -> list[TokenTransferLog] | None:
            async with sem:
                block = await self.get_block(number)
                if not block:
                    return None
                hashes = [
                    tx["hash"] for tx in block.get("transactions") or []
                    if isinstance(tx, dict) and input_mentions(tx.get("input", ""), bare)
                ]
                found: list[TokenTransferLog] = []
                for h in hashes:
                    found += transfers_from_receipt(await self.get_receipt(h), wanted)
                return found

        results = await asyncio.gather(*(one(n) for n in range(from_block, to_block + 1)))
        out: list[TokenTransferLog] = []
        last = from_block - 1
        for number, res in zip(range(from_block, to_block + 1), results):
            if res is None:
                break
            out += res
            last = number
        return out, last

    async def gas_price(self) -> int:
        return hex_to_int(await self.call("eth_gasPrice"))

    async def estimate_gas(self, tx: dict) -> int:
        return hex_to_int(await self.call("eth_estimateGas", [tx]))

    async def get_nonce(self, address: str) -> int:
        return hex_to_int(await self.call("eth_getTransactionCount", [address, "pending"]))

    async def send_raw(self, raw_hex: str) -> str:
        return await self.call("eth_sendRawTransaction", [raw_hex])

    async def get_receipt(self, tx_hash: str) -> dict | None:
        return await self.call("eth_getTransactionReceipt", [tx_hash])
