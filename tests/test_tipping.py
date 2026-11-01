"""Tests for group tipping: parsing, amounts, pending confirmations, multi-recipient sends."""
from decimal import Decimal

import pytest
from sqlalchemy import select

from bot import crypto, neo_send, service, tipping
from bot.db import Ledger, Token, init_db, make_session_factory

MASTER = crypto.generate_master_key()
GAS_HASH = neo_send.GAS_HASH


# ------------------------------------------------------------------ parsing
def test_parse_multiple_recipients_each():
    req = tipping.parse_tip("@alice @bob 5 USDT")
    assert req.handles == ["@alice", "@bob"] and req.amount_text == "5"
    assert req.symbol == "USDT" and req.network is None and req.split is False


def test_parse_split_network_and_duplicates():
    req = tipping.parse_tip("@alice @ALICE 10 usdt bsc split")
    assert req.handles == ["@alice"] and req.split and req.network == "bsc"


def test_parse_reply_form_has_no_handles():
    assert tipping.parse_tip("5 GAS").handles == []


@pytest.mark.parametrize("text", ["", "@alice", "@alice 5", "@al 5 GAS", "5 US-DT", "@alice 5 USDT bsc extra"])
def test_parse_rejects_bad_input(text):
    with pytest.raises(tipping.TipError):
        tipping.parse_tip(text)


# ------------------------------------------------------------------ amounts
def test_amounts_each_and_split():
    each = tipping.parse_tip("@a @b @c 3 GAS")
    assert tipping.amounts(each, 8, 3) == (300000000, 900000000)
    split = tipping.parse_tip("@a @b @c 1 GAS split")
    e, total = tipping.amounts(split, 8, 3)
    assert e == 33333333 and total == 99999999  # remainder stays with the sender


def test_amounts_limits():
    req = tipping.parse_tip("@a 1 GAS")
    with pytest.raises(tipping.TipError):
        tipping.amounts(req, 8, 0)
    with pytest.raises(tipping.TipError):
        tipping.amounts(req, 8, tipping.MAX_RECIPIENTS + 1)
    with pytest.raises(tipping.TipError):
        tipping.amounts(tipping.parse_tip("@a @b 0.00000001 GAS split"), 8, 2)
    with pytest.raises(tipping.TipError):
        tipping.amounts(tipping.parse_tip("@a 1e3 GAS"), 8, 1)


class T:  # tiny token stand-in
    def __init__(self, symbol):
        self.symbol = symbol


def test_pick_token_by_symbol_and_network():
    usdt_bsc, usdt_x, gas = T("USDT"), T("USDT"), T("GAS")
    cands = [(usdt_bsc, "bsc", "BNB Smart Chain"), (usdt_x, "neo_x", "Neo X"), (gas, "neo_n3", "Neo N3")]
    assert tipping.pick_token(cands, "gas", None)[0] is gas
    assert tipping.pick_token(cands, "USDT", "bsc")[0] is usdt_bsc
    assert tipping.pick_token(cands, "USDT", "neox")[0] is usdt_x
    with pytest.raises(tipping.TipError):
        tipping.pick_token(cands, "USDT", None)  # ambiguous
    with pytest.raises(tipping.TipError):
        tipping.pick_token(cands, "DOGE", None)


# ------------------------------------------------------------------ pending store
def test_pending_is_single_use_and_expires():
    store = tipping.PendingStore()
    item = tipping.PendingTip(1, 2, "neo_n3", 3, 100, [])
    nonce = store.add(item)
    assert store.peek(nonce) is item          # peeking does not consume it
    assert store.take(nonce) is item
    assert store.take(nonce) is None           # second tap finds nothing
    old = tipping.PendingTip(1, 2, "neo_n3", 3, 100, [], expires=0)
    assert store.peek(store.add(old)) is None


# ------------------------------------------------------------------ execution
class FakeOps:
    slug = "neo_n3"

    def __init__(self, outcomes=None, fail_check_on=None):
        self.sent = []
        self.outcomes = list(outcomes or [])
        self.fail_check_on = fail_check_on  # address whose check_send raises

    async def balances(self, address, tokens):
        return {}

    async def check_send(self, token, address, to, raw, balances):
        if to == self.fail_check_on:
            raise neo_send.SendError("Not enough GAS for the fee.")

    async def send(self, private_key, token, from_address, to, raw):
        self.sent.append((to, raw))
        status = self.outcomes.pop(0) if self.outcomes else "confirmed"
        return neo_send.SendOutcome(status, f"0xtx{len(self.sent)}", "")


@pytest.fixture
async def env():
    engine, factory = make_session_factory("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    async with factory() as s:
        await service.seed_defaults(s)
        user = await service.get_or_create_user(s, 1, "alice")
        wallets = await service.ensure_wallets(s, user, MASTER)
        await s.commit()
        wallet = next(w for w in wallets if w.key_type == "neo_n3")
        gas = await s.scalar(select(Token).where(Token.symbol == "GAS", Token.contract == GAS_HASH))
        yield s, user, wallet, gas
    await engine.dispose()


RECIPIENTS = [(10, "@bob", "NB"), (11, "@carol", "NC"), (12, "@dave", "ND")]


async def test_tip_sends_to_everyone_and_records_each(env):
    s, user, wallet, gas = env
    ops = FakeOps()
    res = await tipping.run_tip(s, ops, user, gas, wallet, b"k", [gas], RECIPIENTS, 100, {})
    assert [r.status for r in res] == ["confirmed"] * 3
    assert [x[0] for x in ops.sent] == ["NB", "NC", "ND"] and all(x[1] == 100 for x in ops.sent)
    rows = (await s.execute(select(Ledger))).scalars().all()
    assert len(rows) == 3 and all(r.kind == "transfer_out" and r.status == "confirmed" for r in rows)
    assert sorted(r.counterparty_user_id for r in rows) == [10, 11, 12]


async def test_unconfirmed_tip_stops_the_rest(env):
    s, user, wallet, gas = env
    ops = FakeOps(outcomes=["confirmed", "unknown"])
    res = await tipping.run_tip(s, ops, user, gas, wallet, b"k", [gas], RECIPIENTS, 100, {})
    assert [r.status for r in res] == ["confirmed", "unknown", "skipped"]
    assert len(ops.sent) == 2  # never sent to the third


async def test_failed_check_stops_and_sends_nothing_more(env):
    s, user, wallet, gas = env
    ops = FakeOps(fail_check_on="NC")
    res = await tipping.run_tip(s, ops, user, gas, wallet, b"k", [gas], RECIPIENTS, 100, {})
    assert [r.status for r in res] == ["confirmed", "failed", "skipped"]
    assert "fee" in res[1].detail
    assert len(ops.sent) == 1


async def test_daily_limit_applies_across_the_batch(env):
    s, user, wallet, gas = env
    # 1 GAS per tx and 2 GAS per day: the third tip must be refused.
    limits = {"GAS": (Decimal(1), Decimal(2))}
    ops = FakeOps()
    res = await tipping.run_tip(s, ops, user, gas, wallet, b"k", [gas], RECIPIENTS, 100_000_000, limits)
    assert [r.status for r in res] == ["confirmed", "confirmed", "failed"]
    assert len(ops.sent) == 2
