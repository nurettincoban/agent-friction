from __future__ import annotations

import sys
from pathlib import Path

import pytest

from agent_friction.attribution import (
    PathResolver,
    command_role,
    is_replacement,
    is_setup_command,
    normalize_command,
    objectionable_step,
    parse_command,
    path_candidates,
    public_key,
)

needs_symlinks = pytest.mark.skipif(sys.platform == "win32", reason="needs symlink privileges")

# The POSIX-style literals below are deliberate: transcripts are data, and the resolver
# is purely lexical, so they must behave the same on Windows and macOS runners.


@pytest.fixture
def resolver() -> PathResolver:
    return PathResolver(Path("/work/repo"), home="/home/dev")


def test_absolute_paths_inside_and_outside(resolver: PathResolver) -> None:
    assert resolver.resolve("/work/repo/src/a.py") == "src/a.py"
    assert resolver.resolve("/work/repo") == ""
    assert resolver.resolve("/work/repo-other/src/a.py") is None
    assert resolver.resolve("/etc/passwd") is None
    assert resolver.resolve("/work/repo/../secrets.txt") is None


def test_relative_paths_use_cwd_then_repo_root(resolver: PathResolver) -> None:
    assert resolver.resolve("src/a.py") == "src/a.py"
    assert resolver.resolve("./src/../src/a.py") == "src/a.py"
    assert resolver.resolve("a.py", cwd="/work/repo/pkg") == "pkg/a.py"
    assert resolver.resolve("../a.py", cwd="/work/repo/pkg") == "a.py"
    assert resolver.resolve("../../a.py", cwd="/work/repo/pkg") is None
    assert resolver.resolve("a.py", cwd="/somewhere/else") is None


def test_home_git_dir_and_junk(resolver: PathResolver) -> None:
    home = PathResolver(Path("/home/dev/proj"), home="/home/dev")
    assert home.resolve("~/proj/x.py") == "x.py"
    assert resolver.resolve("/work/repo/.git/config") is None
    assert resolver.resolve("/work/repo/.github/ci.yml") == ".github/ci.yml"
    for junk in (None, "", "   ", "a\nb", "x" * 5000, "nul\0byte"):
        assert resolver.resolve(junk) is None


def test_windows_paths_do_not_crash_and_can_match() -> None:
    win = PathResolver(Path("/work/repo"), extra_roots=[r"C:\Users\dev\repo"])
    assert win.resolve(r"C:\Users\dev\repo\src\a.py") == "src/a.py"
    assert win.resolve(r"C:\Users\dev\other\a.py") is None
    assert win.resolve(r"src\a.py", cwd=r"C:\Users\dev\repo") == "src/a.py"
    assert win.resolve(r"D:\x\y.py") is None
    assert win.contains(r"C:\Users\dev\repo\sub")


def test_drive_letter_case_and_mixed_separators() -> None:
    win = PathResolver(Path("/unused"), extra_roots=["c:\\Users\\dev\\repo"])
    assert win.resolve("C:/Users/dev/repo/src/a.py") == "src/a.py"
    assert win.resolve("c:\\Users\\dev\\repo\\src/b.py") == "src/b.py"
    assert win.resolve("..\\repo\\x.py", cwd="C:\\Users\\dev\\repo") == "x.py"
    assert win.resolve("C:\\Users\\dev\\repo2\\a.py") is None


def test_real_directories_resolve_on_this_platform(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    res = PathResolver(repo)
    assert res.resolve(str(repo / "pkg" / "a.py")) == "pkg/a.py"
    assert res.resolve("a.py", cwd=str(repo / "pkg")) == "pkg/a.py"
    assert res.resolve(str(tmp_path / "other" / "a.py")) is None
    assert res.contains(str(repo / "pkg")) and not res.contains(str(tmp_path))


def test_parse_command_flags_inspection() -> None:
    for command in ("sed -n 1,5p a.py", "git log -3", "cat a | wc -l", "git diff | grep x"):
        assert parse_command(command).inspection, command
    for command in ("sed -i s/a/b/ a.py", "git commit -m x", "pytest", "git stash pop"):
        assert not parse_command(command).inspection, command
    # exit-status probes ask a question; "no" is an answer, not a failure
    for command in (
        "command -v docker",
        "type docker",
        "hash docker 2>/dev/null",
        "pgrep -f vite",
        "lsof -i :3000",
        "gh pr view",
        "gh pr checks 12",
        "pip show requests",
        "npm ls left-pad",
        "git diff --quiet",
        "git diff --exit-code",
        "git merge-base --is-ancestor a b",
        "git check-ignore -q build",
        "git rev-parse --verify feature",
        "test -f x",
        "[ -d node_modules ]",
    ):
        assert parse_command(command).inspection, command
    assert parse_command("command -v docker").key == "command"
    assert parse_command("command docker ps -q").key == "docker ps"


def test_extra_roots_and_nested_worktree_win() -> None:
    res = PathResolver(Path("/work/repo"), ["/work/repo/.claude/worktrees/feat", "/old/place"])
    assert res.resolve("/work/repo/.claude/worktrees/feat/src/a.py") == "src/a.py"
    assert res.resolve("/old/place/src/a.py") == "src/a.py"
    assert res.contains("/old/place/sub") and not res.contains("/tmp") and res.contains(None)


@needs_symlinks
def test_no_symlink_resolution_of_targets(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (tmp_path / "outside.txt").write_text("x")
    (repo / "link.txt").symlink_to(tmp_path / "outside.txt")
    assert PathResolver(repo).resolve(str(repo / "link.txt")) == "link.txt"


@needs_symlinks
def test_symlinked_repo_root_matches_both_spellings(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    res = PathResolver(link)
    assert res.resolve(str(real / "a.py")) == "a.py"
    assert res.resolve(str(link / "a.py")) == "a.py"


@pytest.mark.parametrize(
    ("command", "key"),
    [
        ("npm test", "npm test"),
        ("npm test -- foo", "npm test"),
        ("CI=1 NODE_ENV=test npm test", "npm test"),
        ("npm run test:unit -- --watch", "npm run test:unit"),
        ("cd /repo && pytest tests/a.py -x 2>&1 | tail -20", "pytest"),
        ("/home/u/.venv/bin/pytest -q", "pytest"),
        (".venv/bin/python scripts/gen.py --all", "python scripts/gen.py"),
        ("python3 -m pytest -k x", "python -m pytest"),
        ("python3.12 manage.py migrate", "python manage.py migrate"),
        ("python -c 'print(1)'", "python <inline>"),
        ("uv run pytest tests/", "uv run pytest"),
        ("./scripts/build.sh --fast", "scripts/build.sh"),
        ('git commit -m "fix; a && b | c"', "git commit"),
        ("docker compose up -d", "docker compose up"),
        ("source .venv/bin/activate && make lint", "make lint"),
        ("timeout 30 cargo test some_filter", "cargo test"),
        ("sudo env FOO=1 make install", "make install"),
        ("go test ./...", "go test"),
        ("cat <<'EOF' > x.txt\nrm -rf / && boom\nEOF", "cat"),
        ("make build > out.log 2> err.log", "make build"),
        ("npm run build; npm test -- x", "npm test"),  # the last statement sets the exit code
        ("mkdir -p out && pytest tests/x.py", "pytest"),
        ("touch x && uv pip install -e . && python -m pytest | tail -5", "python -m pytest"),
        ("uv pip install -e .", "uv pip install"),
        ("npm ci && npm run build && npm test", "npm test"),
        ("make build && echo ok", "make build"),
        ("cd api && git status", "git status"),
        ("rm -rf dist && mkdir dist", "mkdir"),  # nothing significant: last real command
        ("make a\nmake b && echo ok", "make b"),
        ("rm -rf build", "rm"),
        ("export A=1", "export"),
        ("# just a comment", ""),
        ("", ""),
        ("FOO=1", ""),
        ("echo 'unbalanced", "echo"),
        # options before the subcommand, with and without a value
        ("make -j4 test", "make test"),
        ("FOO=1 make -j 4 -f build/Makefile test", "make test"),
        ("git -C /repo -c user.name=x commit -m 'x'", "git commit"),
        ("pnpm --filter web test", "pnpm test"),
        ("kubectl -n prod get pods", "kubectl get"),
        ("terraform -chdir=infra plan", "terraform plan"),
        ("cargo +nightly test", "cargo test"),
        ("mvn -q test", "mvn test"),
        # heredoc bodies are data, not commands
        ("cat <<EOF > x.py\nimport y\nEOF\npython x.py", "python x.py"),
        ("cat > t.sh <<-'END'\n\tnpm run nope\n\tEND\nnpm run build", "npm run build"),
        ("python - <<'PY'\nimport os\nos.system('rm -rf x')\nPY", "python <stdin>"),
        ("python3 <<EOF\nprint(1)\nEOF", "python"),
        ("ruff check --fix .", "ruff check"),
        ("ruff format --check .", "ruff format"),
        # control flow, subshells and nested shells are looked through
        ("if ! make lint; then exit 1; fi", "make lint"),
        ("if pytest; then echo ok; fi", "pytest"),
        ("for f in a b; do pytest $f; done", "pytest"),
        ("while ! nc -z localhost 5432; do sleep 1; done; make migrate", "make migrate"),
        ("(cd api && pytest)", "pytest"),
        ("bash -c 'pytest'", "pytest"),
        ('sh -c "cd api && cargo test --all"', "cargo test"),
        ("xargs pytest", "pytest"),
        ("time make test", "make test"),
        ("nohup npm start &", "npm start"),
        # pipelines and lists are named after the significant program, not the last word
        ("echo hi | python -m json.tool", "python -m json.tool"),
        ("pytest || true", "pytest"),
        ("pytest; echo done", "pytest"),
        ("timeout 60 pytest -k foo 2>&1 | tail -5", "pytest"),
        ("grep -rn foo src | head", "grep"),
        ("ls | wc -l", "ls"),
        # only allowlisted tools keep a subcommand; one more word only after a runner/group
        ("poetry run python -m pytest", "poetry run python -m pytest"),
        ("bundle exec rspec spec/a_spec.rb", "bundle exec rspec"),
        ("gh pr create --title x", "gh pr create"),
        ("git stash pop", "git stash pop"),
        ("docker compose run web pytest", "docker compose run"),
        ("docker run --rm -it registry.corp/img pytest", "docker run"),
        ("kubectl exec pod-1 -- ls", "kubectl exec"),
        ("yarn workspace web test", "yarn workspace web test"),
        ("aws s3 cp secret-bucket/x .", "aws s3"),
        ("tox -e py312", "tox"),
        # anything else is the executable alone: arguments are never part of a key
        ("./run hunter2pass", "run"),
        ("mytool acmecorp-merger-codename --flag", "mytool"),
        ("deploy prod-eu-1", "deploy"),
        ("ssh prod-host-17 uptime", "ssh"),
        ("psql -h db.internal -U admin shop", "psql"),
        ("mysql -uroot -phunter2 shop", "mysql"),
        ("curl -H 'Cookie: sid=abc' https://x.test", "curl"),
        ("scripts/release.sh v1.2.3-internal", "scripts/release.sh"),
        # a wrapper's own options and their values are not the command
        ("sudo -u alice pytest", "pytest"),
        ("xargs -n 1 pytest", "pytest"),
        ("find . -name '*.py' | xargs -n 1 -P 4 pytest", "pytest"),
        ("timeout -s KILL 30 make test", "make test"),
        ("env -u FOO BAR=1 npm test", "npm test"),
        ("nice -n 10 cargo build", "cargo build"),
        ("watch -n 5 make test", "make test"),
        ("uv run --group dev pytest", "uv run pytest"),
        ("uv run --with rich --python 3.12 python -m pytest", "uv run python -m pytest"),
        ("npx -p typescript tsc", "npx tsc"),
        ("docker compose -f ci.yml up", "docker compose up"),
        # a key never holds a number or anything the redactor would mask
        ("make 2", "make"),
        ("python 0123456789abcdef0123456789abcdef0123.py", "python"),
        ("npm run sk-ant-" + "api03-" + "a" * 36, "npm run"),
        ("bob@corp.test --help", ""),
    ],
)
def test_normalize_command(command: str, key: str) -> None:
    assert normalize_command(command) == key


def test_parse_command_reports_programs_and_tokens() -> None:
    parsed = parse_command("cat notes/x.md; cd sub && git diff --stat src/a.py | grep foo")
    assert (parsed.key, parsed.program, parsed.last_program) == ("git diff", "git", "grep")
    # only the arguments of the command the line is named after: no notes/x.md, "sub" or "foo"
    assert parsed.tokens == ("diff", "--stat", "src/a.py")
    assert [(s.key, s.significant) for s in parsed.steps] == [
        ("cat", False),
        ("cd", False),
        ("git diff", False),
        ("grep", False),
    ]
    assert parsed.inspection


def test_path_candidates() -> None:
    words = ["pytest", "tests/test_a.py::test_x", "-k", "foo", "--cov=src/pkg", "app.py:12"]
    words += ["'my", "dir/b.py'", "-v"]
    assert path_candidates(words) == ["tests/test_a.py", "src/pkg", "app.py", "dir/b.py"]
    assert path_candidates(["(see", "src/a.py).", "`b.py`,", "plain", "-x.y"]) == [
        "src/a.py",
        "b.py",
    ]


def test_keys_of_unknown_tools_never_contain_arguments() -> None:
    for tool in ("mytool", "./run", "tools/deploy", "frobnicate", "acme-cli"):
        for args in ("hunter2pass", "sub command", "--token abc login", "prod eu-1 --force"):
            key = normalize_command(f"{tool} {args}")
            assert key == tool.removeprefix("./"), (tool, args, key)


def test_objectionable_step() -> None:
    def pick(command: str, reason: str = "") -> str:
        step = objectionable_step(parse_command(command), reason)
        return step.key if step else ""

    chain = "rm -rf migrations/versions && alembic revision --autogenerate"
    assert parse_command(chain).key == "alembic revision"
    assert pick(chain) == "rm"  # the destructive step
    assert pick(chain, "don't run alembic by hand") == "alembic revision"  # named in the reason
    assert pick("cd api && git add -A && git push --force") == "git push"
    assert pick("cat Makefile && npm run build && npm test") == "npm run build"  # first to act
    assert pick("ls && cat x") == "cat"  # nothing but inspection: the line's own key
    assert pick("cd api && make test", "wrong directory, cd elsewhere") == "make test"
    assert pick("") == ""


@pytest.mark.parametrize(
    ("key", "public"),
    [
        ("pytest", "pytest"),
        ("python -m pytest", "python -m pytest"),
        ("npm run test:unit", "npm run test:unit"),
        ("make test", "make test"),
        ("uv run pytest", "uv run pytest"),
        ("git commit", "git commit"),
        ("docker compose up", "docker compose up"),
        ("python deploy_acme_customer.py", "python <script:93d162>"),
        ("make deploy-customer-acme", "make <target:37bd73>"),
        ("npm run deploy:acme-prod", "npm run <target:f16a85>"),
        ("uvx internal-tool-acme", "uvx <target:b0d558>"),
        ("python -m acme.jobs", "python -m <module:3a8a44>"),
        ("mytool", "<tool:373279>"),
        ("run", "<tool:acba25>"),  # a subcommand word is not a public program name
        ("", ""),
    ],
)
def test_public_key_hashes_project_specific_names(key: str, public: str) -> None:
    assert public_key(key) == public
    assert public_key(public_key(key)) == public or "<" in public


@pytest.mark.parametrize(
    ("failed", "other", "expected"),
    [
        ("pytest", "make test", True),
        ("pytest", "uv run pytest", True),
        ("pytest", "python -m pytest", True),
        ("npm run build", "pnpm build", True),
        ("mypy", "pyright", True),
        ("pytest", "pip install", False),
        ("pytest", "uv sync", False),
        ("npm test", "git commit", False),
        ("pytest", "rm", False),
        ("pytest", "make codegen", False),
        ("ruff check", "ruff format", False),
        ("pytest", "pytest", False),
    ],
)
def test_is_replacement(failed: str, other: str, expected: bool) -> None:
    assert is_replacement(failed, other) is expected


def test_roles_and_setup_commands() -> None:
    assert [command_role(k) for k in ("pytest", "npm run test:unit", "cargo clippy", "tsc")] == [
        "test",
        "test",
        "lint",
        "typecheck",
    ]
    assert command_role("git commit") is None and command_role("mytool") is None
    for key in ("pip install", "uv sync", "npm ci", "docker compose up", "python -m venv"):
        assert is_setup_command(key), key
    for key in ("pytest", "git add", "make test", "mytool", ""):
        assert not is_setup_command(key), key


# ---- robustness ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "key"),
    [
        ("sed -i '' 's/a/b/' f", "sed"),
        ("git commit -m ''", "git commit"),
        ('echo ""', "echo"),
        ("grep '' file", "grep"),
        ("pytest -k '' tests", "pytest"),
        ("printf '' > file", "printf"),
        ("'' ''", ""),
        ("", ""),
        ("python3 - <<EOF\nprint(1)\nEOF", "python <stdin>"),
        ("python3 - <<'PY'\nimport app\nPY", "python <stdin>"),
    ],
)
def test_empty_words_and_stdin_scripts(command: str, key: str) -> None:
    assert parse_command(command).key == key


_FUZZ_ATOMS = (
    "pytest", "git", "commit", "-m", "''", '""', "'", '"', "`", "$(", ")", "(", "{", "}", ";",
    "&&", "||", "|", "&", ">", ">>", "<", "<<EOF", "<<'EOF'", "<<-", "EOF", "\n", "\t", " ", "\\",
    "$", "${X:-", "2>&1", "-", "--", "=", "FOO=", "sed", "-i", "bash", "-c", "sh", "env", "sudo",
    "cd", "src/a.py", "é", "日本語", "‮", "\x1b[2J", "\x00", "#", "!", "*", "?", "[", "]",
    "for", "do", "done", "if", "then", "fi", "while", "case", "esac", "python3", "npm", "run",
    "docker", "compose", "exec", "uv", "make", "time", "xargs", "<(", "<<<", "~", "..", "/",
)  # fmt: skip


def test_parse_command_never_raises_on_shell_ish_noise() -> None:
    import random

    rng = random.Random(20261115)
    for _ in range(4000):
        size = rng.randint(0, 14)
        glue = rng.choice(("", " ", " ", " "))
        command = glue.join(rng.choice(_FUZZ_ATOMS) for _ in range(size))
        parsed = parse_command(command)  # must not raise, whatever the input
        assert isinstance(parsed.key, str) and len(parsed.key) < 200, command
        assert "\n" not in parsed.key, command
        assert public_key(parsed.key) is not None
        path_candidates(parsed.tokens)
        objectionable_step(parsed, command)


def test_chdir_follows_cd_lexically(resolver: PathResolver) -> None:
    cwd = "/work/repo"
    assert resolver.chdir(cwd, ("src",)) == "/work/repo/src"
    assert resolver.chdir(cwd, ("src", "..", "..")) == "/work"
    assert resolver.chdir(cwd, ("~/other",)) == "/home/dev/other"
    assert resolver.chdir(cwd, ()) == cwd
    for unknown in ("-", "$WORK", "build-*", ""):
        assert resolver.chdir(cwd, (unknown,)) is None
    assert resolver.outside("/tmp/scratch") and resolver.outside("../sibling", cwd)
    assert not resolver.outside("src/a.py", cwd) and not resolver.outside("/work/repo/x")


@pytest.mark.parametrize(
    ("command", "moves"),
    [
        ("cd /tmp/x && python repro.py", ("/tmp/x",)),
        ("cd web; cd src && npm test", ("web", "src")),
        ("git -C ../other status", ("../other",)),
        ("make -C docs html", ("docs",)),
        ("npm --prefix web test", ("web",)),
        ("uv --directory tools run pytest", ("tools",)),
        ("pytest -q", ()),
    ],
)
def test_steps_remember_where_they_run(command: str, moves: tuple[str, ...]) -> None:
    assert parse_command(command).steps[-1].moves == moves


@pytest.mark.parametrize(
    ("command", "key"),
    [
        ("node -e 'process.exit(1)'", "node <inline>"),
        ("ruby -e 'puts 1'", "ruby <inline>"),
        ("perl -e 'print 1'", "perl <inline>"),
        ("bun -e 'console.log(1)'", "bun <inline>"),
        ("python3 - <<'PY'\nprint(1)\nPY", "python <stdin>"),
        ("php artisan migrate --force", "php artisan migrate"),
        ("poetry run pytest -q", "poetry run pytest"),
        ("command -v docker", "command"),
    ],
)
def test_round_seven_keys(command: str, key: str) -> None:
    assert normalize_command(command) == key


def test_a_workspace_name_is_not_public() -> None:
    hidden = public_key("yarn workspace acme-billing test")
    assert (
        "acme" not in hidden and hidden.startswith("yarn workspace <") and hidden.endswith("test")
    )
    assert public_key("python <inline>") == "python <inline>"
    assert public_key("python manage.py migrate") == "python manage.py migrate"


def test_drive_letter_roots_ignore_case() -> None:
    resolver = PathResolver(Path("/work/repo"), ["C:\\Users\\Dev\\Shop"])
    for raw in (
        "c:\\users\\dev\\shop\\src\\a.py",
        "C:/Users/Dev/Shop/src/a.py",
        "C:\\USERS\\DEV\\SHOP\\src\\a.py",
    ):
        assert resolver.resolve(raw) == "src/a.py"
    assert resolver.resolve("C:\\Users\\Dev\\Shopping\\a.py") is None
