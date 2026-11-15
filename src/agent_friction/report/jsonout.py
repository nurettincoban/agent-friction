"""JSON output: the canonical, machine-readable form of an analysis."""

from __future__ import annotations

import json
from typing import Any


def render_json(result: dict[str, Any]) -> str:
    """Serialize an analysis deterministically (sorted keys, trailing newline)."""
    return json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
