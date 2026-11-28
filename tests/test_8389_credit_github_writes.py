# SPDX-License-Identifier: AGPL-3.0-only
"""#8389 : toutes les écritures GitHub de la projection débitent le crédit.

#8357 bornait les commentaires d'étape ; les créations d'issues, les PATCH,
#les cartes du plan et les épiques de sujets passaient encore par `gh api`
#sans plafond ni fenêtre mémorisée. Le faux `gh` de ces témoins ne fait aucun
#appel réseau : un passage de 100 créations doit rester borné, et un 403
#secondaire doit arrêter la passe sans que le geste suivant parte.
"""
from __future__ import annotations

import unittest
from unittest import mock

from ameesh import plan_github as pg
from ameesh import subject_github, work, work_subjects

from .fake_github import FakeGh
from .test_8193_work_subjects import _SubjectBase
from .test_8195_subject_github import REPO as SUBJECT_REPO
from .test_l126_issues import PUBLIC, _Base as LotBase
from .test_plan import REPO as PLAN_REPO, _PlanDb


class Clock:
    """#8357 : une horloge réglable ; aucun témoin n'attend la vraie."""

    def __init__(self, now: float = 1000.0):
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


class ApprovingGh(FakeGh):
    """Le faux `gh`, avec une fenêtre de refus secondaire actionnable."""

    def __init__(self):
        super().__init__()
        self.refuse_patch = False
        self.patches = 0

    def run(self, argv, stdin=None, timeout=None):
        if (self.refuse_patch and argv[:3] == ["api", "-X", "PATCH"]
                and len(argv) > 3):
            self.patches += 1
            return 1, ("HTTP/2.0 403 Forbidden\r\n"
                       "Retry-After: 120\r\n"
                       "X-Ratelimit-Remaining: 4999\r\n\r\n"
                       '{"message":"You have exceeded a secondary rate limit."}'), ""
        return super().run(argv, stdin, timeout)


class IssueWriteCreditTest(LotBase):
    """Les créations et PATCH d'issues de `project_lots` (#L126)."""

    def make_writes(self, clock, *, writes_per_pass=pg.MAX_WRITES_PER_PASS,
                    writes_per_hour=pg.MAX_WRITES_PER_HOUR):
        self.sleeps = []

        def sleep(seconds):
            self.sleeps.append(seconds)
            clock.advance(seconds)

        reads = pg.SharedReads(self.state, clock=clock)
        return pg._Writes(self.gh, reads=reads, clock=clock, sleep=sleep,
                          writes_per_pass=writes_per_pass,
                          writes_per_hour=writes_per_hour)

    def project_with(self, clock, **kw):
        writes = self.make_writes(clock, **kw)
        with mock.patch.object(pg, "_writes_for", return_value=writes):
            return self.project()

    def issue_posts(self, gh=None):
        gh = self.gh if gh is None else gh
        return [call for call in gh.writes()
                if call[:3] == ["api", "-X", "POST"]
                and len(call) > 3 and call[3] == "repos/%s/issues" % PUBLIC]

    def test_cent_lots_trois_passages_une_heure_borne_les_post(self):
        # Le témoin du lot : 100 créations, trois passages simulés dans
        # l'heure. Le plafond par passage doit tenir à chaque fois, et le
        # troisième passage ne doit pas rattraper tout le reste d'un coup.
        for n in range(100):
            work.add(self.db, title="Lot %03d" % n, app="web")
        posted = 0
        created = 0
        for index in range(3):
            self.gh.reset_calls()
            report = self.project_with(Clock(1000.0 + 30.0 * index))
            posts = [call for call in self.gh.writes()
                     if call[:2] == ["api", "-X"] and call[2] in ("POST", "PATCH")]
            posted += len(posts)
            created += report["counts"]["created"]
            self.assertLessEqual(len(posts), pg.MAX_WRITES_PER_PASS)
        self.assertGreater(created, 0)
        self.assertLess(created, 100)
        self.assertLessEqual(posted, 3 * pg.MAX_WRITES_PER_PASS)
        # Le crédit est le même fichier pour tous les gestes de la projection.
        self.assertTrue(pg.SharedReads(self.state, clock=Clock(1030.0))
                        .content_credit_available(
                            limit=pg.MAX_WRITES_PER_HOUR, window=pg.CONTENT_CREDIT_WINDOW_S,
                            name=pg.CONTENT_CREDIT_NAME))

    def test_credit_horaire_partage_arrete_puis_reprend_a_la_fenetre(self):
        for n in range(10):
            work.add(self.db, title="Lot %02d" % n, app="web")
        # Les étiquettes sont des écritures : les créer hors du témoin pour
        # que le plafond horaire ne mesure que les créations d'issues.
        dry = self.project(dry_run=True)
        self.gh.repo(PUBLIC)["labels"].extend(dry["labels_created"])
        self.gh.reset_calls()
        first = self.project_with(Clock(4000.0), writes_per_hour=5)
        self.assertEqual(first["counts"]["created"], 5)
        self.gh.reset_calls()
        second = self.project_with(Clock(4050.0), writes_per_hour=5)
        self.assertEqual(second["counts"]["created"], 0)
        self.assertEqual(self.gh.writes(), [])
        third = self.project_with(
            Clock(4000.0 + pg.CONTENT_CREDIT_WINDOW_S + 1), writes_per_hour=5)
        self.assertEqual(third["counts"]["created"], 5)

    def test_patch_borne_et_deferre(self):
        lots = [work.add(self.db, title="Lot %d" % n, app="web") for n in range(3)]
        self.project_with(Clock(1000.0))
        for lot in lots:
            self.db.execute("UPDATE work_items SET title = %s WHERE id = %s",
                            ("Renommé %d" % lot["id"], lot["id"]))
        self.gh.reset_calls()
        report = self.project_with(Clock(2000.0), writes_per_pass=2,
                                   writes_per_hour=1000)
        patches = [call for call in self.gh.writes()
                   if call[:3] == ["api", "-X", "PATCH"]]
        self.assertEqual(len(patches), 2)
        self.assertEqual(report["counts"]["updated"], 2)
        self.assertEqual(report["counts"]["deferred"], 1)

    def test_403_secondaire_arrete_et_le_geste_suivant_ne_part_pas(self):
        class RefusingIssueGh(FakeGh):
            def __init__(self):
                super().__init__()
                self.issue_posts = 0

            def run(self, argv, stdin=None, timeout=None):
                if (argv[:3] == ["api", "-X", "POST"] and len(argv) > 3
                        and argv[3] == "repos/%s/issues" % PUBLIC):
                    self.issue_posts += 1
                    if self.issue_posts == 1:
                        return 1, ("HTTP/2.0 403 Forbidden\r\n"
                                   "Retry-After: 120\r\n"
                                   "X-Ratelimit-Remaining: 4999\r\n\r\n"
                                   '{"message":"You have exceeded a secondary rate limit."}'), ""
                return super().run(argv, stdin, timeout)

        gh = RefusingIssueGh()
        self.gh = gh
        work.add(self.db, title="Premier", app="web")
        work.add(self.db, title="Second", app="web")
        first = self.project_with(Clock(5000.0))
        self.assertEqual(gh.issue_posts, 1)
        self.assertTrue(first["errors"])
        self.assertIsNotNone(pg.SharedReads(self.state, clock=Clock(5000.0)).blocked())
        gh.reset_calls()
        second = self.project_with(Clock(5100.0))
        self.assertEqual(gh.issue_posts, 1)          # refusé sans requête
        self.assertFalse([call for call in gh.writes()
                          if call[:3] == ["api", "-X", "POST"]])
        self.assertTrue(second["counts"]["deferred"])
        # Le Retry-After expiré, les créations restantes reprennent.
        third = self.project_with(Clock(5000.0 + 200.0))
        self.assertEqual(gh.issue_posts, 3)
        self.assertEqual(third["counts"]["created"], 2)


class PlanCardCreditTest(_PlanDb):
    """Les cartes du plan (`project_github`) passent par le même crédit."""

    def test_plan_projection_s_arrete_proprement_sur_le_credit(self):
        # Plus de trois étiquettes sont nécessaires avant la première carte :
        # un plafond de trois écritures doit arrêter la passe sans lever.
        self.sync()                                 # les paquets du canon de test
        gh = FakeGh()
        gh.repo(PLAN_REPO)["visibility"] = "private"
        clock = Clock(1000.0)
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            clock.advance(seconds)

        writes = pg._Writes(gh, reads=pg.SharedReads(self.state, clock=clock),
                            clock=clock, sleep=sleep, writes_per_pass=3)
        with mock.patch.object(pg, "_writes_for", return_value=writes):
            report = pg.project_github(self.db, gh, PLAN_REPO)
        self.assertEqual(len(gh.writes()), 3)
        self.assertTrue(report["notes"])


class SubjectWriteCreditTest(_SubjectBase):
    """Les épiques de sujets (`subject_github`) débitent le même crédit."""

    def test_sync_arrete_la_passe_et_ne_tente_pas_le_sujet_suivant(self):
        gh = ApprovingGh()
        repo = gh.repo(SUBJECT_REPO)
        repo["visibility"] = "private"
        for number, body in ((812, "Humain 812"), (814, "Humain 814")):
            repo["issues"].append({"id": 9000 + number, "number": number,
                                   "title": "Epic %d" % number, "body": body,
                                   "labels": [], "state": "open", "state_reason": None})
            repo["next"] = max(repo["next"], number + 1)
        first, _ = work_subjects.add(self.db, "premier", "Premier", project="t8389")
        second, _ = work_subjects.add(self.db, "second", "Second", project="t8389")
        ctx = subject_github.Context(None, self.db, gh)
        subject_github.adopt(self.db, ctx, first, "%s#812" % SUBJECT_REPO)
        subject_github.adopt(self.db, ctx, second, "%s#814" % SUBJECT_REPO)
        for subject in (first, second):
            lot = work.add(self.db, title="Lot %s" % subject["slug"], app="t8389")
            work_subjects.attach(self.db, lot["id"], subject["slug"], actor="human:proprio")
        gh.refuse_patch = True
        gh.reset_calls()
        report = subject_github.sync(None, self.db, gh, written={}, cache={})
        self.assertEqual(gh.patches, 1)
        self.assertEqual(report["updated"], 0)
        self.assertEqual(len(report["errors"]), 1)
        # Le geste suivant, dans la même fenêtre, est refusé sans requête.
        gh.reset_calls()
        again = subject_github.sync(None, self.db, gh, written={}, cache={})
        self.assertEqual(gh.patches, 1)
        self.assertEqual(len(again["errors"]), 1)     # notée, sans requête

    def test_adopt_403_secondaire_est_une_erreur_lisible(self):
        gh = ApprovingGh()
        repo = gh.repo(SUBJECT_REPO)
        repo["issues"].append({"id": 9812, "number": 812, "title": "Epic 812",
                               "body": "Humain", "labels": [], "state": "open",
                               "state_reason": None})
        subject, _ = work_subjects.add(self.db, "epique", "Épique", project="t8389")
        gh.refuse_patch = True
        ctx = subject_github.Context(None, self.db, gh)
        with self.assertRaisesRegex(work.WorkError, "rate limit"):
            subject_github.adopt(self.db, ctx, subject, "%s#812" % SUBJECT_REPO)
        self.assertEqual(gh.patches, 1)


if __name__ == "__main__":
    unittest.main()
