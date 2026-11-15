from __future__ import annotations

import pytest

from agent_friction import redact

# Secrets are assembled at runtime so no scanner mistakes these fixtures for real ones.
FAKE = "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"


@pytest.mark.parametrize(
    ("text", "leak", "marker"),
    [
        ("key AKIA" + "IOSFODNN7EXAMPLE here", "IOSFODNN7EXAMPLE", "[REDACTED:aws-key]"),
        ("sk-ant-" + "api03-" + FAKE, FAKE, "[REDACTED:api-key]"),
        ("OPENAI sk-" + "proj-" + FAKE, FAKE, "[REDACTED:api-key]"),
        ("ghp_" + FAKE, FAKE, "[REDACTED:api-key]"),
        ("github_pat_" + FAKE, FAKE, "[REDACTED:api-key]"),
        ("xoxb-" + "1234567890-abcdefghij", "1234567890-abcdefghij", "[REDACTED:api-key]"),
        ("sk_live_" + FAKE, FAKE, "[REDACTED:api-key]"),
        (
            "eyJhbGciOiJIUzI1NiJ9." + "eyJzdWIiOiIxMjM0NTYifQ." + "sflKxwRJSMeKKF2QT4",
            "sflKxwRJSMeKKF2QT4",
            "[REDACTED:jwt]",
        ),
        ("Authorization: Bearer abc.def-123456", "abc.def-123456", "[REDACTED"),
        ("curl -H 'Authorization: Basic dXNlcjpwYXNz'", "dXNlcjpwYXNz", "[REDACTED"),
        ("postgres://shop:hunter2@db.internal/shop", "hunter2", "[REDACTED:credentials]@"),
        ("password=hunter2!", "hunter2", "password=[REDACTED]"),
        ('"db_password": "s3cr3t-value"', "s3cr3t-value", "[REDACTED]"),
        ("export API_KEY='zzzz-yyyy-xxxx'", "zzzz-yyyy", "[REDACTED]"),
        ("mysql --password hunter22 -u root", "hunter22", "--password [REDACTED]"),
        ("login --token=tok_123456", "tok_123456", "[REDACTED]"),
        ("mail jane.doe+x@example.co.uk now", "jane.doe", "[REDACTED:email]"),
        ("sha " + "0123456789abcdef" * 2 + " end", "0123456789abcdef", "[REDACTED:hex]"),
        ("blob " + FAKE + " end", FAKE, "[REDACTED:high-entropy]"),
    ],
)
def test_secret_patterns_are_masked(text: str, leak: str, marker: str) -> None:
    out = redact.redact(text)
    assert leak not in out
    assert marker in out


def test_private_key_block_is_masked_even_when_unterminated() -> None:
    block = "-----BEGIN RSA " + "PRIVATE KEY-----\nMIIEvQIBADANBgkq\nhkiG9w0BAQEF\n"
    assert redact.redact("x\n" + block + "-----END RSA PRIVATE KEY-----\ny") == (
        "x\n[REDACTED:private-key]\ny"
    )
    assert "MIIEvQ" not in redact.redact(block)


@pytest.mark.parametrize(
    "text",
    [
        "def test_user_can_login_with_two_factor_enabled(self):",
        "src/agent_friction/adapters/claude_code.py",
        "getUserAccountSettingsHandlerFactory",
        "The author: Jane wrote this; tokens are counted per request",
        "pytest tests/test_checkout.py::test_rounding -q",
    ],
)
def test_ordinary_text_is_left_alone(text: str) -> None:
    assert redact.redact(text, home="/nonexistent-home") == text


def test_home_directories_become_tilde() -> None:
    assert redact.redact_home("/home/alice/proj/a.py", home="/x") == "~/proj/a.py"
    assert redact.redact_home("open /Users/bob/Code/app", home="/x") == "open ~/Code/app"
    assert redact.redact_home(r"C:\Users\carol\repo\a.py", home="/x") == r"~\repo\a.py"
    assert redact.redact_home("/srv/data/custom/x", home="/srv/data") == "~/custom/x"
    assert redact.redact_home("/root/.claude/projects", home="/x") == "~/.claude/projects"
    assert redact.redact_home("see /homework/notes", home="/x") == "see /homework/notes"


def test_truncate_and_one_line() -> None:
    assert redact.truncate("abc", 10) == "abc"
    assert redact.truncate("abcdefghij", 5) == "abcd…"
    assert redact.truncate("abc", 0) == ""
    assert redact.one_line("a\n  b\t c", 50) == "a b c"
    assert len(redact.one_line("word " * 200, 40)) <= 40


def test_excerpt_respects_limit_and_keeps_head_and_tail() -> None:
    text = "HEAD " + "x" * 5000 + " TAIL"
    out = redact.excerpt(text, 240)
    assert len(out) <= 240
    assert out.startswith("HEAD") and out.endswith("TAIL") and "…" in out
    assert redact.excerpt("a\n\n\n  \nb  \nc", 240) == "a\nb\nc"


def test_excerpt_redacts_before_truncating() -> None:
    # The secret straddles the cut: it must not survive as a recognisable fragment.
    secret = "ghp_" + FAKE
    text = "x" * 60 + " token " + secret + " " + "y" * 400
    out = redact.excerpt(text, 100)
    assert secret[:12] not in out


def test_entropy_helper() -> None:
    assert redact.shannon_entropy("") == 0.0
    assert redact.shannon_entropy("aaaa") == 0.0
    assert redact.shannon_entropy("abcd") == pytest.approx(2.0)
