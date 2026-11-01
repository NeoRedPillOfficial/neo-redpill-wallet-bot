"""A once-a-day user statistic for the bot's owners: how many people use the bot, and how many joined."""
import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

from decimal import Decimal

from sqlalchemy import Float, cast, func, select

from . import db, usage

log = logging.getLogger("walletbot.dailystats")
STATE_FILE = "dailystats.state"
CHECK_EVERY = 300  # seconds


def due(now: datetime, last_sent: str, hour_utc: int) -> bool:
    """True once per UTC day, from `hour_utc` on."""
    return now.hour >= hour_utc and last_sent != now.strftime("%Y-%m-%d")


def _arrow(now: int, before: int) -> str:
    if now > before:
        return f"▲ {now - before} more"
    if now < before:
        return f"▼ {before - now} fewer"
    return "no change"


def render(total: int, day: int, week: int, prev_week: int, month: int, with_wallets: int) -> str:
    return (
        "📊 <b>Daily bot statistics</b>\n\n"
        f"👥 Total users: <b>{total}</b>\n"
        f"🆕 New in the last 24 hours: <b>{day}</b>\n"
        f"📅 New in the last 7 days: <b>{week}</b> ({_arrow(week, prev_week)} than the 7 days before)\n"
        f"🗓 New in the last 30 days: <b>{month}</b>\n"
        f"👛 Users with a wallet: <b>{with_wallets}</b>"
    )


async def collect(sessions) -> dict:
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    async def count(session, since=None, until=None) -> int:
        query = select(func.count()).select_from(db.User)
        if since is not None:
            query = query.where(db.User.created_at >= since)
        if until is not None:
            query = query.where(db.User.created_at < until)
        return int((await session.execute(query)).scalar() or 0)

    async with sessions() as session:
        wallets = (await session.execute(select(func.count(func.distinct(db.Wallet.user_id))))).scalar() or 0
        return {
            "total": await count(session),
            "day": await count(session, now - timedelta(days=1)),
            "week": await count(session, now - timedelta(days=7)),
            "prev_week": await count(session, now - timedelta(days=14), now - timedelta(days=7)),
            "month": await count(session, now - timedelta(days=30)),
            "with_wallets": int(wallets),
        }


async def text(sessions, prices=None) -> str:
    out = render(**await collect(sessions))
    if prices is not None:
        try:
            out += "\n\n" + await money(sessions, prices)
        except Exception as exc:
            log.exception("Money report failed")
            out += f"\n\n💸 <i>The money report could not be built ({type(exc).__name__}). Please tell the developer.</i>"
    try:
        out += "\n\n" + usage.render()
    except Exception:
        log.exception("Usage report failed")
    return out


# ------------------------------------------------------------------ money moved, by feature
SECTIONS = (  # (key, label, counts as money leaving the bot)
    ("deposit", "⬇️ Deposits into the bot", False),
    ("withdraw", "⬆️ Sends to outside addresses", True),
    ("transfer", "↗️ Sends to other bot users", True),
    ("tip", "🎁 Tips", True),
    ("swap", "🔄 Trades (swaps)", True),
    ("bridge", "🌉 Bridges", True),
)


def section_of(kind: str, tip: bool) -> str | None:
    if kind == "transfer_out":
        return "tip" if tip else "transfer"
    return {"deposit": "deposit", "withdraw": "withdraw", "swap": "swap", "bridge_out": "bridge"}.get(kind)


def _usd(value: Decimal) -> str:
    return f"${value:,.0f}" if value >= 100 else f"${value:,.2f}"


def render_money(periods: list[tuple[str, dict]]) -> str:
    """periods: [(title, {section: (count, usd, unpriced_count)})]"""
    lines = ["💸 <b>Money moved</b> <i>(confirmed only, valued at today's prices)</i>"]
    for title, data in periods:
        lines.append(f"\n<b>{title}</b>")
        out_total, out_count, unpriced = Decimal(0), 0, 0
        for key, label, leaving in SECTIONS:
            count, usd, miss = data.get(key, (0, Decimal(0), 0))
            if not count:
                continue
            lines.append(f"{label}: {count} · {_usd(usd)}")
            unpriced += miss
            if leaving:
                out_total += usd
                out_count += count
        if not out_count and not data.get("deposit", (0,))[0]:
            lines.append("Nothing yet")
        elif out_count:
            lines.append(f"<b>Total moved out: {out_count} transactions · {_usd(out_total)}</b>")
        if unpriced:
            lines.append(f"<i>{unpriced} transactions used tokens with no price, so they are not in the dollar totals.</i>")
    lines.append("\n<i>Votes, claims, airdrops, rain and quizzes are not recorded as money moves, so they are not counted here.</i>")
    return "\n".join(lines)


async def money(sessions, prices) -> str:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    windows = (("Last 24 hours", now - timedelta(days=1)), ("Last 7 days", now - timedelta(days=7)), ("All time", None))
    tip_flag = func.coalesce(db.Ledger.note, "").like("tip %")
    periods = []
    async with sessions() as session:
        for title, since in windows:
            query = (select(db.Ledger.kind, tip_flag, db.Ledger.token_id, func.count(), func.sum(func.abs(cast(db.Ledger.amount, Float))))
                     .where(db.Ledger.status == "confirmed")
                     .group_by(db.Ledger.kind, tip_flag, db.Ledger.token_id))
            if since is not None:
                query = query.where(db.Ledger.created_at >= since)
            rows = (await session.execute(query)).all()
            ids = {r[2] for r in rows}
            tokens = {}
            if ids:
                found = await session.execute(select(db.Token, db.Chain).join(db.Chain, db.Token.chain_id == db.Chain.id).where(db.Token.id.in_(ids)))
                tokens = {t.id: (t, c.slug) for t, c in found.all()}
            data: dict = {}
            for kind, tip, token_id, count, total in rows:
                key = section_of(kind, bool(tip))
                if key is None:
                    continue
                n, usd, miss = data.get(key, (0, Decimal(0), 0))
                n += int(count)
                token = tokens.get(token_id)
                value = None
                if token is not None and total is not None:
                    try:
                        value = await prices.usd_value(token[0], token[1], int(float(total)))
                    except Exception:
                        value = None
                if value is None:
                    miss += int(count)
                else:
                    usd += value
                data[key] = (n, usd, miss)
            periods.append((title, data))
    return render_money(periods)


def _read(path: str) -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


async def run_forever(bot, sessions, targets, prices=None, hour_utc: int | None = None, path: str = STATE_FILE) -> None:
    hour = int(os.getenv("STATS_HOUR_UTC", "3")) if hour_utc is None else hour_utc  # 3 UTC = 9:00 in Bangladesh
    while True:
        try:
            now = datetime.now(timezone.utc)
            if due(now, _read(path), hour):
                message = await text(sessions, prices)
                sent = 0
                for chat_id in await targets():
                    try:
                        await bot.send_message(chat_id, message)
                        sent += 1
                    except Exception as exc:
                        log.warning("Daily stats not delivered to %s: %s", chat_id, type(exc).__name__)
                if sent:  # only mark the day done when somebody got it
                    with open(path, "w") as f:
                        f.write(now.strftime("%Y-%m-%d"))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Daily statistics failed")
        await asyncio.sleep(CHECK_EVERY)
