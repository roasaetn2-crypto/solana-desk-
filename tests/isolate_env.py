"""Tests never pick up live API keys from the box or Render env."""
import os

os.environ.setdefault("DESK_OFFLINE", "1")
# CoinGecko Demo, Gemini, Groq and OpenRouter keys must never be used by tests;
# they mock HTTP and, when needed, set fake keys themselves.
for _name in ("COINGECKO_DEMO_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY"):
    os.environ.pop(_name, None)
