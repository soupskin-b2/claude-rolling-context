#!/usr/bin/env python3
"""Regression suite for issue #6 — the on/off toggle.

Five parts, no framework needed:

  A. switch semantics — session vs --global scope, flag file, env override,
     precedence, idempotence. Runs in an isolated ROLLING_CONTEXT_HOME so it
     never touches the real one.
  B. marker scanning — unit cases for the per-session marker, including the
     false-positive guard: reading or discussing the plugin's own source in a
     conversation puts the literal marker in a user-role message, and that must
     NOT toggle anything. Only <local-command-stdout> blocks count.
  C. off from the start — a conversation well over the trigger must produce no
     compaction request at all, and the full history must reach upstream.
  D. off is adaptive, not destructive — the important one. Compress on turn 1
     while on, flip off, prove turn 2 goes upstream at full size AND that the
     stored compression is still there; flip back on, prove turn 3 is
     compressed again by REUSING that stored compression, with no second
     compaction request. Turning off must never cost the work already done.
  E. session scope — two conversations through one proxy: turning off in A
     must leave B compressing, A must be able to opt back in, and a
     machine-wide off must override a session that opted in.

  python tests/test_toggle.py
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PROXY = os.path.join(HERE, "..", "proxy")
MOCK = os.path.join(HERE, "mock_endpoint.py")
SWITCH = os.path.join(PROXY, "switch.py")
SESSION_OFF = "<<rolling-context:session-off>>"
SESSION_ON = "<<rolling-context:session-on>>"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_port(port, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", port), 0.3).close()
            return True
        except OSError:
            time.sleep(0.15)
    return False


def fake_home(root, settings_env):
    home = os.path.join(root, "home")
    os.makedirs(os.path.join(home, ".claude"), exist_ok=True)
    with open(os.path.join(home, ".claude", "settings.json"), "w", encoding="utf-8") as f:
        json.dump({"env": settings_env}, f)
    return home


def clean_env(home, **extra):
    env = dict(os.environ)
    for k in ("ROLLING_CONTEXT_UPSTREAM", "ROLLING_CONTEXT_SUMMARIZER_URL",
              "ROLLING_CONTEXT_SUMMARIZER_KEY", "ROLLING_CONTEXT_SUMMARIZER_FORMAT",
              "ROLLING_CONTEXT_MODEL", "ROLLING_CONTEXT_PORT",
              "ROLLING_CONTEXT_DISABLE", "ROLLING_CONTEXT_HOME",
              # Run from inside Claude Code, switch.py would record the
              # developer's own session instead of printing the marker these
              # legacy cases test. session_files() covers that path.
              "CLAUDE_CODE_SESSION_ID"):
        env.pop(k, None)
    # Never the default MITM port: a test proxy there shares it with the
    # user's real proxy (Windows) and answers their sessions with a test CA.
    # Nor the default proxy port: `switch.py status` asks /health there, so a
    # bare switch_cmd reached the developer's live proxy. A closed port reads
    # "NOT RUNNING"; a test that wants its own proxy passes its port in extra.
    env.update(HOME=home, USERPROFILE=home,
               ROLLING_CONTEXT_PORT=str(free_port()),
               ROLLING_CONTEXT_MITM_PORT=str(free_port()))
    env.update(extra)
    return env


def check(results, name, ok, note=None):
    results.append(ok)
    print(("    PASS  " if ok else "    FAIL  ") + name)
    if note:
        print(f"          {note}")


# ---------------------------------------------------------------- part A ----

def switch_cmd(home, *args, **extra):
    env = clean_env(os.path.join(home, "unused"), ROLLING_CONTEXT_HOME=home, **extra)
    out = subprocess.run([sys.executable, SWITCH, *args],
                         capture_output=True, text=True, env=env)
    return out.returncode, out.stdout.strip()


def switch_semantics(root):
    home = os.path.join(root, "switchhome")
    os.makedirs(home, exist_ok=True)
    flag = os.path.join(home, "disabled")
    results = []
    print("  switch semantics")

    rc, out = switch_cmd(home, "status")
    check(results, "starts ON", rc == 0 and "rolling-context: ON" in out)

    # default scope is the session: a marker, and no machine-wide side effect
    rc, out = switch_cmd(home, "off")
    check(results, "bare off emits the session marker",
          rc == 0 and out.startswith(SESSION_OFF))
    check(results, "bare off does NOT touch the machine-wide flag",
          not os.path.exists(flag))

    rc, out = switch_cmd(home, "on")
    check(results, "bare on emits the session marker",
          rc == 0 and out.startswith(SESSION_ON))

    rc, out = switch_cmd(home, "off", "--global")
    check(results, "off --global writes the flag",
          rc == 0 and os.path.exists(flag) and SESSION_OFF not in out)

    rc, out = switch_cmd(home, "status")
    check(results, "status reports OFF machine-wide", "rolling-context: OFF" in out)
    check(results, "status never emits a marker (it would toggle the session)",
          SESSION_OFF not in out and SESSION_ON not in out)

    rc, out = switch_cmd(home, "on")
    check(results, "session on warns machine-wide off still wins",
          rc == 0 and "machine-wide" in out and "wins over this session" in out)

    rc, _ = switch_cmd(home, "off", "--global")
    check(results, "off --global twice is idempotent", rc == 0 and os.path.exists(flag))

    rc, out = switch_cmd(home, "on", "--global")
    check(results, "on --global removes the flag", rc == 0 and not os.path.exists(flag))

    rc, _ = switch_cmd(home, "on", "--global")
    check(results, "on --global twice is idempotent",
          rc == 0 and not os.path.exists(flag))

    rc, out = switch_cmd(home, "status", ROLLING_CONTEXT_DISABLE="1")
    check(results, "env var forces OFF with no flag file",
          "OFF machine-wide (ROLLING_CONTEXT_DISABLE=1" in out)

    rc, out = switch_cmd(home, "on", "--global", ROLLING_CONTEXT_DISABLE="1")
    check(results, "on --global warns that the env var still wins",
          rc == 0 and "still wins" in out)

    rc, out = switch_cmd(home, "bogus")
    check(results, "unknown action exits nonzero", rc == 2)

    # config default — on out of the box, overridable by config.json
    cfg = os.path.join(home, "config.json")
    rc, out = switch_cmd(home, "status")
    check(results, "default is ON with no config file",
          "default for new sessions: ON" in out and "(built-in)" in out)

    for body, want, label in [
        ('{"enabled": false}', "OFF", "config can default new sessions OFF"),
        ('{"enabled": true}', "ON", "config can default new sessions ON"),
        ('{"enabled": "yes"}', "ON", "non-boolean enabled falls back to ON"),
        ('{"trigger": 1}', "ON", "config without enabled falls back to ON"),
        ('not json at all', "ON", "malformed config falls back to ON, never off"),
        ('[]', "ON", "non-object config falls back to ON"),
    ]:
        with open(cfg, "w", encoding="utf-8") as f:
            f.write(body)
        rc, out = switch_cmd(home, "status")
        check(results, label, f"default for new sessions: {want}" in out)
    os.remove(cfg)

    return sum(1 for ok in results if not ok)


# ---------------------------------------------------------------- part B/C ----

def conversation(pairs, tail):
    msgs = []
    for i in range(pairs):
        msgs.append({"role": "user", "content": f"question {i} " + "x" * 400})
        msgs.append({"role": "assistant", "content": f"answer {i} " + "y" * 400})
    msgs.append({"role": "user", "content": tail})
    return msgs


def post(port, msgs, session_id=None):
    headers = {"content-type": "application/json", "x-api-key": "k",
               "anthropic-version": "2023-06-01"}
    if session_id:
        headers["X-Claude-Code-Session-Id"] = session_id
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/messages",
        data=json.dumps({"model": "glm-4.6", "max_tokens": 64,
                         "stream": True, "messages": msgs}).encode(),
        headers=headers,
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        r.read()


B_SESSION = "sess-beta"


def health(port, session_id=None):
    query = f"?session={session_id}" if session_id else ""
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health{query}", timeout=10) as r:
        return json.loads(r.read())


def requests_from(log):
    with open(log, encoding="utf-8") as f:
        return [json.loads(l)["detail"] for l in f if json.loads(l)["kind"] == "request"]


def start_stack(work, switch_home, mock_port, proxy_port, log):
    os.makedirs(work, exist_ok=True)
    home = fake_home(work, {"ANTHROPIC_BASE_URL": f"http://127.0.0.1:{proxy_port}",
                            "ROLLING_CONTEXT_UPSTREAM": f"http://127.0.0.1:{mock_port}"})
    env = clean_env(home, ROLLING_CONTEXT_PORT=str(proxy_port),
                    ROLLING_CONTEXT_HOME=switch_home,
                    ROLLING_CONTEXT_TRIGGER="1000", ROLLING_CONTEXT_TARGET="400")
    mock = subprocess.Popen([sys.executable, MOCK, str(mock_port), log],
                            env=dict(os.environ, MOCK_STRICT="0"))
    proxy = subprocess.Popen([sys.executable, "server.py"], cwd=PROXY, env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert wait_port(mock_port), "mock endpoint did not start"
    assert wait_port(proxy_port), "proxy did not start"
    return mock, proxy


def off_from_start(root):
    """Toggle off before any traffic: nothing should ever be compressed."""
    print("  off from the start")
    work = os.path.join(root, "offstart")
    switch_home = os.path.join(work, "flag")
    os.makedirs(switch_home, exist_ok=True)
    switch_cmd(switch_home, "off", "--global")

    mock_port, proxy_port = free_port(), free_port()
    log = os.path.join(work, "mock.jsonl")
    mock, proxy = start_stack(work, switch_home, mock_port, proxy_port, log)
    results = []
    try:
        base = conversation(24, "turn one")
        post(proxy_port, base)
        time.sleep(6)  # give a compaction every chance to fire
        turn2 = base + [{"role": "assistant", "content": "ok"},
                        {"role": "user", "content": "turn two"}]
        post(proxy_port, turn2)
        time.sleep(1)
        h = health(proxy_port)
    finally:
        proxy.terminate()
        mock.terminate()

    reqs = requests_from(log)
    chat = [r for r in reqs if not r["compaction"]]
    compactions = [r for r in reqs if r["compaction"]]
    local = len(json.dumps(turn2))
    sent = chat[-1]["convo_chars"] if chat else None

    check(results, "no compaction was ever triggered", not compactions,
          f"{len(compactions)} compaction request(s) seen")
    check(results, "no summary reached upstream",
          bool(chat) and not any(c["carries_summary"] for c in chat))
    check(results, "full history went upstream",
          sent is not None and sent >= local * 0.99,
          f"{sent:,} chars sent vs {local:,} held locally")
    check(results, "/health reports enabled=false", h.get("enabled") is False)
    check(results, "chat requests still succeeded", len(chat) == 2,
          f"{len(chat)} chat request(s) reached upstream")
    return sum(1 for ok in results if not ok)


def adaptive_not_destructive(root):
    """Flip off mid-session, then back on. Nothing computed may be lost."""
    print("  off mid-session is adaptive, not destructive")
    work = os.path.join(root, "midsession")
    switch_home = os.path.join(work, "flag")
    os.makedirs(switch_home, exist_ok=True)
    switch_cmd(switch_home, "on", "--global")

    mock_port, proxy_port = free_port(), free_port()
    log = os.path.join(work, "mock.jsonl")
    mock, proxy = start_stack(work, switch_home, mock_port, proxy_port, log)
    results = []
    try:
        # turn 1, plugin ON — this is what computes the compression
        base = conversation(24, "turn one")
        post(proxy_port, base)
        time.sleep(6)
        h_on = health(proxy_port)
        stored_before = h_on.get("stored_compressions", 0)
        compactions_after_t1 = len([r for r in requests_from(log) if r["compaction"]])

        # flip OFF mid-session — no restart
        switch_cmd(switch_home, "off", "--global")
        turn2 = base + [{"role": "assistant", "content": "ok"},
                        {"role": "user", "content": "turn two"}]
        post(proxy_port, turn2)
        time.sleep(1)
        h_off = health(proxy_port)
        reqs_after_t2 = requests_from(log)
        turn2_req = [r for r in reqs_after_t2 if not r["compaction"]][-1]

        # flip back ON — the stored compression must be reused as-is
        switch_cmd(switch_home, "on", "--global")
        turn3 = turn2 + [{"role": "assistant", "content": "ok"},
                         {"role": "user", "content": "turn three"}]
        post(proxy_port, turn3)
        time.sleep(1)
        h_back = health(proxy_port)
        reqs_after_t3 = requests_from(log)
        turn3_idx = max(i for i, r in enumerate(reqs_after_t3) if not r["compaction"])
        turn3_req = reqs_after_t3[turn3_idx]
        # Compactions that had completed BEFORE turn 3 went upstream. Anything
        # after that index is the normal rolling cycle re-arming for turn 4 —
        # the mock reports a fixed 150k input tokens on every reply, so being
        # over trigger again is expected and is not recomputed work.
        compactions_before_t3 = len(
            [r for r in reqs_after_t3[:turn3_idx] if r["compaction"]]
        )
    finally:
        proxy.terminate()
        mock.terminate()

    local2 = len(json.dumps(turn2))
    local3 = len(json.dumps(turn3))

    check(results, "turn 1 computed a compression while on",
          compactions_after_t1 >= 1 and stored_before >= 1,
          f"{compactions_after_t1} compaction(s), {stored_before} stored")
    check(results, "toggle took effect with no proxy restart",
          h_off.get("enabled") is False and h_on.get("enabled") is True)
    check(results, "turn 2 (off) sent the full history, uncompressed",
          turn2_req["convo_chars"] >= local2 * 0.99 and not turn2_req["carries_summary"],
          f"{turn2_req['convo_chars']:,} chars sent vs {local2:,} held locally")
    check(results, "turn 2 (off) DISCARDED NOTHING — store intact",
          h_off.get("stored_compressions", -1) == stored_before,
          f"{stored_before} stored before, {h_off.get('stored_compressions')} after")
    check(results, "turn 3 (back on) is compressed again",
          turn3_req["convo_chars"] < local3 * 0.75 and turn3_req["carries_summary"],
          f"{turn3_req['convo_chars']:,} chars sent vs {local3:,} held locally")
    check(results, "turn 3 REUSED the pre-toggle compression, no recompute first",
          compactions_before_t3 == compactions_after_t1,
          f"{compactions_after_t1} compaction(s) computed before off, "
          f"{compactions_before_t3} had run when turn 3 was sent")
    return sum(1 for ok in results if not ok)


# ---------------------------------------------------------------- part D ----

def session_scan(root):
    """Unit cases for the marker scanner, incl. the false-positive guard."""
    print("  session marker scanning")
    home = fake_home(os.path.join(root, "scan"), {})
    out = subprocess.run([sys.executable, os.path.join(HERE, "session_scan_probe.py")],
                         capture_output=True, text=True, env=clean_env(home))
    if out.returncode != 0:
        print("    FAIL  probe crashed")
        print("          " + (out.stderr.strip().splitlines() or ["?"])[-1])
        return 1
    results = json.loads(out.stdout.strip().splitlines()[-1])["cases"]
    for name, ok in results:
        print(("    PASS  " if ok else "    FAIL  ") + name)
    return sum(1 for _, ok in results if not ok)


# ---------------------------------------------------------------- part E ----

def marked(marker_action):
    """A user message shaped like real slash-command output."""
    out = subprocess.run([sys.executable, SWITCH, marker_action],
                         capture_output=True, text=True,
                         env=clean_env(os.path.join(HERE, "nohome")))
    return {"role": "user",
            "content": f"<local-command-stdout>{out.stdout.strip()}"
                       f"</local-command-stdout>"}


def session_scope(root):
    """Two conversations, one proxy: turning off in A must not touch B."""
    print("  session scope is per-conversation")
    work = os.path.join(root, "scope")
    switch_home = os.path.join(work, "flag")
    os.makedirs(switch_home, exist_ok=True)
    switch_cmd(switch_home, "on", "--global")  # machine-wide ON for the whole case

    mock_port, proxy_port = free_port(), free_port()
    log = os.path.join(work, "mock.jsonl")
    mock, proxy = start_stack(work, switch_home, mock_port, proxy_port, log)
    results = []
    try:
        # Conversation A opts out via the marker; B says nothing.
        a1 = conversation(24, "alpha turn one") + [marked("off")]
        b1 = conversation(24, "beta turn one")
        b1[0] = {"role": "user", "content": "beta divergence " + "z" * 400}
        post(proxy_port, a1)
        post(proxy_port, b1, session_id=B_SESSION)
        time.sleep(6)  # let B's compaction land

        a2 = a1 + [{"role": "assistant", "content": "ok"},
                   {"role": "user", "content": "alpha turn two"}]
        b2 = b1 + [{"role": "assistant", "content": "ok"},
                   {"role": "user", "content": "beta turn two"}]
        post(proxy_port, a2)
        post(proxy_port, b2, session_id=B_SESSION)
        time.sleep(1)
        h = health(proxy_port)
        stored_mid = h.get("stored_compressions", 0)
        b_counted = health(proxy_port, B_SESSION).get("session", {})
        other_counted = health(proxy_port, "never-seen").get("session", {})

        # A opts back in; the compression stored for A's history is reusable
        # because <local-command-stdout> is stripped before hashing.
        a3 = a2 + [{"role": "assistant", "content": "ok"}, marked("on"),
                   {"role": "user", "content": "alpha turn three"}]
        post(proxy_port, a3)
        time.sleep(6)
        a4 = a3 + [{"role": "assistant", "content": "ok"},
                   {"role": "user", "content": "alpha turn four"}]
        post(proxy_port, a4)
        time.sleep(1)

        # Machine-wide off must override a session that opted in.
        switch_cmd(switch_home, "off", "--global")
        a5 = a4 + [{"role": "assistant", "content": "ok"},
                   {"role": "user", "content": "alpha turn five"}]
        post(proxy_port, a5)
        time.sleep(1)
    finally:
        proxy.terminate()
        mock.terminate()

    chat = [r for r in requests_from(log) if not r["compaction"]]
    by_tail = {}
    for r in chat:
        by_tail.setdefault(r["convo_chars"], r)
    # requests arrive in order: a1, b1, a2, b2, a3, a4, a5
    a2_req, b2_req = chat[2], chat[3]
    a4_req, a5_req = chat[5], chat[6]

    check(results, "session A (off) sent its full history",
          not a2_req["carries_summary"]
          and a2_req["convo_chars"] >= len(json.dumps(a2)) * 0.99,
          f"{a2_req['convo_chars']:,} chars sent vs {len(json.dumps(a2)):,} held")
    check(results, "session B (untouched) was still compressed",
          b2_req["carries_summary"]
          and b2_req["convo_chars"] < len(json.dumps(b2)) * 0.75,
          f"{b2_req['convo_chars']:,} chars sent vs {len(json.dumps(b2)):,} held")
    check(results, "A opting out never blocked B's compression",
          stored_mid >= 1, f"{stored_mid} compression(s) stored")
    check(results, "/health?session= counts B's compression and its savings",
          b_counted.get("compressions", 0) >= 1 and b_counted.get("tokens_saved", 0) > 0,
          str(b_counted))
    check(results, "a session the proxy never compressed counts none",
          other_counted.get("compressions") == 0 and other_counted.get("tokens_saved") == 0,
          str(other_counted))
    check(results, "session A opting back in is compressed again",
          a4_req["carries_summary"]
          and a4_req["convo_chars"] < len(json.dumps(a4)) * 0.75,
          f"{a4_req['convo_chars']:,} chars sent vs {len(json.dumps(a4)):,} held")
    check(results, "machine-wide off overrides a session that opted in",
          not a5_req["carries_summary"]
          and a5_req["convo_chars"] >= len(json.dumps(a5)) * 0.99,
          f"{a5_req['convo_chars']:,} chars sent vs {len(json.dumps(a5)):,} held")
    return sum(1 for ok in results if not ok)


# ---------------------------------------------------------------- part F ----

def config_default(root):
    """config.json sets the default; an explicit session marker still wins."""
    print("  config default drives the proxy")
    work = os.path.join(root, "cfg")
    switch_home = os.path.join(work, "flag")
    os.makedirs(switch_home, exist_ok=True)
    switch_cmd(switch_home, "on", "--global")
    with open(os.path.join(switch_home, "config.json"), "w", encoding="utf-8") as f:
        f.write('{"enabled": false}')

    mock_port, proxy_port = free_port(), free_port()
    log = os.path.join(work, "mock.jsonl")
    mock, proxy = start_stack(work, switch_home, mock_port, proxy_port, log)
    results = []
    try:
        # A says nothing: it must follow the configured default (off).
        a1 = conversation(24, "alpha turn one")
        post(proxy_port, a1)
        time.sleep(6)
        a2 = a1 + [{"role": "assistant", "content": "ok"},
                   {"role": "user", "content": "alpha turn two"}]
        post(proxy_port, a2)
        time.sleep(1)
        h = health(proxy_port)

        # B opts in explicitly, against the configured default.
        b1 = conversation(24, "beta turn one")
        b1[0] = {"role": "user", "content": "beta divergence " + "z" * 400}
        b1.append(marked("on"))
        post(proxy_port, b1)
        time.sleep(6)
        b2 = b1 + [{"role": "assistant", "content": "ok"},
                   {"role": "user", "content": "beta turn two"}]
        post(proxy_port, b2)
        time.sleep(1)
    finally:
        proxy.terminate()
        mock.terminate()

    chat = [r for r in requests_from(log) if not r["compaction"]]
    a2_req, b2_req = chat[1], chat[3]

    check(results, "/health reports the configured default",
          h.get("default_enabled") is False and h.get("enabled") is True,
          f"default_enabled={h.get('default_enabled')}, enabled={h.get('enabled')}")
    check(results, "a session that says nothing follows the config default (off)",
          not a2_req["carries_summary"]
          and a2_req["convo_chars"] >= len(json.dumps(a2)) * 0.99,
          f"{a2_req['convo_chars']:,} chars sent vs {len(json.dumps(a2)):,} held")
    check(results, "an explicit session on overrides the config default",
          b2_req["carries_summary"]
          and b2_req["convo_chars"] < len(json.dumps(b2)) * 0.75,
          f"{b2_req['convo_chars']:,} chars sent vs {len(json.dumps(b2)):,} held")
    return sum(1 for ok in results if not ok)


# ---------------------------------------------------------------- part G ----

def subagent_inheritance(root):
    """A subagent inherits its parent session's toggle.

    Subagents get their own transcript — so their message list never contains
    the parent's marker — but they inherit the parent's session id. The proxy
    latches the marker against that id, which is what carries the setting in.
    """
    print("  subagents inherit their session's toggle")
    work = os.path.join(root, "subagent")
    switch_home = os.path.join(work, "flag")
    os.makedirs(switch_home, exist_ok=True)
    switch_cmd(switch_home, "on", "--global")

    mock_port, proxy_port = free_port(), free_port()
    log = os.path.join(work, "mock.jsonl")
    mock, proxy = start_stack(work, switch_home, mock_port, proxy_port, log)
    results = []
    PARENT, OTHER = "sess-parent-aaa", "sess-other-bbb"
    try:
        # Parent turns compression off for its own session.
        p1 = conversation(24, "parent turn one") + [marked("off")]
        post(proxy_port, p1, session_id=PARENT)
        time.sleep(1)

        # A subagent of that session: own transcript, no marker, same id.
        sub = conversation(24, "subagent doing a task")
        sub[0] = {"role": "user", "content": "subagent divergence " + "s" * 400}
        post(proxy_port, sub, session_id=PARENT)
        time.sleep(6)
        sub2 = sub + [{"role": "assistant", "content": "ok"},
                      {"role": "user", "content": "subagent step two"}]
        post(proxy_port, sub2, session_id=PARENT)
        time.sleep(1)

        # An unrelated session must be untouched by all of it.
        o1 = conversation(24, "other session turn one")
        o1[0] = {"role": "user", "content": "other divergence " + "o" * 400}
        post(proxy_port, o1, session_id=OTHER)
        time.sleep(6)
        o2 = o1 + [{"role": "assistant", "content": "ok"},
                   {"role": "user", "content": "other turn two"}]
        post(proxy_port, o2, session_id=OTHER)
        time.sleep(1)

        # Parent opts back in; its subagents must follow it back.
        p2 = p1 + [{"role": "assistant", "content": "ok"}, marked("on")]
        post(proxy_port, p2, session_id=PARENT)
        time.sleep(1)
        sub3 = sub2 + [{"role": "assistant", "content": "ok"},
                       {"role": "user", "content": "subagent step three"}]
        post(proxy_port, sub3, session_id=PARENT)
        time.sleep(1)
    finally:
        proxy.terminate()
        mock.terminate()

    chat = [r for r in requests_from(log) if not r["compaction"]]
    sub2_req, o2_req, sub3_req = chat[2], chat[4], chat[6]

    check(results, "subagent inherits the parent's off, despite no marker",
          not sub2_req["carries_summary"]
          and sub2_req["convo_chars"] >= len(json.dumps(sub2)) * 0.99,
          f"{sub2_req['convo_chars']:,} chars sent vs {len(json.dumps(sub2)):,} held")
    check(results, "an unrelated session is NOT affected",
          o2_req["carries_summary"]
          and o2_req["convo_chars"] < len(json.dumps(o2)) * 0.75,
          f"{o2_req['convo_chars']:,} chars sent vs {len(json.dumps(o2)):,} held")
    check(results, "subagent follows the parent back on",
          sub3_req["carries_summary"]
          and sub3_req["convo_chars"] < len(json.dumps(sub3)) * 0.75,
          f"{sub3_req['convo_chars']:,} chars sent vs {len(json.dumps(sub3)):,} held")
    return sum(1 for ok in results if not ok)


def session_files(root):
    """/roll (and /rolling-context:on|off run inside Claude Code) record the
    session's choice in a state file keyed by its session id, instead of a
    marker. Same reach as the marker had: the session's subagents follow it,
    other sessions do not, and it wins over a stale marker in the transcript.
    """
    print("  session state files")
    work = os.path.join(root, "sessfiles")
    switch_home = os.path.join(work, "flag")
    os.makedirs(switch_home, exist_ok=True)
    results = []
    PARENT, OTHER = "sess-file-parent", "sess-file-other"

    rc, out = switch_cmd(switch_home, "off", "--session", PARENT)
    state_file = os.path.join(switch_home, "sessions", PARENT + ".json")
    check(results, "off --session writes a state file and prints no marker",
          rc == 0 and os.path.exists(state_file) and SESSION_OFF not in out)
    rc, out = switch_cmd(switch_home, "off", CLAUDE_CODE_SESSION_ID=OTHER + "-env")
    check(results, "CLAUDE_CODE_SESSION_ID is used when --session is not given",
          os.path.exists(os.path.join(switch_home, "sessions", OTHER + "-env.json")))
    rc, out = switch_cmd(switch_home, "status", "--session", PARENT)
    check(results, "status reports this session's own state",
          "this session: OFF" in out, out.splitlines()[2] if len(out.splitlines()) > 2 else out)

    mock_port, proxy_port = free_port(), free_port()
    log = os.path.join(work, "mock.jsonl")
    mock, proxy = start_stack(work, switch_home, mock_port, proxy_port, log)
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{proxy_port}/health?session={PARENT}", timeout=10) as r:
            h = json.loads(r.read())
        check(results, "/health?session= reports the session as off",
              {k: h.get("session", {}).get(k) for k in ("enabled", "scope")}
              == {"enabled": False, "scope": "this session"}, str(h.get("session")))

        p1 = conversation(24, "parent turn one")
        post(proxy_port, p1, session_id=PARENT)
        time.sleep(6)
        p2 = p1 + [{"role": "assistant", "content": "ok"},
                   {"role": "user", "content": "parent turn two"}]
        post(proxy_port, p2, session_id=PARENT)
        time.sleep(1)

        o1 = conversation(24, "other session turn one")
        o1[0] = {"role": "user", "content": "other divergence " + "o" * 400}
        post(proxy_port, o1, session_id=OTHER)
        time.sleep(6)
        o2 = o1 + [{"role": "assistant", "content": "ok"},
                   {"role": "user", "content": "other turn two"}]
        post(proxy_port, o2, session_id=OTHER)
        time.sleep(1)

        # Back on through the file, while the transcript still carries an
        # old "off" marker from before state files: the file wins.
        switch_cmd(switch_home, "on", "--session", PARENT)
        p3 = p2 + [{"role": "assistant", "content": "ok"}, marked("off"),
                   {"role": "assistant", "content": "ok"},
                   {"role": "user", "content": "parent turn three"}]
        post(proxy_port, p3, session_id=PARENT)
        time.sleep(6)
        p4 = p3 + [{"role": "assistant", "content": "ok"},
                   {"role": "user", "content": "parent turn four"}]
        post(proxy_port, p4, session_id=PARENT)
        time.sleep(1)
    finally:
        proxy.terminate()
        mock.terminate()

    chat = [r for r in requests_from(log) if not r["compaction"]]
    p2_req, o2_req, p4_req = chat[1], chat[3], chat[5]
    check(results, "the session that turned off sends its full history",
          not p2_req["carries_summary"]
          and p2_req["convo_chars"] >= len(json.dumps(p2)) * 0.99,
          f"{p2_req['convo_chars']:,} chars sent vs {len(json.dumps(p2)):,} held")
    check(results, "another session is NOT affected",
          o2_req["carries_summary"]
          and o2_req["convo_chars"] < len(json.dumps(o2)) * 0.75,
          f"{o2_req['convo_chars']:,} chars sent vs {len(json.dumps(o2)):,} held")
    check(results, "on through the file wins over a stale off marker",
          p4_req["carries_summary"]
          and p4_req["convo_chars"] < len(json.dumps(p4)) * 0.75,
          f"{p4_req['convo_chars']:,} chars sent vs {len(json.dumps(p4)):,} held")
    return sum(1 for ok in results if not ok)


def latch_unit(root):
    """The latch survives losing the marker, and is bounded."""
    print("  session latch")
    home = fake_home(os.path.join(root, "latch"), {})
    probe = (
        "import server;"
        "s=server.SessionToggleStore(limit=3);"
        "s.set('a', True); s.set('b', False);"
        "r=[s.get('a') is True, s.get('b') is False, s.get('zz') is None,"
        " s.get('') is None, s.set('a', True) is False, s.set('a', False) is True];"
        "s.set('c', True); s.set('d', True); s.set('e', True);"
        "r.append(len(s) == 3);"
        "r.append(s.get('b') is None);"
        "s2=server.SessionToggleStore();"
        "s2.set('', True); r.append(len(s2) == 0);"
        "print(r)"
    )
    out = subprocess.run([sys.executable, "-c", probe], cwd=PROXY,
                         capture_output=True, text=True, env=clean_env(home))
    if out.returncode != 0:
        print("    FAIL  latch probe crashed")
        print("          " + (out.stderr.strip().splitlines() or ["?"])[-1])
        return 1
    got = eval(out.stdout.strip().splitlines()[-1])
    names = ["latched value reads back (off)", "latched value reads back (on)",
             "unknown session -> None", "empty session id -> None",
             "unchanged set reports no change", "changed set reports a change",
             "bounded at the limit", "oldest session evicted first",
             "empty session id is never stored"]
    results = []
    for name, ok in zip(names, got):
        check(results, name, ok)
    return sum(1 for ok in results if not ok)


def main():
    root = tempfile.mkdtemp(prefix="rolling-context-toggle-")
    try:
        print("toggle")
        failures = switch_semantics(root)
        print()
        failures += session_scan(root)
        print()
        failures += off_from_start(root)
        print()
        failures += adaptive_not_destructive(root)
        print()
        failures += session_scope(root)
        print()
        failures += config_default(root)
        print()
        failures += latch_unit(root)
        print()
        failures += subagent_inheritance(root)
        print()
        failures += session_files(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print()
    if failures:
        print(f"FAILED ({failures})")
        sys.exit(1)
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()
