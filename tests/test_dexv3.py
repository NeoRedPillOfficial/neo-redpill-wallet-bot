import asyncio
from decimal import Decimal

from bot import dexquote, dexv3

W = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
TOKEN = "0x" + "ab" * 20
ME = "0x" + "cd" * 20
DEX = dexquote.BY_KEY["uni3_eth"]


def words(data: str) -> list[str]:
    raw = data.removeprefix("0x")[8:]
    return [raw[i:i + 64] for i in range(0, len(raw), 64)]


def test_selectors_match_the_known_ids():
    assert dexv3.SEL_QUOTE_SINGLE == "c6a5026a" and dexv3.SEL_EXACT_SINGLE == "04e45aaf"
    assert dexv3.SEL_MULTICALL == "5ae401dc" and dexv3.SEL_UNWRAP == "49404b7c" and dexv3.SEL_EXACT_PATH == "b858183f"


def test_path_is_token_fee_token():
    path = dexv3.encode_path([W, TOKEN], (3000,))
    assert len(path) == 43 and path[20:23] == (3000).to_bytes(3, "big")
    assert dexv3.encode_path([W, DEX.stable, TOKEN], (500, 3000)).hex().count("bb8") >= 1


def test_buy_calldata_is_one_exact_input_call_paying_the_wallet():
    data = dexv3.encode_swap_buy([W, TOKEN], (3000,), 10 ** 16, 123, ME, 999)
    assert data[2:10] == "5ae401dc"
    w = words(data)
    assert int(w[0], 16) == 999 and int(w[1], 16) == 64 and int(w[2], 16) == 1  # deadline, offset, one call
    inner_len = int(w[4], 16)
    inner = "".join(w[5:])[:inner_len * 2]
    assert inner[:8] == "04e45aaf" and inner_len == 4 + 7 * 32
    f = [inner[8 + 64 * i: 72 + 64 * i] for i in range(7)]
    assert f[0].endswith(W.lower()[2:]) and f[1].endswith(TOKEN[2:]) and int(f[2], 16) == 3000
    assert f[3].endswith(ME[2:]) and int(f[4], 16) == 10 ** 16 and int(f[5], 16) == 123 and int(f[6], 16) == 0


def test_sell_swaps_to_the_router_then_unwraps_to_the_wallet():
    data = dexv3.encode_swap_sell([TOKEN, W], (500,), 5 * 10 ** 18, 777, ME, 1000)
    w = words(data)
    assert int(w[2], 16) == 2  # two calls
    off1, off2 = int(w[3], 16), int(w[4], 16)
    body = "".join(w[3:])  # offsets are relative to the start of the array content (word 3)
    base = 3 * 64
    raw = data[2 + 8:]
    first = raw[base + off1 * 2: base + off1 * 2 + 64]
    n1 = int(first, 16)
    call1 = raw[base + off1 * 2 + 64: base + off1 * 2 + 64 + n1 * 2]
    assert call1[:8] == "04e45aaf"
    assert call1[8 + 64 * 3: 8 + 64 * 4].endswith("0" * 39 + "2")  # recipient is the router itself
    n2 = int(raw[base + off2 * 2: base + off2 * 2 + 64], 16)
    call2 = raw[base + off2 * 2 + 64: base + off2 * 2 + 64 + n2 * 2]
    assert call2[:8] == "49404b7c" and int(call2[8:72], 16) == 777 and call2[72:].endswith(ME[2:])


def test_multi_hop_uses_exact_input_with_the_packed_path():
    data = dexv3.encode_swap_buy([W, DEX.stable, TOKEN], (500, 3000), 10 ** 16, 5, ME, 1)
    raw = data[10:]
    inner_len = int(raw[4 * 64: 5 * 64], 16)
    inner = raw[5 * 64: 5 * 64 + inner_len * 2]
    assert inner[:8] == "b858183f"
    t = [inner[8 + 64 * i: 72 + 64 * i] for i in range(8)]
    assert int(t[0], 16) == 32 and int(t[1], 16) == 128 and t[2].endswith(ME[2:])
    assert int(t[5], 16) == 20 + 3 + 20 + 3 + 20


class FakeRpc:
    """A quoter that has pools only on the 0.3% and 1% tiers of the straight route."""
    def __init__(self, pools):
        self.pools = pools
        self.calls = 0

    async def _eth_call(self, to, data):
        self.calls += 1
        if data[2:10] == dexv3.SEL_QUOTE_SINGLE:
            w = words(data)
            fee, amount = int(w[3], 16), int(w[2], 16)
            if fee in self.pools:
                return "0x" + f"{amount * self.pools[fee] // 1000:064x}" + "00" * 96
            raise RuntimeError("execution reverted")
        raise RuntimeError("execution reverted")


def test_the_best_fee_tier_wins_and_the_trade_fee_is_that_tiers():
    rpc = FakeRpc({3000: 900, 10000: 700})
    q = asyncio.run(dexv3.quote(rpc, DEX, TOKEN, "TKN", 18, 18, "ETH", 10 ** 17, True, Decimal(1), 10 ** 9))
    assert q.fees == (3000,) and q.amount_out_raw == 10 ** 17 * 900 // 1000
    assert q.fee_rate == Decimal("0.003") and q.path == [W, TOKEN]
    assert q.min_out_raw == q.amount_out_raw * 99 // 100


def test_no_pool_gives_no_quote():
    assert asyncio.run(dexv3.quote(FakeRpc({}), DEX, TOKEN, "T", 18, 18, "ETH", 10 ** 17, True, Decimal(1), None)) is None


def test_verify_accepts_only_the_expected_contracts():
    def rpc_for(router_w, quoter_w, quoter_f):
        class R:
            async def _eth_call(self, to, data):
                sel = data[2:10]
                if to == DEX.router:
                    return "0x" + "0" * 24 + router_w.lower()[2:]
                return "0x" + "0" * 24 + (quoter_w if sel == dexv3.SEL_WETH9 else quoter_f).lower()[2:]
        return R()
    assert asyncio.run(dexv3.verify(rpc_for(W, W, DEX.factory), DEX))
    assert not asyncio.run(dexv3.verify(rpc_for(W, W, "0x" + "11" * 20), DEX))
    assert not asyncio.run(dexv3.verify(rpc_for("0x" + "22" * 20, W, DEX.factory), DEX))

    class Dead:
        async def _eth_call(self, to, data):
            raise RuntimeError("down")
    assert not asyncio.run(dexv3.verify(Dead(), DEX))


def test_v3_dexes_are_offered_only_for_v3_pools():
    ids = ["uniswap"]
    assert [d.key for d in dexquote.dexes_for("eth", ids, {"uniswap": ("v3",)})] == ["uni3_eth"]
    assert [d.key for d in dexquote.dexes_for("eth", ids, {"uniswap": ("v2",)})] == ["uniswap"]
    assert [d.key for d in dexquote.dexes_for("eth", ids, {"uniswap": ("v4",)})] == ["uni4_eth"]
    assert {d.key for d in dexquote.dexes_for("eth", ids)} == {"uniswap", "uni3_eth", "uni4_eth"}  # no labels: both are tried
