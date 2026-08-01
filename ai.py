#!/usr/bin/env python3
"""Entrada compatível do Orpheus.

O programa foi separado em módulos por propósito. Este arquivo fica como
lançador para manter `python3 ai.py` e o atalho `ai` funcionando.
"""

from orpheus.app import main


if __name__ == "__main__":
    raise SystemExit(main())
