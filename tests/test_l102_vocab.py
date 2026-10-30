# SPDX-License-Identifier: AGPL-3.0-only
"""L102 T1 (#4391): the Vocabulary grammar and the active module's vocabulary.

CORE names a work item "tâche" (feminine); the dev tooling module declares
DEV ("lot", masculine), read through the registry at discovery. The core
never imports a module implementation, and the registry is not modified.
"""
from __future__ import annotations

import unittest
from unittest import mock

from ameesh import modules, vocab
from ameesh.modules import dev


class CoreVocabularyTest(unittest.TestCase):
    def test_singular_plural_and_gender(self):
        self.assertEqual(vocab.CORE.singular, "tâche")
        self.assertEqual(vocab.CORE.plural, "tâches")
        self.assertEqual(vocab.CORE.gender, "f")
        self.assertTrue(vocab.CORE.feminine)

    def test_singular_articles(self):
        self.assertEqual(vocab.CORE.definite, "la tâche")
        self.assertEqual(vocab.CORE.indefinite, "une tâche")

    def test_contractions(self):
        self.assertEqual(vocab.CORE.of, "de la tâche")
        self.assertEqual(vocab.CORE.to, "à la tâche")

    def test_plural_articles(self):
        self.assertEqual(vocab.CORE.definite_plural, "les tâches")
        self.assertEqual(vocab.CORE.indefinite_plural, "des tâches")
        self.assertEqual(vocab.CORE.of_plural, "des tâches")
        self.assertEqual(vocab.CORE.to_plural, "aux tâches")

    def test_title_forms(self):
        self.assertEqual(vocab.CORE.title, "Tâche")
        self.assertEqual(vocab.CORE.title_plural, "Tâches")

    def test_label_and_count(self):
        self.assertEqual(vocab.CORE.label(), "tâche")
        self.assertEqual(vocab.CORE.label(4391), "tâche #4391")
        self.assertEqual(vocab.CORE.label(4391, capital=True), "Tâche #4391")
        self.assertEqual(vocab.CORE.count(0), "0 tâche")
        self.assertEqual(vocab.CORE.count(1), "1 tâche")
        self.assertEqual(vocab.CORE.count(2), "2 tâches")
        self.assertEqual(vocab.CORE.noun, "tâche")
        self.assertEqual(vocab.CORE.noun_for(1), "tâche")
        self.assertEqual(vocab.CORE.noun_for(2), "tâches")

    def test_agreement(self):
        self.assertEqual(vocab.CORE.agree("stagnant"), "stagnante")
        self.assertEqual(vocab.CORE.agree("stagnant", plural=True), "stagnantes")
        self.assertEqual(vocab.CORE.agree("beau", "belle"), "belle")
        self.assertEqual(vocab.CORE.agree("stagnant", "stagnante", plural=True),
                         "stagnantes")


class DevVocabularyTest(unittest.TestCase):
    def test_the_dev_module_declares_its_vocabulary(self):
        self.assertEqual(dev.VOCABULARY, dev.DEV)
        self.assertEqual(dev.DEV.singular, "lot")
        self.assertEqual(dev.DEV.plural, "lots")
        self.assertEqual(dev.DEV.gender, "m")

    def test_masculine_articles_and_contractions(self):
        self.assertEqual(dev.DEV.definite, "le lot")
        self.assertEqual(dev.DEV.indefinite, "un lot")
        self.assertEqual(dev.DEV.of, "du lot")
        self.assertEqual(dev.DEV.to, "au lot")
        self.assertEqual(dev.DEV.definite_plural, "les lots")
        self.assertEqual(dev.DEV.indefinite_plural, "des lots")
        self.assertEqual(dev.DEV.of_plural, "des lots")
        self.assertEqual(dev.DEV.to_plural, "aux lots")

    def test_title_and_labels(self):
        self.assertEqual(dev.DEV.title, "Lot")
        self.assertEqual(dev.DEV.title_plural, "Lots")
        self.assertEqual(dev.DEV.label(4391), "lot #4391")
        self.assertEqual(dev.DEV.label(4391, capital=True), "Lot #4391")
        self.assertEqual(dev.DEV.count(2), "2 lots")
        self.assertEqual(dev.DEV.agree("stagnant"), "stagnant")
        self.assertEqual(dev.DEV.agree("stagnant", plural=True), "stagnants")


class ElisionTest(unittest.TestCase):
    def test_a_vowel_elides(self):
        idea = vocab.Vocabulary("idée", "idées", "f")
        self.assertEqual(idea.definite, "l'idée")
        self.assertEqual(idea.of, "de l'idée")
        self.assertEqual(idea.to, "à l'idée")
        self.assertEqual(idea.indefinite, "une idée")

    def test_elision_is_forced_or_forbidden(self):
        self.assertEqual(vocab.Vocabulary("heure", "heures", "f").definite, "l'heure")
        self.assertEqual(vocab.Vocabulary("hôtel", "hôtels", "m",
                                          elision=True).of, "de l'hôtel")
        self.assertEqual(vocab.Vocabulary("héros", "héros", "m",
                                          elision=False).definite, "le héros")


class VocabularyValidationTest(unittest.TestCase):
    def test_a_word_is_required(self):
        with self.assertRaises(ValueError):
            vocab.Vocabulary("", "lots", "m")
        with self.assertRaises(ValueError):
            vocab.Vocabulary("lot", "", "m")
        with self.assertRaises(ValueError):
            vocab.Vocabulary(" lot", "lots", "m")

    def test_gender_and_elision_are_checked(self):
        with self.assertRaises(ValueError):
            vocab.Vocabulary("lot", "lots", "n")
        with self.assertRaises(ValueError):
            vocab.Vocabulary("lot", "lots", "m", elision="yes")


class ActiveVocabularyTest(unittest.TestCase):
    def setUp(self):
        vocab.reset()
        self.addCleanup(vocab.reset)

    def test_the_dev_module_is_the_active_vocabulary(self):
        self.assertEqual(vocab.active(), dev.DEV)

    def test_without_a_declaring_module_core_serves(self):
        self.assertEqual(vocab.active(modules=[]), vocab.CORE)
        self.assertEqual(vocab.active(modules=[object()]), vocab.CORE)

    def test_the_first_declaration_wins(self):
        other = vocab.Vocabulary("ouvrage", "ouvrages", "m")
        first = type("First", (), {"NAME": "first", "VOCABULARY": other})()
        second = type("Second", (), {"NAME": "second", "VOCABULARY": dev.DEV})()
        self.assertEqual(vocab.active(modules=[first, second]), other)

    def test_an_invalid_declaration_is_refused(self):
        broken = type("Broken", (), {"NAME": "broken", "VOCABULARY": "lot"})()
        with self.assertRaises(ValueError):
            vocab.active(modules=[broken])

    def test_the_bare_profile_serves_core(self):
        with mock.patch.object(modules, "always_active", return_value=()):
            vocab.reset()
            self.assertEqual(vocab.active(), vocab.CORE)

    def test_a_broken_discovery_serves_core(self):
        from ameesh.modules import registry
        with mock.patch.object(registry, "discover", side_effect=RuntimeError("boom")):
            vocab.reset()
            self.assertEqual(vocab.active(), vocab.CORE)


if __name__ == "__main__":
    unittest.main()
