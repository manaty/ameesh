# SPDX-License-Identifier: AGPL-3.0-only
"""L750/L756 (#561/#566) — le périmètre d'une release, et sa porte.

Une release se fige à un commit **nommé** de sa cible quand le lot de release
est ouvert (`ameesh release open <lot> --base <commit>`) :

* les fusions de la cible postérieures au gel vont à la release suivante ;
* seuls les correctifs d'un bloquant de cette release sont cherry-pickés sur
  `release/x.y`, chacun avec sa propre exécution impactée ;
* la suite **complète** tourne une fois sur la tête gelée, et c'est elle qui
  certifie la release (L756) — jamais le plan impacté d'une PR, jamais le
  check vide d'une PR de version seule.

`ameesh release check <lot>` refuse une branche de release dont la base a
bougé après le gel sans raison enregistrée (`ameesh release reason <lot>
--text …`), et refuse l'absence d'une exécution de suite complète verte sur la
tête gelée exacte. La file de fusion applique la même porte à un lot de classe
release qui la traverse (`merge_queue.readiness`).

Le périmètre et la raison vivent au **journal du lot** (`release-perimeter:
<sha>`, `release-base-moved: <texte>`), comme toute règle dictée : ils se
relisent par `ameesh work show` et par les relecteurs.
"""
from __future__ import annotations

import json
import re
import sys

from . import lot_class, plan_git, work

#: le périmètre de release, écrit au journal du lot : `<préfixe> <sha>`
PERIMETER_PREFIX = "release-perimeter:"
#: la raison enregistrée d'un écart : `<préfixe> <texte>`
REASON_PREFIX = "release-base-moved:"
#: un commit abrégé (7) ou complet (40), jamais autre chose
_HEX = re.compile(r"^[0-9a-f]{7,40}$")


class ReleaseError(RuntimeError):
    """La porte d'une release refuse : le message dit quoi corriger."""


def _events(db, lot):
    return work.events(db, lot, limit=200) or []


def _last_note(db, lot, prefix: str) -> str:
    """Le dernier texte journalisé sous `prefix` (les événements sont relus du
    plus récent au plus ancien), ou la chaîne vide."""
    for row in _events(db, lot):
        text = str(row.get("note") or "")
        if text.startswith(prefix):
            return text[len(prefix):].strip()
    return ""


def perimeter(db, lot) -> str:
    """Le commit de la cible gelé comme périmètre, ou la chaîne vide."""
    return _last_note(db, lot, PERIMETER_PREFIX)


def reason(db, lot) -> str:
    """La raison enregistrée d'une base qui a bougé, ou la chaîne vide."""
    return _last_note(db, lot, REASON_PREFIX)


def record_perimeter(db, lot, base: str, *, actor: str = "") -> str:
    """Fige le périmètre de la release `lot` au commit `base` de sa cible.

    Idempotent pour le même commit ; refuse un autre périmètre : une autre
    release est un autre lot. Lève `ReleaseError` (commit illisible, lot
    inconnu, périmètre déjà figé ailleurs)."""
    value = str(base or "").strip().lower()
    if not _HEX.fullmatch(value):
        raise ReleaseError("release perimeter: %r is not a commit id (7 to 40 hex digits)"
                           % base)
    if work.get(db, lot) is None:
        raise ReleaseError("lot %s not found" % lot)
    current = perimeter(db, lot).lower()
    if current:
        if current.startswith(value) or value.startswith(current):
            return current
        raise ReleaseError("lot #%s already freezes its perimeter at %s: a new release is a new "
                           "lot" % (lot, current[:12]))
    work.note(db, lot, "%s %s" % (PERIMETER_PREFIX, value), actor=actor)
    return value


def record_reason(db, lot, text: str, *, actor: str = "") -> str:
    """Enregistre pourquoi la base de la release a bougé après le gel."""
    value = str(text or "").strip()
    if not value:
        raise ReleaseError("release base moved: a reason is required (--text \"…\")")
    if work.get(db, lot) is None:
        raise ReleaseError("lot %s not found" % lot)
    work.note(db, lot, "%s %s" % (REASON_PREFIX, value), actor=actor)
    return value


def base_moved(repo: str, branch: str, target: str, frozen: str) -> dict:
    """La base de la branche `branch` a-t-elle bougé depuis le périmètre ?

    La base est le merge-base de la branche et de sa cible : tant que la
    branche n'a pas absorbé la cible, il vaut le périmètre ; dès qu'une fusion
    (ou un rebasage) la fait avancer, il n'est plus le commit gelé. Le dépôt
    est lu tel quel — l'appelant fait `git fetch` avant."""
    tip = plan_git._commit(repo, branch)
    if not tip:
        raise ReleaseError("branch %s is not in %s" % (branch, repo))
    ref = plan_git._commit(repo, target)
    if not ref:
        raise ReleaseError("target %s is not in %s" % (target, repo))
    base = plan_git._out(repo, "merge-base", tip, ref)
    if not base:
        raise ReleaseError("cannot read the merge base of %s and %s in %s"
                           % (branch, target, repo))
    pinned = plan_git._commit(repo, frozen)
    if not pinned:
        raise ReleaseError("the frozen perimeter %s is not a commit of %s"
                           % (frozen[:12], repo))
    return {"branch": branch, "target": target, "tip": tip, "base": base,
            "perimeter": pinned, "moved": base != pinned}


def project_of(cfg, item: dict):
    """Le projet radar du lot (`row["app"]`), ou None : jamais d'exception."""
    from . import radar
    name = str(item.get("app") or "").strip()
    if cfg is None or not name:
        return None
    try:
        return radar.settings_of(cfg).projects.get(name)
    except Exception:  # noqa: BLE001 — réglages radar illisibles : pas de projet
        return None


def check(db, lot, *, repo: str | None = None, cfg=None, gh=None, full_ci=None) -> dict:
    """La porte d'une release : périmètre gelé, base non élargie, suite
    complète verte sur la **tête fraîche** de la branche. Certifier la seule
    tête gelée laisserait passer un commit postérieur au gel (correctif
    cherry-pické) sans exécution propre : la tête qui serait publiée est celle
    qui est certifiée. Rend le constat (`{lot, target, perimeter, base, moved,
    reason, frozen_sha, tip, certified, head_moved, full_suite}`) ou lève
    `ReleaseError` avec le geste qui manque."""
    from . import merge_queue
    row = work.get(db, lot)
    if row is None:
        raise ReleaseError("lot %s not found" % lot)
    repo = repo or lot_class.repository(cfg, row)
    if not repo:
        raise ReleaseError("lot #%s has no repository (radar.projects.%s.repo, or --repo)"
                           % (lot, row.get("app") or "?"))
    target = lot_class.default_base(cfg, row, repo)
    why = lot_class.release_reason(row, target)
    branch = str(row.get("branch") or "").strip()
    if not why and branch.startswith("release/"):
        # une PR de release (release/x.y -> main) peut n'être release que par sa
        # branche : la porte s'applique quand même (le geste est une publication)
        why = "release branch %s" % branch
    if not why:
        raise ReleaseError("lot #%s is not a release lot (%s)" % (lot, why or "no release axis"))
    frozen = perimeter(db, lot)
    if not frozen:
        raise ReleaseError("lot #%s has no frozen release perimeter: ameesh release open %s "
                           "--base <commit of %s>" % (lot, lot, target or "the target"))
    if not branch:
        raise ReleaseError("lot #%s has no branch" % lot)
    movement = base_moved(repo, branch, target, frozen)
    recorded = reason(db, lot)
    if movement["moved"] and not recorded:
        raise ReleaseError(
            "release branch %s absorbed %s after the freeze (perimeter %s, merge base %s): "
            "later merges go to the next release, only a fix for this one is cherry-picked "
            "(with its own impacted run). Record the reason if this is deliberate: "
            "ameesh release reason %s --text \"…\""
            % (branch, target, movement["perimeter"][:12], movement["base"][:12], lot))
    freeze = plan_git.latest_freeze(work.milestones(db, lot))
    if not freeze or not freeze.get("sha"):
        raise ReleaseError("lot #%s is not frozen: ameesh work milestone %s frozen --sha <commit>"
                           % (lot, lot))
    frozen_tip = merge_queue.pinned(repo, freeze["sha"])
    if frozen_tip is None:
        raise ReleaseError("the freeze of lot #%s names %r, not a commit of %s"
                           % (lot, freeze["sha"], repo))
    head = movement["tip"]
    project = project_of(cfg, row)
    try:
        runs = merge_queue.required_full_ci(project, head, full_ci, lot=lot)
    except merge_queue.QueueError as exc:
        raise ReleaseError(str(exc)) from exc
    return {"schema": "ameesh-release/1", "lot": lot, "target": target,
            "perimeter": movement["perimeter"],
            "branch": branch, "tip": head, "base": movement["base"],
            "moved": movement["moved"], "reason": recorded, "frozen_sha": frozen_tip,
            "certified": head, "head_moved": head != frozen_tip,
            "class_why": why, "full_suite": runs}


def render(report: dict) -> list[str]:
    """Les lignes de `ameesh release check` (une preuve lisible)."""
    runs = report.get("full_suite") or {}
    return [
        "lot #%s: release perimeter %s (%s)" % (report["lot"], report["perimeter"][:12],
                                                report["class_why"]),
        "  branch     %s -> %s, base %s%s" % (
            report["branch"], report["target"], report["base"][:12],
            " (MOVED after the freeze)" if report["moved"] else " (frozen)"),
        "  reason     %s" % (report["reason"] or "—"),
        "  frozen     %s" % report["frozen_sha"][:12],
        "  head       %s%s" % (report["tip"][:12],
                               " (MOVED after the freeze: certified itself)"
                               if report.get("head_moved") else ""),
        "  full suite %s/%s run(s), %s" % (runs.get("runs", 0), runs.get("suites", "?"),
                                           "green" if runs.get("green") else "NOT green"),
    ]


def run(cfg, args) -> int:
    """`ameesh release open|check|reason` : la porte de release (L750/L756)."""
    from .db import open_db
    db = open_db(cfg)
    try:
        if args.release_command == "open":
            base = record_perimeter(db, args.id, args.base, actor=args.actor)
            print("lot #%d: release perimeter frozen at %s" % (args.id, base[:12]))
            return 0
        if args.release_command == "reason":
            text = record_reason(db, args.id, args.text, actor=args.actor)
            print("lot #%d: reason recorded (%s)" % (args.id, text))
            return 0
        report = check(db, args.id, repo=args.repo, cfg=cfg)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        else:
            print("\n".join(render(report)))
        return 0
    except ReleaseError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1


def add_parser(sub, func) -> None:
    parser = sub.add_parser(
        "release", help="release perimeter and gate (L750/L756): freeze the base at a named "
                        "commit, refuse a moved base or a missing full suite")
    rel = parser.add_subparsers(dest="release_command", required=True)
    open_p = rel.add_parser(
        "open", help="freeze the release perimeter at a named commit of its target")
    open_p.add_argument("id", type=int)
    open_p.add_argument("--base", required=True,
                        help="commit of the target the release is cut from")
    open_p.add_argument("--actor", default="")
    open_p.set_defaults(func=func)
    check_p = rel.add_parser(
        "check", help="refuse a moved base without a reason, or a missing full-suite run")
    check_p.add_argument("id", type=int)
    check_p.add_argument("--repo", default=None, help="the project repository (default: its "
                                                      "declared radar.projects repository)")
    check_p.add_argument("--json", action="store_true", help="`ameesh-release/1` report")
    check_p.set_defaults(func=func)
    reason_p = rel.add_parser(
        "reason", help="record why the release branch's base moved after the freeze")
    reason_p.add_argument("id", type=int)
    reason_p.add_argument("--text", required=True)
    reason_p.add_argument("--actor", default="")
    reason_p.set_defaults(func=func)
