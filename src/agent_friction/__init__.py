"""agent-friction: map where coding agents struggle in your codebase.

The command line is the main interface. For scripting, ``analyze`` and
``AnalyzeOptions`` return the same result as ``scan -f json`` (see docs/schema.md).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

__version__ = "0.1.0"
__all__ = ["AnalyzeOptions", "__version__", "analyze"]

if TYPE_CHECKING:  # pragma: no cover
    from agent_friction.engine import AnalyzeOptions, analyze


def __getattr__(name: str) -> Any:
    # Lazy, so `agent-friction --version` does not import the whole engine.
    if name in ("analyze", "AnalyzeOptions"):
        from agent_friction import engine

        return getattr(engine, name)
    raise AttributeError(f"module 'agent_friction' has no attribute {name!r}")
