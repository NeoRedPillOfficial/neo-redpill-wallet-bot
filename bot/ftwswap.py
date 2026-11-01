"""Trading on FTW Swap (Neo N3) from the bot's own wallet.

What is known, and from where: one real FTW Swap transaction made from this wallet was decoded. It calls the swap contract
0x997ced5777a3f66485d66828bda3864b8c8bdf95, method "swapB", with the arguments
    (sender, token you pay, token you receive, exact amount you receive, most you are willing to pay, deadline in milliseconds)
and the contract reports the result in a "Swap" event: (sender, token paid, token received, amount paid, amount received, fee).
So a trade here is "I want exactly this much of the token; I will pay at most that much".

Because the swap method fixes the amount you RECEIVE, a quote for "spend X" is found by asking the node to dry-run the
swap (a read-only simulation, nothing is sent) with different receive amounts until the price just fits inside X.
Every real swap is dry-run again by the node first; anything unexpected stops it before a fee is spent.
"""
import asyncio
import logging
import time
from dataclasses import dataclass
from decimal import Decimal

from . import neo_send
from .neo_rpc import normalize_hash

log = logging.getLogger("walletbot.ftw")

SWAP_CONTRACT = "0x997ced5777a3f66485d66828bda3864b8c8bdf95"
METHOD = "swapB"
FEE = Decimal("0.0025")  # FTW's documented trading fee
DEADLINE_SECONDS = 600
GAS_HASH = neo_send.GAS_HASH
GAS_RESERVE = 30_000_000  # 0.3 GAS kept back when spending GAS, for the transaction's own fees
_HUGE = 10 ** 30
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def script_hash(address: str) -> str:
    """The 0x… script hash of a Neo N3 address (the 20 bytes inside it, written back to front)."""
    n = 0
    for ch in address.strip():
        n = n * 58 + B58.index(ch)
    raw = n.to_bytes(25, "big")
    return "0x" + raw[1:21][::-1].hex()


def _h160(value: str) -> dict:
    return {"type": "Hash160", "value": normalize_hash(value)}


def _int(value: int) -> dict:
    return {"type": "Integer", "value": str(int(value))}


def build_args(sender: str, token_in: str, token_out: str, amount_out: int, max_in: int, deadline_ms: int) -> list[dict]:
    """The argument list of swapB, in the order the real transaction used."""
    return [_h160(sender), _h160(token_in), _h160(token_out), _int(amount_out), _int(max_in), _int(deadline_ms)]


def _signers(sender: str, token_in: str, token_out: str) -> list[dict]:
    return [{"account": normalize_hash(sender), "scopes": "CalledByEntry,CustomContracts",
             "allowedcontracts": [SWAP_CONTRACT, normalize_hash(token_in), normalize_hash(token_out)]}]


@dataclass(frozen=True)
class SwapResult:
    amount_in: int
    amount_out: int
    fee: int
    system_fee: int  # GAS (8 decimals) the node says running it costs


def parse_simulation(result) -> SwapResult | None:
    """The FTW "Swap" event of a successful dry run, or None if it failed or did not swap."""
    if not isinstance(result, dict) or "HALT" not in str(result.get("state", "")).upper():
        return None
    for note in result.get("notifications") or []:
        if normalize_hash(str(note.get("contract", ""))) != SWAP_CONTRACT or note.get("eventname") != "Swap":
            continue
        try:
            values = note["state"]["value"]
            nums = [int(v["value"]) for v in values[3:6]]
            return SwapResult(nums[0], nums[1], nums[2], int(str(result.get("gasconsumed", "0")) or 0))
        except (KeyError, ValueError, TypeError, IndexError):
            return None
    return None


class NodeUnavailable(Exception):
    """The node could not be asked (as opposed to the node answering that the swap would fail)."""


async def simulate(rpc, sender: str, token_in: str, token_out: str, amount_out: int, max_in: int) -> SwapResult | None:
    """Read-only dry run on the node. None when the node says the swap would fail (no pool, too much, or over the price
    limit). Raises NodeUnavailable when the node could not be asked: that must never be read as "does not fit"."""
    last = None
    for attempt in range(3):
        deadline_ms = int((time.time() + DEADLINE_SECONDS) * 1000)
        params = [SWAP_CONTRACT, METHOD, build_args(sender, token_in, token_out, amount_out, max_in, deadline_ms),
                  _signers(sender, token_in, token_out)]
        try:
            return parse_simulation(await rpc.call("invokefunction", params))
        except Exception as exc:
            last = exc
            log.warning("FTW simulation call failed (try %s): %r", attempt + 1, exc)
            await asyncio.sleep(0.4)
    raise NodeUnavailable(repr(last))


@dataclass(frozen=True)
class Quote:
    buying: bool  # True: paying GAS for the token
    token_in: str
    token_out: str
    in_symbol: str
    out_symbol: str
    in_decimals: int
    out_decimals: int
    amount_in_raw: int  # what the node says it costs right now
    amount_out_raw: int  # what you get, exactly
    fee_raw: int  # FTW's fee, in the paid token (already inside amount_in)
    max_in_raw: int  # the most you will pay (slippage)
    impact_pct: Decimal
    system_fee_raw: int

    @property
    def pay(self) -> Decimal:
        return Decimal(self.amount_in_raw) / Decimal(10) ** self.in_decimals

    @property
    def receive(self) -> Decimal:
        return Decimal(self.amount_out_raw) / Decimal(10) ** self.out_decimals

    @property
    def max_pay(self) -> Decimal:
        return Decimal(self.max_in_raw) / Decimal(10) ** self.in_decimals

    @property
    def fee(self) -> Decimal:
        return Decimal(self.fee_raw) / Decimal(10) ** self.in_decimals

    @property
    def system_fee(self) -> Decimal:
        return Decimal(self.system_fee_raw) / Decimal(10) ** 8


def budget_within_balance(spend_raw: int, balance_raw: int | None, slippage_pct: Decimal) -> int:
    """The amount to quote so that "the most you pay" (price + slippage) can never be more than the wallet holds."""
    if not balance_raw or balance_raw <= 0:
        return spend_raw
    room = int((Decimal(balance_raw) / (Decimal(1) + slippage_pct / Decimal(100))).to_integral_value(rounding="ROUND_FLOOR"))
    return max(0, min(spend_raw, room))


def budget_for(spend_raw: int, slippage_pct: Decimal) -> int:
    """The amount to quote so that the MOST you can pay (price + slippage) is never more than `spend_raw`:
    typing 1000 means "I will not pay more than 1000, whatever happens"."""
    if spend_raw <= 0:
        return 0
    budget = int((Decimal(spend_raw) / (Decimal(1) + slippage_pct / Decimal(100))).to_integral_value(rounding="ROUND_FLOOR"))
    while budget > 0 and max_in_for(budget, slippage_pct) > spend_raw:  # rounding up must never push it over
        budget -= 1
    return budget


def max_in_for(amount_in: int, slippage_pct: Decimal) -> int:
    """The most to pay: the quoted price plus the slippage, rounded up."""
    limit = Decimal(amount_in) * (Decimal(1) + slippage_pct / Decimal(100))
    return int(limit.to_integral_value(rounding="ROUND_CEILING"))


def impact_percent(spot_in: int, spot_out: int, amount_in: int, amount_out: int) -> Decimal:
    """How much worse the price is than a one-token-sized trade's price (0 when it cannot be worked out)."""
    if min(spot_in, spot_out, amount_in, amount_out) <= 0:
        return Decimal(0)
    spot = Decimal(spot_in) / Decimal(spot_out)
    got = Decimal(amount_in) / Decimal(amount_out)
    return max(Decimal(0), (got / spot - 1) * 100)


async def quote(rpc, sender: str, token_in: str, token_out: str, in_symbol: str, out_symbol: str, in_decimals: int,
                out_decimals: int, spend_raw: int, slippage_pct: Decimal, buying: bool) -> Quote | None:
    """The best exact receive amount whose cost fits inside `spend_raw`. None if there is no pool or it is too small."""
    if spend_raw <= 0:
        return None
    # 1. the pool's price for a small trade (also proves a pool exists)
    probe = 10 ** out_decimals
    spot = None
    for _ in range(6):
        spot = await simulate(rpc, sender, token_in, token_out, probe, _HUGE)
        if spot is not None or probe <= 1:
            break
        probe = max(1, probe // 10)
    if spot is None or spot.amount_in <= 0:
        return None
    # 2. the receive amount if there were no price impact is an upper bound; find the best one that fits
    hi = spend_raw * probe // spot.amount_in + 1
    lo = 0
    best = None
    for _ in range(40):
        if hi - lo <= max(1, hi // 2000):
            break
        mid = (lo + hi) // 2
        got = await simulate(rpc, sender, token_in, token_out, mid, spend_raw)
        if got is not None and got.amount_in <= spend_raw:
            lo, best = mid, got
        else:
            hi = mid
    if best is None or lo <= 0:
        return None
    for _ in range(6):  # use as much of the amount as the pool allows: scale the last good answer up to the spend
        cand = best.amount_out * spend_raw // max(1, best.amount_in)
        if cand <= lo:
            break
        got = await simulate(rpc, sender, token_in, token_out, cand, spend_raw)
        if got is None or got.amount_in > spend_raw:
            cand = lo + (cand - lo) // 2
            if cand <= lo:
                break
            got = await simulate(rpc, sender, token_in, token_out, cand, spend_raw)
            if got is None or got.amount_in > spend_raw:
                break
        lo, best = cand, got
    return Quote(
        buying=buying, token_in=normalize_hash(token_in), token_out=normalize_hash(token_out), in_symbol=in_symbol,
        out_symbol=out_symbol, in_decimals=in_decimals, out_decimals=out_decimals, amount_in_raw=best.amount_in,
        amount_out_raw=best.amount_out or lo, fee_raw=best.fee, max_in_raw=max_in_for(best.amount_in, slippage_pct),
        impact_pct=impact_percent(spot.amount_in, probe, best.amount_in, best.amount_out or lo),
        system_fee_raw=best.system_fee)


async def sell_price_in_gas(rpc, sender: str, token: str, decimals: int, held_raw: int) -> Decimal | None:
    """What FTW's pool pays in GAS for one whole token, from a dry run of a tiny sale of the sender's own tokens.
    None when there is no pool, the sender holds too little, or the node can't be asked. Read-only."""
    if held_raw <= 0:
        return None
    try:
        for gas_raw in (10 ** 7, 10 ** 6, 10 ** 5, 10 ** 4):  # 0.1, 0.01, 0.001, 0.0001 GAS
            got = await simulate(rpc, sender, token, GAS_HASH, gas_raw, held_raw)
            if got is not None and got.amount_in > 0 and got.amount_out > 0:
                gas = Decimal(got.amount_out) / Decimal(10) ** 8
                tokens = Decimal(got.amount_in) / Decimal(10) ** decimals
                return gas / tokens
    except NodeUnavailable:
        return None
    return None


def _num(value: Decimal) -> str:
    from .dexquote import _num
    return _num(value)


def _usd(value: Decimal | None, price: Decimal | None) -> str:
    from .dexquote import _usd
    return _usd(value, price)


def render_quote(q: Quote, slippage_pct: Decimal, in_usd: Decimal | None, out_usd: Decimal | None,
                 wanted_raw: int | None = None) -> str:
    """The quote as a Telegram HTML message."""
    import html as _h
    ins, outs = _h.escape(q.in_symbol), _h.escape(q.out_symbol)
    fee_pct = (FEE * 100).normalize()
    lines = [
        "💱 <b>FTW Swap</b> · Neo N3\n",
        f"You receive: <b>{_num(q.receive)} {outs}</b>{_usd(q.receive, out_usd)}",
        f"You pay ≈ <b>{_num(q.pay)} {ins}</b>{_usd(q.pay, in_usd)}",
        f"At most you pay (with {slippage_pct.normalize():f}% slippage): {_num(q.max_pay)} {ins}",
        f"Trade fee: {fee_pct:f}% = {_num(q.fee)} {ins}{_usd(q.fee, in_usd)} (included in the price)",
        f"Price impact: {q.impact_pct:.2f}%" + ("  ⚠️ high" if q.impact_pct >= 5 else ""),
        f"Network fee: paid in GAS (the node estimates {_num(q.system_fee)} GAS for the contract, plus a small network fee). "
        "The real amount is shown after the trade.",
    ]
    if wanted_raw and q.amount_in_raw < wanted_raw * 98 // 100:
        want = Decimal(wanted_raw) / Decimal(10) ** q.in_decimals
        lines.append(f"\nℹ️ You asked to spend {_num(want)} {ins}, but this pool can fill only part of it right now, so this trade "
                     f"uses {_num(q.pay)} {ins}. The rest stays in your wallet. Tap Confirm to trade this much, or try another amount.")
    return "\n".join(lines)


async def swap(rpc_url: str, private_key: bytes, q: Quote) -> str:
    """Dry-runs and then sends the swap described by a quote. Returns the transaction id.
    Raises neo_send.SendError (safe to show) for anything that stops it BEFORE the broadcast."""
    from neo3.api.helpers.signing import sign_with_account
    from neo3.api.wrappers import ChainFacade, GenericContract
    from neo3.core import types
    from neo3.network.payloads.verification import Signer, WitnessScope
    from neo3.wallet.account import Account

    account = Account.from_private_key(private_key)
    contract = GenericContract(types.UInt160.from_string(SWAP_CONTRACT))
    deadline_ms = int((time.time() + DEADLINE_SECONDS) * 1000)
    allowed = [types.UInt160.from_string(h) for h in (SWAP_CONTRACT, q.token_in, q.token_out)]
    scope = WitnessScope.CALLED_BY_ENTRY | WitnessScope.CUSTOM_CONTRACTS

    def make_signer():
        return Signer(account.script_hash, scope, allowed_contracts=list(allowed))

    def make_call():
        return contract.call_function(METHOD, [
            account.script_hash, types.UInt160.from_string(q.token_in), types.UInt160.from_string(q.token_out),
            q.amount_out_raw, q.max_in_raw, deadline_ms])

    async def dry_run():
        facade = ChainFacade(rpc_host=rpc_url)
        facade.add_test_signer(make_signer())
        return await facade.test_invoke(make_call())

    try:
        dry = await neo_send.retry_before_sending(dry_run)
    except Exception as exc:
        log.exception("FTW dry run failed")
        raise neo_send.SendError("I could not prepare this swap, so nothing was sent. "
                                 f"({(str(exc) or type(exc).__name__)[:120]})")
    if "HALT" not in str(dry.state).upper():
        raise neo_send.SendError("The network says this swap would fail (the price may have moved or the pool is too small). "
                                 "Nothing was sent and nothing was spent.")

    async def build_and_broadcast():
        facade = ChainFacade(rpc_host=rpc_url)
        facade.add_signer(sign_with_account(account), make_signer())
        return await facade.invoke_fast(make_call())

    try:
        tx_id = await neo_send.retry_before_sending(build_and_broadcast)
    except Exception as exc:
        log.exception("FTW broadcast failed")
        raise neo_send.SendError("The network did not accept the swap. Check your balance before retrying. "
                                 f"({(str(exc) or type(exc).__name__)[:120]})")
    return normalize_hash(str(tx_id))


async def outcome_details(rpc, tx_hash: str) -> tuple[SwapResult | None, int]:
    """(the Swap event of the finished transaction, the GAS fees it paid in 8-decimal units). Never raises."""
    result, fees = None, 0
    try:
        log_ = await rpc.call("getapplicationlog", [tx_hash])
        for ex in log_.get("executions", []):
            result = parse_simulation({"state": ex.get("vmstate", ""), "notifications": ex.get("notifications", []),
                                       "gasconsumed": ex.get("gasconsumed", "0")}) or result
    except Exception:
        pass
    try:
        raw = await rpc.call("getrawtransaction", [tx_hash, True])
        fees = int(str(raw.get("sysfee", "0"))) + int(str(raw.get("netfee", "0")))
    except Exception:
        pass
    return result, fees
