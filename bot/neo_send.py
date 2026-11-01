"""Sending NEP-17 tokens on Neo N3 (withdrawals and user-to-user sends).

The Neo SDK (neo-mamba) is only used to build, sign and broadcast the
transaction. Everything around it (address and amount validation, limits,
balance and GAS checks, confirming the outcome on-chain) is plain code that
can be tested without a network.
"""
import asyncio
import logging
import re
import traceback
from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Awaitable, Callable

import base58

from .neo_rpc import NeoRpc, normalize_hash

log = logging.getLogger("walletbot.send")

NEO_N3_ADDRESS_VERSION = 0x35
GAS_HASH = "0xd2a4cff31913016155e38e474a2c06d08be276cf"
# 0.05 GAS (8 decimals) always kept in the wallet to pay network/system fees.
MIN_GAS_RESERVE = 5_000_000


class SendError(Exception):
    """A problem whose message is safe and useful to show to the user."""


# ------------------------------------------------------------------- validation
def is_valid_address(address: str) -> bool:
    try:
        raw = base58.b58decode_check(address.strip())
    except Exception:
        return False
    return len(raw) == 21 and raw[0] == NEO_N3_ADDRESS_VERSION


def parse_amount(text: str, decimals: int) -> int:
    """User text -> integer in the token's smallest unit. Strict on purpose."""
    text = text.strip()
    if not re.fullmatch(r"\d+(\.\d+)?", text):
        raise SendError("Enter a plain number such as 12 or 12.5 (no commas or letters).")
    with localcontext() as ctx:
        ctx.prec = 100
        scaled = Decimal(text) * (Decimal(10) ** decimals)
        if scaled != scaled.to_integral_value():
            where = "no decimals" if decimals == 0 else f"at most {decimals} decimal places"
            raise SendError(f"This token allows {where}.")
        raw = int(scaled)
    if raw <= 0:
        raise SendError("The amount must be greater than zero.")
    return raw


def _units(amount: Decimal, decimals: int) -> int:
    with localcontext() as ctx:
        ctx.prec = 100
        return int(amount * (Decimal(10) ** decimals))


def check_limits(
    symbol: str,
    decimals: int,
    raw_amount: int,
    used_24h_raw: int,
    limits: dict[str, tuple[Decimal, Decimal]],
) -> None:
    if symbol.upper() not in limits:
        return  # no cap configured for this token (for example tokens users add themselves)
    per_tx, daily = limits[symbol.upper()]
    if raw_amount > _units(per_tx, decimals):
        raise SendError(f"The limit per transaction is {per_tx:f} {symbol}.")
    if used_24h_raw + raw_amount > _units(daily, decimals):
        raise SendError(f"This would pass your 24-hour limit of {daily:f} {symbol}.")


def check_balance(
    symbol: str, token_contract: str, raw_amount: int, balances: dict[str, int]
) -> None:
    """Enough of the token, and enough GAS left over to pay the fees."""
    contract = normalize_hash(token_contract)
    held = balances.get(contract, 0)
    gas = balances.get(GAS_HASH, 0)
    if raw_amount > held:
        raise SendError(f"You don't have enough {symbol}.")
    if contract == GAS_HASH:
        if raw_amount + MIN_GAS_RESERVE > gas:
            raise SendError(
                "Keep at least 0.05 GAS in your wallet to pay the network fee. "
                "Send a smaller amount."
            )
    elif gas < MIN_GAS_RESERVE:
        raise SendError(
            "You need at least 0.05 GAS in your Neo N3 wallet to pay the network fee. "
            "Deposit some GAS first."
        )


def max_sendable(token_contract: str, balances: dict[str, int]) -> int:
    held = balances.get(normalize_hash(token_contract), 0)
    if normalize_hash(token_contract) == GAS_HASH:
        return max(0, held - MIN_GAS_RESERVE)
    return held


# ------------------------------------------------------------------- broadcasting
# (private key, token contract hash, destination address, raw amount) -> tx hash
SenderFn = Callable[[bytes, str, str, int], Awaitable[str]]


# Steps of building a transaction that only READ from the node. If the node does not answer during one of them,
# nothing has been signed or sent, so doing it again is safe. Anything else (above all the broadcast itself) is never repeated.
_READ_ONLY_STEPS = ("get_version", "get_block_count", "calculate_network_fee", "invoke_script", "test_invoke", "get_application_log")


_OWN_FRAMES = {"send", "perform_send", "retry_before_sending", "make_mamba_sender", "failed_before_sending"}


def failed_before_sending(exc: BaseException) -> bool:
    """True when the error came from a read-only step, so no transaction can have reached the network."""
    names = [f.name.lower() for f in traceback.extract_tb(exc.__traceback__)]
    cause = exc.__cause__ or exc.__context__
    while cause is not None:
        names += [f.name.lower() for f in traceback.extract_tb(cause.__traceback__)]
        cause = cause.__cause__ or cause.__context__
    if any("send" in n for n in names if n not in _OWN_FRAMES):
        return False  # something that sends: it may have gone out
    return any(step in n for n in names for step in _READ_ONLY_STEPS)


async def retry_before_sending(make_call, attempts: int = 3, pause: float = 1.5):
    """Runs make_call(); if the node does not answer in a read-only step it tries again (up to `attempts`).
    A failure that could mean the transaction was sent is raised at once, never repeated."""
    for attempt in range(1, attempts + 1):
        try:
            return await make_call()
        except SendError:
            raise
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            if not failed_before_sending(exc):
                raise
            log.warning("Neo node did not answer before sending (%s), attempt %s of %s", type(exc).__name__, attempt, attempts)
            if attempt == attempts:
                raise SendError("The Neo node is not answering right now. Nothing was sent, so you can safely try again in a moment.")
            await asyncio.sleep(pause)


def make_mamba_sender(rpc_url: str) -> SenderFn:
    """Sender backed by the neo-mamba SDK."""

    async def send(private_key: bytes, token_contract: str, to_address: str, amount: int) -> str:
        from neo3.api.helpers.signing import sign_with_account
        from neo3.api.wrappers import ChainFacade, NEP17Contract
        from neo3.core import types
        from neo3.network.payloads.verification import Signer
        from neo3.wallet.account import Account

        account = Account.from_private_key(private_key)
        contract = NEP17Contract(types.UInt160.from_string(token_contract))

        # Dry run first (on its own connection, with a test-only signer): catches an
        # unaffordable transfer before any fee is spent. A signer can only be added once
        # per connection, so the real transaction uses a fresh one.
        async def dry_run():
            dry_facade = ChainFacade(rpc_host=rpc_url)
            dry_facade.add_test_signer(Signer(account.script_hash))
            return await dry_facade.test_invoke(contract.transfer(account.address, to_address, amount))

        dry = await retry_before_sending(dry_run)
        if "HALT" not in str(dry.state).upper() or dry.result is not True:
            raise SendError("The transfer would fail on-chain. Check your balance.")

        async def build_and_broadcast():
            facade = ChainFacade(rpc_host=rpc_url)
            facade.add_signer(sign_with_account(account), Signer(account.script_hash))
            return await facade.invoke_fast(contract.transfer(account.address, to_address, amount))

        tx_id = await retry_before_sending(build_and_broadcast)
        return str(tx_id)

    return send


@dataclass(frozen=True)
class SendOutcome:
    status: str  # confirmed | failed | unknown
    tx_hash: str
    detail: str = ""


async def wait_for_outcome(
    rpc: NeoRpc, tx_hash: str, timeout: float = 90.0, interval: float = 3.0, expect_true: bool = True
) -> SendOutcome:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last_error = ""
    while True:
        try:
            outcome = await (
                rpc.get_tx_outcome(tx_hash) if expect_true else rpc.get_tx_outcome(tx_hash, expect_true=False)
            )
        except Exception as exc:  # node hiccup: keep trying until the deadline
            outcome, last_error = None, str(exc)
        if outcome is not None:
            ok, detail = outcome
            return SendOutcome("confirmed" if ok else "failed", tx_hash, detail)
        if loop.time() >= deadline:
            return SendOutcome("unknown", tx_hash, last_error)
        await asyncio.sleep(interval)


async def perform_send(
    *,
    rpc: NeoRpc,
    sender: SenderFn,
    private_key: bytes,
    token_contract: str,
    to_address: str,
    amount: int,
    timeout: float = 90.0,
    interval: float = 3.0,
) -> SendOutcome:
    """Broadcast the transfer, then watch the chain for its result.

    Raises SendError if the transaction was not accepted by the network.
    Returns status 'unknown' if it was broadcast but not seen on-chain in time
    (the caller must then tell the user to check the transaction, not retry).
    """
    try:
        tx_hash = await sender(private_key, token_contract, to_address, amount)
    except SendError:
        raise
    except Exception as exc:
        log.exception("Broadcast failed")
        raise SendError(
            "The network did not accept the transaction. Check your balance before retrying. "
            f"({(str(exc) or type(exc).__name__)[:150]})"
        )
    return await wait_for_outcome(rpc, normalize_hash(tx_hash), timeout, interval)
