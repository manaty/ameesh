# SPDX-License-Identifier: AGPL-3.0-only
"""Découpage de la documentation en passages (titre, section, lien public).

* `site/docs/**.md` → pages MkDocs publiées sous `SITE_BASE` (`index.md` → `…/`,
  `a/b.md` → `…/a/b/`), ancre calculée comme le fait l'extension `toc` ;
* `docs/design/**.md` → liens GitHub (`DESIGN_BASE`), ancre à la façon GitHub.

Rien n'est inventé : un passage est un morceau de texte de la page, débarrassé
des commentaires HTML et de la syntaxe des liens, borné en taille.
"""
from __future__ import annotations

import os
import re
import unicodedata

SITE_BASE = "https://ameesh.org/docs/"
DESIGN_BASE = "https://github.com/manaty/ameesh/blob/main/docs/design/"
MAX_CHARS = 1400

_HEADING = re.compile(r"^(#{1,4})\s+(.+?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_COMMENT = re.compile(r"<!--.*?-->", re.S)
_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_ATTR = re.compile(r"\s*\{[#.][^}]*\}\s*$")
_TAG = re.compile(r"</?[a-zA-Z][^>]*>")


def mkdocs_slug(title: str) -> str:
    """Comme `markdown.extensions.toc.slugify` (réglage par défaut de MkDocs)."""
    value = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^\w\s-]", "", value).strip().lower()
    return re.sub(r"[-\s]+", "-", value)


def github_slug(title: str) -> str:
    value = title.strip().lower()
    value = re.sub(r"[^\w\- ]", "", value, flags=re.UNICODE)
    return value.replace(" ", "-")


def _clean_heading(text: str) -> str:
    text = _ATTR.sub("", text)
    text = _LINK.sub(r"\1", text)
    return text.replace("`", "").replace("*", "").strip()


def _clean_body(text: str) -> str:
    text = _COMMENT.sub("", text)
    text = _LINK.sub(r"\1", text)
    text = _TAG.sub("", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def page_url(rel: str, source: str) -> str:
    if source == "design":
        return DESIGN_BASE + rel
    path = rel[:-3]  # .md
    if path == "index":
        return SITE_BASE
    if path.endswith("/index"):
        return SITE_BASE + path[: -len("index")]
    return SITE_BASE + path + "/"


def _split(text: str, limit: int) -> list[str]:
    """Coupe aux paragraphes ; un paragraphe trop long est coupé aux lignes."""
    parts: list[str] = []
    current = ""
    for para in re.split(r"\n\s*\n", text):
        pieces = [para]
        if len(para) > limit:
            pieces, buf = [], ""
            for line in para.splitlines():
                if buf and len(buf) + len(line) + 1 > limit:
                    pieces.append(buf)
                    buf = ""
                buf = (buf + "\n" + line) if buf else line[:limit * 2]
            if buf:
                pieces.append(buf)
        for piece in pieces:
            if current and len(current) + len(piece) + 2 > limit:
                parts.append(current)
                current = ""
            current = (current + "\n\n" + piece) if current else piece
    if current.strip():
        parts.append(current)
    return parts


def passages_of(markdown: str, rel: str, source: str) -> list[dict]:
    """Passages d'une page : un par section (`#`…`####`), recoupés à `MAX_CHARS`."""
    base = page_url(rel, source)
    slug = github_slug if source == "design" else mkdocs_slug
    lines = markdown.replace("\r\n", "\n").split("\n")
    if lines and lines[0].strip() == "---":  # front matter
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                lines = lines[i + 1:]
                break
    title = ""
    sections: list[tuple[str, str, list[str]]] = []  # (titre de section, ancre, lignes)
    current: tuple[str, str, list[str]] = ("", "", [])
    in_fence = False
    for line in lines:
        if _FENCE.match(line):
            in_fence = not in_fence
            current[2].append(line)
            continue
        m = None if in_fence else _HEADING.match(line)
        if m:
            text = _clean_heading(m.group(2))
            if len(m.group(1)) == 1 and not title:
                title = text
                continue
            sections.append(current)
            current = (text, slug(text), [])
            continue
        current[2].append(line)
    sections.append(current)
    if not title:
        title = os.path.splitext(os.path.basename(rel))[0].replace("-", " ")
    out: list[dict] = []
    for section, anchor, body in sections:
        text = _clean_body("\n".join(body))
        if len(text) < 40:
            continue
        url = base + ("#" + anchor if anchor else "")
        for n, chunk in enumerate(_split(text, MAX_CHARS)):
            out.append({
                "id": f"{source}:{rel}#{anchor}:{n}",
                "title": title,
                "section": section,
                "url": url,
                "text": chunk,
                "source": source,
            })
    return out


def build(site_docs: str, design_docs: str | None) -> dict:
    """L'index complet (JSON sérialisable), ordre stable."""
    passages: list[dict] = []
    roots = [(site_docs, "site")] + ([(design_docs, "design")] if design_docs else [])
    for root, source in roots:
        files = []
        for folder, _dirs, names in os.walk(root):
            for name in names:
                if name.endswith(".md"):
                    files.append(os.path.relpath(os.path.join(folder, name), root).replace(os.sep, "/"))
        # la vue d'ensemble d'abord : c'est le repli quand rien ne correspond
        files.sort(key=lambda r: (r != "index.md", r))
        for rel in files:
            with open(os.path.join(root, rel), encoding="utf-8") as fh:
                passages.extend(passages_of(fh.read(), rel, source))
    return {"v": 1, "passages": passages}
