from decimal import Decimal as D

from bot import market, prices

CONTRACT = "0x" + "ab" * 20


class Tok:
    def __init__(self, symbol, contract, is_default=False):
        self.symbol, self.contract, self.is_default = symbol, contract, is_default


DEX = {"pairs": [
    {"chainId": "bsc", "dexId": "pancakeswap", "url": "https://dexscreener.com/bsc/p1", "priceUsd": "0.5",
     "baseToken": {"address": CONTRACT.upper().replace("0X", "0x")}, "liquidity": {"usd": 50000}, "volume": {"h24": 1200}},
    {"chainId": "bsc", "dexId": "pancakeswap", "url": "https://dexscreener.com/bsc/p2", "priceUsd": "0.49",
     "baseToken": {"address": CONTRACT}, "liquidity": {"usd": 900}, "volume": {"h24": 5}},
    {"chainId": "ethereum", "dexId": "uniswap", "url": "u", "priceUsd": "9", "baseToken": {"address": CONTRACT},
     "liquidity": {"usd": 10 ** 6}},  # other network: ignored
    {"chainId": "bsc", "dexId": "biswap", "url": "b", "priceUsd": "0.5", "baseToken": {"address": "0x" + "cd" * 20},
     "liquidity": {"usd": 10 ** 6}},  # another token with a pool: ignored (not our contract)
]}


def test_dexscreener_matches_exact_contract_network_and_keeps_best_pool_per_dex():
    pairs = market.parse_dexscreener(DEX, "bsc", CONTRACT)
    assert [p.dex for p in pairs] == ["PancakeSwap"]
    assert pairs[0].liquidity == D(50000) and pairs[0].price == D("0.5")
    assert market.parse_dexscreener({"nope": 1}, "bsc", CONTRACT) == []
    assert market.parse_dexscreener(DEX, "neo_n3", CONTRACT) == []


def test_cex_list_drops_dexes_stale_and_duplicates():
    data = {"tickers": [
        {"market": {"name": "Binance", "identifier": "binance"}, "converted_volume": {"usd": 900}},
        {"market": {"name": "PancakeSwap v2", "identifier": "pancakeswap_new"}, "converted_volume": {"usd": 5000}},
        {"market": {"name": "Gate.io", "identifier": "gate"}, "converted_volume": {"usd": 100}},
        {"market": {"name": "Binance", "identifier": "binance"}, "converted_volume": {"usd": 10}},
        {"market": {"name": "OldEx", "identifier": "old"}, "converted_volume": {"usd": 99999}, "is_stale": True},
    ]}
    assert [c.name for c in market.parse_coingecko_exchanges(data)] == ["Binance", "Gate.io"]


def test_binance_parser():
    assert prices.parse_binance([{"symbol": "GASUSDT", "price": "3.9"}, {"symbol": "GASBTC", "price": "1"},
                                 {"symbol": "X", "price": "?"}]) == {"GAS": D("3.9")}


def test_render_shows_contract_and_how_to():
    info = market.TradeInfo(dex=market.parse_dexscreener(DEX, "bsc", CONTRACT), cex=[market.Cex("Binance")], price=D("0.5"))
    text = market.render(Tok("ABC", CONTRACT), "bsc", "BNB Smart Chain", info)
    assert CONTRACT in text and "PancakeSwap" in text and "Binance" in text and "buy ABC" in text
    buttons = market.trade_buttons(Tok("ABC", CONTRACT), "bsc", info)
    assert buttons[0][1].startswith("https://pancakeswap.finance/swap") and CONTRACT in buttons[0][1]
    assert any("poocoin.app/tokens/" + CONTRACT in url for _, url in buttons)


def test_native_coin_has_no_contract_and_neo_n3_lists_its_dexes():
    text = market.render(Tok("BNB", "native", True), "bsc", "BNB Smart Chain", market.TradeInfo(cex=[market.Cex("Binance")]))
    assert "no contract address" in text
    n3 = market.render(Tok("NRP", "0x" + "11" * 20, True), "neo_n3", "Neo N3", market.TradeInfo(bifinance=True))
    assert "FTW Swap" in n3 and "Flamingo" in n3 and "Bifinance" in n3
    nrp = "0x789518aa302b571e3e825f2c85a01ad731014a45"
    urls = [u for _, u in market.trade_buttons(Tok("NRP", nrp, True), "neo_n3", market.TradeInfo())]
    assert ("https://www.forthewin.network/swap?tokenA=0xd2a4cff31913016155e38e474a2c06d08be276cf&tokenB=" + nrp) in urls
    assert "https://flamingo.finance/trade/NRP_FUSD" in urls


def test_gas_gets_cex_links_from_coingecko_tickers():
    data = {"tickers": [
        {"base": "GAS", "target": "USDT", "market": {"name": "Binance", "identifier": "binance"},
         "trade_url": "https://www.binance.com/en/trade/GAS_USDT?ref=x", "converted_volume": {"usd": 900}},
        {"base": "GAS", "target": "USDT", "market": {"name": "KuCoin", "identifier": "kucoin"},
         "trade_url": "http://insecure", "converted_volume": {"usd": 500}},
    ]}
    cex = market.parse_coingecko_exchanges(data)
    assert cex[0].url.startswith("https://www.binance.com/") and cex[0].pair == "GAS/USDT" and cex[1].url == ""
    text = market.render(Tok("GAS", "0xd2a4cff31913016155e38e474a2c06d08be276cf", True), "neo_n3", "Neo N3",
                         market.TradeInfo(cex=cex))
    assert 'href="https://www.binance.com/en/trade/GAS_USDT?ref=x"' in text and "KuCoin" in text
    assert "tokenA=0xef4073a0f2b305a38ec4050e4d3d28bc40ea63f5&amp;tokenB=0xd2a4cff" in text or "tokenB=0xd2a4cff" in text
