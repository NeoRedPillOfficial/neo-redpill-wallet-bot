"""Settles transactions that were sent but not confirmed in time.

When a send, tip or bridge is broadcast but the bot could not see it confirm within its
wait, the ledger row is left as "unknown". This job asks the network about those rows
once a minute and marks them confirmed or failed, and tells the user.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

from .db import Chain, Ledger, Token, User

log = logging.getLogger("walletbot.reconcile")

PENDING_STATUSES = ("pending", "unknown")
LOOK_BACK = timedelta(days=7)  # older unconfirmed rows are left alone
MAX_ROWS = 100
STALE_NO_HASH = timedelta(minutes=15)  # a pending row with no transaction after this long was never sent
AUTO_NO_HASH = timedelta(hours=1)  # the background job tidies those up by itself after this long


async def check_status(ops, kind: str, tx_hash: str) -> str | None:
    """'confirmed' or 'failed' once the network has the transaction, None while it doesn't."""
    if hasattr(ops.rpc, "get_signature_status"):  # Solana
        status = await ops.rpc.get_signature_status(tx_hash)
        if not status:
            return None
        if status.get("err"):
            return "failed"
        return "confirmed" if status.get("confirmationStatus") in ("confirmed", "finalized") else None
    if hasattr(ops.rpc, "get_receipt"):  # an EVM network
        receipt = await ops.rpc.get_receipt(tx_hash)
        if not receipt:
            return None
        return "confirmed" if str(receipt.get("status", "")).lower() in ("0x1", "1") else "failed"
    # Neo N3. A bridge deposit returns nothing, token transfers return true.
    outcome = await ops.rpc.get_tx_outcome(tx_hash, expect_true=(kind != "bridge_out"))
    if outcome is None:
        return None
    return "confirmed" if outcome[0] else "failed"


async def clear_stale(session_factory, user_id: int | None = None, older: timedelta = STALE_NO_HASH) -> int:
    """Pending rows that never got a transaction hash can't ever be confirmed (nothing was recorded as sent). Mark them
    failed so they stop showing as pending. Only the history record changes; balances are read from the chain."""
    cutoff = datetime.now(timezone.utc) - older
    query = update(Ledger).where(Ledger.status.in_(PENDING_STATUSES), Ledger.tx_hash.is_(None), Ledger.created_at < cutoff)
    if user_id is not None:
        query = query.where(Ledger.user_id == user_id)
    async with session_factory() as session:
        result = await session.execute(query.values(status="failed"))
        await session.commit()
    return int(result.rowcount or 0)


async def run_once(session_factory, ops_by_slug: dict, notify=None, user_id: int | None = None) -> int:
    """Check every unsettled row that has a transaction hash. Returns how many were settled.

    notify(user, token, chain, row, new_status) is called for each settled row (optional).
    """
    since = datetime.now(timezone.utc) - LOOK_BACK
    query = (
        select(Ledger, Token, Chain)
        .join(Token, Token.id == Ledger.token_id)
        .join(Chain, Chain.id == Token.chain_id)
        .where(
            Ledger.status.in_(PENDING_STATUSES),
            Ledger.tx_hash.is_not(None),
            Ledger.created_at >= since,
        )
        .order_by(Ledger.id)
        .limit(MAX_ROWS)
    )
    if user_id is not None:
        query = query.where(Ledger.user_id == user_id)
    settled = []
    async with session_factory() as session:
        for row, token, chain in (await session.execute(query)).all():
            ops = ops_by_slug.get(chain.slug)
            if ops is None:
                continue
            try:
                new_status = await check_status(ops, row.kind, row.tx_hash)
            except Exception as exc:
                log.warning("Could not check tx %s on %s: %s", row.tx_hash, chain.slug, exc)
                continue
            if new_status:
                row.status = new_status
                settled.append((row, token, chain))
        await session.commit()
        if notify is not None:
            for row, token, chain in settled:
                user = await session.get(User, row.user_id)
                try:
                    await notify(user, token, chain, row, row.status)
                except Exception:
                    log.exception("Could not notify user %s", row.user_id)
    return len(settled)


async def run_forever(session_factory, ops_by_slug: dict, notify=None, interval: float = 60.0) -> None:
    while True:
        try:
            await run_once(session_factory, ops_by_slug, notify)
            await clear_stale(session_factory, older=AUTO_NO_HASH)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Reconcile pass failed")
        await asyncio.sleep(interval)
