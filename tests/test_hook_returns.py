#!/usr/bin/env python3
"""The start hook returns as soon as its work is done.

`/roll restart` and the SessionStart hook run start-proxy.ps1 (Windows) or
start-proxy.sh with pipes on stdout/stderr, and wait for those pipes to close.
Start-Process -RedirectStandardOutput made the proxy inherit them, so the
caller waited for as long as the proxy lived - in practice its own timeout:
/roll restart sat ~30s after the log said "Proxy healthy". This runs the
platform's hook the same way, in a throwaway home on free ports, and times it.

  A. cold start  - hook returns promptly, proxy serves, its log file is written
  B. forced restart (handover) - returns promptly, a new process serves

  python tests/test_hook_returns.py
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_toggle import check, free_port  # noqa: E402

HOOKS = os.path.join(HERE, "..", "hooks")
DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))
# Well above a healthy hook (2-5s), well below the ~30s the inherited pipes cost.
PROMPT = 12


def hook_argv():
    if os.name == "nt":
        return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                os.path.join(HOOKS, "start-proxy.ps1")]
    return ["bash", os.path.join(HOOKS, "start-proxy.sh")]


def health(port):
    try:
        with DIRECT.open(f"http://127.0.0.1:{port}/health", timeout=2) as r:
            return json.loads(r.read())
    except Exception:  # noqa: BLE001
        return None


def run_hook(env):
    """Run the hook with piped stdio, as the mod does; seconds until EOF."""
    t0 = time.time()
    p = subprocess.Popen(hook_argv(), env=env, stdin=subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        p.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        # The bug itself: the proxy the hook started holds these pipes, so
        # waiting on them again would hang for as long as it lives.
        p.kill()
        p.stdout.close()
        p.stderr.close()
        p.wait()
    return time.time() - t0


def main():
    root = tempfile.mkdtemp(prefix="rc-hook-")
    home = os.path.join(root, "home")
    os.makedirs(os.path.join(home, ".claude"))
    with open(os.path.join(home, ".claude", "settings.json"), "w", encoding="utf-8") as f:
        json.dump({"env": {"ROLLING_CONTEXT_UPSTREAM": "http://127.0.0.1:9"}}, f)
    port, mitm = free_port(), free_port()
    # Never the developer's own proxy, ports or settings: every inherited
    # ROLLING_CONTEXT_* goes, and the home is a throwaway.
    env = {k: v for k, v in os.environ.items() if not k.startswith("ROLLING_CONTEXT_")}
    env.update(HOME=home, USERPROFILE=home, ROLLING_CONTEXT_PORT=str(port),
               ROLLING_CONTEXT_MITM_PORT=str(mitm),
               ROLLING_CONTEXT_HOME=os.path.join(root, "state"))
    results, pids = [], set()
    try:
        print("A. cold start")
        secs = run_hook(env)
        h = health(port)
        check(results, f"the hook's pipes close within {PROMPT}s", secs < PROMPT, f"{secs:.1f}s")
        check(results, "the proxy serves", bool(h) and h.get("service") == "rolling-context")
        if h:
            pids.add(h["pid"])
        log = os.path.join(home, ".claude", "rolling-context-proxy.log")
        check(results, "the proxy wrote its stdout log itself", os.path.exists(log))

        print("B. forced restart")
        secs = run_hook(dict(env, ROLLING_CONTEXT_FORCE_RESTART="1"))
        h2 = health(port)
        check(results, f"the hook's pipes close within {PROMPT}s", secs < PROMPT, f"{secs:.1f}s")
        check(results, "a new process serves", bool(h2) and h2.get("pid") != (h or {}).get("pid"),
              (h2 or {}).get("pid"))
        if h2:
            pids.add(h2["pid"])
    finally:
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        time.sleep(1)
        shutil.rmtree(root, ignore_errors=True)

    failed = results.count(False)
    print(f"\n{len(results) - failed}/{len(results)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
