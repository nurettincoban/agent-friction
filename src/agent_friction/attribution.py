"""Attribution: map raw transcript paths and shell commands onto stable keys.

* Paths become repo-relative POSIX paths (or are dropped when outside the repo).
* Shell commands become a short normalized form, so ``npm test -- foo`` and
  ``CI=1 npm test`` are the same hotspot.

Everything here is lexical. Nothing touches the filesystem, which keeps the
behaviour identical for files that no longer exist and for transcripts that
were recorded on another machine.
"""

from __future__ import annotations

import contextlib
import posixpath
import re
import shlex
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_MAX_PATH_CHARS = 1024


def to_posix(path: str) -> str:
    """Normalize Windows separators so prefix matching works on any host."""
    if _DRIVE_RE.match(path) or ("\\" in path and "/" not in path):
        return path.replace("\\", "/")
    return path


def _is_abs(path: str) -> bool:
    return path.startswith("/") or bool(_DRIVE_RE.match(path))


class PathResolver:
    """Resolve raw paths from transcripts to repo-relative POSIX paths.

    ``roots`` are absolute directories that all count as "the repo": the repo
    itself, its symlink-resolved form, its git worktrees, and any aliases the
    user supplied (for example the path the repo had on the machine where the
    transcripts were recorded).
    """

    def __init__(
        self,
        repo: Path,
        extra_roots: Iterable[Path | str] = (),
        home: Path | str | None = None,
    ) -> None:
        self.repo = repo
        self.home = to_posix(str(home if home is not None else Path.home())).rstrip("/")
        roots: list[str] = []
        candidates: list[str] = [str(repo.absolute())]
        with contextlib.suppress(OSError):
            candidates.append(str(repo.resolve()))
        candidates.extend(str(r) for r in extra_roots)
        for candidate in candidates:
            root = posixpath.normpath(to_posix(candidate))
            if _is_abs(root) and root not in roots:
                roots.append(root)
        self.primary = roots[0]
        # Longest first so a worktree nested inside the repo wins over the repo.
        self.roots: tuple[str, ...] = tuple(sorted(roots, key=lambda r: (-len(r), r)))

    def _absolute(self, raw: str, cwd: str | None) -> str | None:
        path = raw.strip().strip("\"'`")
        if not path or len(path) > _MAX_PATH_CHARS or "\0" in path or "\n" in path:
            return None
        path = to_posix(path)
        if path == "~" or path.startswith("~/"):
            path = self.home + path[1:]
        if not _is_abs(path):
            base = to_posix(cwd) if cwd else self.primary
            if not _is_abs(base):
                return None
            path = posixpath.join(base, path)
        return posixpath.normpath(path)

    def _relative(self, absolute: str) -> str | None:
        for root in self.roots:
            if absolute == root:
                return ""
            prefix = root.rstrip("/") + "/"
            if absolute.startswith(prefix):
                return absolute[len(prefix) :]
        return None

    def resolve(self, raw: str | None, cwd: str | None = None) -> str | None:
        """Repo-relative path for ``raw``, ``""`` for the repo root, ``None`` if outside.

        Relative paths are resolved against the session ``cwd`` (or the repo
        root when the cwd is unknown). Paths inside ``.git`` are dropped.
        """
        if not raw:
            return None
        absolute = self._absolute(raw, cwd)
        if absolute is None:
            return None
        rel = self._relative(absolute)
        if rel is None or rel == ".git" or rel.startswith(".git/"):
            return None
        return rel

    def contains(self, cwd: str | None) -> bool:
        """True when ``cwd`` is inside the repo (unknown cwd counts as inside)."""
        if not cwd:
            return True
        absolute = self._absolute(cwd, None)
        return absolute is not None and self._relative(absolute) is not None


# --------------------------------------------------------------------------------------
# Shell command normalization
# --------------------------------------------------------------------------------------

#: Segments that only prepare the environment; skipped when picking the primary command.
SETUP_PROGRAMS = frozenset(
    {"cd", "export", "source", ".", "set", "pushd", "popd", "unset", "true", ":", "echo"}
)
#: Prefixes stripped in front of the real program.
WRAPPER_PROGRAMS = frozenset({"sudo", "time", "env", "nohup", "command", "exec", "nice"})
#: Interpreters: the script (or ``-m module``) is part of the command identity.
INTERPRETERS = frozenset(
    {"python", "python3", "node", "bash", "sh", "zsh", "ruby", "perl", "php", "deno", "bun"}
    | {"tsx", "ts-node", "pwsh"}
)
#: Programs whose first argument is never a subcommand.
NO_SUBCOMMAND = frozenset(
    {"ls", "cat", "rm", "mkdir", "grep", "egrep", "fgrep", "rg", "find", "sed", "awk", "head"}
    | {"tail", "cp", "mv", "touch", "chmod", "chown", "curl", "wget", "pytest", "ruff", "mypy"}
    | {"eslint", "tsc", "jest", "vitest", "prettier", "black", "flake8", "pylint", "wc", "diff"}
    | {"sort", "uniq", "xargs", "tee", "which", "stat", "file", "test", "[", "cmp", "kill"}
    | {"sleep", "open", "code", "jq", "tar", "unzip", "zip", "ln", "du", "df", "ps", "type"}
)
#: After one of these subcommands the next word still identifies what runs.
CONTINUING_SUBCOMMANDS = frozenset(
    {"run", "exec", "compose", "pr", "issue", "repo", "x", "dlx", "tool", "task", "workspace"}
    | {"run-script", "buildx", "api"}
)
#: Read-only inspection commands. Their failures are routine probing, not friction.
INSPECTION_PROGRAMS = frozenset(
    {"ls", "cat", "head", "tail", "wc", "stat", "file", "find", "cd", "pwd", "echo", "which"}
    | {"type", "grep", "egrep", "fgrep", "rg", "test", "[", "diff", "cmp", "true", "false"}
    | {"command", "du", "df", "ps", "tree", "sleep", "kill", "pkill", "lsof"}
)

_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SUBCOMMAND_RE = re.compile(r"^[a-z][a-z0-9:_-]*$")
_PYTHON_RE = re.compile(r"^(python|pypy)[\d.]*$")
_SEPARATORS = frozenset({";", "&&", "||", "&", "|", "|&", "(", ")", ";;"})
_PIPE = frozenset({"|", "|&"})
_STATEMENT_END = frozenset({";", "&", ";;"})
_VENDOR_DIRS = frozenset({"bin", "node_modules", "venv", ".venv", "Scripts", ".bin"})
_SCRIPT_RE = re.compile(r"\.[A-Za-z0-9]{1,5}$")


@dataclass(frozen=True)
class ParsedCommand:
    """Result of :func:`parse_command`."""

    key: str  # normalized command, "" if nothing recognisable
    program: str  # program of the primary segment (basename)
    last_program: str  # program whose exit status the shell reports
    tokens: tuple[str, ...]  # arguments of the last statement, for path extraction


def _tokenize(command: str) -> list[str]:
    text = command.split("<<", 1)[0]  # heredoc bodies are data, not commands
    text = text.replace("\\\n", " ").replace("\n", " ; ")
    lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        return list(lexer)
    except ValueError:  # unbalanced quotes
        return text.split()


def _statements(tokens: Sequence[str]) -> list[list[tuple[list[str], bool]]]:
    """Split tokens into statements (``;``/newline) of simple commands.

    Each simple command carries a flag that is True for a pipeline tail.
    """
    statements: list[list[tuple[list[str], bool]]] = [[]]
    current: list[str] = []
    piped = False
    skip_next = False
    for token in tokens:
        if skip_next:
            skip_next = False
            continue
        if token in _SEPARATORS:
            if current:
                statements[-1].append((current, piped))
            current = []
            piped = token in _PIPE
            if token in _STATEMENT_END:
                statements.append([])
            continue
        if token[0] in "<>":  # redirection: drop it, its target and a leading fd number
            skip_next = token.strip("<>&") == ""
            if current and current[-1].isdigit():
                current.pop()
            continue
        if token.startswith("#") and not current:
            break
        current.append(token)
    if current:
        statements[-1].append((current, piped))
    return [statement for statement in statements if statement]


def _strip_prefixes(words: Sequence[str]) -> list[str]:
    out = list(words)
    while out:
        head = out[0]
        if _ENV_ASSIGN_RE.match(head) or head in WRAPPER_PROGRAMS:
            out.pop(0)
        elif head == "timeout" and len(out) > 2:
            out = out[2:]
        else:
            break
    return out


def _program_name(word: str) -> str:
    """Basename for absolute/vendored executables, cleaned relative path for repo scripts."""
    word = to_posix(word)
    if "/" not in word:
        return word
    parts = [p for p in word.split("/") if p not in ("", ".")]
    if not parts:
        return word
    if _is_abs(word) or word.startswith("~") or any(p in _VENDOR_DIRS for p in parts[:-1]):
        return parts[-1]
    return posixpath.normpath(word)


def _looks_like_script(word: str) -> bool:
    return not word.startswith("-") and ("/" in word or bool(_SCRIPT_RE.search(word)))


def _key(words: Sequence[str]) -> str:
    if not words:
        return ""
    program = _program_name(words[0])
    if _PYTHON_RE.match(program):
        program = "python"  # python3, python3.12, pypy3 ... are one command
    rest = list(words[1:])
    parts = [program]
    base = program.rsplit("/", 1)[-1]
    if base in INTERPRETERS or _PYTHON_RE.match(base):
        if rest and rest[0] == "-m" and len(rest) > 1:
            parts += ["-m", rest[1]]
            return " ".join(parts)
        if rest and rest[0] in ("-c", "-e"):
            parts.append(rest[0])
            return " ".join(parts)
        if rest and _looks_like_script(rest[0]):
            parts.append(_program_name(rest[0]))
            return " ".join(parts)
    if base in NO_SUBCOMMAND or "/" in program:
        return program
    if rest and _SUBCOMMAND_RE.match(rest[0]):
        parts.append(rest[0])
        if rest[0] in CONTINUING_SUBCOMMANDS and len(rest) > 1 and _SUBCOMMAND_RE.match(rest[1]):
            parts.append(rest[1])
    return " ".join(parts)


def parse_command(command: str) -> ParsedCommand:
    """Normalize a shell command line. Rules are documented in docs/methodology.md.

    The shell reports the exit status of the *last* statement, so that is the
    one we name: in ``npm run build; npm test`` a failure is ``npm test``'s.
    Within it, setup commands (``cd x && ...``) and pipeline tails are skipped.
    """
    statements = [
        [(words, piped) for words, piped in ((_strip_prefixes(w), p) for w, p in st) if words]
        for st in _statements(_tokenize(command))
    ]
    statements = [st for st in statements if st]
    if not statements:
        return ParsedCommand("", "", "", ())
    segments = statements[-1]
    heads = [words for words, piped in segments if not piped]
    primary = next(
        (w for w in heads if _program_name(w[0]) not in SETUP_PROGRAMS),
        heads[0] if heads else segments[0][0],
    )
    last = segments[-1][0]
    return ParsedCommand(
        key=_key(primary),
        program=_program_name(primary[0]).rsplit("/", 1)[-1],
        last_program=_program_name(last[0]).rsplit("/", 1)[-1],
        tokens=tuple(
            word
            for words, _ in segments
            if _program_name(words[0]) not in SETUP_PROGRAMS
            for word in words[1:]
        ),
    )


def normalize_command(command: str) -> str:
    """Shorthand for ``parse_command(command).key``."""
    return parse_command(command).key


_PATH_SUFFIX_RE = re.compile(r"(::.*|:\d+(:\d+)?)$")


def path_candidates(tokens: Iterable[str]) -> list[str]:
    """Words of a command (or prose) that might be paths, in order, without duplicates."""
    seen: list[str] = []
    for token in tokens:
        word = token.lstrip("\"'`([{<").rstrip("\"'`)]}>,;.:!?")
        if word.startswith("--") and "=" in word:
            word = word.split("=", 1)[1]
        word = _PATH_SUFFIX_RE.sub("", word)
        if not word or word.startswith("-") or len(word) > _MAX_PATH_CHARS:
            continue
        if "/" not in word and "." not in word and "\\" not in word:
            continue
        if word not in seen:
            seen.append(word)
    return seen
