#!/bin/sh
# Render harness configs from templates, then run the given command.
# Needs LITELLM_BASE_URL (proxy root, no /v1), e.g. https://litellm.example.com
set -eu

render() {  # template -> destination, replacing __LITELLM_BASE_URL__
    mkdir -p "$(dirname "$2")"
    sed "s#__LITELLM_BASE_URL__#${LITELLM_BASE_URL%/}#g" "$1" > "$2"
}

if [ -n "${LITELLM_BASE_URL:-}" ]; then
    t=/opt/harness-bench/templates
    render "$t/pi-models.json"     "$HOME/.pi/agent/models.json"
    render "$t/opencode.json"      "$HOME/.config/opencode/opencode.json"
    render "$t/codex-config.toml"  "${CODEX_HOME:-$HOME/.codex}/config.toml"
fi

exec "$@"
