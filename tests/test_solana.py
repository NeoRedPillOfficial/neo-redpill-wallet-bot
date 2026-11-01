"""Tests for Solana support. No network: everything uses fakes."""
import os
import struct
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from bot import solana_keys as k
from bot import solana_parse, solana_send
from bot.neo_send import SendError
from bot.solana_rpc import SolRpcError

# ------------------------------------------------------------------ keys, base58
def test_ed25519_matches_the_rfc_test_vector():
    seed = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
    assert k.from_seed(seed).public_key.hex() == "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"


def test_base58_round_trip_and_the_system_program():
    assert k.b58encode(bytes(32)) == "1" * 32  # the system program's well-known address
    assert k.b58decode("1" * 32) == bytes(32)
    for _ in range(100):
        data = os.urandom(32)
        assert k.b58decode(k.b58encode(data)) == data
    with pytest.raises(ValueError):
        k.b58decode("0OIl")  # characters that aren't in the alphabet


def test_address_validation():
    assert k.is_valid_address(k.generate().address)
    assert k.is_valid_address(k.b58encode(k.TOKEN_PROGRAM))
    for bad in ("", "abc", "0x" + "ab" * 20, "O" * 44, "1" * 31, None):
        assert not k.is_valid_address(bad)


def test_program_ids_are_32_bytes():
    for program in (k.TOKEN_PROGRAM, k.TOKEN_2022_PROGRAM, k.ASSOCIATED_TOKEN_PROGRAM, k.METADATA_PROGRAM):
        assert len(program) == 32


def test_wallet_import_export_formats():
    kp = k.generate()
    phantom = k.export_text(kp)  # base58 of seed + public key
    assert len(k.b58decode(phantom)) == 64
    for text in (phantom, kp.seed.hex(), str(list(kp.seed + kp.public_key)), k.b58encode(kp.seed)):
        assert k.parse_import(text).address == kp.address
    wrong_half = k.b58encode(kp.seed + k.generate().public_key)
    for bad in ("", "hello world", "[1, 2, 3]", wrong_half, k.b58encode(os.urandom(40))):
        with pytest.raises(ValueError):
            k.parse_import(bad)


def test_real_keys_are_on_the_curve_and_program_addresses_are_not():
    for _ in range(40):
        assert k.is_on_curve(k.generate().public_key)
    pda, bump = k.find_program_address([b"seed"], k.TOKEN_PROGRAM)
    assert not k.is_on_curve(pda) and 0 <= bump <= 255
    assert k.find_program_address([b"seed"], k.TOKEN_PROGRAM) == (pda, bump)  # deterministic
    owner, mint = k.generate().public_key, k.generate().public_key
    ata = k.associated_token_address(owner, mint)
    assert not k.is_on_curve(ata) and ata != k.associated_token_address(mint, owner)


def test_metadata_parsing():
    def padded(text, size):
        raw = text.encode()
        return struct.pack("<I", size) + raw + b"\x00" * (size - len(raw))

    data = bytes([4]) + bytes(32) + bytes(32) + padded("USD Coin", 32) + padded("USDC", 10) + b"tail"
    assert k.parse_metadata(data) == ("USD Coin", "USDC")
    with pytest.raises(Exception):
        k.parse_metadata(b"\x04" + bytes(10))
    with pytest.raises(ValueError):
        k.parse_metadata(bytes([4]) + bytes(64) + struct.pack("<I", 9999))


# --------------------------------------------------------------- transactions
def read_transaction(raw: bytes) -> dict:
    """An independent reader for a legacy transaction, so building and reading don't share a bug."""
    assert raw[0] == 1
    signature, message = raw[1:65], raw[65:]
    n_signers, n_readonly_signed, n_readonly_unsigned = message[0], message[1], message[2]
    count = message[3]
    keys = [message[4 + 32 * i : 36 + 32 * i] for i in range(count)]
    pos = 4 + 32 * count
    blockhash, pos = message[pos : pos + 32], pos + 32
    n_instructions, pos = message[pos], pos + 1
    instructions = []
    for _ in range(n_instructions):
        program, n_accounts = message[pos], message[pos + 1]
        accounts = list(message[pos + 2 : pos + 2 + n_accounts])
        pos += 2 + n_accounts
        length = message[pos]
        data, pos = message[pos + 1 : pos + 1 + length], pos + 1 + length
        instructions.append((keys[program], [keys[i] for i in accounts], data))
    assert pos == len(message)  # nothing left over
    return dict(
        signature=signature, message=message, keys=keys, blockhash=blockhash, instructions=instructions,
        header=(n_signers, n_readonly_signed, n_readonly_unsigned),
    )


def test_sol_transfer_transaction_layout_and_signature():
    sender, recipient = k.generate(), k.generate()
    blockhash = os.urandom(32)
    message = k.compile_message(sender.public_key, [k.system_transfer(sender.public_key, recipient.public_key, 1_500_000)], blockhash)
    raw = k.sign_transaction(sender.seed, message)
    tx = read_transaction(raw)
    assert tx["header"] == (1, 0, 1)  # one signer; the system program is the one read-only unsigned account
    assert tx["keys"] == [sender.public_key, recipient.public_key, k.SYSTEM_PROGRAM]
    assert tx["blockhash"] == blockhash
    program, accounts, data = tx["instructions"][0]
    assert program == k.SYSTEM_PROGRAM and accounts == [sender.public_key, recipient.public_key]
    assert data == struct.pack("<IQ", 2, 1_500_000)
    Ed25519PublicKey.from_public_bytes(sender.public_key).verify(tx["signature"], tx["message"])  # raises if wrong
    assert k.first_signature(raw) == k.b58encode(tx["signature"])


def test_token_transfer_with_account_creation_layout():
    sender, recipient, mint = k.generate(), k.generate(), k.generate().public_key
    source = k.generate().public_key
    destination = k.associated_token_address(recipient.public_key, mint)
    instructions = [
        k.create_associated_token_account_idempotent(sender.public_key, recipient.public_key, mint),
        k.token_transfer_checked(source, mint, destination, sender.public_key, 2_500_000, 6),
    ]
    raw = k.sign_transaction(sender.seed, k.compile_message(sender.public_key, instructions, os.urandom(32)))
    tx = read_transaction(raw)
    assert tx["keys"][0] == sender.public_key and tx["header"][0] == 1
    create, transfer = tx["instructions"]
    assert create[0] == k.ASSOCIATED_TOKEN_PROGRAM and create[2] == bytes([1])
    assert create[1] == [sender.public_key, destination, recipient.public_key, mint, k.SYSTEM_PROGRAM, k.TOKEN_PROGRAM]
    assert transfer[0] == k.TOKEN_PROGRAM and transfer[1] == [source, mint, destination, sender.public_key]
    assert transfer[2] == struct.pack("<BQB", 12, 2_500_000, 6)
    Ed25519PublicKey.from_public_bytes(sender.public_key).verify(tx["signature"], tx["message"])


def test_compact_u16():
    assert k.compact_u16(0) == b"\x00" and k.compact_u16(127) == b"\x7f"
    assert k.compact_u16(128) == b"\x80\x01" and k.compact_u16(16384) == b"\x80\x80\x01"


# ------------------------------------------------------- reading received funds
WALLET, SENDER, MINT = "WalletWalletWalletWalletWalletWalletWa", "SenderSenderSenderSenderSenderSenderSe", "MintMintMintMintMintMintMintMintMintMint"


def _tx(keys, pre, post, pre_tokens=(), post_tokens=(), err=None):
    return {
        "meta": {
            "err": err, "preBalances": pre, "postBalances": post,
            "preTokenBalances": list(pre_tokens), "postTokenBalances": list(post_tokens),
        },
        "transaction": {"message": {"accountKeys": keys}},
    }


def _key(pubkey, signer=False):
    return {"pubkey": pubkey, "signer": signer, "writable": True}


def _bal(index, owner, amount):
    return {"accountIndex": index, "mint": MINT, "owner": owner, "uiTokenAmount": {"amount": str(amount)}}


def test_incoming_sol_is_read_with_its_sender():
    tx = _tx([_key(SENDER, True), _key(WALLET)], [2_000_000_000, 0], [1_499_995_000, 500_000_000])
    assert solana_parse.parse_deposits(tx, WALLET, set()) == [solana_parse.Deposit("native", 500_000_000, SENDER)]


def test_incoming_token_is_read_only_for_tracked_mints():
    tx = _tx(
        [_key(SENDER, True), _key("SenderTokenAccount"), _key("WalletTokenAccount"), _key(MINT)],
        [5_000_000, 2_000_000, 2_000_000, 1], [4_995_000, 2_000_000, 2_000_000, 1],
        pre_tokens=[_bal(1, SENDER, 1000), _bal(2, WALLET, 0)],
        post_tokens=[_bal(1, SENDER, 600), _bal(2, WALLET, 400)],
    )
    assert solana_parse.parse_deposits(tx, WALLET, {MINT}) == [solana_parse.Deposit(MINT, 400, SENDER)]
    assert solana_parse.parse_deposits(tx, WALLET, {"someOtherMint"}) == []  # a token the user hasn't added: ignored


def test_own_outgoing_failed_and_empty_transactions_are_ignored():
    own = _tx([_key(WALLET, True), _key(SENDER)], [2_000_000_000, 0], [1_000_000_000, 999_995_000])
    assert solana_parse.parse_deposits(own, WALLET, set()) == []
    failed = _tx([_key(SENDER, True), _key(WALLET)], [2, 0], [1, 1], err={"InstructionError": [0, "x"]})
    assert solana_parse.parse_deposits(failed, WALLET, set()) == []
    unrelated = _tx([_key(SENDER, True), _key("Other")], [2, 0], [1, 1])
    assert solana_parse.parse_deposits(unrelated, WALLET, set()) == []


# --------------------------------------------------------------------- sending
class FakeRpc:
    def __init__(self, *, sol=10_000_000_000, accounts=None, existing=(), statuses=None, height=100,
                 send_error=None, rent=2_039_280):
        self.sol, self.accounts, self.existing = sol, accounts or {}, set(existing)
        self.statuses = list(statuses or [{"confirmationStatus": "confirmed", "err": None}])
        self.height, self.send_error, self.rent = height, send_error, rent
        self.sent = []

    async def get_balance(self, address):
        return self.sol

    async def token_accounts(self, owner, mint):
        return self.accounts.get(mint, [])

    async def get_account(self, address):
        return {"owner": "x", "lamports": 1, "data": b""} if address in self.existing else None

    async def min_balance_for_rent(self, size):
        return self.rent

    async def latest_blockhash(self):
        return os.urandom(32), 150

    async def send_transaction(self, tx):
        if self.send_error:
            raise self.send_error
        self.sent.append(tx)
        return k.first_signature(tx)

    async def get_signature_status(self, signature):
        return self.statuses.pop(0) if len(self.statuses) > 1 else (self.statuses[0] if self.statuses else None)

    async def block_height(self):
        return self.height


def _parties():
    return k.generate(), k.generate()


async def test_sol_send_is_signed_broadcast_and_confirmed():
    sender, recipient = _parties()
    rpc = FakeRpc()
    out = await solana_send.perform_send(
        rpc=rpc, private_key=sender.seed, from_addr=sender.address, to_addr=recipient.address,
        mint=None, decimals=9, raw=1_000_000_000, interval=0.001,
    )
    assert out.status == "confirmed" and out.tx_hash == k.first_signature(rpc.sent[0])
    tx = read_transaction(rpc.sent[0])
    assert tx["instructions"][0][2] == struct.pack("<IQ", 2, 1_000_000_000)
    Ed25519PublicKey.from_public_bytes(sender.public_key).verify(tx["signature"], tx["message"])


async def test_token_send_opens_the_recipients_account_when_missing_and_charges_for_it():
    sender, recipient = _parties()
    mint = k.generate().address
    source = k.generate().address
    rpc = FakeRpc(accounts={mint: [(source, 100), (k.generate().address, 5)]})
    plan = await solana_send.plan_transfer(rpc, sender.address, recipient.address, mint, 6, 50)
    assert plan.creates_account and len(plan.instructions) == 2
    assert plan.fee_lamports == k.LAMPORTS_PER_SIGNATURE + rpc.rent
    out = await solana_send.perform_send(
        rpc=rpc, private_key=sender.seed, from_addr=sender.address, to_addr=recipient.address,
        mint=mint, decimals=6, raw=50, interval=0.001,
    )
    assert out.status == "confirmed"
    tx = read_transaction(rpc.sent[0])
    assert [i[0] for i in tx["instructions"]] == [k.ASSOCIATED_TOKEN_PROGRAM, k.TOKEN_PROGRAM]
    assert tx["instructions"][1][1][0] == k.b58decode(source)  # sent from the account that holds enough


async def test_token_send_skips_account_creation_when_it_exists():
    sender, recipient = _parties()
    mint = k.generate().address
    ata = k.b58encode(k.associated_token_address(recipient.public_key, k.b58decode(mint)))
    rpc = FakeRpc(accounts={mint: [(k.generate().address, 100)]}, existing={ata})
    plan = await solana_send.plan_transfer(rpc, sender.address, recipient.address, mint, 6, 50)
    assert not plan.creates_account and len(plan.instructions) == 1 and plan.fee_lamports == k.LAMPORTS_PER_SIGNATURE


async def test_token_send_refuses_when_no_single_account_has_enough_or_none_exists():
    sender, recipient = _parties()
    mint = k.generate().address
    with pytest.raises(SendError):
        await solana_send.plan_transfer(FakeRpc(), sender.address, recipient.address, mint, 6, 1)
    split = FakeRpc(accounts={mint: [(k.generate().address, 30), (k.generate().address, 30)]})
    with pytest.raises(SendError):
        await solana_send.plan_transfer(split, sender.address, recipient.address, mint, 6, 50)


async def test_not_enough_sol_sends_nothing():
    sender, recipient = _parties()
    rpc = FakeRpc(sol=1_000)
    with pytest.raises(SendError):
        await solana_send.perform_send(
            rpc=rpc, private_key=sender.seed, from_addr=sender.address, to_addr=recipient.address,
            mint=None, decimals=9, raw=500, interval=0.001,
        )
    assert rpc.sent == []


async def test_node_refusal_becomes_a_clear_message():
    sender, recipient = _parties()
    rpc = FakeRpc(send_error=SolRpcError("Transaction simulation failed: insufficient funds for rent"))
    with pytest.raises(SendError, match="minimum balance"):
        await solana_send.perform_send(
            rpc=rpc, private_key=sender.seed, from_addr=sender.address, to_addr=recipient.address,
            mint=None, decimals=9, raw=1_000_000_000, interval=0.001,
        )


async def test_unclear_broadcast_is_resolved_by_looking_up_the_transaction():
    sender, recipient = _parties()
    rpc = FakeRpc(send_error=TimeoutError("no answer"))  # may or may not have gone out
    out = await solana_send.perform_send(
        rpc=rpc, private_key=sender.seed, from_addr=sender.address, to_addr=recipient.address,
        mint=None, decimals=9, raw=1_000_000_000, interval=0.001,
    )
    assert out.status == "confirmed"  # it had gone out: the status lookup finds it by its signature


async def test_on_chain_failure_and_never_landing():
    sender, recipient = _parties()
    common = dict(private_key=sender.seed, from_addr=sender.address, to_addr=recipient.address,
                  mint=None, decimals=9, raw=1_000_000_000, timeout=0.01, interval=0.001)
    failed = await solana_send.perform_send(rpc=FakeRpc(statuses=[{"err": {"x": 1}, "confirmationStatus": "confirmed"}]), **common)
    assert failed.status == "failed"
    expired = await solana_send.perform_send(rpc=FakeRpc(statuses=[None], height=999), **common)
    assert expired.status == "failed" and "nothing was sent" in expired.detail  # past its blockhash: can never land
    waiting = await solana_send.perform_send(rpc=FakeRpc(statuses=[None], height=100), **common)
    assert waiting.status == "unknown"  # might still land: the user is told not to send again


# --------------------------------------------------------------- database parts
@pytest.fixture
async def world():
    from bot import crypto, service
    from bot.db import init_db, make_session_factory

    engine, factory = make_session_factory("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    master = crypto.generate_master_key()
    async with factory() as s:
        await service.seed_defaults(s)
        alice = await service.get_or_create_user(s, 1, "alice")
        bob = await service.get_or_create_user(s, 2, "bob")
        a_wallets = await service.ensure_wallets(s, alice, master)
        b_wallets = await service.ensure_wallets(s, bob, master)
        await s.commit()
    pick = lambda ws: next(w for w in ws if w.key_type == "solana")  # noqa: E731
    yield factory, master, alice, bob, pick(a_wallets), pick(b_wallets)
    await engine.dispose()


async def test_every_user_gets_a_solana_wallet_that_can_be_exported_and_imported(world):
    from bot import service

    factory, master, alice, _, a_wallet, _ = world
    assert k.is_valid_address(a_wallet.address)
    async with factory() as s:
        secret = await service.export_wallet(s, alice, master, "solana")
        assert k.parse_import(secret).address == a_wallet.address
        other = k.generate()
        imported = await service.import_wallet(s, alice, master, "solana", k.export_text(other))
        assert imported.address == other.address and imported.imported
        await s.commit()


class WatchRpc:
    def __init__(self, signatures, transactions):
        self.signatures, self.transactions, self.asked_until = signatures, transactions, []

    async def signatures_since(self, address, until, **kw):
        self.asked_until.append(until)
        return self.signatures

    async def get_transaction(self, signature):
        return self.transactions.get(signature)


async def test_watcher_records_a_sol_deposit_once_and_notifies(world):
    from sqlalchemy import select

    from bot import solana_watcher
    from bot.db import Ledger, SolCursor

    factory, _, alice, bob, a_wallet, b_wallet = world
    tx = _tx([_key(b_wallet.address, True), _key(a_wallet.address)], [3_000_000_000, 0], [2_499_995_000, 500_000_000])
    rpc = WatchRpc([{"signature": "S" * 88, "err": None}], {"S" * 88: tx})
    ops = SimpleNamespace(rpc=rpc, slug="solana")
    told = []

    async def notify(user, token, transfer, chain_name):
        told.append((user.username, token.symbol, transfer.amount, chain_name))

    await solana_watcher.scan_wallet(factory, ops, notify, a_wallet.id)
    assert told == [("alice", "SOL", 500_000_000, "Solana")]
    async with factory() as s:
        row = (await s.execute(select(Ledger).where(Ledger.user_id == alice.id))).scalar_one()
        assert row.kind == "transfer_in" and row.counterparty_user_id == bob.id  # the sender is a bot user
        assert row.tx_hash == "S" * 88 and int(row.amount) == 500_000_000
        cursor = (await s.execute(select(SolCursor))).scalar_one()
        assert cursor.last_signature == "S" * 88
    rpc.signatures = []  # nothing new
    await solana_watcher.scan_wallet(factory, ops, notify, a_wallet.id)
    assert len(told) == 1 and rpc.asked_until[-1] == "S" * 88


async def test_watcher_leaves_the_cursor_alone_when_a_transaction_cant_be_read_yet(world):
    from sqlalchemy import select

    from bot import solana_watcher
    from bot.db import SolCursor

    factory, _, _, _, a_wallet, _ = world
    rpc = WatchRpc([{"signature": "T" * 88, "err": None}], {})  # the node doesn't have it yet
    ops = SimpleNamespace(rpc=rpc, slug="solana")
    with pytest.raises(RuntimeError):
        await solana_watcher.scan_wallet(factory, ops, lambda *a: None, a_wallet.id)
    async with factory() as s:
        cursor = (await s.execute(select(SolCursor))).scalar_one_or_none()
        assert cursor is None or cursor.last_signature is None  # it will be retried, nothing is skipped
