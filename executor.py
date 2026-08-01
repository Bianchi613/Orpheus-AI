"""Executor local do Orpheus.

Este módulo responde: como o agente age no terminal com segurança?
Aqui ficam bloqueios permanentes, aprovação humana, sudo, simulação APT,
streaming de saída, watchdog e execução real dos comandos.
"""

from __future__ import annotations

import json
import os
import re
import selectors
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import config as cfg
import state

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
    """Resultado padronizado de qualquer comando executado pelo agente."""
    stdout: str
    stderr: str
    exit_code: int | None
    timed_out: bool


def blocked_reason(command: str) -> str | None:
    """Retorna o motivo de bloqueio quando o comando viola regra permanente."""
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
    """Decide se um comando parece apenas leitura e pode rodar sem confirmação."""
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
    """Bloqueia comandos compostos arriscados, principalmente envolvendo APT."""
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
    """Converte uma ação APT de instalação/upgrade em simulação segura."""
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
    """Extrai simulações APT de comandos compostos separados por &&, ; ou ||."""
    segments = re.split(r"\s*(?:&&|;|\|\|)\s*", command.strip())
    simulations: list[str] = []
    for segment in segments:
        simulation = apt_simulation_command(segment)
        if simulation:
            simulations.append(simulation)
    return simulations


def apt_simulation_reveals_removal(text: str) -> bool:
    """Detecta se a simulação do APT pretende remover pacotes."""
    patterns = [
        r"(?m)^Os pacotes a seguir serão REMOVIDOS:",
        r"(?m)^The following packages will be REMOVED:",
        r"(?m)^Remv\b",
        r"(?m)^Remover\b",
    ]
    return any(re.search(pattern, text, re.I) for pattern in patterns)


def apt_plan_safety_reason(command: str) -> str | None:
    """Roda simulação APT antes de permitir instalação/upgrade real."""
    simulations = apt_simulation_commands(command)
    if not simulations:
        return None

    for simulation in simulations:
        try:
            completed = subprocess.run(
                simulation,
                shell=True,
                executable="/bin/bash",
                cwd=str(cfg.WORKDIR),
                env=clean_child_environment(),
                text=True,
                capture_output=True,
                timeout=min(cfg.DEFAULT_TIMEOUT, 45),
                errors="replace",
            )
        except Exception:
            continue

        combined = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
        if apt_simulation_reveals_removal(combined):
            return "o plano do APT removeria pacotes; isso é bloqueado"

    return None


def docker_status_failed(command: str, result: CommandResult) -> bool:
    """Reconhece quando systemctl status docker mostra falha real do serviço."""
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
    """Executa diagnóstico interno rápido sem pedir aprovação ao usuário."""
    state.append_log("internal_command", command)
    try:
        completed = subprocess.run(
            command,
            shell=True,
            executable="/bin/bash",
            cwd=str(cfg.WORKDIR),
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
        state.append_log("internal_result", json.dumps({"exit_code": result.exit_code, "stdout": result.stdout, "stderr": result.stderr}, ensure_ascii=False))
        return result
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        result = CommandResult(clip(stdout, output_limit), clip(stderr, output_limit), None, True)
        state.append_log("internal_timeout", json.dumps({"stdout": result.stdout, "stderr": result.stderr}, ensure_ascii=False))
        return result
    except Exception as exc:
        result = CommandResult("", f"ERRO DO EXECUTOR: {type(exc).__name__}: {exc}", 1, False)
        state.append_log("internal_error", result.stderr)
        return result


def ask_approval(command: str) -> bool:
    """Pede confirmação humana antes de comandos que podem alterar o sistema."""
    if cfg.AUTO_APPROVE or looks_read_only(command):
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
    """Detecta uso de sudo dentro do comando."""
    return bool(re.search(r"(^|[\s;&|])sudo(?:\s|$)", command, re.I))


def sudo_is_ready() -> bool:
    """Confere se sudo já está autenticado e pode rodar sem pedir senha agora."""
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
    # Lista restrita usada pelo /sudo setup para liberar manutenção sem senha.
    # Mesmo com isso, as regras de bloqueio do executor continuam valendo.
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


def refresh_sudo() -> None:
    """Renova a sessão sudo pedindo senha diretamente no terminal local."""
    print("Vou pedir sua senha pelo sudo. Ela não será salva nem enviada para a IA.")
    try:
        completed = subprocess.run(["sudo", "-v"], cwd=str(cfg.WORKDIR))
    except FileNotFoundError:
        print("Erro: sudo não encontrado no sistema.")
        return

    if completed.returncode == 0:
        print("sudo autorizado por alguns minutos.")
    else:
        print(f"sudo não autorizado. Código de saída: {completed.returncode}")


def setup_passwordless_sudo() -> None:
    """Cria regra sudoers opcional para comandos de manutenção específicos."""
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
            cwd=str(cfg.WORKDIR),
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
    """Limita saída grande mantendo começo e fim, para caber no prompt/log."""
    if len(text) <= limit:
        return text

    half = max(1, (limit - 100) // 2)
    removed = len(text) - (2 * half)
    return (
        text[:half]
        + f"\n\n... [{removed} caracteres omitidos] ...\n\n"
        + text[-half:]
    )


LONG_RUNNING_COMMANDS = re.compile(
    # Comandos que normalmente baixam/instalam coisas e podem ficar minutos rodando.
    r"\b(?:"
    r"apt(?:-get)?\s+(?:update|install|upgrade|full-upgrade|dist-upgrade)|"
    r"flatpak\s+(?:install|update|repair)|"
    r"snap\s+(?:install|refresh)|"
    r"ollama\s+pull|"
    r"docker\s+(?:pull|build|compose\s+build)|"
    r"npm\s+(?:install|ci)|"
    r"pip(?:3)?\s+install|"
    r"python3?\s+-m\s+pip\s+install"
    r")\b",
    re.I,
)


def timeout_for_command(command: str, default_timeout: float) -> float:
    """Escolhe timeout maior para comandos sabidamente demorados."""
    if LONG_RUNNING_COMMANDS.search(command):
        return max(default_timeout, cfg.LONG_COMMAND_TIMEOUT)
    return default_timeout


def append_capture(current: str, text: str, limit: int) -> str:
    """Acumula saída ao vivo sem deixar a memória crescer sem limite."""
    cap = max(limit * 2, limit + 1000)
    combined = current + text
    if len(combined) <= cap:
        return combined

    keep = max(1, (cap - 120) // 2)
    return (
        combined[:keep]
        + "\n\n... [saída omitida durante captura ao vivo] ...\n\n"
        + combined[-keep:]
    )


def stop_process_tree(process: subprocess.Popen[Any]) -> None:
    """Tenta encerrar o processo e seus filhos quando há timeout."""
    try:
        if hasattr(os, "killpg"):
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=5)
    except Exception:
        try:
            if hasattr(os, "killpg"):
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except Exception:
            pass


def run_captured_streaming(command: str, timeout: float, output_limit: int) -> CommandResult:
    """Executa comando não interativo mostrando saída em tempo real."""
    effective_timeout = timeout_for_command(command, timeout)
    start = time.monotonic()
    last_output = start
    last_idle_notice = start
    captured = ""
    notices: list[str] = []

    process = subprocess.Popen(
        command,
        shell=True,
        executable="/bin/bash",
        cwd=str(cfg.WORKDIR),
        env=clean_child_environment(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        preexec_fn=os.setsid if hasattr(os, "setsid") else None,
    )

    selector = selectors.DefaultSelector()
    assert process.stdout is not None
    fd = process.stdout.fileno()
    os.set_blocking(fd, False)
    selector.register(fd, selectors.EVENT_READ)

    try:
        while True:
            now = time.monotonic()

            if now - start > effective_timeout:
                message = f"TIMEOUT: comando excedeu {effective_timeout:.0f}s e foi interrompido pelo executor."
                print(f"\n[{message}]")
                notices.append(message)
                stop_process_tree(process)
                stdout = clip(captured, output_limit)
                stderr = "\n".join(notices)
                return CommandResult(stdout, stderr, None, True)

            if cfg.IDLE_TIMEOUT > 0 and now - last_output >= cfg.IDLE_TIMEOUT and now - last_idle_notice >= cfg.IDLE_TIMEOUT:
                silent_for = now - last_output
                remaining = max(0.0, effective_timeout - (now - start))
                message = (
                    f"sem nova saída há {silent_for:.0f}s; comando ainda está rodando... "
                    f"tempo restante antes de abortar: {remaining:.0f}s"
                )
                print(f"\n[{message}]", flush=True)
                notices.append(message)
                last_idle_notice = now

            events = selector.select(timeout=1)
            if events:
                for key, _ in events:
                    try:
                        chunk = os.read(key.fd, 4096)
                    except BlockingIOError:
                        continue

                    if not chunk:
                        try:
                            selector.unregister(key.fd)
                        except Exception:
                            pass
                        continue

                    text = chunk.decode(errors="replace")
                    captured = append_capture(captured, text, output_limit)
                    print(text, end="", flush=True)
                    last_output = time.monotonic()

            if process.poll() is not None:
                while True:
                    try:
                        chunk = os.read(fd, 4096)
                    except BlockingIOError:
                        break
                    except OSError:
                        break
                    if not chunk:
                        break
                    text = chunk.decode(errors="replace")
                    captured = append_capture(captured, text, output_limit)
                    print(text, end="", flush=True)

                stdout = clip(captured, output_limit)
                stderr = "\n".join(notices)
                return CommandResult(stdout, stderr, process.returncode, False)
    finally:
        selector.close()


def run_command(command: str, timeout: float, output_limit: int) -> CommandResult:
    """Executor principal chamado pela ferramenta run_shell do modelo."""
    state.append_log("command", command)
    state.write_status("running_command", "iniciando comando", command=command)

    reason = blocked_reason(command)
    if reason:
        message = f"BLOQUEADO PELO EXECUTOR: {reason}."
        print(f"\n⛔ {command}")
        print(f"   {message}")
        result = CommandResult("", message, 126, False)
        state.append_log("blocked", message)
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
        state.append_log("rejected", message)
        return result

    print(f"\n▶ {cfg.WORKDIR}$ {command}")

    try:
        if needs_interactive_terminal(command):
            completed = subprocess.run(
                command,
                shell=True,
                executable="/bin/bash",
                cwd=str(cfg.WORKDIR),
                env=clean_child_environment(),
                timeout=timeout_for_command(command, timeout),
            )
            print(f"[código de saída: {completed.returncode}]")
            result = CommandResult(
                "",
                "Comando interativo executado no terminal; saída não capturada pelo executor.",
                completed.returncode,
                False,
            )
            state.append_log("interactive_result", json.dumps({"exit_code": result.exit_code, "stderr": result.stderr}, ensure_ascii=False))
            return result

        result = run_captured_streaming(command, timeout, output_limit)
        print(f"[código de saída: {result.exit_code if result.exit_code is not None else 'timeout'}]")
        state.append_log("result", json.dumps({"exit_code": result.exit_code, "stdout": result.stdout, "stderr": result.stderr}, ensure_ascii=False))
        state.write_status("command_finished", f"comando finalizado com código {result.exit_code}", command=command, exit_code=result.exit_code)
        return result

    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        stdout = clip(stdout, output_limit)
        stderr = clip(stderr, output_limit)
        print(f"[tempo limite excedido após {timeout:.0f}s]")
        result = CommandResult(stdout, stderr, None, True)
        state.append_log("timeout", json.dumps({"stdout": result.stdout, "stderr": result.stderr}, ensure_ascii=False))
        return result

    except Exception as exc:
        message = f"ERRO DO EXECUTOR: {type(exc).__name__}: {exc}"
        print(message, file=sys.stderr)
        result = CommandResult("", message, 1, False)
        state.append_log("executor_error", message)
        return result
