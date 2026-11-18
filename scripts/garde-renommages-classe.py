#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""#7777 — un fichier déplacé garde la classe de son ancien chemin.

Le canon §8 range des chemins du cœur en classe sensible (porte de fusion,
SQL, droits, argent, release…). Quand un lot **déplace** un fichier vers
``src/ameesh/modules/`` sans que le canon nomme le nouveau chemin, le fichier
tombe au défaut de la politique — souvent ``light`` — et la protection de
l'ancien chemin ne suit pas le déplacement. #7663 l'a montré : le moteur de
release Nexlink déplacé dans le module Nexlink est passé ``light`` alors que
``src/ameesh/nexlink_release.py`` restait ``sensitive`` ; le canon a dû être
complété à la main (``modules/nexlink/**``). #7650 a fait de même pour le
stockage du module dev.

État au 18/11 des déplacements #7637 déjà fusionnés : ``nexlink_release.py``
→ ``modules/nexlink/release.py`` (#7663, le cas d'origine) et
``nexlink_alerts.py`` → ``modules/nexlink/alerts.py`` (#7793), tous deux
``sensitive`` par la règle ``src/ameesh/modules/nexlink/**`` du canon
(home 462c534) ; le second est passé de ``light`` à ``sensitive``.

Le gel reçoit du garde (``ameesh.freeze_checks``) les renommages du diff gelé
(``git diff -M`` entre le merge-base de la cible et la tête, JSON dans
[`RENAMES_ENV`]) et la politique du canon configuré, comme
``ameesh review-class``. Pour chaque couple ``(ancien, nouveau)`` :

- si la classe du nouveau chemin est **plus faible** que celle de l'ancien,
  le gel est refusé avec le chemin à ajouter au canon et la classe à
  conserver — le compagnon canon fusionne avant le lot ;
- si la classe du nouveau chemin est au moins égale (règle du canon ou défaut
  plus strict), il passe.

Un renommage n'est pas deviné : seuls les couples fournis par le diff gelé
sont lus. Une variable absente (lancement manuel de ``scripts/freeze-checks.py``,
composition d'un paquet dont les unités ont déjà été gelées) laisse le
contrôle inactif, avec une ligne explicite ; une variable **illisible** refuse
(doute, jamais un vert). Le gel, lui, transmet toujours la liste — vide
comprise — et refuse si le diff ne se lit pas.

La politique vient du canon configuré : tous les canons configurés, la classe
la plus haute l'emporte, un projet qu'aucun canon ne déclare ou une politique
illisible refuse. ``--canon DIR --ref REV`` rejoue un canon git épinglé, pour
les témoins et le banc, sans configuration d'hôte.

Usage : scripts/garde-renommages-classe.py [--project ameesh]
        [--canon DIR --ref REV] [--renames JSON]
Sortie : 0 aucune perte de classe (ou contrôle inactif), 1 au moins un
déplacement affaiblit la classe, 2 lecture impossible.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass

#: la variable où le garde du gel pose les renommages du diff, en JSON
RENAMES_ENV = "AMEESH_FREEZE_RENAMES"
#: rangs de la politique de revue ; inconnu = doute, jamais « light »
RANK = {"light": 0, "normal": 1, "sensitive": 2}


class GuardError(Exception):
    """Lecture impossible : la garde refuse de conclure au vert."""


@dataclass(frozen=True)
class Move:
    """Un renommage du diff gelé : ancien chemin, nouveau chemin."""

    old: str
    new: str


@dataclass(frozen=True)
class Finding:
    """Un déplacement dont le nouveau chemin demande une revue plus faible."""

    move: Move
    old_class: str
    old_rule: tuple | None
    new_class: str

    def render(self) -> str:
        old_rule = (", ".join(self.old_rule) if self.old_rule
                    else "défaut de la politique")
        return (
            "REFUSÉ  %s (%s) < %s (%s, %s)\n"
            "        ajouter « %s » au canon, classe %s — et garder « %s » "
            "tant qu'il existe sur la cible"
            % (self.move.new, self.new_class, self.move.old, self.old_class,
               old_rule, self.move.new, self.old_class, self.move.old))


def _repo_path(value: object) -> str:
    """Un chemin de dépôt relatif, ou un refus : la garde ne devine pas."""
    if not isinstance(value, str) or not value:
        raise GuardError("chemin de renommage invalide : %r" % (value,))
    if (value.startswith("/") or "\\" in value
            or any(part in ("", ".", "..") for part in value.split("/"))):
        raise GuardError("chemin hors du dépôt : %r" % (value,))
    return value


def read_renames(raw: str) -> list[Move]:
    """Les couples du diff gelé, triés ; une forme inconnue refuse.

    Le format est une liste JSON de paires ``["ancien", "nouveau"]`` (ce que
    ``ameesh.freeze_checks`` écrit). Une variable absente n'est pas lue ici :
    l'appelant l'a déjà distinguée du doute.
    """
    try:
        data = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise GuardError("renommages illisibles : %s" % exc) from exc
    if not isinstance(data, list):
        raise GuardError("renommages illisibles : une liste de paires attendue")
    moves = set()
    for entry in data:
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            raise GuardError("renommages illisibles : paire attendue, reçu %r"
                             % (entry,))
        old, new = _repo_path(entry[0]), _repo_path(entry[1])
        if old == new:
            raise GuardError("renommage vers lui-même : %s" % old)
        moves.add(Move(old, new))
    return sorted(moves, key=lambda move: (move.old, move.new))


def check(moves, class_of) -> list[Finding]:
    """Les déplacements dont le nouveau chemin est classé plus bas.

    ``class_of(chemin) -> (classe, règle)`` : la politique de revue du canon ;
    une classe inconnue est un doute, jamais un vert.
    """
    findings = []
    for move in moves:
        old_class, old_rule = class_of(move.old)
        new_class, _new_rule = class_of(move.new)
        for name in (old_class, new_class):
            if name not in RANK:
                raise GuardError("classe de revue inconnue : %r" % (name,))
        if RANK[new_class] < RANK[old_class]:
            findings.append(Finding(move, old_class, old_rule, new_class))
    return findings


def _policy(canons, project: str):
    """La politique de revue d'une liste de canons déjà lus.

    Rend ``(class_of, ref)`` : ``ref`` porte le commit du canon lu. Un projet
    non déclaré ou une politique illisible refusent.
    """
    from ameesh import lot_class

    ref = [""]

    def class_of(path: str) -> tuple:
        result = lot_class.class_of_paths(canons, project, [path])
        if result.get("doubt"):
            raise GuardError("politique de revue illisible : %s"
                             % (result.get("why") or "doute"))
        ref[0] = result.get("policy_ref") or ref[0]
        entry = next((item for item in result.get("files") or ()
                      if item.get("path") == path), None)
        if entry is None:
            raise GuardError("chemin non classé par le canon : %s" % path)
        return entry["class"], tuple(entry["rule"]) if entry.get("rule") else None

    return class_of, ref


def configured_canons(project: str):
    """Les canons configurés de l'hôte, comme ``ameesh review-class``."""
    from ameesh import canon as canon_mod
    from ameesh import config as config_mod

    try:
        cfg = config_mod.load()
        canons = canon_mod.load_configured(cfg)
    except Exception as exc:  # config ou canon illisible : pas de classe supposée
        raise GuardError("canon configuré illisible : %s" % exc) from exc
    if not canons:
        raise GuardError("aucun canon configuré : politique de %r inconnue" % project)
    for loaded in canons:
        # un canon déclaré mais dont le chemin manque est rendu sans
        # fédération : pas une politique, et `class_of_paths` le lirait comme
        # un repli, donc comme un vert. Refus fermé.
        if not getattr(loaded, "federation", None):
            raise GuardError("canon illisible : %s (fédération absente)"
                             % (getattr(loaded, "root", "") or "canon"))
    return canons


def explicit_canon(root: str, ref: str):
    """Un canon épinglé lu hors configuration de l'hôte (dépôt git)."""
    from ameesh import canon as canon_mod

    try:
        loaded = canon_mod.load(root, ref=ref or "HEAD")
    except Exception as exc:  # racine, git ou ref illisible : pas de classe supposée
        raise GuardError("canon épinglé illisible : %s (%s)" % (root, exc)) from exc
    if not getattr(loaded, "readable", False):
        raise GuardError("canon épinglé illisible : %s" % root)
    if not getattr(loaded, "federation", None):
        raise GuardError("canon épinglé illisible : %s (fédération absente)" % root)
    return [loaded]


def configured_class_of(project: str):
    """La politique du canon configuré, comme ``ameesh review-class``."""
    return _policy(configured_canons(project), project)


def explicit_class_of(root: str, ref: str, project: str):
    """La politique d'un canon épinglé, hors configuration de l'hôte."""
    return _policy(explicit_canon(root, ref), project)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project", default="ameesh",
                        help="projet du canon (défaut : ameesh)")
    parser.add_argument("--canon", default="",
                        help="canon épinglé (dépôt git) ; sinon canon configuré de l'hôte")
    parser.add_argument("--ref", default="HEAD",
                        help="référence du canon épinglé (défaut : HEAD)")
    parser.add_argument("--renames", default=None,
                        help="renommages JSON ; sinon %s de l'environnement" % RENAMES_ENV)
    args = parser.parse_args(argv)
    raw = args.renames if args.renames is not None else os.environ.get(RENAMES_ENV)
    if raw is None:
        print("garde renommages/classe #7777 : renommages inconnus (%s absent, "
              "pas un gel) — contrôle inactif" % RENAMES_ENV)
        return 0
    try:
        moves = read_renames(raw)
    except GuardError as exc:
        print("garde renommages/classe #7777 : %s" % exc, file=sys.stderr)
        return 2
    if not moves:
        print("garde renommages/classe #7777 : aucun renommage dans le diff gelé")
        return 0
    try:
        if args.canon:
            class_of, ref = explicit_class_of(args.canon, args.ref, args.project)
        else:
            class_of, ref = configured_class_of(args.project)
        findings = check(moves, class_of)
    except GuardError as exc:
        print("garde renommages/classe #7777 : %s" % exc, file=sys.stderr)
        return 2
    if findings:
        for finding in findings:
            print(finding.render())
        print("garde renommages/classe #7777 : %d déplacement(s) affaibli(s) "
              "sur %d — compléter le canon (compagnon) avant le gel"
              % (len(findings), len(moves)))
        return 1
    print("garde renommages/classe #7777 : %d renommage(s), classe de relecture "
          "conservée" % len(moves) + (" (canon %s)" % ref[0] if ref[0] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
