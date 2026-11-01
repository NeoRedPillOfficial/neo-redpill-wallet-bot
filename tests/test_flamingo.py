import asyncio
from decimal import Decimal

from bot import flamingo as fl

GAS = fl.GAS_HASH
NRP = "0x789518aa302b571e3e825f2c85a01ad731014a45"
MID = "0x1005d400bcc2a56b7352f09e273be3f9933a5fb1"
ME = "0xedc31312cdc9ba2ade52d2b9698fdbcc80efbd2c"  # the wallet of the decoded transaction
ME_B64, BOOK_B64 = "LL3vgMzbj2m50lLeKrrJzRITw+0=", "TV2JwGPMsbxY5g/RCX0rZJyOJuw="


def _t(contract, frm, to, amount):
    f = lambda v: {"type": "ByteString", "value": v}
    return {"contract": contract, "eventname": "Transfer",
            "state": {"type": "Array", "value": [f(frm), f(to), {"type": "Integer", "value": str(amount)}]}}


# the events of the real transaction 0x22f4a40c… (shortened to the parts that touch the wallet)
REAL = {"state": "HALT", "gasconsumed": "9876543", "notifications": [
    _t(GAS, ME_B64, BOOK_B64, 1000000),
    _t(MID, "8OrlXl3nmEc23w7PKZF6r7r2bi8=", BOOK_B64, 10597039),
    _t(NRP, BOOK_B64, ME_B64, 45057450426)]}


def test_the_real_transaction_is_understood():
    sim = fl.parse_simulation(REAL, GAS, NRP, ME)
    assert (sim.amount_in, sim.amount_out, sim.system_fee) == (1000000, 45057450426, 9876543)


def test_arguments_follow_the_order_of_the_real_transaction():
    args = fl.build_args(ME, 1000000, 44832163174, [GAS, MID, NRP], (11, 23))
    assert [a["type"] for a in args] == ["Hash160", "Integer", "Integer", "Array", "Array"]
    assert args[0]["value"] == ME and args[1]["value"] == "1000000" and args[2]["value"] == "44832163174"
    assert [h["value"] for h in args[3]["value"]] == [GAS, MID, NRP]
    assert [m["value"] for m in args[4]["value"]] == ["11", "23"]
    assert fl.CONVERT_CONTRACT == "0xf40f694362957d56801a8cef7e62a83f7f1b7b0f" and fl.METHOD == "standardConvert"


def test_the_real_minimum_is_exactly_half_a_percent_under_the_result():
    assert fl.min_out_for(45057450426, Decimal("0.5")) == 44832163173  # the wallet used 44832163174 (rounded), never above
    assert fl.min_out_for(100, Decimal(1)) == 99


def test_failed_or_empty_dry_runs_are_not_a_quote():
    assert fl.parse_simulation({"state": "FAULT", "notifications": REAL["notifications"]}, GAS, NRP, ME) is None
    assert fl.parse_simulation({"state": "HALT", "notifications": []}, GAS, NRP, ME) is None
    assert fl.parse_simulation({"state": "HALT", "notifications": [_t(NRP, BOOK_B64, "AAAAAAAAAAAAAAAAAAAAAAAAAAA=", 5)]}, GAS, NRP, ME) is None
    assert fl.parse_simulation(None, GAS, NRP, ME) is None


def test_only_seen_routes_are_offered():
    assert fl.has_route(NRP) and not fl.has_route("0x" + "12" * 20)
    assert fl.routes_for(NRP, True) == [([GAS, MID, NRP], (11, 23))]
    sells = fl.routes_for(NRP, False)
    assert all(p == [NRP, MID, GAS] for p, _m in sells) and {m for _p, m in sells} == {(23, 11), (11, 23)}
    assert fl.routes_for("0x" + "12" * 20, True) == []


class Node:
    """Answers invokefunction like the node: pays out 450.57 NRP per 0.01 GAS (a little less for bigger amounts)."""

    def __init__(self, ok_markets=((11, 23),)):
        self.ok, self.calls = set(ok_markets), 0

    async def call(self, method, params):
        assert method == "invokefunction" and params[0] == fl.CONVERT_CONTRACT and params[1] == "standardConvert"
        self.calls += 1
        a = params[2]
        amount = int(a[1]["value"])
        markets = tuple(int(m["value"]) for m in a[4]["value"])
        path = [h["value"] for h in a[3]["value"]]
        if markets not in self.ok:
            return {"state": "FAULT", "notifications": []}
        buying = path[0] == GAS
        rate = 45057450426 / 1000000 if buying else 1000000 / 45057450426
        out = int(amount * rate * (1 - amount / 10**12))
        tin, tout = path[0], path[-1]
        return {"state": "HALT", "gasconsumed": "5000000", "notifications": [
            _t(tin, ME_B64, BOOK_B64, amount), _t(tout, BOOK_B64, ME_B64, out)]}


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def test_a_buy_quote_pays_exactly_the_amount_and_sets_the_minimum_received():
    q = run(fl.quote(Node(), ME, NRP, "NRP", 8, 10**8, Decimal(1), True))
    assert q.amount_in_raw == 10**8 and q.in_symbol == "GAS" and q.out_symbol == "NRP"
    assert q.min_out_raw == fl.min_out_for(q.amount_out_raw, Decimal(1)) and q.min_out_raw < q.amount_out_raw
    assert q.path == (GAS, MID, NRP) and q.markets == (11, 23)
    assert Decimal(0) <= q.impact_pct < 1
    text = fl.render_quote(q, Decimal(1), Decimal("1.25"), Decimal("0.00003"))
    for needle in ("Flamingo", "You pay", "You receive", "At least you receive", "1% slippage", "Price impact", "Network fee"):
        assert needle in text, needle


def test_a_sell_finds_the_market_order_the_node_accepts():
    q = run(fl.quote(Node(ok_markets=((23, 11),)), ME, NRP, "NRP", 8, 1000 * 10**8, Decimal(1), False))
    assert q is not None and q.markets == (23, 11) and q.in_symbol == "NRP" and q.out_symbol == "GAS"
    q2 = run(fl.quote(Node(ok_markets=((11, 23),)), ME, NRP, "NRP", 8, 1000 * 10**8, Decimal(1), False))
    assert q2 is not None and q2.markets == (11, 23)
    assert run(fl.quote(Node(ok_markets=()), ME, NRP, "NRP", 8, 1000 * 10**8, Decimal(1), False)) is None


def test_an_unknown_token_or_zero_amount_gives_no_quote_and_a_dead_node_is_an_error():
    assert run(fl.quote(Node(), ME, "0x" + "12" * 20, "X", 8, 10**8, Decimal(1), True)) is None
    assert run(fl.quote(Node(), ME, NRP, "NRP", 8, 0, Decimal(1), True)) is None

    class Down:
        async def call(self, *a):
            raise TimeoutError("down")
    try:
        run(fl.quote(Down(), ME, NRP, "NRP", 8, 10**8, Decimal(1), True))
    except fl.NodeUnavailable:
        return
    raise AssertionError("an unreachable node must not look like 'no route'")


def test_outcome_is_read_from_the_chain_events():
    class Rpc:
        async def call(self, method, params):
            if method == "getapplicationlog":
                return {"executions": [{"notifications": REAL["notifications"]}]}
            return {"sysfee": "9000000", "netfee": "1500000"}
    q = run(fl.quote(Node(), ME, NRP, "NRP", 8, 10**6, Decimal(1), True))
    paid, got, fees = run(fl.outcome_details(Rpc(), "0x" + "ab" * 32, q, ME))
    assert (paid, got, fees) == (1000000, 45057450426, 10500000)


def test_discover_finds_two_hop_route():
    import asyncio, os, tempfile
    from bot import flamingo as f
    os.chdir(tempfile.mkdtemp())
    f._ROUTES.clear(); f._loaded = False
    wallet = "0x" + "11" * 20
    token = "0x" + "22" * 20
    hub = f.HUBS[0]
    good = {(hub, 7), (token, 9)}  # GAS->hub market 7, hub->token market 9

    import base64
    enc = lambda h: {"type": "ByteString", "value": base64.b64encode(bytes.fromhex(h[2:])[::-1]).decode()}
    other = "0x" + "33" * 20

    class Rpc:
        async def call(self, method, params):
            args = params[2]
            path = [a["value"] for a in args[3]["value"]]
            markets = tuple(int(a["value"]) for a in args[4]["value"])
            ok = (len(path) == 2 and path[1] == hub and markets == (7,)) or (len(path) == 3 and path[1] == hub and path[2] == token and markets == (7, 9))
            if not ok:
                return {"state": "FAULT"}
            amt = 1000 * len(path)
            return {"state": "HALT", "gasconsumed": "1", "notifications": [
                {"contract": f.GAS_HASH, "eventname": "Transfer", "state": {"value": [
                    enc(wallet), enc(other), {"type": "Integer", "value": "1000000"}]}},
                {"contract": path[-1], "eventname": "Transfer", "state": {"value": [
                    enc(other), enc(wallet), {"type": "Integer", "value": str(amt)}]}}]}

    route = asyncio.run(f.discover(Rpc(), wallet, token))
    assert route == ([f.GAS_HASH, hub, token], (7, 9))
    assert f.has_route(token)
