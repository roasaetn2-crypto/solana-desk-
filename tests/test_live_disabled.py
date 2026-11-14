"""LOCAL CHANGE: prove live trading can never arm and nothing can be signed."""
import os, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from desk import live
from desk.solana_agent import SolanaAgent


class LiveDisabled(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); d = Path(self.tmp.name)
        self.a = SolanaAgent(ledger=d / "p.json", bench_path=d / "b.db", live_ledger=d / "l.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_ready_is_false_even_with_a_key_in_env(self):
        os.environ["SOLANA_PRIVATE_KEY"] = "[1,2,3]"
        try:
            ok, why = live.ready()
            self.assertFalse(ok); self.assertIn("DISABLED", why)
            self.assertIsNone(live._key_material())
        finally:
            os.environ.pop("SOLANA_PRIVATE_KEY", None)

    def test_set_mode_live_rejected_with_correct_phrase(self):
        r = self.a.set_mode("live", live.EXECUTION["live_confirm_phrase"])
        self.assertFalse(r["ok"]); self.assertIn("DISABLED", r["error"])
        self.assertEqual(self.a.mode, "paper")

    def test_broker_is_paper_even_if_mode_forced(self):
        self.a.mode = "live"
        self.assertNotIsInstance(self.a.broker, live.LiveBroker)

    def test_livebroker_cannot_buy_sell_or_sign(self):
        b = self.a.live
        with self.assertRaises(live.LiveError): b.buy("RealMint111", "X", 10, 1.0)
        with self.assertRaises(live.LiveError): b._sign_and_send({})
        with self.assertRaises(live.LiveError): live._keypair()
        self.assertIsNone(b.pubkey())

    def test_live_buy_accepts_groq_mult(self):
        """The agent passes groq_mult on every buy. If LiveBroker.buy does not
        accept it, every real fill would TypeError at the last step."""
        import inspect
        params = inspect.signature(live.LiveBroker.buy).parameters
        self.assertIn("groq_mult", params)
        self.assertIn("horizon", params)

    def test_breaker_runs_in_live_mode(self):
        """The drawdown breaker used to be gated on `mode != live`, so it never
        updated for real money. Seed three losses in a row, force live mode, run
        one cycle, and assert the breaker actually tripped."""
        import time as _t
        from unittest import mock
        now = _t.time()
        self.a.paper.s["closed"] = [{"pnl_usd": -1.0, "closed": now - 10},
                                    {"pnl_usd": -1.0, "closed": now - 20},
                                    {"pnl_usd": -1.0, "closed": now - 30}]
        self.a.mode = "live"
        with mock.patch("desk.solana_agent.scanner.new_tokens", return_value=[]):
            self.a.run_once()
        self.assertEqual(self.a.breaker["state"], "cooldown",
                         "breaker did not run on the live path")


if __name__ == "__main__":
    unittest.main()
