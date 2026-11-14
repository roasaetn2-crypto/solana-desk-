"""Seat 5b: LIVE execution. Real money. Off by default, every boot.

This is the only file in the desk that can move real funds. It is deliberately
small, deliberately boring, and deliberately hard to switch on:

    * the key is read from the environment only (SOLANA_PRIVATE_KEY), never a
      file, never the database, never an API response, never a log line
    * the desk boots in paper mode and does not remember live mode across a
      restart, so a crash can never leave you silently trading
    * arming live needs the exact confirmation phrase in config.EXECUTION
    * every buy passes four independent caps before a transaction is built
    * the kill switch is checked before every send

The broker keeps the exact same ledger shape as PaperBroker, so the agent loop,
the exit rules and the dashboard do not change at all. Only the fill is real.

Routing is Jupiter (free lite endpoint, no key). Cash is USDC, so the ledger
stays in dollars and never drifts with the SOL price. A token with no direct
USDC route still fills: Jupiter builds the multi-hop through SOL itself.
"""
from __future__ import annotations

import base64
import json
import os
import time
from datetime import date
from pathlib import Path

import requests

from config import EXECUTION, RISK
from desk import chain
from desk.broker import PaperBroker
from desk.ops import killed

LIVE_LEDGER = Path("live_ledger.json")

# LOCAL CHANGE (see LOCAL_CHANGES.md): live trading is permanently disabled in
# this checkout. No key is read, nothing is signed, nothing is sent on-chain.
LIVE_DISABLED = True
LIVE_DISABLED_MSG = ("LIVE TRADING IS DISABLED in this build (owner rule: the desk never "
                     "holds a wallet key or signs/sends transactions). Paper only.")


class LiveError(Exception):
    """Anything that should stop a live order. Never contains the key."""


# ---------- key handling: env only, never surfaced ----------
def _key_material() -> str | None:
    if LIVE_DISABLED:
        return None  # never even look at the environment for a key
    raw = os.environ.get("SOLANA_PRIVATE_KEY", "").strip()
    return raw or None


def redact(text: str) -> str:
    """Scrub the key out of any string before it can reach a log or an API."""
    raw = _key_material()
    if not raw:
        return text
    out = text.replace(raw, "***REDACTED***")
    if raw.startswith("["):
        try:
            out = out.replace(str(json.loads(raw)), "***REDACTED***")
        except Exception:
            pass
    return out


def _key_bytes() -> bytes | None:
    raw = _key_material()
    if not raw:
        return None
    if raw.startswith("["):
        try:
            return bytes(json.loads(raw))
        except Exception as e:
            raise LiveError(redact(f"SOLANA_PRIVATE_KEY is not a valid byte array: {e}"))
    try:
        import base58  # lazy: paper mode must not need it
    except ImportError:
        raise LiveError("base58 is not installed; run: pip install base58")
    try:
        return base58.b58decode(raw)
    except Exception as e:
        raise LiveError(redact(f"SOLANA_PRIVATE_KEY is not valid base58: {e}"))


def _keypair():
    if LIVE_DISABLED:
        raise LiveError(LIVE_DISABLED_MSG)
    try:
        from solders.keypair import Keypair  # lazy: paper mode must not need it
    except ImportError:
        raise LiveError("solders is not installed; run: pip install solders")
    b = _key_bytes()
    if not b:
        raise LiveError("no SOLANA_PRIVATE_KEY in the environment")
    try:
        return Keypair.from_bytes(b)
    except Exception as e:
        raise LiveError(redact(f"could not load the key: {e}"))


def ready() -> tuple[bool, str]:
    """Can this machine actually sign? Returns (ok, human reason)."""
    if LIVE_DISABLED:
        return False, LIVE_DISABLED_MSG
    if not _key_material():
        return False, "no SOLANA_PRIVATE_KEY set — live mode cannot arm"
    try:
        import solders  # noqa: F401
        import base58  # noqa: F401
    except ImportError as e:
        return False, f"missing dependency ({e.name}); run: pip install solders base58"
    try:
        _keypair()
    except LiveError as e:
        return False, str(e)
    return True, "key loaded, signing available"


class LiveBroker(PaperBroker):
    """Same book as PaperBroker, real fills through Jupiter."""

    mode = "live"

    def __init__(self, path: Path | str = LIVE_LEDGER, session: requests.Session | None = None):
        fresh = not Path(path).exists()
        super().__init__(path)
        if fresh:
            # live P&L starts at zero: it measures trading, not a seeded bankroll
            self.s = {"cash": 0.0, "positions": {}, "day": str(date.today()),
                      "day_start": 0.0, "closed": []}
            self._save()
        self.http = session or requests.Session()
        self._dec: dict[str, int] = {
            EXECUTION["usdc_mint"]: 6,
            EXECUTION["sol_mint"]: 9,
        }

    def reset(self):
        """Live P&L resets to zero, never to a seeded paper bankroll."""
        self.s = {"cash": 0.0, "positions": {}, "day": str(date.today()),
                  "day_start": 0.0, "closed": []}
        self._save()

    # ---------- identity ----------
    def pubkey(self) -> str | None:
        try:
            return str(_keypair().pubkey())
        except LiveError:
            return None

    def ready(self) -> tuple[bool, str]:
        return ready()

    # ---------- plumbing ----------
    def _rpc(self, method: str, params: list) -> dict:
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        r = self.http.post(chain.rpc_url(), json=body, timeout=20)
        r.raise_for_status()
        out = r.json()
        if out.get("error"):
            err = out["error"]
            detail = err.get("message") or err.get("data") or err
            logs = ((err.get("data") or {}).get("logs") or []) if isinstance(err.get("data"), dict) else []
            if logs:
                detail = f"{detail} — {' | '.join(str(x) for x in logs[-3:])[:240]}"
            raise LiveError(redact(f"RPC {method} failed: {detail}"))
        return out.get("result") or {}

    def _decimals(self, mint: str) -> int:
        if mint in self._dec:
            return self._dec[mint]
        res = self._rpc("getTokenSupply", [mint])
        dec = int(((res.get("value") or {}).get("decimals", 0)))
        if dec <= 0:
            raise LiveError(f"could not read decimals for {mint}")
        self._dec[mint] = dec
        return dec

    def _quote(self, in_mint: str, out_mint: str, amount_raw: int,
               slippage_bps: int | None = None) -> dict:
        if amount_raw <= 0:
            raise LiveError("quote amount is zero")
        bps = min(slippage_bps or EXECUTION["live_max_slippage_bps"],
                  EXECUTION["live_max_slippage_bps"])
        r = self.http.get(
            f"{EXECUTION['jupiter_base']}/quote",
            params={"inputMint": in_mint, "outputMint": out_mint,
                    "amount": str(amount_raw), "slippageBps": str(bps),
                    "restrictIntermediateTokens": "true"},
            timeout=20,
        )
        if r.status_code != 200:
            raise LiveError(redact(f"no route ({r.status_code}): {r.text[:160]}"))
        q = r.json()
        if not q.get("outAmount"):
            raise LiveError("route returned no output amount")
        return q

    def _sign_and_send(self, quote: dict) -> str:
        if LIVE_DISABLED:
            raise LiveError(LIVE_DISABLED_MSG)
        kp = _keypair()
        r = self.http.post(
            f"{EXECUTION['jupiter_base']}/swap",
            json={"quoteResponse": quote, "userPublicKey": str(kp.pubkey()),
                  "wrapAndUnwrapSol": True, "dynamicComputeUnitLimit": True,
                  "prioritizationFeeLamports": "auto"},
            timeout=30,
        )
        if r.status_code != 200:
            raise LiveError(redact(f"swap build failed ({r.status_code}): {r.text[:160]}"))
        tx_b64 = r.json().get("swapTransaction")
        if not tx_b64:
            raise LiveError("swap build returned no transaction")

        from solders.message import to_bytes_versioned
        from solders.transaction import VersionedTransaction
        try:
            tx = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
            sig = kp.sign_message(to_bytes_versioned(tx.message))
            tx = VersionedTransaction.populate(tx.message, [sig])
        except Exception as e:
            raise LiveError(redact(f"could not sign the transaction: {e}"))
        signed = base64.b64encode(bytes(tx)).decode()

        res = self._rpc("sendTransaction",
                        [signed, {"encoding": "base64", "skipPreflight": False,
                                  "preflightCommitment": "confirmed", "maxRetries": 3}])
        sig = res if isinstance(res, str) else (res or {}).get("signature")
        if not sig:
            raise LiveError("node accepted no signature")
        self._confirm(sig)
        return sig

    def _confirm(self, sig: str):
        deadline = time.time() + EXECUTION["confirm_timeout_seconds"]
        while time.time() < deadline:
            try:
                res = self._rpc("getSignatureStatuses", [[sig], {"searchTransactionHistory": True}])
                v = (res.get("value") or [None])[0]
            except Exception:
                v = None
            if v:
                if v.get("err"):
                    raise LiveError(redact(f"transaction {sig[:12]}… failed on chain: {v['err']}"))
                if v.get("confirmationStatus") in ("confirmed", "finalized"):
                    return
            time.sleep(2)
        raise LiveError(f"transaction {sig[:12]}… did not confirm in "
                        f"{EXECUTION['confirm_timeout_seconds']}s")

    # ---------- caps ----------
    def position_notional(self) -> float:
        return round(sum(p.get("cost", 0.0) for p in self.s["positions"].values()), 2)

    def loss_limit_usd(self) -> float:
        return round(EXECUTION["live_daily_loss_pct"] * EXECUTION["live_max_notional_usd"], 2)

    def _guard_buy(self, mint: str, ticket_usd: float):
        ok, why = ready()
        if mint.upper().startswith("DEMO"):
            raise LiveError("refusing a synthetic demo token — live only trades real mints")
        if not ok:
            raise LiveError(why)
        if killed():
            raise LiveError("kill switch is tripped — no live buys")
        if ticket_usd <= 0:
            raise LiveError("ticket is zero")
        if ticket_usd > EXECUTION["live_max_trade_usd"]:
            raise LiveError(f"ticket ${ticket_usd:.2f} above the live per-trade cap "
                            f"${EXECUTION['live_max_trade_usd']:.2f}")
        if self.position_notional() + ticket_usd > EXECUTION["live_max_notional_usd"]:
            raise LiveError(f"would put ${self.position_notional() + ticket_usd:.2f} at risk, "
                            f"above the live notional cap "
                            f"${EXECUTION['live_max_notional_usd']:.2f}")
        if self.day_pnl() <= -self.loss_limit_usd():
            raise LiveError(f"live daily loss limit hit (${self.day_pnl():.2f} of "
                            f"-${self.loss_limit_usd():.2f}) — no new buys today")
        if self.open_count() >= RISK["max_open_positions"]:
            raise LiveError("max open positions")

    # ---------- orders ----------
    def buy(self, mint: str, symbol: str, ticket_usd: float, price: float,
            horizon: str | None = None, groq_mult: float | None = None) -> dict:
        if LIVE_DISABLED:
            raise LiveError(LIVE_DISABLED_MSG)
        self._guard_buy(mint, ticket_usd)
        dec = self._decimals(mint)
        raw_in = int(round(ticket_usd * 10 ** 6))          # USDC has 6 decimals
        q = self._quote(EXECUTION["usdc_mint"], mint, raw_in)
        out_raw = int(q["outAmount"])
        if out_raw <= 0:
            raise LiveError("route would return zero tokens")
        qty = out_raw / 10 ** dec
        sig = self._sign_and_send(q)
        fill = ticket_usd / qty if qty else price

        self.s["cash"] -= ticket_usd
        self.s["positions"][mint] = {
            "symbol": symbol, "qty": qty, "entry": fill, "last": fill,
            "opened": time.time(), "cost": ticket_usd, "half_taken": False,
            "tx": sig, "live": True,
        }
        if groq_mult is not None:
            # Same measurement hook as the paper broker: tag the position with the
            # multiplier that sized it so the close can journal realised P&L against
            # it. groq_sizer.buckets() joins these rows to test for ranking skill.
            self.s["positions"][mint]["groq_mult"] = float(groq_mult)
        self._save()
        return {"side": "buy", "mint": mint, "symbol": symbol, "qty": qty, "fill": fill,
                "fee": 0.0, "ticket_usd": ticket_usd, "tx": sig, "live": True,
                "price_impact": q.get("priceImpactPct")}

    def sell(self, mint: str, fraction: float, price: float, reason: str) -> dict:
        if LIVE_DISABLED:
            raise LiveError(LIVE_DISABLED_MSG)
        p = self.s["positions"][mint]
        dec = self._decimals(mint)
        qty = p["qty"] * fraction
        raw = int(qty * 10 ** dec)
        if raw <= 0:
            raise LiveError("nothing to sell")
        q = self._quote(mint, EXECUTION["usdc_mint"], raw)
        proceeds = int(q["outAmount"]) / 10 ** 6
        sig = self._sign_and_send(q)
        fill = proceeds / qty if qty else price

        self.s["cash"] += proceeds
        p["qty"] -= qty
        p["last"] = fill
        p["proceeds"] = p.get("proceeds", 0.0) + proceeds
        out = {"side": "sell", "mint": mint, "symbol": p["symbol"], "qty": qty,
               "fill": fill, "fee": 0.0, "reason": reason, "tx": sig, "live": True}
        if fraction >= 1 or p["qty"] <= 1e-12:
            pnl = round(p["proceeds"] - p["cost"], 2)
            out["pnl_usd"] = pnl
            self.s["closed"].append({"mint": mint, "symbol": p["symbol"], "pnl_usd": pnl,
                                     "reason": reason, "closed": time.time(), "tx": sig})
            if p.get("groq_mult") is not None:
                # Realised live P&L tagged with the multiplier that sized the ticket.
                try:
                    from desk.ops import journal
                    journal("groq_sizer_pnl", mint=mint, symbol=p.get("symbol"),
                            mult=float(p["groq_mult"]), pnl_usd=pnl, reason=reason,
                            ticket_usd=p.get("cost"), entry=p.get("entry"),
                            opened=p.get("opened"), closed=time.time(), live=True)
                except Exception:
                    pass
            del self.s["positions"][mint]
        self._save()
        return out

    # ---------- dashboard view ----------
    def snapshot(self) -> dict:
        snap = super().snapshot()
        snap.update({
            "mode": "live",
            "label": "live P&L",
            "wallet": self.pubkey(),
            "notional": self.position_notional(),
            "caps": {
                "max_trade": EXECUTION["live_max_trade_usd"],
                "max_notional": EXECUTION["live_max_notional_usd"],
                "daily_loss_usd": self.loss_limit_usd(),
            },
        })
        return snap
