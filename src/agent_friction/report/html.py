"""HTML renderer: a single self-contained file with an interactive friction treemap.

The page has no external requests. Data is embedded as JSON and every string
from a transcript is inserted with ``textContent``, never ``innerHTML``, because
transcripts are untrusted input.
"""

from __future__ import annotations

import json
from functools import cache
from importlib.resources import files
from typing import Any

from agent_friction.report._common import (
    PASTE_NOTE,
    paste_deny,
    paste_lines,
    paste_settings_json,
)


def _embed_json(result: dict[str, Any]) -> str:
    """JSON that is safe inside a <script> element (no early ``</script>`` close)."""
    raw = json.dumps(result, sort_keys=True, ensure_ascii=False)
    return (
        raw.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace(" ", "\\u2028")
        .replace(" ", "\\u2029")
    )


@cache
def _template() -> str:
    return files("agent_friction.report").joinpath("template.html").read_text(encoding="utf-8")


def render_html(result: dict[str, Any]) -> str:
    """Render a self-contained interactive HTML friction map."""
    # The "Ready to paste" text, made by the same code as the other formats.
    # The raw list of what humans said is material for a diagnosis, not for the map: the
    # page never shows it, so it does not carry it either.
    shown = {
        **{k: v for k, v in result.items() if k != "conversation"},
        "paste_text": {
            "note": PASTE_NOTE,
            "agents_md": "\n".join(paste_lines(result)),
            "settings": paste_settings_json(result),
            "replay": [f"{rule}: replayed, {what}." for rule, what in paste_deny(result)],
        },
    }
    return _template().replace("__DATA__", _embed_json(shown))
