"""Uniswap-V3-style pools (Uniswap V3, PancakeSwap V3): quotes and the swap's calldata.

V3 has no getAmountsOut. A quote asks the pool family's QuoterV2 contract (a read-only eth_call, nothing is spent), once
for every fee tier of a straight route and for the routes through the wrapped coin and the stablecoin. The best answer
wins. The swap goes through the router's multicall: buying sends the coin as the transaction's value (the router wraps
it); selling sends the token to the router, which unwraps the coin and pays it to the wallet in the same transaction.

The router, quoter and factory addresses are checked on-chain before every swap (verify), and the node's dry run
(estimateGas) refuses a swap that would fail, so a wrong address or a bad pool costs nothing.
Tokens that take a tax on every transfer often fail to SELL on V3 pools; the dry run then stops the trade.
"""
import asyncio
import logging
from decimal import Decimal

from . import dexquote
from .dexquote import _selector, addr_word, build_quote, word

log = logging.getLogger("walletbot.dex")

SEL_QUOTE_SINGLE = _selector("quoteExactInputSingle((address,address,uint256,uint24,uint160))", "c6a5026a")
SEL_QUOTE_PATH = _selector("quoteExactInput(bytes,uint256)", "cdca1753")
SEL_EXACT_SINGLE = _selector("exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))", "04e45aaf")
SEL_EXACT_PATH = _selector("exactInput((bytes,address,uint256,uint256))", "b858183f")
SEL_MULTICALL = _selector("multicall(uint256,bytes[])", "5ae401dc")
SEL_UNWRAP = _selector("unwrapWETH9(uint256,address)", "49404b7c")
SEL_WETH9 = _selector("WETH9()", "4aa4a4fc")
SEL_FACTORY = _selector("factory()", "c45a0155")
ADDRESS_THIS = "0x0000000000000000000000000000000000000002"  # "the router itself" as a recipient


def _pad(raw: bytes) -> str:
    return raw.hex().ljust((len(raw) + 31) // 32 * 64, "0")


def encode_path(tokens: list[str], fees: tuple) -> bytes:
    """token, fee, token, fee, token ... as the pools' packed path."""
    out = bytes.fromhex(tokens[0].removeprefix("0x"))
    for fee, token in zip(fees, tokens[1:]):
        out += int(fee).to_bytes(3, "big") + bytes.fromhex(token.removeprefix("0x"))
    return out


def encode_quote_single(token_in: str, token_out: str, amount_in: int, fee: int) -> str:
    return "0x" + SEL_QUOTE_SINGLE + addr_word(token_in) + addr_word(token_out) + word(amount_in) + word(fee) + word(0)


def encode_quote_path(path: bytes, amount_in: int) -> str:
    return "0x" + SEL_QUOTE_PATH + word(64) + word(amount_in) + word(len(path)) + _pad(path)


def decode_first_word(result: str) -> int:
    raw = (result or "").removeprefix("0x")
    if len(raw) < 64:
        raise ValueError("the quoter gave no answer")
    return int(raw[:64], 16)


def _swap_call(tokens: list[str], fees: tuple, amount_in: int, min_out: int, recipient: str) -> str:
    """The router call for one swap, along a single pool or a multi-hop path (hex, no 0x)."""
    if len(tokens) == 2:
        return (SEL_EXACT_SINGLE + addr_word(tokens[0]) + addr_word(tokens[1]) + word(fees[0]) + addr_word(recipient)
                + word(amount_in) + word(min_out) + word(0))
    path = encode_path(tokens, fees)
    # exactInput((bytes path, address recipient, uint256 amountIn, uint256 amountOutMinimum)): a dynamic tuple
    return (SEL_EXACT_PATH + word(32) + word(128) + addr_word(recipient) + word(amount_in) + word(min_out)
            + word(len(path)) + _pad(path))


def _multicall(deadline: int, calls: list[str]) -> str:
    """multicall(uint256 deadline, bytes[] data): the router refuses to run after the deadline."""
    heads, bodies, offset = "", "", 32 * len(calls)
    for call in calls:
        raw = bytes.fromhex(call)
        body = word(len(raw)) + _pad(raw)
        heads += word(offset)
        bodies += body
        offset += len(body) // 2
    return "0x" + SEL_MULTICALL + word(deadline) + word(64) + word(len(calls)) + heads + bodies


def encode_swap_buy(path: list[str], fees: tuple, amount_in: int, min_out: int, wallet: str, deadline: int) -> str:
    """Buy: the coin goes in as the transaction value, the token comes straight to the wallet."""
    return _multicall(deadline, [_swap_call(path, fees, amount_in, min_out, wallet)])


def encode_swap_sell(path: list[str], fees: tuple, amount_in: int, min_out: int, wallet: str, deadline: int) -> str:
    """Sell: the swap pays the router, then the router unwraps to the coin and sends it to the wallet (with the same minimum)."""
    unwrap = SEL_UNWRAP + word(min_out) + addr_word(wallet)
    return _multicall(deadline, [_swap_call(path, fees, amount_in, min_out, ADDRESS_THIS), unwrap])


async def verify(rpc, dex) -> bool:
    """The router and the quoter must be real contracts of this DEX: wrapped coin as expected, quoter's factory as expected."""
    wrapped = dex.wrapped.lower().removeprefix("0x")
    factory = dex.factory.lower().removeprefix("0x")

    def tail(value) -> str:
        return (value or "").lower().removeprefix("0x")[-40:]

    try:
        router_w, quoter_w, quoter_f = await asyncio.gather(
            rpc._eth_call(dex.router, "0x" + SEL_WETH9), rpc._eth_call(dex.quoter, "0x" + SEL_WETH9),
            rpc._eth_call(dex.quoter, "0x" + SEL_FACTORY))
    except Exception:
        return False
    return tail(router_w) == wrapped and tail(quoter_w) == wrapped and tail(quoter_f) == factory


def candidates(dex, token: str, buying: bool) -> list[tuple[list[str], tuple]]:
    """[(token path, fee tiers)] to quote: every straight pool, and two hops through the stablecoin."""
    t = dexquote.token_address(dex, token)
    if t.lower() == dex.wrapped.lower():
        return []
    straight = [([dex.wrapped, t] if buying else [t, dex.wrapped], (f,)) for f in dex.fees]
    via = [([dex.wrapped, dex.stable, t] if buying else [t, dex.stable, dex.wrapped], (a, b))
           for a in dex.fees for b in dex.fees if dex.stable.lower() != t.lower()]
    return straight + via


async def _one(rpc, dex, tokens: list[str], fees: tuple, amount_in: int) -> int:
    """What the quoter says this route pays out, or 0 when there is no such pool (the quoter reverts)."""
    try:
        if len(tokens) == 2:
            data = encode_quote_single(tokens[0], tokens[1], amount_in, fees[0])
        else:
            data = encode_quote_path(encode_path(tokens, fees), amount_in)
        return decode_first_word(await rpc._eth_call(dex.quoter, data))
    except Exception:
        return 0


async def quote(rpc, dex, token_contract: str, token_symbol: str, in_dec: int, out_dec: int, native_symbol: str,
                amount_in: int, buying: bool, slippage_pct: Decimal, gas_price_wei: int | None):
    """The best V3 quote, or None when no route has a pool for this amount."""
    options = candidates(dex, token_contract, buying)
    if not options:
        return None
    outs = await asyncio.gather(*(_one(rpc, dex, tokens, fees, amount_in) for tokens, fees in options))
    best = max(range(len(options)), key=lambda i: outs[i])
    if outs[best] <= 0:
        return None
    tokens, fees = options[best]
    small_in = max(1, amount_in // 1000)
    small = await _one(rpc, dex, tokens, fees, small_in)
    gas = Decimal(dexquote.SWAP_GAS_UNITS * gas_price_wei) / Decimal(10 ** 18) if gas_price_wei else None
    symbols = {dex.wrapped.lower(): native_symbol, dex.stable.lower(): "USD",
               dexquote.token_address(dex, token_contract).lower(): token_symbol}
    rate = sum((Decimal(f) / Decimal(1_000_000) for f in fees), Decimal(0))
    return build_quote(
        dex=dex, buying=buying, amount_in=amount_in, amount_out=outs[best], small_in=small_in, small_out=small,
        in_decimals=in_dec, out_decimals=out_dec, slippage_pct=slippage_pct,
        pay_symbol=native_symbol if buying else token_symbol, receive_symbol=token_symbol if buying else native_symbol,
        route=[symbols.get(a.lower(), "?") for a in tokens], path=tokens, gas_native=gas, fees=tuple(fees), fee_rate=rate)
