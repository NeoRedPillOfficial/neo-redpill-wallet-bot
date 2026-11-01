import asyncio

import pytest

from bot import neo_send
from bot.neo_send import SendError


async def _get_version():
    raise asyncio.TimeoutError()


async def _send_transaction():
    raise asyncio.TimeoutError()


def test_read_only_failure_is_retried_and_then_succeeds():
    calls = {"n": 0}

    async def call():
        calls["n"] += 1
        if calls["n"] < 3:
            await _get_version()
        return "tx"

    assert asyncio.run(neo_send.retry_before_sending(call, attempts=3, pause=0)) == "tx"
    assert calls["n"] == 3


def test_a_failure_that_may_have_sent_is_never_retried():
    calls = {"n": 0}

    async def call():
        calls["n"] += 1
        await _send_transaction()

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(neo_send.retry_before_sending(call, attempts=3, pause=0))
    assert calls["n"] == 1


def test_unknown_failures_are_not_retried_and_giving_up_says_nothing_was_sent():
    calls = {"n": 0}

    async def odd():
        calls["n"] += 1
        raise ValueError("boom")

    with pytest.raises(ValueError):
        asyncio.run(neo_send.retry_before_sending(odd, attempts=3, pause=0))
    assert calls["n"] == 1

    async def always_down():
        await _get_version()

    with pytest.raises(SendError) as e:
        asyncio.run(neo_send.retry_before_sending(always_down, attempts=2, pause=0))
    assert "Nothing was sent" in str(e.value)
