# SPDX-License-Identifier: AGPL-3.0-only
"""#2442 : un réalignement mécanique sur la cible ne redemande pas de verdict.

Témoins : le critère (fusions d'essai propres et même arbre sur la pointe de la
cible) — fusion de la cible, rebasage, cible avancée depuis, commit de l'auteur,
fusion « maligne », résolution de conflit ; les candidats au report (dernier
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
import types
import unittest
from unittest import mock

from ameesh import lot_class, merge_queue, registry, verdict_carry, work

from .support import PgTestCase

A = "a" * 40
B = "b" * 40
C = "c" * 40

ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
           GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")

FAM = {"author": "deepseek", "rev1": "claude", "rev2": "codex"}


def _family(db, name):
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
        self.assertEqual(self.check(frozen), (True, "same merge result"))

    def test_the_target_may_have_moved_since(self):
        frozen = self.repo.merge_target()
        _git(self.repo.root, "switch", "-q", "main")
        t2 = _commit(self.repo.root, "later.txt", "later\n", "t2")
        self.assertTrue(self.check(frozen, t2)[0])

    def test_a_rebase_on_the_target_is_a_realignment(self):
        _git(self.repo.root, "switch", "-q", "-c", "rebased", "main")
        _git(self.repo.root, "cherry-pick", self.repo.reviewed)
        self.assertTrue(self.check(_git(self.repo.root, "rev-parse", "HEAD"))[0])

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
        fake = types.SimpleNamespace(cfg=config.Config(), dry_run=False, runner_id="r")
        carried = {"lot": 7, "result": "carried", "target": "main", "tip": A,
                   "carried": [{"actor": "rev1", "verdict": "ok", "from": B}]}
        error = {"lot": 8, "result": "error", "detail": "target main not found on origin"}
        quiet = {"lot": 9, "result": "no-repository", "detail": "x"}
        with mock.patch.object(verdict_carry, "run_pass",
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
        self.repo = Repo()
        self.addCleanup(self.repo.cleanup)
        self.lot = work.add(self.db, title="un lot", branch="lot/9-x",
                            branch_target="main", assignee="author")["id"]
        work.milestone(self.db, self.lot, "frozen", sha=self.repo.reviewed, actor="author")

    def verdict(self, actor, sha, verdict="ok"):
        work.milestone(self.db, self.lot, "verdict", sha=sha, actor=actor, verdict=verdict)

    def freeze(self, sha):
        work.milestone(self.db, self.lot, "frozen", sha=sha, actor="author")

    def carry(self, **extra):
        return verdict_carry.carry(self.cfg, self.db, self.lot, repo=self.repo.root, **extra)

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
        with mock.patch.dict(os.environ, self.env(), clear=True), \
                mock.patch.object(lot_class, "repository", return_value=self.repo.root), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = mesh_cli.main(["work", "milestone", str(self.lot), "frozen", "--sha",
                                  frozen, "--actor", "author"])
        self.assertEqual(code, 0, err.getvalue())
        self.assertIn("verdicts carried (alignment on main@", out.getvalue())
        self.assertEqual([m["actor"] for m in self.on(frozen)], ["rev1"])

if __name__ == "__main__":
    unittest.main()
