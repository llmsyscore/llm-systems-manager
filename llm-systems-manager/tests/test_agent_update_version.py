"""#433: agent update offers and install topology unit detection.

update_available must only fire when the manager's local agent copy is
STRICTLY newer than the agent's reported version — a manager behind the
fleet must not offer downgrades. The AE unit must be recognized in the
package unit dir (/usr/lib/systemd/system), not just /etc/systemd/system.
"""
from __future__ import annotations

import os

import agent_registry
import manager_mod


# ── agent_update_available ──────────────────────────────────────────

def test_newer_latest_offers_update():
    assert agent_registry.agent_update_available("v2026.07.15-1", "v2026.07.15-5")


def test_equal_versions_no_update():
    assert not agent_registry.agent_update_available("v2026.07.15-5", "v2026.07.15-5")


def test_older_latest_is_not_an_update():
    # The #433 report: agent self-updated to -5, manager's copy is -1.
    assert not agent_registry.agent_update_available("v2026.07.15-5", "v2026.07.15-1")


def test_date_rollover_orders_correctly():
    assert agent_registry.agent_update_available("v2026.07.15-9", "v2026.07.16-1")
    assert not agent_registry.agent_update_available("v2026.07.16-1", "v2026.07.15-9")


def test_build_suffix_compares_numerically_not_lexically():
    assert agent_registry.agent_update_available("v2026.07.15-9", "v2026.07.15-10")


def test_missing_versions_never_offer():
    assert not agent_registry.agent_update_available(None, "v2026.07.15-5")
    assert not agent_registry.agent_update_available("v2026.07.15-5", None)
    assert not agent_registry.agent_update_available(None, None)


def test_unparseable_falls_back_to_inequality():
    assert agent_registry.agent_update_available("dev-build", "v2026.07.15-5")
    assert not agent_registry.agent_update_available("dev-build", "dev-build")


# ── install_topology: packaged unit dir counts as local ─────────────

def _topology_with_units(monkeypatch, present: set):
    real_isfile = os.path.isfile

    def fake_isfile(path):
        if str(path).endswith("llm-systems-alarm-engine.service"):
            return str(path) in present
        return real_isfile(path)

    monkeypatch.setattr(manager_mod.os.path, "isfile", fake_isfile)
    return manager_mod.install_topology()


def test_ae_unit_in_etc_counts(monkeypatch):
    topo = _topology_with_units(
        monkeypatch, {"/etc/systemd/system/llm-systems-alarm-engine.service"})
    assert topo["ae_local_unit"] is True


def test_ae_unit_in_usr_lib_counts(monkeypatch):
    # deb/rpm packages ship the unit here — the #433 missing-restart-button bug.
    topo = _topology_with_units(
        monkeypatch, {"/usr/lib/systemd/system/llm-systems-alarm-engine.service"})
    assert topo["ae_local_unit"] is True


def test_no_ae_unit_anywhere(monkeypatch):
    topo = _topology_with_units(monkeypatch, set())
    assert topo["ae_local_unit"] is False
