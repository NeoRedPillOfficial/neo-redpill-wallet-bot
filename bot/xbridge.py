"""Bridging a coin or token from one EVM network to another, through LI.FI (an aggregator of many bridges).

LI.FI is asked for a quote (a read-only web request); it answers with the best route and a ready transaction. Nothing is
trusted blindly. Before anything is signed the transaction is checked here:
  * it must be sent from the wallet, on the chain the person chose,
  * it must go to the LI.FI contract (the "Diamond") and nowhere else, and the approval must go to that same contract,
  * the coin it carries must be no more than the amount (plus a small allowance for the bridge's own fee),
  * the network's dry run (estimateGas) must accept it.
A token is approved for EXACTLY the amount being bridged, never unlimited.
Supported: the EVM networks LI.FI serves (Ethereum, BNB Chain, Polygon, Arbitrum, Optimism, Base, Avalanche).
Neo N3, Neo X and Solana are not served by it (Neo N3 <-> Neo X has its own bridge).
"""
import asyncio
import logging
import os
import time
from dataclasses import dataclass
from decimal import Decimal

log = logging.getLogger("walletbot.xbridge")

LIFI_URL = "https://li.quest/v1"
DIAMOND = "0x1231DEB6f5749EF6cE6943a275A1D3E7486F4EaE"  # LI.FI's contract; the same on the networks above
NATIVE_TOKEN = "0x0000000000000000000000000000000000000000"
SUPPORTED_CHAIN_IDS = {1: "eth", 56: "bsc", 137: "polygon", 42161: "arbitrum", 10: "optimism", 8453: "base", 43114: "avalanche"}
DEFAULT_SLIPPAGE = Decimal("0.005")  # 0.5%
FEE_ALLOWANCE_PERCENT = 5  # coin carried beyond the amount (a bridge's own fee) may be at most this many percent more
STATUS_POLL_SECONDS = 20
STATUS_GIVE_UP_SECONDS = 20 * 60


class XBridgeError(Exception):
    """Something the person should hear, in words that are safe to show."""


@dataclass(frozen=True)
class XQuote:
    from_chain_id: int
    to_chain_id: int
    from_token: str  # address (zero address for the coin)
    to_token: str
    from_symbol: str
    to_symbol: str
    from_decimals: int
    to_decimals: int
    amount_in: int
    amount_out: int
    min_out: int
    tool: str
    duration_s: int
    fee_usd: Decimal | None
    gas_usd: Decimal | None
    approval_address: str | None
    tx_to: str
    tx_data: str
    tx_value: int
    tx_gas_limit: int | None
    native_in: bool
    provider: str = "lifi"  # "lifi" or "relay"
    request_id: str = ""  # Relay's id for the transfer, used to follow it
    sol: dict | None = None  # Solana-origin transfers: {"instructions": [...], "tables": [...]}

    @property
    def pay(self) -> Decimal:
        return Decimal(self.amount_in) / (Decimal(10) ** self.from_decimals)

    @property
    def receive(self) -> Decimal:
        return Decimal(self.amount_out) / (Decimal(10) ** self.to_decimals)

    @property
    def min_receive(self) -> Decimal:
        return Decimal(self.min_out) / (Decimal(10) ** self.to_decimals)


def _int(value, default: int = 0) -> int:
    try:
        text = str(value)
        return int(text, 16) if text.lower().startswith("0x") else int(Decimal(text))
    except Exception:
        return default


def _dec(value) -> Decimal | None:
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _usd_sum(items) -> Decimal | None:
    total, seen = Decimal(0), False
    for it in items or []:
        v = _dec((it or {}).get("amountUSD"))
        if v is not None:
            total += v
            seen = True
    return total if seen else None


def parse_quote(data: dict, *, wallet: str, from_chain_id: int, to_chain_id: int, amount_in: int,
                to_address: str | None = None) -> XQuote:
    """Reads LI.FI's answer and refuses anything that is not exactly the transfer that was asked for."""
    try:
        action, est, tx = data["action"], data["estimate"], data["transactionRequest"]
        from_tok, to_tok = action["fromToken"], action["toToken"]
        quote = XQuote(
            from_chain_id=int(action["fromChainId"]), to_chain_id=int(action["toChainId"]),
            from_token=str(from_tok["address"]), to_token=str(to_tok["address"]),
            from_symbol=str(from_tok.get("symbol") or "?"), to_symbol=str(to_tok.get("symbol") or "?"),
            from_decimals=int(from_tok["decimals"]), to_decimals=int(to_tok["decimals"]),
            amount_in=_int(action.get("fromAmount")), amount_out=_int(est.get("toAmount")), min_out=_int(est.get("toAmountMin")),
            tool=str(data.get("toolDetails", {}).get("name") or data.get("tool") or "bridge"),
            duration_s=_int(est.get("executionDuration")),
            fee_usd=_usd_sum([f for f in est.get("feeCosts") or [] if not f.get("included")] or est.get("feeCosts")),
            gas_usd=_usd_sum([g for g in est.get("gasCosts") or [] if g.get("type") in ("SEND", "SUM")]),
            approval_address=est.get("approvalAddress"),
            tx_to=str(tx["to"]), tx_data=str(tx["data"]), tx_value=_int(tx.get("value")),
            tx_gas_limit=_int(tx.get("gasLimit")) or None,
            native_in=str(from_tok["address"]).lower() == NATIVE_TOKEN,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise XBridgeError("The bridge service gave an answer I couldn't read. Nothing was sent.") from exc
    if quote.from_chain_id != from_chain_id or quote.to_chain_id != to_chain_id:
        raise XBridgeError("The bridge service answered for different networks than you chose. Nothing was sent.")
    if quote.amount_in != amount_in or quote.amount_out <= 0:
        raise XBridgeError("The bridge service gave an answer that doesn't match your amount. Nothing was sent.")
    if to_address is not None and str(action.get("toAddress", to_address)).lower() != to_address.lower():
        raise XBridgeError("The bridge would pay out to a different address than yours, so I refused it. Nothing was sent.")
    if str(tx.get("from", wallet)).lower() != wallet.lower():
        raise XBridgeError("The bridge transaction is not for your wallet. Nothing was sent.")
    if _int(tx.get("chainId"), from_chain_id) != from_chain_id:
        raise XBridgeError("The bridge transaction is for another network. Nothing was sent.")
    if quote.tx_to.lower() != DIAMOND.lower():
        raise XBridgeError("The bridge transaction goes to a contract I don't recognise, so I refused it. Nothing was sent.")
    if not quote.native_in and quote.approval_address and not est.get("skipApproval") and quote.approval_address.lower() != DIAMOND.lower():
        raise XBridgeError("The bridge asks for an approval to a contract I don't recognise, so I refused it. Nothing was sent.")
    # Coin carried = (the amount, when the coin itself is bridged) + the bridge's own fee in that coin, which the quote lists.
    fees = _fee_allowance(est)
    limit = (amount_in * (100 + FEE_ALLOWANCE_PERCENT) // 100 + fees) if quote.native_in else fees
    if quote.tx_value > limit:
        log.warning("Bridge refused (%s): tx value %s, amount %s, fee allowance %s, feeCosts %s", quote.tool, quote.tx_value,
                    amount_in, fees, est.get("feeCosts"))
        raise XBridgeError("The bridge transaction would send more coin than expected, so I refused it. Nothing was sent.")
    if quote.native_in and quote.tx_value < amount_in:
        raise XBridgeError("The bridge transaction does not carry the full amount. Nothing was sent.")
    return quote


def _fee_allowance(estimate: dict) -> int:
    """Coin a token bridge may carry for the bridge's own fee: what the quote itself lists as a fee in the coin."""
    total = 0
    for fee in estimate.get("feeCosts") or []:
        token = (fee or {}).get("token") or {}
        if str(token.get("address", "")).lower() == NATIVE_TOKEN:
            total += _int(fee.get("amount"))
    return total * 12 // 10  # a little room


async def fetch_quote(session, *, wallet: str, from_chain_id: int, to_chain_id: int, from_token: str, to_token: str,
                      amount_in: int, slippage: Decimal = DEFAULT_SLIPPAGE) -> XQuote:
    """Asks LI.FI for the best route and checks it. `session` is an aiohttp client session."""
    params = {
        "fromChain": from_chain_id, "toChain": to_chain_id, "fromToken": from_token, "toToken": to_token,
        "fromAddress": wallet, "toAddress": wallet, "fromAmount": str(amount_in), "slippage": str(slippage),
        "order": "CHEAPEST", "integrator": os.getenv("LIFI_INTEGRATOR", "neoredpill-wallet"),
    }
    headers = {"accept": "application/json"}
    key = os.getenv("LIFI_API_KEY")
    if key:
        headers["x-lifi-api-key"] = key
    last: Exception | None = None
    for attempt in range(3):
        try:
            async with session.get(f"{LIFI_URL}/quote", params=params, headers=headers) as resp:
                body = await resp.json(content_type=None)
                if resp.status == 404:
                    raise XBridgeError("No bridge route was found for this token and amount. Try a different amount, "
                                       "token or network.")
                if resp.status == 429:
                    raise ConnectionError("rate limited")
                if resp.status >= 400:
                    msg = str(body.get("message", ""))[:150] if isinstance(body, dict) else ""
                    raise XBridgeError(f"The bridge service refused this request. {msg}".strip())
            return parse_quote(body, wallet=wallet, from_chain_id=from_chain_id, to_chain_id=to_chain_id, amount_in=amount_in)
        except XBridgeError:
            raise
        except Exception as exc:
            last = exc
            await asyncio.sleep(1.0 + attempt)
    log.warning("LI.FI quote failed: %r", last)
    raise XBridgeError("I couldn't reach the bridge service. Nothing was sent. Please try again in a minute.")


async def fetch_status(session, tx_hash: str, from_chain_id: int, to_chain_id: int) -> dict:
    """{'status': PENDING|DONE|FAILED|NOT_FOUND, 'received': int | None, 'tx': str | None}."""
    params = {"txHash": tx_hash, "fromChain": from_chain_id, "toChain": to_chain_id}
    try:
        async with session.get(f"{LIFI_URL}/status", params=params, headers={"accept": "application/json"}) as resp:
            body = await resp.json(content_type=None)
    except Exception:
        return {"status": "UNKNOWN", "received": None, "tx": None}
    if not isinstance(body, dict):
        return {"status": "UNKNOWN", "received": None, "tx": None}
    receiving = body.get("receiving") or {}
    return {"status": str(body.get("status") or "UNKNOWN").upper(), "received": _int(receiving.get("amount")) or None,
            "tx": receiving.get("txHash")}


def format_duration(seconds: int) -> str:
    if seconds <= 0:
        return "a few minutes"
    if seconds < 90:
        return f"about {seconds} seconds"
    return f"about {round(seconds / 60)} minutes"


def render_quote(q: XQuote, from_name: str, to_name: str, native_symbol: str, native_usd: Decimal | None = None) -> str:
    """The quote as a Telegram HTML message (every name is escaped; token symbols are chosen by strangers)."""
    import html
    from .dexquote import _num, _usd
    fs, ts, tool = html.escape(q.from_symbol), html.escape(q.to_symbol), html.escape(q.tool)
    lines = [
        f"🌉 <b>Bridge {html.escape(from_name)} ➜ {html.escape(to_name)}</b>\n",
        f"You send: <b>{_num(q.pay)} {fs}</b> on {html.escape(from_name)}",
        f"You receive ≈ <b>{_num(q.receive)} {ts}</b> on {html.escape(to_name)}",
        f"Minimum received: {_num(q.min_receive)} {ts}",
        f"Route: {tool}",
        f"Time: {format_duration(q.duration_s)}",
    ]
    if q.fee_usd is not None:
        lines.append(f"Bridge fee: ≈ ${q.fee_usd:,.2f}" if q.fee_usd >= Decimal("0.01") else "Bridge fee: &lt; $0.01")
    if q.gas_usd is not None:
        lines.append(f"Network fee (estimate, on {html.escape(from_name)}): ≈ ${q.gas_usd:,.2f}")
    if not q.native_in and q.tx_value:
        coin = Decimal(q.tx_value) / (Decimal(10) ** 18)
        lines.append(f"Sent with it for the bridge: {_num(coin)} {html.escape(native_symbol)}{_usd(coin, native_usd)}")
    lines.append("\nYou need some " + html.escape(native_symbol) + f" on {html.escape(from_name)} to pay the network fee.")
    return "\n".join(lines)


# ------------------------------------------------------------------ choosing between bridges
SOLANA_CHAIN_ID = 1151111081099710  # LI.FI's id for Solana (checked against its chain list at run time)
SOLANA_NATIVE = "11111111111111111111111111111111"
_SUPPORT = {"at": 0.0, "ids": set(), "solana": None}
SUPPORT_KEEP_SECONDS = 3600


@dataclass(frozen=True)
class RouteOption:
    index: int
    tool: str
    amount_out: int
    min_out: int
    fee_usd: Decimal | None
    gas_usd: Decimal | None
    duration_s: int
    tags: tuple
    step: dict  # the step exactly as LI.FI gave it, to ask for its transaction after the person chooses
    to_decimals: int
    to_symbol: str

    @property
    def receive(self) -> Decimal:
        return Decimal(self.amount_out) / (Decimal(10) ** self.to_decimals)


async def fetch_supported(session, force: bool = False) -> tuple[set[int], dict | None]:
    """(chain ids LI.FI serves, Solana's info or None). Remembered for an hour; if LI.FI can't be asked, the last answer
    (or the built-in list) is used."""
    now = time.time()
    if not force and _SUPPORT["ids"] and now - _SUPPORT["at"] < SUPPORT_KEEP_SECONDS:
        return _SUPPORT["ids"], _SUPPORT["solana"]
    try:
        async with session.get(f"{LIFI_URL}/chains", params={"chainTypes": "EVM,SVM"}, headers={"accept": "application/json"}) as resp:
            body = await resp.json(content_type=None)
        ids, solana = set(), None
        for chain in (body or {}).get("chains", []):
            cid = int(chain["id"])
            ids.add(cid)
            if str(chain.get("chainType", "")).upper() == "SVM":
                solana = {"id": cid, "native": (chain.get("nativeToken") or {}).get("address") or SOLANA_NATIVE}
        if ids:
            _SUPPORT.update(at=now, ids=ids, solana=solana)
            return ids, solana
    except Exception as exc:
        log.warning("LI.FI chain list unavailable: %r", exc)
    if not _SUPPORT["ids"]:
        _SUPPORT.update(ids=set(SUPPORTED_CHAIN_IDS), solana={"id": SOLANA_CHAIN_ID, "native": SOLANA_NATIVE})
    return _SUPPORT["ids"], _SUPPORT["solana"]


def _step_fee_usd(step: dict) -> Decimal | None:
    est = step.get("estimate") or {}
    return _usd_sum([f for f in est.get("feeCosts") or [] if not f.get("included")] or est.get("feeCosts"))


def parse_route_options(body: dict, limit: int = 5) -> list[RouteOption]:
    """The single-step routes of LI.FI's answer, best price first, with a note on which is cheapest and which is fastest.
    Routes that need several separate transactions are left out."""
    options: list[RouteOption] = []
    for route in (body or {}).get("routes") or []:
        steps = route.get("steps") or []
        if len(steps) != 1:
            continue
        step = steps[0]
        try:
            est = step["estimate"]
            to_tok = route.get("toToken") or step["action"]["toToken"]
            options.append(RouteOption(
                index=0, tool=str((step.get("toolDetails") or {}).get("name") or step.get("tool") or "bridge"),
                amount_out=_int(route.get("toAmount") or est.get("toAmount")), min_out=_int(route.get("toAmountMin") or est.get("toAmountMin")),
                fee_usd=_step_fee_usd(step), gas_usd=_dec(route.get("gasCostUSD")),
                duration_s=_int(est.get("executionDuration")), tags=tuple(route.get("tags") or ()), step=step,
                to_decimals=int(to_tok["decimals"]), to_symbol=str(to_tok.get("symbol") or "?")))
        except (KeyError, TypeError, ValueError):
            continue
    options = [o for o in options if o.amount_out > 0]
    options.sort(key=lambda o: -o.amount_out)
    out = []
    for i, o in enumerate(options[:limit]):
        out.append(RouteOption(i, o.tool, o.amount_out, o.min_out, o.fee_usd, o.gas_usd, o.duration_s, o.tags, o.step,
                               o.to_decimals, o.to_symbol))
    return out


def route_labels(options: list[RouteOption]) -> list[str]:
    """For each option, a short badge: best price (the most received), fastest, or nothing."""
    labels = [""] * len(options)
    if not options:
        return labels
    best = max(range(len(options)), key=lambda i: options[i].amount_out)
    timed = [i for i, o in enumerate(options) if o.duration_s > 0]
    fastest = min(timed, key=lambda i: options[i].duration_s) if timed else None
    for i in range(len(options)):
        parts = []
        if i == best:
            parts.append("⭐ Best price")
        if i == fastest and len(options) > 1:
            parts.append("⚡ Fastest")
        labels[i] = " · ".join(parts)
    return labels


async def fetch_routes(session, *, wallet: str, from_chain_id: int, to_chain_id: int, from_token: str, to_token: str,
                       amount_in: int, to_address: str | None = None, slippage: Decimal = DEFAULT_SLIPPAGE) -> list[RouteOption]:
    """Every bridge LI.FI can use for this transfer, as options to choose from (read-only)."""
    body = {
        "fromChainId": from_chain_id, "toChainId": to_chain_id, "fromTokenAddress": from_token, "toTokenAddress": to_token,
        "fromAmount": str(amount_in), "fromAddress": wallet, "toAddress": to_address or wallet,
        "options": {"slippage": float(slippage), "order": "CHEAPEST", "integrator": os.getenv("LIFI_INTEGRATOR", "neoredpill-wallet")},
    }
    headers = {"accept": "application/json", "content-type": "application/json"}
    key = os.getenv("LIFI_API_KEY")
    if key:
        headers["x-lifi-api-key"] = key
    last: Exception | None = None
    for attempt in range(3):
        try:
            async with session.post(f"{LIFI_URL}/advanced/routes", json=body, headers=headers) as resp:
                data = await resp.json(content_type=None)
                if resp.status == 429:
                    raise ConnectionError("rate limited")
                if resp.status >= 400 and resp.status != 404:
                    msg = str(data.get("message", ""))[:150] if isinstance(data, dict) else ""
                    raise XBridgeError(f"The bridge service refused this request. {msg}".strip())
            options = parse_route_options(data if isinstance(data, dict) else {})
            if not options:
                raise XBridgeError("No bridge can carry this token and amount between these networks right now. "
                                   "Try a different amount or token.")
            return options
        except XBridgeError:
            raise
        except Exception as exc:
            last = exc
            await asyncio.sleep(1.0 + attempt)
    log.warning("LI.FI routes failed: %r", last)
    raise XBridgeError("I couldn't reach the bridge service. Nothing was sent. Please try again in a minute.")


async def fetch_step_quote(session, step: dict, *, wallet: str, from_chain_id: int, to_chain_id: int, amount_in: int,
                           to_address: str | None = None) -> XQuote:
    """The transaction for the route the person chose, checked like any other quote."""
    if step.get("__relay__"):
        return await fetch_relay_quote(session, wallet=wallet, from_chain_id=from_chain_id, to_chain_id=to_chain_id,
                                       from_token=step["from_token"], to_token=step["to_token"], amount_in=amount_in, to_address=to_address)
    headers = {"accept": "application/json", "content-type": "application/json"}
    key = os.getenv("LIFI_API_KEY")
    if key:
        headers["x-lifi-api-key"] = key
    last: Exception | None = None
    for attempt in range(3):
        try:
            async with session.post(f"{LIFI_URL}/advanced/stepTransaction", json=step, headers=headers) as resp:
                data = await resp.json(content_type=None)
                if resp.status == 429:
                    raise ConnectionError("rate limited")
                if resp.status >= 400:
                    msg = str(data.get("message", ""))[:150] if isinstance(data, dict) else ""
                    raise XBridgeError(f"The bridge service could not prepare that route. {msg}".strip())
            return parse_quote(data, wallet=wallet, from_chain_id=from_chain_id, to_chain_id=to_chain_id, amount_in=amount_in,
                               to_address=to_address)
        except XBridgeError:
            raise
        except Exception as exc:
            last = exc
            await asyncio.sleep(1.0 + attempt)
    log.warning("LI.FI stepTransaction failed: %r", last)
    raise XBridgeError("I couldn't reach the bridge service. Nothing was sent. Please try again in a minute.")


async def fetch_token(session, chain_id: int, symbol: str) -> dict | None:
    """The token with this symbol on a chain, as LI.FI knows it: {'address', 'decimals', 'symbol'}; None if unknown."""
    try:
        async with session.get(f"{LIFI_URL}/token", params={"chain": chain_id, "token": symbol},
                               headers={"accept": "application/json"}) as resp:
            if resp.status >= 400:
                return None
            data = await resp.json(content_type=None)
        if not isinstance(data, dict) or not data.get("address") or str(data.get("symbol", "")).lower() != symbol.lower():
            return None
        return {"address": str(data["address"]), "decimals": int(data["decimals"]), "symbol": str(data["symbol"])}
    except Exception:
        return None


def render_options(options: list[RouteOption], from_name: str, to_name: str, pay_text: str, pay_symbol: str) -> str:
    """The list of bridges to choose from (every name escaped)."""
    import html
    from .dexquote import _num
    labels = route_labels(options)
    lines = [f"🌉 <b>{html.escape(from_name)} ➜ {html.escape(to_name)}</b>",
             f"You send: <b>{html.escape(pay_text)} {html.escape(pay_symbol)}</b>\n", "Choose a bridge:\n"]
    for opt, label in zip(options, labels):
        fee = ("&lt; $0.01" if opt.fee_usd < Decimal("0.01") else f"≈ ${opt.fee_usd:,.2f}") if opt.fee_usd is not None else "n/a"
        gas = f" · network fee ≈ ${opt.gas_usd:,.2f}" if opt.gas_usd is not None else ""
        lines.append(f"<b>{opt.index + 1}. {html.escape(opt.tool)}</b>" + (f"  {label}" if label else ""))
        lines.append(f"   You receive ≈ {_num(opt.receive)} {html.escape(opt.to_symbol)} · bridge fee {fee}{gas} · {format_duration(opt.duration_s)}")
    return "\n".join(lines)


# ------------------------------------------------------------------ Relay (relay.link), a second bridge to compare
RELAY_URL = "https://api.relay.link"
RELAY_SLIPPAGE_BPS = "50"  # 0.5%
RELAY_SOLANA_ID = 792703809  # Relay's id for Solana (LI.FI uses another number)
APPROVE_SELECTOR = "0x095ea7b3"
# Relay's own contracts, from its documentation (docs.relay.link, "Contract Addresses"). A quote may only send a
# transaction to one of these, or to a contract that Relay's chain list names for that network.
RELAY_KNOWN = {a.lower() for a in (
    "0xf70da97812cb96acdf810712aa562db8dfa3dbef",  # solver (EVM)
    "0x4cd00e387622c35bddb9b4c962c136462338bc31",  # depository
    "0x113a327221d2c4660684449bfc39bc14ad1aaf38", "0xf5042e6ffac5a625d4e7848e0b01373d8eb9e222", "0x8fdceeda2951a9747feaf25311435448bce47b2a",
    "0xcd740b0e005cb8647f9baf4febedc8753ceef861", "0xbbbfd134e9b44bfb5123898ba36b01de7ab93d98", "0xaec31c3780521c34ca59dc2eb5fb9ee2e285cebe",
    "0xb758f3bfa7b9d39ef5457d7c7ffb3702f2ad3982", "0x3ec130b627944cad9b2750300ecb0a695da522b6",
    "0x953c95146eb8ce763f35caf2f1d46ddf6a33bea2", "0x58cc3e0aa6cd7bf795832a225179ec2d848ce3e7",
    "0x9ef6d3c2f60d7b9008d74cab1fc0f899c957c819", "0xb92fe925dc43a0ecde6c8b1a2709c170ec4fff4f", "0xe16870b028704e38dbc254a84d3f72c8ba345ca9",
    "0x8754bc615047de01228a7527b712806a71a8dc9a", "0xccc88a9d1b4ed6b0eaba998850414b24f1c315be", "0xf6e54bbf91e564fcf0df3ed9f2dd82913e9232c3",
    "0x6328ea02d28131e58b28e55c6258258a30d90e05", "0x0729e2a4716d9f7dd17858d5d504426d228697f7",
    "0xe4dfbd5e6784db1058124e2c87554c0591f0fb7b", "0x5567954cd026cebfe486062cb75848e0f1c12e58",
)}
_RELAY_CHAINS: dict = {"at": 0.0, "by_id": {}}


def relay_key() -> str:
    return os.getenv("RELAY_API_KEY", "").strip()


def relay_enabled() -> bool:
    return bool(relay_key())


def _relay_headers() -> dict:
    return {"accept": "application/json", "content-type": "application/json", "x-api-key": relay_key()}


def _hex_addresses(node, depth: int = 0) -> set[str]:
    """Every 0x…40-hex address anywhere inside a piece of JSON."""
    import re
    found: set[str] = set()
    if depth > 6:
        return found
    if isinstance(node, str):
        if re.fullmatch(r"0x[0-9a-fA-F]{40}", node):
            found.add(node.lower())
    elif isinstance(node, dict):
        for v in node.values():
            found |= _hex_addresses(v, depth + 1)
    elif isinstance(node, (list, tuple)):
        for v in node:
            found |= _hex_addresses(v, depth + 1)
    return found


async def relay_allowed(session, chain_id: int) -> set[str]:
    """Relay's own contracts for one network: the documented list plus what Relay's chain list says for that network
    (only its `contracts` and solver entries, never token lists)."""
    allowed = set(RELAY_KNOWN)
    now = time.time()
    if now - _RELAY_CHAINS["at"] > 3600 or not _RELAY_CHAINS["by_id"]:
        try:
            async with session.get(f"{RELAY_URL}/chains", headers={"accept": "application/json", "x-api-key": relay_key()}) as resp:
                body = await resp.json(content_type=None)
            by_id = {}
            for chain in (body or {}).get("chains", []):
                found = _hex_addresses(chain.get("contracts")) | _hex_addresses(chain.get("protocol")) | _hex_addresses(chain.get("solverAddress"))
                by_id[int(chain["id"])] = found
            if by_id:
                _RELAY_CHAINS.update(at=now, by_id=by_id)
        except Exception as exc:
            log.warning("Relay chain list unavailable: %r", exc)
    return allowed | set(_RELAY_CHAINS["by_id"].get(chain_id, ()))


def parse_relay_quote(data: dict, *, wallet: str, from_chain_id: int, to_chain_id: int, amount_in: int, to_address: str | None,
                      from_token: str, to_token: str, allowed: set[str]) -> XQuote:
    """Reads Relay's answer and refuses anything that is not exactly the transfer that was asked for. Fails closed:
    anything unexpected (extra steps, signatures, an unknown contract) means nothing is sent."""
    def refuse(why: str):
        raise XBridgeError(f"{why} Nothing was sent.")

    try:
        steps = data["steps"]
        details = data["details"]
        out_cur = details["currencyOut"]
        amount_out = _int(out_cur.get("amount"))
        in_amount = _int((details.get("currencyIn") or {}).get("amount"), amount_in)
    except (KeyError, TypeError, AttributeError) as exc:
        raise XBridgeError("Relay gave an answer I couldn't read. Nothing was sent.") from exc
    if not isinstance(steps, list) or not 1 <= len(steps) <= 2:
        refuse("Relay's route needs steps I don't support, so I refused it.")
    approve_tx, deposit_tx, request_id = None, None, ""
    for step in steps:
        items = step.get("items") or []
        if step.get("kind") != "transaction" or len(items) != 1 or step.get("id") not in ("approve", "deposit"):
            refuse("Relay's route needs steps I don't support, so I refused it.")
        tx = (items[0] or {}).get("data") or {}
        request_id = request_id or str(step.get("requestId") or "")
        if str(tx.get("from", wallet)).lower() != wallet.lower():
            refuse("The Relay transaction is not for your wallet.")
        if _int(tx.get("chainId"), from_chain_id) != from_chain_id:
            refuse("The Relay transaction is for another network.")
        if step["id"] == "approve":
            approve_tx = tx
        else:
            deposit_tx = tx
    if deposit_tx is None:
        refuse("Relay's route has no deposit step.")
    native_in = from_token.lower() == NATIVE_TOKEN
    if native_in and approve_tx is not None:
        refuse("Relay asked for an approval on a plain coin transfer, so I refused it.")
    if not native_in and approve_tx is None:
        approve_tx = None  # the allowance may already be enough; the bridge step below is still checked
    to_tx = str(deposit_tx.get("to", "")).lower()
    if to_tx not in allowed:
        refuse("The Relay transaction goes to a contract I don't recognise, so I refused it.")
    value = _int(deposit_tx.get("value"))
    fee_room = _int(((data.get("fees") or {}).get("relayer") or {}).get("amount")) if native_in else 0
    limit = (amount_in * (100 + FEE_ALLOWANCE_PERCENT) // 100 + fee_room) if native_in else 0
    if value > limit:
        refuse("The Relay transaction would send more coin than expected, so I refused it.")
    if native_in and value < amount_in:
        refuse("The Relay transaction does not carry the full amount.")
    if in_amount != amount_in or amount_out <= 0:
        refuse("Relay gave an answer that doesn't match your amount.")
    recipient = str(details.get("recipient") or to_address or wallet)
    same = recipient == to_address if (to_address and not to_address.startswith("0x")) else (
        to_address is None or recipient.lower() == to_address.lower())
    if not same:
        refuse("Relay would pay out to a different address than yours, so I refused it.")
    spender = None
    if approve_tx is not None:
        raw = str(approve_tx.get("data", "")).lower()
        if str(approve_tx.get("to", "")).lower() != from_token.lower() or not raw.startswith(APPROVE_SELECTOR) or len(raw) < 10 + 128:
            refuse("Relay asked for an approval I don't recognise, so I refused it.")
        spender = "0x" + raw[10 + 24:10 + 64]
        if spender.lower() not in allowed:
            refuse("Relay asked for an approval to a contract I don't recognise, so I refused it.")
        if int(raw[10 + 64:10 + 128], 16) < amount_in:
            refuse("Relay's approval is smaller than the amount.")
    elif not native_in:
        spender = str(deposit_tx.get("to"))  # nothing to approve in the quote: the allowance may already be enough
        if spender.lower() not in allowed:
            refuse("The Relay transaction goes to a contract I don't recognise, so I refused it.")
    cur_in = (details.get("currencyIn") or {}).get("currency") or {}
    fees = data.get("fees") or {}
    min_out = _int(out_cur.get("minimumAmount")) or amount_out * (10_000 - int(RELAY_SLIPPAGE_BPS)) // 10_000
    return XQuote(
        from_chain_id=from_chain_id, to_chain_id=to_chain_id, from_token=from_token, to_token=to_token,
        from_symbol=str(cur_in.get("symbol") or "?"), to_symbol=str((out_cur.get("currency") or {}).get("symbol") or "?"),
        from_decimals=int(cur_in.get("decimals") or 18), to_decimals=int((out_cur.get("currency") or {}).get("decimals") or 18),
        amount_in=amount_in, amount_out=amount_out, min_out=min_out, tool="Relay",
        duration_s=_int(details.get("timeEstimate")),
        fee_usd=_dec((fees.get("relayer") or {}).get("amountUsd")), gas_usd=_dec((fees.get("gas") or {}).get("amountUsd")),
        approval_address=spender, tx_to=str(deposit_tx["to"]), tx_data=str(deposit_tx.get("data", "0x")), tx_value=value,
        tx_gas_limit=None, native_in=native_in, provider="relay", request_id=request_id,
    )


async def fetch_relay_quote(session, *, wallet: str, from_chain_id: int, to_chain_id: int, from_token: str, to_token: str,
                            amount_in: int, to_address: str | None = None) -> XQuote:
    """Asks Relay for a route and checks it."""
    relay_to = RELAY_SOLANA_ID if to_chain_id == SOLANA_CHAIN_ID else to_chain_id
    from_solana = from_chain_id == SOLANA_CHAIN_ID
    relay_from = RELAY_SOLANA_ID if from_solana else from_chain_id
    body = {
        "user": wallet, "originChainId": relay_from, "destinationChainId": relay_to, "originCurrency": from_token,
        "destinationCurrency": to_token, "amount": str(amount_in), "tradeType": "EXACT_INPUT",
        "recipient": to_address or wallet, "slippageTolerance": RELAY_SLIPPAGE_BPS,
    }
    last: Exception | None = None
    for attempt in range(2):
        try:
            async with session.post(f"{RELAY_URL}/quote/v2", json=body, headers=_relay_headers()) as resp:
                data = await resp.json(content_type=None)
                if resp.status == 429:
                    raise ConnectionError("rate limited")
                if resp.status >= 400:
                    msg = str(data.get("message", ""))[:150] if isinstance(data, dict) else ""
                    raise XBridgeError(f"Relay refused this request. {msg}".strip())
            if from_solana:
                return parse_relay_solana_quote(data, wallet=wallet, from_chain_id=from_chain_id, to_chain_id=to_chain_id,
                                                amount_in=amount_in, to_address=to_address, from_token=from_token, to_token=to_token)
            allowed = await relay_allowed(session, from_chain_id)
            return parse_relay_quote(data, wallet=wallet, from_chain_id=from_chain_id, to_chain_id=to_chain_id, amount_in=amount_in,
                                     to_address=to_address, from_token=from_token, to_token=to_token, allowed=allowed)
        except XBridgeError:
            raise
        except Exception as exc:
            last = exc
            await asyncio.sleep(1.0 + attempt)
    log.warning("Relay quote failed: %r", last)
    raise XBridgeError("I couldn't reach Relay. Nothing was sent.")


def relay_option(q: XQuote, wallet: str, params: dict) -> RouteOption:
    """Relay's quote as one more bridge to choose from. `params` is what is needed to ask Relay again for a fresh quote."""
    return RouteOption(index=0, tool="Relay", amount_out=q.amount_out, min_out=q.min_out, fee_usd=q.fee_usd, gas_usd=q.gas_usd,
                       duration_s=q.duration_s, tags=(), step={"__relay__": True, **params}, to_decimals=q.to_decimals,
                       to_symbol=q.to_symbol)


def merge_options(*groups: list[RouteOption], limit: int = 6) -> list[RouteOption]:
    """All the bridges from every source, best price first, numbered again."""
    flat = [o for g in groups for o in g]
    flat.sort(key=lambda o: -o.amount_out)
    return [RouteOption(i, o.tool, o.amount_out, o.min_out, o.fee_usd, o.gas_usd, o.duration_s, o.tags, o.step, o.to_decimals, o.to_symbol)
            for i, o in enumerate(flat[:limit])]


async def fetch_relay_status(session, request_id: str) -> dict:
    """Same answer shape as fetch_status: {'status': PENDING|DONE|FAILED|UNKNOWN, 'received': None, 'tx': str | None}."""
    try:
        async with session.get(f"{RELAY_URL}/intents/status/v3", params={"requestId": request_id},
                               headers={"accept": "application/json", "x-api-key": relay_key()}) as resp:
            body = await resp.json(content_type=None)
    except Exception:
        return {"status": "UNKNOWN", "received": None, "tx": None}
    if not isinstance(body, dict):
        return {"status": "UNKNOWN", "received": None, "tx": None}
    status = str(body.get("status") or "").lower()
    hashes = body.get("txHashes") or []
    out = {"received": None, "tx": hashes[0] if hashes else None}
    if status == "success":
        return {"status": "DONE", **out}
    if status in ("refund", "refunded"):
        return {"status": "DONE", "substatus": "REFUNDED", **out}
    if status == "failure":
        return {"status": "FAILED", **out}
    return {"status": "PENDING" if status in ("waiting", "depositing", "pending", "delayed") else "UNKNOWN", **out}


# ------------------------------------------------------------------ Relay from Solana
SOL_SYSTEM_OK = {0, 2, 3}  # create account, transfer, create account with seed (never "assign" or nonce changes)
SOL_TOKEN_OK = {1, 3, 9, 12, 16, 17, 18}  # init account, transfer, close, transfer-checked, sync-native (never approve / set-authority)
SOL_FEE_ROOM = 20_000_000  # lamports (0.02 SOL) allowed beyond the amount for fees and account rent
SOL_MAX_TX_BYTES = 1232


def parse_relay_solana_quote(data: dict, *, wallet: str, from_chain_id: int, to_chain_id: int, amount_in: int,
                             to_address: str | None, from_token: str, to_token: str) -> XQuote:
    """Relay's answer for a transfer that starts on Solana. Checks everything that can be checked from the answer alone
    (the money itself is checked later against a dry run of the finished transaction)."""
    from . import solana_keys as sk

    def refuse(why: str):
        raise XBridgeError(f"{why} Nothing was sent.")

    try:
        steps = data["steps"]
        details = data["details"]
        out_cur = details["currencyOut"]
        amount_out = _int(out_cur.get("amount"))
        in_amount = _int((details.get("currencyIn") or {}).get("amount"), amount_in)
    except (KeyError, TypeError, AttributeError) as exc:
        raise XBridgeError("Relay gave an answer I couldn't read. Nothing was sent.") from exc
    if not isinstance(steps, list) or len(steps) != 1 or steps[0].get("kind") != "transaction" or len(steps[0].get("items") or []) != 1:
        refuse("Relay's route needs steps I don't support, so I refused it.")
    step = steps[0]
    item = step["items"][0].get("data") or {}
    raw_ins = item.get("instructions")
    if not isinstance(raw_ins, list) or not 1 <= len(raw_ins) <= 24:
        refuse("Relay's Solana transaction has an unexpected shape, so I refused it.")
    wallet_b = sk.b58decode(wallet)
    clean = []
    for ins in raw_ins:
        try:
            program = sk.b58decode(str(ins["programId"]))
            payload = bytes.fromhex(str(ins.get("data", "")))
            accounts = []
            for k in ins["keys"]:
                key = sk.b58decode(str(k["pubkey"]))
                if len(key) != 32:
                    raise ValueError
                if k.get("isSigner") and key != wallet_b:
                    refuse("Relay's transaction needs another signer, so I refused it.")
                accounts.append((str(k["pubkey"]), bool(k.get("isSigner")), bool(k.get("isWritable"))))
            if len(program) != 32 or len(accounts) > 64:
                raise ValueError
        except XBridgeError:
            raise
        except Exception:
            refuse("Relay's Solana transaction could not be read, so I refused it.")
        if program == sk.SYSTEM_PROGRAM and (len(payload) < 4 or int.from_bytes(payload[:4], "little") not in SOL_SYSTEM_OK):
            refuse("Relay's transaction does something I don't allow, so I refused it.")
        if program in (sk.TOKEN_PROGRAM, sk.TOKEN_2022_PROGRAM) and (not payload or payload[0] not in SOL_TOKEN_OK):
            refuse("Relay's transaction does something I don't allow, so I refused it.")
        clean.append({"program": str(ins["programId"]), "data": payload.hex(), "keys": accounts})
    tables = [str(t) for t in (item.get("addressLookupTableAddresses") or [])]
    if len(tables) > 8 or any(len(sk.b58decode(t)) != 32 for t in tables):
        refuse("Relay's Solana transaction has an unexpected shape, so I refused it.")
    if in_amount != amount_in or amount_out <= 0:
        refuse("Relay gave an answer that doesn't match your amount.")
    recipient = str(details.get("recipient") or to_address or "")
    if to_address is not None and recipient.lower() != to_address.lower():
        refuse("Relay would pay out to a different address than yours, so I refused it.")
    cur_in = (details.get("currencyIn") or {}).get("currency") or {}
    fees = data.get("fees") or {}
    out_c = out_cur.get("currency") or {}
    min_out = _int(out_cur.get("minimumAmount")) or amount_out * (10_000 - int(RELAY_SLIPPAGE_BPS)) // 10_000
    return XQuote(
        from_chain_id=from_chain_id, to_chain_id=to_chain_id, from_token=from_token, to_token=to_token,
        from_symbol=str(cur_in.get("symbol") or "?"), to_symbol=str(out_c.get("symbol") or "?"),
        from_decimals=int(cur_in.get("decimals") or 9), to_decimals=int(out_c.get("decimals") or 18),
        amount_in=amount_in, amount_out=amount_out, min_out=min_out, tool="Relay", duration_s=_int(details.get("timeEstimate")),
        fee_usd=_dec((fees.get("relayer") or {}).get("amountUsd")), gas_usd=_dec((fees.get("gas") or {}).get("amountUsd")),
        approval_address=None, tx_to="", tx_data="", tx_value=0, tx_gas_limit=None, native_in=from_token == SOLANA_NATIVE,
        provider="relay", request_id=str(step.get("requestId") or ""), sol={"instructions": clean, "tables": tables},
    )


def _token_amount(data: bytes) -> int:
    return int.from_bytes(data[64:72], "little") if len(data) >= 72 else 0


async def execute_solana(rpc, private_key: bytes, wallet: str, q: XQuote, timeout: float = 90.0):
    """Builds the version-0 transaction, runs it as a dry run, checks what it would take from the wallet, then signs and sends.
    Raises SendError before anything irreversible. Returns a SendOutcome."""
    import base64
    from . import solana_keys as sk
    from .neo_send import SendError
    from .solana_send import friendly_error, wait_for_signature
    from .solana_rpc import SolRpcError

    if not q.sol:
        raise SendError("This route has no Solana transaction. Nothing was sent.")
    payer = sk.b58decode(wallet)
    instructions = [sk.Instruction(sk.b58decode(i["program"]), tuple((sk.b58decode(k), s, w) for k, s, w in i["keys"]),
                                   bytes.fromhex(i["data"])) for i in q.sol["instructions"]]
    try:
        tables = []
        for t in q.sol["tables"]:
            acct = await rpc.get_account(t)
            if acct is None or acct["owner"] != sk.ADDRESS_LOOKUP_TABLE_PROGRAM_B58:
                raise SendError("Relay's transaction uses an address list I don't recognise. Nothing was sent.")
            tables.append((sk.b58decode(t), sk.parse_lookup_table(acct["data"])))
        before_sol = await rpc.get_balance(wallet)
        token_accts = [] if q.native_in else await rpc.token_accounts(wallet, q.from_token)
        blockhash, last_valid = await rpc.latest_blockhash()
        message = sk.compile_v0_message(payer, instructions, blockhash, tables)
    except SendError:
        raise
    except Exception:
        log.exception("Solana bridge preparation failed")
        raise SendError("I couldn't prepare the Solana transaction. Nothing was sent.")
    unsigned = sk.compact_u16(1) + bytes(64) + message
    if len(unsigned) > SOL_MAX_TX_BYTES:
        raise SendError("Relay's Solana transaction is too large to send. Nothing was sent.")
    watch = [wallet] + [a for a, _ in token_accts]
    try:
        sim = await rpc.call("simulateTransaction", [base64.b64encode(unsigned).decode(), {
            "encoding": "base64", "sigVerify": False, "replaceRecentBlockhash": True, "commitment": "confirmed",
            "accounts": {"encoding": "base64", "addresses": watch}}])
    except Exception as exc:
        raise SendError(f"The dry run of the Solana transaction failed, so nothing was sent. ({str(exc)[:120]})")
    value = (sim or {}).get("value") or {}
    if value.get("err"):
        raise SendError("The dry run of the Solana transaction failed, so nothing was sent.")
    after = value.get("accounts") or []
    try:
        after_sol = int(after[0]["lamports"])
        sol_spent = before_sol - after_sol
        token_spent = 0
        for (addr, held), post in zip(token_accts, after[1:]):
            now = _token_amount(base64.b64decode(post["data"][0])) if post else 0
            token_spent += held - now
    except Exception:
        raise SendError("I couldn't check what the Solana transaction would take. Nothing was sent.")
    allowed_sol = (q.amount_in if q.native_in else 0) + SOL_FEE_ROOM
    if sol_spent > allowed_sol:
        raise SendError("The Solana transaction would take more SOL than expected, so I stopped. Nothing was sent.")
    if not q.native_in and token_spent > q.amount_in:
        raise SendError("The Solana transaction would take more tokens than expected, so I stopped. Nothing was sent.")
    if before_sol < (q.amount_in if q.native_in else 0) + SOL_FEE_ROOM // 4:
        raise SendError("Not enough SOL for the amount plus the network fee. Nothing was sent.")
    transaction = sk.sign_transaction(private_key, message)
    signature = sk.first_signature(transaction)
    try:
        await rpc.send_transaction(transaction)
    except SolRpcError as exc:
        raise SendError(friendly_error(exc))
    except Exception:
        log.exception("Solana bridge broadcast gave no clear answer; checking for %s", signature)
    return await wait_for_signature(rpc, signature, last_valid, timeout, 2.0)


async def execute(rpc, private_key: bytes, wallet: str, q: XQuote, timeout: float = 150.0):
    """Approves exactly the amount (tokens only), dry-runs the bridge transaction, signs and sends it.
    Returns (SendOutcome of the bridge transaction, approval tx hash or None). Raises SendError before anything irreversible."""
    from . import dexswap
    from .dexquote import addr_word
    from .evm_rpc import hex_to_int
    from .evm_send import GAS_BUFFER_PERCENT, PRICE_BUFFER_PERCENT, wait_for_receipt
    from .neo_send import SendError

    try:
        gas_price = await rpc.gas_price() * PRICE_BUFFER_PERCENT // 100
        native_balance = await rpc.get_balance(wallet)
    except Exception:
        raise SendError("I couldn't reach the network to check your balance. Nothing was sent.")
    chain_id = q.from_chain_id
    approve_hash = None
    if q.native_in:
        if native_balance < q.tx_value + 500_000 * gas_price:
            raise SendError("Your balance is too low for the amount plus the network fee. Nothing was sent.")
    else:
        try:
            held = hex_to_int(await rpc._eth_call(q.from_token, "0x70a08231" + addr_word(wallet)))
        except Exception:
            raise SendError("I couldn't read your token balance. Nothing was sent.")
        if held < q.amount_in:
            raise SendError("You don't have that much of the token. Nothing was sent.")
        if native_balance < q.tx_value + 800_000 * gas_price:
            raise SendError("You need some of the network's coin to pay the fee. Nothing was sent.")
        spender = q.approval_address or DIAMOND
        try:
            allowed = hex_to_int(await rpc._eth_call(q.from_token, dexswap.encode_allowance(wallet, spender)))
        except Exception:
            allowed = 0
        if allowed < q.amount_in:
            data = dexswap.encode_approve(spender, q.amount_in)  # exactly this amount, never "unlimited"
            gas = await dexswap._estimate(rpc, {"from": wallet, "to": q.from_token, "data": data}, "approval")
            approve_hash = await dexswap._sign_send(rpc, chain_id, private_key, wallet, q.from_token, 0, data, gas, gas_price)
            done = await wait_for_receipt(rpc, approve_hash, timeout=timeout)
            if done.status != "confirmed":
                raise SendError("The one-off approval did not confirm, so I did not bridge. Nothing was moved.")
    call = {"from": wallet, "to": q.tx_to, "data": q.tx_data}
    if q.tx_value:
        call["value"] = hex(q.tx_value)
    gas = await dexswap._estimate(rpc, call, "bridge")  # the network's dry run: refuses a bridge that would fail
    if q.tx_gas_limit:
        gas = max(gas, min(q.tx_gas_limit, gas * 3))
    try:
        tx_hash = await dexswap._sign_send(rpc, chain_id, private_key, wallet, q.tx_to, q.tx_value, q.tx_data, gas, gas_price)
    except Exception as exc:
        log.exception("Bridge broadcast failed")
        raise SendError(f"The network did not accept the bridge transaction. Check your balance before retrying. "
                        f"({(str(exc) or type(exc).__name__)[:120]})")
    return await wait_for_receipt(rpc, tx_hash, timeout=timeout), approve_hash
