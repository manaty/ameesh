# SPDX-License-Identifier: AGPL-3.0-only
"""#2813 : l'auto-fusion GitHub d'un paquet, armée à sa publication (canon §12).

Décision owner du 20/10 (« yes » au bureau, fil du lot #2813) : aucun geste
ameesh n'armait l'auto-fusion des paquets ; h-1700, h-1900 et h-2000 sont
restés verts et ouverts jusqu'à la fusion manuelle du bureau. Le geste qui
publie le paquet arme désormais `gh pr merge --auto --merge
--match-head-commit <tête>` ; tout nouveau push désarme, et la publication
suivante réarme sur la nouvelle tête.

* **Publier** un porteur `integration/**`, c'est demander sa suite complète :
  `ameesh ci run <porteur> --full` (étiquette `ci-full-<tête>`). Juste après la
  pose (ou quand la composition est déjà demandée ou verte), `arm` arme
  l'auto-fusion sur la tête gelée. Une auto-fusion déjà armée par ameesh sur
  cette même tête est laissée telle quelle ; toute autre (autre tête, armée à
  la main) est désarmée puis réarmée : GitHub ne vérifie
  `--match-head-commit` qu'à l'armement (#1656), une auto-fusion ne doit donc
  jamais survivre à la tête qu'elle a vue.
* **Seule la forge fait attendre la CI** : l'armement exige un check requis
  imposé par GitHub sur la cible (protection classique ou ruleset,
  `fire.required_checks` sans déclaration de projet). Sans check imposé —
  manaty/nexlink, privé sans GitHub Pro, protection illisible (403) —, une
  auto-fusion fusionnerait sans attendre la CI ou serait refusée : rien n'est
  armé, et l'équivalent est le feu du pilote à la composition (`ameesh work
  fire <porteur> --sha <tête>`, file de la cible), que le message rappelle.
* **Tout nouveau push désarme** : `watch` (passage `ameesh release tick` des
  projets de `packet_watch`, toutes les cinq minutes) relit chaque porteur
  ouvert armé par ameesh ; si la tête de sa PR n'est plus celle de
  l'armement, l'auto-fusion est désarmée. Une tête poussée n'a aucun check
  `tests` tant que sa suite complète n'est pas demandée, et cette suite dure
  bien plus que le passage : le désarmement arrive avant tout vert.

La trace est le journal du lot (aucun état local) : `auto-merge armed …`,
`auto-merge disarmed …` et `auto-merge not-armed …`, avec dépôt, PR et têtes.
L'armement et le désarmement d'un même porteur se sérialisent sous le bail
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
#: durée du bail `auto-merge:<lot>` qui sérialise l'armement et le désarmement
LOCK_TTL_S = 120.0
#: une marque du journal du lot
MARK_RE = re.compile(
    r"^auto-merge (?P<state>armed|disarmed|not-armed) repo=(?P<repo>[\w.-]+/[\w.-]+) "
    r"pr=(?P<pr>\d+) sha=(?P<sha>[0-9a-f]{40})\b")
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
    """Les marques d'auto-fusion du lot, de la plus ancienne à la plus récente."""
    out = []
    for ev in work.events(db, int(lot), limit=limit) or ():
        m = MARK_RE.match((ev.get("note") or "").strip())
        if m:
            out.append({"id": int(ev.get("id") or 0), "state": m.group("state"),
                        "repo": m.group("repo"), "pr": int(m.group("pr")),
                        "sha": m.group("sha")})
    return sorted(out, key=lambda m: m["id"])


def active(rows) -> dict | None:
    """Le dernier armement d'ameesh qu'aucun désarmement n'a suivi, ou None.

    Un `not-armed` ne l'efface pas : un réarmement refusé après un
    désarmement impossible laisse l'ancien armement à surveiller."""
    current = None
    for row in rows or ():
        if row["state"] == "armed":
            current = row
        elif row["state"] == "disarmed" and current is not None \
                and current["pr"] == row["pr"] and current["repo"] == row["repo"]:
            current = None
    return current


def _lock(db, lot: int):
    """Le bail `auto-merge:<lot>`, ou None si un autre le tient.

    `arm` (publication) et `check` (passage) le prennent tous deux : sans lui,
    un passage qui a lu l'ancien armement désarmerait l'armement que la
    publication vient de faire sur la nouvelle tête."""
    ops = storage.of(db).work
    owner = "auto-merge:%s:%d" % (socket.gethostname(), os.getpid())
    key = "auto-merge:%d" % int(lot)
    epoch = ops.land_lock(key, owner, LOCK_TTL_S)
    return (ops, owner, epoch, key) if epoch is not None else None


def _unlock(handle) -> None:
    ops, owner, epoch, key = handle
    try:
        ops.land_unlock(key, owner, epoch)
    except Exception:  # noqa: BLE001 - le bail expire seul
        pass


def _note(db, lot: int, state: str, slug: str, number: int, sha: str, rest: str,
          actor: str) -> None:
    work.note(db, int(lot), "auto-merge %s repo=%s pr=%d sha=%s %s"
              % (state, slug, int(number), sha, rest.strip()), actor=actor)


# --------------------------------------------------------------------------
# GitHub
# --------------------------------------------------------------------------

def _error(proc) -> str:
    return ((proc.stderr or proc.stdout or "").strip()[:300]
            or "exit %d" % proc.returncode)


def pr_view(slug: str, number: int, *, gh) -> dict:
    """État, tête, base et auto-fusion de la PR ; `land.LandError` sinon."""
    proc = gh(["pr", "view", str(int(number)), "--repo", slug, "--json",
               "state,headRefOid,baseRefName,autoMergeRequest"])
    if proc.returncode != 0:
        raise land.LandError("gh pr view %d: %s" % (int(number), _error(proc)))
    try:
        data = json.loads(proc.stdout or "")
    except ValueError as exc:
        raise land.LandError("gh pr view %d: unreadable output" % int(number)) from exc
    if not isinstance(data, dict):
        raise land.LandError("gh pr view %d: unexpected output" % int(number))
    return {"state": str(data.get("state") or "").upper(),
            "head": str(data.get("headRefOid") or "").strip().lower(),
            "base": str(data.get("baseRefName") or "").strip(),
            "auto": data.get("autoMergeRequest") is not None}


# --------------------------------------------------------------------------
# armement (publication)
# --------------------------------------------------------------------------

def arm(db, lot: int, *, slug: str, number: int, sha: str, target: str, gh,
        actor: str = work.SYSTEM_SENDER) -> dict:
    """Arme l'auto-fusion du porteur `lot` (PR `number` de `slug`) sur `sha`.

    Rend `{state, why}` ; `state` vaut `armed`, `rearmed` (une auto-fusion
    d'une autre tête a été désarmée d'abord), `already-armed`, `unprotected`
    (aucun check imposé par la forge : rien d'armé, le feu est le geste),
    `moved` (la PR n'est plus sur `sha`) ou `error`. Hors `already-armed`,
    chaque issue est journalisée sur le lot ; aucune n'est fatale à la
    demande de CI qui la précède."""
    sha = str(sha or "").strip().lower()
    number = int(number)
    if not _SHA.fullmatch(sha):
        return {"state": "error", "why": "the frozen head %r is not a full commit id" % sha}
    handle = _lock(db, lot)
    if handle is None:
        return {"state": "error", "why": "another auto-merge operation holds lot #%d: "
                                         "rerun `ameesh ci run %d --full`" % (lot, lot)}
    try:
        return _arm_locked(db, lot, slug=slug, number=number, sha=sha, target=target,
                           gh=gh, actor=actor)
    finally:
        _unlock(handle)


def _arm_locked(db, lot: int, *, slug: str, number: int, sha: str, target: str, gh,
                actor: str) -> dict:
    def refuse(state: str, why: str) -> dict:
        _note(db, lot, "not-armed", slug, number, sha, "— %s" % why, actor)
        return {"state": state, "why": why}

    try:
        required = fire.required_checks(slug, target, None, gh=gh)
    except land.LandError as exc:
        return refuse("unprotected" if isinstance(exc, land.ForbiddenLandError) else "error",
                      "the required checks of %s cannot be read (%s): an auto-merge would "
                      "not be bound to the CI, nothing armed%s"
                      % (target, exc, _fire_hint(lot, sha)
                         if isinstance(exc, land.ForbiddenLandError) else ""))
    if not required:
        return refuse("unprotected",
                      "%s has no required check enforced by GitHub: an auto-merge would "
                      "merge without waiting for the CI, nothing armed%s"
                      % (target, _fire_hint(lot, sha)))
    try:
        view = pr_view(slug, number, gh=gh)
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
    mine = active(marks(db, lot))
    if view["auto"] and mine and mine["sha"] == sha and mine["pr"] == number \
            and mine["repo"] == slug:
        return {"state": "already-armed",
                "why": "auto-merge already armed by ameesh on %s" % sha[:12]}
    state = "armed"
    if view["auto"]:
        # GitHub ne relit pas `--match-head-commit` après l'armement (#1656) :
        # une auto-fusion d'une autre tête (ou armée à la main) ne survit pas.
        error = land.pr_disable_auto(slug, number, gh=gh)
        if error is not None:
            return refuse("error", "the auto-merge already armed on PR #%d could not be "
                                   "disarmed (%s)" % (number, error))
        state = "rearmed"
    proc = gh(["pr", "merge", str(number), "--repo", slug, "--auto", "--%s" % METHOD,
               "--match-head-commit", sha], timeout=120.0)
    if proc.returncode != 0:
        return refuse("error", "gh pr merge --auto: %s" % _error(proc))
    names = ",".join(dict.fromkeys(context for context, _app in required))
    _note(db, lot, "armed", slug, number, sha,
          "method=%s required=%s — merges itself once green; a new push disarms it"
          % (METHOD, names), actor)
    return {"state": state, "why": "auto-merge armed on %s (required: %s)"
            % (sha[:12], names)}


def _fire_hint(lot: int, sha: str) -> str:
    return (" — give the pilot's fire instead: ameesh work fire %d --sha %s"
            % (int(lot), sha[:12]))


# --------------------------------------------------------------------------
# désarmement (nouveau push)
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
    """Un porteur : désarme une auto-fusion d'ameesh dont la tête a bougé.

    `result` : `idle` (rien d'armé par ameesh), `armed` (même tête), `unarmed`
    (plus d'auto-fusion sur la PR), `closed` (PR fusionnée ou fermée),
    `disarmed`/`would-disarm`, `busy` (une publication tient le bail : au
    passage suivant) ou `error`."""
    if not active(marks(db, lot)):
        return {"lot": lot, "result": "idle"}
    handle = None if dry_run else _lock(db, lot)
    if handle is None and not dry_run:
        return {"lot": lot, "result": "busy"}
    try:
        return _check_locked(db, lot, gh=gh, dry_run=dry_run, actor=actor)
    finally:
        if handle is not None:
            _unlock(handle)


def _check_locked(db, lot: int, *, gh, dry_run: bool, actor: str) -> dict:
    # relu sous le bail : une publication a pu réarmer depuis la première lecture
    mine = active(marks(db, lot))
    if mine is None:
        return {"lot": lot, "result": "idle"}
    out = {"lot": lot, "pr": mine["pr"], "sha": mine["sha"]}
    try:
        view = pr_view(mine["repo"], mine["pr"], gh=gh)
    except land.LandError as exc:
        return dict(out, result="error", error=str(exc))
    if view["state"] != "OPEN":
        return dict(out, result="closed", state=view["state"])
    if not view["auto"]:
        return dict(out, result="unarmed")
    if view["head"] == mine["sha"]:
        return dict(out, result="armed")
    out["head"] = view["head"]
    if dry_run:
        return dict(out, result="would-disarm")
    error = land.pr_disable_auto(mine["repo"], mine["pr"], gh=gh)
    if error is not None:
        return dict(out, result="error", error="disarm: %s" % error)
    _note(db, lot, "disarmed", mine["repo"], mine["pr"], mine["sha"],
          "head=%s — the head moved since the arming; `ameesh ci run %d --full` re-arms "
          "on the new head" % (view["head"] or "?", int(lot)), actor)
    return dict(out, result="disarmed")


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
