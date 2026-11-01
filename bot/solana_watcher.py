"""Deposit watcher for Solana.

For each wallet it reads the list of confirmed transactions that touch the address, newer than
the last one it processed, and records what the wallet received (SOL, and tokens the user has on
their list). Reading transactions rather than comparing balances gives the exact amount, the
sender and the transaction id.
"""
import asyncio
import logging

from sqlalchemy import select

from . import service, unlisted
from .db import Ledger, SolCursor, Token, User, Wallet
from .neo_rpc import Transfer
from .solana_parse import NATIVE, parse_deposits

log = logging.getLogger("walletbot.solana_watcher")


async def scan_wallet(session_factory, ops, notify, wallet_id: int) -> None:
    rpc = ops.rpc
    fresh: list[tuple[User, Token, Transfer]] = []
    strangers: list[tuple[User, str, str, str | None]] = []
    async with session_factory() as s:
        wallet = await s.get(Wallet, wallet_id)
        user = await s.get(User, wallet.user_id)
        chain = await service.get_chain(s, ops.slug)
        if chain is None:
            return
        cursor = await s.scalar(select(SolCursor).where(SolCursor.wallet_id == wallet_id))
        until = cursor.last_signature if cursor else None
        signatures = await rpc.signatures_since(wallet.address, until)

        if cursor is None:
            cursor = SolCursor(wallet_id=wallet_id, last_signature=None)
            s.add(cursor)
            if wallet.imported:
                # An imported wallet may already have a long history: start from now.
                cursor.last_signature = signatures[0]["signature"] if signatures else None
                await s.commit()
                return
        if not signatures:
            await s.commit()
            return

        tokens = await service.visible_tokens(s, user, chain.id)
        by_asset = {NATIVE if t.contract == NATIVE else t.contract: t for t in tokens if t.decimals_verified}
        mints = {a for a in by_asset if a != NATIVE}
        ours = {w.address: w for w in (await s.execute(select(Wallet).where(Wallet.key_type == "solana"))).scalars()}

        for entry in reversed(signatures):  # oldest first
            if entry.get("err") is not None:
                continue
            signature = entry["signature"]
            tx = await rpc.get_transaction(signature)
            if tx is None:
                raise RuntimeError(f"transaction {signature} not available yet")  # retry next round, cursor unchanged
            meta = tx.get("meta") or {}
            seen_mints = {
                b["mint"] for b in (meta.get("postTokenBalances") or []) if b.get("owner") == wallet.address
            }
            for deposit in parse_deposits(tx, wallet.address, mints | seen_mints):
                token = by_asset.get(deposit.asset)
                if token is None:
                    if deposit.amount > 0:
                        strangers.append((user, deposit.asset, signature, deposit.sender))
                    continue
                seen = await s.scalar(
                    select(Ledger.id).where(
                        Ledger.user_id == user.id,
                        Ledger.token_id == token.id,
                        Ledger.tx_hash == signature,
                        Ledger.kind.in_(("deposit", "transfer_in")),
                    )
                )
                if seen is not None:
                    continue
                sender_wallet = ours.get(deposit.sender) if deposit.sender else None
                s.add(
                    Ledger(
                        user_id=user.id,
                        token_id=token.id,
                        kind="transfer_in" if sender_wallet is not None else "deposit",
                        amount=deposit.amount,
                        counterparty_user_id=sender_wallet.user_id if sender_wallet is not None else None,
                        tx_hash=signature,
                        event_index=0,
                        status="confirmed",
                        note=f"from {deposit.sender}" if deposit.sender else None,
                    )
                )
                fresh.append(
                    (user, token, Transfer(signature, token.contract, deposit.amount, deposit.sender, service.now_ms(), 0))
                )
        cursor.last_signature = signatures[0]["signature"]  # only after everything above succeeded
        await s.commit()
        chain_name = chain.name

    for user, token, transfer in fresh:
        try:
            await notify(user, token, transfer, chain_name)
        except Exception:
            log.exception("Notification failed for user %s", user.id)
    for user, mint, signature, sender in strangers:
        await unlisted.report(user, chain_name, mint, tx=signature, detail={"sender": sender, "tx": signature})


class RateLimited(Exception):
    """The RPC answered 429: stop this pass and wait, instead of hammering it with every wallet."""


COOLDOWN_SECONDS = 60.0
_warned_429 = False


async def scan_all(session_factory, ops, notify, concurrency: int = 1) -> bool:
    """Scan every Solana wallet once. Returns False if the RPC rate-limited us (caller should wait)."""
    global _warned_429
    async with session_factory() as s:
        wallet_ids = list((await s.execute(select(Wallet.id).where(Wallet.key_type == "solana"))).scalars())
    sem = asyncio.Semaphore(concurrency)
    limited = False

    async def one(wallet_id: int) -> None:
        nonlocal limited
        async with sem:
            if limited:
                return
            try:
                await scan_wallet(session_factory, ops, notify, wallet_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if getattr(exc, "status", None) == 429:
                    limited = True
                    return
                log.warning("Solana scan failed for wallet %s: %s: %s", wallet_id, type(exc).__name__, exc)

    await asyncio.gather(*(one(i) for i in wallet_ids))
    if limited and not _warned_429:
        _warned_429 = True
        log.warning(
            "The Solana RPC is rate-limiting this server (429). Waiting %ss between tries. "
            "Deposits are detected late until you set SOLANA_RPC to a provider with a free key (Helius, Alchemy, QuickNode).",
            int(COOLDOWN_SECONDS),
        )
    return not limited


async def run_forever(session_factory, ops, notify, interval: float = 10.0) -> None:
    log.info("Solana watcher started (every %ss)", interval)
    while True:
        try:
            ok = await scan_all(session_factory, ops, notify)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Solana watcher pass failed")
            ok = True
        await asyncio.sleep(interval if ok else COOLDOWN_SECONDS)
