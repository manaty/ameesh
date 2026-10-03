"""Identité d'une session : mêmes règles que agent-mail v0.

`$AGENT_MAIL_NAME`, sinon le plus long préfixe d'alias qui contient le dossier,
sinon le nom du dossier assaini. Les alias sont partagés avec la v0
(`~/.config/agent-mail/aliases.tsv`), lus et écrits de façon atomique.
"""
from __future__ import annotations

import os
import re
import tempfile

from .config import NAME_RE, Config

CHANTIER_MAX = 64


def aliases(cfg: Config) -> list[tuple[str, str, str]]:
    """[(dossier réel, nom, chantier)] triés du préfixe le plus long au plus court."""
    out: list[tuple[str, str, str]] = []
    try:
        with open(cfg.aliases_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) >= 2 and parts[0] and parts[1]:
                    chantier = parts[2] if len(parts) > 2 else ""
                    out.append((os.path.realpath(os.path.expanduser(parts[1])), parts[0], chantier))
    except FileNotFoundError:
        pass
    return sorted(out, key=lambda t: -len(t[0]))


def resolve_name(cwd: str | None, cfg: Config) -> str:
    env = os.environ.get("AGENT_MAIL_NAME")
    if env and NAME_RE.match(env):
        return env
    real = os.path.realpath(cwd or os.getcwd())
    for path, name, _chantier in aliases(cfg):
        if real == path or real.startswith(path + os.sep):
            return name
    base = re.sub(r"[^A-Za-z0-9._-]", "-", os.path.basename(real) or "racine")
    return base[:64] or "racine"


def chantier_of(name: str, cfg: Config) -> str:
    for _path, alias, chantier in aliases(cfg):
        if alias == name:
            return chantier
    return ""


def write_alias(cfg: Config, name: str, directory: str, chantier: str = "") -> str:
    """Ajoute/remplace un alias sans jamais laisser le fichier à moitié écrit."""
    os.makedirs(cfg.config_dir, mode=0o700, exist_ok=True)
    path = os.path.realpath(os.path.expanduser(directory))
    keep: list[str] = []
    if os.path.exists(cfg.aliases_path):
        with open(cfg.aliases_path, encoding="utf-8") as fh:
            for line in fh.read().splitlines():
                if line.split("\t")[0] != name:
                    keep.append(line)
    keep.append("\t".join([name, path] + ([chantier] if chantier else [])))
    fd, tmp = tempfile.mkstemp(dir=cfg.config_dir, prefix=".aliases.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(keep) + "\n")
        os.replace(tmp, cfg.aliases_path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path
