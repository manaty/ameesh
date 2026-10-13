# SPDX-License-Identifier: AGPL-3.0-only
"""Mediated background ownership: contract, old-server fallback and failures."""
from __future__ import annotations

import dataclasses
import os
import signal
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

from . import support  # noqa: F401
from ameesh import background, platform, storage
from ameesh.db import DbError
from ameesh.mediated_executor import contract as C
from ameesh.storage.remote.client import RemoteDb
from ameesh.platform.signals import ProcessHandle


class MediatedOwnershipTest(unittest.TestCase):
    def test_read_operation_is_admitted_without_transport(self):
        op = C.load().get("turn_resources.running_started_before")
        self.assertFalse(op.write)
        self.assertIn("H", op.scope)
        self.assertEqual(op.forced["host"], "executor_host")
        self.assertIn("admitted_agents", op.rules)
        db = RemoteDb(cfg=None, transport=object())
        with self.assertRaises(AttributeError):
            storage.of(db).turn_resources.running_started_before("h", 0.0)

    @unittest.skipUnless(platform.is_linux() and hasattr(os, "pidfd_open")
                         and hasattr(signal, "pidfd_send_signal"), "Linux pidfds")
    def test_real_remote_cleanup_matrix(self):
        for source, protected, orphan, killed in (
                ("old-client", False, True, True),
                ("old-server", False, True, True),
                ("old-server", True, True, False),
                ("registry", False, False, True),
                ("registry", True, True, False),
                ("empty-admission", False, True, False),
                ("network", False, True, False)):
            with self.subTest(source=source, protected=protected):
                self._cleanup(source, protected, orphan, killed)

    def _cleanup(self, source, protected, orphan, killed):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                 start_new_session=True)
        handle = None
        requests, signals, notes = [], [], []
        class Transport:
            def call(self, request, *, idempotency_key):
                requests.append(request)
                if request.op == "turn_resources.running_started_before":
                    if source == "old-server":
                        raise C.NotSupportedRemotely("old server")
                    if source == "network":
                        raise DbError("network unavailable")
                    if source == "empty-admission":
                        raise C.Forbidden("no admitted agents")
                    rows = ([{"id": 1, "turn_id": "foreign", "pgid": child.pid}]
                            if protected else [])
                    return C.OpResult(rows, 1.0)
                return C.OpResult(None, 1.0)
        try:
            table = platform.process_table()
            member = next(p for p in table if p["pid"] == child.pid)
            handle = ProcessHandle.open(child.pid, member["start"])
            self.assertIsNotNone(handle)
            contract = C.load()
            if source == "old-client":
                contract = dataclasses.replace(contract, operations={
                    k: v for k, v in contract.operations.items()
                    if k != "turn_resources.running_started_before"})
            db = RemoteDb(cfg=None, transport=Transport(), contract=contract)
            db.book.hold("test", "exec:test:h:1", 1)
            db.book.open_turn("own", "test")
            runner = SimpleNamespace(db=db, mediated=True, host="h",
                                     harness_pids={child.pid} if protected and
                                     source == "old-server" else set(),
                                     container_runtime=lambda: None)
            tracker = background.Tracker(runner, log=notes.append)
            tracker.TERM_GRACE_S = .05
            tracker.entries = [background.Entry(agent="test", turn_id="own", pgid=7000,
                               ended=0, deadline=1, grace_s=1, handles={child.pid: handle})]
            original = signal.pidfd_send_signal
            def send(fd, signum, *args):
                signals.append(signum)
                return original(fd, signum, *args)
            with mock.patch.object(signal, "pidfd_send_signal", side_effect=send):
                tracker.tick(now=0)
                tracker.tick(now=2)
            self.assertEqual(platform.alive(child.pid), not killed)
            if killed:
                self.assertIn(signal.SIGTERM, signals)
            else:
                self.assertEqual(signals, [])
            close = [r for r in requests if r.op == "turn_resources.close_turn"]
            self.assertEqual(len(close), 1)
            self.assertEqual(close[0].kwargs["orphan"], orphan)
            if source == "network":
                self.assertGreaterEqual(sum(r.op == "turn_resources.running_started_before"
                                            for r in requests), 2)
        finally:
            if handle is not None:
                handle.close()
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)

    def test_registry_pagination_visits_every_page(self):
        runner = SimpleNamespace(db=object(), host="h", harness_pids=set())
        tracker = background.Tracker(runner, log=lambda _: None)
        entry = background.Entry(agent="test", turn_id="own", pgid=7000,
                                 ended=0, deadline=1, grace_s=1)
        rows = [{"id": i, "pgid": 8000+i, "turn_id": "foreign"} for i in range(1, 202)]
        def page(host, before, *, after_id, limit):
            return [r for r in rows if r["id"] > after_id][:min(limit, 200)]
        with mock.patch.object(background.storage, "of") as store:
            store.return_value.turn_resources.running_started_before.side_effect = page
            self.assertIn(8201, tracker._foreign_groups(entry))
