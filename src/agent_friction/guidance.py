"""What the repository already tells agents: AGENTS.md, CLAUDE.md and friends.

A recommendation to "add a line to AGENTS.md" is wasted effort when the line
is already there. This module reads the repository's instruction files and
answers two questions: where is this path or command *mentioned* (plain text on
word boundaries), and where does a line *instruct* about it (the command stands
in command context, the path next to an instruction verb)? Nothing is
interpreted beyond that, and nothing from the files is ever copied into the
result, only ``file:line``.
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
    "CLAUDE.local.md",
    "GEMINI.md",
    ".github/copilot-instructions.md",
    ".cursorrules",
    ".windsurfrules",
)
#: Directories whose Markdown files are rules for Claude Code, at any depth.
CLAUDE_RULES_DIR = ".claude/rules"
#: Files whose ``@path`` lines pull other files in (followed one level, inside the repo).
IMPORTING_FILES = frozenset({"CLAUDE.md", "CLAUDE.local.md"})
MAX_IMPORTS = 20
_IMPORT_RE = re.compile(
    r"(?:^|\s)@((?:\./|\.\./)?[\w][\w./-]*\.[A-Za-z]{1,8}|\./[\w./-]+)(?=$|[\s),.;:])"
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
        self._imports: dict[str, list[str]] = {}

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
                try:
                    rules = sorted(
                        p.relative_to(self.repo).as_posix()
                        for p in (self.repo / CLAUDE_RULES_DIR).rglob("*.md")
                        if p.is_file()
                    )
                except (OSError, ValueError):
                    rules = []
                self._cursor += rules[:MAX_CURSOR_RULES]
        return self._cursor

    def _imported(self, rel: str) -> list[str]:
        """Files that ``rel`` (a CLAUDE.md) pulls in with ``@path`` lines, one level deep."""
        if rel.rsplit("/", 1)[-1] not in IMPORTING_FILES:
            return []
        if rel not in self._imports:
            base = PurePosixPath(rel).parent
            found: list[str] = []
            fenced = False
            for _, line in self._read(rel):
                if line.lstrip().startswith("```"):
                    fenced = not fenced
                if fenced:
                    continue
                for target in _IMPORT_RE.findall(line):
                    parts: list[str] = []
                    for part in (base / target).parts:
                        if part == "..":
                            if not parts:
                                parts = [".."]
                                break
                            parts.pop()
                        elif part != ".":
                            parts.append(part)
                    path = "/".join(parts)
                    if path and not path.startswith("..") and path != rel and path not in found:
                        found.append(path)
            self._imports[rel] = found[:MAX_IMPORTS]
        return self._imports[rel]

    def files(self, near: str = "") -> list[str]:
        """Instruction files that apply to the repo-relative path ``near``."""
        names = [*ROOT_FILES, *self._cursor_rules(), *SETTINGS_FILES]
        parts = [p for p in PurePosixPath(near).parts if p != "."]
        if ".." in parts or near.startswith("/"):
            parts = []  # not a path inside the repository
        for depth in range(1, len(parts) + 1):
            directory = "/".join(parts[:depth])
            names.extend(f"{directory}/{name}" for name in NESTED_FILES)
        with_imports: list[str] = []
        for name in names:
            with_imports.append(name)
            with_imports.extend(self._imported(name))
        return list(dict.fromkeys(with_imports))

    def mentions(
        self, terms: list[str], near: str = "", affirmed: bool = False, context: str = ""
    ) -> list[Mention]:
        """Where the files that apply to ``near`` name any of ``terms``.

        With ``affirmed``, a line that names a term only to rule it out ("Do not run
        `make test` locally", "`make test` was removed", "`make test` is slow") is not
        a mention: it does not document the term as the thing to use.

        ``context`` asks for more than the words being there:

        * ``"command"``: a term of several words (``make test``) must stand where a
          command stands: in backticks, in a code fence, after a ``$`` prompt, or
          after run/use/with/via/call. "We make test data with factories" does not
          mention ``make test``. One distinctive word (``pytest``) is enough as it is.
        * ``"file"``: the line must also carry an instruction about the file (edit,
          change, modify, generated, regenerate, never, don't, do not, ...). "See
          src/schema_gen.py for an example of our style" does not instruct anything.
        """
        wanted = [t.strip() for t in dict.fromkeys(terms) if usable(t)]
        patterns = [(t, _pattern(t)) for t in wanted]
        if not patterns or self.repo is None:
            return []
        found: list[Mention] = []
        for rel in self.files(near):
            fenced = False
            for number, line in self._read(rel):
                if line.lstrip().startswith("```"):
                    fenced = not fenced
                    continue
                hits = [(t, m) for t, m in ((t, p.search(line)) for t, p in patterns) if m]
                if affirmed:
                    hits = [(t, m) for t, m in hits if not _negated(line, m.start(), m.end())]
                if context == "command":
                    hits = [
                        (t, m)
                        for t, m in hits
                        if " " not in t or fenced or _as_command(line, m.start())
                    ]
                elif context == "file":
                    hits = hits if _INSTRUCTION_RE.search(line) else []
                if hits:
                    found.append(Mention(rel, number))
                    if len(found) >= MAX_MENTIONS:
                        return found
        return found


#: Words in front of a command that say "this is a command to run".
_COMMAND_LEAD_RE = re.compile(
    r"(?:^\s*\$\s+|\b(?:run|use|using|with|via|call|invoke|execute|try)\s+(?:the\s+)?[\"']?)$",
    re.I,
)
#: Words that make a line an instruction about a file rather than a pointer to it.
_INSTRUCTION_RE = re.compile(
    r"\b(?:(?:hand-)?edit\w*|chang\w*|modif\w*|generated|regenerat\w*|never|don't|dont|do not"
    r"|must not|overwrit\w*|rewrit\w*|touch\w*|read-only)\b",
    re.I,
)


def _as_command(line: str, start: int) -> bool:
    """Does the text at ``start`` stand where a command stands on ``line``?"""
    before = line[:start]
    if before.count("`") % 2 == 1:
        return True  # inside a backtick span
    return _COMMAND_LEAD_RE.search(before) is not None


#: Words in front of a name that rule it out, and words after it that say it is gone.
_NEGATED_BEFORE_RE = re.compile(
    r"\b(?:do not|don't|dont|never|avoid|stop|not|no longer|instead of|rather than|unlike"
    r"|deprecated|forbidden|must not|should not|shouldn't|cannot|can't|won't)\b[^.;:!?]{0,40}$",
    re.I,
)
_NEGATED_AFTER_RE = re.compile(
    r"^[`'\")*_\s]*(?:(?:was|were|is|are|has been|have been|got)\s+(?:\w+\s+)?"
    r"(?:removed|deleted|deprecated|gone|dropped|retired|replaced|broken|disabled|forbidden"
    r"|not\b|no longer|(?:too |very |really )?(?:slow|flaky|unreliable|expensive|heavy))"
    r"|(?:no longer|does not|doesn't|will not|won't|must not|should not|is not|isn't)\b)",
    re.I,
)


def _negated(line: str, start: int, end: int) -> bool:
    return bool(_NEGATED_BEFORE_RE.search(line[:start]) or _NEGATED_AFTER_RE.search(line[end:]))


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
