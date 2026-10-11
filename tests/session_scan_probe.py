#!/usr/bin/env python3
"""Unit cases for server._session_disabled, run out-of-process by test_toggle.py.

Importing server.py resolves the upstream endpoint at module level, so this
runs as its own process under a fake HOME rather than inside the test runner.
Prints one JSON object: {"cases": [[name, ok], ...]}.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "proxy"))

import server  # noqa: E402
import switch  # noqa: E402

OFF = switch.MARKER_OFF
ON = switch.MARKER_ON


def stdout_block(marker):
    return f"<local-command-stdout>{marker}\nrolling-context is now set.</local-command-stdout>"


def user(text):
    return {"role": "user", "content": text}


def blocks(*texts):
    return {"role": "user",
            "content": [{"type": "text", "text": t} for t in texts]}


CASES = [
    ("no marker anywhere -> None (follow machine setting)",
     [user("hello"), user("world")], None),

    ("off marker in command output -> True",
     [user("hello"), user(stdout_block(OFF))], True),

    ("on marker in command output -> False",
     [user(stdout_block(ON))], False),

    ("newest marker wins: off then on -> False",
     [user(stdout_block(OFF)), user("work"), user(stdout_block(ON))], False),

    ("newest marker wins: on then off -> True",
     [user(stdout_block(ON)), user("work"), user(stdout_block(OFF))], True),

    ("two markers in ONE message: last one wins",
     [user(stdout_block(ON) + "\n" + stdout_block(OFF))], True),

    ("found inside list-shaped content blocks",
     [blocks("chatter", stdout_block(OFF))], True),

    # A tool result is never command output, even with the tags inside: that
    # is how reading this plugin's source used to switch a session off. The
    # slash command's real path is now the session state file.
    ("NOT found inside a nested tool_result content list",
     [{"role": "user", "content": [
         {"type": "tool_result", "content": [
             {"type": "text", "text": stdout_block(OFF)}]}]}], None),

    # The false-positive guard. Reading or grepping the plugin's own source in
    # a conversation puts the literal marker in a tool result, which is a
    # user-role message. It must NOT toggle anything.
    ("bare marker outside a command block is IGNORED (Read of switch.py)",
     [user(f'MARKER_OFF = "{OFF}"  # source code in a tool result')], None),

    ("marker discussed in prose is IGNORED",
     [user(f"why does {OFF} not work?")], None),

    ("bare marker after a real one does not override it",
     [user(stdout_block(ON)), user(f"here is the source: {OFF}")], False),

    ("unterminated command block is IGNORED",
     [user(f"<local-command-stdout>{OFF}")], None),

    ("non-text content is skipped safely",
     [{"role": "user", "content": [{"type": "image", "source": {"data": "x"}}]},
      user(stdout_block(OFF))], True),

    ("empty history -> None", [], None),

    # What actually happened in the wild: a tool result that quoted this
    # proxy's source, where an opening tag, the marker and a closing tag
    # happened to fall in that order. It is not command output.
    ("tags + marker inside a tool_result are IGNORED",
     [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1",
       "content": f'r"<local-command-stdout>(.*?)" ... MARKER_OFF = "{OFF}" ... '
                  f'"</local-command-stdout>"'}]}], None),

    ("a user message that only quotes a command block mid-text is IGNORED",
     [user(f"look at this: {stdout_block(OFF)}")], None),

    ("an assistant message is never command output",
     [{"role": "assistant", "content": stdout_block(OFF)}], None),
]


def main():
    results = []
    for name, messages, want in CASES:
        try:
            got = server._session_disabled(messages)
            ok = got is want
        except Exception as e:  # a crash here would take the proxy down
            ok = False
            name += f" [raised {type(e).__name__}: {e}]"
        results.append([name, ok])

    # The marker must survive hashing untouched in one direction and be
    # invisible in the other: <local-command-stdout> is stripped for hashing,
    # so toggling must not invalidate compressions computed before it.
    plain = user("some real conversation turn")
    tagged = user("some real conversation turn" + stdout_block(OFF))
    results.append(["toggling does not change a message's hash",
                    server._hash_message(plain) == server._hash_message(tagged)])

    print(json.dumps({"cases": results}))


if __name__ == "__main__":
    main()
