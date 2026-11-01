"""Group events: rain (a first-come, first-served airdrop) and quiz (multiple choice, with prizes).

Pure logic only, so it can be tested without Telegram; main.py wires it to the chat.

Nothing is held in escrow: the tokens stay in the organizer's own wallet until the event ends, and the
prizes are then sent as ordinary on-chain transfers (the same engine as /tip, so the same checks, limits
and fee rules apply). Events live in memory: if the bot restarts, a running event simply stops and
nothing has been sent.
"""
import asyncio
import random
import re
import secrets
import time
from dataclasses import dataclass, field

from . import neo_send

MAX_RAIN_PEOPLE = 20  # every winner is a separate on-chain transfer, one after another
DEFAULT_RAIN_PEOPLE = 5
MAX_MINUTES = 525600  # one year: in practice a rain never times out; it ends when full or cancelled
DEFAULT_MINUTES = 5

MAX_QUESTIONS = 10
QUESTION_SECONDS = 30  # the default time for a question
MIN_SECONDS = 5
MAX_SECONDS = 600
CLAIM_HOURS = 24  # how long quiz winners have to claim their reward
MAX_QUESTION_LEN = 200
MAX_OPTION_LEN = 60
SETUP_TTL_SECONDS = 900

RAIN_USAGE = (
    "<b>How to start a rain</b> (group admins only)\n"
    "<code>/rain 100 NRP</code>: 100 NRP shared by the first 5 people to tap Claim\n"
    "<code>/rain 100 NRP 10</code>: first 10 people, equal shares (the default, same as <code>fixed</code>)\n"
    "<code>/rain 100 NRP 10 random</code>: the same 100 NRP, but every claimer gets a different random share\n"
    "Add a network if the symbol exists on several: <code>/rain 50 USDT 5 bsc</code>\n"
    f"Up to {MAX_RAIN_PEOPLE} people. The rain ends when everyone has claimed, or when you press Cancel. "
    "Each prize is sent from your own wallet the moment someone claims."
)
QUIZ_USAGE = (
    "<b>How to start a quiz</b>\n"
    "The easy way: prepare it in my private chat (🌧 Rain & 🧠 Quiz) and send its code in a group, like <code>/quiz Q-3HDP</code>.\n"
    "Group admins can also type it: <code>/quiz 10 NRP</code> gives 10 NRP to the fastest correct answer on each question.\n"
    "Add a network if the symbol exists on several: <code>/quiz 5 USDT bsc</code>"
)
QUESTIONS_HELP = (
    "<b>Send me the questions</b>, one per line, in this form:\n"
    "<code>question | correct answer | wrong answer | wrong answer</code>\n\n"
    "Example:\n"
    "<code>In what year was Bitcoin created? | 2009 | 2012 | 2015\n"
    "What is the symbol of the Neo gas token? | GAS | FUEL</code>\n\n"
    "• Each question has the correct answer first, then 1 to 3 wrong ones (I shuffle them).\n"
    f"• Up to {MAX_QUESTIONS} questions.\n"
    "• Members answer by tapping buttons. A wrong tap locks that person out of the question."
)


class EventError(Exception):
    """A problem the organizer can fix; the message is safe to show in the group."""


# ------------------------------------------------------------------------- rain
@dataclass
class RainRequest:
    amount_text: str
    symbol: str
    network: str | None
    people: int
    minutes: int
    mode: str = "fixed"  # fixed: equal shares | random: different random shares of the same total


def parse_rain(args: str) -> RainRequest:
    """Text after /rain: amount, symbol, then optionally the number of people and a network.

    There is no time to choose: the rain ends when it is full or the organizer cancels. `minutes` is only a
    safety limit so a forgotten rain can't stay open forever."""
    parts = args.split()
    if len(parts) < 2:
        raise EventError("I need an amount and a token symbol.")
    amount_text, symbol = parts[0], parts[1]
    if not symbol.isalnum():
        raise EventError("That token symbol doesn't look right.")
    numbers: list[int] = []
    network = None
    mode = None
    for part in parts[2:]:
        if part.lower() in ("fixed", "random"):
            if mode is not None:
                raise EventError("Choose either fixed or random, not both.")
            mode = part.lower()
        elif part.isdigit():
            numbers.append(int(part))
        elif network is None and part.replace("_", "").isalnum():  # network names like neo_x
            network = part
        else:
            raise EventError(f"I didn't understand “{part[:20]}”.")
    if len(numbers) > 1:
        raise EventError("Too many numbers. Use: amount symbol [people] [network].")
    people = numbers[0] if numbers else DEFAULT_RAIN_PEOPLE
    minutes = MAX_MINUTES
    if not 1 <= people <= MAX_RAIN_PEOPLE:
        raise EventError(f"People must be between 1 and {MAX_RAIN_PEOPLE}.")
    return RainRequest(amount_text, symbol, network, people, minutes, mode or "fixed")


def rain_amounts(amount_text: str, decimals: int, people: int, mode: str = "fixed",
                 rng: random.Random | None = None) -> tuple[list, int]:
    """([amount for each claimer, in claim order], total) in raw units.

    fixed: equal shares; a remainder that doesn't divide stays with the organizer.
    random: the whole amount is split into different random shares. Everyone gets at least a quarter of an
    equal share, so nobody ends up with dust. The shares add up to exactly the amount.
    """
    try:
        value = neo_send.parse_amount(amount_text, decimals)
    except neo_send.SendError as exc:
        raise EventError(str(exc))
    each = value // people
    if each <= 0:
        raise EventError("That amount is too small to share between so many people.")
    if mode != "random" or people == 1:
        return [each] * people, each * people
    rng = rng or random.SystemRandom()
    floor = max(1, each // 4)
    spare = value - floor * people
    weights = [rng.random() + 0.05 for _ in range(people)]
    shares = [floor + int(spare * w / sum(weights)) for w in weights]
    shares[rng.randrange(people)] += value - sum(shares)  # rounding goes to someone, so the total is exact
    rng.shuffle(shares)
    return shares, value


def quiz_amounts(amount_text: str, decimals: int, slots: int, mode: str = "fixed",
                 rng: random.Random | None = None) -> tuple[list, int]:
    """([prize for each winner slot], total) in raw units.

    fixed: the amount is what every winner gets. random: the amount is a total that is split into different random
    shares, one per winner slot (the shares add up to exactly that total).
    """
    if mode == "random":
        return rain_amounts(amount_text, decimals, slots, "random", rng)
    try:
        each = neo_send.parse_amount(amount_text, decimals)
    except neo_send.SendError as exc:
        raise EventError(str(exc))
    if each <= 0:
        raise EventError("The prize must be more than zero.")
    return [each] * slots, each * slots


def format_clock(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


@dataclass
class Rain:
    id: str
    chat_id: int
    starter_tg_id: int
    starter_user_id: int
    starter_label: str
    slug: str
    network_name: str
    token_id: int
    symbol: str
    decimals: int
    each_raw: int
    people: int
    minutes: int
    ends_at: float = 0.0  # safety deadline (time.monotonic()), set when the organizer confirms
    started_at: float = 0.0  # time.monotonic() when the rain began
    finished_at: float = 0.0  # when the last claim came in, or the rain was stopped
    message_id: int | None = None
    claimed: dict = field(default_factory=dict)  # telegram id -> (user_id, label, address)
    mode: str = "fixed"
    amounts: list = field(default_factory=list)  # prize per claimer in claim order (random mode: all different)
    claim_amounts: dict = field(default_factory=dict)  # telegram id -> raw amount they were promised
    state: str = "open"  # open | paying | cancelled | done
    lines: list = field(default_factory=list)  # one result line per payout, shown in the group
    paid: int = 0
    pending: int = 0  # payouts still being sent
    failed: bool = False  # a payout failed: no more claims are accepted
    chat_title: str = ""
    code: str = ""  # the code of the rain prepared in private, if it came from there
    poster_tg_id: int = 0  # who posted the code in the group, when that is not the person who prepared it
    pinned: bool = False  # the rain message was pinned (only when a group admin posted it)
    inflight: set = field(default_factory=set)  # telegram ids whose payout is being sent right now
    wake: asyncio.Event = field(default_factory=asyncio.Event)

    def seconds_left(self, now: float | None = None) -> float:
        return self.ends_at - (time.monotonic() if now is None else now)

    def elapsed(self, now: float | None = None) -> float:
        end = self.finished_at or (time.monotonic() if now is None else now)
        return max(0.0, end - self.started_at)

    def is_full(self) -> bool:
        return len(self.claimed) >= self.people

    def try_claim(self, tg_id: int, user_id: int, label: str, address: str) -> str:
        """'ok', 'closed', 'owner' (the organizer can't claim), 'dup' or 'full'. Never raises."""
        if self.state != "open" or self.seconds_left() <= 0:
            return "closed"
        if tg_id == self.starter_tg_id:
            return "owner"
        if tg_id in self.claimed:
            return "dup"
        if self.is_full():
            return "full"
        self.claimed[tg_id] = (user_id, label, address)
        index = len(self.claimed) - 1
        self.claim_amounts[tg_id] = self.amounts[index] if index < len(self.amounts) else self.each_raw
        self.wake.set()
        return "ok"


def new_id() -> str:
    return secrets.token_hex(8)


# ------------------------------------------------------------------------- quiz
@dataclass
class Question:
    text: str
    correct: str
    wrong: list


def build_question(number: int, text: str, correct: str, wrong: list) -> Question:
    """Check one question and its answers. `number` is only used in the error messages."""
    text, correct, wrong = text.strip(), correct.strip(), [w.strip() for w in wrong]
    if not 1 <= len(wrong) <= 3:
        raise EventError(f"Question {number}: give 1 to 3 wrong answers.")
    if not text or len(text) > MAX_QUESTION_LEN:
        raise EventError(f"Question {number}: the question must be 1 to {MAX_QUESTION_LEN} characters.")
    options = [correct] + wrong
    if any(not o or len(o) > MAX_OPTION_LEN for o in options):
        raise EventError(f"Question {number}: every answer must be 1 to {MAX_OPTION_LEN} characters.")
    if len({o.lower() for o in options}) != len(options):
        raise EventError(f"Question {number}: two answers are the same.")
    return Question(text, correct, wrong)


def parse_questions(text: str) -> list[Question]:
    """One question per line: question | correct | wrong [| wrong [| wrong]]."""
    out: list[Question] = []
    for number, raw in enumerate((ln for ln in text.splitlines() if ln.strip()), start=1):
        parts = [p.strip() for p in raw.split("|")]
        if len(parts) < 3 or len(parts) > 5:
            raise EventError(
                f"Line {number}: use “question | correct answer | wrong answer” (up to 3 wrong answers)."
            )
        out.append(build_question(number, parts[0], parts[1], parts[2:]))
        if len(out) > MAX_QUESTIONS:
            raise EventError(f"At most {MAX_QUESTIONS} questions.")
    if not out:
        raise EventError("I didn't find any questions.")
    return out


MAX_QUESTION_WINNERS = 10  # fastest correct answers that win on each question
MAX_TOP_WINNERS = 50  # best scores that win at the end


def format_seconds(seconds: int) -> str:
    """10 -> '10s', 60 -> '1 min', 90 -> '1 min 30s'."""
    seconds = int(seconds)
    minutes, rest = divmod(seconds, 60)
    if not minutes:
        return f"{rest}s"
    return f"{minutes} min" + (f" {rest}s" if rest else "")


_SECONDS_RE = re.compile(r"^(\d{1,4})\s*(s|sec|secs|seconds?|m|min|mins|minutes?)?$", re.I)


def parse_seconds(text: str) -> int:
    """'45', '45s', '2m', '1 min' -> seconds. A bare number means seconds."""
    match = _SECONDS_RE.match(text.strip())
    if not match:
        raise EventError("Send the time like 30, 45s or 2 min.")
    value = int(match.group(1))
    if (match.group(2) or "s").lower().startswith("m"):
        value *= 60
    if not MIN_SECONDS <= value <= MAX_SECONDS:
        raise EventError(f"The time must be between {format_seconds(MIN_SECONDS)} and {format_seconds(MAX_SECONDS)}.")
    return value


def rank_parts(text: str) -> list[str]:
    """'1000, 500 250' -> ['1000', '500', '250']"""
    return [p for p in re.split(r"[,\s;]+", text.strip()) if p]


def parse_rank_amounts(text: str, decimals: int) -> list[int]:
    """'1000, 500, 250' -> the prize for 1st, 2nd, 3rd place, in raw units."""
    parts = rank_parts(text)
    if not parts:
        raise EventError("Send the amounts like 1000, 500, 250 (first place, second place, ...).")
    out = []
    for part in parts:
        try:
            value = neo_send.parse_amount(part, decimals)
        except neo_send.SendError as exc:
            raise EventError(f"“{part[:20]}”: {exc}")
        if value <= 0:
            raise EventError("Every prize must be more than zero.")
        out.append(value)
    return out


def winners_limit(style: str) -> int:
    return MAX_QUESTION_WINNERS if style == "question" else MAX_TOP_WINNERS


def plan_amounts(style: str, mode: str, winners: int, amount_text: str, decimals: int,
                 question_count: int) -> tuple[list, int]:
    """([prize for every winner slot], number of winners). Raises EventError if the amount doesn't fit.

    Slots are listed question by question (style "question") or by place (style "top")."""
    if mode == "ranked":
        per_rank = parse_rank_amounts(amount_text, decimals)
        return (per_rank * question_count if style == "question" else per_rank), len(per_rank)
    slots = winners * question_count if style == "question" else winners
    amounts, _total = quiz_amounts(amount_text, decimals, slots, mode)
    return amounts, winners


@dataclass
class QuizSetup:
    """A quiz being set up. The questions arrive (and the prizes are worked out) before it starts.

    style "question": on every question the fastest correct answers win, `winners` of them.
    style "top": everybody answers every question; at the end the `winners` best scores win, a tie
    going to whoever answered faster overall (the times are kept secret).
    mode "fixed": amount_text is what every winner gets. mode "random": it is a total shared at random.
    mode "ranked": amount_text lists the prize for 1st place, 2nd place, ... ("1000, 500, 250"); that also sets `winners`.
    """
    id: str
    chat_id: int
    starter_tg_id: int
    starter_user_id: int
    starter_label: str
    slug: str
    network_name: str
    token_id: int
    symbol: str
    decimals: int
    amount_text: str
    style: str = "question"
    mode: str = "fixed"
    winners: int = 1
    seconds: int = QUESTION_SECONDS  # how long each question stays open
    expires: float = field(default_factory=lambda: time.monotonic() + SETUP_TTL_SECONDS)
    questions: list | None = None
    amounts: list = field(default_factory=list)  # one prize per winner slot
    each_raw: int = 0  # the biggest single prize: what the send limits are checked against
    chat_title: str = ""
    code: str = ""  # the code of the quiz prepared in private, if it came from there
    poster_tg_id: int = 0  # who posted the code in the group, when that is not the person who prepared it

    @property
    def is_random(self) -> bool:
        return self.mode == "random"

    @property
    def top_raw(self) -> int:
        return self.each_raw

    @property
    def total_raw(self) -> int:
        return sum(self.amounts)

    def slot_count(self, question_count: int) -> int:
        return self.winners * question_count if self.style == "question" else self.winners

    def plan(self, questions: list) -> None:
        """Fix the questions and work out every prize. Raises EventError if the amount doesn't fit."""
        amounts, self.winners = plan_amounts(
            self.style, self.mode, self.winners, self.amount_text, self.decimals, len(questions)
        )
        self.questions, self.amounts, self.each_raw = questions, amounts, max(amounts)

    def prize_for(self, question_index: int, slot: int) -> int:
        """Style "question": the prize for the fastest (slot 0), second fastest (slot 1), ... on a question."""
        return self.amounts[question_index * self.winners + slot]


@dataclass
class Win:
    tg_id: int
    user_id: int
    label: str
    address: str
    prize: int = 0  # raw amount for the question they won


@dataclass
class Player:
    """Style "top": one person's score."""
    tg_id: int
    user_id: int
    label: str
    address: str
    correct: int = 0
    seconds: float = 0.0  # total time of their correct answers: breaks ties, never shown to anyone


def rank_players(players: dict) -> list:
    """Players with at least one correct answer, best first: most correct, then least total time."""
    return sorted((p for p in players.values() if p.correct > 0), key=lambda p: (-p.correct, p.seconds))


@dataclass
class Round:
    index: int
    question: Question
    options: list
    correct_pos: int
    slots: list = field(default_factory=list)  # style "question": who holds each prize slot (None = free)
    winners: dict = field(default_factory=dict)  # slot -> Win, once the winner's wallet is known
    answered: set = field(default_factory=set)
    closed: bool = False
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    started: float = 0.0  # time.monotonic() when the question was shown
    correct_count: int = 0  # style "top": how many answered correctly

    @classmethod
    def make(cls, index: int, question: Question, rng: random.Random | None = None, slots: int = 0) -> "Round":
        rng = rng or random.SystemRandom()
        options = [question.correct] + list(question.wrong)
        rng.shuffle(options)
        return cls(index, question, options, options.index(question.correct), slots=[None] * slots)

    def answer(self, tg_id: int, pos: int) -> tuple[str, int | None]:
        """Style "question". ('win', slot), ('wrong', None), ('already', None), ('late', None) or ('closed', None).

        No awaits in here, so the fastest correct taps get the slots in the order they arrive."""
        if self.closed:
            return "closed", None
        if tg_id in self.answered:
            return "already", None
        free = next((i for i, holder in enumerate(self.slots) if holder is None), None)
        if free is None:
            return "late", None
        self.answered.add(tg_id)
        if pos != self.correct_pos:
            return "wrong", None
        self.slots[free] = tg_id
        return "win", free

    def release(self, tg_id: int, slot: int) -> None:
        """Undo a win whose wallet lookup failed, so someone else can still take the slot."""
        if self.slots[slot] == tg_id and slot not in self.winners:
            self.slots[slot] = None
        self.answered.discard(tg_id)

    def complete(self) -> bool:
        return bool(self.slots) and len(self.winners) >= len(self.slots)

    def lookups_pending(self) -> bool:
        return any(holder is not None and slot not in self.winners for slot, holder in enumerate(self.slots))

    def answer_top(self, tg_id: int, pos: int, now: float | None = None) -> tuple[str, bool, float]:
        """Style "top". ('ok', was_correct, seconds_since_shown), ('already', ...) or ('closed', ...)."""
        if self.closed:
            return "closed", False, 0.0
        if tg_id in self.answered:
            return "already", False, 0.0
        self.answered.add(tg_id)
        correct = pos == self.correct_pos
        if correct:
            self.correct_count += 1
        return "ok", correct, max(0.0, (time.monotonic() if now is None else now) - self.started)


@dataclass
class Quiz:
    id: str
    setup: QuizSetup
    questions: list
    current: Round | None = None
    cancelled: bool = False  # the organizer pressed Cancel
    wins: list = field(default_factory=list)  # style "question": a Win for every prize slot that was won
    players: dict = field(default_factory=dict)  # style "top": telegram id -> Player


def wins_by_user(wins: list) -> list[tuple[Win, int, int]]:
    """[(Win, number of prizes won, total prize in raw units)] in order of first win."""
    order: list[int] = []
    count: dict[int, int] = {}
    total: dict[int, int] = {}
    first: dict[int, Win] = {}
    for win in wins:
        if win.tg_id not in count:
            order.append(win.tg_id)
            first[win.tg_id] = win
            count[win.tg_id] = 0
            total[win.tg_id] = 0
        count[win.tg_id] += 1
        total[win.tg_id] += win.prize
    return [(first[t], count[t], total[t]) for t in order]


# ------------------------------------------------------------------- prepared events and their codes
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O or 1/I, so a code is easy to read out
_CODE_RE = re.compile(r"^([RQ])-?([A-HJ-NP-Z2-9]{4})$")
MAX_PENDING = 10  # prepared but not yet sent, per person


def new_code(kind: str, taken) -> str:
    """A random code like R-7K2Q (rain) or Q-3HDP (quiz) that nobody has now."""
    prefix = "R" if kind == "rain" else "Q"
    while True:
        code = f"{prefix}-" + "".join(secrets.choice(CODE_ALPHABET) for _ in range(4))
        if code not in taken:
            return code


def normalize_code(text: str) -> str | None:
    """'r7k2q', 'R-7K2Q' -> 'R-7K2Q'; None if the text isn't shaped like a code."""
    match = _CODE_RE.match(text.strip().upper())
    return f"{match.group(1)}-{match.group(2)}" if match else None


@dataclass
class Prepared:
    """A rain or quiz that its owner set up in private and can send to a group with its code."""
    code: str
    kind: str  # rain | quiz
    req: object = None  # RainRequest, for a rain
    quiz: dict | None = None  # {amount_text, symbol, network, questions}, for a quiz
    status: str = "pending"  # pending | live | done | cancelled
    note: str = ""  # where it went, or how it ended


# ------------------------------------------------------------------- claiming quiz rewards
@dataclass
class Prize:
    """What one quiz winner is owed."""
    label: str
    amount: int  # raw units
    note: str = ""
    claimed: bool = False
    claiming: bool = False  # a payout is being sent right now


@dataclass
class ClaimBoard:
    """The winners of a quiz. Rewards are sent when each winner claims, not before: a winner may never
    have started the bot, and opening it to claim is what starts it."""
    id: str
    chat_id: int
    starter_tg_id: int
    starter_user_id: int
    slug: str
    network_name: str
    token_id: int
    symbol: str
    decimals: int
    head: str
    prizes: dict = field(default_factory=dict)  # telegram id -> Prize
    message_id: int | None = None
    expires_at: float = 0.0  # time.time() deadline (wall clock, so it survives a restart)
    code: str = ""

    def expired(self, now: float | None = None) -> bool:
        return (time.time() if now is None else now) >= self.expires_at

    def to_dict(self) -> dict:
        data = {k: getattr(self, k) for k in (
            "id", "chat_id", "starter_tg_id", "starter_user_id", "slug", "network_name", "token_id", "symbol",
            "decimals", "head", "message_id", "expires_at", "code")}
        data["prizes"] = {
            str(tg): {"label": p.label, "amount": p.amount, "note": p.note, "claimed": p.claimed}
            for tg, p in self.prizes.items()
        }
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "ClaimBoard":
        prizes = {
            int(tg): Prize(p["label"], int(p["amount"]), p.get("note", ""), bool(p.get("claimed", False)))
            for tg, p in data.get("prizes", {}).items()
        }
        keep = {k: data[k] for k in (
            "id", "chat_id", "starter_tg_id", "starter_user_id", "slug", "network_name", "token_id", "symbol",
            "decimals", "head")}
        return cls(**keep, prizes=prizes, message_id=data.get("message_id"), expires_at=float(data.get("expires_at", 0)),
                   code=data.get("code", ""))
