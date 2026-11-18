# harness-bench

Qual combinação de **ferramenta de agente** (harness) e **modelo** resolve suas tarefas pelo menor custo?

Este repositório roda as mesmas tarefas reais em vários pares harness × modelo. Para cada par, mede:

- se a tarefa foi resolvida, pelos testes ocultos;
- quantos tokens foram gastos;
- quanto custou de verdade, pelos spend logs do LiteLLM.

O relatório é ordenado por **custo por tarefa resolvida**.

Harnesses suportados: Claude Code, Pi, OpenCode e Codex CLI, todos via LiteLLM, e Cursor (`agent` CLI), que entra só com taxa de acerto e tempo.

## Início rápido

Requisitos: Python 3.12+, `git` e os CLIs instalados.

```bash
python3 tests/smoke.py                               # teste offline do script

export LITELLM_BASE_URL=https://seu-litellm.exemplo.com
export LITELLM_API_KEY=sk-...                        # usada pelos agentes
export LITELLM_SPEND_KEY=sk-...                      # leitura de /spend/logs

cp config.litellm.example.json config.litellm.json   # preencha modelos e tarefas
python3 harness_bench_litellm.py run --config config.litellm.json --dry-run
python3 harness_bench_litellm.py run --config config.litellm.json --reps 1 --only pi   # + --allow-unsandboxed se não houver sandbox
python3 harness_bench_litellm.py run --config config.litellm.json
```

O resultado fica em `results-litellm/summary.md`.

Com a imagem Docker, os configs de Pi, OpenCode e Codex são gerados no container a partir de `LITELLM_BASE_URL`. Sem Docker, use `pi-models.example.json`, `opencode.example.json` e `codex-config.example.toml`, trocando `__LITELLM_BASE_URL__`. Para o Claude Code, o próprio config cuida disso.

## Dois scripts

| Script | Harnesses | Custo |
|---|---|---|
| `harness_bench_litellm.py` | Claude Code, Pi, OpenCode, Codex | sempre real (LiteLLM) |
| `harness_bench.py` | todos, incluindo Cursor | LiteLLM → tabela de preços → custo reportado pelo harness |

## Documentação completa

[`HARNESS_BENCH.md`](HARNESS_BENCH.md) reúne o passo a passo, a configuração de cada harness, o que validar no LiteLLM, as regras para uma comparação justa e o código-fonte completo. Dá para recriar o projeto só com esse arquivo.

## Segurança

Os agentes rodam **sem pedir aprovação** e os testes executam código escrito por eles. O script isola o que consegue:

- workdir temporário fora do repositório, sem histórico git;
- ambiente mínimo, sem a chave de spend do LiteLLM;
- arquivos de teste protegidos contra adulteração;
- diff de auditoria e logs sem segredos.

O resto depende de sandbox. O `run` exige `sandbox.prefix` ou `--allow-unsandboxed`, que só deve ser usado dentro de uma VM descartável. Os configs de exemplo já usam a imagem de `sandbox/Dockerfile`, que tem todos os CLIs:

```bash
docker build -f sandbox/Dockerfile -t harness-bench-agents \
  --build-arg UID=$(id -u) --build-arg GID=$(id -g) .
python3 tests/docker_e2e.py
```
 Detalhes na seção "Segurança" do [`HARNESS_BENCH.md`](HARNESS_BENCH.md).

O `.gitignore` já exclui configs locais, resultados e tarefas reais. Não force a inclusão desses arquivos.
