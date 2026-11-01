"""Tests for EVM support (BNB Smart Chain / Neo X), add-token and the EVM watcher.
All use fakes: no network needed."""
from types import SimpleNamespace

import pytest
from eth_account import Account
from sqlalchemy import select

from bot import chains, crypto, evm_rpc, evm_send, evm_watcher, service
from bot import wallets as w
from bot.config import EvmChainConfig
from bot.db import Ledger, init_db, make_session_factory
from bot.evm_rpc import EvmRpcError, TokenTransferLog
from bot.neo_send import SendError

MASTER = crypto.generate_master_key()
CFG = EvmChainConfig("bsc", "BNB Smart Chain", 56, "http://unused", "BNB", 5)
TOKEN = "0x" + "ab" * 20
ETH = 10**18


# ----------------------------------------------------------------- ABI helpers
def test_encoding():
    addr = "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed"
    assert evm_rpc.pad_address(addr) == "0" * 24 + addr[2:].lower()
    assert evm_rpc.encode_balance_of(addr) == "0x70a08231" + "0" * 24 + addr[2:].lower()
    data = evm_rpc.encode_transfer(addr, 1000)
    assert data.startswith("0xa9059cbb") and len(data) == 2 + 8 + 64 + 64
    assert data.endswith(format(1000, "064x"))
    with pytest.raises(ValueError):
        evm_rpc.encode_transfer(addr, -1)


def _abi_string(text: str) -> str:
    body = text.encode()
    return "0x" + (32).to_bytes(32, "big").hex() + len(body).to_bytes(32, "big").hex() + body.hex().ljust(64, "0")


def test_decode_strings():
    assert evm_rpc.decode_abi_string(_abi_string("USDT")) == "USDT"
    assert evm_rpc.decode_abi_string("0x" + b"MKR".hex().ljust(64, "0")) == "MKR"  # bytes32 style
    for bad in ("0x", "0xzz", "0x" + "00" * 40):
        with pytest.raises(EvmRpcError):
            evm_rpc.decode_abi_string(bad)
    with pytest.raises(EvmRpcError):
        evm_rpc.hex_to_int("0x")


def test_parse_transfer_log():
    base = {
        "address": TOKEN.upper().replace("0X", "0x"),
        "topics": [evm_rpc.TRANSFER_TOPIC, "0x" + "0" * 24 + "11" * 20, "0x" + "0" * 24 + "22" * 20],
        "data": "0x" + format(5 * ETH, "064x"),
        "transactionHash": "0xAA" + "bb" * 31,
        "logIndex": "0x3",
        "blockNumber": "0x3e9",
    }
    lg = evm_rpc.parse_transfer_log(base)
    assert (lg.token, lg.sender, lg.recipient, lg.amount, lg.log_index, lg.block) == (
        TOKEN, "0x" + "11" * 20, "0x" + "22" * 20, 5 * ETH, 3, 1001,
    )
    nft = dict(base, topics=base["topics"] + ["0x" + "00" * 32], data="0x")
    assert evm_rpc.parse_transfer_log(nft) is None
    assert evm_rpc.parse_transfer_log(dict(base, data="0x01")) is None


async def test_token_info_reads_the_contract():
    rpc = evm_rpc.EvmRpc("http://unused")

    async def fake_call(method, params=None):
        if method == "eth_getCode":
            return "0x6080"
        data = params[0]["data"]
        return {
            "0x313ce567": "0x" + format(18, "064x"),
            "0x95d89b41": _abi_string("USDT"),
            "0x06fdde03": _abi_string("Tether USD"),
        }[data]

    rpc.call = fake_call
    assert await rpc.token_info(TOKEN) == ("USDT", "Tether USD", 18)

    async def no_contract(method, params=None):
        return "0x"

    rpc.call = no_contract
    with pytest.raises(EvmRpcError, match="no contract"):
        await rpc.token_info(TOKEN)


# ------------------------------------------------------------------ fake node
class FakeEvmRpc:
    def __init__(self, head=1000, logs=None, balances=None, token_balances=None, gas=21000,
                 price=10**9, fail_estimate=False, receipt=None, nonce=7):
        self.head, self.logs = head, logs or []
        self.balances, self.token_balances = balances or {}, token_balances or {}
        self.gas, self.price, self.fail_estimate = gas, price, fail_estimate
        self.receipt, self.nonce = receipt, nonce
        self.sent, self.log_calls = [], []
        self.address_required, self.block_mode = False, False
        self.token_infos = {}

    async def block_number(self):
        return self.head

    async def get_open_logs(self, start, end, recipients, chunk=50):
        self.log_calls.append((start, end, recipients))
        return [lg for lg in self.logs if start <= lg.block <= end]

    async def get_transfers_by_blocks(self, start, end, wallets, concurrency=6):
        self.log_calls.append((start, end, wallets))
        return [lg for lg in self.logs if start <= lg.block <= end], end

    async def token_info(self, contract):
        info = self.token_infos.get(contract.lower())
        if info is None:
            raise EvmRpcError("no contract")
        return info

    async def get_balance(self, address, block=None):
        value = self.balances.get(address.lower(), 0)
        if value is None:
            raise EvmRpcError("node down")
        return value

    async def erc20_balance(self, contract, owner):
        value = self.token_balances.get(contract.lower(), 0)
        if value is None:
            raise EvmRpcError("call failed")
        return value

    async def estimate_gas(self, tx):
        if self.fail_estimate:
            raise EvmRpcError("execution reverted")
        return self.gas

    async def gas_price(self):
        return self.price

    async def get_nonce(self, address):
        return self.nonce

    async def send_raw(self, raw_hex):
        self.sent.append(raw_hex)
        return "0x" + "ab" * 32

    async def get_receipt(self, tx_hash):
        return self.receipt


# --------------------------------------------------------------------- sending
async def test_quote_adds_headroom_and_catches_reverts():
    q = await evm_send.quote(FakeEvmRpc(), "0xa", "0xb", evm_send.NATIVE, 1)
    assert (q.gas_limit, q.gas_price) == (25200, 1_100_000_000)
    assert q.fee == 25200 * 1_100_000_000
    with pytest.raises(SendError, match="would fail"):
        await evm_send.quote(FakeEvmRpc(fail_estimate=True), "0xa", "0xb", TOKEN, 1)


async def test_signed_transactions_recover_to_the_sender():
    kp, to = w.evm_generate(), w.evm_generate().address
    q = evm_send.Quote(60000, 10**9)
    for contract in (evm_send.NATIVE, TOKEN):
        rpc = FakeEvmRpc()
        tx_hash = await evm_send.sign_and_send(rpc, 56, kp.private_key, kp.address, to, contract, 10**15, q)
        assert tx_hash == "0x" + "ab" * 32
        assert len(rpc.sent) == 1 and rpc.sent[0].startswith("0x")
        assert Account.recover_transaction(rpc.sent[0]) == kp.address


@pytest.mark.parametrize("status,expected", [("0x1", "confirmed"), ("0x0", "failed")])
async def test_perform_send_outcomes(status, expected):
    kp, to = w.evm_generate(), w.evm_generate().address
    rpc = FakeEvmRpc(receipt={"status": status})
    out = await evm_send.perform_send(
        rpc=rpc, chain_id=56, private_key=kp.private_key, from_addr=kp.address, to_addr=to,
        token_contract=TOKEN, raw=5, interval=0.001,
    )
    assert out.status == expected and out.tx_hash == "0x" + "ab" * 32


async def test_perform_send_unknown_when_no_receipt_and_refusal():
    kp, to = w.evm_generate(), w.evm_generate().address
    out = await evm_send.perform_send(
        rpc=FakeEvmRpc(receipt=None), chain_id=56, private_key=kp.private_key, from_addr=kp.address,
        to_addr=to, token_contract=evm_send.NATIVE, raw=5, timeout=0, interval=0.001,
    )
    assert out.status == "unknown"
    with pytest.raises(SendError, match="would fail"):
        await evm_send.perform_send(
            rpc=FakeEvmRpc(fail_estimate=True), chain_id=56, private_key=kp.private_key,
            from_addr=kp.address, to_addr=to, token_contract=TOKEN, raw=5,
        )


# ------------------------------------------------------------------ chain ops
def test_clean_label():
    assert chains.clean_label("USDT", 20) == "USDT"
    assert chains.clean_label("Tether USD", 60) == "Tether USD"
    for bad in ("", "  ", "<b>x</b>", "Ｕ𝗦𝗗𝗧", "x" * 21, "-bad", "a\nb"):
        assert chains.clean_label(bad, 20) is None


def test_evm_address_rules():
    ops = chains.EvmOps(CFG, FakeEvmRpc())
    good = "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed"
    assert ops.is_valid_address(good) and ops.is_valid_address(good.lower())
    assert not ops.is_valid_address("0x5AAeb6053F3E94C9b9A09f33669435E7Ef1BeAed")  # bad checksum
    assert not ops.is_valid_address("5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed")  # no 0x
    assert not ops.is_valid_address("0x1234")
    assert ops.normalize_contract(good) == good.lower()
    with pytest.raises(SendError):
        ops.normalize_contract("nonsense")


def test_n3_contract_rules():
    ops = chains.N3Ops(rpc=None, sender=None)
    assert ops.normalize_contract("ABCDEF" + "00" * 17) == "0x" + "abcdef" + "00" * 17
    with pytest.raises(SendError):
        ops.normalize_contract("0x1234")


async def test_evm_balances_are_per_token_and_failures_are_isolated():
    addr = w.evm_generate().address
    rpc = FakeEvmRpc(balances={addr.lower(): ETH}, token_balances={TOKEN: 500, "0x" + "cd" * 20: None})
    ops = chains.EvmOps(CFG, rpc)
    tokens = [
        SimpleNamespace(contract="native"),
        SimpleNamespace(contract=TOKEN),
        SimpleNamespace(contract="0x" + "cd" * 20),
    ]
    out = await ops.balances(addr, tokens)
    assert out == {"native": ETH, TOKEN: 500, "0x" + "cd" * 20: None}
    down = chains.EvmOps(CFG, FakeEvmRpc(balances={addr.lower(): None}))
    with pytest.raises(EvmRpcError):
        await down.balances(addr, tokens[:1])


async def test_evm_check_send_needs_token_and_native_for_fees():
    ops = chains.EvmOps(CFG, FakeEvmRpc())
    token = SimpleNamespace(contract=TOKEN, symbol="USDT")
    native = SimpleNamespace(contract="native", symbol="BNB")
    me, to = w.evm_generate().address, w.evm_generate().address
    fee = 25200 * 1_100_000_000

    await ops.check_send(token, me, to, 500, {"native": fee, TOKEN: 500})  # exactly enough
    with pytest.raises(SendError, match="enough USDT"):
        await ops.check_send(token, me, to, 501, {"native": ETH, TOKEN: 500})
    with pytest.raises(SendError, match="network fee"):
        await ops.check_send(token, me, to, 1, {"native": fee - 1, TOKEN: 500})
    with pytest.raises(SendError, match="read"):
        await ops.check_send(token, me, to, 1, {"native": ETH, TOKEN: None})
    # Sending the native coin must leave the fee behind.
    await ops.check_send(native, me, to, ETH - fee, {"native": ETH})
    with pytest.raises(SendError, match="smaller amount"):
        await ops.check_send(native, me, to, ETH, {"native": ETH})
    assert await ops.max_sendable(native, me, {"native": ETH}) == ETH - fee
    assert await ops.max_sendable(token, me, {"native": ETH, TOKEN: 500}) == 500


# --------------------------------------------------------- database + watchers
@pytest.fixture
async def world():
    engine, factory = make_session_factory("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    async with factory() as s:
        await service.seed_defaults(s)
        user = await service.get_or_create_user(s, 1, "alice")
        wallets = await service.ensure_wallets(s, user, MASTER)
        await s.commit()
    yield factory, user, next(x for x in wallets if x.key_type == "evm")
    await engine.dispose()


async def _add_usdt(factory, user, contract=TOKEN):
    async with factory() as s:
        chain = await service.get_chain(s, "bsc")
        token, _ = await service.add_token_for_user(s, user, chain.id, contract, "USDT", "Tether USD", 18)
        return token


async def test_added_tokens_are_per_user(world):
    factory, user, _ = world
    async with factory() as s:
        bob = await service.get_or_create_user(s, 2, "bob")
        await service.ensure_wallets(s, bob, MASTER)
        await s.commit()
        chain = await service.get_chain(s, "bsc")
        token, new = await service.add_token_for_user(s, user, chain.id, TOKEN, "USDT", "Tether USD", 18)
        again, new_again = await service.add_token_for_user(s, user, chain.id, TOKEN, "USDT", "Tether USD", 18)
        assert new and not new_again and again.id == token.id
        assert token.decimals_verified and not token.is_default
        assert {t.symbol for t in await service.visible_tokens(s, user, chain.id)} == {"BNB", "USDT"}
        assert {t.symbol for t in await service.visible_tokens(s, bob, chain.id)} == {"BNB"}
        assert token.id in await service.allowed_token_ids(s, user, chain.id)
        assert token.id not in await service.allowed_token_ids(s, bob, chain.id)
        assert len(await service.look_alike_tokens(s, chain.id, "usdt", "0x" + "cd" * 20)) == 1
        assert await service.look_alike_tokens(s, chain.id, "usdt", TOKEN) == []


async def test_token_deposit_detected_once_and_only_for_added_tokens(world):
    factory, user, wallet = world
    token = await _add_usdt(factory, user)
    stranger_token = "0x" + "cd" * 20
    me = wallet.address.lower()
    mk = lambda tok, tx, idx: TokenTransferLog(tok, "0x" + "11" * 20, me, 5 * ETH, tx, idx, 1001)
    rpc = FakeEvmRpc(head=1000)
    ops = SimpleNamespace(cfg=CFG, rpc=rpc)
    seen = []

    async def notify(u, t, transfer, chain_name):
        seen.append((t.symbol, transfer.amount, chain_name))

    await evm_watcher.scan_token_logs(factory, ops, notify)  # first run: cursor only
    async with factory() as s:
        assert (await service.get_chain(s, "bsc")).last_scanned_block == 995
    assert rpc.log_calls == []

    rpc.head = 1010
    rpc.logs = [mk(TOKEN, "0x" + "aa" * 32, 3), mk(stranger_token, "0x" + "bb" * 32, 4)]
    await evm_watcher.scan_token_logs(factory, ops, notify)
    assert seen == [("USDT", 5 * ETH, "BNB Smart Chain")]
    assert rpc.log_calls[-1][2] == [me]
    async with factory() as s:
        chain = await service.get_chain(s, "bsc")
        assert chain.last_scanned_block == 1005
        rows = (await s.execute(select(Ledger))).scalars().all()
        assert len(rows) == 1 and rows[0].kind == "deposit" and rows[0].token_id == token.id
        chain.last_scanned_block = 995  # force a rescan of the same blocks
        await s.commit()
    await evm_watcher.scan_token_logs(factory, ops, notify)
    assert len(seen) == 1  # not credited twice


async def test_native_deposits_are_balance_increases(world):
    factory, user, wallet = world
    key = wallet.address.lower()
    rpc = FakeEvmRpc(head=1000, balances={key: 0})
    ops = SimpleNamespace(cfg=CFG, rpc=rpc)
    seen = []

    async def notify(u, t, transfer, chain_name):
        seen.append(transfer.amount)

    async def step(balance):
        rpc.balances[key] = balance
        await evm_watcher.scan_native(factory, ops, notify)

    await step(0)  # new wallet starts empty: nothing to report
    await step(3 * ETH)
    await step(3 * ETH)  # unchanged
    await step(2 * ETH)  # went down (a send): not a deposit, baseline drops
    await step(2 * ETH + ETH // 2)
    assert seen == [3 * ETH, ETH // 2]
    async with factory() as s:
        kinds = [r.kind for r in (await s.execute(select(Ledger))).scalars()]
        assert kinds == ["deposit", "deposit"]


async def test_unlisted_token_found_by_reading_blocks_is_recorded_and_announced(world):
    from bot import unlisted

    factory, user, wallet = world
    doge = "0x" + "dd" * 20
    me = wallet.address.lower()
    rpc = FakeEvmRpc(head=1000)
    rpc.block_mode = True  # the node refuses log searches, so the watcher reads blocks
    rpc.token_infos[doge] = ("DOGE", "Doge Coin", 18)
    ops = SimpleNamespace(cfg=CFG, rpc=rpc)
    told = []

    async def hook(u, network, asset, detail=None):
        told.append((u.id, network, asset.lower(), detail))

    unlisted._seen.clear()
    unlisted._sent.clear()
    unlisted.HOOK = hook
    try:
        async def notify(*a):
            raise AssertionError("an unlisted token must not go through the normal deposit notice")

        await evm_watcher.scan_token_logs(factory, ops, notify)  # first run: cursor only
        rpc.head = 1010
        rpc.logs = [TokenTransferLog(doge, "0x" + "11" * 20, me, 2 * ETH, "0x" + "ee" * 32, 0, 1001)]
        await evm_watcher.scan_token_logs(factory, ops, notify)
    finally:
        unlisted.HOOK = None
    assert len(told) == 1
    _uid, network, asset, detail = told[0]
    assert asset == doge and detail["symbol"] == "DOGE" and detail["amount"] == "2"
    async with factory() as s:
        rows = (await s.execute(select(Ledger))).scalars().all()
        assert len(rows) == 1 and rows[0].kind == "deposit"  # shows in the user's history
        # the seen-only token does not make a real token with the same symbol look fake
        chain = await service.get_chain(s, "bsc")
        assert await service.look_alike_tokens(s, chain.id, "doge", "0x" + "ff" * 20) == []
