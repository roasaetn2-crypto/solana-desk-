"""Tests never touch the optional free data sources unless a test mocks them."""
import os

os.environ.setdefault("DESK_OFFLINE", "1")

# The box/Render env may hold a real CoinGecko Demo key; tests must never use it.
os.environ.pop("COINGECKO_DEMO_API_KEY", None)
