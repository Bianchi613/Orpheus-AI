#!/usr/bin/env python3
"""
ai.py - Assistente de terminal com OpenAI ou Ollama + shell local.

Fluxo:
    você -> modelo pela API -> comando local -> resultado -> modelo

Requisitos:
    Python 3.10+
    Nenhuma biblioteca Python externa.
    Para IA local, instale e rode o Ollama separadamente.
    Para API externa, configure OPENAI_API_KEY no .env.

Configuração:
    Crie um arquivo .env ao lado deste script (veja .env.example).
    export AI_AUTO_APPROVE="1"             # opcional: executa comandos permitidos sem confirmar
    export AI_COMMAND_TIMEOUT="60"          # opcional: segundos por comando

Uso:
    python3 ai.py
    python3 ai.py --provider ollama "descubra por que o Docker não inicia"

Comandos internos:
    /new          limpa o contexto da conversa
    /where        mostra a pasta de trabalho
    /sudo         pede sua senha no terminal e renova a sessão sudo
    /sudo setup   configura sudo sem senha para comandos de manutenção
    /mem          mostra a memória salva
    /remember     salva uma informação na memória
    /forget       remove uma informação da memória pelo número
    /auto on      ativa execução automática dos comandos permitidos
    /auto off     exige confirmação para comandos que podem alterar o sistema
    /self         mostra ou alterna auto-programação do agente
    /status       mostra um painel simples de bordo do computador
    /board        mostra o painel principal do Orpheus
    /brief        mostra um resumo curto do estado atual
    /alerts       mostra apenas os alertas atuais com severidade
    /baseline     salva o estado atual como baseline local
    /compare      compara o estado atual com o baseline salvo
    /incident     resume o incidente/saúde atual e o próximo passo
    /health       executa checagens simples e mostra alertas
    /watch N      repete /status a cada N segundos
    /web URL      busca uma URL pública e resume o retorno
    /logs         mostra o caminho e as últimas linhas do log
    /logs tail N  mostra as últimas N linhas do log
    /logs grep X  filtra o log por texto
    /exit         encerra
"""

from __future__ import annotations

import os
import argparse
import json
import re
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

WORKDIR = Path.cwd().resolve()
SCRIPT_DIR = Path(__file__).resolve().parent
APP_VERSION = "2026.08.01.27"
APP_NAME = "Orpheus"

WEB_FETCH_TOOL = {
    "type": "function",
    "function": {
        "name": "fetch_url",
        "description": (
            "Busca uma URL na internet por HTTP/HTTPS e devolve status, headers e corpo em texto. "
            "Use para consultar documentação, páginas públicas e APIs públicas; nunca para ler segredos."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL HTTP ou HTTPS para consultar."},
                "method": {"type": "string", "enum": ["GET", "HEAD"], "description": "Método HTTP."},
                "max_bytes": {"type": "integer", "description": "Limite máximo do corpo retornado em bytes."}
            },
            "required": ["url"],
            "additionalProperties": False
        }
    }
}

RESET = "\033[0m"
BOLD = "\033[1m"
COLOR_USER = "\033[96m"
COLOR_AI = "\033[92m"
COLOR_INFO = "\033[93m"


def colorize(text: str, color: str) -> str:
    if not sys.stdout.isatty():
        return text
    return f"{color}{text}{RESET}"


def sanitize_log_text(text: str) -> str:
    cleaned = text.replace("\r", "")
    patterns = [
        re.compile(r"(?i)(authorization:\s*bearer\s+)[^\s]+"),
        re.compile(r"(?i)\b([A-Z0-9_]*(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)[A-Z0-9_]*)=([^\s]+)"),
    ]
    for pattern in patterns:
        cleaned = pattern.sub(lambda m: (m.group(1) if m.lastindex else "") + "[REDACTED]", cleaned)
    return cleaned

def append_log(event: str, message: str) -> None:
    try:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{timestamp}] {event}: {sanitize_log_text(message)}\n"
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(line)
    except OSError:
        pass


def read_log_tail(lines: int = 40) -> str:
    if not LOG_PATH.is_file():
        return f"Log ainda não existe: {LOG_PATH}"
    try:
        content = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return f"Não consegui ler o log em {LOG_PATH}"
    tail = content[-max(1, lines):]
    return "\n".join(tail) if tail else f"Log vazio: {LOG_PATH}"


def grep_log(term: str, limit: int = 80) -> str:
    if not LOG_PATH.is_file():
        return f"Log ainda não existe: {LOG_PATH}"
    try:
        content = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return f"Não consegui ler o log em {LOG_PATH}"
    needle = term.casefold()
    matches = [line for line in content if needle in line.casefold()]
    matches = matches[-max(1, limit):]
    return "\n".join(matches) if matches else f"Nenhuma linha encontrada para: {term}"


SHELL_TOOL = {
    "type": "function",
    "function": {
        "name": "run_shell",
        "description": (
            "Executa um comando no terminal Linux local e devolve stdout, stderr, "
            "código de saída e informação de timeout."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Comando shell a executar."}
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    },
}


class ApiClient:
    """Cliente HTTP mínimo para Chat Completions (OpenAI e Ollama)."""

    def __init__(self, base_url: str, api_key: str | None = None) -> None:
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.api_key = api_key

    def chat(self, model: str, messages: list[Any]) -> dict[str, Any]:
        payload = json.dumps(
            {
                "model": model,
                "messages": messages,
                "tools": [SHELL_TOOL, WEB_FETCH_TOOL],
                "tool_choice": "auto",
                "stream": False,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(self.url, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"não foi possível conectar a {self.url}: {exc.reason}") from exc


def load_dotenv(path: Path) -> None:
    """Carrega um .env simples sem sobrescrever variáveis já exportadas."""
    if not path.is_file():
        return
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            print(f"Aviso: linha {number} inválida em {path.name}; ignorada.", file=sys.stderr)
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            print(f"Aviso: nome inválido na linha {number} de {path.name}; ignorado.", file=sys.stderr)
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def load_memory() -> list[str]:
    if not MEMORY_PATH.is_file():
        return []

    try:
        data = json.loads(MEMORY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        print(f"Aviso: não consegui ler {MEMORY_PATH.name}; memória ignorada.", file=sys.stderr)
        return []

    if isinstance(data, dict):
        items = data.get("items", [])
    elif isinstance(data, list):
        items = data
    else:
        return []

    return [item.strip() for item in items if isinstance(item, str) and item.strip()]


def save_memory(items: list[str]) -> None:
    MEMORY_PATH.write_text(
        json.dumps({"items": items}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def normalize_memory_item(text: str) -> str:
    return " ".join(text.strip().split()).casefold()


def canonical_memory_key(text: str) -> str:
    cleaned = normalize_memory_item(text)
    for prefix in (
        "sistema do usuário:",
        "usuário do sistema:",
        "grupos do usuário:",
        "kernel do sistema:",
        "memória ram total:",
        "armazenamento atual do usuário:",
        "docker instalado:",
        "docker compose instalado:",
        "docker daemon funcionando:",
        "docker engine validado:",
        "bateria do notebook:",
        "cpu do sistema:",
        "gpu do sistema:",
        "wi-fi do sistema:",
    ):
        if cleaned.startswith(prefix):
            return prefix
    return cleaned


MEMORY_IMPORTANCE_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(prefere|preferencia|preferência|quer|gosta|evite|nunca|sempre)\b", re.I), "preferência"),
    (re.compile(r"\b(meu nome|me chama|pode me chamar|sou o)\b", re.I), "identidade"),
    (re.compile(r"\b(zorin|ubuntu|debian|fedora|arch|windows|docker|ollama|openai)\b", re.I), "ambiente"),
    (re.compile(r"\b(auto-program|autoprogram|self-modify|sudo|sem sudo|grupo docker)\b", re.I), "operação"),
    (re.compile(r"\b(sistema do usuário|usuário do sistema|grupo[s]? do usuário|kernel|cpu|processador|ram|memória|ssd|armazenamento|bateria|wifi|wi-fi|placa de rede|gpu)\b", re.I), "ambiente"),
]

MEMORY_IGNORE_PATTERNS = [
    re.compile(pattern, re.I)
    for pattern in (
        r"^oi\b",
        r"^e ai\b",
        r"^obrigad[oa]\b",
        r"^valeu\b",
        r"^teste\b",
        r"^ok\b$",
        r"^blz\b",
    )
]


def is_important_memory(text: str) -> bool:
    cleaned = " ".join(text.strip().split())
    if not cleaned or len(cleaned) < 8:
        return False
    if any(pattern.search(cleaned) for pattern in MEMORY_IGNORE_PATTERNS):
        return False
    return any(pattern.search(cleaned) for pattern, _tag in MEMORY_IMPORTANCE_RULES)


def classify_memory_item(text: str) -> str:
    for pattern, tag in MEMORY_IMPORTANCE_RULES:
        if pattern.search(text):
            return tag
    return "geral"


def memory_priority(item: str) -> tuple[int, str]:
    tag = classify_memory_item(item)
    order = {
        "identidade": 0,
        "preferência": 1,
        "ambiente": 2,
        "operação": 3,
        "geral": 4,
    }
    return (order.get(tag, 9), normalize_memory_item(item))


def select_relevant_memory(items: list[str], limit: int = 12) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for item in sorted(items, key=memory_priority):
        key = canonical_memory_key(item)
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
        if len(unique) >= limit:
            break
    return unique


def add_memory_items(memory: list[str], items: list[str], force: bool = False) -> list[str]:
    added: list[str] = []
    seen = {canonical_memory_key(item) for item in memory}

    for item in items:
        cleaned = " ".join(item.strip().split())
        if not force and not is_important_memory(cleaned):
            continue
        key = canonical_memory_key(cleaned)
        if key in seen:
            continue
        memory.append(cleaned)
        seen.add(key)
        added.append(cleaned)

    if added:
        memory[:] = sorted(memory, key=memory_priority)
        save_memory(memory)

    return added


def memory_text(items: list[str]) -> str:
    selected = select_relevant_memory(items)
    if not selected:
        return "Memória persistente: vazia."

    lines = ["Memória persistente do usuário mais relevante:"]
    lines.extend(f"- {item}" for item in selected)
    return "\n".join(lines)


def memory_system_message(items: list[str]) -> str | None:
    selected = select_relevant_memory(items)
    if not selected:
        return None

    lines = [
        "Fatos persistentes do usuário mais relevantes. Priorize preferências, identidade, ambiente e restrições operacionais nas decisões e respostas.",
    ]
    lines.extend(f"- {item}" for item in selected)
    return "\n".join(lines)


def self_modify_text() -> str:
    if SELF_MODIFY:
        return (
            "Auto-programação: ativa. Quando o usuário pedir para corrigir, melhorar "
            "ou revisar o próprio agente, você pode editar ai.py, .ai_prompt.txt, "
            ".env.example e documentação relacionada. Depois de mexer no código, "
            "valide com python3 -m py_compile ai.py antes de encerrar. Nunca leia, "
            "copie ou revele .env, chaves ou senhas."
        )

    return (
        "Auto-programação: desativada. Não edite o próprio agente; apenas diagnostique "
        "e explique o que mudaria."
    )


def infer_memory_from_user_text(text: str) -> list[str]:
    normalized = " ".join(text.strip().split())
    lowered = normalized.casefold()
    items: list[str] = []

    if (
        "aja direto no terminal" in lowered
        or "agir como agente" in lowered
        or "dar os comandos" in lowered
        or "ele nao orienta" in lowered
        or "ele não orienta" in lowered
    ):
        items.append("usuário prefere que eu aja direto no terminal e execute os comandos sozinho")

    if "auto se program" in lowered or "se auto program" in lowered or "auto-program" in lowered:
        items.append("usuário quer que o agente possa se auto-programar quando solicitado")

    if "ollama" in lowered:
        items.append("usuário quer usar Ollama como opção de modelo local")

    if "openai" in lowered or "api externa" in lowered:
        items.append("usuário quer a opção de usar API externa por .env")

    return items


def infer_memory_from_command_output(command: str, result: "CommandResult") -> list[str]:
    if result.exit_code != 0:
        return []

    text = "\n".join(part for part in [result.stdout, result.stderr] if part).strip()
    if not text:
        return []

    lowered_command = command.casefold()
    lowered_text = text.casefold()
    items: list[str] = []

    if "/etc/os-release" in lowered_command or "lsb_release" in lowered_command:
        pretty = re.search(r'(?m)^PRETTY_NAME="?([^"\n]+)"?$', text)
        if pretty:
            items.append(f"sistema do usuário: {pretty.group(1).strip()}")
        else:
            distro = re.search(r'(?m)^Distributor ID:\s*(.+)$', text)
            desc = re.search(r'(?m)^Description:\s*(.+)$', text)
            release = re.search(r'(?m)^Release:\s*(.+)$', text)
            if desc:
                items.append(f"sistema do usuário: {desc.group(1).strip()}")
            elif distro and release:
                items.append(
                    f"sistema do usuário: {distro.group(1).strip()} {release.group(1).strip()}"
                )

    if lowered_command.strip() == "whoami":
        user = text.splitlines()[0].strip()
        if user:
            items.append(f"usuário do sistema: {user}")

    if lowered_command.strip() == "groups":
        groups = " ".join(text.split())
        if groups:
            items.append(f"grupos do usuário: {groups}")
        if re.search(r"\bdocker\b", groups, re.I):
            items.append("usuário bianchi pertence ao grupo docker")

    if "uname" in lowered_command:
        kernel = " ".join(text.split())
        if kernel:
            items.append(f"kernel do sistema: {kernel}")

    if "free -h" in lowered_command:
        ram_match = re.search(r"Mem\.:\s+([^\s]+)", text)
        if ram_match:
            items.append(f"memória RAM total: {ram_match.group(1)}")

    if "lscpu" in lowered_command:
        model = re.search(r"(?m)^Model name:\s*(.+)$", text)
        if model:
            items.append(f"cpu do sistema: {model.group(1).strip()}")

    if "lsblk" in lowered_command and ("-d" in lowered_command or "-o name,model,size,type" in lowered_command):
        for line in text.splitlines():
            if " disk" in line or line.rstrip().endswith(" disk"):
                cleaned_line = " ".join(line.split())
                items.append(f"ssd do sistema: {cleaned_line}")
                break

    if "lspci" in lowered_command:
        for line in text.splitlines():
            if re.search(r"vga|3d|display|network", line, re.I):
                cleaned_line = " ".join(line.split())
                if re.search(r"vga|3d|display", line, re.I):
                    items.append(f"gpu do sistema: {cleaned_line}")
                if re.search(r"network", line, re.I):
                    items.append(f"wi-fi do sistema: {cleaned_line}")

    if "df -h /" in lowered_command or "df -h --output" in lowered_command:
        fields = text.splitlines()[-1].split()
        if len(fields) >= 4:
            items.append(f"armazenamento atual do usuário: {fields[2]} usados e {fields[3]} livres em {fields[1]}")

    if "docker" in lowered_command or "docker" in lowered_text:
        docker_version = re.search(r"Docker version\s+([0-9][^\s,]+)", text)
        if not docker_version:
            docker_version = re.search(r"(?m)^\s*Version:\s*([0-9][^\s]+)", text)
        if docker_version:
            items.append(f"Docker instalado: versão {docker_version.group(1).strip()}")

        compose_version = re.search(r"Docker Compose version\s+v?([0-9][^\s]+)", text)
        if compose_version:
            items.append(f"Docker Compose instalado: versão {compose_version.group(1).strip()}")

        client_server = re.search(r"client=([^\s]+)\s+server=([^\s]+)", text)
        if client_server:
            items.append(
                "Docker daemon funcionando: "
                f"client={client_server.group(1)} server={client_server.group(2)}"
            )

        engine_info = re.search(r"driver=([^\s]+)\s+cgroup=([^\s]+)\s+kernel=([^\n]+)", text)
        if engine_info:
            items.append(
                "Docker engine validado: "
                f"driver={engine_info.group(1)} cgroup={engine_info.group(2)} "
                f"kernel={engine_info.group(3).strip()}"
            )

        if re.search(r"(?m)^active\s*$", text) and re.search(r"(?m)^enabled\s*$", text):
            if "docker.socket" in lowered_command:
                items.append("Docker socket ativo e habilitado")
            else:
                items.append("Docker service ativo e habilitado")

        if "Active: active (running)" in text and "docker.service" in lowered_text:
            items.append("Docker service ativo e rodando")

        if re.search(r"(?m)^docker:x:\d+:.*\bbianchi\b", text) or re.search(
            r"(?m)\bbianchi\b.*\bdocker\b|\bdocker\b.*\bbianchi\b", text
        ):
            items.append("usuário bianchi pertence ao grupo docker")

        if "permission denied while trying to connect to the docker api" in lowered_text:
            items.append(
                "sessão atual pode precisar ser reaberta para usar Docker sem sudo "
                "após entrar no grupo docker"
            )

    return items


def shell_exists(command: str) -> bool:
    return subprocess.run(
        f"command -v {shlex.quote(command)} >/dev/null 2>&1",
        shell=True,
        executable="/bin/bash",
        cwd=str(WORKDIR),
        env=clean_child_environment(),
    ).returncode == 0


def read_text_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def collect_system_snapshot() -> dict[str, Any]:
    snapshot: dict[str, Any] = {}

    date_result = internal_read_command("date '+%F %T %Z'", min(DEFAULT_TIMEOUT, 10), 1000)
    snapshot["time"] = (date_result.stdout or date_result.stderr).strip() or "indisponível"

    commands = {
        "host": "hostname",
        "kernel": "uname -srmo",
        "uptime": "uptime -p",
        "load": "awk '{print $1, $2, $3}' /proc/loadavg",
        "cpu": "LC_ALL=C lscpu | grep -m1 'Model name:' | cut -d: -f2- | sed 's/^ *//'",
        "mem": "LANG=C free -h | awk '/^Mem:/ {print $2, $3, $7}' | tr -d '\r'",
        "root": "df -h / | awk 'NR==2 {print $3, $2, $5}' | tr -d '\r'",
        "ip": "hostname -I | awk '{print $1}'",
        "route": "ip route | grep '^default' | head -n1 | awk '{print $3}'",
        "wifi": "nmcli -t -f ACTIVE,SSID,SIGNAL dev wifi 2>/dev/null | grep '^yes:' | head -n1 | cut -d: -f2,3",
        "docker": "docker info --format '{{.ServerVersion}}' 2>/dev/null",
        "battery": 'for d in /sys/class/power_supply/BAT*; do [ -d "$d" ] || continue; cat "$d/capacity" 2>/dev/null; echo; cat "$d/status" 2>/dev/null; break; done',
    }

    for key, command in commands.items():
        result = internal_read_command(command, min(DEFAULT_TIMEOUT, 20), 4000)
        raw_value = (result.stdout or result.stderr).strip()
        if key == "mem" and raw_value and raw_value != "indisponível":
            parts = raw_value.split()
            if len(parts) >= 3:
                raw_value = f"{parts[0]} total, {parts[1]} usados, {parts[2]} disponiveis"
        elif key == "root" and raw_value and raw_value != "indisponível":
            parts = raw_value.split()
            if len(parts) >= 3:
                raw_value = f"{parts[0]} usados de {parts[1]} ({parts[2]})"
        elif key == "wifi" and raw_value and raw_value != "indisponível":
            parts = raw_value.split(":", 1)
            if len(parts) == 2:
                raw_value = f"{parts[0]} ({parts[1]})"
        elif key == "battery" and raw_value and raw_value != "indisponível":
            parts = [line.strip() for line in raw_value.splitlines() if line.strip()]
            if len(parts) >= 2:
                raw_value = f"{parts[0]}% ({parts[1]})"

        if result.exit_code != 0 and not raw_value:
            snapshot[key] = "indisponível"
        else:
            cleaned = raw_value.replace("REJEITADO PELO USUÁRIO: o comando não foi executado.", "").replace("REJEITADO PELO USUÁRIO", "").strip()
            snapshot[key] = cleaned or "indisponível"

    temp_value = "indisponível"
    thermal_base = Path('/sys/class/thermal')
    if thermal_base.exists():
        for zone in sorted(thermal_base.glob('thermal_zone*')):
            raw = read_text_file(zone / 'temp')
            if raw.isdigit():
                c = int(raw) / 1000
                if c > 0:
                    temp_value = f"{c:.1f}°C"
                    break
    snapshot["temp"] = temp_value

    recent_errors = internal_read_command("journalctl -p 3 -n 5 --no-pager 2>/dev/null", min(DEFAULT_TIMEOUT, 20), 4000)
    snapshot["recent_errors"] = (recent_errors.stdout or recent_errors.stderr).strip() or "nenhum erro crítico recente visível"
    return snapshot


def build_health_alerts(snapshot: dict[str, Any]) -> list[dict[str, str]]:
    alerts: list[dict[str, str]] = []

    def add_alert(severity: str, message: str) -> None:
        alerts.append({"severity": severity, "message": message})

    root = str(snapshot.get("root", ""))
    usage_match = re.search(r"\((\d+)%\)", root)
    if usage_match:
        usage = int(usage_match.group(1))
        if usage >= 92:
            add_alert("critico", f"disco raiz em uso crítico: {root}")
        elif usage >= 85:
            add_alert("atencao", f"disco raiz em uso alto: {root}")

    mem = str(snapshot.get("mem", ""))
    if "indisponível" in mem.casefold():
        add_alert("atencao", "não consegui ler o estado da memória RAM")

    route = str(snapshot.get("route", ""))
    if route in {"", "indisponível"}:
        add_alert("critico", "sem rota padrão de rede detectada")

    docker = str(snapshot.get("docker", ""))
    if docker in {"", "indisponível"}:
        add_alert("atencao", "docker não respondeu ao diagnóstico simples")

    battery = str(snapshot.get("battery", ""))
    battery_match = re.search(r"(\d+)%", battery)
    if battery_match and int(battery_match.group(1)) <= 20 and "Charging" not in battery:
        add_alert("atencao", f"bateria baixa: {battery}")

    temp = str(snapshot.get("temp", ""))
    temp_match = re.search(r"([0-9]+(?:\.[0-9]+)?)°C", temp)
    if temp_match and float(temp_match.group(1)) >= 85:
        add_alert("critico", f"temperatura alta: {temp}")

    recent_errors = str(snapshot.get("recent_errors", ""))
    if recent_errors and recent_errors != "nenhum erro crítico recente visível":
        first_line = recent_errors.splitlines()[0][:160]
        add_alert("atencao", f"há erros críticos recentes no journal: {first_line}")

    return alerts


def summarize_alerts(alerts: list[dict[str, str]]) -> str:
    if not alerts:
        return "sem alertas ativos"
    counts = {"critico": 0, "atencao": 0, "info": 0}
    for alert in alerts:
        severity = alert.get("severity", "info")
        counts[severity] = counts.get(severity, 0) + 1
    parts = []
    if counts.get("critico"):
        parts.append(f"{counts['critico']} crítico(s)")
    if counts.get("atencao"):
        parts.append(f"{counts['atencao']} atenção")
    if counts.get("info"):
        parts.append(f"{counts['info']} info")
    return ", ".join(parts) if parts else "sem alertas ativos"


def render_alerts(alerts: list[dict[str, str]]) -> str:
    if not alerts:
        return "Alertas: nenhum alerta simples no momento"
    lines = ["Alertas:"]
    for item in alerts:
        severity = item.get("severity", "info").upper()
        lines.append(f"- [{severity}] {item.get('message', '')}")
    return "\n".join(lines)


def render_status(snapshot: dict[str, Any], alerts: list[dict[str, str]]) -> str:
    lines = [
        "Painel de bordo do Orpheus",
        f"Hora:        {snapshot.get('time', 'indisponível')}",
        f"Host:        {snapshot.get('host', 'indisponível')}",
        f"Kernel:      {snapshot.get('kernel', 'indisponível')}",
        f"Uptime:      {snapshot.get('uptime', 'indisponível')}",
        f"Carga:       {snapshot.get('load', 'indisponível')}",
        f"CPU:         {snapshot.get('cpu', 'indisponível')}",
        f"Memória:     {snapshot.get('mem', 'indisponível')}",
        f"Disco /:     {snapshot.get('root', 'indisponível')}",
        f"Temperatura: {snapshot.get('temp', 'indisponível')}",
        f"IP local:    {snapshot.get('ip', 'indisponível')}",
        f"Gateway:     {snapshot.get('route', 'indisponível')}",
        f"Wi‑Fi:       {snapshot.get('wifi', 'indisponível')}",
        f"Docker:      {snapshot.get('docker', 'indisponível')}",
        f"Bateria:     {snapshot.get('battery', 'indisponível')}",
    ]
    lines.append(f"Resumo:      {summarize_alerts(alerts)}")
    lines.append(render_alerts(alerts))
    return "\n".join(lines)


def print_status_panel() -> tuple[dict[str, Any], list[str]]:
    snapshot = collect_system_snapshot()
    alerts = build_health_alerts(snapshot)
    print(render_status(snapshot, alerts))
    return snapshot, alerts


def run_watch(interval_seconds: int) -> None:
    interval = max(2, min(interval_seconds, 3600))
    print(f"Monitorando o painel a cada {interval} segundo(s). Ctrl+C para parar.")
    try:
        while True:
            print("c", end="")
            print_status_panel()
            print(f"\nAtualizando em {interval} segundo(s)...")
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nMonitoramento encerrado.")


def baseline_path() -> Path:
    return SCRIPT_DIR / ".orpheus_baseline.json"


def save_baseline(snapshot: dict[str, Any]) -> Path:
    path = baseline_path()
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def load_baseline() -> dict[str, Any] | None:
    path = baseline_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def compare_with_baseline(snapshot: dict[str, Any], baseline: dict[str, Any]) -> list[str]:
    keys = ["kernel", "uptime", "load", "mem", "root", "ip", "route", "wifi", "docker", "battery", "temp"]
    changes: list[str] = []
    for key in keys:
        before = str(baseline.get(key, "indisponível"))
        now = str(snapshot.get(key, "indisponível"))
        if before != now:
            changes.append(f"{key}: {before} -> {now}")
    return changes


def print_brief() -> None:
    snapshot = collect_system_snapshot()
    alerts = build_health_alerts(snapshot)
    print(f"Orpheus | host={snapshot.get('host', 'indisponível')} | uptime={snapshot.get('uptime', 'indisponível')} | carga={snapshot.get('load', 'indisponível')} | memória={snapshot.get('mem', 'indisponível')} | disco={snapshot.get('root', 'indisponível')} | docker={snapshot.get('docker', 'indisponível')} | alertas={summarize_alerts(alerts)}")


def print_alerts_only() -> None:
    snapshot = collect_system_snapshot()
    alerts = build_health_alerts(snapshot)
    print(render_alerts(alerts))


def print_incident_summary() -> None:
    snapshot = collect_system_snapshot()
    alerts = build_health_alerts(snapshot)
    print("Incidente / saúde atual")
    print(f"Host: {snapshot.get('host', 'indisponível')}")
    print(f"Resumo: {summarize_alerts(alerts)}")
    if alerts:
        top = alerts[0]
        print(f"Principal alerta: [{top.get('severity', 'info').upper()}] {top.get('message', '')}")
        print("Próximo passo: investigar ou validar o componente indicado no principal alerta.")
    else:
        print("Principal alerta: nenhum")
        print("Próximo passo: seguir em observação normal.")


def print_web_summary(url: str) -> None:
    append_log("web_request", url)
    result = fetch_url(url, "GET", 4000)
    if result.get("error"):
        append_log("web_error", str(result.get("error")))
        print(f"Falha na busca web: {result['error']}")
        return
    print(f"URL: {result.get('url')}")
    print(f"Status: {result.get('status')} {result.get('reason', '')}".rstrip())
    body = str(result.get("body", "")).strip()
    append_log("web_result", json.dumps({"status": result.get("status"), "url": result.get("url"), "bytes_returned": result.get("bytes_returned")}, ensure_ascii=False))
    if body:
        preview = " ".join(body.split())[:500]
        print(f"Prévia: {preview}")
    else:
        print("Prévia: resposta sem corpo")


def load_prompt_template() -> str:
    try:
        text = PROMPT_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        print(f"Erro: não consegui ler o prompt em {PROMPT_PATH}.", file=sys.stderr)
        raise SystemExit(1)

    if not text:
        print(f"Erro: o prompt {PROMPT_PATH} está vazio.", file=sys.stderr)
        raise SystemExit(1)

    return text


def render_prompt(template: str, memory: list[str]) -> str:
    return (
        template
        .replace("{{WORKDIR}}", str(WORKDIR))
        .replace("{{MEMORY}}", memory_text(memory))
        .replace("{{SELF_MODIFY}}", self_modify_text())
    )


# O .env da pasta atual tem precedência sobre o que fica junto do script.
if WORKDIR != Path(__file__).resolve().parent:
    load_dotenv(WORKDIR / ".env")
load_dotenv(Path(__file__).resolve().parent / ".env")

MEMORY_PATH = Path(os.environ.get("AI_MEMORY_FILE", SCRIPT_DIR / ".ai_memory.json")).expanduser()
LOG_PATH = Path(os.environ.get("AI_LOG_FILE", SCRIPT_DIR / "ai.log")).expanduser()

MAX_STEPS = int(os.environ.get("AI_MAX_STEPS", "30"))
DEFAULT_TIMEOUT = float(os.environ.get("AI_COMMAND_TIMEOUT", "60"))
MAX_CAPTURE = int(os.environ.get("AI_MAX_OUTPUT", "12000"))
AUTO_APPROVE = os.environ.get("AI_AUTO_APPROVE", "0") == "1"
SELF_MODIFY = os.environ.get("AI_SELF_MODIFY", "0") == "1"
PROMPT_PATH = Path(os.environ.get("AI_PROMPT_FILE", SCRIPT_DIR / ".ai_prompt.txt"))


# Bloqueios absolutos. Nem AI_AUTO_APPROVE=1 ignora esta lista.
BLOCK_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(^|[\s;&|])(?:doas|pkexec)(?:\s|$)", re.I),
     "elevação de privilégios não interativa é proibida"),
    (re.compile(r"(^|[\s;&|])su(?:\s|$)", re.I),
     "troca para usuário privilegiado é proibida"),

    (re.compile(r"(^|[\s;&|])(?:rm|unlink|shred|truncate)(?:\s|$)", re.I),
     "exclusão ou destruição de arquivos é proibida"),
    (re.compile(r"\bfind\b[^\n]*(?:-delete|-exec\s+(?:rm|unlink|shred)\b)", re.I),
     "exclusão por find é proibida"),
    (re.compile(r"\bgit\s+clean\b|\bgit\s+reset\s+--hard\b", re.I),
     "limpeza ou descarte destrutivo do Git é proibido"),

    (re.compile(r"\bmkfs(?:\.[a-z0-9_+-]+)?\b", re.I),
     "formatação de sistema de arquivos é proibida"),
    (re.compile(r"(^|[\s;&|])(?:fdisk|sfdisk|cfdisk|parted|wipefs|badblocks)(?:\s|$)", re.I),
     "alteração ou limpeza de disco é proibida"),
    (re.compile(r"(^|[\s;&|])dd(?:\s|$)[^\n]*\bof\s*=", re.I),
     "sobrescrita de dispositivo ou arquivo com dd é proibida"),
    (re.compile(r">\s*/dev/(?:sd|nvme|mmcblk|mapper/|disk/)", re.I),
     "gravação direta em dispositivo é proibida"),

    (re.compile(r"\b(?:apt|apt-get)\s+(?:remove|purge|autoremove)\b", re.I),
     "remoção de pacotes APT é proibida"),
    (re.compile(r"\bdpkg\s+(?:-r|--remove|-P|--purge)\b", re.I),
     "remoção de pacotes DPKG é proibida"),
    (re.compile(r"\b(?:dnf|yum|zypper)\s+(?:remove|erase)\b", re.I),
     "remoção de pacotes é proibida"),
    (re.compile(r"\bpacman\s+-R[a-z]*\b", re.I),
     "remoção de pacotes Pacman é proibida"),
    (re.compile(r"\b(?:snap\s+remove|flatpak\s+uninstall)\b", re.I),
     "desinstalação é proibida"),
    (re.compile(r"\b(?:pip|pip3)\s+uninstall\b", re.I),
     "desinstalação por pip é proibida"),
    (re.compile(r"\bpython(?:3(?:\.\d+)?)?\s+-m\s+pip\s+uninstall\b", re.I),
     "desinstalação por pip é proibida"),
    (re.compile(r"\b(?:npm\s+(?:uninstall|remove|rm)|yarn\s+remove|pnpm\s+(?:remove|uninstall))\b", re.I),
     "desinstalação de pacote JavaScript é proibida"),
    (re.compile(r"\b(?:gem|cargo)\s+uninstall\b", re.I),
     "desinstalação é proibida"),

    (re.compile(
        r"\b(?:docker|podman)\s+(?:rm|rmi|system\s+prune|container\s+prune|"
        r"image\s+prune|volume\s+prune|network\s+prune)\b", re.I
    ), "remoção ou prune de contêineres é proibido"),

    (re.compile(r"\bsystemctl\s+(?:disable|mask)\b", re.I),
     "desabilitar ou mascarar serviços é proibido"),
    (re.compile(r"(^|[\s;&|])(?:shutdown|reboot|poweroff|halt)(?:\s|$)", re.I),
     "desligamento ou reinicialização automática é proibida"),
    (re.compile(r"\b(?:userdel|groupdel|deluser|delgroup)\b", re.I),
     "remoção de usuários ou grupos é proibida"),

    # Impede formas óbvias de esconder comandos bloqueados.
    (re.compile(r"(^|[\s;&|])(?:bash|sh|zsh|fish)\s+-[a-z]*c(?:\s|$)", re.I),
     "shell aninhado com -c é proibido"),
    (re.compile(r"(^|[\s;&|])eval(?:\s|$)", re.I),
     "eval é proibido"),
    (re.compile(r"\bbase64\b[^\n]*(?:-d|--decode)[^\n]*(?:\||;)[^\n]*\b(?:sh|bash|zsh)\b", re.I),
     "execução de comando codificado é proibida"),
    (re.compile(r"\b(?:curl|wget)\b[^\n]*\|\s*(?:sh|bash|zsh)\b", re.I),
     "executar download diretamente em um shell é proibido"),

    # Proteção de segredos e do processo pai.
    (re.compile(r"/proc/(?:self|\d+|\$PPID)/environ", re.I),
     "leitura do ambiente de processos é proibida"),
    (re.compile(r"\b(?:cat|less|more|head|tail|sed|grep)\b[^\n]*(?:\.env\b|credentials|"
                r"\.aws/|\.ssh/|keyring|secrets?)", re.I),
     "leitura de credenciais ou segredos é proibida"),
]


# Comandos normalmente apenas informativos. Estes rodam sem confirmação.
READ_ONLY_PREFIXES = {
    "pwd", "ls", "dir", "stat", "file", "du", "df", "free", "uptime", "date",
    "whoami", "id", "groups", "uname", "hostname", "lscpu", "lsblk", "lspci",
    "lsusb", "ip", "ss", "netstat", "ping", "getent", "dig", "nslookup",
    "ps", "pgrep", "systemctl", "journalctl", "dmesg", "lpstat", "lpinfo",
    "docker", "podman", "git", "python", "python3", "pytest", "node", "npm",
    "ollama", "curl", "wget", "grep", "rg", "find", "locate", "which",
    "whereis", "type", "command", "readlink", "realpath", "cat", "head",
    "lsb_release", "dpkg", "apt", "apt-cache", "printf", "true",
    "tail", "less", "more", "wc", "sort", "uniq", "cut", "awk", "sed",
}

# Subcomandos que mudam estado mesmo quando o programa principal costuma ser informativo.
STATE_CHANGING_WORDS = re.compile(
    r"\b(?:start|stop|restart|reload|enable|install|upgrade|update|"
    r"create|build|run|exec|commit|push|pull|checkout|switch|merge|rebase|"
    r"add|write|edit|apply|set|replace|move|copy|touch|mkdir|chmod|chown|"
    r"kill|pkill|killall|tee)\b",
    re.I,
)


@dataclass
class CommandResult:
    stdout: str
    stderr: str
    exit_code: int | None
    timed_out: bool


def blocked_reason(command: str) -> str | None:
    normalized = command.strip()
    for pattern, reason in BLOCK_RULES:
        if pattern.search(normalized):
            return reason
    compound_reason = compound_state_change_reason(normalized)
    if compound_reason:
        return compound_reason
    return None


def first_program(command: str) -> str:
    """Tenta descobrir o primeiro programa real do comando."""
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return ""

    while tokens and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tokens[0]):
        tokens.pop(0)

    while tokens and Path(tokens[0]).name == "sudo":
        tokens.pop(0)
        while tokens and tokens[0].startswith("-"):
            tokens.pop(0)

    if not tokens:
        return ""

    program = Path(tokens[0]).name
    return program


def strip_discard_redirections(command: str) -> str:
    """Remove redirecionamentos que descartam saida; eles nao mudam o sistema."""
    cleaned = re.sub(r"\s+(?:[12])?>\s*/dev/null\b", "", command)
    cleaned = re.sub(r"\s+&>\s*/dev/null\b", "", cleaned)
    cleaned = re.sub(r"\s+2>&1\b", "", cleaned)
    return cleaned.strip()


def looks_read_only(command: str) -> bool:
    normalized = command.strip()
    if not normalized:
        return False

    segments = re.split(r"\s*(?:&&|;|\|\|)\s*", normalized)
    saw_real_read = False

    for segment in segments:
        piece = strip_discard_redirections(segment.strip())
        if not piece:
            continue
        if re.fullmatch(r"cd\s+.+", piece):
            continue
        if piece in {"true", ":"}:
            continue
        if piece in {". /etc/os-release", ". /usr/lib/os-release"}:
            continue

        program = first_program(piece)
        if program not in READ_ONLY_PREFIXES:
            return False
        if program == "apt" and not re.search(r"\bapt\s+list\b", piece, re.I):
            return False
        if STATE_CHANGING_WORDS.search(piece):
            return False
        if re.search(r"(?<![<])>{1,2}(?![>])", piece):
            return False
        saw_real_read = True

    return saw_real_read


def compound_state_change_reason(command: str) -> str | None:
    normalized = command.strip()
    if not normalized:
        return None

    if not re.search(r"\s(?:&&|;|\|\|)\s", normalized):
        return None

    lower = normalized.casefold()
    touches_apt_sources = any(
        marker in lower
        for marker in (
            "/etc/apt/sources.list",
            "/etc/apt/sources.list.d/",
            "/usr/share/keyrings/",
            "apt-key",
        )
    )
    has_apt_install = bool(
        re.search(r"\b(?:apt|apt-get)\s+(?:install|upgrade|full-upgrade|dist-upgrade)\b", lower)
    )
    if touches_apt_sources and has_apt_install:
        return (
            "alteração de repositório/chaves e instalação APT no mesmo comando é proibida; "
            "faça em etapas separadas"
        )

    apt_actions = re.findall(
        r"\b(?:apt|apt-get)\s+(?:install|upgrade|full-upgrade|dist-upgrade|remove|purge|autoremove)\b",
        lower,
    )
    if len(apt_actions) >= 2:
        return "múltiplas ações do APT no mesmo comando são proibidas; faça em etapas separadas"

    return None


def apt_simulation_command(command: str) -> str | None:
    stripped = command.strip()
    pattern = re.compile(
        r"^(?P<prefix>\s*(?:sudo\s+)?)"
        r"(?P<tool>apt-get|apt)"
        r"(?P<rest>\s+(?:install|upgrade|full-upgrade|dist-upgrade)\b.*)$",
        re.I,
    )
    match = pattern.match(stripped)
    if not match:
        return None

    prefix = match.group("prefix")
    tool = match.group("tool")
    rest = match.group("rest")

    simulated_rest = re.sub(r"(?<!\S)-y(?!\S)", "", rest)
    simulated_rest = re.sub(r"(?<!\S)--yes(?!\S)", "", simulated_rest)
    simulated_rest = re.sub(r"(?<!\S)-qq(?!\S)", "", simulated_rest)
    simulated_rest = re.sub(r"\s+", " ", simulated_rest).strip()

    base_tool = "apt-get" if tool.casefold() == "apt" else tool
    return f"{prefix}{base_tool} -s {simulated_rest}".strip()


def apt_simulation_commands(command: str) -> list[str]:
    segments = re.split(r"\s*(?:&&|;|\|\|)\s*", command.strip())
    simulations: list[str] = []
    for segment in segments:
        simulation = apt_simulation_command(segment)
        if simulation:
            simulations.append(simulation)
    return simulations


def apt_simulation_reveals_removal(text: str) -> bool:
    patterns = [
        r"(?m)^Os pacotes a seguir serão REMOVIDOS:",
        r"(?m)^The following packages will be REMOVED:",
        r"(?m)^Remv\b",
        r"(?m)^Remover\b",
    ]
    return any(re.search(pattern, text, re.I) for pattern in patterns)


def apt_plan_safety_reason(command: str) -> str | None:
    simulations = apt_simulation_commands(command)
    if not simulations:
        return None

    for simulation in simulations:
        try:
            completed = subprocess.run(
                simulation,
                shell=True,
                executable="/bin/bash",
                cwd=str(WORKDIR),
                env=clean_child_environment(),
                text=True,
                capture_output=True,
                timeout=min(DEFAULT_TIMEOUT, 45),
                errors="replace",
            )
        except Exception:
            continue

        combined = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
        if apt_simulation_reveals_removal(combined):
            return "o plano do APT removeria pacotes; isso é bloqueado"

    return None


def docker_status_failed(command: str, result: CommandResult) -> bool:
    normalized = " ".join(command.strip().split()).casefold()
    if "systemctl status docker" not in normalized:
        return False
    text = "\n".join(part for part in (result.stdout, result.stderr) if part)
    return bool(
        text and (
            "Active: failed" in text
            or "Failed to start docker.service" in text
            or "Result: exit-code" in text
        )
    )


def internal_read_command(command: str, timeout: float = 20, output_limit: int = 4000) -> CommandResult:
    append_log("internal_command", command)
    try:
        completed = subprocess.run(
            command,
            shell=True,
            executable="/bin/bash",
            cwd=str(WORKDIR),
            env=clean_child_environment(),
            text=True,
            capture_output=True,
            timeout=timeout,
            errors="replace",
        )
        result = CommandResult(
            clip(completed.stdout or "", output_limit),
            clip(completed.stderr or "", output_limit),
            completed.returncode,
            False,
        )
        append_log("internal_result", json.dumps({"exit_code": result.exit_code, "stdout": result.stdout, "stderr": result.stderr}, ensure_ascii=False))
        return result
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        result = CommandResult(clip(stdout, output_limit), clip(stderr, output_limit), None, True)
        append_log("internal_timeout", json.dumps({"stdout": result.stdout, "stderr": result.stderr}, ensure_ascii=False))
        return result
    except Exception as exc:
        result = CommandResult("", f"ERRO DO EXECUTOR: {type(exc).__name__}: {exc}", 1, False)
        append_log("internal_error", result.stderr)
        return result


def ask_approval(command: str) -> bool:
    global AUTO_APPROVE

    if AUTO_APPROVE or looks_read_only(command):
        return True

    print("\nA IA quer executar um comando que pode alterar o estado:")
    print(f"  {command}")
    while True:
        try:
            answer = input("Digite s para executar ou n para rejeitar [s/n]: ").strip().lower()
        except EOFError:
            print("Sem entrada interativa para confirmar; comando rejeitado.")
            return False
        if answer in {"s", "sim", "y", "yes"}:
            return True
        if answer in {"n", "nao", "não", "no"}:
            return False
        print("Resposta vazia ou inválida. Use s ou n.")


def command_uses_sudo(command: str) -> bool:
    return bool(re.search(r"(^|[\s;&|])sudo(?:\s|$)", command, re.I))


def sudo_is_ready() -> bool:
    try:
        completed = subprocess.run(
            ["sudo", "-n", "true"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return False
    return completed.returncode == 0


def needs_interactive_terminal(command: str) -> bool:
    """Comandos com senha/TTY não funcionam com stdout/stderr capturados."""
    return command_uses_sudo(command) and not sudo_is_ready()


SUDOERS_COMMANDS = [
    "/usr/bin/apt",
    "/usr/bin/apt-get",
    "/usr/bin/dpkg",
    "/usr/bin/systemctl",
    "/usr/bin/install",
    "/usr/bin/mkdir",
    "/usr/bin/chmod",
    "/usr/bin/chown",
    "/usr/bin/tee",
    "/usr/bin/cp",
    "/usr/bin/mv",
    "/usr/bin/curl",
    "/usr/sbin/usermod",
    "/usr/sbin/groupadd",
    "/usr/bin/gpasswd",
    "/usr/bin/docker",
]


def clean_child_environment() -> dict[str, str]:
    """Evita entregar chaves e tokens aos comandos executados."""
    env: dict[str, str] = {}
    sensitive = re.compile(
        r"(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|PRIVATE[_-]?KEY)",
        re.I,
    )

    for key, value in os.environ.items():
        if sensitive.search(key):
            continue
        env[key] = value

    return env


def clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text

    half = max(1, (limit - 100) // 2)
    removed = len(text) - (2 * half)
    return (
        text[:half]
        + f"\n\n... [{removed} caracteres omitidos] ...\n\n"
        + text[-half:]
    )


def run_command(command: str, timeout: float, output_limit: int) -> CommandResult:
    global WORKDIR

    append_log("command", command)

    reason = blocked_reason(command)
    if reason:
        message = f"BLOQUEADO PELO EXECUTOR: {reason}."
        print(f"\n⛔ {command}")
        print(f"   {message}")
        result = CommandResult("", message, 126, False)
        append_log("blocked", message)
        return result

    apt_reason = apt_plan_safety_reason(command)
    if apt_reason:
        message = f"BLOQUEADO PELO EXECUTOR: {apt_reason}."
        print(f"\n⛔ {command}")
        print(f"   {message}")
        return CommandResult("", message, 126, False)

    if not ask_approval(command):
        message = "REJEITADO PELO USUÁRIO: o comando não foi executado."
        print(f"   {message}")
        result = CommandResult("", message, 125, False)
        append_log("rejected", message)
        return result

    print(f"\n▶ {WORKDIR}$ {command}")

    try:
        if needs_interactive_terminal(command):
            completed = subprocess.run(
                command,
                shell=True,
                executable="/bin/bash",
                cwd=str(WORKDIR),
                env=clean_child_environment(),
                timeout=timeout,
            )
            print(f"[código de saída: {completed.returncode}]")
            result = CommandResult(
                "",
                "Comando interativo executado no terminal; saída não capturada pelo executor.",
                completed.returncode,
                False,
            )
            append_log("interactive_result", json.dumps({"exit_code": result.exit_code, "stderr": result.stderr}, ensure_ascii=False))
            return result

        completed = subprocess.run(
            command,
            shell=True,
            executable="/bin/bash",
            cwd=str(WORKDIR),
            env=clean_child_environment(),
            text=True,
            capture_output=True,
            timeout=timeout,
            errors="replace",
        )

        stdout = clip(completed.stdout or "", output_limit)
        stderr = clip(completed.stderr or "", output_limit)

        if stdout:
            print(stdout, end="" if stdout.endswith("\n") else "\n")
        if stderr:
            print(stderr, end="" if stderr.endswith("\n") else "\n", file=sys.stderr)

        print(f"[código de saída: {completed.returncode}]")
        result = CommandResult(stdout, stderr, completed.returncode, False)
        append_log("result", json.dumps({"exit_code": result.exit_code, "stdout": result.stdout, "stderr": result.stderr}, ensure_ascii=False))
        return result

    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        stdout = clip(stdout, output_limit)
        stderr = clip(stderr, output_limit)
        print(f"[tempo limite excedido após {timeout:.0f}s]")
        result = CommandResult(stdout, stderr, None, True)
        append_log("timeout", json.dumps({"stdout": result.stdout, "stderr": result.stderr}, ensure_ascii=False))
        return result

    except Exception as exc:
        message = f"ERRO DO EXECUTOR: {type(exc).__name__}: {exc}"
        print(message, file=sys.stderr)
        result = CommandResult("", message, 1, False)
        append_log("executor_error", message)
        return result


def get_attr(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def fetch_url(url: str, method: str = "GET", max_bytes: int = 12000) -> dict[str, Any]:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return {"error": "apenas URLs http e https são permitidas"}
    if not parsed.netloc:
        return {"error": "URL inválida"}

    try:
        limit = max(256, min(int(max_bytes), 200000))
    except (TypeError, ValueError):
        limit = 12000

    request = urllib.request.Request(
        url,
        method=(method or "GET").upper(),
        headers={
            "User-Agent": f"ai-terminal/{APP_VERSION}",
            "Accept": "text/plain, text/html, application/json, */*;q=0.1",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = b"" if request.get_method() == "HEAD" else response.read(limit + 1)
            truncated = len(body) > limit
            if truncated:
                body = body[:limit]
            charset = response.headers.get_content_charset() or "utf-8"
            text_body = body.decode(charset, errors="replace")
            return {
                "url": url,
                "method": request.get_method(),
                "status": getattr(response, "status", None),
                "reason": getattr(response, "reason", ""),
                "headers": dict(response.headers.items()),
                "body": text_body,
                "truncated": truncated,
                "bytes_returned": len(body),
            }
    except urllib.error.HTTPError as exc:
        body = exc.read(limit + 1)
        truncated = len(body) > limit
        if truncated:
            body = body[:limit]
        charset = exc.headers.get_content_charset() or "utf-8"
        return {
            "url": url,
            "method": request.get_method(),
            "status": exc.code,
            "reason": str(exc.reason),
            "headers": dict(exc.headers.items()),
            "body": body.decode(charset, errors="replace"),
            "truncated": truncated,
            "bytes_returned": len(body),
            "http_error": True,
        }
    except Exception as exc:
        return {
            "url": url,
            "method": request.get_method(),
            "error": f"{type(exc).__name__}: {exc}",
        }


def command_result_text(result: CommandResult) -> str:
    return json.dumps(
        {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
        },
        ensure_ascii=False,
    )


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
    normalized = text.strip()
    if not normalized:
        return False

    return any(pattern.search(normalized) for pattern in TOOL_NUDGE_PATTERNS)


def extract_safe_probe_command(text: str) -> str | None:
    if not text.strip():
        return None

    code_block = re.search(r"```(?:bash|sh)?\s*\n(.*?)```", text, re.I | re.S)
    if code_block:
        for raw_line in code_block.group(1).splitlines():
            candidate = raw_line.strip()
            if candidate and looks_read_only(candidate):
                return candidate

    for pattern in PROBE_COMMAND_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        candidate = match.group(0).strip()
        if looks_read_only(candidate):
            return candidate

    return None


def refresh_sudo() -> None:
    print("Vou pedir sua senha pelo sudo. Ela não será salva nem enviada para a IA.")
    try:
        completed = subprocess.run(["sudo", "-v"], cwd=str(WORKDIR))
    except FileNotFoundError:
        print("Erro: sudo não encontrado no sistema.")
        return

    if completed.returncode == 0:
        print("sudo autorizado por alguns minutos.")
    else:
        print(f"sudo não autorizado. Código de saída: {completed.returncode}")


def setup_passwordless_sudo() -> None:
    user = os.environ.get("USER") or os.environ.get("LOGNAME")
    if not user:
        print("Erro: não consegui descobrir o usuário atual.")
        return

    sudoers_path = "/etc/sudoers.d/bianchi-ai"
    commands = ", ".join(SUDOERS_COMMANDS)
    content = (
        "# Permite que o assistente de terminal use comandos de manutencao sem senha.\n"
        "# Criado pelo comando interno /sudo setup do ai.py.\n"
        f"{user} ALL=(ALL) NOPASSWD: {commands}\n"
    )

    print("Isso vai criar a regra:")
    print(f"  {sudoers_path}")
    print("Comandos liberados sem senha:")
    for command in SUDOERS_COMMANDS:
        print(f"  {command}")
    print("Sua senha sera pedida pelo sudo agora. Ela nao sera salva.")

    answer = input("Continuar? [s/N]: ").strip().lower()
    if answer not in {"s", "sim", "y", "yes"}:
        print("Cancelado.")
        return

    tmp_name = ""
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as tmp:
            tmp.write(content)
            tmp_name = tmp.name

        check = subprocess.run(["visudo", "-cf", tmp_name])
        if check.returncode != 0:
            print("Erro: a regra gerada nao passou na validacao do visudo.")
            return

        install = subprocess.run(
            ["sudo", "install", "-o", "root", "-g", "root", "-m", "0440", tmp_name, sudoers_path],
            cwd=str(WORKDIR),
        )
        if install.returncode != 0:
            print(f"Erro: nao consegui instalar a regra sudoers. Codigo: {install.returncode}")
            return

        final_check = subprocess.run(["sudo", "visudo", "-cf", sudoers_path])
        if final_check.returncode != 0:
            print("Aviso: a regra foi escrita, mas a validacao final falhou.")
            return

        print("Pronto: sudo sem senha configurado para os comandos listados.")
    except FileNotFoundError as exc:
        print(f"Erro: comando necessario nao encontrado: {exc.filename}")
    finally:
        if tmp_name:
            try:
                Path(tmp_name).unlink()
            except OSError:
                pass


def run_task(
    client: ApiClient,
    model: str,
    prompt_template: str,
    history: list[Any],
    memory: list[str],
    user_text: str,
) -> None:
    append_log("user", user_text)
    history.append({"role": "user", "content": user_text})
    remembered_from_user = add_memory_items(memory, infer_memory_from_user_text(user_text))
    for item in remembered_from_user:
        print(f"Memória atualizada: {item}")
    tool_nudges = 0
    summary_nudges = 0

    for _step in range(1, MAX_STEPS + 1):
        try:
            messages = [
                {
                    "role": "system",
                    "content": render_prompt(prompt_template, memory),
                },
            ]
            memory_message = memory_system_message(memory)
            if memory_message:
                messages.append({"role": "system", "content": memory_message})
            messages.extend(history)
            response = client.chat(model, messages)
            append_log("api_response", json.dumps(response, ensure_ascii=False)[:4000])
        except KeyboardInterrupt:
            print("\nInterrompido pelo usuário.")
            return
        except Exception as exc:
            append_log("api_error", f"{type(exc).__name__}: {exc}")
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
            final_text = (message.get("content") or "").strip()
            if not final_text and summary_nudges < 2:
                summary_nudges += 1
                history.append(
                    {
                        "role": "user",
                        "content": (
                            "Agora responda em texto curto, em português, dizendo "
                            "o que você encontrou, o que fez e se a tarefa terminou."
                        ),
                    }
                )
                continue
            probe_command = extract_safe_probe_command(final_text)
            if probe_command:
                result = run_command(probe_command, DEFAULT_TIMEOUT, MAX_CAPTURE)
                remembered_from_command = add_memory_items(
                    memory,
                    infer_memory_from_command_output(probe_command, result),
                )
                for item in remembered_from_command:
                    print(f"Memória atualizada: {item}")
                history.append(
                    {
                        "role": "user",
                        "content": (
                            f'O comando de diagnóstico "{probe_command}" foi executado '
                            f'automaticamente. Resultado JSON: {command_result_text(result)}. '
                            "Continue a tarefa sem me pedir para rodar comandos."
                        ),
                    }
                )
                tool_nudges += 1
                continue
            if should_nudge_tool_use(final_text) and tool_nudges < 4:
                tool_nudges += 1
                history.append(
                    {
                        "role": "user",
                        "content": (
                            "Você tem acesso direto à ferramenta run_shell. "
                            "Não peça para eu rodar comandos que você mesmo pode executar. "
                            "Se precisar investigar, instalar, validar ou verificar algo, "
                            "chame a ferramenta agora e continue a tarefa."
                        ),
                    }
                )
                continue
            print(colorize(f"\nIA: {final_text or 'tarefa encerrada sem resposta textual.'}", COLOR_AI))
            return

        should_prompt_validation = False
        should_prompt_docker_journal = False
        for call in tool_calls:
            function = call.get("function") or {}
            result: CommandResult | None = None
            command = ""
            name = function.get("name")
            try:
                raw_args = function.get("arguments") or "{}"
                args = raw_args if isinstance(raw_args, dict) else json.loads(raw_args)
            except json.JSONDecodeError as exc:
                args = None
                result_text = json.dumps({"error": f"argumentos inválidos: {exc}"})
            else:
                if name == "run_shell":
                    try:
                        command = args.get("command")
                        if not isinstance(command, str) or not command.strip():
                            raise ValueError("o campo command deve ser uma string não vazia")
                        result = run_command(command, DEFAULT_TIMEOUT, MAX_CAPTURE)
                        remembered_from_command = add_memory_items(
                            memory,
                            infer_memory_from_command_output(command, result),
                        )
                        for item in remembered_from_command:
                            print(f"Memória atualizada: {item}")
                        if docker_status_failed(command, result):
                            should_prompt_docker_journal = True
                        result_text = command_result_text(result)
                    except ValueError as exc:
                        result_text = json.dumps({"error": f"argumentos inválidos: {exc}"})
                elif name == "fetch_url":
                    url = args.get("url")
                    method = args.get("method", "GET")
                    max_bytes = args.get("max_bytes", 12000)
                    if not isinstance(url, str) or not url.strip():
                        result_text = json.dumps({"error": "o campo url deve ser uma string não vazia"})
                    else:
                        result_text = json.dumps(
                            fetch_url(url.strip(), str(method), max_bytes),
                            ensure_ascii=False,
                        )
                else:
                    result_text = json.dumps({"error": "ferramenta desconhecida"})

            history.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id", function.get("name", "run_shell")),
                    "content": result_text,
                }
            )
            if result and result.exit_code == 0 and not looks_read_only(command):
                should_prompt_validation = True

        if should_prompt_docker_journal:
            history.append(
                {
                    "role": "user",
                    "content": (
                        "O status do Docker falhou. Investigue agora com "
                        "journalctl -u docker -n 100 --no-pager, identifique a causa "
                        "e continue sozinho."
                    ),
                }
            )
            continue

        if should_prompt_validation:
            history.append(
                {
                    "role": "user",
                    "content": (
                        "Continue sozinho. Se você acabou de instalar, configurar ou alterar "
                        "algo, valide agora com comandos objetivos antes de encerrar e depois "
                        "resuma o resultado em português."
                    ),
                }
            )

    print(f"\nInterrompido: o limite de {MAX_STEPS} ciclos foi atingido.")


def print_banner(provider: str, model: str, memory_count: int) -> None:
    mode = "automático" if AUTO_APPROVE else "confirma mudanças"
    self_mode = "ativa" if SELF_MODIFY else "desativada"
    print(colorize(APP_NAME, BOLD + COLOR_INFO))
    print(f"Versão:   {APP_VERSION}")
    print(f"Provedor: {provider}")
    print(f"Modelo:   {model}")
    print(f"Pasta:  {WORKDIR}")
    print(f"Modo:   {mode}")
    print(f"Prompt:  {PROMPT_PATH}")
    print("Bloqueios permanentes: exclusão, desinstalação e formatação.")
    print("sudo: permitido somente com confirmação e senha no terminal.")
    print(f"Auto-programação: {self_mode}. Use /self on, /self off ou /self status.")
    print("Comandos de bordo: /board, /brief, /alerts, /baseline, /compare, /incident.")
    print(f"Memória: {memory_count} item(ns) salvo(s). Use /mem, /remember e /forget.")
    print("Digite /exit para sair.\n")


def main() -> int:
    global AUTO_APPROVE, SELF_MODIFY

    parser = argparse.ArgumentParser(description="Orpheus - computador de bordo inteligente para Linux")
    parser.add_argument("--provider", choices=("openai", "ollama"))
    parser.add_argument("--model")
    parser.add_argument("pedido", nargs="*")
    parser.add_argument("--self-modify", choices=("on", "off"))
    args = parser.parse_args()

    provider = (args.provider or os.environ.get("AI_PROVIDER", "openai")).lower()
    if provider == "ollama":
        model = args.model or os.environ.get("OLLAMA_MODEL", "qwen3:8b")
        base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1")
        client = ApiClient(base_url)
    else:
        model = args.model or os.environ.get("OPENAI_MODEL", "gpt-5.6")
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            print("Erro: OPENAI_API_KEY não está definida no ambiente ou no arquivo .env.")
            return 1
        client = ApiClient("https://api.openai.com/v1", api_key)
        os.environ.pop("OPENAI_API_KEY", None)
        del api_key

    if args.self_modify == "on":
        SELF_MODIFY = True
    elif args.self_modify == "off":
        SELF_MODIFY = False

    prompt_template = load_prompt_template()
    history: list[Any] = []
    memory = load_memory()
    print_banner(provider, model, len(memory))

    initial = " ".join(args.pedido).strip()
    if initial:
        run_task(client, model, prompt_template, history, memory, initial)

    while True:
        try:
            text = input(colorize("\nVocê: ", COLOR_USER)).strip()
        except (EOFError, KeyboardInterrupt):
            print("\nEncerrado.")
            return 0

        if not text:
            continue

        command = text.lower()

        if command in {"/exit", "/quit", "exit", "quit"}:
            print("Encerrado.")
            return 0

        if command in {"/status", "/board"}:
            print_status_panel()
            continue

        if command == "/brief":
            print_brief()
            continue

        if command == "/alerts":
            print_alerts_only()
            continue

        if command == "/baseline":
            snapshot = collect_system_snapshot()
            path = save_baseline(snapshot)
            print(f"Baseline salva em {path}")
            continue

        if command == "/compare":
            snapshot = collect_system_snapshot()
            baseline = load_baseline()
            if not baseline:
                print("Nenhum baseline salvo ainda. Use /baseline primeiro.")
                continue
            changes = compare_with_baseline(snapshot, baseline)
            if changes:
                print("Mudanças desde o baseline:")
                for item in changes:
                    print(f"- {item}")
            else:
                print("Nenhuma mudança detectada nos campos monitorados.")
            continue

        if command == "/incident":
            print_incident_summary()
            continue

        if command == "/health":
            _snapshot, alerts = print_status_panel()
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
            run_watch(interval)
            continue

        if command.startswith("/web "):
            url = text[len("/web "):].strip()
            if not url:
                print("Uso: /web URL")
                continue
            print_web_summary(url)
            continue

        if command == "/logs":
            print(f"Log: {LOG_PATH}")
            print(read_log_tail(40))
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
            print(f"Log: {LOG_PATH}")
            print(read_log_tail(lines))
            continue

        if command.startswith("/logs grep "):
            term = text[len("/logs grep "):].strip()
            if not term:
                print("Uso: /logs grep TEXTO")
                continue
            print(f"Log: {LOG_PATH}")
            print(grep_log(term))
            continue

        if command == "/new":
            history.clear()
            print("Contexto limpo.")
            continue

        if command == "/where":
            print(WORKDIR)
            continue

        if command == "/mem":
            if not memory:
                print("Memória vazia.")
            else:
                for index, item in enumerate(memory, 1):
                    print(f"{index}. {item}")
            continue

        if command.startswith("/remember "):
            item = text[len("/remember "):].strip()
            if not item:
                print("Uso: /remember informação para salvar")
                continue
            added = add_memory_items(memory, [item], force=True)
            if not added:
                print("Essa informação já está na memória.")
                continue
            print(f"Memória salva em {MEMORY_PATH}.")
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
            save_memory(memory)
            print(f"Removido: {removed}")
            continue

        if command == "/sudo":
            refresh_sudo()
            continue

        if command == "/sudo setup":
            setup_passwordless_sudo()
            continue

        if command == "/auto on":
            AUTO_APPROVE = True
            print("Execução automática ativada para comandos permitidos.")
            continue

        if command == "/auto off":
            AUTO_APPROVE = False
            print("Confirmação ativada para comandos que podem alterar o estado.")
            continue

        if command in {"/self", "/self status"}:
            status = "ativa" if SELF_MODIFY else "desativada"
            print(f"Auto-programação: {status}.")
            continue

        if command == "/self on":
            SELF_MODIFY = True
            os.environ["AI_SELF_MODIFY"] = "1"
            added = add_memory_items(
                memory,
                ["usuário quer que o agente possa se auto-programar quando solicitado"],
                force=True,
            )
            for item in added:
                print(f"Memória atualizada: {item}")
            print("Auto-programação ativada nesta sessão.")
            continue

        if command == "/self off":
            SELF_MODIFY = False
            os.environ["AI_SELF_MODIFY"] = "0"
            print("Auto-programação desativada nesta sessão.")
            continue

        run_task(client, model, prompt_template, history, memory, text)


if __name__ == "__main__":
    raise SystemExit(main())
