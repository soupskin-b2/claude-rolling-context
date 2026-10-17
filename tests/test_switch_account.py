#!/usr/bin/env python3
"""Switching the desktop app to another account (switch_account.py).

  A. save: the login the app is signed in with (its cookie database, through
     SQLite, and config.json's oauth keys with lastKnownAccountUuid) is saved
     under its account; a signed-out app saves nothing and leaves a saved
     login alone.
  B. restore: an account's saved login takes the place of the one there, the
     rest of config.json untouched and still tab-indented, a stale journal
     gone; restoring none signs the app out.
  C. run: quits the app (a stand-in process, ended outright when it will not
     quit), saves, restores and starts it again.
  D. switch: refuses the account signed in and one with no saved login,
     never looks for the real app when the data folder is overridden, and
     its detached helper switches by itself.
  E. after a move: the helper waits for the move to finish, and gives up
     without quitting the app when it is cancelled.
  F. on Windows the app is quit with WM_ENDSESSION, as at sign-out (a
     stand-in window that quits on it, tests/endsession_window.py); elsewhere
     what ran under the app is ended with it. The app is the parent of the
     CLI it runs from its data folder. It starts again through its package
     (Windows Store), its bundle (macOS), or as it was started (Linux: an
     AppImage by its file, else its command line, read while it ran).

  python tests/test_switch_account.py
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = tempfile.mkdtemp(prefix="rolling-context-switch-")
APP = os.path.join(ROOT, "app")
LAUNCHED = os.path.join(ROOT, "launched")
os.environ.update(HOME=ROOT, USERPROFILE=ROOT, ROLLING_CONTEXT_CLAUDE_APP_DIR=APP,
                  ROLLING_CONTEXT_SWITCH_QUIT_S="1", ROLLING_CONTEXT_SWITCH_REAP_S="1",
                  ROLLING_CONTEXT_SWITCH_LAUNCH=json.dumps(
                      [sys.executable, "-c", f"open({LAUNCHED!r}, 'a').write('x')"]))
os.environ.pop("CLAUDE_CONFIG_DIR", None)
sys.path.insert(0, os.path.join(HERE, "..", "proxy"))

import move_session as m  # noqa: E402
import switch_account as s  # noqa: E402

failures = []

A = "aaaaaaaa-0000-0000-0000-000000000001"
B = "bbbbbbbb-0000-0000-0000-000000000002"
OTHER_KEYS = {"locale": "en-US", "userThemeMode": "dark", "dxt:allowlistEnabled:org": True}


def check(cond, label, detail=""):
    print(f"    {'PASS' if cond else 'FAIL'}  {label}")
    if detail:
        print(f"          {detail}")
    if not cond:
        failures.append(label)


def cookies(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        os.remove(path)
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE cookies (host_key TEXT, name TEXT, encrypted_value BLOB)")
    db.executemany("INSERT INTO cookies VALUES (?, ?, ?)", rows)
    db.commit()
    db.close()


def cookie_names(path):
    db = sqlite3.connect(path)
    try:
        return sorted(r[0] + "=" + r[1].decode() for r in db.execute("SELECT name, encrypted_value FROM cookies"))
    finally:
        db.close()


def signed_in_as(account):
    """The app's data folder as it is while account is signed in."""
    cookies(s.cookies_path(), [(".claude.ai", "sessionKey", f"key-{account}".encode()),
                               (".claude.ai", "lastActiveOrg", f"org-{account}".encode())])
    s.write_config(dict(OTHER_KEYS, **{"oauth:tokenCache": f"tok-{account}",
                                        "oauth:tokenCacheV2": f"tok2-{account}",
                                        "lastKnownAccountUuid": account}))


def config():
    return s.read_config()


def stand_in_app():
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def gone(p, seconds=5):
    until = time.time() + seconds
    while p.poll() is None and time.time() < until:
        time.sleep(0.1)
    return p.poll() is not None


def launches(want=None, seconds=10):
    """How often the app was started again (the start is detached: waits
    up to seconds for want)."""
    until = time.time() + seconds
    while True:
        try:
            with open(LAUNCHED) as f:
                n = len(f.read())
        except OSError:
            n = 0
        if want is None or n >= want or time.time() > until:
            return n
        time.sleep(0.1)


def wait_state(want, seconds=30):
    until = time.time() + seconds
    while time.time() < until:
        st = m.read_json(s.state_path(), {}) or {}
        if st.get("state") in want:
            return st
        time.sleep(0.2)
    return m.read_json(s.state_path(), {}) or {}


print("A. saving the login the app is signed in with")
signed_in_as(A)
check(s.save_login() == A, "saved under the account config.json names")
saved = m.read_json(os.path.join(s.login_dir(A), "login.json"))
check(saved["config"] == {"oauth:tokenCache": f"tok-{A}", "oauth:tokenCacheV2": f"tok2-{A}",
                          "lastKnownAccountUuid": A},
      "only the oauth keys and the account are saved from config.json", str(saved["config"]))
check(cookie_names(os.path.join(s.login_dir(A), "Cookies")) == [f"lastActiveOrg=org-{A}", f"sessionKey=key-{A}"],
      "the cookie database is saved whole")
check(m.has_login(A) and not m.has_login(B), "has_login sees it")
os.makedirs(os.path.join(APP, "claude-code-sessions", A, "org"))
os.makedirs(os.path.join(APP, "claude-code-sessions", B, "org"))
check({a["account"]: a["login"] for a in m.accounts()} == {A: True, B: False}, "accounts() marks it")

cookies(s.cookies_path(), [(".claude.ai", "lastActiveOrg", b"org")])        # signed out: no sessionKey
check(s.save_login() is None, "a signed-out app saves nothing")
check(cookie_names(os.path.join(s.login_dir(A), "Cookies"))[1] == f"sessionKey=key-{A}",
      "and the saved login stays as it was")
signed_in_as(A)
cfg = config()
del cfg["oauth:tokenCache"], cfg["oauth:tokenCacheV2"]
s.write_config(cfg)
check(s.save_login() is None, "nor does one without its oauth token cache")


print("B. restoring a saved login")
signed_in_as(B)
s.save_login()
signed_in_as(A)
with open(s.cookies_path() + "-journal", "w") as f:
    f.write("stale")
s.restore_login(B)
check(cookie_names(s.cookies_path()) == [f"lastActiveOrg=org-{B}", f"sessionKey=key-{B}"], "B's cookies are in place")
check(not os.path.exists(s.cookies_path() + "-journal"), "the old journal is gone")
cfg = config()
check(cfg["lastKnownAccountUuid"] == B and cfg["oauth:tokenCache"] == f"tok-{B}", "B's oauth keys are in place")
check(all(cfg[k] == v for k, v in OTHER_KEYS.items()), "the rest of config.json is untouched")
with open(s.config_path(), encoding="utf-8") as f:
    text = f.read()
check(text.startswith("{\n\t\"") and not text.endswith("\n"), "config.json stays tab-indented, as the app writes it")
check(m.signed_in() == B, "move_session.signed_in reads it")

s.restore_login(None)
cfg = config()
check(not os.path.exists(s.cookies_path()), "signing out removes the cookie database")
check(not any(k.startswith("oauth:") for k in cfg) and "lastKnownAccountUuid" not in cfg,
      "and the oauth keys", str(list(cfg)))
check(all(cfg[k] == v for k, v in OTHER_KEYS.items()), "and nothing else")


print("C. run: quit, save, restore, start again")
signed_in_as(A)
os.remove(os.path.join(s.login_dir(A), "login.json"))         # A's login is saved by the switch itself
app = stand_in_app()
st = s.run(B, app.pid, None)
check(gone(app), "the app is quit (ended outright when it does not quit)")
check(st["state"] == "switched" and st["from"] == A, "switched from A", str(st))
check(m.has_login(A), "A's login was saved on the way out")
check(m.signed_in() == B and cookie_names(s.cookies_path())[1] == f"sessionKey=key-{B}", "B is signed in")
check(st["launched"] and launches(1) == 1, "the app is started again")


print("D. switch")
out = s.switch(B, app_pid=999999)
check(not out["ok"] and "already signed in" in out["error"], "refuses the account signed in", str(out))
out = s.switch("cccccccc-0000-0000-0000-000000000003", app_pid=999999)
check(not out["ok"] and "No saved login" in out["error"], "refuses an account with no saved login")
out = s.switch(A)
check(not out["ok"] and "not running in the Claude desktop app" in out["error"],
      "with the data folder overridden it never looks for the real app", str(out))

app = stand_in_app()
out = s.switch(A, app_pid=app.pid)
check(out["ok"] and out["from"] == B and out["appPid"] == app.pid, "starts the helper", str(out))
st = wait_state({"switched", "failed"})
check(st.get("state") == "switched" and gone(app), "the detached helper quits the app and switches", str(st))
check(m.signed_in() == A and launches(2) == 2, "A is signed in and the app started again")

app = stand_in_app()
out = s.switch("new", app_pid=app.pid)
st = wait_state({"switched", "failed"})
check(out["ok"] and st.get("state") == "switched", "Sign in to another account switches to none")
check(m.signed_in() is None and not os.path.exists(s.cookies_path()), "the app starts signed out")
check(m.has_login(A) and m.has_login(B), "both saved logins stay")
check(s.forget(B)["ok"] and not m.has_login(B), "forget removes one")
s.restore_login(A)


print("E. after a move")
CLI = "11111111-2222-3333-4444-555555555555"
m.write_json(m.state_path(CLI), {"state": "pending"})
app = stand_in_app()
helper = subprocess.Popen([sys.executable, os.path.join(HERE, "..", "proxy", "switch_account.py"), "run",
                           "--to", "new", "--app-pid", str(app.pid), "--after-move", CLI],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(2)
check(app.poll() is None and (m.read_json(s.state_path()) or {}).get("state") == "waiting-for-move",
      "waits while the move is pending, the app left running")
m.write_json(m.state_path(CLI), {"state": "cancelled"})
helper.wait(10)
check((m.read_json(s.state_path()) or {}).get("state") == "abandoned" and app.poll() is None,
      "a cancelled move abandons the switch, the app left running")
app.kill()

m.write_json(m.state_path(CLI), {"state": "pending"})
app = stand_in_app()
helper = subprocess.Popen([sys.executable, os.path.join(HERE, "..", "proxy", "switch_account.py"), "run",
                           "--to", "new", "--app-pid", str(app.pid), "--after-move", CLI],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(1.5)
m.write_json(m.state_path(CLI), {"state": "done"})
helper.wait(20)
check((m.read_json(s.state_path()) or {}).get("state") == "switched" and gone(app),
      "a finished move lets the switch go ahead")


print("F. the app's process")
if sys.platform == "win32":
    got = os.path.join(ROOT, "window.txt")
    win = subprocess.Popen([sys.executable, os.path.join(HERE, "endsession_window.py"), got],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    until = time.time() + 10
    while not os.path.exists(got) and time.time() < until:
        time.sleep(0.1)
    began = time.time()
    check(s.quit_app(win.pid) and time.time() - began < 5,
          "a window that quits on WM_ENDSESSION is quit that way, not ended outright")
    with open(got) as f:
        seen = f.read().split()
    check(seen == ["ready", "query", "1", "end", "1", "1", "quit"],
          "it is asked first, then told the session ends to close the app (ENDSESSION_CLOSEAPP)", str(seen))
    with open(os.path.join(s.m.logins_dir(), "switch.log")) as f:
        check("quit on session end" in f.read().splitlines()[-1], "the log says how it quit")
else:
    # The app, with a helper under it that outlives it (as its network
    # service may): the helper ignores SIGTERM and is ended after REAP_S.
    kid = os.path.join(ROOT, "kid.txt")
    app = subprocess.Popen([sys.executable, "-c",
                            "import subprocess, sys, time\n"
                            "k = subprocess.Popen([sys.executable, '-c', 'import signal, time; "
                            "signal.signal(15, signal.SIG_IGN); time.sleep(120)'])\n"
                            f"open({kid!r}, 'w').write(str(k.pid))\n"
                            "time.sleep(120)"])
    until = time.time() + 10
    while not (os.path.exists(kid) and open(kid).read()) and time.time() < until:
        time.sleep(0.1)
    kpid = int(open(kid).read())
    check(kpid in s.descendants(app.pid), "what runs under the app is found", str(s.descendants(app.pid)))
    check(s.quit_app(app.pid) and gone(app), "the app quits on SIGTERM")
    check(not m.pid_alive(kpid), "what it left running is ended too")
    with open(os.path.join(s.m.logins_dir(), "switch.log")) as f:
        tail = f.read().splitlines()[-2:]
    check("quit on SIGTERM" in tail[0] and "left behind" in tail[1], "the log says so", str(tail))
check(all(s.APP_CLI.search(x) for x in (
          r"C:\Users\u\AppData\Roaming\Claude\claude-code\2.1.295\d2e2\claude.exe",
          r"C:\Users\u\AppData\Local\Claude-Data\claude_pzs8sxrjxfjjc\claude-code\2.1.299\f058\claude.exe",
          "/home/u/.config/Claude/claude-code/2.1.295/d2e2/claude",
          "/Users/u/Library/Application Support/Claude/claude-code/2.1.295/d2e2/claude"))
      and not any(s.APP_CLI.search(x) for x in (
          r"C:\Users\u\.local\bin\claude.exe", "/home/u/.local/bin/claude",
          "/home/u/.local/share/claude/versions/2.1.295",
          "/home/u/.config/Claude/claude-code/2.1.295/d2e2/claude-helper")),
      "the app's CLI is told apart from one started in a terminal")
me = s.find_app()
check(me == (None, None) or me[0] != os.getpid(), "find_app answers the app or nothing", str(me))
os.environ.pop("ROLLING_CONTEXT_SWITCH_LAUNCH")
if sys.platform == "win32":
    pkg = os.path.join(ROOT, "WindowsApps", "Claude_2.37337.1.0_x64__pzs8sxrjxfjjc")
    os.makedirs(os.path.join(pkg, "app"))
    with open(os.path.join(pkg, "AppxManifest.xml"), "w", encoding="utf-8") as f:
        f.write('<Package><Applications><Application Id="ClaudeApp" Executable="app\\claude.exe"/>'
                '<Application Id="SshAskpass"/></Applications></Package>')
    argv = s.launch_argv(os.path.join(pkg, "app", "claude.exe"))
    check(argv == ["explorer.exe", "shell:AppsFolder\\Claude_pzs8sxrjxfjjc!ClaudeApp"],
          "a Store install starts through its package", str(argv))
    check(s.launch_argv(r"C:\Users\u\AppData\Local\AnthropicClaude\claude.exe")
          == [r"C:\Users\u\AppData\Local\AnthropicClaude\claude.exe"], "another install by its exe")
elif sys.platform == "darwin":
    check(s.launch_argv("/Applications/Claude.app/Contents/MacOS/Claude") == ["open", "/Applications/Claude.app"],
          "a macOS app starts through its bundle")
else:
    sleep = [sys.executable, "-c", "import time; time.sleep(30)", "--class=Claude"]
    for env, want, label in (
            ({"APPIMAGE": "/home/u/Apps/Claude.AppImage"}, ["/home/u/Apps/Claude.AppImage"],
             "an AppImage starts again by its file, not from its mount"),
            ({}, sleep, "anything else as it was started, arguments too")):
        p = subprocess.Popen(sleep, env={**os.environ, **env})
        time.sleep(0.3)
        argv = s.launch_argv("/tmp/.mount_Claude/claude", p.pid)
        p.kill()
        p.wait()
        check(argv == want, label, str(argv))
    os.environ["CLAUDECODE"] = "1"
    p = subprocess.Popen(sleep, env={k: v for k, v in os.environ.items() if k != "CLAUDECODE"} | {"ONLY_APP": "y"})
    time.sleep(0.3)
    env = s.launch_env(p.pid)
    p.kill()
    p.wait()
    os.environ.pop("CLAUDECODE")
    check(env and env.get("ONLY_APP") == "y" and "CLAUDECODE" not in env,
          "it starts again in its own environment, not this session's")
    os.environ["SNAP"] = "/snap/claude/1"
    out = s.switch("new", app_pid=os.getpid())
    os.environ.pop("SNAP")
    check(out["ok"] is False and "sandbox" in out["error"], "a switch is refused inside a Snap or Flatpak", str(out))
    check(s.launch_argv("/opt/Claude/claude") == ["/opt/Claude/claude"], "with the app gone, by its exe")

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all passed")
