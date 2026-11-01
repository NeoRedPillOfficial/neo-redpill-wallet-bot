"""Fee estimates in confirmation messages, and the logo cache for built-in coins."""
import io
import os
import tempfile
import time
from decimal import Decimal as D

from bot import chains, homecard, logos


class FakeEvmRpc:
    def __init__(self, estimate=50_000, price=3_000_000_000, fail_estimate=False):
        self._estimate, self._price, self._fail = estimate, price, fail_estimate

    async def estimate_gas(self, tx):
        if self._fail:
            raise chains.EvmRpcError("execution reverted")
        return self._estimate

    async def gas_price(self):
        return self._price


class Tok:
    def __init__(self, contract, symbol="X", decimals=18):
        self.contract, self.symbol, self.decimals = contract, symbol, decimals


def _evm(rpc, symbol="BNB"):
    ops = chains.EvmOps.__new__(chains.EvmOps)
    ops.rpc, ops.native_symbol, ops.slug, ops.name = rpc, symbol, "bsc", "BNB Smart Chain"
    return ops


def test_fee_text_single_and_many_transfers():
    est = chains.FeeEstimate(200_000_000_000_000, 18, "BNB")
    assert chains.fee_text(est, 1) == "about <b>0.0002 BNB</b>"
    text = chains.fee_text(est, 10)
    assert "about <b>0.002 BNB</b>" in text and "0.0002 BNB for each of 10 transfers" in text
    assert chains.fee_text(None, 3) == "" and chains.fee_text(chains.FeeEstimate(0, 18, "BNB")) == ""


def test_fee_text_shows_the_price_in_usd():
    est = chains.FeeEstimate(200_000_000_000_000, 18, "BNB")
    assert chains.fee_text(est, 1, D("600")) == "about <b>0.0002 BNB</b> (about $0.12)"
    assert "(about $1.20)" in chains.fee_text(est, 10, D("600"))
    assert "(less than $0.01)" in chains.fee_text(est, 1, D("1"))
    assert chains.fee_text(est, 1, None) == "about <b>0.0002 BNB</b>"  # no price: just the coin amount


def test_solana_token_fee_mentions_the_one_time_account_cost():
    est = chains.FeeEstimate(5000, 9, "SOL", worst=5000 + 2_039_280)
    text = chains.fee_text(est, 2)
    assert "0.00001 SOL" in text and "up to 0.00408856 SOL" in text and "no token account" in text


async def test_neo_n3_estimate_is_in_gas():
    ops = chains.N3Ops.__new__(chains.N3Ops)
    est = await ops.estimate_fee(Tok("0xabc", "NRP", 8), "N-address")
    assert est.symbol == "GAS" and chains.fee_text(est, 5).startswith("about <b>0.1 GAS</b>")


async def test_evm_estimate_uses_the_node_and_native_symbol():
    est = await _evm(FakeEvmRpc()).estimate_fee(Tok("native", "BNB"), "0x" + "11" * 20)
    # 50,000 gas * 1.2 headroom * 3 gwei * 1.1 headroom
    assert est.symbol == "BNB" and est.per_transfer == 60_000 * 3_300_000_000


async def test_evm_estimate_falls_back_to_a_typical_transfer_when_the_dry_run_fails():
    est = await _evm(FakeEvmRpc(fail_estimate=True)).estimate_fee(Tok("0x" + "22" * 20), "0x" + "11" * 20)
    assert est.per_transfer == 65_000 * 3_300_000_000


async def test_evm_estimate_is_none_when_the_node_is_down():
    class Down(FakeEvmRpc):
        async def gas_price(self):
            raise chains.EvmRpcError("down")

    assert await _evm(Down(fail_estimate=True)).estimate_fee(Tok("native"), "0x" + "11" * 20) is None


# ------------------------------------------------------------------ logos
def _holding(symbol, contract, slug, default):
    return homecard.Holding(symbol, "1", slug, D(1), D(1), slug, contract, default)


def _png():
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (40, 40), (10, 200, 120)).save(buf, format="PNG")
    return buf.getvalue()


async def test_gas_logo_is_shared_between_neo_n3_and_neo_x_and_retried_after_a_hiccup():
    with tempfile.TemporaryDirectory() as tmp:
        saved = (logos.CACHE_DIR, logos.CUSTOM_DIR, logos._download, logos._GECKO.copy())
        logos.CACHE_DIR, logos.CUSTOM_DIR = os.path.join(tmp, "logos"), os.path.join(tmp, "logos", "custom")
        logos._INFLIGHT.clear()
        logos._GECKO.update(at=0.0, images={})
        state = {"gecko_up": False}
        calls = []

        async def fake_download(url):
            calls.append(url)
            return _png() if url == "https://img/gas.png" else None

        class Feed:
            async def _get_json(self, url, params=None):
                if not state["gecko_up"]:
                    raise RuntimeError("429 too many requests")
                return [{"id": "gas", "image": "https://img/gas.png"}]

        orig_sleep = logos.asyncio.sleep

        async def no_sleep(_):
            return None

        logos.asyncio.sleep = no_sleep
        logos._download = fake_download
        try:
            n3 = _holding("GAS", "0xd2a4cff31913016155e38e474a2c06d08be276cf", "neo_n3", True)
            nx = _holding("GAS", "native", "neo_x", True)
            first = await logos.load(Feed(), [n3, nx])
            assert first == {}  # CoinGecko was rate limited: nothing yet
            # a built-in coin is not hidden for a day: it is asked again after 10 minutes
            for h in (n3, nx):
                path = os.path.join(logos.CACHE_DIR, homecard.logo_key(h) + ".none")
                assert os.path.exists(path)
                os.utime(path, (time.time() - 700, time.time() - 700))
            logos._GECKO.update(at=0.0, images={})
            state["gecko_up"] = True
            second = await logos.load(Feed(), [n3, nx])
            assert set(second) == {homecard.logo_key(n3), homecard.logo_key(nx)}
            # a third network with the same coin symbol finds the shared copy without downloading
            before = len(calls)
            other = _holding("GAS", "native", "other_chain", True)
            third = await logos.load(Feed(), [other])
            assert homecard.logo_key(other) in third and len(calls) == before
        finally:
            logos.asyncio.sleep = orig_sleep
            logos.CACHE_DIR, logos.CUSTOM_DIR, logos._download = saved[0], saved[1], saved[2]
            logos._GECKO.clear()
            logos._GECKO.update(saved[3])


async def test_neo_x_gas_uses_the_neo_n3_gas_logo_already_on_disk_even_if_coingecko_is_down():
    with tempfile.TemporaryDirectory() as tmp:
        saved = (logos.CACHE_DIR, logos.CUSTOM_DIR, logos._download, logos._GECKO.copy())
        logos.CACHE_DIR, logos.CUSTOM_DIR = os.path.join(tmp, "logos"), os.path.join(tmp, "logos", "custom")
        os.makedirs(logos.CACHE_DIR)
        logos._INFLIGHT.clear()
        logos._GECKO.update(at=0.0, images={})
        n3 = _holding("GAS", "0xd2a4cff31913016155e38e474a2c06d08be276cf", "neo_n3", True)
        nx = _holding("GAS", "native", "neo_x", True)
        with open(os.path.join(logos.CACHE_DIR, homecard.logo_key(n3) + ".png"), "wb") as fh:  # downloaded long ago
            fh.write(_png())
        calls = []

        async def fake_download(url):
            calls.append(url)
            return None

        class Feed:
            async def _get_json(self, url, params=None):
                raise RuntimeError("429")

        logos._download = fake_download
        try:
            for order in ([nx, n3], [n3, nx]):  # the order on the page must not matter
                for name in os.listdir(logos.CACHE_DIR):
                    if name.startswith("coin_") or name.endswith(".none"):
                        os.remove(os.path.join(logos.CACHE_DIR, name))
                out = await logos.load(Feed(), order)
                assert set(out) == {homecard.logo_key(n3), homecard.logo_key(nx)} and calls == []
        finally:
            logos.CACHE_DIR, logos.CUSTOM_DIR, logos._download = saved[0], saved[1], saved[2]
            logos._GECKO.clear()
            logos._GECKO.update(saved[3])
