"""Transaction history text. Pure functions (no Telegram, no database), so they can be tested.

Every row of the ledger becomes an Entry; render_history turns a list of entries into
one Telegram message. Times are shown in UTC.
"""
import html
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

MAX_LEN = 3900
STALE_PENDING = timedelta(minutes=15)

# Block explorers, only used for links. Anything not listed here simply gets no link.
EXPLORERS = {
    "mainnet": {
        "neo_n3": "https://dora.coz.io/transaction/neo3/mainnet/{tx}",
        "neo_x": "https://xexplorer.neo.org/tx/{tx}",
        "bsc": "https://bscscan.com/tx/{tx}",
        "eth": "https://etherscan.io/tx/{tx}",
        "polygon": "https://polygonscan.com/tx/{tx}",
        "arbitrum": "https://arbiscan.io/tx/{tx}",
        "optimism": "https://optimistic.etherscan.io/tx/{tx}",
        "base": "https://basescan.org/tx/{tx}",
        "avalanche": "https://snowtrace.io/tx/{tx}",
        "solana": "https://solscan.io/tx/{tx}",
    },
    "testnet": {
        "neo_n3": "https://dora.coz.io/transaction/neo3/testnet/{tx}",
        "bsc": "https://testnet.bscscan.com/tx/{tx}",
        "eth": "https://sepolia.etherscan.io/tx/{tx}",
        "solana": "https://solscan.io/tx/{tx}?cluster=devnet",
    },
}
EXPLORER_NAMES = {
    "neo_n3": "Dora", "neo_x": "Neo X Explorer", "bsc": "BscScan", "eth": "Etherscan", "polygon": "PolygonScan",
    "arbitrum": "Arbiscan", "optimism": "Optimistic Etherscan", "base": "BaseScan", "avalanche": "Snowtrace",
    "solana": "Solscan",
}
_TX_HASH = re.compile(r"(0x)?[0-9a-fA-F]{64}|[1-9A-HJ-NP-Za-km-z]{80,90}")  # hex id, or a Solana (base58) signature
_NOTE_TARGET = re.compile(r"^(?:tip )?to (\S+) on ")

KIND_LABEL = {
    "deposit": "⬇️ Deposit",
    "transfer_in": "⬇️ Received",
    "withdraw": "⬆️ Withdraw",
    "transfer_out": "↗️ Sent",
    "bridge_out": "🌉 Bridge out",
    "swap": "🔄 Swap",
}


@dataclass(frozen=True)
class Entry:
    kind: str
    status: str  # pending | confirmed | failed | unknown
    amount_text: str  # already signed and formatted, e.g. "+12.5" or "-3"
    symbol: str
    chain_name: str
    tx_hash: str | None
    note: str | None
    created_at: datetime
    counterparty: str | None = None  # @username of the other side, if they use the bot
    url: str | None = None
    in_transit: bool = False  # a bridge that left its source network and hasn't landed yet


def explorer_url(network: str, slug: str, tx_hash: str | None) -> str | None:
    template = EXPLORERS.get(network, {}).get(slug)
    if not template or not tx_hash or not _TX_HASH.fullmatch(tx_hash):
        return None
    return template.format(tx=tx_hash)


def tx_html(network: str, slug: str | None, tx_hash: str | None) -> str:
    """Telegram HTML for a transaction: the hash (tap to copy) and, where we know the network's block
    explorer, a link to watch it live on-chain. Empty if there is no hash."""
    if not tx_hash:
        return ""
    text = f"Tx: <code>{html.escape(tx_hash)}</code>"
    url = explorer_url(network, slug or "", tx_hash)
    if url:
        text += f'\n🔗 <a href="{html.escape(url, quote=True)}">View on {EXPLORER_NAMES.get(slug, "block explorer")}</a>'
    return text


def aware(moment: datetime) -> datetime:
    """SQLite hands back naive datetimes; they are stored as UTC."""
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _short(address: str) -> str:
    return f"{address[:6]}…{address[-4:]}" if len(address) > 14 else address


def kind_label(kind: str, note: str | None) -> str:
    if kind == "transfer_out" and (note or "").startswith("tip "):
        return "🎁 Tip sent"
    return KIND_LABEL.get(kind, html.escape(kind))


def _other_side(entry: Entry) -> str | None:
    outgoing = entry.kind in ("withdraw", "transfer_out")
    if entry.counterparty:
        return f"{'to' if outgoing else 'from'} {html.escape(entry.counterparty)}"
    match = _NOTE_TARGET.match(entry.note or "")
    if outgoing and match:
        return f"to <code>{html.escape(_short(match.group(1)))}</code>"
    return None


def _status_text(entry: Entry, now: datetime) -> str:
    if entry.in_transit:
        return "🚚 On its way (usually a few minutes)"
    if entry.status == "confirmed":
        return "✅ Confirmed"
    if entry.status == "failed":
        return "❌ Failed"
    if entry.status == "unknown":
        return "⚠️ Sent, not confirmed yet (I keep checking)"
    if not entry.tx_hash and now - aware(entry.created_at) > STALE_PENDING:
        return "⏳ Pending. No transaction was recorded, so check your balance before trying again"
    return "⏳ Pending"


def _entry_text(entry: Entry, now: datetime) -> str:
    sym = html.escape(entry.symbol)
    head = f"{kind_label(entry.kind, entry.note)}  <b>{html.escape(entry.amount_text)} {sym}</b>"
    where = html.escape(entry.chain_name)
    other = _other_side(entry)
    if other:
        where += f" · {other}"
    stamp = aware(entry.created_at).strftime("%d %b %H:%M")
    tail = f"{_status_text(entry, now)} · {stamp}"
    if entry.url:
        tail += f' · <a href="{html.escape(entry.url, quote=True)}">view tx</a>'
    return f"{head}\n{where}\n{tail}"


def render_history(
    entries: list[Entry], *, pending: bool, page: int, pages: int, total: int, now: datetime
) -> str:
    if pending:
        title = "⏳ <b>Pending transactions</b>"
        intro = ("Waiting for the network. I re-check these every minute and message you when they settle.\n"
                 "There is nothing to approve or cancel here: a transaction that was sent to the network can't be cancelled, "
                 "it only waits to be confirmed. Records that were never sent can be cleared with 🧹 below.")
    else:
        title = "📜 <b>Transaction history</b>"
        intro = f"Page {page + 1} of {pages} · {total} in total · times are UTC"
    if not entries:
        empty = (
            "Nothing is pending. 🎉"
            if pending
            else "No transactions yet. Deposits, sends, tips and bridges will show up here."
        )
        return f"{title}\n\n{empty}"
    shown = list(entries)
    while True:
        body = "\n\n".join(_entry_text(e, now) for e in shown)
        more = "" if len(shown) == len(entries) else f"\n\n…and {len(entries) - len(shown)} more."
        text = f"{title}\n{intro}\n\n{body}{more}"
        if len(text) <= MAX_LEN or len(shown) <= 1:
            return text
        shown.pop()
