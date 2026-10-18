# SPDX-License-Identifier: AGPL-3.0-only
"""#775: the link and anchor checker of `docs/` (scripts/doc-links.py).

No database: GitHub's slug rule (accents, dashes, duplicates), the broken
cases on a throwaway tree, and the guard on the repository's `docs/`. This
module reads `docs/`: it is declared in `tests/parts/lecteurs-doc.txt` (L161a).
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

    def test_closing_hash_needs_a_space(self):
        self.assertEqual(doc_links.HEADING.match("# C#").group(2), "C#")
        self.assertEqual(doc_links.HEADING.match("# C# ###").group(2), "C#")
        self.assertEqual(doc_links.HEADING.match("# Title ###").group(2), "Title")


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
        self.assertEqual(self.reasons("[absent](nowhere.md)\n"), ["missing file"])
        self.assertEqual(self.reasons("[ancre](target.md#absente)\n"), ["missing anchor"])

    def test_directory_link_is_valid(self):
        self.assertEqual(self.reasons("[dossier](.)\n"), [])

    def test_external_links_ignored(self):
        self.assertEqual(self.reasons(
            "[web](https://example.org/x#y) [mail](mailto:a@b.c) [rel](//example.org)\n"), [])

    def test_code_is_not_read(self):
        self.assertEqual(self.reasons(
            "```\n[absent](nowhere.md)\n```\n\nUn `[absent](nowhere.md)` inline.\n"), [])

    def test_fence_marker_is_remembered(self):
        self.assertEqual(self.reasons(
            "```\n[absent](nowhere.md)\n~~~\n[absent](nowhere.md)\n```\n"), [])
        self.assertEqual(self.reasons(
            "~~~\n[absent](nowhere.md)\n```\n[absent](nowhere.md)\n~~~\n"), [])

    def test_longer_fence_closes(self):
        self.assertEqual(self.reasons(
            "````\n[absent](nowhere.md)\n```\n[absent](nowhere.md)\n````\n"), [])

    def test_link_after_a_closed_fence_is_read(self):
        self.assertEqual(self.reasons("```\ncode\n```\n[absent](nowhere.md)\n"),
                         ["missing file"])

    def test_indented_code_is_not_read(self):
        self.assertEqual(self.reasons("    [absent](nowhere.md)\n"), [])
        self.assertEqual(self.reasons("[faux](#faux-titre)\n\n    # Faux titre\n"),
                         ["missing anchor"])

    def test_setext_headings(self):
        self.assertEqual(self.reasons("[titre](#mon-titre)\n\nMon titre\n=========\n"), [])
        self.assertEqual(self.reasons("[titre](#mon-titre)\n\nMon titre\n---------\n"), [])

    def test_fence_breaks_setext_context(self):
        self.assertEqual(self.reasons(
            "[faux](#titre)\n\nTitre\n```\ncode\n```\n---\n"), ["missing anchor"])

    def test_html_anchors(self):
        self.assertEqual(self.reasons('<a id="custom"></a>\n\n[lien](#custom)\n'), [])
        self.assertEqual(self.reasons('<a name="legacy"></a>\n\n[lien](#legacy)\n'), [])

    def test_html_anchor_requires_a_real_attribute(self):
        for text in ('<a data-id="ghost"></a>', '<a title="id=\'ghost\'"></a>',
                     '<!-- <a id="ghost"></a> -->', '`<a id="ghost"></a>`'):
            with self.subTest(text=text):
                self.assertEqual(self.reasons(text + '\n\n[link](#ghost)\n'),
                                 ["missing anchor"])

    def test_footnote_is_not_a_reference(self):
        self.assertEqual(self.reasons("[^1]: Voir.\n"), [])
        self.assertEqual(self.reasons("[^1]: Voir.\n\n[ok](target.md)\n"), [])
        self.assertEqual(self.reasons("[^1]: Voir [absent](nowhere.md).\n"),
                         ["missing file"])

    def test_reference_without_definition(self):
        self.assertEqual(self.reasons("Voir [la cible][ref].\n"),
                         ["reference without definition"])
        self.assertEqual(self.reasons("Voir [la cible][ref].\n\n[ref]: target.md\n"), [])

    def test_absolute_link_from_the_repository_root(self):
        self.assertEqual(self.reasons("[absolu](/docs/target.md)\n"), [])
        self.assertEqual(self.reasons("[absolu](/docs/nowhere.md)\n"), ["missing file"])

    def test_duplicate_heading_anchor(self):
        self.assertEqual(self.reasons("[deuxième](target.md#sous-section-1)\n"),
                         ["missing anchor"])


class DocsTreeTest(unittest.TestCase):
    """The guard itself: the repository's docs/ has no broken link."""

    def test_docs_are_clean(self):
        problems = doc_links.broken_links(REPO / "docs", REPO)
        self.assertEqual([(str(p), n, t, r) for p, n, t, r in problems], [])


if __name__ == "__main__":
    unittest.main()
