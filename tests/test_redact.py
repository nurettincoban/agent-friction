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
        (
            "git clone HTTPS://bob:s3cretpw@host.example/r.git",
            "s3cretpw",
            "[REDACTED:credentials]@",
        ),
        ("the password is hunter2", "hunter2", "password is [REDACTED]"),
        ("staging passphrase was correct-horse", "correct-horse", "[REDACTED]"),
        ("curl -u bob:s3cretpw https://x.example", "s3cretpw", "-u [REDACTED:credentials]"),
        ("curl --user=bob:s3cretpw https://x.example", "s3cretpw", "[REDACTED:credentials]"),
        ("-H 'Authorization: Basic abc'", "abc", "Authorization: Basic [REDACTED:token]"),
        ("Authorization: Token abc123", "abc123", "[REDACTED:token]"),
        ('{"authorization": "raw-secret-value"}', "raw-secret-value", "[REDACTED:token]"),
        ("password=hunter2!", "hunter2", "password=[REDACTED]"),
        ('"db_password": "s3cr3t-value"', "s3cr3t-value", "[REDACTED]"),
        ("export API_KEY='zzzz-yyyy-xxxx'", "zzzz-yyyy", "[REDACTED]"),
        ("mysql --password hunter22 -u root", "hunter22", "--password [REDACTED]"),
        ("login --token=tok_123456", "tok_123456", "[REDACTED]"),
        ("mail jane.doe+x@example.co.uk now", "jane.doe", "[REDACTED:email]"),
        ("sha " + "0123456789abcdef" * 2 + " end", "0123456789abcdef", "[REDACTED:hex]"),
        ("blob " + FAKE + " end", FAKE, "[REDACTED:high-entropy]"),
        ("mysql -uroot -phunter2 shop", "hunter2", "-p[REDACTED]"),
        ("sshpass -p hunter2 ssh host", "hunter2", "[REDACTED]"),
        ("curl -H 'Cookie: sessionid=abc123def; csrf=xyz987'", "abc123def", "[REDACTED:cookie]"),
        ("Cookie: a=1; csrf=xyz987", "xyz987", "Cookie: [REDACTED:cookie]"),
        ("< Set-Cookie: sid=s3cr3tvalue; Path=/; HttpOnly", "s3cr3tvalue", "[REDACTED:cookie]"),
        (
            "DefaultEndpointsProtocol=https;AccountName=acct;AccountKey=abcd1234efgh==;Endpoint=x",
            "abcd1234efgh",
            "AccountKey=[REDACTED]",
        ),
        ("Endpoint=sb://x.test/;SharedAccessKey=abcd1234efgh=", "abcd1234efgh", "[REDACTED]"),
        (
            "SG." + "abcdefghijklmnopqrstuv." + FAKE + "abcdefg",
            "abcdefghijklmnopqrstuv",
            "api-key]",
        ),
        ("https://api.x.test/cb?token=abc123tok&x=1", "abc123tok", "token=[REDACTED]&x=1"),
        ("https://x.test/a?b=2&secret=shh-123", "shh-123", "secret=[REDACTED]"),
        ("https://x.blob.test/c/f?sv=2020&sig=abcDEF123%3D", "abcDEF123", "sig=[REDACTED]"),
        ("curl 'https://x.test?api_key=k123456'", "k123456", "api_key=[REDACTED]"),
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
        "git log -p",
        "ls -p src && mkdir -p out/dir",
        "ssh -p 2222 host",
        "GET /items?page=2&sort=name&key_order=asc",
        "sorted(items, key=name)",
        "the cookie banner is too tall",
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


def test_more_secret_shapes() -> None:
    aws_secret = "wJalrXUtnFEMI/K7MDENG/" + "bPxRfiCYEXAMPLEKEY"
    assert aws_secret not in redact.redact(f"aws_secret is {aws_secret} ok")
    assert redact.redact("docker login -u bob -p S3cr3tPass registry.example") == (
        "docker login -u bob -p [REDACTED] registry.example"
    )
    django = "SECRET_KEY = 'django-insecure-" + "x9!k2@pq7$' # settings"
    assert "django-insecure" not in redact.redact(django)
    assert redact.redact("The author: Jane") == "The author: Jane"


def test_commit_ids_and_ip_hosts_are_kept() -> None:
    sha = "3f2a9c1b7d4e5f60718293a4b5c6d7e8f9012345"
    assert len(sha) == redact.GIT_SHA_LENGTH
    assert redact.redact(f"git revert {sha}") == f"git revert {sha}"
    assert sha + "ab" not in redact.redact(f"token {sha}ab")  # longer hex is still masked
    assert redact.redact("ssh root@8.8.4.4 uptime") == "ssh root@8.8.4.4 uptime"
    assert "jane@example.com" not in redact.redact("mail jane@example.com")


def test_control_and_bidi_characters_never_survive() -> None:
    raw = "a\x1b[2Jb\x1b]0;title\x07c‮d\te\nf\x00\x9b⁦g"
    assert redact.strip_controls(raw) == "abcd\te\nfg"
    assert redact.excerpt(raw, 240) == "abcd\te\nfg"
    assert redact.one_line(raw, 240) == "abcd e fg"
    assert redact.strip_controls("plain text") == "plain text"


def test_private_addresses_and_internal_hosts_are_masked() -> None:
    text = redact.redact(
        'connection to "db.internal" (10.2.3.4) failed; tried 192.168.1.20, 172.16.0.9, '
        "http://nas.local:8080/x and build01.corp"
    )
    for secret in ("db.internal", "10.2.3.4", "192.168.1.20", "172.16.0.9", "nas.local", "build01"):
        assert secret not in text
    assert text.count("[REDACTED:private-ip]") == 3
    assert text.count("[REDACTED:internal-host]") == 3
    kept = (
        "8.8.8.8 172.32.0.1 110.2.3.4 version 10.2.3 .claude/settings.local.json "
        "threading.local() self.internal example.com"
    )
    assert redact.redact(kept) == kept


@pytest.mark.parametrize(
    "text",
    [
        "redis://:hunter2pass@cache:6379/0",
        "echo hunter2pass | docker login --password-stdin -u bob registry.example.com",
        "log in with password hunter2pass and retry",
        "using password hunter2pass",
        "pw hunter2pass",
        "the pw is hunter2pass",
        "password: hunter2pass",
    ],
)
def test_more_shapes_of_a_password(text: str) -> None:
    assert "hunter2pass" not in redact.redact(text)


@pytest.mark.parametrize(
    "text",
    [
        "with password authentication enabled",
        "use the password field of the form",
        "http://example.com:8080/path",
        "postgres://db.example.com:5432/shop",
        "echo done | tail -1",
    ],
)
def test_password_rules_leave_ordinary_text_alone(text: str) -> None:
    assert redact.redact(text) == text


@pytest.mark.parametrize(
    "text",
    [
        "machine api.example.com login bob password hunter2pass",
        "machine h\n  login bob\n  password hunter2pass\n",
        "curl -X POST https://hooks.slack.com/services/T000/B000/hunter2pass -d '{}'",
        "https://discord.com/api/webhooks/1234567890/hunter2pass",
        "openssl enc -aes-256-cbc -k hunter2pass -in data",
        "openssl rsa -passin pass:hunter2pass -in key.pem",
    ],
)
def test_netrc_webhooks_and_openssl_passwords(text: str) -> None:
    assert "hunter2pass" not in redact.redact(text)


@pytest.mark.parametrize(
    "text",
    [
        "the login page password field is required",
        "openssl x509 -in cert.pem -noout",
        "see https://api.slack.com/messaging/webhooks",
    ],
)
def test_those_rules_leave_ordinary_text_alone(text: str) -> None:
    assert redact.redact(text) == text


@pytest.mark.parametrize(
    ("text", "secret", "expected"),
    [
        ("X-Api-Key: Tr0ub4dor&3", "&3", "X-Api-Key: [REDACTED]"),
        ("curl -H 'X-Api-Key: Tr0ub4dor&3' u", "&3", "curl -H 'X-Api-Key: [REDACTED]' u"),
        ('curl -H "X-Auth-Token: a b&c" u', "b&c", 'curl -H "X-Auth-Token: [REDACTED]" u'),
        ("Api-Key: zz&y\nnext line", "zz&y", "Api-Key: [REDACTED]\nnext line"),
        ("PASSWORD='ab&cd ef' ./run", "cd ef", "PASSWORD='[REDACTED]' ./run"),
        ('{"password": "ab cd&e", "user": "bob"}', "cd&e", '"[REDACTED]", "user": "bob"}'),
        ("token: ab&cd rest", "&cd", "token: [REDACTED] rest"),
    ],
)
def test_a_secret_value_is_redacted_whole(text: str, secret: str, expected: str) -> None:
    out = redact.redact(text)
    assert secret not in out and expected in out


def test_an_ampersand_still_ends_a_query_parameter() -> None:
    assert redact.redact("https://x.test/?token=abcd1234&user=bob").endswith("[REDACTED]&user=bob")
    assert redact.redact("auth-flow: we need to rework it") == "auth-flow: we need to rework it"


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("SECRET_KEY_BASE=plainwordsecret bin/rails s", "plainwordsecret"),
        ("export SIGNING_KEY=hunter2", "hunter2"),
        ("SIGNING_KEY='hunter two' make sign", "hunter two"),
        ("MY_TOKEN_V2=abcd1234 ./deploy", "abcd1234"),
        ("DB_PASSWD_RO=plainword psql", "plainword"),
        ("vault login s.AbCdEf123456", "s.AbCdEf123456"),
        ("htpasswd -b /etc/nginx/.htpasswd bob hunter2", "hunter2"),
        ("htpasswd -cb users bob hunter2", "hunter2"),
        ("net use Z: \\\\files\\share hunter2 /user:bob", "hunter2"),
        ("net use \\\\files\\share /user:corp\\bob hunter2", "hunter2"),
    ],
)
def test_env_style_names_and_login_commands(text: str, secret: str) -> None:
    masked = redact.redact(text)
    assert secret not in masked and "[REDACTED" in masked, masked


@pytest.mark.parametrize(
    "text",
    ["PATH=/usr/bin make", "KEYS=1", "NODE_ENV=production npm test", "vault login -method=oidc"],
)
def test_env_rules_leave_ordinary_assignments_alone(text: str) -> None:
    assert redact.redact(text) == text


def test_windows_home_with_forward_slashes_leaves_no_drive() -> None:
    assert redact.redact_home("C:/Users/runneradmin/work/x", home="/nowhere") == "~/work/x"
