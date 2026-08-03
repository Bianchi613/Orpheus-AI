"""Estado local, memória e painel textual do Orpheus.

Este módulo responde: o que o agente sabe, grava e monitora localmente?
Ele concentra memória, log, status vivo, baseline e painel de saúde no terminal.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import config as cfg


def _internal_read_command(command: str, timeout: float = 20, output_limit: int = 4000):
    """Importação tardia evita ciclo entre state.py e executor.py."""
    from executor import internal_read_command

    return internal_read_command(command, timeout, output_limit)


def _clean_child_environment() -> dict[str, str]:
    """Importação tardia do ambiente limpo usado por diagnósticos internos."""
    from executor import clean_child_environment

    return clean_child_environment()


def _fetch_url(url: str, method: str = "GET", max_bytes: int = 12000) -> dict[str, Any]:
    """Importação tardia evita ciclo entre state.py e api.py."""
    from api import fetch_url

    return fetch_url(url, method, max_bytes)

def sanitize_log_text(text: str) -> str:
    """Remove segredos óbvios antes de escrever qualquer coisa no log local."""
    cleaned = text.replace("\r", "")
    patterns = [
        re.compile(r"(?i)(authorization:\s*bearer\s+)[^\s]+"),
        re.compile(r"(?i)\b([A-Z0-9_]*(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)[A-Z0-9_]*)=([^\s]+)"),
    ]
    for pattern in patterns:
        cleaned = pattern.sub(lambda m: (m.group(1) if m.lastindex else "") + "[REDACTED]", cleaned)
    return cleaned

def append_log(event: str, message: str) -> None:
    """Grava eventos do agente em ai.log sem parar o programa se o log falhar."""
    try:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{timestamp}] {event}: {sanitize_log_text(message)}\n"
        cfg.LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with cfg.LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(line)
    except OSError:
        pass


def read_log_tail(lines: int = 40) -> str:
    """Lê as últimas linhas do log para o comando interno /logs."""
    if not cfg.LOG_PATH.is_file():
        return f"Log ainda não existe: {cfg.LOG_PATH}"
    try:
        content = cfg.LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return f"Não consegui ler o log em {cfg.LOG_PATH}"
    tail = content[-max(1, lines):]
    return "\n".join(tail) if tail else f"Log vazio: {cfg.LOG_PATH}"


def grep_log(term: str, limit: int = 80) -> str:
    """Procura texto no log local para o comando interno /logs grep."""
    if not cfg.LOG_PATH.is_file():
        return f"Log ainda não existe: {cfg.LOG_PATH}"
    try:
        content = cfg.LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return f"Não consegui ler o log em {cfg.LOG_PATH}"
    needle = term.casefold()
    matches = [line for line in content if needle in line.casefold()]
    matches = matches[-max(1, limit):]
    return "\n".join(matches) if matches else f"Nenhuma linha encontrada para: {term}"

def write_status(state: str, summary: str, **extra: Any) -> None:
    """Atualiza o arquivo vivo ai.status.json usado pelo /monitor."""
    payload = {
        "app": cfg.APP_NAME,
        "version": cfg.APP_VERSION,
        "session_id": cfg.SESSION_ID,
        "task_id": cfg.CURRENT_TASK_ID,
        "pid": os.getpid(),
        "cwd": str(cfg.WORKDIR),
        "state": state,
        "summary": summary,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "updated_ts": time.time(),
    }
    payload.update(extra)
    try:
        cfg.STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        cfg.STATE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass


def read_status() -> dict[str, Any] | None:
    """Carrega o último status vivo gravado pelo agente."""
    if not cfg.STATE_PATH.is_file():
        return None
    try:
        return json.loads(cfg.STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def ensure_local_files() -> None:
    """Cria arquivos locais básicos na primeira execução real do agente.

    Esses arquivos são privados, ignorados pelo git e pertencem à máquina onde
    o Orpheus está rodando. A função não sobrescreve dados existentes.
    """
    try:
        cfg.MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        if not cfg.MEMORY_PATH.exists():
            cfg.MEMORY_PATH.write_text(
                json.dumps({"items": []}, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
    except OSError:
        pass

    try:
        cfg.LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        cfg.LOG_PATH.touch(exist_ok=True)
    except OSError:
        pass


def print_monitor() -> None:
    """Mostra uma leitura humana do ai.status.json."""
    status = read_status()
    if not status:
        print(f"Status ainda não existe: {cfg.STATE_PATH}")
        return
    print(f"Status: {cfg.STATE_PATH}")
    for key in ("app", "version", "session_id", "task_id", "pid", "cwd", "state", "summary", "updated_at"):
        print(f"{key}: {status.get(key, 'indisponível')}")
    age = time.time() - float(status.get("updated_ts", 0) or 0)
    print(f"idade_do_heartbeat: {age:.1f}s")
    for key in ("step", "command", "provider", "model", "wait_seconds", "remaining_seconds", "last_error"):
        if key in status:
            print(f"{key}: {status[key]}")


def watch_monitor(interval: int) -> None:
    """Reexibe o monitor em loop, útil para acompanhar outra janela do agente."""
    interval = max(1, interval)
    try:
        while True:
            os.system("clear")
            print_monitor()
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nMonitor interrompido.")

def load_memory() -> list[str]:
    """Lê a memória persistente local, se existir."""
    if not cfg.MEMORY_PATH.is_file():
        return []

    try:
        data = json.loads(cfg.MEMORY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        print(f"Aviso: não consegui ler {cfg.MEMORY_PATH.name}; memória ignorada.", file=sys.stderr)
        return []

    if isinstance(data, dict):
        items = data.get("items", [])
    elif isinstance(data, list):
        items = data
    else:
        return []

    return [item.strip() for item in items if isinstance(item, str) and item.strip()]


def save_memory(items: list[str]) -> None:
    """Salva a memória em JSON local ignorado pelo git."""
    cfg.MEMORY_PATH.write_text(
        json.dumps({"items": items}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def normalize_memory_item(text: str) -> str:
    """Normaliza texto para comparar memórias sem duplicar por maiúsculas/espaços."""
    return " ".join(text.strip().split()).casefold()


def canonical_memory_key(text: str) -> str:
    """Gera uma chave estável para substituir fatos duradouros do mesmo tipo."""
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


# Regras simples para decidir o que vale salvar: preferências, identidade,
# ambiente da máquina e escolhas operacionais do usuário.
MEMORY_IMPORTANCE_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(prefere|preferencia|preferência|quer|gosta|evite|nunca|sempre)\b", re.I), "preferência"),
    (re.compile(r"\b(meu nome|me chama|pode me chamar|sou o)\b", re.I), "identidade"),
    (re.compile(r"\b(zorin|ubuntu|debian|fedora|arch|windows|docker|ollama|openai)\b", re.I), "ambiente"),
    (re.compile(r"\b(auto-program|autoprogram|self-modify|sudo|sem sudo|grupo docker)\b", re.I), "operação"),
    (re.compile(r"\b(sistema do usuário|usuário do sistema|grupo[s]? do usuário|kernel|cpu|processador|ram|memória|ssd|armazenamento|bateria|wifi|wi-fi|placa de rede|gpu)\b", re.I), "ambiente"),
]

# Frases curtas de conversa comum não devem virar memória permanente.
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
    """Decide se um texto é importante o bastante para entrar na memória."""
    cleaned = " ".join(text.strip().split())
    if not cleaned or len(cleaned) < 8:
        return False
    if is_invalid_memory_item(cleaned):
        return False
    if any(pattern.search(cleaned) for pattern in MEMORY_IGNORE_PATTERNS):
        return False
    return any(pattern.search(cleaned) for pattern, _tag in MEMORY_IMPORTANCE_RULES)


def is_invalid_memory_item(text: str) -> bool:
    """Bloqueia fatos persistentes que parecem template/código quebrado."""
    return bool(
        re.search(r"\{[^{}]*\}", text)
        or re.search(r"\b[a-zA-Z_][a-zA-Z0-9_]*\.group\(\d+\)", text)
    )


def classify_memory_item(text: str) -> str:
    """Classifica memória para ordenar o que entra primeiro no prompt."""
    for pattern, tag in MEMORY_IMPORTANCE_RULES:
        if pattern.search(text):
            return tag
    return "geral"


def memory_priority(item: str) -> tuple[int, str]:
    """Dá prioridade para identidade/preferências antes de fatos gerais."""
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
    """Escolhe poucas memórias relevantes para não lotar o prompt do modelo."""
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
    """Adiciona ou substitui memórias novas, evita duplicatas e salva no disco."""
    added: list[str] = []
    changed = False
    index_by_key = {canonical_memory_key(item): idx for idx, item in enumerate(memory)}

    for item in items:
        cleaned = " ".join(item.strip().split())
        if is_invalid_memory_item(cleaned):
            continue
        if not force and not is_important_memory(cleaned):
            continue
        key = canonical_memory_key(cleaned)
        existing_index = index_by_key.get(key)
        if existing_index is not None:
            if normalize_memory_item(memory[existing_index]) == normalize_memory_item(cleaned):
                continue
            memory[existing_index] = cleaned
            added.append(cleaned)
            changed = True
            continue
        memory.append(cleaned)
        index_by_key[key] = len(memory) - 1
        added.append(cleaned)
        changed = True

    if changed:
        memory[:] = sorted(memory, key=memory_priority)
        save_memory(memory)

    return added


def memory_text(items: list[str]) -> str:
    """Renderiza memória dentro do prompt externo .ai_prompt.txt."""
    selected = select_relevant_memory(items)
    if not selected:
        return "Memória persistente: vazia."

    lines = ["Memória persistente do usuário mais relevante:"]
    lines.extend(f"- {item}" for item in selected)
    return "\n".join(lines)


def memory_system_message(items: list[str]) -> str | None:
    """Cria uma mensagem system extra reforçando memórias importantes."""
    selected = select_relevant_memory(items)
    if not selected:
        return None

    lines = [
        "Fatos persistentes do usuário mais relevantes. Priorize preferências, identidade, ambiente e restrições operacionais nas decisões e respostas.",
    ]
    lines.extend(f"- {item}" for item in selected)
    return "\n".join(lines)

def infer_memory_from_user_text(text: str) -> list[str]:
    """Extrai memórias a partir do que o usuário escreve na conversa."""
    normalized = " ".join(text.strip().split())
    lowered = normalized.casefold()
    items: list[str] = []

    if any(phrase in lowered for phrase in (
        "está funcionando",
        "esta funcionando",
        "ta funcionando",
        "tá funcionando",
        "funciona?",
        "está funcinando",
        "esta funcinando",
    )):
        items.append("quando o usuário perguntar se algo está funcionando sem contexto explícito, devo pedir clarificação antes de assumir o alvo")

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
    """Extrai memórias técnicas a partir de comandos bem-sucedidos."""
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
    """Confere se um comando existe no PATH sem imprimir nada."""
    return subprocess.run(
        f"command -v {shlex.quote(command)} >/dev/null 2>&1",
        shell=True,
        executable="/bin/bash",
        cwd=str(cfg.WORKDIR),
        env=_clean_child_environment(),
    ).returncode == 0


def read_text_file(path: Path) -> str:
    """Lê arquivo pequeno de diagnóstico; falha vira string vazia."""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _format_snapshot_value(key: str, raw_value: str) -> str:
    """Normaliza campos do snapshot para uma apresentação curta e estável."""
    if key == "mem" and raw_value and raw_value != "indisponível":
        parts = raw_value.split()
        if len(parts) >= 3:
            return f"{parts[0]} total, {parts[1]} usados, {parts[2]} disponiveis"
    elif key == "root" and raw_value and raw_value != "indisponível":
        parts = raw_value.split()
        if len(parts) >= 3:
            return f"{parts[0]} usados de {parts[1]} ({parts[2]})"
    elif key == "wifi" and raw_value and raw_value != "indisponível":
        parts = raw_value.split(":", 1)
        if len(parts) == 2:
            return f"{parts[0]} ({parts[1]})"
    elif key == "battery" and raw_value and raw_value != "indisponível":
        parts = [line.strip() for line in raw_value.splitlines() if line.strip()]
        if len(parts) >= 2:
            return f"{parts[0]}% ({parts[1]})"
    return raw_value



def _clean_snapshot_value(raw_value: str) -> str:
    """Remove marcadores locais de rejeição antes de mostrar no painel."""
    return raw_value.replace(
        "REJEITADO PELO USUÁRIO: o comando não foi executado.",
        "",
    ).replace("REJEITADO PELO USUÁRIO", "").strip()



def collect_system_snapshot() -> dict[str, Any]:
    """Coleta um retrato rápido do computador para /status, /board e baseline."""
    snapshot: dict[str, Any] = {}

    date_result = _internal_read_command("date '+%F %T %Z'", min(cfg.DEFAULT_TIMEOUT, 10), 1000)
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
        "docker": "command -v docker >/dev/null 2>&1 && docker --version 2>/dev/null | awk '{print $3}' | tr -d ','",
        "battery": 'for d in /sys/class/power_supply/BAT*; do [ -d "$d" ] || continue; cat "$d/capacity" 2>/dev/null; echo; cat "$d/status" 2>/dev/null; break; done',
    }

    for key, command in commands.items():
        result = _internal_read_command(command, min(cfg.DEFAULT_TIMEOUT, 20), 4000)
        raw_value = (result.stdout or result.stderr).strip()
        formatted_value = _format_snapshot_value(key, raw_value)

        if result.exit_code != 0 and not formatted_value:
            snapshot[key] = "indisponível"
        else:
            cleaned = _clean_snapshot_value(formatted_value)
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

    recent_errors = _internal_read_command("journalctl -p 3 -n 5 --no-pager 2>/dev/null", min(cfg.DEFAULT_TIMEOUT, 20), 4000)
    snapshot["recent_errors"] = (recent_errors.stdout or recent_errors.stderr).strip() or "nenhum erro crítico recente visível"
    return snapshot


def build_health_alerts(snapshot: dict[str, Any]) -> list[dict[str, str]]:
    """Transforma o snapshot em alertas simples: disco, rede, docker, bateria etc."""
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
    """Resume quantos alertas críticos/de atenção existem."""
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
    """Formata alertas para mostrar no terminal."""
    if not alerts:
        return "Alertas: nenhum alerta simples no momento"
    lines = ["Alertas:"]
    for item in alerts:
        severity = item.get("severity", "info").upper()
        lines.append(f"- [{severity}] {item.get('message', '')}")
    return "\n".join(lines)


def render_status(snapshot: dict[str, Any], alerts: list[dict[str, str]]) -> str:
    """Monta o painel textual do Orpheus."""
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
    """Executa diagnóstico e imprime o painel completo."""
    snapshot = collect_system_snapshot()
    alerts = build_health_alerts(snapshot)
    print(render_status(snapshot, alerts))
    return snapshot, alerts


def run_watch(interval_seconds: int) -> None:
    """Atualiza o painel /status repetidamente no terminal."""
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
    """Caminho do baseline local; é ignorado pelo git."""
    return cfg.SCRIPT_DIR / ".orpheus_baseline.json"


def save_baseline(snapshot: dict[str, Any]) -> Path:
    """Salva o estado atual para comparar depois com /compare."""
    path = baseline_path()
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def load_baseline() -> dict[str, Any] | None:
    """Carrega o baseline salvo, se existir e for JSON válido."""
    path = baseline_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def compare_with_baseline(snapshot: dict[str, Any], baseline: dict[str, Any]) -> list[str]:
    """Compara campos principais do snapshot atual com o baseline salvo."""
    keys = ["kernel", "uptime", "load", "mem", "root", "ip", "route", "wifi", "docker", "battery", "temp"]
    changes: list[str] = []
    for key in keys:
        before = str(baseline.get(key, "indisponível"))
        now = str(snapshot.get(key, "indisponível"))
        if before != now:
            changes.append(f"{key}: {before} -> {now}")
    return changes


def print_brief() -> None:
    """Mostra uma linha curta com o estado principal do computador."""
    snapshot = collect_system_snapshot()
    alerts = build_health_alerts(snapshot)
    print(f"Orpheus | host={snapshot.get('host', 'indisponível')} | uptime={snapshot.get('uptime', 'indisponível')} | carga={snapshot.get('load', 'indisponível')} | memória={snapshot.get('mem', 'indisponível')} | disco={snapshot.get('root', 'indisponível')} | docker={snapshot.get('docker', 'indisponível')} | alertas={summarize_alerts(alerts)}")


def print_alerts_only() -> None:
    """Mostra só alertas, sem painel completo."""
    snapshot = collect_system_snapshot()
    alerts = build_health_alerts(snapshot)
    print(render_alerts(alerts))


def print_incident_summary() -> None:
    """Resume a saúde atual em formato de incidente/próximo passo."""
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
    """Busca uma URL pelo fetch_url e imprime uma prévia curta."""
    append_log("web_request", url)
    result = _fetch_url(url, "GET", 4000)
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
