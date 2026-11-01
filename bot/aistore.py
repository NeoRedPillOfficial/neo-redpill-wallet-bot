"""Remembers in which groups an admin switched the AI Assistant's automatic answers off (ai_groups.json).

Only plain data: a list of group ids. Nothing secret is stored.
"""
import json
import logging
import os

log = logging.getLogger("walletbot")
PATH = "ai_groups.json"


def load_off(path: str = PATH) -> set[int]:
    try:
        with open(path, encoding="utf-8") as fh:
            return {int(x) for x in json.load(fh)}
    except FileNotFoundError:
        return set()
    except Exception:
        log.exception("Could not read the AI group settings")
        return set()


def save_off(off: set[int], path: str = PATH) -> None:
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(sorted(off), fh)
        os.replace(tmp, path)
    except Exception:
        log.exception("Could not save the AI group settings")
