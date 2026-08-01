# Orpheus

Orpheus e um assistente de IA para operar o terminal Linux local. Ele conversa em linguagem natural, executa comandos no proprio terminal, le `stdout`, `stderr` e codigo de saida, e continua trabalhando ate concluir a tarefa ou precisar de uma decisao humana.

O projeto usa somente a biblioteca padrao do Python. Nao ha dependencias Python em `pip install -r requirements.txt`.

Versao atual no codigo: `2026.08.01.30`.

## O Que Ele Faz

- conversa diretamente no terminal, sem interface grafica;
- usa OpenAI ou Ollama;
- executa comandos locais via ferramenta interna;
- pede confirmacao antes de comandos que alteram o sistema;
- bloqueia acoes destrutivas permanentes;
- salva memoria persistente em JSON;
- pode consultar URLs publicas com `/web`;
- mostra painel de bordo do sistema;
- grava status vivo em `ai.status.json`;
- permite acompanhar o agente com `--monitor`, `/monitor` e `/monitor watch N`;
- mostra saida dos comandos em tempo real;
- avisa quando um comando fica sem nova saida;
- usa timeout maior para instaladores e downloads demorados;
- registra eventos em log local;
- compara o estado atual com um baseline;
- pode se auto-programar quando essa opcao estiver ativa;
- possui comentarios no codigo explicando os blocos principais.

## Instalacao

Em Ubuntu, Zorin, Debian e derivados:

```sh
sudo apt update
sudo xargs -a system-requirements.txt apt install -y
```

O arquivo `system-requirements.txt` contem os programas de sistema esperados:

```text
python3
bash
sudo
coreutils
curl
ca-certificates
```

Ollama e Docker aparecem como opcionais nesse arquivo.

## Configuracao

Copie o exemplo:

```sh
cp .env.example .env
```

Escolha o provedor no `.env`:

```env
AI_PROVIDER=openai
```

ou:

```env
AI_PROVIDER=ollama
```

Para OpenAI:

```env
OPENAI_API_KEY=
OPENAI_MODEL=gpt-5.6
```

Para Ollama:

```env
OLLAMA_MODEL=qwen3:8b
OLLAMA_BASE_URL=http://localhost:11434/v1
```

Para baixar o modelo local:

```sh
ollama pull qwen3:8b
```

## Variaveis Do Executor

As principais opcoes do `.env.example` sao:

```env
AI_AUTO_APPROVE=0
AI_SELF_MODIFY=0
AI_MAX_STEPS=30
AI_COMMAND_TIMEOUT=300
AI_LONG_COMMAND_TIMEOUT=900
AI_IDLE_TIMEOUT=45
AI_MAX_OUTPUT=12000
AI_PROMPT_FILE=
```

`AI_AUTO_APPROVE=1` executa automaticamente comandos permitidos. Comandos perigosos continuam bloqueados.

`AI_SELF_MODIFY=1` permite que o agente altere seus proprios arquivos quando voce pedir para corrigir ou melhorar o agente.

`AI_COMMAND_TIMEOUT=300` evita que downloads grandes sejam cortados cedo demais.

`AI_LONG_COMMAND_TIMEOUT=900` dá mais tempo para instaladores e downloads como `apt`, `flatpak`, `snap`, `ollama pull` e `docker pull`.

`AI_IDLE_TIMEOUT=45` faz o executor avisar quando um comando fica muito tempo sem produzir saída, sem matar o comando antes do timeout total.

`AI_PROMPT_FILE` permite apontar para outro prompt. Se ficar vazio, ele usa `.ai_prompt.txt` ao lado do `ai.py`.

Tambem existem variaveis opcionais para trocar os caminhos dos arquivos locais:

```env
AI_MEMORY_FILE=/caminho/para/.ai_memory.json
AI_LOG_FILE=/caminho/para/ai.log
AI_STATE_FILE=/caminho/para/ai.status.json
```

## Como Rodar

Direto:

```sh
python3 ai.py
```

Com um pedido inicial:

```sh
python3 ai.py "verifique se o Docker esta funcionando"
```

Forcando provedor:

```sh
python3 ai.py --provider openai
python3 ai.py --provider ollama
```

Modo monitor, para ver o estado vivo gravado pelo agente:

```sh
python3 ai.py --monitor
```

Se o atalho estiver instalado, use:

```sh
ai
```

## Memoria

A memoria persistente fica em `.ai_memory.json`, ou no caminho definido por `AI_MEMORY_FILE`.

Comandos:

- `/mem` mostra a memoria salva;
- `/remember texto` salva uma informacao manualmente;
- `/forget N` remove o item numero `N`.

A memoria manual salva qualquer frase valida. A memoria automatica salva apenas fatos considerados uteis, como sistema operacional, preferencias, Docker instalado, servico do Docker, grupo `docker`, provedor desejado e auto-programacao.

O arquivo `.ai_memory.json` e local e esta no `.gitignore`. Ele nao deve ser enviado para o GitHub.

## Monitor E Status Vivo

O Orpheus grava o estado atual em `ai.status.json`, ou no caminho definido por `AI_STATE_FILE`.

Esse arquivo mostra informacoes como:

- versao do agente;
- ID da sessao;
- ID da tarefa atual;
- PID do processo;
- pasta atual;
- estado atual, como `idle`, `api_wait`, `running_command`, `command_finished`, `completed` ou `stopped`;
- resumo curto;
- horario da ultima atualizacao.

Formas de consultar:

```sh
python3 ai.py --monitor
```

Dentro do agente:

```text
/monitor
/monitor watch 2
```

`/monitor watch N` reexibe o status a cada `N` segundos.

O `ai.status.json` tambem e local e esta no `.gitignore`.

## Auto-Programacao

Quando `AI_SELF_MODIFY=1`, o agente pode editar arquivos do proprio projeto quando voce pedir algo como:

```text
corrija o agente
melhore seu codigo
revise e arrume o ai.py
```

Comandos:

- `/self status` mostra o estado;
- `/self on` ativa na sessao atual;
- `/self off` desativa na sessao atual.

Depois de mexer no codigo, o agente deve validar com:

```sh
python3 -m py_compile ai.py
```

Ele nao deve ler nem revelar `.env`, chaves, tokens ou senhas.

O prompt operacional fica fora do codigo, em `.ai_prompt.txt`. O `ai.py` apenas carrega esse prompt e injeta variaveis como pasta atual, memoria e estado de auto-programacao.

## Seguranca

Bloqueios permanentes:

- exclusao de arquivos e diretorios;
- remocao, purge, autoremove e desinstalacao;
- formatacao, particionamento e escrita direta em disco;
- prune/remocao de containers, imagens, volumes e redes;
- desligamento ou reinicializacao automatica;
- troca interativa de usuario;
- leitura de credenciais e segredos.

Comandos que alteram estado pedem confirmacao. Comandos de leitura normalmente rodam direto.

`sudo` e permitido quando necessario, mas a senha e digitada no terminal e nao e enviada para a IA.

Antes de executar instalacoes ou upgrades por APT, o executor tenta simular o plano para detectar remocoes de pacotes. Se a simulacao indicar remocao, o comando e bloqueado.

Para comandos demorados, o executor usa saida em tempo real e watchdog:

- comandos comuns usam `AI_COMMAND_TIMEOUT`;
- instaladores/downloads usam `AI_LONG_COMMAND_TIMEOUT`;
- se nao houver nova saida por `AI_IDLE_TIMEOUT`, o terminal mostra um aviso;
- se o timeout total acabar, o processo e interrompido e a IA recebe o resultado como timeout para tentar outro caminho.

## Comandos Internos

Comandos de conversa:

- `/new` limpa o contexto da conversa;
- `/where` mostra a pasta atual;
- `/exit` encerra.

Comandos de execucao:

- `/auto on` ativa execucao automatica para comandos permitidos;
- `/auto off` volta a pedir confirmacao para comandos que alteram estado;
- `/sudo` renova a sessao sudo;
- `/sudo setup` cria uma regra sudoers limitada para comandos de manutencao.

Comandos de memoria:

- `/mem`;
- `/remember texto`;
- `/forget N`.

Comandos de bordo:

- `/status` ou `/board` mostra o painel principal;
- `/brief` mostra um resumo curto;
- `/alerts` mostra alertas atuais;
- `/health` executa checagens simples;
- `/incident` resume o estado atual e o proximo passo;
- `/watch N` repete o painel a cada `N` segundos;
- `/monitor` mostra o status vivo atual;
- `/monitor watch N` acompanha o status vivo a cada `N` segundos.

Baseline:

- `/baseline` salva o estado atual em `.orpheus_baseline.json`;
- `/compare` compara o estado atual com o baseline salvo.

Web e logs:

- `/web URL` busca uma URL publica e resume o retorno;
- `/logs` mostra o caminho e ultimas linhas do log;
- `/logs tail N` mostra as ultimas `N` linhas;
- `/logs grep TEXTO` filtra o log por texto.

## Arquivos Do Projeto

- `ai.py`: programa principal;
- `.ai_prompt.txt`: prompt operacional externo;
- `.env.example`: exemplo de configuracao;
- `.env`: configuracao local, nao deve ser compartilhada;
- `.ai_memory.json`: memoria persistente;
- `.orpheus_baseline.json`: baseline local criado por `/baseline`;
- `ai.status.json`: status vivo local do agente;
- `ai.log`: log local do agente;
- `system-requirements.txt`: pacotes de sistema.

Arquivos locais ignorados pelo git:

- `.env`;
- `.ai_memory.json`;
- `.orpheus_baseline.json`;
- `ai.status.json`;
- `ai.log`;
- `*.log`;
- `logs/`;
- `.local-backups/`;
- `.local-trash/`;
- `__pycache__/`.

## Observacoes

- O agente nao usa interface grafica.
- O funcionamento acontece na mesma janela do terminal.
- OpenAI funciona via API externa configurada no `.env`.
- Ollama funciona localmente se o servidor estiver rodando.
- Apos adicionar usuario ao grupo `docker`, pode ser necessario sair e entrar novamente na sessao para usar Docker sem `sudo`.
