"""The brain: one interface, many models, free ones first.

The desk does not care who is thinking. It cares that the answer arrives in the
same typed shape every time, so the rest of the pipeline cannot tell a frontier
model from a 3B model running on your laptop.

Providers, cheapest first:

    ollama      local, no key, no limits, no data leaves your machine
    groq        free tier, very fast, needs a free API key
    gemini      Google free tier, needs a free API key
    openrouter  free ":free" models, needs a free API key
    anthropic   Claude, paid
    heuristic   no model at all, deterministic rules (always available)

Pick one with DESK_BRAIN=groq. With nothing set, the desk uses a local Ollama if it is
running, else the heuristic. Cloud keys are only auto-detected with DESK_BRAIN_AUTODETECT=1.

Nothing here can size a trade. This module returns text; the code decides.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

import requests

TIMEOUT = int(os.getenv("DESK_BRAIN_TIMEOUT", "60"))


@dataclass
class Provider:
    key: str
    label: str
    model: str
    env: str | None          # env var holding the API key (None = no key needed)
    free: bool
    note: str


PROVIDERS: dict[str, Provider] = {
    "ollama": Provider(
        "ollama", "Ollama (local)", os.getenv("OLLAMA_MODEL", "llama3.2"),
        None, True,
        "runs on your machine, no key, no limits, nothing leaves your computer",
    ),
    "groq": Provider(
        # The default used to be llama-3.3-70b-versatile, which Groq has since
        # retired: a live call now answers 404 "model not found", so the analyst
        # seat failed on every request even with a valid key. gpt-oss-120b is the
        # same model the advisory layer uses (brain_advisor.GROQ_MODEL), so one
        # BRAIN_GROQ_MODEL override now moves both seats together.
        "groq", "Groq",
        os.getenv("BRAIN_GROQ_MODEL", os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")),
        "GROQ_API_KEY", True,
        "free tier, extremely fast, generous rate limit",
    ),
    "gemini": Provider(
        "gemini", "Google Gemini", os.getenv("GEMINI_MODEL", "gemini-3.6-flash"),
        "GEMINI_API_KEY", True,
        "free tier, good at structured output",
    ),
    "openrouter": Provider(
        "openrouter", "OpenRouter", os.getenv("OPENROUTER_MODEL", "nvidia/nemotron-3.5-super-vl:free"),
        "OPENROUTER_API_KEY", True,
        "free models with a ':free' suffix, one key for many models",
    ),
    "anthropic": Provider(
        "anthropic", "Claude", os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514"),
        "ANTHROPIC_API_KEY", False,
        "paid, strongest reasoning",
    ),
}


# ---------- which provider is active ----------
def _ollama_up() -> bool:
    try:
        r = requests.get(os.getenv("OLLAMA_HOST", "http://localhost:11434") + "/api/tags",
                         timeout=2)
        return r.status_code == 200
    except Exception:
        return False


def active() -> str:
    """Resolve the provider to use. Explicit env wins, then auto-detect."""
    forced = os.getenv("DESK_BRAIN", "").strip().lower()
    if forced in PROVIDERS:
        return forced
    if forced in ("heuristic", "none", "off"):
        return "heuristic"
    if _ollama_up():
        return "ollama"
    # A cloud key alone no longer switches the analyst/dialogue seats to a model: the
    # GEMINI/GROQ keys now feed the advisory-only second opinion (desk/brain_advisor.py), and
    # they must not change the analyst's score, which gates trades. Opt in explicitly with
    # DESK_BRAIN=<provider> or DESK_BRAIN_AUTODETECT=1.
    if os.getenv("DESK_BRAIN_AUTODETECT", "").strip().lower() in ("1", "true", "yes"):
        for name in ("groq", "gemini", "openrouter", "anthropic"):
            if os.getenv(PROVIDERS[name].env or "", "").strip():
                return name
    return "heuristic"


def describe() -> str:
    name = active()
    if name == "heuristic":
        return "heuristic (no model configured)"
    p = PROVIDERS[name]
    return f"{p.label} \u00b7 {p.model}"


# ---------- raw calls ----------
def _post(url: str, *, headers=None, json_body=None, timeout=TIMEOUT):
    r = requests.post(url, headers=headers or {}, json=json_body, timeout=timeout)
    if r.status_code >= 400:
        raise RuntimeError(f"{r.status_code}: {r.text[:300]}")
    return r.json()


def _ollama(system: str, messages: list[dict], max_tokens: int) -> str:
    host = os.getenv("OLLAMA_HOST", "http://localhost:11434")
    body = {
        "model": PROVIDERS["ollama"].model,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.2, "num_predict": max_tokens},
        "messages": [{"role": "system", "content": system}] + messages,
    }
    out = _post(f"{host}/api/chat", json_body=body)
    return out.get("message", {}).get("content", "")


def _openai_compatible(base: str, key: str, model: str, system: str,
                       messages: list[dict], max_tokens: int) -> str:
    out = _post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json_body={
            "model": model,
            "temperature": 0.2,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system}] + messages,
        },
    )
    return out["choices"][0]["message"]["content"]


def _gemini(system: str, messages: list[dict], max_tokens: int) -> str:
    key = os.getenv("GEMINI_API_KEY", "")
    model = PROVIDERS["gemini"].model
    contents = [
        {"role": "user" if m["role"] == "user" else "model", "parts": [{"text": m["content"]}]}
        for m in messages
    ]
    out = _post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
        json_body={
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": contents,
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": max_tokens,
                "responseMimeType": "application/json",
            },
        },
    )
    return out["candidates"][0]["content"]["parts"][0]["text"]


def _anthropic(system: str, messages: list[dict], max_tokens: int) -> str:
    out = _post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": os.getenv("ANTHROPIC_API_KEY", ""),
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        json_body={
            "model": PROVIDERS["anthropic"].model,
            "max_tokens": max_tokens,
            "temperature": 0.2,
            "system": system,
            "messages": messages,
        },
    )
    return "".join(b.get("text", "") for b in out.get("content", []))


def chat(system: str, messages: list[dict], max_tokens: int = 1500) -> str:
    """Send a prompt to whichever provider is active. Raises on failure."""
    name = active()
    if name == "ollama":
        return _ollama(system, messages, max_tokens)
    if name == "groq":
        return _openai_compatible("https://api.groq.com/openai/v1",
                                  os.getenv("GROQ_API_KEY", ""),
                                  PROVIDERS["groq"].model, system, messages, max_tokens)
    if name == "openrouter":
        return _openai_compatible("https://openrouter.ai/api/v1",
                                  os.getenv("OPENROUTER_API_KEY", ""),
                                  PROVIDERS["openrouter"].model, system, messages, max_tokens)
    if name == "gemini":
        return _gemini(system, messages, max_tokens)
    if name == "anthropic":
        return _anthropic(system, messages, max_tokens)
    raise RuntimeError("no model configured")


# ---------- structured output ----------
_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


def extract_json(text: str) -> dict:
    """Pull a JSON object out of whatever the model wrapped it in."""
    if not text:
        raise ValueError("empty response")
    m = _JSON_BLOCK.search(text)
    if m:
        return json.loads(m.group(1))
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in response")
    return json.loads(text[start:end + 1])


def structured(system: str, messages: list[dict], schema, max_tokens: int = 1500):
    """Ask for JSON, parse it, validate it against a pydantic model.

    Returns None on any failure so callers can fall back to the heuristic.
    A model outage must never stall the desk.
    """
    try:
        raw = chat(system, messages, max_tokens)
        data = extract_json(raw)
        return schema.model_validate(data)
    except Exception:
        return None


def health() -> dict:
    """What the dashboard shows about the brain."""
    name = active()
    p = PROVIDERS.get(name)
    return {
        "provider": name,
        "label": describe(),
        "model": p.model if p else None,
        "free": bool(p.free) if p else True,
        "needs_key": bool(p.env) if p else False,
        "key_present": bool(os.getenv(p.env or "", "").strip()) if p and p.env else True,
    }
