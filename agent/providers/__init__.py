"""Provider modules — configure_all(ctx) + register_all_routes(app) wire every one."""

from . import lms, llama, lms_tools, terminal, vllm

_MODULES = (lms, llama, lms_tools, terminal, vllm)


def configure_all(ctx) -> None:
    for m in _MODULES:
        m.set_context(ctx)


def register_all_routes(app) -> None:
    for m in _MODULES:
        m.register_routes(app)
