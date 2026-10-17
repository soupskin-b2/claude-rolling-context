#!/usr/bin/env python3
"""Moving a desktop app Code session to another account (move_session.py).

  A. accounts: finds this session's record by its CLI id, lists every account
     with the org its sessions live in, its email where a .claude.json knows
     it, its nickname, and its latest session; a worktree session is blocked.
  B. prepare: backs up the record and transcript, writes the record into the
     target account rebound to it (its connectors, no Remote Control links,
     live), clears a delete marker left by an earlier move, and leaves the
     source, the transcript and the plugin's per-session state alone.
  C. finish: nothing happens while the session is live here; once the app has
     archived it, what the app wrote since is carried over and the source
     record is removed the way the app's delete does, transcript untouched.
  D. cancel takes the copy back, and a finisher then leaves everything.
  E. the real detached finisher completes a move by itself.
  F. sessions lists another account's sessions, each with why it cannot be
     imported (no transcript on this computer); import moves one into this
     session's account at once, backed up and removed from the other the way
     the app's delete does.
  G. a Store install that moved its data out of the package (app 2.42 on)
     is followed to Claude-Data/<package family> by the marker it leaves.

  python tests/test_move_session.py
"""
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = tempfile.mkdtemp(prefix="rolling-context-move-")
APP = os.path.join(ROOT, "app")
os.environ.update(HOME=ROOT, USERPROFILE=ROOT, ROLLING_CONTEXT_CLAUDE_APP_DIR=APP,
                  ROLLING_CONTEXT_MOVE_GRACE_S="1", ROLLING_CONTEXT_MOVE_SETTLE_S="0")
os.environ.pop("CLAUDE_CONFIG_DIR", None)
sys.path.insert(0, os.path.join(HERE, "..", "proxy"))

import move_session as m  # noqa: E402

failures = []

A, A_ORG = "aaaaaaaa-0000-0000-0000-000000000001", "0a0a0a0a-0000-0000-0000-000000000001"
B, B_ORG = "bbbbbbbb-0000-0000-0000-000000000002", "0b0b0b0b-0000-0000-0000-000000000002"
C, C_ORG = "cccccccc-0000-0000-0000-000000000003", "0c0c0c0c-0000-0000-0000-000000000003"
CLI = "11111111-2222-3333-4444-555555555555"
LOCAL = "local_99999999-8888-7777-6666-555555555555"
CWD = os.path.join(ROOT, "repo")


def check(cond, label, detail=""):
    print(f"    {'PASS' if cond else 'FAIL'}  {label}")
    if detail:
        print(f"          {detail}")
    if not cond:
        failures.append(label)


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(data if isinstance(data, str) else json.dumps(data))


def read(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def org_dir(account, org):
    return os.path.join(APP, "claude-code-sessions", account, org)


def record(**over):
    rec = {
        "sessionId": LOCAL, "cliSessionId": CLI, "cwd": CWD, "originCwd": CWD,
        "createdAt": 1, "lastActivityAt": 100, "lastFocusedAt": 100, "model": "claude-opus-5-5",
        "isArchived": False, "title": "The session", "permissionMode": "auto",
        "remoteMcpServersConfig": [{"name": "A's connector", "uuid": "a-conn"}],
        "bridgeSessionIds": ["session_A"], "remoteControlAutoEligible": True,
        "toolSurfaceSnapshot": {"x": 1}, "promptAppendSnapshot": {"append": "A"},
        "steeredByRemoteClient": False, "alwaysAllowedReasons": [], "sessionPermissionUpdates": [],
    }
    rec.update(over)
    return rec


def world():
    shutil.rmtree(ROOT, ignore_errors=True)
    os.makedirs(CWD)
    # The app's cross product of accounts and orgs: every account has a
    # scheduled-tasks.json under every org, and one org holds its sessions.
    for acct in (A, B, C):
        for org in (A_ORG, B_ORG, C_ORG):
            write(os.path.join(org_dir(acct, org), "scheduled-tasks.json"), "[]")
    write(os.path.join(org_dir(A, A_ORG), f"{LOCAL}.json"), record())
    write(os.path.join(org_dir(A, A_ORG), "local_other-a.json"),
          {"sessionId": "local_other-a", "cliSessionId": "x", "cwd": CWD, "isArchived": False,
           "title": "Another A session", "lastActivityAt": 50, "remoteMcpServersConfig": []})
    write(os.path.join(org_dir(B, B_ORG), "local_b-latest.json"),
          {"sessionId": "local_b-latest", "cliSessionId": "y", "cwd": CWD, "isArchived": False,
           "title": "B's latest", "lastActivityAt": 300,
           "remoteMcpServersConfig": [{"name": "B's connector", "uuid": "b-conn"}]})
    write(os.path.join(org_dir(B, B_ORG), "local_b-old.json"),
          {"sessionId": "local_b-old", "cliSessionId": "z", "cwd": CWD, "isArchived": False,
           "title": "B's old", "lastActivityAt": 200, "remoteMcpServersConfig": []})
    write(os.path.join(org_dir(C, C_ORG), "local_c-arch.json"),
          {"sessionId": "local_c-arch", "cliSessionId": "w", "cwd": CWD, "isArchived": True,
           "title": "C archived", "lastActivityAt": 10, "remoteMcpServersConfig": []})
    write(os.path.join(org_dir(C, C_ORG), "archived-sessions.idx"), {"v": 1, "archived": ["local_c-arch"]})
    # The transcript, the plugin's switch for this session, a .claude.json.
    write(os.path.join(ROOT, ".claude", "projects", "repo-slug", f"{CLI}.jsonl"), '{"type":"user"}\n')
    write(os.path.join(ROOT, ".claude-rolling-context", "sessions", f"{CLI}.json"), {"disabled": True})
    write(os.path.join(ROOT, ".claude.json"),
          {"oauthAccount": {"accountUuid": B, "emailAddress": "b@example.com", "organizationUuid": B_ORG}})
    os.environ["ROLLING_CONTEXT_MOVE_NO_SPAWN"] = "1"


def by_account(info):
    return {a["account"]: a for a in info["accounts"]}


# Tests A-D drive finish in this process; E lets prepare spawn it for real.
_spawn = m.spawn_finisher
m.spawn_finisher = lambda cli, pid: (_spawn(cli, pid) if not os.environ.get("ROLLING_CONTEXT_MOVE_NO_SPAWN")
                                     else None)


def test_accounts():
    print("A. accounts")
    world()
    m.name(C, "Old account")
    info = m.describe(CLI)
    s = info["session"]
    check(s and s["account"] == A and s["org"] == A_ORG and s["localId"] == LOCAL,
          "finds this session's record by its CLI id", str(s))
    check(s["kind"] == "folder" and s["transcript"].endswith(f"{CLI}.jsonl"), "kind and transcript")
    accts = by_account(info)
    check(set(accts) == {A, B, C}, "lists every account folder")
    check(accts[B]["org"] == B_ORG and accts[C]["org"] == C_ORG,
          "the org is the one holding the account's sessions, not a scheduled-tasks one")
    check(accts[B]["email"] == "b@example.com", "the email a .claude.json knows")
    check(accts[C]["name"] == "Old account", "the nickname")
    check(accts[B]["sessions"] == 2 and accts[B]["latestTitle"] == "B's latest", "B's latest live session")
    check(accts[C]["sessions"] == 0 and accts[C]["latestTitle"] == "C archived" and accts[C]["latestArchived"],
          "an account with only archived sessions is named by the latest of those")
    check(info["blocked"] is None and info["pending"] is None, "nothing blocks it")

    m.name(C, "")
    check(by_account(m.describe(CLI))[C]["name"] is None, "an empty name clears the nickname")

    write(os.path.join(org_dir(A, A_ORG), f"{LOCAL}.json"), record(worktreePath=os.path.join(ROOT, "wt")))
    info = m.describe(CLI)
    check(info["session"]["kind"] == "worktree" and "worktree" in (info["blocked"] or ""),
          "a worktree session is blocked (archiving it would delete the worktree)")
    out = m.prepare(CLI, B)
    check(not out["ok"] and not os.path.exists(os.path.join(org_dir(B, B_ORG), f"{LOCAL}.json")),
          "and prepare refuses it")

    info = m.describe("no-such-session")
    check(info["session"] is None and info["blocked"], "a session without a desktop record is blocked")


def test_prepare():
    print("B. prepare")
    world()
    write(os.path.join(org_dir(B, B_ORG), f"deleted_{LOCAL[len('local_'):]}"), "123")
    write(os.path.join(org_dir(B, B_ORG), "archived-sessions.idx"), {"v": 1, "archived": [LOCAL, "local_b-x"]})
    check(not m.prepare(CLI, A)["ok"], "refuses the account it is already in")
    check(not m.prepare(CLI, "nope")["ok"], "refuses an unknown account")
    out = m.prepare(CLI, B)
    check(out["ok"], "prepares the move", json.dumps(out)[:300])
    target = os.path.join(org_dir(B, B_ORG), f"{LOCAL}.json")
    rec = read(target)
    check(rec["cliSessionId"] == CLI and rec["sessionId"] == LOCAL and rec["cwd"] == CWD,
          "the copy keeps the CLI session id, record id and folder (transcript, scratchpad, compressions)")
    check(rec["remoteMcpServersConfig"] == [{"name": "B's connector", "uuid": "b-conn"}],
          "the copy takes the connectors of B's latest session")
    check(not any(k in rec for k in ("bridgeSessionIds", "toolSurfaceSnapshot", "promptAppendSnapshot")),
          "and drops what was bound to A")
    check(rec["isArchived"] is False and rec["remoteControlAutoEligible"] is False, "live, no Remote Control")
    check(rec["title"] == "The session" and rec["model"] == "claude-opus-5-5", "the rest carries over")
    check(not os.path.exists(os.path.join(org_dir(B, B_ORG), f"deleted_{LOCAL[len('local_'):]}")),
          "the delete marker from an earlier move away is cleared")
    check(read(os.path.join(org_dir(B, B_ORG), "archived-sessions.idx"))["archived"] == ["local_b-x"],
          "and the session is no longer listed archived there")
    check(os.path.exists(os.path.join(org_dir(A, A_ORG), f"{LOCAL}.json")), "the source stays until it is archived")
    check(os.path.exists(os.path.join(ROOT, ".claude", "projects", "repo-slug", f"{CLI}.jsonl")),
          "the transcript is untouched")
    check(read(os.path.join(ROOT, ".claude-rolling-context", "sessions", f"{CLI}.json")) == {"disabled": True},
          "the plugin's per-session switch is untouched")
    backup = out["pending"]["backup"]
    check(sorted(os.listdir(backup)) == sorted([f"{CLI}.jsonl", f"{LOCAL}.json"]),
          "the record and transcript are backed up", backup)
    check(not m.prepare(CLI, C)["ok"], "a second move waits for the first")
    info = m.describe(CLI)
    check(info["session"]["account"] == A and info["pending"]["to"]["account"] == B,
          "while pending, this session is still A's and the move is reported")


def test_finish():
    print("C. finish")
    world()
    m.prepare(CLI, B)
    src = os.path.join(org_dir(A, A_ORG), f"{LOCAL}.json")
    target = os.path.join(org_dir(B, B_ORG), f"{LOCAL}.json")
    out = m.finish(CLI, os.getpid(), poll_s=0.05, until=time.time() + 0.3)
    check(out["state"] == "expired" and os.path.exists(src) and os.path.exists(target),
          "nothing is removed while the session is live here", str(out))

    world()
    m.prepare(CLI, B)
    # The app archives it: its CLI stops (a pid that is not running) and the
    # record says archived, with the title it last wrote.
    write(src, record(isArchived=True, title="Renamed at the end", lastActivityAt=999))
    write(os.path.join(org_dir(A, A_ORG), "archived-sessions.idx"), {"v": 1, "archived": [LOCAL, "local_k"]})
    out = m.finish(CLI, 0, poll_s=0.05, until=time.time() + 5)
    check(out["state"] == "done", "finishes once the app has archived it", str(out))
    check(not os.path.exists(src), "the source record is removed")
    marker = os.path.join(org_dir(A, A_ORG), f"deleted_{LOCAL[len('local_'):]}")
    check(os.path.exists(marker), "with the app's delete marker")
    check(read(os.path.join(org_dir(A, A_ORG), "archived-sessions.idx"))["archived"] == ["local_k"],
          "and off A's archived list")
    rec = read(target)
    check(rec["title"] == "Renamed at the end" and rec["isArchived"] is False and rec["lastActivityAt"] == 999,
          "what the app wrote before archiving is carried over, live")
    check(os.path.exists(os.path.join(ROOT, ".claude", "projects", "repo-slug", f"{CLI}.jsonl")),
          "the transcript is untouched")
    info = m.describe(CLI)
    check(info["session"]["account"] == B and info["pending"] is None, "the session is now B's")

    world()
    m.prepare(CLI, B)
    os.utime(target, (time.time() + 50, time.time() + 50))   # the app in B wrote it since
    write(src, record(isArchived=True, title="Stale"))
    m.finish(CLI, 0, poll_s=0.05, until=time.time() + 5)
    check(read(target)["title"] == "The session", "a copy the app has written since is left as it is")

    world()
    m.prepare(CLI, B)
    write(src, record(isArchived=True))
    out = m.finish(CLI, os.getpid(), poll_s=0.05, until=time.time() + 5)
    check(out["state"] == "done", "archived while the waited-on pid lives on: finishes after the grace")

    world()
    m.prepare(CLI, B)
    os.remove(src)
    out = m.finish(CLI, 0, poll_s=0.05, until=time.time() + 5)
    check(out["state"] == "source-gone" and os.path.exists(target),
          "a source that went away by itself stops the finisher and keeps the copy")


def test_cancel():
    print("D. cancel")
    world()
    check(not m.cancel(CLI)["ok"], "nothing to cancel")
    m.prepare(CLI, B)
    target = os.path.join(org_dir(B, B_ORG), f"{LOCAL}.json")
    check(m.cancel(CLI)["ok"] and not os.path.exists(target), "cancel removes the copy")
    src = os.path.join(org_dir(A, A_ORG), f"{LOCAL}.json")
    write(src, record(isArchived=True))
    m.finish(CLI, 0, poll_s=0.05, until=time.time() + 1)
    check(os.path.exists(src), "a finisher of a cancelled move leaves the source alone")
    check(m.describe(CLI)["pending"] is None, "and the move is no longer reported")


def test_spawned():
    print("E. the detached finisher")
    world()
    os.environ.pop("ROLLING_CONTEXT_MOVE_NO_SPAWN")
    # Through the CLI, as the mod runs it: its parent is this process.
    r = subprocess.run([sys.executable, os.path.join(HERE, "..", "proxy", "move_session.py"),
                        "prepare", "--session", CLI, "--to", B],
                       capture_output=True, text=True, timeout=30, env=os.environ)
    out = json.loads(r.stdout or "{}")
    check(out.get("ok") and out["pending"].get("finisherPid"), "prepare starts a finisher", r.stdout + r.stderr)
    src = os.path.join(org_dir(A, A_ORG), f"{LOCAL}.json")
    write(src, record(isArchived=True))
    deadline = time.time() + 20
    while time.time() < deadline and os.path.exists(src):
        time.sleep(0.2)
    check(not os.path.exists(src) and m.describe(CLI)["session"]["account"] == B,
          "it completes the move by itself once the session is archived")


def test_import():
    print("F. import from another account")
    world()
    b_jsonl = os.path.join(ROOT, ".claude", "projects", "repo-slug", "y.jsonl")
    write(b_jsonl, '{"type":"user"}\n')
    write(os.path.join(org_dir(B, B_ORG), "archived-sessions.idx"), {"v": 1, "archived": ["local_b-old"]})
    write(os.path.join(org_dir(B, B_ORG), "local_b-old.json"),
          {"sessionId": "local_b-old", "cliSessionId": "z", "cwd": CWD, "isArchived": True,
           "title": "B's old", "lastActivityAt": 200, "remoteMcpServersConfig": []})

    listed = m.list_sessions(B)["sessions"]
    check([s["localId"] for s in listed] == ["local_b-latest", "local_b-old"], "live sessions first, newest first")
    latest = listed[0]
    check(latest["title"] == "B's latest" and latest["cliSessionId"] == "y" and latest["kind"] == "folder"
          and latest["archived"] is False and latest["blocked"] is None, "a still session can be imported", str(latest))
    check(listed[1]["archived"] is True and "transcript is not on this computer" in (listed[1]["blocked"] or ""),
          "an archived one is marked, and one without a transcript is blocked")

    out = m.import_session(CLI, B, "local_b-old")
    check(not out["ok"] and os.path.exists(os.path.join(org_dir(B, B_ORG), "local_b-old.json")),
          "import refuses one without a transcript, leaving it where it is")
    check(not m.import_session(CLI, A, "local_other-a")["ok"], "refuses a session already in this account")
    check(not m.import_session(CLI, B, "local_nope")["ok"], "refuses a session that is not there")
    write(os.path.join(org_dir(A, A_ORG), "local_dup.json"), {"sessionId": "local_dup", "cliSessionId": "y"})
    check(not m.import_session(CLI, B, "local_b-latest")["ok"], "refuses one this account already has")
    os.remove(os.path.join(org_dir(A, A_ORG), "local_dup.json"))

    out = m.import_session(CLI, B, "local_b-latest")
    check(out["ok"], "imports it", json.dumps(out)[:300])
    target = os.path.join(org_dir(A, A_ORG), "local_y.json")
    rec = read(target)
    check(rec["sessionId"] == "local_y" and rec["cliSessionId"] == "y" and rec["title"] == "B's latest"
          and rec["isArchived"] is False,
          "it lands in this session's account and org as local_<cli id>, the id the app adopts it under")
    check(out["imported"]["opened"] is False, "the app's resume link is opened only when asked")
    check(rec["remoteMcpServersConfig"] == [{"name": "A's connector", "uuid": "a-conn"}],
          "with this account's connectors")
    check(not os.path.exists(os.path.join(org_dir(B, B_ORG), "local_b-latest.json"))
          and os.path.exists(os.path.join(org_dir(B, B_ORG), "deleted_b-latest")),
          "and leaves B the way the app's delete does")
    check(os.path.exists(b_jsonl), "the transcript is untouched")
    check(sorted(os.listdir(out["imported"]["backup"])) == ["local_b-latest.json", "y.jsonl"],
          "the record and transcript are backed up")
    check(m.pending("y") is None and read(m.state_path("y"))["state"] == "imported", "recorded as imported")
    check([s["localId"] for s in m.list_sessions(B)["sessions"]] == ["local_b-old"], "B no longer lists it")

    # An archived session comes in live, and off the archived list it left.
    write(os.path.join(ROOT, ".claude", "projects", "repo-slug", "z.jsonl"), "{}\n")
    check(m.import_session(CLI, B, "local_b-old")["ok"], "an archived session imports")
    check(read(os.path.join(org_dir(A, A_ORG), "local_z.json"))["isArchived"] is False, "live")
    check(read(os.path.join(org_dir(B, B_ORG), "archived-sessions.idx"))["archived"] == [],
          "and off B's archived list")

    # Through the CLI, as the mod runs it.
    r = subprocess.run([sys.executable, os.path.join(HERE, "..", "proxy", "move_session.py"),
                        "sessions", "--account", C], capture_output=True, text=True, timeout=30, env=os.environ)
    out = json.loads(r.stdout or "{}")
    check(out.get("ok") and [s["localId"] for s in out["sessions"]] == ["local_c-arch"],
          "the sessions command answers in one JSON object", r.stdout + r.stderr)


def test_data_dir():
    print("G. the app's data folder")
    roaming = os.path.join(ROOT, "Roaming", "Claude")
    local = os.path.join(ROOT, "Local")
    os.makedirs(roaming)
    saved = {k: os.environ.get(k) for k in ("LOCALAPPDATA",)}
    os.environ["LOCALAPPDATA"] = local
    try:
        check(m.moved_app_dir(roaming) is None, "the app's own folder while it has not moved")
        open(os.path.join(roaming, ".user-data-left.claude_pzs8sxrjxfjjc"), "w").close()
        check(m.moved_app_dir(roaming) is None, "not a moved folder that is not there")
        moved = os.path.join(local, "Claude-Data", "claude_pzs8sxrjxfjjc")
        os.makedirs(moved)
        check(m.moved_app_dir(roaming) == moved,
              "a Store install's data, moved out of the package, in Claude-Data/<package family>")
        os.remove(os.path.join(roaming, ".user-data-left.claude_pzs8sxrjxfjjc"))
        open(os.path.join(roaming, ".user-data-back.claude_pzs8sxrjxfjjc"), "w").close()
        check(m.moved_app_dir(roaming) is None, "back in its own folder once it moved back")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


if __name__ == "__main__":
    try:
        test_accounts()
        test_prepare()
        test_finish()
        test_cancel()
        test_import()
        test_spawned()
        test_data_dir()
    finally:
        time.sleep(0.5)
        shutil.rmtree(ROOT, ignore_errors=True)
    print(f"\n{'ALL PASSED' if not failures else f'{len(failures)} FAILED: ' + ', '.join(failures)}")
    sys.exit(1 if failures else 0)
