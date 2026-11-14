"""The dashboard order is a trading decision, not a cosmetic one.

On a phone the old shell put the tab bar *after* the hero, the backtest, the
agent roster and the activity feed, so the navigation only appeared once you had
scrolled past four sections -- and open positions, the only thing you act on,
sat behind all of it. These tests pin the trader-first order so a later markup
edit cannot quietly put the chart back in front of the book.
"""
from __future__ import annotations

from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"


def html() -> str:
    return (WEB / "index.html").read_text(encoding="utf-8")


def js() -> str:
    return (WEB / "app.js").read_text(encoding="utf-8")


def at(hay: str, needle: str) -> int:
    i = hay.find(needle)
    assert i >= 0, f"missing {needle!r}"
    return i


def test_tab_bar_comes_before_the_hero() -> None:
    h = html()
    assert at(h, 'id="deskTabs"') < at(h, 'class="hero" id="hero"')


def test_positions_come_before_the_equity_curve() -> None:
    """The book leads. The curve is context, not the headline."""
    h = html()
    assert at(h, 'id="posCards"') < at(h, 'class="hero" id="hero"')


def test_hero_comes_before_the_decision_log() -> None:
    h = html()
    assert at(h, 'class="hero" id="hero"') < at(h, 'id="decBody"')


def test_read_only_boards_sit_below_the_desk() -> None:
    """Backtest/agents/activity are reference; they must not push the book down."""
    h = html()
    main_end = at(h, "</main>")
    for sid in ("backtestSec", "agentsSec", "activitySec"):
        assert at(h, f'id="{sid}"') > main_end, f"{sid} should sit below </main>"


def test_execution_mode_leads_the_control_column() -> None:
    """The only panel that changes desk behaviour should be first on the column."""
    h = html()
    left = h.split('class="col col-left"', 1)[1].split("</section>", 1)[0]
    assert at(left, 'id="execPanel"') < at(left, "The seats")


def test_groq_panels_are_folded_by_default() -> None:
    """Sizer is off and the post-mortem is offline analysis: both are reference."""
    h = html()
    for pid in ("groqPanel", "postmortemPanel"):
        assert f'<details class="panel fold" id="{pid}">' in h, f"{pid} should be a folded <details>"
        # and it must still carry its live ids, or the renderers go dark
    for el in ("groqBadge", "groqLine", "groqBudgetBar", "groqBudgetNum",
               "groqSummary", "groqList", "pmBadge", "pmLine", "pmBar"):
        assert f'id="{el}"' in h, f"missing #{el}"


def test_position_card_renders_the_exit_ladder() -> None:
    """A scalper needs the rung, not just the P&L."""
    j = js()
    assert "function ladderHtml(" in j
    # it must be wired into the card template, or it is dead code
    card = j.split('class="pos-card"', 1)[1].split("</article>", 1)[0]
    assert "ladderHtml(p)" in card


def test_ladder_thresholds_come_from_the_desk() -> None:
    """The rungs must be the ladder in force, never a hard-coded guess."""
    j = js()
    assert "EXIT_LADDER = s.exits.ladder" in j
    # and it must be cached before the cards render, not after
    body = j.split("function render(s) {", 1)[1]
    assert body.index("EXIT_LADDER = s.exits.ladder") < body.index("const pos = b.positions")


def css() -> str:
    return (WEB / "styles.css").read_text(encoding="utf-8")


def test_the_cat_is_never_hidden() -> None:
    """The mascot is the desk's identity, and it lives top-left in the header.

    A phone-layout pass once set `.cat-dock { display: none }` to buy vertical
    room. It bought it by deleting the one piece of character on the screen the
    trader stares at all day. The cat shrinks on a phone; it never leaves.
    """
    c = css()
    assert "display: none" not in c.split(".cat-dock")[1].split("}")[0], (
        "the cat dock must never be display:none"
    )
    # And it must still be the first thing in the brand block.
    h = html()
    assert at(h, 'class="cat-dock"') < at(h, 'class="titles"')


def test_the_cat_has_a_phone_size() -> None:
    """Phones get a smaller cat, not a missing one."""
    c = css()
    assert ".topbar .mascot-wrap { --cat:" in c
