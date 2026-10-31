#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""#2542 : les contrôles statiques d'ameesh, passés avant le gel d'un lot.

`ameesh work milestone <lot> frozen --sha S` (`ameesh.freeze_checks`) lance ce
script à la racine de l'arbre construit du commit S, jamais dans un arbre de
travail. Un contrôle rouge refuse le gel : l'auteur le voit avant le paquet,
au lieu d'un regel et d'une heure de CI perdue (h-0900 test_1272, h-2000
test_1349 le 18/10 : une classe de test concrète importée d'un autre module,
refusée par l'attribution #1632 seulement en CI de paquet).

N'entrent ici que des contrôles que la suite exige déjà, statiques (AST ou
compilation : ni base, ni réseau) et bon marché. Chacun tourne dans son
processus, en parallèle des autres : une quinzaine de secondes en tout, le
plus long d'entre eux. Un contrôle de plus est une ligne de [`CHECKS`] ;
`tests/test_2542_freeze_checks.py` vérifie que chaque test nommé existe.

#3687 : quand le diff du lot touche la porte de fusion, les modules de contrat
de la porte ([`GATE_CONTRACTS`]) tournent aussi, et un rouge refuse le gel :
#3584 changeait `_complete_authors` sans adapter test_1374, et seule la revue
de fond l'a vu (h-25 : un regel et une CI complète perdus). Les chemins de la
porte viennent du garde (`ameesh.freeze_checks`, variable [`GATE_ENV`], lus à
la politique) ; `[]` n'ajoute rien, une variable absente ou illisible est un
doute et lance les contrats. Seules leurs classes sans base tournent (le gel
reste sans Postgres) : les variantes `PgTestCase` restent à la CI du paquet.

Usage : scripts/freeze-checks.py   (à la racine d'un arbre ameesh)
Sortie : 0 tous verts, 1 au moins un rouge, 2 hors d'un arbre ameesh.
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

#: (nom, arguments de l'interpréteur) ; un `unittest` nomme des classes de la suite
CHECKS = (
    # #2809/#2449 : refuser les paramètres du canon codés en dur avant la relecture
    ("garde mécanique #2449", ["scripts/garde-mecanique.py", "--root", "."]),
    # une erreur de syntaxe ne se voit sinon qu'à l'import, dans la part qui l'importe
    ("compilation", ["-m", "compileall", "-q", "-j", "0", "src", "scripts", "tests"]),
    # #3606 : une source du contrat de train changée sans ré-épinglage ne se
    # voyait qu'en CI de paquet (#3581, 4 h plus tard)
    ("contrat de train #3606", ["-m", "unittest", "-q",
                                "tests.test_3606_train_pins.TrainPinsTest"]),
    # #1632/#1944 : aucune classe de test concrète d'un autre module liée au module
    ("attribution #1632", ["-m", "unittest", "-q",
                           "tests.test_l161e_map.AttributionContractTest",
                           "tests.test_l161e_map.LiensTest"]),
    # L63 : l'OS ne se lit que dans ameesh.platform
    ("lint L63", ["-m", "unittest", "-q", "tests.test_l63_platform.LintTest"]),
    # #1060 : chaque type d'alerte littéral est déclaré
    ("alert types #1060", ["-m", "unittest", "-q",
                           "tests.test_1060_alert_types.AlertTypesDeclaredTest"]),
    # L161a : un module qui lit docs/ ou site/ est déclaré lecteur de la doc
    ("doc readers L161a", ["-m", "unittest", "-q",
                           "tests.test_l161a_ci_sans_code.ReadersTest"]),
    # #4521 : un alias `sys.modules` vers une implémentation de module ne peut
    # pas tomber dans une classe de relecture plus faible que son ancien chemin
    # (les `modules/dev/**` déplacés par L100 tombent au défaut léger).
    ("alias de module #4521", ["scripts/garde-alias-classe.py", "--root", "."]),
    # #4521 B/P1 auditeur : la politique épinglée lue en CI (canon privé
    # illisible sur un runner) ne doit pas être plus faible que le canon de
    # l'hôte ; une classe épinglée plus faible refuse le gel. Les règles
    # peuvent bouger sans changer la classe (B/P2 auditeur).
    ("alias de module #4521 (politique épinglée)",
     ["scripts/garde-alias-classe.py", "--root", ".",
      "--check-policy", "scripts/alias-review-classes.json"]),
)
#: #3687 : les modules de contrat de la porte de fusion (avis, auteurs, feu,
#: désignation, report des verdicts) ; seules leurs classes sans base tournent
GATE_CONTRACTS = (
    "tests.test_1374_fire",                         # porte des avis, auteurs, feu
    "tests.test_1617_review_designation",           # désignation des relecteurs
    "tests.test_1617_review_designation_pg",        # marque de désignation
    "tests.test_2273_merge_pr_required",            # checks requis hors API
    "tests.test_2394_review_gate_verdicts",         # verdicts comptés à la porte
    "tests.test_2442_verdict_carry",                # report sur tête réalignée
    "tests.test_5042_verdict_carry_adaptation",     # report sur tête adaptée
    # test_2447 (activation au gel) n'a que des classes Postgres : CI du paquet
)
#: #3687 : les chemins de la porte touchés, en JSON, posés par le garde du gel
GATE_ENV = "AMEESH_FREEZE_GATE"
#: la base des classes qui demandent Postgres (tests/support.py)
PG_BASE = "PgTestCase"
#: borne d'un contrôle (secondes) ; l'appelant borne l'ensemble
TIMEOUT = 240
#: lignes de sortie montrées pour un contrôle rouge
TAIL = 40


def _base_names(node: ast.ClassDef) -> list:
    names = []
    for base in node.bases:
        if isinstance(base, ast.Name):
            names.append(base.id)
        elif isinstance(base, ast.Attribute):
            names.append(base.attr)
    return names


def static_classes(root: str, module: str) -> list:
    """Les classes de test de `module` qui tournent sans base : publiques, une
    `…TestCase` parmi leurs bases (celles du module comprises), aucune
    [`PG_BASE`]. Un module absent ou illisible n'en a aucune."""
    path = os.path.join(root, *module.split(".")) + ".py"
    try:
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=path)
    except (OSError, SyntaxError, ValueError):
        return []
    local = {node.name: _base_names(node) for node in tree.body
             if isinstance(node, ast.ClassDef)}

    def ancestry(name, seen=()):
        found = set()
        for base in local.get(name, ()):
            found.add(base)
            if base in local and base not in seen:
                found |= ancestry(base, seen + (name,))
        return found

    out = []
    for name in local:
        bases = ancestry(name)
        if name.startswith("_") or PG_BASE in bases \
                or not any(base.endswith("TestCase") for base in bases):
            continue
        out.append("%s.%s" % (module, name))
    return out


def gate_touched(environ) -> tuple:
    """`(lancer les contrats, raison)` d'après [`GATE_ENV`] : une liste vide
    ne lance rien ; absente ou illisible, le doute les lance."""
    raw = environ.get(GATE_ENV)
    if raw is None:
        return True, "gate paths unknown"
    try:
        paths = json.loads(raw)
    except ValueError:
        return True, "gate paths unreadable"
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        return True, "gate paths unreadable"
    if not paths:
        return False, "merge gate not touched"
    shown = ", ".join(paths[:3]) + (" …" if len(paths) > 3 else "")
    return True, "merge gate touched: %s" % shown


def gate_checks(root: str, environ) -> tuple:
    """`(contrôles, raison)` : le contrôle des contrats de la porte, ou aucun."""
    touched, why = gate_touched(environ)
    if not touched:
        return (), why
    ids = [ident for module in GATE_CONTRACTS for ident in static_classes(root, module)]
    if not ids:
        return (), why + "; no database-free contract class"
    return (("contrat de la porte #3687", ["-m", "unittest", "-q", *ids]),), why


def _run(check, root: str, env: dict) -> tuple:
    name, args = check
    start = time.monotonic()
    try:
        proc = subprocess.run([sys.executable, *args], cwd=root, env=env,
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=TIMEOUT)
        code, output = proc.returncode, proc.stdout.decode("utf-8", "replace")
    except subprocess.TimeoutExpired as exc:
        code = None
        output = (exc.output or b"").decode("utf-8", "replace") \
            + "\ndid not finish in %d s" % TIMEOUT
    return name, code, time.monotonic() - start, output


def main(argv=None) -> int:
    root = os.getcwd()
    if not (os.path.isdir(os.path.join(root, "src", "ameesh"))
            and os.path.isdir(os.path.join(root, "tests"))):
        print("freeze-checks: run from the root of an ameesh tree", file=sys.stderr)
        return 2
    env = dict(os.environ, PYTHONPATH=os.path.join(root, "src"), AMEESH_REQUIRE_DB="0")
    env.pop(GATE_ENV, None)
    gate, why = gate_checks(root, os.environ)
    print("--  gate contracts: %s" % ("run (%s)" % why if gate else "none (%s)" % why))
    checks = CHECKS + gate
    with ThreadPoolExecutor(max_workers=max(1, len(checks))) as pool:
        results = list(pool.map(lambda check: _run(check, root, env), checks))
    red = 0
    for name, code, seconds, output in results:
        print("%-3s %s (%.1f s)" % ("ok" if code == 0 else "RED", name, seconds))
        if code != 0:
            red += 1
            lines = output.rstrip().splitlines()[-TAIL:]
            print("\n".join("    " + line for line in lines))
    return 1 if red else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
