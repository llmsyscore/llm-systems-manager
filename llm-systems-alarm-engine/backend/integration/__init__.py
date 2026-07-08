"""
Integration sub-package for llm-systems-alarm-engine.

Holds metric_flatten: the shared flatten/alias layer that turns raw agent
metric payloads into alarm-engine MetricPoints. Imported directly as a
submodule (backend.integration.metric_flatten) by the live ingest routes.
"""
