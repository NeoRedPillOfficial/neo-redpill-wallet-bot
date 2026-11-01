"""Hourly encrypted backup of the wallet database, sent to Telegram.

The copy is made with SQLite's own backup API (safe while the bot is running,
unlike copying the file), compressed, and encrypted with BACKUP_PASSWORD
(AES-256-GCM, key from scrypt). It never contains the MASTER_KEY: the database
alone cannot be used to reach anyone's funds.

Restore:  python -m bot.backup restore <backup file> [output.db]
"""
import asyncio
import getpass
import gzip
import logging
import os
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

log = logging.getLogger(__name__)

MAGIC = b"NRPB1"
KEEP_DAYS = 14
BACKUP_DIR = Path("backups")
CODE_EVERY = 24 * 3600  # bot code + guide: at least once a day
MAX_TELEGRAM_BYTES = 45 * 1024 * 1024  # Telegram bots may send files up to 50 MB


def sqlite_path(database_url: str) -> str | None:
    """File path of a sqlite DATABASE_URL, or None for any other database."""
    if not database_url.startswith("sqlite"):
        return None
    path = database_url.split("///", 1)[-1].split("?", 1)[0]
    return path or None


def _key(password: str, salt: bytes) -> bytes:
    return Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(password.encode())


def encrypt_bytes(password: str, data: bytes) -> bytes:
    salt, nonce = os.urandom(16), os.urandom(12)
    return MAGIC + salt + nonce + AESGCM(_key(password, salt)).encrypt(nonce, data, MAGIC)


def decrypt_bytes(password: str, blob: bytes) -> bytes:
    if not blob.startswith(MAGIC):
        raise ValueError("This is not a wallet backup file.")
    salt, nonce, body = blob[5:21], blob[21:33], blob[33:]
    try:
        return AESGCM(_key(password, salt)).decrypt(nonce, body, MAGIC)
    except Exception:
        raise ValueError("Wrong backup password, or the file is damaged.") from None


def snapshot(db_file: str) -> bytes:
    """A consistent copy of the database as bytes (works while the bot writes)."""
    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, "copy.db")
        src = sqlite3.connect(db_file, timeout=30)
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        with open(target, "rb") as fh:
            return fh.read()


def make_backup(db_file: str, password: str, folder: Path = BACKUP_DIR) -> Path:
    """Write backups/walletbot-<UTC time>.nrpb and delete ones older than KEEP_DAYS."""
    raw = snapshot(db_file)
    blob = encrypt_bytes(password, gzip.compress(raw, 6))
    folder.mkdir(parents=True, exist_ok=True)
    name = datetime.now(timezone.utc).strftime("walletbot-%Y%m%d-%H%M%S.nrpb")
    path = folder / name
    path.write_bytes(blob)
    cutoff = time.time() - KEEP_DAYS * 86400
    for old in folder.glob("walletbot-*.nrpb"):
        if old != path and old.stat().st_mtime < cutoff:
            old.unlink(missing_ok=True)
    return path


def restore_file(password: str, backup: Path, out: Path) -> None:
    data = gzip.decompress(decrypt_bytes(password, backup.read_bytes()))
    out.write_bytes(data)


CODE_ROOT = Path(__file__).resolve().parent.parent
_CODE_DIRS = ("bot",)  # the only folder that is part of the bot itself (logos/ is a cache the bot refills by itself)
# Code and notes only. Not .json/.png/.jpg: in the main folder those are files the bot rewrites while it runs
# (campaigns.json, rains.json, viewprefs.json ...), and they would make the code look "changed" every hour.
_CODE_SUFFIXES = {".py", ".txt", ".md", ".ini", ".toml", ".cfg", ".json", ".png", ".jpg", ".jpeg", ".ttf"}
_CODE_ROOT_SUFFIXES = {".py", ".txt", ".md", ".ini", ".toml", ".cfg"}
_CODE_ROOT_FILES = {".env.example"}
_CODE_SKIP_PARTS = {"__pycache__", ".pytest_cache", ".venv", "backups"}
_CODE_SKIP_FILES = {"price_history.json"}


def code_files(root: Path = CODE_ROOT) -> list[Path]:
    """The bot's own files, chosen by a strict whitelist: never .env, databases (including their
    -wal/-shm side files), logs, backups or old copies of the bot. Anything not clearly code stays out."""
    found = []
    for path in sorted(root.rglob("*")):
        try:
            if not path.is_file():
                continue
        except OSError:
            continue
        rel = path.relative_to(root)
        if len(rel.parts) == 1:
            if path.name not in _CODE_ROOT_FILES and path.suffix not in _CODE_ROOT_SUFFIXES:
                continue
        elif rel.parts[0] not in _CODE_DIRS:
            continue
        if any(part in _CODE_SKIP_PARTS for part in rel.parts) or path.name in _CODE_SKIP_FILES:
            continue
        if path.name.startswith("update_") or path.name == ".env":
            continue
        if path.suffix not in _CODE_SUFFIXES and path.name not in _CODE_ROOT_FILES:
            continue
        if len(rel.parts) == 1 and path.name not in _CODE_ROOT_FILES and path.suffix not in _CODE_ROOT_SUFFIXES:
            continue
        found.append(path)
    return found


def code_fingerprint(root: Path = CODE_ROOT) -> str:
    import hashlib
    h = hashlib.sha256()
    for path in code_files(root):
        try:
            h.update(str(path.relative_to(root)).encode() + b"\0" + path.read_bytes())
        except OSError:
            continue
    return h.hexdigest()


def make_code_zip(root: Path = CODE_ROOT, folder: Path = BACKUP_DIR) -> tuple[Path, str]:
    """Zip the bot's own files into backups/walletbot-code-<time>.zip. Contains no secrets."""
    import zipfile
    fingerprint = code_fingerprint(root)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / datetime.now(timezone.utc).strftime("walletbot-code-%Y%m%d-%H%M%S.zip")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for f in code_files(root):
            try:
                z.write(f, f.relative_to(root).as_posix())
            except OSError:
                log.warning("Skipped %s in the code backup (could not read it)", f)
    cutoff = time.time() - 14 * 86400
    for old in folder.glob("walletbot-code-*.zip"):
        if old != path and old.stat().st_mtime < cutoff:
            old.unlink(missing_ok=True)
    return path, fingerprint


async def run_forever(bot, resolve_recipients, db_file: str, password: str, every: int = 3600) -> None:
    """Make a backup now and then every `every` seconds, and send it to each recipient.

    `resolve_recipients` is an async function returning the Telegram ids to send to.
    """
    from aiogram.types import FSInputFile

    first = True  # right after the bot starts, the code and the guide are always sent too
    while True:
        try:
            path = await asyncio.to_thread(make_backup, db_file, password)
            size = path.stat().st_size
            if size > MAX_TELEGRAM_BYTES:
                log.error("Backup is %d MB, too big for Telegram; kept only on the server", size // 1048576)
            else:
                ids = await resolve_recipients()
                if not ids:
                    log.warning("Backup made but nobody to send it to yet (they must press Start in the bot)")
                for tg_id in ids:
                    try:
                        await bot.send_document(
                            tg_id, FSInputFile(path, filename=_nice_name("USER-DATA-BACKUP", ".nrpb")),
                            caption=f"🔐 USER DATA backup (wallet database) · {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC\n"
                                    "Encrypted. Keep it; you need the backup password to open it.",
                            disable_notification=True,
                        )
                    except Exception:
                        log.exception("Could not send the backup to %s", tg_id)
            try:
                await _send_code_if_changed(bot, resolve_recipients, force=first)
                first = False
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Code backup failed")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Backup failed")
        await asyncio.sleep(every)


async def _send_code_if_changed(bot, resolve_recipients, force: bool = False) -> None:
    """Send the bot code and the recovery guide once a day, or sooner when the bot's files changed."""
    from aiogram.types import FSInputFile

    state = BACKUP_DIR / "code.state"
    fingerprint = await asyncio.to_thread(code_fingerprint)
    if state.exists() and not force:
        try:
            old_fp, old_time = state.read_text().split()
            if old_fp == fingerprint and time.time() - float(old_time) < CODE_EVERY:
                return
        except ValueError:
            pass
    path, fingerprint = await asyncio.to_thread(make_code_zip)
    guide = CODE_ROOT / "RECOVERY_GUIDE.txt"
    sent = False
    for tg_id in await resolve_recipients():
        try:
            await bot.send_document(
                tg_id, FSInputFile(path, filename=_nice_name("BOT-CODE", ".zip")),
                caption=f"🧩 BOT CODE backup (once a day, and when the bot is updated) · {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC\n"
                        "No passwords or user data inside. Keep only the newest one.",
                disable_notification=True,
            )
            sent = True
            if guide.exists():
                await bot.send_document(
                    tg_id, FSInputFile(guide, filename="RECOVERY-GUIDE.txt"),
                    caption="📘 RECOVERY GUIDE: how to restore the bot on a new server. Keep it with your keys.",
                    disable_notification=True,
                )
        except Exception:
            log.exception("Could not send the code backup to %s", tg_id)
    if sent:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        state.write_text(f"{fingerprint} {time.time()}")


def _nice_name(label: str, ext: str) -> str:
    """The file name people see in Telegram, e.g. BOT-CODE_2026-10-24_1230.zip"""
    return f"{label}_{datetime.now(timezone.utc):%Y-%m-%d_%H%M}{ext}"


def _cli(argv: list[str]) -> int:
    if len(argv) < 3 or argv[1] != "restore":
        print("Usage: python -m bot.backup restore <backup file> [output.db]")
        return 2
    src = Path(argv[2])
    out = Path(argv[3]) if len(argv) > 3 else Path("walletbot-restored.db")
    if out.exists():
        print(f"{out} already exists. Choose another name or delete it first.")
        return 1
    try:
        restore_file(getpass.getpass("Backup password: "), src, out)
    except ValueError as exc:
        print(exc)
        return 1
    print(f"Done. Database written to {out}.")
    print("To use it: stop the bot, rename it to walletbot.db (keep your .env with the same MASTER_KEY), start the bot.")
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv))
