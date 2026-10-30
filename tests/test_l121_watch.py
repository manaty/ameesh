# SPDX-License-Identifier: AGPL-3.0-only
"""L121 (#120): the watch of an agent, managed by ameesh: a systemd user
timer on the host, the versioned instruction (`consignes/veille.md`), the
watch recorded in the database (0362) and shown in `ameesh projects`."""
from __future__ import annotations

import dataclasses
import json
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

from .support import EMPTY_CONFIG, PgTestCase


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
        # the working rules of the canon (home#71, §§3/6/11 bis/12): integration
        # by hourly packages, review before CI and no CI per lot between
        # packages, sober proofs — carried by the watch text, which travels
        # alone (the brief also carries the card's short projection)
        for words in ("packages", "30 min at most", "Review before CI",
                      "frozen head", "cancelled", "Sober proofs",
                      "a proof is a link", "sha256", "merge-tree", "3 lines"):
            self.assertIn(words, text)
        # #1676/#1658/#2332 : le paquet est horaire (canon §11 bis, owner 15/10
        # 19:50) ; la coupure 11:00/23:00 ne compose plus de paquet
        self.assertIn("a new package waits for the previous one", text)
        self.assertNotIn("about ten lots", text)
        self.assertNotIn("next cut", text)
        # #3342 : l'identité d'émission `veille` n'a pas de lecteur ; le texte
        # dit où déposer le compte rendu, au lieu de renvoyer à `veille`.
        self.assertIn("Never reply to `veille`", text)
        self.assertIn("ameesh fil", text)
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
        cfg = config_mod.load(env=dict(timer_env, AMEESH_CONFIG=EMPTY_CONFIG))
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

    def test_nothing_fails_a_firing_after_its_message_is_committed(self):
        # codex #303 r3 (lot #360): the registry becomes unreachable once the
        # instruction is committed; the firing still succeeds (a failure would
        # make the timer retry and send it twice)
        from ameesh import registry
        self.enable()
        real = registry.get

        def instructions() -> int:
            return len([m for m in storage.of(self.db).mailbox.unread("w121", 500)
                        if m["sender"] == "veille"])
        before = instructions()

        def down_after_delivery(db, name):
            if instructions() > before:
                raise db_mod.Unavailable("registry down")
            return real(db, name)
        with mock.patch.object(watch.registry, "get", side_effect=down_after_delivery), \
                mock.patch.object(watch.fil, "record"):
            self.assertEqual(watch.send(self.here, self.db, "w121"),
                             "instruction sent to w121")
        self.assertEqual(instructions(), before + 1)

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


class WakeTest(PgTestCase):
    """#1020: the one-shot wake carries the identity of its agent BY
    CONSTRUCTION, and the witness reads the installed unit back."""

    def setUp(self) -> None:
        self.register("w121", "claude")
        self.units = tempfile.mkdtemp(prefix="ameesh-1020-")
        self.addCleanup(shutil.rmtree, self.units, True)
        self.env = {watch.UNIT_DIR_ENV: self.units, "PATH": "/usr/bin:/bin",
                    "AMEESH_DSN": "postgres://u:secret@db/x", "AMEESH_HOST": "laptop"}
        self.here = dataclasses.replace(self.cfg, host="laptop")
        patcher = mock.patch.object(watch.platform, "is_linux", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.calls: list = []
        self.unit_env = 'PATH=/usr/bin:/bin AGENT_MAIL_NAME=w121'
        self.exec_start = ("{ path=/usr/bin/env ; "
                           "argv[]=/usr/bin/env AGENT_MAIL_NAME=w121 "
                           "/home/u/.local/bin/ameesh watch fire --agent w121 "
                           "--wake deadline ; ignore_errors=no ; status=0/0 }")
        self.timer_state = "active"

    def run_(self, argv, **kw):
        self.calls.append(argv[2:])
        if argv[2:4] == ["show", "-p"]:
            prop = argv[4]
            if prop == "Environment":
                return mock.Mock(returncode=0, stdout=self.unit_env, stderr="")
            if prop == "ExecStart":
                return mock.Mock(returncode=0, stdout=self.exec_start, stderr="")
            if prop == "ActiveState":
                return mock.Mock(returncode=0, stdout=self.timer_state + "\n", stderr="")
        return mock.Mock(returncode=0, stdout="", stderr="")

    def run_for(self, agent: str):
        """The systemd answers of the identity witness for a wake of `agent`
        (`run_` answers for w121, the default agent of these tests)."""
        def run_(argv, **kw):
            self.calls.append(argv[2:])
            if argv[2:4] == ["show", "-p"]:
                if argv[4] == "Environment":
                    return mock.Mock(returncode=0, stdout="PATH=/usr/bin:/bin %s=%s"
                                     % (watch.IDENTITY_ENV, agent), stderr="")
                if argv[4] == "ExecStart":
                    return mock.Mock(returncode=0, stdout="argv[]=/usr/bin/env %s=%s …"
                                     % (watch.IDENTITY_ENV, agent), stderr="")
                if argv[4] == "ActiveState":
                    return mock.Mock(returncode=0, stdout=self.timer_state + "\n", stderr="")
            return mock.Mock(returncode=0, stdout="", stderr="")
        return run_

    def arm(self, message="REVEIL #1020 : verifier le temoin", **kw):
        return watch.arm(self.here, self.db, "w121", "deadline", 1800, message,
                         run=self.run_, env=self.env, actor="human:owner", **kw)

    def register_more(self, name: str):
        """`PgTestCase.register` after `setUp`: the `self.env` dict handed to
        `watch.arm` shadows the `env()` method the CLI runner calls."""
        env = self.env
        del self.env
        try:
            return self.register(name, "claude")
        finally:
            self.env = env

    def test_the_unit_declares_and_applies_the_identity(self):
        found = watch.wake_units("w121", "deadline", 1800,
                                 "/home/u/.local/bin/ameesh", "/usr/bin:/bin", "/units")
        self.assertEqual(sorted(found),
                         ["ameesh-wake-4-w121-deadline.service",
                          "ameesh-wake-4-w121-deadline.timer"])
        service = found["ameesh-wake-4-w121-deadline.service"]
        # declared, and applied on the exec line: systemd's EnvironmentFile
        # overrides Environment=, so env(1) is what the child really carries
        self.assertIn('Environment="AGENT_MAIL_NAME=w121"', service)
        self.assertIn('ExecStart="%s" "AGENT_MAIL_NAME=w121" '
                      '"/home/u/.local/bin/ameesh" watch fire '
                      '--agent w121 --wake deadline' % watch._env_binary(), service)
        # the environment files come before the exec line that pins the identity
        self.assertLess(service.index("EnvironmentFile=-%h/.config/ameesh/env"),
                        service.index('ExecStart='))
        self.assertLess(service.index("EnvironmentFile=-/units/ameesh-wake-4-w121-deadline.env"),
                        service.index('ExecStart='))
        self.assertIn("Restart=on-failure", service)   # a failed firing retries
        timer = found["ameesh-wake-4-w121-deadline.timer"]
        self.assertIn("OnCalendar=", timer)
        self.assertNotIn("OnActiveSec=", timer)
        self.assertIn("AccuracySec=1s\n", timer)
        self.assertNotIn("OnUnitActiveSec", timer)     # one shot, not a period

    def test_parse_wake_delay(self):
        self.assertEqual(watch.parse_wake_delay("45m", None), 2700)
        self.assertEqual(watch.parse_wake_delay("2h", None), 7200)
        self.assertEqual(watch.parse_wake_delay(None, "2026-10-13T20:00:00Z",
                                                now=1791918000.0), 3600)
        for argv in [("45m", "2026-10-13T20:00:00Z"), (None, None),
                     ("5", None), (None, "2026-10-13T20:00:00")]:
            with self.assertRaises(watch.WatchError, msg=argv):
                watch.parse_wake_delay(argv[0], argv[1], now=1791918000.0)

    def test_arm_fire_and_the_identity_witness(self):
        text = self.arm()
        self.assertIn("AGENT_MAIL_NAME=w121 pinned", text)
        self.assertEqual(sorted(os.listdir(self.units)),
                         ["ameesh-wake-4-w121-deadline.env",
                          "ameesh-wake-4-w121-deadline.service",
                          "ameesh-wake-4-w121-deadline.timer",
                          "ameesh-wake-4-w121-deadline.wake.json"])
        state = os.path.join(self.units, "ameesh-wake-4-w121-deadline.wake.json")
        self.assertEqual(stat.S_IMODE(os.stat(state).st_mode), 0o600)
        with open(state, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["message"], "REVEIL #1020 : verifier le temoin")
        self.assertIn(["daemon-reload"], self.calls)
        self.assertIn(["start", "ameesh-wake-4-w121-deadline.timer"], self.calls)
        # the identity is read back from systemd, not trusted: declared, and
        # applied on the exec line (the one an environment file cannot take)
        self.assertIn(["show", "-p", "Environment", "--value",
                       "ameesh-wake-4-w121-deadline.service"], self.calls)
        self.assertIn(["show", "-p", "ExecStart", "--value",
                       "ameesh-wake-4-w121-deadline.service"], self.calls)
        # a firing without the identity is refused by the witness: this is the
        # failure that muted the seven watches
        with self.assertRaisesRegex(watch.WatchError, "AGENT_MAIL_NAME"):
            watch.fire(self.here, self.db, "w121", "deadline", run=self.run_,
                       env={watch.UNIT_DIR_ENV: self.units})
        # with it, the message is delivered to the agent, from the agent
        with mock.patch.object(watch.fil, "record") as thread:
            self.assertEqual(watch.fire(self.here, self.db, "w121", "deadline",
                                        run=self.run_,
                                        env={"AGENT_MAIL_NAME": "w121",
                                             watch.UNIT_DIR_ENV: self.units}),
                             "wake deadline fired: message sent to w121")
        [message] = [m for m in storage.of(self.db).mailbox.unread("w121", 200)
                     if m["sender"] == "w121"]
        self.assertEqual(message["body"], "REVEIL #1020 : verifier le temoin")
        self.assertEqual(thread.call_args.kwargs["meta"],
                         {"action": "wake", "wake": "deadline", "host": "laptop",
                          "audit": "watch"})
        self.assertEqual(thread.call_args.kwargs["mailbox_ids"], [int(message["id"])])
        # fired once: the wake is removed
        self.assertEqual(os.listdir(self.units), [])

    def test_a_wake_never_takes_the_files_of_a_watch(self):
        # lot #1039: `ameesh-watch-<agent>-<slug>` was exactly the name of the
        # periodic watch of an agent called `<agent>-<slug>`: arming the wake
        # stopped that watch's timer, overwrote its units and its pinned env,
        # and the firing removed them — the watch was lost while its
        # agent_watches row still showed it active.
        self.register_more("x")
        self.register_more("x-deadline")
        for name, text in watch.units("x-deadline", 1800, "/home/u/.local/bin/ameesh",
                                      "/usr/bin:/bin", self.units).items():
            with open(os.path.join(self.units, name), "w", encoding="utf-8") as fh:
                fh.write(text)
        with open(os.path.join(self.units, watch.env_name("x-deadline")), "w",
                  encoding="utf-8") as fh:
            fh.write('AMEESH_HOST="laptop"\n')

        def watch_files():
            return {name: open(os.path.join(self.units, name), encoding="utf-8").read()
                    for name in sorted(os.listdir(self.units))
                    if name.startswith(watch.UNIT_PREFIX)}
        before = watch_files()
        self.assertEqual(sorted(before), ["ameesh-watch-x-deadline.env",
                                          "ameesh-watch-x-deadline.service",
                                          "ameesh-watch-x-deadline.timer"])
        with mock.patch.object(watch.fil, "record"):
            watch.arm(self.here, self.db, "x", "deadline", 1800, "REVEIL x",
                      run=self.run_for("x"), env=self.env, actor="human:owner")
        self.assertEqual(watch_files(), before)          # the watch is untouched
        with mock.patch.object(watch.fil, "record"):
            watch.fire(self.here, self.db, "x", "deadline", run=self.run_for("x"),
                       env={"AGENT_MAIL_NAME": "x", watch.UNIT_DIR_ENV: self.units})
        # the firing takes its own wake away, never the watch of `x-deadline`
        self.assertEqual(watch_files(), before)

    def test_two_wakes_whose_flat_names_would_meet_stay_distinct(self):
        # lot #1039: (a, b-c) and (a-b, c) flatten to the same `a-b-c`; the
        # second wake would overwrite the units, the pinned env and the message
        # of the first (and one of the two messages would be lost). The length
        # of the agent keeps the two pairs apart.
        self.register_more("a")
        self.register_more("a-b")
        self.assertEqual(watch.wake_name("a", "b-c", "timer"), "ameesh-wake-1-a-b-c.timer")
        self.assertEqual(watch.wake_name("a-b", "c", "timer"), "ameesh-wake-3-a-b-c.timer")

        def names(agent, slug):
            return {watch.wake_name(agent, slug, suffix)
                    for suffix in ("service", "timer", "wake.json")} \
                | {watch.wake_env_name(agent, slug)}
        self.assertEqual(names("a", "b-c") & names("a-b", "c"), set())

        for agent, slug in (("a", "b-c"), ("a-b", "c")):
            with mock.patch.object(watch.fil, "record"):
                watch.arm(self.here, self.db, agent, slug, 1800, "REVEIL %s" % agent,
                          run=self.run_for(agent), env=self.env, actor="human:owner")
        self.assertEqual(sorted(name for name in os.listdir(self.units)
                                if name.endswith(".wake.json")),
                         ["ameesh-wake-1-a-b-c.wake.json",
                          "ameesh-wake-3-a-b-c.wake.json"])
        # each firing sends its own message and removes only its own wake
        for agent, slug in (("a", "b-c"), ("a-b", "c")):
            with mock.patch.object(watch.fil, "record"):
                watch.fire(self.here, self.db, agent, slug, run=self.run_for(agent),
                           env={"AGENT_MAIL_NAME": agent,
                                watch.UNIT_DIR_ENV: self.units})
        self.assertEqual(os.listdir(self.units), [])
        for agent in ("a", "a-b"):
            [message] = [m for m in storage.of(self.db).mailbox.unread(agent, 200)
                         if m["sender"] == agent]
            self.assertEqual(message["body"], "REVEIL %s" % agent)

    def test_arm_refuses_a_unit_that_does_not_carry_the_identity(self):
        self.unit_env = "PATH=/usr/bin:/bin"     # what a hand-made unit shows
        with self.assertRaisesRegex(watch.WatchError, "does not carry"):
            self.arm()
        self.assertEqual(os.listdir(self.units), [])            # nothing left armed
        self.assertIn(["stop", "ameesh-wake-4-w121-deadline.timer"], self.calls)

    def test_arm_refuses_a_unit_whose_exec_does_not_apply_the_identity(self):
        # the declaration alone is not enough: an EnvironmentFile overrides
        # Environment= (systemd), so a host file could take the identity away
        # from the child — the witness must read the pinned exec line.
        self.exec_start = ("{ path=/home/u/.local/bin/ameesh ; "
                           "argv[]=/home/u/.local/bin/ameesh watch fire --agent w121 "
                           "--wake deadline ; ignore_errors=no ; status=0/0 }")
        with self.assertRaisesRegex(watch.WatchError, "does not apply"):
            self.arm()
        self.assertEqual(os.listdir(self.units), [])
        self.assertIn(["stop", "ameesh-wake-4-w121-deadline.timer"], self.calls)

    def test_arm_refuses_a_timer_that_is_not_active(self):
        self.timer_state = "inactive"
        with self.assertRaisesRegex(watch.WatchError, "timer is 'inactive'"):
            self.arm()
        self.assertEqual(os.listdir(self.units), [])

    def write_legacy_wake(self, agent: str, slug: str) -> list[str]:
        """#4755: the four files of a wake armed before #1039, as they were
        written (`ameesh-watch-<agent>-<slug>.*`)."""
        names = {}
        for suffix, text in watch.wake_units(agent, slug, 1800, "/home/u/.local/bin/ameesh",
                                             "/usr/bin:/bin", self.units).items():
            names[suffix.rsplit(".", 1)[1]] = text
        names["env"] = 'AMEESH_HOST="laptop"\n'
        names["wake.json"] = json.dumps({"agent": agent, "slug": slug,
                                         "message": "REVEIL ancien"}) + "\n"
        for suffix, text in names.items():
            with open(os.path.join(self.units, "ameesh-watch-%s-%s.%s" % (agent, slug, suffix)),
                      "w", encoding="utf-8") as fh:
                fh.write(text)
        return sorted("ameesh-watch-%s-%s.%s" % (agent, slug, s) for s in names)

    def test_a_wake_armed_before_1039_disarms_itself_on_its_firing(self):
        # #4755: `watch fire` looked for the state under the new prefix only,
        # failed, and `Restart=on-failure` restarted the unit every 2 min
        # (8622 restarts of ameesh-watch-deepseek1-1369-m24h since 18/10).
        self.register_more("x")
        self.assertEqual(len(self.write_legacy_wake("x", "1369-m24h")), 4)
        with mock.patch.object(watch.fil, "record") as thread:
            text = watch.fire(self.here, self.db, "x", "1369-m24h", run=self.run_for("x"),
                              env={"AGENT_MAIL_NAME": "x", watch.UNIT_DIR_ENV: self.units})
        self.assertIn("armed before #1039", text)
        self.assertEqual(os.listdir(self.units), [])            # one firing, no restart
        self.assertFalse(thread.called)                          # the stale message is not sent
        self.assertIn(["stop", "ameesh-watch-x-1369-m24h.timer"], self.calls)
        # a state already gone (fired, cancelled): the firing succeeds too
        text = watch.fire(self.here, self.db, "x", "1369-m24h", run=self.run_for("x"),
                          env={"AGENT_MAIL_NAME": "x", watch.UNIT_DIR_ENV: self.units})
        self.assertIn("already fired or cancelled", text)

    def test_the_sweep_takes_legacy_wakes_never_a_watch(self):
        # the old name of the wake (x, deadline) is the periodic watch of
        # `x-deadline` (#1039): only a service that runs `watch fire` goes
        self.register_more("x")
        self.register_more("x-deadline")
        for name, text in watch.units("x-deadline", 1800, "/home/u/.local/bin/ameesh",
                                      "/usr/bin:/bin", self.units).items():
            with open(os.path.join(self.units, name), "w", encoding="utf-8") as fh:
                fh.write(text)
        watch_files = sorted(os.listdir(self.units))
        self.write_legacy_wake("x", "m6h")
        self.assertFalse(watch._disarm_legacy(self.units, "x", "deadline", self.run_for("x")))
        with mock.patch.object(watch.fil, "record"):
            watch.arm(self.here, self.db, "x", "next", 1800, "REVEIL x",
                      run=self.run_for("x"), env=self.env, actor="human:owner")
        left = sorted(os.listdir(self.units))
        self.assertEqual([n for n in left if n.startswith(watch.UNIT_PREFIX)], watch_files)
        self.assertNotIn("ameesh-watch-x-m6h.service", left)
        self.assertIn("ameesh-wake-1-x-next.wake.json", left)

    def test_cancel(self):
        self.arm()
        self.calls.clear()
        self.assertEqual(watch.disarm(self.here, self.db, "w121", "deadline",
                                      run=self.run_, env=self.env,
                                      actor="human:owner"),
                         "wake deadline of w121 cancelled by human:owner")
        self.assertEqual(os.listdir(self.units), [])
        with self.assertRaisesRegex(watch.WatchError, "no wake deadline of w121"):
            watch.disarm(self.here, self.db, "w121", "deadline", run=self.run_,
                         env=self.env, actor="human:owner")

    def test_refusals(self):
        with self.assertRaises(watch.WatchError):
            watch.arm(self.here, self.db, "nobody", "deadline", 1800, "m",
                      run=self.run_, env=self.env, actor="human:owner")
        for slug in ("", "Deadline", "a b", "-"):
            with self.assertRaises(watch.WatchError, msg=slug):
                watch.arm(self.here, self.db, "w121", slug, 1800, "m",
                          run=self.run_, env=self.env, actor="human:owner")
        with self.assertRaises(watch.WatchError):
            self.arm(message="   ")

    def test_who_arms_the_wake_of_another_agent(self):
        # the rule of `watch enable`: an agent arms its own wake
        from ameesh import assignments, identity
        def as_(name, orchestrator=False):
            return mock.patch.multiple(
                identity, resolve_binding=mock.Mock(return_value=SimpleNamespace(name=name))), \
                mock.patch.object(assignments, "is_orchestrator", return_value=orchestrator)
        who, role = as_("dev9")
        with who, role, self.assertRaisesRegex(watch.WatchError, "only its own watch"):
            watch.arm(self.here, self.db, "w121", "deadline", 1800, "m",
                      run=self.run_, env=self.env)
        self.assertEqual((os.listdir(self.units), self.calls), ([], []))
        who, role = as_("mesh-design", orchestrator=True)
        with who, role:
            watch.arm(self.here, self.db, "w121", "deadline", 1800, "m",
                      run=self.run_, env=self.env)
        self.assertEqual(len(os.listdir(self.units)), 4)


if __name__ == "__main__":
    unittest.main()
