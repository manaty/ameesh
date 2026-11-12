# SPDX-License-Identifier: AGPL-3.0-only
"""#7000 : une renumérotation de migration imposée par la réservation garde les avis.

Constat nexlink #6953 (11/11) : le pilote échange les réservations 784 et 783 ;
le delta n'est que des renommages et les renvois au numéro dans les en-têtes, et
le report le refusait (« … differs outside a test or a conflict »). Témoin : le
même delta, en deux commits (renommage, puis renvois), sur une cible qui a pris
784. Refus : une ligne SQL changée en plus du numéro, un numéro hors réservation,
un renommage hors du répertoire de migrations (ou d'un compagnon déclaré), un nom
changé au-delà du préfixe, une sous-chaîne du numéro remplacée."""
from __future__ import annotations

import os
import unittest

from ameesh import radar, verdict_carry

from .test_2442_verdict_carry import Repo, _git

MIGRATION = """-- 784 / #6953 — le hub sert un nouvel inscrit jusqu'à son admission.
-- Rollback : supabase/rollback/784_restore.sql. Témoin :
-- supabase/tests/784_hub_service_test.sql. Ni 1784 ni 7840 ne bougent.
BEGIN;
SELECT ops.record_migration('784');
CREATE OR REPLACE FUNCTION public.hub_serves() RETURNS boolean LANGUAGE sql AS $$ SELECT true $$;
COMMIT;
"""
ROLLBACK = "-- Rollback de 784 / #6953.\nBEGIN;\nDROP FUNCTION public.hub_serves();\nCOMMIT;\n"
NEW_TEST = "-- 784 / #6953 — témoin.\nSELECT public.hub_serves();\n"
OLD_TEST = "-- 753 : admission.\nSELECT 1;\n"


def _put(root, path, text):
    full = os.path.join(root, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as fh:
        fh.write(text)
    _git(root, "add", path)


def _commit(root, message="c"):
    _git(root, "commit", "-q", "-m", message)
    return _git(root, "rev-parse", "HEAD")


class RenumberingTest(unittest.TestCase):
    """Le banc : `main` porte 753 ; le lot ajoute 784 (migration, rollback, test),
    et retouche le test de 753 ; la cible prend ensuite un autre 784."""

    MIG = "supabase/migrations/%s_hub_service.sql"
    ROLL = "supabase/rollback/%s_restore.sql"
    TEST = "supabase/tests/%s_hub_service_test.sql"

    def setUp(self) -> None:
        self.repo = Repo()
        self.addCleanup(self.repo.cleanup)
        root = self.root = self.repo.root
        _put(root, "supabase/migrations/753_admission.sql", "SELECT 753;\n")
        _put(root, "supabase/tests/753_admission_test.sql", OLD_TEST)
        base = _commit(root, "753")
        _git(root, "switch", "-q", "-c", "lot/6953", base)
        _put(root, self.MIG % "784", MIGRATION)
        _put(root, self.ROLL % "784", ROLLBACK)
        _put(root, self.TEST % "784", NEW_TEST)
        _put(root, "supabase/tests/753_admission_test.sql",
             OLD_TEST + "-- 784/#6953 : le hub sert.\n")
        self.reviewed = _commit(root, "lot")
        _git(root, "switch", "-q", "main")
        _put(root, "supabase/migrations/784_other_lot.sql", "SELECT 'other';\n")
        self.tip = _commit(root, "the other lot takes 784")
        _git(root, "switch", "-q", "lot/6953")
        self.renumber = verdict_carry.Renumbering(
            "supabase/migrations", frozenset({783}), ("supabase/rollback",))

    def rename(self, old="784", new="783", *, mig=None):
        for pattern in (self.MIG, self.ROLL, self.TEST):
            target = (mig if pattern is self.MIG and mig else pattern % new)
            _git(self.root, "mv", pattern % old, target)
        return _commit(self.root, "renumber %s to %s" % (old, new))

    def references(self, old="784", new="783", *, path=None, edit=None):
        """Les renvois au numéro, en jeton entier, dans les fichiers renommés et le
        test de 753 ; `edit` retouche en plus le contenu de `path`."""
        for target in (path or self.MIG % new, self.ROLL % new, self.TEST % new,
                       "supabase/tests/753_admission_test.sql"):
            full = os.path.join(self.root, target)
            with open(full, encoding="utf-8") as fh:
                text = fh.read()
            text = verdict_carry._substitute(text.encode(), {old: new}).decode()
            if edit and target == (path or self.MIG % new):
                text = edit(text)
            _put(self.root, target, text)
        return _commit(self.root, "references")

    def carriable(self, frozen, renumber="default"):
        renumber = self.renumber if renumber == "default" else renumber
        return verdict_carry._carriable(self.root, self.reviewed, frozen, self.tip,
                                        renumber=renumber)

    def refused(self, frozen, needle, renumber="default"):
        ok, why, adaptation = self.carriable(frozen, renumber)
        self.assertFalse(ok, why)
        self.assertFalse(adaptation)
        self.assertIn(needle, why)

    def test_the_case_of_6953_is_an_adaptation(self):
        self.rename()
        frozen = self.references()
        ok, why, adaptation = self.carriable(frozen)
        self.assertTrue(ok and adaptation, why)
        self.assertIn("adaptation : renumérotation 784→783", why)
        self.assertIn("1 test file(s) changed", why)
        self.assertIn("2 commit(s) proved", why)

    def test_a_renumbering_in_one_commit(self):
        self.rename()
        _git(self.root, "reset", "-q", "--soft", "HEAD~1")
        frozen = self.references()
        ok, why, adaptation = self.carriable(frozen)
        self.assertTrue(ok and adaptation, why)

    def test_the_token_is_whole(self):
        self.assertEqual(verdict_carry._substitute(
            b"784 '784' 784_x 1784 7840 a784 v784b #784\n", {"784": "783"}),
            b"783 '783' 783_x 1784 7840 a784 v784b #783\n")
        self.assertEqual(verdict_carry._substitute(b"783 784", {"784": "783", "783": "782"}),
                         b"782 783")

    def test_a_sql_line_changed_beyond_the_number_is_refused(self):
        self.rename()
        frozen = self.references(edit=lambda text: text.replace("SELECT true", "SELECT false"))
        self.refused(frozen, "is not supabase/migrations/784_hub_service.sql with only 784→783")

    def test_a_substring_of_the_number_is_refused(self):
        self.rename()
        frozen = self.references(edit=lambda text: text.replace("1784", "1783"))
        self.refused(frozen, "with only 784→783 changed")

    def test_a_number_outside_the_reservation_is_refused(self):
        self.rename("784", "785")
        frozen = self.references("784", "785")
        self.refused(frozen, "785 is not the lot's reservation (783)")

    def test_without_a_reservation_nothing_is_a_renumbering(self):
        self.rename()
        self.refused(self.references(), "differs outside a test or a conflict", renumber=None)

    def test_a_rename_outside_the_migrations_directory_is_refused(self):
        self.rename()
        frozen = self.references()
        bare = verdict_carry.Renumbering("supabase/migrations", frozenset({783}))
        self.refused(frozen, "supabase/rollback/783_restore.sql differs outside", bare)

    def test_a_name_changed_beyond_the_prefix_is_refused(self):
        self.rename(mig=self.MIG.replace("service", "services") % "783")
        frozen = self.references(path=self.MIG.replace("service", "services") % "783")
        self.refused(frozen, "differs outside a test or a conflict")

    def test_another_migration_touched_is_refused(self):
        self.rename()
        _put(self.root, "supabase/migrations/753_admission.sql", "SELECT 753; -- 783\n")
        self.refused(self.references(), "753_admission.sql differs outside")

    def test_a_migration_of_the_target_is_not_the_lot_s(self):
        # B1 qa-01 #213399 : la tête relue n'a jamais touché ce fichier.
        _git(self.root, "merge", "-q", "--no-edit", "main")
        _git(self.root, "mv", "supabase/migrations/784_other_lot.sql",
             "supabase/migrations/783_other_lot.sql")
        self.refused(_commit(self.root, "steal"), "_other_lot.sql differs outside")

    def test_a_target_file_touched_then_restored_is_refused(self):
        # B1 dev3 #213435 : il porte le nouveau numéro, mais n'est pas au lot.
        _git(self.root, "switch", "-q", "main")
        _put(self.root, "supabase/migrations/783_target.sql", "-- 783\n")
        self.tip = _commit(self.root, "a target file numbered 783")
        _git(self.root, "switch", "-q", "lot/6953")
        _git(self.root, "merge", "-q", "--no-edit", "main")
        self.rename()
        self.references()
        _put(self.root, "supabase/migrations/783_target.sql", "-- 784\n")
        _commit(self.root, "touched")
        _put(self.root, "supabase/migrations/783_target.sql", "-- 783\n")
        self.refused(_commit(self.root, "restored"), "neither a test adaptation")

    def test_two_migrations_to_one_number_are_refused(self):
        # B2 dev3 #213435 : deux 783 à l'arrivée.
        _git(self.root, "switch", "-q", "lot/6953")
        _put(self.root, "supabase/migrations/785_second.sql", "SELECT 785;\n")
        self.reviewed = _commit(self.root, "second migration")
        self.rename()
        _git(self.root, "mv", "supabase/migrations/785_second.sql",
             "supabase/migrations/783_second.sql")
        _put(self.root, "supabase/migrations/783_second.sql", "SELECT 783;\n")
        frozen = self.references()
        self.refused(frozen, "to the same number 783")

    def test_a_padded_number_is_the_same_number(self):
        # B dev01 #213475 : `0783_…` et `783_…` sont deux migrations 783.
        _put(self.root, "supabase/migrations/785_second.sql", "SELECT 785;\n")
        self.reviewed = _commit(self.root, "second migration")
        self.rename()
        _git(self.root, "mv", "supabase/migrations/785_second.sql",
             "supabase/migrations/0783_second.sql")
        frozen = self.references()
        renumber = verdict_carry.Renumbering(
            "supabase/migrations", frozenset({783}), ("supabase/rollback",))
        self.refused(frozen, "to the same number", renumber)

    def symlink(self, path, target):
        full = os.path.join(self.root, path)
        os.remove(full)
        os.symlink(target, full)
        _git(self.root, "add", path)

    def test_a_symlink_is_not_a_renumbering(self):
        # B cx2 #213476 : le lien ouvrirait un autre fichier.
        self.rename()
        self.references()
        # le blob du lien est exactement le contenu attendu (cas de cx2)
        with open(os.path.join(self.root, self.MIG % "783"), encoding="utf-8") as fh:
            self.symlink(self.MIG % "783", fh.read())
        self.refused(_commit(self.root, "link"), "is not supabase/migrations/784_hub_service.sql")

    def test_a_mode_change_is_not_a_renumbering(self):
        self.rename()
        os.chmod(os.path.join(self.root, self.MIG % "783"), 0o755)
        frozen = self.references()
        self.assertIn("100755", _git(self.root, "ls-tree", frozen, self.MIG % "783"))
        self.refused(frozen, "is not supabase/migrations/784_hub_service.sql")

    def test_a_type_changed_then_restored_in_the_history_is_refused(self):
        self.rename()
        self.references()
        with open(os.path.join(self.root, self.MIG % "783"), encoding="utf-8") as fh:
            text = fh.read()
        self.symlink(self.MIG % "783", text)
        _commit(self.root, "link")
        os.remove(os.path.join(self.root, self.MIG % "783"))
        _put(self.root, self.MIG % "783", text)
        self.refused(_commit(self.root, "back"), "neither a test adaptation")

    def two_reservations(self, second):
        """B dev3 #213504 : 784→783 et 785→782 ; la migration 785 cite 784."""
        _put(self.root, "supabase/migrations/785_second.sql", "-- 785\nSELECT 784;\n")
        self.reviewed = _commit(self.root, "second migration")
        self.rename()
        _git(self.root, "mv", "supabase/migrations/785_second.sql",
             "supabase/migrations/782_second.sql")
        _put(self.root, "supabase/migrations/782_second.sql", second)
        frozen = self.references()
        return frozen, verdict_carry.Renumbering(
            "supabase/migrations", frozenset({783, 782}), ("supabase/rollback",))

    def test_each_file_changes_only_its_own_number(self):
        frozen, renumber = self.two_reservations("-- 782\nSELECT 784;\n")
        ok, why, adaptation = self.carriable(frozen, renumber)
        self.assertTrue(ok and adaptation, why)
        self.assertIn("784→783, 785→782", why)

    def test_another_renumbered_number_in_a_file_is_refused(self):
        frozen, renumber = self.two_reservations("-- 782\nSELECT 783;\n")
        self.refused(frozen, "with only 785→782 changed", renumber)

    def test_another_lot_migration_touched_then_restored_is_refused(self):
        # B qa-01 #213550 : 786 n'est pas renumérotée, son SELECT 784 ne bouge pas.
        _put(self.root, "supabase/migrations/786_third.sql", "-- 786\nSELECT 784;\n")
        self.reviewed = _commit(self.root, "third migration")
        self.rename()
        self.references()
        _put(self.root, "supabase/migrations/786_third.sql", "-- 786\nSELECT 783;\n")
        _commit(self.root, "touched")
        _put(self.root, "supabase/migrations/786_third.sql", "-- 786\nSELECT 784;\n")
        self.refused(_commit(self.root, "restored"), "neither a test adaptation")

    def test_a_product_change_hidden_in_the_history_is_refused(self):
        _put(self.root, "secret.txt", "s\n")
        _commit(self.root, "secret")
        _git(self.root, "rm", "-q", "secret.txt")
        _commit(self.root, "gone")
        self.rename()
        self.refused(self.references(), "neither a test adaptation")

    def test_the_reservation_of_the_lot(self):
        row = {"id": 6953, "migration": 783, "migration_repo": "manaty/nexlink",
               "migrations": [{"number": 783, "repo": "github.com/manaty/nexlink"},
                              {"number": 12, "repo": "manaty/ameesh"}]}
        found = verdict_carry.renumbering_of(None, row, self.root)
        self.assertEqual(found, verdict_carry.Renumbering(
            "supabase/migrations", frozenset({783}), ()))
        self.assertIsNone(verdict_carry.renumbering_of(
            None, {"id": 1, "migration": None, "migration_repo": "manaty/nexlink"}, self.root))


class CompanionsWithoutGithubTest(unittest.TestCase):
    """B2 qa-01 #213399 : `github` est facultatif, le dépôt se lit sur `origin`."""

    def test_companions_follow_the_origin(self):
        repo = Repo()
        self.addCleanup(repo.cleanup)
        _git(repo.root, "remote", "add", "origin", "git@github.com:manaty/nexlink.git")
        cfg = type("Cfg", (), {"radar": {"projects": {"nexlink": {
            "repo": repo.root, "migrations": "supabase/migrations",
            "migrations_companions": ["supabase/rollback"]}}}})()
        row = {"id": 6953, "migration": 783, "migration_repo": "manaty/nexlink"}
        self.assertEqual(verdict_carry.renumbering_of(cfg, row, repo.root).companions,
                         ("supabase/rollback",))


class SettingsTest(unittest.TestCase):

    def parse(self, companions):
        return radar.parse_settings({"projects": {"nexlink": {
            "repo": "/tmp/x", "migrations": "supabase/migrations", "github": "manaty/nexlink",
            "migrations_companions": companions}}}).projects["nexlink"]

    def test_companions_are_declared_paths(self):
        self.assertEqual(self.parse(["supabase/rollback/", "supabase/registry.txt"])
                         .migrations_companions, ("supabase/rollback", "supabase/registry.txt"))
        self.assertEqual(self.parse("supabase/rollback").migrations_companions,
                         ("supabase/rollback",))

    def test_refusals(self):
        for bad in (["../up"], [""], [3], {"a": 1}, ["a/./b"]):
            with self.subTest(bad):
                with self.assertRaises(radar.SettingsError):
                    self.parse(bad)


if __name__ == "__main__":
    unittest.main()
