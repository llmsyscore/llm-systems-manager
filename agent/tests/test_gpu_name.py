# agent/tests/test_gpu_name.py
"""#624: the AMD GPU name must be the lspci Device (model) field, not the
vendor field ("Advanced Micro Devices, Inc. [AMD/ATI]")."""
from __future__ import annotations

from pathlib import Path

from test_collector_reprobe import gpu  # shared module loader

_LSPCI_VMM = (
    "Slot:\t01:00.0\n"
    "Class:\tVGA compatible controller\n"
    "Vendor:\tAdvanced Micro Devices, Inc. [AMD/ATI]\n"
    "Device:\tNavi 31 [Radeon RX 7900 XT/7900 XTX/7900 GRE/7900M]\n"
    "SVendor:\tSapphire Technology Limited\n"
    "SDevice:\tPULSE RX 7900 XT\n"
    "Rev:\tc8\n"
)


def _run_lspci_name(monkeypatch, out, calls=None):
    monkeypatch.setattr(gpu, "_GPU_PATH", Path("/sys/class/drm/card0/device"))
    monkeypatch.setattr(gpu.shutil, "which", lambda name: "/usr/bin/lspci")
    monkeypatch.setattr(
        gpu.subprocess, "check_output",
        lambda cmd, **k: (calls.append(cmd) if calls is not None else None) or out)
    return gpu._lspci_amd_name()


def test_amd_name_is_the_device_field_not_the_vendor(monkeypatch):
    name = _run_lspci_name(monkeypatch, _LSPCI_VMM)
    assert name == "Navi 31 [Radeon RX 7900 XT/7900 XTX/7900 GRE/7900M]"
    assert "Advanced Micro Devices" not in name


def test_amd_name_none_when_device_key_missing_or_blank(monkeypatch):
    assert _run_lspci_name(monkeypatch, "Slot:\t01:00.0\nVendor:\tAMD\n") is None
    assert _run_lspci_name(monkeypatch, "Slot:\t01:00.0\nDevice:\t\n") is None


def test_lspci_invoked_with_keyed_vmm_format(monkeypatch):
    calls: list = []
    _run_lspci_name(monkeypatch, _LSPCI_VMM, calls)
    assert "-vmm" in calls[0] and "-nn" not in calls[0]
