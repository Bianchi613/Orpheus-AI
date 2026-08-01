"""Configuração central do Orpheus.

Este módulo responde: com quais caminhos, flags, modelo e limites o agente roda?
Ele carrega o .env, define arquivos locais e guarda estado mutável da sessão.
"""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

# WORKDIR é a pasta onde o usuário abriu o agente. Os comandos rodam daqui.
WORKDIR = Path.cwd().resolve()

# SCRIPT_DIR aponta para a raiz do projeto, onde ficam ai.py, .env e .ai_prompt.txt.
SCRIPT_DIR = Path(__file__).resolve().parent

APP_VERSION = "2026.08.01.30"
APP_NAME = "Orpheus"

# SESSION_ID identifica esta execução do agente.
SESSION_ID = time.strftime("%Y%m%d-%H%M%S")

# CURRENT_TASK_ID é atualizado pelo ai.py quando uma tarefa nova começa.
CURRENT_TASK_ID = "idle"

RESET = "[0m"
BOLD = "[1m"
COLOR_USER = "[96m"
COLOR_AI = "[92m"
COLOR_INFO = "[93m"


def colorize(text: str, color: str) -> str:
    """Aplica cor ANSI só quando a saída é um terminal real."""
    if not sys.stdout.isatty():
        return text
    return f"{color}{text}{RESET}"


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


# O .env da pasta atual tem precedência sobre o que fica junto do script.
# Isso permite rodar o mesmo ai.py em outra pasta com configuração diferente.
if WORKDIR != SCRIPT_DIR:
    load_dotenv(WORKDIR / ".env")
load_dotenv(SCRIPT_DIR / ".env")

# Caminhos locais. Todos estes arquivos são privados e ignorados pelo git.
MEMORY_PATH = Path(os.environ.get("AI_MEMORY_FILE", SCRIPT_DIR / ".ai_memory.json")).expanduser()
LOG_PATH = Path(os.environ.get("AI_LOG_FILE", SCRIPT_DIR / "ai.log")).expanduser()
STATE_PATH = Path(os.environ.get("AI_STATE_FILE", SCRIPT_DIR / "ai.status.json")).expanduser()
PROMPT_PATH = Path(os.environ.get("AI_PROMPT_FILE", SCRIPT_DIR / ".ai_prompt.txt"))

# Variáveis principais do executor. Todas podem ser ajustadas no .env.
MAX_STEPS = int(os.environ.get("AI_MAX_STEPS", "30"))
DEFAULT_TIMEOUT = float(os.environ.get("AI_COMMAND_TIMEOUT", "60"))
LONG_COMMAND_TIMEOUT = float(os.environ.get("AI_LONG_COMMAND_TIMEOUT", "900"))
IDLE_TIMEOUT = float(os.environ.get("AI_IDLE_TIMEOUT", "45"))
MAX_CAPTURE = int(os.environ.get("AI_MAX_OUTPUT", "12000"))
AUTO_APPROVE = os.environ.get("AI_AUTO_APPROVE", "0") == "1"
SELF_MODIFY = os.environ.get("AI_SELF_MODIFY", "0") == "1"
