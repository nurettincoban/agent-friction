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
import itertools
import os
import posixpath
import re
import shlex
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from agent_friction.redact import redact

_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_MAX_PATH_CHARS = 1024
#: A Windows drive as Git Bash (``/c/Users``), WSL (``/mnt/c/Users``) and Cygwin
#: (``/cygdrive/c/Users``) spell it.
_UNIX_DRIVE_RE = re.compile(r"^/(?:mnt/|cygdrive/)?([A-Za-z])(?=/|$)")


def to_posix(path: str) -> str:
    """Normalize Windows separators and drive-letter case so prefix matching works anywhere."""
    if _DRIVE_RE.match(path):
        return path[0].upper() + path[1:].replace("\\", "/")
    if "\\" in path and "/" not in path:
        return path.replace("\\", "/")
    return path


def _is_abs(path: str) -> bool:
    return path.startswith("/") or bool(_DRIVE_RE.match(path))


#: Where agents keep worktrees inside a repository: Claude Code's
#: ``.claude/worktrees/<name>/`` and the common ``.worktrees/<name>/``.
_NESTED_WORKTREE_RE = re.compile(r"^(?:\.claude/worktrees|\.worktrees)/[^/]+(?:/|$)")


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
        self._not_ours: set[str] = set()  # directories `adopt` has looked at and turned down
        self._spellings: dict[str, str] = {}  # under a Windows root: one spelling per name
        self.drives = any(_DRIVE_RE.match(root) for root in roots)
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
        base = to_posix(cwd) if cwd else self.primary
        if not _is_abs(path):
            if not _is_abs(base):
                return None
            path = posixpath.join(base, path)
        elif self.drives or _DRIVE_RE.match(base):
            # A Unix-style shell on Windows: `cd /c/Users/me/repo` is `C:/Users/me/repo`.
            drive = _UNIX_DRIVE_RE.match(path)
            if drive is not None:
                path = f"{drive.group(1).upper()}:/{path[drive.end() :].lstrip('/')}"
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
                # A worktree kept inside the repository is the repository, also after
                # it was merged and removed: `git worktree list` no longer knows it.
                rel = _NESTED_WORKTREE_RE.sub("", absolute[len(prefix) :], count=1)
                return self._one_spelling(rel) if fold else rel
        return None

    def _one_spelling(self, rel: str) -> str:
        """``rel`` as it is spelled on disk, or as it was first seen.

        Under a Windows root ``SRC\\App\\util.py`` and ``src\\app\\util.py`` are one
        file. Each name is given one spelling, directory by directory: the one the
        working tree has, if the repository is here, and otherwise the first one met.
        """
        parts: list[str] = []
        for part in rel.split("/"):
            key = "/".join(parts).casefold() + "/" + part.casefold()
            if key not in self._spellings:
                self._spellings[key] = self._disk_name(parts, part) or part
            parts.append(self._spellings[key])
        return "/".join(parts)

    def _disk_name(self, parents: Sequence[str], name: str) -> str | None:
        try:
            directory = self.repo.joinpath(*parents)
            if (directory / name).exists():
                return name
            names = [entry.name for entry in directory.iterdir()]
            same = [e for e in names if e.casefold() == name.casefold()]
        except OSError:
            return None
        return same[0] if len(same) == 1 else None

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

    def absolute(self, raw: str, cwd: str | None = None) -> str | None:
        """``raw`` as a normalized absolute path, or ``None`` if that cannot be told."""
        return self._absolute(raw, cwd)

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

    def adopt(self, cwd: str | None) -> bool:
        """Take a directory that *is* the repo under another name as one more root.

        A session started through a symbolic link (``~/proj`` for ``/data/proj``)
        records the link's spelling. If ``cwd`` exists here and its real path is
        the repository's, or lies inside it, the spelling that stands for the
        repo root becomes a root. Returns True if ``cwd`` is inside the repo then.
        """
        absolute = self._absolute(cwd, None) if cwd else None
        if absolute is None or self._relative(absolute) is not None:
            return absolute is not None
        if absolute in self._not_ours:
            return False
        self._not_ours.add(absolute)
        try:
            if not Path(absolute).is_dir():
                return False
            real = os.path.realpath(absolute)
            root = os.path.realpath(self.primary)
        except (OSError, ValueError):
            return False
        alias = absolute
        if real != root:
            below = real[len(root) :] if real.startswith(root.rstrip("/") + "/") else ""
            below = "/" + below.lstrip("/") if below else ""
            if not below or not absolute.endswith(below):
                return False
            alias = absolute[: -len(below)]
            try:
                if os.path.realpath(alias) != root:
                    return False
            except (OSError, ValueError):
                return False
        self.roots = tuple(sorted({*self.roots, alias}, key=lambda r: (-len(r), r)))
        return True

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
    {"sudo", "time", "gtime", "env", "nohup", "command", "exec", "nice", "xargs", "watch"}
    | {"stdbuf"}
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
    "gtime": frozenset({"-f", "-o", "--format", "--output"}),
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
    | {"type", "grep", "egrep", "fgrep", "rg", "test", "[", "[[", "diff", "cmp", "true", "false"}
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
MATCH_FILTERS = frozenset({"grep", "egrep", "fgrep", "rg", "diff", "cmp", "test", "[", "[["})
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
# ``py`` is the Windows launcher: `py -m pytest` is `python -m pytest`
_PYTHON_RE = re.compile(r"^(?:(?:python|pypy)[\d.]*|py)$")
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
    #: Its output never reaches the transcript: ``>/dev/null 2>&1``, ``&>/dev/null``.
    #: Nothing in the line's output can then be this step's verdict.
    silenced: bool = False
    #: The file its standard output is redirected into (``cat > notes.md <<EOF``), if any.
    into: str = ""
    #: Wrappers stripped in front of the program, by base name (``time``, ``sudo``).
    wrappers: tuple[str, ...] = ()
    #: The workspace package it was told to run in (``pnpm --filter web``, ``yarn
    #: workspace web``), if the selector reads as a directory. Where that is on disk
    #: (``web/``, ``packages/web/``) only the repository can say.
    workspace: str = ""
    #: The container call it was run through, without options: ``docker compose exec
    #: web``, ``docker exec``, ``kubectl exec``. Empty for a command run on the host.
    container: str = ""


#: Programs that, with their output redirected into a file (or, for ``tee``, given one),
#: are a way of writing that file and nothing else.
FILE_WRITERS = frozenset({"cat", "tee", "echo", "printf"})


def written_file(step: Step) -> str:
    """The file ``step`` writes if it is a plain write: ``cat > f <<EOF``, ``tee f``."""
    if step.program not in FILE_WRITERS:
        return ""
    if step.program == "tee":
        return next((a for a in step.args if not a.startswith("-")), "")
    return step.into


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


#: An armored block (a certificate, a key) passed as an argument: data, never commands.
_ARMOR_RE = re.compile(r"-----BEGIN [^-\n]*-----.*?(?:-----END [^-\n]*-----|\Z)", re.S)


#: A single word or quoted string longer than this is data (a blob, a generated file
#: passed inline), never a command name or a path. `shlex` builds a token one character
#: at a time, which is quadratic: 400 KB takes ten seconds. It is cut out beforehand.
MAX_WORD_CHARS = 65536
_N = f"{{{MAX_WORD_CHARS},}}"  # the quantifier "{65536,}"
_LONG_WORD_RES = (
    (re.compile(r"'[^']" + _N + "'"), "'LONG-DATA'"),
    (re.compile(r'"(?:[^"\\]|\\.)' + _N + '"', re.S), '"LONG-DATA"'),
    (re.compile(r"[^\s'\"]" + _N), "LONG-DATA"),
    # ... and a quote that opens and never closes
    (re.compile(r"'[^']" + _N + r"\Z"), "'LONG-DATA'"),
    (re.compile(r'"(?:[^"\\]|\\.)' + _N + r"\Z", re.S), '"LONG-DATA"'),
)


def _tokenize(command: str) -> list[str]:
    text = _ARMOR_RE.sub("ARMORED-DATA", _strip_heredocs(command))
    if len(text) > MAX_WORD_CHARS:
        for pattern, stand_in in _LONG_WORD_RES:
            text = pattern.sub(stand_in, text)
    lines = text.replace("\\\n", " ")
    lexer = shlex.shlex(lines.replace("\n", " ; "), posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        return list(lexer)
    except ValueError:  # unbalanced quotes
        # Where a quote opens and never closes, the lines after the first are most
        # likely the rest of a multi-line argument: the command is on the first line.
        return lines.split("\n", 1)[0].split()


class _Segment(list[str]):
    """The words of one simple command, and where its output went."""

    out_null = False
    err_null = False
    into = ""

    def redirect(self, fd: str, operator: str, target: str) -> None:
        null = target == "/dev/null"
        if "<" in operator:
            return
        if operator.startswith("&>") or (operator == ">&" and not target.isdigit()):
            self.out_null = self.err_null = null
        elif operator == ">&":  # `2>&1`: one stream follows the other
            if fd == "2" and target == "1":
                self.err_null = self.out_null
            elif fd in ("", "1") and target == "2":
                self.out_null = self.err_null
        elif fd == "2":
            self.err_null = null
        elif fd in ("", "1"):
            self.out_null = null
            self.into = "" if null else target


def _segments(tokens: Sequence[str]) -> list[_Segment]:
    """Split tokens into simple commands, dropping redirections and comments."""
    segments: list[_Segment] = []
    current = _Segment()
    pending: tuple[str, str] | None = None  # a redirection waiting for its target
    comment = False
    for token in tokens:
        if token in _SEPARATORS:
            if current:
                segments.append(current)
            current = _Segment()
            comment = comment and token not in (";", ";;")
            pending = None
            continue
        if comment:
            continue
        if pending is not None:
            current.redirect(*pending, token)
            pending = None
            continue
        if token[:1] in ("<", ">") or token[:2] == "&>":
            # redirection: drop it, its target and a leading fd number
            fd = current.pop() if current and current[-1].isdigit() else ""
            operator = token.rstrip("&") if token[:2] == "&>" else token
            if token.strip("<>&|") == "":
                pending = (fd, operator)
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
        elif _unwrapped(head.rsplit("/", 1)[-1], out[1:]) is not None:
            # `dotenv run -- pytest`, `xvfb-run npm test`: about the command they start
            out = _unwrapped(head.rsplit("/", 1)[-1], out[1:]) or []
        elif head in WRAPPER_PROGRAMS or (
            _is_abs(head) and head.rsplit("/", 1)[-1] in WRAPPER_PROGRAMS
        ):
            out = _strip_wrapper(head.rsplit("/", 1)[-1], out[1:])  # `/usr/bin/time -v`
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


_CONTAINER_TOOLS = frozenset({"docker", "podman", "docker-compose", "podman-compose", "kubectl"})


def _container_prefix(stripped: Sequence[str]) -> str:
    """How the words in front of a command put it in a container (see ``Step.container``).

    A compose service is named (``docker compose exec web``): services are part of
    the project. A container or pod is not: its name is often an id of the moment.
    """
    names = [_program_name(w) for w in stripped]
    at = next((i for i, name in enumerate(names) if name in _CONTAINER_TOOLS), None)
    if at is None:
        return ""
    head, rest = names[at], list(stripped[at + 1 :])
    verb = next((w for w in rest if w in ("exec", "run")), "")
    if not verb:
        return ""
    if head == "kubectl" or (head in ("docker", "podman") and "compose" not in rest):
        return f"{head} exec" if verb == "exec" or head == "kubectl" else ""
    tool = head if head.endswith("-compose") else f"{head} compose"
    service = rest[-1] if rest[-1] not in ("exec", "run", "--") else ""
    return f"{tool} {verb} {service}".strip()


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


#: Programs that start another command with something added to its environment (secrets,
#: a virtual display, a pseudo-terminal): subcommand that does so (``""``: none needed),
#: and the options of theirs that take a separate value.
ENV_LAUNCHERS: dict[str, tuple[str, frozenset[str]]] = {
    "dotenv": ("", frozenset({"-e", "-f", "--file", "-c", "-v", "-o"})),
    "doppler": ("run", frozenset({"-p", "--project", "-c", "--config", "-t", "--token"})),
    "op": ("run", frozenset({"--env-file", "--account", "--session"})),
    "direnv": ("exec", frozenset()),
    "xvfb-run": (
        "",
        frozenset({"-n", "-s", "-e", "-f", "-p", "-w", "--server-num", "--server-args"})
        | {"--error-file", "--auth-file", "--wait"},
    ),
}


def _unwrapped(program: str, rest: Sequence[str]) -> list[str] | None:
    """The words of the command ``program rest`` starts, or ``None`` if it starts none."""
    out = list(rest)
    if program == "script":
        # util-linux: `script -q -c 'pytest -q' /dev/null`; BSD: `script -q /dev/null pytest`
        while out and out[0].startswith("-") and out[0] != "--":
            option = out.pop(0)
            if option.startswith("-") and not option.startswith("--") and "c" in option[1:]:
                return _tokenize(out[0]) if out else None
            if option == "--command":
                return _tokenize(out[0]) if out else None
        return out[1:] if len(out) > 1 else None
    spec = ENV_LAUNCHERS.get(program)
    if spec is None:
        return None
    subcommand, takes_value = spec

    def options() -> None:
        while out and out[0].startswith("-") and out[0] != "--":
            option = out.pop(0)
            if option in takes_value and out:
                out.pop(0)

    options()
    if subcommand:
        if not out or out[0] != subcommand:
            return None  # `op item get`, `doppler secrets`: their own business
        out.pop(0)
        if program == "direnv":
            if not out:
                return None
            out.pop(0)  # the directory whose environment is loaded
    elif program == "dotenv" and out and out[0] == "run":
        out.pop(0)
    options()
    if out and out[0] == "--":
        out.pop(0)
    return out or None


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


#: What Windows appends to a program's name: ``npm.cmd``, ``python.exe``, ``gradlew.bat``.
_WINDOWS_SUFFIX_RE = re.compile(r"(?<=[^/.])\.(?:cmd|exe|bat)$", re.I)


def _program_name(word: str) -> str:
    """Basename for absolute/vendored executables, cleaned relative path for repo scripts."""
    word = _WINDOWS_SUFFIX_RE.sub("", to_posix(word))
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


#: Launchers: programs that run another command inside an environment. The key is the
#: launcher followed by the key of what it runs, by the ordinary rules: ``uv run python
#: scripts/seed.py``, ``bundle exec rails test``, ``npx tsx scripts/a.ts``. The value
#: is the subcommand that launches (none for ``npx``, ``bunx``, ``uvx``).
LAUNCHERS: dict[str, frozenset[str]] = {
    "uv": frozenset({"run"}),
    "poetry": frozenset({"run"}),
    "pipenv": frozenset({"run"}),
    "hatch": frozenset({"run"}),
    "pdm": frozenset({"run"}),
    "rye": frozenset({"run"}),
    "conda": frozenset({"run"}),
    "mamba": frozenset({"run"}),
    "micromamba": frozenset({"run"}),
    "bundle": frozenset({"exec"}),
    "pnpm": frozenset({"exec", "dlx"}),
    "yarn": frozenset({"exec", "dlx"}),
    "npm": frozenset({"exec"}),
    "bun": frozenset({"x", "run"}),
    "npx": frozenset(),
    "bunx": frozenset(),
    "uvx": frozenset(),
}
#: Options of a launching subcommand that take a value (``uv run --with rich``).
_LAUNCH_VALUE_OPTIONS = frozenset(
    {"--with", "--with-editable", "--with-requirements", "--python", "-p", "--group"}
    | {"--extra", "--only-group", "--no-group", "--package", "--env-file", "--index"}
    | {"--default-index", "--index-url", "--extra-index-url", "--project", "--directory"}
    | {"-n", "--name", "--prefix", "--cwd", "-C", "--filter", "-F", "--from", "--script"}
    | {"-c", "--call", "-w", "--workspace"}
)


def _launched(base: str, rest: Sequence[str]) -> tuple[str, list[str]] | None:
    """``(launcher, words of the command it runs)`` if ``base rest`` launches one."""
    launching = LAUNCHERS.get(base)
    if launching is None:
        return None
    rest = _skip_options(base, rest)
    launcher = base
    if launching:
        if not rest or rest[0] not in launching:
            return None
        launcher, rest = f"{base} {rest[0]}", list(rest[1:])
    inner = list(rest)
    while inner and inner[0].startswith("-"):
        option = inner.pop(0)
        if option == "--":
            break
        if option in ("-c", "--call") and base in ("npx", "npm", "pnpm"):
            return launcher, []  # `npx -c '<shell>'`: a script, not a command name
        if option in _LAUNCH_VALUE_OPTIONS and inner and not inner[0].startswith("-"):
            inner.pop(0)
    if base == "bun" and launcher == "bun run" and not (inner and _looks_like_script(inner[0])):
        return None  # `bun run build`: a package script, named as such
    return launcher, inner


def launcher_split(key: str) -> tuple[str, str]:
    """``("uv run", "python scripts/seed.py")`` for a launched key; ``("", key)`` otherwise."""
    words = key.split()
    if not words or words[0] not in LAUNCHERS:
        return "", key
    launching = LAUNCHERS[words[0]]
    if not launching:
        return (words[0], " ".join(words[1:])) if len(words) > 1 else ("", key)
    if len(words) > 2 and words[1] in launching:
        if words[0] == "bun" and words[1] == "run" and not _looks_like_script(words[2]):
            return "", key
        return " ".join(words[:2]), " ".join(words[2:])
    return "", key


def inner_key(key: str) -> str:
    """The command a launched key runs (``pytest`` for ``uv run pytest``), or ``key``."""
    return launcher_split(key)[1]


_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
#: Switches of a shell that take no value (`-x`, `-eux`, `-e`); never `-c`, `-s`, `-o`.
_SHELL_SWITCH_RE = re.compile(r"^-[abefhkmnptuvxBCEHPT]+$")


def _raw_key(words: Sequence[str]) -> str:
    if not words:
        return ""
    program = _program_name(words[0])
    if _PYTHON_RE.match(program):
        program = "python"  # python3, python3.12, pypy3 ... are one command
    elif _PIP_RE.match(program):
        program = "pip"  # pip3, pip3.12
    rest = list(words[1:])
    base = program.rsplit("/", 1)[-1]
    launched = _launched(base, rest) if "/" not in program else None
    if launched is not None:
        launcher, inner = launched
        return f"{launcher} {_raw_key(inner)}".strip()
    if base in _SHELLS and "/" not in program:
        # `bash scripts/run.sh`, `sh -x ./scripts/run.sh` and `scripts/run.sh` are one
        # command: the script. Which shell ran it, and how loudly, is not what it is.
        script = next((w for w in rest if not _SHELL_SWITCH_RE.match(w)), "")
        if script and _looks_like_script(script):
            return _program_name(script)
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
            if _workspace_directory(rest[1]) is not None:
                # ``yarn workspace web test`` is ``yarn test`` run in ``web/``, as
                # ``npm --prefix web test`` is: the package is where, not what
                return _raw_key([words[0], *rest[2:]])
            # ``yarn workspace @acme/web <script>``: which package, and what is run in it
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


_PIP_RE = re.compile(r"^pip3(?:\.\d+)?$")
#: ``pnpm --filter web test``: the package the command is run in.
_WORKSPACE_OPTIONS = frozenset({"--filter", "-F"})
_WORKSPACE_NAME_RE = re.compile(r"^(?:\./)?([A-Za-z0-9_][\w.-]*(?:/[A-Za-z0-9_][\w.-]*)*)/?$")


def _workspace_directory(name: str) -> str | None:
    """The directory a workspace selector stands for, if it reads as one.

    ``web``, ``./web`` and ``packages/web`` do. A scoped package name
    (``@acme/web``), a pattern (``web...``, ``!web``, ``{packages/*}``) and a
    git range (``[main]``) do not: where they run cannot be read off the word.
    """
    found = _WORKSPACE_NAME_RE.match(name)
    if found is None or name.endswith("..."):
        return None
    return found.group(1)


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


#: Tools that take that option after the subcommand as well (`npm test --prefix web`).
_DIR_ANYWHERE = frozenset({"npm", "pnpm"})


def _own_directory(program: str, args: Sequence[str]) -> list[str]:
    """The directory a tool was pointed at with ``-C dir`` / ``--prefix=dir``, if any."""
    options = _DIR_OPTIONS.get(program)
    if not options:
        return []
    for index, word in enumerate(args):
        if word == "--" or (not word.startswith("-") and program not in _DIR_ANYWHERE):
            break  # options of the tool itself come before its subcommand
        name, eq, value = word.partition("=")
        if name in options:
            if not eq:
                if index + 1 >= len(args):
                    return []
                value = args[index + 1]
            return [value]
    return []


def _workspace(program: str, args: Sequence[str]) -> str:
    """The package a step is run in: ``pnpm --filter web``, ``yarn workspace web``."""
    if program == "yarn" and len(args) > 2 and args[0] == "workspace":
        return _workspace_directory(args[1]) or ""
    if program != "pnpm":
        return ""
    for index, word in enumerate(args):
        if not word.startswith("-"):
            break
        name, eq, value = word.partition("=")
        if name in _WORKSPACE_OPTIONS:
            if not eq:
                if index + 1 >= len(args):
                    return ""
                value = args[index + 1]
            return _workspace_directory(value) or ""
    return ""


#: What no command word begins with.
_NOT_A_COMMAND = frozenset("-=+,;:%^*?#@)]}")


def _steps(command: str, depth: int = 0, moves: tuple[str, ...] = ()) -> list[Step]:
    steps: list[Step] = []
    for segment in _segments(_tokenize(command)):
        words = _strip_prefixes(segment)
        if not words or words[0][:1] in _NOT_A_COMMAND:
            continue  # `-----END`, `=value`: a line of data, not a command word
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
        silenced = segment.out_null and segment.err_null
        args = tuple(words[1:])
        stripped = segment[: max(len(segment) - len(words), 0)]
        wrappers = tuple(
            name for name in (w.rsplit("/", 1)[-1] for w in stripped) if name in WRAPPER_PROGRAMS
        )
        steps.append(
            Step(
                key,
                program,
                args,
                inspection,
                significant,
                where,
                silenced,
                segment.into,
                wrappers,
                _workspace(program, words[1:]),
                _container_prefix(stripped),
            )
        )
    return steps


def _masked(command: str, program: str) -> bool:
    try:
        return _status_masked(command, program)
    except (IndexError, ValueError, RecursionError):
        return False


_MASKING = frozenset({"|", "|&", "||", "&"})


def _pieces(command: str) -> list[tuple[list[str], str]]:
    """The simple commands of a line: (words, operator after it)."""
    pieces: list[tuple[list[str], str]] = []
    current: list[str] = []
    for token in _tokenize(command):
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
    return pieces


_TEXT_PRINTERS = frozenset(
    {"git log", "git show", "git diff", "git blame", "docker logs", "kubectl logs", "journalctl"}
)


def ends_looking(command: str) -> bool:
    """Does the last pipeline of a line of several print other text (``...; git log -3``)?

    What stands at the end of the output is then that step's: a traceback quoted in a
    commit message or a log file is not the failure of the command before it.
    """
    try:
        pieces = _pieces(command)
    except (IndexError, ValueError, RecursionError):
        return False
    start = len(pieces) - 1
    while start > 0 and pieces[start - 1][1] in ("|", "|&"):
        start -= 1
    if start <= 0:
        return False
    program, args = _program_of(pieces[start][0])
    # only what prints text written elsewhere: `git status` or `ls` quote nothing
    return program in FILE_READERS or _raw_key([program, *args]) in _TEXT_PRINTERS


_FAILURE_WORD_RE = re.compile(r"fail|error|\^E\b|\bE {2,}|✗|✘|×|traceback|exception|warn", re.I)
_SUCCESS_WORD_RE = re.compile(r"pass|\bok\b|success|✓|✔|\bdone\b|\d+ tests?\b|summary", re.I)


def selects_failures(command: str) -> bool:
    """Does the line end in a filter that lets only failure lines through?

    ``pytest | grep -E "^FAILED|^E  "``: no output then means no failure. A pattern
    that would also show a passing summary (``grep -E "passed|failed"``) printing
    nothing means that nothing ran, which proves nothing.
    """
    try:
        pieces = _pieces(command)
    except (IndexError, ValueError, RecursionError):
        return False
    if len(pieces) < 2 or pieces[-2][1] not in ("|", "|&"):
        return False
    program, args = _program_of(pieces[-1][0])
    if program not in ("grep", "egrep", "fgrep", "rg"):
        return False
    if any(a in ("-v", "--invert-match", "-c", "--count", "-L") for a in args):
        return False
    pattern = next((a for a in args if not a.startswith("-")), "")
    return bool(_FAILURE_WORD_RE.search(pattern)) and not _SUCCESS_WORD_RE.search(pattern)


def in_sequence(command: str) -> bool:
    """Does ``command`` run commands one after another (``a; b``, ``a && b``)?

    A pipeline alone (``a | tail``) does not: its output is one command's.
    """
    try:
        pieces = _pieces(command)
    except ValueError:
        return False
    return any(operator not in ("", "|", "|&") for _, operator in pieces[:-1])


#: Programs that print a file given as an argument (``sed -n``, ``grep pattern file``).
FILE_READERS = frozenset(
    {"cat", "head", "tail", "less", "more", "nl", "bat", "grep", "egrep", "fgrep", "rg", "sed"}
    | {"awk"}
)
_PATTERN_FIRST = frozenset({"grep", "egrep", "fgrep", "rg", "sed", "awk"})


#: What output is piped into to be read: a pager or a filter.
_READ_THROUGH = FILE_READERS | {"wc", "sort", "uniq", "cut", "jq", "column", "fold", "tr", "xxd"}


def looked_at(command: str) -> frozenset[str]:
    """Programs of ``command`` whose output goes into a pager or filter (``x | head``).

    Running something to read what it prints is a look, whatever the program is.
    """
    try:
        pieces = _pieces(command)
    except (IndexError, ValueError, RecursionError):
        return frozenset()
    found: set[str] = set()
    for (words, operator), (after, _) in itertools.pairwise(pieces):
        if operator in ("|", "|&") and _program_of(after)[0] in _READ_THROUGH:
            found.add(_program_of(words)[0])
    return frozenset(found)


def same_invocation(red: str, green: str, key: str, other: str = "", loose: bool = False) -> bool:
    """Is ``green`` the command line ``red`` again, as far as its arguments go?

    Compared are the words of the step keyed ``key`` in ``red`` and of the step
    keyed ``other`` (default: the same key) in ``green``. A launcher or activation
    in front of either does not count (``uv run pytest -q`` is ``pytest -q``). For
    ad-hoc code (``python -c``, a heredoc, SQL given to ``psql -c``) the text of the
    snippet is part of the line. With ``loose``, switches are ignored and the green
    run may name fewer things than the red one (``pytest`` after ``pytest
    tests/test_a.py -x``): enough for "the same job", where a changed value
    (``--month 2026-13``, then ``2026-12``) is not.
    """
    if is_snippet(key):
        return normalize_argv(red) == normalize_argv(green)
    try:
        was = next((s for s in parse_command(red).steps if s.key == key), None)
        now = next((s for s in parse_command(green).steps if s.key == (other or key)), None)
    except Exception:
        return False
    if was is None or now is None:
        return False
    a, b = (was.program, *was.args), (now.program, *now.args)
    if loose:
        named = [w for w in b if not w.startswith("-")]
        return all(w in a for w in named[1:])
    short, long = sorted((a, b), key=len)
    return long[len(long) - len(short) :] == short


#: Programs that are handed code or a query on the command line.
_QUERY_FLAGS: dict[str, frozenset[str]] = {
    "psql": frozenset({"-c", "--command"}),
    "mysql": frozenset({"-e", "--execute"}),
    "mariadb": frozenset({"-e", "--execute"}),
    "sqlite3": frozenset(),
    "duckdb": frozenset({"-c"}),
    "jq": frozenset(),
    "yq": frozenset(),
    "awk": frozenset(),
    "sed": frozenset(),
    "redis-cli": frozenset(),
    "mongosh": frozenset({"--eval"}),
}


def is_snippet(key: str) -> bool:
    """Is ``key`` a command that runs code written on its line (inline code, SQL, a filter)?"""
    return is_inline_code(key) or key.split(" ", 1)[0].rsplit("/", 1)[-1] in _QUERY_FLAGS


def _program_of(words: list[str]) -> tuple[str, list[str]]:
    segments = _segments(words)
    stripped = _strip_prefixes(segments[0]) if segments else []
    if not stripped:
        return "", []
    return _program_name(stripped[0]).rsplit("/", 1)[-1], stripped[1:]


def status_is_last_steps(command: str) -> bool:
    """Does the line end in ``; step``, so that its exit status is that step's alone?"""
    try:
        pieces = [p for p in _pieces(command) if _program_of(p[0])[0]]
    except (IndexError, ValueError, RecursionError):
        return False
    return len(pieces) > 1 and pieces[-2][1] == ";"


def prints_another_file(command: str) -> bool:
    """Does a later step of the line print a file that nothing earlier on the line names?

    ``make build; cat docs/troubleshooting.md`` prints the document after the build's
    own output, and the two cannot be told apart. ``pytest > out.log 2>&1; tail out.log``
    prints what the test run wrote: that is the run's own output.
    """
    try:
        pieces = _pieces(command)
    except (IndexError, ValueError, RecursionError):
        return False
    seen: list[str] = []
    for position, (words, _) in enumerate(pieces):
        program, args = _program_of(words)
        piped = position > 0 and pieces[position - 1][1] in ("|", "|&")
        if position > 0 and seen and not piped and program in FILE_READERS:
            files = [a for a in args if not a.startswith("-") and a not in ("<", ">", ">>")]
            if program in _PATTERN_FIRST:
                files = files[1:]
            if program in ("head", "tail"):
                files = [f for f in files if not f.isdigit()]
            earlier = " ".join(seen)
            if any(f.rsplit("/", 1)[-1] not in earlier for f in files):
                return True
        if program:
            seen.extend(words)
    return False


def _status_masked(command: str, program: str, depth: int = 0) -> bool:
    """Is the exit status of the last ``program`` step hidden by what follows it?

    ``pytest | tail -5``, ``pytest; echo done`` and ``pytest || true`` report the
    status of ``tail``, ``echo`` and (at worst) ``true``. ``pytest && echo ok``
    does not hide a failure, and ``set -o pipefail`` restores a pipeline's.
    """
    pieces = _pieces(command)
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


_DRIVE_PATH_RE = re.compile(r"(?:^|[\s\"'=(])[A-Za-z]:\\")
_WINDOWS_SEPARATOR_RE = re.compile(r"\\(?=[^\s\"'\\])")


def is_windows_path(path: str | None) -> bool:
    """``C:\\Users\\dev\\proj`` or ``C:/Users/dev/proj``: a path with a drive letter."""
    return bool(path) and re.match(r"^[A-Za-z]:[\\/]", path or "") is not None


def posix_separators(command: str, windows: bool | None = None) -> str:
    """``python scripts\\gen.py`` as a POSIX shell would need it: ``python scripts/gen.py``.

    In PowerShell and cmd a backslash separates path components; read as POSIX it
    escapes the next character and the path collapses to ``scriptsgen.py``. Applied
    when the session runs on Windows (``windows``), or, if that is unknown, when the
    line itself contains a drive-letter path.
    """
    if windows is None:
        windows = _DRIVE_PATH_RE.search(command) is not None
    return _WINDOWS_SEPARATOR_RE.sub("/", command) if windows else command


#: `NAME=value` standing alone on the line (not `NAME=value command`), with a plain value.
_ASSIGNMENT_RE = re.compile(
    r"(?:^|[;&\n])\s*(?:export\s+)?([A-Za-z_]\w*)="
    r"(\"[\w./:@%+,~ -]*\"|'[\w./:@%+,~ -]*'|[\w./:@%+,~-]+)(?=\s*(?:;|&&|\n|$))"
)
_VARIABLE_RE = re.compile(r"\$(?:\{[A-Za-z_]\w*\}|[A-Za-z_]\w*)")
#: What a shell variable that the line does not define is called in a command key.
VARIABLE = "<variable>"


def expand_assignments(command: str) -> str:
    """``A=scripts/x.sh; $A`` with the variable filled in: ``A=scripts/x.sh; scripts/x.sh``.

    Only assignments of a plain value made on the same line are followed; nothing
    is evaluated.
    """
    if "$" not in command or "=" not in command:
        return command
    for match in reversed(list(_ASSIGNMENT_RE.finditer(command))):
        name, value = match.group(1), match.group(2).strip("\"'")
        head, tail = command[: match.end()], command[match.end() :]
        tail = re.sub(r"\$(?:\{" + name + r"\}|" + name + r"\b)", value.replace("\\", "\\\\"), tail)
        command = head + tail
    return command


def _name_variables(step: Step) -> Step:
    """A step run through a variable nobody defined on the line: ``<variable> test``."""
    if "$" not in step.key and "$" not in step.program:
        return step
    return replace(
        step,
        key=_VARIABLE_RE.sub(VARIABLE, step.key),
        program=_VARIABLE_RE.sub(VARIABLE, step.program),
    )


def parse_command(command: str, windows: bool | None = None) -> ParsedCommand:
    """Normalize a shell command line. Rules are documented in docs/methodology.md.

    The line is split into simple commands (across ``;``, ``&&``, ``||``, pipes,
    subshells, ``if``/``for`` bodies and ``bash -c`` scripts; heredoc bodies are
    dropped). It is named after its **last significant** command: setup
    (``cd``, ``export``, ``echo``), housekeeping (``mkdir``, ``touch``, ``rm``)
    and read-only inspection (``cat``, ``tail``, ``grep``) are passed over, so
    ``mkdir -p out && pytest | tail -5`` is ``pytest``.
    """
    command = expand_assignments(posix_separators(command, windows))
    try:
        steps = [_name_variables(step) for step in _steps(command)]
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


#: Destructive, but local and easily undone: next to a push or a delete, not the problem.
_RECOVERABLE_KEYS = frozenset({"git commit", "git stash", "git branch", "git tag"})
_FORCE_FLAGS = frozenset({"-f", "--force", "--force-with-lease", "--hard", "-rf", "-fr"})


def _worst(steps: Sequence[Step]) -> Step | None:
    """The first destructive step; one that is forced or cannot be undone locally first."""
    risky = [s for s in steps if s.program in DESTRUCTIVE_PROGRAMS or s.key in DESTRUCTIVE_KEYS]
    for step in risky:
        if step.key not in _RECOVERABLE_KEYS or _FORCE_FLAGS & set(step.args):
            return step
    return risky[0] if risky else None


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
    # Looking is never what was objected to while the line also changes something.
    named = [
        step
        for step in parsed.steps
        if step.program.lower() in words
        and step.program not in SETUP_PROGRAMS
        and not step.inspection
    ]
    if named:
        # "git" names `git commit` and `git push -f` alike: then the destructive one.
        return _worst(named) or named[0]
    worst = _worst(parsed.steps)
    if worst is not None:
        return worst
    for step in parsed.steps:
        if not step.inspection and step.program not in SETUP_PROGRAMS:
            return step
    return next((s for s in parsed.steps if s.key == parsed.key), parsed.steps[-1])


def denied_step(parsed: ParsedCommand, said: str = "") -> Step | None:
    """The step of a command line that a permission rule or hook most plausibly refused.

    In order: a step the refusal names; the **first** step if it writes a file by
    redirection (`cat >> tests/test_a.py <<EOF ... && git push`: a write through the
    shell is what such rules are there to stop, and the line never got past it); a
    destructive step, as for a human's objection; otherwise the first step:
    permission rules match a command line from its start, so `cat notes.md && uv
    run ruff check` was refused at `cat`, not at the check the line ends with.
    """
    if not parsed.steps:
        return None
    words = set(re.findall(r"[\w.+-]+", said.lower()))
    named = any(s.program.lower() in words and not s.inspection for s in parsed.steps)
    waits = ("cd", "sleep", "true", ":")
    first = next((s for s in parsed.steps if s.key and s.program not in waits), None)
    if named:
        return objectionable_step(parsed, said)
    if first is not None and written_file(first):
        return first
    if _worst(parsed.steps) is not None:
        return objectionable_step(parsed, said)
    quiet = (*waits, "echo")
    return next((s for s in parsed.steps if s.key and s.program not in quiet), parsed.steps[0])


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
    """``test``, ``build``, ``lint``, ``typecheck`` or ``format``; ``None`` if unknown.

    A launched command has the role of what it runs: ``bundle exec rails test`` is a
    test, whatever ``bundle`` or ``rails`` are.
    """
    for word in reversed(_key_words(inner_key(key))):
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


#: The ecosystem a tool belongs to. ``pytest`` and ``npm test`` are both test runners,
#: and neither says anything about the other's code.
TOOL_FAMILIES: dict[str, frozenset[str]] = {
    "python": frozenset(
        {"python", "pytest", "tox", "nox", "uv", "uvx", "poetry", "pipenv", "pdm", "hatch"}
        | {"ruff", "flake8", "pylint", "mypy", "pyright", "black", "isort", "unittest", "pip"}
        | {"ty", "coverage", "pipx", "conda", "django-admin", "alembic", "flask"}
    ),
    "js": frozenset(
        {"npm", "pnpm", "yarn", "bun", "bunx", "npx", "node", "deno", "jest", "vitest", "mocha"}
        | {"eslint", "tsc", "prettier", "playwright", "cypress", "karma", "ava", "webpack"}
        | {"biome", "stylelint", "vite", "turbo", "nx", "next", "tsx", "ts-node", "rollup"}
    ),
    "rust": frozenset({"cargo", "rustc", "rustfmt", "clippy", "nextest", "rustup"}),
    "go": frozenset({"go", "gofmt", "golangci-lint", "staticcheck", "gotestsum"}),
    "ruby": frozenset({"bundle", "rspec", "rubocop", "rails", "ruby", "gem"}),
    "php": frozenset({"php", "phpunit", "composer", "phpstan", "artisan"}),
    "jvm": frozenset({"gradle", "gradlew", "mvn", "mvnw", "sbt", "java", "javac", "kotlinc"}),
    "dotnet": frozenset({"dotnet", "msbuild", "nuget"}),
    "c": frozenset({"cmake", "ctest", "ninja", "gcc", "clang", "cc", "meson", "bazel"}),
    "shell": frozenset({"shellcheck", "shfmt", "bats"}),
}


def tool_family(key: str) -> str | None:
    """``python``, ``js``, ``rust``, ... for a key whose tool says so; ``None`` otherwise.

    ``make test``, ``just lint``, ``task build``, ``pre-commit run`` and a script of
    the repository (``./scripts/test.sh``) are neutral: they wrap whatever the
    project uses, and say nothing about which ecosystem that is.
    """
    for word in key.split(" (", 1)[0].split():
        name = word.rsplit("/", 1)[-1]
        if _SCRIPT_RE.search(name):
            continue  # a script's name is the project's, not a tool's
        for family, names in TOOL_FAMILIES.items():
            if name in names:
                return family
    return None


def same_family(one: str, other: str) -> bool:
    """Could ``one`` and ``other`` be about the same code? Not ``pytest`` and ``npm test``."""
    mine, theirs = tool_family(one), tool_family(other)
    return mine is None or theirs is None or mine == theirs


def is_replacement(failed: str, other: str) -> bool:
    """Can ``other`` stand in for ``failed``? (``make test`` for ``pytest``.)

    True when both do the same job (same role) in the same tool family, with
    make-style wrappers fitting any family (``make lint`` for ``ruff check``, never
    ``npx eslint``), or when one wraps the other's tool (``uv run pytest`` for
    ``pytest``). Never for setup, version-control, housekeeping or inspection
    commands.
    """
    if not failed or not other or failed == other:
        return False
    # the same command through a launcher: `uv run python scripts/gen.py` for
    # `python scripts/gen.py`, `poetry run alembic upgrade` for `alembic upgrade`
    if inner_key(failed) == inner_key(other) and INLINE not in other and STDIN not in other:
        return True
    if is_setup_command(other):
        return False
    program = other.split()[0]
    if program in ("git", "gh", "glab") or program in HOUSEKEEPING_PROGRAMS | SETUP_PROGRAMS:
        return False
    if not same_family(failed, other):
        return False  # another ecosystem's tool: it ran other code
    role = command_role(failed)
    if role is not None and role == command_role(other):
        return True
    # the same program by another path: `./bin/zebractl` for `zebractl`
    was, now = failed.split(), other.split()
    if was[1:] == now[1:] and "/" in now[0] and now[0].rsplit("/", 1)[-1] == was[0]:
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


#: Tool names that say nothing about a project: the built-in tools of the agents whose
#: transcripts are read, and the generic format's operation words.
PUBLIC_TOOLS = frozenset(
    [
        "read",
        "edit",
        "write",
        "multiedit",
        "notebookedit",
        "notebookread",
        "bash",
        "bashoutput",
        "killshell",
        "killbash",
        "taskoutput",
        "taskstop",
        "grep",
        "glob",
        "ls",
        "webfetch",
        "websearch",
        "task",
        "agent",
        "todowrite",
        "todoread",
        "exitplanmode",
        "enterplanmode",
        "askuserquestion",
        "skill",
        "slashcommand",
        "toolsearch",
        "powershell",
        "listmcpresourcestool",
        "readmcpresourcetool",
        "search",
        "shell",
        "other",
        "view",
        "create",
        "str_replace",
        "str_replace_editor",
        "str_replace_based_edit_tool",
        "insert",
        "undo_edit",
        "apply_patch",
        "read_file",
        "write_file",
        "edit_file",
        "list_dir",
        "list_directory",
        "run_terminal_cmd",
        "run_command",
        "execute_command",
        "run_shell_command",
        "shell_command",
        "file_search",
        "grep_search",
        "codebase_search",
        "search_files",
        "search_file_content",
        "list_files",
        "replace",
        "replace_in_file",
        "write_to_file",
        "delete_file",
        "web_search",
        "web_fetch",
        "update_plan",
    ]
)


def _tool_hash(kind: str, name: str) -> str:
    return f"<{kind}:{hashlib.sha256(name.encode()).hexdigest()[:PUBLIC_HASH_DIGITS]}>"


def public_tool(name: str) -> str:
    """A tool name as it may be shown without excerpts.

    Built-in tool names are kept. Any other name was chosen by a project or by
    one of its plugins and becomes ``<tool:hash>``; an MCP tool
    ``mcp__<server>__<tool>`` becomes ``mcp__<server:hash>__<tool:hash>``, so
    that tools of one server can still be told to belong together.
    """
    if name.lower() in PUBLIC_TOOLS:
        return name
    if name.startswith("mcp__") and "__" in name[5:]:
        server, _, tool = name[5:].partition("__")
        return f"mcp__{_tool_hash('server', server)}__{_tool_hash('tool', tool)}"
    return _tool_hash("tool", name)


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
        if (keep is not None and keep(word)) or word == VARIABLE:
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
_SETUP_ARGS = frozenset(
    {"install", "ci", "bootstrap", "setup", "migrate", "up", "login", "sync", "download"}
)
#: Words at which the name of a preparing step is complete (`npx prisma generate`).
_STEP_WORDS = _SETUP_ARGS | frozenset(
    {"generate", "upgrade", "restore", "pull", "tidy", "vendor", "seed", "reset", "prepare"}
    | {"build", "start", "init", "fetch", "deploy", "create", "db:setup", "db:migrate"}
)
_SUBCOMMAND_WORD_RE = re.compile(r"^[a-z][a-z0-9:_-]*$")


def setup_label(step: Step) -> str:
    """How a preparing step is named in advice: enough of it to be run as written.

    The key of ``npx playwright install chromium`` is ``npx playwright``, which
    installs nothing. The label goes on through the subcommand words up to and
    including the one that says what the step does (``npx playwright install``,
    ``npx prisma generate``, ``go mod download``); file arguments and other
    options are left out. ``docker compose up`` keeps ``-d``: without it the
    command stays in the foreground.
    """
    key = step.key
    known = key.split()
    if not known or is_inline_code(key):
        return key
    words = [arg for arg in step.args if arg[:1] != "-"]
    # the subcommand words the key already covers
    rest = list(words)
    for word in known[1:]:
        if word in rest:
            rest = rest[rest.index(word) + 1 :]
    label = key
    if not _STEP_WORDS & set(known):
        more: list[str] = []
        for word in rest[:3]:
            if not _SUBCOMMAND_WORD_RE.match(word):
                break
            more.append(word)
            if word in _STEP_WORDS:
                label = " ".join([key, *more])
                break
    if label.split()[-1] == "up" and {"-d", "--detach"} & set(step.args):
        label += " -d"
    return label


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


_SLEEP_PREFIX_RE = re.compile(r"^(?:sleep \d+(?:\.\d+)?[smh]? ?(?:&&|;) ?)+")


def normalize_argv(command: str) -> str:
    """A command line with whitespace collapsed and leading ``VAR=value`` and waits dropped.

    Two runs are "the same command again" only if this text is identical.
    """
    text = re.sub(r"\s+", " ", command).strip()
    # `sleep 3 && curl ...` is `curl ...` again, a little later
    text = _SLEEP_PREFIX_RE.sub("", text)
    return _ENV_PREFIX_RE.sub("", text)
