# SPDX-License-Identifier: AGPL-3.0-only
"""#4259 : aucun littéral propre à un mesh client dans src/ameesh/**.

Règle owner du 28/10/2026 (canon processus/lots.md §7) : ameesh est
générique ; une valeur propre à un client (organisation, dépôt, sigle,
personne) vient de sa configuration, jamais du code. Les motifs refusés sont
lus dans ``tests/client_literals.txt`` pour pouvoir ajouter un client sans
toucher à la garde.

Hors garde : les commentaires Python (traces d'incident, pas des valeurs) et
les migrations SQL. Les chaînes, docstrings comprises, et les noms sont lus.
"""
from __future__ import annotations

import io
import os
import re
import tokenize
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src", "ameesh")
PATTERNS_FILE = os.path.join(ROOT, "tests", "client_literals.txt")
EXCLUDED_DIRS = ("migrations", "__pycache__")


def load_patterns(path: str = PATTERNS_FILE) -> list[re.Pattern]:
    patterns = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            text = line.strip()
            if text and not text.startswith("#"):
                patterns.append(re.compile(text, re.I))
    return patterns


def _python_segments(source: str):
    """(ligne, texte) de chaque jeton lu : tout sauf les commentaires."""
    tokens = tokenize.generate_tokens(io.StringIO(source).readline)
    for token in tokens:
        if token.type in (tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE,
                          tokenize.INDENT, tokenize.DEDENT,
                          tokenize.ENDMARKER):
            continue
        yield token.start[0], token.string


def _text_segments(source: str):
    for number, line in enumerate(source.splitlines(), 1):
        yield number, line


def scan_source(source: str, *, python: bool, patterns) -> list[tuple[int, str]]:
    segments = _python_segments(source) if python else _text_segments(source)
    hits = []
    for number, text in segments:
        for pattern in patterns:
            match = pattern.search(text)
            if match:
                hits.append((number, match.group(0)))
    return hits


def scan_tree(root: str = SRC, patterns=None) -> list[str]:
    patterns = load_patterns() if patterns is None else patterns
    findings = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDED_DIRS)
        for name in sorted(filenames):
            path = os.path.join(dirpath, name)
            try:
                with open(path, encoding="utf-8") as handle:
                    source = handle.read()
            except UnicodeDecodeError:
                continue
            rel = os.path.relpath(path, ROOT)
            for number, literal in scan_source(
                    source, python=name.endswith(".py"), patterns=patterns):
                findings.append("%s:%d: %r" % (rel, number, literal))
    return findings


class ClientLiteralsGuard(unittest.TestCase):

    def test_aucun_litteral_client_dans_le_code(self):
        findings = scan_tree()
        self.assertEqual(
            findings, [],
            "littéraux propres à un mesh client dans src/ameesh (#4259) : "
            "les mettre en configuration\n" + "\n".join(findings))

    def test_fichier_de_motifs(self):
        patterns = load_patterns()
        self.assertTrue(patterns)
        texts = {p.pattern for p in patterns}
        self.assertIn("thepackengers", texts)

    def test_garde_rouge_sur_un_litteral_ajoute(self):
        patterns = load_patterns()
        for source in ('REPO = "thepackengers/home"\n',
                       'AUTHORITY = "ima"\n',
                       'def f():\n    """Lit le depot TPK."""\n',
                       'x = f"{a}/issue-manager-agent"\n'):
            with self.subTest(source=source):
                self.assertTrue(scan_source(source, python=True,
                                            patterns=patterns))
        self.assertTrue(scan_source("<p>ThePackengers</p>\n", python=False,
                                    patterns=patterns))

    def test_commentaires_et_mots_voisins_admis(self):
        patterns = load_patterns()
        source = ("# incident TPK : 62/65 agents IMA en pause\n"
                  "x = 1  # IMA\n"
                  "image = 'imagination'\n"
                  "maxima = 'minima'\n")
        self.assertEqual(scan_source(source, python=True, patterns=patterns),
                         [])

    def test_migrations_hors_garde(self):
        self.assertIn("migrations", EXCLUDED_DIRS)


if __name__ == "__main__":
    unittest.main()
