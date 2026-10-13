# SPDX-License-Identifier: AGPL-3.0-only
"""#1104 — une fusion enregistrée **hors file** ouvre le lot de relecture L539.

Constat de l'orchestrateur (13/10) : un seul `review-after-merge:#…` en base
pour ~1019 lots, parce que le lot de relecture n'était ouvert que par
l'atterrissage de la file de fusion. Témoin : une fusion constatée par le
relevé des branches (L118) ou déclarée (`ameesh work merged`) l'ouvre aussi,
une seule fois ; une fusion dont la classe ne peut pas être lue (pas de gel,
dépôt illisible) n'ouvre rien.

Le canon est un **vrai** canon minimal (`default: light`) et la fusion est
constatée par git : la classe est calculée, pas doublée.
"""
from __future__ import annotations

import dataclasses
import os
import subprocess
import unittest
from unittest import mock

from ameesh import plan_git, registry, review_lot, storage, work

from .support import PgTestCase


def _git(repo, *args, env=None):
    base = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
                GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
    base.update(env or {})
    proc = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, env=base)
    if proc.returncode != 0:
        raise AssertionError("git %s : %s" % (" ".join(args), proc.stderr))
    return proc.stdout.strip()


class OpenForMergeRuleTest(unittest.TestCase):
    """La règle d'entrée : déjà ouvert (aucune relecture de classe), un doute
    n'ouvre rien, une lecture impossible ne lève jamais."""

    def opened(self):
        return [{"id": 7, "state": "merged"}]

    def test_un_lot_deja_ouvert_ne_relit_pas_la_classe(self):
        ops = mock.Mock()
        ops.by_issue_ref.return_value = self.opened()
        with mock.patch.object(review_lot.storage, "of", return_value=mock.Mock(work=ops)), \
                mock.patch.object(review_lot.work, "get", return_value={"id": 1}), \
                mock.patch.object(review_lot, "class_at_merge") as klass:
            out = review_lot.open_for_merge(None, 1)
        self.assertFalse(out["created"])
        self.assertEqual(out["id"], 7)
        klass.assert_not_called()

    def test_un_doute_n_ouvre_rien(self):
        ops = mock.Mock()
        ops.by_issue_ref.return_value = []
        ops.open_by_ref.return_value = []
        ops.events.return_value = []
        with mock.patch.object(review_lot.storage, "of", return_value=mock.Mock(work=ops)), \
                mock.patch.object(review_lot.work, "get",
                                  return_value={"id": 1, "assignee": "a"}), \
                mock.patch.object(review_lot, "class_at_merge",
                                  return_value={"class": "sensitive", "why": "pas de gel"}), \
                mock.patch.object(review_lot.work, "add") as add:
            out = review_lot.open_for_merge(None, 1)
        self.assertFalse(out["created"])
        self.assertEqual(out["class"], "sensitive")
        add.assert_not_called()

    def test_une_lecture_impossible_ne_leve_pas(self):
        with mock.patch.object(review_lot.storage, "of",
                               side_effect=RuntimeError("base injoignable")):
            out = review_lot.open_for_merge(None, 3)
        self.assertFalse(out["created"])
        self.assertIn("RuntimeError", out["why"])


class _Witness(PgTestCase):
    """Un dépôt réel, un canon minimal (`light` par défaut, `**/*.sql`
    sensible), un agent placé dessus : de quoi constater une vraie fusion et
    lire une vraie classe."""

    def setUp(self):
        super().setUp()
        self.root = os.path.join(self.tmp, "canon-test")
        os.makedirs(self.root)
        self.write(os.path.join(self.root, "index.md"),
                   "---\ntype: Index\ntitle: canon de test\n---\n")
        self.write(os.path.join(self.root, "federation.yaml"),
                   "id: test\nmembers: []\nreview_policies:\n  risk_classes:\n"
                   "    default: light\n    rules:\n"
                   "      - paths: ['**/*.sql']\n        class: sensitive\n")
        patcher = mock.patch.object(
            self.db, "cfg",
            dataclasses.replace(self.db.cfg, canon=self.root, canon_untrusted=True,
                                require_responsible=False))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.repo = os.path.join(self.tmp, "depot")
        os.makedirs(self.repo)
        _git(self.repo, "init", "-q", "-b", "main")
        self.commit("socle")
        _git(self.repo, "checkout", "-q", "-b", "develop")
        _git(self.repo, "config", plan_git.TARGET_GIT_KEY, "develop")
        registry.upsert(self.db, "claude1", harness="deepseek", host=self.cfg.host,
                        cwd=self.repo)

    def write(self, path, text):
        with open(path, "w") as fh:
            fh.write(text)

    def commit(self, name, text=None):
        path = os.path.join(self.repo, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.write(path, text if text is not None else name + "\n")
        _git(self.repo, "add", name)
        _git(self.repo, "commit", "-q", "-m", "ajoute %s" % name)

    def lot(self, title="un lot leger"):
        return work.add(self.db, title=title, app="ameesh", assignee="claude1",
                        actor="orch")["id"]

    def frozen_on_branch(self, lot, branch, name="docs/preuve.md"):
        """Une branche portant un changement `light`, gelée sur sa pointe."""
        _git(self.repo, "checkout", "-q", "-b", branch, "develop")
        self.commit(name)
        sha = _git(self.repo, "rev-parse", "HEAD")
        work.milestone(self.db, lot, "frozen", sha=sha, actor="claude1")
        return sha

    def review_lots(self, lot):
        return storage.of(self.db).work.by_issue_ref(review_lot.ref_of(lot))


class SweepWitnessTest(_Witness):
    """Le relevé des branches (L118), constat d'une fusion manuelle locale."""

    def test_une_fusion_locale_ouvre_le_lot_de_relecture(self):
        lot = self.lot()
        self.frozen_on_branch(lot, "lot/1104-preuve")
        _git(self.repo, "checkout", "-q", "develop")
        _git(self.repo, "merge", "-q", "--no-ff", "-m", "ameesh-work: %d" % lot,
             "lot/1104-preuve")
        report = {r["work_item"]: r for r in plan_git.sync_branches(
            self.db, host=self.cfg.host)["results"]}
        self.assertEqual(report[lot]["result"], "merged")
        rows = self.review_lots(lot)
        self.assertEqual(len(rows), 1, rows)
        item = work.get(self.db, rows[0]["id"])
        self.assertEqual((item["state"], item["assignee"], item["type"], item["app"]),
                         ("intake", None, "evolution", "ameesh"))
        self.assertIn("Classe : light", item["body"])
        self.assertNotIn("no file changed", item["body"])   # classe du diff fusionné
        self.assertIn("23:59 Europe/Paris", item["body"])
        self.assertIn("codex", item["body"])       # famille opposée à deepseek

    def test_une_fusion_sensible_n_ouvre_rien(self):
        """Le piège du gel devenu ancêtre : `cible..gel` est vide après la
        fusion et se lirait « light ». La classe est lue sur le diff que la
        fusion a fait entrer — `**/*.sql` est sensible, rien ne s'ouvre."""
        lot = self.lot()
        self.frozen_on_branch(lot, "lot/1104-sensible", name="db/x.sql")
        _git(self.repo, "checkout", "-q", "develop")
        _git(self.repo, "merge", "-q", "--no-ff", "-m", "ameesh-work: %d" % lot,
             "lot/1104-sensible")
        report = {r["work_item"]: r for r in plan_git.sync_branches(
            self.db, host=self.cfg.host)["results"]}
        self.assertEqual(report[lot]["result"], "merged")
        self.assertEqual(self.review_lots(lot), [])

    def test_la_relecture_ne_s_ouvre_qu_une_fois(self):
        lot = self.lot()
        self.frozen_on_branch(lot, "lot/1104-une-fois")
        _git(self.repo, "checkout", "-q", "develop")
        _git(self.repo, "merge", "-q", "--no-ff", "-m", "ameesh-work: %d" % lot,
             "lot/1104-une-fois")
        self.assertEqual(plan_git.sync_branches(self.db, host=self.cfg.host)["merged"], 1)
        self.assertEqual(plan_git.sync_branches(self.db, host=self.cfg.host)["merged"], 0)
        self.assertEqual(len(self.review_lots(lot)), 1)


class DeclaredMergeWitnessTest(_Witness):
    """`ameesh work merged` : une fusion déclarée, sans PR ni file."""

    def test_work_merged_ouvre_le_lot_de_relecture(self):
        lot = self.lot()
        self.frozen_on_branch(lot, "lot/1104-declare")
        done = work.merged(self.db, lot, sha="abcdef1234", note="fusion locale",
                           actor="claude1")
        self.assertEqual(done["result"], "merged")
        rows = self.review_lots(lot)
        self.assertEqual(len(rows), 1, rows)
        self.assertIn("Classe : light", work.get(self.db, rows[0]["id"])["body"])

    def test_sans_gel_la_classe_est_un_doute_et_rien_ne_s_ouvre(self):
        """La classe se lit sur le diff de la fusion : sans gel, elle est
        inconnue, et un doute n'ouvre pas de lot de relecture (même règle que
        la file)."""
        lot = self.lot()
        self.assertEqual(work.merged(self.db, lot, sha="abcdef1234",
                                     actor="claude1")["result"], "merged")
        self.assertEqual(self.review_lots(lot), [])

    def test_un_lot_sensible_n_ouvre_rien(self):
        """Une classe supérieure au `light` n'ouvre rien : la relecture
        d'après fusion est celle des lots légers (canon §2)."""
        lot = self.lot()
        self.frozen_on_branch(lot, "lot/1104-sensible", name="db/x.sql")
        self.assertEqual(work.merged(self.db, lot, sha="abcdef1234",
                                     actor="claude1")["result"], "merged")
        self.assertEqual(self.review_lots(lot), [])
