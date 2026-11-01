import random
import time

import pytest

from bot import events


def test_parse_rain_defaults_and_options():
    r = events.parse_rain("100 NRP")
    assert (r.amount_text, r.symbol, r.people, r.network) == ("100", "NRP", 5, None)
    r = events.parse_rain("50 USDT 10 bsc")
    assert (r.people, r.network) == (10, "bsc")


@pytest.mark.parametrize("args", ["", "5", "5 NRP 0", "5 NRP 21", "5 NRP 3 5", "5 NRP a b"])
def test_parse_rain_rejects(args):
    with pytest.raises(events.EventError):
        events.parse_rain(args)


def test_rain_amounts_fixed_and_random():
    amounts, total = events.rain_amounts("10", 0, 3)
    assert (amounts, total) == ([3, 3, 3], 9)
    with pytest.raises(events.EventError):
        events.rain_amounts("1", 0, 5)
    for seed in range(50):
        amounts, total = events.rain_amounts("1000", 0, 7, "random", random.Random(seed))
        assert total == 1000 and sum(amounts) == 1000 and len(amounts) == 7
        assert min(amounts) >= 1000 // 7 // 4  # nobody gets dust
    assert len(set(events.rain_amounts("1000", 0, 7, "random", random.Random(1))[0])) > 1


def test_parse_rain_mode():
    assert events.parse_rain("100 NRP 10 random").mode == "random"
    assert events.parse_rain("100 NRP 10 bsc fixed").mode == "fixed"
    assert events.parse_rain("100 NRP").mode == "fixed"
    with pytest.raises(events.EventError):
        events.parse_rain("100 NRP random fixed")


def test_rain_claims():
    r = events.Rain("a", 1, 100, 1, "x", "bsc", "BSC", 1, "T", 18, 5, 2, 1, ends_at=time.monotonic() + 60)
    assert r.try_claim(100, 1, "o", "0x") == "owner"
    assert r.try_claim(1, 2, "a", "0x1") == "ok"
    assert r.claim_amounts[1] == 5  # no list of amounts: everyone gets each_raw
    assert r.try_claim(1, 2, "a", "0x1") == "dup"
    assert r.try_claim(2, 3, "b", "0x2") == "ok"
    assert r.try_claim(3, 4, "c", "0x3") == "full"
    r.state = "paying"
    assert r.try_claim(9, 9, "z", "0x") == "closed"


def test_parse_questions():
    qs = events.parse_questions("Q1? | a | b | c\n\nQ2? | x | y")
    assert [q.correct for q in qs] == ["a", "x"]
    for bad in ["", "Q | only", "Q | a | a", "Q | a | b | c | d | e", "\n".join(["Q | a | b"] * 11)]:
        with pytest.raises(events.EventError):
            events.parse_questions(bad)


def test_round_fastest_correct_take_the_slots():
    q = events.Question("Q", "a", ["b", "c"])
    rnd = events.Round.make(0, q, random.Random(1), slots=2)
    right = rnd.correct_pos
    wrong = (right + 1) % 3
    assert rnd.answer(1, wrong) == ("wrong", None)
    assert rnd.answer(1, right) == ("already", None)
    assert rnd.answer(2, right) == ("win", 0)
    assert rnd.answer(3, right) == ("win", 1)
    assert rnd.answer(4, right) == ("late", None)
    rnd.winners[0] = events.Win(2, 2, "b", "y", 5)
    assert not rnd.complete() and rnd.lookups_pending()
    rnd.release(3, 1)  # a failed lookup gives the slot back
    assert rnd.answer(4, right) == ("win", 1)
    rnd.closed = True
    assert rnd.answer(5, right) == ("closed", None)


def test_round_top_style_counts_everyone():
    q = events.Question("Q", "a", ["b"])
    rnd = events.Round.make(0, q, random.Random(2))
    right = rnd.correct_pos
    rnd.started = 100.0
    assert rnd.answer_top(1, right, now=101.5) == ("ok", True, 1.5)
    assert rnd.answer_top(1, right, now=102.0)[0] == "already"
    assert rnd.answer_top(2, 1 - right, now=102.0) == ("ok", False, 2.0)
    assert rnd.correct_count == 1


def test_rank_players_by_score_then_speed():
    a = events.Player(1, 1, "a", "x", correct=5, seconds=60.0)
    b = events.Player(2, 2, "b", "y", correct=5, seconds=70.0)
    c = events.Player(3, 3, "c", "z", correct=7, seconds=200.0)
    d = events.Player(4, 4, "d", "w", correct=0, seconds=0.0)
    ranked = events.rank_players({1: a, 2: b, 3: c, 4: d})
    assert [p.tg_id for p in ranked] == [3, 1, 2]  # most correct first; equal scores: the faster; zero scores never win


def test_wins_by_user():
    a, b = events.Win(1, 1, "a", "x", 5), events.Win(2, 2, "b", "y", 7)
    board = events.wins_by_user([a, b, a])
    assert [(w.tg_id, n, t) for w, n, t in board] == [(1, 2, 10), (2, 1, 7)]


def _setup(**kw):
    base = dict(id="i", chat_id=1, starter_tg_id=2, starter_user_id=3, starter_label="x", slug="bsc",
                network_name="BSC", token_id=1, symbol="T", decimals=0, amount_text="10")
    base.update(kw)
    return events.QuizSetup(**base)


def test_quiz_plan_fixed_per_question():
    setup = _setup(style="question", winners=2, amount_text="10")
    qs = [events.Question("q", "a", ["b"])] * 3
    setup.plan(qs)
    assert setup.amounts == [10] * 6 and setup.total_raw == 60 and setup.top_raw == 10
    assert setup.prize_for(1, 1) == 10


def test_quiz_plan_random_pot_is_split_exactly():
    setup = _setup(style="top", winners=4, amount_text="1000", mode="random")
    setup.plan([events.Question("q", "a", ["b"])] * 2)
    assert len(setup.amounts) == 4 and setup.total_raw == 1000 and min(setup.amounts) >= 1000 // 4 // 4
    tiny = _setup(style="question", winners=3, amount_text="5", mode="random")
    with pytest.raises(events.EventError):
        tiny.plan([events.Question("q", "a", ["b"])] * 3)  # 9 slots, 5 units: too small to share


def test_codes():
    assert events.normalize_code("r7k2q") == "R-7K2Q"
    assert events.normalize_code(" R-7K2Q ") == "R-7K2Q"
    for not_a_code in ("100", "NRP", "R-7K0Q", "X-7K2Q", "R-7K2"):
        assert events.normalize_code(not_a_code) is None
    taken = set()
    for _ in range(300):
        code = events.new_code("rain", taken)
        assert code not in taken and events.normalize_code(code) == code
        taken.add(code)
    assert events.new_code("quiz", set()).startswith("Q-")


def test_timer_parsing():
    assert [events.parse_seconds(t) for t in ("45", "45s", "2m", "1 min", "10 sec")] == [45, 45, 120, 60, 10]
    for bad in ("", "3", "601", "abc", "0"):
        with pytest.raises(events.EventError):
            events.parse_seconds(bad)
    assert events.format_seconds(60) == "1 min" or "60" in events.format_seconds(60)


def test_ranked_rewards_and_flexible_winners():
    assert events.parse_rank_amounts("1000, 500 250", 0) == [1000, 500, 250]
    amounts, winners = events.plan_amounts("top", "ranked", 1, "1000, 500", 0, 4)
    assert (amounts, winners) == ([1000, 500], 2)
    amounts, winners = events.plan_amounts("question", "ranked", 1, "9, 3", 0, 3)
    assert (amounts, winners) == ([9, 3] * 3, 2)
    amounts, winners = events.plan_amounts("top", "fixed", 7, "5", 0, 2)
    assert amounts == [5] * 7 and winners == 7


def test_claim_board_roundtrip():
    b = events.ClaimBoard("Q-1", -5, 1, 2, "neo_x", "Neo X", 3, "NRP", 8, "head",
                          {10: events.Prize("@a", 100, "2 won", True), 11: events.Prize("@b", 50)},
                          message_id=9, expires_at=time.time() + 100, code="Q-AAAA")
    c = events.ClaimBoard.from_dict(b.to_dict())
    assert c.prizes[10].claimed and not c.prizes[11].claimed and c.prizes[11].amount == 50
    assert not c.expired() and events.ClaimBoard.from_dict({**b.to_dict(), "expires_at": 1}).expired()
