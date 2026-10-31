# SPDX-License-Identifier: AGPL-3.0-only
"""#2442 : un réalignement mécanique sur la cible ne redemande pas de verdict.

Témoins : le critère (fusions d'essai propres et même arbre sur la pointe de la
cible) — fusion de la cible, rebasage, cible avancée depuis, commit de l'auteur,
fusion « maligne », résolution de conflit (côté cible : reportée,
#4053 ; ligne ajoutée ou côté du lot : refusée) ; les candidats au report (dernier
verdict de chaque relecteur, hors tête gelée) ; puis sur Postgres le report de
bout en bout : jalons au nom des relecteurs, porte franchie, blocage reporté,
idempotence, issue négative journalisée une fois, verdict déjà rendu sur la
tête gelée gardé, passage de l'exécuteur et ligne de la CLI."""
from __future__ import annotations

import contextlib
import io
import os
import shutil
import subprocess
import tempfile
import threading
import types
import unittest
from unittest import mock

from ameesh import db as db_mod
from ameesh import lot_class, merge_queue, plan_git, registry, storage, verdict_carry, work

from .support import PgTestCase, patch_families

A = "a" * 40
B = "b" * 40
C = "c" * 40

ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
           GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")

FAM = {"author": "deepseek", "rev1": "claude", "rev2": "codex"}


def _family(db, name, **kw):
    return FAM.get(str(name or "").strip(), "")


def _git(root, *args) -> str:
    return subprocess.run(["git", "-C", root, *args], check=True, env=ENV,
                          capture_output=True, text=True).stdout.strip()


def _commit(root, path, text, message) -> str:
    with open(os.path.join(root, path), "w", encoding="utf-8") as fh:
        fh.write(text)
    _git(root, "add", path)
    _git(root, "commit", "-q", "-m", message)
    return _git(root, "rev-parse", "HEAD")


class Repo:
    """Un dépôt : `main` (B0 → T1 → T2), la branche du lot (R = B0 + lot.txt)."""

    def __init__(self) -> None:
        self.root = tempfile.mkdtemp(prefix="ameesh-2442-")
        subprocess.run(["git", "init", "-q", "-b", "main", self.root], check=True, env=ENV)
        self.b0 = _commit(self.root, "shared.txt", "one\ntwo\nthree\n", "b0")
        _git(self.root, "switch", "-q", "-c", "lot/9-x")
        self.reviewed = _commit(self.root, "lot.txt", "lot\n", "lot")
        _git(self.root, "switch", "-q", "main")
        self.t1 = _commit(self.root, "other.txt", "other\n", "t1")

    def merge_target(self) -> str:
        """F = R réaligné par fusion de la cible."""
        _git(self.root, "switch", "-q", "lot/9-x")
        _git(self.root, "merge", "-q", "--no-edit", "main")
        return _git(self.root, "rev-parse", "HEAD")

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


class CriterionTest(unittest.TestCase):
    """`realigned` : atterrir la nouvelle tête, c'est atterrir la tête relue."""

    def setUp(self) -> None:
        self.repo = Repo()
        self.addCleanup(self.repo.cleanup)

    def check(self, frozen, tip=None):
        return verdict_carry.realigned(self.repo.root, self.repo.reviewed, frozen,
                                       tip or self.repo.t1)

    def test_a_merge_of_the_target_is_a_realignment(self):
        frozen = self.repo.merge_target()
        self.assertEqual(self.check(frozen),
                         (True, "same merge result, mechanical history and same lot delta"))

    def test_the_target_may_have_moved_since(self):
        frozen = self.repo.merge_target()
        _git(self.repo.root, "switch", "-q", "main")
        t2 = _commit(self.repo.root, "later.txt", "later\n", "t2")
        self.assertTrue(self.check(frozen, t2)[0])

    def test_a_rebase_on_the_target_is_a_realignment(self):
        _git(self.repo.root, "switch", "-q", "-c", "rebased", "main")
        _git(self.repo.root, "cherry-pick", self.repo.reviewed)
        self.assertTrue(self.check(_git(self.repo.root, "rev-parse", "HEAD"))[0])

    def test_a_rebase_dropping_a_commit_already_in_the_target_is_a_realignment(self):
        # N1 qa-01 #2486 : B0 ; R = A+B ; T1 = copie de A (+ Z) ; F = T1+B'.
        # Le rebase abandonne A, déjà appliqué dans la cible : le delta propre
        # du lot reste B. `rebase_identity` compte 1/2 patch-ids et refuse ; la
        # preuve « chaque commit relu est rejoué ou déjà atterri » l'accepte.
        _git(self.repo.root, "switch", "-q", "-c", "reviewed2", self.repo.b0)
        a = _commit(self.repo.root, "a.txt", "a\n", "A")
        b = _commit(self.repo.root, "b.txt", "b\n", "B")
        reviewed = _git(self.repo.root, "rev-parse", "HEAD")
        _git(self.repo.root, "switch", "-q", "main")
        _git(self.repo.root, "cherry-pick", a)
        _commit(self.repo.root, "z.txt", "z\n", "Z")
        tip = _git(self.repo.root, "rev-parse", "HEAD")
        _git(self.repo.root, "switch", "-q", "-c", "rebased2", tip)
        _git(self.repo.root, "cherry-pick", b)
        frozen = _git(self.repo.root, "rev-parse", "HEAD")
        self.assertFalse(plan_git.rebase_identity(
            self.repo.root, reviewed, frozen, tip)["mechanical"])
        ok, why = verdict_carry.realigned(self.repo.root, reviewed, frozen, tip)
        self.assertTrue(ok, why)
        self.assertIn("already landed in the target", why)

    def test_a_dropped_commit_not_landed_in_the_target_is_not_a_realignment(self):
        # N1 qa-01 #2486 : A ajoute X, B le retire ; la cible ne porte pas X.
        # L'atterrissage est le même (F = T), mais A n'est pas atterri : un
        # abandon sans preuve de contenu dans la cible ne reporte rien.
        _git(self.repo.root, "switch", "-q", "-c", "reviewed3", self.repo.b0)
        _commit(self.repo.root, "x.txt", "x\n", "A")
        _git(self.repo.root, "rm", "-q", "x.txt")
        _git(self.repo.root, "commit", "-q", "-m", "B")
        reviewed = _git(self.repo.root, "rev-parse", "HEAD")
        frozen = self.repo.t1
        ok, why = verdict_carry.realigned(self.repo.root, reviewed, frozen, self.repo.t1)
        self.assertFalse(ok)
        self.assertIn("neither replayed nor already landed", why)

    def test_an_author_commit_is_not_a_realignment(self):
        self.repo.merge_target()
        frozen = _commit(self.repo.root, "lot.txt", "lot, changed\n", "fix")
        ok, why = self.check(frozen)
        self.assertFalse(ok)
        self.assertIn("merge results differ", why)

    def test_an_evil_merge_is_not_a_realignment(self):
        _git(self.repo.root, "switch", "-q", "lot/9-x")
        _git(self.repo.root, "merge", "-q", "--no-commit", "main")
        with open(os.path.join(self.repo.root, "other.txt"), "w", encoding="utf-8") as fh:
            fh.write("other, edited in the merge\n")
        _git(self.repo.root, "add", "other.txt")
        _git(self.repo.root, "commit", "-q", "--no-edit")
        self.assertFalse(self.check(_git(self.repo.root, "rev-parse", "HEAD"))[0])

    def test_content_from_another_branch_is_not_a_realignment(self):
        _git(self.repo.root, "switch", "-q", "-c", "elsewhere", self.repo.b0)
        _commit(self.repo.root, "foreign.txt", "foreign\n", "foreign")
        _git(self.repo.root, "switch", "-q", "lot/9-x")
        _git(self.repo.root, "merge", "-q", "--no-edit", "main")
        _git(self.repo.root, "merge", "-q", "--no-edit", "elsewhere")
        self.assertFalse(self.check(_git(self.repo.root, "rev-parse", "HEAD"))[0])

    def test_a_conflict_resolution_is_a_judgment(self):
        _git(self.repo.root, "switch", "-q", "lot/9-x")
        reviewed = _commit(self.repo.root, "shared.txt", "one\nLOT\nthree\n", "lot edit")
        _git(self.repo.root, "switch", "-q", "main")
        tip = _commit(self.repo.root, "shared.txt", "one\nMAIN\nthree\n", "main edit")
        _git(self.repo.root, "switch", "-q", "lot/9-x")
        subprocess.run(["git", "-C", self.repo.root, "merge", "-q", "--no-edit", "main"],
                       env=ENV, capture_output=True)
        with open(os.path.join(self.repo.root, "shared.txt"), "w", encoding="utf-8") as fh:
            fh.write("one\nLOT\nthree\n")
        _git(self.repo.root, "add", "shared.txt")
        _git(self.repo.root, "commit", "-q", "--no-edit")
        frozen = _git(self.repo.root, "rev-parse", "HEAD")
        ok, why = verdict_carry.realigned(self.repo.root, reviewed, frozen, tip)
        self.assertFalse(ok)
        self.assertIn("resolves a conflict", why)

    def _conflict(self):
        """#4053 : R ajoute lot2.txt, puis modifie la ligne 2 de shared.txt et
        lot.txt (le commit garde un contenu une fois le conflit résolu côté
        cible) ; la cible modifie la même ligne. Rend (R, T)."""
        _git(self.repo.root, "switch", "-q", "lot/9-x")
        _commit(self.repo.root, "lot2.txt", "lot2\n", "lot 2")
        with open(os.path.join(self.repo.root, "lot.txt"), "w", encoding="utf-8") as fh:
            fh.write("lot, edited\n")
        _git(self.repo.root, "add", "lot.txt")
        reviewed = _commit(self.repo.root, "shared.txt", "one\nLOT\nthree\n", "lot edit")
        _git(self.repo.root, "switch", "-q", "main")
        tip = _commit(self.repo.root, "shared.txt", "one\nMAIN\nthree\n", "main edit")
        return reviewed, tip

    def _rebase(self, tip, resolution):
        """Rebase à la main de R sur T ; le conflit de shared.txt prend `resolution`."""
        _git(self.repo.root, "switch", "-q", "-c", "rebased-4053", tip)
        _git(self.repo.root, "cherry-pick", "%s..lot/9-x~1" % self.repo.b0)
        subprocess.run(["git", "-C", self.repo.root, "cherry-pick", "lot/9-x"],
                       env=ENV, capture_output=True)
        with open(os.path.join(self.repo.root, "shared.txt"), "w", encoding="utf-8") as fh:
            fh.write(resolution)
        _git(self.repo.root, "add", "shared.txt")
        _git(self.repo.root, "-c", "core.editor=true", "cherry-pick", "--continue")
        return _git(self.repo.root, "rev-parse", "HEAD")

    def test_a_rebase_resolved_on_the_target_side_is_a_realignment(self):
        # #4053, témoin 1 (#3993, 78f18bd4 → 5b64add1) : rebase dont le seul
        # morceau en conflit prend le côté de la cible ; aucune ligne nouvelle.
        reviewed, tip = self._conflict()
        frozen = self._rebase(tip, "one\nMAIN\nthree\n")
        self.assertIsNone(verdict_carry.merged_tree(self.repo.root, tip, reviewed))
        self.assertEqual(verdict_carry.conflict_hunks(self.repo.root, tip, reviewed), 1)
        self.assertEqual(
            verdict_carry.realigned(self.repo.root, reviewed, frozen, tip),
            (True, "conflict resolved on the target side: 1 hunk(s) already carried by "
                   "the target, no new line (3 mechanical commit(s), 1 resolved on the "
                   "target side)"))
        # la cible avance sans toucher au lot : le report tient
        _git(self.repo.root, "switch", "-q", "main")
        t2 = _commit(self.repo.root, "later.txt", "later\n", "t2")
        self.assertTrue(verdict_carry.realigned(self.repo.root, reviewed, frozen, t2)[0])

    def test_a_merge_resolved_on_the_target_side_is_a_realignment(self):
        reviewed, tip = self._conflict()
        _git(self.repo.root, "switch", "-q", "lot/9-x")
        subprocess.run(["git", "-C", self.repo.root, "merge", "-q", "--no-edit", "main"],
                       env=ENV, capture_output=True)
        with open(os.path.join(self.repo.root, "shared.txt"), "w", encoding="utf-8") as fh:
            fh.write("one\nMAIN\nthree\n")
        _git(self.repo.root, "add", "shared.txt")
        _git(self.repo.root, "commit", "-q", "--no-edit")
        frozen = _git(self.repo.root, "rev-parse", "HEAD")
        ok, why = verdict_carry.realigned(self.repo.root, reviewed, frozen, tip)
        self.assertTrue(ok, why)
        self.assertIn("conflict resolved on the target side: 1 hunk(s)", why)
        # hors de ce mode, la fusion résolue n'est pas mécanique
        self.assertIn("not the clean, unmodified merge", verdict_carry.mechanical_history(
            self.repo.root, reviewed, frozen, tip)[1])

    def test_a_resolution_adding_a_line_is_a_judgment(self):
        # #4053, témoin 2 : le côté de la cible, plus une ligne de l'auteur.
        reviewed, tip = self._conflict()
        frozen = self._rebase(tip, "one\nMAIN\nadded\nthree\n")
        ok, why = verdict_carry.realigned(self.repo.root, reviewed, frozen, tip)
        self.assertFalse(ok)
        self.assertIn("resolves a conflict", why)

    def test_a_rebase_resolved_on_the_lot_side_is_a_judgment(self):
        # #4053, témoin 3 : le côté du lot (theirs) écrase la cible.
        reviewed, tip = self._conflict()
        frozen = self._rebase(tip, "one\nLOT\nthree\n")
        ok, why = verdict_carry.realigned(self.repo.root, reviewed, frozen, tip)
        self.assertFalse(ok)
        self.assertIn("resolves a conflict", why)

    def test_a_target_side_landing_with_author_commits_is_a_judgment(self):
        # Même atterrissage côté cible, mais un secret ajouté puis retiré par
        # l'auteur reste dans l'historique : refusé.
        reviewed, tip = self._conflict()
        self._rebase(tip, "one\nMAIN\nthree\n")
        _commit(self.repo.root, "secret.txt", "s3cr3t\n", "oups")
        _git(self.repo.root, "rm", "-q", "secret.txt")
        _git(self.repo.root, "commit", "-q", "-m", "retiré")
        frozen = _git(self.repo.root, "rev-parse", "HEAD")
        ok, why = verdict_carry.realigned(self.repo.root, reviewed, frozen, tip)
        self.assertFalse(ok)
        self.assertIn("the history is not mechanical", why)
        self.assertIn("not a replay of a reviewed commit", why)

    def test_content_masked_by_the_target_is_not_a_realignment(self):
        # R1 cx1, P1-2 : l'auteur ajoute à l'identique un fichier de la cible,
        # sans en descendre. Le même arbre sur T le masque ; le delta du lot le
        # montre — sinon il réapparaîtrait le jour où la cible le retire.
        _git(self.repo.root, "switch", "-q", "lot/9-x")
        frozen = _commit(self.repo.root, "other.txt", "other\n", "copie de la cible")
        self.assertEqual(verdict_carry.merged_tree(self.repo.root, self.repo.t1, frozen),
                         verdict_carry.merged_tree(self.repo.root, self.repo.t1,
                                                   self.repo.reviewed))
        ok, why = self.check(frozen)
        self.assertFalse(ok)
        self.assertIn("not a replay of a reviewed commit", why)
        # la preuve du delta, seule, le refuse aussi
        same = plan_git.rebase_identity(self.repo.root, self.repo.reviewed, frozen,
                                        self.repo.t1)
        self.assertFalse(same["mechanical"])
        _git(self.repo.root, "switch", "-q", "main")
        t2 = _commit(self.repo.root, "other.txt", "reverted\n", "la cible change d'avis")
        self.assertNotEqual(verdict_carry.merged_tree(self.repo.root, t2, frozen),
                            verdict_carry.merged_tree(self.repo.root, t2, self.repo.reviewed))

    def test_an_added_then_removed_secret_is_not_a_realignment(self):
        # R1 qa-01 : fusion de la cible, puis un secret ajouté et retiré. Arbre
        # et delta net sont les mêmes ; l'historique porterait le secret.
        self.repo.merge_target()
        _commit(self.repo.root, "secret.txt", "s3cr3t\n", "oups")
        _git(self.repo.root, "rm", "-q", "secret.txt")
        _git(self.repo.root, "commit", "-q", "-m", "retiré")
        frozen = _git(self.repo.root, "rev-parse", "HEAD")
        ok, why = self.check(frozen)
        self.assertFalse(ok)
        self.assertIn("not a replay of a reviewed commit", why)

    def test_a_merge_retouched_in_history_only_is_not_a_realignment(self):
        # Une fusion qui ajoute un fichier, retiré au commit suivant : même
        # atterrissage, mais la fusion n'est pas la fusion propre de ses parents.
        _git(self.repo.root, "switch", "-q", "lot/9-x")
        _git(self.repo.root, "merge", "-q", "--no-commit", "main")
        with open(os.path.join(self.repo.root, "hidden.txt"), "w", encoding="utf-8") as fh:
            fh.write("hidden\n")
        _git(self.repo.root, "add", "hidden.txt")
        _git(self.repo.root, "commit", "-q", "--no-edit")
        _git(self.repo.root, "rm", "-q", "hidden.txt")
        _git(self.repo.root, "commit", "-q", "-m", "retiré")
        ok, why = verdict_carry.mechanical_history(
            self.repo.root, self.repo.reviewed, _git(self.repo.root, "rev-parse", "HEAD"),
            self.repo.t1)
        self.assertFalse(ok)
        self.assertIn("not the clean, unmodified merge", why)

    def test_merges_and_replays_are_mechanical_history(self):
        frozen = self.repo.merge_target()
        self.assertEqual(verdict_carry.mechanical_history(
            self.repo.root, self.repo.reviewed, frozen, self.repo.t1),
            (True, "1 mechanical commit(s)"))
        _git(self.repo.root, "switch", "-q", "-c", "rebased", "main")
        _git(self.repo.root, "cherry-pick", self.repo.reviewed)
        self.assertTrue(verdict_carry.mechanical_history(
            self.repo.root, self.repo.reviewed, _git(self.repo.root, "rev-parse", "HEAD"),
            self.repo.t1)[0])

    def test_a_shallow_clone_is_refused(self):
        # N2 qa-01 #2486 : `mechanical_history` dépend de l'histoire complète ;
        # un clone superficiel ferait passer un commit manquant pour neuf.
        shallow = tempfile.mkdtemp(prefix="ameesh-2442-shallow-")
        self.addCleanup(shutil.rmtree, shallow, ignore_errors=True)
        subprocess.run(["git", "clone", "-q", "--depth", "1",
                        "file://" + self.repo.root, shallow],
                       check=True, env=ENV, capture_output=True)
        self.assertEqual(
            verdict_carry.mechanical_history(shallow, A, B, C),
            (False, "complete Git history required; shallow clone refused"))
        ok, why = verdict_carry.realigned(shallow, A, B, C)
        self.assertFalse(ok)
        self.assertIn("shallow clone refused", why)

    def test_the_same_head_carries_nothing(self):
        self.assertEqual(self.check(self.repo.reviewed), (False, "same head"))

    def test_an_unknown_commit_proves_nothing(self):
        self.assertFalse(self.check(C)[0])
        self.assertIsNone(verdict_carry.merged_tree(self.repo.root, self.repo.t1, C))


class PendingTest(unittest.TestCase):
    """Les candidats : le dernier verdict de chaque relecteur, hors tête gelée."""

    @staticmethod
    def marks(*rows):
        return [dict({"kind": "verdict", "verdict": "ok"}, **row, at_ts=float(i), id=i)
                for i, row in enumerate(rows, start=1)]

    def test_latest_verdict_of_each_reviewer_off_the_freeze(self):
        rows = self.marks({"actor": "rev1", "sha": A}, {"actor": "rev2", "sha": A},
                          {"actor": "rev1", "sha": B[:12], "verdict": "blocked"})
        got = verdict_carry.pending(rows, C)
        self.assertEqual(sorted(got), ["rev1", "rev2"])
        self.assertEqual(got["rev1"]["verdict"], "blocked")

    def test_a_verdict_on_the_freeze_is_never_replaced(self):
        rows = self.marks({"actor": "rev1", "sha": A}, {"actor": "rev1", "sha": C[:10]})
        self.assertEqual(verdict_carry.pending(rows, C), {})
        later = self.marks({"actor": "rev1", "sha": C}, {"actor": "rev1", "sha": A})
        self.assertEqual(verdict_carry.pending(later, C), {})

    def test_an_outcome_is_found_from_an_abbreviated_sha(self):
        # N1 qa-01 : la note porte des SHA à 12 caractères ; un verdict qui cite
        # 7 caractères ou le SHA complet retrouve la même issue.
        notes = ["verdict not carried: %s → %s on main@%s: x" % (A[:12], C[:12], B[:12])]
        self.assertTrue(verdict_carry._decided(notes, A[:7], C))
        self.assertTrue(verdict_carry._decided(notes, A, C[:8]))
        self.assertFalse(verdict_carry._decided(notes, B[:7], C))

    def test_no_freeze_and_no_sha_carry_nothing(self):
        rows = self.marks({"actor": "rev1", "sha": A}, {"actor": "rev2", "sha": ""})
        self.assertEqual(verdict_carry.pending(rows, ""), {})
        self.assertEqual(sorted(verdict_carry.pending(rows, C)), ["rev1"])


class ScopeTest(unittest.TestCase):
    """Le report écrit des verdicts que la porte compte : le changer, c'est
    changer la porte de fusion — deux avis de familles distinctes."""

    def test_the_carry_module_is_in_the_critical_scope(self):
        path = "src/ameesh/verdict_carry.py"
        klass = {"class": "light", "files": [{"path": path, "class": "light", "rule": [path]}]}
        self.assertEqual(merge_queue.required_verdicts(klass), 2)


class ExecutorPassTest(unittest.TestCase):
    """La passe de l'exécuteur : configuration relue, reports dits, une même
    erreur dite une seule fois."""

    def test_carried_is_logged_and_a_repeated_error_once(self):
        from ameesh import config, runner
        fake = types.SimpleNamespace(cfg=config.Config(), dry_run=False, runner_id="r", db=None)
        carried = {"lot": 7, "result": "carried", "target": "main", "tip": A,
                   "carried": [{"actor": "rev1", "verdict": "ok", "from": B}]}
        error = {"lot": 8, "result": "error", "detail": "target main not found on origin"}
        quiet = {"lot": 9, "result": "no-repository", "detail": "x"}
        with mock.patch("ameesh.migrations.require_compatible_writer"), \
                mock.patch.object(verdict_carry, "run_pass",
                                  return_value=[carried, error, quiet]) as run_pass, \
                mock.patch.object(runner, "fresh_radar", side_effect=lambda cfg: cfg), \
                mock.patch.object(runner, "log_async") as log:
            runner.Runner.carry_once(fake, None)
            runner.Runner.carry_once(fake, None)
        self.assertEqual(run_pass.call_count, 2)
        lines = [c.args[0] for c in log.call_args_list]
        self.assertEqual(sum("lot #7 verdicts carried" in line for line in lines), 2)
        self.assertEqual(sum("lot #8" in line for line in lines), 1)
        self.assertFalse(any("lot #9" in line for line in lines))


class CarryTest(PgTestCase):
    """Le report de bout en bout, sur Postgres et un vrai dépôt sans origin."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        for name, harness in (("author", "deepseek"), ("rev1", "claude"), ("rev2", "codex")):
            registry.upsert(cls.db, name, harness=harness, host="pc")

    def setUp(self) -> None:
        # #2664 : le banc n'a pas de canon Git, les familles compilées sont
        # épinglées ; le registre seedé en setUpClass reste en place.
        patch_families(self)
        self.repo = Repo()
        self.addCleanup(self.repo.cleanup)
        self.lot = work.add(self.db, title="un lot", branch="lot/9-x",
                            branch_target="main", assignee="author")["id"]
        work.milestone(self.db, self.lot, "frozen", sha=self.repo.reviewed, actor="author")

    def verdict(self, actor, sha, verdict="ok"):
        mark = work.milestone(self.db, self.lot, "verdict", sha=sha, actor=actor,
                              verdict=verdict)
        # #2928 : la porte sensible ne compte que les verdicts à reçu exact.
        full = _git(self.repo.root, "rev-parse", sha)
        harness = FAM[actor]
        self.db.execute(
            "INSERT INTO provenance_receipts"
            " (subject, sha, actor, session, turn, harness, work_item_id)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (subject) DO NOTHING",
            ("milestone:%d" % int(mark["id"]), full, actor, "s-" + harness,
             "t", harness, self.lot))

    def freeze(self, sha):
        work.milestone(self.db, self.lot, "frozen", sha=sha, actor="author")

    def carry(self, **extra):
        return verdict_carry.carry(self.cfg, self.db, self.lot, repo=self.repo.root, **extra)

    def test_hold_excludes_one_reviewer_without_losing_the_other_verdict(self):
        self.verdict("rev1", self.repo.reviewed)
        self.verdict("rev2", self.repo.reviewed)
        self.db.execute("UPDATE agent_registry SET review_hold = %s::jsonb"
                        " WHERE name = 'rev1'", ('{"reason": "rename", "actor": "human:alice"}',))
        self.addCleanup(self.db.execute,
                        "UPDATE agent_registry SET review_hold = NULL WHERE name = 'rev1'")
        frozen = self.repo.merge_target()
        self.freeze(frozen)
        outcome = self.carry(dry_run=True)
        self.assertEqual(outcome["result"], "would-carry", outcome)
        outcome = self.carry()
        self.assertEqual(outcome["result"], "carried", outcome)
        self.assertEqual([m["actor"] for m in self.on(frozen)], ["rev2"])
        self.assertEqual(sorted(m["actor"] for m in self.on(self.repo.reviewed)),
                         ["rev1", "rev2"])

    def test_withdrawal_survives_realigned_freeze_and_late_carry(self):
        seats = storage.of(self.db).work
        seats.designate_reviewer(self.lot, "rev1", actor="human:alice")
        self.verdict("rev1", self.repo.reviewed)
        seats.designate_reviewer(self.lot, "rev1", actor="human:alice", remove=True)
        frozen = self.repo.merge_target()
        self.assertNotEqual(verdict_carry.would_carry(
            self.cfg, self.db, self.lot, frozen, repo=self.repo.root)["result"], "would-carry")
        self.freeze(frozen)
        self.assertNotEqual(self.carry()["result"], "carried")
        self.assertTrue(self.db.query("SELECT withdrawn FROM work_review_withdrawals"
                                     " WHERE work_item_id = %s AND reviewer = 'rev1'",
                                     (self.lot,))[0]["withdrawn"])
        self.assertNotIn("rev1", work.get(self.db, self.lot)["reviewers"])
        with self.assertRaisesRegex(db_mod.DbError, "withdrawn"):
            self.verdict("rev1", frozen)
        seats.designate_reviewer(self.lot, "rev1", actor="human:alice")
        self.assertEqual(self.carry()["result"], "carried")

    def test_withdrawal_during_carry_computation_is_rechecked(self):
        seats = storage.of(self.db).work
        seats.designate_reviewer(self.lot, "rev1", actor="human:alice")
        self.verdict("rev1", self.repo.reviewed)
        frozen = self.repo.merge_target()
        self.freeze(frozen)
        real = verdict_carry._carriable

        def withdraw(*args):
            seats.designate_reviewer(self.lot, "rev1", actor="human:alice", remove=True)
            return real(*args)

        with mock.patch.object(verdict_carry, "_carriable", side_effect=withdraw):
            self.assertNotEqual(self.carry()["result"], "carried")
        self.assertTrue(self.db.query("SELECT withdrawn FROM work_review_withdrawals"
                                     " WHERE work_item_id = %s AND reviewer = 'rev1'",
                                     (self.lot,))[0]["withdrawn"])

    def on(self, sha):
        return [m for m in work.milestones(self.db, self.lot)
                if m["kind"] == "verdict" and m["sha"] == sha]

    def gate(self, sha):
        # la porte de fusion : deux avis de familles distinctes (paquet 67, #2431)
        klass = {"class": lot_class.SENSITIVE, "doubt": False,
                 "files": [{"path": "src/ameesh/merge_queue.py", "class": "sensitive",
                            "rule": ["src/ameesh/merge_queue.py"]}]}
        with mock.patch.object(merge_queue, "actor_family", _family):
            return merge_queue.review_gate(self.db, work.milestones(self.db, self.lot), sha,
                                           repo=self.repo.root, klass=klass,
                                           item=dict(work.get(self.db, self.lot),
                                                     assignee="author"))

    def notes(self, prefix):
        return [e["note"] for e in work.events(self.db, self.lot)
                if str(e.get("note") or "").startswith(prefix)]

    def test_verdicts_follow_a_mechanical_realignment(self):
        self.verdict("rev1", self.repo.reviewed)
        self.verdict("rev2", self.repo.reviewed[:12])
        frozen = self.repo.merge_target()
        self.freeze(frozen)
        self.assertFalse(self.gate(frozen)["ok"])
        result = self.carry()
        self.assertEqual(result["result"], "carried", result)
        carried = self.on(frozen)
        self.assertEqual(sorted(m["actor"] for m in carried), ["rev1", "rev2"])
        self.assertTrue(all(m["note"].startswith(verdict_carry.CARRIED) for m in carried))
        gate = self.gate(frozen)
        self.assertTrue(gate["ok"], gate["why"])
        self.assertEqual(len(self.notes(verdict_carry.CARRIED)), 1)
        self.assertIn("no re-review due", verdict_carry.describe(result))
        again = self.carry()
        self.assertEqual(again["result"], "idle")
        self.assertEqual(len(self.on(frozen)), 2)

    def test_carried_quorum_persists_validation(self):
        self.verdict("rev1", self.repo.reviewed)
        self.verdict("rev2", self.repo.reviewed)
        frozen = self.repo.merge_target()
        self.freeze(frozen)
        with mock.patch.object(merge_queue, "review_gate_for_item",
                               side_effect=lambda *args, **kw: self.gate(frozen)):
            self.assertEqual(self.carry()["result"], "carried")
        self.assertEqual(work.get(self.db, self.lot)["state"], "validated")

    def test_a_block_is_carried_too(self):
        self.verdict("rev1", self.repo.reviewed, "blocked")
        self.verdict("rev2", self.repo.reviewed)
        frozen = self.repo.merge_target()
        self.freeze(frozen)
        self.assertEqual(self.carry()["result"], "carried")
        gate = self.gate(frozen)
        self.assertFalse(gate["ok"])
        self.assertEqual([b["actor"] for b in gate["blocked"]], ["rev1"])

    def test_a_verdict_already_on_the_freeze_is_kept(self):
        self.verdict("rev1", self.repo.reviewed)
        self.verdict("rev2", self.repo.reviewed)
        frozen = self.repo.merge_target()
        self.freeze(frozen)
        self.verdict("rev2", frozen, "blocked")
        result = self.carry()
        self.assertEqual([e["actor"] for e in result["carried"]], ["rev1"])
        rev2 = [m for m in self.on(frozen) if m["actor"] == "rev2"]
        self.assertEqual([m["verdict"] for m in rev2], ["blocked"])

    def test_a_new_change_is_refused_once_and_journaled(self):
        self.verdict("rev1", self.repo.reviewed)
        self.repo.merge_target()
        frozen = _commit(self.repo.root, "lot.txt", "lot, changed\n", "fix")
        self.freeze(frozen)
        result = self.carry()
        self.assertEqual(result["result"], "not-carried")
        self.assertEqual(self.on(frozen), [])
        self.assertEqual(len(self.notes(verdict_carry.NOT_CARRIED)), 1)
        self.assertIn("merge results differ", self.notes(verdict_carry.NOT_CARRIED)[0])
        again = self.carry()
        self.assertEqual((again["result"], again["detail"]),
                         ("idle", "already decided for this freeze"))

    def test_dry_run_writes_nothing(self):
        self.verdict("rev1", self.repo.reviewed)
        frozen = self.repo.merge_target()
        self.freeze(frozen)
        self.assertEqual(self.carry(dry_run=True)["result"], "would-carry")
        self.assertEqual(self.on(frozen), [])

    def test_a_freeze_moved_meanwhile_writes_nothing(self):
        self.verdict("rev1", self.repo.reviewed)
        frozen = self.repo.merge_target()
        self.freeze(frozen)
        real = verdict_carry.realigned

        def refreeze(*args):
            self.freeze(self.repo.reviewed)
            self.freeze(_commit(self.repo.root, "late.txt", "late\n", "late"))
            return real(*args)

        with mock.patch.object(verdict_carry, "realigned", side_effect=refreeze):
            self.assertEqual(self.carry()["result"], "moved")
        self.assertEqual(self.on(frozen), [])

    def test_a_verdict_given_during_the_computation_is_kept(self):
        # R1 cx1, P1-1 : un verdict réel déposé entre le calcul et l'écriture
        # est relu sous le verrou du lot : le sien vaut, pas la copie.
        self.verdict("rev1", self.repo.reviewed)
        self.verdict("rev2", self.repo.reviewed)
        frozen = self.repo.merge_target()
        self.freeze(frozen)
        real = verdict_carry.realigned

        def meanwhile(*args):
            result = real(*args)
            self.verdict("rev1", frozen, "blocked")
            return result

        with mock.patch.object(verdict_carry, "realigned", side_effect=meanwhile):
            result = self.carry()
        self.assertEqual([e["actor"] for e in result["carried"]], ["rev2"])
        rev1 = [m["verdict"] for m in self.on(frozen) if m["actor"] == "rev1"]
        self.assertEqual(rev1, ["blocked"])
        gate = self.gate(frozen)
        self.assertFalse(gate["ok"])
        self.assertEqual([b["actor"] for b in gate["blocked"]], ["rev1"])

    def test_a_concurrent_verdict_waits_for_the_carry_and_wins(self):
        # R1 cx1, P1-1 : la relecture des jalons et la copie tiennent le verrou
        # de ligne du lot. Un verdict réel déposé par une autre connexion
        # pendant l'écriture attend le COMMIT, puis l'emporte à la porte.
        self.verdict("rev1", self.repo.reviewed)
        self.verdict("rev2", self.repo.reviewed)
        frozen = self.repo.merge_target()
        self.freeze(frozen)
        other = db_mod.connect(self.cfg)
        self.addCleanup(other.close)
        done = threading.Event()
        errors: list = []

        def genuine():
            try:
                mark = work.milestone(other, self.lot, "verdict", sha=frozen, actor="rev1",
                                      verdict="blocked")
                # #2928 : l'avis réel porte son reçu exact, comme au runner.
                other.execute(
                    "INSERT INTO provenance_receipts"
                    " (subject, sha, actor, session, turn, harness, work_item_id)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s)"
                    " ON CONFLICT (subject) DO NOTHING",
                    ("milestone:%d" % int(mark["id"]), frozen, "rev1",
                     "s-claude", "t", FAM["rev1"], self.lot))
            except Exception as exc:  # noqa: BLE001 - remonté par l'assertion
                errors.append(exc)
            finally:
                done.set()

        real, calls, waited = verdict_carry.pending, [], []

        def inside(marks, declared):
            calls.append(1)
            if len(calls) == 2:       # la relecture sous le verrou, dans `_write`
                worker = threading.Thread(target=genuine, daemon=True)
                worker.start()
                waited.append(done.wait(1.0))
            return real(marks, declared)

        with mock.patch.object(verdict_carry, "pending", side_effect=inside):
            result = self.carry()
        self.assertTrue(done.wait(30.0))
        self.assertEqual(errors, [])
        self.assertEqual(waited, [False], "le verdict concurrent a dû attendre le COMMIT")
        self.assertEqual(result["result"], "carried")
        gate = self.gate(frozen)
        self.assertFalse(gate["ok"])
        self.assertEqual([b["actor"] for b in gate["blocked"]], ["rev1"])

    def test_the_copy_is_dated_from_its_original(self):
        # R1 cx1, P1-1 : `now()` date le début de transaction. Un verdict réel
        # dont la transaction a commencé avant l'écriture du report doit rester
        # plus récent que la copie : elle garde la date exacte de l'original
        # (l'id départage), même à moins de 1 ms du verdict réel.
        self.verdict("rev1", self.repo.reviewed)
        self.verdict("rev2", self.repo.reviewed)
        frozen = self.repo.merge_target()
        self.freeze(frozen)
        self.assertEqual(self.carry()["result"], "carried")
        marks = work.milestones(self.db, self.lot)
        original = [m for m in marks if m["kind"] == "verdict" and m["actor"] == "rev1"
                    and m["sha"] == self.repo.reviewed][0]
        copy = [m for m in self.on(frozen) if m["actor"] == "rev1"][0]
        self.assertEqual(copy["at_ts"], original["at_ts"])
        self.assertGreater(copy["id"], original["id"])
        self.db.execute("INSERT INTO work_item_milestones (work_item_id, kind, sha, actor,"
                      " verdict, note, at) VALUES (%s, 'verdict', %s, 'rev1', 'blocked',"
                      " 'réel, transaction ouverte avant le report',"
                      " to_timestamp(%s))",
                      (self.lot, frozen, original["at_ts"] + 0.0005))
        # #2928 : cet avis réel porte lui aussi son reçu exact.
        blocked = self.db.query(
            "SELECT id FROM work_item_milestones WHERE work_item_id = %s"
            " AND kind = 'verdict' AND actor = 'rev1' AND verdict = 'blocked'"
            " ORDER BY id DESC LIMIT 1", (self.lot,))[0]
        self.db.execute(
            "INSERT INTO provenance_receipts"
            " (subject, sha, actor, session, turn, harness, work_item_id)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (subject) DO NOTHING",
            ("milestone:%d" % int(blocked["id"]), frozen, "rev1", "s-claude", "t",
             FAM["rev1"], self.lot))
        gate = self.gate(frozen)
        self.assertFalse(gate["ok"])
        self.assertEqual([b["actor"] for b in gate["blocked"]], ["rev1"])

    def test_without_repository_nothing_is_proven(self):
        self.verdict("rev1", self.repo.reviewed)
        self.freeze(self.repo.merge_target())
        result = verdict_carry.carry(self.cfg, self.db, self.lot)
        self.assertEqual(result["result"], "no-repository")

    def test_the_executor_pass_carries(self):
        self.verdict("rev1", self.repo.reviewed)
        frozen = self.repo.merge_target()
        self.freeze(frozen)
        with mock.patch.object(lot_class, "repository", return_value=self.repo.root):
            report = verdict_carry.run_pass(self.cfg, self.db)
        mine = [r for r in report if r["lot"] == self.lot]
        self.assertEqual([r["result"] for r in mine], ["carried"])
        self.assertEqual([m["actor"] for m in self.on(frozen)], ["rev1"])
        with mock.patch.object(lot_class, "repository", return_value=self.repo.root):
            again = verdict_carry.run_pass(self.cfg, self.db)
        self.assertEqual([r for r in again if r["lot"] == self.lot], [])

    def test_the_freeze_command_says_what_it_carried(self):
        from ameesh import mesh_cli
        self.verdict("rev1", self.repo.reviewed)
        frozen = self.repo.merge_target()
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ,
                             dict(self.env(), AGENT_MAIL_NAME="author"),
                             clear=True), \
                mock.patch.object(lot_class, "repository", return_value=self.repo.root), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = mesh_cli.main(["work", "milestone", str(self.lot), "frozen", "--sha",
                                  frozen, "--actor", "author"])
        self.assertEqual(code, 0, err.getvalue())
        self.assertIn("verdicts carried (alignment on main@", out.getvalue())
        self.assertEqual([m["actor"] for m in self.on(frozen)], ["rev1"])

if __name__ == "__main__":
    unittest.main()
