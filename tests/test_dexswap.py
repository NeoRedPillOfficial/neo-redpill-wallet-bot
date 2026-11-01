from decimal import Decimal

from bot import dexquote, dexswap
from bot.neo_send import SendError

PANCAKE = dexquote.BY_KEY["pancake"]
TOK = "0x2222222222222222222222222222222222222222"
ME = "0x3333333333333333333333333333333333333333"
NATIVE = 10**18


def test_swap_calldata_layout_matches_the_abi():
    path = [PANCAKE.wrapped, TOK]
    buy = dexswap.encode_swap_buy(1234, path, ME, 99999)
    assert buy.startswith("0xb6f9de95")
    b = buy[10:]
    assert [int(b[i * 64:(i + 1) * 64], 16) for i in (0, 1, 3, 4)] == [1234, 128, 99999, 2]
    assert b[128:192].endswith(ME.removeprefix("0x"))  # the receiver
    assert b[320:384].endswith(PANCAKE.wrapped.lower().removeprefix("0x")) and b[384:448].endswith(TOK.removeprefix("0x"))
    sell = dexswap.encode_swap_sell(777, 1234, path, ME, 99999)
    assert sell.startswith("0x791ac947")
    s = sell[10:]
    assert [int(s[i * 64:(i + 1) * 64], 16) for i in (0, 1, 2, 4, 5)] == [777, 1234, 160, 99999, 2]
    ap = dexswap.encode_approve(PANCAKE.router, 5)
    assert ap.startswith("0x095ea7b3") and int(ap[-64:], 16) == 5 and len(ap) == 10 + 128
    assert dexswap.encode_allowance(ME, PANCAKE.router).startswith("0xdd62ed3e")


class FakeChain:
    """Records what is sent. `allowance` and balances are settable; estimate_gas can be made to revert."""

    def __init__(self, native=NATIVE * 10, tokens=10**24, allowance=0, revert_swap=False, wrapped=None):
        self.native, self.tokens, self.allowance = native, tokens, allowance
        self.revert_swap, self.sent, self.estimates = revert_swap, [], []
        self.wrapped = (wrapped or PANCAKE.wrapped).lower().removeprefix("0x")

    async def _eth_call(self, to, data):
        if data.startswith("0xad5c4648"):
            return "0x" + "0" * 24 + self.wrapped
        if data.startswith("0x70a08231"):
            return hex(self.tokens)
        if data.startswith("0xdd62ed3e"):
            return hex(self.allowance)
        raise RuntimeError("unexpected call")

    async def gas_price(self): return 3 * 10**9
    async def get_balance(self, addr): return self.native
    async def get_nonce(self, addr): return len(self.sent)

    async def estimate_gas(self, tx):
        from bot.evm_rpc import EvmRpcError
        self.estimates.append(tx)
        if self.revert_swap and tx.get("data", "").startswith("0x791ac947") or self.revert_swap and tx.get("data", "").startswith("0xb6f9de95"):
            raise EvmRpcError("execution reverted: PancakeRouter: INSUFFICIENT_OUTPUT_AMOUNT")
        return 150_000

    async def send_raw(self, raw):
        self.sent.append(raw)
        return "0x" + f"{len(self.sent):064x}"

    async def get_receipt(self, h): return {"status": "0x1"}


def _quote(buying):
    return dexquote.build_quote(dex=PANCAKE, buying=buying, amount_in=NATIVE if buying else 1000 * 10**18,
                                amount_out=1000 * 10**18 if buying else NATIVE, small_in=1, small_out=1, in_decimals=18,
                                out_decimals=18, slippage_pct=Decimal(1), pay_symbol="BNB" if buying else "Z",
                                receive_symbol="Z" if buying else "BNB", route=["BNB", "Z"],
                                path=[PANCAKE.wrapped, TOK] if buying else [TOK, PANCAKE.wrapped])


async def test_buy_sends_one_transaction_with_the_coin_as_value():
    rpc = FakeChain()
    outcome, approve = await dexswap.swap(rpc, 56, b"k" * 32, ME, _quote(True), TOK, "BNB")
    assert outcome.status == "confirmed" and approve is None and len(rpc.sent) == 1
    assert rpc.estimates[-1]["value"] == hex(NATIVE)  # the network dry-ran it with the amount


async def test_sell_approves_exactly_the_amount_first_then_swaps():
    rpc = FakeChain(allowance=0)
    outcome, approve = await dexswap.swap(rpc, 56, b"k" * 32, ME, _quote(False), TOK, "BNB")
    assert approve and outcome.status == "confirmed" and len(rpc.sent) == 2
    assert rpc.estimates[0]["data"].startswith("0x095ea7b3")
    assert int(rpc.estimates[0]["data"][-64:], 16) == 1000 * 10**18  # exactly the amount, never unlimited
    assert rpc.estimates[1]["data"].startswith("0x791ac947")


async def test_no_approval_is_sent_when_the_allowance_is_enough():
    rpc = FakeChain(allowance=10**30)
    _outcome, approve = await dexswap.swap(rpc, 56, b"k" * 32, ME, _quote(False), TOK, "BNB")
    assert approve is None and len(rpc.sent) == 1


async def _refused(rpc, buying, text):
    try:
        await dexswap.swap(rpc, 56, b"k" * 32, ME, _quote(buying), TOK, "BNB")
    except SendError as exc:
        assert text in str(exc), str(exc)
        assert rpc.sent == [] or text == "approval", "nothing may be sent"
        return
    raise AssertionError("should have been refused")


async def test_refusals_happen_before_anything_is_sent():
    await _refused(FakeChain(native=NATIVE // 2), True, "balance is too low")  # cannot pay the amount plus fee
    await _refused(FakeChain(tokens=5), False, "don't have that much")
    await _refused(FakeChain(native=1000, allowance=10**30), False, "network fee")
    await _refused(FakeChain(wrapped=dexquote.BY_KEY["uniswap"].wrapped), True, "could not verify")  # the wrong router
    await _refused(FakeChain(revert_swap=True), True, "would fail")  # the network's dry run says the swap reverts
    rpc = FakeChain(revert_swap=True, allowance=10**30)
    await _refused(rpc, False, "price moved too much")
