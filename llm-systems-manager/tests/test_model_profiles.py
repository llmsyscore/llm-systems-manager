from __future__ import annotations
import os
import pytest
import model_profiles


@pytest.fixture
def store(tmp_path):
    return model_profiles.ProfileStore(tmp_path / "model_profiles.json")


class TestPutAndRead:
    def test_put_creates_entry_and_marks_active(self, store):
        m = store.put_profile("ag1", "modelA", "default", {"ctx-size": "4096"})
        assert m["active"] == "default"
        assert m["profiles"]["default"] == {"ctx-size": "4096"}

    def test_get_model_returns_none_when_absent(self, store):
        assert store.get_model("ag1", "nope") is None

    def test_get_agent_returns_all_models(self, store):
        store.put_profile("ag1", "modelA", "default", {"a": "1"})
        store.put_profile("ag1", "modelB", "default", {"b": "2"})
        agent = store.get_agent("ag1")
        assert set(agent.keys()) == {"modelA", "modelB"}

    def test_second_profile_does_not_steal_active(self, store):
        store.put_profile("ag1", "modelA", "default", {"a": "1"})
        m = store.put_profile("ag1", "modelA", "code", {"a": "2"})
        assert m["active"] == "default"
        assert set(m["profiles"]) == {"default", "code"}

    def test_make_active_switches(self, store):
        store.put_profile("ag1", "modelA", "default", {"a": "1"})
        m = store.put_profile("ag1", "modelA", "code", {"a": "2"}, make_active=True)
        assert m["active"] == "code"

    def test_update_existing_profile_in_place_keeps_active(self, store):
        store.put_profile("ag1", "m", "default", {"a": "1"})
        store.put_profile("ag1", "m", "code", {"a": "2"})  # default stays active
        m = store.put_profile("ag1", "m", "default", {"a": "9"})  # update existing
        assert m["active"] == "default"
        assert m["profiles"]["default"] == {"a": "9"}
        assert set(m["profiles"]) == {"default", "code"}

    def test_get_agent_unknown_returns_empty(self, store):
        assert store.get_agent("nobody") == {}

    def test_persists_0600_across_instances(self, tmp_path):
        p = tmp_path / "model_profiles.json"
        model_profiles.ProfileStore(p).put_profile("ag1", "m", "default", {"x": "1"})
        assert oct(os.stat(p).st_mode & 0o777) == "0o600"
        assert model_profiles.ProfileStore(p).get_model("ag1", "m")["active"] == "default"


class TestMutations:
    def _seeded(self, store):
        store.put_profile("ag1", "m", "default", {"a": "1"})
        store.put_profile("ag1", "m", "code", {"a": "2"})
        return store

    def test_set_active(self, store):
        self._seeded(store)
        assert store.set_active("ag1", "m", "code")["active"] == "code"

    def test_set_active_unknown_raises(self, store):
        self._seeded(store)
        with pytest.raises(KeyError):
            store.set_active("ag1", "m", "ghost")

    def test_rename_preserves_active(self, store):
        self._seeded(store)
        store.set_active("ag1", "m", "code")
        m = store.rename("ag1", "m", "code", "coding")
        assert m["active"] == "coding"
        assert set(m["profiles"]) == {"default", "coding"}

    def test_rename_collision_raises(self, store):
        self._seeded(store)
        with pytest.raises(ValueError):
            store.rename("ag1", "m", "code", "default")

    def test_delete_non_active(self, store):
        self._seeded(store)
        m = store.delete("ag1", "m", "code")
        assert set(m["profiles"]) == {"default"}

    def test_delete_active_falls_back(self, store):
        self._seeded(store)  # default (active) + code
        m = store.delete("ag1", "m", "default")  # delete the active one
        assert "default" not in m["profiles"]
        assert m["active"] == "code"  # fell back to the remaining profile

    def test_delete_active_prefers_default_fallback(self, store):
        store.put_profile("ag1", "m", "default", {"a": "1"})
        store.put_profile("ag1", "m", "code", {"a": "2"})
        store.put_profile("ag1", "m", "chat", {"a": "3"}, make_active=True)
        m = store.delete("ag1", "m", "chat")  # active was chat
        assert m["active"] == "default"  # prefers "default" over "code"

    def test_delete_last_raises(self, store):
        store.put_profile("ag1", "m", "only", {"a": "1"})
        with pytest.raises(ValueError):
            store.delete("ag1", "m", "only")


class TestEndpoints:
    @pytest.fixture
    def client(self, tmp_path, monkeypatch):
        import manager_mod as M
        import model_profiles as MP
        store = MP.ProfileStore(tmp_path / "p.json")
        monkeypatch.setattr(MP, "STORE", store, raising=False)
        import agent_registry
        monkeypatch.setattr(agent_registry, "default_agent_id_for",
                            lambda p: "ag1", raising=False)
        M.app.config.update(TESTING=True)
        c = M.app.test_client()
        with c.session_transaction() as sess:
            sess["auth_ok"] = True
        return c

    def test_save_then_list(self, client):
        r = client.post("/api/llm/profiles/modelA/save",
                        json={"name": "default", "values": {"ctx-size": "4096"},
                              "make_active": True})
        assert r.status_code == 200 and r.get_json()["ok"] is True
        lst = client.get("/api/llm/profiles").get_json()
        assert lst["modelA"]["active"] == "default"

    def test_activate_unknown_404(self, client):
        client.post("/api/llm/profiles/m/save", json={"name": "default", "values": {}})
        r = client.post("/api/llm/profiles/m/activate", json={"name": "ghost"})
        assert r.status_code == 404

    def test_delete_last_profile_409(self, client):
        client.post("/api/llm/profiles/m/save", json={"name": "default", "values": {}})
        r = client.post("/api/llm/profiles/m/delete", json={"name": "default"})
        assert r.status_code == 409  # can't delete the only profile

    def test_delete_active_falls_back_200(self, client):
        client.post("/api/llm/profiles/m/save", json={"name": "default", "values": {}})
        client.post("/api/llm/profiles/m/save",
                    json={"name": "code", "values": {}, "make_active": True})
        r = client.post("/api/llm/profiles/m/delete", json={"name": "code"})  # delete active
        assert r.status_code == 200
        assert r.get_json()["model"]["active"] == "default"  # fell back

    def test_save_rejects_overlong_name_400(self, client):
        r = client.post("/api/llm/profiles/m/save",
                        json={"name": "x" * 65, "values": {}})
        assert r.status_code == 400

    def test_save_rejects_non_object_values_400(self, client):
        r = client.post("/api/llm/profiles/m/save",
                        json={"name": "default", "values": "oops"})
        assert r.status_code == 400

    def test_slash_model_id_stored_decoded(self, client):
        # Model ids contain '/'; URLs encode it as %2F. The store key MUST be the
        # decoded id so it matches the frontend's raw ids — else the seed loop
        # never finds the entry and resets active every refresh (#118 stick bug).
        client.post("/api/llm/profiles/a%2Fb/save",
                    json={"name": "default", "values": {}})
        lst = client.get("/api/llm/profiles").get_json()
        assert "a/b" in lst
        assert "a%2Fb" not in lst
