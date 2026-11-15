"""A hand-written analysis result used to test renderers independently of the engine."""

from __future__ import annotations

import copy
from typing import Any

_SAMPLE: dict[str, Any] = {
    "schema_version": 1,
    "tool": {"name": "agent-friction", "version": "0.1.0"},
    "generated_at": "2026-11-01T12:00:00Z",
    "repo": {"root": "~/src/shop", "name": "shop"},
    "window": {"start": "2026-10-01T00:00:00Z", "end": "2026-11-01T00:00:00Z"},
    "sources": [
        {
            "kind": "claude-code",
            "path": "~/.claude/projects/-src-shop",
            "sessions": 6,
            "skipped_lines": 0,
        }
    ],
    "totals": {
        "sessions": 6,
        "tool_calls": 412,
        "friction_events": 31,
        "est_minutes_lost": 74.5,
        "corrections": 4,
        "files_affected": 3,
    },
    "signals": [
        {
            "id": "edit-miss",
            "title": "Edit missed",
            "description": "Edit target text not found.",
            "count": 9,
        },
        {
            "id": "user-correction",
            "title": "Human correction",
            "description": "The human corrected the agent.",
            "count": 4,
        },
        {
            "id": "command-failure",
            "title": "Command failed",
            "description": "A shell command failed.",
            "count": 12,
        },
        {
            "id": "search-miss",
            "title": "Search found nothing",
            "description": "No results.",
            "count": 0,
        },
    ],
    "hotspots": [
        {
            "path": "src/shop/generated/schema.py",
            "kind": "file",
            "score": 82.4,
            "sessions": 5,
            "events": 14,
            "est_minutes_lost": 38.0,
            "signals": {"edit-miss": 9, "user-correction": 2, "edit-thrash": 3},
            "confidence": "high",
            "why": "Edits miss in 5 of 6 sessions; the file is large and generated.",
            "recommendations": [
                {
                    "id": "split-or-mark-generated",
                    "title": "Mark generated sections",
                    "detail": "Tell agents to regenerate with `make schema` instead of editing.",
                }
            ],
            "evidence": [
                {
                    "signal": "edit-miss",
                    "session_id": "aaaaaaaa-1111",
                    "timestamp": "2026-10-03T10:00:00Z",
                    "summary": "old_string not found",
                    "excerpt": "<script>alert('x')</script> | pipe `tick`",
                    "source": "~/.claude/projects/-src-shop/a.jsonl",
                    "line": 42,
                }
            ],
        },
        {
            "path": "src/shop/api/client.py",
            "kind": "file",
            "score": 41.0,
            "sessions": 3,
            "events": 6,
            "est_minutes_lost": 12.0,
            "signals": {"user-correction": 2, "edit-miss": 1},
            "confidence": "medium",
            "why": "A human corrected deprecated API use in 3 sessions.",
            "recommendations": [
                {
                    "id": "codify-correction",
                    "title": "Write down the rule",
                    "detail": 'The human said: "use fetch_v2, not fetch". Add it to AGENTS.md.',
                }
            ],
            "evidence": [],
        },
        {
            "path": "README.md",
            "kind": "file",
            "score": 5.0,
            "sessions": 1,
            "events": 1,
            "est_minutes_lost": 0.5,
            "signals": {"edit-miss": 1},
            "confidence": "low",
            "why": "One miss.",
            "recommendations": [],
            "evidence": [],
        },
    ],
    "commands": [
        {
            "path": "pytest",
            "kind": "command",
            "score": 66.0,
            "sessions": 4,
            "events": 12,
            "est_minutes_lost": 24.0,
            "signals": {"command-failure": 12},
            "confidence": "high",
            "why": "`pytest` fails in 4 sessions; tests need `make test`.",
            "recommendations": [
                {
                    "id": "document-command",
                    "title": "Document the test command",
                    "detail": "Add `make test` to AGENTS.md.",
                }
            ],
            "evidence": [],
        }
    ],
    "tree": {
        "name": "shop",
        "path": "",
        "score": 70.0,
        "events": 21,
        "est_minutes_lost": 50.5,
        "children": [
            {
                "name": "src",
                "path": "src",
                "score": 70.0,
                "events": 20,
                "est_minutes_lost": 50.0,
                "children": [
                    {
                        "name": "shop",
                        "path": "src/shop",
                        "score": 70.0,
                        "events": 20,
                        "est_minutes_lost": 50.0,
                        "children": [
                            {
                                "name": "generated",
                                "path": "src/shop/generated",
                                "score": 82.4,
                                "events": 14,
                                "est_minutes_lost": 38.0,
                                "children": [
                                    {
                                        "name": "schema.py",
                                        "path": "src/shop/generated/schema.py",
                                        "score": 82.4,
                                        "events": 14,
                                        "est_minutes_lost": 38.0,
                                        "children": [],
                                    }
                                ],
                            },
                            {
                                "name": "api",
                                "path": "src/shop/api",
                                "score": 41.0,
                                "events": 6,
                                "est_minutes_lost": 12.0,
                                "children": [
                                    {
                                        "name": "client.py",
                                        "path": "src/shop/api/client.py",
                                        "score": 41.0,
                                        "events": 6,
                                        "est_minutes_lost": 12.0,
                                        "children": [],
                                    }
                                ],
                            },
                        ],
                    }
                ],
            },
            {
                "name": "README.md",
                "path": "README.md",
                "score": 5.0,
                "events": 1,
                "est_minutes_lost": 0.5,
                "children": [],
            },
        ],
    },
}


def sample_result() -> dict[str, Any]:
    """A fresh deep copy so tests can mutate it freely."""
    return copy.deepcopy(_SAMPLE)
