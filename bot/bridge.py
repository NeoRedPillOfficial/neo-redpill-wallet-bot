"""Neo N3 <-> Neo X bridge for GAS, using the official bridge contracts.

Source of truth (read from the contracts' code, bane-labs/bridge-neo-contracts and
bane-labs/bridge-evm-contracts):

  Neo N3 -> Neo X   depositNative(Hash160 from, Hash160 to, int amount, int maxFee)
                    on the N3 NeoXBridge contract. `amount` is GAS (8 decimals). The
                    bridge fee (nativeDepositFee, in GAS) is charged ON TOP of `amount`.
                    `to` is the Neo X address as 20 bytes: the Neo X address digits are
                    passed as the Hash160 string, the normal way.
  Neo X -> Neo N3   withdrawNative(address _to, uint256 _maxFee) payable on
                    0x1212000000000000000000000000000000000004. msg.value = amount + fee
                    (wei, 18 decimals; amount must be a multiple of 1e10). `_to` is the
                    N3 account's script hash written as an address: "0x" + the digits
                    Neo shows for that script hash (the address bytes reversed).

A validator-signed relayer completes the transfer on the other side, about 1-2
minutes later. This module only does the source-chain transaction; the existing
deposit watchers notify the user when the funds arrive.

Only GAS is bridged. The official bridge does not bridge NEO, NRP or FRANK.
"""
import logging
from dataclasses import dataclass

import base58
from eth_account import Account
from eth_utils import keccak, to_checksum_address

from . import evm_send, neo_send
from .evm_rpc import pad_address
from .neo_send import GAS_HASH, MIN_GAS_RESERVE, SendError, SendOutcome

log = logging.getLogger("walletbot.bridge")

N3_BRIDGE = {
    "mainnet": "0xbb19cfc864b73159277e1fd39694b3fd5fc613d2",
    "testnet": "0x2ba94444d43c9a084a5660982a9f95f43f07422e",
}
NEOX_BRIDGE = "0x1212000000000000000000000000000000000004"
SCALE = 10**10  # Neo X GAS has 18 decimals, Neo N3 GAS has 8
N3_DECIMALS = 8
ARRIVAL_HINT = "usually 1-2 minutes"


# ------------------------------------------------------------ address mapping
def n3_address_to_neox_form(n3_address: str) -> str:
    """The `_to` value for withdrawNative: '0x' + the script hash as Neo displays it."""
    raw = base58.b58decode_check(n3_address.strip())
    if len(raw) != 21 or raw[0] != neo_send.NEO_N3_ADDRESS_VERSION:
        raise SendError("That is not a valid Neo N3 address.")
    return "0x" + raw[1:][::-1].hex()


def neox_address_to_hash160_string(evm_address: str) -> str:
    """The `to` value for depositNative as a Hash160 string (same digits)."""
    text = evm_address.strip().lower()
    if len(text) != 42 or not text.startswith("0x"):
        raise SendError("That is not a valid Neo X address.")
    int(text, 16)  # raises ValueError on non-hex
    return text


# ------------------------------------------------------------------ limits
@dataclass(frozen=True)
class Limits:
    """Bridge settings in the SOURCE chain's smallest unit."""

    fee: int
    min_amount: int
    max_amount: int
    paused: bool


def _stack_value(result: dict):
    if str(result.get("state", "")).upper() != "HALT" or not result.get("stack"):
        raise SendError("The bridge contract did not answer. Try again later.")
    item = result["stack"][0]
    if item["type"] == "Boolean":
        return bool(item["value"])
    return int(item["value"])


async def read_n3_limits(neo_rpc, bridge_hash: str) -> Limits:
    async def call(method: str):
        return _stack_value(await neo_rpc.call("invokefunction", [bridge_hash, method, []]))

    try:
        fee = await call("nativeDepositFee")
        low = await call("minNativeDeposit")
        high = await call("maxNativeDeposit")
        paused = bool(await call("isPaused")) or bool(await call("depositsArePaused"))
    except SendError:
        raise
    except Exception as exc:
        raise SendError(f"I couldn't read the bridge settings on Neo N3 ({str(exc)[:80]}).")
    return Limits(fee=fee, min_amount=low, max_amount=high, paused=paused)


def _sel(signature: str) -> str:
    return keccak(text=signature)[:4].hex()


async def _call_word(x_rpc, signature: str, index: int = 0) -> int:
    data = await x_rpc.call("eth_call", [{"to": NEOX_BRIDGE, "data": "0x" + _sel(signature)}, "latest"])
    digits = data[2:] if data.startswith("0x") else data
    if len(digits) < 64 * (index + 1):
        raise SendError("The Neo X bridge did not answer as expected.")
    return int(digits[64 * index : 64 * (index + 1)], 16)


async def read_neox_limits(x_rpc) -> Limits:
    """nativeBridge() returns: paused, deposit(nonce,root), withdrawal(nonce,root),
    config(fee, min, max, maxDeposits, scalingFactor), one 32-byte word each."""
    try:
        words = await x_rpc.call("eth_call", [{"to": NEOX_BRIDGE, "data": "0x" + _sel("nativeBridge()")}, "latest"])
        digits = words[2:] if words.startswith("0x") else words
        if len(digits) < 64 * 10:
            raise SendError("The Neo X bridge did not answer as expected.")
        w = [int(digits[i * 64 : (i + 1) * 64], 16) for i in range(10)]
        if w[9] != 10:  # we only know how to scale 18 -> 8 decimals
            raise SendError("The Neo X bridge uses a decimal setting I don't support. Bridging is disabled.")
        paused = bool(w[0]) or bool(await _call_word(x_rpc, "bridgePaused()")) or bool(
            await _call_word(x_rpc, "withdrawalsPaused()")
        )
        return Limits(fee=w[5], min_amount=w[6], max_amount=w[7], paused=paused)
    except SendError:
        raise
    except Exception as exc:
        raise SendError(f"I couldn't read the bridge settings on Neo X ({str(exc)[:80]}).")


# --------------------------------------------------------------------- plans
@dataclass(frozen=True)
class Plan:
    direction: str  # "n3_to_x" | "x_to_n3"
    amount8: int  # GAS in 8-decimal units (what the user typed, what arrives)
    fee: int  # bridge fee, in the SOURCE chain's smallest unit
    network_fee_estimate: int  # estimated network fee on the source chain (smallest unit)
    max_fee: int  # passed to the contract as maxFee


def plan_n3_to_x(amount8: int, limits: Limits, gas_balance: int) -> Plan:
    if limits.paused:
        raise SendError("The bridge is paused right now. Try again later.")
    if amount8 < limits.min_amount:
        raise SendError(f"The minimum is {_fmt(limits.min_amount, 8)} GAS.")
    if limits.max_amount and amount8 > limits.max_amount:
        raise SendError(f"The maximum per bridge transfer is {_fmt(limits.max_amount, 8)} GAS.")
    need = amount8 + limits.fee + MIN_GAS_RESERVE
    if gas_balance < need:
        raise SendError(
            f"You need {_fmt(need, 8)} GAS on Neo N3: the amount, the bridge fee "
            f"({_fmt(limits.fee, 8)}) and 0.05 for network fees. You have {_fmt(gas_balance, 8)}."
        )
    return Plan("n3_to_x", amount8, limits.fee, MIN_GAS_RESERVE, limits.fee)


def plan_x_to_n3(amount8: int, limits: Limits, gas_balance_wei: int, network_fee_wei: int) -> Plan:
    if limits.paused:
        raise SendError("The bridge is paused right now. Try again later.")
    wei = amount8 * SCALE
    if wei < limits.min_amount:
        raise SendError(f"The minimum is {_fmt(limits.min_amount, 18)} GAS.")
    if limits.max_amount and wei > limits.max_amount:
        raise SendError(f"The maximum per bridge transfer is {_fmt(limits.max_amount, 18)} GAS.")
    need = wei + limits.fee + network_fee_wei
    if gas_balance_wei < need:
        raise SendError(
            f"You need about {_fmt(need, 18)} GAS on Neo X: the amount, the bridge fee "
            f"({_fmt(limits.fee, 18)}) and the network fee. You have {_fmt(gas_balance_wei, 18)}."
        )
    return Plan("x_to_n3", amount8, limits.fee, network_fee_wei, limits.fee)


def max_amount8_n3_to_x(limits: Limits, gas_balance: int) -> int:
    room = gas_balance - limits.fee - MIN_GAS_RESERVE
    if limits.max_amount:
        room = min(room, limits.max_amount)
    return max(0, room)


def max_amount8_x_to_n3(limits: Limits, gas_balance_wei: int, network_fee_wei: int) -> int:
    room = gas_balance_wei - limits.fee - network_fee_wei
    if limits.max_amount:
        room = min(room, limits.max_amount)
    return max(0, room // SCALE)


def _fmt(raw: int, decimals: int) -> str:
    from .service import format_amount

    return format_amount(raw, decimals)


# ------------------------------------------------------------------ Neo N3 side
def make_mamba_depositor(rpc_url: str):
    """Returns an async function that calls depositNative with neo-mamba."""

    async def deposit(private_key: bytes, bridge_hash: str, to_hash160: str, amount8: int, max_fee: int) -> str:
        from neo3.api.helpers.signing import sign_with_account
        from neo3.api.wrappers import ChainFacade, GenericContract
        from neo3.core import types
        from neo3.network.payloads.verification import Signer, WitnessScope
        from neo3.wallet.account import Account as NeoAccount

        account = NeoAccount.from_private_key(private_key)
        contract = GenericContract(types.UInt160.from_string(bridge_hash))
        args = [account.script_hash, types.UInt160.from_string(to_hash160), amount8, max_fee]
        # GLOBAL scope: the bridge calls the GAS contract on the user's behalf, which a
        # "called by entry" signature would not cover. This signature is used for this one transaction only.
        signer = Signer(account.script_hash, WitnessScope.GLOBAL)
        # Separate connections for the dry run and the real transaction: a signer can only be added once.
        dry_facade = ChainFacade(rpc_host=rpc_url)
        dry_facade.add_test_signer(signer)
        dry = await dry_facade.test_invoke(contract.call_function("depositNative", args))
        if "HALT" not in str(dry.state).upper():
            raise SendError("The bridge would reject this deposit. Check the amount and your balance.")

        facade = ChainFacade(rpc_host=rpc_url)
        facade.add_signer(sign_with_account(account), signer)
        tx_id = await facade.invoke_fast(contract.call_function("depositNative", args))
        return str(tx_id)

    return deposit


async def run_n3_to_x(
    *, neo_rpc, depositor, bridge_hash: str, private_key: bytes, to_neox_address: str, plan: Plan,
    timeout: float = 90.0, interval: float = 3.0,
) -> SendOutcome:
    to = neox_address_to_hash160_string(to_neox_address)
    try:
        tx_hash = await depositor(private_key, bridge_hash, to, plan.amount8, plan.max_fee)
    except SendError:
        raise
    except Exception as exc:
        log.exception("Bridge deposit failed")
        raise SendError(
            "The network did not accept the bridge transaction. Check your balance before retrying. "
            f"({str(exc)[:150]})"
        )
    # depositNative returns nothing, so success is just 'the transaction ran without faulting'.
    return await neo_send.wait_for_outcome(neo_rpc, tx_hash.lower(), timeout, interval, expect_true=False)


# ------------------------------------------------------------------ Neo X side
def encode_withdraw_native(to_neox_form: str, max_fee: int) -> str:
    return "0x" + _sel("withdrawNative(address,uint256)") + pad_address(to_neox_form) + format(max_fee, "064x")


async def quote_x_to_n3(x_rpc, from_addr: str, to_n3_address: str, amount8: int, fee: int):
    """Dry-run the withdrawal on Neo X (estimate_gas reverts if the bridge would refuse it)."""
    call = {
        "from": from_addr,
        "to": NEOX_BRIDGE,
        "value": hex(amount8 * SCALE + fee),
        "data": encode_withdraw_native(n3_address_to_neox_form(to_n3_address), fee),
    }
    try:
        estimate = await x_rpc.estimate_gas(call)
        price = await x_rpc.gas_price()
    except Exception as exc:
        raise SendError(f"The bridge would reject this withdrawal ({str(exc)[:120]}).")
    return evm_send.Quote(
        gas_limit=estimate * evm_send.GAS_BUFFER_PERCENT // 100,
        gas_price=price * evm_send.PRICE_BUFFER_PERCENT // 100,
    )


async def run_x_to_n3(
    *, x_rpc, chain_id: int, private_key: bytes, from_addr: str, to_n3_address: str, plan: Plan,
    timeout: float = 120.0, interval: float = 3.0,
) -> SendOutcome:
    q = await quote_x_to_n3(x_rpc, from_addr, to_n3_address, plan.amount8, plan.fee)
    try:
        nonce = await x_rpc.get_nonce(from_addr)
        tx = {
            "to": to_checksum_address(NEOX_BRIDGE),
            "value": plan.amount8 * SCALE + plan.fee,
            "data": encode_withdraw_native(n3_address_to_neox_form(to_n3_address), plan.max_fee),
            "gas": q.gas_limit,
            "gasPrice": q.gas_price,
            "nonce": nonce,
            "chainId": chain_id,
        }
        signed = Account.sign_transaction(tx, private_key)
        raw_tx = getattr(signed, "raw_transaction", None) or signed.rawTransaction
        raw_hex = raw_tx.hex()
        raw_hex = raw_hex if raw_hex.startswith("0x") else "0x" + raw_hex
        tx_hash = str(await x_rpc.send_raw(raw_hex)).lower()
    except SendError:
        raise
    except Exception as exc:
        log.exception("Bridge withdrawal failed")
        raise SendError(
            "The network did not accept the bridge transaction. Check your balance before retrying. "
            f"({str(exc)[:150]})"
        )
    return await evm_send.wait_for_receipt(x_rpc, tx_hash, timeout, interval)
