# SPDX-License-Identifier: AGPL-3.0-only
"""#3954 — les contrats transverses qu'un lot doit exécuter.

Un contrat transverse vérifie une interface entière au lieu d'un module
précis : le contrat de l'exécuteur médié confronte `contract.json` à toutes
les opérations déclarées dans `storage.interface`, le contrat du cluster
confronte les droits de `deploy/sql` à toutes les colonnes des migrations. La
carte mesurée (L161e) ne relie pas toujours ces tests aux fichiers qu'ils
lisent : un module importé par le harnais de test avant le début du test ne
laisse aucune trace dans son contexte de couverture. Un lot qui change
`storage/interface.py` ou `contract.json` ne déclenchait donc pas
`test_l107_executor_contract`, et le rouge déterministe n'apparaissait qu'à la
CI du paquet (h-37, #3885 ; #3916 pour un test du lot).

La liste ci-dessous est **déclarée** et versionnée avec le code, comme les
déclencheurs `ALWAYS_TRIGGERS` du plan : chaque module de contrat nomme les
fichiers ou dossiers dont un changement le rend dû. Un chemin finissant par
`/` couvre le dossier et ses descendants ; tout autre chemin est un fichier
exact. Les noms de modules sont ceux de `tests/` sans préfixe.

`hits` rend les contrats dus par un change set (`git diff --name-status`) ;
`problem` dit ce qui empêche de s'y fier (module déclaré disparu), pour que
l'appelant déclare la sélection incertaine au lieu de sauter le contrat.
"""
from __future__ import annotations

#: contrat → fichiers ou dossiers dont un changement le rend dû. Trier par
#: module et, dans chaque module, garder les chemins triés (témoin #3954).
CONTRACTS = {
    "test_cluster": (
        ".dockerignore",
        "Dockerfile",
        "deploy/k8s/",
        "deploy/sql/",
        "src/ameesh/db.py",
        "src/ameesh/harness_descriptors/",
        "src/ameesh/migrations/",
        "src/ameesh/runner.py",
    ),
    "test_l107_executor_contract": (
        "docs/EXECUTEUR-MEDIEE.md",
        "src/ameesh/config.py",
        "src/ameesh/db.py",
        "src/ameesh/mediated_executor/",
        "src/ameesh/storage/interface.py",
        "src/ameesh/storage/postgres/",
        "tests/golden/mediated_executor/",
    ),
    # #4574 : la liste « toujours » et la liste pilote sont lues comme des
    # données (liste figée de test_always_list_sorted_and_known) ; la carte ne
    # relie pas un fichier texte au test qui le lit (#4256 exclu de h-06).
    "test_l161f_plan": (
        "tests/parts/",
    ),
}


def matches(path: str, pattern: str) -> bool:
    """`path` est-il le fichier `pattern`, ou sous son dossier `pattern/` ?"""
    path = str(path or "")
    pattern = str(pattern or "")
    if not path or not pattern:
        return False
    if pattern.endswith("/"):
        folder = pattern.rstrip("/")
        return path == folder or path.startswith(folder + "/")
    return path == pattern


def hits(changes) -> dict[str, str]:
    """Les contrats dus par `changes` : `{module: premier chemin changé}`.

    `changes` est la suite `(statut, chemin)` des fichiers changés, telle que
    `git diff --name-status --no-renames` la rend ; le statut A, M, D ou T
    n'importe pas : un fichier lu qui disparaît rend le contrat dû aussi. Un
    contrat n'est nommé qu'une fois, par le premier chemin qui le déclenche,
    dans l'ordre des changements (le tri est fait par l'appelant).
    """
    found: dict[str, str] = {}
    for change in changes or ():
        try:
            _status, path = change
        except (TypeError, ValueError):
            continue
        path = str(path or "")
        for module, patterns in CONTRACTS.items():
            if module in found:
                continue
            if any(matches(path, pattern) for pattern in patterns):
                found[module] = path
    return found


def problem(modules) -> str:
    """Ce qui rend la déclaration inutilisable pour `modules`, ou "".

    Un module déclaré qui n'existe plus est un contrat qu'on sauterait en
    silence : l'appelant doit déclarer la sélection incertaine (suite complète
    en CI, contrats seuls en prévalidation), jamais continuer sans lui.
    """
    known = set(modules or ())
    absent = sorted(module for module in CONTRACTS if module not in known)
    if absent:
        return ("contrat transverse déclaré sans module: %s"
                % ", ".join(absent[:3]))
    return ""
