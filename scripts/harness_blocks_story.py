"""Write the synthetic "harness blocks" sessions used in docs/comparison.md.

Twenty Claude Code sessions in which an agent watches a build. In each one it tries
`sleep N; gh run view` 14 times and Claude Code's own rule refuses it ("Blocked:
sleep 30 followed by: ..."). In two sessions a human also declines a `sleep 60`,
and in three a human stops an `ls`. The repository has nothing to do with any of it.

    uv run python scripts/harness_blocks_story.py OUT_DIR
    agent-friction scan REPO --transcripts OUT_DIR --repo-alias /repo --no-git
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.builders import T0, Session, session_uuid

BLOCKED = (
    "Blocked: sleep {n} followed by: gh run view. To wait for a condition, use Monitor with "
    "an until-loop. To wait for a command you started, use run_in_background: true."
)


def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for i in range(20):
        s = Session(session_uuid(f"s{i}"), "/repo", T0 + timedelta(days=i))
        s.user("watch the build")
        for k in range(14):
            command = f"sleep {30 + k}; gh run view"
            s.tool("Bash", {"command": command}, BLOCKED.format(n=30 + k), is_error=True)
            s.bash("gh run view", "in progress")
        if i < 2:
            s.reject("Bash", {"command": "sleep 60"})
        if i < 3:
            s.bash("ls src", "a.py").interrupt()
        s.save(out)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
