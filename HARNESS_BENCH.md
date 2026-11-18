# harness-bench: custo por tarefa resolvida (harness × modelo)

Script para comparar pares **harness × modelo** em tarefas reais do seu código. Ele mede taxa de acerto, tokens e **custo por tarefa resolvida**.

Ambiente-alvo:

| Harness | Cobrança | Modelos | Fonte do custo no relatório |
|---|---|---|---|
| Claude Code | API via LiteLLM | Anthropic | LiteLLM (real) |
| Pi | API via LiteLLM | OpenAI, Anthropic, Gemini | LiteLLM (real) |
| OpenCode | API via LiteLLM | OpenAI, Anthropic, Gemini | LiteLLM (real) |
| Codex CLI | API via LiteLLM | OpenAI | LiteLLM (real) |
| Cursor (`agent` CLI) | Enterprise (assento) | OpenAI, Anthropic, Gemini, Grok | sem tokens no JSON: só taxa de acerto e tempo |

Gemini CLI não entra no benchmark.

Requisitos: Python 3.12+ (só biblioteca padrão), `git` e os CLIs instalados e autenticados.

## Ideia central

1. Cada execução recebe uma tag única (`bench-<data>-<tarefa>__<harness>__<modelo>__rN`).
2. O script exporta essa tag como `BENCH_TAG` e o harness a envia ao LiteLLM no header `x-litellm-tags`.
3. Depois da rodada, `reconcile` busca os spend logs do LiteLLM e soma o custo real de cada tag.

Assim o custo vem da fatura real (o que o LiteLLM cobrou), não da estimativa de cada harness. Isso inclui chamadas auxiliares, como subagents, compactação e geração de título.

## Dois scripts

| Script | Harnesses | Custo | Saída |
|---|---|---|---|
| `harness_bench.py` | todos, incluindo Cursor | `auto` (LiteLLM → tabela de preços → reportado) | `results/` |
| `harness_bench_litellm.py` | só os que passam pelo LiteLLM: Claude Code, Pi, OpenCode e Codex | sempre LiteLLM (custo real) | `results-litellm/` |

O `harness_bench_litellm.py` usa o mesmo motor. Os dois arquivos precisam estar na mesma pasta. Ele faz o seguinte:

- remove do config qualquer harness com `parser: cursor`;
- exige a seção `litellm` no config;
- depois da rodada, espera `--settle` segundos (padrão 60) para o LiteLLM gravar os logs;
- roda `reconcile` e gera o relatório só com custo real.

```bash
cp config.litellm.example.json config.litellm.json
python3 harness_bench_litellm.py run --config config.litellm.json --dry-run
python3 harness_bench_litellm.py run --config config.litellm.json --reps 1 --only pi   # smoke test
python3 harness_bench_litellm.py run --config config.litellm.json                     # rodada completa
python3 harness_bench_litellm.py reconcile --config config.litellm.json               # se os logs atrasarem
```

Se uma execução aparecer com custo `n/a`, a tag não chegou no LiteLLM. Veja a seção "LiteLLM: o que validar".

## Estrutura

```
harness-bench/
├── harness_bench.py
├── harness_bench_litellm.py     # versão só LiteLLM (sem Cursor)
├── README.md / AGENTS.md
├── scripts/build_md.py          # gera este HARNESS_BENCH.md
├── tests/smoke.py               # teste ponta a ponta com agentes e LiteLLM falsos
├── config.json                  # copie de config.example.json
├── config.litellm.json          # copie de config.litellm.example.json
├── pi-models.example.json       # vai para ~/.pi/agent/models.json
├── opencode.example.json        # vai para ~/.config/opencode/opencode.json (ou opencode.json do projeto)
├── codex-config.example.toml    # trecho para ~/.codex/config.toml
└── tasks/
    └── fizzbuzz/
        ├── repo/fizzbuzz.py
        └── hidden_tests/test_hidden.py   # copiado só DEPOIS do agente rodar
```

## Passo a passo

```bash
# Variáveis
export LITELLM_BASE_URL=https://seu-litellm.exemplo.com
export LITELLM_API_KEY=sk-...        # virtual key usada pelos harnesses
export LITELLM_SPEND_KEY=sk-...      # key com permissão de leitura de spend (pode ser a mesma)

cp config.example.json config.json   # troque os placeholders de modelo, preço e tarefas

# 0. Teste offline do próprio script (sem rede, sem CLIs reais)
python3 tests/smoke.py

# 1. Confira os comandos gerados (não gasta nada)
python3 harness_bench.py run --config config.json --dry-run

# 2. Smoke test: 1 repetição por harness. Confira results/logs/ antes de seguir
python3 harness_bench.py run --config config.json --reps 1 --only pi
python3 harness_bench.py run --config config.json --reps 1 --only claude-code opencode codex cursor

# 3. Rodada completa (retoma de onde parou)
python3 harness_bench.py run --config config.json

# 4. Custo real do LiteLLM + relatório
python3 harness_bench.py reconcile --config config.json

# Relatório de novo, forçando uma fonte de custo
python3 harness_bench.py report --config config.json --cost-source litellm
```

Saídas em `results/`:

- `results.jsonl`: uma linha por execução, com dados brutos;
- `summary.md` e `summary.csv`: tabela ordenada por custo por tarefa resolvida, mais a matriz de acerto por tarefa;
- `logs/`: saída JSON de cada agente, stderr, setup e testes.

## Fontes de custo

| Fonte | De onde vem | Quando usar |
|---|---|---|
| `litellm` | spend logs do LiteLLM, por tag | Principal. Custo real |
| `computed` | tokens do harness × tabela `prices` | Quando não há LiteLLM, ou para estimar o Cursor se ele expuser tokens |
| `reported` | custo que o próprio harness informa | Último recurso |

`auto` (padrão) usa `litellm`, depois `computed`, depois `reported`. A coluna "Fonte custo" mostra qual fonte foi usada.

## Configuração por harness

### Claude Code

O `config.example.json` já define as variáveis por execução:

- `ANTHROPIC_BASE_URL` = LiteLLM;
- `ANTHROPIC_AUTH_TOKEN` = virtual key;
- `ANTHROPIC_CUSTOM_HEADERS` = `x-litellm-tags: {run_tag}`.

O `{run_tag}` é trocado pela tag de cada execução. Os guias da LiteLLM divergem sobre a autenticação (`ANTHROPIC_AUTH_TOKEN` ou header `x-litellm-api-key`). Use o formato que já funciona hoje na sua máquina.

### Pi

Copie `pi-models.example.json` para `~/.pi/agent/models.json` ou mescle com o arquivo existente. Pi aceita `headers` por provider, e `$BENCH_TAG` é lido do ambiente.

- Modelos Claude: use o provider `litellm-anthropic` (`api: anthropic-messages`). Pela API OpenAI-compatível, o prompt caching da Anthropic pode não funcionar, o que encarece e distorce a comparação.
- Modelos GPT e Gemini: use o provider `litellm` (`openai-completions`).
- Formato do modelo no config: `provider/modelo`. O comando usa `--provider {provider} --model {model_name}`.

```json
{
  "providers": {
    "litellm": {
      "baseUrl": "https://SEU-LITELLM/v1",
      "api": "openai-completions",
      "apiKey": "$LITELLM_API_KEY",
      "headers": { "x-litellm-tags": "$BENCH_TAG" },
      "models": [
        { "id": "GPT_MODEL" },
        { "id": "GEMINI_MODEL" }
      ]
    },
    "litellm-anthropic": {
      "baseUrl": "https://SEU-LITELLM",
      "api": "anthropic-messages",
      "apiKey": "$LITELLM_API_KEY",
      "headers": { "x-litellm-tags": "$BENCH_TAG" },
      "models": [
        { "id": "CLAUDE_MODEL" }
      ]
    }
  }
}
```

### OpenCode

Use `opencode.example.json` como config global ou do projeto. Os exemplos oficiais mostram `{env:VAR}` em `apiKey`. Não achei exemplo oficial com `{env:...}` dentro de `headers`. **Confirme no LiteLLM se a tag chega.** Modelo no config: `litellm/MODELO`.

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "litellm": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "LiteLLM",
      "options": {
        "baseURL": "https://SEU-LITELLM/v1",
        "apiKey": "{env:LITELLM_API_KEY}",
        "headers": { "x-litellm-tags": "{env:BENCH_TAG}" }
      },
      "models": {
        "CLAUDE_MODEL": {},
        "GPT_MODEL": {},
        "GEMINI_MODEL": {}
      }
    }
  }
}
```

### Codex CLI

Adicione o trecho ao `~/.codex/config.toml`. Prefira um `CODEX_HOME` dedicado ao benchmark. `env_http_headers` mapeia header → nome da env var. **Confirme o nome dessas chaves na documentação da sua versão do Codex.** O comando usa `--full-auto` (sandbox com escrita no workspace). Se a tarefa precisar de rede (ex.: `npm ci`), deixe isso no `setup`, que roda fora do agente.

```toml
# Trecho para ~/.codex/config.toml (ou CODEX_HOME dedicado ao benchmark)
[model_providers.litellm]
name = "LiteLLM"
base_url = "https://SEU-LITELLM/v1"
env_key = "LITELLM_API_KEY"                         # nome da env var com a chave
wire_api = "responses"                              # use "chat" se seu LiteLLM não expõe /responses
env_http_headers = { "x-litellm-tags" = "BENCH_TAG" }  # header <- valor da env var BENCH_TAG
```

Parser: o `usage` do evento `turn.completed` é o total acumulado da thread, então o script pega o último. `cached_input_tokens` faz parte de `input_tokens`, e reasoning faz parte de `output_tokens`.

### Cursor (`agent` CLI)

- A documentação oficial do `--output-format json` **não tem campos de tokens nem de custo**. Algumas versões incluem `usage`, e o parser lê esse campo quando ele aparece.
- Sem tokens, o Cursor entra só com taxa de acerto e tempo. Custo por tarefa não se aplica: o plano Enterprise cobra por assento e pool de uso, não por token da sua API.
- Para estimar custo, compare o uso no dashboard do Cursor antes e depois da rodada.
- **Grok só existe no Cursor**, então não dá para isolar o efeito do harness com Grok.
- Binário: a documentação usa `agent`. Em instalações antigas pode ser `cursor-agent`. Ajuste o `command`.
- Flags usadas: `-p`, `--output-format json`, `--force` (aprova comandos) e `--trust` (confia no workspace no modo headless).

## LiteLLM: o que validar

- `reconcile` tenta primeiro `GET /spend/logs/v2` (paginado, datas `YYYY-MM-DD HH:MM:SS`). Se falhar, usa o legado `GET /spend/logs?summarize=false`. A associação entre logs e execuções é feita pelo campo `request_tags`, no próprio script.
- Uma key que não é admin precisa da permissão `get_spend_routes` para ler spend. Peça ao time que administra o LiteLLM.
- Os logs podem demorar alguns segundos para gravar. Rode `reconcile` alguns minutos depois de terminar. Pode rodar quantas vezes quiser.
- Se aparecer "Unmatched runs", a tag não chegou. Confira em `results/logs/` e no painel do LiteLLM se `x-litellm-tags` está nas requests daquele harness.
- Spend por tag aparece como feature enterprise em parte da documentação da LiteLLM. O script não depende do endpoint de tags: ele lê os logs individuais e filtra localmente. Mesmo assim, o campo `request_tags` precisa estar populado.

## Comparação justa

- **Comece pelo mesmo modelo em harnesses diferentes**: Claude em Claude Code × Pi × OpenCode × Cursor; GPT em Codex × Pi × OpenCode × Cursor. Isso isola o efeito do harness. Depois compare modelos.
- **Configuração limpa**: o Claude Code carrega `~/.claude/CLAUDE.md`, MCP servers e plugins. Isso aumenta tokens. Rode o benchmark com um usuário ou HOME dedicado, e faça o mesmo para os outros harnesses (`~/.pi/agent/`, `~/.config/opencode/`, `CODEX_HOME`).
- **Mesmo "esforço"**: thinking e reasoning effort mudam muito o custo. Fixe esses parâmetros por modelo e registre no nome do par (ex.: `GPT_MODEL` com `-c model_reasoning_effort=medium` no Codex; `modelo:high` no Pi).
- **Segurança**: os agentes rodam sem pedir aprovação. Use VM ou container sem acesso a dados sensíveis.
- **Variância**: use `reps` ≥ 3 e 5–10 tarefas antes de concluir algo.

## Como escolher tarefas

- Use bugs reais já corrigidos, com `ref` = commit anterior ao fix.
- Escreva o prompt como uma issue, sem dica da solução.
- Use como testes ocultos os testes do commit do fix.
- `setup` (ex.: `npm ci`) roda antes do agente e não entra no custo.

## Placeholders do `command`

`{prompt}`, `{model}`, `{provider}` (parte antes da `/`), `{model_name}` (parte depois da `/`).
Em `env`: `${VAR}` é expandido do ambiente e `{run_tag}` vira a tag da execução. `BENCH_TAG` é sempre exportada.

---

## `harness_bench.py`

```python
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
```

## `harness_bench_litellm.py`

```python
#!/usr/bin/env python3
"""harness-bench (LiteLLM only): compare harnesses that bill through LiteLLM.

Same engine as harness_bench.py, with these differences:
- Cursor (and any harness with parser "cursor") is removed from the matrix.
- Cost always comes from LiteLLM spend logs (real spend, matched by per-run tag).
- After the run it waits for LiteLLM to write logs, then reconciles and reports.
- Results go to results-litellm/ by default, separate from the full benchmark.

Usage:
  python3 harness_bench_litellm.py run       --config config.litellm.json [--reps 3] [--only pi codex] [--settle 60]
  python3 harness_bench_litellm.py reconcile --config config.litellm.json
  python3 harness_bench_litellm.py report    --config config.litellm.json
"""
import argparse
import json
import sys
import time
from pathlib import Path

if sys.version_info < (3, 12):
    sys.exit("Python 3.12+ required")

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_bench as hb  # noqa: E402

EXCLUDED_PARSERS = {"cursor"}


def load_config(path):
    cfg_path = Path(path).resolve()
    cfg = json.loads(cfg_path.read_text())
    if not cfg.get("litellm"):
        sys.exit("config needs a 'litellm' section (base_url, api_key)")
    excluded = {name for name, h in cfg["harnesses"].items() if h.get("parser") in EXCLUDED_PARSERS}
    cfg["harnesses"] = {k: v for k, v in cfg["harnesses"].items() if k not in excluded}
    dropped = [m for m in cfg["matrix"] if m["harness"] in excluded]
    cfg["matrix"] = [m for m in cfg["matrix"] if m["harness"] not in excluded]
    if dropped:
        print(f"ignored {len(dropped)} matrix entries for non-LiteLLM harnesses: {sorted(excluded)}")
    unknown = {m["harness"] for m in cfg["matrix"]} - set(cfg["harnesses"])
    if unknown:
        sys.exit(f"matrix uses undefined harnesses: {sorted(unknown)}")
    cfg.setdefault("output_dir", "results-litellm")
    cfg["cost_source"] = "litellm"
    return cfg, cfg_path.parent


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--config", required=True)
    r.add_argument("--reps", type=int)
    r.add_argument("--only", nargs="*", help="only these harness names")
    r.add_argument("--keep", action="store_true", help="keep work directories")
    r.add_argument("--dry-run", action="store_true", help="print commands only")
    r.add_argument("--settle", type=int, default=60,
                   help="seconds to wait for LiteLLM to write spend logs before reconcile")
    for name in ("reconcile", "report"):
        s = sub.add_parser(name)
        s.add_argument("--config", required=True)
    args = ap.parse_args()
    args.cost_source = "litellm"
    args.no_report = True  # report only after reconcile, when LiteLLM cost exists

    cfg, base = load_config(args.config)
    if args.cmd == "run":
        hb.cmd_run(args, cfg, base)
        if args.dry_run:
            return
        print(f"\nwaiting {args.settle}s for LiteLLM spend logs...", flush=True)
        time.sleep(args.settle)
        try:
            hb.cmd_reconcile(args, cfg, base)
        except Exception as e:  # noqa: BLE001
            print(f"reconcile failed: {e}\nRun later: python3 {Path(__file__).name} reconcile --config {args.config}")
    elif args.cmd == "reconcile":
        hb.cmd_reconcile(args, cfg, base)
    else:
        hb.cmd_report(args, cfg, base)


if __name__ == "__main__":
    main()
```

## `config.litellm.example.json`

```json
{
  "reps": 3,
  "timeout": 1800,
  "cost_source": "litellm",
  "output_dir": "results-litellm",
  "work_dir": "work",
  "litellm": {
    "base_url": "${LITELLM_BASE_URL}",
    "api_key": "${LITELLM_SPEND_KEY}"
  },
  "harnesses": {
    "claude-code": {
      "parser": "claude",
      "command": [
        "claude",
        "-p",
        "{prompt}",
        "--model",
        "{model}",
        "--output-format",
        "json",
        "--dangerously-skip-permissions"
      ],
      "env": {
        "ANTHROPIC_BASE_URL": "${LITELLM_BASE_URL}",
        "ANTHROPIC_AUTH_TOKEN": "${LITELLM_API_KEY}",
        "ANTHROPIC_CUSTOM_HEADERS": "x-litellm-tags: {run_tag}"
      }
    },
    "pi": {
      "parser": "pi",
      "command": [
        "pi",
        "--mode",
        "json",
        "--provider",
        "{provider}",
        "--model",
        "{model_name}",
        "{prompt}"
      ]
    },
    "opencode": {
      "parser": "opencode",
      "command": [
        "opencode",
        "run",
        "--format",
        "json",
        "--model",
        "{model}",
        "{prompt}"
      ]
    },
    "codex": {
      "parser": "codex",
      "command": [
        "codex",
        "exec",
        "--json",
        "--full-auto",
        "--skip-git-repo-check",
        "-c",
        "model_provider=litellm",
        "-m",
        "{model}",
        "{prompt}"
      ]
    }
  },
  "matrix": [
    {
      "harness": "claude-code",
      "model": "CLAUDE_MODEL",
      "price": "claude"
    },
    {
      "harness": "pi",
      "model": "litellm-anthropic/CLAUDE_MODEL",
      "price": "claude"
    },
    {
      "harness": "opencode",
      "model": "litellm/CLAUDE_MODEL",
      "price": "claude"
    },
    {
      "harness": "codex",
      "model": "GPT_MODEL",
      "price": "gpt"
    },
    {
      "harness": "pi",
      "model": "litellm/GPT_MODEL",
      "price": "gpt"
    },
    {
      "harness": "opencode",
      "model": "litellm/GPT_MODEL",
      "price": "gpt"
    },
    {
      "harness": "pi",
      "model": "litellm/GEMINI_MODEL",
      "price": "gemini"
    },
    {
      "harness": "opencode",
      "model": "litellm/GEMINI_MODEL",
      "price": "gemini"
    }
  ],
  "tasks": [
    {
      "id": "fizzbuzz",
      "repo": "tasks/fizzbuzz/repo",
      "prompt": "The function fizzbuzz in fizzbuzz.py has bugs. Fix it so fizzbuzz(n) returns 'Fizz' for multiples of 3, 'Buzz' for multiples of 5, 'FizzBuzz' for multiples of both, and str(n) otherwise. Do not ask questions.",
      "hidden_tests": "tasks/fizzbuzz/hidden_tests",
      "test": "python3 -m unittest -q test_hidden.py"
    },
    {
      "id": "exemplo-repo-real",
      "repo": "git@github.com:SUA-ORG/SEU-REPO.git",
      "ref": "COMMIT_ANTES_DO_FIX",
      "setup": "npm ci",
      "prompt": "Descreva aqui o bug ou a feature, como numa issue real.",
      "hidden_tests": "tasks/exemplo-repo-real/hidden_tests",
      "test": "npx vitest run tests/hidden",
      "timeout": 2400
    }
  ]
}
```

## `config.example.json`

```json
{
  "reps": 3,
  "timeout": 1800,
  "cost_source": "auto",
  "output_dir": "results",
  "work_dir": "work",
  "litellm": {
    "base_url": "${LITELLM_BASE_URL}",
    "api_key": "${LITELLM_SPEND_KEY}"
  },
  "harnesses": {
    "claude-code": {
      "parser": "claude",
      "command": [
        "claude",
        "-p",
        "{prompt}",
        "--model",
        "{model}",
        "--output-format",
        "json",
        "--dangerously-skip-permissions"
      ],
      "env": {
        "ANTHROPIC_BASE_URL": "${LITELLM_BASE_URL}",
        "ANTHROPIC_AUTH_TOKEN": "${LITELLM_API_KEY}",
        "ANTHROPIC_CUSTOM_HEADERS": "x-litellm-tags: {run_tag}"
      }
    },
    "pi": {
      "parser": "pi",
      "command": [
        "pi",
        "--mode",
        "json",
        "--provider",
        "{provider}",
        "--model",
        "{model_name}",
        "{prompt}"
      ]
    },
    "cursor": {
      "parser": "cursor",
      "command": [
        "agent",
        "-p",
        "--output-format",
        "json",
        "--force",
        "--trust",
        "--model",
        "{model}",
        "{prompt}"
      ]
    },
    "opencode": {
      "parser": "opencode",
      "command": [
        "opencode",
        "run",
        "--format",
        "json",
        "--model",
        "{model}",
        "{prompt}"
      ]
    },
    "codex": {
      "parser": "codex",
      "command": [
        "codex",
        "exec",
        "--json",
        "--full-auto",
        "--skip-git-repo-check",
        "-c",
        "model_provider=litellm",
        "-m",
        "{model}",
        "{prompt}"
      ]
    }
  },
  "matrix": [
    {
      "harness": "claude-code",
      "model": "CLAUDE_MODEL",
      "price": "claude"
    },
    {
      "harness": "pi",
      "model": "litellm-anthropic/CLAUDE_MODEL",
      "price": "claude"
    },
    {
      "harness": "opencode",
      "model": "litellm/CLAUDE_MODEL",
      "price": "claude"
    },
    {
      "harness": "cursor",
      "model": "CURSOR_CLAUDE_MODEL",
      "price": "claude"
    },
    {
      "harness": "codex",
      "model": "GPT_MODEL",
      "price": "gpt"
    },
    {
      "harness": "pi",
      "model": "litellm/GPT_MODEL",
      "price": "gpt"
    },
    {
      "harness": "opencode",
      "model": "litellm/GPT_MODEL",
      "price": "gpt"
    },
    {
      "harness": "cursor",
      "model": "CURSOR_GPT_MODEL",
      "price": "gpt"
    },
    {
      "harness": "pi",
      "model": "litellm/GEMINI_MODEL",
      "price": "gemini"
    },
    {
      "harness": "opencode",
      "model": "litellm/GEMINI_MODEL",
      "price": "gemini"
    },
    {
      "harness": "cursor",
      "model": "CURSOR_GEMINI_MODEL",
      "price": "gemini"
    },
    {
      "harness": "cursor",
      "model": "CURSOR_GROK_MODEL",
      "price": "grok"
    }
  ],
  "prices": {
    "_comment": "USD por 1M tokens. Usado só quando não há custo do LiteLLM (ex.: Cursor). Preencha com a tabela oficial.",
    "claude": {
      "input": 0,
      "output": 0,
      "cache_read": 0,
      "cache_write": 0
    },
    "gpt": {
      "input": 0,
      "output": 0,
      "cache_read": 0,
      "cache_write": 0
    },
    "gemini": {
      "input": 0,
      "output": 0,
      "cache_read": 0,
      "cache_write": 0
    },
    "grok": {
      "input": 0,
      "output": 0,
      "cache_read": 0,
      "cache_write": 0
    }
  },
  "tasks": [
    {
      "id": "fizzbuzz",
      "repo": "tasks/fizzbuzz/repo",
      "prompt": "The function fizzbuzz in fizzbuzz.py has bugs. Fix it so fizzbuzz(n) returns 'Fizz' for multiples of 3, 'Buzz' for multiples of 5, 'FizzBuzz' for multiples of both, and str(n) otherwise. Do not ask questions.",
      "hidden_tests": "tasks/fizzbuzz/hidden_tests",
      "test": "python3 -m unittest -q test_hidden.py"
    },
    {
      "id": "exemplo-repo-real",
      "repo": "git@github.com:SUA-ORG/SEU-REPO.git",
      "ref": "COMMIT_ANTES_DO_FIX",
      "setup": "npm ci",
      "prompt": "Descreva aqui o bug ou a feature, como numa issue real.",
      "hidden_tests": "tasks/exemplo-repo-real/hidden_tests",
      "test": "npx vitest run tests/hidden",
      "timeout": 2400
    }
  ]
}
```

## Tarefa de exemplo

`tasks/fizzbuzz/repo/fizzbuzz.py` (bugs propositais):

```python
def fizzbuzz(n):
    if n % 3 == 0:
        return "Fizz"
    if n % 5 == 0:
        return "Buzz"
    if n % 15 == 0:
        return "FizzBuzz"
    return n
```

`tasks/fizzbuzz/hidden_tests/test_hidden.py`:

```python
import unittest
from fizzbuzz import fizzbuzz


class TestFizzBuzz(unittest.TestCase):
    def test_values(self):
        self.assertEqual(fizzbuzz(1), "1")
        self.assertEqual(fizzbuzz(3), "Fizz")
        self.assertEqual(fizzbuzz(5), "Buzz")
        self.assertEqual(fizzbuzz(15), "FizzBuzz")
        self.assertEqual(fizzbuzz(30), "FizzBuzz")


if __name__ == "__main__":
    unittest.main()
```

## Testes offline

Rode `python3 tests/smoke.py`. Ele usa agentes e LiteLLM falsos para validar os parsers, o `reconcile` e os dois scripts, sem rede e sem CLIs reais.

`tests/smoke.py`:

```python
#!/usr/bin/env python3
"""End-to-end smoke test with fake agents and a fake LiteLLM. No network, no real CLIs.

Run: python3 tests/smoke.py   (exit 0 = pass)
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
import fake_litellm  # noqa: E402


def check(cond, msg):
    if not cond:
        sys.exit(f"FAIL: {msg}")
    print(f"ok   {msg}")


def main():
    tmp = Path(tempfile.mkdtemp(prefix="hb-smoke-"))
    try:
        for f in ("harness_bench.py", "harness_bench_litellm.py"):
            shutil.copy(ROOT / f, tmp)
        shutil.copytree(ROOT / "tasks" / "fizzbuzz", tmp / "tasks" / "fizzbuzz")
        tags_file = tmp / "tags.txt"
        os.environ["FAKE_TAGS_FILE"] = str(tags_file)
        os.environ["FAKE_KEY"] = fake_litellm.KEY

        srv, port = fake_litellm.serve()
        threading.Thread(target=srv.serve_forever, daemon=True).start()

        agent = str(ROOT / "tests" / "fake_agent.py")
        harness = lambda kind, model_field: {  # noqa: E731
            "parser": kind, "command": [sys.executable, agent, kind, model_field],
            "env": {"ANTHROPIC_CUSTOM_HEADERS": "x-litellm-tags: {run_tag}"}}
        cfg = {
            "reps": 1,
            "litellm": {"base_url": f"http://127.0.0.1:{port}", "api_key": "${FAKE_KEY}"},
            "harnesses": {k: harness(k, "{model_name}") for k in
                          ("claude", "opencode", "pi", "codex", "cursor")},
            "matrix": [
                {"harness": "claude", "model": "fix"},
                {"harness": "opencode", "model": "p/nofix"},
                {"harness": "pi", "model": "p/fix"},
                {"harness": "codex", "model": "fix", "price": "x"},
                {"harness": "cursor", "model": "fix", "price": "x"},
            ],
            "prices": {"x": {"input": 3, "output": 15}},
            "tasks": [{"id": "fizzbuzz", "repo": "tasks/fizzbuzz/repo", "prompt": "fix it",
                       "hidden_tests": "tasks/fizzbuzz/hidden_tests",
                       "test": f"{sys.executable} -m unittest -q test_hidden.py"}],
        }
        (tmp / "config.json").write_text(json.dumps(cfg))

        def run(*args):
            p = subprocess.run([sys.executable, *args, "--config", "config.json"], cwd=tmp,
                               capture_output=True, text=True)
            if p.returncode != 0:
                sys.exit(f"FAIL: {' '.join(args)}\n{p.stdout}\n{p.stderr}")
            return p.stdout

        # --- full script
        run("harness_bench.py", "run")
        rows = {r["harness"]: r for r in map(json.loads, (tmp / "results/results.jsonl").read_text().split("\n")[:-1])}
        check(len(rows) == 5, "full script: 5 runs recorded")
        check(rows["claude"]["passed"] and not rows["opencode"]["passed"], "hidden tests decide pass/fail")
        check(rows["claude"]["input"] == 310 and rows["claude"]["cache_read"] == 40000, "claude: modelUsage summed")
        check(rows["opencode"]["turns"] == 3 and rows["opencode"]["reasoning"] == 150, "opencode: step_finish summed")
        check(rows["pi"]["turns"] == 3 and rows["pi"]["input"] == 2400, "pi: only message_end counted")
        check(rows["codex"]["input"] == 5000 and rows["codex"]["cache_read"] == 15000, "codex: cached is subset of input")
        check(abs(rows["codex"]["cost_computed"] - 0.0735) < 1e-9, "codex: computed cost from prices")
        check(not rows["cursor"]["parse_ok"] and rows["cursor"]["cost_computed"] is None, "cursor: no usage, no fake $0")

        out = run("harness_bench.py", "reconcile")
        check("4/5 runs matched" in out, "reconcile: tagged runs matched, cursor unmatched")
        rows = {r["harness"]: r for r in map(json.loads, (tmp / "results/results.jsonl").read_text().split("\n")[:-1])}
        check(abs(rows["pi"]["cost_litellm"] - 0.02) < 1e-9, "reconcile: unrelated rows ignored")
        check((tmp / "results/summary.md").exists() and (tmp / "results/summary.csv").exists(), "report files written")

        # --- LiteLLM-only script
        tags_file.unlink()
        out = run("harness_bench_litellm.py", "run", "--settle", "0")
        check("ignored 1 matrix entries" in out, "litellm script: cursor dropped")
        check("4/4 runs matched" in out, "litellm script: all runs matched")
        summary = (tmp / "results-litellm/summary.md").read_text()
        check("cursor" not in summary and summary.count("| litellm |") == 4, "litellm script: cost source litellm only")
        print("\nPASS")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
```

`tests/fake_agent.py`:

```python
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
```

`tests/fake_litellm.py`:

```python
"""Fake LiteLLM /spend/logs/v2 for tests: 2 rows of $0.01 per tag in FAKE_TAGS_FILE,
plus one unrelated row that must be ignored."""
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

KEY = "test-key"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.headers.get("Authorization") != f"Bearer {KEY}":
            self.send_response(401)
            self.end_headers()
            return
        path = os.environ["FAKE_TAGS_FILE"]
        tags = open(path).read().split() if os.path.exists(path) else []
        rows = [{"request_tags": json.dumps([t]), "spend": 0.01,
                 "prompt_tokens": 100, "completion_tokens": 10} for t in tags for _ in range(2)]
        rows.append({"request_tags": ["unrelated"], "spend": 9.0, "prompt_tokens": 1})
        body = json.dumps({"data": rows, "total_pages": 1}).encode()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def serve(port=0):
    """Start server; return (server, port)."""
    srv = HTTPServer(("127.0.0.1", port), Handler)
    return srv, srv.server_address[1]
```

## Referências

- Claude Code + LiteLLM (headers e tags): https://docs.litellm.ai/docs/tutorials/claude_code_customer_tracking
- LiteLLM spend tracking: https://docs.litellm.ai/docs/proxy/cost_tracking
- Pi custom models / headers: https://cdn.jsdelivr.net/npm/@earendil-works/pi-coding-agent@0.81.1/docs/models.md
- Pi JSON mode: https://cdn.jsdelivr.net/npm/@earendil-works/pi-coding-agent@0.81.1/docs/json.md
- OpenCode + LiteLLM: https://docs.litellm.ai/docs/tutorials/opencode_integration
- OpenCode stream JSON: https://littlebearapps.com/help/untether/opencode-stream-json-cheatsheet/
- Codex exec JSONL: https://codex.danielvaughan.com/2026/04/08/codex-exec-jsonl-reference/
- Cursor CLI output format: https://cursor.com/docs/cli/reference/output-format
- Cursor CLI parameters: https://cursor.com/docs/cli/reference/parameters
