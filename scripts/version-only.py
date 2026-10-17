#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Une PR qui ne change QUE le numéro de version, ou que de la
documentation ? (lots L157, L161a)

    scripts/version-only.py [BASE [TÊTE]]

Compare deux commits (défaut : `HEAD^1` et `HEAD`, le commit de fusion d'une
PR que `actions/checkout` extrait, face à la branche cible) et écrit
`version_only`, `docs_only` (`true` ou `false`) et `doc_readers` sur la
sortie standard (format de `$GITHUB_OUTPUT`) ; la raison va sur la sortie
d'erreur.

Vrai seulement si le seul fichier changé est `pyproject.toml` et que la
seule ligne changée y est `version = "…"` (une ligne retirée, une ajoutée).
La CI saute alors la suite : elle a déjà tourné sur `main` pour ce contenu
(.github/workflows/tests.yml, job « changements »). Au moindre doute
(commit introuvable, autre fichier, autre ligne), c'est faux : la suite
tourne.

Documentation seule (L161a) : chaque fichier changé est sous `docs/` ou
`site/`, ou un `*.md` hors de `src/`, `tests/`, `.github/` et `examples/`.
La CI ne passe alors que les tests qui lisent ces fichiers, déclarés dans
`tests/parts/lecteurs-doc.txt` (`doc_readers`), sans base.
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys

VERSION_FILE = "pyproject.toml"
DOC_DIRS = ("docs/", "site/")
# examples/ : le canon d'exemple, lu par test_canon sur base
CODE_DIRS = ("src/", "tests/", ".github/", "examples/")
READERS_FILE = (pathlib.Path(__file__).resolve().parent.parent
                / "tests" / "parts" / "lecteurs-doc.txt")
_VERSION_LINE = re.compile(r'^version\s*=\s*"[^"\s]+"\s*$')


def version_only(names: list[str], patch: str) -> tuple[bool, str]:
    """(vrai, raison) depuis la liste des fichiers changés et le diff sans
    contexte (`git diff -U0`) de `pyproject.toml`."""
    names = [n for n in names if n.strip()]
    if names != [VERSION_FILE]:
        return False, "fichiers changés : %s" % (", ".join(names) or "aucun")
    removed, added = [], []
    for line in patch.splitlines():
        if line.startswith(("+++", "---")):
            continue
        if line.startswith("+"):
            added.append(line[1:])
        elif line.startswith("-"):
            removed.append(line[1:])
    if len(removed) != 1 or len(added) != 1:
        return False, "%d ligne(s) retirée(s), %d ajoutée(s) dans %s" % (
            len(removed), len(added), VERSION_FILE)
    for line in removed + added:
        if not _VERSION_LINE.match(line.strip()):
            return False, "ligne changée hors version : %r" % line.strip()
    if removed[0].strip() == added[0].strip():
        return False, "version inchangée"
    return True, "seul le numéro de version change (%s → %s)" % (
        removed[0].split("=", 1)[1].strip(), added[0].split("=", 1)[1].strip())


def is_doc(name: str) -> bool:
    """Un fichier de documentation, qu'aucun code du paquet ne lit."""
    if name.startswith(DOC_DIRS):
        return True
    return name.endswith(".md") and not name.startswith(CODE_DIRS)


def docs_only(names: list[str]) -> tuple[bool, str]:
    """(vrai, raison) : chaque fichier changé est de la documentation."""
    names = [n for n in names if n.strip()]
    if not names:
        return False, "aucun fichier changé"
    code = [n for n in names if not is_doc(n)]
    if code:
        return False, "hors documentation : %s" % ", ".join(code[:5])
    return True, "documentation seule (%d fichier(s))" % len(names)


def doc_readers(path: pathlib.Path = READERS_FILE) -> list[str]:
    """Modules de test qui lisent la documentation (un par ligne, `#` en
    commentaire), en noms pointés pour `python -m unittest`.

    #1609 : `tests/parts/*.txt` est fusionné en `union` (.gitattributes) : deux
    lots qui ajoutent le même lecteur au même endroit donnent une ligne
    dupliquée. L'ordre du fichier est gardé, chaque module n'est passé qu'une
    fois à `python -m unittest`."""
    names = []
    for line in path.read_text(encoding="utf-8").splitlines():
        name = line.split("#", 1)[0].strip()
        if name:
            names.append("tests." + name)
    return list(dict.fromkeys(names))


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True,
                          text=True).stdout


def main(argv: list[str]) -> int:
    base = argv[0] if argv else "HEAD^1"
    head = argv[1] if len(argv) > 1 else "HEAD"
    try:
        # --no-renames : un fichier sorti de src/ compte des deux côtés
        names = _git("diff", "--name-only", "--no-renames", base, head).splitlines()
        patch = _git("diff", "-U0", base, head, "--", VERSION_FILE)
        ok, why = version_only(names, patch)
        doc, doc_why = docs_only(names)
        readers = doc_readers() if doc else []
    except (OSError, subprocess.CalledProcessError) as exc:
        ok, why = False, "diff illisible (%s)" % exc
        doc, doc_why, readers = False, why, []
    print("version_only=%s" % ("true" if ok else "false"))
    print("docs_only=%s" % ("true" if doc else "false"))
    print("doc_readers=%s" % " ".join(readers))
    print("version-only : %s" % why, file=sys.stderr)
    print("docs-only : %s" % doc_why, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
