from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from agent_friction import __version__
from agent_friction.cli import EXIT_ERROR, EXIT_GATE, EXIT_OK, EXIT_USAGE, main, parse_when

FIXTURES = Path(__file__).parent / "fixtures"
INPUT = [
    str(FIXTURES / "shopfront"),
    "--transcripts",
    str(FIXTURES / "transcripts"),
    "--repo-alias",
    "/home/dev/shopfront",
    "--no-git",
]


def run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


def test_no_command_prints_help() -> None:
    code, out, _ = run()
    assert code == EXIT_USAGE
    assert "scan" in out and "compare" in out


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert run("--version")[0] == 0
    assert __version__ in capsys.readouterr().out


def test_scan_terminal() -> None:
    code, out, _ = run("scan", *INPUT, "--color", "never")
    assert code == EXIT_OK
    assert "src/shopfront/api/schema_gen.py" in out
    assert "make test" in out
    assert "\033[" not in out


def test_scan_formats_inferred_from_extension(tmp_path: Path) -> None:
    for ext, marker in (
        (".json", '"schema_version": 1'),
        (".md", "# Agent friction map"),
        (".html", "<!doctype html>"),
    ):
        target = tmp_path / f"report{ext}"
        code, out, err = run("scan", *INPUT, "-o", str(target))
        assert code == EXIT_OK, err
        assert out == ""
        assert marker in target.read_text(encoding="utf-8")
        assert f"wrote {ext.lstrip('.').replace('md', 'markdown')}" in err


def test_scan_no_excerpts() -> None:
    _, out, _ = run("scan", *INPUT, "-f", "json", "--no-excerpts")
    result = json.loads(out)
    excerpts = [e.get("excerpt") for h in result["hotspots"] for e in h["evidence"]]
    assert excerpts and all(x is None for x in excerpts)


def test_fail_on_gate() -> None:
    code, _, err = run("scan", *INPUT, "--fail-on", "50")
    assert code == EXIT_GATE
    assert "schema_gen.py" in err
    assert run("scan", *INPUT, "--fail-on", "99")[0] == EXIT_OK


def test_show_file_dir_command_and_missing(tmp_path: Path) -> None:
    code, out, _ = run("show", "schema_gen.py", "--repo", *INPUT)
    assert code == EXIT_OK and "Evidence" in out and "Recommendations" in out
    saved = tmp_path / "r.json"
    run("scan", *INPUT, "-o", str(saved))
    assert "command: pytest" in run("show", "pytest", "--from", str(saved))[1]
    assert "directory: src/shopfront" in run("show", "src/shopfront", "--from", str(saved))[1]
    code, _, err = run("show", "nope.py", "--from", str(saved))
    assert code == EXIT_ERROR and "no friction recorded" in err


def test_compare(tmp_path: Path) -> None:
    before = tmp_path / "before.json"
    run("scan", *INPUT, "-o", str(before))
    data = json.loads(before.read_text(encoding="utf-8"))
    data["hotspots"][0]["events"] = 1
    after = tmp_path / "after.json"
    after.write_text(json.dumps(data), encoding="utf-8")
    code, out, _ = run("compare", "--allow-overlap", str(before), str(after))
    assert code == EXIT_OK and "Improved (1 target)" in out
    code, out, err = run(
        "compare", "--allow-overlap", str(after), str(before), "-f", "json", "--fail-on-regression"
    )
    assert code == EXIT_GATE
    assert json.loads(out)["changes"][0]["status"] == "regressed"
    path = data["hotspots"][0]["path"]
    assert "--fail-on-regression: 1 target regressed or new" in err
    assert f"{path} (regressed)" in err


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("not json", "not valid JSON"),
        ('{"a": 1}', "not an agent-friction result"),
        ('{"schema_version": 99}', "schema_version 99"),
    ],
)
def test_compare_rejects_bad_input(tmp_path: Path, content: str, message: str) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(content, encoding="utf-8")
    code, _, err = run("compare", "--allow-overlap", str(bad), str(bad))
    assert code == EXIT_ERROR and message in err
    code, _, err = run("compare", "--allow-overlap", str(tmp_path / "missing.json"), str(bad))
    assert code == EXIT_ERROR and "no such file" in err


def test_no_transcripts_is_a_clean_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "empty-config"))
    monkeypatch.setenv("HOME", str(tmp_path))
    code, _, err = run("scan", str(tmp_path))
    assert code == EXIT_ERROR
    assert err.startswith("agent-friction: ") and "Traceback" not in err
    assert "--allow-empty" in err
    code, _, err = run("scan", str(tmp_path), "--no-git")
    assert code == EXIT_ERROR and "--allow-empty" not in err
    code, _, err = run("sources", str(tmp_path))
    assert code == EXIT_ERROR and "No Claude Code transcripts found" in err


def test_sources_lists_discovered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = tmp_path / "cfg"
    from agent_friction.discovery import encode_cwd

    proj = config / "projects" / encode_cwd(str(repo.resolve()))
    proj.mkdir(parents=True)
    (proj / "s.jsonl").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    code, out, _ = run("sources", str(repo))
    assert code == EXIT_OK and "s.jsonl" in out


def test_bad_arguments_are_usage_errors() -> None:
    assert run("scan", "--since", "yesterday-ish")[0] == EXIT_USAGE
    assert run("scan", "--top", "0")[0] == EXIT_USAGE
    assert run("scan", "--fail-on", "101")[0] == EXIT_USAGE


def test_parse_when() -> None:
    now = datetime(2026, 11, 15, tzinfo=timezone.utc)
    assert parse_when("30d", now=now) == datetime(2026, 10, 16, tzinfo=timezone.utc)
    assert parse_when("2w", now=now).day == 1
    assert parse_when("12h", now=now).hour == 12
    assert parse_when("2026-01-31").tzinfo is not None
    assert parse_when("2026-01-31T09:00:00Z").hour == 9


def test_scan_nonexistent_repo_is_an_error(tmp_path: Path) -> None:
    code, _, err = run("scan", str(tmp_path / "nope"), "-t", str(FIXTURES / "transcripts"))
    assert code == EXIT_ERROR and "does not exist" in err


def test_scan_with_no_sessions_in_window_is_an_error() -> None:
    code, _, err = run("scan", *INPUT, "--since", "2030-01-01")
    assert code == EXIT_ERROR and "no agent session touched" in err
    code, out, _ = run("scan", *INPUT, "--since", "2030-01-01", "--allow-empty")
    assert code == EXIT_OK and "No recurring friction" in out


def test_unknown_output_extension_is_a_usage_error(tmp_path: Path) -> None:
    code, _, err = run("scan", *INPUT, "-o", str(tmp_path / "report.xyz"))
    assert code == EXIT_USAGE and "--format" in err
    code, _, _ = run("scan", *INPUT, "-o", str(tmp_path / "report.xyz"), "-f", "markdown")
    assert code == EXIT_OK
    code, _, _ = run("scan", *INPUT, "-o", str(tmp_path / "report.txt"))
    assert code == EXIT_OK
    assert "\033[" not in (tmp_path / "report.txt").read_text(encoding="utf-8")


def test_compare_low_sample_does_not_trip_gate(tmp_path: Path) -> None:
    before = tmp_path / "before.json"
    run("scan", *INPUT, "-o", str(before))
    data = json.loads(before.read_text(encoding="utf-8"))
    data["hotspots"][0]["events"] = 100
    data["totals"]["sessions"] = 2
    after = tmp_path / "after.json"
    after.write_text(json.dumps(data), encoding="utf-8")
    code, _, err = run(
        "compare", "--allow-overlap", str(before), str(after), "--fail-on-regression"
    )
    assert code == EXIT_OK and "low sample" in err
    code, _, _ = run(
        "compare",
        "--allow-overlap",
        str(before),
        str(after),
        "--fail-on-regression",
        "--min-sessions",
        "1",
    )
    assert code == EXIT_GATE


def test_compare_rejects_hollow_result(tmp_path: Path) -> None:
    hollow = tmp_path / "hollow.json"
    hollow.write_text('{"schema_version": 1}', encoding="utf-8")
    code, _, err = run("compare", "--allow-overlap", str(hollow), str(hollow))
    assert code == EXIT_ERROR and "not an agent-friction result" in err


def test_compare_refuses_different_repos(tmp_path: Path) -> None:
    before = tmp_path / "before.json"
    run("scan", *INPUT, "-o", str(before))
    data = json.loads(before.read_text(encoding="utf-8"))
    data["repo"]["name"] = "something-else"
    other = tmp_path / "other.json"
    other.write_text(json.dumps(data), encoding="utf-8")
    code, _, err = run("compare", "--allow-overlap", str(before), str(other))
    assert code == EXIT_ERROR and "different repositories" in err
    assert (
        run("compare", "--allow-overlap", str(before), str(other), "--allow-repo-mismatch")[0]
        == EXIT_OK
    )


def test_show_accepts_a_raw_command_line(tmp_path: Path) -> None:
    saved = tmp_path / "r.json"
    run("scan", *INPUT, "-o", str(saved))
    code, out, _ = run("show", "PYTEST -x tests/test_refunds.py", "--from", str(saved))
    assert code == EXIT_OK and "command: pytest" in out


def test_non_utf8_stdout_does_not_crash() -> None:
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252")
    code = main(["scan", *INPUT, "--color", "never"], stdout=stream, stderr=io.StringIO())
    stream.flush()
    assert code == EXIT_OK
    assert "█" in raw.getvalue().decode("utf-8")


def test_inverted_window_is_a_usage_error() -> None:
    code, _, err = run("scan", *INPUT, "--since", "2026-11-01", "--until", "2026-10-01")
    assert code == EXIT_USAGE and "must be before" in err


def test_output_into_missing_directory_is_an_error(tmp_path: Path) -> None:
    code, _, err = run("scan", *INPUT, "-o", str(tmp_path / "missing" / "x.html"))
    assert code == EXIT_ERROR and "does not exist" in err


def test_sources_with_explicit_transcripts() -> None:
    code, out, _ = run("sources", *INPUT[:-1])  # drop --no-git, not a `sources` flag
    assert code == EXIT_OK and ".jsonl" in out


def test_sources_rejects_non_transcript_file(tmp_path: Path) -> None:
    bogus = tmp_path / "notes.md"
    bogus.write_text("# just markdown\n", encoding="utf-8")
    code, _, err = run("sources", str(tmp_path), "-t", str(bogus))
    assert code == EXIT_ERROR and "not a JSONL transcript" in err


def test_git_only_scan_with_allow_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import shutil
    import subprocess

    if shutil.which("git") is None:
        pytest.skip("git not installed")
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "no-config"))

    def git(*a: str) -> None:
        subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.name", "Dev")
    git("config", "user.email", "dev@example.com")
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-q", "-m", "add a\n\nCo-Authored-By: Claude <noreply@anthropic.com>")
    code, _, err = run("scan", str(repo))
    assert code == EXIT_ERROR and "No Claude Code transcripts" in err
    code, out, err = run("scan", str(repo), "--allow-empty", "-f", "json")
    assert code == EXIT_OK, err
    assert json.loads(out)["settings"]["git"] is True


def test_bad_json_inputs_are_clean_errors(tmp_path: Path) -> None:
    code, _, err = run("compare", "--allow-overlap", str(tmp_path), str(tmp_path))
    assert code == EXIT_ERROR and "is a directory" in err
    good = tmp_path / "r.json"
    run("scan", *INPUT, "-o", str(good))
    utf16 = tmp_path / "ps.json"
    utf16.write_text(good.read_text(encoding="utf-8"), encoding="utf-16")
    assert run("compare", "--allow-overlap", str(good), str(utf16))[0] == EXIT_OK
    garbage = tmp_path / "bin.json"
    garbage.write_bytes(b"\x80\x81\x82{")
    code, _, err = run("compare", "--allow-overlap", str(good), str(garbage))
    assert code == EXIT_ERROR and "Traceback" not in err


def test_absurd_since_and_directory_output_are_usage_errors(tmp_path: Path) -> None:
    assert run("scan", *INPUT, "--since", "99999999999d")[0] == EXIT_USAGE
    code, _, err = run("scan", *INPUT, "-o", str(tmp_path))
    assert code == EXIT_USAGE and "is a directory" in err


def test_bot_author_flag_validates_regex() -> None:
    assert run("scan", *INPUT, "--bot-author", "(")[0] == EXIT_USAGE
    assert run("scan", *INPUT, "--bot-author", "ci-robot")[0] == EXIT_OK


def test_repo_alias_must_be_absolute() -> None:
    assert run("scan", *INPUT, "--repo-alias", "relative/path")[0] == EXIT_USAGE
    assert run("scan", *INPUT, "--repo-alias", "C:\\Users\\dev\\shop")[0] == EXIT_OK


def test_scan_from_a_subdirectory_uses_the_repo_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / "src").mkdir()
    monkeypatch.chdir(tmp_path / "src")
    _, _, err = run("sources", "-t", str(tmp_path / "none.jsonl"))
    assert f"using the repository root {tmp_path}" in err
    assert "pass . to map only this directory" in err
    _, _, err = run("show", "pytest", "-t", str(tmp_path / "none.jsonl"))
    assert "using the repository root" not in err


def test_show_accepts_repo_positionally() -> None:
    code, out, _ = run("show", "pytest", *INPUT)
    assert code == EXIT_OK and "pytest" in out
    assert run("show", "pytest", *INPUT, "--repo", INPUT[0])[0] == EXIT_USAGE


def test_show_finds_a_pseudonymized_command_by_its_real_name(tmp_path: Path) -> None:
    from agent_friction.attribution import public_key

    saved = tmp_path / "private.json"
    assert run("scan", *INPUT, "--no-excerpts", "-o", str(saved))[0] == EXIT_OK
    data = json.loads(saved.read_text(encoding="utf-8"))
    hashed = public_key("make deploy-acme")
    assert hashed != "make deploy-acme"
    data["commands"].append({**data["commands"][0], "path": hashed})
    saved.write_text(json.dumps(data), encoding="utf-8")
    code, out, _ = run("show", "make deploy-acme", "--from", str(saved))
    assert code == EXIT_OK and hashed in out


def test_agent_commit_flag_validates_regex() -> None:
    assert run("scan", *INPUT, "--agent-commit", "[")[0] == EXIT_USAGE
    assert run("scan", *INPUT, "--agent-commit", "Generated-by: mybot")[0] == EXIT_OK


def test_scan_of_a_missing_repo_says_so() -> None:
    code, _, err = run("scan", "/no/such/repo/anywhere")
    assert code == EXIT_ERROR and "does not exist" in err


def test_output_dash_is_stdout() -> None:
    code, out, _ = run("scan", *INPUT, "-f", "json", "-o", "-")
    assert code == EXIT_OK and json.loads(out)["schema_version"] == 1


def test_compare_writes_to_a_file_with_format_from_extension(tmp_path: Path) -> None:
    saved = tmp_path / "scan.json"
    assert run("scan", *INPUT, "-o", str(saved))[0] == EXIT_OK
    report = tmp_path / "cmp.md"
    code, out, _ = run("compare", "--allow-overlap", str(saved), str(saved), "-o", str(report))
    assert code == EXIT_OK and out == ""
    assert report.read_text(encoding="utf-8").startswith("# Friction comparison")


def test_an_unwritable_output_is_an_error_not_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def deny(*_a: object, **_k: object) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "write_text", deny)
    code, _, err = run("scan", *INPUT, "-o", str(tmp_path / "x.html"))
    assert code == EXIT_ERROR and "cannot write" in err and "Permission denied" in err


def test_a_failing_stdout_is_an_error_not_a_traceback() -> None:
    class Full(io.StringIO):
        def write(self, s: str) -> int:
            raise OSError(28, "No space left on device")

    err = io.StringIO()
    code = main(["scan", *INPUT, "-f", "json"], stdout=Full(), stderr=err)
    assert code == EXIT_ERROR and "No space left" in err.getvalue()


def test_compare_rejects_damaged_entries(tmp_path: Path) -> None:
    saved = tmp_path / "scan.json"
    assert run("scan", *INPUT, "-o", str(saved))[0] == EXIT_OK
    data = json.loads(saved.read_text(encoding="utf-8"))
    data["commands"] = [1, 2]
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(data), encoding="utf-8")
    code, _, err = run("compare", "--allow-overlap", str(saved), str(bad))
    assert code == EXIT_ERROR and "commands[0] is not an object" in err


@pytest.mark.parametrize(
    ("damage", "message"),
    [
        (lambda d: d["hotspots"][0].pop("events"), "hotspots[0].events is missing"),
        (lambda d: d["hotspots"][0].update(sessions="3"), "hotspots[0].sessions is not an"),
        (lambda d: d["commands"][0].update(score=None), "commands[0].score is not a number"),
        (lambda d: d["hotspots"][1].pop("path"), "hotspots[1].path is missing"),
        (lambda d: d["exposure"]["file"].update(x="2"), "exposure.file is not a map"),
    ],
)
def test_compare_names_the_damaged_field(tmp_path: Path, damage: Any, message: str) -> None:
    saved = tmp_path / "scan.json"
    run("scan", *INPUT, "-o", str(saved))
    data = json.loads(saved.read_text(encoding="utf-8"))
    damage(data)
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(data), encoding="utf-8")
    code, _, err = run("compare", "--allow-overlap", str(saved), str(bad))
    assert code == EXIT_ERROR and message in err and "Traceback" not in err


def test_package_root_exports_the_python_api() -> None:
    import agent_friction

    assert agent_friction.analyze.__name__ == "analyze"
    assert agent_friction.AnalyzeOptions.__name__ == "AnalyzeOptions"


def test_scan_warns_about_unreadable_transcripts(monkeypatch: pytest.MonkeyPatch) -> None:
    import agent_friction.cli as cli

    real = cli._analyze

    def fake(args: object, repo: Path, err: object = None) -> dict:  # type: ignore[type-arg]
        result = real(args, repo)  # type: ignore[arg-type]
        result["sources"].append(
            {"kind": "unreadable", "path": "x.jsonl", "error": "permission denied"}
        )
        return result

    monkeypatch.setattr(cli, "_analyze", fake)
    code, _, err = run("scan", *INPUT)
    assert code == EXIT_OK and "skipped x.jsonl: permission denied" in err


def test_more_than_fifty_hotspots_are_all_kept(tmp_path: Path) -> None:
    """--top trims the terminal view only; JSON, show and compare see every hotspot."""
    repo = tmp_path / "repo"
    repo.mkdir()
    lines = []
    n = 0
    for f in range(60):
        (repo / f"m{f:02}.py").write_text("x = 1\n")
        for s in range(2):
            n += 1
            sid, minute = f"s{f}-{s}", f % 50
            base = f"2026-11-05T{10 + s:02}:{minute:02}"
            lines += [
                {"session": sid, "ts": base + ":00Z", "kind": "user_message", "text": "go"},
                {
                    "session": sid,
                    "ts": base + ":05Z",
                    "kind": "tool_call",
                    "id": n,
                    "op": "edit",
                    "path": f"m{f:02}.py",
                    "old": "nope",
                    "new": "y",
                    "cwd": str(repo),
                },
                {
                    "session": sid,
                    "ts": base + ":09Z",
                    "kind": "tool_result",
                    "id": n,
                    "ok": False,
                    "error": "edit_not_found",
                    "output": "String to replace not found",
                },
            ]
    log = tmp_path / "log.jsonl"
    log.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    args = [str(repo), "--transcripts", str(log), "--no-git"]
    code, out, _ = run("scan", *args, "-f", "json")
    assert code == EXIT_OK
    assert len(json.loads(out)["hotspots"]) == 60
    code, out, _ = run("scan", *args, "--top", "3")
    assert code == EXIT_OK and "m59.py" not in out.split("What to fix first")[0]
    code, out, _ = run("show", "m59.py", *args)
    assert code == EXIT_OK and "m59.py" in out
    saved = tmp_path / "a.json"
    run("scan", *args, "-o", str(saved))
    code, out, _ = run("compare", "--allow-overlap", str(saved), str(saved), "-f", "json")
    assert code == EXIT_OK
    assert len(json.loads(out)["changes"]) == 60


def test_option_abbreviations_are_rejected(capsys: pytest.CaptureFixture[str]) -> None:
    code, _, _ = run("scan", *INPUT, "--no-exc")
    assert code == EXIT_USAGE and "unrecognized arguments: --no-exc" in capsys.readouterr().err


def test_show_accepts_an_absolute_or_cwd_relative_path(monkeypatch: pytest.MonkeyPatch) -> None:
    target = (FIXTURES / "shopfront" / "api" / "openapi.yaml").resolve()
    code, out, _ = run("show", str(target), "--repo", *INPUT)
    assert code == EXIT_OK and "file: api/openapi.yaml" in out
    monkeypatch.chdir(FIXTURES / "shopfront" / "api")
    code, out, _ = run("show", "openapi.yaml", "--repo", *INPUT)
    assert code == EXIT_OK and "file: api/openapi.yaml" in out


def test_missing_transcript_path_is_an_error_even_with_allow_empty(tmp_path: Path) -> None:
    code, _, err = run("scan", str(tmp_path), "-t", str(tmp_path / "nope"), "--allow-empty")
    assert code == EXIT_ERROR and "transcript path does not exist" in err


def test_compare_json_is_a_versioned_document(tmp_path: Path) -> None:
    saved = tmp_path / "a.json"
    run("scan", *INPUT, "-o", str(saved))
    code, out, _ = run("compare", "--allow-overlap", str(saved), str(saved), "-f", "json")
    doc = json.loads(out)
    assert code == EXIT_OK and doc["schema_version"] == 1
    assert doc["tool"]["version"] == __version__ and doc["before"]["sessions"] == 6
    assert isinstance(doc["changes"], list) and doc["low_sample"] is False


def test_show_with_an_empty_target_is_a_usage_error() -> None:
    code, _, err = run("show", "  ", "--repo", *INPUT)
    assert code == EXIT_USAGE and "needs a target" in err


def _windowed(tmp_path: Path, name: str, first: str, last: str) -> Path:
    saved = tmp_path / "base.json"
    if not saved.exists():
        run("scan", *INPUT, "-o", str(saved))
    data = json.loads(saved.read_text(encoding="utf-8"))
    data["window"].update(first_event=first, last_event=last, start=first, end=last)
    out = tmp_path / name
    out.write_text(json.dumps(data), encoding="utf-8")
    return out


def test_compare_refuses_overlapping_or_swapped_windows(tmp_path: Path) -> None:
    before = _windowed(tmp_path, "b.json", "2026-10-01T00:00:00Z", "2026-10-31T12:00:00Z")
    overlapping = _windowed(tmp_path, "o.json", "2026-10-01T00:00:00Z", "2026-11-30T00:00:00Z")
    later = _windowed(tmp_path, "l.json", "2026-11-01T00:00:00Z", "2026-11-30T00:00:00Z")
    code, _, err = run("compare", str(before), str(overlapping))
    assert code == EXIT_ERROR and "overlap" in err and "--since 2026-10-31T12:00:01Z" in err
    code, _, err = run("compare", str(later), str(before), "--fail-on-regression")
    assert code == EXIT_ERROR and "swapped" in err
    assert run("compare", str(before), str(later))[0] == EXIT_OK
    assert run("compare", "--allow-overlap", str(before), str(overlapping))[0] == EXIT_OK


def test_sources_names_copies_and_encodings(tmp_path: Path) -> None:
    line = {"session": "s", "ts": "2026-11-05T10:00:00Z", "kind": "user_message", "text": "go"}
    (tmp_path / "a.jsonl").write_text(json.dumps(line) + "\n", encoding="utf-8")
    (tmp_path / "b.jsonl").write_text(json.dumps(line) + "\n", encoding="utf-8")
    other = {**line, "session": "t"}
    (tmp_path / "c.jsonl").write_text(json.dumps(other) + "\n", encoding="utf-16")
    code, out, _ = run("sources", str(tmp_path), "-t", str(tmp_path))
    assert code == EXIT_OK
    assert "repeat an earlier file" in out and "read as utf-16" in out


def test_show_json_and_output_file(tmp_path: Path) -> None:
    saved = tmp_path / "r.json"
    run("scan", *INPUT, "-o", str(saved))
    code, out, _ = run("show", "pytest", "--from", str(saved), "-f", "json")
    doc = json.loads(out)
    assert code == EXIT_OK and doc["hotspot"]["path"] == "pytest" and doc["directory"] is None
    target = tmp_path / "detail.txt"
    code, out, _ = run("show", "src/shopfront", "--from", str(saved), "-o", str(target))
    assert code == EXIT_OK and out == ""
    assert "directory: src/shopfront" in target.read_text(encoding="utf-8")


def test_compare_accepts_color(tmp_path: Path) -> None:
    saved = tmp_path / "a.json"
    run("scan", *INPUT, "-o", str(saved))
    data = json.loads(saved.read_text(encoding="utf-8"))
    data["hotspots"][0]["events"] = 1
    after = tmp_path / "b.json"
    after.write_text(json.dumps(data), encoding="utf-8")
    code, out, _ = run("compare", "--allow-overlap", "--color", "always", str(saved), str(after))
    assert code == EXIT_OK and "\033[" in out
    assert "\033[" not in run("compare", "--allow-overlap", str(saved), str(after))[1]


def test_no_session_in_repo_names_where_sessions_ran() -> None:
    code, _, err = run(
        "scan",
        str(FIXTURES / "shopfront"),
        "--transcripts",
        str(FIXTURES / "transcripts"),
        "--no-git",
    )
    assert code == EXIT_ERROR and "--repo-alias /home/dev/shopfront" in err


def test_format_accepts_extension_spellings() -> None:
    code, out, _ = run("scan", *INPUT, "-f", "md")
    assert code == EXIT_OK and out.startswith("# Agent friction map")
    code, out, _ = run("scan", *INPUT, "-f", "txt")
    assert code == EXIT_OK and out.startswith("agent-friction · ")


def test_outside_a_git_repository_says_so(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _, _, err = run("sources", "-t", str(tmp_path / "none.jsonl"))
    assert "is not inside a git repository" in err


def test_removed_worktree_sessions_are_pointed_out(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_friction import cli, discovery

    monkeypatch.setattr(discovery, "removed_sibling_sessions", lambda repo: [("/w/shop-wt2", 2)])
    err = io.StringIO()
    cli._note_removed_worktrees(Path(), err)
    assert "2 sessions ran in /w/shop-wt2" in err.getvalue()
    assert "--repo-alias /w/shop-wt2" in err.getvalue()


def test_compare_rejects_deeply_nested_json_cleanly(tmp_path: Path) -> None:
    deep = tmp_path / "deep.json"
    deep.write_text('{"a":' + "[" * 100000 + "]" * 100000 + "}", encoding="utf-8")
    code, _, err = run("compare", "--allow-overlap", str(deep), str(deep))
    # Python 3.14's decoder parses it (then it is not a result); older ones hit the depth limit.
    assert code == 1 and err.startswith("agent-friction: ")
    assert "nested too deeply" in err or "not an agent-friction result" in err


def test_show_lists_directories_when_a_command_ran_in_several(tmp_path: Path) -> None:
    before = tmp_path / "scan.json"
    run("scan", *INPUT, "-o", str(before))
    data = json.loads(before.read_text(encoding="utf-8"))
    base = dict(data["commands"][0])
    data["commands"] = [
        {**base, "path": "npm test (packages/web/)"},
        {**base, "path": "npm test (packages/api/)"},
    ]
    scan = tmp_path / "two.json"
    scan.write_text(json.dumps(data), encoding="utf-8")
    code, _, err = run("show", "npm test", "--from", str(scan))
    assert code == EXIT_USAGE
    assert "ran in several directories" in err and "'npm test (packages/api/)'" in err
    data["commands"] = data["commands"][:1]
    scan.write_text(json.dumps(data), encoding="utf-8")
    code, out, _ = run("show", "npm test", "--from", str(scan))
    assert code == EXIT_OK and "packages/web/" in out


def test_show_from_a_saved_scan_refuses_flags_it_cannot_honour(tmp_path: Path) -> None:
    saved = tmp_path / "scan.json"
    run("scan", *INPUT, "-o", str(saved))
    target = json.loads(saved.read_text(encoding="utf-8"))["commands"][0]["path"]
    code, out, err = run("show", target, "--from", str(saved), "--no-excerpts")
    assert code == EXIT_USAGE and "scan --no-excerpts" in err and out == ""
    for flag in (["--since", "7d"], ["-t", str(tmp_path / "nope")], ["--no-git"]):
        code, _, err = run("show", target, "--from", str(saved), *flag)
        assert code == EXIT_USAGE and "not valid with --from" in err, flag
    code, out, _ = run("show", target, "--from", str(saved), "--max-evidence", "1", "-f", "json")
    assert code == EXIT_OK
    private = tmp_path / "private.json"
    run("scan", *INPUT, "--no-excerpts", "-o", str(private))
    hidden = json.loads(private.read_text(encoding="utf-8"))["commands"][0]["path"]
    code, _, _ = run("show", hidden, "--from", str(private), "--no-excerpts")
    assert code == EXIT_OK


def test_argument_errors_lead_with_the_message(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["scan", "--top", "0"])
    assert code == EXIT_USAGE
    err = capsys.readouterr().err
    assert err.startswith("agent-friction scan: error: argument --top")
    assert "usage:" not in err and "--help" in err


def test_a_shallow_clone_is_noted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import agent_friction.cli as cli

    monkeypatch.setattr(cli, "_is_shallow", lambda repo: True)
    with_git = [a for a in INPUT if a != "--no-git"]
    _, _, err = run("scan", *with_git)
    assert "shallow clone" in err
    _, _, err = run("scan", *INPUT)
    assert "shallow clone" not in err
    monkeypatch.undo()
    assert cli._is_shallow(tmp_path / "missing") is False


def test_columns_narrows_the_terminal_table(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_friction.cli import _term_width

    monkeypatch.setenv("COLUMNS", "60")
    assert _term_width(None, io.StringIO()) == 60
    assert _term_width(Path("out.txt"), io.StringIO()) == 100
    monkeypatch.setenv("COLUMNS", "20")
    assert _term_width(None, io.StringIO()) == 40
    monkeypatch.delenv("COLUMNS")
    assert _term_width(None, io.StringIO()) == 100


# ---- Git Bash path conversion ---------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "meant"),
    [
        ("C:/Program Files/Git/home/dev/shop", "/home/dev/shop"),
        ("C:/msys64/home/me/x", "/home/me/x"),
        ("c:/program files/git/Users/me/code", "/Users/me/code"),
    ],
)
def test_a_path_git_bash_rewrote_is_recognized(given: str, meant: str) -> None:
    from agent_friction.cli import undo_msys_rewrite

    assert undo_msys_rewrite(given) == meant


@pytest.mark.parametrize(
    "given",
    [
        "/home/dev/shop",
        r"C:\Users\Dev\Shop",
        "C:/Users/Dev/Shop",
        "C:/Program Files/Git",
        "D:/work/git/data/proj",  # a checkout under a directory that happens to be "git"
        "D:/Tools/Git/tmp/repo",  # not a usual install root
    ],
)
def test_ordinary_paths_are_left_alone(given: str) -> None:
    from agent_friction.cli import undo_msys_rewrite

    assert undo_msys_rewrite(given) is None


def test_scan_undoes_a_git_bash_rewrite_and_says_how_to_stop_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_friction import cli

    rewritten = "C:/Program Files/Git/home/dev/shopfront"
    argv = [rewritten if a == "/home/dev/shopfront" else a for a in INPUT]
    # elsewhere than Git Bash, a path is taken as given
    monkeypatch.setattr(cli, "under_git_bash", lambda: False)
    code, _, err = run("scan", *argv, "--color", "never")
    assert code == EXIT_ERROR and "Git Bash" not in err
    monkeypatch.setattr(cli, "under_git_bash", lambda: True)
    code, out, err = run("scan", *argv, "--color", "never")
    assert code == EXIT_OK and "schema_gen.py" in out
    assert "Git Bash rewrote --repo-alias /home/dev/shopfront" in err
    assert "MSYS_NO_PATHCONV=1" in err


def test_no_session_hint_names_where_the_sessions_ran(tmp_path: Path) -> None:
    code, _, err = run(
        "scan",
        str(tmp_path),
        "--transcripts",
        str(FIXTURES / "transcripts"),
        "--repo-alias",
        "/somewhere/else",
        "--no-git",
    )
    assert code == EXIT_ERROR
    assert "pass --repo-alias /home/dev/shopfront" in err


# ---- demo, prompt, skill ---------------------------------------------------------------


def test_demo_needs_no_input(tmp_path: Path) -> None:
    code, out, err = run("demo", "--color", "never")
    assert code == EXIT_OK and "schema_gen.py" in out and "make test" in out
    assert "agent-friction demo -o demo.html" in err
    target = tmp_path / "demo.html"
    code, _, err = run("demo", "-o", str(target))
    assert code == EXIT_OK and "<html" in target.read_text(encoding="utf-8")
    assert "bundled demo" not in err


def test_scan_from_a_saved_result_renders_the_prompt(tmp_path: Path) -> None:
    saved = tmp_path / "scan.json"
    assert run("scan", *INPUT, "-f", "json", "-o", str(saved))[0] == EXIT_OK
    code, out, _ = run("scan", "--from", str(saved), "-f", "prompt", str(FIXTURES / "shopfront"))
    assert code == EXIT_OK
    assert out.startswith("# Reduce agent friction in shopfront")
    assert "make test" in out
    code, _, err = run("scan", "--from", str(saved), "--since", "30d", str(tmp_path))
    assert code == EXIT_USAGE and "not valid with --from" in err


def test_install_skill(tmp_path: Path) -> None:
    from agent_friction.cli import SKILL_NAME, skill_text

    text = skill_text()
    assert text.startswith("---\nname: agent-friction-fix\n")
    assert "-f prompt" in text and "approv" in text
    code, out, _ = run("install-skill", "--print")
    assert code == EXIT_OK and out == text
    code, _, err = run("install-skill", "--project", str(tmp_path))
    target = tmp_path / ".claude" / "skills" / SKILL_NAME / "SKILL.md"
    assert code == EXIT_OK and target.read_text(encoding="utf-8") == text
    assert f"/{SKILL_NAME}" in err
    assert run("install-skill", "--project", str(tmp_path))[0] == EXIT_OK  # up to date
    target.write_text("mine\n", encoding="utf-8")
    code, _, err = run("install-skill", "--project", str(tmp_path))
    assert code == EXIT_ERROR and "--force" in err
    assert target.read_text(encoding="utf-8") == "mine\n"
    assert run("install-skill", "--project", str(tmp_path), "--force")[0] == EXIT_OK
    assert target.read_text(encoding="utf-8") == text
    code, _, err = run("install-skill", "--project", str(tmp_path / "nope"))
    assert code == EXIT_ERROR and "not a directory" in err


def test_install_skill_defaults_to_the_home_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert run("install-skill")[0] == EXIT_OK
    assert (tmp_path / ".claude" / "skills" / "agent-friction-fix" / "SKILL.md").is_file()


def test_the_agent_tools_own_rules_are_reported_apart_and_counted_nowhere(tmp_path: Path) -> None:
    from tests.builders import Session

    blocked = "Blocked: sleep {n} followed by: gh run view. To wait for a condition, use Monitor"
    transcripts = tmp_path / "t"
    for i in range(6):
        s = Session(f"s{i}", "/repo").user("watch the build")
        for k in range(5):
            s.tool(
                "Bash",
                {"command": f"sleep {30 + k}; gh run view"},
                blocked.format(n=30 + k),
                is_error=True,
            )
            s.bash("gh run view", "in progress")
        s.save(transcripts)
    repo = tmp_path / "repo"
    repo.mkdir()
    argv = [str(repo), "--transcripts", str(transcripts), "--repo-alias", "/repo", "--no-git"]
    code, out, err = run("scan", *argv, "--color", "never", "--fail-on", "10")
    assert code == EXIT_OK and "sleep" not in err
    assert "The agent tool's own rules (not counted above)" in out
    assert "sleep: refused 30 times in 6 sessions" in out
    assert "Built into" not in out  # the explanation, not a title among the fixes
    assert "Commands that keep failing" not in out
    code, out, _ = run("scan", *argv, "-f", "json")
    data = json.loads(out)
    assert data["commands"] == [] and data["totals"]["friction_events"] == 0
    assert data["totals"]["agent_tool_events"] == 30
    (row,) = data["agent_tool"]
    assert (row["command"], row["events"], row["sessions"]) == ("sleep", 30, 6)
    assert row["recommendations"][0]["origin"] == "agent-tool"
    md = run("scan", *argv, "-f", "markdown")[1]
    assert "## The agent tool's own rules (not counted above)" in md
    prompt = run("scan", *argv, "-f", "prompt")[1]
    assert "## Optional: the agent tool's own rules" in prompt
    assert "## Nothing to change yet" in prompt
    html = run("scan", *argv, "-f", "html")[1]
    assert 'id="agenttool"' in html


# ---- --since with a saved scan ---------------------------------------------------------


def test_since_a_saved_scan_reads_only_what_came_after_it(tmp_path: Path) -> None:
    before, after = tmp_path / "before.json", tmp_path / "after.json"
    assert run("scan", *INPUT, "--until", "2026-11-05", "-o", str(before))[0] == EXIT_OK
    assert run("scan", *INPUT, "--since", str(before), "-o", str(after))[0] == EXIT_OK
    last = json.loads(before.read_text(encoding="utf-8"))["window"]["last_event"]
    first = json.loads(after.read_text(encoding="utf-8"))["window"]["first_event"]
    assert first > last
    # No session on both sides, so compare needs no --allow-overlap.
    code, out, err = run("compare", str(before), str(after), "--color", "never")
    assert code == EXIT_OK, err
    assert "overlap" not in err
    assert "schema_gen.py" in out
    # Nothing came after the later scan.
    code, _, err = run("scan", *INPUT, "--since", str(after))
    assert code == EXIT_ERROR
    assert "no agent session touched" in err


def test_since_a_saved_scan_that_is_missing_or_empty_is_a_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run("scan", *INPUT, "--since", str(tmp_path / "nope.json"))[0] == EXIT_USAGE
    assert "can't read the saved scan" in capsys.readouterr().err
    empty = tmp_path / "empty.json"
    empty.write_text('{"window": {}}', encoding="utf-8")
    assert run("scan", *INPUT, "--since", str(empty))[0] == EXIT_USAGE
    assert "not a saved scan with events" in capsys.readouterr().err
    listed = tmp_path / "list.json"
    listed.write_text("[]", encoding="utf-8")
    assert run("scan", *INPUT, "--since", str(listed))[0] == EXIT_USAGE
    # Anything not ending in .json is still a time.
    assert run("scan", *INPUT, "--since", "2026-11-05", "--color", "never")[0] == EXIT_OK
