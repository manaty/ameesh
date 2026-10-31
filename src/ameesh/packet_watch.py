# SPDX-License-Identifier: AGPL-3.0-only
"""#2601: "missed slot" alert for the hourly packets (canon §7, §11 bis).

Constat de l'auditeur (19/10, analyse profonde) : ≈ 4 h 36 sur 24 h sans paquet
en composition ni en CI alors que des lots prêts attendaient. Aucun code ne
compose `paquet-h` (le train reste éteint) ; le composeur n'est réveillé que
par courrier, et quatre trous sur cinq coïncidaient avec un tour du composeur
sur ses propres lots.

Ce module est l'option courte du lot : il ne compose rien, il **réveille**. À
chaque créneau compilé (`mechanics.last_packet`, bloc `packets.cutoffs` du
canon), une fois le délai de grâce passé et jusqu'au créneau suivant :

* la cible est-elle libre ? — aucun lot « Paquet horaire h-… » (même sans
  branche ; titre seul borné au créneau courant ou précédent, pour qu'un
  composeur mort ne taise pas les créneaux suivants) ni porteur
  `integration/paquet*` ouvert pour
  le projet (un paquet en composition ou en CI occupe la cible, §12), et aucun
  porteur fusionné depuis moins que la grâce (le composeur enchaîne), ni
  paquet déjà fusionné pendant le créneau surveillé ;
* la liste calculée (`packet_candidates.inventory`, la même que
  `scripts/paquet-candidats.py`) rend-elle au moins un lot prêt ?

Si oui, une alerte urgente part, une seule fois par créneau, aux destinataires
déclarés (le premier est le seul composeur désigné, les autres sont informés).
Le repli attend au moins la grâce après l’alerte (15 min minimum), sans réponse,
et vérifie en base qu’aucune composition n’a commencé avant de prendre le relais. Aucune écriture hors de l'état local
(`<state_dir>/packet-watch/state.json`) et du courrier.

Configuration de l'hôte (clé `packet_watch`, à côté de `release`) ::

    "packet_watch": {"host": "pc-smichea",
                     "projects": {"ameesh": {"notify": ["<composeur>", "<pilote>"],
                                             "grace_s": 900}}}

Le dépôt, la cible et le clone viennent de `radar.projects.<nom>` (la lecture
de `paquet-candidats`) ; `target` et `app` peuvent les remplacer, et `repo`
nomme le dépôt local d'un hôte sans radar (clone jetable, comme le script). Un
profil radar illisible (`QueueError`) ne fait pas tomber la veille : la cible
`target` sert de repli et l'erreur est consignée (`config_error`). Sans bloc,
rien ne tourne ; sans cadence de paquets au canon, rien n'est inventé.
"""
from __future__ import annotations

import contextlib
import dataclasses
import datetime as _dt
import json
import os
import re
import shutil
import tempfile
import types

from . import mail, work, work_lifecycle as lifecycle

SCHEMA = "ameesh-packet-watch/1"
#: délai laissé au composeur après le créneau avant l'alerte
GRACE_S = 900.0
#: préfixe des branches de porteur de paquet (`paquet-h-…`, `paquet-84-nexlink`)
CARRIER_PREFIX = "integration/paquet"
#: états d'un porteur ouvert
OPEN_STATES = (lifecycle.REQUESTED, lifecycle.IN_PROGRESS, *lifecycle.TO_VALIDATE, lifecycle.BLOCKED, lifecycle.WAITING_HUMAN)
#: porteurs fusionnés relus pour « vient d'être fusionné »
RECENT_MERGED = 50


class WatchError(RuntimeError):
    """Configuration ou lecture impossible : aucune conclusion."""


@dataclasses.dataclass(frozen=True)
class ProjectWatch:
    name: str
    notify: tuple[str, ...]
    grace_s: float = GRACE_S
    target: str = ""
    app: str = ""
    repo: str = ""

    @classmethod
    def from_config(cls, name: str, entry) -> "ProjectWatch":
        if not isinstance(entry, dict):
            raise WatchError("packet_watch.projects.%s: an object is required" % name)
        unknown = set(entry) - {"notify", "grace_s", "target", "app", "repo"}
        if unknown:
            raise WatchError("packet_watch.projects.%s: unknown key(s) %s"
                             % (name, ", ".join(sorted(unknown))))
        notify = entry.get("notify")
        if isinstance(notify, str):
            notify = [notify]
        if not isinstance(notify, list) or not notify \
                or not all(isinstance(n, str) and n.strip() for n in notify):
            raise WatchError("packet_watch.projects.%s.notify: a non-empty list of "
                             "recipients is required (composer and pilot)" % name)
        try:
            grace = float(entry.get("grace_s", GRACE_S))
        except (TypeError, ValueError) as exc:
            raise WatchError("packet_watch.projects.%s.grace_s: a number is required"
                             % name) from exc
        if grace < 0:
            raise WatchError("packet_watch.projects.%s.grace_s: >= 0 is required" % name)
        return cls(name=name, notify=tuple(dict.fromkeys(n.strip() for n in notify)),
                   grace_s=grace, target=str(entry.get("target") or "").strip(),
                   app=str(entry.get("app") or "").strip(),
                   repo=os.path.abspath(os.path.expanduser(str(entry["repo"])))
                   if entry.get("repo") else "")


def projects(cfg) -> dict[str, ProjectWatch]:
    """Les projets surveillés sur CET hôte ; vide sans bloc ou sur un autre hôte."""
    block = getattr(cfg, "packet_watch", None) or {}
    if not isinstance(block, dict):
        raise WatchError("packet_watch: an object is required")
    unknown = set(block) - {"host", "role", "projects"}
    if unknown:
        raise WatchError("packet_watch: unknown key(s) %s" % ", ".join(sorted(unknown)))
    host = block.get("host")
    role = block.get("role") or (getattr(cfg, "release", None) or {}).get("role")
    if role is not None and (not isinstance(role, str) or not role.strip()):
        raise WatchError("packet_watch.role: a non-empty role name is required")
    if not role and host and str(host) != cfg.host:
        return {}
    declared = block.get("projects") or {}
    if not isinstance(declared, dict):
        raise WatchError("packet_watch.projects: an object is required")
    return {name: ProjectWatch.from_config(name, entry)
            for name, entry in sorted(declared.items())}


# --------------------------------------------------------------------------
# créneau
# --------------------------------------------------------------------------

def slot(rules, now: float, grace_s: float) -> dict | None:
    """Le créneau à surveiller à `now`, ou None (pas de cadence, grâce en cours).

    Le créneau est le dernier paquet compilé à `now` ou avant ; il se surveille
    de `slot + grace` jusqu'au créneau suivant, jamais au-delà : le suivant le
    remplace.
    """
    if not getattr(rules, "packet_cutoffs", None):
        return None
    at = float(rules.last_packet(now))
    if now < at + float(grace_s):
        return None
    local = _dt.datetime.fromtimestamp(at, rules.zone())
    return {"at": at, "label": local.strftime("%H:%M"),
            "stamp": local.strftime("%Y%m%d-%H%M"),
            "next": float(rules.next_packet(now))}


# --------------------------------------------------------------------------
# porteurs
# --------------------------------------------------------------------------

def _carrier(row, app: str, target: str = "", *, composing: bool = False) -> bool:
    from .merge_queue import _remote_branch
    if (row.get("app") or "") != app:
        return False
    declared = str(row.get("branch_target") or "")
    if target and declared and _remote_branch(declared) != _remote_branch(target):
        return False
    # Anchored title: a bug about packets is not a composition vehicle.
    return (str(row.get("branch") or "").startswith(CARRIER_PREFIX)
            or (composing and re.match(r"^paquet\s+horaire\s+h-",
                                       str(row.get("title") or "").strip(), re.I)
                is not None))


def carriers(db, app: str, now: float, grace_s: float, *, slot_at: float,
             target: str = "", previous_at: float | None = None) -> dict:
    """Porteurs ouverts, fusions récentes et créneau déjà servi.

    Une fusion dans [slot_at, now] sert le créneau jusqu'au suivant, même
    après la grâce et après un redémarrage du watcher. Seul le jalon effectif
    de fusion fait foi ; modifier un ancien porteur ne sert pas un créneau.

    #3407 N2 : un lot de composition reconnu par son seul titre (aucune
    branche `integration/paquet*`) n'occupe que son créneau. Passé le créneau
    précédent, il ne compte plus comme porteur : un composeur mort ne peut
    donc pas éteindre les créneaux suivants. Un âge illisible est traité de
    même (jamais de silence indéfini).
    """
    from . import storage
    st = storage.of(db).work
    opened = []
    for state in OPEN_STATES:
        for row in st.items(state=state, assignee=None, limit=None):
            if not _carrier(row, app, target, composing=True):
                continue
            if previous_at is not None \
                    and not str(row.get("branch") or "").startswith(CARRIER_PREFIX):
                try:
                    created = float(row.get("created_ts"))
                except (TypeError, ValueError):
                    continue
                if created < float(previous_at):
                    continue
            opened.append(int(row["id"]))
    merged_ids = [int(row["id"])
                  for row in st.items(state=lifecycle.DELIVERY_RECORDED, assignee=None, limit=RECENT_MERGED)
                  if _carrier(row, app, target)]
    recent, served = [], []
    for row in st.delays(RECENT_MERGED, ids=merged_ids) if merged_ids else ():
        try:
            merged = float(row["merged_ts"])
        except (KeyError, TypeError, ValueError):
            continue
        if not merged <= now:
            continue
        ident = int(row["work_item_id"])
        if now - merged < float(grace_s):
            recent.append(ident)
        if slot_at <= merged:
            served.append(ident)
    return {"open": sorted(opened), "recent": sorted(recent), "served": sorted(served)}


# --------------------------------------------------------------------------
# état local
# --------------------------------------------------------------------------

def state_path(cfg) -> str:
    return os.path.join(cfg.state_dir, "packet-watch", "state.json")


def load_state(cfg) -> dict:
    path = state_path(cfg)
    try:
        with open(path, encoding="utf-8") as stream:
            data = json.load(stream)
    except FileNotFoundError:
        return {"schema": SCHEMA, "projects": {}}
    except (OSError, ValueError):
        # un état illisible ne tait jamais l'alerte : au pire elle repart
        return {"schema": SCHEMA, "projects": {}}
    if not isinstance(data, dict) or data.get("schema") != SCHEMA \
            or not isinstance(data.get("projects"), dict):
        return {"schema": SCHEMA, "projects": {}}
    return data


def save_state(cfg, state: dict) -> None:
    path = state_path(cfg)
    folder = os.path.dirname(path)
    os.makedirs(folder, mode=0o700, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="state.", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


# --------------------------------------------------------------------------
# passage
# --------------------------------------------------------------------------

def _inventory(cfg, db, watch: ProjectWatch, app: str) -> dict:
    """La liste calculée de `paquet-candidats`, dans le clone privé du radar."""
    from . import merge_queue, packet_candidates
    try:
        project = merge_queue.declared_projects(cfg).get(watch.name)
    except merge_queue.QueueError as exc:
        raise WatchError(str(exc)) from exc
    declared = project is not None
    if not declared:
        if not watch.repo:
            raise WatchError("project %r is in neither radar.projects nor "
                             "packet_watch.projects.%s.repo: the candidate list cannot "
                             "be read" % (watch.name, watch.name))
        # comme `paquet-candidats` hors radar : un clone jetable du dépôt local
        project = types.SimpleNamespace(name=watch.name, target="", repo=watch.repo)
    repo = watch.repo or project.repo
    clone, scratch = merge_queue.working_clone(cfg, repo, project if declared else None)
    try:
        return packet_candidates.inventory(db, project, repo=repo, clone=clone,
                                           target=watch.target or None, app=app, cfg=cfg)
    except packet_candidates.CandidateError as exc:
        raise WatchError(str(exc)) from exc
    finally:
        if scratch:
            shutil.rmtree(scratch, ignore_errors=True)


def body(watch: ProjectWatch, at: dict, payload: dict, *, now: float | None = None) -> str:
    by_id = {entry["work_item"]: entry for entry in payload.get("candidates") or ()}
    lines = [
        "Créneau de paquet %s manqué (%s) : cible %s @ %s libre, aucun porteur "
        "integration/paquet* ni lot Paquet horaire h-* ouvert, %d lot(s) prêt(s)."
        % (at["label"], watch.name, payload.get("target"),
           str(payload.get("base") or "")[:12], len(payload.get("ready") or ())),
        "",
    ]
    for lot in payload.get("ready") or ():
        entry = by_id.get(lot) or {}
        lines.append("  #%-5d %s  %s" % (lot, str(entry.get("source")
                                                  or entry.get("published_head") or "")[:12],
                                         entry.get("branch") or ""))
    if payload.get("errors"):
        # #3813 : une erreur propre à un lot laisse les autres composables.
        lines += ["", "Lectures impossibles (lots à écarter) :"]
        lines += ["  %s" % error for error in payload["errors"]]
    fallback_at = _dt.datetime.fromtimestamp(
        (now if now is not None else float(at["at"]) + watch.grace_s)
        + max(watch.grace_s, GRACE_S), _dt.timezone.utc).isoformat()
    lines += ["", "Composeur désigné : %s (premier destinataire notify)." % watch.notify[0],
              "Les autres destinataires ne composent pas en parallèle. Repli seulement "
              "après %s, sans réponse du composeur, et après vérification en base "
              "qu'aucun lot de composition n'est ouvert ; le pilote désigne alors "
              "un seul remplaçant." % fallback_at,
              "Geste pour %s : composer integration/paquet-h-%s maintenant depuis "
                  "`scripts/paquet-candidats.py` (canon §11 bis, §12), écarts motivés "
                  "au manifeste. Une seule alerte par créneau (#2601)."
              % (watch.notify[0], at["stamp"])]
    return "\n".join(lines)


def _paused(db, watch: ProjectWatch, target: str, now: float) -> dict:
    """#4466 : la pause déclarée du paquet horaire de la cible (`packet.pause`,
    #4361), `{"pause": …}` si elle court encore ou s'est échue depuis moins
    que la grâce (le tic reprend à son prochain passage), sinon `{}`.

    Pendant une suspension voulue (staging hors créneau), aucun créneau n'est
    manqué et aucun composeur n'est désigné. Une pause levée (`resumed`) rend
    la main tout de suite. Un journal illisible ne tait rien
    (`{"error": …}`, l'alerte part comme avant #4466).
    """
    if not target:
        return {}
    from . import packet_pause
    try:
        data = packet_pause.latest(db, project=watch.name, target=target)
    except Exception as exc:  # noqa: BLE001 — journal illisible : aucune pause lue
        return {"error": "%s: %s" % (type(exc).__name__, exc)}
    if packet_pause.active_of(data, float(now) - watch.grace_s) is None:
        return {}
    return {"pause": {"until": data.get("until"), "reason": data.get("reason") or "",
                      "by": data.get("by") or "", "target": target}}


def check(cfg, db, watch: ProjectWatch, rules, *, now: float, state: dict,
          inventory=None, send=None, dry_run: bool = False) -> dict:
    """Un passage pour un projet ; rend `{result, …}` et met `state` à jour.

    `result` : `no-cadence`, `grace`, `already-alerted`, `paused` (pause
    déclarée de la cible, #4466), `busy` (porteur ouvert ou tout juste
    fusionné), `served`, `nothing-ready`, `alerted`, `would-alert`.
    """
    app = watch.app or watch.name
    at = slot(rules, now, watch.grace_s)
    if at is None:
        return {"project": watch.name,
                "result": "grace" if getattr(rules, "packet_cutoffs", None) else "no-cadence"}
    out = {"project": watch.name, "slot": at["label"], "slot_at": at["at"]}
    mine = state.setdefault("projects", {}).setdefault(watch.name, {})
    if float(mine.get("last_alert_slot") or 0) >= at["at"]:
        return dict(out, result="already-alerted")
    from . import merge_queue
    config_error = ""
    try:
        project = merge_queue.declared_projects(cfg).get(watch.name)
    except merge_queue.QueueError as exc:
        # #3407 N3 : un profil radar incompatible ne fait plus tomber la
        # veille en `result=error` ; la cible explicite reste le repli.
        project = None
        config_error = "QueueError: %s" % exc
    if config_error:
        out["config_error"] = config_error
    target = watch.target or (getattr(project, "target", "") if project else "")
    previous_at = float(rules.last_packet(at["at"] - 1)) \
        if getattr(rules, "packet_cutoffs", None) else None
    paused = _paused(db, watch, target, now)
    if paused.get("pause"):
        return dict(out, result="paused", pause=paused["pause"])
    if paused.get("error"):
        out["pause_error"] = paused["error"]
    held = carriers(db, app, now, watch.grace_s, slot_at=at["at"], target=target,
                    previous_at=previous_at)
    if held["served"]:
        return dict(out, result="served", carriers=held)
    if held["open"] or held["recent"]:
        return dict(out, result="busy", carriers=held)
    payload = (inventory or (lambda: _inventory(cfg, db, watch, app)))()
    ready = list(payload.get("ready") or ())
    out.update(ready=ready, errors=list(payload.get("errors") or ()),
               base=payload.get("base"))
    if not ready:
        return dict(out, result="nothing-ready")
    text = body(watch, at, payload, now=now)
    if dry_run:
        return dict(out, result="would-alert", body=text, notify=list(watch.notify))
    deliver = send or (lambda to, message, data: mail.send(
        db, work.SYSTEM_SENDER, to, message, kind="event", payload=data))
    data = {"packet_slot_missed": {"project": watch.name, "slot": at["label"],
                                   "slot_at": at["at"], "ready": ready,
                                   "target": payload.get("target"),
                                   "base": payload.get("base")},
            "urgent": True}
    sent, failed = [], []
    for recipient in watch.notify:
        try:
            deliver(recipient, text, data)
            sent.append(recipient)
        except Exception as exc:  # noqa: BLE001 - un destinataire n'empêche pas l'autre
            failed.append("%s: %s" % (recipient, exc))
    if sent:
        mine["last_alert_slot"] = at["at"]
        mine["last_alert_ready"] = ready
    return dict(out, result="alerted" if sent else "not-delivered",
                notify=sent, failed=failed)


def tick(cfg, db, *, now: float, rules_of, dry_run: bool = False,
         only: str | None = None, gh=None) -> list[dict]:
    """Tous les projets surveillés de l'hôte ; `rules_of(name)` rend la
    mécanique compilée (`release_cutoff.compiled_rules`, sans repli).

    #2813 : le même passage fusionne au vert un porteur publié, ou le désarme
    si sa tête ou sa cible a changé depuis la publication
    (`packet_automerge.watch`) ; seuls une fusion, un désarmement et une
    erreur sont rendus, un porteur en attente ne dit rien ici (son motif est
    au journal du lot, `auto-merge held …`, #3175)."""
    declared = projects(cfg)
    if only:
        if only not in declared:
            raise WatchError("project %r is not declared under packet_watch.projects" % only)
        declared = {only: declared[only]}
    if not declared:
        return []
    state = load_state(cfg)
    results = []
    for name, watch in declared.items():
        try:
            results.append(check(cfg, db, watch, rules_of(name), now=now, state=state,
                                 dry_run=dry_run))
        except Exception as exc:  # noqa: BLE001 - un projet ne fait pas tomber l'autre
            results.append({"project": name, "result": "error",
                            "error": "%s: %s" % (type(exc).__name__, exc)})
        results += _auto_merge(cfg, db, name, watch.app or name, gh=gh, dry_run=dry_run,
                               checkout=watch.repo)
    if not dry_run:
        save_state(cfg, state)
    return results


def _auto_merge(cfg, db, name: str, app: str, *, gh, dry_run: bool,
                checkout: str = "") -> list[dict]:
    """#2813 : les fusions, désarmements et erreurs des porteurs publiés de `app`."""
    from . import packet_automerge
    try:
        rows = packet_automerge.watch(cfg, db, app, gh=gh, dry_run=dry_run,
                                      checkout=checkout)
    except Exception as exc:  # noqa: BLE001 - l'alerte de créneau reste rendue
        return [{"project": name, "result": "error",
                 "error": "auto-merge: %s: %s" % (type(exc).__name__, exc)}]
    out = []
    for row in rows:
        if row["result"] == "error":
            out.append({"project": name, "result": "error", "lot": row["lot"],
                        "error": "auto-merge of #%d: %s" % (row["lot"], row.get("error"))})
        elif row["result"] in ("disarmed", "would-disarm", "merged", "would-merge"):
            out.append(dict(row, project=name, result="auto-merge-" + row["result"]))
    return out
