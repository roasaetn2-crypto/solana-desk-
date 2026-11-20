"""Live trade stream for open positions: parsing, the price overlay, subscriptions, reconnects."""
import json
import threading
import time

import pytest

import config
from desk import chart, fees, live_feed, scanner

MINT = "So11111111111111111111111111111111111111112"
MINT2 = "Mint2222222222222222222222222222222222222222"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    live_feed.reset()
    live_feed._stop.clear()
    monkeypatch.setitem(config.FEED, "trade_stream", True)
    monkeypatch.setattr(fees, "sol_price_usd", lambda: 100.0)
    chart._TICKS.clear()
    scanner._QUOTE_CACHE.clear()
    yield
    live_feed._stop.set()
    live_feed.reset()
    chart._TICKS.clear()
    scanner._QUOTE_CACHE.clear()


def _trade(mint=MINT, cap=30.0, **kw):
    m = {"txType": "buy", "mint": mint, "marketCapSol": cap, "solAmount": 0.5, "tokenAmount": 1000,
         "name": "IGNORED", "symbol": "IGN"}
    m.update(kw)
    return json.dumps(m)


def test_off_by_default():
    # the fixture turns it on for the other tests; the SHIPPED default must be off
    assert '"trade_stream": False' in open("config.py").read()


def test_price_is_market_cap_over_supply_in_usd():
    live_feed.set_mints([MINT])
    assert live_feed.ingest(_trade(cap=30.0), now=1000.0) is True
    # 30 SOL / 1B supply = 3e-8 SOL; x $100 = 3e-6 USD
    assert live_feed.last_price(MINT, now=1001.0) == pytest.approx(3e-6)
    assert chart.ticks(MINT)[-1][1] == pytest.approx(3e-6)        # the candle store got the tick
    assert chart.ticks(MINT)[-1][2] == pytest.approx(50.0)        # 0.5 SOL traded x $100


def test_falls_back_to_the_trades_own_ratio():
    live_feed.set_mints([MINT])
    live_feed.ingest(_trade(cap=0, solAmount=1.0, tokenAmount=1_000_000), now=1000.0)
    assert live_feed.last_price(MINT, now=1000.5) == pytest.approx(1e-4)   # 1 SOL / 1M tokens x $100


def test_only_held_coins_and_only_trades_are_used():
    live_feed.set_mints([MINT])
    assert live_feed.ingest(_trade(mint=MINT2), now=1.0) is False          # not held
    assert live_feed.ingest(_trade(txType="create"), now=1.0) is False     # not a trade
    assert live_feed.ingest("not json", now=1.0) is False
    assert live_feed.ingest("x" * 70_000, now=1.0) is False
    assert live_feed.ingest(json.dumps([1, 2]), now=1.0) is False
    assert live_feed.last_price(MINT, now=2.0) is None


def test_a_stale_price_is_ignored_and_off_means_none(monkeypatch):
    live_feed.set_mints([MINT])
    live_feed.ingest(_trade(), now=1000.0)
    assert live_feed.last_price(MINT, now=1100.0) is None                  # older than 15s
    live_feed.ingest(_trade(), now=2000.0)
    monkeypatch.setitem(config.FEED, "trade_stream", False)
    assert live_feed.last_price(MINT, now=2000.5) is None                  # switched off


def test_set_mints_drops_closed_positions_and_caps_the_list():
    live_feed.set_mints([MINT])
    live_feed.ingest(_trade(), now=time.time())
    live_feed.set_mints([MINT2])
    assert live_feed.last_price(MINT) is None                               # closed: forgotten
    many = [f"M{i:03d}" + "1" * 38 for i in range(60)]
    live_feed.set_mints(many)
    assert live_feed.health()["wanted"] == live_feed.MAX_MINTS


def test_quote_many_overlays_only_the_price_and_keeps_liquidity(monkeypatch):
    base = {"price_usd": 1e-6, "liquidity_usd": 55_000.0, "volume_h24": 9.0, "liq_source": "dexscreener"}
    scanner._QUOTE_CACHE[MINT] = {"exp": time.time() + 60, "q": dict(base)}
    live_feed.set_mints([MINT])
    live_feed.ingest(_trade(cap=60.0), now=time.time())
    out = scanner.quote_many([MINT], None)
    assert out[MINT]["price_usd"] == pytest.approx(6e-6)                    # the streamed price
    assert out[MINT]["liquidity_usd"] == 55_000.0 and out[MINT]["live"] is True


def test_quote_many_is_untouched_when_the_stream_is_off(monkeypatch):
    base = {"price_usd": 1e-6, "liquidity_usd": 55_000.0}
    scanner._QUOTE_CACHE[MINT] = {"exp": time.time() + 60, "q": dict(base)}
    live_feed.set_mints([MINT])
    live_feed.ingest(_trade(cap=60.0), now=time.time())
    monkeypatch.setitem(config.FEED, "trade_stream", False)
    assert scanner.quote_many([MINT], None)[MINT]["price_usd"] == 1e-6


# ---------- the connection loop, against a fake websocket module ----------
class _Timeout(Exception):
    pass


class _FakeWS:
    def __init__(self, script, sent):
        self.script, self.sent = list(script), sent
        self.pinged = 0

    def settimeout(self, _):
        pass

    def send(self, data):
        self.sent.append(json.loads(data))

    def ping(self):
        self.pinged += 1

    def recv(self):
        if not self.script:
            time.sleep(0.01)
            raise _Timeout()
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        pass


def _fake_module(connections, sent):
    class M:
        WebSocketTimeoutException = _Timeout

        @staticmethod
        def create_connection(url, timeout=None):
            if not connections:
                raise OSError("no more connections")
            return _FakeWS(connections.pop(0), sent)
    return M


def _run_loop(monkeypatch, connections, sent, seconds=0.6):
    monkeypatch.setattr(live_feed, "_ws_mod", lambda: _fake_module(connections, sent))
    monkeypatch.setattr(live_feed, "BACKOFF_START_S", 0.01)
    monkeypatch.setattr(live_feed, "RECV_TIMEOUT_S", 0.01)
    t = threading.Thread(target=live_feed._run, daemon=True)
    t.start()
    time.sleep(seconds)
    live_feed._stop.set()
    t.join(2)


def test_loop_subscribes_to_held_coins_and_streams_prices(monkeypatch):
    sent = []
    live_feed.set_mints([MINT])
    _run_loop(monkeypatch, [[_trade(cap=30.0)]], sent)
    assert {"method": "subscribeTokenTrade", "keys": [MINT]} in sent
    assert all(m["method"] in ("subscribeTokenTrade", "unsubscribeTokenTrade") for m in sent)
    assert live_feed.last_price(MINT, now=time.time()) == pytest.approx(3e-6)


def test_loop_reconnects_and_resubscribes_after_a_drop(monkeypatch):
    sent = []
    live_feed.set_mints([MINT])
    _run_loop(monkeypatch, [[OSError("boom")], [_trade()]], sent, seconds=0.8)
    subs = [m for m in sent if m["method"] == "subscribeTokenTrade"]
    assert len(subs) >= 2                                  # subscribed again on the new connection
    assert live_feed.health()["reconnects"] >= 1


def test_loop_syncs_new_and_closed_positions_while_connected(monkeypatch):
    sent = []
    live_feed.set_mints([MINT])

    def later():
        time.sleep(0.15)
        live_feed.set_mints([MINT2])                       # MINT sold, MINT2 bought
    threading.Thread(target=later, daemon=True).start()
    _run_loop(monkeypatch, [[]], sent, seconds=0.7)
    assert {"method": "unsubscribeTokenTrade", "keys": [MINT]} in sent
    assert {"method": "subscribeTokenTrade", "keys": [MINT2]} in sent


def test_no_connection_is_opened_while_off_or_nothing_is_held(monkeypatch):
    opened = []
    monkeypatch.setattr(live_feed, "_ws_mod", lambda: type("M", (), {
        "WebSocketTimeoutException": _Timeout,
        "create_connection": staticmethod(lambda *a, **k: opened.append(1))}))
    live_feed.set_mints([])                                 # nothing held
    t = threading.Thread(target=live_feed._run, daemon=True)
    t.start(); time.sleep(0.2); live_feed._stop.set(); t.join(3)
    assert opened == []


def test_setting_exists_and_toggles_the_config():
    from desk import settings
    s = settings.resolve("feed.trade_stream")
    s.set(False)
    assert config.FEED["trade_stream"] is False
    s.set(True)
    assert config.FEED["trade_stream"] is True
