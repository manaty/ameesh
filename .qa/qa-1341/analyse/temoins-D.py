#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""#1341 D — témoins de bout en bout du garde-fou de publication.

Exécute le *bloc `run:` réel* de l'étape « fusion des mesures » de
`.github/workflows/carte.yml` (extrait du YAML) sur cinq jeux de pièces
synthétiques, avec `bash -e` (le shell par défaut de GitHub Actions) :

  1. complet            : les 6 parts avec durees.json → carte publiée ;
  2. part manquante     : un artefact absent → refus, aucune carte ;
  3. part annulée       : artefact publié sans durees.json (cas psy1 réel) → refus ;
  4. durées vides       : durees.json vide → refus ;
  5. matrice non verte  : needs.mesure.result = cancelled → refus avant fusion ;
  6. avant correctif    : la même pièce sans --attendu → carte tronquée publiée.

Sortie : `witness/resultats.json` + journal lisible. Aucune écriture dépôt,
aucun geste CI.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import textwrap

ICI = pathlib.Path(__file__).resolve().parent
WT = ICI.parent / "wt-abd"
WORKFLOW = (WT / ".github" / "workflows" / "carte.yml").read_text(encoding="utf-8")

PARTS = ["mesure-psycopg-1", "mesure-psycopg-2", "mesure-psycopg-3",
         "mesure-psycopg-4", "mesure-psql-1", "mesure-psql-2"]


def bloc_fusion() -> str:
    """Le texte exact du `run: |` de « fusion des mesures »."""
    lignes = WORKFLOW.splitlines()
    for i, ligne in enumerate(lignes):
        if ligne.strip() == "- name: fusion des mesures":
            for j in range(i, len(lignes)):
                if lignes[j].strip() == "run: |":
                    suite = []
                    for k in range(j + 1, len(lignes)):
                        if lignes[k].strip() and not lignes[k].startswith(" " * 10):
                            break
                        suite.append(lignes[k][10:] if lignes[k].startswith(" " * 10)
                                     else lignes[k].strip())
                    return "\n".join(suite) + "\n"
    raise SystemExit("étape « fusion des mesures » introuvable dans carte.yml")


def ecrire_part(dossier: pathlib.Path, durees, flux: bool = False) -> None:
    dossier.mkdir(parents=True, exist_ok=True)
    if durees is not None:
        (dossier / "durees.json").write_text(json.dumps(durees), encoding="utf-8")
    if flux:
        (dossier / "flux.jsonl").write_text('{"type": "debut"}\n', encoding="utf-8")


def scenario(nom: str, parts: dict, resultat: str = "success") -> dict:
    """Joue le bloc réel dans un décor isolé ; renvoie le verdict."""
    with tempfile.TemporaryDirectory(prefix=f"temoin-1341-{nom}-") as tmp:
        racine = pathlib.Path(tmp)
        for cible in ("scripts", "src", "tests"):
            (racine / cible).symlink_to(WT / cible)
        mesures = racine / "mesures"
        mesures.mkdir()
        for part, (durees, flux) in parts.items():
            ecrire_part(mesures / part, durees, flux)
        etiquette = racine / "resume.md"
        # GitHub runners ont `python` (setup-python) ; sur cet hôte, un shim.
        shim = racine / "bin"
        shim.mkdir()
        (shim / "python").symlink_to(sys.executable)
        env = {**os.environ, "RESULTAT_MESURE": resultat,
               "PARTS_MESURE": " ".join(PARTS),
               "GITHUB_STEP_SUMMARY": str(etiquette),
               "PATH": f"{shim}:{os.environ['PATH']}"}
        proc = subprocess.run(["bash", "-e", "-c", bloc_fusion()], cwd=racine, env=env,
                              capture_output=True, text=True)
        carte = racine / "carte" / "carte.json.gz"
        infos = {"scenario": nom, "code": proc.returncode,
                 "carte_publiee": carte.exists(),
                 "sortie": (proc.stdout + proc.stderr).strip()}
        if carte.exists():
            import gzip
            contenu = json.load(gzip.open(carte, "rt", encoding="utf-8"))
            infos["schema"] = contenu["schema"]
            infos["modules_avec_durees"] = sum(
                1 for i in contenu["modules"].values() if i.get("durations"))
        return infos


def avant_correctif(nom: str, parts: dict) -> dict:
    """L'ancienne commande, sans --attendu : la carte est publiée tronquée."""
    with tempfile.TemporaryDirectory(prefix=f"temoin-1341-{nom}-") as tmp:
        racine = pathlib.Path(tmp)
        for cible in ("scripts", "src", "tests"):
            (racine / cible).symlink_to(WT / cible)
        mesures = racine / "mesures"
        mesures.mkdir()
        for part, (durees, flux) in parts.items():
            ecrire_part(mesures / part, durees, flux)
        carte = racine / "carte" / "carte.json.gz"
        carte.parent.mkdir()
        proc = subprocess.run(
            [sys.executable, str(WT / "scripts" / "map.py"), "merge",
             "--output", str(carte), *[str(p) for p in sorted(mesures.iterdir())]],
            cwd=racine, capture_output=True, text=True)
        infos = {"scenario": nom, "code": proc.returncode, "carte_publiee": carte.exists(),
                 "sortie": (proc.stdout + proc.stderr).strip()}
        if carte.exists():
            import gzip
            contenu = json.load(gzip.open(carte, "rt", encoding="utf-8"))
            infos["modules_avec_durees"] = sum(
                1 for i in contenu["modules"].values() if i.get("durations"))
        return infos


def main() -> int:
    # chaque part porte ses propres modules : une part absente doit se voir
    complets = {}
    for i, part in enumerate(PARTS, 1):
        pilote = "psycopg" if "psycopg" in part else "psql"
        complets[part] = ({pilote: {f"test_{pilote}_{i}": 10.0 + i}}, False)

    resultats = []
    resultats.append(scenario("complet", complets))
    resultats.append(scenario("part-manquante", {p: v for p, v in complets.items()
                                                 if p != "mesure-psql-2"}))
    annulee = dict(complets)
    annulee["mesure-psycopg-1"] = (None, True)  # psy1 : artefact 18 Mo, pas de durées
    resultats.append(scenario("part-annulee-sans-durees", annulee))
    resultats.append(avant_correctif("avant-correctif-sans-garde", annulee))
    vides = dict(complets)
    vides["mesure-psql-1"] = ({}, False)
    resultats.append(scenario("durees-vides", vides))
    resultats.append(scenario("matrice-non-verte", complets, resultat="cancelled"))

    (ICI / "resultats.json").write_text(
        json.dumps(resultats, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    ok = {"complet": True, "part-manquante": False, "part-annulee-sans-durees": False,
          "durees-vides": False, "matrice-non-verte": False,
          "avant-correctif-sans-garde": True}
    for r in resultats:
        if r["scenario"] == "complet":
            attendu = "carte publiée"
        elif r["scenario"] == "avant-correctif-sans-garde":
            attendu = "carte tronquée publiée (défaut d'origine)"
        else:
            attendu = "refus"
        verdict = "OK" if (r["carte_publiee"] == ok[r["scenario"]]) else "KO"
        detail = (f' modules_avec_durees={r["modules_avec_durees"]}'
                  if r.get("modules_avec_durees") is not None else "")
        print(f'[{verdict}] {r["scenario"]:28s} code={r["code"]:2d} '
              f'carte={r["carte_publiee"]}{detail} ({attendu})')
        for ligne in r["sortie"].splitlines()[:4]:
            print(f'        {ligne}')
    return 0 if all(r["carte_publiee"] == ok[r["scenario"]] for r in resultats) else 1


if __name__ == "__main__":
    sys.exit(main())
