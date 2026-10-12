# SPDX-License-Identifier: AGPL-3.0-only
"""L121 (#120): the watch of an agent, managed by ameesh: a systemd user
timer on the host, the versioned instruction (`consignes/veille.md`), the
watch recorded in the database (0362) and shown in `ameesh projects`."""
from __future__ import annotations

import dataclasses
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import fil, projects, storage, watch

from .support import PgTestCase


class TextTest(unittest.TestCase):
    def test_every(self):
        self.assertEqual(watch.parse_every("30m"), 1800)
        self.assertEqual(watch.parse_every("2h"), 7200)
        self.assertEqual(watch.parse_every("90"), 90)
        for bad in ("59", "0", "-5m", "soon", ""):
            with self.assertRaises(watch.WatchError, msg=bad):
                watch.parse_every(bad)
        self.assertEqual([watch.every_text(s) for s in (1800, 7200, 90)], ["30m", "2h", "90s"])

    def test_the_instruction_is_versioned_and_readable(self):
        text = watch.consigne("mesh-design")
        self.assertIn("mesh-design: make a SHORT check", text)
        self.assertNotIn("$agent", text)
        # the delivery rules of docs/ORCHESTRATEUR.md: merge first, prepare
        # every deployment, the owner keeps production
        for words in ("Merge first", "PREPARED in full", "rollback", "owner's decisions"):
            self.assertIn(words, text)
        self.assertIsNone(fil.unreadable_reason(text))

    def test_units(self):
        found = watch.units("mesh-design", 1800, "/home/u/.local/bin/ameesh", "/usr/bin:/bin")
        self.assertEqual(sorted(found), ["ameesh-watch-mesh-design.service",
                                         "ameesh-watch-mesh-design.timer"])
        service = found["ameesh-watch-mesh-design.service"]
        self.assertIn('ExecStart="/home/u/.local/bin/ameesh" watch send --agent mesh-design',
                      service)
        self.assertIn('Environment="PATH=/usr/bin:/bin"', service)
        timer = found["ameesh-watch-mesh-design.timer"]
        self.assertIn("OnActiveSec=1800\nOnUnitActiveSec=1800\n", timer)
        self.assertIn("WantedBy=timers.target", timer)


class WatchTest(PgTestCase):
    def setUp(self) -> None:
        self.db.execute("DELETE FROM agent_watches")
        self.register("w121", "claude")
        self.units = tempfile.mkdtemp(prefix="ameesh-l121-")
        self.addCleanup(shutil.rmtree, self.units, True)
        self.env = {watch.UNIT_DIR_ENV: self.units, "PATH": "/usr/bin:/bin"}
        self.calls: list = []
        self.here = dataclasses.replace(self.cfg, host="laptop")
        patcher = mock.patch.object(watch.platform, "is_linux", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_(self, argv, **kw):
        self.calls.append(argv[2:])
        return mock.Mock(returncode=0, stdout="", stderr="")

    def enable(self, cfg=None, every=1800):
        with mock.patch.object(watch.fil, "record") as note:
            warnings = watch.enable(cfg or self.here, self.db, "w121", every, run=self.run_,
                                    env=self.env, actor="human:owner")
        return warnings, note

    def test_enable_send_disable(self):
        warnings, note = self.enable()
        self.assertEqual(warnings, [])
        self.assertEqual(sorted(os.listdir(self.units)),
                         ["ameesh-watch-w121.service", "ameesh-watch-w121.timer"])
        self.assertEqual(self.calls, [["daemon-reload"],
                                      ["enable", "ameesh-watch-w121.timer"],
                                      ["restart", "ameesh-watch-w121.timer"]])
        [row] = storage.of(self.db).watches.list()
        self.assertEqual((row["agent"], row["every_s"], row["host"], row["enabled_by"],
                          row["last_sent_ts"]), ("w121", 1800, "laptop", "human:owner", None))
        self.assertIn("Watch of w121 enabled on laptop by human:owner: every 30m",
                      note.call_args.kwargs["text"])
        self.assertEqual(note.call_args.kwargs["meta"]["action"], "watch")
        # a firing sends the versioned instruction, from veille
        sent = []
        self.assertEqual(watch.send(self.here, self.db, "w121",
                                    mail_send=lambda argv: sent.append(argv) or 0),
                         "instruction sent to w121")
        self.assertEqual(sent, [["send", "w121", watch.consigne("w121"), "--from", "veille"]])
        self.assertIsNotNone(storage.of(self.db).watches.list("w121")[0]["last_sent_ts"])
        # a failed send is an error (the unit fails, visible in systemd)
        with self.assertRaises(watch.WatchError):
            watch.send(self.here, self.db, "w121", mail_send=lambda argv: 2)
        # disabled: no unit, no row, noted; a timer left behind sends nothing
        self.calls.clear()
        with mock.patch.object(watch.fil, "record") as note:
            self.assertEqual(watch.disable(self.here, self.db, "w121", run=self.run_,
                                           env=self.env, actor="human:owner"), [])
        self.assertEqual(os.listdir(self.units), [])
        self.assertEqual(self.calls, [["disable", "--now", "ameesh-watch-w121.timer"],
                                      ["daemon-reload"]])
        self.assertEqual(storage.of(self.db).watches.list(), [])
        self.assertIn("Watch of w121 disabled by human:owner", note.call_args.kwargs["text"])
        sent.clear()
        self.assertIn("nothing sent", watch.send(self.here, self.db, "w121",
                                                 mail_send=lambda argv: sent.append(argv) or 0))
        self.assertEqual(sent, [])
        with self.assertRaises(watch.WatchError):
            watch.disable(self.here, self.db, "w121", run=self.run_, env=self.env)

    def test_a_watch_moved_to_another_host(self):
        self.enable(cfg=dataclasses.replace(self.cfg, host="vm"))
        warnings, _ = self.enable(every=3600)
        self.assertEqual(len(warnings), 1)
        self.assertIn("moves from vm to laptop", warnings[0])
        self.assertEqual(storage.of(self.db).watches.list("w121")[0]["every_s"], 3600)
        # the timer left on vm sends nothing
        sent = []
        self.assertIn("is on laptop", watch.send(dataclasses.replace(self.cfg, host="vm"),
                                                 self.db, "w121",
                                                 mail_send=lambda argv: sent.append(argv) or 0))
        self.assertEqual(sent, [])

    def test_refusals_and_the_hand_written_unit(self):
        with self.assertRaises(watch.WatchError):
            watch.enable(self.here, self.db, "nobody", 1800, run=self.run_, env=self.env)
        self.calls.clear()
        self.run_ = lambda argv, **kw: mock.Mock(returncode=1, stdout="", stderr="no bus")
        with self.assertRaises(watch.WatchError):
            self.enable()
        self.assertEqual(storage.of(self.db).watches.list(), [])     # nothing recorded
        del self.run_
        open(os.path.join(self.units, "ameesh-veille-w121.timer"), "w").close()
        warnings, _ = self.enable()
        self.assertIn("hand-written watch of w121", warnings[0])

    def test_who_sets_the_watch_of_another_agent(self):
        # mesh-design, 2026-10-12: an agent sets its own watch; another's is
        # for humans and orchestrator or design agents (the rule of
        # `work move --correct`)
        from ameesh import assignments, identity
        def as_(name, orchestrator=False):
            return mock.patch.multiple(
                identity, resolve_binding=mock.Mock(return_value=SimpleNamespace(name=name))), \
                mock.patch.object(assignments, "is_orchestrator", return_value=orchestrator)
        def enable():
            with mock.patch.object(watch.fil, "record"):
                return watch.enable(self.here, self.db, "w121", 1800, run=self.run_,
                                    env=self.env)
        who, role = as_("dev9")
        with who, role, self.assertRaisesRegex(watch.WatchError, "only its own watch"):
            enable()
        self.assertEqual((os.listdir(self.units), self.calls), ([], []))   # nothing done
        who, role = as_("w121")
        with who, role:
            enable()
        self.assertEqual(storage.of(self.db).watches.list("w121")[0]["enabled_by"], "w121")
        who, role = as_("dev9")
        with who, role, self.assertRaises(watch.WatchError):
            watch.disable(self.here, self.db, "w121", run=self.run_, env=self.env)
        self.assertEqual(len(storage.of(self.db).watches.list("w121")), 1)
        who, role = as_("mesh-design", orchestrator=True)
        with who, role:
            enable()
        self.assertEqual(storage.of(self.db).watches.list("w121")[0]["enabled_by"],
                         "mesh-design")
        who, role = as_("")
        with who, role, mock.patch("getpass.getuser", return_value="owner"):
            enable()
        self.assertEqual(storage.of(self.db).watches.list("w121")[0]["enabled_by"],
                         "human:owner")

    def test_shown_in_projects(self):
        self.enable()
        view = projects.snapshot(self.db, paid_harnesses=())
        agent = next(a for p in view["projects"] for a in p["agents"] if a["name"] == "w121")
        self.assertEqual(agent["watch"], {"every_s": 1800, "host": "laptop",
                                          "last_sent_ts": None})
        text = "\n".join(projects.format_lines(view, 140, show_inactive=True))
        self.assertIn("watch: every 30m on laptop, never sent", text)
        narrow = "\n".join(projects.format_lines(view, 50, show_inactive=True))
        self.assertIn("watch: every 30m on laptop", narrow)


if __name__ == "__main__":
    unittest.main()
