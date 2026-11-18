# Sandbox image for harness-bench: coding-agent CLIs + git + Python 3.12 + Node.js 22.
#
# Build (from the repo root; UID/GID must match the host user that owns the workdir):
#   docker build -f sandbox/Dockerfile -t harness-bench-agents \
#     --build-arg UID=$(id -u) --build-arg GID=$(id -g) .
#
# Pin CLI versions for reproducible runs, e.g. --build-arg CLAUDE_CODE_VERSION=2.1.0
FROM node:22-bookworm-slim AS node

FROM python:3.12-slim-bookworm

# Node.js runtime from the official image (the npm CLIs below need it).
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

ARG UID=1000
ARG GID=1000
ARG CLAUDE_CODE_VERSION=latest
ARG PI_VERSION=latest
ARG OPENCODE_VERSION=latest
ARG CODEX_VERSION=latest
ARG INSTALL_CURSOR=false

# git: workdir baseline/diff; tini: PID 1. Python 3.12 comes from the base image.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      git ca-certificates curl bash tini \
 && rm -rf /var/lib/apt/lists/*

RUN npm install -g --no-fund --no-audit \
      "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
      "opencode-ai@${OPENCODE_VERSION}" \
      "@openai/codex@${CODEX_VERSION}" \
 && npm install -g --no-fund --no-audit --ignore-scripts \
      "@earendil-works/pi-coding-agent@${PI_VERSION}" \
 && npm cache clean --force

# Unprivileged user with the host UID/GID so files written in the bind-mounted
# workdir stay owned by the host user.
RUN if getent passwd "${UID}" >/dev/null; then userdel -r "$(getent passwd "${UID}" | cut -d: -f1)"; fi \
 && if ! getent group "${GID}" >/dev/null; then groupadd -g "${GID}" agent; fi \
 && useradd -m -u "${UID}" -g "${GID}" -s /bin/bash agent

# Harness config templates. entrypoint.sh renders them with LITELLM_BASE_URL at start.
COPY pi-models.example.json      /opt/harness-bench/templates/pi-models.json
COPY opencode.example.json       /opt/harness-bench/templates/opencode.json
COPY codex-config.example.toml   /opt/harness-bench/templates/codex-config.toml
COPY sandbox/entrypoint.sh       /usr/local/bin/harness-bench-entrypoint
RUN chmod 0755 /usr/local/bin/harness-bench-entrypoint

USER agent
ENV HOME=/home/agent \
    PATH=/home/agent/.local/bin:/usr/local/bin:/usr/bin:/bin \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Cursor CLI is optional: the LiteLLM-only benchmark does not use it.
RUN if [ "${INSTALL_CURSOR}" = "true" ]; then curl -fsSL https://cursor.com/install | bash; fi

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/harness-bench-entrypoint"]
CMD ["bash"]
