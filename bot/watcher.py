"""Deposit watcher: finds incoming Neo N3 token transfers to user wallets,
records them in the ledger, and notifies the user.

Safe to run repeatedly: each chain event is recorded once (the ledger has a
unique key on user/token/tx/event/kind), and every scan re-reads a short
overlap window so a lagging node cannot cause a missed deposit.
"""
import asyncio
import logging
import time
from typing import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import service, unlisted
from .db import Chain, Ledger, Token, User, Wallet
from .neo_rpc import NeoRpc, Transfer, normalize_hash

log = logging.getLogger("walletbot.watcher")

OVERLAP_MS = 5 * 60 * 1000
# (user, token, transfer, network name) -> sends the user a message
Notify = Callable[[User, Token, Transfer, str], Awaitable[None]]


def now_ms() -> int:
    return int(time.time() * 1000)


async def scan_wallet(
    session: AsyncSession,
    rpc: NeoRpc,
    wallet: Wallet,
    user: User,
    tokens_by_hash: dict[str, Token],
    now: int | None = None,
    allowed: set[int] | None = None,
    strangers: list | None = None,
) -> list[tuple[Token, Transfer]]:
    """Record new deposits for one wallet. Returns them so the caller can notify
    only after the database commit succeeded."""
    now = now if now is not None else now_ms()
    start = max(wallet.watch_from_ms, wallet.last_scan_ms - OVERLAP_MS)
    transfers = await rpc.get_received(wallet.address, start, now)

    fresh: list[tuple[Token, Transfer]] = []
    for t in transfers:
        if t.timestamp_ms < wallet.watch_from_ms:
            continue
        if t.from_address == wallet.address:  # moved between own addresses
            continue
        token = tokens_by_hash.get(t.asset)
        if token is None or (allowed is not None and token.id not in allowed):
            # Not tracked, or not on this user's list: ignored, but remembered so the user can be told.
            if strangers is not None and t.amount > 0:
                strangers.append((t.asset, t.tx_hash, t.from_address))
            continue
        if not token.decimals_verified:
            log.warning("Skipping %s deposit %s: decimals not verified yet", token.symbol, t.tx_hash)
            continue
        already = await session.scalar(
            select(Ledger.id).where(
                Ledger.user_id == user.id,
                Ledger.token_id == token.id,
                Ledger.tx_hash == t.tx_hash,
                Ledger.event_index == t.notify_index,
                Ledger.kind.in_(("deposit", "transfer_in")),
            )
        )
        if already is not None:
            continue
        # A transfer from another bot user's wallet is a transfer, not an outside deposit.
        sender_wallet = (
            await session.scalar(select(Wallet).where(Wallet.address == t.from_address))
            if t.from_address
            else None
        )
        session.add(
            Ledger(
                user_id=user.id,
                token_id=token.id,
                kind="transfer_in" if sender_wallet is not None else "deposit",
                amount=t.amount,
                counterparty_user_id=sender_wallet.user_id if sender_wallet is not None else None,
                tx_hash=t.tx_hash,
                event_index=t.notify_index,
                status="confirmed",
                note=f"from {t.from_address}" if t.from_address else None,
            )
        )
        fresh.append((token, t))

    wallet.last_scan_ms = max(wallet.last_scan_ms, now)
    await session.commit()
    return fresh


async def scan_all(session_factory, rpc: NeoRpc, notify: Notify, concurrency: int = 5) -> None:
    async with session_factory() as s:
        chain = await s.scalar(select(Chain).where(Chain.slug == "neo_n3"))
        if chain is None:
            return
        tokens = list((await s.execute(select(Token).where(Token.chain_id == chain.id))).scalars())
        wallet_ids = list(
            (await s.execute(select(Wallet.id).where(Wallet.key_type == "neo_n3"))).scalars()
        )
    tokens_by_hash = {normalize_hash(t.contract): t for t in tokens}
    chain_id, chain_name = chain.id, chain.name
    sem = asyncio.Semaphore(concurrency)

    async def one(wallet_id: int) -> None:
        async with sem:
            try:
                async with session_factory() as s:
                    wallet = await s.get(Wallet, wallet_id)
                    user = await s.get(User, wallet.user_id)
                    allowed = await service.allowed_token_ids(s, user, chain_id)
                    strangers: list[str] = []
                    fresh = await scan_wallet(
                        s, rpc, wallet, user, tokens_by_hash, allowed=allowed, strangers=strangers
                    )
            except Exception:
                log.exception("Scan failed for wallet %s", wallet_id)
                return
            for token, transfer in fresh:
                try:
                    await notify(user, token, transfer, chain_name)
                except Exception:
                    log.exception("Notification failed for user %s", user.id)
            for asset, tx_hash, sender in dict.fromkeys(strangers):
                await unlisted.report(user, chain_name, asset, tx=tx_hash, detail={"sender": sender, "tx": tx_hash})

    await asyncio.gather(*(one(i) for i in wallet_ids))


async def run_forever(session_factory, rpc: NeoRpc, notify: Notify, interval: float = 8.0) -> None:
    log.info("Deposit watcher started (every %ss)", interval)
    while True:
        try:
            await scan_all(session_factory, rpc, notify)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Deposit scan failed")
        await asyncio.sleep(interval)
