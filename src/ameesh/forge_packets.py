# SPDX-License-Identifier: AGPL-3.0-only
"""#6088 — les unités d'un paquet ouvert SANS lot porteur (composition à la main).

Un paquet composé à la main (branche `integration/paquet*`, PR ouverte) n'a
pas toujours de lot porteur : `planned_waits.packet_carriers` ne le voit pas,
et l'unité qu'il transporte lève une alerte SLO « review GO → merge » jusqu'au
niveau 3 alors que sa fusion est déjà dans la CI du paquet (#6067, #6069,
06/11).

Conditions du pilote (mesh-design, 07/11 01:10Z, note du lot) :

1. l'appartenance se lit aux trailers `ameesh-work:` des commits de
   composition de la branche ouverte (le lecteur de `packet record` et de
   `planned_waits.packet_trailers`), jamais au manifeste ;
2. « en paquet » seulement tant que la PR est ouverte ET que sa tête contient
   la tête gelée de l'unité. Forge ou commit illisible : aucune conclusion,
   l'alerte part comme avant et nomme l'échec — jamais de silence ;
3. rien de propre à un projet en dur : le dépôt vient du lot
   (`work.lot_repository`).

Une unité ainsi portée n'a pas d'escalade propre, comme une unité d'un paquet
à porteur (#2919) : sa ligne `verdict_merge` est retirée, sans trace. #6104 :
aucun lot porteur ne surveille une telle PR, donc ce silence est borné. La
ligne reste, et nomme la PR, quand la CI de la PR est rouge, ou quand la PR est
ouverte depuis plus longtemps que la cible de l'étape (la cible SLO de la
ligne, aucune durée propre à ce module) : un paquet rouge, abandonné ou
immobile ne porte plus la fusion.

La liste des PR ouvertes est lue une fois par dépôt et par passe, et gardée
`TTL_S` entre deux passes (borne de coût de la passe d'alertes, comme
`gate_cache` #3761). Une passe à blanc de `db.batched` ne lit pas la forge.
"""
from __future__ import annotations

import json
import time

#: préfixe des branches de paquet (§11 bis/§12)
PACKET_PREFIX = "integration/paquet"
#: durée de vie de la liste des PR ouvertes d'un dépôt, entre deux passes
TTL_S = 300.0
#: au plus ce nombre de PR ouvertes lues par dépôt
MAX_PRS = 50

_OPEN: dict[str, tuple[float, list[dict] | None, str]] = {}


def _default_gh(args: list[str]):
    from . import land
    return land._gh(args, timeout=30.0)


def open_packets(slug: str, *, gh=None, now: float | None = None,
                 cache: dict | None = None) -> tuple[list[dict] | None, str]:
    """Les PR ouvertes `integration/paquet*` de `slug` : `(prs, "")`, ou
    `(None, raison)` quand la forge ne répond pas lisiblement."""
    now = time.time() if now is None else float(now)
    store = _OPEN if cache is None else cache
    hit = store.get(slug)
    if hit is not None and now - hit[0] < TTL_S:
        return hit[1], hit[2]
    prs: list[dict] | None
    reason = ""
    try:
        proc = (gh or _default_gh)(
            ["pr", "list", "--repo", slug, "--state", "open", "--limit", str(MAX_PRS),
             "--json", "number,headRefName,headRefOid,baseRefName,createdAt,statusCheckRollup"])
        if proc.returncode != 0:
            raise ValueError("gh pr list rc %s" % proc.returncode)
        rows = json.loads(proc.stdout or "[]")
        if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
            raise ValueError("gh pr list: unexpected output")
        prs = [{"number": int(r["number"]), "branch": str(r["headRefName"]),
                "head": str(r.get("headRefOid") or ""),
                "base": str(r.get("baseRefName") or ""),
                "created_ts": _iso_ts(r.get("createdAt")),
                "ci": ci_state(r.get("statusCheckRollup"))}
               for r in rows
               if str(r.get("headRefName") or "").startswith(PACKET_PREFIX)]
    except Exception as exc:  # noqa: BLE001 — forge illisible : aucune conclusion
        prs, reason = None, "forge unreadable (%s)" % str(exc)[:120]
    store[slug] = (now, prs, reason)
    return prs, reason


#: conclusions d'un check (CheckRun) ou états d'un statut (StatusContext) rouges ;
#: CANCELLED n'en est pas : un run annulé est d'ordinaire remplacé sur la même
#: tête, et un paquet laissé annulé tombe sous la borne de durée
_RED = frozenset({"FAILURE", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE",
                  "ERROR"})
#: un check pas encore conclu
_RUNNING = frozenset({"QUEUED", "IN_PROGRESS", "PENDING", "WAITING", "REQUESTED",
                      "EXPECTED"})


def ci_state(rollup) -> str:
    """#6104 : l'état des checks de la tête d'une PR : `red` dès qu'un check
    est rouge, sinon `running` tant qu'un check n'est pas conclu, sinon
    `green` ; `none` sans aucun check. Une forme illisible rend `none` : elle
    ne lève pas le silence à elle seule (la borne de durée reste)."""
    if not isinstance(rollup, list) or not rollup:
        return "none"
    running = False
    for check in rollup:
        if not isinstance(check, dict):
            continue
        conclusion = str(check.get("conclusion") or "").upper()
        state = str(check.get("state") or "").upper()
        status = str(check.get("status") or "").upper()
        if conclusion in _RED or state in _RED:
            return "red"
        if (status and status != "COMPLETED") or state in _RUNNING:
            running = True
    return "running" if running else "green"


def _iso_ts(value) -> float | None:
    """`2026-11-07T03:05:09Z` → epoch ; `None` si absent ou illisible."""
    import datetime as _dt
    try:
        return _dt.datetime.fromisoformat(
            str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def stale_reason(found: dict, target_s, now: float) -> str:
    """#6104 : pourquoi la PR ouverte trouvée par `vehicle` ne porte plus la
    fusion (« » si elle la porte) : CI rouge, ou PR ouverte depuis plus que la
    cible `target_s` de l'étape `verdict_merge`."""
    if found.get("ci") == "red":
        return "its CI is red"
    created = found.get("created_ts")
    if created is not None and target_s:
        age = float(now) - float(created)
        if age > float(target_s):
            return "open for %ds, beyond the step target (%ds)" % (int(age), int(target_s))
    return ""


def _frozen_head(milestones) -> str:
    heads = [m for m in milestones or () if m.get("kind") == "frozen" and m.get("sha")]
    if not heads:
        return ""
    last = max(heads, key=lambda m: (float(m.get("at_ts") or m.get("ts") or 0),
                                     int(m.get("id") or 0)))
    return str(last["sha"])


def vehicle(cfg, row: dict, milestones, *, db=None, gh=None, now: float | None = None,
            cache: dict | None = None) -> dict:
    """La PR de paquet ouverte qui porte ce lot.

    Rend `{"pr": n, "branch": b, "head": h, "created_ts": t, "ci": état}`
    quand une PR ouverte
    `integration/paquet*` cite le lot dans ses trailers de composition et
    que sa tête contient la tête gelée du lot ; `{"unreadable": raison}`
    quand on ne peut pas conclure ; `{}` quand aucune PR ne le porte."""
    from . import planned_waits, work
    from . import plan_git

    rid = int(row["id"])
    frozen = _frozen_head(milestones)
    if not frozen:
        return {}
    try:
        slug = work.lot_repository(row, cfg)
    except Exception:  # noqa: BLE001 — dépôt du lot inconnu : aucun véhicule lisible
        return {}
    prs, reason = open_packets(slug, gh=gh, now=now, cache=cache)
    if prs is None:
        return {"unreadable": reason}
    if not prs:
        return {}
    from . import fire, lot_class
    # #6825 : le chemin radar peut appartenir à l'ancien hôte du pilote.
    # Garder la résolution du canon, puis utiliser les mêmes sources locales
    # que la porte de revue (#6765), en vérifiant l'identité du dépôt.
    repo = fire._checkout(lot_class.repository(cfg, row))
    if repo and not fire._same_repository(repo, slug):
        repo = None
    if not repo:
        repo = fire.review_checkout(cfg, db, row, slug=slug, caller=True)
    unreadable = ""
    for pr in prs:
        if not repo:
            unreadable = "PR #%d: no local clone of %s" % (pr["number"], slug)
            continue
        if not pr["head"]:
            unreadable = "PR #%d: head absent from forge response" % pr["number"]
            continue
        members = planned_waits.packet_trailers(
            repo, {"branch": pr["branch"], "branch_head": pr["head"]},
            pr["base"] or None, head=pr["head"])
        if members is None:
            unreadable = "PR #%d: head %s not readable locally" % (
                pr["number"], pr["head"][:12])
            continue
        if rid not in members:
            continue
        ancestry = plan_git._git(repo, "merge-base", "--is-ancestor", frozen, pr["head"])
        if ancestry.returncode not in (0, 1):
            unreadable = "PR #%d: ancestry of frozen head %s not readable locally" % (
                pr["number"], frozen[:12])
            continue
        if ancestry.returncode != 0:
            continue  # cité, mais la tête du paquet ne contient pas ce gel
        return {"pr": pr["number"], "branch": pr["branch"], "head": pr["head"],
                "created_ts": pr.get("created_ts"), "ci": pr.get("ci") or "none"}
    return {"unreadable": unreadable} if unreadable else {}
