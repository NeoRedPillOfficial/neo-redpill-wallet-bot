"""Tests for the Neo N3 <-> Neo X bridge (no network: fake nodes)."""
import base58
import pytest
from eth_account import Account

from bot import bridge, neo_send
from bot import wallets as w
from bot.neo_send import SendError, SendOutcome

GAS = 10**8
WEI = 10**18


# ------------------------------------------------------------ address mapping
def test_n3_address_maps_to_reversed_script_hash():
    kp = w.neo_n3_generate()
    raw = base58.b58decode_check(kp.address)[1:]  # the script hash bytes as stored in the address
    form = bridge.n3_address_to_neox_form(kp.address)
    assert form == "0x" + raw[::-1].hex()
    assert len(form) == 42 and form == form.lower()
    # the same digits Neo shows for that script hash: converting back gives the address
    back = base58.b58encode_check(bytes([0x35]) + bytes.fromhex(form[2:])[::-1]).decode()
    assert back == kp.address


def test_n3_address_validation():
    with pytest.raises(SendError):
        bridge.n3_address_to_neox_form("not-an-address")
    evm = w.evm_generate().address
    with pytest.raises(SendError):
        bridge.n3_address_to_neox_form(evm)


def test_neox_address_to_hash160_string():
    evm = w.evm_generate().address
    assert bridge.neox_address_to_hash160_string(evm) == evm.lower()
    for bad in ("0x1234", "nope", "0x" + "zz" * 20, ""):
        with pytest.raises((SendError, ValueError)):
            bridge.neox_address_to_hash160_string(bad)


# --------------------------------------------------------------- reading limits
class FakeNeoRpc:
    def __init__(self, values, outcomes=None):
        self.values = values  # method -> int | bool
        self.outcomes = list(outcomes or [])

    async def call(self, method, params=None):
        assert method == "invokefunction"
        value = self.values[params[1]]
        item = {"type": "Boolean", "value": value} if isinstance(value, bool) else {"type": "Integer", "value": str(value)}
        return {"state": "HALT", "stack": [item]}

    async def get_tx_outcome(self, tx_hash, expect_true=True):
        assert expect_true is False  # the bridge deposit returns nothing
        return self.outcomes.pop(0) if self.outcomes else None


N3_VALUES = {
    "nativeDepositFee": GAS // 10,
    "minNativeDeposit": GAS,
    "maxNativeDeposit": 1000 * GAS,
    "isPaused": False,
    "depositsArePaused": False,
}


async def test_read_n3_limits():
    lim = await bridge.read_n3_limits(FakeNeoRpc(N3_VALUES), bridge.N3_BRIDGE["testnet"])
    assert lim == bridge.Limits(fee=GAS // 10, min_amount=GAS, max_amount=1000 * GAS, paused=False)
    paused = await bridge.read_n3_limits(FakeNeoRpc({**N3_VALUES, "depositsArePaused": True}), "0x1")
    assert paused.paused


class FakeXRpc:
    def __init__(self, scaling=10, paused=False, bridge_paused=False, withdrawals_paused=False,
                 fee=WEI // 10, low=WEI, high=1000 * WEI, receipt=None, fail_estimate=False):
        self.scaling, self.paused = scaling, paused
        self.flags = {"bridgePaused()": bridge_paused, "withdrawalsPaused()": withdrawals_paused}
        self.cfg = (fee, low, high)
        self.receipt = receipt
        self.fail_estimate = fail_estimate
        self.sent = []
        self.estimated = []

    async def call(self, method, params=None):
        assert method == "eth_call"
        data = params[0]["data"]
        for sig, flag in self.flags.items():
            if data == "0x" + bridge._sel(sig):
                return "0x" + int(flag).to_bytes(32, "big").hex()
        assert data == "0x" + bridge._sel("nativeBridge()")
        words = [int(self.paused), 5, 0, 3, 0, *self.cfg, 50, self.scaling]
        return "0x" + "".join(x.to_bytes(32, "big").hex() for x in words)

    async def estimate_gas(self, tx):
        self.estimated.append(tx)
        if self.fail_estimate:
            raise RuntimeError("execution reverted")
        return 100_000

    async def gas_price(self):
        return 10**9

    async def get_nonce(self, address):
        return 4

    async def send_raw(self, raw):
        self.sent.append(raw)
        return "0x" + "cd" * 32

    async def get_receipt(self, tx_hash):
        return self.receipt


async def test_read_neox_limits():
    lim = await bridge.read_neox_limits(FakeXRpc())
    assert lim == bridge.Limits(fee=WEI // 10, min_amount=WEI, max_amount=1000 * WEI, paused=False)
    for kwargs in ({"paused": True}, {"bridge_paused": True}, {"withdrawals_paused": True}):
        assert (await bridge.read_neox_limits(FakeXRpc(**kwargs))).paused


async def test_unknown_decimal_scaling_disables_the_bridge():
    with pytest.raises(SendError):
        await bridge.read_neox_limits(FakeXRpc(scaling=0))


# -------------------------------------------------------------------- the plans
N3_LIMITS = bridge.Limits(fee=GAS // 10, min_amount=GAS, max_amount=100 * GAS, paused=False)
X_LIMITS = bridge.Limits(fee=WEI // 10, min_amount=WEI, max_amount=100 * WEI, paused=False)


def test_plan_n3_to_x_rules():
    plan = bridge.plan_n3_to_x(5 * GAS, N3_LIMITS, 6 * GAS)
    assert plan.amount8 == 5 * GAS and plan.fee == GAS // 10 and plan.max_fee == GAS // 10
    with pytest.raises(SendError, match="minimum"):
        bridge.plan_n3_to_x(GAS // 2, N3_LIMITS, 50 * GAS)
    with pytest.raises(SendError, match="maximum"):
        bridge.plan_n3_to_x(101 * GAS, N3_LIMITS, 500 * GAS)
    with pytest.raises(SendError, match="You need"):
        bridge.plan_n3_to_x(5 * GAS, N3_LIMITS, 5 * GAS)  # nothing left for the fee
    with pytest.raises(SendError, match="paused"):
        bridge.plan_n3_to_x(5 * GAS, bridge.Limits(1, GAS, 100 * GAS, True), 50 * GAS)


def test_plan_x_to_n3_rules():
    fee = 10**15
    plan = bridge.plan_x_to_n3(5 * GAS, X_LIMITS, 6 * WEI, fee)
    assert plan.amount8 == 5 * GAS and plan.fee == WEI // 10
    with pytest.raises(SendError, match="minimum"):
        bridge.plan_x_to_n3(GAS // 2, X_LIMITS, 50 * WEI, fee)  # 0.5 GAS < 1 GAS minimum
    with pytest.raises(SendError, match="You need"):
        bridge.plan_x_to_n3(5 * GAS, X_LIMITS, 5 * WEI, fee)


def test_max_amounts():
    assert bridge.max_amount8_n3_to_x(N3_LIMITS, 20 * GAS) == 20 * GAS - GAS // 10 - neo_send.MIN_GAS_RESERVE
    assert bridge.max_amount8_n3_to_x(N3_LIMITS, 500 * GAS) == 100 * GAS  # capped by the bridge maximum
    assert bridge.max_amount8_n3_to_x(N3_LIMITS, 0) == 0
    got = bridge.max_amount8_x_to_n3(X_LIMITS, 20 * WEI, 10**15)
    assert got * bridge.SCALE <= 20 * WEI - X_LIMITS.fee - 10**15 < (got + 1) * bridge.SCALE


# ------------------------------------------------------------ Neo X withdrawal
def test_withdraw_call_data_layout():
    kp = w.neo_n3_generate()
    form = bridge.n3_address_to_neox_form(kp.address)
    data = bridge.encode_withdraw_native(form, 12345)
    assert data.startswith("0x" + bridge._sel("withdrawNative(address,uint256)"))
    body = data[2 + 8 :]
    assert len(body) == 128
    assert body[24:64] == form[2:]  # the address word is left-padded with 12 zero bytes
    assert int(body[64:], 16) == 12345


async def test_withdrawal_is_signed_for_the_bridge_with_amount_plus_fee():
    kp, n3 = w.evm_generate(), w.neo_n3_generate()
    plan = bridge.plan_x_to_n3(2 * GAS, X_LIMITS, 50 * WEI, 10**15)
    rpc = FakeXRpc(receipt={"status": "0x1"})
    out = await bridge.run_x_to_n3(
        x_rpc=rpc, chain_id=47763, private_key=kp.private_key, from_addr=kp.address,
        to_n3_address=n3.address, plan=plan, interval=0.001,
    )
    assert out.status == "confirmed" and out.tx_hash == "0x" + "cd" * 32
    assert Account.recover_transaction(rpc.sent[0]) == kp.address
    est = rpc.estimated[0]  # the dry run used the exact value and destination
    assert est["to"] == bridge.NEOX_BRIDGE
    assert int(est["value"], 16) == 2 * WEI + WEI // 10
    assert est["data"] == bridge.encode_withdraw_native(bridge.n3_address_to_neox_form(n3.address), WEI // 10)


async def test_withdrawal_refused_by_dry_run_sends_nothing():
    kp, n3 = w.evm_generate(), w.neo_n3_generate()
    plan = bridge.plan_x_to_n3(2 * GAS, X_LIMITS, 50 * WEI, 10**15)
    rpc = FakeXRpc(fail_estimate=True)
    with pytest.raises(SendError):
        await bridge.run_x_to_n3(
            x_rpc=rpc, chain_id=47763, private_key=kp.private_key, from_addr=kp.address,
            to_n3_address=n3.address, plan=plan,
        )
    assert rpc.sent == []


# ------------------------------------------------------------- Neo N3 deposit
async def test_deposit_passes_the_right_arguments_and_waits_for_the_result():
    calls = []

    async def depositor(private_key, bridge_hash, to, amount8, max_fee):
        calls.append((bridge_hash, to, amount8, max_fee))
        return "0xABCD"

    plan = bridge.plan_n3_to_x(5 * GAS, N3_LIMITS, 20 * GAS)
    evm = w.evm_generate().address
    rpc = FakeNeoRpc(N3_VALUES, outcomes=[None, (True, "")])
    out = await bridge.run_n3_to_x(
        neo_rpc=rpc, depositor=depositor, bridge_hash=bridge.N3_BRIDGE["testnet"], private_key=b"k",
        to_neox_address=evm, plan=plan, interval=0.001,
    )
    assert calls == [(bridge.N3_BRIDGE["testnet"], evm.lower(), 5 * GAS, GAS // 10)]
    assert out == SendOutcome("confirmed", "0xabcd", "")


async def test_deposit_errors_become_user_messages():
    async def boom(*args):
        raise RuntimeError("rpc exploded")

    plan = bridge.plan_n3_to_x(5 * GAS, N3_LIMITS, 20 * GAS)
    with pytest.raises(SendError, match="did not accept"):
        await bridge.run_n3_to_x(
            neo_rpc=FakeNeoRpc(N3_VALUES), depositor=boom, bridge_hash="0x1", private_key=b"k",
            to_neox_address=w.evm_generate().address, plan=plan,
        )
