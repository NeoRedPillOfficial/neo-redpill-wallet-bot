"""Pure-logic tests for sending: validation, limits, balances, confirmation."""
from decimal import Decimal

import pytest

from bot import neo_rpc, neo_send
from bot import wallets as w
from bot.config import parse_send_limits
from bot.neo_send import GAS_HASH, MIN_GAS_RESERVE, SendError

NRP = "0x789518aa302b571e3e825f2c85a01ad731014a45"
LIMITS = parse_send_limits("GAS=50/200,NRP=1000/5000")


# ------------------------------------------------------------------ addresses
def test_valid_neo_address_accepted_and_garbage_rejected():
    good = w.neo_n3_generate().address
    assert neo_send.is_valid_address(good)
    assert neo_send.is_valid_address(f"  {good}  ")
    flipped = good[:-1] + ("A" if good[-1] != "A" else "B")  # breaks the checksum
    assert not neo_send.is_valid_address(flipped)
    assert not neo_send.is_valid_address(w.evm_generate().address)
    assert not neo_send.is_valid_address("")
    assert not neo_send.is_valid_address("hello")


# --------------------------------------------------------------------- amounts
@pytest.mark.parametrize(
    "text,decimals,expected",
    [("12", 8, 1_200_000_000), ("12.5", 8, 1_250_000_000), ("0.00000001", 8, 1), ("5", 0, 5),
     ("123456789012345678901234567890.12345678", 8, 12345678901234567890123456789012345678)],
)
def test_parse_amount_ok(text, decimals, expected):
    assert neo_send.parse_amount(text, decimals) == expected


@pytest.mark.parametrize(
    "text,decimals",
    [("", 8), ("0", 8), ("0.0", 8), ("-1", 8), ("1,5", 8), ("1e5", 8), ("abc", 8),
     ("1.123456789", 8), ("5.5", 0), (" ", 8), ("NaN", 8), ("1.", 8), (".5", 8)],
)
def test_parse_amount_rejects(text, decimals):
    with pytest.raises(SendError):
        neo_send.parse_amount(text, decimals)


def test_send_limits_config_parsing():
    assert LIMITS["GAS"] == (Decimal(50), Decimal(200))
    for bad in ("GAS", "GAS=50", "GAS=0/5", "GAS=10/5", "GAS=x/y"):
        with pytest.raises(RuntimeError):
            parse_send_limits(bad)


# ---------------------------------------------------------------------- limits
def test_limits():
    one_gas = 10**8
    neo_send.check_limits("GAS", 8, 50 * one_gas, 0, LIMITS)  # exactly the cap is fine
    with pytest.raises(SendError, match="per transaction"):
        neo_send.check_limits("GAS", 8, 51 * one_gas, 0, LIMITS)
    with pytest.raises(SendError, match="24-hour"):
        neo_send.check_limits("GAS", 8, 20 * one_gas, 190 * one_gas, LIMITS)
    # A token with no configured cap (e.g. one a user added) is not capped.
    neo_send.check_limits("FRANK", 8, 10**30, 0, LIMITS)


# -------------------------------------------------------------------- balances
def test_balance_checks():
    one_gas = 10**8
    balances = {GAS_HASH: 10 * one_gas, NRP: 500}
    neo_send.check_balance("NRP", NRP, 500, balances)
    with pytest.raises(SendError, match="enough NRP"):
        neo_send.check_balance("NRP", NRP, 501, balances)
    with pytest.raises(SendError, match="0.05 GAS"):
        neo_send.check_balance("NRP", NRP, 1, {NRP: 500, GAS_HASH: MIN_GAS_RESERVE - 1})
    with pytest.raises(SendError, match="0.05 GAS"):
        neo_send.check_balance("NRP", NRP, 1, {NRP: 500})  # no GAS at all
    # Sending GAS must leave the fee reserve behind.
    neo_send.check_balance("GAS", GAS_HASH, 10 * one_gas - MIN_GAS_RESERVE, balances)
    with pytest.raises(SendError, match="Keep at least 0.05 GAS"):
        neo_send.check_balance("GAS", GAS_HASH, 10 * one_gas, balances)


def test_max_sendable():
    balances = {GAS_HASH: 10**8, NRP: 500}
    assert neo_send.max_sendable(GAS_HASH, balances) == 10**8 - MIN_GAS_RESERVE
    assert neo_send.max_sendable(NRP, balances) == 500
    assert neo_send.max_sendable(GAS_HASH, {GAS_HASH: 1}) == 0


# --------------------------------------------------------------- tx outcomes
def test_parse_tx_outcome():
    ok = {"executions": [{"vmstate": "HALT", "stack": [{"type": "Boolean", "value": True}]}]}
    assert neo_rpc.parse_tx_outcome(ok) == (True, "")
    refused = {"executions": [{"vmstate": "HALT", "stack": [{"type": "Boolean", "value": False}]}]}
    assert neo_rpc.parse_tx_outcome(refused)[0] is False
    fault = {"executions": [{"vmstate": "FAULT", "exception": "insufficient funds", "stack": []}]}
    assert neo_rpc.parse_tx_outcome(fault) == (False, "insufficient funds")
    assert neo_rpc.parse_tx_outcome({"executions": []})[0] is False


# ------------------------------------------------------------------- broadcast
class ScriptedRpc:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)

    async def get_tx_outcome(self, tx_hash):
        item = self.outcomes.pop(0) if self.outcomes else None
        if isinstance(item, Exception):
            raise item
        return item


async def test_perform_send_confirmed_after_polling():
    calls = []

    async def sender(key, contract, to, amount):
        calls.append((contract, to, amount))
        return "ABCDEF"  # no 0x, upper case: must be normalised

    rpc = ScriptedRpc([None, neo_rpc.RpcError("flaky"), (True, "")])
    outcome = await neo_send.perform_send(
        rpc=rpc, sender=sender, private_key=b"k" * 32, token_contract=NRP,
        to_address="Nto", amount=7, timeout=5, interval=0.001,
    )
    assert calls == [(NRP, "Nto", 7)]
    assert (outcome.status, outcome.tx_hash) == ("confirmed", "0xabcdef")


async def test_perform_send_reports_failure_and_unknown():
    async def sender(*a):
        return "0x01"

    failed = await neo_send.perform_send(
        rpc=ScriptedRpc([(False, "FAULT")]), sender=sender, private_key=b"k" * 32,
        token_contract=NRP, to_address="N", amount=1, timeout=5, interval=0.001,
    )
    assert failed.status == "failed" and failed.detail == "FAULT"

    unknown = await neo_send.perform_send(
        rpc=ScriptedRpc([]), sender=sender, private_key=b"k" * 32,
        token_contract=NRP, to_address="N", amount=1, timeout=0, interval=0.001,
    )
    assert unknown.status == "unknown"  # broadcast, but never seen on-chain


async def test_perform_send_broadcast_errors():
    async def refuses(*a):
        raise RuntimeError("mempool full")

    async def dry_run_fails(*a):
        raise SendError("The transfer would fail on-chain. Check your balance.")

    with pytest.raises(SendError, match="did not accept"):
        await neo_send.perform_send(
            rpc=ScriptedRpc([]), sender=refuses, private_key=b"k" * 32,
            token_contract=NRP, to_address="N", amount=1,
        )
    with pytest.raises(SendError, match="would fail"):
        await neo_send.perform_send(
            rpc=ScriptedRpc([]), sender=dry_run_fails, private_key=b"k" * 32,
            token_contract=NRP, to_address="N", amount=1,
        )
