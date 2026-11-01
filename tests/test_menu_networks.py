"""Tests for the home page layout, /help text, and the extra EVM networks."""
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from bot import config, helptext, homeview, service
from bot.config import EvmChainConfig
from bot.db import init_db, make_session_factory

NOW = datetime(2026, 10, 19, 4, 30, 5, tzinfo=timezone.utc)
D = Decimal


def row(symbol, qty, price=None, value=None, zero=False):
    return homeview.Row(symbol, qty, zero, D(price) if price else None, D(value) if value else None)


# -------------------------------------------------------------------- home page
def test_home_shows_total_networks_and_folds_empty_ones():
    sections = [
        homeview.Section("neo_n3", "Neo N3", [row("GAS", "12.5", "4.5", "56.25"), row("NEO", "0", zero=True)], True),
        homeview.Section("eth", "Ethereum", [row("ETH", "0", zero=True)]),
        homeview.Section("polygon", "Polygon", [row("POL", "0", zero=True)]),
        homeview.Section("bsc", "BNB Smart Chain", [row("BNB", "2", "600", "1200")]),
    ]
    text = homeview.render_home(sections, NOW, "classic")
    assert "≈ $1,256.25" in text  # 56.25 + 1200
    assert "<b>GAS</b>  12.5  ≈ <b>$56.25</b>" in text and "$4.50 per GAS" in text
    assert "<b>NEO</b>  0" in text  # Neo N3 is always listed in full
    assert text.index("Neo N3") < text.index("BNB Smart Chain")
    assert "Nothing yet on: Ethereum · Polygon" in text
    assert "<b>Ethereum</b>" not in text  # an empty network is folded into one line
    assert "04:30:05 UTC" in text


def test_home_without_any_price_has_no_total_line():
    text = homeview.render_home([homeview.Section("neo_n3", "Neo N3", [row("NRP", "10")], True)], NOW, "classic")
    assert "Total balance" not in text and "<b>NRP</b>  10" in text


def test_home_unavailable_network_and_token():
    sections = [
        homeview.Section("neo_n3", "Neo N3", [row("GAS", None)], True),
        homeview.Section("eth", "Ethereum", None),
    ]
    text = homeview.render_home(sections, NOW, "classic")
    assert "<b>GAS</b>  unavailable" in text
    assert "Ethereum</b>\n<blockquote>⚠️ Network unavailable" in text


def test_home_escapes_token_and_network_names():
    text = homeview.render_home([homeview.Section("neo_n3", "A<b>", [row("X&Y", "1")], True)], NOW, "classic")
    assert "A&lt;b&gt;" in text and "X&amp;Y" in text


def test_home_stays_under_the_telegram_limit():
    def big(slug, n):
        return homeview.Section(slug, slug.title(), [row(f"TOK{i}", "1.5", "2", "3") for i in range(n)])

    sections = [homeview.Section("neo_n3", "Neo N3", [row("GAS", "1", "4", "4")], True)]
    sections += [big(f"chain{i}", 40) for i in range(8)]
    text = homeview.render_home(sections, NOW, "classic")
    assert len(text) <= 4096
    assert "Neo N3" in text and "and more networks" in text  # the tail was dropped, not cut mid-tag
    assert text.count("<blockquote>") == text.count("</blockquote>")


# ------------------------------------------------------------------------- help
NETS = [("Neo N3", "GAS"), ("Neo X", "GAS"), ("Ethereum", "ETH")]


def test_help_main_lists_networks_and_commands():
    text = helptext.main_text(NETS, bridge_on=True)
    assert "Neo N3 · Neo X · Ethereum" in text and "/bridge" in text and "/tip" in text and "/help" in text
    assert "/bridge" not in helptext.main_text(NETS, bridge_on=False)
    assert len(text) < 4096


def test_every_help_topic_exists_and_fits():
    for bridge_on in (True, False):
        for key, label in helptext.topic_keys(bridge_on):
            body = helptext.topic_text(key, NETS, bridge_on)
            assert body and len(body) < 4096, key
            assert f"help:{key}".encode().__len__() <= 64
    assert helptext.topic_text("nonsense", NETS, True) is None
    assert helptext.topic_text("bridge", NETS, False) is None
    assert ("bridge", "🌉 Bridge") in helptext.topic_keys(True)
    assert all(k != "bridge" for k, _ in helptext.topic_keys(False))


def test_getting_started_names_each_networks_fee_coin():
    body = helptext.topic_text("start", NETS, True)
    assert "GAS on Neo X" in body and "ETH on Ethereum" in body


# ------------------------------------------------------------- more EVM networks
def test_builtin_evm_specs_are_sane():
    ids, envs = set(), set()
    for slug, (name, main_id, test_id, symbol, env_var, confirmations) in config.EVM_CHAIN_SPECS.items():
        assert len(slug) <= 20 and slug == slug.lower()
        assert main_id not in ids and test_id != main_id
        ids.add(main_id)
        assert env_var not in envs and env_var.endswith("_RPC")
        envs.add(env_var)
        assert confirmations >= 1 and symbol
    for slug in ("eth", "polygon", "arbitrum", "optimism", "base", "avalanche", "bsc", "neo_x"):
        assert slug in config.EVM_CHAIN_SPECS
    assert config.EVM_CHAIN_SPECS["eth"][1] == 1 and config.EVM_CHAIN_SPECS["polygon"][1] == 137


def test_extra_chains_parse():
    parsed = config.parse_extra_chains(
        "linea|Linea|59144|eth|https://rpc.linea.build; gnosis|Gnosis Chain|100|xDAI|https://rpc.gnosischain.com;",
        reserved={"bsc", "eth"},
    )
    assert set(parsed) == {"linea", "gnosis"}
    assert parsed["linea"] == EvmChainConfig("linea", "Linea", 59144, "https://rpc.linea.build", "ETH", 12)
    assert parsed["gnosis"].native_symbol == "XDAI" and parsed["gnosis"].chain_id == 100
    assert config.parse_extra_chains("", set()) == {}


@pytest.mark.parametrize(
    "text",
    [
        "x|Name|1|ETH",  # missing the RPC
        "bsc|Name|1|ETH|https://r",  # slug already used
        "A!|Name|1|ETH|https://r",  # bad slug
        "x|Name|abc|ETH|https://r",  # bad chain id
        "x|Name|0|ETH|https://r",
        "x|Name|1|ETH|ftp://r",  # not http(s)
        "x|<b>Name|1|ETH|https://r",  # markup in the name
        "x|Name|1|E T H|https://r",  # bad symbol
        "x|N|1|ETH|https://r;x|N2|2|ETH|https://r2",  # duplicate slug
    ],
)
def test_extra_chains_reject_bad_entries(text):
    with pytest.raises(RuntimeError):
        config.parse_extra_chains(text, reserved={"bsc", "eth"})


async def test_seeding_creates_every_configured_chain_with_its_native_coin():
    cfgs = {
        "eth": EvmChainConfig("eth", "Ethereum", 1, "http://x", "ETH", 12),
        "polygon": EvmChainConfig("polygon", "Polygon", 137, "http://x", "POL", 30),
        "linea": EvmChainConfig("linea", "Linea", 59144, "http://x", "ETH", 12),
    }
    engine, factory = make_session_factory("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    async with factory() as s:
        await service.seed_defaults(s, cfgs)
        await service.seed_defaults(s, cfgs)  # running twice must not duplicate anything
        user = await service.get_or_create_user(s, 1, "alice")
        for slug, symbol in (("eth", "ETH"), ("polygon", "POL"), ("linea", "ETH"), ("bsc", "BNB"), ("neo_x", "GAS")):
            chain = await service.get_chain(s, slug)
            assert chain is not None and chain.key_type == "evm", slug
            native = await service.native_token(s, chain.id)
            assert native is not None and native.symbol == symbol and native.is_default and native.decimals == 18
            assert [t.symbol for t in await service.visible_tokens(s, user, chain.id)] == [symbol]
    await engine.dispose()
