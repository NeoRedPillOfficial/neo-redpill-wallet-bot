import asyncio

from bot import evm_watcher as w

ME = "0x" + "aa" * 20


def tx(h, to, value, frm="0x" + "bb" * 20):
    return {"hash": "0x" + h * 64, "to": to, "from": frm, "value": hex(value)}


def test_only_coin_transfers_to_the_wallet_are_found():
    blocks = [{"transactions": [tx("1", ME.upper().replace("0X", "0x"), 5 * 10 ** 16), tx("2", "0x" + "cc" * 20, 10 ** 18),
                                tx("3", ME, 0), tx("4", None, 7)]}, None, {"transactions": [tx("5", ME, 10 ** 15)]}]
    found = w.incoming_txs(blocks, ME)
    assert [(h[-1], v) for h, _s, v in found] == [("1", 5 * 10 ** 16), ("5", 10 ** 15)]


def test_a_single_deposit_matches_its_transaction_exactly():
    txs = [("0x" + "1" * 64, "0xs", 5 * 10 ** 16)]
    fresh, already, rest = w.split_deposit(5 * 10 ** 16, txs, set())
    assert fresh == txs and already == 0 and rest == 0


def test_two_deposits_in_one_scan_become_two_records():
    txs = [("0xa", "s1", 3), ("0xb", "s2", 4)]
    fresh, already, rest = w.split_deposit(7, txs, set())
    assert [f[0] for f in fresh] == ["0xa", "0xb"] and rest == 0


def test_a_coin_sent_by_a_contract_leaves_a_remainder_without_a_hash():
    fresh, already, rest = w.split_deposit(10, [("0xa", "s", 4)], set())
    assert fresh == [("0xa", "s", 4)] and rest == 6


def test_a_transaction_already_recorded_is_not_recorded_or_announced_again():
    fresh, already, rest = w.split_deposit(4, [("0xa", "s", 4)], {"0xa"})
    assert fresh == [] and already == 4 and rest == 0


def test_the_amount_never_exceeds_the_balance_increase():
    fresh, already, rest = w.split_deposit(3, [("0xa", "s", 10)], set())
    assert fresh == [("0xa", "s", 3)] and rest == 0


def test_blocks_are_fetched_and_failures_skipped():
    class Rpc:
        async def get_block(self, n):
            if n == 11:
                raise RuntimeError("boom")
            return {"transactions": [tx(str(n % 10), ME, n)]}
    blocks = asyncio.run(w._blocks_between(Rpc(), 10, 12))
    assert [b is not None for b in blocks] == [True, False, True]
    assert [v for _h, _s, v in w.incoming_txs(blocks, ME)] == [10, 12]
