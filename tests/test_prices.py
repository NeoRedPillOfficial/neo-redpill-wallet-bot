from decimal import Decimal

from bot import prices


class T:
    def __init__(self, symbol, decimals, is_default):
        self.symbol, self.decimals, self.is_default = symbol, decimals, is_default


BIFINANCE = [
    {"base_currency": "NRP", "quote_currency": "USDT", "last_price": "0.05"},
    {"base_currency": "BNB", "quote_currency": "ETH", "last_price": "0.2"},  # not a USDT pair: ignored
    {"base_currency": "BAD", "quote_currency": "USDT", "last_price": "oops"},
    {"base_currency": "ZERO", "quote_currency": "USDT", "last_price": "0"},
    {"nonsense": True},
]


def test_parse_bifinance_keeps_only_valid_usdt_pairs():
    assert prices.parse_bifinance(BIFINANCE) == {"NRP": Decimal("0.05")}
    assert prices.parse_bifinance({"not": "a list"}) == {}


def test_parse_coingecko():
    data = {"gas": {"usd": 4.5}, "neo": {"usd": 11}, "binancecoin": {}}
    assert prices.parse_coingecko(data) == {"GAS": Decimal("4.5"), "NEO": Decimal("11")}


def test_format_usd():
    assert prices.format_usd(Decimal("1234.5")) == "$1,234.50"
    assert prices.format_usd(Decimal("0.0512")) == "$0.0512"
    assert prices.format_usd(Decimal("0")) == "$0.00"


async def test_prices_by_symbol_for_builtin_and_by_contract_for_added():
    import time

    feed = prices.PriceFeed()
    feed._cache, feed._fetched = {"GAS": Decimal("4"), "USDT": Decimal("1")}, time.monotonic()
    usdt = T("USDT", 18, False)
    usdt.contract = "0x" + "ab" * 20
    usdt_cached = (Decimal("1.001"), time.monotonic())
    assert await feed.price_of(T("GAS", 8, True), "neo_n3") == Decimal("4")
    assert await feed.usd_value(T("GAS", 8, True), "neo_n3", 250_000_000) == Decimal("10")
    assert await feed.price_of(T("NEO", 0, True), "neo_n3") is None
    assert await feed.usd_value(T("GAS", 8, True), "neo_n3", None) is None
    # A user-added token is NEVER priced by symbol, even though "USDT" is in the symbol table.
    async def no_network(url, params=None):
        raise RuntimeError("offline")

    feed._get_json = no_network
    assert await feed.price_of(usdt, "bsc") is None
    # ...but a contract-matched price is used.
    feed._contract_cache[f"bsc:{usdt.contract}"] = usdt_cached
    assert await feed.price_of(usdt, "bsc") == Decimal("1.001")
    # Networks CoinGecko can't price by contract (Neo X, Neo N3) give nothing.
    assert await feed.price_of(usdt, "neo_x") is None
