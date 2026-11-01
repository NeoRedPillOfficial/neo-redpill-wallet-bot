from decimal import Decimal

from bot import dexquote, menustrip

PANCAKE = dexquote.BY_KEY["pancake"]
UNI = dexquote.BY_KEY["uniswap"]
TOK = "0x2222222222222222222222222222222222222222"


def test_get_amounts_out_encoding_matches_the_abi():
    data = dexquote.encode_get_amounts_out(10**18, [PANCAKE.wrapped, PANCAKE.stable])
    assert data.startswith("0xd06ca61f")
    body = data[10:]
    assert len(body) == 64 * 5
    assert int(body[:64], 16) == 10**18  # amountIn
    assert int(body[64:128], 16) == 64  # offset of the array
    assert int(body[128:192], 16) == 2  # two addresses
    assert body[192:256].endswith(PANCAKE.wrapped.lower().removeprefix("0x"))


def test_router_answer_is_decoded_and_junk_is_refused():
    raw = "0x" + f"{32:064x}" + f"{2:064x}" + f"{1000:064x}" + f"{2500:064x}"
    assert dexquote.decode_amounts(raw) == [1000, 2500]
    for bad in ("0x", "", "0x" + "00" * 40):
        try:
            dexquote.decode_amounts(bad)
        except ValueError:
            continue
        raise AssertionError("should refuse " + bad)


def test_routes_buying_selling_and_native():
    assert dexquote.candidate_paths(PANCAKE, TOK, True)[0] == [PANCAKE.wrapped, TOK]
    assert dexquote.candidate_paths(PANCAKE, TOK, False)[0] == [TOK, PANCAKE.wrapped]
    assert dexquote.candidate_paths(PANCAKE, TOK, True)[1] == [PANCAKE.wrapped, PANCAKE.stable, TOK]
    assert dexquote.candidate_paths(PANCAKE, "native", True) == []  # the coin for itself is not a trade


def test_dexes_are_found_by_chain_and_dexscreener_id_without_repeats():
    assert [d.key for d in dexquote.dexes_for("bsc", ["pancakeswap", "pancakeswap-v3"])] == ["pancake", "pancake3"]
    assert [d.key for d in dexquote.dexes_for("bsc", ["pancakeswap-v3"])] == ["pancake3"]
    assert [d.key for d in dexquote.dexes_for("eth", ["uniswap", "sushiswap"])] == ["uniswap", "uni3_eth", "uni4_eth", "sushi_eth"]
    assert [d.key for d in dexquote.dexes_for("polygon", ["quickswap", "sushiswap"])] == ["quick", "sushi_poly"]
    assert dexquote.dexes_for("bsc", ["quickswap"]) == []  # a DEX that is not on that network
    assert dexquote.dexes_for("solana", ["raydium"]) == []


def test_every_router_has_a_distinct_valid_address_and_the_right_fee():
    keys = [d.key for d in dexquote.DEXES]
    assert len(keys) == len(set(keys))
    for d in dexquote.DEXES:
        for a in (d.router, d.wrapped, d.stable):
            assert len(a) == 42 and a.startswith("0x") and int(a, 16) > 0
    assert PANCAKE.fee == Decimal("0.0025") and UNI.fee == Decimal("0.003")


def test_price_impact_and_minimum_received():
    assert dexquote.price_impact(1000, 900, 10, 10) == Decimal(10)  # 10% worse than the tiny-trade price
    assert dexquote.price_impact(1000, 1000, 10, 10) == 0
    assert dexquote.price_impact(0, 5, 1, 1) == 0
    assert dexquote.min_out(10_000, Decimal(1)) == 9_900
    assert dexquote.min_out(10_000, Decimal("0.5")) == 9_950
    q = dexquote.build_quote(dex=PANCAKE, buying=True, amount_in=10**18, amount_out=2_000 * 10**18, small_in=10**15,
                             small_out=2 * 10**18, in_decimals=18, out_decimals=18, slippage_pct=Decimal(1),
                             pay_symbol="BNB", receive_symbol="ZSWAP", route=["BNB", "ZSWAP"], path=[PANCAKE.wrapped, TOK])
    assert q.receive == 2000 and q.min_receive == Decimal(1980) and q.min_out_raw == 1980 * 10**18
    assert q.lp_fee == Decimal("0.0025")
    assert q.impact_pct == 0


class FakeRpc:
    """A tiny pool: 1 coin = 1000 tokens (minus the fee), only through the wrapped coin."""

    def __init__(self, dex, pool=True):
        self.dex, self.pool = dex, pool

    async def _eth_call(self, to, data):
        body = data[10:]
        if data.startswith("0x" + dexquote.SEL_WETH):
            return "0x" + "0" * 24 + self.dex.wrapped.lower().removeprefix("0x")
        amount_in, count = int(body[:64], 16), int(body[128:192], 16)
        path = ["0x" + body[192 + 64 * i + 24: 256 + 64 * i] for i in range(count)]
        if not self.pool or len(path) != 2:
            raise RuntimeError("execution reverted")
        buying = path[0] == self.dex.wrapped.lower()
        out = amount_in * 997 // 1000 * 1000 if buying else amount_in * 997 // 1000 // 1000
        return "0x" + f"{32:064x}" + f"{2:064x}" + f"{amount_in:064x}" + f"{out:064x}"


async def test_quote_through_a_pool_and_without_one():
    q = await dexquote.quote(FakeRpc(PANCAKE), PANCAKE, TOK, "ZSWAP", 18, "BNB", Decimal("0.5"), True, Decimal(1), 3 * 10**9)
    assert q.route == ["BNB", "ZSWAP"] and q.receive_symbol == "ZSWAP" and q.path == [PANCAKE.wrapped, TOK]
    assert q.receive > 490 and q.gas_native == Decimal(dexquote.SWAP_GAS_UNITS * 3 * 10**9) / Decimal(10**18)
    sell = await dexquote.quote(FakeRpc(PANCAKE), PANCAKE, TOK, "ZSWAP", 18, "BNB", Decimal("1000"), False)
    assert sell.pay_symbol == "ZSWAP" and sell.receive_symbol == "BNB" and sell.receive < 1
    assert await dexquote.quote(FakeRpc(PANCAKE, pool=False), PANCAKE, TOK, "ZSWAP", 18, "BNB", Decimal(1), True) is None


async def test_a_router_that_does_not_report_the_expected_wrapped_coin_is_refused():
    assert await dexquote.verify_router(FakeRpc(PANCAKE), PANCAKE)
    assert not await dexquote.verify_router(FakeRpc(UNI), PANCAKE)  # answers with another chain's wrapped coin

    class Broken:
        async def _eth_call(self, to, data):
            raise RuntimeError("no contract")
    assert not await dexquote.verify_router(Broken(), PANCAKE)


def test_the_quote_message_has_every_number_the_trader_asked_for():
    q = dexquote.build_quote(dex=PANCAKE, buying=True, amount_in=5 * 10**17, amount_out=1_500 * 10**18, small_in=5 * 10**14,
                             small_out=int(1.6 * 10**18), in_decimals=18, out_decimals=18, slippage_pct=Decimal(1),
                             pay_symbol="BNB", receive_symbol="ZSWAP", route=["BNB", "ZSWAP"], path=[PANCAKE.wrapped, TOK],
                             gas_native=Decimal("0.0007"))
    text = dexquote.render_quote(q, Decimal(1), "BNB", Decimal(600), Decimal("0.2"), "BNB Chain")
    for needle in ("PancakeSwap", "BNB Chain", "You pay", "0.5 BNB", "You receive", "ZSWAP", "Minimum received (1% slippage)",
                   "Trade fee: 0.25%", "Price impact", "Network fee", "$", "Route: BNB → ZSWAP"):
        assert needle in text, needle


def test_trade_and_bridge_menu_label_is_still_a_menu_button():
    class B:
        text, callback_data = "🔁 Trade & Bridge", "tb"
    assert menustrip.is_menu_button(B())


def test_a_tiny_trade_and_a_hostile_symbol_cannot_break_telegram_html():
    q = dexquote.build_quote(dex=PANCAKE, buying=False, amount_in=10**12, amount_out=10**9, small_in=10**9, small_out=10**6,
                             in_decimals=18, out_decimals=18, slippage_pct=Decimal(1), pay_symbol="<b>Z", receive_symbol="BNB",
                             route=["<b>Z", "BNB"], path=[TOK, PANCAKE.wrapped])
    text = dexquote.render_quote(q, Decimal(1), "BNB", Decimal(600), Decimal("0.0001"), "BNB Chain")
    assert "(< " not in text and "&lt; $0.01" in text  # "<" is escaped, so Telegram accepts the message
    assert "&lt;b&gt;Z" in text
