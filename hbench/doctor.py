"""`doctor`: check that a benchmark can run, before it spends anything (ADR 0010, decision 10).

Every check is independent and reports `ok`, `warning` (ok, but worth a look) or a failure with a reason. The checks
start no agent and spend no tokens: they read the config and the environment, look up each harness CLI, ask Docker
about the image and the network, and, only with `--network`, make two `GET`s to the configured LiteLLM (a health
check and one page of spend logs, to see the spend key's permission). Details name variables, never their values.
"""

import json
import os
import secrets
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

from hbench.sandboxes import Sandbox
from hbench.sandboxes.docker import DockerSandbox
from hbench.suites import SuiteError

PROBE_TIMEOUT_S = 120


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    warning: bool = False  # passed, but the detail is worth reading


def run_checks(cfg: Mapping[str, Any], base: Path, network: bool, engine: ModuleType | None = None) -> list[Check]:
    """Every check of `cfg` (as loaded from the config file in `base`). `engine` is the `harness_bench` module
    (found like the CLI finds it when not given). An invalid config is one failed `config` check: nothing else can
    be judged then."""
    if engine is None:
        from hbench.cli import _engine  # here, not at the top: the CLI imports this module

        engine = _engine()
    try:
        expanded = engine.expand_suites(cfg, base)
        engine.validate_config(expanded)
    except SystemExit as e:  # the engine reports a config error by exiting with the message
        return [Check("config", False, str(e.code))]
    except (KeyError, SuiteError) as e:
        return [Check("config", False, f"missing or invalid setting: {e}")]
    sandbox: Sandbox = engine.make_sandbox(expanded)
    env = engine.base_env(expanded)
    checks = [Check("config", True, "valid"), _env_check(expanded, engine)]
    checks += _sandbox_checks(sandbox, expanded, engine, env)
    image_ready = all(c.ok for c in checks if c.name == "sandbox")
    checks += _harness_checks(expanded, sandbox, image_ready, engine, env)
    urls = _url_checks(expanded, engine)
    checks += urls
    if network and urls[0].ok and not urls[0].warning and expanded.get("litellm"):
        checks += _network_checks(expanded["litellm"], engine)
    return checks


# ---------------------------------------------------------------- environment


def _referenced_variables(cfg: Mapping[str, Any], engine: ModuleType) -> list[str]:
    """Every variable a `${VAR}` of the config reads: harness env, `litellm` and `judge`."""
    values = [v for harness in cfg["harnesses"].values() for v in harness.get("env", {}).values()]
    for section in ("litellm", "judge"):
        values += [v for v in (cfg.get(section) or {}).values() if isinstance(v, str)]
    return sorted({name for value in values for name in engine.env_var_names(value)})


def _env_check(cfg: Mapping[str, Any], engine: ModuleType) -> Check:
    """A `${VAR}` that is not set is a failure (it would expand to nothing); an `env_passthrough` name that is not
    set is only passed on when it is, so it is a warning."""
    names = _referenced_variables(cfg, engine)
    missing = [name for name in names if not os.environ.get(name)]
    unset_passthrough = [name for name in cfg.get("env_passthrough", []) if name not in os.environ]
    if missing:
        passthrough = f"; env_passthrough not set: {', '.join(unset_passthrough)}" if unset_passthrough else ""
        return Check("env", False, f"not set: {', '.join(missing)}{passthrough}")
    if unset_passthrough:
        return Check("env", True, f"env_passthrough not set (not passed on): {', '.join(unset_passthrough)}", True)
    return Check("env", True, f"{len(names)} variables set")


# ---------------------------------------------------------------- sandbox


def _run(engine: ModuleType, argv: list[str], env: Mapping[str, str], cwd: Path | None = None) -> tuple[int, str, str]:
    """`(returncode, stdout, stderr)` of a helper command through the engine's `run_proc`; 127 when it is missing."""
    try:
        rc, _, out, err = engine.run_proc(argv, cwd, env, PROBE_TIMEOUT_S)
    except OSError as e:
        return 127, "", f"{argv[0]}: {e.strerror}"
    return rc, out, err


def _first_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


def _sandbox_checks(
    sandbox: Sandbox, cfg: Mapping[str, Any], engine: ModuleType, env: Mapping[str, str]
) -> list[Check]:
    if not isinstance(sandbox, DockerSandbox):
        note = "" if sandbox.isolated else ": no isolation, a run needs --allow-unsandboxed"
        return [Check("sandbox", True, sandbox.describe() + note)]
    return [_image_check(sandbox, engine, env), _network_check(sandbox, engine, env)]


def _image_check(sandbox: DockerSandbox, engine: ModuleType, env: Mapping[str, str]) -> Check:
    rc, out, err = _run(engine, ["docker", "image", "inspect", sandbox.image], env)
    if rc:
        reason = _first_line(err) or f"exit {rc}"
        return Check(
            "sandbox", False, f"image {sandbox.image} is not available ({reason}): docker pull it or build it first"
        )
    try:
        info = json.loads(out)[0]
        digests = {info["Id"], *(ref.rpartition("@")[2] for ref in info.get("RepoDigests", []))}
    except (ValueError, LookupError, TypeError, AttributeError):
        return Check("sandbox", False, f"cannot read the reply of docker image inspect {sandbox.image}")
    if sandbox.image_digest() not in digests:
        return Check(
            "sandbox", False, f"image {sandbox.image} exists but its digest is not the pinned one ({sorted(digests)})"
        )
    return Check("sandbox", True, f"{sandbox.describe()}, image present")


def _network_check(sandbox: DockerSandbox, engine: ModuleType, env: Mapping[str, str]) -> Check:
    if sandbox.network == "none":
        return Check("network", True, "none: no network inside the container")
    create = f"docker network create --internal {sandbox.network}"
    rc, out, err = _run(engine, ["docker", "network", "inspect", sandbox.network], env)
    if rc:
        return Check("network", False, f"docker has no network {sandbox.network!r} ({_first_line(err)}): {create}")
    try:
        internal = json.loads(out)[0]["Internal"]
    except (ValueError, LookupError, TypeError):
        return Check("network", False, f"cannot read the reply of docker network inspect {sandbox.network}")
    if internal is True:
        return Check("network", True, f"{sandbox.network} is internal: containers reach only what is attached to it")
    return Check(
        "network",
        True,
        f"{sandbox.network} is not internal: agents can reach the internet. Create one with `{create}` and attach "
        "only the gateway to it",
        warning=True,
    )


# ---------------------------------------------------------------- harness CLIs


def _harness_checks(
    cfg: Mapping[str, Any], sandbox: Sandbox, image_ready: bool, engine: ModuleType, env: Mapping[str, str]
) -> list[Check]:
    """One check per harness of the matrix: its CLI is where the sandbox runs it."""
    names = list(dict.fromkeys(pair["harness"] for pair in cfg["matrix"]))
    checks = []
    for name in names:
        command = cfg["harnesses"][name].get("command")
        cli = command[0] if isinstance(command, list) and command else None
        if cli is None:
            checks.append(Check(f"harness {name}", True, "no command to look up"))
        elif isinstance(sandbox, DockerSandbox):
            checks.append(_probe_in_container(name, cli, sandbox, image_ready, engine, env))
        else:
            checks.append(_look_up_on_path(name, cli, sandbox))
    return checks


def _look_up_on_path(name: str, cli: str, sandbox: Sandbox) -> Check:
    found = shutil.which(cli)
    if found:
        return Check(f"harness {name}", True, found)
    if sandbox.isolated:  # a prefix or a plugin may run it somewhere the host cannot see
        return Check(
            f"harness {name}",
            True,
            f"{cli} is not on the host PATH (fine if the sandbox runs it elsewhere)",
            warning=True,
        )
    return Check(f"harness {name}", False, f"{cli} is not on PATH")


def _probe_in_container(
    name: str, cli: str, sandbox: DockerSandbox, image_ready: bool, engine: ModuleType, env: Mapping[str, str]
) -> Check:
    """`<cli> --version` in the sandbox, with the limits and the no-network default of an agent."""
    check = f"harness {name}"
    if not image_ready:
        return Check(check, False, "not checked: the image is not available")
    with tempfile.TemporaryDirectory(prefix="harness-bench-doctor-") as tmp:
        workdir, env_file = Path(tmp), Path(tmp) / "env"
        engine.write_env_file({}, env_file)
        session = sandbox.create(workdir, f"doctor-{secrets.token_hex(4)}")  # a name of its own per invocation
        try:
            rc, out, err = _run(engine, session.argv([cli, "--version"], env_file), env, workdir)
        finally:
            session.destroy()
    if rc:
        return Check(check, False, f"{cli} --version failed in the image: {_first_line(err) or f'exit {rc}'}")
    return Check(check, True, _first_line(out) or f"{cli} answers")


# ---------------------------------------------------------------- URLs and the gateway


def _url_checks(cfg: Mapping[str, Any], engine: ModuleType) -> list[Check]:
    """The LiteLLM and judge URLs follow the https rule (http only for localhost)."""
    urls = {
        f"{section}.base_url": str(cfg[section]["base_url"])
        for section in ("litellm", "judge")
        if (cfg.get(section) or {}).get("base_url")
    }
    if not urls:
        return [Check("urls", True, "no litellm or judge section")]
    problems, unset = [], []
    for setting, raw in urls.items():
        missing = sorted(n for n in engine.env_var_names(raw) if not os.environ.get(n))
        if missing:
            unset += missing
            continue
        try:
            engine.check_litellm_url(engine.expand_env(raw), setting)
        except SystemExit as e:
            problems.append(str(e.code))
    if problems:
        return [Check("urls", False, "; ".join(problems))]
    if unset:
        return [Check("urls", True, f"skipped: {', '.join(unset)} not set", warning=True)]
    return [Check("urls", True, ", ".join(urls))]


def _network_checks(litellm: Mapping[str, Any], engine: ModuleType) -> list[Check]:
    """One `GET` of the health endpoint and one of a single page of spend logs, both to the configured gateway."""
    url, key = engine.expand_env(litellm["base_url"]), engine.expand_env(litellm.get("api_key", ""))
    now = datetime.now(UTC)
    fmt = "%Y-%m-%d %H:%M:%S"
    spend = {"start_date": (now - timedelta(hours=1)).strftime(fmt), "end_date": now.strftime(fmt)}
    requests: tuple[tuple[str, str, dict[str, Any]], ...] = (
        ("litellm health", "/health/liveliness", {}),
        ("litellm spend key", "/spend/logs/v2", {**spend, "page": 1, "page_size": 1}),
    )
    checks = []
    for name, path, params in requests:
        try:
            engine.litellm_get(url, key, path, params)
        except (OSError, ValueError) as e:  # URLError, HTTPError and timeouts are OSErrors; a bad reply a ValueError
            checks.append(Check(name, False, f"GET {path}: {type(e).__name__}: {e}"))
        else:
            checks.append(Check(name, True, f"GET {path} answered"))
    return checks
