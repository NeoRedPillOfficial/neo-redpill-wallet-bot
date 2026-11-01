"""Tests for transaction history: the text, the queries, and settling unconfirmed transactions."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from bot import crypto, helptext, historyview, reconcile, service
from bot.db import Ledger, init_db, make_session_factory
from sqlalchemy import select

NOW = datetime(2026, 10, 19, 4, 30, tzinfo=timezone.utc)
HASH = "0x" + "ab" * 32
MASTER = crypto.generate_master_key()


def entry(**kw):
    base = dict(
        kind="deposit", status="confirmed", amount_text="+12.5", symbol="GAS", chain_name="Neo N3",
        tx_hash=HASH, note=None, created_at=NOW - timedelta(minutes=3),
    )
    base.update(kw)
    return historyview.Entry(**base)


# ------------------------------------------------------------------- the text
def test_explorer_links_only_for_known_networks_and_real_hashes():
    assert historyview.explorer_url("mainnet", "eth", HASH) == f"https://etherscan.io/tx/{HASH}"
    assert historyview.explorer_url("mainnet", "neo_n3", HASH).startswith("https://dora.coz.io/")
    assert historyview.explorer_url("testnet", "polygon", HASH) is None  # no testnet explorer listed
    assert historyview.explorer_url("mainnet", "someclone", HASH) is None
    assert historyview.explorer_url("mainnet", "eth", None) is None
    assert historyview.explorer_url("mainnet", "eth", 'x"><script>') is None  # never build a link from junk


def test_every_kind_and_status_is_labelled():
    text = historyview.render_history(
        [
            entry(kind="deposit"),
            entry(kind="transfer_in", counterparty="@alice"),
            entry(kind="withdraw", amount_text="-1", note="to NabcdefghijklmnopqrstuvwxyZ12345678 on neo_n3"),
            entry(kind="transfer_out", amount_text="-1", counterparty="@bob"),
            entry(kind="transfer_out", amount_text="-1", note="tip to 0x1234567890abcdef1234567890abcdef12345678 on bsc"),
            entry(kind="bridge_out", amount_text="-0.165", status="unknown"),
            entry(kind="withdraw", amount_text="-2", status="failed"),
            entry(status="pending"),
        ],
        pending=False, page=0, pages=1, total=8, now=NOW,
    )
    for label in ("⬇️ Deposit", "⬇️ Received", "⬆️ Withdraw", "↗️ Sent", "🎁 Tip sent", "🌉 Bridge out"):
        assert label in text
    for status in ("✅ Confirmed", "❌ Failed", "⏳ Pending", "⚠️ Sent, not confirmed yet"):
        assert status in text
    assert "from @alice" in text and "to @bob" in text
    assert "to <code>0x1234…5678</code>" in text  # long addresses are shortened
    assert "Page 1 of 1 · 8 in total · times are UTC" in text


def test_entry_lines_show_amount_network_time_and_link():
    text = historyview.render_history(
        [entry(url="https://etherscan.io/tx/x")], pending=False, page=0, pages=1, total=1, now=NOW
    )
    assert "<b>+12.5 GAS</b>" in text and "Neo N3" in text and "19 Oct 04:27" in text
    assert '<a href="https://etherscan.io/tx/x">view tx</a>' in text


def test_naive_database_times_are_treated_as_utc():
    naive = NOW.replace(tzinfo=None) - timedelta(hours=1)
    text = historyview.render_history([entry(created_at=naive)], pending=False, page=0, pages=1, total=1, now=NOW)
    assert "19 Oct 03:30" in text


def test_pending_view_states():
    assert "Nothing is pending" in historyview.render_history([], pending=True, page=0, pages=1, total=0, now=NOW)
    assert "No transactions yet" in historyview.render_history([], pending=False, page=0, pages=1, total=0, now=NOW)
    transit = entry(kind="bridge_out", in_transit=True)
    assert "On its way" in historyview.render_history([transit], pending=True, page=0, pages=1, total=1, now=NOW)
    # a "pending" row with no transaction that is old: tell the user not to assume anything
    stale = entry(status="pending", tx_hash=None, created_at=NOW - timedelta(hours=1))
    assert "check your balance" in historyview.render_history([stale], pending=True, page=0, pages=1, total=1, now=NOW)
    fresh = entry(status="pending", tx_hash=None, created_at=NOW - timedelta(minutes=1))
    assert "check your balance" not in historyview.render_history([fresh], pending=True, page=0, pages=1, total=1, now=NOW)


def test_text_is_escaped_and_stays_under_the_limit():
    nasty = entry(symbol="<b>X", chain_name="A&B", counterparty="@a<b>")
    assert "&lt;b&gt;X" in historyview.render_history([nasty], pending=False, page=0, pages=1, total=1, now=NOW)
    many = [entry(url=f"https://etherscan.io/tx/{HASH}", amount_text=f"+{i}") for i in range(60)]
    text = historyview.render_history(many, pending=True, page=0, pages=1, total=60, now=NOW)
    assert len(text) <= 4096 and "more." in text


def test_help_mentions_history():
    assert "/history" in helptext.main_text([("Neo N3", "GAS")], True)
    assert ("history", "📜 History") in helptext.topic_keys(False)
    assert "Pending" in helptext.topic_text("history", [("Neo N3", "GAS")], False)


# ------------------------------------------------- checking a transaction on-chain
class FakeN3Rpc:
    def __init__(self, outcome):
        self.outcome, self.asked = outcome, []

    async def get_tx_outcome(self, tx_hash, expect_true=True):
        self.asked.append((tx_hash, expect_true))
        return self.outcome


class FakeEvmRpc:
    def __init__(self, receipt):
        self.receipt = receipt

    async def get_receipt(self, tx_hash):
        return self.receipt


async def test_check_status_neo_n3():
    rpc = FakeN3Rpc((True, ""))
    assert await reconcile.check_status(SimpleNamespace(rpc=rpc), "transfer_out", HASH) == "confirmed"
    assert await reconcile.check_status(SimpleNamespace(rpc=rpc), "bridge_out", HASH) == "confirmed"
    assert rpc.asked == [(HASH, True), (HASH, False)]  # a bridge deposit returns nothing, so don't expect true
    assert await reconcile.check_status(SimpleNamespace(rpc=FakeN3Rpc((False, "x"))), "withdraw", HASH) == "failed"
    assert await reconcile.check_status(SimpleNamespace(rpc=FakeN3Rpc(None)), "withdraw", HASH) is None


async def test_check_status_evm():
    for receipt, want in (({"status": "0x1"}, "confirmed"), ({"status": "0x0"}, "failed"), (None, None)):
        assert await reconcile.check_status(SimpleNamespace(rpc=FakeEvmRpc(receipt)), "withdraw", HASH) == want


# ----------------------------------------------------------------- the database
@pytest.fixture
async def world():
    engine, factory = make_session_factory("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    async with factory() as s:
        await service.seed_defaults(s)
        alice = await service.get_or_create_user(s, 1, "alice")
        bob = await service.get_or_create_user(s, 2, "bob")
        await s.commit()
    yield factory, alice, bob
    await engine.dispose()


async def _token(s, slug, symbol):
    from bot.db import Token

    chain = await service.get_chain(s, slug)
    return await s.scalar(select(Token).where(Token.chain_id == chain.id, Token.symbol == symbol))


def _row(user, token, kind, amount, status, minutes_ago, tx="a", **kw):
    return Ledger(
        user_id=user.id, token_id=token.id, kind=kind, amount=amount, status=status,
        tx_hash=None if tx is None else "0x" + tx * 64,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago), **kw,
    )


async def test_history_is_newest_first_paged_and_per_user(world):
    factory, alice, bob = world
    async with factory() as s:
        gas = await _token(s, "neo_n3", "GAS")
        for i in range(8):
            s.add(_row(alice, gas, "deposit", 100 + i, "confirmed", minutes_ago=100 - i, tx=f"{i}"))
        s.add(_row(bob, gas, "deposit", 5, "confirmed", 1, tx="9"))
        s.add(_row(alice, gas, "withdraw", -7, "unknown", 0.5, tx="f"))
        await s.commit()
        assert await service.history_count(s, alice, False) == 9
        assert await service.history_count(s, bob, False) == 1
        first = await service.history_page(s, alice, False, 0, 6)
        assert [int(r.amount) for r, _, _ in first][0] == -7  # newest first
        assert len(first) == 6 and all(c.slug == "neo_n3" and t.symbol == "GAS" for _, t, c in first)
        rest = await service.history_page(s, alice, False, 6, 6)
        assert len(rest) == 3
        assert {r.id for r, _, _ in first}.isdisjoint({r.id for r, _, _ in rest})
        assert all(r.user_id == alice.id for r, _, _ in first + rest)


async def test_pending_filter_counts_pending_and_unknown_only(world):
    factory, alice, _ = world
    async with factory() as s:
        gas = await _token(s, "neo_n3", "GAS")
        s.add_all([
            _row(alice, gas, "withdraw", -1, "pending", 1, tx=None),
            _row(alice, gas, "withdraw", -2, "unknown", 2, tx="b"),
            _row(alice, gas, "withdraw", -3, "confirmed", 3, tx="c"),
            _row(alice, gas, "withdraw", -4, "failed", 4, tx="d"),
        ])
        await s.commit()
        assert await service.history_count(s, alice, True) == 2
        rows = await service.history_page(s, alice, True, 0, 10)
        assert sorted(int(r.amount) for r, _, _ in rows) == [-2, -1]


async def test_bridge_in_transit_until_it_lands_somewhere_else(world):
    factory, alice, _ = world
    async with factory() as s:
        gas = await _token(s, "neo_n3", "GAS")
        x_gas = await _token(s, "neo_x", "GAS")
        s.add(_row(alice, gas, "bridge_out", -16_500_000, "confirmed", 5, tx="a"))
        s.add(_row(alice, gas, "bridge_out", -1, "confirmed", 60, tx="b"))  # too old to be "in transit"
        await s.commit()
        assert [int(r.amount) for r, _, _ in await service.bridges_in_transit(s, alice)] == [-16_500_000]
        # an unrelated deposit on the SAME network doesn't count as arrival...
        s.add(_row(alice, gas, "deposit", 5, "confirmed", 2, tx="c"))
        await s.commit()
        assert len(await service.bridges_in_transit(s, alice)) == 1
        # ...a deposit on the other network after the bridge does
        s.add(_row(alice, x_gas, "deposit", 5, "confirmed", 1, tx="d"))
        await s.commit()
        assert await service.bridges_in_transit(s, alice) == []


async def test_usernames(world):
    factory, alice, bob = world
    async with factory() as s:
        assert await service.usernames(s, {alice.id, bob.id}) == {alice.id: "alice", bob.id: "bob"}
        assert await service.usernames(s, set()) == {}


async def test_run_once_settles_unknown_rows_and_tells_the_user(world):
    factory, alice, bob = world
    async with factory() as s:
        gas = await _token(s, "neo_n3", "GAS")
        s.add_all([
            _row(alice, gas, "transfer_out", -1, "unknown", 10, tx="a"),
            _row(alice, gas, "withdraw", -2, "unknown", 10, tx="b"),
            _row(alice, gas, "withdraw", -3, "unknown", 10, tx="c"),
            _row(alice, gas, "withdraw", -4, "pending", 1, tx=None),  # in flight: no hash yet, leave it
            _row(bob, gas, "withdraw", -5, "unknown", 10, tx="e"),
        ])
        await s.commit()
    outcomes = {"0x" + "a" * 64: (True, ""), "0x" + "b" * 64: (False, "faulted"), "0x" + "c" * 64: None, "0x" + "e" * 64: (True, "")}

    class Rpc:
        async def get_tx_outcome(self, tx_hash, expect_true=True):
            return outcomes[tx_hash]

    told = []

    async def notify(user, token, chain, row, status):
        told.append((user.username, int(row.amount), status))

    ops = {"neo_n3": SimpleNamespace(rpc=Rpc())}
    assert await reconcile.run_once(factory, ops, notify, user_id=alice.id) == 2  # bob's row is not touched
    assert sorted(told) == [("alice", -2, "failed"), ("alice", -1, "confirmed")]
    async with factory() as s:
        status = {int(r.amount): r.status for r in (await s.execute(select(Ledger))).scalars()}
    assert status == {-1: "confirmed", -2: "failed", -3: "unknown", -4: "pending", -5: "unknown"}
    assert await reconcile.run_once(factory, ops) == 1  # now everyone's: only bob's row was left to settle


async def test_run_once_survives_a_broken_node_and_unknown_networks(world):
    factory, alice, _ = world
    async with factory() as s:
        gas = await _token(s, "neo_n3", "GAS")
        s.add(_row(alice, gas, "withdraw", -1, "unknown", 10, tx="a"))
        await s.commit()

    class Broken:
        async def get_tx_outcome(self, tx_hash, expect_true=True):
            raise RuntimeError("node down")

    assert await reconcile.run_once(factory, {"neo_n3": SimpleNamespace(rpc=Broken())}) == 0
    assert await reconcile.run_once(factory, {}) == 0  # that network isn't served right now
    async with factory() as s:
        assert (await s.execute(select(Ledger.status))).scalar() == "unknown"


def test_tx_html_shows_the_hash_and_a_live_explorer_link():
    text = historyview.tx_html("mainnet", "bsc", HASH)
    assert f"<code>{HASH}</code>" in text
    assert f'<a href="https://bscscan.com/tx/{HASH}">View on BscScan</a>' in text
    assert "View on Etherscan" in historyview.tx_html("mainnet", "eth", HASH)
    assert "<a " not in historyview.tx_html("mainnet", "someclone", HASH)  # unknown network: hash only, no link
    assert historyview.tx_html("mainnet", "bsc", None) == ""
