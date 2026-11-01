"""Counts how often each part of the bot is used (button taps and commands), per day, for the owners' statistic.
Only a feature name and a Telegram id are kept, never what the person typed."""
import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone

log = logging.getLogger("walletbot.usage")
FILE = "usage.json"
KEEP_DAYS = 40

FEATURES = (  # (label, button prefixes, commands)
    ("🔄 Trade (swap)", ("f:", "fa:", "fd:", "fhi:", "fok:", "fs:", "m:", "md:", "q:", "qa:", "qd:", "qhi:", "qok:", "qs:", "trc:", "trd:", "tb", "trade"), ("trade",)),
    ("↗️ Send", ("send", "sendok:", "sendtok:"), ("send",)),
    ("🎁 Tip", ("tipok:", "tipno:", "tpl:", "tpn:"), ("tip",)),
    ("🌧 Rain", ("rain:", "raincancel:", "rainno:", "rainok:", "prep:rain"), ("rain",)),
    ("🧠 Quiz", ("qz:", "qzgo:", "qzno:", "qzstart:", "qzstop:", "prep:quiz", "prq:done", "prq:more"), ("quiz",)),
    ("🎉 Airdrops and events", ("air", "airmine", "events", "mine", "aist:now", "pstop:", "pcancel:"), ("airdrop",)),
    ("🌉 Bridge", ("bridge", "brdir:", "brok:", "xb", "xba:", "xbd:", "xbl", "xbok:", "xbr:", "xbs:", "xbt:"), ("bridge",)),
    ("🗳 Vote and Earn", ("vote", "vt:c", "vt:m", "earn"), ("vote", "earn")),
    ("👛 Wallets (keys, import, export, switch)", ("keys", "wallets", "import", "importgo:", "export", "exportgo:", "swm", "delm", "sw:", "dl1", "dl2", "dl3", "ex:"), ("wallets", "keys", "import", "export")),
    ("🪙 Tokens (add, remove)", ("addtok", "addnet:", "addok:", "rmtok", "tokmenu", "unl:"), ("addtoken", "removetoken")),
    ("📜 History and pending", ("histclr", "pclear"), ("history", "pending")),
    ("🤖 AI Assistant", ("ai:start", "ai:reset", "aicr", "aisc", "aist", "aiskip", "aibskip"), ("ai",)),
)

_STATE: dict = {"days": {}, "dirty": False, "loaded": False}


def feature_of(callback_data: str | None = None, command: str | None = None) -> str | None:
    for label, prefixes, commands in FEATURES:
        if command and command.lower() in commands:
            return label
        if callback_data and any(callback_data == p or (p.endswith(":") and callback_data.startswith(p)) for p in prefixes):
            return label
    return None


def _load() -> None:
    if _STATE["loaded"]:
        return
    _STATE["loaded"] = True
    try:
        with open(FILE) as f:
            _STATE["days"] = json.load(f)
    except (OSError, ValueError):
        _STATE["days"] = {}


def record(user_id: int, label: str | None, now: datetime | None = None) -> None:
    if not label:
        return
    _load()
    day = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
    entry = _STATE["days"].setdefault(day, {}).setdefault(label, [0, []])
    entry[0] += 1
    if user_id not in entry[1]:
        entry[1].append(user_id)
    _STATE["dirty"] = True


def summary(days: int, now: datetime | None = None) -> list[tuple[str, int, int]]:
    """[(feature, taps, different people)] over the last `days` days, busiest first."""
    _load()
    now = now or datetime.now(timezone.utc)
    wanted = {(now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)}
    taps: dict[str, int] = {}
    people: dict[str, set] = {}
    for day, features in _STATE["days"].items():
        if day not in wanted:
            continue
        for label, (count, ids) in features.items():
            taps[label] = taps.get(label, 0) + count
            people.setdefault(label, set()).update(ids)
    return sorted(((label, taps[label], len(people[label])) for label in taps), key=lambda r: -r[1])


def render(now: datetime | None = None) -> str:
    lines = ["🧭 <b>Which parts of the bot are used</b> <i>(button taps and commands; people = different users)</i>"]
    for title, days in (("Last 24 hours", 1), ("Last 7 days", 7)):
        rows = summary(days, now)
        lines.append(f"\n<b>{title}</b>")
        lines += [f"{label}: {taps} taps · {people} people" for label, taps, people in rows] or ["No use recorded yet"]
    return "\n".join(lines)


def flush() -> None:
    if not _STATE["dirty"]:
        return
    cutoff = (datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    _STATE["days"] = {d: v for d, v in _STATE["days"].items() if d >= cutoff}
    tmp = FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(_STATE["days"], f)
    os.replace(tmp, FILE)
    _STATE["dirty"] = False


async def run_forever(every: int = 60) -> None:
    try:
        while True:
            await asyncio.sleep(every)
            try:
                flush()
            except Exception:
                log.exception("Could not save the usage counts")
    finally:
        try:
            flush()
        except Exception:
            pass
