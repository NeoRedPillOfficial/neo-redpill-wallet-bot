import base64

from bot import neovote

K1 = "02" + "ab" * 32
K2 = "03" + "cd" * 32


def test_address_hash160_known_address():
    # the zero script hash is the address NKuyBkoGdZZSLyPbJEetheRhMjeznFZszf
    assert neovote.address_hash160("NKuyBkoGdZZSLyPbJEetheRhMjeznFZszf") == "0x" + "00" * 20


def test_parse_candidates_ranks_by_votes_and_keeps_non_consensus_candidates():
    # the node's "active" flag only marks the 7 consensus nodes: every registered candidate must stay in the list
    k3 = "03" + "ee" * 32
    got = neovote.parse_candidates([
        {"publickey": K2, "votes": "50", "active": False},
        {"publickey": K1, "votes": "900", "active": True},
        {"publickey": k3, "votes": "9999", "active": False},
        {"publickey": "bad", "votes": "1"},
    ])
    assert [(c.pubkey, c.votes, c.rank) for c in got] == [(k3, 9999, 1), (K1, 900, 2), (K2, 50, 3)]


def test_tiers():
    assert [neovote.tier(r) for r in (0, 1, 7, 8, 21, 22)] == ["none", "consensus", "consensus", "committee", "committee", "none"]


def test_votes_text():
    assert neovote.votes_text(12_345_678) == "12.35M"
    assert neovote.votes_text(1500) == "1.5k"
    assert neovote.votes_text(40) == "40"


def _state(items):
    return {"state": "HALT", "stack": [{"type": "Struct", "value": items}]}


def test_parse_account_state_with_vote():
    raw = bytes.fromhex(K1)
    balance, vote = neovote.parse_account_state(_state([
        {"type": "Integer", "value": "25"}, {"type": "Integer", "value": "100"},
        {"type": "ByteString", "value": base64.b64encode(raw).decode()}, {"type": "Integer", "value": "0"},
    ]))
    assert (balance, vote) == (25, K1)


def test_parse_account_state_without_vote_or_state():
    assert neovote.parse_account_state(_state([
        {"type": "Integer", "value": "3"}, {"type": "Integer", "value": "1"}, {"type": "Any"}, {"type": "Integer", "value": "0"},
    ])) == (3, None)
    assert neovote.parse_account_state({"state": "HALT", "stack": [{"type": "Any"}]}) == (0, None)


def test_parse_account_state_fault():
    import pytest
    from bot.neo_rpc import RpcError
    with pytest.raises(RpcError):
        neovote.parse_account_state({"state": "FAULT", "exception": "boom", "stack": []})


def test_parse_unclaimed():
    assert neovote.parse_unclaimed({"unclaimed": "123456789", "address": "x"}) == 123456789
    assert neovote.parse_unclaimed({}) == 0


def test_label_uses_names_file(tmp_path):
    p = tmp_path / "n.json"
    p.write_text('{"%s": "Cool Node"}' % K1.upper())
    names = neovote.load_names(p)
    assert neovote.label(neovote.Candidate(K1, 1, 1), names) == "Cool Node"
    assert neovote.label(neovote.Candidate(K2, 1, 2), names) == "Candidate #2"
    assert neovote.load_names(tmp_path / "missing.json") == {}


def test_assign_roles_uses_node_lists_then_rank():
    cands = neovote.parse_candidates([{"publickey": K1, "votes": "900"}, {"publickey": K2, "votes": "50"}])
    by_list = neovote.assign_roles(cands, {K1, K2}, {K1})
    assert [c.role for c in by_list] == ["consensus", "committee"]
    by_rank = neovote.assign_roles(cands, set(), set())
    assert [c.role for c in by_rank] == ["consensus", "consensus"]  # ranks 1 and 2
    outside = neovote.assign_roles(cands, {K1}, set())
    assert [c.role for c in outside] == ["committee", "candidate"]


def test_parse_keys_and_gas_per_block():
    assert neovote.parse_keys([K1, {"publickey": K2, "votes": "1"}, "short"]) == {K1, K2}
    assert neovote.parse_gas_per_block({"state": "HALT", "stack": [{"type": "Integer", "value": "500000000"}]}) == 500_000_000


def test_vote_percent_and_total():
    cands = neovote.parse_candidates([{"publickey": K1, "votes": "750"}, {"publickey": K2, "votes": "250"}])
    assert neovote.total_votes(cands) == 1000
    assert neovote.vote_percent(cands[0], cands) == 75.0


def test_estimate_daily_gas():
    gas = 5 * 10**8
    top = neovote.Candidate(K1, 1_000_000, 1, "consensus")
    mid = neovote.Candidate(K2, 1_000_000, 10, "committee")
    out = neovote.Candidate(K2, 1_000_000, 30, "candidate")
    a, b = neovote.estimate_daily_gas(top, gas, 100), neovote.estimate_daily_gas(mid, gas, 100)
    assert 15 * 10**6 < a < 17 * 10**6  # about 0.16 GAS a day
    assert abs(a - 2 * b) <= 2  # the top 7 pool is twice the size
    assert neovote.estimate_daily_gas(out, gas, 100) == 0


# ------------------------------------------------------------- vote wallet steps
import asyncio

from bot import neo_send

MAIN = neovote.Account("NMAIN", b"mainkey")
SLOT = neovote.Account("NSLOT", b"slotkey")
GAS = neo_send.GAS_HASH


class FakeRpc:
    def __init__(self, gas=0, neo=0, vote_to=None):
        self.gas, self.neo, self.vote_to = gas, neo, vote_to

    async def get_balances(self, address):
        return {GAS: self.gas}


def _run(steps, rpc, outcomes=None, monkeypatch_status="confirmed"):
    """Runs the steps with a fake chain where every transaction is confirmed (or the status given)."""
    async def fake_wait(rpc_, tx_hash, *a, **k):
        return neo_send.SendOutcome(monkeypatch_status, tx_hash, "boom" if monkeypatch_status == "failed" else "")
    original = neo_send.wait_for_outcome
    neo_send.wait_for_outcome = fake_wait
    try:
        return asyncio.run(neovote.run_steps(rpc, steps))
    finally:
        neo_send.wait_for_outcome = original


def _fakes(rpc, vote_to=None, neo=0):
    calls = []

    async def transfer(key, token, to, amount):
        calls.append(("transfer", key, token, to, amount))
        return "0x" + "ab" * 32

    async def vote(key, pubkey):
        calls.append(("vote", key, pubkey))
        return "0x" + "cd" * 32

    async def standing(rpc_, address):
        return neovote.Standing(neo, vote_to, 0)

    neovote.get_standing = standing
    return calls, transfer, vote


def test_vote_steps_new_wallet_tops_up_moves_and_votes():
    rpc = FakeRpc(gas=0)
    calls, transfer, vote = _fakes(rpc, vote_to=None)
    result = _run(neovote.vote_steps(rpc, transfer, vote, MAIN, SLOT, K1, 5), rpc)
    assert result.ok
    assert calls == [
        ("transfer", b"mainkey", GAS, "NSLOT", neovote.FEE_TOPUP),
        ("transfer", b"mainkey", neovote.NEO_HASH, "NSLOT", 5),
        ("vote", b"slotkey", K1),
    ]


def test_vote_steps_existing_vote_skips_topup_and_revote():
    rpc = FakeRpc(gas=neovote.MIN_SLOT_GAS)
    calls, transfer, vote = _fakes(rpc, vote_to=K1)
    result = _run(neovote.vote_steps(rpc, transfer, vote, MAIN, SLOT, K1, 2), rpc)
    assert result.ok
    assert calls == [("transfer", b"mainkey", neovote.NEO_HASH, "NSLOT", 2)]


def test_run_steps_stops_at_the_first_failure():
    rpc = FakeRpc(gas=0)
    calls, transfer, vote = _fakes(rpc, vote_to=None)
    result = _run(neovote.vote_steps(rpc, transfer, vote, MAIN, SLOT, K1, 5), rpc, monkeypatch_status="failed")
    assert not result.ok and "fees" in result.failed_step and result.reason == "boom"
    assert len(calls) == 1  # nothing was sent after the failed step


def test_claim_steps_for_main_is_one_zero_transfer_to_itself():
    rpc = FakeRpc(gas=0)
    calls, transfer, vote = _fakes(rpc)
    assert _run(neovote.claim_steps(rpc, transfer, MAIN, MAIN, True), rpc).ok
    assert calls == [("transfer", b"mainkey", neovote.NEO_HASH, "NMAIN", 0)]


def test_claim_steps_for_a_vote_wallet_claims_then_sweeps_the_gas_to_main():
    rpc = FakeRpc(gas=neovote.MIN_SLOT_GAS + 100_000_000)  # enough fee GAS, and 1 GAS arrived
    calls, transfer, vote = _fakes(rpc)
    assert _run(neovote.claim_steps(rpc, transfer, MAIN, SLOT, False), rpc).ok
    assert calls[0] == ("transfer", b"slotkey", neovote.NEO_HASH, "NSLOT", 0)
    assert calls[1] == ("transfer", b"slotkey", GAS, "NMAIN", rpc.gas - neovote.SLOT_RESERVE)


def test_claim_steps_do_not_sweep_dust():
    rpc = FakeRpc(gas=neovote.SLOT_RESERVE + 10)
    calls, transfer, vote = _fakes(rpc)
    assert _run(neovote.claim_steps(rpc, transfer, MAIN, SLOT, False), rpc).ok
    assert [c[2] for c in calls] == [neovote.NEO_HASH]


def test_withdraw_moves_all_neo_and_gas_back():
    rpc = FakeRpc(gas=50_000_000)
    calls, transfer, vote = _fakes(rpc, neo=7, vote_to=K1)
    assert _run(neovote.withdraw_steps(rpc, transfer, MAIN, SLOT), rpc).ok
    assert calls == [
        ("transfer", b"slotkey", neovote.NEO_HASH, "NMAIN", 7),
        ("transfer", b"slotkey", GAS, "NMAIN", 50_000_000 - neovote.WITHDRAW_RESERVE),
    ]


def test_withdraw_with_no_neo_skips_the_neo_transfer():
    rpc = FakeRpc(gas=neovote.MIN_SLOT_GAS)
    calls, transfer, vote = _fakes(rpc, neo=0)
    assert _run(neovote.withdraw_steps(rpc, transfer, MAIN, SLOT), rpc).ok
    assert calls == []  # nothing to move, GAS below the sweep floor


def test_finish_vote_votes_only_when_not_voting_yet():
    rpc = FakeRpc(gas=neovote.MIN_SLOT_GAS)
    calls, transfer, vote = _fakes(rpc, vote_to=None)
    assert _run(neovote.finish_vote_steps(rpc, transfer, vote, MAIN, SLOT, K1), rpc).ok
    assert calls == [("vote", b"slotkey", K1)]


# ------------------------------------------------------------- vote wallets in the database
import pytest

from bot import crypto, service
from bot.db import init_db, make_session_factory

MASTER = crypto.generate_master_key()


@pytest.fixture
async def vote_world():
    engine, factory = make_session_factory("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    async with factory() as s:
        alice = await service.get_or_create_user(s, 1, "alice")
        bob = await service.get_or_create_user(s, 2, "bob")
        await s.commit()
    yield factory, alice, bob
    await engine.dispose()


async def test_vote_wallet_is_created_once_per_candidate_and_key_round_trips(vote_world):
    factory, alice, bob = vote_world
    async with factory() as s:
        first = await service.create_vote_wallet(s, alice, MASTER, K1.upper())
        await s.commit()
        key = service.vote_wallet_key(MASTER, first)
        assert len(key) == 32 and first.candidate == K1  # stored in lower case
        assert (await service.get_vote_wallet(s, alice, K1)).address == first.address
        assert await service.get_vote_wallet(s, alice, K2) is None
        assert await service.get_vote_wallet(s, bob, K1) is None  # another person never sees it
        second = await service.create_vote_wallet(s, alice, MASTER, K2)
        await s.commit()
        assert [w.address for w in await service.get_vote_wallets(s, alice)] == [first.address, second.address]
        assert first.address != second.address


async def test_vote_wallet_key_needs_the_right_master_key(vote_world):
    factory, alice, _bob = vote_world
    async with factory() as s:
        wallet = await service.create_vote_wallet(s, alice, MASTER, K1)
        await s.commit()
        with pytest.raises(Exception):
            service.vote_wallet_key(crypto.generate_master_key(), wallet)


def test_names_file_can_use_the_first_characters_of_a_public_key():
    names = {K1[:8]: {"name": "Short Key Node"}}
    assert neovote.label(neovote.Candidate(K1, 1, 1), names) == "Short Key Node"
    assert neovote.label(neovote.Candidate(K2, 1, 2), names) == "Candidate #2"
