#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""#1341 C — agrège des flux de parts (ameesh-test-event/1) en durées par module.

    flux-durations.py --driver psql [--out F.json] PART_DIR [PART_DIR…]

Chaque `PART_DIR` contient un `flux.jsonl` (une part de `test-parts.py run
--flow`). Sortie : `{driver: {module: secondes}}`, la forme acceptée par
`scripts/test-parts.py durations`, plus des statistiques de couverture :

  - `somme` : somme des `duree_s` des tests d'un module (borne basse) ;
  - `span`  : dernier `ts` − premier (`ts` − `duree_s`) du module (setUp /
    setUpClass intra-module compris, convention de `durees.json`) ;
  - un module est `complet` si la part a une ligne `fin` (non annulée) ou si
    le nombre de tests tracés atteint le nombre de `def test` du source ;
    sinon il est `partiel` et n'est pas proposé pour la table.

Sortie JSON : ``{"driver":…, "durees": {module: span_s}, "couverture": {…}}``.
Aucune écriture dans le dépôt : le script est un outil de qualification.
"""
from __future__ import annotations

import argparse
import datetime
import json
import pathlib
import re
import sys

TESTS = pathlib.Path(sys.argv[0]).resolve().parents[3] / "tests"


def test_count(module: str, tests: pathlib.Path) -> int:
    chemin = tests / f"{module}.py"
    if not chemin.exists():
        return 0
    return max(1, len(re.findall(r"^\s*def test", chemin.read_text(encoding="utf-8"), re.M)))


def _ts(txt: str) -> float:
    return datetime.datetime.fromisoformat(txt.replace("Z", "+00:00")).timestamp()


def aggregate(parts: list[pathlib.Path], tests: pathlib.Path) -> dict:
    durees: dict[str, dict] = {}
    fini: dict[str, bool] = {}
    for part in parts:
        flux = part / "flux.jsonl"
        if not flux.exists():
            print(f"{part} : pas de flux.jsonl", file=sys.stderr)
            continue
        nom_part = part.name
        fin = False
        for ligne in flux.read_text(encoding="utf-8").splitlines():
            try:
                fait = json.loads(ligne)
            except ValueError:
                continue
            if fait.get("type") == "fin":
                fin = True
            if fait.get("type") != "test":
                continue
            module = fait.get("module")
            if not module:
                continue
            d = durees.setdefault(module, {"somme": 0.0, "tests": 0,
                                           "debut": None, "fin": None, "parts": set()})
            d["parts"].add(nom_part)
            d["somme"] += float(fait.get("duree_s") or 0.0)
            d["tests"] += 1
            t = _ts(fait["ts"])
            debut = t - float(fait.get("duree_s") or 0.0)
            d["debut"] = debut if d["debut"] is None else min(d["debut"], debut)
            d["fin"] = t if d["fin"] is None else max(d["fin"], t)
        fini[nom_part] = fin
    modules = {}
    for module, d in durees.items():
        attendu = test_count(module, tests)
        complet = any(fini.get(p, False) for p in d["parts"]) or d["tests"] >= attendu
        modules[module] = {"somme": round(d["somme"], 1),
                           "span": round(d["fin"] - d["debut"], 1),
                           "tests": d["tests"], "def_test": attendu,
                           "complet": complet, "parts": sorted(d["parts"])}
    return modules


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--driver", required=True, choices=("psql", "psycopg"))
    ap.add_argument("--out")
    ap.add_argument("--tests", default=str(TESTS))
    ap.add_argument("parts", nargs="+")
    args = ap.parse_args(argv)

    modules = aggregate([pathlib.Path(p) for p in args.parts], pathlib.Path(args.tests))
    connus = sorted(p.stem for p in pathlib.Path(args.tests).glob("test*.py"))
    complet = [m for m, d in modules.items() if d["complet"]]
    partiel = [m for m, d in modules.items() if not d["complet"]]
    absents = [m for m in connus if m not in modules]
    resultat = {
        "driver": args.driver,
        "durees": {m: modules[m]["span"] for m in complet},
        "couverture": {
            "modules_source": len(connus),
            "vus": len(modules),
            "complets": len(complet),
            "partiels": sorted(partiel),
            "absents": sorted(absents),
            "somme_totale_s": round(sum(modules[m]["span"] for m in complet), 1),
        },
        "detail": {m: modules[m] for m in sorted(modules)},
    }
    texte = json.dumps(resultat, indent=1, ensure_ascii=False, sort_keys=True)
    if args.out:
        pathlib.Path(args.out).write_text(texte + "\n", encoding="utf-8")
    print(f"{args.driver} : {len(complet)} module(s) complets, "
          f"{len(partiel)} partiel(s), {len(absents)} absent(s) ; "
          f"total {resultat['couverture']['somme_totale_s']:.0f} s")
    if partiel:
        print("  partiels : " + ", ".join(partiel))
    if absents:
        print("  absents  : " + ", ".join(absents))
    return 0


if __name__ == "__main__":
    sys.exit(main())
