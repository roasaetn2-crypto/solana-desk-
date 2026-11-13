"""FOMO seat: offline, no key, no network.

Everything here runs with FOMO_API_KEY unset and DESK_OFFLINE=1. The point is
to prove the seat is inert by default and only ever nudges or vetoes when the
evidence is real -- never the other way round.
"""
import os

os.environ.setdefault("DESK_OFFLINE", "1")
os.environ.pop("FOMO_API_KEY", None)

import pytest

from desk import devcheck, fomo, meme


# ------------------------------------------------------------------ fixtures

def _row(text, pnl=0.0, trade=0.0, likes=0, is_dev=False):
    return {"text": text, "realizedPnlUsd": pnl, "tradeUsd": trade,
            "likes": likes, "isDev": is_dev, "handle": "x"}


def _ev(**kw):
    base = {"available": True, "warnings": [], "severe": False,
            "buy_disabled": False, "sell_disabled": False,
            "trading_disabled": False, "dev_holdings": [], "dev_pnl_usd": 0.0,
            "thesis": None, "why": []}
    base.update(kw)
    return base


def _rep(**kw):
    rep = {"available": True, "source": "rugcheck", "creator_hold_pct": 0.0,
           "creator_prev_rugs": 0, "sniper_pct": 0.0, "holder_count": 500,
           "lp_locked": True, "mint_renounced": True, "freeze_renounced": True,
           "flags": [], "dev_score": 70, "reasons": [], "hard_no": [], "legit": True}
    rep.update(kw)
    return rep


@pytest.fixture(autouse=True)
def _no_key(monkeypatch):
    monkeypatch.delenv("FOMO_API_KEY", raising=False)
    fomo.reset_cache()
    yield
    fomo.reset_cache()


# ------------------------------------------------------- inert without a key

def test_disabled_without_a_key():
    assert fomo.enabled() is False
    assert fomo.api_key() == ""


def test_evidence_is_unknown_not_clean_without_a_key():
    ev = fomo.token_evidence("So11111111111111111111111111111111111111112")
    assert ev["available"] is False
    assert "FOMO_API_KEY not set" in ev["why"]
    # unknown must never look like a clean pass
    assert fomo.kill_reason(ev) is None
    assert fomo.thesis_modifier(ev) == (0, [])


def test_no_credits_are_spent_without_a_key():
    before = fomo.usage()["spent"]
    fomo.token_evidence("So11111111111111111111111111111111111111112", "thesis")
    assert fomo.usage()["spent"] == before


# ------------------------------------------------------------- sentiment math

def test_pleading_bagholder_is_not_bullish():
    s = fomo.thesis_sentiment("go to up please bro this is everything")
    assert s["score"] < 0.2, s


def test_irony_is_discounted_hard():
    plain = fomo.thesis_sentiment("send it higher")
    ironic = fomo.thesis_sentiment("sure send it higher lol")
    assert plain["score"] > 0.5
    assert ironic["score"] < plain["score"]


def test_low_information_text_is_damped():
    s = fomo.thesis_sentiment("free")
    assert abs(s["score"]) < 0.35


def test_bullish_phrases_are_recognised():
    for t in ("accumulated some BONK now", "topped up, bag is heavy",
              "still holding", "bonk has held up remarkably well"):
        assert fomo.thesis_sentiment(t)["score"] > 0, t


# ------------------------------------------------------------ thesis scoring

def test_thin_crowd_shrinks_toward_neutral():
    rows = [_row("send it higher", pnl=0, trade=0, likes=0) for _ in range(4)]
    sc = fomo.score_theses(rows)
    assert sc["confidence"] == "low"
    assert 50 < sc["score"] < 75          # shrunk, not raw


def test_credible_crowd_moves_further():
    rows = [_row("send it higher", pnl=900, trade=2000, likes=8) for _ in range(10)]
    sc = fomo.score_theses(rows)
    assert sc["confidence"] == "high"
    assert sc["score"] > 70


def test_empty_theses_are_unknown_not_neutral():
    # "nobody wrote anything" is unknown, not a neutral 50 -- and the desk must
    # not read an unknown as permission to nudge the score either way.
    sc = fomo.score_theses([])
    assert sc["score"] is None
    assert sc["confidence"] == "none"
    assert fomo.thesis_modifier({"available": True, "thesis": sc}) == (0, [])


def test_dev_thesis_is_downweighted():
    rows = [_row("send it", pnl=5000, trade=9000, likes=40, is_dev=True)]
    assert fomo._credibility(rows[0]) < fomo._credibility(_row("send it", pnl=5000,
                                                              trade=9000, likes=40))


# --------------------------------------------------------------- hard vetoes

def test_sell_disabled_is_a_kill():
    assert fomo.kill_reason(_ev(sell_disabled=True)) == "fomo_sell_disabled"


def test_trading_disabled_is_a_kill():
    assert fomo.kill_reason(_ev(trading_disabled=True)) == "fomo_trading_disabled"


def test_transfer_fee_is_a_kill():
    ev = _ev(warnings=[{"type": "HIGH_TRANSFER_FEE"}])
    assert fomo.kill_reason(ev) == "fomo_transfer_fee"


def test_severe_alone_is_not_a_kill():
    assert fomo.kill_reason(_ev(severe=True)) is None


def test_unknown_evidence_is_never_a_kill():
    assert fomo.kill_reason(_ev(available=False)) is None
    assert fomo.kill_reason(None) is None


# ------------------------------------------------------- devcheck integration

def test_apply_fomo_without_evidence_is_a_noop():
    rep = _rep()
    out = devcheck.apply_fomo(rep, None)
    assert out["hard_no"] == [] and out["legit"] is True


def test_apply_fomo_with_unavailable_evidence_is_a_noop():
    rep = _rep()
    out = devcheck.apply_fomo(rep, _ev(available=False))
    assert out["hard_no"] == [] and out["legit"] is True


def test_apply_fomo_adds_a_hard_veto():
    rep = _rep()
    out = devcheck.apply_fomo(rep, _ev(sell_disabled=True))
    assert "fomo_sell_disabled" in out["hard_no"]
    assert out["legit"] is False
    assert devcheck.dev_kill(out) == "fomo_sell_disabled"


def test_apply_fomo_dev_dumping_is_a_veto():
    out = devcheck.apply_fomo(_rep(), _ev(dev_pnl_usd=-1200.0))
    assert "fomo_dev_dumping" in out["hard_no"]


def test_apply_fomo_bearish_thesis_is_a_veto():
    ev = _ev(thesis={"available": True, "score": 12, "confidence": "high",
                     "count": 30, "dev_theses": 0})
    out = devcheck.apply_fomo(_rep(), ev)
    assert "fomo_thesis_bearish" in out["hard_no"]


def test_apply_fomo_weak_bearish_thesis_is_not_a_veto():
    ev = _ev(thesis={"available": True, "score": 12, "confidence": "low",
                     "count": 3, "dev_theses": 0})
    out = devcheck.apply_fomo(_rep(), ev)
    assert out["hard_no"] == []


def test_apply_fomo_can_never_clear_an_existing_veto():
    rep = _rep(hard_no=["dev_hold"], legit=False)
    out = devcheck.apply_fomo(rep, _ev(thesis={"available": True, "score": 99,
                                               "confidence": "high", "count": 50}))
    assert "dev_hold" in out["hard_no"]
    assert out["legit"] is False


# -------------------------------------------------------- meme nudge, bounded

def _token():
    return {"mint": "M", "symbol": "T", "name": "T", "age_minutes": 120,
            "price_usd": 1e-6, "liquidity_usd": 60_000, "mcap_usd": 300_000,
            "volume_h1": 90_000, "buys_h1": 300, "sells_h1": 120, "buyers_h1": 200}


def _facts():
    return {"holder_count": 500, "top_wallet_pct": 0.02, "top_10_pct": 0.20}


def _ds():
    return {"has_x": True, "has_telegram": True, "has_website": True,
            "boosts_active": True, "liquidity_usd": 60_000, "mcap_usd": 300_000}


def test_scorecard_without_fomo_is_unchanged():
    a = meme.scorecard(_token(), _facts(), _ds(), _rep())
    b = meme.scorecard(_token(), _facts(), _ds(), _rep(), fomo_ev=None)
    assert a["total"] == b["total"]
    assert a["fomo_nudge"] == 0


def test_bullish_thesis_nudge_is_capped_at_eight():
    ev = _ev(thesis={"available": True, "score": 100, "confidence": "high",
                     "count": 80, "dev_theses": 0})
    base = meme.scorecard(_token(), _facts(), _ds(), _rep())
    hot = meme.scorecard(_token(), _facts(), _ds(), _rep(), fomo_ev=ev)
    assert hot["fomo_nudge"] <= 8
    assert hot["parts"]["community"] - base["parts"]["community"] <= 8


def test_bearish_thesis_nudge_is_capped_at_minus_eight():
    ev = _ev(thesis={"available": True, "score": 0, "confidence": "high",
                     "count": 80, "dev_theses": 0})
    base = meme.scorecard(_token(), _facts(), _ds(), _rep())
    cold = meme.scorecard(_token(), _facts(), _ds(), _rep(), fomo_ev=ev)
    assert cold["fomo_nudge"] >= -8
    assert base["parts"]["community"] - cold["parts"]["community"] <= 8


def test_low_confidence_thesis_does_not_move_the_score():
    ev = _ev(thesis={"available": True, "score": 95, "confidence": "low",
                     "count": 2, "dev_theses": 0})
    base = meme.scorecard(_token(), _facts(), _ds(), _rep())
    out = meme.scorecard(_token(), _facts(), _ds(), _rep(), fomo_ev=ev)
    assert out["fomo_nudge"] == 0
    assert out["total"] == base["total"]


def test_weights_still_sum_to_one():
    assert abs(sum(meme.MEME["weights"].values()) - 1.0) < 1e-9
    sc = meme.scorecard(_token(), _facts(), _ds(), _rep())
    assert set(sc["parts"]) == set(meme.MEME["weights"])


def test_broken_fomo_payload_cannot_break_the_scorecard():
    sc = meme.scorecard(_token(), _facts(), _ds(), _rep(), fomo_ev={"thesis": "nonsense"})
    assert sc["fomo_nudge"] == 0
    assert 0 <= sc["total"] <= 100


# ------------------------------------------------------------ credit metering

def test_budget_is_checked_before_the_call(monkeypatch):
    monkeypatch.setenv("FOMO_API_KEY", "test-key")
    monkeypatch.setattr(fomo.netutil, "offline", lambda: False)
    assert fomo.enabled() is True
    monkeypatch.setattr(fomo, "affordable", lambda cost: False)
    called = []

    def transport(method, url, **kw):
        called.append(url)
        raise AssertionError("must not call upstream when unaffordable")

    ev = fomo.token_evidence("So11111111111111111111111111111111111111112",
                             "signals", transport=transport)
    assert called == []
    assert ev["available"] is False


def test_a_failing_upstream_is_unknown_not_clean(monkeypatch):
    monkeypatch.setenv("FOMO_API_KEY", "test-key")
    monkeypatch.setattr(fomo.netutil, "offline", lambda: False)
    monkeypatch.setattr(fomo, "RETRIES", 1)   # no backoff sleeps in the suite

    def transport(method, url, **kw):
        raise fomo.FomoError("upstream said 503")

    ev = fomo.token_evidence("So11111111111111111111111111111111111111112",
                             "signals", transport=transport)
    assert ev["available"] is False
    assert fomo.kill_reason(ev) is None
    assert fomo.thesis_modifier(ev) == (0, [])
