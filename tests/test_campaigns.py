import json

from bot import campaigns as cm


def make(**kw):
    base = dict(id="abc123", creator_tg_id=1, creator_user_id=1, creator_label="@boss", title="Launch", description="",
                slug="neo_n3", network_name="Neo N3", token_id=1, symbol="NRP", decimals=8, seconds=7 * 86400)
    base.update(kw)
    c = cm.Campaign(**base)
    c.state, c.starts, c.ends = "open", 1000.0, 1000.0 + 7 * 86400
    return c


def test_input_checks():
    assert cm.x_handle("@Neo_Red") == "Neo_Red"
    assert cm.x_handle("https://x.com/NeoRed/status/1") == "NeoRed"
    assert cm.ig_handle("https://www.instagram.com/neo.red/") == "neo.red"
    for bad in ("", "a b", "x" * 20, "https://evil.com/neo"):
        try:
            cm.x_handle(bad)
            assert False, bad
        except cm.CampaignError:
            pass
    assert cm.tg_target("https://t.me/NeoRedPill") == "@NeoRedPill"
    for bad in ("https://t.me/+AbCdEf", "hello", "@ab"):
        try:
            cm.tg_target(bad)
            assert False, bad
        except cm.CampaignError:
            pass
    assert cm.parse_duration("7 days") == 7 * 86400
    assert cm.parse_duration("10m") == 600 and cm.parse_duration("15 min") == 900
    assert cm.parse_duration("1h") == 3600 and cm.parse_duration("1h30m") == 5400
    assert cm.parse_duration("2d 6h") == 2 * 86400 + 6 * 3600
    for bad in ("0m", "91d", "x", "7", "10 apples", "", "30s"):
        try:
            cm.parse_duration(bad)
            assert False, bad
        except cm.CampaignError:
            pass
    assert cm.format_duration(5400) == "1 hour 30 minutes" and cm.format_duration(86400) == "1 day"
    assert cm.x_post_url("https://x.com/a/status/1")
    try:
        cm.x_post_url("https://example.com/a")
        assert False
    except cm.CampaignError:
        pass


def test_points_rank_and_ties():
    c = make()
    a = c.add_task("tg:join", "Join", "https://t.me/x", "auto", "@x", 10)
    b = c.add_task("x:follow", "Follow", "https://x.com/x", "approve", "", 20)
    p1, p2, p3 = c.join(1, "one"), c.join(2, "two"), c.join(3, "three")
    c.mark(p1, a.id, "done", now=10)
    c.mark(p2, a.id, "done", now=5)
    c.mark(p2, b.id, "done", "@two", now=6)
    c.mark(p3, b.id, "pending", "@three", now=7)
    assert c.points_of(p2) == 30 and c.points_of(p1) == 10 and c.points_of(p3) == 0
    assert [p.tg_id for p, _ in c.ranking()] == [2, 1]  # 3 has no points yet
    c.mark(p3, a.id, "done", now=20)  # ties with p1 at 10; p1 got there first
    assert [p.tg_id for p, _ in c.ranking()] == [2, 1, 3]
    assert c.rank_of(3) == 3 and c.rank_of(99) is None
    assert [x[2].handle for x in c.pending_reviews()] == ["@three"]


def test_handle_uniqueness_ignores_rejected():
    c = make()
    t = c.add_task("x:follow", "F", "u", "approve")
    p1, p2 = c.join(1, "a"), c.join(2, "b")
    c.mark(p1, t.id, "pending", "@Same")
    assert c.handle_taken(t.id, "@same", 2)
    assert not c.handle_taken(t.id, "@same", 1)
    c.mark(p1, t.id, "rejected", "@Same")
    assert not c.handle_taken(t.id, "@same", 2)


def test_completion_reward_caps_and_once_per_person():
    c = make(comp_mode="instant", comp_each=500, comp_max=2)
    t1 = c.add_task("tg:join", "J", "u", "auto", "@x")
    people = [c.join(i, f"p{i}") for i in (1, 2, 3)]
    assert c.claim_completion(people[0], now=2000) is None  # nothing done yet
    for p in people:
        c.mark(p, t1.id, "done", now=2000)
    d1 = c.claim_completion(people[0], now=2001)
    assert d1 and d1.raw == 500 and d1.kind == "completion"
    assert c.claim_completion(people[0], now=2002) is None  # once only
    assert c.claim_completion(people[1], now=2003)
    assert c.claim_completion(people[2], now=2004) is None  # places are full
    assert c.comp_left() == 0
    # a task added later: new finishers need it too, but earlier winners keep their reward
    t2 = c.add_task("web:visit", "L", "u", "instant")
    assert not c.all_done(people[0]) and c.comp_count() == 2


def test_no_reward_after_end_or_when_off():
    c = make(comp_mode="none")
    t = c.add_task("web:visit", "L", "u", "instant")
    p = c.join(1, "a")
    c.mark(p, t.id, "done", now=2000)
    assert c.claim_completion(p, now=2000) is None
    c2 = make(comp_mode="manual", comp_each=1, comp_max=1)
    t2 = c2.add_task("web:visit", "L", "u", "instant")
    p2 = c2.join(1, "a")
    c2.mark(p2, t2.id, "done", now=2000)
    assert c2.claim_completion(p2, now=c2.ends + 1) is None


def test_top_prizes_settle_once_and_follow_rank():
    c = make(top_mode="auto", top_prizes=[100, 50, 25])
    t = c.add_task("tg:join", "J", "u", "auto", "@x", 10)
    for i, when in ((1, 30), (2, 10), (3, 20)):
        c.mark(c.join(i, f"p{i}"), t.id, "done", now=when)
    assert c.finish_if_due(c.ends + 1) and c.state == "ended"
    made = c.settle_top(now=5)
    assert [(d.tg_id, d.raw, d.rank) for d in made] == [(2, 100, 1), (3, 50, 2), (1, 25, 3)]
    assert c.settle_top() == []  # never twice
    assert len(c.owed()) == 3


def test_fewer_people_than_prizes_and_budget():
    c = make(top_mode="manual", top_prizes=[100, 50, 25], comp_mode="instant", comp_each=10, comp_max=5)
    assert c.budget() == 100 + 50 + 25 + 50
    t = c.add_task("web:visit", "L", "u", "instant")
    c.mark(c.join(1, "a"), t.id, "done", now=2000)
    c.state = "ended"
    assert len(c.settle_top()) == 1
    c.dues[0].status = "paid"
    assert c.unpaid_budget() == 0


def test_unpaid_budget_reserves_until_paid():
    c = make(comp_mode="instant", comp_each=10, comp_max=5, top_mode="auto", top_prizes=[100])
    assert c.unpaid_budget() == 150
    d = cm.Due(1, "a", 10, "completion", 0, "paid")
    c.dues.append(d)
    assert c.unpaid_budget() == 140


def test_remove_task_clears_progress():
    c = make()
    t = c.add_task("web:visit", "L", "u", "instant")
    p = c.join(1, "a")
    c.mark(p, t.id, "done")
    assert c.remove_task(t.id) and c.points_of(p) == 0 and t.id not in p.entries


def test_task_limit():
    c = make()
    for _ in range(cm.MAX_TASKS):
        c.add_task("web:visit", "L", "u", "instant")
    try:
        c.add_task("web:visit", "L", "u", "instant")
        assert False
    except cm.CampaignError:
        pass


def test_save_load_round_trip(tmp_path):
    c = make(comp_mode="manual", comp_each=7, comp_max=3, top_mode="auto", top_prizes=[9, 8])
    t = c.add_task("x:follow", "F", "https://x.com/a", "approve", "", 20)
    p = c.join(5, "five")
    c.mark(p, t.id, "pending", "@five", now=12)
    c.dues.append(cm.Due(5, "five", 7, "completion", 0, "failed", "no wallet", 13))
    path = str(tmp_path / "c.json")
    cm.save({c.id: c}, path)
    json.load(open(path))
    back = cm.load(path)[c.id]
    assert back.title == "Launch" and back.top_prizes == [9, 8] and back.comp_each == 7
    assert back.participants[5].entries[t.id].handle == "@five"
    assert back.dues[0].status == "failed" and back.tasks[0].points == 20
    assert cm.load(str(tmp_path / "missing.json")) == {}


def test_texts_are_html_safe_and_short():
    c = make(title="<b>Hi</b>", comp_mode="instant", comp_each=500_000_000, comp_max=10, top_mode="manual", top_prizes=[1, 2], funding="funded")
    c.add_task("tg:join", "Join <us>", "u", "auto", "@x")
    p = c.join(1, "<evil>")
    fmt = lambda raw: str(raw / 1e8)
    for text in (cm.event_text(c, p, fmt, 1500), cm.board_text(c, fmt, 1), cm.manage_text(c, fmt, 1500),
                 cm.participants_text(c), cm.dues_text(c, fmt), cm.task_text(c, c.tasks[0], None)):
        assert "<evil>" not in text and "<us>" not in text and "<b>Hi</b>" not in text
        assert len(text) < 4096
    assert "Funded" in cm.funding_badge(c) and "Pay when due" in cm.funding_badge(make())
    assert cm.format_left(90000).startswith("1d") and cm.format_left(0) == "ended"


def test_old_save_with_days_still_loads(tmp_path):
    c = make()
    d = cm.to_dict(c)
    d.pop("seconds")
    d["days"] = 3
    back = cm.from_dict(d)
    assert back.seconds == 3 * 86400


def test_platform_links_and_titles():
    assert cm.platform_url("x", "follow", "@NeoRed") == "https://x.com/NeoRed"
    assert cm.platform_url("gh", "star", "https://github.com/neo-project/neo") .endswith("/neo")
    assert cm.platform_url("yt", "sub", "https://www.youtube.com/@neo").startswith("https://")
    assert cm.platform_url("rd", "join", "https://www.reddit.com/r/NEO/")
    assert cm.platform_url("dc", "join", "https://discord.gg/abc")
    for plat, act, bad in (("x", "like", "https://example.com/p"), ("yt", "sub", "https://x.com/a"), ("rd", "upvote", "hello")):
        try:
            cm.platform_url(plat, act, bad)
            assert False, bad
        except cm.CampaignError:
            pass
    assert cm.task_title("x", "follow", "https://x.com/NeoRed") == "Follow @NeoRed on X (Twitter)"
    assert "post" in cm.task_title("x", "repost", "https://x.com/a/status/1")
    assert cm.task_title("web", "visit", "https://www.neoredpill.com/x") == "Visit neoredpill.com"
    assert cm.profile_name("dc", "https://discord.com/users/Neo_Fan") == "Neo_Fan"
    assert cm.profile_name("rd", "u/NeoFan") == "NeoFan" and cm.profile_name("rd", "NeoFan") == "NeoFan"
    assert cm.profile_name("x", "@neo") == "@neo"
    for kind in cm.KINDS:
        assert ":" in kind
    assert len(cm.PLATFORMS) >= 10


def test_every_kind_has_text_that_fits():
    for kind in cm.KINDS:
        c = make(top_mode="manual", top_prizes=[1])
        t = c.add_task(kind, "Do it", "https://x.com/a", "approve", "@x", 10, "42")
        assert len(cm.task_text(c, t, None)) < 4096


def test_no_points_without_a_leaderboard():
    c = make(comp_mode="instant", comp_each=5, comp_max=2)
    assert not c.uses_points()
    t = c.add_task("x:follow", "Follow", "u", "instant", "", 0)
    p = c.join(1, "a")
    fmt = lambda raw: str(raw)
    assert "pts" not in cm.event_text(c, p, fmt, 1500) and "points" not in cm.event_text(c, p, fmt, 1500).lower()
    assert "of 1 tasks" in cm.event_text(c, p, fmt, 1500)
    c.mark(p, t.id, "done", now=1600)
    assert "participants" in cm.board_text(c, fmt, 1) and "pts" not in cm.board_text(c, fmt, 1)
    assert "pts" not in cm.board_text(c, fmt, 1) and "pts" not in cm.participants_text(c) and "pts" not in cm.manage_text(c, fmt, 1500)
    lb = make(top_mode="manual", top_prizes=[5])
    assert lb.uses_points()
    lb.add_task("web:visit", "V", "u", "instant", "", 10)
    assert "10 pts" in cm.manage_text(lb, fmt, 1500)


def test_lucky_draw_picks_only_finishers_once():
    import random
    c = make(raffle_mode="auto", raffle_each=9, raffle_winners=2)
    t1 = c.add_task("web:visit", "A", "u", "instant", "", 0)
    t2 = c.add_task("web:visit", "B", "u", "instant", "", 0)
    for i in range(1, 6):
        p = c.join(i, f"p{i}")
        c.mark(p, t1.id, "done", now=100 + i)
        if i != 5:  # person 5 skipped the second task: not in the draw
            c.mark(p, t2.id, "done", now=200 + i)
    assert [p.tg_id for p in c.finishers()] == [1, 2, 3, 4]
    made = c.settle_top(now=1, rng=random.Random(1))
    assert len(made) == 2 and all(d.kind == "raffle" and d.raw == 9 for d in made)
    assert len({d.tg_id for d in made}) == 2 and 5 not in {d.tg_id for d in made}
    assert c.settle_top() == []
    assert c.budget() == 18


def test_lucky_draw_with_few_finishers_and_combined_budget():
    c = make(raffle_mode="manual", raffle_each=4, raffle_winners=3, comp_mode="instant", comp_each=2, comp_max=5, top_mode="auto", top_prizes=[10, 5])
    assert c.budget() == 12 + 10 + 15
    t = c.add_task("web:visit", "A", "u", "instant", "", 5)
    c.mark(c.join(1, "a"), t.id, "done", now=100)
    c.state = "ended"
    made = c.settle_top(now=2)
    assert sorted((d.kind, d.raw) for d in made) == [("raffle", 4), ("top", 10)]  # only one person finished


def test_question_task_answer_matching():
    assert cm.norm_answer("  Neo Red  Pill! ") == cm.norm_answer("neo red pill")
    assert cm.norm_answer("A") != cm.norm_answer("B")


def test_old_task_kinds_are_converted(tmp_path):
    c = make()
    c.add_task("web:visit", "L", "u", "instant")
    d = cm.to_dict(c)
    for old, task in zip(("link",), d["tasks"]):
        task["kind"] = old
    d["tasks"].append(dict(d["tasks"][0], id=2, kind="x_follow"))
    back = cm.from_dict(d)
    assert [t.kind for t in back.tasks] == ["web:visit", "x:follow"]


def test_list_card_and_short_reward_lines():
    c = make(comp_mode="instant", comp_each=500_000_000, comp_max=2, raffle_mode="auto", raffle_each=100_000_000, raffle_winners=3,
             top_mode="manual", top_prizes=[300_000_000, 200_000_000])
    fmt = lambda raw: f"{raw / 1e8:g}"
    card = cm.list_card(c, fmt, 1500, 1)
    assert "@boss" in card and "Pool" in card and "⚡" in card and "🎲" in card and "🏆" in card
    assert fmt(c.budget()) in card and "places left" in card
    assert cm.reward_icons(c) == "⚡🎲🏆" and cm.reward_icons(make()) == ""
    assert "Total rewards" in cm.reward_lines(c, fmt)[0]
    assert len(card) < 600
    c.banner = "FILEID"
    back = cm.from_dict(cm.to_dict(c))
    assert back.banner == "FILEID"


def test_time_zone_and_date_parsing():
    import datetime as dt
    assert cm.parse_tz("UTC+6") == 360 and cm.parse_tz("+5:30") == 330 and cm.parse_tz("-4") == -240 and cm.parse_tz("utc") == 0
    for bad in ("x", "+99", "+5:99"):
        try:
            cm.parse_tz(bad)
            assert False, bad
        except cm.CampaignError:
            pass
    now = dt.datetime(2026, 10, 23, 6, 0, tzinfo=dt.timezone.utc).timestamp()  # 12:00 in UTC+6
    at = lambda text, tz=360: dt.datetime.fromtimestamp(cm.parse_when(text, tz, now), dt.timezone.utc)
    assert at("2026-10-25 14:30") == dt.datetime(2026, 10, 25, 8, 30, tzinfo=dt.timezone.utc)
    assert at("25 Oct 18:00") == dt.datetime(2026, 10, 25, 12, 0, tzinfo=dt.timezone.utc)
    assert at("tomorrow 9:00") == dt.datetime(2026, 10, 24, 3, 0, tzinfo=dt.timezone.utc)
    assert at("today 6pm") == dt.datetime(2026, 10, 23, 12, 0, tzinfo=dt.timezone.utc)
    assert at("5 Jan 10:00").year == 2027  # a date already far in the past means next year
    assert cm.parse_when("now", 0, now) == now
    for bad in ("soon", "32 Oct 10:00", "2026-13-01 10:00", "tomorrow 25:00"):
        try:
            cm.parse_when(bad, 0, now)
            assert False, bad
        except cm.CampaignError:
            pass
    assert cm.format_when(now, 360) == "23 Oct 2026 12:00 UTC+6" and cm.format_when(now) == "23 Oct 2026 06:00 UTC"


def test_scheduled_start_and_exact_end():
    c = make(seconds=3600)
    c.state = "draft"
    assert c.schedule(1000) == (1000, 4600)  # starts when published
    c.start_at = 5000
    assert c.schedule(1000) == (5000, 8600)
    c.end_at = 9000
    assert c.schedule(1000) == (5000, 9000)
    assert c.schedule(9500) is None  # the chosen end already passed
    c.starts, c.ends, c.state = 5000, 9000, "open"
    assert c.is_upcoming(1000) and not c.is_active(1000)
    assert c.is_active(5000) and not c.is_upcoming(5000) and not c.is_active(9000)
    t = c.add_task("web:visit", "L", "u", "instant")
    p = c.join(1, "a")
    c.mark(p, t.id, "done", now=1000)
    c.comp_mode, c.comp_each, c.comp_max = "instant", 5, 2
    assert c.claim_completion(p, now=1000) is None  # not started yet


def test_invites_count_only_real_friends():
    c = make()  # the creator is telegram id 1
    t = c.add_task("ref:invite", "Invite 2", "", "auto", goal=2)
    web = c.add_task("web:visit", "L", "u", "instant")
    c.join(10, "inviter")
    assert c.record_referral(20, 10)  # inviter 10 is taking part: recorded
    assert not c.record_referral(20, 10)  # only the first invite counts
    assert not c.record_referral(10, 10)  # nobody invites themselves
    assert not c.record_referral(30, c.creator_tg_id) and not c.record_referral(40, 77)  # not the creator; the inviter must be taking part
    assert c.invite_count(10) == 0  # friend 20 has not joined or finished anything yet
    friend = c.join(20, "friend")
    assert c.invite_count(10) == 0
    c.mark(friend, web.id, "done", now=5)
    assert c.invite_count(10) == 1
    assert c.record_referral(21, 10)
    c.mark(c.join(21, "f21"), web.id, "done", now=6)
    assert c.invite_count(10) == 2 and c.invite_count(20) == 0
    assert not c.record_referral(21, 10)  # already a participant
    back = cm.from_dict(cm.to_dict(c))
    assert back.referrals == {20: 10, 21: 10} and back.task(t.id).goal == 2


def test_csv_export_and_pages():
    import csv as csvmod, io
    c = make(top_mode="manual", top_prizes=[5])
    t = c.add_task("x:follow", "Follow, \"us\"", "u", "approve", "", 10)
    for i in range(1, 20):
        c.mark(c.join(i, f"=SUM({i})" if i == 1 else f"user{i}"), t.id, "done", f"@h{i}", now=100 + i)
    rows = list(csvmod.reader(io.StringIO(cm.export_csv(c))))
    assert len(rows) == 20 and rows[0][0] == "telegram_id" and "Follow" in rows[0][5]
    assert any("@h2" in r[5] for r in rows[1:])
    assert cm.people_pages(c) == 3
    texts = [cm.participants_text(c, p) for p in range(3)]
    assert all(len(x) < 4096 for x in texts) and "page 1/3" in texts[0] and "page 3/3" in texts[2]
    assert cm.participants_text(c, 99).count("<b>") >= 1  # out-of-range page is clamped


def test_reward_text_in_time_line():
    c = make(seconds=3600)
    c.state = "draft"
    assert "Runs for 1 hour" in cm.time_line(c, 0)
    c.start_at, c.end_at = 1_800_000_000, 1_800_090_000
    assert "Planned" in cm.time_line(c, 0)
    c.state, c.starts, c.ends = "open", c.start_at, c.end_at
    assert "Starts in" in cm.time_line(c, c.start_at - 7200) and "Ends" in cm.time_line(c, c.start_at + 1)


def test_daily_check_in_needs_different_days():
    c = make()
    t = c.add_task("rc:daily", "Check in on 3 days", "", "auto", goal=3)
    p = c.join(5, "a")
    day = 86400
    assert c.check_in(p, t, 10 * day + 5) == (1, False)
    assert c.check_in(p, t, 10 * day + 900) == (1, True)  # same day again does nothing
    assert t.id not in p.entries
    assert c.check_in(p, t, 11 * day) == (2, False)
    assert c.check_in(p, t, 13 * day) == (3, False)  # a skipped day is fine, they only need 3 different days
    assert p.entries[t.id].status == "done" and c.all_done(p)
    back = cm.from_dict(cm.to_dict(c))
    assert back.participants[5].checkins[t.id] == [10, 11, 13]


def test_balance_task_fields_and_csv_wallets():
    import csv as csvmod, io
    c = make()
    t = c.add_task("bal:min", "Hold 100 NRP", "", "auto", goal=10_000_000_000, token_id=7)
    p = c.join(5, "a")
    c.mark(p, t.id, "done", now=50)
    back = cm.from_dict(cm.to_dict(c))
    assert back.task(t.id).token_id == 7 and back.task(t.id).goal == 10_000_000_000
    rows = list(csvmod.reader(io.StringIO(cm.export_csv(c, {5: ["0xabc", "NAbc"]}))))
    assert rows[0][-1] == "wallets" and rows[1][-1] == "0xabc NAbc"
    assert "Hold" in cm.task_text(c, t, None) or "wallet" in cm.task_text(c, t, None)


def test_draw_board_lists_everyone_without_ranks_then_the_winners():
    c = make(raffle_mode="auto", raffle_each=5, raffle_winners=2)
    t = c.add_task("x:follow", "Follow", "u", "instant", "", 0)
    fmt = lambda raw: str(raw)
    for i in range(25):
        p = c.join(i + 1, f"user{i + 1}")
        c.mark(p, t.id, "done", now=1600 + i)
    text = cm.board_text(c, fmt, 3)
    assert all(f"user{i}" in text for i in range(1, 26))  # nobody is cut off at 10
    assert "🥇" not in text and "1. " not in text  # no ranks in a draw
    assert "happens when the airdrop ends" in text
    c.settle_top(now=2000)
    text = cm.board_text(c, fmt, 3)
    assert "Lucky draw winners" in text and len(cm.winners(c)) == 2
    assert text.index("Lucky draw winners") < text.index("Everyone who joined")
