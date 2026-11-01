"""Token logos for the photo card.

Where a logo comes from, in order: a file you put in logos/custom/ (SYMBOL.png), a fixed link for a few
tokens (NRP), CoinGecko for the built-in coins (GAS, NEO, BNB, ETH, ...), then Trust Wallet's public asset
list and DexScreener for tokens by contract address. Logos are matched by the exact contract, never by name.
Downloaded logos are kept in the logos/ folder, so each is fetched once. A token with no logo gets the
colored letter circle. Home never waits more than a few seconds for logos: missing ones keep downloading
in the background and show on the next refresh.
"""
import asyncio
import io
import logging
import os
import time

import aiohttp

from . import homecard, market, prices, tls

log = logging.getLogger("walletbot.logos")

CACHE_DIR = "logos"
CUSTOM_DIR = os.path.join(CACHE_DIR, "custom")
BUNDLED_DIR = os.path.join(os.path.dirname(__file__), "assets", "logos")  # logos shipped with the bot (SYMBOL.png)
SIZE = 256
OK_DAYS, NONE_DAYS = 14, 1
MAX_BYTES = 1_000_000
TRUST_CHAIN = {
    "bsc": "smartchain", "eth": "ethereum", "polygon": "polygon", "arbitrum": "arbitrum", "optimism": "optimism",
    "base": "base", "avalanche": "avalanchec", "solana": "solana",
}
TW_ASSET = "https://raw.githubusercontent.com/trustwallet/assets/master/blockchains/{chain}/assets/{addr}/logo.png"
TW_INFO = "https://raw.githubusercontent.com/trustwallet/assets/master/blockchains/{chain}/info/logo.png"
DS_LOGO = "https://dd.dexscreener.com/ds-data/tokens/{chain}/{addr}.png"
COINGECKO_MARKETS = "https://api.coingecko.com/api/v3/coins/markets"
KNOWN = {  # Neo N3 contract -> logo link (put a better NRP.png in logos/custom/ to override)
    "0x789518aa302b571e3e825f2c85a01ad731014a45": ["https://neoredpill.com/wp-content/uploads/2025/01/NeoRedPill_Logo_50x50px.png"],
}
_GECKO: dict = {"at": 0.0, "images": {}}
_GECKO_LOCK = asyncio.Lock()  # one CoinGecko call at a time, so many logos at once can't trip its rate limit
TRANSIENT_NONE_SECONDS = 600  # a lookup that failed because of the network is retried after 10 minutes, not a day
_INFLIGHT: dict[str, asyncio.Task] = {}


def candidates(h, gecko: dict[str, str]) -> list[str]:
    """Logo links to try for a holding, best first."""
    urls: list[str] = []
    contract = (h.contract or "")
    low = contract.lower()
    native = not low or low == "native"
    urls += KNOWN.get(low, [])
    symbol = h.symbol.upper()
    if h.is_default and symbol in gecko:
        urls.append(gecko[symbol])
    chain = TRUST_CHAIN.get(h.slug)
    if native and chain:
        urls.append(TW_INFO.format(chain=chain))
    if not native and chain:
        addr = contract
        if h.slug != "solana":
            try:
                from eth_utils import to_checksum_address

                addr = to_checksum_address(contract)
            except Exception:
                addr = contract
        urls.append(TW_ASSET.format(chain=chain, addr=addr))
        ds_chain = market.DEXSCREENER_CHAIN.get(h.slug)
        if ds_chain:
            urls.append(DS_LOGO.format(chain=ds_chain, addr=contract if h.slug == "solana" else low))
    return urls


def _prepare(data: bytes):
    """Image bytes -> a square RGBA picture, or None if it isn't a usable image."""
    from PIL import Image

    try:
        img = Image.open(io.BytesIO(data)).convert("RGBA")
        side = max(img.size)
        square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        square.paste(img, ((side - img.width) // 2, (side - img.height) // 2))
        return square.resize((SIZE, SIZE), Image.LANCZOS)
    except Exception:
        return None


def _mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _fresh(path: str, days: int) -> bool:
    try:
        return time.time() - os.path.getmtime(path) < days * 86400
    except OSError:
        return False


def _read(path: str):
    from PIL import Image

    try:
        with Image.open(path) as img:
            return img.convert("RGBA")
    except Exception:
        return None


_MEMORY: dict[str, tuple[float, object]] = {}  # path -> (file time, image): a logo is decoded once, not on every Home


def _read_cached(path: str):
    try:
        stamp = os.path.getmtime(path)
    except OSError:
        return None
    hit = _MEMORY.get(path)
    if hit and hit[0] == stamp:
        return hit[1]
    img = _read(path)
    if img is not None:
        _MEMORY[path] = (stamp, img)
    return img


def _shared_path(h) -> str | None:
    """Built-in coins keep one logo per symbol, shared by every network that uses it (GAS on Neo N3 and on Neo X)."""
    return os.path.join(CACHE_DIR, f"coin_{h.symbol.upper()}.png") if h.is_default else None


def _remember_shared(h, img) -> None:
    """Keep one copy per built-in coin symbol, so another network's copy of the same coin can use it."""
    shared = _shared_path(h)
    if shared is None or img is None or os.path.exists(shared):
        return
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        img.save(shared)
    except Exception:
        pass


def _from_disk(key: str, h):
    """A logo you provided (logos/custom/SYMBOL.png), or one downloaded earlier."""
    for folder in (CUSTOM_DIR, BUNDLED_DIR):  # your own file wins over the one shipped with the bot
        for name in (f"{h.symbol.upper()}.png", f"{key}.png"):
            path = os.path.join(folder, name)
            if os.path.exists(path):
                img = _read_cached(path)
                if img is not None:
                    return img
    path = os.path.join(CACHE_DIR, f"{key}.png")
    if _fresh(path, OK_DAYS):
        img = _read_cached(path)
        _remember_shared(h, img)  # also covers logos downloaded before the shared copies existed
        return img
    shared = _shared_path(h)
    if shared and _fresh(shared, OK_DAYS):
        return _read_cached(shared)  # another network's copy of the same coin
    return None


async def _download(url: str) -> bytes | None:
    timeout = aiohttp.ClientTimeout(total=5)
    async with aiohttp.ClientSession(timeout=timeout, connector=aiohttp.TCPConnector(ssl=tls.context())) as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                return None
            data = await resp.content.read(MAX_BYTES + 1)
            return data if len(data) <= MAX_BYTES else None


async def _gecko_images(feed) -> dict[str, str]:
    """SYMBOL -> logo link for the built-in coins, one CoinGecko call a day."""
    async with _GECKO_LOCK:
        if _GECKO["images"] and time.monotonic() - _GECKO["at"] < 86400:
            return _GECKO["images"]
        for attempt in range(2):
            try:
                ids = ",".join(prices.COINGECKO_IDS.values())
                data = await feed._get_json(COINGECKO_MARKETS, {"vs_currency": "usd", "ids": ids})
                by_id = {v: k for k, v in prices.COINGECKO_IDS.items()}
                images = {by_id[item["id"]]: item["image"] for item in data if item.get("id") in by_id and item.get("image")}
                if images:
                    _GECKO["images"], _GECKO["at"] = images, time.monotonic()
                    return images
            except Exception as exc:
                log.warning("CoinGecko logos unavailable: %s", exc)
            await asyncio.sleep(1.5)
        return _GECKO["images"]


async def _extra_urls(feed, h) -> list[str]:
    """Logo links found by asking a service about the exact contract: DexScreener's token profile, then CoinGecko's contract page.
    Used when the fixed sources have nothing, so a newly added token still gets its logo when one exists anywhere public."""
    contract = (h.contract or "")
    low = contract.lower()
    if not low or low == "native":
        return []
    urls: list[str] = []
    ds_chain = market.DEXSCREENER_CHAIN.get(h.slug)
    if ds_chain:
        try:
            data = await feed._get_json(f"https://api.dexscreener.com/latest/dex/tokens/{contract}")
            for pair in (data or {}).get("pairs") or []:
                base = pair.get("baseToken") or {}
                same = str(base.get("address", "")).lower() == low if h.slug != "solana" else base.get("address") == contract
                image = (pair.get("info") or {}).get("imageUrl")
                if same and image:
                    urls.append(str(image))
                    break
        except Exception:
            pass
    platform = prices.COINGECKO_PLATFORMS.get(h.slug)
    if platform:
        try:
            addr = contract if h.slug == "solana" else low
            data = await feed._get_json(f"https://api.coingecko.com/api/v3/coins/{platform}/contract/{addr}",
                                        {"localization": "false", "tickers": "false", "market_data": "false",
                                         "community_data": "false", "developer_data": "false", "sparkline": "false"})
            image = (data or {}).get("image") or {}
            for name in ("large", "small", "thumb"):
                if image.get(name):
                    urls.append(str(image[name]))
                    break
        except Exception:
            pass
    return urls


async def _fetch_and_cache(feed, key: str, h):
    os.makedirs(CACHE_DIR, exist_ok=True)
    try:
        gecko = await _gecko_images(feed) if h.is_default else {}
        urls = candidates(h, gecko)
        extra_done = False
        index = 0
        while index < len(urls):
            url = urls[index]
            index += 1
            try:
                data = await _download(url)
            except Exception:
                data = None
            img = _prepare(data) if data else None
            if img is not None:
                img.save(os.path.join(CACHE_DIR, f"{key}.png"))
                shared = _shared_path(h)
                if shared:
                    img.save(shared)
                return img
            if index >= len(urls) and not extra_done:  # the fixed sources are exhausted: ask the services about this contract
                extra_done = True
                urls += await _extra_urls(feed, h)
        with open(os.path.join(CACHE_DIR, f"{key}.none"), "w") as fh:  # nothing found: pause before asking again
            fh.write("")
    except Exception as exc:
        log.warning("Logo for %s failed: %r", h.symbol, exc)
    finally:
        _INFLIGHT.pop(key, None)
    return None


async def load(feed, holdings, wait: float = 3.0) -> dict:
    """{logo key: image} for the holdings that have a logo. Waits at most `wait` seconds for downloads."""
    out: dict = {}
    pending: dict[str, asyncio.Task] = {}
    missing = []
    for h in holdings:  # first pass: everything already on disk (this also makes the shared per-coin copies)
        key = homecard.logo_key(h)
        img = _from_disk(key, h)
        if img is not None:
            out[key] = img
        else:
            missing.append((key, h))
    for key, h in missing:  # second pass: a coin whose twin on another network was just found needs no download
        img = _from_disk(key, h)
        if img is not None:
            out[key] = img
            continue
        none_path = os.path.join(CACHE_DIR, f"{key}.none")
        if h.is_default:  # built-in coins are retried every 10 minutes: their logo source is reliable, a miss is a hiccup
            if time.time() - _mtime(none_path) < TRANSIENT_NONE_SECONDS:
                continue
        elif _fresh(none_path, NONE_DAYS):
            continue
        task = _INFLIGHT.get(key)
        if task is None:
            task = _INFLIGHT[key] = asyncio.create_task(_fetch_and_cache(feed, key, h))
        pending[key] = task
    if pending:
        await asyncio.wait(list(pending.values()), timeout=wait)  # unfinished ones keep going in the background
        for key, task in pending.items():
            if task.done() and not task.cancelled():
                try:
                    img = task.result()
                except Exception:
                    img = None
                if img is not None:
                    out[key] = img
    return out
