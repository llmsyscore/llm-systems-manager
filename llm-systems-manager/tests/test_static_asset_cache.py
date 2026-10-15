"""#1261: serve-time ?v= content stamps on /static URLs and long-lived
Cache-Control on versioned static responses."""
import re

import manager_mod


def _client():
    c = manager_mod.app.test_client()
    with c.session_transaction() as s:
        s["auth_ok"] = True
        s["role"] = "admin"
    return c


def test_asset_stamp_is_stable_and_tracks_content(tmp_path, monkeypatch):
    monkeypatch.setattr(manager_mod, "STATIC_DIR", tmp_path)
    monkeypatch.setattr(manager_mod, "_ASSET_HASH_CACHE", {})
    (tmp_path / "js").mkdir()
    f = tmp_path / "js" / "a.js"
    f.write_bytes(b"one")
    h1 = manager_mod._asset_stamp("js/a.js")
    assert re.fullmatch(r"[0-9a-f]{10}", h1)
    assert manager_mod._asset_stamp("js/a.js") == h1
    f.write_bytes(b"two!")
    assert manager_mod._asset_stamp("js/a.js") != h1
    assert manager_mod._asset_stamp("js/missing.js") is None


def test_stamp_static_urls_rewrites_src_and_href(tmp_path, monkeypatch):
    monkeypatch.setattr(manager_mod, "STATIC_DIR", tmp_path)
    monkeypatch.setattr(manager_mod, "_ASSET_HASH_CACHE", {})
    (tmp_path / "js").mkdir(); (tmp_path / "css").mkdir()
    (tmp_path / "js" / "a.js").write_bytes(b"js")
    (tmp_path / "css" / "a.css").write_bytes(b"css")
    hj = manager_mod._asset_stamp("js/a.js")
    hc = manager_mod._asset_stamp("css/a.css")
    html = ('<link rel="stylesheet" href="/static/css/a.css?v=2026.01.01-1">'
            '<script src="/static/js/a.js"></script>'
            '<script src="/static/js/gone.js?v=old"></script>'
            '<iframe src="/alarm/?v=keep"></iframe>')
    out = manager_mod._stamp_static_urls(html)
    assert f'href="/static/css/a.css?v={hc}"' in out
    assert f'src="/static/js/a.js?v={hj}"' in out
    assert 'src="/static/js/gone.js?v=old"' in out
    assert 'src="/alarm/?v=keep"' in out


def test_index_serves_hash_stamped_static_urls():
    r = _client().get("/")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    stamps = re.findall(r'(?:src|href)="/static/[^"?]+\?v=([^"&]+)"', html)
    assert stamps, "index.html carries no /static URLs"
    assert all(re.fullmatch(r"[0-9a-f]{10}", s) for s in stamps), stamps
    assert not re.search(r'(?:src|href)="/static/(?:js|css)/[^"?]+"', html), "unstamped js/css URL"


def test_versioned_static_gets_immutable_cache():
    c = _client()
    r = c.get("/static/css/base.css?v=abc")
    assert r.status_code == 200
    assert r.headers["Cache-Control"] == "private, max-age=31536000, immutable"


def test_unversioned_js_keeps_default_cache():
    c = _client()
    r = c.get("/static/css/base.css")
    assert r.status_code == 200
    assert "immutable" not in (r.headers.get("Cache-Control") or "")


def test_fonts_and_icons_get_one_day_cache():
    c = _client()
    for p in ("/static/fonts/plex-sans-var-latin.woff2", "/static/icons/icon-192.png"):
        r = c.get(p)
        assert r.status_code == 200, p
        assert r.headers["Cache-Control"] == "private, max-age=86400", p


def test_login_redirect_on_static_is_not_cached():
    r = manager_mod.app.test_client().get("/static/css/base.css?v=abc")
    assert r.status_code == 302
    assert "immutable" not in (r.headers.get("Cache-Control") or "")
