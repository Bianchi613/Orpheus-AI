# Arquitetura Do Orpheus

Este documento explica como o Orpheus foi dividido depois da refatoracao do `ai.py`.

O objetivo da divisao nao e deixar arquivos pequenos por estetica. A divisao existe para separar responsabilidades grandes e reais.

## Visao Geral

```text
ai.py
orpheus/
  __init__.py
  config.py
  api.py
  executor.py
  state.py
  app.py
```

Fluxo principal:

```text
usuario
  -> orpheus/app.py
  -> orpheus/api.py
  -> modelo OpenAI/Ollama
  -> ferramenta solicitada
  -> orpheus/executor.py ou orpheus/state.py
  -> resultado volta ao modelo
  -> resposta final no terminal
```

## Responsabilidade De Cada Parte

### `ai.py`

Entrada compatível do programa.

Ele deve continuar simples:

- importar `main`;
- chamar `main()`;
- preservar `python3 ai.py`;
- preservar o atalho `ai`.

### `orpheus/config.py`

Configuração central.

Responsavel por:

- carregar `.env`;
- definir `WORKDIR`;
- definir `SCRIPT_DIR`;
- guardar `APP_VERSION`;
- guardar `SESSION_ID`;
- guardar `CURRENT_TASK_ID`;
- definir caminhos privados:
  - `.ai_memory.json`;
  - `ai.log`;
  - `ai.status.json`;
  - `.ai_prompt.txt`;
- ler flags:
  - `AI_AUTO_APPROVE`;
  - `AI_SELF_MODIFY`;
  - `AI_MAX_STEPS`;
  - `AI_COMMAND_TIMEOUT`;
  - `AI_LONG_COMMAND_TIMEOUT`;
  - `AI_IDLE_TIMEOUT`;
  - `AI_MAX_OUTPUT`;
- definir cores do terminal.

Quando precisar de uma configuracao global, coloque aqui.

### `orpheus/api.py`

Comunicação com modelos e busca web.

Responsavel por:

- cliente HTTP compatível com Chat Completions;
- OpenAI;
- Ollama;
- ferramenta `run_shell` exposta ao modelo;
- ferramenta `fetch_url` exposta ao modelo;
- busca HTTP/HTTPS pública com limite de bytes;
- erros de HTTP/rede da API.

Nao deve executar comandos locais. Isso fica em `executor.py`.

### `orpheus/executor.py`

Executor local do terminal.

Responsavel por:

- executar comandos;
- pedir confirmacao humana quando o comando altera estado;
- permitir leitura direta quando o comando parece seguro;
- bloquear acoes destrutivas;
- bloquear leitura de segredos;
- lidar com `sudo`;
- configurar `/sudo setup`;
- simular APT antes de instalacao/upgrade;
- detectar plano APT que removeria pacotes;
- executar comandos com saida em tempo real;
- avisar quando um comando fica sem nova saida;
- aplicar timeout comum e timeout longo;
- devolver `stdout`, `stderr`, `exit_code` e `timed_out` para o modelo.

Regra importante: toda acao real no sistema deve passar pelo executor.

### `orpheus/state.py`

Estado local do Orpheus.

Responsavel por:

- log local `ai.log`;
- sanitizacao simples de segredos no log;
- memoria persistente `.ai_memory.json`;
- substituicao de memorias antigas do mesmo tipo;
- inferencia automatica de memorias a partir de texto do usuario;
- inferencia automatica de memorias a partir de comandos bem-sucedidos;
- status vivo `ai.status.json`;
- comandos de monitor:
  - `/monitor`;
  - `/monitor watch N`;
  - `--monitor`;
- painel textual:
  - `/status`;
  - `/board`;
  - `/brief`;
  - `/alerts`;
  - `/health`;
  - `/incident`;
- baseline:
  - `/baseline`;
  - `/compare`.

Arquivos de estado sao locais e nao devem subir para o GitHub.

### `orpheus/app.py`

Orquestração principal.

Responsavel por:

- carregar prompt externo;
- injetar memoria no prompt;
- injetar permissao de auto-programacao no prompt;
- abrir o REPL do terminal;
- interpretar comandos internos;
- manter historico da conversa;
- chamar a API;
- receber tool calls;
- chamar executor/state/api conforme a ferramenta;
- devolver resultados de ferramentas para o modelo;
- evitar que a IA apenas oriente quando pode agir;
- pedir clarificacao quando o pedido e ambiguo;
- encerrar quando a tarefa termina.

Se uma mudanca envolve como o agente decide o proximo passo, provavelmente fica aqui.

## Arquivos Locais Privados

Estes arquivos sao dados locais e estao no `.gitignore`:

```text
.env
.ai_memory.json
.orpheus_baseline.json
ai.status.json
ai.log
*.log
logs/
.local-backups/
.local-trash/
__pycache__/
```

Nao coloque chaves, memoria, logs ou status em commits.

## Auto-Programacao

Quando `AI_SELF_MODIFY=1`, o agente pode editar o proprio projeto se o usuario pedir.

Arquivos normais de auto-programacao:

```text
ai.py
orpheus/*.py
.ai_prompt.txt
.env.example
README.md
ARCHITECTURE.md
system-requirements.txt
```

Arquivos que nao devem ser lidos nem revelados:

```text
.env
arquivos de credenciais
chaves
tokens
senhas
```

## Validacao Obrigatoria

Depois de mexer no codigo:

```sh
python3 -m py_compile ai.py orpheus/*.py
python3 ai.py --help
python3 ai.py --monitor
printf '/exit\n' | python3 ai.py --provider ollama
```

Depois de mexer somente em documentacao, pelo menos rode:

```sh
git diff --check
```

## Como Pensar Em Novas Mudancas

- Configuracao, caminhos e flags: `config.py`.
- Modelo, OpenAI, Ollama e HTTP publico: `api.py`.
- Comandos locais, bloqueios e sudo: `executor.py`.
- Memoria, logs, monitor, painel e baseline: `state.py`.
- Loop do agente, prompt e comandos internos: `app.py`.
- Compatibilidade de entrada: `ai.py`.
