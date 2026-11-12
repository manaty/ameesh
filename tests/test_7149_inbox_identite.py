# SPDX-License-Identifier: AGPL-3.0-only
"""#7149 : la boîte d'un agent mené par l'exécuteur n'est pas lisible par un autre.

Incident : un watcher du bureau a lu en SQL direct le courrier de claude2
(64 messages depuis le 10/11) et en a traité trois comme siens.  La remise
agent-mail n'était pas en cause ; ces gardes ferment les chemins CLI de
lecture croisée : une identité annoncée qui contredit la liaison de la session
est refusée, et `inbox`/`show` refusent le courrier d'un agent `execute` à qui
n'y est pas lié.
"""
from __future__ import annotations

import os

from ameesh import mail, registry, session_bindings
from .support import PgTestCase


class InboxIdentityTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.db.execute("TRUNCATE session_bindings RESTART IDENTITY")
        registry.upsert(self.db, "sender", host=self.cfg.host, harness="claude")
        registry.upsert(self.db, "victim", host=self.cfg.host, harness="claude",
                        mode="execute")
        registry.upsert(self.db, "persona", host=self.cfg.host, harness="claude",
                        mode="externe")
        registry.upsert(self.db, "bureau", host=self.cfg.host, harness="claude",
                        mode="externe")
        mail.send(self.db, "sender", "victim", "secret pour victim")
        mail.send(self.db, "sender", "persona", "courrier de persona")
        mail.send(self.db, "sender", "bureau", "courrier du bureau")

    def test_another_execute_box_is_refused_by_name(self):
        proc = self.cli("inbox", "victim")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("driven by its executor", proc.stderr)
        self.assertNotIn("secret pour victim", proc.stdout)

    def test_another_execute_box_is_refused_with_inherited_name(self):
        proc = self.cli("inbox", env=self.env(AGENT_MAIL_NAME="victim"))
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("driven by its executor", proc.stderr)
        self.assertNotIn("secret pour victim", proc.stdout)

    def test_external_persona_box_stays_readable(self):
        proc = self.cli("inbox", "persona")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("courrier de persona", proc.stdout)

    def test_announced_name_contradicting_the_binding_is_refused(self):
        session_bindings.bind(self.cfg, self.db, "bureau", session_id="test-7149",
                              harness="codex", pid=os.getpid(), by="bureau")
        proc = self.cli("inbox", env=self.env(AGENT_MAIL_NAME="persona"))
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("ambiguous identity", proc.stderr)
        self.assertNotIn("courrier de persona", proc.stdout)
        self.assertNotIn("courrier du bureau", proc.stdout)

    def test_show_refuses_an_inherited_execute_identity(self):
        mid = mail.send(self.db, "sender", "victim", "secret pour show")
        proc = self.cli("show", str(mid), env=self.env(AGENT_MAIL_NAME="victim"))
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("driven by its executor", proc.stderr)
        self.assertNotIn("secret pour show", proc.stdout)

    def test_show_allows_the_bound_session(self):
        mid = mail.send(self.db, "sender", "victim", "secret pour show lie")
        session_bindings.bind(self.cfg, self.db, "victim", session_id="test-7149-show",
                              harness="claude", pid=os.getpid(), by="victim", force=True)
        proc = self.cli("show", str(mid))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("secret pour show lie", proc.stdout)

    def test_announced_name_cannot_send_without_from(self):
        session_bindings.bind(self.cfg, self.db, "bureau", session_id="test-7149",
                              harness="codex", pid=os.getpid(), by="bureau")
        proc = self.cli("send", "persona", "hello",
                        env=self.env(AGENT_MAIL_NAME="persona"))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("ambiguous identity", proc.stderr)

    def test_bound_session_reads_its_own_box(self):
        session_bindings.bind(self.cfg, self.db, "bureau", session_id="test-7149",
                              harness="codex", pid=os.getpid(), by="bureau")
        proc = self.cli("inbox")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("courrier du bureau", proc.stdout)
