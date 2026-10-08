#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Construit l'index de passages de l'assistant à partir de la documentation.

    python3 chatbot/build_index.py                 # → chatbot/index.json
    python3 chatbot/build_index.py --out FICHIER   # ailleurs

Sources : `site/docs/` (pages publiques, liens vers https://ameesh.org/docs/)
et `docs/design/` (documents de conception en français, liens vers GitHub).
L'index n'est pas versionné : il est reconstruit à chaque déploiement, à partir
de ce qui est publié.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from ameesh_chat import indexer  # noqa: E402


def main(argv=None) -> int:
    root = os.path.dirname(HERE)
    parser = argparse.ArgumentParser(description="Index de passages de l'assistant du site.")
    parser.add_argument("--site-docs", default=os.path.join(root, "site", "docs"))
    parser.add_argument("--design-docs", default=os.path.join(root, "docs", "design"))
    parser.add_argument("--no-design", action="store_true", help="pages publiques seulement")
    parser.add_argument("--out", default=os.path.join(HERE, "index.json"))
    args = parser.parse_args(argv)
    index = indexer.build(args.site_docs, None if args.no_design else args.design_docs)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(index, fh, ensure_ascii=False, separators=(",", ":"))
    chars = sum(len(p["text"]) for p in index["passages"])
    print(f"{len(index['passages'])} passages, {chars} caractères → {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
