# SPDX-License-Identifier: AGPL-3.0-only
"""#8195 (tranche 4 de #8191, issue manaty/ameesh#6403) : l'épique GitHub d'un sujet.

`work subject add --github owner/repo#N` adopte un ticket lisible ;
`work subject project --repo` crée l'épique plus tard ; la section entre
marqueurs liste les lots et suit leur état ; le texte humain hors marqueurs
n'est jamais modifié ; le ticket fermé, lu par la veille GitHub, clôt le
sujet. GitHub est toujours simulé (`tests/fake_github.py`, `tests/fakebin/gh`).
"""
from __future__ import annotations

import json
import os
from unittest import mock

from ameesh import github_watch as watch
from ameesh import plan_github, storage, subject_github, work, work_subjects

from .fake_github import FakeGh
from .test_8193_work_subjects import APP, _SubjectBase

REPO = "thepackengers/transport-manager"
HUMAN = "Plan de la refonte.\n\nNe pas toucher à ce paragraphe."
FAKEBIN = os.path.join(os.path.dirname(__file__), "fakebin")


def fake(issues=(), visibility="private") -> FakeGh:
    gh = FakeGh()
    repo = gh.repo(REPO)
    repo["visibility"] = visibility
    for number, body, state in issues:
        repo["issues"].append({"id": 9000 + number, "number": number, "title": "Epic %d" % number,
                               "body": body, "labels": [], "state": state,
                               "state_reason": None})
        repo["next"] = max(repo["next"], number + 1)
    return gh


class SubjectEpicTest(_SubjectBase):
    def setUp(self):
        super().setUp()
        self.gh = fake([(812, HUMAN, "open"), (813, "", "closed")])
        self.ctx = subject_github.Context(None, self.db, self.gh)
        self.subject, _ = work_subjects.add(self.db, "pricing-v2", "Pricing v2", project=APP,
                                            actor="human:proprio")

    def body(self, number=812):
        return next(i for i in self.gh.repo(REPO)["issues"]
                    if i["number"] == number)["body"]

    def lot(self, title):
        item = work.add(self.db, title=title, app=APP, actor="human:proprio", footprint=[])
        work_subjects.attach(self.db, item["id"], "pricing-v2", actor="human:proprio")
        return item

    def test_adopt_writes_a_delimited_section_and_keeps_human_text(self):
        first = self.lot("Grille tarifaire")
        row = subject_github.adopt(self.db, self.ctx, self.subject, "%s#812" % REPO)
        self.assertEqual(row["github_ref"], "%s#812" % REPO)
        body = self.body()
        self.assertTrue(body.startswith(HUMAN + "\n\n"))
        self.assertIn("<!-- ameesh:subject=%d -->" % self.subject["id"], body)
        self.assertIn("<!-- ameesh:subject-end=%d -->" % self.subject["id"], body)
        self.assertIn("- [ ] ameesh #%d — Grille tarifaire (intake)" % first["id"], body)
        # idempotent : même référence, aucune nouvelle écriture
        self.gh.reset_calls()
        subject_github.adopt(self.db, self.ctx, row, "%s#812" % REPO)
        self.assertEqual(self.gh.writes(), [])

    def test_unreadable_closed_or_taken_issue_refused_before_any_write(self):
        with self.assertRaisesRegex(work.WorkError, "cannot read the issue.*grant it access"):
            subject_github.adopt(self.db, self.ctx, self.subject, "%s#999" % REPO)
        with self.assertRaisesRegex(work.WorkError, "is closed"):
            subject_github.adopt(self.db, self.ctx, self.subject, "%s#813" % REPO)
        with self.assertRaisesRegex(work.WorkError, "owner/repo#N"):
            subject_github.adopt(self.db, self.ctx, self.subject, "transport-manager#812")
        self.assertEqual(self.gh.writes(), [])
        subject_github.adopt(self.db, self.ctx, self.subject, "%s#812" % REPO)
        other, _ = work_subjects.add(self.db, "other", "Autre", project=APP)
        with self.assertRaisesRegex(work.WorkError, "already the epic of subject pricing-v2"):
            subject_github.adopt(self.db, self.ctx, other, "%s#812" % REPO.upper())
        mine = work_subjects.get(self.db, "pricing-v2", project=APP)
        with self.assertRaisesRegex(work.WorkError, "already has its epic"):
            subject_github.create_epic(self.db, self.ctx, mine, REPO)

    def test_projection_rewrites_on_lot_added_and_closed_only(self):
        subject_github.adopt(self.db, self.ctx, self.subject, "%s#812" % REPO)
        written, cache = {}, {}   # gardés par le Projector d'un passage à l'autre
        report = subject_github.sync(None, self.db, self.gh, written=written, cache=cache)
        self.assertEqual((report["updated"], report["errors"]), (0, []))
        # un humain édite hors marqueurs : gardé
        issue = next(i for i in self.gh.repo(REPO)["issues"] if i["number"] == 812)
        issue["body"] = "Avant.\n\n" + issue["body"] + "\nAprès."
        a = self.lot("Grille tarifaire")
        b = self.lot("Remises")
        self.assertEqual(subject_github.sync(None, self.db, self.gh, written=written, cache=cache)["updated"], 1)
        body = self.body()
        self.assertTrue(body.startswith("Avant.\n\n" + HUMAN + "\n\n"))
        self.assertTrue(body.endswith("\nAprès."))
        self.assertIn("- [ ] ameesh #%d — Grille tarifaire" % a["id"], body)
        self.assertIn("- [ ] ameesh #%d — Remises" % b["id"], body)
        # rien n'a changé : ni lecture ni écriture de GitHub
        self.gh.reset_calls()
        subject_github.sync(None, self.db, self.gh, written=written, cache=cache)
        self.assertEqual(self.gh.calls(), [])
        work.close(self.db, a["id"], abandoned=True, actor="human:proprio")
        self.assertEqual(subject_github.sync(None, self.db, self.gh, written=written, cache=cache)["updated"], 1)
        self.assertIn("- [x] ameesh #%d — Grille tarifaire (closed)" % a["id"], self.body())
        self.assertEqual(self.body().count("<!-- ameesh:subject=%d -->" % self.subject["id"]), 1)

    def test_human_edit_after_the_pre_check_is_kept(self):
        # B ameesh-dev3 #233701 : la CLI pré-contrôle (1re lecture) ; une édition
        # humaine avant l'adoption n'est pas écrasée par le corps de cette lecture.
        subject_github.check_adoptable(self.db, self.gh, "%s#812" % REPO, subject=self.subject)
        issue = next(i for i in self.gh.repo(REPO)["issues"] if i["number"] == 812)
        issue["body"] = HUMAN + "\n\nAjout humain."
        subject_github.adopt(self.db, self.ctx, self.subject, "%s#812" % REPO)
        self.assertTrue(self.body().startswith(HUMAN + "\n\nAjout humain.\n\n"))

    def test_guard_fails_closed(self):
        # B ameesh-dev3 #233698 : jamais de publication avec une garde dégradée.
        with mock.patch.object(plan_github, "settings_of",
                               side_effect=plan_github.SettingsError("bad github key")):
            with self.assertRaises(plan_github.SettingsError):
                subject_github.Context(mock.Mock(host="atelier"), self.db, self.gh)
            projector = plan_github.Projector(mock.Mock(host="atelier", forge_hosts=()),
                                              gh=self.gh)
            subject_github.adopt(self.db, self.ctx, self.subject, "%s#812" % REPO)
            self.lot("Grille tarifaire")
            self.gh.reset_calls()
            projector._subjects(self.db)
            self.assertEqual(self.gh.writes(), [])
        hosts = storage.of(self.db).hosts
        with mock.patch.object(type(hosts), "current", side_effect=RuntimeError("db down")):
            with self.assertRaises(RuntimeError):
                subject_github.Context(None, self.db, self.gh)
        # garde complète : un nom d'hôte du mesh n'est pas publié sur un dépôt public
        ctx = subject_github.Context(None, self.db, self.gh, local_host="ameesh-agents-1")
        self.assertIn("host name", ctx.guard.problems("voir ameesh-agents-1", public=True))

    def test_slug_and_issue_ref_pass_the_guard(self):
        # B P1 mty-amh-impl-11 (17add0e0) : le slug n'entre ni dans les
        # marqueurs ni dans le texte sans la garde ; issue_ref non plus.
        gh = fake([(812, "", "open"), (814, "", "open")], visibility="public")
        ctx = subject_github.Context(None, self.db, gh, local_host="ameesh-agents-1")
        subject, _ = work_subjects.add(self.db, "ameesh-agents-1", "Neutre", project=APP)
        item = work.add(self.db, title="Neutre", app=APP, actor="human:proprio", footprint=[])
        work_subjects.attach(self.db, item["id"], "ameesh-agents-1")
        self.db.execute("UPDATE work_items SET issue_ref = %s WHERE id = %s",
                        ("ameesh-agents-1/x#3", item["id"]))
        subject_github.adopt(self.db, ctx, subject, "%s#812" % REPO)
        body = next(i for i in gh.repo(REPO)["issues"] if i["number"] == 812)["body"]
        self.assertNotIn("ameesh-agents-1", body)
        self.assertIn("<!-- ameesh:subject=%d -->" % subject["id"], body)
        self.assertIn("(subject #%d)" % subject["id"], body)
        # projection : même garde sur la réécriture
        second = work.add(self.db, title="Autre", app=APP, actor="human:proprio", footprint=[])
        work_subjects.attach(self.db, second["id"], "ameesh-agents-1")
        cfg = mock.Mock(host="ameesh-agents-1", forge_hosts=())
        with mock.patch.object(plan_github, "settings_of", return_value=plan_github.Settings()):
            report = subject_github.sync(cfg, self.db, gh, written={}, cache=ctx.cache)
        self.assertEqual((report["updated"], report["errors"]), (1, []))
        body = next(i for i in gh.repo(REPO)["issues"] if i["number"] == 812)["body"]
        self.assertIn("ameesh #%d — Autre" % second["id"], body)
        self.assertNotIn("ameesh-agents-1", body)
        # création : même garde
        later, _ = work_subjects.add(self.db, "ameesh-agents-1-bis", "Neutre bis", project=APP)
        created = subject_github.create_epic(self.db, ctx, later, REPO)
        number = int(created["github_ref"].rsplit("#", 1)[1])
        body = next(i for i in gh.repo(REPO)["issues"] if i["number"] == number)["body"]
        self.assertIn("(subject #%d)" % later["id"], body)
        self.assertNotIn("ameesh-agents-1", body)
        # terme exclu de l'organisation, même en privé ; la section entière est
        # contrôlée : un champ non filtré refuse l'écriture sans recopier le terme
        guard = plan_github.Guard(exclude_terms=("TPK",))
        lots = [dict(id=1, title="Neutre", state="intake", issue_ref="")]
        text = subject_github.render(dict(id=5, slug="tpk", title="Neutre"), lots,
                                     guard=guard, public=False)
        self.assertNotIn("tpk", text.casefold())
        with mock.patch.object(subject_github, "_title", return_value="TPK"):
            with self.assertRaises(work.WorkError) as caught:
                subject_github.render(dict(id=5, slug="ok", title="x"), lots,
                                      guard=guard, public=False)
        self.assertNotIn("TPK", str(caught.exception))
        self.assertEqual(next(i for i in gh.repo(REPO)["issues"]
                              if i["number"] == 814)["body"], "")

    def test_damaged_markers_are_refused_not_guessed(self):
        with self.assertRaisesRegex(work.WorkError, "damaged"):
            subject_github.splice("x <!-- ameesh:subject=7 --> y", 7, "S")
        self.assertEqual(subject_github.splice("", 7, "S"), "S")

    def test_secret_like_title_is_withheld_and_mentions_quieted(self):
        gh = fake([(812, "", "open")], visibility="public")
        ctx = subject_github.Context(None, self.db, gh)
        self.lot("Clé ghp_" + "a" * 36)
        self.lot("Voir @someone")
        subject_github.adopt(self.db, ctx, self.subject, "%s#812" % REPO)
        body = next(i for i in gh.repo(REPO)["issues"] if i["number"] == 812)["body"]
        self.assertNotIn("ghp_", body)
        self.assertIn(subject_github.WITHHELD, body)
        self.assertNotIn("@someone", body)

    def test_project_creates_the_epic_later(self):
        lot = self.lot("Grille tarifaire")
        row = subject_github.create_epic(self.db, self.ctx, self.subject, REPO)
        created = self.gh.issue(REPO, "Pricing v2")
        self.assertEqual(row["github_ref"], "%s#%d" % (REPO, created["number"]))
        self.assertIn("ameesh #%d — Grille tarifaire" % lot["id"], created["body"])
        self.gh.state["missing_repos"] = ["acme/nope"]
        other, _ = work_subjects.add(self.db, "other", "Autre", project=APP)
        with self.assertRaisesRegex(work.WorkError, "cannot read the repository"):
            subject_github.create_epic(self.db, self.ctx, other, "acme/nope")

    def test_closed_epic_closes_the_subject_through_the_watch(self):
        subject_github.adopt(self.db, self.ctx, self.subject, "%s#812" % REPO)
        sub = watch.Subscription(APP, REPO, "watcher", 900)
        rows = [dict(id=9812, number=812, title="Epic", body=self.body(), state="closed",
                     updated_at=watch.iso(1800000000))]
        events = list(watch.events_from(sub, "issue", rows))
        self.assertEqual([(e["kind"], e["issue_ref"]) for e in events],
                         [(watch.SUBJECT_CLOSED, "%s#812" % REPO)])
        # ouvert : ni événement ni demande (le corps porte un marqueur ameesh)
        self.assertEqual(list(watch.events_from(sub, "issue", [dict(rows[0], state="open")])), [])
        closed = subject_github.close_by_issue(self.db, "%s#812" % REPO,
                                               actor="github:%s#812" % REPO)
        self.assertEqual([(s["slug"], s["state"]) for s in closed], [("pricing-v2", "closed")])
        self.assertEqual(subject_github.close_by_issue(self.db, "%s#812" % REPO), [])

    def test_projector_runs_the_subject_pass(self):
        subject_github.adopt(self.db, self.ctx, self.subject, "%s#812" % REPO)
        lot = self.lot("Grille tarifaire")
        cfg = mock.Mock(host="atelier", forge_hosts=())
        projector = plan_github.Projector(cfg, gh=self.gh)
        with mock.patch.object(plan_github, "settings_of", return_value=plan_github.Settings()):
            projector._subjects(self.db)
        self.assertIn("ameesh #%d — Grille tarifaire" % lot["id"], self.body())


class SubjectEpicDeliveryTest(_SubjectBase):
    def test_deliver_closes_the_subject_without_a_digest(self):
        from ameesh import mail, registry
        self.db.execute("TRUNCATE github_watch_cursors, github_watch_events, github_watch_scans,"
                        " github_watch_pending")
        registry.upsert(self.db, "watcher", harness="codex", chantier=APP)
        subject, _ = work_subjects.add(self.db, "pricing-v2", "Pricing v2", project=APP)
        storage.of(self.db).work.subject_set_github(subject["id"], "%s#812" % REPO)
        sub = watch.Subscription(APP, REPO, "watcher", 900)
        event = dict(kind=watch.SUBJECT_CLOSED, object_id=9812, issue_ref="%s#812" % REPO)
        self.assertEqual(watch.deliver(self.db, sub, [event], 1800000000, [], "atelier"), 0)
        self.assertEqual(mail.unread(self.db, "watcher"), [])
        row = work_subjects.get(self.db, "pricing-v2", project=APP)
        self.assertEqual((row["state"], row["closed_by"]), ("closed", "github:%s#812" % REPO))


class SubjectEpicCliTest(_SubjectBase):
    def gh_env(self, state):
        path = os.path.join(self.tmp, "gh-state.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
        return path, self.env(AMEESH_GH_BIN=os.path.join(FAKEBIN, "gh"),
                              AMEESH_FAKE_GH_STATE=path)

    def test_cli_adopt_refusal_creates_nothing_and_project_later(self):
        work.add(self.db, title="Amorce", app=APP, actor="human:proprio", footprint=[])
        state = fake([(812, HUMAN, "open")]).state
        path, env = self.gh_env(state)
        refused = self.mesh("work", "subject", "add", "late", "--title", "Late", "--app", APP,
                            "--github", "%s#999" % REPO, env=env)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("cannot read the issue", refused.stderr + refused.stdout)
        self.assertIsNone(work_subjects.find(self.db, "late", project=APP))
        adopted = self.mesh("work", "subject", "add", "pricing-v3", "--title", "Pricing v3",
                            "--app", APP, "--github", "%s#812" % REPO, env=env)
        self.assertEqual(adopted.returncode, 0, adopted.stderr)
        self.assertIn("epic: %s#812" % REPO, adopted.stdout)
        with open(path, encoding="utf-8") as handle:
            body = json.load(handle)["repos"][REPO]["issues"][0]["body"]
        self.assertTrue(body.startswith(HUMAN))
        subject_id = work_subjects.get(self.db, "pricing-v3", project=APP)["id"]
        self.assertIn("<!-- ameesh:subject=%d -->" % subject_id, body)
        self.assertEqual(self.mesh("work", "subject", "add", "later", "--title", "Later",
                                   "--app", APP, env=env).returncode, 0)
        project = self.mesh("work", "subject", "project", "later", "--app", APP,
                            "--repo", REPO, env=env)
        self.assertEqual(project.returncode, 0, project.stderr)
        self.assertIn("epic %s#" % REPO, project.stdout)
        show = self.mesh("work", "subject", "show", "later", "--app", APP, env=env)
        self.assertIn("epic     : %s#" % REPO, show.stdout)
