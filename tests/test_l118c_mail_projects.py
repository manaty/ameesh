# SPDX-License-Identifier: AGPL-3.0-only
"""L118c patch: automatic mail projects and existing correction identity guard."""
from types import SimpleNamespace
from unittest import mock

from ameesh import work, work_projects
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

    def rejected_correction_identity(self, **identity_env):
        lot = work.add(self.db, title="Unclassified")
        events = work.events(self.db, lot["id"])
        env = self.env(AMEESH_ALERT_ORCHESTRATORS="orch", **identity_env)
        for command in (("move", str(lot["id"]), "merged", "--correct", "wrong state"),):
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
            for action in (lambda: work.corrector(self.cfg, self.db),):
                with self.assertRaisesRegex(work.WorkError, "correction refusée"):
                    action()
            own.agents[0].fiche.data["roles"] = ["design"]
            self.assertEqual(work.corrector(self.cfg, self.db), "dev1")
        self.assertEqual(work.get(self.db, lot["id"])["app"], "")
        self.assertEqual(work.events(self.db, lot["id"]), events)

    def test_unknown_project_preserves_existing_creation(self):
        proc = self.send_new()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(work.list_items(self.db)[0]["app"], "")

    def test_ambiguous_own_canon_project_is_refused(self):
        self.db.execute("UPDATE agent_registry SET canon = 'own' WHERE name = 'orch'")
        cards = [SimpleNamespace(title="orch", team=p) for p in ("A", "B")]
        canon = SimpleNamespace(owns=lambda c: c == "own", agents=cards)
        with mock.patch.object(work, "configured_canons", return_value=[canon]):
            with self.assertRaisesRegex(work.WorkError, "ambiguous"):
                work_projects.mail_project(self.cfg, self.db, "orch", "dev1")
            cards[1].team = "A"
            self.assertEqual(work_projects.mail_project(self.cfg, self.db, "orch", "dev1"), "A")
