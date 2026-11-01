"""Trading on Flamingo Finance (Neo N3) from the bot's own wallet.

What is known, and from where: one real transaction made on Flamingo's Swap page (0.01 GAS for 450.57 NRP) was decoded.
It calls the contract 0xf40f694362957d56801a8cef7e62a83f7f1b7b0f, method "standardConvert", with five arguments:
    (sender, amount you pay, the least you accept, [tokens of the route], [market numbers of the route])
Here: sender = the wallet, 1000000 (0.01 GAS), 44832163174 (exactly 0.5% under what it received),
[GAS, 0x1005d4…, NRP] and [11, 23]. The route goes GAS -> 0x1005d4… -> NRP through two pools, and "11" and "23" are
Flamingo's market numbers for those two hops. The transaction is signed with the scope CustomContracts, allowing the
order-book contract, the convert contract and the token being paid.

Because the market numbers of a route can't be worked out from the tokens, only routes that were really seen are
listed in ROUTES (today: NRP <-> GAS). Every swap is dry-run by the node first; anything unexpected stops it before a fee
is spent, and the result is read from the Transfer events, not from a guess.
"""
import asyncio
import base64
import logging
from dataclasses import dataclass
from decimal import Decimal

from . import neo_send
from .neo_rpc import normalize_hash

log = logging.getLogger("walletbot.flamingo")

CONVERT_CONTRACT = "0xf40f694362957d56801a8cef7e62a83f7f1b7b0f"
ORDERBOOK_CONTRACT = "0xec268e9c642b7d09d10fe658bcb1cc63c0895d4d"
METHOD = "standardConvert"
GAS_HASH = neo_send.GAS_HASH


@dataclass(frozen=True)
class Route:
    middle: str  # the token between GAS and the traded token
    buy_markets: tuple  # market numbers for GAS -> middle -> token (seen on-chain)
    sell_markets: tuple  # the same hops backwards; the order is checked by the node's dry run


ROUTES = {
    "0x789518aa302b571e3e825f2c85a01ad731014a45": Route(  # NRP
        middle="0x1005d400bcc2a56b7352f09e273be3f9933a5fb1", buy_markets=(11, 23), sell_markets=(23, 11)),
}


def _h160(value: str) -> dict:
    return {"type": "Hash160", "value": normalize_hash(value)}


def _int(value: int) -> dict:
    return {"type": "Integer", "value": str(int(value))}


def build_args(sender: str, amount_in: int, min_out: int, path: list[str], markets: tuple) -> list[dict]:
    """The argument list of standardConvert, in the order the real transaction used."""
    return [_h160(sender), _int(amount_in), _int(min_out), {"type": "Array", "value": [_h160(h) for h in path]},
            {"type": "Array", "value": [_int(m) for m in markets]}]


def _signers(sender: str, token_in: str) -> list[dict]:
    return [{"account": normalize_hash(sender), "scopes": "CustomContracts",
             "allowedcontracts": [ORDERBOOK_CONTRACT, CONVERT_CONTRACT, normalize_hash(token_in)]}]


def _hash_of(item) -> str | None:
    try:
        return "0x" + base64.b64decode(item["value"])[::-1].hex()
    except Exception:
        return None


def transfers(notifications, token: str, wallet: str) -> tuple[int, int]:
    """(amount of `token` that reached the wallet, amount of it that left the wallet), from Transfer events."""
    got = spent = 0
    token, wallet = normalize_hash(token), normalize_hash(wallet)
    for note in notifications or []:
        if normalize_hash(str(note.get("contract", ""))) != token or note.get("eventname") != "Transfer":
            continue
        try:
            frm, to, amount = note["state"]["value"][:3]
            amount = int(amount["value"])
        except (KeyError, ValueError, TypeError, IndexError):
            continue
        if _hash_of(to) == wallet:
            got += amount
        if _hash_of(frm) == wallet:
            spent += amount
    return got, spent


@dataclass(frozen=True)
class Sim:
    amount_in: int  # what left the wallet
    amount_out: int  # what reached the wallet
    system_fee: int  # GAS (8 decimals) the node says it costs


class NodeUnavailable(Exception):
    pass


def parse_simulation(result, token_in: str, token_out: str, wallet: str) -> Sim | None:
    if not isinstance(result, dict) or "HALT" not in str(result.get("state", "")).upper():
        return None
    notes = result.get("notifications") or []
    out, _ = transfers(notes, token_out, wallet)
    _, spent = transfers(notes, token_in, wallet)
    if out <= 0 or spent <= 0:
        return None
    try:
        fee = int(str(result.get("gasconsumed", "0")) or 0)
    except ValueError:
        fee = 0
    return Sim(spent, out, fee)


async def simulate(rpc, wallet: str, path: list[str], markets: tuple, amount_in: int, min_out: int = 1) -> Sim | None:
    """Read-only dry run on the node. None when the node says the swap would fail; NodeUnavailable when it can't be asked."""
    params = [CONVERT_CONTRACT, METHOD, build_args(wallet, amount_in, min_out, path, markets), _signers(wallet, path[0])]
    last = None
    for attempt in range(3):
        try:
            return parse_simulation(await rpc.call("invokefunction", params), path[0], path[-1], wallet)
        except Exception as exc:
            last = exc
            log.warning("Flamingo simulation call failed (try %s): %r", attempt + 1, exc)
            await asyncio.sleep(0.4)
    raise NodeUnavailable(repr(last))


@dataclass(frozen=True)
class Quote:
    buying: bool
    token_in: str
    token_out: str
    in_symbol: str
    out_symbol: str
    in_decimals: int
    out_decimals: int
    amount_in_raw: int
    amount_out_raw: int  # what the dry run gives right now
    min_out_raw: int  # the least you accept (slippage)
    path: tuple
    markets: tuple
    impact_pct: Decimal
    system_fee_raw: int

    @property
    def pay(self) -> Decimal:
        return Decimal(self.amount_in_raw) / Decimal(10) ** self.in_decimals

    @property
    def receive(self) -> Decimal:
        return Decimal(self.amount_out_raw) / Decimal(10) ** self.out_decimals

    @property
    def min_receive(self) -> Decimal:
        return Decimal(self.min_out_raw) / Decimal(10) ** self.out_decimals

    @property
    def system_fee(self) -> Decimal:
        return Decimal(self.system_fee_raw) / Decimal(10) ** 8


def min_out_for(amount_out: int, slippage_pct: Decimal) -> int:
    """The least to accept: the quoted amount minus the slippage, rounded down."""
    return int((Decimal(amount_out) * (Decimal(1) - slippage_pct / Decimal(100))).to_integral_value(rounding="ROUND_FLOOR"))


def impact_percent(small_in: int, small_out: int, amount_in: int, amount_out: int) -> Decimal:
    if min(small_in, small_out, amount_in, amount_out) <= 0:
        return Decimal(0)
    small_rate = Decimal(small_out) / Decimal(small_in)
    rate = Decimal(amount_out) / Decimal(amount_in)
    return max(Decimal(0), (1 - rate / small_rate) * 100)


# ------------------------------------------------------------------ finding a route for any token
# Flamingo's route needs the market number of every hop, and those can't be worked out from the tokens. They can be FOUND:
# the node's dry run only succeeds when the markets really connect the tokens, so every market number is tried (read-only,
# nothing is sent) and the working ones are kept. Routes are remembered on disk, so the search runs once per token.
HUBS = (
    "0x1005d400bcc2a56b7352f09e273be3f9933a5fb1",  # the hub NRP's route goes through (seen on-chain)
    "0x48c40d4666f93408be1bef038b6722404d9a4c2a",  # bNEO
    "0xf0151f528127558851b39c2cd8aa47da7418ab28",  # FLM
)
MARKET_NUMBERS = range(1, 101)
PROBE_GAS = 1_000_000  # 0.01 GAS, the amount of the real transaction
CACHE_FILE = "flamingo_routes.json"
FOUND_KEEP = 7 * 86400
MISSING_KEEP = 3600
_ROUTES: dict[str, dict] = {}
_loaded = False


def _load_cache() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        import json
        with open(CACHE_FILE, encoding="utf-8") as fh:
            _ROUTES.update(json.load(fh))
    except Exception:
        pass


def _save_cache() -> None:
    try:
        import json
        with open(CACHE_FILE + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(_ROUTES, fh)
        import os
        os.replace(CACHE_FILE + ".tmp", CACHE_FILE)
    except Exception:
        log.warning("Could not save the Flamingo route cache")


def known_route(token_contract: str):
    """((path, markets) or None, searched): the remembered buy route, and whether it was searched for recently."""
    import time
    _load_cache()
    token = normalize_hash(str(token_contract))
    seed = ROUTES.get(token)
    if seed is not None:
        return ([GAS_HASH, seed.middle, token], tuple(seed.buy_markets)), True
    entry = _ROUTES.get(token)
    if not entry:
        return None, False
    age = time.time() - entry.get("ts", 0)
    if entry.get("path"):
        return ((list(entry["path"]), tuple(entry["markets"])), True) if age < FOUND_KEEP else (None, False)
    return None, age < MISSING_KEEP


def has_route(token_contract: str) -> bool:
    route, _ = known_route(token_contract)
    return route is not None


async def _probe(rpc, wallet: str, path: list[str], markets: tuple):
    """(Sim or None, errored): a quiet dry run of one candidate route with the real transaction's amount."""
    params = [CONVERT_CONTRACT, METHOD, build_args(wallet, PROBE_GAS, 1, path, markets), _signers(wallet, path[0])]
    try:
        return parse_simulation(await rpc.call("invokefunction", params), path[0], path[-1], wallet), False
    except Exception:
        return None, True


async def _try_all(rpc, wallet: str, candidates: list) -> list:
    """Dry-runs all candidates (a few at a time). [(path, markets, Sim)] of those that work.
    Raises NodeUnavailable when the node mostly could not be asked, so a dead node is never taken for 'no route'."""
    gate = asyncio.Semaphore(16)

    async def one(path, markets):
        async with gate:
            return await _probe(rpc, wallet, path, markets)

    results = await asyncio.gather(*(one(p, m) for p, m in candidates))
    if candidates and sum(1 for _s, err in results if err) * 2 > len(candidates):
        raise NodeUnavailable("the node did not answer")
    return [(p, m, sim) for (p, m), (sim, _err) in zip(candidates, results) if sim is not None]


async def discover(rpc, wallet: str, token_contract: str):
    """Finds the best route GAS -> token (directly, or through a hub token). (path, markets) or None. Remembers the answer."""
    import time
    token = normalize_hash(str(token_contract))
    if token == GAS_HASH:
        return None
    _load_cache()
    direct = [([GAS_HASH, token], (m,)) for m in MARKET_NUMBERS]
    hubs = [h for h in HUBS if h != token]
    first = [([GAS_HASH, h], (m,)) for h in hubs for m in MARKET_NUMBERS]
    hits = await _try_all(rpc, wallet, direct + first)
    found = [(p, m, sim) for p, m, sim in hits if p[-1] == token]
    second = []
    for h in hubs:
        mine = [(m, sim) for p, m, sim in hits if p == [GAS_HASH, h]]
        if mine:
            a = max(mine, key=lambda x: x[1].amount_out)[0][0]
            second += [([GAS_HASH, h, token], (a, m)) for m in MARKET_NUMBERS]
    if second:
        found += await _try_all(rpc, wallet, second)
    if found:
        path, markets, _sim = max(found, key=lambda x: x[2].amount_out)
        _ROUTES[token] = {"path": list(path), "markets": list(markets), "ts": time.time()}
        _save_cache()
        return list(path), tuple(markets)
    _ROUTES[token] = {"ts": time.time()}
    _save_cache()
    return None


async def ensure_route(rpc, wallet: str, token_contract: str):
    """The buy route (path, markets) for a token, searching for it if it was not found before. None if Flamingo has none."""
    route, searched = known_route(token_contract)
    if route is not None or searched:
        return route
    return await discover(rpc, wallet, token_contract)


def routes_for(token_contract: str, buying: bool):
    """[(path, markets)] to try, GAS to the token (buying) or the token to GAS (selling)."""
    route, _ = known_route(token_contract)
    if route is None:
        return []
    path, markets = route
    if buying:
        return [(list(path), tuple(markets))]
    # selling goes the same hops backwards; if the node refuses that order of market numbers, the other order is tried
    back = list(reversed(path))
    options = [(back, tuple(reversed(markets)))]
    if len(markets) > 1:
        options.append((back, tuple(markets)))
    return options


async def quote(rpc, wallet: str, token_contract: str, token_symbol: str, token_decimals: int, spend_raw: int,
                slippage_pct: Decimal, buying: bool) -> Quote | None:
    """A quote for paying `spend_raw` of GAS (buying) or of the token (selling). None if there is no route or pool."""
    if spend_raw <= 0:
        return None
    await ensure_route(rpc, wallet, token_contract)
    for path, markets in routes_for(token_contract, buying):
        got = await simulate(rpc, wallet, path, markets, spend_raw)
        if got is None:
            continue
        small_in = max(1, spend_raw // 100)
        small = await simulate(rpc, wallet, path, markets, small_in) if small_in != spend_raw else got
        dec_in, dec_out = (8, token_decimals) if buying else (token_decimals, 8)
        sym_in, sym_out = ("GAS", token_symbol) if buying else (token_symbol, "GAS")
        return Quote(
            buying=buying, token_in=path[0], token_out=path[-1], in_symbol=sym_in, out_symbol=sym_out, in_decimals=dec_in,
            out_decimals=dec_out, amount_in_raw=got.amount_in, amount_out_raw=got.amount_out,
            min_out_raw=min_out_for(got.amount_out, slippage_pct), path=tuple(path), markets=tuple(markets),
            impact_pct=impact_percent(small.amount_in, small.amount_out, got.amount_in, got.amount_out) if small else Decimal(0),
            system_fee_raw=got.system_fee)
    return None


def _num(value: Decimal) -> str:
    from .dexquote import _num
    return _num(value)


def _usd(value: Decimal | None, price: Decimal | None) -> str:
    from .dexquote import _usd
    return _usd(value, price)


def render_quote(q: Quote, slippage_pct: Decimal, in_usd: Decimal | None, out_usd: Decimal | None) -> str:
    import html as _h
    ins, outs = _h.escape(q.in_symbol), _h.escape(q.out_symbol)
    lines = [
        "💱 <b>Flamingo</b> · Neo N3\n",
        f"You pay: <b>{_num(q.pay)} {ins}</b>{_usd(q.pay, in_usd)}",
        f"You receive ≈ <b>{_num(q.receive)} {outs}</b>{_usd(q.receive, out_usd)}",
        f"At least you receive (with {slippage_pct.normalize():f}% slippage): {_num(q.min_receive)} {outs}",
        "Trade fee: included in the price (this quote is already after all pool fees)",
        f"Price impact: {q.impact_pct:.2f}%" + ("  ⚠️ high" if q.impact_pct >= 5 else ""),
        f"Network fee: paid in GAS (the node estimates {_num(q.system_fee)} GAS for the contract, plus a small network fee). "
        "The real amount is shown after the trade.",
    ]
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
    contract = GenericContract(types.UInt160.from_string(CONVERT_CONTRACT))
    allowed = [types.UInt160.from_string(h) for h in (ORDERBOOK_CONTRACT, CONVERT_CONTRACT, q.token_in)]

    def make_signer():
        return Signer(account.script_hash, WitnessScope.CUSTOM_CONTRACTS, allowed_contracts=list(allowed))

    def make_call():
        return contract.call_function(METHOD, [
            account.script_hash, q.amount_in_raw, q.min_out_raw,
            [types.UInt160.from_string(h) for h in q.path], list(q.markets)])

    async def dry_run():
        facade = ChainFacade(rpc_host=rpc_url)
        facade.add_test_signer(make_signer())
        return await facade.test_invoke(make_call())

    try:
        dry = await neo_send.retry_before_sending(dry_run)
    except Exception as exc:
        log.exception("Flamingo dry run failed")
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
        log.exception("Flamingo broadcast failed")
        raise neo_send.SendError("The network did not accept the swap. Check your balance before retrying. "
                                 f"({(str(exc) or type(exc).__name__)[:120]})")
    return normalize_hash(str(tx_id))


async def outcome_details(rpc, tx_hash: str, q: Quote, wallet: str) -> tuple[int | None, int | None, int]:
    """(amount paid, amount received, GAS fees in 8-decimal units) of the finished swap, read from the chain. Never raises."""
    paid = got = None
    fees = 0
    try:
        log_ = await rpc.call("getapplicationlog", [tx_hash])
        notes = [n for ex in log_.get("executions", []) for n in ex.get("notifications", [])]
        out, _ = transfers(notes, q.token_out, wallet)
        _, spent = transfers(notes, q.token_in, wallet)
        paid, got = (spent or None), (out or None)
    except Exception:
        pass
    try:
        raw = await rpc.call("getrawtransaction", [tx_hash, True])
        fees = int(str(raw.get("sysfee", "0"))) + int(str(raw.get("netfee", "0")))
    except Exception:
        pass
    return paid, got, fees
