"""Hand-rolled builders for synthetic transcripts used by the tests and fixtures.

They mirror the on-disk shape of Claude Code session files (one JSON object
per line) closely enough to exercise the real parser, without containing any
real session data.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from agent_friction import signals
from agent_friction.adapters import parse_transcript
from agent_friction.attribution import PathResolver
from agent_friction.model import Finding

EDIT_NOT_FOUND = (
    "<tool_use_error>String to replace not found in file.\nString: {old}</tool_use_error>"
)
EDIT_AMBIGUOUS = (
    "<tool_use_error>Found {n} matches of the string to replace, but replace_all is false. "
    "To replace all occurrences, set replace_all to true. To replace only one occurrence, "
    "please provide more context to uniquely identify the instance.\nString: {old}"
    "</tool_use_error>"
)
NOT_READ = (
    "<tool_use_error>File has not been read yet. Read it first before writing to it."
    "</tool_use_error>"
)
STALE = (
    "<tool_use_error>File has been modified since read, either by the user or by a linter. "
    "Read it again before attempting to write it.</tool_use_error>"
)
MISSING = "<tool_use_error>File does not exist.</tool_use_error>"
REJECTED = (
    "The user doesn't want to proceed with this tool use. The tool use was rejected (eg. if "
    "it was a file edit, the new_string was NOT written to the file). "
)
REJECTED_STOP = "STOP what you are doing and wait for the user to tell you how to proceed."
REJECTED_SAID = "To tell you how to proceed, the user said:\n"
INTERRUPT = "[Request interrupted by user]"
INTERRUPT_TOOL = "[Request interrupted by user for tool use]"

T0 = datetime(2026, 11, 2, 9, 0, 0, tzinfo=timezone.utc)


def session_uuid(name: str) -> str:
    """A stable UUID derived from a readable name."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"agent-friction-fixture/{name}"))


class Session:
    """Builds one Claude Code transcript, advancing a fake clock as it goes."""

    def __init__(
        self,
        session_id: str = "s1",
        cwd: str = "/repo",
        start: datetime = T0,
        *,
        sidechain: bool = False,
        branch: str = "main",
    ) -> None:
        self.session_id = session_id
        self.cwd = cwd
        self.now = start
        self.sidechain = sidechain
        self.branch = branch
        self.salt = "side" if sidechain else "main"  # keeps line uuids unique per file
        self.lines: list[str] = []
        #: ``permissionDecision`` written on every tool result from now on, if set.
        self.decision: dict[str, Any] | None = None
        self._count = 0
        self._parent: str | None = None

    # ---- low level -------------------------------------------------------------------

    def tick(self, seconds: float) -> Session:
        self.now += timedelta(seconds=seconds)
        return self

    def raw(self, obj: Any) -> Session:
        """Append an arbitrary line (a dict is JSON-encoded, a string is written as is)."""
        self.lines.append(obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False))
        return self

    def _id(self, prefix: str = "") -> str:
        self._count += 1
        return (
            prefix
            + uuid.uuid5(uuid.NAMESPACE_URL, f"{self.session_id}/{self.salt}/{self._count}").hex
        )

    def _line(self, kind: str, message: dict[str, Any], **extra: Any) -> dict[str, Any]:
        line_uuid = str(uuid.UUID(self._id()))
        obj: dict[str, Any] = {
            "parentUuid": self._parent,
            "isSidechain": self.sidechain,
            "userType": "external",
            "cwd": self.cwd,
            "sessionId": self.session_id,
            "version": "2.1.0",
            "gitBranch": self.branch,
            "type": kind,
            "message": message,
            "uuid": line_uuid,
            "timestamp": self.now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            **extra,
        }
        self._parent = line_uuid
        self.raw(obj)
        return obj

    # ---- messages --------------------------------------------------------------------

    def user(self, text: str, *, as_blocks: bool = False, **extra: Any) -> Session:
        content: Any = [{"type": "text", "text": text}] if as_blocks else text
        self._line("user", {"role": "user", "content": content}, **extra)
        return self

    def queued(
        self,
        text: str,
        *,
        as_blocks: bool = False,
        origin: dict[str, Any] | None = None,
        mode: str | None = "prompt",
        meta: bool = False,
    ) -> Session:
        """A message delivered while the agent was working (a `queued_command` attachment)."""
        self.tick(1)
        stamp = self.now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        attachment: dict[str, Any] = {
            "type": "queued_command",
            "prompt": [{"type": "text", "text": text}] if as_blocks else text,
            "source_uuid": str(uuid.UUID(self._id())),
            "timestamp": stamp,
        }
        if mode is not None:
            attachment["commandMode"] = mode
        if origin is not None:
            attachment["origin"] = origin
        if meta:
            attachment["isMeta"] = True
        line_uuid = str(uuid.UUID(self._id()))
        self.raw(
            {
                "parentUuid": self._parent,
                "isSidechain": self.sidechain,
                "cwd": self.cwd,
                "sessionId": self.session_id,
                "type": "attachment",
                "attachment": attachment,
                "uuid": line_uuid,
                "timestamp": stamp,
            }
        )
        self._parent = line_uuid
        return self

    def say(self, text: str, thinking: str | None = None) -> Session:
        blocks: list[dict[str, Any]] = []
        if thinking:
            blocks.append({"type": "thinking", "thinking": thinking, "signature": "sig"})
        blocks.append({"type": "text", "text": text})
        self._assistant(blocks)
        return self

    def _assistant(self, blocks: list[dict[str, Any]]) -> None:
        self._line(
            "assistant",
            {
                "id": self._id("msg_"),
                "type": "message",
                "role": "assistant",
                "model": "claude-test",
                "content": blocks,
                "stop_reason": None,
            },
            requestId=self._id("req_"),
        )

    def call(self, name: str, tool_input: dict[str, Any], *, think: float = 4) -> str:
        """Emit only the assistant tool_use line and return its id."""
        self.tick(think)
        call_id = self._id("toolu_")
        self._assistant([{"type": "tool_use", "id": call_id, "name": name, "input": tool_input}])
        return call_id

    def result(
        self,
        call_id: str,
        content: Any,
        *,
        is_error: bool = False,
        took: float = 2,
        tool_use_result: Any = None,
    ) -> Session:
        self.tick(took)
        block: dict[str, Any] = {"tool_use_id": call_id, "type": "tool_result", "content": content}
        if is_error:
            block["is_error"] = True
        extra: dict[str, Any] = {}
        if tool_use_result is not None:
            extra["toolUseResult"] = tool_use_result
        if self.decision is not None:
            extra["permissionDecision"] = self.decision
        self._line("user", {"role": "user", "content": [block]}, **extra)
        return self

    def tool(
        self,
        name: str,
        tool_input: dict[str, Any],
        content: Any = "ok",
        *,
        is_error: bool = False,
        think: float = 4,
        took: float = 2,
        tool_use_result: Any = None,
    ) -> Session:
        call_id = self.call(name, tool_input, think=think)
        return self.result(
            call_id, content, is_error=is_error, took=took, tool_use_result=tool_use_result
        )

    # ---- tools -----------------------------------------------------------------------

    def read(self, path: str, *, missing: bool = False, **kw: Any) -> Session:
        tool_input = {
            "file_path": path,
            **{k: v for k, v in kw.items() if k in ("offset", "limit")},
        }
        if missing:
            return self.tool("Read", tool_input, MISSING, is_error=True)
        return self.tool("Read", tool_input, "     1→# contents\n     2→...")

    def edit(
        self, path: str, old: str = "old", new: str = "new", *, error: str | None = None, n: int = 3
    ) -> Session:
        tool_input = {"file_path": path, "old_string": old, "new_string": new, "replace_all": False}
        if error is None:
            return self.tool("Edit", tool_input, f"The file {path} has been updated successfully.")
        return self.tool("Edit", tool_input, error.format(old=old, n=n), is_error=True)

    def write(self, path: str, content: str = "data\n", *, error: str | None = None) -> Session:
        tool_input = {"file_path": path, "content": content}
        if error is None:
            return self.tool("Write", tool_input, f"File created successfully at: {path}")
        return self.tool("Write", tool_input, error, is_error=True)

    def bash(
        self, command: str, output: str = "", *, exit_code: int = 0, took: float = 5
    ) -> Session:
        tool_input = {"command": command, "description": "run command"}
        if exit_code:
            text = f"Exit code {exit_code}\n{output}".rstrip()
            return self.tool(
                "Bash", tool_input, text, is_error=True, took=took, tool_use_result=f"Error: {text}"
            )
        structured = {
            "stdout": output,
            "stderr": "",
            "interrupted": False,
            "isImage": False,
            "noOutputExpected": False,
        }
        return self.tool("Bash", tool_input, output, took=took, tool_use_result=structured)

    def grep(self, pattern: str, path: str | None = None, *, found: bool = False) -> Session:
        tool_input: dict[str, Any] = {"pattern": pattern}
        if path is not None:
            tool_input["path"] = path
        text = f"Found 1 file\n{path or '.'}/match.py" if found else "No matches found"
        return self.tool("Grep", tool_input, text)

    def glob(self, pattern: str, path: str | None = None, *, found: bool = False) -> Session:
        tool_input: dict[str, Any] = {"pattern": pattern}
        if path is not None:
            tool_input["path"] = path
        return self.tool("Glob", tool_input, "src/match.py" if found else "No files found")

    def reject(self, name: str, tool_input: dict[str, Any], reason: str | None = None) -> Session:
        text = REJECTED + (REJECTED_SAID + reason if reason else REJECTED_STOP)
        self.tool(name, tool_input, text, is_error=True, took=20)
        if reason is None:
            self.user(INTERRUPT_TOOL, as_blocks=True)
        return self

    def interrupt(self) -> Session:
        return self.tick(1).user(INTERRUPT, as_blocks=True)

    # ---- output ----------------------------------------------------------------------

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"

    def save(self, directory: Path, name: str | None = None) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{name or self.session_id}.jsonl"
        path.write_text(self.text(), encoding="utf-8", newline="\n")
        return path


def detect(
    session: Session | Path,
    tmp_path: Path,
    repo: str = "/repo",
    known: tuple[str, ...] = (),
    files: tuple[str, ...] = (),
    dirs: tuple[str, ...] = (),
) -> list[Finding]:
    """Parse a built session and run the detectors against a purely lexical repo.

    ``known`` are repo-relative paths treated as existing files; every path the
    session touches with a file tool is known as well, as in the real engine.
    ``dirs`` are repo-relative directories that exist.
    """
    path = session if isinstance(session, Path) else session.save(tmp_path)
    resolver = PathResolver(Path(repo))
    parsed = parse_transcript(path)
    touched = set(known) | set(files)
    for stream in parsed.streams:
        for event in stream.events:
            rel = resolver.resolve(event.path, event.cwd) if event.op != "search" else None
            if rel:
                touched.add(rel)
    ctx = signals.DetectContext(
        resolver,
        is_file=lambda rel: rel in touched or "." in rel.rsplit("/", 1)[-1],
        is_known_file=lambda rel: rel in touched,
        first_party=lambda top: any(rel.split("/")[0] in (top, f"{top}.py") for rel in touched),
        exists=lambda rel: rel in touched or rel in dirs,
    )
    found: list[Finding] = []
    for stream in parsed.streams:
        found.extend(signals.detect(stream, ctx))
    return found


def by_signal(findings: list[Finding], signal: str) -> list[Finding]:
    return [f for f in findings if f.signal == signal]
