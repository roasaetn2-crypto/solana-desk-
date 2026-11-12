"""Paper autostart and the optional Turso store. No secrets, no live trading."""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from desk import intent, store


class FakeTurso:
    """In-memory libSQL stand-in that speaks the /v2/pipeline payload."""

    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        self.tokens = []
        self.urls = []

    def __call__(self, url, token, payload):
        self.urls.append(url)
        self.tokens.append(token)
        results = []
        for req in payload.get("requests") or []:
            if req.get("type") == "close":
                results.append({"type": "ok", "response": {"type": "close"}})
                continue
            stmt = req["stmt"]
            sql = stmt["sql"]
            args = []
            for a in stmt.get("args") or []:
                typ, val = a.get("type"), a.get("value")
                if typ == "null" or val is None:
                    args.append(None)
                elif typ == "integer":
                    args.append(int(val))
                elif typ == "float":
                    args.append(float(val))
                else:
                    args.append(val)
            cur = self.db.execute(sql, args)
            self.db.commit()
            if cur.description:
                cols = [d[0] for d in cur.description]
                rows = []
                for row in cur.fetchall():
                    cells = []
                    for v in row:
                        if v is None:
                            cells.append({"type": "null"})
                        elif isinstance(v, int) and not isinstance(v, bool):
                            cells.append({"type": "integer", "value": str(v)})
                        elif isinstance(v, float):
                            cells.append({"type": "float", "value": str(v)})
                        else:
                            cells.append({"type": "text", "value": str(v)})
                    rows.append(cells)
                results.append({"type": "ok", "response": {"type": "execute", "result": {
                    "cols": [{"name": c} for c in cols], "rows": rows,
                    "affected_row_count": 0}}})
            else:
                results.append({"type": "ok", "response": {"type": "execute", "result": {
                    "cols": [], "rows": [], "affected_row_count": cur.rowcount,
                    "last_insert_rowid": str(cur.lastrowid)}}})
        return 200, {"results": results}


class AgentStub:
    def __init__(self, mode="paper"):
        self.mode = mode
        self.calls = []

    def start(self, persist_intent=True, intent_source="operator"):
        self.calls.append({"persist_intent": persist_intent, "intent_source": intent_source})
        return {"ok": True, "message": "agent started"}


class Autostart(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {
            "DESK_DATA_DIR": self.tmp.name,
            "PAPER_AUTOSTART": "1",
        }, clear=False)
        self.env.start()
        # a leftover KILL in the test cwd must not leak in
        self.kill_patch = mock.patch("desk.intent.killed", return_value=False)
        self.kill_patch.start()

    def tearDown(self):
        self.kill_patch.stop()
        self.env.stop()
        self.tmp.cleanup()

    def test_default_is_on(self):
        os.environ.pop("PAPER_AUTOSTART", None)
        self.assertTrue(intent.paper_autostart_enabled())
        go, why = intent.should_autostart("paper")
        self.assertTrue(go)
        self.assertIn("starting", why)

    def test_flag_off(self):
        os.environ["PAPER_AUTOSTART"] = "0"
        go, why = intent.should_autostart("paper")
        self.assertFalse(go)
        self.assertEqual(why, "off")

    def test_never_autostart_live(self):
        os.environ["PAPER_AUTOSTART"] = "1"
        go, why = intent.should_autostart("live")
        self.assertFalse(go)
        self.assertEqual(why, "mode is live")

    def test_respects_operator_pause(self):
        intent.record_loop_intent("stopped", source="operator")
        go, why = intent.should_autostart("paper")
        self.assertFalse(go)
        self.assertEqual(why, "operator paused")
        intent.record_loop_intent("paused", source="operator")
        go, why = intent.should_autostart("paper")
        self.assertFalse(go)

    def test_pause_survives_reload(self):
        intent.record_loop_intent("stopped", source="operator")
        # new process would re-read the file
        self.assertEqual(intent.loop_intent(), "stopped")
        self.assertTrue((Path(self.tmp.name) / "operator_intent.json").exists())

    def test_maybe_autostart_starts_paper_and_logs_once(self):
        agent = AgentStub("paper")
        with mock.patch("desk.netutil.offline", return_value=False), mock.patch("builtins.print") as pr:
            out = intent.maybe_autostart(agent)
        self.assertTrue(out["started"])
        self.assertEqual(len(agent.calls), 1)
        self.assertEqual(agent.calls[0]["intent_source"], "autostart")
        printed = " ".join(str(c.args[0]) for c in pr.call_args_list)
        self.assertIn("paper autostart: starting the loop", printed)
        self.assertEqual(printed.count("paper autostart:"), 1)

    def test_maybe_autostart_skips_when_paused(self):
        intent.record_loop_intent("stopped")
        agent = AgentStub("paper")
        with mock.patch("desk.netutil.offline", return_value=False), mock.patch("builtins.print") as pr:
            out = intent.maybe_autostart(agent)
        self.assertFalse(out["started"])
        self.assertEqual(agent.calls, [])
        printed = " ".join(str(c.args[0]) for c in pr.call_args_list)
        self.assertIn("operator paused", printed)


class Store(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fake = FakeTurso()
        self.env = mock.patch.dict(os.environ, {
            "DESK_DATA_DIR": self.tmp.name,
            "TURSO_DATABASE_URL": "libsql://desk-test.turso.io",
            "TURSO_AUTH_TOKEN": "secret-token-do-not-leak",
        }, clear=False)
        self.env.start()
        store.reset_runtime()
        store._poster = self.fake

    def tearDown(self):
        store._poster = None
        store.reset_runtime()
        self.env.stop()
        self.tmp.cleanup()

    def test_unconfigured_is_noop(self):
        store._poster = None
        os.environ.pop("TURSO_DATABASE_URL", None)
        os.environ.pop("TURSO_AUTH_TOKEN", None)
        os.environ.pop("DATABASE_URL", None)
        os.environ.pop("DATABASE_AUTH_TOKEN", None)
        store.reset_runtime()
        self.assertFalse(store.configured())
        store.append_journal({"kind": "cycle"})
        store.sync_kv_file("paper_ledger")
        self.assertEqual(store.restore()["configured"], False)

    def test_roundtrip_journal_ledger_intent_settings(self):
        d = Path(self.tmp.name)
        journal = d / "journal.jsonl"
        events = [
            {"time": "2026-11-12T00:00:00+00:00", "kind": "decision", "mint": "MintA"},
            {"time": "2026-11-12T00:00:30+00:00", "kind": "entry", "mint": "MintA"},
        ]
        journal.write_text("".join(json.dumps(e) + "\n" for e in events))
        (d / "paper_ledger.json").write_text(json.dumps({"cash": 18.5, "positions": {"MintA": {"qty": 1}}, "closed": []}))
        (d / "operator_intent.json").write_text(json.dumps({"loop": "running"}))
        (d / "operator_settings.json").write_text(json.dumps({"risk.max_daily_loss_usd": 5.0}))

        out = store.restore()  # remote empty -> push local
        self.assertIn("journal:2", " ".join(out["pushed"]))
        self.assertIn("paper_ledger", out["pushed"])
        self.assertIn("operator_intent", out["pushed"])
        self.assertIn("operator_settings", out["pushed"])

        store.append_journal({"time": "2026-11-12T00:01:00+00:00", "kind": "exit", "mint": "MintA"})

        # wipe local files as a deploy would
        for p in d.iterdir():
            if p.is_file():
                p.unlink()
        store.reset_runtime()
        pulled = store.restore()
        self.assertIn("journal", pulled["pulled"])
        self.assertIn("paper_ledger", pulled["pulled"])
        self.assertIn("operator_intent", pulled["pulled"])
        self.assertIn("operator_settings", pulled["pulled"])
        lines = [json.loads(ln) for ln in journal.read_text().splitlines() if ln.strip()]
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[-1]["kind"], "exit")
        ledger = json.loads((d / "paper_ledger.json").read_text())
        self.assertEqual(ledger["cash"], 18.5)
        self.assertEqual(json.loads((d / "operator_intent.json").read_text())["loop"], "running")
        self.assertEqual(json.loads((d / "operator_settings.json").read_text())["risk.max_daily_loss_usd"], 5.0)

    def test_errors_never_include_the_token(self):
        def boom(url, token, payload):
            raise RuntimeError(f"auth failed for {token} at {url}")
        store._poster = boom
        store.reset_runtime()
        with mock.patch("builtins.print") as pr:
            store.append_journal({"kind": "cycle"})
        printed = " ".join(str(c.args[0]) for c in pr.call_args_list)
        self.assertNotIn("secret-token-do-not-leak", printed)
        self.assertIn("[redacted]", printed)

    def test_pipeline_url_shape(self):
        self.assertTrue(store.pipeline_url("libsql://desk-test.turso.io").startswith("https://"))
        self.assertTrue(store.pipeline_url("libsql://desk-test.turso.io").endswith("/v2/pipeline"))
        self.assertEqual(
            store.pipeline_url("https://desk-test.turso.io/v2/pipeline"),
            "https://desk-test.turso.io/v2/pipeline",
        )


if __name__ == "__main__":
    unittest.main()
