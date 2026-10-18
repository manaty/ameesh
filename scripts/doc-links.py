#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Checker of the internal links and anchors of `docs/` (#775).

`docs/` is read on GitHub (the MkDocs site is built from `site/docs/`): its
relative links and `#anchors` must resolve there. Nothing checked them until
now — two anchors had been dead since L156/L42 (#774). This script walks a
documentation tree and reports:

- a link whose target file does not exist;
- a `#anchor` that no heading of the target file produces, under GitHub's
  slug rules (lowercase, punctuation removed, spaces to hyphens, duplicates
  suffixed `-1`, `-2`…);
- a reference-style use `[text][id]` whose `[id]:` definition is missing.

External links (`http:`, `mailto:`…), links inside fenced, indented or inline
code, and links to an existing directory (GitHub renders its tree page) are
not reported. Headings are ATX (`# title`) or setext (`title` underlined by
`===`/`---`); HTML anchors `<a id="…">`/`<a name="…">` also count. A fenced
block closes only on its own marker character and on a run at least as long
as the opening one, so a `~~~` inside a ``` block stays code.

Usage:

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

# [text](target "title") — also captures images ![alt](target)
INLINE_LINK = re.compile(r"\[[^\]]*\]\(\s*<?([^)>\s]+)>?(?:\s+[\"'][^\"']*[\"'])?\s*\)")
# [text][id]: use of a reference
REFERENCE_USE = re.compile(r"\[[^\]]*\]\[([^\]]+)\]")
# [id]: target "title" — definition of a reference (footnotes `[^1]:` excluded
# at use site, they are prose, not a link target)
REFERENCE_DEF = re.compile(r"^\s{0,3}\[([^\]]+)\]:\s*<?(\S+?)>?(?:\s+[\"'(].*)?$")
# ATX heading: a closing `#` run needs a space before it (`# C#` keeps its `#`).
HEADING = re.compile(r"^(#{1,6})\s+(.*?)(?:\s+#+)?\s*$")
# Setext heading underline: `===` (h1) or `---` (h2).
SETEXT = re.compile(r"^ {0,3}(?:=+|-+)\s*$")
# Fence opening/closing: up to 3 spaces, then a run of at least 3 backticks or
# tildes, then an optional info string (opening) or only spaces (closing).
FENCE = re.compile(r"^( {0,3})(`{3,}|~{3,})(.*)$")
# Indented code block (CommonMark: 4 spaces or a tab).
INDENTED_CODE = re.compile(r"^(?: {4,}|\t)")
INLINE_CODE = re.compile(r"`+[^`]*`+")
# An explicit HTML anchor GitHub exposes as an id (`<a id="x">`, `<a name="x">`).
HTML_ANCHOR = re.compile(r"<a\s+[^>]*?(?:id|name)\s*=\s*[\"']([^\"']+)[\"']", re.I)
# A line that cannot be the text of a setext heading.
LIST_ITEM = re.compile(r"^\s*(?:[-+*]|\d+[.)])\s+")
# Anything with a scheme (http:, mailto:, data:…) or starting with // is external.
EXTERNAL = re.compile(r"^(?:[a-zA-Z][a-zA-Z0-9+.-]*:|//)")

MISSING_FILE = "missing file"
MISSING_ANCHOR = "missing anchor"
MISSING_DEFINITION = "reference without definition"


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


def fence_state(line: str, opened: tuple[str, int] | None) -> tuple[str, int] | None:
    """Update the fenced-code state on `line`.

    `opened` is `(marker character, marker length)` of the opening fence, or
    None outside a fence. An opening fence is a run of at least three
    backticks or tildes; it closes only on the same character with a run at
    least as long, and only spaces may follow the closing run (#1406/N1).
    """
    match = FENCE.match(line)
    if opened is None:
        if match is None:
            return None
        marker, info = match.group(2), match.group(3)
        if marker[0] == "`" and "`" in info:
            return None  # an inline code span, not a fence
        return marker[0], len(marker)
    if match is None:
        return opened
    marker, rest = match.group(2), match.group(3)
    if marker[0] == opened[0] and len(marker) >= opened[1] and not rest.strip():
        return None
    return opened


def _prose_lines(lines):
    """Yield `(line number, line)`, with fenced/indented code blanked out.

    A blank line in place of each code line also resets the setext context of
    the caller: a `---` after a code block is a thematic break, not the
    underline of a paragraph written before the block (#1406/N2).
    """
    opened: tuple[str, int] | None = None
    for number, line in enumerate(lines, 1):
        if opened is not None:
            opened = fence_state(line, opened)
            yield number, ""
            continue
        if INDENTED_CODE.match(line):
            yield number, ""
            continue
        state = fence_state(line, None)
        if state is not None:
            opened = state
            yield number, ""
            continue
        yield number, line


def _setext_text(line: str) -> bool:
    """True when `line` may be the text of a setext heading."""
    text = line.strip()
    return bool(
        text
        and not text.startswith(("#", ">", "|", "<"))
        and not LIST_ITEM.match(text)
        and not SETEXT.match(text)
        and not REFERENCE_DEF.match(line)
    )


def _add_anchor(anchors: set[str], seen: dict[str, int], text: str) -> None:
    base = slug(text)
    rank = seen.get(base, 0)
    seen[base] = rank + 1
    anchors.add(base if rank == 0 else f"{base}-{rank}")


def headings(path: pathlib.Path) -> set[str]:
    """Every anchor GitHub produces for the headings and HTML anchors of `path`."""
    anchors: set[str] = set()
    seen: dict[str, int] = {}
    previous = ""
    text = path.read_text(encoding="utf-8", errors="replace").splitlines()
    for _, line in _prose_lines(text):
        for match in HTML_ANCHOR.finditer(line):
            anchors.add(match.group(1))
        match = HEADING.match(line)
        if match:
            _add_anchor(anchors, seen, match.group(2))
            previous = ""
            continue
        if previous and SETEXT.match(line):
            _add_anchor(anchors, seen, previous)
            previous = ""
            continue
        previous = line.strip() if _setext_text(line) else ""
    return anchors


def links_of(path: pathlib.Path):
    """Yield `(line number, target)` of every link a reader can click.

    Reference definitions are collected first: a `[text][id]` use may precede
    its `[id]: target` definition (the usual layout puts them at the end).
    Footnote definitions (`[^1]: …`) are prose, not link definitions.
    """
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    definitions: dict[str, str] = {}
    for _, line in _prose_lines(lines):
        definition = REFERENCE_DEF.match(line)
        if definition and not definition.group(1).startswith("^"):
            definitions[definition.group(1).lower()] = definition.group(2)
    for number, line in _prose_lines(lines):
        definition = REFERENCE_DEF.match(line)
        if definition and not definition.group(1).startswith("^"):
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
                problems.append((path, number, target, MISSING_DEFINITION))
                continue
            file_part, _, anchor = target.partition("#")
            file_part, anchor = unquote(file_part), unquote(anchor).lower()
            if file_part.startswith("/"):
                destination = (repo / file_part.lstrip("/")).resolve()
            else:
                destination = (path.parent / file_part).resolve()
            if file_part and not destination.exists():
                problems.append((path, number, target, MISSING_FILE))
                continue
            if not anchor:
                continue
            if not file_part:
                destination = path
            if destination.is_dir() or destination.suffix != ".md":
                continue
            found = anchors.setdefault(destination, headings(destination))
            if anchor not in found:
                problems.append((path, number, target, MISSING_ANCHOR))
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
    lines = [f"{root}: {files} file(s), {len(problems)} broken link(s)"]
    for path, number, target, reason in problems:
        lines.append(f"  {path}:{number}: {target} ({reason})")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=str(DOCS),
                        help="documentation tree to check (default: docs/)")
    parser.add_argument("--repo", default=str(REPO),
                        help="repository root, for absolute links (default: the repository)")
    parser.add_argument("--json", action="store_true", help="JSON report")
    args = parser.parse_args(argv)
    root, repo = pathlib.Path(args.root), pathlib.Path(args.repo)
    if not root.is_dir():
        print(f"doc-links: {root} is not a directory", file=sys.stderr)
        return 2
    problems = broken_links(root, repo)
    print(report(root, problems, args.json))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
