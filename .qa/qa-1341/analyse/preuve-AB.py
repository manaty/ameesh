#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""#1341 A/B — preuve que le découpage réel (CLI) suit la carte.

Vérifie la chaîne complète du correctif A : `test-parts.py split --map
carte.json.gz` (le chemin qu'emprunte carte.yml) reproduit exactement le
découpage simulé, avec la carte précédente réelle (96a795fb2, 248 durées
psycopg, 0 psql) et avec la table statique seule. B : la liste pilote psql
coupée en 2 parts avec une carte qui porte ses durées sous mesure.

Sortie : `analyse/preuve-AB.json`. Aucune écriture dépôt, aucun geste CI.
"""
from __future__ import annotations

import gzip
import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile

ICI = pathlib.Path(__file__).resolve().parent
ART = ICI.parent / "artifacts"
WT = ICI.parent / "wt-abd"
CARTE = ART / "carte-96a795fb2" / "carte.json.gz"
PY = sys.executable


def _charger(nom, chemin):
    spec = importlib.util.spec_from_file_location(nom, chemin)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


parts = _charger("test_parts_ab", WT / "scripts" / "test-parts.py")


def cli_split(driver: str, n: int, carte: pathlib.Path | None, liste: str | None = None):
    """Le découpage tel que la CI le lit : sortie du sous-commande split."""
    contenu = {}
    for k in range(1, n + 1):
        cmd = [PY, "scripts/test-parts.py", "split", "--parts", str(n), "--part", str(k),
               "--driver", driver]
        if carte:
            cmd += ["--map", str(carte)]
        if liste:
            cmd += ["--list", liste]
        proc = subprocess.run(cmd, cwd=WT, capture_output=True, text=True)
        if proc.returncode != 0:
            raise SystemExit(f"split {driver} {k}/{n} : {proc.stderr}")
        contenu[k] = sorted(ligne.split(".", 1)[1] for ligne in proc.stdout.splitlines()
                            if ligne.startswith("tests."))
    return contenu


def simule(driver: str, n: int, carte: pathlib.Path | None, liste: str | None = None):
    modules = parts.modules()
    if liste:
        modules = parts.read_list(WT / liste, modules)
    table = parts.map_durations(carte) if carte else None
    if table is not None and not table.get(driver):
        table = None
    pesees, _ = parts.weight(modules, driver, table)
    return {i: sorted(mods) for i, mods in enumerate(parts.split(pesees, n), 1)}


def main() -> int:
    resultat = {}
    # 1. lecture de la carte précédente
    table = parts.map_durations(CARTE)
    carte_json = json.loads(gzip.open(CARTE, "rt", encoding="utf-8").read())
    attendu = {m: i["durations"]["psycopg"] for m, i in carte_json["modules"].items()
               if (i.get("durations") or {}).get("psycopg") is not None}
    resultat["lecture_carte"] = {
        "psycopg": len(table.get("psycopg", {})), "psql": len(table.get("psql", {})),
        "identique_simulation": table.get("psycopg") == attendu,
    }
    # 2. psycopg 4 parts : statique (A0) et carte (A1)
    a0_cli = cli_split("psycopg", 4, None)
    a1_cli = cli_split("psycopg", 4, CARTE)
    resultat["psy_A0_cli_egale_simulation"] = a0_cli == simule("psycopg", 4, None)
    resultat["psy_A1_cli_egale_simulation"] = a1_cli == simule("psycopg", 4, CARTE)
    resultat["psy_A0_egale_A1"] = a0_cli == a1_cli
    sortie = json.loads((ICI / "simulate-1341b.json").read_text(encoding="utf-8"))
    resultat["A0_max_s"] = sortie["psy_4_parts"]["A0_statique"]["max_s"]
    resultat["A1_precedente_max_s"] = sortie["psy_4_parts"]["A1_precedente"]["max_s"]
    # 3. psql pilote 2 parts avec une carte qui porte ses durées sous mesure
    pilote = json.loads((ART / "mesure-psql-1" / "durees.json").read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="carte-1341-") as tmp:
        synthetique = pathlib.Path(tmp) / "carte-psql.json.gz"
        with gzip.open(synthetique, "wt", encoding="utf-8") as fh:
            json.dump({"schema": "ameesh-carte/1", "modules": {
                m: {"durations": {"psql": v}} for m, v in pilote["psql"].items()}}, fh)
        b1_cli = cli_split("psql", 2, synthetique, "tests/parts/pilote-psql.txt")
        b1_sim = simule("psql", 2, synthetique, "tests/parts/pilote-psql.txt")
    b0_cli = cli_split("psql", 2, None, "tests/parts/pilote-psql.txt")
    resultat["psql_B0_cli_egale_simulation"] = b0_cli == simule(
        "psql", 2, None, "tests/parts/pilote-psql.txt")
    resultat["psql_B1_cli_egale_simulation"] = b1_cli == b1_sim
    resultat["psql_B0_egale_B1"] = b0_cli == b1_cli

    (ICI / "preuve-AB.json").write_text(
        json.dumps(resultat, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    for cle, valeur in resultat.items():
        print(f"{cle}: {valeur}")
    attendus = ["lecture_carte", "psy_A0_cli_egale_simulation", "psy_A1_cli_egale_simulation",
                "psql_B0_cli_egale_simulation", "psql_B1_cli_egale_simulation"]
    ok = (resultat["lecture_carte"]["identique_simulation"]
          and all(resultat[c] for c in attendus if c != "lecture_carte"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
