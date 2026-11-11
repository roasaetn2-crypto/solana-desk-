"""FOMO links always carry r=romsq and are never hardcoded in the web UI."""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from desk import links  # noqa: E402


def _has_ref(url: str) -> bool:
    return re.search(r"[?&]r=romsq(?:&|$)", url) is not None or url.rstrip("/") == links.SIGNUP_URL.rstrip("/")


class FomoLinks(unittest.TestCase):
    def test_coin_url_requires_romsq_referral(self):
        u = links.coin_url("So11111111111111111111111111111111111111112")
        self.assertTrue(_has_ref(u), u)
        self.assertIn("r=romsq", u)
        self.assertIn("chainId=1399811149", u)
        self.assertTrue(u.startswith("https://fomo.family/coin?"))
        self.assertNotIn("bit.ly", u)
        self.assertNotIn("tinyurl", u)

    def test_unknown_chain_falls_back_to_signup_with_referral(self):
        u = links.coin_url("MINT", "nonsense")
        self.assertEqual(u, links.SIGNUP_URL)
        self.assertTrue(_has_ref(u))

    def test_profile_and_signup_carry_referral(self):
        self.assertTrue(_has_ref(links.SIGNUP_URL))
        self.assertIn("r=romsq", links.profile_url("@romsq"))
        self.assertIn("r=romsq", links.profile_url("romsq"))

    def test_with_url_attaches_the_module_link(self):
        row = links.with_url({"mint": "AbcMint111", "symbol": "X"})
        self.assertEqual(row["fomo_url"], links.coin_url("AbcMint111"))
        self.assertTrue(_has_ref(row["fomo_url"]))

    def test_web_ui_does_not_hardcode_fomo_host(self):
        """Every FOMO URL must be built by desk/links.py and sent as fomo_url."""
        banned = []
        for path in (ROOT / "web").rglob("*"):
            if path.suffix.lower() not in {".js", ".html", ".css"}:
                continue
            text = path.read_text(errors="ignore")
            if "fomo.family" in text:
                banned.append(str(path.relative_to(ROOT)))
        self.assertEqual(banned, [])


if __name__ == "__main__":
    unittest.main()
