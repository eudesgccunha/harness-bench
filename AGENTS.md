# AGENTS.md

Benchmark de **harness × modelo** para agentes de código. Métrica principal: **custo por tarefa resolvida**. O benchmark de verdade roda em outra máquina, com CLIs reais e LiteLLM. Neste repositório, a verificação é offline, com agentes e LiteLLM falsos.

## Mapa

- `harness_bench.py`: motor e única fonte de verdade. Contém `run`, `reconcile` (custo real do LiteLLM), `report` e um parser por harness.
- `harness_bench_litellm.py`: wrapper fino sobre o motor. Remove harnesses com `parser: cursor` e força o custo LiteLLM. Lógica nova vai para o motor; o wrapper só filtra o config e encadeia comandos.
- `HARNESS_BENCH.md`: arquivo **gerado** por `scripts/build_md.py`. Edite o gerador e regenere o arquivo.
- `tests/`: `fake_agent.py` imita a saída JSON de cada CLI e sonda vazamentos (env, histórico git, adulteração de testes). `fake_litellm.py` imita `/spend/logs/v2`, e `smoke.py` testa tudo ponta a ponta.
- `*.example.*`: configs de exemplo do benchmark (`config*.json`) e de cada harness apontando para o LiteLLM (Pi, OpenCode, Codex).

## Regras

- Python 3.12, só biblioteca padrão. O ambiente de execução não instala dependências.
- O único acesso de rede é `GET` no LiteLLM configurado, feito pelo `reconcile`.
- Segredos entram só por env (`${VAR}` no config). `config*.json` (exceto exemplos), `results*/` e `.cache/` ficam fora do git, porque contêm URLs internas, código e respostas dos agentes.
- Docs e relatório em português. Código e comentários em inglês.
- Custo desconhecido é `None`, nunca `0`. Um `0` falso joga o par para o topo do ranking de custo.
- O agente e o repositório da tarefa são **não confiáveis**. Toda mudança em `cmd_run` preserva as defesas: workdir fora do repositório e sem histórico git; ambiente por allowlist (`BASE_ENV` + `env_passthrough` + `env` do harness), sem a chave de spend; `protected_paths` restaurados a partir de um snapshot fora do workdir; diff de auditoria; logs com redação; `rmtree_inside` para apagar; comandos sem `shell=True` e passados por `wrap()` (sandbox). O `tests/smoke.py` tem um caso para cada defesa. Uma defesa nova ganha um caso novo.

## Semântica de usage por harness

Verificada contra a documentação e fontes da comunidade em nov/2026. O formato muda entre versões dos CLIs.

- `claude`: soma `modelUsage`, que inclui modelos auxiliares. Sem `modelUsage`, usa `usage`.
- `codex`: o `usage` de `turn.completed` é o total acumulado da thread, então use só o último. `cached_input_tokens` faz parte de `input_tokens`, e `reasoning_output_tokens` faz parte de `output_tokens`.
- `pi`: conte só `message_end` com `role: assistant`. O mesmo usage também aparece em `message_update` e em `turn_end`.
- `opencode`: soma todos os `step_finish`. `reasoning` fica separado de `output`.
- `cursor`: o JSON oficial não tem usage. O parser retorna `None` e o par entra só com taxa de acerto e tempo.
- O custo real vem do LiteLLM. Cada execução exporta `BENCH_TAG`, o harness envia a tag no header `x-litellm-tags` e o `reconcile` filtra os logs por `request_tags`.

## Adicionar um harness

1. Escreva `parse_<nome>(stdout) -> dict | None` em `harness_bench.py` e registre a função em `PARSERS`.
2. Adicione o formato de saída em `tests/fake_agent.py` e asserts em `tests/smoke.py`.
3. Adicione o harness em `config.example.json`. Se ele passa pelo LiteLLM, adicione também em `config.litellm.example.json`, com config de exemplo que envia `BENCH_TAG`.
4. Documente em `scripts/build_md.py`.

## Pronto quando

`python3 tests/smoke.py` termina com `PASS` e `python3 scripts/build_md.py` regenerou o `HARNESS_BENCH.md`.
