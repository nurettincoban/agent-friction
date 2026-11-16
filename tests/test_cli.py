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
    code, out, _ = run(
        "compare", "--allow-overlap", str(after), str(before), "-f", "json", "--fail-on-regression"
    )
    assert code == EXIT_GATE
    assert json.loads(out)["changes"][0]["status"] == "regressed"


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
    assert code == EXIT_USAGE and "does not exist" in err


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

    def fake(args: object, repo: Path) -> dict:  # type: ignore[type-arg]
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
    assert code == EXIT_OK and out == "" and "directory: src/shopfront" in target.read_text(encoding="utf-8")


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
