#!/usr/bin/env python3
"""harness-bench: compare coding-agent harness x model pairs.

Metrics per pair: pass rate, tokens, cost per run, cost per solved task,
cost per 1M tokens. Python 3.12+, standard library only.

Usage:
  python3 harness_bench.py run    --config config.json [--reps 3] [--only HARNESS] [--keep] [--dry-run]
  python3 harness_bench.py reconcile --config config.json   # pull real spend from LiteLLM
  python3 harness_bench.py report --config config.json
"""
import argparse
import csv
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

if sys.version_info < (3, 12):
    sys.exit("Python 3.12+ required")


# ---------------------------------------------------------------- helpers

def num(d, *keys):
    """First numeric value found under any of keys, else 0."""
    if not isinstance(d, dict):
        return 0
    for k in keys:
        v = d.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return v
    return 0


def json_lines(text):
    """Yield JSON objects from a JSONL stream; skip non-JSON lines."""
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def empty_usage():
    return {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0,
            "cache_write": 0, "cost_reported": None, "turns": 0}


def expand_env(value):
    return re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), ""), value)


# ---------------------------------------------------------------- parsers
# Each parser takes the agent stdout and returns a usage dict or None.

def parse_claude(stdout):
    """claude -p --output-format json (single result object; stream-json also works)."""
    result = None
    for obj in json_lines(stdout):
        if obj.get("type") == "result":
            result = obj
    if result is None:
        try:
            result = json.loads(stdout)
        except json.JSONDecodeError:
            return None
    u = empty_usage()
    model_usage = result.get("modelUsage")
    if isinstance(model_usage, dict) and model_usage:
        # Includes side calls (subagents, small helper model), not only the main model.
        for m in model_usage.values():
            u["input"] += num(m, "inputTokens")
            u["output"] += num(m, "outputTokens")
            u["cache_read"] += num(m, "cacheReadInputTokens")
            u["cache_write"] += num(m, "cacheCreationInputTokens")
    else:
        usage = result.get("usage", {})
        u["input"] = num(usage, "input_tokens")
        u["output"] = num(usage, "output_tokens")
        u["cache_read"] = num(usage, "cache_read_input_tokens")
        u["cache_write"] = num(usage, "cache_creation_input_tokens")
    if isinstance(result.get("total_cost_usd"), (int, float)):
        u["cost_reported"] = result["total_cost_usd"]
    u["turns"] = num(result, "num_turns")
    return u


def parse_opencode(stdout):
    """opencode run --format json: sum every step_finish event."""
    u = empty_usage()
    cost, seen = 0.0, False
    for obj in json_lines(stdout):
        if str(obj.get("type", "")).replace("-", "_") != "step_finish":
            continue
        part = obj.get("part", obj)
        tok = part.get("tokens", {}) or {}
        cache = tok.get("cache") if isinstance(tok.get("cache"), dict) else {}
        u["input"] += num(tok, "input")
        u["output"] += num(tok, "output")
        u["reasoning"] += num(tok, "reasoning")
        u["cache_read"] += num(cache, "read") or num(tok, "cacheRead", "cache_read")
        u["cache_write"] += num(cache, "write") or num(tok, "cacheWrite", "cache_write")
        cost += num(part, "cost")
        u["turns"] += 1
        seen = True
    if not seen:
        return None
    u["cost_reported"] = cost
    return u


def parse_pi(stdout):
    """pi --mode json: sum usage of assistant messages on message_end events only."""
    u = empty_usage()
    cost, seen, has_cost = 0.0, False, False
    for obj in json_lines(stdout):
        if obj.get("type") != "message_end":
            continue
        msg = obj.get("message", {}) or {}
        if msg.get("role") != "assistant":
            continue
        usage = msg.get("usage", {}) or {}
        u["input"] += num(usage, "input", "inputTokens", "input_tokens")
        u["output"] += num(usage, "output", "outputTokens", "output_tokens")
        u["cache_read"] += num(usage, "cacheRead", "cache_read")
        u["cache_write"] += num(usage, "cacheWrite", "cache_write")
        c = usage.get("cost")
        if isinstance(c, dict) and isinstance(c.get("total"), (int, float)):
            cost += c["total"]
            has_cost = True
        elif isinstance(c, (int, float)):
            cost += c
            has_cost = True
        u["turns"] += 1
        seen = True
    if not seen:
        return None
    u["cost_reported"] = cost if has_cost else None
    return u


def parse_cursor(stdout):
    """agent -p --output-format json. Official docs show no usage fields; some versions
    emit usage{inputTokens,outputTokens,cacheReadTokens,cacheWriteTokens}. Returns None
    when no usage is present (pass rate and time are still recorded)."""
    result = None
    for obj in json_lines(stdout):
        if obj.get("type") == "result":
            result = obj
    if not result or not isinstance(result.get("usage"), dict):
        return None
    usage = result["usage"]
    u = empty_usage()
    u["input"] = num(usage, "inputTokens", "input_tokens")
    u["output"] = num(usage, "outputTokens", "output_tokens")
    u["cache_read"] = num(usage, "cacheReadTokens", "cache_read_tokens")
    u["cache_write"] = num(usage, "cacheWriteTokens", "cache_write_tokens")
    cost = num(usage, "totalCostUSD") or num(result, "total_cost_usd")
    u["cost_reported"] = cost or None
    u["turns"] = 1
    return u


def parse_codex(stdout):
    """codex exec --json: usage on turn.completed is the thread's running total, so take
    the last one. cached_input_tokens is a subset of input_tokens; reasoning_output_tokens
    is a subset of output_tokens (not added again)."""
    last, turns = None, 0
    for obj in json_lines(stdout):
        if obj.get("type") == "turn.completed" and isinstance(obj.get("usage"), dict):
            last = obj["usage"]
            turns += 1
    if last is None:
        return None
    u = empty_usage()
    cached = num(last, "cached_input_tokens")
    u["input"] = max(num(last, "input_tokens") - cached, 0)
    u["cache_read"] = cached
    u["output"] = num(last, "output_tokens")
    u["turns"] = turns
    return u


PARSERS = {"claude": parse_claude, "opencode": parse_opencode, "pi": parse_pi,
           "cursor": parse_cursor, "codex": parse_codex}


# ---------------------------------------------------------------- cost

def computed_cost(u, price):
    """Cost from token counts and a USD per 1M tokens price entry."""
    if not price or not any(v for k, v in price.items() if not k.startswith("_")):
        return None  # missing or all-zero price entry: fall back to reported cost
    out_price = price.get("output", 0)
    return (u["input"] * price.get("input", 0)
            + (u["output"] + u["reasoning"]) * out_price
            + u["cache_read"] * price.get("cache_read", price.get("input", 0))
            + u["cache_write"] * price.get("cache_write", price.get("input", 0))) / 1e6


# ---------------------------------------------------------------- run

def sh(cmd, cwd, timeout, env=None, log=None):
    """Run a shell command; return (returncode, timed_out)."""
    try:
        p = subprocess.run(cmd, shell=True, cwd=cwd, env=env, timeout=timeout,
                           stdin=subprocess.DEVNULL, capture_output=True, text=True)
        if log:
            log.write_text(p.stdout + "\n--- stderr ---\n" + p.stderr)
        return p.returncode, False
    except subprocess.TimeoutExpired:
        return -1, True


def is_git_source(src):
    return src.endswith(".git") or "://" in src or src.startswith("git@") or (Path(src) / ".git").exists()


def prepare_workdir(task, base, workdir):
    src = task["repo"]
    if not re.match(r"^(\w+://|git@)", src) and not Path(src).is_absolute():
        src = str((base / src).resolve())
    if is_git_source(src):
        cache = base / ".cache" / task["id"]
        if not cache.exists():
            cache.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "clone", "--quiet", src, str(cache)], check=True)
        subprocess.run(["git", "clone", "--quiet", "--local", str(cache), str(workdir)], check=True)
        if task.get("ref"):
            subprocess.run(["git", "-C", str(workdir), "checkout", "--quiet", task["ref"]], check=True)
    else:
        shutil.copytree(src, workdir)


def build_cmd(template, model, prompt):
    provider, _, model_name = model.partition("/") if "/" in model else ("", "", model)
    fields = {"model": model, "provider": provider, "model_name": model_name,
              "prompt": prompt}
    return [part.format(**fields) for part in template]


def done_keys(results_path):
    keys = set()
    if results_path.exists():
        for obj in json_lines(results_path.read_text()):
            keys.add((obj["task"], obj["harness"], obj["model"], obj["rep"]))
    return keys


def cmd_run(args, cfg, base):
    out_dir = base / cfg.get("output_dir", "results")
    out_dir.mkdir(parents=True, exist_ok=True)
    logs_dir = out_dir / "logs"
    logs_dir.mkdir(exist_ok=True)
    work_root = base / cfg.get("work_dir", "work")
    results_path = out_dir / "results.jsonl"
    finished = done_keys(results_path)
    reps = args.reps or cfg.get("reps", 1)
    harnesses = cfg["harnesses"]
    prices = cfg.get("prices", {})

    for pair in cfg["matrix"]:
        hname, model = pair["harness"], pair["model"]
        if args.only and hname not in args.only:
            continue
        h = harnesses[hname]
        price = prices.get(pair.get("price", model))
        for task in cfg["tasks"]:
            for rep in range(1, reps + 1):
                key = (task["id"], hname, model, rep)
                if key in finished and not args.dry_run:
                    print(f"skip  {key} (done)")
                    continue
                cmd = build_cmd(h["command"], model, task["prompt"])
                if args.dry_run:
                    print(shlex.join(cmd))
                    continue
                tag = f"{task['id']}__{hname}__{re.sub(r'[^A-Za-z0-9_.-]', '_', model)}__r{rep}"
                workdir = work_root / tag
                run_tag = f"bench-{datetime.now().strftime('%Y%m%d%H%M%S')}-{tag}"
                env = os.environ.copy()
                env["BENCH_TAG"] = run_tag  # Pi models.json can reference $BENCH_TAG in headers
                env.update({k: expand_env(v).replace("{run_tag}", run_tag)
                            for k, v in h.get("env", {}).items()})
                if workdir.exists():
                    shutil.rmtree(workdir)
                workdir.parent.mkdir(parents=True, exist_ok=True)
                prepare_workdir(task, base, workdir)

                if task.get("setup"):
                    sh(task["setup"], workdir, task.get("setup_timeout", 900),
                       log=logs_dir / f"{tag}.setup.log")

                print(f"run   {tag}", flush=True)
                t0 = time.time()
                timed_out = False
                try:
                    p = subprocess.run(cmd, cwd=workdir, env=env, stdin=subprocess.DEVNULL,
                                       capture_output=True, text=True,
                                       timeout=task.get("timeout", cfg.get("timeout", 1800)))
                    stdout, stderr, rc = p.stdout, p.stderr, p.returncode
                except subprocess.TimeoutExpired as e:
                    stdout = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
                    stderr = e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or "")
                    rc, timed_out = -1, True
                wall = time.time() - t0
                (logs_dir / f"{tag}.agent.jsonl").write_text(stdout)
                (logs_dir / f"{tag}.agent.stderr").write_text(stderr)

                usage = PARSERS[h["parser"]](stdout)
                parse_ok = usage is not None
                usage = usage or empty_usage()

                if task.get("hidden_tests"):
                    src = (base / task["hidden_tests"]).resolve()
                    shutil.copytree(src, workdir, dirs_exist_ok=True)
                test_rc, test_to = sh(task["test"], workdir, task.get("test_timeout", 900),
                                      log=logs_dir / f"{tag}.test.log")

                row = {
                    "ts": datetime.now(UTC).isoformat(timespec="seconds"),
                    "task": task["id"], "harness": hname, "model": model, "rep": rep,
                    "passed": test_rc == 0 and not test_to,
                    "agent_rc": rc, "agent_timed_out": timed_out,
                    "wall_s": round(wall, 1), "parse_ok": parse_ok,
                    "run_tag": run_tag, "t_start": t0, "t_end": t0 + wall,
                    **usage,
                    "cost_computed": computed_cost(usage, price) if parse_ok else None,
                }
                with results_path.open("a") as f:
                    f.write(json.dumps(row) + "\n")
                print(f"done  {tag} passed={row['passed']} tokens="
                      f"{usage['input'] + usage['output'] + usage['reasoning'] + usage['cache_read'] + usage['cache_write']}"
                      f" cost={row['cost_computed'] if row['cost_computed'] is not None else usage['cost_reported']}",
                      flush=True)
                if not args.keep:
                    shutil.rmtree(workdir, ignore_errors=True)
    if not args.dry_run and not getattr(args, "no_report", False):
        cmd_report(args, cfg, base)


# ---------------------------------------------------------------- LiteLLM

def litellm_get(base_url, key, path, params):
    url = base_url.rstrip("/") + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode())


def litellm_rows(base_url, key, start, end):
    """Individual spend log rows. Tries /spend/logs/v2 (paginated), then legacy /spend/logs."""
    fmt = "%Y-%m-%d %H:%M:%S"
    try:
        rows, page = [], 1
        while True:
            data = litellm_get(base_url, key, "/spend/logs/v2", {
                "start_date": start.strftime(fmt), "end_date": end.strftime(fmt),
                "page": page, "page_size": 1000})
            batch = data.get("data", []) if isinstance(data, dict) else data
            rows += batch
            total_pages = data.get("total_pages", 1) if isinstance(data, dict) else 1
            if page >= total_pages or not batch:
                return rows
            page += 1
    except Exception as e:  # noqa: BLE001
        print(f"/spend/logs/v2 failed ({e}); trying legacy /spend/logs")
    data = litellm_get(base_url, key, "/spend/logs", {
        "start_date": start.strftime("%Y-%m-%d"),
        "end_date": (end).strftime("%Y-%m-%d"), "summarize": "false"})
    return data.get("data", []) if isinstance(data, dict) else data


def row_tags(r):
    tags = r.get("request_tags") or []
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except json.JSONDecodeError:
            tags = [t.strip() for t in tags.split(",")]
    return tags if isinstance(tags, list) else []


def cmd_reconcile(args, cfg, base):
    lcfg = cfg.get("litellm") or sys.exit("config has no 'litellm' section")
    base_url, key = expand_env(lcfg["base_url"]), expand_env(lcfg["api_key"])
    if not base_url or not key:
        sys.exit("litellm.base_url / litellm.api_key empty (check env vars)")
    results_path = base / cfg.get("output_dir", "results") / "results.jsonl"
    runs = list(json_lines(results_path.read_text()))
    tagged = [r for r in runs if r.get("run_tag")]
    if not tagged:
        sys.exit("no runs with run_tag")
    start = datetime.fromtimestamp(min(r["t_start"] for r in tagged) - 3600, UTC)
    end = datetime.fromtimestamp(max(r["t_end"] for r in tagged) + 3600, UTC)
    logs = litellm_rows(base_url, key, start, end)
    by_tag = {}
    for row in logs:
        for t in row_tags(row):
            by_tag.setdefault(t, []).append(row)
    matched = 0
    for r in runs:
        hits = by_tag.get(r.get("run_tag"), [])
        if hits:
            matched += 1
            r["cost_litellm"] = sum(num(h, "spend") for h in hits)
            r["litellm_requests"] = len(hits)
            r["litellm_prompt_tokens"] = sum(num(h, "prompt_tokens") for h in hits)
            r["litellm_completion_tokens"] = sum(num(h, "completion_tokens") for h in hits)
    results_path.write_text("".join(json.dumps(r) + "\n" for r in runs))
    print(f"LiteLLM: {len(logs)} log rows, {matched}/{len(runs)} runs matched by tag")
    if matched < len(tagged):
        print("Unmatched runs: harness did not send x-litellm-tags, run not via LiteLLM "
              "(e.g. Cursor), or logs not written yet.")
    cmd_report(args, cfg, base)


# ---------------------------------------------------------------- report

def fmt_money(v):
    if v is None:
        return "n/a"
    if math.isinf(v):
        return "inf"
    return f"${v:.4f}" if v < 1 else f"${v:.2f}"


def fmt_int(v):
    return "n/a" if v is None or math.isinf(v) else f"{v:,.0f}"


def aggregate(rows, cost_source):
    groups = {}
    for r in rows:
        groups.setdefault((r["harness"], r["model"]), []).append(r)
    table = []
    for (h, m), rs in groups.items():
        n = len(rs)
        solved = sum(1 for r in rs if r["passed"])
        tokens = sum(r["input"] + r["output"] + r.get("reasoning", 0)
                     + r["cache_read"] + r["cache_write"] for r in rs)
        fresh_in = sum(r["input"] for r in rs)
        cache_read = sum(r["cache_read"] for r in rs)

        def run_cost(r):
            if cost_source != "auto":
                v = r.get(f"cost_{cost_source}")
                return v, (cost_source if v is not None else None)
            for src in ("litellm", "computed", "reported"):
                if r.get(f"cost_{src}") is not None:
                    return r[f"cost_{src}"], src
            return None, None

        picked = [run_cost(r) for r in rs]
        costs = [c for c, _ in picked]
        cost = None if any(c is None for c in costs) else sum(costs)
        sources = sorted({src for _, src in picked if src})
        table.append({
            "harness": h, "model": m, "cost_source": "+".join(sources) or "n/a",
            "runs": n, "solved": solved,
            "pass_rate": solved / n,
            "tokens_total": tokens,
            "tokens_per_run": tokens / n,
            "tokens_per_solved": tokens / solved if solved else math.inf,
            "cache_hit": cache_read / (cache_read + fresh_in) if cache_read + fresh_in else 0,
            "cost_total": cost,
            "cost_per_run": cost / n if cost is not None else None,
            "cost_per_solved": (cost / solved if solved else math.inf) if cost is not None else None,
            "cost_per_1m_tokens": cost / tokens * 1e6 if cost is not None and tokens else None,
            "avg_turns": sum(r["turns"] for r in rs) / n,
            "avg_wall_s": sum(r["wall_s"] for r in rs) / n,
            "parse_failures": sum(1 for r in rs if not r["parse_ok"]),
        })

    def sort_key(t):
        c = t["cost_per_solved"]
        return (c is None, c if c is not None else 0, -t["pass_rate"])
    table.sort(key=sort_key)
    return table


def cmd_report(args, cfg, base):
    out_dir = base / cfg.get("output_dir", "results")
    results_path = out_dir / "results.jsonl"
    if not results_path.exists():
        sys.exit(f"no results at {results_path}")
    rows = list(json_lines(results_path.read_text()))
    cost_source = getattr(args, "cost_source", None) or cfg.get("cost_source", "auto")
    table = aggregate(rows, cost_source)

    with (out_dir / "summary.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(table[0].keys()))
        w.writeheader()
        w.writerows(table)

    head = ("| # | Harness | Modelo | Resolvidas | Taxa | Custo/resolvida | Custo/execução "
            "| Custo/1M tokens | Tokens/resolvida | Tokens/execução | Cache hit | Turnos | Tempo (s) "
            "| Fonte custo |")
    lines = [f"# Harness x modelo — {datetime.now().strftime('%Y-%m-%d %H:%M')}",
             "", f"Fonte de custo: `{cost_source}` (auto = litellm > computed > reported). Ordenado por custo por tarefa resolvida.", "",
             head, "|" + "---|" * 14]
    for i, t in enumerate(table, 1):
        lines.append(
            f"| {i} | {t['harness']} | {t['model']} | {t['solved']}/{t['runs']} "
            f"| {t['pass_rate']:.0%} | {fmt_money(t['cost_per_solved'])} | {fmt_money(t['cost_per_run'])} "
            f"| {fmt_money(t['cost_per_1m_tokens'])} | {fmt_int(t['tokens_per_solved'])} "
            f"| {fmt_int(t['tokens_per_run'])} | {t['cache_hit']:.0%} | {t['avg_turns']:.1f} "
            f"| {t['avg_wall_s']:.0f} | {t['cost_source']} |")
    bad = [t for t in table if t["parse_failures"]]
    if bad:
        lines += ["", "**Atenção:** sem dados de tokens do harness em: "
                  + ", ".join(f"{t['harness']}/{t['model']} ({t['parse_failures']})" for t in bad)
                  + ". Veja `results/logs/*.agent.jsonl`."]

    # per-task pass matrix
    tasks = sorted({r["task"] for r in rows})
    pairs = [(t["harness"], t["model"]) for t in table]
    lines += ["", "## Por tarefa (resolvidas/execuções)", "",
              "| Tarefa | " + " | ".join(f"{h}<br>{m}" for h, m in pairs) + " |",
              "|---|" + "---|" * len(pairs)]
    for task in tasks:
        cells = []
        for h, m in pairs:
            rs = [r for r in rows if r["task"] == task and r["harness"] == h and r["model"] == m]
            cells.append(f"{sum(r['passed'] for r in rs)}/{len(rs)}" if rs else "-")
        lines.append(f"| {task} | " + " | ".join(cells) + " |")

    md = "\n".join(lines) + "\n"
    (out_dir / "summary.md").write_text(md)
    print("\n" + md)
    print(f"saved {out_dir / 'summary.md'} and {out_dir / 'summary.csv'}")


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--config", required=True)
    r.add_argument("--reps", type=int)
    r.add_argument("--only", nargs="*", help="only these harness names")
    r.add_argument("--keep", action="store_true", help="keep work directories")
    r.add_argument("--dry-run", action="store_true", help="print commands only")
    p = sub.add_parser("report")
    p.add_argument("--config", required=True)
    p.add_argument("--cost-source", choices=["auto", "litellm", "computed", "reported"])
    c = sub.add_parser("reconcile", help="fetch real spend per run from LiteLLM")
    c.add_argument("--config", required=True)
    args = ap.parse_args()

    cfg_path = Path(args.config).resolve()
    cfg = json.loads(cfg_path.read_text())
    base = cfg_path.parent
    {"run": cmd_run, "report": cmd_report, "reconcile": cmd_reconcile}[args.cmd](args, cfg, base)


if __name__ == "__main__":
    main()
