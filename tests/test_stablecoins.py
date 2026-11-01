import asyncio
import re

from bot import service


def test_every_stablecoin_entry_is_well_formed_and_unique():
    for slug, items in service.STABLECOINS.items():
        seen = set()
        for contract, symbol, decimals in items:
            assert re.fullmatch(r"0x[0-9a-fA-F]{40}", contract) or slug == "solana", (slug, symbol)
            assert contract.lower() not in seen
            seen.add(contract.lower())
            assert symbol in ("USDT", "USDC", "DAI") and decimals in (6, 18)


def test_is_stablecoin_ignores_case_and_other_networks():
    assert service.is_stablecoin("eth", "0xdac17f958d2ee523a2206206994597c13d831ec7")
    assert not service.is_stablecoin("bsc", "0xdac17f958d2ee523a2206206994597c13d831ec7")
    assert not service.is_stablecoin("eth", "native")


def test_solana_addresses_are_case_sensitive():
    mint = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    assert service.is_stablecoin("solana", mint)
    assert not service.is_stablecoin("solana", mint.lower())
