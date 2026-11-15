# SPDX-License-Identifier: AGPL-3.0-only
"""#7476 : un siège de veille ne dort pas avec la feuille de route épuisée."""
import time
from types import SimpleNamespace as NS
from unittest import mock

from ameesh import agent_pause, file_decoupage as fd, github_watch, mail, registry, work
from .support import PgTestCase
from . import test_4610_sleep_work_mail as fixtures


class WatchSleepTest(PgTestCase):
    def setUp(self):
        super().setUp()
        for name in ("pilote", "dev0", "dev1", "dev2"):
            registry.upsert(self.db, name, harness="deepseek", host=self.cfg.host,
                            cwd=self.tmp)
        self.db.execute("UPDATE agent_registry SET responsible = 'human:alice',"
                        " team = 'ameesh'")
        pilots = mock.patch("ameesh.alert_briefs.project_pilots", return_value=["pilote"])
        pilots.start()
        self.addCleanup(pilots.stop)
        self.state = {}
        self.t0 = time.time()

    listing = fixtures.SleepWorkMailTest.listing
    run_pass = fixtures.SleepWorkMailTest.run_pass
    split_lot = fixtures.SleepWorkMailTest.split_lot
    make_split = fixtures.SleepWorkMailTest.make_split
    notes = fixtures.SleepWorkMailTest.notes
    sleep_all = fixtures.SleepWorkMailTest.sleep_all
    lift_event = fixtures.SleepWorkMailTest.lift_event

    def watch(self, project="ameesh"):
        return mock.patch.object(github_watch, "subscriptions", return_value=[
            github_watch.Subscription(project, "acme/app", "dev1", 900)])

    def test_watcher_is_spared_without_mail_and_after_hourly_recall(self):
        split = self.make_split()
        work.note(self.db, split, "roadmap: épuisée ameesh (test)", actor="pilote")
        with self.watch():
            for offset in (1900, 5500):
                if offset == 5500:
                    mail.send(self.db, "ameesh", "dev1", "Relance entrée GitHub non traitée")
                out = self.run_pass(offset, self.listing("dev0", "dev1"))
                by_agent = {r["agent"]: r for g in out if g["kind"] == "sleep"
                            for r in g["agents"]}
                self.assertFalse(by_agent["dev1"]["slept"])
                self.assertIn("GitHub watcher", by_agent["dev1"]["why"])
                self.assertFalse(registry.get(self.db, "dev1")["operator_paused"])
        notes = [n for n in self.notes(split) if n.startswith("sleep exempted: dev1:")]
        self.assertEqual(len(notes), 1)
        self.assertIn("GitHub watcher", notes[0])

    def test_existing_automatic_sleep_is_lifted_and_journalled(self):
        split = self.sleep_all()
        mail.send(self.db, "ameesh", "dev1", "Entrée GitHub à traiter")
        with self.watch():
            out = self.run_pass(2000)
            self.assertEqual([g["agents"] for g in out if g["kind"] == "wake"], [["dev1"]])
            self.assertFalse([g for g in self.run_pass(5600) if g["kind"] == "wake"])
        self.assertFalse(registry.get(self.db, "dev1")["operator_paused"])
        self.assertTrue(registry.get(self.db, "dev0")["operator_paused"])
        self.assertIn("GitHub watcher", self.lift_event("dev1")["reason"])
        self.assertTrue(any("sleep exempted: dev1:" in n and "GitHub watcher" in n
                            for n in self.notes(split)))

    def test_watcher_of_another_project_still_sleeps(self):
        with self.watch("other"):
            self.sleep_all()
        self.assertTrue(registry.get(self.db, "dev1")["operator_paused"])

    def test_human_pause_is_preserved(self):
        agent_pause.change(self.db, "dev1", paused=True, reason="maintenance",
                           actor="human:alice", principal="human:alice")
        with self.watch():
            self.run_pass(2000)
        self.assertTrue(registry.get(self.db, "dev1")["operator_paused"])

    def test_uses_validated_subscriptions_not_role_alone(self):
        def card(name, roles, projects, spec):
            return NS(title=name, roles=roles, effective_projects=projects,
                      fiche=NS(data={"github_watch": spec}))
        canons = [NS(agents=[
            card("valid", ["github-watch"], ["app"],
                 {"app": {"repos": ["acme/app"]}}),
            card("foreign", ["github-watch"], ["elsewhere"],
                 {"app": {"repos": ["acme/other"]}}),
            card("role-only", ["github-watch"], ["app"], {}),
        ])]
        self.assertEqual(fd._github_watchers(canons), {("valid", "app")})
