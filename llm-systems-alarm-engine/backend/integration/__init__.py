"""
Integration sub-package for llm-systems-alarm-engine.

Bridges the alarm engine with llm-systems-manager via HTTP push and
reverse-proxy patterns so the alarm engine can be a drop-in tab
inside the existing LLM dashboard.
"""

from .llm_systems_manager_adapter import LLMSystemsManagerAdapter
from .manager_proxy import (
    AlarmWebSocketBridge,
    check_alarm_engine_health,
    forward_metric_to_alarm_engine,
    forward_metrics_batch as forward_metrics_batch_proxy,
)

__all__ = [
    "LLMSystemsManagerAdapter",
    "AlarmWebSocketBridge",
    "check_alarm_engine_health",
    "forward_metric_to_alarm_engine",
    "forward_metrics_batch_proxy",
]