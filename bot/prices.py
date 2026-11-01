"""USD prices for the home page.

Sources, in order: Bifinance's public ticker list (it lists NRP), CoinGecko's public price
endpoint, then Binance's public price list for the big coins (GAS, NEO, BNB, ETH, ...).
A token added by contract address that CoinGecko doesn't know is priced from the biggest
DEX pool for that exact contract (DexScreener: PancakeSwap and the other DEXes), if the pool
has real liquidity. The last good price of a coin is kept for a few hours, so one failing
source doesn't blank a price. If everything fails the home page simply shows no prices;
balances never depend on this.

Built-in tokens are priced by symbol. A token a user adds by contract address can
carry any symbol, including a real coin's (a fake "USDT"), so those are priced by
CONTRACT ADDRESS only (CoinGecko; BNB Chain, Ethereum, Polygon, Arbitrum, Optimism, Base, Avalanche). A token that can't be matched by
contract gets no price, never a symbol-matched one.
"""
import logging
import time
import os
import asyncio
from decimal import Decimal

import aiohttp

from . import tls

log = logging.getLogger("walletbot.prices")

BIFINANCE_URL = "https://api.bifinance.com/api/market/v2/all-tickers-and-pairs"
COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"
BINANCE_URL = "https://api.binance.com/api/v3/ticker/price"
DEXSCREENER_URL = "https://api.dexscreener.com/latest/dex/tokens/{address}"
MIN_POOL_LIQUIDITY = Decimal(1000)  # USD: smaller pools are too easy to fake
STALE_AFTER = 6 * 3600  # a coin's last good price is reused for up to 6 hours if every source fails
BINANCE_MIRROR_URL = "https://data-api.binance.vision/api/v3/ticker/price"
CRYPTOCOMPARE_URL = "https://min-api.cryptocompare.com/data/pricemulti"
COINGECKO_TOKEN_URL = "https://api.coingecko.com/api/v3/simple/token_price/{platform}"
COINGECKO_PLATFORMS = {  # bot network slug -> CoinGecko platform
    "bsc": "binance-smart-chain",
    "eth": "ethereum",
    "polygon": "polygon-pos",
    "arbitrum": "arbitrum-one",
    "optimism": "optimistic-ethereum",
    "base": "base",
    "avalanche": "avalanche",
    "solana": "solana",
}
COINGECKO_IDS = {
    "GAS": "gas", "NEO": "neo", "BNB": "binancecoin",
    "ETH": "ethereum", "POL": "polygon-ecosystem-token", "AVAX": "avalanche-2", "SOL": "solana",
    "USDT": "tether", "USDC": "usd-coin", "DAI": "dai",
}
CACHE_SECONDS = 60


def parse_bifinance(data) -> dict[str, Decimal]:
    """SYMBOL -> USDT price, from the all-tickers-and-pairs list."""
    prices: dict[str, Decimal] = {}
    if not isinstance(data, list):
        return prices
    for item in data:
        try:
            if str(item["quote_currency"]).upper() != "USDT":
                continue
            price = Decimal(str(item["last_price"]))
        except Exception:
            continue
        if price > 0:
            prices[str(item["base_currency"]).upper()] = price
    return prices


def parse_coingecko(data) -> dict[str, Decimal]:
    prices: dict[str, Decimal] = {}
    if not isinstance(data, dict):
        return prices
    for symbol, cg_id in COINGECKO_IDS.items():
        try:
            price = Decimal(str(data[cg_id]["usd"]))
        except Exception:
            continue
        if price > 0:
            prices[symbol] = price
    return prices


def parse_binance(data) -> dict[str, Decimal]:
    """SYMBOL -> price, from Binance's [{"symbol": "GASUSDT", "price": "3.9"}] list (USDT pairs only)."""
    prices: dict[str, Decimal] = {}
    if not isinstance(data, list):
        return prices
    for item in data:
        try:
            pair = str(item["symbol"]).upper()
            if not pair.endswith("USDT"):
                continue
            price = Decimal(str(item["price"]))
        except Exception:
            continue
        if price > 0:
            prices[pair[:-4]] = price
    return prices


def parse_cryptocompare(data) -> dict[str, Decimal]:
    """SYMBOL -> USD price, from {"NEO": {"USD": 7.1}, ...}."""
    prices: dict[str, Decimal] = {}
    if not isinstance(data, dict):
        return prices
    for symbol in COINGECKO_IDS:
        try:
            price = Decimal(str(data[symbol]["USD"]))
        except Exception:
            continue
        if price > 0:
            prices[symbol] = price
    return prices


def format_usd(value: Decimal) -> str:
    if value >= 1:
        return f"${value:,.2f}"
    if value == 0:
        return "$0.00"
    return "$" + f"{value:.6f}".rstrip("0").rstrip(".")


class PriceFeed:
    def __init__(self, ttl: int = CACHE_SECONDS):
        self._ttl = ttl
        self._cache: dict[str, Decimal] = {}
        self._fetched = 0.0
        self._retry_after = 0.0
        self._contract_cache: dict[str, tuple[Decimal | None, float]] = {}
        self._last_good: dict[str, tuple[Decimal, float]] = {}
        self.bifinance_symbols: set[str] = set()
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None

    async def _get_json(self, url: str, params: dict | None = None):
        timeout = aiohttp.ClientTimeout(total=5)
        headers = {}
        key = os.getenv("COINGECKO_API_KEY", "").strip()
        if key and "coingecko.com" in url:
            headers["x-cg-demo-api-key"] = key  # free "demo" key: a higher limit than no key
        async with aiohttp.ClientSession(timeout=timeout, headers=headers, connector=aiohttp.TCPConnector(ssl=tls.context())) as session:
            async with session.get(url, params=params) as resp:
                resp.raise_for_status()
                return await resp.json(content_type=None)

    async def prices(self) -> dict[str, Decimal]:
        """Prices from memory at once. A stale list is returned immediately while one refresh runs in the background,
        so no person ever waits on the web (and a crowd of people never makes a crowd of requests)."""
        age = time.monotonic() - self._fetched
        if self._cache and age < self._ttl:
            return self._cache
        if not self._cache:  # first ever load: wait for it
            await self._refresh()
            return self._cache
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._refresh())
        return self._cache

    async def _refresh(self) -> None:
        if self._lock.locked():  # another refresh is already running: wait for it instead of repeating it
            async with self._lock:
                return
        async with self._lock:
            if time.monotonic() < self._retry_after and self._cache:  # a feed just refused us: do not hammer it
                return
            await self._load()

    async def _load(self) -> None:
        async def bifi():
            return parse_bifinance(await self._get_json(BIFINANCE_URL))

        async def gecko():
            params = {"ids": ",".join(COINGECKO_IDS[s] for s in COINGECKO_IDS), "vs_currencies": "usd"}
            return parse_coingecko(await self._get_json(COINGECKO_URL, params))

        async def binance():
            # The whole public list (a list of named coins fails completely if even one of them is not listed any more).
            last: Exception | None = None
            for url in (BINANCE_URL, BINANCE_MIRROR_URL):
                try:
                    wanted = parse_binance(await self._get_json(url))
                    return {k: v for k, v in wanted.items() if k in COINGECKO_IDS}
                except Exception as exc:
                    last = exc
            raise last

        async def cryptocompare():
            data = await self._get_json(CRYPTOCOMPARE_URL, {"fsyms": ",".join(COINGECKO_IDS), "tsyms": "USD"})
            return parse_cryptocompare(data)

        results = await asyncio.gather(bifi(), gecko(), binance(), cryptocompare(), return_exceptions=True)  # all at once
        found: dict[str, Decimal] = {}
        names = ("Bifinance", "CoinGecko", "Binance", "CryptoCompare")
        for name, result in zip(names, results):
            if isinstance(result, Exception):
                log.warning("%s prices unavailable: %s", name, result)
        if not isinstance(results[0], Exception):
            found.update(results[0])
            self.bifinance_symbols = set(results[0])
        for result in results[1:]:  # order of trust: Bifinance, CoinGecko, Binance, CryptoCompare
            if not isinstance(result, Exception):
                found.update({k: v for k, v in result.items() if k not in found and (k in COINGECKO_IDS)})
        now = time.monotonic()
        for symbol, price in found.items():
            self._last_good[symbol] = (price, now)
        for symbol, (price, when) in self._last_good.items():  # keep a coin's last good price if every source failed it
            if symbol not in found and now - when < STALE_AFTER:
                found[symbol] = price
        if any(s not in found for s in COINGECKO_IDS):
            self._retry_after = time.monotonic() + 120
        if found:
            self._cache, self._fetched = found, time.monotonic()

    async def _contract_price(self, slug: str, contract: str) -> Decimal | None:
        platform = COINGECKO_PLATFORMS.get(slug)
        if platform is None or (slug != "solana" and not contract.startswith("0x")):
            return None
        contract = contract if slug == "solana" else contract.lower()  # Solana addresses are case-sensitive
        key = f"{slug}:{contract}"
        hit = self._contract_cache.get(key)
        # a token with no price is asked about only every 10 minutes, a price every 5: Home must not wait on the web each time
        if hit and time.monotonic() - hit[1] < (300 if hit[0] is not None else 600):
            return hit[0]
        try:
            data = await self._get_json(
                COINGECKO_TOKEN_URL.format(platform=platform),
                {"contract_addresses": contract, "vs_currencies": "usd"},
            )
            entry = data[contract] if contract in data else next(iter(data.values()))  # asked for exactly one
            price = Decimal(str(entry["usd"]))
            price = price if price > 0 else None
        except Exception:
            price = None
        if price is None:  # CoinGecko doesn't know it (or is busy): the biggest real DEX pool for this exact contract
            price = await self._dex_price(slug, contract)
        self._contract_cache[key] = (price, time.monotonic())
        return price

    async def _dex_price(self, slug: str, contract: str) -> Decimal | None:
        from . import market

        try:
            data = await self._get_json(DEXSCREENER_URL.format(address=contract))
            pairs = market.parse_dexscreener(data, slug, contract)
        except Exception:
            return None
        for pair in pairs:  # biggest liquidity first
            if pair.price is not None and pair.liquidity >= MIN_POOL_LIQUIDITY:
                return pair.price
        return None

    async def price_of(self, token, chain_slug: str) -> Decimal | None:
        """USD price of one whole token, or None if there is no trustworthy price."""
        if token.is_default:
            price = (await self.prices()).get(token.symbol.upper())
            if price is not None or str(getattr(token, "contract", "native")).lower() == "native":
                return price
        return await self._contract_price(chain_slug, token.contract)

    async def usd_value(self, token, chain_slug: str, raw_amount: int | None) -> Decimal | None:
        if raw_amount is None:
            return None
        price = await self.price_of(token, chain_slug)
        if price is None:
            return None
        return Decimal(raw_amount) / (Decimal(10) ** token.decimals) * price
