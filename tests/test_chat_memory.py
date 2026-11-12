"""Desk AI conversation persistence: local file + Turso, no secrets, trimmed."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from desk import chat_memory, store  # noqa: E402
from tests.test_persist import FakeTurso  # noqa: E402

SECRET = "deskTOKEN999xyz"


class Memory(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"DESK_DATA_DIR": self.tmp.name, "DESK_TOKEN": SECRET})
        self.env.start()
        chat_memory.reset()

    def tearDown(self):
        chat_memory.reset()
        self.env.stop()
        self.tmp.cleanup()

    def test_append_trim_and_reload(self):
        for i in range(55):
            chat_memory.append({"role": "user" if i % 2 == 0 else "ai", "text": f"msg {i}", "t": i})
        msgs = chat_memory.messages()
        self.assertEqual(len(msgs), 50)
        self.assertEqual(msgs[0]["text"], "msg 5")
        self.assertEqual(msgs[-1]["text"], "msg 54")
        path = Path(self.tmp.name) / "desk_chat.json"
        self.assertTrue(path.exists())
        chat_memory.reset()
        again = chat_memory.messages()
        self.assertEqual(len(again), 50)
        self.assertEqual(again[-1]["text"], "msg 54")

    def test_clear(self):
        chat_memory.append({"role": "user", "text": "hi", "t": 1})
        chat_memory.clear()
        self.assertEqual(chat_memory.messages(), [])
        chat_memory.reset()
        self.assertEqual(chat_memory.messages(), [])

    def test_seed_only_when_empty(self):
        first = chat_memory.seed([{"role": "user", "text": "cached", "t": 1},
                                  {"role": "ai", "text": "ok", "t": 2}])
        self.assertEqual([m["text"] for m in first], ["cached", "ok"])
        second = chat_memory.seed([{"role": "user", "text": "nope", "t": 3}])
        self.assertEqual([m["text"] for m in second], ["cached", "ok"])

    def test_scrubs_secrets(self):
        chat_memory.append({"role": "ai", "text": f"token {SECRET} stay secret", "t": 1})
        text = chat_memory.messages()[0]["text"]
        self.assertNotIn(SECRET, text)
        raw = (Path(self.tmp.name) / "desk_chat.json").read_text()
        self.assertNotIn(SECRET, raw)

    def test_history_for_model(self):
        chat_memory.append({"role": "user", "text": "one", "t": 1})
        chat_memory.append({"role": "ai", "text": "two", "t": 2})
        h = chat_memory.history_for_model()
        self.assertEqual(h, [{"role": "user", "text": "one"}, {"role": "ai", "text": "two"}])

    def test_rejects_bad_payloads(self):
        self.assertEqual(chat_memory.append({"role": "system", "text": "nope"}), [])
        self.assertEqual(chat_memory.append({"role": "user", "text": "   "}), [])
        self.assertEqual(chat_memory.append("plain"), [])


class TursoRoundtrip(unittest.TestCase):
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
        chat_memory.reset()

    def tearDown(self):
        store._poster = None
        store.reset_runtime()
        chat_memory.reset()
        self.env.stop()
        self.tmp.cleanup()

    def test_survives_deploy_wipe(self):
        chat_memory.append({"role": "user", "text": "why was PEPE skipped", "t": 1})
        chat_memory.append({"role": "ai", "text": "ticket under the floor", "t": 2})
        d = Path(self.tmp.name)
        for p in d.iterdir():
            if p.is_file():
                p.unlink()
        chat_memory.reset()
        store.reset_runtime()
        out = store.restore()
        self.assertIn("desk_chat", out["pulled"])
        chat_memory.reset()
        msgs = chat_memory.messages()
        self.assertEqual([m["text"] for m in msgs], ["why was PEPE skipped", "ticket under the floor"])


if __name__ == "__main__":
    unittest.main()
