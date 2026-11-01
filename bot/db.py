"""Database schema.

Money is stored as integers in the token's smallest unit (like wei / GAS
fractions) to avoid floating point errors. Each user's funds live in their
own on-chain wallet; the `ledger` table records activity for history and
notifications.
"""
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    wallets: Mapped[list["Wallet"]] = relationship(back_populates="user")


class Wallet(Base):
    """One key pair per user per key type.

    key_type 'evm'    -> one address, used on Neo X and every other EVM chain.
    key_type 'neo_n3' -> Neo N3 address (secp256r1, different format).
    """

    __tablename__ = "wallets"
    __table_args__ = (UniqueConstraint("user_id", "key_type"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    key_type: Mapped[str] = mapped_column(String(16))
    address: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    encrypted_key: Mapped[bytes] = mapped_column(LargeBinary)
    imported: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    # Deposit watcher (unix ms). watch_from_ms is the floor: transfers older than
    # it are never reported, so a newly created or imported wallet does not
    # replay history. last_scan_ms is how far the watcher has scanned.
    watch_from_ms: Mapped[int] = mapped_column(BigInteger, default=0)
    last_scan_ms: Mapped[int] = mapped_column(BigInteger, default=0)

    user: Mapped[User] = relationship(back_populates="wallets")


class SavedWallet(Base):
    """A wallet the user has put aside (for example after importing another one). It is never deleted by the bot:
    the user can switch back to it, export it, or delete it themselves. Only the 'wallets' table row is in use."""

    __tablename__ = "saved_wallets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    key_type: Mapped[str] = mapped_column(String(16))
    address: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    encrypted_key: Mapped[bytes] = mapped_column(LargeBinary)
    imported: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class VoteWallet(Base):
    """A small Neo N3 wallet the bot keeps for one of the user's votes.

    A Neo account votes for ONE candidate with ALL its NEO. To let a person vote with any amount, and for several
    candidates, each candidate they vote for gets its own account here. The key is encrypted like a normal wallet
    key, so it is in the database backup and needs the same master key.
    """

    __tablename__ = "vote_wallets"
    __table_args__ = (UniqueConstraint("user_id", "candidate"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    candidate: Mapped[str] = mapped_column(String(66))  # the candidate's public key (hex)
    address: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    encrypted_key: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Chain(Base):
    __tablename__ = "chains"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(32), unique=True)  # neo_n3, neo_x, bsc...
    name: Mapped[str] = mapped_column(String(64))
    key_type: Mapped[str] = mapped_column(String(16))  # evm | neo_n3 | solana
    evm_chain_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rpc_url: Mapped[str | None] = mapped_column(String(256), nullable=True)
    last_scanned_block: Mapped[int] = mapped_column(BigInteger, default=0)


class Token(Base):
    __tablename__ = "tokens"
    __table_args__ = (UniqueConstraint("chain_id", "contract"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chain_id: Mapped[int] = mapped_column(ForeignKey("chains.id"), index=True)
    # Contract hash (Neo N3) or contract address (EVM). 'native' for coin on EVM.
    contract: Mapped[str] = mapped_column(String(66))
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(128))
    decimals: Mapped[int] = mapped_column(Integer)
    # False until decimals were confirmed on-chain. Unverified tokens are never
    # credited or displayed with an amount, because a wrong decimals value
    # would show users the wrong balance.
    decimals_verified: Mapped[bool] = mapped_column(default=False)
    is_default: Mapped[bool] = mapped_column(default=False)
    added_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    chain: Mapped[Chain] = relationship()


class UserToken(Base):
    """A token a user added themselves (by contract address).

    Token metadata is shared, but visibility is per user: a token one person
    adds never shows up in anyone else's wallet unless they add it too. This
    keeps scam tokens from spreading.
    """

    __tablename__ = "user_tokens"
    __table_args__ = (UniqueConstraint("user_id", "token_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    token_id: Mapped[int] = mapped_column(ForeignKey("tokens.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class NativeBalance(Base):
    """Last seen native-coin balance of an EVM wallet on a chain.

    EVM nodes have no 'transfers received' query for the native coin (BNB, GAS),
    so the watcher spots deposits as increases between two checks.
    """

    __tablename__ = "native_balances"
    __table_args__ = (UniqueConstraint("wallet_id", "chain_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    wallet_id: Mapped[int] = mapped_column(ForeignKey("wallets.id"), index=True)
    chain_id: Mapped[int] = mapped_column(ForeignKey("chains.id"))
    amount: Mapped[int] = mapped_column(Numeric(78, 0), default=0)


class Ledger(Base):
    """Append-only activity history (deposits, withdrawals, transfers, tips).

    This is a record of what happened, not the source of truth for balances:
    funds live in each user's own on-chain wallet, so balances are read from
    the chain. The unique constraint makes recording a chain event idempotent.
    """

    __tablename__ = "ledger"
    __table_args__ = (
        Index("ix_ledger_user_created", "user_id", "created_at"),
        UniqueConstraint("user_id", "token_id", "tx_hash", "event_index", "kind"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    token_id: Mapped[int] = mapped_column(ForeignKey("tokens.id"))
    # deposit | withdraw | transfer_in | transfer_out | tip_in | tip_out | bridge | fee
    kind: Mapped[str] = mapped_column(String(16))
    amount: Mapped[int] = mapped_column(Numeric(78, 0))  # signed: + credit, - debit
    counterparty_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    tx_hash: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)  # Solana ids are 88 characters
    event_index: Mapped[int] = mapped_column(Integer, default=0)  # n-th transfer in the tx
    status: Mapped[str] = mapped_column(String(12), default="confirmed")
    note: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class SolCursor(Base):
    """How far the Solana deposit watcher has read a wallet's transaction list:
    the newest signature it has already processed."""

    __tablename__ = "sol_cursors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    wallet_id: Mapped[int] = mapped_column(ForeignKey("wallets.id"), unique=True)
    last_signature: Mapped[str | None] = mapped_column(String(100), nullable=True)


def make_session_factory(database_url: str):
    if database_url.startswith("sqlite"):
        # Many watchers (one per network) write to the same file. Wait up to 30 s for a busy file instead of failing with
        # "database is locked", and use the write-ahead log, which lets readers and one writer work at the same time.
        engine = create_async_engine(database_url, future=True, connect_args={"timeout": 30})

        from sqlalchemy import event

        @event.listens_for(engine.sync_engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):
            cur = dbapi_conn.cursor()
            try:
                cur.execute("PRAGMA busy_timeout=30000")
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA synchronous=NORMAL")
            except Exception:
                pass
            finally:
                cur.close()
    else:
        engine = create_async_engine(database_url, future=True)
    return engine, async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def init_db(engine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
