"""The home page text. Pure functions (no Telegram, no network), so they can be tested.

Layout: a title, the total in USD, then one block per network that holds something
(always Neo N3). Networks with nothing in them are folded into one short line, so
adding many networks doesn't bury the page. Telegram caps a message at 4096
characters, so a very long page switches to a compact layout and finally drops
the last networks rather than breaking.
"""
import html
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from . import prices

MAX_LEN = 3900
TEXT_STYLE = "grouped"  # classic | grouped | table | list | sheet

CHAIN_EMOJI = {
    "neo_n3": "🟢",
    "neo_x": "⚡",
    "bsc": "🟡",
    "eth": "🔷",
    "polygon": "🟣",
    "arbitrum": "🔵",
    "optimism": "🔴",
    "base": "🔹",
    "avalanche": "🔺",
    "solana": "🟪",
}
DEFAULT_EMOJI = "⚪"


@dataclass(frozen=True)
class Row:
    symbol: str
    qty: str | None  # formatted amount, None = can't be shown (node down / decimals unverified)
    zero: bool
    price: Decimal | None
    value: Decimal | None
    contract: str = ""  # used to find the token's logo
    is_default: bool = False


@dataclass(frozen=True)
class Section:
    slug: str
    name: str
    rows: list[Row] | None  # None = the whole network could not be reached
    always_show: bool = False


def _row_line(row: Row, compact: bool) -> str:
    sym = html.escape(row.symbol)
    if row.qty is None:
        return f"<b>{sym}</b>  unavailable"
    if row.zero:
        return f"<b>{sym}</b>  0"
    if row.value is None:
        return f"<b>{sym}</b>  {row.qty}"
    line = f"<b>{sym}</b>  {row.qty}  ≈ <b>{prices.format_usd(row.value)}</b>"
    if not compact and row.price is not None:
        line += f"\n<i>{prices.format_usd(row.price)} per {sym}</i>"
    return line


def _holds_something(rows: list[Row]) -> bool:
    return any(r.qty is None or not r.zero for r in rows)


def _block(section: Section, compact: bool) -> str:
    emoji = CHAIN_EMOJI.get(section.slug, DEFAULT_EMOJI)
    head = f"{emoji} <b>{html.escape(section.name)}</b>"
    if section.rows is None:
        return f"{head}\n<blockquote>⚠️ Network unavailable right now</blockquote>"
    body = "\n".join(_row_line(r, compact) for r in section.rows)
    return f"{head}\n<blockquote>{body}</blockquote>"


def render_home(sections: list[Section], now: datetime, style: str | None = None) -> str:
    style = style or TEXT_STYLE
    if style != "classic":
        return _render_short(sections, now, style)
    total = Decimal(0)
    priced = False
    for section in sections:
        for row in section.rows or []:
            if row.price is not None:
                priced = True
            if row.value is not None:
                total += row.value

    shown = [s for s in sections if s.rows is None or s.always_show or _holds_something(s.rows)]
    empty = [s.name for s in sections if s not in shown]

    head = ["💊 <b>NeoRedPill Wallet</b>", ""]
    if priced:
        head += ["💰 <b>Total balance</b>", f"<b>≈ {prices.format_usd(total)}</b>", ""]
    foot = []
    if empty:
        foot.append("⚪ <i>Nothing yet on: " + " · ".join(html.escape(n) for n in empty) + "</i>")
        foot.append("")
    if priced:
        foot.append("<i>The total counts tokens that have a known price.</i>")
    foot += [f"🕒 Updated {now.strftime('%H:%M:%S')} UTC", "🔔 You get a message here whenever you receive something."]

    for compact in (False, True):
        keep = len(shown)
        while keep >= 1:
            blocks = [_block(s, compact) for s in shown[:keep]]
            note = [] if keep == len(shown) else ["…and more networks. Open 📥 Deposit to see every address.", ""]
            text = "\n".join(head) + "\n" + "\n\n".join(blocks) + "\n\n" + "\n".join(note + foot)
            if len(text) <= MAX_LEN:
                return text
            if not compact:
                break  # try the compact layout before dropping networks
            keep -= 1
    return "\n".join(head + ["Too many tokens to list here."] + foot)


def _short_qty(qty: str) -> str:
    try:
        v = Decimal(qty.replace(",", ""))
    except Exception:
        return qty[:14]
    if v == 0:
        return "0"
    if abs(v) >= 1:
        return f"{v:,.4f}".rstrip("0").rstrip(".")
    digits = 4 - abs(v).adjusted() - 1
    return (f"{v:.{min(digits, 18)}f}").rstrip("0").rstrip(".") or "0"


def _money(value) -> str:
    return prices.format_usd(value) if value is not None else "—"


def _render_short(sections: list[Section], now: datetime, style: str) -> str:
    """Short layouts for the text home page: zero balances are hidden, one line per token, no long price notes."""
    held = []  # (section, row)
    down = []
    for section in sections:
        if section.rows is None:
            down.append(section.name)
            continue
        for row in section.rows:
            if row.qty is not None and not row.zero:
                held.append((section, row))
    total = sum((r.value for _s, r in held if r.value is not None), Decimal(0))
    priced = any(r.value is not None for _s, r in held)
    nets = {s.name for s, _r in held}
    empty = [s.name for s in sections if s.rows is not None and s.name not in nets]
    held.sort(key=lambda sr: (sr[1].value is None, -(sr[1].value or Decimal(0))))
    out = ["💊 <b>NeoRedPill Wallet</b>"]
    if priced:
        out.append(f"💰 <b>{prices.format_usd(total)}</b>  ·  {len(held)} tokens  ·  {len(nets)} networks")
    out.append("")
    if style == "grouped":
        blocks = []
        for section in sections:
            mine = [r for s_, r in held if s_ is section]
            if not mine:
                continue
            sub = sum((r.value for r in mine if r.value is not None), Decimal(0))
            blocks.append((sub, section, mine))
        blocks.sort(key=lambda b: -b[0])
        for sub, section, mine in blocks:
            emoji = CHAIN_EMOJI.get(section.slug, DEFAULT_EMOJI)
            out.append(f"{emoji} <b>{html.escape(section.name)}</b>  ·  {_money(sub)}")
            for r in sorted(mine, key=lambda r: -(r.value or Decimal(0))):
                qty = _short_qty(r.qty)
                if r.price is not None and r.value is not None:
                    out.append(f" ▸ <b>{html.escape(r.symbol)}</b> · {qty} × {prices.format_usd(r.price)} = <b>{_money(r.value)}</b>")
                else:
                    out.append(f" ▸ <b>{html.escape(r.symbol)}</b> · {qty}  (no price yet)")
            out.append("")
    elif style == "sheet":
        out.append("<pre>" + _sheet(held, total, priced) + "</pre>")
        out.append("")
    elif style == "table":
        lines = []
        for section, r in held:
            emoji = CHAIN_EMOJI.get(section.slug, DEFAULT_EMOJI)
            lines.append(f"{emoji} {html.escape(r.symbol[:6]):<6} {_short_qty(r.qty)[:11]:>11} {_money(r.value):>9}")
        out.append("<pre>" + "\n".join(lines) + "</pre>")
        out.append("")
    else:  # list
        for section, r in held:
            emoji = CHAIN_EMOJI.get(section.slug, DEFAULT_EMOJI)
            out.append(f"{emoji} <b>{html.escape(r.symbol)}</b>  <code>{_short_qty(r.qty)}</code>")
            out.append(f"      ≈ <b>{_money(r.value)}</b>  ·  <i>{html.escape(section.name)}</i>")
        out.append("")
    if down:
        out.append("⚠️ <i>Unavailable now: " + ", ".join(html.escape(n) for n in down) + "</i>")
    if empty:
        out.append("⚪ <i>Nothing yet: " + " · ".join(html.escape(n) for n in empty) + "</i>")
    out.append(f"🕒 {now.strftime('%H:%M:%S')} UTC  ·  🔔 I message you when you receive something")
    text = "\n".join(out)
    return text if len(text) <= MAX_LEN else text[:MAX_LEN - 40].rsplit("\n", 1)[0] + "\n…and more tokens"


NET_CODE = {"neo_n3": "N3", "neo_x": "NX", "bsc": "BSC", "eth": "ETH", "polygon": "POL", "arbitrum": "ARB",
            "optimism": "OP", "base": "BASE", "avalanche": "AVAX", "solana": "SOL"}


def _fit(text: str, width: int, right: bool = False) -> str:
    text = text if len(text) <= width else text[: width - 1] + "…"
    return text.rjust(width) if right else text.ljust(width)


def _sheet(held, total, priced) -> str:
    """The tokens as a spreadsheet-like table: header row, ruled cells, a total row. Plain characters only, so columns line up."""
    rows = [(r.symbol, NET_CODE.get(s.slug, s.slug[:4].upper()), _short_qty(r.qty), _money(r.value)) for s, r in held]
    w = [max([5] + [len(x[0]) for x in rows])]
    w[0] = min(w[0], 6)
    w.append(max([3] + [len(x[1]) for x in rows]))
    w.append(min(10, max([6] + [len(x[2]) for x in rows])))
    w.append(min(10, max([5] + [len(x[3]) for x in rows] + [len(_money(total))])))

    def line(l, m, r):
        return l + m.join("─" * x for x in w) + r

    def row(cells, head=False):
        parts = [_fit(c, w[i], right=i >= 2) for i, c in enumerate(cells)]
        return "│" + "│".join(parts) + "│"

    out = [line("┌", "┬", "┐"), row(("Token", "Net", "Amount", "Value")), line("├", "┼", "┤")]
    out += [row(r) for r in rows]
    out += [line("├", "┴" if False else "┼", "┤"), row(("TOTAL", "", "", _money(total) if priced else "—")), line("└", "┴", "┘")]
    return html.escape("\n".join(out))
