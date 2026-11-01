"""Doing a swap on a Uniswap-V2-style DEX from the bot's own wallet.

Order of events (nothing is signed until every check has passed):
  1. the router address is checked on-chain (its WETH() must be the expected wrapped coin),
  2. the balances are checked (the amount, plus the network fee),
  3. selling a token: if the router may not yet spend that exact amount, a one-off approval for EXACTLY that amount is
     sent first and waited for,
  4. the swap is dry-run by the network (a swap that would fail is refused here, before any fee is spent),
  5. the swap is signed and sent, and the receipt is awaited.
The "SupportingFeeOnTransferTokens" router functions are used, which also work for ordinary tokens and for tokens that
take a tax on every transfer. The minimum payout (slippage) and a 10-minute deadline are part of the transaction itself,
so the router refuses a trade that would give you less.
"""
import logging
import time

from eth_account import Account
from eth_utils import to_checksum_address

from . import dexquote, dexv3, dexv4
from .dexquote import _selector, addr_word, word
from .evm_rpc import EvmRpcError, hex_to_int
from .evm_send import GAS_BUFFER_PERCENT, PRICE_BUFFER_PERCENT, wait_for_receipt
from .neo_send import SendError, SendOutcome

log = logging.getLogger("walletbot.dex")

DEADLINE_SECONDS = 600
SEL_APPROVE = _selector("approve(address,uint256)", "095ea7b3")
SEL_ALLOWANCE = _selector("allowance(address,address)", "dd62ed3e")
SEL_BUY = _selector("swapExactETHForTokensSupportingFeeOnTransferTokens(uint256,address[],address,uint256)", "b6f9de95")
SEL_SELL = _selector("swapExactTokensForETHSupportingFeeOnTransferTokens(uint256,uint256,address[],address,uint256)", "791ac947")


def encode_approve(spender: str, amount: int) -> str:
    return "0x" + SEL_APPROVE + addr_word(spender) + word(amount)


def encode_allowance(owner: str, spender: str) -> str:
    return "0x" + SEL_ALLOWANCE + addr_word(owner) + addr_word(spender)


def _path_tail(path: list[str]) -> str:
    return word(len(path)) + "".join(addr_word(a) for a in path)


def encode_swap_buy(min_out: int, path: list[str], to: str, deadline: int) -> str:
    """swapExactETHForTokensSupportingFeeOnTransferTokens: the coin is sent as the transaction value."""
    return "0x" + SEL_BUY + word(min_out) + word(128) + addr_word(to) + word(deadline) + _path_tail(path)


def encode_swap_sell(amount_in: int, min_out: int, path: list[str], to: str, deadline: int) -> str:
    """swapExactTokensForETHSupportingFeeOnTransferTokens: the router pulls `amount_in` tokens, so it needs an allowance."""
    return "0x" + SEL_SELL + word(amount_in) + word(min_out) + word(160) + addr_word(to) + word(deadline) + _path_tail(path)


def _friendly(exc: Exception) -> str:
    text = str(exc)
    low = text.lower()
    if "insufficient_output_amount" in low or "too little received" in low:
        return "the price moved too much (the minimum you set would not be met)"
    if "transfer_from_failed" in low or "stf" in low.split() or "transferfrom" in low or "allowance" in low:
        return "the token would not let the DEX take it"
    if "insufficient funds" in low:
        return "not enough of the network's coin to pay the fee"
    return text[:120] or "the network refused it"


async def _estimate(rpc, tx: dict, what: str = "swap") -> int:
    try:
        return int(await rpc.estimate_gas(tx)) * GAS_BUFFER_PERCENT // 100
    except EvmRpcError as exc:
        raise SendError(f"The network says this {what} would fail ({_friendly(exc)}). Nothing was sent and nothing was spent.")


async def _sign_send(rpc, chain_id: int, key: bytes, wallet: str, to: str, value: int, data: str, gas: int, gas_price: int) -> str:
    nonce = await rpc.get_nonce(wallet)
    tx = {"to": to_checksum_address(to), "value": value, "data": data, "gas": gas, "gasPrice": gas_price,
          "nonce": nonce, "chainId": chain_id}
    signed = Account.sign_transaction(tx, key)
    raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
    raw_hex = raw.hex()
    if not raw_hex.startswith("0x"):
        raw_hex = "0x" + raw_hex
    return str(await rpc.send_raw(raw_hex)).lower()


async def swap(rpc, chain_id: int, private_key: bytes, wallet: str, q: "dexquote.Quote", token_contract: str,
               native_symbol: str, timeout: float = 150.0) -> tuple[SendOutcome, str | None]:
    """Runs the swap described by a quote. Returns (the swap's outcome, the approval's tx hash or None).
    Raises SendError, with a message safe to show, for anything that stops it BEFORE the swap is sent."""
    dex = q.dex
    if not await dexquote.verify_router(rpc, dex):
        raise SendError(f"I could not verify the {dex.name} router on-chain, so I did not trade. Try again later.")
    try:
        gas_price = await rpc.gas_price() * PRICE_BUFFER_PERCENT // 100
        native_balance = await rpc.get_balance(wallet)
    except Exception:
        raise SendError("I couldn't reach the network to check your balance. Nothing was sent.")
    fee_room = dexquote.SWAP_GAS_UNITS * gas_price
    token = token_contract
    approve_hash = None
    deadline = int(time.time()) + DEADLINE_SECONDS

    if q.buying:
        if native_balance < q.amount_in_raw + fee_room:
            raise SendError(f"You need {native_symbol} for the amount plus the network fee. Your balance is too low.")
        data = (dexv4.encode_swap(dexv4.pool_key(token, *q.fees), True, q.amount_in_raw, q.min_out_raw, deadline) if dex.version == 4
                else dexv3.encode_swap_buy(q.path, q.fees, q.amount_in_raw, q.min_out_raw, wallet, deadline) if dex.version == 3
                else encode_swap_buy(q.min_out_raw, q.path, wallet, deadline))
        value = q.amount_in_raw
    else:
        try:
            held = hex_to_int(await rpc._eth_call(token, "0x70a08231" + addr_word(wallet)))
        except Exception:
            raise SendError("I couldn't read your token balance. Nothing was sent.")
        if held < q.amount_in_raw:
            raise SendError("You don't have that much of the token.")
        if native_balance < fee_room * (3 if dex.version == 4 else 2):
            raise SendError(f"You need some {native_symbol} to pay the network fee (the approval and the swap).")
        spender = dexv4.PERMIT2 if dex.version == 4 else dex.router  # V4 pulls the token through Permit2
        try:
            allowed = hex_to_int(await rpc._eth_call(token, encode_allowance(wallet, spender)))
        except Exception:
            allowed = 0
        if allowed < q.amount_in_raw:
            data = encode_approve(spender, q.amount_in_raw)  # exactly this amount, never "unlimited"
            gas = await _estimate(rpc, {"from": wallet, "to": token, "data": data})
            approve_hash = await _sign_send(rpc, chain_id, private_key, wallet, token, 0, data, gas, gas_price)
            done = await wait_for_receipt(rpc, approve_hash, timeout=timeout)
            if done.status != "confirmed":
                raise SendError("The one-off approval did not confirm, so I did not swap. Nothing was traded. "
                                "Check your wallet before trying again.")
        if dex.version == 4:
            try:
                amount, expires = dexv4.decode_permit2_allowance(
                    await rpc._eth_call(dexv4.PERMIT2, dexv4.encode_permit2_allowance(wallet, token, dex.router)))
            except Exception:
                amount, expires = 0, 0
            if amount < q.amount_in_raw or expires <= int(time.time()) + 120:
                data = dexv4.encode_permit2_approve(token, dex.router, q.amount_in_raw, int(time.time()) + 1800)
                gas = await _estimate(rpc, {"from": wallet, "to": dexv4.PERMIT2, "data": data})
                p2_hash = await _sign_send(rpc, chain_id, private_key, wallet, dexv4.PERMIT2, 0, data, gas, gas_price)
                done = await wait_for_receipt(rpc, p2_hash, timeout=timeout)
                if done.status != "confirmed":
                    raise SendError("The second one-off approval did not confirm, so I did not swap. Nothing was traded.")
                approve_hash = approve_hash or p2_hash
        data = (dexv4.encode_swap(dexv4.pool_key(token, *q.fees), False, q.amount_in_raw, q.min_out_raw, deadline) if dex.version == 4
                else dexv3.encode_swap_sell(q.path, q.fees, q.amount_in_raw, q.min_out_raw, wallet, deadline) if dex.version == 3
                else encode_swap_sell(q.amount_in_raw, q.min_out_raw, q.path, wallet, deadline))
        value = 0

    call = {"from": wallet, "to": dex.router, "data": data}
    if value:
        call["value"] = hex(value)
    gas = await _estimate(rpc, call)  # the network's dry run
    try:
        tx_hash = await _sign_send(rpc, chain_id, private_key, wallet, dex.router, value, data, gas, gas_price)
    except Exception as exc:
        log.exception("Swap broadcast failed")
        raise SendError(f"The network did not accept the swap. Check your balance before retrying. ({(str(exc) or type(exc).__name__)[:120]})")
    return await wait_for_receipt(rpc, tx_hash, timeout=timeout), approve_hash
