"""Remembers who has been given the current set of menu buttons under the chat box.

A Telegram keyboard only changes when the bot sends a message with the new one, so the first time someone talks to the
bot after an update that changes the buttons, they get a short note with the new keyboard. Kept in keyboards.json
(plain list of telegram ids), so a restart does not repeat it.
"""
import json
import logging

log = logging.getLogger("walletbot")
PATH = "keyboards.json"
VERSION = "v2"  # change this when the buttons change, so everyone gets the new ones once
_SEEN: set[str] = set()
_LOADED = False


def _load() -> None:
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    try:
        with open(PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and data.get("version") == VERSION:
            _SEEN.update(str(x) for x in data.get("ids", []))
    except FileNotFoundError:
        pass
    except Exception:
        log.exception("Could not read %s", PATH)


def seen(tg_id: int) -> bool:
    _load()
    return str(tg_id) in _SEEN


def mark(tg_id: int) -> None:
    _load()
    _SEEN.add(str(tg_id))
    try:
        with open(PATH, "w", encoding="utf-8") as fh:
            json.dump({"version": VERSION, "ids": sorted(_SEEN)}, fh)
    except Exception:
        log.exception("Could not save %s", PATH)
