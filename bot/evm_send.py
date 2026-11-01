"""Sending the native coin or an ERC-20 token on an EVM chain (BSC, Neo X, ...).

Flow: quote (estimate gas, which doubles as a dry run) -> sign -> broadcast ->
wait for the receipt. Takes any object with the EvmRpc methods, so it can be
tested with a fake.
"""
import asyncio
import logging
from dataclasses import dataclass

from eth_account import Account
from eth_utils import to_checksum_address

from .evm_rpc import EvmRpcError, encode_transfer
from .neo_send import SendError, SendOutcome

log = logging.getLogger("walletbot.evm_send")

NATIVE = "native"
GAS_BUFFER_PERCENT = 120  # 20% headroom over the estimate
PRICE_BUFFER_PERCENT = 110  # 10% headroom so the tx isn't priced out by a small spike


@dataclass(frozen=True)
class Quote:
    gas_limit: int
    gas_price: int

    @property
    def fee(self) -> int:
        """Worst-case fee in the chain's smallest native unit."""
        return self.gas_limit * self.gas_price


def build_call(from_addr: str, to_addr: str, token_contract: str, raw: int) -> dict:
    """The transaction fields used for gas estimation."""
    if token_contract == NATIVE:
        return {"from": from_addr, "to": to_addr, "value": hex(raw)}
    return {"from": from_addr, "to": token_contract, "data": encode_transfer(to_addr, raw)}


async def quote(rpc, from_addr: str, to_addr: str, token_contract: str, raw: int) -> Quote:
    """Estimate gas. A transfer that would revert (not enough tokens, a token that
    blocks the transfer, ...) fails here, before any fee is spent."""
    try:
        estimate = await rpc.estimate_gas(build_call(from_addr, to_addr, token_contract, raw))
        price = await rpc.gas_price()
    except EvmRpcError as exc:
        raise SendError(
            "The network says this transfer would fail. Check your balance and the amount. "
            f"({str(exc)[:120]})"
        )
    return Quote(gas_limit=estimate * GAS_BUFFER_PERCENT // 100, gas_price=price * PRICE_BUFFER_PERCENT // 100)


async def sign_and_send(
    rpc,
    chain_id: int,
    private_key: bytes,
    from_addr: str,
    to_addr: str,
    token_contract: str,
    raw: int,
    q: Quote,
) -> str:
    """Sign a legacy (gasPrice) transaction and broadcast it. Returns the tx hash."""
    nonce = await rpc.get_nonce(from_addr)
    if token_contract == NATIVE:
        tx = {"to": to_checksum_address(to_addr), "value": raw}
    else:
        tx = {
            "to": to_checksum_address(token_contract),
            "value": 0,
            "data": encode_transfer(to_addr, raw),
        }
    tx.update({"gas": q.gas_limit, "gasPrice": q.gas_price, "nonce": nonce, "chainId": chain_id})
    signed = Account.sign_transaction(tx, private_key)
    raw_tx = getattr(signed, "raw_transaction", None) or signed.rawTransaction  # name varies by version
    raw_hex = raw_tx.hex()
    if not raw_hex.startswith("0x"):
        raw_hex = "0x" + raw_hex
    return str(await rpc.send_raw(raw_hex)).lower()


async def wait_for_receipt(
    rpc, tx_hash: str, timeout: float = 120.0, interval: float = 3.0
) -> SendOutcome:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last_error = ""
    while True:
        try:
            receipt = await rpc.get_receipt(tx_hash)
        except Exception as exc:
            receipt, last_error = None, str(exc)
        if receipt:
            ok = str(receipt.get("status", "")).lower() in ("0x1", "1")
            return SendOutcome("confirmed" if ok else "failed", tx_hash, "" if ok else "the transaction reverted")
        if loop.time() >= deadline:
            log.warning("No receipt for %s after %ss (last RPC error: %s)", tx_hash, int(timeout), last_error or "none")
            return SendOutcome("unknown", tx_hash, last_error)
        await asyncio.sleep(interval)


async def perform_send(
    *,
    rpc,
    chain_id: int,
    private_key: bytes,
    from_addr: str,
    to_addr: str,
    token_contract: str,
    raw: int,
    timeout: float = 120.0,
    interval: float = 3.0,
) -> SendOutcome:
    q = await quote(rpc, from_addr, to_addr, token_contract, raw)
    try:
        tx_hash = await sign_and_send(
            rpc, chain_id, private_key, from_addr, to_addr, token_contract, raw, q
        )
    except SendError:
        raise
    except Exception as exc:
        log.exception("EVM broadcast failed")
        raise SendError(
            "The network did not accept the transaction. Check your balance before retrying. "
            f"({str(exc)[:150]})"
        )
    return await wait_for_receipt(rpc, tx_hash, timeout, interval)
