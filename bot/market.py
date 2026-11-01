"""The 📈 Trade page: where a token the person already holds can be bought or sold.

Matching is by CONTRACT ADDRESS, never by symbol alone, so a fake token with a real coin's name is not
listed. Sources: DexScreener (every DEX pool for that exact contract: PancakeSwap, Uniswap, ...),
CoinGecko (exchange listings for that exact contract or coin), Bifinance (its public ticker list), plus
fixed links for Neo N3 (FTW Swap, Flamingo). Poocoin has no public API, so BNB Chain tokens get a
chart link to it. Nothing here moves money: the bot only shows where to trade.

The pure functions (parsing, formatting) have no network access, so they can be tested.
"""
import html
import os
import time
from dataclasses import dataclass, field
from decimal import Decimal

from . import prices

DEXSCREENER_CHAIN = {  # bot network -> DexScreener chainId
    "bsc": "bsc", "eth": "ethereum", "polygon": "polygon", "arbitrum": "arbitrum", "optimism": "optimism",
    "base": "base", "avalanche": "avalanche", "solana": "solana", "neo_x": "neox",
}
COINGECKO_COIN_URL = "https://api.coingecko.com/api/v3/coins/{path}"
COINGECKO_TICKERS_URL = "https://api.coingecko.com/api/v3/coins/{coin_id}/tickers"  # every market, biggest volume first
COINGECKO_DETAIL = {"localization": "false", "tickers": "true", "market_data": "false", "community_data": "false",
                    "developer_data": "false", "sparkline": "false"}
DEX_NAMES = {
    "pancakeswap": "PancakeSwap", "uniswap": "Uniswap", "sushiswap": "SushiSwap", "quickswap": "QuickSwap",
    "raydium": "Raydium", "orca": "Orca", "aerodrome": "Aerodrome", "traderjoe": "Trader Joe",
    "camelot": "Camelot", "velodrome": "Velodrome", "biswap": "BiSwap", "apeswap": "ApeSwap",
}
DEX_WORDS = ("swap", "dex", "uniswap", "pancake", "sushi", "curve", "balancer", "quickswap", "raydium", "orca",
             "jupiter", "aerodrome", "velodrome", "camelot", "trader joe", "traderjoe", "1inch", "kyber", "apeswap",
             "biswap", "meteora", "flamingo")
SWAP_LINKS = {  # dexId -> link with the token filled in
    "pancakeswap": "https://pancakeswap.finance/swap?chain=bsc&outputCurrency={addr}",
    "uniswap": "https://app.uniswap.org/swap?outputCurrency={addr}",
}
# Neo N3 contract hashes used to build FTW Swap links (the link format comes from neoredpill.com/how-to-buy-nrp)
NEO_HASH = "0xef4073a0f2b305a38ec4050e4d3d28bc40ea63f5"
GAS_HASH = "0xd2a4cff31913016155e38e474a2c06d08be276cf"
FTW_SWAP_URL = "https://www.forthewin.network/swap?tokenA={a}&tokenB={b}"
FTW_SWAP_HOME = "https://www.forthewin.network/swap"
KNOWN_DEX_LINKS = {  # exact trade pages found for a token: neoredpill.com/nrp-token
    "0x789518aa302b571e3e825f2c85a01ad731014a45": [("Flamingo", "https://flamingo.finance/trade/NRP_FUSD")],
}
FLAMINGO_HOME = "https://flamingo.finance"
BIFINANCE_URL = "https://www.bifinance.com"  # Bifinance publishes no per-coin link we could verify


def bifinance_url(symbol: str) -> str:
    """Bifinance's trading page for a coin. Set BIFINANCE_TRADE_URL in .env to a link with {symbol} in it
    (for example https://www.bifinance.com/trade/{symbol}_USDT) once the exact format is known."""
    template = os.getenv("BIFINANCE_TRADE_URL", "").strip()
    if template.startswith("https://"):
        try:
            return template.format(symbol=symbol.upper())
        except (KeyError, IndexError, ValueError):
            pass
    return BIFINANCE_URL
CACHE_SECONDS = 600
_CACHE: dict[str, tuple[float, "TradeInfo"]] = {}


@dataclass(frozen=True)
class Pair:
    dex: str  # nice DEX name
    dex_id: str
    url: str  # DexScreener page of the pool
    price: Decimal | None
    liquidity: Decimal
    volume24h: Decimal
    versions: tuple = ()  # DexScreener's version labels for this DEX's pools ("v2", "v3", "v4"), when it gives them


@dataclass(frozen=True)
class Cex:
    name: str
    url: str = ""  # the exchange's own trading page for this coin, when CoinGecko gives one
    pair: str = ""  # e.g. GAS/USDT


@dataclass
class TradeInfo:
    dex: list[Pair] = field(default_factory=list)
    cex: list["Cex"] = field(default_factory=list)
    bifinance: bool = False
    price: Decimal | None = None
    price_source: str = ""
    errors: list[str] = field(default_factory=list)  # sources that did not answer


def _dec(value) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal(0)


def same_address(a: str, b: str, slug: str) -> bool:
    return a == b if slug == "solana" else a.lower() == b.lower()  # Solana addresses are case-sensitive


def parse_dexscreener(data, slug: str, contract: str) -> list[Pair]:
    """Pools where THIS contract is the traded (base) token, on this network, biggest liquidity first,
    one per DEX."""
    chain = DEXSCREENER_CHAIN.get(slug)
    pairs = data.get("pairs") if isinstance(data, dict) else None
    if chain is None or not isinstance(pairs, list):
        return []
    best: dict[str, Pair] = {}
    labels: dict[str, set] = {}
    for item in pairs:
        try:
            if str(item.get("chainId", "")).lower() != chain:
                continue
            if not same_address(str(item["baseToken"]["address"]), contract, slug):
                continue
            dex_id = str(item.get("dexId", "")).lower() or "dex"
            labels.setdefault(dex_id, set()).update(str(x).lower() for x in (item.get("labels") or []))
            liquidity = _dec((item.get("liquidity") or {}).get("usd"))
            price = _dec(item.get("priceUsd"))
            pair = Pair(
                dex=DEX_NAMES.get(dex_id, dex_id.replace("-", " ").title()), dex_id=dex_id,
                url=str(item.get("url") or ""), price=price if price > 0 else None, liquidity=liquidity,
                volume24h=_dec((item.get("volume") or {}).get("h24")),
            )
        except Exception:
            continue
        if dex_id not in best or pair.liquidity > best[dex_id].liquidity:
            best[dex_id] = pair
    import dataclasses
    return sorted((dataclasses.replace(p, versions=tuple(sorted(labels.get(i, ())))) for i, p in best.items()),
                  key=lambda p: -p.liquidity)


def is_dex_name(name: str) -> bool:
    low = name.lower()
    return any(word in low for word in DEX_WORDS)


def parse_coingecko_exchanges(data) -> list[Cex]:
    """Centralized exchanges that list the coin (with the trading page and pair), biggest volume first."""
    tickers = data.get("tickers") if isinstance(data, dict) else None
    if not isinstance(tickers, list):
        return []
    rows = []
    for t in tickers:
        try:
            market = t["market"]
            name = str(market["name"])
            ident = str(market.get("identifier", ""))
            if t.get("is_stale") or t.get("is_anomaly") or is_dex_name(name) or is_dex_name(ident):
                continue
            url = str(t.get("trade_url") or "")
            if not url.startswith("https://"):
                url = ""
            pair = f"{t.get('base', '')}/{t.get('target', '')}".strip("/")
            rows.append((_dec((t.get("converted_volume") or {}).get("usd")), Cex(name, url, pair)))
        except Exception:
            continue
    rows.sort(key=lambda r: -r[0])
    out: list[Cex] = []
    seen: set[str] = set()
    for _, cex in rows:
        if cex.name not in seen:
            seen.add(cex.name)
            out.append(cex)
    return out


def ftw_swap_link(token_contract: str) -> str:
    """FTW Swap's own link for this token: paired with GAS (GAS itself is paired with NEO)."""
    contract = token_contract.lower()
    if contract == GAS_HASH:
        return FTW_SWAP_URL.format(a=NEO_HASH, b=GAS_HASH)
    return FTW_SWAP_URL.format(a=GAS_HASH, b=contract)


def neo_n3_dex_links(token) -> list[tuple[str, str]]:
    contract = str(token.contract).lower()
    links = [("FTW Swap", ftw_swap_link(contract))]
    links += KNOWN_DEX_LINKS.get(contract) or [("Flamingo", FLAMINGO_HOME)]
    return links


def is_native(token) -> bool:
    return str(token.contract).lower() == "native"


def contract_text(token, slug: str) -> str | None:
    """The contract address to show, or None for a network's own coin (it has none)."""
    if is_native(token):
        return None
    return str(token.contract)


def swap_link(pair: Pair, contract: str) -> str:
    template = SWAP_LINKS.get(pair.dex_id)
    return template.format(addr=contract) if template else pair.url


def cex_line(c: Cex) -> str:
    name = html.escape(c.name)
    label = f'<a href="{html.escape(c.url, quote=True)}">{name}</a>' if c.url else name
    return f"• {label}" + (f"  ·  {html.escape(c.pair)}" if c.pair else "")


def money(value: Decimal) -> str:
    if value >= 1_000_000:
        return f"${value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"${value / 1_000:.1f}K"
    return f"${value:.0f}"


def explorer_note(slug: str) -> str:
    return {"solana": "Solana", "neo_n3": "Neo N3", "neo_x": "Neo X"}.get(slug, "")


def render(token, slug: str, network: str, info: TradeInfo) -> str:
    sym = html.escape(token.symbol)
    contract = contract_text(token, slug)
    lines = [f"📈 <b>Trade {sym}</b>  ·  {html.escape(network)}", ""]
    if contract:
        lines += ["<b>Contract address</b> (tap to copy):", f"<code>{html.escape(contract)}</code>", ""]
    else:
        lines += [f"{sym} is the main coin of {html.escape(network)}, so it has no contract address.", ""]
    if info.price is not None:
        src = f" ({html.escape(info.price_source)})" if info.price_source else ""
        lines += [f"💰 Price: <b>{prices.format_usd(info.price)}</b>{src}", ""]

    cex = list(info.cex)
    if info.bifinance and not any(c.name.lower().startswith("bifinance") for c in cex):
        cex.insert(0, Cex("Bifinance", bifinance_url(token.symbol), f"{token.symbol}/USDT"))
    lines.append("🏦 <b>Exchanges (CEX)</b>")
    if cex:
        lines.append("\n".join(cex_line(c) for c in cex[:8]))
    elif "CoinGecko" in info.errors:
        lines.append("<i>I couldn't check the exchanges right now. Try again in a minute.</i>")
    else:
        lines.append("<i>None found for this token.</i>")
    lines.append("")

    lines.append("🔄 <b>DEX</b>")
    if info.dex:
        for pair in info.dex[:5]:
            lines.append(f"• {html.escape(pair.dex)}  ·  liquidity {money(pair.liquidity)}  ·  24h volume {money(pair.volume24h)}")
    elif slug == "neo_n3":
        dexes = "\n".join(f'• <a href="{html.escape(u, quote=True)}">{html.escape(n)}</a>' for n, u in neo_n3_dex_links(token))
        lines.append(dexes + "\n<i>I can't check automatically whether this token has a pool there. If a page shows no pool, search the contract address above.</i>")
    else:
        lines.append("<i>No DEX pool found for this exact contract.</i>")
    lines.append("")

    lines.append("🧭 <b>How to trade it</b>")
    lines.extend(how_to(token, slug, network, bool(info.dex), bool(cex)))
    if info.errors:
        lines += ["", "⚠️ <i>Could not reach: " + ", ".join(html.escape(e) for e in info.errors) + ". The list may be incomplete.</i>"]
    lines += ["", "⚠️ <i>Only trade the contract address shown above. Different tokens can share a name, and fakes are common.</i>"]
    return "\n".join(lines)


def how_to(token, slug: str, network: str, has_dex: bool, has_cex: bool) -> list[str]:
    sym = html.escape(token.symbol)
    net = html.escape(network)
    if is_native(token) or token.contract.lower() == "native":
        return [f"1. Buy or sell {sym} on an exchange above, or swap it on a DEX of {net}.",
                "2. To sell on an exchange, deposit from <b>📥 Deposit</b> using the right network."]
    if has_dex:
        steps = [f"1. Open the DEX button below and connect a wallet on <b>{net}</b>.",
                 f"2. To <b>buy {sym}</b>: pick the token you pay with, and paste the contract address above as the token to receive.",
                 f"3. To <b>sell {sym}</b>: choose {sym} (paste the contract address if it isn't listed) and swap it for a coin you want.",
                 "4. Check the price and slippage on the DEX before you confirm."]
        if has_cex:
            steps.append("• Or use an exchange above: deposit with <b>📥 Deposit</b> on the right network.")
        return steps
    if slug == "neo_n3":
        return ["1. Open FTW Swap or Flamingo below with a Neo N3 wallet.",
                f"2. Search for {sym} by pasting the contract address above. If it isn't found, it has no pool there yet.",
                "3. Swap against GAS or another token you hold, and check the amount before you confirm."]
    if has_cex:
        return [f"1. Trade {sym} on an exchange above.",
                "2. To sell there, send it with <b>💸 Send</b> to the exchange's deposit address for the same network."]
    return ["I found no trading place for this exact contract yet. It may be new or not listed anywhere."]


def trade_buttons(token, slug: str, info: TradeInfo) -> list[tuple[str, str]]:
    """(label, url) link buttons: swap links first, then charts."""
    contract = contract_text(token, slug)
    out: list[tuple[str, str]] = []
    if contract:
        for pair in info.dex[:3]:
            link = swap_link(pair, contract)
            if link:
                out.append((f"🔄 {pair.dex}", link))
        if slug == "bsc":
            out.append(("📊 Poocoin chart", f"https://poocoin.app/tokens/{contract}"))
        if info.dex and info.dex[0].url:
            out.append(("📈 DexScreener chart", info.dex[0].url))
    if slug == "neo_n3" and contract:
        out += [(f"🔄 {name}", url) for name, url in neo_n3_dex_links(token)]
    if info.bifinance:
        out.append(("🏦 Bifinance", bifinance_url(token.symbol)))
    return out


async def lookup(feed, token, slug: str) -> TradeInfo:
    """Ask the sources (cached for 10 minutes). `feed` is the bot's PriceFeed (it owns the HTTP session logic)."""
    key = f"{slug}:{token.contract}:{token.symbol}"
    hit = _CACHE.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    import asyncio

    info = TradeInfo()
    contract = contract_text(token, slug)
    tasks = {}
    if contract and slug in DEXSCREENER_CHAIN:
        tasks["DexScreener"] = feed._get_json(f"https://api.dexscreener.com/latest/dex/tokens/{contract}")
    coin_id = prices.COINGECKO_IDS.get(token.symbol.upper())
    if coin_id and token.is_default:
        # built-in coins (GAS, NEO, BNB, ETH, ...), native or not: the coin's own list of markets
        tasks["CoinGecko"] = feed._get_json(COINGECKO_TICKERS_URL.format(coin_id=coin_id), {"order": "volume_desc", "depth": "false"})
    elif contract and slug in prices.COINGECKO_PLATFORMS and (slug == "solana" or contract.startswith("0x")):
        addr = contract if slug == "solana" else contract.lower()
        tasks["CoinGecko"] = feed._get_json(
            COINGECKO_COIN_URL.format(path=f"{prices.COINGECKO_PLATFORMS[slug]}/contract/{addr}"), COINGECKO_DETAIL)
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    for name, result in zip(tasks, results):
        if isinstance(result, Exception):
            info.errors.append(name)
            continue
        if name == "DexScreener":
            info.dex = parse_dexscreener(result, slug, contract)
        else:
            info.cex = parse_coingecko_exchanges(result)
    # Bifinance: its own tickers have no contract, so a symbol match is trusted only for the built-in tokens
    if token.is_default:
        try:
            await feed.prices()
            info.bifinance = token.symbol.upper() in feed.bifinance_symbols
        except Exception:
            info.errors.append("Bifinance")
    price = await feed.price_of(token, slug)
    if price is not None:
        info.price = price
        info.price_source = "Bifinance, CoinGecko or Binance" if token.is_default else "CoinGecko or the biggest DEX pool"
    elif info.dex and info.dex[0].price is not None and info.dex[0].liquidity >= prices.MIN_POOL_LIQUIDITY:
        info.price, info.price_source = info.dex[0].price, info.dex[0].dex
    if not info.errors:  # don't cache a half answer
        _CACHE[key] = (time.monotonic(), info)
    return info
