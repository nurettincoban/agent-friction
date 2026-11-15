from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from pathlib import Path

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
    code, out, _ = run("compare", str(before), str(after))
    assert code == EXIT_OK and "Improved (1)" in out
    code, out, _ = run("compare", str(after), str(before), "-f", "json", "--fail-on-regression")
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
    code, _, err = run("compare", str(bad), str(bad))
    assert code == EXIT_ERROR and message in err
    code, _, err = run("compare", str(tmp_path / "missing.json"), str(bad))
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
    assert code == EXIT_ERROR and "not a directory" in err


def test_scan_with_no_sessions_in_window_is_an_error() -> None:
    code, _, err = run("scan", *INPUT, "--since", "2030-01-01")
    assert code == EXIT_ERROR and "no agent session touched" in err
    code, out, _ = run("scan", *INPUT, "--since", "2030-01-01", "--allow-empty")
    assert code == EXIT_OK and "No recurring friction" in out


def test_unknown_output_extension_is_a_usage_error(tmp_path: Path) -> None:
    code, _, err = run("scan", *INPUT, "-o", str(tmp_path / "report.txt"))
    assert code == EXIT_USAGE and "--format" in err
    code, _, _ = run("scan", *INPUT, "-o", str(tmp_path / "report.txt"), "-f", "markdown")
    assert code == EXIT_OK


def test_compare_low_sample_does_not_trip_gate(tmp_path: Path) -> None:
    before = tmp_path / "before.json"
    run("scan", *INPUT, "-o", str(before))
    data = json.loads(before.read_text(encoding="utf-8"))
    data["hotspots"][0]["events"] = 100
    data["totals"]["sessions"] = 2
    after = tmp_path / "after.json"
    after.write_text(json.dumps(data), encoding="utf-8")
    code, _, err = run("compare", str(before), str(after), "--fail-on-regression")
    assert code == EXIT_OK and "low sample" in err
    code, _, _ = run(
        "compare", str(before), str(after), "--fail-on-regression", "--min-sessions", "1"
    )
    assert code == EXIT_GATE


def test_compare_rejects_hollow_result(tmp_path: Path) -> None:
    hollow = tmp_path / "hollow.json"
    hollow.write_text('{"schema_version": 1}', encoding="utf-8")
    code, _, err = run("compare", str(hollow), str(hollow))
    assert code == EXIT_ERROR and "not an agent-friction result" in err


def test_compare_refuses_different_repos(tmp_path: Path) -> None:
    before = tmp_path / "before.json"
    run("scan", *INPUT, "-o", str(before))
    data = json.loads(before.read_text(encoding="utf-8"))
    data["repo"]["name"] = "something-else"
    other = tmp_path / "other.json"
    other.write_text(json.dumps(data), encoding="utf-8")
    code, _, err = run("compare", str(before), str(other))
    assert code == EXIT_ERROR and "different repositories" in err
    assert run("compare", str(before), str(other), "--allow-repo-mismatch")[0] == EXIT_OK


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
