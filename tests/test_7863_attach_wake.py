# SPDX-License-Identifier: AGPL-3.0-only
"""#7863 : réveil natif d'une session `attach` idle.

Preuve de qualification (note du lot) : un hook Claude Code `asyncRewake`
tourne en fond et rouvre un tour quand il sort en 2, sans toucher à la TUI.
Le réglage posé par l'attach enchaîne `SessionStart` et `Stop` sur
`agent-mail wait`, une veille en lecture seule. Ces tests couvrent la veille
(arrivée, passif, anti-boucle, mono-veilleur, aucun marquage lu), le document
de réglages et son branchement dans `run_attach` (version réellement lancée,
nettoyage au détachement).
"""
from __future__ import annotations

import contextlib
import fcntl
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import attach_wake, mail
from ameesh.runner import run_attach

from .support import FAKEBIN, PgTestCase


class DocumentTest(unittest.TestCase):
    def test_session_start_and_stop_arm_the_read_only_watcher(self):
        doc = attach_wake.document("alpha", 120, "/opt/bin/agent-mail")
        hooks = doc["hooks"]
        self.assertEqual(sorted(hooks), ["SessionStart", "Stop"])
        for event in ("SessionStart", "Stop"):
            entry = hooks[event][0]["hooks"][0]
            self.assertEqual(entry["type"], "command")
            self.assertTrue(entry["asyncRewake"])
            self.assertEqual(entry["timeout"], 150)
            self.assertIn("wait", entry["command"])
            self.assertIn("--agent alpha", entry["command"])
            self.assertIn("--timeout 120", entry["command"])
        self.assertIn("--delay 2", hooks["Stop"][0]["hooks"][0]["command"])
        self.assertNotIn("--delay", hooks["SessionStart"][0]["hooks"][0]["command"])

    def _binary(self, directory: str, version: str) -> str:
        path = os.path.join(directory, "claude")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\necho '%s (Claude Code)'\n" % version)
        os.chmod(path, 0o755)
        return path

    def test_old_version_gets_no_wake_hook(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = self._binary(directory, "2.1.9")
            cfg = SimpleNamespace(agent_dir=lambda _name: directory)
            wake = attach_wake.prepare(
                cfg, "alpha", harness="claude", binary=binary,
                env=os.environ.copy(), seconds=60, tag="1")
            self.assertIsNone(wake.path)
            self.assertEqual(wake.version, "2.1.9")
            self.assertIn("2.1.304", wake.reason)
            self.assertEqual(sorted(os.listdir(directory)), ["claude"])

    def test_settings_file_is_private_and_cleaned(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = self._binary(directory, "2.1.304")
            cfg = SimpleNamespace(agent_dir=lambda _name: directory)
            with mock.patch("ameesh.attach_wake.shutil.which",
                            return_value="/opt/bin/agent-mail"):
                wake = attach_wake.prepare(
                    cfg, "alpha", harness="claude", binary=binary,
                    env=os.environ.copy(), seconds=60, tag="7")
            self.assertTrue(wake.path)
            self.assertEqual(wake.version, "2.1.304")
            self.assertTrue(wake.path.endswith("attach-wake-7-%d.json" % os.getpid()))
            self.assertEqual(stat.S_IMODE(os.stat(wake.path).st_mode), 0o600)
            with open(wake.path, encoding="utf-8") as handle:
                self.assertEqual(json.load(handle), attach_wake.document(
                    "alpha", 60, "/opt/bin/agent-mail"))
            attach_wake.cleanup(wake)
            self.assertFalse(os.path.exists(wake.path))
            attach_wake.cleanup(wake)  # idempotent

    def test_non_claude_harness_gets_no_wake_hook(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = SimpleNamespace(agent_dir=lambda _name: directory)
            wake = attach_wake.prepare(
                cfg, "alpha", harness="codex", binary="codex",
                env=os.environ.copy(), seconds=60, tag="1")
            self.assertIsNone(wake.path)
            self.assertIn("codex", wake.reason)


class WaitCommandTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.register("w7863", "claude", cwd=self.tmp)

    def _watcher(self, *args: str) -> subprocess.Popen:
        proc = subprocess.Popen(
            [sys.executable, "-m", "ameesh.cli", "wait", *args],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=self.env(AGENT_MAIL_NAME="w7863"), cwd=self.tmp)
        self.addCleanup(self._reap, proc)
        return proc

    @staticmethod
    def _reap(proc: subprocess.Popen) -> None:
        if proc.poll() is None:
            proc.kill()
        try:
            proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    def test_non_passive_mail_ends_the_watch_without_marking_it_read(self):
        message_id = mail.send(self.db, "alpha", "w7863", "ALPHA-7863 sujet",
                               thread=False)
        proc = self._watcher("--timeout", "20", "--poll", "0.1", "--since", "0")
        out, err = proc.communicate(timeout=20)
        self.assertEqual(proc.returncode, 2, err)
        self.assertIn("#%d" % message_id, err)
        self.assertIn("ALPHA-7863", err)
        row = self.db.query(
            "SELECT delivered_at FROM agent_mailbox WHERE id = %s",
            (message_id,))[0]
        self.assertIsNone(row["delivered_at"])

    def test_passive_mail_does_not_wake(self):
        mail.send(self.db, "alpha", "w7863", "ACCUSE-7863", thread=False,
                  payload={"ack": True})
        proc = self._watcher("--timeout", "2", "--poll", "0.1", "--since", "0")
        out, err = proc.communicate(timeout=20)
        self.assertEqual(proc.returncode, 0, err)
        self.assertEqual(out, "")
        self.assertEqual(err, "")

    def test_mail_arriving_after_the_watch_starts_wakes_it(self):
        """Le cas du lot : session idle, le courrier arrive pendant la veille."""
        lock = os.path.join(self.state, "attach-wait", "w7863.lock")
        proc = self._watcher("--timeout", "20", "--poll", "0.1")
        self.wait_for(lambda: os.path.exists(lock), timeout=10)
        time.sleep(0.5)  # la borne de démarrage est posée juste après le verrou
        message_id = mail.send(self.db, "alpha", "w7863", "PENDANT-7863",
                               thread=False)
        out, err = proc.communicate(timeout=20)
        self.assertEqual(proc.returncode, 2, err)
        self.assertIn("#%d" % message_id, err)
        row = self.db.query(
            "SELECT delivered_at FROM agent_mailbox WHERE id = %s",
            (message_id,))[0]
        self.assertIsNone(row["delivered_at"])

    def test_a_message_already_unread_never_wakes_the_next_watch(self):
        """Anti-boucle : après un réveil, le Stop relance la veille sur le
        même message non livré ; elle ne doit pas le réveiller à nouveau."""
        message_id = mail.send(self.db, "alpha", "w7863", "BOUCLE-7863",
                               thread=False)
        first = self._watcher("--timeout", "20", "--poll", "0.1", "--since", "0")
        out, err = first.communicate(timeout=20)
        self.assertEqual(first.returncode, 2, err)
        self.assertIn("#%d" % message_id, err)
        # Le message est toujours non lu : une veille qui repart sans `--since`
        # prend sa borne au démarrage et ne réveille que le courrier suivant.
        second = self._watcher("--timeout", "2", "--poll", "0.1")
        out, err = second.communicate(timeout=20)
        self.assertEqual(second.returncode, 0, err)
        self.assertEqual(err, "")

    def test_one_watcher_per_agent(self):
        lock = os.path.join(self.state, "attach-wait", "w7863.lock")
        first = self._watcher("--timeout", "20", "--poll", "0.1")
        holder = []

        def armed() -> bool:
            if not os.path.exists(lock):
                return False
            fd = os.open(lock, os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                holder.append(fd)
                return True
            os.close(fd)
            return False

        self.wait_for(armed, timeout=10)
        second = self._watcher("--timeout", "20", "--poll", "0.1")
        out, err = second.communicate(timeout=20)
        self.assertEqual(second.returncode, 0, err)
        self.assertIsNone(first.poll())
        first.kill()
        first.communicate(timeout=10)
        for fd in holder:
            os.close(fd)

    def test_usage_errors(self):
        for args in (["--timeout", "0"], ["--poll", "0"], ["--agent", ".."],
                     ["--unknown"], ["--timeout"]):
            proc = self._watcher(*args)
            out, err = proc.communicate(timeout=20)
            self.assertEqual(proc.returncode, 2, (args, err))
            self.assertIn("usage: agent-mail wait", err)


class AttachWakeTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.register("att7863", "claude", cwd=self.tmp)
        patch = mock.patch.dict(os.environ, {"AMEESH_BIN_DIR": FAKEBIN,
                                             "AMEESH_TEST_LOG": self.turns_log})
        patch.start()
        self.addCleanup(patch.stop)

    def test_attach_arms_and_cleans_the_settings(self):
        seen: dict = {}
        real = attach_wake.prepare

        def capture(*args, **kwargs):
            wake = real(*args, **kwargs)
            seen["wake"] = wake
            if wake.path:
                seen["mode"] = stat.S_IMODE(os.stat(wake.path).st_mode)
                with open(wake.path, encoding="utf-8") as handle:
                    seen["doc"] = json.load(handle)
            return wake

        err = io.StringIO()
        with mock.patch("ameesh.attach_wake.shutil.which",
                        return_value="/opt/bin/agent-mail"):
            with mock.patch("ameesh.attach_wake.prepare", side_effect=capture):
                with contextlib.redirect_stderr(err):
                    code = run_attach(self.cfg, self.db, "att7863", max_seconds=30)
        self.assertEqual(code, 0, err.getvalue())
        wake = seen["wake"]
        self.assertTrue(wake.path, wake.reason)
        self.assertEqual(seen["mode"], 0o600)
        self.assertIn("SessionStart", seen["doc"]["hooks"])
        self.assertIn("Stop", seen["doc"]["hooks"])
        self.assertFalse(os.path.exists(wake.path))
        argv = self.turns()[0]["argv"]
        self.assertIn("--settings", argv)
        self.assertEqual(argv[argv.index("--settings") + 1], wake.path)

    def test_old_claude_gets_no_settings_but_records_the_version(self):
        err = io.StringIO()
        with mock.patch("ameesh.attach_wake.probe_version",
                        return_value="2.1.9"):
            with contextlib.redirect_stderr(err):
                code = run_attach(self.cfg, self.db, "att7863", max_seconds=30)
        self.assertEqual(code, 0, err.getvalue())
        argv = self.turns()[0]["argv"]
        self.assertNotIn("--settings", argv)
