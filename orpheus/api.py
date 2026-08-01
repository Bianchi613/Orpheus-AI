"""Comunicação com modelos e ferramentas web do Orpheus.

Este módulo responde: como o agente fala com OpenAI/Ollama e como busca URLs públicas?
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from . import config as cfg

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
        # OpenAI e Ollama usam caminho compatível /chat/completions.
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.api_key = api_key

    def chat(self, model: str, messages: list[Any]) -> dict[str, Any]:
        """Envia histórico + ferramentas para o modelo e devolve o JSON bruto."""
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

def fetch_url(url: str, method: str = "GET", max_bytes: int = 12000) -> dict[str, Any]:
    """Ferramenta web simples: busca página pública e devolve texto limitado."""
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
            "User-Agent": f"ai-terminal/{cfg.APP_VERSION}",
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
