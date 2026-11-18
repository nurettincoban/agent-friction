"""The error lines a hotspot's failures have in common: what the tool actually said.

A row that says "`go test` failed 3 times" gives the symptom. The line that keeps coming
back in the output ("dial tcp 127.0.0.1:5432: connect: connection refused", "make:
bin/golangci-lint: No such file or directory", "Command timed out after 2m") gives the
mechanism, and usually the fix. This module picks those lines out of the raw output of a
hotspot's events, folds the parts that vary from run to run (numbers, durations, hex ids,
temporary paths) and counts in how many events and sessions each one recurs.

No list of known errors is involved: a line qualifies by looking like an error line, and
it is reported by recurring.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from agent_friction.model import Finding

#: Signatures kept per hotspot, at most.
MAX_SIGNATURES = 3
#: Other spellings of a signature kept.
MAX_VARIANTS = 2
#: Characters of a signature line kept.
SIGNATURE_CHARS = 200
#: Lines of one event's output that are looked at, at most (head and tail).
MAX_LINES = 400

#: Words that make an output line an error line.
_ERROR_RE = re.compile(
    r"\b(?:error|errors|err|fail|failed|failure|fatal|panic|exception|traceback|refused"
    r"|denied|not found|no such|cannot|can't|could not|couldn't|unable|missing|undefined"
    r"|timed out|timeout|killed|segmentation|abort(?:ed)?|unreachable|forbidden|invalid"
    r"|not recognized|not installed|command not found|no module named|unknown|mismatch"
    r"|expected|assert(?:ion)?|found \d+ matches|not set|requires?|exit(?:ed)? (?:code|status))\b"
    r"|✗|✘|×|E\d{3,4}\b|^E\s",
    re.I,
)
#: Lines that are an error line in form only: the echo of the command, the exit code the
#: harness adds, a bare FAIL/ok marker, summary lines of a test runner.
_NOISE_RE = re.compile(
    r"^\s*(?:\$ |String: |Exit code \d+\s*$|FAIL\s*$|ok\s|---\s*$|=+\s*$|-+\s*$|\.+\s*$"
    r"|\d+ (?:failed|passed)|Tests?:\s|Test Files\s|Ran \d+ tests?|FAILED \("
    r"|make: \*\*\* \[.*\] Error \d+\s*$|error: Recipe"
    r"|npm ERR! (?:A complete log|code|path|errno|syscall)|ELIFECYCLE)",
    re.I,
)
#: Lines that say why, not only that: preferred when two lines recur equally often. A
#: test runner's "--- FAIL: TestX" names what failed; "connection refused" says why.
_CAUSE_RE = re.compile(
    r"refused|no such file|not found|command not found|timed out|denied|not set|missing"
    r"|cannot|can't|could not|unable|not installed|no module named|unreachable|forbidden"
    r"|undefined|matches of the string|requires?|expected .* (?:got|but|received)",
    re.I,
)
#: A test runner naming the test that failed (``--- FAIL: TestX``, ``FAILED tests/x.py::t``,
#: ``✗ renders``): a symptom line.
_TEST_NAME_RE = re.compile(r"^\s*(?:--- FAIL:|FAIL:|FAILED\b|[✗✘×] )", re.I)
_HEX_RE = re.compile(r"\b0x[0-9a-f]+\b|\b[0-9a-f]{7,}\b", re.I)
_DURATION_RE = re.compile(r"\b\d+(?:\.\d+)?\s?(?:ms|s|m|h|sec|secs|seconds|minutes)\b")
_NUMBER_RE = re.compile(r"(?<![\w.])\d+(?:\.\d+)?(?![\w.])")
_TMP_RE = re.compile(r"/(?:tmp|var/folders|private/var)/\S+")
_WS_RE = re.compile(r"\s+")
#: The parts of a line kept as they are: a port, a line:col reference, an exit status are
#: part of what went wrong and the same every time.
_KEEP_RE = re.compile(r":\d{2,5}\b|line \d+|:\d+:\d+")


#: Private-use characters that hold the kept parts while the rest is folded.
_MARK = 0xE000


@dataclass(frozen=True)
class Signature:
    text: str  # the first occurrence, as printed
    events: int
    sessions: int
    #: Other spellings of the line among the events (a different duration or count),
    #: at most ``MAX_VARIANTS``: the first one alone would misstate the others.
    variants: tuple[str, ...] = ()


def _fold(line: str) -> str:
    """The comparable form of a line: what varies between runs replaced by a mark."""
    kept: list[str] = []

    def keep(match: re.Match[str]) -> str:
        kept.append(match.group(0))
        return chr(_MARK + len(kept) - 1)

    text = _KEEP_RE.sub(keep, line)
    text = _TMP_RE.sub("/tmp/…", text)
    text = _HEX_RE.sub("#", text)
    text = _DURATION_RE.sub("<t>", text)
    text = _NUMBER_RE.sub("N", text)
    text = re.sub(r"<t>(?:\s*<t>)+", "<t>", text)
    text = "".join(kept[ord(c) - _MARK] if _MARK <= ord(c) < _MARK + len(kept) else c for c in text)
    return _WS_RE.sub(" ", text).strip().lower()


def error_lines(output: str) -> list[str]:
    """The distinct error lines of one output, in order."""
    lines = output.splitlines()
    if len(lines) > MAX_LINES:
        lines = lines[: MAX_LINES // 2] + lines[-MAX_LINES // 2 :]
    seen: set[str] = set()
    found: list[str] = []
    for raw in lines:
        line = raw.strip()
        if len(line) < 8 or _NOISE_RE.match(line) or not _ERROR_RE.search(line):
            continue
        folded = _fold(line)
        if folded in seen:
            continue
        seen.add(folded)
        found.append(line)
    return found


def signatures(findings: Sequence[Finding], limit: int = MAX_SIGNATURES) -> list[Signature]:
    """The error lines that recur over a hotspot's events, most widespread first.

    A line seen once is not a signature: it says what went wrong that time, not what
    keeps going wrong. With only one event, its first error lines are returned all the
    same (``events`` 1), so a single failure still shows its cause.
    """
    first: dict[str, str] = {}
    events: dict[str, set[int]] = {}
    sessions: dict[str, set[str]] = {}
    order: dict[str, int] = {}
    spellings: dict[str, list[str]] = {}
    with_output = 0
    for finding in findings:
        lines = error_lines(finding.excerpt)
        if lines:
            with_output += 1
        for line in lines:
            key = _fold(line)
            first.setdefault(key, line)
            if line != first[key] and line not in spellings.setdefault(key, []):
                spellings[key].append(line)
            order.setdefault(key, len(order))
            events.setdefault(key, set()).add(finding.fid)
            sessions.setdefault(key, set()).add(finding.unit)
    if not first:
        return []
    keys = list(first)
    if with_output > 1:
        keys = [k for k in keys if len(events[k]) > 1]
    keys.sort(
        key=lambda k: (
            -len(sessions[k]),
            -len(events[k]),
            not _CAUSE_RE.search(first[k]),
            bool(_TEST_NAME_RE.match(first[k])),
            order[k],
        )
    )
    return [
        Signature(
            first[k][:SIGNATURE_CHARS],
            len(events[k]),
            len(sessions[k]),
            tuple(v[:SIGNATURE_CHARS] for v in spellings.get(k, [])[:MAX_VARIANTS]),
        )
        for k in keys[:limit]
    ]
