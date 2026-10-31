# SPDX-License-Identifier: AGPL-3.0-only
"""#5042 : un regel limité aux tests ou à un conflit mécanique garde les avis.

Canon §2 : « conflit mécanique, adaptation de test : aucun avis ». Témoins du
critère sur un vrai dépôt : adaptation de test seule ou après une fusion de la
cible, conflit résolu en gardant les deux côtés (deux ajouts, ou ajout voisin
d'une ligne modifiée : le cas #4975) ; et les refus : ligne de produit, plan de
CI, test retiré, lot fait de tests, désaccord sur une même ligne, ligne ajoutée,
côté perdu, marques restées, fusion « maligne », contenu ajouté puis retiré. Puis
sur Postgres : l'avis favorable se reporte et ouvre la porte, un blocage ne se
reporte pas, et son auteur reste dû."""
from __future__ import annotations

import os
import subprocess
import unittest

from ameesh import registry, verdict_carry, work

from .support import PgTestCase
from . import test_2442_verdict_carry as bench
from .test_2442_verdict_carry import ENV, Repo, _git


def _write(root, path, text):
    full = os.path.join(root, path)
    os.makedirs(os.path.dirname(full) or root, exist_ok=True)
    with open(full, "w", encoding="utf-8") as fh:
        fh.write(text)
    _git(root, "add", path)


def _commit_at(root, path, text, message="c"):
    _write(root, path, text)
    _git(root, "commit", "-q", "-m", message)
    return _git(root, "rev-parse", "HEAD")


class ComposedHunksTest(unittest.TestCase):
    """`composed_hunks` : seule une résolution sans ligne nouvelle est mécanique."""

    CONFLICT = (b"a\n<<<<<<< L\nl1\nb\n||||||| B\nb\n=======\nb2\n>>>>>>> R\nz\n")

    def test_an_addition_next_to_a_line_changed_by_the_other_side(self):
        self.assertEqual(verdict_carry.composed_hunks(self.CONFLICT, b"a\nl1\nb2\nz\n"), 1)

    def test_two_additions_are_juxtaposed_in_either_order(self):
        both = b"<<<<<<< L\nx\nend\n||||||| B\n=======\ny\nend\n>>>>>>> R\n"
        self.assertEqual(verdict_carry.composed_hunks(both, b"x\nend\ny\nend\n"), 1)
        self.assertEqual(verdict_carry.composed_hunks(both, b"y\nend\nx\nend\n"), 1)

    def test_refusals(self):
        for name, resolved in (("new line", b"a\nl1\nb2\nnew\nz\n"),
                               ("side lost", b"a\nb2\nz\n"),
                               ("old line kept", b"a\nl1\nb\nb2\nz\n"),
                               ("outside", b"a!\nl1\nb2\nz\n"),
                               ("trailing", b"a\nl1\nb2\nz\nmore\n")):
            with self.subTest(name):
                self.assertIsNone(verdict_carry.composed_hunks(self.CONFLICT, resolved))

    def test_the_order_of_one_side_is_kept(self):
        both = b"<<<<<<< L\nx\ny\n||||||| B\n=======\nw\n>>>>>>> R\n"
        self.assertEqual(verdict_carry.composed_hunks(both, b"x\ny\nw\n"), 1)
        self.assertIsNone(verdict_carry.composed_hunks(both, b"y\nx\nw\n"))

    def test_both_sides_changing_one_line_is_a_judgment(self):
        both = b"<<<<<<< L\nx = 1\n||||||| B\nx = 0\n=======\nx = 2\n>>>>>>> R\n"
        self.assertIsNone(verdict_carry.composed_hunks(both, b"x = 1\nx = 2\n"))

    def test_unreadable_markers_prove_nothing(self):
        self.assertIsNone(verdict_carry.composed_hunks(b"plain\n", b"plain\n"))
        self.assertIsNone(verdict_carry.composed_hunks(b"<<<<<<< L\nx\n", b"x\n"))
        nested = (b"<<<<<<< L\nx\n||||||| B\n<<<<<<< T\ny\n=======\nz\n>>>>>>> T\n"
                  b"=======\nw\n>>>>>>> R\n")
        self.assertIsNone(verdict_carry.composed_hunks(nested, b"x\nw\n"))


class Conflicted:
    """`main` et le lot divergent sur `sql.txt` ; le lot porte aussi `src.py`."""

    BASE = "head\nrow old\ntail\n"

    def __init__(self, ours: str, theirs: str) -> None:
        self.repo = Repo()
        self.root = self.repo.root
        _git(self.root, "switch", "-q", "main")
        self.b0 = _commit_at(self.root, "sql.txt", self.BASE, "base")
        _git(self.root, "switch", "-q", "-c", "lot/c", self.b0)
        _commit_at(self.root, "src.py", "code\n", "src")
        self.reviewed = _commit_at(self.root, "sql.txt", theirs, "lot")
        _git(self.root, "switch", "-q", "main")
        self.tip = _commit_at(self.root, "sql.txt", ours, "target")

    def resolve(self, text: str) -> str:
        """Fusionne la cible dans le lot et résout `sql.txt` en `text`."""
        _git(self.root, "switch", "-q", "lot/c")
        _run_merge(self.root, "main")
        _write(self.root, "sql.txt", text)
        _git(self.root, "commit", "-q", "--no-edit")
        return _git(self.root, "rev-parse", "HEAD")

    def cleanup(self) -> None:
        self.repo.cleanup()


def _run_merge(root, branch):
    return subprocess.run(["git", "-C", root, "merge", "-q", "--no-edit", branch],
                          env=ENV, capture_output=True, text=True).returncode


class TestAdaptationTest(unittest.TestCase):
    """Un delta de tests : la tête relue porte du produit et son test."""

    def setUp(self) -> None:
        self.repo = Repo()
        self.addCleanup(self.repo.cleanup)
        self.root = self.repo.root
        _git(self.root, "switch", "-q", "lot/9-x")
        _write(self.root, "tests/test_lot.py", "assert 1\n")
        _write(self.root, "tests/parts/plan.txt", "test_lot\n")
        _git(self.root, "commit", "-q", "-m", "test")
        self.reviewed = _git(self.root, "rev-parse", "HEAD")

    def carriable(self, frozen):
        return verdict_carry._carriable(self.root, self.reviewed, frozen, self.repo.t1)

    def test_a_test_only_commit_is_an_adaptation(self):
        frozen = _commit_at(self.root, "tests/test_lot.py", "assert 2\n")
        ok, why, adaptation = self.carriable(frozen)
        self.assertTrue(ok, why)
        self.assertTrue(adaptation)
        self.assertIn("1 test file(s) changed", why)

    def test_a_test_adaptation_after_a_merge_of_the_target(self):
        _git(self.root, "merge", "-q", "--no-edit", "main")
        frozen = _commit_at(self.root, "tests/new_helper_test.py", "x\n")
        ok, why, adaptation = self.carriable(frozen)
        self.assertTrue(ok and adaptation, why)

    def test_a_realignment_stays_a_realignment(self):
        _git(self.root, "merge", "-q", "--no-edit", "main")
        ok, why, adaptation = self.carriable(_git(self.root, "rev-parse", "HEAD"))
        self.assertTrue(ok, why)
        self.assertFalse(adaptation)

    def refused(self, frozen, needle):
        ok, why, adaptation = self.carriable(frozen)
        self.assertFalse(ok)
        self.assertFalse(adaptation)
        self.assertIn(needle, why)

    def test_a_product_line_is_refused(self):
        _write(self.root, "tests/test_lot.py", "assert 2\n")
        self.refused(_commit_at(self.root, "lot.txt", "lot changed\n"),
                     "lot.txt differs outside a test")

    def test_the_ci_plan_is_not_a_test(self):
        self.refused(_commit_at(self.root, "tests/parts/plan.txt", "nothing\n"),
                     "tests/parts/plan.txt differs")

    def test_a_removed_test_is_refused(self):
        _git(self.root, "rm", "-q", "tests/test_lot.py")
        _git(self.root, "commit", "-q", "-m", "gone")
        self.refused(_git(self.root, "rev-parse", "HEAD"), "tests/test_lot.py differs")

    def test_a_lot_made_of_tests_keeps_its_review(self):
        _git(self.root, "switch", "-q", "-c", "only-tests", self.repo.b0)
        self.reviewed = _commit_at(self.root, "tests/test_only.py", "assert 1\n")
        self.refused(_commit_at(self.root, "tests/test_only.py", "assert 2\n"),
                     "made of tests")

    def test_a_product_line_added_then_removed_stays_refused(self):
        # R1 cx1 : elle resterait dans l'historique (un secret, par exemple).
        _commit_at(self.root, "tests/test_lot.py", "assert 2\n")
        _commit_at(self.root, "secret.txt", "s\n")
        _git(self.root, "rm", "-q", "secret.txt")
        _git(self.root, "commit", "-q", "-m", "gone")
        self.refused(_git(self.root, "rev-parse", "HEAD"), "neither a test adaptation")

    def test_a_clean_merge_of_another_branch_is_refused(self):
        _git(self.root, "switch", "-q", "-c", "other", self.repo.b0)
        _commit_at(self.root, "other-branch.txt", "o\n")
        _git(self.root, "switch", "-q", "lot/9-x")
        _commit_at(self.root, "tests/test_lot.py", "assert 2\n")
        _git(self.root, "merge", "-q", "--no-edit", "other")
        self.refused(_git(self.root, "rev-parse", "HEAD"), "differs outside a test")


class MechanicalConflictTest(unittest.TestCase):
    """Le conflit : la tête relue ne fusionne plus proprement dans la cible."""

    def setUpCase(self, ours, theirs) -> Conflicted:
        case = Conflicted(ours, theirs)
        self.addCleanup(case.cleanup)
        return case

    def carriable(self, case, frozen):
        return verdict_carry._carriable(case.root, case.reviewed, frozen, case.tip)

    def test_the_case_of_4975(self):
        # La cible insère un bloc devant la ligne que le lot modifie.
        case = self.setUpCase("head\nblock\nrow old\ntail\n", "head\nrow new\ntail\n")
        ok, why, adaptation = self.carriable(case, case.resolve("head\nblock\nrow new\ntail\n"))
        self.assertTrue(ok and adaptation, why)
        self.assertIn("1 conflict hunk(s) keeping both sides", why)

    def test_a_disagreement_on_one_line_is_a_judgment(self):
        case = self.setUpCase("head\nrow target\ntail\n", "head\nrow lot\ntail\n")
        ok, why, _ = self.carriable(case, case.resolve("head\nrow target\nrow lot\ntail\n"))
        self.assertFalse(ok, why)
        self.assertIn("not resolved by keeping both", why)

    def test_a_new_line_in_the_resolution_is_refused(self):
        case = self.setUpCase("head\nblock\nrow old\ntail\n", "head\nrow new\ntail\n")
        ok, why, _ = self.carriable(case, case.resolve("head\nblock\nrow new\nmine\ntail\n"))
        self.assertFalse(ok, why)

    def test_markers_left_in_the_file_are_refused(self):
        case = self.setUpCase("head\nblock\nrow old\ntail\n", "head\nrow new\ntail\n")
        _git(case.root, "switch", "-q", "lot/c")
        _run_merge(case.root, "main")
        _git(case.root, "add", "sql.txt")
        _git(case.root, "commit", "-q", "--no-edit")
        ok, why, _ = self.carriable(case, _git(case.root, "rev-parse", "HEAD"))
        self.assertFalse(ok, why)

    def test_an_evil_merge_of_the_target_is_refused(self):
        case = self.setUpCase("head\nblock\nrow old\ntail\n", "head\nrow new\ntail\n")
        _git(case.root, "switch", "-q", "lot/c")
        _run_merge(case.root, "main")
        _write(case.root, "sql.txt", "head\nblock\nrow new\ntail\n")
        _write(case.root, "src.py", "evil\n")
        _git(case.root, "commit", "-q", "--no-edit")
        ok, why, _ = self.carriable(case, _git(case.root, "rev-parse", "HEAD"))
        self.assertFalse(ok, why)
        self.assertIn("src.py differs outside", why)

    def test_a_deletion_conflict_is_refused(self):
        case = self.setUpCase("head\nrow old\ntail\nend\n", "head\nrow new\ntail\n")
        _git(case.root, "switch", "-q", "main")
        _git(case.root, "rm", "-q", "sql.txt")
        _git(case.root, "commit", "-q", "-m", "drop")
        case.tip = _git(case.root, "rev-parse", "HEAD")
        _git(case.root, "switch", "-q", "lot/c")
        _run_merge(case.root, "main")
        _write(case.root, "sql.txt", "head\nrow new\ntail\n")
        _git(case.root, "commit", "-q", "--no-edit")
        ok, why, _ = self.carriable(case, _git(case.root, "rev-parse", "HEAD"))
        self.assertFalse(ok, why)


class CarryAdaptationTest(PgTestCase):
    """Bout en bout : même banc que #2442, le lot porte un produit et son test."""

    verdict = bench.CarryTest.verdict
    freeze = bench.CarryTest.freeze
    carry = bench.CarryTest.carry
    on = bench.CarryTest.on
    gate = bench.CarryTest.gate
    notes = bench.CarryTest.notes

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        for name, harness in (("author", "deepseek"), ("rev1", "claude"), ("rev2", "codex")):
            registry.upsert(cls.db, name, harness=harness, host="pc")

    def setUp(self) -> None:
        bench.CarryTest.setUp(self)
        _git(self.repo.root, "switch", "-q", "lot/9-x")
        self.reviewed = _commit_at(self.repo.root, "tests/test_lot.py", "assert 1\n")
        work.milestone(self.db, self.lot, "frozen", sha=self.reviewed, actor="author")

    def test_an_ok_verdict_follows_a_test_adaptation_and_opens_the_gate(self):
        self.verdict("rev1", self.reviewed)
        self.verdict("rev2", self.reviewed)
        frozen = _commit_at(self.repo.root, "tests/test_lot.py", "assert 2\n")
        self.freeze(frozen)
        result = self.carry()
        self.assertEqual(result["result"], "carried", result)
        self.assertEqual(result["adaptations"][self.reviewed][:21], "test or mechanical ad")
        carried = self.on(frozen)
        self.assertEqual(sorted(m["actor"] for m in carried), ["rev1", "rev2"])
        self.assertTrue(all("adaptation (canon §2)" in m["note"] for m in carried))
        self.assertTrue(self.gate(frozen)["ok"])
        self.assertTrue(any("adaptation (canon §2)" in note
                            for note in self.notes(verdict_carry.CARRIED)))

    def test_a_blocking_verdict_does_not_follow_and_its_author_stays_due(self):
        # Le correctif de test peut répondre au blocage : son auteur juge.
        self.verdict("rev1", self.reviewed)
        self.verdict("rev2", self.reviewed, "blocked")
        frozen = _commit_at(self.repo.root, "tests/test_lot.py", "assert 2\n")
        self.freeze(frozen)
        result = self.carry()
        self.assertEqual([e["actor"] for e in result["carried"]], ["rev1"])
        self.assertEqual([e["why"] for e in result["refused"]],
                         [verdict_carry.NOT_OK_ADAPTATION])
        self.assertEqual([m["actor"] for m in self.on(frozen)], ["rev1"])
        self.assertFalse(self.gate(frozen)["ok"])


if __name__ == "__main__":
    unittest.main()
