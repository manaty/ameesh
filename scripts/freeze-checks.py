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

Usage : scripts/freeze-checks.py   (à la racine d'un arbre ameesh)
Sortie : 0 tous verts, 1 au moins un rouge, 2 hors d'un arbre ameesh.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

#: (nom, arguments de l'interpréteur) ; un `unittest` nomme des classes de la suite
CHECKS = (
    # une erreur de syntaxe ne se voit sinon qu'à l'import, dans la part qui l'importe
    ("compilation", ["-m", "compileall", "-q", "-j", "0", "src", "scripts", "tests"]),
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
)
#: borne d'un contrôle (secondes) ; l'appelant borne l'ensemble
TIMEOUT = 240
#: lignes de sortie montrées pour un contrôle rouge
TAIL = 40


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
    with ThreadPoolExecutor(max_workers=len(CHECKS)) as pool:
        results = list(pool.map(lambda check: _run(check, root, env), CHECKS))
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
