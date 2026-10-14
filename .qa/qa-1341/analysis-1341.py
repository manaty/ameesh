#!/usr/bin/env python3
"""#1341 — qualification des deux parts annulées au plafond (lecture seule).

Sources (toutes reproductibles par l'API GitHub, copiées sous artifacts/) :
  - plan/plan.json                       : plan du run tests 38610188942 (tête 0532faed4)
  - flux/{psql,psycopg}-N/flux.jsonl     : flux des 10 parts du run tests (sans mesure)
  - mesure-psycopg-{1..4}/               : parts sous mesure du run carte 38610188898
  - mesure-psql-1/                       : part psql pilote sous mesure, même run carte
  - durees-0532faed4.json                : table statique tests/parts/durees.json (repli)
  - tests-jobs / carte-jobs              : horaires des jobs (API)

Sortie : analysis-1341.json + tableaux lisibles.
"""
import collections
import json
import pathlib
import sys

ART = pathlib.Path(__file__).resolve().parent / "artifacts"


def load_flux(path):
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    pass
    return rows


def part_stats(rows):
    debut = next((r for r in rows if r.get("type") == "debut"), {})
    tests = [r for r in rows if r.get("type") == "test"]
    mods = collections.defaultdict(float)
    nmod = collections.Counter()
    for r in tests:
        mods[r["module"]] += r.get("duree_s", 0.0)
        nmod[r["module"]] += 1
    statuts = collections.Counter(r.get("statut") for r in tests)
    return {
        "modules_planned": len(debut.get("modules", [])),
        "modules_done": len(mods),
        "modules_missing": [m for m in debut.get("modules", []) if m not in mods],
        "tests": len(tests),
        "statuts": dict(statuts),
        "estimee_s": debut.get("estimee_s"),
        "test_s": round(sum(mods.values()), 1),
        "par_module": {m: round(v, 1) for m, v in mods.items()},
        "n_par_module": dict(nmod),
    }


def wall_seconds(jobs_file, names=None):
    """{nom du job: secondes} depuis la réponse API /jobs."""
    data = json.loads(pathlib.Path(jobs_file).read_text())
    out = {}
    for j in data["jobs"]:
        if j.get("started_at") and j.get("completed_at"):
            from datetime import datetime
            fmt = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00"))
            out[j["name"]] = round((fmt(j["completed_at"]) - fmt(j["started_at"])).total_seconds(), 1)
    return out


def split(pesees, parts):
    """Même algorithme que scripts/test-parts.py:split."""
    charges = [0.0] * parts
    contenu = [[] for _ in range(parts)]
    for m in sorted(pesees, key=lambda m: (-pesees[m], m)):
        i = min(range(parts), key=lambda i: (charges[i], i))
        charges[i] += pesees[m]
        contenu[i].append(m)
    return contenu, charges


def main():
    result = {}

    # --- run tests 38610188942 : 10 parts sans mesure -----------------------
    plan = json.loads((ART / "plan" / "plan.json").read_text())
    wall_tests = wall_seconds(ART / "tests-jobs-38610188942.json")
    tests_parts = {}
    poids_tests = {"psql": {}, "psycopg": {}}
    for pilote in ("psql", "psycopg"):
        for part in plan["parts"][pilote]:
            name = f"{pilote} {part['part']}/{part['of']}"
            rows = load_flux(ART / "flux" / f"{pilote}-{part['part']}" / "flux.jsonl")
            st = part_stats(rows)
            st["wall_s"] = wall_tests.get(name)
            st["modules_plan"] = part["modules"]
            st["estimee_plan_s"] = part["estimate_s"]
            tests_parts[name] = st
            for m, v in st["par_module"].items():
                poids_tests[pilote].setdefault(m, v)
    result["tests_run"] = {"run": 38610188942, "head": "0532faed4", "parts": tests_parts}

    # --- run carte 38610188898 : 5 parts sous mesure ------------------------
    wall_carte = wall_seconds(ART / "carte-jobs-38610188898.json")
    mesure_parts = {}
    poids_mesure = {"psql": {}, "psycopg": {}}
    for pilote, n in (("psycopg", 4), ("psql", 1)):
        for p in range(1, n + 1):
            d = ART / f"mesure-{pilote}-{p}"
            rows = load_flux(d / "flux.jsonl")
            st = part_stats(rows)
            dj = d / "durees.json"
            st["durees_json_present"] = dj.exists()
            if dj.exists():
                st["durees"] = json.loads(dj.read_text()).get(pilote, {})
            name = f"mesure {pilote} {p}/{n}"
            st["wall_s"] = wall_carte.get(name)
            mesure_parts[name] = st
            for m, v in st["par_module"].items():
                poids_mesure[pilote][m] = v
    result["carte_run"] = {"run": 38610188898, "head": "0532faed4", "parts": mesure_parts}

    # --- facteurs de surcoût mesure / sans mesure ---------------------------
    surcout = {}
    for pilote in ("psycopg", "psql"):
        communs = [m for m in poids_mesure[pilote] if m in poids_tests[pilote] and poids_tests[pilote][m] > 0.5]
        ratios = sorted(((poids_mesure[pilote][m] / poids_tests[pilote][m], m,
                          poids_mesure[pilote][m], poids_tests[pilote][m]) for m in communs), reverse=True)
        surcout[pilote] = {
            "n": len(ratios),
            "mediane": round(sorted(r for r, _, _, _ in ratios)[len(ratios) // 2], 2) if ratios else None,
            "max": round(ratios[0][0], 2) if ratios else None,
            "top": [{"module": m, "ratio": round(r, 2), "mesure_s": a, "tests_s": b} for r, m, a, b in ratios[:10]],
        }
    result["surcout"] = surcout

    # --- contrefactuels de répartition --------------------------------------
    # psql (tests, 6 parts) : rééquilibré sur les durées réelles observées
    contenu, charges = split(poids_tests["psql"], 6)
    result["rebalance_tests_psql_6"] = {
        "max_s": round(max(charges), 1),
        "parts_s": [round(c, 1) for c in charges],
        "actuel_max_s": max(v["test_s"] for k, v in tests_parts.items() if k.startswith("psql")),
    }
    # psycopg (mesure, 4 parts) : rééquilibré sur les durées sous mesure
    contenu, charges = split(poids_mesure["psycopg"], 4)
    result["rebalance_mesure_psycopg_4"] = {
        "max_s": round(max(charges), 1),
        "parts_s": [round(c, 1) for c in charges],
        "actuel_max_s": max(v["test_s"] for k, v in mesure_parts.items() if k.startswith("mesure psycopg")),
    }
    # psql (mesure, 2 parts) : découpage de la liste pilote sous mesure
    ref = mesure_parts["mesure psql 1/1"].get("durees") or poids_mesure["psql"]
    contenu, charges = split(ref, 2)
    result["rebalance_mesure_psql_2"] = {
        "max_s": round(max(charges), 1),
        "parts_s": [round(c, 1) for c in charges],
        "actuel_s": mesure_parts["mesure psql 1/1"]["test_s"],
    }

    out = pathlib.Path(__file__).resolve().parent / "analysis-1341.json"
    out.write_text(json.dumps(result, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")

    # --- affichage ----------------------------------------------------------
    print("== run tests 38610188942 (sans mesure) ==")
    print(f'{"part":12s} {"mur_m":>6s} {"plan_m":>6s} {"test_m":>6s} {"mod":>7s} {"tests":>6s} {"statuts":>22s}')
    for name, st in tests_parts.items():
        print(f'{name:12s} {(st["wall_s"] or 0)/60:6.1f} {(st["estimee_plan_s"] or 0)/60:6.1f} '
              f'{st["test_s"]/60:6.1f} {st["modules_done"]:3d}/{st["modules_planned"]:<3d} {st["tests"]:6d} {str(st["statuts"]):>22s}')
    print("\n== run carte 38610188898 (sous mesure) ==")
    for name, st in mesure_parts.items():
        print(f'{name:20s} mur={(st["wall_s"] or 0)/60:5.1f}m test={st["test_s"]/60:5.1f}m '
              f'mod={st["modules_done"]}/{st["modules_planned"]} tests={st["tests"]} statuts={st["statuts"]} '
              f'est={st.get("estimee_s")} durees.json={st["durees_json_present"]}')
    print("\n== surcoût mesure (ratio) ==")
    for pilote, s in surcout.items():
        print(f'{pilote}: n={s["n"]} médiane={s["mediane"]} max={s["max"]}')
        for t in s["top"][:6]:
            print(f'   {t["ratio"]:6.2f}x {t["mesure_s"]:8.1f}s vs {t["tests_s"]:7.1f}s  {t["module"]}')
    print("\n== contrefactuels de répartition ==")
    for k in ("rebalance_tests_psql_6", "rebalance_mesure_psycopg_4", "rebalance_mesure_psql_2"):
        v = result[k]
        print(f'{k}: max rééquilibré={v["max_s"]/60:.1f}m  (actuel max={v.get("actuel_max_s", v.get("actuel_s"))/60 if (v.get("actuel_max_s") or v.get("actuel_s")) else 0:.1f}m) parts={[round(x/60,1) for x in v["parts_s"]]}')
    return 0


if __name__ == "__main__":
    sys.exit(main())
