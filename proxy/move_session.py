#!/usr/bin/env python3
"""Move a Claude desktop app Code session to another account on this machine.

The desktop app files each Code session under the account and organization it
was made in:

  <app dir>/claude-code-sessions/<account uuid>/<org uuid>/local_<id>.json

That record is all that ties a session to an account. Everything else a
session has lives in machine-wide places every account on this machine reads:

  ~/.claude/projects/<cwd slug>/<cli session id>.jsonl   the transcript (and a
                                                          folder beside it)
  <temp>/claude/<cwd slug>/<cli session id>/scratchpad   the scratchpad
  ~/.claude-rolling-context/sessions/<cli session id>    this plugin's on/off
  the proxy's compression store                          content-addressed

So a move keeps the CLI session id and the transcript exactly as they are --
the scratchpad, the per-session switch and every stored compression match it
the moment it resumes under the other account -- and moves only the record.
A scratch-workspace session keeps its folder where it is (the app keeps those
folders), and the record still points at it.

Two processes must never write one transcript, so the move finishes only once
the app has archived the session in the account it leaves (which stops its
CLI). `prepare` copies the record into the target account and starts a
detached `finish` that waits for that archive, then removes the source record
the way the app's own delete does (record gone, a deleted_<id> marker), without
touching the transcript -- the app's Delete would remove it, and the moved
session needs it. Until then `cancel` takes the copy back.

`import` is the same move the other way: a session of another account comes
into the account this session is in. Only one account is signed in at a
time and only its sessions run, so nothing of the other account's can be
running: the record moves at once, backed up, copied in, and removed from the
account it leaves the way `finish` removes it. It comes in as local_<cli id>,
the id the app gives a session it adopts; with --open the app's resume link
then lists it at once, and without the link it is listed after a restart.

  python move_session.py accounts --session ID
  python move_session.py prepare  --session ID --to ACCOUNT
  python move_session.py cancel   --session ID
  python move_session.py name     --account ACCOUNT [--name TEXT]
  python move_session.py sessions --account ACCOUNT
  python move_session.py import   --session ID --from ACCOUNT --local LOCAL_ID [--open]
  python move_session.py finish   --session ID [--wait-pid PID]   (internal)

Every command prints one JSON object. ROLLING_CONTEXT_CLAUDE_APP_DIR overrides
the app's data folder (tests).
"""
import glob
import json
import os
import shutil
import subprocess
import sys
import time

BACKUPS_KEPT = 10
FINISH_FOR_S = 24 * 3600     # a finisher waits this long for the archive
# Archived but the CLI seems alive (the pid is not the CLI's): finish anyway.
ARCHIVED_GRACE_S = float(os.environ.get("ROLLING_CONTEXT_MOVE_GRACE_S") or 20)
# Let the app finish writing the archived record.
SETTLE_S = float(os.environ.get("ROLLING_CONTEXT_MOVE_SETTLE_S") or 3)

# Record fields that belong to the account the session leaves: its Remote
# Control bridge, its tabs and side-session links, and the snapshots of its
# tools and system prompt (the app takes fresh ones on the next start).
ACCOUNT_BOUND = (
    "bridgeSessionIds", "remoteControlUserEnabled", "remoteControlSpawn",
    "spawnedFrom", "spawnedFromEndNotified", "peerMessaged", "peerReceipts",
    "chromeTabGroupId", "terminalClaudeTabOrdinal",
    "toolSurfaceSnapshot", "promptAppendSnapshot",
)


# ---- where things are ------------------------------------------------------

def app_dir():
    env = os.environ.get("ROLLING_CONTEXT_CLAUDE_APP_DIR")
    if env:
        return env
    if sys.platform == "win32":
        return os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~/AppData/Roaming"), "Claude")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/Claude")
    return os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "Claude")


def sessions_root():
    return os.path.join(app_dir(), "claude-code-sessions")


def home():
    return os.environ.get("USERPROFILE") if sys.platform == "win32" and os.environ.get("USERPROFILE") \
        else os.path.expanduser("~")


def claude_dir():
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(home(), ".claude")


def moves_dir():
    return os.path.join(claude_dir(), "rolling-context-moves")


def state_path(cli_id):
    return os.path.join(moves_dir(), f"{cli_id}.json")


def names_path():
    return os.path.join(claude_dir(), "rolling-context-accounts.json")


def logins_dir():
    """Where switch_account.py keeps each account's saved app login."""
    return os.path.join(claude_dir(), "rolling-context-logins")


def has_login(account):
    return os.path.isfile(os.path.join(logins_dir(), account, "login.json"))


def signed_in():
    """The account the desktop app is signed in to, as its config.json says."""
    return (read_json(os.path.join(app_dir(), "config.json"), {}) or {}).get("lastKnownAccountUuid")


def bare(local_id):
    return local_id[len("local_"):] if local_id.startswith("local_") else local_id


# ---- small file helpers ----------------------------------------------------

def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, data, indent=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=indent, ensure_ascii=False)
    os.replace(tmp, path)


def log(line):
    try:
        os.makedirs(moves_dir(), exist_ok=True)
        with open(os.path.join(moves_dir(), "moves.log"), "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
    except OSError:
        pass


# ---- the app's records -----------------------------------------------------

def records(account=None):
    """(account, org, path) for every session record, of one account or all."""
    pattern = os.path.join(sessions_root(), account or "*", "*", "local_*.json")
    for path in glob.glob(pattern):
        org_dir = os.path.dirname(path)
        yield os.path.basename(os.path.dirname(org_dir)), os.path.basename(org_dir), path


def archived_index(org_dir):
    return os.path.join(org_dir, "archived-sessions.idx")


def is_archived(record, org_dir):
    if record.get("isArchived"):
        return True
    idx = read_json(archived_index(org_dir), {}) or {}
    return record.get("sessionId") in (idx.get("archived") or [])


def drop_from_index(org_dir, local_id):
    path = archived_index(org_dir)
    idx = read_json(path)
    if isinstance(idx, dict) and local_id in (idx.get("archived") or []):
        idx["archived"] = [i for i in idx["archived"] if i != local_id]
        write_json(path, idx)


def find_session(cli_id, skip=None):
    """The record of the session whose CLI id is cli_id: (account, org, path,
    record), the most recently active when more than one has it; `skip` is a
    path to pass over (a pending move's copy)."""
    found = []
    for account, org, path in records():
        if skip and os.path.normcase(path) == os.path.normcase(skip):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except OSError:
            continue
        if cli_id not in text:
            continue
        try:
            rec = json.loads(text)
        except ValueError:
            continue
        if rec.get("cliSessionId") == cli_id or cli_id in (rec.get("priorCliSessionIds") or []):
            found.append((account, org, path, rec))
    if not found:
        return None
    return max(found, key=lambda f: (not f[3].get("isArchived"),
                                     f[3].get("lastFocusedAt") or 0, f[3].get("lastActivityAt") or 0))


def known_emails():
    """accountUuid -> (email, org uuid), from every .claude.json this machine
    keeps: the CLI's, its backups, and the app's agent-mode copies."""
    files = glob.glob(os.path.join(home(), ".claude.json*"))
    files += glob.glob(os.path.join(claude_dir(), "backups", "*"))
    files += glob.glob(os.path.join(app_dir(), "local-agent-mode-sessions", "*", "*", "*", ".claude", ".claude.json"))
    out = {}
    for path in files:
        acct = (read_json(path, {}) or {}).get("oauthAccount") if os.path.isfile(path) else None
        if isinstance(acct, dict) and acct.get("accountUuid"):
            out.setdefault(acct["accountUuid"], (acct.get("emailAddress"), acct.get("organizationUuid")))
    return out


def accounts():
    """Every account folder the app has, with the org its sessions live in."""
    emails = known_emails()
    names = read_json(names_path(), {}) or {}
    out = []
    for acct_dir in sorted(glob.glob(os.path.join(sessions_root(), "*"))):
        if not os.path.isdir(acct_dir):
            continue
        account = os.path.basename(acct_dir)
        email, known_org = emails.get(account, (None, None))
        # The app writes a scheduled-tasks.json under every org for every
        # account it has seen; the org that holds this account's sessions is
        # the one with records in it.
        orgs = []
        for org_dir in glob.glob(os.path.join(acct_dir, "*")):
            if os.path.isdir(org_dir):
                n = len(glob.glob(os.path.join(org_dir, "local_*.json")))
                orgs.append((n, os.path.exists(archived_index(org_dir)), os.path.basename(org_dir)))
        orgs.sort(reverse=True)
        org = orgs[0][2] if orgs and (orgs[0][0] or orgs[0][1]) else known_org
        # The latest live session names the account best; one that has only
        # archived sessions is named by its latest archived one.
        live, latest = 0, None
        for _, _, path in records(account):
            rec = read_json(path, {}) or {}
            archived = bool(rec.get("isArchived"))
            live += not archived
            key = (not archived, rec.get("lastActivityAt") or 0)
            if latest is None or key > latest[0]:
                latest = (key, rec.get("title") or "")
        out.append({
            "account": account,
            "org": org,
            "email": email,
            "name": names.get(account) or None,
            "sessions": live,
            "latestTitle": latest[1] if latest else None,
            "latestAt": latest[0][1] if latest else None,
            "latestArchived": bool(latest) and not latest[0][0],
            "login": has_login(account),
        })
    return out


def session_kind(rec):
    if rec.get("worktreePath"):
        return "worktree"
    scratch = os.path.normcase(os.path.join(app_dir(), "scratch-workspaces"))
    if os.path.normcase(rec.get("cwd") or "").startswith(scratch):
        return "scratch"
    return "folder"


def transcript(cli_id):
    hits = glob.glob(os.path.join(claude_dir(), "projects", "*", f"{cli_id}.jsonl"))
    return hits[0] if hits else None


def all_transcripts():
    """CLI session id -> transcript, for every transcript on this machine."""
    paths = glob.glob(os.path.join(claude_dir(), "projects", "*", "*.jsonl"))
    return {os.path.basename(p)[:-len(".jsonl")]: p for p in paths}


# ---- the move --------------------------------------------------------------

def rebind(rec, target_account):
    """The record as the target account should hold it."""
    out = {k: v for k, v in rec.items() if k not in ACCOUNT_BOUND}
    # The connectors are the account's: take the set its latest session has.
    latest = None
    for _, _, path in records(target_account):
        other = read_json(path, {}) or {}
        if other.get("sessionId") == rec.get("sessionId"):
            continue
        if latest is None or (other.get("lastActivityAt") or 0) > (latest.get("lastActivityAt") or 0):
            latest = other
    out["remoteMcpServersConfig"] = (latest or {}).get("remoteMcpServersConfig") or []
    out["isArchived"] = False
    out["remoteControlAutoEligible"] = False
    out["steeredByRemoteClient"] = False
    return out


def place(rec, account, org):
    """Write rec into account/org as a live session; its mtime, to tell later
    whether the app has written it since."""
    org_dir = os.path.join(sessions_root(), account, org)
    local_id = rec["sessionId"]
    path = os.path.join(org_dir, f"{local_id}.json")
    write_json(path, rec)
    # A session moved away and back again left the app's delete marker here.
    try:
        os.remove(os.path.join(org_dir, f"deleted_{bare(local_id)}"))
    except OSError:
        pass
    drop_from_index(org_dir, local_id)
    return path, os.path.getmtime(path)


def backup(cli_id, src_path):
    """The record and transcript as they were, kept for the newest moves."""
    dest = os.path.join(moves_dir(), "backups", f"{cli_id}-{int(time.time())}")
    os.makedirs(dest, exist_ok=True)
    shutil.copy2(src_path, dest)
    jsonl = transcript(cli_id)
    if jsonl:
        shutil.copy2(jsonl, dest)
    kept = sorted(glob.glob(os.path.join(moves_dir(), "backups", "*")), key=os.path.getmtime)
    for old in kept[:-BACKUPS_KEPT]:
        shutil.rmtree(old, ignore_errors=True)
    return dest


def spawn_finisher(cli_id, wait_pid):
    """`finish`, detached so it outlives this session's CLI."""
    argv = [sys.executable, os.path.abspath(__file__), "finish", "--session", cli_id,
            "--wait-pid", str(wait_pid)]
    kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
              close_fds=True, cwd=os.path.dirname(os.path.abspath(__file__)))
    if sys.platform == "win32":
        flags = 0x00000008 | 0x00000200   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        try:
            return subprocess.Popen(argv, creationflags=flags | 0x01000000, **kw).pid  # + BREAKAWAY_FROM_JOB
        except OSError:
            return subprocess.Popen(argv, creationflags=flags, **kw).pid
    return subprocess.Popen(argv, start_new_session=True, **kw).pid


def pending(cli_id):
    st = read_json(state_path(cli_id))
    return st if isinstance(st, dict) and st.get("state") == "pending" else None


def describe(cli_id):
    st = pending(cli_id)
    found = find_session(cli_id, skip=(st or {}).get("targetPath"))
    if not found:
        return {"ok": True, "session": None, "accounts": [], "pending": None,
                "blocked": "This session has no desktop app record (it was not started from the Claude app)."}
    account, org, path, rec = found
    kind = session_kind(rec)
    blocked = None
    if kind == "worktree":
        blocked = ("This session runs in a worktree, and archiving it here would delete that worktree. "
                   "Commit or merge its branch, then move a session in the main folder instead.")
    elif rec.get("isArchived"):
        blocked = "This session is archived in this account."
    return {
        "ok": True,
        "session": {
            "account": account, "org": org, "localId": rec.get("sessionId"),
            "title": rec.get("title") or "", "cwd": rec.get("cwd"), "kind": kind,
            "transcript": transcript(cli_id),
        },
        "accounts": accounts(),
        "signedIn": signed_in(),
        "pending": st,
        "blocked": blocked,
    }


def prepare(cli_id, to):
    if pending(cli_id):
        return {"ok": False, "error": "A move of this session is already waiting to finish."}
    info = describe(cli_id)
    if info["blocked"]:
        return {"ok": False, "error": info["blocked"]}
    s = info["session"]
    if to == s["account"]:
        return {"ok": False, "error": "The session is already in that account."}
    target = next((a for a in info["accounts"] if a["account"] == to), None)
    if not target:
        return {"ok": False, "error": f"No account {to} in {sessions_root()}."}
    if not target["org"]:
        return {"ok": False, "error": "That account has no sessions yet, so its organization folder is unknown. "
                                      "Start any Code session in it once, then move."}
    src_path = os.path.join(sessions_root(), s["account"], s["org"], f"{s['localId']}.json")
    rec = read_json(src_path)
    if not isinstance(rec, dict):
        return {"ok": False, "error": f"Could not read {src_path}."}
    saved = backup(cli_id, src_path)
    target_path, mtime = place(rebind(rec, to), to, target["org"])
    st = {
        "state": "pending", "at": time.time(), "cliSessionId": cli_id, "localId": s["localId"],
        "title": s["title"],
        "from": {"account": s["account"], "org": s["org"]},
        "to": {"account": to, "org": target["org"]},
        "sourcePath": src_path, "targetPath": target_path, "targetMtime": mtime, "backup": saved,
    }
    write_json(state_path(cli_id), st, indent=1)
    try:
        st["finisherPid"] = spawn_finisher(cli_id, os.getppid())
        write_json(state_path(cli_id), st, indent=1)
    except OSError as e:
        log(f"{cli_id[:8]} could not start the finisher: {e}")
    log(f"{cli_id[:8]} prepared: {s['account'][:8]} -> {to[:8]} ({s['title']!r}), backup {saved}")
    return {"ok": True, "pending": st}


def cancel(cli_id):
    st = pending(cli_id)
    if not st:
        return {"ok": False, "error": "No move of this session is waiting."}
    rec = read_json(st["targetPath"])
    if isinstance(rec, dict) and rec.get("sessionId") == st["localId"]:
        try:
            os.remove(st["targetPath"])
        except OSError as e:
            return {"ok": False, "error": f"Could not remove the copy: {e}"}
    st["state"] = "cancelled"
    st["endedAt"] = time.time()
    write_json(state_path(cli_id), st, indent=1)
    log(f"{cli_id[:8]} cancelled")
    return {"ok": True, "pending": None}


def pid_alive(pid):
    if not pid or pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000 | 0x00100000, False, pid)  # QUERY_LIMITED_INFORMATION | SYNCHRONIZE
        if not h:
            return False
        try:
            return k.WaitForSingleObject(h, 0) == 0x102     # WAIT_TIMEOUT: still running
        finally:
            k.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def remove_source(src, local_id):
    """Remove a record the way the app's own delete does: record gone, a
    deleted_<id> marker, off the archived list. The transcript stays."""
    org_dir = os.path.dirname(src)
    try:
        os.remove(src)
    except OSError:
        pass
    with open(os.path.join(org_dir, f"deleted_{bare(local_id)}"), "w", encoding="utf-8") as f:
        f.write(str(int(time.time() * 1000)))
    drop_from_index(org_dir, local_id)


def complete(st):
    """The app has archived the session in the account it left: carry over
    what it wrote since the copy, then remove the source record."""
    src = st["sourcePath"]
    final = read_json(src)
    try:
        untouched = os.path.getmtime(st["targetPath"]) == st["targetMtime"]
    except OSError:
        untouched = False
    if isinstance(final, dict) and untouched:
        place(rebind(final, st["to"]["account"]), st["to"]["account"], st["to"]["org"])
    remove_source(src, st["localId"])
    st["state"] = "done"
    st["endedAt"] = time.time()
    write_json(state_path(st["cliSessionId"]), st, indent=1)
    log(f"{st['cliSessionId'][:8]} done: now in {st['to']['account'][:8]}")


def finish(cli_id, wait_pid, poll_s=1.0, until=None):
    until = until or time.time() + FINISH_FOR_S
    archived_since = None
    while time.time() < until:
        st = pending(cli_id)
        if not st:
            return {"ok": True, "state": (read_json(state_path(cli_id), {}) or {}).get("state")}
        rec = read_json(st["sourcePath"])
        if not isinstance(rec, dict):
            # Gone from the account it leaves without our help (deleted there):
            # nothing of the source to remove, and the copy stays.
            st["state"] = "source-gone"
            write_json(state_path(cli_id), st, indent=1)
            log(f"{cli_id[:8]} the source record went away before the archive")
            return {"ok": False, "state": "source-gone"}
        if is_archived(rec, os.path.dirname(st["sourcePath"])):
            archived_since = archived_since or time.time()
            if not pid_alive(wait_pid) or time.time() - archived_since >= ARCHIVED_GRACE_S:
                time.sleep(SETTLE_S)
                complete(st)
                return {"ok": True, "state": "done"}
        else:
            archived_since = None
        time.sleep(poll_s)
    st = pending(cli_id)
    if st:
        st["state"] = "expired"
        write_json(state_path(cli_id), st, indent=1)
        log(f"{cli_id[:8]} expired: never archived in {st['from']['account'][:8]}; the copy stays")
    return {"ok": False, "state": "expired"}


# ---- importing from another account ---------------------------------------

def import_blocked(rec, transcripts):
    """Why this record of another account cannot be imported, else None."""
    cli_id = rec.get("cliSessionId")
    if not cli_id:
        return "It has no CLI session yet (it was never started)."
    if pending(cli_id):
        return "A move of it is waiting to finish."
    if cli_id not in transcripts:
        return "Its transcript is not on this computer."
    return None


def list_sessions(account):
    """Every session of one account, live ones first, newest first, each with
    why it cannot be imported (else null)."""
    transcripts = all_transcripts()
    out = []
    for _, org, path in records(account):
        rec = read_json(path)
        if not isinstance(rec, dict) or not rec.get("sessionId"):
            continue
        out.append({
            "localId": rec["sessionId"], "cliSessionId": rec.get("cliSessionId"), "org": org,
            "title": rec.get("title") or "", "cwd": rec.get("cwd"), "kind": session_kind(rec),
            "archived": is_archived(rec, os.path.dirname(path)),
            "lastActivityAt": rec.get("lastActivityAt") or rec.get("createdAt"),
            "blocked": import_blocked(rec, transcripts),
        })
    out.sort(key=lambda s: (not s["archived"], s["lastActivityAt"] or 0), reverse=True)
    return {"ok": True, "account": account, "sessions": out}


def import_session(cli_id, from_account, local_id, open_app=False):
    """Move local_id from from_account into the account this session is in."""
    here = describe(cli_id)["session"]
    if not here:
        return {"ok": False, "error": "This session has no desktop app record, so its account is unknown."}
    if from_account == here["account"]:
        return {"ok": False, "error": "That session is already in this account."}
    src = next((p for _, _, p in records(from_account) if os.path.basename(p) == f"{local_id}.json"), None)
    rec = read_json(src) if src else None
    if not isinstance(rec, dict):
        return {"ok": False, "error": f"No session {local_id} in that account."}
    blocked = import_blocked(rec, all_transcripts())
    if blocked:
        return {"ok": False, "error": blocked}
    other = rec["cliSessionId"]
    # The id the app gives a session it adopts from the resume link, so the
    # record placed here and the one the app writes are the same file.
    new_id = f"local_{other}"
    target = os.path.join(sessions_root(), here["account"], here["org"], f"{new_id}.json")
    if os.path.exists(target) or any(
            (read_json(p, {}) or {}).get("cliSessionId") == other for _, _, p in records(here["account"])):
        return {"ok": False, "error": "This account already has that session."}
    saved = backup(other, src)
    place(dict(rebind(rec, here["account"]), sessionId=new_id), here["account"], here["org"])
    remove_source(src, local_id)
    st = {
        "state": "imported", "at": time.time(), "cliSessionId": other, "localId": new_id,
        "sourceLocalId": local_id, "title": rec.get("title") or "",
        "from": {"account": from_account, "org": os.path.basename(os.path.dirname(src))},
        "to": {"account": here["account"], "org": here["org"]},
        "sourcePath": src, "targetPath": target, "backup": saved,
        "opened": open_resume(other) if open_app else False,
    }
    write_json(state_path(other), st, indent=1)
    log(f"{other[:8]} imported: {from_account[:8]} -> {here['account'][:8]} ({st['title']!r}), backup {saved}")
    return {"ok": True, "imported": st}


def open_resume(cli_id):
    """Ask the running desktop app to adopt the session now: its resume link
    lists a CLI session under the signed-in account without a restart."""
    url = f"claude://resume?session={cli_id}"
    try:
        if sys.platform == "win32":
            os.startfile(url)
        else:
            subprocess.run(["open" if sys.platform == "darwin" else "xdg-open", url],
                           check=True, timeout=10, capture_output=True)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def name(account, text):
    names = read_json(names_path(), {}) or {}
    text = (text or "").strip()
    if text:
        names[account] = text
    else:
        names.pop(account, None)
    write_json(names_path(), names, indent=1)
    return {"ok": True}


def main(argv):
    if not argv:
        print(__doc__)
        return 2
    cmd, args = argv[0], argv[1:]

    def opt(flag, default=None):
        return args[args.index(flag) + 1] if flag in args and args.index(flag) + 1 < len(args) else default

    cli_id = opt("--session")
    if cmd == "accounts" and cli_id:
        out = describe(cli_id)
    elif cmd == "prepare" and cli_id and opt("--to"):
        out = prepare(cli_id, opt("--to"))
    elif cmd == "cancel" and cli_id:
        out = cancel(cli_id)
    elif cmd == "finish" and cli_id:
        out = finish(cli_id, int(opt("--wait-pid", "0")))
    elif cmd == "name" and opt("--account"):
        out = name(opt("--account"), opt("--name", ""))
    elif cmd == "sessions" and opt("--account"):
        out = list_sessions(opt("--account"))
    elif cmd == "import" and cli_id and opt("--from") and opt("--local"):
        out = import_session(cli_id, opt("--from"), opt("--local"), "--open" in args)
    else:
        print(__doc__)
        return 2
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
