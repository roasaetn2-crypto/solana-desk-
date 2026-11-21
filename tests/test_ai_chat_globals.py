"""Desk AI drew a blank chat because ai-chat.js used SEAT_NAME, which a cleanup removed from app.js.

ai-chat.js shares globals with app.js (classic scripts). A bare reference to one that no longer exists
throws inside render(), and the chat stays empty. These checks keep the two files in step.
"""
import re
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"
CHAT = (WEB / "ai-chat.js").read_text(encoding="utf-8")
APP = (WEB / "app.js").read_text(encoding="utf-8")


def test_helpers_ai_chat_needs_still_exist_in_app_js():
    assert re.search(r"^const esc\s*=", APP, re.M), "esc() was removed from app.js"
    assert re.search(r"^function headImg\(", APP, re.M), "headImg() was removed from app.js"


def test_seat_name_is_never_used_bare():
    # allowed: the typeof guard inside seatName(); anything else would throw when it is gone
    bare = [m.start() for m in re.finditer(r"(?<![\w.])SEAT_NAME(?!\w)", CHAT)]
    # every remaining occurrence must sit on the guarded line
    for b in bare:
        line = CHAT[CHAT.rfind("\n", 0, b) + 1:CHAT.find("\n", b)]
        assert "typeof SEAT_NAME" in line or line.lstrip().startswith("//"), line


def test_chat_renders_agent_chips_through_the_safe_helper():
    assert "esc(seatName(a))" in CHAT
