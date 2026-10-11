#!/usr/bin/env python3
"""The /config options (userConfig) and compression retention.

  A. switch.py reads pluginConfigs live from ~/.claude/settings.json: /config
     beats the environment, the enabled install wins when two are present,
     a change applies without a restart, target stays below trigger, and an
     unknown retention label falls back to the default.
  B. retention: a compression unused for longer than the setting is deleted
     from memory (and so from disk); one in use, one still pending, and
     everything under "Never" are kept; expired ones are dropped on load.

  python tests/test_settings.py
"""
import json
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = tempfile.mkdtemp(prefix="rolling-context-settings-")
# Before importing anything that reads them: a throwaway home, flag dir, port
# and store, so nothing here touches the developer's real proxy or settings.
os.environ.update(HOME=ROOT, USERPROFILE=ROOT,
                  ROLLING_CONTEXT_HOME=os.path.join(ROOT, "flags"),
                  ROLLING_CONTEXT_PORT="1",
                  ROLLING_CONTEXT_STORE_PATH=os.path.join(ROOT, "store.json"))
for k in ("ROLLING_CONTEXT_TRIGGER", "ROLLING_CONTEXT_TARGET"):
    os.environ.pop(k, None)
os.makedirs(os.path.join(ROOT, ".claude"), exist_ok=True)
sys.path.insert(0, os.path.join(HERE, "..", "proxy"))

import switch  # noqa: E402

SETTINGS = os.path.join(ROOT, ".claude", "settings.json")
failures = []


def check(cond, label, detail=""):
    print(f"    {'PASS' if cond else 'FAIL'}  {label}")
    if detail:
        print(f"          {detail}")
    if not cond:
        failures.append(label)


def write_settings(configs, enabled=None):
    with open(SETTINGS, "w", encoding="utf-8") as f:
        json.dump({"pluginConfigs": configs, "enabledPlugins": enabled or {}}, f)
    # Make sure the mtime cache sees a change even on a coarse clock.
    t = time.time() + len(json.dumps(configs))
    os.utime(SETTINGS, (t, t))


def options():
    print("  /config options")
    check(switch.trigger_tokens() == (switch.DEFAULT_TRIGGER, "default"),
          "no settings, no env: built-in defaults")

    os.environ["ROLLING_CONTEXT_TRIGGER"] = "150000"
    check(switch.trigger_tokens() == (150000, "ROLLING_CONTEXT_TRIGGER"),
          "the environment is used when /config has nothing")

    write_settings({"rolling-context@nestor-plugins": {"options": {
        "trigger_tokens": 180000, "target_tokens": 60000, "retention": "3 days"}}})
    check(switch.trigger_tokens() == (180000, "/config"), "/config beats the environment")
    check(switch.target_tokens() == (60000, "/config"), "target read from /config")
    check(switch.retention() == ("3 days", 3 * 86400), "retention read from /config")
    os.environ.pop("ROLLING_CONTEXT_TRIGGER")

    write_settings({
        "rolling-context@nestor-plugins": {"options": {"trigger_tokens": 111111}},
        "rolling-context@my-fork": {"options": {"trigger_tokens": 222222}},
    }, enabled={"rolling-context@nestor-plugins": False, "rolling-context@my-fork": True})
    check(switch.trigger_tokens()[0] == 222222, "the enabled install's options win")

    write_settings({"rolling-context@x": {"options": {
        "trigger_tokens": 50000, "target_tokens": 90000, "retention": "forever-ish"}}})
    check(switch.target_tokens()[0] == 25000, "a target at or above the trigger is capped at half")
    check(switch.retention() == (switch.DEFAULT_RETENTION,
                                 switch.RETENTION_CHOICES[switch.DEFAULT_RETENTION]),
          "an unknown retention label falls back to the default")

    write_settings({"rolling-context@x": {"options": {"retention": "Never"}}})
    check(switch.retention() == ("Never", None), "Never means no limit")

    with open(os.path.join(HERE, "..", ".claude-plugin", "plugin.json"), encoding="utf-8") as f:
        listed = json.load(f)["userConfig"]["retention"]["options"]
    check(listed == list(switch.RETENTION_CHOICES),
          "plugin.json's dropdown lists exactly the labels switch.py understands")


def retention():
    print("  retention")
    import server  # noqa: E402 — after the environment above is in place

    now = time.time()

    def entry(age, **kw):
        e = server.store.add()
        e.update(prefix=[{"role": "user", "content": "summary"}],
                 original_hashes=["h"], owner="o", last_used=now - age, saved_at=now - age)
        e.update(kw)
        return e

    write_settings({"rolling-context@x": {"options": {"retention": "1 hour"}}})
    old = entry(2 * 3600)
    fresh = entry(60)
    pending = entry(2 * 3600, pending=[{"role": "user", "content": "next"}])
    dropped = server._prune_expired(now)
    kept = server.store.compressions
    check(dropped == 1 and old not in kept, "unused past the setting: deleted",
          f"dropped {dropped}")
    check(fresh in kept, "recently used: kept")
    check(pending in kept, "still waiting to be promoted: kept")

    write_settings({"rolling-context@x": {"options": {"retention": "Never"}}})
    ancient = entry(365 * 86400)
    check(server._prune_expired(now) == 0 and ancient in server.store.compressions,
          "Never keeps everything")

    # On disk: an entry whose last use is past retention is not restored.
    write_settings({"rolling-context@x": {"options": {"retention": "1 day"}}})
    server.store._compressions = []
    entry(3 * 86400)
    entry(3600)
    server._write_state()
    server.store._compressions = []
    server._load_state()
    restored = server.store.compressions
    check(len(restored) == 1 and now - restored[0]["last_used"] < 2 * 3600,
          "on load, only compressions used within the setting come back",
          f"{len(restored)} restored")


def main():
    try:
        print("settings")
        options()
        print()
        retention()
    finally:
        shutil.rmtree(ROOT, ignore_errors=True)
    print()
    if failures:
        print(f"FAILED ({len(failures)})")
        sys.exit(1)
    print("ALL SETTINGS CHECKS PASSED")


if __name__ == "__main__":
    main()
