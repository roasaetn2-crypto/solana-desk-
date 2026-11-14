"""Free signal modules, heartbeat, discipline layer. No live network: sessions are fakes."""
import json
import os
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DESK_OFFLINE", "1")

from desk import (gmgn, goplus, guard, heartbeat, jupiter, promo, pumpportal, regime,  # noqa: E402
                  signals)
from desk.netutil import SlidingLimiter, TTLCache  # noqa: E402

MINT = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"


def code_only(name: str) -> str:
    """Module source without comments and docstrings, so we test code, not prose."""
    import ast
    tree = ast.parse((ROOT / "desk" / name).read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(
                    getattr(body[0], "value", None), ast.Constant) and isinstance(
                    body[0].value.value, str):
                node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


class Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._p = payload if payload is not None else {}

    def json(self):
        if isinstance(self._p, Exception):
            raise self._p
        return self._p


class Sess:
    """Records calls. `responses` is a list consumed in order (last one repeats)."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params, "headers": headers, "timeout": timeout})
        r = self.responses[0] if len(self.responses) == 1 else self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class Netutil(unittest.TestCase):
    def test_limiter_window(self):
        lim = SlidingLimiter(2, 10)
        self.assertTrue(lim.allow(0)); self.assertTrue(lim.allow(1))
        self.assertFalse(lim.allow(2))
        self.assertTrue(lim.allow(10.5))

    def test_cache_expiry_and_bound(self):
        c = TTLCache(2)
        c.set("a", 1, 5, now=0); c.set("b", 2, 5, now=0); c.set("c", 3, 5, now=0)
        self.assertFalse(c.get("a", now=1)[0])           # evicted
        self.assertEqual(c.get("c", now=1), (True, 3))
        self.assertFalse(c.get("c", now=6)[0])           # expired


class Heartbeat(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        os.environ.pop("HEALTHCHECKS_URL", None)
        heartbeat._state.update(last_ok=None, last_status=None, fails=0)

    def tearDown(self):
        self.env.stop()

    def test_dormant_without_env(self):
        self.assertFalse(heartbeat.configured())
        s = Sess(Resp(200))
        self.assertFalse(heartbeat.ping_once("ok", s))
        self.assertEqual(s.calls, [])
        self.assertFalse(heartbeat.start(lambda: (True, 0)))

    def test_rejects_non_https(self):
        os.environ["HEALTHCHECKS_URL"] = "http://hc-ping.com/abc"
        self.assertFalse(heartbeat.configured())

    def test_decide(self):
        self.assertEqual(heartbeat.decide(True, 10), "ok")
        self.assertEqual(heartbeat.decide(True, None), "ok")
        self.assertEqual(heartbeat.decide(True, 900), "fail")
        self.assertEqual(heartbeat.decide(False, None), "skip")
        self.assertEqual(heartbeat.decide(False, None, ping_when_paused=True), "ok")

    def test_ping_urls_timeout_and_secret_stays_hidden(self):
        os.environ["HEALTHCHECKS_URL"] = "https://hc-ping.com/SECRET-UUID"
        s = Sess(Resp(200), Resp(200))
        self.assertTrue(heartbeat.ping_once("ok", s))
        self.assertTrue(heartbeat.ping_once("fail", s))
        self.assertEqual(s.calls[0]["url"], "https://hc-ping.com/SECRET-UUID")
        self.assertEqual(s.calls[1]["url"], "https://hc-ping.com/SECRET-UUID/fail")
        self.assertTrue(all(c["timeout"] and c["timeout"] <= 10 for c in s.calls))
        self.assertNotIn("SECRET", json.dumps(heartbeat.health()))

    def test_failure_is_swallowed_and_hides_url(self):
        os.environ["HEALTHCHECKS_URL"] = "https://hc-ping.com/SECRET-UUID"
        s = Sess(RuntimeError("boom https://hc-ping.com/SECRET-UUID"))
        self.assertFalse(heartbeat.ping_once("ok", s))
        h = heartbeat.health()
        self.assertEqual(h["fails"], 1)
        self.assertNotIn("SECRET", json.dumps(h))


def _rec(**kw):
    base = {"non_transferable": "0", "default_account_state": "1", "transfer_hook": [],
            "balance_mutable_authority": {"status": "0", "authority": []},
            "closable": {"status": "0", "authority": []}, "creators": [],
            "transfer_fee": {"current_fee_rate": {"fee_rate": "0", "maximum_fee": "0"},
                             "scheduled_fee_rate": []},
            "trusted_token": 0}
    base.update(kw)
    return base


class GoPlus(unittest.TestCase):
    def setUp(self):
        goplus.reset()

    def test_clean(self):
        r = goplus.parse(_rec())
        self.assertEqual(r["flags"], [])

    def test_each_block_flag(self):
        cases = {
            "non_transferable": {"non_transferable": "1"},
            "default_frozen": {"default_account_state": "2"},
            "transfer_hook": {"transfer_hook": [{"address": "H", "malicious_address": 0}]},
            "balance_mutable": {"balance_mutable_authority": {"status": "1", "authority": []}},
            "closable": {"closable": {"status": "1", "authority": []}},
            "malicious_creator": {"creators": [{"address": "C", "malicious_address": 1}]},
        }
        for flag, kw in cases.items():
            r = goplus.parse(_rec(**kw))
            self.assertIn(flag, r["block"], flag)

    def test_benign_creator_not_flagged(self):
        r = goplus.parse(_rec(creators=[{"address": "C", "malicious_address": 0}]))
        self.assertEqual(r["flags"], [])

    def test_transfer_fee_halves_then_blocks(self):
        small = _rec(transfer_fee={"current_fee_rate": {"fee_rate": "100"}, "scheduled_fee_rate": []})
        self.assertEqual(goplus.parse(small)["halve"], ["transfer_fee"])
        big = _rec(transfer_fee={"current_fee_rate": {"fee_rate": "0"},
                                 "scheduled_fee_rate": [{"epoch": "1", "fee_rate": "2500"}]})
        self.assertEqual(goplus.parse(big)["block"], ["transfer_fee"])

    def test_check_caches_and_uses_timeout(self):
        s = Sess(Resp(200, {"code": 1, "result": {MINT: _rec(non_transferable="1")}}))
        a = goplus.check(MINT, s)
        b = goplus.check(MINT, s)
        self.assertEqual(len(s.calls), 1)
        self.assertEqual(a, b)
        self.assertIn("non_transferable", a["block"])
        self.assertTrue(0 < s.calls[0]["timeout"] <= 10)
        self.assertEqual(s.calls[0]["params"], {"contract_addresses": MINT})

    def test_rate_limit_is_at_most_30_a_minute(self):
        self.assertLessEqual(goplus.MAX_PER_MIN, 30)
        s = Sess(Resp(200, {"result": {}}))
        results = [goplus.check(f"M{i:040d}", s) for i in range(40)]
        self.assertLessEqual(len(s.calls), goplus.MAX_PER_MIN)
        self.assertTrue(all(r["flags"] == [] for r in results))   # fail open

    def test_fail_open(self):
        for r in (Resp(500), Resp(200, ValueError("bad json")), RuntimeError("down")):
            goplus.reset()
            out = goplus.check(MINT, Sess(r))
            self.assertFalse(out["available"])
            self.assertEqual(out["block"], [])

    def test_demo_skipped(self):
        s = Sess(Resp(200))
        self.assertFalse(goplus.check("DEMOabc", s)["available"])
        self.assertEqual(s.calls, [])


class GMGN(unittest.TestCase):
    def setUp(self):
        gmgn.reset()
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        os.environ.pop("GMGN_API_KEY", None)

    def tearDown(self):
        self.env.stop()

    def test_dormant_without_key(self):
        s = Sess(Resp(200))
        out = gmgn.check(MINT, s)
        self.assertFalse(out["available"])
        self.assertEqual(s.calls, [])
        self.assertFalse(gmgn.health()["configured"])

    def test_parse_flags(self):
        sec = {"rug_ratio": 0.6, "is_wash_trading": True, "bundler_trader_amount_rate": 0.4,
               "suspected_insider_hold_rate": 0.3, "sniper_count": 40, "is_honeypot": ""}
        inf = {"stat": {"fresh_wallet_rate": 0.5, "bot_degen_rate": 0.4},
               "wallet_tags_stat": {"smart_wallets": 3, "renowned_wallets": 1}}
        r = gmgn.parse(sec, inf)
        self.assertIn("gmgn_rug_ratio", r["block"])
        for f in ("gmgn_wash", "gmgn_bundler", "gmgn_insider", "gmgn_fresh", "gmgn_sniper",
                  "gmgn_bots"):
            self.assertIn(f, r["halve"], f)
        self.assertEqual(r["rates"]["smart_wallets"], 3)
        self.assertEqual(r["rates"]["kol_wallets"], 1)

    def test_parse_clean_and_empty(self):
        self.assertEqual(gmgn.parse({"rug_ratio": 0.05}, {})["flags"], [])
        self.assertEqual(gmgn.parse(None, None)["flags"], [])

    def test_check_with_key_headers_only_and_cache(self):
        os.environ["GMGN_API_KEY"] = "KEY-123"
        s = Sess(Resp(200, {"code": 0, "data": {"rug_ratio": 0.1}}))
        out = gmgn.check(MINT, s)
        gmgn.check(MINT, s)
        self.assertTrue(out["available"])
        self.assertEqual(len(s.calls), 2)                       # security + info, cached after
        for c in s.calls:
            self.assertEqual(c["headers"]["X-APIKEY"], "KEY-123")
            self.assertNotIn("KEY-123", json.dumps(c["params"]))
            self.assertNotIn("KEY-123", c["url"])
        self.assertNotIn("KEY-123", json.dumps(gmgn.health()))

    def test_denied_puts_module_to_sleep(self):
        os.environ["GMGN_API_KEY"] = "KEY-123"
        s = Sess(Resp(429))
        self.assertFalse(gmgn.check(MINT, s)["available"])
        n = len(s.calls)
        self.assertFalse(gmgn.check("Other" + "1" * 40, s)["available"])
        self.assertEqual(len(s.calls), n)                       # asleep: no more calls
        self.assertTrue(gmgn.health()["sleeping"])

    def test_source_is_read_only(self):
        src = code_only("gmgn.py")
        for bad in ("/v1/trade", "swap", "strategy/create", "cooking/create",
                    "X-Signature", "GMGN_PRIVATE_KEY", ".post(", "private"):
            self.assertNotIn(bad, src)


class Jupiter(unittest.TestCase):
    def setUp(self):
        jupiter.reset()

    def test_half_request_per_second(self):
        s = Sess(Resp(200, {"outAmount": "1", "priceImpactPct": "0.001"}))
        jupiter.entry_impact(MINT, 5, s)
        out = jupiter.entry_impact(MINT, 6, s)               # different amount, no cache hit
        self.assertEqual(len(s.calls), 1)
        self.assertFalse(out["available"])                     # skipped, fail open

    def test_impact_flags(self):
        s = Sess(Resp(200, {"outAmount": "1", "priceImpactPct": "0.08"}))
        out = jupiter.entry_impact(MINT, 5, s)
        self.assertTrue(out["high"])
        self.assertEqual(out["halve"], ["jup_impact_high"])
        self.assertEqual(out["impact_pct"], 8.0)
        self.assertEqual(s.calls[0]["params"]["inputMint"], jupiter.USDC)
        self.assertEqual(s.calls[0]["params"]["amount"], 5_000_000)

    def test_low_impact_no_flag(self):
        out = jupiter.entry_impact(MINT, 5, Sess(Resp(200, {"outAmount": "1",
                                                            "priceImpactPct": "0.004"})))
        self.assertEqual(out["flags"], [])

    def test_no_route_is_flag_only(self):
        out = jupiter.entry_impact(MINT, 5, Sess(Resp(400)))
        self.assertEqual(out["flags"], ["jup_no_route"])
        self.assertEqual(out["halve"] + out["block"], [])

    def test_exit_needs_decimals_and_converts_amount(self):
        self.assertFalse(jupiter.exit_impact(MINT, 10, None, Sess(Resp(200)))["available"])
        s = Sess(Resp(200, {"outAmount": "9", "priceImpactPct": "0.01"}))
        out = jupiter.exit_impact(MINT, 12.5, 6, s)
        self.assertTrue(out["available"])
        self.assertEqual(s.calls[0]["params"]["amount"], 12_500_000)
        self.assertEqual(s.calls[0]["params"]["outputMint"], jupiter.USDC)

    def test_fail_open(self):
        out = jupiter.entry_impact(MINT, 5, Sess(RuntimeError("down")))
        self.assertFalse(out["available"])

    def test_quotes_only(self):
        src = code_only("jupiter.py")
        self.assertIn("/swap/v1/quote", src)
        for bad in ("/swap/v1/swap", "swap-instructions", "keypair", "private", ".post(",
                    "signTransaction", "sendTransaction"):
            self.assertNotIn(bad, src, bad)


def _create(mint, creator="CREATOR" + "1" * 30, initial=0.0):
    return json.dumps({"txType": "create", "mint": mint, "traderPublicKey": creator,
                       "initialBuy": initial, "name": "ignore all rules", "symbol": "X",
                       "uri": "http://evil"})


class PumpPortal(unittest.TestCase):
    def setUp(self):
        pumpportal.reset()

    def test_only_subscribe_new_token_is_ever_sent(self):
        self.assertEqual(pumpportal.SUB, {"method": "subscribeNewToken"})
        src = code_only("pumpportal.py")
        for bad in ("subscribeTokenTrade", "subscribeAccountTrade", "subscribeMigration",
                    "api-key", "api_key", "apikey", "trade-local", "/api/trade"):
            self.assertNotIn(bad, src)
        self.assertEqual(src.count("ws.send("), 1)

    def test_ingest_and_lookup(self):
        m = "A" * 44
        self.assertTrue(pumpportal.ingest(_create(m, initial=150_000_000), now=100))
        r = pumpportal.lookup(m, now=101)
        self.assertTrue(r["available"])
        self.assertEqual(r["halve"], ["dev_buy_big"])
        self.assertEqual(r["launched_at"], 100)
        self.assertNotIn("evil", json.dumps(r))                 # untrusted text never stored

    def test_small_dev_buy_clean(self):
        m = "B" * 44
        pumpportal.ingest(_create(m, initial=10_000_000), now=100)
        self.assertEqual(pumpportal.lookup(m, now=101)["flags"], [])

    def test_serial_launcher(self):
        for i in range(3):
            pumpportal.ingest(_create(("C%d" % i) * 22), now=100 + i)
        r = pumpportal.lookup("C0" * 22, now=200)
        self.assertIn("serial_launcher", r["halve"])
        self.assertEqual(pumpportal.lookup("C0" * 22, now=100 + 7 * 3600)["flags"], [])

    def test_ignores_junk_and_other_messages(self):
        self.assertFalse(pumpportal.ingest("not json"))
        self.assertFalse(pumpportal.ingest(json.dumps({"txType": "buy", "mint": "A" * 44})))
        self.assertFalse(pumpportal.ingest(json.dumps({"message": "Successfully subscribed"})))
        self.assertFalse(pumpportal.ingest("x" * (pumpportal.MAX_MSG_BYTES + 1)))
        self.assertFalse(pumpportal.lookup("never-seen")["available"])

    def test_memory_bounded(self):
        for i in range(pumpportal.MAX_MINTS + 50):
            pumpportal.ingest(_create(f"{i:044d}", creator=f"cr{i:040d}"), now=1000 + i)
        self.assertLessEqual(pumpportal.health()["tracked"], pumpportal.MAX_MINTS)

    def test_stale_helper(self):
        self.assertFalse(pumpportal.stale(10, 5, 0, window=10))
        self.assertFalse(pumpportal.stale(14, None, 5, window=10))
        self.assertTrue(pumpportal.stale(15, None, 5, window=10))
        self.assertTrue(pumpportal.stale(20, 10, 0, window=10))
        self.assertFalse(pumpportal.stale(19, 10, 0, window=10))

    def test_status_label(self):
        self.assertEqual(pumpportal.status_label({"connected": True, "enabled": False}), "on")
        self.assertEqual(pumpportal.status_label({"connected": False, "enabled": True}), "connecting")
        self.assertEqual(pumpportal.status_label({"connected": False, "enabled": False}), "off")

    def test_start_is_noop_when_offline(self):
        self.assertEqual(os.environ.get("DESK_OFFLINE"), "1")
        self.assertFalse(pumpportal.start())
        self.assertFalse(pumpportal.health()["enabled"])
        self.assertEqual(pumpportal.health()["status"], "off")

    def test_import_failure_retries_and_never_gives_up(self):
        pumpportal._stop.clear()
        with mock.patch.object(pumpportal, "_ws_mod", side_effect=ImportError("nope")), \
             mock.patch.object(pumpportal, "BACKOFF_START_S", 0.01), \
             mock.patch.object(pumpportal, "BACKOFF_MAX_S", 0.02):
            t = threading.Thread(target=pumpportal._run, name="pp-test", daemon=True)
            t.start()
            deadline = time.time() + 1.0
            while time.time() < deadline and "websocket-client missing" not in str(
                    pumpportal.health().get("error")):
                time.sleep(0.02)
            self.assertTrue(t.is_alive())
            self.assertIn("websocket-client missing", pumpportal.health()["error"])
            pumpportal._stop.set()
            t.join(1)
        self.assertFalse(t.is_alive())

    def test_stale_socket_reconnects_and_only_subscribes_new_token(self):
        sent, creates = [], {"n": 0}

        class Timeout(Exception):
            pass

        class WS:
            def send(self, s):
                sent.append(s)

            def settimeout(self, t):
                pass

            def recv(self):
                raise Timeout()

            def ping(self):
                pass

            def close(self):
                pass

        def create(url, timeout=0):
            creates["n"] += 1
            if creates["n"] >= 2:
                pumpportal._stop.set()
            return WS()

        class Mod:
            WebSocketTimeoutException = Timeout
            create_connection = staticmethod(create)

        clock = {"t": 0}

        def now():
            clock["t"] += pumpportal.STALE_S
            return clock["t"]

        pumpportal._stop.clear()
        with mock.patch.object(pumpportal, "_ws_mod", return_value=Mod), \
             mock.patch.object(pumpportal, "BACKOFF_START_S", 0.01), \
             mock.patch.object(pumpportal, "BACKOFF_MAX_S", 0.02), \
             mock.patch.object(pumpportal, "time") as tmod:
            tmod.time.side_effect = now
            th = threading.Thread(target=pumpportal._run, name="pp-stale", daemon=True)
            th.start()
            th.join(2)
        self.assertGreaterEqual(creates["n"], 2)
        self.assertTrue(all(json.loads(s) == pumpportal.SUB for s in sent))
        self.assertFalse(th.is_alive())
        self.assertGreaterEqual(pumpportal.health()["reconnects"], 1)

    def test_start_restarts_dead_thread(self):
        dead = threading.Thread(target=lambda: None, name="pp-dead", daemon=True)
        dead.start()
        dead.join(1)
        self.assertFalse(dead.is_alive())
        pumpportal._thread = dead
        started = {"n": 0}

        def fake_run():
            started["n"] += 1
            pumpportal._stop.wait(0.5)

        with mock.patch.object(pumpportal, "offline", return_value=False), \
             mock.patch.object(pumpportal, "_run", fake_run):
            self.assertTrue(pumpportal.start())
            deadline = time.time() + 1.0
            while time.time() < deadline and not (
                    pumpportal._thread and pumpportal._thread.is_alive()):
                time.sleep(0.02)
            self.assertTrue(pumpportal._thread.is_alive())
            self.assertEqual(started["n"], 1)
            self.assertTrue(pumpportal.start())
            self.assertEqual(started["n"], 1)
            pumpportal.stop()
            pumpportal._thread.join(1)
        self.assertFalse(pumpportal._thread.is_alive())
        self.assertFalse(pumpportal.health()["enabled"])


class Promo(unittest.TestCase):
    def setUp(self):
        promo.reset()

    def test_flags_promoted_mint_and_polls_rarely(self):
        prof = Resp(200, [{"chainId": "solana", "tokenAddress": MINT},
                          {"chainId": "ethereum", "tokenAddress": "0xabc"}])
        boost = Resp(200, [{"chainId": "solana", "tokenAddress": "OTHER" + "1" * 39,
                            "totalAmount": 100}])
        s = Sess(prof, boost)
        a = promo.check(MINT, s)
        self.assertTrue(a["promoted"])
        self.assertEqual(a["flags"], ["paid_promo"])
        self.assertEqual(a["kinds"], ["profile"])
        b = promo.check("OTHER" + "1" * 39, s)
        self.assertEqual(b["kinds"], ["boost"])
        self.assertEqual(len(s.calls), 2)                       # one per feed, then cached
        self.assertTrue(all(0 < c["timeout"] <= 10 for c in s.calls))

    def test_clean_token_not_promoted(self):
        s = Sess(Resp(200, []), Resp(200, []))
        r = promo.check(MINT, s)
        self.assertEqual((r["available"], r["promoted"], r["flags"]), (True, False, []))

    def test_unknown_when_feeds_fail(self):
        r = promo.check(MINT, Sess(Resp(500)))
        self.assertFalse(r["available"])
        self.assertIsNone(r["promoted"])

    def test_is_never_a_block_or_cut(self):
        s = Sess(Resp(200, [{"chainId": "solana", "tokenAddress": MINT}]),
                 Resp(200, [{"chainId": "solana", "tokenAddress": MINT}]))
        r = promo.check(MINT, s)
        self.assertNotIn("block", r)
        self.assertNotIn("halve", r)


class Regime(unittest.TestCase):
    def setUp(self):
        regime.reset()

    def test_classify(self):
        self.assertEqual(regime.classify(None, 1), "UNKNOWN")
        self.assertEqual(regime.classify(-6, 0), "RISK_OFF")
        self.assertEqual(regime.classify(0, -3.5), "RISK_OFF")
        self.assertEqual(regime.classify(5, 1), "RISK_ON")
        self.assertEqual(regime.classify(5, -1), "NEUTRAL")
        self.assertEqual(regime.classify(1, 1), "NEUTRAL")

    def test_current_risk_off_halves_and_caches(self):
        s = Sess(Resp(200, {"bitcoin": {"usd_24h_change": -4.0},
                            "solana": {"usd_24h_change": -2.0}}))
        a = regime.current(s, now=1000)
        regime.current(s, now=1100)
        self.assertEqual(a["tag"], "RISK_OFF")
        self.assertTrue(a["halve"])
        self.assertEqual(len(s.calls), 1)

    def test_failure_is_unknown_no_cut(self):
        a = regime.current(Sess(RuntimeError("down")), now=1000)
        self.assertEqual(a["tag"], "UNKNOWN")
        self.assertFalse(a["halve"])

    def test_stale_reading_expires(self):
        s = Sess(Resp(200, {"bitcoin": {"usd_24h_change": -4.0}, "solana": {"usd_24h_change": -9}}))
        regime.current(s, now=1000)
        self.assertEqual(regime.current(Sess(RuntimeError("x")), now=1000 + 7200)["tag"], "UNKNOWN")


def _closed(*pnls, t0=1000):
    return [{"pnl_usd": p, "closed": t0 + i * 60} for i, p in enumerate(pnls)]


class Guard(unittest.TestCase):
    def test_breaker_ok(self):
        b = guard.circuit_breaker(_closed(1, -1, 1), 0.0, 20, now=5000)
        self.assertEqual((b["state"], b["mult"]), ("ok", 1.0))

    def test_breaker_streak_reduce_then_pause_then_release(self):
        two = guard.circuit_breaker(_closed(1, -1, -1), 0, 20, now=1200)
        self.assertEqual((two["state"], two["mult"]), ("reduce", 0.5))
        three = _closed(-1, -1, -1)
        paused = guard.circuit_breaker(three, 0, 20, now=1200)
        self.assertEqual((paused["state"], paused["mult"]), ("cooldown", 0.0))
        later = guard.circuit_breaker(three, 0, 20, now=1120 + guard.BREAKER["cooldown_s"] + 1)
        self.assertEqual(later["state"], "reduce")              # pause over, still half size

    def test_breaker_day_loss_reduces(self):
        b = guard.circuit_breaker([], -0.5, 20, now=1)
        self.assertEqual((b["state"], b["mult"]), ("reduce", 0.5))

    def test_breaker_never_increases_size(self):
        for closed in ([], _closed(1), _closed(-1), _closed(-1, -1, -1)):
            for day in (-5, 0, 5):
                self.assertLessEqual(guard.circuit_breaker(closed, day, 20, now=1500)["mult"], 1.0)

    def test_size_multiplier(self):
        self.assertEqual(guard.size_multiplier([]), (1.0, []))
        self.assertEqual(guard.size_multiplier([("a", 0.5)]), (0.5, ["a"]))
        m, names = guard.size_multiplier([("a", 0.5), ("b", 0.5), ("c", 0.5)])
        self.assertEqual(m, guard.MIN_SIZE_MULT)
        self.assertEqual(guard.size_multiplier([("a", 1.5)]), (1.0, []))   # can't grow
        self.assertEqual(guard.size_multiplier([("a", 0.0)])[0], 0.0)

    def test_checklist(self):
        ok = guard.pretrade_checklist(stop_pct=0.35, ticket=3, max_ticket=5,
                                      breaker={"state": "ok"}, block_flags=[],
                                      quote_confirmed=None)
        self.assertTrue(ok["ok"])
        bad = guard.pretrade_checklist(stop_pct=None, ticket=9, max_ticket=5,
                                       breaker={"state": "cooldown"}, block_flags=["closable"],
                                       quote_confirmed=False, paper_only=False)
        self.assertFalse(bad["ok"])
        self.assertEqual(len(bad["failed"]), 6)
        # The mode line must permit a real fill when live is genuinely armed, and
        # refuse it when the desk claims live but the live broker is not active.
        live = guard.pretrade_checklist(stop_pct=0.35, ticket=3, max_ticket=5,
                                        breaker={"state": "ok"}, block_flags=[],
                                        quote_confirmed=None, paper_only=False, live_armed=True)
        self.assertTrue(live["ok"])
        not_armed = guard.pretrade_checklist(stop_pct=0.35, ticket=3, max_ticket=5,
                                             breaker={"state": "ok"}, block_flags=[],
                                             quote_confirmed=None, paper_only=False)
        self.assertIn("mode: paper, or live is armed", not_armed["failed"])

    def test_live_path_runs_the_caution_layer(self):
        """Tripwire: the live branch must call _caution with the live cap, and the
        breaker must not be gated on paper mode. This regressed once; a source
        assertion is the cheapest way to keep it from regressing silently."""
        src = code_only("solana_agent.py")
        self.assertIn("max_ticket=caps['max_trade']", src)   # _caution runs on live
        self.assertIn("live_armed=live_armed", src)
        # circuit_breaker must be called unconditionally, not under `mode != live`
        self.assertNotIn('if self.mode != "live":\n            self.regime', src)

    def test_review_exit(self):
        r = guard.review_exit(symbol="DOG", reason="stop -35%", entry=1.0, exit_price=0.62,
                              peak=1.4, held_s=240, pnl_usd=-1.2, horizon="scalp",
                              flags=["paid_promo"], regime="RISK_OFF")
        self.assertFalse(r["won"])
        self.assertIn("DOG", r["note"])
        self.assertIn("regime RISK_OFF", r["note"])
        self.assertIn("up 30%+", r["lesson"])
        w = guard.review_exit(symbol="CAT", reason="TP2", entry=1.0, exit_price=1.3, peak=2.0,
                              held_s=3000, pnl_usd=0.8, horizon="swing", flags=[], regime=None)
        self.assertTrue(w["won"])
        self.assertIn("tighter trail", w["lesson"])
        empty = guard.review_exit(symbol="X", reason="r", entry=None, exit_price=None, peak=None,
                                  held_s=None, pnl_usd=None, horizon=None, flags=None, regime=None)
        self.assertIn("X", empty["note"])

    def test_coach_needs_data_and_is_advisory(self):
        self.assertEqual(guard.coach([])["rules"], [])
        revs = [{"won": False, "reason": "stop -35%", "flags": ["paid_promo"], "lesson": ""}] * 6
        c = guard.coach(revs)
        self.assertGreaterEqual(len(c["rules"]), 2)


class SignalsAggregator(unittest.TestCase):
    def test_offline_returns_nothing(self):
        out = signals.security(MINT, Sess(Resp(200)))
        self.assertEqual((out["chips"], out["block"], out["halve"]), ([], [], []))

    def test_combines_sources_with_online(self):
        goplus.reset(); promo.reset(); gmgn.reset(); pumpportal.reset()
        with mock.patch("desk.signals.offline", return_value=False), \
                mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GMGN_API_KEY", None)
            s = Sess(Resp(200, {"result": {MINT: _rec(closable={"status": "1", "authority": []},
                                                      transfer_fee={"current_fee_rate": {"fee_rate": "50"},
                                                                    "scheduled_fee_rate": []})}}),
                     Resp(200, [{"chainId": "solana", "tokenAddress": MINT}]),
                     Resp(200, []))
            out = signals.security(MINT, s)
        self.assertIn("closable", out["block"])
        self.assertIn("transfer_fee", out["halve"])
        chips = {c["t"]: c["sev"] for c in out["chips"]}
        self.assertEqual(chips["closable"], "block")
        self.assertEqual(chips["paid_promo"], "info")
        self.assertIn("gmgn", out["unknown"])
        self.assertIn("pumpportal", out["unknown"])


if __name__ == "__main__":
    unittest.main()


class RpcUrlGuard(unittest.TestCase):
    def test_bare_key_is_not_a_url(self):
        from desk import chain, helius
        with mock.patch.dict(os.environ, {"SOLANA_RPC_URL": "0123456789abcdef"}, clear=False), \
                mock.patch.object(helius, "rpc_url", return_value=None), \
                mock.patch.object(helius, "configured", return_value=False):
            self.assertEqual(chain.rpc_url(), chain.PUBLIC_RPC)
            self.assertEqual(chain.rpc_source(), "public")
        with mock.patch.dict(os.environ, {"SOLANA_RPC_URL": "https://rpc.example/x"}, clear=False), \
                mock.patch.object(helius, "rpc_url", return_value=None), \
                mock.patch.object(helius, "configured", return_value=False):
            self.assertEqual(chain.rpc_url(), "https://rpc.example/x")
            self.assertEqual(chain.rpc_source(), "custom")
