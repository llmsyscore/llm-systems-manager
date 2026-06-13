"""Hardware metric collectors for the LLM Systems Agent.

Each module owns one data source and exposes `collect_*` + `set_deps(*, config)`.
`configure_all(config)` re-hands CONFIG to every module — main() and
`/config/reload` call this once; adding a collector means one import here.
"""

from . import _shared, gpu, ups, liquidctl, system

_MODULES = (_shared, gpu, ups, liquidctl, system)


def configure_all(config) -> None:
    for m in _MODULES:
        m.set_deps(config=config)
