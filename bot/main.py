"""Telegram bot entry point: wallets, balances on several networks, add token by
contract, import/export, send/withdraw."""
import asyncio
import csv
import html
import io
import os
import logging
import re
import secrets
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram import BaseMiddleware
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    BufferedInputFile,
    InputMediaPhoto,
    BotCommandScopeAllGroupChats,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from decimal import Decimal

from . import (
    backup,
    flamingo,
    ftwswap,
    neovote,
    tokensafety,
    xbridge,
    aistore,
    assistant,
    bridge,
    campaigns,
    chains,
    dexquote,
    dexswap,
    events,
    evm_send,
    evm_watcher,
    helptext,
    historyview,
    homecard,
    homeview,
    logos as token_logos,
    market,
    menustrip,
    neo_send,
    pricechart,
    prices,
    rainstore,
    reconcile,
    service,
    solana_watcher,
    dailystats,
    usage,
    tipping,
    tls,
    unlisted,
    kbstore,
    viewpref,
    watcher,
    memes,
)
from .config import load_settings
from .db import Chain, Token, User, Wallet, init_db, make_session_factory
from .evm_rpc import EvmRpc
from .neo_rpc import NeoRpc
from .solana_rpc import SolRpc

log = logging.getLogger("walletbot")
router = Router()

SETTINGS = load_settings()
ENGINE, SESSIONS = make_session_factory(SETTINGS.database_url)
NEO_RPC = NeoRpc(SETTINGS.neo_n3_rpc)

# Every network the bot serves. EVM networks that fail the startup check are removed.
OPS: dict[str, chains.ChainOps] = {
    "neo_n3": chains.N3Ops(NEO_RPC, neo_send.make_mamba_sender(SETTINGS.neo_n3_rpc))
}
for _slug, _cfg in SETTINGS.evm_chains.items():
    OPS[_slug] = chains.EvmOps(_cfg, EvmRpc(_cfg.rpc_url))
if SETTINGS.solana_rpc:
    OPS["solana"] = chains.SolanaOps(SolRpc(SETTINGS.solana_rpc))

USER_LOCKS: dict[tuple, asyncio.Lock] = defaultdict(asyncio.Lock)  # one send at a time per user AND network


class _Locks:
    """Several per-network locks taken together (a bridge touches two networks)."""

    def __init__(self, locks):
        self.locks = locks

    def locked(self) -> bool:
        return any(lock.locked() for lock in self.locks)

    async def __aenter__(self):
        for lock in self.locks:
            await lock.acquire()

    async def __aexit__(self, *exc):
        for lock in reversed(self.locks):
            lock.release()


def send_lock(user_id: int, *slugs: str) -> _Locks:
    """A pending send on one network must not block the same person on another network."""
    return _Locks([USER_LOCKS[(user_id, slug)] for slug in sorted(set(slugs))])
PENDING_TIPS = tipping.PendingStore()
PRICES = prices.PriceFeed()
BRIDGE_N3_HASH = bridge.N3_BRIDGE.get(SETTINGS.network)  # None if NETWORK is something else: bridge off
DEPOSITOR = bridge.make_mamba_depositor(SETTINGS.neo_n3_rpc)
KEY_TYPE_LABEL = {"evm": "EVM (all EVM networks)", "neo_n3": "Neo N3", "solana": "Solana"}
CHAIN_TIMEOUT = 15  # seconds to wait for one network when building a page
EXPORT_DELETE_AFTER = 120  # seconds
BACK = [InlineKeyboardButton(text="⬅️ Back", callback_data="home")]
PRIVATE_ONLY = "Use this in a private chat with the bot."


class ImportFlow(StatesGroup):
    waiting_key = State()


class AddTokenFlow(StatesGroup):
    contract = State()


class SendFlow(StatesGroup):
    dest = State()
    amount = State()


class BridgeFlow(StatesGroup):
    amount = State()


class QuizFlow(StatesGroup):
    questions = State()


class AIFlow(StatesGroup):
    asking = State()


HIGH_IMPACT = Decimal(20)  # percent: above this a trade needs a second, explicit tap


def _confirm_button(nonce: str, prefix: str, impact) -> InlineKeyboardButton:
    """Confirm trade; for a trade with a very high price impact the first tap only opens a warning (prefix + 'hi')."""
    if impact is not None and Decimal(impact) >= HIGH_IMPACT:
        return InlineKeyboardButton(text=f"⚠️ Confirm anyway ({Decimal(impact):.0f}% price impact)", callback_data=f"{prefix}hi:{nonce}")
    return InlineKeyboardButton(text="✅ Confirm trade", callback_data=f"{prefix}ok:{nonce}")


def _high_impact_text(impact, give: str, get: str) -> str:
    return (f"🚨 <b>This trade has a {Decimal(impact):.0f}% price impact.</b>\n\n"
            f"The pool is small compared with your trade, so you would pay <b>{give}</b> but get only <b>{get}</b>, "
            "far less than the pool's normal price. You can't undo it.\n\n"
            "A smaller amount gets a much fairer price. Do you really want to trade this much?")


class FtwFlow(StatesGroup):
    amount = State()  # typing the amount to trade on FTW Swap (Neo N3)


class TradeFlow(StatesGroup):
    amount = State()  # typing the amount to quote on a DEX


class PrepFlow(StatesGroup):
    """Preparing a rain or quiz in private, step by step, to be sent later with /rain or /quiz in a group."""
    rain_amount = State()
    rain_people = State()
    quiz_amount = State()
    quiz_winners = State()
    quiz_seconds = State()
    q_text = State()
    q_correct = State()
    q_wrong = State()


BTN_HOME, BTN_SEND, BTN_DEPOSIT = "🏠 Home", "💸 Send", "📥 Deposit"
BTN_BRIDGE, BTN_ADD, BTN_HELP = "🌉 Bridge", "➕ Add token", "❓ Help"
BTN_HISTORY = "📜 History"
BTN_KEYS = "👛 Wallets"
BTN_KEYS_OLD = "🔑 Wallet keys"  # the label older keyboards still show
BTN_REMOVE, BTN_EVENTS, BTN_VIEW, BTN_REFRESH = "🗑 Remove token", "🌧 Rain & Quiz", "🖼 Home view", "🔄 Refresh"
BTN_TRADE = "📈 Trade"
BTN_TB = "🔁 Trade & Bridge"
BTN_TOKENS = "🪙 Add / Remove token"
BTN_EARN = "💎 Earn"
BTN_AIRDROP_OLD = "🪂 Airdrops"  # the label older keyboards still show
SHORTCUT_TEXTS = {BTN_HOME, BTN_SEND, BTN_DEPOSIT, BTN_HISTORY, BTN_BRIDGE, BTN_ADD, BTN_HELP, BTN_KEYS, BTN_KEYS_OLD,
                  BTN_REMOVE, BTN_EVENTS, BTN_VIEW, BTN_REFRESH, BTN_TRADE, BTN_EARN, BTN_AIRDROP_OLD, BTN_TOKENS, BTN_TB}
HISTORY_PAGE = 6  # rows per history page
PENDING_LIMIT = 15


def shortcut_keyboard(tg_id: int | None = None) -> ReplyKeyboardMarkup:
    """The buttons that stay under the chat box. Everyone gets every action here, in text mode and photo mode."""
    K = KeyboardButton
    second = [K(text=BTN_HISTORY), K(text=BTN_TOKENS)]
    if _any_bridge():
        second.insert(1, K(text=BTN_BRIDGE))
    return ReplyKeyboardMarkup(
        keyboard=[
            [K(text=BTN_HOME), K(text=BTN_SEND), K(text=BTN_DEPOSIT)],
            second,
            [K(text=BTN_TRADE), K(text=BTN_EARN), K(text=BTN_KEYS)],
            [K(text=BTN_HELP), K(text=BTN_EVENTS), K(text=BTN_VIEW)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Pick a shortcut below, or type /help",
    )


def home_keyboard(multi: bool = False) -> InlineKeyboardMarkup:
    rows = [[
        InlineKeyboardButton(text="💸 Send", callback_data="send"),
        InlineKeyboardButton(text="📥 Deposit", callback_data="wallets"),
    ]]
    second = [InlineKeyboardButton(text="🪙 Add / Remove token", callback_data="tokmenu")]
    if _any_bridge():
        second.insert(0, InlineKeyboardButton(text="🌉 Bridge", callback_data="bridge"))
    rows.append(second)
    rows.append([
        InlineKeyboardButton(text="📜 History", callback_data="hist:all:0"),
        InlineKeyboardButton(text="🔄 Refresh", callback_data="home"),
    ])
    rows.append([
        InlineKeyboardButton(text="🔑 Import wallet", callback_data="import"),
        InlineKeyboardButton(text="📤 Export wallet", callback_data="export"),
    ])
    if multi:
        rows.append([InlineKeyboardButton(text="🔄 Switch wallet", callback_data="swm")])
    rows.append([InlineKeyboardButton(text="❓ Help", callback_data="help")])
    rows.append([InlineKeyboardButton(text="🌧 Rain & 🧠 Quiz", callback_data="events")])
    rows.append([InlineKeyboardButton(text="💎 Earn", callback_data="earn")])
    rows.append([
        InlineKeyboardButton(text="📈 Trade", callback_data="trade"),
        InlineKeyboardButton(text="🖼 Home view: Text / Photo", callback_data="viewmenu"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def home_only() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[BACK])


def pick_type_keyboard(action: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=f"{action}:{key}")]
            for key, label in KEY_TYPE_LABEL.items()
        ]
        + [BACK]
    )


def _short(address: str) -> str:
    return f"{address[:6]}…{address[-4:]}"


async def _reply(message: Message, text: str, **kwargs):
    """Reply to a message; if that message is gone (deleted, or an old queued update) just send a new one."""
    try:
        return await message.reply(text, **kwargs)
    except TelegramBadRequest:
        return await message.answer(text, **kwargs)


def _private(event) -> bool:
    chat = event.chat if isinstance(event, Message) else event.message.chat
    return chat.type == ChatType.PRIVATE


async def _load(message_or_cb) -> tuple:
    tg = message_or_cb.from_user
    session = SESSIONS()
    user = await service.get_or_create_user(session, tg.id, tg.username)
    await service.ensure_wallets(session, user, SETTINGS.master_key)
    await session.commit()
    STARTED.add(tg.id)
    return session, user


async def _has_started(tg_id: int) -> bool:
    """True if this person has opened the bot (they have a wallet here, so a prize can be sent to them)."""
    if tg_id in STARTED:
        return True
    async with SESSIONS() as session:
        found = await service.find_user_by_telegram_id(session, tg_id)
    if found is not None:
        STARTED.add(tg_id)
    return found is not None


async def _show(event, text: str, markup=None) -> None:
    """A button press edits the message it was on; a typed command or shortcut sends a new one."""
    if isinstance(event, CallbackQuery):
        try:
            await event.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest as exc:
            if "not modified" not in str(exc).lower():
                raise
    else:
        await event.answer(text, reply_markup=markup)


async def _done(event, text: str | None = None, alert: bool = False) -> None:
    """Answer a button press (stops its spinner). Typed messages just get the text, if any."""
    if isinstance(event, CallbackQuery):
        await event.answer(text, show_alert=alert)
    elif text:
        await event.answer(text)


_FTW_PRICE_CACHE: dict = {}  # token contract -> (time, USD price or None)
FTW_PRICE_SECONDS = 180


async def _ftw_prices(user, ops, rows):
    """A Neo N3 token no price feed lists (like FRANK) gets its price from FTW Swap's pool: a dry run of selling a tiny amount
    of the person's own tokens for GAS, times GAS's USD price. Only tokens the person holds are looked up."""
    missing = [(t, a) for t, a, p in rows if p is None and a and t.decimals_verified and str(t.contract).lower() != "native"
               and ftwswap.normalize_hash(t.contract) != ftwswap.GAS_HASH]
    if not missing:
        return rows
    try:
        gas_usd = (await asyncio.wait_for(PRICES.prices(), 6)).get("GAS")
        async with SESSIONS() as s:
            wallet = await service.get_wallet(s, user, "neo_n3")
        if not gas_usd or wallet is None:
            return rows
    except Exception:
        return rows
    found = {}
    for token, amount in missing:
        cached = _FTW_PRICE_CACHE.get(token.contract)
        if cached and time.monotonic() - cached[0] < FTW_PRICE_SECONDS:
            found[token.contract] = cached[1]
            continue
        try:
            gas_each = await asyncio.wait_for(ftwswap.sell_price_in_gas(ops.rpc, ftwswap.script_hash(wallet.address), token.contract, token.decimals, amount), 8)
        except Exception:
            gas_each = None
        usd = gas_each * gas_usd if gas_each is not None else None
        _FTW_PRICE_CACHE[token.contract] = (time.monotonic(), usd)
        found[token.contract] = usd
    return [(t, a, p if p is not None else found.get(t.contract)) for t, a, p in rows]


async def _chain_rows(user, slug: str, ops, with_prices: bool):
    """[(token, amount, price)] for one network, or None if the network can't be reached.
    Each network uses its own database session so they can be fetched at the same time."""
    try:
        async with SESSIONS() as s:
            rows = await asyncio.wait_for(service.chain_view(s, user, slug, ops), CHAIN_TIMEOUT)
        if not with_prices:
            return [(token, amount, None) for token, amount in rows]
        # look the prices up at the same time, not one after another
        found = await asyncio.gather(*(PRICES.price_of(token, slug) for token, _ in rows), return_exceptions=True)
        out = [(token, amount, None if isinstance(price, Exception) else price) for (token, amount), price in zip(rows, found)]
        if slug == "neo_n3":
            out = await _ftw_prices(user, ops, out)
        return out
    except Exception as exc:
        log.warning("Network %s unavailable for a page: %r", slug, exc)
        return None


async def _gather_views(user, with_prices: bool = False) -> dict:
    items = list(OPS.items())
    results = await asyncio.gather(*(_chain_rows(user, slug, ops, with_prices) for slug, ops in items))
    return {slug: rows for (slug, _), rows in zip(items, results)}


async def _home_sections(user) -> list:
    views = await _gather_views(user, with_prices=True)
    sections = []
    for slug, ops in OPS.items():
        rows = views.get(slug)
        if rows is not None:
            built = []
            for token, amount, price in rows:
                qty = None if amount is None else service.format_amount(amount, token.decimals)
                value = None
                if amount and price is not None:
                    value = Decimal(amount) / (Decimal(10) ** token.decimals) * price
                built.append(homeview.Row(token.symbol, qty, amount == 0, price, value, token.contract, bool(token.is_default)))
            rows = built
        sections.append(homeview.Section(slug, ops.name, rows, always_show=(slug == "neo_n3")))
    return sections


async def _home_text(user) -> str:
    return homeview.render_home(await _home_sections(user), datetime.now(timezone.utc))


# A button under a photo can't "edit text" (a photo has none). Every screen in the bot edits
# the message its button is on, so for a photo message we send the screen as a new message and delete the photo.
_ORIGINAL_EDIT_TEXT = Message.edit_text


async def _edit_text_any(self, text, **kwargs):
    if getattr(self, "photo", None):
        extra = {k: v for k, v in kwargs.items() if k in ("reply_markup", "parse_mode", "link_preview_options")}
        try:
            await self.delete()
        except Exception:
            pass
        return await self.answer(text, **extra)
    return await _ORIGINAL_EDIT_TEXT(self, text, **kwargs)


Message.edit_text = _edit_text_any


class _KeyboardRefresh(BaseMiddleware):
    """The first time someone uses the bot after the buttons changed, send them the new buttons (once)."""

    async def __call__(self, handler, event, data):
        try:
            message = event if isinstance(event, Message) else getattr(event, "message", None)
            user = getattr(event, "from_user", None)
            if message is not None and user is not None and message.chat.type == "private" and not kbstore.seen(user.id):
                kbstore.mark(user.id)
                await message.answer("⌨️ I updated the menu buttons below. They are the same in text and photo mode.",
                                     reply_markup=shortcut_keyboard(user.id))
        except Exception:
            log.exception("Could not send the new buttons")
        return await handler(event, data)


router.message.outer_middleware(_KeyboardRefresh())
router.callback_query.outer_middleware(_KeyboardRefresh())


class _UsageCounter(BaseMiddleware):
    """Counts which part of the bot is used (a feature name and who, never what was typed)."""

    async def __call__(self, handler, event, data):
        try:
            user = getattr(event, "from_user", None)
            if user is not None and not user.is_bot:
                if isinstance(event, CallbackQuery):
                    usage.record(user.id, usage.feature_of(callback_data=event.data))
                elif isinstance(event, Message) and (event.text or "").startswith("/"):
                    command = event.text.split()[0][1:].split("@")[0]
                    usage.record(user.id, usage.feature_of(command=command))
        except Exception:
            log.exception("Usage count failed")
        return await handler(event, data)


router.message.outer_middleware(_UsageCounter())
router.callback_query.outer_middleware(_UsageCounter())


class _PhoneMenuMiddleware(BaseRequestMiddleware):
    """For people on the photo home page, take the menu buttons off every bot message (see menustrip)."""

    async def __call__(self, make_request, bot, method):
        markup = getattr(method, "reply_markup", None)
        chat_id = getattr(method, "chat_id", None)
        if isinstance(markup, InlineKeyboardMarkup) and isinstance(chat_id, int) and chat_id > 0:
            rows = menustrip.strip_rows(markup.inline_keyboard)
            if len(rows) != len(markup.inline_keyboard) or any(len(a) != len(b) for a, b in zip(rows, markup.inline_keyboard)):
                if rows:
                    method.reply_markup = InlineKeyboardMarkup(inline_keyboard=rows)
                elif type(method).__name__.startswith("Edit"):
                    method.reply_markup = InlineKeyboardMarkup(inline_keyboard=[])  # None would keep the old buttons
                else:
                    method.reply_markup = None
        return await make_request(bot, method)


def _page_row(page: int, pages: int) -> list:
    if pages <= 1:
        return []
    return [[
        InlineKeyboardButton(text="◀", callback_data=f"hp:{(page - 1) % pages}"),
        InlineKeyboardButton(text=f"{page + 1} / {pages}", callback_data=f"hp:{page}"),
        InlineKeyboardButton(text="▶", callback_data=f"hp:{(page + 1) % pages}"),
    ]]


# The last card each person was shown, per page: (Telegram file id, number of pages, caption).
# A photo Telegram already has can be shown again without uploading it, which is almost instant.
LAST_CARD: dict[tuple[int, int], tuple[str, int, str | None]] = {}


def _file_id_of(msg) -> str | None:
    try:
        return msg.photo[-1].file_id
    except Exception:
        return None


async def _chat_action(message: Message, action: str = "upload_photo") -> None:
    try:
        await message.bot.send_chat_action(message.chat.id, action)
    except Exception:
        pass


async def _send_home(event, user, page: int = 0) -> None:
    """Show the home page in the way this person chose: classic text, or the photo card.

    Photo card: if this person was shown a card before, that card appears at once (no upload), then the
    fresh one replaces it a moment later. The first time, a "sending a photo" status shows while it is built."""
    tg_id = event.from_user.id
    started = time.monotonic()
    message = event if isinstance(event, Message) else event.message
    on_photo = isinstance(event, CallbackQuery) and bool(getattr(message, "photo", None))
    switch_row: list = []  # Switch wallet lives on the 👛 Wallets page, not under the home page
    multi = False
    if viewpref.mode(tg_id) == "photo":
        shown = None  # the photo message on screen that the fresh card will replace
        try:
            cached = LAST_CARD.get((tg_id, page))
            if cached is not None:
                try:
                    file_id, cached_pages, cached_caption = cached
                    rows = _page_row(page, cached_pages) + switch_row
                    cached_markup = InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
                    if on_photo:
                        try:
                            await message.edit_media(InputMediaPhoto(media=file_id, caption=cached_caption), reply_markup=cached_markup)
                        except TelegramBadRequest as exc:
                            if "not modified" not in str(exc).lower():
                                raise
                        shown = message
                    else:
                        shown = await message.answer_photo(file_id, caption=cached_caption, reply_markup=cached_markup)
                        if isinstance(event, CallbackQuery):
                            try:
                                await message.delete()
                            except Exception:
                                pass
                except Exception as exc:  # the saved photo is gone or unusable: forget it and build a fresh one
                    log.info("Saved card not usable: %r", exc)
                    LAST_CARD.pop((tg_id, page), None)
                    shown = None
            elif not on_photo and isinstance(event, Message):
                asyncio.create_task(_chat_action(message))
            instant = time.monotonic() - started

            sections = await _home_sections(user)
            data_time = time.monotonic() - started - instant
            now = datetime.now(timezone.utc)
            try:  # token logos: from disk, or fetched for up to 3 seconds; never blocks Home for longer
                held = homecard.holdings(sections)
                lo, hi = homecard._chunk_bounds(max(0, min(page, homecard.page_count(sections) - 1)), homecard.STYLES[homecard.STYLE])
                logo_images = await token_logos.load(PRICES, held[lo:hi])
            except Exception as exc:
                log.warning("Token logos unavailable: %r", exc)
                logo_images = {}
            png, pages = await asyncio.to_thread(homecard.render, sections, now, page, logo_images)
            draw_time = time.monotonic() - started - instant - data_time
            page = max(0, min(page, pages - 1))
            caption = homecard.caption(sections, now, page, pages) or None
            rows = _page_row(page, pages) + switch_row  # page arrows (and Switch wallet): every other action is in the keyboard under the chat box
            markup = InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
            file = BufferedInputFile(png, filename="wallet.jpg")
            sent = None
            if shown is not None or on_photo:
                target = shown if shown is not None else message
                try:
                    sent = await target.edit_media(InputMediaPhoto(media=file, caption=caption), reply_markup=markup)
                except TelegramBadRequest as exc:
                    if "not modified" not in str(exc).lower():
                        raise
            else:
                if isinstance(event, CallbackQuery):
                    try:
                        await message.delete()
                    except Exception:
                        pass
                sent = await message.answer_photo(file, caption=caption, reply_markup=markup)
            file_id = _file_id_of(sent) if isinstance(sent, Message) else None
            if file_id:
                LAST_CARD[(tg_id, page)] = (file_id, pages, caption)
            log.info("Home photo: shown at once %.1fs, data %.1fs, drawing %.1fs, sending %.1fs",
                     instant if cached is not None else 0.0, data_time, draw_time,
                     time.monotonic() - started - instant - data_time - draw_time)
            return
        except TelegramBadRequest as exc:
            if "not modified" in str(exc).lower():
                return
            log.warning("Photo home failed, using text: %r", exc)
        except Exception as exc:
            log.warning("Photo home failed, using text: %r", exc)
        if shown is not None:  # an older card is already on screen: keep it rather than add a text page
            return
    text = await _home_text(user)
    log.info("Home text built in %.1fs", time.monotonic() - started)
    if on_photo:
        try:
            await message.delete()
        except Exception:
            pass
        await message.answer(text, reply_markup=home_keyboard(multi))
    else:
        await _show(event, text, home_keyboard(multi))


VIEW_TEXT = (
    "🖼 <b>Choose how your home page looks</b>\n\n"
    "📝 <b>Text</b>: the classic page, fast and simple.\n"
    "🖼 <b>Photo</b>: a picture card with your total, a chart and every token with its price. "
    "With many tokens it has pages (◀ ▶).\n\n"
    "You can change this any time with /view or the 🖼 button on the home page."
)


def _view_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Text (classic)", callback_data="view:text"),
         InlineKeyboardButton(text="🖼 Photo card", callback_data="view:photo")],
    ])


# ---------------------------------------------------------------------- history
def _history_entry(row, token, chain, names: dict, in_transit: bool = False) -> historyview.Entry:
    amount = int(row.amount)
    username = names.get(row.counterparty_user_id) if row.counterparty_user_id else None
    return historyview.Entry(
        kind=row.kind,
        status=row.status,
        amount_text=("+" if amount > 0 else "-") + service.format_amount(abs(amount), token.decimals),
        symbol=token.symbol,
        chain_name=chain.name,
        tx_hash=row.tx_hash,
        note=row.note,
        created_at=row.created_at,
        counterparty=f"@{username}" if username else None,
        url=historyview.explorer_url(SETTINGS.network, chain.slug, row.tx_hash),
        in_transit=in_transit,
    )


def _history_keyboard(pending: bool, page: int, pages: int, pending_count: int) -> InlineKeyboardMarkup:
    mode = "pending" if pending else "all"
    rows = [[
        InlineKeyboardButton(text=("" if pending else "• ") + "📜 All", callback_data="hist:all:0"),
        InlineKeyboardButton(text=("• " if pending else "") + f"⏳ Pending ({pending_count})", callback_data="hist:pending:0"),
    ]]
    if not pending and pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="◀ Newer", callback_data=f"hist:all:{page - 1}"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton(text="Older ▶", callback_data=f"hist:all:{page + 1}"))
        rows.append(nav)
    if pending and pending_count:
        rows.append([InlineKeyboardButton(text="🧹 Clear stuck ones", callback_data="histclr")])
    rows.append([
        InlineKeyboardButton(text="🔄 Check & refresh", callback_data=f"histchk:{mode}:{page}"),
        InlineKeyboardButton(text="🏠 Home", callback_data="home"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def history_show(event, pending: bool, page: int = 0, check: bool = False, note: str = "") -> None:
    if not _private(event):
        return await _done(event, PRIVATE_ONLY, alert=True)
    session, user = await _load(event)
    await _done(event)
    if check:  # ask the networks about anything still unconfirmed, then show the fresh state
        try:
            await reconcile.run_once(SESSIONS, OPS, user_id=user.id)
        except Exception:
            log.exception("History check failed")
    async with session:
        transit = await service.bridges_in_transit(session, user)
        transit_ids = {row.id for row, _, _ in transit}
        waiting = await service.history_count(session, user, True)
        pending_count = waiting + len(transit)
        if pending:
            rows = await service.history_page(session, user, True, 0, PENDING_LIMIT)
            page, pages, total = 0, 1, pending_count
            rows = sorted(rows + [t for t in transit if t[0].id not in {r.id for r, _, _ in rows}],
                          key=lambda x: (historyview.aware(x[0].created_at), x[0].id), reverse=True)
        else:
            total = await service.history_count(session, user, False)
            pages = max(1, -(-total // HISTORY_PAGE))
            page = min(max(page, 0), pages - 1)
            rows = await service.history_page(session, user, False, page * HISTORY_PAGE, HISTORY_PAGE)
        names = await service.usernames(session, {r.counterparty_user_id for r, _, _ in rows if r.counterparty_user_id})
    entries = [_history_entry(r, t, c, names, in_transit=r.id in transit_ids) for r, t, c in rows]
    text = historyview.render_history(
        entries, pending=pending, page=page, pages=pages, total=total, now=datetime.now(timezone.utc)
    )
    await _show(event, (note + "\n\n" if note else "") + text, _history_keyboard(pending, page, pages, pending_count))


async def history_open(event: Message | CallbackQuery, state: FSMContext):
    await state.clear()
    await history_show(event, pending=False)


async def pending_open(event: Message | CallbackQuery, state: FSMContext):
    await state.clear()
    await history_show(event, pending=True)


@router.callback_query(F.data.regexp(r"^hist(chk)?:(all|pending):\d+$"))
async def history_button(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    head, mode, page = cb.data.split(":")
    await history_show(cb, pending=(mode == "pending"), page=int(page), check=(head == "histchk"))


@router.callback_query(F.data == "histclr")
async def history_clear_stuck(cb: CallbackQuery, state: FSMContext):
    """Pending records that were never sent can't settle by themselves: check the network, then mark those as failed."""
    await state.clear()
    if not _private(cb):
        return await _done(cb, PRIVATE_ONLY, alert=True)
    session, user = await _load(cb)
    await session.close()
    try:
        await reconcile.run_once(SESSIONS, OPS, user_id=user.id)  # first let the network settle what it knows
        cleared = await reconcile.clear_stale(SESSIONS, user_id=user.id)
    except Exception:
        log.exception("Clearing stuck history failed")
        cleared = 0
    note = (f"🧹 Cleared {cleared} record(s) that were never sent. They are marked failed. Your balances are not affected."
            if cleared else "🧹 Nothing to clear: the rest are real transactions still waiting for the network.")
    await history_show(cb, pending=True, note=note)


def _help_networks() -> list[tuple[str, str]]:
    return [(ops.name, ops.native_symbol) for ops in OPS.values()]


def support_names() -> list[str]:
    """Everyone behind the Support buttons: SUPPORT_USERNAME first, then SUPPORT_EXTRA, no repeats."""
    out: list[str] = []
    for n in (SETTINGS.support_username, *SETTINGS.support_extra):
        if n and n.lower() not in [o.lower() for o in out]:
            out.append(n)
    return out


def _support_row() -> list:
    """Support buttons: one per person (@aziz1683 and @btc_mahadi unless changed in .env)."""
    names = support_names()
    if not names:
        return []
    return [[InlineKeyboardButton(text=f"🆘 Support · @{n}", url=f"https://t.me/{n}")] for n in names]


def _ai_row() -> list:
    return [[InlineKeyboardButton(text="🤖 AI Assistant", callback_data="ai:start")]]


def _help_keyboard(topic: str) -> InlineKeyboardMarkup:
    if topic == "main":
        keys = helptext.topic_keys(_any_bridge())
        rows = [
            [InlineKeyboardButton(text=label, callback_data=f"help:{key}") for key, label in keys[i : i + 2]]
            for i in range(0, len(keys), 2)
        ]
        return InlineKeyboardMarkup(inline_keyboard=rows + _ai_row() + _support_row() + [[InlineKeyboardButton(text="🏠 Home", callback_data="home")]])
    return InlineKeyboardMarkup(
        inline_keyboard=_ai_row() + _support_row() + [[
            InlineKeyboardButton(text="⬅️ Help menu", callback_data="help"),
            InlineKeyboardButton(text="🏠 Home", callback_data="home"),
        ]]
    )


def _help_page(topic: str) -> tuple[str, InlineKeyboardMarkup]:
    nets, bridge_on = _help_networks(), _any_bridge()
    text = None if topic == "main" else helptext.topic_text(topic, nets, bridge_on)
    if text is None:
        topic, text = "main", helptext.main_text(nets, bridge_on)
    return text, _help_keyboard(topic)


WELCOME = (
    "👋 <b>Welcome to the NeoRedPill Wallet!</b>\n"
    "Your shortcuts are now under the chat box 👇\n"
    "Not sure where to begin? Tap <b>❓ Help</b> or send /help."
)


# ------------------------------------------------------------------ /start, home
@router.message(CommandStart())
async def start(message: Message, command: CommandObject, state: FSMContext):
    arg = (command.args or "").strip()
    if arg.startswith("quiz_") and _private(message):  # the organizer's private link from /quiz in a group
        return await quiz_deeplink(message, state, arg[len("quiz_"):])
    if arg.startswith("air_") and _private(message):  # a link to an airdrop shared by its creator
        session, _user = await _load(message)
        await session.close()
        await message.answer(WELCOME, reply_markup=shortcut_keyboard(message.from_user.id))
        cid, _, ref = arg[len("air_"):].partition("_")
        c = CAMPAIGNS.get(cid)
        if c is None:
            return await message.answer("This airdrop is not available.")
        if ref.isdigit() and c.record_referral(message.from_user.id, int(ref)):
            _air_save()
        return await _air_show_event(message, c)
    if not _private(message):
        await _reply(message, "Please message me privately to use your wallet. 🔒 In a group, /help shows what I can do.")
        return
    session, user = await _load(message)
    await session.close()
    await message.answer(WELCOME, reply_markup=shortcut_keyboard(message.from_user.id))
    if viewpref.get(message.from_user.id) is None:  # first time: let them pick the look of the home page
        await message.answer(VIEW_TEXT, reply_markup=_view_keyboard())
        return
    await _send_home(message, user)


@router.callback_query(F.data == "home")
async def home(event: Message | CallbackQuery, state: FSMContext):
    await state.clear()
    if not _private(event):
        return await _done(event, PRIVATE_ONLY, alert=True)
    session, user = await _load(event)
    await session.close()
    await _done(event)
    await _send_home(event, user)


@router.callback_query(F.data.startswith("hp:"))
async def home_page(cb: CallbackQuery, state: FSMContext):
    if not _private(cb):
        return await _done(cb, PRIVATE_ONLY, alert=True)
    try:
        page = int(cb.data.split(":", 1)[1])
    except ValueError:
        page = 0
    session, user = await _load(cb)
    await session.close()
    await _done(cb)
    await _send_home(cb, user, page)


TB_TEXT = ("📈 <b>Trade</b>: see where a token you hold is listed and trade it on a DEX.\n"
           "🌉 <b>Bridge</b>: move GAS between your Neo N3 and Neo X wallets.")


@router.callback_query(F.data == "tb")
async def tb_menu(event: Message | CallbackQuery, state: FSMContext):
    await state.clear()
    if not _private(event):
        return await _done(event, PRIVATE_ONLY, alert=True)
    await _done(event)
    row = [InlineKeyboardButton(text="📈 Trade", callback_data="trade")]
    if _any_bridge():
        row.append(InlineKeyboardButton(text="🌉 Bridge", callback_data="bridge"))
    await _show(event, TB_TEXT, InlineKeyboardMarkup(inline_keyboard=[row, BACK]))


@router.callback_query(F.data == "trade")
async def trade_start(event: Message | CallbackQuery, state: FSMContext):
    """📈 Trade: pick one of your tokens; the next page shows its contract address and where to trade it."""
    await state.clear()
    if not _private(event):
        return await _done(event, PRIVATE_ONLY, alert=True)
    session, user = await _load(event)
    async with session:
        counts = {}
        for slug, ops in OPS.items():
            chain = await service.get_chain(session, slug)
            if chain is None:
                continue
            n = len(await service.visible_tokens(session, user, chain.id))
            if n:
                counts[slug] = n
    await _done(event)
    if not counts:
        return await _show(event, "You have no tokens yet. Use ➕ Add token or 📥 Deposit first.", home_only())
    buttons = [InlineKeyboardButton(text=f"{_xb_emoji(slug)} {OPS[slug].name}", callback_data=f"trc:{slug}") for slug in counts]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    text = ("📈 <b>Trade</b>\n\n<b>Step 1.</b> Which network is the token on?\n"
            "Next you pick the token, then I show its contract address and where you can buy or sell it.")
    await _show(event, text, InlineKeyboardMarkup(inline_keyboard=rows + [BACK]))


@router.callback_query(F.data.startswith("trc:"))
async def trade_chain(cb: CallbackQuery, state: FSMContext):
    """Step 2 of 📈 Trade: the tokens the person has on the chosen network."""
    if not _private(cb):
        return await _done(cb, PRIVATE_ONLY, alert=True)
    slug = cb.data.split(":", 1)[1]
    ops = OPS.get(slug)
    if ops is None:
        return await _done(cb, "That network is not available right now.", alert=True)
    session, user = await _load(cb)
    async with session:
        chain = await service.get_chain(session, slug)
        tokens = await service.visible_tokens(session, user, chain.id) if chain else []
    await _done(cb)
    if not tokens:
        return await _show(cb, "You have no tokens on this network.", home_only())
    main_tokens = [t for t in tokens if not service.is_stablecoin(slug, t.contract)]  # stablecoins are offered on the coin's page
    buttons = [InlineKeyboardButton(text=token.symbol[:24], callback_data=f"trd:{slug}:{token.id}") for token in (main_tokens or tokens)]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    await _show(cb, f"📈 <b>Trade · {html.escape(ops.name)}</b>\n\n<b>Step 2.</b> Which token?",
                InlineKeyboardMarkup(inline_keyboard=rows + [[InlineKeyboardButton(text="⬅️ Back", callback_data="trade")]]))


@router.callback_query(F.data.startswith("trd:"))
async def trade_pick(cb: CallbackQuery, state: FSMContext):
    if not _private(cb):
        return await _done(cb, PRIVATE_ONLY, alert=True)
    try:
        _, slug, token_id = cb.data.split(":", 2)
        token_id = int(token_id)
    except ValueError:
        return await _done(cb)
    ops = OPS.get(slug)
    if ops is None:
        return await _done(cb, "That network is not available right now.", alert=True)
    session, user = await _load(cb)
    async with session:
        chain = await service.get_chain(session, slug)
        tokens = await service.visible_tokens(session, user, chain.id) if chain else []
    token = next((t for t in tokens if t.id == token_id), None)
    if token is None:
        return await _done(cb, "That token is not on your list any more.", alert=True)
    await _done(cb, "Looking it up…")
    info = None
    try:
        info = await asyncio.wait_for(market.lookup(PRICES, token, slug), 30)
        text = market.render(token, slug, ops.name, info)
        links = market.trade_buttons(token, slug, info)
    except Exception as exc:
        log.warning("Trade lookup failed: %r", exc)
        text = "I couldn't reach the price and exchange services right now. Please try again in a moment."
        links = []
    rows, pair = [], []
    for label, url in links:
        pair.append(InlineKeyboardButton(text=label, url=url))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    listed = dexquote.dexes_for(slug, [p.dex_id for p in info.dex], {p.dex_id: p.versions for p in info.dex}) if info else []
    try:
        dexes = await _best_dexes(slug, token, ops, listed)
    except Exception:
        log.exception("DEX check failed")
        dexes = list(listed)
    dex_rows = _dex_buttons(token, slug, dexes)
    if slug == "neo_n3" and normalize_hash_safe(token.contract) not in (ftwswap.GAS_HASH, "native"):
        dex_rows = [[InlineKeyboardButton(text="💱 Trade here with FTW Swap", callback_data=f"f:{token.id}")]] + dex_rows
        dex_rows = dex_rows[:1] + [[InlineKeyboardButton(text="💱 Trade here with Flamingo", callback_data=f"m:{token.id}")]] + dex_rows[1:]
    if dex_rows:  # the swap happens in the bot, so the outside swap links are not needed
        rows = [r for r in rows if not any(str(b.text).startswith("🔄") for b in r)]
    rows = dex_rows + rows
    if str(token.contract).lower() == "native" and slug in service.STABLECOINS and ops.key_type != "solana":
        stables = [t for t in tokens if service.is_stablecoin(slug, t.contract)]
        if stables:
            rows.append([InlineKeyboardButton(text=f"💵 Trade {token.symbol} for {t.symbol}", callback_data=f"trd:{slug}:{t.id}")
                         for t in stables[:3]])
    rows.append([InlineKeyboardButton(text="⬅️ Tokens", callback_data=f"trc:{slug}")])
    await _show(cb, text[:4000], InlineKeyboardMarkup(inline_keyboard=rows))


@router.message(Command("view"))
async def view_command(message: Message, state: FSMContext):
    await state.clear()
    if not _private(message):
        return await _reply(message, PRIVATE_ONLY)
    await message.answer(VIEW_TEXT, reply_markup=_view_keyboard())


@router.callback_query(F.data == "viewmenu")
async def view_menu(cb: CallbackQuery, state: FSMContext):
    if not _private(cb):
        return await _done(cb, PRIVATE_ONLY, alert=True)
    await _done(cb)
    await _show(cb, VIEW_TEXT, _view_keyboard())


@router.callback_query(F.data.in_({"view:text", "view:photo"}))
async def view_choose(cb: CallbackQuery, state: FSMContext):
    if not _private(cb):
        return await _done(cb, PRIVATE_ONLY, alert=True)
    choice = cb.data.split(":", 1)[1]
    viewpref.set_mode(cb.from_user.id, choice)
    await _done(cb, "Photo card on 🖼" if choice == "photo" else "Text page on 📝")
    session, user = await _load(cb)
    await session.close()
    # the keyboard under the chat box changes with the view, so send it with a short message
    note = ("🖼 Photo card is on. All your actions are in the keyboard under the chat box 👇" if choice == "photo"
            else "📝 Text page is on. Your shortcuts are under the chat box 👇")
    await cb.message.answer(note, reply_markup=shortcut_keyboard(cb.from_user.id))
    await _send_home(cb, user)


@router.message(Command("help"))
async def help_command(message: Message, state: FSMContext):
    await state.clear()
    text, markup = _help_page("main")
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "events")
async def events_info(cb: CallbackQuery, state: FSMContext):
    """The Rain & Quiz button: how they work, and where you prepare one in private."""
    await state.clear()
    if not _private(cb):
        return await _done(cb, PRIVATE_ONLY, alert=True)
    await _done(cb)
    text = helptext.topic_text("events", _help_networks(), _any_bridge())
    waiting = len(_pending(cb.from_user.id, "rain")) + len(_pending(cb.from_user.id, "quiz"))
    rows = [[
        InlineKeyboardButton(text="🌧 Prepare a rain", callback_data="prep:rain"),
        InlineKeyboardButton(text="🧠 Prepare a quiz", callback_data="prep:quiz"),
    ]]
    if PREPARED.get(cb.from_user.id):
        rows.append([InlineKeyboardButton(text=f"📋 My rains & quizzes ({waiting} waiting)", callback_data="mine")])
    rows.append([
        InlineKeyboardButton(text="⬅️ Help menu", callback_data="help"),
        InlineKeyboardButton(text="🏠 Home", callback_data="home"),
    ])
    await _show(cb, text, InlineKeyboardMarkup(inline_keyboard=rows))


def _mine_page(tg_id: int) -> tuple[str, InlineKeyboardMarkup]:
    items = list(PREPARED.get(tg_id, {}).values())
    if not items:
        return ("📋 <b>My rains & quizzes</b>\n\nYou haven't prepared anything yet.",
                InlineKeyboardMarkup(inline_keyboard=[EVENTS_BACK]))
    lines, rows = [], []
    for p in items:
        icon = "🌧" if p.kind == "rain" else "🧠"
        word = "rain" if p.kind == "rain" else "quiz"
        if p.status == "pending":
            state_text = f"⏳ waiting. In your group send <code>/{word} {p.code}</code>"
            rows.append([InlineKeyboardButton(text=f"🗑 Cancel {p.code}", callback_data=f"pcancel:{p.code}")])
        elif p.status == "live":
            state_text = f"🟢 {html.escape(p.note)}"
            if p.kind == "rain":
                rows.append([InlineKeyboardButton(text=f"🛑 Stop {p.code}", callback_data=f"pstop:{p.code}")])
        elif p.status == "done":
            state_text = f"✅ finished: {html.escape(p.note)}"
        else:
            state_text = "🚫 cancelled"
        lines.append(f"{icon} <code>{p.code}</code> · {_prepared_describe(p)}\n    {state_text}")
    if any(p.status in ("done", "cancelled") for p in items):
        rows.append([InlineKeyboardButton(text="🧹 Clear finished", callback_data="pclear")])
    rows.append(EVENTS_BACK)
    return "📋 <b>My rains & quizzes</b>\n\n" + "\n\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "mine")
async def mine(cb: CallbackQuery):
    if not _private(cb):
        return await _done(cb, PRIVATE_ONLY, alert=True)
    await _done(cb)
    text, markup = _mine_page(cb.from_user.id)
    await _show(cb, text, markup)


@router.callback_query(F.data.startswith("pcancel:"))
async def prepared_cancel(cb: CallbackQuery):
    item = PREPARED.get(cb.from_user.id, {}).get(cb.data.split(":", 1)[1])
    if item is None or item.status != "pending":
        await cb.answer("That one is no longer waiting.", show_alert=True)
    else:
        item.status, item.note = "cancelled", ""
        await cb.answer(f"{item.code} cancelled.")
    text, markup = _mine_page(cb.from_user.id)
    await _show(cb, text, markup)


@router.callback_query(F.data.startswith("pstop:"))
async def prepared_stop(cb: CallbackQuery):
    code = cb.data.split(":", 1)[1]
    rain = next((r for r in RAINS.values() if r.code == code and r.starter_tg_id == cb.from_user.id and r.state == "open"), None)
    if rain is None:
        await cb.answer("That rain is not running any more.", show_alert=True)
    else:
        rain.state = "cancelled"
        rain.wake.set()
        await cb.answer("Stopping the rain…")
        await asyncio.sleep(2)
    text, markup = _mine_page(cb.from_user.id)
    await _show(cb, text, markup)


@router.callback_query(F.data == "pclear")
async def prepared_clear(cb: CallbackQuery):
    mine_items = PREPARED.get(cb.from_user.id, {})
    for code in [c for c, p in mine_items.items() if p.status in ("done", "cancelled")]:
        del mine_items[code]
    await cb.answer("Cleared.")
    text, markup = _mine_page(cb.from_user.id)
    await _show(cb, text, markup)


EVENTS_BACK = [InlineKeyboardButton(text="⬅️ Rain & Quiz", callback_data="events"), InlineKeyboardButton(text="🏠 Home", callback_data="home")]


async def _prep_token_page(cb: CallbackQuery, state: FSMContext, kind: str) -> None:
    """Step 1 of preparing: which of your tokens."""
    if not _private(cb):
        return await _done(cb, PRIVATE_ONLY, alert=True)
    await state.clear()
    if len(_pending(cb.from_user.id, kind)) >= events.MAX_PENDING:
        await _done(cb)
        return await _show(cb, (f"You already have {events.MAX_PENDING} waiting. Cancel some under 📋 My rains & quizzes first."),
                           InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📋 My rains & quizzes", callback_data="mine")], EVENTS_BACK]))
    session, user = await _load(cb)
    await session.close()
    await _done(cb)
    await _tp_networks(cb, user, kind)


@router.callback_query(F.data == "prep:rain")
async def prep_rain(cb: CallbackQuery, state: FSMContext):
    await _prep_token_page(cb, state, "rain")


@router.callback_query(F.data == "prep:quiz")
async def prep_quiz(cb: CallbackQuery, state: FSMContext):
    await _prep_token_page(cb, state, "quiz")


@router.callback_query(F.data.regexp(r"^prtok:(rain|quiz):\d+$"))
async def prep_token(cb: CallbackQuery, state: FSMContext):
    """Step 2: the token is chosen; ask for the amount (rain) or how the prize works (quiz)."""
    if not _private(cb):
        return await _done(cb, PRIVATE_ONLY, alert=True)
    _tag, kind, token_id_text = cb.data.split(":")
    session, user = await _load(cb)
    async with session:
        token = await session.get(Token, int(token_id_text))
        chain = await session.get(Chain, token.chain_id) if token else None
        if chain is None or chain.slug not in OPS:
            return await cb.answer("That network is not available right now.", show_alert=True)
        ops = OPS[chain.slug]
        held = None
        try:
            wallet = await service.get_wallet(session, user, ops.key_type)
            held = (await ops.balances(wallet.address, [token])).get(ops.token_key(token))
        except Exception:
            log.exception("Could not read the balance while preparing an event")
        symbol, decimals, net_name, slug, tid = token.symbol, token.decimals, ops.name, chain.slug, token.id
    await _done(cb)
    await state.clear()
    await state.update_data(kind=kind, token_id=tid, slug=slug, symbol=symbol, decimals=decimals,
                            held=held if held is None else int(held))
    sym = html.escape(symbol)
    have = f"You have <b>{service.format_amount(held, decimals)} {sym}</b>.\n\n" if held is not None else ""
    if kind == "rain":
        await state.set_state(PrepFlow.rain_amount)
        return await _show(cb, (
            f"🌧 <b>Rain with {sym}</b> on {html.escape(net_name)}\n{have}"
            f"How much {sym} do you want to rain <b>in total</b>? Send a number, like <code>100</code>.\n/cancel stops this."
        ), InlineKeyboardMarkup(inline_keyboard=[EVENTS_BACK]))
    await _show(cb, (
        f"🧠 <b>Quiz with {sym}</b> on {html.escape(net_name)}\n{have}"
        "Who should win?"
    ), InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚡ The fastest correct answers, on every question", callback_data="prqstyle:question")],
        [InlineKeyboardButton(text="🏆 The best scores at the end (a tie goes to the faster player)", callback_data="prqstyle:top")],
        EVENTS_BACK,
    ]))


def _number_in(text: str, decimals: int) -> int:
    """The amount typed by the person, in raw units; raises EventError with a plain message."""
    try:
        value = neo_send.parse_amount(text.strip(), decimals)
    except neo_send.SendError as exc:
        raise events.EventError(str(exc))
    if value <= 0:
        raise events.EventError("The amount must be more than zero.")
    return value


@router.message(PrepFlow.rain_amount, F.text)
async def prep_rain_amount(message: Message, state: FSMContext):
    if not _private(message):
        return
    data = await state.get_data()
    try:
        value = _number_in(message.text, data["decimals"])
    except events.EventError as exc:
        return await message.answer(f"❌ {html.escape(str(exc))} Send the total amount again, like <code>100</code>.")
    sym = html.escape(data["symbol"])
    if data.get("held") is not None and value > data["held"]:
        return await message.answer(
            f"❌ You only have {service.format_amount(data['held'], data['decimals'])} {sym}. Send a smaller amount."
        )
    await state.update_data(amount_text=message.text.strip())
    await state.set_state(PrepFlow.rain_people)
    await message.answer(
        f"Rain of <b>{html.escape(message.text.strip())} {sym}</b>. How many people can claim it?\n"
        f"Pick a number, or send your own (1 to {events.MAX_RAIN_PEOPLE}).",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=str(n), callback_data=f"prpeople:{n}") for n in (3, 5, 10, 20)]
        ]),
    )


async def _rain_people_chosen(event, state: FSMContext, people: int) -> None:
    data = await state.get_data()
    reply = event.message.answer if isinstance(event, CallbackQuery) else event.answer
    if not 1 <= people <= events.MAX_RAIN_PEOPLE:
        return await reply(f"❌ Pick a number from 1 to {events.MAX_RAIN_PEOPLE}.")
    try:
        events.rain_amounts(data["amount_text"], data["decimals"], people)
    except events.EventError as exc:
        return await reply(f"❌ {html.escape(str(exc))} Pick fewer people.")
    await state.update_data(people=people)
    await reply(
        f"{people} people. How should the {html.escape(data['symbol'])} be shared?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⚖️ Equal shares (everyone gets the same)", callback_data="prmode:fixed")],
            [InlineKeyboardButton(text="🎲 Random shares (everyone gets a different amount)", callback_data="prmode:random")],
        ]),
    )


@router.callback_query(PrepFlow.rain_people, F.data.regexp(r"^prpeople:\d+$"))
async def prep_rain_people_button(cb: CallbackQuery, state: FSMContext):
    await _done(cb)
    await _rain_people_chosen(cb, state, int(cb.data.split(":")[1]))


@router.message(PrepFlow.rain_people, F.text)
async def prep_rain_people_text(message: Message, state: FSMContext):
    if not _private(message):
        return
    if not message.text.strip().isdigit():
        return await message.answer(f"❌ Send a whole number from 1 to {events.MAX_RAIN_PEOPLE}.")
    await _rain_people_chosen(message, state, int(message.text.strip()))


@router.callback_query(PrepFlow.rain_people, F.data.regexp(r"^prmode:(fixed|random)$"))
async def prep_rain_mode(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if "people" not in data:
        return await cb.answer("Pick the number of people first.", show_alert=True)
    mode = cb.data.split(":")[1]
    await _done(cb)
    req = events.RainRequest(data["amount_text"], data["symbol"], data["slug"], data["people"], events.MAX_MINUTES, mode)
    if len(_pending(cb.from_user.id, "rain")) >= events.MAX_PENDING:
        await state.clear()
        return await cb.message.answer(
            f"You already have {events.MAX_PENDING} rains waiting. Cancel some under 📋 My rains & quizzes first."
        )
    code = events.new_code("rain", _all_codes())
    PREPARED.setdefault(cb.from_user.id, {})[code] = events.Prepared(code, "rain", req=req)
    await state.clear()
    sym = html.escape(data["symbol"])
    text = (
        f"✅ <b>Rain ready</b>: {html.escape(req.amount_text)} {sym}, the first {req.people} people, "
        f"{'equal shares' if mode == 'fixed' else 'random shares'}.\n\n"
        f"Its code is <code>{code}</code>. Go to any group where I am a member and send <code>/rain {code}</code>. "
        "It starts at once, with a Cancel button for you.\n"
        "You can prepare more rains; each gets its own code. See them all under 📋 My rains & quizzes."
    )
    try:
        amounts, total = events.rain_amounts(req.amount_text, data["decimals"], req.people, mode)
        problem = await _funds_error(data["slug"], (await _user_id(cb)), data["token_id"], max(amounts), total)
        if problem:
            text += f"\n\n⚠️ Right now: {html.escape(problem)} Top up before you start it."
        fee_line = await _fee_line(data["slug"], (await _user_id(cb)), data["token_id"], req.people)
        if fee_line:
            text += f"\n\n{fee_line} That is the most it can cost if all {req.people} people claim."
    except Exception:
        log.exception("Funds check after preparing a rain failed")
    await cb.message.answer(text, reply_markup=home_only())


async def _user_id_for(tg_id: int) -> int:
    async with SESSIONS() as session:
        user = await service.find_user_by_telegram_id(session, tg_id)
        return user.id


async def _user_id(cb: CallbackQuery) -> int:
    session, user = await _load(cb)
    async with session:
        return user.id


@router.callback_query(F.data.regexp(r"^prqstyle:(question|top)$"))
async def prep_quiz_style(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if data.get("kind") != "quiz":
        return await cb.answer("Start again from the Rain & Quiz button.", show_alert=True)
    style = cb.data.split(":")[1]
    await _done(cb)
    await state.update_data(style=style)
    await cb.message.answer(
        "How should the reward work?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💰 Fixed: every winner gets the same amount", callback_data="prqkind:fixed")],
            [InlineKeyboardButton(text="🎲 Random: a total amount is split randomly among the winners", callback_data="prqkind:random")],
            [InlineKeyboardButton(text="🥇 By place: e.g. 1000 for 1st, 500 for 2nd, 250 for 3rd", callback_data="prqkind:ranked")],
        ]),
    )


@router.callback_query(F.data.regexp(r"^prqkind:(fixed|random|ranked)$"))
async def prep_quiz_kind(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if data.get("kind") != "quiz" or "style" not in data:
        return await cb.answer("Start again from the Rain & Quiz button.", show_alert=True)
    await _done(cb)
    mode = cb.data.split(":")[1]
    await state.update_data(mode=mode)
    limit = events.winners_limit(data["style"])
    if mode == "ranked":  # the list of amounts decides how many winners there are
        await state.update_data(winners=1)
        await state.set_state(PrepFlow.quiz_amount)
        where = "on each question" if data["style"] == "question" else "at the end"
        return await cb.message.answer(
            f"Send the prize for each place {where}, best first, separated by commas. "
            f"Example: <code>1000, 500, 250</code> = 1000 {html.escape(data['symbol'])} for 1st, 500 for 2nd, 250 for 3rd. "
            f"Up to {limit} places."
        )
    await state.set_state(PrepFlow.quiz_winners)
    if data["style"] == "question":
        ask = f"How many people can win on <b>each question</b>? (the fastest correct answers, 1 to {limit})"
    else:
        ask = f"How many <b>top players</b> win at the end? (1 to {limit})"
    await cb.message.answer(
        ask + "\nPick a number, or send your own.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=str(n), callback_data=f"prqwin:{n}") for n in sorted({1, 2, 3, 5, 10} & set(range(1, limit + 1)))
        ]]),
    )


async def _quiz_winners_chosen(event, state: FSMContext, winners: int) -> None:
    data = await state.get_data()
    reply = event.message.answer if isinstance(event, CallbackQuery) else event.answer
    limit = events.winners_limit(data["style"])
    if not 1 <= winners <= limit:
        return await reply(f"❌ Pick a number from 1 to {limit}.")
    await state.update_data(winners=winners)
    await state.set_state(PrepFlow.quiz_amount)
    sym = html.escape(data["symbol"])
    if data["mode"] == "fixed":
        ask = f"How much {sym} does <b>each winner</b> get? Send a number, like <code>10</code>."
    else:
        ask = (f"What <b>total</b> amount of {sym} should be shared randomly among all the winners? "
               "Send a number, like <code>100</code>.")
    await reply(ask)


@router.callback_query(PrepFlow.quiz_winners, F.data.regexp(r"^prqwin:\d+$"))
async def prep_quiz_winners_button(cb: CallbackQuery, state: FSMContext):
    await _done(cb)
    await _quiz_winners_chosen(cb, state, int(cb.data.split(":")[1]))


@router.message(PrepFlow.quiz_winners, F.text)
async def prep_quiz_winners_text(message: Message, state: FSMContext):
    if not _private(message):
        return
    if not message.text.strip().isdigit():
        return await message.answer("❌ Send a whole number.")
    await _quiz_winners_chosen(message, state, int(message.text.strip()))


async def _ask_timer(message: Message, state: FSMContext, amount_text: str) -> None:
    data = await state.get_data()
    await state.update_data(quiz_amount_text=amount_text)
    await state.set_state(PrepFlow.quiz_seconds)
    summary = _reward_summary(data["style"], data["mode"], data["winners"], amount_text, data["symbol"])
    await message.answer(
        f"Reward set: {summary}.\n\n⏱ How long does each question stay open? Pick one, or send your own "
        "(like <code>45</code>, <code>90s</code> or <code>2 min</code>; from 5 seconds to 10 minutes).",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=events.format_seconds(n), callback_data=f"prqsec:{n}") for n in (10, 20, 30)],
            [InlineKeyboardButton(text=events.format_seconds(n), callback_data=f"prqsec:{n}") for n in (45, 60, 120)],
        ]),
    )


async def _ask_first_question(message: Message, state: FSMContext, seconds: int) -> None:
    await state.update_data(seconds=seconds, questions=[])
    await state.set_state(PrepFlow.q_text)
    await message.answer(
        f"Timer set: <b>{events.format_seconds(seconds)}</b> per question.\n\n"
        f"Now the questions (up to {events.MAX_QUESTIONS}). <b>Question 1</b>: send the question."
    )


@router.callback_query(PrepFlow.quiz_seconds, F.data.regexp(r"^prqsec:\d+$"))
async def prep_quiz_seconds_button(cb: CallbackQuery, state: FSMContext):
    await _done(cb)
    await _ask_first_question(cb.message, state, int(cb.data.split(":")[1]))


@router.message(PrepFlow.quiz_seconds, F.text)
async def prep_quiz_seconds_text(message: Message, state: FSMContext):
    if not _private(message):
        return
    try:
        seconds = events.parse_seconds(message.text)
    except events.EventError as exc:
        return await message.answer(f"❌ {html.escape(str(exc))}")
    await _ask_first_question(message, state, seconds)


@router.message(PrepFlow.quiz_amount, F.text)
async def prep_quiz_amount(message: Message, state: FSMContext):
    if not _private(message):
        return
    data = await state.get_data()
    try:
        if data["mode"] == "ranked":
            places = len(events.parse_rank_amounts(message.text, data["decimals"]))
            if places > events.winners_limit(data["style"]):
                raise events.EventError(f"At most {events.winners_limit(data['style'])} places.")
            await state.update_data(winners=places)
            data["winners"] = places
        else:
            _number_in(message.text, data["decimals"])
        events.plan_amounts(data["style"], data["mode"], data["winners"], message.text.strip(), data["decimals"], 1)
    except events.EventError as exc:
        return await message.answer(f"❌ {html.escape(str(exc))} Send the amount again.")
    await _ask_timer(message, state, message.text.strip())


@router.message(PrepFlow.q_text, F.text)
async def prep_q_text(message: Message, state: FSMContext):
    if not _private(message):
        return
    text = message.text.strip()
    if not text or len(text) > events.MAX_QUESTION_LEN:
        return await message.answer(f"❌ The question must be 1 to {events.MAX_QUESTION_LEN} characters. Send it again.")
    await state.update_data(cur_text=text)
    await state.set_state(PrepFlow.q_correct)
    await message.answer("Now the <b>correct answer</b>.")


@router.message(PrepFlow.q_correct, F.text)
async def prep_q_correct(message: Message, state: FSMContext):
    if not _private(message):
        return
    text = message.text.strip()
    if not text or len(text) > events.MAX_OPTION_LEN:
        return await message.answer(f"❌ An answer must be 1 to {events.MAX_OPTION_LEN} characters. Send it again.")
    await state.update_data(cur_correct=text)
    await state.set_state(PrepFlow.q_wrong)
    await message.answer("Now the <b>wrong answers</b>: 1 to 3, one on each line. I mix all the answers up for the players.")


def _questions_done_text(data: dict, count: int, code: str) -> str:
    summary = _reward_summary(data["style"], data["mode"], data["winners"], data["quiz_amount_text"], data["symbol"])
    return (f"✅ <b>Quiz ready</b>: {count} question{'s' if count != 1 else ''}, "
            f"{events.format_seconds(data.get('seconds', events.QUESTION_SECONDS))} each. Reward: {summary}.\n\n"
            f"Its code is <code>{code}</code>. Go to any group where I am a member and send <code>/quiz {code}</code>. "
            "It starts at once, and the first message has a Cancel button for you.")


@router.message(PrepFlow.q_wrong, F.text)
async def prep_q_wrong(message: Message, state: FSMContext):
    if not _private(message):
        return
    data = await state.get_data()
    questions = list(data.get("questions", []))
    wrong = [ln.strip() for ln in message.text.splitlines() if ln.strip()]
    try:
        events.build_question(len(questions) + 1, data["cur_text"], data["cur_correct"], wrong)
    except events.EventError as exc:
        return await message.answer(f"❌ {html.escape(str(exc))} Send the wrong answers again, one per line.")
    questions.append([data["cur_text"], data["cur_correct"], wrong])
    await state.update_data(questions=questions)
    if len(questions) >= events.MAX_QUESTIONS:
        return await _finish_quiz_draft(message, state, message.from_user.id)
    await message.answer(
        f"Question {len(questions)} saved.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="➕ Add another question", callback_data="prq:more"),
            InlineKeyboardButton(text="✅ That's all", callback_data="prq:done"),
        ]]),
    )


async def _finish_quiz_draft(message: Message, state: FSMContext, tg_id: int) -> None:
    data = await state.get_data()
    questions = [events.Question(t, c, w) for t, c, w in data.get("questions", [])]
    if len(_pending(tg_id, "quiz")) >= events.MAX_PENDING:
        await state.clear()
        return await message.answer(
            f"You already have {events.MAX_PENDING} quizzes waiting. Cancel some under 📋 My rains & quizzes first."
        )
    code = events.new_code("quiz", _all_codes())
    PREPARED.setdefault(tg_id, {})[code] = events.Prepared(code, "quiz", quiz={
        "amount_text": data["quiz_amount_text"], "symbol": data["symbol"], "network": data["slug"], "questions": questions,
        "style": data["style"], "mode": data["mode"], "winners": data["winners"],
        "seconds": data.get("seconds", events.QUESTION_SECONDS),
    })
    await state.clear()
    text = _questions_done_text(data, len(questions), code)
    try:  # a heads-up if the balance doesn't cover the most this quiz can pay
        amounts, _w = events.plan_amounts(data["style"], data["mode"], data["winners"], data["quiz_amount_text"],
                                          data["decimals"], len(questions))
        total = sum(amounts)
        problem = await _funds_error(data["slug"], await _user_id_for(tg_id), data["token_id"], max(amounts), total)
        if problem:
            text += f"\n\n⚠️ Right now: {html.escape(problem)} Top up before you start it."
    except Exception:
        log.exception("Funds check after preparing a quiz failed")
    await message.answer(text, reply_markup=home_only())


@router.callback_query(F.data == "prq:more")
async def prep_q_more(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    count = len(data.get("questions", []))
    if not count:
        return await cb.answer("Start again from the Rain & Quiz button.", show_alert=True)
    await _done(cb)
    await state.set_state(PrepFlow.q_text)
    await cb.message.answer(f"<b>Question {count + 1}</b>: send the question.")


@router.callback_query(F.data == "prq:done")
async def prep_q_done(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get("questions"):
        return await cb.answer("Start again from the Rain & Quiz button.", show_alert=True)
    await _done(cb)
    await _finish_quiz_draft(cb.message, state, cb.from_user.id)


@router.callback_query(F.data.regexp(r"^help(:\w+)?$"))
async def help_button(cb: CallbackQuery):
    topic = cb.data.partition(":")[2] or "main"
    text, markup = _help_page(topic)
    await _show(cb, text, markup)
    await cb.answer()


@router.callback_query(F.data == "tokmenu")
async def token_menu(event: Message | CallbackQuery, state: FSMContext):
    """One button for both: add a token or remove one."""
    await state.clear()
    await _done(event)
    await _show(event, "🪙 <b>Tokens</b>\n\nAdd a token to your wallet by its contract address, or remove one you don't want to see.",
                InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="➕ Add a token", callback_data="addtok"),
                     InlineKeyboardButton(text="🗑 Remove a token", callback_data="rmtok")],
                    [InlineKeyboardButton(text="🏠 Home", callback_data="home")]]))


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Cancelled.")


# These two handlers are registered before the flows below on purpose: a shortcut button or
# command pressed in the middle of a flow (typing an address, an amount...) must win over
# that flow's "any text" handler, and it clears the flow.
@router.message(Command("home", "wallets", "send", "bridge", "addtoken", "history", "pending", "keys", "import", "export", "removetoken", "trade", "vote", "earn"))
async def command_shortcut(message: Message, command: CommandObject, state: FSMContext):
    action = {
        "home": home, "wallets": wallets, "send": send_start, "bridge": bridge_start,
        "addtoken": add_start, "history": history_open, "pending": pending_open,
        "keys": keys_menu, "import": keys_menu, "export": keys_menu, "removetoken": remove_start,
        "trade": trade_start, "vote": vote_home, "earn": earn_menu,
    }
    await action[command.command](message, state)


@router.message(F.text.in_(SHORTCUT_TEXTS))
async def keyboard_shortcut(message: Message, state: FSMContext):
    if not _private(message):
        return
    if message.text == BTN_HELP:
        return await help_command(message, state)
    if message.text == BTN_EVENTS:
        await state.clear()
        return await events_info(message, state)
    if message.text in (BTN_EARN, BTN_AIRDROP_OLD):
        return await earn_menu(message, state)
    if message.text == BTN_TOKENS:
        return await token_menu(message, state)
    action = {
        BTN_REMOVE: remove_start, BTN_VIEW: view_command, BTN_REFRESH: home, BTN_TRADE: trade_start,
        BTN_HOME: home, BTN_SEND: send_start, BTN_DEPOSIT: wallets, BTN_HISTORY: history_open,
        BTN_BRIDGE: bridge_start, BTN_TB: tb_menu, BTN_ADD: add_start, BTN_KEYS: keys_menu, BTN_KEYS_OLD: keys_menu,
    }
    await action[message.text](message, state)


KEYS_TEXT = (
    "👛 <b>Wallets</b>\n\n"
    "📤 <b>Export</b> shows a wallet's private key so you can use it in another app "
    "(Phantom, MetaMask, a Neo wallet).\n"
    "📥 <b>Import</b> adds a wallet you already have and starts using it. Your other wallet is <b>kept</b>: use <b>Switch</b> to go back to it.\n"
    "🔄 <b>Switch</b> chooses which of your wallets the bot uses.\n"
    "🗑 <b>Delete</b> removes a wallet you no longer want (you must confirm twice).\n\n"
    "⚠️ Anyone with a key controls the funds. Never share it."
)


def _keys_markup() -> InlineKeyboardMarkup:
    # these labels are deliberately NOT the home-page menu labels: in photo mode those get hidden from bot messages
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📤 Export a wallet", callback_data="export"),
         InlineKeyboardButton(text="📥 Import a wallet", callback_data="import")],
        [InlineKeyboardButton(text="🔄 Switch wallet", callback_data="swm"),
         InlineKeyboardButton(text="🗑 Delete a wallet", callback_data="delm")],
        BACK,
    ])


async def keys_menu(message: Message, state: FSMContext):
    await state.clear()
    if not _private(message):
        return await message.answer(PRIVATE_ONLY)
    await message.answer(KEYS_TEXT, reply_markup=_keys_markup())


@router.callback_query(F.data == "keys")
async def keys_back(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    if not _private(cb):
        return await cb.answer(PRIVATE_ONLY, show_alert=True)
    await _done(cb)
    await _show(cb, KEYS_TEXT, _keys_markup())


@router.callback_query(F.data == "wallets")
async def wallets(event: Message | CallbackQuery, state: FSMContext):
    await state.clear()
    if not _private(event):
        return await _done(event, PRIVATE_ONLY, alert=True)
    session, user = await _load(event)
    async with session:
        rows = await service.ensure_wallets(session, user, SETTINGS.master_key)
        await session.commit()
    evm_names = " · ".join(html.escape(o.name) for o in OPS.values() if o.key_type == "evm")
    lines = ["📥 <b>Deposit addresses</b>", "Tap an address to copy it.", ""]
    for wl in sorted(rows, key=lambda x: x.key_type != "neo_n3"):
        if wl.key_type == "neo_n3":
            lines += ["🟢 <b>Neo N3</b>", f"<code>{wl.address}</code>", ""]
        elif wl.key_type == "solana":
            if "solana" in OPS:
                lines += ["🟪 <b>Solana</b>", f"<code>{wl.address}</code>", ""]
        else:
            lines += ["🔷 <b>EVM address</b>", f"<code>{wl.address}</code>", f"<i>The same address works on: {evm_names}</i>", ""]
    lines.append("⚠️ Send each asset to the address of its own network. A token sent to the wrong network can be lost.")
    await _show(event, "\n".join(lines), home_only())
    await _done(event)


# ---------------------------------------------------------------------- export
TYPE_ORDER = {"neo_n3": 0, "evm": 1, "solana": 2}
KEYS_BACK = [InlineKeyboardButton(text="⬅️ Back", callback_data="keys")]


async def _wallet_entries(session, user) -> list[dict]:
    """Every wallet this person has: the ones in use first, then the ones put aside."""
    active = await service.ensure_wallets(session, user, SETTINGS.master_key)
    saved = await service.saved_wallets(session, user)
    out = [{"type": wl.key_type, "address": wl.address, "ref": "a", "active": True} for wl in active]
    out += [{"type": sv.key_type, "address": sv.address, "ref": str(sv.id), "active": False} for sv in saved]
    out.sort(key=lambda e: (TYPE_ORDER.get(e["type"], 9), not e["active"], e["ref"]))
    return out


def _entry_label(e: dict) -> str:
    return f"{KEY_TYPE_LABEL[e['type']].split(' (')[0]} · {_short(e['address'])}" + (" ✅ in use" if e["active"] else "")


def _entry_button(e: dict, action: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=_entry_label(e), callback_data=f"{action}:{e['type']}:{e['ref']}:{e['address'][:6]}")


@router.callback_query(F.data == "export")
async def export_pick(cb: CallbackQuery):
    if not _private(cb):
        return await cb.answer(PRIVATE_ONLY, show_alert=True)
    session, user = await _load(cb)
    async with session:
        entries = await _wallet_entries(session, user)
    await cb.message.edit_text(
        "Which wallet do you want to export?\n\n⚠️ Anyone who sees the key controls the funds.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_entry_button(e, "ex")] for e in entries] + [KEYS_BACK]),
    )
    await cb.answer()


async def _send_private_key(message, key_type: str, secret: str):
    """Shows an exported key so it can be copied but not screenshotted, forwarded or saved:
    the message is protected and the key sits behind a spoiler, with a Copy button that puts it on the clipboard."""
    label = html.escape(KEY_TYPE_LABEL[key_type])
    note = f"This message deletes itself in {EXPORT_DELETE_AFTER} seconds. Tap 📋 Copy key, then keep the key safe."
    kb = None
    if len(secret) <= 256:
        from aiogram.types import CopyTextButton
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="📋 Copy key", copy_text=CopyTextButton(text=secret))]])
    try:
        return await message.answer(
            f"<b>{label} private key</b>\n<tg-spoiler>{html.escape(secret)}</tg-spoiler>\n\n{note}",
            protect_content=True, reply_markup=kb)
    except Exception:
        log.warning("Protected key message failed, sending the plain copyable one")
        return await message.answer(
            f"<b>{label} private key</b>\n<code>{html.escape(secret)}</code>\n\n"
            f"This message deletes itself in {EXPORT_DELETE_AFTER} seconds. Tap the key to copy it, then keep it safe.")


@router.callback_query(F.data.regexp(r"^ex:\w+:(a|\d+):\S+$"))
async def export_wallet_go(cb: CallbackQuery):
    if not _private(cb):
        return await cb.answer(PRIVATE_ONLY, show_alert=True)
    _tag, key_type, ref, address6 = cb.data.split(":", 3)
    session, user = await _load(cb)
    async with session:
        try:
            if ref == "a":
                current = await service.get_wallet(session, user, key_type)
                if current is None or not current.address.startswith(address6):
                    return await cb.answer("That wallet changed. Open Export again.", show_alert=True)
                secret = await service.export_wallet(session, user, SETTINGS.master_key, key_type)
            else:
                secret = await service.export_saved_wallet(session, user, SETTINGS.master_key, key_type, int(ref))
        except ValueError:
            return await cb.answer("That wallet is not available any more.", show_alert=True)
    sent = await _send_private_key(cb.message, key_type, secret)
    await cb.answer("Key sent")
    await asyncio.sleep(EXPORT_DELETE_AFTER)
    try:
        await sent.delete()
    except Exception:
        pass


@router.callback_query(F.data.startswith("exportgo:"))
async def export_go(cb: CallbackQuery):
    """Old buttons from before wallets could be switched: exports the wallet in use."""
    if not _private(cb):
        return await cb.answer(PRIVATE_ONLY, show_alert=True)
    key_type = cb.data.split(":", 1)[1]
    session, user = await _load(cb)
    async with session:
        secret = await service.export_wallet(session, user, SETTINGS.master_key, key_type)
    sent = await _send_private_key(cb.message, key_type, secret)
    await cb.answer("Key sent")
    await asyncio.sleep(EXPORT_DELETE_AFTER)
    try:
        await sent.delete()
    except Exception:
        pass


# -------------------------------------------------------------- switch and delete wallets
def _forget_card(tg_id: int) -> None:
    for key in [k for k in LAST_CARD if k[0] == tg_id]:
        LAST_CARD.pop(key, None)


@router.callback_query(F.data == "swm")
async def switch_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    if not _private(cb):
        return await cb.answer(PRIVATE_ONLY, show_alert=True)
    await _done(cb)
    session, user = await _load(cb)
    async with session:
        entries = await _wallet_entries(session, user)
    rows = [[_entry_button(e, "sw")] for e in entries if not e["active"]]
    if not rows:
        return await _show(cb, "🔄 <b>Switch wallet</b>\n\nYou only have one wallet of each type. "
                               "Import another one with 📥 Import a wallet, then you can switch between them.",
                           InlineKeyboardMarkup(inline_keyboard=[KEYS_BACK]))
    lines = ["🔄 <b>Switch wallet</b>", "", "The wallets in use now:"]
    lines += [f"• {html.escape(_entry_label(e))}" for e in entries if e["active"]]
    lines += ["", "Tap a wallet below to start using it. The one in use is kept, so you can switch back any time.",
              "Your balances and deposits then follow the wallet you chose."]
    await _show(cb, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows + [KEYS_BACK]))


@router.callback_query(F.data.regexp(r"^sw:\w+:\d+:\S+$"))
async def switch_go(cb: CallbackQuery, state: FSMContext):
    if not _private(cb):
        return await cb.answer(PRIVATE_ONLY, show_alert=True)
    _tag, key_type, ref, _a6 = cb.data.split(":", 3)
    session, user = await _load(cb)
    async with session:
        try:
            wallet = await service.switch_wallet(session, user, key_type, int(ref))
            await session.commit()
        except ValueError:
            return await cb.answer("That wallet is not available any more.", show_alert=True)
    _forget_card(cb.from_user.id)
    await cb.answer("Switched")
    await _show(cb, f"✅ Now using this {html.escape(KEY_TYPE_LABEL[key_type])} wallet:\n<code>{wallet.address}</code>\n\n"
                    "The wallet you used before is kept. Switch back any time.",
                InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔄 Switch again", callback_data="swm")], BACK]))


@router.callback_query(F.data == "delm")
async def delete_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    if not _private(cb):
        return await cb.answer(PRIVATE_ONLY, show_alert=True)
    await _done(cb)
    session, user = await _load(cb)
    async with session:
        entries = await _wallet_entries(session, user)
    await _show(cb, "🗑 <b>Delete a wallet</b>\n\nWhich wallet do you want to delete?\n\n"
                    "⚠️ Deleting removes its key from this bot for good. Export it first if it holds anything.",
                InlineKeyboardMarkup(inline_keyboard=[[_entry_button(e, "dl1")] for e in entries] + [KEYS_BACK]))


async def _delete_target(session, user, key_type: str, ref: str, address6: str):
    """The wallet being deleted if it is still the one that was picked, else None."""
    if ref == "a":
        current = await service.get_wallet(session, user, key_type)
        return current if current is not None and current.address.startswith(address6) else None
    saved = next((s for s in await service.saved_wallets(session, user) if s.id == int(ref) and s.key_type == key_type), None)
    return saved


@router.callback_query(F.data.regexp(r"^dl[12]:\w+:(a|\d+):\S+$"))
async def delete_ask(cb: CallbackQuery, state: FSMContext):
    if not _private(cb):
        return await cb.answer(PRIVATE_ONLY, show_alert=True)
    step, key_type, ref, address6 = cb.data.split(":", 3)
    session, user = await _load(cb)
    async with session:
        target = await _delete_target(session, user, key_type, ref, address6)
        only_one = ref == "a" and not [s for s in await service.saved_wallets(session, user) if s.key_type == key_type]
    if target is None:
        return await cb.answer("That wallet changed. Open Delete again.", show_alert=True)
    await _done(cb)
    label = html.escape(KEY_TYPE_LABEL[key_type])
    extra = ("\n\nThis is your only " + label + " wallet, so a <b>new empty wallet</b> will be created for you."
             if only_one else "\n\nYour other " + label + " wallet will be used instead." if ref == "a" else "")
    if key_type == "neo_n3" and ref == "a":
        extra += "\nYour vote wallets are not touched."
    if step == "dl1":
        return await _show(cb, f"🗑 <b>Delete this {label} wallet?</b>\n<code>{target.address}</code>{extra}\n\n"
                               "⚠️ The bot will forget its key. Anything still in it can no longer be reached unless you exported the key.",
                           InlineKeyboardMarkup(inline_keyboard=[
                               [InlineKeyboardButton(text="📤 Export it first", callback_data=f"ex:{key_type}:{ref}:{address6}")],
                               [InlineKeyboardButton(text="🗑 Yes, continue", callback_data=f"dl2:{key_type}:{ref}:{address6}")],
                               [InlineKeyboardButton(text="❌ Cancel", callback_data="keys")]]))
    await _show(cb, f"🛑 <b>Last chance.</b>\nDelete this {label} wallet for good?\n<code>{target.address}</code>{extra}\n\n"
                    "This cannot be undone.",
                InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🗑 Delete it for good", callback_data=f"dl3:{key_type}:{ref}:{address6}")],
                    [InlineKeyboardButton(text="❌ No, keep it", callback_data="keys")]]))


@router.callback_query(F.data.regexp(r"^dl3:\w+:(a|\d+):\S+$"))
async def delete_go(cb: CallbackQuery, state: FSMContext):
    if not _private(cb):
        return await cb.answer(PRIVATE_ONLY, show_alert=True)
    _tag, key_type, ref, address6 = cb.data.split(":", 3)
    session, user = await _load(cb)
    async with session:
        target = await _delete_target(session, user, key_type, ref, address6)
        if target is None:
            return await cb.answer("That wallet changed. Open Delete again.", show_alert=True)
        try:
            now_in_use = await service.delete_wallet(session, user, SETTINGS.master_key, key_type, None if ref == "a" else int(ref))
            await session.commit()
        except ValueError:
            return await cb.answer("That wallet is not available any more.", show_alert=True)
    _forget_card(cb.from_user.id)
    await cb.answer("Deleted")
    text = "✅ Wallet deleted."
    if now_in_use is not None:
        text += f"\nYour {html.escape(KEY_TYPE_LABEL[key_type])} wallet in use now:\n<code>{now_in_use.address}</code>"
    await _show(cb, text, InlineKeyboardMarkup(inline_keyboard=[KEYS_BACK, BACK]))


# ---------------------------------------------------------------------- import
@router.callback_query(F.data == "import")
async def import_pick(cb: CallbackQuery):
    if not _private(cb):
        return await cb.answer(PRIVATE_ONLY, show_alert=True)
    await cb.message.edit_text(
        "Which wallet do you want to import?\n\n"
        "Your current wallet of that type is <b>kept</b>. The imported one becomes the one in use, "
        "and you can switch back any time with 🔄 Switch wallet.",
        reply_markup=pick_type_keyboard("importgo"),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("importgo:"))
async def import_go(cb: CallbackQuery, state: FSMContext):
    if not _private(cb):
        return await cb.answer(PRIVATE_ONLY, show_alert=True)
    key_type = cb.data.split(":", 1)[1]
    await state.set_state(ImportFlow.waiting_key)
    await state.update_data(key_type=key_type)
    hint = {
        "evm": "a 64-character hex key",
        "solana": "the base58 private key that Phantom or Solflare shows",
    }.get(key_type, "a WIF key or 64-character hex key")
    await cb.message.edit_text(
        f"Send me your <b>{html.escape(KEY_TYPE_LABEL[key_type])}</b> private key ({hint}).\n"
        "I will delete your message right away. Send /cancel to stop."
    )
    await cb.answer()


@router.message(ImportFlow.waiting_key, F.text)
async def import_receive(message: Message, state: FSMContext):
    if not _private(message):
        return
    data = await state.get_data()
    key_type = data["key_type"]
    try:
        await message.delete()  # remove the secret from the chat immediately
    except Exception:
        pass
    session, user = await _load(message)
    async with session:
        try:
            wallet = await service.import_wallet(session, user, SETTINGS.master_key, key_type, message.text)
            await session.commit()
        except ValueError as exc:
            await message.answer(f"❌ {html.escape(str(exc))}\nTry again or send /cancel.")
            return
    await state.clear()
    _forget_card(message.from_user.id)
    await message.answer(
        f"✅ Imported. Your {html.escape(KEY_TYPE_LABEL[key_type])} address in use is now:\n<code>{wallet.address}</code>\n\n"
        "Your previous wallet is kept. Use 🔄 Switch wallet to go back to it.",
        reply_markup=home_keyboard(),
    )


# ---------------------------------------------------------------- remove token
@router.callback_query(F.data == "rmtok")
async def remove_start(event: Message | CallbackQuery, state: FSMContext):
    await state.clear()
    if not _private(event):
        return await _done(event, PRIVATE_ONLY, alert=True)
    session, user = await _load(event)
    async with session:
        rows = await service.removable_tokens(session, user)
    if not rows:
        await _show(
            event,
            "🗑 <b>Remove token</b>\n\nYou haven't added any tokens yet. "
            "The built-in ones (NEO, GAS, NRP, FRANK and each network's own coin) can't be removed.",
            home_only(),
        )
        return await _done(event)
    buttons = [
        [InlineKeyboardButton(text=f"{t.symbol[:20]} · {c.name}"[:60], callback_data=f"rmtokask:{t.id}")]
        for t, c in rows[:40]
    ]
    await _show(
        event,
        "🗑 <b>Remove token</b>\n\nPick a token to take off your list. This only hides it: "
        "your balance stays in your wallet and you can add it again any time.",
        InlineKeyboardMarkup(inline_keyboard=buttons + [BACK]),
    )
    await _done(event)


@router.callback_query(F.data.regexp(r"^rmtokask:\d+$"))
async def remove_ask(cb: CallbackQuery):
    if not _private(cb):
        return await cb.answer(PRIVATE_ONLY, show_alert=True)
    token_id = int(cb.data.split(":", 1)[1])
    session, user = await _load(cb)
    async with session:
        rows = await service.removable_tokens(session, user)
    found = next(((t, c) for t, c in rows if t.id == token_id), None)
    if found is None:
        return await cb.answer("That token is not on your list.", show_alert=True)
    token, chain = found
    await cb.message.edit_text(
        f"Remove <b>{html.escape(token.symbol)}</b> ({html.escape(chain.name)}) from your list?\n"
        f"<code>{html.escape(token.contract)}</code>\n\n"
        "Your balance is not touched. It just stops showing here, and deposits of it stop being announced "
        "until you add it again.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Yes, remove", callback_data=f"rmtokyes:{token_id}"),
             InlineKeyboardButton(text="Cancel", callback_data="rmtok")],
        ]),
    )
    await cb.answer()


@router.callback_query(F.data.regexp(r"^rmtokyes:\d+$"))
async def remove_do(cb: CallbackQuery):
    if not _private(cb):
        return await cb.answer(PRIVATE_ONLY, show_alert=True)
    session, user = await _load(cb)
    async with session:
        token = await service.remove_token(session, user, int(cb.data.split(":", 1)[1]))
    if token is None:
        return await cb.answer("Nothing to remove.", show_alert=True)
    await cb.message.edit_text(
        f"✅ <b>{html.escape(token.symbol)}</b> removed from your list.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Remove another", callback_data="rmtok")], BACK,
        ]),
    )
    await cb.answer("Removed")


# ------------------------------------------------------------------- add token
@router.callback_query(F.data == "addtok")
async def add_start(event: Message | CallbackQuery, state: FSMContext):
    await state.clear()
    if not _private(event):
        return await _done(event, PRIVATE_ONLY, alert=True)
    names = [InlineKeyboardButton(text=ops.name, callback_data=f"addnet:{slug}") for slug, ops in OPS.items()]
    buttons = [names[i : i + 2] for i in range(0, len(names), 2)]
    await _show(event, "Which network is the token on?", InlineKeyboardMarkup(inline_keyboard=buttons + [BACK]))
    await _done(event)


@router.callback_query(F.data.startswith("addnet:"))
async def add_network(cb: CallbackQuery, state: FSMContext):
    if not _private(cb):
        return await cb.answer(PRIVATE_ONLY, show_alert=True)
    slug = cb.data.split(":", 1)[1]
    if slug not in OPS:
        return await cb.answer("That network is not available.", show_alert=True)
    await state.set_state(AddTokenFlow.contract)
    await state.update_data(slug=slug)
    await cb.message.edit_text(
        f"Send the token's <b>contract address</b> on {html.escape(OPS[slug].name)}.\n"
        "Send /cancel to stop."
    )
    await cb.answer()


async def _scam_check(ops, contract: str) -> tuple[str | None, list[str]]:
    """(reason to refuse, warnings) for a token about to be added. Only EVM networks can be checked."""
    chain_id = getattr(getattr(ops, "cfg", None), "chain_id", None)
    if not isinstance(ops, chains.EvmOps) or not chain_id or SETTINGS.network != "mainnet":
        return None, []
    return await tokensafety.check(int(chain_id), contract)


def _scam_refusal(symbol: str, reason: str) -> str:
    return (f"🚫 <b>I did not add {html.escape(symbol)}.</b>\nThis looks like a scam: {html.escape(reason)}.\n"
            "Do not buy it, approve it or swap it.")


async def _offer_token(event, state: FSMContext, slug: str, raw_contract: str) -> bool:
    """Read the token from the chain and show the "Token found" confirmation. False if it could not be read."""
    target = event.message if isinstance(event, CallbackQuery) else event
    ops = OPS[slug]
    try:
        contract = ops.normalize_contract(raw_contract)
        info = await ops.token_info(contract)  # read from the chain, not from the user
    except neo_send.SendError as exc:
        await target.answer(f"❌ {html.escape(str(exc))}\nTry again or send /cancel.")
        return False
    except Exception:
        log.exception("Token lookup failed")
        await target.answer("❌ I couldn't reach the network to read that token. Try again in a moment.")
        return False
    refuse, safety_warnings = await _scam_check(ops, contract)
    if refuse:
        await state.clear()
        await target.answer(_scam_refusal(info.symbol, refuse), reply_markup=home_only())
        return True
    session, user = await _load(event)
    async with session:
        chain = await service.get_chain(session, ops.slug)
        existing = await service.find_token(session, chain.id, contract)
        mine = await service.visible_tokens(session, user, chain.id)
        if existing is not None and any(t.id == existing.id for t in mine):
            await state.clear()
            await target.answer(
                f"{html.escape(info.symbol)} is already in your list on {html.escape(ops.name)}.",
                reply_markup=home_only(),
            )
            return True
        lookalikes = await service.look_alike_tokens(session, chain.id, info.symbol, contract)
    warning = (
        f"\n\n🚨 <b>Another token named {html.escape(info.symbol)} already exists on this network "
        "with a different contract.</b> One of them may be a fake. Check the contract address "
        "against the project's official source."
        if lookalikes
        else ""
    )
    if safety_warnings:
        warning += "\n\n⚠️ " + html.escape(" ".join(safety_warnings))
    nonce = secrets.token_hex(8)
    await state.update_data(
        slug=slug, contract=contract, symbol=info.symbol, name=info.name, decimals=info.decimals, nonce=nonce
    )
    await target.answer(
        "<b>Token found</b>\n"
        f"Symbol: <b>{html.escape(info.symbol)}</b>\n"
        f"Name: {html.escape(info.name)}\n"
        f"Decimals: {info.decimals}\n"
        f"Network: {html.escape(ops.name)}\n"
        f"Contract: <code>{html.escape(contract)}</code>\n\n"
        "Anyone can create a token with any name. Only add tokens you trust, and compare the "
        f"contract address with the project's official one.{warning}",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✅ Add this token", callback_data=f"addok:{nonce}")],
                [InlineKeyboardButton(text="❌ Cancel", callback_data="home")],
            ]
        ),
    )
    return True


@router.message(AddTokenFlow.contract, F.text)
async def add_contract(message: Message, state: FSMContext):
    if not _private(message):
        return
    data = await state.get_data()
    await _offer_token(message, state, data["slug"], message.text)


# Tokens named in an "unlisted token" notice: short id -> (telegram id, network slug, contract).
# In memory on purpose: after a restart the old button says to use ➕ Add token instead.
UNLISTED_OFFERS: dict[str, tuple[int, str, str]] = {}
_MAX_OFFERS = 5000


async def send_unlisted_offer(bot_obj, telegram_id: int, slug: str | None, network: str, asset: str,
                              intro: str | None = None, detail: dict | None = None):
    """The "you received a token that isn't on your list" message, with Yes / No buttons when the network is known."""
    markup = None
    spam = tokensafety.sent_by_itself((detail or {}).get("sender"), asset)  # sent by its own contract: a spam airdrop
    if slug is not None and not spam:  # no "Yes, add it" button for a spam token
        if len(UNLISTED_OFFERS) >= _MAX_OFFERS:
            UNLISTED_OFFERS.clear()
        offer_id = secrets.token_hex(6)
        UNLISTED_OFFERS[offer_id] = (telegram_id, slug, asset)
        markup = InlineKeyboardMarkup(
            inline_keyboard=[[
                InlineKeyboardButton(text="✅ Yes, add it", callback_data=f"unl:{offer_id}"),
                InlineKeyboardButton(text="❌ No", callback_data="unlno"),
            ]]
        )
    detail = detail or {}
    if intro is None:
        if detail.get("amount") and detail.get("symbol"):
            intro = f"📥 <b>You received {html.escape(detail['amount'])} {html.escape(detail['symbol'])}</b> ({html.escape(network)})"
        else:
            intro = f"📥 <b>You received a token</b> ({html.escape(network)})"
        intro += ", but it isn't on your list" + ("." if spam else " yet.")
    lines = [intro]
    if detail.get("sender"):
        lines.append(f"From: <code>{html.escape(str(detail['sender']))}</code>")
    lines.append(f"Token: <code>{html.escape(asset)}</code>")
    if detail.get("tx"):
        lines.append(historyview.tx_html(SETTINGS.network, slug, str(detail["tx"])))
    if spam:
        closing = ("\n\n🚫 <b>This is a spam token. Don't add it.</b>\n"
                   "It was sent by its own contract, which is how scam airdrops work. Never approve it, swap it or open "
                   "links from it. If you ever need to add it anyway, use ➕ Add token with the contract address.")
    else:
        closing = ("\n\n<b>Do you want to add this token to your list?</b>\n"
                   "Only say yes if you expected it. Anyone can create a fake token with any name.")
    await bot_obj.send_message(telegram_id, "\n".join(lines) + closing, reply_markup=markup)


async def announce_in_bot_transfer(
    bot_obj, ops, token, sender_id: int, sender_name: str | None, sender_address: str,
    recipient_id: int | None, raw: int, tx_hash: str,
) -> str:
    """Tell the receiver at once when another user of this bot sends them a token on an EVM network.

    This does not wait for the chain scan (which needs a good RPC endpoint). The scan finds the same
    transfer later and skips it, because the receiver's history row is written here.
    Returns "sent", "unreachable" (Telegram will not let the bot message them: they never pressed Start
    in a private chat with the bot, or blocked it) or "skipped" (nothing to announce).
    """
    if not isinstance(ops, chains.EvmOps) or token.contract == evm_send.NATIVE or not recipient_id or not tx_hash:
        return "skipped"
    try:
        async with SESSIONS() as s:
            recipient = await s.get(User, recipient_id)
            if recipient is None:
                return "skipped"
            chain = await service.get_chain(s, ops.slug)
            allowed = await service.allowed_token_ids(s, recipient, chain.id)
            row = await service.record_incoming_from_user(s, recipient, token, sender_id, raw, tx_hash, sender_address)
            chain_name, telegram_id, recipient_name = chain.name, recipient.telegram_id, recipient.username
        if row is None:
            log.info("Transfer %s to user %s was already recorded; no second notice", tx_hash, recipient_id)
            return "skipped"
        amount = service.format_amount(raw, token.decimals)
        who = f"@{html.escape(sender_name)}" if sender_name else f"<code>{html.escape(sender_address)}</code>"
        sym = html.escape(token.symbol)
        if token.id in allowed:
            await bot_obj.send_message(
                telegram_id,
                f"📥 <b>Transfer received</b>\n{amount} {sym} ({html.escape(chain_name)})\nFrom: {who}\n"
                f"{historyview.tx_html(SETTINGS.network, ops.slug, tx_hash)}",
            )
        else:
            await send_unlisted_offer(
                bot_obj, telegram_id, ops.slug, chain_name, token.contract,
                intro=f"👀 <b>{who} sent you {amount} {sym}</b> ({html.escape(chain_name)}), but it isn't on your list yet.",
            )
        log.info("Told user %s (@%s) about the %s %s they received", recipient_id, recipient_name, amount, token.symbol)
        return "sent"
    except TelegramForbiddenError:
        log.warning(
            "Cannot message user %s: they have not started a private chat with the bot, or they blocked it. "
            "They must open the bot and press Start.", recipient_id,
        )
        return "unreachable"
    except Exception:
        log.exception("Could not tell user %s about the transfer they received", recipient_id)
        return "skipped"


UNREACHABLE_NOTE = (
    "ℹ️ I can't message {who}: they haven't opened a private chat with the bot (or blocked it). "
    "Ask them to open the bot and press Start. The tokens are already in their wallet."
)


@router.callback_query(F.data == "unlno")
async def unlisted_no(cb: CallbackQuery):
    await cb.answer()
    try:
        await cb.message.edit_text("Okay, I left it off your list. You can still add it later with ➕ Add token.")
    except TelegramBadRequest:
        pass


@router.callback_query(F.data.startswith("unl:"))
async def unlisted_add(cb: CallbackQuery, state: FSMContext):
    """One tap: read the token from the chain and add it. Only a look-alike name asks for a second look."""
    if not _private(cb):
        return await cb.answer(PRIVATE_ONLY, show_alert=True)
    offer = UNLISTED_OFFERS.get(cb.data.split(":", 1)[1])
    if offer is None or offer[0] != cb.from_user.id:
        return await cb.answer("This button has expired. Tap ➕ Add token and paste the address.", show_alert=True)
    await cb.answer()
    await state.clear()
    slug, raw_contract = offer[1], offer[2]
    ops = OPS.get(slug)
    if ops is None:
        return await cb.message.answer("That network is not available right now.")
    try:
        contract = ops.normalize_contract(raw_contract)
        info = await ops.token_info(contract)  # read from the chain
    except Exception:
        log.exception("Token lookup failed")
        return await cb.message.answer(
            "❌ I couldn't read that token. If it keeps failing, it is probably a fake token, so please don't add it. "
            "(If you are sure it is real, try ➕ Add token in a moment.)")
    refuse, _w = await _scam_check(ops, contract)
    if refuse:
        return await cb.message.answer(_scam_refusal(info.symbol, refuse), reply_markup=home_only())
    session, user = await _load(cb)
    async with session:
        chain = await service.get_chain(session, ops.slug)
        lookalikes = await service.look_alike_tokens(session, chain.id, info.symbol, contract)
        if lookalikes:  # another token with this name exists: show the warning screen and ask again
            pass
        else:
            token, _new = await service.add_token_for_user(
                session, user, chain.id, contract, info.symbol, info.name, info.decimals
            )
    if lookalikes:
        await _offer_token(cb, state, slug, contract)
        return
    try:
        await cb.message.edit_text(
            f"✅ <b>{html.escape(token.symbol)}</b> is on your list now ({html.escape(ops.name)}). "
            "Your balance shows on the home page.",
            reply_markup=home_only(),
        )
    except TelegramBadRequest:
        await cb.message.answer(f"✅ {html.escape(token.symbol)} added.", reply_markup=home_only())


@router.callback_query(F.data.startswith("addok:"))
async def add_confirm(cb: CallbackQuery, state: FSMContext):
    if not _private(cb):
        return await cb.answer(PRIVATE_ONLY, show_alert=True)
    data = await state.get_data()
    if not data or data.get("nonce") != cb.data.split(":", 1)[1]:
        return await cb.answer("This confirmation has expired.", show_alert=True)
    await state.clear()
    ops = OPS[data["slug"]]
    session, user = await _load(cb)
    async with session:
        chain = await service.get_chain(session, ops.slug)
        token, _new = await service.add_token_for_user(
            session, user, chain.id, data["contract"], data["symbol"], data["name"], data["decimals"]
        )
        wallet = await service.get_wallet(session, user, ops.key_type)
    await cb.message.edit_text(
        f"✅ <b>{html.escape(token.symbol)}</b> added on {html.escape(ops.name)}.\n\n"
        f"To receive it, deposit to:\n<code>{wallet.address}</code>\n\n"
        f"To send it you also need a little {html.escape(ops.native_symbol)} in this wallet "
        "to pay network fees.",
        reply_markup=home_keyboard(),
    )
    await cb.answer()


# ----------------------------------------------------------------------- sending
async def _balance_tokens(session, chain, token) -> list[Token]:
    """The tokens whose balances a send needs: the token itself and the native coin (for fees)."""
    out = [token]
    native = await service.native_token(session, chain.id)
    if native is not None and native.id != token.id:
        out.append(native)
    return out


_TP_TOKEN_CB = {"send": "sendtok:{id}", "rain": "prtok:rain:{id}", "quiz": "prtok:quiz:{id}", "air": "aitok:{id}", "bal": "aibt:{id}"}


def _tp_title(ctx: str) -> str:
    return {"send": "💸 <b>Send</b>", "rain": "🌧 <b>Prepare a rain</b>", "quiz": "🧠 <b>Prepare a quiz</b>",
            "air": "🪂 <b>Airdrop reward</b>", "bal": "💰 <b>Holding requirement</b>"}[ctx.split(":", 1)[0]]


def _tp_back(ctx: str) -> list:
    kind, _, extra = ctx.partition(":")
    if kind == "send":
        return BACK
    if kind in ("rain", "quiz"):
        return EVENTS_BACK
    if kind == "air":
        return AIR_BACK
    return [InlineKeyboardButton(text="⬅️ Back", callback_data=f"aim:{extra}")]


def _tp_pairs(buttons: list) -> list:
    return [buttons[i:i + 2] for i in range(0, len(buttons), 2)]


async def _tp_groups(user, ctx: str) -> dict:
    """{network slug: [(token, amount)]}: what the person can pick, funded tokens only (any token for the holding requirement)."""
    views = await _gather_views(user)
    groups: dict = {}
    for slug in OPS:
        for token, amount, _price in views.get(slug) or []:
            if amount or ctx.startswith("bal"):
                groups.setdefault(slug, []).append((token, amount))
    return groups


async def _tp_networks(event, user, ctx: str) -> None:
    """Step 1 of every token picker (Send, rain, quiz, airdrop): which network. One network goes straight to its tokens."""
    groups = await _tp_groups(user, ctx)
    back = _tp_back(ctx)
    if not groups:
        text = ("Nothing to send yet. Your balances may be unavailable, or you hold nothing." if ctx == "send"
                else "You have nothing to give yet. Your balances may be unavailable, or your wallet is empty.")
        return await _show(event, f"{_tp_title(ctx)}\n\n{text}" if ctx != "send" else text, InlineKeyboardMarkup(inline_keyboard=[back]))
    if len(groups) == 1:
        return await _tp_tokens(event, ctx, next(iter(groups)), groups, single=True)
    buttons = [InlineKeyboardButton(text=f"{_xb_emoji(slug)} {OPS[slug].name} ({len(items)})", callback_data=f"tpn:{ctx}:{slug}")
               for slug, items in groups.items()]
    await _show(event, f"{_tp_title(ctx)}\n\n<b>Step 1.</b> Which network is the token on?",
                InlineKeyboardMarkup(inline_keyboard=_tp_pairs(buttons) + [back]))


async def _tp_tokens(event, ctx: str, slug: str, groups: dict, single: bool = False) -> None:
    """Step 2: the tokens on the chosen network."""
    items = groups.get(slug) or []
    if not items:
        return await _done(event, "You have nothing on this network.", alert=True) if isinstance(event, CallbackQuery) else None
    template = _TP_TOKEN_CB[ctx.split(":", 1)[0]]
    buttons = []
    for token, amount in items:
        label = token.symbol[:10]
        if amount:
            label += " · " + service.format_amount(amount, token.decimals)[:9]
        buttons.append(InlineKeyboardButton(text=label, callback_data=template.format(id=token.id)))
    back = _tp_back(ctx) if single else [InlineKeyboardButton(text="⬅️ Networks", callback_data=f"tpl:{ctx}"),
                                         InlineKeyboardButton(text="🏠 Home", callback_data="home")]
    head = f"{_tp_title(ctx)} · {html.escape(OPS[slug].name)}\n\n<b>Which token?</b>"
    await _show(event, head, InlineKeyboardMarkup(inline_keyboard=_tp_pairs(buttons) + [back]))


@router.callback_query(F.data.startswith("tpn:"))
async def tp_network(cb: CallbackQuery, state: FSMContext):
    if not _private(cb):
        return await _done(cb, PRIVATE_ONLY, alert=True)
    ctx, slug = cb.data[4:].rsplit(":", 1)
    if ctx.split(":", 1)[0] not in _TP_TOKEN_CB or slug not in OPS:
        return await _done(cb, "That network is not available right now.", alert=True)
    session, user = await _load(cb)
    await session.close()
    await _done(cb)
    await _tp_tokens(cb, ctx, slug, await _tp_groups(user, ctx))


@router.callback_query(F.data.startswith("tpl:"))
async def tp_networks_again(cb: CallbackQuery, state: FSMContext):
    if not _private(cb):
        return await _done(cb, PRIVATE_ONLY, alert=True)
    ctx = cb.data[4:]
    if ctx.split(":", 1)[0] not in _TP_TOKEN_CB:
        return await _done(cb)
    session, user = await _load(cb)
    await session.close()
    await _done(cb)
    groups = await _tp_groups(user, ctx)
    buttons = [InlineKeyboardButton(text=f"{_xb_emoji(slug)} {OPS[slug].name} ({len(items)})", callback_data=f"tpn:{ctx}:{slug}")
               for slug, items in groups.items()]
    await _show(cb, f"{_tp_title(ctx)}\n\n<b>Step 1.</b> Which network is the token on?",
                InlineKeyboardMarkup(inline_keyboard=_tp_pairs(buttons) + [_tp_back(ctx)]))


@router.callback_query(F.data == "send")
async def send_start(event: Message | CallbackQuery, state: FSMContext):
    await state.clear()
    if not _private(event):
        return await _done(event, PRIVATE_ONLY, alert=True)
    session, user = await _load(event)
    await session.close()
    await _done(event)
    await _tp_networks(event, user, "send")


@router.callback_query(F.data.startswith("sendtok:"))
async def send_token(cb: CallbackQuery, state: FSMContext):
    if not _private(cb):
        return await cb.answer(PRIVATE_ONLY, show_alert=True)
    token_id = int(cb.data.split(":", 1)[1])
    session, user = await _load(cb)
    async with session:
        token = await session.get(Token, token_id)
        chain = await session.get(Chain, token.chain_id)
    if chain is None or chain.slug not in OPS:
        return await cb.answer("That network is not available right now.", show_alert=True)
    await state.set_state(SendFlow.dest)
    await state.update_data(token_id=token_id, slug=chain.slug)
    await cb.message.edit_text(
        f"Send {html.escape(token.symbol)} on {html.escape(OPS[chain.slug].name)} to whom?\n\n"
        "Send an <b>@username</b> of someone who uses this bot, or a wallet address on this network.\n"
        "Send /cancel to stop."
    )
    await cb.answer()


@router.message(SendFlow.dest, F.text)
async def send_dest(message: Message, state: FSMContext):
    if not _private(message):
        return
    data = await state.get_data()
    ops = OPS[data["slug"]]
    text = message.text.strip()
    session, user = await _load(message)
    async with session:
        own = await service.get_wallet(session, user, ops.key_type)
        if text.startswith("@"):
            found = await service.find_recipient(session, text, ops.key_type)
            if found is None:
                await message.answer("I don't know that username. They need to start this bot first. Try again or /cancel.")
                return
            r_user, r_wallet = found
            if r_user.id == user.id:
                await message.answer("You can't send to yourself. Try again or /cancel.")
                return
            address, dest_user_id, label = r_wallet.address, r_user.id, f"@{r_user.username}"
        elif ops.is_valid_address(text):
            if text.lower() == own.address.lower():
                await message.answer("That is your own address. Try again or /cancel.")
                return
            owner = await service.find_user_by_address(session, text)
            address, dest_user_id = text, (owner.id if owner else None)
            label = f"@{owner.username}" if owner and owner.username else "address"
        else:
            await message.answer(f"That is not a valid @username or {html.escape(ops.name)} address. Try again or /cancel.")
            return
    await state.update_data(dest_address=address, dest_user_id=dest_user_id, dest_label=label)
    await state.set_state(SendFlow.amount)
    await message.answer("How much? Send a number (for example 12.5), or <b>max</b>.")


@router.message(SendFlow.amount, F.text)
async def send_amount(message: Message, state: FSMContext):
    if not _private(message):
        return
    data = await state.get_data()
    ops = OPS[data["slug"]]
    session, user = await _load(message)
    async with session:
        token = await session.get(Token, data["token_id"])
        chain = await service.get_chain(session, ops.slug)
        wallet = await service.get_wallet(session, user, ops.key_type)
        try:
            if not token.decimals_verified:
                raise neo_send.SendError(f"{token.symbol} is not ready yet. Try again later.")
            try:
                balances = await ops.balances(wallet.address, await _balance_tokens(session, chain, token))
            except Exception:
                raise neo_send.SendError("I can't read your balance right now. Try again in a moment.")
            if message.text.strip().lower() == "max":
                raw = await ops.max_sendable(token, wallet.address, balances)
                if raw <= 0:
                    raise neo_send.SendError("You have nothing sendable (fees come out of your balance).")
            else:
                raw = neo_send.parse_amount(message.text, token.decimals)
            used = await service.sent_last_24h(session, user, token)
            neo_send.check_limits(token.symbol, token.decimals, raw, used, SETTINGS.send_limits)
            await ops.check_send(token, wallet.address, data["dest_address"], raw, balances)
        except neo_send.SendError as exc:
            await message.answer(f"❌ {html.escape(str(exc))}\nTry another amount or /cancel.")
            return
        except Exception:
            log.exception("Amount check failed")
            await message.answer("❌ I couldn't check that with the network. Try again in a moment.")
            return
    nonce = secrets.token_hex(8)
    await state.update_data(amount_raw=raw, nonce=nonce)
    to = data["dest_address"]
    fee_line = ""
    try:
        fee_line = await _fee_line(data["slug"], (await _user_id(message)), token.id, 1)
    except Exception:
        pass
    await message.answer(
        "<b>Please confirm</b>\n"
        f"Send: <b>{service.format_amount(raw, token.decimals)} {html.escape(token.symbol)}</b>\n"
        f"To: {html.escape(data['dest_label'])} (<code>{_short(to)}</code>)\n"
        f"Network: {html.escape(ops.name)}\n"
        + (fee_line + "\n\n" if fee_line else f"A network fee in {html.escape(ops.native_symbol)} is paid from your wallet.\n\n")
        + "Transfers cannot be reversed.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✅ Confirm send", callback_data=f"sendok:{nonce}")],
                [InlineKeyboardButton(text="❌ Cancel", callback_data="home")],
            ]
        ),
    )


@router.callback_query(F.data.startswith("sendok:"))
async def send_confirm(cb: CallbackQuery, state: FSMContext):
    if not _private(cb):
        return await cb.answer(PRIVATE_ONLY, show_alert=True)
    data = await state.get_data()
    if not data or data.get("nonce") != cb.data.split(":", 1)[1]:
        return await cb.answer("This confirmation has expired.", show_alert=True)
    await state.clear()  # single use: a second tap finds nothing to confirm
    await cb.answer()
    lock = send_lock(cb.from_user.id, data["slug"])
    if lock.locked():
        return await cb.message.edit_text("Another send on this network is still in progress. Please wait.")
    ops = OPS.get(data["slug"])
    if ops is None:
        return await cb.message.edit_text("That network is not available right now.", reply_markup=home_only())
    async with lock:
        await cb.message.edit_text("⏳ Sending… this can take up to a couple of minutes.")
        session, user = await _load(cb)
        async with session:
            token = await session.get(Token, data["token_id"])
            chain = await service.get_chain(session, ops.slug)
            wallet = await service.get_wallet(session, user, ops.key_type)
            sender_id, sender_name, sender_address = user.id, user.username, wallet.address
            raw, to = data["amount_raw"], data["dest_address"]
            # Re-check everything: time has passed since the confirmation screen.
            try:
                balances = await ops.balances(wallet.address, await _balance_tokens(session, chain, token))
                used = await service.sent_last_24h(session, user, token)
                neo_send.check_limits(token.symbol, token.decimals, raw, used, SETTINGS.send_limits)
                await ops.check_send(token, wallet.address, to, raw, balances)
            except neo_send.SendError as exc:
                return await cb.message.edit_text(f"❌ {html.escape(str(exc))}", reply_markup=home_only())
            except Exception:
                log.exception("Pre-send check failed")
                return await cb.message.edit_text(
                    "❌ I couldn't verify your balance. Nothing was sent.", reply_markup=home_only()
                )
            kind = "transfer_out" if data["dest_user_id"] else "withdraw"
            row = await service.record_outgoing(
                session, user, token, kind, raw, f"to {to} on {ops.slug}", data["dest_user_id"]
            )
            private_key = await service.get_private_key(session, user, SETTINGS.master_key, ops.key_type)
            try:
                outcome = await ops.send(private_key, token, wallet.address, to, raw)
            except neo_send.SendError as exc:
                row.status = "failed"
                row.note = f"{row.note} | {str(exc)[:100]}"[:250]
                await session.commit()
                return await cb.message.edit_text(f"❌ {html.escape(str(exc))}", reply_markup=home_only())
            except Exception:
                log.exception("Send failed before broadcast")
                row.status = "failed"
                await session.commit()
                return await cb.message.edit_text(
                    "❌ Something went wrong before the transaction was sent. Nothing was sent.",
                    reply_markup=home_only(),
                )
            row.tx_hash = outcome.tx_hash
            row.status = outcome.status
            await session.commit()

    sym = html.escape(token.symbol)
    amount_text = service.format_amount(raw, token.decimals)
    label = html.escape(data["dest_label"])
    tx = historyview.tx_html(SETTINGS.network, ops.slug, outcome.tx_hash)
    if outcome.status == "confirmed":
        text = f"✅ <b>Sent</b> {amount_text} {sym} to {label} on {html.escape(ops.name)}\n{tx}"
    elif outcome.status == "failed":
        text = (
            f"❌ The transaction failed on-chain ({html.escape(outcome.detail)}). "
            f"A small {html.escape(ops.native_symbol)} fee may have been used.\n{tx}"
        )
    else:
        text = (
            "⚠️ The transaction was sent but I could not confirm it yet. "
            f"<b>Do not send again</b> until you check your balance.\n{tx}"
        )
    if outcome.status == "confirmed" and data.get("dest_user_id"):
        told = await announce_in_bot_transfer(
            cb.bot, ops, token, sender_id, sender_name, sender_address, data["dest_user_id"], raw, outcome.tx_hash
        )
        if told == "unreachable":
            text += "\n\n" + UNREACHABLE_NOTE.format(who=label)
    await cb.message.edit_text(text, reply_markup=home_only())


# ------------------------------------------------------------------------ earn
VOTE_SEND, _UNUSED = neovote.make_mamba_voter(SETTINGS.neo_n3_rpc)
TRANSFER = neo_send.make_mamba_sender(SETTINGS.neo_n3_rpc)
VOTE_NAMES = neovote.load_names()
VOTE_BACK = [InlineKeyboardButton(text="⬅️ Back", callback_data="vote")]
ROLE_TEXT = {"consensus": "✅ Elected · consensus node", "committee": "✅ Elected · council member", "candidate": "⚪ Candidate, not elected right now"}
FEE_NOTE = "about 0.02 GAS per transaction"


class VoteFlow(StatesGroup):
    amount = State()


def _vote_tier_line(candidate) -> str:
    if candidate.role == "candidate":
        return f"⚠️ rank #{candidate.rank}: not elected right now, so it earns nothing until it reaches the top 21"
    return f"✅ elected: earns GAS (rank #{candidate.rank})"


def _gas(raw: int) -> str:
    return service.format_amount(raw, 8)


def _cand_name(pubkey: str, candidates) -> str:
    c = neovote.find(candidates, pubkey)
    return neovote.label(c, VOTE_NAMES) if c is not None else neovote.short_key(pubkey)


async def _vote_state(event):
    """(main wallet address, main standing, [Slot]) read from the network. Raises when it cannot be read."""
    session, user = await _load(event)
    async with session:
        wallet = await service.get_wallet(session, user, "neo_n3")
        rows = [(r.id, r.address, r.candidate) for r in await service.get_vote_wallets(session, user)]
    main = await neovote.get_standing(NEO_RPC, wallet.address)
    slots = list(await asyncio.gather(*(neovote.read_slot(NEO_RPC, i, a, c) for i, a, c in rows)))
    return wallet.address, main, slots


# ------------------------------------------------------------------ earn page
def _earn_view(main, slots, candidates=()):
    lines = ["💎 <b>Earn</b>", "",
             f"💰 GAS earned from holding NEO: <b>{_gas(main.unclaimed)} GAS</b>"]
    if slots:
        voted = sum(s.standing.unclaimed for s in slots)
        lines.append(f"🗳 GAS earned from voting: <b>{_gas(voted)} GAS</b>")
        lines.append(f"✨ Total waiting: <b>{_gas(main.unclaimed + voted)} GAS</b>")
    lines += ["", "Tap <b>Claim your GAS</b> to collect it into your wallet.", "",
              "🗳 <b>Vote</b>: vote for a Neo candidate to earn more GAS.",
              "🪂 <b>Airdrops</b>: finish tasks and win token rewards."]
    rows = [[InlineKeyboardButton(text="💰 Claim your GAS", callback_data="vt:c")],
            [InlineKeyboardButton(text="🗳 Vote", callback_data="vote")],
            [InlineKeyboardButton(text="🪂 Airdrops", callback_data="air")],
            BACK]
    return "\n".join(lines), rows


@router.callback_query(F.data == "earn")
async def earn_menu(event: Message | CallbackQuery, state: FSMContext):
    await state.clear()
    if not _private(event):
        return await _done(event, PRIVATE_ONLY, alert=True)
    await _done(event)
    if "neo_n3" not in OPS:
        return await _show(event, "💎 <b>Earn</b>", InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🪂 Airdrops", callback_data="air")], BACK]))
    try:
        _addr, main, slots = await _vote_state(event)
        try:
            candidates = await neovote.get_candidates(NEO_RPC) if slots else []
        except Exception:
            candidates = []
        text, rows = _earn_view(main, slots, candidates)
    except Exception:
        log.exception("Could not read the GAS waiting")
        text = ("💎 <b>Earn</b>\n\nI couldn't read your GAS from the network just now, but you can still use the buttons.")
        rows = [[InlineKeyboardButton(text="💰 Claim your GAS", callback_data="vt:c")],
                [InlineKeyboardButton(text="🗳 Vote", callback_data="vote")],
                [InlineKeyboardButton(text="🪂 Airdrops", callback_data="air")], BACK]
    await _show(event, text, InlineKeyboardMarkup(inline_keyboard=rows))


# ------------------------------------------------------------------ vote pages
def _vote_intro_view(main, slots, candidates):
    """First page of /vote: how it works and how you earn. `main` may be None when the network did not answer."""
    lines = [
        "🗳 <b>Vote &amp; earn GAS</b>",
        "<i>Neo N3 governance</i>",
        "",
        "<b>How it works</b>",
        "1. NEO holders vote for candidates. Each NEO is 1 vote.",
        "2. The 21 candidates with the most votes become the <b>council</b>. The top 7 of them also create the blocks.",
        "3. You choose how much NEO to vote with, and for which candidate. You can vote for several candidates.",
        "4. Your NEO stays yours. You can take it back whenever you like.",
        "",
        "<b>How you earn</b>",
        "• Voting for any elected candidate (all 21 of the council) earns you GAS.",
        "• Each candidate's reward pool is shared among the people who voted for it, in proportion to their NEO.",
        "• If a candidate drops out of the top 21, the GAS from that vote stops until you vote for another.",
        "• Tap <b>Claim GAS</b> any time to collect what you earned.",
    ]
    if main is not None:
        lines += ["", f"Your NEO available to vote: <b>{main.neo}</b>"]
        if slots:
            lines.append("Your votes:")
            for s in slots:
                lines.append(f"• {html.escape(_cand_name(s.candidate, candidates))}: <b>{s.standing.neo} NEO</b>")
        else:
            lines.append("Your votes: <b>none yet</b>")
        if main.neo <= 0 and not slots:
            lines.append("<i>You have no NEO yet. You can look at the candidates now, and deposit NEO when you want to vote.</i>")
    rows = [[InlineKeyboardButton(text="🗳 Vote for a candidate", callback_data="vt:l:0")]]
    if slots:
        rows.append([InlineKeyboardButton(text="📋 My votes", callback_data="vt:m")])
    rows.append([InlineKeyboardButton(text="⬅️ Back", callback_data="earn")])
    return "\n".join(lines), rows


def _vote_button_text(c) -> str:
    """One line of the candidate list: rank, name, location, votes. (Buttons hold text and emoji only.)"""
    where = neovote.location(c, VOTE_NAMES)
    text = f"#{c.rank} {neovote.label(c, VOTE_NAMES)}"
    if where:
        text += f" · 📍{where}"
    return f"{text} · {neovote.votes_text(c.votes)} votes"


def _vote_list_view(candidates):
    """The 21 council candidates, one button each with everything on it."""
    council = candidates[:neovote.COMMITTEE_SIZE]
    text = ("🏛 <b>Neo council · top 21 candidates</b>\n\n"
            f"Votes cast in total: <b>{neovote.votes_text(neovote.total_votes(candidates))}</b> NEO\n"
            "All 21 earn their voters GAS. Tap one to see the details and vote.")
    rows = [[InlineKeyboardButton(text=_vote_button_text(c), callback_data=f"vt:s:{c.rank}")] for c in council]
    rows.append([InlineKeyboardButton(text="⬅️ Back", callback_data="vote")])
    return text, rows


def _vote_detail_view(candidate, candidates, gas_per_block: int, my_neo: int = 0):
    """One candidate: status, votes cast, share, key, address, reward estimate. Two buttons: vote, back."""
    name = neovote.label(candidate, VOTE_NAMES)
    where = neovote.location(candidate, VOTE_NAMES)
    try:
        address = neovote.candidate_address(candidate.pubkey)
    except Exception:
        address = ""
    percent = neovote.vote_percent(candidate, candidates)
    lines = [f"🏛 <b>#{candidate.rank} · {html.escape(name)}</b>", ROLE_TEXT[candidate.role]]
    if where:
        lines.append(f"📍 {html.escape(where)}")
    lines += ["", f"Votes cast: <b>{candidate.votes:,}</b> NEO",
              f"Share of all votes cast: <b>{percent:.2f}%</b>",
              f"Share of all NEO: <b>{candidate.votes * 100 / neovote.TOTAL_NEO:.2f}%</b>",
              "", "<b>Member information</b>", f"Public key: <code>{candidate.pubkey}</code>"]
    if address:
        lines.append(f"Address: <code>{address}</code>")
    lines.append("")
    if candidate.role == "candidate":
        lines.append("Voting for this candidate earns <b>no GAS</b> right now, because it is not in the top 21.")
    else:
        estimate = neovote.estimate_daily_gas(candidate, gas_per_block, 100)
        lines.append(f"Rough reward: about <b>{_gas(estimate)} GAS a day</b> for 100 NEO voting here "
                     "<i>(an estimate: it changes with votes and the GAS per block)</i>")
        if my_neo:
            mine = neovote.estimate_daily_gas(candidate, gas_per_block, my_neo)
            lines.append(f"For your {my_neo} NEO: about <b>{_gas(mine)} GAS a day</b>")
    rows = [[InlineKeyboardButton(text="🗳 Vote for this candidate", callback_data=f"vt:v:{candidate.rank}")],
            [InlineKeyboardButton(text="⬅️ Back", callback_data="vt:l:0")]]
    return "\n".join(lines), rows


def _amount_buttons(rank: int, balance: int) -> list:
    """Quick amounts (a quarter, half, three quarters, all) as whole NEO, without repeats."""
    seen, row = set(), []
    for pct in (25, 50, 75, 100):
        n = max(1, balance * pct // 100)
        if n in seen or n > balance:
            continue
        seen.add(n)
        row.append(InlineKeyboardButton(text=("All " if n == balance else f"{pct}% · ") + str(n), callback_data=f"vt:q:{rank}:{pct}"))
    return [row[i:i + 2] for i in range(0, len(row), 2)]


def _amount_view(candidate, balance: int, existing: int):
    lines = ["🗳 <b>Vote for this candidate</b>", "",
             f"Candidate: <b>{html.escape(neovote.label(candidate, VOTE_NAMES))}</b> (<code>{neovote.short_key(candidate.pubkey)}</code>)",
             _vote_tier_line(candidate), "",
             f"Your NEO available: <b>{balance}</b>"]
    if existing:
        lines.append(f"Already voting for this candidate: <b>{existing} NEO</b> (what you add joins it)")
    lines += ["", "<b>How much NEO do you want to vote with?</b>",
              "Tap an amount, or <b>type any whole number</b> and send it. NEO can't be split into parts."]
    rows = _amount_buttons(candidate.rank, balance)
    rows.append([InlineKeyboardButton(text="⬅️ Back", callback_data=f"vt:s:{candidate.rank}")])
    return "\n".join(lines), rows


def _confirm_vote_view(candidate, amount: int, balance: int, nonce: str):
    text = ("<b>Please confirm your vote</b>\n\n"
            f"Candidate: <b>{html.escape(neovote.label(candidate, VOTE_NAMES))}</b> (<code>{neovote.short_key(candidate.pubkey)}</code>)\n"
            f"{_vote_tier_line(candidate)}\n\n"
            f"Vote with: <b>{amount} NEO</b> of your {balance}\n\n"
            "<b>What happens</b>\n"
            "1. A small vote wallet for this candidate is used (made for you the first time).\n"
            f"2. {amount} NEO moves there from your main wallet, plus about 0.1 GAS for its fees.\n"
            "3. It votes for the candidate.\n\n"
            "• The rest of your NEO stays in your main wallet.\n"
            "• You can take the NEO back any time under 📋 My votes.\n"
            f"• A few small GAS fees are paid from your wallet ({FEE_NOTE}).")
    rows = [[InlineKeyboardButton(text=f"✅ Vote with {amount} NEO", callback_data=f"vt:ok:{nonce}")],
            [InlineKeyboardButton(text="⬅️ Back", callback_data=f"vt:v:{candidate.rank}")]]
    return text, rows


@router.callback_query(F.data == "vote")
async def vote_home(event: Message | CallbackQuery, state: FSMContext):
    await state.clear()
    if not _private(event):
        return await _done(event, PRIVATE_ONLY, alert=True)
    if "neo_n3" not in OPS:
        return await _done(event, "Neo N3 is not available on this bot.", alert=True)
    await _done(event)
    main, slots, candidates = None, [], []
    try:
        candidates = await neovote.get_candidates(NEO_RPC)
        _addr, main, slots = await _vote_state(event)
    except Exception:
        log.exception("Could not read the governance state")
    text, rows = _vote_intro_view(main, slots, candidates)
    await _show(event, text, InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.regexp(r"^vt:l:\d+$"))
async def vote_list(cb: CallbackQuery, state: FSMContext):
    if not _private(cb):
        return await _done(cb, PRIVATE_ONLY, alert=True)
    await state.clear()
    await _done(cb)
    try:
        candidates = await neovote.get_candidates(NEO_RPC)
    except Exception:
        log.exception("Could not read the candidates")
        return await _show(cb, "❌ I couldn't reach the Neo network. Try again in a moment.", home_only())
    if not candidates:
        return await _show(cb, "I couldn't find any candidates right now. Try again in a moment.", InlineKeyboardMarkup(inline_keyboard=[VOTE_BACK]))
    text, rows = _vote_list_view(candidates)
    await _show(cb, text, InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.regexp(r"^vt:s:\d+$"))
async def vote_detail(cb: CallbackQuery, state: FSMContext):
    """One candidate's page (anyone can look, no NEO needed)."""
    if not _private(cb):
        return await _done(cb, PRIVATE_ONLY, alert=True)
    await state.clear()
    await _done(cb)
    rank = int(cb.data.split(":")[2])
    try:
        candidates = await neovote.get_candidates(NEO_RPC)
        gas_per_block = await neovote.get_gas_per_block(NEO_RPC)
    except Exception:
        log.exception("Could not read the candidate")
        return await _show(cb, "❌ I couldn't reach the Neo network. Try again in a moment.", home_only())
    chosen = next((c for c in candidates if c.rank == rank), None)
    if chosen is None:
        return await _show(cb, "That candidate is not in the list any more.", InlineKeyboardMarkup(inline_keyboard=[VOTE_BACK]))
    my_neo = 0
    try:
        my_neo = (await _vote_state(cb))[1].neo
    except Exception:
        pass
    text, rows = _vote_detail_view(chosen, candidates, gas_per_block, my_neo)
    await _show(cb, text, InlineKeyboardMarkup(inline_keyboard=rows))


async def _vote_amount_page(event, state: FSMContext, rank: int):
    """The 'how much?' page for the candidate at this rank. Sets the state so a typed number is read."""
    try:
        candidates = await neovote.get_candidates(NEO_RPC, fresh=True)
        _addr, main, slots = await _vote_state(event)
    except Exception:
        log.exception("Could not read the governance state")
        return await _show(event, "❌ I couldn't reach the Neo network. Try again in a moment.", home_only())
    chosen = next((c for c in candidates if c.rank == rank), None)
    if chosen is None:
        return await _show(event, "That candidate is not in the list any more. Please choose again.", InlineKeyboardMarkup(inline_keyboard=[VOTE_BACK]))
    if main.neo <= 0:
        await state.clear()
        return await _show(event, "You need NEO to vote, and your main Neo N3 wallet has none available. Deposit NEO first, then come back.",
                           InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📥 Deposit NEO", callback_data="wallets")],
                                                                 [InlineKeyboardButton(text="⬅️ Back", callback_data=f"vt:s:{rank}")]]))
    existing = next((s.standing.neo for s in slots if s.candidate == chosen.pubkey), 0)
    await state.set_state(VoteFlow.amount)
    await state.update_data(vote_key=chosen.pubkey, vote_rank=rank)
    text, rows = _amount_view(chosen, main.neo, existing)
    await _show(event, text, InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.regexp(r"^vt:v:\d+$"))
async def vote_amount(cb: CallbackQuery, state: FSMContext):
    if not _private(cb):
        return await _done(cb, PRIVATE_ONLY, alert=True)
    await _done(cb)
    await _vote_amount_page(cb, state, int(cb.data.split(":")[2]))


async def _vote_confirm_page(event, state: FSMContext, amount: int):
    data = await state.get_data()
    try:
        candidates = await neovote.get_candidates(NEO_RPC, fresh=True)
        _addr, main, _slots = await _vote_state(event)
    except Exception:
        log.exception("Could not read the governance state")
        return await _show(event, "❌ I couldn't reach the Neo network. Try again in a moment.", home_only())
    chosen = neovote.find(candidates, data.get("vote_key"))
    if chosen is None:
        return await _show(event, "That candidate is not in the list any more. Please choose again.", InlineKeyboardMarkup(inline_keyboard=[VOTE_BACK]))
    if amount > main.neo:
        return await _show(event, f"You only have {main.neo} NEO available.", InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Back", callback_data=f"vt:v:{chosen.rank}")]]))
    nonce = secrets.token_hex(8)
    await state.set_state(None)
    await state.update_data(vote_nonce=nonce, vote_amount=amount, vote_key=chosen.pubkey)
    text, rows = _confirm_vote_view(chosen, amount, main.neo, nonce)
    await _show(event, text, InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.regexp(r"^vt:q:\d+:\d+$"))
async def vote_quick(cb: CallbackQuery, state: FSMContext):
    """A quick amount button (25%, 50%, 75%, all)."""
    if not _private(cb):
        return await _done(cb, PRIVATE_ONLY, alert=True)
    await _done(cb)
    _t, _q, rank, pct = cb.data.split(":")
    try:
        main = (await _vote_state(cb))[1]
    except Exception:
        return await _show(cb, "❌ I couldn't reach the Neo network. Try again in a moment.", home_only())
    data = await state.get_data()
    if not data.get("vote_key"):
        return await _vote_amount_page(cb, state, int(rank))
    await _vote_confirm_page(cb, state, max(1, main.neo * int(pct) // 100))


@router.message(VoteFlow.amount, F.text)
async def vote_amount_typed(message: Message, state: FSMContext):
    """The person typed how much NEO to vote with."""
    try:
        amount = neo_send.parse_amount(message.text, 0)
    except neo_send.SendError:
        return await message.answer("Please send a whole number of NEO, for example 5. Or /cancel.")
    await _vote_confirm_page(message, state, amount)


# ------------------------------------------------------------ doing the work
def _steps_text(result, ok_text: str) -> str:
    lines = []
    for label, outcome in result.done or []:
        lines.append(f"✅ {html.escape(label)} · {historyview.tx_html(SETTINGS.network, 'neo_n3', outcome.tx_hash)}")
    if result.ok:
        return (ok_text + ("\n\n" + "\n".join(lines) if lines else ""))
    lines.append(f"❌ Stopped at: <b>{html.escape(result.failed_step)}</b>\n{html.escape(result.reason)}")
    lines.append("\nNothing is lost: your NEO and GAS are in your own wallets. Open 📋 My votes to see where everything is and to finish or take it back.")
    return "\n".join(lines)


async def _vote_exec(cb: CallbackQuery, kind: str, **kw):
    """Runs one vote action (vote / finish / claim / withdraw) under the network lock and shows how it went."""
    lock = send_lock(cb.from_user.id, "neo_n3")
    if lock.locked():
        return await cb.message.edit_text("Another transaction on Neo N3 is still in progress. Please wait.")
    async with lock:
        await cb.message.edit_text("⏳ Working on it… there can be a few steps, this can take a few minutes. Please don't press anything.")
        session, user = await _load(cb)
        try:
            async with session:
                wallet = await service.get_wallet(session, user, "neo_n3")
                main = neovote.Account(wallet.address, await service.get_private_key(session, user, SETTINGS.master_key, "neo_n3"))
                slots = {r.id: r for r in await service.get_vote_wallets(session, user)}
                balances = await NEO_RPC.get_balances(main.address)
                if balances.get(neo_send.GAS_HASH, 0) < 3 * chains.N3_FEE_PER_TRANSFER + neo_send.MIN_GAS_RESERVE:
                    return await cb.message.edit_text(
                        "❌ You need a little more GAS in your Neo N3 wallet (about 0.15) to pay the network fees. Deposit some GAS first.",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[VOTE_BACK]))

                def account(row):
                    return neovote.Account(row.address, service.vote_wallet_key(SETTINGS.master_key, row))

                if kind == "vote":
                    row = await service.get_vote_wallet(session, user, kw["pubkey"])
                    if row is None:
                        row = await service.create_vote_wallet(session, user, SETTINGS.master_key, kw["pubkey"])
                        await session.commit()  # the new wallet's key is saved BEFORE any NEO is moved into it
                    steps = neovote.vote_steps(NEO_RPC, TRANSFER, VOTE_SEND, main, account(row), kw["pubkey"], kw["amount"])
                    ok_text = f"✅ <b>Vote sent.</b> {kw['amount']} NEO now votes for your candidate."
                elif kind == "finish":
                    row = slots[kw["slot"]]
                    steps = neovote.finish_vote_steps(NEO_RPC, TRANSFER, VOTE_SEND, main, account(row), row.candidate)
                    ok_text = "✅ <b>Vote finished.</b>"
                elif kind == "withdraw":
                    row = slots[kw["slot"]]
                    steps = neovote.withdraw_steps(NEO_RPC, TRANSFER, main, account(row))
                    ok_text = "✅ <b>Done.</b> The NEO and GAS are back in your main wallet."
                else:  # claim
                    target = kw["target"]
                    steps = []
                    if target in ("m", "all"):
                        steps += neovote.claim_steps(NEO_RPC, TRANSFER, main, main, True)
                    for sid, row in slots.items():
                        if target in ("all", str(sid)):
                            steps += neovote.claim_steps(NEO_RPC, TRANSFER, main, account(row), False)
                    ok_text = "✅ <b>GAS claimed.</b> It is in your main Neo N3 wallet."
                    kw["back"] = "earn"
        except Exception:
            log.exception("Governance action could not be prepared")
            return await cb.message.edit_text("❌ Something went wrong before anything was sent. Nothing was changed.",
                                              reply_markup=InlineKeyboardMarkup(inline_keyboard=[VOTE_BACK]))
        result = await neovote.run_steps(NEO_RPC, steps)
    neovote._cache["time"] = 0.0
    back = kw.get("back", "vote")
    await cb.message.edit_text(_steps_text(result, ok_text), reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Back", callback_data=back)]]))


@router.callback_query(F.data.startswith("vt:ok:"))
async def vote_confirm(cb: CallbackQuery, state: FSMContext):
    if not _private(cb):
        return await cb.answer(PRIVATE_ONLY, show_alert=True)
    data = await state.get_data()
    if not data or data.get("vote_nonce") != cb.data.split(":", 2)[2]:
        return await cb.answer("This confirmation has expired.", show_alert=True)
    pubkey, amount = data["vote_key"], int(data["vote_amount"])
    await state.clear()  # single use
    await cb.answer()
    await _vote_exec(cb, "vote", pubkey=pubkey, amount=amount)


# ----------------------------------------------------------------- claim pages
def _claim_view(main, slots, candidates):
    """Earn → Claim your GAS: the GAS from holding NEO and from each vote, a button for each, and one for all."""
    items = []  # (button label, target, raw GAS)
    if main.unclaimed > 0:
        items.append(("Holding NEO", "m", main.unclaimed))
    for s in slots:
        if s.standing.unclaimed > 0:
            items.append((f"Vote: {_cand_name(s.candidate, candidates)}", str(s.id), s.standing.unclaimed))
    total = sum(i[2] for i in items)
    lines = ["💰 <b>Claim your GAS</b>", "",
             f"GAS from holding NEO: <b>{_gas(main.unclaimed)} GAS</b>"]
    for s in slots:
        lines.append(f"GAS from voting for {html.escape(_cand_name(s.candidate, candidates))}: <b>{_gas(s.standing.unclaimed)} GAS</b>")
    lines += ["", "Everyone who holds NEO earns GAS from the holder share (10% of the GAS the network creates). "
                  "It builds up by itself, and you collect it here."]
    rows = []
    if not items:
        lines += ["", "Nothing is waiting yet."]
    else:
        lines += ["", f"Each claim pays small GAS network fees ({FEE_NOTE}), so a very small amount can cost more than it gives. "
                      "It keeps building up, so you can also wait."]
        for label, target, raw in items:
            rows.append([InlineKeyboardButton(text=f"💰 Claim · {label[:24]} · {_gas(raw)} GAS", callback_data=f"vt:cl:{target}")])
        if len(items) > 1:
            rows.append([InlineKeyboardButton(text=f"💰 Claim all at once · {_gas(total)} GAS", callback_data="vt:cl:all")])
    rows.append([InlineKeyboardButton(text="⬅️ Back", callback_data="earn")])
    return "\n".join(lines), rows


async def _claim_page(cb: CallbackQuery, state: FSMContext):
    if not _private(cb):
        return await _done(cb, PRIVATE_ONLY, alert=True)
    await state.clear()
    await _done(cb)
    try:
        candidates = await neovote.get_candidates(NEO_RPC)
        _addr, main, slots = await _vote_state(cb)
    except Exception:
        log.exception("Could not read the unclaimed GAS")
        return await _show(cb, "❌ I couldn't reach the Neo network. Try again in a moment.", home_only())
    text, rows = _claim_view(main, slots, candidates)
    await _show(cb, text, InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data == "vt:c")
async def vote_claim(cb: CallbackQuery, state: FSMContext):
    """Earn → Claim your GAS."""
    await _claim_page(cb, state)


@router.callback_query(F.data.regexp(r"^vt:cl:(m|all|\d+)$"))
async def vote_claim_ask(cb: CallbackQuery, state: FSMContext):
    if not _private(cb):
        return await _done(cb, PRIVATE_ONLY, alert=True)
    await _done(cb)
    target = cb.data.split(":")[2]
    nonce = secrets.token_hex(8)
    await state.update_data(vote_nonce=nonce, vote_target=target, vote_claim=True)
    what = {"m": "the GAS from holding NEO", "all": "all your GAS (from holding NEO and from your votes)"}.get(target, "the GAS from this vote")
    await _show(cb, f"<b>Claim {what}?</b>\n\nIt is paid into your main Neo N3 wallet. Small GAS network fees are paid from your wallet ({FEE_NOTE}).",
                InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✅ Claim", callback_data=f"vt:cok:{nonce}")],
                    [InlineKeyboardButton(text="⬅️ Back", callback_data="vt:c")]]))


@router.callback_query(F.data.startswith("vt:cok:"))
async def vote_claim_confirm(cb: CallbackQuery, state: FSMContext):
    if not _private(cb):
        return await cb.answer(PRIVATE_ONLY, show_alert=True)
    data = await state.get_data()
    if not data or not data.get("vote_claim") or data.get("vote_nonce") != cb.data.split(":", 2)[2]:
        return await cb.answer("This confirmation has expired.", show_alert=True)
    target = data["vote_target"]
    await state.clear()
    await cb.answer()
    await _vote_exec(cb, "claim", target=target)


# -------------------------------------------------------------------- my votes
@router.callback_query(F.data == "vt:m")
async def vote_mine(cb: CallbackQuery, state: FSMContext):
    if not _private(cb):
        return await _done(cb, PRIVATE_ONLY, alert=True)
    await state.clear()
    await _done(cb)
    try:
        candidates = await neovote.get_candidates(NEO_RPC)
        _addr, main, slots = await _vote_state(cb)
    except Exception:
        log.exception("Could not read the votes")
        return await _show(cb, "❌ I couldn't reach the Neo network. Try again in a moment.", home_only())
    if not slots:
        return await _show(cb, "You have not voted yet.", InlineKeyboardMarkup(inline_keyboard=[VOTE_BACK]))
    lines = ["📋 <b>My votes</b>", "", f"NEO in your main wallet: <b>{main.neo}</b>", ""]
    rows = []
    for s in slots:
        name = _cand_name(s.candidate, candidates)
        lines.append(f"• <b>{html.escape(name)}</b>: {s.standing.neo} NEO · {_gas(s.standing.unclaimed)} GAS waiting")
        rows.append([InlineKeyboardButton(text=f"{name[:24]} · {s.standing.neo} NEO", callback_data=f"vt:w:{s.id}")])
    rows.append(VOTE_BACK)
    await _show(cb, "\n".join(lines) + "\n\nTap one to claim, finish or take your NEO back.", InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.regexp(r"^vt:w:\d+$"))
async def vote_slot(cb: CallbackQuery, state: FSMContext):
    if not _private(cb):
        return await _done(cb, PRIVATE_ONLY, alert=True)
    await state.clear()
    await _done(cb)
    sid = int(cb.data.split(":")[2])
    try:
        candidates = await neovote.get_candidates(NEO_RPC)
        _addr, _main, slots = await _vote_state(cb)
    except Exception:
        log.exception("Could not read the vote")
        return await _show(cb, "❌ I couldn't reach the Neo network. Try again in a moment.", home_only())
    s = next((x for x in slots if x.id == sid), None)
    if s is None:
        return await _show(cb, "That vote was not found.", InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Back", callback_data="vt:m")]]))
    cand = neovote.find(candidates, s.candidate)
    lines = [f"📋 <b>{html.escape(_cand_name(s.candidate, candidates))}</b>",
             _vote_tier_line(cand) if cand else "⚠️ This candidate is not registered any more, so this vote earns nothing.", "",
             f"Voting with: <b>{s.standing.neo} NEO</b>",
             f"GAS waiting: <b>{_gas(s.standing.unclaimed)} GAS</b>",
             f"GAS kept in this vote wallet for fees: {_gas(s.gas)} GAS"]
    rows = []
    unfinished = s.standing.neo > 0 and s.standing.vote_to != s.candidate
    if unfinished:
        lines += ["", "⚠️ The NEO is here but the vote was not finished. Tap <b>Finish the vote</b>."]
        rows.append([InlineKeyboardButton(text="🔁 Finish the vote", callback_data=f"vt:fi:{s.id}")])
    if s.standing.unclaimed > 0:
        rows.append([InlineKeyboardButton(text=f"💰 Claim {_gas(s.standing.unclaimed)} GAS", callback_data=f"vt:cl:{s.id}")])
    rows.append([InlineKeyboardButton(text="↩️ Take the NEO back", callback_data=f"vt:wd:{s.id}")])
    rows.append([InlineKeyboardButton(text="⬅️ Back", callback_data="vt:m")])
    await _show(cb, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.regexp(r"^vt:fi:\d+$"))
async def vote_finish(cb: CallbackQuery, state: FSMContext):
    if not _private(cb):
        return await cb.answer(PRIVATE_ONLY, show_alert=True)
    await state.clear()
    await cb.answer()
    await _vote_exec(cb, "finish", slot=int(cb.data.split(":")[2]))


@router.callback_query(F.data.regexp(r"^vt:wd:\d+$"))
async def vote_withdraw_ask(cb: CallbackQuery, state: FSMContext):
    if not _private(cb):
        return await _done(cb, PRIVATE_ONLY, alert=True)
    await _done(cb)
    sid = cb.data.split(":")[2]
    nonce = secrets.token_hex(8)
    await state.update_data(vote_nonce=nonce, vote_target=sid, vote_withdraw=True)
    await _show(cb, "<b>Take the NEO back?</b>\n\nAll the NEO and GAS in this vote wallet move to your main Neo N3 wallet, and this vote ends. "
                    f"Small GAS network fees are paid ({FEE_NOTE}).",
                InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✅ Take it back", callback_data=f"vt:wdok:{nonce}")],
                    [InlineKeyboardButton(text="⬅️ Back", callback_data=f"vt:w:{sid}")]]))


@router.callback_query(F.data.startswith("vt:wdok:"))
async def vote_withdraw(cb: CallbackQuery, state: FSMContext):
    if not _private(cb):
        return await cb.answer(PRIVATE_ONLY, show_alert=True)
    data = await state.get_data()
    if not data or not data.get("vote_withdraw") or data.get("vote_nonce") != cb.data.split(":", 2)[2]:
        return await cb.answer("This confirmation has expired.", show_alert=True)
    sid = int(data["vote_target"])
    await state.clear()
    await cb.answer()
    await _vote_exec(cb, "withdraw", slot=sid)


# ----------------------------------------------------------------------- bridge
DIRECTION_LABEL = {"n3x": ("Neo N3", "Neo X"), "xn3": ("Neo X", "Neo N3")}


@dataclass
class BridgeCtx:
    direction: str
    limits: bridge.Limits
    balance: int  # GAS on the source chain, smallest unit
    token: Token
    src_wallet: object
    dst_wallet: object


def _bridge_available() -> bool:
    return BRIDGE_N3_HASH is not None and "neo_n3" in OPS and "neo_x" in OPS


def _brand(dex) -> str:
    """The exchange without its pool version: "PancakeSwap V3" -> "PancakeSwap"."""
    return re.sub(r"\s+V[234]$", "", dex.name)


def _dex_buttons(token, slug: str, dexes) -> list[list[InlineKeyboardButton]]:
    """One "Trade here with <exchange>" button for every exchange (the best pool of each, see _best_dexes)."""
    if str(token.contract).lower() == "native":
        return []
    return [[InlineKeyboardButton(text=f"💱 Trade here with {_brand(d)}", callback_data=f"q:{token.id}:{d.key}")] for d in dexes]


_PROBE_CACHE: dict = {}  # (network, token id) -> (time, {dex key: what a tiny buy returns})
PROBE_SECONDS = 900


async def _best_dexes(slug: str, token, ops, listed) -> list:
    """One entry per exchange: the pool version that pays out the most for a tiny buy, best exchange first.
    Every DEX of the network is asked directly (a read-only price check, nothing is spent), because DexScreener lists
    only the biggest pools. An exchange DexScreener lists but that cannot be priced here keeps its first listed version."""
    if str(token.contract).lower() == "native" or not hasattr(ops, "rpc") or not hasattr(ops, "cfg"):
        return []
    key = (slug, token.id)
    cached = _PROBE_CACHE.get(key)
    if cached and time.monotonic() - cached[0] < PROBE_SECONDS:
        outs = cached[1]
    else:
        async def one(dex):
            try:
                if not await dexquote.verify_router(ops.rpc, dex):
                    return dex.key, 0
                got = await dexquote.quote(ops.rpc, dex, token.contract, token.symbol, token.decimals, ops.native_symbol,
                                           Decimal("0.001"), True)
                return dex.key, (got.amount_out_raw if got is not None else 0)
            except Exception:
                return dex.key, 0

        try:
            pairs = await asyncio.wait_for(asyncio.gather(*(one(d) for d in dexquote.DEXES if d.chain == slug)), 10)
        except Exception:
            pairs = []
        outs = dict(pairs)
        if outs:
            _PROBE_CACHE[key] = (time.monotonic(), outs)
    best: dict[str, tuple] = {}  # exchange -> (what it pays, dex)
    for d in dexquote.DEXES:
        if d.chain != slug:
            continue
        got = outs.get(d.key, 0)
        if got > 0 and (_brand(d) not in best or got > best[_brand(d)][0]):
            best[_brand(d)] = (got, d)
    for d in listed:  # listed by DexScreener but not priceable here: keep one button for that exchange
        best.setdefault(_brand(d), (0, d))
    ranked = sorted(best.values(), key=lambda gd: -gd[0])
    return [d for _g, d in ranked]


def normalize_hash_safe(value) -> str:
    v = str(value).strip().lower()
    return v if v in ("native", "") or v.startswith("0x") else "0x" + v


async def _dex_token(event, token_id: int):
    """(token, network slug) of one of the person's tokens on a network where in-bot trading exists."""
    session, user = await _load(event)
    async with session:
        for slug in sorted({d.chain for d in dexquote.DEXES}):
            chain = await service.get_chain(session, slug)
            if chain is None:
                continue
            for token in await service.visible_tokens(session, user, chain.id):
                if token.id == token_id:
                    return token, slug
    return None, None


@router.callback_query(F.data.startswith("q:"))
async def quote_start(cb: CallbackQuery, state: FSMContext):
    """💱 Trade here with <DEX>: buy the token with the network's coin, or sell it for the coin."""
    await state.clear()
    if not _private(cb):
        return await _done(cb, PRIVATE_ONLY, alert=True)
    try:
        _, token_id, key = cb.data.split(":")
        token_id, dex = int(token_id), dexquote.BY_KEY[key]
    except (ValueError, KeyError):
        return await _done(cb)
    token, slug = await _dex_token(cb, token_id)
    if token is None or slug != dex.chain or OPS.get(slug) is None:
        return await _done(cb, "That token is not on your list any more.", alert=True)
    coin = OPS[slug].native_symbol
    await _done(cb)
    await _show(cb, f"💱 <b>{html.escape(token.symbol)} on {dex.name}</b> · {html.escape(OPS[slug].name)}\n\nWhat do you want to do?",
                InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text=f"Buy {token.symbol} with {coin}", callback_data=f"qd:{token.id}:{key}:buy")],
                    [InlineKeyboardButton(text=f"Sell {token.symbol} for {coin}", callback_data=f"qd:{token.id}:{key}:sell")],
                    [InlineKeyboardButton(text="⬅️ Back", callback_data=f"trd:{slug}:{token.id}")],
                ]))


@router.callback_query(F.data.startswith("qd:"))
async def quote_direction(cb: CallbackQuery, state: FSMContext):
    if not _private(cb):
        return await _done(cb, PRIVATE_ONLY, alert=True)
    try:
        _, token_id, key, way = cb.data.split(":")
        token_id, dex = int(token_id), dexquote.BY_KEY[key]
    except (ValueError, KeyError):
        return await _done(cb)
    token, slug = await _dex_token(cb, token_id)
    if token is None or way not in ("buy", "sell") or slug != dex.chain or OPS.get(slug) is None:
        return await _done(cb, "That token is not on your list any more.", alert=True)
    await _done(cb)
    keep = (await state.get_data()).get("slip", str(dexquote.DEFAULT_SLIPPAGE))
    await state.set_state(TradeFlow.amount)
    await state.update_data(token_id=token.id, dex=key, way=way, slip=keep)
    coin = OPS[slug].native_symbol
    bal_raw, bal = await _trade_balance(cb, token, slug, way)
    await state.update_data(bal_raw=str(bal_raw) if bal_raw is not None else "")
    spend = coin if way == "buy" else html.escape(token.symbol)
    what = f"how much {coin} you want to spend" if way == "buy" else f"how many {html.escape(token.symbol)} you want to sell"
    have = (f"Your balance: <b>{dexquote._num(bal)} {spend}</b>\n" if bal is not None
            else "I couldn't read your balance just now.\n")
    rows = []
    if bal_raw:
        rows.append([InlineKeyboardButton(text=t, callback_data=f"qa:{v}") for t, v in
                     (("25%", "25"), ("50%", "50"), ("75%", "75"), ("Max", "100"))])
    rows.append([InlineKeyboardButton(text="⬅️ Back", callback_data=f"q:{token.id}:{key}")])
    hint = await _price_hint(token, slug, way, bal)
    await _show(cb, f"{hint}{have}Tap a share of it, or type {what}, for example <code>0.5</code>.\n"
                    "You will see the full quote before anything is sent.", InlineKeyboardMarkup(inline_keyboard=rows))


async def _price_hint(token, slug: str, way: str, bal) -> str:
    """Lines for the amount page: the token's price, and roughly what the whole balance would buy.
    Only a guide: the quote that follows includes the trade fee and the price impact."""
    coin = OPS[slug].native_symbol
    try:
        live, token_usd = await asyncio.gather(
            asyncio.wait_for(PRICES.prices(), 6), asyncio.wait_for(PRICES.price_of(token, slug), 6), return_exceptions=True)
    except Exception:
        return ""
    coin_usd = live.get(coin) if isinstance(live, dict) else None
    token_usd = token_usd if isinstance(token_usd, Decimal) else None
    if not coin_usd or not token_usd:
        return ""
    sym = html.escape(token.symbol)
    per = token_usd / coin_usd
    lines = [f"💲 Price: 1 {sym} ≈ <b>${dexquote._num(token_usd)}</b> (≈ {dexquote._num(per)} {coin})"]
    if bal:
        if way == "buy":
            lines.append(f"With all your {coin} ({dexquote._num(bal)}) you could get about {dexquote._num(bal / per)} {sym}")
        else:
            lines.append(f"Selling all your {sym} ({dexquote._num(bal)}) would give about {dexquote._num(bal * per)} {coin}")
        lines.append("<i>A guide only: the quote adds the trade fee and price impact.</i>")
    return "\n".join(lines) + "\n\n"


async def _trade_balance(event, token, slug: str, way: str):
    """(raw amount you can spend, as a Decimal) for this trade, or (None, None). Buying spends the coin, selling the token.
    For the coin, the network fee of the swap is kept back so that "Max" can really be traded."""
    ops = OPS[slug]
    try:
        session, user = await _load(event)
        async with session:
            chain = await service.get_chain(session, slug)
            wallet = await service.get_wallet(session, user, ops.key_type)
            native = await service.native_token(session, chain.id)
            spend_token = native if way == "buy" else token
            balances = await asyncio.wait_for(ops.balances(wallet.address, await _balance_tokens(session, chain, spend_token)), 10)
            raw = await ops.max_sendable(spend_token, wallet.address, balances)
        if way == "buy":
            try:
                price = await asyncio.wait_for(ops.rpc.gas_price(), 6)
            except Exception:
                price = 0
            raw = max(0, raw - dexquote.SWAP_GAS_UNITS * price * 2)  # room for the swap's fee, with a margin
        return raw, Decimal(raw) / (Decimal(10) ** (18 if way == "buy" else token.decimals))
    except Exception as exc:
        log.warning("Trade balance unavailable: %r", exc)
        return None, None


def _slippage_keyboard(token, key: str, way: str, slip: str, nonce: str | None, slug: str, impact=None) -> InlineKeyboardMarkup:
    row = [InlineKeyboardButton(text=("✅ " if Decimal(slip) == c else "") + f"{c.normalize():f}%", callback_data=f"qs:{c.normalize():f}")
           for c in dexquote.SLIPPAGE_CHOICES]
    rows = [row]
    if nonce:
        rows.append([_confirm_button(nonce, "q", impact)])
    rows.append([InlineKeyboardButton(text="✏️ Another amount", callback_data=f"qd:{token.id}:{key}:{way}")])
    rows.append([InlineKeyboardButton(text="⬅️ Back", callback_data=f"trd:{slug}:{token.id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _make_quote(token, slug: str, data: dict):
    """(quote or None, gas price). Reads the router on-chain; sends nothing."""
    ops, dex = OPS[slug], dexquote.BY_KEY[data["dex"]]
    try:
        gas_price = await asyncio.wait_for(ops.rpc.gas_price(), 8)
    except Exception:
        gas_price = None
    return await asyncio.wait_for(
        dexquote.quote(ops.rpc, dex, token.contract, token.symbol, token.decimals, ops.native_symbol,
                       Decimal(data["amount"]), data["way"] == "buy", Decimal(data.get("slip", str(dexquote.DEFAULT_SLIPPAGE))), gas_price), 30)


async def _show_quote(event, state: FSMContext) -> None:
    data = await state.get_data()
    token, slug = await _dex_token(event, int(data["token_id"]))
    if token is None or OPS.get(slug) is None:
        return await _show(event, "That token is not on your list any more.", home_only())
    ops, dex = OPS[slug], dexquote.BY_KEY[data["dex"]]
    way, slip, key = data["way"], Decimal(data.get("slip", str(dexquote.DEFAULT_SLIPPAGE))), data["dex"]
    try:
        q = await _make_quote(token, slug, data)
    except Exception as exc:
        log.warning("Swap quote failed: %r", exc)
        return await _show(event, "I couldn't reach the network just now. Please try again in a moment.",
                           _slippage_keyboard(token, key, way, str(slip), None, slug))
    if q is None:
        return await _show(event, f"I couldn't find a {dex.name} route for that amount. The token may trade only in another "
                                  "pool type there, or the pool is too small.",
                           _slippage_keyboard(token, key, way, str(slip), None, slug))
    try:
        live = await asyncio.wait_for(PRICES.prices(), 6)
    except Exception:
        live = {}
    token_usd = None
    try:
        token_usd = await asyncio.wait_for(PRICES.price_of(token, slug), 6)
    except Exception:
        pass
    nonce = secrets.token_hex(4)
    await state.update_data(nonce=nonce, preview_out=str(q.amount_out_raw), impact=str(q.impact_pct),
                            give=f"{dexquote._num(q.pay)} {q.pay_symbol}", get=f"{dexquote._num(q.receive)} {q.receive_symbol}")
    extra = ("\n\n" + ("Selling needs a one-off approval for exactly this amount, sent first (it costs a small network fee). "
                       if not q.buying else "")
             + "Prices move: if the price changes too much before you confirm, I will stop and show a new quote. "
               "Some tokens charge their own tax on trades; it is not in this quote. Not financial advice.")
    text = dexquote.render_quote(q, slip, ops.native_symbol, live.get(ops.native_symbol), token_usd, html.escape(ops.name)) + extra
    await _show(event, text[:4000], _slippage_keyboard(token, key, way, str(slip), nonce, slug, q.impact_pct))


async def _quote_or_explain(event, state: FSMContext) -> None:
    """Shows the quote. Whatever goes wrong, the person always gets an answer (never silence)."""
    try:
        await _show_quote(event, state)
    except Exception:
        log.exception("Showing the swap quote failed")
        try:
            msg = "I couldn't show that quote just now. Please try again, or tap /cancel."
            await (event.message.answer(msg) if isinstance(event, CallbackQuery) else event.answer(msg))
        except Exception:
            log.exception("Could not even send the error message")


@router.message(TradeFlow.amount, F.text)
async def quote_amount(message: Message, state: FSMContext):
    text = (message.text or "").strip().replace(",", ".")
    try:
        if text.lower() == "max":
            raw = (await state.get_data()).get("bal_raw")
            if not raw:
                raise ValueError
            return await _use_share(message, state, 100)
        amount = Decimal(text)
        if amount <= 0 or amount > Decimal("1e15"):
            raise ValueError
    except Exception:
        return await message.answer("Please type a number bigger than zero, like 0.5")
    await state.update_data(amount=str(amount))
    try:
        await message.bot.send_chat_action(message.chat.id, "typing")
    except Exception:
        pass
    await _quote_or_explain(message, state)


async def _use_share(event, state: FSMContext, percent: int) -> None:
    data = await state.get_data()
    raw = data.get("bal_raw")
    if not raw:
        msg = "I don't know your balance, so please type an amount."
        return await (event.message.answer(msg) if isinstance(event, CallbackQuery) else event.answer(msg))
    token, slug = await _dex_token(event, int(data["token_id"]))
    if token is None:
        return await _show(event, "That token is not on your list any more.", home_only())
    decimals = 18 if data["way"] == "buy" else token.decimals
    amount = Decimal(int(raw) * percent // 100) / (Decimal(10) ** decimals)
    if amount <= 0:
        msg = "That share is too small to trade. Try a bigger one."
        return await (event.message.answer(msg) if isinstance(event, CallbackQuery) else event.answer(msg))
    await state.update_data(amount=f"{amount:f}")
    await _quote_or_explain(event, state)


@router.callback_query(F.data.startswith("qa:"), TradeFlow.amount)
async def quote_share(cb: CallbackQuery, state: FSMContext):
    try:
        percent = int(cb.data.split(":", 1)[1])
        if percent not in (25, 50, 75, 100):
            raise ValueError
    except ValueError:
        return await _done(cb)
    await _done(cb)
    await _use_share(cb, state, percent)


@router.callback_query(F.data.startswith("qs:"), TradeFlow.amount)
async def quote_slippage(cb: CallbackQuery, state: FSMContext):
    try:
        slip = Decimal(cb.data.split(":", 1)[1])
    except Exception:
        return await _done(cb)
    if slip not in dexquote.SLIPPAGE_CHOICES or "amount" not in (await state.get_data()):
        return await _done(cb)
    await state.update_data(slip=str(slip))
    await _done(cb)
    await _quote_or_explain(cb, state)


async def _high_impact_screen(cb: CallbackQuery, state: FSMContext, prefix: str):
    data = await state.get_data()
    if not data or data.get("nonce") != cb.data.split(":", 1)[1]:
        return await cb.answer("This confirmation has expired. Start the trade again.", show_alert=True)
    await cb.answer()
    nonce = data["nonce"]
    again = f"{_DIR_PREFIX.get(data.get('dexk', 'ftw'), 'fd')}:{data['token_id']}:{data['way']}" if prefix == "f" else f"qd:{data['token_id']}:{data['dex']}:{data['way']}"
    await cb.message.edit_text(
        _high_impact_text(data.get("impact", "0"), html.escape(data.get("give", "?")), html.escape(data.get("get", "?"))),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Choose a smaller amount", callback_data=again)],
            [InlineKeyboardButton(text="⚠️ Yes, trade anyway", callback_data=f"{prefix}ok:{nonce}")],
        ]))


@router.callback_query(F.data.startswith("qhi:"))
async def quote_high_impact(cb: CallbackQuery, state: FSMContext):
    if not _private(cb):
        return await cb.answer(PRIVATE_ONLY, show_alert=True)
    await _high_impact_screen(cb, state, "q")


@router.callback_query(F.data.startswith("fhi:"))
async def ftw_high_impact(cb: CallbackQuery, state: FSMContext):
    if not _private(cb):
        return await cb.answer(PRIVATE_ONLY, show_alert=True)
    await _high_impact_screen(cb, state, "f")


@router.callback_query(F.data.startswith("qok:"))
async def quote_confirm(cb: CallbackQuery, state: FSMContext):
    """✅ Confirm trade: a fresh quote, every safety check, then the swap itself."""
    if not _private(cb):
        return await cb.answer(PRIVATE_ONLY, show_alert=True)
    data = await state.get_data()
    if not data or data.get("nonce") != cb.data.split(":", 1)[1]:
        return await cb.answer("This confirmation has expired. Start the trade again.", show_alert=True)
    await state.clear()  # single use: a second tap finds nothing to confirm
    await cb.answer()
    token, slug = await _dex_token(cb, int(data["token_id"]))
    ops, dex = OPS.get(slug or ""), dexquote.BY_KEY.get(data["dex"])
    if token is None or ops is None or dex is None:
        return await cb.message.edit_text("That token or network is not available right now.", reply_markup=home_only())
    lock = send_lock(cb.from_user.id, slug)
    if lock.locked():
        return await cb.message.edit_text("Another transaction on this network is still in progress. Please wait.")
    async with lock:
        await cb.message.edit_text("⏳ Checking the price…")
        try:
            fresh = await _make_quote(token, slug, data)
        except Exception:
            return await cb.message.edit_text("❌ I couldn't reach the network. Nothing was sent.", reply_markup=home_only())
        if fresh is None:
            return await cb.message.edit_text("❌ The route is gone. Nothing was sent.", reply_markup=home_only())
        slip = Decimal(data.get("slip", str(dexquote.DEFAULT_SLIPPAGE)))
        seen = int(data.get("preview_out", "0"))
        if seen and fresh.amount_out_raw < dexquote.min_out(seen, slip):
            # the price moved against the person after they looked at it: show the new quote instead of trading
            await state.set_state(TradeFlow.amount)
            await state.update_data(**{k: v for k, v in data.items() if k not in ("nonce", "preview_out")})
            await cb.message.answer("⚠️ The price moved while you were deciding, so I did not trade. Here is the new quote.")
            return await _show_quote(cb.message, state)
        await cb.message.edit_text(
            "⏳ Trading… " + ("The approval comes first. " if not fresh.buying else "") + "This can take a couple of minutes.")
        session, user = await _load(cb)
        async with session:
            chain = await service.get_chain(session, slug)
            wallet = await service.get_wallet(session, user, ops.key_type)
            wallet_address = wallet.address
            visible = await service.visible_tokens(session, user, chain.id)
            paid = token if not fresh.buying else next((t for t in visible if str(t.contract).lower() == "native"), None)
            if paid is None:
                return await cb.message.edit_text("❌ I could not find your wallet's coin on this network.", reply_markup=home_only())
            row = await service.record_outgoing(
                session, user, paid, "swap", fresh.amount_in_raw,
                f"swap on {dex.name} ({slug}): {fresh.pay} {fresh.pay_symbol} for {fresh.receive_symbol}")
            private_key = await service.get_private_key(session, user, SETTINGS.master_key, ops.key_type)
            receive_token = token if fresh.buying else next((t for t in visible if str(t.contract).lower() == "native"), None)
            before = await _balance_of(ops, wallet_address, receive_token)
            try:
                outcome, approve_hash = await dexswap.swap(ops.rpc, ops.cfg.chain_id, private_key, wallet_address, fresh,
                                                           token.contract, ops.native_symbol)
            except neo_send.SendError as exc:
                row.status = "failed"
                row.note = f"{row.note} | {str(exc)[:100]}"[:250]
                await session.commit()
                return await cb.message.edit_text(f"❌ {html.escape(str(exc))}", reply_markup=home_only())
            except Exception:
                log.exception("Swap failed before it was sent")
                row.status = "failed"
                await session.commit()
                return await cb.message.edit_text("❌ Something went wrong before the swap was sent. Nothing was traded.",
                                                  reply_markup=home_only())
            try:
                row.tx_hash, row.status = outcome.tx_hash, outcome.status
                await session.commit()
            except Exception:  # the swap already happened: never let a bookkeeping error hide the result
                log.exception("Could not save the swap result in History")
    tx = historyview.tx_html(SETTINGS.network, slug, outcome.tx_hash)
    paid_txt = f"{dexquote._num(fresh.pay)} {html.escape(fresh.pay_symbol)}"
    if outcome.status == "confirmed":
        got_txt, now_txt, fee_txt = "", "", ""
        try:
            after = await _balance_of(ops, wallet_address, receive_token)
            dec = Decimal(10) ** receive_token.decimals
            if after is not None:
                now_txt = f"\nYour balance now: <b>{dexquote._num(Decimal(after) / dec)} {html.escape(receive_token.symbol)}</b>"
            fees = await _fees_paid(ops.rpc, [outcome.tx_hash] + ([approve_hash] if approve_hash else []))
            if fees:
                fee_coin = Decimal(fees) / (Decimal(10) ** 18)
                try:
                    usd = (await asyncio.wait_for(PRICES.prices(), 6)).get(ops.native_symbol)
                except Exception:
                    usd = None
                fee_txt = (f"\n⛽ Network fee paid: <b>{dexquote._num(fee_coin)} {html.escape(ops.native_symbol)}</b>"
                           f"{dexquote._usd(fee_coin, usd)}" + (" (approval + swap)" if approve_hash else ""))
            if after is not None and before is not None:
                gained = after - before
                approx = ""
                if not fresh.buying:  # the coin balance also went down by the network fees: add them back
                    gained += fees
                    approx = "" if fees else "≈ "
                if gained > 0:
                    got_txt = f"📥 Received: <b>{approx}{dexquote._num(Decimal(gained) / dec)} {html.escape(receive_token.symbol)}</b>\n"
        except Exception:
            log.warning("Could not read the balances after the swap")
        if not got_txt:
            got_txt = (f"📥 Received: about <b>{dexquote._num(fresh.receive)} {html.escape(fresh.receive_symbol)}</b> "
                       f"(at least {dexquote._num(fresh.min_receive)})\n")
        text = (f"✅ <b>Trade done</b> on {dex.name} ({html.escape(ops.name)})\n"
                f"📤 Sent: <b>{paid_txt}</b>\n{got_txt.rstrip()}{fee_txt}{now_txt}\n{tx}")
    elif outcome.status == "failed":
        text = (f"❌ The swap failed on-chain ({html.escape(outcome.detail)}). Nothing was traded; a small "
                f"{html.escape(ops.native_symbol)} fee may have been used.\n{tx}")
    else:
        text = ("⚠️ The swap was sent but I could not confirm it yet. <b>Do not trade again</b> until you check your balance.\n" + tx)
    try:
        await cb.message.edit_text(text, reply_markup=home_only())
    except Exception:
        log.exception("Could not edit the swap result message; sending it as a new one")
        try:
            await cb.message.answer(text, reply_markup=home_only())
        except Exception:
            log.exception("Could not send the swap result")
            plain = re.sub(r"<[^>]+>", "", text)
            await cb.message.answer(plain)


# ---------------------------------------------------------------- FTW Swap (Neo N3)
async def _ftw_token(event, token_id: int):
    """One of the person's Neo N3 tokens (not GAS itself) that can be traded against GAS."""
    session, user = await _load(event)
    async with session:
        chain = await service.get_chain(session, "neo_n3")
        if chain is None:
            return None
        for token in await service.visible_tokens(session, user, chain.id):
            if token.id == token_id and normalize_hash_safe(token.contract) not in (ftwswap.GAS_HASH, "native"):
                return token
    return None


async def _ftw_wallet(event) -> str:
    session, user = await _load(event)
    async with session:
        return (await service.get_wallet(session, user, "neo_n3")).address


def _ftw_keyboard(token, slip: str, nonce: str | None, way: str, impact=None, dexk: str = "ftw") -> InlineKeyboardMarkup:
    row = [InlineKeyboardButton(text=("✅ " if Decimal(slip) == c else "") + f"{c.normalize():f}%", callback_data=f"fs:{c.normalize():f}")
           for c in dexquote.SLIPPAGE_CHOICES]
    rows = [row]
    if nonce:
        rows.append([_confirm_button(nonce, "f", impact)])
    rows.append([InlineKeyboardButton(text="✏️ Another amount", callback_data=f"{_DIR_PREFIX[dexk]}:{token.id}:{way}")])
    rows.append([InlineKeyboardButton(text="⬅️ Back", callback_data=f"trd:neo_n3:{token.id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


_DEX_NAMES = {"ftw": "FTW Swap", "flam": "Flamingo"}
_DIR_PREFIX = {"ftw": "fd", "flam": "md"}
_START_PREFIX = {"ftw": "f", "flam": "m"}


@router.callback_query(F.data.startswith("f:"))
async def ftw_start(cb: CallbackQuery, state: FSMContext):
    """💱 Trade here with FTW Swap: buy the token with GAS, or sell it for GAS."""
    await _n3_start(cb, state, "ftw")


@router.callback_query(F.data.startswith("m:"))
async def flamingo_start(cb: CallbackQuery, state: FSMContext):
    """💱 Trade here with Flamingo."""
    await _n3_start(cb, state, "flam")


async def _n3_start(cb: CallbackQuery, state: FSMContext, dexk: str):
    await state.clear()
    if not _private(cb):
        return await _done(cb, PRIVATE_ONLY, alert=True)
    try:
        token_id = int(cb.data.split(":")[1])
    except (ValueError, IndexError):
        return await _done(cb)
    token = await _ftw_token(cb, token_id)
    if token is None:
        return await _done(cb, "That token is not on your list any more.", alert=True)
    await _done(cb)
    if dexk == "flam" and not flamingo.has_route(token.contract):
        # first time for this token: look for its Flamingo route (read-only, a few seconds), then remember it
        await _show(cb, f"🔎 Looking for {html.escape(token.symbol)} on Flamingo… this takes a few seconds the first time.", None)
        try:
            route = await asyncio.wait_for(flamingo.ensure_route(OPS["neo_n3"].rpc, ftwswap.script_hash(await _ftw_wallet(cb)),
                                                                 token.contract), 90)
        except Exception as exc:
            log.warning("Flamingo route search failed: %r", exc)
            return await _show(cb, "Flamingo couldn't be reached just now. Please try again in a minute.", home_only())
        if route is None:
            return await _show(cb, f"{html.escape(token.symbol)} isn't traded on Flamingo (no pool found). "
                                   "You can still try FTW Swap from the token page.", home_only())
    d = _DIR_PREFIX[dexk]
    await _show(cb, f"💱 <b>{html.escape(token.symbol)} on {_DEX_NAMES[dexk]}</b> · Neo N3\n\nWhat do you want to do?",
                InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text=f"Buy {token.symbol} with GAS", callback_data=f"{d}:{token.id}:buy")],
                    [InlineKeyboardButton(text=f"Sell {token.symbol} for GAS", callback_data=f"{d}:{token.id}:sell")],
                    [InlineKeyboardButton(text="⬅️ Back", callback_data=f"trd:neo_n3:{token.id}")],
                ]))


async def _ftw_balance(event, token, way: str):
    """(raw spendable, Decimal) of what this trade spends, or (None, None). Spending GAS keeps 0.3 GAS back for fees."""
    ops = OPS["neo_n3"]
    try:
        address = await _ftw_wallet(event)
        got = await asyncio.wait_for(ops.rpc.get_balances(address), 10)
        if way == "buy":
            raw = max(0, int(got.get(ftwswap.GAS_HASH, 0)) - ftwswap.GAS_RESERVE)
            return raw, Decimal(raw) / Decimal(10 ** 8)
        raw = int(got.get(normalize_hash_safe(token.contract), 0))
        return raw, Decimal(raw) / (Decimal(10) ** token.decimals)
    except Exception as exc:
        log.warning("FTW balance unavailable: %r", exc)
        return None, None


@router.callback_query(F.data.startswith("fd:"))
async def ftw_direction(cb: CallbackQuery, state: FSMContext):
    await _n3_direction(cb, state, "ftw")


@router.callback_query(F.data.startswith("md:"))
async def flamingo_direction(cb: CallbackQuery, state: FSMContext):
    await _n3_direction(cb, state, "flam")


async def _n3_direction(cb: CallbackQuery, state: FSMContext, dexk: str):
    if not _private(cb):
        return await _done(cb, PRIVATE_ONLY, alert=True)
    try:
        _, token_id, way = cb.data.split(":")
        token_id = int(token_id)
    except ValueError:
        return await _done(cb)
    token = await _ftw_token(cb, token_id)
    if token is None or way not in ("buy", "sell"):
        return await _done(cb, "That token is not on your list any more.", alert=True)
    await _done(cb)
    keep = (await state.get_data()).get("slip", str(dexquote.DEFAULT_SLIPPAGE))
    await state.set_state(FtwFlow.amount)
    bal_raw, bal = await _ftw_balance(cb, token, way)
    await state.update_data(token_id=token.id, way=way, slip=keep, dexk=dexk, bal_raw=str(bal_raw) if bal_raw is not None else "")
    spend = "GAS" if way == "buy" else html.escape(token.symbol)
    what = "how much GAS you want to spend" if way == "buy" else f"how many {html.escape(token.symbol)} you want to sell"
    have = (f"Your balance: <b>{dexquote._num(bal)} {spend}</b>\n" if bal is not None else "I couldn't read your balance just now.\n")
    rows = []
    if bal_raw:
        rows.append([InlineKeyboardButton(text=t, callback_data=f"fa:{v}") for t, v in
                     (("25%", "25"), ("50%", "50"), ("75%", "75"), ("Max", "100"))])
    rows.append([InlineKeyboardButton(text="⬅️ Back", callback_data=f"{_START_PREFIX[dexk]}:{token.id}")])
    hint = await _price_hint(token, "neo_n3", way, bal)
    await _show(cb, f"{hint}{have}Tap a share of it, or type {what}, for example <code>0.5</code>.\n"
                    "You will see the full quote before anything is sent.", InlineKeyboardMarkup(inline_keyboard=rows))


def _ftw_budget(data: dict, spend_raw: int) -> int:
    """The amount to quote. FTW fixes what you receive, so what the person typed is the MOST they can pay, slippage included.
    Flamingo fixes what you pay, so the typed amount is paid as it is (slippage protects the amount received)."""
    if data.get("dexk") == "flam":
        return spend_raw
    return ftwswap.budget_for(spend_raw, Decimal(data.get("slip", str(dexquote.DEFAULT_SLIPPAGE))))


async def _ftw_make_quote(token, data: dict, address: str, spend_raw: int):
    spend_raw = _ftw_budget(data, spend_raw)
    if spend_raw <= 0:
        return None
    buying = data["way"] == "buy"
    if data.get("dexk") == "flam":
        return await asyncio.wait_for(
            flamingo.quote(OPS["neo_n3"].rpc, ftwswap.script_hash(address), token.contract, token.symbol, token.decimals, spend_raw,
                           Decimal(data.get("slip", str(dexquote.DEFAULT_SLIPPAGE))), buying), 45)
    t_in, t_out = (ftwswap.GAS_HASH, normalize_hash_safe(token.contract)) if buying else (normalize_hash_safe(token.contract), ftwswap.GAS_HASH)
    sym_in, sym_out = ("GAS", token.symbol) if buying else (token.symbol, "GAS")
    dec_in, dec_out = (8, token.decimals) if buying else (token.decimals, 8)
    return await asyncio.wait_for(
        ftwswap.quote(OPS["neo_n3"].rpc, ftwswap.script_hash(address), t_in, t_out, sym_in, sym_out, dec_in, dec_out, spend_raw,
                      Decimal(data.get("slip", str(dexquote.DEFAULT_SLIPPAGE))), buying), 45)


async def _ftw_show_quote(event, state: FSMContext) -> None:
    data = await state.get_data()
    token = await _ftw_token(event, int(data["token_id"]))
    if token is None:
        return await _show(event, "That token is not on your list any more.", home_only())
    way, slip = data["way"], str(Decimal(data.get("slip", str(dexquote.DEFAULT_SLIPPAGE))))
    dexk = data.get("dexk", "ftw")
    spend_raw = int(data["spend_raw"])
    try:
        address = await _ftw_wallet(event)
        q = await _ftw_make_quote(token, data, address, spend_raw)
    except Exception as exc:
        log.warning("FTW quote failed: %r", exc)
        return await _show(event, "I couldn't reach the network just now. Please try again in a moment.", _ftw_keyboard(token, slip, None, way, None, dexk))
    if q is None:
        return await _show(event, f"{_DEX_NAMES[dexk]} has no route for this token and GAS, or the pool is too small for that amount. "
                                  "Try a smaller amount.", _ftw_keyboard(token, slip, None, way, None, dexk))
    try:
        live = await asyncio.wait_for(PRICES.prices(), 6)
    except Exception:
        live = {}
    token_usd = None
    try:
        token_usd = await asyncio.wait_for(PRICES.price_of(token, "neo_n3"), 6)
    except Exception:
        pass
    gas_usd = live.get("GAS")
    in_usd, out_usd = (gas_usd, token_usd) if q.buying else (token_usd, gas_usd)
    budget = _ftw_budget(data, spend_raw)
    note = ""
    nonce = secrets.token_hex(4)
    await state.update_data(nonce=nonce, preview_out=str(q.amount_out_raw), impact=str(q.impact_pct),
                            give=f"{dexquote._num(q.pay)} {q.in_symbol}", get=f"{dexquote._num(q.receive)} {q.out_symbol}")
    extra = ("\n\nPrices move: if the price changes too much before you confirm, I will stop and show a new quote. "
             "Some tokens charge their own tax on trades; it is not in this quote. Not financial advice.")
    body = (flamingo.render_quote(q, Decimal(slip), in_usd, out_usd) if dexk == "flam"
            else ftwswap.render_quote(q, Decimal(slip), in_usd, out_usd, budget))
    await _show(event, (body + note + extra)[:4000], _ftw_keyboard(token, slip, nonce, way, q.impact_pct, dexk))


async def _ftw_quote_or_explain(event, state: FSMContext) -> None:
    try:
        await _ftw_show_quote(event, state)
    except Exception:
        log.exception("Showing the FTW quote failed")
        try:
            msg = "I couldn't show that quote just now. Please try again, or tap /cancel."
            await (event.message.answer(msg) if isinstance(event, CallbackQuery) else event.answer(msg))
        except Exception:
            log.exception("Could not even send the error message")


@router.message(FtwFlow.amount, F.text)
async def ftw_amount(message: Message, state: FSMContext):
    text = (message.text or "").strip().replace(",", ".")
    data = await state.get_data()
    token = await _ftw_token(message, int(data.get("token_id", 0) or 0))
    if token is None:
        await state.clear()
        return await message.answer("That token is not on your list any more.", reply_markup=home_only())
    dec = 8 if data["way"] == "buy" else token.decimals
    try:
        if text.lower() == "max":
            return await _ftw_use_share(message, state, 100)
        amount = Decimal(text)
        spend_raw = int(amount * (Decimal(10) ** dec))
        if amount <= 0 or amount > Decimal("1e15") or spend_raw <= 0:
            raise ValueError
    except Exception:
        return await message.answer("Please type a number bigger than zero, like 0.5")
    if dec == 0 and amount != spend_raw:
        return await message.answer(f"{html.escape(token.symbol)} can only be traded in whole units. Please type a whole number, like 1.")
    await state.update_data(spend_raw=str(spend_raw))
    try:
        await message.bot.send_chat_action(message.chat.id, "typing")
    except Exception:
        pass
    await _ftw_quote_or_explain(message, state)


async def _ftw_use_share(event, state: FSMContext, percent: int) -> None:
    data = await state.get_data()
    raw = data.get("bal_raw")
    reply = (lambda m: event.message.answer(m)) if isinstance(event, CallbackQuery) else (lambda m: event.answer(m))
    if not raw:
        return await reply("I don't know your balance, so please type an amount.")
    spend_raw = int(raw) * percent // 100
    if spend_raw <= 0:
        return await reply("That share is too small to trade. Try a bigger one.")
    await state.update_data(spend_raw=str(spend_raw))
    await _ftw_quote_or_explain(event, state)


@router.callback_query(F.data.startswith("fa:"), FtwFlow.amount)
async def ftw_share(cb: CallbackQuery, state: FSMContext):
    try:
        percent = int(cb.data.split(":", 1)[1])
        if percent not in (25, 50, 75, 100):
            raise ValueError
    except ValueError:
        return await _done(cb)
    await _done(cb)
    await _ftw_use_share(cb, state, percent)


@router.callback_query(F.data.startswith("fs:"), FtwFlow.amount)
async def ftw_slippage(cb: CallbackQuery, state: FSMContext):
    try:
        slip = Decimal(cb.data.split(":", 1)[1])
    except Exception:
        return await _done(cb)
    if slip not in dexquote.SLIPPAGE_CHOICES or "spend_raw" not in (await state.get_data()):
        return await _done(cb)
    await state.update_data(slip=str(slip))
    await _done(cb)
    await _ftw_quote_or_explain(cb, state)


@router.callback_query(F.data.startswith("fok:"))
async def ftw_confirm(cb: CallbackQuery, state: FSMContext):
    """✅ Confirm trade: a fresh quote, the node's dry run, then the swap itself."""
    if not _private(cb):
        return await cb.answer(PRIVATE_ONLY, show_alert=True)
    data = await state.get_data()
    if not data or data.get("nonce") != cb.data.split(":", 1)[1]:
        return await cb.answer("This confirmation has expired. Start the trade again.", show_alert=True)
    await state.clear()
    await cb.answer()
    token = await _ftw_token(cb, int(data["token_id"]))
    ops = OPS.get("neo_n3")
    if token is None or ops is None:
        return await cb.message.edit_text("That token or network is not available right now.", reply_markup=home_only())
    lock = send_lock(cb.from_user.id, "neo_n3")
    if lock.locked():
        return await cb.message.edit_text("Another transaction on this network is still in progress. Please wait.")
    async with lock:
        await cb.message.edit_text("⏳ Checking the price…")
        try:
            address = await _ftw_wallet(cb)
            try:
                fresh = await _ftw_make_quote(token, data, address, int(data["spend_raw"]))
            except Exception as first:
                log.warning("Fresh N3 quote failed once (%r), retrying", first)
                await asyncio.sleep(1.5)
                fresh = await _ftw_make_quote(token, data, address, int(data["spend_raw"]))
        except Exception as exc:
            log.exception("Fresh N3 quote at confirm failed")
            return await cb.message.edit_text(
                f"❌ I couldn't reach the network ({html.escape(type(exc).__name__)}). Nothing was sent. Please try again.",
                reply_markup=home_only())
        if fresh is None:
            return await cb.message.edit_text("❌ The pool can't fill that amount any more. Nothing was sent.", reply_markup=home_only())
        slip = Decimal(data.get("slip", str(dexquote.DEFAULT_SLIPPAGE)))
        seen = int(data.get("preview_out", "0"))
        if seen and fresh.amount_out_raw < dexquote.min_out(seen, slip):
            # the price got worse than the person agreed to: show the new quote instead of trading
            await state.set_state(FtwFlow.amount)
            await state.update_data(**{k: v for k, v in data.items() if k not in ("nonce", "preview_out")})
            await cb.message.answer("⚠️ The price moved while you were deciding, so I did not trade. Here is the new quote.")
            return await _ftw_show_quote(cb.message, state)
        await cb.message.edit_text("⏳ Trading… This can take a minute.")
        session, user = await _load(cb)
        async with session:
            chain = await service.get_chain(session, "neo_n3")
            gas_token = next((t for t in await service.visible_tokens(session, user, chain.id)
                              if normalize_hash_safe(t.contract) == ftwswap.GAS_HASH), None)
            paid = gas_token if fresh.buying else token
            if paid is None:
                return await cb.message.edit_text("❌ I could not find GAS in your wallet's token list.", reply_markup=home_only())
            row = await service.record_outgoing(session, user, paid, "swap", fresh.amount_in_raw,
                                                f"swap on {_DEX_NAMES[data.get('dexk', 'ftw')]}: {fresh.pay} {fresh.in_symbol} for {fresh.out_symbol}")
            private_key = await service.get_private_key(session, user, SETTINGS.master_key, ops.key_type)
            try:
                tx_hash = await (flamingo if data.get("dexk") == "flam" else ftwswap).swap(SETTINGS.neo_n3_rpc, private_key, fresh)
            except neo_send.SendError as exc:
                row.status = "failed"
                row.note = f"{row.note} | {str(exc)[:100]}"[:250]
                await session.commit()
                return await cb.message.edit_text(f"❌ {html.escape(str(exc))}", reply_markup=home_only())
            except Exception:
                log.exception("FTW swap failed before it was sent")
                row.status = "failed"
                await session.commit()
                return await cb.message.edit_text("❌ Something went wrong before the swap was sent. Nothing was traded.",
                                                  reply_markup=home_only())
        outcome = await neo_send.wait_for_outcome(ops.rpc, tx_hash, timeout=90.0, expect_true=False)
        try:
            row.tx_hash, row.status = outcome.tx_hash, outcome.status
            await session.commit()
        except Exception:
            log.exception("Could not save the FTW swap result in History")
    tx = historyview.tx_html(SETTINGS.network, "neo_n3", outcome.tx_hash)
    if outcome.status == "confirmed":
        trade_fee = None
        if data.get("dexk") == "flam":
            paid_seen, got_seen, fees = await flamingo.outcome_details(ops.rpc, outcome.tx_hash, fresh, address)
            paid_raw, got_raw = paid_seen or fresh.amount_in_raw, got_seen or fresh.amount_out_raw
        else:
            done, fees = await ftwswap.outcome_details(ops.rpc, outcome.tx_hash)
            paid_raw = done.amount_in if done else fresh.amount_in_raw
            got_raw = done.amount_out if done else fresh.amount_out_raw
            trade_fee = done.fee if done and done.fee else None
        dex_name = _DEX_NAMES[data.get("dexk", "ftw")]
        dec_in, dec_out = fresh.in_decimals, fresh.out_decimals
        lines = [f"✅ <b>Trade done</b> on {dex_name} (Neo N3)",
                 f"📤 Sent: <b>{dexquote._num(Decimal(paid_raw) / Decimal(10) ** dec_in)} {html.escape(fresh.in_symbol)}</b>",
                 f"📥 Received: <b>{dexquote._num(Decimal(got_raw) / Decimal(10) ** dec_out)} {html.escape(fresh.out_symbol)}</b>"]
        if trade_fee:
            lines.append(f"Trade fee: {dexquote._num(Decimal(trade_fee) / Decimal(10) ** dec_in)} {html.escape(fresh.in_symbol)}")
        if fees:
            try:
                usd = (await asyncio.wait_for(PRICES.prices(), 6)).get("GAS")
            except Exception:
                usd = None
            coin = Decimal(fees) / Decimal(10 ** 8)
            lines.append(f"⛽ Network fee paid: <b>{dexquote._num(coin)} GAS</b>{dexquote._usd(coin, usd)}")
        text = "\n".join(lines) + f"\n{tx}"
    elif outcome.status == "failed":
        text = f"❌ The swap failed on-chain ({html.escape(outcome.detail)}). Nothing was traded; a small GAS fee may have been used.\n{tx}"
    else:
        text = "⚠️ The swap was sent but I could not confirm it yet. <b>Do not trade again</b> until you check your balance.\n" + tx
    try:
        await cb.message.edit_text(text, reply_markup=home_only())
    except Exception:
        log.exception("Could not edit the FTW result message; sending it as a new one")
        try:
            await cb.message.answer(text, reply_markup=home_only())
        except Exception:
            await cb.message.answer(re.sub(r"<[^>]+>", "", text))


async def _balance_of(ops, address: str, token) -> int | None:
    """The raw balance of one token, or None if it can't be read."""
    try:
        got = await asyncio.wait_for(ops.balances(address, [token]), 10)
        value = got.get(ops.token_key(token))
        return None if value is None else int(value)
    except Exception:
        return None


async def _fees_paid(rpc, hashes: list[str]) -> int:
    """Network fees (in the coin's smallest unit) that these transactions cost; 0 if it can't be worked out."""
    total = 0
    for h in hashes:
        try:
            r = await asyncio.wait_for(rpc.get_receipt(h), 8)
            used = int(str(r["gasUsed"]), 16)
            price = int(str(r.get("effectiveGasPrice") or "0x0"), 16)
            if not price:
                tx = await asyncio.wait_for(rpc.call("eth_getTransactionByHash", [h]), 8)
                price = int(str(tx["gasPrice"]), 16)
            total += used * price
        except Exception:
            return 0
    return total


async def _bridge_ctx(session, user, direction: str) -> BridgeCtx:
    n3, x = OPS["neo_n3"], OPS["neo_x"]
    n3w = await service.get_wallet(session, user, "neo_n3")
    xw = await service.get_wallet(session, user, "evm")
    try:
        if direction == "n3x":
            limits = await bridge.read_n3_limits(n3.rpc, BRIDGE_N3_HASH)
            balance = (await n3.rpc.get_balances(n3w.address)).get(neo_send.GAS_HASH, 0)
            chain = await service.get_chain(session, "neo_n3")
            token = await service.find_token(session, chain.id, neo_send.GAS_HASH)
            return BridgeCtx(direction, limits, balance, token, n3w, xw)
        limits = await bridge.read_neox_limits(x.rpc)
        balance = await x.rpc.get_balance(xw.address)
        chain = await service.get_chain(session, "neo_x")
        token = await service.native_token(session, chain.id)
        return BridgeCtx(direction, limits, balance, token, xw, n3w)
    except neo_send.SendError:
        raise
    except Exception:
        log.exception("Bridge context failed")
        raise neo_send.SendError("I couldn't reach the network to prepare the bridge. Try again in a moment.")


async def _bridge_plan(ctx: BridgeCtx, amount8: int | None) -> tuple[bridge.Plan, int]:
    """Plan for an amount (None = the most that can be bridged). Returns (plan, network fee estimate)."""
    x = OPS["neo_x"]
    if ctx.direction == "n3x":
        if amount8 is None:
            amount8 = bridge.max_amount8_n3_to_x(ctx.limits, ctx.balance)
            if amount8 <= 0:
                raise neo_send.SendError("You have nothing bridgeable (the bridge fee and network fees come out of your GAS).")
        return bridge.plan_n3_to_x(amount8, ctx.limits, ctx.balance), bridge.MIN_GAS_RESERVE
    if amount8 is None:
        probe = max(1, -(-ctx.limits.min_amount // bridge.SCALE))  # smallest valid amount, to price the fee
        q = await bridge.quote_x_to_n3(x.rpc, ctx.src_wallet.address, ctx.dst_wallet.address, probe, ctx.limits.fee)
        amount8 = bridge.max_amount8_x_to_n3(ctx.limits, ctx.balance, q.fee)
        if amount8 <= 0:
            raise neo_send.SendError("You have nothing bridgeable (the bridge fee and network fees come out of your GAS).")
    # Friendly limit/balance errors first: the network dry run below would only say "rejected".
    bridge.plan_x_to_n3(amount8, ctx.limits, ctx.balance, 0)
    q = await bridge.quote_x_to_n3(x.rpc, ctx.src_wallet.address, ctx.dst_wallet.address, amount8, ctx.limits.fee)
    return bridge.plan_x_to_n3(amount8, ctx.limits, ctx.balance, q.fee), q.fee


@router.callback_query(F.data == "bridge")
async def bridge_start(event: Message | CallbackQuery, state: FSMContext):
    await state.clear()
    if not _private(event):
        return await _done(event, PRIVATE_ONLY, alert=True)
    if _xbridge_available():
        return await _xb_pick_source(event, state)
    if not _bridge_available():
        return await _done(event, "The bridge needs both Neo N3 and Neo X to be set up on this bot.", alert=True)
    await _show(
        event,
        "<b>Bridge GAS</b>\nMove GAS between your own Neo N3 and Neo X wallets using the official Neo bridge.\n"
        "Only GAS can be bridged.",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Neo N3 ➜ Neo X", callback_data="brdir:n3x")],
                [InlineKeyboardButton(text="Neo X ➜ Neo N3", callback_data="brdir:xn3")],
                BACK,
            ]
        ),
    )
    await _done(event)


@router.callback_query(F.data.startswith("brdir:"))
async def bridge_direction(cb: CallbackQuery, state: FSMContext):
    if not _private(cb):
        return await cb.answer(PRIVATE_ONLY, show_alert=True)
    direction = cb.data.split(":", 1)[1]
    if direction not in DIRECTION_LABEL or not _bridge_available():
        return await cb.answer("Not available.", show_alert=True)
    await cb.answer()
    session, user = await _load(cb)
    async with session:
        try:
            ctx = await _bridge_ctx(session, user, direction)
        except neo_send.SendError as exc:
            return await cb.message.edit_text(f"❌ {html.escape(str(exc))}", reply_markup=home_only())
    src, dst = DIRECTION_LABEL[direction]
    dec = 8 if direction == "n3x" else 18
    fmt = lambda v: service.format_amount(v, dec)  # noqa: E731
    await state.set_state(BridgeFlow.amount)
    await state.update_data(direction=direction)
    await cb.message.edit_text(
        f"<b>Bridge GAS: {src} ➜ {dst}</b>\n"
        f"Your GAS on {src}: {fmt(ctx.balance)}\n"
        f"Bridge fee: {fmt(ctx.limits.fee)} GAS (on top of the amount)\n"
        f"Minimum: {fmt(ctx.limits.min_amount)} GAS · Maximum: {fmt(ctx.limits.max_amount) if ctx.limits.max_amount else 'none'}\n\n"
        "How much GAS do you want to move? Send a number (for example 5), or <b>max</b>.\n/cancel to stop."
    )


@router.message(BridgeFlow.amount, F.text)
async def bridge_amount(message: Message, state: FSMContext):
    if not _private(message):
        return
    data = await state.get_data()
    direction = data["direction"]
    session, user = await _load(message)
    async with session:
        try:
            ctx = await _bridge_ctx(session, user, direction)
            if message.text.strip().lower() == "max":
                amount8 = None
            else:
                amount8 = neo_send.parse_amount(message.text, 8)
            plan, net_fee = await _bridge_plan(ctx, amount8)
        except neo_send.SendError as exc:
            return await message.answer(f"❌ {html.escape(str(exc))}\nTry another amount or /cancel.")
    src, dst = DIRECTION_LABEL[direction]
    src_dec = 8 if direction == "n3x" else 18
    nonce = secrets.token_hex(8)
    await state.update_data(amount8=plan.amount8, nonce=nonce)
    await message.answer(
        "<b>Please confirm</b>\n"
        f"Bridge: <b>{service.format_amount(plan.amount8, 8)} GAS</b> from {src} to {dst}\n"
        f"You receive: {service.format_amount(plan.amount8, 8)} GAS on {dst}, in your own wallet "
        f"(<code>{_short(ctx.dst_wallet.address)}</code>)\n"
        f"Bridge fee: {service.format_amount(plan.fee, src_dec)} GAS, paid on {src}\n"
        f"Network fee on {src}: about {service.format_amount(net_fee, src_dec)} GAS\n"
        f"Arrival: {bridge.ARRIVAL_HINT} after the transaction confirms. I'll message you when it lands.\n\n"
        "Bridge transfers cannot be reversed.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✅ Confirm bridge", callback_data=f"brok:{nonce}")],
                [InlineKeyboardButton(text="❌ Cancel", callback_data="home")],
            ]
        ),
    )


@router.callback_query(F.data.startswith("brok:"))
async def bridge_confirm(cb: CallbackQuery, state: FSMContext):
    if not _private(cb):
        return await cb.answer(PRIVATE_ONLY, show_alert=True)
    data = await state.get_data()
    if not data or data.get("nonce") != cb.data.split(":", 1)[1]:
        return await cb.answer("This confirmation has expired.", show_alert=True)
    await state.clear()  # single use
    await cb.answer()
    if not _bridge_available():
        return await cb.message.edit_text("The bridge is not available right now.", reply_markup=home_only())
    lock = send_lock(cb.from_user.id, "neo_n3", "neo_x")
    if lock.locked():
        return await cb.message.edit_text("Another send on Neo N3 or Neo X is still in progress. Please wait.")
    direction = data["direction"]
    src, dst = DIRECTION_LABEL[direction]
    async with lock:
        await cb.message.edit_text("⏳ Bridging… this can take a couple of minutes.")
        session, user = await _load(cb)
        async with session:
            try:
                ctx = await _bridge_ctx(session, user, direction)  # fresh limits and balance
                plan, _ = await _bridge_plan(ctx, data["amount8"])
            except neo_send.SendError as exc:
                return await cb.message.edit_text(f"❌ {html.escape(str(exc))}", reply_markup=home_only())
            src_raw = plan.amount8 if direction == "n3x" else plan.amount8 * bridge.SCALE
            row = await service.record_outgoing(
                session, user, ctx.token, "bridge_out", src_raw, f"bridge {src} to {dst}"
            )
            key_type = "neo_n3" if direction == "n3x" else "evm"
            private_key = await service.get_private_key(session, user, SETTINGS.master_key, key_type)
            try:
                if direction == "n3x":
                    outcome = await bridge.run_n3_to_x(
                        neo_rpc=OPS["neo_n3"].rpc, depositor=DEPOSITOR, bridge_hash=BRIDGE_N3_HASH,
                        private_key=private_key, to_neox_address=ctx.dst_wallet.address, plan=plan,
                    )
                else:
                    outcome = await bridge.run_x_to_n3(
                        x_rpc=OPS["neo_x"].rpc, chain_id=OPS["neo_x"].cfg.chain_id, private_key=private_key,
                        from_addr=ctx.src_wallet.address, to_n3_address=ctx.dst_wallet.address, plan=plan,
                    )
            except neo_send.SendError as exc:
                row.status = "failed"
                row.note = f"{row.note} | {str(exc)[:100]}"[:250]
                await session.commit()
                return await cb.message.edit_text(f"❌ {html.escape(str(exc))}", reply_markup=home_only())
            except Exception:
                log.exception("Bridge failed before broadcast")
                row.status = "failed"
                await session.commit()
                return await cb.message.edit_text(
                    "❌ Something went wrong before the transaction was sent. Nothing was sent.",
                    reply_markup=home_only(),
                )
            row.tx_hash = outcome.tx_hash
            row.status = outcome.status
            await session.commit()
    amount = service.format_amount(plan.amount8, 8)
    tx = historyview.tx_html(SETTINGS.network, "neo_n3" if direction == "n3x" else "neo_x", outcome.tx_hash)
    if outcome.status == "confirmed":
        text = (
            f"✅ <b>Bridge started</b>: {amount} GAS left {src}.\n"
            f"It should arrive on {dst} {bridge.ARRIVAL_HINT}; I'll message you when it lands.\n{tx}"
        )
    elif outcome.status == "failed":
        text = (
            f"❌ The bridge transaction failed on-chain ({html.escape(outcome.detail)}). "
            f"A small network fee may have been used.\n{tx}"
        )
    else:
        text = (
            "⚠️ The transaction was sent but I could not confirm it yet. "
            f"<b>Do not bridge again</b> until you check your balances.\n{tx}"
        )
    await cb.message.edit_text(text, reply_markup=home_only())


# ------------------------------------------------- bridge between many networks (through LI.FI)
class XBridgeFlow(StatesGroup):
    amount = State()  # typing how much to bridge


_XB_TASKS: set = set()  # keeps the "did it arrive?" watchers alive
_XB_PAIR = ("neo_n3", "neo_x")  # these two have their own official bridge (GAS only)


def _xb_http():
    import aiohttp
    return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30), connector=aiohttp.TCPConnector(ssl=tls.context()))


def _xbridge_available() -> bool:
    evm = [s for s, o in OPS.items() if o.key_type == "evm"]
    return len(evm) >= 2 or ("solana" in OPS and len(evm) >= 1)


def _any_bridge() -> bool:
    return _bridge_available() or _xbridge_available()


def _xb_chain_id(slug: str, solana: dict | None):
    ops = OPS[slug]
    if ops.key_type == "evm":
        return ops.cfg.chain_id
    if ops.key_type == "solana":
        return (solana or {}).get("id", xbridge.SOLANA_CHAIN_ID)
    return None


def _xb_emoji(slug: str) -> str:
    return homeview.CHAIN_EMOJI.get(slug, homeview.DEFAULT_EMOJI)


async def _xb_sources() -> list[str]:
    """Networks a person can start a bridge from (Solana as a starting network is not available yet)."""
    async with _xb_http() as http:
        ids, solana = await xbridge.fetch_supported(http)
    out = []
    for slug, ops in OPS.items():
        if ops.key_type == "solana":
            if xbridge.relay_enabled():  # Solana as a starting network goes through Relay
                out.append(slug)
            continue
        if (slug in _XB_PAIR and _bridge_available()) or (ops.key_type == "evm" and _xb_chain_id(slug, solana) in ids):
            out.append(slug)
    return out


async def _xb_targets(src: str) -> list[str]:
    """Networks one can bridge to from `src`."""
    async with _xb_http() as http:
        ids, solana = await xbridge.fetch_supported(http)
    out: list[str] = []
    if src in _XB_PAIR and _bridge_available():
        out.append("neo_x" if src == "neo_n3" else "neo_n3")
    if OPS[src].key_type == "solana" and xbridge.relay_enabled():
        return [slug for slug, o in OPS.items() if o.key_type == "evm" and _xb_chain_id(slug, solana) in ids]
    if OPS[src].key_type == "evm" and _xb_chain_id(src, solana) in ids:
        for slug in OPS:
            cid = _xb_chain_id(slug, solana)
            if slug != src and slug not in out and cid is not None and cid in ids:
                out.append(slug)
    return out


def _xb_grid(buttons: list) -> list[list]:
    return [buttons[i:i + 2] for i in range(0, len(buttons), 2)]


async def _xb_pick_source(event, state: FSMContext) -> None:
    await state.clear()
    if not _private(event):
        return await _done(event, PRIVATE_ONLY, alert=True)
    if not _any_bridge():
        return await _done(event, "Bridging isn't set up on this bot.", alert=True)
    await _done(event)
    sources = await _xb_sources()
    if not sources:
        return await _show(event, "No bridge is available right now.", home_only())
    buttons = [InlineKeyboardButton(text=f"{_xb_emoji(s)} {OPS[s].name}", callback_data=f"xbs:{s}") for s in sources]
    await _show(
        event,
        "🌉 <b>Bridge</b>\n\nMove a coin or token from one of your wallets to another network.\n\n"
        "<b>Step 1.</b> Which network do you want to move it <b>from</b>?\n"
        + ("" if xbridge.relay_enabled() else "<i>Bridging from Solana needs Relay to be switched on.</i>"),
        InlineKeyboardMarkup(inline_keyboard=_xb_grid(buttons) + [BACK]),
    )


@router.callback_query(F.data == "xb")
async def xb_start(cb: CallbackQuery, state: FSMContext):
    await _xb_pick_source(cb, state)


@router.callback_query(F.data.startswith("xbs:"))
async def xb_source(cb: CallbackQuery, state: FSMContext):
    if not _private(cb):
        return await cb.answer(PRIVATE_ONLY, show_alert=True)
    src = cb.data.split(":", 1)[1]
    if src not in OPS:
        return await cb.answer("Not available.", show_alert=True)
    await state.clear()
    await cb.answer()
    try:
        targets = await _xb_targets(src)
    except Exception:
        log.exception("Bridge targets failed")
        targets = []
    if not targets:
        return await cb.message.edit_text(f"No bridge is available from {html.escape(OPS[src].name)} right now.",
                                          reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                              [InlineKeyboardButton(text="⬅️ Back", callback_data="xb")]]))
    buttons = [InlineKeyboardButton(text=f"{_xb_emoji(t)} {OPS[t].name}", callback_data=f"xbd:{src}:{t}") for t in targets]
    note = ""
    if src in _XB_PAIR and _bridge_available():
        other = "Neo X" if src == "neo_n3" else "Neo N3"
        note = f"\n<i>{other} uses the official Neo bridge (GAS only).</i>"
    await cb.message.edit_text(
        f"🌉 <b>From {html.escape(OPS[src].name)}</b>\n\n<b>Step 2.</b> Which network do you want to move it <b>to</b>?{note}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=_xb_grid(buttons) + [[InlineKeyboardButton(text="⬅️ Back", callback_data="xb")]]))


async def _xb_wallet_tokens(event, src: str):
    """[(token, raw balance)] of the person's tokens on `src` that they hold (verified decimals only), plus the wallet address."""
    ops = OPS[src]
    session, user = await _load(event)
    async with session:
        chain = await service.get_chain(session, src)
        wallet = await service.get_wallet(session, user, ops.key_type)
        tokens = [t for t in await service.visible_tokens(session, user, chain.id) if t.decimals_verified]
    bals = await asyncio.wait_for(ops.balances(wallet.address, tokens), 15)
    held = []
    for t in tokens:
        raw = bals.get(ops.token_key(t))
        if raw:
            held.append((t, int(raw)))
    return held, wallet.address


@router.callback_query(F.data.startswith("xbd:"))
async def xb_destination(cb: CallbackQuery, state: FSMContext):
    if not _private(cb):
        return await cb.answer(PRIVATE_ONLY, show_alert=True)
    try:
        _, src, dst = cb.data.split(":")
        assert src in OPS and dst in OPS and src != dst
    except Exception:
        return await cb.answer("Not available.", show_alert=True)
    if {src, dst} == set(_XB_PAIR):  # Neo N3 <-> Neo X: the official bridge, which moves GAS
        return await bridge_direction(cb.model_copy(update={"data": "brdir:" + ("n3x" if src == "neo_n3" else "xn3")}), state)
    await state.clear()
    await cb.answer()
    try:
        held, _addr = await _xb_wallet_tokens(cb, src)
    except Exception:
        log.exception("Bridge token list failed")
        return await cb.message.edit_text("I couldn't read your balances just now. Please try again in a moment.", reply_markup=home_only())
    if not held:
        return await cb.message.edit_text(f"You have nothing to bridge on {html.escape(OPS[src].name)} yet.", reply_markup=home_only())
    buttons = [InlineKeyboardButton(text=f"{t.symbol} · {dexquote._num(Decimal(raw) / (Decimal(10) ** t.decimals))}",
                                    callback_data=f"xbt:{src}:{dst}:{t.id}") for t, raw in held[:12]]
    await cb.message.edit_text(
        f"🌉 <b>{html.escape(OPS[src].name)} ➜ {html.escape(OPS[dst].name)}</b>\n\n<b>Step 3.</b> Which coin or token do you want to bridge?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=_xb_grid(buttons) + [
            [InlineKeyboardButton(text="⬅️ Back", callback_data=f"xbs:{src}")]]))


async def _xb_spendable(src: str, token, raw: int) -> int:
    """What can be bridged: the whole token balance, or the coin balance minus room for the network fee."""
    if str(token.contract).lower() != "native":
        return raw
    if OPS[src].key_type == "solana":
        return max(0, raw - 10_000_000)  # keep 0.01 SOL for fees and account rent
    try:
        price = await asyncio.wait_for(OPS[src].rpc.gas_price(), 6)
    except Exception:
        price = 0
    return max(0, raw - 700_000 * price)


@router.callback_query(F.data.startswith("xbt:"))
async def xb_token(cb: CallbackQuery, state: FSMContext):
    if not _private(cb):
        return await cb.answer(PRIVATE_ONLY, show_alert=True)
    try:
        _, src, dst, token_id = cb.data.split(":")
        token_id = int(token_id)
        assert src in OPS and dst in OPS
    except Exception:
        return await cb.answer("Not available.", show_alert=True)
    await cb.answer()
    try:
        held, _addr = await _xb_wallet_tokens(cb, src)
    except Exception:
        return await cb.message.edit_text("I couldn't read your balances just now. Please try again in a moment.", reply_markup=home_only())
    pick = next(((t, raw) for t, raw in held if t.id == token_id), None)
    if pick is None:
        return await cb.message.edit_text("You don't hold that token on this network any more.", reply_markup=home_only())
    token, raw = pick
    spendable = await _xb_spendable(src, token, raw)
    await state.set_state(XBridgeFlow.amount)
    await state.update_data(src=src, dst=dst, token_id=token.id, bal_raw=str(spendable))
    have = dexquote._num(Decimal(spendable) / (Decimal(10) ** token.decimals))
    rows = [[InlineKeyboardButton(text=t, callback_data=f"xba:{v}") for t, v in
             (("25%", 25), ("50%", 50), ("75%", 75), ("Max", 100))]] if spendable else []
    rows.append([InlineKeyboardButton(text="⬅️ Back", callback_data=f"xbd:{src}:{dst}")])
    await cb.message.edit_text(
        f"🌉 <b>{html.escape(OPS[src].name)} ➜ {html.escape(OPS[dst].name)}</b> · {html.escape(token.symbol)}\n\n"
        f"Your balance: <b>{have} {html.escape(token.symbol)}</b>"
        + (" (a little is kept for the network fee)" if str(token.contract).lower() == "native" else "")
        + "\n\n<b>Step 4.</b> Tap a share, or type how much you want to bridge, for example 0.5.\n"
        "You will see the available bridges and their fees before anything is sent.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


async def _xb_token_of(event, src: str, token_id: int):
    session, user = await _load(event)
    async with session:
        chain = await service.get_chain(session, src)
        for t in await service.visible_tokens(session, user, chain.id):
            if t.id == token_id:
                return t
    return None


@router.message(XBridgeFlow.amount, F.text)
async def xb_amount(message: Message, state: FSMContext):
    if not _private(message):
        return
    data = await state.get_data()
    token = await _xb_token_of(message, data.get("src", ""), int(data.get("token_id", 0) or 0)) if data else None
    if token is None:
        await state.clear()
        return await message.answer("That token is not on your list any more.", reply_markup=home_only())
    text = (message.text or "").strip().replace(",", ".")
    try:
        if text.lower() == "max":
            raw = int(data["bal_raw"])
        else:
            amount = Decimal(text)
            raw = int(amount * (Decimal(10) ** token.decimals))
            if amount <= 0 or amount > Decimal("1e15") or raw <= 0:
                raise ValueError
    except Exception:
        return await message.answer("Please type a number bigger than zero, like 0.5")
    if raw > int(data.get("bal_raw", "0") or 0):
        return await message.answer("That is more than you can bridge. Please type a smaller amount or tap Max.")
    await state.update_data(spend_raw=str(raw))
    await _xb_show_routes(message, state)


@router.callback_query(F.data.startswith("xba:"), XBridgeFlow.amount)
async def xb_share(cb: CallbackQuery, state: FSMContext):
    try:
        percent = int(cb.data.split(":")[1])
        assert 1 <= percent <= 100
    except Exception:
        return await cb.answer()
    data = await state.get_data()
    raw = int(data.get("bal_raw", "0") or 0) * percent // 100
    if raw <= 0:
        return await cb.answer("That share is too small.", show_alert=True)
    await cb.answer()
    await state.update_data(spend_raw=str(raw))
    await _xb_show_routes(cb, state)


def _xb_options_keyboard(data: dict) -> InlineKeyboardMarkup:
    rows = []
    for o in data["options"]:
        rows.append([InlineKeyboardButton(text=o["label"][:60], callback_data=f"xbr:{o['i']}")])
    rows.append([InlineKeyboardButton(text="✏️ Another amount", callback_data=f"xbt:{data['src']}:{data['dst']}:{data['token_id']}")])
    rows.append([InlineKeyboardButton(text="⬅️ Back", callback_data=f"xbd:{data['src']}:{data['dst']}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _xb_show_routes(event, state: FSMContext) -> None:
    data = await state.get_data()
    src, dst = data["src"], data["dst"]
    token = await _xb_token_of(event, src, int(data["token_id"]))
    if token is None:
        await state.clear()
        return await _show(event, "That token is not on your list any more.", home_only())
    spend_raw = int(data["spend_raw"])
    await _show(event, "🔎 Looking for the best bridges…", None)
    probe_ctx = None
    try:
        async with _xb_http() as http:
            ids, solana = await xbridge.fetch_supported(http)
            from_cid, to_cid = _xb_chain_id(src, solana), _xb_chain_id(dst, solana)
            native_in = str(token.contract).lower() == "native"
            session, user = await _load(event)
            async with session:
                src_wallet = await service.get_wallet(session, user, OPS[src].key_type)
                dst_wallet = await service.get_wallet(session, user, OPS[dst].key_type)
            if dst_wallet is None:
                return await _show(event, "You have no wallet on that network yet.", home_only())
            if native_in:
                to_token = (solana or {}).get("native", xbridge.SOLANA_NATIVE) if OPS[dst].key_type == "solana" else xbridge.NATIVE_TOKEN
            else:
                info = await xbridge.fetch_token(http, to_cid, token.symbol)
                if info is None:
                    return await _show(event, f"{html.escape(token.symbol)} isn't available on {html.escape(OPS[dst].name)} through "
                                              "the bridges I can use. Try another token.", _xb_options_keyboard(
                                                  {"options": [], "src": src, "dst": dst, "token_id": token.id}))
                to_token = info["address"]
            sol_src = OPS[src].key_type == "solana"
            probe_ctx = None  # set below once everything needed to ask again is known
            from_token = (xbridge.SOLANA_NATIVE if sol_src else xbridge.NATIVE_TOKEN) if native_in else token.contract
            probe_ctx = dict(src_wallet=src_wallet.address, dst_wallet=dst_wallet.address, from_cid=from_cid, to_cid=to_cid,
                             from_token=from_token, to_token=to_token, sol_src=sol_src, src=src, dst=dst)
            lifi_error = None
            try:
                if sol_src:
                    raise xbridge.XBridgeError("Bridging from Solana needs Relay, and it is not available for this route.")
                options = await xbridge.fetch_routes(
                    http, wallet=src_wallet.address, from_chain_id=from_cid, to_chain_id=to_cid, from_token=from_token,
                    to_token=to_token, amount_in=spend_raw, to_address=dst_wallet.address)
            except xbridge.XBridgeError as exc:
                options, lifi_error = [], exc
            if xbridge.relay_enabled() and OPS[src].key_type in ("evm", "solana") and OPS[dst].key_type in ("evm", "solana") and src != dst and not (OPS[src].key_type == OPS[dst].key_type == "solana"):
                try:
                    rq = await xbridge.fetch_relay_quote(
                        http, wallet=src_wallet.address, from_chain_id=from_cid, to_chain_id=to_cid, from_token=from_token,
                        to_token=to_token, amount_in=spend_raw, to_address=dst_wallet.address)
                    options = xbridge.merge_options(options, [xbridge.relay_option(
                        rq, src_wallet.address, {"from_token": from_token, "to_token": to_token})])
                except Exception as exc:
                    log.info("Relay not offered: %r", exc)
            if not options:
                raise lifi_error or xbridge.XBridgeError("No bridge found for this amount.")
    except xbridge.XBridgeError as exc:
        hint = ""
        if probe_ctx and "No bridge" in str(exc):
            try:
                hint = await _xb_min_hint(probe_ctx, token, spend_raw, int(data.get("bal_raw", "0") or 0))
            except Exception:
                log.exception("Minimum amount search failed")
        return await _show(event, f"❌ {html.escape(str(exc))}{hint}", _xb_options_keyboard(
            {"options": [], "src": src, "dst": dst, "token_id": token.id}))
    except Exception:
        log.exception("Bridge routes failed")
        return await _show(event, "I couldn't look up bridges just now. Nothing was sent. Please try again in a minute.", home_only())
    labels = xbridge.route_labels(options)
    stored = []
    for opt, badge in zip(options, labels):
        stored.append({
            "i": opt.index, "step": opt.step, "tool": opt.tool,
            "label": f"{opt.index + 1}. {opt.tool} · {dexquote._num(opt.receive)} {opt.to_symbol}" + (" ⭐" if "Best" in badge else "") + (" ⚡" if "Fastest" in badge else ""),
        })
    pay_text = dexquote._num(Decimal(spend_raw) / (Decimal(10) ** token.decimals))
    text = xbridge.render_options(options, OPS[src].name, OPS[dst].name, pay_text, token.symbol)
    await state.update_data(options=stored, from_cid=from_cid, to_cid=to_cid, to_addr=dst_wallet.address, list_text=text,
                            symbol=token.symbol)
    await _show(event, text[:4000] + "\n\n<i>Fees and prices change quickly. You confirm on the next screen.</i>",
                _xb_options_keyboard({"options": stored, "src": src, "dst": dst, "token_id": token.id}))


async def _xb_min_hint(ctx: dict, token, spend_raw: int, balance_raw: int) -> str:
    """When no bridge takes the amount: tries larger amounts and says the smallest one that works (about)."""
    price = await asyncio.wait_for(PRICES.price_of(token, ctx["src"]), 6)
    if not price or price <= 0:
        return "\n\nTry a bigger amount."
    unit = Decimal(10) ** token.decimals
    levels = [Decimal(x) for x in ("0.5", "1", "2", "3", "5", "7", "10", "15", "20", "30", "50", "100", "200")]
    tries = []
    for usd in levels:
        raw = int(usd / price * unit)
        if spend_raw < raw:  # tested even above the balance: the person should learn what they would need
            tries.append((usd, raw))
    if not tries:
        return "\n\nTry a bigger amount."
    gate = asyncio.Semaphore(4)

    async def works(raw: int) -> bool:
        async with gate:
            try:
                async with _xb_http() as http:
                    if not ctx["sol_src"]:
                        try:
                            if await xbridge.fetch_routes(
                                    http, wallet=ctx["src_wallet"], from_chain_id=ctx["from_cid"], to_chain_id=ctx["to_cid"],
                                    from_token=ctx["from_token"], to_token=ctx["to_token"], amount_in=raw, to_address=ctx["dst_wallet"]):
                                return True
                        except Exception:
                            pass
                    if xbridge.relay_enabled() and OPS[ctx["src"]].key_type in ("evm", "solana") and OPS[ctx["dst"]].key_type in ("evm", "solana"):
                        await xbridge.fetch_relay_quote(
                            http, wallet=ctx["src_wallet"], from_chain_id=ctx["from_cid"], to_chain_id=ctx["to_cid"],
                            from_token=ctx["from_token"], to_token=ctx["to_token"], amount_in=raw, to_address=ctx["dst_wallet"])
                        return True
            except Exception:
                return False
            return False

    results = await asyncio.gather(*(works(raw) for _, raw in tries))
    for (usd, raw), ok in zip(tries, results):
        if ok:
            need = f"\n\n💡 The smallest amount that works is about <b>{dexquote._num(Decimal(raw) / unit)} {html.escape(token.symbol)}</b> (≈ ${usd})."
            if raw > balance_raw:
                have = dexquote._num(Decimal(balance_raw) / unit)
                return need + f"\nYou can bridge {have} {html.escape(token.symbol)} now, which is less. Add more {html.escape(token.symbol)} to bridge."
            return need + " Try that or more."
    return "\n\nI tried amounts up to about $200 and none worked right now. Try again later."


async def _xb_checked_quote(event, data: dict, idx: int):
    """(quote, wallet address) for the chosen route, asked for fresh from LI.FI and checked."""
    src = data["src"]
    session, user = await _load(event)
    async with session:
        wallet = await service.get_wallet(session, user, OPS[src].key_type)
    chosen = next(o for o in data["options"] if o["i"] == idx)
    async with _xb_http() as http:
        q = await xbridge.fetch_step_quote(http, chosen["step"], wallet=wallet.address, from_chain_id=int(data["from_cid"]),
                                           to_chain_id=int(data["to_cid"]), amount_in=int(data["spend_raw"]), to_address=data["to_addr"])
    return q, wallet.address


@router.callback_query(F.data.startswith("xbr:"), XBridgeFlow.amount)
async def xb_route(cb: CallbackQuery, state: FSMContext):
    if not _private(cb):
        return await cb.answer(PRIVATE_ONLY, show_alert=True)
    data = await state.get_data()
    try:
        idx = int(cb.data.split(":")[1])
        assert any(o["i"] == idx for o in data.get("options", []))
    except Exception:
        return await cb.answer("These options have expired. Please start again.", show_alert=True)
    await cb.answer()
    await cb.message.edit_text("⏳ Preparing that bridge…")
    try:
        q, _addr = await _xb_checked_quote(cb, data, idx)
    except xbridge.XBridgeError as exc:
        return await cb.message.edit_text(f"❌ {html.escape(str(exc))}", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Other bridges", callback_data="xbl")]]))
    except Exception:
        log.exception("Bridge step failed")
        return await cb.message.edit_text("I couldn't prepare that bridge just now. Nothing was sent.", reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ Other bridges", callback_data="xbl")]]))
    nonce = secrets.token_hex(4)
    await state.update_data(nonce=nonce, idx=idx, shown_out=str(q.amount_out))
    body = xbridge.render_quote(q, OPS[data["src"]].name, OPS[data["dst"]].name, OPS[data["src"]].native_symbol)
    await cb.message.edit_text(
        body + "\n\n<i>Bridges can't be undone. Check the networks and the amount. Not financial advice.</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Confirm bridge", callback_data=f"xbok:{nonce}")],
            [InlineKeyboardButton(text="⬅️ Other bridges", callback_data="xbl")]]))


@router.callback_query(F.data == "xbl", XBridgeFlow.amount)
async def xb_list(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get("options"):
        return await cb.answer("These options have expired. Please start again.", show_alert=True)
    await cb.answer()
    await cb.message.edit_text(data["list_text"][:4000], reply_markup=_xb_options_keyboard(data))


async def _xb_watch(bot, chat_id: int, tx_hash: str, from_cid: int, to_cid: int, symbol: str, decimals: int, to_name: str,
                   request_id: str = ""):
    """Tells the person when the bridge has landed (or failed). Checks every 20 seconds for up to 20 minutes."""
    waited = 0
    result = {"status": "UNKNOWN"}
    try:
        async with _xb_http() as http:
            while waited < xbridge.STATUS_GIVE_UP_SECONDS:
                await asyncio.sleep(xbridge.STATUS_POLL_SECONDS)
                waited += xbridge.STATUS_POLL_SECONDS
                if request_id:
                    result = await xbridge.fetch_relay_status(http, request_id)
                else:
                    result = await xbridge.fetch_status(http, tx_hash, from_cid, to_cid)
                if result["status"] in ("DONE", "FAILED"):
                    break
        status = result["status"]
        if status == "DONE":
            got = (f"{dexquote._num(Decimal(result['received']) / (Decimal(10) ** decimals))} {html.escape(symbol)} "
                   if result.get("received") else "")
            text = f"✅ <b>Bridge complete.</b> {got}arrived on {html.escape(to_name)}. You'll also get the normal deposit message."
            if result.get("substatus") == "REFUNDED":
                text = "↩️ The bridge could not finish and your funds were returned to you on the first network."
        elif status == "FAILED":
            text = "❌ The bridge failed. Check your balance on both networks; your funds may have been returned."
        else:
            text = (f"⏳ The bridge is still not confirmed after {waited // 60} minutes. This can happen with slow bridges. "
                    "Check your balances later and do not bridge the same amount again until you have.")
        await bot.send_message(chat_id, text)
    except Exception:
        log.exception("Bridge watcher failed")


@router.callback_query(F.data.startswith("xbok:"), XBridgeFlow.amount)
async def xb_confirm(cb: CallbackQuery, state: FSMContext):
    if not _private(cb):
        return await cb.answer(PRIVATE_ONLY, show_alert=True)
    data = await state.get_data()
    if not data or data.get("nonce") != cb.data.split(":", 1)[1]:
        return await cb.answer("This confirmation has expired. Start again.", show_alert=True)
    await cb.answer()
    src, dst = data["src"], data["dst"]
    ops = OPS[src]
    token = await _xb_token_of(cb, src, int(data["token_id"]))
    if token is None:
        await state.clear()
        return await cb.message.edit_text("That token is not available any more.", reply_markup=home_only())
    lock = send_lock(cb.from_user.id, src)
    if lock.locked():
        return await cb.message.edit_text("Another transaction on this network is still in progress. Please wait.")
    async with lock:
        await cb.message.edit_text("⏳ Checking the price…")
        try:
            fresh, address = await _xb_checked_quote(cb, data, int(data["idx"]))
        except xbridge.XBridgeError as exc:
            return await cb.message.edit_text(f"❌ {html.escape(str(exc))}", reply_markup=home_only())
        except Exception:
            log.exception("Fresh bridge quote failed")
            return await cb.message.edit_text("❌ I couldn't reach the bridge service. Nothing was sent. Please try again.", reply_markup=home_only())
        shown = int(data.get("shown_out", "0"))
        if shown and fresh.amount_out < shown * 99 // 100:
            nonce = secrets.token_hex(4)
            await state.update_data(nonce=nonce, shown_out=str(fresh.amount_out))
            body = xbridge.render_quote(fresh, ops.name, OPS[dst].name, ops.native_symbol)
            return await cb.message.edit_text(
                "⚠️ <b>The price changed.</b> Here is the new quote:\n\n" + body,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✅ Confirm bridge", callback_data=f"xbok:{nonce}")],
                    [InlineKeyboardButton(text="⬅️ Other bridges", callback_data="xbl")]]))
        await state.clear()
        await cb.message.edit_text("⏳ Bridging… this can take a minute.")
        session, user = await _load(cb)
        async with session:
            row = await service.record_outgoing(session, user, token, "bridge_out", fresh.amount_in, f"bridge {src} to {dst}")
            try:
                private_key = await service.get_private_key(session, user, SETTINGS.master_key, ops.key_type)
                if fresh.sol is not None:
                    outcome, approve_hash = await xbridge.execute_solana(ops.rpc, private_key, address, fresh), None
                else:
                    outcome, approve_hash = await xbridge.execute(ops.rpc, private_key, address, fresh)
            except neo_send.SendError as exc:
                row.status = "failed"
                row.note = f"{row.note} | {str(exc)[:100]}"[:250]
                await session.commit()
                return await cb.message.edit_text(f"❌ {html.escape(str(exc))}", reply_markup=home_only())
            except Exception:
                log.exception("Bridge failed before broadcast")
                row.status = "failed"
                await session.commit()
                return await cb.message.edit_text("❌ Something went wrong before the transaction was sent. Nothing was sent.", reply_markup=home_only())
            row.tx_hash = outcome.tx_hash
            row.status = outcome.status
            await session.commit()
    tx = historyview.tx_html(SETTINGS.network, src, outcome.tx_hash)
    amount = f"{dexquote._num(fresh.pay)} {html.escape(fresh.from_symbol)}"
    if outcome.status == "confirmed":
        await cb.message.edit_text(
            f"✅ <b>Bridge started</b>: {amount} left {html.escape(ops.name)}.\n"
            f"It should arrive on {html.escape(OPS[dst].name)} in {xbridge.format_duration(fresh.duration_s)}. I'll message you when it lands.\n{tx}",
            reply_markup=home_only())
        task = asyncio.create_task(_xb_watch(cb.bot, cb.message.chat.id, outcome.tx_hash, fresh.from_chain_id, fresh.to_chain_id,
                                             fresh.to_symbol, fresh.to_decimals, OPS[dst].name, fresh.request_id))
        _XB_TASKS.add(task)
        task.add_done_callback(_XB_TASKS.discard)
    elif outcome.status == "failed":
        await cb.message.edit_text(f"❌ The bridge transaction failed on-chain ({html.escape(outcome.detail)}). "
                                   f"A small network fee may have been used.\n{tx}", reply_markup=home_only())
    else:
        await cb.message.edit_text("⚠️ The transaction was sent but I could not confirm it yet. "
                                   f"<b>Do not bridge again</b> until you check your balances.\n{tx}", reply_markup=home_only())


# ------------------------------------------------------------------ set a token's logo yourself
def _is_logo_admin(user) -> bool:
    name = (user.username or "").lower()
    return name in SETTINGS.airdrop_admin_usernames or user.id in SETTINGS.airdrop_admin_ids or user.id in SETTINGS.event_admin_ids


LOGO_HELP = ("🖼 <b>Set a token's logo</b>\n\nSend a square picture with this caption:\n<code>/setlogo SYMBOL</code>\n"
             "for example <code>/setlogo FRANK</code>. The logo shows on the home card at the next refresh.")


@router.message(F.photo, F.caption.regexp(r"^/setlogo(@\w+)?(\s|$)"))
async def setlogo_photo(message: Message):
    if not _private(message) or not _is_logo_admin(message.from_user):
        return
    parts = (message.caption or "").split()
    symbol = re.sub(r"[^A-Za-z0-9]", "", parts[1]).upper() if len(parts) > 1 else ""
    if not symbol or len(symbol) > 20:
        return await message.answer(LOGO_HELP)
    buf = io.BytesIO()
    await message.bot.download(message.photo[-1], destination=buf)
    img = token_logos._prepare(buf.getvalue())
    if img is None:
        return await message.answer("I couldn't read that picture. Please send it as a photo.")
    os.makedirs(token_logos.CUSTOM_DIR, exist_ok=True)
    img.save(os.path.join(token_logos.CUSTOM_DIR, f"{symbol}.png"))
    await message.answer(f"✅ Saved the logo for {html.escape(symbol)}. Open Home to see it.")


@router.message(Command("setlogo"))
async def setlogo_help(message: Message):
    if not _private(message) or not _is_logo_admin(message.from_user):
        return
    await message.answer(LOGO_HELP)


# ------------------------------------------------------------------------ tipping
def _display(user, fallback: str = "member") -> str:
    return f"@{user.username}" if user.username else fallback


@router.message(Command("tip"))
async def tip_command(message: Message, command: CommandObject):
    if _private(message):
        return await message.answer(tipping.USAGE + "\n\nTipping works in group chats.")
    if message.from_user is None or message.from_user.is_bot:
        return
    try:
        req = tipping.parse_tip(command.args or "")
    except tipping.TipError as exc:
        return await _reply(message, f"❌ {html.escape(str(exc))}\n\n{tipping.USAGE}")

    session, user = await _load(message)
    async with session:
        try:
            candidates = []
            for slug, ops in OPS.items():
                chain = await service.get_chain(session, slug)
                for t in await service.visible_tokens(session, user, chain.id):
                    if t.decimals_verified:
                        candidates.append((t, slug, ops.name))
            token, slug, _name = tipping.pick_token(candidates, req.symbol, req.network)
            ops = OPS[slug]

            # Who gets the tip: mentioned usernames plus the person replied to.
            people: dict[int, tuple[str, str]] = {}  # user_id -> (label, address)
            missing: list[str] = []
            for handle in req.handles:
                found = await service.find_recipient(session, handle, ops.key_type)
                if found is None:
                    missing.append(handle)
                else:
                    people[found[0].id] = (_display(found[0]), found[1].address)
            replied = message.reply_to_message.from_user if message.reply_to_message else None
            if replied is not None and not replied.is_bot:
                r_user = await service.find_user_by_telegram_id(session, replied.id)
                r_wallet = await service.get_wallet(session, r_user, ops.key_type) if r_user else None
                if r_wallet is None:
                    missing.append(f"@{replied.username}" if replied.username else replied.full_name)
                else:
                    people[r_user.id] = (_display(r_user, replied.full_name), r_wallet.address)
            if missing:
                names = ", ".join(html.escape(m) for m in missing)
                raise tipping.TipError(f"{names} hasn't started this bot yet, so I can't send to them. Nothing was sent.")
            people.pop(user.id, None)  # tipping yourself is a no-op, not an error
            recipients = [(uid, label, addr) for uid, (label, addr) in people.items()]
            each, total = tipping.amounts(req, token.decimals, len(recipients))

            own = await service.get_wallet(session, user, ops.key_type)
            chain = await service.get_chain(session, slug)
            balances = await ops.balances(own.address, await _balance_tokens(session, chain, token))
            used = await service.sent_last_24h(session, user, token)
            neo_send.check_limits(token.symbol, token.decimals, each, used, SETTINGS.send_limits)
            await ops.check_send(token, own.address, recipients[0][2], total, balances)
        except tipping.TipError as exc:
            return await _reply(message, f"❌ {html.escape(str(exc))}")
        except neo_send.SendError as exc:
            return await _reply(message, f"❌ {html.escape(str(exc))}")
        except Exception:
            log.exception("Tip check failed")
            return await _reply(message, "❌ I couldn't check that with the network. Try again in a moment.")

    nonce = PENDING_TIPS.add(
        tipping.PendingTip(message.from_user.id, message.chat.id, slug, token.id, each, recipients)
    )
    sym = html.escape(token.symbol)
    fee_line = await _fee_line(slug, user.id, token.id, len(recipients))
    who = "\n".join(f"• {html.escape(label)}" for _, label, _ in recipients)
    total_line = (
        f"Total: <b>{service.format_amount(total, token.decimals)} {sym}</b>\n" if len(recipients) > 1 else ""
    )
    await _reply(message, 
        f"<b>Confirm tip</b> from {html.escape(_display(user, message.from_user.full_name))}\n"
        f"Each: <b>{service.format_amount(each, token.decimals)} {sym}</b> on {html.escape(ops.name)}\n"
        f"{total_line}To:\n{who}\n\n"
        + (fee_line + "\n" if fee_line else "")
        + f"The sender pays the network fee in {html.escape(ops.native_symbol)}. Only the sender can confirm. "
        "Tips are real transfers and cannot be reversed.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Yes, send", callback_data=f"tipok:{nonce}"),
                    InlineKeyboardButton(text="❌ Cancel", callback_data=f"tipno:{nonce}"),
                ]
            ]
        ),
    )


@router.callback_query(F.data.startswith("tipno:"))
async def tip_cancel(cb: CallbackQuery):
    pending = PENDING_TIPS.peek(cb.data.split(":", 1)[1])
    if pending is None:
        return await cb.answer("This tip has expired.", show_alert=True)
    if cb.from_user.id != pending.sender_tg_id:
        return await cb.answer("Only the person who started this tip can cancel it.", show_alert=True)
    PENDING_TIPS.take(cb.data.split(":", 1)[1])
    await cb.message.edit_text("Tip cancelled.")
    await cb.answer()


@router.callback_query(F.data.startswith("tipok:"))
async def tip_confirm(cb: CallbackQuery):
    nonce = cb.data.split(":", 1)[1]
    pending = PENDING_TIPS.peek(nonce)
    if pending is None:
        return await cb.answer("This tip has expired. Send /tip again.", show_alert=True)
    if cb.from_user.id != pending.sender_tg_id:  # someone else tapped: leave the tip untouched
        return await cb.answer("Only the person who started this tip can confirm it.", show_alert=True)
    lock = send_lock(cb.from_user.id, pending.slug)
    if lock.locked():
        return await cb.answer("Another send on this network is still in progress. Please wait.", show_alert=True)
    pending = PENDING_TIPS.take(nonce)  # single use
    if pending is None:
        return await cb.answer("This tip has expired.", show_alert=True)
    await cb.answer()
    ops = OPS.get(pending.slug)
    if ops is None:
        return await cb.message.edit_text("That network is not available right now. Nothing was sent.")
    async with lock:
        await cb.message.edit_text("⏳ Sending tips… this can take a few minutes.")
        session, user = await _load(cb)
        async with session:
            token = await session.get(Token, pending.token_id)
            chain = await service.get_chain(session, ops.slug)
            wallet = await service.get_wallet(session, user, ops.key_type)
            sender_id, sender_name, sender_address = user.id, user.username, wallet.address
            private_key = await service.get_private_key(session, user, SETTINGS.master_key, ops.key_type)
            results = await tipping.run_tip(
                session, ops, user, token, wallet, private_key,
                await _balance_tokens(session, chain, token), pending.recipients,
                pending.each_raw, SETTINGS.send_limits,
            )
    sym = html.escape(token.symbol)
    each = service.format_amount(pending.each_raw, token.decimals)
    lines = []
    for r in results:
        label = html.escape(r.label)
        if r.status == "confirmed":
            link = historyview.explorer_url(SETTINGS.network, ops.slug, r.tx_hash)
            lines.append(f"✅ {label}: {each} {sym}" + (f' · <a href="{html.escape(link, quote=True)}">view tx</a>' if link else ""))
        elif r.status == "skipped":
            lines.append(f"⏭ {label}: not sent")
        elif r.status == "unknown":
            lines.append(f"⚠️ {label}: sent but not confirmed yet, do not tip again until checked")
        else:
            lines.append(f"❌ {label}: {html.escape(r.detail or 'failed')}")
    ok = sum(1 for r in results if r.status == "confirmed")
    for r in results:
        if r.status == "confirmed" and r.recipient_id:
            outcome_note = await announce_in_bot_transfer(
                cb.bot, ops, token, sender_id, sender_name, sender_address, r.recipient_id,
                pending.each_raw, r.tx_hash,
            )
            if outcome_note == "unreachable":
                lines.append(UNREACHABLE_NOTE.format(who=html.escape(r.label)))
    await cb.message.edit_text(f"<b>Tips: {ok} of {len(results)} sent</b>\n" + "\n".join(lines))


# ------------------------------------------------------------------------ events: rain and quiz
RAINS: dict[str, events.Rain] = {}
PENDING_RAINS: dict[str, tuple[events.Rain, float]] = {}  # waiting for the organizer's confirmation
QUIZ_SETUPS: dict[str, events.QuizSetup] = {}
QUIZZES: dict[str, events.Quiz] = {}
STARTED: set[int] = set()  # telegram ids known to have started the bot (a cache, so a tap costs no lookup)
PREPARED: dict[int, dict[str, events.Prepared]] = {}  # telegram id -> {code -> a rain/quiz prepared in private}


def _tg_label(u) -> str:
    return f"@{u.username}" if u.username else u.full_name


def _all_codes() -> set:
    return {code for mine in PREPARED.values() for code in mine}


def _pending(tg_id: int, kind: str) -> list:
    return [p for p in PREPARED.get(tg_id, {}).values() if p.kind == kind and p.status == "pending"]


def _prepared_describe(p: events.Prepared) -> str:
    if p.kind == "rain":
        extra = f" on {html.escape(p.req.network)}" if p.req.network else ""
        return (f"{html.escape(p.req.amount_text)} {html.escape(p.req.symbol)}{extra} · first {p.req.people} people · "
                f"{'equal shares' if p.req.mode == 'fixed' else 'random shares'}")
    q = p.quiz
    return (f"{_reward_summary(q['style'], q['mode'], q['winners'], q['amount_text'], q['symbol'])} · "
            f"{len(q['questions'])} questions · {events.format_seconds(q.get('seconds', events.QUESTION_SECONDS))} each")


def _prepared_finish(tg_id: int, code: str, status: str, note: str) -> None:
    rec = PREPARED.get(tg_id, {}).get(code) if code else None
    if rec is not None:
        rec.status, rec.note = status, note


def _rain_to_dict(r: events.Rain) -> dict:
    """The plain data of an open rain, for rains.json. Times are saved as 'seconds so far', not clock readings."""
    keys = ("id", "chat_id", "starter_tg_id", "starter_user_id", "starter_label", "slug", "network_name", "token_id",
            "symbol", "decimals", "each_raw", "people", "minutes", "message_id", "mode", "amounts", "lines", "paid",
            "failed", "chat_title", "code", "poster_tg_id", "pinned")
    data = {k: getattr(r, k) for k in keys}
    data["claimed"] = {str(k): list(v) for k, v in r.claimed.items()}
    data["claim_amounts"] = {str(k): v for k, v in r.claim_amounts.items()}
    data["inflight"] = sorted(r.inflight)
    data["elapsed"] = r.elapsed()
    return data


def _save_rains() -> None:
    rainstore.save([_rain_to_dict(r) for r in RAINS.values() if r.state == "open"])


def _rain_from_dict(d: dict) -> events.Rain:
    now = time.monotonic()
    rain = events.Rain(
        id=d["id"], chat_id=d["chat_id"], starter_tg_id=d["starter_tg_id"], starter_user_id=d["starter_user_id"],
        starter_label=d["starter_label"], slug=d["slug"], network_name=d["network_name"], token_id=d["token_id"],
        symbol=d["symbol"], decimals=d["decimals"], each_raw=d["each_raw"], people=d["people"], minutes=d["minutes"],
        message_id=d.get("message_id"), mode=d.get("mode", "fixed"), amounts=list(d.get("amounts", [])),
        lines=list(d.get("lines", [])), paid=d.get("paid", 0), failed=d.get("failed", False),
        chat_title=d.get("chat_title", ""), code=d.get("code", ""), poster_tg_id=d.get("poster_tg_id", 0),
        pinned=d.get("pinned", False),
    )
    rain.claimed = {int(k): tuple(v) for k, v in d.get("claimed", {}).items()}
    rain.claim_amounts = {int(k): int(v) for k, v in d.get("claim_amounts", {}).items()}
    rain.started_at = now - float(d.get("elapsed", 0))
    rain.ends_at = rain.started_at + rain.minutes * 60
    for tg_id in d.get("inflight", []):  # a payout was being sent when the bot stopped: don't pay twice, tell the organizer
        label = html.escape(rain.claimed.get(int(tg_id), (0, "someone"))[1])
        rain.lines.append(f"⚠️ {label}: the payout was interrupted by a restart. Check the organizer's history before sending again.")
    return rain


def _restore_rains(bot_obj) -> None:
    """After a restart: put the rains that were still open back to work."""
    for d in rainstore.load():
        try:
            rain = _rain_from_dict(d)
        except Exception:
            log.exception("Could not restore a saved rain")
            continue
        if any(r.chat_id == rain.chat_id for r in RAINS.values()):
            continue
        RAINS[rain.id] = rain
        _spawn(run_rain(bot_obj, rain))
        log.info("Restored the open rain %s in chat %s (%s of %s claimed)", rain.id, rain.chat_id, len(rain.claimed), rain.people)


def _pick_prepared_any(poster_tg_id: int, kind: str, code: str | None):
    """Like _pick_prepared, but a code that someone else prepared works too: the person who prepared it can hand the
    code to a friend, and the friend posts it. Returns (item, error message, tg id of the person who prepared it)."""
    if code:
        for owner_tg, mine in PREPARED.items():
            item = mine.get(code)
            if item is not None and item.kind == kind:
                if item.status != "pending":
                    return None, f"❌ <code>{code}</code> was already sent or cancelled.", owner_tg
                return item, "", owner_tg
        return None, (f"❌ I don't know the code <code>{code}</code>. Check it, or ask the person who prepared it "
                      "(codes are forgotten when the bot restarts)."), poster_tg_id
    item, error = _pick_prepared(poster_tg_id, kind, code)
    return item, error, poster_tg_id


def _pick_prepared(tg_id: int, kind: str, code: str | None):
    """The prepared rain/quiz a /rain or /quiz without details means: (item, "") or (None, message for the person)."""
    word = "rain" if kind == "rain" else "quiz"
    usage = events.RAIN_USAGE if kind == "rain" else events.QUIZ_USAGE
    mine = PREPARED.get(tg_id, {})
    if code:
        item = mine.get(code)
        if item is None or item.kind != kind:
            return None, (f"❌ I don't know the code <code>{code}</code>. You can see your codes under "
                          "<b>🌧 Rain & 🧠 Quiz → 📋 My rains & quizzes</b> in private.")
        if item.status != "pending":
            return None, f"❌ <code>{code}</code> was already sent or cancelled."
        return item, ""
    pending = _pending(tg_id, kind)
    if not pending:
        return None, (f"You haven't prepared a {word} yet. Open me in private, tap <b>🌧 Rain & 🧠 Quiz</b>, then "
                      f"<b>Prepare a {word}</b>, or write it here:\n\n{usage}")
    if len(pending) > 1:
        lines = "\n".join(f"<code>{p.code}</code> · {_prepared_describe(p)}" for p in pending)
        return None, (f"You have {len(pending)} prepared {word}s. Send the one you want with its code, "
                      f"like <code>/{word} {pending[0].code}</code>:\n\n{lines}")
    return pending[0], ""
EVENT_TASKS: set = set()  # keeps running events from being garbage-collected
CONFIRM_TTL = 300


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    EVENT_TASKS.add(task)
    task.add_done_callback(EVENT_TASKS.discard)


async def _still_admin(bot_obj, chat_id: int, tg_id: int) -> bool:
    """Whether this person may still run events in that group (checked again when they confirm)."""
    if tg_id in SETTINGS.event_admin_ids:
        return True
    try:
        member = await bot_obj.get_chat_member(chat_id, tg_id)
    except Exception:
        return False
    return member.status in ("creator", "administrator")


async def _event_token(session, user, symbol: str, network: str | None):
    """The token (and its network) this user means by a symbol, among the tokens on their list."""
    candidates = []
    for slug, ops in OPS.items():
        chain = await service.get_chain(session, slug)
        for t in await service.visible_tokens(session, user, chain.id):
            if t.decimals_verified:
                candidates.append((t, slug, ops.name))
    return tipping.pick_token(candidates, symbol, network)


async def _is_event_admin(message: Message) -> bool:
    """Group admins, or a Telegram id listed in EVENT_ADMINS."""
    tg_id = message.from_user.id
    if tg_id in SETTINGS.event_admin_ids:
        return True
    try:
        member = await message.bot.get_chat_member(message.chat.id, tg_id)
    except Exception:
        return False
    return member.status in ("creator", "administrator")


async def _edit_event(bot_obj, chat_id: int, message_id: int | None, text: str, markup=None) -> bool:
    """Edit an event message. False if it can't be edited (deleted, ...), so the caller can post a new one."""
    if message_id is None:
        return False
    try:
        await bot_obj.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=markup)
        return True
    except TelegramBadRequest as exc:
        return "not modified" in str(exc).lower()
    except Exception:
        log.exception("Could not edit an event message")
        return False


async def _funds_error(slug: str, starter_user_id: int, token_id: int, each_raw: int, total_raw: int) -> str | None:
    """Why the organizer can't pay `total_raw` (None if they can). Nothing is held back: this is a check."""
    ops = OPS.get(slug)
    if ops is None:
        return "That network is not available right now."
    try:
        async with SESSIONS() as session:
            user = await session.get(User, starter_user_id)
            token = await session.get(Token, token_id)
            chain = await service.get_chain(session, slug)
            wallet = await service.get_wallet(session, user, ops.key_type)
            balances = await ops.balances(wallet.address, await _balance_tokens(session, chain, token))
            used = await service.sent_last_24h(session, user, token)
            held = balances.get(ops.token_key(token))
            if held is None:
                return "I can't read your balance right now. Try again in a moment."
            sym = token.symbol
            if held < total_raw:
                return (f"You need {service.format_amount(total_raw, token.decimals)} {sym} for this, "
                        f"but you have {service.format_amount(held, token.decimals)}.")
            neo_send.check_limits(sym, token.decimals, each_raw, used + total_raw - each_raw, SETTINGS.send_limits)
    except neo_send.SendError as exc:
        return str(exc)
    except Exception:
        log.exception("Funds check for an event failed")
        return "I couldn't check your balance with the network. Try again in a moment."
    return None


async def _fee_line(slug: str, user_id: int, token_id: int, transfers: int) -> str:
    """'⛽ Estimated network fee: about 0.0004 BNB ...' for `transfers` sends from this user's wallet; empty if unknown.
    The fee is paid in the network's own coin, which differs per chain (GAS, BNB, ETH, SOL ...)."""
    ops = OPS.get(slug)
    if ops is None or transfers < 1:
        return ""
    try:
        async with SESSIONS() as session:
            user = await session.get(User, user_id)
            token = await session.get(Token, token_id)
            wallet = await service.get_wallet(session, user, ops.key_type)
            est = await asyncio.wait_for(ops.estimate_fee(token, wallet.address), 8)
        try:  # the coin's price in USD, so the fee also shows what it costs in money
            price = (await asyncio.wait_for(PRICES.prices(), 5)).get(est.symbol.upper()) if est else None
        except Exception:
            price = None
        text = chains.fee_text(est, transfers, price)
        return f"⛽ <b>Estimated network fee:</b> {text}. Paid from your wallet in {html.escape(ops.native_symbol)}." if text else ""
    except Exception:
        log.warning("Fee estimate failed for %s", slug, exc_info=True)
        return ""


async def _payout(bot_obj, slug: str, starter_user_id: int, starter_tg_id: int, token_id: int, groups: list):
    """Send each prize as a normal transfer from the organizer's wallet, one after another.

    groups: [((user_id, label, address), raw_amount, note)]. Stops at the first problem (the rest are
    reported as not sent), exactly like /tip. Returns (lines for the group message, number paid).
    """
    ops = OPS.get(slug)
    if ops is None:
        return ["That network is not available right now. Nothing was sent."], 0
    lines: list[str] = []
    paid = 0
    sent: list = []
    stopped = False
    async with send_lock(starter_tg_id, slug):
        async with SESSIONS() as session:
            user = await session.get(User, starter_user_id)
            token = await session.get(Token, token_id)
            chain = await service.get_chain(session, slug)
            wallet = await service.get_wallet(session, user, ops.key_type)
            private_key = await service.get_private_key(session, user, SETTINGS.master_key, ops.key_type)
            balance_tokens = await _balance_tokens(session, chain, token)
            sender_id, sender_name, sender_address = user.id, user.username, wallet.address
            sym = html.escape(token.symbol)
            for recipient, raw, note in groups:
                label = html.escape(recipient[1])
                if stopped:
                    lines.append(f"⏭ {label}: not sent")
                    continue
                result = (await tipping.run_tip(
                    session, ops, user, token, wallet, private_key, balance_tokens, [recipient], raw, SETTINGS.send_limits,
                ))[0]
                amount = f"{service.format_amount(raw, token.decimals)} {sym}"
                if result.status == "confirmed":
                    paid += 1
                    link = historyview.explorer_url(SETTINGS.network, ops.slug, result.tx_hash)
                    lines.append(f"✅ {label}: {amount}{note}" + (f' · <a href="{html.escape(link, quote=True)}">view tx</a>' if link else ""))
                    sent.append((token, recipient, raw, result.tx_hash, label))
                elif result.status == "unknown":
                    stopped = True
                    lines.append(f"⚠️ {label}: sent but not confirmed yet, check before paying again")
                else:
                    stopped = True
                    lines.append(f"❌ {label}: {html.escape(result.detail or 'failed')}")
    for token, recipient, raw, tx_hash, label in sent:
        outcome = await announce_in_bot_transfer(
            bot_obj, ops, token, sender_id, sender_name, sender_address, recipient[0], raw, tx_hash
        )
        if outcome == "unreachable":
            lines.append(UNREACHABLE_NOTE.format(who=label))
    return lines, paid


async def _delete_quietly(message: Message) -> None:
    """Remove the organizer's command from the group so nobody sees it (needs the delete-messages admin right)."""
    try:
        await message.delete()
    except Exception:
        pass


async def _note(message: Message, text: str, fallback_markup=None, **kwargs):
    """Tell the organizer something privately. If I can't message them yet, say it in the group instead."""
    try:
        return await message.bot.send_message(message.from_user.id, text, **kwargs)
    except (TelegramForbiddenError, TelegramBadRequest):
        pass
    hint = "\n\n<i>I couldn't message you privately, so this is shown here. Open me and press Start to keep these private.</i>"
    return await _reply(message, text + hint, **(kwargs if fallback_markup is None else {**kwargs, "reply_markup": fallback_markup}))


# ----------------------------------------------------------------------------------- rain
def _rain_text(rain: events.Rain) -> str:
    sym = html.escape(rain.symbol)
    total = service.format_amount(sum(rain.amounts) or rain.each_raw * rain.people, rain.decimals)
    if rain.mode == "random":
        share = "a <b>random</b> share each (everyone gets a different amount)"
    else:
        share = f"<b>{service.format_amount(rain.each_raw, rain.decimals)} {sym}</b> each"
    return (
        f"🌧 <b>Rain!</b> {html.escape(rain.starter_label)} is raining <b>{total} {sym}</b> "
        f"({html.escape(rain.network_name)})\n"
        f"The first {rain.people} people to tap Claim get {share}.\n\n"
        f"Claimed: <b>{len(rain.claimed)}/{rain.people}</b>\n"
        + ("\n" + "\n".join(rain.lines) + "\n" if rain.lines else "")
        + "\nPrizes are sent right away when you claim.\n"
        "New here? Tapping Claim creates your wallet. Open me in private and press Start to see your tokens."
    )


def _rain_keyboard(rain: events.Rain) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🎁 Claim", callback_data=f"rain:{rain.id}"),
        InlineKeyboardButton(text="🛑 Cancel (organizer)", callback_data=f"raincancel:{rain.id}"),
    ]])


@router.message(Command("rain"))
async def rain_command(message: Message, command: CommandObject):
    if _private(message):
        return await message.answer(events.RAIN_USAGE + "\n\nRain works in group chats.")
    if message.from_user is None or message.from_user.is_bot:
        return
    arg = (command.args or "").strip()
    prepared = None
    code = events.normalize_code(arg) if arg else None
    if not arg or code:
        prepared, error, owner_tg = _pick_prepared_any(message.from_user.id, "rain", code)
        if prepared is None:
            await _delete_quietly(message)
            return await _note(message, error)
        req = prepared.req  # prepared in private: whoever has the code can send it, no admin needed
    else:
        owner_tg = message.from_user.id
        if not await _is_event_admin(message):
            return await _note(
                message,
                "❌ Only group admins can start a rain by typing the details. Anyone can send one they prepared in private: "
                "tap <b>🌧 Rain & 🧠 Quiz</b> in my private chat, prepare it, then send <code>/rain CODE</code> here.",
            )
        await _delete_quietly(message)  # the details stay between you and me until you confirm
        try:
            req = events.parse_rain(arg)
        except events.EventError as exc:
            return await _note(message, f"❌ {html.escape(str(exc))}\n\n{events.RAIN_USAGE}")

    session, user = await _load(message)
    async with session:
        try:
            same_person = owner_tg == message.from_user.id
            if not same_person:  # someone else's code: the prizes come from the person who prepared it
                user = await service.find_user_by_telegram_id(session, owner_tg)
                if user is None:
                    return await _note(message, "❌ I can't find the wallet of the person who prepared that code.")
            candidates = []
            for slug, ops in OPS.items():
                chain = await service.get_chain(session, slug)
                for t in await service.visible_tokens(session, user, chain.id):
                    if t.decimals_verified:
                        candidates.append((t, slug, ops.name))
            token, slug, network_name = tipping.pick_token(candidates, req.symbol, req.network)
            amounts, total = events.rain_amounts(req.amount_text, token.decimals, req.people, req.mode)
            each = max(amounts)  # the biggest single prize, for the send-limit check
            user_id, label = user.id, _display(user, message.from_user.full_name if same_person else "member")
            token_id, symbol, decimals = token.id, token.symbol, token.decimals
        except (tipping.TipError, events.EventError) as exc:
            return await _note(message, f"❌ {html.escape(str(exc))}")
        except Exception:
            log.exception("Rain check failed")
            return await _note(message, "❌ I couldn't check that with the network. Try again in a moment.")
    problem = await _funds_error(slug, user_id, token_id, each, total)
    if problem:
        return await _note(message, f"❌ {html.escape(problem)}")

    rain = events.Rain(
        id=events.new_id(), chat_id=message.chat.id, starter_tg_id=owner_tg, starter_user_id=user_id,
        starter_label=label, slug=slug, network_name=network_name, token_id=token_id, symbol=symbol,
        decimals=decimals, each_raw=each, people=req.people, minutes=req.minutes, mode=req.mode, amounts=amounts,
        chat_title=message.chat.title or "your group", code=prepared.code if prepared else "",
        poster_tg_id=message.from_user.id,
    )
    now = time.monotonic()
    for key in [k for k, (_r, exp) in PENDING_RAINS.items() if exp < now]:
        del PENDING_RAINS[key]
    await _delete_quietly(message)  # keep the group tidy: only the announcement shows
    if prepared is not None:  # prepared (and so already decided) in private: start right away
        err = await _launch_rain(message.bot, rain)
        if err:
            await _note(message, f"❌ {html.escape(err)}")
        return
    PENDING_RAINS[rain.id] = (rain, now + CONFIRM_TTL)
    sym = html.escape(symbol)
    fee_line = await _fee_line(slug, user_id, token_id, req.people)
    share = (f"<b>{service.format_amount(each, decimals)} {sym}</b> each" if req.mode == "fixed"
             else "a <b>random</b> share each (different for everyone)")
    await _note(
        message,
        f"<b>Confirm rain</b> in <b>{html.escape(message.chat.title or 'your group')}</b>\n"
        f"Prize: <b>{service.format_amount(total, decimals)} {sym}</b> on {html.escape(network_name)}, "
        f"{share} for the first {req.people} people, "
        "it ends when everyone has claimed, or when you press Cancel.\n\n"
        + (fee_line + f" That is the most it can cost if all {req.people} people claim.\n\n" if fee_line else "")
        + "The tokens stay in your wallet; I send each prize from it the moment someone claims. "
        f"You pay the network fees ({html.escape(OPS[slug].native_symbol)}). Keep the balance until then, "
        "or the payouts will stop. Transfers are real and cannot be reversed.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Start the rain", callback_data=f"rainok:{rain.id}"),
            InlineKeyboardButton(text="❌ Cancel", callback_data=f"rainno:{rain.id}"),
        ]]),
    )


@router.callback_query(F.data.startswith("rainno:"))
async def rain_no(cb: CallbackQuery):
    item = PENDING_RAINS.get(cb.data.split(":", 1)[1])
    if item is None:
        return await cb.answer("This has expired.", show_alert=True)
    if cb.from_user.id != item[0].starter_tg_id:
        return await cb.answer("Only the person who started this can cancel it.", show_alert=True)
    PENDING_RAINS.pop(item[0].id, None)
    await cb.answer()
    await cb.message.edit_text("Rain cancelled.")


@router.callback_query(F.data.startswith("rainok:"))
async def rain_ok(cb: CallbackQuery):
    item = PENDING_RAINS.get(cb.data.split(":", 1)[1])
    if item is None or item[1] < time.monotonic():
        return await cb.answer("This has expired. Send /rain again.", show_alert=True)
    rain = item[0]
    if cb.from_user.id != rain.starter_tg_id:
        return await cb.answer("Only the person who started this can confirm it.", show_alert=True)
    if not await _still_admin(cb.bot, rain.chat_id, cb.from_user.id):
        return await cb.answer("You are not an admin of that group (any more), so I can't start the rain there.", show_alert=True)
    PENDING_RAINS.pop(rain.id, None)  # single use
    err = await _launch_rain(cb.bot, rain)
    if err:
        PENDING_RAINS[rain.id] = item
        return await cb.answer(err[:190], show_alert=True)
    await cb.answer()
    try:
        await cb.message.edit_text("✅ Your rain is live in the group. Use the Cancel button there to stop it.")
    except Exception:
        pass


async def _launch_rain(bot_obj, rain: events.Rain) -> str | None:
    """Post the rain in its group and start it. Returns why it can't start (for the organizer), or None if it did."""
    rec = PREPARED.get(rain.starter_tg_id, {}).get(rain.code) if rain.code else None
    if rain.code and (rec is None or rec.status != "pending"):
        return "This rain was already sent or cancelled."
    if any(r.chat_id == rain.chat_id and r.state in ("open", "paying") for r in RAINS.values()):
        return "A rain is already running in that group. Wait until it ends, or cancel it."
    RAINS[rain.id] = rain  # claimed now, so a second launch can't slip in while we check the balance
    if rec is not None:
        rec.status, rec.note = "live", "starting"
    problem = await _funds_error(rain.slug, rain.starter_user_id, rain.token_id, max(rain.amounts), sum(rain.amounts))
    sent = None
    if problem is None:
        rain.started_at = time.monotonic()
        rain.ends_at = rain.started_at + rain.minutes * 60
        try:
            sent = await bot_obj.send_message(rain.chat_id, _rain_text(rain), reply_markup=_rain_keyboard(rain))
        except Exception:
            log.exception("Could not post the rain in the group")
            problem = "I couldn't post in the group. Is the bot still in it, and allowed to write?"
    if sent is None:
        RAINS.pop(rain.id, None)
        if rec is not None:
            rec.status, rec.note = "pending", ""
        return problem
    rain.message_id = sent.message_id
    _save_rains()
    if rec is not None:
        rec.note = f"live in {rain.chat_title}"
    # Pin it only when a group admin posted it: a normal member must not be able to pin messages in the group.
    if await _still_admin(bot_obj, rain.chat_id, rain.poster_tg_id or rain.starter_tg_id):
        try:
            await bot_obj.pin_chat_message(rain.chat_id, rain.message_id, disable_notification=True)
            rain.pinned = True
        except Exception:
            pass  # pinning needs the 'pin messages' admin right; the rain works without it
    _save_rains()
    _spawn(run_rain(bot_obj, rain))
    return None


@router.callback_query(F.data.startswith("rain:"))
async def rain_claim(cb: CallbackQuery):
    rain = RAINS.get(cb.data.split(":", 1)[1])
    if rain is None or rain.state != "open":
        return await cb.answer("This rain has ended.", show_alert=True)
    if cb.from_user.is_bot:
        return await cb.answer()
    ops = OPS.get(rain.slug)
    if ops is None:
        return await cb.answer("That network is not available right now.", show_alert=True)
    try:
        session, user = await _load(cb)
        async with session:
            wallet = await service.get_wallet(session, user, ops.key_type)
            label = _display(user, cb.from_user.full_name)
            user_id, address = user.id, wallet.address
    except Exception:
        log.exception("Rain claim failed")
        return await cb.answer("Something went wrong. Try again.", show_alert=True)
    result = rain.try_claim(cb.from_user.id, user_id, label, address)
    messages = {
        "ok": "🎁 You got it! Your prize is on its way. Open me in private and press Start to see your tokens.",
        "closed": "This rain has ended.",
        "owner": "You started this rain, so you can't claim it.",
        "dup": "You already claimed this one.",
        "full": "Too late, everyone's claimed already.",
    }
    if result == "ok":
        rain.pending += 1
        rain.inflight.add(cb.from_user.id)
        _save_rains()  # the claim is on disk before the payout starts
        _spawn(pay_claim(cb.bot, rain, (user_id, label, address), rain.claim_amounts[cb.from_user.id], cb.from_user.id))
    await cb.answer(messages[result], show_alert=result != "ok")


@router.callback_query(F.data.startswith("raincancel:"))
async def rain_cancel(cb: CallbackQuery):
    rain = RAINS.get(cb.data.split(":", 1)[1])
    if rain is None or rain.state != "open":
        return await cb.answer("This rain has ended.", show_alert=True)
    if cb.from_user.id not in (rain.starter_tg_id, rain.poster_tg_id):
        return await cb.answer("Only the organizer can cancel the rain.", show_alert=True)
    rain.state = "cancelled"
    _save_rains()
    rain.wake.set()
    await cb.answer("Cancelled.")


async def run_rain(bot_obj, rain: events.Rain) -> None:
    try:
        last = None
        while rain.state == "open" and rain.seconds_left() > 0 and not rain.is_full():
            try:
                await asyncio.wait_for(rain.wake.wait(), timeout=max(1.0, rain.seconds_left()))
                await asyncio.sleep(1)  # several claims in a burst become one edit
            except asyncio.TimeoutError:
                pass
            rain.wake.clear()
            if rain.state != "open":
                break
            text = _rain_text(rain)
            if text != last:
                await _edit_event(bot_obj, rain.chat_id, rain.message_id, text, _rain_keyboard(rain))
                last = text
        await finish_rain(bot_obj, rain)
    except Exception:
        log.exception("Rain %s failed", rain.id)
        _prepared_finish(rain.starter_tg_id, rain.code, "done", f"stopped by an error · {rain.chat_title}")
        rain.state = "done"
        RAINS.pop(rain.id, None)
        try:
            await bot_obj.send_message(rain.chat_id, "⚠️ The rain stopped because of an error. Prizes already shown as paid were sent.")
        except Exception:
            pass


async def _post_or_edit(bot_obj, chat_id: int, message_id: int | None, text: str) -> None:
    if not await _edit_event(bot_obj, chat_id, message_id, text):
        try:
            await bot_obj.send_message(chat_id, text)
        except Exception:
            log.exception("Could not post an event result")


async def pay_claim(bot_obj, rain: events.Rain, winner: tuple, raw: int, tg_id: int = 0) -> None:
    """Send one claimer's prize right away. A failure closes the rain so nobody else is left unpaid."""
    try:
        lines, paid = await _payout(
            bot_obj, rain.slug, rain.starter_user_id, rain.starter_tg_id, rain.token_id, [(winner, raw, "")]
        )
    except Exception:
        log.exception("Rain payout failed")
        lines, paid = [f"❌ {html.escape(winner[1])}: failed"], 0
    rain.lines.extend(lines)
    rain.paid += paid
    if paid == 0 and rain.state == "open":
        rain.failed = True
        rain.state = "paying"  # no more claims
    rain.pending -= 1
    rain.inflight.discard(tg_id)
    _save_rains()
    rain.wake.set()


async def finish_rain(bot_obj, rain: events.Rain) -> None:
    head = f"🌧 <b>Rain from {html.escape(rain.starter_label)}</b>"
    cancelled = rain.state == "cancelled"
    timed_out = rain.state == "open" and rain.seconds_left() <= 0 and not rain.is_full()
    rain.finished_at = time.monotonic()  # the clock stops at the last claim, not after the payouts
    if rain.state == "open":
        rain.state = "paying"  # no more claims from here on
    for _ in range(300):  # let payouts that are still being sent finish
        if rain.pending <= 0:
            break
        await asyncio.sleep(1)
    sym = html.escape(rain.symbol)
    if rain.mode == "random":
        share = f"{service.format_amount(sum(rain.amounts), rain.decimals)} {sym} shared randomly"
    else:
        share = f"{service.format_amount(rain.each_raw, rain.decimals)} {sym} each"
    if not rain.claimed:
        text = f"{head} was cancelled. Nothing was sent." if cancelled else f"{head} is over. Nobody claimed it. Nothing was sent."
    else:
        word = "was cancelled" if cancelled else "is over"
        text = (f"{head} {word}: <b>{rain.paid} of {len(rain.claimed)} paid</b> ({share}, "
                f"{html.escape(rain.network_name)})\n" + "\n".join(rain.lines))
        if rain.failed:
            text += "\n⚠️ A payout failed, so I stopped the rain. Check your balance and fee coin."
    await _post_or_edit(bot_obj, rain.chat_id, rain.message_id, text)
    rain.state = "done"
    RAINS.pop(rain.id, None)
    _save_rains()
    if rain.pinned:
        try:
            await bot_obj.unpin_chat_message(rain.chat_id, rain.message_id)  # the rain is over: take the pin off
        except Exception:
            log.info("Could not unpin the rain message (the bot needs the 'pin messages' admin right)")
    clock = events.format_clock(rain.elapsed())
    n = len(rain.claimed)
    if cancelled:
        last = f"🛑 Rain cancelled after <b>{clock}</b>. {n} of {rain.people} had claimed."
    elif timed_out:
        last = f"⌛ Rain stopped after <b>{clock}</b> (the {events.MAX_MINUTES} minute limit). {n} of {rain.people} claimed."
    elif n >= rain.people:
        last = f"⚡ Rain over! All {rain.people} prizes were claimed in <b>{clock}</b>."
    else:
        last = f"Rain over after <b>{clock}</b>. {n} of {rain.people} claimed."
    _prepared_finish(rain.starter_tg_id, rain.code, "cancelled" if cancelled else "done",
                     f"{n} of {rain.people} claimed in {clock} · {rain.chat_title}")
    try:
        await bot_obj.send_message(rain.chat_id, last)
    except Exception:
        log.exception("Could not post the rain summary")


# ----------------------------------------------------------------------------------- quiz
def _reward_summary(style: str, mode: str, winners: int, amount_text: str, symbol: str) -> str:
    """One sentence on who wins what, for the quiz messages."""
    amt = f"{html.escape(amount_text)} {html.escape(symbol)}"
    if mode == "ranked":
        places = " / ".join(html.escape(x) for x in events.rank_parts(amount_text))
        count = len(events.rank_parts(amount_text))
        if style == "question":
            return f"on every question the {count} fastest correct answers win {places} {html.escape(symbol)} (1st / 2nd / ...)"
        return f"the top {count} win {places} {html.escape(symbol)} (1st / 2nd / ...)"
    if style == "question":
        who = "the fastest correct answer" if winners == 1 else f"the {winners} fastest correct answers"
        where = "on every question"
    else:
        who = "the best score" if winners == 1 else f"the {winners} best scores"
        where = "at the end"
    if mode == "fixed":
        return f"{who} {where} win {amt} each"
    return f"{who} {where} share {amt} in total, split at random"


def _quiz_reward_text(setup: events.QuizSetup) -> str:
    return _reward_summary(setup.style, setup.mode, setup.winners, setup.amount_text, setup.symbol)


@router.message(Command("quiz"))
async def quiz_command(message: Message, command: CommandObject):
    if _private(message):
        return await message.answer(events.QUIZ_USAGE + "\n\nQuizzes run in group chats.")
    if message.from_user is None or message.from_user.is_bot:
        return
    parts = (command.args or "").split()
    prepared = None
    code = events.normalize_code(parts[0]) if len(parts) == 1 else None
    if not parts or code:
        prepared, error, owner_tg = _pick_prepared_any(message.from_user.id, "quiz", code)
        if prepared is None:
            await _delete_quietly(message)
            return await _note(message, error)
        d = prepared.quiz
        parts = [d["amount_text"], d["symbol"]] + ([d["network"]] if d["network"] else [])
    elif not await _is_event_admin(message):
        return await _note(
            message,
            "❌ Only group admins can start a quiz by typing the details. Anyone can send one they prepared in private: "
            "tap <b>🌧 Rain & 🧠 Quiz</b> in my private chat, prepare it, then send <code>/quiz CODE</code> here.",
        )
    if prepared is None:
        owner_tg = message.from_user.id
    await _delete_quietly(message)  # the setup stays private
    if len(parts) < 2 or len(parts) > 3 or not parts[1].isalnum() or (len(parts) == 3 and not parts[2].replace("_", "").isalnum()):
        return await _note(message, f"❌ I need a prize amount and a token symbol.\n\n{events.QUIZ_USAGE}")
    amount_text, symbol, network = parts[0], parts[1], (parts[2] if len(parts) == 3 else None)
    qd = prepared.quiz if prepared is not None else {}
    style, mode, winners = qd.get("style", "question"), qd.get("mode", "fixed"), qd.get("winners", 1)
    seconds = qd.get("seconds", events.QUESTION_SECONDS)

    session, user = await _load(message)
    async with session:
        try:
            same_person = owner_tg == message.from_user.id
            if not same_person:  # someone else's code: the prizes come from the person who prepared it
                user = await service.find_user_by_telegram_id(session, owner_tg)
                if user is None:
                    return await _note(message, "❌ I can't find the wallet of the person who prepared that code.")
            candidates = []
            for slug, ops in OPS.items():
                chain = await service.get_chain(session, slug)
                for t in await service.visible_tokens(session, user, chain.id):
                    if t.decimals_verified:
                        candidates.append((t, slug, ops.name))
            token, slug, network_name = tipping.pick_token(candidates, symbol, network)
            # a first sanity check; the real amounts come with the questions
            first = max(events.parse_rank_amounts(amount_text, token.decimals)) if mode == "ranked" else _number_in(amount_text, token.decimals)
            user_id, label = user.id, _display(user, message.from_user.full_name if same_person else "member")
            token_id, sym_raw, decimals = token.id, token.symbol, token.decimals
        except (tipping.TipError, events.EventError) as exc:
            return await _note(message, f"❌ {html.escape(str(exc))}")
        except Exception:
            log.exception("Quiz check failed")
            return await _note(message, "❌ I couldn't check that with the network. Try again in a moment.")
    problem = await _funds_error(slug, user_id, token_id, first, first)
    if problem:
        return await _note(message, f"❌ {html.escape(problem)}")

    setup = events.QuizSetup(
        id=events.new_id(), chat_id=message.chat.id, starter_tg_id=owner_tg, starter_user_id=user_id,
        starter_label=label, slug=slug, network_name=network_name, token_id=token_id, symbol=sym_raw,
        decimals=decimals, amount_text=amount_text, style=style, mode=mode, winners=winners, seconds=seconds,
        poster_tg_id=message.from_user.id,
    )
    now = time.monotonic()
    for key in [k for k, v in QUIZ_SETUPS.items() if v.expires < now]:
        del QUIZ_SETUPS[key]
    QUIZ_SETUPS[setup.id] = setup
    setup.chat_title = message.chat.title or "your group"
    if prepared is not None:  # prepared (and so already decided) in private: start right away
        setup.code = prepared.code
        try:
            setup.plan(prepared.quiz["questions"])
        except events.EventError as exc:
            QUIZ_SETUPS.pop(setup.id, None)
            return await _note(message, f"❌ {html.escape(str(exc))}")
        err = await _launch_quiz(message.bot, setup)
        if err:
            QUIZ_SETUPS.pop(setup.id, None)
            await _note(message, f"❌ {html.escape(err)}")
        return
    me = await message.bot.me()
    text = (
        f"🧠 <b>Quiz setup</b>\n"
        f"Reward: {_quiz_reward_text(setup)} ({html.escape(network_name)}).\n\n"
        "Tap the button, then send me the questions here in private, so the answers stay secret. "
        "Nothing is shown in the group until you confirm."
    )
    await _note(
        message, text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✍️ Send the questions", callback_data=f"qzstart:{setup.id}")
        ]]),
        fallback_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✍️ Send the questions", url=f"https://t.me/{me.username}?start=quiz_{setup.id}")
        ]]),
    )


@router.callback_query(F.data.startswith("qzstart:"))
async def quiz_start_private(cb: CallbackQuery, state: FSMContext):
    setup = QUIZ_SETUPS.get(cb.data.split(":", 1)[1])
    if setup is None or setup.expires < time.monotonic():
        return await cb.answer("This has expired. Start again with /quiz in the group.", show_alert=True)
    if cb.from_user.id != setup.starter_tg_id:
        return await cb.answer("Only the admin who started this quiz can set it up.", show_alert=True)
    await cb.answer()
    await state.clear()
    await state.set_state(QuizFlow.questions)
    await state.update_data(quiz_id=setup.id)
    await cb.message.answer(
        f"🧠 <b>Quiz setup</b>\nReward: {_quiz_reward_text(setup)} ({html.escape(setup.network_name)})\n\n" + events.QUESTIONS_HELP
    )


async def quiz_deeplink(message: Message, state: FSMContext, setup_id: str):
    setup = QUIZ_SETUPS.get(setup_id)
    if setup is None or setup.expires < time.monotonic():
        return await message.answer("This quiz link has expired. Start again with /quiz in the group.")
    if message.from_user.id != setup.starter_tg_id:
        return await message.answer("Only the admin who started this quiz can set it up.")
    await state.clear()
    await state.set_state(QuizFlow.questions)
    await state.update_data(quiz_id=setup_id)
    await message.answer(
        f"🧠 <b>Quiz setup</b>\nReward: {_quiz_reward_text(setup)} ({html.escape(setup.network_name)})\n\n" + events.QUESTIONS_HELP
    )


def _quiz_confirm_text(setup: events.QuizSetup, total: int) -> str:
    where = f" for <b>{html.escape(setup.chat_title)}</b>" if setup.chat_title else ""
    return (
        f"<b>{len(setup.questions)} questions</b> ready{where}, {events.format_seconds(setup.seconds)} each. Reward: {_quiz_reward_text(setup)}. "
        f"At most <b>{service.format_amount(total, setup.decimals)} {html.escape(setup.symbol)}</b> in total, "
        "paid from your wallet when the quiz ends (you pay the network fees). Start it in the group?"
    )


def _quiz_confirm_keyboard(setup: events.QuizSetup) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Start the quiz", callback_data=f"qzgo:{setup.id}"),
        InlineKeyboardButton(text="❌ Cancel", callback_data=f"qzno:{setup.id}"),
    ]])


@router.message(QuizFlow.questions, F.text)
async def quiz_questions(message: Message, state: FSMContext):
    if not _private(message):
        return
    data = await state.get_data()
    setup = QUIZ_SETUPS.get(data.get("quiz_id", ""))
    if setup is None or setup.expires < time.monotonic():
        await state.clear()
        return await message.answer("This quiz setup has expired. Start again with /quiz in the group.")
    try:
        questions = events.parse_questions(message.text)
    except events.EventError as exc:
        return await message.answer(f"❌ {html.escape(str(exc))}\n\n{events.QUESTIONS_HELP}")
    try:
        setup.plan(questions)
    except events.EventError as exc:
        return await message.answer(f"❌ {html.escape(str(exc))}\n\nSend fewer questions, or a bigger amount.")
    total = setup.total_raw
    problem = await _funds_error(setup.slug, setup.starter_user_id, setup.token_id, setup.top_raw, total)
    if problem:
        return await message.answer(f"❌ {html.escape(problem)}\n\nSend fewer questions, or top up your wallet and send them again.")
    setup.questions = questions
    await state.clear()
    await message.answer(_quiz_confirm_text(setup, total), reply_markup=_quiz_confirm_keyboard(setup))


@router.callback_query(F.data.startswith("qzno:"))
async def quiz_no(cb: CallbackQuery):
    setup = QUIZ_SETUPS.get(cb.data.split(":", 1)[1])
    if setup is None:
        return await cb.answer("This has expired.", show_alert=True)
    if cb.from_user.id != setup.starter_tg_id:
        return await cb.answer("Only the organizer can cancel it.", show_alert=True)
    QUIZ_SETUPS.pop(setup.id, None)
    await cb.answer()
    await cb.message.edit_text("Quiz cancelled.")


@router.callback_query(F.data.startswith("qzgo:"))
async def quiz_go(cb: CallbackQuery):
    setup = QUIZ_SETUPS.get(cb.data.split(":", 1)[1])
    if setup is None or setup.expires < time.monotonic() or not setup.questions:
        return await cb.answer("This has expired. Start again with /quiz in the group.", show_alert=True)
    if cb.from_user.id != setup.starter_tg_id:
        return await cb.answer("Only the organizer can start it.", show_alert=True)
    if not await _still_admin(cb.bot, setup.chat_id, cb.from_user.id):
        return await cb.answer("You are not an admin of that group (any more), so I can't start the quiz there.", show_alert=True)
    err = await _launch_quiz(cb.bot, setup)
    if err:
        return await cb.answer(err[:190], show_alert=True)
    await cb.answer()
    await cb.message.edit_text("✅ The quiz is starting in the group. I'll post the results there.")


def _quiz_stop_row(quiz_id: str) -> list:
    return [InlineKeyboardButton(text="🛑 Cancel quiz (organizer)", callback_data=f"qzstop:{quiz_id}")]


async def _launch_quiz(bot_obj, setup: events.QuizSetup) -> str | None:
    """Post the quiz start in its group and run it. Returns why it can't start (for the organizer), or None."""
    rec = PREPARED.get(setup.starter_tg_id, {}).get(setup.code) if setup.code else None
    if setup.code and (rec is None or rec.status != "pending"):
        return "This quiz was already sent or cancelled."
    if any(q.setup.chat_id == setup.chat_id for q in QUIZZES.values()):
        return "A quiz is already running in that group. Wait until it ends."
    quiz = events.Quiz(setup.id, setup, setup.questions)
    QUIZZES[quiz.id] = quiz  # claimed now, so a second launch can't slip in while we check the balance
    if rec is not None:
        rec.status, rec.note = "live", "starting"
    problem = await _funds_error(setup.slug, setup.starter_user_id, setup.token_id, setup.top_raw, setup.total_raw)
    if problem is None:
        if setup.style == "top":
            how = ("Everybody answers every question. At the end the players with the most correct answers win; "
                   "if scores are equal, the one who answered faster overall ranks higher.")
        else:
            how = "Tap the buttons to answer; a wrong tap locks you out of that question."
        how += (f" Each question is open for {events.format_seconds(setup.seconds)}. "
                "Only people who have already started me in private can play: open me, press Start, then join in. "
                "Prizes are sent to the winners automatically at the end.")
        try:
            await bot_obj.send_message(
                setup.chat_id,
                f"🧠 <b>Quiz time!</b> {html.escape(setup.starter_label)} is running a quiz with {len(quiz.questions)} questions.\n"
                f"Reward: {_quiz_reward_text(setup)} ({html.escape(setup.network_name)}).\n{how}\n"
                "Starting in 10 seconds…",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[_quiz_stop_row(quiz.id)]),
            )
        except Exception:
            log.exception("Could not post the quiz start")
            problem = "I couldn't post in the group. Is the bot still in it, and allowed to write?"
    if problem is not None:
        QUIZZES.pop(quiz.id, None)
        if rec is not None:
            rec.status, rec.note = "pending", ""
        return problem
    QUIZ_SETUPS.pop(setup.id, None)  # single use
    if rec is not None:
        rec.note = f"live in {setup.chat_title}"
    _spawn(run_quiz(bot_obj, quiz))
    return None


@router.callback_query(F.data.startswith("qzstop:"))
async def quiz_stop(cb: CallbackQuery):
    quiz = QUIZZES.get(cb.data.split(":", 1)[1])
    if quiz is None:
        return await cb.answer("This quiz has ended.")
    if cb.from_user.id not in (quiz.setup.starter_tg_id, quiz.setup.poster_tg_id):
        return await cb.answer("Only the organizer can cancel the quiz.", show_alert=True)
    quiz.cancelled = True
    if quiz.current is not None:
        quiz.current.wake.set()
    await cb.answer("Cancelling the quiz…")


@router.callback_query(F.data.startswith("qz:"))
async def quiz_answer(cb: CallbackQuery):
    tapped = time.monotonic()  # the moment the tap reached me: speed is measured from here
    try:
        _tag, quiz_id, idx_text, pos_text = cb.data.split(":")
        idx, pos = int(idx_text), int(pos_text)
    except ValueError:
        return await cb.answer()
    quiz = QUIZZES.get(quiz_id)
    rnd = quiz.current if quiz else None
    if rnd is None or rnd.index != idx:
        return await cb.answer("This question is closed.")
    if cb.from_user.is_bot:
        return await cb.answer()
    if cb.from_user.id == quiz.setup.starter_tg_id:
        return await cb.answer("The organizer can't play their own quiz.", show_alert=True)
    if not await _has_started(cb.from_user.id):
        return await cb.answer(
            "Only people who have started the bot can play, so a prize can reach you. "
            "Open me in private, press Start, then come back and tap again.", show_alert=True)
    if quiz.setup.style == "top":
        return await _quiz_answer_top(cb, quiz, rnd, pos, tapped)
    result, slot = rnd.answer(cb.from_user.id, pos)  # decided right here, with no waiting: the fastest correct taps win
    if result != "win":
        text = {
            "closed": "This question is closed.",
            "already": "You already answered this one.",
            "late": "Too late, the prizes for this question are taken.",
            "wrong": "❌ Wrong answer. You're out for this question.",
        }[result]
        return await cb.answer(text)
    rnd.winners[slot] = events.Win(cb.from_user.id, 0, _tg_label(cb.from_user), "", quiz.setup.prize_for(idx, slot))
    if rnd.complete():
        rnd.wake.set()
    await cb.answer("🎉 Correct! You were first!" if slot == 0 else f"🎉 Correct! You came #{slot + 1}!")


async def _quiz_answer_top(cb: CallbackQuery, quiz: events.Quiz, rnd: events.Round, pos: int, tapped: float) -> None:
    """Style "top": everybody answers; the score and the (secret) speed are kept for the final ranking."""
    result, correct, seconds = rnd.answer_top(cb.from_user.id, pos, tapped)
    if result == "closed":
        return await cb.answer("This question is closed.")
    if result == "already":
        return await cb.answer("You already answered this one.")
    player = quiz.players.get(cb.from_user.id)
    if player is None:
        player = quiz.players[cb.from_user.id] = events.Player(cb.from_user.id, 0, _tg_label(cb.from_user), "")
    if correct:
        player.correct += 1
        player.seconds += seconds
    await cb.answer("✅ Answer received.")  # no hint whether it was right


async def run_quiz(bot_obj, quiz: events.Quiz) -> None:
    setup = quiz.setup
    top = setup.style == "top"
    total = len(quiz.questions)
    try:
        await asyncio.sleep(10)
        for idx, question in enumerate(quiz.questions):
            if quiz.cancelled:
                break
            rnd = events.Round.make(idx, question, slots=0 if top else setup.winners)
            quiz.current = rnd
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=option, callback_data=f"qz:{quiz.id}:{idx}:{pos}")]
                for pos, option in enumerate(rnd.options)
            ] + [_quiz_stop_row(quiz.id)])
            if top:
                note = f"{events.format_seconds(setup.seconds)} · speed counts"
            else:
                note = f"{events.format_seconds(setup.seconds)} · {setup.winners} winner{'s' if setup.winners != 1 else ''}"
            sent = await bot_obj.send_message(
                setup.chat_id,
                f"❓ <b>Question {idx + 1}/{total}</b> · {note}\n\n{html.escape(question.text)}",
                reply_markup=keyboard,
            )
            rnd.started = time.monotonic()
            try:
                await asyncio.wait_for(rnd.wake.wait(), timeout=setup.seconds)
            except asyncio.TimeoutError:
                pass
            rnd.closed = True
            answer = html.escape(question.correct)
            if quiz.cancelled:
                outcome = "🛑 Cancelled by the organizer."
            elif top:
                outcome = f"✅ {rnd.correct_count} of {len(rnd.answered)} answered correctly. The answer: <b>{answer}</b>"
            else:
                won = [rnd.winners[i] for i in sorted(rnd.winners)]
                quiz.wins.extend(won)
                if won:
                    names = "\n".join(f"{n}. <b>{html.escape(w.label)}</b>" for n, w in enumerate(won, start=1))
                    outcome = f"🏁 Fastest:\n{names}\nThe answer: <b>{answer}</b>"
                else:
                    outcome = f"⏰ Time's up! The answer: <b>{answer}</b>"
            await _edit_event(
                bot_obj, setup.chat_id, sent.message_id,
                f"❓ <b>Question {idx + 1}/{total}</b>\n\n{html.escape(question.text)}\n\n{outcome}",
            )
            if quiz.cancelled:
                break
            await asyncio.sleep(3)
        quiz.current = None
        await finish_quiz(bot_obj, quiz)
    except Exception:
        log.exception("Quiz %s failed", quiz.id)
        _prepared_finish(setup.starter_tg_id, setup.code, "done", f"stopped by an error · {setup.chat_title}")
        quiz.current = None
        QUIZZES.pop(quiz.id, None)
        try:
            await bot_obj.send_message(setup.chat_id, "⚠️ The quiz stopped because of an error. No prizes were sent.")
        except Exception:
            pass


async def finish_quiz(bot_obj, quiz: events.Quiz) -> None:
    """Announce the winners and send every prize. Everybody who played has started the bot, so they all have a wallet."""
    setup = quiz.setup
    cancelled = quiz.cancelled
    head = (f"🧠 <b>Quiz by {html.escape(setup.starter_label)} was cancelled.</b>" if cancelled
            else f"🧠 <b>Quiz by {html.escape(setup.starter_label)} is over!</b>")
    status = "cancelled" if cancelled else "done"
    if setup.style == "top":
        ranked = events.rank_players(quiz.players)[: setup.winners]
        owed = [(p.tg_id, p.label, setup.amounts[n], f" · {p.correct} correct") for n, p in enumerate(ranked)]
    else:
        owed = [(w.tg_id, w.label, won, f" · {count} won") for w, count, won in events.wins_by_user(quiz.wins)]
    QUIZZES.pop(quiz.id, None)

    async def post(text: str) -> None:
        try:
            await bot_obj.send_message(setup.chat_id, text)
        except Exception:
            log.exception("Could not post the quiz result")

    if not owed:
        _prepared_finish(setup.starter_tg_id, setup.code, status, f"nobody won · {setup.chat_title}")
        return await post(f"{head}\nNobody got a question right." if setup.style == "top" else f"{head}\nNobody won a prize.")
    groups, lines = [], []
    ops = OPS.get(setup.slug)
    async with SESSIONS() as session:
        for tg_id, label, raw, note in owed:
            user = await service.find_user_by_telegram_id(session, tg_id)
            wallet = await service.get_wallet(session, user, ops.key_type) if user is not None and ops is not None else None
            if wallet is None:
                lines.append(f"⚠️ {html.escape(label)}: no wallet found, nothing sent")
                continue
            groups.append(((user.id, label, wallet.address), raw, note))
    paid = 0
    if groups:
        try:
            more, paid = await _payout(bot_obj, setup.slug, setup.starter_user_id, setup.starter_tg_id, setup.token_id, groups)
            lines += more
        except Exception:
            log.exception("Quiz payout failed")
            lines.append("❌ Something went wrong while sending the prizes. Check the organizer's history.")
    _prepared_finish(setup.starter_tg_id, setup.code, status, f"{paid}/{len(owed)} paid · {setup.chat_title}")
    await post(f"{head}\n\n" + "\n".join(lines))


# ------------------------------------------------------------------------ startup
# ------------------------------------------------------------------------ airdrops (tasks + rewards)
# The rules and the screens' text live in campaigns.py. This part shows the screens, asks Telegram who joined a
# channel, and sends the rewards with the same safe payout used by rain and quiz.
CAMPAIGNS: dict[str, campaigns.Campaign] = campaigns.load()
AIR_PAYING: set = set()
AIR_BACK = [InlineKeyboardButton(text="⬅️ Airdrops", callback_data="air"), InlineKeyboardButton(text="🏠 Home", callback_data="home")]
AIR_INTRO = (
    "🪂 <b>Airdrops</b>\n\n"
    "Run a timed event where people finish tasks (join your channel, follow you on X or Instagram, visit your "
    "website) and earn token rewards. Or join one that is running now.\n\n"
    "Tap <b>Create an airdrop</b> to make one, or <b>Active airdrops</b> to take part."
)


class AirFlow(StatesGroup):
    title = State()
    desc = State()
    duration = State()
    comp_each = State()
    comp_max = State()
    top_amounts = State()
    task_target = State()
    task_answer = State()
    banner = State()
    tz = State()
    bal_amount = State()
    edit_title = State()
    edit_desc = State()
    start_in = State()
    end_in = State()
    banner_edit = State()
    raffle_each = State()
    raffle_n = State()
    handle = State()


def _air_save() -> None:
    campaigns.save(CAMPAIGNS)


def _air_label(tg) -> str:
    return f"@{tg.username}" if tg.username else (tg.full_name or "someone")


def _air_fmt(c: campaigns.Campaign):
    return lambda raw: service.format_amount(raw, c.decimals)


def _air_plain(text: str) -> str:
    import re
    return re.sub(r"<[^>]+>", "", text or "")[:120]


async def _air_link(bot_obj, c: campaigns.Campaign) -> str:
    me = await bot_obj.me()
    return f"https://t.me/{me.username}?start=air_{c.id}"


def _air_restore() -> None:
    """After a restart: a payment that was in progress may or may not have gone out, so it is never repeated by itself."""
    changed = False
    for c in CAMPAIGNS.values():
        for due in c.dues:
            if due.status == "paying":
                due.status, due.note, changed = "failed", "interrupted by a restart: check your history before paying again", True
    if changed:
        _air_save()


def _air_reserved(creator_tg_id: int, token_id: int, except_id: str = "") -> int:
    """What the creator's other funded airdrops with this token still have to pay."""
    return sum(
        c.unpaid_budget() for c in CAMPAIGNS.values()
        if c.id != except_id and c.creator_tg_id == creator_tg_id and c.token_id == token_id
        and c.funding == "funded" and c.state in ("draft", "open", "ended")
    )


async def _air_funds_error(c: campaigns.Campaign) -> str | None:
    total = c.budget() + _air_reserved(c.creator_tg_id, c.token_id, c.id)
    each = max([c.comp_each, c.raffle_each] + list(c.top_prizes))
    return await _funds_error(c.slug, c.creator_user_id, c.token_id, each, total)


async def _air_tell(bot_obj, tg_id: int, text: str, markup=None) -> bool:
    try:
        await bot_obj.send_message(tg_id, text, reply_markup=markup)
        return True
    except Exception:
        return False


async def _air_pay_due(bot_obj, c: campaigns.Campaign, due: campaigns.Due) -> bool:
    """Send one reward from the creator's wallet. Never twice: it is marked 'paying' before anything is sent."""
    key = (c.id, id(due))
    if due.status == "paid" or key in AIR_PAYING:
        return due.status == "paid"
    AIR_PAYING.add(key)
    try:
        due.status = "paying"
        _air_save()
        ops = OPS.get(c.slug)
        if ops is None:
            due.status, due.note = "failed", "that network is not available right now"
            return False
        async with SESSIONS() as session:
            person = await service.find_user_by_telegram_id(session, due.tg_id)
            wallet = await service.get_wallet(session, person, ops.key_type) if person else None
            target = (person.id, wallet.address) if wallet else None
        if target is None:
            due.status, due.note = "failed", "this person has no wallet in the bot yet"
            return False
        lines, paid = await _payout(
            bot_obj, c.slug, c.creator_user_id, c.creator_tg_id, c.token_id, [((target[0], due.label, target[1]), due.raw, "")]
        )
        if paid:
            due.status, due.note = "paid", ""
            return True
        due.status, due.note = "failed", _air_plain(lines[0] if lines else "not sent")
        return False
    except Exception:
        log.exception("Airdrop payout failed")
        due.status, due.note = "failed", "unexpected error, check your history before paying again"
        return False
    finally:
        AIR_PAYING.discard(key)
        _air_save()


async def _air_progress(bot_obj, c: campaigns.Campaign, person: campaigns.Participant) -> str:
    """After a task is completed: the completion reward, if this person earned it. Returns a line for them."""
    due = c.claim_completion(person, time.time())
    _air_save()
    if due is None:
        if c.raffle_mode != "none" and c.all_done(person):
            return "\n\n🎲 You finished every task, so you are in the lucky draw when it ends."
        return ""
    sym = html.escape(c.symbol)
    amount = f"{service.format_amount(due.raw, c.decimals)} {sym}"
    if c.comp_mode == "instant":
        if await _air_pay_due(bot_obj, c, due):
            return f"\n\n🎉 You finished every task! <b>{amount}</b> was sent to your wallet."
        await _air_tell(bot_obj, c.creator_tg_id,
                        f"⚠️ Could not pay {html.escape(due.label)} ({amount}) in <b>{html.escape(c.title)}</b>: "
                        f"{html.escape(due.note)}\nOpen 💎 Earn → 🪂 Airdrops → your airdrop → 💸 To pay to try again.")
        return f"\n\n🎉 You finished every task! <b>{amount}</b> is yours. The payment could not be sent yet, the creator has been told."
    await _air_tell(bot_obj, c.creator_tg_id,
                    f"🎉 {html.escape(due.label)} finished every task in <b>{html.escape(c.title)}</b>. "
                    f"Their reward of {amount} is waiting for you under 💸 To pay.")
    return f"\n\n🎉 You finished every task! <b>{amount}</b> will be sent to you by the creator."


async def _air_announce_winners(bot_obj, c: campaigns.Campaign) -> None:
    """After a draw or first-come airdrop ends: send the winner list to everyone who took part and to the creator."""
    if c.raffle_mode == "none" and c.comp_mode == "none":
        return
    lines = campaigns.winners_lines(c, _air_fmt(c))
    head = f"🏁 <b>{html.escape(c.title)}</b> has ended.\n{len(c.participants)} people took part, {len(c.finishers())} finished every task."
    if not lines:
        lines = ["Nobody finished every task, so there were no winners."]
    chunks, cur = [], head + "\n"
    for line in lines:
        if len(cur) + len(line) > 3700:
            chunks.append(cur)
            cur = ""
        cur += "\n" + line
    chunks.append(cur)
    chunks[-1] += "\n\nOnly the people in this winner list receive a reward."
    markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="👥 Participants & winners", callback_data=f"aib:{c.id}")]])
    for tg_id in dict.fromkeys([c.creator_tg_id, *c.participants.keys()]):
        for i, chunk in enumerate(chunks):
            await _air_tell(bot_obj, tg_id, chunk, markup if i == len(chunks) - 1 else None)
        await asyncio.sleep(0.05)


async def _air_finish(bot_obj, c: campaigns.Campaign) -> None:
    """The airdrop has ended: pick the winners, pay them if automatic, and tell everyone concerned."""
    made = c.settle_top()
    _air_save()
    fmt = _air_fmt(c)
    sym = html.escape(c.symbol)

    def automatic(due) -> bool:
        return (due.kind == "top" and c.top_mode == "auto") or (due.kind == "raffle" and c.raffle_mode == "auto")

    for due in made:
        if automatic(due):
            await _air_pay_due(bot_obj, c, due)
    for due in made:
        status = ("it was sent to your wallet" if due.status == "paid"
                  else "the creator will send it" if not automatic(due) else "it could not be sent yet, the creator has been told")
        what = "You finished <b>#%d</b> on the leaderboard and won" % due.rank if due.kind == "top" else "You won the <b>lucky draw</b>:"
        await _air_tell(bot_obj, due.tg_id,
                        f"🏆 <b>{html.escape(c.title)}</b> has ended. {what} <b>{fmt(due.raw)} {sym}</b>: {status}.")
    await _air_announce_winners(bot_obj, c)
    owed = len(c.owed())
    auto_paid = sum(1 for d in made if d.status == "paid")
    summary = (f"🏁 <b>{html.escape(c.title)}</b> has ended.\n{len(c.participants)} people took part, {len(c.finishers())} finished every task."
               + (f"\n{len(made)} prize(s) decided, {auto_paid} paid automatically." if made else "")
               + (f"\n💸 {owed} reward(s) still to pay." if owed else ""))
    rows = [[InlineKeyboardButton(text=f"💸 To pay ({owed})", callback_data=f"aidue:{c.id}")]] if owed else []
    rows.append([InlineKeyboardButton(text="🏆 Leaderboard" if c.uses_points() else "👥 Participants & winners", callback_data=f"aib:{c.id}")])
    await _air_tell(bot_obj, c.creator_tg_id, summary, InlineKeyboardMarkup(inline_keyboard=rows))


async def _air_admins() -> set:
    """Who may approve an announcement: AIRDROP_ADMINS from .env, and the support user (if they have opened the bot)."""
    ids = set(SETTINGS.airdrop_admin_ids)
    names = {n.lower() for n in (SETTINGS.support_username, *SETTINGS.support_extra, *SETTINGS.airdrop_admin_usernames) if n}
    async with SESSIONS() as session:
        for name in names:
            person = await service.find_user_by_username(session, name)
            if person is not None:
                ids.add(person.telegram_id)
    return ids


async def _air_request_broadcast(bot_obj, c: campaigns.Campaign) -> None:
    """A new airdrop is always listed in the Airdrops menu. Telling every user about it needs the team's OK."""
    if c.broadcast != "none":
        return
    admins = await _air_admins()
    c.broadcast = "pending"
    _air_save()
    text = (f"🔔 <b>New airdrop</b> from {html.escape(c.creator_label)}\n\n"
            + campaigns.event_text(c, None, _air_fmt(c), time.time()) + "\n\nNotify <b>every user</b> of this bot about it, or skip? "
            "(Skipping keeps it in the Airdrops list.)")
    markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📣 Notify everyone", callback_data=f"aibc:ok:{c.id}"),
                                                    InlineKeyboardButton(text="⏭ Skip", callback_data=f"aibc:no:{c.id}")]])
    reached = 0
    c.review_msgs = []
    for admin in admins:
        try:
            if c.banner:
                await bot_obj.send_photo(admin, c.banner)
            sent = await bot_obj.send_message(admin, text[:4000], reply_markup=markup)
            c.review_msgs.append([admin, sent.message_id])
            reached += 1
        except Exception:
            log.warning("Airdrop %s: could not reach team member %s", c.id, admin)
    if reached:
        await _air_tell(bot_obj, c.creator_tg_id, f"📣 <b>{html.escape(c.title)}</b> was sent to our team. If they approve, every user gets a notification. "
                                                  "Either way it is already in 💎 Earn → 🪂 Airdrops for anyone to join.")
    else:
        c.broadcast = "none"
        _air_save()
        log.warning("Airdrop %s: no admin could be reached to approve the announcement", c.id)
        await _air_tell(bot_obj, c.creator_tg_id, f"ℹ️ <b>{html.escape(c.title)}</b> is in 💎 Earn → 🪂 Airdrops. Our team could not be reached to announce it to everyone yet.")


async def _air_broadcast(bot_obj, c: campaigns.Campaign, report_to: list) -> None:
    async with SESSIONS() as session:
        ids = [i for i in await service.all_telegram_ids(session) if i != c.creator_tg_id]
    now = time.time()
    text = "🪂 <b>New airdrop!</b>\n\n" + campaigns.list_card(c, _air_fmt(c), now, None) + "\n\nTap to see the tasks and rewards."
    markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🪂 Open airdrop", callback_data=f"aiv:{c.id}")]])
    ok = failed = 0
    for tg_id in ids:
        for attempt in (1, 2):
            try:
                if c.banner:
                    await bot_obj.send_photo(tg_id, c.banner, caption=text[:1000], reply_markup=markup)
                else:
                    await bot_obj.send_message(tg_id, text, reply_markup=markup)
                ok += 1
            except TelegramRetryAfter as exc:
                await asyncio.sleep(exc.retry_after + 1)
                continue
            except Exception:
                failed += 1
            break
        await asyncio.sleep(0.05)  # stay well under Telegram's sending limits
    c.broadcast = "sent"
    _air_save()
    for who in report_to:
        await _air_tell(bot_obj, who, f"📣 <b>{html.escape(c.title)}</b> was announced to {ok} people ({failed} could not be reached).")


BROADCAST_TASKS: set = set()


async def _air_close_reviews(bot_obj, c: campaigns.Campaign, line: str) -> None:
    """After one team member decides, every copy of the request shows the result and loses its buttons."""
    for chat_id, message_id in c.review_msgs:
        try:
            await bot_obj.edit_message_text(f"{line}\n\n{html.escape(c.title)}", chat_id=chat_id, message_id=message_id)
        except Exception:
            pass
    c.review_msgs = []
    _air_save()


@router.callback_query(F.data.regexp(r"^aibc:(ok|no):[0-9a-f]{6}$"))
async def air_broadcast_decide(cb: CallbackQuery):
    _tag, choice, cid = cb.data.split(":")
    if cb.from_user.id not in await _air_admins():
        return await cb.answer("Only the team can decide this.", show_alert=True)
    c = CAMPAIGNS.get(cid)
    if c is None or c.broadcast != "pending":
        return await cb.answer("Already decided.", show_alert=True)
    who = f"@{cb.from_user.username}" if cb.from_user.username else cb.from_user.full_name
    if choice == "no":
        c.broadcast = "rejected"
        _air_save()
        await cb.answer("Skipped")
        await _air_close_reviews(cb.bot, c, f"⏭ Skipped by {html.escape(who)}. It stays in the Airdrops list.")
        return await _air_tell(cb.bot, c.creator_tg_id, f"ℹ️ <b>{html.escape(c.title)}</b> will not be announced to everyone, but it stays in 💎 Earn → 🪂 Airdrops and people can still join.")
    c.broadcast = "approved"
    _air_save()
    await cb.answer("Notifying everyone…")
    await _air_close_reviews(cb.bot, c, f"📣 Notifying everyone (decided by {html.escape(who)}).")
    await _air_tell(cb.bot, c.creator_tg_id, f"✅ <b>{html.escape(c.title)}</b> was approved. Everyone is being notified now.")
    task = asyncio.create_task(_air_broadcast(cb.bot, c, list({*await _air_admins(), c.creator_tg_id})))
    BROADCAST_TASKS.add(task)
    task.add_done_callback(BROADCAST_TASKS.discard)


async def _air_loop(bot_obj) -> None:
    """Every few seconds: end the airdrops whose time is up."""
    while True:
        try:
            now = time.time()
            for c in list(CAMPAIGNS.values()):
                if c.finish_if_due(now):
                    _air_save()
                if c.state == "ended" and not c.top_settled:
                    await _air_finish(bot_obj, c)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Airdrop check failed")
        await asyncio.sleep(20)


# ---- menu and lists
@router.callback_query(F.data == "air")
async def air_menu(event: Message | CallbackQuery, state: FSMContext):
    await state.clear()
    if not _private(event):
        return await _done(event, PRIVATE_ONLY, alert=True)
    await _done(event)
    now = time.time()
    live = [c for c in CAMPAIGNS.values() if c.is_active(now)]
    mine = [c for c in CAMPAIGNS.values() if c.creator_tg_id == event.from_user.id]
    soon = [c for c in CAMPAIGNS.values() if c.is_upcoming(now)]
    rows = [[InlineKeyboardButton(text=f"🔎 Active airdrops ({len(live)})", callback_data="airlist")]]
    if soon:
        rows.append([InlineKeyboardButton(text=f"🗓 Upcoming ({len(soon)})", callback_data="airup")])
    rows.append([InlineKeyboardButton(text="➕ Create an airdrop", callback_data="aicr")])
    if mine:
        rows.append([InlineKeyboardButton(text=f"📋 My airdrops ({len(mine)})", callback_data="airmine")])
    rows.append([InlineKeyboardButton(text="❓ How it works", callback_data="help:airdrop"),
                 InlineKeyboardButton(text="⬅️ Back", callback_data="earn")])
    await _show(event, AIR_INTRO, InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.in_({"airlist", "airup"}))
async def air_list(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await _done(cb)
    upcoming = cb.data == "airup"
    now = time.time()
    pick = (lambda c: c.is_upcoming(now)) if upcoming else (lambda c: c.is_active(now))
    live = sorted((c for c in CAMPAIGNS.values() if pick(c)), key=lambda c: c.starts if upcoming else c.ends)[:8]
    title = "🗓 <b>Upcoming airdrops</b>" if upcoming else "🔎 <b>Active airdrops</b>"
    if not live:
        return await _show(cb, f"{title}\n\nNothing here right now.", InlineKeyboardMarkup(inline_keyboard=[AIR_BACK]))
    cards = "\n\n".join(campaigns.list_card(c, _air_fmt(c), now, i) for i, c in enumerate(live, start=1))
    rows = [[InlineKeyboardButton(text=f"{i}. {c.title[:22]} {campaigns.reward_icons(c)} · "
                                       + (campaigns.format_left(c.starts - now).replace(" left", "") if upcoming else campaigns.format_left(c.ends - now)),
                                  callback_data=f"aiv:{c.id}")] for i, c in enumerate(live, start=1)]
    await _show(cb, f"{title} ({len(live)})\n\n{cards}\n\n⚡ first come, first served · 🎲 lucky draw · 🏆 leaderboard\nTap one to see its tasks.",
                InlineKeyboardMarkup(inline_keyboard=rows + [AIR_BACK]))


@router.callback_query(F.data == "airmine")
async def air_mine(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await _done(cb)
    now = time.time()
    mine = sorted((c for c in CAMPAIGNS.values() if c.creator_tg_id == cb.from_user.id), key=lambda c: -c.created)[:12]
    icons = {"draft": "📝", "open": "🟢", "ended": "🏁", "cancelled": "🚫"}
    rows = [[InlineKeyboardButton(text=f"{icons[c.state]} {campaigns.list_line(c, now)}", callback_data=f"aim:{c.id}")] for c in mine]
    await _show(cb, "📋 <b>My airdrops</b>", InlineKeyboardMarkup(inline_keyboard=rows + [AIR_BACK]))


def _air_get(cb: CallbackQuery, cid: str, creator: bool = False):
    """The airdrop, or None (the person is told why). creator=True means only its creator may do this."""
    c = CAMPAIGNS.get(cid)
    if c is None:
        return None
    if creator and c.creator_tg_id != cb.from_user.id:
        return None
    return c


async def _air_missing(cb: CallbackQuery) -> None:
    await cb.answer("This airdrop is not available.", show_alert=True)


async def _air_new_person(bot_obj, c: campaigns.Campaign, person: campaigns.Participant) -> None:
    """Tell the creator about a new person: every one of the first 30, then every 10th."""
    n = len(c.participants)
    if n <= 30 or n % 10 == 0:
        await _air_tell(bot_obj, c.creator_tg_id, f"👤 {html.escape(person.label)} joined <b>{html.escape(c.title)}</b> · {n} taking part",
                        InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="👥 See everyone", callback_data=f"aipp:{c.id}:0")]]))


async def _air_screen(event, c: campaigns.Campaign, text: str, markup) -> None:
    """Show an airdrop screen. With a banner it is a picture message; a long text goes in a message after the picture."""
    if not c.banner:
        return await _show(event, text, markup)
    target = event.message if isinstance(event, CallbackQuery) else event
    try:
        if len(text) <= 1000:  # a picture's caption can hold 1000 characters
            await target.answer_photo(c.banner, caption=text, reply_markup=markup)
        else:
            await target.answer_photo(c.banner, caption=f"🪂 <b>{html.escape(c.title)}</b>")
            await target.answer(text, reply_markup=markup)
    except TelegramBadRequest:
        log.warning("Airdrop %s: its banner could not be sent", c.id)
        return await _show(event, text, markup)
    if isinstance(event, CallbackQuery):
        try:
            await event.message.delete()
        except Exception:
            pass


# ---- a person taking part
async def _air_show_event(event, c: campaigns.Campaign) -> None:
    tg_id = event.from_user.id
    if tg_id == c.creator_tg_id:
        return await _air_show_manage(event, c)
    now = time.time()
    person = c.participant(tg_id)
    rows = []
    if c.is_active(now):
        for task in c.tasks:
            icon = campaigns.task_status_icon(person.entries.get(task.id) if person else None)
            rows.append([InlineKeyboardButton(text=f"{icon} {campaigns.KINDS[task.kind][0]} {task.title[:40]}", callback_data=f"ait:{c.id}:{task.id}")])
    if c.is_active(now) and person is not None and any(
            t.kind in ("tg:join", "ref:invite", "bal:min") and not (person.entries.get(t.id) and person.entries[t.id].status == "done") for t in c.tasks):
        rows.append([InlineKeyboardButton(text="✅ Verify all", callback_data=f"aiva:{c.id}")])
    rows.append([InlineKeyboardButton(text="🏆 Leaderboard" if c.uses_points() else "👥 Participants & winners", callback_data=f"aib:{c.id}"),
                 InlineKeyboardButton(text="🔄 Refresh", callback_data=f"aiv:{c.id}")])
    await _air_screen(event, c, campaigns.event_text(c, person, _air_fmt(c), now), InlineKeyboardMarkup(inline_keyboard=rows + [AIR_BACK]))


@router.callback_query(F.data.regexp(r"^aiv:[0-9a-f]{6}$"))
async def air_view(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    c = _air_get(cb, cb.data.split(":")[1])
    if c is None:
        return await _air_missing(cb)
    await _done(cb)
    await _air_show_event(cb, c)


@router.callback_query(F.data.regexp(r"^aib:[0-9a-f]{6}(:\d+)?$"))
async def air_board(cb: CallbackQuery):
    parts = cb.data.split(":")
    c = _air_get(cb, parts[1])
    if c is None:
        return await _air_missing(cb)
    await _done(cb)
    page = int(parts[2]) if len(parts) > 2 else 0
    pages = campaigns.board_pages(c)
    page = min(page, pages - 1)
    back = "aim" if cb.from_user.id == c.creator_tg_id else "aiv"
    rows = []
    if pages > 1:
        rows.append([InlineKeyboardButton(text="◀", callback_data=f"aib:{c.id}:{(page - 1) % pages}"),
                     InlineKeyboardButton(text=f"{page + 1} / {pages}", callback_data=f"aib:{c.id}:{page}"),
                     InlineKeyboardButton(text="▶", callback_data=f"aib:{c.id}:{(page + 1) % pages}")])
    rows.append([InlineKeyboardButton(text="🔄 Refresh", callback_data=f"aib:{c.id}:{page}"),
                 InlineKeyboardButton(text="⬅️ Back", callback_data=f"{back}:{c.id}")])
    await _show(cb, campaigns.board_text(c, _air_fmt(c), cb.from_user.id, page=page), InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.regexp(r"^ait:[0-9a-f]{6}:\d+$"))
async def air_task(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    _tag, cid, tid = cb.data.split(":")
    c = _air_get(cb, cid)
    task = c.task(int(tid)) if c else None
    if c is None or task is None:
        return await _air_missing(cb)
    now = time.time()
    if c.is_upcoming(now):
        return await cb.answer(f"It starts in {campaigns.format_left(c.starts - now).replace(' left', '')}.", show_alert=True)
    if not c.is_active(now):
        return await cb.answer("This airdrop is not running.", show_alert=True)
    if cb.from_user.id == c.creator_tg_id:
        return await cb.answer("You made this airdrop, so you can't take part in it.", show_alert=True)
    session, _user = await _load(cb)  # creates their wallet, so a reward can reach them
    await session.close()
    is_new = cb.from_user.id not in c.participants
    person = c.join(cb.from_user.id, _air_label(cb.from_user))
    _air_save()
    if is_new:
        await _air_new_person(cb.bot, c, person)
    await _done(cb)
    entry = person.entries.get(task.id)
    rows = [[InlineKeyboardButton(text="🔗 Open", url=task.url)]] if task.url else []
    invites = c.invite_count(person.tg_id) if task.kind == "ref:invite" else (
        len(person.checkins.get(task.id, [])) if task.kind == "rc:daily" else None)
    if task.kind == "rc:daily" and (entry is None or entry.status != "done"):
        rows.append([InlineKeyboardButton(text="✅ Check in today", callback_data=f"aick:{cid}:{tid}")])
    elif task.kind == "bal:min" and (entry is None or entry.status != "done"):
        rows.append([InlineKeyboardButton(text="✅ Verify balance", callback_data=f"aitv:{cid}:{tid}")])
    elif task.kind == "ref:invite" and (entry is None or entry.status != "done"):
        rows.append([InlineKeyboardButton(text="📨 My invite link", callback_data=f"aiin:{cid}"),
                     InlineKeyboardButton(text="🔄 Check my invites", callback_data=f"aitv:{cid}:{tid}")])
    elif entry is None or entry.status == "rejected":
        if task.kind == "tg:join":
            rows.append([InlineKeyboardButton(text="✅ Verify", callback_data=f"aitv:{cid}:{tid}")])
        elif task.kind == "web:visit" and task.verify == "instant":
            rows.append([InlineKeyboardButton(text="✅ I visited it", callback_data=f"aitv:{cid}:{tid}")])
        elif task.kind == "qz:answer":
            rows.append([InlineKeyboardButton(text="✍️ Answer", callback_data=f"aith:{cid}:{tid}")])
        else:
            needs_handle = campaigns.KINDS[task.kind][2]
            rows.append([InlineKeyboardButton(text="✍️ Send my username" if needs_handle else "✍️ Send a note", callback_data=f"aith:{cid}:{tid}")])
    rows.append([InlineKeyboardButton(text="⬅️ Back", callback_data=f"aiv:{cid}")])
    await _show(cb, campaigns.task_text(c, task, entry, invites), InlineKeyboardMarkup(inline_keyboard=rows))


async def _air_auto_check(bot_obj, c: campaigns.Campaign, person: campaigns.Participant, task: campaigns.Task) -> tuple[bool, str]:
    """Tasks the bot can check by itself. Returns (done, why not)."""
    if task.kind == "tg:join":
        try:
            member = await bot_obj.get_chat_member(task.chat, person.tg_id)
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            log.warning("Airdrop membership check failed for %s: %r", task.chat, exc)
            return False, "I can't check that chat right now. The creator needs to make me an admin there."
        joined = member.status in ("member", "administrator", "creator") or (member.status == "restricted" and getattr(member, "is_member", False))
        return joined, ("" if joined else "I can't see you in it yet. Join it first, then try again.")
    if task.kind == "ref:invite":
        have = c.invite_count(person.tg_id)
        return have >= task.goal, f"{have} of {task.goal} friends so far. A friend counts once they finish a task."
    if task.kind == "bal:min":
        ops_slug = None
        try:
            async with SESSIONS() as session:
                token = await session.get(Token, task.token_id)
                chain = await session.get(Chain, token.chain_id) if token else None
                if chain is None or chain.slug not in OPS:
                    return False, "That network is not available right now."
                ops = OPS[chain.slug]
                user = await service.find_user_by_telegram_id(session, person.tg_id)
                wallet = await service.get_wallet(session, user, ops.key_type) if user else None
                if wallet is None:
                    return False, "You have no wallet here yet."
                held = (await ops.balances(wallet.address, await _balance_tokens(session, chain, token))).get(ops.token_key(token))
                need, dec, sym = task.goal, token.decimals, token.symbol
        except Exception:
            log.exception("Airdrop balance check failed")
            return False, "I can't read your balance right now. Try again in a moment."
        if held is None:
            return False, "I can't read your balance right now. Try again in a moment."
        return int(held) >= need, f"You have {service.format_amount(int(held), dec)} {sym}. You need {service.format_amount(need, dec)}."
    if task.kind == "web:visit" and task.verify == "instant":
        return True, ""
    return False, "Send your username first."


@router.callback_query(F.data.regexp(r"^aitv:[0-9a-f]{6}:\d+$"))
async def air_verify(cb: CallbackQuery):
    _tag, cid, tid = cb.data.split(":")
    c = _air_get(cb, cid)
    task = c.task(int(tid)) if c else None
    person = c.participant(cb.from_user.id) if c else None
    if c is None or task is None or person is None or not c.is_active(time.time()):
        return await cb.answer("This airdrop is not running.", show_alert=True)
    entry = person.entries.get(task.id)
    if entry is not None and entry.status in ("done", "pending"):
        return await cb.answer("Already sent.", show_alert=True)
    ok, why = await _air_auto_check(cb.bot, c, person, task)
    if not ok:
        return await cb.answer(why, show_alert=True)
    c.mark(person, task.id, "done")
    _air_save()
    await cb.answer("✅ Done!" + (f" +{task.points} points" if c.uses_points() else ""))
    extra = await _air_progress(cb.bot, c, person)
    if extra:
        await cb.message.answer(extra.strip())
    await _air_show_event(cb, c)


@router.callback_query(F.data.regexp(r"^aick:[0-9a-f]{6}:\d+$"))
async def air_check_in(cb: CallbackQuery):
    _tag, cid, tid = cb.data.split(":")
    c = _air_get(cb, cid)
    task = c.task(int(tid)) if c else None
    person = c.participant(cb.from_user.id) if c else None
    if c is None or task is None or task.kind != "rc:daily" or person is None or not c.is_active(time.time()):
        return await cb.answer("This airdrop is not running.", show_alert=True)
    days, again = c.check_in(person, task, time.time())
    _air_save()
    if again:
        return await cb.answer(f"You already checked in today ({days} of {task.goal}). Come back tomorrow.", show_alert=True)
    await cb.answer(f"✅ Checked in: {days} of {task.goal}")
    if days >= task.goal:
        extra = await _air_progress(cb.bot, c, person)
        if extra:
            await cb.message.answer(extra.strip())
    await _air_show_event(cb, c)


@router.callback_query(F.data.regexp(r"^aiva:[0-9a-f]{6}$"))
async def air_verify_all(cb: CallbackQuery):
    """TaskOn's 'Verify all': check every task the bot can check, in one tap."""
    c = _air_get(cb, cb.data.split(":")[1])
    person = c.participant(cb.from_user.id) if c else None
    if c is None or person is None or not c.is_active(time.time()):
        return await cb.answer("Open a task first, then you can verify them all.", show_alert=True)
    await cb.answer("⏳ Checking…")
    got, waiting = 0, []
    for task in c.tasks:
        entry = person.entries.get(task.id)
        if task.kind not in ("tg:join", "ref:invite", "bal:min") or (entry is not None and entry.status == "done"):
            continue
        ok, why = await _air_auto_check(cb.bot, c, person, task)
        if ok:
            c.mark(person, task.id, "done")
            got += 1
        else:
            waiting.append(task.title)
    _air_save()
    summary = (f"✅ {got} task(s) verified." if got else "Nothing new to verify.") + (f"\n⏳ Not yet: {', '.join(waiting)[:120]}" if waiting else "")
    await cb.message.answer(summary)
    if got:
        extra = await _air_progress(cb.bot, c, person)
        if extra:
            await cb.message.answer(extra.strip())
    await _air_show_event(cb, c)


@router.callback_query(F.data.regexp(r"^aiin:[0-9a-f]{6}$"))
async def air_invite_link(cb: CallbackQuery):
    c = _air_get(cb, cb.data.split(":")[1])
    if c is None or c.participant(cb.from_user.id) is None:
        return await _air_missing(cb)
    await _done(cb)
    me = await cb.bot.me()
    link = f"https://t.me/{me.username}?start=air_{c.id}_{cb.from_user.id}"
    await cb.message.answer(f"📨 Your personal invite link for <b>{html.escape(c.title)}</b>. Send it to friends:\n\n{html.escape(link)}\n\n"
                            "A friend counts once they open it and finish at least one task.")


@router.callback_query(F.data.regexp(r"^aith:[0-9a-f]{6}:\d+$"))
async def air_handle_ask(cb: CallbackQuery, state: FSMContext):
    _tag, cid, tid = cb.data.split(":")
    c = _air_get(cb, cid)
    task = c.task(int(tid)) if c else None
    person = c.participant(cb.from_user.id) if c else None
    if c is None or task is None or person is None or not c.is_active(time.time()):
        return await cb.answer("This airdrop is not running.", show_alert=True)
    entry = person.entries.get(task.id)
    if entry is not None and entry.status in ("done", "pending"):
        return await cb.answer("Already sent.", show_alert=True)
    await _done(cb)
    await state.clear()
    await state.set_state(AirFlow.handle)
    await state.update_data(cid=cid, tid=int(tid))
    plat = task.kind.split(":")[0]
    if task.kind == "qz:answer":
        ask = f"❓ <b>{html.escape(task.title)}</b>\n\nType your answer."
    elif campaigns.KINDS[task.kind][2]:
        where = campaigns.PLATFORMS[plat][1]
        ask = f"Send your <b>{html.escape(where)}</b> username" + (", like <code>@yourname</code>." if plat in ("x", "ig") else ".")
    else:
        ask = "Send a short note (your username on that site, or what you did)."
    await _show(cb, f"{ask}\n/cancel stops this.", InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Back", callback_data=f"ait:{cid}:{tid}")]]))


@router.message(AirFlow.handle, F.text)
async def air_handle_in(message: Message, state: FSMContext):
    data = await state.get_data()
    c = CAMPAIGNS.get(data.get("cid", ""))
    task = c.task(data.get("tid", 0)) if c else None
    person = c.participant(message.from_user.id) if c else None
    if c is None or task is None or person is None or not c.is_active(time.time()):
        await state.clear()
        return await message.answer("This airdrop is not running.")
    plat = task.kind.split(":")[0]
    if task.kind == "qz:answer":  # the bot checks the answer itself
        if campaigns.norm_answer(message.text) != campaigns.norm_answer(task.answer):
            return await message.answer("❌ Not correct. Try again, or /cancel.")
        await state.clear()
        c.mark(person, task.id, "done", "")
        _air_save()
        await message.answer("✅ Correct!" + (f" +{task.points} points" if c.uses_points() else ""))
        extra = await _air_progress(message.bot, c, person)
        if extra:
            await message.answer(extra.strip())
        return await _air_show_event(message, c)
    try:
        handle = campaigns.clean_text(message.text, 80) if plat == "web" else campaigns.profile_name(plat, message.text)
    except campaigns.CampaignError as exc:
        return await message.answer(f"{html.escape(str(exc))}\nTry again, or /cancel.")
    if plat != "web" and c.handle_taken(task.id, handle, message.from_user.id):
        return await message.answer("Someone else already used that username for this task. Send yours, or /cancel.")
    await state.clear()
    if task.verify == "approve":
        c.mark(person, task.id, "pending", handle)
        _air_save()
        await message.answer(f"⏳ Sent. The creator will check <b>{html.escape(handle)}</b> and approve you.")
        await _air_tell(message.bot, c.creator_tg_id,
                        f"🔍 <b>{html.escape(c.title)}</b>: {html.escape(person.label)} sent {html.escape(handle)} for "
                        f"“{html.escape(task.title)}”. Check it, then choose:",
                        InlineKeyboardMarkup(inline_keyboard=[[
                            InlineKeyboardButton(text="✅ Approve", callback_data=f"aiap:{c.id}:{person.tg_id}:{task.id}"),
                            InlineKeyboardButton(text="❌ Reject", callback_data=f"aiaj:{c.id}:{person.tg_id}:{task.id}"),
                        ]]))
        return
    c.mark(person, task.id, "done", handle)
    _air_save()
    await message.answer("✅ Done!" + (f" +{task.points} points" if c.uses_points() else ""))
    extra = await _air_progress(message.bot, c, person)
    if extra:
        await message.answer(extra.strip())
    await _air_show_event(message, c)


# ---- the creator: review, people, payments
@router.callback_query(F.data.regexp(r"^ai(ap|aj):[0-9a-f]{6}:\d+:\d+$"))
async def air_decide(cb: CallbackQuery):
    kind, cid, tg, tid = cb.data.split(":")
    c = _air_get(cb, cid, creator=True)
    task = c.task(int(tid)) if c else None
    person = c.participant(int(tg)) if c else None
    if c is None or task is None or person is None:
        return await cb.answer("Not available any more.", show_alert=True)
    entry = person.entries.get(task.id)
    if entry is None or entry.status != "pending":
        return await cb.answer("Already decided.", show_alert=True)
    if kind == "aiap":
        c.mark(person, task.id, "done", entry.handle)
        _air_save()
        await cb.answer("Approved ✅")
        await _air_tell(cb.bot, person.tg_id, f"✅ <b>{html.escape(c.title)}</b>: “{html.escape(task.title)}” was approved." + (f" +{task.points} points" if c.uses_points() else ""))
        extra = await _air_progress(cb.bot, c, person)
        if extra:
            await _air_tell(cb.bot, person.tg_id, extra.strip())
    else:
        c.mark(person, task.id, "rejected", entry.handle)
        _air_save()
        await cb.answer("Rejected")
        await _air_tell(cb.bot, person.tg_id, f"❌ <b>{html.escape(c.title)}</b>: “{html.escape(task.title)}” was not accepted. Open the airdrop and send it again.")
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass


@router.callback_query(F.data.regexp(r"^airev:[0-9a-f]{6}$"))
async def air_review(cb: CallbackQuery):
    c = _air_get(cb, cb.data.split(":")[1], creator=True)
    if c is None:
        return await _air_missing(cb)
    await _done(cb)
    items = c.pending_reviews()[:6]
    if not items:
        text = f"🔍 <b>{html.escape(c.title)}</b>\n\nNothing waiting for you. ✅"
        rows = []
    else:
        text = f"🔍 <b>{html.escape(c.title)}</b> · {len(c.pending_reviews())} to review\nCheck each username on its site, then approve or reject."
        rows = []
        for person, task, entry in items:
            rows.append([InlineKeyboardButton(text=f"{person.label[:16]} · {entry.handle[:18]} · {task.title[:14]}", url=task.url)])
            rows.append([InlineKeyboardButton(text="✅ Approve", callback_data=f"aiap:{c.id}:{person.tg_id}:{task.id}"),
                         InlineKeyboardButton(text="❌ Reject", callback_data=f"aiaj:{c.id}:{person.tg_id}:{task.id}")])
        rows.append([InlineKeyboardButton(text="🔄 Refresh", callback_data=f"airev:{c.id}")])
    await _show(cb, text, InlineKeyboardMarkup(inline_keyboard=rows + [[InlineKeyboardButton(text="⬅️ Back", callback_data=f"aim:{c.id}")]]))


@router.callback_query(F.data.regexp(r"^aipp:[0-9a-f]{6}(:\d+)?$"))
async def air_people(cb: CallbackQuery):
    parts = cb.data.split(":")
    c = _air_get(cb, parts[1], creator=True)
    if c is None:
        return await _air_missing(cb)
    await _done(cb)
    pages = campaigns.people_pages(c)
    page = min(int(parts[2]) if len(parts) > 2 else 0, pages - 1)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"aipp:{c.id}:{page - 1}"))
    nav.append(InlineKeyboardButton(text="🔄", callback_data=f"aipp:{c.id}:{page}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"aipp:{c.id}:{page + 1}"))
    await _show(cb, campaigns.participants_text(c, page), InlineKeyboardMarkup(inline_keyboard=[
        nav, [InlineKeyboardButton(text="📤 Export CSV", callback_data=f"aiexp:{c.id}"), InlineKeyboardButton(text="⬅️ Back", callback_data=f"aim:{c.id}")]]))


@router.callback_query(F.data.regexp(r"^aiexp:[0-9a-f]{6}$"))
async def air_export(cb: CallbackQuery):
    c = _air_get(cb, cb.data.split(":")[1], creator=True)
    if c is None:
        return await _air_missing(cb)
    await _done(cb, "Preparing the file…")
    wallets: dict = {}
    try:
        async with SESSIONS() as session:
            for tg_id in c.participants:
                person = await service.find_user_by_telegram_id(session, tg_id)
                if person is not None:
                    wallets[tg_id] = [w.address for w in (await session.scalars(select(Wallet).where(Wallet.user_id == person.id)))]
    except Exception:
        log.exception("Could not read wallet addresses for the export")
    data = campaigns.export_csv(c, wallets).encode("utf-8-sig")  # utf-8-sig so Excel reads names correctly
    await cb.message.answer_document(BufferedInputFile(data, filename=f"airdrop-{c.id}.csv"),
                                     caption=f"👥 {len(c.participants)} people · {html.escape(c.title)}")


async def _air_show_dues(cb: CallbackQuery, c: campaigns.Campaign) -> None:
    rows = []
    fmt = _air_fmt(c)
    for index, due in enumerate(c.dues):
        if due.status == "paid" or len(rows) >= 12:
            continue
        rows.append([
            InlineKeyboardButton(text=f"💸 Pay {due.label[:14]} · {fmt(due.raw)}", callback_data=f"aipay:{c.id}:{index}"),
            InlineKeyboardButton(text="✔ Paid", callback_data=f"aipaid:{c.id}:{index}"),
        ])
    if len(c.owed()) > 1:
        rows.append([InlineKeyboardButton(text="💸 Pay everyone now", callback_data=f"aipayall:{c.id}")])
    await _show(cb, campaigns.dues_text(c, fmt) + "\n\n<i>Pay sends it from your wallet. ✔ Paid only marks it, for rewards you sent yourself.</i>",
                InlineKeyboardMarkup(inline_keyboard=rows + [[InlineKeyboardButton(text="⬅️ Back", callback_data=f"aim:{c.id}")]]))


@router.callback_query(F.data.regexp(r"^aidue:[0-9a-f]{6}$"))
async def air_dues(cb: CallbackQuery):
    c = _air_get(cb, cb.data.split(":")[1], creator=True)
    if c is None:
        return await _air_missing(cb)
    await _done(cb)
    await _air_show_dues(cb, c)


@router.callback_query(F.data.regexp(r"^aipay:[0-9a-f]{6}:\d+$"))
async def air_pay(cb: CallbackQuery):
    _tag, cid, index = cb.data.split(":")
    c = _air_get(cb, cid, creator=True)
    if c is None or int(index) >= len(c.dues):
        return await _air_missing(cb)
    due = c.dues[int(index)]
    if due.status == "paid":
        return await cb.answer("Already paid.", show_alert=True)
    await cb.answer("⏳ Sending…")
    ok = await _air_pay_due(cb.bot, c, due)
    if not ok:
        await cb.message.answer(f"⚠️ Could not pay {html.escape(due.label)}: {html.escape(due.note)}")
    await _air_show_dues(cb, c)


@router.callback_query(F.data.regexp(r"^aipaid:[0-9a-f]{6}:\d+$"))
async def air_marked_paid(cb: CallbackQuery):
    _tag, cid, index = cb.data.split(":")
    c = _air_get(cb, cid, creator=True)
    if c is None or int(index) >= len(c.dues):
        return await _air_missing(cb)
    due = c.dues[int(index)]
    if due.status != "paid":
        due.status, due.note = "paid", "marked paid by the creator"
        _air_save()
    await cb.answer("Marked as paid ✔")
    await _air_show_dues(cb, c)


@router.callback_query(F.data.regexp(r"^aipayall:[0-9a-f]{6}$"))
async def air_pay_all(cb: CallbackQuery):
    c = _air_get(cb, cb.data.split(":")[1], creator=True)
    if c is None:
        return await _air_missing(cb)
    await cb.answer("⏳ Sending… this can take a few minutes.")
    for due in list(c.owed()):
        if not await _air_pay_due(cb.bot, c, due):
            await cb.message.answer(f"⚠️ Stopped at {html.escape(due.label)}: {html.escape(due.note)}")
            break
    await _air_show_dues(cb, c)


# ---- the creator: manage
async def _air_show_manage(event, c: campaigns.Campaign) -> None:
    now = time.time()
    rows = []
    if c.state in ("draft", "open"):
        rows.append([InlineKeyboardButton(text="➕ Add a task", callback_data=f"aiadd:{c.id}"),
                     InlineKeyboardButton(text="🖼 Banner", callback_data=f"aibn:{c.id}")])
        rows.append([InlineKeyboardButton(text="✏️ Edit title", callback_data=f"aiet:{c.id}"),
                     InlineKeyboardButton(text="📝 Edit description", callback_data=f"aied:{c.id}")])
        if c.tasks:
            rows.append([InlineKeyboardButton(text="🗑 Remove a task", callback_data=f"airm:{c.id}")])
    if c.state == "draft":
        rows.append([InlineKeyboardButton(text="🗓 Time frame", callback_data=f"aisch:{c.id}"),
                     InlineKeyboardButton(text="🔗 Share link", callback_data=f"aishare:{c.id}")])
        rows.append([InlineKeyboardButton(text="🚀 Publish", callback_data=f"aipub:{c.id}"),
                     InlineKeyboardButton(text="🗑 Delete", callback_data=f"aican:{c.id}")])
    if c.state in ("open", "ended"):
        rows.append([InlineKeyboardButton(text=f"👥 People ({len(c.participants)})", callback_data=f"aipp:{c.id}:0"),
                     InlineKeyboardButton(text="🏆 Leaderboard" if c.uses_points() else "👥 Participants & winners", callback_data=f"aib:{c.id}")])
        rows.append([InlineKeyboardButton(text=f"🔍 Review ({len(c.pending_reviews())})", callback_data=f"airev:{c.id}"),
                     InlineKeyboardButton(text=f"💸 To pay ({len(c.owed())})", callback_data=f"aidue:{c.id}")])
    if c.state == "open":
        rows.append([InlineKeyboardButton(text="🔗 Share link", callback_data=f"aishare:{c.id}")])
        rows.append([InlineKeyboardButton(text="🏁 End now", callback_data=f"aiend:{c.id}"),
                     InlineKeyboardButton(text="🚫 Cancel", callback_data=f"aican:{c.id}")])
    rows.append([InlineKeyboardButton(text="🔄 Refresh", callback_data=f"aim:{c.id}")])
    await _air_screen(event, c, campaigns.manage_text(c, _air_fmt(c), now), InlineKeyboardMarkup(inline_keyboard=rows + [AIR_BACK]))


@router.callback_query(F.data.regexp(r"^aim:[0-9a-f]{6}$"))
async def air_manage(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    c = _air_get(cb, cb.data.split(":")[1], creator=True)
    if c is None:
        return await _air_missing(cb)
    await _done(cb)
    await _air_show_manage(cb, c)


@router.callback_query(F.data.regexp(r"^aishare:[0-9a-f]{6}$"))
async def air_share(cb: CallbackQuery):
    c = _air_get(cb, cb.data.split(":")[1], creator=True)
    if c is None:
        return await _air_missing(cb)
    await _done(cb)
    link = await _air_link(cb.bot, c)
    note = ("\n\nIt is not published yet, so people see a 'coming soon' page. Tip: post this link on X, Instagram or anywhere, "
            "then add the link of your post as a task (X → Like, Repost or Quote a post)." if c.state == "draft" else "")
    await cb.message.answer(f"🔗 Share this link. People who open it land straight on your airdrop:\n\n{html.escape(link)}{note}")


@router.callback_query(F.data.regexp(r"^aipub:[0-9a-f]{6}$"))
async def air_publish(cb: CallbackQuery):
    c = _air_get(cb, cb.data.split(":")[1], creator=True)
    if c is None or c.state != "draft":
        return await _air_missing(cb)
    if not c.tasks:
        return await cb.answer("Add at least one task first.", show_alert=True)
    if c.funding == "funded":
        problem = await _air_funds_error(c)
        if problem:
            await cb.answer()
            return await cb.message.answer(f"❌ Not published: this airdrop is marked <b>Funded</b>, but {html.escape(problem)}\nAdd funds and press Publish again.")
    now = time.time()
    sched = c.schedule(now)
    if sched is None:
        return await cb.answer("The end time you chose has already passed. Tap 🗓 Time frame to change it.", show_alert=True)
    await _done(cb)
    c.state, (c.starts, c.ends) = "open", sched
    _air_save()
    link = await _air_link(cb.bot, c)
    when = (f"is scheduled: it starts {campaigns.format_when(c.starts)} and ends {campaigns.format_when(c.ends)}"
            if c.is_upcoming(now) else f"is live until {campaigns.format_when(c.ends)}")
    await cb.message.answer(f"🚀 <b>{html.escape(c.title)}</b> {when}.\nShare this link:\n{html.escape(link)}")
    await _air_request_broadcast(cb.bot, c)
    await _air_show_manage(cb, c)


@router.callback_query(F.data.regexp(r"^ai(end|can):[0-9a-f]{6}$"))
async def air_end_ask(cb: CallbackQuery):
    kind, cid = cb.data.split(":")
    c = _air_get(cb, cid, creator=True)
    if c is None or c.state not in ("draft", "open"):
        return await _air_missing(cb)
    await _done(cb)
    if kind == "aican" and c.state == "draft":
        text, yes = "Delete this draft?", "🗑 Yes, delete"
    elif kind == "aican":
        text, yes = "Cancel this airdrop? Nobody can join any more and no top prizes are made. Rewards already earned stay payable.", "🚫 Yes, cancel"
    else:
        text, yes = "End this airdrop now? The top prizes are decided from the points so far.", "🏁 Yes, end now"
    await _show(cb, text, InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=yes, callback_data=f"{kind}y:{cid}"), InlineKeyboardButton(text="Back", callback_data=f"aim:{cid}")]]))


@router.callback_query(F.data.regexp(r"^ai(end|can)y:[0-9a-f]{6}$"))
async def air_end_do(cb: CallbackQuery, state: FSMContext):
    kind, cid = cb.data.split(":")
    c = _air_get(cb, cid, creator=True)
    if c is None or c.state not in ("draft", "open"):
        return await _air_missing(cb)
    if kind == "aicany" and c.state == "draft":
        del CAMPAIGNS[c.id]
        _air_save()
        await _done(cb, "Deleted")
        return await air_mine(cb, state)
    await _done(cb)
    if kind == "aicany":
        c.state, c.top_settled = "cancelled", True
        _air_save()
        await _air_show_manage(cb, c)
        return
    c.state = "ended"
    _air_save()
    await _air_finish(cb.bot, c)
    await _air_show_manage(cb, c)


@router.callback_query(F.data.regexp(r"^airm:[0-9a-f]{6}$"))
async def air_remove_pick(cb: CallbackQuery):
    c = _air_get(cb, cb.data.split(":")[1], creator=True)
    if c is None:
        return await _air_missing(cb)
    await _done(cb)
    rows = [[InlineKeyboardButton(text=f"🗑 {t.title[:40]}", callback_data=f"aidel:{c.id}:{t.id}")] for t in c.tasks]
    await _show(cb, "Which task do you want to remove? Points already earned for it are removed too.",
                InlineKeyboardMarkup(inline_keyboard=rows + [[InlineKeyboardButton(text="⬅️ Back", callback_data=f"aim:{c.id}")]]))


@router.callback_query(F.data.regexp(r"^aidel:[0-9a-f]{6}:\d+$"))
async def air_remove_do(cb: CallbackQuery):
    _tag, cid, tid = cb.data.split(":")
    c = _air_get(cb, cid, creator=True)
    if c is None:
        return await _air_missing(cb)
    c.remove_task(int(tid))
    _air_save()
    await _done(cb, "Removed")
    await _air_show_manage(cb, c)


# ---- the creator: add a task
@router.callback_query(F.data.regexp(r"^aiadd:[0-9a-f]{6}$"))
async def air_add(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    c = _air_get(cb, cb.data.split(":")[1], creator=True)
    if c is None or c.state not in ("draft", "open"):
        return await _air_missing(cb)
    if len(c.tasks) >= campaigns.MAX_TASKS:
        return await cb.answer(f"At most {campaigns.MAX_TASKS} tasks.", show_alert=True)
    await _done(cb)
    items = [(plat, icon, name) for plat, (icon, name, _acts) in campaigns.PLATFORMS.items()]
    rows = [[InlineKeyboardButton(text=f"{icon} {name}", callback_data=f"aip:{c.id}:{plat}") for plat, icon, name in items[i:i + 2]]
            for i in range(0, len(items), 2)]
    await _show(cb, "➕ <b>Add a task</b>\nPick the platform.", InlineKeyboardMarkup(inline_keyboard=rows + [[InlineKeyboardButton(text="⬅️ Back", callback_data=f"aim:{c.id}")]]))


@router.callback_query(F.data.regexp(r"^aip:[0-9a-f]{6}:[a-z]+$"))
async def air_add_platform(cb: CallbackQuery, state: FSMContext):
    _tag, cid, plat = cb.data.split(":")
    c = _air_get(cb, cid, creator=True)
    if c is None or plat not in campaigns.PLATFORMS:
        return await _air_missing(cb)
    icon, name, acts = campaigns.PLATFORMS[plat]
    if len(acts) == 1:
        return await _air_kind_prompt(cb, state, c, plat, acts[0][0])
    await _done(cb)
    rows = [[InlineKeyboardButton(text=label, callback_data=f"aik:{cid}:{plat}:{act}")] for act, label in acts]
    await _show(cb, f"{icon} <b>{html.escape(name)}</b>\nWhat should people do?", InlineKeyboardMarkup(inline_keyboard=rows + [[InlineKeyboardButton(text="⬅️ Back", callback_data=f"aiadd:{cid}")]]))


@router.callback_query(F.data.regexp(r"^aik:[0-9a-f]{6}:[a-z]+:[a-z]+$"))
async def air_add_kind(cb: CallbackQuery, state: FSMContext):
    _tag, cid, plat, act = cb.data.split(":")
    c = _air_get(cb, cid, creator=True)
    if c is None or f"{plat}:{act}" not in campaigns.KINDS:
        return await _air_missing(cb)
    await _air_kind_prompt(cb, state, c, plat, act)


async def _air_kind_prompt(cb: CallbackQuery, state: FSMContext, c: campaigns.Campaign, plat: str, act: str) -> None:
    kind, cid = f"{plat}:{act}", c.id
    await _done(cb)
    await state.clear()
    await state.set_state(AirFlow.task_target)
    await state.update_data(cid=cid, kind=kind)
    pname = campaigns.PLATFORMS[plat][1]
    if kind == "tg:join":
        ask = ("Send the channel or group as <code>@username</code> (or its t.me link). For a private one, forward me any message from it.\n\n"
               "⚠️ <b>Add me as an admin there first</b>, so I can check who really joined.")
    elif kind == "qz:answer":
        ask = "Type the <b>question</b> people must answer. For example: <i>What is the name of our token?</i>"
    elif kind == "ref:invite":
        ask = "How many friends must each person invite? Send a number from 1 to 50."
    elif kind == "rc:daily":
        ask = "On how many different days must people check in? Send a number from 2 to 30."
    elif kind == "bal:min":
        session, user = await _load(cb)
        await session.close()
        return await _tp_networks(cb, user, f"bal:{cid}")
    elif plat == "web":
        ask = "Send the link people should visit (starting with https://)."
    elif act in campaigns.POST_ACTIONS:
        ask = (f"Send the link of the {html.escape(pname)} post or video.\n\n💡 To make people engage with <b>your own post</b>: "
               "tap 🔗 Share link on this airdrop, post that link on " + html.escape(pname) + ", copy the link of your post, and paste it here.")
    elif plat in ("x", "ig", "tt", "gh"):
        ask = f"Send the {html.escape(pname)} account (<code>@name</code> or its link)."
    else:
        ask = f"Send the {html.escape(pname)} link (starting with https://)."
    await _show(cb, f"{ask}\n/cancel stops this.", InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Back", callback_data=f"aim:{cid}")]]))


async def _air_target_ready(message: Message, state: FSMContext, title: str, url: str, chat: str = "") -> None:
    data = await state.get_data()
    await state.update_data(title=title, url=url, chat=chat)
    if data["kind"] in ("tg:join", "ref:invite", "rc:daily", "bal:min"):
        await state.update_data(verify="auto")
        return await _air_after_verify(message, state)
    note = "their username" if campaigns.KINDS[data["kind"]][2] else "a short note from them"
    await message.answer(
        "How should I check this task?\n\n"
        f"⚡ <b>Accept at once</b>: {note} is accepted straight away (fast, but I can't check it's true).\n"
        "✋ <b>I approve each person</b>: you see who sent what, and approve or reject.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⚡ Accept at once", callback_data="aivf:instant")],
            [InlineKeyboardButton(text="✋ I approve each person", callback_data="aivf:approve")],
        ]))


async def _air_add_now(event, state: FSMContext, points: int) -> None:
    data = await state.get_data()
    c = CAMPAIGNS.get(data.get("cid", ""))
    if c is None or "verify" not in data:
        return await _show(event, "Start again from Add a task.", None)
    try:
        c.add_task(data["kind"], data["title"], data["url"], data["verify"], data.get("chat", ""), points, data.get("answer", ""), data.get("goal", 0), data.get("token_id", 0))
    except campaigns.CampaignError as exc:
        return await _show(event, html.escape(str(exc)), None)
    _air_save()
    await state.clear()
    if isinstance(event, CallbackQuery):
        await event.answer("Task added ✅")
    await _air_show_manage(event, c)


async def _air_after_verify(event, state: FSMContext) -> None:
    """Points are only asked for when the airdrop has a leaderboard; otherwise the task is added now."""
    data = await state.get_data()
    c = CAMPAIGNS.get(data.get("cid", ""))
    if c is not None and c.uses_points():
        rows = [[InlineKeyboardButton(text=f"{n} points", callback_data=f"aipts:{n}") for n in (5, 10, 20, 50)]]
        return await _show(event, "How many points is this task worth? The top scorers win the leaderboard prizes.", InlineKeyboardMarkup(inline_keyboard=rows))
    await _air_add_now(event, state, 0)


@router.message(AirFlow.task_target, F.forward_origin)
async def air_target_forward(message: Message, state: FSMContext):
    data = await state.get_data()
    if data.get("kind") != "tg:join":
        return await message.answer("Please send the link or text, not a forwarded message.")
    origin = message.forward_origin
    chat = getattr(origin, "chat", None) or getattr(origin, "sender_chat", None)
    if chat is None:
        return await message.answer("That message doesn't come from a channel or group. Forward one from it, or send its @username.")
    await _air_check_chat(message, state, chat.id)


@router.message(AirFlow.task_target, F.text)
async def air_target_text(message: Message, state: FSMContext):
    data = await state.get_data()
    kind = data.get("kind", "")
    plat, _, act = kind.partition(":")
    try:
        if kind == "tg:join":
            return await _air_check_chat(message, state, campaigns.tg_target(message.text))
        if kind == "ref:invite":
            if not message.text.strip().isdigit() or not 1 <= int(message.text.strip()) <= 50:
                raise campaigns.CampaignError("Send a number from 1 to 50.")
            goal = int(message.text.strip())
            await state.update_data(goal=goal, verify="auto")
            return await _air_target_ready(message, state, f"Invite {goal} friend{'s' if goal != 1 else ''}", "")
        if kind == "rc:daily":
            if not message.text.strip().isdigit() or not 2 <= int(message.text.strip()) <= 30:
                raise campaigns.CampaignError("Send a number from 2 to 30.")
            days = int(message.text.strip())
            await state.update_data(goal=days, verify="auto")
            return await _air_target_ready(message, state, f"Check in on {days} different days", "")
        if kind == "qz:answer":
            await state.update_data(title=campaigns.clean_text(message.text, 120), url="")
            await state.set_state(AirFlow.task_answer)
            return await message.answer("Now type the <b>correct answer</b>. Capitals and extra spaces don't matter.")
        url = campaigns.valid_url(message.text) if plat == "web" else campaigns.platform_url(plat, act, message.text)
        title = campaigns.task_title(plat, act, url)
    except campaigns.CampaignError as exc:
        return await message.answer(f"{html.escape(str(exc))}\nTry again, or /cancel.")
    await _air_target_ready(message, state, title, url)


@router.callback_query(AirFlow.task_target, F.data.regexp(r"^aibt:\d+$"))
async def air_bal_token(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if data.get("kind") != "bal:min":
        return await cb.answer("Start again from Add a task.", show_alert=True)
    async with SESSIONS() as session:
        token = await session.get(Token, int(cb.data.split(":")[1]))
        chain = await session.get(Chain, token.chain_id) if token else None
    if chain is None or chain.slug not in OPS:
        return await cb.answer("That network is not available right now.", show_alert=True)
    await _done(cb)
    await state.update_data(token_id=token.id, symbol=token.symbol, decimals=token.decimals, net=OPS[chain.slug].name)
    await state.set_state(AirFlow.bal_amount)
    await _show(cb, f"How much <b>{html.escape(token.symbol)}</b> must each person hold at least? Send a number, like <code>100</code>.", None)


@router.message(AirFlow.bal_amount, F.text)
async def air_bal_amount(message: Message, state: FSMContext):
    d = await state.get_data()
    try:
        raw = _number_in(message.text, d["decimals"])
    except events.EventError as exc:
        return await message.answer(f"{html.escape(str(exc))}\nTry again, or /cancel.")
    shown = service.format_amount(raw, d["decimals"])
    await state.update_data(goal=raw, verify="auto")
    await state.set_state(AirFlow.task_target)
    await _air_target_ready(message, state, f"Hold at least {shown} {d['symbol']} ({d['net']})", "")


@router.message(AirFlow.task_answer, F.text)
async def air_task_answer(message: Message, state: FSMContext):
    try:
        answer = campaigns.clean_text(message.text, 80)
    except campaigns.CampaignError as exc:
        return await message.answer(f"{html.escape(str(exc))}\nTry again, or /cancel.")
    await state.update_data(answer=answer, verify="auto")
    try:
        await message.delete()  # the answer shouldn't stay visible in the chat
    except Exception:
        pass
    await _air_after_verify(message, state)


async def _air_check_chat(message: Message, state: FSMContext, target) -> None:
    """The bot must be an admin of the channel or group, or it can't tell who joined."""
    try:
        chat = await message.bot.get_chat(target)
        me = await message.bot.me()
        mine = await message.bot.get_chat_member(chat.id, me.id)
    except (TelegramBadRequest, TelegramForbiddenError):
        return await message.answer("I can't find that chat, or I'm not in it. Add me as an admin there, then send it again.")
    if mine.status not in ("administrator", "creator"):
        return await message.answer("I'm in that chat but not an admin. Make me an admin there (so I can see its members), then send it again.")
    url = f"https://t.me/{chat.username}" if chat.username else (getattr(chat, "invite_link", None) or "")
    if not url:
        try:
            url = await message.bot.export_chat_invite_link(chat.id)
        except (TelegramBadRequest, TelegramForbiddenError):
            return await message.answer("It is private and I can't make an invite link. Give me the 'invite users' right, or use a public @username.")
    await _air_target_ready(message, state, f"Join {chat.title or 'the chat'}", url, str(chat.id))


@router.callback_query(F.data.regexp(r"^aivf:(instant|approve)$"))
async def air_verify_mode(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if "kind" not in data or "title" not in data:
        return await cb.answer("Start again from Add a task.", show_alert=True)
    await state.update_data(verify=cb.data.split(":")[1])
    await _done(cb)
    await _air_after_verify(cb, state)


@router.callback_query(F.data.regexp(r"^aipts:\d+$"))
async def air_points(cb: CallbackQuery, state: FSMContext):
    await _air_add_now(cb, state, int(cb.data.split(":")[1]))


# ---- the creator: make a new airdrop
@router.callback_query(F.data == "aicr")
async def air_create(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    if not _private(cb):
        return await _done(cb, PRIVATE_ONLY, alert=True)
    open_now = sum(1 for c in CAMPAIGNS.values() if c.creator_tg_id == cb.from_user.id and c.state in ("draft", "open"))
    if open_now >= campaigns.MAX_OPEN_PER_CREATOR:
        return await cb.answer(f"You can have {campaigns.MAX_OPEN_PER_CREATOR} airdrops at a time. Finish or delete one first.", show_alert=True)
    await _done(cb)
    await state.set_state(AirFlow.title)
    await _show(cb, "➕ <b>New airdrop</b>\n\nWhat is its title? For example <i>NRP launch week</i>.\n/cancel stops this.",
                InlineKeyboardMarkup(inline_keyboard=[AIR_BACK]))


@router.message(AirFlow.title, F.text)
async def air_title(message: Message, state: FSMContext):
    try:
        title = campaigns.clean_text(message.text, campaigns.MAX_TITLE)
    except campaigns.CampaignError as exc:
        return await message.answer(f"{html.escape(str(exc))}\nTry again, or /cancel.")
    await state.update_data(title=title)
    await state.set_state(AirFlow.desc)
    await message.answer("Add a short description (what it is, why join), or skip.",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Skip", callback_data="aiskip")]]))


async def _air_ask_banner(event, state: FSMContext) -> None:
    await state.set_state(AirFlow.banner)
    await _show(event, "🖼 Send a <b>banner picture</b> for your airdrop (optional). It shows at the top of your airdrop page. "
                       "A wide picture looks best.",
                InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Skip", callback_data="aibskip")]]))


@router.message(AirFlow.banner, F.photo)
async def air_banner_photo(message: Message, state: FSMContext):
    await state.update_data(banner=message.photo[-1].file_id)
    await _air_ask_time(message, state)


@router.callback_query(AirFlow.banner, F.data == "aibskip")
async def air_banner_skip(cb: CallbackQuery, state: FSMContext):
    await _done(cb)
    await _air_ask_time(cb, state)


@router.message(AirFlow.banner, F.text)
async def air_banner_text(message: Message, state: FSMContext):
    await message.answer("Please send a picture, or tap Skip.")


@router.callback_query(F.data.regexp(r"^aibn:[0-9a-f]{6}$"))
async def air_banner_edit(cb: CallbackQuery, state: FSMContext):
    c = _air_get(cb, cb.data.split(":")[1], creator=True)
    if c is None or c.state not in ("draft", "open"):
        return await _air_missing(cb)
    await _done(cb)
    await state.clear()
    await state.set_state(AirFlow.banner_edit)
    await state.update_data(cid=c.id)
    rows = [[InlineKeyboardButton(text="🗑 Remove banner", callback_data=f"aibrm:{c.id}")]] if c.banner else []
    await _show(cb, "🖼 Send the new banner picture.", InlineKeyboardMarkup(inline_keyboard=rows + [[InlineKeyboardButton(text="⬅️ Back", callback_data=f"aim:{c.id}")]]))


@router.message(AirFlow.banner_edit, F.photo)
async def air_banner_set(message: Message, state: FSMContext):
    data = await state.get_data()
    c = CAMPAIGNS.get(data.get("cid", ""))
    if c is None or c.creator_tg_id != message.from_user.id:
        await state.clear()
        return await message.answer("This airdrop is not available.")
    c.banner = message.photo[-1].file_id
    _air_save()
    await state.clear()
    await message.answer("✅ Banner updated.")
    await _air_show_manage(message, c)


@router.message(AirFlow.banner_edit, F.text)
async def air_banner_edit_text(message: Message):
    await message.answer("Please send a picture, or /cancel.")


@router.callback_query(F.data.regexp(r"^ai(et|ed):[0-9a-f]{6}$"))
async def air_edit_text_ask(cb: CallbackQuery, state: FSMContext):
    kind, cid = cb.data.split(":")
    c = _air_get(cb, cid, creator=True)
    if c is None or c.state not in ("draft", "open"):
        return await _air_missing(cb)
    await _done(cb)
    await state.clear()
    await state.set_state(AirFlow.edit_title if kind == "aiet" else AirFlow.edit_desc)
    await state.update_data(cid=cid)
    await _show(cb, "✏️ Send the new title." if kind == "aiet" else "📝 Send the new description.",
                InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Back", callback_data=f"aim:{cid}")]]))


async def _air_edit_text_save(message: Message, state: FSMContext, field: str, limit: int) -> None:
    d = await state.get_data()
    c = CAMPAIGNS.get(d.get("cid", ""))
    if c is None or c.creator_tg_id != message.from_user.id or c.state not in ("draft", "open"):
        await state.clear()
        return await message.answer("This airdrop is not available.")
    try:
        value = campaigns.clean_text(message.text, limit)
    except campaigns.CampaignError as exc:
        return await message.answer(f"{html.escape(str(exc))}\nTry again, or /cancel.")
    setattr(c, field, value)
    _air_save()
    await state.clear()
    await message.answer("✅ Saved.")
    await _air_show_manage(message, c)


@router.message(AirFlow.edit_title, F.text)
async def air_edit_title(message: Message, state: FSMContext):
    await _air_edit_text_save(message, state, "title", campaigns.MAX_TITLE)


@router.message(AirFlow.edit_desc, F.text)
async def air_edit_desc(message: Message, state: FSMContext):
    await _air_edit_text_save(message, state, "description", campaigns.MAX_TEXT)


@router.callback_query(F.data.regexp(r"^aibrm:[0-9a-f]{6}$"))
async def air_banner_remove(cb: CallbackQuery, state: FSMContext):
    c = _air_get(cb, cb.data.split(":")[1], creator=True)
    if c is None:
        return await _air_missing(cb)
    c.banner = ""
    _air_save()
    await state.clear()
    await _done(cb, "Banner removed")
    await _air_show_manage(cb, c)


async def _air_ask_time(event, state: FSMContext) -> None:
    await state.set_state(AirFlow.duration)
    rows = [[InlineKeyboardButton(text=label, callback_data=f"aid:{secs}") for label, secs in row]
            for row in (
                [("10 min", 600), ("30 min", 1800), ("1 hour", 3600)],
                [("6 hours", 21600), ("1 day", 86400), ("3 days", 259200)],
                [("7 days", 604800), ("14 days", 1209600), ("30 days", 2592000)],
            )]
    rows.append([InlineKeyboardButton(text="📅 Pick exact start & end time", callback_data="aisc")])
    await _show(event, "⏱ <b>Time frame</b>\n\nHow long should it run? Tap one (it starts when you publish), or <b>type your own length</b> "
                       "with a unit: <code>15m</code>, <code>1h</code>, <code>1h30m</code>, <code>2d</code> (1 minute to 90 days).\n\n"
                       "Or tap <b>Pick exact start & end time</b> to post it later, from one date and time to another.",
                InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.regexp(r"^aisch:[0-9a-f]{6}$"))
async def air_reschedule(cb: CallbackQuery, state: FSMContext):
    c = _air_get(cb, cb.data.split(":")[1], creator=True)
    if c is None or c.state != "draft":
        return await _air_missing(cb)
    await _done(cb)
    await state.clear()
    await state.update_data(edit_cid=c.id)
    await _air_ask_time(cb, state)


TZ_BUTTONS = [("UTC", 0), ("+1", 60), ("+2", 120), ("+3", 180), ("+5:30", 330), ("+6", 360), ("+7", 420), ("+8", 480), ("-5", -300), ("-8", -480)]


@router.callback_query(F.data == "aisc")
async def air_custom_time(cb: CallbackQuery, state: FSMContext):
    await _done(cb)
    await state.set_state(AirFlow.tz)
    rows = [[InlineKeyboardButton(text=label, callback_data=f"aitz:{mins}") for label, mins in TZ_BUTTONS[i:i + 5]] for i in range(0, len(TZ_BUTTONS), 5)]
    await _show(cb, "🌍 Which <b>time zone</b> will you type the times in? Tap one, or type it like <code>UTC+6</code> or <code>+5:30</code>.",
                InlineKeyboardMarkup(inline_keyboard=rows))


async def _air_ask_start(event, state: FSMContext, tz_min: int) -> None:
    await state.update_data(tz_min=tz_min)
    await state.set_state(AirFlow.start_in)
    now = time.time()
    await _show(event, f"🟢 <b>When should it start?</b> (time zone {campaigns.format_when(now, tz_min).split(' ', 4)[-1]})\n\n"
                       "Type a date and time, like <code>2026-10-25 14:30</code>, <code>25 Oct 18:00</code> or <code>tomorrow 9:00</code>.\n"
                       f"Now it is <b>{campaigns.format_when(now, tz_min)}</b>.",
                InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="▶️ As soon as I publish", callback_data="aist:now")]]))


@router.callback_query(AirFlow.tz, F.data.regexp(r"^aitz:-?\d+$"))
async def air_tz_button(cb: CallbackQuery, state: FSMContext):
    await _done(cb)
    await _air_ask_start(cb, state, int(cb.data.split(":")[1]))


@router.message(AirFlow.tz, F.text)
async def air_tz_text(message: Message, state: FSMContext):
    try:
        mins = campaigns.parse_tz(message.text)
    except campaigns.CampaignError as exc:
        return await message.answer(f"{html.escape(str(exc))}\nTry again, or /cancel.")
    await _air_ask_start(message, state, mins)


async def _air_ask_end(event, state: FSMContext, start_at: float) -> None:
    d = await state.get_data()
    await state.update_data(start_at=start_at)
    await state.set_state(AirFlow.end_in)
    await _show(event, "🔴 <b>When should it end?</b>\n\nType a date and time, like <code>2026-10-30 18:00</code>, "
                       "or a length counted from the start, like <code>3d</code> or <code>12h</code>.", None)


@router.callback_query(AirFlow.start_in, F.data == "aist:now")
async def air_start_now(cb: CallbackQuery, state: FSMContext):
    await _done(cb)
    await _air_ask_end(cb, state, 0.0)


@router.message(AirFlow.start_in, F.text)
async def air_start_text(message: Message, state: FSMContext):
    d = await state.get_data()
    now = time.time()
    try:
        ts = campaigns.parse_when(message.text, d.get("tz_min", 0), now)
    except campaigns.CampaignError as exc:
        return await message.answer(f"{html.escape(str(exc))}\nTry again, or /cancel.")
    if ts < now - 60:
        return await message.answer("That time has already passed. Send a time in the future.")
    if ts > now + 365 * 86400:
        return await message.answer("That is more than a year away. Send an earlier time.")
    await _air_ask_end(message, state, 0.0 if ts <= now + 60 else ts)


@router.message(AirFlow.end_in, F.text)
async def air_end_text(message: Message, state: FSMContext):
    d = await state.get_data()
    now = time.time()
    start = d.get("start_at", 0.0)  # 0 = it starts the moment it is published
    base = start or now
    was_length = True
    try:
        try:
            end = base + campaigns.parse_duration(message.text)
        except campaigns.CampaignError:
            was_length = False
            end = campaigns.parse_when(message.text, d.get("tz_min", 0), now)
    except campaigns.CampaignError as exc:
        return await message.answer(f"{html.escape(str(exc))}\nSend a date and time, or a length like 3d.")
    length = int(end - base)
    if length < campaigns.MIN_SECONDS:
        return await message.answer("The end must be at least a minute after the start.")
    if length > campaigns.MAX_SECONDS:
        return await message.answer("An airdrop can run for 90 days at most. Send an earlier end.")
    # a length counted from "when I publish" is kept as a length; everything else is an exact end time
    exact_end = 0.0 if (was_length and not start) else end
    await _air_time_done(message, state, length, start, exact_end)


@router.message(AirFlow.desc, F.text)
async def air_desc(message: Message, state: FSMContext):
    try:
        desc = campaigns.clean_text(message.text, campaigns.MAX_TEXT)
    except campaigns.CampaignError as exc:
        return await message.answer(f"{html.escape(str(exc))}\nTry again, or press Skip.")
    await state.update_data(desc=desc)
    await _air_ask_banner(message, state)


@router.callback_query(AirFlow.desc, F.data == "aiskip")
async def air_desc_skip(cb: CallbackQuery, state: FSMContext):
    await _done(cb)
    await state.update_data(desc="")
    await _air_ask_banner(cb, state)


async def _air_ask_token(event, state: FSMContext, user) -> None:
    await _tp_networks(event, user, "air")


async def _air_time_done(event, state: FSMContext, seconds: int, start_at: float = 0.0, end_at: float = 0.0) -> None:
    d = await state.get_data()
    cid = d.get("edit_cid")
    if cid:  # changing the time frame of a draft
        c = CAMPAIGNS.get(cid)
        await state.clear()
        if c is not None and c.state == "draft":
            c.seconds, c.start_at, c.end_at = seconds, start_at, end_at
            _air_save()
            return await _air_show_manage(event, c)
        return
    await state.update_data(seconds=seconds, start_at=start_at, end_at=end_at)
    await state.set_state(None)
    session, user = await _load(event)
    await session.close()
    await _air_ask_token(event, state, user)


@router.callback_query(AirFlow.duration, F.data.regexp(r"^aid:\d+$"))
async def air_time_button(cb: CallbackQuery, state: FSMContext):
    seconds = int(cb.data.split(":")[1])
    if not campaigns.MIN_SECONDS <= seconds <= campaigns.MAX_SECONDS:
        return await cb.answer("Pick a time from the list.", show_alert=True)
    await _done(cb)
    await _air_time_done(cb, state, seconds)


@router.message(AirFlow.duration, F.text)
async def air_time_text(message: Message, state: FSMContext):
    try:
        seconds = campaigns.parse_duration(message.text)
    except campaigns.CampaignError as exc:
        return await message.answer(f"{html.escape(str(exc))}\nTry again, or /cancel.")
    await _air_time_done(message, state, seconds)


@router.callback_query(F.data.regexp(r"^aitok:\d+$"))
async def air_token(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if "seconds" not in data:
        return await cb.answer("Start again from Create an airdrop.", show_alert=True)
    session, user = await _load(cb)
    async with session:
        token = await session.get(Token, int(cb.data.split(":")[1]))
        chain = await session.get(Chain, token.chain_id) if token else None
        if chain is None or chain.slug not in OPS:
            return await cb.answer("That network is not available right now.", show_alert=True)
        info = dict(token_id=token.id, slug=chain.slug, symbol=token.symbol, decimals=token.decimals,
                    net_name=OPS[chain.slug].name, user_id=user.id, rw=[])
    await _done(cb)
    await state.update_data(**info)
    await _air_reward_screen(cb, state)


REWARD_KINDS = [
    ("fcfs", "⚡ First come, first served"),
    ("raffle", "🎲 Lucky draw"),
    ("top", "🏆 Leaderboard (points)"),
]


async def _air_reward_screen(event, state: FSMContext) -> None:
    d = await state.get_data()
    chosen = set(d.get("rw", []))
    rows = [[InlineKeyboardButton(text=("☑️ " if key in chosen else "⬜ ") + label, callback_data=f"airt:{key}")] for key, label in REWARD_KINDS]
    rows.append([InlineKeyboardButton(text="➡️ Continue", callback_data="airt:go")])
    await _show(event, f"How is the reward in <b>{html.escape(d['symbol'])}</b> given out? Tap to choose one or more, then Continue.\n\n"
                       "⚡ <b>First come, first served</b>: a fixed amount for each of the first people who finish every task. No points.\n"
                       "🎲 <b>Lucky draw</b>: when it ends, random winners are picked among everyone who finished every task. No points.\n"
                       "🏆 <b>Leaderboard</b>: tasks give points, and the top scorers win prizes when it ends.",
                InlineKeyboardMarkup(inline_keyboard=rows + [AIR_BACK]))


@router.callback_query(F.data.regexp(r"^airt:(fcfs|raffle|top|go)$"))
async def air_reward_toggle(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    if "token_id" not in d:
        return await cb.answer("Start again from Create an airdrop.", show_alert=True)
    key = cb.data.split(":")[1]
    chosen = list(d.get("rw", []))
    if key == "go":
        if not chosen:
            return await cb.answer("Choose at least one.", show_alert=True)
        await _done(cb)
        await state.update_data(want_comp="fcfs" in chosen, want_raffle="raffle" in chosen, want_top="top" in chosen)
        return await _air_step(cb, state)
    chosen = [k for k in chosen if k != key] if key in chosen else chosen + [key]
    await state.update_data(rw=chosen)
    await _done(cb)
    await _air_reward_screen(cb, state)


def _air_pick_mode(prefix: str, auto_label: str, auto_cb: str, manual_cb: str):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=auto_label, callback_data=auto_cb),
                                                  InlineKeyboardButton(text="✋ Manual", callback_data=manual_cb)]])


async def _air_step(event, state: FSMContext) -> None:
    """Ask the next missing question of the reward set-up, or make the airdrop when everything is known."""
    d = await state.get_data()
    sym = html.escape(d["symbol"])
    if d.get("want_comp") and "comp_each" not in d:
        await state.set_state(AirFlow.comp_each)
        return await _show(event, f"⚡ How much <b>{sym}</b> does <b>each person</b> get? Send a number, like <code>5</code>.", None)
    if d.get("want_comp") and "comp_max" not in d:
        await state.set_state(AirFlow.comp_max)
        rows = [[InlineKeyboardButton(text=str(n), callback_data=f"aicp:{n}") for n in (10, 25, 50, 100)]]
        return await _show(event, "How many people can get it (the first ones to finish every task)? Tap one or type a number.", InlineKeyboardMarkup(inline_keyboard=rows))
    if d.get("want_comp") and "comp_mode" not in d:
        await state.set_state(None)
        return await _show(event, "How is this reward paid?\n\n"
                                  "⚡ <b>Instant</b>: sent to their wallet the moment they finish.\n"
                                  "✋ <b>Manual</b>: you send it yourself later, from your list of rewards to pay.",
                           _air_pick_mode("c", "⚡ Instant", "aicm:instant", "aicm:manual"))
    if d.get("want_raffle") and "raffle_each" not in d:
        await state.set_state(AirFlow.raffle_each)
        return await _show(event, f"🎲 How much <b>{sym}</b> does <b>each lucky winner</b> get? Send a number.", None)
    if d.get("want_raffle") and "raffle_winners" not in d:
        await state.set_state(AirFlow.raffle_n)
        rows = [[InlineKeyboardButton(text=str(n), callback_data=f"airn:{n}") for n in (1, 3, 5, 10)]]
        return await _show(event, "How many winners should be drawn? Tap one or type a number.", InlineKeyboardMarkup(inline_keyboard=rows))
    if d.get("want_raffle") and "raffle_mode" not in d:
        await state.set_state(None)
        return await _show(event, "How are the lucky-draw prizes paid when it ends?\n\n"
                                  "⚡ <b>Automatic</b>: I send them to the winners.\n"
                                  "✋ <b>Manual</b>: you send them yourself.",
                           _air_pick_mode("r", "⚡ Automatic", "airm2:auto", "airm2:manual"))
    if d.get("want_top") and "top_prizes" not in d:
        await state.set_state(AirFlow.top_amounts)
        return await _show(event, f"🏆 Send the prizes for 1st, 2nd, 3rd… in <b>{sym}</b>, separated by commas. "
                                  f"For example <code>100, 50, 25</code> (up to {campaigns.MAX_TOP} places).", None)
    if d.get("want_top") and "top_mode" not in d:
        await state.set_state(None)
        return await _show(event, "How are the leaderboard prizes paid when it ends?\n\n"
                                  "⚡ <b>Automatic</b>: I send them to the winners.\n"
                                  "✋ <b>Manual</b>: you send them yourself, from your list of rewards to pay.",
                           _air_pick_mode("t", "⚡ Automatic", "aitm:auto", "aitm:manual"))
    if "funding" not in d:
        await state.set_state(None)
        return await _show(event, "Last question: how do people know the reward is real?\n\n"
                                  "✅ <b>Funded</b>: I check now that your wallet holds the full reward, and show everyone a <b>Funded</b> badge.\n"
                                  "⏳ <b>Pay when due</b>: no check. Rewards are paid from your wallet when earned. People see this too.",
                           InlineKeyboardMarkup(inline_keyboard=[
                               [InlineKeyboardButton(text="✅ Funded", callback_data="aifund:funded")],
                               [InlineKeyboardButton(text="⏳ Pay when due", callback_data="aifund:later")]]))
    await _air_make(event, state)


@router.message(AirFlow.comp_each, F.text)
async def air_comp_each(message: Message, state: FSMContext):
    d = await state.get_data()
    try:
        raw = _number_in(message.text, d["decimals"])
    except events.EventError as exc:
        return await message.answer(f"{html.escape(str(exc))}\nTry again, or /cancel.")
    await state.update_data(comp_each=raw)
    await _air_step(message, state)


async def _air_comp_max(event, state: FSMContext, people: int) -> None:
    await state.update_data(comp_max=people)
    await _air_step(event, state)


@router.callback_query(AirFlow.comp_max, F.data.regexp(r"^aicp:\d+$"))
async def air_comp_max_button(cb: CallbackQuery, state: FSMContext):
    await _done(cb)
    await _air_comp_max(cb, state, int(cb.data.split(":")[1]))


@router.message(AirFlow.comp_max, F.text)
async def air_comp_max_text(message: Message, state: FSMContext):
    text = message.text.strip()
    if not text.isdigit() or not 1 <= int(text) <= campaigns.MAX_COMP_WINNERS:
        return await message.answer(f"Send a whole number from 1 to {campaigns.MAX_COMP_WINNERS}.")
    await _air_comp_max(message, state, int(text))


@router.callback_query(F.data.regexp(r"^aicm:(instant|manual)$"))
async def air_comp_mode(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    if "comp_max" not in d:
        return await cb.answer("Start again from Create an airdrop.", show_alert=True)
    await _done(cb)
    await state.update_data(comp_mode=cb.data.split(":")[1])
    await _air_step(cb, state)


@router.message(AirFlow.raffle_each, F.text)
async def air_raffle_each(message: Message, state: FSMContext):
    d = await state.get_data()
    try:
        raw = _number_in(message.text, d["decimals"])
    except events.EventError as exc:
        return await message.answer(f"{html.escape(str(exc))}\nTry again, or /cancel.")
    await state.update_data(raffle_each=raw)
    await _air_step(message, state)


async def _air_raffle_n(event, state: FSMContext, winners: int) -> None:
    await state.update_data(raffle_winners=winners)
    await _air_step(event, state)


@router.callback_query(AirFlow.raffle_n, F.data.regexp(r"^airn:\d+$"))
async def air_raffle_n_button(cb: CallbackQuery, state: FSMContext):
    await _done(cb)
    await _air_raffle_n(cb, state, int(cb.data.split(":")[1]))


@router.message(AirFlow.raffle_n, F.text)
async def air_raffle_n_text(message: Message, state: FSMContext):
    text = message.text.strip()
    if not text.isdigit() or not 1 <= int(text) <= campaigns.MAX_COMP_WINNERS:
        return await message.answer(f"Send a whole number from 1 to {campaigns.MAX_COMP_WINNERS}.")
    await _air_raffle_n(message, state, int(text))


@router.callback_query(F.data.regexp(r"^airm2:(auto|manual)$"))
async def air_raffle_mode(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    if "raffle_winners" not in d:
        return await cb.answer("Start again from Create an airdrop.", show_alert=True)
    await _done(cb)
    await state.update_data(raffle_mode=cb.data.split(":")[1])
    await _air_step(cb, state)


@router.message(AirFlow.top_amounts, F.text)
async def air_top_amounts(message: Message, state: FSMContext):
    d = await state.get_data()
    try:
        prizes = events.parse_rank_amounts(message.text, d["decimals"])
    except events.EventError as exc:
        return await message.answer(f"{html.escape(str(exc))}\nTry again, or /cancel.")
    if len(prizes) > campaigns.MAX_TOP:
        return await message.answer(f"At most {campaigns.MAX_TOP} places. Send fewer amounts.")
    await state.update_data(top_prizes=prizes)
    await _air_step(message, state)


@router.callback_query(F.data.regexp(r"^aitm:(auto|manual)$"))
async def air_top_mode(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    if "top_prizes" not in d:
        return await cb.answer("Start again from Create an airdrop.", show_alert=True)
    await _done(cb)
    await state.update_data(top_mode=cb.data.split(":")[1])
    await _air_step(cb, state)


@router.callback_query(F.data.regexp(r"^aifund:(funded|later)$"))
async def air_funding(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    if "token_id" not in d:
        return await cb.answer("Start again from Create an airdrop.", show_alert=True)
    await _done(cb)
    await state.update_data(funding=cb.data.split(":")[1])
    await _air_step(cb, state)


async def _air_make(event, state: FSMContext) -> None:
    d = await state.get_data()
    tg = event.from_user
    comp, raffle, top = d.get("want_comp"), d.get("want_raffle"), d.get("want_top")
    c = campaigns.Campaign(
        id=campaigns.new_id(CAMPAIGNS), creator_tg_id=tg.id, creator_user_id=d["user_id"], creator_label=_air_label(tg),
        title=d["title"], description=d.get("desc", ""), slug=d["slug"], network_name=d["net_name"], token_id=d["token_id"],
        symbol=d["symbol"], decimals=d["decimals"], seconds=d["seconds"], funding=d["funding"],
        comp_mode=d.get("comp_mode", "none") if comp else "none",
        comp_each=d.get("comp_each", 0) if comp else 0,
        comp_max=d.get("comp_max", 0) if comp else 0,
        raffle_mode=d.get("raffle_mode", "none") if raffle else "none",
        raffle_each=d.get("raffle_each", 0) if raffle else 0,
        raffle_winners=d.get("raffle_winners", 0) if raffle else 0,
        top_mode=d.get("top_mode", "none") if top else "none",
        top_prizes=list(d.get("top_prizes", [])) if top else [],
        created=time.time(), banner=d.get("banner", ""), start_at=d.get("start_at", 0.0), end_at=d.get("end_at", 0.0),
    )
    if c.funding == "funded":
        problem = await _air_funds_error(c)
        if problem:
            d.pop("funding", None)
            await state.set_data(d)
            return await _show(event, f"❌ I can't mark this <b>Funded</b>: {html.escape(problem)}\n\nAdd funds and try again, or choose Pay when due.",
                               InlineKeyboardMarkup(inline_keyboard=[
                                   [InlineKeyboardButton(text="✅ Try Funded again", callback_data="aifund:funded")],
                                   [InlineKeyboardButton(text="⏳ Pay when due", callback_data="aifund:later")]]))
    CAMPAIGNS[c.id] = c
    _air_save()
    await state.clear()
    await _show(event, "✅ <b>Airdrop created</b> as a draft. Now add its tasks, then press Publish.", None)
    await _air_show_manage(event, c)


async def _owner_ids() -> list:
    """Telegram ids of the owners (the same people who receive the backups)."""
    ids = set(SETTINGS.backup_ids)
    async with SESSIONS() as session:
        for name in SETTINGS.backup_recipients:
            person = await service.find_user_by_username(session, name)
            if person is not None:
                ids.add(person.telegram_id)
    return sorted(ids)


@router.message(Command("stats"))
async def stats_command(message: Message):
    if not _private(message) or message.from_user.id not in await _owner_ids():
        return
    await message.answer(await dailystats.text(SESSIONS, PRICES))


@router.message(Command("aitest"))
async def ai_test_command(message: Message):
    """Owners only: shows how the AI is set up and whether each configured model really answers (no keys are shown)."""
    if not _private(message) or message.from_user.id not in await _owner_ids():
        return
    key, free_key = SETTINGS.anthropic_api_key, SETTINGS.ai_api_key
    if not (key or free_key):
        return await message.answer("🤖 No AI key is set. Add AI_PROVIDER, AI_API_KEY and AI_MODEL to .env and restart the bot.")
    if not free_key:
        return await message.answer(f"🤖 AI: Claude, model {html.escape(SETTINGS.assistant_model)}. Ask it something in the AI Assistant to test.")
    url, model = assistant.provider_settings(SETTINGS.ai_provider, SETTINGS.ai_model, SETTINGS.ai_base_url)
    await message.answer(f"🤖 Testing the AI: provider <b>{html.escape(SETTINGS.ai_provider)}</b>, key is set ✅\nModels tried in this order:")
    lines = await assistant.probe(await _ai_http(), url, free_key, model)
    web = SETTINGS.ai_provider == "gemini" or "compound" in model
    await message.answer("\n".join(html.escape(l) for l in lines) + "\n\n"
                         + ("🌐 Web search: available." if web else "🌐 Web search: only with Gemini or a Groq compound model (groq/compound)."))


@router.message(Command("candidates"))
async def candidates_file(message: Message):
    """Team only: sends the live list of Neo candidates (rank, votes, public key) as a text file, used to fill in their names."""
    if not _private(message):
        return
    uname = (message.from_user.username or "").lower()
    if uname not in {n.lower().lstrip("@") for n in SETTINGS.backup_recipients} and message.from_user.id not in set(SETTINGS.backup_ids or []):
        return
    try:
        candidates = await neovote.get_candidates(NEO_RPC, fresh=True)
    except Exception:
        return await message.answer("❌ I couldn't reach the Neo network. Try again in a moment.")
    text = "rank | votes | public key\n" + "\n".join(f"{c.rank} | {c.votes} | {c.pubkey}" for c in candidates) + "\n"
    await message.answer_document(BufferedInputFile(text.encode(), filename="neo-candidates.txt"),
                                  caption=f"Neo candidates ({len(candidates)}). Send this file, with a screenshot of the names from the governance website, to Claude.")


@router.message(Command("airdrop"))
async def airdrop_command(message: Message, state: FSMContext):
    await air_menu(message, state)


# ------------------------------------------------------------------ AI Assistant
LIMITER = assistant.Limiter(gap=6.0, daily=SETTINGS.assistant_daily_limit)
_BOT_ME: dict = {}
_AI_HTTP: dict = {}


def _ai_topic_label(topic: str) -> str:
    return dict(helptext.topic_keys(_any_bridge())).get(topic, "")


def _ai_support_text() -> str:
    return ", ".join(f"@{n}" for n in support_names()) or "the bot's team"


async def _ai_http():
    """One shared HTTP session for Claude (created on first use)."""
    import aiohttp

    sess = _AI_HTTP.get("s")
    if sess is None or sess.closed:
        sess = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=25), connector=aiohttp.TCPConnector(ssl=tls.context())
        )
        _AI_HTTP["s"] = sess
    return sess


MEMORY = assistant.Memory()
AUTO = assistant.AutoLimiter()
AI_OFF: set[int] = aistore.load_off()  # groups where an admin turned the automatic answers off
AI_MSGS: dict[tuple[int, int], None] = {}  # (chat, message) of my answers, so a reply to one continues the conversation


def _remember_ai_message(chat_id: int, message_id: int) -> None:
    AI_MSGS[(chat_id, message_id)] = None
    while len(AI_MSGS) > 3000:
        AI_MSGS.pop(next(iter(AI_MSGS)))


_AI_CHART: dict = {}  # conversation -> coin symbol: the last answer was a price, so a chart goes with it


async def _ai_chart(symbol: str) -> tuple[bytes | None, str]:
    """(PNG bytes or None, caption line). The chart is drawn off the event loop."""
    try:
        points, source = await asyncio.wait_for(pricechart.get_series(await _ai_http(), symbol), 12)
        png = await asyncio.to_thread(pricechart.render_chart, symbol, points, source)
    except Exception:
        log.exception("Price chart failed for %s", symbol)
        return None, ""
    if png:
        return png, pricechart.caption_note(symbol, points, source)
    return None, f"📈 I only just started recording {symbol}'s price, so there is no chart yet. Ask again in a few hours."


async def _ai_send(message: Message, answer: str, symbol: str | None, *, reply: bool, reply_markup=None):
    """Sends the answer; a price answer goes out as a chart picture with the answer as its caption."""
    send = message.reply if reply else message.answer
    if symbol:
        png, note = await _ai_chart(symbol)
        if png:
            body = assistant.pretty(answer) + ("\n\n" + html.escape(note) if note else "")
            if len(body) <= 1000:
                send_photo = message.reply_photo if reply else message.answer_photo
                return await send_photo(BufferedInputFile(png, filename=f"{symbol}.png"), caption=body, reply_markup=reply_markup)
        elif note:
            answer = answer + "\n\n" + note
    pieces = assistant.split_text(answer, 3300) or [answer]
    sent = None
    for i, piece in enumerate(pieces):
        last = i == len(pieces) - 1
        text = assistant.pretty(piece)
        try:
            sent = await (send if i == 0 else message.answer)(text, reply_markup=reply_markup if last else None)
        except TelegramBadRequest:  # a cut piece with broken markup: send it as plain text
            sent = await (send if i == 0 else message.answer)(html.escape(piece), reply_markup=reply_markup if last else None, parse_mode=None)
    return sent


_IMG_HTTP: dict = {}
MEME_LIMITER = assistant.Limiter(gap=15.0, daily=15)


async def _img_http():
    """HTTP session for the picture service, which can take up to a minute to draw."""
    import aiohttp

    sess = _IMG_HTTP.get("s")
    if sess is None or sess.closed:
        sess = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=70), connector=aiohttp.TCPConnector(ssl=tls.context())
        )
        _IMG_HTTP["s"] = sess
    return sess


async def _ai_complete(system: str, user: str) -> str | None:
    """One plain answer from whichever AI is configured (used to write memes)."""
    url, model = assistant.provider_settings(SETTINGS.ai_provider, SETTINGS.ai_model, SETTINGS.ai_base_url)
    return await assistant.complete(await _ai_http(), system=system, user=user, claude_key=SETTINGS.anthropic_api_key,
                                    claude_model=SETTINGS.assistant_model, free_key=SETTINGS.ai_api_key, url=url,
                                    model=model, max_tokens=400)


async def _send_meme(message: Message, request: str, context: str = "", *, reply: bool, markup=None) -> None:
    """Makes a meme or picture for the request and sends it."""
    send = message.reply if reply else message.answer
    if not (SETTINGS.anthropic_api_key or SETTINGS.ai_api_key):
        return await send("🎭 Memes need the AI Assistant switched on. Ask the bot's owners.")
    uid = message.from_user.id
    if MEME_LIMITER.too_fast(uid):
        return await send("One moment 🙂 Ask for the next meme in a few seconds.", reply_markup=markup)
    if not MEME_LIMITER.claude_ok(uid, int(time.time() // 86400)):
        return await send("🎭 That's enough memes for today. Come back tomorrow!", reply_markup=markup)
    try:
        await message.bot.send_chat_action(message.chat.id, "upload_photo")
    except Exception:
        pass
    try:
        png, caption = await memes.make_meme(await _img_http(), _ai_complete, request, context)
    except Exception:
        log.exception("Meme failed")
        png, caption = None, "I couldn't make that one just now 😅 Please try again in a moment."
    if png is None:
        return await send(html.escape(caption), reply_markup=markup)
    send_photo = message.reply_photo if reply else message.answer_photo
    sent = await send_photo(BufferedInputFile(png, filename="meme.png"), caption=html.escape(caption)[:1000], reply_markup=markup)
    if reply:
        _remember_ai_message(message.chat.id, sent.message_id)


async def _own_balances(user_id: int) -> str | None:
    """The asker's OWN balances as friendly text (None if they have no wallet yet). Read-only."""
    async with SESSIONS() as session:
        user = await service.find_user_by_telegram_id(session, user_id)
    if user is None:
        return None
    lines = ["👛 <b>Your balances</b> (live)\n"]
    total, any_value = Decimal(0), False
    for sec in await asyncio.wait_for(_home_sections(user), 25):
        if sec.rows is None:
            lines.append(f"<b>{html.escape(sec.name)}</b>: couldn't reach the network just now")
            continue
        held = [r for r in sec.rows if not r.zero]
        if not held:
            lines.append(f"<b>{html.escape(sec.name)}</b>: nothing yet")
            continue
        parts = []
        for r in held:
            part = f"{html.escape(str(r.qty))} {html.escape(r.symbol)}"
            if r.value is not None:
                part += f" (≈ ${r.value:,.2f})"
                total += Decimal(str(r.value))
                any_value = True
            parts.append(part)
        lines.append(f"<b>{html.escape(sec.name)}</b>: " + ", ".join(parts))
    if any_value:
        lines.append(f"\n💰 Total ≈ <b>${total:,.2f}</b>")
    lines.append("\nOpen 🏠 Home any time for the full view.")
    return "\n".join(lines)


async def _ai_facts(question: str, user_id: int, private: bool) -> str:
    """What the bot reads for the AI so it can answer exactly: current events for everyone; in a private chat also
    this person's OWN balances and votes. Never anything about anybody else. Read-only: it can't move funds."""
    parts: list[str] = []
    now = time.time()
    try:
        if assistant.ASKS_EVENT.search(question) or not (assistant.ASKS_BALANCE.search(question) or assistant.ASKS_VOTE.search(question)):
            live = [c for c in CAMPAIGNS.values() if c.is_active(now)]
            soon = [c for c in CAMPAIGNS.values() if c.is_upcoming(now)]
            lines = [f"AIRDROP EVENTS: {len(live)} active, {len(soon)} upcoming. Open them with 💎 Earn → 🪂 Airdrops."]
            for c in sorted(live, key=lambda c: c.ends)[:6] + sorted(soon, key=lambda c: c.starts)[:3]:
                person = c.participants.get(user_id) if private else None
                lines.append(assistant.plain(campaigns.event_text(c, person, _air_fmt(c), now))[:700])
            if not live and not soon:
                lines.append("There is no active or upcoming airdrop right now.")
            parts.append("\n\n".join(lines))
            rains = [r for r in RAINS.values() if r.state == "open"]
            parts.append(f"RAINS open now: {len(rains)}. QUIZZES running now: {len([q for q in QUIZZES.values() if not q.cancelled])}.")
    except Exception:
        log.exception("AI facts: events")
    if private and assistant.ASKS_BALANCE.search(question):
        try:
            async with SESSIONS() as session:
                user = await service.find_user_by_telegram_id(session, user_id)
            if user is not None:
                lines = ["THIS PERSON'S OWN BALANCES (read just now):"]
                for sec in await asyncio.wait_for(_home_sections(user), 25):
                    if sec.rows is None:
                        lines.append(f"{sec.name}: the network could not be reached just now")
                        continue
                    held = [r for r in sec.rows if not r.zero]
                    if held:
                        lines.append(f"{sec.name}: " + ", ".join(
                            f"{r.qty} {r.symbol}" + (f" (≈ ${r.value:.2f})" if r.value is not None else "") for r in held))
                    else:
                        lines.append(f"{sec.name}: nothing")
                parts.append("\n".join(lines))
        except Exception:
            log.exception("AI facts: balances")
            parts.append("The balances could not be read just now.")
    if assistant.ASKS_VOTE.search(question) and "neo_n3" in OPS:
        try:
            candidates = await asyncio.wait_for(neovote.get_candidates(NEO_RPC), 15)
            lines = ["NEO GOVERNANCE (live): top 21 candidates = the committee, top 7 = consensus nodes. Rank. Name. Votes."]
            for c in candidates[:neovote.COMMITTEE_SIZE]:
                lines.append(f"{c.rank}. {neovote.label(c, VOTE_NAMES)} · {neovote.votes_text(c.votes)} votes")
            parts.append("\n".join(lines))
            if private:
                async with SESSIONS() as session:
                    user = await service.find_user_by_telegram_id(session, user_id)
                    wallet = await service.get_wallet(session, user, "neo_n3") if user else None
                    rows = [(r.id, r.address, r.candidate) for r in await service.get_vote_wallets(session, user)] if user else []
                if wallet is not None:
                    main = await asyncio.wait_for(neovote.get_standing(NEO_RPC, wallet.address), 15)
                    slots = list(await asyncio.gather(*(neovote.read_slot(NEO_RPC, i, a, c) for i, a, c in rows)))
                    me = [f"THIS PERSON'S OWN NEO: {main.neo} NEO in the main wallet; GAS from holding NEO waiting: {_gas(main.unclaimed)} GAS."]
                    for sl in slots:
                        me.append(f"Voted {sl.standing.neo} NEO for {_cand_name(sl.candidate, candidates)}; GAS waiting from it: {_gas(sl.standing.unclaimed)}.")
                    if not slots:
                        me.append("They have not voted yet.")
                    parts.append("\n".join(me))
        except Exception:
            log.exception("AI facts: vote")
            parts.append("The Neo governance data could not be read just now.")
    return "\n\n".join(parts)


async def ai_reply(question: str, user_id: int, who, group: bool = False, auto: bool = False, context: str = "") -> str | None:
    """The Assistant's answer as plain text (None if the question isn't about this bot). `who` identifies the
    conversation (private chat, or a person in a group): it keeps the memory of what was said."""
    question = question.strip()[:600]
    if assistant.wants_reset(question):
        MEMORY.clear(who)
        return "Okay, we start fresh. What would you like to know about the bot?"
    chat = assistant.smalltalk(question)
    if chat:
        return chat
    index = assistant.get_index(_help_networks(), _any_bridge())
    history, prev_topic = MEMORY.history(who), MEMORY.topic(who)
    try:  # live prices (USD per coin), so it can price an amount or convert one coin to another
        live = await asyncio.wait_for(PRICES.prices(), 6)
    except Exception:
        live = {}
    price_q = assistant.parse_price_question(question, live.keys()) if live else None
    if live:
        pricechart.HISTORY.record(live)
    _AI_CHART.pop(who, None)
    if price_q is not None:
        answer = assistant.price_answer(price_q, live)
        MEMORY.add(who, question, answer, None)
        if not price_q.quote or price_q.quote == price_q.base:  # a price in dollars gets a chart; a coin-to-coin conversion does not
            _AI_CHART[who] = price_q.base
        return answer
    key = SETTINGS.anthropic_api_key
    free_key = SETTINGS.ai_api_key
    today = int(time.time() // 86400)
    ai_on = bool(key or free_key)
    facts = ""
    if ai_on:
        try:
            facts = await asyncio.wait_for(_ai_facts(question, user_id, isinstance(who, tuple) and who[0] == "p"), 40)
        except Exception:
            log.exception("AI facts failed")
    model_q = question + (f"\n<group_context>\n{context[:6000]}\n</group_context>" if context else "") + (assistant.AUTO_NOTE if auto else assistant.GROUP_NOTE if group else "")
    if ai_on and LIMITER.claude_ok(user_id, today):  # a real conversation: the AI reads the whole guide and the chat so far
        if free_key:  # a free provider (Gemini, Groq, OpenRouter)
            url, model = assistant.provider_settings(SETTINGS.ai_provider, SETTINGS.ai_model, SETTINGS.ai_base_url)
            text = None
            if SETTINGS.ai_provider == "gemini" and "googleapis.com" in url and os.getenv("AI_WEB", "1") != "0":
                text = await assistant.ask_gemini_web(await _ai_http(), free_key, model, model_q,
                                                      assistant.knowledge_text(index), _ai_support_text(),
                                                      assistant.live_prices_block(live), history, facts)
            if not text:
                text = await assistant.ask_free(await _ai_http(), url, free_key, model, model_q,
                                                assistant.knowledge_text(index), _ai_support_text(),
                                                assistant.live_prices_block(live), history, facts)
        else:
            text = await assistant.ask_claude(await _ai_http(), key, SETTINGS.assistant_model, model_q,
                                              assistant.knowledge_text(index), _ai_support_text(),
                                              assistant.live_prices_block(live), history, facts)
        if auto and assistant.is_skip(text):
            return None  # not about this bot: stay quiet
        if text:
            top, _s = index.search(f"{MEMORY.last_question(who)} {question}" if assistant.is_followup(question) else question, k=3)
            MEMORY.add(who, question, text, assistant.topic_of(top) or prev_topic)
            return text
    if auto and ai_on and not assistant.should_auto_answer(question, index):
        return None  # the AI could not decide: only the strict built-in rule may answer on its own
    if ai_on and assistant.wants_overview(question):
        return None  # the AI did not answer: say so instead of dumping the help text at someone who just chatted
    answer, topic = assistant.local_reply(question, index, prev_topic, MEMORY.last_question(who), _ai_topic_label)
    if answer:
        MEMORY.add(who, question, answer, topic)
    return answer


NOT_SURE_GROUP = ("I couldn't answer that just now 🤔 Please ask again in a moment, or ask the group admins or {who}.")
NOT_SURE = ("I couldn't answer that just now, my AI service may be busy. Please ask again in a moment, or tap ❓ Help → Support to reach {who}.")


def _ai_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧹 New chat", callback_data="ai:reset")],
        [InlineKeyboardButton(text="📖 Help menu", callback_data="help"),
         InlineKeyboardButton(text="🏠 Home", callback_data="home")],
    ])


@router.callback_query(F.data == "ai:reset")
async def ai_reset(cb: CallbackQuery, state: FSMContext):
    MEMORY.clear(("p", cb.from_user.id))
    await state.set_state(AIFlow.asking)
    await cb.answer("Okay, I forgot our chat.")
    await cb.message.answer("🧹 Fresh start. What would you like to know about the bot?", reply_markup=_ai_keyboard())


@router.callback_query(F.data == "ai:start")
async def ai_start(cb: CallbackQuery, state: FSMContext):
    if not _private(cb):
        return await cb.answer("Mention me with your question, or reply to one of my messages.", show_alert=True)
    await state.set_state(AIFlow.asking)
    await cb.answer()
    await cb.message.answer(
        "🤖 <b>AI Assistant</b>\n\n"
        "Ask me anything, like talking to a person. About this bot, crypto and Neo, news from the web, writing, translations, code or ideas. "
        "I remember our chat, so you can follow up:\n"
        "• <i>How do I tip in a group?</i>\n"
        "• <i>What's my balance?</i>\n"
        "• <i>What's the latest news about Neo?</i>\n"
        "• <i>Make a meme about waiting for the bridge</i> 🎭\n\n"
        "I can read your balances and the live prices. In a group I can read the thread you reply to. I can't send money or see anyone's keys, "
        "and I never ask for yours.\n"
        "Tap <b>🧹 New chat</b> to start over, or any menu button when you are done.",
        reply_markup=_ai_keyboard(),
    )


@router.message(AIFlow.asking, F.text, ~F.text.startswith("/"))
async def ai_private_question(message: Message, state: FSMContext):
    if message.text in SHORTCUT_TEXTS:
        return await keyboard_shortcut(message, state)
    if LIMITER.too_fast(message.from_user.id):
        return await message.answer("One moment, please. Ask me again in a few seconds.")
    try:
        await message.bot.send_chat_action(message.chat.id, "typing")
    except Exception:
        pass
    if memes.wants_image(message.text):
        return await _send_meme(message, message.text, reply=False, markup=_ai_keyboard())
    if assistant.wants_own_balance(message.text):  # answered straight from the wallet, it never depends on the AI
        try:
            text = await _own_balances(message.from_user.id)
        except Exception:
            log.exception("AI balance failed")
            text = "I couldn't read your balances just now 😕 Please try again in a moment, or tap 🏠 Home."
        return await message.answer(text or "You don't have a wallet yet. Send /start to create one.", reply_markup=_ai_keyboard())
    try:
        answer = await ai_reply(message.text, message.from_user.id, ("p", message.from_user.id))
    except Exception:
        log.exception("AI Assistant failed")
        answer = None
    if answer:
        await _ai_send(message, answer, _AI_CHART.pop(("p", message.from_user.id), None), reply=False, reply_markup=_ai_keyboard())
    else:
        await message.answer(NOT_SURE.format(who=_ai_support_text()), reply_markup=_ai_keyboard())


@router.message(Command("meme"))
async def meme_command(message: Message, command: CommandObject):
    """/meme your idea: the AI draws a meme."""
    idea = (command.args or "").strip()
    if not idea:
        return await message.reply("🎭 Tell me the idea, for example: <code>/meme waiting for the bridge to finish</code>")
    context = ""
    if message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        context, _said = _group_context(message, False)
    await _send_meme(message, f"make a meme: {idea}", context, reply=message.chat.type != ChatType.PRIVATE)


@router.message(Command("ai"), F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def ai_group_switch(message: Message, command: CommandObject):
    """/ai on, /ai off, /ai (status): group admins choose whether I answer questions about the bot without being mentioned."""
    arg = (command.args or "").strip().lower()
    on = message.chat.id not in AI_OFF
    if arg in ("on", "off"):
        if message.from_user is None or not await _is_event_admin(message):
            return await message.reply("Only group admins can change this.")
        if arg == "on":
            AI_OFF.discard(message.chat.id)
        else:
            AI_OFF.add(message.chat.id)
        aistore.save_off(AI_OFF)
        return await message.reply("🤖 I will now answer questions about the bot on my own here." if arg == "on" else
                                   "🤖 Okay, I will only answer when someone mentions me or replies to me.")
    await message.reply(
        f"🤖 <b>AI Assistant</b>: automatic answers are <b>{'on' if on else 'off'}</b> in this group.\n"
        "When on, I answer clear questions about this bot without being mentioned. You can always mention me "
        f"(<code>@{_BOT_ME.get('username') or 'NeoRedPillWalletBot'} your question</code>) or reply to my messages.\n"
        "Admins: <code>/ai on</code> or <code>/ai off</code>."
    )


GROUP_RECENT: dict[int, "collections.deque"] = {}  # chat id -> the last 60 messages (kept in memory only, never saved)


def _person_name(user) -> str:
    return (f"@{user.username}" if user.username else user.full_name) if user else "someone"


def _remember_group_message(message: Message) -> None:
    import collections

    buf = GROUP_RECENT.setdefault(message.chat.id, collections.deque(maxlen=60))
    parent = message.reply_to_message.message_id if message.reply_to_message else None
    buf.append((_person_name(message.from_user), (message.text or "")[:500], message.message_id, parent))
    while len(GROUP_RECENT) > 500:
        GROUP_RECENT.pop(next(iter(GROUP_RECENT)))


def _group_context(message: Message, replied_is_me: bool) -> tuple[str, str]:
    """(context text for the AI, the replied-to message's text). It has the person asking, the reply thread they point at
    (the message they replied to and the messages it was a reply to, oldest first) and the latest messages in the group."""
    buf = list(GROUP_RECENT.get(message.chat.id, ()))
    by_id = {mid: (n, t, parent) for n, t, mid, parent in buf}
    parts = [f"The person asking: {_person_name(message.from_user)}"]
    said = ""
    used: set[int] = {message.message_id}
    reply = message.reply_to_message
    if reply is not None and not replied_is_me:
        said = (reply.text or reply.caption or "").strip()
        if said:
            chain = [(_person_name(reply.from_user), said[:700])]
            used.add(reply.message_id)
            parent = by_id.get(reply.message_id, (None, None, None))[2]
            while parent is not None and parent in by_id and parent not in used and len(chain) < 8:
                n, t, nxt = by_id[parent]
                chain.append((n, t[:500]))
                used.add(parent)
                parent = nxt
            parts.append("Reply thread (oldest first; the person replied to the LAST one):\n" + "\n".join(f"{n}: {t}" for n, t in reversed(chain)))
    recent = [(n, t) for n, t, mid, _p in buf if mid not in used and t][-20:]
    if recent:
        parts.append("Latest messages in this group (oldest first):\n" + "\n".join(f"{n}: {t}" for n, t in recent))
    if len(parts) == 1:
        return "", ""
    return "\n\n".join(parts), said


@router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.text, ~F.text.startswith("/"))
async def ai_group_question(message: Message):
    if message.from_user is None or message.from_user.is_bot:
        return
    _remember_group_message(message)
    if not _BOT_ME:
        me = await message.bot.me()
        _BOT_ME.update(id=me.id, username=me.username or "")
    reply = message.reply_to_message
    replied = reply.from_user.id if (reply and reply.from_user) else None
    continuing = bool(reply and (message.chat.id, reply.message_id) in AI_MSGS)
    ask, mentioned, question = assistant.group_question(message.text or "", replied, _BOT_ME["id"], _BOT_ME["username"], continuing)
    asked_me = ask
    if not ask:
        # nobody asked me, but is this a clear question about the bot? Answer it, politely and not too often.
        if (reply is not None and replied != _BOT_ME["id"]) or message.chat.id in AI_OFF:
            return  # people talking to each other, or an admin turned this off
        idx = assistant.get_index(_help_networks(), _any_bridge())
        ai_on = bool(SETTINGS.anthropic_api_key or SETTINGS.ai_api_key)
        # with a real AI the AI decides (it stays silent when the message is not about this bot); without it, strict rules
        if not (assistant.might_be_about_bot(message.text or "", idx) if ai_on else assistant.should_auto_answer(message.text or "", idx)):
            return
        if not AUTO.allow(message.chat.id, message.from_user.id, int(time.time() // 86400)):
            return
        ask, question = True, (message.text or "").strip()
    context = ""
    if asked_me:  # I was asked directly: read what the person is pointing at
        context, replied_text = _group_context(message, replied == _BOT_ME["id"])
        if not assistant.looks_like_question(question) and replied_text and len(question) < 40:
            question = f"{question or 'Please answer this'} (the question is in the message I replied to)"
    if not question and not context:
        return await message.reply("Ask me a question about this bot, for example: <i>how do I add a token?</i>")
    question = question or "Please answer the question in the group context."
    if LIMITER.too_fast(message.from_user.id):
        return
    if asked_me and memes.wants_image(question):
        return await _send_meme(message, question, context, reply=True)
    if asked_me and assistant.wants_own_balance(question):
        return await message.reply("👛 For your privacy I never show balances in a group. Message me privately and ask again: "
                                   f"<code>@{_BOT_ME.get('username') or 'NeoRedPillWalletBot'}</code> → 🤖 AI Assistant.")
    try:
        await message.bot.send_chat_action(message.chat.id, "typing")
    except Exception:
        pass
    try:
        answer = await ai_reply(question, message.from_user.id, (message.chat.id, message.from_user.id), group=True, auto=not asked_me, context=context)
    except Exception:
        log.exception("AI Assistant failed in a group")
        answer = None
    if answer is None:
        if mentioned:  # asked directly: say so. A plain reply to my message that is off topic gets no answer.
            await message.reply(NOT_SURE_GROUP.format(who=_ai_support_text()))
        elif not asked_me:
            AUTO.release(message.chat.id, message.from_user.id)
        return
    sent = await _ai_send(message, answer, _AI_CHART.pop((message.chat.id, message.from_user.id), None), reply=True)
    _remember_ai_message(message.chat.id, sent.message_id)




async def _price_history_loop() -> None:
    """Every 10 minutes the prices the bot already reads are written to the chart history."""
    while True:
        try:
            live = await PRICES.prices()
            if live:
                pricechart.HISTORY.record(live)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Price history sample failed")
        await asyncio.sleep(pricechart.SAMPLE_EVERY)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    await init_db(ENGINE)
    async with SESSIONS() as session:
        await service.seed_defaults(session, SETTINGS.evm_chains)
        # Confirm token decimals on-chain; unverified tokens are not credited.
        for symbol, outcome in (await service.sync_token_decimals(session, NEO_RPC)).items():
            log.info("Token %s: %s", symbol, outcome)

    # Only serve EVM networks whose node answers and reports the expected chain id.
    for slug in [s for s, o in OPS.items() if isinstance(o, chains.EvmOps)]:
        problem = await chains.verify_evm_chain(OPS[slug])
        if problem:
            log.error("Network %s is DISABLED: %s", slug, problem)
            await OPS[slug].rpc.close()
            del OPS[slug]
        else:
            urls = [u for u in (getattr(OPS[slug].cfg, "rpc_url", "") or "").split(",") if u.strip()]
            host = ", ".join(u.strip().split("//")[-1].split("/")[0] for u in urls)  # hosts only, never a key
            log.info("Network %s ready (chain id %s, RPC %s)", slug, OPS[slug].cfg.chain_id, host or "?")
    for slug in [s for s, o in OPS.items() if s in service.STABLECOINS]:
        try:
            async with SESSIONS() as session:
                added = await asyncio.wait_for(service.seed_stablecoins(session, slug, OPS[slug]), 60)
            log.info("Stablecoins on %s: %s", slug, ", ".join(added) or "none confirmed")
        except Exception:
            log.exception("Could not add the stablecoins of %s", slug)
    if "solana" in OPS:  # same check for Solana: the node must answer and be the right cluster
        problem = await chains.verify_solana(OPS["solana"], SETTINGS.network)
        if problem:
            log.error("Network solana is DISABLED: %s", problem)
            await OPS["solana"].rpc.close()
            del OPS["solana"]
        else:
            log.info("Network solana ready")
    log.info("Mode: %s | networks: %s | send limits: %s", SETTINGS.network, list(OPS), SETTINGS.send_limits)

    bot = Bot(
        SETTINGS.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )
    bot.session.middleware(_PhoneMenuMiddleware())
    try:  # the "/" menu in Telegram
        menu = [
            BotCommand(command="start", description="Open your wallet"),
            BotCommand(command="home", description="Balances and prices"),
            BotCommand(command="view", description="Home page: text or photo card"),
            BotCommand(command="trade", description="Where to buy or sell a token"),
            BotCommand(command="airdrop", description="Airdrops: tasks and rewards"),
            BotCommand(command="earn", description="Claim GAS, vote, airdrops"),
            BotCommand(command="vote", description="Vote on Neo and earn GAS"),
            BotCommand(command="wallets", description="Deposit addresses"),
            BotCommand(command="send", description="Send tokens"),
            BotCommand(command="history", description="All your transactions"),
            BotCommand(command="pending", description="Transactions still waiting"),
        ]
        if _any_bridge():
            menu.append(BotCommand(command="bridge", description="Bridge coins between networks"))
        menu += [
            BotCommand(command="keys", description="Import or export a wallet"),
            BotCommand(command="addtoken", description="Add a token by contract address"),
            BotCommand(command="removetoken", description="Remove a token from your list"),
            BotCommand(command="tip", description="Tip people in a group"),
            BotCommand(command="help", description="What this bot can do"),
            BotCommand(command="cancel", description="Cancel what you are doing"),
        ]
        await bot.set_my_commands(menu)
        await bot.set_my_commands(
            [
                BotCommand(command="tip", description="Tip people in this group"),
                BotCommand(command="rain", description="Start a rain with your code"),
                BotCommand(command="quiz", description="Start a quiz with your code"),
                BotCommand(command="ai", description="AI Assistant: automatic answers on or off"),
                BotCommand(command="meme", description="Make a meme: /meme your idea"),
                BotCommand(command="help", description="What this bot can do"),
            ],
            scope=BotCommandScopeAllGroupChats(),
        )
    except Exception:
        log.exception("Could not set the bot command menu (not fatal)")

    async def notify_deposit(user, token, transfer, chain_name: str) -> None:
        amount = service.format_amount(transfer.amount, token.decimals)
        title, sender = "Deposit received", ""
        if transfer.from_address:
            async with SESSIONS() as s:
                from_user = await service.find_user_by_address(s, transfer.from_address)
            if from_user is not None and from_user.username:
                title, sender = "Transfer received", f"\nFrom: @{html.escape(from_user.username)}"
            else:
                sender = f"\nFrom: <code>{html.escape(transfer.from_address)}</code>"
        slug = next((k for k, o in OPS.items() if o.name.lower() == chain_name.lower()), None)
        tx = "\n" + historyview.tx_html(SETTINGS.network, slug, transfer.tx_hash) if transfer.tx_hash else ""
        await bot.send_message(
            user.telegram_id,
            f"📥 <b>{title}</b>\n{amount} {html.escape(token.symbol)} ({html.escape(chain_name)}){sender}{tx}",
        )

    async def notify_unlisted(user, network: str, asset: str, detail: dict | None = None) -> None:
        slug = next((k for k, o in OPS.items() if o.name.lower() == network.lower()), None)
        await send_unlisted_offer(bot, user.telegram_id, slug, network, asset, detail=detail)

    unlisted.HOOK = notify_unlisted

    async def notify_settled(user, token, chain, row, status: str) -> None:
        amount = service.format_amount(abs(int(row.amount)), token.decimals)
        title = "✅ <b>Transaction confirmed</b>" if status == "confirmed" else "❌ <b>Transaction failed</b>"
        await bot.send_message(
            user.telegram_id,
            f"{title}\n{historyview.kind_label(row.kind, row.note)} {amount} {html.escape(token.symbol)} "
            f"({html.escape(chain.name)})\n{historyview.tx_html(SETTINGS.network, chain.slug, row.tx_hash)}",
        )

    tasks = [
        asyncio.create_task(watcher.run_forever(SESSIONS, NEO_RPC, notify_deposit)),
        asyncio.create_task(reconcile.run_forever(SESSIONS, OPS, notify_settled)),
    ]
    for ops in OPS.values():
        if isinstance(ops, chains.EvmOps):
            tasks.append(asyncio.create_task(evm_watcher.run_forever(SESSIONS, ops, notify_deposit)))
        elif isinstance(ops, chains.SolanaOps):
            tasks.append(asyncio.create_task(solana_watcher.run_forever(SESSIONS, ops, notify_deposit, SETTINGS.solana_poll_seconds)))
    dp = Dispatcher(storage=MemoryStorage())
    _restore_rains(bot)  # rains that were open when the bot last stopped carry on
    _air_restore()
    tasks.append(asyncio.create_task(_air_loop(bot)))  # ends airdrops when their time is up
    tasks.append(asyncio.create_task(_price_history_loop()))  # keeps a price history for the AI Assistant's charts
    _bk_file = backup.sqlite_path(SETTINGS.database_url)

    async def _backup_targets() -> list:
        return await _owner_ids()

    tasks.append(asyncio.create_task(usage.run_forever()))
    tasks.append(asyncio.create_task(dailystats.run_forever(bot, SESSIONS, _owner_ids, PRICES)))  # one statistic a day to the owners
    if SETTINGS.backup_password and _bk_file:
        tasks.append(asyncio.create_task(backup.run_forever(bot, _backup_targets, _bk_file, SETTINGS.backup_password, SETTINGS.backup_every)))
    else:
        log.warning("Automatic backups are OFF (set BACKUP_PASSWORD in .env to turn them on)")
    dp.include_router(router)
    try:
        await dp.start_polling(bot)
    finally:
        for task in tasks:
            task.cancel()
        await NEO_RPC.close()
        for ops in OPS.values():
            if isinstance(ops, (chains.EvmOps, chains.SolanaOps)):
                await ops.rpc.close()


if __name__ == "__main__":
    asyncio.run(main())
