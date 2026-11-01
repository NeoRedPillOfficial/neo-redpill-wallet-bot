import os
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from dotenv import find_dotenv, load_dotenv

load_dotenv()


def duplicate_env_keys(text: str) -> dict[str, list[int]]:
    """Setting names that appear more than once in an .env file, with their line numbers.
    The last one wins, which is easy to miss (an indented copy at the bottom overrides the line above)."""
    seen: dict[str, list[int]] = {}
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        if key.startswith("export "):
            key = key[7:].strip()
        seen.setdefault(key, []).append(number)
    return {key: lines for key, lines in seen.items() if len(lines) > 1}


def warn_duplicate_env_keys() -> None:
    import logging

    try:
        path = find_dotenv(usecwd=True)
        if not path:
            return
        with open(path, encoding="utf-8") as handle:
            repeated = duplicate_env_keys(handle.read())
    except OSError:
        return
    for key, lines in repeated.items():
        logging.getLogger("walletbot.config").warning(
            "%s appears %s times in .env (lines %s). Only the LAST one is used. Delete the others.",
            key, len(lines), ", ".join(map(str, lines)),
        )

# Optional caps in whole tokens: SYMBOL=per_transaction/per_24h. A token with no
# entry has no cap (tokens users add themselves are uncapped). The numbers below
# are arbitrary starting points; tune them to what each token is worth.
DEFAULT_SEND_LIMITS = "GAS=50/200,NEO=100/500,NRP=1000000/5000000,FRANK=1000000/5000000"

# EVM networks the bot can serve. A network is switched on by setting its RPC
# env var. slug: (name, mainnet chain id, testnet chain id, native symbol,
# RPC env var, confirmations to wait before trusting a block)
EVM_CHAIN_SPECS = {
    "neo_x": ("Neo X", 47763, 12227332, "GAS", "NEO_X_RPC", 2),
    "bsc": ("BNB Smart Chain", 56, 97, "BNB", "BSC_RPC", 2),
    "eth": ("Ethereum", 1, 11155111, "ETH", "ETH_RPC", 3),
    "polygon": ("Polygon", 137, 80002, "POL", "POLYGON_RPC", 10),
    "arbitrum": ("Arbitrum One", 42161, 421614, "ETH", "ARBITRUM_RPC", 10),
    "optimism": ("Optimism", 10, 11155420, "ETH", "OPTIMISM_RPC", 10),
    "base": ("Base", 8453, 84532, "ETH", "BASE_RPC", 10),
    "avalanche": ("Avalanche C-Chain", 43114, 43113, "AVAX", "AVALANCHE_RPC", 10),
}

# Any other EVM chain can be added without code, in .env:
#   EXTRA_EVM_CHAINS=slug|Name|chain_id|SYMBOL|rpc_url;slug2|Name2|chain_id2|SYMBOL2|rpc_url2
# The chain id is used exactly as written (it is NOT switched by NETWORK).
EXTRA_CHAIN_CONFIRMATIONS = 12
_SLUG = re.compile(r"[a-z][a-z0-9_]{1,19}")
_SYMBOL = re.compile(r"[A-Za-z0-9]{1,10}")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._\-]{0,29}")


def parse_send_limits(text: str) -> dict[str, tuple[Decimal, Decimal]]:
    limits: dict[str, tuple[Decimal, Decimal]] = {}
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            symbol, rest = part.split("=")
            per_tx_text, daily_text = rest.split("/")
            per_tx, daily = Decimal(per_tx_text), Decimal(daily_text)
        except (ValueError, InvalidOperation):
            raise RuntimeError(f"Bad SEND_LIMITS entry {part!r}. Use SYMBOL=per_tx/daily.")
        if not (per_tx > 0 and daily >= per_tx):
            raise RuntimeError(f"SEND_LIMITS {part!r}: need 0 < per_tx <= daily.")
        limits[symbol.strip().upper()] = (per_tx, daily)
    return limits


def parse_extra_chains(text: str, reserved: set[str]) -> dict[str, "EvmChainConfig"]:
    """EXTRA_EVM_CHAINS -> configs. A bad entry stops the bot with a clear message."""
    chains: dict[str, EvmChainConfig] = {}
    for entry in text.split(";"):
        entry = entry.strip()
        if not entry:
            continue
        parts = [p.strip() for p in entry.split("|")]
        if len(parts) != 5:
            raise RuntimeError(f"Bad EXTRA_EVM_CHAINS entry {entry!r}. Use slug|Name|chain_id|SYMBOL|rpc_url.")
        slug, name, chain_id_text, symbol, url = parts
        if not _SLUG.fullmatch(slug) or slug in reserved or slug in chains:
            raise RuntimeError(f"EXTRA_EVM_CHAINS: {slug!r} is not a valid, unused slug (lower-case letters, digits, _).")
        if not _NAME.fullmatch(name) or not _SYMBOL.fullmatch(symbol):
            raise RuntimeError(f"EXTRA_EVM_CHAINS {slug!r}: plain letters/digits only in the name and symbol.")
        if not chain_id_text.isdigit() or int(chain_id_text) <= 0:
            raise RuntimeError(f"EXTRA_EVM_CHAINS {slug!r}: chain_id must be a positive number.")
        if not url.startswith(("http://", "https://")):
            raise RuntimeError(f"EXTRA_EVM_CHAINS {slug!r}: the RPC must be an http(s) URL.")
        chains[slug] = EvmChainConfig(
            slug=slug, name=name, chain_id=int(chain_id_text), rpc_url=url,
            native_symbol=symbol.upper(), confirmations=EXTRA_CHAIN_CONFIRMATIONS,
        )
    return chains


@dataclass(frozen=True)
class EvmChainConfig:
    slug: str
    name: str
    chain_id: int
    rpc_url: str
    native_symbol: str
    confirmations: int


@dataclass(frozen=True)
class Settings:
    bot_token: str
    database_url: str
    master_key: str
    network: str
    neo_n3_rpc: str
    send_limits: dict[str, tuple[Decimal, Decimal]] = field(default_factory=dict)
    evm_chains: dict[str, EvmChainConfig] = field(default_factory=dict)
    solana_rpc: str = ""  # empty = Solana switched off
    solana_poll_seconds: float = 120.0  # pause between passes over all Solana wallets
    event_admin_ids: frozenset = frozenset()  # Telegram ids always allowed to start /rain and /quiz
    airdrop_admin_usernames: tuple = ("btc_mahadi",)  # @usernames (besides the support user) that approve airdrop announcements
    airdrop_admin_ids: frozenset = frozenset()  # Telegram ids that approve airdrop announcements (the support user is always one)
    anthropic_api_key: str = ""  # optional: lets the AI Assistant write answers with Claude (env ANTHROPIC_API_KEY)
    ai_api_key: str = ""  # optional FREE provider key for the AI Assistant (env AI_API_KEY), see AI_PROVIDER
    ai_provider: str = "gemini"  # gemini | groq | openrouter (env AI_PROVIDER)
    ai_model: str = ""  # env AI_MODEL; empty = a sensible default for the provider
    ai_base_url: str = ""  # env AI_BASE_URL; empty = the provider's address
    assistant_model: str = "claude-haiku-5-5"  # env ASSISTANT_MODEL
    assistant_daily_limit: int = 80  # Claude answers per person per day (env ASSISTANT_DAILY_LIMIT); the built-in answer has no limit
    support_extra: tuple = ("btc_mahadi",)  # more @usernames that get their own Support button (env SUPPORT_EXTRA, comma separated)
    backup_password: str = ""  # env BACKUP_PASSWORD; empty = hourly backups off
    backup_recipients: tuple = ("aziz1683", "btc_mahadi")  # @usernames that receive the backup (env BACKUP_RECIPIENTS)
    backup_ids: frozenset = frozenset()  # Telegram ids that receive the backup (env BACKUP_IDS)
    backup_every: int = 3600  # seconds between backups (env BACKUP_EVERY_MINUTES)
    support_username: str = "aziz1683"  # Telegram @username behind the Support button in /help (empty hides it)


def _poll_seconds(text: str, default: float = 120.0, minimum: float = 10.0) -> float:
    """SOLANA_POLL_SECONDS: how often to check Solana wallets. Bad or too small values fall back safely."""
    try:
        value = float(text) if text.strip() else default
    except ValueError:
        return default
    return max(value, minimum)


def _id_set(text: str) -> frozenset:
    """EVENT_ADMINS: comma-separated Telegram user ids. Anything that isn't a number is ignored."""
    return frozenset(int(p) for p in (text or "").replace(" ", "").split(",") if p.lstrip("-").isdigit())


def load_settings() -> Settings:
    warn_duplicate_env_keys()
    master_key = os.getenv("MASTER_KEY", "")
    if not master_key:
        raise RuntimeError(
            "MASTER_KEY is not set. Generate one with: python -m bot.crypto"
        )
    bot_token = os.getenv("BOT_TOKEN", "")
    if not bot_token:
        raise RuntimeError("BOT_TOKEN is not set.")
    network = os.getenv("NETWORK", "testnet")
    if network not in ("testnet", "mainnet"):
        raise RuntimeError("NETWORK must be 'testnet' or 'mainnet'.")
    neo_n3_rpc = os.getenv("NEO_N3_RPC", "")
    if not neo_n3_rpc:
        raise RuntimeError(
            "NEO_N3_RPC is not set. Use a Neo N3 node that runs the RpcNep17Tracker plugin."
        )
    evm_chains: dict[str, EvmChainConfig] = {}
    for slug, (name, main_id, test_id, native, env_var, confirmations) in EVM_CHAIN_SPECS.items():
        url = os.getenv(env_var, "").strip()
        if url:
            evm_chains[slug] = EvmChainConfig(
                slug=slug,
                name=name,
                chain_id=main_id if network == "mainnet" else test_id,
                rpc_url=url,
                native_symbol=native,
                confirmations=confirmations,
            )
    evm_chains.update(parse_extra_chains(os.getenv("EXTRA_EVM_CHAINS", ""), set(EVM_CHAIN_SPECS) | {"neo_n3"}))
    return Settings(
        bot_token=bot_token,
        database_url=os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./walletbot.db"),
        master_key=master_key,
        network=network,
        neo_n3_rpc=neo_n3_rpc,
        send_limits=parse_send_limits(os.getenv("SEND_LIMITS", DEFAULT_SEND_LIMITS)),
        evm_chains=evm_chains,
        solana_rpc=os.getenv("SOLANA_RPC", "").strip(),
        solana_poll_seconds=_poll_seconds(os.getenv("SOLANA_POLL_SECONDS", "")),
        event_admin_ids=_id_set(os.getenv("EVENT_ADMINS", "")),
        airdrop_admin_usernames=tuple(n.strip().lstrip("@").lower() for n in os.getenv("AIRDROP_ADMIN_USERNAMES", "btc_mahadi").split(",") if n.strip()),
        airdrop_admin_ids=_id_set(os.getenv("AIRDROP_ADMINS", "")),
        support_username=os.getenv("SUPPORT_USERNAME", "aziz1683").strip().lstrip("@"),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", "").strip(),
        ai_api_key=os.getenv("AI_API_KEY", "").strip(),
        ai_provider=(os.getenv("AI_PROVIDER", "gemini").strip().lower() or "gemini"),
        ai_model=os.getenv("AI_MODEL", "").strip(),
        ai_base_url=os.getenv("AI_BASE_URL", "").strip(),
        assistant_model=os.getenv("ASSISTANT_MODEL", "claude-haiku-5-5").strip() or "claude-haiku-5-5",
        assistant_daily_limit=int(os.getenv("ASSISTANT_DAILY_LIMIT", "80") or 80),
        backup_password=os.getenv("BACKUP_PASSWORD", "").strip(),
        backup_recipients=tuple(n.strip().lstrip("@").lower() for n in os.getenv("BACKUP_RECIPIENTS", "aziz1683,btc_mahadi").split(",") if n.strip()),
        backup_ids=_id_set(os.getenv("BACKUP_IDS", "")),
        backup_every=max(10, int(os.getenv("BACKUP_EVERY_MINUTES", "60") or 60)) * 60,
        support_extra=tuple(n.strip().lstrip("@") for n in os.getenv("SUPPORT_EXTRA", "btc_mahadi").split(",") if n.strip()),
    )
