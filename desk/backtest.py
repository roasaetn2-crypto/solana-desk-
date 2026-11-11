"""Read-only replay of recorded desk history against alternative gate settings.

This module never changes live settings, never places an order, and never
invents a price. Where a rejected coin has no later recorded prices, the
outcome is marked unknown.
"""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import config
from config import ENTRY, PAPER, RISK, SCAN
from desk import exits
from desk.broker import PaperBroker
from desk.ops import data_file
from desk.risk import Account, check_token_trade
from desk.analyst import TokenThesis

# Kills the backtest will not reopen, even under looser score/chase/gap gates.
HARD_REASONS = {
    "mint_authority", "freeze_authority", "top_wallet", "top_10", "no_sells",
    "meme_score", "no_community",
}
HARD_STAGES = {"dev", "security", "live"}
HARD_PREFIXES = ("sec_", "dev_")

GAP_RE = re.compile(
    r"([\d.eE+-]+)\s+vs\s+\w+\s+([\d.eE+-]+)|"
    r"GeckoTerminal\s+([\d.eE+-]+)\s+vs\s+DexScreener\s+([\d.eE+-]+)",
    re.I,
)


def journal_path(path: str | Path | None = None) -> Path:
    return Path(path) if path else data_file("journal.jsonl")


def parse_ts(value) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    try:
        if "T" in text:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
            try:
                return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc).timestamp()
            except ValueError:
                continue
    except ValueError:
        return None
    return None


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _num(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(n):
        return None
    return n


def rules_features(token: dict | None, extras: dict | None = None) -> dict:
    """Old-desk rules features, computed only from recorded fields."""
    t = dict(token or {})
    if extras:
        t.update({k: v for k, v in extras.items() if v is not None})
    vol1, buys = _num(t.get("volume_h1")), _num(t.get("buys_h1"))
    p1, p5 = _num(t.get("change_h1")), _num(t.get("change_m5"))
    b5, s5 = _num(t.get("buys_m5") or t.get("buys5")), _num(t.get("sells_m5") or t.get("sells5"))
    alive = clamp(vol1 / 25_000) if vol1 is not None else _num(t.get("alive_6h"))
    organic = clamp(buys / 150) if buys is not None else _num(t.get("organic"))
    if _num(t.get("flush")) is not None:
        flush = _num(t.get("flush"))
    elif p5 is not None and p5 <= -5 and b5 is not None and s5 is not None and b5 >= s5:
        flush = 1.0
    elif p5 is not None and p5 < 0:
        flush = 0.5
    elif p5 is not None:
        flush = 0.0
    else:
        flush = None
    chase = clamp(p1 / 80) if p1 is not None else _num(t.get("chase"))
    parts = [alive, organic, flush, (None if chase is None else 1 - chase)]
    score = round(100 * sum(parts) / 4) if all(p is not None for p in parts) else _num(t.get("rules_score") or t.get("score"))
    return {
        "alive_6h": alive, "organic": organic, "flush": flush, "chase": chase,
        "rules_score": score, "change_h1": p1,
    }


def current_settings() -> dict:
    """The gates the live paper desk uses today. No 0-100 score or alive_6h gate."""
    return {
        "id": "current",
        "label": "current",
        "current": True,
        "min_score": 0,
        "max_chase": 1.0,
        "min_alive_6h": 0.0,
        "max_price_gap_pct": float(ENTRY["max_price_gap_pct"]),
        "max_h1_change_pct": float(SCAN["max_h1_change_pct"]),
        "min_thesis_score": float(config.MIN_SCORE),
        "require_valid_setup": True,
    }


def default_grid() -> list[dict]:
    """Current row, then isolated sweeps. Alternatives never auto-apply."""
    cur = current_settings()
    rows = [cur]
    for score in (50, 55, 60, 65, 70):
        rows.append({
            "id": f"score_{score}",
            "label": f"score {score}",
            "current": False,
            "min_score": score,
            "max_chase": 0.30,
            "min_alive_6h": 0.70,
            "max_price_gap_pct": float(ENTRY["max_price_gap_pct"]),
            "max_h1_change_pct": None,
            "min_thesis_score": None,
            "require_valid_setup": False,
        })
    for chase in (0.20, 0.30, 0.50):
        rows.append({
            "id": f"chase_{chase:.2f}",
            "label": f"chase {chase:.2f}",
            "current": False,
            "min_score": 50,
            "max_chase": chase,
            "min_alive_6h": 0.70,
            "max_price_gap_pct": float(ENTRY["max_price_gap_pct"]),
            "max_h1_change_pct": None,
            "min_thesis_score": None,
            "require_valid_setup": False,
        })
    for alive in (0.50, 0.70, 0.90):
        rows.append({
            "id": f"alive_{alive:.2f}",
            "label": f"alive {alive:.2f}",
            "current": False,
            "min_score": 50,
            "max_chase": 0.30,
            "min_alive_6h": alive,
            "max_price_gap_pct": float(ENTRY["max_price_gap_pct"]),
            "max_h1_change_pct": None,
            "min_thesis_score": None,
            "require_valid_setup": False,
        })
    for gap in (10.0, 25.0, 40.0):
        rows.append({
            "id": f"gap_{int(gap)}",
            "label": f"gap {int(gap)}%",
            "current": False,
            "min_score": 50,
            "max_chase": 0.30,
            "min_alive_6h": 0.70,
            "max_price_gap_pct": gap,
            "max_h1_change_pct": None,
            "min_thesis_score": None,
            "require_valid_setup": False,
        })
    return rows


def _is_hard(reason: str | None, stage: str | None) -> bool:
    r, s = (reason or ""), (stage or "")
    if s in HARD_STAGES or r in HARD_REASONS:
        return True
    return any(r.startswith(p) for p in HARD_PREFIXES)


def _score_of(feat: dict) -> float | None:
    if feat.get("rules_score") is not None:
        return feat["rules_score"]
    return feat.get("meme_score")


def decide(cand: dict, settings: dict) -> tuple[str, str]:
    """Return ('pass'|'reject'|'unknown', reason)."""
    if cand.get("hard"):
        return "reject", f"hard kill ({cand.get('reason') or cand.get('stage') or 'scam/dev'})"
    feat = cand["features"]
    if settings.get("require_valid_setup"):
        th = cand.get("thesis") or {}
        if th.get("verdict") != "valid_setup" and cand.get("approved") is not True:
            return "reject", f"analyst verdict: {th.get('verdict') or 'none'}"
    min_th = settings.get("min_thesis_score")
    if min_th is not None:
        ts = (cand.get("thesis") or {}).get("score")
        if ts is None and settings.get("require_valid_setup") and cand.get("approved") is not True:
            if cand.get("thesis"):
                return "reject", "no thesis score"
        if ts is not None and ts < min_th:
            return "reject", f"thesis {ts} < {min_th}"

    h1_cap = settings.get("max_h1_change_pct")
    if h1_cap is not None and feat.get("change_h1") is not None and feat["change_h1"] > h1_cap:
        return "reject", "chase"

    min_alive = settings.get("min_alive_6h") or 0
    if min_alive > 0:
        if feat.get("alive_6h") is None:
            return "unknown", "no alive_6h on record"
        if feat["alive_6h"] < min_alive:
            return "reject", f"alive_6h {feat['alive_6h']:.2f} < {min_alive}"

    max_chase = settings.get("max_chase")
    if max_chase is not None and max_chase < 1:
        if feat.get("chase") is None:
            return "unknown", "no chase on record"
        if feat["chase"] > max_chase:
            return "reject", f"chase {feat['chase']:.2f} > {max_chase}"

    min_score = settings.get("min_score") or 0
    if min_score > 0:
        sc = _score_of(feat)
        if sc is None:
            return "unknown", "no score on record"
        if sc < min_score:
            return "reject", f"score {sc} < {min_score}"

    gap_cap = settings.get("max_price_gap_pct")
    if gap_cap is not None:
        gap = feat.get("gap_pct")
        if gap is not None and gap > gap_cap:
            return "reject", f"price_gap {gap:.1f}% > {gap_cap}"
        if gap is None and cand.get("reason") == "price_gap":
            # We know the live desk rejected it for a gap, but not the size.
            # Tightening or holding the same cap keeps the reject; loosening is unknown.
            if gap_cap >= float(ENTRY["max_price_gap_pct"]):
                return "unknown", "price_gap recorded without a size"
            return "reject", "price_gap (size unknown, cap tighter than live)"
        if cand.get("reason") == "no_confirm_quote" and ENTRY.get("require_confirm_quote"):
            return "reject", "no_confirm_quote"

    return "pass", "approved"


def _snap_token(obj: dict | None) -> dict:
    if not obj:
        return {}
    keys = ("mint", "symbol", "price_usd", "mcap_usd", "liquidity_usd",
            "volume_m5", "volume_h1", "volume_h6", "volume_h24",
            "buys_h1", "sells_h1", "buyers_h1", "buys_m5", "sells_m5",
            "change_m5", "change_h1", "age_minutes", "source")
    return {k: obj[k] for k in keys if obj.get(k) is not None}


def _add_price(prices: dict, mint: str, ts: float | None, price: float | None,
               quote: dict | None = None, raw: bool = True) -> None:
    if not mint or ts is None or not price or price <= 0:
        return
    rec = {"ts": ts, "price": float(price), "raw": raw}
    if quote:
        for k in ("volume_h6", "volume_h24", "liquidity_usd"):
            if quote.get(k) is not None:
                rec[k] = quote[k]
    prices.setdefault(mint, []).append(rec)


def _parse_gap_reason(reason: str | None) -> tuple[float | None, float | None, float | None]:
    if not reason:
        return None, None, None
    m = GAP_RE.search(reason)
    if not m:
        return None, None, None
    a, b = m.group(1) or m.group(3), m.group(2) or m.group(4)
    p1, p2 = _num(a), _num(b)
    if not p1 or not p2:
        return p1, p2, None
    mid = (p1 + p2) / 2
    gap = abs(p1 / p2 - 1) * 100 if p2 else None
    # also as percent of the other quote
    gap = max(abs(p1 / p2 - 1), abs(p2 / p1 - 1)) * 100
    return p1, p2, gap


def _new_cand(mint: str, ts: float, **kw) -> dict:
    return {
        "mint": mint, "ts": ts, "symbol": kw.get("symbol") or mint[:6],
        "token": kw.get("token") or {}, "thesis": kw.get("thesis") or {},
        "features": kw.get("features") or {},
        "approved": kw.get("approved"), "reason": kw.get("reason"),
        "stage": kw.get("stage"), "hard": kw.get("hard", False),
        "ticket_usd": kw.get("ticket_usd"), "source": kw.get("source"),
    }


def load_journal(path: str | Path | None = None) -> dict:
    p = journal_path(path)
    events: list[dict] = []
    if p.exists():
        with p.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return _history_from_events(events, source=str(p), label="desk journal")


def _history_from_events(events: list[dict], source: str, label: str) -> dict:
    candidates: dict[tuple, dict] = {}
    prices: dict[str, list] = {}
    kinds = {}
    t_min = t_max = None

    def key_for(mint, ts):
        return (mint, round(ts or 0, 1))

    def upsert(c):
        mint, ts = c["mint"], c["ts"]
        if not mint or ts is None or str(mint).startswith("DEMO"):
            return
        # merge into a nearby candidate of the same mint (confirm follows decision)
        for dt in (0, 1, 2, 5, 10, 30):
            k = (mint, round(ts - dt, 1)) if dt else key_for(mint, ts)
            if k in candidates:
                old = candidates[k]
                for field in ("token", "thesis", "features"):
                    merged = dict(old.get(field) or {})
                    merged.update({kk: vv for kk, vv in (c.get(field) or {}).items() if vv is not None})
                    old[field] = merged
                for field in ("reason", "stage", "symbol", "ticket_usd", "source"):
                    if c.get(field) and not old.get(field):
                        old[field] = c[field]
                if c.get("approved") is not None:
                    old["approved"] = c["approved"]
                old["hard"] = old.get("hard") or c.get("hard")
                return old
        k = key_for(mint, ts)
        candidates[k] = c
        return c

    for ev in events:
        kind = ev.get("kind") or ""
        kinds[kind] = kinds.get(kind, 0) + 1
        ts = parse_ts(ev.get("time"))
        if ts is not None:
            t_min = ts if t_min is None else min(t_min, ts)
            t_max = ts if t_max is None else max(t_max, ts)
        mint = ev.get("mint") or (ev.get("token") or {}).get("mint")
        if mint and str(mint).startswith("DEMO"):
            continue

        if kind in ("decision", "reject"):
            token = _snap_token(ev.get("token") if isinstance(ev.get("token"), dict) else ev)
            if ev.get("price_usd") and "price_usd" not in token:
                token["price_usd"] = ev["price_usd"]
            extras = {}
            sc = ev.get("scorecard") if isinstance(ev.get("scorecard"), dict) else {}
            thesis = ev.get("thesis") if isinstance(ev.get("thesis"), dict) else {}
            feat = rules_features(token, {
                "alive_6h": ev.get("alive_6h"), "chase": ev.get("chase"),
                "organic": ev.get("organic"), "flush": ev.get("flush"),
                "rules_score": ev.get("rules_score"),
            })
            if sc.get("total") is not None:
                feat["meme_score"] = sc["total"]
            gap = _num(ev.get("gap_pct"))
            sp, cp = _num(ev.get("scan_price")), _num(ev.get("confirm_price"))
            if gap is None and sp and cp:
                gap = max(abs(sp / cp - 1), abs(cp / sp - 1)) * 100
            if ev.get("reason") == "price_gap" and gap is None:
                _, _, gap = _parse_gap_reason(str(ev.get("reason") or "") + " " + str(ev.get("detail") or ev.get("note") or ""))
            feat["gap_pct"] = gap
            feat["scan_price"] = sp
            feat["confirm_price"] = cp
            stage = ev.get("stage")
            reason = ev.get("reason")
            c = _new_cand(
                mint, ts or 0, symbol=ev.get("symbol") or token.get("symbol"),
                token=token, thesis=thesis, features=feat,
                approved=ev.get("approved") if kind == "decision" else False,
                reason=reason, stage=stage, ticket_usd=ev.get("ticket_usd"),
                source=ev.get("source") or token.get("source"),
                hard=_is_hard(reason, stage),
            )
            upsert(c)
            px = cp or _num(token.get("price_usd")) or sp
            _add_price(prices, mint, ts, px, token)

        elif kind in ("entry", "exit", "quote"):
            raw_px = _num(ev.get("price_usd") or ev.get("quote") or ev.get("price"))
            fill = _num(ev.get("fill"))
            if raw_px:
                _add_price(prices, mint, ts, raw_px, ev, raw=True)
            elif fill and kind == "entry":
                _add_price(prices, mint, ts, fill / (1 + PAPER["slippage_pct"]), ev, raw=False)
            elif fill and kind == "exit":
                _add_price(prices, mint, ts, fill / (1 - PAPER["slippage_pct"]), ev, raw=False)
            if kind == "entry":
                # keep a handle so simulation can prefer the recorded fill time
                for c in candidates.values():
                    if c["mint"] == mint and abs((c["ts"] or 0) - (ts or 0)) < 120:
                        c["filled"] = True
                        c["entry_fill"] = fill
                        c["entry_ts"] = ts
                        break

    for mint, pts in prices.items():
        pts.sort(key=lambda r: r["ts"])
        # drop exact duplicate ticks
        out, last = [], None
        for r in pts:
            sig = (round(r["ts"], 3), round(r["price"], 12))
            if sig != last:
                out.append(r)
                last = sig
        prices[mint] = out

    cand_list = sorted(candidates.values(), key=lambda c: (c["ts"], c["mint"]))
    return {
        "source": source,
        "label": label,
        "candidates": cand_list,
        "prices": prices,
        "kinds": kinds,
        "t_start": t_min,
        "t_end": t_max,
        "events": len(events),
    }


def load_old_desk(db_path: str | Path | None = None) -> dict | None:
    """Optional: replay coins the previous desk watched (it never filled a paper trade)."""
    path = Path(db_path or os.environ.get("OLD_DESK_DB") or "")
    if not path or not path.exists():
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    events: list[dict] = []
    prices: dict[str, list] = {}
    try:
        for row in conn.execute("SELECT mint, price_usd, volume_m5, ts FROM prices WHERE price_usd > 0"):
            _add_price(prices, row["mint"], row["ts"], row["price_usd"],
                       {"volume_m5": row["volume_m5"]} if row["volume_m5"] else None)
        for row in conn.execute("SELECT * FROM verdicts"):
            ts = row["created_at"]
            feat_src = {
                "alive_6h": row["alive_6h"], "organic": row["organic"],
                "flush": row["flush"], "chase": row["chase"],
                "rules_score": row["score"], "price_usd": row["price"],
            }
            events.append({
                "time": ts, "kind": "decision", "mint": row["mint"],
                "approved": bool(row["passed"]), "reason": row["reason"] or row["failed_rule"],
                "stage": "analyst", "rules_score": row["score"],
                "alive_6h": row["alive_6h"], "organic": row["organic"],
                "flush": row["flush"], "chase": row["chase"],
                "price_usd": row["price"], "token": feat_src,
                "thesis": {"verdict": "valid_setup" if row["passed"] else "skip",
                           "score": None},
            })
        for row in conn.execute("SELECT * FROM tickets"):
            reason = row["reason"] or ""
            p1, p2, gap = _parse_gap_reason(reason)
            stage = "confirm" if "confirm" in reason.lower() or "price disagrees" in reason.lower() else "analyst"
            if "strategist" in reason.lower():
                stage = "analyst"
            ev = {
                "time": row["created_at"], "kind": "reject", "mint": row["mint"],
                "symbol": row["asset"], "reason": ("price_gap" if gap is not None else reason[:80]),
                "stage": stage, "price_usd": row["entry"],
                "scan_price": p1, "confirm_price": p2, "gap_pct": gap,
                "token": {"mint": row["mint"], "symbol": row["asset"], "price_usd": row["entry"]},
            }
            events.append(ev)
    finally:
        conn.close()
    hist = _history_from_events(events, source=str(path), label="old desk watchlist")
    # merge the richer price table (not just event prices)
    for mint, pts in prices.items():
        hist["prices"].setdefault(mint, []).extend(pts)
        hist["prices"][mint].sort(key=lambda r: r["ts"])
    hist["notes"] = (
        "Old desk never filled a paper trade. Entries are hypothetical at the "
        "recorded ticket or verdict price, exits use only recorded price ticks."
    )
    return hist


def _later_prices(prices: list[dict], ts: float) -> list[dict]:
    return [p for p in prices if p["ts"] > ts + 1e-6]


def _quote_from(tick: dict) -> dict:
    return {
        "price_usd": tick["price"],
        "volume_h6": tick.get("volume_h6") or 0,
        "volume_h24": tick.get("volume_h24") or 0,
        "liquidity_usd": tick.get("liquidity_usd") or 0,
    }


def _dummy_thesis(cand: dict) -> TokenThesis:
    th = cand.get("thesis") or {}
    return TokenThesis(
        verdict="valid_setup",
        score=int(th["score"]) if th.get("score") is not None else 10,
        red_flags=list(th.get("red_flags") or []),
        green_flags=list(th.get("green_flags") or ["backtest"]),
        shape=th.get("shape") or "crowd",
        invalidation=th.get("invalidation") or "recorded stop",
        sources=list(th.get("sources") or ["backtest"]),
    )


def simulate(history: dict, settings: dict) -> dict:
    """Replay one setting against recorded prices only. Does not touch live state."""
    fd, tmp = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    ledger = Path(tmp)
    try:
        if ledger.exists():
            ledger.unlink()
        broker = PaperBroker(ledger)
        # start from a clean $20 book even if a leftover file appeared
        broker.reset()
        unknown = 0
        unknown_why: dict[str, int] = {}
        considered = 0
        passed = 0
        equity_pts = [(history.get("t_start") or 0, broker.equity())]
        open_end = 0

        by_mint_prices = history.get("prices") or {}
        # event timeline: candidate appearances + every price tick of interest
        steps: list[tuple[float, str, object]] = []
        for c in history.get("candidates") or []:
            steps.append((c["ts"], "cand", c))
        # ticks are processed when we already hold the mint
        for mint, pts in by_mint_prices.items():
            for tick in pts:
                steps.append((tick["ts"], "tick", (mint, tick)))
        steps.sort(key=lambda x: (x[0], 0 if x[1] == "cand" else 1))

        for ts, typ, payload in steps:
            if typ == "tick":
                mint, tick = payload
                if mint not in broker.s["positions"]:
                    continue
                sig = broker.exit_signal(mint, _quote_from(tick), now=ts)
                if sig:
                    frac, why = sig
                    broker.sell(mint, frac, tick["price"], why)
                equity_pts.append((ts, broker.equity({mint: tick["price"] for mint in broker.s["positions"]})))
                continue

            cand = payload
            considered += 1
            verdict, why = decide(cand, settings)
            if verdict == "unknown":
                unknown += 1
                unknown_why[why] = unknown_why.get(why, 0) + 1
                continue
            if verdict != "pass":
                continue
            passed += 1
            mint = cand["mint"]
            if mint in broker.s["positions"]:
                continue
            token = cand.get("token") or {}
            feat = cand.get("features") or {}
            entry_px = feat.get("confirm_price") or _num(token.get("price_usd")) or feat.get("scan_price")
            if not entry_px:
                # first recorded price at or after the decision
                pts = by_mint_prices.get(mint) or []
                later_eq = [p for p in pts if p["ts"] >= cand["ts"] - 1]
                entry_px = later_eq[0]["price"] if later_eq else None
            later = _later_prices(by_mint_prices.get(mint) or [], cand["ts"])
            # If the only prices are at decision time, there is no path to an exit.
            if not entry_px:
                unknown += 1
                unknown_why["no entry price on record"] = unknown_why.get("no entry price on record", 0) + 1
                continue
            if not later:
                unknown += 1
                unknown_why["no later prices"] = unknown_why.get("no later prices", 0) + 1
                continue

            age = _num(token.get("age_minutes")) or 0
            horizon = exits.horizon_for(age)
            stop = exits.profile(horizon)["stop_pct"]
            liq = _num(token.get("liquidity_usd")) or 1e9
            acct = Account(
                equity=broker.equity(),
                day_pnl=broker.day_pnl(),
                open_positions=broker.open_count(),
                realized_equity=broker.realized_equity(),
            )
            # size with a passing thesis; gate decisions already happened above
            ok, ticket, reason = check_token_trade(
                _dummy_thesis(cand), acct, liq,
                now=datetime.fromtimestamp(ts, tz=timezone.utc), stop_pct=stop)
            if not ok:
                if "max open" in reason or "daily loss" in reason or "fee floor" in reason or "below fee" in reason:
                    # capacity / book rules: not a data gap
                    continue
                unknown += 1
                unknown_why[reason] = unknown_why.get(reason, 0) + 1
                continue
            fill = broker.buy(mint, cand.get("symbol") or mint[:6], ticket, entry_px, horizon=horizon)
            broker.s["positions"][mint]["opened"] = cand["ts"]
            # Exits are applied by later 'tick' steps on the same timeline so a
            # price is never consumed twice.
            equity_pts.append((cand["ts"], broker.equity()))

        # leftover opens: mark to last recorded price for drawdown only
        for mint, pos in list(broker.s["positions"].items()):
            pts = by_mint_prices.get(mint) or []
            if pts:
                pos["last"] = pts[-1]["price"]
                open_end += 1
        end_eq = broker.equity()
        start_eq = float(PAPER["starting_equity_usd"])
        # max drawdown from the marked equity path
        peak, max_dd = start_eq, 0.0
        for _, eq in equity_pts + [((history.get("t_end") or 0), end_eq)]:
            peak = max(peak, eq)
            max_dd = max(max_dd, peak - eq)

        closed = list(broker.s["closed"])
        wins = sum(1 for c in closed if c.get("pnl_usd", 0) > 0)
        pnl = round(sum(c.get("pnl_usd", 0) for c in closed), 2)
        n = len(closed)
        return {
            "id": settings.get("id"),
            "label": settings.get("label") or settings.get("id"),
            "current": bool(settings.get("current")),
            "settings": {k: settings[k] for k in (
                "min_score", "max_chase", "min_alive_6h", "max_price_gap_pct",
                "max_h1_change_pct", "min_thesis_score") if k in settings},
            "considered": considered,
            "passed_gates": passed,
            "trades": n,
            "wins": wins,
            "win_rate": round(wins / n, 3) if n else None,
            "pnl_usd": pnl,
            "max_drawdown_usd": round(max_dd, 2),
            "unknown": unknown,
            "unknown_reasons": unknown_why,
            "open_end": open_end,
            "end_equity": round(end_eq, 2),
            "starting_equity": start_eq,
        }
    finally:
        try:
            ledger.unlink(missing_ok=True)
        except OSError:
            pass


def _span_hours(hist: dict) -> float | None:
    if not hist.get("t_start") or not hist.get("t_end"):
        return None
    return max(0.0, (hist["t_end"] - hist["t_start"]) / 3600)


def run(history: dict, grid: list[dict] | None = None) -> dict:
    settings_list = grid or default_grid()
    rows = [simulate(history, s) for s in settings_list]
    hours = _span_hours(history)
    n_cand = len(history.get("candidates") or [])
    n_px = sum(len(v) for v in (history.get("prices") or {}).values())
    n_mint_px = len(history.get("prices") or {})
    thin = n_cand < 20 or (hours is not None and hours < 24) or n_px < 50
    caveats = []
    if thin:
        caveats.append(
            "Thin sample: this is only a short stretch of recorded tickets, "
            "not a reliable forecast. A few nights of data would make it more useful."
        )
    if history.get("notes"):
        caveats.append(history["notes"])
    caveats.append(
        "Rejected coins with no later recorded price are counted as unknown, never guessed."
    )
    caveats.append(
        "Read only: these numbers do not change live settings. Any retune is a separate, logged step."
    )
    return {
        "ok": True,
        "source": history.get("source"),
        "label": history.get("label"),
        "candidates": n_cand,
        "price_points": n_px,
        "priced_mints": n_mint_px,
        "hours": round(hours, 2) if hours is not None else None,
        "t_start": history.get("t_start"),
        "t_end": history.get("t_end"),
        "kinds": history.get("kinds") or {},
        "thin": thin,
        "caveats": caveats,
        "rows": rows,
    }


def report(journal: str | Path | None = None, old_db: str | Path | None = None) -> dict:
    """Build the dashboard payload from the on-disk journal (and optional old desk)."""
    hist = load_journal(journal)
    out = run(hist)
    alt = None
    db = old_db if old_db is not None else os.environ.get("OLD_DESK_DB")
    if db:
        loaded = load_old_desk(db)
        if loaded and loaded.get("candidates"):
            alt = run(loaded)
    out["old_desk"] = alt
    return out


def main(argv: list[str] | None = None) -> int:
    import sys
    args = list(sys.argv[1:] if argv is None else argv)
    journal = None
    old = os.environ.get("OLD_DESK_DB")
    i = 0
    while i < len(args):
        if args[i] in ("--journal", "-j") and i + 1 < len(args):
            journal = args[i + 1]; i += 2
        elif args[i] in ("--old-desk",) and i + 1 < len(args):
            old = args[i + 1]; i += 2
        else:
            i += 1
    data = report(journal, old)
    # compact table for the terminal
    print(f"{data['label']}: {data['candidates']} candidates, "
          f"{data['price_points']} prices, {data.get('hours') or 0}h")
    print(f"{'setting':<16} {'trades':>6} {'win%':>7} {'pnl':>9} {'maxDD':>8} {'unk':>5} {'now':>4}")
    for r in data["rows"]:
        wr = "" if r["win_rate"] is None else f"{100*r['win_rate']:.0f}%"
        mark = "<" if r["current"] else ""
        print(f"{r['label']:<16} {r['trades']:>6} {wr:>7} {r['pnl_usd']:>9.2f} "
              f"{r['max_drawdown_usd']:>8.2f} {r['unknown']:>5} {mark:>4}")
    for c in data["caveats"]:
        print("-", c)
    if data.get("old_desk"):
        print("\n--- old desk watchlist (never filled) ---")
        od = data["old_desk"]
        print(f"{od['label']}: {od['candidates']} candidates, {od['price_points']} prices")
        for r in od["rows"]:
            wr = "" if r["win_rate"] is None else f"{100*r['win_rate']:.0f}%"
            mark = "<" if r["current"] else ""
            print(f"{r['label']:<16} {r['trades']:>6} {wr:>7} {r['pnl_usd']:>9.2f} "
                  f"{r['max_drawdown_usd']:>8.2f} {r['unknown']:>5} {mark:>4}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
