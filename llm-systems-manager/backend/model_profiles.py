"""Per-model llama config profile library (issue #118).

Stores named config value-sets per (agent_id, model_id) in
data/model_profiles.json (0600). Pure storage — the active profile's values are
written to the agent's config.ini by the frontend via the existing
/api/llm/config endpoint, so config.ini and the active profile stay in sync.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path


class ProfileStore:
    def __init__(self, path: "Path | str") -> None:
        self._path = Path(path)
        self._lock = threading.RLock()

    def _load(self) -> dict:
        try:
            with open(self._path) as f:
                return json.load(f)
        except (FileNotFoundError, ValueError):
            return {}

    def _save(self, data: dict) -> None:
        tmp = f"{self._path}.{os.getpid()}.tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self._path)

    def get_agent(self, agent_id: str) -> dict:
        with self._lock:
            return self._load().get(agent_id, {})

    def get_model(self, agent_id: str, model_id: str):
        with self._lock:
            return self._load().get(agent_id, {}).get(model_id)

    def put_profile(self, agent_id: str, model_id: str, name: str,
                    values: dict, make_active: bool = False) -> dict:
        with self._lock:
            data = self._load()
            a = data.setdefault(agent_id, {})
            m = a.setdefault(model_id, {"active": name, "profiles": {}})
            m["profiles"][name] = dict(values)
            if make_active or m.get("active") not in m["profiles"]:
                m["active"] = name
            self._save(data)
            return m

    def set_active(self, agent_id: str, model_id: str, name: str) -> dict:
        with self._lock:
            data = self._load()
            m = data.get(agent_id, {}).get(model_id)
            if not m or name not in m["profiles"]:
                raise KeyError(name)
            m["active"] = name
            self._save(data)
            return m

    def rename(self, agent_id: str, model_id: str, old: str, new: str) -> dict:
        new = (new or "").strip()
        with self._lock:
            data = self._load()
            m = data.get(agent_id, {}).get(model_id)
            if not m or old not in m["profiles"]:
                raise KeyError(old)
            if not new or new in m["profiles"]:
                raise ValueError("name exists or empty")
            m["profiles"][new] = m["profiles"].pop(old)
            if m["active"] == old:
                m["active"] = new
            self._save(data)
            return m

    def delete(self, agent_id: str, model_id: str, name: str) -> dict:
        with self._lock:
            data = self._load()
            m = data.get(agent_id, {}).get(model_id)
            if not m or name not in m["profiles"]:
                raise KeyError(name)
            if len(m["profiles"]) <= 1:
                raise ValueError("cannot delete the last profile")
            del m["profiles"][name]
            # Deleting the active profile falls back to another (prefer default).
            if m["active"] == name:
                m["active"] = "default" if "default" in m["profiles"] else next(iter(m["profiles"]))
            self._save(data)
            return m


from urllib.parse import unquote  # noqa: E402

from flask import jsonify, request as flask_request  # noqa: E402

import agent_registry  # type: ignore[import-not-found]  # sibling

STORE: "ProfileStore | None" = None


def _target_agent_id() -> "str | None":
    return flask_request.args.get("agent") or agent_registry.default_agent_id_for("llama")


def _model_key(model_id: str) -> str:
    # WSGI leaves %2F encoded in the path, so a slash-bearing model id arrives
    # percent-encoded; decode it so the store key matches the frontend's raw id.
    return unquote(model_id)


def register_routes(app, ctx, *, profiles_path) -> None:
    global STORE
    if STORE is None:
        STORE = ProfileStore(profiles_path)

    @app.route("/api/llm/profiles", methods=["GET"])
    def llm_profiles_list():
        aid = _target_agent_id()
        return jsonify(STORE.get_agent(aid) if aid else {})

    @app.route("/api/llm/profiles/<path:model_id>/save", methods=["POST"])
    def llm_profiles_save(model_id):
        model_id = _model_key(model_id)
        aid = _target_agent_id()
        body = flask_request.get_json(force=True) or {}
        name = (body.get("name") or "").strip()
        if not aid or not name:
            return jsonify({"ok": False, "error": "agent and name required"}), 400
        if len(name) > 64:
            return jsonify({"ok": False, "error": "name too long"}), 400
        vals = body.get("values") or {}
        if not isinstance(vals, dict):
            return jsonify({"ok": False, "error": "values must be an object"}), 400
        m = STORE.put_profile(aid, model_id, name, vals,
                              make_active=bool(body.get("make_active")))
        return jsonify({"ok": True, "model": m})

    @app.route("/api/llm/profiles/<path:model_id>/activate", methods=["POST"])
    def llm_profiles_activate(model_id):
        model_id = _model_key(model_id)
        aid = _target_agent_id()
        if not aid:
            return jsonify({"ok": False, "error": "no agent resolved"}), 400
        name = ((flask_request.get_json(force=True) or {}).get("name") or "").strip()
        try:
            m = STORE.set_active(aid, model_id, name)
        except KeyError:
            return jsonify({"ok": False, "error": "no such profile"}), 404
        return jsonify({"ok": True, "model": m, "values": m["profiles"][name]})

    @app.route("/api/llm/profiles/<path:model_id>/rename", methods=["POST"])
    def llm_profiles_rename(model_id):
        model_id = _model_key(model_id)
        aid = _target_agent_id()
        if not aid:
            return jsonify({"ok": False, "error": "no agent resolved"}), 400
        b = flask_request.get_json(force=True) or {}
        to = (b.get("to") or "").strip()
        if len(to) > 64:
            return jsonify({"ok": False, "error": "name too long"}), 400
        try:
            m = STORE.rename(aid, model_id, (b.get("from") or "").strip(), to)
        except KeyError:
            return jsonify({"ok": False, "error": "no such profile"}), 404
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 409
        return jsonify({"ok": True, "model": m})

    @app.route("/api/llm/profiles/<path:model_id>/delete", methods=["POST"])
    def llm_profiles_delete(model_id):
        model_id = _model_key(model_id)
        aid = _target_agent_id()
        if not aid:
            return jsonify({"ok": False, "error": "no agent resolved"}), 400
        name = ((flask_request.get_json(force=True) or {}).get("name") or "").strip()
        try:
            m = STORE.delete(aid, model_id, name)
        except KeyError:
            return jsonify({"ok": False, "error": "no such profile"}), 404
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 409
        return jsonify({"ok": True, "model": m})
