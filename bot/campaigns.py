"""Airdrops: timed events where people finish tasks (join a channel, follow on X, ...) and earn rewards.

Pure logic with no Telegram code, so it can be tested. The bot (main.py) shows the screens, checks
Telegram membership and sends the money; everything about *what an airdrop is* lives here.

How an airdrop works
  * A creator picks a token, a length (minutes, hours or days), and up to two kinds of reward:
      - a completion reward: the first N people who finish every task get a fixed amount each;
      - top prizes: when the airdrop ends, the best scorers get prizes by rank.
  * Each reward is paid automatically ("instant" / "auto") or by the creator by hand ("manual").
  * Tasks are checked in one of three ways: "auto" (the bot asks Telegram), "instant" (the username
    is accepted at once) or "approve" (the creator accepts or rejects each person).
  * The creator can add tasks at any time while the airdrop runs.
Everything is stored in campaigns.json (plain data, no keys), so a restart never ends an airdrop.
"""
import csv
import html
import io
import json
import logging
import os
import random
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

log = logging.getLogger("walletbot")
PATH = "campaigns.json"

MIN_SECONDS = 60
MAX_SECONDS = 90 * 86400
MAX_TASKS = 12
MAX_TOP = 10
MAX_COMP_WINNERS = 500
MAX_OPEN_PER_CREATOR = 5
MAX_TITLE = 60
MAX_TEXT = 300
DEFAULT_POINTS = 10

# platform -> (icon, name, [(action, label)]). A task's kind is "platform:action", like "x:follow".
PLATFORMS = {
    "tg": ("✈️", "Telegram", [("join", "Join a channel or group")]),
    "x": ("𝕏", "X (Twitter)", [("follow", "Follow an account"), ("like", "Like a post"), ("repost", "Repost a post"),
                                ("quote", "Quote a post"), ("reply", "Reply to a post")]),
    "dc": ("💬", "Discord", [("join", "Join a server")]),
    "ig": ("📸", "Instagram", [("follow", "Follow an account"), ("like", "Like a post")]),
    "yt": ("▶️", "YouTube", [("sub", "Subscribe to a channel"), ("watch", "Watch or like a video")]),
    "rd": ("👽", "Reddit", [("join", "Join a subreddit"), ("upvote", "Upvote a post")]),
    "tt": ("🎵", "TikTok", [("follow", "Follow an account"), ("like", "Like a video")]),
    "fb": ("📘", "Facebook", [("follow", "Follow or like a page")]),
    "gh": ("🐙", "GitHub", [("star", "Star a repository"), ("follow", "Follow an account")]),
    "web": ("🌐", "Website", [("visit", "Visit a link")]),
    "ref": ("🤝", "Invite friends", [("invite", "Invite friends with your link")]),
    "rc": ("🔁", "Daily check-in", [("daily", "Check in every day")]),
    "bal": ("💰", "Token balance", [("min", "Hold a minimum token balance")]),
    "qz": ("❓", "Question", [("answer", "Answer a question")]),
}
# which sites a link for each platform may point to
HOSTS = {
    "x": ("x.com", "twitter.com"), "dc": ("discord.gg", "discord.com", "discord.gg"), "ig": ("instagram.com",),
    "yt": ("youtube.com", "youtu.be", "m.youtube.com"), "rd": ("reddit.com", "old.reddit.com"),
    "tt": ("tiktok.com", "vm.tiktok.com"), "fb": ("facebook.com", "fb.com", "m.facebook.com"), "gh": ("github.com",),
}
NO_HANDLE = ("tg", "web", "qz", "ref", "rc", "bal")  # these tasks have nothing to type in as a username
KINDS = {  # kind -> (icon, label, does the person send a username?)
    f"{plat}:{act}": (icon, f"{label} · {pname}", plat not in NO_HANDLE)
    for plat, (icon, pname, acts) in PLATFORMS.items() for act, label in acts
}
LEGACY_KINDS = {"tg_join": "tg:join", "x_follow": "x:follow", "x_like": "x:like", "ig_follow": "ig:follow", "link": "web:visit"}
POST_ACTIONS = {"like", "repost", "quote", "reply", "upvote", "watch"}
VERIFY_LABEL = {
    "auto": "checked by the bot",
    "instant": "accepted at once",
    "approve": "the creator approves",
}


class CampaignError(Exception):
    """A problem with a person's input, in plain words."""


# ------------------------------------------------------------------------- input checks
def clean_text(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    if not text:
        raise CampaignError("That can't be empty.")
    if len(text) > limit:
        raise CampaignError(f"Please keep it under {limit} characters.")
    return text


def valid_url(text: str) -> str:
    text = (text or "").strip()
    if not re.match(r"^https?://", text, re.I):
        raise CampaignError("Send the full link, starting with https://")
    parsed = urlparse(text)
    if not parsed.netloc or "." not in parsed.netloc or " " in text or len(text) > 300:
        raise CampaignError("That doesn't look like a working link.")
    return text


def _handle_from(text: str, hosts: tuple, pattern: str, what: str, max_len: int) -> str:
    text = (text or "").strip()
    if re.match(r"^https?://", text, re.I):
        parsed = urlparse(text)
        host = parsed.netloc.lower().removeprefix("www.").removeprefix("mobile.")
        if host not in hosts:
            raise CampaignError(f"That is not a {what} link.")
        parts = [p for p in parsed.path.split("/") if p]
        text = parts[0] if parts else ""
    text = text.lstrip("@").strip()
    if not text or len(text) > max_len or not re.fullmatch(pattern, text):
        raise CampaignError(f"Send a {what} username, like @name.")
    return text


def x_handle(text: str) -> str:
    return _handle_from(text, ("x.com", "twitter.com"), r"[A-Za-z0-9_]{1,15}", "X", 15)


def ig_handle(text: str) -> str:
    handle = _handle_from(text, ("instagram.com",), r"[A-Za-z0-9._]{1,30}", "Instagram", 30)
    if handle.endswith(".") or ".." in handle:
        raise CampaignError("That is not a valid Instagram username.")
    return handle


def x_post_url(text: str) -> str:
    url = valid_url(text)
    host = urlparse(url).netloc.lower().removeprefix("www.").removeprefix("mobile.")
    if host not in ("x.com", "twitter.com"):
        raise CampaignError("Send a link to a post on x.com or twitter.com.")
    return url


def platform_url(plat: str, action: str, text: str) -> str:
    """The link people open for a task. Accounts can also be sent as @name on X, Instagram, TikTok and GitHub."""
    text = (text or "").strip()
    if action not in POST_ACTIONS and plat in ("x", "ig", "tt", "gh") and re.fullmatch(r"@?[A-Za-z0-9_.\-]{1,39}", text):
        name = text.lstrip("@")
        return {"x": f"https://x.com/{name}", "ig": f"https://instagram.com/{name}",
                "tt": f"https://tiktok.com/@{name}", "gh": f"https://github.com/{name}"}[plat]
    url = valid_url(text)
    host = urlparse(url).netloc.lower().removeprefix("www.")
    if plat in HOSTS and host not in HOSTS[plat]:
        raise CampaignError(f"That link is not on {PLATFORMS[plat][1]}. Send a {PLATFORMS[plat][1]} link.")
    return url


def task_title(plat: str, action: str, url: str) -> str:
    label = dict(PLATFORMS[plat][2])[action]
    pname = PLATFORMS[plat][1]
    if plat == "web":
        return f"Visit {urlparse(url).netloc.removeprefix('www.')}"
    parts = [p for p in urlparse(url).path.split("/") if p]
    if action in POST_ACTIONS:
        return f"{label.split(' a ')[0]} this post on {pname}" if plat != "yt" else "Watch and like this video on YouTube"
    name = parts[-1].lstrip("@") if parts else ""
    return f"{label.split(' ')[0]} {'@' + name if name and plat in ('x', 'ig', 'tt', 'gh') else name or 'us'} on {pname}"[:60]


def profile_name(plat: str, text: str) -> str:
    """The username a person sends as proof, cleaned up. X and Instagram are checked strictly."""
    if plat == "x":
        return "@" + x_handle(text)
    if plat == "ig":
        return "@" + ig_handle(text)
    text = (text or "").strip()
    if re.match(r"^https?://", text, re.I):
        parts = [p for p in urlparse(text).path.split("/") if p]
        text = parts[-1] if parts else ""
    text = re.sub(r"^(?:u|user)/", "", text.lstrip("@/").strip(), flags=re.I)
    if not 2 <= len(text) <= 60 or not re.fullmatch(r"[\w.\-#]+(?: ?[\w.\-#]+)*", text):
        raise CampaignError(f"Send your {PLATFORMS.get(plat, ('', 'account'))[1]} username.")
    return text


def tg_target(text: str) -> str:
    """'@name', 't.me/name' or 'https://t.me/name' -> '@name'. Private invite links can't be used."""
    text = (text or "").strip()
    match = re.match(r"^(?:https?://)?(?:t\.me|telegram\.me)/([A-Za-z0-9_]+)/?$", text, re.I)
    if match:
        text = "@" + match.group(1)
    if re.fullmatch(r"@[A-Za-z][A-Za-z0-9_]{4,31}", text):
        return text
    raise CampaignError(
        "Send the channel or group as @username, or its t.me link. "
        "For a private one, forward me any message from it."
    )


def parse_tz(text: str) -> int:
    """'UTC', '+6', '+05:30', 'UTC+6', '-4' -> minutes east of UTC."""
    raw = re.sub(r"\s+", "", (text or "").lower()).removeprefix("utc").removeprefix("gmt")
    if raw in ("", "z", "0", "+0", "-0"):
        return 0
    match = re.fullmatch(r"([+-])(\d{1,2})(?::?(\d{2}))?", raw)
    if not match:
        raise CampaignError("Send your time zone like UTC, +6, +5:30 or -4.")
    minutes = int(match.group(2)) * 60 + int(match.group(3) or 0)
    if minutes > 14 * 60 or int(match.group(3) or 0) >= 60:
        raise CampaignError("That is not a real time zone. Try +6 or -4.")
    return minutes if match.group(1) == "+" else -minutes


_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


def parse_when(text: str, tz_minutes: int, now: float) -> float:
    """A date and time typed in the creator's time zone -> unix seconds.

    Accepts: now · today 18:00 · tomorrow 9:30 · 2026-10-25 14:30 · 25 Oct 14:30 · 25 Oct 2026 14:30.
    """
    raw = " ".join((text or "").lower().replace(",", " ").split())
    if raw in ("now", "asap"):
        return now
    local_now = datetime.fromtimestamp(now + tz_minutes * 60, timezone.utc)
    clock = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*$", raw)
    if clock and not clock.group(2) and not clock.group(3):  # a bare number ("25 Oct 2026") is not a time
        clock = None
    if not clock:
        raise CampaignError("Send the date and the time, like 2026-10-25 14:30, 25 Oct 14:30 or tomorrow 18:00.")
    hour, minute = int(clock.group(1)), int(clock.group(2) or 0)
    if clock.group(3):
        if not 1 <= hour <= 12:
            raise CampaignError("That hour is not valid.")
        hour = hour % 12 + (12 if clock.group(3) == "pm" else 0)
    if hour > 23 or minute > 59:
        raise CampaignError("That time is not valid.")
    day_part = raw[: clock.start()].strip()
    try:
        if day_part in ("today", ""):
            base = local_now.date()
        elif day_part == "tomorrow":
            base = (local_now + timedelta(days=1)).date()
        elif re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", day_part):
            y, mo, d = map(int, day_part.split("-"))
            base = datetime(y, mo, d, tzinfo=timezone.utc).date()
        else:
            m = re.fullmatch(r"(\d{1,2})\s+([a-z]{3})[a-z]*(?:\s+(\d{4}))?", day_part)
            if not m or m.group(2) not in _MONTHS:
                raise ValueError
            year = int(m.group(3)) if m.group(3) else local_now.year
            base = datetime(year, _MONTHS[m.group(2)], int(m.group(1)), tzinfo=timezone.utc).date()
            if not m.group(3) and datetime(base.year, base.month, base.day, hour, minute, tzinfo=timezone.utc).timestamp() < local_now.timestamp() - 86400 * 30:
                base = base.replace(year=base.year + 1)
    except ValueError:
        raise CampaignError("I could not read that date. Try 2026-10-25 14:30 or 25 Oct 14:30.")
    local = datetime(base.year, base.month, base.day, hour, minute, tzinfo=timezone.utc)
    return local.timestamp() - tz_minutes * 60


def format_when(ts: float, tz_minutes: int = 0) -> str:
    local = datetime.fromtimestamp(ts + tz_minutes * 60, timezone.utc)
    sign = "+" if tz_minutes >= 0 else "-"
    hh, mm = divmod(abs(tz_minutes), 60)
    zone = "UTC" if tz_minutes == 0 else f"UTC{sign}{hh}" + (f":{mm:02d}" if mm else "")
    return f"{local.day} {local.strftime('%b')} {local.year} {local:%H:%M} {zone}"


def norm_answer(text: str) -> str:
    """For comparing answers: ignores capitals, extra spaces and punctuation at the ends."""
    return " ".join((text or "").lower().split()).strip(" .,!?;:\"'")


def parse_duration(text: str) -> int:
    """'10m', '15 min', '1h', '1h 30m', '2d', '1d12h' -> seconds. A unit is required, so '7' is never guessed."""
    raw = (text or "").strip().lower()
    parts = re.findall(r"(\d+)\s*(days?|d|hours?|hrs?|h|minutes?|mins?|m)(?![a-z])", raw)
    leftover = re.sub(r"(\d+)\s*(days?|d|hours?|hrs?|h|minutes?|mins?|m)(?![a-z])", "", raw)
    if not parts or re.sub(r"[\s,]|and", "", leftover):
        raise CampaignError("Send the time with a unit, like 10m, 1h, 1h30m or 2d.")
    total = 0
    for number, unit in parts:
        total += int(number) * {"d": 86400, "h": 3600, "m": 60}[unit[0]]
    if not MIN_SECONDS <= total <= MAX_SECONDS:
        raise CampaignError(f"The time must be between {format_duration(MIN_SECONDS)} and {format_duration(MAX_SECONDS)}.")
    return total


def format_duration(seconds: int) -> str:
    seconds = int(seconds)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    out = [f"{n} {word}{'' if n == 1 else 's'}" for n, word in ((days, "day"), (hours, "hour"), (minutes, "minute")) if n]
    return " ".join(out) or "under a minute"


def format_left(seconds: float) -> str:
    seconds = int(seconds)
    if seconds <= 0:
        return "ended"
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}d {hours}h left"
    if hours:
        return f"{hours}h {minutes}m left"
    return f"{max(minutes, 1)}m left"


def new_id(taken) -> str:
    while True:
        value = secrets.token_hex(3)
        if value not in taken:
            return value


# ------------------------------------------------------------------------------- data
@dataclass
class Task:
    id: int
    kind: str
    title: str
    url: str  # what the person opens
    verify: str  # auto | instant | approve
    chat: str = ""  # tg:join: the chat id or @username the bot checks
    points: int = DEFAULT_POINTS
    answer: str = ""  # qz:answer: the correct answer, checked by the bot
    goal: int = 0  # ref:invite: friends needed · rc:daily: days needed · bal:min: the amount, in the token's smallest unit
    token_id: int = 0  # bal:min: which token


@dataclass
class Entry:
    status: str  # done | pending | rejected
    handle: str = ""
    at: float = 0.0


@dataclass
class Participant:
    tg_id: int
    label: str
    entries: dict = field(default_factory=dict)  # task id -> Entry
    finished_at: float = 0.0  # when the last task was completed (breaks ties: earlier wins)
    checkins: dict = field(default_factory=dict)  # task id -> the days (counted since 1970) this person checked in


@dataclass
class Due:
    """A reward that someone has earned. It is paid, or still waiting to be paid."""

    tg_id: int
    label: str
    raw: int
    kind: str  # completion | top
    rank: int = 0
    status: str = "owed"  # owed | paid | failed
    note: str = ""
    at: float = 0.0


@dataclass
class Campaign:
    id: str
    creator_tg_id: int
    creator_user_id: int
    creator_label: str
    title: str
    description: str
    slug: str  # network
    network_name: str
    token_id: int
    symbol: str
    decimals: int
    seconds: int
    funding: str = "later"  # funded | later
    comp_mode: str = "none"  # none | instant | manual
    comp_each: int = 0
    comp_max: int = 0
    top_mode: str = "none"  # none | auto | manual
    top_prizes: list = field(default_factory=list)
    start_at: float = 0.0  # a start time picked by the creator (0 = the moment it is published)
    end_at: float = 0.0  # an end time picked by the creator (0 = start + seconds)
    referrals: dict = field(default_factory=dict)  # friend's tg id -> the tg id of who invited them
    review_msgs: list = field(default_factory=list)  # [chat id, message id] of the approval requests sent to the team
    broadcast: str = "none"  # none | pending | approved | rejected | sent: the team's OK to notify every user
    banner: str = ""  # Telegram file id of the creator's banner picture
    raffle_mode: str = "none"  # none | auto | manual: a lucky draw among everyone who finished every task
    raffle_each: int = 0
    raffle_winners: int = 0
    state: str = "draft"  # draft | open | ended | cancelled
    created: float = 0.0
    starts: float = 0.0
    ends: float = 0.0
    tasks: list = field(default_factory=list)
    participants: dict = field(default_factory=dict)  # tg id -> Participant
    dues: list = field(default_factory=list)
    top_settled: bool = False
    next_task_id: int = 1

    def uses_points(self) -> bool:
        """Points only matter when there is a leaderboard prize. First come first served and lucky draws don't use them."""
        return self.top_mode != "none"

    # ---- tasks
    def task(self, task_id: int):
        return next((t for t in self.tasks if t.id == task_id), None)

    def add_task(self, kind: str, title: str, url: str, verify: str, chat: str = "", points: int = DEFAULT_POINTS,
                 answer: str = "", goal: int = 0, token_id: int = 0) -> Task:
        if kind not in KINDS:
            raise CampaignError("Unknown task type.")
        if len(self.tasks) >= MAX_TASKS:
            raise CampaignError(f"An airdrop can have at most {MAX_TASKS} tasks.")
        task = Task(self.next_task_id, kind, title, url, verify, chat, points, answer, goal, token_id)
        self.next_task_id += 1
        self.tasks.append(task)
        return task

    def remove_task(self, task_id: int) -> bool:
        before = len(self.tasks)
        self.tasks = [t for t in self.tasks if t.id != task_id]
        for p in self.participants.values():
            p.entries.pop(task_id, None)
        return len(self.tasks) < before

    # ---- people
    def is_active(self, now: float) -> bool:
        return self.state == "open" and self.starts <= now < self.ends

    def is_upcoming(self, now: float) -> bool:
        return self.state == "open" and now < self.starts

    def schedule(self, now: float):
        """(starts, ends) if published now, or None when the chosen end time has already passed."""
        if self.end_at:
            start = max(self.start_at, now) if self.start_at else now
            return (start, self.end_at) if self.end_at > start + 30 else None
        start = max(self.start_at, now) if self.start_at else now
        return start, start + self.seconds

    # ---- recurring tasks
    def check_in(self, person: Participant, task: Task, now: float):
        """Record today's check-in. Returns (days so far, was it already done today)."""
        today = int(now // 86400)
        days = person.checkins.setdefault(task.id, [])
        if today in days:
            return len(days), True
        days.append(today)
        if len(days) >= task.goal:
            self.mark(person, task.id, "done", f"{len(days)} days", now)
        return len(days), False

    # ---- invites
    def record_referral(self, friend: int, inviter: int) -> bool:
        """Remember who invited a friend. Only the first invite counts; nobody invites themselves or the creator."""
        if friend == inviter or friend == self.creator_tg_id or inviter == self.creator_tg_id:
            return False
        if friend in self.referrals or friend in self.participants:
            return False
        if inviter not in self.participants:
            return False
        self.referrals[friend] = inviter
        return True

    def invite_count(self, inviter: int) -> int:
        """Friends this person invited who really took part (finished at least one task)."""
        total = 0
        for friend, who in self.referrals.items():
            person = self.participants.get(friend)
            if who == inviter and person is not None and self.done_count(person) > 0:
                total += 1
        return total

    def participant(self, tg_id: int):
        return self.participants.get(tg_id)

    def join(self, tg_id: int, label: str) -> Participant:
        person = self.participants.get(tg_id)
        if person is None:
            person = self.participants[tg_id] = Participant(tg_id, label)
        else:
            person.label = label
        return person

    def points_of(self, person: Participant) -> int:
        total = 0
        for task in self.tasks:
            entry = person.entries.get(task.id)
            if entry is not None and entry.status == "done":
                total += task.points
        return total

    def done_count(self, person: Participant) -> int:
        return sum(1 for t in self.tasks if (e := person.entries.get(t.id)) is not None and e.status == "done")

    def all_done(self, person: Participant) -> bool:
        return bool(self.tasks) and self.done_count(person) == len(self.tasks)

    def max_points(self) -> int:
        return sum(t.points for t in self.tasks)

    def handle_taken(self, task_id: int, handle: str, except_tg: int) -> bool:
        wanted = handle.lower()
        for p in self.participants.values():
            if p.tg_id == except_tg:
                continue
            entry = p.entries.get(task_id)
            if entry is not None and entry.status != "rejected" and entry.handle.lower() == wanted:
                return True
        return False

    def mark(self, person: Participant, task_id: int, status: str, handle: str = "", now: float | None = None) -> Entry:
        now = time.time() if now is None else now
        entry = Entry(status, handle, now)
        person.entries[task_id] = entry
        if status == "done":
            person.finished_at = now
        return entry

    def pending_reviews(self) -> list:
        """[(participant, task, entry)] waiting for the creator, oldest first."""
        out = []
        for p in self.participants.values():
            for task in self.tasks:
                entry = p.entries.get(task.id)
                if entry is not None and entry.status == "pending":
                    out.append((p, task, entry))
        out.sort(key=lambda item: item[2].at)
        return out

    def ranking(self) -> list:
        """[(participant, points)], best first. Equal points: whoever got there first. Nobody with 0 points."""
        rows = [(p, self.points_of(p)) for p in self.participants.values()]
        rows = [r for r in rows if r[1] > 0]
        rows.sort(key=lambda r: (-r[1], r[0].finished_at, r[0].tg_id))
        return rows

    def rank_of(self, tg_id: int):
        for index, (p, _points) in enumerate(self.ranking(), start=1):
            if p.tg_id == tg_id:
                return index
        return None

    # ---- money
    def budget(self) -> int:
        """The most this airdrop can ever pay out."""
        total = self.comp_each * self.comp_max if self.comp_mode != "none" else 0
        if self.top_mode != "none":
            total += sum(self.top_prizes)
        if self.raffle_mode != "none":
            total += self.raffle_each * self.raffle_winners
        return total

    def unpaid_budget(self) -> int:
        """What is still to be paid if everything goes as planned (used to reserve funds)."""
        if self.state in ("ended", "cancelled") and self.top_settled:
            return sum(d.raw for d in self.dues if d.status != "paid")
        paid = sum(d.raw for d in self.dues if d.status == "paid")
        return max(self.budget() - paid, 0)

    def comp_count(self) -> int:
        return sum(1 for d in self.dues if d.kind == "completion")

    def comp_left(self) -> int:
        return max(self.comp_max - self.comp_count(), 0) if self.comp_mode != "none" else 0

    def claim_completion(self, person: Participant, now: float | None = None):
        """The reward for finishing every task, if this person earned it and a place is free."""
        if self.comp_mode == "none" or not self.is_active(time.time() if now is None else now):
            return None
        if not self.all_done(person) or self.comp_left() <= 0:
            return None
        if any(d.kind == "completion" and d.tg_id == person.tg_id for d in self.dues):
            return None
        due = Due(person.tg_id, person.label, self.comp_each, "completion", 0, "owed", "", time.time() if now is None else now)
        self.dues.append(due)
        return due

    def finishers(self) -> list:
        """Everyone who finished every task, earliest first."""
        done = [p for p in self.participants.values() if self.all_done(p)]
        done.sort(key=lambda p: (p.finished_at, p.tg_id))
        return done

    def settle_top(self, now: float | None = None, rng=None) -> list:
        """Called once when the airdrop has ended: makes the prizes for the top scorers and the lucky-draw winners."""
        if self.top_settled:
            return []
        self.top_settled = True
        now = time.time() if now is None else now
        made = []
        if self.top_mode != "none":
            for place, (person, _points) in enumerate(self.ranking()[: len(self.top_prizes)], start=1):
                due = Due(person.tg_id, person.label, self.top_prizes[place - 1], "top", place, "owed", "", now)
                self.dues.append(due)
                made.append(due)
        if self.raffle_mode != "none":
            pool = self.finishers()
            chosen = pool if len(pool) <= self.raffle_winners else (rng or random.SystemRandom()).sample(pool, self.raffle_winners)
            for person in chosen:
                due = Due(person.tg_id, person.label, self.raffle_each, "raffle", 0, "owed", "", now)
                self.dues.append(due)
                made.append(due)
        return made

    def owed(self) -> list:
        return [d for d in self.dues if d.status in ("owed", "failed")]

    def finish_if_due(self, now: float) -> bool:
        if self.state == "open" and now >= self.ends:
            self.state = "ended"
            return True
        return False


# ------------------------------------------------------------------------ storage
def _entry_from(d: dict) -> Entry:
    return Entry(str(d.get("status", "pending")), str(d.get("handle", "")), float(d.get("at", 0)))


def to_dict(c: Campaign) -> dict:
    out = {k: v for k, v in c.__dict__.items() if k not in ("tasks", "participants", "dues", "referrals")}
    out["referrals"] = {str(k): v for k, v in c.referrals.items()}
    out["tasks"] = [t.__dict__ for t in c.tasks]
    out["dues"] = [d.__dict__ for d in c.dues]
    out["participants"] = {
        str(tg): {
            "tg_id": p.tg_id, "label": p.label, "finished_at": p.finished_at,
            "checkins": {str(tid): days for tid, days in p.checkins.items()},
            "entries": {str(tid): e.__dict__ for tid, e in p.entries.items()},
        }
        for tg, p in c.participants.items()
    }
    return out


def from_dict(d: dict) -> Campaign:
    base = {k: v for k, v in d.items() if k not in ("tasks", "participants", "dues", "referrals")}
    known = Campaign.__dataclass_fields__
    if "seconds" not in base:  # saved by the first version, which counted whole days
        base["seconds"] = int(base.get("days", 1)) * 86400
    camp = Campaign(**{k: v for k, v in base.items() if k in known})
    camp.tasks = [Task(**{k: v for k, v in t.items() if k in Task.__dataclass_fields__}) for t in d.get("tasks", [])]
    for task in camp.tasks:
        task.kind = LEGACY_KINDS.get(task.kind, task.kind)
    camp.dues = [Due(**{k: v for k, v in x.items() if k in Due.__dataclass_fields__}) for x in d.get("dues", [])]
    camp.referrals = {int(k): int(v) for k, v in (d.get("referrals") or {}).items()}
    camp.participants = {}
    for tg, p in (d.get("participants") or {}).items():
        person = Participant(int(p.get("tg_id", tg)), str(p.get("label", "someone")), {}, float(p.get("finished_at", 0)))
        person.entries = {int(tid): _entry_from(e) for tid, e in (p.get("entries") or {}).items()}
        person.checkins = {int(tid): [int(x) for x in days] for tid, days in (p.get("checkins") or {}).items()}
        camp.participants[int(tg)] = person
    return camp


def save(campaigns: dict, path: str = PATH) -> None:
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump([to_dict(c) for c in campaigns.values()], fh)
        os.replace(tmp, path)  # all or nothing: a crash never leaves half a file
    except Exception:
        log.exception("Could not save the airdrops")


def load(path: str = PATH) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except Exception:
        log.exception("Could not read the saved airdrops")
        return {}
    out = {}
    for item in data if isinstance(data, list) else []:
        try:
            camp = from_dict(item)
            out[camp.id] = camp
        except Exception:
            log.exception("Skipped one unreadable airdrop")
    return out


def export_csv(c: Campaign, wallets: dict | None = None) -> str:
    """Everything the creator may want to keep: who took part, what they sent, points, and what they are owed."""
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["telegram_id", "name", "tasks_done", "points", "finished_all_at_utc"]
               + [f"task {t.id}: {t.title}" for t in c.tasks] + ["reward_kind", "reward_amount_raw", "reward_status", "wallets"])
    for person in sorted(c.participants.values(), key=lambda p: (-c.points_of(p), -c.done_count(p), p.finished_at or 1e18)):
        cells = []
        for task in c.tasks:
            entry = person.entries.get(task.id)
            cells.append("" if entry is None else (entry.status + (f" {entry.handle}" if entry.handle and task.kind != "qz:answer" else "")))
        dues = [d for d in c.dues if d.tg_id == person.tg_id]
        finished = datetime.fromtimestamp(person.finished_at, timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if c.all_done(person) and person.finished_at else ""
        w.writerow([person.tg_id, person.label, c.done_count(person), c.points_of(person), finished] + cells
                   + [";".join(d.kind for d in dues), ";".join(str(d.raw) for d in dues), ";".join(d.status for d in dues),
                      " ".join((wallets or {}).get(person.tg_id, []))])
    return out.getvalue()


# ------------------------------------------------------------------------ screens (text)
def _e(text) -> str:
    return html.escape(str(text))


def funding_badge(c: Campaign) -> str:
    if c.funding == "funded":
        return "✅ <b>Funded</b>: the creator's wallet held the full reward when this airdrop was made"
    return "⏳ <b>Pay when due</b>: the reward is paid from the creator's wallet when it is earned. It is not set aside in advance"


def reward_short(c: Campaign, fmt) -> str:
    """One compact line naming every way to win, for lists."""
    sym = _e(c.symbol)
    parts = []
    if c.comp_mode != "none":
        parts.append(f"⚡ {fmt(c.comp_each)} {sym} each · first {c.comp_max}")
    if c.raffle_mode != "none":
        parts.append(f"🎲 {c.raffle_winners} random × {fmt(c.raffle_each)} {sym}")
    if c.top_mode != "none":
        parts.append(f"🏆 top {len(c.top_prizes)} · {fmt(sum(c.top_prizes))} {sym}")
    return "  ".join(parts) or "no reward"


def reward_icons(c: Campaign) -> str:
    return ("⚡" if c.comp_mode != "none" else "") + ("🎲" if c.raffle_mode != "none" else "") + ("🏆" if c.top_mode != "none" else "")


def list_card(c: Campaign, fmt, now: float, number: int | None) -> str:
    """A block of the 'Active airdrops' list: what it is, who runs it, what you can win and for how long."""
    head = f"{number}. " if number else ""
    lines = [f"<b>{head}{_e(c.title)}</b>", f"   by {_e(c.creator_label)} · {_e(c.network_name)}",
             f"   💰 Pool <b>{fmt(c.budget())} {_e(c.symbol)}</b>", f"   {reward_short(c, fmt)}"]
    if c.comp_mode != "none":
        lines[-1] += f" ({c.comp_left()} places left)"
    when = f"🗓 starts in {format_left(c.starts - now).replace(' left', '')}" if c.is_upcoming(now) else f"⏱ {format_left(c.ends - now)}"
    lines.append(f"   {when} · 👥 {len(c.participants)} joined · {len(c.tasks)} task(s)")
    return "\n".join(lines)


def reward_lines(c: Campaign, fmt) -> list[str]:
    sym = _e(c.symbol)
    lines = [f"💰 Total rewards: <b>{fmt(c.budget())} {sym}</b>"]
    if c.comp_mode != "none":
        how = "sent to you at once" if c.comp_mode == "instant" else "sent by the creator"
        lines.append(
            f"⚡ <b>First come, first served</b>: finish every task and get <b>{fmt(c.comp_each)} {sym}</b> "
            f"(first {c.comp_max} people, {how}). Places left: <b>{c.comp_left()}</b>"
        )
    if c.raffle_mode != "none":
        how = "sent automatically" if c.raffle_mode == "auto" else "sent by the creator"
        lines.append(
            f"🎲 <b>Lucky draw</b> at the end: {c.raffle_winners} random winner(s) among everyone who finished every task get "
            f"<b>{fmt(c.raffle_each)} {sym}</b> each ({how})"
        )
    if c.top_mode != "none":
        prizes = ", ".join(f"#{i} {fmt(p)}" for i, p in enumerate(c.top_prizes, start=1))
        how = "sent automatically when it ends" if c.top_mode == "auto" else "sent by the creator when it ends"
        lines.append(f"🏆 <b>Leaderboard</b> ({how}): {prizes} {sym}")
    return lines


def task_status_icon(entry) -> str:
    if entry is None:
        return "⬜"
    return {"done": "✅", "pending": "⏳", "rejected": "❌"}.get(entry.status, "⬜")


def _pts(c: Campaign, task: Task) -> str:
    return f" · {task.points} pts" if c.uses_points() else ""


def time_line(c: Campaign, now: float) -> str:
    if c.state == "draft":
        if c.start_at or c.end_at:
            return f"🗓 Planned: {format_when(c.start_at) if c.start_at else 'when published'} → " + (format_when(c.end_at) if c.end_at else f"{format_duration(c.seconds)} later")
        return f"🗓 Runs for {format_duration(c.seconds)} once published"
    if c.is_upcoming(now):
        return f"🗓 Starts in <b>{format_left(c.starts - now).replace(' left', '')}</b> · {format_when(c.starts)}\n⏱ Ends {format_when(c.ends)}"
    if c.state == "open":
        return f"⏱ Ends {format_when(c.ends)}"
    return f"🏁 {format_when(c.starts)} → {format_when(c.ends)}"


def event_text(c: Campaign, person, fmt, now: float) -> str:
    if c.state == "open":
        left = "starts soon" if c.is_upcoming(now) else format_left(c.ends - now)
    else:
        left = {"draft": "coming soon", "ended": "ended", "cancelled": "cancelled"}[c.state]
    lines = [f"🪂 <b>{_e(c.title)}</b>", f"by {_e(c.creator_label)} · {_e(c.network_name)} · <b>{left}</b>", time_line(c, now)]
    if c.description:
        lines += ["", _e(c.description)]
    lines += [""] + reward_lines(c, fmt) + [funding_badge(c), "", "<b>Tasks</b>"]
    if not c.tasks:
        lines.append("No tasks yet.")
    for task in c.tasks:
        entry = person.entries.get(task.id) if person else None
        lines.append(f"{task_status_icon(entry)} {KINDS[task.kind][0]} {_e(task.title)}{_pts(c, task)}")
    if person is not None:
        if c.uses_points():
            rank = c.rank_of(person.tg_id)
            lines += ["", f"Your points: <b>{c.points_of(person)}</b> of {c.max_points()}" + (f" · rank <b>#{rank}</b>" if rank else "")]
        else:
            lines += ["", f"Your progress: <b>{c.done_count(person)}</b> of {len(c.tasks)} tasks"]
    lines.append(f"👥 {len(c.participants)} taking part · ✅ {len(c.finishers())} finished all tasks")
    return "\n".join(lines)


def task_text(c: Campaign, task: Task, entry, invites: int | None = None) -> str:
    icon, name, needs_handle = KINDS[task.kind]
    plat = task.kind.split(":")[0]
    head = f"{icon} <b>{_e(task.title)}</b>"
    lines = [head, f"{VERIFY_LABEL[task.verify] if task.kind not in ('qz:answer', 'ref:invite', 'rc:daily', 'bal:min') else 'checked by the bot'}" + (f" · {task.points} points" if c.uses_points() else ""), ""]
    if task.kind == "tg:join":
        lines.append("1. Tap <b>Open</b> and join.\n2. Come back and tap <b>Verify</b>. I check it for you.")
    elif task.kind == "qz:answer":
        lines.append("Tap <b>Answer</b> and type your answer.")
    elif task.kind == "rc:daily":
        lines.append(f"Come back <b>every day</b> and tap <b>Check in today</b>. You need {task.goal} different day(s).")
        if invites is not None:
            lines.append(f"\nDays checked in: <b>{invites}</b> of {task.goal}")
    elif task.kind == "bal:min":
        lines.append("Keep the tokens in your wallet here. Tap <b>Verify balance</b> and I check it for you.")
    elif task.kind == "ref:invite":
        lines.append(f"1. Tap <b>My invite link</b> and share it with friends.\n2. A friend counts once they open it <b>and finish at least one task</b>.\n"
                     f"3. Invite <b>{task.goal}</b> friend(s), then tap <b>Check my invites</b>.")
        if invites is not None:
            lines.append(f"\nYour invites so far: <b>{invites}</b> of {task.goal}")
    elif needs_handle:
        where = PLATFORMS[plat][1]
        lines.append(f"1. Tap <b>Open</b> and do the task on {where}.\n2. Tap <b>Send my username</b> and type your {where} username.")
        if task.verify == "approve":
            lines.append("The creator will check it and approve you.")
    else:
        lines.append("1. Tap <b>Open</b> and visit it.\n2. Come back and confirm.")
        if task.verify == "approve":
            lines.append("You will be asked for a short note. The creator will approve you.")
    if entry is not None:
        text = {"done": "✅ You finished this task.",
                "pending": "⏳ Sent. Waiting for the creator to approve.",
                "rejected": "❌ The creator did not accept this. You can send it again."}.get(entry.status, "")
        if entry.handle and task.kind != "qz:answer":
            text += f" ({_e(entry.handle)})"
        lines += ["", text]
    return "\n".join(lines)


BOARD_PER_PAGE = 40


def winners(c: Campaign) -> list:
    """Everyone who has won a prize in a draw or a first-come reward, in the order it was decided."""
    return sorted((d for d in c.dues if d.kind in ("raffle", "completion")), key=lambda d: (d.at, d.tg_id))


def winners_lines(c: Campaign, fmt) -> list[str]:
    """The winner list as lines of text (empty while nothing has been decided)."""
    won = winners(c)
    if not won:
        return []
    kind = "Lucky draw winners" if any(d.kind == "raffle" for d in won) else "First come, first served winners"
    lines = [f"🎉 <b>{kind}</b> ({len(won)})"]
    lines += [f"• {_e(d.label)} · {fmt(d.raw)} {_e(c.symbol)}" for d in won]
    return lines


def board_pages(c: Campaign) -> int:
    if c.uses_points():
        return 1
    return max(1, -(-len(c.participants) // BOARD_PER_PAGE))


def board_text(c: Campaign, fmt, viewer_tg: int | None = None, limit: int = 10, page: int = 0) -> str:
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    won = winners_lines(c, fmt)
    if not c.uses_points():  # no points here: everybody who took part, in the order they joined. No ranks.
        people = list(c.participants.values())  # insertion order = the order they joined
        pages = board_pages(c)
        page = min(max(page, 0), pages - 1)
        lines = [f"👥 <b>{_e(c.title)}</b> · participants",
                 f"{len(people)} took part · {len(c.finishers())} finished every task"
                 + (f" · page {page + 1}/{pages}" if pages > 1 else "")]
        if won:
            lines += [""] + won
        elif c.raffle_mode != "none" and c.state == "open":
            lines += ["", "🎲 The lucky draw happens when the airdrop ends. Only people who finish every task can win."]
        lines += ["", "<b>Everyone who joined</b> (in the order they joined; ✅ = finished every task)"]
        if not people:
            lines.append("Nobody has joined yet. Be the first!")
        for person in people[page * BOARD_PER_PAGE:(page + 1) * BOARD_PER_PAGE]:
            you = " ← you" if viewer_tg == person.tg_id else ""
            lines.append(f"{'✅' if c.all_done(person) else '•'} {_e(person.label)}{you}")
        return "\n".join(lines)
    rows = c.ranking()
    lines = [f"🏆 <b>{_e(c.title)}</b> · leaderboard", f"{len(c.participants)} taking part · {c.max_points()} points possible", ""]
    if won:
        lines += won + [""]
    if not rows:
        lines.append("Nobody has points yet. Be the first!")
    for index, (person, points) in enumerate(rows[:limit], start=1):
        prize = ""
        if index <= len(c.top_prizes):
            prize = f" · prize {fmt(c.top_prizes[index - 1])} {_e(c.symbol)}"
        you = " ← you" if viewer_tg == person.tg_id else ""
        lines.append(f"{medals.get(index, f'{index}.')} {_e(person.label)} · {points} pts{prize}{you}")
    if viewer_tg is not None and viewer_tg in c.participants:
        rank = c.rank_of(viewer_tg)
        if rank and rank > limit:
            lines += ["…", f"{rank}. you · {c.points_of(c.participants[viewer_tg])} pts"]
    return "\n".join(lines)


def manage_text(c: Campaign, fmt, now: float) -> str:
    running = f"🗓 Scheduled · starts in {format_left(c.starts - now).replace(' left', '')}" if c.is_upcoming(now) else f"🟢 Running · {format_left(c.ends - now)}"
    states = {"draft": "📝 Draft (people see a 'coming soon' page)", "open": running, "ended": "🏁 Ended", "cancelled": "🚫 Cancelled"}
    lines = [f"🪂 <b>{_e(c.title)}</b>", states[c.state], f"{_e(c.network_name)} · {_e(c.symbol)}", time_line(c, now), ""]
    lines += reward_lines(c, fmt) + [funding_badge(c)]
    lines += ["", f"<b>Tasks ({len(c.tasks)})</b>"]
    if not c.tasks:
        lines.append("None yet. Add at least one, then publish.")
    for task in c.tasks:
        how = "checked by the bot" if task.kind in ("tg:join", "qz:answer", "ref:invite", "rc:daily", "bal:min") else VERIFY_LABEL[task.verify]
        lines.append(f"{KINDS[task.kind][0]} {_e(task.title)}{_pts(c, task)} · {how}")
    lines += ["", f"👥 {len(c.participants)} taking part · 🔍 {len(c.pending_reviews())} to review · 💸 {len(c.owed())} to pay"]
    return "\n".join(lines)


PEOPLE_PER_PAGE = 8


def people_pages(c: Campaign) -> int:
    return max(1, -(-len(c.participants) // PEOPLE_PER_PAGE))


def participants_text(c: Campaign, page: int = 0) -> str:
    """For the creator: who is taking part, what they sent for each task, and their progress."""
    rows = sorted(c.participants.values(), key=lambda p: (-c.points_of(p), -c.done_count(p), p.finished_at or 1e18))
    page = min(max(page, 0), people_pages(c) - 1)
    start = page * PEOPLE_PER_PAGE
    lines = [f"👥 <b>{_e(c.title)}</b> · {len(rows)} taking part · {len(c.finishers())} finished all tasks"
             + (f" · page {page + 1}/{people_pages(c)}" if len(rows) > PEOPLE_PER_PAGE else ""), ""]
    if not rows:
        lines.append("Nobody has joined yet. Share the link!")
    for index, person in enumerate(rows[start:start + PEOPLE_PER_PAGE], start=start + 1):
        score = f"{c.points_of(person)} pts" if c.uses_points() else f"{c.done_count(person)}/{len(c.tasks)} tasks"
        lines.append(f"{index}. <b>{_e(person.label)}</b> · {score}")
        for task in c.tasks:
            entry = person.entries.get(task.id)
            if entry is None:
                continue
            extra = f" {_e(entry.handle)}" if entry.handle and task.kind != "qz:answer" else ""
            lines.append(f"   {task_status_icon(entry)} {_e(task.title)}{extra}")
    if len(rows) > PEOPLE_PER_PAGE:
        lines.append("\nUse 📤 Export CSV for the full list.")
    return "\n".join(lines)


def dues_text(c: Campaign, fmt) -> str:
    lines = [f"💸 <b>{_e(c.title)}</b> · rewards to pay", ""]
    owed = c.owed()
    if not owed:
        lines.append("Nothing to pay right now. ✅")
    for due in c.dues:
        if due.status == "paid":
            continue
        what = {"completion": "finished all tasks", "raffle": "won the lucky draw"}.get(due.kind, f"rank #{due.rank}")
        mark = "⚠️" if due.status == "failed" else "⏳"
        note = f"\n   {_e(due.note)}" if due.note else ""
        lines.append(f"{mark} {_e(due.label)} · {fmt(due.raw)} {_e(c.symbol)} · {what}{note}")
    paid = sum(1 for d in c.dues if d.status == "paid")
    if paid:
        lines += ["", f"Already paid: {paid}"]
    return "\n".join(lines)


def list_line(c: Campaign, now: float) -> str:
    left = format_left(c.ends - now) if c.state == "open" else c.state
    return f"{c.title[:28]} · {left}"
