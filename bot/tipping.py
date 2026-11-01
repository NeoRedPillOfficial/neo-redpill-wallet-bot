"""Group tipping: parse /tip, plan the amounts, hold the pending confirmation, run the sends.

Pure logic lives here so it can be tested without Telegram. main.py only wires it up.
Every recipient gets its own real on-chain transfer, paid for by the sender.
"""
import re
import secrets
import time
from dataclasses import dataclass, field

from . import neo_send, service

MAX_RECIPIENTS = 10
PENDING_TTL_SECONDS = 300
USAGE = (
    "<b>How to tip</b>\n"
    "<code>/tip @alice @bob 5 USDT</code> — 5 USDT to each person\n"
    "<code>/tip @alice @bob 10 USDT split</code> — 10 USDT shared equally\n"
    "Reply to someone's message with <code>/tip 5 GAS</code> to tip them.\n"
    "Add a network if the symbol exists on several, e.g. <code>/tip @alice 5 USDT bsc</code>."
)

_HANDLE = re.compile(r"^@[A-Za-z0-9_]{3,32}$")
_NETWORK_ALIASES = {
    "n3": "neon3", "neo": "neon3", "x": "neox", "bnb": "bsc", "ethereum": "eth",
    "matic": "polygon", "pol": "polygon", "arb": "arbitrum", "op": "optimism", "avax": "avalanche", "sol": "solana",
}


class TipError(Exception):
    """A problem the sender can fix; the message is safe to show in the group."""


@dataclass
class TipRequest:
    handles: list[str]
    amount_text: str
    symbol: str
    network: str | None
    split: bool


def _norm_net(value: str) -> str:
    key = re.sub(r"[^a-z0-9]", "", value.lower())
    return _NETWORK_ALIASES.get(key, key)


def parse_tip(args: str) -> TipRequest:
    """Parse the text after /tip: @handles, one amount, a symbol, optional network and 'split'."""
    handles: list[str] = []
    rest: list[str] = []
    split = False
    for part in args.split():
        if part.startswith("@"):
            if not _HANDLE.match(part):
                raise TipError(f"{part} is not a valid username.")
            if part.lower() not in (h.lower() for h in handles):  # same person twice counts once
                handles.append(part)
        elif part.lower() == "split":
            split = True
        else:
            rest.append(part)
    if len(rest) not in (2, 3):
        raise TipError("I need an amount and a token symbol.")
    amount_text, symbol = rest[0], rest[1]
    if not symbol.isalnum():
        raise TipError("That token symbol doesn't look right.")
    return TipRequest(handles, amount_text, symbol, rest[2] if len(rest) == 3 else None, split)


def pick_token(candidates: list, symbol: str, network: str | None):
    """candidates: [(Token, network_slug, network_name)] the sender can use. Returns one entry."""
    found = [c for c in candidates if c[0].symbol.lower() == symbol.lower()]
    if network:
        want = _norm_net(network)
        found = [c for c in found if want in (_norm_net(c[1]), _norm_net(c[2]))]
    if not found:
        where = f" on {network}" if network else ""
        raise TipError(f"You don't have {symbol}{where} in your token list.")
    if len(found) > 1:
        nets = ", ".join(sorted({c[1] for c in found}))
        raise TipError(f"{symbol} exists on several networks ({nets}). Add the network, e.g. /tip 5 {symbol} {found[0][1]}.")
    return found[0]


def amounts(req: TipRequest, decimals: int, count: int) -> tuple[int, int]:
    """Returns (each, total) in raw units."""
    if count < 1:
        raise TipError("Tell me who to tip: mention @usernames or reply to a message.")
    if count > MAX_RECIPIENTS:
        raise TipError(f"You can tip at most {MAX_RECIPIENTS} people at once.")
    try:
        value = neo_send.parse_amount(req.amount_text, decimals)
    except neo_send.SendError as exc:
        raise TipError(str(exc))
    if req.split:
        each = value // count
        if each <= 0:
            raise TipError("That amount is too small to split between so many people.")
        return each, each * count
    return value, value * count


# ------------------------------------------------------------ pending confirmations
@dataclass
class PendingTip:
    sender_tg_id: int
    chat_id: int
    slug: str
    token_id: int
    each_raw: int
    recipients: list  # [(user_id, label, address)]
    expires: float = field(default_factory=lambda: time.monotonic() + PENDING_TTL_SECONDS)


class PendingStore:
    """In-memory, single-use. Lost on restart, which just means 'tap /tip again'."""

    def __init__(self):
        self._items: dict[str, PendingTip] = {}

    def add(self, item: PendingTip) -> str:
        self._purge()
        nonce = secrets.token_hex(8)
        self._items[nonce] = item
        return nonce

    def peek(self, nonce: str) -> PendingTip | None:
        self._purge()
        return self._items.get(nonce)

    def take(self, nonce: str) -> PendingTip | None:
        self._purge()
        return self._items.pop(nonce, None)

    def _purge(self) -> None:
        now = time.monotonic()
        for key in [k for k, v in self._items.items() if v.expires < now]:
            del self._items[key]


# ------------------------------------------------------------------ execution
@dataclass
class TipResult:
    label: str
    status: str  # confirmed | failed | unknown | skipped
    tx_hash: str = ""
    detail: str = ""
    recipient_id: int | None = None  # the receiving user of this bot, when there is one


async def run_tip(
    session, ops, user, token, wallet, private_key, balance_tokens, recipients, each_raw, send_limits
) -> list[TipResult]:
    """Send `each_raw` to every recipient, one after another, re-checking before each.

    Stops at the first problem: later recipients are reported as skipped and nothing
    is sent for them, so one fault can't repeat across the list.
    """
    results: list[TipResult] = []
    stopped = False
    for user_id, label, address in recipients:
        if stopped:
            results.append(TipResult(label, "skipped", detail="not sent"))
            continue
        try:
            balances = await ops.balances(wallet.address, balance_tokens)
            used = await service.sent_last_24h(session, user, token)
            neo_send.check_limits(token.symbol, token.decimals, each_raw, used, send_limits)
            await ops.check_send(token, wallet.address, address, each_raw, balances)
        except neo_send.SendError as exc:
            results.append(TipResult(label, "failed", detail=str(exc)))
            stopped = True
            continue
        except Exception:
            results.append(TipResult(label, "failed", detail="could not check your balance"))
            stopped = True
            continue
        row = await service.record_outgoing(
            session, user, token, "transfer_out", each_raw, f"tip to {address} on {ops.slug}", user_id
        )
        try:
            outcome = await ops.send(private_key, token, wallet.address, address, each_raw)
        except neo_send.SendError as exc:
            row.status = "failed"
            row.note = f"{row.note} | {str(exc)[:100]}"[:250]
            await session.commit()
            results.append(TipResult(label, "failed", detail=str(exc)))
            stopped = True
            continue
        except Exception:
            row.status = "failed"
            await session.commit()
            results.append(TipResult(label, "failed", detail="error before sending, nothing was sent"))
            stopped = True
            continue
        row.tx_hash = outcome.tx_hash
        row.status = outcome.status
        await session.commit()
        results.append(TipResult(label, outcome.status, outcome.tx_hash, outcome.detail or "", user_id))
        if outcome.status != "confirmed":
            stopped = True
    return results
