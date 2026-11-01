"""Heads-up for unlisted tokens, and removing tokens from a user's list."""
import pytest

from bot import crypto, service, unlisted
from bot.db import init_db, make_session_factory


def setup_function(_):
    unlisted._seen.clear()
    unlisted._sent.clear()


def test_one_notice_per_token_and_network():
    assert unlisted.allow(1, "BNB Smart Chain", "0xAbC", now=0)
    assert not unlisted.allow(1, "BNB Smart Chain", "0xabc", now=1)  # same token (case-insensitive)
    assert unlisted.allow(1, "Ethereum", "0xabc", now=2)  # same address on another network is another token
    assert unlisted.allow(2, "BNB Smart Chain", "0xabc", now=3)  # another user


def test_a_spammer_cannot_flood_a_user():
    sent = [unlisted.allow(1, "Polygon", f"0x{i}", now=i) for i in range(40)]
    assert sent.count(True) == unlisted.MAX_PER_HOUR
    assert unlisted.allow(1, "Polygon", "0xlater", now=3601 + 40)  # allowed again after an hour
    assert unlisted.allow(1, "Polygon", "0xfriend", now=41, tx="0xt", priority=True) is not None  # never held back for a bot user


async def test_report_sends_through_the_hook_once():
    told = []

    async def hook(user, network, asset, detail=None):
        told.append((user.id, network, asset))

    unlisted.HOOK = hook
    try:
        user = type("U", (), {"id": 7})()
        await unlisted.report(user, "Solana", "MintA")
        await unlisted.report(user, "Solana", "MintA")
        assert told == [(7, "Solana", "MintA")]
    finally:
        unlisted.HOOK = None


async def test_a_failing_hook_does_not_break_the_watcher():
    async def hook(user, network, asset, detail=None):
        raise RuntimeError("telegram is down")

    unlisted.HOOK = hook
    try:
        await unlisted.report(type("U", (), {"id": 8})(), "Solana", "MintB")  # must not raise
    finally:
        unlisted.HOOK = None


@pytest.fixture
async def world():
    engine, factory = make_session_factory("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    async with factory() as s:
        await service.seed_defaults(s)
        alice = await service.get_or_create_user(s, 1, "alice")
        bob = await service.get_or_create_user(s, 2, "bob")
        chain = await service.get_chain(s, "bsc") or await service.get_chain(s, "neo_x")
        await s.commit()
    yield factory, alice, bob, chain
    await engine.dispose()


async def test_remove_hides_the_token_for_that_user_only(world):
    factory, alice, bob, chain = world
    async with factory() as s:
        token, _ = await service.add_token_for_user(s, alice, chain.id, "0x" + "ab" * 20, "SCAM", "Scam Coin", 18)
        await service.add_token_for_user(s, bob, chain.id, "0x" + "ab" * 20, "SCAM", "Scam Coin", 18)
    async with factory() as s:
        assert [t.symbol for t, _ in await service.removable_tokens(s, alice)] == ["SCAM"]
        assert (await service.remove_token(s, alice, token.id)).symbol == "SCAM"
        assert await service.removable_tokens(s, alice) == []
        assert token.id not in await service.allowed_token_ids(s, alice, chain.id)
        assert token.id in await service.allowed_token_ids(s, bob, chain.id)  # bob keeps his
        assert await service.remove_token(s, alice, token.id) is None  # nothing left to remove


async def test_built_in_tokens_cannot_be_removed_and_a_token_can_be_added_back(world):
    factory, alice, _, chain = world
    async with factory() as s:
        native = await service.native_token(s, chain.id)
        assert await service.remove_token(s, alice, native.id) is None
        assert native.id in await service.allowed_token_ids(s, alice, chain.id)
        token, newly = await service.add_token_for_user(s, alice, chain.id, "0x" + "cd" * 20, "OK", "Ok", 6)
        assert newly
        await service.remove_token(s, alice, token.id)
        again, newly = await service.add_token_for_user(s, alice, chain.id, "0x" + "cd" * 20, "OK", "Ok", 6)
        assert newly and again.id == token.id


async def test_two_first_messages_from_a_new_user_at_once_do_not_crash(tmp_path):
    import asyncio

    engine, factory = make_session_factory(f"sqlite+aiosqlite:///{tmp_path / 'race.db'}")
    await init_db(engine)

    async def first_message():
        async with factory() as s:
            user = await service.get_or_create_user(s, 777, "newbie")
            await s.commit()
            return user.id

    ids = await asyncio.gather(*(first_message() for _ in range(6)))
    assert len(set(ids)) == 1  # everyone ended up with the same single row
    await engine.dispose()


async def test_incoming_from_another_user_is_recorded_once_and_shows_in_history(world):
    factory, alice, bob, chain = world
    tx = "0x" + "11" * 32
    async with factory() as s:
        token, _ = await service.add_token_for_user(s, alice, chain.id, "0x" + "ee" * 20, "NEWT", "New", 18)
        # bob has NOT added the token (it is "unlisted" for him), and still gets a history row
        row = await service.record_incoming_from_user(s, bob, token, alice.id, 5 * 10**17, tx, "0xalice")
        assert row is not None and row.kind == "transfer_in" and row.event_index == -1
        again = await service.record_incoming_from_user(s, bob, token, alice.id, 5 * 10**17, tx, "0xalice")
        assert again is None  # the chain scan or a second call must not duplicate it
    async with factory() as s:
        rows = await service.history_page(s, bob, False, 0, 10)
        assert [(r.kind, int(r.amount)) for r, _, _ in rows] == [("transfer_in", 5 * 10**17)]
