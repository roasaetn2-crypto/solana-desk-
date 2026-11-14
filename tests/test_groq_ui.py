"""The Groq sizer panel: the dashboard must actually show the model in action.

These are source and contract assertions, not pixel tests. The failure mode they
guard against is the one that already happened twice on this desk: a backend
feature ships, the UI silently never renders it, and the operator believes the
model is working because the code says it is. A panel that is wired to nothing
looks identical to a panel with nothing to report.
"""
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from desk import groq_sizer as g  # noqa: E402

WEB = ROOT / "web"


def read(name: str) -> str:
    return (WEB / name).read_text(encoding="utf-8")


class GroqPanel(unittest.TestCase):
    def test_status_contract(self):
        """status() is what the panel polls, so its keys are the API."""
        s = g.status()
        for k in ("enabled", "reason", "model", "budget_total", "budget_left",
                  "calls_today", "min_mult", "max_mult", "recent", "recent_calls",
                  "recent_cuts", "avg_cut"):
            self.assertIn(k, s)
        # The clamp is the whole safety story: a panel that reported a cap above
        # 1.0 would mean the sizer can grow a ticket, which it must never do.
        self.assertEqual(s["max_mult"], 1.0)
        self.assertGreaterEqual(s["min_mult"], 0.0)
        self.assertLessEqual(s["min_mult"], s["max_mult"])
        self.assertIsInstance(s["recent"], list)

    def test_status_never_raises_or_calls_out(self):
        """The panel polls on every refresh. A status() that could raise or do
        network I/O would either break the dashboard or spend the quota it is
        reporting on."""
        src = (ROOT / "desk" / "groq_sizer.py").read_text(encoding="utf-8")
        body = src.split("def status()", 1)[1].split("\ndef ", 1)[0]
        self.assertNotIn("requests.", body)
        self.assertNotIn("urlopen", body)
        self.assertIn("except Exception", body)
        # And it must be a pure read of the ring, not a re-derivation.
        self.assertIn("_RECENT", body)

    def test_recent_ring_is_bounded(self):
        """An unbounded ring would grow for the life of the process."""
        src = (ROOT / "desk" / "groq_sizer.py").read_text(encoding="utf-8")
        self.assertRegex(src, r"_RECENT:\s*deque\s*=\s*deque\(maxlen=\d+\)")

    def test_note_records_cuts_only_when_shrunk(self):
        """`cut` drives the amber styling. If it were true at 1.0x every row
        would look like an intervention."""
        before = len(g._RECENT)
        g._note(0.5, {"source": "groq", "confidence": 70, "reasons": ["thin"]}, "m1", "AAA")
        g._note(1.0, {"source": "groq", "confidence": 50, "reasons": []}, "m2", "BBB")
        rows = list(g._RECENT)
        self.assertEqual(len(rows), min(before + 2, g._RECENT.maxlen))
        self.assertTrue(rows[1]["cut"], "0.5x should be marked as a cut")
        self.assertFalse(rows[0]["cut"], "1.0x should not be marked as a cut")
        # Newest first, so the panel reads top-down as most recent.
        self.assertEqual(rows[0]["symbol"], "BBB")

    def test_note_never_raises_on_garbage(self):
        """_note sits inside the hot path of every buy. It must swallow junk."""
        g._note(0.7, {"reasons": "not-a-list"}, None, None)   # type: ignore[arg-type]
        g._note("x", {}, "m", "S")                            # type: ignore[arg-type]

    def test_html_panel_and_ids(self):
        html = read("index.html")
        self.assertIn('id="groqPanel"', html)
        for el in ("groqBadge", "groqLine", "groqBudgetBar", "groqBudgetNum",
                   "groqSummary", "groqList"):
            self.assertIn(f'id="{el}"', html, f"missing #{el} in index.html")
        # The panel must sit inside the left column, not float outside the grid.
        left = html.split('class="col col-left"', 1)[1].split("</section>", 1)[0]
        self.assertIn('id="groqPanel"', left)

    def test_js_renderer_is_wired_into_refresh(self):
        js = read("app.js")
        self.assertIn("function renderGroq(", js)
        self.assertIn("function groqRow(", js)
        self.assertIn("function groqChip(", js)
        # refresh() is the only thing that runs on the poll timer. If renderGroq
        # is not called from it, the panel is static HTML forever.
        refresh = js.split("async function refresh()", 1)[1]
        self.assertIn("renderGroq(s && s.groq_sizer)", refresh)
        # The chip reads the multiplier the agent stored, never re-derives one.
        chip = js.split("function groqChip(", 1)[1].split("function ", 1)[0]
        self.assertIn("sizes.guard.groq", chip)
        self.assertIn("mult < 1", chip)

    def test_js_has_no_unescaped_interpolation_of_reasons(self):
        """Reasons come from a model and can contain quotes or angle brackets.
        Every interpolation in the panel rows must go through esc()."""
        js = read("app.js")
        row = js.split("function groqRow(", 1)[1].split("function renderGroq(", 1)[0]
        for m in re.finditer(r"\$\{([^}]*)\}", row):
            expr = m.group(1).strip()
            if expr.startswith("esc(") or expr.startswith("meta ?"):
                continue
            if expr in ("cls", "multTxt"):
                continue
            self.fail(f"unescaped interpolation in groqRow: ${{{expr}}}")

    def test_css_classes_exist(self):
        css = read("styles.css")
        for sel in (".groq-list", ".groq-row", ".groq-row.cut", ".groq-row.pass",
                    ".groq-row.fail", ".groq-row .mult"):
            self.assertIn(sel, css, f"missing {sel} in styles.css")

    def test_agent_exposes_the_payload(self):
        src = (ROOT / "desk" / "solana_agent.py").read_text(encoding="utf-8")
        self.assertIn('"groq_sizer": groq_sizer.status()', src)


if __name__ == "__main__":
    unittest.main()
