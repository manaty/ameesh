#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""#4094 : les compteurs de la doc sont engendrés, jamais recopiés à la main.

`docs/EXECUTEUR-MEDIEE.md` citait des totaux (opérations admises, refusées,
interface entière). Deux lots qui ajoutaient chacun une opération refusée
portaient tous deux 462/534 → 463/535 : les modifications identiques se
fusionnaient sans conflit, mais l'arbre composé comptait 464/536 et
`test_l107_executor_contract` rougissait (h-46, #3981 + #4046, un tour de
ci-full perdu). C'est le compteur que #1517 a retiré de `contract.json` en le
dérivant ; cette doc garde le même piège.

Le bloc entre `<!-- compteurs:debut -->` et `<!-- compteurs:fin -->` est la
seule copie des totaux : il est engendré depuis `contract.json` par ce script,
et `scripts/paquet-auto.py` le réécrit après composition avant les tests, avec
un commit mécanique. `tests/test_l107_executor_contract.py` refuse un bloc
périmé, donc une opération ajoutée à l'interface sans régénération rougit.

    scripts/doc-compteurs.py --check          # défaut : ne touche à rien
    scripts/doc-compteurs.py --write          # réécrit le bloc, puis affiche
    scripts/doc-compteurs.py --contract X --doc Y --write   # banc d'essai

Codes de retour : 0 bloc à jour (ou réécrit), 1 bloc périmé ou illisible,
2 entrée invalide.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ameesh.mediated_executor import contract as C  # noqa: E402

#: la doc et les marques du bloc engendré (lignes entières, HTML invisible)
DOC = "docs/EXECUTEUR-MEDIEE.md"
BEGIN = "<!-- compteurs:debut -->"
END = "<!-- compteurs:fin -->"

#: les libellés du bloc, dans l'ordre rendu ; une ligne par compteur pour
#: qu'un lot qui n'en change qu'un ne touche pas les lignes des autres
LABELS = (
    ("operations", "- opérations admises : %d"),
    ("refused", "- opérations refusées : %d"),
    ("interface", "- interface entière : %d"),
)


def counters(contract: C.Contract) -> dict[str, int]:
    """Les totaux du contrat, tous dérivés : rien n'est stocké."""
    return {
        "operations": len(contract.operations),
        "refused": sum(len(methods) for methods in contract.refused.values()),
        "interface": contract.interface_total,
    }


def render(contract: C.Contract) -> str:
    """Le contenu du bloc, sans ses marques ni saut de ligne final."""
    values = counters(contract)
    return "\n".join(line % values[key] for key, line in LABELS)


def _marker_lines(lines: list[str]) -> tuple[int, int]:
    begins = [index for index, line in enumerate(lines)
              if line.strip() == BEGIN]
    ends = [index for index, line in enumerate(lines) if line.strip() == END]
    if len(begins) != 1 or len(ends) != 1:
        raise ValueError(
            "bloc de compteurs introuvable ou dédoublé : une ligne %r et une "
            "ligne %r attendues, %d et %d trouvées"
            % (BEGIN, END, len(begins), len(ends)))
    begin, end = begins[0], ends[0]
    if end <= begin:
        raise ValueError("bloc de compteurs inversé : %r avant %r" % (END, BEGIN))
    return begin, end


def block(text: str) -> str:
    """Le contenu du bloc engendré, sans ses marques."""
    lines = text.splitlines()
    begin, end = _marker_lines(lines)
    return "\n".join(lines[begin + 1:end])


def replace_block(text: str, content: str) -> str:
    """La doc avec le bloc remplacé ; le reste du fichier est inchangé."""
    lines = text.splitlines()
    begin, end = _marker_lines(lines)
    newline = "\r\n" if "\r\n" in text else "\n"
    trailing = newline if text.endswith(("\n", "\r\n")) else ""
    kept = lines[:begin + 1] + content.splitlines() + lines[end:]
    return newline.join(kept) + trailing


def check(text: str, contract: C.Contract) -> list[str]:
    """Les défauts du bloc : vide si le bloc est la sortie de `render`."""
    try:
        current = block(text)
    except ValueError as exc:
        return [str(exc)]
    expected = render(contract)
    if current == expected:
        return []
    return ["bloc de compteurs périmé : %d ligne(s) attendue(s), %d lue(s) ; "
            "lancer scripts/doc-compteurs.py --write"
            % (len(expected.splitlines()), len(current.splitlines()))]


def load_contract(path: Path | None = None) -> C.Contract:
    """La table du paquet, ou celle d'un fichier (banc d'essai)."""
    if path is None:
        return C.load()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("contrat %s illisible : %s" % (path, exc)) from exc
    try:
        return C.parse(data)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("contrat %s invalide : %s" % (path, exc)) from exc


def _doc_path(repo: Path, name: str) -> Path:
    path = Path(name)
    return path if path.is_absolute() else repo / path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Engendre (ou contrôle) le bloc de compteurs de la doc "
                    "depuis le contrat (#4094).")
    parser.add_argument("--repo", default=".", help="racine du dépôt (défaut : .)")
    parser.add_argument("--doc", default=DOC,
                        help="doc à lire ou réécrire (défaut : %s)" % DOC)
    parser.add_argument("--contract", default=None,
                        help="table à lire (défaut : celle du paquet)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true",
                      help="réécrit le bloc s'il est périmé")
    mode.add_argument("--check", action="store_true",
                      help="contrôle sans écrire (défaut)")
    args = parser.parse_args(argv)
    repo = Path(args.repo).resolve()
    path = _doc_path(repo, args.doc)
    try:
        contract = load_contract(Path(args.contract) if args.contract else None)
    except ValueError as exc:
        print("doc-compteurs : %s" % exc, file=sys.stderr)
        return 2
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        print("doc-compteurs : %s illisible : %s" % (path, exc), file=sys.stderr)
        return 2
    problems = check(text, contract)
    if not problems:
        print("compteurs à jour : %s" % _summary(contract))
        return 0
    if not args.write:
        for problem in problems:
            print("doc-compteurs : %s" % problem, file=sys.stderr)
        return 1
    try:
        fixed = replace_block(text, render(contract))
    except ValueError as exc:
        # des marques absentes ou dédoublées ne se réécrivent pas d'office
        print("doc-compteurs : %s" % exc, file=sys.stderr)
        return 1
    try:
        path.write_text(fixed, encoding="utf-8")
    except OSError as exc:
        print("doc-compteurs : %s non réécrite : %s" % (path, exc),
              file=sys.stderr)
        return 2
    print("compteurs réécrits : %s → %s" % (path, _summary(contract)))
    return 0


def _summary(contract: C.Contract) -> str:
    values = counters(contract)
    return ("%d opérations admises, %d refusées, interface %d"
            % (values["operations"], values["refused"], values["interface"]))


if __name__ == "__main__":
    sys.exit(main())
