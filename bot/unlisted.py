"""A notice for tokens that arrive but are not on the user's list.

The bot only tracks tokens a user has added (so scam tokens that anyone can airdrop stay out of
sight). Without a notice, someone who sends a real token they forgot to add would think the bot
is broken. So every non-zero transfer of an unlisted token produces a message ("you received X, do
you want to add it?"). Limits keep a spammer from flooding anyone: one message per transfer, at most
MAX_PER_HOUR per user (transfers from another user of this bot are never held back), and nothing at
all for zero-value transfers.
"""
import logging
import time

log = logging.getLogger("walletbot.unlisted")

MAX_PER_HOUR = 20
_WINDOW = 3600.0
_MAX_REMEMBERED = 20000

HOOK = None  # set by the bot: async (user, network_name, asset, detail) -> None; detail is a dict or None
_seen: set[tuple] = set()
_sent: dict[int, list[float]] = {}


def allow(user_id: int, network: str, asset: str, now: float | None = None,
          tx: str | None = None, priority: bool = False) -> bool:
    """Whether to message this user about this transfer now (and remember that we did).

    With a transaction hash each transfer is its own notice; without one (networks whose scan has no
    hash to give) it is one notice per token. `priority` (the sender is another user of this bot)
    skips the hourly limit.
    """
    now = time.monotonic() if now is None else now
    key = (user_id, network, asset.lower(), (tx or "").lower())
    if key in _seen:
        return False
    recent = [t for t in _sent.get(user_id, []) if now - t < _WINDOW]
    _sent[user_id] = recent
    if len(recent) >= MAX_PER_HOUR and not priority:
        return False
    if len(_seen) >= _MAX_REMEMBERED:
        _seen.clear()
    _seen.add(key)
    recent.append(now)
    return True


async def report(user, network: str, asset: str, tx: str | None = None, detail: dict | None = None,
                 priority: bool = False) -> None:
    if HOOK is None:
        return
    if not allow(user.id, network, asset, tx=tx, priority=priority):
        log.info("Heads-up for %s on %s to user %s skipped (already told, or hourly limit reached)", asset, network, user.id)
        return
    try:
        await HOOK(user, network, asset, detail)
        log.info("Heads-up for %s on %s sent to user %s", asset, network, user.id)
    except Exception:
        log.exception("Could not send the unlisted-token notice to user %s", user.id)
