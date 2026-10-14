#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""#1341 v2 (R1) — témoins de bout en bout du gel corrigé : plan unique par run.

Exécute le *bloc `run:` réel* de l'étape « fusion des mesures » de
`.github/workflows/carte.yml` (gel v2, plan unique `plan-mesure`), extrait du
YAML, sur des jeux de pièces synthétiques, avec `bash -e` :

  1. complet                  : les 6 parts avec durees.json → carte publiée ;
  2. part manquante           : artefact absent → refus, aucune carte ;
  3. part annulée             : artefact sans durees.json (cas psy1 réel) → refus ;
  4. durées vides             : durees.json vide → refus ;
  5. matrice non verte        : needs.mesure.result = cancelled → refus ;
  6. avant correctif          : la même pièce sans --attendu/--plan → carte
                                tronquée publiée (défaut d'origine, D) ;
  7. dérive de cache K1/K2    : plan calculé sur K1, part 2 mesurée sur K2
                                (l'ancien défaut A+D) → refus par la couverture
                                effective : `b` omis, `a` doublé ;
  8. plan unique K1           : les deux parts lisent le même plan K1 → carte
                                publiée, les 4 modules une seule fois.

Le témoin 7 vérifie aussi, à la source, que `parts_incompletes` (la garde D de
la v1) ne voyait rien — les deux `durees.json` sont non vides — et que c'est la
nouvelle garde de couverture qui refuse.

Sortie : `resultats-v2.json` + journal lisible. Aucune écriture dépôt, aucun
geste CI.
"""
from __future__ import annotations

import collections
import gzip
import importlib.util
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

ICI = pathlib.Path(__file__).resolve().parent
WT = ICI.parent / "wt-abd"
WORKFLOW = (WT / ".github" / "workflows" / "carte.yml").read_text(encoding="utf-8")

PARTS = ["mesure-psycopg-1", "mesure-psycopg-2", "mesure-psycopg-3",
         "mesure-psycopg-4", "mesure-psql-1", "mesure-psql-2"]
NOM_PART = re.compile(r"mesure-(psycopg|psql)-(\d+)$")

#: le témoin K1/K2 de la R1 : mêmes modules, poids différents
K1 = {"a": 8.0, "b": 7.0, "c": 6.0, "d": 5.0}
K2 = {"a": 7.0, "b": 8.0, "c": 6.0, "d": 5.0}


def _charger(nom: str, fichier: str):
    spec = importlib.util.spec_from_file_location(nom, WT / "scripts" / fichier)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cartemod = _charger("carte_v2", "map.py")
partsmod = _charger("parts_v2", "test-parts.py")


def _bloc_run(etape: str) -> str:
    """Le texte exact du `run: |` d'une étape nommée du workflow."""
    lignes = WORKFLOW.splitlines()
    for i, ligne in enumerate(lignes):
        if ligne.strip() == f"- name: {etape}":
            for j in range(i, len(lignes)):
                if lignes[j].strip() == "run: |":
                    suite = []
                    for k in range(j + 1, len(lignes)):
                        if lignes[k].strip() and not lignes[k].startswith(" " * 10):
                            break
                        suite.append(lignes[k][10:] if lignes[k].startswith(" " * 10)
                                     else lignes[k].strip())
                    return "\n".join(suite) + "\n"
    raise SystemExit(f"étape « {etape} » introuvable dans carte.yml")


def bloc_fusion() -> str:
    """Le texte exact du `run: |` de « fusion des mesures » (gel v2)."""
    return _bloc_run("fusion des mesures")


def ecrire_part(dossier: pathlib.Path, durees, flux: bool = False) -> None:
    dossier.mkdir(parents=True, exist_ok=True)
    if durees is not None:
        (dossier / "durees.json").write_text(json.dumps(durees), encoding="utf-8")
    if flux:
        (dossier / "flux.jsonl").write_text('{"type": "debut"}\n', encoding="utf-8")


def plan_depuis(parts: dict) -> dict:
    """Le plan unique qui correspond à un jeu de parts synthétiques."""
    sortie: dict = {"schema": "ameesh-test-plan/1", "kind": "mesure", "parts": {},
                    "classes": {}}
    par_pilote: dict[str, list] = collections.defaultdict(list)
    for nom, (durees, _) in sorted(parts.items()):
        m = NOM_PART.fullmatch(nom)
        if m is None or not durees:
            continue
        par_pilote[m.group(1)].append((int(m.group(2)), durees))
    for pilote, entrees in par_pilote.items():
        of = max(n for n, _ in entrees)
        sortie["parts"][pilote] = [
            {"part": n, "of": of, "modules": sorted(durees[pilote]),
             "label": f"{pilote} {n}/{of}", "pilot_list": pilote == "psql"}
            for n, durees in sorted(entrees)]
    return sortie


def scenario(nom: str, parts: dict, plan: dict, parts_mesure=None,
             resultat: str = "success") -> dict:
    """Joue le bloc réel dans un décor isolé ; renvoie le verdict."""
    with tempfile.TemporaryDirectory(prefix=f"temoin-1341-v2-{nom}-") as tmp:
        racine = pathlib.Path(tmp)
        for cible in ("scripts", "src", "tests"):
            (racine / cible).symlink_to(WT / cible)
        mesures = racine / "mesures"
        mesures.mkdir()
        for part, (durees, flux) in parts.items():
            ecrire_part(mesures / part, durees, flux)
        (racine / "plan").mkdir()
        (racine / "plan" / "plan.json").write_text(
            json.dumps(plan, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        etiquette = racine / "resume.md"
        shim = racine / "bin"
        shim.mkdir()
        (shim / "python").symlink_to(sys.executable)
        env = {**os.environ, "RESULTAT_MESURE": resultat,
               "PARTS_MESURE": " ".join(parts_mesure or PARTS),
               "GITHUB_STEP_SUMMARY": str(etiquette),
               "PATH": f"{shim}:{os.environ['PATH']}"}
        proc = subprocess.run(["bash", "-e", "-c", bloc_fusion()], cwd=racine, env=env,
                              capture_output=True, text=True)
        carte = racine / "carte" / "carte.json.gz"
        infos = {"scenario": nom, "code": proc.returncode,
                 "carte_publiee": carte.exists(),
                 "sortie": (proc.stdout + proc.stderr).strip()}
        if carte.exists():
            contenu = json.load(gzip.open(carte, "rt", encoding="utf-8"))
            infos["schema"] = contenu["schema"]
            infos["modules_avec_durees"] = sum(
                1 for i in contenu["modules"].values() if i.get("durations"))
        return infos


def avant_correctif(nom: str, parts: dict) -> dict:
    """L'ancienne commande, sans --attendu ni --plan : carte tronquée publiée."""
    with tempfile.TemporaryDirectory(prefix=f"temoin-1341-v2-{nom}-") as tmp:
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
            contenu = json.load(gzip.open(carte, "rt", encoding="utf-8"))
            infos["modules_avec_durees"] = sum(
                1 for i in contenu["modules"].values() if i.get("durations"))
        return infos


def temoin_cache() -> dict:
    """Le témoin K1/K2 à la source : découpage, garde D v1, garde de couverture."""
    part1 = partsmod.split(K1, 2)[0]
    part2_k2 = partsmod.split(K2, 2)[1]
    part2_k1 = partsmod.split(K1, 2)[1]
    with tempfile.TemporaryDirectory(prefix="temoin-1341-v2-source-") as tmp:
        racine = pathlib.Path(tmp)
        ecrire_part(racine / "mesure-psycopg-1", {"psycopg": {m: 1.0 for m in part1}})
        ecrire_part(racine / "mesure-psycopg-2", {"psycopg": {m: 1.0 for m in part2_k2}})
        dossiers = [str(p) for p in sorted(racine.iterdir())]
        plan_k1 = plan_depuis({"mesure-psycopg-1": ({"psycopg": {m: 1.0 for m in part1}}, False),
                               "mesure-psycopg-2": ({"psycopg": {m: 1.0 for m in part2_k1}}, False)})
        return {
            "K1": K1, "K2": K2,
            "part1_K1": part1,
            "part2_K2": part2_k2,
            "b_omis": "b" not in part1 + part2_k2,
            "a_double": (part1 + part2_k2).count("a") == 2,
            "garde_D_v1_voit_rien": cartemod.parts_incompletes(
                dossiers, ["mesure-psycopg-1", "mesure-psycopg-2"]) == [],
            "couverture_refuse": cartemod.couverture_incomplete(dossiers, plan_k1),
            "plan_unique_couvre_exactement": sorted(
                m for entree in plan_k1["parts"]["psycopg"] for m in entree["modules"]) == sorted(K1),
        }


def plan_egale_split() -> dict:
    """Le plan du job `plan-mesure` reproduit exactement `split` (ancienne route).

    Rejoue la commande réelle du workflow sur la carte de l'incident
    (`96a795fb2`, 248 durées psycopg / 0 psql) et compare chaque part du plan
    à `test-parts.py split`, le chemin que prenait chaque job de mesure."""
    carte = ICI.parent / "artifacts" / "carte-96a795fb2" / "carte.json.gz"
    if not carte.exists():
        return {"absent": str(carte), "toutes_egales": False}
    with tempfile.TemporaryDirectory(prefix="temoin-1341-v2-plan-") as tmp:
        out = pathlib.Path(tmp) / "plan.json"
        proc = subprocess.run(
            [sys.executable, str(WT / "scripts" / "test-parts.py"), "plan", "--out", str(out),
             "--parts", " ".join(PARTS), "--list-psql", "tests/parts/pilote-psql.txt",
             "--map", str(carte)],
            cwd=WT, capture_output=True, text=True)
        plan = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {}
        comparaisons = {}
        for pilote, entrees in sorted(plan.get("parts", {}).items()):
            for e in entrees:
                cmd = [sys.executable, "scripts/test-parts.py", "split",
                       "--parts", str(e["of"]), "--part", str(e["part"]),
                       "--driver", pilote, "--map", str(carte)]
                if pilote == "psql":
                    cmd += ["--list", "tests/parts/pilote-psql.txt"]
                p = subprocess.run(cmd, cwd=WT, capture_output=True, text=True)
                attendu = [l.split("tests.", 1)[1] for l in p.stdout.splitlines()
                           if l.startswith("tests.")]
                comparaisons[f"{pilote}-{e['part']}"] = attendu == e["modules"]
        return {"code": proc.returncode, "parties": comparaisons,
                "toutes_egales": bool(comparaisons) and all(comparaisons.values())}


def bloc_plan() -> dict:
    """Le bloc réel du job `plan-mesure`, avec et sans carte dans le cache.

    Avec la carte de l'incident : le plan est écrit et la carte source est
    recopiée dans l'artefact (la fusion la relira comme « précédente »).
    Sans carte : le plan retombe sur la table statique, sans carte source."""
    carte = ICI.parent / "artifacts" / "carte-96a795fb2" / "carte.json.gz"
    resultats = {}
    for nom, avec in (("avec-carte", True), ("sans-carte", False)):
        with tempfile.TemporaryDirectory(prefix=f"temoin-1341-v2-planbloc-{nom}-") as tmp:
            racine = pathlib.Path(tmp)
            for cible in ("scripts", "src", "tests"):
                (racine / cible).symlink_to(WT / cible)
            if avec:
                (racine / "carte").mkdir()
                shutil.copy(carte, racine / "carte" / "carte.json.gz")
            env = {**os.environ, "PARTS_MESURE": " ".join(PARTS),
                   "GITHUB_STEP_SUMMARY": str(racine / "resume.md")}
            proc = subprocess.run(["bash", "-e", "-c", _bloc_run("plan unique du run")],
                                  cwd=racine, env=env, capture_output=True, text=True)
            plan = racine / "plan" / "plan.json"
            infos = {"code": proc.returncode,
                     "plan_ecrit": plan.exists(),
                     "source_recopiee": (racine / "plan" / "carte-source.json.gz").exists(),
                     "sortie": (proc.stdout + proc.stderr).strip()}
            if plan.exists():
                contenu = json.loads(plan.read_text(encoding="utf-8"))
                infos["parts"] = {p: len(e) for p, e in contenu["parts"].items()}
                infos["source"] = {p: i.get("source") for p, i in contenu["poids"].items()}
            resultats[nom] = infos
    resultats["ok"] = (
        all(r["code"] == 0 and r["plan_ecrit"] for r in
            (resultats["avec-carte"], resultats["sans-carte"]))
        and resultats["avec-carte"]["source_recopiee"]
        and not resultats["sans-carte"]["source_recopiee"]
        and resultats["avec-carte"]["source"].get("psycopg") == "carte"
        and resultats["sans-carte"]["source"].get("psycopg") == "statique")
    return resultats


def main() -> int:
    complets = {}
    for i, part in enumerate(PARTS, 1):
        pilote = "psycopg" if "psycopg" in part else "psql"
        complets[part] = ({pilote: {f"test_{pilote}_{i}": 10.0 + i}}, False)
    plan_complet = plan_depuis(complets)

    resultats = []
    resultats.append(scenario("complet", complets, plan_complet))
    resultats.append(scenario("part-manquante", {p: v for p, v in complets.items()
                                                 if p != "mesure-psql-2"}, plan_complet))
    annulee = dict(complets)
    annulee["mesure-psycopg-1"] = (None, True)  # psy1 : artefact 18 Mo, pas de durées
    resultats.append(scenario("part-annulee-sans-durees", annulee, plan_complet))
    resultats.append(avant_correctif("avant-correctif-sans-garde", annulee))
    vides = dict(complets)
    vides["mesure-psql-1"] = ({}, False)
    resultats.append(scenario("durees-vides", vides, plan_complet))
    resultats.append(scenario("matrice-non-verte", complets, plan_complet,
                              resultat="cancelled"))

    # R1 : plan unique K1 ; la part 2 mesurée sur K2 (cache changé) doit être refusée.
    mesure_k1 = {"mesure-psycopg-1": ({"psycopg": {m: 1.0 for m in partsmod.split(K1, 2)[0]}}, False),
                 "mesure-psycopg-2": ({"psycopg": {m: 1.0 for m in partsmod.split(K1, 2)[1]}}, False)}
    derive = {"mesure-psycopg-1": ({"psycopg": {m: 1.0 for m in partsmod.split(K1, 2)[0]}}, False),
              "mesure-psycopg-2": ({"psycopg": {m: 1.0 for m in partsmod.split(K2, 2)[1]}}, False)}
    plan_k1 = plan_depuis(mesure_k1)
    resultats.append(scenario("derive-cache-K1K2", derive, plan_k1,
                              parts_mesure=["mesure-psycopg-1", "mesure-psycopg-2"]))
    resultats.append(scenario("plan-unique-K1", mesure_k1, plan_k1,
                              parts_mesure=["mesure-psycopg-1", "mesure-psycopg-2"]))

    source = temoin_cache()
    plan_split = plan_egale_split()
    plan_bloc = bloc_plan()
    (ICI / "resultats-v2.json").write_text(
        json.dumps({"source": source, "plan_egale_split": plan_split,
                    "plan_bloc": plan_bloc, "scenarios": resultats}, indent=1,
                   ensure_ascii=False) + "\n",
        encoding="utf-8")

    attendus = {"complet": True, "part-manquante": False, "part-annulee-sans-durees": False,
                "durees-vides": False, "matrice-non-verte": False,
                "avant-correctif-sans-garde": True, "derive-cache-K1K2": False,
                "plan-unique-K1": True}
    ok = True
    print("== témoin source K1/K2 ==")
    print(f'  part1(K1)[a,d]={source["part1_K1"]} part2(K2)={source["part2_K2"]} : '
          f'b omis={source["b_omis"]}, a doublé={source["a_double"]}, '
          f'garde D v1 muette={source["garde_D_v1_voit_rien"]}')
    for probleme in source["couverture_refuse"]:
        print(f'  couverture : {probleme}')
    ok &= source["b_omis"] and source["a_double"] and source["garde_D_v1_voit_rien"]
    ok &= bool(source["couverture_refuse"]) and source["plan_unique_couvre_exactement"]
    print("== plan du job plan-mesure == split sur la carte 96a795fb2 ==")
    for partie, meme in sorted(plan_split.get("parties", {}).items()):
        print(f'  {partie}: plan == split : {meme}')
    print(f'  toutes égales : {plan_split["toutes_egales"]}')
    ok &= plan_split["toutes_egales"]
    print("== bloc réel du job plan-mesure ==")
    for nom in ("avec-carte", "sans-carte"):
        r = plan_bloc[nom]
        print(f'  {nom:11s} code={r["code"]} plan={r["plan_ecrit"]} '
              f'source={r["source_recopiee"]} parts={r.get("parts")} '
              f'poids={r.get("source")}')
    ok &= plan_bloc["ok"]
    for r in resultats:
        verdict = "OK" if r["carte_publiee"] == attendus[r["scenario"]] else "KO"
        ok &= r["carte_publiee"] == attendus[r["scenario"]]
        detail = (f' modules_avec_durees={r["modules_avec_durees"]}'
                  if r.get("modules_avec_durees") is not None else "")
        print(f'[{verdict}] {r["scenario"]:28s} code={r["code"]:2d} '
              f'carte={r["carte_publiee"]}{detail}')
        for ligne in r["sortie"].splitlines()[:4]:
            print(f'        {ligne}')
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
