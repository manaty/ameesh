# SPDX-License-Identifier: AGPL-3.0-only
"""L121 (#120): the watch of an agent, managed by ameesh: a systemd user
timer on the host, the versioned instruction (`consignes/veille.md`), the
watch recorded in the database (0362) and shown in `ameesh projects`."""
from __future__ import annotations

import dataclasses
import os
import stat
import subprocess
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import db as db_mod
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
        # codex r1 (#303): the host's ameesh environment, then the watch's
        # pinned variables (the later file wins in systemd)
        self.assertIn("TimeoutStartSec=300\n", service)
        self.assertIn("EnvironmentFile=-%h/.config/ameesh/env\n"
                      "EnvironmentFile=-/units/ameesh-watch-mesh-design.env\n",
                      watch.units("mesh-design", 1800, "/a", "/b", "/units")
                      ["ameesh-watch-mesh-design.service"])
        timer = found["ameesh-watch-mesh-design.timer"]
        self.assertIn("OnActiveSec=1800\nOnUnitActiveSec=1800\n", timer)
        self.assertIn("WantedBy=timers.target", timer)


    def test_pinned_env(self):
        text = watch.pinned_env({"AMEESH_DSN": 'postgres://u:p"a\\s$x`y@h/db',
                                 "AGENT_MESH_SCHEMA": "s", "AMEESH_EMPTY": "",
                                 "HOME": "/home/u", "AMEESH_POLL": "3"}, "laptop")
        self.assertEqual(text, 'AMEESH_DSN="postgres://u:p\\"a\\\\s\\$x\\`y@h/db"\n'
                               'AGENT_MESH_DSN=""\nAMEESH_DATABASE_URL=""\n'
                               'AGENT_MESH_DATABASE_URL=""\n'
                               'AMEESH_SCHEMA="s"\nAGENT_MESH_SCHEMA=""\n'
                               'AMEESH_HOST="laptop"\nAGENT_MESH_HOST=""\n')
        self.assertEqual(watch.pinned_env({}, ""), "")
        with self.assertRaises(watch.WatchError):
            watch.pinned_env({"AMEESH_DSN": "a\nb"}, "h")

    def test_pinned_values_win_over_the_host_file(self):
        # codex r2 (#303): the resolved value under the canonical name, the
        # other names emptied; the host's env file comes first, the watch's last
        from ameesh import config as config_mod
        pinned = watch.pinned_env({"AMEESH_DATABASE_URL": "postgresql://b/db",
                                   "AMEESH_DSN": "", "AGENT_MESH_HOST": "vm-b"}, "vm-b")
        host_file = {"AMEESH_DSN": "postgresql://a/db", "AMEESH_HOST": "vm-a",
                     "AGENT_MESH_DATABASE_URL": "postgresql://a2/db"}
        timer_env = dict(host_file)
        for line in pinned.splitlines():
            name, _, value = line.partition("=")
            timer_env[name] = value.strip('"')
        cfg = config_mod.load(env=dict(timer_env, AMEESH_CONFIG="/nonexistent"))
        self.assertEqual((cfg.dsn, cfg.host), ("postgresql://b/db", "vm-b"))

    def test_linger(self):
        def answer(code=0, out="", err=""):
            return lambda argv, **kw: mock.Mock(returncode=code, stdout=out, stderr=err)
        self.assertIsNone(watch.linger_warning(answer(out="yes\n"), "u"))
        self.assertIn("linger is off for u", watch.linger_warning(answer(out="no\n"), "u"))
        for run in (answer(code=1, err="no such user"), answer(out="")):
            self.assertIn("unreadable", watch.linger_warning(run, "u"))
        def fails(argv, **kw):
            raise FileNotFoundError("loginctl")
        self.assertIn("enable-linger u", watch.linger_warning(fails, "u"))


class WatchTest(PgTestCase):
    def setUp(self) -> None:
        self.db.execute("DELETE FROM agent_watches")
        self.register("w121", "claude")
        self.units = tempfile.mkdtemp(prefix="ameesh-l121-")
        self.addCleanup(shutil.rmtree, self.units, True)
        self.env = {watch.UNIT_DIR_ENV: self.units, "PATH": "/usr/bin:/bin",
                    "AMEESH_DSN": "postgres://u:secret@db/x", "AMEESH_HOST": "laptop"}
        self.linger = "yes"
        self.calls: list = []
        self.here = dataclasses.replace(self.cfg, host="laptop")
        patcher = mock.patch.object(watch.platform, "is_linux", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def failing(tx, to, body):
        raise RuntimeError("mailbox refused")

    def run_(self, argv, **kw):
        if argv[0] == "loginctl":
            return mock.Mock(returncode=0, stdout=self.linger + "\n", stderr="")
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
                         ["ameesh-watch-w121.env", "ameesh-watch-w121.service",
                          "ameesh-watch-w121.timer"])
        # the variables set at enable are pinned for the timer, owner only
        env_file = os.path.join(self.units, "ameesh-watch-w121.env")
        with open(env_file, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), 'AMEESH_DSN="postgres://u:secret@db/x"\n'
                                        'AGENT_MESH_DSN=""\nAMEESH_DATABASE_URL=""\n'
                                        'AGENT_MESH_DATABASE_URL=""\n'
                                        'AMEESH_HOST="laptop"\nAGENT_MESH_HOST=""\n')
        self.assertEqual(stat.S_IMODE(os.stat(env_file).st_mode), 0o600)
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
        with mock.patch.object(watch.fil, "record") as thread:
            self.assertEqual(watch.send(self.here, self.db, "w121"),
                             "instruction sent to w121")
        [message] = [m for m in storage.of(self.db).mailbox.unread("w121", 200)
                     if m["sender"] == "veille"]
        self.assertEqual(message["body"], watch.consigne("w121"))
        self.assertEqual(thread.call_args.kwargs["mailbox_ids"], [int(message["id"])])
        self.assertIsNotNone(storage.of(self.db).watches.list("w121")[0]["last_sent_ts"])
        sent = []
        # a failed send is an error (the unit fails, visible in systemd)
        with self.assertRaises(watch.WatchError):
            watch.send(self.here, self.db, "w121", mail_send=self.failing)
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
                                                 mail_send=lambda tx, to, body: sent.append(to) or 1))
        self.assertEqual(sent, [])
        with self.assertRaises(watch.WatchError):
            watch.disable(self.here, self.db, "w121", run=self.run_, env=self.env)

    def test_linger_off_is_a_warning(self):
        self.linger = "no"
        warnings, _ = self.enable()
        self.assertEqual(len(warnings), 1)
        self.assertIn("loginctl enable-linger", warnings[0])
        self.assertEqual(len(storage.of(self.db).watches.list()), 1)  # enabled all the same

    def test_a_watch_moved_to_another_host(self):
        vm = dataclasses.replace(self.cfg, host="vm")
        vm_units = tempfile.mkdtemp(prefix="ameesh-l121-vm-")
        self.addCleanup(shutil.rmtree, vm_units, True)
        vm_env = dict(self.env, **{watch.UNIT_DIR_ENV: vm_units})
        with mock.patch.object(watch.fil, "record"):
            watch.enable(vm, self.db, "w121", 1800, run=self.run_, env=vm_env,
                         actor="human:owner")
        warnings, _ = self.enable(every=3600)
        self.assertEqual(len(warnings), 1)
        self.assertIn("moves from vm to laptop", warnings[0])
        self.assertEqual(storage.of(self.db).watches.list("w121")[0]["every_s"], 3600)
        # the timer left on vm sends nothing
        sent = []
        self.assertIn("is on laptop", watch.send(vm, self.db, "w121",
                                                 mail_send=lambda tx, to, body: sent.append(to) or 1))
        self.assertEqual(sent, [])
        # codex r1 (#303): disable on the old host removes its timer only;
        # the watch, now on laptop, stays
        with mock.patch.object(watch.fil, "record") as note:
            warnings = watch.disable(vm, self.db, "w121", run=self.run_, env=vm_env,
                                     actor="human:owner")
        self.assertEqual(os.listdir(vm_units), [])
        self.assertIn("only the timer of vm was removed", warnings[0])
        self.assertIn("stays on laptop", note.call_args.kwargs["text"])
        self.assertEqual(storage.of(self.db).watches.list("w121")[0]["host"], "laptop")
        self.assertEqual(watch.send(self.here, self.db, "w121",
                                    mail_send=lambda tx, to, body: sent.append(to) or 1),
                         "instruction sent to w121")
        # from a host with no timer left: the watch itself is disabled
        with mock.patch.object(watch.fil, "record"):
            warnings = watch.disable(vm, self.db, "w121", run=self.run_, env=vm_env,
                                     actor="human:owner")
        self.assertIn("its timer stays on laptop", warnings[0])
        self.assertEqual(storage.of(self.db).watches.list(), [])

    def test_delete_keeps_a_watch_moved_meanwhile(self):
        self.enable()
        watches = storage.of(self.db).watches
        self.assertIsNone(watches.delete("w121", "vm"))
        self.assertEqual(len(watches.list("w121")), 1)
        self.assertEqual(watches.delete("w121", "laptop")["host"], "laptop")

    def test_a_firing_holds_the_watch_until_it_is_sent(self):
        # codex r1 (#303): the host check and the sending are one transaction;
        # a move from another host waits for the end of the firing
        self.enable()
        other = db_mod.connect(self.cfg)
        self.addCleanup(other.close)
        moves = []

        def mail_send(tx, to, body):
            try:
                # one statement: the psql driver runs each in its own session
                other.execute("set lock_timeout = '300ms';"
                              " update agent_watches set host = 'vm' where agent = 'w121'")
                moves.append("moved")
            except db_mod.DbError as exc:
                moves.append("blocked: %s" % exc)
            return 1
        self.assertEqual(watch.send(self.here, self.db, "w121", mail_send=mail_send),
                         "instruction sent to w121")
        self.assertEqual(len(moves), 1)
        self.assertTrue(moves[0].startswith("blocked"), moves)
        # moved after the firing: the next firing from laptop sends nothing
        other.execute("update agent_watches set host = 'vm' where agent = 'w121'")
        self.assertIn("is on vm", watch.send(self.here, self.db, "w121",
                                             mail_send=lambda tx, to, body: self.fail("sent")))
        # a failed send leaves last_sent_ts untouched (rolled back)
        other.execute("update agent_watches set host = 'laptop', last_sent_at = null"
                      " where agent = 'w121'")
        with self.assertRaises(watch.WatchError):
            watch.send(self.here, self.db, "w121", mail_send=self.failing)
        self.assertIsNone(storage.of(self.db).watches.list("w121")[0]["last_sent_ts"])

    def test_a_failed_firing_drops_no_message(self):
        # codex r2 (#303): the message is dropped in the watch's transaction;
        # if the end of the firing fails, it goes with it (no second copy at
        # the next firing)
        from ameesh import mail
        self.enable()

        def drop_then_fail(tx, to, body):
            mail.send(tx, "veille", to, body, host="laptop", thread=False)
            raise RuntimeError("update failed")
        with self.assertRaises(watch.WatchError):
            watch.send(self.here, self.db, "w121", mail_send=drop_then_fail)
        self.assertEqual([m for m in storage.of(self.db).mailbox.unread("w121", 200)
                          if m["sender"] == "veille"], [])
        self.assertIsNone(storage.of(self.db).watches.list("w121")[0]["last_sent_ts"])
        # the default sending too: the transaction fails after the message is
        # dropped (the update or the commit), and the message goes with it
        from ameesh.storage.postgres import watches as pg_watches

        def deliver_then_fail(self_, agent, host, send):
            with self_.db.transaction() as tx:
                send(tx)
                raise RuntimeError("commit failed")
        with mock.patch.object(pg_watches.Watches, "deliver", deliver_then_fail), \
                self.assertRaises(watch.WatchError):
            watch.send(self.here, self.db, "w121")
        self.assertEqual([m for m in storage.of(self.db).mailbox.unread("w121", 200)
                          if m["sender"] == "veille"], [])

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
