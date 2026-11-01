"""Keeps the rains that are still open in rains.json, so a restart of the bot doesn't end them.

Only plain data goes in the file (no keys, no secrets): who started the rain, where, how much, and who claimed.
"""
import json
import logging
import os

log = logging.getLogger("walletbot")
PATH = "rains.json"


def save(rains: list[dict], path: str = PATH) -> None:
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(rains, fh)
        os.replace(tmp, path)  # all or nothing: a crash never leaves half a file
    except Exception:
        log.exception("Could not save the open rains")


def load(path: str = PATH) -> list[dict]:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return [d for d in data if isinstance(d, dict)]
    except FileNotFoundError:
        return []
    except Exception:
        log.exception("Could not read the saved rains")
        return []
