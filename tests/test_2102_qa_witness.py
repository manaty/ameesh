# SPDX-License-Identifier: AGPL-3.0-only
"""#2102 — témoins indépendants de la course détachement / relevé (QA DeepSeek).

Fichier de travail du relecteur, non poussé : il ne rejoue pas les tests de
l'auteur, il reproduit les deux courses que la garde atomique doit fermer.

1. un relevé déjà en cours (sonde de branche) ne doit pas réassocier une
   branche détachée pendant la sonde ;
2. un détachement ne doit pas effacer une réassociation explicite concurrente.
"""
from __future__ import annotations

import os

from ameesh import plan_git, storage, work

from .test_merged_work_item_tracking import _Base, _git
from . import test_merged_work_item_tracking as tracking


class DetachRaceWitnessTest(_Base):
    """Les aides de `MergeSweepTest`, sans hériter de ses tests."""

    commit = tracking.MergeSweepTest.commit
    lot = tracking.MergeSweepTest.lot
    sync = tracking.MergeSweepTest.sync

    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.tmp, "depot")
        os.makedirs(self.repo)
        _git(self.repo, "init", "-q", "-b", "main")
        self.commit("socle")
        _git(self.repo, "checkout", "-q", "-b", "develop")
        _git(self.repo, "config", plan_git.TARGET_GIT_KEY, "develop")
        self.agent("claude1", cwd=self.repo)
        for state in (plan_git._pr_body_backoff, plan_git._pr_body_cache):
            state.clear()
            self.addCleanup(state.clear)

    def branch(self, name):
        _git(self.repo, "checkout", "-q", "-b", name, "develop")
        self.commit(name.replace("/", "-"))
        _git(self.repo, "checkout", "-q", "develop")

    def blocked_lot(self):
        lot = self.lot("vérification après déploiement")
        work.move(self.db, lot["id"], "build", actor="claude1")
        work.move(self.db, lot["id"], "blocked", actor="claude1")
        return lot

    def test_releve_en_cours_ne_reassocie_pas_une_branche_detachee(self):
        lot = self.blocked_lot()
        n = lot["id"]
        name = "lot/%d-supplantee" % n
        self.branch(name)
        real = plan_git.branch_probe

        def probe(repo, branch, target, **kw):
            # le détachement arrive pendant la sonde, après la lecture du nom
            # (le lot n'a pas encore de branche : l'opérateur la nomme)
            work.detach_branch(self.db, n, name, reason="PR supplantée (course)")
            return real(repo, branch, target, **kw)

        plan_git.branch_probe = probe
        calls, results = [], []
        real_attach = work.attach_implicit_branch

        def attach(*args, **kw):
            calls.append(args[2] if len(args) > 2 else kw.get("branch"))
            results.append(real_attach(*args, **kw))
            return results[-1]

        work.attach_implicit_branch = attach
        try:
            self.sync()
        finally:
            plan_git.branch_probe = real
            work.attach_implicit_branch = real_attach
        # le relevé a bien TENTÉ l'association implicite, et la garde l'a refusée
        self.assertEqual(calls, [name], "le relevé n'a pas tenté l'association implicite")
        self.assertEqual(results, [None], "l'association implicite n'a pas été refusée")
        self.assertIsNone(work.get(self.db, n)["branch"],
                          "le relevé a réassocié une branche détachée pendant sa sonde")
        self.assertTrue(work.is_detached(storage.of(self.db), n, name))

    def test_detachement_ne_ravit_pas_une_reassociation_concurrente(self):
        lot = self.blocked_lot()
        n = lot["id"]
        old = "lot/%d-supplantee" % n
        new = "lot/%d-rivale" % n
        self.branch(old)
        work.set_branch(self.db, n, old, actor="orch")
        real_get = work.get
        state = {"done": False}

        def get_then_rival(db, item_id, *a, **kw):
            item = real_get(db, item_id, *a, **kw)
            if int(item_id) == n and not state["done"]:
                state["done"] = True
                storage.of(db).work.set_branch(
                    n, new, None, actor="pilote",
                    note="branch %s (before: %s)" % (new, old))
            return item

        work.get = get_then_rival
        try:
            try:
                work.detach_branch(self.db, n, reason="PR supplantée")
            except work.WorkError:
                pass
        finally:
            work.get = real_get
        self.assertEqual(real_get(self.db, n)["branch"], new,
                         "le détachement a effacé une réassociation concurrente")
