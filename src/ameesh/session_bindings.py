# SPDX-License-Identifier: AGPL-3.0-only
"""Liaisons de session : l'identité d'une session EXTERNE (L41, décision 0030).

Une session interactive lancée par un humain n'a ni bail ni AGENT_MAIL_NAME.
Avant L41, les hooks appelaient l'`agent-mail` v0, qui tirait l'identité du
DOSSIER (alias par préfixe, sinon nom du dossier) et lisait la boîte fichier :
une session sous ~/Work prenait l'identité d'une autre équipe, et aucune
session externe ne voyait son courrier v1. L41 (0030) remplace cela par une
liaison EXPLICITE, en base :

    ameesh mail bind <agent> --session <id> --harness claude|codex|deepseek [--pid N]

* l'identifiant de session est celui que le harnais passe au hook (JSON
  `session_id` sur stdin) ; la liaison vaut pour CET hôte ;
* `--pid` (recommandé) : le PID du harnais. Le hook n'accepte alors la liaison
  que si ce PID est un ancêtre de son propre processus — un identifiant de
  session recopié dans un autre processus ne donne rien. L46 : l'heure de
  démarrage du processus est enregistrée avec le PID et recontrôlée : un PID
  recyclé par un autre processus ne vaut rien. L63 : en secondes epoch
  (`pid_started_at`, migration 0048) par la couche plateforme, sur tous les
  OS ; l'ancienne valeur en tops d'horloge Linux (`pid_start`, 0036) reste
  lue par conversion. Si l'OS ne donne ni l'ascendance ni l'heure de
  démarrage, la liaison `--pid` est refusée : jamais de contrôle muet ;
* refusée si l'agent détient un bail vivant (il est mené par l'exécuteur,
  qui lui remet son courrier dans la consigne de ses tours), ou si la session
  est déjà liée à un autre agent ;
* L46 : refusée aussi, sauf `--force`, pour un agent `execute` sans bail
  vivant à l'instant (« agent mené par l'exécuteur ; une session externe lui
  volerait son courrier ») — l'import du pont ne force jamais ;
* sans liaison ni AGENT_MAIL_NAME, le hook ne remet RIEN et n'écrit rien.

Les liaisons sont journalisées dans le fil (`audit: session_bind`).
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Iterable

from . import fil, platform, registry, storage
from .config import NAME_RE, Config

#: harnais dont le hook passe un identifiant de session
HARNESSES = ("claude", "codex", "deepseek")

#: un identifiant de session : imprimable, sans espace, 200 caractères au plus
SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,199}$")

class BindError(ValueError):
    """Liaison refusée : le message dit pourquoi (aucune écriture faite)."""


def ancestors(pid: int | None = None) -> list[int]:
    """L41 (0030) : l'ascendance du processus, du plus proche au plus lointain.

    Inclut `pid` lui-même (par défaut le processus courant), puis ses
    ascendants jusqu'à init, par la couche plateforme (L63). Lève
    `platform.NotAvailable` si l'OS ne permet pas de la remonter."""
    return platform.ancestry(pid)


def pid_started_at(pid: int | None) -> float | None:
    """L46, L63 : l'heure de démarrage du processus `pid` en secondes epoch,
    ou None (pas de PID, processus absent). Avec le PID, elle identifie un
    processus : un PID recyclé a une autre heure de démarrage. Lève
    `platform.NotAvailable` si l'OS ne la donne pas."""
    if not pid:
        return None
    return platform.start_time(int(pid))


def expected_start(row: dict) -> float | None:
    """L'heure de démarrage enregistrée d'une liaison, en secondes epoch :
    `pid_started_at` (L63), sinon l'ancienne `pid_start` (tops d'horloge
    Linux, 0036) convertie. None : aucune heure enregistrée. Lève
    `platform.NotAvailable` si l'ancienne valeur ne se convertit pas ici."""
    if row.get("pid_started_at") is not None:
        return float(row["pid_started_at"])
    if row.get("pid_start") is not None:
        return platform.ticks_to_epoch(int(row["pid_start"]))
    return None


def _same_process(row: dict) -> bool:
    """L46 : le PID lié est-il encore le MÊME processus ? Vrai sans heure de
    démarrage enregistrée (liaison antérieure à L46 : contrôle du seul PID).
    L63 : faux si l'OS ne permet pas de le vérifier (fail-closed)."""
    if not row.get("pid"):
        return True
    try:
        expected = expected_start(row)
        if expected is None:
            return True
        return platform.same_start(expected, pid_started_at(int(row["pid"])))
    except platform.NotAvailable:
        return False


def check_harness(harness: str) -> str:
    if harness not in HARNESSES:
        raise BindError("harnais invalide : %r (%s)" % (harness, "|".join(HARNESSES)))
    return harness


def check_session(session_id: str) -> str:
    if not isinstance(session_id, str) or not SESSION_RE.match(session_id):
        raise BindError("identifiant de session invalide : %r" % (session_id,))
    return session_id


def check_pid(pid) -> int | None:
    if pid in (None, ""):
        return None
    try:
        value = int(pid)
    except (TypeError, ValueError):
        raise BindError("pid invalide : %r" % (pid,)) from None
    if value <= 1:
        raise BindError("pid invalide : %r" % (pid,))
    return value


def lease_holder(db, agent: str) -> str | None:
    """Le détenteur du bail VIVANT de l'agent, ou None (pas de bail vivant)."""
    row = storage.of(db).leases.state(agent)
    if row and row.get("live") and row.get("lease_owner"):
        return str(row["lease_owner"])
    return None


def _audit(cfg: Config, db, *, by: str, agent: str, text: str, meta: dict) -> None:
    row = registry.get(db, agent)
    fil.record(cfg, db, sender=by, recipients=[agent], text=text,
               project=fil.project_for(cfg, fil.agent_project(row)),
               meta=dict(meta, audit="session_bind"))


@dataclass
class BindResult:
    status: str          # created | updated | unchanged
    row: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


#: L46 : refus d'une liaison à un agent `execute` (sauf --force)
EXECUTE_REFUSAL = ("agent mené par l'exécuteur ; une session externe lui volerait "
                   "son courrier")


def bind(cfg: Config, db, agent: str, *, session_id: str, harness: str,
         pid=None, by: str, force: bool = False) -> BindResult:
    """L41 (0030) : lie une session externe de CET hôte à `agent`.

    Lève `BindError` (rien n'est écrit) si l'agent détient un bail vivant ou
    si la session est déjà liée à un autre agent. Même agent : idempotent
    (le PID est mis à jour s'il change).

    L46 : un agent `execute` (mené par l'exécuteur, même sans bail vivant à
    l'instant) est refusé sauf `force` — entre deux tours, la session externe
    prendrait le courrier destiné à son prochain tour. `force` ne lève jamais
    le refus du bail vivant."""
    if not NAME_RE.match(agent or ""):
        raise BindError("nom d'agent invalide : %r" % (agent,))
    check_harness(harness)
    check_session(session_id)
    pid = check_pid(pid)
    holder = lease_holder(db, agent)
    if holder:
        raise BindError(
            "%s détient un bail vivant (exécuteur %s) : il est mené par l'exécuteur, "
            "qui lui remet son courrier ; une session externe ne se lie pas à lui"
            % (agent, holder))
    existing = registry.get(db, agent)
    if existing is not None and existing.get("mode") == "execute" and not force:
        raise BindError("%s : %s (ameesh set %s mode=externe, ou --force)"
                        % (agent, EXECUTE_REFUSAL, agent))
    store = storage.of(db).session_bindings
    host = cfg.host
    # L46 : l'heure de démarrage du PID lié, contrôlée par le hook avec le PID ;
    # L63 : sans moyen de la lire sur cet OS, pas de liaison --pid (le hook ne
    # pourrait rien vérifier)
    try:
        start = pid_started_at(pid) if pid is not None else None
    except platform.NotAvailable as exc:
        raise BindError("--pid %d invérifiable sur cet hôte (%s) : liaison refusée"
                        % (pid, exc)) from None
    row = store.bind(host=host, harness=harness, session_id=session_id, agent=agent,
                     pid=pid, created_by=by, pid_started_at=start)
    status = "created"
    if row is None:
        current = store.active(host, harness, session_id)
        if current is None:  # révoquée entre-temps : on réessaie une fois
            row = store.bind(host=host, harness=harness, session_id=session_id,
                             agent=agent, pid=pid, created_by=by, pid_started_at=start)
            if row is None:
                raise BindError("liaison concurrente de la session %s : réessayez"
                                % session_id)
        elif current.get("agent") != agent:
            raise BindError("la session %s %s est déjà liée à %s (ameesh mail unbind "
                            "--session %s --harness %s d'abord)"
                            % (harness, session_id, current.get("agent"),
                               session_id, harness))
        elif pid is not None and (current.get("pid") != pid
                                  or current.get("pid_start") is not None
                                  or current.get("pid_started_at") != start):
            row = store.set_pid(int(current["id"]), pid, start) or current
            status = "updated"
        else:
            row, status = current, "unchanged"
    result = BindResult(status=status, row=row)
    if pid is not None and start is None:
        result.warnings.append("pid %d introuvable sur cet hôte : son heure de démarrage "
                               "n'est pas enregistrée (seul le PID sera contrôlé)" % pid)
    if existing is not None and existing.get("mode") == "execute":
        result.warnings.append("%s est un agent `execute` lié de force : %s"
                               % (agent, EXECUTE_REFUSAL))
    if existing is None:
        # L46 : lier une session, c'est déclarer l'agent EXTERNE. Inscrit ici
        # (et non au premier hook), il ne naît pas `execute` par un courrier
        # qui le précéderait (`send` inscrit un destinataire inconnu).
        registry.upsert_unleased(db, agent, harness=harness, host=host)
        result.warnings.append("%s n'était pas dans le registre : inscrit comme agent "
                               "externe (session humaine)" % agent)
    others = [b for b in store.listing(agent=agent)
              if not (b.get("host") == host and b.get("harness") == harness
                      and b.get("session_id") == session_id)]
    if others:
        result.warnings.append(
            "%s est aussi liée à %d autre(s) session(s) : %s" % (
                agent, len(others), ", ".join("%s:%s" % (b.get("harness"), b.get("session_id"))
                                             for b in others[:5])))
    if status != "unchanged":
        _audit(cfg, db, by=by, agent=agent,
               text="Session %s %s %s à %s sur %s par %s%s." % (
                   harness, session_id, "liée" if status == "created" else "re-liée",
                   agent, host, by, (" (pid ancêtre exigé : %d)" % pid) if pid else
                   " (sans contrôle de pid)"),
               meta={"action": "bind", "harness": harness, "session": session_id,
                     "host": host, "pid": pid})
    return result


def unbind(cfg: Config, db, *, session_id: str, harness: str, by: str) -> dict | None:
    """Révoque la liaison active de la session sur cet hôte ; None s'il n'y en a pas."""
    check_harness(harness)
    check_session(session_id)
    row = storage.of(db).session_bindings.revoke(cfg.host, harness, session_id)
    if row is not None:
        _audit(cfg, db, by=by, agent=row["agent"],
               text="Liaison de la session %s %s à %s révoquée sur %s par %s." % (
                   harness, session_id, row["agent"], cfg.host, by),
               meta={"action": "unbind", "harness": harness, "session": session_id,
                     "host": cfg.host})
    return row


def revoke_agent(cfg: Config, db, agent: str, *, by: str, why: str) -> list[dict]:
    """L46 : révoque TOUTES les liaisons actives de `agent` (tous hôtes) —
    `ameesh adopt` le fait passer sous l'exécuteur : aucune session externe ne
    doit plus recevoir son courrier. Journalisé dans le fil ; rend les
    liaisons révoquées."""
    store = storage.of(db).session_bindings
    done = []
    for row in store.listing(agent=agent):
        revoked = store.revoke(row["host"], row["harness"], row["session_id"])
        if revoked is not None:
            done.append(revoked)
    if done:
        _audit(cfg, db, by=by, agent=agent,
               text="Liaison(s) de session de %s révoquée(s) par %s (%s) : %s." % (
                   agent, by, why, ", ".join("%s:%s@%s" % (r["harness"], r["session_id"],
                                                          r["host"]) for r in done)),
               meta={"action": "unbind", "reason": why,
                     "sessions": [r["session_id"] for r in done]})
    return done


def listing(db, *, host: str | None = None, include_revoked: bool = False) -> list[dict]:
    return storage.of(db).session_bindings.listing(host=host,
                                                   include_revoked=include_revoked)


@dataclass
class ImportReport:
    imported: list[tuple[str, str, str]] = field(default_factory=list)  # (session, agent, détail)
    skipped: list[tuple[str, str]] = field(default_factory=list)        # (session, raison)


def import_file(cfg: Config, db, path: str, *, by: str) -> ImportReport:
    """Importe les liaisons du pont local (`external-session-bindings.json`).

    Format : `{"version": 1, "sessions": {"<session_id>": {"name", "pid",
    "harness", "cwd"}}}`. `cwd` n'est pas repris : le dossier ne donne jamais
    d'identité (0030). Chaque entrée passe par les mêmes contrôles que
    `bind` ; une entrée refusée est rapportée avec sa raison, sans arrêter
    l'import. Lève OSError / ValueError si le fichier est illisible."""
    with open(os.path.expanduser(path), encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict) or not isinstance(data.get("sessions"), dict):
        raise ValueError("format inattendu : objet {\"version\", \"sessions\": {…}} attendu")
    report = ImportReport()
    for session_id, entry in data["sessions"].items():
        if not isinstance(entry, dict):
            report.skipped.append((str(session_id), "entrée illisible"))
            continue
        agent = entry.get("name") or ""
        harness = entry.get("harness") or ""
        try:
            result = bind(cfg, db, agent, session_id=str(session_id), harness=harness,
                          pid=entry.get("pid"), by=by)
        except BindError as exc:
            report.skipped.append((str(session_id), str(exc)))
            continue
        if result.status == "unchanged":
            report.skipped.append((str(session_id), "déjà liée à %s (identique)" % agent))
            continue
        detail = "%s, pid %s" % (harness, result.row.get("pid") or "non contrôlé")
        if result.status == "updated":
            detail += ", pid mis à jour"
        report.imported.append((str(session_id), agent, detail))
    return report


# --------------------------------------------------------------------------
# résolution (appelée par identity.resolve_binding)
# --------------------------------------------------------------------------

def for_hook(cfg: Config, db, harness: str, session_id: str,
             chain: Iterable[int] | None = None) -> tuple[dict | None, str]:
    """La liaison qui vaut pour le hook de CETTE session, et sinon pourquoi.

    Rend (ligne, "") si la liaison active existe et que son PID (s'il est
    renseigné) est dans l'ascendance du hook ; (ligne, raison) si elle existe
    mais ne vaut pas ici ; (None, raison) sans liaison."""
    if harness not in HARNESSES or not SESSION_RE.match(session_id or ""):
        return None, "pas d'identifiant de session exploitable"
    row = storage.of(db).session_bindings.active(cfg.host, harness, session_id)
    if row is None:
        return None, "session %s %s non liée (ameesh mail bind)" % (harness, session_id)
    pid = row.get("pid")
    if pid:
        try:
            chain = list(ancestors() if chain is None else chain)
        except platform.NotAvailable as exc:
            return row, ("ascendance du hook invérifiable sur cet hôte (%s) : liaison "
                         "--pid sans effet" % exc)
        if int(pid) not in chain:
            return row, ("pid %s de la liaison absent de l'ascendance du hook "
                         "(session reprise ailleurs ? re-liez avec --pid)" % pid)
        if not _same_process(row):
            return row, ("pid %s de la liaison réutilisé par un autre processus (heure de "
                         "démarrage différente) : re-liez avec --pid" % pid)
    return row, ""


def by_ancestry(cfg: Config, db, chain: Iterable[int] | None = None) -> dict | None:
    """La liaison (avec PID) dont le harnais est l'ancêtre le plus proche de ce
    processus — pour `whoami`, `send`, `inbox` lancés DANS une session liée
    (ils ne reçoivent pas l'identifiant de session). None sinon."""
    chain = list(ancestors() if chain is None else chain)
    if not chain:
        return None
    rows = [r for r in storage.of(db).session_bindings.with_pids(cfg.host, chain)
            if _same_process(r)]   # L46 : un PID recyclé ne compte pas
    if not rows:
        return None
    rank = {pid: index for index, pid in enumerate(chain)}
    return min(rows, key=lambda r: (rank.get(int(r["pid"]), len(chain)), -int(r["id"])))
