# SPDX-License-Identifier: AGPL-3.0-only
"""Identité d'une session : explicite, et liée au bail (jamais déduite du cwd).

Une session a une identité si, et seulement si :

1. `AGENT_MAIL_NAME` est posée (le runner la pose pour le harnais qu'il lance) ;
2. si `AMEESH_RUNNER_ID` et `AMEESH_LEASE_EPOCH` sont aussi posées (les
   anciens `AGENT_MESH_*` restent lus en alias),
   le bail correspondant doit être **vivant et détenu par ce runner** : c'est
   l'identité *liée au bail*, qui empêche une session périmée ou une autre
   machine de consommer le courrier ;
3. sinon, c'est une identité *explicite* (session hors runner, repli fichier).

Le dossier courant ne donne **jamais** d'identité : lire la documentation d'un
autre agent dans son worktree ne doit pas consommer son courrier (incident
mesh-design du 2026-10-09). Les alias restent, pour ce qu'ils sont : une
convention d'affichage (chantier, barre d'état, diagnostic).
"""
from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass

from .config import NAME_RE, Config

CHANTIER_MAX = 64


@dataclass(frozen=True)
class Binding:
    """L'identité d'une session, et d'où elle vient."""

    name: str = ""
    source: str = "none"      # runner | explicit | none
    runner_id: str = ""
    epoch: int = 0
    ok: bool = False
    reason: str = ""

    @property
    def bound_to_lease(self) -> bool:
        return self.source == "runner" and self.ok

    def describe(self) -> str:
        if self.ok:
            return "%s (%s)" % (self.name, self.source)
        return "non lié" + (" : %s" % self.reason if self.reason else "")


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


def legacy_name(cwd: str | None, cfg: Config) -> str:
    """Nom déduit du dossier — **diagnostic seulement**, jamais une identité.

    Conservé pour `whoami --cwd` et la barre d'état (affichage), qui ne
    consomment rien et ne donnent aucune autorité.
    """
    real = os.path.realpath(cwd or os.getcwd())
    for path, name, _chantier in aliases(cfg):
        if real == path or real.startswith(path + os.sep):
            return name
    base = re.sub(r"[^A-Za-z0-9._-]", "-", os.path.basename(real) or "racine")
    return base[:64] or "racine"


def resolve_binding(cfg: Config, db=None) -> Binding:
    """L'identité de cette session : explicite, et liée au bail si le runner l'a posée.

    `db` est optionnel : sans base (repli fichier), une identité explicite suffit
    — il n'y a pas de bail à vérifier.
    """
    name = os.environ.get("AGENT_MAIL_NAME") or ""
    if not name or not NAME_RE.match(name):
        return Binding(reason="AGENT_MAIL_NAME absente (le runner la pose pour le harnais)")
    runner_id = os.environ.get("AMEESH_RUNNER_ID") or os.environ.get("AGENT_MESH_RUNNER_ID") or ""
    epoch_text = (os.environ.get("AMEESH_LEASE_EPOCH")
                  or os.environ.get("AGENT_MESH_LEASE_EPOCH") or "")
    if runner_id and epoch_text and db is not None:
        try:
            epoch = int(epoch_text)
        except ValueError:
            return Binding(name=name, source="runner", runner_id=runner_id,
                           reason="AMEESH_LEASE_EPOCH illisible")
        from . import registry  # import tardif : identity ne dépend pas de la base
        ok, reason = registry.lease_matches(db, name, runner_id, epoch)
        return Binding(name=name, source="runner", runner_id=runner_id, epoch=epoch,
                       ok=ok, reason=reason)
    return Binding(name=name, source="explicit", ok=True)


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
