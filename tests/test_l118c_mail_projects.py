# SPDX-License-Identifier: AGPL-3.0-only
"""L118c: mail-created lots keep their project and corrections are guarded;
a lot-linked message is filed in the project thread of its lot (#515)."""
import os
from types import SimpleNamespace
from unittest import mock

from ameesh import db as db_mod, decisions, exploitation, projects, storage, work, work_projects
from .test_l118_assignments import _Base


class MailProjectTests(_Base):
    def setUp(self):
        super().setUp()
        for name in ("orch", "dev1", "dev2"):
            self.agent(name)

    def send_new(self, *options):
        # #540 revue r2 : le forçage exige une session liée ; ce fixture force
        # au nom d'orch, la session est donc liée à orch.
        return self.cli("send", "dev1", "Build the release.", "--from", "orch",
                        "--new-lot", "Release", "--estimate", "1h", *options,
                        env=self.env(AGENT_MAIL_NAME="orch",
                                     AMEESH_ALERT_ORCHESTRATORS="orch"))

    def project(self, name, *, team=None, chantier=""):
        self.db.execute("UPDATE agent_registry SET team = %s, chantier = %s WHERE name = %s",
                        (team, chantier, name))

    def test_explicit_project_overrides_both_agents(self):
        self.project("orch", team="sender")
        self.project("dev1", team="recipient")
        proc = self.send_new("--app", " explicit ", "--new-project")   # #506: nobody declares it
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
        proc = self.send_new("--force", "test: second lot of dev1 (#540)")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(work.list_items(self.db)[0]["app"], "legacy")

    def test_recipient_project_is_the_fallback(self):
        self.project("dev1", team="recipient")
        proc = self.send_new()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(work.list_items(self.db)[0]["app"], "recipient")

    # -- #515 : le fil d'un message lié à un lot est celui du projet du lot ---

    def thread_path(self, project, lot):
        return os.path.join(self.cfg.threads_root, project, "%s.md" % lot)

    def lot_message(self, lot, dest="dev1"):
        return self.cli("send", dest, "Message lié au lot.", "--from", "orch",
                        "--lot", str(lot), env=self.env(AMEESH_ALERT_ORCHESTRATORS="orch"))

    def test_lot_message_is_filed_in_the_lot_project_thread(self):
        self.project("orch", chantier="A")
        self.project("dev1", chantier="A")
        lot = work.add(self.db, title="Fil du lot", app="B")
        proc = self.lot_message(lot["id"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(self.thread_path("B", lot["id"]), encoding="utf-8") as fh:
            self.assertIn("Message lié au lot.", fh.read())
        self.assertFalse(os.path.exists(self.thread_path("A", lot["id"])))
        [row] = self.db.query("SELECT project, lot FROM thread_index WHERE lot = %s",
                              (str(lot["id"]),))
        self.assertEqual((row["project"], row["lot"]), ("B", str(lot["id"])))

    def test_lot_without_project_keeps_the_sender_thread(self):
        self.project("orch", chantier="A")
        self.project("dev1", chantier="A")
        lot = work.add(self.db, title="Sans projet")
        proc = self.lot_message(lot["id"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(os.path.exists(self.thread_path("A", lot["id"])))
        self.assertFalse(os.path.exists(self.thread_path("B", lot["id"])))

    def test_free_label_keeps_the_sender_thread_without_warning(self):
        self.project("orch", chantier="A")
        self.project("dev1", chantier="A")
        proc = self.lot_message("SUJET-LIBRE")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("project unreadable", proc.stderr)
        with open(self.thread_path("A", "SUJET-LIBRE"), encoding="utf-8") as fh:
            self.assertIn("Message lié au lot.", fh.read())
        self.assertFalse(os.path.exists(self.thread_path("B", "SUJET-LIBRE")))

    def test_all_broadcast_of_a_lot_is_grouped_in_the_lot_project(self):
        self.project("orch", team="", chantier="")
        self.project("dev1", chantier="A")
        self.project("dev2", chantier="C")
        lot = work.add(self.db, title="Diffusion", app="B")
        proc = self.lot_message(lot["id"], dest="all")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(self.thread_path("B", lot["id"]), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("agent:dev1", text)
        self.assertIn("agent:dev2", text)
        for project in ("A", "C"):
            self.assertFalse(os.path.exists(self.thread_path(project, lot["id"])))

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
                                agents=[SimpleNamespace(title="orch", team="wrong")])
        own = SimpleNamespace(owns=lambda name: name == "own",
                              agents=[SimpleNamespace(title="orch", team="right")])
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

    def test_work_add_inherits_creator_and_rejects_explicit_blank(self):
        self.project("dev1", team="creator")
        self.project("dev2", team="assignee")
        env = self.env(AGENT_MAIL_NAME="dev1")
        proc = self.mesh("work", "add", "--title", "New", "--estimate", "1h",
                         "--assignee", "dev2", env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(work.list_items(self.db)[0]["app"], "creator")
        proc = self.mesh("work", "add", "--title", "Blank", "--estimate", "1h",
                         "--app", " ", env=env)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("empty project", proc.stderr)
        self.assertEqual(len(work.list_items(self.db)), 1)
        proc = self.mesh("work", "add", "--title", "Override", "--estimate", "1h",
                         "--app", "explicit", "--new-project", env=env)   # #506: undeclared
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(work.list_items(self.db)[0]["app"], "explicit")

    # #506: an explicit project must be known on the three creation paths
    def create(self, path, project, *options):
        if path == "work add":
            return self.mesh("work", "add", "--title", "W %s" % project, "--estimate", "1h",
                             "--app", project, *options,
                             env=self.env(AGENT_MAIL_NAME="dev1"))
        if path == "mail":
            return self.send_new("--app", project, *options)
        return self.mesh("work", "backlog", "add", "--title", "B %s" % project,
                         "--value", "kept", "--score", "30", "--estimate", "30m",
                         "--app", project, *options,
                         env=self.env(AGENT_MAIL_NAME="dev1"))

    def test_unknown_explicit_project_is_refused_with_close_names(self):
        work.add(self.db, title="Known", app="ameesh")
        for path in ("work add", "mail", "backlog"):
            with self.subTest(path=path):
                proc = self.create(path, "ameeshh")
                self.assertNotEqual(proc.returncode, 0, proc.stderr)
                self.assertIn("unknown project 'ameeshh'", proc.stderr)
                self.assertIn("close names: ameesh (1 letter apart)", proc.stderr)
                self.assertIn("--new-project", proc.stderr)
                proc = self.create(path, "zzz-nowhere")
                self.assertNotEqual(proc.returncode, 0, proc.stderr)
                self.assertIn("no close name", proc.stderr)
        self.assertEqual([i["title"] for i in work.list_items(self.db)], ["Known"])
        self.assertEqual(self.unread("dev1"), [])
        with self.assertRaisesRegex(work.WorkError, "unknown project"):
            work_projects.creation_project(self.cfg, self.db, "dev1", "ameeshh")
        with self.assertRaisesRegex(work.WorkError, "unknown project"):
            work_projects.mail_project(self.cfg, self.db, "orch", "dev1", "ameeshh")
        for path in ("work add", "mail", "backlog"):
            with self.subTest(path=path, project="known"):
                proc = self.create(path, " ameesh ")
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(work.list_items(self.db)[0]["app"], "ameesh")

    def test_new_project_introduces_it_and_journals_who_did(self):
        for path, project, actor in (("work add", "alpha", "dev1"), ("mail", "beta", "orch"),
                                     ("backlog", "gamma", "dev1")):
            # #540: three lots for dev1 through mail, none started
            force = ("--force", "test: lots of dev1 (#540)") if path == "mail" else ()
            with self.subTest(path=path):
                proc = self.create(path, project, "--new-project", *force)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                lot = work.list_items(self.db)[0]
                self.assertEqual(lot["app"], project)
                notes = " | ".join(e["note"] or "" for e in work.events(self.db, lot["id"]))
                self.assertIn("project '%s' introduced by %s (--new-project)"
                              % (project, actor), notes)
                # known from now on: no flag needed, and no second introduction
                proc = self.create(path, project, *force)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                proc = self.create(path, project, "--new-project", *force)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                again = work.list_items(self.db)[0]
                self.assertNotIn("introduced", " ".join(
                    e["note"] or "" for e in work.events(self.db, again["id"])))
        self.assertEqual(len(self.unread("dev1")), 3)

    def test_new_project_goes_with_an_explicit_project(self):
        procs = {
            "work add": self.mesh("work", "add", "--title", "W", "--estimate", "1h",
                                  "--new-project", env=self.env(AGENT_MAIL_NAME="dev1")),
            "mail": self.send_new("--new-project"),
            "backlog": self.mesh("work", "backlog", "add", "--title", "B", "--value", "kept",
                                 "--score", "30", "--new-project",
                                 env=self.env(AGENT_MAIL_NAME="dev1")),
        }
        for path, proc in procs.items():
            with self.subTest(path=path):
                self.assertNotEqual(proc.returncode, 0, proc.stderr)
                self.assertIn("--new-project goes with", proc.stderr)
        self.assertEqual(work.list_items(self.db), [])
        self.assertEqual(self.unread("dev1"), [])

    def test_work_add_uses_bound_creator_even_with_an_explicit_actor(self):
        self.project("dev1", team="bound")
        self.project("orch", team="other")
        proc = self.mesh("work", "add", "--title", "New", "--estimate", "1h",
                         "--actor", "orch", env=self.env(AGENT_MAIL_NAME="dev1"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(work.list_items(self.db)[0]["app"], "bound")

    def test_backlog_add_inherits_the_creator_project(self):
        self.project("dev1", team="creator")
        proc = self.mesh("work", "backlog", "add", "--title", "Item", "--value", "one rule",
                         "--score", "30", "--team", "creator", "--estimate", "30m",
                         env=self.env(AGENT_MAIL_NAME="dev1"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        item = work.list_items(self.db)[0]
        self.assertEqual((item["type"], item["app"]), ("improvement", "creator"))
        self.assertNotIn(item["id"], [a["lot"] for a in work_projects.alerts(self.db)])
        proc = self.mesh("work", "backlog", "add", "--title", "Unknown", "--value", "kept",
                         "--score", "30", "--estimate", "30m",
                         env=self.env(AGENT_MAIL_NAME="dev2"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        unknown = work.list_items(self.db)[0]
        self.assertEqual((unknown["title"], unknown["app"]), ("Unknown", ""))
        self.assertIn(unknown["id"], [a["lot"] for a in work_projects.alerts(self.db)])

    def test_backlog_api_inherits_and_validates_the_project(self):
        from ameesh import backlog
        self.project("dev1", team="creator")
        item = backlog.add(self.db, title="Api", expected_value="one rule", value_score=30,
                           estimate="30m", actor="dev1")
        self.assertEqual(item["app"], "creator")
        for app in ("unconfigured-project", " "):
            with self.subTest(app=app), self.assertRaises(backlog.BacklogError):
                backlog.add(self.db, title="Refused", expected_value="kept", value_score=30,
                            estimate="30m", actor="dev1", app=app)
        item = backlog.add(self.db, title="Known", expected_value="kept", value_score=30,
                           estimate="30m", actor="dev2", app=" creator ")
        self.assertEqual(item["app"], "creator")
        proc = self.mesh("work", "backlog", "add", "--title", "Flag", "--value", "kept",
                         "--score", "30", "--app", "nowhere", "--estimate", "30m",
                         env=self.env(AGENT_MAIL_NAME="dev2"))
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("unknown project", proc.stderr)
        proc = self.mesh("work", "backlog", "add", "--title", "Flag", "--value", "kept",
                         "--score", "30", "--app", "creator", "--estimate", "30m",
                         env=self.env(AGENT_MAIL_NAME="dev2"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(work.list_items(self.db)[0]["app"], "creator")
        self.assertNotIn("Refused", [i["title"] for i in work.list_items(self.db)])

    def test_backlog_known_projects_follow_the_given_configuration(self):
        from ameesh import backlog

        class NoCfg:   # a connection without `cfg`
            def __init__(self, db):
                self._db = db

            def __getattr__(self, name):
                if name == "cfg":
                    raise AttributeError(name)
                return getattr(self._db, name)

        def github(*names):
            return SimpleNamespace(github={"projects": {n: "acme/%s" % n for n in names}})

        bare = NoCfg(self.db)
        item = backlog.add(bare, title="Alpha", expected_value="kept", value_score=30,
                           estimate="30m", actor="dev2", app="alpha", cfg=github("alpha"))
        self.assertEqual(item["app"], "alpha")
        args = SimpleNamespace(backlog_command="add", title="Run", expected_value="kept",
                               value_score=30, priority=backlog.DEFAULT_PRIORITY, source="",
                               team=None, requires=[], body="", package=None, actor="dev2",
                               app="beta", json=False)
        with self.assertRaisesRegex(backlog.BacklogError, "unknown project"):
            backlog.add(bare, title="Beta", expected_value="kept", value_score=30,
                        estimate="30m", actor="dev2", app="beta")
        self.assertEqual(backlog.run(bare, args), 2)
        with mock.patch.object(self.db, "cfg", github("other"), create=True):
            item = backlog.add(self.db, title="Gamma", expected_value="kept", value_score=30,
                               estimate="30m", actor="dev2", app="gamma",
                               cfg=github("gamma"))
            self.assertEqual(item["app"], "gamma")
            with self.assertRaisesRegex(backlog.BacklogError, "unknown project"):
                backlog.add(self.db, title="Other", expected_value="kept", value_score=30,
                            estimate="30m", actor="dev2", app="other", cfg=github("gamma"))
        self.assertNotIn("Beta", [i["title"] for i in work.list_items(self.db)])

    def test_decision_creation_inherits_requester_then_parent(self):
        parent = work.add(self.db, title="Parent", app="parent")
        self.project("dev1", team="requester")
        view = decisions.ask(self.cfg, self.db, requester="dev1", question="Proceed?",
                             new_lot="Decision", parent_lot=str(parent["id"]))
        self.assertEqual(work.get(self.db, view["lot"])["app"], "requester")
        self.project("dev1", team="")
        view = decisions.ask(self.cfg, self.db, requester="dev1", question="Proceed?",
                             new_lot="Child", parent_lot=str(parent["id"]))
        self.assertEqual(work.get(self.db, view["lot"])["app"], "parent")
        proc = self.mesh("decide", "ask", "--new-lot", "CLI child", "--parent-lot",
                         str(parent["id"]), "--question", "Proceed?",
                         env=self.env(AGENT_MAIL_NAME="dev1"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(work.list_items(self.db)[0]["app"], "parent")

    def test_creation_uses_only_own_canon_and_refuses_ambiguous_projects(self):
        self.db.execute("UPDATE agent_registry SET canon = 'own' WHERE name = 'dev1'")
        def canon(team, own=True):
            return SimpleNamespace(owns=lambda name: own and name == "own",
                                   agents=[SimpleNamespace(title="dev1", team=team)])
        with mock.patch.object(work, "configured_canons", return_value=[canon("foreign", False),
                                                                                   canon("own")]):
            self.assertEqual(work_projects.creation_project(self.cfg, self.db, "dev1"), "own")
        with mock.patch.object(work, "configured_canons", return_value=[canon("a"), canon("b")]):
            with self.assertRaisesRegex(work.WorkError, "ambiguous"):
                work_projects.creation_project(self.cfg, self.db, "dev1")

    def test_decision_creation_falls_back_to_source_mail_lot(self):
        parent = work.add(self.db, title="Parent", app="parent")
        proc = self.cli("send", "orch", "Proceed?", "--from", "dev1", "--lot", str(parent["id"]))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        mail_id = self.db.query("SELECT id FROM agent_mailbox ORDER BY id LIMIT 1")[0]["id"]
        view = decisions.ask(self.cfg, self.db, requester="dev1", question="Proceed?",
                             new_lot="Child", source_mail=mail_id)
        self.assertEqual(work.get(self.db, view["lot"])["app"], "parent")

    def test_follow_up_decision_accepts_completed_source_lots(self):
        for state in ("merged", "closed"):
            parent = work.add(self.db, title="Parent", app="parent")
            proc = self.cli("send", "orch", "Proceed?", "--from", "dev1",
                            "--lot", str(parent["id"]))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            mail_id = self.db.query("SELECT id FROM agent_mailbox WHERE kind = 'notify'"
                                    " ORDER BY id DESC LIMIT 1")[0]["id"]
            if state == "closed":
                work.close(self.db, parent["id"], abandoned=True)
            else:
                self.db.execute("UPDATE work_items SET state = %s WHERE id = %s",
                                (state, parent["id"]))
            events = work.events(self.db, parent["id"])
            for requester_project in ("requester", ""):
                with self.subTest(state=state, requester_project=requester_project):
                    self.project("dev1", team=requester_project)
                    view = decisions.ask(self.cfg, self.db, requester="dev1", question="Proceed?",
                                         new_lot="Follow up", source_mail=mail_id)
                    self.assertEqual(work.get(self.db, view["lot"])["app"],
                                     requester_project or "parent")
                    self.assertEqual(work.get(self.db, parent["id"])["state"],
                                     "delivered" if state in ("merged", "promoted") else state)
                    self.assertEqual(work.events(self.db, parent["id"]), events)

    def test_known_requester_project_does_not_resolve_an_unused_parent(self):
        self.project("dev1", team="requester")
        with mock.patch("ameesh.assignments.resolve_lot",
                        side_effect=AssertionError("unused parent lookup")):
            view = decisions.ask(self.cfg, self.db, requester="dev1", question="Proceed?",
                                 new_lot="Follow up", parent_lot="unknown old reference")
        self.assertEqual(work.get(self.db, view["lot"])["app"], "requester")

    def test_duplicate_own_canon_cards_refuse_inheritance_and_skip_backfill(self):
        self.db.execute("UPDATE agent_registry SET canon = 'own' WHERE name = 'dev1'")
        cards = [SimpleNamespace(title="dev1", team=p) for p in ("A", "B")]
        canon = SimpleNamespace(owns=lambda c: c == "own", agents=cards,
                                agent=lambda name: cards[0])
        lot = work.add(self.db, title="Ambiguous", assignee="dev1", actor="orch")
        events = work.events(self.db, lot["id"])
        with mock.patch.object(work, "configured_canons", return_value=[canon]):
            with self.assertRaisesRegex(work.WorkError, "ambiguous"):
                work_projects.creation_project(self.cfg, self.db, "dev1")
            with self.assertRaisesRegex(work.WorkError, "ambiguous"):
                work_projects.mail_project(self.cfg, self.db, "dev1", "orch")
            rows = work_projects.backfill_app(self.cfg, self.db, actor="human:owner")
            self.assertEqual(rows[0]["status"], "skipped")
            cards[1].team = "A"
            self.assertEqual(work_projects.agent_project(self.cfg, self.db, "dev1"), "A")
        self.assertEqual(work.get(self.db, lot["id"])["app"], "")
        self.assertEqual(work.events(self.db, lot["id"]), events)

    def test_unknown_creation_project_preserves_human_and_unclassified_flows(self):
        self.assertEqual(work_projects.creation_project(self.cfg, self.db, "human:owner"), "")
        self.assertEqual(work_projects.creation_project(self.cfg, self.db, "dev1", " "), "")

    def test_backfill_is_guarded_dry_run_idempotent_and_uses_creation_event(self):
        self.project("dev1", team="assigned")
        self.project("orch", team="created")
        assigned = work.add(self.db, title="Assigned", assignee="dev1", actor="orch")
        created = work.add(self.db, title="Created", actor="orch")
        for n in range(105):
            work.note(self.db, created["id"], str(n), actor="dev2")
        unknown = work.add(self.db, title="Unknown", actor="human:owner")
        closed = work.add(self.db, title="Closed", actor="orch")
        work.close(self.db, closed["id"], abandoned=True)
        named = work.add(self.db, title="Named", app="existing", actor="orch")
        before = {r["id"]: work.events(self.db, r["id"]) for r in (assigned, created, unknown)}
        proc = self.mesh("work", "backfill-app", "--dry-run", "--actor", "human:owner")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("planned — assigned", proc.stdout)
        self.assertIn("planned — created", proc.stdout)
        self.assertEqual({i: work.events(self.db, i) for i in before}, before)
        proc = self.mesh("work", "backfill-app", env=self.env(AGENT_MAIL_NAME="dev1"))
        self.assertNotEqual(proc.returncode, 0)
        proc = self.mesh("work", "backfill-app", "--actor", "human:owner")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for lot, project in ((assigned, "assigned"), (created, "created"), (unknown, ""),
                             (closed, ""), (named, "existing")):
            self.assertEqual(work.get(self.db, lot["id"])["app"], project)
        self.assertEqual(work.events(self.db, assigned["id"])[0]["actor"], "human:owner")
        self.assertEqual(work_projects.backfill_app(self.cfg, self.db, actor="human:owner"),
                         [{"id": unknown["id"], "status": "skipped", "reason": "no known project"}])

    def test_backfill_skips_ambiguous_assignee_without_creator_fallback(self):
        lot = work.add(self.db, title="Ambiguous", assignee="dev1", actor="orch")
        with mock.patch.object(work_projects, "agent_project",
                               side_effect=work.WorkError("ambiguous project")):
            rows = work_projects.backfill_app(self.cfg, self.db, actor="human:owner")
        self.assertEqual(rows[0]["status"], "skipped")
        self.assertEqual(work.get(self.db, lot["id"])["app"], "")

    def test_backfill_storage_does_not_overwrite_a_changed_lot(self):
        lot = work.add(self.db, title="Concurrent", assignee="dev1")
        api = storage.of(self.db).work
        self.assertIsNone(api.set_app(lot["id"], "p", actor="orch", if_empty=True, assignee="dev2"))
        api.set_app(lot["id"], "manual", actor="orch")
        events = work.events(self.db, lot["id"])
        self.assertIsNone(api.set_app(lot["id"], "p", actor="orch", if_empty=True, assignee="dev1"))
        self.assertEqual(work.get(self.db, lot["id"])["app"], "manual")
        self.assertEqual(work.events(self.db, lot["id"]), events)

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

    def test_package_exclusion_is_the_pilot_gesture(self):
        # #4043 : `move … build --package-exclusion` (sans boucle d'auteur)
        # passe la garde de `--correct` : refusé à un agent de développement.
        lot = work.add(self.db, title="Excluded")
        work.move(self.db, lot["id"], "build")
        work.move(self.db, lot["id"], "qa")
        cmd = ("work", "move", str(lot["id"]), "build", "--package-exclusion")
        proc = self.mesh(*cmd, env=self.env(AGENT_MAIL_NAME="dev1",
                                            AMEESH_ALERT_ORCHESTRATORS="orch"))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("package exclusion refused", proc.stderr)
        self.assertEqual(work.get(self.db, lot["id"])["state"], "qa")
        proc = self.mesh(*cmd, env=self.env(AGENT_MAIL_NAME="orch",
                                            AMEESH_ALERT_ORCHESTRATORS="orch"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        item = work.get(self.db, lot["id"])
        self.assertEqual((item["state"], int(item["loops"])), ("build", 0))
        self.assertEqual(work.events(self.db, lot["id"])[0]["actor"], "orch")

    def rejected_correction_identity(self, **identity_env):
        lot = work.add(self.db, title="Unclassified")
        events = work.events(self.db, lot["id"])
        env = self.env(AMEESH_ALERT_ORCHESTRATORS="orch", **identity_env)
        for command in (("set-app", str(lot["id"]), "p"),
                        ("move", str(lot["id"]), "merged", "--correct", "wrong state")):
            for actor in (None, "human:owner", "orch"):
                with self.subTest(command=command[0], actor=actor):
                    args = ("--actor", actor) if actor else ()
                    proc = self.mesh("work", *command, *args, env=env)
                    self.assertNotEqual(proc.returncode, 0, proc.stderr)
                    self.assertIn("correction refused: invalid session identity", proc.stderr)
                    item = work.get(self.db, lot["id"])
                    self.assertEqual((item["app"], item["state"]), ("", "intake"))
                    self.assertEqual(work.events(self.db, lot["id"]), events)

    def test_rejected_runner_cannot_correct_as_an_orchestrator(self):
        self.rejected_correction_identity(AGENT_MAIL_NAME="orch",
                                         AMEESH_RUNNER_ID="worker-runner",
                                         AMEESH_LEASE_EPOCH="1")

    def test_malformed_identity_cannot_correct_as_a_human(self):
        self.rejected_correction_identity(AGENT_MAIL_NAME="!")

    def test_design_role_uses_existing_correction_guard(self):
        lot = work.add(self.db, title="Unclassified")
        binding = SimpleNamespace(name="dev2", ok=True, source="session")
        agent = SimpleNamespace(title="dev2", fiche=SimpleNamespace(data={"roles": ["design"]}))
        canon = SimpleNamespace(agents=[agent], owns=lambda c: c is None)
        with mock.patch("ameesh.identity.resolve_binding", return_value=binding), \
                mock.patch("ameesh.canon.load_configured", return_value=[canon]):
            work_projects.set_app(self.cfg, self.db, lot["id"], "p")
        self.assertEqual(work.events(self.db, lot["id"])[0]["actor"], "dev2")

    def test_foreign_canon_namesake_cannot_grant_correction_roles(self):
        self.db.execute("UPDATE agent_registry SET canon = 'own' WHERE name = 'dev1'")
        def canon(ident, role):
            card = SimpleNamespace(title="dev1", fiche=SimpleNamespace(data={"roles": [role]}))
            return SimpleNamespace(owns=lambda c: c == ident, agents=[card])
        own, foreign = canon("own", "worker"), canon("foreign", "design")
        binding = SimpleNamespace(name="dev1", ok=True, source="session")
        lot = work.add(self.db, title="Unclassified")
        events = work.events(self.db, lot["id"])
        with mock.patch("ameesh.identity.resolve_binding", return_value=binding), \
                mock.patch("ameesh.canon.load_configured", return_value=[foreign, own]):
            for action in (lambda: work.corrector(self.cfg, self.db),
                           lambda: work_projects.set_app(self.cfg, self.db, lot["id"], "p"),
                           lambda: work_projects.backfill_app(self.cfg, self.db)):
                with self.assertRaisesRegex(work.WorkError, "correction refused"):
                    action()
            own.agents[0].fiche.data["roles"] = ["design"]
            self.assertEqual(work.corrector(self.cfg, self.db), "dev1")
        self.assertEqual(work.get(self.db, lot["id"])["app"], "")
        self.assertEqual(work.events(self.db, lot["id"]), events)

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
