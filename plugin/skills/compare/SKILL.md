---
name: compare
description: Check whether agent friction in this repository went down since an earlier agent-friction scan, file by file and command by command. Use when the user asks whether the fixes helped, or to compare against the last scan.
argument-hint: "[before.json after.json]"
---

# Did the friction go down?

This plugin carries its own copy of agent-friction. Run it as
`sh "${CLAUDE_PLUGIN_ROOT}/plugin/agent-friction"`, never as a bare `agent-friction`. It needs
Python 3.10 or newer; if the launcher says it found none, tell the user and stop. Do not
install anything, and do not change any file except the scan file below.

## 1. Two scans given

If the user named two saved scans (`$ARGUMENTS`), compare them and go to step 4:

```sh
sh "${CLAUDE_PLUGIN_ROOT}/plugin/agent-friction" compare BEFORE.json AFTER.json --color never
```

## 2. Find the earlier scan

Otherwise use the newest `.agent-friction/scan-*.json` (the names sort by time). If there is
none, tell the user to run `/agent-friction:scan` or `/agent-friction:fix` first, and that a
comparison makes sense once agents have worked here for a while after it (a week or two).
Stop.

## 3. Scan what came after it, and compare

The scan files quote transcript text. If `.agent-friction/` is not ignored by git
(`git check-ignore -q .agent-friction/`), ask the user first whether to add it to
`.gitignore`.

`--since` with a saved scan reads only the events after that scan's last one, with the same
transcript options it used, so no event is counted on both sides. From the repository root,
as one command:

```sh
before=$(ls .agent-friction/scan-*.json 2>/dev/null | sort | tail -n 1) && [ -n "$before" ] && after=.agent-friction/after-$(date -u +%Y-%m-%d-%H%M%S).json && sh "${CLAUDE_PLUGIN_ROOT}/plugin/agent-friction" scan --since "$before" -f json -o "$after" && sh "${CLAUDE_PLUGIN_ROOT}/plugin/agent-friction" compare "$before" "$after" --color never
```

If the scan fails because no session came after the earlier one, tell the user there is
nothing to compare yet, and stop.

## 4. Tell the user

Read the session count on the "after" side first. Fewer than 3 sessions is too early to
tell (the session that made the earlier scan may be the only one): say so, give the date to
try again, and do not report the verdicts. Otherwise, in a few lines, lead with the outcome:

- what got better: `improved`, `resolved`, `replaced` (agents switched to the recommended
  command), `enforced` (a new rule now refuses the call and agents go another way);
- what got worse or is new: `regressed`, `new`;
- `inactive` (nobody touched it afterwards) and `too-little-data` say nothing either way;
  mention them only as that.

If something regressed, offer `/agent-friction:fix`.
