"""G87E — a faithful Python port of the "G87E parametrized" Pine v6 strategy,
plus the fixed variant, and a bar-by-bar backtester that can run either one.

Why this module exists
----------------------
The Pine script is 32 lines and it *looks* fine on TradingView. It is not. The
entry condition is written with `or` instead of `and`, so it fires on almost
every bar, and the long branch is tested first, so the strategy is a permanent
long-only bias machine that happens to have a short branch it never reaches.
On a meme coin that is a one-way ticket to zero.

This module keeps the original logic intact as ``baseline`` so the damage can be
measured, and adds ``fixed`` with the corrections applied. Both run through the
same execution model, so the only difference between the two equity curves is
the strategy logic itself.

Execution model (identical for both variants)
---------------------------------------------
* Signal is evaluated on the close of bar i, entry fills at that close
  (matches Pine's ``process_orders_on_close=true``).
* Stops are checked intrabar against high/low, worst case first: if a bar's
  range touches both the stop and a trail level, the stop wins. This is
  deliberately pessimistic — Pine's trail on close-only data is optimistic.
* Fees are charged on both sides; slippage is charged on both sides. Meme coins
  do not fill at the mid.
* Position size is risk-based: risk% of equity / (ATR * stopMult), capped at a
  notional ceiling. Same formula as the Pine ``crudo`` / ``tope`` pair.

Nothing here talks to the network, places an order, or touches live settings.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict


# --------------------------------------------------------------------------- #
# Indicators
# --------------------------------------------------------------------------- #
def atr(candles: list[dict], length: int = 14) -> list[float | None]:
    """Wilder ATR, same as Pine's ta.atr. None until the first value exists."""
    out: list[float | None] = [None] * len(candles)
    if len(candles) < 2:
        return out
    trs: list[float] = []
    for i, c in enumerate(candles):
        if i == 0:
            trs.append(float(c["high"]) - float(c["low"]))
            continue
        prev_close = float(candles[i - 1]["close"])
        tr = max(
            float(c["high"]) - float(c["low"]),
            abs(float(c["high"]) - prev_close),
            abs(float(c["low"]) - prev_close),
        )
        trs.append(tr)
    # Pine's ta.atr uses RMA (Wilder smoothing).
    alpha = 1.0 / length
    rma: float | None = None
    for i, tr in enumerate(trs):
        if i < length - 1:
            continue
        if rma is None:
            rma = sum(trs[i - length + 1:i + 1]) / length
        else:
            rma = rma + alpha * (tr - rma)
        out[i] = rma
    return out


def _qty_step(price: float) -> float:
    """Smallest sensible quantity increment for a given price.

    The Pine script hard-rounds to 3 decimals. For SOL at $200 that is fine.
    For a meme coin at $0.000002 it rounds the position to 0.000 and the
    strategy silently never trades. This scales the step to the price instead.
    """
    if price <= 0:
        return 1e-3
    # keep ~6 significant digits of the token, never coarser than 1e-3
    step = 10 ** math.floor(math.log10(price) - 6)
    return max(min(step, 1e-3), 1e-12)


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
@dataclass
class Config:
    # --- same knobs as the Pine inputs ---
    atr_len: int = 14
    risk_pct: float = 1.0
    stop_mult: float = 1.5
    trail_mult: float = 0.2
    # Pine uses one number for both "when the trail arms" and "how far behind it
    # sits". Splitting them is the single most valuable change: arming at 0.2xATR
    # is inside the noise, so the trail converts winners into scratches.
    trail_arm_mult: float | None = None      # None -> use trail_mult
    trail_offset_mult: float | None = None   # None -> use trail_mult
    long_only: bool = False                  # memes: shorts are a donation
    # Risk sizing on a token with a 20% ATR and a 2.5xATR stop deploys ~2% of the
    # account. The Pine's 90%-of-equity "tope" never binds, so the strategy is a
    # dust position. This floors the notional so the trade is worth taking.
    min_notional_pct: float = 0.0
    max_bars: int = 5
    initial_capital: float = 50.0
    fee_pct: float = 0.05          # Pine commission_value, per side
    # --- corrections that do not exist in the Pine at all ---
    slippage_pct: float = 0.30     # memes do not fill at mid, per side
    cooldown_bars: int = 3         # no instant re-entry after an exit
    notional_cap: float = 0.90     # fraction of equity, the Pine "tope"
    atr_floor_pct: float = 0.05    # ATR as % of price, floor against flat bars
    require_trend: bool = True     # EMA filter on the fixed variant
    ema_len: int = 50
    min_bars_between_entries: int = 0
    compounding: bool = True       # size off current equity, not the start
    # --- what to run ---
    variant: str = "fixed"         # "baseline" | "fixed"


# --------------------------------------------------------------------------- #
# Strategy
# --------------------------------------------------------------------------- #
def _ema(values: list[float], length: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    k = 2.0 / (length + 1.0)
    ema: float | None = None
    for i, v in enumerate(values):
        if i < length - 1:
            continue
        if ema is None:
            ema = sum(values[i - length + 1:i + 1]) / length
        else:
            ema = v * k + ema * (1 - k)
        out[i] = ema
    return out


def signals(candles: list[dict], cfg: Config) -> list[dict]:
    """One dict per bar: {"long": bool, "short": bool, "atr": float, "qty": float}.

    baseline = the Pine exactly as written, including the `or` bug.
    fixed    = the Pine with the bug corrected and the guards added.
    """
    n = len(candles)
    closes = [float(c["close"]) for c in candles]
    atrs = atr(candles, cfg.atr_len)
    emas = _ema(closes, cfg.ema_len) if cfg.variant == "fixed" else [None] * n
    out: list[dict] = []

    for i in range(n):
        row = {"long": False, "short": False, "atr": 0.0, "qty": 0.0, "why": ""}
        a = atrs[i]
        if a is None or a <= 0 or i < 30:
            out.append(row)
            continue
        price = closes[i]
        # ATR floor: a flat bar gives atr=0, and qty = risk/0 = inf.
        a_eff = max(a, price * cfg.atr_floor_pct / 100.0)
        row["atr"] = a_eff

        up3 = price > closes[i - 3]
        up12 = price > closes[i - 12]
        up30 = price > closes[i - 30]

        if cfg.variant == "baseline":
            # ---- the Pine, verbatim: `or`, long checked first, no filter ----
            long_ok = up3 or up12 or up30
            short_ok = (not up3) or (not up12) or (not up30)
            if long_ok:
                row["long"], row["why"] = True, "or-long"
            elif short_ok:
                row["short"], row["why"] = True, "or-short"
        else:
            # ---- fixed: AND, mutually exclusive, trend-filtered ----
            long_ok = up3 and up12 and up30
            short_ok = (not up3) and (not up12) and (not up30)
            if cfg.require_trend and emas[i] is not None:
                long_ok = long_ok and price > emas[i]
                short_ok = short_ok and price < emas[i]
            if long_ok:
                row["long"], row["why"] = True, "and-long"
            elif short_ok:
                row["short"], row["why"] = True, "and-short"

        # sizing: identical formula in both variants
        equity = cfg.initial_capital
        risk_usd = equity * (cfg.risk_pct / 100.0)
        raw = risk_usd / (a_eff * cfg.stop_mult)
        cap = equity * cfg.notional_cap / price
        qty = min(raw, cap)
        step = _qty_step(price)
        row["qty"] = math.floor(qty / step) * step
        out.append(row)
    return out


# --------------------------------------------------------------------------- #
# Backtester
# --------------------------------------------------------------------------- #
@dataclass
class Trade:
    side: str
    entry_i: int
    entry_ts: float
    entry: float
    qty: float
    exit_i: int = -1
    exit_ts: float = 0.0
    exit: float = 0.0
    reason: str = ""
    pnl: float = 0.0
    pnl_pct: float = 0.0
    bars: int = 0
    mae_pct: float = 0.0
    mfe_pct: float = 0.0


@dataclass
class Result:
    variant: str
    trades: list[Trade] = field(default_factory=list)
    equity: list[float] = field(default_factory=list)
    equity_ts: list[float] = field(default_factory=list)
    start_equity: float = 0.0
    end_equity: float = 0.0
    fees_paid: float = 0.0
    max_dd_pct: float = 0.0
    exposure_pct: float = 0.0

    def stats(self) -> dict:
        wins = [t for t in self.trades if t.pnl > 0]
        losses = [t for t in self.trades if t.pnl <= 0]
        gross_win = sum(t.pnl for t in wins)
        gross_loss = -sum(t.pnl for t in losses)
        n = len(self.trades)
        longs = [t for t in self.trades if t.side == "long"]
        shorts = [t for t in self.trades if t.side == "short"]
        avg_bars = sum(t.bars for t in self.trades) / n if n else 0.0
        return {
            "variant": self.variant,
            "trades": n,
            "longs": len(longs),
            "shorts": len(shorts),
            "win_rate_pct": round(100.0 * len(wins) / n, 2) if n else 0.0,
            "start_equity": round(self.start_equity, 2),
            "end_equity": round(self.end_equity, 2),
            "net_pnl": round(self.end_equity - self.start_equity, 2),
            "return_pct": round(100.0 * (self.end_equity / self.start_equity - 1.0), 2)
            if self.start_equity else 0.0,
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
            "avg_trade_pct": round(sum(t.pnl_pct for t in self.trades) / n, 3) if n else 0.0,
            "avg_bars_held": round(avg_bars, 2),
            "max_dd_pct": round(self.max_dd_pct, 2),
            "fees_paid": round(self.fees_paid, 2),
            "exposure_pct": round(self.exposure_pct, 2),
            "best_trade_pct": round(max((t.pnl_pct for t in self.trades), default=0.0), 2),
            "worst_trade_pct": round(min((t.pnl_pct for t in self.trades), default=0.0), 2),
        }


def backtest(candles: list[dict], cfg: Config) -> Result:
    """Bar-by-bar replay. One position at a time, no pyramiding."""
    sig = signals(candles, cfg)
    equity = cfg.initial_capital
    res = Result(variant=cfg.variant, start_equity=equity, end_equity=equity)
    fee = cfg.fee_pct / 100.0
    slip = cfg.slippage_pct / 100.0

    pos: dict | None = None
    last_exit_i = -10_000
    bars_in_market = 0

    for i, c in enumerate(candles):
        price = float(c["close"])
        hi, lo = float(c["high"]), float(c["low"])
        ts = float(c["ts"])

        # ---------------- manage an open position ----------------
        if pos is not None:
            bars_in_market += 1
            side = pos["side"]
            entry = pos["entry"]
            stop = pos["stop"]
            atr_v = pos["atr"]
            arm_dist = atr_v * (cfg.trail_arm_mult if cfg.trail_arm_mult is not None else cfg.trail_mult)
            trail_dist = atr_v * (cfg.trail_offset_mult if cfg.trail_offset_mult is not None else cfg.trail_mult)
            # track excursion
            if side == "long":
                pos["mfe"] = max(pos["mfe"], (hi / entry - 1.0) * 100.0)
                pos["mae"] = min(pos["mae"], (lo / entry - 1.0) * 100.0)
            else:
                pos["mfe"] = max(pos["mfe"], (1.0 - lo / entry) * 100.0)
                pos["mae"] = min(pos["mae"], (1.0 - hi / entry) * 100.0)

            exit_price = None
            reason = ""

            # 1) hard stop first (pessimistic)
            if side == "long" and lo <= stop:
                exit_price, reason = stop, "stop"
            elif side == "short" and hi >= stop:
                exit_price, reason = stop, "stop"
            else:
                # 2) trail: only active once price has run trail_dist in favour
                if side == "long":
                    if hi >= entry + arm_dist:
                        pos["trail_on"] = True
                        pos["peak"] = max(pos["peak"], hi)
                        trail_stop = pos["peak"] - trail_dist
                        pos["stop"] = max(pos["stop"], trail_stop)
                        if lo <= pos["stop"]:
                            exit_price, reason = pos["stop"], "trail"
                else:
                    if lo <= entry - arm_dist:
                        pos["trail_on"] = True
                        pos["trough"] = min(pos["trough"], lo)
                        trail_stop = pos["trough"] + trail_dist
                        pos["stop"] = min(pos["stop"], trail_stop)
                        if hi >= pos["stop"]:
                            exit_price, reason = pos["stop"], "trail"

            # 3) time stop
            if exit_price is None and i - pos["entry_i"] >= cfg.max_bars:
                exit_price, reason = price, "max_bars"

            if exit_price is not None:
                fill = exit_price * (1 - slip) if side == "long" else exit_price * (1 + slip)
                gross = (fill - entry) * pos["qty"] if side == "long" else (entry - fill) * pos["qty"]
                exit_fee = fill * pos["qty"] * fee
                pnl = gross - exit_fee
                equity += pnl
                res.fees_paid += exit_fee
                res.trades.append(Trade(
                    side=side, entry_i=pos["entry_i"], entry_ts=pos["entry_ts"],
                    entry=entry, qty=pos["qty"], exit_i=i, exit_ts=ts, exit=fill,
                    reason=reason, pnl=pnl,
                    pnl_pct=100.0 * pnl / pos["cost"],
                    bars=i - pos["entry_i"],
                    mae_pct=pos["mae"], mfe_pct=pos["mfe"],
                ))
                pos = None
                last_exit_i = i

        # ---------------- look for an entry ----------------
        if pos is None and i < len(candles) - 1:
            row = sig[i]
            want_short = row["short"] and not cfg.long_only
            if (row["long"] or want_short) and row["qty"] > 0:
                if i - last_exit_i < cfg.cooldown_bars:
                    pass
                else:
                    side = "long" if row["long"] else "short"
                    a = row["atr"]
                    # recompute qty off live equity when compounding
                    if cfg.compounding:
                        risk_usd = equity * (cfg.risk_pct / 100.0)
                        raw = risk_usd / (a * cfg.stop_mult)
                        cap = equity * cfg.notional_cap / price
                        q = min(raw, cap)
                        if cfg.min_notional_pct > 0:
                            q = max(q, equity * cfg.min_notional_pct / price)
                            q = min(q, cap)
                        step = _qty_step(price)
                        q = math.floor(q / step) * step
                    else:
                        q = row["qty"]
                    if q <= 0:
                        pass
                    else:
                        fill = price * (1 + slip) if side == "long" else price * (1 - slip)
                        cost = fill * q
                        entry_fee = cost * fee
                        if cost + entry_fee > equity:
                            q = max(0.0, (equity * 0.98) / (fill * (1 + fee)))
                        if q > 0:
                            cost = fill * q
                            entry_fee = cost * fee
                            equity -= entry_fee
                            res.fees_paid += entry_fee
                            stop = fill - a * cfg.stop_mult if side == "long" else fill + a * cfg.stop_mult
                            pos = {
                                "side": side, "entry": fill, "qty": q, "cost": cost + entry_fee,
                                "entry_i": i, "entry_ts": ts, "atr": a, "stop": stop,
                                "peak": fill, "trough": fill, "mfe": 0.0, "mae": 0.0,
                                "trail_on": False,
                            }

        res.equity.append(equity)
        res.equity_ts.append(ts)

    # close any open position at the last close
    if pos is not None and candles:
        c = candles[-1]
        price = float(c["close"])
        side = pos["side"]
        fill = price * (1 - slip) if side == "long" else price * (1 + slip)
        gross = (fill - pos["entry"]) * pos["qty"] if side == "long" else (pos["entry"] - fill) * pos["qty"]
        exit_fee = fill * pos["qty"] * fee
        pnl = gross - exit_fee
        equity += pnl
        res.fees_paid += exit_fee
        res.trades.append(Trade(
            side=side, entry_i=pos["entry_i"], entry_ts=pos["entry_ts"],
            entry=pos["entry"], qty=pos["qty"], exit_i=len(candles) - 1,
            exit_ts=float(c["ts"]), exit=fill, reason="eod", pnl=pnl,
            pnl_pct=100.0 * pnl / pos["cost"], bars=len(candles) - 1 - pos["entry_i"],
            mae_pct=pos["mae"], mfe_pct=pos["mfe"],
        ))
        if res.equity:
            res.equity[-1] = equity

    res.end_equity = equity
    res.exposure_pct = 100.0 * bars_in_market / len(candles) if candles else 0.0
    # max drawdown on the equity curve
    peak, mdd = -1e18, 0.0
    for e in res.equity:
        peak = max(peak, e)
        if peak > 0:
            mdd = max(mdd, 100.0 * (peak - e) / peak)
    res.max_dd_pct = mdd
    return res


def run_both(candles: list[dict], cfg: Config) -> dict:
    """Baseline and fixed on the same candles. Same execution model both sides."""
    base_cfg = Config(**{**asdict(cfg), "variant": "baseline"})
    fix_cfg = Config(**{**asdict(cfg), "variant": "fixed"})
    b = backtest(candles, base_cfg)
    f = backtest(candles, fix_cfg)
    return {"baseline": b.stats(), "fixed": f.stats(),
            "baseline_result": b, "fixed_result": f}
