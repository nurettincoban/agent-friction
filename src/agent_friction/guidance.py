"""What the repository already tells agents: AGENTS.md, CLAUDE.md and friends.

A recommendation to "add a line to AGENTS.md" is wasted effort when the line
is already there. This module reads the repository's instruction files and
answers one question: where is this path or command mentioned? Matching is
plain text on word boundaries; nothing is interpreted, and nothing from the
files is ever copied into the result, only ``file:line``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

#: Instruction files read at the repository root.
ROOT_FILES: tuple[str, ...] = (
    "AGENTS.md",
    "CLAUDE.md",
    ".github/copilot-instructions.md",
    ".cursorrules",
)
#: Instruction files also read in the directories above a hotspot.
NESTED_FILES: tuple[str, ...] = ("AGENTS.md", "CLAUDE.md")
#: Directory whose files (any depth one) are rules for Cursor.
CURSOR_RULES_DIR = ".cursor/rules"
#: Settings files whose ``permissions.deny`` rules are read.
SETTINGS_FILES: tuple[str, ...] = (".claude/settings.json", ".claude/settings.local.json")
#: Larger files are not read: instruction files are small, and this must stay cheap.
MAX_FILE_BYTES = 200_000
MAX_CURSOR_RULES = 50
#: Mentions reported per recommendation.
MAX_MENTIONS = 3
#: Shorter terms would match by accident.
MIN_TERM_CHARS = 4
#: A bare launcher says nothing: every AGENTS.md mentions `make` or `npm` somewhere.
GENERIC_TERMS = frozenset(
    {
        "bash",
        "bundle",
        "cargo",
        "docker",
        "dotnet",
        "gradle",
        "make",
        "node",
        "pnpm",
        "poetry",
        "python",
        "python3",
        "yarn",
    }
)


@dataclass(frozen=True)
class Mention:
    """One place an instruction file names a path or command."""

    file: str  # repo-relative, forward slashes
    line: int  # 1-based

    def as_dict(self) -> dict[str, object]:
        return {"file": self.file, "line": self.line}

    def __str__(self) -> str:
        return f"{self.file}:{self.line}"


def usable(term: str) -> bool:
    """Is ``term`` specific enough to look for?"""
    term = term.strip()
    return len(term) >= MIN_TERM_CHARS and term not in GENERIC_TERMS and "<" not in term


def _pattern(term: str) -> re.Pattern[str]:
    # `pytest` is not mentioned by `pytest-cov`, nor `a.py` by `data.py`; a path may
    # follow a slash (`src/api/schema_gen.py` mentions `schema_gen.py`).
    return re.compile(r"(?<![\w.-])" + re.escape(term) + r"(?![\w-])(?!\.\w)")


class Guidance:
    """The instruction files of one repository, read lazily and at most once."""

    def __init__(self, repo: Path | None) -> None:
        self.repo = repo
        self._lines: dict[str, list[tuple[int, str]]] = {}
        self._seen: set[Path] = set()
        self._cursor: list[str] | None = None

    def _read(self, rel: str) -> list[tuple[int, str]]:
        """The searchable lines of ``rel``; empty if it is missing, large or not text."""
        if rel in self._lines:
            return self._lines[rel]
        lines: list[tuple[int, str]] = []
        self._lines[rel] = lines
        if self.repo is None:
            return lines
        try:
            root = self.repo.resolve()
            path = (self.repo / rel).resolve()
            if root not in path.parents or path in self._seen or not path.is_file():
                return lines  # outside the repo, or AGENTS.md is a link to CLAUDE.md
            self._seen.add(path)
            if path.stat().st_size > MAX_FILE_BYTES:
                return lines
            text = path.read_bytes().decode("utf-8", "replace")
        except (OSError, RuntimeError, ValueError):
            return lines
        if "\x00" in text:
            return lines
        numbered = list(enumerate(text.splitlines(), 1))
        if rel in SETTINGS_FILES:
            numbered = _deny_lines(text, numbered)
        lines.extend(numbered)
        return lines

    def _cursor_rules(self) -> list[str]:
        if self._cursor is None:
            self._cursor = []
            if self.repo is not None:
                try:
                    found = sorted(
                        p.name for p in (self.repo / CURSOR_RULES_DIR).iterdir() if p.is_file()
                    )
                except OSError:
                    found = []
                self._cursor = [f"{CURSOR_RULES_DIR}/{name}" for name in found[:MAX_CURSOR_RULES]]
        return self._cursor

    def files(self, near: str = "") -> list[str]:
        """Instruction files that apply to the repo-relative path ``near``."""
        names = [*ROOT_FILES, *self._cursor_rules(), *SETTINGS_FILES]
        parts = [p for p in PurePosixPath(near).parts if p != "."]
        if ".." in parts or near.startswith("/"):
            parts = []  # not a path inside the repository
        for depth in range(1, len(parts) + 1):
            directory = "/".join(parts[:depth])
            names.extend(f"{directory}/{name}" for name in NESTED_FILES)
        return names

    def mentions(self, terms: list[str], near: str = "") -> list[Mention]:
        """Where the files that apply to ``near`` name any of ``terms``."""
        patterns = [_pattern(t.strip()) for t in dict.fromkeys(terms) if usable(t)]
        if not patterns or self.repo is None:
            return []
        found: list[Mention] = []
        for rel in self.files(near):
            for number, line in self._read(rel):
                if any(p.search(line) for p in patterns):
                    found.append(Mention(rel, number))
                    if len(found) >= MAX_MENTIONS:
                        return found
        return found


def _deny_lines(text: str, numbered: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """Only the lines of a settings file that hold a ``permissions.deny`` rule."""
    try:
        data = json.loads(text)
    except ValueError:
        return []
    permissions = data.get("permissions") if isinstance(data, dict) else None
    deny = permissions.get("deny") if isinstance(permissions, dict) else None
    if not isinstance(deny, list):
        return []
    rules = [json.dumps(rule) for rule in deny if isinstance(rule, str)]
    return [(n, line) for n, line in numbered if any(rule in line for rule in rules)]


def path_terms(rel: str) -> list[str]:
    """A file is mentioned by its path or, if that is distinctive, by its name."""
    name = rel.rsplit("/", 1)[-1]
    generic = name.lower() in {"__init__.py", "index.js", "index.ts", "main.py", "mod.rs"}
    return [rel] if generic or name == rel else [rel, name]
