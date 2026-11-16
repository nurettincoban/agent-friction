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
import hashlib
import posixpath
import re
import shlex
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from agent_friction.redact import redact

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
            # Windows file systems ignore case: a root with a drive letter matches any
            # spelling of itself (``c:\\Users\\Dev\\Shop`` is ``C:/users/dev/shop``).
            fold = bool(_DRIVE_RE.match(root))
            here, there = (absolute.casefold(), root.casefold()) if fold else (absolute, root)
            if here == there:
                return ""
            prefix = there.rstrip("/") + "/"
            if here.startswith(prefix):
                return absolute[len(prefix) :]
        return None

    def chdir(self, cwd: str | None, moves: Iterable[str]) -> str | None:
        """Absolute directory after applying ``cd``-style ``moves`` to ``cwd``.

        ``None`` if a move cannot be followed lexically (``cd -``, ``cd $DIR``):
        the caller then keeps what it knew.
        """
        here = cwd
        for move in moves:
            if not move or move == "-" or any(ch in move for ch in "$`*?"):
                return None
            there = self._absolute(move, here)
            if there is None:
                return None
            here = there
        return here

    def outside(self, raw: str, cwd: str | None = None) -> bool:
        """True if ``raw`` is a path that provably lies outside every root."""
        absolute = self._absolute(raw, cwd)
        return absolute is not None and self._relative(absolute) is None

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
    | {"timeout", "ionice", "doas", "caffeinate", "setsid", "chronic", "unbuffer"}
)
#: Options of those wrappers that take a separate value (``sudo -u alice pytest``).
WRAPPER_VALUE_OPTIONS: dict[str, frozenset[str]] = {
    "sudo": frozenset({"-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-T", "-U", "--user"})
    | {"--group", "--chdir", "--host", "--prompt", "--role", "--type", "--other-user"},
    "doas": frozenset({"-u", "-C"}),
    "xargs": frozenset({"-n", "-I", "-P", "-L", "-d", "-s", "-a", "-E", "-J", "-R"})
    | {"--max-args", "--max-procs", "--max-lines", "--delimiter", "--arg-file", "--replace"},
    "env": frozenset({"-u", "-C", "-S", "--unset", "--chdir", "--split-string"}),
    "timeout": frozenset({"-s", "-k", "--signal", "--kill-after"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "ionice": frozenset({"-c", "-n", "-p"}),
    "watch": frozenset({"-n", "-d", "--interval"}),
    "stdbuf": frozenset({"-i", "-o", "-e"}),
    "time": frozenset({"-f", "-o", "--format", "--output"}),
    "exec": frozenset({"-a"}),
}
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
    | {"ruff"}
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
    "kubectl": frozenset({"-n", "--namespace", "--context", "--kubeconfig", "--cluster", "-s"}),
    "helm": frozenset({"-n", "--namespace", "--kube-context", "--kubeconfig"}),
    "podman": frozenset({"--connection", "--root", "--log-level"}),
    "cargo": frozenset({"--manifest-path", "--color", "-Z"}),
    "uv": frozenset({"--python", "-p", "--cache-dir", "--color", "--group", "--only-group"})
    | {"--no-group", "--with", "--with-editable", "--with-requirements", "--extra", "--env-file"}
    | {"--package", "--index", "--default-index", "--index-url", "--extra-index-url", "--from"}
    | {"--script", "--python-preference", "--resolution", "--config-file", "--refresh-package"},
    "uvx": frozenset({"--python", "-p", "--from", "--with", "--index", "--index-url"}),
    "npx": frozenset({"-p", "--package", "-c", "--call", "-w", "--workspace"}),
    "bun": frozenset({"--cwd", "--config", "-c", "--env-file", "--filter"}),
    "pipx": frozenset({"--python", "--spec", "--pip-args", "--index-url"}),
    "hatch": frozenset({"-e", "--env", "-p", "--project", "--data-dir", "--cache-dir"}),
    "nox": frozenset({"-s", "--session", "-f", "--noxfile", "-p", "--python"}),
    "bundle": frozenset({"--gemfile"}),
    "bazel": frozenset({"--output_base", "--bazelrc"}),
    "gradle": frozenset({"-p", "--project-dir", "-b", "--build-file", "-c", "-I", "-g"}),
    "gradlew": frozenset({"-p", "--project-dir", "-b", "--build-file", "-c", "-I", "-g"}),
    "mvn": frozenset({"-f", "--file", "-pl", "--projects", "-s", "--settings", "-P", "-T"}),
    "dotnet": frozenset({"--project", "-p", "-c", "--configuration", "-f", "--framework"}),
    "docker": frozenset({"--context", "-H", "--host", "-c", "-l", "--log-level", "--config"})
    | {"--project-name", "--project-directory", "--env-file", "--profile", "--file"},
    "nx": frozenset({"--project", "--configuration", "-c"}),
    "turbo": frozenset({"--filter", "-F", "--cwd"}),
    "yarn": frozenset({"--cwd", "--mode"}),
    "deno": frozenset({"-c", "--config", "--import-map", "--lock"}),
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
    | {"hash", "pgrep", "pidof", "nc", "ss", "netstat", "whoami", "id", "uname", "hostname"}
    | {"ag", "ack", "fd", "fdfind", "pg_isready", "ping", "dig", "nslookup", "host"}
)
#: Read-only subcommands of otherwise significant programs.
INSPECTION_KEYS = frozenset(
    {"git diff", "git status", "git log", "git show", "git branch", "git blame", "git grep"}
    | {"git ls-files", "git rev-parse", "git remote", "git stash list", "git describe"}
    # exit-status probes: "is there a PR / is it installed / is it ignored / an ancestor?"
    | {"git check-ignore", "git merge-base", "git ls-remote", "git cat-file", "git config"}
    | {"git show-ref", "git symbolic-ref", "git name-rev", "git for-each-ref", "git worktree list"}
    | {"gh pr view", "gh pr status", "gh pr checks", "gh pr list", "gh pr diff", "gh run view"}
    | {"gh run list", "gh run watch", "gh issue view", "gh issue list", "gh repo view"}
    | {"gh auth", "pip show", "pip list", "pip freeze", "pip3 show", "pip3 list", "npm ls"}
    | {"npm list", "npm view", "npm outdated", "pnpm list", "pnpm ls", "pnpm why", "yarn list"}
    | {"yarn why", "uv pip show", "uv pip list", "uv pip freeze", "uv tree", "cargo tree"}
    | {"docker ps", "docker images", "docker inspect", "kubectl get", "kubectl describe"}
    | {"brew list", "systemctl status", "systemctl is-active"}
)
#: Flags that turn an inspection command into one with a meaningful result.
_NOT_INSPECTION_FLAGS = ("-i", "--in-place")
_IN_PLACE_EDITORS = frozenset({"sed", "awk", "sort"})
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
#: Tools whose first argument is a target the project defines, and how such a name looks.
TARGET_TOOLS = frozenset({"make", "gmake", "just", "task", "rake", "nox", "tox", "invoke"})
_TARGET_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9:_.-]*$")
_NUMERIC_RE = re.compile(r"^[-+]?[\d.,:]+[a-z%]{0,2}$", re.I)
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
    #: Directory changes in effect when the step runs, in order: earlier ``cd`` commands
    #: of the line, then its own ``-C`` / ``--prefix`` / ``--directory`` option.
    moves: tuple[str, ...] = ()


@dataclass(frozen=True)
class ParsedCommand:
    """Result of :func:`parse_command`."""

    key: str  # normalized command, "" if nothing recognisable
    program: str  # program of the primary step (basename)
    last_program: str  # final program of the command line (its status is reported)
    tokens: tuple[str, ...]  # arguments of the primary step, for path extraction
    inspection: bool = False  # read-only probing: a failure is not friction
    steps: tuple[Step, ...] = ()  # every simple command, in order
    #: The line's exit status is not the primary step's: it is piped into a filter, or
    #: followed by ``; ...`` or ``|| ...``. An exit code of 0 then proves nothing.
    masked: bool = False


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
        if token[:1] in ("<", ">"):  # redirection: drop it, its target and a leading fd number
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
        elif head == "command" and len(out) > 1 and out[1] in ("-v", "-V"):
            return ["command"]  # `command -v docker` asks whether docker exists
        elif head in WRAPPER_PROGRAMS:
            out = _strip_wrapper(head, out[1:])
        else:
            inner = _container_command(out)
            if not inner:
                break
            out = inner  # ``docker compose exec web pytest`` is about ``pytest``
    return out


#: Options of ``docker``/``compose``/``exec``/``run`` that take a separate value.
_CONTAINER_VALUE_OPTIONS = frozenset(
    {"-f", "--file", "-p", "--project-name", "--project-directory", "--profile", "--env-file"}
    | {"-H", "--host", "--context", "-c", "--config", "--log-level", "-e", "--env", "-u"}
    | {"--user", "-w", "--workdir", "--name", "-v", "--volume", "--publish", "--entrypoint"}
    | {"--index", "-l", "--label", "--detach-keys", "--scale", "--network", "-n", "--namespace"}
    | {"--container", "--kubeconfig", "--cluster", "--pod-running-timeout"}
)


def _skip_container_options(words: list[str]) -> list[str]:
    out = list(words)
    while out and out[0].startswith("-") and out[0] != "--":
        option = out.pop(0)
        if option in _CONTAINER_VALUE_OPTIONS and out:
            out.pop(0)
    return out


def _container_command(words: Sequence[str]) -> list[str]:
    """The command run *inside* a container, or ``[]`` if ``words`` is not such a call.

    ``docker compose exec|run <service> CMD``, ``docker exec <container> CMD`` and
    ``kubectl exec <pod> -- CMD``. Where a check runs does not change what it is.
    """
    head = _program_name(words[0]) if words else ""
    rest = list(words[1:])
    if head == "kubectl":
        if "exec" not in rest or "--" not in rest or rest.index("exec") > rest.index("--"):
            return []
        return rest[rest.index("--") + 1 :]
    if head in ("docker", "podman"):
        rest = _skip_container_options(rest)
        if rest and rest[0] == "compose":
            head, rest = "docker-compose", rest[1:]
        elif rest and rest[0] == "exec":
            rest = _skip_container_options(rest[1:])
            return rest[1:]  # after the container name
        else:
            return []
    if head in ("docker-compose", "podman-compose"):
        rest = _skip_container_options(rest)
        if not rest or rest[0] not in ("exec", "run"):
            return []
        rest = _skip_container_options(rest[1:])
        return rest[1:]  # after the service name
    return []


def _strip_wrapper(wrapper: str, rest: list[str]) -> list[str]:
    """Drop a wrapper's own options (with their values) and positional arguments."""
    takes_value = WRAPPER_VALUE_OPTIONS.get(wrapper, frozenset())
    out = rest
    while out and out[0].startswith("-") and out[0] != "-":
        option = out.pop(0)
        if option == "--":
            break
        if option in takes_value and out:
            out.pop(0)  # ``sudo -u alice``, ``xargs -n 1``: the value is not the command
    if wrapper == "timeout" and out:
        out.pop(0)  # the duration
    if wrapper == "env":
        while out and _ENV_ASSIGN_RE.match(out[0]):
            out.pop(0)
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


#: Placeholders for code passed on the command line or on standard input.
INLINE = "<inline>"
STDIN = "<stdin>"
_INLINE_FLAGS = frozenset({"-c", "-e", "-lc", "-ic", "--eval", "-p", "--print", "-r", "-pe", "-ne"})
#: Scripts that are themselves command dispatchers.
_SUBCOMMAND_SCRIPTS = frozenset({"manage.py", "artisan"})


def is_inline_code(key: str) -> bool:
    """A key that stands for ad-hoc code (``python <inline>``), not a repeatable command."""
    return INLINE in key or STDIN in key


def _key(words: Sequence[str]) -> str:
    """Normalized identity of one simple command (documented in docs/methodology.md)."""
    return _sanitize(_raw_key(words))


def _sanitize(key: str) -> str:
    """A key never contains a number-only word or anything the redactor would mask.

    The key is cut at the first such word; if the executable itself is one,
    there is no key.
    """
    kept: list[str] = []
    for word in key.split(" "):
        if word in ("-m", INLINE, STDIN):
            kept.append(word)
            continue
        if not word or _NUMERIC_RE.match(word) or redact(word) != word:
            break
        kept.append(word)
    while kept and kept[-1] == "-m":
        kept.pop()
    return " ".join(kept)


def _raw_key(words: Sequence[str]) -> str:
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
        if rest and rest[0] in _INLINE_FLAGS:
            return f"{program} {INLINE}"  # ``python -c``, ``node -e``: code, not a command
        if rest and rest[0] == "-":
            return f"{program} {STDIN}"  # ``python - <<EOF``: a script on standard input
        if rest and _looks_like_script(rest[0]):
            script = _program_name(rest[0])
            # ``python manage.py migrate``, ``php artisan migrate``: the subcommand counts
            dispatcher = script.rsplit("/", 1)[-1] in _SUBCOMMAND_SCRIPTS
            if dispatcher and len(rest) > 1 and _SUBCOMMAND_RE.match(rest[1]):
                return f"{program} {script} {rest[1]}"
            return f"{program} {script}"
        if base == "php" and rest and rest[0] == "artisan":
            sub = rest[1] if len(rest) > 1 and _SUBCOMMAND_RE.match(rest[1]) else ""
            return f"{program} artisan {sub}".strip()
    if base not in SUBCOMMAND_TOOLS or "/" in program:
        return program  # unknown tool or repo script: the executable alone, never its arguments
    rest = _skip_options(base, rest)
    parts = [program]
    if rest and base in TARGET_TOOLS and _TARGET_RE.match(rest[0]):
        parts.append(rest[0])  # ``make deploy-ZebraCustomer``: targets are names in any case
    elif rest and _SUBCOMMAND_RE.match(rest[0]):
        parts.append(rest[0])
        if base == "yarn" and rest[0] == "workspace" and len(rest) > 2:
            # ``yarn workspace <name> <script>``: which package, and what is run in it
            parts.append(rest[1])
            if _SUBCOMMAND_RE.match(rest[2]):
                parts.append(rest[2])
        elif rest[0] in CONTINUING_SUBCOMMANDS.get(base, ()):
            tail = _skip_options(base, rest[1:])
            named = rest[0] in ("run", "run-script") and base in ("npm", "pnpm", "yarn", "bun")
            word_re = _TARGET_RE if named else _SUBCOMMAND_RE  # scripts are project names
            if tail and word_re.match(tail[0]):
                runs = "python" if _PYTHON_RE.match(tail[0]) else tail[0]
                parts.append(runs)
                if runs == "python" and len(tail) > 2 and tail[1] == "-m":
                    parts += ["-m", tail[2]]  # ``poetry run python -m pytest``
    return " ".join(parts)


def _is_inspection(words: Sequence[str], key: str) -> bool:
    program = _program_name(words[0]).rsplit("/", 1)[-1]
    if key == "terraform plan" and any(w.lstrip("-") == "detailed-exitcode" for w in words):
        return True  # exit 2 means "there are changes": an answer, not a failure
    if program not in INSPECTION_PROGRAMS and key not in INSPECTION_KEYS:
        return False
    if program not in _IN_PLACE_EDITORS:
        return True
    return not any(
        w == f or w.startswith(f + "=") for w in words[1:] for f in _NOT_INSPECTION_FLAGS
    )


#: Options with which a tool is told to work in another directory.
_DIR_OPTIONS: dict[str, frozenset[str]] = {
    "git": frozenset({"-C"}),
    "make": frozenset({"-C", "--directory"}),
    "npm": frozenset({"--prefix", "-C"}),
    "pnpm": frozenset({"--dir", "-C", "--prefix"}),
    "yarn": frozenset({"--cwd"}),
    "bun": frozenset({"--cwd"}),
    "uv": frozenset({"--directory", "--project"}),
    "cargo": frozenset({"-C"}),
    "poetry": frozenset({"-C", "--directory", "-P", "--project"}),
    "pip": frozenset(),
    "just": frozenset({"-d", "--working-directory"}),
    "terraform": frozenset({"-chdir"}),
    "docker": frozenset({"--project-directory"}),
    "cmake": frozenset({"-S", "--build"}),
    "go": frozenset({"-C"}),
    "tar": frozenset(),
}


def _own_directory(program: str, args: Sequence[str]) -> list[str]:
    """The directory a tool was pointed at with ``-C dir`` / ``--prefix=dir``, if any."""
    options = _DIR_OPTIONS.get(program)
    if not options:
        return []
    for index, word in enumerate(args):
        if not word.startswith("-"):
            break  # options of the tool itself come before its subcommand
        name, eq, value = word.partition("=")
        if name in options:
            if eq:
                return [value]
            if index + 1 < len(args):
                return [args[index + 1]]
    return []


def _steps(command: str, depth: int = 0, moves: tuple[str, ...] = ()) -> list[Step]:
    steps: list[Step] = []
    for segment in _segments(_tokenize(command)):
        words = _strip_prefixes(segment)
        if not words:
            continue
        program = _program_name(words[0]).rsplit("/", 1)[-1]
        if program in ("cd", "pushd"):
            target = next((w for w in words[1:] if not w.startswith("-") or w == "-"), "~")
            moves = (*moves, target)
        if (
            program in SHELLS
            and depth < _MAX_NESTING
            and len(words) > 2
            and words[1] in ("-c", "-lc", "-ic")
        ):
            nested = _steps(words[2], depth + 1, moves)
            if nested:
                steps.extend(nested)
                continue
        key = _key(words)
        inspection = _is_inspection(words, key)
        significant = not (
            inspection or program in SETUP_PROGRAMS or program in HOUSEKEEPING_PROGRAMS
        )
        where = (*moves, *_own_directory(program, words[1:]))
        steps.append(Step(key, program, tuple(words[1:]), inspection, significant, where))
    return steps


def _masked(command: str, program: str) -> bool:
    try:
        return _status_masked(command, program)
    except (IndexError, ValueError, RecursionError):
        return False


_MASKING = frozenset({"|", "|&", "||", "&"})


def _status_masked(command: str, program: str, depth: int = 0) -> bool:
    """Is the exit status of the last ``program`` step hidden by what follows it?

    ``pytest | tail -5``, ``pytest; echo done`` and ``pytest || true`` report the
    status of ``tail``, ``echo`` and (at worst) ``true``. ``pytest && echo ok``
    does not hide a failure, and ``set -o pipefail`` restores a pipeline's.
    """
    tokens = _tokenize(command)
    pieces: list[tuple[list[str], str]] = []  # (words of a simple command, operator after it)
    current: list[str] = []
    for token in tokens:
        if token in _SEPARATORS:
            if current and token not in ("(", ")"):
                pieces.append((current, token))
                current = []
            elif pieces and not current and token not in ("(", ")") and pieces[-1][1] == "":
                pieces[-1] = (pieces[-1][0], token)
            continue
        current.append(token)
    if current:
        pieces.append((current, ""))
    index = -1
    for position, (words, _) in enumerate(pieces):
        stripped = _strip_prefixes(_segments(words)[0] if _segments(words) else [])
        if not stripped:
            continue
        name = _program_name(stripped[0]).rsplit("/", 1)[-1]
        if name == program or (stripped and _key(stripped).split(" ")[0] == program):
            index = position
        elif (
            name in SHELLS
            and depth < _MAX_NESTING
            and len(stripped) > 2
            and stripped[1] in ("-c", "-lc", "-ic")
            and _status_masked(stripped[2], program, depth + 1)
        ):
            return True
    if index < 0:
        return False
    after = [operator for _, operator in pieces[index:]]
    following = [words for words, _ in pieces[index + 1 :]]
    real = [w for w in following if _strip_prefixes(_segments(w)[0] if _segments(w) else [])]
    if not real:
        return False  # nothing runs after it (`pytest;`, `...; then pytest; fi`)
    if after[0] in ("|", "|&") and "pipefail" in command:
        return False
    return after[0] in _MASKING or any(op in (";", "||") for op in after[:-1])


def parse_command(command: str) -> ParsedCommand:
    """Normalize a shell command line. Rules are documented in docs/methodology.md.

    The line is split into simple commands (across ``;``, ``&&``, ``||``, pipes,
    subshells, ``if``/``for`` bodies and ``bash -c`` scripts; heredoc bodies are
    dropped). It is named after its **last significant** command: setup
    (``cd``, ``export``, ``echo``), housekeeping (``mkdir``, ``touch``, ``rm``)
    and read-only inspection (``cat``, ``tail``, ``grep``) are passed over, so
    ``mkdir -p out && pytest | tail -5`` is ``pytest``.
    """
    try:
        steps = _steps(command)
    except (IndexError, ValueError, RecursionError):  # never let one odd line stop a scan
        steps = []
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
        masked=_masked(command, primary.program),
    )


def objectionable_step(parsed: ParsedCommand, reason: str = "") -> Step | None:
    """The step of a declined command line the human most plausibly objected to.

    In order: a step that does something (not read-only inspection) whose
    program the stated reason names; the first
    destructive step (``rm``, ``git push``, ...); the first step that is
    neither setup nor inspection; the primary step.
    """
    if not parsed.steps:
        return None
    words = set(re.findall(r"[\w.+-]+", reason.lower()))
    for step in parsed.steps:
        named = step.program.lower() in words and step.program not in SETUP_PROGRAMS
        # Looking is never what was objected to while the line also changes something.
        if named and not step.inspection:
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


# ---- what a command is for ---------------------------------------------------------------

#: Words of a key that say what job a command does. ``pytest``, ``make test`` and
#: ``npm run test:unit`` are all test runners; one can stand in for another.
ROLE_WORDS: dict[str, frozenset[str]] = {
    "test": frozenset(
        {"test", "tests", "pytest", "jest", "vitest", "mocha", "rspec", "unittest", "tox", "nox"}
        | {"phpunit", "ctest", "e2e", "spec", "playwright", "cypress", "karma", "ava", "nextest"}
    ),
    "build": frozenset({"build", "compile", "webpack", "assemble", "dist", "bundle", "package"}),
    "lint": frozenset(
        {"lint", "eslint", "ruff", "flake8", "pylint", "clippy", "rubocop", "vet", "stylelint"}
        | {"shellcheck", "golangci-lint", "biome"}
    ),
    "typecheck": frozenset({"mypy", "pyright", "tsc", "typecheck", "type-check", "types", "ty"}),
    "format": frozenset({"format", "fmt", "prettier", "black", "gofmt", "rustfmt", "isort"}),
}
#: Subcommands and targets that prepare the environment rather than do the job.
_SETUP_WORDS = frozenset(
    {"install", "ci", "sync", "add", "bootstrap", "setup", "deps", "restore", "fetch", "venv"}
    | {"download", "init", "migrate", "upgrade", "update", "i", "up", "login", "generate"}
)
_SETUP_PROGRAMS = frozenset({"pip", "pip3", "apt", "apt-get", "brew", "conda", "mamba", "rustup"})


def _key_words(key: str) -> list[str]:
    return [part for word in key.split() for part in re.split(r"[:/]", word) if part]


def command_role(key: str) -> str | None:
    """``test``, ``build``, ``lint``, ``typecheck`` or ``format``; ``None`` if unknown."""
    for word in reversed(_key_words(key)):
        stem = word.rsplit(".", 1)[0] if _SCRIPT_RE.search(word) else word
        for role, names in ROLE_WORDS.items():
            if stem in names:
                return role
    return None


def is_setup_command(key: str) -> bool:
    """Installing dependencies, creating environments, starting services, migrating."""
    words = key.split()
    if not words:
        return False
    if words[0] in _SETUP_PROGRAMS or words[:3] == ["python", "-m", "pip"]:
        return True
    if words[:3] == ["python", "-m", "venv"]:
        return True
    if words[0] in ("git", "gh", "glab"):
        return False
    return words[0] in SUBCOMMAND_TOOLS and any(w in _SETUP_WORDS for w in words[1:])


#: Targets and scripts that run every check of a project.
AGGREGATE_WORDS = frozenset(
    {"check", "checks", "ci", "verify", "validate", "all", "precommit", "pre-commit", "qa"}
    | {"check-all", "test-all", "lint-all", "presubmit", "prepush", "pre-push"}
)


def is_aggregate_check(key: str) -> bool:
    """``make check``, ``npm run ci``, ``pre-commit run``: one command for several jobs."""
    words = key.split()
    if not words or words[0] in ("git", "gh", "glab", "cargo", "go", "terraform"):
        return False  # `cargo check`, `terraform validate` are single jobs of their own
    if words[0] == "pre-commit":
        return len(words) == 1 or words[1] == "run"
    if command_role(words[0]) is not None:
        return False  # `ruff check`, `biome ci`: a subcommand of one tool
    return any(part in AGGREGATE_WORDS for part in _key_words(" ".join(words[1:])))


def is_replacement(failed: str, other: str) -> bool:
    """Can ``other`` stand in for ``failed``? (``make test`` for ``pytest``.)

    True when both do the same job (same role) or one wraps the other's tool
    (``uv run pytest`` for ``pytest``). Never for setup, version-control,
    housekeeping or inspection commands.
    """
    if not failed or not other or failed == other or is_setup_command(other):
        return False
    program = other.split()[0]
    if program in ("git", "gh", "glab") or program in HOUSEKEEPING_PROGRAMS | SETUP_PROGRAMS:
        return False
    role = command_role(failed)
    if role is not None and role == command_role(other):
        return True
    return failed.split()[-1] == other.split()[-1] and failed.split()[-1] not in (
        "run",
        INLINE,
        STDIN,
    )


# ---- keys without free-form names (include_excerpts=False) -------------------------------

#: Subcommands and target names that say nothing about a particular project.
_PUBLIC_WORDS = frozenset(
    {"commit", "push", "pull", "status", "diff", "log", "show", "checkout", "switch", "branch"}
    | {"merge", "rebase", "reset", "clone", "tag", "restore", "clean", "blame", "grep", "apply"}
    | {"cherry-pick", "revert", "bisect", "mv", "rm", "pop", "list", "drop", "save", "prune"}
    | {"start", "dev", "serve", "preview", "watch", "check", "doc", "docs", "bench", "publish"}
    | {"remove", "uninstall", "new", "create", "lock", "export", "version", "info", "view"}
    | {"down", "logs", "ps", "images", "rmi", "stop", "restart", "kill", "inspect", "cp", "top"}
    | {"get", "describe", "delete", "rollout", "scale", "plan", "destroy", "validate", "output"}
    | {"mod", "tidy", "work", "tool", "all", "release", "coverage", "cov", "unit", "integration"}
    | {"debug", "prod", "production", "staging", "local", "s3", "ec2", "iam", "sts", "lambda"}
    | {"auth", "config", "configure", "cache", "audit", "outdated", "prepare", "typegen"}
    | {"codegen", "seed", "shell", "console", "routes", "db", "storage", "deploy", "help", "why"}
    | {"exec", "run", "run-script", "dlx", "x", "pip", "compose", "buildx", "pr", "issue", "repo"}
    | {"workflow", "mr", "stash", "remote", "worktree", "submodule", "task", "workspace"}
    | {"revision", "downgrade", "history", "current", "makemigrations", "runserver", "head"}
    | {
        "-m",
        "-c",
        "<stdin>",
        "<inline>",
        "manage.py",
        "artisan",
        "http.server",
        "json.tool",
        "pdb",
        "timeit",
        "ensurepip",
        "django",
    }
    | {"twine", "pre_commit", "compileall", "check-ignore", "merge-base", "rev-parse"}
    | {"ls-files", "ls-remote", "cat-file", "show-ref", "symbolic-ref", "name-rev", "ls"}
    | {"for-each-ref", "freeze", "tree", "is-active", "checks", "status"}
)
#: Well-known executables (beyond the tables above) that may open a key.
_PUBLIC_PROGRAMS = frozenset(
    {"python", "coverage", "uvicorn", "gunicorn", "flask", "sh"}
    | {"node", "ruby", "perl", "php", "java", "javac", "gcc", "g++", "clang", "cc", "ld", "ar"}
    | {"curl", "wget", "ssh", "scp", "rsync", "tar", "zip", "unzip", "gzip", "psql", "mysql"}
    | {"sqlite3", "redis-cli", "mongosh", "open", "xdg-open", "code", "vim", "nano", "ed"}
    | {"ts-node", "tsx", "esbuild", "rollup", "vite", "next", "nuxt", "ng", "expo", "storybook"}
    | {"tsc", "babel", "sass", "pre-commit", "husky", "lint-staged", "codespell", "bandit"}
    | {"sphinx-build", "mkdocs", "jupyter", "ipython", "celery", "streamlit", "scrapy", "dbt"}
    | {"ansible", "ansible-playbook", "vagrant", "packer", "pulumi", "skaffold", "minikube"}
    | {"kind", "k3d", "act", "jq", "yq", "xargs", "tee", "date", "git-lfs", "ctest", "cmake"}
)
#: Role words that are job names, not executables (``make test``, never a bare ``test``).
_JOB_NAMES = frozenset(
    {"test", "tests", "build", "lint", "format", "fmt", "types", "spec", "dist", "package"}
    | {"bundle", "compile", "e2e", "typecheck", "type-check", "assemble"}
)
PUBLIC_PROGRAMS = (
    _PUBLIC_PROGRAMS
    | SUBCOMMAND_TOOLS
    | INTERPRETERS
    | INSPECTION_PROGRAMS
    | HOUSEKEEPING_PROGRAMS
    | DESTRUCTIVE_PROGRAMS
    | SETUP_PROGRAMS
    | WRAPPER_PROGRAMS
    | _SETUP_PROGRAMS
    | (frozenset(word for names in ROLE_WORDS.values() for word in names) - _JOB_NAMES)
)
PUBLIC_VOCABULARY = PUBLIC_PROGRAMS | _PUBLIC_WORDS | _SETUP_WORDS | _JOB_NAMES


#: Hex digits of the SHA-256 shown in a placeholder.
PUBLIC_HASH_DIGITS = 6


def _placeholder(word: str, position: int, previous: str) -> str:
    if position == 0:
        kind = "tool"
    elif previous == "-m":
        kind = "module"
    elif "/" in word or _SCRIPT_RE.search(word):
        kind = "script"
    else:
        kind = "target"
    return f"<{kind}:{hashlib.sha256(word.encode()).hexdigest()[:PUBLIC_HASH_DIGITS]}>"


def public_key(key: str, keep: Callable[[str], bool] | None = None) -> str:
    """``key`` with every project-specific name replaced by a short stable hash.

    Words from a fixed public vocabulary (well-known programs, subcommands and
    target names such as ``test`` or ``build``) are kept; any other word (a
    script path, a make target, an npm script, an unknown executable) becomes
    ``<kind:hash>``. The same name always gives the same placeholder, so
    grouping across sessions and reports still works. ``keep`` may vouch for a
    word that is shown anyway (a script path inside the repository).
    """
    out: list[str] = []
    previous = ""
    for position, word in enumerate(key.split()):
        parts = [p for p in word.split(":") if p]
        if keep is not None and keep(word):
            known = True
        elif position == 0:
            known = word in PUBLIC_PROGRAMS
        else:
            known = word in PUBLIC_VOCABULARY or (
                bool(parts) and all(p in PUBLIC_VOCABULARY for p in parts)
            )
        if known:
            out.append(word)
        else:
            out.append(_placeholder(word, position, previous))
        previous = word
    return " ".join(out)


# ---- shell commands that change files ----------------------------------------------------

_WRITE_FLAGS = frozenset({"--fix", "--write", "--fix-only", "--unsafe-fixes", "--in-place"})
_CHECK_FLAGS = frozenset({"--check", "--diff", "--dry-run", "--list-different", "-l", "-d"})
#: Formatters that only rewrite files when told to.
_NEEDS_WRITE_FLAG = frozenset({"prettier", "gofmt", "clang-format", "biome", "eslint"})
_GIT_REWRITES = frozenset(
    {"git apply", "git checkout", "git restore", "git reset", "git revert", "git merge"}
    | {"git rebase", "git cherry-pick", "git pull", "git stash", "git stash pop"}
    | {"git stash apply", "git switch", "git am"}
)


#: Calls with which ad-hoc code writes, moves or deletes files (Python, Node, Ruby, shell).
WRITE_CALL_RE = re.compile(
    r"\bwrite_text\b|\bwrite_bytes\b|\.write\(|\.writelines\("
    r"|\bopen\([^)\n]*,\s*['\"][wax]b?\+?['\"]|\bopen\([^)\n]*mode\s*=\s*['\"][wax]"
    r"|\bwriteFile(?:Sync)?\b|\bappendFile(?:Sync)?\b|\bfs\.(?:write|rename|unlink|rm|copyFile)"
    r"|\bFile\.(?:write|open|rename|delete)\b|\bshutil\.(?:copy\w*|move|rmtree)\b"
    r"|\bos\.(?:replace|rename|remove|unlink|makedirs)\b|\.unlink\(|\.rename\(|\.replace\([^,)]*\)$"
    r"|\.touch\(|\bfileinput\b|\bjson\.dump\(|\byaml\.(?:safe_)?dump\(",
    re.M,
)
#: Tools whose whole purpose is to rewrite source files.
CODEMOD_PROGRAMS = frozenset(
    {"jscodeshift", "comby", "codemod", "sd", "rpl", "pyupgrade", "autoflake", "autopep8"}
    | {"2to3", "libcst", "ast-grep", "sg", "semgrep", "rector", "ts-morph", "fastmod", "gofix"}
    | {"unimport", "add-trailing-comma", "docformatter", "yapf", "dprint", "taplo"}
)
#: File commands that change the tree when pointed at project files.
_TREE_PROGRAMS = frozenset({"mv", "cp", "rm", "ln", "install", "rsync", "unzip", "tar", "truncate"})
_GIT_TREE_KEYS = frozenset({"git mv", "git rm", "git clean"})


def is_shell_edit(parsed: ParsedCommand, command: str) -> bool:
    """True if a shell command line rewrites files in place (as far as its words tell).

    ``sed -i``, ``perl -pi``, formatters (``ruff format``, ``black``,
    ``prettier --write``, ``gofmt -w``, ``cargo fmt``), ``--fix``/``--write``
    flags, ``patch``, ``git apply/checkout/restore/stash/mv/rm``, codemods
    (``jscodeshift``, ``comby``), ``mv``/``cp``/``rm`` on project files,
    ``tee`` and ``>`` redirections to a file, and inline or heredoc code that
    calls something that writes (``python3 - <<EOF ... write_text ... EOF``,
    ``node -e "fs.writeFileSync(...)"``). Such a command is a change, like an Edit.
    """
    if any(is_inline_code(s.key) for s in parsed.steps) and WRITE_CALL_RE.search(command):
        return True
    for step in parsed.steps:
        args = step.args
        flags = set(args)
        program = step.program
        if flags & _WRITE_FLAGS:
            return True
        words = step.key.split()
        if program in CODEMOD_PROGRAMS or (words and words[-1] in CODEMOD_PROGRAMS):
            return True
        if step.key in _GIT_TREE_KEYS:
            return True
        if program in _TREE_PROGRAMS and any(_is_source_target(a) for a in args if a[:1] != "-"):
            return True
        if program in ("sed", "gsed") and any(a == "--in-place" or a[:2] == "-i" for a in args):
            return True
        if program == "perl" and any(re.match(r"^-[a-z0-9]*i", a) for a in args):
            return True
        if program == "patch" or step.key in _GIT_REWRITES:
            return True
        if program == "tee" and any(_is_source_target(a) for a in args if a[:1] != "-"):
            return True
        if program in _NEEDS_WRITE_FLAG:
            if flags & {"-w", "-i", "--write"}:
                return True
        elif command_role(step.key) == "format" and not flags & _CHECK_FLAGS:
            return True
        # `uv run ruff format .`: the key stops at the tool behind the wrapper
        if words and words[-1] in args and not flags & _CHECK_FLAGS:
            follows = args[args.index(words[-1]) + 1 : args.index(words[-1]) + 2]
            if follows and command_role(f"{words[-1]} {follows[0]}") == "format":
                return True
    tokens = _tokenize(command)
    for index, token in enumerate(tokens):
        target = tokens[index + 1] if index + 1 < len(tokens) else ""
        if token in (">", ">>", ">|") and _is_source_target(target):
            return True
    return False


_SCRATCH_RE = re.compile(r"\.(?:log|out|txt|tmp|pid|err)$|^&|^/dev/|^/tmp/|^/var/|^\$TMP", re.I)


#: Words that make a command a setup step wherever they stand (`npx playwright install`).
_SETUP_ARGS = frozenset({"install", "ci", "bootstrap", "setup", "migrate", "up", "login", "sync"})
_ENV_FILE_RE = re.compile(r"^\.env(?:\..+)?$|\.local(?:\.\w+)?$")
_TEMPLATE_FILE_RE = re.compile(r"[._-](?:example|sample|template|dist|defaults?)(?:\.\w+)?$", re.I)


def is_setup_step(step: Step) -> bool:
    """Does ``step`` prepare the environment: install, start a service, migrate, log in?"""
    if is_setup_command(step.key):
        return True
    if step.program in ("git", "gh", "glab") or "/" in step.program:
        return False
    words = [a for a in step.args if a[:1] != "-"][:3]
    return step.program in SUBCOMMAND_TOOLS and any(w in _SETUP_ARGS for w in words)


def env_copy(step: Step) -> str | None:
    """``cp .env.example .env``: creating local configuration from a template.

    Returns the command as it can be shown (program and file names), or ``None``.
    That is a setup step, not a change to the code, although it writes a file.
    """
    if step.program not in ("cp", "ln", "mv", "install"):
        return None
    names = [a for a in step.args if a[:1] != "-"]
    if len(names) != 2 or not all(re.fullmatch(r"[\w./-]+", n) for n in names):
        return None
    source, target = (n.rsplit("/", 1)[-1] for n in names)
    if _ENV_FILE_RE.search(target) or _TEMPLATE_FILE_RE.search(source):
        return f"{step.program} {names[0]} {names[1]}"
    return None


#: Commands that only show or send something; nothing local is different afterwards.
REPORT_ONLY_KEYS = frozenset(
    {"docker logs", "docker compose logs", "kubectl logs", "git fetch", "git push"}
)


def may_change_files(key: str) -> bool:
    """Could a successful run of ``key`` have changed something, as far as its name tells?

    Everything that is not a check: a script, a generator, a build, an install,
    a commit. Tests, linters and type checkers (and aggregate targets that run
    them) only report; a formatter or ``--fix`` run is recognised separately.
    """
    if not key or is_aggregate_check(key) or key in REPORT_ONLY_KEYS:
        return False
    return command_role(key) not in ("test", "lint", "typecheck")


def _is_source_target(target: str) -> bool:
    """A file a redirection writes that is plausibly part of the project, not a log."""
    return bool(target) and not _SCRATCH_RE.search(target)


_ENV_PREFIX_RE = re.compile(r"^(?:\s*[A-Za-z_][A-Za-z0-9_]*=(?:'[^']*'|\"[^\"]*\"|\S*)\s+)+")


def normalize_argv(command: str) -> str:
    """A command line with whitespace collapsed and leading ``VAR=value`` dropped.

    Two runs are "the same command again" only if this text is identical.
    """
    text = re.sub(r"\s+", " ", command).strip()
    return _ENV_PREFIX_RE.sub("", text)
