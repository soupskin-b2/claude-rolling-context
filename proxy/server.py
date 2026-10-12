"""
Claude Rolling Context Proxy

A transparent proxy between Claude Code and the Anthropic API.
Compresses old messages in the background using Haiku, keeping recent messages
verbatim. Zero latency — compression runs async, applied on the next request.

Uses content-based matching: hashes each message, recognizes previously compressed
messages by their content, and replaces them with the compressed version.
No sessions, no fingerprints — just content recognition.

Pure stdlib — no external dependencies needed.
"""

import os as _os
import sys as _sys

# Started by start-proxy.ps1 with no redirection: Start-Process -Redirect*
# makes the child inherit the hook's own stdout/stderr pipes, and whoever ran
# the hook (`/roll restart`, the SessionStart hook) waits on them until this
# long-lived process exits - in practice, until its timeout. The hook names
# the files instead, and they are opened here, before any import can fail.
_stdio_log = _os.environ.pop("ROLLING_CONTEXT_STDIO_LOG", "")
if _stdio_log:
    try:
        for _fd, _path in ((1, _stdio_log), (2, _stdio_log + ".err")):
            _h = _os.open(_path, _os.O_WRONLY | _os.O_CREAT | _os.O_TRUNC, 0o644)
            _os.dup2(_h, _fd)
            _os.close(_h)
        _sys.stdout = open(1, "w", encoding="utf-8", errors="replace", buffering=1, closefd=False)
        _sys.stderr = open(2, "w", encoding="utf-8", errors="replace", buffering=1, closefd=False)
    except OSError:
        pass

import hashlib
import itertools
import json
import os
from collections import OrderedDict
import sys
import logging
import logging.handlers
import threading
import time
import socket
import ssl
import http.client
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

import endpoints
import switch
from compressor import RollingCompressor

class FlushRotatingHandler(logging.handlers.RotatingFileHandler):
    def emit(self, record):
        super().emit(record)
        self.flush()


def _log_setting(name, default):
    """Env first, then settings.json — same precedence as every other knob."""
    return os.environ.get(name) or endpoints.settings_env().get(name) or default


# Was hardcoded DEBUG with an unrotated FileHandler, while start-proxy.sh
# redirected stdout into a second file. Every line landed on disk twice and
# nothing ever pruned either one; a reporter hit 5.9 GB in rolling-context-proxy.log
# on a proxy left up for days (issue #7).
_LEVEL = getattr(logging, str(_log_setting("ROLLING_CONTEXT_LOG_LEVEL", "INFO")).upper(),
                 logging.INFO)
try:
    _MAX_MB = max(1, int(_log_setting("ROLLING_CONTEXT_LOG_MAX_MB", "10")))
except (TypeError, ValueError):
    _MAX_MB = 10
try:
    _BACKUPS = max(0, int(_log_setting("ROLLING_CONTEXT_LOG_BACKUPS", "3")))
except (TypeError, ValueError):
    _BACKUPS = 3

_log_path = os.path.join(os.path.expanduser("~"), ".claude", "rolling-context-debug.log")
# encoding is explicit: without it Windows falls back to cp1252 and the em
# dashes in these log messages land as mojibake.
_log_handler = FlushRotatingHandler(
    _log_path, mode="a", encoding="utf-8",
    maxBytes=_MAX_MB * 1024 * 1024, backupCount=_BACKUPS,
)
_log_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
_log_handler.setLevel(_LEVEL)

# stdout is redirected to rolling-context-proxy.log by the start hook, which
# only truncates on restart — so it must not carry routine traffic or a
# long-lived proxy grows without bound. WARNING and above keeps crashes and
# real problems visible there without duplicating the whole stream.
_stream_handler = logging.StreamHandler(sys.stdout)
_stream_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
_stream_handler.setLevel(max(_LEVEL, logging.WARNING))

logging.basicConfig(level=_LEVEL, handlers=[_stream_handler, _log_handler])
log = logging.getLogger("rolling-context")
log.info(
    f"Logging at {logging.getLevelName(_LEVEL)}; {_log_path} rotates at "
    f"{_MAX_MB} MB x {_BACKUPS}. Set ROLLING_CONTEXT_LOG_LEVEL=DEBUG for detail."
)

LISTEN_PORT = endpoints.LISTEN_PORT
# The MITM front-end port — Claude Code reaches us here via HTTPS_PROXY, which
# (unlike ANTHROPIC_BASE_URL) keeps the destination api.anthropic.com and so
# never trips Claude Code's Remote Control / GrowthBook gate. See mitm_frontend.
MITM_PORT = int(os.environ.get("ROLLING_CONTEXT_MITM_PORT") or "5590")
CA_DIR = os.path.join(os.path.expanduser("~"), ".claude", "proxy-ca")
# How long a SIGTERM waits for in-flight requests before the process exits.
DRAIN_SECONDS = float(os.environ.get("ROLLING_CONTEXT_DRAIN_SECONDS") or "20")
# How long a draining proxy waits for its successor before it exits anyway.
SUCCESSOR_WAIT_SECONDS = 15.0
# Set by main(): starts a drain from a request handler (POST /drain).
_drain_handler = None


def _successor_listening():
    """True once another of our proxies answers /health on our port."""
    try:
        conn = http.client.HTTPConnection("127.0.0.1", LISTEN_PORT, timeout=0.5)
        conn.request("GET", "/health")
        body = json.loads(conn.getresponse().read() or b"{}")
        conn.close()
    except Exception:
        return False
    return body.get("service") == "rolling-context" and body.get("pid") != os.getpid()


def _plugin_version() -> str:
    """Our version, read from the proxy/VERSION file next to this file.

    The plugin manifest carries no pinned version, so the plugin manager
    versions installs by commit SHA and every commit is an update with no
    version bump. The proxy keeps its own version here instead: bump
    proxy/VERSION, and only that file, when proxy/ code changes, so the
    start hook restarts a stale proxy. Falls back to the manifest for
    checkouts that still pin one.

    Reported on /health so the start hook can tell "the proxy is serving" from
    "the proxy is serving, but it is the version you just replaced" without
    consulting a side file that a crash can leave lying about.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        with open(os.path.join(root, "proxy", "VERSION"), encoding="utf-8-sig") as f:
            text = f.read().strip()
        if text:
            return text.split()[0]
    except Exception:
        pass
    try:
        with open(os.path.join(root, ".claude-plugin", "plugin.json"), encoding="utf-8-sig") as f:
            return str(json.load(f).get("version") or "unknown")
    except Exception:
        return "unknown"


VERSION = _plugin_version()

# Requests this process is serving right now — the number the start hook asks
# for before it decides to replace us on an upgrade. Killing a proxy that is
# mid-stream cuts every open /v1/messages response in every session routed
# through it (nestor-plugins issue #1). The hook waits for this to hit zero.
_inflight = 0
_inflight_lock = threading.Lock()
_draining = False
# Set once a successor has been handed the store: from then on this process
# leaves store-<port>.json to it.
_handed_over = False

# Streams that stay open for as long as a client is connected - the desktop
# app's session event stream (/v1/code/sessions/<id>/worker/events/stream)
# reconnects whenever it drops. Counting them as in flight made the proxy
# look permanently busy, so an upgrade never found it idle. They are left
# out: a drain cuts them last, once a successor is listening for the
# reconnect.
_LONG_LIVED = ("/events/stream",)


def _long_lived(path):
    path = urlparse(path).path
    return any(path.endswith(tail) for tail in _LONG_LIVED)


_long_lived_open = 0


def _track_request(method):
    """Run one BaseHTTPRequestHandler do_* body under the in-flight counter."""
    def wrap(fn):
        def inner(self):
            global _inflight, _long_lived_open
            if _long_lived(self.path):
                with _inflight_lock:
                    _long_lived_open += 1
                try:
                    return fn(self)
                finally:
                    with _inflight_lock:
                        _long_lived_open -= 1
            with _inflight_lock:
                _inflight += 1
            try:
                return fn(self)
            finally:
                with _inflight_lock:
                    _inflight -= 1
        inner.__name__ = fn.__name__
        return inner
    return wrap

UPSTREAM_URL = endpoints.load_upstream(LISTEN_PORT)
# Live: /config (pluginConfigs in settings.json) first, then the environment
# this proxy was started with, then the defaults. _refresh_limits() re-reads
# them on every request, so a change in /config applies from the next one.
TRIGGER_TOKENS = switch.trigger_tokens()[0]
TARGET_TOKENS = switch.target_tokens()[0]
# Empty = native mode compresses with the session's own model (prompt-cache
# hit); set to pin a specific summarizer model.
SUMMARIZER_MODEL = os.environ.get("ROLLING_CONTEXT_MODEL") or ""
# After a failed compression, wait this long before trying again — otherwise a
# failing summarizer (e.g. rate-limited) gets re-hammered on every request.
# Scoped per conversation: a single global timestamp meant one conversation's
# failure silenced every other conversation on the machine for 5 minutes.
FAILURE_COOLDOWN = int(os.environ.get("ROLLING_CONTEXT_FAILURE_COOLDOWN") or "300")
# Ceiling on concurrent background compactions across all conversations. This
# exists to bound cost and upstream rate-limit pressure, NOT to serialise work:
# before v1.11.4 the limit was effectively 1 and applied globally, so one
# conversation compacting starved every other conversation of a compression
# entry — which is why only ~21% of eligible requests were ever injected (#8).
MAX_CONCURRENT_COMPACTIONS = max(
    1, int(os.environ.get("ROLLING_CONTEXT_MAX_CONCURRENT") or "4"))

ssl_ctx = endpoints.outbound_ssl_context()
_parsed_upstream = urlparse(UPSTREAM_URL)
UPSTREAM_PATH = _parsed_upstream.path or ""


def _join_path(upstream_path: str, request_path: str) -> str:
    """Join upstream path with request path, handling edge cases."""
    if not upstream_path:
        return request_path
    if not request_path or request_path == "/":
        return upstream_path
    if upstream_path.endswith("/") and request_path.startswith("/"):
        return upstream_path[:-1] + request_path
    if not upstream_path.endswith("/") and not request_path.startswith("/"):
        return upstream_path + "/" + request_path
    return upstream_path + request_path


compressor = RollingCompressor(
    trigger_tokens=TRIGGER_TOKENS,
    target_tokens=TARGET_TOKENS,
    summarizer_model=SUMMARIZER_MODEL,
)


def _refresh_limits():
    """Pick up a trigger/target changed in /config since the last request."""
    global TRIGGER_TOKENS, TARGET_TOKENS
    trigger, target = switch.trigger_tokens()[0], switch.target_tokens()[0]
    if (trigger, target) != (TRIGGER_TOKENS, TARGET_TOKENS):
        log.info(f"[CONFIG] trigger {TRIGGER_TOKENS:,} -> {trigger:,}, "
                 f"target {TARGET_TOKENS:,} -> {target:,}")
        TRIGGER_TOKENS, TARGET_TOKENS = trigger, target
        compressor.trigger_tokens, compressor.target_tokens = trigger, target


def _upstream_conn():
    """Create a connection to the upstream server."""
    if _parsed_upstream.scheme == "https":
        return http.client.HTTPSConnection(
            _parsed_upstream.hostname,
            _parsed_upstream.port or 443,
            context=ssl_ctx,
            timeout=600,
        )
    else:
        return http.client.HTTPConnection(
            _parsed_upstream.hostname,
            _parsed_upstream.port or 80,
            timeout=600,
        )


# ---------------------------------------------------------------------------
# Content-based matching
# ---------------------------------------------------------------------------

import re

_VOLATILE_TAGS_RE = re.compile(
    r"<(?:system-reminder|local-command-caveat|local-command-stdout|"
    r"available-deferred-tools)>.*?</(?:system-reminder|local-command-caveat|"
    r"local-command-stdout|available-deferred-tools)>",
    re.DOTALL,
)


def _strip_volatile_tags(text: str) -> str:
    """Strip Claude Code's dynamic tags that change between requests."""
    return _VOLATILE_TAGS_RE.sub("", text)


# --- per-session toggle (issue #6) ------------------------------------------
# The session's own choice lives in a state file keyed by its Claude Code
# session id (switch.session_disabled). Before that, /rolling-context:off
# printed a marker meant to land in the transcript inside
# <local-command-stdout>; it is still honoured, but only in a user message
# that IS command output (_command_output_texts), so a tool result that quotes
# the marker between the tags — reading switch.py, say — toggles nothing.

_STDOUT_BLOCK_RE = re.compile(
    r"<local-command-stdout>(.*?)</local-command-stdout>", re.DOTALL
)
_SESSION_MARKER_RE = re.compile(r"<<rolling-context:session-(off|on)>>")


class SessionToggleStore:
    """Latches each session's toggle against its Claude Code session id.

    The marker only ever appears in the conversation that ran the command, but
    Claude Code sends X-Claude-Code-Session-Id on every request and a subagent
    inherits its parent's session id (subagents get their own transcript, not
    their own session). Latching the marker against that id is what makes
    /rolling-context:off reach the subagents a session spawns.

    It also outlives the marker: if a conversation is /compact'ed and the
    marker is summarized away, the latch still holds.

    Bounded and lossy on purpose. Overflowing or restarting the proxy forgets a
    session, which costs savings for one more request until the marker is seen
    again — never correctness.
    """

    def __init__(self, limit=512):
        self._lock = threading.Lock()
        self._state = OrderedDict()
        self._limit = limit

    def set(self, session_id: str, disabled: bool):
        if not session_id:
            return
        with self._lock:
            previous = self._state.get(session_id)
            self._state[session_id] = disabled
            self._state.move_to_end(session_id)
            while len(self._state) > self._limit:
                self._state.popitem(last=False)
            return previous != disabled

    def get(self, session_id: str):
        if not session_id:
            return None
        with self._lock:
            if session_id not in self._state:
                return None
            self._state.move_to_end(session_id)
            return self._state[session_id]

    def __len__(self):
        return len(self._state)


session_toggles = SessionToggleStore()


def _command_output_texts(message: dict):
    """The texts of a message that are real slash-command output.

    Claude Code records a local command's output as a user message whose text
    starts with <local-command-stdout>. Anything else that merely contains the
    tag (a tool result quoting this proxy's source, a file read, a pasted log)
    is not command output, however its tags happen to pair up.
    """
    if message.get("role") != "user":
        return
    content = message.get("content", "")
    if isinstance(content, str):
        texts = [content]
    elif isinstance(content, list):
        texts = [b.get("text") for b in content
                 if isinstance(b, dict) and b.get("type") == "text"]
    else:
        return
    for text in texts:
        if isinstance(text, str) and text.lstrip().startswith("<local-command-stdout>"):
            yield text


def _session_disabled(messages: list):
    """Newest legacy /rolling-context session marker in this conversation.

    Returns True (off), False (on), or None (never set — follow the machine
    setting). Scans newest-first so the last toggle wins. Session state files
    (switch.session_disabled) take precedence; this only covers transcripts
    from before them.
    """
    for msg in reversed(messages):
        for text in _command_output_texts(msg):
            # Cheap reject first: the vast majority of messages never match.
            if "rolling-context:session-" not in text:
                continue
            for block in reversed(_STDOUT_BLOCK_RE.findall(text)):
                found = _SESSION_MARKER_RE.findall(block)
                if found:
                    return found[-1] == "off"
    return None


def _normalize_content(content):
    """Strip volatile metadata (cache_control, system-reminder) for stable hashing."""
    if isinstance(content, str):
        return _strip_volatile_tags(content)
    if isinstance(content, list):
        result = []
        for block in content:
            if isinstance(block, dict):
                b = {}
                for k, v in block.items():
                    if k == "cache_control":
                        continue
                    if k == "content" and isinstance(v, (list, str)):
                        b[k] = _normalize_content(v)
                    elif k == "text" and isinstance(v, str):
                        b[k] = _strip_volatile_tags(v)
                    else:
                        b[k] = v
                result.append(b)
            else:
                result.append(block)
        return result
    return content


def _hash_message(msg: dict) -> str:
    """Stable hash of a message, ignoring cache_control metadata."""
    role = msg.get("role", "")
    content = _normalize_content(msg.get("content", ""))
    if not isinstance(content, str):
        content = json.dumps(content, sort_keys=True)
    raw = f"{role}:{content}"
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:16]


def _hash_messages(messages: list) -> list:
    return [_hash_message(m) for m in messages]


class CompressionStore:
    """Content-based compression tracking. No sessions, no fingerprints, no keys.

    Stores a list of compressions. Each has original_hashes (what was compressed)
    and prefix (the replacement). On ANY request, scans messages — if the hashes
    match a stored compression, replaces them with the prefix.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._compressions = []  # list of compression entries
        self._eids = itertools.count(1)

    def find_match(self, msg_hashes: list, messages: list = None):
        """Find a compression whose hash chain appears in msg_hashes.

        Returns the match whose chain ends furthest into the request
        (latest compression = covers the most history).
        Replaces everything up to and including the match, since the
        compression already contains a summary of everything before it.
        """
        with self._lock:
            best = None
            best_end = -1  # position in msg_hashes where the match ends
            for entry in self._compressions:
                oh = entry["original_hashes"]
                if not oh:
                    continue
                # Search for the hash chain in msg_hashes
                chain_len = len(oh)
                found = False
                for start in range(len(msg_hashes) - chain_len + 1):
                    if msg_hashes[start:start + chain_len] == oh:
                        end = start + chain_len
                        if end > best_end:
                            best = entry
                            best_end = end
                        found = True
                        break
                # DEBUG, not WARNING: a scan miss is the ordinary case, emitted
                # per stored entry per request, and it dumps conversation
                # content to disk. At WARNING it was the single largest
                # contributor to a 5.9 GB log (issue #7) — 56,960 lines against
                # 53 real injections. The isEnabledFor guard means the diff is
                # not even computed unless someone asked for DEBUG.
                if (not found and chain_len <= len(msg_hashes)
                        and log.isEnabledFor(logging.DEBUG)):
                    mismatches = []
                    for i in range(min(chain_len, len(msg_hashes))):
                        if oh[i] != msg_hashes[i]:
                            mismatches.append(i)
                    log.debug(
                        f"[MATCH] No match: chain={chain_len} req={len(msg_hashes)} "
                        f"mismatches={len(mismatches)} at positions: "
                        f"{mismatches[:10]}{'...' if len(mismatches) > 10 else ''}"
                    )
                    # Dump content of first mismatched message for debugging
                    if mismatches and messages and entry.get("_debug_messages"):
                        idx = mismatches[0]
                        stored_msg = entry["_debug_messages"][idx] if idx < len(entry["_debug_messages"]) else None
                        incoming_msg = messages[idx] if idx < len(messages) else None
                        if stored_msg and incoming_msg:
                            s_content = str(stored_msg.get("content", ""))[:500]
                            i_content = str(incoming_msg.get("content", ""))[:500]
                            log.debug(
                                f"[MATCH] Mismatch at [{idx}] role={stored_msg.get('role')}:\n"
                                f"  STORED:   {s_content}\n"
                                f"  INCOMING: {i_content}"
                            )
            return best, best_end

    def add(self) -> dict:
        entry = {
            "eid": next(self._eids),  # identity for thread chains (never reused)
            "original_hashes": [],   # hashes of original messages we replaced
            "prefix": None,          # compressed replacement messages
            "pending": None,         # pending compression result
            "pending_hashes": None,  # hashes for pending
            "thread": None,          # background compression thread
        }
        with self._lock:
            self._compressions.append(entry)
        return entry

    def remove(self, entry: dict):
        with self._lock:
            self._compressions = [e for e in self._compressions if e is not entry]
        if entry.get("prefix") is not None:
            _persist_soon()

    @property
    def compressions(self):
        return self._compressions


store = CompressionStore()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _forward_headers(req_headers: dict, body: bytes = None, strip_encoding: bool = False) -> dict:
    headers = {}
    for key, value in req_headers.items():
        lower = key.lower()
        if lower in ("host", "transfer-encoding", "connection", "content-length"):
            continue
        if strip_encoding and lower == "accept-encoding":
            continue
        headers[key] = value
    if body is not None:
        headers["content-length"] = str(len(body))
    log.debug(f"[HDR] Forwarding headers: {list(headers.keys())}")
    return headers


def get_passthrough_headers(req_headers: dict) -> dict:
    headers = {}
    for key, value in req_headers.items():
        lower = key.lower()
        if lower not in ("host", "content-length", "transfer-encoding"):
            headers[key] = value
    return headers


def _validate_tool_pairs(messages: list) -> list:
    tool_use_ids = set()
    valid_from = 0
    for i, msg in enumerate(messages):
        content = msg.get("content", "")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "tool_use":
                        tool_use_ids.add(block.get("id", ""))
                    elif block.get("type") == "tool_result":
                        if block.get("tool_use_id", "") not in tool_use_ids:
                            valid_from = i + 1
    if valid_from > 0:
        log.info(f"Dropping {valid_from} messages with orphaned tool_result references")
    return messages[valid_from:]


# Failure timestamps per conversation, not one global clock. The global version
# meant a single rate-limited conversation put every other conversation on the
# machine into a FAILURE_COOLDOWN-long silence (#8).
_compression_failures = {}          # conversation key -> time.time() of failure
_failure_lock = threading.Lock()

# Injection accounting, so "is this thing actually working?" is answerable from
# the log at INFO instead of requiring DEBUG and a transcript scrape (#8).
_stats = {"injected": 0, "missed": 0}
_stats_lock = threading.Lock()

# Cache-integrity sentinel. The design promises that between compactions every
# request is append-only against what was actually SENT upstream last turn —
# that is what makes the prompt cache hit. This verifies the promise instead of
# assuming it: the one sanctioned prefix rewrite is a compaction injection;
# any other divergence is a cache bust and gets flagged at WARNING.
# Hashes are the normalized kind (_hash_message), so cache_control breakpoint
# moves — which don't change tokens — don't false-alarm.
_last_sent = {}                     # conversation key -> hashes of messages sent upstream
_last_sent_lock = threading.Lock()
_cache_stats = {"append_only": 0, "compaction_rewrites": 0,
                "tail_divergences": 0, "busts": 0}


def _conversation_key(session_id: str, msg_hashes: list) -> str:
    """Identify a conversation for concurrency accounting ONLY.

    Never used for matching — the store stays content-addressed, so this cannot
    reintroduce session coupling.

    The session id alone is NOT enough. A subagent inherits its parent's
    X-Claude-Code-Session-Id (it gets its own transcript, not its own session),
    so keying on the id would collapse a whole agent team into one conversation
    and they would block each other — #8 again, scoped to the team. Measured
    before this was fixed: a parent plus two subagents produced 2 compactions
    instead of 3, and neither subagent ever received a summary.

    The first message hash separates transcripts within a session, and is stable
    for the life of a transcript because Claude Code always sends the full
    history. The session id still scopes it, so two sessions that happen to open
    with identical text stay distinct.
    """
    head = msg_hashes[0] if msg_hashes else "empty"
    if session_id:
        return "sid:" + session_id + ":" + head
    return "msg:" + head


def _note_compression_failure(owner: str):
    now = time.time()
    with _failure_lock:
        _compression_failures[owner] = now
        if len(_compression_failures) > 512:
            cutoff = now - FAILURE_COOLDOWN
            for k in [k for k, v in _compression_failures.items() if v < cutoff]:
                _compression_failures.pop(k, None)


def _cooldown_remaining(owner: str) -> float:
    with _failure_lock:
        failed_at = _compression_failures.get(owner, 0.0)
    return FAILURE_COOLDOWN - (time.time() - failed_at)


# Wall-clock time of the last compression injection — the moment old messages
# actually left the model's context. Exposed at /lean/status so companion
# plugins (nestor-lean) can invalidate "the model already saw this" knowledge.
_last_injection_ts = 0.0


# --- message threads (beta message-threads-2026-08-12) ----------------------
# Claude Code sends the full history once, with thread {"type": "create"}, and
# every later turn as thread {"type": "continue", "previous_message_id": ...}
# carrying only the new messages: the server holds the rest. A proxy that
# compresses what it can see cannot act on a continue, so before this the only
# compressions were the rare ones that landed on a session start or resume.
#
# The way back to the full history is the client's own: a continue answered
# with 404 thread_not_found is resent at once as a create with the whole
# transcript (Claude Code: "replay_not_found"), with no lasting penalty. So the
# proxy asks for it exactly when it has a use for it:
#   - "compress": a continue reported more than the trigger, and nothing is
#     compressing this conversation — the create feeds the normal compression.
#   - "inject":   a compression for this conversation is ready and this chain
#     has not carried it yet — the create gets the summary injected, and the
#     server starts a new, small thread from it. Later continues extend that.
# Each chain (one server-side thread, followed through its response message
# ids) remembers which compression it was created with.
_THREAD_NOT_FOUND = json.dumps({
    "type": "error",
    "error": {
        "type": "not_found_error",
        "message": ("thread_not_found: rolling-context is rebasing this "
                    "conversation onto its compressed history; resend as create"),
        "details": {"error_code": "thread_not_found"},
    },
}).encode()

_thread_chains = OrderedDict()      # response message id -> chain dict
_thread_forced = {}                 # conversation key -> time of last "compress" force
_threads_lock = threading.Lock()


def _new_chain(convo_key, injected_eid=None) -> dict:
    return {"convo_key": convo_key, "injected": injected_eid, "over": False}


def _chain_for(message_id):
    with _threads_lock:
        return _thread_chains.get(message_id) if message_id else None


def _remember_chain(message_id, chain: dict):
    if not message_id:
        return
    with _threads_lock:
        _thread_chains[message_id] = chain
        while len(_thread_chains) > 4096:
            _thread_chains.popitem(last=False)
    _persist_soon()


# ---------------------------------------------------------------------------
# Persistence. Ready compressions and thread chains live in memory, so a proxy
# restart used to throw them all away: every long conversation then went
# upstream once at full size (its whole local transcript, nothing to swap in)
# and paid a fresh cache write plus a from-scratch compaction. They are now
# mirrored to disk and reloaded on start. Writes are debounced and atomic —
# the restart hook force-kills the process, so there is no exit hook to rely on.
# ---------------------------------------------------------------------------

# One file per listen port: a second proxy (a test run, a custom-endpoint
# instance) must never load or overwrite the main proxy's summaries.
STORE_PATH = os.environ.get("ROLLING_CONTEXT_STORE_PATH") or os.path.join(
    switch.flag_dir(), f"store-{LISTEN_PORT}.json")
_STORE_VERSION = 1
_STORE_MAX_ENTRIES = 64           # newest ready compressions kept on disk
_STORE_MAX_CHAINS = 1024          # newest thread anchors kept on disk
_STORE_MAX_AGE = 14 * 86400       # thread anchors older than this are dead
# How long an unused compression lives is the user's call (the "Delete unused
# compressions after" row in /config, switch.retention()): _prune_expired drops
# it from memory and so from disk. A compression is "used" when it is made and
# each time it is injected into a request.
_PRUNE_INTERVAL = 60
_persist_dirty = threading.Event()


def _persist_soon():
    _persist_dirty.set()


def _snapshot_state() -> dict:
    with store._lock:
        ready = [e for e in store._compressions if e.get("prefix") is not None]
    ready = ready[-_STORE_MAX_ENTRIES:]
    entries = [{
        "eid": e["eid"],
        "owner": e.get("owner"),
        "original_hashes": e["original_hashes"],
        "prefix": e["prefix"],
        "rebase_requested": bool(e.get("rebase_requested")),
        "saved_at": e.setdefault("saved_at", time.time()),
        "last_used": _last_used(e),
    } for e in ready]
    with _threads_lock:
        items = list(_thread_chains.items())[-_STORE_MAX_CHAINS:]
    chains, anchors, index = [], {}, {}
    for mid, chain in items:
        k = id(chain)
        if k not in index:
            index[k] = len(chains)
            chains.append(dict(chain))
        anchors[mid] = index[k]
    return {"version": _STORE_VERSION, "saved_at": time.time(),
            "entries": entries, "chains": chains, "anchors": anchors}


def _write_state():
    state = _snapshot_state()
    os.makedirs(os.path.dirname(STORE_PATH), exist_ok=True)
    tmp = STORE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, separators=(",", ":"))
    os.replace(tmp, STORE_PATH)


def _persist_loop():
    while True:
        _persist_dirty.wait()
        time.sleep(1.0)               # coalesce bursts (promotion + chain + flag)
        _persist_dirty.clear()
        if _handed_over:
            continue
        try:
            _write_state()
        except Exception as e:
            log.warning(f"[STORE] Could not save {STORE_PATH}: {e}")


def _load_state():
    try:
        with open(STORE_PATH, encoding="utf-8") as f:
            state = json.load(f)
    except FileNotFoundError:
        return
    except Exception as e:
        log.warning(f"[STORE] Ignoring unreadable {STORE_PATH}: {e}")
        return
    if state.get("version") != _STORE_VERSION:
        log.info(f"[STORE] Ignoring {STORE_PATH} (version {state.get('version')})")
        return
    now = time.time()
    keep_for = switch.retention()[1]
    max_eid = 0
    loaded = []
    expired = 0
    for d in state.get("entries", []):
        last_used = d.get("last_used") or d.get("saved_at", 0)
        if keep_for is not None and now - last_used > keep_for:
            expired += 1
            continue
        if not d.get("original_hashes") or not d.get("prefix"):
            continue
        loaded.append({
            "eid": d["eid"], "owner": d.get("owner"),
            "original_hashes": d["original_hashes"], "prefix": d["prefix"],
            "pending": None, "pending_hashes": None, "thread": None,
            "rebase_requested": d.get("rebase_requested", False),
            "saved_at": d["saved_at"],
            "last_used": last_used,
        })
        max_eid = max(max_eid, d["eid"])
    chains = state.get("chains", [])
    fresh = now - state.get("saved_at", 0) <= _STORE_MAX_AGE
    with store._lock:
        store._compressions[:0] = loaded
        store._eids = itertools.count(max(max_eid + 1, next(store._eids)))
    n_anchors = 0
    if fresh:
        with _threads_lock:
            for mid, i in state.get("anchors", {}).items():
                if 0 <= i < len(chains):
                    _thread_chains[mid] = chains[i]
                    n_anchors += 1
    log.info(f"[STORE] Restored {len(loaded)} compression(s) and {n_anchors} "
             f"thread anchor(s) from {STORE_PATH}"
             + (f"; {expired} unused past retention dropped" if expired else ""))


def _last_used(entry: dict) -> float:
    return entry.get("last_used") or entry.get("saved_at") or time.time()


def _prune_expired(now=None) -> int:
    """Drop ready compressions unused for longer than the retention setting.

    Only finished ones: an entry still compressing or waiting to be promoted
    is left alone. Returns how many were dropped.
    """
    label, keep_for = switch.retention()
    if keep_for is None:
        return 0
    now = time.time() if now is None else now
    with store._lock:
        stale = [e for e in store._compressions
                 if e.get("prefix") is not None and e.get("pending") is None
                 and not (e.get("thread") is not None and e["thread"].is_alive())
                 and now - _last_used(e) > keep_for]
        if stale:
            store._compressions = [e for e in store._compressions
                                   if not any(e is x for x in stale)]
    if stale:
        log.info(f"[STORE] Deleted {len(stale)} compression(s) unused for over "
                 f"{label} ({len(store.compressions)} kept)")
        _persist_soon()
    return len(stale)


def _prune_loop():
    while True:
        time.sleep(_PRUNE_INTERVAL)
        try:
            _prune_expired()
        except Exception as e:
            log.warning(f"[STORE] Retention sweep failed: {e}")


def _usable_compression(convo_key):
    """Newest ready compression owned by this conversation, or None."""
    if not convo_key:
        return None
    for entry in reversed(store.compressions):
        if entry.get("owner") == convo_key and entry.get("prefix") is not None:
            return entry
    return None


def _thread_rebase_reason(chain: dict):
    """Why this continue should become a create, or None to let it through."""
    key = chain["convo_key"]
    ready = _usable_compression(key)
    if ready is not None and ready["eid"] != chain["injected"]:
        # Once per compression: if the create fails to pick it up (history
        # drifted), forcing again would just loop.
        if not ready.get("rebase_requested"):
            ready["rebase_requested"] = True
            _persist_soon()
            return "inject"
    if not chain["over"]:
        return None
    if key and any(e.get("owner") == key and e["thread"] is not None
                   and e["thread"].is_alive() for e in store.compressions):
        return None
    if key and _cooldown_remaining(key) > 0:
        return None
    # A conversation the proxy has never seen a create for (it restarted) has
    # no key yet; the chain object stands in until the create gives it one.
    force_key = key or id(chain)
    now = time.time()
    with _threads_lock:
        if now - _thread_forced.get(force_key, 0.0) < FAILURE_COOLDOWN:
            return None
        _thread_forced[force_key] = now
        if len(_thread_forced) > 512:
            for k in [k for k, v in _thread_forced.items() if now - v > FAILURE_COOLDOWN]:
                _thread_forced.pop(k, None)
    chain["over"] = False
    return "compress"


def _do_background_compression(entry: dict, messages: list, auth_headers: dict,
                               real_token_count: int = None, payload: dict = None):
    """Compress messages. Key = hashes of messages that were summarized (not kept verbatim)."""
    log.info(f"[BG] Starting compression of {len(messages)} messages...")
    try:
        compressed = compressor.compress(messages, auth_headers,
                                         real_token_count=real_token_count, payload=payload)
        if compressed is None:
            # Nothing worth compressing — don't leave an empty entry behind
            store.remove(entry)
            return
        # compressed = [summary, (carried system,) ack] + recent_verbatim
        # Prefix = ONLY the summary part — verbatim messages come from the
        # original request during injection, so including them in the prefix
        # would cause duplication.
        from compressor import prefix_len
        plen = prefix_len(compressed)
        prefix = compressed[:plen]
        # Key = the messages that were summarized away (not the verbatim ones).
        recent_count = len(compressed) - plen
        summarized = messages[:len(messages) - recent_count]
        # Skip old summary prefix if present
        start = prefix_len(summarized)
        key_hashes = _hash_messages(summarized[start:])
        entry["pending"] = prefix
        entry["pending_hashes"] = key_hashes
        entry["_debug_messages"] = summarized[start:]  # for mismatch debugging
        log.info(
            f"[BG] Compression ready: "
            f"{compressor._count_chars(prefix):,} chars "
            f"({len(prefix)} prefix messages, key={len(key_hashes)} hashes, "
            f"summarized {len(summarized) - start} messages)"
        )
    except Exception as e:
        owner = entry.get("owner") or ""
        _note_compression_failure(owner)
        log.error(
            f"[BG] Compression failed (this conversation cools down "
            f"{FAILURE_COOLDOWN}s; others are unaffected): {e}",
            exc_info=True,
        )
        entry["pending"] = None


class ProxyHandler(BaseHTTPRequestHandler):
    """Handle HTTP requests, proxy to upstream API."""
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass

    def _read_body(self) -> bytes:
        length = int(self.headers.get("content-length", 0))
        return self.rfile.read(length) if length > 0 else b""

    def _get_headers_dict(self) -> dict:
        return {key: value for key, value in self.headers.items()}

    def _proxy_raw(self, method: str):
        """Raw proxy — forward request and stream response back."""
        body = self._read_body()
        headers = _forward_headers(self._get_headers_dict(), body if body else None)

        log.info(f"[RAW] {method} {self.path} -> {UPSTREAM_URL} (body={len(body)} bytes)")

        try:
            conn = _upstream_conn()
            upstream_full_path = _join_path(UPSTREAM_PATH, self.path)
            conn.request(method, upstream_full_path, body=body if body else None, headers=headers)
            resp = conn.getresponse()

            log.info(f"[RAW] Response: {resp.status} {resp.reason}")

            self.send_response(resp.status)
            resp_headers = resp.getheaders()
            log.debug(f"[RAW] Response headers: {resp_headers}")
            has_content_length = False
            for key, value in resp_headers:
                lower = key.lower()
                if lower in ("connection", "transfer-encoding"):
                    continue
                if lower == "content-length":
                    has_content_length = True
                self.send_header(key, value)
            if not has_content_length:
                self.send_header("Connection", "close")
            self.end_headers()

            total_bytes = 0
            while True:
                chunk = resp.read1(8192)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
                total_bytes += len(chunk)

            log.info(f"[RAW] Done streaming {total_bytes:,} bytes")
            conn.close()
        except Exception as e:
            log.error(f"[RAW] Upstream error: {e}", exc_info=True)
            error_body = json.dumps({"error": endpoints.annotate_upstream_tls_error(e)}).encode()
            self.send_response(502)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(error_body)))
            self.end_headers()
            self.wfile.write(error_body)

    def do_GET(self):
        log.info(f"[REQ] GET {self.path}")
        parsed = urlparse(self.path)
        normalized_path = parsed.path
        if normalized_path == "/health":
            self._handle_health(parse_qs(parsed.query).get("session", [""])[0])
        elif normalized_path == "/debug/compressions":
            self._handle_debug_compressions()
        elif normalized_path == "/lean/status":
            self._handle_lean_status()
        else:
            self._tracked_get()

    @_track_request("GET")
    def _tracked_get(self):
        self._proxy_raw("GET")

    def do_POST(self):
        if urlparse(self.path).path == "/drain":
            self._handle_drain()
        else:
            self._tracked_post()

    def _handle_drain(self):
        """Hand over to a successor: the start hook's restart, in place of a kill.

        Windows has no SIGTERM to deliver - a kill there is TerminateProcess,
        which cuts every open stream at once. Here this process stops
        listening, so the successor can bind, and finishes what it has open.
        A browser page could POST here too; it always sends Origin and cannot
        set the custom header without a preflight, so either refuses it.
        """
        log.info("[REQ] POST /drain")
        ok = not self.headers.get("Origin") and self.headers.get("X-Rolling-Context") == "drain"
        body = json.dumps({"draining": ok, "pid": os.getpid(),
                           "in_flight": _inflight}).encode()
        self.send_response(200 if ok else 403)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()
        if ok and _drain_handler is not None:
            _drain_handler("Drain requested", handover=True)

    @_track_request("POST")
    def _tracked_post(self):
        log.info(f"[REQ] POST {self.path}")
        if self.path.startswith("/v1/messages"):
            self._handle_messages()
        else:
            self._proxy_raw("POST")

    @_track_request("PUT")
    def do_PUT(self):
        log.info(f"[REQ] PUT {self.path}")
        self._proxy_raw("PUT")

    @_track_request("DELETE")
    def do_DELETE(self):
        log.info(f"[REQ] DELETE {self.path}")
        self._proxy_raw("DELETE")

    @_track_request("PATCH")
    def do_PATCH(self):
        log.info(f"[REQ] PATCH {self.path}")
        self._proxy_raw("PATCH")

    def do_OPTIONS(self):
        log.info(f"[REQ] OPTIONS {self.path}")
        self._proxy_raw("OPTIONS")

    def _handle_debug_compressions(self):
        entries = []
        for i, entry in enumerate(store.compressions):
            info = {
                "index": i,
                "hash_chain_length": len(entry.get("original_hashes") or []),
                "has_prefix": entry["prefix"] is not None,
                "prefix_content": None,
            }
            if entry["prefix"]:
                for msg in entry["prefix"]:
                    content = msg.get("content", "")
                    if isinstance(content, str) and "[ROLLING_CONTEXT_SUMMARY]" in content:
                        info["prefix_content"] = content
            entries.append(info)
        body = json.dumps(entries, indent=2).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_lean_status(self):
        """Machine-readable status for companion plugins (nestor-lean).

        last_injection_ts is global across all conversations flowing through
        this proxy — consumers must treat it as a conservative signal (a
        compression in ANY session invalidates, which only costs savings,
        never correctness).
        """
        data = {
            "status": "ok",
            "last_injection_ts": _last_injection_ts,
            "stored_compressions": len(store.compressions),
            "enabled": not switch.is_disabled(),
            "cache_integrity": dict(_cache_stats),
        }
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_health(self, session_id=""):
        _refresh_limits()
        active = sum(
            1 for e in store.compressions
            if e["thread"] is not None and e["thread"].is_alive()
        )
        from compressor import NATIVE_MODE, SUMMARIZER_FORMAT
        data = {
            "status": "ok",
            # Identity, not decoration. The start hook decides whether to start
            # a proxy by asking this port who is on it; "some server answered
            # 200" is not the same answer as "our proxy answered". Anything
            # else on the port (a stray dev server on 5588) must not read as
            # us, or the hook would leave sessions pointed at it.
            "service": "rolling-context",
            "version": VERSION,
            "pid": os.getpid(),
            "enabled": not switch.is_disabled(),
            "default_enabled": switch.config_default_enabled(),
            "trigger_tokens": TRIGGER_TOKENS,
            "target_tokens": TARGET_TOKENS,
            "retention": switch.retention()[0],
            "summarizer_model": SUMMARIZER_MODEL or "(session model)",
            "summarizer_mode": "native" if NATIVE_MODE else f"flattened/{SUMMARIZER_FORMAT}",
            "upstream_url": UPSTREAM_URL,
            "compression_count": compressor.compression_count,
            "total_tokens_saved": compressor.total_tokens_saved,
            "stored_compressions": len(store.compressions),
            "active_compressions": active,
            # In-flight client requests (streams included). The start hook
            # refuses to replace a proxy while this is non-zero.
            "active_requests": _inflight,
            "can_drain": True,
            "draining": _draining,
            "cache_integrity": dict(_cache_stats),
        }
        if session_id:
            # What this session's next request would do, for /roll and the
            # status line: same precedence as _handle_messages.
            if switch.is_disabled():
                on, why = False, "machine-wide"
            else:
                state = switch.session_disabled(session_id)
                why = "this session"
                if state is None:
                    state = session_toggles.get(session_id)
                if state is None:
                    state, why = not switch.config_default_enabled(), "default"
                on = not state
            data["session"] = {"enabled": on, "scope": why}
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_messages(self):
        _refresh_limits()
        raw_body = self._read_body()
        req_headers = self._get_headers_dict()
        auth_headers = get_passthrough_headers(req_headers)

        log.info(f"[MSG] POST {self.path} (body={len(raw_body)} bytes)")
        log.debug(f"[MSG] Request headers: {list(req_headers.keys())}")

        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError:
            log.error("[MSG] Invalid JSON in request body")
            error_body = b'{"error":"Invalid JSON"}'
            self.send_response(400)
            self.send_header("content-length", str(len(error_body)))
            self.end_headers()
            self.wfile.write(error_body)
            return

        messages = payload.get("messages", [])
        is_streaming = payload.get("stream", False)
        model = payload.get("model", "unknown")
        thread = payload.get("thread") if isinstance(payload.get("thread"), dict) else {}
        thread_type = thread.get("type")  # "create", "continue", or None (no threads)

        # Hash all messages for content-based matching
        msg_hashes = _hash_messages(messages)
        msg_chars = compressor._count_chars(messages)

        # Claude Code sends this on every request, and a subagent inherits its
        # parent's — it gets its own transcript, not its own session. That is
        # what lets a session's toggle reach the agents it spawns.
        session_id = self.headers.get("X-Claude-Code-Session-Id") or ""

        log.info(
            f"[MSG] model={model} stream={is_streaming} "
            f"messages={len(messages)} chars={msg_chars:,} "
            f"session={session_id[:8] or '(none)'}"
            + (f" thread={thread_type}" if thread_type else "")
        )

        # /rolling-context:off — resolved fresh per request so the toggle is
        # live. Machine-wide off wins; otherwise this conversation's own marker
        # decides, and falls back to on. Disabled means "stop acting", not
        # "forget": stored compressions are left intact so turning back on
        # resumes without recompressing.
        # A marker in this request updates the latch for its session; requests
        # without one (later turns, and the subagents this session spawns) read
        # it back.
        marker_state = _session_disabled(messages)
        if marker_state is not None:
            if session_toggles.set(session_id, marker_state):
                log.info(
                    f"[MSG] Session {session_id[:8] or '(none)'} toggled "
                    f"{'OFF' if marker_state else 'ON'} by marker"
                )

        # Precedence: env kill-switch, then an explicit machine-wide off, then
        # this session's own setting (its state file, written by /roll or
        # /rolling-context:on|off, before any legacy marker), then the
        # configured default.
        if switch.is_disabled():
            disabled, scope = True, "machine-wide"
        else:
            session_state = switch.session_disabled(session_id)
            scope = "this session"
            if session_state is None:
                session_state = marker_state
            if session_state is None:
                session_state = session_toggles.get(session_id)
                scope = "inherited from this session"
            if session_state is None:
                disabled, scope = not switch.config_default_enabled(), "config default"
            else:
                disabled = session_state
        if disabled:
            log.info(
                f"[MSG] rolling-context is OFF ({scope}) — passing through "
                f"untouched ({len(store.compressions)} compression(s) kept for later)"
            )

        # Promote any pending compressions
        for entry in store.compressions:
            if entry["pending"] is not None:
                entry["prefix"] = entry["pending"]
                entry["original_hashes"] = entry["pending_hashes"]
                entry["pending"] = None
                entry["pending_hashes"] = None
                entry["last_used"] = time.time()
                _persist_soon()
                log.info(
                    f"[MSG] Compression promoted: {len(entry['prefix'])} prefix messages "
                    f"replacing {len(entry['original_hashes'])} originals"
                )

        # A continue carries only the new turn; the history it extends lives on
        # the server. When the proxy needs that history — to compress it, or to
        # swap a ready summary in — it answers thread_not_found and Claude Code
        # resends this same turn as a create with the full transcript.
        chain = None
        if thread_type == "continue":
            prev_id = thread.get("previous_message_id")
            chain = _chain_for(prev_id)
            if chain is None:
                chain = _new_chain(None)
            reason = None if disabled else _thread_rebase_reason(chain)
            if reason:
                log.info(
                    f"[THREAD] Asking the client to resend {str(prev_id)[:16]}... "
                    f"as a create with full history ({reason})"
                )
                self.send_response(404)
                self.send_header("content-type", "application/json")
                self.send_header("x-should-retry", "false")
                self.send_header("content-length", str(len(_THREAD_NOT_FOUND)))
                self.end_headers()
                self.wfile.write(_THREAD_NOT_FOUND)
                return

        # Scan: do any stored compressions match this request's messages?
        # Skipped entirely while off — find_match is also the only caller that
        # prunes no-longer-helpful entries, so not running it keeps the store
        # exactly as it was. Never on a continue: rewriting a partial turn
        # would corrupt the history the server is holding.
        match, match_end = ((None, -1) if disabled or thread_type == "continue"
                            else store.find_match(msg_hashes, messages))
        injected = False

        if match and match["prefix"] is not None and match_end > 0:
            # Replace everything up to match_end with the prefix
            # (prefix contains summary of everything before it)
            new_messages = messages[match_end:]

            # Strip cache_control from injected prefix messages ONLY.
            # The verbatim tail keeps Claude Code's cache_control breakpoints —
            # stripping those disabled prompt caching entirely, so every request
            # after the first injection paid full input-token cost (issue #1/#4).
            for msg in match["prefix"]:
                content = msg.get("content", "")
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict):
                            block.pop("cache_control", None)

            merged = match["prefix"] + new_messages
            merged = _validate_tool_pairs(merged)

            merged_chars = compressor._count_chars(merged)
            if merged_chars < msg_chars:
                log.info(
                    f"[MSG] Injecting: {msg_chars:,} -> {merged_chars:,} chars "
                    f"({len(messages)} -> {len(merged)} messages, "
                    f"replaced 0-{match_end} with {len(match['prefix'])} prefix "
                    f"+ {len(new_messages)} new)"
                )
                payload["messages"] = merged
                msg_chars = merged_chars
                injected = True
                match["last_used"] = time.time()
                global _last_injection_ts
                _last_injection_ts = time.time()
            else:
                log.info(
                    f"[MSG] Compression no longer helps: "
                    f"merged={merged_chars:,} >= current={msg_chars:,} chars, removing"
                )
                store.remove(match)
                match = None

        # Save current state for post-response compression trigger
        current_messages = payload.get("messages", messages)

        # Cache-integrity sentinel: compare what we are about to send against
        # what this conversation sent last turn. Keyed like concurrency
        # accounting — on the ORIGINAL incoming head hash, which is stable for
        # the life of a transcript even after injection rewrites the payload.
        sent_hashes = msg_hashes if not injected else _hash_messages(current_messages)
        convo_key = _conversation_key(session_id, msg_hashes)
        if chain is None and thread_type == "create":
            # A new server-side thread. It is built from whatever we send, so
            # it carries the injected compression (if any) from here on.
            chain = _new_chain(convo_key, match["eid"] if injected else None)
        cache_expectation = "first"
        with _last_sent_lock:
            prev_sent = _last_sent.get(convo_key)
            if prev_sent is not None:
                shorter = min(len(prev_sent), len(sent_hashes))
                if prev_sent[:shorter] == sent_hashes[:shorter]:
                    # Append-only (or a retry/rewind that is a pure prefix of
                    # last turn) — the cached prefix is intact either way.
                    cache_expectation = "hit"
                    _cache_stats["append_only"] += 1
                elif injected:
                    cache_expectation = "rewrite"
                    _cache_stats["compaction_rewrites"] += 1
                    log.info(
                        "[CACHE] prefix rewritten by compaction injection — "
                        "one expected cache miss, re-caches next turn"
                    )
                else:
                    div = next(
                        (i for i in range(shorter)
                         if prev_sent[i] != sent_hashes[i]),
                        shorter,
                    )
                    if div >= len(prev_sent) - 2:
                        # Only the tail changed — the cache still hits
                        # everything before it. Headless --continue resumes
                        # rewrite the final message this way; ordinary, cheap.
                        cache_expectation = "tail"
                        _cache_stats["tail_divergences"] += 1
                        log.info(
                            f"[CACHE] tail divergence at message "
                            f"{div}/{len(prev_sent)} — cache hits up to there"
                        )
                    else:
                        cache_expectation = "bust"
                        _cache_stats["busts"] += 1
                        log.warning(
                            f"[CACHE] BUST: request diverges from last turn at "
                            f"message {div}/{len(prev_sent)} with no compaction "
                            f"to justify it — prompt cache will miss"
                        )
            _last_sent[convo_key] = sent_hashes
            # Bounded: one entry per live transcript is small, but sessions
            # accumulate over a long-running proxy.
            while len(_last_sent) > 512:
                _last_sent.pop(next(iter(_last_sent)))

        # Forward request — strip Accept-Encoding so we get plain text SSE
        body = json.dumps(payload).encode()
        headers = _forward_headers(req_headers, body, strip_encoding=True)

        log.info(f"[MSG] Forwarding to {UPSTREAM_URL}{self.path} ({len(body):,} bytes)")

        try:
            conn = _upstream_conn()
            upstream_full_path = _join_path(UPSTREAM_PATH, self.path)
            conn.request("POST", upstream_full_path, body=body, headers=headers)
            resp = conn.getresponse()

            log.info(f"[MSG] Upstream response: {resp.status} {resp.reason}")

            self.send_response(resp.status)
            resp_headers = resp.getheaders()
            log.debug(f"[MSG] Response headers: {resp_headers}")
            has_content_length = False
            for key, value in resp_headers:
                lower = key.lower()
                if lower in ("connection", "transfer-encoding"):
                    continue
                if lower == "content-length":
                    has_content_length = True
                self.send_header(key, value)
            if not has_content_length:
                self.send_header("Connection", "close")
            self.end_headers()

            log.info(f"[MSG] Streaming response...")

            # Stream response and capture SSE token data
            buffer = b""
            total_bytes = 0
            total_input = 0
            cache_read = None  # None = usage didn't report a cache split
            response_id = None  # the next continue names it as previous_message_id
            while True:
                chunk = resp.read1(8192)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
                total_bytes += len(chunk)
                if is_streaming:
                    buffer += chunk

            log.info(f"[MSG] Done streaming {total_bytes:,} bytes")

            # Extract input tokens from SSE stream
            if is_streaming and buffer:
                try:
                    text = buffer.decode("utf-8", errors="replace")
                    for line in text.split("\n"):
                        if not line.startswith("data: "):
                            continue
                        data_str = line[6:]
                        if data_str == "[DONE]":
                            continue
                        try:
                            data = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue
                        evt_type = data.get("type", "")

                        # Anthropic native: usage in message_start.message.usage
                        if evt_type == "message_start":
                            response_id = data.get("message", {}).get("id") or response_id
                            usage = data.get("message", {}).get("usage", {})
                            tokens = (
                                usage.get("input_tokens", 0)
                                + usage.get("cache_creation_input_tokens", 0)
                                + usage.get("cache_read_input_tokens", 0)
                            )
                            if tokens > 0:
                                total_input = tokens
                                cache_read = usage.get("cache_read_input_tokens", 0)
                                log.info(
                                    f"[MSG] Input tokens from message_start: {total_input:,} "
                                    f"(cache_read={cache_read:,} "
                                    f"cache_create={usage.get('cache_creation_input_tokens', 0):,} "
                                    f"fresh={usage.get('input_tokens', 0):,})"
                                )

                        # Proxy/converter: usage in message_delta.usage (e.g. CodeGate)
                        elif evt_type == "message_delta":
                            usage = data.get("usage", {})
                            tokens = int(usage.get("input_tokens", 0))
                            if tokens > 0 and tokens > total_input:
                                total_input = tokens
                                log.info(f"[MSG] Input tokens from message_delta: {total_input:,}")

                    if total_input == 0:
                        sse_lines = [l for l in text.split("\n") if l.startswith("data: ")]
                        log.warning(
                            f"[MSG] No input tokens found in SSE! "
                            f"Total events: {len(sse_lines)}"
                        )
                except Exception as e:
                    log.warning(f"[MSG] Failed to parse SSE for tokens: {e}")
            elif not is_streaming and buffer:
                try:
                    data = json.loads(buffer)
                    response_id = data.get("id")
                    usage = data.get("usage", {})
                    total_input = (
                        usage.get("input_tokens", 0)
                        + usage.get("cache_creation_input_tokens", 0)
                        + usage.get("cache_read_input_tokens", 0)
                    )
                    if total_input > 0:
                        cache_read = usage.get("cache_read_input_tokens", 0)
                        log.info(
                            f"[MSG] Input tokens from response: {total_input:,} "
                            f"(cache_read={cache_read:,})"
                        )
                except Exception as e:
                    log.warning(f"[MSG] Failed to parse response for tokens: {e}")

            conn.close()

            # Fallback: estimate tokens from chars if SSE didn't provide usage
            if total_input == 0 and msg_chars > 0:
                total_input = msg_chars // 4  # rough chars-to-tokens estimate
                log.info(
                    f"[MSG] No tokens from SSE, estimating from chars: "
                    f"{msg_chars:,} chars -> ~{total_input:,} tokens"
                )

            # Sentinel verification: append-only requests must actually hit the
            # cache. A zero cache_read on an expected hit means something
            # between us and the tokens changed (headers, serialization, TTL
            # expiry) — exactly the class of regression issue #1 was.
            if (cache_expectation == "hit" and cache_read == 0
                    and total_input > 10_000):
                log.warning(
                    f"[CACHE] expected a cache hit (append-only request) but "
                    f"cache_read=0 on {total_input:,} input tokens — "
                    f"cache miss with no rewrite to explain it"
                )

            # Injection accounting (#8). Whether the proxy is actually doing its
            # job should be answerable from INFO logs, not by correlating
            # session transcripts against DEBUG output.
            if not disabled:
                with _stats_lock:
                    if injected:
                        _stats["injected"] += 1
                    elif total_input > TRIGGER_TOKENS:
                        _stats["missed"] += 1
                    seen = _stats["injected"] + _stats["missed"]
                    if seen and seen % 25 == 0:
                        log.info(
                            f"[STATS] prefix injected on {_stats['injected']}/{seen} "
                            f"requests at or over the trigger "
                            f"({_stats['injected'] / seen * 100:.0f}%)"
                        )

            if chain is not None and resp.status == 200:
                _remember_chain(response_id, chain)
                if (thread_type == "continue" and not disabled
                        and total_input > TRIGGER_TOKENS):
                    if not chain["over"]:
                        log.info(
                            f"[THREAD] {total_input:,} tokens on a continue "
                            f"(trigger: {TRIGGER_TOKENS:,}) — the next turn "
                            f"will fetch the full history to compress it"
                        )
                    chain["over"] = True
                    _persist_soon()

            # Trigger compression based on token count. The minimum message
            # count keeps us from "compressing" sessions whose bulk is the
            # system prompt / first-message context, which we can't remove.
            if disabled:
                if total_input > TRIGGER_TOKENS:
                    log.info(
                        f"[MSG] {total_input:,} tokens is over trigger, but "
                        f"rolling-context is OFF — not compressing"
                    )
            elif total_input > 0 and total_input > TRIGGER_TOKENS and len(current_messages) >= 6:
                convo_key = _conversation_key(session_id, msg_hashes)
                # Per conversation, not process-global. The old check scanned
                # every entry in the store, so ANY conversation compacting
                # blocked ALL others — and did so via a bare `pass`, with no log
                # line, which is why it never showed up in anyone's logs. Most
                # conversations never won the race, never got a compression
                # entry, and so never had anything to inject on later turns (#8).
                mine_compressing = any(
                    e["thread"] is not None and e["thread"].is_alive()
                    and e.get("owner") == convo_key
                    for e in store.compressions
                )
                in_flight = sum(
                    1 for e in store.compressions
                    if e["thread"] is not None and e["thread"].is_alive()
                )
                # A conversation with no usable compression yet is paying full
                # context cost on every single turn, and it is precisely the one
                # a busy machine starves: it always arrives to find the slots
                # taken. So the cap applies to REFRESHES only, and cold starts
                # are admitted up to a hard ceiling. Making the cap absolute
                # reintroduced #8 in a new form — measured: with 5 other
                # conversations running, the sixth never compressed at all.
                has_usable = any(
                    e.get("owner") == convo_key and e.get("prefix") is not None
                    for e in store.compressions
                )
                cooldown_left = _cooldown_remaining(convo_key)
                if mine_compressing:
                    log.info(
                        "[MSG] Over trigger, but this conversation is already "
                        "compressing — skipping"
                    )
                elif in_flight >= MAX_CONCURRENT_COMPACTIONS * 2:
                    log.info(
                        f"[MSG] Over trigger, but {in_flight} compactions are in "
                        f"flight (hard ceiling {MAX_CONCURRENT_COMPACTIONS * 2}) "
                        f"— skipping. Raise ROLLING_CONTEXT_MAX_CONCURRENT if "
                        f"this is frequent."
                    )
                elif in_flight >= MAX_CONCURRENT_COMPACTIONS and has_usable:
                    log.info(
                        f"[MSG] Over trigger, but {in_flight} compactions are in "
                        f"flight (cap {MAX_CONCURRENT_COMPACTIONS}) and this "
                        f"conversation already has a summary — deferring refresh"
                    )
                elif cooldown_left > 0:
                    log.info(
                        f"[MSG] Over trigger but this conversation's last "
                        f"compression failed — cooling down another {cooldown_left:.0f}s"
                    )
                else:
                    log.info(
                        f"[MSG] API reported {total_input:,} tokens (trigger: {TRIGGER_TOKENS:,}). "
                        f"Compressing in background..."
                    )
                    entry = store.add()
                    entry["owner"] = convo_key
                    t = threading.Thread(
                        target=_do_background_compression,
                        args=(entry, current_messages, auth_headers),
                        kwargs={"real_token_count": total_input, "payload": payload},
                        daemon=True,
                    )
                    t.start()
                    entry["thread"] = t

        except Exception as e:
            log.error(f"[MSG] Upstream error: {e}", exc_info=True)
            error_body = json.dumps({"error": endpoints.annotate_upstream_tls_error(e)}).encode()
            self.send_response(502)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(error_body)))
            self.end_headers()
            self.wfile.write(error_body)


class ThreadedHTTPServer(HTTPServer):
    """Handle each request in a new thread."""
    # HTTPServer sets SO_REUSEADDR, which on Windows lets a second proxy bind
    # this port while the first still listens; the two then split requests.
    # Claim it exclusively there (see mitm_frontend.bind_listener).
    allow_reuse_address = not hasattr(socket, "SO_EXCLUSIVEADDRUSE")

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()
    def process_request(self, request, client_address):
        t = threading.Thread(target=self._handle, args=(request, client_address))
        t.daemon = True
        t.start()

    def _handle(self, request, client_address):
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)


def main():
    from compressor import (NATIVE_MODE, NATIVE_FALLBACK, SUMMARIZER_BASE_URL,
                            SUMMARIZER_FORMAT)
    log.info(f"Starting Rolling Context Proxy on port {LISTEN_PORT}")
    log.info(f"  Trigger at: {TRIGGER_TOKENS:,} tokens")
    log.info(f"  Compress down to: {TARGET_TOKENS:,} tokens (recent context)")
    log.info(f"  Summarizer model: {SUMMARIZER_MODEL or '(session model)'}")
    log.info(f"  Summarizer mode: "
             f"{'native (cloned session request, prompt-cached)' if NATIVE_MODE else f'flattened/{SUMMARIZER_FORMAT}'}")
    log.info(f"  Forwarding to: {UPSTREAM_URL}")
    # Printed separately from the upstream on purpose: when these two disagree
    # unintentionally, compaction 401s forever and nothing ever compresses.
    log.info(f"  Compacting via: {SUMMARIZER_BASE_URL}"
             f"{' (third-party — flattened fallback armed)' if NATIVE_FALLBACK else ''}")
    log.info(f"  Matching: content-based (no sessions/fingerprints)")
    label = switch.retention()[0]
    log.info(f"  Unused compressions deleted after: {label} (set in /config)")
    _load_state()
    threading.Thread(target=_persist_loop, name="store-saver", daemon=True).start()
    threading.Thread(target=_prune_loop, name="store-pruner", daemon=True).start()
    swept = switch.prune_session_files()
    if swept:
        log.info(f"  Removed {swept} stale per-session state file(s)")

    # Started by a hook right after it asked the running proxy to hand over
    # (POST /drain): that proxy may still be saving the store and letting go
    # of the port, so keep trying for a while instead of exiting at once.
    bind_until = time.time() + (10 if os.environ.get("ROLLING_CONTEXT_SUCCESSOR") else 0)
    while True:
        try:
            server = ThreadedHTTPServer(("127.0.0.1", LISTEN_PORT), ProxyHandler)
            break
        except OSError as e:
            if time.time() < bind_until:
                time.sleep(0.2)
                continue
            log.error(f"Port {LISTEN_PORT} is already in use ({e}); another proxy "
                      f"is serving it. Exiting.")
            sys.exit(1)

    # Start the MITM front-end. Claude Code connects to it via HTTPS_PROXY,
    # believing it is talking to api.anthropic.com; the front-end terminates TLS
    # (with a CA Claude Code trusts via NODE_EXTRA_CA_CERTS) and hands the
    # decrypted, plaintext connection to our normal ProxyHandler — so every byte
    # of compression/streaming logic below is reused unchanged. Everything that
    # is not api.anthropic.com is blind-tunnelled straight through.
    try:
        import mitm_frontend

        def _on_terminated(tls_sock, addr, host):
            server.RequestHandlerClass(tls_sock, addr, server)

        # If the upstream is a custom gateway (not Anthropic, not a sibling on
        # loopback), intercept that host too so Claude Code's traffic to it is
        # still rewritten.
        _extra = []
        _uh = (_parsed_upstream.hostname or "").lower()
        if _uh and _uh not in ("127.0.0.1", "localhost", "::1") and not (
            _uh == "api.anthropic.com" or _uh.endswith(".anthropic.com")
        ):
            _extra = [_uh]

        mitm_frontend.start_in_thread(MITM_PORT, CA_DIR, _on_terminated, log=log.info,
                                      extra_hosts=_extra, local_ports=(LISTEN_PORT,))
        log.info(f"  MITM front-end: 127.0.0.1:{MITM_PORT} (HTTPS_PROXY entrypoint)"
                 + (f", also intercepting {_extra[0]}" if _extra else ""))
    except OSError as e:
        # The port Claude Code is wired to is held by another process. Running
        # on without it would leave a proxy that receives nothing while the
        # stranger answers every session — exit so the hook sees the failure.
        log.error(f"MITM port {MITM_PORT} is already in use ({e}); another proxy "
                  f"(or a stray test run) is answering HTTPS_PROXY. Exiting.")
        os._exit(1)
    except Exception as e:
        log.warning(f"MITM front-end disabled ({e!r}); HTTPS_PROXY entrypoint unavailable")

    def _drain_and_exit(reason, handover=False):
        # Stop taking new connections, let what is mid-flight finish, then
        # leave. New sessions get ConnectionRefused for the second or two
        # until the replacement binds — the SDK retries connection errors —
        # whereas a stream cut in half is surfaced to the user as an API error.
        global _draining, _handed_over
        _draining = True
        log.info(f"{reason}: draining {_inflight} in-flight request(s) (up to {DRAIN_SECONDS}s)")
        # The successor loads the store when it starts: write it now, and
        # leave the file to the successor from here on.
        try:
            _write_state()
        except Exception as e:
            log.warning(f"[STORE] Could not save {STORE_PATH} before handing over: {e}")
        _handed_over = True
        try:
            import mitm_frontend
            mitm_frontend.close_listener()
        except Exception:
            pass
        server.shutdown()
        # Release the port itself, not just the accept loop, so the
        # successor can bind while this process finishes its requests.
        try:
            server.socket.close()
        except OSError:
            pass
        deadline = time.time() + DRAIN_SECONDS
        while _inflight > 0 and time.time() < deadline:
            time.sleep(0.1)
        # Long-lived streams reconnect when cut: on a handover, cut them only
        # once the successor is there to take the reconnect, not into a
        # closed port. A plain stop (SIGTERM) has no successor to wait for.
        if handover and _long_lived_open:
            deadline = time.time() + SUCCESSOR_WAIT_SECONDS
            while time.time() < deadline and not _successor_listening():
                time.sleep(0.25)
        log.info(f"Exiting with {_inflight} request(s) still open")
        os._exit(0)

    def _request_drain(reason, handover=False):
        # Never call server.shutdown() from the thread running serve_forever.
        if _draining:
            return
        threading.Thread(target=_drain_and_exit, args=(reason, handover),
                         daemon=True).start()

    global _drain_handler
    _drain_handler = _request_drain

    def _on_signal(signum, _frame):
        _request_drain(f"Signal {signum}")

    import signal
    for _sig in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGINT", None)):
        if _sig is not None:
            try:
                signal.signal(_sig, _on_signal)
            except (ValueError, OSError):
                pass

    server.serve_forever()
    # serve_forever returns only when shutdown() was called: the drain thread
    # owns the exit from here, so wait for it rather than falling off the end.
    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
