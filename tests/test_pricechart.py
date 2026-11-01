import os
import tempfile
import time

from bot import assistant, pricechart


def _points(n=40, start=1.0, step=0.01):
    now = time.time()
    return [(now - (n - 1 - i) * 3600, start + i * step) for i in range(n)]


def test_history_samples_are_spaced_and_old_ones_dropped():
    path = os.path.join(tempfile.mkdtemp(), "h.json")
    h = pricechart.History(path)
    t = 1_000_000.0
    assert h.record({"nrp": 0.00004, "GAS": 5}, now=t)
    assert not h.record({"NRP": 0.00005}, now=t + 60)  # too soon
    assert h.record({"NRP": 0.00005}, now=t + 700)
    assert len(h.data["NRP"]) == 2
    h.record({"NRP": 0.00006}, now=t + 9 * 86400)  # the first two are older than 8 days now
    assert len(h.data["NRP"]) == 1
    h.save()
    assert pricechart.History(path).data["NRP"][0][1] == 0.00006


def test_binance_candles_are_parsed_and_junk_is_skipped():
    rows = [[1700000000000, "1", "2", "0.5", "1.5", "10"], ["bad"], [1700003600000, "1", "2", "0.5", "1.8", "10"]]
    assert pricechart.parse_klines(rows) == [(1700000000.0, 1.5), (1700003600.0, 1.8)]
    assert pricechart.parse_klines({"code": -1121}) == []


def test_chart_is_a_png_and_needs_enough_points():
    png = pricechart.render_chart("NRP", _points(40, 0.00004, 0.000001), "recorded")
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 5000
    assert pricechart.render_chart("NRP", _points(3)) is None
    flat = pricechart.render_chart("GAS", _points(20, 5.0, 0.0))  # a flat line must not crash
    assert flat[:4] == b"\x89PNG"


def test_caption_and_change():
    pts = _points(30, 1.0, 0.1)
    assert pricechart.change_percent(pts) > 0
    note = pricechart.caption_note("GAS", pts, "candles")
    assert "GAS" in note and "+" in note and "recorded" not in note
    assert "recorded by the bot" in pricechart.caption_note("NRP", pts, "recorded")
    assert pricechart.caption_note("NRP", _points(2), "recorded") == ""


def test_chart_requests_are_understood_as_price_questions():
    syms = {"NRP", "GAS", "BNB"}
    for q in ("NRP chart", "show me the BNB price chart", "gas graph please", "NRP trend"):
        pq = assistant.parse_price_question(q, syms)
        assert pq is not None and not pq.quote, q
    assert assistant.parse_price_question("1000 nrp to gas", syms).quote == "GAS"
