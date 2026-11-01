"""Sending SOL or an SPL token on Solana.

Flow: look up what is needed (token accounts, whether the recipient already has one) ->
build the transaction -> sign -> broadcast (the node test-runs it first and refuses it if it
would fail) -> wait for it to be confirmed. Takes any object with the SolRpc methods, so it
can be tested with a fake.
"""
import asyncio
import base64
import logging
from dataclasses import dataclass

from . import solana_keys as keys
from .neo_send import SendError, SendOutcome
from .solana_rpc import SolRpcError

log = logging.getLogger("walletbot.solana_send")

NATIVE = "native"
FALLBACK_RENT_SYSTEM_ACCOUNT = 890_880  # lamports a 0-byte account must hold to stay alive
FALLBACK_RENT_TOKEN_ACCOUNT = 2_039_280  # lamports for a 165-byte token account


def friendly_error(exc: Exception) -> str:
    text = str(exc)
    low = text.lower()
    if "rent" in low:
        return (
            "That would leave an account below Solana's minimum balance. "
            "Send a little less, or send everything."
        )
    if "insufficient" in low:
        return "Not enough SOL for the amount plus the network fee."
    if "blockhash" in low:
        return "The network was busy and dropped the request. Nothing was sent. Try again."
    return f"The network did not accept the transaction. Nothing was sent. ({text[:150]})"


@dataclass(frozen=True)
class Plan:
    instructions: list
    fee_lamports: int  # network fee plus any rent for a new token account (paid by the sender)
    creates_account: bool = False


async def plan_transfer(rpc, from_addr: str, to_addr: str, mint: str | None, decimals: int, raw: int) -> Plan:
    """Work out the instructions and the SOL they will cost. Raises SendError for anything the sender can fix."""
    payer, recipient = keys.b58decode(from_addr), keys.b58decode(to_addr)
    if mint is None:
        return Plan([keys.system_transfer(payer, recipient, raw)], keys.LAMPORTS_PER_SIGNATURE)

    mint_bytes = keys.b58decode(mint)
    accounts = await rpc.token_accounts(from_addr, mint)
    if not accounts:
        raise SendError("You don't hold any of this token.")
    source, held = max(accounts, key=lambda item: item[1])
    if held < raw:
        raise SendError("You don't have enough of this token in a single token account.")
    destination = keys.associated_token_address(recipient, mint_bytes)
    instructions = []
    fee = keys.LAMPORTS_PER_SIGNATURE
    creates = await rpc.get_account(keys.b58encode(destination)) is None
    if creates:
        # The recipient has no token account for this mint yet: the sender creates it (and pays its rent).
        instructions.append(keys.create_associated_token_account_idempotent(payer, recipient, mint_bytes))
        try:
            fee += await rpc.min_balance_for_rent(keys.TOKEN_ACCOUNT_SIZE)
        except Exception:
            fee += FALLBACK_RENT_TOKEN_ACCOUNT
    instructions.append(
        keys.token_transfer_checked(keys.b58decode(source), mint_bytes, destination, payer, raw, decimals)
    )
    return Plan(instructions, fee, creates)


async def wait_for_signature(rpc, signature: str, last_valid_block_height: int, timeout: float, interval: float) -> SendOutcome:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last_error = ""
    while True:
        try:
            status = await rpc.get_signature_status(signature)
        except Exception as exc:
            status, last_error = None, str(exc)
        if status:
            if status.get("err"):
                return SendOutcome("failed", signature, "the transaction failed on-chain")
            if status.get("confirmationStatus") in ("confirmed", "finalized"):
                return SendOutcome("confirmed", signature)
        if loop.time() >= deadline:
            break
        await asyncio.sleep(interval)
    # Not seen in time. A Solana transaction can only land before its blockhash expires, so once the
    # chain is past that point and it still isn't there, it can never land.
    try:
        if await rpc.block_height() > last_valid_block_height and not await rpc.get_signature_status(signature):
            return SendOutcome("failed", signature, "the network never included it, so nothing was sent")
    except Exception as exc:
        last_error = str(exc)
    return SendOutcome("unknown", signature, last_error)


async def perform_send(
    *,
    rpc,
    private_key: bytes,
    from_addr: str,
    to_addr: str,
    mint: str | None,
    decimals: int,
    raw: int,
    timeout: float = 60.0,
    interval: float = 2.0,
) -> SendOutcome:
    plan = await plan_transfer(rpc, from_addr, to_addr, mint, decimals, raw)
    sol_needed = plan.fee_lamports + (raw if mint is None else 0)
    if await rpc.get_balance(from_addr) < sol_needed:
        raise SendError("Not enough SOL for the amount plus the network fee (and the new token account, if one is needed).")
    blockhash, last_valid = await rpc.latest_blockhash()
    message = keys.compile_message(keys.b58decode(from_addr), plan.instructions, blockhash)
    transaction = keys.sign_transaction(private_key, message)
    signature = keys.first_signature(transaction)
    try:
        await rpc.send_transaction(transaction)
    except SolRpcError as exc:  # the node refused it: nothing went out
        raise SendError(friendly_error(exc))
    except Exception:
        # No clear answer (timeout, dropped connection). It may still have gone out, and we know its
        # id, so fall through and look for it instead of guessing.
        log.exception("Solana broadcast gave no clear answer; checking for %s", signature)
    return await wait_for_signature(rpc, signature, last_valid, timeout, interval)
