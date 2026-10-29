# SPDX-License-Identifier: AGPL-3.0-only
"""Interpreter shared by packet composition and merged-tree freeze checks."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

_PYTHON_INTERPRETER: str | None = None
_PYTHON_OVERRIDE: str | None = None

def _supports_psycopg(python: str) -> bool:
    try:
        proc = subprocess.run([python, "-c", "import psycopg"],
                              stdin=subprocess.DEVNULL, capture_output=True,
                              timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def project_python(worktree: Path) -> str:
    """Un interpréteur qui porte psycopg, pour les outils de l'arbre.

    Le script lui-même n'a que la bibliothèque standard ; ses enfants
    (`paquet-candidats`, `paquet-compose`, les tests) importent le projet.
    Un agent qui lance `python3 scripts/paquet-auto.py` avec le Python du
    système n'a pas psycopg : on prend le `.venv` de l'arbre, celui du
    worktree principal, ou le Python du venv d'`ameesh` (le wrapper de la
    session). À défaut, `sys.executable` : `paquet-candidats` rendra son
    refus, sans le maquiller.
    """
    global _PYTHON_INTERPRETER
    if _PYTHON_OVERRIDE:
        return _PYTHON_OVERRIDE
    if _PYTHON_INTERPRETER:
        return _PYTHON_INTERPRETER
    candidates: list[str] = []
    env = os.environ.get("AMEESH_PYTHON")
    if env:
        candidates.append(env)
    candidates.append(sys.executable)
    candidates.append(str(worktree / ".venv" / "bin" / "python"))
    listed = subprocess.run(["git", "-C", str(worktree), "worktree", "list", "--porcelain"],
                            capture_output=True, text=True, timeout=30)
    for line in listed.stdout.splitlines():
        if line.startswith("worktree "):
            candidates.append(str(Path(line[len("worktree "):]) /
                                 ".venv" / "bin" / "python"))
    wrapper = shutil.which("ameesh")
    if wrapper:
        try:
            text = Path(wrapper).read_text(encoding="utf-8")
        except OSError:
            text = ""
        match = re.search(r"exec\s+(\S+)/ameesh\b", text)
        if match:
            candidates.append(match.group(1) + "/python")
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        if Path(candidate).is_file() and _supports_psycopg(candidate):
            _PYTHON_INTERPRETER = candidate
            return candidate
    _PYTHON_INTERPRETER = sys.executable
    return _PYTHON_INTERPRETER

