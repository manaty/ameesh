#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""#1341 phase 2 — preuves de répartition A/B/C sur la base qualifiée 0532faed4.

Algorithme exact : `weight`/`split` de
`wt-abd/scripts/test-parts.py` (importlib), chargé depuis la base qualifiée.

Scénarios (tous rejouables depuis `artifacts/`) :

  psql 6 parts (run tests 38610188942, sans mesure)
    S0        poids = table statique seule (comportement du run)
    C-somme   table + Σ duree_s des flux (245 modules complets)
    C-span    table + enveloppe (dernier ts − premier ts-durée) des flux

  psycopg 4 parts (run carte 38610188898, sous mesure)
    A0        poids = table statique (comportement actuel de carte.yml)
    A1-carte  poids = durées sous mesure publiables par la carte (psy2-4)
    A1-plein  poids = durées sous mesure des 4 parts (régime établi)

  psql pilote 2 parts (mesure, 45 modules)
    B0        poids = table statique (aucune carte psql)
    B1        poids = durées sous mesure de mesure-psql-1 (carte)

Sortie : `simulate-1341b.json` + tableaux lisibles. Aucune écriture dépôt.
"""
from __future__ import annotations

import collections
import importlib.util
import json
import pathlib
import sys

ICI = pathlib.Path(__file__).resolve().parent
ART = ICI.parent / "artifacts"
WT = ICI.parent / "wt-abd"


def _charger(nom: str, chemin: pathlib.Path):
    spec = importlib.util.spec_from_file_location(nom, chemin)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


parts = _charger("test_parts_1341", WT / "scripts" / "test-parts.py")


def flux(path: pathlib.Path) -> list[dict]:
    lignes = []
    for ligne in path.read_text(encoding="utf-8").splitlines():
        if ligne.strip():
            try:
                lignes.append(json.loads(ligne))
            except ValueError:
                pass
    return lignes


def stats_flux(path: pathlib.Path) -> dict:
    """Σ duree_s et enveloppe (span) par module, + modules annoncés/finis."""
    debut, fin = None, False
    somme: dict[str, float] = collections.defaultdict(float)
    bornes: dict[str, list] = {}
    for fait in flux(path):
        if fait.get("type") == "debut":
            debut = fait
        elif fait.get("type") == "fin":
            fin = True
        elif fait.get("type") == "test" and fait.get("module"):
            import datetime
            d = float(fait.get("duree_s") or 0.0)
            t = datetime.datetime.fromisoformat(fait["ts"].replace("Z", "+00:00")).timestamp()
            somme[fait["module"]] += d
            b = bornes.setdefault(fait["module"], [t - d, t])
            b[0], b[1] = min(b[0], t - d), max(b[1], t)
    span = {m: round(b[1] - b[0], 1) for m, b in bornes.items()}
    return {"debut": debut or {}, "fin": fin, "somme": dict(somme), "span": span}


def table_statique(driver: str) -> dict:
    table = json.loads((ART / "durees-0532faed4.json").read_text(encoding="utf-8"))
    return table.get(driver, {})


def repartir(modules: list[str], driver: str, poids_table: dict, n: int,
             verite: dict, verite_somme: dict | None = None) -> dict:
    """Découpe avec `poids_table` (algorithme exact) et évalue avec `verite`."""
    pesees, repli = parts.weight(modules, driver, {driver: poids_table})
    contenu = parts.split(pesees, n)
    par_part = []
    for i, mods in enumerate(contenu, 1):
        charge = sum(verite.get(m, pesees[m]) for m in mods)
        part = {"part": i, "modules": len(mods),
                "poids_s": round(sum(pesees[m] for m in mods), 1),
                "verite_s": round(charge, 1)}
        if verite_somme is not None:
            part["somme_s"] = round(sum(verite_somme.get(m, 0.0) for m in mods), 1)
        par_part.append(part)
    return {"parts": par_part, "max_s": round(max(p["verite_s"] for p in par_part), 1),
            "max_somme_s": round(max(p.get("somme_s", 0.0) for p in par_part), 1)
            if verite_somme is not None else None,
            "repli": len(repli), "contenu": contenu}


def population_mesuree() -> list[str]:
    """Les 250 modules réellement exécutés par l'incident, sur 0532faed4.

    `parts.modules()` lit l'arbre de travail *avec* le correctif, donc 251
    modules (le test D ajouté n'existe pas sur la base) : sans ce filtre, les
    invariants « A0/S0 reproduisent les parts réelles » deviennent faux et les
    poids changent. On ne simule que la population mesurée par l'incident.
    """
    vus = set()
    plan = json.loads((ART / "plan" / "plan.json").read_text(encoding="utf-8"))
    for entree in plan["parts"]["psql"]:
        vus.update(entree["modules"])
    for n in range(1, 5):
        debut = next(f for f in flux(ART / f"mesure-psycopg-{n}" / "flux.jsonl")
                     if f.get("type") == "debut")
        vus.update(debut["modules"])
    return sorted(set(parts.modules()) & vus)


def main() -> int:
    modules = population_mesuree()  # 250 sur la base qualifiée 0532faed4
    sortie: dict = {"base": "0532faed4", "modules": len(modules)}

    # ---- vérité terrain : run tests, sans mesure --------------------------
    tests = {}
    for n in range(1, 7):
        tests[n] = stats_flux(ART / "flux" / f"psql-{n}" / "flux.jsonl")
    verite_psql, verite_psql_somme = {}, {}
    for n, st in tests.items():
        for m, s in st["span"].items():
            verite_psql[m] = s
        for m, s in st["somme"].items():
            verite_psql_somme[m] = verite_psql_somme.get(m, 0.0) + s
    # 5 modules sans mesure complète : garder la valeur de la table statique
    for m in modules:
        verite_psql.setdefault(m, table_statique("psql").get(m, 10.0))
    sortie["verite_psql_6_parts"] = {
        "total_somme_s": round(sum(verite_psql_somme.values()), 1)}

    # S0 / C — la table rafraîchie ne prend que les 245 modules complets
    # (sortie de `flux-durations.py`, comme le ferait `test-parts.py durations`)
    flux_psql = json.loads((ICI / "c-psql-flux.json").read_text(encoding="utf-8"))
    complets = flux_psql["durees"]
    stat_psql = table_statique("psql")
    situation = {}
    situation["S0_statique"] = repartir(modules, "psql", stat_psql, 6, verite_psql,
                                        verite_psql_somme)
    c_somme = dict(stat_psql)
    c_somme.update({m: flux_psql["detail"][m]["somme"] for m in complets})
    situation["C_somme"] = repartir(modules, "psql", c_somme, 6, verite_psql,
                                    verite_psql_somme)
    c_span = dict(stat_psql)
    c_span.update(complets)
    situation["C_span"] = repartir(modules, "psql", c_span, 6, verite_psql,
                                   verite_psql_somme)
    sortie["psql_6_parts"] = {k: {kk: vv for kk, vv in v.items() if kk != "contenu"}
                              for k, v in situation.items()}
    sortie["psql_6_parts"]["couverture_C"] = {
        "complets": len(complets), "total_modules": len(modules),
        "poids_reels_s": round(sum(complets.values()), 1)}
    # vérification : S0 doit reproduire exactement les parts du plan du run
    plan = json.loads((ART / "plan" / "plan.json").read_text(encoding="utf-8"))
    plan_psql = {e["part"]: sorted(e["modules"]) for e in plan["parts"]["psql"]}
    simule = {i: sorted(mods) for i, mods in enumerate(situation["S0_statique"]["contenu"], 1)}
    sortie["psql_6_parts"]["S0_reproduit_le_plan"] = plan_psql == simule

    # ---- vérité terrain : run carte, sous mesure --------------------------
    verite_psy: dict[str, float] = {}
    verite_psy_somme: dict[str, float] = {}
    poids_carte_psy: dict[str, float] = {}
    for n in range(1, 5):
        d = ART / f"mesure-psycopg-{n}"
        st = stats_flux(d / "flux.jsonl")
        verite_psy.update(st["span"])
        for m, s in st["somme"].items():
            verite_psy_somme[m] = verite_psy_somme.get(m, 0.0) + s
        dj = d / "durees.json"
        if dj.exists():
            poids_carte_psy.update(json.loads(dj.read_text(encoding="utf-8")).get("psycopg", {}))
    for m in modules:
        verite_psy.setdefault(m, stat_psql.get(m, 10.0))
    stat_psy = table_statique("psycopg")

    # carte précédente (96a795fb2, run 38601979838) : 248 durées psycopg
    precedente = json.loads(
        __import__("gzip").open(ART / "carte-96a795fb2" / "carte.json.gz", "rt",
                                encoding="utf-8").read())
    poids_prec = {m: i["durations"]["psycopg"] for m, i in precedente["modules"].items()
                  if (i.get("durations") or {}).get("psycopg") is not None}

    a0 = repartir(modules, "psycopg", stat_psy, 4, verite_psy, verite_psy_somme)
    a1_carte = repartir(modules, "psycopg", poids_carte_psy, 4, verite_psy, verite_psy_somme)
    a1_prec = repartir(modules, "psycopg", poids_prec, 4, verite_psy, verite_psy_somme)
    a1_plein = repartir(modules, "psycopg", verite_psy, 4, verite_psy, verite_psy_somme)
    # vérification : A0 doit reproduire les parts réellement mesurées
    reels = {}
    for n in range(1, 5):
        premiere = next(f for f in flux(ART / f"mesure-psycopg-{n}" / "flux.jsonl")
                        if f.get("type") == "debut")
        reels[n] = sorted(premiere["modules"])
    simule_a0 = {i: sorted(mods) for i, mods in enumerate(a0["contenu"], 1)}
    sortie["psy_4_parts"] = {
        "A0_statique": {k: v for k, v in a0.items() if k != "contenu"},
        "A1_carte": {k: v for k, v in a1_carte.items() if k != "contenu"},
        "A1_precedente": {k: v for k, v in a1_prec.items() if k != "contenu"},
        "A1_plein": {k: v for k, v in a1_plein.items() if k != "contenu"},
        "modules_poids_carte": len(poids_carte_psy),
        "modules_poids_precedente": len(poids_prec),
        "A0_reproduit_les_parts": reels == simule_a0,
    }

    # ---- mesure psql pilote, 2 parts --------------------------------------
    d = ART / "mesure-psql-1"
    st = stats_flux(d / "flux.jsonl")
    verite_pilote, verite_pilote_somme = st["span"], st["somme"]
    poids_pilote = json.loads((d / "durees.json").read_text(encoding="utf-8"))["psql"]
    pilote = parts.read_list(WT / "tests" / "parts" / "pilote-psql.txt", modules)
    b0 = repartir(pilote, "psql", stat_psql, 2, verite_pilote, verite_pilote_somme)
    b1 = repartir(pilote, "psql", poids_pilote, 2, verite_pilote, verite_pilote_somme)
    sortie["mesure_psql_2_parts"] = {
        "modules": len(pilote), "actuel_1_part_s": round(sum(verite_pilote.values()), 1),
        "actuel_1_part_somme_s": round(sum(verite_pilote_somme.values()), 1),
        "B0_statique": {k: v for k, v in b0.items() if k != "contenu"},
        "B1_carte": {k: v for k, v in b1.items() if k != "contenu"},
    }

    (ICI / "simulate-1341b.json").write_text(
        json.dumps(sortie, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"base {sortie['base']} — {len(modules)} modules de test")
    print(f"vérité psql (6 parts, Σ tests) : {sortie['verite_psql_6_parts']['total_somme_s']/60:.1f} min")
    print("\npsql 6 parts — max évalué sur les durées réelles (enveloppe / Σ tests) :")
    for k, v in sortie["psql_6_parts"].items():
        if not (isinstance(v, dict) and "max_s" in v):
            continue
        print(f"  {k:12s} max={v['max_s']/60:5.1f} / {v['max_somme_s']/60:5.1f} min  "
              f"parts={[round(p['verite_s']/60,1) for p in v['parts']]}  (repli {v['repli']})")
    print(f"  C couvre {sortie['psql_6_parts']['couverture_C']['complets']}/"
          f"{sortie['psql_6_parts']['couverture_C']['total_modules']} modules")
    print("\npsycopg 4 parts sous mesure — max évalué sur les durées sous mesure :")
    for k in ("A0_statique", "A1_carte", "A1_precedente", "A1_plein"):
        v = sortie["psy_4_parts"][k]
        print(f"  {k:14s} max={v['max_s']/60:5.1f} / {v['max_somme_s']/60:5.1f} min  "
              f"parts={[round(p['verite_s']/60,1) for p in v['parts']]}  (repli {v['repli']})")
    print(f"  poids carte : {sortie['psy_4_parts']['modules_poids_carte']}, "
          f"précédente : {sortie['psy_4_parts']['modules_poids_precedente']}/{len(modules)}")
    print("\nmesure psql pilote (45 modules) en 2 parts :")
    v = sortie["mesure_psql_2_parts"]
    print(f"  actuel 1 part = {v['actuel_1_part_s']/60:.1f} enveloppe / "
          f"{v['actuel_1_part_somme_s']/60:.1f} Σ min")
    for k in ("B0_statique", "B1_carte"):
        print(f"  {k:12s} max={v[k]['max_s']/60:5.1f} / {v[k]['max_somme_s']/60:5.1f} min  "
              f"parts={[round(p['verite_s']/60,1) for p in v[k]['parts']]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
