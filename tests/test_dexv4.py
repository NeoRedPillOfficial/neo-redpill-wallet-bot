import asyncio
from decimal import Decimal

from bot import dexquote, dexv4

TOKEN = "0x" + "ab" * 20
ME = "0x" + "cd" * 20
DEX = dexquote.BY_KEY["uni4_eth"]


def split(data: str) -> list[str]:
    raw = data.removeprefix("0x")[8:]
    return [raw[i:i + 64] for i in range(0, len(raw), 64)]


def test_selectors():
    assert dexv4.SEL_EXECUTE == "3593564c" and dexv4.SEL_QUOTE == "aa9d21cb"


def test_the_coin_is_the_first_currency_of_the_pool_key():
    key = dexv4.pool_key(TOKEN, 3000, 60)
    assert key == (dexv4.ZERO, TOKEN, 3000, 60, dexv4.ZERO)


def test_quote_calldata_layout():
    w = split(dexv4.encode_quote(dexv4.pool_key(TOKEN, 500, 10), True, 10 ** 16))
    assert int(w[0], 16) == 32 and int(w[1], 16) == 0 and w[2].endswith(TOKEN[2:])
    assert int(w[3], 16) == 500 and int(w[4], 16) == 10 and int(w[5], 16) == 0  # hooks
    assert int(w[6], 16) == 1 and int(w[7], 16) == 10 ** 16 and int(w[8], 16) == 256 and int(w[9], 16) == 0


def _decode_execute(data: str):
    w = split(data)
    raw = data[10:]

    def at(off):  # a bytes value at a byte offset: (length, hex)
        n = int(raw[off * 2: off * 2 + 64], 16)
        return n, raw[off * 2 + 64: off * 2 + 64 + n * 2]
    commands = at(int(w[0], 16))
    arr_off = int(w[1], 16)
    count = int(raw[arr_off * 2: arr_off * 2 + 64], 16)
    items = []
    for i in range(count):
        rel = int(raw[arr_off * 2 + 64 + 64 * i: arr_off * 2 + 128 + 64 * i], 16)
        items.append(at(arr_off + 32 + rel))
    return int(w[2], 16), commands, items


def test_execute_runs_one_v4_swap_command():
    data = dexv4.encode_swap(dexv4.pool_key(TOKEN, 3000, 60), True, 10 ** 16, 555, 1234)
    assert data[2:10] == "3593564c"
    deadline, commands, items = _decode_execute(data)
    assert deadline == 1234 and commands == (1, "10") and len(items) == 1
    # the V4_SWAP input is abi.encode(bytes actions, bytes[] params)
    n, inner = items[0]
    w = [inner[i:i + 64] for i in range(0, len(inner), 64)]
    assert int(w[0], 16) == 64
    actions_len = int(w[2], 16)
    assert actions_len == 3 and w[3].startswith("060c0f")
    params_at = int(w[1], 16) // 32
    count = int(w[params_at], 16)
    assert count == 3
    first_off = int(w[params_at + 1], 16) // 32
    base = params_at + 1
    p0_len = int(w[base + first_off], 16)
    p0 = w[base + first_off + 1: base + first_off + 1 + p0_len // 32]
    assert int(p0[0], 16) == 32 and p0[2].endswith(TOKEN[2:]) and int(p0[3], 16) == 3000
    assert int(p0[6], 16) == 1 and int(p0[7], 16) == 10 ** 16 and int(p0[8], 16) == 555 and int(p0[9], 16) == 288 and int(p0[10], 16) == 0
    off1 = int(w[base + 1], 16) // 32
    p1 = w[base + off1 + 1: base + off1 + 3]  # SETTLE_ALL(currency in, max amount)
    assert int(p1[0], 16) == 0 and int(p1[1], 16) == 10 ** 16
    off2 = int(w[base + 2], 16) // 32
    p2 = w[base + off2 + 1: base + off2 + 3]  # TAKE_ALL(currency out, minimum)
    assert p2[0].endswith(TOKEN[2:]) and int(p2[1], 16) == 555


def test_selling_flips_the_direction_and_pays_with_the_token():
    data = dexv4.encode_swap(dexv4.pool_key(TOKEN, 3000, 60), False, 7, 3, 99)
    _, _, items = _decode_execute(data)
    inner = items[0][1]
    w = [inner[i:i + 64] for i in range(0, len(inner), 64)]
    params_at = int(w[1], 16) // 32
    base = params_at + 1
    off0 = int(w[base], 16) // 32
    p0 = w[base + off0 + 1: base + off0 + 12]
    assert int(p0[6], 16) == 0  # zeroForOne false: token -> coin
    off1 = int(w[base + 1], 16) // 32
    assert w[base + off1 + 1].endswith(TOKEN[2:])  # the token is what gets settled (paid)


def test_permit2_calls():
    d = dexv4.encode_permit2_approve(TOKEN, DEX.router, 5, 1000)
    assert d[2:10] == dexv4.SEL_PERMIT2_APPROVE and len(d) == 2 + 8 + 64 * 4
    assert dexv4.decode_permit2_allowance("0x" + f"{5:064x}" + f"{1000:064x}" + "00" * 32) == (5, 1000)
    assert dexv4.decode_permit2_allowance("0x") == (0, 0)


def test_quote_picks_the_best_tier_and_reports_its_fee():
    class Rpc:
        async def _eth_call(self, to, data):
            w = split(data)
            fee = int(w[3], 16)
            if fee in (500, 3000):
                return "0x" + f"{int(w[7], 16) * (800 if fee == 500 else 900) // 1000:064x}" + "00" * 32
            raise RuntimeError("revert")
    q = asyncio.run(dexv4.quote(Rpc(), DEX, TOKEN, "TKN", 18, 18, "ETH", 10 ** 17, True, Decimal(1), 10 ** 9))
    assert q.fees == (3000, 60) and q.amount_out_raw == 9 * 10 ** 16 and q.fee_rate == Decimal("0.003")
    assert q.path == [dexv4.ZERO, TOKEN] and q.route == ["ETH", "TKN"]
    assert asyncio.run(dexv4.quote(type("R", (), {"_eth_call": lambda s, t, d: (_ for _ in ()).throw(RuntimeError())})(),
                                   DEX, TOKEN, "T", 18, 18, "ETH", 1, True, Decimal(1), None)) is None


def test_verify_checks_the_pool_manager_and_that_contracts_exist():
    def rpc(manager, code):
        class R:
            async def _eth_call(self, to, data):
                return "0x" + "0" * 24 + manager.lower()[2:]

            async def call(self, method, params):
                return code
        return R()
    assert asyncio.run(dexv4.verify(rpc(DEX.factory, "0x6080"), DEX))
    assert not asyncio.run(dexv4.verify(rpc("0x" + "11" * 20, "0x6080"), DEX))
    assert not asyncio.run(dexv4.verify(rpc(DEX.factory, "0x"), DEX))


def test_every_dex_entry_is_complete_and_unique():
    keys = [d.key for d in dexquote.DEXES]
    assert len(keys) == len(set(keys))
    for d in dexquote.DEXES:
        for a in (d.router, d.wrapped, d.stable):
            assert len(a) == 42 and int(a, 16) > 0, d.key
        if d.version >= 3:
            assert len(d.quoter) == 42 and len(d.factory) == 42 and int(d.factory, 16) > 0, d.key
        if d.version == 3:
            assert d.fees and all(f > 0 for f in d.fees), d.key
    assert {"base", "optimism", "avalanche"} <= {d.chain for d in dexquote.DEXES}
    assert [d.key for d in dexquote.dexes_for("base", ["uniswap", "aerodrome"], {"uniswap": ("v3",)})] == ["uni3_base"]
