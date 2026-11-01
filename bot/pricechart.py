"""Price charts for the AI Assistant: "NRP price" gets a picture of the last days next to the number.

Where the history comes from:
  * Big coins (GAS, NEO, BNB, ETH, SOL, AVAX, POL): Binance's public candles (hourly, last 7 days), so the chart is
    complete at once.
  * Every other coin (NRP and the rest): the bot records the prices it already reads (one sample every 10 minutes,
    kept for 8 days in price_history.json). Such a chart fills in as time passes; with too few samples the bot just
    answers in words and says it started recording.
The drawing uses Pillow (already used for the home card). Parsing and drawing have no network access, so they can be tested.
"""
import json
import logging
import os
import time
from datetime import datetime, timezone
from decimal import Decimal
from io import BytesIO

log = logging.getLogger("walletbot.chart")

BINANCE_KLINES = "https://api.binance.com/api/v3/klines"
BINANCE_PAIR = {"GAS": "GASUSDT", "NEO": "NEOUSDT", "BNB": "BNBUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT",
                "AVAX": "AVAXUSDT", "POL": "POLUSDT"}
HISTORY_PATH = "price_history.json"
SAMPLE_EVERY = 600  # seconds between two recorded prices of a coin
KEEP_SECONDS = 8 * 86400
MIN_POINTS = 6  # fewer than this is not a chart
ASSETS = os.path.join(os.path.dirname(__file__), "assets")


# ------------------------------------------------------------------ recorded history
class History:
    """Prices the bot has seen, per coin: [(unix time, price)], oldest first."""

    def __init__(self, path: str = HISTORY_PATH):
        self.path = path
        self.data: dict[str, list[tuple[float, float]]] = {}
        self._saved = 0.0
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as fh:
                raw = json.load(fh)
            self.data = {str(k).upper(): [(float(t), float(p)) for t, p in v] for k, v in raw.items()}
        except FileNotFoundError:
            self.data = {}
        except Exception:
            log.exception("Could not read the price history")
            self.data = {}

    def record(self, prices: dict, now: float | None = None) -> bool:
        """Adds a sample for every coin whose last sample is old enough. True when something was added."""
        now = time.time() if now is None else now
        added = False
        for symbol, price in prices.items():
            try:
                value = float(price)
            except Exception:
                continue
            if value <= 0:
                continue
            rows = self.data.setdefault(str(symbol).upper(), [])
            if rows and now - rows[-1][0] < SAMPLE_EVERY:
                continue
            rows.append((now, value))
            added = True
        cutoff = now - KEEP_SECONDS
        for symbol, rows in list(self.data.items()):
            if rows and rows[0][0] < cutoff:
                self.data[symbol] = [r for r in rows if r[0] >= cutoff]
        if added and now - self._saved > 1800:
            self.save(now)
        return added

    def save(self, now: float | None = None) -> None:
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, separators=(",", ":"))
            os.replace(tmp, self.path)
            self._saved = time.time() if now is None else now
        except Exception:
            log.exception("Could not save the price history")

    def series(self, symbol: str, days: float = 7) -> list[tuple[float, float]]:
        cutoff = time.time() - days * 86400
        return [r for r in self.data.get(symbol.upper(), []) if r[0] >= cutoff]


HISTORY = History()


# ------------------------------------------------------------------ candles from Binance
def parse_klines(data) -> list[tuple[float, float]]:
    """[(unix time, close price)] from Binance's candle list."""
    out: list[tuple[float, float]] = []
    if not isinstance(data, list):
        return out
    for row in data:
        try:
            out.append((float(row[0]) / 1000.0, float(row[4])))
        except Exception:
            continue
    return out


async def fetch_klines(session, symbol: str, hours: int = 168) -> list[tuple[float, float]]:
    pair = BINANCE_PAIR.get(symbol.upper())
    if not pair:
        return []
    try:
        async with session.get(BINANCE_KLINES, params={"symbol": pair, "interval": "1h", "limit": str(hours)}) as resp:
            if resp.status != 200:
                return []
            return parse_klines(await resp.json(content_type=None))
    except Exception as exc:
        log.warning("Binance candles unavailable for %s: %s", symbol, type(exc).__name__)
        return []


async def get_series(session, symbol: str) -> tuple[list[tuple[float, float]], str]:
    """(points, where they came from: 'candles' or 'recorded'). Candles when available, else the recorded history."""
    points = await fetch_klines(session, symbol)
    if len(points) >= MIN_POINTS:
        return points, "candles"
    return HISTORY.series(symbol), "recorded"


# ------------------------------------------------------------------ drawing
def _price_text(value: float) -> str:
    if value >= 1000:
        return f"${value:,.0f}"
    if value >= 1:
        return f"${value:,.2f}"
    text = f"{Decimal(str(value)):.8f}".rstrip("0").rstrip(".")
    return "$" + (text if text else "0")


def _axis_text(value: float) -> str:
    return _price_text(value)


def change_percent(points: list[tuple[float, float]]) -> float:
    if len(points) < 2 or points[0][1] == 0:
        return 0.0
    return (points[-1][1] - points[0][1]) / points[0][1] * 100.0


def _font(kind: str, size: int):
    from PIL import ImageFont

    files = {"bold": "Inter-Bold.ttf", "semi": "Inter-SemiBold.ttf", "med": "Inter-Medium.ttf", "reg": "Inter-Regular.ttf"}
    for path in (os.path.join(ASSETS, files[kind]), "DejaVuSans-Bold.ttf" if kind in ("bold", "semi") else "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def render_chart(symbol: str, points: list[tuple[float, float]], source: str = "candles") -> bytes | None:
    """A dark PNG line chart with the price, the change over the period and the high and low. None if there is too little data."""
    if len(points) < MIN_POINTS:
        return None
    from PIL import Image, ImageDraw

    S = 2  # draw at 2x, then shrink: smooth lines
    W, H = 900 * S, 520 * S
    bg, panel, grid, text, sub = (14, 17, 24), (22, 27, 38), (40, 47, 62), (240, 243, 248), (140, 150, 168)
    pts = sorted(points)
    values = [p for _t, p in pts]
    lo, hi = min(values), max(values)
    if hi == lo:
        hi, lo = hi * 1.01 + 1e-12, lo * 0.99
    pad = (hi - lo) * 0.12
    lo, hi = lo - pad, hi + pad
    change = change_percent(pts)
    up = change >= 0
    line = (46, 204, 113) if up else (240, 84, 84)

    img = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((24 * S, 24 * S, W - 24 * S, H - 24 * S), radius=24 * S, fill=panel)

    # header
    d.text((56 * S, 46 * S), f"{symbol.upper()} / USD", font=_font("semi", 26 * S), fill=sub)
    span_days = max(1, round((pts[-1][0] - pts[0][0]) / 86400))
    d.text((W - 56 * S, 50 * S), f"last {span_days} day{'s' if span_days != 1 else ''}", font=_font("med", 22 * S), fill=sub, anchor="ra")
    d.text((56 * S, 84 * S), _price_text(values[-1]), font=_font("bold", 64 * S), fill=text)
    sign = "+" if up else "−"
    d.text((W - 56 * S, 100 * S), f"{sign}{abs(change):.2f}%", font=_font("bold", 40 * S), fill=line, anchor="ra")

    # plot area
    left, right, top, bottom = 230 * S, W - 70 * S, 196 * S, H - 112 * S
    t0, t1 = pts[0][0], pts[-1][0]
    span_t = (t1 - t0) or 1.0

    def xy(t: float, v: float) -> tuple[float, float]:
        return left + (t - t0) / span_t * (right - left), bottom - (v - lo) / (hi - lo) * (bottom - top)

    for i in range(5):  # horizontal grid with price labels
        y = top + (bottom - top) * i / 4
        d.line((left, y, right, y), fill=grid, width=S)
        value = hi - (hi - lo) * i / 4
        d.text((left - 14 * S, y), _axis_text(value), font=_font("reg", 19 * S), fill=sub, anchor="rm")

    xs = [xy(t, v) for t, v in pts]
    # soft fill under the line
    fill_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    fd = ImageDraw.Draw(fill_layer)
    fd.polygon(xs + [(xs[-1][0], bottom), (xs[0][0], bottom)], fill=line + (45,))
    img = Image.alpha_composite(img.convert("RGBA"), fill_layer).convert("RGB")
    d = ImageDraw.Draw(img)
    d.line(xs, fill=line, width=4 * S, joint="curve")
    ex, ey = xs[-1]
    d.ellipse((ex - 8 * S, ey - 8 * S, ex + 8 * S, ey + 8 * S), fill=line, outline=panel, width=3 * S)

    # high and low marks
    i_hi, i_lo = values.index(max(values)), values.index(min(values))
    for idx, label, dy in ((i_hi, "high", -12), (i_lo, "low", 12)):
        x, y = xs[idx]
        d.ellipse((x - 5 * S, y - 5 * S, x + 5 * S, y + 5 * S), outline=sub, width=2 * S)
        anchor = "ms" if dy < 0 else "mt"
        x = min(max(x, left + 110 * S), right - 110 * S)
        d.text((x, y + dy * S), f"{label} {_price_text(values[idx])}", font=_font("med", 18 * S), fill=sub, anchor=anchor)

    # time labels
    fmt = "%d %b" if span_t > 2 * 86400 else "%H:%M"
    for i in range(4):
        t = t0 + span_t * i / 3
        x = left + (right - left) * i / 3
        label = datetime.fromtimestamp(t, tz=timezone.utc).strftime(fmt)
        d.text((x, bottom + 14 * S), label, font=_font("reg", 19 * S), fill=sub, anchor="ma" if 0 < i < 3 else ("la" if i == 0 else "ra"))

    foot = "NeoRedPill Wallet Bot  ·  live exchange prices  ·  not financial advice"
    if source == "recorded":
        foot = "NeoRedPill Wallet Bot  ·  recorded by the bot  ·  not financial advice"
    d.text((W // 2, H - 58 * S), foot, font=_font("reg", 17 * S), fill=sub, anchor="ma")

    out = BytesIO()
    img.resize((W // S, H // S), Image.LANCZOS).save(out, "PNG", optimize=True)
    return out.getvalue()


def caption_note(symbol: str, points: list[tuple[float, float]], source: str) -> str:
    """One short line about the chart, for the photo caption."""
    if len(points) < MIN_POINTS:
        return ""
    days = max(1, round((points[-1][0] - points[0][0]) / 86400))
    sign = "+" if change_percent(points) >= 0 else "−"
    where = "" if source == "candles" else " (recorded by the bot)"
    return f"📈 {symbol.upper()} over the last {days} day{'s' if days != 1 else ''}: {sign}{abs(change_percent(points)):.2f}%{where}"
