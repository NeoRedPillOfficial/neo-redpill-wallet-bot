from collections import namedtuple

from bot import menustrip

B = namedtuple("B", "text callback_data")


def test_menu_and_back_buttons_are_dropped_but_step_buttons_stay():
    rows = [
        [B("💸 Send", "send"), B("📥 Deposit", "wallets")],
        [B("GAS (Neo N3)", "tok:1")],
        [B("❌ Cancel", "home")],
        [B("⬅️ Back", "home"), B("🏠 Home", "home")],
        [B("⬅️ Help menu", "help")],
    ]
    kept = menustrip.strip_rows(rows)
    assert kept == [[B("GAS (Neo N3)", "tok:1")], [B("❌ Cancel", "home")], [B("⬅️ Help menu", "help")]]


def test_full_home_keyboard_becomes_empty():
    rows = [[B("💸 Send", "send")], [B("🔄 Refresh", "home")], [B("🖼 Home view: Text / Photo", "viewmenu")]]
    assert menustrip.strip_rows(rows) == []


def test_wallet_keys_page_buttons_survive_photo_mode():
    from types import SimpleNamespace as B
    for text in ("📤 Export a wallet", "📥 Import a wallet", "🔄 Switch wallet", "🗑 Delete a wallet"):
        assert not menustrip.is_menu_button(B(text=text, callback_data="x"))
