# SPDX-License-Identifier: AGPL-3.0-only
"""#8395 — fragments de révision du contrat module v1, pliés au paquet.

Le banc est pur : un contrat synthétique de la même forme que le vrai (en-tête
« Contract revision: **1.N, date** », liste de révisions sans ligne vide,
ancre des sections), sa surface et ``CONTRACT_VERSION``. Les tests du composeur
lui-même (deux fragments dans un paquet, fragment mal formé) sont dans
``tests/test_3807_paquet_auto.py``.
"""
from __future__ import annotations

import json
import pathlib
import tempfile
import unittest

from ameesh import contract_fragments as cf


CONTRACT = """\
# External module public contract, version 1

Contract revision: **1.11, 27 November 2026**. Scope: an
installed external module and its client processes.
<!-- the number is assigned by the packet composer -->
Revision 1.10 adds ten (#10); it is additive to 1.9.
Revision 1.11 adds eleven (#11); it is additive to 1.10.

The [IMA mapping](mapping.md) records the consumer.

## Body

Ordinary contract body.

<!-- contract-revision-sections: the packet composer folds sections here -->

## Consumer qualification and delivery

End.
"""

SURFACE = json.dumps(
    {"schema": "ameesh-module-contract-surface/1", "contract": "1.11",
     "document": "docs/api/module-contract-v1.md"},
    indent=2) + "\n"

VERSION = '''\
"""Public contract surfaces."""

#: revision of the written contract served by this package
CONTRACT_VERSION = "1.11"
'''


def fragment(lot: int, adds: str, sections: str = "") -> cf.Fragment:
    text = "adds: " + adds
    if sections:
        text += "\n\n" + sections
    return cf.parse_fragment(lot, text, "<lot %d>" % lot)


def with_paragraphs(*paragraphs: str) -> str:
    """Le contrat courant d'un lot à l'ancienne manière : paragraphes ajoutés.

    Les paragraphes restent dans le bloc de révisions (pas de ligne vide avant
    eux) ; la ligne vide qui ferme le bloc et le mapping qui suit sont gardés.
    """
    marker = "\n\nThe [IMA mapping](mapping.md) records the consumer."
    return CONTRACT.replace(marker, "\n" + "\n".join(paragraphs) + marker, 1)


class FragmentParseTest(unittest.TestCase):
    def test_a_fragment_carries_its_adds_line_and_sections(self):
        parsed = cf.parse_fragment(
            8395,
            "<!-- fragment -->\nadds: `ameesh demo` (#8395): a read; it is\n"
            "additive to {previous}.\n\n## Demo\n\nThe body.\n",
            "8395.md")
        self.assertEqual(parsed.lot, 8395)
        self.assertEqual(parsed.adds,
                         "`ameesh demo` (#8395): a read; it is\nadditive to {previous}.")
        self.assertEqual(parsed.sections, "## Demo\n\nThe body.")

    def test_a_fragment_without_adds_is_refused_naming_the_lot(self):
        with self.assertRaises(cf.FragmentError) as caught:
            cf.parse_fragment(8395, "## Section\n\ntext\n")
        self.assertIn("#8395", str(caught.exception))
        self.assertIn("adds:", str(caught.exception))

    def test_a_fragment_that_writes_its_number_is_refused(self):
        with self.assertRaises(cf.FragmentError) as caught:
            cf.parse_fragment(
                8395, "adds: Revision 1.12 adds a read; it is additive to 1.11.\n")
        self.assertIn("#8395", str(caught.exception))

    def test_fragment_filenames_are_lot_numbers(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = pathlib.Path(tmp) / cf.DIR_REL
            directory.mkdir(parents=True)
            (directory / "8395.md").write_text(
                "adds: a read.\n", encoding="utf-8")
            (directory / "README").write_text("not a fragment\n", encoding="utf-8")
            found = cf.fragment_files(tmp)
            self.assertEqual(sorted(found), [8395])
            with self.assertRaises(cf.FragmentError):
                (directory / "fragment-8400.md").write_text(
                    "adds: a read.\n", encoding="utf-8")
                cf.fragment_files(tmp)


class PlanFoldTest(unittest.TestCase):
    def fold(self, fragments, legacy=None, lots=None, date="2026-11-28",
             contract=None):
        lots = lots if lots is not None else sorted(fragments)
        return cf.plan_fold(CONTRACT, contract or CONTRACT, SURFACE, VERSION,
                            fragments, legacy or {}, lots, date)

    def test_one_fragment_gets_the_next_revision(self):
        fold = self.fold({7: fragment(
            7, "`ameesh demo --json` (#7): the demo read; it is additive to "
               "{previous}.", "## Demo\n\nThe object is `ameesh-demo/1`.")})
        self.assertEqual([(r.number, r.lot, r.kind) for r in fold.revisions],
                         [("1.12", 7, "fragment")])
        self.assertEqual(fold.header_before, "1.11")
        self.assertEqual(fold.header_after, "1.12")
        self.assertIn("Contract revision: **1.12, 28 November 2026**",
                      fold.contract)
        self.assertIn("Revision 1.12 adds `ameesh demo --json` (#7): the demo "
                      "read; it is additive to 1.11.", fold.contract)
        self.assertIn("## Demo\n\nThe object is `ameesh-demo/1`.", fold.contract)
        self.assertLess(fold.contract.index("## Demo"),
                        fold.contract.index(cf.ANCHOR))
        self.assertIn('"contract": "1.12"', fold.surface)
        self.assertIn('CONTRACT_VERSION = "1.12"', fold.version)
        self.assertEqual(fold.removed, ("docs/api/module-contract-v1.d/7.md",))

    def test_two_fragments_get_consecutive_revisions_in_manifest_order(self):
        fold = self.fold({
            7: fragment(7, "`ameesh seven` (#7); it is additive to {previous}."),
            8: fragment(8, "`ameesh eight` (#8); it is additive to {previous}."),
        }, lots=[8, 7])
        self.assertEqual([(r.number, r.lot) for r in fold.revisions],
                         [("1.12", 8), ("1.13", 7)])
        self.assertIn("additive to 1.11", fold.contract)
        self.assertIn("additive to 1.12", fold.contract)
        self.assertEqual(fold.header_after, "1.13")
        self.assertIn('"contract": "1.13"', fold.surface)
        self.assertIn('CONTRACT_VERSION = "1.13"', fold.version)

    def test_only_the_last_additive_reference_is_rewritten(self):
        """N2 (#8395) : une référence voulue à une autre révision est gardée."""
        fold = self.fold({7: fragment(
            7, "a read; additive to 1.5 for the old object; it is additive "
               "to 1.9.")})
        self.assertIn("additive to 1.5 for the old object", fold.contract)
        self.assertIn("it is additive to 1.11.", fold.contract)

    def test_an_old_style_paragraph_is_renumbered_without_a_refreeze(self):
        legacy = ("Revision 1.12 adds an old read (#12); it is additive to 1.11.",
                  "Revision 1.12 adds a second read (#13); it is additive to 1.11.")
        current = with_paragraphs(*legacy)
        fold = self.fold({}, legacy={12: (legacy[0],), 13: (legacy[1],)},
                         lots=[12, 13], contract=current)
        self.assertEqual([(r.number, r.lot, r.kind) for r in fold.revisions],
                         [("1.12", 12, "legacy"), ("1.13", 13, "legacy")])
        self.assertIn("Revision 1.13 adds a second read (#13); it is additive "
                      "to 1.12.", fold.contract)
        # le paragraphe d'origine, numéroté 1.12 par les deux lots, ne reste pas
        self.assertNotIn("Revision 1.12 adds a second", fold.contract)
        self.assertIn("Revision 1.12 adds an old read", fold.contract)

    def test_fragments_and_old_style_interleave_in_manifest_order(self):
        legacy = "Revision 1.12 adds an old read (#12); it is additive to 1.11."
        current = with_paragraphs(legacy)
        fold = self.fold({7: fragment(
            7, "`ameesh seven` (#7); it is additive to {previous}.")},
            legacy={12: (legacy,)}, lots=[12, 7], contract=current)
        self.assertEqual([(r.number, r.lot, r.kind) for r in fold.revisions],
                         [("1.12", 12, "legacy"), ("1.13", 7, "fragment")])
        self.assertIn("additive to 1.12", fold.contract)

    def test_a_fragment_for_an_uncomposed_lot_is_refused_naming_it(self):
        with self.assertRaises(cf.FragmentError) as caught:
            self.fold({7: fragment(7, "a read; it is additive to {previous}.")},
                      lots=[])
        self.assertIn("#7", str(caught.exception))

    def test_a_lot_with_both_ways_is_refused_naming_it(self):
        legacy = "Revision 1.12 adds an old read (#12); it is additive to 1.11."
        with self.assertRaises(cf.FragmentError) as caught:
            self.fold({12: fragment(12, "a read; it is additive to {previous}.")},
                      legacy={12: (legacy,)}, lots=[12],
                      contract=with_paragraphs(legacy))
        self.assertIn("#12", str(caught.exception))
        self.assertIn("ambigu", str(caught.exception))

    def test_an_unattributed_revision_paragraph_is_refused(self):
        current = with_paragraphs(
            "Revision 1.12 adds something (#99); it is additive to 1.11.")
        with self.assertRaises(cf.FragmentError) as caught_current:
            self.fold({}, lots=[], contract=current)
        self.assertIn("1.12", str(caught_current.exception))

    def test_a_missing_anchor_is_refused_naming_the_lot(self):
        broken = CONTRACT.replace(cf.ANCHOR, "no anchor")
        with self.assertRaises(cf.FragmentError) as caught:
            self.fold({7: fragment(7, "a read; it is additive to {previous}.",
                                   "## Section\n\nBody.")}, contract=broken)
        self.assertIn("#7", str(caught.exception))
        self.assertIn("ancre", str(caught.exception))

    def test_the_folded_tree_plans_no_new_revision(self):
        fold = self.fold({7: fragment(
            7, "a read; it is additive to {previous}.", "## Section\n\nBody.")})
        again = cf.plan_fold(fold.contract, fold.contract, fold.surface,
                             fold.version, {}, {}, [], "2026-11-28")
        self.assertFalse(again.changed)
        self.assertEqual(again.contract, fold.contract)
        self.assertEqual(again.surface, fold.surface)
        self.assertEqual(again.version, fold.version)

    def test_a_fragment_without_sections_only_adds_its_revision_line(self):
        fold = self.fold({7: fragment(7, "a read; it is additive to "
                                         "{previous}.")})
        self.assertNotIn("## Section", fold.contract)
        self.assertEqual(fold.contract.count("Revision 1.12 adds"), 1)


class ApplyFoldTest(unittest.TestCase):
    def test_apply_fold_writes_the_three_files_and_removes_the_fragment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            for rel, text in ((cf.CONTRACT_REL, CONTRACT),
                              (cf.SURFACE_REL, SURFACE),
                              (cf.VERSION_REL, VERSION),
                              (cf.DIR_REL + "/7.md",
                               "adds: a read; it is additive to {previous}.\n")):
                path = root / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            fold = cf.plan_fold(
                CONTRACT, CONTRACT, SURFACE, VERSION,
                {7: fragment(7, "a read; it is additive to {previous}.")},
                {}, [7], "2026-11-28")
            changed = cf.apply_fold(root, fold)
            self.assertIn(cf.CONTRACT_REL, changed)
            self.assertIn(cf.SURFACE_REL, changed)
            self.assertIn(cf.VERSION_REL, changed)
            self.assertIn(cf.DIR_REL + "/7.md", changed)
            self.assertFalse((root / cf.DIR_REL / "7.md").exists())
            self.assertIn("Revision 1.12 adds", (root / cf.CONTRACT_REL)
                          .read_text(encoding="utf-8"))
            self.assertEqual(cf.read_fragments(root), {})

    def test_an_unchanged_fold_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            fold = cf.plan_fold(CONTRACT, CONTRACT, SURFACE, VERSION, {}, {}, [],
                                "2026-11-28")
            self.assertEqual(cf.apply_fold(tmp, fold), [])


class LegacyDiffTest(unittest.TestCase):
    def test_a_diff_yields_the_added_revision_paragraphs(self):
        diff = """\
diff --git a/docs/api/module-contract-v1.md b/docs/api/module-contract-v1.md
--- a/docs/api/module-contract-v1.md
+++ b/docs/api/module-contract-v1.md
@@ -50,0 +51,4 @@
+Revision 1.12 adds twelve (#12); it is additive to 1.11.
+It continues here.
+
+Revision 1.13 adds thirteen (#13); it is additive to 1.12.
"""
        paragraphs = cf.added_revision_paragraphs(diff)
        self.assertEqual(paragraphs, (
            "Revision 1.12 adds twelve (#12); it is additive to 1.11.\n"
            "It continues here.",
            "Revision 1.13 adds thirteen (#13); it is additive to 1.12.",
        ))

    def test_a_diff_without_revision_paragraph_is_empty(self):
        diff = "+just a table row\n"
        self.assertEqual(cf.added_revision_paragraphs(diff), ())


if __name__ == "__main__":
    unittest.main()
