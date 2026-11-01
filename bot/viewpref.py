"""Which home page each person wants: "text" (the classic page) or "photo" (a picture card).

Kept in viewprefs.json (plain data: telegram id -> choice), so no database change is needed and a
restart keeps everyone's choice. Someone who has never chosen is not in the file; the bot asks them once.
"""
import json
import logging
import os

log = logging.getLogger("walletbot")
PATH = "viewprefs.json"
CHOICES = ("text", "photo")
_PREFS: dict[str, str] = {}
_LOADED = False


def _load() -> None:
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    try:
        with open(PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            _PREFS.update({str(k): v for k, v in data.items() if v in CHOICES})
    except FileNotFoundError:
        pass
    except Exception:
        log.exception("Could not read %s", PATH)


def get(tg_id: int) -> str | None:
    """"text", "photo", or None if this person has not chosen yet."""
    _load()
    return _PREFS.get(str(tg_id))


def mode(tg_id: int) -> str:
    return get(tg_id) or "text"


def set_mode(tg_id: int, choice: str) -> None:
    if choice not in CHOICES:
        return
    _load()
    _PREFS[str(tg_id)] = choice
    tmp = PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(_PREFS, fh)
        os.replace(tmp, PATH)
    except Exception:
        log.exception("Could not save %s", PATH)
