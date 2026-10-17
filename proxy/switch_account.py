#!/usr/bin/env python3
"""Switch the Claude desktop app to another account without signing in again.

The app keeps the login of the account it is signed in to in two places in
its data folder (see move_session.app_dir):

  Network/Cookies    the claude.ai session cookies (SQLite, locked while the
                     app runs)
  config.json        oauth:tokenCache, oauth:tokenCacheV2 and
                     lastKnownAccountUuid, the account they belong to

Everything else there is the machine's, or already kept per account. The
cookie values and token caches are encrypted with a key of this OS user's
(DPAPI on Windows, the keychain on macOS), the same for every account, so a
copy taken while one account is signed in works again later on this
computer and nowhere else.

A switch quits the app (as Windows does at sign-out, with WM_ENDSESSION,
which the app answers with its own clean quit; SIGTERM elsewhere), saves the
login of the account it was signed in to under
~/.claude/rolling-context-logins/<account>/, puts the saved login of the
account picked in its place and starts the app again. An account with no
saved login gets a signed-out app: sign in once and it is saved the next time
the app switches away from it. Signing out in the app ends that login on the
server, so the saved copy stops working too; switch here instead.

The app's own sessions run inside it, so the switch runs in a detached helper
that outlives them: `switch` checks, starts `run` and returns at once.

  python switch_account.py switch --to ACCOUNT|new [--after-move CLI_ID] [--app-pid PID]
  python switch_account.py forget --account ACCOUNT
  python switch_account.py run    --to ACCOUNT|new --app-pid PID [--exe EXE] [--after-move CLI_ID]
                                                                         (internal)

Every command prints one JSON object. ROLLING_CONTEXT_CLAUDE_APP_DIR overrides
the app's data folder, ROLLING_CONTEXT_SWITCH_LAUNCH (a JSON argv) how the app
is started again (tests).
"""
import glob
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time

import move_session as m

# How long a clean quit gets before the app is ended outright: the app's own
# quit cleanup gives up after 15 s and exits 2 s later.
QUIT_S = float(os.environ.get("ROLLING_CONTEXT_SWITCH_QUIT_S") or 25)
# How long what ran under the app gets to end with it before it is ended.
REAP_S = float(os.environ.get("ROLLING_CONTEXT_SWITCH_REAP_S") or 10)
# How long a Move and switch waits for the move to finish.
AFTER_MOVE_S = 600
OAUTH = "oauth:"
ACCOUNT_KEY = "lastKnownAccountUuid"


# ---- where things are ------------------------------------------------------

def cookies_path():
    return os.path.join(m.app_dir(), "Network", "Cookies")


def config_path():
    return os.path.join(m.app_dir(), "config.json")


def login_dir(account):
    return os.path.join(m.logins_dir(), account)


def state_path():
    return os.path.join(m.logins_dir(), "switch.json")


def log(line):
    try:
        os.makedirs(m.logins_dir(), exist_ok=True)
        with open(os.path.join(m.logins_dir(), "switch.log"), "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
    except OSError:
        pass


def short(account):
    return (account or "nobody")[:8]


# ---- the app's config.json -------------------------------------------------

def read_config():
    cfg = m.read_json(config_path(), {})
    return cfg if isinstance(cfg, dict) else {}


def write_config(cfg):
    # As the app writes it: tab-indented, no newline at the end.
    tmp = f"{config_path()}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(cfg, indent="\t", ensure_ascii=False))
    os.replace(tmp, config_path())


def login_keys(cfg):
    return {k: v for k, v in cfg.items() if k.startswith(OAUTH) or k == ACCOUNT_KEY}


# ---- saving and restoring a login (the app is not running) ------------------

def signed_in_cookies(path):
    """Whether this cookie database holds a claude.ai login."""
    try:
        db = sqlite3.connect(path)
        try:
            n = db.execute("SELECT count(*) FROM cookies WHERE name = 'sessionKey' "
                           "AND host_key LIKE '%claude.ai'").fetchone()[0]
        finally:
            db.close()
        return n > 0
    except sqlite3.Error:
        return False


def save_login():
    """Save the login the app is signed in with, under its account. The
    account, or None when the app is signed out (nothing is saved then, so a
    good saved login is never replaced by a signed-out one)."""
    cfg = read_config()
    account = cfg.get(ACCOUNT_KEY)
    keys = login_keys(cfg)
    if not account or not any(k.startswith(OAUTH) for k in keys) or not os.path.isfile(cookies_path()) \
            or not signed_in_cookies(cookies_path()):
        log(f"not signed in as {short(account)}: nothing saved")
        return None
    dest = login_dir(account)
    tmp = f"{dest}.{os.getpid()}.tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    # Through SQLite, not a file copy: a journal left by an app that was ended
    # outright is rolled back, not lost.
    src = sqlite3.connect(cookies_path())
    dst = sqlite3.connect(os.path.join(tmp, "Cookies"))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    m.write_json(os.path.join(tmp, "login.json"), {"account": account, "savedAt": time.time(), "config": keys},
                 indent=1)
    if sys.platform != "win32":
        os.chmod(tmp, 0o700)
    shutil.rmtree(dest, ignore_errors=True)
    os.replace(tmp, dest)
    log(f"saved the login of {short(account)}")
    return account


def clear_cookies():
    for suffix in ("", "-journal", "-wal", "-shm"):
        try:
            os.remove(cookies_path() + suffix)
        except FileNotFoundError:
            pass


def restore_login(account):
    """Put account's saved login in place, or sign the app out for None."""
    cfg = {k: v for k, v in read_config().items() if not (k.startswith(OAUTH) or k == ACCOUNT_KEY)}
    saved = m.read_json(os.path.join(login_dir(account), "login.json")) if account else None
    clear_cookies()
    if isinstance(saved, dict):
        os.makedirs(os.path.dirname(cookies_path()), exist_ok=True)
        tmp = f"{cookies_path()}.{os.getpid()}.tmp"
        shutil.copy2(os.path.join(login_dir(account), "Cookies"), tmp)
        os.replace(tmp, cookies_path())
        cfg.update(saved.get("config") or {})
    write_config(cfg)
    log(f"restored the login of {short(account)}" if isinstance(saved, dict) else "signed the app out")


# ---- the app's process -----------------------------------------------------

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _Entry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]


def parent_of(pid):
    if sys.platform == "win32":
        k = ctypes.windll.kernel32
        k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        snap = k.CreateToolhelp32Snapshot(0x2, 0)   # TH32CS_SNAPPROCESS
        if snap in (None, wintypes.HANDLE(-1).value):
            return None
        try:
            e = _Entry()
            e.dwSize = ctypes.sizeof(_Entry)
            ok = k.Process32FirstW(wintypes.HANDLE(snap), ctypes.byref(e))
            while ok:
                if e.th32ProcessID == pid:
                    return e.th32ParentProcessID or None
                ok = k.Process32NextW(wintypes.HANDLE(snap), ctypes.byref(e))
        finally:
            k.CloseHandle(wintypes.HANDLE(snap))
        return None
    try:
        out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
        return int(out.stdout.strip()) or None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def exe_of(pid):
    if sys.platform == "win32":
        k = ctypes.windll.kernel32
        k.OpenProcess.restype = wintypes.HANDLE
        h = k.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return None
        try:
            buf = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(buf))
            ok = k.QueryFullProcessImageNameW(wintypes.HANDLE(h), 0, buf, ctypes.byref(size))
            return buf.value if ok else None
        finally:
            k.CloseHandle(wintypes.HANDLE(h))
    if os.path.exists(f"/proc/{pid}/exe"):
        try:
            return os.readlink(f"/proc/{pid}/exe")
        except OSError:
            return None
    try:
        out = subprocess.run(["ps", "-o", "comm=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


# The Claude Code CLI the app runs from its data folder, wherever that is
# (Roaming\Claude, the Store install's Claude-Data\<package family>,
# ~/.config/Claude): <data>/claude-code/<version>/<hash>/claude.
APP_CLI = re.compile(r"[\\/]claude-code[\\/][^\\/]+[\\/][^\\/]+[\\/]claude(?:\.exe)?$", re.IGNORECASE)


def find_app():
    """(pid, exe) of the app this session runs in: the parent of the Claude
    Code CLI the app started from its data folder (shells may sit between it
    and this script). A CLI started from a terminal is not the app's, and is
    never taken for it: nothing is quit then."""
    pid = os.getpid()
    for _ in range(12):
        parent = parent_of(pid)
        if not parent:
            break
        if APP_CLI.search(exe_of(pid) or ""):
            exe = exe_of(parent)
            return (parent, exe) if exe else (None, None)
        pid = parent
    return None, None


def end_session(pid):
    """Tell the app's windows that the Windows session is ending, as Windows
    does at sign-out: the app's main window answers WM_ENDSESSION with its
    own quit (the one the tray's Exit runs), which skips the running-sessions
    prompt and flushes its stores first. Closing the window only hides it to
    the tray. True when there was a window to tell."""
    u = ctypes.windll.user32
    found = []
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def each(hwnd, _):
        owner = wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid:
            cls = ctypes.create_unicode_buffer(256)
            u.GetClassNameW(hwnd, cls, 256)
            if cls.value == "Chrome_WidgetWin_1":    # its windows, not Chromium's own
                found.append(hwnd)
        return True

    u.EnumWindows(proc(each), 0)
    u.SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
                                      wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
    ENDSESSION_CLOSEAPP, SMTO_ABORTIFHUNG = 0x1, 0x2
    WM_QUERYENDSESSION, WM_ENDSESSION = 0x11, 0x16
    res = ctypes.c_size_t()
    for msg, wparam in ((WM_QUERYENDSESSION, 0), (WM_ENDSESSION, 1)):
        for hwnd in found:
            u.SendMessageTimeoutW(hwnd, msg, wparam, ENDSESSION_CLOSEAPP, SMTO_ABORTIFHUNG, 5000, ctypes.byref(res))
    return bool(found)


def descendants(pid):
    """Every process under pid (POSIX): the app's helpers (GPU, network
    service, renderers) may outlive it for a moment once it is ended."""
    try:
        out = subprocess.run(["ps", "-A", "-o", "pid=,ppid="], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    kids = {}
    for line in out.splitlines():
        try:
            c, p = (int(x) for x in line.split())
        except ValueError:
            continue
        kids.setdefault(p, []).append(c)
    found, todo = [], [pid]
    while todo:
        for c in kids.get(todo.pop(), []):
            if c not in found:
                found.append(c)
                todo.append(c)
    return found


def quit_app(pid):
    """Quit the app the way it quits itself, then end it outright once QUIT_S
    is up; True when it is gone. Elsewhere SIGTERM is that quit."""
    began = time.time()
    family = [] if sys.platform == "win32" else descendants(pid)
    if sys.platform == "win32":
        how = "session end" if end_session(pid) else None
    else:
        try:
            os.kill(pid, 15)
            how = "SIGTERM"
        except OSError:
            how = None
    until = time.time() + (QUIT_S if how else 0)
    while m.pid_alive(pid) and time.time() < until:
        time.sleep(0.25)
    if not m.pid_alive(pid):
        log(f"the app ({pid}) quit on {how} in {time.time() - began:.1f}s")
        return reap(family)
    log(f"the app ({pid}) did not quit on {how} within {QUIT_S:g}s: ending it" if how
        else f"the app ({pid}) has no window to tell: ending it")
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=15)
    else:
        for p in [pid] + family:
            try:
                os.kill(p, 9)
            except OSError:
                pass
    until = time.time() + 10
    while m.pid_alive(pid) and time.time() < until:
        time.sleep(0.25)
    return not m.pid_alive(pid) and reap(family)


def reap(family, seconds=None):
    """Wait for what was under the app to end too (POSIX, where nothing like
    taskkill /T ends it with the app), ending what still runs: its network
    service writes the cookie database. A process a session started detached
    (as this helper is) is no longer under the app and is left alone."""
    until = time.time() + (REAP_S if seconds is None else seconds)
    while any(m.pid_alive(p) for p in family) and time.time() < until:
        time.sleep(0.25)
    left = [p for p in family if m.pid_alive(p)]
    for p in left:
        try:
            os.kill(p, 9)
        except OSError:
            pass
    until = time.time() + 5
    while any(m.pid_alive(p) for p in left) and time.time() < until:
        time.sleep(0.1)
    if left:
        log(f"ended {len(left)} process(es) the app left behind")
    return True


def wait_unlocked(seconds=15):
    """The app's helper processes may hold the cookie database a moment longer."""
    until = time.time() + seconds
    while time.time() < until:
        try:
            if os.path.exists(cookies_path()):
                with open(cookies_path(), "r+b"):
                    pass
            return True
        except OSError:
            time.sleep(0.5)
    return False


def proc_environ(pid):
    try:
        with open(f"/proc/{pid}/environ", "rb") as f:
            items = f.read().split(b"\0")
    except OSError:
        return {}
    return dict(i.decode("utf-8", "replace").split("=", 1) for i in items if b"=" in i)


def proc_cmdline(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return [a.decode("utf-8", "replace") for a in f.read().split(b"\0") if a]
    except OSError:
        return []


def launch_argv(exe, pid=None):
    """How to start the app again: from its executable, and on Linux from
    how it was started, read while it runs (an AppImage's exe is inside a
    mount that goes away with it, and an app run by a system Electron is that
    electron plus its app)."""
    env = os.environ.get("ROLLING_CONTEXT_SWITCH_LAUNCH")
    if env:
        return json.loads(env)
    if sys.platform == "win32":
        # A Store (MSIX) install starts through its package, not its exe.
        pkg = re.search(r"[\\/]WindowsApps[\\/]([^\\/_]+)_[^\\/]*__([^\\/]+)[\\/]", exe or "")
        if pkg:
            root = exe[:pkg.end()]
            try:
                with open(os.path.join(root, "AppxManifest.xml"), encoding="utf-8") as f:
                    app_id = re.search(r'<Application\s+Id="([^"]+)"', f.read()).group(1)
            except (OSError, AttributeError):
                app_id = "Claude"
            return ["explorer.exe", f"shell:AppsFolder\\{pkg.group(1)}_{pkg.group(2)}!{app_id}"]
        return [exe] if exe else None
    if sys.platform == "darwin":
        bundle = re.match(r"(.*?\.app)/Contents/MacOS/", exe or "")
        return ["open", bundle.group(1)] if bundle else ["open", "-a", "Claude"]
    if pid and proc_environ(pid).get("APPIMAGE"):
        return [proc_environ(pid)["APPIMAGE"]]
    return proc_cmdline(pid) or ([exe] if exe else None)


def launch_env(pid):
    """The environment to start the app again in. Windows and macOS start it
    through the shell (explorer, open), which gives it its own; on Linux the
    helper starts it, so it gets the one it ran in, read while it runs, not
    this session's (CLAUDECODE, the proxy's HTTPS_PROXY)."""
    if sys.platform in ("win32", "darwin") or not pid:
        return None
    return proc_environ(pid) or None


def sandboxed():
    """In a Flatpak or Snap everything inside goes when the app does, the
    helper too: it would quit the app and never start it again."""
    return os.path.exists("/.flatpak-info") or bool(os.environ.get("SNAP"))


def launch(exe, argv=None, env=None):
    argv = argv or launch_argv(exe)
    kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
              env=env)
    try:
        if not argv:
            raise OSError("no way to start it")
        if sys.platform == "win32":
            subprocess.Popen(argv, creationflags=0x00000008 | 0x00000200, **kw)
        else:
            subprocess.Popen(argv, start_new_session=True, **kw)
        return True
    except OSError as e:
        log(f"could not start the app ({argv}): {e}")
        if sys.platform == "win32":
            try:
                os.startfile("claude://")
                return True
            except OSError:
                pass
        return False


# ---- the switch ------------------------------------------------------------

def write_state(st):
    m.write_json(state_path(), st, indent=1)


def wait_for_move(cli_id):
    """Move and switch: the move finishes once the app has archived the
    session and its CLI has ended. True when it did."""
    until = time.time() + AFTER_MOVE_S
    while time.time() < until:
        st = m.read_json(m.state_path(cli_id), {}) or {}
        if st.get("state") == "done":
            return True
        if st.get("state") != "pending":
            return False
        time.sleep(1)
    return False


def run(to, app_pid, exe, after_move=None):
    # How the app was started, and in what, read now while it still runs.
    argv, env = launch_argv(exe, app_pid), launch_env(app_pid)
    st = {"state": "quitting", "at": time.time(), "to": to, "appPid": app_pid, "exe": exe, "launch": argv}
    if after_move:
        st["state"] = "waiting-for-move"
        write_state(st)
        if not wait_for_move(after_move):
            st.update(state="abandoned", reason="the move did not finish", endedAt=time.time())
            write_state(st)
            log(f"switch to {short(to)} abandoned: the move of {after_move[:8]} did not finish")
            return st
        st["state"] = "quitting"
    write_state(st)
    if not quit_app(app_pid) or not wait_unlocked():
        st.update(state="failed", reason="the app would not quit", endedAt=time.time())
        write_state(st)
        log(f"switch to {short(to)} failed: the app ({app_pid}) would not quit")
        return st
    time.sleep(1)
    try:
        st["from"] = save_login()
        restore_login(None if to == "new" else to)
        st["state"] = "switched"
    except (OSError, sqlite3.Error) as e:
        st.update(state="failed", reason=str(e))
        log(f"switch to {short(to)} failed: {e}")
    st["launched"] = launch(exe, argv, env)
    st["endedAt"] = time.time()
    write_state(st)
    log(f"switched {short(st.get('from'))} -> {short(to)}, app started again: {st['launched']}")
    return st


def spawn_run(to, app_pid, exe, after_move):
    argv = [sys.executable, os.path.abspath(__file__), "run", "--to", to, "--app-pid", str(app_pid),
            "--exe", exe or ""]
    if after_move:
        argv += ["--after-move", after_move]
    kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
              close_fds=True, cwd=os.path.dirname(os.path.abspath(__file__)))
    if sys.platform == "win32":
        flags = 0x00000008 | 0x00000200   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        try:
            return subprocess.Popen(argv, creationflags=flags | 0x01000000, **kw).pid  # + BREAKAWAY_FROM_JOB
        except OSError:
            return subprocess.Popen(argv, creationflags=flags, **kw).pid
    return subprocess.Popen(argv, start_new_session=True, **kw).pid


def switch(to, after_move=None, app_pid=None):
    current = m.signed_in()
    if to != "new":
        if to == current and not after_move:
            return {"ok": False, "error": "The app is already signed in to that account."}
        if not m.has_login(to):
            return {"ok": False, "error": "No saved login for that account: sign in to it once (Sign in to "
                                          "another account), and it is saved when the app switches away."}
    if sandboxed():
        return {"ok": False, "error": "The Claude app runs in a Flatpak or Snap sandbox here, which would end "
                                      "the switch along with the app: sign in from the app instead."}
    exe = exe_of(app_pid) if app_pid else None
    # With another data folder (tests), the app this runs in is not the one
    # whose login would change: never quit it.
    if not app_pid and not os.environ.get("ROLLING_CONTEXT_CLAUDE_APP_DIR"):
        app_pid, exe = find_app()
    if not app_pid:
        return {"ok": False, "error": "This session is not running in the Claude desktop app."}
    st = m.read_json(state_path(), {}) or {}
    if st.get("state") in ("waiting-for-move", "quitting") and m.pid_alive(st.get("helperPid") or 0):
        return {"ok": False, "error": "A switch is already under way."}
    helper = spawn_run(to, app_pid, exe, after_move)
    write_state({"state": "starting", "at": time.time(), "to": to, "appPid": app_pid, "helperPid": helper})
    log(f"switch {short(current)} -> {short(to)} started (app {app_pid}, helper {helper})"
        + (f" after the move of {after_move[:8]}" if after_move else ""))
    return {"ok": True, "to": to, "from": current, "appPid": app_pid}


def forget(account):
    shutil.rmtree(login_dir(account), ignore_errors=True)
    log(f"forgot the login of {short(account)}")
    return {"ok": True}


def main(argv):
    if not argv:
        print(__doc__)
        return 2
    cmd, args = argv[0], argv[1:]

    def opt(flag, default=None):
        return args[args.index(flag) + 1] if flag in args and args.index(flag) + 1 < len(args) else default

    pid = int(opt("--app-pid", "0") or 0) or None
    if cmd == "switch" and opt("--to"):
        out = switch(opt("--to"), opt("--after-move"), pid)
    elif cmd == "run" and opt("--to") and pid:
        out = run(opt("--to"), pid, opt("--exe") or exe_of(pid), opt("--after-move"))
    elif cmd == "forget" and opt("--account"):
        out = forget(opt("--account"))
    else:
        print(__doc__)
        return 2
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
