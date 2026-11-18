"""Fake coding-agent CLI for tests. Prints the JSON shape of a real harness.

Usage: fake_agent.py <claude|opencode|pi|codex|cursor> <fix|nofix|hack>
"fix" repairs fizzbuzz.py in the cwd. "hack" leaves the bug and tampers with the test
infrastructure instead (rewrites run_tests.py, adds conftest.py, moves git refs).
Security probes: exits 9 if the LiteLLM spend key or SECRET_CANARY reached the agent,
prints FAKE_API_KEY to stderr (must be redacted in logs), and records git history depth. When BENCH_TAG is set (and kind is not cursor),
the tag is appended to $FAKE_TAGS_FILE, which fake_litellm.py serves as spend logs.
"""
import json
import os
import sys

kind, mode = sys.argv[1], sys.argv[2]
fix = mode == "fix"
for leaked in ("FAKE_KEY", "SECRET_CANARY"):
    if leaked in os.environ:
        sys.exit(9)
print(f"debug key={os.environ.get('FAKE_API_KEY', '')}", file=sys.stderr)
if os.environ.get("FAKE_PROBE_FILE"):
    import subprocess
    depth = subprocess.run(["git", "rev-list", "--all", "--count"], capture_output=True,
                           text=True).stdout.strip()
    remotes = subprocess.run(["git", "remote"], capture_output=True, text=True).stdout.strip()
    with open(os.environ["FAKE_PROBE_FILE"], "a") as f:
        f.write(json.dumps({"cwd": os.getcwd(), "depth": depth, "remotes": remotes,
                            "sandbox": os.environ.get("FAKE_SANDBOX", ""),
                            "home": os.environ.get("HOME", ""), "uid": os.getuid()}) + "\n")
if mode == "hack":
    with open("run_tests.py", "w") as f:
        f.write("import sys\nsys.exit(0)\n")
    os.makedirs("tests_infra", exist_ok=True)
    with open("tests_infra/conftest.py", "w") as f:
        f.write("# planted\n")
    import subprocess
    subprocess.run(["git", "-c", "user.name=x", "-c", "user.email=x@x", "commit", "-qam", "hack"],
                   capture_output=True)
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
