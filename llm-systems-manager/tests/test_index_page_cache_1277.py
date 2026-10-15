"""#1277: GET / serves a cached, pre-stamped page and applies only the
per-request pieces (hide list, globals script, brand vars)."""
import re
import time

import manager_mod

_INDEX = (
    '<html><head><link rel="stylesheet" href="/static/css/a.css"></head><body>\n'
    '<div id="serverStateBanner" class="state-banner" style="margin-left:16px;padding:2px;"></div>\n'
    '<button class="tab-btn active" id="tabBtnOverall" onclick="switchTab(1)">Overall</button>\n'
    '<button id="subTabBtnDashLmstudio">LM</button>\n'
    '<!-- id="subTabBtnLlmLmstudio" mentioned in a comment -->\n'
    '<button style="color:red" id="subTabBtnLlmLmstudio">LM</button>\n'
    '</body></html>'
)


def _client():
    c = manager_mod.app.test_client()
    with c.session_transaction() as s:
        s["auth_ok"] = True
        s["role"] = "admin"
    return c


def _static(tmp_path, monkeypatch, html=_INDEX):
    monkeypatch.setattr(manager_mod, "STATIC_DIR", tmp_path)
    monkeypatch.setattr(manager_mod, "_ASSET_HASH_CACHE", {})
    monkeypatch.setattr(manager_mod, "_PAGE_CACHE", None)
    (tmp_path / "css").mkdir(exist_ok=True)
    (tmp_path / "css" / "a.css").write_bytes(b"css")
    (tmp_path / "index.html").write_text(html, encoding="utf-8")


def _caps(monkeypatch, **vis):
    base = {"llama": True, "lms": True, "either": True}
    base.update(vis)
    monkeypatch.setattr(manager_mod.agent_registry, "approved_agent_caps", lambda: base)


def test_hide_appends_to_existing_style_and_adds_style_otherwise(tmp_path, monkeypatch):
    _static(tmp_path, monkeypatch)
    _caps(monkeypatch, llama=False, lms=False, either=False)
    html = _client().get("/").get_data(as_text=True)
    assert 'id="serverStateBanner" class="state-banner" style="margin-left:16px;padding:2px;;display:none"' in html
    assert 'id="tabBtnOverall" style="display:none" onclick="switchTab(1)"' in html
    assert 'id="subTabBtnDashLmstudio" style="display:none"' in html
    assert '<button style="color:red;display:none" id="subTabBtnLlmLmstudio">' in html
    assert '<!-- id="subTabBtnLlmLmstudio" mentioned in a comment -->' in html


def test_visible_caps_leave_tags_untouched(tmp_path, monkeypatch):
    _static(tmp_path, monkeypatch)
    _caps(monkeypatch, lms=False)
    html = _client().get("/").get_data(as_text=True)
    assert html.count("display:none") == 2
    assert 'id="subTabBtnDashLmstudio" style="display:none"' in html
    assert 'id="tabBtnOverall" onclick' in html


def test_page_cache_reused_until_index_or_asset_changes(tmp_path, monkeypatch):
    _static(tmp_path, monkeypatch)
    _caps(monkeypatch)
    c = _client()
    h1 = c.get("/").get_data(as_text=True)
    first = manager_mod._PAGE_CACHE
    c.get("/")
    assert manager_mod._PAGE_CACHE is first
    v1 = re.search(r'a\.css\?v=([0-9a-f]{10})', h1).group(1)
    # asset edit → new stamp, index.html untouched
    time.sleep(0.01)
    (tmp_path / "css" / "a.css").write_bytes(b"css2")
    h2 = c.get("/").get_data(as_text=True)
    assert manager_mod._PAGE_CACHE is not first
    assert re.search(r'a\.css\?v=([0-9a-f]{10})', h2).group(1) != v1
    # index.html edit → new page text
    second = manager_mod._PAGE_CACHE
    (tmp_path / "index.html").write_text(_INDEX.replace("Overall", "Summary"), encoding="utf-8")
    h3 = c.get("/").get_data(as_text=True)
    assert manager_mod._PAGE_CACHE is not second
    assert ">Summary<" in h3


def test_globals_and_brand_injected_per_request(tmp_path, monkeypatch):
    _static(tmp_path, monkeypatch)
    _caps(monkeypatch)
    html = _client().get("/").get_data(as_text=True)
    assert "window.__MGR_AGENT=" in html
    assert "--brand-accent:" in html
    assert "window.__MGR_AGENT=" not in manager_mod._PAGE_CACHE["html"]


def test_real_index_hides_every_listed_element(monkeypatch):
    monkeypatch.setattr(manager_mod, "_PAGE_CACHE", None)
    _caps(monkeypatch, llama=False, lms=False, either=False)
    html = _client().get("/").get_data(as_text=True)
    for el_id, _cap in manager_mod._INITIAL_HIDE_IDS:
        tag = re.search(rf'<[a-zA-Z]+[^<>]*\bid="{el_id}"[^<>]*>', html).group(0)
        assert "display:none" in tag, el_id
