#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Une PR qui ne change QUE le numéro de version ? (lot L157)

    scripts/version-only.py [BASE [TÊTE]]

Compare deux commits (défaut : `HEAD^1` et `HEAD`, le commit de fusion d'une
PR que `actions/checkout` extrait, face à la branche cible) et écrit
`version_only=true` ou `version_only=false` sur la sortie standard (format
de `$GITHUB_OUTPUT`) ; la raison va sur la sortie d'erreur.

Vrai seulement si le seul fichier changé est `pyproject.toml` et que la
seule ligne changée y est `version = "…"` (une ligne retirée, une ajoutée).
La CI saute alors la suite : elle a déjà tourné sur `main` pour ce contenu
(.github/workflows/tests.yml, job « changements »). Au moindre doute
(commit introuvable, autre fichier, autre ligne), c'est faux : la suite
tourne.
"""
from __future__ import annotations

import re
import subprocess
import sys

VERSION_FILE = "pyproject.toml"
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


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True,
                          text=True).stdout


def main(argv: list[str]) -> int:
    base = argv[0] if argv else "HEAD^1"
    head = argv[1] if len(argv) > 1 else "HEAD"
    try:
        names = _git("diff", "--name-only", base, head).splitlines()
        patch = _git("diff", "-U0", base, head, "--", VERSION_FILE)
        ok, why = version_only(names, patch)
    except (OSError, subprocess.CalledProcessError) as exc:
        ok, why = False, "diff illisible (%s)" % exc
    print("version_only=%s" % ("true" if ok else "false"))
    print("version-only : %s" % why, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
