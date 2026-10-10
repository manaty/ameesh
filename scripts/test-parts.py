#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Découpage de la suite de tests en parts parallèles (L116).

    scripts/test-parts.py repartir --parts N --part K --pilote psql [--liste F]
    scripts/test-parts.py lancer   --parts N --part K --pilote psql [--liste F]
                                   [--durees-sortie F.json]
    scripts/test-parts.py durees   --pilote psql F.json [F.json…]

La répartition est **déterministe** et se fait **par module** de test
(`tests/test*.py`, le motif de `unittest discover`) : un module n'est jamais
coupé, ses `setUpClass` et son schéma jetable restent ensemble. Les modules
sont pesés par la durée mesurée de `tests/parts/durees.json` (par pilote),
puis placés du plus lourd au plus léger dans la part la moins chargée.
Un module absent de la table (nouveau) est pesé par son nombre de tests
(`def test_`) multiplié par la durée moyenne d'un test du pilote : c'est le
repli, signalé sur la sortie d'erreur pour qu'on rafraîchisse la table.

`--liste` restreint la suite aux modules d'un fichier (un nom par ligne, `#`
pour les commentaires) : `tests/parts/pilote-psql.txt` sur une PR.

`lancer` exécute la part dans ce processus (`unittest`, verbeux) et mesure la
durée de chaque module ; `durees` fusionne ces mesures dans la table.
Le signal SIGINT est remis à son traitement par défaut avant les tests : lancé
en arrière-plan par un shell, il serait sinon ignoré, et hérité ignoré par
les sous-processus des tests (exécuteur, CLI) qui s'arrêtent sur SIGINT.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import pathlib
import re
import signal
import sys
import time
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
TESTS = REPO / "tests"
TABLE = TESTS / "parts" / "durees.json"
#: durée par test quand la table n'a rien pour le pilote (repli du repli)
DEFAUT_PAR_TEST = 0.5


def modules(tests: pathlib.Path = TESTS) -> list[str]:
    """Les modules que `unittest discover -s tests` trouverait (sans préfixe)."""
    return sorted(p.stem for p in tests.glob("test*.py"))


def lire_liste(chemin: str | os.PathLike, connus: list[str]) -> list[str]:
    noms = []
    for ligne in pathlib.Path(chemin).read_text(encoding="utf-8").splitlines():
        nom = ligne.split("#", 1)[0].strip()
        if nom:
            noms.append(nom)
    inconnus = sorted(set(noms) - set(connus))
    if inconnus:
        raise SystemExit(f"{chemin} : module(s) inconnu(s) : {', '.join(inconnus)}")
    return sorted(set(noms))


def nombre_de_tests(module: str, tests: pathlib.Path = TESTS) -> int:
    texte = (tests / f"{module}.py").read_text(encoding="utf-8")
    return max(1, len(re.findall(r"^\s*def test", texte, re.M)))


def poids(mods: list[str], pilote: str, table: dict | None = None,
          tests: pathlib.Path = TESTS) -> tuple[dict[str, float], list[str]]:
    """Poids de chaque module, et la liste de ceux pesés par le repli."""
    if table is None:
        table = json.loads(TABLE.read_text(encoding="utf-8")) if TABLE.exists() else {}
    mesures = table.get(pilote, {})
    connus = [m for m in mesures if m in set(mods)]
    total_tests = sum(nombre_de_tests(m, tests) for m in connus)
    par_test = (sum(mesures[m] for m in connus) / total_tests) if total_tests else DEFAUT_PAR_TEST
    resultat, repli = {}, []
    for m in mods:
        if m in mesures:
            resultat[m] = float(mesures[m])
        else:
            resultat[m] = nombre_de_tests(m, tests) * par_test
            repli.append(m)
    return resultat, repli


def repartir(pesees: dict[str, float], parts: int) -> list[list[str]]:
    """Plus lourd d'abord, dans la part la moins chargée (à égalité : la
    première). Même entrée, même découpage, sur toutes les machines."""
    charges = [0.0] * parts
    contenu: list[list[str]] = [[] for _ in range(parts)]
    for m in sorted(pesees, key=lambda m: (-pesees[m], m)):
        i = min(range(parts), key=lambda i: (charges[i], i))
        charges[i] += pesees[m]
        contenu[i].append(m)
    return [sorted(c) for c in contenu]


def selection(args) -> tuple[list[str], float, list[str]]:
    if not 1 <= args.part <= args.parts:
        raise SystemExit(f"part {args.part} hors de 1..{args.parts}")
    mods = modules()
    if args.liste:
        mods = lire_liste(args.liste, mods)
    pesees, repli = poids(mods, args.pilote)
    part = repartir(pesees, args.parts)[args.part - 1]
    return part, sum(pesees[m] for m in part), [m for m in repli if m in part]


class _Resultat(unittest.TextTestResult):
    """Chronomètre par module : le temps écoulé depuis la fin du test précédent
    est compté au module du test qui se termine (setUpClass compris)."""

    durees: collections.Counter

    def startTestRun(self):
        self.durees = collections.Counter()
        self._depuis = time.monotonic()
        super().startTestRun()

    def stopTest(self, test):
        super().stopTest(test)
        maintenant = time.monotonic()
        module = type(test).__module__
        if module.startswith("unittest"):  # _ErrorHolder (setUpClass en échec)
            module = getattr(test, "description", "?").split("(")[-1].split(".")[0]
        self.durees[module.rsplit(".", 1)[-1]] += maintenant - self._depuis
        self._depuis = maintenant


def lancer(args) -> int:
    signal.signal(signal.SIGINT, signal.default_int_handler)
    part, estimee, repli = selection(args)
    print(f"part {args.part}/{args.parts} ({args.pilote}) : {len(part)} module(s), "
          f"~{estimee:.0f} s estimées", flush=True)
    if repli:
        print(f"  pesés par le nombre de tests (absents de {TABLE.relative_to(REPO)}) : "
              f"{', '.join(repli)}", file=sys.stderr, flush=True)
    os.chdir(REPO)
    sys.path[:0] = [str(REPO), str(REPO / "src")]
    suite = unittest.TestLoader().loadTestsFromNames([f"tests.{m}" for m in part])
    runner = unittest.TextTestRunner(verbosity=2, resultclass=_Resultat)
    resultat = runner.run(suite)
    durees = {m: round(t, 1) for m, t in sorted(resultat.durees.items())}
    if args.durees_sortie:
        pathlib.Path(args.durees_sortie).write_text(
            json.dumps({args.pilote: durees}, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    lents = sorted(durees.items(), key=lambda kv: -kv[1])[:5]
    print("modules les plus lents : " + ", ".join(f"{m} {t:.0f} s" for m, t in lents), flush=True)
    return 0 if resultat.wasSuccessful() else 1


def durees(args) -> int:
    table = json.loads(TABLE.read_text(encoding="utf-8")) if TABLE.exists() else {}
    mesures = table.setdefault(args.pilote, {})
    for f in args.fichiers:
        mesures.update(json.loads(pathlib.Path(f).read_text(encoding="utf-8")).get(args.pilote, {}))
    connus = set(modules())
    table[args.pilote] = {m: t for m, t in sorted(mesures.items()) if m in connus}
    TABLE.write_text(json.dumps(table, indent=1, sort_keys=True, ensure_ascii=False) + "\n",
                     encoding="utf-8")
    print(f"{TABLE.relative_to(REPO)} : {len(table[args.pilote])} module(s) pour {args.pilote}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sous = ap.add_subparsers(dest="commande", required=True)
    for nom in ("repartir", "lancer"):
        p = sous.add_parser(nom)
        p.add_argument("--parts", type=int, required=True)
        p.add_argument("--part", type=int, required=True)
        p.add_argument("--pilote", choices=("psql", "psycopg"), required=True)
        p.add_argument("--liste", help="fichier de modules (ex. tests/parts/pilote-psql.txt)")
        if nom == "lancer":
            p.add_argument("--durees-sortie", help="écrit les durées mesurées (JSON)")
    p = sous.add_parser("durees")
    p.add_argument("--pilote", choices=("psql", "psycopg"), required=True)
    p.add_argument("fichiers", nargs="+")
    args = ap.parse_args(argv)
    if args.commande == "repartir":
        part, estimee, _ = selection(args)
        print("\n".join(f"tests.{m}" for m in part))
        print(f"~{estimee:.0f} s estimées", file=sys.stderr)
        return 0
    return lancer(args) if args.commande == "lancer" else durees(args)


if __name__ == "__main__":
    sys.exit(main())
