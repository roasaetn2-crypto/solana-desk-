"""Seat-to-seat dialogue: the desks talk to each other before a ticket exists.

The reference desk is a pipeline, but a pipeline is silent. This module makes
each seat address the next one, and lets the analyst and the risk engine argue:

    Scanner  -> Chain      "here is what survived my cuts"
    Chain    -> Dev        "authorities and concentration, straight off the node"
    Dev      -> Analyst    "creator holds X, snipers Y, my verdict is ..."
    Analyst  -> Risk       "here is the thesis and what would invalidate it"
    Risk     -> Analyst    "here is the cap I will allow, and why"
    Analyst  -> Risk       "rebuttal, or acceptance"
    Risk     -> Broker     "final: ticket or no ticket"

When a brain is configured (see desk/brain.py: local Ollama, Groq, Gemini,
OpenRouter or Claude), the analyst/risk exchange is a real model conversation:
each seat has its own persona and may not exceed its role. With no model at all,
the same exchange is rendered deterministically from the numbers, so the
transcript is always honest and always available.

The rule that never bends: the Risk seat speaks last on size, and the Broker
seat only fills what Risk allowed.
"""
from __future__ import annotations

import os

from config import ANALYST_MAX_TOKENS, ANALYST_MODEL, EXITS
from desk import brain

SEATS = {
    "scanner": "Seat 1 · Scanner",
    "chain": "Seat 2 · Chain facts",
    "dev": "Seat 2b · Dev investigator",
    "analyst": "Seat 3 · Analyst",
    "risk": "Seat 4 · Risk engine",
    "broker": "Seat 5 · Paper broker",
}

PERSONAS = {
    "scanner": ("You are the scanner seat. You only state what survived your arithmetic cuts: "
                "age, liquidity, volume, mcap, trades, sell ratio. You do not judge quality."),
    "chain": ("You are the chain-facts seat. You only state on-chain facts: mint/freeze authority, "
              "supply, top-wallet and top-10 concentration excluding the curve and pool. "
              "You never give an opinion on price."),
    "dev": ("You are the dev-investigator seat. You only speak about the creator wallet: how much "
            "the dev holds, prior launches and rugs, sniper/insider share, LP lock. "
            "You may issue a hard no. You never size a trade."),
    "analyst": ("You are the analyst seat. You form a thesis from the fields and the other seats' "
                "reports. You may argue for a setup, but you may never name a dollar size, "
                "never say 'buy', and never override a hard no."),
    "risk": ("You are the risk engine, speaking as a person for the transcript but bound by code. "
             "You state the three caps (risk, position size, 2% of pool), the smallest of which wins, "
             "and you may veto. You are the last word on size."),
    "broker": ("You are the paper broker. You only confirm what was filled, at what price, and "
               "which exit rules now apply. You never invent a trade."),
}


def _fmt_money(x) -> str:
    try:
        return f"${float(x):,.0f}"
    except (TypeError, ValueError):
        return "$0"


def _pct(x) -> str:
    return f"{x:.1%}" if isinstance(x, (int, float)) else "n/a"


# ---------- deterministic transcript ----------
def _template_debate(token: dict, facts: dict, ds: dict, dev: dict, sc: dict,
                     thesis: dict, approved: bool, ticket: float, reason: str,
                     sizes: dict) -> list[dict]:
    sym = token.get("symbol", "?")
    msgs: list[dict] = []

    def say(frm: str, to: str, text: str, stance: str = "info"):
        msgs.append({"from": frm, "to": to, "seat": SEATS[frm], "text": text,
                     "stance": stance})

    say("scanner", "chain",
        f"{sym} survived my cuts: age {token.get('age_minutes')}m, liq "
        f"{_fmt_money(token.get('liquidity_usd'))}, 1h vol "
        f"{_fmt_money(token.get('volume_h1'))}, {token.get('buys_h1')} buys / "
        f"{token.get('sells_h1')} sells, mcap {_fmt_money(token.get('mcap_usd'))}. "
        f"Over to you for the chain facts.")

    say("chain", "dev",
        f"On-chain: mint authority {'live' if facts.get('mint_authority') else 'renounced'}, "
        f"freeze {'live' if facts.get('freeze_authority') else 'renounced'}. "
        f"Top real wallet {_pct(facts.get('top_wallet_pct'))}, top-10 "
        f"{_pct(facts.get('top_10_pct'))}, curve/pool excluded at "
        f"{_pct(facts.get('program_held_pct'))}. Dev seat, your read?")

    hard = dev.get("hard_no") or []
    say("dev", "analyst",
        f"Creator {dev.get('creator') or 'unknown'} holds "
        f"{_pct(dev.get('creator_hold_pct'))}; prior rugs "
        f"{dev.get('creator_prev_rugs') if dev.get('creator_prev_rugs') is not None else 'unknown'}; "
        f"snipers {_pct(dev.get('sniper_pct'))}; LP locked "
        f"{dev.get('lp_locked')}. Dev score {dev.get('dev_score')}/100. "
        + (f"HARD NO: {', '.join(hard)}." if hard else "No hard no from me."),
        "veto" if hard else ("support" if dev.get("legit") else "warn"))

    say("analyst", "risk",
        f"Thesis on {sym}: {thesis.get('verdict')} {thesis.get('score')}/10, shape "
        f"{thesis.get('shape')}. Meme scorecard {sc['total']}/100 "
        f"({sc['verdict']}, narrative: {sc['narrative']['primary']}). "
        f"Invalidation: {thesis.get('invalidation')}. "
        f"Risk, what size will you allow?",
        "support" if thesis.get("verdict") == "valid_setup" else "warn")

    if approved:
        say("risk", "analyst",
            f"Caps: risk {_fmt_money(sizes.get('by_risk'))}, size "
            f"{_fmt_money(sizes.get('by_size'))}, pool 2% "
            f"{_fmt_money(sizes.get('by_pool'))}. Smallest wins: ticket "
            f"{_fmt_money(ticket)}. Approved. This is the maximum; the model cannot raise it.",
            "support")
        say("analyst", "risk",
            "Accepted. I am not asking for more. The invalidation stands and I expect "
            "the broker to honour it.", "support")
        say("risk", "broker",
            f"Final: fill {sym} for {_fmt_money(ticket)}. Apply stop "
            f"-{int(EXITS['stop_pct'] * 100)}%, take half at {EXITS['take_half_at']}x, "
            f"volume-decay and {EXITS['max_hold_hours']}h max hold.", "support")
    else:
        say("risk", "analyst",
            f"Veto: {reason}. Caps were risk {_fmt_money(sizes.get('by_risk'))}, size "
            f"{_fmt_money(sizes.get('by_size'))}, pool {_fmt_money(sizes.get('by_pool'))}. "
            "No ticket. Argue the thesis, not the size.", "veto")
        say("analyst", "risk",
            f"Understood. I will not override a hard no. Benching {sym} and moving on.",
            "accept")
        say("risk", "broker",
            f"Final: no fill on {sym}. Nothing to execute.", "veto")

    return msgs


# ---------- model-backed transcript ----------
def _model_debate(token: dict, facts: dict, ds: dict, dev: dict, sc: dict,
                  thesis: dict, approved: bool, ticket: float, reason: str,
                  sizes: dict) -> list[dict] | None:
    if brain.active() == "heuristic":
        return None

    sym = token.get("symbol", "?")
    brief = (f"Token {sym} ({token.get('mint')}).\n"
             f"Scanner: age {token.get('age_minutes')}m liq {token.get('liquidity_usd')} "
             f"vol1h {token.get('volume_h1')} buys {token.get('buys_h1')} sells {token.get('sells_h1')} "
             f"mcap {token.get('mcap_usd')}.\n"
             f"Chain: mint_auth={facts.get('mint_authority')} freeze_auth={facts.get('freeze_authority')} "
             f"top1={facts.get('top_wallet_pct')} top10={facts.get('top_10_pct')}.\n"
             f"Dev: creator_hold={dev.get('creator_hold_pct')} rugs={dev.get('creator_prev_rugs')} "
             f"snipers={dev.get('sniper_pct')} score={dev.get('dev_score')} hard_no={dev.get('hard_no')}.\n"
             f"Meme scorecard: {sc['total']}/100 parts={sc['parts']} narrative={sc['narrative']['primary']}.\n"
             f"Thesis: {thesis.get('verdict')} {thesis.get('score')}/10 shape={thesis.get('shape')} "
             f"red={thesis.get('red_flags')}.\n"
             f"Risk decision: approved={approved} ticket={ticket} reason={reason} sizes={sizes}.")

    text = brain.chat(
        ("You are writing the internal transcript of a trading desk's seats talking to "
         "each other. Output ONLY lines in the form `seat>seat: text`, one per line, "
         "in this order: scanner>chain, chain>dev, dev>analyst, analyst>risk, "
         "risk>analyst, analyst>risk, risk>broker. Each seat must obey its role. "
         "The risk seat has the last word on size. Never use the word 'buy' as an "
         "instruction. Keep each line under 45 words. Use the numbers given."),
        [{"role": "user", "content": brief}],
        max_tokens=min(ANALYST_MAX_TOKENS, 1200),
    )
    if not text:
        return None

    out = []
    for line in text.splitlines():
        line = line.strip().lstrip("-* ").strip()
        if ">" not in line or ":" not in line:
            continue
        head, body = line.split(":", 1)
        frm, to = [x.strip().lower() for x in head.split(">", 1)]
        if frm not in SEATS or to not in SEATS:
            continue
        stance = ("veto" if frm == "risk" and not approved and to == "broker"
                  else "support" if approved and frm == "risk" else "info")
        out.append({"from": frm, "to": to, "seat": SEATS[frm],
                    "text": body.strip(), "stance": stance})
    return out if len(out) >= 4 else None


def debate(token: dict, facts: dict, ds: dict, dev: dict, sc: dict, thesis: dict,
           approved: bool, ticket: float, reason: str, sizes: dict) -> dict:
    """Return {'messages': [...], 'backend': provider|'template'}."""
    msgs = _model_debate(token, facts, ds, dev, sc, thesis, approved, ticket, reason, sizes)
    if msgs:
        return {"messages": msgs, "backend": brain.active()}
    return {"messages": _template_debate(token, facts, ds, dev, sc, thesis,
                                         approved, ticket, reason, sizes),
            "backend": "template"}
               
