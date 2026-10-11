#!/usr/bin/env python3
"""The handover a restart uses, and the loopback relay the status line needs.

  A. loopback relay - Claude Code sends a plugin's http:// fetch through
     HTTPS_PROXY too, so the mod's GET for /health reached the MITM port as
     `GET http://127.0.0.1:<port>/health` and was refused as "not a CONNECT":
     the status line said "proxy not running" for a proxy that was running.
     That request is now relayed to our own port; one for any other host or
     port is still refused (no open proxy).
  B. /drain - refused without the hook's header, and with an Origin (a page).
  C. long-lived streams - the desktop app's session event stream stays open
     for as long as the app is connected. It must not count as in flight, or
     the proxy never looks idle and an upgrade is deferred forever.
  D. handover - POST /drain releases the port at once, so a successor binds
     while the old process keeps its open stream; the old one exits once the
     successor answers /health, and only then cuts the stream.

  python tests/test_drain.py
"""
import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_toggle import check, clean_env, fake_home, free_port, wait_port  # noqa: E402

PROXY = os.path.join(HERE, "..", "proxy")
STREAM = "/v1/code/sessions/cse_test/worker/events/stream"
DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class Upstream(http.server.BaseHTTPRequestHandler):
    """Holds an event stream open until the client goes away; 404 otherwise."""
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        if not self.path.startswith(STREAM):
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("transfer-encoding", "chunked")
        self.end_headers()
        try:
            while True:
                self.wfile.write(b"7\r\n: ping\n\r\n")
                self.wfile.flush()
                time.sleep(0.5)
        except OSError:
            pass


def health(port, session=None):
    q = f"?session={session}" if session else ""
    with DIRECT.open(f"http://127.0.0.1:{port}/health{q}", timeout=5) as r:
        return json.loads(r.read())


def post_drain(port, headers):
    req = urllib.request.Request(f"http://127.0.0.1:{port}/drain", data=b"",
                                 method="POST", headers=headers)
    try:
        with DIRECT.open(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def raw(port, data, timeout=5):
    """Send bytes, return everything read until the peer closes."""
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    s.sendall(data)
    out = b""
    try:
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            out += chunk
    except OSError:
        pass
    s.close()
    return out


def refused(port):
    # Windows retries a refused SYN for ~2s before reporting it.
    try:
        socket.create_connection(("127.0.0.1", port), timeout=3).close()
        return False
    except ConnectionRefusedError:
        return True
    except OSError:
        return False


def start_proxy(env):
    return subprocess.Popen([sys.executable, "server.py"], cwd=PROXY, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    root = tempfile.mkdtemp(prefix="rc-drain-")
    up_port, port, mitm = free_port(), free_port(), free_port()
    upstream = http.server.ThreadingHTTPServer(("127.0.0.1", up_port), Upstream)
    upstream.daemon_threads = True
    upstream.handle_error = lambda *a: None   # a client that went away; expected
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    home = fake_home(root, {})
    env = clean_env(home, ROLLING_CONTEXT_PORT=str(port),
                    ROLLING_CONTEXT_HOME=os.path.join(root, "state"),
                    ROLLING_CONTEXT_UPSTREAM=f"http://127.0.0.1:{up_port}",
                    ROLLING_CONTEXT_MITM_PORT=str(mitm),
                    ROLLING_CONTEXT_DRAIN_SECONDS="3")
    first = start_proxy(env)
    second = None
    results = []
    try:
        assert wait_port(port) and wait_port(mitm), "proxy did not start"

        print("A. loopback relay through the MITM port")
        via = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": f"http://127.0.0.1:{mitm}"}))
        try:
            with via.open(f"http://127.0.0.1:{port}/health?session=s1", timeout=5) as r:
                body = json.loads(r.read())
        except Exception as e:
            body = {"error": repr(e)}
        check(results, "GET http://127.0.0.1:<port>/health through the proxy port answers",
              body.get("service") == "rolling-context", str(body)[:200])
        out = raw(mitm, b"GET http://127.0.0.1:%d/health HTTP/1.1\r\n"
                        b"Host: 127.0.0.1\r\nConnection: close\r\n\r\n" % port)
        check(results, "localhost by name, origin-form rewrite", b" 200 " in out.split(b"\r\n")[0])
        out = raw(mitm, b"GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\n\r\n")
        check(results, "another host is refused (no open proxy)", out == b"", out[:80])
        out = raw(mitm, b"GET http://127.0.0.1:%d/ HTTP/1.1\r\nHost: x\r\n\r\n" % up_port)
        check(results, "another loopback port is refused", out == b"", out[:80])

        print("B. /drain guards")
        code, _ = post_drain(port, {})
        check(results, "no X-Rolling-Context header -> 403", code == 403, code)
        code, _ = post_drain(port, {"X-Rolling-Context": "drain", "Origin": "https://evil.test"})
        check(results, "an Origin (a web page) -> 403", code == 403, code)
        check(results, "still serving after refused drains", health(port).get("pid") == first.pid)

        print("C. a long-lived stream is not in flight")
        stream = socket.create_connection(("127.0.0.1", port), timeout=10)
        stream.sendall(f"GET {STREAM} HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n".encode())
        got = stream.recv(4096)
        check(results, "the stream is open through the proxy", b"200" in got.split(b"\r\n")[0],
              got[:80])
        h = health(port)
        check(results, "active_requests is 0 while it is open", h.get("active_requests") == 0,
              h.get("active_requests"))
        check(results, "/health says it can drain", h.get("can_drain") is True)

        print("D. handover")
        code, body = post_drain(port, {"X-Rolling-Context": "drain"})
        check(results, "POST /drain accepted", code == 200 and body.get("draining") is True, body)
        released = False
        end = time.time() + 8
        while time.time() < end and not released:
            # Released = nothing accepts there any more. (A plain bind test
            # fails on Linux while the old process's connections are open,
            # whoever holds the listener.)
            try:
                socket.create_connection(("127.0.0.1", port), timeout=3).close()
                time.sleep(0.1)
            except ConnectionRefusedError:
                released = True
            except OSError:
                time.sleep(0.1)
        check(results, "the ports are released while the old process lives",
              released and first.poll() is None and refused(mitm),
              f"released={released} alive={first.poll() is None}")
        stream.settimeout(0.5)
        try:
            alive = bool(stream.recv(4096))
        except socket.timeout:
            alive = True
        except OSError:
            alive = False
        check(results, "the open stream survives until a successor is up", alive)

        second = start_proxy(dict(env, ROLLING_CONTEXT_SUCCESSOR="1"))
        assert wait_port(port), "successor did not start"
        h2 = health(port)
        check(results, "the successor serves", h2.get("pid") == second.pid, h2.get("pid"))
        try:
            first.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        check(results, "the old process exits once the successor answers",
              first.poll() is not None)
        stream.settimeout(3)
        try:
            rest = b""
            while True:
                chunk = stream.recv(4096)
                if not chunk:
                    break
                rest += chunk
            cut = True
        except OSError:
            cut = True
        except socket.timeout:
            cut = False
        check(results, "the stream is cut after, so the client reconnects", cut)
        stream.close()
    finally:
        for p in (first, second):
            if p and p.poll() is None:
                p.kill()
        upstream.shutdown()
        shutil.rmtree(root, ignore_errors=True)

    failed = results.count(False)
    print(f"\n{len(results) - failed}/{len(results)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
