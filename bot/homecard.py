"""The photo version of the home page: a light "portfolio" card drawn with Pillow.

Page 1 shows the total, a chart of how the money is split between networks, and the first tokens.
When there are more tokens than fit, later pages show just the next tokens. Only tokens the person
actually holds are drawn. Pure drawing code: no Telegram, no network (the caller fetches the data).
"""
import html
import io
import os
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from . import prices
from .homeview import Row, Section

PER_PAGE = 8
# Layouts: how many tokens fit per page (first page, later pages), the row height, and where the rows start on page 1.
STYLES = {
    "classic": dict(first=8, nxt=8, row_h=112, gap=10, top1=655, summary="full"),
    "compact": dict(first=10, nxt=12, row_h=112, gap=10, top1=410, summary="card"),
    "tight": dict(first=12, nxt=14, row_h=92, gap=8, top1=410, summary="card"),
    "minimal": dict(first=16, nxt=17, row_h=80, gap=6, top1=250, summary="line", fixed_h=1696),
}
STYLE = "minimal"


def _chunk_bounds(page: int, st: dict) -> tuple[int, int]:
    if page == 0:
        return 0, st["first"]
    start = st["first"] + (page - 1) * st["nxt"]
    return start, start + st["nxt"]
W = 1080
S = 2  # draw at 2x, then shrink: smooth edges
_FONT_DIR = os.path.join(os.path.dirname(__file__), "assets")
_FONT_FILES = {
    "reg": "Inter-Regular.ttf", "med": "Inter-Medium.ttf", "semi": "Inter-SemiBold.ttf", "bold": "Inter-Bold.ttf",
}
_FALLBACKS = {  # used only if the bundled fonts are missing
    "reg": ["C:/Windows/Fonts/segoeui.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"],
    "med": ["C:/Windows/Fonts/segoeui.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"],
    "semi": ["C:/Windows/Fonts/segoeuib.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"],
    "bold": ["C:/Windows/Fonts/segoeuib.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"],
}

INK = (20, 24, 40)
MUTED = (112, 120, 142)
GREEN = (14, 150, 100)
LINE = (228, 232, 243)
BG = (244, 246, 251)
CARD = (255, 255, 255)
RED = (230, 50, 70)
CHIP = (235, 238, 248)
CHIP2 = (232, 235, 244)
DISC = (245, 247, 252, 255)
FOOT = (130, 138, 160)
BG2 = None  # second background colour: a vertical gradient from BG to BG2
GLOW = []  # soft coloured glows behind the card: (x, y, radius, colour, strength)
PILL_TEXT = (255, 255, 255)
THEMES = {
    "light": {},
    "redpill": dict(INK=(245, 245, 248), MUTED=(150, 152, 168), GREEN=(60, 214, 150), LINE=(48, 40, 48), BG=(14, 12, 16),
                    CARD=(26, 22, 28), RED=(230, 50, 70), CHIP=(44, 36, 46), CHIP2=(52, 44, 54), DISC=(52, 44, 54, 255),
                    FOOT=(120, 120, 136)),
    "midnight": dict(INK=(236, 241, 255), MUTED=(138, 150, 182), GREEN=(52, 211, 153), LINE=(36, 48, 82), BG=(9, 14, 30),
                     CARD=(18, 27, 52), RED=(99, 102, 241), CHIP=(30, 42, 76), CHIP2=(38, 50, 88), DISC=(38, 50, 88, 255),
                     FOOT=(110, 124, 160)),
    "ocean": dict(INK=(15, 40, 60), MUTED=(96, 128, 150), GREEN=(0, 150, 136), LINE=(196, 224, 236), BG=(232, 245, 250),
                  CARD=(255, 255, 255), RED=(0, 140, 190), CHIP=(214, 236, 246), CHIP2=(206, 230, 242), DISC=(236, 246, 250, 255),
                  FOOT=(110, 140, 160)),
}
THEMES.update({
    "gold": dict(INK=(255, 247, 228), MUTED=(190, 172, 130), GREEN=(255, 214, 102), LINE=(255, 214, 102, 60), BG=(34, 26, 10),
                 CARD=(255, 214, 102, 20), RED=(255, 190, 40), CHIP=(255, 214, 102, 36), CHIP2=(255, 214, 102, 44),
                 DISC=(255, 255, 255, 235), FOOT=(170, 150, 110), BG2=(6, 6, 8), PILL_TEXT=(40, 28, 0),
                 GLOW=[(940, 110, 440, (255, 190, 40), 110), (60, 1000, 520, (200, 120, 20), 70)]),
    "emerald": dict(INK=(240, 255, 248), MUTED=(140, 205, 180), GREEN=(120, 255, 200), LINE=(255, 255, 255, 40), BG=(4, 64, 52),
                    CARD=(255, 255, 255, 22), RED=(0, 229, 153), CHIP=(255, 255, 255, 30), CHIP2=(255, 255, 255, 36),
                    DISC=(255, 255, 255, 235), FOOT=(120, 190, 165), BG2=(3, 18, 22), PILL_TEXT=(2, 40, 30),
                    GLOW=[(940, 120, 440, (0, 229, 153), 110), (80, 900, 520, (0, 140, 200), 70)]),
    "neon": dict(INK=(255, 255, 255), MUTED=(190, 178, 235), GREEN=(94, 255, 200), LINE=(255, 255, 255, 46), BG=(46, 16, 101),
                 CARD=(255, 255, 255, 26), RED=(255, 63, 140), CHIP=(255, 255, 255, 34), CHIP2=(255, 255, 255, 40),
                 DISC=(255, 255, 255, 235), FOOT=(190, 178, 235), BG2=(12, 8, 40),
                 GLOW=[(930, 120, 420, (255, 63, 140), 120), (80, 760, 520, (80, 120, 255), 90), (1000, 1300, 480, (160, 60, 255), 90)]),
    "sunset": dict(INK=(255, 255, 255), MUTED=(255, 205, 190), GREEN=(255, 226, 120), LINE=(255, 255, 255, 50), BG=(255, 94, 98),
                   CARD=(255, 255, 255, 30), RED=(90, 20, 90), CHIP=(255, 255, 255, 40), CHIP2=(255, 255, 255, 46),
                   DISC=(255, 255, 255, 240), FOOT=(255, 225, 215), BG2=(88, 24, 110),
                   GLOW=[(900, 100, 460, (255, 200, 90), 110), (60, 900, 520, (255, 80, 140), 80)]),
    "aurora": dict(INK=(240, 255, 252), MUTED=(150, 214, 220), GREEN=(110, 255, 190), LINE=(255, 255, 255, 40), BG=(5, 60, 80),
                   CARD=(255, 255, 255, 22), RED=(0, 200, 170), CHIP=(255, 255, 255, 30), CHIP2=(255, 255, 255, 36),
                   DISC=(255, 255, 255, 235), FOOT=(130, 200, 205), BG2=(6, 14, 40), PILL_TEXT=(4, 30, 40),
                   GLOW=[(120, 160, 460, (0, 230, 170), 100), (980, 900, 520, (60, 120, 255), 90), (200, 1400, 460, (120, 80, 255), 80)]),
})
_BASE = {k: globals()[k] for k in ("INK", "MUTED", "GREEN", "LINE", "BG", "CARD", "RED", "CHIP", "CHIP2", "DISC", "FOOT", "BG2", "GLOW", "PILL_TEXT")}


def use_theme(name: str) -> None:
    """Switches the card's colours (module-wide)."""
    values = dict(_BASE)
    values.update(THEMES.get(name, {}))
    globals().update(values)


THEME = "gold"
OUT = 1.5  # output size relative to the 1080 design: sharper text and logos on a phone screen
CHAIN_COLOR = {
    "neo_n3": (0, 200, 140), "neo_x": (120, 90, 255), "bsc": (243, 186, 47), "eth": (98, 126, 234),
    "polygon": (130, 71, 229), "arbitrum": (40, 160, 240), "optimism": (255, 4, 32), "base": (0, 82, 255),
    "avalanche": (232, 65, 66), "solana": (153, 69, 255),
}
TOKEN_COLOR = {
    "NRP": (230, 50, 70), "GAS": (0, 200, 140), "NEO": (0, 229, 153), "BNB": (243, 186, 47),
    "SOL": (153, 69, 255), "ETH": (98, 126, 234), "USDT": (38, 161, 123), "USDC": (39, 117, 202),
}
_PALETTE = [(255, 122, 89), (64, 140, 255), (0, 176, 160), (160, 98, 255), (240, 160, 40), (230, 80, 130)]


@dataclass(frozen=True)
class Holding:
    symbol: str
    qty: str
    network: str
    price: Decimal | None
    value: Decimal | None
    slug: str = ""
    contract: str = ""
    is_default: bool = False


def holdings(sections: list[Section]) -> list[Holding]:
    """Tokens the person holds, biggest USD value first (unpriced ones last)."""
    out = []
    for section in sections:
        for row in section.rows or []:
            if row.qty is None or row.zero:
                continue
            out.append(Holding(row.symbol, row.qty, section.name, row.price, row.value, section.slug, row.contract, row.is_default))
    out.sort(key=lambda h: (h.value is None, -(h.value or Decimal(0))))
    return out


def page_count(sections: list[Section], style: str | None = None) -> int:
    st = STYLES[style or STYLE]
    n = len(holdings(sections))
    if n <= st["first"]:
        return 1
    return 1 + -(-(n - st["first"]) // st["nxt"])


_FONTS: dict = {}


def _font(kind: str, size: float):
    """Fonts are loaded once and reused (loading one from disk for every piece of text was slow)."""
    key = (kind, size)
    if key not in _FONTS:
        _FONTS[key] = _load_font(kind, size)
    return _FONTS[key]


def _load_font(kind: str, size: float):
    from PIL import ImageFont

    path = os.path.join(_FONT_DIR, _FONT_FILES[kind])
    candidates = [path] + _FALLBACKS[kind]
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, int(size * S))
        except OSError:
            continue
    return ImageFont.load_default()


def _color_for(symbol: str):
    if symbol.upper() in TOKEN_COLOR:
        return TOKEN_COLOR[symbol.upper()]
    return _PALETTE[sum(ord(c) for c in symbol) % len(_PALETTE)]


def _short_qty(qty: str) -> str:
    """A readable amount: long tails like 0.000151962759280941 are rounded."""
    try:
        v = Decimal(qty.replace(",", ""))
    except Exception:
        return _cut(qty, 16)
    if v == 0:
        return "0"
    mag = abs(v)
    if mag >= 1:
        text = f"{v:,.4f}".rstrip("0").rstrip(".")
    else:
        digits = 4 - mag.adjusted() - 1  # keep about 4 significant digits
        text = f"{v:.{min(digits, 18)}f}".rstrip("0").rstrip(".")
    return text or "0"


def _cut(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def logo_key(h: Holding) -> str:
    """File-safe name for a token's logo."""
    c = (h.contract or "").lower()
    if not c or c == "native":
        return f"{h.slug}_native_{h.symbol.upper()}"
    return f"{h.slug}_{c}"


def _summary_card(d, rr, txt, sc, width_of, sections, held, priced_total, any_priced, pages):
    """A short summary card: total, counts and the network donut (no legend), about half as tall as the full one."""
    rr((50, 130, 1030, 390), 40, fill=CARD, outline=LINE, width=2)
    txt((100, 156), "Total balance", "med", 26, MUTED)
    total_text = prices.format_usd(priced_total) if (any_priced or not held) else "No prices yet"
    size = 80 if len(total_text) <= 10 else 64 if len(total_text) <= 12 else 52
    txt((100, 192), total_text, "bold", size, INK)
    nets = {h.network for h in held}
    chip = f"{len(held)} token{'s' if len(held) != 1 else ''}  ·  {len(nets)} network{'s' if len(nets) != 1 else ''}"
    rr((100, 304, 100 + width_of(chip, "semi", 24) + 40, 352), 24, fill=CHIP)
    txt((120, 314), chip, "semi", 24, INK)
    by_net = {}
    for section in sections:
        total = sum((r.value for r in section.rows or [] if r.value is not None), Decimal(0))
        if total > 0:
            by_net[section.name] = (total, CHAIN_COLOR.get(section.slug, _color_for(section.name)))
    cx, cy, R = 850, 260, 92
    box = [sc(cx - R), sc(cy - R), sc(cx + R), sc(cy + R)]
    grand = sum((v for v, _c in by_net.values()), Decimal(0))
    if grand > 0:
        angle = -90.0
        for _name, (value, col) in sorted(by_net.items(), key=lambda kv: -kv[1][0]):
            sweep = 360.0 * float(value / grand)
            d.pieslice(box, angle, angle + max(sweep - 1.2, 0.5), fill=col)
            angle += sweep
    else:
        d.ellipse(box, fill=CHIP2)
    d.ellipse([sc(cx - R + 36), sc(cy - R + 36), sc(cx + R - 36), sc(cy + R - 36)], fill=CARD)
    txt((cx, cy - 8), str(len(nets) or 0), "bold", 40, INK, "mm")
    txt((cx, cy + 24), "networks" if len(nets) != 1 else "network", "med", 18, MUTED, "mm")


def _summary_line(rr, txt, width_of, held, priced_total, any_priced):
    """No card at all: the total sits right under the title, with the counts beside it."""
    total_text = prices.format_usd(priced_total) if (any_priced or not held) else "No prices yet"
    size = 76 if len(total_text) <= 10 else 62 if len(total_text) <= 12 else 50
    txt((60, 128), total_text, "bold", size, INK)
    nets = {h.network for h in held}
    chip = f"{len(held)} token{'s' if len(held) != 1 else ''}  ·  {len(nets)} network{'s' if len(nets) != 1 else ''}"
    w = width_of(chip, "semi", 24) + 40
    rr((1030 - w, 150, 1030, 198), 24, fill=CHIP)
    txt((1030 - w + 20, 160), chip, "semi", 24, INK)


def _background(Image, height: int):
    """The card's backdrop: a flat colour, or a top-to-bottom gradient with soft glows (themes with BG2)."""
    if not BG2:
        return Image.new("RGB", (W * S, height * S), BG)
    from PIL import ImageFilter
    w, h = W * S, height * S
    column = Image.new("RGB", (1, h))
    px = column.load()
    for yy in range(h):
        t = yy / max(1, h - 1)
        px[0, yy] = tuple(int(BG[i] + (BG2[i] - BG[i]) * t) for i in range(3))
    img = column.resize((w, h))
    for gx, gy, gr, col, strength in GLOW:
        layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        from PIL import ImageDraw as _D
        _D.Draw(layer).ellipse([(gx - gr) * S, (gy - gr) * S, (gx + gr) * S, (gy + gr) * S], fill=col + (strength,))
        layer = layer.filter(ImageFilter.GaussianBlur(gr * S / 2.2))
        img = Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")
    return img


def _allocation_panel(d, rr, txt, width_of, held, total, y0, y1):
    """Fills the empty space under a short list: where the money sits, as one bar per network."""
    rr((50, y0, 1030, y1), 36, fill=CARD, outline=LINE, width=2)
    txt((90, y0 + 26), "Where your money is", "bold", 32, INK)
    by_net: dict[str, Decimal] = {}
    for h in held:
        if h.value is not None and h.value > 0:
            by_net[h.network] = by_net.get(h.network, Decimal(0)) + h.value
    if not by_net or total <= 0:
        return
    ordered = sorted(by_net.items(), key=lambda kv: -kv[1])
    slug_of = {h.network: h.slug for h in held}
    top = y0 + 92
    pitch = 70
    room = y1 - top - 16
    bars = min(len(ordered), 6)
    if room >= bars * pitch + 240:  # lots of room: a donut chart above the bars
        R = int(min(190, (room - bars * pitch - 40) / 2))
        cx, cy = 540, top + R + 10
        box = [(cx - R) * S, (cy - R) * S, (cx + R) * S, (cy + R) * S]
        angle = -90.0
        for name, value in ordered:
            sweep = 360.0 * float(value / total)
            d.pieslice(box, angle, angle + max(sweep - 1.2, 0.5), fill=CHAIN_COLOR.get(slug_of.get(name, ""), _color_for(name)))
            angle += sweep
        hole = R * 0.64
        d.ellipse([(cx - hole) * S, (cy - hole) * S, (cx + hole) * S, (cy + hole) * S], fill=BG)
        txt((cx, cy - 10), prices.format_usd(total), "bold", 30 if len(prices.format_usd(total)) <= 8 else 24, INK, "mm")
        txt((cx, cy + 24), "total", "med", 20, MUTED, "mm")
        top = cy + R + 40
    fit = max(1, int((y1 - top - 16) // pitch))
    for name, value in ordered[:fit]:
        share = float(value / total)
        accent = CHAIN_COLOR.get(slug_of.get(name, ""), _color_for(name))
        txt((90, top), _cut(name, 20), "semi", 26, INK)
        txt((990, top), f"{share * 100:.0f}%" if share >= 0.01 else "<1%", "semi", 26, MUTED, "ra")
        rr((90, top + 40, 990, top + 54), 7, fill=CHIP2)
        rr((90, top + 40, 90 + max(14, 900 * share), top + 54), 7, fill=accent)
        top += pitch


def render(sections: list[Section], now: datetime, page: int = 0, logos: dict | None = None, style: str | None = None) -> tuple[bytes, int]:
    """(JPEG bytes, number of pages). `page` is 0-based and clamped to a real page."""
    from PIL import Image, ImageDraw

    held = holdings(sections)
    st = STYLES[style or STYLE]
    pages = page_count(sections, style)
    page = max(0, min(page, pages - 1))
    lo, hi = _chunk_bounds(page, st)
    chunk = held[lo:hi]

    def sc(v):
        return int(v * S)

    first = page == 0
    row_h, gap = st["row_h"], st["gap"]
    top = st["top1"] if first else 170
    fixed = st.get("fixed_h")
    if fixed:  # always the same big picture: with few tokens the rows grow, and what is left over is filled with a panel
        avail = fixed - top - 70
        row_h = max(st["row_h"], min(132, int(avail / max(1, len(chunk))) - 8))
        gap = 6 if row_h < 100 else 10
    k = row_h / 112
    fz = 1.08 if row_h >= 125 else 1.0 if row_h >= 112 else 0.85 if row_h >= 90 else 0.78  # font scale
    height = fixed or top + max(1, len(chunk)) * (row_h + gap) + 70  # under about 5:8, or Telegram shrinks the photo
    img = _background(Image, height)
    d = ImageDraw.Draw(img, "RGBA")

    def rr(box, r, fill=None, outline=None, width=1):
        d.rounded_rectangle([sc(v) for v in box], radius=sc(r), fill=fill, outline=outline, width=width)

    def txt(xy, s, kind, size, fill, anchor="la"):
        d.text((sc(xy[0]), sc(xy[1])), s, font=_font(kind, size), fill=fill, anchor=anchor)

    def width_of(s, kind, size):
        return d.textlength(s, font=_font(kind, size)) / S

    # header
    txt((60, 36), "NeoRedPill Wallet", "bold", 40, INK)
    txt((60, 88), "Portfolio" if first else f"Assets  ·  page {page + 1} of {pages}", "med", 26, MUTED)
    rr((760, 38, 1020, 98), 30, fill=RED)
    txt((890, 68), now.strftime("%H:%M UTC"), "bold", 26, PILL_TEXT, "mm")

    priced_total = sum((h.value for h in held if h.value is not None), Decimal(0))
    any_priced = any(h.value is not None for h in held)
    unpriced = any(h.value is None for h in held)

    if first and st["summary"] == "card":
        _summary_card(d, rr, txt, sc, width_of, sections, held, priced_total, any_priced, pages)
    elif first and st["summary"] == "line":
        _summary_line(rr, txt, width_of, held, priced_total, any_priced)
    if first and st["summary"] == "full":
        rr((50, 130, 1030, 560), 44, fill=CARD, outline=LINE, width=2)
        txt((100, 166), "Total balance", "med", 28, MUTED)
        total_text = prices.format_usd(priced_total) if (any_priced or not held) else "No prices yet"
        size = 92 if len(total_text) <= 10 else 74 if len(total_text) <= 12 else 58
        txt((100, 204), total_text, "bold", size, INK)
        nets = {h.network for h in held}
        chip = f"{len(held)} token{'s' if len(held) != 1 else ''}  ·  {len(nets)} network{'s' if len(nets) != 1 else ''}"
        rr((100, 325, 100 + width_of(chip, "semi", 26) + 44, 377), 26, fill=CHIP)
        txt((122, 336), chip, "semi", 26, INK)

        # share of the money per network
        by_net: dict[str, Decimal] = {}
        color_of: dict[str, tuple] = {}
        for section in sections:
            total = sum((r.value for r in section.rows or [] if r.value is not None), Decimal(0))
            if total > 0:
                by_net[section.name] = total
                color_of[section.name] = CHAIN_COLOR.get(section.slug, _color_for(section.name))
        cx, cy, R = 820, 345, 110
        box = [sc(cx - R), sc(cy - R), sc(cx + R), sc(cy + R)]
        grand = sum(by_net.values(), Decimal(0))
        if grand > 0:
            ordered = sorted(by_net.items(), key=lambda kv: -kv[1])
            angle = -90.0
            for name, value in ordered:
                sweep = 360.0 * float(value / grand)
                d.pieslice(box, angle, angle + max(sweep - 1.2, 0.5), fill=color_of[name])
                angle += sweep
        else:
            ordered = []
            d.ellipse(box, fill=CHIP2)
        d.ellipse([sc(cx - R + 42), sc(cy - R + 42), sc(cx + R - 42), sc(cy + R - 42)], fill=CARD)
        txt((cx, cy - 10), str(len(nets) or 0), "bold", 46, INK, "mm")
        txt((cx, cy + 27), "networks" if len(nets) != 1 else "network", "med", 20, MUTED, "mm")
        ly = 410
        for name, value in ordered[:3]:
            d.ellipse([sc(100), sc(ly + 5), sc(122), sc(ly + 27)], fill=color_of[name])
            txt((138, ly), _cut(name, 18), "semi", 26, INK)
            pct = float(value / grand * 100)
            txt((560, ly), f"{pct:.0f}%" if pct >= 1 else "<1%", "med", 26, MUTED, "ra")
            ly += 44
        txt((60, 590), "Your assets", "bold", 38, INK)
        txt((1020, 602), f"page 1 of {pages}" if pages > 1 else "", "med", 24, MUTED, "ra")

    y = top
    if not chunk:
        rr((50, y, 1030, y + row_h), 28, fill=CARD, outline=LINE, width=2)
        txt((540, y + row_h / 2), "No tokens yet. Tap Deposit to get started.", "med", 28, MUTED, "mm")
    for h in chunk:
        rr((50, y, 1030, y + row_h), 28, fill=CARD, outline=LINE, width=2)
        col = _color_for(h.symbol)
        cx, cy, r = 106 if k >= 1 else 98, y + row_h / 2, 30 * max(k, 0.8)
        logo = (logos or {}).get(logo_key(h))
        if logo is not None:  # the token's real logo, in a circle
            side = sc(2 * r)
            art = logo.convert("RGBA").resize((side, side), Image.LANCZOS)
            disc = Image.new("RGBA", (side, side), DISC)
            disc.alpha_composite(art)
            mask = Image.new("L", (side, side), 0)
            ImageDraw.Draw(mask).ellipse([0, 0, side - 1, side - 1], fill=255)
            img.paste(disc.convert("RGB"), (sc(cx - r), sc(cy - r)), mask)
        else:  # no logo found: a colored circle with the first letter
            d.ellipse([sc(cx - r), sc(cy - r), sc(cx + r), sc(cy + r)], fill=col)
            txt((cx, cy), h.symbol[:1].upper(), "bold", 30 * fz, (255, 255, 255), "mm")
        lx = 154 if k >= 1 else 146
        sym = _cut(h.symbol, 10)
        txt((lx, y + 10 * k), sym, "bold", 35 * fz, INK)
        # network chip next to the name
        net = _cut(h.network, 18)
        chip_x = lx + width_of(sym, "bold", 35 * fz) + 16
        chip_w = width_of(net, "med", 21 * fz) + 44
        rr((chip_x, y + 17 * k, chip_x + chip_w, y + 49 * k), 16 * max(k, 0.8), fill=CHIP)
        ncol = CHAIN_COLOR.get(h.slug, _color_for(h.network))
        d.ellipse([sc(chip_x + 12), sc(y + 27 * k), sc(chip_x + 24), sc(y + 27 * k + 12)], fill=ncol)
        txt((chip_x + 32, y + 33 * k), net, "med", 21 * fz, MUTED, "lm")
        # the balance is the star of the row
        qty = _short_qty(h.qty)
        txt((lx, y + 58 * k), qty, "bold", 31 * fz, GREEN)
        txt((lx + width_of(qty, "bold", 31 * fz) + 10, y + 64 * k), sym, "med", 24 * fz, MUTED)
        value = prices.format_usd(h.value) if h.value is not None else "—"
        txt((1000, y + 10 * k), value, "bold", 35 * fz, INK if h.value is not None else MUTED, "ra")
        price = f"@ {prices.format_usd(h.price)}" if h.price is not None else "price n/a"
        txt((1000, y + 64 * k), price, "med", 23 * fz, MUTED, "ra")
        y += row_h + gap

    foot = f"Updated {now.strftime('%H:%M:%S')} UTC"
    if unpriced:
        foot += "  ·  total counts tokens with a known price"
    if fixed:
        if fixed - 60 - y >= 230 and any_priced:
            _allocation_panel(d, rr, txt, width_of, held, priced_total, y + 8, fixed - 62)
        txt((540, fixed - 46), foot, "reg", 24, FOOT, "ma")
    else:
        txt((540, y + 14), foot, "reg", 24, FOOT, "ma")

    out = io.BytesIO()
    img.resize((int(W * OUT), int(height * OUT)), Image.LANCZOS).convert("RGB").save(
        out, format="JPEG", quality=95, subsampling=0, optimize=True)  # about half the size of a PNG: faster to upload
    return out.getvalue(), pages


def caption(sections: list[Section], now: datetime, page: int, pages: int) -> str:
    """Text under the picture. Empty unless something needs saying (the card already has the total and the time)."""
    down = [html.escape(s.name) for s in sections if s.rows is None]
    if not down:
        return ""
    return ("⚠️ Unavailable right now: " + ", ".join(down))[:1000]


use_theme(THEME)
