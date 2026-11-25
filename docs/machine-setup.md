# Machine setup

How each person runs the benchmark on their own machine. The run itself is described in [Running a model × harness benchmark](running-a-benchmark.md).

## Common steps

1. Clone the repository and run `cp config.litellm.example.json config.litellm.json`.
2. Use **your own virtual key**, with a budget. This keeps cost separate per person.
3. To merge results from several people, use **the same config, the same tasks and the same CLI versions**, and record the machine in the result (see [roadmap](roadmap.md), M7).
4. Build the image **on each machine**: it uses the local user's `UID`/`GID`.

## Linux

- Docker Engine, with your user in the `docker` group, or rootless Docker.
- Nothing beyond the standard flow.

## macOS (Intel and Apple Silicon)

- **Docker Desktop or OrbStack:** the temporary directory (`/var/folders/...`) is usually shared by default, so the bind mount works.
- **Colima:** by default it mounts only `$HOME`. Set an absolute `work_dir` inside your home and outside the repository, for example:

  ```json
  "work_dir": "~/hb-work"
  ```

  Otherwise the container sees an empty directory.
- Apple Silicon: the image builds natively for arm64. Do not force `--platform linux/amd64`, which is slow under emulation.
- Give the Docker VM enough memory. Each container in the example `prefix` can use up to `--memory 4g`.

## Windows

- Run **inside WSL2** (Ubuntu), with Docker Desktop's WSL integration or Docker Engine installed in WSL.
- Clone the repository and set `work_dir` on the Linux filesystem (`~/...`), **not** under `/mnt/c/...`, which is slow and breaks permissions.
- Native PowerShell is not supported: `prefix` and the paths assume POSIX.

## Limiting what the agent can reach

The `docker` sandbox (`"sandbox": {"type": "docker", "image": "harness-bench-agents@sha256:<digest>", ...}`) gives a container no network (`--network none`) unless you name one. The agents must reach LiteLLM, so use an **internal** Docker network and attach only the gateway to it:

```bash
docker network create --internal hb-egress
docker network connect hb-egress <litellm-container>      # the gateway answers on that network
# in the config: "sandbox": {"type": "docker", "image": "...@sha256:...", "network": "hb-egress",
#                            "limits": {"cpus": 2, "memory": "4g", "pids": 512}}
```

An internal network has no route out, so a prompt-injected agent can talk to the gateway and nothing else. The agents reach the gateway by its name on that network, so the `LITELLM_BASE_URL` they get (through `env_passthrough` or the harness `env`) must use that name; `litellm.base_url`, which the engine uses on the host for `reconcile`, must stay a URL the host can reach (https, or http for localhost). `python3 -m hbench doctor --config config.json` checks that the image has the pinned digest, that every CLI answers inside it and that the network is `Internal: true` (or `none`); it warns for any other network. The digest of a local image is `docker image inspect harness-bench-agents --format '{{.Id}}'`.

## Corporate networks

- If LiteLLM is reachable only through a VPN, check that the container (default Docker bridge network, no `--network` flag in the example `prefix`) can reach it:

  ```bash
  docker run --rm --entrypoint curl harness-bench-agents -sI "$LITELLM_BASE_URL"
  ```
- Behind an HTTP proxy, add `HTTPS_PROXY`, `HTTP_PROXY` and `NO_PROXY` to `env_passthrough`.
- With a corporate CA, mount the certificate in `prefix` (`-v /path/ca.crt:/etc/ssl/certs/corp.crt:ro`) and pass `NODE_EXTRA_CA_CERTS` and `SSL_CERT_FILE` pointing to it. The CLIs are Node.js programs and do not use the host's CA store.
