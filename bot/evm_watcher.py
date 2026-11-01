"""Deposit watcher for EVM chains (BNB Smart Chain, Neo X, ...).

Two mechanisms, because EVM nodes can't list "everything received":
  * ERC-20 / BEP-20 tokens: scan Transfer event logs addressed to our wallets,
    block range by block range, with a cursor stored per chain. Many free nodes refuse
    that search; the watcher then switches to reading blocks one by one (works on any
    node, sees every token) and looks at the receipts of transactions that name our wallets.
  * Native coin (BNB, GAS): compare each wallet's balance at a recent, settled
    block with the last value we saw. An increase is a deposit.

Both only look at blocks that are `confirmations` behind the chain head, so a
small reorg can't produce a phantom deposit. Every recorded event is unique in
the ledger, so scanning twice never double-credits.
"""
import asyncio
import time
import logging

from sqlalchemy import select

from eth_utils import to_checksum_address

from . import service, unlisted
from .chains import clean_label
from .evm_rpc import LogsRestricted
from .db import Chain, Ledger, NativeBalance, Token, User, Wallet
from .neo_rpc import Transfer
from .evm_send import NATIVE

log = logging.getLogger("walletbot.evm_watcher")

_last_heartbeat: dict[str, float] = {}
HEARTBEAT_SECONDS = 120.0

MAX_BLOCK_SPAN = 1000  # blocks per scan; keeps eth_getLogs within provider limits
BLOCK_SPAN = 150  # blocks per scan when reading blocks one by one
BLOCK_CATCHUP_LIMIT = 6000  # further behind than this, skip to now instead of reading thousands of old blocks


async def _register_seen_token(s, rpc, chain, contract: str) -> Token | None:
    """A token nobody has listed yet: read it from its contract and keep a row, so the transfer can be
    shown in history and the user's Yes button has something to add. The row is on nobody's list."""
    existing = await service.find_token(s, chain.id, contract)
    if existing is not None:
        return existing
    try:
        symbol, name, decimals = await rpc.token_info(to_checksum_address(contract))
    except Exception as exc:
        log.info("%s: could not read token %s (%s); notice will name the contract only", chain.slug, contract, exc)
        return None
    symbol = clean_label(symbol, 32) or "UNKNOWN"
    name = clean_label(name, 128) or symbol
    token = Token(
        chain_id=chain.id, contract=to_checksum_address(contract), symbol=symbol, name=name,
        decimals=decimals, decimals_verified=True, is_default=False, added_by=None,
    )
    s.add(token)
    await s.flush()
    return token


async def scan_token_logs(session_factory, ops, notify) -> None:
    cfg, rpc = ops.cfg, ops.rpc
    head = await rpc.block_number()
    safe = head - cfg.confirmations
    fresh: list[tuple[User, Token, Transfer]] = []
    # (user, contract, tx hash, detail for the message) of transfers of tokens that are not on the user's list
    strangers: list[tuple[User, str, str, dict, bool]] = []

    async with session_factory() as s:
        chain = await service.get_chain(s, cfg.slug)
        if chain is None:
            return
        if chain.last_scanned_block == 0:  # first run: start now, don't replay history
            chain.last_scanned_block = safe
            await s.commit()
            return
        reading_blocks = rpc.address_required or rpc.block_mode
        if reading_blocks and safe - chain.last_scanned_block > BLOCK_CATCHUP_LIMIT:
            log.warning(
                "%s: the bot was %s blocks behind; skipping ahead to now (transfers in the gap get no notice, "
                "balances are unaffected).", cfg.slug, safe - chain.last_scanned_block,
            )
            chain.last_scanned_block = safe - 20
            await s.commit()
            return
        start = chain.last_scanned_block + 1
        end = min(safe, start + (BLOCK_SPAN if reading_blocks else MAX_BLOCK_SPAN) - 1)
        if end < start:
            return
        wallets = list((await s.execute(select(Wallet).where(Wallet.key_type == "evm"))).scalars())
        by_address = {w.address.lower(): w for w in wallets}
        tokens = {
            t.contract.lower(): t
            for t in (await s.execute(select(Token).where(Token.chain_id == chain.id))).scalars()
            if t.contract != NATIVE
        }
        logs = []
        if by_address:
            if not reading_blocks:
                try:
                    logs = await rpc.get_open_logs(start, end, list(by_address))
                except LogsRestricted as exc:
                    rpc.block_mode = True
                    reading_blocks = True
                    log.warning(
                        "%s: this RPC does not answer log searches (%s). Switching to reading blocks one by one: "
                        "it works on any RPC and sees every token, but uses more requests.",
                        cfg.slug, str(exc)[:200],
                    )
                    end = min(end, start + BLOCK_SPAN - 1)
            if reading_blocks:
                logs, end = await rpc.get_transfers_by_blocks(start, end, list(by_address))
                if end < start:  # the node does not have the next block yet
                    return

        if logs:
            log.info("%s: blocks %s-%s: %s token transfer(s) to bot wallets", cfg.slug, start, end, len(logs))
        now = time.monotonic()
        if now - _last_heartbeat.get(cfg.slug, 0.0) >= HEARTBEAT_SECONDS:
            _last_heartbeat[cfg.slug] = now
            log.info("%s: token scan ok (%s), scanned up to block %s, chain at %s, watching %s wallets and %s tokens",
                     cfg.slug, "reading blocks" if reading_blocks else "log search", end, head, len(by_address), len(tokens))
        allowed_cache: dict[int, set[int]] = {}
        for lg in logs:
            wallet, token = by_address.get(lg.recipient), tokens.get(lg.token)
            if wallet is None or lg.sender == lg.recipient or lg.amount <= 0:
                continue
            user = await s.get(User, wallet.user_id)
            if user.id not in allowed_cache:
                allowed_cache[user.id] = await service.allowed_token_ids(s, user, chain.id)
            sender_wallet = by_address.get(lg.sender)
            if token is None or token.id not in allowed_cache[user.id]:
                if token is None:
                    token = await _register_seen_token(s, rpc, chain, lg.token)
                    if token is not None:
                        tokens[lg.token] = token
                log.info("%s: %s sent an unlisted token %s to user %s (will send a notice)",
                         cfg.slug, lg.sender, lg.token, user.id)
                detail = {"tx": lg.tx_hash}
                if token is not None:
                    detail["amount"] = service.format_amount(lg.amount, token.decimals)
                    detail["symbol"] = token.symbol
                    # Keep it in the receiver's history, and skip the notice if the bot already
                    # sent one when the sender's transfer confirmed.
                    known = await s.scalar(
                        select(Ledger.id).where(
                            Ledger.user_id == user.id, Ledger.token_id == token.id,
                            Ledger.tx_hash == lg.tx_hash, Ledger.event_index.in_((lg.log_index, -1)),
                            Ledger.kind.in_(("deposit", "transfer_in")),
                        )
                    )
                    if known is not None:
                        continue
                    s.add(Ledger(
                        user_id=user.id, token_id=token.id,
                        kind="transfer_in" if sender_wallet is not None else "deposit", amount=lg.amount,
                        counterparty_user_id=sender_wallet.user_id if sender_wallet is not None else None,
                        tx_hash=lg.tx_hash, event_index=lg.log_index, status="confirmed", note=f"from {lg.sender}",
                    ))
                sender_user = await s.get(User, sender_wallet.user_id) if sender_wallet is not None else None
                detail["sender"] = f"@{sender_user.username}" if sender_user is not None and sender_user.username else lg.sender
                strangers.append((user, lg.token, lg.tx_hash, detail, sender_wallet is not None))
                continue
            if not token.decimals_verified:
                continue
            seen = await s.scalar(
                select(Ledger.id).where(
                    Ledger.user_id == user.id,
                    Ledger.token_id == token.id,
                    Ledger.tx_hash == lg.tx_hash,
                    Ledger.event_index.in_((lg.log_index, -1)),  # -1: recorded when the sender's transfer confirmed
                    Ledger.kind.in_(("deposit", "transfer_in")),
                )
            )
            if seen is not None:
                continue
            s.add(
                Ledger(
                    user_id=user.id,
                    token_id=token.id,
                    kind="transfer_in" if sender_wallet is not None else "deposit",
                    amount=lg.amount,
                    counterparty_user_id=sender_wallet.user_id if sender_wallet is not None else None,
                    tx_hash=lg.tx_hash,
                    event_index=lg.log_index,
                    status="confirmed",
                    note=f"from {lg.sender}",
                )
            )
            fresh.append(
                (user, token, Transfer(lg.tx_hash, token.contract, lg.amount, lg.sender, service.now_ms(), lg.log_index))
            )
        chain.last_scanned_block = end  # only after everything above succeeded
        await s.commit()
        chain_name = chain.name

    for user, token, transfer in fresh:
        try:
            await notify(user, token, transfer, chain_name)
        except Exception:
            log.exception("Notification failed for user %s", user.id)
    for user, contract, tx_hash, detail, from_bot_user in strangers:
        await unlisted.report(user, chain_name, contract, tx=tx_hash, detail=detail, priority=from_bot_user)


# ---------------------------------------------------------------- finding the transaction behind a coin deposit
NATIVE_LOOKBACK = 200  # blocks to search for the incoming transaction when the last checked block is not known
_last_safe: dict[tuple[str, int], int] = {}  # (network, wallet) -> block at which the balance was last read


def incoming_txs(blocks, wallet_address: str) -> list[tuple[str, str, int]]:
    """[(tx hash, sender, amount)] of plain coin transfers to the wallet found in full blocks (oldest first)."""
    me = wallet_address.lower()
    out = []
    for block in blocks:
        for tx in (block or {}).get("transactions", []) or []:
            try:
                if str(tx.get("to") or "").lower() != me:
                    continue
                value = int(tx.get("value", "0x0"), 16)
            except (ValueError, TypeError, AttributeError):
                continue
            if value > 0:
                out.append((str(tx["hash"]).lower(), str(tx.get("from") or "").lower(), value))
    return out


def split_deposit(deposit: int, txs: list[tuple[str, str, int]], known: set[str]) -> tuple[list[tuple[str, str, int]], int, int]:
    """Match a balance increase to the transactions behind it.
    Returns (new transactions to record, amount already recorded for known ones, remainder with no transaction found).
    Contract-sent coins and fees make the sum differ, so the remainder keeps the totals exact."""
    left = deposit
    fresh: list[tuple[str, str, int]] = []
    already = 0
    for tx_hash, sender, value in txs:
        if left <= 0:
            break
        amount = min(value, left)
        left -= amount
        if tx_hash in known:
            already += amount
        else:
            fresh.append((tx_hash, sender, amount))
    return fresh, already, left


async def _blocks_between(rpc, low: int, high: int, wide: int = 8) -> list:
    """Full blocks low..high (a few at a time). Missing blocks are skipped."""
    sem = asyncio.Semaphore(wide)

    async def one(n: int):
        async with sem:
            try:
                return await rpc.get_block(n)
            except Exception:
                return None
    return list(await asyncio.gather(*(one(n) for n in range(low, high + 1))))


async def scan_native(session_factory, ops, notify, concurrency: int = 5) -> None:
    cfg, rpc = ops.cfg, ops.rpc
    safe = (await rpc.block_number()) - cfg.confirmations
    async with session_factory() as s:
        chain = await service.get_chain(s, cfg.slug)
        token = await service.native_token(s, chain.id) if chain else None
        if chain is None or token is None:
            return
        wallet_ids = list((await s.execute(select(Wallet.id).where(Wallet.key_type == "evm"))).scalars())
        chain_id, chain_name = chain.id, chain.name
    sem = asyncio.Semaphore(concurrency)

    async def one(wallet_id: int) -> None:
        async with sem:
            try:
                async with session_factory() as s:
                    wallet = await s.get(Wallet, wallet_id)
                    user = await s.get(User, wallet.user_id)
                    balance = await rpc.get_balance(wallet.address, block=safe)
                    row = await s.scalar(
                        select(NativeBalance).where(
                            NativeBalance.wallet_id == wallet_id, NativeBalance.chain_id == chain_id
                        )
                    )
                    deposits: list[tuple[str, str | None, int]] = []  # (tx hash or "", sender, amount)
                    if row is None:
                        # A wallet we generated started empty, so anything on it is a deposit.
                        # An imported wallet may already hold funds: take that as the baseline.
                        row = NativeBalance(
                            wallet_id=wallet_id,
                            chain_id=chain_id,
                            amount=balance if wallet.imported else 0,
                        )
                        s.add(row)
                    if balance > int(row.amount):
                        amount_up = balance - int(row.amount)
                        txs = []
                        try:  # find the real transaction(s), so history and the notice can link to them
                            low = _last_safe.get((cfg.slug, wallet_id), safe - NATIVE_LOOKBACK) + 1
                            low = max(low, safe - NATIVE_LOOKBACK)
                            txs = incoming_txs(await _blocks_between(rpc, low, safe), wallet.address)
                        except Exception:
                            log.exception("Could not look up the transaction behind a deposit on %s", cfg.slug)
                        known = set()
                        if txs:
                            known = set((await s.execute(
                                select(Ledger.tx_hash).where(
                                    Ledger.user_id == user.id, Ledger.token_id == token.id,
                                    Ledger.tx_hash.in_([t[0] for t in txs]), Ledger.kind.in_(("deposit", "transfer_in")))
                            )).scalars())
                        fresh_txs, _already, rest = split_deposit(amount_up, txs, known)
                        for tx_hash, sender, amount in fresh_txs:
                            s.add(Ledger(user_id=user.id, token_id=token.id, kind="deposit", amount=amount, tx_hash=tx_hash,
                                         event_index=0, status="confirmed", note=f"from {sender}"))
                            deposits.append((tx_hash, sender, amount))
                        if rest > 0:
                            s.add(Ledger(user_id=user.id, token_id=token.id, kind="deposit", amount=rest, status="confirmed",
                                         note="balance increase"))
                            deposits.append(("", None, rest))
                    row.amount = balance
                    await s.commit()
                    _last_safe[(cfg.slug, wallet_id)] = safe
            except Exception:
                log.exception("Native scan failed for wallet %s", wallet_id)
                return
            for tx_hash, sender, amount in deposits:
                try:
                    transfer = Transfer(tx_hash, NATIVE, amount, sender, service.now_ms(), 0)
                    await notify(user, token, transfer, chain_name)
                except Exception:
                    log.exception("Notification failed for user %s", user.id)

    await asyncio.gather(*(one(i) for i in wallet_ids))


async def run_forever(session_factory, ops, notify, interval: float = 6.0) -> None:
    log.info("EVM watcher started for %s (every %ss)", ops.cfg.slug, interval)
    while True:
        for step in (scan_token_logs, scan_native):
            try:
                await step(session_factory, ops, notify)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("%s failed on %s", step.__name__, ops.cfg.slug)
        await asyncio.sleep(interval)
