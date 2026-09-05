"""Per-user dashboard layouts (#870): a signed-in user reads and writes
their own layout document; bypass / anonymous requests keep the shared file."""
from __future__ import annotations

import json

import pytest

import auth
import manager_mod as M


@pytest.fixture
def layouts(tmp_path, monkeypatch):
    shared = tmp_path / "layout.json"
    shared.write_text(json.dumps({"cols": 3, "theme": "graphite"}))
    monkeypatch.setattr(M, "LAYOUT_FILE", shared)
    monkeypatch.setattr(M, "LAYOUTS_DIR", tmp_path / "layouts")
    monkeypatch.setattr(auth, "auth_mode", lambda: "disabled")
    return tmp_path


def _client(user=None):
    c = M.app.test_client()
    if user is not None:
        with c.session_transaction() as s:
            s["auth_ok"] = True
            s["user"] = user
            s["role"] = "admin"
    return c


def test_anonymous_uses_shared_file(layouts):
    c = _client()
    assert c.get("/api/layout").get_json()["cols"] == 3
    assert c.post("/api/layout", json={"cols": 5}).status_code == 200
    assert json.loads((layouts / "layout.json").read_text()) == {"cols": 5}
    assert not (layouts / "layouts").exists()


def test_first_sign_in_seeds_from_shared_file(layouts):
    c = _client("alice")
    assert c.get("/api/layout").get_json() == {"cols": 3, "theme": "graphite"}
    assert not (layouts / "layouts" / "alice.json").exists()


def test_users_hold_independent_layouts(layouts):
    a, b = _client("alice"), _client("bob")
    assert a.post("/api/layout", json={"cols": 4, "theme": "oled"}).status_code == 200
    assert a.get("/api/layout").get_json() == {"cols": 4, "theme": "oled"}
    assert b.get("/api/layout").get_json() == {"cols": 3, "theme": "graphite"}
    assert b.post("/api/layout", json={"theme": "frost"}).status_code == 200
    assert a.get("/api/layout").get_json() == {"cols": 4, "theme": "oled"}
    assert b.get("/api/layout").get_json() == {"theme": "frost"}
    assert json.loads((layouts / "layout.json").read_text()) == {"cols": 3, "theme": "graphite"}
    assert (layouts / "layouts" / "alice.json").is_file()
    assert (layouts / "layouts" / "bob.json").is_file()


def test_shared_file_unchanged_by_signed_in_user(layouts):
    _client("alice").post("/api/layout", json={"cols": 6})
    assert _client().get("/api/layout").get_json()["cols"] == 3


@pytest.mark.parametrize("bad", ["", "..", "Al ice", "../etc", "x" * 33])
def test_unsafe_session_username_falls_back_to_shared(layouts, bad):
    c = _client(bad)
    assert c.post("/api/layout", json={"cols": 7}).status_code == 200
    assert json.loads((layouts / "layout.json").read_text()) == {"cols": 7}
    assert not (layouts / "layouts").exists()


def test_legacy_theme_mapped_per_user(layouts):
    (layouts / "layouts").mkdir()
    (layouts / "layouts" / "carol.json").write_text(json.dumps({"theme": "classic"}))
    assert _client("carol").get("/api/layout").get_json()["theme"] == "oled"


def test_delete_user_layout_removes_only_that_file(layouts):
    _client("alice").post("/api/layout", json={"cols": 1})
    _client("bob").post("/api/layout", json={"cols": 2})
    M.delete_user_layout("alice")
    M.delete_user_layout("nobody")
    assert not (layouts / "layouts" / "alice.json").exists()
    assert (layouts / "layouts" / "bob.json").is_file()


def test_export_includes_per_user_layouts(layouts, monkeypatch):
    monkeypatch.setattr(M, "_REPO_ROOT_PATH", layouts)
    (layouts / "data").mkdir()
    (layouts / "data" / "layout.json").write_text("{}")
    (layouts / "data" / "layouts").mkdir()
    (layouts / "data" / "layouts" / "alice.json").write_text('{"cols": 4}')
    (layouts / "data" / "layouts" / "notes.txt").write_text("x")
    files = M._build_manager_archive()
    assert files["data/layouts/alice.json"] == b'{"cols": 4}'
    assert "data/layouts/notes.txt" not in files
    assert M._file_category("data/layouts/alice.json") == "config"


@pytest.mark.parametrize("name", ["data/layouts/../x.json", "data/layouts/A.json",
                                  "data/layouts/x.txt", "data/layouts/x/y.json"])
def test_import_rejects_unsafe_layout_entries(layouts, monkeypatch, name):
    monkeypatch.setattr(M, "_REPO_ROOT_PATH", layouts)
    result = M._import_apply_manager({name: b"{}", "manifest.json": b"{}"})
    assert result["written"] == []
    assert M._file_category(name) is None


def test_import_writes_per_user_layout(layouts, monkeypatch):
    monkeypatch.setattr(M, "_REPO_ROOT_PATH", layouts)
    result = M._import_apply_manager({"data/layouts/alice.json": b'{"cols": 9}',
                                      "manifest.json": b"{}"})
    assert len(result["written"]) == 1
    assert (layouts / "data" / "layouts" / "alice.json").read_text() == '{"cols": 9}'
