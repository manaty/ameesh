# SPDX-License-Identifier: AGPL-3.0-only
"""L118c: mail-created lots keep their project and corrections are guarded."""
from types import SimpleNamespace
from unittest import mock

from ameesh import db as db_mod, exploitation, projects, work, work_projects
from .test_l118_assignments import _Base


class MailProjectTests(_Base):
    def setUp(self):
        super().setUp()
        for name in ("orch", "dev1", "dev2"):
            self.agent(name)

    def send_new(self, *options):
        return self.cli("send", "dev1", "Build the release.", "--from", "orch",
                        "--new-lot", "Release", "--estimate", "1h", *options,
                        env=self.env(AMEESH_ALERT_ORCHESTRATORS="orch"))

    def project(self, name, *, team=None, chantier=""):
        self.db.execute("UPDATE agent_registry SET team = %s, chantier = %s WHERE name = %s",
                        (team, chantier, name))

    def test_explicit_project_overrides_both_agents(self):
        self.project("orch", team="sender")
        self.project("dev1", team="recipient")
        proc = self.send_new("--app", " explicit ")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lot = work.list_items(self.db)[0]
        self.assertEqual(lot["app"], "explicit")
        self.assertEqual(self.unread("dev1")[0]["work_item_id"], str(lot["id"]))
        self.assertNotIn("--app", self.unread("dev1")[0]["body"])
        view = projects.snapshot(self.db, project="explicit")
        self.assertEqual(view["projects"][0]["lots_open"], 1)

    def test_sender_team_takes_precedence_then_chantier(self):
        self.project("orch", team="sender", chantier="legacy")
        self.project("dev1", team="recipient")
        proc = self.send_new()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(work.list_items(self.db)[0]["app"], "sender")
        self.db.execute("UPDATE agent_registry SET team = NULL WHERE name = 'orch'")
        proc = self.send_new()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(work.list_items(self.db)[0]["app"], "legacy")

    def test_recipient_project_is_the_fallback(self):
        self.project("dev1", team="recipient")
        proc = self.send_new()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(work.list_items(self.db)[0]["app"], "recipient")

    def test_no_project_refuses_without_lot_or_mail(self):
        proc = self.send_new()
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("no project known", proc.stderr)
        self.assertIn("--app <project>", proc.stderr)
        self.assertEqual(work.list_items(self.db), [])
        self.assertEqual(self.unread("dev1"), [])

    def test_project_flag_requires_creation_and_a_nonempty_value(self):
        for args, detail in ((("--app", "p"), "--app goes with --new-lot"),
                             (("--new-lot", "x", "--app", " "), "empty project"),
                             (("--new-lot", "x", "--app"), "empty project")):
            with self.subTest(args=args):
                proc = self.cli("send", "dev1", "Work.", "--from", "orch", *args)
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertIn(detail, proc.stderr)
        self.assertEqual(work.list_items(self.db), [])
        self.assertEqual(self.unread("dev1"), [])

    def test_canon_fallback_only_uses_the_agents_own_canon(self):
        self.db.execute("UPDATE agent_registry SET canon = 'own' WHERE name = 'orch'")
        other = SimpleNamespace(owns=lambda name: name == "other",
                                agent=lambda name: SimpleNamespace(team="wrong"))
        own = SimpleNamespace(owns=lambda name: name == "own",
                              agent=lambda name: SimpleNamespace(team="right"))
        with mock.patch.object(work, "configured_canons", return_value=[other, own]):
            self.assertEqual(work_projects.mail_project(self.cfg, self.db, "orch", "dev1"),
                             "right")
        self.project("dev1", team="recipient")
        with mock.patch.object(work, "configured_canons", return_value=[other]):
            self.assertEqual(work_projects.mail_project(self.cfg, self.db, "orch", "dev1"),
                             "recipient")

    def test_human_sender_falls_back_to_recipient(self):
        self.project("dev1", team="recipient")
        self.assertEqual(work_projects.mail_project(self.cfg, self.db, "human:owner", "dev1"),
                         "recipient")

    def test_set_app_human_is_journaled_and_idempotent(self):
        lot = work.add(self.db, title="Unclassified")
        proc = self.mesh("work", "set-app", str(lot["id"]), "new", "--actor", "human:owner")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(work.get(self.db, lot["id"])["app"], "new")
        events = work.events(self.db, lot["id"])
        self.assertEqual(events[0]["actor"], "human:owner")
        self.assertEqual(events[0]["note"], "project '' → 'new'")
        proc = self.mesh("work", "set-app", str(lot["id"]), "new", "--actor", "human:owner")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(work.events(self.db, lot["id"]), events)

    def test_bound_worker_cannot_correct_or_impersonate_a_privileged_actor(self):
        lot = work.add(self.db, title="Unclassified")
        env = self.env(AGENT_MAIL_NAME="dev1", AMEESH_ALERT_ORCHESTRATORS="orch")
        events = work.events(self.db, lot["id"])
        for actor in (None, "human:owner", "orch"):
            with self.subTest(actor=actor):
                args = ("--actor", actor) if actor else ()
                proc = self.mesh("work", "set-app", str(lot["id"]), "p", *args, env=env)
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("correction refused", proc.stderr)
                self.assertEqual(work.get(self.db, lot["id"])["app"], "")
                self.assertEqual(work.events(self.db, lot["id"]), events)

    def test_bound_orchestrator_corrects_as_itself(self):
        lot = work.add(self.db, title="Wrong project", app="old")
        env = self.env(AGENT_MAIL_NAME="orch", AMEESH_ALERT_ORCHESTRATORS="orch")
        proc = self.mesh("work", "set-app", str(lot["id"]), "new", env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        event = work.events(self.db, lot["id"])[0]
        self.assertEqual((event["actor"], event["note"]), ("orch", "project 'old' → 'new'"))

    def test_design_role_uses_existing_correction_guard(self):
        lot = work.add(self.db, title="Unclassified")
        binding = SimpleNamespace(name="dev2")
        agent = SimpleNamespace(title="dev2", fiche=SimpleNamespace(data={"roles": ["design"]}))
        canon = SimpleNamespace(agents=[agent])
        with mock.patch("ameesh.identity.resolve_binding", return_value=binding), \
                mock.patch("ameesh.canon.load_configured", return_value=[canon]):
            work_projects.set_app(self.cfg, self.db, lot["id"], "p")
        self.assertEqual(work.events(self.db, lot["id"])[0]["actor"], "dev2")

    def test_set_app_rejects_missing_lot_or_empty_project(self):
        lot = work.add(self.db, title="Unclassified")
        for item_id, project, detail in ((lot["id"], " ", "empty project"),
                                         (lot["id"] + 1000, "p", "not found")):
            with self.subTest(item_id=item_id, project=project):
                proc = self.mesh("work", "set-app", str(item_id), project,
                                 "--actor", "human:owner")
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn(detail, proc.stderr)
        self.assertEqual(len(work.events(self.db, lot["id"])), 1)

    def test_project_change_rolls_back_if_its_journal_cannot_be_written(self):
        lot = work.add(self.db, title="Unclassified")
        self.db.execute("CREATE FUNCTION reject_project_event() RETURNS trigger "
                        "LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'journal unavailable'; END $$")
        self.db.execute("CREATE TRIGGER reject_project_event BEFORE INSERT ON work_item_events "
                        "FOR EACH ROW EXECUTE FUNCTION reject_project_event()")
        try:
            with self.assertRaises(db_mod.DbError):
                work_projects.set_app(self.cfg, self.db, lot["id"], "p", actor="human:owner")
            self.assertEqual(work.get(self.db, lot["id"])["app"], "")
            self.assertEqual(len(work.events(self.db, lot["id"])), 1)
        finally:
            self.db.execute("DROP TRIGGER reject_project_event ON work_item_events")
            self.db.execute("DROP FUNCTION reject_project_event()")

    def test_alerts_cover_open_lots_once_and_have_stable_keys(self):
        blank = work.add(self.db, title="Empty", assignee="dev1")
        spaces = work.add(self.db, title="Spaces", app="  ")
        waiting = work.add(self.db, title="Waiting")
        self.db.execute("UPDATE work_items SET state = 'waiting_human' WHERE id = %s",
                        (waiting["id"],))
        work.add(self.db, title="Named", app="p")
        for state in ("merged", "promoted", "closed"):
            lot = work.add(self.db, title=state)
            if state == "closed":
                work.close(self.db, lot["id"], abandoned=True)
            else:
                self.db.execute("UPDATE work_items SET state = %s WHERE id = %s", (state, lot["id"]))
        current = [a for a in exploitation.alerts(self.cfg, self.db)
                   if a["type"] == "lot_without_project"]
        self.assertEqual(sorted(a["lot"] for a in current),
                         sorted([blank["id"], spaces["id"], waiting["id"]]))
        keys = {a["lot"]: exploitation.alert_key(a) for a in current}
        work.note(self.db, blank["id"], "Work started", actor="dev1")
        self.db.execute("UPDATE work_items SET assignee = 'dev2' WHERE id = %s", (blank["id"],))
        again = work_projects.alerts(self.db)
        self.assertEqual({a["lot"]: exploitation.alert_key(a) for a in again}, keys)
        work_projects.set_app(self.cfg, self.db, blank["id"], "p", actor="human:owner")
        self.assertNotIn(blank["id"], [a["lot"] for a in work_projects.alerts(self.db)])
