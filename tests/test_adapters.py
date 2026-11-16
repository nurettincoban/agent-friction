from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from agent_friction import model
from agent_friction.adapters import claude_code, detect_format, generic, parse_transcript
from agent_friction.adapters.common import classify_error, parse_ts, rejection_reason
from tests import builders
from tests.builders import Session


def kinds(parsed: model.ParsedFile) -> list[str]:
    return [e.kind for s in parsed.streams for e in s.events]


def test_parse_ts_variants() -> None:
    expected = datetime(2026, 11, 2, 9, 0, 1, 500000, tzinfo=timezone.utc)
    assert parse_ts("2026-11-02T09:00:01.500Z") == expected
    assert parse_ts("2026-11-02T09:00:01.5+00:00") == expected
    assert parse_ts("2026-11-02T10:00:01.500000123+0100") == expected
    assert parse_ts("2026-11-02 09:00:01") == expected.replace(microsecond=0)
    assert parse_ts(1793610001.5) == expected
    assert parse_ts(1793610001500) == expected
    for bad in (None, "", "yesterday", "2026-13-45T00:00:00Z", True, [], 1e30):
        assert parse_ts(bad) is None


def test_classify_error_and_rejection() -> None:
    assert classify_error("String to replace not found in file.")[0] == model.ERR_EDIT_NOT_FOUND
    assert classify_error("Found 4 matches of the string to replace, but ...") == (
        model.ERR_EDIT_AMBIGUOUS,
        4,
    )
    assert classify_error("File has not been read yet. Read it first")[0] == model.ERR_NOT_READ
    assert classify_error("File has been modified since read, ...")[0] == model.ERR_STALE
    assert classify_error("File does not exist.")[0] == model.ERR_MISSING
    assert classify_error("something else")[0] == model.ERR_OTHER
    assert rejection_reason("all good") is None
    assert rejection_reason(builders.REJECTED + builders.REJECTED_STOP) == ""
    assert rejection_reason(builders.REJECTED + builders.REJECTED_SAID + "use make\n") == "use make"


def test_claude_basic_events(tmp_path: Path) -> None:
    s = Session()
    s.user("please fix the bug")
    s.say("on it", thinking="secret plan")
    s.read("/repo/a.py", offset=10, limit=5)
    s.edit("/repo/a.py", "x", "y", error=builders.EDIT_AMBIGUOUS, n=7)
    s.bash("pytest -q", "boom", exit_code=2)
    s.grep("Needle", "/repo/src")
    parsed = parse_transcript(s.save(tmp_path))
    assert parsed.kind == "claude-code" and parsed.skipped_lines == 0
    (stream,) = parsed.streams
    assert stream.session_id == "s1" and not stream.sidechain
    # agent prose and thinking leave a time marker and nothing else
    markers = [e for e in stream.events if e.kind == "agent_message"]
    assert markers and all(not (m.text or m.output or m.new_text) for m in markers)
    events = [e for e in stream.events if e.kind != "agent_message"]
    assert [e.kind for e in events] == ["user_message"] + ["tool_call", "tool_result"] * 4
    read, read_result, edit, edit_result, bash, bash_result, grep, grep_result = events[1:]
    assert (read.op, read.path, read.read_range, read.cwd) == (
        "read",
        "/repo/a.py",
        "10:5",
        "/repo",
    )
    assert read_result.ok and read.ts is not None and read.line == 3
    assert edit.op == "edit" and len(edit.edits) == 1
    assert (edit_result.ok, edit_result.error, edit_result.matches) == (False, "edit_ambiguous", 7)
    assert "<tool_use_error>" not in edit_result.output
    assert (bash.op, bash.command) == ("shell", "pytest -q")
    assert (bash_result.ok, bash_result.error, bash_result.exit_code) == (False, "exit", 2)
    assert (grep.op, grep.pattern, grep.path) == ("search", "Needle", "/repo/src")
    assert grep_result.ok and grep_result.empty
    assert all("secret plan" not in (e.text + e.output) for e in events)


def test_claude_user_text_classification(tmp_path: Path) -> None:
    s = Session()
    s.user("real prompt <system-reminder>hidden</system-reminder>")
    s.user("<system-reminder>only a reminder</system-reminder>")
    s.user("<local-command-stdout>ls output</local-command-stdout>")
    s.user("<command-name>/clear</command-name>\n<command-args></command-args>")
    s.user("meta text", isMeta=True)
    s.user("This session is being continued from a previous conversation...")
    s.user("compact summary text", isCompactSummary=True)
    s.user(builders.INTERRUPT, as_blocks=True)
    s.user(builders.INTERRUPT_TOOL)
    s.user("block prompt", as_blocks=True)
    events = parse_transcript(s.save(tmp_path)).streams[0].events
    assert [(e.kind, e.text, e.synthetic) for e in events] == [
        ("user_message", "real prompt", False),
        ("user_message", "", True),
        ("interrupt", builders.INTERRUPT, False),
        ("interrupt", builders.INTERRUPT_TOOL, False),
        ("user_message", "block prompt", False),
    ]


def test_claude_rejection_interrupt_and_content_shapes(tmp_path: Path) -> None:
    s = Session()
    s.user("go")
    s.reject("Edit", {"file_path": "/repo/a.py", "old_string": "a", "new_string": "b"}, "not this")
    s.tool(
        "Bash",
        {"command": "sleep 100"},
        "[Request interrupted by user for tool use]",
        is_error=True,
    )
    s.tool("Bash", {"command": "sleep 5"}, "partial", tool_use_result={"interrupted": True})
    s.tool(
        "Read", {"file_path": "/repo/a.py"}, [{"type": "text", "text": "one"}, {"type": "image"}]
    )
    s.tool("Bash", {"command": "false"}, "Exit code 1")  # failure without is_error
    s.tool("Glob", {"pattern": "*.zz"}, "", tool_use_result={"numFiles": 0, "filenames": []})
    results = [
        e for e in parse_transcript(s.save(tmp_path)).streams[0].events if e.kind == "tool_result"
    ]
    assert (results[0].error, results[0].reason) == ("rejected", "not this")
    assert results[1].error == "interrupted" and results[2].error == "interrupted"
    assert results[3].ok and results[3].output == "one"
    assert (results[4].ok, results[4].error, results[4].exit_code) == (False, "exit", 1)
    assert results[5].ok and results[5].empty


def test_claude_malformed_and_unknown_lines_never_crash(tmp_path: Path) -> None:
    s = Session()
    s.raw("{not json")
    s.raw("[1, 2, 3]")
    s.raw('"just a string"')
    s.raw({"type": "attachment", "attachment": {"x": 1}})
    s.raw({"type": "queue-operation", "operation": "enqueue"})
    s.raw({"type": "summary", "summary": "s"})
    s.raw({"type": "future-thing-nobody-knows"})
    s.raw({"no_type": True})
    s.raw({"type": "user"})  # no message
    s.raw({"type": "user", "message": {"role": "user", "content": 42}})
    s.raw({"type": "assistant", "message": {"role": "assistant", "content": "plain string"}})
    s.raw({"type": "assistant", "message": {"content": [None, 3, {"type": "tool_use"}]}})
    s.raw(
        {
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "id": "t", "name": "X", "input": "str"}]},
        }
    )
    s.raw({"type": "user", "message": {"content": [{"type": "tool_result"}, "junk"]}})
    s.raw("")
    s.user("still parsed")
    parsed = parse_transcript(s.save(tmp_path))
    assert parsed.skipped_lines == 5  # 3 non-object lines + 2 user lines with unusable message
    assert parsed.ignored_lines == 5
    events = [e for e in parsed.streams[0].events if e.kind != "agent_message"]
    assert [(e.kind, e.tool, e.text) for e in events] == [
        ("tool_call", "X", ""),
        ("user_message", "", "still parsed"),
    ]


def test_claude_binary_garbage_file(tmp_path: Path) -> None:
    path = tmp_path / "garbage.jsonl"
    path.write_bytes(b"\xff\xfe\x00\x01not json\n\x80\x81\n")
    parsed = parse_transcript(path)
    assert parsed.streams == [] and parsed.skipped_lines == 2


def test_claude_sidechains_and_subagent_files(tmp_path: Path) -> None:
    main = Session("abc")
    main.user("top level")
    main.raw(
        {
            "type": "user",
            "isSidechain": True,
            "sessionId": "abc",
            "uuid": "side-1",
            "message": {"role": "user", "content": "inline sidechain prompt"},
        }
    )
    parsed = parse_transcript(main.save(tmp_path))
    assert [(s.sidechain, len(s.events)) for s in parsed.streams] == [(False, 1), (True, 1)]

    sub = Session("ignored-id", sidechain=True)
    sub.user("task prompt").read("/repo/a.py")
    path = sub.save(tmp_path / "abc" / "subagents", "agent-1")
    assert claude_code.session_id_for(path) == "abc"
    (stream,) = parse_transcript(path).streams
    assert stream.session_id == "abc" and stream.sidechain


def test_claude_duplicate_uuids_are_dropped_across_files(tmp_path: Path) -> None:
    first = Session("one")
    first.user("hello").read("/repo/a.py")
    resumed = Session("two")
    resumed.lines = list(first.lines)  # a resumed session replays the old lines
    resumed.user("and more")
    seen: dict[str, str] = {}
    a = parse_transcript(first.save(tmp_path), seen)
    b = parse_transcript(resumed.save(tmp_path), seen)
    assert len(kinds(a)) == 3
    assert [e.text for s in b.streams for e in s.events] == ["and more"]
    # the resumed file continues the first session: one piece of work, one session
    assert [s.session_id for s in b.streams] == ["one"]
    assert set(seen.values()) == {"one"}


def test_format_detection(tmp_path: Path) -> None:
    claude = Session().user("hi").save(tmp_path, "claude")
    gen = tmp_path / "gen.jsonl"
    gen.write_text('garbage\n{"session": "s", "kind": "user_message", "text": "hi"}\n')
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    assert detect_format(claude) == "claude-code"
    assert detect_format(gen) == "generic"
    assert detect_format(empty) == "claude-code"
    assert detect_format(tmp_path / "missing.jsonl") == "claude-code"


def test_generic_format(tmp_path: Path) -> None:
    lines: list[object] = [
        {"session": "s1", "ts": "2026-11-05T16:00:00Z", "kind": "user_message", "text": "do it"},
        {
            "session": "s1",
            "kind": "tool_call",
            "id": 1,
            "op": "edit",
            "path": "a.py",
            "old": "a",
            "new": "b",
            "cwd": "/repo",
        },
        {
            "session": "s1",
            "kind": "tool_result",
            "id": 1,
            "ok": False,
            "output": "String to replace not found",
        },
        {"session": "s1", "kind": "tool_call", "id": "2", "op": "shell", "command": "make"},
        {"session": "s1", "kind": "tool_result", "id": "2", "exit_code": 2, "output": "nope"},
        {
            "session": "s1",
            "kind": "tool_call",
            "id": 3,
            "op": "search",
            "pattern": "X",
            "tool": "ripgrep",
        },
        {"session": "s1", "kind": "tool_result", "id": 3, "matches": 0},
        {"session": "s1", "kind": "tool_call", "id": 4, "op": "teleport"},
        {
            "session": "s1",
            "kind": "tool_result",
            "id": 4,
            "ok": False,
            "error": "rejected",
            "reason": "no",
        },
        {"session": "s1", "kind": "interrupt"},
        {"session": "s2", "kind": "user_message", "text": "sub", "subagent": True},
        {"session": "s1", "kind": "telemetry", "text": "ignored kind"},
        {"session": "s1", "kind": "tool_call"},  # no id: skipped
        {"kind": "user_message", "text": "no session"},  # skipped
        {"session": "s1", "kind": "user_message", "text": "   "},  # skipped
        "not an object",
    ]
    path = tmp_path / "g.jsonl"
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n{broken\n")
    parsed = generic.parse_file(path)
    assert (parsed.kind, parsed.skipped_lines, parsed.ignored_lines) == ("generic", 5, 1)
    main, sub = parsed.streams
    assert (main.session_id, main.sidechain, sub.session_id, sub.sidechain) == (
        "s1",
        False,
        "s2",
        True,
    )
    ev = main.events
    assert ev[0].ts == datetime(2026, 11, 5, 16, tzinfo=timezone.utc)
    assert (ev[1].op, ev[1].path, ev[1].cwd, len(ev[1].edits)) == ("edit", "a.py", "/repo", 1)
    assert (ev[2].ok, ev[2].error) == (False, "edit_not_found")
    assert (ev[4].ok, ev[4].error, ev[4].exit_code) == (False, "exit", 2)
    assert (ev[5].tool, ev[6].empty) == ("ripgrep", True)
    assert (ev[7].op, ev[8].error, ev[8].reason) == ("other", "rejected", "no")
    assert ev[9].kind == "interrupt"


def test_error_tag_inside_successful_output_is_not_an_error(tmp_path: Path) -> None:
    printed = 'def test_x():\n    assert "<tool_use_error>String to replace not found" in out\n'
    s = Session().user("go")
    s.bash("sed -n '1,5p' tests/test_edit.py", printed)
    s.tool("Read", {"file_path": "/repo/a.py"}, "1\u2192[Request interrupted by user] is a marker")
    s.tool("Edit", {"file_path": "/repo/a.py"}, builders.NOT_READ)  # tag first, no is_error flag
    results = [
        e for e in parse_transcript(s.save(tmp_path)).streams[0].events if e.kind == "tool_result"
    ]
    assert [(r.ok, r.error) for r in results] == [(True, ""), (True, ""), (False, "not_read")]
    assert "<tool_use_error>" in results[0].output  # successful output is kept verbatim


def test_question_detection_keeps_one_bit_of_agent_prose() -> None:
    from agent_friction.adapters.common import QUESTION_TOOL_RE, asks_question

    assert asks_question("Done.\n\nWant me to refactor util.py? It would take a few minutes.")
    assert asks_question("Which one?")
    assert not asks_question("Why did it fail? A typo.\n\nFixed in a.py.")
    assert not asks_question("") and not asks_question("Done.")
    for tool in ("AskUserQuestion", "ask_user", "ExitPlanMode", "ask_followup_question"):
        assert QUESTION_TOOL_RE.search(tool), tool
    for tool in ("Bash", "Edit", "Task", "TodoWrite", "WebSearch"):
        assert not QUESTION_TOOL_RE.search(tool), tool


def test_pure_insertions_are_flagged_without_keeping_text(tmp_path: Path) -> None:
    s = Session().user("go")
    s.edit("/repo/a.py", "x = 1", "x = 1\nprint(x)").edit("/repo/a.py", "x = 1", "x = 2")
    (stream,) = parse_transcript(s.save(tmp_path)).streams
    first, second = (e for e in stream.events if e.kind == model.TOOL_CALL)
    assert first.insertions == frozenset(first.edits) and second.insertions == frozenset()
    assert "print" not in repr(first.edits)


def test_generic_events_repeated_across_files_are_read_once(tmp_path: Path) -> None:
    lines = [
        {"session": "s1", "ts": "2026-11-05T16:00:00Z", "kind": "user_message", "text": "go"},
        {"session": "s1", "ts": "2026-11-05T16:00:01Z", "kind": "tool_call", "id": 1, "op": "shell",
         "command": "make"},
        {"session": "s1", "ts": "2026-11-05T16:00:02Z", "kind": "tool_result", "id": 1,
         "exit_code": 2, "output": "nope"},
    ]  # fmt: skip
    text = "\n".join(json.dumps(line) for line in lines) + "\n"
    first, copy = tmp_path / "a.jsonl", tmp_path / "copy.jsonl"
    first.write_text(text)
    copy.write_text(text)
    seen: dict[str, str] = {}
    one = parse_transcript(first, seen)
    two = parse_transcript(copy, seen)
    assert sum(len(s.events) for s in one.streams) == 3
    assert sum(len(s.events) for s in two.streams) == 0
    assert sum(len(s.events) for s in parse_transcript(copy).streams) == 3


def test_implausible_timestamps_are_missing() -> None:
    assert parse_ts("1970-01-01T00:00:05Z") is None and parse_ts(12) is None
    assert parse_ts("1999-12-31T23:59:59Z") is None and parse_ts("not a time") is None
    assert parse_ts("2000-01-01T00:00:00Z") == datetime(2000, 1, 1, tzinfo=timezone.utc)


def test_absurdly_nested_json_is_a_skipped_line(tmp_path: Path) -> None:
    deep = '{"a":' * 100_000 + "1" + "}" * 100_000
    s = Session().user("go").bash("make", "nope", exit_code=2)
    path = tmp_path / "deep.jsonl"
    path.write_text(s.text() + deep + "\n")
    parsed = parse_transcript(path)
    # Python 3.14 can parse it (then it is a line of no known type, ignored); older
    # versions raise RecursionError, which must count as a skipped line, not a crash.
    assert parsed.skipped_lines + parsed.ignored_lines == 1
    assert sum(len(st.events) for st in parsed.streams) == 3
    generic_file = tmp_path / "g.jsonl"
    generic_file.write_text(
        json.dumps({"session": "s", "kind": "user_message", "text": "go"}) + "\n" + deep + "\n"
    )
    g = generic.parse_file(generic_file)
    assert g.skipped_lines + g.ignored_lines == 1


def test_lone_surrogates_are_replaced_at_the_boundary(tmp_path: Path) -> None:
    assert model.load_json_line('{"a\\ud83d": ["x\\udc00y", {"b": "\\ud83d\\ude00 ok"}]}') == {
        "a\ufffd": ["x\ufffdy", {"b": "\U0001f600 ok"}]  # a whole pair is a character
    }
    assert model.load_json_line('{"path": "C:\\\\users\\\\dev"}') == {"path": "C:\\users\\dev"}
    for kind, line in (
        ("claude", '{"type":"user","sessionId":"s","uuid":"u1","cwd":"/repo",'
                   '"timestamp":"2026-11-05T16:00:00Z",'
                   '"message":{"role":"user","content":"go \\ud83d"}}'),
        ("generic", '{"session":"g","ts":"2026-11-05T16:00:00Z","kind":"user_message",'
                    '"text":"go \\ud83d"}'),
    ):  # fmt: skip
        path = tmp_path / f"{kind}.jsonl"
        path.write_text(line + "\n")
        (stream,) = parse_transcript(path).streams
        assert [e.text for e in stream.events] == ["go \ufffd"]
        stream.events[0].text.encode("utf-8")


def test_powershell_is_a_shell(tmp_path: Path) -> None:
    s = Session(cwd="C:\\Users\\dev\\shop").user("go")
    s.tool("PowerShell", {"command": "npm run build"}, "Exit code 1\nerror TS2304", is_error=True)
    s.tool("Bash", {"command": "npm test"}, "ok")
    s.tool("TodoWrite", {"command": "not a shell"}, "ok")
    (stream,) = parse_transcript(s.save(tmp_path)).streams
    calls = [e for e in stream.events if e.kind == model.TOOL_CALL]
    assert [(e.op, e.command) for e in calls[:2]] == [
        (model.OP_SHELL, "npm run build"),
        (model.OP_SHELL, "npm test"),
    ]
    assert calls[2].op != model.OP_SHELL and not calls[2].command
