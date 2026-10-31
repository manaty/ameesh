# SPDX-License-Identifier: AGPL-3.0-only
"""L385: explicit prerequisites, service validation and work CLI."""
from __future__ import annotations

import datetime
import json
import os
import time

from . import storage, work, work_lifecycle as lifecycle


def _id(value):
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise work.WorkError("dependency lot identifiers must be positive integers")
    return value


def add(db, item_id, blocker_id, *, actor):
    _id(item_id)
    _id(blocker_id)
    if not isinstance(actor, str) or not actor.strip():
        raise work.WorkError("a dependency edit needs an identified actor")
    result = storage.of(db).work_dependencies.add(item_id, blocker_id, actor=actor)
    errors = {"self_dependency": "a lot cannot depend on itself",
              "unknown_lot": "dependency endpoints must be existing lots",
              "finished_lot": "new dependencies require two open lots",
              "dependency_cycle": "this dependency would create a cycle"}
    if result.get("error"):
        raise work.WorkError(errors[result["error"]])
    return result["added"]


def remove(db, item_id, blocker_id, *, actor):
    _id(item_id)
    _id(blocker_id)
    if not isinstance(actor, str) or not actor.strip():
        raise work.WorkError("a dependency edit needs an identified actor")
    if work.get(db, item_id) is None or work.get(db, blocker_id) is None:
        raise work.WorkError("dependency endpoints must be existing lots")
    return storage.of(db).work_dependencies.remove(item_id, blocker_id, actor=actor)


def grouped(db, item_ids):
    result = {}
    for edge in storage.of(db).work_dependencies.for_items(item_ids):
        result.setdefault(int(edge["work_item_id"]), []).append(edge)
    return result


# --------------------------------------------------------------------------
# #4167 (manaty/ameesh#3185, volet 1) : liens typés entre lots
# --------------------------------------------------------------------------

#: `duplicates`, `supersedes`, `part_of` sont orientés (le lot édité est le
#: doublon, le remplaçant, la part) ; `conflicts_with` est symétrique.
LINK_KINDS = ("duplicates", "supersedes", "part_of", "conflicts_with")

#: lecture inverse d'un lien orienté, vu depuis sa cible
_INVERSE = {"duplicates": "duplicated by", "supersedes": "superseded by",
            "part_of": "has part", "conflicts_with": "conflicts with"}

_LINK_ERRORS = {"self_link": "a lot cannot be linked to itself",
                "unknown_lot": "link endpoints must be existing lots",
                "finished_lot": "a conflict links two open lots",
                "link_cycle": "this link would create a cycle",
                "no_link": "no conflicts_with link between these lots"}


def _kind(kind):
    if kind not in LINK_KINDS:
        raise work.WorkError("link kind must be one of %s" % ", ".join(LINK_KINDS))
    return kind


def _actor(actor, what="a link edit"):
    if not isinstance(actor, str) or not actor.strip():
        raise work.WorkError("%s needs an identified actor" % what)


def add_link(db, item_id, other_id, kind, *, actor):
    """`item_id` <kind> `other_id` ; rend True si le lien est nouveau."""
    _id(item_id)
    _id(other_id)
    _kind(kind)
    _actor(actor)
    result = storage.of(db).work_dependencies.add_link(item_id, other_id, kind, actor=actor)
    if result.get("error"):
        raise work.WorkError(_LINK_ERRORS[result["error"]])
    return result["added"]


def remove_link(db, item_id, other_id, kind, *, actor):
    _id(item_id)
    _id(other_id)
    _kind(kind)
    _actor(actor)
    if work.get(db, item_id) is None or work.get(db, other_id) is None:
        raise work.WorkError(_LINK_ERRORS["unknown_lot"])
    return storage.of(db).work_dependencies.remove_link(item_id, other_id, kind, actor=actor)


def decide_link(db, item_id, other_id, decision, *, actor):
    """Consigne la décision prise sur un conflit : l'alerte se tait."""
    _id(item_id)
    _id(other_id)
    _actor(actor, "a conflict decision")
    decision = (decision or "").strip()
    if not decision:
        raise work.WorkError("a conflict decision needs its text (--decision)")
    result = storage.of(db).work_dependencies.decide_link(
        item_id, other_id, decision=decision, actor=actor)
    if result.get("error"):
        raise work.WorkError(_LINK_ERRORS[result["error"]])
    return result["decided"]


def links(db, item_ids):
    """`{lot: [lien vu depuis ce lot]}` : `kind` lu depuis le lot (`duplicated
    by` pour la cible d'un `duplicates`), `lot` l'autre extrémité, son état,
    `open`, la décision d'un conflit."""
    wanted = {int(i) for i in item_ids}
    out = {}
    for row in storage.of(db).work_dependencies.links_for(sorted(wanted)):
        a, b, kind = int(row["work_item_id"]), int(row["other_id"]), row["kind"]
        for lot, peer, label, state, forward in (
                (a, b, kind.replace("_", " "), row["other_state"], True),
                (b, a, _INVERSE[kind], row["item_state"], False)):
            if lot not in wanted:
                continue
            out.setdefault(lot, []).append({
                "kind": kind, "label": label, "forward": forward or kind == "conflicts_with",
                "lot": peer, "state": state, "open": bool(row["open"]),
                "created_by": row["created_by"], "created_ts": row["created_ts"],
                "decision": row.get("decision"), "decided_by": row.get("decided_by"),
                "decided_ts": row.get("decided_ts")})
    return out


def alerts(db):
    """#4167 : deux lots ouverts liés `conflicts_with` sans décision consignée.

    Une alerte par lien, adressée à l'assigné du premier lot (sinon du second) ;
    elle se tait quand la décision est consignée (`work links --decide`), quand
    le lien est retiré ou qu'un des deux lots se ferme."""
    from .exploitation import _alert
    out = []
    for row in storage.of(db).work_dependencies.undecided_conflicts():
        a, b = int(row["work_item_id"]), int(row["other_id"])
        agent = (row.get("item_assignee") or row.get("other_assignee") or "").strip() or None
        out.append(_alert(
            "lot_conflict_undecided", agent, row["created_ts"], 1, 1,
            "lots #%d and #%d conflict and no decision is recorded; decide, then "
            "ameesh work links %d --decide %d --decision \"…\" (or remove the link)"
            % (a, b, a, b),
            lot=a, other_lot=b, title=row.get("item_title"),
            other_title=row.get("other_title"), project=row.get("item_app"),
            lot_state=row.get("item_state"), other_state=row.get("other_state")))
    return out


def add_links_parser(sub, func):
    parser = sub.add_parser(
        "links", help="#4167: inspect or edit typed links between lots (duplicates, "
                      "supersedes, part_of, conflicts_with)")
    parser.add_argument("id", type=int, help="lot")
    edit = parser.add_mutually_exclusive_group()
    edit.add_argument("--add", type=int, metavar="LOT",
                      help="add the link: <id> <kind> LOT (with --kind)")
    edit.add_argument("--remove", type=int, metavar="LOT",
                      help="remove the link <id> <kind> LOT (with --kind)")
    edit.add_argument("--decide", type=int, metavar="LOT",
                      help="record the decision on the conflict with LOT (with --decision)")
    parser.add_argument("--kind", choices=LINK_KINDS)
    parser.add_argument("--decision", default="", help="the decision taken on the conflict")
    parser.add_argument("--actor", default="")
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(func=func)


def run_links(db, args):
    _id(args.id)
    if work.get(db, args.id) is None:
        raise work.WorkError("lot #%d not found" % args.id)
    changed = None
    if args.add is not None or args.remove is not None:
        if not args.kind:
            raise work.WorkError("--add and --remove need --kind (%s)" % ", ".join(LINK_KINDS))
        if args.add is not None:
            changed = add_link(db, args.id, args.add, args.kind, actor=args.actor)
        else:
            changed = remove_link(db, args.id, args.remove, args.kind, actor=args.actor)
    elif args.decide is not None:
        changed = decide_link(db, args.id, args.decide, args.decision, actor=args.actor)
    elif args.kind or args.decision:
        raise work.WorkError("--kind goes with --add/--remove, --decision with --decide")
    rows = links(db, [args.id]).get(args.id, [])
    if args.json:
        print(json.dumps({"id": args.id, "changed": changed, "links": rows},
                         ensure_ascii=False, indent=2))
        return 0
    if changed is not None:
        print("lot #%d: link %s" % (args.id, "updated" if changed else "unchanged"))
    for row in rows:
        line = "#%d %s #%d (%s)" % (args.id, row["label"], row["lot"], row["state"])
        if row["kind"] == "conflicts_with":
            line += (" — decided by %s: %s" % (row["decided_by"], row["decision"])
                     if row.get("decision") else
                     " — undecided" if row["open"] else "")
        print(line)
    if not rows:
        print("lot #%d: no typed links" % args.id)
    return 0


# --------------------------------------------------------------------------
# #1945 : atterrissage planifié sur un véhicule de paquet
# --------------------------------------------------------------------------

#: #2171 N4 : une déclaration reste une échéance courte et finie. Une borne
#: haute empêche un `--until 2099-12-31` de parquer un lot sans terme crédible.
#: C'est un terme d'EXPLOITATION, pas une règle de processus neuve : le défaut
#: est réglable par `AMEESH_LANDING_MAX_DAYS`, comme le seuil stale
#: (`AMEESH_STALE_AFTER`, `stagnation.py`). Le canon §7 refuse un paramètre
#: mécanique codé en dur ; le geste lit donc son terme dans l'environnement.
DEFAULT_MAX_DAYS = 90
LANDING_MAX_ENV = "AMEESH_LANDING_MAX_DAYS"


def max_days(env=None):
    """Le terme maximal d'une déclaration, en jours : env, sinon 90."""
    env = os.environ if env is None else env
    raw = (env.get(LANDING_MAX_ENV) or "").strip()
    if not raw:
        return DEFAULT_MAX_DAYS
    try:
        value = int(raw)
    except ValueError:
        raise work.WorkError("unreadable %s: %r (whole days ahead)"
                             % (LANDING_MAX_ENV, raw))
    if value <= 0:
        raise work.WorkError("%s must be positive" % LANDING_MAX_ENV)
    return value


def _require_frozen_with_verdict(db, item_id):
    """#2171 N3 : on ne parque qu'un lot gelé, au verdict ok ACQUIS.

    Même vérité que l'étiquette de `stagnation` (`progress._jalons_de_lot`) :
    le verdict retenu doit porter sur le dernier gel. Sans cette garde, le geste
    masquerait stale pour un lot encore en cours (build) ou en attente de
    relecture. L'absence de lot est laissée au stockage (`unknown_lot`)."""
    from . import progress
    item = work.get(db, item_id)
    if item is None:
        return
    if item.get("state") not in lifecycle.TO_VALIDATE:
        raise work.WorkError("the landing lot must be in review (qa) before it "
                             "can be parked")
    st = storage.of(db)
    jalons = progress._jalons_de_lot(
        item, st.progress.lot_transitions([item_id]),
        st.progress.lot_actions([item_id]),
        st.progress.lot_milestones([item_id]))
    if not jalons.get("last_frozen"):
        raise work.WorkError("the landing lot must be frozen (qa) before it can "
                             "be parked")
    if jalons.get("verdict_kind") != "ok" or not jalons.get("verdict_current"):
        raise work.WorkError("the landing lot needs an acquired ok verdict on its "
                             "last freeze")


def land(db, item_id, vehicle_id, *, until_on, actor):
    """Déclare (ou renouvelle) l'atterrissage planifié du lot sur un véhicule.

    Le lot et le véhicule doivent être ouverts et distincts ; l'échéance est
    une journée ISO, fin de journée incluse, bornée à [aujourd'hui, +90 j]
    (#2171 N4). Le lot parké doit être en revue (qa), gelé, avec un verdict ok
    acquis sur son dernier gel (#2171 N3). L'admission est ÉPINGLÉE au gel
    du véhicule : il doit porter une tête gelée et ne pas être `blocked`. Un
    simple geste, aucun texte libre ; l'étape et la tête épinglée sont relues
    à chaque passe, un nouveau gel exige un renouvellement explicite."""
    _id(item_id)
    _id(vehicle_id)
    if not isinstance(actor, str) or not actor.strip():
        raise work.WorkError("a landing declaration needs an identified actor")
    try:
        day = datetime.date.fromisoformat(str(until_on)[:10])
    except (TypeError, ValueError, OverflowError):
        raise work.WorkError("--until expects an ISO day (YYYY-MM-DD)")
    # #2171 N4 : bornes de la déclaration — ni un jour déjà passé, ni une
    # échéance invraisemblable (le résolveur ne lit que des journées finies).
    today = datetime.date.today()
    if day < today:
        raise work.WorkError("--until %s is already past: the deadline must be "
                             "today or later" % day.isoformat())
    limit = max_days()
    if (day - today).days > limit:
        raise work.WorkError("--until %s is too far: at most %d days ahead"
                             % (day.isoformat(), limit))
    with db.transaction() as tx:
        result = storage.of(tx).work_dependencies.set_landing(
            item_id, vehicle_id, until_on=day.isoformat(), actor=actor)
        # The storage call retains the graph and endpoint locks until this
        # outer transaction ends. Recheck under those locks; a refused verdict
        # rolls back both the declaration and its journal before any commit.
        if not result.get("error"):
            _require_frozen_with_verdict(tx, item_id)
    errors = {"self_landing": "a lot cannot land on itself",
              "unknown_lot": "landing endpoints must be existing lots",
              "not_in_review": "the landing lot must be in review (qa)",
              "finished_lot": "the landing lot must still be open",
              "finished_vehicle": "the vehicle must still be open",
              "no_freeze": "the vehicle has no frozen head: freeze it before "
                           "declaring the landing",
              "blocked_vehicle": "the vehicle is blocked: unblock it or wait for "
                                 "its next freeze"}
    if result.get("error"):
        raise work.WorkError(errors[result["error"]])
    return result["landed"]


def clear_landing(db, item_id, *, actor):
    """Retire la déclaration d'atterrissage (rend le signal stale)."""
    _id(item_id)
    if not isinstance(actor, str) or not actor.strip():
        raise work.WorkError("a landing declaration needs an identified actor")
    return storage.of(db).work_dependencies.clear_landing(item_id, actor=actor)


def _warn_landing_stale(db, row, frozen_sha, actor):
    """#3641 : prévient le déclarant qu'un regel ne reporte plus sa déclaration.

    Le journal et le courrier portent sur le VÉHICULE, jamais sur le lot
    déclaré : une note ou un courrier lié au lot déclaré compterait comme son
    activité récente et effacerait le signal stale que la déclaration devenue
    caduque doit justement rendre (#1945, revue B)."""
    from . import mail
    item_id = int(row["work_item_id"])
    vehicle_id = int(row["vehicle_id"])
    pinned = str(row.get("pinned_sha") or "").strip()
    recipient = str(row.get("created_by") or "").strip()
    text = ("Lot #%d: vehicle #%d was re-frozen on %s, while its planned landing is "
            "pinned to %s. The declaration is not carried automatically: renew it "
            "(`ameesh work landing %d --vehicle %d --until YYYY-MM-DD`) or clear it."
            % (item_id, vehicle_id, frozen_sha[:12], pinned[:12], item_id, vehicle_id))
    try:
        work.note(db, vehicle_id,
                  "Landing declaration of lot #%d needs renewal: vehicle re-frozen "
                  "on %s, declaration pinned to %s."
                  % (item_id, frozen_sha[:12], pinned[:12]),
                  actor=actor or work.SYSTEM_SENDER)
    except Exception:  # noqa: BLE001 - un journal en échec ne défait pas le gel
        pass
    if not recipient or recipient.startswith(work.HUMAN_PREFIX) \
            or recipient == work.SYSTEM_SENDER:
        return
    try:
        mail.send(db, work.SYSTEM_SENDER, recipient, text, kind="event",
                  work_item_id=str(vehicle_id))
    except Exception:  # noqa: BLE001 - destinataire inconnu ou courrier indisponible
        pass


def warn_landings_after_freeze(db, vehicle_id, frozen_sha, *, actor=""):
    """#3641 : un regel de véhicule ne reporte pas silencieusement une déclaration.

    Une déclaration est épinglée à un gel ; un nouveau gel la rend caduque.
    Reporter automatiquement sur une tête descendante a été essayé puis retiré
    (revue codex #142034) : l'ascendance et la détection de revert ne prouvent
    pas le contenu courant — un commit ordinaire peut retirer l'unité, et
    `plan_git` ne reconnaît que les `Revert` qui citent le commit. Le geste sûr
    est donc de **prévenir le déclarant sans aucun report** : la déclaration
    reste en place, le signal stale revient (#1945), et le pilote renouvelle
    explicitement sur la nouvelle tête.

    La prévention est revalidée sous le verrou des landings et le verrou de
    ligne du véhicule : `frozen_sha` doit encore être le gel COURANT et la ligne
    doit encore porter `pinned_sha`. Un gel retardé ne prévient donc pas une
    déclaration déjà renouvelée sur un gel plus récent, et une déclaration
    remplacée n'est ni touchée ni signalée.
    """
    st = storage.of(db)
    rows = st.work_dependencies.landings_on_vehicle(int(vehicle_id))
    result = {"notified": [], "skipped": []}
    for row in rows:
        item_id = int(row["work_item_id"])
        pinned = str(row.get("pinned_sha") or "").strip()
        if not pinned or pinned == frozen_sha:
            continue
        if not st.work_dependencies.landing_matches(
                item_id, int(vehicle_id), pinned, frozen_sha=frozen_sha):
            result["skipped"].append(item_id)
            continue
        result["notified"].append(item_id)
        _warn_landing_stale(db, row, frozen_sha, actor or "")
    return result


def landings(db, item_ids):
    """`{lot: ligne}` des atterrissages déclarés (lecture groupée)."""
    return {int(row["work_item_id"]): row
            for row in storage.of(db).work_dependencies.landings_for(item_ids)}


def landing_head(db, vehicle_id):
    """Dernier gel du véhicule (sha), ou None : la tête exacte, lue en base.

    #2171 N2 : même départage que `set_landing` et `_with_heads` — `(at, id)`
    le plus grand, jamais l'ordre de lecture."""
    from .planned_waits import latest_freeze
    row = latest_freeze(storage.of(db).progress.lot_milestones([int(vehicle_id)]))
    return row["sha"].strip() if row else None


def landing_label(landing, head=None):
    """Libellé d'une attente d'atterrissage : véhicule, étape, tête, échéance."""
    label = "landing on vehicle #%d" % int(landing["vehicle"])
    step = landing.get("step")
    if step or head:
        details = [d for d in (step, "head %s" % head[:12] if head else None) if d]
        label += " (%s)" % ", ".join(details)
    if landing.get("until"):
        label += " until %s" % landing["until"]
    return label


# --------------------------------------------------------------------------
# L385 suite : réveiller l'agent d'un dépendant quand sa dernière attente tombe
# --------------------------------------------------------------------------

def _pinned(row) -> bool:
    """Le lot est-il pinné sur une révision Process (colonne 0452) ?

    Lu dans la ligne d'activité (`open_lots_activity` porte `workflow_ref` par
    `to_jsonb`, '' sur une base d'avant la migration), jamais par une sonde de
    colonne ni un SELECT brut (#1922/B5)."""
    return bool(str(row.get("workflow_ref") or ""))


def released_by(db, blocker_id) -> list[dict]:
    """Lots ouverts dont la fermeture de `blocker_id` lève l'attente.

    Ne sont candidats que les lots qui déclarent `blocker_id` en prérequis
    EXPLICITE (une arête `work_item_dependencies`) : un engagement `depends_on`
    (lot ou package) n'est pas une arête et sa cible fermée ne réveille
    personne par elle-même ; il ne fait que retenir un candidat tant qu'il vise
    un lot ouvert.

    Lecture faite DANS la transaction de fermeture, après l'écriture de la
    transition (et sous le verrou de graphe) : un dépendant n'est rendu que
    s'il ne lui reste AUCUNE attente une fois le blocage fermé — aucune arête
    bloquante, aucun engagement ouvert, aucun début planifié futur, la même
    vérité que `planned_wait`. Les lots fermés, fusionnés ou sans arête vers
    `blocker_id` n'en font jamais partie.

    #1922/N13 : un lot pinné sur une révision Process n'est pas libéré par la
    fermeture — seul un `release` du registre, vérifié par C, lève sa retenue.
    Il est donc écarté ici, à la source : la réouverture `intake` des
    dépendants non assignés (B4) comme le courrier de réveil à leur agent
    promettraient une reprise que le registre seul peut autoriser.

    C'est le caractère « dernière attente » qui rend le réveil idempotent :
    tant qu'un autre bloqueur tient, la fermeture ne rend personne ; la
    fermeture qui suit, sérialisée par le verrou de graphe, est la seule à
    voir l'attente tomber. Aucun marqueur à poser, aucun doublon à déduire.
    """
    st = storage.of(db)
    blocker_id = int(blocker_id)
    open_items = st.operations.open_lots_activity(None)
    rows = [r for r in open_items
            if int(r["id"]) != blocker_id and not _pinned(r)]
    ids = [int(r["id"]) for r in rows]
    if not ids:
        return []
    edges = st.work_dependencies.for_items(ids)
    linked = {int(e["work_item_id"]) for e in edges
              if int(e["blocker_id"]) == blocker_id and not e["blocking"]}
    if not linked:
        return []
    from . import planned_waits  # import tardif : plan importe les attentes
    candidates = [r for r in rows if int(r["id"]) in linked]
    # #2378 : une demande de décision ne retient pas le réveil d'un lot libéré
    waits = planned_waits.read(db, candidates, open_items=open_items, dependencies=edges,
                               decisions=())
    return [r for r in candidates if int(r["id"]) not in waits]


def lock(db) -> None:
    """Prend le verrou de graphe des dépendances dans la transaction courante.

    À appeler par toute fermeture de lot, avant de lire `released_by` : deux
    fermetures concurrentes (B et C, tous deux attendus par D) décident alors
    l'une APRÈS l'autre. Sans lui, chacune lit l'autre bloqueur encore ouvert
    et aucune ne réveille D (réveil perdu) ; ou, en lisant après coup, les
    deux voient la libération et D reçoit deux courriers (doublon). Le verrou
    est celui des éditions de graphe (`add`/`remove`), tenu jusqu'au commit :
    la décision et le courrier qu'elle produit ne se séparent pas.
    """
    storage.of(db).work_dependencies.lock()


def _released_text(row, blocker_id, blocker_state, blocker_reason=""):
    """Le corps court et factuel d'un réveil de dépendance."""
    lot = int(row["id"])
    title = (row.get("title") or "").strip()[:60]
    title = " \"%s\"" % title if title else ""
    if blocker_state in lifecycle.DELIVERED:
        return ("Dependency released: lot #%d is %s; lot #%d%s no longer waits on "
                "any lot. Resume it when you are ready."
                % (int(blocker_id), blocker_state, lot, title))
    why = " (%s)" % blocker_reason if blocker_reason else ""
    return ("Dependency released: lot #%d was closed%s, not merged; lot #%d%s no longer "
            "waits on any lot. Reassess the prerequisite before resuming."
            % (int(blocker_id), why, lot, title))


def notify_released(db, blocker_id, *, blocker_state, blocker_reason="",
                    after_commit=None) -> list[dict]:
    """Dépose un courrier durable à l'agent de chaque dépendant dont
    `blocker_id` était le dernier prérequis explicite.

    Appelé DANS la transaction qui ferme le bloqueur, après l'écriture de la
    transition : le courrier est committé avec elle. Il n'existe donc aucune
    fenêtre « fermeture commise, réveil non écrit » — un échec d'écriture
    annule la fermeture entière, et la reprise (relevé suivant, seconde
    commande) refait les deux. Le message est un `event` ordinaire porté par
    le lot dépendant : le runner existant réveille l'agent par LISTEN/NOTIFY,
    sans sondage ni session neuve ; un agent arrêté à la main reçoit le
    courrier à sa reprise, rien ne le redémarre.

    Ne rattrape AUCUNE erreur : avaler l'échec ici committerait la fermeture
    sans son réveil, précisément le défaut que ce chemin doit exclure. Le fil
    lisible est écrit après le commit (`after_commit`, appelé par l'appelant).

    #1668 : un dépendant NON ASSIGNÉ n'a pas de destinataire ; s'il est encore
    `blocked` alors que plus rien ne le retient, il repasse `intake` dans la
    même transaction — il redevient démarrable au self-service sans geste
    d'agent, au lieu d'attendre une alerte stale (jusqu'à 14 h).

    Rend une ligne par courrier déposé (`lot`, `to`, `message_id`)."""
    from . import mail
    out: list[dict] = []
    rows = released_by(db, blocker_id)
    unassigned = [int(r["id"]) for r in rows if not (r.get("assignee") or "").strip()]
    if unassigned:
        storage.of(db).work_dependencies.release_unassigned(
            unassigned, actor=work.SYSTEM_SENDER)
    for row in rows:
        to = (row.get("assignee") or "").strip()
        if not to or to.startswith(work.HUMAN_PREFIX):
            continue
        lot = int(row["id"])
        body = _released_text(row, blocker_id, blocker_state, blocker_reason)
        charge = {"dependent": lot, "blocker": int(blocker_id), "blocker_state": blocker_state}
        if blocker_reason:
            charge["blocker_reason"] = blocker_reason
        message_id = mail.send(
            db, work.SYSTEM_SENDER, to, body, kind="event", work_item_id=str(lot),
            payload={"dependency": charge}, after_commit=after_commit)
        out.append({"lot": lot, "to": to, "message_id": message_id})
    return out


def wait_label(wait, plural="lots"):
    """Le libellé d'une attente planifiée.

    `plural` est le nom d'une tâche dans le vocabulaire actif : `progress`
    passe celui de l'instantané (CORE « tâches »), les lecteurs v1 gardent
    « lots » (#4729)."""
    if not wait:
        return None
    if wait["what"] == "scheduled":
        return ("scheduled wake %s" if wait.get("wake_ts") is not None
                else "planned start %s") % wait["start"]
    if wait["what"] == "hold":
        if wait.get("gate"):
            return "external hold until %s (%s)" % (wait.get("until"), wait.get("gate"))
        # #1664 : le hold déclaré, avec son auteur et sa date (le pilote juge
        # sur pièce s'il tient toujours).
        label = "hold"
        if wait.get("by"):
            label += " by %s" % wait["by"]
        if wait.get("since"):
            label += " since %s" % time.strftime("%Y-%m-%d %H:%M",
                                                 time.localtime(float(wait["since"])))
        return label
    if wait["what"] == "packet":
        # #2335 : le lot n'attend pas un exécuteur de lot mais la fusion du
        # porteur `integration/**` (le composeur/pilote la porte).
        label = "waiting for packet merge #%s" % wait.get("vehicle")
        if wait.get("branch"):
            label += " (%s)" % wait["branch"]
        return label
    if wait["what"] == "landing":
        return landing_label(wait, wait.get("head"))
    if wait["what"] == "decision":
        # #2378 : la demande de décision ouverte, son décideur et son échéance
        return "decision #%d for %s until %s" % (
            wait["decision"], wait.get("owner") or "any human", wait.get("until"))
    return "waiting on %s %s" % (plural, ", ".join("#%d" % i for i in wait["lots"]))


def add_parser(sub, func):
    parser = sub.add_parser("dependencies", help="inspect or edit explicit blocking prerequisites")
    parser.add_argument("id", type=int, help="waiting lot")
    edit = parser.add_mutually_exclusive_group()
    edit.add_argument("--add", type=int, metavar="LOT", help="add a prerequisite lot")
    edit.add_argument("--remove", type=int, metavar="LOT", help="remove a prerequisite lot")
    parser.add_argument("--actor", default="")
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(func=func)


def run(db, args):
    _id(args.id)
    if work.get(db, args.id) is None:
        raise work.WorkError("lot #%d not found" % args.id)
    changed = None
    if args.add is not None:
        changed = add(db, args.id, args.add, actor=args.actor)
    elif args.remove is not None:
        changed = remove(db, args.id, args.remove, actor=args.actor)
    edges = grouped(db, [args.id]).get(args.id, [])
    if args.json:
        print(json.dumps({"id": args.id, "changed": changed, "dependencies": edges},
                         ensure_ascii=False, indent=2))
    else:
        if changed is not None:
            print("lot #%d: dependency %s" % (args.id, "updated" if changed else "unchanged"))
        for edge in edges:
            print("#%d depends on #%d (%s, %s)" % (
                args.id, edge["blocker_id"], edge["blocker_state"],
                "blocking" if edge["blocking"] else "satisfied"))
        if not edges:
            print("lot #%d: no explicit prerequisites" % args.id)
    return 0


def add_landing_parser(sub, func):
    parser = sub.add_parser(
        "landing", help="#1945: declare (or clear) the planned landing on a package vehicle")
    parser.add_argument("id", type=int, help="landing lot (done, waiting for the vehicle)")
    parser.add_argument("--vehicle", type=int, metavar="LOT",
                        help="open lot that carries the composition (the vehicle)")
    parser.add_argument("--until", metavar="YYYY-MM-DD",
                        help="finite deadline: the alert returns after that day")
    parser.add_argument("--clear", action="store_true", help="withdraw the declaration")
    parser.add_argument("--actor", default="")
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(func=func)


def run_landing(db, args):
    _id(args.id)
    if work.get(db, args.id) is None:
        raise work.WorkError("lot #%d not found" % args.id)
    changed = None
    if args.clear:
        changed = clear_landing(db, args.id, actor=args.actor)
    else:
        if args.vehicle is None or not args.until:
            raise work.WorkError("a landing declaration needs --vehicle and --until "
                                 "(or --clear)")
        changed = land(db, args.id, args.vehicle, until_on=args.until, actor=args.actor)
    row = landings(db, [args.id]).get(args.id)
    if args.json:
        print(json.dumps({"id": args.id, "changed": changed, "landing": row},
                         ensure_ascii=False, indent=2))
        return 0
    if changed is not None:
        print("lot #%d: landing %s" % (args.id, "updated" if changed else "unchanged"))
    if row:
        wait = {"what": "landing", "vehicle": int(row["vehicle_id"]),
                "step": row.get("vehicle_state"), "until": row.get("until_on")}
        # la tête affichée est celle de l'admission (épinglée), pas la courante
        print("#%d: %s" % (args.id,
                           landing_label(wait, row.get("pinned_sha")
                                         or landing_head(db, int(row["vehicle_id"])))))
    else:
        print("lot #%d: no planned landing" % args.id)
    return 0
