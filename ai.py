"""Orquestração principal do Orpheus.

Este módulo responde: como configuração, API, executor e estado viram um agente?
Ele contém o prompt, o loop da tarefa, comandos internos e a entrada main().
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from typing import Any

import api
import config as cfg
import executor
import state

def self_modify_text() -> str:
    """Diz ao modelo se ele pode alterar o próprio agente nesta sessão."""
    if cfg.SELF_MODIFY:
        return (
            "Auto-programação: ativa. Quando o usuário pedir para corrigir, melhorar "
            "ou revisar o próprio agente, você pode editar ai.py, config.py, api.py, executor.py e state.py, "
            ".ai_prompt.txt, .env.example e documentação relacionada. Depois de mexer "
            "no código, valide com python3 -m py_compile ai.py config.py api.py executor.py state.py antes de encerrar. Nunca leia, "
            "copie ou revele .env, chaves ou senhas."
        )

    return (
        "Auto-programação: desativada. Não edite o próprio agente; apenas diagnostique "
        "e explique o que mudaria."
    )

def load_prompt_template() -> str:
    """Carrega o prompt externo; o comportamento do agente mora fora do código."""
    try:
        text = cfg.PROMPT_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        print(f"Erro: não consegui ler o prompt em {cfg.PROMPT_PATH}.", file=sys.stderr)
        raise SystemExit(1)

    if not text:
        print(f"Erro: o prompt {cfg.PROMPT_PATH} está vazio.", file=sys.stderr)
        raise SystemExit(1)

    return text


def render_prompt(template: str, memory: list[str]) -> str:
    """Preenche variáveis do prompt e informa os caminhos internos reais."""
    return (
        template
        .replace("{{WORKDIR}}", str(cfg.WORKDIR))
        .replace("{{SCRIPT_DIR}}", str(cfg.SCRIPT_DIR))
        .replace("{{LOG_PATH}}", str(cfg.LOG_PATH))
        .replace("{{STATE_PATH}}", str(cfg.STATE_PATH))
        .replace("{{MEMORY_PATH}}", str(cfg.MEMORY_PATH))
        .replace("{{PROMPT_PATH}}", str(cfg.PROMPT_PATH))
        .replace("{{MEMORY}}", state.memory_text(memory))
        .replace("{{SELF_MODIFY}}", self_modify_text())
    )

def command_result_text(result: executor.CommandResult) -> str:
    """Serializa resultado de comando no formato que volta para o modelo."""
    text = "\n".join(part for part in (result.stdout, result.stderr) if part)
    diagnostic_hint = None
    if re.search(r"(operação não permitida|permission denied|operation not permitted)", text, re.I):
        diagnostic_hint = (
            "O comando falhou por permissão. Explique isso ao usuário e tome a próxima "
            "providência segura: tentar uma alternativa sem privilégio, pedir autorização "
            "para sudo quando a tarefa realmente exigir, ou encerrar com conclusão clara."
        )

    return json.dumps(
        {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
            "diagnostic_hint": diagnostic_hint,
        },
        ensure_ascii=False,
    )


def tool_call_signature(call: dict[str, Any]) -> str:
    """Identifica chamadas repetidas, ignorando o limite variável do leitor de log."""
    function = call.get("function") or {}
    name = str(function.get("name") or "tool")
    if name == "read_agent_log":
        return name
    raw_args = function.get("arguments") or "{}"
    try:
        args = raw_args if isinstance(raw_args, dict) else json.loads(raw_args)
        normalized_args = json.dumps(args, ensure_ascii=False, sort_keys=True)
    except (json.JSONDecodeError, TypeError):
        normalized_args = str(raw_args)
    return f"{name}:{normalized_args}"


# Padrões usados para detectar quando a IA está orientando demais em vez de agir.
TOOL_NUDGE_PATTERNS = [
    re.compile(pattern, re.I)
    for pattern in (
        r"execute o seguinte comando",
        r"rode o seguinte comando",
        r"run the following command",
        r"```(?:bash|sh)?",
        r"\blsb_release -a\b",
        r"\bcat /etc/os-release\b",
        r"\bsudo apt\b",
        r"\bapt install\b",
    )
]

# Comandos de diagnóstico seguros que o executor pode rodar automaticamente
# quando o modelo escreveu "execute tal comando" em vez de chamar a ferramenta.
PROBE_COMMAND_PATTERNS = [
    re.compile(pattern, re.I)
    for pattern in (
        r"\bcat /etc/os-release\b",
        r"\blsb_release -a\b",
        r"\buname -a\b",
        r"\bwhoami\b",
        r"\bpwd\b",
        r"\bdocker --version\b",
        r"\bsystemctl status [A-Za-z0-9_.@-]+\b",
        r"\bjournalctl -u [A-Za-z0-9_.@-]+\b[^\n`]*",
    )
]


def should_nudge_tool_use(text: str) -> bool:
    """Detecta resposta textual que deveria ter usado run_shell."""
    normalized = text.strip()
    if not normalized:
        return False

    return any(pattern.search(normalized) for pattern in TOOL_NUDGE_PATTERNS)


def extract_safe_probe_command(text: str) -> str | None:
    """Extrai um comando de diagnóstico seguro de uma resposta textual da IA."""
    if not text.strip():
        return None

    code_block = re.search(r"```(?:bash|sh)?\s*\n(.*?)```", text, re.I | re.S)
    if code_block:
        for raw_line in code_block.group(1).splitlines():
            candidate = raw_line.strip()
            if candidate and executor.looks_read_only(candidate):
                return candidate

    for pattern in PROBE_COMMAND_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        candidate = match.group(0).strip()
        if executor.looks_read_only(candidate):
            return candidate

    return None



def append_history_message(history: list[Any], role: str, content: str, **extra: Any) -> None:
    """Adiciona mensagem ao histórico em formato compatível com a API."""
    payload: dict[str, Any] = {"role": role, "content": content}
    payload.update(extra)
    history.append(payload)



def run_task(
    client: api.ApiClient,
    model: str,
    prompt_template: str,
    history: list[Any],
    memory: list[str],
    user_text: str,
) -> None:
    """Loop agente: manda pedido para a IA, executa ferramentas e continua."""
    cfg.CURRENT_TASK_ID = f"task-{int(time.time())}"
    state.append_log("user", user_text)
    state.write_status("task_started", "tarefa recebida", task_input=user_text)
    history.append({"role": "user", "content": user_text})
    remembered_from_user = state.add_memory_items(memory, state.infer_memory_from_user_text(user_text))
    for item in remembered_from_user:
        print(f"Memória atualizada: {item}")

    ambiguous_working_query = False
    lowered_user_text = " ".join(user_text.strip().split()).casefold()
    if any(phrase in lowered_user_text for phrase in (
        "está funcionando",
        "esta funcionando",
        "ta funcionando",
        "tá funcionando",
        "funciona?",
        "está funcinando",
        "esta funcinando",
    )):
        explicit_targets = (
            " você", " vc", " tu", "docker", "internet", "wifi", "wi-fi", "bluetooth",
            "áudio", "audio", "microfone", "impressora", "site", "servidor", "serviço",
            "servico", "app", "programa"
        )
        ambiguous_working_query = not any(token in lowered_user_text for token in explicit_targets)
    tool_nudges = 0
    summary_nudges = 0
    executed_tool_signatures: set[str] = set()
    duplicate_tool_nudges = 0

    for _step in range(1, cfg.MAX_STEPS + 1):
        # Cada ciclo envia o histórico atual para a API. Se o modelo pedir
        # ferramenta, executamos e devolvemos o resultado no próximo ciclo.
        try:
            messages = [
                {
                    "role": "system",
                    "content": render_prompt(prompt_template, memory),
                },
            ]
            memory_message = state.memory_system_message(memory)
            if memory_message:
                messages.append({"role": "system", "content": memory_message})
            messages.extend(history)
            state.write_status("api_wait", "aguardando resposta da API", provider=client.url, model=model, step=_step)
            response = client.chat(model, messages)
            state.append_log("api_response", json.dumps(response, ensure_ascii=False)[:4000])
            state.write_status("api_done", "resposta da API recebida", provider=client.url, model=model, step=_step)
        except KeyboardInterrupt:
            print("\nInterrompido pelo usuário.")
            return
        except Exception as exc:
            message = str(exc)
            if "timed out" in message.lower() or "timeout" in message.lower():
                state.append_log("api_timeout", f"{type(exc).__name__}: {exc}")
                print(f"\nA API demorou para responder: {type(exc).__name__}: {exc}")
                print("O agente não travou; a espera terminou por tempo de rede/API.")
                print("Você pode tentar novamente, trocar de provedor/modelo ou repetir o pedido.")
                return
            state.append_log("api_error", f"{type(exc).__name__}: {exc}")
            print(f"\nErro na API: {type(exc).__name__}: {exc}")
            return

        try:
            message = response["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            print(f"\nErro: resposta inesperada da API: {response}")
            return
        history.append(message)
        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            # Sem tool_calls significa que o modelo respondeu em texto. Antes de
            # aceitar, tentamos corrigir casos em que ele só mandou o usuário
            # rodar um comando que o próprio agente poderia executar.
            final_text = (message.get("content") or "").strip()
            if not final_text and summary_nudges < 2:
                summary_nudges += 1
                append_history_message(
                    history,
                    "user",
                    (
                        "Agora responda em texto curto, em português, dizendo "
                        "o que você encontrou, o que fez e se a tarefa terminou."
                    ),
                )
                continue
            if ambiguous_working_query and summary_nudges < 2:
                summary_nudges += 1
                append_history_message(
                    history,
                    "user",
                    (
                        "O pedido do usuário está ambíguo: ele perguntou se algo está funcionando, "
                        "mas não disse o alvo. Não assuma Docker nem outro componente por memória. "
                        "Responda apenas pedindo clarificação objetiva do alvo. Se o usuário falar de você, "
                        "responda sobre seu próprio estado em vez de inferir outro sistema."
                    ),
                )
                continue
            if should_nudge_tool_use(final_text) and tool_nudges < 4:
                tool_nudges += 1
                append_history_message(
                    history,
                    "user",
                    (
                        "Você tem acesso direto à ferramenta run_shell. "
                        "Não peça para eu rodar comandos que você mesmo pode executar. "
                        "Se precisar investigar, instalar, validar ou verificar algo, "
                        "chame a ferramenta agora e continue a tarefa."
                    ),
                )
                continue
            state.write_status("completed", "tarefa concluída", step=_step)
            state.append_log("assistant", final_text or "tarefa encerrada sem resposta textual.")
            print(cfg.colorize(f"\nIA: {final_text or 'tarefa encerrada sem resposta textual.'}", cfg.COLOR_AI))
            return

        should_prompt_validation = False
        should_prompt_docker_journal = False
        intent = (message.get("content") or "").strip()
        has_new_tool = any(tool_call_signature(call) not in executed_tool_signatures for call in tool_calls)
        if intent and has_new_tool:
            print(cfg.colorize(f"\nIA: {intent}", cfg.COLOR_AI))
        elif has_new_tool:
            first_function = (tool_calls[0].get("function") or {}) if tool_calls else {}
            first_name = first_function.get("name")
            try:
                first_raw_args = first_function.get("arguments") or "{}"
                first_args = first_raw_args if isinstance(first_raw_args, dict) else json.loads(first_raw_args)
            except json.JSONDecodeError:
                first_args = {}
            if first_name == "run_shell" and first_args.get("command"):
                fallback_intent = f"Vou pedir autorização para executar: {first_args['command']}"
            elif first_name in {"fetch_url", "api.fetch_url"} and first_args.get("url"):
                fallback_intent = f"Vou consultar esta URL para continuar a investigação: {first_args['url']}"
            else:
                fallback_intent = f"Vou usar a ferramenta {first_name or 'solicitada'} para continuar a tarefa."
            print(cfg.colorize(f"\nIA: {fallback_intent}", cfg.COLOR_AI))

        for call_index, call in enumerate(tool_calls):
            # Para cada ferramenta chamada pelo modelo, executamos localmente e
            # acrescentamos uma mensagem role=tool com o resultado. Isso evita o
            # erro da API de tool_call sem resposta correspondente.
            function = call.get("function") or {}
            result: executor.CommandResult | None = None
            command = ""
            name = function.get("name")
            try:
                raw_args = function.get("arguments") or "{}"
                args = raw_args if isinstance(raw_args, dict) else json.loads(raw_args)
            except json.JSONDecodeError as exc:
                args = None
                result_text = json.dumps({"error": f"argumentos inválidos: {exc}"})
            else:
                signature = tool_call_signature(call)
                duplicate_call = signature in executed_tool_signatures
                if duplicate_call:
                    result_text = json.dumps({
                        "error": "ferramenta já usada nesta tarefa; não repita nem amplie a consulta, responda com as evidências disponíveis"
                    })
                    duplicate_tool_nudges += 1
                else:
                    executed_tool_signatures.add(signature)

                if duplicate_call:
                    pass
                elif name == "run_shell":
                    try:
                        command = args.get("command")
                        if not isinstance(command, str) or not command.strip():
                            raise ValueError("o campo command deve ser uma string não vazia")
                        result = executor.run_command(command, cfg.DEFAULT_TIMEOUT, cfg.MAX_CAPTURE)
                        remembered_from_command = state.add_memory_items(
                            memory,
                            state.infer_memory_from_command_output(command, result),
                        )
                        for item in remembered_from_command:
                            print(f"Memória atualizada: {item}")
                        if executor.docker_status_failed(command, result):
                            should_prompt_docker_journal = True
                        result_text = command_result_text(result)
                    except ValueError as exc:
                        result_text = json.dumps({"error": f"argumentos inválidos: {exc}"})
                elif name in {"fetch_url", "api.fetch_url"}:
                    url = args.get("url")
                    method = args.get("method", "GET")
                    max_bytes = args.get("max_bytes", 12000)
                    if not isinstance(url, str) or not url.strip():
                        result_text = json.dumps({"error": "o campo url deve ser uma string não vazia"})
                    else:
                        result_text = json.dumps(
                            api.fetch_url(url.strip(), str(method), max_bytes),
                            ensure_ascii=False,
                        )
                elif name == "read_agent_log":
                    lines = args.get("lines", 40)
                    try:
                        result_text = state.read_recent_activity(int(lines))
                    except (TypeError, ValueError):
                        result_text = json.dumps({"error": "lines deve ser um número inteiro"})
                else:
                    result_text = json.dumps({"error": "ferramenta desconhecida"})

            append_history_message(
                history,
                "tool",
                result_text,
                tool_call_id=call.get("id", function.get("name", "run_shell")),
            )
            if result and result.exit_code == 125:
                cancelled = json.dumps({"error": "ação cancelada após rejeição do usuário"})
                for pending_call in tool_calls[call_index + 1:]:
                    pending_function = pending_call.get("function") or {}
                    append_history_message(
                        history,
                        "tool",
                        cancelled,
                        tool_call_id=pending_call.get("id", pending_function.get("name", "tool")),
                    )
                state.write_status("cancelled", "ação rejeitada pelo usuário", step=_step)
                print(cfg.colorize("\nIA: Entendido. A ação foi cancelada e não vou insistir nesta tarefa.", cfg.COLOR_AI))
                return
            if result and result.exit_code == 0 and not executor.looks_read_only(command):
                should_prompt_validation = True

        if duplicate_tool_nudges:
            if duplicate_tool_nudges > 1:
                state.write_status("stopped", "repetição de ferramenta bloqueada", step=_step)
                print(cfg.colorize(
                    "\nIA: Interrompi a tarefa porque a mesma consulta foi solicitada repetidamente.",
                    cfg.COLOR_AI,
                ))
                return
            append_history_message(
                history,
                "user",
                (
                    "A consulta repetida foi bloqueada. Não chame outra ferramenta. "
                    "Responda agora usando somente as ações explicitamente presentes no resultado anterior; "
                    "se não houver prova suficiente, diga isso claramente."
                ),
            )
            continue

        if should_prompt_docker_journal:
            # Caso especial: Docker falhou. O próximo passo útil quase sempre é
            # ler journalctl, então o agente é empurrado para investigar.
            append_history_message(
                history,
                "user",
                (
                    "O status do Docker falhou. Investigue agora com "
                    "journalctl -u docker -n 100 --no-pager, identifique a causa "
                    "e continue sozinho."
                ),
            )
            continue

        if should_prompt_validation:
            # Depois de uma mudança real, o agente deve validar em vez de encerrar
            # cedo demais dizendo só "pronto".
            append_history_message(
                history,
                "user",
                (
                    "Continue sozinho. Se você acabou de instalar, configurar ou alterar "
                    "algo, valide agora com comandos objetivos antes de encerrar e depois "
                    "resuma o resultado em português."
                ),
            )

    print(f"\nInterrompido: o limite de {cfg.MAX_STEPS} ciclos foi atingido.")


def print_banner(provider: str, model: str, memory_count: int) -> None:
    """Imprime o cabeçalho inicial com modo, modelo, caminhos e comandos."""
    mode = "automático" if cfg.AUTO_APPROVE else "confirma comandos"
    self_mode = "ativa" if cfg.SELF_MODIFY else "desativada"
    print(cfg.colorize(cfg.APP_NAME, cfg.BOLD + cfg.COLOR_INFO))
    print(f"Versão:   {cfg.APP_VERSION}")
    print(f"Sessão:   {cfg.SESSION_ID}")
    print(f"Provedor: {provider}")
    print(f"Modelo:   {model}")
    print(f"Pasta:  {cfg.WORKDIR}")
    print(f"Modo:   {mode}")
    print(f"Prompt:  {cfg.PROMPT_PATH}")
    print(f"Log:     {cfg.LOG_PATH}")
    print(f"Status:  {cfg.STATE_PATH}")
    print("Bloqueios permanentes: exclusão, desinstalação e formatação.")
    print("sudo: permitido somente com confirmação e senha no terminal.")
    print(f"Auto-programação: {self_mode}. Use /self on, /self off ou /self status.")
    print("Comandos de bordo: /board, /brief, /alerts, /baseline, /compare, /incident, /monitor.")
    print(f"Memória: {memory_count} item(ns) salvo(s). Use /mem, /remember e /forget.")
    print("Digite /exit para sair.\n")


def main() -> int:
    """Entrada principal: parseia argumentos e abre o REPL do terminal."""
    parser = argparse.ArgumentParser(description="Orpheus - computador de bordo inteligente para Linux")
    parser.add_argument("--provider", choices=("openai", "xai", "ollama"))
    parser.add_argument("--model")
    parser.add_argument("pedido", nargs="*")
    parser.add_argument("--self-modify", choices=("on", "off"))
    parser.add_argument("--monitor", action="store_true")
    args = parser.parse_args()

    if args.monitor:
        # Modo observador: apenas lê ai.status.json e sai.
        state.print_monitor()
        return 0

    provider = (args.provider or os.environ.get("AI_PROVIDER", "openai")).lower()
    if provider == "ollama":
        model = args.model or os.environ.get("OLLAMA_MODEL", "qwen3:8b")
        base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1")
        client = api.ApiClient(base_url)
    elif provider == "xai":
        model = args.model or os.environ.get("XAI_MODEL", "grok-build-0.1")
        api_key = os.environ.get("XAI_API_KEY")
        if not api_key:
            print("Erro: XAI_API_KEY não está definida no ambiente ou no arquivo .env.")
            return 1
        base_url = os.environ.get("XAI_BASE_URL", "https://api.x.ai/v1")
        client = api.ApiClient(base_url, api_key)
        os.environ.pop("XAI_API_KEY", None)
        del api_key
    else:
        model = args.model or os.environ.get("OPENAI_MODEL", "gpt-5.6")
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            print("Erro: OPENAI_API_KEY não está definida no ambiente ou no arquivo .env.")
            return 1
        client = api.ApiClient("https://api.openai.com/v1", api_key)
        os.environ.pop("OPENAI_API_KEY", None)
        del api_key

    if args.self_modify == "on":
        # Permissão de auto-programação pode vir do argumento ou do .env.
        cfg.SELF_MODIFY = True
    elif args.self_modify == "off":
        cfg.SELF_MODIFY = False

    prompt_template = load_prompt_template()
    history: list[Any] = []
    state.ensure_local_files()
    memory = state.load_memory()
    state.write_status("idle", "agente iniciado e aguardando comando", provider=provider, model=model)
    print_banner(provider, model, len(memory))

    initial = " ".join(args.pedido).strip()
    if initial:
        run_task(client, model, prompt_template, history, memory, initial)

    while True:
        # REPL principal: lê comandos internos (/mem, /logs etc.) ou envia texto
        # normal para o agente resolver com API + ferramentas.
        try:
            text = input(cfg.colorize("\nVocê: ", cfg.COLOR_USER)).strip()
        except (EOFError, KeyboardInterrupt):
            state.write_status("stopped", "agente encerrado pelo terminal")
            print("\nEncerrado.")
            return 0

        if not text:
            continue

        command = text.lower()

        if command in {"/exit", "/quit", "exit", "quit"}:
            state.write_status("stopped", "encerrado por comando do usuário")
            print("Encerrado.")
            return 0

        if command in {"/status", "/board"}:
            state.print_status_panel()
            continue

        if command == "/brief":
            state.print_brief()
            continue

        if command == "/alerts":
            state.print_alerts_only()
            continue

        if command == "/baseline":
            snapshot = state.collect_system_snapshot()
            path = state.save_baseline(snapshot)
            print(f"Baseline salva em {path}")
            continue

        if command == "/compare":
            snapshot = state.collect_system_snapshot()
            baseline = state.load_baseline()
            if not baseline:
                print("Nenhum baseline salvo ainda. Use /baseline primeiro.")
                continue
            changes = state.compare_with_baseline(snapshot, baseline)
            if changes:
                print("Mudanças desde o baseline:")
                for item in changes:
                    print(f"- {item}")
            else:
                print("Nenhuma mudança detectada nos campos monitorados.")
            continue

        if command == "/incident":
            state.print_incident_summary()
            continue

        if command == "/health":
            _snapshot, alerts = state.print_status_panel()
            if alerts:
                print("\nResumo: há alertas que merecem atenção.")
            else:
                print("\nResumo: nenhuma condição simples de alerta foi detectada.")
            continue

        if command.startswith("/watch"):
            parts = text.split(maxsplit=1)
            interval = 5
            if len(parts) == 2:
                try:
                    interval = int(parts[1])
                except ValueError:
                    print("Uso: /watch N")
                    continue
            state.run_watch(interval)
            continue

        if command.startswith("/web "):
            url = text[len("/web "):].strip()
            if not url:
                print("Uso: /web URL")
                continue
            state.print_web_summary(url)
            continue

        if command == "/monitor":
            state.print_monitor()
            continue

        if command.startswith("/monitor watch"):
            parts = text.split()
            interval = 2
            if len(parts) >= 3:
                try:
                    interval = int(parts[2])
                except ValueError:
                    print("Uso: /monitor watch N")
                    continue
            state.watch_monitor(interval)
            continue

        if command == "/logs":
            print(f"Log: {cfg.LOG_PATH}")
            print(state.read_log_tail(40))
            continue

        if command.startswith("/logs tail"):
            parts = text.split(maxsplit=2)
            lines = 40
            if len(parts) >= 3:
                try:
                    lines = int(parts[2])
                except ValueError:
                    print("Uso: /logs tail N")
                    continue
            print(f"Log: {cfg.LOG_PATH}")
            print(state.read_log_tail(lines))
            continue

        if command.startswith("/logs grep "):
            term = text[len("/logs grep "):].strip()
            if not term:
                print("Uso: /logs grep TEXTO")
                continue
            print(f"Log: {cfg.LOG_PATH}")
            print(state.grep_log(term))
            continue

        if command == "/new":
            history.clear()
            print("Contexto limpo.")
            continue

        if command == "/where":
            print(cfg.WORKDIR)
            continue

        if command == "/mem":
            if not memory:
                print("Memória vazia.")
            else:
                records = {
                    state.canonical_memory_key(record["text"]): record
                    for record in state.memory_records()
                }
                for index, item in enumerate(memory, 1):
                    record = records.get(state.canonical_memory_key(item), {})
                    created_at = record.get("created_at", "data desconhecida")
                    updated_at = record.get("updated_at", created_at)
                    date_text = created_at if updated_at == created_at else f"{created_at}; atualizada {updated_at}"
                    print(f"{index}. [{date_text}] {item}")
            continue

        if command.startswith("/remember "):
            item = text[len("/remember "):].strip()
            if not item:
                print("Uso: /remember informação para salvar")
                continue
            added = state.add_memory_items(memory, [item], force=True)
            if not added:
                print("Essa informação já está na memória.")
                continue
            print(f"Memória salva em {cfg.MEMORY_PATH}.")
            continue

        if command.startswith("/forget "):
            raw_index = command[len("/forget "):].strip()
            try:
                index = int(raw_index)
            except ValueError:
                print("Uso: /forget número")
                continue
            if not 1 <= index <= len(memory):
                print("Número fora da memória.")
                continue
            removed = memory.pop(index - 1)
            state.save_memory(memory)
            print(f"Removido: {removed}")
            continue

        if command == "/sudo":
            executor.refresh_sudo()
            continue

        if command == "/sudo setup":
            executor.setup_passwordless_sudo()
            continue

        if command == "/auto on":
            cfg.AUTO_APPROVE = True
            print("Execução automática ativada para comandos permitidos.")
            continue

        if command == "/auto off":
            cfg.AUTO_APPROVE = False
            print("Confirmação ativada para todos os comandos.")
            continue

        if command in {"/self", "/self status"}:
            status = "ativa" if cfg.SELF_MODIFY else "desativada"
            print(f"Auto-programação: {status}.")
            continue

        if command == "/self on":
            cfg.SELF_MODIFY = True
            os.environ["AI_SELF_MODIFY"] = "1"
            added = state.add_memory_items(
                memory,
                ["usuário quer que o agente possa se auto-programar quando solicitado"],
                force=True,
            )
            for item in added:
                print(f"Memória atualizada: {item}")
            print("Auto-programação ativada nesta sessão.")
            continue

        if command == "/self off":
            cfg.SELF_MODIFY = False
            os.environ["AI_SELF_MODIFY"] = "0"
            print("Auto-programação desativada nesta sessão.")
            continue

        run_task(client, model, prompt_template, history, memory, text)


if __name__ == "__main__":
    raise SystemExit(main())
