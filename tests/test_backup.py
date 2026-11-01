import sqlite3
import pytest
from bot import backup


def test_roundtrip(tmp_path):
    db = tmp_path / "w.db"
    c = sqlite3.connect(db); c.execute("create table t(x)"); c.execute("insert into t values (42)"); c.commit(); c.close()
    p = backup.make_backup(str(db), "pw", tmp_path / "b")
    assert b"SQLite" not in p.read_bytes()
    out = tmp_path / "r.db"
    backup.restore_file("pw", p, out)
    assert sqlite3.connect(out).execute("select x from t").fetchone() == (42,)


def test_wrong_password(tmp_path):
    blob = backup.encrypt_bytes("a", b"data")
    with pytest.raises(ValueError):
        backup.decrypt_bytes("b", blob)


def test_sqlite_path():
    assert backup.sqlite_path("sqlite+aiosqlite:///./walletbot.db") == "./walletbot.db"
    assert backup.sqlite_path("postgresql+asyncpg://x") is None


def test_files_the_bot_rewrites_while_running_do_not_count_as_code(tmp_path):
    from bot import backup

    (tmp_path / "bot").mkdir()
    (tmp_path / "bot" / "main.py").write_text("print(1)")
    (tmp_path / "logos").mkdir()
    (tmp_path / "logos" / "coin_NEO.png").write_bytes(b"x")
    (tmp_path / "requirements.txt").write_text("aiogram")
    first = backup.code_fingerprint(tmp_path)
    for name in ("campaigns.json", "rains.json", "viewprefs.json", "keyboards.json", "flamingo_routes.json"):
        (tmp_path / name).write_text("{}")
    (tmp_path / "logos" / "coin_GAS.png").write_bytes(b"y")
    (tmp_path / "chart.png").write_bytes(b"z")
    names = {p.name for p in backup.code_files(tmp_path)}
    assert names == {"main.py", "requirements.txt"}
    assert backup.code_fingerprint(tmp_path) == first  # runtime files do not make the code look changed
    (tmp_path / "bot" / "main.py").write_text("print(2)")
    assert backup.code_fingerprint(tmp_path) != first  # a real code change does
