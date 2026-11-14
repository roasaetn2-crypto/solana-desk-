#!/usr/bin/env python3
"""What is the crowd saying about a ticker right now? Free, no keys, one command.

    python scripts/social_report.py WIF
    python scripts/social_report.py --mint <mint-address>
    python scripts/social_report.py --watchlist
    python scripts/social_report.py --boosted

Reads three free sources through `desk/social.py`:

  telegram   public channel previews (t.me/s/<channel>) on the watchlist
  boosts     DexScreener paid-promotion feed
  profiles   DexScreener token profiles (carry the project's own X / Telegram)

Nothing here trades or writes state. It is the same reader the desk uses, run
by hand so you can see what it sees.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import SOCIAL                      # noqa: E402
from desk import social                        # noqa: E402


def show_pulse(ticker: str, mint: str | None = None) -> dict:
    p = social.pulse(ticker, mint)
    print(f"\n{'=' * 62}")
    print(f"TICKER: ${p.get('ticker') or ticker}")
    print(f"{'=' * 62}")
    if not p.get("available"):
        print(f"  unavailable: {p.get('why') or 'no data'}")
        return p
    print(f"  score            {p['score']}/100")
    print(f"  telegram msgs    {p['messages']}  across {p['channels_live']}/{p['channels_total']} live channels")
    print(f"  telegram views   {p['views']:,}")
    print(f"  boosted          {'yes (paid promotion)' if p['boosted'] else 'no'}")
    print(f"  profile          {'yes' if p['profiled'] else 'no'}"
          f"   x={p['has_x']}  telegram={p['has_telegram']}")
    if p.get("hits"):
        print("  where:")
        for h in p["hits"]:
            print(f"    @{h['channel']}: {h['messages']} msg, {h['views']:,} views")
            for s in h.get("samples", [])[:2]:
                print(f"      \"{s}\"")
    return p


def show_watchlist() -> None:
    print(f"\nTelegram watchlist ({len(SOCIAL['channels'])} channels)")
    print("-" * 62)
    for ch in SOCIAL["channels"]:
        r = social.telegram(ch)
        state = f"{len(r['messages'])} msgs" if r["available"] else f"unavailable ({r['why']})"
        print(f"  @{ch:<26} {state}")


def show_boosted(limit: int = 15) -> None:
    b = social.dex_boosts()
    p = social.dex_profiles()
    print(f"\nDexScreener boosts: {len(b.get('mints') or {})} Solana mints")
    print(f"DexScreener profiles: {len(p.get('mints') or {})} Solana mints")
    print("-" * 62)
    for mint, info in list((b.get("mints") or {}).items())[:limit]:
        prof = (p.get("mints") or {}).get(mint) or {}
        print(f"  {mint[:12]}…  boost={info.get('amount')} total={info.get('total')}"
              f"  x={prof.get('has_x')} tg={prof.get('has_telegram')}")
        if info.get("description"):
            print(f"      {info['description'][:100]}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Read ticker-keyed social attention.")
    ap.add_argument("ticker", nargs="?", help="ticker, e.g. WIF or $WIF")
    ap.add_argument("--mint", help="Solana mint address (for boost/profile lookup)")
    ap.add_argument("--watchlist", action="store_true", help="check every channel")
    ap.add_argument("--boosted", action="store_true", help="list boosted mints")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    if args.watchlist:
        show_watchlist()
        return 0
    if args.boosted:
        show_boosted()
        return 0
    if not args.ticker:
        ap.print_help()
        return 1

    p = show_pulse(args.ticker, args.mint)
    if args.json:
        print(json.dumps(p, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
