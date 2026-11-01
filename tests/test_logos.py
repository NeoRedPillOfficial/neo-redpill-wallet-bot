import io
import os
from decimal import Decimal as D

import pytest

from bot import homecard, logos
from bot.homeview import Row, Section

BSC_TOKEN = "0x" + "ab" * 20


def _holding(symbol="ABC", contract=BSC_TOKEN, slug="bsc", is_default=False):
    return homecard.Holding(symbol, "1", "BNB Smart Chain", D(1), D(1), slug, contract, is_default)


def test_logo_candidates_use_the_exact_contract():
    urls = logos.candidates(_holding(), {})
    assert any("trustwallet/assets" in u and "smartchain/assets/0x" in u for u in urls)
    assert any("dd.dexscreener.com/ds-data/tokens/bsc/" + BSC_TOKEN in u for u in urls)
    assert logos.candidates(_holding("GAS", "native", "neo_x", True), {"GAS": "https://img/gas.png"})[0] == "https://img/gas.png"
    nrp = _holding("NRP", "0x789518aa302b571e3e825f2c85a01ad731014a45", "neo_n3", True)
    assert logos.candidates(nrp, {})[0].startswith("https://neoredpill.com/")
    assert logos.homecard.logo_key(nrp) == "neo_n3_0x789518aa302b571e3e825f2c85a01ad731014a45"
    assert homecard.logo_key(_holding("BNB", "native", "bsc", True)) == "bsc_native_BNB"


async def test_load_downloads_once_caches_and_falls_back(tmp_path, monkeypatch):
    pytest.importorskip("PIL")
    from PIL import Image

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(logos, "CACHE_DIR", str(tmp_path / "logos"))
    monkeypatch.setattr(logos, "CUSTOM_DIR", str(tmp_path / "logos" / "custom"))
    logos._INFLIGHT.clear()
    buf = io.BytesIO()
    Image.new("RGB", (60, 40), (200, 30, 30)).save(buf, format="PNG")
    calls = []

    async def fake_download(url):
        calls.append(url)
        return buf.getvalue() if "trustwallet" in url else None

    class Feed:
        async def _get_json(self, url, params=None):
            raise RuntimeError("offline")

    monkeypatch.setattr(logos, "_download", fake_download)
    good, bad = _holding("ABC"), _holding("ZZZ", "0x" + "cd" * 20)
    first = await logos.load(Feed(), [good])
    assert homecard.logo_key(good) in first and first[homecard.logo_key(good)].size == (logos.SIZE, logos.SIZE)
    n = len(calls)
    again = await logos.load(Feed(), [good])  # from disk: no new download
    assert homecard.logo_key(good) in again and len(calls) == n
    # nothing found for this one: no logo (letter circle), and it is not asked again right away
    monkeypatch.setattr(logos, "_download", lambda url: _none())
    assert await logos.load(Feed(), [bad]) == {}


async def _none():
    return None


def test_render_with_a_logo_makes_a_png():
    pytest.importorskip("PIL")
    from PIL import Image

    rows = [Row("ABC", "1.0", False, D("2"), D("2"), BSC_TOKEN, False)]
    sections = [Section("bsc", "BNB Smart Chain", rows)]
    png, _ = homecard.render(sections, __import__("datetime").datetime.now(), 0,
                             {homecard.logo_key(homecard.holdings(sections)[0]): Image.new("RGBA", (128, 128), (255, 0, 0, 255))})
    assert png[:2] == b"\xff\xd8"
