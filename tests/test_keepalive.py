from desk import keepalive


def test_no_url_means_no_thread(monkeypatch):
    monkeypatch.delenv("RENDER_EXTERNAL_URL", raising=False)
    assert keepalive.start() is False


def test_can_be_disabled(monkeypatch):
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://x.onrender.com")
    monkeypatch.setenv("SELF_PING", "0")
    assert keepalive.start() is False


def test_url_built_from_render_env(monkeypatch):
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://x.onrender.com/")
    monkeypatch.delenv("SELF_PING", raising=False)
    assert keepalive._url() == "https://x.onrender.com/api/health"
