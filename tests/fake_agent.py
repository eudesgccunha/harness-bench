"""Fake coding-agent CLI for tests. Prints the JSON shape of a real harness.

Usage: fake_agent.py <claude|opencode|pi|codex|cursor> <fix|nofix>
"fix" repairs fizzbuzz.py in the cwd. When BENCH_TAG is set (and kind is not cursor),
the tag is appended to $FAKE_TAGS_FILE, which fake_litellm.py serves as spend logs.
"""
import json
import os
import sys

kind, fix = sys.argv[1], sys.argv[2] == "fix"
if fix:
    with open("fizzbuzz.py", "w") as f:
        f.write("def fizzbuzz(n):\n    if n % 15 == 0:\n        return 'FizzBuzz'\n"
                "    if n % 3 == 0:\n        return 'Fizz'\n    if n % 5 == 0:\n"
                "        return 'Buzz'\n    return str(n)\n")

if kind == "claude":
    print(json.dumps({"type": "result", "num_turns": 4, "total_cost_usd": 0.05,
                      "usage": {"input_tokens": 10, "output_tokens": 500},
                      "modelUsage": {
                          "main": {"inputTokens": 10, "outputTokens": 500,
                                   "cacheReadInputTokens": 40000, "cacheCreationInputTokens": 12000},
                          "helper": {"inputTokens": 300, "outputTokens": 20}}}))
elif kind == "opencode":
    print("progress text that is not JSON")
    for _ in range(3):
        print(json.dumps({"type": "step_finish", "part": {"cost": 0.004, "tokens": {
            "input": 2000, "output": 150, "reasoning": 50, "cache": {"read": 8000, "write": 0}}}}))
elif kind == "codex":
    print(json.dumps({"type": "thread.started"}))
    print(json.dumps({"type": "turn.completed", "usage": {
        "input_tokens": 20000, "cached_input_tokens": 15000,
        "output_tokens": 900, "reasoning_output_tokens": 400}}))
elif kind == "cursor":
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "ok"}))
elif kind == "pi":
    print(json.dumps({"type": "session", "version": 3}))
    msg = {"role": "assistant", "usage": {"input": 800, "output": 120, "cacheRead": 1500,
                                          "cacheWrite": 0, "cost": {"total": 0.001}}}
    for _ in range(3):  # same usage on 3 event types: only message_end must count
        for typ in ("message_update", "message_end", "turn_end"):
            print(json.dumps({"type": typ, "message": msg}))
else:
    sys.exit(f"unknown kind {kind}")

if os.environ.get("BENCH_TAG") and kind != "cursor":
    with open(os.environ["FAKE_TAGS_FILE"], "a") as f:
        f.write(os.environ["BENCH_TAG"] + "\n")
