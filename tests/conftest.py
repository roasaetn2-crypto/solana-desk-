"""Tests never touch the optional free data sources unless a test mocks them."""
import os

os.environ.setdefault("DESK_OFFLINE", "1")

# The box/Render env may hold a real CoinGecko Demo key; tests must never use it.
os.environ.pop("COINGECKO_DEMO_API_KEY", None)

# Advisory AI keys must never be picked up by tests; they mock HTTP and set fake keys themselves.
os.environ.pop("GEMINI_API_KEY", None)
os.environ.pop("GROQ_API_KEY", None)
os.environ.pop("OPENROUTER_API_KEY", None)
