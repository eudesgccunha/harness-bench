# Design: a skills axis for the benchmark

Status: proposal, not implemented. Nothing in `harness_bench.py` changes until this design is accepted.

## Why

harness-bench ranks **harness × model** pairs by cost per solved task. It does not say whether a skill (Superpowers TDD, Pocock `diagnosing-bugs`, Caveman, an `AGENTS.md` block) helps. The [agent-skill-cookbook](https://github.com/eudesgccunha/agent-skill-cookbook) recommends skills for one developer's scenarios, and almost all of those recommendations are `read` or `web` evidence, not `tested`. This axis turns each recommendation into a measurement.

"Best" here means best for **this developer's scenarios**, not best in the community.

## Question the axis answers

For a fixed harness, model and task set, does adding skill set S change the **solve rate** or the **cost per solved task**, compared with no skill and compared with another skill set?

## New concept: skill set

A skill set is a pinned, read-only bundle plus an optional instruction block.

```json
"skill_sets": {
  "none": {},
  "sp-tdd": {
    "pack": "skill_packs/superpowers@22f0b46",
    "skills": ["test-driven-development", "verification-before-completion"],
    "instructions": "Use test-driven-development for every code change."
  },
  "pocock-tdd": {
    "pack": "skill_packs/mattpocock-skills@49dd158",
    "skills": ["tdd"],
    "instructions": "Use tdd for every code change."
  }
},
"matrix": [
  {"harness": "claude-code", "model": "m1", "skill_set": "none"},
  {"harness": "claude-code", "model": "m1", "skill_set": "sp-tdd"},
  {"harness": "claude-code", "model": "m1", "skill_set": "pocock-tdd"}
]
```

- `none` is the control. Every comparison needs it.
- `pack` points to a directory pinned by commit. The engine never downloads packs.
- `skills` lists the skill folders copied in. Only those are visible to the agent.
- `instructions` is appended to the task's `AGENTS.md` block (or the harness equivalent), identical wording across sets so only the skill differs.

## Rules inherited from `AGENTS.md`

| Rule | How the axis keeps it |
|---|---|
| Standard library only | Copy and hash files with `shutil` and `hashlib`. No new dependency |
| Only network access is a `GET` to LiteLLM | `skill_packs/` are vendored by you, or baked into the sandbox image at build time. The engine does not fetch from GitHub or npm |
| Untrusted agent and repo | The pack is mounted read-only and lives outside the workdir. The agent cannot edit a skill during a run |
| Env from an allowlist | Unchanged. Skills get no new variables. The spend key stays out |
| `protected_paths` restored | Unchanged. The skill directory is not part of the audit diff and is never restored from the agent's copy |
| Unknown cost is `None`, never `0` | Unchanged |
| `wrap()` and no `shell=True` | Unchanged. The skill home is bind-mounted by `sandbox.prefix` |
| A new defense gets a smoke case | See [Tests](#tests) |

## How a run changes

1. Resolve `skill_set` for the pair. Fail if the pack path is missing, not under `skill_packs/`, or contains a symlink that leaves the pack (`validate_config`).
2. Hash the pack files and the instruction text. Record the hashes.
3. Create a per-run agent home outside the workdir (`<work_root>/<tag>.home`). Copy only the listed skills into the path the harness reads:

   | Harness | User skills path (verify per version) |
   |---|---|
   | Claude Code | `~/.claude/skills` |
   | OpenCode | `~/.config/opencode/skills` |
   | Pi | `~/.pi/agent/skills` |
   | Codex | `~/.agents/skills` |

4. Point `HOME` (and `XDG_CONFIG_HOME` if needed) at that per-run home through the harness `env`, so no skill leaks in from the host.
5. Run the agent as today. Everything after (parse, diff, restore, record) is unchanged.
6. Delete the per-run home with `rmtree_inside`.

### Limitation: plugin hooks

Plain skill folders are portable. Plugin hooks are not: Superpowers loads `using-superpowers` through a session-start hook, and Caveman activates through hooks. A skill-folder install measures "the skill is available and the instructions mention it", not "the plugin runs as in daily use". Two ways to handle it:

- **Skill-only runs (default).** State the limitation next to every result.
- **Plugin runs.** Only where the harness can load a plugin from a directory, and only after the hook scripts pass the audit method in the cookbook's `security.md`. Hooks run shell commands, so this needs the egress restriction (M15) first. Unverified for each harness.

## Results and report

- Add `skill_set`, `skill_pack_hash` and `instructions_hash` to each `results.jsonl` row.
- Extend the resume key to `(task, harness, model, skill_set, rep)`.
- Group the report by `(harness, model, skill_set)`. Show solve rate with the Wilson interval (M1), cost per solved task, tokens, and the delta against `none`.
- Compare two skill sets on the same harness and model with the paired test from M11 (McNemar). Both ran the same tasks.
- Mark a delta as "not distinguishable" when the Wilson intervals overlap. With few runs, most deltas will be.

### How many runs

Ten tasks × three reps is 30 runs per cell. At a 50% solve rate the Wilson 95% interval is about ±17 points. That cannot show a 5 or 10 point effect. Plan for a larger effect (20 points or more), more tasks, or more reps. Say the interval in every conclusion.

## Tasks must match the scenarios

The cookbook scenarios decide the task set. Each task needs an automatic grader, because there is no human in the loop.

| Cookbook scenario | Gradable task | Grader |
|---|---|---|
| Bug fix | Repo with a failing hidden test | Hidden tests pass |
| New feature | Spec plus hidden tests | Hidden tests pass |
| Kubernetes manifest | Add a Deployment and Service to a repo layout | `kubeconform -strict` passes and a script asserts required fields (limits, probes, non-root, pinned image) |
| Refactor | Restructure a module | Existing tests unchanged and green, plus a lint or structure check |
| Diagram | Draw a flow of a given module as HTML | File parses, contains the required real names, and has no external URLs |
| Architecture exploration | Answer fixed questions about a repo | Exact-match facts and `path:line` checked by a script |
| Review | Diff with seeded defects | Share of seeded defects named in the review |
| Incident | Read-only diagnosis of a recorded failure | Root cause matches a fixed answer |

Tasks that cannot be graded by a script stay out of the benchmark. Do not add an LLM judge in this proposal.

## Experiments

| Id | Question | Sets compared | Task kinds |
|---|---|---|---|
| E1 | Does strict TDD help? | `none`, `sp-tdd`, `pocock-tdd` | Bug fix, new feature |
| E2 | Which debugging skill, and when? | `none`, `sp-debug`, `pocock-diagnose` | Easy bugs and hard or flaky bugs |
| E3 | Does a review skill find more defects? | `none`, `pocock-review`, `sp-review` | Seeded-defect diffs |
| E4 | What does Caveman cost or save? | `none`, `caveman` | Any. Watch output tokens and solve rate |
| E5 | Does an `AGENTS.md` workflow block help? | `none`, `block` | Any |
| E6 | Does Ponytail reduce diff size? | `none`, `ponytail` | New feature. Also report diff lines |

Run E1 first. It maps to the strongest claim in the cookbook ("Superpowers TDD fits agents that skip tests").

## Security of skill packs

A skill is instructions for the agent, and a pack may hold scripts. Before a pack enters `skill_packs/`:

1. Audit it with the cookbook's method (`getting-started/security.md`): network calls, reads of `.env` and secrets, hook scripts.
2. Record its commit and file hash.
3. Reject packs with hooks that use the network, unless the run has egress restriction.
4. Run with the benchmark's usual sandbox. The sandbox has no real secrets, only the LiteLLM key for that run.

A skill that tells the agent to read `.env` finds only the sandbox, which holds no `.env`. A skill that sends data out finds only the proxy host when M15 is in place.

## Tests

One smoke case per new defense, as `AGENTS.md` requires:

- The pack is mounted read-only: a fake agent tries to write into its skill folder and the write must fail or not persist.
- Only listed skills appear in the agent home: the fake agent lists the folder and reports the names.
- `HOME` points to the per-run home: the fake agent prints it and the test checks it is not the host home.
- The skill directory is not in the audit diff and is not restored from the agent's copy.
- `validate_config` rejects a pack outside `skill_packs/`, an escaping symlink and a missing pack.
- The per-run home is removed after the run, including after a timeout.
- The resume key separates two skill sets for the same pair.

`fake_agent.py` gains a probe for each case. `tests/docker_e2e.py` runs once with a skill set because `sandbox/` and `wrap()` are involved.

## Implementation order

1. M1 (Wilson interval) and M11 (paired test). Without them the deltas mean nothing.
2. M3 (`null` harness) as a sanity check for the graders.
3. `skill_sets` in config and `validate_config` checks.
4. Per-run home and the copy step, with the smoke cases.
5. Results fields, resume key and report grouping.
6. First experiment (E1) on 10 bug-fix and feature tasks.
7. Feed the result into the cookbook and raise the page from `read` to `tested`, with the date, versions and a link to the summary.

## What the cookbook does with the results

- A skill page moves to `Level: tested` only when a run covers it. The basis names the harness, model, task kinds, number of runs and the interval.
- If a result contradicts a recommendation, the page changes. The author's pick in `skills/tradeoffs.md` follows the data.
- Results that are inside the noise stay `read`, with a note that the benchmark could not separate the options.

## Open questions

- Which harnesses can load a plugin from a directory without a global install?
- Does each harness read user skills from a per-run `HOME`, or only from fixed paths?
- How to count a skill read as "used"? A tool-call count (M10) shows if the agent opened the skill at all. A skill that is never opened cannot help, and that is a finding by itself.
- Whether instruction text and skill text interact. E5 isolates the instruction block to check.
