"""The AI Assistant: finds the right Help passages, refuses off-topic questions, and falls back when Claude fails."""
import os
import tempfile

from bot import assistant, helptext

NETS = [("Neo N3", "GAS"), ("BNB Smart Chain", "BNB")]


def _index():
    return assistant.get_index(NETS, True)


def _topics(question):
    top, _ = _index().search(question)
    return [c.topic for c in top]


def test_questions_find_the_right_guide():
    assert _topics("How do I add a token?")[0] == "tokens"
    assert _topics("how to create an airdrop")[0] == "airdrop"
    assert _topics("how do I bridge GAS")[0] == "bridge"
    assert _topics("how to start a rain")[0] == "rain"
    assert _topics("can i import my metamask wallet")[0] == "keys"
    assert _topics("how does lucky draw work")[0] == "airdrop"
    assert _topics("who is support")[0] == "support"
    assert _topics("কিভাবে টোকেন যোগ করব")[0] == "tokens"  # Bangla


def test_off_topic_questions_get_no_answer():
    assert _topics("what is the weather today") == []
    assert _topics("who won the football match") == []
    assert _topics("") == []


def test_overview_and_advice_are_recognised():
    assert assistant.wants_overview("what can this bot do") and assistant.wants_overview("hello")
    assert not assistant.wants_overview("how do I send")
    assert assistant.is_advice("bitcoin price prediction") and assistant.is_advice("should I buy NRP")
    assert not assistant.is_advice("how do I send tokens")


def test_local_answer_is_from_help_and_names_the_guide():
    labels = dict(helptext.topic_keys(True))
    text = assistant.local_answer("how do I add a token", _index(), lambda t: labels.get(t, ""))
    assert "contract address" in text and "Full guide: Help" in text and "<" not in text  # plain text, no HTML tags
    assert len(text) < 2000


def test_every_help_topic_is_in_the_knowledge():
    topics = {c.topic for c in _index().chunks}
    assert {k for k, _ in helptext.topic_keys(True)} <= topics and "main" in topics
    assert all(len(c.text) < 1200 for c in _index().chunks)


def test_the_latest_features_are_answerable():
    assert _topics("how do I use the daily check in task")[0] == "airdrop"
    assert _topics("what network fee will I pay for a tip")[0] in ("tip", "send")
    assert _topics("how do I contact aziz")[0] == "support"


def test_question_detection_for_replies():
    assert assistant.looks_like_question("how do I send?") and assistant.looks_like_question("what is rain")
    assert assistant.looks_like_question("কিভাবে পাঠাবো")
    assert not assistant.looks_like_question("thanks!") and not assistant.looks_like_question("lol nice")


def test_group_messages_for_the_bot():
    f = lambda text, reply=None: assistant.group_question(text, reply, 99, "NeoRedPillWalletBot")
    assert f("@NeoRedPillWalletBot how do I tip?") == (True, True, "how do I tip?")
    assert f("hey @neoredpillwalletbot, what is rain")[:2] == (True, True)  # the name works in any case
    assert f("how do I tip?", 99)[:2] == (True, False)  # a question in reply to the bot
    assert f("thanks!", 99)[:2] == (False, False)  # not a question: stay quiet
    assert f("how do I tip?", 5)[:2] == (False, False)  # a reply to someone else
    assert f("hello everyone")[:2] == (False, False)


def test_limiter_gap_and_daily_cap():
    lim = assistant.Limiter(gap=6, daily=2)
    assert not lim.too_fast(1, now=100.0) and lim.too_fast(1, now=103.0) and not lim.too_fast(1, now=107.0)
    assert not lim.too_fast(2, now=100.0)  # another person is not slowed
    assert lim.claude_ok(1, 5) and lim.claude_ok(1, 5) and not lim.claude_ok(1, 5)
    assert lim.claude_ok(1, 6)  # a new day


class FakeResp:
    def __init__(self, status, data):
        self.status, self._data = status, data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._data


class FakeSession:
    def __init__(self, resp=None, boom=False):
        self.resp, self.boom, self.sent = resp, boom, None

    def post(self, url, json=None, headers=None):
        self.sent = (url, json, headers)
        if self.boom:
            raise RuntimeError("network down")
        return self.resp


async def test_claude_gets_the_whole_guide_the_chat_so_far_and_the_live_prices():
    ok = FakeSession(FakeResp(200, {"content": [{"type": "text", "text": "**Tap** Add / Remove token, then `Add a token`."}]}))
    knowledge = assistant.knowledge_text(_index())
    history = [("how do I tip?", "Reply to a message with /tip 5 GAS.")]
    text = await assistant.ask_claude(ok, "key", "claude-haiku-5-5", "give me an example", knowledge, "@aziz1683",
                                      "NRP: $0.000036", history)
    assert text == "**Tap** Add / Remove token, then `Add a token`."
    url, body, headers = ok.sent
    assert url.endswith("/v1/messages") and headers["x-api-key"] == "key"
    system = body["system"]
    assert "<knowledge>" in system[1]["text"] and "Tipping in groups" in system[1]["text"]  # the whole guide, not a few passages
    assert system[1]["cache_control"] == {"type": "ephemeral"}  # the big part is cached between questions
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["user", "assistant", "user"]  # the earlier turn is part of the conversation
    assert body["messages"][0]["content"] == "how do I tip?" and body["messages"][1]["content"].startswith("Reply to")
    last = body["messages"][-1]["content"]
    assert "give me an example" in last and "NRP: $0.000036" in last and "@aziz1683" in last
    assert "private keys" in system[0]["text"] and "ollow-up" in system[0]["text"] and "typos" in system[0]["text"]


async def test_claude_failures_return_none_so_the_builtin_answer_is_used():
    for sess in (FakeSession(FakeResp(429, {})), FakeSession(boom=True), FakeSession(FakeResp(200, {"content": []}))):
        assert await assistant.ask_claude(sess, "k", "m", "q", "kb", "s") is None


def test_the_whole_guide_fits_in_one_prompt():
    kb = assistant.knowledge_text(_index())
    assert 8_000 < len(kb) < 40_000 and "Airdrops" in kb and "Support" in kb


def test_clean_reply_trims_long_text():
    assert len(assistant.clean_reply("word " * 1000)) <= 3610


# ------------------------------------------------------------------ live prices
from decimal import Decimal as D

PRICES = {"NRP": D("0.000036"), "GAS": D("1.25"), "BNB": D("745.21"), "ETH": D("2411.42")}


def _price(question):
    q = assistant.parse_price_question(question, PRICES.keys())
    return None if q is None else assistant.price_answer(q, PRICES)


def test_price_of_an_amount_is_calculated():
    text = _price("what is the price of 100 NRP?")
    assert text.startswith("100 NRP ≈ $0.0036") and "1 NRP = $0.000036" in text and "Not financial advice" in text
    assert _price("how much is 5 bnb in usd").startswith("5 BNB ≈ $3,726.05")
    assert _price("how much is 2.5 BNB worth").startswith("2.5 BNB ≈ $1,863.02")
    assert _price("100 nrp").startswith("100 NRP ≈ $0.0036")  # just an amount and a coin
    assert _price("2.5k nrp price").startswith("2,500 NRP ≈ $0.09")


def test_price_of_one_coin_and_conversions():
    assert _price("gas price").startswith("1 GAS = $1.25")
    assert _price("NRP price").startswith("1 NRP = $0.000036")
    text = _price("1000 nrp to gas")
    assert text.startswith("1,000 NRP ≈ 0.0288 GAS")


def test_a_typo_in_the_coin_name_is_understood_and_said():
    text = _price("100 NLP price")
    assert text.startswith("I read NLP as NRP.") and "100 NRP ≈ $0.0036" in text


def test_questions_that_are_not_price_questions_are_left_alone():
    assert _price("how do I send 100 NRP") is None
    assert _price("how to tip 5 gas") is None
    assert _price("how to add a token") is None
    assert _price("NRP") is None


def test_a_coin_without_a_price_says_so():
    q = assistant.parse_price_question("100 abc price", ["ABC", "NRP"])
    assert "don't have a live price for ABC" in assistant.price_answer(q, {"NRP": D(1)})


def test_typos_in_ordinary_questions_still_find_the_guide():
    assert _topics("how do i tranfer money to my friend")[0] == "send"
    assert "send" in _topics("how to withdrwa")[:2]


# ------------------------------------------------------------------ conversation
def _chat():
    mem, labels = assistant.Memory(), dict(helptext.topic_keys(True))
    who = ("p", 1)

    def ask(question, now=None):
        if assistant.wants_reset(question):
            mem.clear(who)
            return "reset"
        small = assistant.smalltalk(question)
        if small:
            return small
        text, topic = assistant.local_reply(question, _index(), mem.topic(who, now), mem.last_question(who, now),
                                            lambda t: labels.get(t, ""))
        if text:
            mem.add(who, question, text, topic, now)
        return text

    return ask, mem, who


def test_a_follow_up_example_is_about_the_topic_of_the_chat_not_the_words_of_the_message():
    ask, _mem, _who = _chat()
    assert "Tipping in groups" in ask("how do I tip in a group?")
    example = ask("give me an example")
    assert "/tip 5 GAS" in example and "Tap 💸 Send" not in example  # the tip example, not the send steps
    send = ask("how do I send money")
    assert "Sending" in send
    assert "Example: you want to send 5 NRP" in ask("can you give an example?")


def test_the_memory_keeps_the_topic_for_short_follow_ups():
    ask, mem, who = _chat()
    ask("how do I create an airdrop?")
    assert mem.topic(who) == "airdrop"
    again = ask("explain again")
    assert again and "irdrop" in again
    assert "Example: you want 100 new members" in ask("an example please")
    assert len(mem.history(who)) == 3


def test_a_new_question_changes_the_topic():
    ask, mem, who = _chat()
    ask("how do I tip?")
    ask("how do I add a token?")
    assert mem.topic(who) == "tokens"
    assert "Example: you want FRANK" in ask("example")


def test_small_talk_and_reset():
    ask, mem, who = _chat()
    ask("how do I tip?")
    assert "welcome" in ask("thanks!").lower()
    assert "AI Assistant" in ask("who are you?")
    assert ask("new chat") == "reset" and mem.topic(who) is None and mem.history(who) == []
    assert "Which part of the bot" in ask("give me an example")  # nothing to give an example of any more: it asks


def test_memory_forgets_after_a_while_and_keeps_only_recent_turns():
    mem = assistant.Memory(turns=3, ttl=100)
    for i in range(5):
        mem.add("a", f"q{i}", f"a{i}", "tip", now=float(i))
    assert [q for q, _ in mem.history("a", now=5.0)] == ["q2", "q3", "q4"]
    assert mem.topic("a", now=50.0) == "tip" and mem.topic("a", now=500.0) is None and mem.history("a", now=500.0) == []
    mem.add("b", "q", "a", None, now=0.0)
    mem.add("c", "q2", "a2", "rain", now=0.0)
    assert mem.topic("b", now=1.0) is None and mem.topic("c", now=1.0) == "rain"  # people don't share memory


def test_replying_to_my_answer_continues_the_conversation_in_a_group():
    f = lambda text, reply=None, cont=False: assistant.group_question(text, reply, 99, "NeoRedPillWalletBot", cont)
    assert f("give me an example", 99, True)[:2] == (True, False)  # not a question word, but it answers my answer
    assert f("give me an example", 99, False)[:2] == (False, False)  # a reply to some other bot message
    assert f("lol", 5, True)[:2] == (False, False)  # a reply to someone else


# ------------------------------------------------------------------ answering without being mentioned
def test_clear_questions_about_the_bot_are_answered_on_their_own():
    auto = lambda text: assistant.should_auto_answer(text, _index())
    assert auto("How do I tip someone in this group?")
    assert auto("how can I create an airdrop?")
    assert auto("what is rain and how do I start one?")
    assert auto("how do I add a token to this wallet bot?")
    assert auto("how do I bridge GAS to Neo X?")
    assert auto("Is the NeoRedPill bot safe? where are my keys stored?")


def test_ordinary_group_talk_is_left_alone():
    auto = lambda text: assistant.should_auto_answer(text, _index())
    assert not auto("gm everyone")  # not a question
    assert not auto("tip")  # too short
    assert not auto("what is the weather today?")  # not about the bot
    assert not auto("how do I send money to my brother in a bank?")  # "send" alone is not enough without the bot named
    assert not auto("did you see how the rain was yesterday? so heavy")  # one word, no good match
    assert not auto("how do I tip? see https://example.com/some/long/page")  # messages with links are skipped
    assert not auto("we should add a token to the exchange list, what do you think about the price?")
    assert not auto("how do I " + "very " * 100 + "tip")  # a long rant


def test_generic_features_need_the_bot_to_be_named():
    auto = lambda text: assistant.should_auto_answer(text, _index())
    assert not auto("how do I deposit money?")
    assert auto("how do I deposit money in this bot?")


def test_automatic_answers_are_rate_limited():
    lim = assistant.AutoLimiter(chat_gap=25, user_gap=60, daily=2)
    assert lim.allow(1, 10, 5, now=100.0)
    assert not lim.allow(1, 11, 5, now=110.0)  # the same chat too soon
    assert lim.allow(2, 11, 5, now=110.0)  # another chat has its own clock
    assert lim.allow(1, 11, 5, now=130.0)  # another person after the chat gap
    assert not lim.allow(1, 12, 5, now=500.0)  # the daily cap for the chat is used up
    assert lim.allow(1, 12, 6, now=600.0)  # a new day


def test_the_off_switch_is_saved():
    from bot import aistore

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "ai_groups.json")
        assert aistore.load_off(path) == set()
        aistore.save_off({-100123, -100456}, path)
        assert aistore.load_off(path) == {-100123, -100456}


def test_free_provider_settings():
    url, model = assistant.provider_settings("groq")
    assert "groq.com" in url and model
    url, model = assistant.provider_settings("gemini", model="my-model")
    assert "generativelanguage" in url and model == "my-model"
    assert assistant.provider_settings("unknown")[0] == assistant.PROVIDERS["gemini"][0]
    assert assistant.provider_settings("groq", base_url="http://x/y")[0] == "http://x/y"


def test_google_key_goes_in_googles_own_header_too():
    import asyncio
    class R:
        status = 200
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def json(self): return {"choices": [{"message": {"content": "ok"}}]}
    class S:
        def __init__(self): self.h = None
        def post(self, url, json=None, headers=None): self.h = headers; return R()
    g = S(); asyncio.run(assistant.ask_free(g, "https://generativelanguage.googleapis.com/x", "KEY", "m", "q", "K", "s"))
    assert g.h["x-goog-api-key"] == "KEY" and g.h["Authorization"] == "Bearer KEY"
    o = S(); asyncio.run(assistant.ask_free(o, "https://api.groq.com/x", "KEY", "m", "q", "K", "s"))
    assert "x-goog-api-key" not in o.h


def test_replies_keep_bold_and_code_but_stay_safe():
    raw = "Yes ✅ tap **🌧 Rain** then send `/rain R-7K2Q`.\n\n## Steps\n- one <b>x</b>\n* two"
    t = assistant.clean_reply(raw)
    assert "##" not in t and t.count("• ") == 2
    h = assistant.pretty(t)
    assert "<b>🌧 Rain</b>" in h and "<code>/rain R-7K2Q</code>" in h
    assert "&lt;b&gt;x&lt;/b&gt;" in h  # the model's own tags are escaped
    assert "**" not in assistant.clean_reply("cut **off")


def test_the_ai_stays_quiet_on_skip_and_the_filter_is_loose_but_not_wild():
    assert assistant.is_skip("SKIP") and assistant.is_skip("skip.") and not assistant.is_skip("Skipping is not possible")
    idx = _index()
    assert assistant.might_be_about_bot("Is it possible to rain where NeoRedPill bot is not added?", idx)
    assert not assistant.might_be_about_bot("what time is the match tonight?", idx)
    assert not assistant.might_be_about_bot("lol that was funny", idx)
    lim = assistant.AutoLimiter(chat_gap=25, user_gap=60, daily=5)
    assert lim.allow(1, 2, 5, now=100.0) and not lim.allow(1, 3, 5, now=101.0)
    lim.release(1, 2)
    assert lim.allow(1, 3, 5, now=102.0)
