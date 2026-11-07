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
   (`work.lot_repository`), et l'alerte tue nomme sa source (la PR).

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
             "--json", "number,headRefName,headRefOid,baseRefName"])
        if proc.returncode != 0:
            raise ValueError("gh pr list rc %s" % proc.returncode)
        rows = json.loads(proc.stdout or "[]")
        if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
            raise ValueError("gh pr list: unexpected output")
        prs = [{"number": int(r["number"]), "branch": str(r["headRefName"]),
                "head": str(r.get("headRefOid") or ""),
                "base": str(r.get("baseRefName") or "")}
               for r in rows
               if str(r.get("headRefName") or "").startswith(PACKET_PREFIX)]
    except Exception as exc:  # noqa: BLE001 — forge illisible : aucune conclusion
        prs, reason = None, "forge unreadable (%s)" % str(exc)[:120]
    store[slug] = (now, prs, reason)
    return prs, reason


def _frozen_head(milestones) -> str:
    heads = [m for m in milestones or () if m.get("kind") == "frozen" and m.get("sha")]
    if not heads:
        return ""
    last = max(heads, key=lambda m: (float(m.get("at_ts") or m.get("ts") or 0),
                                     int(m.get("id") or 0)))
    return str(last["sha"])


def vehicle(cfg, row: dict, milestones, *, gh=None, now: float | None = None,
            cache: dict | None = None) -> dict:
    """La PR de paquet ouverte qui porte ce lot.

    Rend `{"pr": n, "branch": b, "head": h}` quand une PR ouverte
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
    from . import lot_class
    repo = lot_class.repository(cfg, row)
    unreadable = ""
    for pr in prs:
        if not repo or not pr["head"]:
            unreadable = "PR #%d: no local clone of %s" % (pr["number"], slug)
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
        if plan_git._git(repo, "merge-base", "--is-ancestor",
                         frozen, pr["head"]).returncode != 0:
            continue  # cité, mais la tête du paquet ne contient pas ce gel
        return {"pr": pr["number"], "branch": pr["branch"], "head": pr["head"]}
    return {"unreadable": unreadable} if unreadable else {}
