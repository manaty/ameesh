# SPDX-License-Identifier: AGPL-3.0-only
"""L148 (stage 2c) : fonctions renommées en anglais, ancien nom en alias.

Stage 2c of the L148a plan (`docs/design/l148-plan.md`, §4) renames the free
files' French function names; the old name stays as a plain module alias
(`old = new`) for the callers not yet renamed, and leaves in stage 7.
`ameesh chat` also swaps its `--consigne` flag for `--instruction`, the old
spelling staying accepted and hidden (same recipe as stage 2a).
"""
from __future__ import annotations

import contextlib
import io
import unittest

from ameesh import (chat, identity, plan_github, progress, roadmap_dev, watch)


def parse(parser, *argv: str):
    """The namespace and what a deprecated spelling wrote on stderr."""
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        namespace = parser.parse_args(list(argv))
    return namespace, err.getvalue()


class ChatOptionTest(unittest.TestCase):

    def setUp(self):
        self.parser = chat.build_parser()

    def test_instruction_flag_and_deprecated_consigne(self):
        self.assertTrue(parse(self.parser, "--instruction")[0].consigne)
        namespace, err = parse(self.parser, "--consigne")
        self.assertTrue(namespace.consigne)
        self.assertIn("--consigne is deprecated, use --instruction", err)

    def test_help_shows_instruction_only(self):
        text = self.parser.format_help()
        self.assertIn("--instruction", text)
        self.assertNotIn("--consigne", text)


class RenamedFunctionsTest(unittest.TestCase):
    """The English definition, the French name as a plain alias."""

    PAIRS = ((identity, "chantier_of", "workstream_of"),
             (chat, "consigne", "instruction"),
             (watch, "consigne", "instruction"),
             (plan_github, "fiche_link", "card_link"),
             (progress, "_jalons_de_lot", "_work_item_milestones"),
             (progress, "_jalons_declares", "_declared_milestones"),
             (roadmap_dev, "jalon", "milestone"),
             (roadmap_dev, "livree", "delivered"),
             (roadmap_dev, "abandonnee", "abandoned"),
             (roadmap_dev, "en_cours", "in_progress"),
             (roadmap_dev, "etat_generique", "generic_state"))

    def test_alias_is_the_english_function(self):
        for module, french, english in self.PAIRS:
            with self.subTest(module=module.__name__, name=french):
                self.assertIs(getattr(module, french), getattr(module, english))

    def test_defaults_are_unchanged(self):
        # The generic value vocabulary is out of #147 (decision §5.5): the
        # renamed functions still return the documented French keys.
        self.assertEqual(roadmap_dev.milestone("livree"), "merged")
        self.assertEqual(roadmap_dev.generic_state("approved"), "validee")
        self.assertTrue(roadmap_dev.delivered("merged"))
        self.assertTrue(roadmap_dev.abandoned("closed"))
        self.assertTrue(roadmap_dev.in_progress("build"))


if __name__ == "__main__":
    unittest.main()
