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
python3 harness_bench_litellm.py run --config config.litellm.json --reps 1 --only pi
python3 harness_bench_litellm.py run --config config.litellm.json
```

O resultado fica em `results-litellm/summary.md`.

Antes, configure cada harness para usar o LiteLLM e enviar a tag da execução. Os arquivos de exemplo são `pi-models.example.json`, `opencode.example.json` e `codex-config.example.toml`. Para o Claude Code, o próprio config cuida disso.

## Dois scripts

| Script | Harnesses | Custo |
|---|---|---|
| `harness_bench_litellm.py` | Claude Code, Pi, OpenCode, Codex | sempre real (LiteLLM) |
| `harness_bench.py` | todos, incluindo Cursor | LiteLLM → tabela de preços → custo reportado pelo harness |

## Documentação completa

[`HARNESS_BENCH.md`](HARNESS_BENCH.md) reúne o passo a passo, a configuração de cada harness, o que validar no LiteLLM, as regras para uma comparação justa e o código-fonte completo. Dá para recriar o projeto só com esse arquivo.

## Segurança

- Os agentes rodam **sem pedir aprovação**. Use VM ou container isolado, sem acesso a credenciais sensíveis.
- O `.gitignore` já exclui configs locais, resultados e tarefas reais. Não force a inclusão desses arquivos.
- Passe chaves só por variável de ambiente.
