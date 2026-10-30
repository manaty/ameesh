# SPDX-License-Identifier: AGPL-3.0-only
"""#3256 A : remplir la file — le lot de découpage (spec
docs/design/3256-debit-file.md §2, canon §4).

Le 28/10, 32 agents sur une quarantaine étaient au repos, `idle_capacity`
relevait 0 lot démarrable, et aucun rôle n'avait pour tâche de remplir la
file. Ce module est ce geste, déterministe (§7 du canon), joué par la passe
`take` du notifieur :

* **déclencheur** : dans un projet, au moins `file.repos_part` des agents
  réveillables sont au repos et le projet n'a aucun lot démarrable, sans
  interruption depuis `file.repos_s` (bloc mécanique du canon, `mechanics`).
  Bloc absent : rien ne se déclenche ;
* **geste** : un lot `evolution` P1 « Découper la feuille de route de
  <projet> : des lots démarrables pour N agents », assigné au pilote du
  projet ; un seul ouvert par projet (`source` = `file-decoupage:<projet>`,
  verrou consultatif de transaction) ;
* **repli** : une heure après sa création sans lot démarrable du projet créé
  depuis, l'auditeur reçoit UN courrier (marque `repli auditeur` sur le lot)
  et crée les lots au niveau 1 ;
* **feuille de route épuisée** : une note `roadmap: épuisée <projet>` sur le
  lot de découpage ouvert met en sommeil les agents d'exécution au repos du
  projet (pause opérateur posée par `ameesh:file-decoupage`, l'humain
  responsable n'étant que le principal, raison `SLEEP_REASON`, levée
  conditionnelle), jamais un relecteur ni un agent critique ; l'alerte
  `roadmap_exhausted` (une par lot, dédoublonnée par le notifieur) prévient
  l'owner ; les hôtes burst sont rendus par le drainage de #3167 B (#4148),
  hors de ce module. Le premier lot démarrable du projet réveille les
  dormeurs. #4525 : une relecture désignée ouverte, ou le dernier relecteur
  disponible d'une famille pendant qu'un gel attend un avis, n'est jamais
  endormi ; le motif d'exemption est journalisé sur le lot de découpage.
  #4610 : un agent à qui un lot actionnable est attribué, ou qui a reçu un
  courrier non lu de son pilote ou d'un orchestrateur, n'est pas endormi (le
  courrier compte s'il a moins de `file.wake_mail_s`, à défaut `file.repos_s`,
  secondes) ; la pause est levée dès qu'un de ces deux événements arrive,
  avec l'acteur et la raison au journal de la pause. #4619 : la même fenêtre
  de courrier s'applique au sommeil et à la levée, et le journal nomme le lot
  réellement actionnable, pas seulement un lot assigné.
* **#4623, la dernière note fait foi** : une note `roadmap: rouverte <projet>`
  postérieure à la dernière `roadmap: épuisée <projet>` désarme le sommeil et
  lève les dormeurs du projet dans la même passe (le geste est journalisé sur
  le lot de découpage, avec l'auteur et la note) ; `roadmap_exhausted` suit la
  même règle. Une note de réouverture antérieure à la dernière note
  d'épuisement ne change rien.

« Au repos » se lit comme `idle_capacity` (#1677 : sans lot actionnable) ;
la durée de 30 min porte sur la condition du projet, suivie dans l'état du
notifieur (`state["queue_fill"]`) : un agent sans lot prend des tours de
courrier toutes les quelques minutes, son dernier tour ne dit pas son repos.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from . import planned_waits, storage
from . import sous_utilisation as su
from . import work
from .agent_pause import FILE_DECOUPAGE_ACTOR as SLEEP_ACTOR

#: identité du lot de découpage d'un projet (un seul ouvert)
SOURCE_PREFIX = "file-decoupage:"
#: repli de l'auditeur : une heure après la création (§2.2, 3)
FALLBACK_S = 3600.0
#: marque du repli, posée une fois sur le lot de découpage
FALLBACK_MARK = "repli auditeur"
#: la note du pilote (§1, « Feuille de route épuisée »)
EXHAUSTED_PREFIX = "roadmap: épuisée"
EXHAUSTED_RE = re.compile(r"^\s*roadmap\s*:\s*épuisée\s+(\S+)", re.IGNORECASE)
#: #4623 : la note qui rouvre la feuille de route ; deux orthographes déclarées
REOPENED_PREFIX = "roadmap: rouverte"
REOPENED_RE = re.compile(r"^\s*roadmap\s*:\s*r(?:é)?ouverte\s+(\S+)", re.IGNORECASE)
#: toutes les notes de feuille de route, lues d'un seul préfixe : les deux
#: formes sont ensuite départagées par leur ordre dans le journal
ROADMAP_PREFIX = "roadmap:"
#: raison de la pause opérateur posée par le sommeil, levée par elle seule
SLEEP_REASON = "roadmap: épuisée %s (#3256 §2.3)"
#: #4623 : raison journalisée à la levée automatique due à une réouverture
REOPEN_REASON = "roadmap: rouverte %s (#3256 §2.3)"
#: #4525 : reconnaissance de CETTE pause, pour l'alerte (une raison humaine qui
#: ressemblerait au texte ne doit pas être prise pour le sommeil automatique) ;
#: le projet est capturé pour retrouver les dormeurs d'un projet sans mesure
SLEEP_REASON_RE = re.compile(r"^roadmap\s*:\s*épuisée\s+(\S+)\s+\(#3256\s+§2\.3\)$")
#: #4623 : marque de la levée journalisée sur le lot de découpage, une fois
#: par note de réouverture
REOPEN_MARK = "sleep lifted: roadmap reopened"
#: préfixe de la note d'exemption écrite sur le lot de découpage
SLEEP_EXEMPT_PREFIX = "sleep exempted:"
#: #4610 : préfixe de la raison de levée automatique, au journal des pauses
SLEEP_LIFT_PREFIX = "automatic sleep lifted:"
#: #4610 : courriers relus par agent pour retrouver le dernier envoi du pilote
#: ou d'un orchestrateur (les plus récents d'abord, `mail.history`)
MAIL_HISTORY_LIMIT = 200
#: #4619 : non-lus actifs relus en plus de l'historique (du plus ancien au plus
#: récent, `mail.unread_active`) : un courrier pertinent plus vieux que les
#: derniers courriers lus ou passifs ne doit pas être masqué. L'opération est
#: au contrat médié (contrairement à `mail.unread`), donc le repli garde la
#: même lecture sur un exécuteur médié.
MAIL_UNREAD_LIMIT = 200
#: état du notifieur : `{projet: début de la condition}`
STATE_KEY = "queue_fill"


def is_sleep_reason(reason) -> bool:
    """#4525 : la raison est-elle exactement celle du sommeil automatique ?

    L'alerte de pause opérateur s'en sert pour ne pas demander à l'owner de
    mettre fin à une maintenance qui n'a jamais eu lieu, et pour dire que le
    premier lot démarrable lève le sommeil.
    """
    return bool(SLEEP_REASON_RE.match(str(reason or "").strip()))


def project_of_sleep_reason(reason) -> str:
    """#4610 : le projet que porte la raison du sommeil ("" si inconnu)."""
    match = SLEEP_REASON_RE.match(str(reason or "").strip())
    return match.group(1) if match else ""


def source_of(project: str) -> str:
    return SOURCE_PREFIX + project


def is_split_lot(item: dict | None) -> bool:
    """#4756 : un lot de découpage (`file-decoupage:<projet>`) est un geste du
    pilote : hors file d'équipe (#540) à son attribution, jamais pris par un
    exécutant au repos (L165)."""
    return str((item or {}).get("source") or "").startswith(SOURCE_PREFIX)


def title_of(project: str, count: int) -> str:
    return ("Découper la feuille de route de %s : des lots démarrables pour %d agent(s)"
            % (project, count))


def params(cfg):
    """Le bloc `file` compilé du canon appliqué, ou None (absent, illisible) :
    sans paramètres déclarés, rien ne se déclenche."""
    from . import mechanics
    try:
        return mechanics.configured(cfg).queue_fill
    except Exception:  # noqa: BLE001 - canon illisible : aucun geste supposé
        return None


@dataclass
class Measure:
    project: str
    wakeable: list = field(default_factory=list)
    idle: list = field(default_factory=list)
    startable: list = field(default_factory=list)

    def share(self) -> float:
        return len(self.idle) / len(self.wakeable) if self.wakeable else 0.0

    def to_dict(self) -> dict:
        return {"project": self.project, "wakeable": list(self.wakeable),
                "idle": list(self.idle), "startable": list(self.startable),
                "share": round(self.share(), 3)}


def wakeable(listing: list, canons, *, configured: bool) -> list:
    """§1 : les agents d'exécution réveillables — mode `execute`, ni
    éphémères, ni arrêtés, ni en pause (budget ou opérateur), ni chat de
    l'owner, ni critiques ou relecteurs (rôles critiques du canon ; un
    verdict inconnu vaut critique, `critical_verdict`). Un agent retiré
    (`retired_ts` : le retrait laisse statut, mode et bail) ou sans exécuteur
    vivant n'est pas une capacité : il diluerait la part au repos."""
    from .chat import is_chat_agent
    out = []
    for row in listing:
        if row.get("ephemeral") or is_chat_agent(row) or row.get("retired_ts"):
            continue
        if not su._awake_runner(row):
            continue
        if (row.get("mode") or "execute") != "execute":
            continue
        if su._state(row) in ("stopped", "paused"):
            continue
        critical, _unknown = su.critical_verdict(row, canons, configured=configured)
        if critical:
            continue
        out.append(row)
    return out


def measure(listing: list, lots: list, waits: dict, now: float, canons, *,
            configured: bool, footprint: dict | None = None) -> dict:
    """`{projet: Measure}` pour chaque projet d'au moins un agent réveillable.

    Au repos : `idle_agents` (sans lot actionnable, #1677). Démarrable : un
    lot ouvert sans assigné ni exécutant externe, hors file d'amélioration,
    qu'aucune attente planifiée ne retient et que la garde d'empreinte
    n'occupe pas (`split_startable`, #3185/#4699) ; `footprint` est la
    lecture partagée `sous_utilisation.footprint_blockers` (#4717)."""
    from . import projects as projects_mod
    pool = wakeable(listing, canons, configured=configured)
    names = {row["name"] for row in pool}
    idle = {row["name"] for row in su.idle_agents(listing, now, 0.0, waiting_free=True,
                                                  actionable_idle=True)
            if row["name"] in names}
    startable, _blocked = su.split_startable(su.waiting_project_lots(lots), waits,
                                             footprint)
    out: dict = {}
    for row in pool:
        for project in projects_mod.agent_projects(row):
            item = out.setdefault(project, Measure(project))
            item.wakeable.append(row["name"])
            if row["name"] in idle:
                item.idle.append(row["name"])
    for lot in startable:
        project = projects_mod.lot_project(lot)
        if project in out:
            out[project].startable.append(int(lot["id"]))
    for item in out.values():
        item.wakeable.sort()
        item.idle.sort()
        item.startable.sort()
    return out


def held(measures: dict, since: dict, now: float, part: float, repos_s: float) -> tuple:
    """(projets dont la condition tient depuis `repos_s`, nouvel état).

    Condition : au moins `part` des réveillables au repos ET aucun lot
    démarrable. L'état garde le début de chaque condition en cours ; une
    condition interrompue repart de zéro."""
    state = {}
    due = []
    for project, item in sorted(measures.items()):
        if not item.wakeable or item.startable or item.share() < part:
            continue
        start = float(since.get(project) or now)
        state[project] = start
        if now - start >= repos_s:
            due.append(project)
    return due, state


#: états rendus par `roadmap_state`
EXHAUSTED_STATE = "exhausted"
REOPENED_STATE = "reopened"


def _roadmap_order(notes: list) -> list:
    """Les notes dans l'ordre du journal quand il est lisible (#4623).

    Les lignes de `events_noted` portent leur `id`, monotone pour un lot ; une
    liste sans `id` (appel direct, test) garde son ordre d'entrée, qui est
    celui du journal par contrat."""
    rows = list(notes or ())
    if rows and all(row.get("id") is not None for row in rows):
        rows.sort(key=lambda row: int(row.get("id") or 0))
    return rows


def roadmap_notes(db, item_id: int) -> list:
    """Les notes de feuille de route du lot, de la plus ancienne à la plus
    récente (#4623).

    Un seul préfixe (`roadmap:`) suffit : les formes épuisée/rouverte sont
    départagées par `roadmap_state`, dans l'ordre du journal (`id` d'événement,
    stable et monotone pour un lot)."""
    return _roadmap_order(work.events_noted(db, int(item_id), ROADMAP_PREFIX))


def roadmap_state(notes: list, project: str) -> str | None:
    """L'état de la feuille de route de `project` : `exhausted`, `reopened`,
    ou None sans note qui nomme le projet (#4623).

    `notes` est lue dans l'ordre chronologique et **la dernière note qui nomme
    le projet fait foi** : une réouverture postérieure à la dernière note
    d'épuisement désarme le sommeil, et inversement. C'est ce que le pilote
    attend en notant `roadmap: rouverte <projet>` ; avant #4623, la seule
    présence d'une note d'épuisement rendait le sommeil définitif jusqu'au
    classement du lot de découpage."""
    state = None
    for row in _roadmap_order(notes):
        note = str((row or {}).get("note") or "")
        match = EXHAUSTED_RE.match(note)
        if match and match.group(1).strip(".,;:") == project:
            state = EXHAUSTED_STATE
            continue
        match = REOPENED_RE.match(note)
        if match and match.group(1).strip(".,;:") == project:
            state = REOPENED_STATE
    return state


def exhausted(notes: list, project: str) -> bool:
    """La dernière note de feuille de route nommant le projet est-elle
    `roadmap: épuisée <projet>` ? (#4623 : une réouverture postérieure rend
    faux, même si une note d'épuisement plus ancienne figure dans `notes`.)"""
    return roadmap_state(notes, project) == EXHAUSTED_STATE


def sleeping_projects(listing: list) -> set:
    """Les projets dont au moins un agent porte la pause du sommeil (#4623).

    `measure` écarte les agents en pause : sans cette union, un projet dont
    tous les exécutants dorment sortirait de la passe, et la note de
    réouverture ne serait jamais lue."""
    from . import projects as projects_mod
    out = set()
    for row in listing or ():
        if not row.get("operator_paused"):
            continue
        match = SLEEP_REASON_RE.match(
            str(row.get("operator_pause_reason") or "").strip())
        if not match:
            continue
        project = match.group(1)
        if project in projects_mod.agent_projects(row):
            out.add(project)
    return out


def body_of(project: str, item: Measure, since_ts: float, now: float,
            losses: list) -> str:
    lines = [
        "Lot de découpage (#3256 A, canon §4) : la file de %s est vide alors que la "
        "capacité est là." % project,
        "",
        "Mesure : %d agent(s) réveillable(s) au repos sur %d (%d %%), depuis %s, "
        "sans lot démarrable : %s." % (
            len(item.idle), len(item.wakeable), round(100 * item.share()),
            su._duration(now - since_ts), ", ".join(item.idle)),
    ]
    if losses:
        lines.append("Quota perdu au reset si rien ne change : %s." % "; ".join(losses))
    lines += [
        "Feuille de route : la source déclarée du projet (docs/ROADMAP.md, décisions "
        "owner `ameesh decide`).",
        "",
        "Attendu du pilote : des lots démarrables, d'au plus une journée chacun et de "
        "classe conforme au §1 du canon, pour %d agent(s) ; ce lot se ferme quand la "
        "file en compte assez. Rien à découper : notez « %s %s » avec sa source "
        "et laissez ce lot ouvert (les agents en trop passent en sommeil jusqu'au "
        "prochain lot démarrable, l'owner reçoit une alerte). Pour rouvrir : notez "
        "« %s %s » sur ce lot, le sommeil est levé à la passe suivante." % (
            len(item.idle), EXHAUSTED_PREFIX, project, REOPENED_PREFIX, project),
        "Repli : sans lot démarrable créé dans l'heure, l'auditeur crée les lots.",
    ]
    return "\n".join(lines)


def _losses(db, now: float) -> list:
    """« claude/primaire five_hour 78 % » : la part de fenêtre perdue au reset
    si rien ne change, par jauge de compte de forfait (`Gauge.expiring`, L74) ;
    vide si illisible."""
    out = []
    try:
        gauges = su.latest_account_gauges(db, now)
    except Exception:  # noqa: BLE001 - le corps se passe de la perte
        return out
    for (harness, account), items in sorted(gauges.items(), key=lambda kv: str(kv[0])):
        for gauge, _observed in items:
            try:
                lost = su.expiring(gauge, now)
            except Exception:  # noqa: BLE001
                continue
            if lost and lost[1] > 0:
                out.append("%s/%s %s %d %%" % (harness, account or "default", gauge.key,
                                               round(100 * float(lost[1]))))
    return out[:6]


def _open_split(db, project: str):
    return work.open_with_source(db, source_of(project))


def _create(cfg, db, project: str, item: Measure, since_ts: float, now: float, *,
            listing: list, canons, actor: str) -> dict:
    """Crée le lot de découpage, assigné au pilote, sous un verrou par projet :
    deux passes concurrentes n'en ouvrent qu'un."""
    from . import alert_briefs
    pilots = alert_briefs.project_pilots(project, listing, cfg=cfg, db=db, canons=canons)
    pilot = pilots[0] if pilots else None
    source = source_of(project)
    with db.transaction() as tx:
        tx.query("SELECT pg_advisory_xact_lock(hashtextextended(lower(%s), 0))",
                 (source,))
        existing = _open_split(tx, project)
        if existing is not None:
            return {"lot": int(existing["id"]), "created": False}
        created = storage.of(tx).work.add(
            type="evolution", source=source, app=project,
            title=title_of(project, len(item.idle)),
            body=body_of(project, item, since_ts, now, _losses(tx, now)),
            issue_ref=None, workstream=None, assignee=None, budget_usd=None,
            note="created (#3256 A: %d/%d agents idle, no startable lot)"
                 % (len(item.idle), len(item.wakeable)),
            actor=actor, priority=1)
    lot = int(created["id"])
    out = {"lot": lot, "created": True, "pilot": pilot}
    if pilot:
        try:
            work.assign(db, lot, pilot, actor=actor)
        except Exception as exc:  # noqa: BLE001 - le lot reste ouvert, le pilote le voit
            out["assign_error"] = "%s: %s" % (type(exc).__name__, str(exc)[:160])
        else:
            # #4756 : une attribution sans erreur qui n'a pas posé l'assigné
            # (file d'équipe, #540) est aussi un refus.
            row = work.get(db, lot) or {}
            if (row.get("assignee") or "").strip() != pilot:
                out["assign_error"] = "not assigned (now: %s)" % (
                    (row.get("assignee") or "").strip() or "nobody")
    if out.get("assign_error"):
        _tell_refusal(db, lot, project, pilot, out["assign_error"], actor=actor)
    return out


def _tell_refusal(db, lot: int, project: str, pilot: str | None, why: str, *,
                  actor: str) -> None:
    """#4756 : un refus d'attribution du lot de découpage est dit au pilote
    (courrier et journal du lot) au lieu de rester dans le rendu du passage ;
    sans pilote, le journal le dit et le repli de l'auditeur suit."""
    from . import mail
    text = ("lot de découpage #%d de %s non attribué au pilote %s : %s"
            % (lot, project, pilot or "(aucun)", why))
    if pilot:
        mail.send(db, actor, pilot,
                  "%s. Prenez-le (ameesh work assign %d %s) ou dites qui le prend ; "
                  "il n'est pas pris par un exécutant au repos (#4756)."
                  % (text, lot, pilot),
                  work_item_id=str(lot), idempotency_key="file-decoupage-refus-%d" % lot)
    work.note(db, lot, text, actor=actor)


#: la note d'attribution d'un lot qui n'avait pas d'assigné (`work.assign`)
TAKEN_FROM_NOBODY = "(before: nobody)"


def _created_since(db, lots: list, project: str, split: dict, waits: dict) -> list:
    """Les lots du projet arrivés démarrables depuis le lot de découpage :
    créés depuis, qu'aucune attente ne retient, et créés SANS assigné —
    encore sans assigné, ou pris depuis (`assigned to … (before: nobody)`).
    Un lot créé déjà assigné n'est pas passé par la file."""
    from . import projects as projects_mod
    start = float(split.get("created_ts") or 0.0)
    out = []
    for lot in lots:
        ident = int(lot["id"])
        if (ident == int(split["id"]) or ident in waits
                or projects_mod.lot_project(lot) != project
                or float(lot.get("created_ts") or 0.0) < start):
            continue
        if not (lot.get("assignee") or "").strip():
            out.append(ident)
            continue
        taken = work.events_noted(db, ident, "assigned to")
        if any(TAKEN_FROM_NOBODY in (row.get("note") or "") for row in taken):
            out.append(ident)
    return out


def _fallback(db, project: str, split: dict, created: list, now: float, *,
              actor: str) -> dict | None:
    """Repli (§2.2, 3) : un seul courrier à l'auditeur, marqué sur le lot."""
    from . import mail
    from .alert_briefs import AUDITOR_NAME
    lot = int(split["id"])
    if created or now - float(split.get("created_ts") or now) < FALLBACK_S:
        return None
    if work.events_noted(db, lot, FALLBACK_MARK):
        return None
    body = ("Repli du lot de découpage #%d (#3256 A, canon §4) : créé il y a %s, il n'a "
            "donné lieu à aucun lot démarrable de %s. Geste de niveau 1 : créez vous-même "
            "les lots démarrables (au plus une journée chacun, classe du §1), sans toucher "
            "au code ni à la priorité des lots du pilote ; puis notez-les sur #%d."
            % (lot, su._duration(now - float(split["created_ts"])), project, lot))
    mail.send(db, actor, AUDITOR_NAME, body, work_item_id=str(lot),
              idempotency_key="file-decoupage-repli-%d" % lot)
    work.note(db, lot, "%s : aucun lot démarrable créé en %s, %s prévenu"
              % (FALLBACK_MARK, su._duration(now - float(split["created_ts"])),
                 AUDITOR_NAME), actor=actor)
    return {"lot": lot, "auditor": AUDITOR_NAME}


def _owner_of(rows: list) -> str | None:
    owner = su._common_responsible(rows)
    if owner and not str(owner).startswith("human:"):
        owner = "human:%s" % owner
    return owner


def _reviewer_spares(db, project: str, lots: list, listing: list, *,
                     cfg=None, canons=None) -> tuple:
    """#4525 : qui le sommeil doit-il épargner, et pourquoi ?

    Rend `(pending, keep, error)` :

    * `pending` : `{nom minuscule: [ligne de pending_reviews]}`. Une ligne est
      une relecture **désignée ouverte** sur le dernier gel d'un lot ouvert
      (le calcul partagé #3033 : le gel, la désignation et l'absence de
      verdict de ce relecteur sur cette tête) ;
    * `keep` : `{nom: (famille, lot)}` pour le **dernier relecteur
      disponible** d'une famille qui peut encore relire **un gel en attente**.
      Le calcul est fait gel par gel : deux gels peuvent exclure des noms
      différents de la même famille et avoir besoin de relecteurs différents.
      Les lots ouverts en `qa` portent un gel ; les auteurs de ce gel (lus à
      la marque de désignation, `frozen_authors`) et leur famille sont
      inéligibles. Un agent d'une famille dont il ne reste que lui est
      épargné, sinon le sommeil retirerait la famille entière et le gel
      n'aurait plus de relecteur possible (constat #4507 / #4496) ;
    * `error` : la lecture des relectures désignées a échoué. Le sommeil ne
      retire alors personne : le doute ne supprime pas un relecteur.

    Le calcul reste prudent : sans marque de désignation complète, les
    familles d'auteurs sont inconnues et toutes les familles restent
    éligibles ; une lecture de gel en échec ne fait qu'épargner davantage.

    « Dernier relecteur disponible » se lit sur les agents **disponibles** du
    projet (`review_designation.is_available`), pas sur les seuls agents au
    repos : un agent occupé par un lot reste un relecteur possible.
    """
    from . import alert_briefs
    from . import plan_git
    from . import projects as projects_mod
    from . import registry
    from . import review_designation as rd
    from . import review_designation_activation as activation

    try:
        rows = activation.pending_reviews(db)
    except Exception as exc:  # noqa: BLE001 - le doute ne retire pas un relecteur
        return {}, {}, "%s: %s" % (type(exc).__name__, str(exc)[:160])
    pending: dict = {}
    for row in rows or ():
        name = str((row or {}).get("name") or "").strip()
        if name:
            pending.setdefault(name.lower(), []).append(row)

    # Les familles réelles du registre : la politique du canon n'est pas
    # relue ici, un harnais hors politique est simplement épargné en trop.
    families = set()
    for row in listing or ():
        try:
            family = str(registry.live_harness(dict(row)) or "").strip().lower()
        except Exception:  # noqa: BLE001 - une ligne illisible n'est pas une famille
            family = ""
        if family:
            families.add(family)
    try:
        closed = alert_briefs.claims_closed_hosts(db)
    except Exception:  # noqa: BLE001 - hôte inconnu : ne ferme rien
        closed = set()
    # #4779 : un profil de relecture illisible (capacités ou rôles absents du
    # registre) n'est pas une preuve de capacité : l'agent n'entre pas dans le
    # vivier des relecteurs, exactement comme la désignation le refuse. Le
    # doute est borné à cet agent : un seul profil illisible ne rend pas toute
    # l'exemption illisible et ne bloque pas le sommeil des autres (régression
    # test_4610) ; la trace du refus est celle du gel (`prepare`).
    try:
        candidates = [c for c in rd.candidates_from_registry(
            listing or (), project=project, families=families, closed_hosts=closed)
            if c.available and c.family]
    except Exception:  # noqa: BLE001 - pas de vivier : rien à épargner en plus
        candidates = []
    by_name = {c.name: c for c in candidates}
    # Le pilote du projet reçoit l'alerte de capacité, il n'est pas un siège de
    # relecture : la désignation l'exclut (`_candidates`), le sommeil doit donc
    # l'exclure aussi du choix du représentant de famille. Sinon un pilote au
    # nom « max » serait épargné et le vrai relecteur de la famille endormi
    # (B/P2 de la relecture cx2, #4525).
    try:
        pilots = alert_briefs.project_pilots(
            project, listing or (), cfg=cfg, db=db, canons=canons)
    except Exception:  # noqa: BLE001 - rôle illisible : ne retire personne
        pilots = []
    if pilots:
        by_name.pop(pilots[0], None)

    waiting = [lot for lot in lots or ()
               if str((lot or {}).get("state") or "") == "qa"
               and projects_mod.lot_project(lot or {}) == project]
    # Un représentant par (gel, famille), pas un représentant global : deux
    # gels peuvent exclure des noms différents de la même famille (auteurs,
    # correcteurs) et avoir besoin de relecteurs différents. Agréger les noms
    # puis garder le max global laisserait l'un des gels sans relecteur
    # (B/P2 de la relecture cx2, second tour #4525).
    keep: dict = {}
    for lot in waiting:
        lot = lot or {}
        try:
            lot_id = int(lot.get("id"))
        except (TypeError, ValueError):
            continue
        # Une dispense `-none` ou une marque `review-designation-none` dit que
        # ce gel n'attend aucun avis avant fusion (lot léger, #2561/#1979) :
        # le sommeil n'a alors pas de relecteur à épargner pour lui.
        if any(str(name).strip() == "-none" for name in lot.get("reviewers") or ()):
            continue
        try:
            freeze = plan_git.latest_freeze(work.milestones(db, lot_id, None) or ())
            if freeze is None:
                continue
            if work.events_noted(
                    db, lot_id, "review-designation-none:%d:%d:"
                                % (lot_id, int(freeze.get("id") or 0))):
                continue
        except Exception:  # noqa: BLE001 - lecture illisible : le doute épargne
            pass
        try:
            authors = activation.frozen_authors(db, {"id": lot_id})
        except Exception:  # noqa: BLE001 - marque illisible : toutes éligibles
            authors = None
        forbidden_names = {str(lot.get("assignee") or "").strip()} - {""}
        forbidden_families = set()
        for author in authors or ():
            name = str((author or {}).get("name") or "").strip()
            if name:
                forbidden_names.add(name)
            forbidden_families.update(rd.proven_families(author))
        by_family: dict = {}  # famille -> noms éligibles sur CE gel
        for name, candidate in by_name.items():
            if name in forbidden_names or candidate.family in forbidden_families:
                continue
            by_family.setdefault(candidate.family, set()).add(name)
        for family, names in by_family.items():
            # Le nom le plus grand est le représentant stable de la famille
            # pour ce gel : les autres membres peuvent dormir.
            name = max(names)
            keep.setdefault(name, (family, lot_id))
    return pending, keep, None


def _journal_exemption(db, split_id, name: str, why: str) -> None:
    """Écrit une fois le motif d'exemption sur le lot de découpage (#4525).

    Une note par agent (ou par cause globale), pas une par passage : le motif
    est une trace, pas un journal de boucle. Une trace en échec ne défait pas
    le sommeil.
    """
    if split_id is None:
        return
    prefix = "%s %s:" % (SLEEP_EXEMPT_PREFIX, name)
    try:
        if work.events_noted(db, int(split_id), prefix):
            return
        work.note(db, int(split_id), "%s %s" % (prefix, why), actor=SLEEP_ACTOR)
    except Exception:  # noqa: BLE001 - une trace ne bloque jamais le geste
        pass


def _fresh_state(db, names) -> tuple:
    """#4610 : relit l'état des candidats au moment du geste.

    Le relevé de la passe précède de peu le sommeil : un lot attribué, une
    relecture due ou un courrier arrivé entre-temps ne doit pas être ignoré.
    Rend `({nom: ligne}, erreur)` ; une lecture en échec ne fait endormir
    personne (le doute épargne, comme #4525).
    """
    from . import db as db_mod
    wanted = {str(name) for name in names if name}
    if not wanted:
        return {}, None
    try:
        rows = storage.of(db).operations.listing()
    except db_mod.Unavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - le doute ne retire personne
        return {}, "%s: %s" % (type(exc).__name__, str(exc)[:160])
    return {str(row.get("name")): row for row in rows or ()
            if str(row.get("name") or "") in wanted}, None


def _senders(cfg, db, listing, projects, *, canons=None) -> tuple:
    """#4610 : les expéditeurs dont le courrier compte.

    Les orchestrateurs déclarés du registre (`sous_utilisation.alert_orchestrators`,
    rôle du canon ou `AMEESH_ALERT_ORCHESTRATORS`) et les pilotes des projets
    de l'agent (`alert_briefs.project_pilots`) : rien n'est propre à un projet
    ni à un nom en dur. Rend `(noms, erreur)` ; une erreur de lecture invite à
    ne pas endormir, jamais à deviner.
    """
    from . import alert_briefs
    try:
        names = set(su.alert_orchestrators(cfg, db, listing, canons=canons))
    except Exception as exc:  # noqa: BLE001 - rôle illisible : le doute épargne
        return set(), "%s: %s" % (type(exc).__name__, str(exc)[:160])
    for project in projects or ():
        try:
            names.update(alert_briefs.project_pilots(
                project, listing, cfg=cfg, db=db, canons=canons))
        except Exception:  # noqa: BLE001 - les orchestrateurs couvrent le rôle
            continue
    return names, None


def mail_lifters(cfg, db, listing, row, *, canons=None) -> tuple:
    """#4619 : les expéditeurs dont le courrier lève le sommeil de cet agent.

    Même règle que le sommeil et la levée (#4610) : orchestrateurs déclarés et
    pilotes des projets de l'agent. Rend `(noms, erreur)`."""
    from . import projects as projects_mod
    return _senders(cfg, db, listing, projects_mod.agent_projects(row),
                    canons=canons)


def _recent_mail(db, name: str, *, now: float, senders, window_s=None) -> tuple:
    """#4610 : les courriers NON LUS de `senders`, les plus récents d'abord.

    `window_s` borne l'âge (None : aucune borne). Deux lectures se complètent
    (#4619) : `mail.unread_active` montre les non-lus qui ouvrent un tour, du
    plus ancien au plus récent, qu'un flot de courrier lu ou passif plus récent
    ne masque pas ; `mail.history` montre les derniers courriers, qu'un agent en
    sommeil peut accumuler au-delà de `UNREAD_LIMIT` non-lus. L'union est
    dédoublonnée et triée du plus récent au plus ancien. Un message passif
    (copie, accusé) ne réveille pas. Rend `(messages, erreur)` ; une lecture en
    échec n'endort ni ne réveille.
    """
    from . import mail
    rows = []
    try:
        rows.extend(mail.history(db, name, limit=MAIL_HISTORY_LIMIT) or ())
        rows.extend(mail.unread_active(db, name, limit=MAIL_UNREAD_LIMIT) or ())
    except Exception as exc:  # noqa: BLE001 - une lecture en échec n'endort pas
        return [], "%s: %s" % (type(exc).__name__, str(exc)[:160])
    seen = set()
    out = []
    for row in rows:
        key = row.get("id")
        if key is not None:
            if key in seen:
                continue
            seen.add(key)
        if row.get("delivered_ts") is not None:
            continue
        if str(row.get("sender") or "").strip() not in senders:
            continue
        if mail.passive_reason(row):
            continue
        if window_s is not None:
            created = row.get("created_ts")
            if created is None or now - float(created) > float(window_s):
                continue
        out.append(row)
    out.sort(key=lambda row: (float(row.get("created_ts") or 0.0),
                              int(row.get("id") or 0)), reverse=True)
    return out, None


def _sleep(db, project: str, item: Measure, by_name: dict, now: float, *,
           split_id=None, lots=(), listing=(), cfg=None, canons=None,
           mail_window_s=None) -> list:
    """Met en sommeil les agents d'exécution au repos du projet (§2.3).

    #4525 : une relecture désignée ouverte n'est jamais endormie, et le dernier
    relecteur disponible d'une famille ne l'est non plus tant qu'un gel du
    projet attend un avis. Le motif est journalisé sur le lot de découpage.

    #4610 : un lot actionnable attribué, ou un courrier non lu récent du pilote
    ou d'un orchestrateur, retient aussi l'agent. L'état est relu au moment du
    geste : le relevé de la passe peut précéder l'attribution ou le courrier.
    """
    from . import agent_pause
    out = []
    reason = SLEEP_REASON % project
    pending, keep, unreadable = _reviewer_spares(
        db, project, lots, listing, cfg=cfg, canons=canons)
    if unreadable:
        why = "review exemptions unreadable: %s" % unreadable
        _journal_exemption(db, split_id, "*", why)
        for name in item.idle:
            out.append({"agent": name, "slept": False, "why": why})
        return out
    fresh, fresh_error = _fresh_state(db, item.idle)
    if fresh_error:
        why = "fresh state unreadable: %s" % fresh_error
        _journal_exemption(db, split_id, "*", why)
        for name in item.idle:
            out.append({"agent": name, "slept": False, "why": why})
        return out
    for name in item.idle:
        owner = _owner_of([by_name.get(name) or {}])
        if not owner:
            out.append({"agent": name, "slept": False, "why": "no human responsible"})
            continue
        pending_rows = pending.get(name.lower()) or []
        if pending_rows:
            why = ("designated review pending on #%s"
                   % (pending_rows[0].get("lot") or "?"))
            out.append({"agent": name, "slept": False, "why": why})
            _journal_exemption(db, split_id, name, why)
            continue
        kept = keep.get(name)
        if kept is not None:
            why = ("only eligible reviewer of family %s for pending freeze #%s"
                   % (kept[0], kept[1]))
            out.append({"agent": name, "slept": False, "why": why})
            _journal_exemption(db, split_id, name, why)
            continue
        row = fresh.get(name) or by_name.get(name) or {}
        if int(row.get("actionable_lots") or 0) > 0:
            # #4619 : `assigned_lot_id` est le lot assigné le plus récemment
            # touché, pas forcément actionnable (prévu plus tard, en attente) ;
            # le journal nomme le lot que l'agent doit réellement prendre.
            lot_id = row.get("actionable_lot_id")
            why = ("assigned actionable lot #%s" % lot_id if lot_id
                   else "actionable work due")
            out.append({"agent": name, "slept": False, "why": why})
            _journal_exemption(db, split_id, name, why)
            continue
        if int(row.get("unread") or 0):
            senders, mail_error = mail_lifters(cfg, db, listing, row,
                                               canons=canons)
            if mail_error:
                why = "mail exemptions unreadable: %s" % mail_error
                out.append({"agent": name, "slept": False, "why": why})
                _journal_exemption(db, split_id, name, why)
                continue
            recent, mail_error = _recent_mail(
                db, name, now=now, senders=senders, window_s=mail_window_s)
            if mail_error:
                why = "mail exemptions unreadable: %s" % mail_error
                out.append({"agent": name, "slept": False, "why": why})
                _journal_exemption(db, split_id, name, why)
                continue
            if recent:
                age = now - float(recent[0].get("created_ts") or now)
                why = "unread mail from %s (%s)" % (
                    recent[0].get("sender"), su._duration(age))
                out.append({"agent": name, "slept": False, "why": why})
                _journal_exemption(db, split_id, name, why)
                continue
        try:
            done = agent_pause.change(db, name, paused=True, reason=reason,
                                      actor=SLEEP_ACTOR, principal=owner, now=now)
        except Exception as exc:  # noqa: BLE001 - un agent refusé n'arrête pas les autres
            out.append({"agent": name, "slept": False,
                        "why": "%s: %s" % (type(exc).__name__, str(exc)[:120])})
            continue
        if done.get("changed"):
            out.append({"agent": name, "slept": True})
    return out


def _wake(db, project: str, listing: list, *, reason: str | None = None) -> list:
    """Le premier lot démarrable réveille les dormeurs du projet : seule la
    pause posée par le sommeil est levée (`expect_reason`).

    #4525 : le geste de levée est automatique comme la pose ; l'acteur est
    `ameesh:file-decoupage`, le principal reste l'humain qui portait la pause.
    #4623 : `reason` nomme la cause de la levée (une note de réouverture) ;
    c'est elle qui est journalisée dans l'événement de pause, la reconnaissance
    de la pause restant `SLEEP_REASON`.
    """
    from . import agent_pause
    from . import projects as projects_mod
    sleep_reason = SLEEP_REASON % project
    lift_reason = reason or sleep_reason
    out = []
    for row in listing:
        if not row.get("operator_paused") or row.get("operator_pause_reason") != sleep_reason:
            continue
        if project not in projects_mod.agent_projects(row):
            continue
        owner = (row.get("operator_pause_for") or row.get("operator_pause_by")
                 or _owner_of([row]))
        if not owner:
            continue
        try:
            done = agent_pause.change(db, row["name"], paused=False, reason=lift_reason,
                                      actor=SLEEP_ACTOR, principal=owner,
                                      expect_reason=sleep_reason)
        except Exception:  # noqa: BLE001 - la pause expire d'elle-même (#3961)
            continue
        if done.get("changed"):
            out.append(row["name"])
    return out


def _wake_work(db, listing: list, now: float, *, cfg=None, canons=None,
               mail_window_s=None) -> list:
    """#4610 : lève le sommeil dès qu'un lot actionnable est attribué à l'agent
    ou qu'un courrier non lu de son pilote ou d'un orchestrateur arrive.

    Le geste est journalisé dans `operator_pause_events` : acteur
    `ameesh:file-decoupage`, principal humain, raison « automatic sleep
    lifted: … » qui nomme la cause. Seule la pause du sommeil automatique est
    levée (`expect_reason`), jamais une pause d'opérateur. Une lecture en
    échec laisse la pause en place : le passage suivant réessaie.

    #4619 : la levée applique la même fenêtre de courrier que le sommeil
    (`mail_window_s`) — sinon un dormeur qui ne lit pas son courrier
    oscillerait entre sommeil et levée à chaque passe.
    """
    from . import agent_pause
    woken: dict = {}
    for row in listing:
        name = str(row.get("name") or "").strip()
        reason = str(row.get("operator_pause_reason") or "")
        if not name or not row.get("operator_paused") or not is_sleep_reason(reason):
            continue
        why = None
        if int(row.get("actionable_lots") or 0) > 0:
            # #4619 : le lot assigné le plus récemment touché n'est pas
            # forcément actionnable ; le journal nomme celui qui l'est.
            lot_id = row.get("actionable_lot_id")
            why = ("assigned actionable lot #%s" % lot_id if lot_id
                   else "actionable work due")
        elif int(row.get("unread") or 0):
            senders, mail_error = mail_lifters(cfg, db, listing, row,
                                               canons=canons)
            if mail_error:
                continue
            recent, mail_error = _recent_mail(db, name, now=now, senders=senders,
                                              window_s=mail_window_s)
            if mail_error or not recent:
                continue
            why = "unread mail from %s" % recent[0].get("sender")
        if why is None:
            continue
        owner = (row.get("operator_pause_for") or row.get("operator_pause_by")
                 or _owner_of([row]))
        if not owner:
            continue
        try:
            done = agent_pause.change(
                db, name, paused=False,
                reason="%s %s" % (SLEEP_LIFT_PREFIX, why),
                actor=SLEEP_ACTOR, principal=owner, expect_reason=reason)
        except Exception:  # noqa: BLE001 - la pause reste, le prochain passage réessaie
            continue
        if done.get("changed"):
            woken.setdefault(project_of_sleep_reason(reason), []).append(name)
    return [{"kind": "wake", "project": project, "agents": sorted(names)}
            for project, names in sorted(woken.items())]


def _journal_reopen(db, split_id, project: str, notes: list, woken: list) -> None:
    """Journalise une fois sur le lot de découpage la levée due à la dernière
    note de réouverture (#4623) : auteur et note d'origine, puis dormeurs levés.

    Une note par événement de réouverture (marque préfixée de son `id`), pas
    une par passe ; une trace en échec ne défait pas la levée."""
    note = None
    for row in _roadmap_order(notes):
        match = REOPENED_RE.match(str((row or {}).get("note") or ""))
        if match and match.group(1).strip(".,;:") == project:
            note = row
    if note is None:
        return
    note_id = int(note.get("id") or 0)
    prefix = "%s %s (note #%d)" % (REOPEN_MARK, project, note_id)
    try:
        if work.events_noted(db, int(split_id), prefix):
            return
        lifted = ", ".join(woken) if woken else "no sleeper"
        work.note(db, int(split_id),
                  "%s (actor %s, reason %r; %s)"
                  % (prefix, note.get("actor") or "?", note.get("note") or "", lifted),
                  actor=SLEEP_ACTOR)
    except Exception:  # noqa: BLE001 - une trace ne bloque jamais la levée
        pass


def run(cfg, db, *, now: float | None = None, state: dict | None = None,
        dry_run: bool = False, actor: str = "ameesh:file-decoupage",
        listing: list | None = None, canons=None, settings=None) -> list:
    """Une passe : déclencheur, repli, sommeil et réveil. Rend les gestes
    (`{"kind": ..., "project": ...}`) ; `state` (celui du notifieur) garde le
    début des conditions en cours sous `STATE_KEY`."""
    now = time.time() if now is None else float(now)
    state = {} if state is None else state
    settings = params(cfg) if settings is None else settings
    if settings is None:
        state.pop(STATE_KEY, None)
        return []
    if canons is None:
        from . import canon as canon_mod
        try:
            canons = canon_mod.load_configured(cfg) if cfg is not None else []
        except Exception:  # noqa: BLE001
            canons = []
    configured = bool(canons) or cfg is not None
    st = storage.of(db)
    listing = st.operations.listing() if listing is None else listing
    lots = st.operations.open_lots_activity(None)
    waits = planned_waits.read(db, lots, now=now) if lots else {}
    # #4717 : le déclencheur de découpage applique la même garde d'empreinte
    # que le débit (#4561) et `idle_capacity` (#4699) — une seule lecture
    # partagée, jamais une copie. Un lot recouvert par un lot en cours n'est
    # pas un lot démarrable offert aux agents au repos.
    footprint = (su.footprint_blockers(db, su.waiting_project_lots(lots), lots)
                 if lots else {})
    measures = measure(listing, lots, waits, now, canons, configured=configured,
                       footprint=footprint)
    due, since = held(measures, state.get(STATE_KEY) or {}, now,
                      settings.repos_part, settings.repos_s)
    state[STATE_KEY] = since
    by_name = {row["name"]: row for row in listing}
    out = []
    # #4610 : la levée sur lot attribué ou courrier ne dépend pas du lot de
    # découpage encore ouvert : elle se fait à chaque passe, pour tous les
    # dormeurs du sommeil automatique.
    if not dry_run:
        out.extend(_wake_work(db, listing, now, cfg=cfg, canons=canons,
                              mail_window_s=settings.mail_window()))
    # #4623 : un projet dont tous les exécutants dorment n'a plus de mesure
    # (`measure` écarte les pauses). Il doit rester dans la passe, sinon la
    # note de réouverture ne serait jamais lue et les dormeurs jamais levés.
    sleeping = sleeping_projects(listing)
    for project in sorted(set(measures) | sleeping):
        item = measures.get(project) or Measure(project)
        split = _open_split(db, project)
        verdict = None
        notes = []
        if split is not None:
            split = dict(split)
            if not split.get("created_ts"):
                match = next((lot for lot in lots
                              if int(lot["id"]) == int(split["id"])), None)
                split["created_ts"] = (match or {}).get("created_ts")
            notes = roadmap_notes(db, int(split["id"]))
            verdict = roadmap_state(notes, project)
        if item.startable or verdict == REOPENED_STATE:
            reason = REOPEN_REASON % project if verdict == REOPENED_STATE else None
            woken = [] if dry_run else _wake(db, project, listing, reason=reason)
            if woken:
                out.append({"kind": "wake", "project": project, "agents": woken})
            if verdict == REOPENED_STATE and not dry_run:
                _journal_reopen(db, int(split["id"]), project, notes, woken)
        if verdict == EXHAUSTED_STATE:
            if not item.startable and item.idle and not dry_run:
                slept = _sleep(db, project, item, by_name, now,
                               split_id=int(split["id"]), lots=lots, listing=listing,
                               cfg=cfg, canons=canons,
                               mail_window_s=settings.mail_window())
                if slept:
                    out.append({"kind": "sleep", "project": project,
                                "lot": int(split["id"]), "agents": slept})
            continue
        if split is None:
            if project in due:
                if dry_run:
                    out.append({"kind": "split", "project": project, "lot": None,
                                "measure": item.to_dict()})
                    continue
                made = _create(cfg, db, project, item, since[project], now,
                               listing=listing, canons=canons, actor=actor)
                out.append(dict(made, kind="split", project=project,
                                measure=item.to_dict()))
            continue
        if split.get("created_ts") is None or dry_run:
            continue
        done = _fallback(db, project, split,
                         _created_since(db, lots, project, split, waits), now, actor=actor)
        if done:
            out.append(dict(done, kind="fallback", project=project))
    return out


def exhausted_alerts(db, listing: list) -> list:
    """`roadmap_exhausted` : un lot de découpage ouvert dont la **dernière**
    note de feuille de route est `roadmap: épuisée <projet>` (#4623 : une
    réouverture postérieure retire l'alerte). Une alerte par lot ; le
    notifieur la pousse une fois à l'owner (dédoublonnage par clé) tant
    qu'elle dure."""
    from . import projects as projects_mod
    out = []
    projects = sorted({p for row in listing for p in projects_mod.agent_projects(row)})
    for project in projects:
        try:
            split = _open_split(db, project)
            if split is None:
                continue
            notes = roadmap_notes(db, int(split["id"]))
        except Exception:  # noqa: BLE001 - une lecture en échec ne lève rien
            continue
        if not exhausted(notes, project):
            continue
        rows = [row for row in listing if project in projects_mod.agent_projects(row)]
        out.append(su._alert(
            "roadmap_exhausted", None, None, 1, None,
            "roadmap exhausted for %s: the pilot noted it on split lot #%d; idle "
            "execution agents are asleep until a startable lot, an assigned actionable "
            "lot, or pilot/orchestrator mail arrives, or the pilot notes `%s %s`; "
            "burst hosts are released by the host drain (#3167 B). Action: add "
            "roadmap items or a decision (ameesh decide), or close #%d" % (
                project, int(split["id"]), REOPENED_PREFIX, project, int(split["id"])),
            lot=int(split["id"]), project=project,
            responsible=_owner_of(rows)))
    return out
