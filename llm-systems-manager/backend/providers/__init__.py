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
    push_endpoint_legacy: str = ""
    default_picker: str = "first_approved"
    pin_dict_key: Optional[str] = None
    sub_tab_keys: tuple = ()
    aggregator: Optional[Callable[[dict[str, dict]], dict]] = None
    card_labels: dict = field(default_factory=dict)


PROVIDERS: dict[str, ProviderSpec] = {}


def register(spec: ProviderSpec) -> None:
    PROVIDERS[spec.name] = spec


def get(name: str) -> Optional[ProviderSpec]:
    return PROVIDERS.get(name)


def names() -> list[str]:
    return list(PROVIDERS.keys())


# Import provider modules so they register at package import time.
from . import llama  # noqa: E402, F401
from . import lms    # noqa: E402, F401
