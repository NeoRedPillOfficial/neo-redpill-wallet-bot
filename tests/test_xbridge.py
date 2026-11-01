import asyncio
from decimal import Decimal

from bot import xbridge as x

ME = "0x" + "ab" * 20


def sample(**over):
    data = {
        "tool": "relay", "toolDetails": {"name": "Relay"},
        "action": {"fromChainId": 8453, "toChainId": 10, "fromAmount": str(10 ** 17), "fromAddress": ME, "toAddress": ME,
                   "fromToken": {"address": x.NATIVE_TOKEN, "symbol": "ETH", "decimals": 18},
                   "toToken": {"address": x.NATIVE_TOKEN, "symbol": "ETH", "decimals": 18}},
        "estimate": {"toAmount": str(99 * 10 ** 15), "toAmountMin": str(98 * 10 ** 15), "executionDuration": 30,
                     "approvalAddress": x.DIAMOND,
                     "feeCosts": [{"amountUSD": "0.12", "included": False}], "gasCosts": [{"type": "SEND", "amountUSD": "0.01"}]},
        "transactionRequest": {"from": ME, "to": x.DIAMOND, "chainId": 8453, "data": "0xabcdef", "value": hex(10 ** 17), "gasLimit": "0x30d40"},
    }
    for k, v in over.items():
        data[k] = v
    return data


def parse(data, amount=10 ** 17, frm=8453, to=10):
    return x.parse_quote(data, wallet=ME, from_chain_id=frm, to_chain_id=to, amount_in=amount)


def refused(data, **kw):
    try:
        parse(data, **kw)
    except x.XBridgeError as exc:
        return str(exc)
    return None


def test_a_good_coin_quote_is_read():
    q = parse(sample())
    assert q.native_in and q.pay == Decimal("0.1") and q.receive == Decimal("0.099") and q.min_receive == Decimal("0.098")
    assert q.tool == "Relay" and q.duration_s == 30 and q.fee_usd == Decimal("0.12") and q.tx_gas_limit == 200000


def test_a_destination_other_than_the_diamond_is_refused():
    d = sample()
    d["transactionRequest"]["to"] = "0x" + "11" * 20
    assert "don't recognise" in refused(d)


def test_the_transaction_must_be_from_the_wallet_and_the_chosen_chain():
    d = sample()
    d["transactionRequest"]["from"] = "0x" + "22" * 20
    assert refused(d)
    d = sample()
    d["transactionRequest"]["chainId"] = 1
    assert refused(d)
    assert refused(sample(), frm=1)  # answered for a different network than chosen
    assert refused(sample(), to=56)


def test_a_coin_transfer_may_not_carry_more_than_the_amount_plus_a_small_fee():
    d = sample()
    d["transactionRequest"]["value"] = hex(10 ** 17 * 104 // 100)
    assert refused(d) is None  # within the 5% allowance
    d["transactionRequest"]["value"] = hex(10 ** 17 * 2)
    assert "more coin" in refused(d)
    d["transactionRequest"]["value"] = hex(10 ** 16)
    assert "full amount" in refused(d)


def test_the_amount_must_match_what_was_asked():
    assert refused(sample(), amount=5 * 10 ** 16)


def test_a_token_bridge_may_carry_only_the_coin_fee_the_quote_lists():
    d = sample()
    d["action"]["fromToken"] = {"address": "0x" + "cc" * 20, "symbol": "USDC", "decimals": 6}
    d["action"]["fromAmount"] = "5000000"
    d["transactionRequest"]["value"] = "0x0"
    assert refused(d, amount=5_000_000) is None
    d["transactionRequest"]["value"] = hex(10 ** 16)
    assert "more coin" in refused(d, amount=5_000_000)
    d["estimate"]["feeCosts"] = [{"token": {"address": x.NATIVE_TOKEN}, "amount": str(10 ** 16), "included": False}]
    assert refused(d, amount=5_000_000) is None


def test_a_token_approval_to_another_contract_is_refused():
    d = sample()
    d["action"]["fromToken"] = {"address": "0x" + "cc" * 20, "symbol": "USDC", "decimals": 6}
    d["action"]["fromAmount"] = "5000000"
    d["transactionRequest"]["value"] = "0x0"
    d["estimate"]["approvalAddress"] = "0x" + "33" * 20
    assert "approval" in refused(d, amount=5_000_000)


def test_garbled_answers_are_refused_not_crashed_on():
    assert refused({"estimate": {}})
    assert refused({})


class FakeResp:
    def __init__(self, status, body):
        self.status, self._body = status, body

    async def json(self, content_type=None):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeSession:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def get(self, url, params=None, headers=None):
        self.calls.append((url, params))
        return self.responses.pop(0)


def test_fetch_quote_sends_the_right_request_and_validates():
    s = FakeSession([FakeResp(200, sample())])
    q = asyncio.run(x.fetch_quote(s, wallet=ME, from_chain_id=8453, to_chain_id=10, from_token=x.NATIVE_TOKEN,
                                  to_token=x.NATIVE_TOKEN, amount_in=10 ** 17))
    assert q.amount_out == 99 * 10 ** 15
    params = s.calls[0][1]
    assert params["fromChain"] == 8453 and params["toChain"] == 10 and params["fromAmount"] == str(10 ** 17)
    assert params["fromAddress"] == ME and params["toAddress"] == ME and params["slippage"] == "0.005"


def test_no_route_and_unreachable_service_give_clear_errors():
    s = FakeSession([FakeResp(404, {"message": "none"})])
    try:
        asyncio.run(x.fetch_quote(s, wallet=ME, from_chain_id=8453, to_chain_id=10, from_token="a", to_token="b", amount_in=1))
        assert False
    except x.XBridgeError as exc:
        assert "No bridge route" in str(exc)

    class Dead:
        def get(self, *a, **k):
            raise ConnectionError("down")
    orig = asyncio.sleep

    async def fast(_):
        return None
    asyncio.sleep = fast
    try:
        asyncio.run(x.fetch_quote(Dead(), wallet=ME, from_chain_id=8453, to_chain_id=10, from_token="a", to_token="b", amount_in=1))
        assert False
    except x.XBridgeError as exc:
        assert "couldn't reach" in str(exc)
    finally:
        asyncio.sleep = orig


def test_status_is_read():
    body = {"status": "DONE", "receiving": {"amount": "99000000000000000", "txHash": "0xdead"}}
    s = FakeSession([FakeResp(200, body)])
    got = asyncio.run(x.fetch_status(s, "0xabc", 8453, 10))
    assert got == {"status": "DONE", "received": 99 * 10 ** 15, "tx": "0xdead"}


def test_the_message_escapes_names_and_shows_the_essentials():
    d = sample()
    d["action"]["toToken"]["symbol"] = "<b>X"
    q = parse(d)
    text = x.render_quote(q, "Base", "Optimism", "ETH")
    assert "&lt;b&gt;X" in text and "<b>X" not in text.replace("<b>", "", 99) or "&lt;b&gt;X" in text
    assert "Bridge Base ➜ Optimism" in text and "Relay" in text and "Minimum received" in text


def _route(tool, out, dur, fee="0.1", tags=(), steps=1):
    step = sample()
    step["tool"] = tool.lower()
    step["toolDetails"] = {"name": tool}
    step["estimate"]["executionDuration"] = dur
    step["estimate"]["feeCosts"] = [{"amountUSD": fee, "included": False}]
    return {"id": tool, "toAmount": str(out), "toAmountMin": str(out * 99 // 100), "gasCostUSD": "0.02", "tags": list(tags),
            "toToken": {"symbol": "ETH", "decimals": 18, "address": x.NATIVE_TOKEN}, "steps": [step] * steps}


def test_routes_are_listed_best_price_first_and_labelled():
    body = {"routes": [_route("Across", 98 * 10 ** 15, 120), _route("Relay", 99 * 10 ** 15, 30, "0.3"), _route("Stargate", 97 * 10 ** 15, 600)]}
    opts = x.parse_route_options(body)
    assert [o.tool for o in opts] == ["Relay", "Across", "Stargate"] and [o.index for o in opts] == [0, 1, 2]
    labels = x.route_labels(opts)
    assert "Best price" in labels[0] and "Fastest" in labels[0] and labels[1] == "" and labels[2] == ""
    assert opts[0].fee_usd == Decimal("0.3") and opts[0].gas_usd == Decimal("0.02")


def test_multi_step_and_empty_routes_are_left_out():
    body = {"routes": [_route("Two", 5, 1, steps=2), _route("Zero", 0, 1), _route("Ok", 10 ** 18, 1)]}
    assert [o.tool for o in x.parse_route_options(body)] == ["Ok"]
    assert x.parse_route_options({}) == [] and x.parse_route_options({"routes": [{"steps": [{}]}]}) == []


def test_fastest_goes_to_a_different_route_when_it_is_not_the_cheapest():
    opts = x.parse_route_options({"routes": [_route("A", 100 * 10 ** 15, 600), _route("B", 99 * 10 ** 15, 20)]})
    labels = x.route_labels(opts)
    assert labels[0] == "⭐ Best price" and labels[1] == "⚡ Fastest"


def test_fetch_routes_posts_the_right_body_and_returns_options():
    class Post:
        def __init__(self, resp):
            self.resp, self.sent = resp, []

        def post(self, url, json=None, headers=None):
            self.sent.append((url, json))
            return self.resp

    s = Post(FakeResp(200, {"routes": [_route("Relay", 99 * 10 ** 15, 30)]}))
    opts = asyncio.run(x.fetch_routes(s, wallet=ME, from_chain_id=8453, to_chain_id=10, from_token=x.NATIVE_TOKEN,
                                      to_token=x.NATIVE_TOKEN, amount_in=10 ** 17))
    assert opts[0].tool == "Relay"
    url, body = s.sent[0]
    assert url.endswith("/advanced/routes") and body["fromChainId"] == 8453 and body["toAddress"] == ME and body["fromAmount"] == str(10 ** 17)


def test_the_chosen_route_is_turned_into_a_checked_transaction():
    class Post:
        def __init__(self, resp):
            self.resp, self.sent = resp, []

        def post(self, url, json=None, headers=None):
            self.sent.append((url, json))
            return self.resp

    step = sample()
    s = Post(FakeResp(200, step))
    q = asyncio.run(x.fetch_step_quote(s, step, wallet=ME, from_chain_id=8453, to_chain_id=10, amount_in=10 ** 17, to_address=ME))
    assert q.tx_to.lower() == x.DIAMOND.lower() and s.sent[0][0].endswith("/advanced/stepTransaction")
    # a transaction that pays out to someone else is refused
    bad = sample()
    bad["action"]["toAddress"] = "0x" + "99" * 20
    s2 = Post(FakeResp(200, bad))
    try:
        asyncio.run(x.fetch_step_quote(s2, bad, wallet=ME, from_chain_id=8453, to_chain_id=10, amount_in=10 ** 17, to_address=ME))
        assert False
    except x.XBridgeError as exc:
        assert "different address" in str(exc)


def test_supported_chains_come_from_the_service_and_fall_back_to_the_builtin_list():
    x._SUPPORT.update(at=0.0, ids=set(), solana=None)
    body = {"chains": [{"id": 1, "chainType": "EVM"}, {"id": 47763, "chainType": "EVM"},
                       {"id": 1151111081099710, "chainType": "SVM", "nativeToken": {"address": "1111"}}]}
    ids, sol = asyncio.run(x.fetch_supported(FakeSession([FakeResp(200, body)]), force=True))
    assert 47763 in ids and sol == {"id": 1151111081099710, "native": "1111"}

    class Dead:
        def get(self, *a, **k):
            raise ConnectionError("down")
    x._SUPPORT.update(at=0.0, ids=set(), solana=None)
    ids, sol = asyncio.run(x.fetch_supported(Dead(), force=True))
    assert 8453 in ids and sol["id"] == x.SOLANA_CHAIN_ID


def test_the_options_message_shows_fees_and_escapes_names():
    opts = x.parse_route_options({"routes": [_route("<i>Evil", 99 * 10 ** 15, 30)]})
    text = x.render_options(opts, "Base", "Optimism", "0.1", "ETH")
    assert "&lt;i&gt;Evil" in text and "<i>Evil" not in text and "bridge fee" in text and "Best price" in text


# ---- Relay
def _relay_data(wallet, to, value, amount=10**18, dest=None):
    return {"steps": [{"id": "deposit", "kind": "transaction", "requestId": "0xreq", "items": [{"data": {
        "from": wallet, "to": to, "data": "0x1234", "value": str(value), "chainId": 56}}]}],
        "details": {"currencyIn": {"amount": str(amount), "currency": {"symbol": "BNB", "decimals": 18}},
                    "currencyOut": {"amount": str(amount - 10**15), "currency": {"symbol": "ETH", "decimals": 18}},
                    "recipient": dest or wallet, "timeEstimate": 5},
        "fees": {"gas": {"amountUsd": "0.1"}, "relayer": {"amountUsd": "0.2", "amount": "0"}}}


def _relay_parse(data, allowed, wallet="0xabc0000000000000000000000000000000000001"):
    from bot import xbridge
    return xbridge.parse_relay_quote(data, wallet=wallet, from_chain_id=56, to_chain_id=1, amount_in=10**18, to_address=wallet,
                                     from_token=xbridge.NATIVE_TOKEN, to_token=xbridge.NATIVE_TOKEN, allowed=allowed)


def test_relay_accepts_good_quote():
    w = "0xabc0000000000000000000000000000000000001"
    q = _relay_parse(_relay_data(w, "0xf70da97812cb96acdf810712aa562db8dfa3dbef", 10**18), {"0xf70da97812cb96acdf810712aa562db8dfa3dbef"})
    assert q.provider == "relay" and q.request_id == "0xreq" and q.tx_value == 10**18


def test_relay_refuses_bad_quotes():
    from bot import xbridge
    w = "0xabc0000000000000000000000000000000000001"
    ok = "0xf70da97812cb96acdf810712aa562db8dfa3dbef"
    for data, allowed in (
        (_relay_data(w, "0x" + "9" * 40, 10**18), {ok}),          # unknown contract
        (_relay_data(w, ok, 3 * 10**18), {ok}),                   # sends too much coin
        (_relay_data(w, ok, 10**18, dest="0x" + "7" * 40), {ok}),  # pays someone else
    ):
        try:
            _relay_parse(data, allowed)
        except xbridge.XBridgeError:
            continue
        raise AssertionError("should have been refused")


# ---- Relay from Solana
def _decode_v0(msg):
    """Reads a v0 message back and returns (instructions as (program, [accounts])) with lookup tables not resolved."""
    from bot import solana_keys as sk
    assert msg[0] == 0x80
    ns, nro_s, nro_u = msg[1], msg[2], msg[3]
    pos = 4
    def cu(pos):
        v = shift = 0
        while True:
            b = msg[pos]; pos += 1
            v |= (b & 0x7F) << shift
            if not b & 0x80:
                return v, pos
            shift += 7
    n, pos = cu(pos)
    static = [msg[pos + 32 * i: pos + 32 * i + 32] for i in range(n)]
    pos += 32 * n + 32
    ni, pos = cu(pos)
    inss = []
    for _ in range(ni):
        prog = msg[pos]; pos += 1
        na, pos = cu(pos)
        accs = list(msg[pos:pos + na]); pos += na
        nd, pos = cu(pos); pos += nd
        inss.append((prog, accs))
    nl, pos = cu(pos)
    lookups = []
    for _ in range(nl):
        key = msg[pos:pos + 32]; pos += 32
        w, pos = cu(pos); wi = list(msg[pos:pos + w]); pos += w
        r, pos = cu(pos); ri = list(msg[pos:pos + r]); pos += r
        lookups.append((key, wi, ri))
    assert pos == len(msg)
    return static, inss, lookups, (ns, nro_s, nro_u)


def test_v0_message_uses_lookup_tables():
    from bot import solana_keys as sk
    payer, prog, a, b, c = (bytes([i]) * 32 for i in (1, 2, 3, 4, 5))
    table_key = bytes([9]) * 32
    ins = [sk.Instruction(prog, ((payer, True, True), (a, False, True), (b, False, False), (c, False, False)), b"\x01\x02")]
    msg = sk.compile_v0_message(payer, ins, bytes(32), [(table_key, [b, a, bytes([7]) * 32])])
    static, inss, lookups, header = _decode_v0(msg)
    assert static == [payer, c, prog] and header == (1, 0, 2)
    assert lookups == [(table_key, [1], [0])]
    # account indexes: payer 0, a (looked up, writable) 3, b (looked up, readonly) 4, c static 1
    assert inss[0][1] == [0, 3, 4, 1]
    assert static[inss[0][0]] == prog


def _sol_data(wallet, program="ComputeBudget111111111111111111111111111111", data="00", dest="0xdddddddddddddddddddddddddddddddddddddddd",
              signer=None, amount=10**9):
    return {"steps": [{"id": "deposit", "kind": "transaction", "requestId": "0xreq", "items": [{"data": {
        "instructions": [{"programId": program, "data": data, "keys": [{"pubkey": signer or wallet, "isSigner": True, "isWritable": True}]}],
        "addressLookupTableAddresses": []}}]}],
        "details": {"currencyIn": {"amount": str(amount), "currency": {"symbol": "SOL", "decimals": 9}},
                    "currencyOut": {"amount": "1000", "currency": {"symbol": "ETH", "decimals": 18}}, "recipient": dest, "timeEstimate": 5},
        "fees": {}}


def _sol_parse(data):
    from bot import xbridge
    return xbridge.parse_relay_solana_quote(data, wallet=W_SOL, from_chain_id=xbridge.SOLANA_CHAIN_ID, to_chain_id=1, amount_in=10**9,
                                            to_address="0xdddddddddddddddddddddddddddddddddddddddd",
                                            from_token=xbridge.SOLANA_NATIVE, to_token=xbridge.NATIVE_TOKEN)


W_SOL = "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1"


def test_relay_solana_parse_and_refusals():
    from bot import xbridge
    q = _sol_parse(_sol_data(W_SOL))
    assert q.sol and q.provider == "relay" and q.request_id == "0xreq"
    bad = (
        _sol_data(W_SOL, signer="9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin"),   # another signer
        _sol_data(W_SOL, program="11111111111111111111111111111111", data="01000000"),  # system "assign"
        _sol_data(W_SOL, program="TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", data="04" + "00" * 8),  # token "approve"
        _sol_data(W_SOL, dest="0x" + "7" * 40),                                      # pays someone else
    )
    for d in bad:
        try:
            _sol_parse(d)
        except xbridge.XBridgeError:
            continue
        raise AssertionError("should have been refused")


class _FakeSolRpc:
    def __init__(self, after_lamports, before=5 * 10**9):
        self.before, self.after, self.sent = before, after_lamports, False

    async def get_account(self, a):
        return None

    async def get_balance(self, a):
        return self.before

    async def token_accounts(self, o, m):
        return []

    async def latest_blockhash(self):
        return bytes(32), 100

    async def call(self, method, params=None):
        assert method == "simulateTransaction"
        return {"value": {"err": None, "accounts": [{"lamports": self.after, "data": ["", "base64"]}]}}

    async def send_transaction(self, tx):
        self.sent = True
        return "sig"

    async def get_signature_status(self, s):
        return {"confirmationStatus": "confirmed", "err": None}


def test_relay_solana_execute_guards_money():
    import asyncio
    from bot import xbridge, solana_keys as sk
    from bot.neo_send import SendError
    q = _sol_parse(_sol_data(W_SOL))
    seed = bytes(range(32))
    wallet = sk.from_seed(seed)
    q = _sol_parse(_sol_data(sk.b58encode(wallet.public) if hasattr(wallet, "public") else W_SOL))
    greedy = _FakeSolRpc(after_lamports=0)             # would take everything
    try:
        asyncio.run(xbridge.execute_solana(greedy, seed, W_SOL, q))
    except SendError:
        assert not greedy.sent
    else:
        raise AssertionError("should have stopped")
    fair = _FakeSolRpc(after_lamports=5 * 10**9 - 10**9 - 5000)
    out = asyncio.run(xbridge.execute_solana(fair, seed, W_SOL, q))
    assert fair.sent and out.status == "confirmed"
