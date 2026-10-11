#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Lecture du flux des résultats de tests (L161b).

    scripts/ci-flux.py resume F.jsonl [F.jsonl…]   >> "$GITHUB_STEP_SUMMARY"

Le flux est écrit par `scripts/test-parts.py lancer --flux F.jsonl` : une
ligne JSON par événement, schéma `ameesh-test-event/1` (étude
docs/design/etudes/l161-tests-par-impact.md, section 3) :

* `debut` : une par part ; `pilote`, `modules` dans l'ordre d'exécution,
  `classes` (P0 module de test changé, P1 touché par un fichier changé, P3
  le reste), `estimee_s` ;
* `test` : une par test terminé ; `module`, `test` (nom pointé), `classe`,
  `raison` (le fichier changé qui l'a fait passer d'abord), `statut`
  (`succes`, `echec`, `erreur`, `saute`, `echec_attendu`,
  `succes_inattendu`), `duree_s`, et pour un échec `extrait` (40 lignes au
  plus) et `rejouer` (la commande qui le relance seul) ;
* `fin` : une par part ; `statut` (`succes`, `echec`, `interrompu`),
  `comptes` par statut, `duree_s` ;
* `impacted` (L161f, parts run from a plan only): once the P0 and P1
  modules of the part are done; `statut` (`vert`, `rouge`), `modules`,
  `rouges` (impacted modules that failed). When every part says `vert`, the
  impacted tests are green: the provisional green
  (`scripts/test-plan.py leaks`).

Chaque ligne porte aussi `v`, `schema`, `type`, `ts` (UTC) et le contexte
de la CI : `depot`, `tete` (commit poussé), `fusion` (commit testé),
`execution` (numéro d'exécution GitHub), `part`.

`resume` en fait le résumé Markdown du job. Une ligne illisible (fichier
coupé par l'arrêt du job) est comptée et ignorée ; un fichier absent donne
un résumé qui le dit, jamais une erreur : l'étape tourne `if: always()`.

L161c ajoutera ici la publication en direct (check run « tests (flux) »)
et la remontée au courrier de l'agent du lot ; ce lot ne la fait pas.
"""
from __future__ import annotations

import argparse
import collections
import datetime
import json
import pathlib
import sys

SCHEMA = "ameesh-test-event/1"
ECHECS = ("echec", "erreur")
#: tests listés au plus dans le résumé, par catégorie
LIMITE = 20


def lire(chemin: str | pathlib.Path) -> tuple[list[dict], int]:
    """Les événements d'un fichier de flux et le nombre de lignes illisibles."""
    evenements, illisibles = [], 0
    for ligne in pathlib.Path(chemin).read_text(encoding="utf-8").splitlines():
        if not ligne.strip():
            continue
        try:
            ev = json.loads(ligne)
        except json.JSONDecodeError:
            illisibles += 1
            continue
        if isinstance(ev, dict) and ev.get("v") == 1:
            evenements.append(ev)
        else:
            illisibles += 1
    return evenements, illisibles


def _instant(ts):
    try:
        return datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def _duree(s: float) -> str:
    return f"{s:.1f} s" if s < 60 else f"{int(s // 60)} min {int(s % 60):02d} s"


def resume(chemins: list[str]) -> str:
    """Résumé Markdown d'un ou plusieurs fichiers de flux."""
    sortie: list[str] = []
    for chemin in chemins:
        if not pathlib.Path(chemin).is_file():
            sortie.append(f"### Flux des tests\n\nAucun flux : `{chemin}` absent "
                          "(la part n'a pas démarré).\n")
            continue
        evenements, illisibles = lire(chemin)
        debut = next((e for e in evenements if e.get("type") == "debut"), {})
        fin = next((e for e in evenements if e.get("type") == "fin"), None)
        tests = [e for e in evenements if e.get("type") == "test"]
        comptes = collections.Counter(e.get("statut", "?") for e in tests)
        part = debut.get("part") or (tests[0].get("part") if tests else "") or chemin
        if fin is None:
            etat = "inachevée (pas de ligne de fin)"
        else:
            etat = {"succes": "verte", "echec": "rouge"}.get(fin.get("statut"), fin.get("statut"))
            etat += f", {_duree(fin.get('duree_s', 0))}"
        sortie.append(f"### Tests {part} : {etat}\n")
        sortie.append(", ".join(f"{n} {s}" for s, n in sorted(comptes.items())) or "aucun test")
        if illisibles:
            sortie[-1] += f" ; {illisibles} ligne(s) illisible(s)"
        sortie.append("")
        classes = debut.get("classes", {})
        touches = [m for m in debut.get("modules", []) if classes.get(m, "P3") != "P3"]
        impacted = next((e for e in evenements if e.get("type") == "impacted"), None)
        if impacted is not None:
            when = ""
            t0, t1 = _instant(debut.get("ts")), _instant(impacted.get("ts"))
            if t0 and t1:
                when = f" after {_duree((t1 - t0).total_seconds())}"
            sortie.append(f"Impacted modules (P0, P1): "
                          f"{'green' if impacted.get('statut') == 'vert' else 'red'}{when}\n")
        if touches:
            sortie.append("Passés d'abord (touchés par la PR) : "
                          + ", ".join(f"`{m}` ({classes[m]})" for m in touches) + "\n")
        echecs = [e for e in tests if e.get("statut") in ECHECS]
        if echecs:
            sortie.append("| statut | test | durée | rejouer |\n|---|---|---|---|")
            for e in echecs[:LIMITE]:
                rejouer = f"`{e['rejouer']}`" if e.get("rejouer") else ""
                sortie.append(f"| {e['statut']} | `{e.get('test', '?')}` | "
                              f"{e.get('duree_s', 0):.1f} s | {rejouer} |")
            if len(echecs) > LIMITE:
                sortie.append(f"\n… et {len(echecs) - LIMITE} autre(s).")
            sortie.append("")
            for e in echecs[:LIMITE]:
                if e.get("extrait"):
                    sortie.append(f"<details><summary><code>{e.get('test', '?')}</code></summary>"
                                  f"\n\n```\n{e['extrait']}\n```\n</details>\n")
        lents = sorted(tests, key=lambda e: -e.get("duree_s", 0))[:5]
        if lents:
            sortie.append("Tests les plus lents : " + ", ".join(
                f"`{e.get('test', '?').removeprefix('tests.')}` {e.get('duree_s', 0):.1f} s"
                for e in lents) + "\n")
    return "\n".join(sortie) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sous = ap.add_subparsers(dest="commande", required=True)
    p = sous.add_parser("resume", help="résumé Markdown du job")
    p.add_argument("fichiers", nargs="+")
    args = ap.parse_args(argv)
    sys.stdout.write(resume(args.fichiers))
    return 0


if __name__ == "__main__":
    sys.exit(main())
