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
fusion est donc faite **par ameesh**, par `gh pr merge --merge
--match-head-commit <tête publiée>` : GitHub vérifie la tête au geste
effectif, et aucune auto-fusion GitHub n'est laissée armée.

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
  jeton administrateur passerait outre la protection, ameesh la relit.
  Sans check imposé (manaty/nexlink, privé sans GitHub Pro, protection en
  403), rien n'est armé : l'équivalent est le feu du pilote à la composition
  (`ameesh work fire <porteur> --sha <tête>`), que le message rappelle.
* **Le passage** (`watch`, `ameesh release tick` des projets de
  `packet_watch`, toutes les cinq minutes) relit chaque porteur ouvert armé :
  tête ou cible changée depuis la publication → désarmé (journal, et
  auto-fusion GitHub éventuelle retirée) ; checks requis verts sur la tête
  publiée → fusionné. La vitesse du passage ne fait que la latence de la
  fusion, jamais sa sûreté : une autre tête ou une autre cible ne fusionne
  jamais par ce chemin.

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
#: une marque du journal du lot ; un armement porte toujours sa cible
MARK_RE = re.compile(
    r"^auto-merge (?P<state>armed|disarmed|merged|not-armed) "
    r"repo=(?P<repo>[\w.-]+/[\w.-]+) pr=(?P<pr>\d+) sha=(?P<sha>[0-9a-f]{40})"
    r"(?: base=(?P<base>[\w./-]+))?(?:\s|$)")
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
                        "sha": m.group("sha"), "base": m.group("base") or ""})
    return sorted(out, key=lambda m: m["id"])


def active(rows) -> dict | None:
    """Le dernier armement qu'aucun désarmement ni fusion n'a suivi, ou None.

    Un armement sans cible ne compte pas (B2 : la cible est épinglée). Un
    `not-armed` ne l'efface pas : une republication refusée laisse l'armement
    précédent sous la garde du passage."""
    current = None
    for row in rows or ():
        if row["state"] == "armed":
            current = row if row["base"] else None
        elif row["state"] in CLOSING and current is not None \
                and current["pr"] == row["pr"] and current["repo"] == row["repo"]:
            current = None
    return current


def _note(db, lot: int, state: str, slug: str, number: int, sha: str, rest: str,
          actor: str) -> None:
    work.note(db, int(lot), "auto-merge %s repo=%s pr=%d sha=%s %s"
              % (state, slug, int(number), sha, rest.strip()), actor=actor)


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
    cible, brouillon, CI, rollup, branche) plus `auto` ; `land.LandError`
    sinon."""
    from . import radar
    proc = gh(["pr", "view", str(int(number)), "--repo", slug, "--json",
               "state,headRefOid,headRefName,baseRefName,isDraft,autoMergeRequest,"
               "statusCheckRollup"])
    if proc.returncode != 0:
        raise land.LandError("gh pr view %d: %s" % (int(number), _error(proc)))
    try:
        data = json.loads(proc.stdout or "")
    except ValueError as exc:
        raise land.LandError("gh pr view %d: unreadable output" % int(number)) from exc
    if not isinstance(data, dict):
        raise land.LandError("gh pr view %d: unexpected output" % int(number))
    rollup = radar.latest_rollup(data.get("statusCheckRollup"))
    pending = any((c.get("status") or "COMPLETED") != "COMPLETED"
                  or (c.get("state") or "") == "PENDING" for c in rollup)
    ci = "green" if radar.ci_green(rollup) else "pending" if pending or not rollup else "red"
    return {"number": int(number), "state": str(data.get("state") or "").upper(),
            "head": str(data.get("headRefOid") or "").strip().lower(),
            "base": str(data.get("baseRefName") or "").strip(),
            "branch": str(data.get("headRefName") or "").strip(),
            "draft": bool(data.get("isDraft")), "ci": ci, "rollup": rollup,
            "auto": data.get("autoMergeRequest") is not None}


# --------------------------------------------------------------------------
# publication
# --------------------------------------------------------------------------

def arm(db, lot: int, *, slug: str, number: int, sha: str, target: str, gh,
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
        out = _arm_locked(db, lot, slug=slug, number=number, sha=sha, target=target,
                          gh=gh, actor=actor, handle=handle)
        if out["state"] in ("armed", "already-armed"):
            # une composition déjà verte fusionne tout de suite, sous le même bail
            out["merge"] = _check_locked(db, lot, gh=gh, dry_run=False, actor=actor,
                                         handle=handle)
        return out
    finally:
        _unlock(handle)


def _arm_locked(db, lot: int, *, slug: str, number: int, sha: str, target: str, gh,
                actor: str, handle=None) -> dict:
    lost = {"state": "error", "why": "the auto-merge lease of lot #%d was lost: rerun "
                                     "`ameesh ci run %d --full`" % (lot, lot)}

    def refuse(state: str, why: str) -> dict:
        _note(db, lot, "not-armed", slug, number, sha, "— %s" % why, actor)
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
    names = ",".join(dict.fromkeys(context for context, _app in required))
    if not _renew(handle):
        return lost
    _note(db, lot, "armed", slug, number, sha,
          "base=%s method=%s required=%s — ameesh merges this head into %s once its "
          "required checks are green; a new push or a retarget disarms it"
          % (target, METHOD, names, target), actor)
    return {"state": "armed", "why": "ameesh merges %s into %s once %s is green"
            % (sha[:12], target, names)}


def _fire_hint(lot: int, sha: str) -> str:
    return (" — give the pilot's fire instead: ameesh work fire %d --sha %s"
            % (int(lot), sha[:12]))


# --------------------------------------------------------------------------
# passage : désarmer (tête ou cible changée) ou fusionner (vert)
# --------------------------------------------------------------------------

def open_carriers(db, app: str) -> list[int]:
    """Les porteurs ouverts du projet `app`."""
    st = storage.of(db).work
    found = []
    for state in OPEN_STATES:
        found += [int(row["id"]) for row in st.items(state=state, assignee=None, limit=None)
                  if carrier(row.get("branch")) and (row.get("app") or "") == app]
    return sorted(set(found))


def check(db, lot: int, *, gh, dry_run: bool = False,
          actor: str = work.SYSTEM_SENDER) -> dict:
    """Un porteur armé : le désarme si sa tête ou sa cible a changé, le
    fusionne si ses checks requis sont verts sur la tête publiée.

    `result` : `idle` (rien d'armé), `pending` (checks pas encore verts),
    `closed` (PR fusionnée ou fermée ailleurs), `disarmed`/`would-disarm`,
    `merged`/`would-merge`, `busy` (une publication tient le bail : au passage
    suivant) ou `error`."""
    if not active(marks(db, lot)):
        return {"lot": lot, "result": "idle"}
    handle = None if dry_run else _lock(db, lot)
    if handle is None and not dry_run:
        return {"lot": lot, "result": "busy"}
    try:
        return _check_locked(db, lot, gh=gh, dry_run=dry_run, actor=actor, handle=handle)
    finally:
        if handle is not None:
            _unlock(handle)


def _check_locked(db, lot: int, *, gh, dry_run: bool, actor: str, handle=None) -> dict:
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
    moved = []
    if view["head"] != sha:
        moved.append("head=%s" % (view["head"] or "?"))
    if view["base"] != base:
        moved.append("now targets %s" % (view["base"] or "?"))
    if moved:
        out["moved"] = moved
        if dry_run:
            return dict(out, result="would-disarm")
        if not _renew(handle):
            return dict(out, result="busy")
        if view["auto"]:
            error = land.pr_disable_auto(slug, number, gh=gh)
            if error is not None:
                return dict(out, result="error", error="disarm: %s" % error)
        _note(db, lot, "disarmed", slug, number, sha,
              "base=%s %s — the PR changed since its publication; `ameesh ci run %d "
              "--full` re-arms on the new head" % (base, " ".join(moved), int(lot)), actor)
        return dict(out, result="disarmed")
    if view["auto"] and not dry_run:
        # armée à la main depuis la publication : ameesh reste seul à fusionner
        if not _renew(handle):
            return dict(out, result="busy")
        error = land.pr_disable_auto(slug, number, gh=gh)
        if error is not None:
            return dict(out, result="error", error="disarm: %s" % error)
    ok, why = fire.ready_pr(view, target=base, project=None, frozen=sha, slug=slug, gh=gh)
    if not ok:
        return dict(out, result="pending", why=why)
    if dry_run:
        return dict(out, result="would-merge")
    if not _renew(handle):
        return dict(out, result="busy")
    # le seul geste de fusion : GitHub vérifie la tête à cet instant précis
    error = land.pr_merge(slug, number, sha, METHOD, gh=gh)
    if error is not None:
        return dict(out, result="error", error="merge: %s" % error)
    _note(db, lot, "merged", slug, number, sha,
          "base=%s — merged by ameesh at its published head, required checks green" % base,
          actor)
    return dict(out, result="merged")


def watch(db, app: str, *, gh=None, dry_run: bool = False) -> list[dict]:
    """Tous les porteurs ouverts de `app` ; un porteur ne fait pas tomber l'autre."""
    gh = gh or land._gh
    results = []
    for lot in open_carriers(db, app):
        try:
            results.append(check(db, lot, gh=gh, dry_run=dry_run))
        except Exception as exc:  # noqa: BLE001 - un porteur ne fait pas tomber l'autre
            results.append({"lot": lot, "result": "error",
                            "error": "%s: %s" % (type(exc).__name__, exc)})
    return results
