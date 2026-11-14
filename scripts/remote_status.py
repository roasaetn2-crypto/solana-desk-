#!/usr/bin/env python3
"""Tell you what state your hosted desk is in, from outside it.

The failure modes all look the same from a browser -- a slow page, or no page --
but they have completely different fixes:

    live          the process is up and the paper loop is ticking
    asleep        free plan stopped the container; the next request wakes it
    suspended     monthly instance-hours exhausted; nothing wakes it this month
    not-deployed  the host answers but the app is not there (404 / no route)
    old-build     the app answers but predates the build you think you pushed
    unreachable   DNS failure or the service was deleted

Guessing between those is what makes "my site expired" so hard to act on, so
this asks the one endpoint that is unauthenticated and reads the answer.

Stdlib only, no repo imports: it must run on a phone with a bare python3.

Usage:
    python3 scripts/remote_status.py https://solana-desk-1.onrender.com
    python3 scripts/remote_status.py https://... --json
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

# A fresh boot is the only way to observe a spin-down from outside: the process
# restarted, so uptime reset. Anything under this is "it just started", which
# means it was not running a moment ago.
FRESH_BOOT_S = 120


def _get(url: str, timeout: float = 60.0) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": "desk-remote-status"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(200_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read(20_000).decode("utf-8", "replace")
    except urllib.error.URLError as e:
        return 0, f"URLError: {e.reason}"
    except Exception as e:  # timeouts, resets, TLS
        return 0, f"{type(e).__name__}: {e}"


def probe(base: str) -> dict:
    base = base.strip().rstrip("/")
    if not base.startswith("http"):
        base = "https://" + base
    t0 = time.time()
    code, body = _get(f"{base}/api/health")
    took = round(time.time() - t0, 2)

    out = {"url": base, "http": code, "seconds": took, "state": None, "detail": None}

    if code == 0:
        out["state"] = "unreachable"
        out["detail"] = body[:200]
        return out

    if code == 404:
        # Two very different causes: a Render service whose build failed, and a
        # host that simply is not this app (example.com also 404s /api/health).
        # The root page tells them apart: a desk serves its login/lock screen.
        rcode, rbody = _get(f"{base}/", timeout=20)
        looks_like_desk = "solana desk" in rbody.lower()
        out["state"] = "not-deployed"
        out["detail"] = (
            "host answered 404 and its root page is not a desk: wrong URL, or the "
            "service was deleted/renamed" if not looks_like_desk
            else "the desk host is up but /api/health is missing: failed build or "
                 "an old commit without that route")
        return out

    if code in (502, 503):
        # Render serves 503 for a suspended service and 502 for a container that
        # will not start. Both look like "site is down" in a browser.
        out["state"] = "suspended" if code == 503 else "crashing"
        out["detail"] = (f"HTTP {code}: Render is not routing to a container -- "
                         f"suspended (instance-hours used up) or a failed start")
        return out

    if code >= 500:
        out["state"] = "crashing"
        out["detail"] = f"HTTP {code}: the app started and failed"
        return out

    try:
        d = json.loads(body)
    except Exception:
        out["state"] = "not-the-desk"
        out["detail"] = "answered 200 but not JSON -- probably a host placeholder page"
        return out

    if not isinstance(d, dict) or "uptime_s" not in d:
        out["state"] = "old-build"
        out["detail"] = ("the app answers but has no uptime_s: it predates the "
                         "health change, so it is not the commit you think it is")
        return out

    up = int(d.get("uptime_s") or 0)
    k = d.get("keepalive") or {}
    x = d.get("exits") or {}
    out.update(
        uptime_s=up,
        booted_at=d.get("booted_at"),
        cycle=d.get("cycle"),
        running=d.get("running"),
        keepalive_on=k.get("on"),
        keepalive_pings=k.get("pings"),
        keepalive_fails=k.get("fails"),
        ladder=x.get("name"),
        ladder_source=x.get("source"),
    )

    if took > 10 and up < FRESH_BOOT_S:
        out["state"] = "asleep"
        out["detail"] = (f"took {took}s and the process is only {up}s old: this "
                         f"request woke it up")
    elif up < FRESH_BOOT_S:
        out["state"] = "just-restarted"
        out["detail"] = f"up {up}s -- a deploy, a crash-restart, or a wake"
    else:
        out["state"] = "live"
        out["detail"] = f"up {up // 60}m{up % 60}s, cycle {d.get('cycle')}"
    return out


HINT = {
    "live": "Nothing to do.",
    "asleep": ("Free plan stopped the container for inactivity. It wakes on the next "
               "request (~50s). To stop this happening, add an external pinger "
               "(cron-job.org / UptimeRobot); the in-app self-pinger cannot wake a "
               "sleeping box because it dies with the process."),
    "just-restarted": ("If you did not push, something restarted it: check the Logs "
                       "tab for a traceback or an out-of-memory kill."),
    "suspended": ("Monthly instance-hours are gone. Nothing you can do until the "
                  "month resets, or upgrade to Starter, or move to the phone."),
    "unreachable": ("DNS or connection failed: the service was deleted, renamed, or "
                    "your network is blocking it."),
    "not-deployed": ("Either the URL is wrong / the service was deleted, or the build "
                     "failed. Open the service in the Render dashboard and read the "
                     "deploy log; a 404 with no desk on / means nothing is serving."),
    "crashing": "The app starts and dies. Read the Logs tab.",
    "old-build": "Push again and watch the deploy finish.",
    "not-the-desk": "You are probably pointing at the wrong host.",
}


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if not args:
        print(__doc__.strip().splitlines()[-1])
        return 2
    r = probe(args[0])
    if "--json" in sys.argv:
        print(json.dumps(r, indent=2))
        return 0 if r["state"] == "live" else 1

    print(f"url    : {r['url']}")
    print(f"state  : {r['state'].upper()}")
    print(f"detail : {r['detail']}")
    if r.get("ladder"):
        print(f"ladder : {r['ladder']} (from {r['ladder_source']})")
    if r.get("keepalive_on") is not None:
        print(f"pinger : on={r['keepalive_on']} pings={r['keepalive_pings']} "
              f"fails={r['keepalive_fails']}")
    print(f"advice : {HINT.get(r['state'], '')}")
    return 0 if r["state"] == "live" else 1


if __name__ == "__main__":
    raise SystemExit(main())
