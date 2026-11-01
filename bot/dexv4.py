"""Uniswap V4 pools (Ethereum): quotes and the swap's calldata.

V4 has one PoolManager for all pools. A pool is named by its "pool key": (currency0, currency1, fee, tickSpacing, hooks);
the network's own coin is the zero address and always sorts first, so a coin<->token pool is (0x0, token, fee, spacing, hooks).
A quote asks the V4Quoter (read-only) about the standard fee tiers of pools without hooks, and the best answer wins.
Pools that use hooks (custom rules, dynamic fees) are NOT found, and the bot then says it has no route.

The swap is one call of the Universal Router: command V4_SWAP with the actions SWAP_EXACT_IN_SINGLE, SETTLE_ALL, TAKE_ALL.
Buying sends the coin as the transaction value. Selling needs the token approved to Permit2 and Permit2 to the router,
both for exactly the amount being sold and the second one expiring within minutes.

The router address is checked on-chain (the quoter's PoolManager must be the expected one) and the node's dry run
(estimateGas) refuses a swap that would fail, so a wrong address or pool costs nothing.
"""
import logging
from decimal import Decimal

from . import dexquote
from .dexquote import _selector, addr_word, build_quote, word

log = logging.getLogger("walletbot.dex")

ZERO = "0x" + "00" * 20
PERMIT2 = "0x000000000022D473030F116dDEE9F6B43aC78BA3"
POOL_MANAGER = "0x000000000004444c5dc75cB358380D2e3dE08A90"
TIERS = ((100, 1), (500, 10), (3000, 60), (10000, 200))  # (fee in hundredths of a basis point, tick spacing)

V4_SWAP = 0x10
SWAP_EXACT_IN_SINGLE, SETTLE_ALL, TAKE_ALL = 0x06, 0x0C, 0x0F

SEL_QUOTE = _selector("quoteExactInputSingle(((address,address,uint24,int24,address),bool,uint128,bytes))", "aa9d21cb")
SEL_EXECUTE = _selector("execute(bytes,bytes[],uint256)", "3593564c")
SEL_POOL_MANAGER = _selector("poolManager()", "dc4c90d3")
SEL_PERMIT2_APPROVE = _selector("approve(address,address,uint160,uint48)", "87517c45")
SEL_PERMIT2_ALLOWANCE = _selector("allowance(address,address,address)", "927da105")


def _pad(raw: bytes) -> str:
    return raw.hex().ljust((len(raw) + 31) // 32 * 64, "0")


def _bytes(raw: bytes) -> str:
    return word(len(raw)) + _pad(raw)


def _bytes_array(items: list[bytes]) -> str:
    heads, bodies, offset = "", "", 32 * len(items)
    for raw in items:
        body = _bytes(raw)
        heads += word(offset)
        bodies += body
        offset += len(body) // 2
    return word(len(items)) + heads + bodies


def pool_key(token: str, fee: int, spacing: int) -> tuple:
    """(currency0, currency1, fee, tickSpacing, hooks) of the coin<->token pool: the coin (zero address) is always first."""
    return (ZERO, token, fee, spacing, ZERO)


def _key_words(key: tuple) -> str:
    return addr_word(key[0]) + addr_word(key[1]) + word(key[2]) + word(key[3] & (2 ** 256 - 1)) + addr_word(key[4])


def encode_quote(key: tuple, zero_for_one: bool, amount_in: int) -> str:
    """V4Quoter.quoteExactInputSingle: a struct with a trailing `bytes hookData` (empty), so it is a dynamic tuple."""
    return ("0x" + SEL_QUOTE + word(32) + _key_words(key) + word(1 if zero_for_one else 0) + word(amount_in)
            + word(8 * 32) + word(0))


def _swap_params(key: tuple, zero_for_one: bool, amount_in: int, min_out: int) -> bytes:
    """abi.encode(ExactInputSingleParams{poolKey, zeroForOne, amountIn, amountOutMinimum, hookData})."""
    hexed = (word(32) + _key_words(key) + word(1 if zero_for_one else 0) + word(amount_in) + word(min_out)
             + word(9 * 32) + word(0))
    return bytes.fromhex(hexed)


def encode_swap(key: tuple, zero_for_one: bool, amount_in: int, min_out: int, deadline: int) -> str:
    """Universal Router execute(): one V4_SWAP command that swaps, pays the input and takes the output for the sender."""
    pay_currency = key[0] if zero_for_one else key[1]
    get_currency = key[1] if zero_for_one else key[0]
    actions = bytes([SWAP_EXACT_IN_SINGLE, SETTLE_ALL, TAKE_ALL])
    params = [_swap_params(key, zero_for_one, amount_in, min_out),
              bytes.fromhex(addr_word(pay_currency) + word(amount_in)),
              bytes.fromhex(addr_word(get_currency) + word(min_out))]
    # V4_SWAP's input is abi.encode(bytes actions, bytes[] params)
    first = _bytes(actions)
    swap_input = bytes.fromhex(word(64) + word(64 + len(first) // 2) + first + _bytes_array(params))
    commands = bytes([V4_SWAP])
    head = _bytes(commands)
    return "0x" + SEL_EXECUTE + word(96) + word(96 + len(head) // 2) + word(deadline) + head + _bytes_array([swap_input])


def encode_permit2_approve(token: str, spender: str, amount: int, expiration: int) -> str:
    return "0x" + SEL_PERMIT2_APPROVE + addr_word(token) + addr_word(spender) + word(amount) + word(expiration)


def encode_permit2_allowance(owner: str, token: str, spender: str) -> str:
    return "0x" + SEL_PERMIT2_ALLOWANCE + addr_word(owner) + addr_word(token) + addr_word(spender)


def decode_permit2_allowance(result: str) -> tuple[int, int]:
    """(amount, expiration) of a Permit2 allowance."""
    raw = (result or "").removeprefix("0x")
    if len(raw) < 128:
        return 0, 0
    return int(raw[:64], 16), int(raw[64:128], 16)


async def verify(rpc, dex) -> bool:
    """The quoter must belong to the real PoolManager, and the router and the quoter must be contracts."""
    try:
        manager = await rpc._eth_call(dex.quoter, "0x" + SEL_POOL_MANAGER)
        code_router = await rpc.call("eth_getCode", [dex.router, "latest"])
        code_quoter = await rpc.call("eth_getCode", [dex.quoter, "latest"])
    except Exception:
        return False
    same = (manager or "").lower().removeprefix("0x")[-40:] == dex.factory.lower().removeprefix("0x")
    return same and len(str(code_router)) > 4 and len(str(code_quoter)) > 4


async def _one(rpc, dex, key: tuple, zero_for_one: bool, amount_in: int) -> int:
    """What the quoter says this pool pays out, or 0 when there is no such pool (the quoter reverts)."""
    try:
        raw = (await rpc._eth_call(dex.quoter, encode_quote(key, zero_for_one, amount_in)) or "").removeprefix("0x")
        return int(raw[:64], 16) if len(raw) >= 64 else 0
    except Exception:
        return 0


async def quote(rpc, dex, token_contract: str, token_symbol: str, in_dec: int, out_dec: int, native_symbol: str,
                amount_in: int, buying: bool, slippage_pct: Decimal, gas_price_wei: int | None):
    """The best coin<->token V4 quote, or None when no hook-less pool of the standard tiers has this token."""
    import asyncio
    if token_contract.lower() == "native":
        return None
    token = token_contract
    keys = [pool_key(token, fee, spacing) for fee, spacing in TIERS]
    outs = await asyncio.gather(*(_one(rpc, dex, k, buying, amount_in) for k in keys))
    best = max(range(len(keys)), key=lambda i: outs[i])
    if outs[best] <= 0:
        return None
    key = keys[best]
    small_in = max(1, amount_in // 1000)
    small = await _one(rpc, dex, key, buying, small_in)
    gas = Decimal(dexquote.SWAP_GAS_UNITS * gas_price_wei) / Decimal(10 ** 18) if gas_price_wei else None
    fee, spacing = key[2], key[3]
    path = [ZERO, token] if buying else [token, ZERO]
    route = [native_symbol, token_symbol] if buying else [token_symbol, native_symbol]
    return build_quote(
        dex=dex, buying=buying, amount_in=amount_in, amount_out=outs[best], small_in=small_in, small_out=small,
        in_decimals=in_dec, out_decimals=out_dec, slippage_pct=slippage_pct,
        pay_symbol=native_symbol if buying else token_symbol, receive_symbol=token_symbol if buying else native_symbol,
        route=route, path=path, gas_native=gas, fees=(fee, spacing), fee_rate=Decimal(fee) / Decimal(1_000_000))
