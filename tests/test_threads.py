#!/usr/bin/env python3
"""Regression suite for message threads (beta message-threads-2026-08-12).

Claude Code sends a conversation's full history once, as thread "create", and
every later turn as thread "continue" carrying only the new messages. Before
this was handled the proxy saw 2-message requests it could neither compress nor
inject into, so compression only ever fired on a session start or resume.

  A. carried system message — the session preamble, inline tool definitions
     (tool_addition) and the effort setting survive a summary verbatim, with
     cache_control stripped, and a rolling re-compression does not duplicate
     them. Covers the bare-string system content replays use.
  C. a long autonomous tool loop (no plain user message for hundreds of
     messages) still compresses: the cut lands at the start of an assistant
     turn, the ack is left out, and every tool_use keeps its tool_result.
  D. a second proxy started on ports that are already serving exits instead
     of sharing them (Windows lets SO_REUSEADDR sockets co-listen).
  B. the rebase loop through a real proxy and the mock upstream:
       continue over the trigger      -> next continue answered 404
                                         thread_not_found ("compress")
       create with the full history   -> compressed in the background
       next continue                  -> 404 again ("inject")
       create with the full history   -> goes upstream with the summary
       continue on the new thread     -> passes through untouched
       proxy force-killed and restarted -> summary and thread anchors come
                                         back from disk: a full-history create
                                         still gets the summary, and the
                                         compressed thread is not rebased again

  python tests/test_threads.py
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PROXY = os.path.join(HERE, "..", "proxy")
MOCK = os.path.join(HERE, "mock_endpoint.py")
sys.path.insert(0, PROXY)

failures = []


def check(cond, label, detail=""):
    print(f"    {'PASS' if cond else 'FAIL'}  {label}")
    if detail:
        print(f"          {detail}")
    if not cond:
        failures.append(label)


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


# --- A ----------------------------------------------------------------------

def carried_system():
    print("\ncarried system message")
    from compressor import (_carried_system, prefix_len, CARRIED_MARKER, SUMMARY_MARKER,
                            ACK_TEXT)

    tool = {"type": "tool_addition", "cache_control": {"type": "ephemeral"},
            "tool": {"type": "tool_definition", "definition": {"name": "mcp__x__y"}}}
    history = [
        {"role": "user", "content": "hello"},
        {"role": "system", "content": "# Environment\npreamble",
         "output_config": {"effort": "medium"}},
        {"role": "assistant", "content": "hi"},
        {"role": "user", "content": "more"},
        {"role": "system", "content": [{"type": "text", "text": "tools arrived"}, tool]},
        {"role": "system", "content": "<total_tokens>1 left</total_tokens>",
         "output_config": {"effort": "high"}},
    ]
    carried = _carried_system(None, history)
    blocks = carried["content"]
    check(CARRIED_MARKER in blocks[0]["text"], "first block carries the marker")
    check(blocks[1] == {"type": "text", "text": "# Environment\npreamble"},
          "bare-string preamble is carried verbatim")
    check([b["type"] for b in blocks[2:]] == ["text", "tool_addition"],
          "tool change and the text describing it are carried, reminders are not",
          str([b["type"] for b in blocks]))
    check(all("cache_control" not in b for b in blocks), "cache_control stripped")
    check(carried.get("output_config") == {"effort": "high"}, "latest effort kept")

    prefix = [{"role": "user", "content": f"{SUMMARY_MARKER}\nsummary"}, carried,
              {"role": "assistant", "content": ACK_TEXT}]
    check(prefix_len(prefix + history) == 3, "prefix length counts the carried message")
    check(prefix_len(prefix[:1] + prefix[2:] + history) == 2, "plain prefix is still 2")
    check(prefix_len(history) == 0, "no prefix on a raw history")
    check(prefix_len(prefix[:2] + [{"role": "assistant", "content": "kept turn"}]) == 2,
          "a kept assistant turn is not mistaken for the ack")
    check(prefix_len(prefix[:1] + history) == 1, "summary with no ack and no carried is 1")

    again = _carried_system(carried, [{"role": "system", "content": "later reminder"}])
    check(again["content"] == blocks, "rolling re-compression does not duplicate it")
    check(_carried_system(None, [{"role": "user", "content": "x"}]) is None,
          "nothing to carry -> no message")


# --- C ----------------------------------------------------------------------

def tool_loop():
    print("\nlong tool loop")
    import compressor as C

    def call(i):
        return {"role": "assistant", "content": [
            {"type": "text", "text": f"step {i} " + "x" * 400},
            {"type": "tool_use", "id": f"tu{i}", "name": "Read", "input": {"i": i}}]}

    def result(i):
        return {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": f"tu{i}", "content": "y" * 400}]}

    msgs = [{"role": "user", "content": "do the long job"},
            {"role": "system", "content": "# Environment\npreamble"}]
    for i in range(60):
        msgs += [call(i), result(i)]
        if i % 7 == 3:
            msgs.append({"role": "system", "content": "<reminder>"})

    comp = C.RollingCompressor(trigger_tokens=3000, target_tokens=1000)
    seen = {}

    def fake_native(payload, messages, cut, auth):
        seen["cut"] = cut
        return "SUMMARY OF STEPS"
    comp._summarize_native = fake_native
    old_native = C.NATIVE_MODE
    C.NATIVE_MODE = True
    try:
        out = comp.compress(msgs, {}, real_token_count=6000, payload={"model": "m"})
    finally:
        C.NATIVE_MODE = old_native
    check(out is not None, "a tool loop with one user message is compressed")
    if out is None:
        return
    n = C.prefix_len(out)
    check(len(out) < len(msgs) / 2, "most of the loop was summarized",
          f"{len(msgs)} -> {len(out)}")
    check(out[n]["role"] == "assistant" and out[n - 1]["role"] in ("user", "system"),
          "kept part opens with an assistant turn right after the prefix",
          str([m["role"] for m in out[:n + 1]]))
    check(all(m.get("content") != C.ACK_TEXT for m in out), "no ack in front of it")
    check(out[1]["role"] == "system" and C.CARRIED_MARKER in out[1]["content"][0]["text"],
          "preamble carried")
    uses = {b["id"] for m in out if isinstance(m["content"], list)
            for b in m["content"] if b.get("type") == "tool_use"}
    results = {b["tool_use_id"] for m in out if isinstance(m["content"], list)
               for b in m["content"] if b.get("type") == "tool_result"}
    check(uses == results, "every kept tool_use has its tool_result and vice versa")
    roles = [m["role"] for m in out]
    check(all(not (a == "system" and b == "system") and not (a == "assistant" and b == "assistant")
              for a, b in zip(roles, roles[1:])), "no adjacent assistant/assistant turns")

    # a second, rolling compression of the already-compressed loop
    for i in range(60, 120):
        out += [call(i), result(i)]
    C.NATIVE_MODE = True
    try:
        again = comp.compress(out, {}, real_token_count=6000, payload={"model": "m"})
    finally:
        C.NATIVE_MODE = old_native
    check(again is not None and C.prefix_len(again) == 2
          and again[2]["role"] == "assistant", "rolls again over its own ack-less prefix",
          str([m["role"] for m in (again or [])[:4]]))
    check(sum(1 for m in again if C.SUMMARY_MARKER in str(m.get("content"))) == 1,
          "exactly one summary after rolling")

    # a plain user boundary still gets the ack
    chat = []
    for i in range(30):
        chat += [{"role": "user", "content": f"q{i} " + "z" * 300},
                 {"role": "assistant", "content": f"a{i} " + "z" * 300}]
    C.NATIVE_MODE = True
    try:
        res = comp.compress(chat, {}, real_token_count=6000, payload={"model": "m"})
    finally:
        C.NATIVE_MODE = old_native
    check(res[1]["content"] == C.ACK_TEXT and res[2]["role"] == "user"
          and C.prefix_len(res) == 2, "plain chat still cuts at a user turn with the ack")


# --- B ----------------------------------------------------------------------

def post(port, msgs, thread, session="sess-threads"):
    body = json.dumps({"model": "claude-test", "max_tokens": 100, "stream": True,
                       "messages": msgs, "thread": thread}).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/messages?beta=true", data=body, method="POST",
        headers={"content-type": "application/json",
                 "X-Claude-Code-Session-Id": session})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            text = r.read().decode()
            msg_id = None
            for line in text.splitlines():
                if line.startswith("data: ") and '"message_start"' in line:
                    msg_id = json.loads(line[6:])["message"]["id"]
            return r.status, msg_id, text
    except urllib.error.HTTPError as e:
        return e.code, None, e.read().decode()


def mock_requests(log):
    out = []
    with open(log, encoding="utf-8") as f:
        for line in f:
            d = json.loads(line)
            if d["kind"] == "request":
                out.append(d["detail"])
    return out


def turn(i, size=1500):
    return [{"role": "user", "content": f"turn {i}: " + "x" * size},
            {"role": "assistant", "content": f"answer {i}: " + "y" * size}]


def rebase_loop(root):
    print("\nrebase loop (proxy + mock upstream)")
    mock_port, proxy_port = free_port(), free_port()
    log = os.path.join(root, "mock.jsonl")
    home = os.path.join(root, "home")
    os.makedirs(os.path.join(home, ".claude"), exist_ok=True)
    with open(os.path.join(home, ".claude", "settings.json"), "w") as f:
        json.dump({"env": {}}, f)
    env = dict(os.environ)
    for k in list(env):
        if k.startswith("ROLLING_CONTEXT_") or k in ("RC_DUMP_DIR", "RC_LOG_PATH"):
            env.pop(k)
    env.update(HOME=home, USERPROFILE=home,
               ROLLING_CONTEXT_HOME=os.path.join(root, "switch"),
               ROLLING_CONTEXT_PORT=str(proxy_port),
               ROLLING_CONTEXT_MITM_PORT=str(free_port()),
               ROLLING_CONTEXT_UPSTREAM=f"http://127.0.0.1:{mock_port}",
               ROLLING_CONTEXT_TRIGGER="3000", ROLLING_CONTEXT_TARGET="400",
               ROLLING_CONTEXT_FAILURE_COOLDOWN="0")
    mock = subprocess.Popen([sys.executable, MOCK, str(mock_port), log],
                            env=dict(os.environ, MOCK_TOKENS_FROM_SIZE="1"))
    def start_proxy():
        return subprocess.Popen([sys.executable, "server.py"], cwd=PROXY, env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    proxy = start_proxy()
    try:
        assert wait_port(mock_port) and wait_port(proxy_port), "stack did not start"
        preamble = {"role": "system", "content": "# Environment\npreamble",
                    "output_config": {"effort": "medium"}}
        tools = {"role": "system", "content": [
            {"type": "text", "text": "tools arrived"},
            {"type": "tool_addition", "tool": {"type": "tool_definition",
                                               "definition": {"name": "mcp__x__y"}}}]}
        first = [{"role": "user", "content": "start"}, preamble,
                 {"role": "assistant", "content": "ok"}, {"role": "user", "content": "go"},
                 tools, {"role": "assistant", "content": "ok"}]

        status, id1, _ = post(proxy_port, first, {"type": "create"})
        check(status == 200 and id1, "create under the trigger passes", f"{status} {id1}")

        status, id2, _ = post(proxy_port, turn(1, 10)[:1],
                              {"type": "continue", "previous_message_id": id1})
        check(status == 200, "continue passes through while nothing is pending")

        n_before = len(mock_requests(log))
        status, _, body = post(proxy_port, turn(2, 10)[:1],
                               {"type": "continue", "previous_message_id": id2})
        check(status == 404 and "thread_not_found" in body,
              "continue after an over-trigger turn -> 404 thread_not_found",
              f"{status} {body[:120]}")
        check(len(mock_requests(log)) == n_before, "the 404 never reached upstream")

        history = first + sum((turn(i) for i in range(1, 7)), [])
        status, id4, _ = post(proxy_port, history + [{"role": "user", "content": "now"}],
                              {"type": "create"})
        check(status == 200, "replayed create with full history passes")

        deadline = time.time() + 20
        while time.time() < deadline and not any(
                r["compaction"] for r in mock_requests(log)):
            time.sleep(0.2)
        time.sleep(0.5)
        check(any(r["compaction"] for r in mock_requests(log)),
              "the full history was compressed in the background")

        history += [{"role": "user", "content": "now"},
                    {"role": "assistant", "content": "done"}]
        status, _, body = post(proxy_port, [{"role": "user", "content": "next"}],
                               {"type": "continue", "previous_message_id": id4})
        check(status == 404 and "thread_not_found" in body,
              "continue once the summary is ready -> 404 (inject)")

        status, id7, _ = post(proxy_port, history + [{"role": "user", "content": "next"}],
                              {"type": "create"})
        sent = [r for r in mock_requests(log) if r["thread"] == "create"][-1]
        check(status == 200 and sent["carries_summary"],
              "the replayed create goes upstream with the summary",
              f"{len(history) + 1} messages held, {sent['messages']} sent")
        check(sent["messages"] < len(history) + 1, "and with fewer messages")
        check(sent["roles"][:3] == ["user", "system", "assistant"]
              and sent["tool_additions"] == 1,
              "preamble and tool definitions ride along in the carried message",
              str(sent["roles"][:4]))

        status, _, _ = post(proxy_port, [{"role": "user", "content": "after"}],
                            {"type": "continue", "previous_message_id": id7})
        last = mock_requests(log)[-1]
        check(status == 200 and last["thread"] == "continue" and last["messages"] == 1,
              "the next continue builds on the compressed thread untouched")

        status, _, _ = post(proxy_port, [{"role": "user", "content": "x"}],
                            {"type": "continue", "previous_message_id": "msg_unknown"})
        check(status == 200, "a thread the proxy never saw is left alone at first")

        # The restart hook force-kills: no exit handler runs, so the store must
        # already be on disk by the time it does.
        store_file = os.path.join(root, "switch", f"store-{proxy_port}.json")
        deadline = time.time() + 10
        while time.time() < deadline and not os.path.exists(store_file):
            time.sleep(0.2)
        time.sleep(1.5)
        check(os.path.exists(store_file), "compressions are saved to disk",
              store_file)
        proxy.kill()
        proxy.wait(10)
        proxy = start_proxy()
        check(wait_port(proxy_port), "proxy restarts")

        # id7's continue reported 150K (the mock's continue figure), so before
        # the kill its chain was flagged over the trigger. Only a restored
        # anchor knows that; a lost one would let this through as unknown.
        n_before = len(mock_requests(log))
        status, _, body = post(proxy_port, [{"role": "user", "content": "after restart"}],
                               {"type": "continue", "previous_message_id": id7})
        check(status == 404 and "thread_not_found" in body
              and len(mock_requests(log)) == n_before,
              "after a restart the thread anchor and its over-trigger flag survive",
              f"{status} {body[:80]}")

        status, _, _ = post(proxy_port, history + [{"role": "user", "content": "resume"}],
                            {"type": "create"})
        sent = [r for r in mock_requests(log) if r["thread"] == "create"][-1]
        check(status == 200 and sent["carries_summary"]
              and sent["messages"] < len(history) + 1,
              "after a restart a full-history create still gets the summary",
              f"{len(history) + 1} messages held, {sent['messages']} sent")
    finally:
        proxy.terminate()
        mock.terminate()
        proxy.wait(10)
        mock.wait(10)


# --- D ----------------------------------------------------------------------

def exclusive_ports(root):
    print("\nexclusive ports")
    home = os.path.join(root, "home-excl")
    os.makedirs(os.path.join(home, ".claude"), exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("ROLLING_CONTEXT_")}
    core, mitm = free_port(), free_port()
    env.update(HOME=home, USERPROFILE=home, ROLLING_CONTEXT_HOME=os.path.join(root, "sw-excl"),
               ROLLING_CONTEXT_PORT=str(core), ROLLING_CONTEXT_MITM_PORT=str(mitm),
               ROLLING_CONTEXT_UPSTREAM="http://127.0.0.1:9")

    def start(e):
        return subprocess.Popen([sys.executable, "server.py"], cwd=PROXY, env=e,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    first = start(env)
    procs = [first]
    try:
        check(wait_port(core) and wait_port(mitm), "first proxy holds both ports")
        for label, port_env in (("core", {}), ("MITM", {"ROLLING_CONTEXT_PORT": str(free_port())})):
            second = start(dict(env, **port_env))
            procs.append(second)
            try:
                code = second.wait(20)
            except subprocess.TimeoutExpired:
                code = None
            check(code not in (None, 0),
                  f"a second proxy on the same {label} port exits", f"exit={code}")
        check(first.poll() is None and wait_port(core, 2) and wait_port(mitm, 2),
              "the first proxy keeps serving")
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
                p.wait(10)


if __name__ == "__main__":
    root = tempfile.mkdtemp(prefix="rc-threads-")
    try:
        carried_system()
        tool_loop()
        rebase_loop(root)
        exclusive_ports(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    if failures:
        print(f"\nFAILED ({len(failures)})")
        sys.exit(1)
    print("\nALL THREAD CHECKS PASSED")
