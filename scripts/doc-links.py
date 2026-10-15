#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Vérificateur des liens internes et des ancres de `docs/` (#775).

`docs/` is read on GitHub (the MkDocs site is built from `site/docs/`): its
relative links and `#anchors` must resolve there. Nothing checked them until
now — two anchors had been dead since L156/L42 (#774). This script walks a
documentation tree and reports:

- a link whose target file does not exist;
- a `#anchor` that no heading of the target file produces, under GitHub's
  slug rules (lowercase, punctuation removed, spaces to hyphens, duplicates
  suffixed `-1`, `-2`…);
- a reference-style use `[text][id]` whose `[id]:` definition is missing.

External links (`http:`, `mailto:`…), links inside fenced or inline code, and
links to an existing directory (GitHub renders its tree page) are not
reported. Usage:

    python3 scripts/doc-links.py [--root docs] [--json]

Exit code 0 when nothing is broken, 1 otherwise (the report names each link).
`tests/test_doc_links.py` runs it on the repository's `docs/`: a broken link
turns the documentation readers red (`tests/parts/lecteurs-doc.txt`).
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import unicodedata
from urllib.parse import unquote

REPO = pathlib.Path(__file__).resolve().parent.parent
DOCS = REPO / "docs"

# [texte](cible "titre") — capture aussi les images ![alt](cible)
INLINE_LINK = re.compile(r"\[[^\]]*\]\(\s*<?([^)>\s]+)>?(?:\s+[\"'][^\"']*[\"'])?\s*\)")
# [texte][id] : usage d'une référence
REFERENCE_USE = re.compile(r"\[[^\]]*\]\[([^\]]+)\]")
# [id]: cible "titre" — définition d'une référence
REFERENCE_DEF = re.compile(r"^\s{0,3}\[([^\]]+)\]:\s*<?(\S+?)>?(?:\s+[\"'(].*)?$")
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
FENCE = re.compile(r"^\s*(```|~~~)")
INLINE_CODE = re.compile(r"`+[^`]*`+")
# tout ce qui a un schéma (http:, mailto:, data:…) ou commence par // est externe
EXTERNAL = re.compile(r"^(?:[a-zA-Z][a-zA-Z0-9+.-]*:|//)")


def slug(text: str) -> str:
    """GitHub's slug of a rendered heading.

    Inline markup is dropped first (links, code, emphasis), then everything
    that is not a letter, a digit, a combining mark, `-` or `_`; spaces become
    hyphens. Accented letters are kept: `Décision` gives `décision`.
    """
    text = re.sub(r"<[^>]*>", "", text)
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\*\*?([^*]*)\*\*?", r"\1", text)
    out = []
    for char in text.strip().lower():
        if char == " ":
            out.append("-")
        elif char in "-_" or unicodedata.category(char)[0] in ("L", "N", "M"):
            out.append(char)
    return "".join(out)


def headings(path: pathlib.Path) -> set[str]:
    """Every anchor GitHub produces for the headings of `path`."""
    anchors: set[str] = set()
    seen: dict[str, int] = {}
    fenced = False
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        match = HEADING.match(line)
        if not match:
            continue
        base = slug(match.group(2))
        rank = seen.get(base, 0)
        seen[base] = rank + 1
        anchors.add(base if rank == 0 else f"{base}-{rank}")
    return anchors


def links_of(path: pathlib.Path):
    """Yield `(line number, target)` of every link a reader can click.

    Reference definitions are collected first: a `[text][id]` use may precede
    its `[id]: target` definition (the usual layout puts them at the end).
    """
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    definitions: dict[str, str] = {}
    fenced = False
    for line in lines:
        if FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        definition = REFERENCE_DEF.match(line)
        if definition:
            definitions[definition.group(1).lower()] = definition.group(2)
    fenced = False
    for number, line in enumerate(lines, 1):
        if FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        definition = REFERENCE_DEF.match(line)
        if definition:
            yield number, definition.group(2)
            continue
        line = INLINE_CODE.sub("", line)
        for target in INLINE_LINK.findall(line):
            yield number, target
        for name in REFERENCE_USE.findall(line):
            if name.lower() not in definitions:
                yield number, f"[{name}]"


def broken_links(root: pathlib.Path, repo: pathlib.Path | None = None):
    """The broken links of `root`, as `(file, line, target, reason)` tuples."""
    repo = repo or root.parent
    anchors: dict[pathlib.Path, set[str]] = {}
    problems = []
    for path in sorted(root.rglob("*.md")):
        for number, target in links_of(path):
            if EXTERNAL.match(target):
                continue
            if target.startswith("[") and target.endswith("]"):
                problems.append((path, number, target, "référence sans définition"))
                continue
            file_part, _, anchor = target.partition("#")
            file_part, anchor = unquote(file_part), unquote(anchor).lower()
            if file_part.startswith("/"):
                destination = (repo / file_part.lstrip("/")).resolve()
            else:
                destination = (path.parent / file_part).resolve()
            if file_part and not destination.exists():
                problems.append((path, number, target, "fichier absent"))
                continue
            if not anchor:
                continue
            if not file_part:
                destination = path
            if destination.is_dir() or destination.suffix != ".md":
                continue
            found = anchors.setdefault(destination, headings(destination))
            if anchor not in found:
                problems.append((path, number, target, "ancre absente"))
    return problems


def report(root: pathlib.Path, problems, as_json: bool) -> str:
    files = len(list(root.rglob("*.md")))
    if as_json:
        return json.dumps({
            "root": str(root),
            "files": files,
            "broken": [
                {"file": str(path), "line": number, "target": target, "reason": reason}
                for path, number, target, reason in problems
            ],
        }, ensure_ascii=False, indent=2)
    lines = [f"{root}: {files} fichiers, {len(problems)} lien(s) cassé(s)"]
    for path, number, target, reason in problems:
        lines.append(f"  {path}:{number}: {target} ({reason})")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=str(DOCS),
                        help="arborescence de documentation à vérifier (défaut : docs/)")
    parser.add_argument("--repo", default=str(REPO),
                        help="racine du dépôt, pour les liens absolus (défaut : le dépôt)")
    parser.add_argument("--json", action="store_true", help="rapport JSON")
    args = parser.parse_args(argv)
    root, repo = pathlib.Path(args.root), pathlib.Path(args.repo)
    if not root.is_dir():
        print(f"doc-links: {root} n'est pas un dossier", file=sys.stderr)
        return 2
    problems = broken_links(root, repo)
    print(report(root, problems, args.json))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
