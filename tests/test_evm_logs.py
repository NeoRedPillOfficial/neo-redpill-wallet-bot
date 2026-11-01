"""eth_getLogs fallback for RPC endpoints that insist on a contract address."""
from bot.evm_rpc import EvmRpc, EvmRpcError, TRANSFER_TOPIC, pad_address

WALLET = "0x" + "11" * 20
TOKEN_A, TOKEN_B = "0x" + "aa" * 20, "0x" + "bb" * 20


def _entry(token, log_index=0):
    return {
        "address": token,
        "topics": [TRANSFER_TOPIC, "0x" + pad_address("0x" + "22" * 20), "0x" + pad_address(WALLET)],
        "data": "0x" + format(5, "064x"),
        "transactionHash": "0x" + "cd" * 32,
        "logIndex": hex(log_index),
        "blockNumber": hex(100),
    }


class Strict(EvmRpc):
    """Refuses a query without an address, like publicnode does."""

    def __init__(self):
        super().__init__("https://example.invalid/rpc")
        self.queries = []

    async def call(self, method, params=None):
        query = params[0]
        self.queries.append(query)
        if "address" not in query:
            raise EvmRpcError("eth_getLogs: {'code': -32701, 'message': 'Please specify an address in your request'}")
        return [_entry(a, i) for i, a in enumerate(query["address"])]


class Open(EvmRpc):
    def __init__(self):
        super().__init__("https://example.invalid/rpc")
        self.queries = []

    async def call(self, method, params=None):
        self.queries.append(params[0])
        return [_entry(TOKEN_A)]


async def test_open_endpoint_is_asked_without_an_address():
    rpc = Open()
    logs = await rpc.get_transfer_logs(1, 200, [WALLET], contracts=[TOKEN_A])
    assert len(logs) == 1 and "address" not in rpc.queries[0] and not rpc.address_required


async def test_strict_endpoint_switches_to_known_contracts_and_remembers():
    rpc = Strict()
    logs = await rpc.get_transfer_logs(1, 200, [WALLET], contracts=[TOKEN_A, TOKEN_B])
    assert rpc.address_required
    assert sorted(lg.token for lg in logs) == [TOKEN_A, TOKEN_B]
    assert "address" in rpc.queries[-1]
    before = len(rpc.queries)
    await rpc.get_transfer_logs(201, 300, [WALLET], contracts=[TOKEN_A])
    assert len(rpc.queries) == before + 1  # no second failed attempt without an address


async def test_strict_endpoint_with_no_known_tokens_returns_nothing():
    rpc = Strict()
    assert await rpc.get_transfer_logs(1, 200, [WALLET], contracts=[]) == []


async def test_other_errors_still_raise():
    class Broken(Open):
        async def call(self, method, params=None):
            raise EvmRpcError("eth_getLogs: {'code': -32000, 'message': 'boom'}")

    try:
        await Broken().get_transfer_logs(1, 2, [WALLET], contracts=[TOKEN_A])
    except EvmRpcError:
        return
    raise AssertionError("should have raised")


async def test_many_contracts_are_split_into_chunks():
    rpc = Strict()
    contracts = ["0x" + format(i, "040x") for i in range(45)]
    await rpc.get_transfer_logs(1, 2, [WALLET], contracts=contracts)
    sizes = [len(q["address"]) for q in rpc.queries if "address" in q]
    assert sizes == [20, 20, 5]


async def test_limit_exceeded_also_switches_to_known_contracts():
    class Limited(Strict):
        async def call(self, method, params=None):
            query = params[0]
            self.queries.append(query)
            if "address" not in query:
                raise EvmRpcError("eth_getLogs: {'code': -32005, 'message': 'limit exceeded'}")
            return [_entry(a, i) for i, a in enumerate(query["address"])]

    rpc = Limited()
    logs = await rpc.get_transfer_logs(1, 200, [WALLET], contracts=[TOKEN_A])
    assert rpc.address_required and len(logs) == 1


async def test_limit_exceeded_on_known_contracts_halves_the_block_range():
    class Narrow(Strict):
        async def call(self, method, params=None):
            q = params[0]
            self.queries.append(q)
            if "address" not in q:
                raise EvmRpcError("eth_getLogs: {'code': -32005, 'message': 'limit exceeded'}")
            if int(q["toBlock"], 16) - int(q["fromBlock"], 16) > 300:
                raise EvmRpcError("eth_getLogs: {'code': -32005, 'message': 'limit exceeded'}")
            return [_entry(TOKEN_A)]

    rpc = Narrow()
    logs = await rpc.get_transfer_logs(1, 1000, [WALLET], contracts=[TOKEN_A])
    assert len(logs) >= 4  # the 1000-block range was split into small pieces that all answered


async def test_second_url_is_used_when_the_first_refuses():
    calls = []

    class Two(EvmRpc):
        async def _call_one(self, url, method, params):
            calls.append(url)
            if "first" in url:
                raise EvmRpcError("eth_getTransactionReceipt: {'code': -32602, 'message': 'Archive requests require a personal token'}")
            return {"status": "0x1"}

    rpc = Two("https://first.example/rpc, https://second.example/rpc")
    assert await rpc.get_receipt("0x" + "ab" * 32) == {"status": "0x1"}
    assert calls == ["https://first.example/rpc", "https://second.example/rpc"]


async def test_all_urls_failing_reports_every_reason():
    class Down(EvmRpc):
        async def _call_one(self, url, method, params):
            raise EvmRpcError("limit exceeded" if "b." in url else "Archive requests")

    try:
        await Down("https://a.example, https://b.example").call("eth_blockNumber")
    except EvmRpcError as exc:
        assert "Archive" in str(exc) and "limit exceeded" in str(exc)
        return
    raise AssertionError("should have raised")


def test_duplicate_env_keys_are_found():
    from bot.config import duplicate_env_keys

    text = "A=1\n# B=2\nBSC_RPC=\n\nX=1\n   BSC_RPC=https://x\nexport A=3\n"
    assert duplicate_env_keys(text) == {"BSC_RPC": [3, 6], "A": [1, 7]}
