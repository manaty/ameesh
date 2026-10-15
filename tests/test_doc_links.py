# SPDX-License-Identifier: AGPL-3.0-only
"""#775 : le vérificateur de liens et d'ancres de `docs/` (scripts/doc-links.py).

Sans base : la règle de slug GitHub (accents, tirets, doublons), les cas cassés
sur une arborescence temporaire, et la garde sur le `docs/` du dépôt. Ce module
lit `docs/` : il est déclaré dans `tests/parts/lecteurs-doc.txt` (L161a).
"""
from __future__ import annotations

import importlib.util
import pathlib
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("doc_links", REPO / "scripts" / "doc-links.py")
doc_links = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(doc_links)


class SlugTest(unittest.TestCase):
    """GitHub's slug rules, on the headings that had gone stale."""

    def test_accent_and_em_dash(self):
        self.assertEqual(doc_links.slug("Plusieurs canons (L42, L43 — décision 0031)"),
                         "plusieurs-canons-l42-l43--décision-0031")
        self.assertEqual(doc_links.slug("Updating ameesh to L35 — working directories"),
                         "updating-ameesh-to-l35--working-directories")

    def test_inline_markup_dropped(self):
        self.assertEqual(doc_links.slug("`ameesh work plan` (L96)"), "ameesh-work-plan-l96")
        self.assertEqual(doc_links.slug("**Gras** et [lien](x.md)"), "gras-et-lien")

    def test_duplicates_are_suffixed(self):
        with tempfile.TemporaryDirectory() as tmp:
            page = pathlib.Path(tmp) / "page.md"
            page.write_text("# Foo\n\n## Foo\n\n### Foo\n", encoding="utf-8")
            self.assertEqual(doc_links.headings(page), {"foo", "foo-1", "foo-2"})


class BrokenLinksTest(unittest.TestCase):
    """One case per rule, on a throwaway tree."""

    def tree(self, text: str) -> tuple[pathlib.Path, pathlib.Path]:
        tmp = pathlib.Path(tempfile.mkdtemp())
        (tmp / "docs").mkdir()
        (tmp / "docs" / "target.md").write_text("# Section\n\n## Sous-section\n",
                                                encoding="utf-8")
        (tmp / "docs" / "page.md").write_text(text, encoding="utf-8")
        return tmp, tmp / "docs"

    def reasons(self, text: str) -> list[str]:
        repo, docs = self.tree(text)
        return [reason for _, _, _, reason in doc_links.broken_links(docs, repo)]

    def test_good_links_pass(self):
        self.assertEqual(self.reasons(
            "[fichier](target.md) et [ancre](target.md#sous-section) et [#section](#section)\n"
            "# Section\n"), [])

    def test_missing_file_and_anchor(self):
        self.assertEqual(self.reasons("[absent](nowhere.md)\n"), ["fichier absent"])
        self.assertEqual(self.reasons("[ancre](target.md#absente)\n"), ["ancre absente"])

    def test_directory_link_is_valid(self):
        self.assertEqual(self.reasons("[dossier](.)\n"), [])

    def test_external_links_ignored(self):
        self.assertEqual(self.reasons(
            "[web](https://example.org/x#y) [mail](mailto:a@b.c) [rel](//example.org)\n"), [])

    def test_code_is_not_read(self):
        self.assertEqual(self.reasons(
            "```\n[absent](nowhere.md)\n```\n\nUn `[absent](nowhere.md)` inline.\n"), [])

    def test_reference_without_definition(self):
        self.assertEqual(self.reasons("Voir [la cible][ref].\n"),
                         ["référence sans définition"])
        self.assertEqual(self.reasons("Voir [la cible][ref].\n\n[ref]: target.md\n"), [])

    def test_absolute_link_from_the_repository_root(self):
        self.assertEqual(self.reasons("[absolu](/docs/target.md)\n"), [])
        self.assertEqual(self.reasons("[absolu](/docs/nowhere.md)\n"), ["fichier absent"])

    def test_duplicate_heading_anchor(self):
        self.assertEqual(self.reasons("[deuxième](target.md#sous-section-1)\n"),
                         ["ancre absente"])


class DocsTreeTest(unittest.TestCase):
    """The guard itself: the repository's docs/ has no broken link."""

    def test_docs_are_clean(self):
        problems = doc_links.broken_links(REPO / "docs", REPO)
        self.assertEqual([(str(p), n, t, r) for p, n, t, r in problems], [])


if __name__ == "__main__":
    unittest.main()
