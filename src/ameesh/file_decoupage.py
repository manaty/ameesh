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
#: raison de la pause opérateur posée par le sommeil, levée par elle seule
SLEEP_REASON = "roadmap: épuisée %s (#3256 §2.3)"
#: #4525 : reconnaissance de CETTE pause, pour l'alerte (une raison humaine qui
#: ressemblerait au texte ne doit pas être prise pour le sommeil automatique)
SLEEP_REASON_RE = re.compile(r"^roadmap\s*:\s*épuisée\s+\S+\s+\(#3256\s+§2\.3\)$")
#: préfixe de la note d'exemption écrite sur le lot de découpage
SLEEP_EXEMPT_PREFIX = "sleep exempted:"
#: état du notifieur : `{projet: début de la condition}`
STATE_KEY = "queue_fill"


def is_sleep_reason(reason) -> bool:
    """#4525 : la raison est-elle exactement celle du sommeil automatique ?

    L'alerte de pause opérateur s'en sert pour ne pas demander à l'owner de
    mettre fin à une maintenance qui n'a jamais eu lieu, et pour dire que le
    premier lot démarrable lève le sommeil.
    """
    return bool(SLEEP_REASON_RE.match(str(reason or "").strip()))


def source_of(project: str) -> str:
    return SOURCE_PREFIX + project


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
            configured: bool) -> dict:
    """`{projet: Measure}` pour chaque projet d'au moins un agent réveillable.

    Au repos : `idle_agents` (sans lot actionnable, #1677). Démarrable : un
    lot ouvert sans assigné ni exécutant externe, hors file d'amélioration,
    qu'aucune attente planifiée ne retient (`split_startable`)."""
    from . import projects as projects_mod
    pool = wakeable(listing, canons, configured=configured)
    names = {row["name"] for row in pool}
    idle = {row["name"] for row in su.idle_agents(listing, now, 0.0, waiting_free=True,
                                                  actionable_idle=True)
            if row["name"] in names}
    startable, _blocked = su.split_startable(su.waiting_project_lots(lots), waits)
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


def exhausted(notes: list, project: str) -> bool:
    """La note du pilote `roadmap: épuisée <projet>` figure-t-elle ?"""
    for row in notes:
        match = EXHAUSTED_RE.match(row.get("note") or "")
        if match and match.group(1).strip(".,;:") == project:
            return True
    return False


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
        "prochain lot démarrable, l'owner reçoit une alerte)." % (
            len(item.idle), EXHAUSTED_PREFIX, project),
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
    return out


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
      disponible** d'une famille qui peut encore relire un gel en attente.
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
    eligible: dict = {}  # famille -> {nom: lot}
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
        for name, candidate in by_name.items():
            if name in forbidden_names or candidate.family in forbidden_families:
                continue
            eligible.setdefault(candidate.family, {}).setdefault(name, lot_id)

    keep: dict = {}
    for family, names in eligible.items():
        # Le nom le plus grand est le représentant stable de la famille : les
        # autres membres peuvent dormir, le dernier reste disponible.
        name = max(names)
        keep[name] = (family, names[name])
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


def _sleep(db, project: str, item: Measure, by_name: dict, now: float, *,
           split_id=None, lots=(), listing=(), cfg=None, canons=None) -> list:
    """Met en sommeil les agents d'exécution au repos du projet (§2.3).

    #4525 : une relecture désignée ouverte n'est jamais endormie, et le dernier
    relecteur disponible d'une famille ne l'est non plus tant qu'un gel du
    projet attend un avis. Le motif est journalisé sur le lot de découpage.
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


def _wake(db, project: str, listing: list) -> list:
    """Le premier lot démarrable réveille les dormeurs du projet : seule la
    pause posée par le sommeil est levée (`expect_reason`).

    #4525 : le geste de levée est automatique comme la pose ; l'acteur est
    `ameesh:file-decoupage`, le principal reste l'humain qui portait la pause.
    """
    from . import agent_pause
    from . import projects as projects_mod
    reason = SLEEP_REASON % project
    out = []
    for row in listing:
        if not row.get("operator_paused") or row.get("operator_pause_reason") != reason:
            continue
        if project not in projects_mod.agent_projects(row):
            continue
        owner = (row.get("operator_pause_for") or row.get("operator_pause_by")
                 or _owner_of([row]))
        if not owner:
            continue
        try:
            done = agent_pause.change(db, row["name"], paused=False, reason=reason,
                                      actor=SLEEP_ACTOR, principal=owner,
                                      expect_reason=reason)
        except Exception:  # noqa: BLE001 - la pause expire d'elle-même (#3961)
            continue
        if done.get("changed"):
            out.append(row["name"])
    return out


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
    measures = measure(listing, lots, waits, now, canons, configured=configured)
    due, since = held(measures, state.get(STATE_KEY) or {}, now,
                      settings.repos_part, settings.repos_s)
    state[STATE_KEY] = since
    by_name = {row["name"]: row for row in listing}
    out = []
    for project, item in sorted(measures.items()):
        split = _open_split(db, project)
        if item.startable:
            woken = [] if dry_run else _wake(db, project, listing)
            if woken:
                out.append({"kind": "wake", "project": project, "agents": woken})
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
        split = dict(split)
        if not split.get("created_ts"):
            match = next((lot for lot in lots if int(lot["id"]) == int(split["id"])), None)
            split["created_ts"] = (match or {}).get("created_ts")
        notes = work.events_noted(db, int(split["id"]), EXHAUSTED_PREFIX)
        if exhausted(notes, project):
            if not item.startable and item.idle and not dry_run:
                slept = _sleep(db, project, item, by_name, now,
                               split_id=int(split["id"]), lots=lots, listing=listing,
                               cfg=cfg, canons=canons)
                if slept:
                    out.append({"kind": "sleep", "project": project,
                                "lot": int(split["id"]), "agents": slept})
            continue
        if split.get("created_ts") is None or dry_run:
            continue
        done = _fallback(db, project, split,
                         _created_since(db, lots, project, split, waits), now, actor=actor)
        if done:
            out.append(dict(done, kind="fallback", project=project))
    return out


def exhausted_alerts(db, listing: list) -> list:
    """`roadmap_exhausted` : un lot de découpage ouvert porte la note du
    pilote. Une alerte par lot ; le notifieur la pousse une fois à l'owner
    (dédoublonnage par clé) tant qu'elle dure."""
    from . import projects as projects_mod
    out = []
    projects = sorted({p for row in listing for p in projects_mod.agent_projects(row)})
    for project in projects:
        try:
            split = _open_split(db, project)
            if split is None:
                continue
            notes = work.events_noted(db, int(split["id"]), EXHAUSTED_PREFIX)
        except Exception:  # noqa: BLE001 - une lecture en échec ne lève rien
            continue
        if not exhausted(notes, project):
            continue
        rows = [row for row in listing if project in projects_mod.agent_projects(row)]
        out.append(su._alert(
            "roadmap_exhausted", None, None, 1, None,
            "roadmap exhausted for %s: the pilot noted it on split lot #%d; idle "
            "execution agents are asleep until a startable lot arrives, burst hosts are "
            "released by the host drain (#3167 B). Action: add roadmap items or a "
            "decision (ameesh decide), or close #%d" % (
                project, int(split["id"]), int(split["id"])),
            lot=int(split["id"]), project=project,
            responsible=_owner_of(rows)))
    return out
