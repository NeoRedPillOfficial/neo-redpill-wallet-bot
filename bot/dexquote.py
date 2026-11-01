"""Quotes for swaps on Uniswap-V2-style DEXes (PancakeSwap, Uniswap V2, SushiSwap, QuickSwap): what you would receive,
the trade fee and the price impact.

A quote only READS the router contract (an eth_call); it spends nothing and needs no signature. The encoding and the
arithmetic are plain functions with no network access, so they can be tested. The swap itself is in dexswap.py.

Only tokens listed on a DEX can be traded, and only a swap between the network's own coin and the token (buy with the
coin, or sell for the coin). Tokens that charge their own tax on every trade are not corrected for: a quote cannot know.
"""
import logging
from dataclasses import dataclass
from decimal import Decimal

log = logging.getLogger("walletbot.dex")


@dataclass(frozen=True)
class Dex:
    key: str  # short id used in buttons, e.g. "pancake"
    name: str  # what people see
    chain: str  # bot network slug
    router: str  # the V2 router contract
    wrapped: str  # wrapped native coin (WBNB, WETH, WMATIC ...)
    stable: str  # a big stablecoin on that chain, used as a second route
    fee: Decimal = Decimal("0.003")  # share of what you pay that the pool keeps
    dex_ids: tuple = ()  # DexScreener dexId prefixes that mean this DEX
    version: int = 2  # 2 = V2 router (getAmountsOut); 3 = V3 (a quoter contract and fee tiers, see dexv3.py)
    quoter: str = ""  # V3: the QuoterV2 contract used for read-only quotes
    factory: str = ""  # V3: the factory the quoter must report, so a wrong address is refused
    fees: tuple = ()  # V3: the pool fee tiers to try, in hundredths of a basis point (500 = 0.05%)


DEXES: list[Dex] = [
    Dex("pancake", "PancakeSwap V2", "bsc", "0x10ED43C718714eb63d5aA57B78B54704E256024E",
        "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c", "0x55d398326f99059fF775485246999027B3197955",
        Decimal("0.0025"), ("pancakeswap",)),
    Dex("uniswap", "Uniswap V2", "eth", "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D",
        "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2", "0xdAC17F958D2ee523a2206206994597C13D831ec7",
        Decimal("0.003"), ("uniswap",)),
    Dex("sushi_eth", "SushiSwap", "eth", "0xd9e1cE17f2641f24aE83637ab66a2cca9C378B9F",
        "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2", "0xdAC17F958D2ee523a2206206994597C13D831ec7",
        Decimal("0.003"), ("sushiswap", "sushi")),
    Dex("quick", "QuickSwap", "polygon", "0xa5E0829CaCEd8fFDD4De3c43696c57F7D7A678ff",
        "0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270", "0xc2132D05D31c914a87C6611C10748AEb04B58e8F",
        Decimal("0.003"), ("quickswap",)),
    Dex("sushi_poly", "SushiSwap", "polygon", "0x1b02dA8Cb0d097eB8D57A175b88c7D8b47997506",
        "0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270", "0xc2132D05D31c914a87C6611C10748AEb04B58e8F",
        Decimal("0.003"), ("sushiswap", "sushi")),
    Dex("sushi_arb", "SushiSwap", "arbitrum", "0x1b02dA8Cb0d097eB8D57A175b88c7D8b47997506",
        "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1", "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9",
        Decimal("0.003"), ("sushiswap", "sushi")),
    Dex("sushi_bsc", "SushiSwap", "bsc", "0x1b02dA8Cb0d097eB8D57A175b88c7D8b47997506",
        "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c", "0x55d398326f99059fF775485246999027B3197955",
        Decimal("0.003"), ("sushiswap", "sushi")),
    Dex("biswap", "BiSwap", "bsc", "0x3a6d8cA21D1CF76F653A67577FA0D27453350dD8",
        "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c", "0x55d398326f99059fF775485246999027B3197955",
        Decimal("0.001"), ("biswap",)),
    Dex("apeswap", "ApeSwap", "bsc", "0xcF0feBd3f17CEf5b47b0cD257aCf6025c5BFf3b7",
        "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c", "0x55d398326f99059fF775485246999027B3197955",
        Decimal("0.002"), ("apeswap",)),
    # V3 pools. Router, quoter and factory are checked on-chain before every swap (verify_router), so a wrong address is refused.
    Dex("uni3_eth", "Uniswap V3", "eth", "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45",
        "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2", "0xdAC17F958D2ee523a2206206994597C13D831ec7",
        Decimal("0.003"), ("uniswap",), 3, "0x61fFE014bA17989E743c5F6cB21bF9697530B21e",
        "0x1F98431c8aD98523631AE4a59f267346ea31F984", (100, 500, 3000, 10000)),
    Dex("uni3_poly", "Uniswap V3", "polygon", "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45",
        "0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270", "0xc2132D05D31c914a87C6611C10748AEb04B58e8F",
        Decimal("0.003"), ("uniswap",), 3, "0x61fFE014bA17989E743c5F6cB21bF9697530B21e",
        "0x1F98431c8aD98523631AE4a59f267346ea31F984", (100, 500, 3000, 10000)),
    Dex("uni3_arb", "Uniswap V3", "arbitrum", "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45",
        "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1", "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9",
        Decimal("0.003"), ("uniswap",), 3, "0x61fFE014bA17989E743c5F6cB21bF9697530B21e",
        "0x1F98431c8aD98523631AE4a59f267346ea31F984", (100, 500, 3000, 10000)),
    Dex("uni3_bsc", "Uniswap V3", "bsc", "0xB971eF87ede563556b2ED4b1C0b0019111Dd85d2",
        "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c", "0x55d398326f99059fF775485246999027B3197955",
        Decimal("0.003"), ("uniswap",), 3, "0x78D78E420Da98ad378D7799bE8f4AF69033EB077",
        "0xdB1d10011AD0Ff90774D0C6Bb92e5C5c8b4461F7", (100, 500, 3000, 10000)),
    # V4 (one PoolManager for all pools; trades go through the Universal Router and Permit2, see dexv4.py). `factory` holds the PoolManager.
    Dex("uni4_eth", "Uniswap V4", "eth", "0x66a9893cC07D91D95644AEDD05D03f95e1dBA8Af",
        "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2", "0xdAC17F958D2ee523a2206206994597C13D831ec7",
        Decimal("0.003"), ("uniswap",), 4, "0x52F0E24D1c21C8A0cB1e5a5dD6198556BD9E1203",
        "0x000000000004444c5dc75cB358380D2e3dE08A90", ()),
    # ---- Base, Optimism, Avalanche (all checked on-chain before a swap, like the others)
    Dex("uni2_base", "Uniswap V2", "base", "0x4752ba5DBc23f44D87826276BF6Fd6b1C372aD24",
        "0x4200000000000000000000000000000000000006", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        Decimal("0.003"), ("uniswap",)),
    Dex("sushi_base", "SushiSwap", "base", "0x6BDED42c6DA8FBf0d2bA55B2fa120C5e0c8D7891",
        "0x4200000000000000000000000000000000000006", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        Decimal("0.003"), ("sushiswap", "sushi")),
    Dex("baseswap", "BaseSwap", "base", "0x327Df1E6de05895d2ab08513aaDD9313Fe505d86",
        "0x4200000000000000000000000000000000000006", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        Decimal("0.0025"), ("baseswap",)),
    Dex("uni3_base", "Uniswap V3", "base", "0x2626664c2603336E57B271c5C0b26F421741e481",
        "0x4200000000000000000000000000000000000006", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        Decimal("0.003"), ("uniswap",), 3, "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a",
        "0x33128a8fC17869897dcE68Ed026d694621f6FDfD", (100, 500, 3000, 10000)),
    Dex("uni4_base", "Uniswap V4", "base", "0x6fF5693b99212Da76ad316178A184AB56D299b43",
        "0x4200000000000000000000000000000000000006", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        Decimal("0.003"), ("uniswap",), 4, "0x0d5e0F971ED27FBfF6c2837bf31316121532048D",
        "0x498581fF718922c3f8e6A244956aF099B2652b2b", ()),
    Dex("pancake3_base", "PancakeSwap V3", "base", "0x678Aa4bF4E210cf2166753e054d5b7c31cc7fa86",
        "0x4200000000000000000000000000000000000006", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        Decimal("0.0025"), ("pancakeswap-v3",), 3, "0xB048Bbc1Ee6b733FFfCFb9e9CeF7375518e25997",
        "0x0BFbCF9fa4f9C56B0F40a671Ad40E0805A091865", (100, 500, 2500, 10000)),
    Dex("uni2_op", "Uniswap V2", "optimism", "0x4A7b5Da61326A6379179b40d00F57E5bbDC962c2",
        "0x4200000000000000000000000000000000000006", "0x94b008aA00579c1307B0EF2c499aD98a8ce58e58",
        Decimal("0.003"), ("uniswap",)),
    Dex("uni3_op", "Uniswap V3", "optimism", "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45",
        "0x4200000000000000000000000000000000000006", "0x94b008aA00579c1307B0EF2c499aD98a8ce58e58",
        Decimal("0.003"), ("uniswap",), 3, "0x61fFE014bA17989E743c5F6cB21bF9697530B21e",
        "0x1F98431c8aD98523631AE4a59f267346ea31F984", (100, 500, 3000, 10000)),
    Dex("uni4_op", "Uniswap V4", "optimism", "0x851116D9223fabED8E56C0E6b8Ad0c31d98B3507",
        "0x4200000000000000000000000000000000000006", "0x94b008aA00579c1307B0EF2c499aD98a8ce58e58",
        Decimal("0.003"), ("uniswap",), 4, "0x1f3131A13296fB91C90870043742C3CDBFF1A8d7",
        "0x9a13F98Cb987694C9F086b1F5eB990EeA8264Ec3", ()),
    Dex("uni2_avax", "Uniswap V2", "avalanche", "0x4752ba5DBc23f44D87826276BF6Fd6b1C372aD24",
        "0xB31f66AA3C1e785363F0875A1B74E27b85FD66c7", "0xB97EF9Ef8734C71904D8002F8b6Bc66Dd9c48a6E",
        Decimal("0.003"), ("uniswap",)),
    Dex("uni3_avax", "Uniswap V3", "avalanche", "0xbb00FF08d01D300023C629E8fFfFcb65A5a578cE",
        "0xB31f66AA3C1e785363F0875A1B74E27b85FD66c7", "0xB97EF9Ef8734C71904D8002F8b6Bc66Dd9c48a6E",
        Decimal("0.003"), ("uniswap",), 3, "0xbe0F5544EC67e9B3b2D979aaA43f18Fd87E6257F",
        "0x740b1c1de25031C31FF4fC9A62f554A55cdC1baD", (100, 500, 3000, 10000)),
    Dex("pancake3", "PancakeSwap V3", "bsc", "0x13f4EA83D0bd40E75C8222255bc855a974568Dd4",
        "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c", "0x55d398326f99059fF775485246999027B3197955",
        Decimal("0.0025"), ("pancakeswap-v3",), 3, "0xB048Bbc1Ee6b733FFfCFb9e9CeF7375518e25997",
        "0x0BFbCF9fa4f9C56B0F40a671Ad40E0805A091865", (100, 500, 2500, 10000)),
]
BY_KEY = {d.key: d for d in DEXES}
SLIPPAGE_CHOICES = (Decimal("0.5"), Decimal("1"), Decimal("3"))  # percent
DEFAULT_SLIPPAGE = Decimal("1")
SWAP_GAS_UNITS = 250_000  # a typical swap; the real number comes from the network when sending


def dexes_for(slug: str, dex_ids, versions: dict | None = None) -> list[Dex]:
    """The DEXes on this network that a token is listed on (from the DexScreener dexIds), in order, no repeats.
    `versions` maps a dexId to DexScreener's pool version labels: a V3 DEX is offered only for V3 pools, and a V2 DEX not
    for a token that has only V3/V4 pools there (when no labels are given, every version of the DEX is offered)."""
    out: list[Dex] = []
    for raw in dex_ids:
        did = str(raw).lower()
        labels = set((versions or {}).get(did, ()) or ())
        for dex in DEXES:
            if dex.chain == slug and any(did.startswith(p) for p in dex.dex_ids) and dex not in out:
                if dex.version == 2 and "-v3" in did:
                    continue  # DexScreener's own id for V3 pools (PancakeSwap): not a V2 pool
                if labels:
                    if dex.version == 3 and "v3" not in labels:
                        continue
                    if dex.version == 4 and "v4" not in labels:
                        continue
                    if dex.version == 2 and "v2" not in labels and labels & {"v3", "v4"}:
                        continue
                out.append(dex)
    return out


# ------------------------------------------------------------------ ABI helpers
def _selector(signature: str, known: str) -> str:
    """The 4-byte function id. Computed when the keccak library is there, and checked against the known value."""
    try:
        from eth_utils import function_signature_to_4byte_selector

        computed = function_signature_to_4byte_selector(signature).hex()
        if computed != known:
            log.error("Selector mismatch for %s: %s vs %s", signature, computed, known)
        return computed
    except Exception:
        return known


SEL_GET_AMOUNTS_OUT = _selector("getAmountsOut(uint256,address[])", "d06ca61f")
SEL_WETH = _selector("WETH()", "ad5c4648")


def word(value: int) -> str:
    return f"{value:064x}"


def addr_word(address: str) -> str:
    return address.lower().removeprefix("0x").rjust(64, "0")


def encode_get_amounts_out(amount_in: int, path: list[str]) -> str:
    """The eth_call data for getAmountsOut(amountIn, path)."""
    data = SEL_GET_AMOUNTS_OUT + word(amount_in) + word(64) + word(len(path))
    return "0x" + data + "".join(addr_word(a) for a in path)


def decode_amounts(result: str) -> list[int]:
    """The uint256[] returned by getAmountsOut."""
    raw = (result or "").removeprefix("0x")
    if len(raw) < 128:
        raise ValueError("the router gave no answer")
    count = int(raw[64:128], 16)
    if count == 0 or len(raw) < 128 + 64 * count:
        raise ValueError("the router answer is incomplete")
    return [int(raw[128 + 64 * i: 192 + 64 * i], 16) for i in range(count)]


def token_address(dex: Dex, contract: str) -> str:
    """The token as the router sees it: the native coin is the wrapped coin."""
    return dex.wrapped if contract.lower() == "native" else contract


def candidate_paths(dex: Dex, token_contract: str, buying: bool) -> list[list[str]]:
    """Routes to try, simplest first: straight through the wrapped coin, then through the stablecoin."""
    t = token_address(dex, token_contract)
    if t.lower() == dex.wrapped.lower():
        return []
    if buying:
        return [[dex.wrapped, t], [dex.wrapped, dex.stable, t]]
    return [[t, dex.wrapped], [t, dex.stable, dex.wrapped]]


@dataclass(frozen=True)
class Quote:
    dex: Dex
    buying: bool
    pay: Decimal  # whole units of what you pay (the coin when buying, the token when selling)
    receive: Decimal  # whole units you get (before slippage)
    min_receive: Decimal
    pay_symbol: str
    receive_symbol: str
    lp_fee: Decimal  # in units of what you pay
    impact_pct: Decimal  # price impact, percent (positive = worse price than the pool's small-trade price)
    route: list[str]  # symbols along the way
    path: list[str]  # the contract addresses along the way
    amount_in_raw: int
    amount_out_raw: int
    min_out_raw: int
    gas_native: Decimal | None = None
    fees: tuple = ()  # V3: the fee tier of each hop of the path
    fee_rate: Decimal | None = None  # the trade fee actually charged (V3 depends on the pool); None = the DEX's usual fee


def price_impact(amount_in: int, amount_out: int, small_in: int, small_out: int) -> Decimal:
    """Percent by which this trade's price is worse than the price for a tiny trade in the same pool."""
    if amount_in <= 0 or amount_out <= 0 or small_in <= 0 or small_out <= 0:
        return Decimal(0)
    this = Decimal(amount_out) / Decimal(amount_in)
    small = Decimal(small_out) / Decimal(small_in)
    return max(Decimal(0), (1 - this / small) * 100)


def min_out(amount_out: int, slippage_pct: Decimal) -> int:
    """The least the swap may pay out before the router refuses it."""
    bps = int((Decimal(slippage_pct) * 100).to_integral_value())
    return amount_out * (10_000 - bps) // 10_000


def build_quote(*, dex: Dex, buying: bool, amount_in: int, amount_out: int, small_in: int, small_out: int,
                in_decimals: int, out_decimals: int, slippage_pct: Decimal, pay_symbol: str, receive_symbol: str,
                route: list[str], path: list[str], gas_native: Decimal | None = None,
                fees: tuple = (), fee_rate: Decimal | None = None) -> Quote:
    pay = Decimal(amount_in) / (Decimal(10) ** in_decimals)
    receive = Decimal(amount_out) / (Decimal(10) ** out_decimals)
    floor = min_out(amount_out, slippage_pct)
    return Quote(
        dex=dex, buying=buying, pay=pay, receive=receive,
        min_receive=Decimal(floor) / (Decimal(10) ** out_decimals),
        pay_symbol=pay_symbol, receive_symbol=receive_symbol,
        lp_fee=pay * (fee_rate if fee_rate is not None else dex.fee),
        impact_pct=price_impact(amount_in, amount_out, small_in, small_out),
        route=route, path=path, amount_in_raw=amount_in, amount_out_raw=amount_out, min_out_raw=floor,
        gas_native=gas_native, fees=tuple(fees), fee_rate=fee_rate,
    )


async def verify_router(rpc, dex: Dex) -> bool:
    """A wrong or fake router address must never get a swap: it has to be a contract whose WETH() is the expected wrapped coin."""
    if dex.version == 3:
        from . import dexv3
        return await dexv3.verify(rpc, dex)
    if dex.version == 4:
        from . import dexv4
        return await dexv4.verify(rpc, dex)
    try:
        got = await rpc._eth_call(dex.router, "0x" + SEL_WETH)
    except Exception:
        return False
    return (got or "").lower().removeprefix("0x")[-40:] == dex.wrapped.lower().removeprefix("0x")


async def quote(rpc, dex: Dex, token_contract: str, token_symbol: str, token_decimals: int, native_symbol: str,
                amount: Decimal, buying: bool, slippage_pct: Decimal = DEFAULT_SLIPPAGE,
                gas_price_wei: int | None = None) -> Quote | None:
    """A quote for buying the token with `amount` of the coin, or selling `amount` of the token for the coin.
    None if there is no route (no pool, or a token this router does not know)."""
    in_dec, out_dec = (18, token_decimals) if buying else (token_decimals, 18)
    amount_in = int(amount * (Decimal(10) ** in_dec))
    if amount_in <= 0:
        return None
    if dex.version == 4:
        from . import dexv4
        return await dexv4.quote(rpc, dex, token_contract, token_symbol, in_dec, out_dec, native_symbol, amount_in, buying,
                                 slippage_pct, gas_price_wei)
    if dex.version == 3:
        from . import dexv3
        return await dexv3.quote(rpc, dex, token_contract, token_symbol, in_dec, out_dec, native_symbol, amount_in, buying,
                                 slippage_pct, gas_price_wei)
    small_in = max(1, amount_in // 1000)  # a tiny trade shows the pool's price without our own impact
    symbols = {dex.wrapped.lower(): native_symbol, dex.stable.lower(): "USD", token_address(dex, token_contract).lower(): token_symbol}
    for path in candidate_paths(dex, token_contract, buying):
        try:
            out = decode_amounts(await rpc._eth_call(dex.router, encode_get_amounts_out(amount_in, path)))[-1]
            small = decode_amounts(await rpc._eth_call(dex.router, encode_get_amounts_out(small_in, path)))[-1]
        except Exception:
            continue  # no pool on this route (the contract reverts): try the next one
        if out <= 0:
            continue
        gas = Decimal(SWAP_GAS_UNITS * gas_price_wei) / Decimal(10 ** 18) if gas_price_wei else None
        return build_quote(
            dex=dex, buying=buying, amount_in=amount_in, amount_out=out, small_in=small_in, small_out=small,
            in_decimals=in_dec, out_decimals=out_dec, slippage_pct=slippage_pct,
            pay_symbol=native_symbol if buying else token_symbol, receive_symbol=token_symbol if buying else native_symbol,
            route=[symbols.get(a.lower(), "?") for a in path], path=path, gas_native=gas)
    return None


# ------------------------------------------------------------------ the message
def _copy_escaped(q: Quote, esc) -> Quote:
    """The quote with every text field made safe for Telegram's HTML (a token symbol is chosen by strangers)."""
    import dataclasses
    return dataclasses.replace(q, pay_symbol=esc(q.pay_symbol), receive_symbol=esc(q.receive_symbol),
                               route=[esc(x) for x in q.route])


def _num(value: Decimal) -> str:
    if value == 0:
        return "0"
    mag = abs(value)
    if mag >= 1000:
        return f"{value:,.2f}"
    if mag >= 1:
        return f"{value:,.4f}".rstrip("0").rstrip(".")
    digits = min(12, 4 - mag.adjusted())
    return f"{value:.{max(digits, 2)}f}".rstrip("0").rstrip(".") or "0"


def _usd(value: Decimal | None, price: Decimal | None) -> str:
    if value is None or price is None:
        return ""
    usd = value * price
    return f" (≈ ${usd:,.2f})" if usd >= Decimal("0.01") else " (&lt; $0.01)"


def render_quote(q: Quote, slippage_pct: Decimal, native_symbol: str, native_usd: Decimal | None,
                 token_usd: Decimal | None = None, network: str = "") -> str:
    """The quote as a Telegram HTML message."""
    pay_usd = native_usd if q.pay_symbol == native_symbol else token_usd
    recv_usd = native_usd if q.receive_symbol == native_symbol else token_usd
    import html as _h
    q = _copy_escaped(q, _h.escape)
    price_line = ""
    if q.receive > 0 and q.pay > 0:
        per = q.pay / q.receive if q.buying else q.receive / q.pay
        token = q.receive_symbol if q.buying else q.pay_symbol
        price_line = f"Price: 1 {token} ≈ {_num(per)} {native_symbol}"
    fee_pct = ((q.fee_rate if q.fee_rate is not None else q.dex.fee) * 100).normalize()
    lines = [
        f"💱 <b>{q.dex.name}</b>" + (f" · {network}" if network else "") + "\n",
        f"You pay: <b>{_num(q.pay)} {q.pay_symbol}</b>{_usd(q.pay, pay_usd)}",
        f"You receive ≈ <b>{_num(q.receive)} {q.receive_symbol}</b>{_usd(q.receive, recv_usd)}",
        f"Minimum received ({slippage_pct.normalize():f}% slippage): {_num(q.min_receive)} {q.receive_symbol}",
        price_line,
        f"Trade fee: {fee_pct:f}% = {_num(q.lp_fee)} {q.pay_symbol}{_usd(q.lp_fee, pay_usd)}",
        f"Price impact: {q.impact_pct:.2f}%" + ("  ⚠️ high" if q.impact_pct >= 5 else ""),
    ]
    if q.gas_native is not None:
        lines.append(f"Network fee (estimate, usually less): up to ≈ {_num(q.gas_native)} {native_symbol}{_usd(q.gas_native, native_usd)}")
    lines.append("Route: " + " → ".join(q.route))
    return "\n".join(x for x in lines if x != "")
