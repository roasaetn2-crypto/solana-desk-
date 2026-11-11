"""Tests never touch the optional free data sources unless a test mocks them."""
import os

os.environ.setdefault("DESK_OFFLINE", "1")
