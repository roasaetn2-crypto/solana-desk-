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


if __name__ == "__main__":
    unittest.main()
