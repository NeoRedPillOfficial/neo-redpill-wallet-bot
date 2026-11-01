"""Reading blocks instead of searching logs (for nodes that refuse eth_getLogs)."""
import pytest

from bot import evm_rpc, unlisted
from bot.evm_rpc import EvmRpc, LogsRestricted, input_mentions, transfers_from_receipt

ME = "0x" + "ab" * 20
OTHER = "0x" + "cd" * 20
TOKEN = "0x" + "11" * 20


def word(addr: str) -> str:
    return addr[2:].rjust(64, "0")


def transfer_input(to: str, amount: int = 5) -> str:
    return "0xa9059cbb" + word(to) + format(amount, "064x")


def log_entry(to: str, sender: str = OTHER, amount: int = 5, index: int = 0, tx: str = "0xaa"):
    return {
        "address": TOKEN, "topics": [evm_rpc.TRANSFER_TOPIC, "0x" + word(sender), "0x" + word(to)],
        "data": "0x" + format(amount, "064x"), "transactionHash": tx, "logIndex": hex(index), "blockNumber": "0x10",
    }


def test_input_mentions_finds_address_argument():
    assert input_mentions(transfer_input(ME), {ME[2:]})
    assert not input_mentions(transfer_input(OTHER), {ME[2:]})
    assert not input_mentions("0x", {ME[2:]})
    assert not input_mentions("", {ME[2:]})


def test_receipt_transfers_only_for_our_wallets_and_only_if_successful():
    receipt = {"status": "0x1", "logs": [log_entry(ME), log_entry(OTHER, index=1)]}
    found = transfers_from_receipt(receipt, {ME})
    assert [(t.recipient, t.amount, t.token) for t in found] == [(ME, 5, TOKEN)]
    assert transfers_from_receipt({"status": "0x0", "logs": [log_entry(ME)]}, {ME}) == []
    assert transfers_from_receipt(None, {ME}) == []


class FakeRpc(EvmRpc):
    def __init__(self, blocks, receipts):
        super().__init__("http://x")
        self.blocks, self.receipts = blocks, receipts
        self.calls = []

    async def call(self, method, params=None):
        self.calls.append(method)
        if method == "eth_getBlockByNumber":
            return self.blocks.get(int(params[0], 16))
        if method == "eth_getTransactionReceipt":
            return self.receipts.get(params[0])
        raise AssertionError(method)


async def test_blocks_scan_finds_unlisted_token_and_fetches_receipts_only_when_needed():
    blocks = {
        1: {"transactions": [{"hash": "0xt1", "input": transfer_input(OTHER)}]},
        2: {"transactions": [{"hash": "0xt2", "input": transfer_input(ME)}, {"hash": "0xt3", "input": "0x"}]},
        3: {"transactions": []},
    }
    receipts = {"0xt2": {"status": "0x1", "logs": [log_entry(ME, tx="0xt2")]}}
    rpc = FakeRpc(blocks, receipts)
    found, last = await rpc.get_transfers_by_blocks(1, 3, [ME])
    assert last == 3
    assert [(t.tx_hash, t.recipient, t.amount) for t in found] == [("0xt2", ME, 5)]
    assert rpc.calls.count("eth_getTransactionReceipt") == 1  # only the transaction that names our wallet


async def test_blocks_scan_stops_before_a_block_the_node_does_not_have_yet():
    rpc = FakeRpc({1: {"transactions": []}, 3: {"transactions": []}}, {})  # block 2 missing
    found, last = await rpc.get_transfers_by_blocks(1, 3, [ME])
    assert found == [] and last == 1


async def test_open_logs_restriction_is_reported_so_the_watcher_can_switch():
    class Refusing(EvmRpc):
        async def call(self, method, params=None):
            raise evm_rpc.EvmRpcError("eth_getLogs: {'code': -32005, 'message': 'limit exceeded'}")

    with pytest.raises(LogsRestricted):
        await Refusing("http://x").get_open_logs(1, 2, [ME])


def test_every_transfer_gets_its_own_notice_but_the_same_one_is_not_repeated():
    unlisted._seen.clear()
    unlisted._sent.clear()
    assert unlisted.allow(1, "BNB Smart Chain", "0xToken", now=0, tx="0xa")
    assert unlisted.allow(1, "BNB Smart Chain", "0xtoken", now=1, tx="0xb")  # same token, new transfer
    assert not unlisted.allow(1, "BNB Smart Chain", "0xtoken", now=2, tx="0xB")  # same transfer again
