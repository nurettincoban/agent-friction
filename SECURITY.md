# Security policy

agent-friction reads agent transcripts, which routinely contain source code, file paths,
command output, and sometimes secrets. The tool is designed around that:

- It runs fully offline. It makes no network requests, and the HTML report loads nothing
  from the network (it ships a strict Content-Security-Policy).
- Evidence excerpts are redacted (API keys and tokens, `Authorization` headers, private keys,
  common password forms, credentials in URLs and `curl -u`, emails, high-entropy strings, your
  home directory) and truncated. `--no-excerpts` removes all transcript text, leaving paths,
  normalized command names, counts, and timestamps.
- It only reads files; it never modifies transcripts or your repository.

Redaction is best-effort pattern matching. Review a report before sharing it outside your team.

## Reporting a vulnerability

Please report security issues privately through
[GitHub security advisories](https://github.com/nurettincoban/agent-friction/security/advisories/new)
rather than a public issue. Leaks that redaction misses count as security issues.
You can expect an acknowledgement within a week.
