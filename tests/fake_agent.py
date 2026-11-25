"""Fake coding-agent CLI for tests. Prints the JSON shape of a real harness.

Usage: fake_agent.py <claude|opencode|pi|codex|cursor> <fix|nofix|hack|probe-hidden|plant-link|artifacts|artifacts-secret|artifacts-link|hide-git|hide-git-clean|hide-ignore|hide-attr|hide-config|fork-bg|slow-once|slow-writer>
"fix" repairs fizzbuzz.py in the cwd. "hack" leaves the bug and tampers with the test
infrastructure instead (rewrites run_tests.py, adds conftest.py, moves git refs).
The "hide-*" modes fix the bug and then try to hide a forbidden change from the diff:
"hide-git" edits run_tests.py behind skip-worktree, info/exclude and a replace ref in its own repo
("hide-git-clean" then deletes that repo),
"hide-ignore" plants conftest.py behind a .gitignore, "hide-attr" edits run_tests.py next to a
.gitattributes with `working-tree-encoding=UTF-16` (makes a naive `git add` fail), and "hide-config" arms .git/config
(fsmonitor, filter, textconv) with a command that touches $FAKE_MARKER.
"probe-hidden" leaves the bug and searches for the hidden tests from where an unsandboxed agent runs (its whole
workdir, its parents up to `/` and the roots in $FAKE_SCAN_ROOTS): a file named in $FAKE_HIDDEN_NAMES (comma
separated) or a .py file holding the marker that every example hidden test carries. It exits 7 when it finds one.
"plant-link" fixes the bug and plants `test_hidden.py` as a symlink to $FAKE_CANARY_FILE, as an agent would to
make the grader overwrite a host file.
"artifacts" writes `report.html` and `summary.json` from `data.csv` (the examples-artifacts suite); "artifacts-secret"
also puts $FAKE_API_KEY in the report, and "artifacts-link" plants `report.html` as a symlink to $FAKE_CANARY_FILE.
"fork-bg" fixes the bug and leaves a background process behind that appends to $FAKE_BG_FILE every 50 ms (it keeps
stdout open and stays in the agent's process group, like `sleep 600 &` in a shell): the engine must kill it.
"slow-writer" appends to late.txt every 50 ms for a minute, in the foreground (an agent that outlasts its timeout).
"slow-once" fixes the bug like "fix", but first sleeps (up to 90 s) while the file $FAKE_SLOW_FLAG exists; it gives up
as soon as its parent is gone, so a killed engine leaves no agent behind.
Security probes: exits 9 if the LiteLLM spend key or SECRET_CANARY reached the agent,
prints FAKE_API_KEY to stderr (must be redacted in logs), and records git history depth. When BENCH_TAG is set (and kind is not cursor),
the tag is appended to $FAKE_TAGS_FILE, which fake_litellm.py serves as spend logs.
"""

import json
import os
import sys
from pathlib import Path

MARKER = "HIDDEN-TEST" + "-MARKER"  # built at runtime: this file must not match its own probe
EXTRA_ROOT_DEPTH = 8  # directory levels below a root named in $FAKE_SCAN_ROOTS
MAX_ENTRIES = 200_000  # entries looked at per root; a probe must end, even on a huge tree
MAX_SCANNED_BYTES = 200_000


def holds_marker(path: str) -> bool:
    try:
        if os.path.getsize(path) > MAX_SCANNED_BYTES:
            return False
        with open(path, errors="replace") as f:
            return MARKER in f.read()
    except OSError:
        return False


def scan(root: Path, names: set[str], depth: int | None) -> list[str]:
    """Hidden test files under `root`, in directories at most `depth` levels down (None: all), after at most
    `MAX_ENTRIES` entries."""
    found = []
    seen = 0
    for dirpath, dirnames, filenames in os.walk(root):
        if depth is not None and len(Path(dirpath).relative_to(root).parts) >= depth:
            dirnames[:] = []  # list this directory, go no deeper
        seen += len(dirnames) + len(filenames)
        for name in filenames:
            path = os.path.join(dirpath, name)
            if name in names or (name.endswith(".py") and holds_marker(path)):
                found.append(path)
        if seen > MAX_ENTRIES:
            break
    return found


def find_hidden_tests() -> list[str]:
    """Every hidden test file an unsandboxed agent finds from where it runs: the whole workdir tree, the files of
    each parent directory up to `/` (not their subdirectories), and the roots of $FAKE_SCAN_ROOTS (path-separated)
    `EXTRA_ROOT_DEPTH` levels down. It does not search `$HOME` or the temp dir: the benchmark may live there, and
    an unsandboxed agent can read anything the user can. Only the sandbox, which mounts the workdir alone, stops that."""
    names = {n for n in os.environ.get("FAKE_HIDDEN_NAMES", "").split(",") if n}
    cwd = Path(os.path.realpath(os.getcwd()))
    roots: list[tuple[Path, int | None]] = [(cwd, None)] + [(p, 0) for p in cwd.parents]
    extra = [r for r in os.environ.get("FAKE_SCAN_ROOTS", "").split(os.pathsep) if r]
    roots += [(Path(os.path.realpath(r)), EXTRA_ROOT_DEPTH) for r in extra]
    return sorted({path for root, depth in roots for path in scan(root, names, depth)})


kind, mode = sys.argv[1], sys.argv[2]
fix = mode in ("fix", "plant-link", "fork-bg", "slow-once") or mode.startswith("hide-")
for leaked in ("FAKE_KEY", "SECRET_CANARY"):
    if leaked in os.environ:
        sys.exit(9)
print(f"debug key={os.environ.get('FAKE_API_KEY', '')}", file=sys.stderr)
if mode == "probe-hidden":
    leaked_files = find_hidden_tests()
    if leaked_files:
        print(f"hidden tests visible to the agent: {leaked_files}", file=sys.stderr)
        sys.exit(7)
if os.environ.get("FAKE_PROBE_FILE"):
    import subprocess

    depth = subprocess.run(["git", "rev-list", "--all", "--count"], capture_output=True, text=True).stdout.strip()
    remotes = subprocess.run(["git", "remote"], capture_output=True, text=True).stdout.strip()
    with open(os.environ["FAKE_PROBE_FILE"], "a") as f:
        f.write(
            json.dumps(
                {
                    "cwd": os.getcwd(),
                    "depth": depth,
                    "remotes": remotes,
                    "sandbox": os.environ.get("FAKE_SANDBOX", ""),
                    "home": os.environ.get("HOME", ""),
                    "uid": os.getuid(),
                }
            )
            + "\n"
        )
if mode == "hack":
    with open("run_tests.py", "w") as f:
        f.write("import sys\nsys.exit(0)\n")
    os.makedirs("tests_infra", exist_ok=True)
    with open("tests_infra/conftest.py", "w") as f:
        f.write("# planted\n")
    import subprocess

    subprocess.run(["git", "-c", "user.name=x", "-c", "user.email=x@x", "commit", "-qam", "hack"], capture_output=True)


def hide_forbidden_change() -> None:
    """Tamper with the test infrastructure while trying to keep the change out of the diff."""
    import subprocess

    def sh(script: str) -> None:
        subprocess.run(["sh", "-c", script], capture_output=True)

    if mode.startswith("hide-git"):
        sh("git init -q . && git add -A && git update-index --skip-worktree run_tests.py")
        sh("echo '*' >> .git/info/exclude")
        sh("git -c user.name=x -c user.email=x@x commit -qm x && git replace $(git rev-list --max-parents=0 HEAD) HEAD")
        with open("run_tests.py", "w") as f:
            f.write("import sys\nsys.exit(0)\n")
        if mode == "hide-git-clean":  # leave no repo behind: the edit must still show by path
            sh("rm -rf .git")
    elif mode == "hide-ignore":
        with open(".gitignore", "w") as f:
            f.write("conftest.py\n")
        with open("conftest.py", "w") as f:
            f.write("# planted\n")
    elif mode == "hide-attr":
        with open(".gitattributes", "w") as f:
            f.write("* working-tree-encoding=UTF-16\n")
        with open("run_tests.py", "w") as f:
            f.write("import sys\nsys.exit(0)\n")
    else:  # hide-config
        marker = os.environ["FAKE_MARKER"]
        sh(
            f"git init -q . && git config core.fsmonitor 'touch {marker}; false'"
            f" && git config filter.x.clean 'touch {marker}; cat' && git config diff.x.textconv 'touch {marker}; cat'"
        )
        with open(".gitattributes", "w") as f:
            f.write("* filter=x diff=x\n")


if mode in ("artifacts", "artifacts-secret"):
    # Turns `data.csv` into the two artifacts of the examples-artifacts suite. `artifacts-secret` also leaks the key
    # of the agent's environment into the report, as an agent might paste its environment into a document.
    leak = f"<p>{os.environ.get('FAKE_API_KEY', '')}</p>" if mode == "artifacts-secret" else ""
    with open("report.html", "w") as f:
        f.write(
            "<!doctype html><html><head><meta charset='utf-8'><title>Report</title></head><body>"
            f"<h1>Sales by region</h1><table><tr><td>north</td><td>10</td></tr><tr><td>south</td><td>20</td></tr>"
            f"</table>{leak}</body></html>\n"
        )
    with open("summary.json", "w") as f:
        json.dump({"total": 30, "rows": 2, "regions": ["north", "south"]}, f)
if mode == "artifacts-link":
    # An agent that plants a symlink where the report goes, to make a grader read a host file and send it to the judge.
    os.symlink(os.environ["FAKE_CANARY_FILE"], "report.html")
    with open("summary.json", "w") as f:
        json.dump({"total": 245, "rows": 3, "regions": ["north", "south", "east"]}, f)
BACKGROUND_WRITER = "import sys, time\nwhile True:\n    open(sys.argv[1], 'a').write('x')\n    time.sleep(0.05)\n"


def leave_background_process() -> None:
    """Start a process that outlives this one unless the engine kills the process group, and wait until it writes."""
    import subprocess
    import time

    path = os.environ["FAKE_BG_FILE"]
    subprocess.Popen([sys.executable, "-c", BACKGROUND_WRITER, path])
    deadline = time.time() + 30
    while not (os.path.exists(path) and os.path.getsize(path)) and time.time() < deadline:
        time.sleep(0.02)


def wait_while_flag_exists() -> None:
    """Sleep while $FAKE_SLOW_FLAG exists, but stop when the parent process dies."""
    import time

    parent, flag = os.getppid(), os.environ["FAKE_SLOW_FLAG"]
    if os.environ.get("FAKE_SLOW_PID"):  # lets the test make sure no agent outlives it
        with open(os.environ["FAKE_SLOW_PID"], "w") as f:
            f.write(str(os.getpid()))
    deadline = time.time() + 90
    while os.path.exists(flag) and os.getppid() == parent and time.time() < deadline:
        time.sleep(0.05)


def write_for_a_minute() -> None:
    import time

    deadline = time.time() + 60
    while time.time() < deadline:
        with open("late.txt", "a") as f:
            f.write("x")
        time.sleep(0.05)


if mode == "slow-writer":
    write_for_a_minute()
if mode == "fork-bg":
    leave_background_process()
if mode == "slow-once":
    wait_while_flag_exists()
if mode == "plant-link":
    os.symlink(os.environ["FAKE_CANARY_FILE"], "test_hidden.py")
if mode.startswith("hide-"):
    hide_forbidden_change()
if fix:
    with open("fizzbuzz.py", "w") as f:
        f.write(
            "def fizzbuzz(n):\n    if n % 15 == 0:\n        return 'FizzBuzz'\n"
            "    if n % 3 == 0:\n        return 'Fizz'\n    if n % 5 == 0:\n"
            "        return 'Buzz'\n    return str(n)\n"
        )

if kind == "claude":
    # stream-json shape: assistant events (one repeated tool_use block) before the result
    for index, name in enumerate(("Read", "Edit", "Edit")):
        block = {"type": "tool_use", "id": f"toolu_{min(index, 1)}", "name": name}
        print(json.dumps({"type": "assistant", "message": {"id": f"msg_{index}", "content": [block]}}))
    print(
        json.dumps(
            {
                "type": "result",
                "num_turns": 4,
                "total_cost_usd": 0.05,
                "usage": {"input_tokens": 10, "output_tokens": 500},
                "modelUsage": {
                    "main": {
                        "inputTokens": 10,
                        "outputTokens": 500,
                        "cacheReadInputTokens": 40000,
                        "cacheCreationInputTokens": 12000,
                    },
                    "helper": {"inputTokens": 300, "outputTokens": 20},
                },
            }
        )
    )
elif kind == "opencode":
    print("progress text that is not JSON")
    for _ in range(2):
        print(json.dumps({"type": "tool_use", "part": {"type": "tool"}}))
    for _ in range(3):
        print(
            json.dumps(
                {
                    "type": "step_finish",
                    "part": {
                        "cost": 0.004,
                        "tokens": {"input": 2000, "output": 150, "reasoning": 50, "cache": {"read": 8000, "write": 0}},
                    },
                }
            )
        )
elif kind == "codex":
    print(json.dumps({"type": "thread.started"}))
    for item in ("command_execution", "command_execution", "agent_message"):
        print(json.dumps({"type": "item.completed", "item": {"type": item}}))
    print(
        json.dumps(
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 20000,
                    "cached_input_tokens": 15000,
                    "output_tokens": 900,
                    "reasoning_output_tokens": 400,
                },
            }
        )
    )
elif kind == "cursor":
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "ok"}))
elif kind == "pi":
    print(json.dumps({"type": "session", "version": 3}))
    msg = {
        "role": "assistant",
        "content": [{"type": "text", "text": "ok"}, {"type": "toolCall", "id": "call_1", "name": "bash"}],
        "usage": {"input": 800, "output": 120, "cacheRead": 1500, "cacheWrite": 0, "cost": {"total": 0.001}},
    }
    for _ in range(3):  # same usage on 3 event types: only message_end must count
        for typ in ("message_update", "message_end", "turn_end"):
            print(json.dumps({"type": typ, "message": msg}))
else:
    sys.exit(f"unknown kind {kind}")

if os.environ.get("BENCH_TAG") and kind != "cursor":
    with open(os.environ["FAKE_TAGS_FILE"], "a") as f:
        f.write(os.environ["BENCH_TAG"] + "\n")
