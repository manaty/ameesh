# SPDX-License-Identifier: AGPL-3.0-only
"""#7644 : l'attache reprend la session sur le compte de la session.

Le compte actif de l'hôte n'est pas celui de la session : l'attache doit
reprendre `agent_registry.session_account` (même règle de continuité que
l'exécuteur), refuser lisiblement quand ce compte n'est plus utilisable, et
expliquer un harnais qui échoue dans les premières secondes.
"""
from __future__ import annotations

import contextlib
import dataclasses
import io
import os
import time
import uuid
from unittest import mock

from ameesh import account_turn, registry, storage
from ameesh.runner import run_attach

from .support import FAKEBIN, PgTestCase


def _account(path: str, *, creds: str = ".credentials.json") -> str:
    os.makedirs(path, exist_ok=True)
    os.chmod(path, 0o700)
    with open(os.path.join(path, creds), "w", encoding="utf-8"):
        pass
    os.chmod(os.path.join(path, creds), 0o600)
    return path


class AttachSessionAccountTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.register("att7644", "claude", cwd=self.tmp)
        self.primaire = _account(os.path.join(self.tmp, "comptes", "claude-1"))
        self.secondaire = _account(os.path.join(self.tmp, "comptes", "claude-2"))
        self.cfg = dataclasses.replace(self.cfg, accounts={"claude": [
            {"name": "primaire", "path": self.primaire},
            {"name": "secondaire", "path": self.secondaire},
        ]})
        self.sid = str(uuid.uuid4())
        self.db.execute(
            "UPDATE agent_registry SET session_id = %s, session_account = 'primaire' "
            "WHERE name = 'att7644'", (self.sid,))
        storage.of(self.db).accounts.init_active(
            self.cfg.host, "claude", "secondaire")
        self.db.execute(
            "UPDATE account_active SET account = 'secondaire' "
            "WHERE host = %s AND harness = 'claude'", (self.cfg.host,))
        patch = mock.patch.dict(os.environ, {"AMEESH_BIN_DIR": FAKEBIN,
                                            "AMEESH_TEST_LOG": self.turns_log})
        patch.start()
        self.addCleanup(patch.stop)

    def test_session_account_wins_over_the_active_account(self):
        """Rouge avant le correctif : l'attache partait sur le compte actif."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = run_attach(self.cfg, self.db, "att7644")
        self.assertEqual(code, 0, err.getvalue())
        turns = self.turns()
        self.assertEqual(len(turns), 1, err.getvalue())
        self.assertEqual(turns[0]["compte"]["CLAUDE_CONFIG_DIR"], self.primaire)
        self.assertIn(self.sid, turns[0]["argv"])
        self.assertIsNone(registry.get(self.db, "att7644")["lease_owner"])

    def test_unusable_session_account_refuses_with_the_way_out(self):
        os.chmod(self.primaire, 0o755)  # dossier devenu non privé
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = run_attach(self.cfg, self.db, "att7644")
        self.assertEqual(code, 1)
        message = err.getvalue()
        self.assertIn("primaire", message)
        self.assertIn("unusable", message)
        self.assertIn("ameesh resume att7644 --fresh", message)
        self.assertEqual(self.turns(), [])  # aucun harnais lancé
        self.assertIsNone(registry.get(self.db, "att7644")["lease_owner"])

    def test_wait_uses_the_session_account_written_by_the_finishing_turn(self):
        """#7644 : le compte se relit après l'attente, comme la session."""
        lease = registry.claim(self.db, "att7644", "runner-before", 30)
        self.assertIsNotNone(lease)
        self.db.execute(
            "UPDATE agent_registry SET status = 'running', session_id = 'old7644', "
            "session_account = 'secondaire' WHERE name = 'att7644'")
        original_sleep = time.sleep

        def finish(seconds):
            if seconds == 2.0:
                self.db.execute(
                    "UPDATE agent_registry SET status = 'idle', session_id = %s, "
                    "session_account = 'primaire' WHERE name = 'att7644'", (self.sid,))
            else:
                original_sleep(seconds)

        with mock.patch("ameesh.runner.time.sleep", side_effect=finish):
            code = run_attach(self.cfg, self.db, "att7644", wait=True)
        self.assertEqual(code, 0)
        self.assertEqual(self.turns()[0]["compte"]["CLAUDE_CONFIG_DIR"], self.primaire)

    def test_early_harness_failure_names_the_account_and_the_folder(self):
        with mock.patch.dict(os.environ, {"AMEESH_TEST_EXIT": "1"}):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = run_attach(self.cfg, self.db, "att7644")
        self.assertEqual(code, 1)
        message = err.getvalue()
        self.assertIn("attach:", message)
        self.assertIn("primaire", message)
        self.assertIn(os.path.realpath(os.path.join(self.primaire, "projects")),
                      message)
        self.assertIn("not found", message)
        self.assertIn("ameesh resume att7644", message)


class AttachSessionAccountCodexTest(PgTestCase):
    """#7644 (4) : le même contrôle vaut pour Codex (session non portable)."""

    def setUp(self):
        super().setUp()
        self.register("cdx7644", "codex", cwd=self.tmp)
        self.primaire = _account(os.path.join(self.tmp, "comptes", "codex-1"),
                                 creds="auth.json")
        self.secondaire = _account(os.path.join(self.tmp, "comptes", "codex-2"),
                                   creds="auth.json")
        self.cfg = dataclasses.replace(self.cfg, accounts={"codex": [
            {"name": "primaire", "path": self.primaire},
            {"name": "secondaire", "path": self.secondaire},
        ]})
        self.db.execute(
            "UPDATE agent_registry SET session_id = %s, session_account = 'primaire' "
            "WHERE name = 'cdx7644'", (str(uuid.uuid4()),))
        storage.of(self.db).accounts.init_active(self.cfg.host, "codex", "secondaire")
        self.db.execute(
            "UPDATE account_active SET account = 'secondaire' "
            "WHERE host = %s AND harness = 'codex'", (self.cfg.host,))

    def test_codex_resumes_on_its_session_account(self):
        # `registry.get` normalise la ligne ; c'est ce que voit `_run_attach`.
        ok, profile = account_turn.attach_profile(
            self.cfg, self.db, "codex", row=registry.get(self.db, "cdx7644"))
        self.assertTrue(ok)
        self.assertIsNotNone(profile)
        self.assertEqual(profile.name, "primaire")

    def test_codex_unusable_session_account_is_refused(self):
        os.chmod(self.primaire, 0o755)
        ok, profile = account_turn.attach_profile(
            self.cfg, self.db, "codex", row=registry.get(self.db, "cdx7644"))
        self.assertFalse(ok)
        self.assertIsNone(profile)
