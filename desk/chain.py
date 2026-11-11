"""Seat 2: chain facts. Straight from a Solana RPC node, no model involved.

Facts kill before judgements do: an open mint authority is not an opinion,
so it never reaches the analyst.
"""
from __future__ import annotations

import hashlib
import logging
import os
import random
import threading
import time

import requests

from config import CHAIN
from desk import helius

log = logging.getLogger("desk.chain")

PUBLIC_RPC = "https://api.mainnet-beta.solana.com"
SYSTEM_PROGRAM = "11111111111111111111111111111111"


def rpc_url() -> str:
    """Helius if a key is set, else SOLANA_RPC_URL, else the public node.

    Resolved per call, not at import, so the desk can be pointed at a better
    node without a restart and so a missing key is never a crash.
    """
    custom = os.getenv("SOLANA_RPC_URL", "").strip()
    if custom and not custom.lower().startswith(("http://", "https://")):
        custom = ""        # a bare key or typo is not a URL: use the public node, never crash
    return helius.rpc_url() or custom or PUBLIC_RPC


def rpc_source() -> str:
    return "helius" if helius.configured() else (
        "custom" if rpc_url() != PUBLIC_RPC else "public")


# After a Helius HTTP error, skip it for a bit so one 401/429 does not
# stall every mint in the cycle. Tests call reset_rpc_state().
_COOL_LOCK = threading.Lock()
_cool_until = 0.0
_COOL_S = {401: 300, 403: 300, 429: 15}


def reset_rpc_state() -> None:
    global _cool_until
    with _COOL_LOCK:
        _cool_until = 0.0
    helius.reset_throttle()


def _cool_helius(status: int) -> None:
    global _cool_until
    seconds = _COOL_S.get(status, 15 if status >= 500 else 15)
    with _COOL_LOCK:
        _cool_until = max(_cool_until, time.time() + seconds)


def _helius_cooled() -> bool:
    with _COOL_LOCK:
        return time.time() < _cool_until


def _http_error(method: str, r: requests.Response) -> requests.HTTPError:
    # Never put the request URL in the exception: it may contain the API key.
    return requests.HTTPError(f"{method} HTTP {r.status_code}", response=r)


def _post_rpc(url: str, method: str, params: list, session: requests.Session | None):
    """One JSON-RPC call to a specific URL, with backoff on 429/5xx."""
    if helius.is_helius_url(url):
        helius.throttle()
    s = session or requests.Session()
    last = None
    for attempt in range(4):
        if helius.is_helius_url(url) and attempt:
            helius.throttle()
        r = s.post(url,
                   json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                   timeout=20)
        if r.status_code == 429 or r.status_code >= 500:
            last = r
            time.sleep(1.5 * (attempt + 1))   # 1.5s, 3s, 4.5s
            continue
        if r.status_code >= 400:
            raise _http_error(method, r)
        body = r.json()
        err = body.get("error") if isinstance(body, dict) else None
        if err is not None:
            code = err.get("code") if isinstance(err, dict) else None
            if code == 429:
                last = r
                time.sleep(1.5 * (attempt + 1))
                continue
            raise RuntimeError(f"{method}: {err}")
        return body["result"]
    if last is not None:
        raise _http_error(method, last)
    raise RuntimeError(f"{method}: retries exhausted")


def rpc(method: str, params: list, session: requests.Session | None = None):
    """One JSON-RPC call, with backoff and a public-RPC fallback.

    The free public node allows only a handful of requests per second and
    answers 429 under any real load. A single 429 used to abort the whole
    token, which is why nothing ever reached the analyst. Retry instead.

    When Helius is configured it is tried first. An HTTP error from Helius
    (401/403 from a bad key, 429 on the free tier, 5xx) must not kill the
    coin: fall back to the public node, which is how the desk ran before.
    Exception text never includes the request URL, so the API key stays out
    of logs and the journal.
    """
    primary = rpc_url()
    url = PUBLIC_RPC if (helius.is_helius_url(primary) and _helius_cooled()) else primary
    try:
        return _post_rpc(url, method, params, session)
    except requests.HTTPError as e:
        status = e.response.status_code if e.response is not None else 0
        if helius.is_helius_url(url) and url.rstrip("/") != PUBLIC_RPC.rstrip("/"):
            _cool_helius(status)
            log.warning("helius %s failed (HTTP %s); falling back to public RPC",
                        method, status or "error")
            return _post_rpc(PUBLIC_RPC, method, params, session)
        raise


def _demo_facts(mint: str) -> dict:
    """Synthetic but stable facts for demo mints (hash-seeded, so a mint keeps its shape)."""
    h = int(hashlib.sha256(mint.encode()).hexdigest(), 16)
    rng = random.Random(h)
    open_mint = rng.random() < 0.12
    open_freeze = rng.random() < 0.08
    top1 = round(rng.uniform(0.01, 0.14), 4)
    top10 = round(min(0.95, top1 + rng.uniform(0.05, 0.42)), 4)
    return {
        "mint_authority": "DemoMintAuth111" if open_mint else None,
        "freeze_authority": "DemoFreezeAuth111" if open_freeze else None,
        "supply": rng.uniform(1e6, 1e9),
        "top_wallet_pct": top1,
        "top_10_pct": top10,
        "program_held_pct": round(rng.uniform(0.1, 0.75), 4),
        "wallets_counted": rng.randint(6, 20),
        "holder_count": rng.randint(40, 900),
        "source": "demo",
    }


def facts(mint: str, session: requests.Session | None = None) -> dict:
    """Mint/freeze authority, supply, and holder concentration of REAL wallets.

    The bonding curve and the AMM pool hold huge balances by design. Their token
    accounts belong to program-owned addresses, not to wallets, so we look up who
    owns each top account and drop anything not owned by the System Program.
    """
    if mint.startswith("DEMO"):
        return _demo_facts(mint)

    info = rpc("getAccountInfo", [mint, {"encoding": "jsonParsed"}], session)
    if not info or not info.get("value"):
        raise RuntimeError(f"{mint}: account not found")
    parsed = info["value"]["data"]["parsed"]["info"]
    decimals = int(parsed["decimals"])
    supply = int(parsed["supply"]) / 10 ** decimals

    base = {
        "mint_authority": parsed.get("mintAuthority"),
        "freeze_authority": parsed.get("freezeAuthority"),
        "supply": supply,
        "decimals": decimals,
        "top_wallet_pct": None,
        "top_10_pct": None,
        "program_held_pct": None,
        "wallets_counted": 0,
        "holder_count": None,
        "source": rpc_source(),
        "partial": True,          # holder distribution not available
    }

    # getTokenLargestAccounts is the expensive call. The free public node
    # throttles it to 429 almost always, and a single 429 used to throw away
    # the whole token even though the authority checks above had succeeded.
    # Fall back to authority-only facts and mark them partial.
    try:
        largest = rpc("getTokenLargestAccounts", [mint], session)["value"]
    except Exception:
        return base

    largest = largest[: CHAIN["largest_accounts_checked"]]
    token_accts = [x["address"] for x in largest]

    # token account -> its owner address
    try:
        accts = rpc("getMultipleAccounts", [token_accts, {"encoding": "jsonParsed"}],
                    session)["value"]
    except Exception:
        return base

    owners = [((a or {}).get("data", {}).get("parsed", {}).get("info", {}) or {}).get("owner")
              for a in accts]

    # owner address -> which program owns it (System Program = a normal wallet)
    uniq = [o for o in dict.fromkeys(owners) if o]
    try:
        owner_accts = rpc("getMultipleAccounts", [uniq, {"encoding": "base64"}],
                          session)["value"] if uniq else []
    except Exception:
        return base
    program_of = {o: (acc or {}).get("owner") for o, acc in zip(uniq, owner_accts)}

    wallets: dict[str, float] = {}
    excluded = 0.0
    for row, owner in zip(largest, owners):
        amount = float(row.get("uiAmount") or 0)
        prog = program_of.get(owner)
        if owner and prog not in (None, SYSTEM_PROGRAM):
            excluded += amount            # curve, pool, vault: not a seller in the usual sense
            continue
        wallets[owner or row["address"]] = wallets.get(owner or row["address"], 0) + amount

    ranked = sorted(wallets.values(), reverse=True)
    share = lambda xs: (sum(xs) / supply) if supply else None

    # The public RPC only exposes the top 20 accounts. With Helius we can ask
    # how many accounts actually hold a balance, which is the number the
    # community score means by "holders".
    holders = helius.holder_count(mint, session) if helius.configured() else None

    return {
        "mint_authority": parsed.get("mintAuthority"),
        "freeze_authority": parsed.get("freezeAuthority"),
        "supply": supply,
        "decimals": decimals,
        "top_wallet_pct": share(ranked[:1]),
        "top_10_pct": share(ranked[:10]),
        "program_held_pct": share([excluded]),
        "wallets_counted": len(ranked),
        "holder_count": holders,
        "source": rpc_source(),
        "partial": False,
    }


def chain_kill(f: dict) -> str | None:
    """Plain facts against plain limits. First failure wins."""
    if f["mint_authority"] and not CHAIN["allow_mint_authority"]:
        return "mint_authority"
    if f["freeze_authority"] and not CHAIN["allow_freeze_authority"]:
        return "freeze_authority"
    if f["top_wallet_pct"] is not None and f["top_wallet_pct"] > CHAIN["max_top_wallet"]:
        return "top_wallet"
    if f["top_10_pct"] is not None and f["top_10_pct"] > CHAIN["max_top_10"]:
        return "top_10"
    return None
