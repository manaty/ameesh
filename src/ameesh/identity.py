# SPDX-License-Identifier: AGPL-3.0-only
"""Identité d'une session : explicite, et liée au bail (jamais déduite du cwd).

Une session a une identité si, et seulement si :

1. `AGENT_MAIL_NAME` est posée (le runner la pose pour le harnais qu'il lance) ;
2. si `AMEESH_RUNNER_ID` et `AMEESH_LEASE_EPOCH` sont aussi posées (les
   anciens `AGENT_MESH_*` restent lus en alias),
   le bail correspondant doit être **vivant et détenu par ce runner** : c'est
   l'identité *liée au bail*, qui empêche une session périmée ou une autre
   machine de consommer le courrier ;
3. sinon, c'est une identité *explicite* (session hors runner, repli fichier) ;
4. sans `AGENT_MAIL_NAME`, une session EXTERNE (lancée par un humain) a une
   identité si elle est **liée** (L41, décision 0030) : `ameesh mail bind
   <agent> --session <id> --harness <h> [--pid N]` lie l'identifiant de
   session du harnais à un nom, sur cet hôte. Le hook la retrouve par
   (hôte, harnais, identifiant reçu en JSON) ; une commande lancée dans la
   session (`whoami`, `send`, `inbox`), qui ne reçoit pas l'identifiant, la
   retrouve par son ascendance (le PID lié est l'un de ses ancêtres). Source
   `session` : jamais liée à un bail, et refusée si l'agent en détient un
   vivant (il est mené par l'exécuteur).

Le dossier courant ne donne **jamais** d'identité : lire la documentation d'un
autre agent dans son worktree ne doit pas consommer son courrier (incident
mesh-design du 2026-10-09). Les alias restent, pour ce qu'ils sont : une
convention d'affichage (workstream, barre d'état, diagnostic).
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
    source: str = "none"      # runner | explicit | session | none
    runner_id: str = ""
    epoch: int = 0
    ok: bool = False
    reason: str = ""
    #: L41 (0030) : la liaison de session (ligne `session_bindings`) si
    #: source == "session"
    session: dict | None = None

    @property
    def bound_to_lease(self) -> bool:
        return self.source == "runner" and self.ok

    def describe(self) -> str:
        if self.ok:
            return "%s (%s)" % (self.name, self.source)
        return "unbound" + (": %s" % self.reason if self.reason else "")

    def describe_session(self) -> str:
        """La liaison de session, en une ligne (vide hors source `session`)."""
        row = self.session or {}
        if not row:
            return ""
        return "binding %s %s on %s, pid %s, by %s" % (
            row.get("harness"), row.get("session_id"), row.get("host"),
            row.get("pid") or "not checked", row.get("created_by") or "?")


def aliases(cfg: Config) -> list[tuple[str, str, str]]:
    """[(dossier réel, nom, workstream)] triés du préfixe le plus long au plus court."""
    out: list[tuple[str, str, str]] = []
    try:
        with open(cfg.aliases_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) >= 2 and parts[0] and parts[1]:
                    workstream = parts[2] if len(parts) > 2 else ""
                    out.append((os.path.realpath(os.path.expanduser(parts[1])), parts[0], workstream))
    except FileNotFoundError:
        pass
    return sorted(out, key=lambda t: -len(t[0]))


def legacy_name(cwd: str | None, cfg: Config) -> str:
    """Nom déduit du dossier — **diagnostic seulement**, jamais une identité.

    Conservé pour `whoami --cwd` et la barre d'état (affichage), qui ne
    consomment rien et ne donnent aucune autorité.
    """
    real = os.path.realpath(cwd or os.getcwd())
    for path, name, _workstream in aliases(cfg):
        if real == path or real.startswith(path + os.sep):
            return name
    base = re.sub(r"[^A-Za-z0-9._-]", "-", os.path.basename(real) or "racine")
    return base[:64] or "racine"


def resolve_binding(cfg: Config, db=None, *, harness: str | None = None,
                    session_id: str | None = None) -> Binding:
    """L'identité de cette session : explicite, et liée au bail si le runner l'a posée.

    `db` est optionnel : sans base (repli fichier), une identité explicite suffit
    — il n'y a pas de bail à vérifier, et pas de liaison de session à lire.

    Sans AGENT_MAIL_NAME (L41, 0030) : la liaison de session. Le hook passe
    `harness` et `session_id` (reçus du harnais) : seule la liaison de CETTE
    session compte. Sans eux, la liaison est cherchée par l'ascendance du
    processus.
    """
    name = os.environ.get("AGENT_MAIL_NAME") or ""
    if not name:
        return _session_binding(cfg, db, harness, session_id)
    if not NAME_RE.match(name):
        return Binding(reason="AGENT_MAIL_NAME not set (the runner sets it for the harness)")
    runner_id = os.environ.get("AMEESH_RUNNER_ID") or os.environ.get("AGENT_MESH_RUNNER_ID") or ""
    epoch_text = (os.environ.get("AMEESH_LEASE_EPOCH")
                  or os.environ.get("AGENT_MESH_LEASE_EPOCH") or "")
    if runner_id and epoch_text and db is not None:
        try:
            epoch = int(epoch_text)
        except ValueError:
            return Binding(name=name, source="runner", runner_id=runner_id,
                           reason="unreadable AMEESH_LEASE_EPOCH")
        from . import registry  # import tardif : identity ne dépend pas de la base
        ok, reason = registry.lease_matches(db, name, runner_id, epoch)
        return Binding(name=name, source="runner", runner_id=runner_id, epoch=epoch,
                       ok=ok, reason=reason)
    return Binding(name=name, source="explicit", ok=True)


#: L244 (#420) : sources acceptées pour signer un geste de droits — le bail du
#: runner (le plus fort), ou une liaison de session explicite (L41). Une simple
#: variable `AGENT_MAIL_NAME` (source `explicit`) est auto-déclarée.
BOUND_SOURCES = ("runner", "session")


def require_bound(cfg: Config, db=None, *, what: str = "this command",
                  sources: tuple[str, ...] = BOUND_SOURCES) -> Binding:
    """L'identité liée qu'exige un geste de droits (L244, #420).

    Refuse une identité absente, un bail qui ne correspond pas, ou une source
    seulement explicite : une signature de droits ne se contente pas d'un nom
    auto-déclaré."""
    binding = resolve_binding(cfg, db)
    if not binding.name or not binding.ok or binding.source not in sources:
        if binding.name and not binding.ok:
            reason = binding.reason or "identity does not match a live lease"
        elif binding.name:
            reason = "identity %s is %s, not bound" % (binding.name, binding.source)
        else:
            reason = binding.reason or "no bound session identity"
        raise ValueError("%s requires a bound session identity (%s)" % (what, reason))
    return binding


_UNBOUND = ("AGENT_MAIL_NAME not set (the runner sets it for the harness) and no "
            "session binding (ameesh mail bind)")


def _session_binding(cfg: Config, db, harness: str | None,
                     session_id: str | None) -> Binding:
    """L41 (0030) : identité d'une session externe, par sa liaison explicite.

    Jamais d'exception : une base sans la table (migration 0034 non appliquée)
    ou injoignable revient à « non lié »."""
    if db is None:
        return Binding(reason=_UNBOUND)
    from . import session_bindings as sb  # import tardif : identity ne dépend pas de la base
    try:
        if session_id is not None or harness is not None:
            row, why = sb.for_hook(cfg, db, harness or "", session_id or "")
            if row is None:
                return Binding(reason="AGENT_MAIL_NAME not set and " + why)
            if why:
                return Binding(name=row["agent"], source="session", session=row,
                               reason=why)
        else:
            row = sb.by_ancestry(cfg, db)
            if row is None:
                return Binding(reason=_UNBOUND)
        holder = sb.lease_holder(db, row["agent"])
    except Exception as exc:  # table absente, base en panne : rien n'est lié
        if getattr(db, "_schema_missing", False):
            # L61 : la vérification de schéma (jointe à cette première
            # requête) a trouvé une base non migrée — c'est l'erreur à dire,
            # pas « non lié »
            raise
        return Binding(reason="unreadable session binding (%s)"
                       % " ".join(str(exc).split())[:120])
    if holder:
        return Binding(name=row["agent"], source="session", session=row,
                       reason="%s is driven by the runner (live lease of %s): "
                              "the session binding no longer applies" % (row["agent"], holder))
    return Binding(name=row["agent"], source="session", session=row, ok=True)


def workstream_of(name: str, cfg: Config) -> str:
    for _path, alias, workstream in aliases(cfg):
        if alias == name:
            return workstream
    return ""


#: L148 (stage 2c): old spelling kept for the callers not yet renamed.
chantier_of = workstream_of

#: Domaine des adresses git des agents — jamais routable, le même que celui de
#: l'agent côté appareil (`mediated_executor/work_repo.py`).
GIT_EMAIL_DOMAIN = "ameesh.invalid"


def git_identity(name: str) -> dict:
    """L'identité git d'une session (#834) : auteur ET committer.

    Une session la porte par l'**environnement** (`GIT_AUTHOR_*`,
    `GIT_COMMITTER_*`), jamais par `git config` : les worktrees d'un même
    dépôt partagent `.git/config`, donc un `user.name` écrit par un agent
    vaut pour tous les autres (incident du 13/10/2026 : une session reprise
    sans identité commitait sous le nom d'un autre agent). Les variables
    d'environnement priment sur la configuration ; la valeur vient du nom
    d'agent du bail — la même source que `AGENT_MAIL_NAME` — donc aucune
    configuration git n'est lue.
    """
    adresse = "%s@%s" % (name, GIT_EMAIL_DOMAIN)
    return {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": adresse,
            "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": adresse}


def write_alias(cfg: Config, name: str, directory: str, workstream: str = "") -> str:
    """Ajoute/remplace un alias sans jamais laisser le fichier à moitié écrit."""
    os.makedirs(cfg.config_dir, mode=0o700, exist_ok=True)
    path = os.path.realpath(os.path.expanduser(directory))
    keep: list[str] = []
    if os.path.exists(cfg.aliases_path):
        with open(cfg.aliases_path, encoding="utf-8") as fh:
            for line in fh.read().splitlines():
                if line.split("\t")[0] != name:
                    keep.append(line)
    keep.append("\t".join([name, path] + ([workstream] if workstream else [])))
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
