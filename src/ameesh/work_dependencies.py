# SPDX-License-Identifier: AGPL-3.0-only
"""L385: explicit prerequisites, service validation and work CLI."""
from __future__ import annotations

import json
import time

from . import storage, work


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
# L385 suite : réveiller l'agent d'un dépendant quand sa dernière attente tombe
# --------------------------------------------------------------------------

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

    C'est le caractère « dernière attente » qui rend le réveil idempotent :
    tant qu'un autre bloqueur tient, la fermeture ne rend personne ; la
    fermeture qui suit, sérialisée par le verrou de graphe, est la seule à
    voir l'attente tomber. Aucun marqueur à poser, aucun doublon à déduire.
    """
    st = storage.of(db)
    blocker_id = int(blocker_id)
    open_items = st.operations.open_lots_activity(None)
    rows = [r for r in open_items if int(r["id"]) != blocker_id]
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
    waits = planned_waits.read(db, candidates, open_items=open_items, dependencies=edges)
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
    if blocker_state == "merged":
        return ("Dependency released: lot #%d is merged; lot #%d%s no longer waits on "
                "any lot. Resume it when you are ready." % (int(blocker_id), lot, title))
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
    Rend une ligne par courrier déposé (`lot`, `to`, `message_id`)."""
    from . import mail
    out: list[dict] = []
    rows = released_by(db, blocker_id)
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


def wait_label(wait):
    if not wait:
        return None
    if wait["what"] == "scheduled":
        return "planned start %s" % wait["start"]
    if wait["what"] == "hold":
        if wait.get("gate") or wait.get("until"):
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
    return "waiting on lots %s" % ", ".join("#%d" % i for i in wait["lots"])


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
