# ADR 0010: Pluggable sandbox, limits, resume and doctor (Phase 6)

- Status: accepted
- Date: 2026-11-24

## Context

Phase 6 of the [evolution plan](../evolution-plan.md) asks for a `Sandbox` protocol with
`local-tmp` and `docker` implementations, per-trial limits (timeout, CPU, memory,
network with a gateway allowlist), heartbeat and resume of an interrupted run by
`trial_id`, and the image digest in the result. Roadmap M7 (image digest), M8 (`doctor`)
and M15 (egress restriction) are scheduled here. Today the sandbox is a `sandbox.prefix`
template applied by `wrap()`, and resume is by `done_keys` over `results.jsonl`.

## Decision

1. **The engine still runs every process** ([ADR 0007](0007-runner-and-grader-protocols.md)).
   A sandbox only builds argv and manages its own lifecycle. Protocol in
   `hbench/sandboxes/base.py`:
   - `Sandbox.create(workdir: Path, trial_id: str) -> SandboxSession`
   - `SandboxSession.argv(argv: list[str], env_file: Path) -> list[str]`
   - `SandboxSession.copy_in(src: Path, rel: str)` / `copy_out(rel: str, dest: Path)`:
     host copies through `safe_copy` (the workdir is a bind mount)
   - `SandboxSession.destroy()`: removes anything left (for docker, `docker rm -f` of the
     trial's named containers), always called in a `finally`
   - `Sandbox.image_digest() -> str | None`, `Sandbox.describe() -> str`.
   The plan's `exec` is `argv` + the engine's `run_proc`.
2. **Implementations:** `prefix` (today's `sandbox.prefix`, byte-identical argv, so old
   configs and goldens keep working), `docker` (structured config, below) and
   `local-tmp` (no isolation; requires `--allow-unsandboxed`, same rule as today). Third
   parties plug in through the entry-point group `harness_bench.sandboxes`
   (`hbench/plugins.py`).
3. **Config:** `sandbox: {"type": "docker", "image": "<name>@sha256:<64 hex>",
   "network": "none" | "<docker network>", "limits": {"cpus": float, "memory": "2g",
   "pids": int}}`. `type` defaults to `prefix` when `prefix` is set, else to none
   (unsandboxed). The docker image **must** be pinned by digest; a tag is a config error.
4. **Docker argv:** `docker run --rm --name hb-<trial_id>-<n> --env-file <env_file>
   -v <workdir>:/work -w /work --network <network> --cpus --memory --memory-swap
   (= memory) --pids-limit --cap-drop ALL --security-opt no-new-privileges
   --user <uid>:<gid> <image> <argv>`. `--network none` by default.
5. **Gateway allowlist (M15):** without a proxy. The user creates an internal Docker
   network (`docker network create --internal hb-egress`) and attaches only the LiteLLM
   container to it; the docs give the commands. `doctor` checks the configured network is
   `Internal: true` (or `none`) and warns otherwise.
6. **Process cleanup for every sandbox:** `run_proc` starts each command in a new session
   (`start_new_session=True`) and kills the whole process group after it exits or times
   out, so an agent cannot leave processes running into grading (this closes the
   unsandboxed race noted in the Phase 5 review). `local-tmp` also sets `RLIMIT_CPU`,
   `RLIMIT_AS` and `RLIMIT_NPROC` from `limits` through `preexec_fn` (best effort,
   documented as no isolation).
7. **Image digest (M7):** `docker` records the pinned digest in `TrialResult.image_digest`
   and `RunInfo` (new field `sandbox: str`, the sandbox description; contract `1.3.0`,
   additive).
8. **Resume by `trial_id`:** `run --resume <run_id>` reopens `<output_dir>/<run_id>/`,
   keeps the same `run_id` (so `trial_id`s match) and skips every trial already in its
   `trials.jsonl`. Each trial is appended to `trials.jsonl` (flushed and `fsync`ed)
   **before** the legacy row, so a crash between the two can only lose the legacy row,
   which resume rewrites; legacy `done_keys` resume without `--resume` is unchanged.
   Leftover work dirs, env files and git dirs of the interrupted trial are removed.
9. **Heartbeat:** `<run_id>/heartbeat.json` (`pid`, `hostname`, `trial_id`, `phase`,
   `updated_at`) written atomically at each phase change and every 30 s by a daemon
   thread. `--resume` refuses while the heartbeat is fresh (< 90 s) and its pid is alive on
   this host, unless `--force`.
10. **`doctor` (M8):** `python3 -m hbench doctor --config C [--network]` spends no tokens:
    config valid, env vars set, each harness CLI present (`shutil.which` for `local-tmp`
    and `prefix`; `docker run <image> <cli> --version` through the engine for `docker`),
    docker image present with the pinned digest, network internal, judge/LiteLLM URLs
    valid. `--network` adds one `GET` to LiteLLM (`/health/liveliness`) and one
    `/spend/logs/v2` page of size 1 to check the spend key permission; this stays within
    the AGENTS.md network rule (GETs to the configured LiteLLM). Exit 1 on any failure.

## Consequences

- Old `sandbox.prefix` configs run unchanged; `docker` gives limits, a no-network default
  and a recorded digest.
- `wrap()` and the env file change, so `make docker-e2e` must pass.
