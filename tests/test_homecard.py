import os
from datetime import datetime, timezone
from decimal import Decimal as D

import pytest

from bot import homecard, viewpref
from bot.homeview import Row, Section


def _sections(extra=0):
    rows = [Row("NRP", "250,000.00", False, D("0.00125"), D("312")), Row("GAS", "0", True, None, None)]
    rows += [Row(f"T{i}", "1.0", False, None, None) for i in range(extra)]
    return [Section("neo_n3", "Neo N3", rows, True), Section("solana", "Solana", None)]


def test_holdings_skip_zero_and_sort_by_value():
    held = homecard.holdings(_sections(2))
    assert [h.symbol for h in held] == ["NRP", "T0", "T1"]


def test_pages():
    assert homecard.page_count(_sections(0)) == 1
    assert homecard.page_count(_sections(15)) == 1  # 16 tokens fit on the first page
    assert homecard.page_count(_sections(16)) == 2  # 17 tokens: 16 on the first page, the rest on the next
    assert homecard.page_count(_sections(40)) == 3


def test_caption_mentions_unavailable_network():
    text = homecard.caption(_sections(), datetime.now(timezone.utc), 0, 1)
    assert "Solana" in text


def test_render_makes_a_png():
    pytest.importorskip("PIL")
    png, pages = homecard.render(_sections(20), datetime.now(timezone.utc), page=1)
    assert png[:2] == b"\xff\xd8" and pages == 2


def test_viewpref_roundtrip(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(viewpref, "PATH", str(tmp_path / "v.json"))
    monkeypatch.setattr(viewpref, "_PREFS", {})
    monkeypatch.setattr(viewpref, "_LOADED", False)
    assert viewpref.get(5) is None and viewpref.mode(5) == "text"
    viewpref.set_mode(5, "photo")
    assert viewpref.mode(5) == "photo"
    viewpref.set_mode(5, "bogus")
    assert viewpref.mode(5) == "photo"
    monkeypatch.setattr(viewpref, "_PREFS", {})
    monkeypatch.setattr(viewpref, "_LOADED", False)
    assert viewpref.get(5) == "photo"


def test_short_qty():
    assert homecard._short_qty("0.000151962759280941") == "0.000152"
    assert homecard._short_qty("17690.81256544") == "17,690.8126"
    assert homecard._short_qty("250,000.00") == "250,000"
