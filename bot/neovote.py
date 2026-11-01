"""Neo N3 governance inside the bot: vote for a candidate and claim the GAS it earns.

Reading (candidates, your vote, GAS waiting) is plain JSON-RPC and is parsed by pure functions that tests can feed
sample answers. Writing (the vote and the claim) uses the neo-mamba SDK the same careful way ordinary sends do: a dry run
on a test-only signer first, then the real transaction, and only read-only steps are ever retried.

How it works on-chain (see developers.neo.org, N3 governance):
* NEO holders vote for registered candidates. The top 21 by votes are the committee; the top 7 are consensus nodes.
* Voters earn GAS only while the candidate they voted for is elected (a bigger share for the top 7).
* The earned GAS is paid out when the account's NEO balance or vote changes. A transfer of 0 NEO to yourself is the
  simplest way to trigger that payout ("claim").
"""
import base64
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import base58

from . import neo_send
from .neo_rpc import NeoRpc, RpcError
from .neo_send import SendError, SendOutcome, retry_before_sending

log = logging.getLogger("walletbot.vote")

NEO_HASH = "0xef4073a0f2b305a38ec4050e4d3d28bc40ea63f5"
CONSENSUS_NODES = 7
COMMITTEE_SIZE = 21
PAGE_SIZE = 7
CACHE_SECONDS = 60
NAMES_FILE = Path(__file__).parent / "assets" / "candidates.json"  # optional {"<public key>": "Name"}


@dataclass(frozen=True)
class Candidate:
    pubkey: str  # 33-byte compressed key as 66 hex characters
    votes: int  # whole NEO voted for it
    rank: int = 0  # 1 = most votes
    role: str = "candidate"  # consensus (top 7) | committee (8-21) | candidate (not elected)


@dataclass(frozen=True)
class Standing:
    neo: int  # whole NEO in the wallet
    vote_to: str | None  # public key the wallet votes for, if any
    unclaimed: int  # GAS waiting, smallest unit (8 decimals)


# ----------------------------------------------------------------- pure helpers
def address_hash160(address: str) -> str:
    """Neo N3 address -> 0x… script hash (the form the node's RPC wants)."""
    raw = base58.b58decode_check(address.strip())
    if len(raw) != 21:
        raise ValueError("not a Neo N3 address")
    return "0x" + raw[1:][::-1].hex()


def short_key(pubkey: str) -> str:
    return f"{pubkey[:6]}…{pubkey[-4:]}"


def votes_text(votes: int) -> str:
    if votes >= 1_000_000:
        return f"{votes / 1_000_000:.2f}".rstrip("0").rstrip(".") + "M"
    if votes >= 1_000:
        return f"{votes / 1_000:.1f}".rstrip("0").rstrip(".") + "k"
    return str(votes)


def tier(rank: int) -> str:
    """'consensus' (rank 1-7), 'committee' (8-21) or 'none'. Rank 0 means not found."""
    if 1 <= rank <= CONSENSUS_NODES:
        return "consensus"
    if CONSENSUS_NODES < rank <= COMMITTEE_SIZE:
        return "committee"
    return "none"


TOTAL_NEO = 100_000_000
BLOCKS_PER_DAY = 5760  # a block about every 15 seconds
DEFAULT_GAS_PER_BLOCK = 5 * 10**8  # the starting value in Neo's docs, used only if the node does not answer
VOTER_SHARE = 0.8  # of the GAS made per block, goes to voters
VOTER_SHARES_TOTAL = 28  # 7 consensus nodes x 2 + 14 other committee members x 1


def role_for(rank: int) -> str:
    return {"consensus": "consensus", "committee": "committee"}.get(tier(rank), "candidate")


def assign_roles(candidates: list[Candidate], committee: set[str], validators: set[str]) -> list[Candidate]:
    """Marks who is a consensus node / committee member. The node's own lists win; by rank is the fallback."""
    out = []
    for c in candidates:
        if validators or committee:
            role = "consensus" if c.pubkey in validators else "committee" if c.pubkey in committee else "candidate"
        else:
            role = role_for(c.rank)
        out.append(Candidate(c.pubkey, c.votes, c.rank, role))
    return out


def parse_keys(result) -> set[str]:
    """From getcommittee (a list of keys) or getnextblockvalidators (a list of {publickey})."""
    keys = set()
    for item in result or []:
        key = item.get("publickey") if isinstance(item, dict) else item
        if isinstance(key, str) and len(key) == 66:
            keys.add(key.lower())
    return keys


def parse_gas_per_block(result: dict) -> int:
    return int(str(_first_stack_item(result).get("value") or "0"))


def total_votes(candidates: list[Candidate]) -> int:
    return sum(c.votes for c in candidates)


def vote_percent(candidate: Candidate, candidates: list[Candidate]) -> float:
    total = total_votes(candidates)
    return candidate.votes * 100 / total if total else 0.0


def estimate_daily_gas(candidate: Candidate, gas_per_block: int, my_neo: int = 100) -> int:
    """About how much GAS (smallest unit) `my_neo` NEO would earn a day by voting for this candidate. 0 if not elected."""
    if candidate.role == "candidate":
        return 0
    shares = 2 if candidate.role == "consensus" else 1
    pool_day = gas_per_block * VOTER_SHARE * BLOCKS_PER_DAY * shares / VOTER_SHARES_TOTAL
    return int(pool_day * my_neo / max(candidate.votes + my_neo, 1))


def candidate_address(pubkey: str) -> str:
    """The Neo N3 address of a candidate's public key (single-signature account)."""
    import hashlib
    script = bytes([0x0C, 0x21]) + bytes.fromhex(pubkey) + bytes([0x41, 0x56, 0xE7, 0xB3, 0x27])
    sha = hashlib.sha256(script).digest()
    try:
        h = hashlib.new("ripemd160", sha).digest()
    except ValueError:
        from Crypto.Hash import RIPEMD160
        h = RIPEMD160.new(sha).digest()
    payload = bytes([neo_send.NEO_N3_ADDRESS_VERSION]) + h
    return base58.b58encode_check(payload).decode()


def load_names(path: Path = NAMES_FILE) -> dict[str, dict]:
    """Optional file {"<public key>": "Name"} or {"<public key>": {"name": "...", "location": "..."}}."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out = {}
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, str):
                value = {"name": value}
            if isinstance(value, dict):
                out[str(key).lower()] = {k: str(v)[:30] for k, v in value.items() if k in ("name", "location") and v}
    return out


def _entry(candidate: Candidate, names: dict | None) -> dict:
    """The names file may list a whole public key or just its first characters (6 or more)."""
    key = candidate.pubkey.lower()
    names = names or {}
    if key in names:
        return names[key] or {}
    for short, entry in names.items():
        if len(short) >= 6 and key.startswith(short):
            return entry or {}
    return {}


def label(candidate: Candidate, names: dict | None = None) -> str:
    return _entry(candidate, names).get("name") or f"Candidate #{candidate.rank}"


def location(candidate: Candidate, names: dict | None = None) -> str:
    return _entry(candidate, names).get("location") or ""


def parse_candidates(result) -> list[Candidate]:
    """From getcandidates: most votes first, ranked from 1. Note the node's "active" flag only means "one of the
    consensus nodes right now", so it must NOT be used to drop candidates (that would leave only 7)."""
    found = []
    for item in result or []:
        key = str(item.get("publickey") or "").lower()
        if len(key) != 66:
            continue
        try:
            votes = int(str(item.get("votes") or "0"))
        except ValueError:
            continue
        found.append((key, votes))
    found.sort(key=lambda kv: (-kv[1], kv[0]))
    return [Candidate(key, votes, i + 1) for i, (key, votes) in enumerate(found)]


def _first_stack_item(result: dict) -> dict:
    if result.get("state") != "HALT":
        raise RpcError(f"script faulted: {result.get('exception') or result.get('state')}")
    stack = result.get("stack") or []
    if not stack:
        raise RpcError("empty answer")
    return stack[0]


def parse_account_state(result: dict) -> tuple[int, str | None]:
    """From invokefunction NEO getAccountState: (NEO balance, public key voted for or None)."""
    item = _first_stack_item(result)
    if item.get("type") in ("Any", None) or not isinstance(item.get("value"), list):
        return 0, None  # an account that never held NEO has no state
    fields = item["value"]
    balance = int(str(fields[0].get("value") or "0")) if fields else 0
    vote = None
    if len(fields) > 2 and fields[2].get("type") == "ByteString" and fields[2].get("value"):
        raw = base64.b64decode(fields[2]["value"])
        if len(raw) == 33:
            vote = raw.hex()
    return balance, vote


def parse_unclaimed(result: dict) -> int:
    """From getunclaimedgas."""
    return max(0, int(str(result.get("unclaimed") or "0")))


# ------------------------------------------------------------------ node reads
_cache: dict = {"time": 0.0, "list": []}


async def get_candidates(rpc: NeoRpc, fresh: bool = False) -> list[Candidate]:
    if not fresh and _cache["list"] and time.monotonic() - _cache["time"] < CACHE_SECONDS:
        return _cache["list"]
    found = parse_candidates(await rpc.call("getcandidates"))
    committee, validators = set(), set()
    try:
        committee = parse_keys(await rpc.call("getcommittee"))
        validators = parse_keys(await rpc.call("getnextblockvalidators"))
    except Exception:
        log.warning("Could not read the committee, using the ranking instead", exc_info=True)
    found = assign_roles(found, committee, validators)
    if found:
        _cache["time"], _cache["list"] = time.monotonic(), found
    return found


async def get_gas_per_block(rpc: NeoRpc) -> int:
    try:
        return parse_gas_per_block(await rpc.call("invokefunction", [NEO_HASH, "getGasPerBlock", []]))
    except Exception:
        log.warning("Could not read the GAS per block", exc_info=True)
        return DEFAULT_GAS_PER_BLOCK


async def get_standing(rpc: NeoRpc, address: str) -> Standing:
    account = {"type": "Hash160", "value": address_hash160(address)}
    state = await rpc.call("invokefunction", [NEO_HASH, "getAccountState", [account]])
    neo, vote = parse_account_state(state)
    unclaimed = 0
    if neo:
        try:
            unclaimed = parse_unclaimed(await rpc.call("getunclaimedgas", [address]))
        except Exception:
            log.warning("Could not read the unclaimed GAS", exc_info=True)
    return Standing(neo, vote, unclaimed)


def find(candidates: list[Candidate], pubkey: str | None) -> Candidate | None:
    if not pubkey:
        return None
    return next((c for c in candidates if c.pubkey == pubkey.lower()), None)


# ---------------------------------------------------------------- transactions
def _ecpoint(pubkey: str):
    try:
        from neo3.core.cryptography import ECPoint
    except ImportError:
        from neo3.core import cryptography
        ECPoint = cryptography.ECPoint
    return ECPoint.deserialize_from_bytes(bytes.fromhex(pubkey))


def make_mamba_voter(rpc_url: str):
    """Returns (vote, claim): async functions (private_key, ...) -> tx hash. Each does a dry run before spending anything."""

    async def _run(private_key: bytes, build_call, fail_text: str) -> str:
        from neo3.api.helpers.signing import sign_with_account
        from neo3.api.wrappers import ChainFacade
        from neo3.network.payloads.verification import Signer
        from neo3.wallet.account import Account

        account = Account.from_private_key(private_key)

        async def dry_run():
            dry_facade = ChainFacade(rpc_host=rpc_url)
            dry_facade.add_test_signer(Signer(account.script_hash))
            return await dry_facade.test_invoke(build_call(account))

        dry = await retry_before_sending(dry_run)
        if "HALT" not in str(dry.state).upper() or dry.result is not True:
            raise SendError(fail_text)

        async def build_and_broadcast():
            facade = ChainFacade(rpc_host=rpc_url)
            facade.add_signer(sign_with_account(account), Signer(account.script_hash))
            return await facade.invoke_fast(build_call(account))

        return str(await retry_before_sending(build_and_broadcast))

    async def vote(private_key: bytes, pubkey: str) -> str:
        from neo3.api.wrappers import NeoToken

        point = _ecpoint(pubkey)
        return await _run(
            private_key,
            lambda account: NeoToken().candidate_vote(account.address, point),
            "The network would refuse this vote (the candidate may no longer be registered). Nothing was sent.",
        )

    async def claim(private_key: bytes) -> str:
        from neo3.api.wrappers import NEP17Contract
        from neo3.core import types

        contract = NEP17Contract(types.UInt160.from_string(NEO_HASH))
        return await _run(
            private_key,
            lambda account: contract.transfer(account.address, account.address, 0),
            "The network would refuse this claim. Nothing was sent.",
        )

    return vote, claim


# ---------------------------------------------------------------- vote wallets
GAS_HASH = neo_send.GAS_HASH
FEE_TOPUP = 10_000_000  # 0.1 GAS moved into a vote wallet so it can pay its own fees
MIN_SLOT_GAS = 3_000_000  # a vote wallet below 0.03 GAS gets a top-up before it acts
SLOT_RESERVE = 5_000_000  # claimed GAS is swept to the main wallet, leaving this much for the next fees
WITHDRAW_RESERVE = 3_000_000  # when withdrawing everything, this much GAS stays behind to pay the last transfer
MIN_SWEEP = 1_000_000  # do not bother sweeping less than 0.01 GAS


@dataclass(frozen=True)
class Account:
    address: str
    key: bytes


@dataclass(frozen=True)
class Slot:
    """A vote wallet as it is on-chain right now."""
    id: int
    address: str
    candidate: str
    standing: Standing
    gas: int  # GAS in the wallet, smallest unit


@dataclass
class StepsResult:
    ok: bool
    failed_step: str = ""
    reason: str = ""
    done: list = None  # [(label, SendOutcome)]


async def read_slot(rpc: NeoRpc, row_id: int, address: str, candidate: str) -> Slot:
    standing = await get_standing(rpc, address)
    balances = await rpc.get_balances(address)
    return Slot(row_id, address, candidate, standing, balances.get(GAS_HASH, 0))


async def run_steps(rpc: NeoRpc, steps) -> StepsResult:
    """Runs [(label, async fn -> tx hash or None)] one after another. A step that returns None is skipped.
    Each transaction must be CONFIRMED on-chain before the next begins, and the first problem stops everything,
    so the money is never left halfway without the caller knowing exactly where."""
    done = []
    for label, step in steps:
        try:
            tx_hash = await step()
        except SendError as exc:
            return StepsResult(False, label, str(exc), done)
        except Exception as exc:
            log.exception("Governance step failed: %s", label)
            return StepsResult(False, label, f"the network did not accept it ({(str(exc) or type(exc).__name__)[:120]})", done)
        if tx_hash is None:
            continue
        outcome = await neo_send.wait_for_outcome(rpc, neo_send.normalize_hash(tx_hash))
        done.append((label, outcome))
        if outcome.status != "confirmed":
            detail = "not seen on-chain yet, check before trying again" if outcome.status == "unknown" else (outcome.detail or "rejected on-chain")
            return StepsResult(False, label, detail, done)
    return StepsResult(True, "", "", done)


def _topup_step(rpc, transfer, main: Account, slot: Account, amount: int = FEE_TOPUP):
    async def step():
        gas = (await rpc.get_balances(slot.address)).get(GAS_HASH, 0)
        if gas >= MIN_SLOT_GAS:
            return None
        return await transfer(main.key, GAS_HASH, slot.address, amount)
    return ("Sending a little GAS for fees to the vote wallet", step)


def _sweep_step(rpc, transfer, slot: Account, main: Account, reserve: int):
    async def step():
        gas = (await rpc.get_balances(slot.address)).get(GAS_HASH, 0)
        amount = gas - reserve
        if amount < MIN_SWEEP:
            return None
        return await transfer(slot.key, GAS_HASH, main.address, amount)
    return ("Moving the GAS to your main wallet", step)


def vote_steps(rpc, transfer, vote, main: Account, slot: Account, candidate: str, amount: int):
    """Vote with `amount` NEO: fee GAS to the vote wallet, the NEO to the vote wallet, then the vote itself."""
    async def move():
        return await transfer(main.key, NEO_HASH, slot.address, amount)

    async def cast():
        if (await get_standing(rpc, slot.address)).vote_to == candidate.lower():
            return None  # this wallet already votes for the candidate: the added NEO simply joins that vote
        return await vote(slot.key, candidate)

    return [_topup_step(rpc, transfer, main, slot), (f"Moving {amount} NEO to the vote wallet", move), ("Casting the vote", cast)]


def finish_vote_steps(rpc, transfer, vote, main: Account, slot: Account, candidate: str):
    """Repeat the last part of a vote that stopped halfway: fee GAS if needed, then the vote."""
    async def cast():
        if (await get_standing(rpc, slot.address)).vote_to == candidate.lower():
            return None
        return await vote(slot.key, candidate)

    return [_topup_step(rpc, transfer, main, slot), ("Casting the vote", cast)]


def claim_steps(rpc, transfer, main: Account, owner: Account, is_main: bool):
    """Collect the GAS that has built up. For a vote wallet the GAS is then moved to the main wallet."""
    async def claim():
        return await transfer(owner.key, NEO_HASH, owner.address, 0)  # a 0 NEO transfer to yourself pays out the GAS

    steps = [] if is_main else [_topup_step(rpc, transfer, main, owner, FEE_TOPUP // 2)]
    steps.append(("Collecting the GAS", claim))
    if not is_main:
        steps.append(_sweep_step(rpc, transfer, owner, main, SLOT_RESERVE))
    return steps


def withdraw_steps(rpc, transfer, main: Account, slot: Account):
    """Take all the NEO (and the GAS) out of a vote wallet, back to the main wallet. This ends that vote."""
    async def move_back():
        neo = (await get_standing(rpc, slot.address)).neo
        if neo <= 0:
            return None
        return await transfer(slot.key, NEO_HASH, main.address, neo)

    return [_topup_step(rpc, transfer, main, slot, FEE_TOPUP // 2), ("Moving the NEO back to your main wallet", move_back),
            _sweep_step(rpc, transfer, slot, main, WITHDRAW_RESERVE)]


async def run_tx(rpc: NeoRpc, send_fn, *args) -> SendOutcome:
    """Broadcast with `send_fn(*args)`, then watch the chain for the result (same rules as an ordinary send)."""
    try:
        tx_hash = await send_fn(*args)
    except SendError:
        raise
    except Exception as exc:
        log.exception("Governance transaction failed to send")
        raise SendError(
            "The network did not accept the transaction. Nothing was changed. "
            f"({(str(exc) or type(exc).__name__)[:150]})"
        )
    return await neo_send.wait_for_outcome(rpc, neo_send.normalize_hash(tx_hash))
