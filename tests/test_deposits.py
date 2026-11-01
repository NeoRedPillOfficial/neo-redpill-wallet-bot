"""Tests for the Neo N3 read side, using a fake RPC (no network needed)."""
import base64

import pytest
from sqlalchemy import select

from bot import chains, crypto, neo_rpc, service, watcher
from bot.db import Ledger, Token, init_db, make_session_factory
from bot.neo_rpc import Transfer

MASTER = crypto.generate_master_key()
GAS = "0xd2a4cff31913016155e38e474a2c06d08be276cf"
NRP = "0x789518aa302b571e3e825f2c85a01ad731014a45"


# ------------------------------------------------------------------ RPC parsers
def test_parse_balances_normalizes_hashes():
    result = {"balance": [{"assethash": GAS.upper().replace("0X", "0x"), "amount": "250000000"}]}
    assert neo_rpc.parse_balances(result) == {GAS: 250000000}


def test_parse_received_maps_fields():
    result = {
        "received": [
            {
                "timestamp": 1700000000000,
                "assethash": GAS,
                "transferaddress": "NSender",
                "amount": "100000000",
                "blockindex": 5,
                "transfernotifyindex": 2,
                "txhash": "ABCDEF",
            }
        ]
    }
    (t,) = neo_rpc.parse_received(result)
    assert (t.tx_hash, t.asset, t.amount, t.from_address, t.notify_index) == (
        "0xabcdef", GAS, 100000000, "NSender", 2,
    )


def test_tx_outcome_void_call_is_a_success_only_when_not_expecting_true():
    log = {"executions": [{"vmstate": "HALT", "stack": [{"type": "Any"}]}]}  # what a void call leaves behind
    assert neo_rpc.parse_tx_outcome(log, expect_true=False) == (True, "")
    assert neo_rpc.parse_tx_outcome(log)[0] is False  # a token transfer must return true
    fault = {"executions": [{"vmstate": "FAULT", "exception": "boom", "stack": []}]}
    assert neo_rpc.parse_tx_outcome(fault, expect_true=False) == (False, "boom")


def test_parse_token_info():
    sym = {"state": "HALT", "stack": [{"type": "ByteString", "value": base64.b64encode(b"NRP").decode()}]}
    dec = {"state": "HALT", "stack": [{"type": "Integer", "value": "8"}]}
    assert neo_rpc.parse_symbol(sym) == "NRP"
    assert neo_rpc.parse_decimals(dec) == 8


def test_parse_token_info_rejects_faults_and_nonsense():
    with pytest.raises(neo_rpc.RpcError):
        neo_rpc.parse_decimals({"state": "FAULT", "stack": [], "exception": "boom"})
    with pytest.raises(neo_rpc.RpcError):
        neo_rpc.parse_decimals({"state": "HALT", "stack": [{"type": "Integer", "value": "999"}]})


# ------------------------------------------------------------------ fake node
class FakeRpc:
    def __init__(self, received=None, balances=None, info=None, fail=False, outcomes=None):
        self.received = received or []
        self.balances = balances or {}
        self.info = info or {}
        self.fail = fail
        self.outcomes = list(outcomes or [])  # scripted get_tx_outcome answers
        self.queries = []

    async def get_tx_outcome(self, tx_hash):
        return self.outcomes.pop(0) if self.outcomes else None

    async def get_received(self, address, start_ms, end_ms):
        self.queries.append((address, start_ms, end_ms))
        if self.fail:
            raise neo_rpc.RpcError("node down")
        return [t for t in self.received if start_ms <= t.timestamp_ms <= end_ms]

    async def get_balances(self, address):
        if self.fail:
            raise neo_rpc.RpcError("node down")
        return self.balances

    async def get_token_info(self, contract):
        if contract not in self.info:
            raise neo_rpc.RpcError("unreadable")
        return self.info[contract]


@pytest.fixture
async def env():
    engine, factory = make_session_factory("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    async with factory() as s:
        await service.seed_defaults(s)
        user = await service.get_or_create_user(s, 1, "alice")
        wallets = await service.ensure_wallets(s, user, MASTER)
        await s.commit()
        neo_wallet = next(x for x in wallets if x.key_type == "neo_n3")
        tokens = (await s.execute(select(Token))).scalars().all()
        by_hash = {neo_rpc.normalize_hash(t.contract): t for t in tokens if t.chain_id == 1}
        yield s, user, neo_wallet, by_hash
    await engine.dispose()


def deposit(tx="0xaa", asset=GAS, amount=100000000, ts=None, sender="NSender", idx=0):
    return Transfer(tx, asset, amount, sender, ts or service.now_ms(), idx)


# ------------------------------------------------------------------ the watcher
async def test_deposit_recorded_once_and_reported_once(env):
    s, user, wallet, by_hash = env
    rpc = FakeRpc(received=[deposit()])
    first = await watcher.scan_wallet(s, rpc, wallet, user, by_hash)
    second = await watcher.scan_wallet(s, rpc, wallet, user, by_hash)  # overlap re-reads it
    assert len(first) == 1 and second == []
    rows = (await s.execute(select(Ledger))).scalars().all()
    assert len(rows) == 1 and rows[0].kind == "deposit" and int(rows[0].amount) == 100000000


async def test_two_transfers_in_one_tx_are_both_kept(env):
    s, user, wallet, by_hash = env
    rpc = FakeRpc(received=[deposit(idx=0), deposit(idx=1)])
    assert len(await watcher.scan_wallet(s, rpc, wallet, user, by_hash)) == 2


async def test_history_before_wallet_creation_is_ignored(env):
    s, user, wallet, by_hash = env
    old = deposit(ts=wallet.watch_from_ms - 1000)
    rpc = FakeRpc(received=[old])
    assert await watcher.scan_wallet(s, rpc, wallet, user, by_hash) == []


async def test_unverified_and_unknown_tokens_are_not_credited(env):
    s, user, wallet, by_hash = env
    assert by_hash[NRP].decimals_verified is False
    rpc = FakeRpc(received=[deposit(asset=NRP), deposit(tx="0xbb", asset="0x" + "11" * 20)])
    assert await watcher.scan_wallet(s, rpc, wallet, user, by_hash) == []
    assert (await s.execute(select(Ledger))).scalars().all() == []


async def test_self_transfer_ignored(env):
    s, user, wallet, by_hash = env
    rpc = FakeRpc(received=[deposit(sender=wallet.address)])
    assert await watcher.scan_wallet(s, rpc, wallet, user, by_hash) == []


async def test_node_failure_does_not_advance_cursor(env):
    s, user, wallet, by_hash = env
    before = wallet.last_scan_ms
    with pytest.raises(neo_rpc.RpcError):
        await watcher.scan_wallet(s, FakeRpc(fail=True), wallet, user, by_hash)
    assert wallet.last_scan_ms == before


async def test_cursor_advances_and_window_overlaps(env):
    s, user, wallet, by_hash = env
    rpc = FakeRpc()
    later = wallet.watch_from_ms + 10 * 60 * 1000
    await watcher.scan_wallet(s, rpc, wallet, user, by_hash, now=later)
    await watcher.scan_wallet(s, rpc, wallet, user, by_hash, now=later + 15000)
    _, start, _ = rpc.queries[-1]
    assert start == later - watcher.OVERLAP_MS  # re-reads the last 5 minutes


# --------------------------------------------------- decimals sync + home view
async def test_sync_verifies_and_corrects_decimals(env):
    s, user, wallet, by_hash = env
    rpc = FakeRpc(
        info={
            service.DEFAULT_TOKENS[2][1]: ("NRP", 6),  # NRP really has 6 decimals
            # FRANK unreadable -> stays unverified
        }
    )
    outcomes = await service.sync_token_decimals(s, rpc)
    assert outcomes["NRP"].startswith("verified")
    assert outcomes["FRANK"] == "unreadable"
    nrp = by_hash[NRP]
    assert nrp.decimals == 6 and nrp.decimals_verified is True


async def test_home_view_live_balances_and_honest_gaps(env):
    s, user, wallet, by_hash = env
    ops = chains.N3Ops(FakeRpc(balances={GAS: 150000000}), sender=None)
    rows = {t.symbol: amount for t, amount in await service.chain_view(s, user, "neo_n3", ops)}
    assert rows["GAS"] == 150000000
    assert rows["NEO"] == 0
    assert rows["NRP"] is None and rows["FRANK"] is None  # unverified decimals

    down_ops = chains.N3Ops(FakeRpc(fail=True), sender=None)
    down = {t.symbol: amount for t, amount in await service.chain_view(s, user, "neo_n3", down_ops)}
    assert all(amount is None for amount in down.values())


async def test_tokens_the_user_has_not_added_are_ignored(env):
    s, user, wallet, by_hash = env
    rpc = FakeRpc(received=[deposit()])
    assert await watcher.scan_wallet(s, rpc, wallet, user, by_hash, allowed=set()) == []
    assert len(await watcher.scan_wallet(s, rpc, wallet, user, by_hash, allowed={by_hash[GAS].id})) == 1


# ------------------------------------------------ sends: ledger and recipients
async def test_transfer_between_bot_users_is_labelled_transfer_in(env):
    s, user, wallet, by_hash = env
    bob = await service.get_or_create_user(s, 2, "bob")
    bob_wallets = await service.ensure_wallets(s, bob, MASTER)
    await s.commit()
    bob_neo = next(x for x in bob_wallets if x.key_type == "neo_n3")
    rpc = FakeRpc(received=[deposit(sender=bob_neo.address)])
    assert len(await watcher.scan_wallet(s, rpc, wallet, user, by_hash)) == 1
    row = (await s.execute(select(Ledger))).scalars().one()
    assert row.kind == "transfer_in" and row.counterparty_user_id == bob.id


async def test_daily_usage_counts_pending_and_confirmed_but_not_failed(env):
    s, user, wallet, by_hash = env
    gas = by_hash[GAS]
    await service.record_outgoing(s, user, gas, "withdraw", 300, "to Nxxx")
    failed = await service.record_outgoing(s, user, gas, "transfer_out", 200, "to Nyyy")
    failed.status = "failed"
    await s.commit()
    assert await service.sent_last_24h(s, user, gas) == 300


async def test_find_recipient_case_insensitive_and_missing(env):
    s, user, wallet, by_hash = env
    found = await service.find_recipient(s, "@ALICE")
    assert found is not None and found[0].id == user.id and found[1].key_type == "neo_n3"
    assert await service.find_recipient(s, "@nobody") is None
    assert await service.find_recipient(s, "@") is None
