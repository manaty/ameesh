# SPDX-License-Identifier: AGPL-3.0-only
"""Memory isolation guards and designation/sync through a real fake-harness turn."""
from __future__ import annotations

import dataclasses
import io
import types
import os
import subprocess
import threading
from unittest import mock

from ameesh import persona_memory as pm, registry, identity, db as db_mod, mail
from ameesh.runner import AgentWorker, Runner
from .support import FAKEBIN, PgTestCase


class MemoryRunnerTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.bare = os.path.join(self.tmp, "memory.git")
        seed = os.path.join(self.tmp, "seed")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", self.bare], check=True)
        subprocess.run(["git", "clone", "-q", self.bare, seed], check=True, capture_output=True)
        for args in (("config", "user.name", "test"), ("config", "user.email", "test@test")):
            subprocess.run(["git", "-C", seed, *args], check=True)
        with open(os.path.join(seed, "MEMORY.md"), "w") as fh:
            fh.write("- Durable project fact\n")
        for args in (("add", "MEMORY.md"), ("commit", "-qm", "init"), ("push", "-q")):
            subprocess.run(["git", "-C", seed, *args], check=True)
        cwd = os.path.join(self.tmp, "work")
        os.makedirs(cwd)
        self.register("memory-a", "claude", cwd=cwd)
        self.db.execute("UPDATE agent_registry SET memory_repository = %s WHERE name = 'memory-a'",
                        (self.bare,))
        self.runner = Runner(dataclasses.replace(self.cfg, persona_memory={}), self.db, once=True)
        lease = registry.claim(self.db, "memory-a", self.runner.runner_id, 3600)
        self.assertIsNotNone(lease)
        self.worker = AgentWorker(self.runner, registry.get(self.db, "memory-a"), lease)
        self.addCleanup(self.worker.watchdog_stop.set)

    def _turn(self):
        with mock.patch.dict(os.environ, {"AMEESH_BIN_DIR": FAKEBIN,
                                         "AMEESH_TEST_LOG": self.turns_log}):
            self.assertTrue(self.worker.run_turn({"kind": "prompt", "prompt": "work", "ids": []}))
            # #4692 : joindre avant de restaurer os.environ ; sinon la synchro
            # copie l'environnement pendant sa restauration (KeyError) et ne pousse rien.
            for thread in threading.enumerate():
                if thread.name == "memory-a-memory":
                    thread.join(60)
                    self.assertFalse(thread.is_alive())

    def test_run_turn_designates_memory_and_automatically_syncs(self):
        original = pm.preface
        def designate(path, agent, session):
            text = original(path, agent, session)
            os.makedirs(os.path.join(path, "journal"))
            with open(pm.journal_path(path, session), "w") as fh:
                fh.write("durable turn fact")
            return text
        with mock.patch.object(pm, "preface", side_effect=designate) as preface, \
                mock.patch.object(pm, "sync", wraps=pm.sync) as sync:
            self._turn()
        preface.assert_called_once()
        sync.assert_called_once()
        self.assertEqual(sync.call_args.kwargs["repository"], self.bare)
        self.assertIn("PERSONA MEMORY", " ".join(self.turns()[0]["argv"]))
        names = subprocess.check_output(["git", "-C", self.bare, "ls-tree", "-r", "--name-only", "main"], text=True)
        self.assertIn("journal/session-", names)

    def test_resumed_activation_discards_inactive_persona_forgery(self):
        path = pm.open_copy(self.bare, self.cfg.state_dir, "memory-a")
        with open(os.path.join(path, "MEMORY.md"), "w") as fh:
            fh.write("forged by an inactive peer")
        for args in (("add", "-A"), ("commit", "-qm", "forged while stopped")):
            subprocess.run(["git", "-C", path, *args], check=True)
        self.worker.agent["session_id"] = "resumed-session"
        with mock.patch.object(self.worker, "_session_connue", return_value=lambda _: True), \
                self.assertLogs(pm.__name__, level="WARNING"):
            self._turn()
        self.assertNotIn("forged", pm.index_text(path))
        self.assertIn("Durable project fact", pm.index_text(path))
        remote = subprocess.check_output(["git", "-C", self.bare, "show", "main:MEMORY.md"], text=True)
        self.assertNotIn("forged", remote)
        self.assertIn("PERSONA MEMORY", " ".join(self.turns()[0]["argv"]))

    def test_failed_activation_never_syncs_local_copy(self):
        with mock.patch.object(pm, "open_copy", side_effect=pm.MemoryError("offline")), \
                mock.patch.object(pm, "sync") as sync:
            self._turn()
        sync.assert_not_called()

    def test_active_consolidation_preserves_uncommitted_and_rejected_journals(self):
        path = pm.open_copy(self.bare, self.cfg.state_dir, "memory-a")
        os.makedirs(os.path.join(path, "journal"))
        committed = os.path.join(path, "journal", "rejected.md")
        with open(committed, "w") as stream:
            stream.write("sk-abcdefghijklmnopqrstuvwxyz0123")
        for args in (("add", "-A"), ("commit", "-qm", "rejected secret")):
            subprocess.run(["git", "-C", path, *args], check=True)
        uncommitted = os.path.join(path, "journal", "current.md")
        with open(uncommitted, "w") as stream:
            stream.write("current turn's unsynced journal")
        head = subprocess.check_output(["git", "-C", path, "rev-parse", "HEAD"], text=True)
        binding = identity.Binding(name="memory-a", source="runner", ok=True)
        connection = types.SimpleNamespace(query=self.db.query, close=lambda: None)
        with mock.patch("ameesh.config.load", return_value=self.runner.cfg), \
                mock.patch.object(db_mod, "connect", return_value=connection), \
                mock.patch.object(identity, "resolve_binding", return_value=binding), \
                mock.patch.object(pm, "open_copy", wraps=pm.open_copy) as opened, \
                mock.patch.object(mail, "send") as sent, mock.patch('sys.stdout', io.StringIO()):
            self.assertEqual(pm.main(["consolidate", "memory-a"]), 0)
        self.assertTrue(os.path.exists(uncommitted), "consolidation lost the current journal")
        self.assertTrue(os.path.exists(committed), "consolidation lost the rejected commit")
        sent.assert_called_once()
        self.assertIn(path, sent.call_args.args[3])
        self.assertEqual(subprocess.check_output(["git", "-C", path, "rev-parse", "HEAD"], text=True), head)
        with open(uncommitted) as stream:
            self.assertIn("current turn", stream.read())
        with open(committed) as stream:
            self.assertIn("sk-", stream.read())
        opened.assert_not_called()
        self.assertEqual(pm.sync(path, "later", repository=self.bare)["status"], "secret")

    def test_active_peer_disables_designation_and_sync(self):
        registry.upsert(self.db, "memory-b", harness="codex", host=self.cfg.host, mode="execute")
        self.db.execute("UPDATE agent_registry SET status = 'running' WHERE name = 'memory-b'")
        with mock.patch.object(pm, "open_copy") as opened, mock.patch.object(pm, "sync") as sync:
            self._turn()
        opened.assert_not_called()
        sync.assert_not_called()
        self.assertNotIn("PERSONA MEMORY", " ".join(self.turns()[0]["argv"]))

    def test_guard_refuses_active_peer_and_accepts_policy_exception(self):
        registry.upsert(self.db, "memory-b", harness="codex", host=self.cfg.host, mode="execute")
        self.db.execute("UPDATE agent_registry SET lease_owner = 'other', "
                        "lease_expires_at = now() + interval '1 minute' WHERE name = 'memory-b'")
        with self.assertRaisesRegex(pm.MemoryError, "share UID"):
            pm.require_isolation(self.cfg, self.db, "memory-a")
        with mock.patch.object(pm, "isolation_policy", return_value="shared-uid-accepted"):
            pm.require_isolation(self.cfg, self.db, "memory-a")

    def test_resumed_sync_refuses_changed_repository(self):
        path = pm.open_copy(self.bare, self.cfg.state_dir, "memory-a")
        with open(os.path.join(path, "note.md"), "w") as fh:
            fh.write("durable note")
        other = os.path.join(self.tmp, "other.git")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", other], check=True)
        self.worker._memory_activated = True
        self.worker.agent["memory_repository"] = other
        with mock.patch.object(pm, "_git", wraps=pm._git) as git:
            self.worker._sync_memory()
            for thread in threading.enumerate():
                if thread.name == "memory-a-memory":
                    thread.join(10)
        self.assertFalse(any("push" in call.args[1:] for call in git.call_args_list))
        self.assertTrue(os.path.exists(os.path.join(path, "note.md")))
