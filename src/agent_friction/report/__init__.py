"""Renderers that turn an analysis result (a plain dict) into human-readable output."""

from __future__ import annotations

from agent_friction.report.html import render_html
from agent_friction.report.jsonout import render_json
from agent_friction.report.markdown import render_markdown
from agent_friction.report.prompt import render_prompt
from agent_friction.report.terminal import render_terminal

FORMATS = ("terminal", "markdown", "json", "html", "prompt")

__all__ = [
    "FORMATS",
    "render_html",
    "render_json",
    "render_markdown",
    "render_prompt",
    "render_terminal",
]
