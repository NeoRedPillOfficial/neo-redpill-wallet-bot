import asyncio
from decimal import Decimal

from bot import ftwswap


class FakeRpc:
    """A node whose FTW pool pays 0.5 GAS for every 1 token (fee included): selling X tokens gives X/2 GAS."""

    def __init__(self, held_ok=True):
        self.calls = []
        self.held_ok = held_ok

    async def call(self, method, params):
        self.calls.append(params)
        amount_out, max_in = int(params[2][3]["value"]), int(params[2][4]["value"])
        tokens_in = amount_out * 2 * 10 ** 6 // 10 ** 8  # 6-decimal token: 1 token = 10**6 raw, pays 0.5 GAS = 5*10**7
        if tokens_in > max_in or not self.held_ok:
            return {"state": "FAULT"}
        return {"state": "HALT", "gasconsumed": "100", "notifications": [{
            "contract": ftwswap.SWAP_CONTRACT, "eventname": "Swap",
            "state": {"value": [{"value": "a"}, {"value": "b"}, {"value": "c"}, {"value": str(tokens_in)},
                                {"value": str(amount_out)}, {"value": "1"}]}}]}


def test_the_price_in_gas_comes_from_a_tiny_dry_run_sale():
    price = asyncio.run(ftwswap.sell_price_in_gas(FakeRpc(), "0x" + "11" * 20, "0x" + "22" * 20, 6, 5 * 10 ** 6))
    assert price == Decimal("0.5")


def test_no_balance_or_no_pool_means_no_price():
    assert asyncio.run(ftwswap.sell_price_in_gas(FakeRpc(), "0x" + "11" * 20, "0x" + "22" * 20, 6, 0)) is None
    assert asyncio.run(ftwswap.sell_price_in_gas(FakeRpc(held_ok=False), "0x" + "11" * 20, "0x" + "22" * 20, 6, 10 ** 9)) is None


def test_a_holding_too_small_for_the_first_probe_uses_a_smaller_one():
    price = asyncio.run(ftwswap.sell_price_in_gas(FakeRpc(), "0x" + "11" * 20, "0x" + "22" * 20, 6, 5 * 10 ** 3))
    assert price == Decimal("0.5")
