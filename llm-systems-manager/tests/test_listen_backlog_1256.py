"""#1256: both Cheroot servers are built with the configured listen backlog
instead of Cheroot's default of 5."""
from types import SimpleNamespace

import manager_mod


def test_listen_backlog_defaults_to_1024_without_the_key(monkeypatch):
    monkeypatch.setattr(manager_mod, "settings", SimpleNamespace(manager=SimpleNamespace()))
    srv = manager_mod._new_cheroot_server(("127.0.0.1", 0))
    assert srv.request_queue_size == 1024
    assert srv.requests.min == 64
    assert srv.keep_alive_conn_limit is None


def test_listen_backlog_follows_config(monkeypatch):
    monkeypatch.setattr(manager_mod.settings.manager, "listen_backlog", 256, raising=False)
    assert manager_mod._listen_backlog() == 256
    srv = manager_mod._new_cheroot_server(("127.0.0.1", 0))
    assert srv.request_queue_size == 256
    assert srv.requests.min == int(getattr(manager_mod.settings.manager, "http_threads", 64) or 64)
