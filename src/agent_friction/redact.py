"""Redaction and truncation of transcript text before it leaves the engine.

Every excerpt, quote and command shown in a report passes through
:func:`redact`. The rules are deliberately biased towards over-redaction: a
mangled excerpt costs little, a leaked credential costs a lot. Redaction is
best-effort pattern matching, not a guarantee; see docs/methodology.md.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from pathlib import Path

DEFAULT_EXCERPT_CHARS = 240
ELLIPSIS = "…"

#: Minimum length of a mixed-case alphanumeric token considered for the entropy rule.
ENTROPY_MIN_LENGTH = 24
#: Minimum Shannon entropy (bits per character) for the entropy rule.
ENTROPY_MIN_BITS = 4.0
#: Minimum length of a pure hex string to be redacted (covers MD5 and longer).
HEX_MIN_LENGTH = 32

_Rule = tuple[str, "re.Pattern[str]", str]

_SECRET_KEY_WORDS = (
    r"pass(?:word|wd|phrase)?|pwd|secret|token|api[_-]?key|apikey|access[_-]?key"
    r"|private[_-]?key|credentials?|auth"
)

_RULES: tuple[_Rule, ...] = (
    (
        "private-key",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)",
            re.S,
        ),
        "[REDACTED:private-key]",
    ),
    ("aws-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "[REDACTED:aws-key]"),
    (
        "api-key",
        re.compile(
            r"\b(?:"
            r"sk-ant-[A-Za-z0-9_-]{8,}"  # Anthropic
            r"|sk-(?:proj-)?[A-Za-z0-9_-]{20,}"  # OpenAI-style
            r"|gh[pousr]_[A-Za-z0-9]{20,}"  # GitHub tokens
            r"|github_pat_[A-Za-z0-9_]{20,}"
            r"|glpat-[A-Za-z0-9_-]{16,}"  # GitLab
            r"|xox[abprs]-[A-Za-z0-9-]{10,}"  # Slack
            r"|AIza[0-9A-Za-z_-]{30,}"  # Google
            r"|[sr]k_(?:live|test)_[A-Za-z0-9]{16,}"  # Stripe
            r"|npm_[A-Za-z0-9]{30,}"
            r"|hf_[A-Za-z0-9]{30,}"
            r")"
        ),
        "[REDACTED:api-key]",
    ),
    ("sendgrid-key", re.compile(r"\bSG\.[\w-]{16,}\.[\w-]{16,}"), "[REDACTED:api-key]"),
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}"),
        "[REDACTED:jwt]",
    ),
    (
        "authorization",
        re.compile(
            r"(?i)\b(authorization[\"']?\s*[:=]\s*[\"']?)((?:bearer|basic|token|digest)\s+)?"
            r"[^\s\"']+"
        ),
        r"\1\2[REDACTED:token]",
    ),
    (
        "bearer",
        re.compile(r"(?i)\b(bearer|basic)\s+(?!\[REDACTED)[A-Za-z0-9._~+/=-]{8,}"),
        r"\1 [REDACTED:token]",
    ),
    (
        "url-credentials",
        re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/:@]+:[^\s/@]+@"),
        r"\1[REDACTED:credentials]@",
    ),
    (
        "user-flag",
        re.compile(r"(?<![\w-])(-u|--user)(\s+|=)[\"']?[^\s:\"']+:[^\s\"']+"),
        r"\1\2[REDACTED:credentials]",
    ),
    (
        "cli-secret",
        re.compile(rf"(?i)(--?(?:{_SECRET_KEY_WORDS})(?:=|\s+))(?!\[REDACTED)([^\s\"']+)"),
        r"\1[REDACTED]",
    ),
    (
        "assignment",
        re.compile(
            rf"(?i)\b([\w-]*(?:{_SECRET_KEY_WORDS})(?:[_-](?:key|id|value|string|b64|base64|salt))?"
            r"[\"']?\s*[:=]\s*[\"']?)"
            r"(?!\[REDACTED)([^\s\"',;&]{4,})"
        ),
        r"\1[REDACTED]",
    ),
    (
        "cookie",
        re.compile(r"(?i)\b((?:set-)?cookie[\"']?\s*[:=]\s*[\"']?)[^\n\"']+"),
        r"\1[REDACTED:cookie]",
    ),
    (
        "connection-string",
        re.compile(
            r"(?i)\b(AccountKey|SharedAccessKey|SharedAccessSignature|sig|signature|key|code"
            r"|access_token|id_token|refresh_token|session|sessionid|sid)=(?!\[REDACTED)"
            r"([^;&\s\"']{6,})"
        ),
        r"\1=[REDACTED]",
    ),
    (
        "db-password-flag",
        re.compile(
            r"(\b(?:mysql\w*|mariadb\w*|sshpass|redis-cli|mongo\w*)\b[^\n|;&]*?\s-[pa])"
            r"(?!assword)(\s*[^\s-]\S*)"
        ),
        r"\1[REDACTED]",
    ),
    (
        "login-password-flag",
        re.compile(r"(\blogin\b[^\n|;&]*?\s(?:-p|--password)(?:\s+|=))(?!\[REDACTED)(\S+)"),
        r"\1[REDACTED]",
    ),
    (
        "prose-secret",
        re.compile(
            r"(?i)\b((?:password|passwd|passphrase|pwd|secret|token|api[ _-]?key|access[ _-]?key)"
            r"\s+(?:is|was)\s+[\"'`]?)(?!\[REDACTED)([^\s\"'`,;]{4,})"
        ),
        r"\1[REDACTED]",
    ),
    (
        "email",
        # the host must end in a name, so ``root@10.0.0.1`` (an ssh target) is not an email
        re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b"),
        "[REDACTED:email]",
    ),
)

_HEX_RE = re.compile(rf"\b[0-9a-fA-F]{{{HEX_MIN_LENGTH},}}\b")
#: Length of a full git commit id. Lower-case hex of exactly this length is left alone:
#: evidence is full of commit ids, and they identify nothing outside the repository.
GIT_SHA_LENGTH = 40
#: A bare AWS-style secret key: 40 characters of base64 with at least one ``/`` or ``+``.
_BASE64_40_RE = re.compile(r"(?<![A-Za-z0-9/+=])[A-Za-z0-9/+]{40}(?![A-Za-z0-9/+=])")
#: Terminal control characters and invisible direction marks. Text from a transcript may
#: contain escape sequences that clear a screen or reorder what a reader sees.
_ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)?|[@-Z\\-_])")
_CONTROL_RE = re.compile(
    "[\x00-\x08\x0b-\x1f\x7f-\x9f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069\ufeff]"
)


def strip_controls(text: str) -> str:
    """Remove ANSI escape sequences, C0/C1 controls (except newline and tab) and bidi marks."""
    if text.isascii() and text.isprintable():
        return text
    return _CONTROL_RE.sub("", _ANSI_RE.sub("", text))


def _hex_sub(match: re.Match[str]) -> str:
    token = match.group(0)
    if len(token) == GIT_SHA_LENGTH and token == token.lower():
        return token
    return "[REDACTED:hex]"


def _base64_sub(match: re.Match[str]) -> str:
    token = match.group(0)
    if (
        ("/" in token or "+" in token)
        and any(c.isdigit() for c in token)
        and any(c.islower() for c in token)
        and any(c.isupper() for c in token)
        and shannon_entropy(token) >= ENTROPY_MIN_BITS
    ):
        return "[REDACTED:high-entropy]"
    return token


_ENTROPY_RE = re.compile(rf"[A-Za-z0-9+_=-]{{{ENTROPY_MIN_LENGTH},}}")
_HOME_RES = (
    re.compile(r"(?<![\w.~-])/(?:home|Users)/[^/\s:\"'`<>|]+"),
    re.compile(r"(?<![\w.~-])/root(?=/|\b)"),
    re.compile(r"\b[A-Za-z]:[\\/]Users[\\/][^\\/\s:\"'`<>|]+"),
)

#: Names of the redaction rules, in application order (documented in methodology.md).
RULE_NAMES: tuple[str, ...] = (
    *(name for name, _, _ in _RULES),
    "hex",
    "high-entropy",
    "home-dir",
)


def shannon_entropy(text: str) -> float:
    """Shannon entropy of ``text`` in bits per character."""
    if not text:
        return 0.0
    total = len(text)
    return -sum((n / total) * math.log2(n / total) for n in Counter(text).values())


def _entropy_sub(match: re.Match[str]) -> str:
    token = match.group(0)
    if (
        any(c.isdigit() for c in token)
        and any(c.islower() for c in token)
        and any(c.isupper() for c in token)
        and shannon_entropy(token) >= ENTROPY_MIN_BITS
    ):
        return "[REDACTED:high-entropy]"
    return token


def redact_home(text: str, home: Path | str | None = None) -> str:
    """Replace home directories (the real one and anything that looks like one) with ``~``."""
    actual = str(home if home is not None else Path.home())
    if len(actual) > 1:
        text = text.replace(actual, "~")
    for pattern in _HOME_RES:
        text = pattern.sub("~", text)
    return text


def redact(text: str, home: Path | str | None = None) -> str:
    """Return ``text`` with secrets, emails and home directories masked."""
    text = strip_controls(text)
    for _name, pattern, replacement in _RULES:
        text = pattern.sub(replacement, text)
    text = _HEX_RE.sub(_hex_sub, text)
    text = _BASE64_40_RE.sub(_base64_sub, text)
    text = _ENTROPY_RE.sub(_entropy_sub, text)
    return redact_home(text, home)


def truncate(text: str, limit: int = DEFAULT_EXCERPT_CHARS) -> str:
    """Cut ``text`` to at most ``limit`` characters, marking the cut with an ellipsis."""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)].rstrip() + ELLIPSIS


_BLANK_LINES_RE = re.compile(r"\n\s*\n+")
_TRAILING_WS_RE = re.compile(r"[ \t]+\n")
_WS_RE = re.compile(r"\s+")


def excerpt(text: str, limit: int = DEFAULT_EXCERPT_CHARS, home: Path | str | None = None) -> str:
    """Redact, tidy whitespace and truncate a multi-line excerpt.

    Redaction runs before truncation so a secret can never be cut in half and
    slip past the patterns. Long inputs keep their head and tail (errors tend
    to be at the end of command output).
    """
    text = text.strip()
    budget = max(limit * 8, 2000)
    if len(text) > budget:
        text = text[: budget // 2] + "\n" + text[-budget // 2 :]
    text = redact(text, home)
    text = _TRAILING_WS_RE.sub("\n", text)
    text = _BLANK_LINES_RE.sub("\n", text).strip()
    if len(text) <= limit:
        return text
    head = max(limit // 3, 1)
    tail = max(limit - head - 3, 1)
    return f"{text[:head].rstrip()} {ELLIPSIS} {text[-tail:].lstrip()}"[: max(limit, 0)]


def one_line(text: str, limit: int = DEFAULT_EXCERPT_CHARS, home: Path | str | None = None) -> str:
    """Redact, collapse all whitespace to single spaces and truncate (for inline quotes)."""
    text = text.strip()
    if len(text) > max(limit * 8, 2000):
        text = text[: max(limit * 8, 2000)]
    return truncate(_WS_RE.sub(" ", redact(text, home)).strip(), limit)
