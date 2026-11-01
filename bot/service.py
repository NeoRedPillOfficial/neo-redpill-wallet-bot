"""Database operations used by the bot handlers (no Telegram code here)."""
import logging
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from . import wallets as w
from .crypto import decrypt_key, encrypt_key
from .db import Chain, Ledger, SavedWallet, Token, User, UserToken, VoteWallet, Wallet
from .neo_rpc import NeoRpc, normalize_hash
from .reconcile import PENDING_STATUSES

log = logging.getLogger("walletbot.service")

# Default chains seeded on first run: (slug, name, key_type, evm_chain_id).
# The chain id here is informational (mainnet values). The bot checks the real
# chain id of each configured RPC at startup (see chains.verify_evm_chain).
DEFAULT_CHAINS = [
    ("neo_n3", "Neo N3", "neo_n3", None),
    ("neo_x", "Neo X", "evm", 47763),
    ("bsc", "BNB Smart Chain", "evm", 56),
    ("solana", "Solana", "solana", None),
]

# (chain slug, contract, symbol, name, decimals, decimals_verified)
# NEO and GAS are protocol-fixed native contracts. NRP and FRANK decimals are
# placeholders until sync_token_decimals() reads them from the chain.
DEFAULT_TOKENS = [
    ("neo_n3", "0xef4073a0f2b305a38ec4050e4d3d28bc40ea63f5", "NEO", "NEO", 0, True),
    ("neo_n3", "0xd2a4cff31913016155e38e474a2c06d08be276cf", "GAS", "GAS", 8, True),
    ("neo_n3", "0x789518aa302b571e3e825f2c85a01ad731014a45", "NRP", "NeoRedPill", 8, False),
    ("neo_n3", "0xa06cfd7ae9dd7befb7bf8e5b8c5902c969182de0", "FRANK", "FrankCoin", 8, False),
    ("neo_x", "native", "GAS", "GAS (Neo X)", 18, True),
    ("bsc", "native", "BNB", "BNB", 18, True),
    ("solana", "native", "SOL", "SOL", 9, True),
]


# Stablecoins offered on every EVM network that has in-bot trading: slug -> [(contract, symbol, decimals)].
# The addresses are checked on the network itself before use (symbol and decimals must match), so a wrong entry is skipped.
STABLECOINS = {
    "eth": [("0xdAC17F958D2ee523a2206206994597C13D831ec7", "USDT", 6),
            ("0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", "USDC", 6),
            ("0x6B175474E89094C44Da98b954EedeAC495271d0F", "DAI", 18)],
    "bsc": [("0x55d398326f99059fF775485246999027B3197955", "USDT", 18),
            ("0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d", "USDC", 18)],
    "polygon": [("0xc2132D05D31c914a87C6611C10748AEb04B58e8F", "USDT", 6),
                ("0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359", "USDC", 6)],
    "arbitrum": [("0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9", "USDT", 6),
                 ("0xaf88d065e77c8cC2239327C5EDb3A432268e5831", "USDC", 6)],
    "optimism": [("0x94b008aA00579c1307B0EF2c499aD98a8ce58e58", "USDT", 6),
                 ("0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85", "USDC", 6)],
    "base": [("0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "USDC", 6),
             ("0xfde4C96c8593536E31F229EA8f37b2ADa2699bb2", "USDT", 6)],
    "avalanche": [("0x9702230A8Ea53601f5cD2dc00fDBc13d4dF4A8c7", "USDT", 6),
                  ("0xB97EF9Ef8734C71904D8002F8b6Bc66Dd9c48a6E", "USDC", 6)],
    "solana": [("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "USDC", 6),
               ("Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB", "USDT", 6)],
    # Flamingo's bridged USDT; its on-chain symbol may read "fUSDT", which is accepted
    "neo_n3": [("0xcd48b160c1bbc9d74997b803b9a7ad50a4bef020", "USDT", 6)],
}


def _same_contract(slug: str, a: str, b: str) -> bool:
    return str(a) == str(b) if slug == "solana" else str(a).lower() == str(b).lower()  # Solana addresses are case-sensitive


def is_stablecoin(slug: str, contract: str) -> bool:
    return any(_same_contract(slug, c, contract) for c, _s, _d in STABLECOINS.get(slug, []))


async def seed_stablecoins(session: AsyncSession, slug: str, ops) -> list[str]:
    """Adds the network's stablecoins as built-in tokens, each only after the network confirms its symbol and decimals.
    Returns the symbols added or confirmed."""
    chain = await session.scalar(select(Chain).where(Chain.slug == slug))
    if chain is None:
        return []
    done = []
    for contract, symbol, decimals in STABLECOINS.get(slug, []):
        try:
            info = await ops.token_info(contract if slug == "solana" else contract.lower())
        except Exception:
            continue
        found = info.symbol.upper()
        if info.decimals != decimals or not (found == symbol or (slug == "neo_n3" and found.endswith(symbol))):
            continue
        stored = contract if slug == "solana" else contract.lower()
        token = await session.scalar(select(Token).where(Token.chain_id == chain.id, func.lower(Token.contract) == stored.lower()))
        if token is None:
            session.add(Token(chain_id=chain.id, contract=stored, symbol=info.symbol if slug == "neo_n3" else symbol, name=symbol,
                              decimals=decimals, decimals_verified=True, is_default=True))
        else:
            token.is_default, token.decimals, token.decimals_verified = True, decimals, True
        done.append(symbol)
    await session.commit()
    return done


def now_ms() -> int:
    return int(time.time() * 1000)


async def seed_defaults(session: AsyncSession, evm_chains: dict | None = None) -> None:
    """Create the built-in chains and tokens, plus every configured EVM chain (slug -> EvmChainConfig)
    and its native coin."""
    chains: dict[str, Chain] = {}
    for slug, name, key_type, chain_id in DEFAULT_CHAINS:
        chain = (await session.execute(select(Chain).where(Chain.slug == slug))).scalar_one_or_none()
        if chain is None:
            chain = Chain(slug=slug, name=name, key_type=key_type, evm_chain_id=chain_id)
            session.add(chain)
            await session.flush()
        chains[slug] = chain
    extra_tokens = []
    for slug, cfg in (evm_chains or {}).items():
        if slug not in chains:
            chain = (await session.execute(select(Chain).where(Chain.slug == slug))).scalar_one_or_none()
            if chain is None:
                chain = Chain(slug=slug, name=cfg.name, key_type="evm", evm_chain_id=cfg.chain_id)
                session.add(chain)
                await session.flush()
            chains[slug] = chain
            extra_tokens.append((slug, "native", cfg.native_symbol, cfg.native_symbol, 18, True))
    for slug, contract, symbol, name, decimals, verified in DEFAULT_TOKENS + extra_tokens:
        exists = (
            await session.execute(
                select(Token).where(Token.chain_id == chains[slug].id, Token.contract == contract)
            )
        ).scalar_one_or_none()
        if exists is None:
            session.add(
                Token(
                    chain_id=chains[slug].id,
                    contract=contract,
                    symbol=symbol,
                    name=name,
                    decimals=decimals,
                    decimals_verified=verified,
                    is_default=True,
                )
            )
    await session.commit()


async def sync_token_decimals(session: AsyncSession, rpc: NeoRpc) -> dict[str, str]:
    """Read symbol/decimals of every Neo N3 token from the chain.

    Confirms decimals (sets decimals_verified) or corrects a wrong stored value.
    A token whose contract cannot be read stays unverified, and therefore is
    neither credited nor shown with an amount. Returns {symbol: outcome}.
    """
    chain = await session.scalar(select(Chain).where(Chain.slug == "neo_n3"))
    if chain is None:
        return {}
    outcomes: dict[str, str] = {}
    tokens = (await session.execute(select(Token).where(Token.chain_id == chain.id))).scalars()
    for token in tokens:
        try:
            symbol, decimals = await rpc.get_token_info(token.contract)
        except Exception as exc:
            log.warning("Could not read token %s (%s): %s", token.symbol, token.contract, exc)
            outcomes[token.symbol] = "unreadable"
            continue
        if symbol != token.symbol:
            log.warning("Token %s on-chain symbol is %r", token.contract, symbol)
        if decimals != token.decimals:
            log.warning("Token %s decimals corrected %s -> %s", token.symbol, token.decimals, decimals)
            token.decimals = decimals
        token.decimals_verified = True
        outcomes[token.symbol] = f"verified, {decimals} decimals"
    await session.commit()
    return outcomes


async def get_or_create_user(
    session: AsyncSession, telegram_id: int, username: str | None
) -> User:
    user = (await session.execute(select(User).where(User.telegram_id == telegram_id))).scalar_one_or_none()
    if user is None:
        user = User(telegram_id=telegram_id, username=username)
        session.add(user)
        try:
            await session.flush()
        except IntegrityError:
            # Two messages from a brand-new user were handled at the same moment and the other one created
            # the row first. This session holds nothing else yet, so drop the failed insert and use that row.
            await session.rollback()
            user = (await session.execute(select(User).where(User.telegram_id == telegram_id))).scalar_one()
            if user.username != username:
                user.username = username
    elif user.username != username:
        user.username = username
    return user


async def ensure_wallets(session: AsyncSession, user: User, master_key: str) -> list[Wallet]:
    """Create the user's EVM, Neo N3 and Solana wallets if they don't exist yet."""
    existing = {
        wl.key_type: wl
        for wl in (await session.execute(select(Wallet).where(Wallet.user_id == user.id))).scalars()
    }
    for key_type, generator in (("evm", w.evm_generate), ("neo_n3", w.neo_n3_generate), ("solana", w.solana_generate)):
        if key_type not in existing:
            kp = generator()
            started = now_ms()
            wallet = Wallet(
                user_id=user.id,
                key_type=key_type,
                address=kp.address,
                encrypted_key=encrypt_key(master_key, kp.private_key, kp.address),
                watch_from_ms=started,
                last_scan_ms=started,
            )
            session.add(wallet)
            existing[key_type] = wallet
    await session.flush()
    return list(existing.values())


async def _address_owner(session: AsyncSession, address: str):
    """('active', Wallet) or ('saved', SavedWallet) holding this address, or None."""
    wl = (await session.execute(select(Wallet).where(Wallet.address == address))).scalar_one_or_none()
    if wl is not None:
        return "active", wl
    sv = (await session.execute(select(SavedWallet).where(SavedWallet.address == address))).scalar_one_or_none()
    return ("saved", sv) if sv is not None else None


async def _stash(session: AsyncSession, current: Wallet) -> SavedWallet:
    """Put the wallet that is in use aside, unchanged. The bot never throws a wallet away."""
    saved = SavedWallet(user_id=current.user_id, key_type=current.key_type, address=current.address,
                        encrypted_key=current.encrypted_key, imported=current.imported, created_at=current.created_at)
    session.add(saved)
    return saved


def _make_active(current: Wallet, address: str, blob: bytes, imported: bool, created_at=None) -> None:
    started = now_ms()
    if current.address != address:
        current.watch_from_ms = started  # deposits are reported from now on, history is not replayed
        current.last_scan_ms = started
    current.address = address
    current.encrypted_key = blob
    current.imported = imported
    if created_at is not None:
        current.created_at = created_at


async def import_wallet(
    session: AsyncSession, user: User, master_key: str, key_type: str, text: str
) -> Wallet:
    """Use an imported key as the wallet of this type. The wallet that was in use is kept, not deleted:
    it is put aside and the user can switch back to it. The same key can't be claimed by someone else."""
    kp = w.parse_import(key_type, text)
    owner = await _address_owner(session, kp.address)
    current = (
        await session.execute(
            select(Wallet).where(Wallet.user_id == user.id, Wallet.key_type == key_type)
        )
    ).scalar_one_or_none()
    if owner is not None:
        kind, row = owner
        if row.user_id != user.id:
            raise ValueError("That wallet is already registered to another user.")
        if kind == "active":
            return row  # already the wallet in use
        return await switch_wallet(session, user, key_type, row.id)  # one the user had put aside: use it again
    blob = encrypt_key(master_key, kp.private_key, kp.address)
    if current is None:
        started = now_ms()
        current = Wallet(user_id=user.id, key_type=key_type, address=kp.address, encrypted_key=blob, imported=True,
                         watch_from_ms=started, last_scan_ms=started)
        session.add(current)
    else:
        await _stash(session, current)
        await session.flush()
        _make_active(current, kp.address, blob, True)
    await session.flush()
    return current


async def saved_wallets(session: AsyncSession, user: User) -> list[SavedWallet]:
    result = await session.execute(select(SavedWallet).where(SavedWallet.user_id == user.id).order_by(SavedWallet.id))
    return list(result.scalars())


async def has_saved_wallets(session: AsyncSession, user: User) -> bool:
    return (await session.scalar(select(SavedWallet.id).where(SavedWallet.user_id == user.id).limit(1))) is not None


async def switch_wallet(session: AsyncSession, user: User, key_type: str, saved_id: int) -> Wallet:
    """Make a wallet that was put aside the one in use. The wallet that was in use is put aside in its place."""
    saved = await session.get(SavedWallet, saved_id)
    current = (
        await session.execute(select(Wallet).where(Wallet.user_id == user.id, Wallet.key_type == key_type))
    ).scalar_one_or_none()
    if saved is None or saved.user_id != user.id or saved.key_type != key_type or current is None:
        raise ValueError("That wallet is not available any more.")
    address, blob, imported, created = saved.address, saved.encrypted_key, saved.imported, saved.created_at
    await session.delete(saved)
    await session.flush()
    await _stash(session, current)
    await session.flush()
    _make_active(current, address, blob, imported, created)
    await session.flush()
    return current


async def delete_wallet(session: AsyncSession, user: User, master_key: str, key_type: str, saved_id: int | None) -> Wallet | None:
    """Delete one of the user's wallets for good (the caller has asked twice).
    saved_id None = the wallet in use: another saved wallet of the same type takes its place; if there is none,
    a brand-new wallet is created. Returns the wallet now in use when it changed, else None."""
    if saved_id is not None:
        saved = await session.get(SavedWallet, saved_id)
        if saved is None or saved.user_id != user.id or saved.key_type != key_type:
            raise ValueError("That wallet is not available any more.")
        await session.delete(saved)
        await session.flush()
        return None
    current = (
        await session.execute(select(Wallet).where(Wallet.user_id == user.id, Wallet.key_type == key_type))
    ).scalar_one_or_none()
    if current is None:
        raise ValueError("No wallet of that type.")
    others = [s for s in await saved_wallets(session, user) if s.key_type == key_type]
    if others:
        nxt = others[-1]  # the one put aside most recently
        address, blob, imported, created = nxt.address, nxt.encrypted_key, nxt.imported, nxt.created_at
        await session.delete(nxt)
        await session.flush()
        _make_active(current, address, blob, imported, created)
    else:
        generator = {"evm": w.evm_generate, "neo_n3": w.neo_n3_generate, "solana": w.solana_generate}[key_type]
        kp = generator()
        _make_active(current, kp.address, encrypt_key(master_key, kp.private_key, kp.address), False, datetime.now(timezone.utc))
    await session.flush()
    return current


async def export_saved_wallet(session: AsyncSession, user: User, master_key: str, key_type: str, saved_id: int) -> str:
    saved = await session.get(SavedWallet, saved_id)
    if saved is None or saved.user_id != user.id or saved.key_type != key_type:
        raise ValueError("That wallet is not available any more.")
    secret = decrypt_key(master_key, saved.encrypted_key, saved.address)
    return w.export_text(w.KeyPair(key_type, saved.address, secret))


async def export_wallet(session: AsyncSession, user: User, master_key: str, key_type: str) -> str:
    wallet = (
        await session.execute(
            select(Wallet).where(Wallet.user_id == user.id, Wallet.key_type == key_type)
        )
    ).scalar_one_or_none()
    if wallet is None:
        raise ValueError("No wallet of that type.")
    secret = decrypt_key(master_key, wallet.encrypted_key, wallet.address)
    return w.export_text(w.KeyPair(key_type, wallet.address, secret))


def format_amount(raw: int, decimals: int) -> str:
    value = Decimal(raw) / (Decimal(10) ** decimals)
    text = f"{value:f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


# ------------------------------------------------------- tokens and balances
async def get_chain(session: AsyncSession, slug: str) -> Chain | None:
    return await session.scalar(select(Chain).where(Chain.slug == slug))


async def get_wallet(session: AsyncSession, user: User, key_type: str) -> Wallet | None:
    return await session.scalar(
        select(Wallet).where(Wallet.user_id == user.id, Wallet.key_type == key_type)
    )


async def get_neo_wallet(session: AsyncSession, user: User) -> Wallet | None:
    return await get_wallet(session, user, "neo_n3")


async def visible_tokens(session: AsyncSession, user: User, chain_id: int) -> list[Token]:
    """The chain's default tokens plus the ones this user added."""
    added = select(UserToken.token_id).where(UserToken.user_id == user.id)
    result = await session.execute(
        select(Token)
        .where(Token.chain_id == chain_id, or_(Token.is_default.is_(True), Token.id.in_(added)))
        .order_by(Token.id)
    )
    return list(result.scalars())


async def allowed_token_ids(session: AsyncSession, user: User, chain_id: int) -> set[int]:
    return {t.id for t in await visible_tokens(session, user, chain_id)}


async def native_token(session: AsyncSession, chain_id: int) -> Token | None:
    return await session.scalar(
        select(Token).where(Token.chain_id == chain_id, Token.contract == "native")
    )


async def chain_view(
    session: AsyncSession, user: User, chain_slug: str, ops
) -> list[tuple[Token, int | None]]:
    """The user's tokens on one network with live on-chain amounts.

    An amount is None when it can't be shown honestly: the node is unreachable
    or the token's decimals are not verified yet.
    """
    chain = await get_chain(session, chain_slug)
    wallet = await get_wallet(session, user, ops.key_type)
    if chain is None or wallet is None:
        return []
    tokens = await visible_tokens(session, user, chain.id)
    try:
        balances = await ops.balances(wallet.address, tokens)
    except Exception as exc:
        log.warning("Balance lookup failed on %s for user %s: %s", chain_slug, user.id, exc)
        balances = None
    rows: list[tuple[Token, int | None]] = []
    for token in tokens:
        if balances is None or not token.decimals_verified:
            rows.append((token, None))
        else:
            rows.append((token, balances.get(ops.token_key(token), 0)))
    return rows


async def find_token(session: AsyncSession, chain_id: int, contract: str) -> Token | None:
    return await session.scalar(
        select(Token).where(Token.chain_id == chain_id, func.lower(Token.contract) == contract.lower())
    )


async def look_alike_tokens(
    session: AsyncSession, chain_id: int, symbol: str, contract: str
) -> list[Token]:
    """Other tokens on this chain with the same symbol but a different contract:
    the classic fake-token trick."""
    # Only tokens somebody actually uses count: defaults and tokens on at least one user's list.
    # A token the bot merely saw arrive (an airdrop nobody accepted) is not a reason to warn.
    in_use = select(UserToken.token_id)
    result = await session.execute(
        select(Token).where(
            Token.chain_id == chain_id,
            func.lower(Token.symbol) == symbol.lower(),
            func.lower(Token.contract) != contract.lower(),
            or_(Token.is_default.is_(True), Token.id.in_(in_use)),
        )
    )
    return list(result.scalars())


async def add_token_for_user(
    session: AsyncSession, user: User, chain_id: int, contract: str, symbol: str, name: str, decimals: int
) -> tuple[Token, bool]:
    """Put a token (read from its contract) on the user's list.

    Returns (token, newly_added). Decimals come from the contract itself, so
    the token is marked verified.
    """
    token = await find_token(session, chain_id, contract)
    if token is None:
        token = Token(
            chain_id=chain_id,
            contract=contract,
            symbol=symbol,
            name=name,
            decimals=decimals,
            decimals_verified=True,
            is_default=False,
            added_by=user.id,
        )
        session.add(token)
        await session.flush()
    elif token.decimals != decimals or not token.decimals_verified:
        token.decimals = decimals
        token.decimals_verified = True
    already = token.is_default or await session.scalar(
        select(UserToken.id).where(UserToken.user_id == user.id, UserToken.token_id == token.id)
    )
    if already:
        await session.commit()
        return token, False
    session.add(UserToken(user_id=user.id, token_id=token.id))
    await session.commit()
    return token, True


async def removable_tokens(session: AsyncSession, user: User) -> list[tuple[Token, Chain]]:
    """Tokens the user added themselves (the built-in ones can't be removed), with their network."""
    rows = await session.execute(
        select(Token, Chain)
        .join(UserToken, UserToken.token_id == Token.id)
        .join(Chain, Chain.id == Token.chain_id)
        .where(UserToken.user_id == user.id, Token.is_default.is_(False))
        .order_by(Chain.id, Token.id)
    )
    return [(t, c) for t, c in rows.all()]


async def remove_token(session: AsyncSession, user: User, token_id: int) -> Token | None:
    """Take a token off the user's list. Only hides it: their balance, their history and other
    people's lists are untouched, and they can add it again any time. Returns None if there was
    nothing to remove (not on their list, or a built-in token)."""
    token = await session.get(Token, token_id)
    if token is None or token.is_default:
        return None
    entry = await session.scalar(
        select(UserToken).where(UserToken.user_id == user.id, UserToken.token_id == token_id)
    )
    if entry is None:
        return None
    await session.delete(entry)
    await session.commit()
    return token


# ------------------------------------------------------------ sending support
async def find_recipient(
    session: AsyncSession, handle: str, key_type: str = "neo_n3"
) -> tuple[User, Wallet] | None:
    """Resolve '@username' to a bot user and their wallet of the given key type."""
    name = handle.strip().lstrip("@").lower()
    if not name:
        return None
    user = await session.scalar(select(User).where(func.lower(User.username) == name))
    if user is None:
        return None
    wallet = await get_wallet(session, user, key_type)
    return (user, wallet) if wallet is not None else None


async def find_user_by_telegram_id(session: AsyncSession, telegram_id: int) -> User | None:
    return await session.scalar(select(User).where(User.telegram_id == telegram_id))


async def find_user_by_username(session: AsyncSession, name: str) -> User | None:
    name = (name or "").strip().lstrip("@").lower()
    if not name:
        return None
    return await session.scalar(select(User).where(func.lower(User.username) == name))


async def all_telegram_ids(session: AsyncSession) -> list[int]:
    return [int(x) for x in (await session.scalars(select(User.telegram_id)))]


async def find_user_by_address(session: AsyncSession, address: str) -> User | None:
    wallet = await session.scalar(select(Wallet).where(Wallet.address == address))
    if wallet is None and address.lower().startswith("0x"):
        # EVM addresses are the same in any letter case (stored checksummed); Neo N3 ones are not.
        wallet = await session.scalar(select(Wallet).where(func.lower(Wallet.address) == address.lower()))
    return await session.get(User, wallet.user_id) if wallet is not None else None


async def sent_last_24h(session: AsyncSession, user: User, token: Token) -> int:
    """Total of this token the user sent (or has pending) in the last 24 hours."""
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    total = await session.scalar(
        select(func.coalesce(func.sum(Ledger.amount), 0)).where(
            Ledger.user_id == user.id,
            Ledger.token_id == token.id,
            Ledger.kind.in_(("withdraw", "transfer_out")),
            Ledger.status.in_(("pending", "confirmed", "unknown")),
            Ledger.created_at >= since,
        )
    )
    return -int(total)  # outgoing rows are stored as negative amounts


async def get_private_key(session: AsyncSession, user: User, master_key: str, key_type: str) -> bytes:
    wallet = await session.scalar(
        select(Wallet).where(Wallet.user_id == user.id, Wallet.key_type == key_type)
    )
    if wallet is None:
        raise ValueError("No wallet of that type.")
    return decrypt_key(master_key, wallet.encrypted_key, wallet.address)


# ------------------------------------------------------------ vote wallets
async def get_vote_wallets(session: AsyncSession, user: User) -> list[VoteWallet]:
    result = await session.execute(select(VoteWallet).where(VoteWallet.user_id == user.id).order_by(VoteWallet.id))
    return list(result.scalars())


async def get_vote_wallet(session: AsyncSession, user: User, candidate: str) -> VoteWallet | None:
    return await session.scalar(
        select(VoteWallet).where(VoteWallet.user_id == user.id, VoteWallet.candidate == candidate.lower())
    )


async def create_vote_wallet(session: AsyncSession, user: User, master_key: str, candidate: str) -> VoteWallet:
    """A new vote wallet for this candidate. The caller must commit BEFORE moving any NEO into it."""
    kp = w.neo_n3_generate()
    wallet = VoteWallet(
        user_id=user.id,
        candidate=candidate.lower(),
        address=kp.address,
        encrypted_key=encrypt_key(master_key, kp.private_key, kp.address),
    )
    session.add(wallet)
    await session.flush()
    return wallet


def vote_wallet_key(master_key: str, wallet: VoteWallet) -> bytes:
    return decrypt_key(master_key, wallet.encrypted_key, wallet.address)


async def record_outgoing(
    session: AsyncSession,
    user: User,
    token: Token,
    kind: str,  # withdraw | transfer_out
    raw_amount: int,
    note: str,
    counterparty_user_id: int | None = None,
) -> Ledger:
    """Write the pending row BEFORE broadcasting, so a crash can't lose the record."""
    row = Ledger(
        user_id=user.id,
        token_id=token.id,
        kind=kind,
        amount=-raw_amount,
        counterparty_user_id=counterparty_user_id,
        status="pending",
        note=note[:250],
    )
    session.add(row)
    await session.commit()
    return row


# ---------------------------------------------------------------------- history
def _history_query(user: User, pending_only: bool):
    query = (
        select(Ledger, Token, Chain)
        .join(Token, Token.id == Ledger.token_id)
        .join(Chain, Chain.id == Token.chain_id)
        .where(Ledger.user_id == user.id)
    )
    return query.where(Ledger.status.in_(PENDING_STATUSES)) if pending_only else query


async def history_page(
    session: AsyncSession, user: User, pending_only: bool, offset: int, limit: int
) -> list[tuple[Ledger, Token, Chain]]:
    """The user's ledger rows, newest first, with each row's token and network."""
    query = _history_query(user, pending_only).order_by(Ledger.created_at.desc(), Ledger.id.desc())
    result = await session.execute(query.offset(offset).limit(limit))
    return [(row, token, chain) for row, token, chain in result.all()]


async def history_count(session: AsyncSession, user: User, pending_only: bool) -> int:
    query = select(func.count()).select_from(Ledger).where(Ledger.user_id == user.id)
    if pending_only:
        query = query.where(Ledger.status.in_(PENDING_STATUSES))
    return int(await session.scalar(query) or 0)


async def bridges_in_transit(
    session: AsyncSession, user: User, minutes: int = 20
) -> list[tuple[Ledger, Token, Chain]]:
    """Bridges that left their source network recently and haven't shown up as a deposit
    on any other network yet. Judged from our own records only, so it is a good guess,
    not proof."""
    since = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    sent = (
        await session.execute(
            _history_query(user, False)
            .where(Ledger.kind == "bridge_out", Ledger.status == "confirmed", Ledger.created_at >= since)
            .order_by(Ledger.created_at.desc())
        )
    ).all()
    if not sent:
        return []
    landed = (
        await session.execute(
            select(Ledger.created_at, Token.chain_id)
            .join(Token, Token.id == Ledger.token_id)
            .where(Ledger.user_id == user.id, Ledger.kind == "deposit", Ledger.created_at >= since)
        )
    ).all()

    def aware(moment: datetime) -> datetime:
        return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)

    return [
        (row, token, chain)
        for row, token, chain in sent
        if not any(
            chain_id != chain.id and aware(created) >= aware(row.created_at) for created, chain_id in landed
        )
    ]


async def usernames(session: AsyncSession, user_ids: set[int]) -> dict[int, str | None]:
    if not user_ids:
        return {}
    result = await session.execute(select(User.id, User.username).where(User.id.in_(user_ids)))
    return {uid: name for uid, name in result.all()}


async def record_incoming_from_user(
    session: AsyncSession, recipient: User, token: Token, sender_id: int, raw_amount: int,
    tx_hash: str, sender_address: str,
) -> Ledger | None:
    """Write the receiver's side of a transfer sent by another user of this bot, right after it confirms.

    Returns the new row, or None if this transfer was already recorded (by the chain scan, or earlier).
    The row uses event_index -1; the chain watchers treat -1 as "already recorded" for that transaction.
    """
    seen = await session.scalar(
        select(Ledger.id).where(
            Ledger.user_id == recipient.id,
            Ledger.token_id == token.id,
            Ledger.tx_hash == tx_hash,
            Ledger.kind.in_(("deposit", "transfer_in")),
        )
    )
    if seen is not None:
        return None
    row = Ledger(
        user_id=recipient.id, token_id=token.id, kind="transfer_in", amount=raw_amount,
        counterparty_user_id=sender_id, tx_hash=tx_hash, event_index=-1, status="confirmed",
        note=f"from {sender_address}"[:250],
    )
    session.add(row)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return None
    return row
