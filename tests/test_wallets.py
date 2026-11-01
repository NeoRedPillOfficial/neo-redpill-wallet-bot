import base64
import os

import pytest
from cryptography.exceptions import InvalidTag

from bot import crypto, service
from bot import wallets as w
from bot.db import init_db, make_session_factory

MASTER = crypto.generate_master_key()


# ------------------------------------------------------------------ EVM / Neo X
def test_evm_known_vector():
    # Well-known test vector: private key 1 -> this address.
    kp = w.evm_from_private_key((1).to_bytes(32, "big"))
    assert kp.address == "0x7E5F4552091A69125d5DfCb7b8C2659029395Bdf"


def test_evm_export_import_roundtrip():
    kp = w.evm_generate()
    again = w.parse_import("evm", w.export_text(kp))
    assert again.address == kp.address
    assert w.parse_import("evm", w.export_text(kp)[2:]).address == kp.address


# ---------------------------------------------------------------------- Neo N3
def test_neo_address_shape_and_roundtrip():
    kp = w.neo_n3_generate()
    assert kp.address.startswith("N") and len(kp.address) == 34
    wif = w.export_text(kp)
    assert w.parse_import("neo_n3", wif).address == kp.address
    assert w.parse_import("neo_n3", kp.private_key.hex()).address == kp.address


def test_neo_matches_reference_library():
    """Cross-check our address derivation against the official-style SDK."""
    neo = pytest.importorskip("neo3.wallet.account")
    for _ in range(5):
        kp = w.neo_n3_generate()
        assert neo.Account.from_private_key(kp.private_key).address == kp.address
        assert neo.Account.from_wif(w.export_text(kp)).address == kp.address


@pytest.mark.parametrize("bad", ["", "abc", "0x" + "zz" * 32, "00" * 32])
def test_bad_imports_rejected(bad):
    with pytest.raises(ValueError):
        w.parse_import("neo_n3", bad)


def test_evm_bad_import_rejected():
    with pytest.raises(ValueError):
        w.parse_import("evm", "not a key")


# ------------------------------------------------------------------ encryption
def test_encryption_roundtrip_and_binding():
    secret = os.urandom(32)
    blob = crypto.encrypt_key(MASTER, secret, "addr1")
    assert crypto.decrypt_key(MASTER, blob, "addr1") == secret
    with pytest.raises(InvalidTag):
        crypto.decrypt_key(MASTER, blob, "addr2")  # bound to its address
    with pytest.raises(InvalidTag):
        crypto.decrypt_key(crypto.generate_master_key(), blob, "addr1")


def test_master_key_length_checked():
    with pytest.raises(ValueError):
        crypto.encrypt_key(base64.b64encode(b"short").decode(), b"x", "a")


# -------------------------------------------------------------------- database
@pytest.fixture
async def session():
    engine, factory = make_session_factory("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    async with factory() as s:
        await service.seed_defaults(s)
        yield s
    await engine.dispose()


async def test_wallet_lifecycle(session):
    user = await service.get_or_create_user(session, 111, "alice")
    first = await service.ensure_wallets(session, user, MASTER)
    second = await service.ensure_wallets(session, user, MASTER)
    assert {x.key_type for x in first} == {"evm", "neo_n3"}
    assert {x.address for x in first} == {x.address for x in second}  # idempotent

    # Exported key matches the stored address.
    wif = await service.export_wallet(session, user, MASTER, "neo_n3")
    neo_addr = next(x.address for x in first if x.key_type == "neo_n3")
    assert w.parse_import("neo_n3", wif).address == neo_addr

    # Import makes the new wallet the one in use but KEEPS the old one; the same key cannot be claimed by someone else.
    new_kp = w.neo_n3_generate()
    imported = await service.import_wallet(session, user, MASTER, "neo_n3", w.export_text(new_kp))
    assert imported.address == new_kp.address and imported.imported
    saved = await service.saved_wallets(session, user)
    assert [s.address for s in saved] == [neo_addr]
    bob = await service.get_or_create_user(session, 222, "bob")
    with pytest.raises(ValueError):
        await service.import_wallet(session, bob, MASTER, "neo_n3", w.export_text(new_kp))
    with pytest.raises(ValueError):  # a wallet put aside is still its owner's
        await service.import_wallet(session, bob, MASTER, "neo_n3", wif)

    # The old wallet can be exported while put aside, and switched back to.
    assert await service.export_saved_wallet(session, user, MASTER, "neo_n3", saved[0].id) == wif
    back = await service.switch_wallet(session, user, "neo_n3", saved[0].id)
    assert back.address == neo_addr
    assert [s.address for s in await service.saved_wallets(session, user)] == [new_kp.address]
    # importing a wallet that is put aside just switches to it
    again = await service.import_wallet(session, user, MASTER, "neo_n3", w.export_text(new_kp))
    assert again.address == new_kp.address

    # Delete: a put-aside wallet goes; the one in use is replaced by another wallet of the type, else by a new one.
    saved = await service.saved_wallets(session, user)
    assert await service.delete_wallet(session, user, MASTER, "neo_n3", saved[0].id) is None
    assert await service.saved_wallets(session, user) == []
    fresh = await service.delete_wallet(session, user, MASTER, "neo_n3", None)
    assert fresh.address not in (neo_addr, new_kp.address) and fresh.address.startswith("N")
    assert (await service.get_wallet(session, user, "neo_n3")).address == fresh.address


def test_default_neo_n3_contract_hashes_are_well_formed():
    import re

    for chain, contract, symbol, _name, _dec, _verified in service.DEFAULT_TOKENS:
        if chain == "neo_n3":
            assert re.fullmatch(r"0x[0-9a-f]{40}", contract), f"{symbol}: {contract}"


def test_format_amount():
    assert service.format_amount(150000000, 8) == "1.5"
    assert service.format_amount(5, 0) == "5"
    assert service.format_amount(0, 18) == "0"
