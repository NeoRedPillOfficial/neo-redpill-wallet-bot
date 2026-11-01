"""One interface over the supported networks, so the bot's flows (home page,
add token, send) don't care whether they talk to Neo N3 or an EVM chain.

Token "keys": balances are returned as {token_key: amount}. The key of an EVM
native coin is 'native', of an EVM token its lower-case contract address, of a
Neo N3 token its 0x-prefixed lower-case contract hash.
"""
import asyncio
import re
from dataclasses import dataclass

from eth_utils import is_address

from . import evm_send, neo_send, solana_keys, solana_send
from .config import EvmChainConfig
from .evm_rpc import EvmRpc, EvmRpcError
from .neo_rpc import NeoRpc, normalize_hash
from .neo_send import SendError, SendOutcome
from .solana_rpc import SolRpc, SolRpcError
from .service import format_amount

_LABEL_OK = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._\-$+]*")


def clean_label(text: str, max_len: int) -> str | None:
    """Token symbols and names come from untrusted contracts. Accept only plain
    printable ASCII, so nothing odd (look-alike letters, markup) reaches users."""
    text = (text or "").strip()
    if not text or len(text) > max_len or not _LABEL_OK.fullmatch(text):
        return None
    return text


@dataclass(frozen=True)
class TokenInfo:
    symbol: str
    name: str
    decimals: int


@dataclass(frozen=True)
class FeeEstimate:
    """What one transfer costs in the network's own coin (smallest unit). `worst` is the most it can cost."""
    per_transfer: int
    decimals: int
    symbol: str
    worst: int = 0  # a higher cost that only happens sometimes (Solana: the recipient has no token account yet)


def usd_text(amount_raw: int, decimals: int, price) -> str:
    """' (about $0.12)' for an amount of the native coin, or '' if the price is unknown."""
    if price is None:
        return ""
    from decimal import Decimal

    usd = Decimal(amount_raw) / (Decimal(10) ** decimals) * Decimal(price)
    if usd <= 0:
        return ""
    return " (less than $0.01)" if usd < Decimal("0.01") else f" (about ${usd:,.2f})"


def fee_text(est: "FeeEstimate | None", transfers: int = 1, price=None) -> str:
    """One line for a confirmation message, e.g. 'about 0.0004 BNB (0.0002 BNB for each of 2 transfers)'. Empty if unknown."""
    if est is None or est.per_transfer <= 0:
        return ""
    sym = est.symbol
    total = est.per_transfer * max(1, transfers)
    text = f"about <b>{format_amount(total, est.decimals)} {sym}</b>{usd_text(total, est.decimals, price)}"
    if transfers > 1:
        text += f" ({format_amount(est.per_transfer, est.decimals)} {sym} for each of {transfers} transfers)"
    if est.worst > est.per_transfer:
        worst = est.worst * max(1, transfers)
        text += (f"; up to {format_amount(worst, est.decimals)} {sym}{usd_text(worst, est.decimals, price)} if the "
                 "receiver has no token account yet")
    return text


PROBE_TO = "0x0000000000000000000000000000000000000001"  # a fresh address, to price a transfer that creates a balance
N3_FEE_PER_TRANSFER = 2_000_000  # 0.02 GAS (8 decimals): a typical NEP-17 transfer's network + system fee
EVM_FALLBACK_UNITS = {"native": 21_000, "token": 65_000}  # gas units, used when the node can't estimate


class ChainOps:
    slug: str
    name: str
    key_type: str  # which wallet key it uses: 'neo_n3', 'evm' or 'solana'
    native_symbol: str

    def is_valid_address(self, address: str) -> bool: ...
    def normalize_contract(self, text: str) -> str: ...
    def token_key(self, token) -> str: ...
    async def balances(self, address: str, tokens: list) -> dict[str, int | None]: ...
    async def token_info(self, contract: str) -> TokenInfo: ...
    async def max_sendable(self, token, address: str, balances: dict) -> int: ...
    async def check_send(self, token, address: str, to_address: str, raw: int, balances: dict) -> None: ...
    async def send(self, private_key: bytes, token, from_address: str, to_address: str, raw: int) -> SendOutcome: ...

    async def estimate_fee(self, token, address: str) -> "FeeEstimate | None":
        """What one transfer of this token costs the sender, or None if it can't be worked out right now."""
        return None

    def _checked_info(self, symbol: str, name: str, decimals: int) -> TokenInfo:
        clean_symbol = clean_label(symbol, 20)
        clean_name = clean_label(name, 60) or clean_symbol
        if clean_symbol is None:
            raise SendError(
                "That token's symbol contains unusual characters, so I won't add it."
            )
        return TokenInfo(clean_symbol, clean_name, decimals)


class N3Ops(ChainOps):
    slug = "neo_n3"
    name = "Neo N3"
    key_type = "neo_n3"
    native_symbol = "GAS"

    def __init__(self, rpc: NeoRpc, sender):
        self.rpc = rpc
        self._sender = sender

    def is_valid_address(self, address: str) -> bool:
        return neo_send.is_valid_address(address)

    def normalize_contract(self, text: str) -> str:
        text = text.strip()
        if not re.fullmatch(r"(0x)?[0-9a-fA-F]{40}", text):
            raise SendError("A Neo N3 token contract is 40 hex characters, like 0x1234…abcd.")
        return normalize_hash(text)

    def token_key(self, token) -> str:
        return normalize_hash(token.contract)

    async def balances(self, address: str, tokens: list) -> dict[str, int | None]:
        return await self.rpc.get_balances(address)

    async def token_info(self, contract: str) -> TokenInfo:
        try:
            symbol, decimals = await self.rpc.get_token_info(contract)
        except Exception:
            raise SendError("I couldn't read that contract as a NEP-17 token on Neo N3.")
        return self._checked_info(symbol, symbol, decimals)

    async def max_sendable(self, token, address: str, balances: dict) -> int:
        return neo_send.max_sendable(token.contract, balances)

    async def check_send(self, token, address, to_address, raw, balances) -> None:
        neo_send.check_balance(token.symbol, token.contract, raw, balances)

    async def estimate_fee(self, token, address: str):
        return FeeEstimate(N3_FEE_PER_TRANSFER, 8, "GAS")

    async def send(self, private_key, token, from_address, to_address, raw) -> SendOutcome:
        return await neo_send.perform_send(
            rpc=self.rpc,
            sender=self._sender,
            private_key=private_key,
            token_contract=token.contract,
            to_address=to_address,
            amount=raw,
        )


class EvmOps(ChainOps):
    key_type = "evm"

    def __init__(self, cfg: EvmChainConfig, rpc: EvmRpc):
        self.cfg = cfg
        self.rpc = rpc
        self.slug = cfg.slug
        self.name = cfg.name
        self.native_symbol = cfg.native_symbol

    def is_valid_address(self, address: str) -> bool:
        address = address.strip()
        return bool(re.fullmatch(r"0x[0-9a-fA-F]{40}", address)) and is_address(address)

    def normalize_contract(self, text: str) -> str:
        text = text.strip()
        if not self.is_valid_address(text):
            raise SendError(
                "That is not a valid contract address. It should look like 0x followed by 40 characters."
            )
        return text.lower()

    def token_key(self, token) -> str:
        return evm_send.NATIVE if token.contract == evm_send.NATIVE else token.contract.lower()

    async def balances(self, address: str, tokens: list) -> dict[str, int | None]:
        async def one(token):
            key = self.token_key(token)
            try:
                if key == evm_send.NATIVE:
                    return key, await self.rpc.get_balance(address)
                return key, await self.rpc.erc20_balance(key, address)
            except Exception:
                return key, None  # shown as "unavailable" for just this token

        results = await asyncio.gather(*(one(t) for t in tokens))
        out = dict(results)
        if out and all(v is None for v in out.values()):
            raise EvmRpcError("node unreachable")  # every lookup failed: treat the chain as down
        return out

    async def token_info(self, contract: str) -> TokenInfo:
        try:
            symbol, name, decimals = await self.rpc.token_info(contract)
        except Exception:
            raise SendError(
                f"I couldn't read that contract as an ERC-20 token on {self.name}. "
                "Check the address and the network."
            )
        return self._checked_info(symbol, name, decimals)

    async def max_sendable(self, token, address: str, balances: dict) -> int:
        key = self.token_key(token)
        held = balances.get(key) or 0
        if key != evm_send.NATIVE:
            return held
        # Native coin: keep enough back to pay this transfer's own fee.
        q = await evm_send.quote(self.rpc, address, address, evm_send.NATIVE, 1)
        return max(0, held - q.fee)

    async def check_send(self, token, address, to_address, raw, balances) -> None:
        key = self.token_key(token)
        held = balances.get(key)
        if held is None:
            raise SendError("I can't read that balance right now. Try again in a moment.")
        if raw > held:
            raise SendError(f"You don't have enough {token.symbol}.")
        contract = evm_send.NATIVE if key == evm_send.NATIVE else key
        q = await evm_send.quote(self.rpc, address, to_address, contract, raw)  # also a dry run
        native_balance = balances.get(evm_send.NATIVE)
        if native_balance is None:
            raise SendError(f"I can't read your {self.native_symbol} balance right now.")
        needed = q.fee + (raw if contract == evm_send.NATIVE else 0)
        if native_balance < needed:
            fee_text = format_amount(q.fee, 18)
            raise SendError(
                f"You need about {fee_text} {self.native_symbol} in this wallet to pay the "
                f"network fee on {self.name}."
                + (" Send a smaller amount." if contract == evm_send.NATIVE else " Deposit some first.")
            )

    async def estimate_fee(self, token, address: str):
        key = self.token_key(token)
        contract = evm_send.NATIVE if key == evm_send.NATIVE else key
        try:
            q = await evm_send.quote(self.rpc, address, PROBE_TO, contract, 1)
            return FeeEstimate(q.fee, 18, self.native_symbol)
        except Exception:
            pass
        try:  # the node refused the dry run (for example no tokens yet): use a typical size of transfer
            price = await self.rpc.gas_price()
            units = EVM_FALLBACK_UNITS["native" if contract == evm_send.NATIVE else "token"]
            return FeeEstimate(units * price * evm_send.PRICE_BUFFER_PERCENT // 100, 18, self.native_symbol)
        except Exception:
            return None

    async def send(self, private_key, token, from_address, to_address, raw) -> SendOutcome:
        contract = evm_send.NATIVE if token.contract == evm_send.NATIVE else token.contract.lower()
        return await evm_send.perform_send(
            rpc=self.rpc,
            chain_id=self.cfg.chain_id,
            private_key=private_key,
            from_addr=from_address,
            to_addr=to_address,
            token_contract=contract,
            raw=raw,
        )


async def verify_evm_chain(ops: EvmOps) -> str | None:
    """None if the node is reachable and is the expected chain, else a reason."""
    try:
        actual = await ops.rpc.chain_id()
    except Exception as exc:
        return f"RPC not reachable ({type(exc).__name__}: {exc})"
    if actual != ops.cfg.chain_id:
        return f"RPC reports chain id {actual}, expected {ops.cfg.chain_id}"
    return None


class SolanaOps(ChainOps):
    """Solana: SOL plus SPL tokens of the classic Token program."""

    slug = "solana"
    name = "Solana"
    key_type = "solana"
    native_symbol = "SOL"

    def __init__(self, rpc: SolRpc):
        self.rpc = rpc

    def is_valid_address(self, address: str) -> bool:
        return solana_keys.is_valid_address(address)

    def normalize_contract(self, text: str) -> str:
        text = text.strip()
        if not solana_keys.is_valid_address(text):
            raise SendError("A Solana token (mint) address is 32 to 44 letters and digits, like EPjFWd…Dt1v.")
        return text  # base58 is case-sensitive: never change the case

    def token_key(self, token) -> str:
        return solana_send.NATIVE if token.contract == solana_send.NATIVE else token.contract

    async def balances(self, address: str, tokens: list) -> dict[str, int | None]:
        native = await self.rpc.get_balance(address)
        spl = await self.rpc.token_balances(address)
        return {
            self.token_key(t): native if self.token_key(t) == solana_send.NATIVE else spl.get(self.token_key(t), 0)
            for t in tokens
        }

    async def token_info(self, contract: str) -> TokenInfo:
        try:
            decimals, program = await self.rpc.mint_info(contract)
        except Exception:
            raise SendError("I couldn't read that address as a token on Solana. Check it and try again.")
        if program != solana_keys.b58encode(solana_keys.TOKEN_PROGRAM):
            raise SendError("That token uses Solana's newer Token-2022 program, which this bot doesn't support yet.")
        try:
            account = await self.rpc.get_account(solana_keys.b58encode(solana_keys.metadata_address(solana_keys.b58decode(contract))))
            name, symbol = solana_keys.parse_metadata(account["data"]) if account else ("", "")
        except Exception:
            name, symbol = "", ""
        if not symbol:
            raise SendError("That token has no readable name and symbol on-chain, so I won't add it.")
        return self._checked_info(symbol, name or symbol, decimals)

    async def max_sendable(self, token, address: str, balances: dict) -> int:
        held = balances.get(self.token_key(token)) or 0
        if self.token_key(token) != solana_send.NATIVE:
            return held
        return max(0, held - solana_keys.LAMPORTS_PER_SIGNATURE)

    async def check_send(self, token, address, to_address, raw, balances) -> None:
        key = self.token_key(token)
        held = balances.get(key)
        if held is None:
            raise SendError("I can't read that balance right now. Try again in a moment.")
        if raw > held:
            raise SendError(f"You don't have enough {token.symbol}.")
        native = balances.get(solana_send.NATIVE)
        if native is None:
            raise SendError("I can't read your SOL balance right now.")
        mint = None if key == solana_send.NATIVE else key
        try:
            plan = await solana_send.plan_transfer(self.rpc, address, to_address, mint, token.decimals, raw)
            if mint is None:
                recipient_balance = await self.rpc.get_balance(to_address)
                try:
                    minimum = await self.rpc.min_balance_for_rent(0)
                except Exception:
                    minimum = solana_send.FALLBACK_RENT_SYSTEM_ACCOUNT
                if recipient_balance == 0 and raw < minimum:
                    raise SendError(
                        f"A brand-new Solana account must receive at least {format_amount(minimum, 9)} SOL."
                    )
        except SolRpcError:
            raise SendError("I couldn't check that with the network. Try again in a moment.")
        needed = plan.fee_lamports + (raw if mint is None else 0)
        if native < needed:
            raise SendError(
                f"You need about {format_amount(needed, 9)} SOL in this wallet"
                + (" (this includes a one-time cost to open the recipient's token account)" if plan.creates_account else " to pay the network fee")
                + ". " + ("Send a smaller amount." if mint is None else "Deposit some first.")
            )

    async def estimate_fee(self, token, address: str):
        base = solana_keys.LAMPORTS_PER_SIGNATURE
        if self.token_key(token) == solana_send.NATIVE:
            return FeeEstimate(base, 9, "SOL")
        try:
            rent = await self.rpc.min_balance_for_rent(solana_keys.TOKEN_ACCOUNT_SIZE)
        except Exception:
            rent = solana_send.FALLBACK_RENT_TOKEN_ACCOUNT
        return FeeEstimate(base, 9, "SOL", worst=base + rent)

    async def send(self, private_key, token, from_address, to_address, raw) -> SendOutcome:
        key = self.token_key(token)
        return await solana_send.perform_send(
            rpc=self.rpc,
            private_key=private_key,
            from_addr=from_address,
            to_addr=to_address,
            mint=None if key == solana_send.NATIVE else key,
            decimals=token.decimals,
            raw=raw,
        )


async def verify_solana(ops: "SolanaOps", network: str) -> str | None:
    """None if the node answers and is the expected cluster (the bot's 'testnet' means Solana devnet)."""
    expected = SOLANA_GENESIS["mainnet" if network == "mainnet" else "devnet"]
    try:
        actual = await ops.rpc.genesis_hash()
    except Exception as exc:
        return f"RPC not reachable ({type(exc).__name__}: {exc})"
    if actual != expected:
        return f"RPC is not the expected Solana cluster for NETWORK={network} (genesis {actual})"
    return None


SOLANA_GENESIS = {
    "mainnet": "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d",
    "devnet": "EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG",
}
