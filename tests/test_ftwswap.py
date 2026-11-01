import asyncio
from decimal import Decimal

from bot import ftwswap as f

GAS = f.GAS_HASH
NRP = "0x789518aa302b571e3e825f2c85a01ad731014a45"
ME = "0xdf087737766ab82919d1ec3455ac3d83ee1bad0b"


def test_the_real_wallet_address_gives_the_real_script_hash():
    # taken from the decoded FTW Swap transaction of this wallet
    assert f.script_hash("NLyhzhRbLqDU2jfrPY7QKpWKi6gyuzDcQJ") == ME


def test_arguments_follow_the_order_of_the_real_transaction():
    args = f.build_args(ME, GAS, NRP, 1_000_000_000, 31775, 1792766623854)
    assert [a["type"] for a in args] == ["Hash160", "Hash160", "Hash160", "Integer", "Integer", "Integer"]
    assert [a["value"] for a in args] == [ME, GAS, NRP, "1000000000", "31775", "1792766623854"]
    assert f.SWAP_CONTRACT == "0x997ced5777a3f66485d66828bda3864b8c8bdf95" and f.METHOD == "swapB"


def _event(amount_in, amount_out, fee):
    ints = [{"type": "Integer", "value": str(v)} for v in (amount_in, amount_out, fee)]
    return {"contract": f.SWAP_CONTRACT, "eventname": "Swap", "state": {"type": "Array", "value": [
        {"type": "ByteString", "value": "x"}, {"type": "ByteString", "value": "y"}, {"type": "ByteString", "value": "z"}] + ints}}


def test_the_swap_event_of_the_real_transaction_is_understood():
    got = f.parse_simulation({"state": "HALT", "gasconsumed": "1234567", "notifications": [_event(28887, 1000000000, 72)]})
    assert (got.amount_in, got.amount_out, got.fee, got.system_fee) == (28887, 1000000000, 72, 1234567)


def test_failed_or_unrelated_dry_runs_are_not_a_quote():
    assert f.parse_simulation({"state": "FAULT", "notifications": [_event(1, 1, 0)]}) is None
    assert f.parse_simulation({"state": "HALT", "notifications": []}) is None
    other = _event(1, 1, 0); other["contract"] = "0x" + "11" * 20
    assert f.parse_simulation({"state": "HALT", "notifications": [other]}) is None
    assert f.parse_simulation(None) is None and f.parse_simulation({"state": "HALT", "notifications": [{"contract": f.SWAP_CONTRACT, "eventname": "Swap", "state": {"value": []}}]}) is None


class Pool:
    """A constant-product pool (0.25% fee) answering invokefunction like the node: it pays `out`, charging `in`."""

    def __init__(self, r_in, r_out, alive=True):
        self.r_in, self.r_out, self.alive, self.calls = r_in, r_out, alive, 0

    async def call(self, method, params):
        assert method == "invokefunction" and params[0] == f.SWAP_CONTRACT and params[1] == "swapB"
        self.calls += 1
        a = params[2]
        out, max_in = int(a[3]["value"]), int(a[4]["value"])
        if not self.alive or out <= 0 or out >= self.r_out:
            return {"state": "FAULT", "notifications": []}
        cost = -(-self.r_in * out * 10000 // ((self.r_out - out) * 9975))  # rounded up
        if cost > max_in:
            return {"state": "FAULT", "notifications": []}
        fee = cost * 25 // 10000
        return {"state": "HALT", "gasconsumed": "1000000", "notifications": [_event(cost, out, fee)]}


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def test_a_quote_spends_just_under_the_amount_and_shows_the_price_impact():
    pool = Pool(r_in=10_000 * 10**8, r_out=50_000_000 * 10**8)
    spend = 5 * 10**8  # 5 GAS
    q = run(f.quote(pool, ME, GAS, NRP, "GAS", "NRP", 8, 8, spend, Decimal(1), True))
    assert q is not None and q.amount_in_raw <= spend and q.amount_in_raw > spend * 0.995  # fits, and uses nearly all of it
    assert q.receive > 24_000  # about 5 GAS * 5000 NRP/GAS, a little less after the fee
    assert q.max_in_raw >= q.amount_in_raw * 1.0099 and q.max_in_raw <= q.amount_in_raw * 1.0101 + 1
    assert Decimal(0) <= q.impact_pct < 2
    assert pool.calls < 40  # does not hammer the node


def test_no_pool_or_a_spend_too_small_for_the_pool_gives_no_quote():
    assert run(f.quote(Pool(1, 1, alive=False), ME, GAS, NRP, "GAS", "NRP", 8, 8, 10**8, Decimal(1), True)) is None
    assert run(f.quote(Pool(10**12, 10**15), ME, GAS, NRP, "GAS", "NRP", 8, 8, 0, Decimal(1), True)) is None


def test_the_quote_message_has_everything_the_trader_asked_for():
    pool = Pool(r_in=10_000 * 10**8, r_out=50_000_000 * 10**8)
    q = run(f.quote(pool, ME, GAS, NRP, "GAS", "NRP", 8, 8, 5 * 10**8, Decimal(1), True))
    text = f.render_quote(q, Decimal(1), Decimal("5"), Decimal("0.001"))
    for needle in ("FTW Swap", "You receive", "NRP", "You pay", "GAS", "1% slippage", "Trade fee: 0.25%", "Price impact", "Network fee", "$"):
        assert needle in text, needle


def test_max_in_rounds_up():
    assert f.max_in_for(28887, Decimal(10)) == 31776  # 31775.7 rounds up, never down
    assert f.max_in_for(100, Decimal(1)) == 101


class Flaky(Pool):
    """Every third call to the node fails to answer at all (a busy public node)."""

    async def call(self, method, params):
        n = getattr(self, "n", 0) + 1
        self.n = n
        if n % 3 == 0:
            raise TimeoutError("node busy")
        return await super().call(method, params)


class Capped(Pool):
    """A pool that refuses to give out more than `cap` of the token in one swap."""

    def __init__(self, *a, cap, **k):
        super().__init__(*a, **k)
        self.cap = cap

    async def call(self, method, params):
        if int(params[2][3]["value"]) > self.cap:
            return {"state": "FAULT", "notifications": []}
        return await super().call(method, params)


def test_a_busy_node_does_not_shrink_the_quote():
    spend = 1000 * 10**8
    q = run(f.quote(Flaky(r_in=2_000_000 * 10**8, r_out=60 * 10**8), ME, NRP, GAS, "NRP", "GAS", 8, 8, spend, Decimal(1), False))
    assert q is not None and q.amount_in_raw <= spend and q.amount_in_raw > spend * 0.99  # the whole amount is used


def test_scaling_up_finds_the_whole_amount_even_when_the_search_stopped_early():
    pool = Pool(r_in=2_000_000 * 10**8, r_out=60 * 10**8)
    spend = 1000 * 10**8
    q = run(f.quote(pool, ME, NRP, GAS, "NRP", "GAS", 8, 8, spend, Decimal(1), False))
    assert q.amount_in_raw > spend * 0.995


def test_a_pool_that_fills_only_part_says_so_and_never_exceeds_the_amount():
    spend = 1000 * 10**8
    pool = Capped(r_in=2_000_000 * 10**8, r_out=60 * 10**8, cap=int(0.0257 * 10**8) )
    q = run(f.quote(pool, ME, NRP, GAS, "NRP", "GAS", 8, 8, spend, Decimal(1), False))
    assert q is not None and q.amount_in_raw < spend * 0.98 and q.amount_in_raw <= spend
    text = f.render_quote(q, Decimal(1), None, None, spend)
    assert "only part of it" in text and "stays in your wallet" in text


def test_a_node_that_never_answers_is_an_error_not_a_zero_quote():
    class Down:
        async def call(self, *a):
            raise TimeoutError("down")
    try:
        run(f.quote(Down(), ME, GAS, NRP, "GAS", "NRP", 8, 8, 10**8, Decimal(1), True))
    except f.NodeUnavailable:
        return
    raise AssertionError("an unreachable node must not look like 'no pool'")


def test_the_price_limit_never_goes_above_what_the_wallet_holds():
    bal = 225_220_58 * 10**6  # the whole balance
    budget = f.budget_within_balance(bal, bal, Decimal(1))
    assert budget < bal
    assert f.max_in_for(budget, Decimal(1)) <= bal  # price + slippage fits inside the balance
    assert f.budget_within_balance(1000, 10**9, Decimal(1)) == 1000  # a small amount is untouched
    assert f.budget_within_balance(500, None, Decimal(1)) == 500  # balance unknown: unchanged
    assert f.max_in_for(f.budget_within_balance(10**8, 10**8, Decimal(3)), Decimal(3)) <= 10**8


def test_the_typed_amount_is_the_most_you_can_ever_pay():
    for typed in (1000 * 10**8, 10**8, 12345, 7, 999_999_999_999):
        for slip in (Decimal("0.5"), Decimal(1), Decimal(3)):
            budget = f.budget_for(typed, slip)
            assert 0 <= budget <= typed
            assert f.max_in_for(budget, slip) <= typed  # price + slippage never goes above what was typed
    assert f.budget_for(0, Decimal(1)) == 0


def test_a_full_quote_stays_under_the_typed_amount_even_at_the_price_limit():
    pool = Pool(r_in=2_000_000 * 10**8, r_out=60 * 10**8)
    typed = 1000 * 10**8
    q = run(f.quote(pool, ME, NRP, GAS, "NRP", "GAS", 8, 8, f.budget_for(typed, Decimal(1)), Decimal(1), False))
    assert q.amount_in_raw <= typed and q.max_in_raw <= typed
    assert q.max_in_raw > typed * 0.99  # and it still uses nearly all of it
