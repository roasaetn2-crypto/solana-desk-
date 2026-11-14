"""Trade signal engine: one auditable BUY / SELL / HOLD / AVOID call per coin.

This is the "call it out" seat. It reads the same recorded fields the rest of the
desk already has (token snapshot, chain facts, links, analyst thesis, meme
scorecard, security chips, market regime) and turns them into a single,
deterministic recommendation:

    action      BUY | SELL | HOLD | AVOID
    direction   bullish | bearish | neutral
    confidence  0-100 conviction
    win_prob    estimated probability the trade reaches TP1 before the stop
    entry / stop / TP1 / TP2 price plan
    signal      a one-line string you can print, alert or show on the dashboard
    prompt      a short natural-language recommendation

Why a separate module:

* It is pure. No network, no model, no clock. The same inputs always produce the
  same call, which is what makes it testable and backtestable.
* It never sizes a trade and never places an order. The risk engine still owns
  size; the broker still owns fills. This module only *recommends*.
* It is the entry-quality gate the older backtests asked for: the 2026-11-12
  gate report showed the exit rules alone cap out around a 17% win rate and the
  fix is to stop buying the wrong coins. A bullish/bearish read with a
  confidence threshold is exactly that filter.

Every component is scored -1.0 .. +1.0 (bearish .. bullish), weighted, summed,
then mapped to 0-100 confidence. A hard security block is an instant AVOID.
"""
from __future__ import annotations

import math

from config import EXITS, SIGNAL
from desk import exits as exit_rules
from desk import meme_tactics
from desk import social

# Human labels for the security flags the desk already produces.
_BLOCK_FLAGS = {
    "non_transferable", "default_frozen", "transfer_hook", "balance_mutable",
    "closable", "malicious_creator", "mint_authority", "freeze_authority",
    "gmgn_honeypot", "gmgn_rug_ratio", "no_sells", "top_wallet", "top_10",
}


def _num(v):
    if v is None or v == "" or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return f


def _clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _avg(*parts):
    vals = [p for p in parts if p is not None]
    return sum(vals) / len(vals) if vals else 0.0


def _flag_list(security) -> tuple[list[str], list[str]]:
    """Return (block_flags, halve_flags) from the several shapes security takes."""
    if not security:
        return [], []
    if isinstance(security, dict):
        block = list(security.get("block") or [])
        halve = list(security.get("halve") or [])
        for c in security.get("chips") or []:
            t = c.get("t") if isinstance(c, dict) else None
            sev = c.get("sev") if isinstance(c, dict) else None
            if not t:
                continue
            if sev == "block" and t not in block:
                block.append(t)
            elif sev == "halve" and t not in halve:
                halve.append(t)
        flags = list(security.get("flags") or [])
        for f in flags:
            if f in _BLOCK_FLAGS and f not in block:
                block.append(f)
        return block, halve
    if isinstance(security, (list, tuple, set)):
        return [str(f) for f in security if str(f) in _BLOCK_FLAGS], []
    return [], []


# ---------------------------------------------------------------------------
# components: each returns (score -1..1, reasons, warnings)
# ---------------------------------------------------------------------------
def _momentum(t: dict) -> tuple[float, list, list]:
    reasons, warnings = [], []
    ch5, ch1 = _num(t.get("change_m5")), _num(t.get("change_h1"))
    v_m5, v_h1, v_h6 = _num(t.get("volume_m5")), _num(t.get("volume_h1")), _num(t.get("volume_h6"))

    short = _clamp((ch5 or 0) / 8.0) if ch5 is not None else None
    hour = _clamp((ch1 or 0) / 70.0) if ch1 is not None else None

    # "volume still arriving": last 5 minutes annualised to an hour vs the hour.
    flow = None
    if v_m5 is not None and v_h1 and v_h1 > 0:
        ratio = (v_m5 * 12.0) / v_h1
        flow = _clamp((ratio - 0.55) / 0.45)
        if ratio >= 0.9:
            reasons.append(f"volume still arriving ({ratio:.2f}x hourly pace)")
        elif ratio < 0.35:
            warnings.append(f"volume fading ({ratio:.2f}x hourly pace)")
    elif v_h1 is not None and v_h6 and v_h6 > 0:
        ratio = v_h1 / (v_h6 / 6.0)
        flow = _clamp((ratio - 0.55) / 0.45)
        if ratio < 0.4:
            warnings.append(f"recent volume fading ({ratio:.2f}x 6h pace)")

    score = _avg(short, hour, flow)
    if ch1 is not None and ch1 > 250:
        warnings.append(f"vertical chart (+{ch1:.0f}% in 1h) — chase risk")
        score -= 0.25
    if ch5 is not None and ch5 < -4:
        warnings.append(f"price rolling over ({ch5:.1f}% in 5m)")
    if ch1 is not None and ch1 > 25:
        reasons.append(f"positive momentum (+{ch1:.0f}% 1h)")
    return _clamp(score), reasons, warnings


def _flow(t: dict) -> tuple[float, list, list]:
    reasons, warnings = [], []
    buys, sells = _num(t.get("buys_h1")), _num(t.get("sells_h1"))
    buyers = _num(t.get("buyers_h1"))

    balance = None
    if buys and sells is not None and buys > 0:
        ratio = sells / buys
        if 0.2 <= ratio <= 0.85:
            balance = 0.6
            reasons.append(f"healthy buy/sell balance ({ratio:.2f})")
        elif ratio < 0.10:
            balance = -1.0
            warnings.append("almost no sells — honeypot smell")
        elif ratio < 0.2:
            balance = -0.2
        elif ratio <= 1.2:
            balance = -0.3
            warnings.append(f"sellers starting to dominate ({ratio:.2f})")
        else:
            balance = -0.9
            warnings.append(f"sellers dominate ({ratio:.2f})")

    diversity = None
    if buys and buyers and buyers > 0:
        per = buys / buyers
        if per <= 2.5:
            diversity = 0.7
            reasons.append(f"{int(buyers)} distinct buyers")
        elif per <= 4.0:
            diversity = 0.0
        else:
            diversity = -0.7
            warnings.append(f"{per:.1f} buys per buyer — concentration")

    return _clamp(_avg(balance, diversity)), reasons, warnings


def _liquidity(t: dict) -> tuple[float, list, list]:
    reasons, warnings = [], []
    liq, mcap = _num(t.get("liquidity_usd")), _num(t.get("mcap_usd"))
    vol1 = _num(t.get("volume_h1"))

    depth = None
    if liq is not None:
        if liq >= 40_000:
            depth = 1.0
            reasons.append(f"deep liquidity ${liq:,.0f}")
        elif liq >= 15_000:
            depth = 0.5
            reasons.append(f"ok liquidity ${liq:,.0f}")
        elif liq >= 5_000:
            depth = -0.2
            warnings.append(f"thin liquidity ${liq:,.0f}")
        else:
            depth = -1.0
            warnings.append(f"very thin liquidity ${liq:,.0f}")

    turn = None
    if vol1 is not None and mcap and mcap > 0:
        vr = vol1 / mcap
        if vr >= 0.5:
            turn = 0.9
            reasons.append(f"volume/mcap {vr:.2f}")
        elif vr >= 0.15:
            turn = 0.4
        elif vr < 0.05:
            turn = -0.8
            warnings.append(f"volume/mcap only {vr:.2f}")

    return _clamp(_avg(depth, turn)), reasons, warnings


def _holders(f: dict) -> tuple[float, list, list]:
    reasons, warnings = [], []
    top1, top10 = _num(f.get("top_wallet_pct")), _num(f.get("top_10_pct"))

    one = None
    if top1 is not None:
        if top1 <= 0.04:
            one = 1.0
            reasons.append(f"top wallet {top1:.1%}")
        elif top1 > 0.08:
            one = -1.0
            warnings.append(f"top wallet {top1:.1%}")
        else:
            one = 0.0

    ten = None
    if top10 is not None:
        if top10 <= 0.25:
            ten = 1.0
            reasons.append(f"top10 hold {top10:.1%}")
        elif top10 > 0.35:
            ten = -1.0
            warnings.append(f"top10 hold {top10:.1%}")
        else:
            ten = 0.0

    return _clamp(_avg(one, ten)), reasons, warnings


def _narrative(sc: dict, links: dict) -> tuple[float, list, list]:
    reasons, warnings = [], []
    total = _num((sc or {}).get("total"))
    score = None
    if total is not None:
        score = _clamp((total - 50.0) / 45.0)
        if total >= 65:
            reasons.append(f"meme scorecard {total:.0f}/100")
        elif total < 45:
            warnings.append(f"weak meme scorecard {total:.0f}/100")
    if links and (links.get("x") or links.get("telegram")):
        score = _avg(score, 0.4)
        reasons.append("has socials")
    elif links:
        score = _avg(score, -0.3)
        warnings.append("no X or Telegram link")
    return _clamp(score if score is not None else 0.0), reasons, warnings


def _safety(security, dev: dict, regime) -> tuple[float, list, list]:
    reasons, warnings = [], []
    block, halve = _flag_list(security)
    if block:
        warnings.append("security block: " + ", ".join(sorted(set(block))[:3]))
        return -1.0, reasons, warnings
    score = 0.0
    if halve:
        score -= 0.6
        warnings.append("security caution: " + ", ".join(sorted(set(halve))[:3]))
    dev = dev or {}
    hard_no = list(dev.get("hard_no") or [])
    if hard_no:
        warnings.append("dev check: " + ", ".join(hard_no[:3]))
        score -= 0.8
    dev_score = _num(dev.get("dev_score"))
    if dev_score is not None:
        score += _clamp((dev_score - 50.0) / 50.0) * 0.5
        if dev_score >= 70:
            reasons.append(f"dev looks clean ({dev_score:.0f}/100)")
    tag = str((regime or {}).get("tag") or (regime if isinstance(regime, str) else "")).upper()
    if "RISK_OFF" in tag:
        score -= 0.5
        warnings.append("risk-off market regime")
    return _clamp(score), reasons, warnings


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------
def _take_profit_plan(horizon: str) -> dict:
    pr = exit_rules.profile(horizon)
    return {
        "tp1_at": pr["tp1_at"], "tp1_frac": pr["tp1_frac"],
        "tp2_at": pr["tp2_at"], "tp2_frac": pr["tp2_frac"],
        "trail_pct": pr["trail_pct"], "stop_pct": pr["stop_pct"],
        "mode": EXITS.get("take_profit_mode", "profile"),
    }


def _price_plan(entry: float | None, tp: dict) -> dict:
    if not entry or entry <= 0:
        return {"entry": None, "stop": None, "tp1": None, "tp2": None, "risk_reward": None}
    stop = entry * (1 - tp["stop_pct"])
    tp1 = entry * tp["tp1_at"]
    tp2 = entry * tp["tp2_at"] if tp["tp2_at"] < 1e8 else None
    rr = (tp["tp1_at"] - 1.0) / tp["stop_pct"] if tp["stop_pct"] else None
    return {"entry": entry, "stop": stop, "tp1": tp1, "tp2": tp2,
            "risk_reward": round(rr, 2) if rr is not None else None}


def _win_probability(composite: float) -> float:
    slope = float(SIGNAL.get("win_prob_slope", 0.55))
    p = 1.0 / (1.0 + math.exp(-slope * composite * 2.0))
    return round(_clamp(p, SIGNAL.get("win_prob_floor", 0.30),
                        SIGNAL.get("win_prob_cap", 0.85)), 3)


def evaluate(token: dict, facts: dict | None = None, links: dict | None = None,
             thesis=None, scorecard: dict | None = None, security=None,
             regime=None, dev: dict | None = None, holding: bool = False) -> dict:
    """Produce one trade signal. Pure: no I/O, no clock, no model."""
    t = token or {}
    f = facts or {}
    links = links or {}
    sc = scorecard or {}
    dev = dev or {}
    if thesis is not None and not isinstance(thesis, dict):
        try:
            thesis = thesis.model_dump()
        except Exception:
            thesis = {}

    w = SIGNAL.get("weights", {})
    m, mr, mw = _momentum(t)
    fl, fr, fw = _flow(t)
    lq, lr, lw = _liquidity(t)
    hd, hr, hw = _holders(f)
    nr, nr_r, nr_w = _narrative(sc, links)
    sf, sr, sw = _safety(security, dev, regime)

    composite = (w.get("momentum", 0.24) * m + w.get("flow", 0.22) * fl
                 + w.get("liquidity", 0.16) * lq + w.get("holders", 0.14) * hd
                 + w.get("narrative", 0.12) * nr + w.get("safety", 0.12) * sf)
    composite = _clamp(composite)

    win_prob = _win_probability(composite)
    if composite > 0.08:
        direction = "bullish"
    elif composite < -0.08:
        direction = "bearish"
    else:
        direction = "neutral"

    # Confidence is conviction in the *direction*: a strongly bearish read is a
    # high-confidence SELL/AVOID, not a low-confidence one. Neutral sits at 50.
    confidence = int(round(_clamp(50.0 + 50.0 * abs(composite), 0.0, 100.0)))

    block, _halve = _flag_list(security)
    hard_block = bool(block) or bool(dev.get("hard_no"))

    # analyst thesis nudges the read but cannot override a hard block. A thesis
    # that agrees with the signal raises conviction; one that disagrees lowers it.
    th_score = _num((thesis or {}).get("score"))
    th_verdict = (thesis or {}).get("verdict")
    if th_score is not None and not hard_block:
        nudge = (th_score - 5) * 2
        if direction == "bearish":
            nudge = -nudge
        confidence = int(round(_clamp(confidence + nudge, 0, 100)))
    if th_verdict == "skip" and not hard_block:
        confidence = max(0, confidence + (6 if direction == "bearish" else -6))
    elif th_verdict == "valid_setup" and not hard_block:
        confidence = min(100, confidence + (-6 if direction == "bearish" else 4))

    min_conf = int(SIGNAL.get("min_confidence", 60))
    min_p = float(SIGNAL.get("min_win_probability", 0.52))
    sell_conf = int(SIGNAL.get("sell_confidence", 68))

    if hard_block:
        action = "AVOID"
    elif direction == "bearish" and confidence >= sell_conf:
        action = "SELL" if holding else "AVOID"
    elif (direction == "bullish" and confidence >= min_conf and win_prob >= min_p):
        action = "BUY"
    else:
        action = "HOLD"

    horizon = exit_rules.horizon_for(_num(t.get("age_minutes")) or 0)
    tp = _take_profit_plan(horizon)
    entry_px = _num(t.get("price_usd"))
    plan = _price_plan(entry_px, tp)

    reasons = list(dict.fromkeys(mr + fr + lr + hr + nr_r + sr))
    warnings = list(dict.fromkeys(mw + fw + lw + hw + nr_w + sw))
    if hard_block:
        reasons = reasons[:2]

    sig = {
        "action": action,
        "direction": direction,
        "confidence": confidence,
        "win_probability": win_prob,
        "composite": round(composite, 4),
        "score": int(round(_clamp((composite + 1.0) * 50.0, 0, 100))),
        "horizon": horizon,
        "take_profit": tp,
        "plan": plan,
        "components": {
            "momentum": round(m, 3), "flow": round(fl, 3),
            "liquidity": round(lq, 3), "holders": round(hd, 3),
            "narrative": round(nr, 3), "safety": round(sf, 3),
        },
        "reasons": reasons[:6],
        "warnings": warnings[:6],
        "holding": bool(holding),
        "hard_block": hard_block,
    }
    # memecoin tactics from the public guide: entry gates can only remove a
    # BUY, and the pullback package swaps in its measured exit ladder.
    sig = meme_tactics.apply(sig, t, f)
    # social attention, ticker-keyed: recorded on every signal so it can be
    # measured against realised P&L. Never changes the action unless the
    # measurement has cleared the bar and SOCIAL["gate"] is on.
    sig = social.apply(sig, t, f)
    sig["signal"] = _signal_text(t.get("symbol") or t.get("mint", "?"), sig)
    sig["prompt"] = _prompt_text(t.get("symbol") or t.get("mint", "?"), sig)
    return sig


def _fmt_price(p) -> str:
    if p is None:
        return "n/a"
    if p >= 1:
        return f"${p:,.4f}"
    if p >= 0.01:
        return f"${p:.5f}"
    return f"${p:.8f}"


def _signal_text(symbol: str, sig: dict) -> str:
    icon = {"BUY": "🟢", "SELL": "🔴", "AVOID": "⛔", "HOLD": "🟡"}.get(sig["action"], "•")
    head = (f"{icon} {sig['action']} ${symbol} · {sig['direction']} "
            f"{sig['confidence']}% · win {int(sig['win_probability'] * 100)}%")
    if sig["action"] in ("BUY", "SELL"):
        p = sig["plan"]
        tp = sig["take_profit"]
        parts = [head, f"entry {_fmt_price(p['entry'])}",
                 f"TP1 {tp['tp1_at']:g}x ({_fmt_price(p['tp1'])}, sell {int(tp['tp1_frac'] * 100)}%)"]
        if p.get("tp2"):
            parts.append(f"TP2 {tp['tp2_at']:g}x ({_fmt_price(p['tp2'])})")
        parts.append(f"stop {_fmt_price(p['stop'])} (-{int(tp['stop_pct'] * 100)}%)")
        return " · ".join(parts)
    why = (sig["warnings"] or sig["reasons"] or ["no edge"])[0]
    return f"{head} · {why}"


def _prompt_text(symbol: str, sig: dict) -> str:
    p = sig["plan"]
    tp = sig["take_profit"]
    if sig["action"] == "BUY":
        return (f"BUY ${symbol}: {sig['direction']} with {sig['confidence']}% confidence "
                f"(win prob {int(sig['win_probability'] * 100)}%). Enter near {_fmt_price(p['entry'])}, "
                f"take profit {tp['tp1_at']:g}x at {_fmt_price(p['tp1'])} selling "
                f"{int(tp['tp1_frac'] * 100)}%, then {tp['tp2_at']:g}x at {_fmt_price(p['tp2'])}; "
                f"stop at {_fmt_price(p['stop'])}. R:R {p['risk_reward']}.")
    if sig["action"] == "SELL":
        return (f"SELL ${symbol}: the read turned {sig['direction']} at {sig['confidence']}% confidence. "
                f"Bank the take-profit / cut the position; do not add.")
    if sig["action"] == "AVOID":
        why = "; ".join(sig["warnings"][:2]) or "bearish read"
        return f"AVOID ${symbol}: {sig['direction']} at {sig['confidence']}% confidence ({why}). No entry."
    return (f"HOLD ${symbol}: {sig['direction']} at {sig['confidence']}% confidence — "
            f"no BUY edge yet (need {int(SIGNAL.get('min_confidence', 60))}%+ bullish). Watch.")


def allows_entry(sig: dict) -> tuple[bool, str]:
    """The gate the agent uses. Returns (ok, reason)."""
    if not SIGNAL.get("enabled", True):
        return True, "signal gate off"
    if sig.get("hard_block"):
        return False, "signal: hard security block"
    if SIGNAL.get("require_bullish", True) and sig.get("direction") == "bearish":
        return False, f"signal bearish {sig.get('confidence')}%"
    if sig.get("action") != "BUY":
        return False, (f"signal {sig.get('action')} {sig.get('confidence')}% "
                       f"(need BUY >= {int(SIGNAL.get('min_confidence', 60))}%)")
    if sig.get("win_probability", 0) < float(SIGNAL.get("min_win_probability", 0.52)):
        return False, f"signal win prob {int(sig.get('win_probability', 0) * 100)}% below floor"
    return True, "signal BUY"


def should_sell(sig: dict) -> tuple[bool, str]:
    """Signal-driven exit for a held position. Returns (sell, reason)."""
    if not SIGNAL.get("sell_on_bearish", True):
        return False, "signal sell off"
    if sig.get("action") == "SELL" and sig.get("confidence", 0) >= int(SIGNAL.get("sell_confidence", 68)):
        return True, f"signal SELL {sig.get('confidence')}% ({sig.get('direction')})"
    return False, "hold"
