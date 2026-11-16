# Security policy

agent-friction reads agent transcripts, which routinely contain source code, file paths,
command output, and sometimes secrets. The tool is designed around that:

- It runs fully offline. It makes no network requests, and the HTML report loads nothing
  from the network (it ships a strict Content-Security-Policy).
- Evidence excerpts are redacted (API keys and tokens, `Authorization` headers, private keys,
  common password forms, credentials in URLs and `curl -u`, emails, high-entropy strings, your
  home directory) and truncated. `--no-excerpts` removes all transcript text, leaving paths,
  pseudonymized command names, counts, and timestamps (details below).
- Terminal control sequences and bidirectional-text characters from transcripts are stripped
  from every report, so a hostile transcript can't rewrite your terminal.
- It only reads files; it never modifies transcripts or your repository.

Redaction is best-effort pattern matching. It recognises secrets by their shape, so an opaque
value passed as a bare argument (for example `./deploy hunter2`) can appear in an excerpt.
`--no-excerpts` removes all transcript text; what remains is paths, counts, timestamps, and
normalized command names made only of publicly known words, with project-specific script,
target and tool names replaced by short SHA-256 pseudonyms (confirmable by someone who guesses
the name). File paths are kept in both modes; they are the point of the report. Review a
report before sharing it outside your team.

## Reporting a vulnerability

Please report security issues privately through
[GitHub security advisories](https://github.com/nurettincoban/agent-friction/security/advisories/new)
rather than a public issue. Leaks that redaction misses count as security issues.
You can expect an acknowledgement within a week.
