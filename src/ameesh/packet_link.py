# SPDX-License-Identifier: AGPL-3.0-only
"""#2443 — persister le paquet d'une unité, sans lire le manifeste.

Un paquet (§11 bis/§12) est une branche `integration/**` portée par un lot
ordinaire (le « porteur »). Sa composition est une suite de commits de fusion
de première parente, chacun portant un trailer `ameesh-work: <lot>`. Jusqu'ici
`work show` et `sync-merges` ne connaissaient cette appartenance que par un
manifeste markdown (`reviews/**`) ou par le corps du porteur : le premier ne
la retrouvait qu'en le relisant, le second ignorait une unité dont le gel
d'origine verdict n'était pas un jalon `frozen` (#2366).

    ameesh work packet record <porteur> [--repo D] [--base SHA] [--head REF]
    ameesh work packet show <lot|porteur> [--json]

`record` relit les commits de composition du dépôt (trailers, jamais le
manifeste), capture le gel effectif de chaque unité au même instant, et écrit
la liaison dans `work_packet_members` (migration 0465). `work show` et
`sync-merges` lisent ensuite cette table : le premier affiche le paquet, le
second ferme une unité quand le commit de composition est entré dans la cible
(`plan_git.probe`, revert #1825 compris) et que le gel courant de l'unité n'a
pas bougé depuis la composition.

`record` ne touche pas au dépôt : aucune référence, aucun commit. Il refuse
avant d'écrire si le dépôt n'est pas celui du porteur, si l'intervalle
`base..head` est incohérent, si un trailer nomme un lot inconnu, ou si la
branche du porteur n'est pas `integration/**`.
"""
from __future__ import annotations

import re

from . import plan_git, storage, work
from .db import DbError


class PacketLinkError(RuntimeError):
    """Composition illisible ou incohérente : rien n'est écrit."""


#: le trailer d'une unité (même forme que `scripts/paquet-compose.py`, #2157)
TRAILER = re.compile(r"(?im)^[ \t>*-]*ameesh-work[ \t]*:[ \t]*#?(\d+)[ \t]*$")
#: une branche de paquet
PACKET_PREFIX = "integration/"


def _commit(repo: str, ref: str) -> str | None:
    return plan_git._commit(repo, ref)


def _text(value) -> str:
    return str(value or "").strip()


def compositions(repo: str, base: str, head: str) -> tuple[list[dict], str, str]:
    """Les unités de composition entre `base` et `head`.

    Lit la première parente de `base..head` : chaque commit de fusion portant
    un/des trailer(s) `ameesh-work` est une unité ; sa tête composée est le
    second parent. Un commit de fusion sans trailer (mise à jour depuis la
    cible) n'est pas une unité. Un lot recomposé dans le même paquet garde sa
    place et prend le commit de composition le plus récent (le dernier dans
    l'ordre de composition).

    Rend `(units, base_sha, head_sha)` ; `units` : `work_item_id`, `unit_sha`,
    `source_sha`. Aucune unité : `PacketLinkError`.
    """
    base_sha, head_sha = _commit(repo, base), _commit(repo, head)
    if base_sha is None:
        raise PacketLinkError("base %r not found in %s" % (base, repo))
    if head_sha is None:
        raise PacketLinkError("head %r not found in %s" % (head, repo))
    if plan_git._git(repo, "merge-base", "--is-ancestor", base_sha,
                     head_sha).returncode != 0:
        raise PacketLinkError("base %s is not an ancestor of head %s"
                              % (base_sha[:12], head_sha[:12]))
    log = plan_git._out(
        repo, "log", "--first-parent", "--merges", "--reverse",
        "--format=%H%x1f%P%x1f%B%x1e", "%s..%s" % (base_sha, head_sha)) or ""
    units: dict[int, dict] = {}
    for record in log.split("\x1e"):
        record = record.strip()
        if not record:
            continue
        commit, _sep, rest = record.partition("\x1f")
        parents, _sep, body = rest.partition("\x1f")
        parents = parents.split()
        if not commit or len(parents) < 2:
            continue
        source = parents[1]
        for match in TRAILER.finditer(body):
            lot = int(match.group(1))
            units[lot] = {"work_item_id": lot, "unit_sha": commit, "source_sha": source}
    if not units:
        raise PacketLinkError(
            "no composition unit between %s and %s: no first-parent merge commit "
            "carries an `ameesh-work:` trailer" % (base_sha[:12], head_sha[:12]))
    return list(units.values()), base_sha, head_sha


def _target(repo: str, package: dict, explicit: str | None) -> str:
    """La cible de lecture : `--target`, sinon celle du porteur, sinon celle du
    dépôt ; publiée (`origin/…`) quand elle existe, comme les relevés."""
    target = _text(explicit) or _text(package.get("branch_target"))
    if not target:
        target = plan_git.default_target(repo) or ""
    if not target:
        raise PacketLinkError("no target: set --target, the carrier's target, "
                              "or `git config %s` in %s" % (plan_git.TARGET_GIT_KEY, repo))
    if _commit(repo, target) is None:
        raise PacketLinkError("target %r not found in %s (no fetch is done)"
                              % (target, repo))
    return plan_git.published_target(repo, target)


def _effective_freeze(db, lot: int, repo: str, target: str) -> str:
    """Le gel effectif de l'unité (déclaré ou d'origine verdict), résolu en
    commit complet quand le dépôt le connaît ; vide sinon (#1756, #2366)."""
    try:
        candidate = plan_git.freeze_candidate(
            work.milestones(db, lot), repo=repo, target=target)
    except Exception as exc:  # noqa: BLE001 - un gel illisible n'est pas une preuve
        raise PacketLinkError("lot #%d: effective freeze unreadable: %s" % (lot, exc)) from exc
    sha = plan_git.commit_id(candidate.get("sha"))
    if not sha:
        return ""
    return _commit(repo, sha) or sha


def record(db, package_id: int, *, repo: str | None = None, base: str | None = None,
           head: str = "HEAD", branch: str | None = None, target: str | None = None,
           actor: str = "", dry_run: bool = False, cfg=None) -> dict:
    """Enregistre la composition du paquet `package_id` ; rend le compte rendu.

    Le dépôt est vérifié comme celui du porteur (`work.lot_repository` /
    `work._checkout`, qui lit l'`origin`) : une composition d'un autre dépôt ne
    peut pas être attachée au porteur. `base` : par défaut la base commune
    entre `head` et la cible. Aucune écriture si `dry_run`.
    """
    package = work.get(db, package_id)
    if package is None:
        raise PacketLinkError("packet carrier #%s not found" % package_id)
    package_branch = _text(branch) or _text(package.get("branch"))
    if not package_branch.startswith(PACKET_PREFIX):
        raise PacketLinkError(
            "packet carrier #%s has branch %r: a package is a lot on an "
            "`integration/**` branch" % (package_id, package_branch or ""))
    declared_branch = _text(package.get("branch"))
    if declared_branch and _text(branch) and _text(branch) != declared_branch:
        raise PacketLinkError(
            "packet carrier #%s is on %s, not %s: correct the carrier's branch "
            "first (`ameesh work branch %s %s`)"
            % (package_id, declared_branch, _text(branch), package_id, declared_branch))
    try:
        slug = work.lot_repository(package, cfg)
    except work.WorkError as exc:
        raise PacketLinkError(str(exc)) from exc
    try:
        top = work._checkout(slug, repo, cfg)
    except work.WorkError as exc:
        raise PacketLinkError(str(exc)) from exc
    target_ref = _target(top, package, target)
    head_sha = _commit(top, head)
    if head_sha is None:
        raise PacketLinkError("head %r not found in %s" % (head, top))
    # La tête enregistrée doit appartenir à la branche du porteur : lier la
    # composition d'une autre branche au mauvais paquet ne se rattrape pas.
    # La ref LOCALE d'abord (une ref distante peut être périmée après un
    # rebasage ou une publication partielle) ; une tête en avance sur la ref
    # est admise (le composeur peut enregistrer avant de pousser), une tête en
    # retard ne l'est pas.
    tip = _commit(top, "refs/heads/" + package_branch)
    if tip is None:
        tip = _commit(top, "refs/remotes/origin/" + package_branch)
    if tip is not None and plan_git._git(
            top, "merge-base", "--is-ancestor", tip, head_sha).returncode != 0:
        raise PacketLinkError(
            "head %s is not on the carrier's branch %s (tip %s): run the "
            "record from the package worktree, or pass --head/--branch"
            % (head_sha[:12], package_branch, tip[:12]))
    if base is None:
        # Une recomposition garde la base de la composition précédente : si la
        # cible a été fusionnée dans le paquet entre-temps, `merge-base` aurait
        # avancé et les unités déjà composées sortiraient de l'intervalle.
        # Une base enregistrée n'est reprise que si elle est encore ancêtre de
        # `head` (sinon le paquet a été rebasé : la base commune fait foi).
        try:
            previous_members = memberships(db, package_ids=[int(package_id)])
        except DbError as exc:
            raise PacketLinkError(
                "recorded packet compositions unreadable (migration 0465 applied?): %s"
                % exc) from exc
        for member in previous_members:
            previous = _text(member.get("base_sha"))
            if previous and _commit(top, previous) \
                    and plan_git._git(top, "merge-base", "--is-ancestor", previous,
                                      head_sha).returncode == 0:
                base = previous
                break
    if base is None:
        base = plan_git._out(top, "merge-base", head_sha, target_ref) or ""
        if not base:
            raise PacketLinkError("no common base between head %s and target %s"
                                  % (head_sha[:12], target_ref))
    units, base_sha, head_sha = compositions(top, base, head_sha)
    known = []
    for unit in units:
        lot = int(unit["work_item_id"])
        if work.get(db, lot) is None:
            raise PacketLinkError(
                "composition trailer names lot #%d, which does not exist: nothing "
                "written" % lot)
        unit["frozen_sha"] = _effective_freeze(db, lot, top, target_ref)
        known.append(unit)
    report = {"package": int(package_id), "branch": package_branch,
              "base": base_sha, "head": head_sha, "target": target_ref,
              "units": known, "dry_run": bool(dry_run), "recorded": False}
    if dry_run:
        return report
    note = ("packet composition recorded: %d unit(s) %s..%s (branch %s)"
            % (len(known), base_sha[:12], head_sha[:12], package_branch))
    try:
        report["members"] = storage.of(db).work.record_packet(
            int(package_id), branch=package_branch, base_sha=base_sha, head_sha=head_sha,
            units=known, note=note, actor=actor)
    except DbError as exc:
        raise PacketLinkError("packet composition not recorded: %s" % exc) from exc
    report["recorded"] = True
    return report


def memberships(db, *, item_ids=None, package_ids=None) -> list[dict]:
    """Les liaisons lues en base (voir `WorkItems.packet_members`, #2443)."""
    return storage.of(db).work.packet_members(item_ids=item_ids,
                                              package_ids=package_ids)


def for_item(db, item_id: int) -> list[dict]:
    """Les paquets enregistrés pour une unité, les plus récents d'abord."""
    return memberships(db, item_ids=[int(item_id)])


def describe(member: dict) -> str:
    """Une ligne lisible d'appartenance, sans manifeste."""
    return "#%s %s (unit %s, base %s)" % (
        member.get("package_id"), member.get("package_branch") or "?",
        (member.get("unit_sha") or "")[:12], (member.get("base_sha") or "")[:12])
