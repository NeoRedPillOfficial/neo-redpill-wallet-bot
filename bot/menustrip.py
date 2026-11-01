"""Photo-mode cleanup of the buttons under bot messages.

People who chose the photo home page have every menu action in the keyboard under the chat box,
so the big menu buttons (Send, Deposit, ...) and the Back / Home buttons are dropped from bot messages.
Buttons that finish a step (choose a token, Confirm, Cancel, page arrows) stay.
Pure functions: they work on any object with .text and .callback_data.
"""

MENU_LABELS = {
    "💸 Send", "📥 Deposit", "🌉 Bridge GAS", "🌉 Bridge", "➕ Add token", "📜 History", "🔄 Refresh",
    "🔑 Import wallet", "📤 Export wallet", "🗑 Remove token", "❓ Help", "🌧 Rain & 🧠 Quiz",
    "🖼 Home view: Text / Photo", "📈 Trade", "🔁 Trade & Bridge", "💎 Earn", "🪙 Add / Remove token",
}


def is_menu_button(button) -> bool:
    text = getattr(button, "text", "") or ""
    data = getattr(button, "callback_data", None)
    if text in MENU_LABELS:
        return True
    # "Back" / "Home" buttons go to the home page; a Cancel button also does, and must stay
    return data == "home" and "cancel" not in text.lower()


def strip_rows(rows: list) -> list:
    """The rows without menu buttons. Rows that end up empty are dropped."""
    out = []
    for row in rows:
        kept = [b for b in row if not is_menu_button(b)]
        if kept:
            out.append(kept)
    return out
