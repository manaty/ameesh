# SPDX-License-Identifier: AGPL-3.0-only
"""#2766 — les avis d'un véhicule de paquet se lisent par lot membre.

Le canon §12 compose un paquet sur une branche `integration/**` : chaque lot
membre entre par son commit de composition, après ses propres avis sur sa tête
gelée. Le paquet lui-même ne porte que la composition (manifeste, résolutions
de conflit) et le §11 bis dit « verdicts acquis : ni gel répété, ni second avis
sur delta ».

La porte de fusion (`merge_queue.review_gate`) classait pourtant le diff gelé
du **paquet entier** : une migration portée par un membre rendait le véhicule
« sensible » à deux avis, alors que le membre les avait déjà. Ce module rend la
lecture propre au véhicule :

* les **membres** sont les lots qui ont déclaré un atterrissage sur le
  véhicule (`work_landings`, #1945) et ceux que nomment les commits de
  composition (`ameesh-work: <lot>`, #2157) ; un lot sans avis sur sa tête
  gelée refuse le véhicule ;
* le **delta propre** du véhicule est l'écart de ses commits à la fusion
  automatique de leurs parents : une résolution de conflit (même recopiant un
  parent), un chemin ramené à sa version de base, un manifeste, une
  adaptation. Un commit de composition qui ne fait qu'apporter le contenu d'un
  membre n'y figure pas ; une adaptation de test ou un manifeste n'exige aucun
  avis (§11 bis) ;
* la classe de ce delta, et elle seule, fixe le nombre d'avis dus sur la tête
  du paquet. Sans delta propre, aucun avis de véhicule n'est dû.

Tout doute (dépôt illisible, membre non gelé, source de composition qui ne
contient pas sa tête gelée) refuse : la lecture ne suppose jamais qu'un avis
acquis couvre un contenu qu'elle ne sait pas nommer.
"""
from __future__ import annotations

import re
from typing import Optional

from . import packet_close, plan_git, work

#: un chemin de test : son adaptation de composition n'exige aucun avis (§11 bis)
_TEST = re.compile(
    r"(^|/)(tests?|__tests__|spec)/|"
    r"(^|/)test_[^/]*\.py$|(^|/)[^/]*_test\.py$|"
    r"\.(?:test|spec)\.(?:[cm]?[jt]sx?)$")
#: les entrées du plan de CI (`tests/parts/`) ne sont pas des tests qu'on
#: adapte : elles décident quels modules tournent, donc elles se relisent
#: (§8 ameesh : scripts/test*, ci* sensibles ; #2792).
_CI_PLAN = re.compile(r"(^|/)tests/parts(/|$)")


class VehicleError(RuntimeError):
    """La lecture du véhicule est impossible : la porte refuse, elle ne devine pas."""


def is_vehicle(row) -> bool:
    """La ligne est-elle un véhicule de paquet (branche `integration/**`) ?"""
    from . import packet_guard

    try:
        branch = row.get("branch") if hasattr(row, "get") else row["branch"]
    except (KeyError, TypeError, AttributeError):
        return False
    return packet_guard.is_packet_branch(branch)


# --------------------------------------------------------------------------
# les membres
# --------------------------------------------------------------------------

def _landing_members(db, vehicle: int) -> list[dict]:
    """Les lots qui ont déclaré un atterrissage sur ce véhicule (#1945).

    Lecture directe de `work_landings` : la déclaration est un fait de base,
    pas un texte libre. Une base illisible ne prouve aucun membre — l'appelant
    gardera alors la lecture historique, jamais un véhicule vide rassurant.
    """
    if db is None:
        return []
    try:
        rows = db.query(
            "SELECT work_item_id, pinned_sha FROM work_landings"
            " WHERE vehicle_id = %s ORDER BY work_item_id", (int(vehicle),))
    except Exception:  # noqa: BLE001 - base illisible : aucun membre lu
        return []
    found = []
    for row in rows or ():
        try:
            lot = int(row["work_item_id"])
        except (KeyError, TypeError, ValueError):
            continue
        # `pinned_sha` est le gel DU VÉHICULE au moment de la déclaration, pas
        # la source composée du membre : la tête du membre se lit en base.
        if lot and lot != int(vehicle):
            found.append({"lot": lot, "source": ""})
    return found


def _trailer_members(repo: str, base: str, head: str, vehicle: int) -> list[dict]:
    """Les lots nommés par les commits de composition (`ameesh-work: <lot>`).

    Chaque fusion de composition a le membre en second parent (#2157) : c'est
    la source composée, jamais une ref mobile. Un message qui nomme plusieurs
    lots (paquet du train #1467) ne dit pas quel fichier va à quel lot : sa
    source est le commit lui-même, et `own_changes` compare ses chemins à tous
    ses parents — le contenu d'un membre y est donc reconnu, seule une
    résolution qui diffère de tous les parents reste propre.
    """
    log = plan_git._out(repo, "log", "--first-parent", "--format=%H",
                        "%s..%s" % (base, head))
    if log is None:
        raise VehicleError("cannot read the composition history %s..%s"
                           % (base[:12], head[:12]))
    found = []
    for sha in log.split():
        parents_text = plan_git._out(repo, "rev-list", "--parents", "-n", "1", sha)
        if parents_text is None:
            raise VehicleError("cannot read the parents of composition commit %s"
                               % sha[:12])
        parents = parents_text.split()[1:]
        message = plan_git._out(repo, "log", "-1", "--format=%B", sha)
        if message is None:
            raise VehicleError("cannot read the message of composition commit %s"
                               % sha[:12])
        lots = []
        for match in plan_git.WORK_TRAILER_RE.finditer(message):
            try:
                lot = int(match.group(1))
            except (TypeError, ValueError):
                continue
            if lot and lot != int(vehicle) and lot not in lots:
                lots.append(lot)
        for lot in lots:
            source = parents[1] if len(lots) == 1 and len(parents) >= 2 else sha
            found.append({"lot": lot, "source": source})
    return found


def members(db, repo: str, base: str, head: str, vehicle: int) -> list[dict]:
    """Les membres du véhicule, dédoublonnés : `[{lot, source}]`.

    Les déclarations d'atterrissage et les trailers de composition se
    complètent : le premier vient de la base, les seconds du contenu composé.
    Un lot qui n'a qu'une déclaration garde une source vide ; `need` lira sa
    tête gelée. Le véhicule ne se cite jamais lui-même.
    """
    vehicle = int(vehicle)
    found: dict[int, dict] = {}
    for entry in _landing_members(db, vehicle):
        found.setdefault(entry["lot"], {"lot": entry["lot"], "source": ""})
    try:
        trailers = _trailer_members(repo, base, head, vehicle)
    except VehicleError:
        raise
    except Exception as exc:  # noqa: BLE001 - lecture git imprévue : doute
        raise VehicleError("cannot read the composition members: %s" % exc) from exc
    for entry in trailers:
        current = found.setdefault(entry["lot"], {"lot": entry["lot"], "source": ""})
        if not current["source"] and entry["source"]:
            current["source"] = entry["source"]
    return sorted(found.values(), key=lambda entry: entry["lot"])


# --------------------------------------------------------------------------
# le delta propre
# --------------------------------------------------------------------------

def _changed(repo: str, base: str, tip: str) -> list[str]:
    text = plan_git._out(repo, "diff", "--name-only", base, tip)
    if text is None:
        raise VehicleError("cannot read the changed paths %s..%s"
                           % (base[:12], tip[:12]))
    return [path for path in text.splitlines() if path]


def _is_review_free(path: str) -> bool:
    """Un chemin que la composition ne soumet à aucun avis (§11 bis).

    Une adaptation de test et le manifeste du paquet disent l'état de la
    composition, pas un contenu relu : ils ne rouvrent pas la relecture. Une
    entrée de `tests/parts/` n'est pas une adaptation de test : elle pilote la
    CI (§8), elle reste un delta à relire (#2792).
    """
    if _CI_PLAN.search(path):
        return False
    return bool(_TEST.search(path)) or bool(packet_close.MANIFEST.match(path))


def _merge_own_paths(repo: str, parents: list, sha: str) -> list[str]:
    """Le delta propre d'un commit de fusion, vide s'il est mécanique.

    Une fusion **propre** n'ajoute que ce que la fusion automatique ne donne
    pas : les chemins dont l'entrée diffère de son arbre attendu. Une
    fusion **en conflit** ajoute les chemins en conflit même quand la
    résolution recopie un parent à l'identique : choisir un camp est un
    jugement, la ligne choisie n'a pas été relue. Une union pure des deux camps
    reste mécanique (#2496) et n'exige aucun avis. Au-delà de deux parents,
    faute d'arbre automatique attendu, le véhicule est refusé : un delta vide
    ne doit jamais couvrir une fusion qu'on ne sait pas rejouer.
    """
    from . import packet_candidates

    if len(parents) == 2:
        merged = packet_candidates.merge_tree(repo, parents[0], parents[1])
        if merged["state"] == "error":
            raise VehicleError("cannot read the automatic merge of %s: %s"
                               % (sha[:12], merged.get("detail") or "no tree"))
        conflicts = set(merged.get("files") or ())
        if conflicts:
            try:
                mechanical = packet_candidates.mechanical_union(
                    repo, parents[0], parents[1], sha) == ""
            except Exception:  # noqa: BLE001 - preuve indisponible : jugement
                mechanical = False
            if mechanical:
                return []
        # L'arbre attendu détecte aussi le contenu entrant supprimé et les
        # changements de mode ; comparer seulement aux parents les masque.
        # Les chemins conflictuels restent propres quel que soit le choix.
        expected = str(merged.get("tree") or "")
        actual = plan_git._out(repo, "rev-parse", "--verify", "-q",
                               sha + "^{tree}") or ""
        if not expected or not actual or not plan_git._out(
                repo, "rev-parse", "--verify", "-q", expected + "^{tree}"):
            raise VehicleError("cannot read the merge trees of %s" % sha[:12])
        return sorted(conflicts | set(_changed(repo, expected, actual)))
    raise VehicleError("cannot verify octopus merge %s: compose with two-parent merges"
                       % sha[:12])


def own_changes(repo: str, base: str, head: str, sources) -> dict:
    """Le delta que le véhicule ajoute au-delà de ses membres.

    `sources` sont les têtes gelées des membres. Les commits du véhicule sont
    ceux de `base..head` qui ne sont pas déjà dans un membre. Un commit de
    fusion est lu par `_merge_own_paths` (conflits et écarts à la fusion
    automatique, choix d'un parent compris) ; un commit simple (manifeste,
    adaptation) est propre pour tout son diff. Rend `{paths, commits}` ; les
    chemins de test et de manifeste sont retirés, et un commit n'est retenu
    que s'il contribue au moins un chemin propre.
    """
    args = ["rev-list", "--topo-order", "--reverse", "%s..%s" % (base, head)]
    sources = [str(source) for source in (sources or ()) if source]
    if sources:
        args += ["--not"] + sources
    listing = plan_git._out(repo, *args)
    if listing is None:
        raise VehicleError("cannot read the vehicle commits %s..%s"
                           % (base[:12], head[:12]))
    paths: list[str] = []
    commits: list[str] = []
    for sha in listing.split():
        parents_text = plan_git._out(repo, "rev-list", "--parents", "-n", "1", sha)
        if parents_text is None:
            raise VehicleError("cannot read the parents of vehicle commit %s"
                               % sha[:12])
        parents = parents_text.split()[1:]
        if not parents:
            continue
        changed = (_merge_own_paths(repo, parents, sha) if len(parents) >= 2
                   else _changed(repo, parents[0], sha))
        changed = sorted({path for path in changed if not _is_review_free(path)})
        if not changed:
            continue
        commits.append(sha)
        for path in changed:
            if path not in paths:
                paths.append(path)
    return {"paths": sorted(paths), "commits": commits}


def designation_changes(db, vehicle: int, row, repo: str, base: str, head: str) -> Optional[dict]:
    """Le delta propre à désigner au gel d'un véhicule, ou None.

    None quand la ligne n'est pas un véhicule, ou quand aucun membre n'est
    lisible : la désignation garde alors le diff gelé entier. Une déclaration
    sans source composée ni gel est ignorée ; un membre composé sans tête
    gelée lève, car ses avis acquis ne peuvent pas être nommés.
    """
    if not is_vehicle(row):
        return None
    found = members(db, repo, base, head, int(vehicle))
    if not found:
        return None
    sources = []
    for entry in found:
        _member_row, _marks, freeze = _member_freeze(
            db, entry["lot"], declared_only=not entry.get("source"))
        if freeze is None:
            continue
        frozen = plan_git._commit(repo, freeze["sha"])
        if not frozen:
            raise VehicleError("constituent #%d: frozen head %s is not a commit of the "
                               "repository" % (int(entry["lot"]), freeze["sha"][:12]))
        sources.append(frozen)
    return own_changes(repo, base, head, sources)


# --------------------------------------------------------------------------
# la porte du véhicule
# --------------------------------------------------------------------------

def _member_freeze(db, lot: int, *, declared_only: bool = False) -> tuple:
    """`(row, marks, freeze)` du membre ; une simple intention peut attendre.

    Sans source composée ni gel, le landing ne permet d'attribuer aucun
    contenu au membre. On l'ignore sans soustraire de commits au delta propre
    du véhicule. Un membre composé sans gel reste refusé (#2775).
    """
    row = work.get(db, int(lot))
    if row is None:
        raise VehicleError("constituent #%d is not a work item" % int(lot))
    marks = work.milestones(db, int(lot))
    freeze = plan_git.latest_freeze(marks)
    sha = str((freeze or {}).get("sha") or "").strip()
    if not sha:
        if declared_only:
            return row, marks, None
        raise VehicleError("constituent #%d has no frozen head: compose a reviewed lot"
                           % int(lot))
    return row, marks, dict(freeze, sha=sha)


def _ancestor(repo: str, tip: str, base: str) -> bool:
    proc = plan_git._git(repo, "merge-base", "--is-ancestor", tip, base)
    if proc.returncode not in (0, 1):
        raise VehicleError("cannot compare %s and %s: git merge-base rc=%d"
                           % (tip[:12], base[:12], proc.returncode))
    return proc.returncode == 0


def need(db, vehicle: int, row, marks, repo: str, base: str, head: str,
         cfg=None, canons=None) -> Optional[dict]:
    """Ce que la porte du véhicule `vehicle` doit exiger, ou None.

    None quand la ligne n'est pas un véhicule, ou quand aucun membre n'est
    lisible : la porte garde alors sa lecture historique (le diff gelé entier),
    jamais un véhicule vide qui passerait sans preuve.

    Sinon rend `{ok, required, klass, why, members, own_paths}`. `ok` vaut
    False dès qu'un membre manque d'avis sur sa tête gelée ou que sa source
    composée ne contient pas cette tête ; `ok` vaut True avec `required == 0`
    quand le véhicule n'a aucun delta propre ; sinon `ok` vaut None et
    `required`/`klass` décrivent le delta propre à relire.
    """
    if not is_vehicle(row):
        return None
    vehicle = int(vehicle)
    try:
        found = members(db, repo, base, head, vehicle)
    except VehicleError as exc:
        return {"ok": False, "required": None, "klass": None,
                "why": str(exc), "members": [], "own_paths": []}
    if not found:
        return None
    total = len(found)
    frozen_sources = []
    constituents = []
    for entry in found:
        lot = int(entry["lot"])
        try:
            member_row, member_marks, freeze = _member_freeze(
                db, lot, declared_only=not entry.get("source"))
        except VehicleError as exc:
            return {"ok": False, "required": None, "klass": None,
                    "why": str(exc), "members": found, "own_paths": []}
        if freeze is None:
            continue
        frozen = plan_git._commit(repo, freeze["sha"])
        if not frozen:
            return {"ok": False, "required": None, "klass": None,
                    "why": "constituent #%d: frozen head %s is not a commit of the "
                           "repository" % (lot, freeze["sha"][:12]),
                    "members": found, "own_paths": []}
        composed = bool(entry.get("source"))
        source = entry.get("source") or frozen
        resolved = plan_git._commit(repo, source)
        if not resolved:
            return {"ok": False, "required": None, "klass": None,
                    "why": "constituent #%d: composed source %s is not a commit of the "
                           "repository" % (lot, str(source)[:12]),
                    "members": found, "own_paths": []}
        try:
            in_vehicle = resolved == head or _ancestor(repo, resolved, head)
            contains = in_vehicle and (resolved == frozen
                                       or _ancestor(repo, frozen, resolved))
        except VehicleError as exc:
            return {"ok": False, "required": None, "klass": None,
                    "why": str(exc), "members": found, "own_paths": []}
        if not in_vehicle:
            if not composed:
                # Une déclaration d'atterrissage peut précéder la composition :
                # ce lot n'est simplement pas encore dans CE véhicule.
                continue
            return {"ok": False, "required": None, "klass": None,
                    "why": "constituent #%d: composed source %s is not in the vehicle "
                           "head %s" % (lot, resolved[:12], head[:12]),
                    "members": found, "own_paths": []}
        if not contains:
            return {"ok": False, "required": None, "klass": None,
                    "why": "constituent #%d: composed source %s does not carry its "
                           "frozen head %s" % (lot, resolved[:12], frozen[:12]),
                    "members": found, "own_paths": []}
        frozen_sources.append(frozen)
        constituents.append(entry)
        # L'avis du membre se lit sur SA tête gelée et SA classe, pas sur le
        # diff du paquet (#2766).
        try:
            from . import lot_class, merge_queue

            klass = lot_class.of_lot(db, lot, cfg=cfg, item=member_row, repo=repo,
                                     head=frozen, milestones=member_marks,
                                     canons=canons)
            gate = merge_queue.review_gate(db, member_marks, frozen, repo=repo,
                                           klass=klass, item=member_row)
        except Exception as exc:  # noqa: BLE001 - lecture impossible : refus
            return {"ok": False, "required": None, "klass": None,
                    "why": "constituent #%d: cannot read its review gate: %s"
                           % (lot, exc), "members": found, "own_paths": []}
        if not gate.get("ok"):
            return {"ok": False, "required": None, "klass": None,
                    "why": "constituent #%d: %s" % (lot, gate.get("why") or "no review"),
                    "members": found, "own_paths": []}
    total = len(constituents)
    if not total:
        return None
    try:
        start = plan_git._out(repo, "merge-base", base, head)
        if not start:
            raise VehicleError("cannot read merge-base(%s, %s)"
                               % (base[:12], head[:12]))
        own = own_changes(repo, start, head, frozen_sources)
    except VehicleError as exc:
        return {"ok": False, "required": None, "klass": None, "why": str(exc),
                "members": found, "own_paths": []}
    if not own["paths"]:
        return {"ok": True, "required": 0, "klass": None, "members": found,
                "own_paths": [], "why": ("%d constituent(s) reviewed on their frozen "
                                         "head(s); no proper vehicle delta" % total)}
    try:
        from . import lot_class

        canons = canons if canons is not None else work.configured_canons(cfg)
        klass = lot_class.class_of_paths(canons, str((row or {}).get("app") or ""),
                                         own["paths"])
        from . import merge_queue

        required = merge_queue.required_verdicts(klass)
    except Exception as exc:  # noqa: BLE001 - classe illisible : refus
        return {"ok": False, "required": None, "klass": None,
                "why": "vehicle class unreadable: %s" % exc, "members": found,
                "own_paths": own["paths"]}
    return {"ok": None, "required": required, "klass": klass, "members": found,
            "own_paths": own["paths"], "commits": own["commits"],
            "why": "proper delta: %d path(s)" % len(own["paths"])}
