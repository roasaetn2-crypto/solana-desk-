"""Prove that a LOCAL Ollama can serve the AI second opinion, with no key and no credits.

Starts a fake Ollama on a spare port that speaks the two endpoints the advisor
uses (/api/tags for the liveness probe, /v1/chat/completions for the opinion),
points OLLAMA_BASE_URL at it, and runs a real Advisor.review().

    python3 scripts/verify_ollama_local.py

Exit code 0 = every check passed.
"""
from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PORT = 11435
OPINION = {"verdict": "skip", "confidence": 64,
           "reasons": ["top-10 holds 71% of supply", "dev already rugged twice"],
           "red_flags": ["concentrated"]}

_fails = 0
GREEN, RED, DIM, OFF = "\033[32m", "\033[31m", "\033[2m", "\033[0m"


def check(name: str, ok: bool, detail: str = ""):
    global _fails
    if ok:
        print(f"  {GREEN}PASS{OFF} {name}" + (f" {DIM}{detail}{OFF}" if detail else ""))
    else:
        _fails += 1
        print(f"  {RED}FAIL{OFF} {name}" + (f" {DIM}{detail}{OFF}" if detail else ""))


class FakeOllama(BaseHTTPRequestHandler):
    seen: list[dict] = []

    def log_message(self, *a):     # keep the output clean
        pass

    def _json(self, code: int, body: dict):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path.startswith("/api/tags"):
            self._json(200, {"models": [{"name": "llama3.2"}]})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        FakeOllama.seen.append({"path": self.path,
                                "auth": self.headers.get("Authorization"),
                                "model": body.get("model")})
        if self.path.startswith("/v1/chat/completions"):
            self._json(200, {"choices": [{"message": {"content": json.dumps(OPINION)}}]})
        else:
            self._json(404, {"error": "not found"})


def main() -> int:
    srv = HTTPServer(("127.0.0.1", PORT), FakeOllama)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    # No keys at all: local Ollama must be the only thing that can answer.
    for k in ("GEMINI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY",
              "CEREBRAS_API_KEY", "CLOUDFLARE_API_TOKEN", "OLLAMA_API_KEY"):
        os.environ.pop(k, None)
    os.environ["OLLAMA_BASE_URL"] = f"http://127.0.0.1:{PORT}"
    os.environ["BRAIN_OLLAMA_LOCAL_MODEL"] = "llama3.2"

    import desk.brain_advisor as ba

    print("\n=== 1. detection ===")
    check("OLLAMA_BASE_URL puts Ollama in local mode", ba.ollama_mode() == "local", ba.ollama_mode())
    check("local model list starts with the configured model",
          ba.ollama_local_models()[0] == "llama3.2", ba.ollama_local_models()[0])
    check("local needs no API key", ba.Ollama("ollama", "OLLAMA_API_KEY", 200).keys() == ["local"])

    adv = ba.Advisor()
    print("\n=== 2. ordering ===")
    names = [p.name for p in adv.providers()]
    check("local Ollama is tried FIRST", names[0] == "ollama", " > ".join(names))
    check("advisor reports itself enabled with zero keys", adv.enabled() is True)
    check("pair() picks the local Ollama", [p.name for p in adv.pair()] == ["ollama"])

    print("\n=== 3. a real review, no key, no credits ===")
    summary = ba.Advisor.summarize(
        {"symbol": "TEST", "price_usd": 0.0012, "liquidity_usd": 42000,
         "mcap_usd": 310000, "age_minutes": 44},
        {"top_10_pct": 71.0, "top_wallet_pct": 19.0},
        {"creator_prev_rugs": 2, "dev_hold_pct": 8.0},
        {"total": 58}, {"chips": []}, {"x": True})
    logs: list[str] = []
    res = adv.review("So1TestMint", summary, logs.append)

    check("review returned a result", res is not None)
    if res:
        check("verdict came from ollama", res.get("source") == "ollama", str(res.get("source")))
        check("verdict is skip", res.get("verdict") == "skip", str(res.get("verdict")))
        check("confidence is 64", res.get("confidence") == 64, str(res.get("confidence")))
        check("reasons survived the round trip",
              (res.get("opinions", {}).get("ollama", {}).get("reasons") or [None])[0]
              == OPINION["reasons"][0])
        check("advisory_only flag set", res.get("advisory_only") is True)
    check("it logged the ollama opinion", any("brain(ollama)" in m for m in logs))

    print("\n=== 4. the request actually made ===")
    posts = [s for s in FakeOllama.seen if s["path"].startswith("/v1/")]
    check("hit the local /v1/chat/completions", bool(posts),
          posts[0]["path"] if posts else "no POST seen")
    check("sent NO Authorization header (no key to send)",
          bool(posts) and posts[0]["auth"] is None, str(posts[0]["auth"]) if posts else "")
    check("asked for the local model", bool(posts) and posts[0]["model"] == "llama3.2",
          posts[0]["model"] if posts else "")

    print("\n=== 5. cloud mode is untouched ===")
    os.environ.pop("OLLAMA_BASE_URL", None)
    ba._local_probe.update(at=0.0, up=False, host="")
    # The probe is a feature, but this check is about the no-local-Ollama case,
    # so pin it off rather than depend on what happens to be listening on 11434.
    real_probe = ba.ollama_local_up
    ba.ollama_local_up = lambda force=False: False
    try:
        check("no local host and no probe -> cloud", ba.ollama_mode() == "cloud", ba.ollama_mode())
        adv2 = ba.Advisor()
        names2 = [p.name for p in adv2.providers()]
        check("cloud Ollama stays LAST", names2[-1] == "ollama", " > ".join(names2))
    finally:
        ba.ollama_local_up = real_probe

    srv.shutdown()
    print()
    if _fails:
        print(f"{RED}{_fails} check(s) failed{OFF}")
        return 1
    print(f"{GREEN}all checks passed{OFF} - local Ollama serves the second opinion "
          "with no key and no credits")
    return 0


if __name__ == "__main__":
    sys.exit(main())
