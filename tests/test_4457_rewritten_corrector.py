# SPDX-License-Identifier: AGPL-3.0-only
"""#4457 — le correcteur d'un correctif réécrit après le gel n'est pas redésigné.

Cas réels #4445 (gel #24857, 1dea508d) et #4446 (gel #24907, 45fa46a3) : le
relecteur cx3 avait poussé un correctif **après** le gel, puis l'auteur l'a
réécrit depuis le B en citant le commit retiré. Le commit d'origine n'était
dans aucune plage gelée : la lecture de la tête ne le voyait plus, et la
désignation suivante redésignait cx3. Le §2 l'exclut pourtant **par son nom**
(« exclusion nominative ») tout en laissant sa famille éligible : un autre
agent relit.

Le reçu exact du commit retiré est la preuve machine : il nomme l'acteur, et
la topologie Git (le commit descend de la tête gelée, il n'est plus ancêtre de
la nouvelle tête) prouve que l'auteur l'a réécrit. Aucune note n'est lue.
"""
from __future__ import annotations

import time
from types import SimpleNamespace
from unittest import mock

from ameesh import (fetch_cache, merge_queue, registry, review_authors,
                    review_designation as rd, work)

from .support import PgTestCase
from .test_2447_review_designation_activation import _ActivationFixture

FAMILIES = ("claude", "codex", "deepseek")


class RewrittenCorrectorTest(_ActivationFixture, PgTestCase):
    """Un lot gelé, un correctif de relecteur retiré, puis un re-gel."""

    def setUp(self):
        super().setUp()
        registry.upsert(self.db, "cx3", harness="codex", host=self.cfg.host,
                        cwd=self.repo.root, status="idle")
        self.db.execute("UPDATE agent_registry SET team = 'ameesh'"
                        " WHERE name = %s", ("cx3",))

    def commit_receipt(self, actor, harness, sha):
        """Le reçu exact d'un commit hors tour (work_item_id reste NULL)."""
        self.db.execute(
            "INSERT INTO provenance_receipts"
            " (subject, sha, actor, session, turn, harness, work_item_id)"
            " VALUES (%s, %s, %s, %s, %s, %s, NULL)"
            " ON CONFLICT (subject) DO NOTHING",
            ("commit:" + sha, sha, actor, "s-" + harness, "t", harness))

    def _milestone(self, kind, sha, *, actor="author", verdict=None):
        """Jalon direct : le témoin des 500 reçus ne paie pas le hook de gel."""
        rows = self.db.query(
            "INSERT INTO work_item_milestones"
            " (work_item_id, kind, sha, actor, verdict)"
            " VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (self.lot, kind, sha, actor, verdict))
        return int(rows[0]["id"])

    def verdict_receipt(self, mark, harness):
        self.db.execute(
            "INSERT INTO provenance_receipts"
            " (subject, sha, actor, session, turn, harness, work_item_id)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (subject) DO NOTHING",
            ("milestone:%d" % int(mark["id"]), self.frozen, mark["actor"],
             "s-" + harness, "t", harness, self.lot))

    def _prepare_rewrite(self):
        """F1, correctif de cx3 sur F1, réécriture par l'auteur : la tête B."""
        head_a = self.frozen
        self.freeze()                       # F1 désigne r1 (claude) et r2 (codex)
        # Le correctif du relecteur est poussé APRÈS le gel, sur sa tête.
        self.repo.write("src/ameesh/mail.py", "lot\ncx3 petit correctif\n")
        fix = self.repo.commit("cx3: correctif de relecture", agent="cx3")
        self.commit_receipt("cx3", "codex", fix)
        # Le reçu est daté avant le verdict : la fenêtre de recherche couvre
        # le tour de relecture, et les reçus de test peuvent se placer après.
        self.db.execute(
            "UPDATE provenance_receipts SET at = now() - interval '10 minutes'"
            " WHERE subject = %s", ("commit:" + fix,))
        work.milestone(self.db, self.lot, "verdict", sha=head_a, actor="cx3",
                       verdict="blocked")
        # L'auteur le réécrit depuis le B : le commit du relecteur n'est plus
        # ancêtre d'aucune tête, et le contenu diffère (pas de report).
        self.repo.git("reset", "--hard", head_a)
        self.repo.write("src/ameesh/mail.py",
                        "lot\ncx3 petit correctif\nauteur : reprise\n")
        head_b = self.repo.commit("author: reprise du correctif", agent="author")
        self.receipts(commits=[head_b])
        self.frozen = head_b
        return head_a, fix, head_b

    def _rewrite_after_the_freeze(self):
        head_a, fix, head_b = self._prepare_rewrite()
        _mark, out = self.freeze()          # F2
        return head_a, fix, head_b, out

    def later_receipts(self, count, *, actor="cx3", harness="codex"):
        """`count` reçus de commits absents, après le correctif, avant F2.

        Un seul INSERT…SELECT : 500 insertions unitaires coûtaient ~20 s au
        témoin #4457 (gel #25249), pour un test qui doit rester sous 15 s.
        """
        self.db.execute(
            "INSERT INTO provenance_receipts"
            " (subject, sha, actor, session, turn, harness, work_item_id, at)"
            " SELECT 'commit:' || sha, sha, %s, %s, %s, %s, NULL,"
            "        now() - interval '9 minutes' + (i * interval '1 second')"
            "   FROM (SELECT i, lpad(to_hex(%s + i), 40, '0') AS sha"
            "           FROM generate_series(0, %s) AS s(i)) AS g"
            " ON CONFLICT (subject) DO NOTHING",
            (actor, "s-" + harness, "t", harness, 0x44570000, count - 1))

    def test_a_rewritten_corrector_is_not_redesignated_and_its_family_stays(self):
        _head_a, _fix, _head_b, out = self._rewrite_after_the_freeze()
        self.assertEqual(out["state"], rd.DESIGNATED, out)
        names = [row["name"] for row in out["reviewers"]]
        self.assertNotIn("cx3", names)
        # Le §2 laisse la famille codex éligible : r2, de la même famille, relit.
        self.assertIn("r2", names)
        self.assertIn("r1", names)
        self.assertIn("corrector(s) excluded: cx3", out["why"])

    def test_the_merge_gate_refuses_the_rewritten_corrector(self):
        _head_a, _fix, head_b, _out = self._rewrite_after_the_freeze()
        harnesses = {"r1": "claude", "r2": "codex", "cx3": "codex"}
        for actor, harness in harnesses.items():
            mark = work.milestone(self.db, self.lot, "verdict", sha=head_b,
                                  actor=actor, verdict="ok")
            self.verdict_receipt(mark, harness)
        klass = {"class": "sensitive", "doubt": False,
                 "files": [{"path": "src/ameesh/mail.py",
                            "class": "sensitive",
                            "rule": ["src/ameesh/*"]}],
                 "repo": self.repo.root, "target": self.base,
                 "base_sha": self.base}
        out = merge_queue.review_gate(
            self.db, work.milestones(self.db, self.lot), head_b,
            repo=self.repo.root, klass=klass, item=work.get(self.db, self.lot),
            required=2, families=FAMILIES)
        self.assertFalse(out.get("error"), out)
        self.assertIn("cx3", out["corrections"])
        self.assertNotIn("cx3", [entry["actor"] for entry in out["accepted"]])
        self.assertEqual([entry["actor"] for entry in out["accepted"]],
                         ["r1", "r2"])

    def test_more_than_500_later_receipts_do_not_hide_the_corrector(self):
        # B/P2 cx3 : une lecture tronquée à 500 reçus (tri DESC, tous lots)
        # perdait le correctif sous l'activité ultérieure de l'agent. Témoin
        # direct (jalons insérés sans hook de gel) pour rester sous 15 s : le
        # hook de gel pèse ~80 s à lui seul dans le profil du gel #25249.
        head_a = self.frozen
        self._milestone("frozen", head_a)
        self.repo.write("src/ameesh/mail.py", "lot\ncx3 petit correctif\n")
        fix = self.repo.commit("cx3: correctif de relecture", agent="cx3")
        self.commit_receipt("cx3", "codex", fix)
        self.db.execute(
            "UPDATE provenance_receipts SET at = now() - interval '10 minutes'"
            " WHERE subject = %s", ("commit:" + fix,))
        self._milestone("verdict", head_a, actor="cx3", verdict="blocked")
        self.repo.git("reset", "--hard", head_a)
        self.repo.write("src/ameesh/mail.py",
                        "lot\ncx3 petit correctif\nauteur : reprise\n")
        head_b = self.repo.commit("author: reprise du correctif", agent="author")
        self.receipts(commits=[head_b])
        self.frozen = head_b
        self.later_receipts(501)
        freeze_id = self._milestone("frozen", head_b)
        freeze = self.db.query(
            "SELECT id, sha, extract(epoch FROM at)::float8 AS at_ts"
            "  FROM work_item_milestones WHERE id = %s", (freeze_id,))[0]
        found = review_authors._rewritten_after_freeze(
            self.db, work.get(self.db, self.lot), freeze, self.repo.root,
            head=head_b, fetch=False)
        self.assertIn("cx3", found)
        # La porte suit la même lecture, malgré les 501 reçus absents.
        harnesses = {"r1": "claude", "r2": "codex", "cx3": "codex"}
        for actor, harness in harnesses.items():
            mark_id = self._milestone("verdict", head_b, actor=actor,
                                      verdict="ok")
            self.verdict_receipt({"id": mark_id, "actor": actor}, harness)
        klass = {"class": "sensitive", "doubt": False,
                 "files": [{"path": "src/ameesh/mail.py",
                            "class": "sensitive",
                            "rule": ["src/ameesh/*"]}],
                 "repo": self.repo.root, "target": self.base,
                 "base_sha": self.base}
        with mock.patch.object(review_authors, "_forge_fetch",
                               return_value=None):
            gate = merge_queue.review_gate(
                self.db, work.milestones(self.db, self.lot), head_b,
                repo=self.repo.root, klass=klass, item=work.get(self.db, self.lot),
                required=2, families=FAMILIES)
        self.assertFalse(gate.get("error"), gate)
        self.assertIn("cx3", gate["corrections"])
        self.assertNotIn("cx3", [entry["actor"] for entry in gate["accepted"]])

    def test_absent_receipts_are_fetched_once_per_host(self):
        # B/P2 auditeur : un SHA absent du clone ne doit pas être redemandé à
        # la forge à chaque lecture ; la deuxième lecture ne fait aucun fetch.
        _head_a, _fix, head_b = self._prepare_rewrite()
        self.later_receipts(10)
        item = work.get(self.db, self.lot)
        freeze = {"id": 999999, "sha": head_b}
        with mock.patch.object(review_authors, "_forge_fetch",
                               return_value=False) as forge:
            first = review_authors._rewritten_after_freeze(
                self.db, item, freeze, self.repo.root, head=head_b, fetch=True)
            self.assertIn("cx3", first)
            calls = forge.call_count
            self.assertGreater(calls, 0)
            self.assertLessEqual(calls, 10)
            again = review_authors._rewritten_after_freeze(
                self.db, item, freeze, self.repo.root, head=head_b, fetch=True)
            self.assertIn("cx3", again)
            self.assertEqual(forge.call_count, calls)

    def test_the_scan_honours_the_sweep_deadline(self):
        # B/P2 auditeur : l'échéance des balayages d'auctorialité s'applique à
        # cette boucle ; épuisée, elle refuse la lecture, elle ne la continue
        # pas.
        _head_a, _fix, head_b = self._prepare_rewrite()
        self.later_receipts(10)
        with self.assertRaises(review_authors.SweepDeadline):
            with review_authors.sweep_deadline(0):
                review_authors._rewritten_after_freeze(
                    self.db, work.get(self.db, self.lot),
                    {"id": 999999, "sha": head_b}, self.repo.root,
                    head=head_b, fetch=True)

    def _only_absent_receipt(self):
        """Une tête B et un seul reçu, absent : le fetch est le dernier."""
        _head_a, fix, head_b = self._prepare_rewrite()
        self.db.execute("DELETE FROM provenance_receipts WHERE subject = %s",
                        ("commit:" + fix,))
        self.later_receipts(1)
        return head_b

    def test_a_slow_fetch_cannot_exceed_the_remaining_time(self):
        # B/P2 cx3 : le temps restant est transmis au fetch ; une récupération
        # lente sur le dernier reçu ne conclut pas sans propager l'expiration.
        head_b = self._only_absent_receipt()

        def slow(repo, sha, *, timeout=None):
            self.assertIsNotNone(timeout)
            time.sleep(0.25)
            return False

        with mock.patch.object(review_authors, "_forge_fetch", side_effect=slow):
            with self.assertRaises(review_authors.SweepDeadline):
                with review_authors.sweep_deadline(0.2):
                    review_authors._rewritten_after_freeze(
                        self.db, work.get(self.db, self.lot),
                        {"id": 999999, "sha": head_b}, self.repo.root,
                        head=head_b, fetch=True)
        # Une coupure par l'échéance n'est pas un refus net : rien à mémoriser.
        self.assertFalse(fetch_cache.refused(
            self.repo.root, "%040x" % 0x44570000,
            state_dir=self.cfg.state_dir))

    def test_a_slow_fetch_cannot_exceed_the_local_budget(self):
        head_b = self._only_absent_receipt()

        def slow(repo, sha, *, timeout=None):
            time.sleep(0.25)
            return False

        with mock.patch.object(review_authors, "REWRITTEN_SCAN_BUDGET_S", 0.2), \
                mock.patch.object(review_authors, "_forge_fetch", side_effect=slow):
            with self.assertRaises(review_authors.review_provenance.ProvenanceError):
                review_authors._rewritten_after_freeze(
                    self.db, work.get(self.db, self.lot),
                    {"id": 999999, "sha": head_b}, self.repo.root,
                    head=head_b, fetch=True)
        self.assertFalse(fetch_cache.refused(
            self.repo.root, "%040x" % 0x44570000,
            state_dir=self.cfg.state_dir))

    def test_only_a_clean_refusal_is_cached(self):
        # B/P2 auditeur : un fetch non lancé ou coupé (None) ne doit pas être
        # mémorisé « refusé » pour 6 h ; un code non nul (False) l'est.
        # #4679 : un reçu introuvable sans rattachement de lot ne prouve rien
        # sur cette tête : il ne fait pas de son acteur un correcteur (le gel
        # le dit à part, `unattached`). Le cache garde son sens.
        head_b = self._only_absent_receipt()
        dummy = "%040x" % 0x44570000
        with mock.patch.object(review_authors, "_forge_fetch", return_value=None):
            out = review_authors._rewritten_after_freeze(
                self.db, work.get(self.db, self.lot),
                {"id": 999999, "sha": head_b}, self.repo.root,
                head=head_b, fetch=True)
        self.assertEqual(out, {})
        self.assertFalse(fetch_cache.refused(
            self.repo.root, dummy, state_dir=self.cfg.state_dir))
        with mock.patch.object(review_authors, "_forge_fetch", return_value=False):
            out = review_authors._rewritten_after_freeze(
                self.db, work.get(self.db, self.lot),
                {"id": 999999, "sha": head_b}, self.repo.root,
                head=head_b, fetch=True)
        self.assertEqual(out, {})
        self.assertTrue(fetch_cache.refused(
            self.repo.root, dummy, state_dir=self.cfg.state_dir))

    def test_a_transport_error_is_not_an_object_refusal(self):
        # B/P2 cx2 : un 128 de DNS/connexion/auth n'est pas « objet non
        # servi » ; le cache négatif ne doit pas le figer six heures.
        sha = "a" * 40
        transport = SimpleNamespace(
            returncode=128, stdout=b"",
            stderr=b"fatal: unable to access: Could not resolve host: forge")
        refused = SimpleNamespace(
            returncode=128, stdout=b"",
            stderr=b"fatal: remote error: upload-pack: not our ref " + sha.encode())
        with mock.patch.object(review_authors.plan_git, "local_commit",
                               return_value=None), \
                mock.patch.object(review_authors.plan_git, "forge_remote",
                                 return_value="origin"):
            with mock.patch.object(review_authors.plan_git, "_git",
                                   return_value=transport):
                self.assertIsNone(
                    review_authors._forge_fetch(self.repo.root, sha))
            with mock.patch.object(review_authors.plan_git, "_git",
                                   return_value=refused):
                self.assertFalse(
                    review_authors._forge_fetch(self.repo.root, sha))

    def test_a_transport_error_can_retry(self):
        # Panne réseau puis objet récupérable : le premier appel est un doute,
        # le second réessaie et trouve l'objet.
        sha = "b" * 40
        transport = SimpleNamespace(
            returncode=128, stdout=b"",
            stderr=b"fatal: unable to access: Could not resolve host: forge")
        success = SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        with mock.patch.object(review_authors.plan_git, "forge_remote",
                               return_value="origin"), \
                mock.patch.object(review_authors.plan_git, "local_commit",
                                  side_effect=[None, None, sha]), \
                mock.patch.object(review_authors.plan_git, "_git",
                                  side_effect=[transport, success]):
            self.assertIsNone(
                review_authors._forge_fetch(self.repo.root, sha))
            self.assertTrue(
                review_authors._forge_fetch(self.repo.root, sha))

