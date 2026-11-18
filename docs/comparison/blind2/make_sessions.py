#!/usr/bin/env python3
"""Write deterministic Claude Code session transcripts for the ferrydesk repo.

stdlib only. Output: sessions/<session-uuid>.jsonl (plus
sessions/<session-uuid>/subagents/agent-<id>.jsonl for sessions that use the
Task tool).
"""
import datetime as dt
import hashlib
import json
import os
import shutil
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.join(HERE, "repo")
OUT = os.path.join(HERE, "sessions")
CWD = "/work/ferrydesk"
VERSION = "2.0.31"
MODEL = "claude-sonnet-4-5-20250929"
NS = uuid.UUID("6f1c2b0e-2a6c-4f4e-9d1e-7f3b0c9a5d21")

REJECT = ("The user doesn't want to proceed with this tool use. The tool use was rejected "
          "(eg. if it was a file edit, the new_string was NOT written to the file). "
          "STOP what you are doing and wait for the user to tell you how to proceed.")
REJECT_WITH_REASON = ("The user doesn't want to proceed with this tool use. The tool use was rejected "
                      "(eg. if it was a file edit, the new_string was NOT written to the file). "
                      "To tell you how to proceed, the user said:\n")
INTERRUPT_TOOL = "[Request interrupted by user for tool use]"
INTERRUPT = "[Request interrupted by user]"


def P(rel):
    return CWD + "/" + rel


def h(*parts, n=22):
    return hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:n]


def b62(*parts, n=24):
    alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    v = int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest(), 16)
    s = ""
    while len(s) < n:
        v, r = divmod(v, 62)
        s += alphabet[r]
    return s


def load_repo():
    files = {}
    for root, _, names in os.walk(REPO):
        for nm in names:
            full = os.path.join(root, nm)
            rel = os.path.relpath(full, REPO)
            with open(full, encoding="utf-8") as f:
                files[P(rel)] = f.read()
    return files


def cat_n(text, offset=1, limit=None):
    lines = text.split("\n")
    if text.endswith("\n"):
        lines = lines[:-1]
    start = offset - 1
    end = len(lines) if limit is None else min(len(lines), start + limit)
    return "\n".join(f"{i + 1:>6}→{lines[i]}" for i in range(start, end)), lines[start:end], len(lines)


class Session:
    def __init__(self, key, start, branch, extra_files=None):
        self.key = key
        self.sid = str(uuid.uuid5(NS, "session-" + key))
        self.t = dt.datetime.fromisoformat(start)
        self.branch = branch
        self.lines = []
        self.parent = None
        self.n = 0
        self.msg_id = None
        self.fs = load_repo()
        for k, v in (extra_files or {}).items():
            self.fs[P(k)] = v
        self.subagents = {}

    # ---- plumbing -------------------------------------------------------
    def _uuid(self):
        self.n += 1
        return str(uuid.uuid5(NS, f"{self.key}-{self.n}"))

    def _ts(self, secs):
        self.t += dt.timedelta(seconds=secs)
        return self.t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{self.t.microsecond // 1000:03d}Z"

    def _base(self, typ, secs):
        u = self._uuid()
        d = {
            "parentUuid": self.parent,
            "isSidechain": False,
            "userType": "external",
            "cwd": CWD,
            "sessionId": self.sid,
            "version": VERSION,
            "gitBranch": self.branch,
            "type": typ,
            "uuid": u,
            "timestamp": self._ts(secs),
        }
        return d

    def _push(self, d):
        self.lines.append(d)
        self.parent = d["uuid"]
        return d

    def _assistant(self, block, stop=None, secs=3.0):
        if self.msg_id is None:
            self.msg_id = "msg_01" + b62(self.key, self.n, "msg")
            self.req_id = "req_011C" + b62(self.key, self.n, "req", n=20)
        d = self._base("assistant", secs)
        d["message"] = {
            "model": MODEL,
            "id": self.msg_id,
            "type": "message",
            "role": "assistant",
            "content": [block],
            "stop_reason": stop,
            "stop_sequence": None,
            "usage": {
                "input_tokens": 4 + self.n % 7,
                "cache_creation_input_tokens": 300 + (self.n * 137) % 2400,
                "cache_read_input_tokens": 14000 + self.n * 911,
                "output_tokens": 40 + (self.n * 53) % 600,
                "service_tier": "standard",
            },
        }
        d["requestId"] = self.req_id
        return self._push(d)

    # ---- public events --------------------------------------------------
    def human(self, text, secs=40.0):
        self.msg_id = None
        self.lines.append({
            "type": "file-history-snapshot",
            "messageId": str(uuid.uuid5(NS, f"{self.key}-snap-{self.n}")),
            "snapshot": {"messageId": str(uuid.uuid5(NS, f"{self.key}-snap-{self.n}")),
                         "trackedFileBackups": {}, "timestamp": self._ts(0.0)},
            "isSnapshotUpdate": False,
        })
        d = self._base("user", secs)
        d["message"] = {"role": "user", "content": text}
        d["permissionMode"] = "default"
        return self._push(d)

    def interrupt_marker(self, tool=False):
        self.msg_id = None
        d = self._base("user", 1.0)
        d["message"] = {"role": "user", "content": [{"type": "text", "text": INTERRUPT_TOOL if tool else INTERRUPT}]}
        return self._push(d)

    def say(self, text, final=False, secs=4.0):
        self._assistant({"type": "text", "text": text}, stop="end_turn" if final else None, secs=secs)
        if final:
            self.msg_id = None

    def think(self, text, secs=3.0):
        self._assistant({"type": "thinking", "thinking": text, "signature": "Eu" + b62(self.key, self.n, "sig", n=60)}, secs=secs)

    def _tool(self, name, inp, content, tur, is_error=False, secs=2.0, run_secs=1.5):
        tid = "toolu_01" + b62(self.key, self.n, "tool", n=22)
        a = self._assistant({"type": "tool_use", "id": tid, "name": name, "input": inp}, stop="tool_use", secs=secs)
        self.msg_id = None
        d = self._base("user", run_secs)
        block = {"tool_use_id": tid, "type": "tool_result", "content": content}
        if is_error or name == "Bash":
            block["is_error"] = is_error
        d["message"] = {"role": "user", "content": [block]}
        d["toolUseResult"] = tur
        d["sourceToolAssistantUUID"] = a["uuid"]
        return self._push(d)

    # ---- tools ----------------------------------------------------------
    def read(self, rel, offset=None, limit=None):
        path = P(rel)
        text = self.fs[path]
        body, sel, total = cat_n(text, offset or 1, limit)
        inp = {"file_path": path}
        if offset:
            inp["offset"] = offset
        if limit:
            inp["limit"] = limit
        tur = {"type": "text", "file": {"filePath": path, "content": "\n".join(sel),
                                        "numLines": len(sel), "startLine": offset or 1, "totalLines": total}}
        return self._tool("Read", inp, body, tur)

    def line_of(self, rel, needle):
        for i, ln in enumerate(self.fs[P(rel)].split("\n"), 1):
            if needle in ln:
                return i
        raise KeyError(needle)

    def edit(self, rel, old, new, replace_all=False, expect=None):
        path = P(rel)
        text = self.fs[path]
        cnt = text.count(old)
        inp = {"file_path": path, "old_string": old, "new_string": new}
        if replace_all:
            inp["replace_all"] = True
        if cnt == 0:
            outcome = "missing"
        elif cnt > 1 and not replace_all:
            outcome = "multi"
        else:
            outcome = "ok"
        if expect and expect != outcome:
            raise AssertionError(f"{self.key} edit {rel}: expected {expect}, got {outcome} (count={cnt}) for {old!r}")
        if outcome == "missing":
            msg = f"String to replace not found in file.\nString: {old}"
            return self._tool("Edit", inp, f"<tool_use_error>{msg}</tool_use_error>", "Error: " + msg, is_error=True)
        if outcome == "multi":
            msg = (f"Found {cnt} matches of the string to replace, but replace_all is false. To replace all "
                   f"occurrences, set replace_all to true. To replace only one occurrence, please provide more "
                   f"context to uniquely identify the instance.\nString: {old}")
            return self._tool("Edit", inp, f"<tool_use_error>{msg}</tool_use_error>", "Error: " + msg, is_error=True)
        start_line = text[: text.index(old)].count("\n") + 1
        newtext = text.replace(old, new) if replace_all else text.replace(old, new, 1)
        self.fs[path] = newtext
        old_l, new_l = old.split("\n"), new.split("\n")
        patch = [{"oldStart": start_line, "oldLines": len(old_l), "newStart": start_line, "newLines": len(new_l),
                  "lines": ["-" + x for x in old_l] + ["+" + x for x in new_l]}]
        _, snippet, _ = cat_n(newtext, max(1, start_line - 2), len(new_l) + 4)
        body, _, _ = cat_n(newtext, max(1, start_line - 2), len(new_l) + 4)
        content = (f"The file {path} has been updated. Here's the result of running `cat -n` on a snippet of the "
                   f"edited file:\n{body}")
        tur = {"filePath": path, "oldString": old, "newString": new, "originalFile": text,
               "structuredPatch": patch, "userModified": False, "replaceAll": replace_all}
        return self._tool("Edit", inp, content, tur)

    def write(self, rel, content):
        path = P(rel)
        existed = path in self.fs
        orig = self.fs.get(path)
        self.fs[path] = content
        if existed:
            msg = f"The file {path} has been updated. Here's the result of running `cat -n` on a snippet of the edited file:\n" + cat_n(content)[0]
            tur = {"type": "update", "filePath": path, "content": content,
                   "structuredPatch": [{"oldStart": 1, "oldLines": orig.count("\n"), "newStart": 1,
                                        "newLines": content.count("\n"),
                                        "lines": ["-" + x for x in orig.rstrip("\n").split("\n")] + ["+" + x for x in content.rstrip("\n").split("\n")]}],
                   "originalFile": orig}
        else:
            msg = f"File created successfully at: {path}"
            tur = {"type": "create", "filePath": path, "content": content, "structuredPatch": [], "originalFile": None}
        return self._tool("Write", {"file_path": path, "content": content}, msg, tur)

    def grep(self, pattern, files=None, content_out=None, path=None, glob=None, mode="files_with_matches"):
        inp = {"pattern": pattern}
        if path:
            inp["path"] = path
        if glob:
            inp["glob"] = glob
        if mode != "files_with_matches":
            inp["output_mode"] = mode
            if mode == "content":
                inp["-n"] = True
        if mode == "content":
            text = content_out or "No matches found"
            tur = {"mode": "content", "numFiles": 0, "filenames": [], "content": content_out or "",
                   "numLines": 0 if not content_out else content_out.count("\n") + 1}
            return self._tool("Grep", inp, text, tur)
        files = [P(f) for f in (files or [])]
        text = f"Found {len(files)} file{'s' if len(files) != 1 else ''}\n" + "\n".join(files) if files else "No files found"
        tur = {"mode": "files_with_matches", "filenames": files, "numFiles": len(files)}
        return self._tool("Grep", inp, text, tur)

    def glob(self, pattern, files):
        files = [P(f) for f in files]
        tur = {"filenames": files, "durationMs": 9, "numFiles": len(files), "truncated": False}
        return self._tool("Glob", {"pattern": pattern}, "\n".join(files) if files else "No files found", tur)

    def bash(self, cmd, out="", code=0, desc=None, stderr="", run_secs=2.5, timeout=None, background=False):
        inp = {"command": cmd}
        if desc:
            inp["description"] = desc
        if timeout:
            inp["timeout"] = timeout
        if background:
            inp["run_in_background"] = True
            bid = "bash_" + str(1 + sum(1 for l in self.lines if l.get("type") == "user" and isinstance(l.get("toolUseResult"), dict) and "backgroundTaskId" in l["toolUseResult"]))
            tur = {"stdout": "", "stderr": "", "interrupted": False, "isImage": False, "backgroundTaskId": bid}
            return self._tool("Bash", inp, f"Command running in background with ID: {bid}", tur, run_secs=0.4)
        if code == 0:
            tur = {"stdout": out, "stderr": stderr, "interrupted": False, "isImage": False}
            return self._tool("Bash", inp, out if out else "(No content)", tur, run_secs=run_secs)
        body = f"Exit code {code}\n{out}".rstrip("\n")
        return self._tool("Bash", inp, body, "Error: " + body, is_error=True, run_secs=run_secs)

    def bash_timeout(self, cmd, partial, timeout_ms=120000, desc=None):
        inp = {"command": cmd}
        if desc:
            inp["description"] = desc
        if timeout_ms != 120000:
            inp["timeout"] = timeout_ms
        secs = timeout_ms / 1000
        human_t = f"{int(secs // 60)}m {secs % 60:.1f}s" if secs >= 60 else f"{secs:.1f}s"
        body = f"Command timed out after {human_t}\n{partial}".rstrip("\n")
        return self._tool("Bash", inp, body, "Error: " + body, is_error=True, run_secs=secs)

    def reject(self, name, inp, reason=None):
        if reason is None:
            self._tool(name, inp, REJECT, "User rejected tool use", is_error=True, run_secs=6.0)
            self.interrupt_marker(tool=True)
        else:
            self._tool(name, inp, REJECT_WITH_REASON + reason, "User rejected tool use", is_error=True, run_secs=14.0)

    def task(self, description, prompt, subagent_type, report, build_sub):
        agent_id = "a" + h(self.key, description, n=16)
        sub = SubAgent(self, agent_id, prompt)
        build_sub(sub)
        sub.say(report, final=True)
        self.subagents[agent_id] = sub.lines
        tur = {"status": "completed", "prompt": prompt, "agentId": agent_id,
               "content": [{"type": "text", "text": report}],
               "totalDurationMs": int(sub.elapsed * 1000), "totalTokens": 18000 + len(report) * 3,
               "totalToolUseCount": sub.tool_count,
               "usage": {"input_tokens": 12, "cache_creation_input_tokens": 4100,
                         "cache_read_input_tokens": 16800, "output_tokens": 640, "service_tier": "standard"}}
        return self._tool("Task", {"description": description, "prompt": prompt, "subagent_type": subagent_type},
                          [{"type": "text", "text": report}], tur, run_secs=sub.elapsed)

    # ---- output ---------------------------------------------------------
    def dump(self):
        path = os.path.join(OUT, self.sid + ".jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for d in self.lines:
                f.write(json.dumps(d, ensure_ascii=False) + "\n")
        for aid, lines in self.subagents.items():
            d = os.path.join(OUT, self.sid, "subagents")
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, f"agent-{aid}.jsonl"), "w", encoding="utf-8") as f:
                for x in lines:
                    f.write(json.dumps(x, ensure_ascii=False) + "\n")


class SubAgent(Session):
    """Sidechain transcript for a Task subagent; shares the parent's clock and fs."""

    def __init__(self, parent, agent_id, prompt):
        self.key = parent.key + "-sub-" + agent_id
        self.sid = parent.sid
        self.t = parent.t + dt.timedelta(seconds=2.5)
        self.t0 = self.t
        self.branch = parent.branch
        self.lines = []
        self.parent = None
        self.n = 0
        self.msg_id = None
        self.fs = parent.fs
        self.subagents = {}
        self.agent_id = agent_id
        self.tool_count = 0
        d = self._base("user", 0.5)
        d["message"] = {"role": "user", "content": prompt}
        self._push(d)

    def _base(self, typ, secs):
        d = Session._base(self, typ, secs)
        d["isSidechain"] = True
        d["agentId"] = self.agent_id
        return d

    def _tool(self, *a, **k):
        self.tool_count += 1
        return Session._tool(self, *a, **k)

    @property
    def elapsed(self):
        return (self.t - self.t0).total_seconds()


# ---------------------------------------------------------------------------
# canned tool output
# ---------------------------------------------------------------------------
MOD = "github.com/tidewater-labs/ferrydesk"


def gotest_all(api="0.018s", booking="0.011s", extra_ok=()):
    lines = [f"?   \t{MOD}/cmd/ferrydesk\t[no test files]",
             f"ok  \t{MOD}/internal/api\t{api}",
             f"ok  \t{MOD}/internal/booking\t{booking}",
             f"ok  \t{MOD}/internal/config\t0.004s",
             f"?   \t{MOD}/internal/db\t[no test files]",
             f"ok  \t{MOD}/internal/pricing\t0.006s"]
    lines += list(extra_ok)
    lines += [
        "--- FAIL: TestCreateAndGetBooking (0.00s)",
        "    postgres_test.go:22: open store: failed to connect to `user=ferry database=ferrydesk`: 127.0.0.1:5432 (localhost): dial error: dial tcp 127.0.0.1:5432: connect: connection refused",
        "--- FAIL: TestActiveHolds (0.00s)",
        "    postgres_test.go:22: open store: failed to connect to `user=ferry database=ferrydesk`: 127.0.0.1:5432 (localhost): dial error: dial tcp 127.0.0.1:5432: connect: connection refused",
        "FAIL",
        f"FAIL\t{MOD}/internal/store\t0.019s",
        "FAIL",
    ]
    return "\n".join(lines)


LINT_FAIL = ("bin/golangci-lint run ./...\nmake: bin/golangci-lint: No such file or directory\n"
             "make: *** [Makefile:14: lint] Error 127")


def server_log(sess, addr=":8080", level="INFO"):
    t = sess.t + dt.timedelta(hours=13, seconds=4.112)
    return f'time={t.strftime("%Y-%m-%dT%H:%M:%S")}.{t.microsecond // 1000:03d}+13:00 level={level} msg=listening addr={addr}'


FARE_CHILDREN = '''package pricing

// Route identifies a crossing and carries its base adult fare.
type Route struct {
	Code      string
	AdultFare Cents
}

// Passengers counts travellers on one booking. Children are under 12.
type Passengers struct {
	Adults   int
	Children int
}

// childFareBps is the share of the adult fare a child pays, in basis points.
const childFareBps = 5000

// Fare returns the total fare for a booking on the given route.
// Each child pays half the adult fare, rounded half up per child.
func Fare(r Route, p Passengers) Cents {
	adult := r.AdultFare * Cents(p.Adults)
	child := ApplyBasisPoints(r.AdultFare, childFareBps) * Cents(p.Children)
	return adult + child
}

// Discount is applied to a fare total, in basis points.
type Discount struct {
	Code string
	Bps  int64
}

// ApplyDiscounts subtracts each discount in order.
func ApplyDiscounts(total Cents, ds []Discount) Cents {
	for _, d := range ds {
		total -= ApplyBasisPoints(total, d.Bps)
	}
	return total
}
'''

REFUND_REASON_UP = "ALTER TABLE refunds ADD COLUMN reason text;\n"
REFUND_REASON_DOWN = "ALTER TABLE refunds DROP COLUMN reason;\n"

ERR500 = '\t\twriteJSON(w, http.StatusInternalServerError, map[string]string{"error": "internal error"})\n\t\treturn\n\t}\n'


# ---------------------------------------------------------------------------
# sessions
# ---------------------------------------------------------------------------
def s01():
    s = Session("s01", "2026-10-05T09:12:04.000", "feat/remaining-seats")
    s.human("can you add remaining_seats to the GET /sailings/{id} response? capacity minus confirmed bookings minus live holds. mobile wants to show \"only 4 left\"")
    s.say("I'll look at how a sailing is served and where seat counts are computed.")
    s.grep("getSailing|SeatsTaken|heldSeats", files=["internal/api/handlers.go", "internal/api/router.go", "internal/booking/service.go",
                                                     "internal/store/postgres.go", "internal/db/queries.sql.go"])
    s.read("internal/booking/service.go")
    ln = s.line_of("internal/api/handlers.go", "func (h *Handlers) getSailing")
    s.read("internal/api/handlers.go", offset=ln - 50, limit=75)
    s.say("The service already has `heldSeats`. I'll add a `RemainingSeats` method next to it and expose it on the sailing response.")
    s.edit("internal/booking/service.go",
           "// heldSeats counts seats in holds that have not expired.",
           "// RemainingSeats is capacity minus confirmed seats minus seats in live holds.\n"
           "func (s *Service) RemainingSeats(ctx context.Context, sl Sailing) (int, error) {\n"
           "\ttaken, err := s.store.SeatsTaken(ctx, sl.ID)\n\tif err != nil {\n\t\treturn 0, err\n\t}\n"
           "\theld, err := s.heldSeats(ctx, sl.ID)\n\tif err != nil {\n\t\treturn 0, err\n\t}\n"
           "\tif n := sl.Capacity - taken - held; n > 0 {\n\t\treturn n, nil\n\t}\n\treturn 0, nil\n}\n\n"
           "// heldSeats counts seats in holds that have not expired.", expect="ok")
    s.edit("internal/api/handlers.go",
           "\tAdultFareCents int64     `json:\"adult_fare_cents\"`\n}\n\ntype bookingResponse",
           "\tAdultFareCents int64     `json:\"adult_fare_cents\"`\n\tRemainingSeats *int      `json:\"remaining_seats,omitempty\"`\n}\n\ntype bookingResponse", expect="ok")
    s.edit("internal/api/handlers.go",
           "\tif err != nil {\n" + ERR500,
           "\tif err != nil {\n" + ERR500 + "\tremaining, err := h.svc.RemainingSeats(r.Context(), sl)\n" + "\tif err != nil {\n" + ERR500,
           expect="multi")
    s.say("That block is repeated all over handlers.go. I'll anchor on the `get sailing` log line instead.")
    s.edit("internal/api/handlers.go",
           '\t\th.log.Error("get sailing", "err", err)\n' + ERR500,
           '\t\th.log.Error("get sailing", "err", err)\n' + ERR500 +
           '\tremaining, err := h.svc.RemainingSeats(r.Context(), sl)\n\tif err != nil {\n\t\th.log.Error("remaining seats", "err", err)\n' + ERR500, expect="ok")
    s.edit("internal/api/handlers.go",
           "\t\tAdultFareCents: int64(sl.AdultFare),\n\t})\n}",
           "\t\tAdultFareCents: int64(sl.AdultFare),\n\t\tRemainingSeats: &remaining,\n\t})\n}", expect="ok")
    s.bash("go build ./...", desc="Build all packages")
    s.bash("go test ./...", gotest_all(), code=1, desc="Run test suite", run_secs=9.0)
    s.say("Everything I touched passes. The only failures are in `internal/store`, which can't connect to Postgres on localhost:5432, so they're unrelated. I'll add a service test for the new method and run the unit packages.")
    s.edit("internal/booking/service_test.go",
           "func TestCancelRefundsTotal(t *testing.T) {",
           "func TestRemainingSeats(t *testing.T) {\n"
           "\tnow := time.Date(2026, 10, 5, 9, 0, 0, 0, time.UTC)\n"
           "\tfs := &fakeStore{sailing: Sailing{ID: 1, Capacity: 10}, taken: 4, holds: []Hold{{Seats: 2, CreatedAt: now, ExpiresAt: now.Add(time.Hour)}}}\n"
           "\tsvc := NewService(fs, 15*time.Minute)\n\tsvc.now = func() time.Time { return now }\n"
           "\tgot, err := svc.RemainingSeats(context.Background(), fs.sailing)\n"
           "\tif err != nil || got != 4 {\n\t\tt.Fatalf(\"got %d, %v\", got, err)\n\t}\n}\n\n"
           "func TestCancelRefundsTotal(t *testing.T) {", expect="ok")
    s.bash("go test ./internal/booking/... ./internal/api/...",
           f"ok  \t{MOD}/internal/booking\t0.010s\nok  \t{MOD}/internal/api\t0.017s", desc="Run unit tests for touched packages")
    s.bash("make lint", LINT_FAIL, code=2, desc="Run linter")
    s.bash("go vet ./...", desc="Run go vet")
    s.say("Done.\n\n- `booking.Service.RemainingSeats` computes capacity minus confirmed seats minus seats in non-expired holds (floored at 0).\n"
          "- `GET /sailings/{id}` now includes `remaining_seats`.\n- Added `TestRemainingSeats`.\n\n"
          "`go build` and `go vet` are clean; booking/api/pricing/config tests pass. The `internal/store` tests failed because no Postgres was reachable, and `make lint` couldn't run because `bin/golangci-lint` isn't there.", final=True)
    return s


def s02():
    s = Session("s02", "2026-10-07T14:03:51.000", "fix/hold-expiry")
    s.human("seat holds aren't expiring?? I put a hold on the 14:00 Picton sailing yesterday afternoon and it's still eating capacity this morning. any idea what's going on")
    s.say("Let me look at how holds decide they've expired.")
    s.read("internal/booking/hold.go")
    s.grep("Expired\\(|ActiveHolds", files=["internal/booking/hold.go", "internal/booking/service.go", "internal/store/postgres.go", "internal/db/queries.sql.go"])
    ln = s.line_of("internal/store/postgres.go", "func (p *Postgres) ActiveHolds")
    s.read("internal/store/postgres.go", offset=ln, limit=14)
    s.think("Hold.Expired uses TTL, but store never sets TTL -> TTL == 0 -> Expired always false. ExpiresAt is populated. Want to confirm with running server.")
    s.say("`Hold.Expired` only returns true when `TTL > 0`, and the store never fills `TTL` in when it loads holds, so loaded holds never expire. Let me start the server and reproduce against the API to be sure.")
    s.bash_timeout("go run ./cmd/ferrydesk", server_log(s), desc="Start API server to reproduce")
    s.say("The server started and kept running in the foreground until the command timed out, so I couldn't hit it from here.", final=True)
    s.human("yeah it's a server, it doesn't exit lol. just write a test for it")
    s.write("internal/booking/hold_test.go",
            "package booking\n\nimport (\n\t\"testing\"\n\t\"time\"\n)\n\n"
            "func TestExpiredUsesExpiresAt(t *testing.T) {\n"
            "\tnow := time.Date(2026, 10, 7, 9, 0, 0, 0, time.UTC)\n"
            "\t// as loaded from the store: ExpiresAt set, TTL not\n"
            "\th := Hold{CreatedAt: now.Add(-2 * time.Hour), ExpiresAt: now.Add(-time.Hour)}\n"
            "\tif !h.Expired(now) {\n\t\tt.Fatalf(\"Expired() = false for a hold that expired an hour ago\")\n\t}\n"
            "\tif h.Expired(now.Add(-90 * time.Minute)) {\n\t\tt.Fatalf(\"Expired() = true before ExpiresAt\")\n\t}\n}\n")
    s.bash("go test ./internal/booking/ -run TestExpired",
           "--- FAIL: TestExpiredUsesExpiresAt (0.00s)\n    hold_test.go:13: Expired() = false for a hold that expired an hour ago\nFAIL\n"
           f"FAIL\t{MOD}/internal/booking\t0.006s\nFAIL", code=1, desc="Run new hold test (expect failure)")
    s.say("That reproduces it. Fixing `Expired` to use `ExpiresAt`, which is the column the store actually populates.")
    s.edit("internal/booking/hold.go",
           "\treturn h.TTL > 0 && now.Sub(h.CreatedAt) > h.TTL",
           "\treturn !h.ExpiresAt.IsZero() && !now.Before(h.ExpiresAt)", expect="ok")
    s.edit("internal/booking/hold.go", "\tExpiresAt time.Time\n\tTTL       time.Duration\n}", "\tExpiresAt time.Time\n}", expect="ok")
    s.bash("go test ./internal/booking/", f"ok  \t{MOD}/internal/booking\t0.008s", desc="Run booking tests")
    s.say("Fixed: holds now expire at `ExpiresAt`. I removed the unused `TTL` field. Booking tests pass, including the new regression test.", final=True)
    s.human("nice. while you're there can createHold log the hold id and expiry? we're flying blind on holds in prod")
    s.edit("internal/api/handlers.go",
           '\tif err != nil {\n\t\th.log.Error("create hold", "err", err)\n' + ERR500 +
           '\twriteJSON(w, http.StatusCreated, map[string]any{"hold_id": hold.ID})',
           '\tif err != nil {\n\t\th.log.Error("create hold", "err", err)\n' + ERR500 +
           '\th.log.Info("hold created", "hold_id", hold.ID, "sailing_id", id, "seats", hold.Seats, "expires_at", hold.ExpiresAt)\n'
           '\twriteJSON(w, http.StatusCreated, map[string]any{"hold_id": hold.ID})', expect="missing")
    ln = s.line_of("internal/api/handlers.go", "func (h *Handlers) createHold")
    s.read("internal/api/handlers.go", offset=ln, limit=30)
    s.edit("internal/api/handlers.go",
           '\twriteJSON(w, http.StatusCreated, map[string]any{"hold_id": hold.ID, "expires_at": hold.ExpiresAt})',
           '\th.log.Info("hold created", "hold_id", hold.ID, "sailing_id", id, "seats", hold.Seats, "expires_at", hold.ExpiresAt)\n'
           '\twriteJSON(w, http.StatusCreated, map[string]any{"hold_id": hold.ID, "expires_at": hold.ExpiresAt})', expect="ok")
    s.bash("go build ./... && go test ./internal/api/", f"ok  \t{MOD}/internal/api\t0.015s", desc="Build and run API tests")
    s.say("Added an info log in `createHold` with hold_id, sailing_id, seats and expires_at. Build and API tests pass.", final=True)
    s.human("thanks")
    return s


def s03():
    s = Session("s03", "2026-10-09T10:20:13.000", "feat/child-fares")
    s.human("we need child fares. under 12 pays half the adult fare. Passengers should get a Children count, the pricing package is where all this lives")
    s.read("internal/pricing/fare.go")
    s.read("internal/pricing/fare_test.go")
    s.say("I'll add `Children` to `Passengers` and price children at 50% of the adult fare.")
    s.edit("internal/pricing/fare.go",
           "// Passengers counts travellers on one booking.\ntype Passengers struct {\n\tAdults int\n}\n\n"
           "// Fare returns the total fare for a booking on the given route.\nfunc Fare(r Route, p Passengers) Cents {\n"
           "\treturn r.AdultFare * Cents(p.Adults)\n}",
           "// Passengers counts travellers on one booking. Children are under 12.\ntype Passengers struct {\n\tAdults   int\n\tChildren int\n}\n\n"
           "// ChildFareRatio is the share of the adult fare paid by a child.\nconst ChildFareRatio = 0.5\n\n"
           "// Fare returns the total fare for a booking on the given route.\nfunc Fare(r Route, p Passengers) float64 {\n"
           "\tadult := float64(r.AdultFare) * float64(p.Adults)\n"
           "\tchild := float64(r.AdultFare) * ChildFareRatio * float64(p.Children)\n"
           "\treturn adult + child\n}", expect="ok")
    s.bash("go build ./...",
           "# github.com/tidewater-labs/ferrydesk/internal/booking\n"
           "internal/booking/service.go:121:83: cannot use total (variable of type float64) as pricing.Cents value in struct literal",
           code=1, desc="Build all packages")
    s.edit("internal/booking/service.go",
           "Status: StatusConfirmed, Total: total})",
           "Status: StatusConfirmed, Total: pricing.Cents(math.Round(total))})", expect="ok")
    s.edit("internal/booking/service.go", "import (\n\t\"context\"\n\t\"errors\"\n", "import (\n\t\"context\"\n\t\"errors\"\n\t\"math\"\n", expect="ok")
    s.bash("go build ./...", desc="Build all packages")
    s.say("Builds now. `Fare` returns a float64 total and `Confirm` rounds it into `Cents`. Next I'll add tests.", final=True)
    s.human("hmm why does Fare return a float64 now? everything money-related here is int64 cents, that's the whole point of the Cents type. please don't round-trip through floats")
    s.say("Sorry, you're right. Let me look at the helpers in money.go and keep it in `Cents` end to end.")
    s.read("internal/pricing/money.go")
    s.edit("internal/pricing/fare.go",
           "// ChildFareRatio is the share of the adult fare paid by a child.\nconst ChildFareRatio = 0.5\n\n"
           "// Fare returns the total fare for a booking on the given route.\nfunc Fare(r Route, p Passengers) float64 {\n"
           "\tadult := float64(r.AdultFare) * float64(p.Adults)\n"
           "\tchild := float64(r.AdultFare) * ChildFareRatio * float64(p.Children)\n"
           "\treturn adult + child\n}",
           "// childFareBps is the share of the adult fare a child pays, in basis points.\nconst childFareBps = 5000\n\n"
           "// Fare returns the total fare for a booking on the given route.\n"
           "// Each child pays half the adult fare, rounded half up per child.\nfunc Fare(r Route, p Passengers) Cents {\n"
           "\tadult := r.AdultFare * Cents(p.Adults)\n"
           "\tchild := ApplyBasisPoints(r.AdultFare, childFareBps) * Cents(p.Children)\n"
           "\treturn adult + child\n}", expect="ok")
    s.edit("internal/booking/service.go",
           "Status: StatusConfirmed, Total: pricing.Cents(math.Round(total))})",
           "Status: StatusConfirmed, Total: total})", expect="ok")
    s.edit("internal/booking/service.go", "\t\"errors\"\n\t\"math\"\n", "\t\"errors\"\n", expect="ok")
    s.edit("internal/pricing/fare_test.go",
           "func TestApplyBasisPointsRoundsHalfUp(t *testing.T) {",
           "func TestFareChildren(t *testing.T) {\n\tr := Route{Code: \"WLG-PCN\", AdultFare: 7451}\n"
           "\t// child fare 3725.5 rounds half up to 3726\n"
           "\tif got := Fare(r, Passengers{Adults: 1, Children: 2}); got != 7451+2*3726 {\n\t\tt.Fatalf(\"got %v\", got)\n\t}\n}\n\n"
           "func TestApplyBasisPointsRoundsHalfUp(t *testing.T) {", expect="ok")
    s.bash("go test ./...", gotest_all(), code=1, desc="Run test suite", run_secs=8.0)
    s.bash("go test ./internal/pricing/ ./internal/booking/ -v -run 'Fare|Cancel'",
           "=== RUN   TestFareAdults\n--- PASS: TestFareAdults (0.00s)\n=== RUN   TestFareChildren\n--- PASS: TestFareChildren (0.00s)\nPASS\n"
           f"ok  \t{MOD}/internal/pricing\t0.005s\n=== RUN   TestCancelRefundsTotal\n--- PASS: TestCancelRefundsTotal (0.00s)\nPASS\nok  \t{MOD}/internal/booking\t0.007s",
           desc="Run pricing and booking tests verbosely")
    s.say("Reworked: `Fare` returns `Cents` again, child fare is `ApplyBasisPoints(adult, 5000)` per child (half up), and nothing goes through float. Pricing and booking tests pass. The `internal/store` failures are connection refused against localhost Postgres, not related.", final=True)
    s.human("ok good. per child is right, 3 kids on 7450 should be 3x3725. ship it")
    s.say("Agreed: 7450 is even, so each child is 3725 and three are 11175. The current code does exactly that.", final=True)
    return s


def s04():
    s = Session("s04", "2026-10-12T16:41:30.000", "feat/refund-reason")
    s.human("add a reason to refunds, nullable text column. we want to record why ops refunded something")
    s.glob("migrations/*.sql", sorted(k[len(CWD) + 1:] for k in s.fs if "/migrations/" in k))
    s.read("migrations/0004_add_refunds.up.sql")
    s.say("I'll add the column to the refunds table definition.")
    s.edit("migrations/0004_add_refunds.up.sql",
           "    amount_cents bigint NOT NULL,\n",
           "    amount_cents bigint NOT NULL,\n    reason       text,\n", expect="ok")
    s.say("Added `reason text` (nullable) to refunds. Next I'll update the sqlc query.", final=True)
    s.human("wait, don't change 0004. that's been applied on staging and everyone's laptop, editing it does nothing for an existing db and breaks the checksum. new migration please")
    s.edit("migrations/0004_add_refunds.up.sql",
           "    amount_cents bigint NOT NULL,\n    reason       text,\n",
           "    amount_cents bigint NOT NULL,\n", expect="ok")
    s.write("migrations/0007_refund_reason.up.sql", REFUND_REASON_UP)
    s.write("migrations/0007_refund_reason.down.sql", REFUND_REASON_DOWN)
    s.say("Reverted 0004 and added 0007_refund_reason. Let me apply it locally.")
    s.reject("Bash", {"command": "make db-reset", "description": "Rebuild local database with migrations"},
             reason="no, db-reset wipes my local data, I've got QA bookings in there. just migrate up")
    s.bash("make migrate-up", "migrate -path migrations -database \"$FERRYDESK_DATABASE_URL\" up\n7/u refund_reason (11.840ms)", desc="Apply pending migrations")
    s.edit("db/query/bookings.sql",
           "-- name: CreateRefund :exec\nINSERT INTO refunds (booking_id, amount_cents) VALUES ($1, $2);",
           "-- name: CreateRefund :exec\nINSERT INTO refunds (booking_id, amount_cents, reason) VALUES ($1, $2, $3);", expect="ok")
    s.bash("make generate", "sqlc generate", desc="Regenerate sqlc code")
    s.edit("internal/store/postgres.go",
           "\treturn p.q.CreateRefund(ctx, db.CreateRefundParams{BookingID: bookingID, AmountCents: int64(amount)})",
           "\treturn p.q.CreateRefund(ctx, db.CreateRefundParams{BookingID: bookingID, AmountCents: int64(amount), Reason: pgtype.Text{}})", expect="ok")
    s.bash("go build ./...", "# github.com/tidewater-labs/ferrydesk/internal/store\ninternal/store/postgres.go:117:118: undefined: pgtype", code=1, desc="Build all packages")
    s.edit("internal/store/postgres.go", "\t\"github.com/jackc/pgx/v5\"\n", "\t\"github.com/jackc/pgx/v5\"\n\t\"github.com/jackc/pgx/v5/pgtype\"\n", expect="ok")
    s.bash("go build ./...", desc="Build all packages")
    s.say("Done:\n- `migrations/0007_refund_reason.{up,down}.sql` adds/drops `refunds.reason text`.\n- `CreateRefund` query takes the reason; sqlc regenerated.\n- Store passes a null reason for now; wiring it to the API is a follow-up.\n\nApplied locally with `make migrate-up` (no reset).", final=True)
    return s


def s05():
    s = Session("s05", "2026-10-15T11:05:42.000", "chore/errcheck-api")
    s.human("CI lint is failing on errcheck in internal/api, can you clean those up")
    s.bash("make lint", LINT_FAIL, code=2, desc="Run linter")
    s.bash("which golangci-lint", "", code=1, desc="Check for golangci-lint on PATH")
    s.say("golangci-lint isn't installed here. I'll run the version pinned in the Makefile through `go run`.")
    lint_out = []
    for ent in ["Route", "Vessel", "Sailing", "Booking", "Refund", "Discount", "Operator", "Terminal"]:
        ln = s.line_of("internal/api/handlers.go", f"\th.admin.Delete{ent}(r.Context(), id)")
        lint_out.append(f"internal/api/handlers.go:{ln}:23: Error return value of `h.admin.Delete{ent}` is not checked (errcheck)\n\th.admin.Delete{ent}(r.Context(), id)\n\t                     ^")
    ln = s.line_of("internal/api/handlers.go", "\tjson.NewEncoder(w).Encode(v)")
    lint_out.append(f"internal/api/handlers.go:{ln}:28: Error return value of `(*encoding/json.Encoder).Encode` is not checked (errcheck)\n\tjson.NewEncoder(w).Encode(v)\n\t                          ^")
    s.bash("go run github.com/golangci/golangci-lint/cmd/golangci-lint@v1.61.0 run ./internal/api/...", "\n".join(lint_out), code=1,
           desc="Run pinned golangci-lint on api package", run_secs=48.0)
    s.say("Nine errcheck hits: the eight admin delete handlers drop the store error, and `writeJSON` ignores the encode error. Starting with the delete handlers, since they all end the same way.")
    s.edit("internal/api/handlers.go",
           "(r.Context(), id)\n\tw.WriteHeader(http.StatusNoContent)\n}",
           "(r.Context(), id); err != nil {\n\t\th.adminError(w, err)\n\t\treturn\n\t}\n\tw.WriteHeader(http.StatusNoContent)\n}", expect="multi")
    s.say("Each call site needs its own context. I'll add a small helper first, then fix them one by one.")
    s.edit("internal/api/handlers.go",
           "func writeJSON(w http.ResponseWriter, status int, v any) {",
           "// adminError maps a store error from an admin handler to a response.\n"
           "func (h *Handlers) adminError(w http.ResponseWriter, err error) {\n"
           "\tif errors.Is(err, booking.ErrNotFound) {\n\t\twriteJSON(w, http.StatusNotFound, map[string]string{\"error\": \"not found\"})\n\t\treturn\n\t}\n"
           "\twriteJSON(w, http.StatusInternalServerError, map[string]string{\"error\": \"internal error\"})\n}\n\n"
           "func writeJSON(w http.ResponseWriter, status int, v any) {", expect="ok")
    for ent in ["Route", "Sailing"]:
        s.edit("internal/api/handlers.go",
               f"\th.admin.Delete{ent}(r.Context(), id)\n",
               f"\tif err := h.admin.Delete{ent}(r.Context(), id); err != nil {{\n\t\th.adminError(w, err)\n\t\treturn\n\t}}\n", expect="ok")
    s.edit("internal/api/handlers.go",
           "\th.admin.DeleteVessel(ctx, id)\n",
           "\tif err := h.admin.DeleteVessel(ctx, id); err != nil {\n\t\th.adminError(w, err)\n\t\treturn\n\t}\n", expect="missing")
    ln = s.line_of("internal/api/handlers.go", "func (h *Handlers) adminDeleteVessel")
    s.read("internal/api/handlers.go", offset=ln, limit=12)
    for ent in ["Vessel", "Booking", "Refund", "Discount", "Operator", "Terminal"]:
        s.edit("internal/api/handlers.go",
               f"\th.admin.Delete{ent}(r.Context(), id)\n",
               f"\tif err := h.admin.Delete{ent}(r.Context(), id); err != nil {{\n\t\th.adminError(w, err)\n\t\treturn\n\t}}\n", expect="ok")
    s.say("Now `writeJSON`.")
    s.edit("internal/api/handlers.go",
           "    json.NewEncoder(w).Encode(v)\n",
           "    if err := json.NewEncoder(w).Encode(v); err != nil {\n        slog.Default().Warn(\"write response\", \"err\", err)\n    }\n", expect="missing")
    ln = s.line_of("internal/api/handlers.go", "func writeJSON")
    s.read("internal/api/handlers.go", offset=ln, limit=6)
    s.edit("internal/api/handlers.go",
           "\tjson.NewEncoder(w).Encode(v)\n",
           "\tif err := json.NewEncoder(w).Encode(v); err != nil {\n\t\tslog.Default().Warn(\"write response\", \"err\", err)\n\t}\n", expect="ok")
    s.bash("go run github.com/golangci/golangci-lint/cmd/golangci-lint@v1.61.0 run ./internal/api/...", "", desc="Re-run pinned golangci-lint on api package", run_secs=21.0)
    s.bash("go build ./... && go test ./internal/api/", f"ok  \t{MOD}/internal/api\t0.016s", desc="Build and run API tests")
    s.say("errcheck is clean for `internal/api`:\n- The 8 admin delete handlers now check the store error via a new `adminError` helper (404 on not found, 500 otherwise). They used to return 204 even when the delete failed.\n- `writeJSON` logs encode failures.\n\n`make lint` fails locally because `bin/golangci-lint` doesn't exist, so I ran the pinned v1.61.0 with `go run`.", final=True)
    return s


def s06():
    s = Session("s06", "2026-10-19T09:30:08.000", "main")
    s.human("8080 clashes with something else on my machine, can you get ferrydesk running on 9090 locally?")
    s.read("README.md")
    s.say("The README says the port comes from `FD_PORT`. I'll start it with that set.")
    s.bash_timeout("FD_PORT=9090 go run ./cmd/ferrydesk", server_log(s, ":8080"), timeout_ms=20000, desc="Start server on port 9090")
    s.say("It started, but the log says it's listening on `:8080`, not `:9090`, so `FD_PORT` was ignored. (The command also kept running until the timeout.)", final=True)
    s.human("why is it still on 8080?")
    s.read("internal/config/config.go")
    s.say("The README is wrong. `config.Load` reads every variable with the prefix `FERRYDESK_` (`envPrefix = \"FERRYDESK_\"`), so the port variable is `FERRYDESK_PORT`. `FD_PORT` isn't read at all, and neither are the other `FD_*` names in the README and `.env.example`. Let me check that it works with the right name, in the background this time.")
    s.bash("FERRYDESK_PORT=9090 go run ./cmd/ferrydesk", background=True, desc="Start server on 9090 in background")
    s.bash("sleep 2 && curl -s localhost:9090/healthz", '{"status":"ok"}', desc="Check health endpoint on 9090")
    s.say("Confirmed: `FERRYDESK_PORT=9090` works and `/healthz` answers on 9090. Want me to fix the README and `.env.example` to use the real prefix?", final=True)
    s.human("huh. ok just fix .env.example, someone else owns the readme so leave that for now")
    s.write(".env.example", "FERRYDESK_PORT=8080\nFERRYDESK_DATABASE_URL=postgres://ferry:ferry@localhost:5432/ferrydesk?sslmode=disable\nFERRYDESK_LOG_LEVEL=info\nFERRYDESK_HOLD_TTL=15m\n")
    s.bash("go test ./internal/config/", f"ok  \t{MOD}/internal/config\t0.004s", desc="Run config tests")
    s.say("`.env.example` now uses `FERRYDESK_*`. I left the README alone. For your machine, put `FERRYDESK_PORT=9090` in `.env`. The server is still running in the background on 9090.", final=True)
    return s


def s07():
    s = Session("s07", "2026-10-23T13:15:27.000", "feat/group-discount")
    s.human("group bookings: 10+ adults get 15% off. before you write anything figure out how discounts get applied today (I think there's something in pricing, and maybe the admin discounts endpoints?) then implement")
    s.say("I'll have a subagent map the current discount paths first.")

    def sub(a):
        a.grep("Discount", files=["internal/pricing/fare.go", "internal/pricing/fare_test.go", "internal/api/handlers.go", "internal/api/admin.go"])
        a.read("internal/pricing/fare.go")
        a.read("internal/pricing/money.go")
        a.grep("ApplyDiscounts\\(", content_out=CWD + "/internal/pricing/fare.go:24:func ApplyDiscounts(total Cents, ds []Discount) Cents {\n"
               + CWD + "/internal/pricing/fare_test.go:19:\tgot := ApplyDiscounts(10000, []Discount{{Code: \"SENIOR\", Bps: 2000}})", mode="content")
        ln = a.line_of("internal/api/handlers.go", "func (h *Handlers) adminListDiscounts")
        a.read("internal/api/handlers.go", offset=ln, limit=40)
    report = ("Findings:\n\n"
              "1. `internal/pricing/fare.go` defines `Discount{Code, Bps}` and `ApplyDiscounts(total Cents, ds []Discount) Cents`, which subtracts each discount in order using `ApplyBasisPoints` from `money.go` (integer cents, rounded half up).\n"
              "2. Nothing in production code calls `ApplyDiscounts` yet; only `fare_test.go` does. `booking.Service.Confirm` calls `pricing.Fare` directly with no discounts.\n"
              "3. The `/admin/discounts` handlers in `internal/api/handlers.go` are generic CRUD over `AdminStore` and don't touch pricing.\n\n"
              "So the natural hook is: build a `[]Discount` in pricing (e.g. a group discount when Adults >= 10) and apply it in `Fare` or in `Confirm`.")
    s.task("Map discount code paths", "Find every place discounts are defined or applied in this Go repo (/work/ferrydesk). Report the functions, how amounts are computed, and who calls them. Do not modify files.",
           "Explore", report, sub)
    s.read("internal/pricing/fare.go")
    s.edit("internal/pricing/fare.go",
           "// Discount is applied to a fare total, in basis points.",
           "// GroupFare applies the 15% group discount for bookings with 10 or more adults.\n"
           "func GroupFare(total Cents, adults int) Cents {\n\tif adults < 10 {\n\t\treturn total\n\t}\n"
           "\treturn Cents(math.Round(float64(total) * 0.85))\n}\n\n"
           "// Discount is applied to a fare total, in basis points.", expect="ok")
    s.edit("internal/pricing/fare.go", "package pricing\n", "package pricing\n\nimport \"math\"\n", expect="ok")
    s.edit("internal/pricing/fare_test.go",
           "func TestApplyDiscounts(t *testing.T) {",
           "func TestGroupFare(t *testing.T) {\n\tif got := GroupFare(74500, 10); got != 63325 {\n\t\tt.Fatalf(\"got %v\", got)\n\t}\n"
           "\tif got := GroupFare(74500, 9); got != 74500 {\n\t\tt.Fatalf(\"got %v\", got)\n\t}\n}\n\n"
           "func TestApplyDiscounts(t *testing.T) {", expect="ok")
    s.bash("go test ./internal/pricing/", f"ok  \t{MOD}/internal/pricing\t0.005s", desc="Run pricing tests")
    s.say("Added `GroupFare`, which takes 15% off totals for 10+ adults, with tests. Next I'll call it from `Confirm`.", final=True)
    s.human("no floats in money math please, we've been through this. there's literally ApplyBasisPoints and a Discount type right there, the subagent even told you. use a Discount{Bps: 1500}")
    s.edit("internal/pricing/fare.go",
           "// GroupFare applies the 15% group discount for bookings with 10 or more adults.\n"
           "func GroupFare(total Cents, adults int) Cents {\n\tif adults < 10 {\n\t\treturn total\n\t}\n"
           "\treturn Cents(math.Round(float64(total) * 0.85))\n}\n\n",
           "// GroupDiscount is 15% off for bookings with 10 or more adults.\nvar GroupDiscount = Discount{Code: \"GROUP10\", Bps: 1500}\n\n"
           "// DiscountsFor returns the automatic discounts that apply to p.\nfunc DiscountsFor(p Passengers) []Discount {\n"
           "\tif p.Adults >= 10 {\n\t\treturn []Discount{GroupDiscount}\n\t}\n\treturn nil\n}\n\n", expect="ok")
    s.edit("internal/pricing/fare.go", "package pricing\n\nimport \"math\"\n", "package pricing\n", expect="ok")
    s.edit("internal/pricing/fare_test.go",
           "func TestGroupFare(t *testing.T) {\n\tif got := GroupFare(74500, 10); got != 63325 {\n\t\tt.Fatalf(\"got %v\", got)\n\t}\n"
           "\tif got := GroupFare(74500, 9); got != 74500 {\n\t\tt.Fatalf(\"got %v\", got)\n\t}\n}",
           "func TestGroupDiscount(t *testing.T) {\n\tp := Passengers{Adults: 10}\n\tif got := ApplyDiscounts(Fare(Route{AdultFare: 7450}, p), DiscountsFor(p)); got != 63325 {\n\t\tt.Fatalf(\"got %v\", got)\n\t}\n"
           "\tif ds := DiscountsFor(Passengers{Adults: 9}); len(ds) != 0 {\n\t\tt.Fatalf(\"got %v\", ds)\n\t}\n}", expect="ok")
    s.edit("internal/booking/service.go",
           "\ttotal := pricing.Fare(pricing.Route{Code: sl.RouteCode, AdultFare: sl.AdultFare}, p)\n",
           "\ttotal := pricing.Fare(pricing.Route{Code: sl.RouteCode, AdultFare: sl.AdultFare}, p)\n\ttotal = pricing.ApplyDiscounts(total, pricing.DiscountsFor(p))\n", expect="ok")
    s.bash("go test ./internal/pricing/ ./internal/booking/", f"ok  \t{MOD}/internal/pricing\t0.005s\nok  \t{MOD}/internal/booking\t0.009s", desc="Run pricing and booking tests")
    s.bash("make lint", LINT_FAIL, code=2, desc="Run linter")
    s.bash("go vet ./internal/...", desc="Run go vet")
    s.say("Redone with integer math: `GroupDiscount = Discount{Code: \"GROUP10\", Bps: 1500}`, and `DiscountsFor(p)` returns it for 10+ adults. `Confirm` applies `ApplyDiscounts(total, DiscountsFor(p))`. Tests pass and vet is clean (`make lint` can't run here, no golangci-lint binary). Pushing the branch.")
    s.reject("Bash", {"command": "git add -A && git commit -m \"pricing: 15% group discount for 10+ adults\" && git push -u origin feat/group-discount", "description": "Commit and push branch"})
    s.human("I'll push it myself once I've looked at the diff, thanks")
    s.say("Sure. Nothing is committed or pushed; the changes are in the working tree on `feat/group-discount`.", final=True)
    return s


def s08():
    s = Session("s08", "2026-10-28T15:50:19.000", "fix/cancel-twice-409")
    s.human("DELETE /bookings/{id} on a booking that's already cancelled gives a 500. should be a 409. and it obviously shouldn't refund it a second time")
    ln = s.line_of("internal/booking/service.go", "func (s *Service) Cancel")
    s.read("internal/booking/service.go", offset=ln, limit=12)
    s.say("`Cancel` never checks the current status, so a second cancel writes a second refund. The 500 probably comes from a constraint on the second refund insert. Fixing the service first.")
    s.edit("internal/booking/service.go",
           "\tb, err := s.store.GetBooking(ctx, bookingID)\n\tif err != nil {\n\t\treturn err\n\t}\n\tif err := s.store.SetBookingStatus",
           "\tb, err := s.store.GetBooking(ctx, bookingID)\n\tif err != nil {\n\t\treturn err\n\t}\n\tif b.Status == StatusCancelled {\n\t\treturn ErrAlreadyCancelled\n\t}\n\tif err := s.store.SetBookingStatus", expect="ok")
    s.say("Now the handler needs to map `ErrAlreadyCancelled` to 409.")
    s.edit("internal/api/handlers.go",
           '\tif errors.Is(err, booking.ErrNotFound) {\n\t\twriteJSON(w, http.StatusNotFound, map[string]string{"error": "not found"})\n\t\treturn\n\t}\n',
           '\tif errors.Is(err, booking.ErrNotFound) {\n\t\twriteJSON(w, http.StatusNotFound, map[string]string{"error": "not found"})\n\t\treturn\n\t}\n'
           '\tif errors.Is(err, booking.ErrAlreadyCancelled) {\n\t\twriteJSON(w, http.StatusConflict, map[string]string{"error": "already cancelled"})\n\t\treturn\n\t}\n', expect="multi")
    s.edit("internal/api/handlers.go",
           '\t\th.log.Error("cancel", "err", err)\n',
           '\t\tif errors.Is(err, booking.ErrAlreadyCancelled) {\n\t\t\twriteJSON(w, http.StatusConflict, map[string]string{"error": "already cancelled"})\n\t\t\treturn\n\t\t}\n\t\th.log.Error("cancel", "err", err)\n', expect="missing")
    ln = s.line_of("internal/api/handlers.go", "func (h *Handlers) cancelBooking")
    s.read("internal/api/handlers.go", offset=ln, limit=20)
    s.edit("internal/api/handlers.go",
           '\t\th.log.Error("cancel booking", "err", err)\n',
           '\t\tif errors.Is(err, booking.ErrAlreadyCancelled) {\n\t\t\twriteJSON(w, http.StatusConflict, map[string]string{"error": "already cancelled"})\n\t\t\treturn\n\t\t}\n\t\th.log.Error("cancel booking", "err", err)\n', expect="ok")
    s.say("I'll verify against a fresh database.")
    s.reject("Bash", {"command": "docker compose down -v && docker compose up -d db && sleep 3 && make migrate-up", "description": "Recreate local Postgres and apply migrations"},
             reason="please stop wiping the db volume, that's where my seed data and the QA bookings live. if you need a db use a separate one, or just unit test it")
    s.say("Understood, no database. I'll cover it with unit tests using the fake store.")
    s.edit("internal/booking/service_test.go",
           "func TestCancelRefundsTotal(t *testing.T) {",
           "func TestCancelTwiceRefundsOnce(t *testing.T) {\n"
           "\tfs := &fakeStore{bookings: map[int64]Booking{7: {ID: 7, Status: StatusConfirmed, Total: 14900}}}\n"
           "\tsvc := NewService(fs, 15*time.Minute)\n\tif err := svc.Cancel(context.Background(), 7); err != nil {\n\t\tt.Fatal(err)\n\t}\n"
           "\tif err := svc.Cancel(context.Background(), 7); err != ErrAlreadyCancelled {\n\t\tt.Fatalf(\"second cancel err = %v\", err)\n\t}\n"
           "\tif len(fs.refunds) != 1 {\n\t\tt.Fatalf(\"refunds = %v\", fs.refunds)\n\t}\n}\n\n"
           "func TestCancelRefundsTotal(t *testing.T) {", expect="ok")
    s.bash("go test ./internal/booking/ ./internal/api/", f"ok  \t{MOD}/internal/booking\t0.009s\nok  \t{MOD}/internal/api\t0.016s", desc="Run booking and API tests")
    s.say("Fixed.\n- `Service.Cancel` returns `ErrAlreadyCancelled` when the booking is already cancelled, before it writes a status change or a refund.\n- `cancelBooking` maps that to 409 `{\"error\":\"already cancelled\"}`.\n- `TestCancelTwiceRefundsOnce` covers it.\n\nI didn't touch your local database.", final=True)
    return s


def s09():
    s = Session("s09", "2026-11-02T10:02:55.000", "feat/sailing-search")
    s.human("listSailings only does a hardcoded 14 day window from now. add optional `from` and `to` query params (YYYY-MM-DD, interpreted as Pacific/Auckland dates), keep the current behaviour as the default, 400 on garbage, and cap the window at 60 days. tests please")
    ln = s.line_of("internal/api/handlers.go", "func (h *Handlers) listSailings")
    s.read("internal/api/handlers.go", offset=ln, limit=24)
    s.say("I'll put the parsing in its own small file so it's easy to test, and call it from `listSailings`.")
    s.write("internal/api/window.go",
            "package api\n\nimport (\n\t\"errors\"\n\t\"time\"\n)\n\n"
            "const maxWindow = 60 * 24 * time.Hour\n\nvar nzt = mustLoad(\"Pacific/Auckland\")\n\n"
            "func mustLoad(name string) *time.Location {\n\tloc, err := time.LoadLocation(name)\n\tif err != nil {\n\t\tpanic(err)\n\t}\n\treturn loc\n}\n\n"
            "// parseWindow turns optional from/to dates into a [from, to) range.\n"
            "func parseWindow(fromS, toS string, now time.Time) (time.Time, time.Time, error) {\n"
            "\tfrom, to := now, now.Add(14*24*time.Hour)\n\tvar err error\n"
            "\tif fromS != \"\" {\n\t\tif from, err = time.ParseInLocation(\"2006-01-02\", fromS, nzt); err != nil {\n\t\t\treturn time.Time{}, time.Time{}, errors.New(\"invalid from\")\n\t\t}\n\t}\n"
            "\tif toS != \"\" {\n\t\tif to, err = time.ParseInLocation(\"2006-01-02\", toS, nzt); err != nil {\n\t\t\treturn time.Time{}, time.Time{}, errors.New(\"invalid to\")\n\t\t}\n\t\tto = to.AddDate(0, 0, 1)\n\t}\n"
            "\tif !to.After(from) {\n\t\treturn time.Time{}, time.Time{}, errors.New(\"to must be after from\")\n\t}\n"
            "\tif to.Sub(from) > maxWindow {\n\t\treturn time.Time{}, time.Time{}, errors.New(\"window longer than 60 days\")\n\t}\n"
            "\treturn from, to, nil\n}\n")
    s.edit("internal/api/handlers.go",
           "\tfrom := time.Now()\n\tto := from.Add(14 * 24 * time.Hour)\n",
           "\tfrom, to, err := parseWindow(r.URL.Query().Get(\"from\"), r.URL.Query().Get(\"to\"), time.Now())\n"
           "\tif err != nil {\n\t\twriteJSON(w, http.StatusBadRequest, map[string]string{\"error\": err.Error()})\n\t\treturn\n\t}\n", expect="ok")
    s.write("internal/api/window_test.go",
            "package api\n\nimport (\n\t\"testing\"\n\t\"time\"\n)\n\n"
            "func TestParseWindow(t *testing.T) {\n\tnow := time.Date(2026, 11, 2, 0, 0, 0, 0, time.UTC)\n"
            "\tcases := []struct {\n\t\tfrom, to string\n\t\twantErr  bool\n\t}{\n"
            "\t\t{\"\", \"\", false},\n\t\t{\"2026-12-01\", \"2026-12-07\", false},\n\t\t{\"2026-12-07\", \"2026-12-01\", true},\n"
            "\t\t{\"2026-12-01\", \"2027-03-01\", true},\n\t\t{\"tomorrow\", \"\", true},\n\t}\n"
            "\tfor _, c := range cases {\n\t\t_, _, err := parseWindow(c.from, c.to, now)\n\t\tif (err != nil) != c.wantErr {\n"
            "\t\t\tt.Errorf(\"parseWindow(%q, %q) err = %v, wantErr %v\", c.from, c.to, err, c.wantErr)\n\t\t}\n\t}\n}\n")
    s.bash("go biuld ./...", "go biuld: unknown command\nRun 'go help' for usage.", code=2, desc="Build all packages")
    s.bash("go build ./...", desc="Build all packages")
    s.bash("go test ./...", gotest_all(api="0.021s"), code=1, desc="Run test suite", run_secs=9.0)
    s.bash("go test ./internal/api/ -run 'ParseWindow|GetSailing' -v",
           "=== RUN   TestGetSailingBadID\n--- PASS: TestGetSailingBadID (0.00s)\n=== RUN   TestParseWindow\n--- PASS: TestParseWindow (0.00s)\nPASS\n"
           f"ok  \t{MOD}/internal/api\t0.019s", desc="Run window tests verbosely")
    s.bash("make lint", LINT_FAIL, code=2, desc="Run linter")
    s.bash("gofmt -l ./internal && go vet ./internal/api/", desc="Check formatting and vet api package")
    s.say("Added `from`/`to` to `GET /sailings`:\n- `internal/api/window.go`: `parseWindow` parses Pacific/Auckland dates; `to` is inclusive. With no params it keeps the old 14-day window. Bad dates, reversed ranges and windows over 60 days return 400.\n- `listSailings` uses it.\n- `window_test.go` covers the cases.\n\nAPI tests pass, and gofmt and vet are clean. `internal/store` tests fail without a local Postgres (connection refused). `make lint` can't find `bin/golangci-lint`.", final=True)
    return s


def s10():
    extra = {"migrations/0007_refund_reason.up.sql": REFUND_REASON_UP, "migrations/0007_refund_reason.down.sql": REFUND_REASON_DOWN}
    s = Session("s10", "2026-11-05T11:30:44.000", "feat/waitlist", extra_files=extra)
    s.human("starting on the waitlist. need a table waitlist_entries (sailing_id, email, seats, created_at). just the migration + a sqlc query to add/list for a sailing, nothing else yet")
    s.glob("migrations/*.up.sql", sorted(k[len(CWD) + 1:] for k in s.fs if "/migrations/" in k and k.endswith(".up.sql")))
    s.read("migrations/0007_refund_reason.up.sql")
    s.say("0007 is the latest migration and only adds one column, so I'll put the new table in it and keep this release to a single migration.")
    s.edit("migrations/0007_refund_reason.up.sql",
           REFUND_REASON_UP,
           REFUND_REASON_UP + "\nCREATE TABLE waitlist_entries (\n    id          bigserial PRIMARY KEY,\n    sailing_id  bigint NOT NULL REFERENCES sailings(id),\n"
           "    email       text NOT NULL,\n    seats       integer NOT NULL,\n    created_at  timestamptz NOT NULL DEFAULT now()\n);\n"
           "CREATE INDEX waitlist_entries_sailing_idx ON waitlist_entries (sailing_id, created_at);\n", expect="ok")
    s.edit("migrations/0007_refund_reason.down.sql", REFUND_REASON_DOWN, "DROP TABLE waitlist_entries;\n" + REFUND_REASON_DOWN, expect="ok")
    s.say("Added `waitlist_entries` to 0007 (up and down). Next, the queries.", final=True)
    s.human("nope, 0007 went out with last week's release, it's applied in prod. new file. (honestly we should write this rule down somewhere)")
    s.edit("migrations/0007_refund_reason.up.sql", s.fs[P("migrations/0007_refund_reason.up.sql")], REFUND_REASON_UP, expect="ok")
    s.edit("migrations/0007_refund_reason.down.sql", "DROP TABLE waitlist_entries;\n" + REFUND_REASON_DOWN, REFUND_REASON_DOWN, expect="ok")
    s.write("migrations/0008_waitlist.up.sql",
            "CREATE TABLE waitlist_entries (\n    id          bigserial PRIMARY KEY,\n    sailing_id  bigint NOT NULL REFERENCES sailings(id),\n"
            "    email       text NOT NULL,\n    seats       integer NOT NULL,\n    created_at  timestamptz NOT NULL DEFAULT now()\n);\n"
            "CREATE INDEX waitlist_entries_sailing_idx ON waitlist_entries (sailing_id, created_at);\n")
    s.write("migrations/0008_waitlist.down.sql", "DROP TABLE waitlist_entries;\n")
    s.write("db/query/waitlist.sql",
            "-- name: AddWaitlistEntry :one\nINSERT INTO waitlist_entries (sailing_id, email, seats) VALUES ($1, $2, $3) RETURNING id;\n\n"
            "-- name: ListWaitlist :many\nSELECT id, sailing_id, email, seats, created_at FROM waitlist_entries WHERE sailing_id = $1 ORDER BY created_at;\n")
    s.bash("make generate && git status --short",
           "sqlc generate\n M internal/db/models.go\n?? db/query/waitlist.sql\n?? internal/db/waitlist.sql.go\n?? migrations/0008_waitlist.down.sql\n?? migrations/0008_waitlist.up.sql",
           desc="Regenerate sqlc code and show status")
    s.bash("go build ./...", desc="Build all packages")
    s.say("0007 is back the way it was. The table is in a new `0008_waitlist` migration (up/down), `db/query/waitlist.sql` has `AddWaitlistEntry` and `ListWaitlist`, and sqlc is regenerated. It builds.", final=True)
    s.human("cool. unrelated but Priya just lost an hour because she set FD_DATABASE_URL like the README says and it kept connecting to the default db. is the readme just wrong?")
    s.grep("FD_|FERRYDESK_", content_out=
           CWD + "/README.md:19:| `FD_PORT`           | `8080`                                                         | HTTP listen port          |\n"
           + CWD + "/README.md:20:| `FD_DATABASE_URL`   | `postgres://ferry:ferry@localhost:5432/ferrydesk?sslmode=disable` |                        |\n"
           + CWD + "/README.md:21:| `FD_LOG_LEVEL`      | `info`                                                         | debug, info, warn, error  |\n"
           + CWD + "/README.md:22:| `FD_HOLD_TTL`       | `15m`                                                          | how long a seat hold lasts |\n"
           + CWD + "/internal/config/config.go:12:const envPrefix = \"FERRYDESK_\"\n"
           + CWD + "/internal/store/postgres_test.go:14:\turl := os.Getenv(\"FERRYDESK_DATABASE_URL\")\n"
           + CWD + "/Makefile:30:\tmigrate -path migrations -database \"$$FERRYDESK_DATABASE_URL\" up\n"
           + CWD + "/.github/workflows/ci.yml:17:      FERRYDESK_DATABASE_URL: postgres://ferry:ferry@localhost:5432/ferrydesk?sslmode=disable",
           mode="content")
    s.say("Yes. The code, the Makefile and CI all use `FERRYDESK_*` (`envPrefix` in `internal/config/config.go`); only the README table says `FD_*`. An unknown `FD_` variable is silently ignored and the default DSN is used, which matches what Priya saw. Want me to fix the README table?", final=True)
    s.human("yes please, fix it")
    s.edit("README.md", "| `FD_PORT`           |", "| `FERRYDESK_PORT`           |", expect="ok")
    s.edit("README.md", "| `FD_DATABASE_URL`   |", "| `FERRYDESK_DATABASE_URL`   |", expect="ok")
    s.edit("README.md", "| `FD_LOG_LEVEL`      |", "| `FERRYDESK_LOG_LEVEL`      |", expect="ok")
    s.edit("README.md", "| `FD_HOLD_TTL`       |", "| `FERRYDESK_HOLD_TTL`       |", expect="ok")
    s.say("README now lists `FERRYDESK_PORT`, `FERRYDESK_DATABASE_URL`, `FERRYDESK_LOG_LEVEL` and `FERRYDESK_HOLD_TTL`.", final=True)
    return s


def s11():
    s = Session("s11", "2026-11-09T14:22:10.000", "feat/fare-preview", extra_files={"internal/pricing/fare.go": FARE_CHILDREN})
    s.human("can you add GET /fares/preview?route=WLG-PCN&adults=2&children=1 that returns the total for that party. no auth, it's for the marketing site's price widget")
    s.read("internal/pricing/fare.go")
    s.read("internal/api/router.go")
    s.grep("AdultFareCents|adult_fare_cents", files=["internal/api/handlers.go", "internal/db/models.go", "internal/db/queries.sql.go", "internal/store/postgres.go", "db/query/sailings.sql"])
    s.say("handlers.go is already very long, so I'll put this in a new `fares.go`. The route's adult fare comes from the next sailing on that route.")
    s.write("internal/api/fares.go",
            "package api\n\nimport (\n\t\"net/http\"\n\t\"strconv\"\n\t\"time\"\n\n\t\"github.com/tidewater-labs/ferrydesk/internal/pricing\"\n)\n\n"
            "type farePreviewResponse struct {\n\tRoute string  `json:\"route\"`\n\tTotal float64 `json:\"total\"`\n}\n\n"
            "func (h *Handlers) farePreview(w http.ResponseWriter, r *http.Request) {\n\tq := r.URL.Query()\n\troute := q.Get(\"route\")\n"
            "\tadults, err1 := strconv.Atoi(q.Get(\"adults\"))\n\tchildren, err2 := strconv.Atoi(q.Get(\"children\"))\n"
            "\tif route == \"\" || err1 != nil || err2 != nil || adults < 0 || children < 0 || adults+children == 0 {\n"
            "\t\twriteJSON(w, http.StatusBadRequest, map[string]string{\"error\": \"route, adults and children are required\"})\n\t\treturn\n\t}\n"
            "\tsls, err := h.svc.Sailings(r.Context(), route, time.Now(), time.Now().Add(30*24*time.Hour))\n"
            "\tif err != nil || len(sls) == 0 {\n\t\twriteJSON(w, http.StatusNotFound, map[string]string{\"error\": \"no upcoming sailings\"})\n\t\treturn\n\t}\n"
            "\ttotal := pricing.Fare(pricing.Route{Code: route, AdultFare: sls[0].AdultFare}, pricing.Passengers{Adults: adults, Children: children})\n"
            "\twriteJSON(w, http.StatusOK, farePreviewResponse{Route: route, Total: float64(total) / 100})\n}\n")
    s.edit("internal/api/router.go", "\tr.Get(\"/healthz\", h.health)\n", "\tr.Get(\"/healthz\", h.health)\n\tr.Get(\"/fares/preview\", h.farePreview)\n", expect="ok")
    s.bash("go build ./...", desc="Build all packages")
    s.say("Added `GET /fares/preview`. It returns `{\"route\": ..., \"total\": 186.25}` in dollars, so the widget can show it directly.", final=True)
    s.human("hmm, why dollars? every other endpoint returns cents as an int, total_cents. the widget formats it. no float conversion")
    s.edit("internal/api/fares.go",
           "type farePreviewResponse struct {\n\tRoute string  `json:\"route\"`\n\tTotal float64 `json:\"total\"`\n}",
           "type farePreviewResponse struct {\n\tRoute      string `json:\"route\"`\n\tTotalCents int64  `json:\"total_cents\"`\n}", expect="ok")
    s.edit("internal/api/fares.go",
           "farePreviewResponse{Route: route, Total: float64(total) / 100})",
           "farePreviewResponse{Route: route, TotalCents: int64(total)})", expect="ok")
    s.say("Switched to `total_cents` (int64). Let me run it and curl the endpoint.")
    s.bash_timeout("make run", "go run ./cmd/ferrydesk\n" + server_log(s), desc="Run the API server")
    s.say("The server blocked until the 2 minute timeout. I'll run it again with a longer timeout and then curl it")
    s.interrupt_marker()
    s.human("no, stop running the server in the foreground, it never exits. write an httptest test for the endpoint instead")
    s.write("internal/api/fares_test.go",
            "package api\n\nimport (\n\t\"encoding/json\"\n\t\"net/http\"\n\t\"net/http/httptest\"\n\t\"testing\"\n)\n\n"
            "func TestFarePreviewRejectsMissingParams(t *testing.T) {\n\th := &Handlers{}\n\trec := httptest.NewRecorder()\n"
            "\th.farePreview(rec, httptest.NewRequest(http.MethodGet, \"/fares/preview?route=WLG-PCN\", nil))\n"
            "\tif rec.Code != http.StatusBadRequest {\n\t\tt.Fatalf(\"code = %d\", rec.Code)\n\t}\n"
            "\tvar body map[string]string\n\t_ = json.NewDecoder(rec.Body).Decode(&body)\n\tif body[\"error\"] == \"\" {\n\t\tt.Fatal(\"want error message\")\n\t}\n}\n")
    s.bash("go test ./...", gotest_all(api="0.020s"), code=1, desc="Run test suite", run_secs=9.0)
    s.bash("go test ./internal/api/ -run FarePreview -v",
           "=== RUN   TestFarePreviewRejectsMissingParams\n--- PASS: TestFarePreviewRejectsMissingParams (0.00s)\nPASS\n"
           f"ok  \t{MOD}/internal/api\t0.014s", desc="Run fare preview test")
    s.say("Added `internal/api/fares_test.go` (httptest, no server). The endpoint returns `{\"route\", \"total_cents\"}`. API tests pass; the only failure in `go test ./...` is `internal/store`, which needs a live Postgres.", final=True)
    s.human("👍")
    return s


def s12():
    extra = {"migrations/0007_refund_reason.up.sql": REFUND_REASON_UP, "migrations/0007_refund_reason.down.sql": REFUND_REASON_DOWN,
             "migrations/0008_waitlist.up.sql": "CREATE TABLE waitlist_entries (\n    id          bigserial PRIMARY KEY,\n    sailing_id  bigint NOT NULL REFERENCES sailings(id),\n"
             "    email       text NOT NULL,\n    seats       integer NOT NULL,\n    created_at  timestamptz NOT NULL DEFAULT now()\n);\n"
             "CREATE INDEX waitlist_entries_sailing_idx ON waitlist_entries (sailing_id, created_at);\n",
             "migrations/0008_waitlist.down.sql": "DROP TABLE waitlist_entries;\n"}
    s = Session("s12", "2026-11-13T09:48:36.000", "chore/seed-refresh", extra_files=extra)
    s.human("scripts/seed.sql is way behind the schema. there's refunds, waitlist, booking references, vessels on routes... can you bring it up to date with a handful of realistic rows? NZ routes")
    s.say("I'll have a subagent pull the current schema together from the migrations while I look at the seed file.")

    def sub(a):
        a.glob("migrations/*.up.sql", sorted(k[len(CWD) + 1:] for k in a.fs if "/migrations/" in k and k.endswith(".up.sql")))
        for f in ["0001_init.up.sql", "0002_bookings.up.sql", "0003_holds.up.sql", "0004_add_refunds.up.sql", "0005_route_vessel.up.sql",
                  "0006_booking_reference.up.sql", "0007_refund_reason.up.sql", "0008_waitlist.up.sql"]:
            a.read("migrations/" + f)
    report = ("Current schema after migrations 0001-0008:\n\n"
              "- routes(code text PK, name text, adult_fare_cents bigint, vessel text NULL)\n"
              "- sailings(id bigserial, route_code -> routes.code, departs_at timestamptz, capacity int, adult_fare_cents bigint)\n"
              "- bookings(id bigserial, sailing_id -> sailings.id, seats int, status text, total_cents bigint, created_at timestamptz default now(), reference text UNIQUE NULL)\n"
              "- holds(id, sailing_id, seats, created_at, expires_at NOT NULL)\n"
              "- refunds(id, booking_id -> bookings.id, amount_cents bigint, created_at, reason text NULL)\n"
              "- waitlist_entries(id, sailing_id, email text, seats int, created_at)\n\n"
              "Status values used in code: held, confirmed, cancelled.")
    s.task("Summarize schema from migrations", "Read every up migration under /work/ferrydesk/migrations and summarize the resulting schema: tables, columns, types, nullability, FKs. Do not modify anything.",
           "general-purpose", report, sub)
    s.read("scripts/seed.sql")
    s.write("scripts/seed.sql",
            "-- Local development seed. Amounts are NZD cents.\n"
            "INSERT INTO routes (code, name, adult_fare_cents, vessel) VALUES\n"
            "  ('WLG-PCN', 'Wellington - Picton', 7450, 'Aratere'),\n  ('AKL-WHK', 'Auckland - Waiheke', 4200, 'Kea'),\n  ('BLU-OBN', 'Bluff - Oban', 9900, 'Island Lynx');\n\n"
            "INSERT INTO sailings (route_code, departs_at, capacity, adult_fare_cents) VALUES\n"
            "  ('WLG-PCN', '2026-12-01 08:45+13', 420, 7450),\n  ('WLG-PCN', '2026-12-01 14:00+13', 420, 7450),\n"
            "  ('AKL-WHK', '2026-12-01 09:00+13', 180, 4200),\n  ('BLU-OBN', '2026-12-02 09:30+13', 90, 9900);\n\n"
            "INSERT INTO bookings (sailing_id, seats, status, total_cents, reference) VALUES\n"
            "  (1, 2, 'confirmed', 14900, 'FD-7Q2KX'),\n  (1, 3, 'confirmed', 18625, 'FD-9MBW4'),\n"
            "  (3, 1, 'cancelled', 4200, 'FD-3TTJ8'),\n  (4, 2, 'confirmed', 19800, 'FD-KD6PA');\n\n"
            "INSERT INTO refunds (booking_id, amount_cents, reason) VALUES\n  (3, 4200, 'customer cancelled > 24h before departure');\n\n"
            "INSERT INTO holds (sailing_id, seats, expires_at) VALUES\n  (2, 4, now() + interval '15 minutes');\n\n"
            "INSERT INTO waitlist_entries (sailing_id, email, seats) VALUES\n  (4, 'aroha@example.co.nz', 2),\n  (4, 'sam.t@example.com', 1);\n")
    s.say("Let me load it into a database to check it applies cleanly.")
    s.reject("Bash", {"command": "./scripts/reset-db.sh", "description": "Rebuild local DB and load seed"})
    s.human("not that script!! it does docker compose down -v and I lose everything. make a throwaway db (createdb ferrydesk_seedcheck or whatever) and load migrations + seed into that")
    s.bash("createdb -h localhost -U ferry ferrydesk_seedcheck", desc="Create throwaway database")
    s.bash("migrate -path migrations -database 'postgres://ferry:ferry@localhost:5432/ferrydesk_seedcheck?sslmode=disable' up",
           "\n".join(f"{i}/u {n} ({t}ms)" for i, n, t in [(1, "init", "9.1"), (2, "bookings", "6.3"), (3, "holds", "4.0"), (4, "add_refunds", "3.8"),
                                                           (5, "route_vessel", "2.2"), (6, "booking_reference", "3.0"), (7, "refund_reason", "1.9"), (8, "waitlist", "4.4")]),
           desc="Apply migrations to throwaway database")
    s.bash("psql 'postgres://ferry:ferry@localhost:5432/ferrydesk_seedcheck?sslmode=disable' -v ON_ERROR_STOP=1 -f scripts/seed.sql",
           "INSERT 0 3\nINSERT 0 4\nINSERT 0 4\nINSERT 0 1\nINSERT 0 1\nINSERT 0 2", desc="Load seed into throwaway database")
    s.bash("dropdb -h localhost -U ferry ferrydesk_seedcheck", desc="Drop throwaway database")
    s.say("The seed loads cleanly on a fresh database with all 8 migrations. Committing and pushing the branch.")
    s.reject("Bash", {"command": "git add scripts/seed.sql && git commit -m \"seed: cover refunds, waitlist, references, vessels\" && git push -u origin chore/seed-refresh", "description": "Commit and push seed update"},
             reason="commit is fine but don't push, I push my own branches")
    s.bash("git add scripts/seed.sql && git commit -m \"seed: cover refunds, waitlist, references, vessels\"",
           "[chore/seed-refresh 4e1a9c2] seed: cover refunds, waitlist, references, vessels\n 1 file changed, 27 insertions(+), 3 deletions(-)", desc="Commit seed update")
    s.say("Committed locally (`4e1a9c2`), not pushed. `scripts/seed.sql` now covers routes with vessels, 4 sailings, bookings with references, a refund with a reason, a live hold and 2 waitlist entries. I checked it against a throwaway `ferrydesk_seedcheck` database and then dropped it; your local DB wasn't touched.", final=True)
    return s


def s13():
    s = Session("s13", "2026-11-17T16:05:02.000", "fix/debug-logging")
    s.human("debug logging doesn't do anything. I set FD_LOG_LEVEL=debug in my .env and I still only get the listening line, no request logs")
    s.grep("LOG_LEVEL|SlogLevel", files=["internal/config/config.go", "cmd/ferrydesk/main.go", "README.md", ".env.example"])
    s.read("internal/config/config.go")
    s.grep("FD_", files=["README.md"], path=CWD)
    s.say("Same cause as the port problem before: the config loader only reads `FERRYDESK_`-prefixed variables (`envPrefix` in `internal/config/config.go`), so `FD_LOG_LEVEL` is ignored and the level stays at `info`. The request logs in `accessLog` are emitted at debug. Setting `FERRYDESK_LOG_LEVEL=debug` in your `.env` will turn them on. The README still mentions `FD_` here, so I'll check what else does.", final=True)
    s.human("ugh, that prefix again. ok I'll fix my .env")
    s.say("I'll also check whether anything else in the repo still says `FD_`.")
    s.interrupt_marker()
    s.human("actually park that, payments just pinged about refunds on prod. quick q: does Cancel refund the full total even if it's an hour before departure? I thought we had a 24h rule")
    ln = s.line_of("internal/booking/service.go", "func (s *Service) Cancel")
    s.read("internal/booking/service.go", offset=ln, limit=12)
    s.grep("24|cutoff|Cutoff|DepartsAt", files=["internal/booking/service.go", "internal/store/postgres.go", "internal/api/handlers.go"])
    s.say("There's no 24h rule in the code. `Service.Cancel` loads the booking, sets it to cancelled and calls `CreateRefund` with `b.Total`: the full amount, whatever the departure time. Nothing compares `DepartsAt` to now anywhere on the cancel path. If there's supposed to be a cutoff, it was never implemented here.", final=True)
    s.human("ok that's what I figured. that's a product call, I'll take it to them. thanks")
    return s


SESSIONS = [s01, s02, s03, s04, s05, s06, s07, s08, s09, s10, s11, s12, s13]


def main():
    if os.path.isdir(OUT):
        shutil.rmtree(OUT)
    os.makedirs(OUT)
    for fn in SESSIONS:
        s = fn()
        s.dump()
        print(s.key, s.sid, len(s.lines), "lines")


if __name__ == "__main__":
    main()
