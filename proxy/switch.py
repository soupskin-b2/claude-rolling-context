#!/usr/bin/env python3
"""Runtime on/off switch for rolling-context.

Two scopes:

  session (default)  Turn compression off for THIS conversation only, leaving
                     every other session alone. Issue #6: heavy planning and
                     optimization work wants the whole repo in view, but the
                     other windows open on the same machine still want the
                     savings.

  --global           Turn it off for every session on this machine.

`ROLLING_CONTEXT_DISABLE=1` already worked for the global case, but it lives in
settings.json env and needs both a Claude Code restart and a proxy restart to
take effect — useless at the moment you actually want it, which is mid-session.
The global scope is a flag file the proxy checks on every request instead, so
it applies to the very next request.

The session scope is a small state file per Claude Code session id, under
<flag dir>/sessions/. Claude Code sends that id on every request as
X-Claude-Code-Session-Id (subagents inherit their parent's), and exports it to
the commands it runs as CLAUDE_CODE_SESSION_ID, so the slash command and the
proxy agree on it without tracking anything else. A session that is closed and
resumed keeps its id, and so keeps whatever it was set to.

It used to be a marker printed into the transcript and recognised by content.
That never reached the transcript the way it was meant to — a prompt-style
slash command's output arrives as a Bash tool result, not inside
<local-command-stdout> — and it could fire by accident when a tool result
merely quoted the marker between two such tags. The marker is still printed
when no session id is available (a Claude Code too old to export one), and
the proxy still honours it in real command output, but a state file wins.
`status` deliberately never prints the marker literals.

Turning off is NOT destructive in either scope. Stored compressions stay in the
proxy's memory untouched; they simply stop being injected, and no new
compression is triggered. Turning back on resumes with everything already
computed, so a session that ran hot before the toggle never pays to compress
that history twice.

The global flag lives at a fixed user-level path rather than in the plugin data
dir: slash commands run through Bash, which does not inherit CLAUDE_PLUGIN_DATA,
so a data-dir flag would be written somewhere the proxy never looks. A fixed
path also survives version bumps, which move the plugin cache directory.

Also here: the options set in /config (the plugin's userConfig), which the
proxy reads live from ~/.claude/settings.json so a change applies from the
next request without a restart.

  python switch.py on|off [--global] [--session ID] | status [--session ID]
"""
import json
import os
import re
import sys
import time

# Kept in one place; server.py imports these rather than restating them.
MARKER_OFF = "<<rolling-context:session-off>>"
MARKER_ON = "<<rolling-context:session-on>>"

PLUGIN_NAME = "rolling-context"

# The /config dropdown for how long an unused compression is kept. Keys must
# match the "options" list in .claude-plugin/plugin.json exactly.
RETENTION_CHOICES = {
    "1 hour": 3600,
    "6 hours": 6 * 3600,
    "1 day": 86400,
    "3 days": 3 * 86400,
    "7 days": 7 * 86400,
    "14 days": 14 * 86400,
    "30 days": 30 * 86400,
    "Never": None,
}
DEFAULT_RETENTION = "14 days"
DEFAULT_TRIGGER = 100000
DEFAULT_TARGET = 40000

# Session state files nobody has touched in this long are swept on proxy start.
SESSION_FILE_MAX_AGE = 90 * 86400


def flag_dir():
    return os.environ.get("ROLLING_CONTEXT_HOME") or os.path.join(
        os.path.expanduser("~"), ".claude-rolling-context"
    )


def flag_path():
    return os.path.join(flag_dir(), "disabled")


def config_path():
    return os.path.join(flag_dir(), "config.json")


def proxy_line(session_id=None):
    """One line on whether the proxy is actually up, and this session's
    compressions as the proxy counts them (None where it has no count).

    "rolling-context: ON" used to be printed by a status command that had
    never checked whether anything was listening — the same blind spot that
    let a crashed proxy read as running (issue #9). On is only meaningful if
    there is a proxy to be on.
    """
    import urllib.parse
    import urllib.request

    port = os.environ.get("ROLLING_CONTEXT_PORT") or "5588"
    query = f"?session={urllib.parse.quote(session_id)}" if session_id else ""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/health{query}", timeout=2) as r:
            body = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        # ASCII only: this prints to a terminal that may be cp1252, where an
        # em dash lands as a replacement character.
        return (f"proxy: NOT RUNNING on port {port} - sessions will fail with "
                f"connection refused.\n"
                f"  It restarts on the next Claude Code session; see "
                f"~/.claude/rolling-context-hook.log"), None
    if body.get("service") != "rolling-context":
        return f"proxy: port {port} is answering, but it is not this proxy", None
    mine = body.get("session") or {}
    counted = mine if "compressions" in mine else None
    return (f"proxy: running on port {port} (v{body.get('version', '?')}, "
            f"{body.get('compression_count', 0)} compressions, "
            f"{body.get('total_tokens_saved', 0):,} tokens saved, "
            f"{body.get('stored_compressions', 0)} stored)"), counted


def config_default_enabled():
    """The default for sessions that have not toggled themselves.

    A fresh install compresses out of the box — that is the whole point of
    installing it — so the built-in default is on, and {"enabled": false} in
    config.json flips it for people who would rather opt in per session.

    Deliberately forgiving: a missing, unreadable, or malformed config falls
    back to on rather than silently disabling the thing the user installed.
    """
    try:
        with open(config_path(), encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return True
    value = cfg.get("enabled", True) if isinstance(cfg, dict) else True
    return value if isinstance(value, bool) else True


# --- options set in /config (userConfig) -------------------------------------
# Claude Code stores them in ~/.claude/settings.json as
# pluginConfigs["<plugin>@<marketplace>"].options. One proxy serves every
# session, so it reads them from there on each request (cached on the file's
# mtime) rather than from the environment it happened to be started with.

_options_cache = {"key": None, "value": {}}


def claude_settings_path():
    return os.path.join(os.path.expanduser("~"), ".claude", "settings.json")


def config_id_path():
    """Where the mod records the pluginConfigs id its /config rows belong to."""
    return os.path.join(os.path.expanduser("~"), ".claude", "rolling-context-config-id")


def _config_id():
    try:
        with open(config_id_path(), encoding="utf-8-sig") as f:
            return f.read().strip()
    except OSError:
        return ""


def plugin_options():
    """This plugin's /config values, or {} when none are stored."""
    path = claude_settings_path()
    try:
        st = os.stat(path)
    except OSError:
        return {}
    live = _config_id()
    key = (st.st_mtime_ns, st.st_size, live)
    if _options_cache["key"] == key:
        return _options_cache["value"]
    value = {}
    try:
        with open(path, encoding="utf-8-sig") as f:
            settings = json.load(f)
        configs = settings.get("pluginConfigs") or {}
        enabled = settings.get("enabledPlugins") or {}
        ids = [i for i in configs
               if i == PLUGIN_NAME or i.startswith(PLUGIN_NAME + "@")]
        # The engine may key the rows the user edits otherwise than the
        # enabled id: the desktop app loads the plugin as `<name>@inline`
        # beside an enabled `<name>@<marketplace>`, and its /config writes
        # the former. The mod records the id its rows carry; that one first.
        # Without it, installed from two marketplaces (say, upstream and a
        # fork), the enabled one is the one whose /config rows the user sees.
        ids.sort(key=lambda i: (i != live, enabled.get(i) is not True, i))
        for i in ids:
            opts = (configs.get(i) or {}).get("options")
            if isinstance(opts, dict):
                value = opts
                break
    except (OSError, ValueError, AttributeError):
        value = {}
    _options_cache.update(key=key, value=value)
    return value


def _user_config():
    """plugin.json's userConfig: the options' types, bounds and choices."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                        ".claude-plugin", "plugin.json")
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f).get("userConfig") or {}
    except (OSError, ValueError, AttributeError):
        return {}


def set_option(name, raw):
    """Store one option in settings.json without /config: (ok, message).

    The mod saves through the session's /config rows, but a desktop session
    sometimes starts without them (the engine left the plugin out of the list
    its rows come from, though the plugin and its mod are loaded). Then this
    writes pluginConfigs itself, into every entry of this plugin (the one the
    rows belong to and the enabled one), so whichever the engine and the proxy
    read holds it. Atomic, by a replace of the whole file.
    """
    spec = _user_config().get(name)
    if not isinstance(spec, dict):
        return False, f"no option {name!r} in plugin.json"
    if spec.get("type") == "number":
        try:
            value = int(raw)
        except ValueError:
            return False, f"{name} takes a whole number of tokens"
        lo, hi = spec.get("min"), spec.get("max")
        if (lo is not None and value < lo) or (hi is not None and value > hi):
            span = (f"{lo:,} to {hi:,}" if lo is not None and hi is not None
                    else f"at least {lo:,}" if lo is not None else f"at most {hi:,}")
            return False, f"{name} takes {span} tokens"
    else:
        choices = spec.get("options") or []
        value = next((c for c in choices if c.lower() == raw.strip().lower()), None)
        if value is None:
            return False, f"{name} takes one of: {', '.join(choices)}"
    path = claude_settings_path()
    try:
        with open(path, encoding="utf-8-sig") as f:
            text = f.read()
        settings = json.loads(text) if text.strip() else {}
    except FileNotFoundError:
        text, settings = "", {}
    except (OSError, ValueError) as e:
        return False, f"settings.json could not be read ({e}); not changed"
    configs = settings.setdefault("pluginConfigs", {})
    enabled = settings.get("enabledPlugins") or {}
    ids = {i for i in configs if i == PLUGIN_NAME or i.startswith(PLUGIN_NAME + "@")}
    ids |= {i for i in (_config_id(),) if i}
    ids |= {i for i, on in enabled.items() if on is True and i.startswith(PLUGIN_NAME + "@")}
    if not ids:
        ids = {PLUGIN_NAME}
    for i in ids:
        entry = configs.get(i) if isinstance(configs.get(i), dict) else {}
        opts = entry.get("options") if isinstance(entry.get("options"), dict) else {}
        opts[name] = value
        entry["options"] = opts
        configs[i] = entry
    out = json.dumps(settings, indent=2, ensure_ascii=False) + ("\n" if text.endswith("\n") or not text else "")
    tmp = f"{path}.rolling-context-{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(out)
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                # Windows refuses the replace while another process has it open.
                if attempt == 4:
                    raise
                time.sleep(0.1)
    except OSError as e:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False, f"settings.json could not be written ({e}); not changed"
    return True, value


def _int_option(name, env, default, minimum):
    """(value, source): /config first, then the environment, then the default."""
    v = plugin_options().get(name)
    if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= minimum:
        return int(v), "/config"
    raw = os.environ.get(env)
    if raw:
        try:
            if int(raw) >= minimum:
                return int(raw), env
        except ValueError:
            pass
    return default, "default"


def trigger_tokens():
    return _int_option("trigger_tokens", "ROLLING_CONTEXT_TRIGGER", DEFAULT_TRIGGER, 1000)


def target_tokens():
    """Never at or above the trigger: that would re-trigger on every turn."""
    target, source = _int_option("target_tokens", "ROLLING_CONTEXT_TARGET", DEFAULT_TARGET, 1000)
    trigger = trigger_tokens()[0]
    if target >= trigger:
        return int(trigger * 0.5), source + " (capped at half the trigger)"
    return target, source


def retention():
    """(label, seconds or None for never): how long an unused compression is kept."""
    v = plugin_options().get("retention")
    if v in RETENTION_CHOICES:
        return v, RETENTION_CHOICES[v]
    return DEFAULT_RETENTION, RETENTION_CHOICES[DEFAULT_RETENTION]


# --- per-session state --------------------------------------------------------

def sessions_dir():
    return os.path.join(flag_dir(), "sessions")


def _session_file(session_id):
    safe = re.sub(r"[^A-Za-z0-9_-]", "", session_id or "")[:128]
    return os.path.join(sessions_dir(), safe + ".json") if safe else None


def set_session(session_id, disabled):
    path = _session_file(session_id)
    if not path:
        return False
    os.makedirs(sessions_dir(), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"disabled": bool(disabled), "at": time.time()}, f)
    os.replace(tmp, path)
    return True


_session_cache = {}


def session_disabled(session_id):
    """True (off), False (on), or None when this session never chose."""
    path = _session_file(session_id)
    if not path:
        return None
    try:
        mtime = os.stat(path).st_mtime_ns
    except OSError:
        _session_cache.pop(path, None)
        return None
    cached = _session_cache.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        with open(path, encoding="utf-8") as f:
            value = bool(json.load(f).get("disabled"))
    except (OSError, ValueError, AttributeError):
        return None
    if len(_session_cache) > 1024:
        _session_cache.clear()
    _session_cache[path] = (mtime, value)
    return value


def prune_session_files(max_age=SESSION_FILE_MAX_AGE):
    cutoff = time.time() - max_age
    try:
        names = os.listdir(sessions_dir())
    except OSError:
        return 0
    removed = 0
    for name in names:
        path = os.path.join(sessions_dir(), name)
        try:
            if os.stat(path).st_mtime < cutoff:
                os.remove(path)
                removed += 1
        except OSError:
            pass
    return removed


def env_disabled():
    return os.environ.get("ROLLING_CONTEXT_DISABLE") == "1"


def is_disabled():
    """True when the proxy should pass every session through untouched.

    Read fresh on every call — the whole point is that the flag takes effect
    without restarting the proxy, so this must never be cached.
    """
    if env_disabled():
        return True
    try:
        return os.path.exists(flag_path())
    except OSError:
        return False


def _set(disabled):
    path = flag_path()
    if disabled:
        os.makedirs(flag_dir(), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("rolling-context disabled via /roll off --global\n")
    else:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


def _cmd(action, is_global=False):
    """How to spell a command in a hint: /roll, the plugin's only command."""
    flag = " --global" if is_global else ""
    return f"/roll {action}{flag}"


def _session(session_id, disabled, lines):
    """Record this session's choice; the marker only when there is no id."""
    if not set_session(session_id, disabled):
        print(MARKER_OFF if disabled else MARKER_ON)
    for line in lines:
        print(line)


def _parse(args):
    session_id = None
    rest = []
    it = iter(args)
    for a in it:
        if a == "--session":
            session_id = next(it, None)
        elif a.startswith("--session="):
            session_id = a.split("=", 1)[1]
        else:
            rest.append(a.lower())
    if not session_id:
        session_id = os.environ.get("CLAUDE_CODE_SESSION_ID") or None
    is_global = bool({"--global", "-g"} & set(rest))
    action = next((a for a in rest if not a.startswith("-")), "status")
    return action, is_global, session_id


def _fmt_seconds(seconds):
    if seconds is None:
        return "never"
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds >= size:
            n = round(seconds / size)
            return f"{n} {unit}{'' if n == 1 else 's'}"
    return f"{int(seconds)} seconds"


def main():
    action, is_global, session_id = _parse(sys.argv[1:])

    if action == "off":
        if is_global:
            _set(True)
            print("rolling-context is now OFF on this machine — every session sends")
            print("its full history upstream, uncompressed.")
            print("Applies from the next request; no restart needed.")
            print("Already-computed compressions are kept, not discarded.")
            print(f"Turn it back on with {_cmd('on', True)}")
            return
        _session(session_id, True, [
            "rolling-context is now OFF for this conversation — it will send its",
            "full history upstream, uncompressed. Other sessions are unaffected.",
            "Applies from the next request; no restart needed.",
            "Already-computed compressions are kept, not discarded.",
            f"Turn it back on with {_cmd('on')}",
        ])
        return

    if action == "on":
        if is_global:
            _set(False)
            if env_disabled():
                print("Flag cleared, but ROLLING_CONTEXT_DISABLE=1 is set in the")
                print("environment and still wins. Remove it from settings.json")
                print("and restart Claude Code.")
                return
            print("rolling-context is now ON for this machine — compression active")
            print("from the next request.")
            print("Compressions computed before the toggle are reused as-is.")
            return
        notes = [
            "rolling-context is now ON for this conversation — compression active",
            "from the next request.",
            "Compressions computed before the toggle are reused as-is.",
        ]
        if is_disabled():
            notes += [
                "",
                "Note: it is still off machine-wide, which wins over this session.",
                f"Clear that with {_cmd('on', True)}",
            ]
        _session(session_id, False, notes)
        return

    if action == "status":
        # Never print the marker literals here: a transcript that quotes them
        # is exactly what the legacy marker scan looks for.
        line, counted = proxy_line(session_id)
        print(line)
        machine_off = True
        if env_disabled():
            print("rolling-context: OFF machine-wide (ROLLING_CONTEXT_DISABLE=1 in the environment)")
        elif os.path.exists(flag_path()):
            print(f"rolling-context: OFF machine-wide (switched off with {_cmd('off', True)})")
        else:
            machine_off = False
            print("rolling-context: ON machine-wide")
        default_on = config_default_enabled()
        print(f"  default for new sessions: {'ON' if default_on else 'OFF'}"
              + ("" if os.path.exists(config_path()) else "  (built-in)"))
        mine = session_disabled(session_id) if session_id else None
        if not session_id:
            print("this session: unknown (no session id; run it from a Claude Code session)")
        elif machine_off:
            print("this session: OFF (machine-wide wins)")
        elif mine is None:
            print(f"this session: {'ON' if default_on else 'OFF'} (default; never toggled here)")
        else:
            print(f"this session: {'OFF' if mine else 'ON'} (set with {_cmd('off' if mine else 'on')})")
        if session_id and counted is not None:
            print(f"  this session's compressions: {counted['compressions']}, "
                  f"{counted.get('tokens_saved', 0):,} tokens saved")
        trigger, trigger_src = trigger_tokens()
        target, target_src = target_tokens()
        label, seconds = retention()
        print(f"compress above: {trigger:,} tokens ({trigger_src})")
        print(f"keep verbatim:  {target:,} tokens ({target_src})")
        print(f"delete unused compressions after: {label}"
              + ("" if "retention" in plugin_options() else " (default)"))
        print("change these in /config")
        return

    if action == "set":
        # switch.py set <option> <value...>, the value as typed ("3 days").
        words, it = [], iter(sys.argv[1:])
        for a in it:
            if a == "--session":
                next(it, None)
            elif not a.startswith("-"):
                words.append(a)
        if len(words) < 3:
            print("Usage: switch.py set <trigger_tokens|target_tokens|retention> <value>")
            sys.exit(2)
        ok, said = set_option(words[1], " ".join(words[2:]))
        print(f"{words[1]} = {said}" if ok else said)
        sys.exit(0 if ok else 1)

    print(f"Unknown action {action!r} — expected on, off, status or set")
    sys.exit(2)


if __name__ == "__main__":
    main()
