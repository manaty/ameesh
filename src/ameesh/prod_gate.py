# SPDX-License-Identifier: AGPL-3.0-only
"""Lot de porte de production : fermé par sa présence sur la branche de prod
(#3022, canon §5, décision owner #122895 option a, lot #2987).

Le défaut d'une porte de production servie depuis la branche de prod (Nexlink :
`main`) n'est pas « fait » quand son correctif entre dans l'intégration
(`develop`) : la porte reste défectueuse jusqu'à la release ou au correctif
d'incident (cas #2915, #2981). Un tel lot porte la marque

    prod-gate: <garde : ce qui tient la porte fermée d'ici là>

posée par une note (`ameesh work note <id> "prod-gate: …"`) ; la dernière
marque fait foi, une note `prod-gate lifted: <raison>` la lève. Un changement
d'état ne la lève pas : c'est la présence sur la branche de prod qui ferme.

`work.close_merged` consulte [`check`] sous le verrou du lot, quelle que soit
la voie de constat (contenu, branche, paquet, PR, déclaration) :

* lot sans marque : rien n'est lu, la fermeture suit son cours ;
* lot marqué : la branche de prod de son projet vient du canon compilé
  (`projects.<app>.production.branch`, `mechanics`) — absente ou mal formée,
  la fermeture est retenue, aucune branche n'est supposée ; le dernier gel du
  lot (sinon le commit constaté) doit être entré dans `origin/<branche>`
  (`plan_git.probe` : ancêtre, patch-id, squash, revert compris). Sinon le lot
  reste ouvert, « en attente prod », la garde nommée dans son journal.

Quand la présence est prouvée, la note de fusion nomme la voie et le commit
de la branche de prod. Une lecture en erreur retient toujours : elle
n'autorise jamais une fermeture.
"""
from __future__ import annotations

import re

#: la marque d'un lot de porte de production, et sa levée explicite
_MARK = re.compile(r"^prod-gate\s*:", re.I)
_LIFT = re.compile(r"^prod-gate\s+lifted\s*:", re.I)
#: motifs donnés au stockage (`Progress.lot_hold_events`), syntaxe commune
#: `re` / ARE PostgreSQL ; le filtre exact reste ici
NOTE_PATTERNS = (_MARK.pattern, _LIFT.pattern)
#: longueur gardée de la garde dans les motifs et le journal
_GUARD_MAX = 160


def is_mark(text) -> bool:
    return bool(_MARK.match(str(text or "").strip()))


def is_lift(text) -> bool:
    return bool(_LIFT.match(str(text or "").strip()))


def marking(events) -> dict | None:
    """La marque en vigueur dans le journal d'un lot, ou None.

    Les événements sont lus dans l'ordre d'écriture ; la dernière marque ou
    levée l'emporte. Rend `{guard, by, since}`."""
    current = None
    for event in events or ():
        text = str(event.get("note") or "").strip()
        if is_lift(text):
            current = None
        elif is_mark(text):
            guard = " ".join(text.split(":", 1)[1].split())[:_GUARD_MAX]
            current = {"guard": guard, "by": str(event.get("actor") or ""),
                       "since": event.get("created_ts")}
    return current


def read_marking(st, lot: int) -> dict | None:
    """La marque d'un lot, lue au journal (lecture ciblée des notes)."""
    events = st.progress.lot_hold_events([int(lot)], NOTE_PATTERNS)
    return marking(e for e in events if int(e["work_item_id"]) == int(lot))


def _retention(what: str, motif: str, mark: dict) -> dict:
    return {"what": what, "motif": motif, "by": mark.get("by") or "",
            "since": mark.get("since"), "guard": mark.get("guard") or ""}


def _guarded(mark: dict) -> str:
    return " (garde : %s)" % mark["guard"] if mark.get("guard") else ""


def _repository(item: dict, repo: str | None, cfg) -> str:
    from . import work as work_mod
    if repo:
        return repo
    return work_mod._checkout(work_mod.lot_repository(item, cfg), None, cfg)


def _production_ref(repo: str, branch: str) -> str | None:
    """La ref publiée de la branche de prod (`origin/<b>`), la locale à défaut."""
    from . import plan_git
    for ref in ("refs/remotes/origin/" + branch, "refs/heads/" + branch):
        if plan_git._commit(repo, ref):
            return ref
    return None


def check(st, item: dict, *, sha: str = "", repo: str | None = None, cfg=None) -> dict:
    """La porte de production d'un lot à fermer.

    Rend `{"retention": dict | None, "proof": dict | None}` : une retenue
    (`what` : `unreadable` pour une erreur de lecture, `production` sinon,
    `motif` lisible) refuse la fermeture ; `proof` (`{branch, ref, how,
    commit}`) dit, pour un lot marqué, où et comment le correctif a atteint la
    branche de prod. Un lot sans marque rend `{retention: None, proof: None}`.
    """
    from . import mechanics, plan_git
    from . import work as work_mod
    from .canon import CanonError, GitError
    lot = int(item["id"])
    try:
        mark = read_marking(st, lot)
    except Exception as exc:  # noqa: BLE001 — l'erreur retient, elle n'ouvre pas
        return {"retention": {
            "what": "unreadable", "by": str(item.get("assignee") or ""), "since": None,
            "motif": "marque de porte de production illisible (%s: %s)"
                     % (type(exc).__name__, " ".join(str(exc).split())[:120])},
            "proof": None}
    if mark is None:
        return {"retention": None, "proof": None}
    project = str(item.get("app") or "").strip()
    try:
        if not project:
            raise mechanics.MechanicsError("the lot has no project (app)")
        branch = mechanics.configured(cfg).production_branch(project)
    except CanonError as exc:          # MechanicsError comprise : aucun repli
        return {"retention": _retention(
            "production", "porte de production%s : branche de prod inconnue (%s), "
            "fermeture refusée" % (_guarded(mark), " ".join(str(exc).split())[:160]),
            mark), "proof": None}
    candidates = []
    try:
        marks = st.progress.lot_milestones([lot])
        freeze = plan_git.latest_freeze(
            [m for m in marks if int(m["work_item_id"]) == lot])
        frozen = plan_git.full_commit_id((freeze or {}).get("sha"))
    except Exception:  # noqa: BLE001 — sans gel lisible, le commit constaté seul
        frozen = ""
    for value in (frozen, plan_git.full_commit_id(sha)):
        if value and value not in candidates:
            candidates.append(value)
    waiting = "en attente prod : %s" % branch
    if not candidates:
        return {"retention": _retention(
            "production", "porte de production%s : %s, aucun commit à y chercher"
            % (_guarded(mark), waiting), mark), "proof": None}
    try:
        checkout = _repository(item, repo, cfg)
        ref = _production_ref(checkout, branch)
        if ref is None:
            raise plan_git.MergeProbeError("branch %s not found in %s" % (branch, checkout))
        for commit in candidates:
            found = plan_git.probe(checkout, commit, ref, lot=lot)
            if found["merged"]:
                return {"retention": None, "proof": {
                    "branch": branch, "ref": found["ref"] or commit,
                    "how": found["how"], "commit": commit,
                    "guard": mark.get("guard") or ""}}
    except (plan_git.MergeProbeError, GitError, work_mod.WorkError) as exc:
        # sans preuve, la porte reste gardée
        return {"retention": _retention(
            "production", "porte de production%s : %s, présence non prouvée (%s)"
            % (_guarded(mark), waiting, " ".join(str(exc).split())[:160]), mark),
            "proof": None}
    return {"retention": _retention(
        "production", "porte de production%s : %s, %s absent de %s"
        % (_guarded(mark), waiting, candidates[0][:12], branch), mark), "proof": None}


def proof_text(proof: dict) -> str:
    """La mention de la note de fusion d'un lot marqué : la voie et le commit."""
    text = "porte de production : présent sur %s (%s %s)" % (
        proof["branch"], proof.get("how") or "?", str(proof.get("ref") or "")[:12])
    if proof.get("guard"):
        text += ", garde levée : %s" % proof["guard"]
    return text
