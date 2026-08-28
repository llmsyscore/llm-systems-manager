"""Manager-side provider registry.

Each upstream LLM provider (llama.cpp, LM Studio, future: vLLM, Ollama, TGI)
declares a `ProviderSpec` and `register()`s it at import time. The registry
is the single source of truth for "what providers exist" — capability keys,
default-picker policy, sub-tab routing, pin dict, aggregator function.

Adding a new provider = one new module here + one new module under
`agent/providers/` + import line below.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    label: str
    capability_key: str
    online_threshold_s: float = 15.0
    default_picker: str = "first_approved"
    pin_dict_key: Optional[str] = None
    # True when a load displaces the host's resident model.
    single_resident: bool = False
    # False when the agent exposes no /<provider>/unload route.
    unloadable: bool = True
    gateway_enabled: bool = False
    sub_tab_keys: tuple = ()
    aggregator: Optional[Callable[[dict[str, dict]], dict]] = None
    card_labels: dict = field(default_factory=dict)


PROVIDERS: dict[str, ProviderSpec] = {}


def int_or_none(v):
    """int(v) for real numbers, None for everything else."""
    return int(v) if isinstance(v, (int, float)) else None


_THERMAL_CRIT = ("Serious", "Critical")


def new_gpu_rollup() -> dict:
    return {"max_temp_c": 0.0, "max_vram_pct": 0.0, "total_power_watts": 0.0,
            "thermal_crit_count": 0}


def gpu_rollup_add(acc: dict, sample: dict) -> dict:
    """Fold one ONLINE host into the fleet GPU rollup and return its row
    fields {power_watts, thermal_crit}; watts follow energy.extract_power."""
    import energy  # type: ignore[import-not-found]  # sibling
    sysb = energy._sys_block(sample)
    gpu = sysb.get("gpu") if isinstance(sysb.get("gpu"), dict) else {}
    mac = sample.get("mac_power") or sysb.get("mac_power")
    mac = mac if isinstance(mac, dict) else {}
    temp = gpu.get("temperature_c")
    if isinstance(temp, (int, float)) and temp > acc["max_temp_c"]:
        acc["max_temp_c"] = float(temp)
    vram = gpu.get("vram_usage_percent")
    if isinstance(vram, (int, float)) and vram > acc["max_vram_pct"]:
        acc["max_vram_pct"] = float(vram)
    watts, _src = energy.extract_power(sample)
    if watts is not None:
        acc["total_power_watts"] += watts
    n = mac.get("thermal_pressure_n")
    crit = (n >= 2) if isinstance(n, (int, float)) else (
        mac.get("thermal_pressure") in _THERMAL_CRIT)
    if crit:
        acc["thermal_crit_count"] += 1
    return {"power_watts": watts, "thermal_crit": crit}


def register(spec: ProviderSpec) -> None:
    PROVIDERS[spec.name] = spec


def get(name: str) -> Optional[ProviderSpec]:
    return PROVIDERS.get(name)


def names() -> list[str]:
    return list(PROVIDERS.keys())


def single_resident_names() -> tuple[str, ...]:
    """Providers whose spec declares single_resident (planner co-placement rule)."""
    return tuple(n for n, s in PROVIDERS.items() if s.single_resident)


def unloadable_names() -> tuple[str, ...]:
    """Providers whose spec declares unloadable (planner scale-down rule)."""
    return tuple(n for n, s in PROVIDERS.items() if s.unloadable)


def pool_provider_names() -> list[str]:
    """Names of providers whose spec uses the pool default-picker."""
    return [n for n, s in PROVIDERS.items() if s.default_picker == "pool"]


# Import provider modules so they register at package import time.
from . import llama  # noqa: E402, F401
from . import lms    # noqa: E402, F401
from . import vllm   # noqa: E402, F401
