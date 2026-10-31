# agent/tests/test_gpu_device_select.py
"""#1304: AMD card selection (GPU_DEVICE, else the card with the most VRAM)
and GTT memory reporting from amdgpu sysfs."""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

_AGENT_ROOT = Path(__file__).resolve().parent.parent

if "collectors" not in sys.modules:
    _pkg = types.ModuleType("collectors")
    _pkg.__path__ = [str(_AGENT_ROOT / "collectors")]
    sys.modules["collectors"] = _pkg


def _load(name: str, path: Path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_load("collectors._shared", _AGENT_ROOT / "collectors" / "_shared.py")
gpu = _load("collectors.gpu", _AGENT_ROOT / "collectors" / "gpu.py")


def _card(root: Path, name: str, pci: str, vendor: str = "0x1002",
          vram_total: int | None = None, **files: str) -> Path:
    dev = root / "devices" / pci
    dev.mkdir(parents=True)
    (dev / "vendor").write_text(vendor + "\n")
    if vram_total is not None:
        (dev / "mem_info_vram_total").write_text(f"{vram_total}\n")
    for fname, val in files.items():
        (dev / fname).write_text(val + "\n")
    card = root / "drm" / name
    card.mkdir(parents=True)
    (card / "device").symlink_to(dev)
    return dev


@pytest.fixture
def sysfs(tmp_path, monkeypatch):
    monkeypatch.setattr(gpu, "_deps", types.SimpleNamespace(
        config=types.SimpleNamespace(GPU_DEVICE="")))
    return tmp_path


def _select(root: Path, sel: str = ""):
    gpu._deps.config.GPU_DEVICE = sel
    found = gpu._find_amd_gpu_path(root / "drm")
    return found.resolve() if found is not None else None


def test_igpu_plus_dgpu_defaults_to_the_card_with_most_vram(sysfs):
    _card(sysfs, "card0", "0000:7c:00.0", vram_total=512 << 20)
    dgpu = _card(sysfs, "card1", "0000:03:00.0", vram_total=24 << 30)
    assert _select(sysfs) == dgpu.resolve()


def test_single_amd_card_is_picked_without_a_setting(sysfs):
    only = _card(sysfs, "card0", "0000:03:00.0")
    _card(sysfs, "card1", "0000:01:00.0", vendor="0x10de", vram_total=48 << 30)
    assert _select(sysfs) == only.resolve()


def test_connector_entries_are_ignored(sysfs):
    dgpu = _card(sysfs, "card1", "0000:03:00.0", vram_total=1)
    (sysfs / "drm" / "card1-DP-1").mkdir()
    assert _select(sysfs) == dgpu.resolve()


@pytest.mark.parametrize("sel", ["card0", "0000:7c:00.0", "7c:00.0", "0000:7C:00.0", "00000000:7c:00.0"])
def test_gpu_device_by_card_name_or_pci_address(sysfs, sel):
    igpu = _card(sysfs, "card0", "0000:7c:00.0", vram_total=512 << 20)
    _card(sysfs, "card1", "0000:03:00.0", vram_total=24 << 30)
    assert _select(sysfs, sel) == igpu.resolve()


def test_gpu_device_by_sysfs_path(sysfs):
    _card(sysfs, "card0", "0000:7c:00.0", vram_total=24 << 30)
    dgpu = _card(sysfs, "card1", "0000:03:00.0", vram_total=512 << 20)
    assert _select(sysfs, str(sysfs / "drm" / "card1")) == dgpu.resolve()
    assert _select(sysfs, str(dgpu)) == dgpu.resolve()


def test_gpu_device_matching_no_amd_card_selects_none(sysfs):
    _card(sysfs, "card0", "0000:03:00.0", vram_total=1)
    assert _select(sysfs, "card7") is None


def test_unmatched_gpu_device_warns_once(monkeypatch, caplog):
    monkeypatch.setattr(gpu, "_GPU_DEVICE_WARNED", set())
    with caplog.at_level("DEBUG"):
        gpu._warn_unmatched_device("card7")
        gpu._warn_unmatched_device("card7")
    levels = [r.levelname for r in caplog.records if "matches no AMD or NVIDIA" in r.message]
    assert levels == ["WARNING", "DEBUG"]


_NV_LIST = ("0, 00000000:01:00.0, GPU-aaaa, 8192\n"
            "1, 00000000:02:00.0, GPU-bbbb, 24576\n")


def _nv(monkeypatch, sel="", out=_NV_LIST, fail=False):
    gpu._deps.config.GPU_DEVICE = sel
    def fake(cmd, **kw):
        if fail:
            raise OSError("nvidia-smi missing")
        return out
    monkeypatch.setattr(gpu.subprocess, "check_output", fake)


def test_nvidia_default_is_the_card_with_most_memory(sysfs, monkeypatch):
    _nv(monkeypatch)
    assert gpu._select_nvidia_id(sysfs / "drm") == "00000000:02:00.0"


@pytest.mark.parametrize("sel", ["0", "GPU-AAAA", "01:00.0", "0000:01:00.0", "card0"])
def test_nvidia_selected_by_index_uuid_pci_or_card(sysfs, monkeypatch, sel):
    _card(sysfs, "card0", "0000:01:00.0", vendor="0x10de")
    _nv(monkeypatch, sel)
    assert gpu._select_nvidia_id(sysfs / "drm") == "00000000:01:00.0"


def test_nvidia_selected_by_drm_card_sysfs_path(sysfs, monkeypatch):
    _card(sysfs, "card0", "0000:01:00.0", vendor="0x10de")
    _nv(monkeypatch, str(sysfs / "drm" / "card0"))
    assert gpu._select_nvidia_id(sysfs / "drm") == "00000000:01:00.0"


def test_nvidia_list_failure_with_selector_is_none(sysfs, monkeypatch):
    _nv(monkeypatch, "0", fail=True)
    assert gpu._select_nvidia_id(sysfs / "drm") is None


def _probe(monkeypatch, amd, nv_present, nv_id):
    for k in ("_GPU_PATH", "_HWMON", "_NVIDIA_PRESENT", "_AMD_NAME", "_NV_NAME"):
        monkeypatch.setattr(gpu, k, gpu._UNPROBED)
    monkeypatch.setattr(gpu, "_NV_ID", "")
    monkeypatch.setattr(gpu, "_find_amd_gpu_path", lambda: amd)
    monkeypatch.setattr(gpu, "_hwmon_dir", lambda: None)
    monkeypatch.setattr(gpu, "_find_nvidia_gpu", lambda: nv_present)
    monkeypatch.setattr(gpu, "_select_nvidia_id", lambda: nv_id)
    monkeypatch.setattr(gpu, "_lspci_amd_name", lambda: None)
    monkeypatch.setattr(gpu, "_nvidia_smi_name", lambda: "RTX")
    monkeypatch.setattr(gpu, "_probe_driver", lambda: None)
    monkeypatch.setattr(gpu._probe_latch, "should_probe", lambda: True)
    gpu._ensure_probed()


def test_probe_amd_found_skips_nvidia(sysfs, monkeypatch):
    _probe(monkeypatch, Path("/amd"), True, "00000000:01:00.0")
    assert gpu._GPU_PATH == Path("/amd") and gpu._NVIDIA_PRESENT is False


def test_probe_selector_naming_nvidia_on_mixed_host(sysfs, monkeypatch):
    gpu._deps.config.GPU_DEVICE = "0"
    _probe(monkeypatch, None, True, "00000000:01:00.0")
    assert gpu._NVIDIA_PRESENT is True and gpu._NV_ID == "00000000:01:00.0"


def test_probe_selector_matching_nothing_warns(sysfs, monkeypatch, caplog):
    monkeypatch.setattr(gpu, "_GPU_DEVICE_WARNED", set())
    gpu._deps.config.GPU_DEVICE = "card9"
    _probe(monkeypatch, None, True, None)
    assert gpu._NVIDIA_PRESENT is False
    assert "matches no AMD or NVIDIA card" in caplog.text


def test_nvidia_selection_matching_nothing_is_none(sysfs, monkeypatch):
    _nv(monkeypatch, "5")
    assert gpu._select_nvidia_id(sysfs / "drm") is None


def test_nvidia_list_failure_falls_back_to_default_card(sysfs, monkeypatch):
    _nv(monkeypatch, fail=True)
    assert gpu._select_nvidia_id(sysfs / "drm") == ""
    monkeypatch.setattr(gpu, "_NV_ID", "")
    assert gpu._nv_id_args() == []
    monkeypatch.setattr(gpu, "_NV_ID", "00000000:02:00.0")
    assert gpu._nv_id_args() == ["-i", "00000000:02:00.0"]


def test_gtt_reported_when_readable(sysfs, monkeypatch):
    dev = _card(sysfs, "card0", "0000:03:00.0",
                mem_info_gtt_used="1073741824", mem_info_gtt_total="4294967296")
    monkeypatch.setattr(gpu, "_GPU_PATH", dev)
    used, total, pct = gpu._gpu_gtt()
    assert (used, total, pct) == (1 << 30, 4 << 30, 25.0)


def test_missing_gtt_is_unknown_not_zero(sysfs, monkeypatch):
    dev = _card(sysfs, "card0", "0000:03:00.0")
    monkeypatch.setattr(gpu, "_GPU_PATH", dev)
    assert gpu._gpu_gtt() == (None, None, None)


def test_unreadable_gtt_used_keeps_total_and_no_percent(sysfs, monkeypatch):
    dev = _card(sysfs, "card0", "0000:03:00.0",
                mem_info_gtt_used="garbage", mem_info_gtt_total="4294967296")
    monkeypatch.setattr(gpu, "_GPU_PATH", dev)
    assert gpu._gpu_gtt() == (None, 4 << 30, None)
