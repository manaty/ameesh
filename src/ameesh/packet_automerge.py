# SPDX-License-Identifier: AGPL-3.0-only
"""#2813 : la fusion automatique d'un paquet, armée à sa publication (canon §12).

Décision owner du 20/10 (« yes » au bureau, fil du lot #2813) : aucun geste
ameesh n'armait la fusion des paquets ; h-1700, h-1900 et h-2000 sont restés
verts et ouverts jusqu'à la fusion manuelle du bureau. Le geste qui publie le
paquet arme désormais sa fusion sur la tête gelée ; tout nouveau push la
désarme, et la publication suivante la réarme sur la nouvelle tête.

**Pourquoi pas l'auto-fusion GitHub** (`gh pr merge --auto`, relecture
ameesh-cx2 de 249b5911, B1/P1) : GitHub ne vérifie `--match-head-commit`
qu'à l'armement et ne désarme pas sur un push d'un compte en écriture (#1656,
`fire` P1-4). Une tête poussée après la publication et déjà verte (commit
déjà testé, run lancé à la main) fusionnerait avant toute surveillance. La
fusion est donc faite **par ameesh**, et aucune auto-fusion GitHub n'est
laissée armée. Le geste lie la tête **et** la cible (R2, ameesh-cx2) :
`gh pr merge --match-head-commit` ne lie que la tête, une PR reciblée juste
avant fusionnerait ailleurs ; l'API « merge a branch » (`POST
repos/<slug>/merges`, `base=<cible épinglée>`, `head=<tête publiée>`)
écrit la branche qu'elle nomme et le commit qu'elle nomme (`merge_into`).

* **Publier** un porteur `integration/**`, c'est demander sa suite complète :
  `ameesh ci run <porteur> --full` (étiquette `ci-full-<tête>`). Juste après la
  pose (ou sur une composition déjà demandée ou verte), `arm` épingle au
  journal du lot la tête, la PR et la cible publiées (`auto-merge armed …
  base=<cible>`), désarme une auto-fusion GitHub trouvée sur la PR, puis
  tente la fusion tout de suite (composition déjà verte).
* **Seule une garde de la forge est prouvée** : l'armement exige un check
  requis imposé par GitHub sur la cible (protection classique ou ruleset,
  `fire.required_checks` sans déclaration de projet), et la fusion exige ces
  checks présents et `SUCCESS` sur la tête exacte (`fire.ready_pr`) — un
  jeton administrateur passerait outre la protection, ameesh la relit ; une
  déclaration de projet s'y ajoute, elle ne remplace jamais la forge.
  Sans check imposé (manaty/nexlink, privé sans GitHub Pro, protection en
  403), rien n'est armé : l'équivalent est le feu du pilote à la composition
  (`ameesh work fire <porteur> --sha <tête>`), que le message rappelle.
* **Le passage** (`watch`, `ameesh release tick` des projets de
  `packet_watch`, toutes les cinq minutes) relit chaque porteur ouvert armé :
  tête, cible ou gel changé depuis la publication → désarmé (journal, et
  auto-fusion GitHub éventuelle retirée). Sinon la fusion passe les mêmes
  portes que le feu (R1, ameesh-cx3) : `fire.ready_verdicts` (classe, avis
  requis et indépendants sur la tête gelée, membres d'un porteur et delta,
  #2766), puis au moins un check imposé par la forge — et tous ceux épinglés
  à la publication — `SUCCESS` sur la tête exacte, avec le projet résolu
  (checks déclarés, `required_statuses`, `ci_required` ; R2, ameesh-cx3) par
  `fire.ready_pr`. La PR est relue juste avant le geste (tête, cible, état).
  La vitesse du passage ne fait que la latence de la fusion, jamais sa
  sûreté : une autre tête ou une autre cible ne fusionne jamais par ce
  chemin.

La trace est le journal du lot (aucun état local) : `auto-merge armed …`,
`disarmed …`, `merged …` et `not-armed …`, avec dépôt, PR, tête et cible. La
publication et le passage d'un même porteur se sérialisent sous le bail
`auto-merge:<lot>` ; le passage relit les marques sous ce bail.
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
from urllib.parse import quote, unquote

from . import fire, land, storage, work

#: une tête de porteur de paquet (§12) ; `integration/paquet-h-…`, `…-nexlink`
CARRIER_PREFIX = "integration/"
#: décision owner : un commit de fusion, comme les fusions manuelles des paquets
METHOD = "merge"
#: états d'un porteur que `watch` relit
OPEN_STATES = ("intake", "build", "qa", "blocked", "waiting_human")
#: durée du bail `auto-merge:<lot>` qui sérialise la publication et le passage ;
#: il est renouvelé avant chaque geste (B4 : plusieurs appels `gh` de 60 à 120 s)
LOCK_TTL_S = 300.0
#: L165 (EP4) : marge du bail élu avant `merge_into` (POST /merges, 120 s de
#: timeout + lecture) ; la course résiduelle est documentée dans
#: docs/HOST-HANDOFFS.md.
AUTOMERGE_CALL_MARGIN_S = 180.0
#: une marque du journal du lot ; un armement porte toujours sa cible
MARK_RE = re.compile(
    r"^auto-merge (?P<state>armed|disarmed|merged|not-armed) "
    r"repo=(?P<repo>[\w.-]+/[\w.-]+) pr=(?P<pr>\d+) sha=(?P<sha>[0-9a-f]{40})"
    r"(?: base=(?P<base>[\w./-]+))?(?: method=[a-z]+)?(?: required=(?P<required>\S+))?"
    r"(?:\s|$)")
#: marques qui referment un armement de la même PR
CLOSING = ("disarmed", "merged")
_SHA = re.compile(r"[0-9a-f]{40}")


def carrier(head_ref) -> bool:
    """La source d'une PR est-elle un porteur de paquet ?"""
    return str(head_ref or "").strip().startswith(CARRIER_PREFIX)


def gh_of(runner):
    """Un `gh` appelable (`gh(args, timeout=…)` → CompletedProcess, celui de
    `land` et `fire`) à partir d'un `plan_github.Gh` (`run(argv)` → tuple)."""
    def call(args, timeout: float = 60.0):
        code, out, err = runner.run(list(args))
        return subprocess.CompletedProcess(["gh", *args], code, out or "", err or "")
    return call


# --------------------------------------------------------------------------
# journal
# --------------------------------------------------------------------------

def marks(db, lot: int, *, limit: int = 200) -> list[dict]:
    """Les marques de fusion automatique du lot, de la plus ancienne à la plus
    récente."""
    out = []
    for ev in work.events(db, int(lot), limit=limit) or ():
        m = MARK_RE.match((ev.get("note") or "").strip())
        if m:
            out.append({"id": int(ev.get("id") or 0), "state": m.group("state"),
                        "repo": m.group("repo"), "pr": int(m.group("pr")),
                        "sha": m.group("sha"), "base": m.group("base") or "",
                        "required": tuple(unquote(name) for name in
                                          (m.group("required") or "").split(",") if name)})
    return sorted(out, key=lambda m: m["id"])


def active(rows) -> dict | None:
    """Le dernier armement qu'aucun désarmement ni fusion n'a suivi, ou None.

    Un armement sans cible ni checks requis ne compte pas (la cible et les
    checks imposés à la publication sont épinglés, relectures R0/R1). Un
    `not-armed` ne l'efface pas : une republication refusée laisse l'armement
    précédent sous la garde du passage."""
    current = None
    for row in rows or ():
        if row["state"] == "armed":
            current = row if row["base"] and row["required"] else None
        elif row["state"] in CLOSING and current is not None \
                and current["pr"] == row["pr"] and current["repo"] == row["repo"]:
            current = None
    return current


def _note(db, lot: int, state: str, slug: str, number: int, sha: str, rest: str,
          actor: str) -> None:
    work.note(db, int(lot), "auto-merge %s repo=%s pr=%d sha=%s %s"
              % (state, slug, int(number), sha, rest.strip()), actor=actor)


def _fenced_hold(db, fence, *, margin_s: float = 0.0) -> bool:
    """L165 (EP4) : renouvelle la garde si elle existe ; False si perdue."""
    if fence is None:
        return True
    from . import pilot_gesture
    try:
        fence.hold(db, margin_s=margin_s)
    except pilot_gesture.GestureRefused:
        return False
    return True


def _note_fenced(db, fence, lot: int, state: str, slug: str, number: int, sha: str,
                 rest: str, actor: str, *, margin_s: float = 0.0) -> bool:
    """Écrit la marque dans la même transaction que le hold du bail élu.

    Sans garde (exécuteur/humain), c'est la note simple. Avec une garde, une
    époque périmée n'écrit pas la marque : c'est la correction B1 de la
    relecture cx2 (le marqueur `armed` ne doit pas survivre à une bascule).
    """
    if fence is None:
        _note(db, lot, state, slug, number, sha, rest, actor)
        return True
    from . import pilot_gesture
    with db.transaction() as tx:
        try:
            fence.hold(tx, margin_s=margin_s)
        except pilot_gesture.GestureRefused:
            return False
        _note(tx, lot, state, slug, number, sha, rest, actor)
        return True


def _lock(db, lot: int):
    """Le bail `auto-merge:<lot>`, ou None si un autre le tient.

    La publication et le passage le prennent tous deux : sans lui, un passage
    qui a lu l'ancien armement désarmerait celui que la publication vient de
    poser sur la nouvelle tête."""
    ops = storage.of(db).work
    owner = "auto-merge:%s:%d" % (socket.gethostname(), os.getpid())
    key = "auto-merge:%d" % int(lot)
    epoch = ops.land_lock(key, owner, LOCK_TTL_S)
    return (ops, owner, epoch, key) if epoch is not None else None


def _renew(handle) -> bool:
    """Le bail est-il encore le nôtre ? Le renouvelle au passage. Appelé juste
    avant chaque geste (désarmement, fusion, marque) : un détenteur périmé
    n'agit pas (même règle que `fire._renew`)."""
    if handle is None:          # dry-run, ou appel de test sans bail
        return True
    ops, owner, epoch, key = handle
    try:
        return ops.land_lock(key, owner, LOCK_TTL_S, epoch=epoch) is not None
    except Exception:  # noqa: BLE001 - bail illisible : on ne mute pas
        return False


def _unlock(handle) -> None:
    ops, owner, epoch, key = handle
    try:
        ops.land_unlock(key, owner, epoch)
    except Exception:  # noqa: BLE001 - le bail expire seul
        pass


# --------------------------------------------------------------------------
# GitHub
# --------------------------------------------------------------------------

def _error(proc) -> str:
    return ((proc.stderr or proc.stdout or "").strip()[:300]
            or "exit %d" % proc.returncode)


def pr_status(slug: str, number: int, *, gh) -> dict:
    """La PR `number`, dans la forme que lit `fire.ready_pr` (état, tête,
    cible, brouillon, CI, rollup, branche) plus `auto` et la cible résolue
    (`base_oid`, #3091) ; `land.LandError` sinon."""
    from . import radar
    proc = gh(["pr", "view", str(int(number)), "--repo", slug, "--json",
               "state,headRefOid,headRefName,baseRefName,baseRefOid,isDraft,"
               "autoMergeRequest,statusCheckRollup,title"])
    if proc.returncode != 0:
        raise land.LandError("gh pr view %d: %s" % (int(number), _error(proc)))
    try:
        data = json.loads(proc.stdout or "")
    except ValueError as exc:
        raise land.LandError("gh pr view %d: unreadable output" % int(number)) from exc
    if not isinstance(data, dict):
        raise land.LandError("gh pr view %d: unexpected output" % int(number))
    rollup = radar.merge_rollup(data.get("statusCheckRollup"), slug=slug,
                                head=data.get("headRefOid") or "",
                                branch=data.get("headRefName") or "", gh=gh)
    # `ci` garde son sens strict (file/affichage) ; `ci_rule` porte la règle
    # owner du 23/10 (#3085) que lit `fire.ready_pr`.
    pending = any((c.get("status") or "COMPLETED") != "COMPLETED"
                  or (c.get("state") or "") == "PENDING" for c in rollup)
    ci = "green" if radar.ci_green(rollup) else "pending" if pending or not rollup else "red"
    ci_rule = radar.ci_state(rollup)
    return {"number": int(number), "state": str(data.get("state") or "").upper(),
            "head": str(data.get("headRefOid") or "").strip().lower(),
            "base": str(data.get("baseRefName") or "").strip(),
            # #3091 : la cible résolue de la PR, la seule lecture fraîche que
            # la porte des avis puisse opposer à la ref locale du checkout.
            "base_oid": str(data.get("baseRefOid") or "").strip().lower(),
            "branch": str(data.get("headRefName") or "").strip(),
            "draft": bool(data.get("isDraft")), "ci": ci, "ci_rule": ci_rule,
            "rollup": rollup,
            "auto": data.get("autoMergeRequest") is not None,
            "title": str(data.get("title") or "")}


# --------------------------------------------------------------------------
# publication
# --------------------------------------------------------------------------

def arm(cfg, db, lot: int, *, slug: str, number: int, sha: str, target: str, gh,
        actor: str = work.SYSTEM_SENDER) -> dict:
    """Arme la fusion du porteur `lot` (PR `number` de `slug`) sur `sha`,
    vers `target`.

    Rend `{state, why, merge?}` ; `state` vaut `armed`, `already-armed`,
    `unprotected` (aucun check imposé par la forge : rien d'armé, le feu est
    le geste), `moved` (la PR n'est plus sur `sha`) ou `error` ; `merge` est
    l'issue de la tentative immédiate (`fusionné` si la composition était
    déjà verte). Hors `already-armed`, chaque issue est journalisée sur le
    lot ; aucune n'est fatale à la demande de CI qui la précède."""
    sha = str(sha or "").strip().lower()
    number = int(number)
    if not _SHA.fullmatch(sha):
        return {"state": "error", "why": "the frozen head %r is not a full commit id" % sha}
    handle = _lock(db, lot)
    if handle is None:
        return {"state": "error", "why": "another auto-merge operation holds lot #%d: "
                                         "rerun `ameesh ci run %d --full`" % (lot, lot)}
    try:
        # L165 (EP4) : l'armement est un geste de fusion du pilote (il ouvre
        # le chemin de `merge_into`) ; sans identité de rôle, `gate` rend None
        # et le passage d'exécuteur reste inchangé. Le merge immédiat reçoit
        # la même garde et la renouvelle juste avant l'appel.
        from . import pilot_gesture
        try:
            fence = pilot_gesture.gate(cfg, db, "merge", "auto-merge arm #%d" % lot)
        except pilot_gesture.GestureRefused as exc:
            return {"state": "error", "why": str(exc)}
        out = _arm_locked(db, lot, slug=slug, number=number, sha=sha, target=target,
                          gh=gh, actor=actor, handle=handle, fence=fence)
        if out["state"] in ("armed", "already-armed"):
            # une composition déjà verte fusionne tout de suite, sous le même bail
            out["merge"] = _check_locked(cfg, db, lot, gh=gh, dry_run=False, actor=actor,
                                         handle=handle, fence=fence)
        return out
    finally:
        _unlock(handle)


def _arm_locked(db, lot: int, *, slug: str, number: int, sha: str, target: str, gh,
                actor: str, handle=None, fence=None) -> dict:
    lost = {"state": "error", "why": "the auto-merge lease of lot #%d was lost: rerun "
                                     "`ameesh ci run %d --full`" % (lot, lot)}

    def refuse(state: str, why: str) -> dict:
        _note_fenced(db, fence, lot, "not-armed", slug, number, sha, "— %s" % why,
                         actor)
        return {"state": state, "why": why}

    if not target:
        return refuse("error", "no target branch: the merge cannot be pinned")
    try:
        required = fire.required_checks(slug, target, None, gh=gh)
    except land.LandError as exc:
        return refuse("unprotected" if isinstance(exc, land.ForbiddenLandError) else "error",
                      "the required checks of %s cannot be read (%s): nothing to prove "
                      "the CI with, nothing armed%s"
                      % (target, exc, _fire_hint(lot, sha)
                         if isinstance(exc, land.ForbiddenLandError) else ""))
    if not required:
        return refuse("unprotected",
                      "%s has no required check enforced by GitHub: nothing to prove the "
                      "CI with, nothing armed%s" % (target, _fire_hint(lot, sha)))
    try:
        view = pr_status(slug, number, gh=gh)
    except land.LandError as exc:
        return refuse("error", str(exc))
    if view["state"] != "OPEN":
        return refuse("error", "PR #%d is %s" % (number, view["state"] or "unreadable"))
    if view["head"] != sha:
        return refuse("moved", "PR #%d head is %s, not the frozen %s"
                      % (number, view["head"][:12] or "?", sha[:12]))
    if view["base"] != target:
        return refuse("error", "PR #%d targets %s, not %s" % (number, view["base"] or "?",
                                                            target))
    if view["auto"]:
        # B1 (#1656, `fire` P1-4) : une auto-fusion GitHub fusionnerait la
        # prochaine tête poussée ; ameesh fusionne lui-même, sur la tête publiée.
        if not _renew(handle):
            return lost
        if not _fenced_hold(db, fence, margin_s=AUTOMERGE_CALL_MARGIN_S):
            return {"state": "error", "why": "the elected pilot lease was lost before "
                                             "disarming the GitHub auto-merge: nothing armed"}
        error = land.pr_disable_auto(slug, number, gh=gh)
        if error is not None:
            return refuse("error", "the GitHub auto-merge armed on PR #%d could not be "
                                   "disarmed (%s): ameesh merges the published head itself"
                          % (number, error))
    mine = active(marks(db, lot))
    if mine and (mine["sha"], mine["pr"], mine["repo"], mine["base"]) == \
            (sha, number, slug, target):
        return {"state": "already-armed",
                "why": "already armed on %s → %s" % (sha[:12], target)}
    names = ",".join(dict.fromkeys(quote(context, safe="")
                                   for context, _app in required))
    if not _renew(handle):
        return lost
    if not _note_fenced(db, fence, lot, "armed", slug, number, sha,
                        "base=%s method=%s required=%s — ameesh merges this head into %s "
                        "once its required checks are green; a new push or a retarget "
                        "disarms it" % (target, METHOD, names, target), actor,
                        margin_s=AUTOMERGE_CALL_MARGIN_S):
        return {"state": "error", "why": "the elected pilot lease was lost before "
                                         "arming: nothing armed"}
    return {"state": "armed", "why": "ameesh merges %s into %s once %s is green"
            % (sha[:12], target, names)}


def _fire_hint(lot: int, sha: str) -> str:
    return (" — give the pilot's fire instead: ameesh work fire %d --sha %s"
            % (int(lot), sha[:12]))


# --------------------------------------------------------------------------
# passage : désarmer (tête ou cible changée) ou fusionner (vert)
# --------------------------------------------------------------------------

def merge_into(slug: str, target: str, sha: str, *, number: int, branch: str, title: str,
               gh) -> tuple:
    """R2 B2 (ameesh-cx2) : LE geste de fusion, lié à la cible épinglée.

    `gh pr merge --match-head-commit` ne lie que la tête : la PR reciblée
    juste avant l'appel fusionnerait dans une autre branche. L'API « merge a
    branch » (`POST repos/<slug>/merges`) nomme la branche écrite (`base`) et
    le commit fusionné (`head`, la tête publiée) : un reciblage de la PR ne
    redirige pas l'écriture, une poussée sur la branche du porteur ne change
    pas le commit fusionné. GitHub marque la PR fusionnée quand sa tête entre
    dans sa base. Comme la poussée de `merge_pr`, l'écriture ne passe pas par
    la porte de la PR : les checks requis sont prouvés par ameesh juste avant.

    Rend `(commit de fusion, None)`, `("", None)` quand la tête est déjà
    contenue dans la cible (204), ou `(None, erreur)`."""
    owner = slug.split("/", 1)[0]
    text = "Merge pull request #%d from %s/%s\n\n%s%sMerged by ameesh at the published " \
           "head %s into %s (packet auto-merge, #2813).\n" % (
               int(number), owner, branch, title, "\n\n" if title else "", sha, target)
    proc = gh(["api", "-X", "POST", "repos/%s/merges" % slug, "-f", "base=%s" % target,
               "-f", "head=%s" % sha, "-f", "commit_message=%s" % text], timeout=120.0)
    if proc.returncode != 0:
        return None, _error(proc)
    out = (proc.stdout or "").strip()
    if not out:
        return "", None
    try:
        data = json.loads(out)
    except ValueError:
        return None, "unreadable merge response: %s" % out[:200]
    commit = str((data or {}).get("sha") or "").strip().lower() if isinstance(data, dict) \
        else ""
    if not _SHA.fullmatch(commit):
        return None, "merge response without a commit: %s" % out[:200]
    return commit, None


def _verdicts(cfg, db, row: dict, milestones: list, *, slug: str, base: str,
              base_sha: str = "") -> tuple:
    """`fire.ready_verdicts` sur le porteur, avec le dépôt et le projet que le
    feu résout (dossier de l'assigné s'il porte ce dépôt, sinon le clone
    déclaré du projet). Rend `(ok, raison, projet)` ; le projet résolu sert
    ensuite à toute la porte de CI (R2, cx3). Une erreur refuse.

    `base_sha` (#3091) est la cible résolue lue sur la PR (`baseRefOid`) :
    elle prouve la base de composition au lieu de dépendre de la fraîcheur de
    la ref locale du checkout."""
    try:
        repo = fire._repo_of(db, row)
        if repo is not None and not fire._same_repository(repo, slug):
            repo = None
        project = fire._project(cfg, repo, slug)
        if repo is None:
            repo = fire._project_checkout(project)
        extra = {"base_sha": base_sha} if str(base_sha or "").strip() else {}
        ok, why, _klass = fire.ready_verdicts(cfg, db, row, milestones, repo=repo,
                                              project=project, target=base, **extra)
    except Exception as exc:  # noqa: BLE001 - une porte illisible ne fusionne pas
        return (False, "the review gate cannot be read: %s: %s" % (type(exc).__name__, exc),
                None)
    return ok, why, project


def _required_ci(view: dict, required, project) -> dict:
    """#3066 : la CI du paquet porte sur les exigences, pas le rollup entier.

    Garder les noms exacts imposés par la forge et le projet, les statuts
    requis par préfixe et la preuve du portier (#2876). `ready_pr` contrôle
    ensuite leur présence, leur succès et leur provenance sur la tête gelée.
    Un macOS facultatif en attente ou rouge ne retient pas la fusion.
    """
    from . import radar
    names = {name for name, _app in fire._required_pairs(required)}
    prefixes = radar.required_statuses(project, view.get("branch"))
    rollup = []
    for entry in view.get("rollup") or ():
        context = (entry.get("__typename") == "StatusContext"
                   or "state" in entry and "conclusion" not in entry)
        name = entry.get("context" if context else "name") or ""
        if (name in names or context and name.startswith(prefixes)
                or entry.get("workflowName") == "paquet"
                or name == "paquet (run proof unavailable)"):
            rollup.append(entry)
    return dict(view, rollup=rollup, ci="green" if radar.ci_green(rollup) else "pending")


def open_carriers(db, app: str) -> list[int]:
    """Les porteurs ouverts du projet `app`."""
    st = storage.of(db).work
    found = []
    for state in OPEN_STATES:
        found += [int(row["id"]) for row in st.items(state=state, assignee=None, limit=None)
                  if carrier(row.get("branch")) and (row.get("app") or "") == app]
    return sorted(set(found))


def check(cfg, db, lot: int, *, gh, dry_run: bool = False,
          actor: str = work.SYSTEM_SENDER) -> dict:
    """Un porteur armé : le désarme si sa tête ou sa cible a changé, le
    fusionne si ses checks requis sont verts sur la tête publiée.

    `result` : `idle` (rien d'armé), `pending` (checks pas encore verts),
    `closed` (PR fusionnée ou fermée ailleurs), `disarmed`/`would-disarm`,
    `merged`/`would-merge`, `busy` (une publication tient le bail : au passage
    suivant) ou `error`."""
    if not active(marks(db, lot)):
        return {"lot": lot, "result": "idle"}
    from . import pilot_gesture
    try:
        fence = (None if dry_run else
                 pilot_gesture.gate(cfg, db, "merge", "auto-merge check #%d" % lot))
    except pilot_gesture.GestureRefused as exc:
        return {"lot": lot, "result": "error", "error": str(exc)}
    handle = None if dry_run else _lock(db, lot)
    if handle is None and not dry_run:
        return {"lot": lot, "result": "busy"}
    try:
        return _check_locked(cfg, db, lot, gh=gh, dry_run=dry_run, actor=actor,
                             handle=handle, fence=fence)
    finally:
        if handle is not None:
            _unlock(handle)


def _check_locked(cfg, db, lot: int, *, gh, dry_run: bool, actor: str,
                  handle=None, fence=None) -> dict:
    # relu sous le bail : une publication a pu réarmer depuis la première lecture
    mine = active(marks(db, lot))
    if mine is None:
        return {"lot": lot, "result": "idle"}
    slug, number, sha, base = mine["repo"], mine["pr"], mine["sha"], mine["base"]
    out = {"lot": lot, "pr": number, "sha": sha, "base": base}
    try:
        view = pr_status(slug, number, gh=gh)
    except land.LandError as exc:
        return dict(out, result="error", error=str(exc))
    if view["state"] != "OPEN":
        return dict(out, result="closed", state=view["state"])
    row, milestones = fire._fresh(db, lot)
    if row is None:
        return dict(out, result="error", error="lot #%d not found" % lot)
    frozen = fire._norm((fire.freeze_of(milestones) or {}).get("sha"))
    moved = []
    if view["head"] != sha:
        moved.append("head=%s" % (view["head"] or "?"))
    if view["base"] != base:
        moved.append("now targets %s" % (view["base"] or "?"))
    if frozen != sha:
        moved.append("frozen=%s" % (frozen or "none"))
    if moved:
        out["moved"] = moved
        if dry_run:
            return dict(out, result="would-disarm")
        if not _renew(handle):
            return dict(out, result="busy")
        if not _fenced_hold(db, fence, margin_s=AUTOMERGE_CALL_MARGIN_S):
            return dict(out, result="error",
                        error="the elected pilot lease was lost before disarming: "
                              "nothing disarmed")
        if view["auto"]:
            error = land.pr_disable_auto(slug, number, gh=gh)
            if error is not None:
                return dict(out, result="error", error="disarm: %s" % error)
        if not _note_fenced(db, fence, lot, "disarmed", slug, number, sha,
                            "base=%s %s — the PR changed since its publication; `ameesh "
                            "ci run %d --full` re-arms on the new head"
                            % (base, " ".join(moved), int(lot)), actor):
            return dict(out, result="error",
                        error="the elected pilot lease was lost before journaling "
                              "the disarm")
        return dict(out, result="disarmed")
    if view["auto"] and not dry_run:
        # armée à la main depuis la publication : ameesh reste seul à fusionner
        if not _renew(handle):
            return dict(out, result="busy")
        if not _fenced_hold(db, fence, margin_s=AUTOMERGE_CALL_MARGIN_S):
            return dict(out, result="error",
                        error="the elected pilot lease was lost before disarming: "
                              "nothing disarmed")
        error = land.pr_disable_auto(slug, number, gh=gh)
        if error is not None:
            return dict(out, result="error", error="disarm: %s" % error)
    # R1 B1 (cx3) : la porte complète des verdicts, comme le feu (classe,
    # avis requis et indépendants sur la tête gelée, membres d'un porteur et
    # delta, #2766), avant toute lecture de CI et tout geste de fusion.
    # #3091 : la cible résolue de la PR prouve la base de composition du
    # paquet ; la ref locale du checkout de l'assigné peut être en retard.
    base_oid = str(view.get("base_oid") or "").strip().lower()
    extra = {"base_sha": base_oid} if base_oid else {}
    ok, why, project = _verdicts(cfg, db, row, milestones, slug=slug, base=base,
                                 **extra)
    if not ok:
        return dict(out, result="pending", why=why)
    # R1 B2 (cx3) : au geste, au moins un check imposé par la forge, et tous
    # ceux de la publication ; une protection allégée depuis ne fusionne rien.
    # Cette preuve se lit sans déclaration : une déclaration ne la remplace pas.
    try:
        forged = fire.required_checks(slug, base, None, gh=gh)
        # R2 (cx3) : la porte de CI garde le projet résolu — checks déclarés,
        # `required_statuses`, `ci_required` — en plus de la forge.
        required = (fire.required_checks(slug, base, project, gh=gh)
                    if project is not None else forged)
    except land.LandError as exc:
        return dict(out, result="pending",
                    why="the required checks of %s cannot be read: %s" % (base, exc))
    names = set(context for context, _app in forged)
    dropped = [name for name in mine["required"] if name not in names]
    if not forged or dropped:
        return dict(out, result="error", error=(
            "%s no longer enforces %s: nothing proves the CI the publication relied on, "
            "nothing merged" % (base, ", ".join(dropped) or "any required check")))
    report: dict = {}
    ok, why = fire.ready_pr(_required_ci(view, required, project),
                            target=base, project=project, frozen=sha,
                            required=required, slug=slug, gh=gh, report=report)
    if not ok:
        # #3186 (décision owner #128512) : un check requis jamais conclu est
        # une preuve absente ; la porte reste fermée et relance son run une
        # fois, sous le bail du passage et la garde du pilote élu.
        reruns = fire.relaunch_unconcluded(
            db, lot, slug, sha, report, gh=gh, dry_run=dry_run, actor=actor,
            guard=lambda: _renew(handle) and _fenced_hold(
                db, fence, margin_s=AUTOMERGE_CALL_MARGIN_S))
        if reruns:
            from . import radar
            return dict(out, result="pending", reruns=reruns,
                        why="%s — %s" % (why, radar.describe_reruns(reruns)))
        return dict(out, result="pending", why=why)
    if dry_run:
        return dict(out, result="would-merge")
    # La PR est relue juste avant le geste, après toutes les lectures de la
    # porte ; une tête, une cible ou un état changés depuis ne fusionnent pas
    # (le passage suivant désarme).
    try:
        fresh = pr_status(slug, number, gh=gh)
    except land.LandError as exc:
        return dict(out, result="error", error="fresh read: %s" % exc)
    if (fresh["state"], fresh["head"], fresh["base"]) != ("OPEN", sha, base):
        return dict(out, result="pending", why=(
            "PR #%d changed just before the merge (%s, head %s, base %s): nothing merged"
            % (number, fresh["state"] or "?", fresh["head"][:12] or "?",
               fresh["base"] or "?")))
    if not _renew(handle):
        return dict(out, result="busy")
    # L165 (EP4) : renouvellement du bail élu juste avant `POST /merges`, avec
    # la marge de l'appel ; sans rôle/identité, `fence` est None et le passage
    # d'exécuteur reste inchangé.
    if fence is not None:
        from . import pilot_gesture
        try:
            fence.hold(db, margin_s=AUTOMERGE_CALL_MARGIN_S)
        except pilot_gesture.GestureRefused as exc:
            return dict(out, result="error", error=str(exc))
    commit, error = merge_into(slug, base, sha, number=number, branch=fresh["branch"],
                               title=fresh.get("title") or "", gh=gh)
    if error is not None:
        return dict(out, result="error", error="merge: %s" % error)
    consigned = list(report.get("consigned") or ())
    note = ("base=%s commit=%s — merged by ameesh into %s at its published head, gates green"
            % (base, commit or "already-contained", base))
    if consigned:
        note += ("; required checks non concluants consignés (règle owner 23/10) : %s"
                 % ", ".join(consigned))
    _note(db, lot, "merged", slug, number, sha, note, actor)
    if consigned:
        _open_correction(db, row, slug=slug, number=number, sha=sha, base=base,
                         consigned=consigned, actor=actor)
    return dict(out, result="merged", commit=commit, consigned=consigned)


def _open_correction(db, row: dict, *, slug: str, number: int, sha: str, base: str,
                     consigned: list, actor: str):
    """#3085 : ouvre le lot de correction d'un check requis non concluant qui
    n'a pas retenu la fusion. Le lot est journalisé, sans assigné (file de
    l'équipe) ; son ouverture ne défait jamais la fusion déjà écrite.
    #3209 : un lot par check tant qu'il est ouvert (`radar.nonconclusive_source`) ;
    le même check au paquet suivant est noté sur ce lot."""
    from . import radar
    app = str(row.get("app") or "")
    out = []
    for text in consigned:
        try:
            out.append(work.add_or_note(
                db,
                title="CI non concluante sur %s@%s : corriger %s" % (slug, sha[:12], text),
                type="bug", priority=2, app=app,
                source=radar.nonconclusive_source(app or slug, text),
                occurrence=("de nouveau non concluant sur la tête %s du paquet %s#%d "
                            "(base %s) : %s" % (sha[:12], slug, int(number), base, text)),
                body=("Le check requis n'a pas conclu (ni vert ni négatif) sur la tête %s "
                      "du paquet %s#%d (base %s) : %s.\n\nRègle owner du 23/10 (canon home "
                      "§3) : un résultat non concluant ne bloque pas la fusion ; il se "
                      "consigne et ouvre ce lot de correction. Rejouer ou réparer le "
                      "check, sans relire le paquet déjà fusionné. Tant que ce lot est "
                      "ouvert, les occurrences suivantes y sont notées."
                      % (sha, slug, int(number), base, text)),
                actor=actor))
        except Exception:  # noqa: BLE001 - la fusion est écrite, le lot est un suivi
            continue
    return out


def watch(cfg, db, app: str, *, gh=None, dry_run: bool = False) -> list[dict]:
    """Tous les porteurs ouverts de `app` ; un porteur ne fait pas tomber l'autre."""
    gh = gh or land._gh
    results = []
    for lot in open_carriers(db, app):
        try:
            results.append(check(cfg, db, lot, gh=gh, dry_run=dry_run))
        except Exception as exc:  # noqa: BLE001 - un porteur ne fait pas tomber l'autre
            results.append({"lot": lot, "result": "error",
                            "error": "%s: %s" % (type(exc).__name__, exc)})
    return results
