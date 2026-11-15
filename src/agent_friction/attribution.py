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
    """Normalize Windows separators and drive-letter case so prefix matching works anywhere."""
    if _DRIVE_RE.match(path):
        return path[0].upper() + path[1:].replace("\\", "/")
    if "\\" in path and "/" not in path:
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
        # The spelling as given is kept too: a POSIX-style root stays usable on Windows,
        # where ``absolute()`` would graft a drive letter onto it.
        candidates: list[str] = [str(repo.absolute()), str(repo)]
        with contextlib.suppress(OSError):
            candidates.append(str(repo.resolve()))
        candidates.extend(str(r) for r in extra_roots)
        for candidate in candidates:
            root = posixpath.normpath(to_posix(candidate))
            if _is_abs(root) and root not in roots:
                roots.append(root)
        if not roots:  # pragma: no cover - absolute() always yields an absolute path
            roots.append("/")
        self.primary = roots[0]
        # Longest first so a worktree nested inside the repo wins over the repo.
        self.roots: tuple[str, ...] = tuple(sorted(roots, key=lambda r: (-len(r), r)))

    @staticmethod
    def normalize(path: Path | str) -> str:
        """Lexically normalized POSIX spelling of an absolute path."""
        return posixpath.normpath(to_posix(str(path)))

    @staticmethod
    def posix_set(paths: Iterable[Path | str]) -> frozenset[str]:
        return frozenset(PathResolver.normalize(p) for p in paths)

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

#: Commands that only prepare the environment; never what a command line is "about".
SETUP_PROGRAMS = frozenset(
    {"cd", "export", "source", ".", "set", "pushd", "popd", "unset", "true", ":", "echo"}
    | {"exit", "return", "break", "continue", "shift", "local", "read", "printf", "wait"}
)
#: Prefixes stripped in front of the real program.
WRAPPER_PROGRAMS = frozenset(
    {"sudo", "time", "env", "nohup", "command", "exec", "nice", "xargs", "watch", "stdbuf"}
)
#: Shell keywords that may precede a command; stripped (``if ! make lint`` is ``make lint``).
_KEYWORD_PREFIXES = frozenset({"if", "elif", "then", "else", "do", "while", "until", "!", "{"})
#: Shell keywords that head a segment with no command in it (``for x in a b``, ``done``).
_KEYWORD_ONLY = frozenset({"for", "select", "case", "function", "done", "fi", "esac", "}", "in"})
#: Shells: ``bash -c '<script>'`` is named after the script it runs.
SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh"})
#: Interpreters: the script (or ``-m module``) is part of the command identity.
INTERPRETERS = SHELLS | frozenset(
    {"python", "node", "ruby", "perl", "php", "deno", "bun", "tsx", "ts-node", "pwsh"}
)
#: Tools whose first word after the options is a subcommand. Only for these is anything
#: beyond the executable name kept, so arguments of unknown tools never enter a key.
SUBCOMMAND_TOOLS = frozenset(
    {"git", "npm", "pnpm", "yarn", "bun", "npx", "uv", "uvx", "cargo", "go", "make", "docker"}
    | {"podman", "kubectl", "helm", "poetry", "pip", "pip3", "pipx", "pipenv", "pdm", "hatch"}
    | {"rye", "gh", "glab", "dotnet", "gradle", "gradlew", "mvn", "bazel", "rake"}
    | {"bundle", "composer", "terraform", "nx", "turbo", "just", "task", "invoke", "deno"}
    | {"brew", "apt", "apt-get", "conda", "mamba", "rustup", "swift", "flutter", "dart", "mix"}
    | {"lerna", "alembic", "flask", "rails", "django-admin", "wrangler", "vercel", "supabase"}
    | {"firebase", "aws", "gcloud", "az", "systemctl", "pre-commit", "cmake", "ninja", "zig"}
)
#: Per tool, the subcommands after which one more word still names what runs: a script
#: runner (``npm run build``, ``uv run pytest``) or a command group (``gh pr create``).
#: Anything else that follows a subcommand is an argument and never part of the key
#: (``docker run <image>``, ``kubectl exec <pod>``, ``yarn workspace <name>``).
_RUNNERS = frozenset({"run", "exec"})
CONTINUING_SUBCOMMANDS: dict[str, frozenset[str]] = {
    "npm": _RUNNERS | {"run-script"},
    "pnpm": _RUNNERS | {"dlx"},
    "yarn": _RUNNERS | {"dlx"},
    "bun": frozenset({"run", "x"}),
    "uv": frozenset({"run", "pip", "tool"}),
    "poetry": frozenset({"run"}),
    "pipenv": frozenset({"run"}),
    "pdm": frozenset({"run"}),
    "hatch": frozenset({"run"}),
    "rye": frozenset({"run"}),
    "bundle": frozenset({"exec"}),
    "nx": frozenset({"run"}),
    "turbo": frozenset({"run"}),
    "deno": frozenset({"run", "task"}),
    "docker": frozenset({"compose", "buildx"}),
    "podman": frozenset({"compose"}),
    "gh": frozenset({"pr", "issue", "repo", "run", "release", "workflow"}),
    "glab": frozenset({"mr", "issue", "repo", "ci"}),
    "git": frozenset({"stash", "remote", "worktree", "submodule"}),
}
#: Options of subcommand tools that take a value, skipped together with it
#: (``git -C /repo commit`` is ``git commit``). ``*`` applies to every tool.
VALUE_OPTIONS: dict[str, frozenset[str]] = {
    "*": frozenset({"-C", "--directory", "--cwd", "--prefix", "--project", "--config", "-f"}),
    "git": frozenset({"-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}),
    "make": frozenset({"-I", "-o", "-W", "--file", "--makefile", "-j", "--jobs", "-l"}),
    "npm": frozenset({"--workspace", "-w", "--userconfig"}),
    "pnpm": frozenset({"--filter", "-F", "--dir", "--workspace-dir"}),
    "yarn": frozenset({"--cwd"}),
    "kubectl": frozenset({"-n", "--namespace", "--context", "--kubeconfig", "--cluster", "-s"}),
    "helm": frozenset({"-n", "--namespace", "--kube-context", "--kubeconfig"}),
    "docker": frozenset({"--context", "-H", "--host", "-c", "-l", "--log-level"}),
    "podman": frozenset({"--connection", "--root", "--log-level"}),
    "cargo": frozenset({"--manifest-path", "--color", "-Z"}),
    "uv": frozenset({"--python", "-p", "--cache-dir", "--color"}),
    "poetry": frozenset({"-P"}),
    "gh": frozenset({"-R", "--repo"}),
    "aws": frozenset({"--profile", "--region", "--output", "--endpoint-url"}),
    "gcloud": frozenset({"--project", "--account", "--configuration"}),
    "terraform": frozenset({"-chdir"}),
    "go": frozenset(),
}
#: Read-only inspection commands. Their failures are routine probing, not friction.
INSPECTION_PROGRAMS = frozenset(
    {"ls", "cat", "head", "tail", "wc", "stat", "file", "find", "cd", "pwd", "echo", "which"}
    | {"type", "grep", "egrep", "fgrep", "rg", "test", "[", "diff", "cmp", "true", "false"}
    | {"command", "du", "df", "ps", "tree", "sleep", "kill", "pkill", "lsof", "awk", "jq"}
    | {"sed", "sort", "uniq", "cut", "tr", "less", "more", "printf", "date", "env", "printenv"}
)
#: Read-only subcommands of otherwise significant programs.
INSPECTION_KEYS = frozenset(
    {"git diff", "git status", "git log", "git show", "git branch", "git blame", "git grep"}
    | {"git ls-files", "git rev-parse", "git remote", "git stash list", "git describe"}
)
#: Flags that turn an inspection command into one with a meaningful result.
_NOT_INSPECTION_FLAGS = ("-i", "--in-place", "--exit-code", "--quiet")
#: Filters whose exit status 1 means "nothing matched / differs", not "failed".
MATCH_FILTERS = frozenset({"grep", "egrep", "fgrep", "rg", "diff", "cmp", "test", "["})
#: Programs that only trim or count another command's output at the end of a pipeline.
OUTPUT_TRIMMERS = frozenset(
    {"head", "tail", "wc", "sort", "uniq", "less", "more", "cut", "tr", "column", "nl"}
)
#: Filesystem housekeeping: rarely the reason a chain of commands failed.
HOUSEKEEPING_PROGRAMS = frozenset(
    {"mkdir", "touch", "rm", "rmdir", "cp", "mv", "chmod", "chown", "ln", "tee"}
)
#: Steps a human is most likely objecting to when declining a command line.
DESTRUCTIVE_PROGRAMS = frozenset(
    {"rm", "rmdir", "mv", "dd", "truncate", "shred", "chmod", "chown", "kill", "pkill", "mkfs"}
)
DESTRUCTIVE_KEYS = frozenset(
    {"git push", "git reset", "git clean", "git checkout", "git restore", "git rebase"}
    | {"git stash", "git branch", "git tag", "git merge", "git commit", "npm publish"}
    | {"pnpm publish", "yarn publish", "cargo publish", "docker rm", "docker rmi"}
    | {"docker system", "docker volume", "kubectl delete", "kubectl apply", "helm uninstall"}
    | {"helm upgrade", "terraform destroy", "terraform apply", "gh release", "uv publish"}
    | {"poetry publish", "alembic downgrade"}
)

_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SUBCOMMAND_RE = re.compile(r"^[a-z][a-z0-9:_-]*$")
_PYTHON_RE = re.compile(r"^(python|pypy)[\d.]*$")
_SEPARATORS = frozenset({";", "&&", "||", "&", "|", "|&", "(", ")", ";;"})
_VENDOR_DIRS = frozenset({"bin", "node_modules", "venv", ".venv", "Scripts", ".bin"})
_SCRIPT_RE = re.compile(r"\.[A-Za-z0-9]{1,5}$")
_HEREDOC_RE = re.compile(r"<<-?\s*(?:'([^']+)'|\"([^\"]+)\"|\\?([A-Za-z_][A-Za-z0-9_]*))")
#: Nested ``bash -c`` scripts are followed this deep.
_MAX_NESTING = 2


@dataclass(frozen=True)
class Step:
    """One simple command of a command line."""

    key: str
    program: str
    args: tuple[str, ...]
    inspection: bool
    significant: bool


@dataclass(frozen=True)
class ParsedCommand:
    """Result of :func:`parse_command`."""

    key: str  # normalized command, "" if nothing recognisable
    program: str  # program of the primary step (basename)
    last_program: str  # final program of the command line (its status is reported)
    tokens: tuple[str, ...]  # arguments of the primary step, for path extraction
    inspection: bool = False  # read-only probing: a failure is not friction
    steps: tuple[Step, ...] = ()  # every simple command, in order


def _strip_heredocs(command: str) -> str:
    """Remove heredoc bodies (they are data) but keep the commands around them."""
    if "<<" not in command:
        return command
    out: list[str] = []
    terminators: list[str] = []
    for line in command.split("\n"):
        if terminators:
            if line.strip() == terminators[0]:
                terminators.pop(0)
            continue
        found = [a or b or c for a, b, c in _HEREDOC_RE.findall(line)]
        if found:
            terminators = found
            line = _HEREDOC_RE.sub(" ", line)
        out.append(line)
    return "\n".join(out)


def _tokenize(command: str) -> list[str]:
    text = _strip_heredocs(command).replace("\\\n", " ").replace("\n", " ; ")
    lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        return list(lexer)
    except ValueError:  # unbalanced quotes
        return text.split()


def _segments(tokens: Sequence[str]) -> list[list[str]]:
    """Split tokens into simple commands, dropping redirections and comments."""
    segments: list[list[str]] = []
    current: list[str] = []
    skip_next = False
    comment = False
    for token in tokens:
        if token in _SEPARATORS:
            if current:
                segments.append(current)
            current = []
            comment = comment and token not in (";", ";;")
            skip_next = False
            continue
        if comment:
            continue
        if skip_next:
            skip_next = False
            continue
        if token[0] in "<>":  # redirection: drop it, its target and a leading fd number
            skip_next = token.strip("<>&") == ""
            if current and current[-1].isdigit():
                current.pop()
            continue
        if token.startswith("#") and not current:
            comment = True
            continue
        current.append(token)
    if current:
        segments.append(current)
    return segments


def _strip_prefixes(words: Sequence[str]) -> list[str]:
    """Drop shell keywords, ``VAR=value`` assignments and wrappers in front of a command."""
    out = list(words)
    while out:
        head = out[0]
        if head in _KEYWORD_ONLY:
            return []
        if head in _KEYWORD_PREFIXES or _ENV_ASSIGN_RE.match(head):
            out.pop(0)
        elif head in WRAPPER_PROGRAMS:
            out.pop(0)
            while out and out[0].startswith("-"):  # the wrapper's own flags
                out.pop(0)
        elif head == "timeout" and len(out) > 2:
            out = out[1:]
            while out and out[0].startswith("-"):
                out.pop(0)
            out = out[1:]
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


def _skip_options(tool: str, rest: Sequence[str]) -> list[str]:
    """Drop the options a subcommand tool takes before its subcommand."""
    takes_value = VALUE_OPTIONS["*"] | VALUE_OPTIONS.get(tool, frozenset())
    out = list(rest)
    while out and (out[0].startswith("-") or out[0].startswith("+")):
        option = out.pop(0)
        if option == "--":
            break
        if option in takes_value and out and not out[0].startswith("-"):
            # ``-j`` and friends take an optional value: only swallow what looks like one
            if option in ("-j", "--jobs", "-l") and not out[0].isdigit():
                continue
            out.pop(0)
    return out


def _key(words: Sequence[str]) -> str:
    """Normalized identity of one simple command (documented in docs/methodology.md)."""
    if not words:
        return ""
    program = _program_name(words[0])
    if _PYTHON_RE.match(program):
        program = "python"  # python3, python3.12, pypy3 ... are one command
    rest = list(words[1:])
    base = program.rsplit("/", 1)[-1]
    if base in INTERPRETERS:
        if rest and rest[0] == "-m" and len(rest) > 1:
            return f"{program} -m {rest[1]}"
        if rest and rest[0] in ("-c", "-e", "-lc", "-ic"):
            return f"{program} -c"
        if rest and _looks_like_script(rest[0]):
            return f"{program} {_program_name(rest[0])}"
    if base not in SUBCOMMAND_TOOLS or "/" in program:
        return program  # unknown tool or repo script: the executable alone, never its arguments
    rest = _skip_options(base, rest)
    parts = [program]
    if rest and _SUBCOMMAND_RE.match(rest[0]):
        parts.append(rest[0])
        if rest[0] in CONTINUING_SUBCOMMANDS.get(base, ()):
            tail = _skip_options(base, rest[1:])
            if tail and _SUBCOMMAND_RE.match(tail[0]):
                runs = "python" if _PYTHON_RE.match(tail[0]) else tail[0]
                parts.append(runs)
                if runs == "python" and len(tail) > 2 and tail[1] == "-m":
                    parts += ["-m", tail[2]]  # ``poetry run python -m pytest``
    return " ".join(parts)


def _is_inspection(words: Sequence[str], key: str) -> bool:
    program = _program_name(words[0]).rsplit("/", 1)[-1]
    if program not in INSPECTION_PROGRAMS and key not in INSPECTION_KEYS:
        return False
    return not any(
        w == f or w.startswith(f + "=") for w in words[1:] for f in _NOT_INSPECTION_FLAGS
    )


def _steps(command: str, depth: int = 0) -> list[Step]:
    steps: list[Step] = []
    for segment in _segments(_tokenize(command)):
        words = _strip_prefixes(segment)
        if not words:
            continue
        program = _program_name(words[0]).rsplit("/", 1)[-1]
        if (
            program in SHELLS
            and depth < _MAX_NESTING
            and len(words) > 2
            and words[1] in ("-c", "-lc", "-ic")
        ):
            nested = _steps(words[2], depth + 1)
            if nested:
                steps.extend(nested)
                continue
        key = _key(words)
        inspection = _is_inspection(words, key)
        significant = not (
            inspection or program in SETUP_PROGRAMS or program in HOUSEKEEPING_PROGRAMS
        )
        steps.append(Step(key, program, tuple(words[1:]), inspection, significant))
    return steps


def parse_command(command: str) -> ParsedCommand:
    """Normalize a shell command line. Rules are documented in docs/methodology.md.

    The line is split into simple commands (across ``;``, ``&&``, ``||``, pipes,
    subshells, ``if``/``for`` bodies and ``bash -c`` scripts; heredoc bodies are
    dropped). It is named after its **last significant** command: setup
    (``cd``, ``export``, ``echo``), housekeeping (``mkdir``, ``touch``, ``rm``)
    and read-only inspection (``cat``, ``tail``, ``grep``) are passed over, so
    ``mkdir -p out && pytest | tail -5`` is ``pytest``.
    """
    steps = _steps(command)
    if not steps:
        return ParsedCommand("", "", "", ())
    primary = next(
        (s for s in reversed(steps) if s.significant),
        None,
    )
    if primary is None:  # nothing but inspection and setup: name it after what was inspected
        for passed_over in (
            SETUP_PROGRAMS | OUTPUT_TRIMMERS | MATCH_FILTERS,
            SETUP_PROGRAMS | OUTPUT_TRIMMERS,
            SETUP_PROGRAMS,
            frozenset[str](),
        ):
            primary = next((s for s in reversed(steps) if s.program not in passed_over), None)
            if primary is not None:
                break
        assert primary is not None
    return ParsedCommand(
        key=primary.key,
        program=primary.program,
        last_program=steps[-1].program,
        tokens=primary.args,
        inspection=primary.inspection,
        steps=tuple(steps),
    )


def objectionable_step(parsed: ParsedCommand, reason: str = "") -> Step | None:
    """The step of a declined command line the human most plausibly objected to.

    In order: a step whose program the stated reason names; the first
    destructive step (``rm``, ``git push``, ...); the first step that is
    neither setup nor inspection; the primary step.
    """
    if not parsed.steps:
        return None
    words = set(re.findall(r"[\w.+-]+", reason.lower()))
    for step in parsed.steps:
        if step.program.lower() in words and step.program not in SETUP_PROGRAMS:
            return step
    for step in parsed.steps:
        if step.program in DESTRUCTIVE_PROGRAMS or step.key in DESTRUCTIVE_KEYS:
            return step
    for step in parsed.steps:
        if not step.inspection and step.program not in SETUP_PROGRAMS:
            return step
    return next((s for s in parsed.steps if s.key == parsed.key), parsed.steps[-1])


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
