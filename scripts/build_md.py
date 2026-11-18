#!/usr/bin/env python3
"""Generate HARNESS_BENCH.md (single-file bundle: docs + all source). Run after any change."""
from pathlib import Path
b=Path(__file__).resolve().parent.parent
R=lambda f:(b/f).read_text()
py,pyl,cfgl,cfg,pim,ocj,cdx,fz,th=map(R,['harness_bench.py','harness_bench_litellm.py','config.litellm.example.json','config.example.json','pi-models.example.json','opencode.example.json','codex-config.example.toml','tasks/fizzbuzz/repo/fizzbuzz.py','tasks/fizzbuzz/hidden_tests/test_hidden.py'])
smoke,fagent,flite=map(R,['tests/smoke.py','tests/fake_agent.py','tests/fake_litellm.py'])
md=f'''# harness-bench: custo por tarefa resolvida (harness × modelo)

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
python3 harness_bench_litellm.py run --config config.litellm.json --reps 1 --only pi   # smoke test (exige sandbox.prefix ou --allow-unsandboxed)
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
- `logs/`: saída JSON de cada agente, stderr, setup, testes e o `.diff` de tudo que o agente mudou. Segredos conhecidos aparecem como `***REDACTED***`.

O comando `run` só roda com `sandbox.prefix` configurado (veja [Segurança](#segurança)). Dentro de uma VM descartável, adicione `--allow-unsandboxed`.

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
- `ANTHROPIC_CUSTOM_HEADERS` = `x-litellm-tags: {{run_tag}}`.

O `{{run_tag}}` é trocado pela tag de cada execução. Os guias da LiteLLM divergem sobre a autenticação (`ANTHROPIC_AUTH_TOKEN` ou header `x-litellm-api-key`). Use o formato que já funciona hoje na sua máquina.

### Pi

Copie `pi-models.example.json` para `~/.pi/agent/models.json` ou mescle com o arquivo existente. Pi aceita `headers` por provider, e `$BENCH_TAG` é lido do ambiente.

- Modelos Claude: use o provider `litellm-anthropic` (`api: anthropic-messages`). Pela API OpenAI-compatível, o prompt caching da Anthropic pode não funcionar, o que encarece e distorce a comparação.
- Modelos GPT e Gemini: use o provider `litellm` (`openai-completions`).
- Formato do modelo no config: `provider/modelo`. O comando usa `--provider {{provider}} --model {{model_name}}`.

```json
{pim}```

### OpenCode

Use `opencode.example.json` como config global ou do projeto. Os exemplos oficiais mostram `{{env:VAR}}` em `apiKey`. Não achei exemplo oficial com `{{env:...}}` dentro de `headers`. **Confirme no LiteLLM se a tag chega.** Modelo no config: `litellm/MODELO`.

```json
{ocj}```

### Codex CLI

Adicione o trecho ao `~/.codex/config.toml`. Prefira um `CODEX_HOME` dedicado ao benchmark. `env_http_headers` mapeia header → nome da env var. **Confirme o nome dessas chaves na documentação da sua versão do Codex.** O comando usa `--full-auto` (sandbox com escrita no workspace). Se a tarefa precisar de rede (ex.: `npm ci`), deixe isso no `setup`, que roda antes do agente.

```toml
{cdx}```

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
- **Variância**: use `reps` ≥ 3 e 5–10 tarefas antes de concluir algo.

## Segurança

Os agentes rodam **sem pedir aprovação** e o comando `test` executa código escrito pelo agente. Trate o agente e o repositório da tarefa como não confiáveis: um README ou issue pode trazer prompt injection.

O que o script faz em cada execução:

1. **Workdir fora do repositório do benchmark**: um diretório temporário novo, ou `work_dir` absoluto fora do repositório (o script recusa um caminho dentro dele). Assim o agente não trabalha ao lado de testes ocultos, config e resultados.
2. **Sem histórico git**: o repositório da tarefa é copiado sem `.git` e recebe um único commit `baseline`. Um clone no commit anterior ao fix ainda teria o commit do fix, e o remote apontaria de volta para o benchmark.
3. **Ambiente mínimo**: o agente recebe só `PATH`, `HOME`, `USER`, locale, `TERM`, `TMPDIR`, as variáveis de `env_passthrough` e as do bloco `env` do harness. O `setup` e o `test` recebem o mesmo, sem o `env` do harness. A chave de leitura de spend do LiteLLM (`litellm.api_key`) **nunca** chega ao agente: o script recusa um config que tente repassá-la.
4. **Arquivos protegidos**: antes do agente rodar, cada caminho em `protected_paths` é copiado para fora do workdir. Antes do teste, esse conteúdo volta e tudo que o agente criou ali é apagado. Use para runner de testes e configs (`tests/`, `package.json`, `pytest.ini`, `conftest.py`, `vitest.config.ts`). Sem isso, um agente pode "resolver" a tarefa fazendo o teste passar sempre.
5. **Auditoria**: `logs/<execução>.diff` guarda tudo que o agente mudou em relação ao baseline.
6. **Logs sem segredos**: valores das variáveis do bloco `env` do harness e a chave de spend são trocados por `***REDACTED***`.
7. **Validação do config**: ids de tarefa e nomes de harness só aceitam letras, dígitos, `_`, `.` e `-`. `repo` e `ref` não podem começar com `-`. `protected_paths` precisam ser relativos e sem `..`. O script só apaga diretórios dentro da raiz de trabalho.
8. **LiteLLM**: `base_url` precisa de `https` (http só para localhost) e redirects não são seguidos, para a chave não ir para outro host.

O que o script **não** resolve sozinho: sem sandbox, o agente roda com o seu usuário e consegue ler qualquer arquivo seu (incluindo `~/.ssh`) e alterar os resultados. Por isso `run` exige `sandbox.prefix` ou `--allow-unsandboxed`.

### `sandbox.prefix`

Comando colocado na frente do agente, do `setup` e do `test`. Placeholders: `{{workdir}}` e `{{env_file}}`, um arquivo `KEY=valor` com permissão 0600, apagado logo após cada passo. Exemplo com Docker, montando só o workdir no mesmo caminho:

```json
"sandbox": {{
  "prefix": ["docker", "run", "--rm", "-i",
             "--env-file", "{{env_file}}",
             "-v", "{{workdir}}:{{workdir}}", "-w", "{{workdir}}",
             "IMAGEM-COM-OS-CLIS"]
}}
```

A imagem precisa ter os CLIs dos harnesses, `git`, `sh` e o que os testes usam. Configure a autenticação dos CLIs por variável de ambiente (LiteLLM), não por arquivos do seu HOME. Com o `HOME` da imagem, cada CLI começa com configuração limpa, o que também deixa a comparação mais justa.

## Como escolher tarefas

- Use bugs reais já corrigidos, com `ref` = commit anterior ao fix.
- Escreva o prompt como uma issue, sem dica da solução.
- Use como testes ocultos os testes do commit do fix.
- `setup` (ex.: `npm ci`) roda antes do agente e não entra no custo.
- Liste em `protected_paths` os testes e configs de teste do repositório.

## Placeholders do `command`

`{{prompt}}`, `{{model}}`, `{{provider}}` (parte antes da `/`), `{{model_name}}` (parte depois da `/`).
Em `env`: `${{VAR}}` é expandido do ambiente e `{{run_tag}}` vira a tag da execução. `BENCH_TAG` é sempre exportada.
Em `sandbox.prefix`: `{{workdir}}` e `{{env_file}}`.

---

## `harness_bench.py`

```python
{py}```

## `harness_bench_litellm.py`

```python
{pyl}```

## `config.litellm.example.json`

```json
{cfgl}```

## `config.example.json`

```json
{cfg}```

## Tarefa de exemplo

`tasks/fizzbuzz/repo/fizzbuzz.py` (bugs propositais):

```python
{fz}```

`tasks/fizzbuzz/hidden_tests/test_hidden.py`:

```python
{th}```

## Testes offline

Rode `python3 tests/smoke.py`. Ele usa agentes e LiteLLM falsos para validar os parsers, o `reconcile` e os dois scripts, sem rede e sem CLIs reais.

`tests/smoke.py`:

```python
{smoke}```

`tests/fake_agent.py`:

```python
{fagent}```

`tests/fake_litellm.py`:

```python
{flite}```

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
'''
(b/'HARNESS_BENCH.md').write_text(md)
print(f"wrote {b / 'HARNESS_BENCH.md'} ({len(md.splitlines())} lines)")
