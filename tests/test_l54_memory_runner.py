# SPDX-License-Identifier: AGPL-3.0-only
"""Memory isolation guards and designation/sync through a real fake-harness turn."""
from __future__ import annotations

import dataclasses
import os
import subprocess
import threading
from unittest import mock

from ameesh import persona_memory as pm, registry
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
        for thread in threading.enumerate():
            if thread.name == "memory-a-memory":
                thread.join(10)
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
        self.worker.agent["memory_repository"] = other
        with mock.patch.object(pm, "_git", wraps=pm._git) as git:
            self.worker._sync_memory()
            for thread in threading.enumerate():
                if thread.name == "memory-a-memory":
                    thread.join(10)
        self.assertFalse(any("push" in call.args[1:] for call in git.call_args_list))
        self.assertTrue(os.path.exists(os.path.join(path, "note.md")))
