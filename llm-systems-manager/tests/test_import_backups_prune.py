"""Manager import-apply keeps only the newest _PREIMPORT_KEEP pre-import backups (#663 parity)."""
from __future__ import annotations

import os

import manager_mod


def test_import_apply_prunes_stale_preimport_backups(tmp_path, monkeypatch):
    monkeypatch.setattr(manager_mod, "_REPO_ROOT_PATH", tmp_path)
    dest = tmp_path / "config" / "llm-systems.toml"
    dest.parent.mkdir()
    dest.write_text("a = 1\n")
    for i in range(6):
        (dest.parent / f"llm-systems.toml.preimport.20260101-0000{i:02d}.bak").write_bytes(b"old")
    result = manager_mod._import_apply_manager({"config/llm-systems.toml": b"a = 2\n",
                                                "manifest.json": b"{}"})
    assert dest.read_text() == "a = 2\n"
    assert len(result["backups"]) == 1
    baks = sorted(n for n in os.listdir(dest.parent) if ".preimport." in n)
    assert len(baks) == manager_mod._PREIMPORT_KEEP
    assert os.path.basename(result["backups"][0]) == baks[-1]
    assert "20260101-000000" not in "".join(baks)
