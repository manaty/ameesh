# SPDX-License-Identifier: AGPL-3.0-only
"""L93 : « mon poste » — schéma local, canal Unix, arrêt propre.

* `build` met en forme des lignes déjà lues (aucune base requise) ;
* la socket d'état est en lecture seule, 0600, un objet JSON par connexion ;
* la socket de contrôle n'admet que `stop` pour un agent de l'hôte ;
* `merge` réunit les exécuteurs d'une même machine ;
* un chemin non-socket n'est jamais remplacé.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import socket
import stat
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import device_state as D
from ameesh import host_cli
from ameesh.runner import Runner


HOST = "pc-test"


def cfg():
    return SimpleNamespace(host=HOST, canon="/tmp/canon", extra_canons=())


def row(name="agent-a", **extra):
    base = {
        "name": name, "host": HOST, "canon": None, "harness": "deepseek",
        "status": "running", "status_text": "turn: build",
        "session_work_item": "382", "assigned_lot_id": 382,
        "session_lot_title": "L93: my workstation", "session_lot_state": "build",
        "turn_started_ts": 1000.0, "turn_label": "messages",
        "lease_live": True, "lease_owner": "runner-%s@%s" % (name, HOST),
        "lease_expires_ts": 1300.0, "status_since_ts": 900.0,
        "last_turn_ts": 800.0, "projects": ["ameesh"], "team": "ameesh",
    }
    base.update(extra)
    return base


def build(**extra):
    options = {
        "agents": [row()],
        "runner_id": "runner-agent-a@" + HOST,
        "socket_path": "/tmp/device-a.sock",
        "control_socket": "/tmp/control-a.sock",
        "host_reading": {"sampled_ts": 1200.0, "cpu_count": 8, "load1": 1.5,
                         "mem_available_bytes": 1000, "mem_total_bytes": 2000,
                         "turns_in_progress": 1, "unmeasured": []},
        "runner_readings": [{"runner_id": "runner-agent-a@" + HOST, "cgroup": {
            "cpu_used": 0.5, "mem_current_bytes": 123, "mem_max_bytes": 456,
            "mem_used": 0.25, "throttled": None}}],
        "lots": {382: {"id": 382, "app": "ameesh", "title": "L93: my workstation",
                       "state": "build"}},
        "organisations": {None: "manaty"},
        "now": 1300.0,
    }
    options.update(extra)
    return D.build(cfg(), **options)


class BuildTest(unittest.TestCase):
    def test_schema_and_grouping(self):
        body = build()
        self.assertEqual(body["schema"], "ameesh-device/1")
        self.assertEqual(body["host"], HOST)
        self.assertEqual(body["generated_ts"], 1300.0)
        self.assertEqual(len(body["runners"]), 1)
        self.assertEqual(body["runners"][0]["control_socket"], "/tmp/control-a.sock")
        mesh = body["meshes"][0]
        self.assertEqual((mesh["mesh"], mesh["organisation"]), ("manaty", "manaty"))
        agent = mesh["agents"][0]
        self.assertEqual(agent["name"], "agent-a")
        self.assertEqual(agent["persona"], "agent-a")
        self.assertEqual(agent["project"], "ameesh")
        self.assertEqual(agent["lot"]["id"], 382)
        self.assertEqual(agent["lot"]["app"], "ameesh")
        self.assertTrue(agent["turn"]["active"])
        self.assertEqual(agent["turn"]["started_ts"], 1000.0)
        self.assertTrue(agent["lease"]["live"])
        self.assertEqual(agent["lease"]["expires_ts"], 1300.0)
        self.assertTrue(agent["sandbox"]["measured"])
        self.assertEqual(agent["sandbox"]["cpu"], 0.5)
        self.assertEqual(agent["sandbox"]["memory_bytes"], 123)
        self.assertIsNone(agent["sandbox"]["network"])
        self.assertIn("network", body["unmeasured"])

    def test_another_host_is_ignored(self):
        body = build(agents=[row("agent-a"), row("agent-b", host="other")])
        names = [a["name"] for a in body["meshes"][0]["agents"]]
        self.assertEqual(names, ["agent-a"])

    def test_an_expired_lease_is_not_an_active_turn(self):
        body = build(agents=[row(lease_live=False)])
        agent = body["meshes"][0]["agents"][0]
        self.assertFalse(agent["turn"]["active"])
        self.assertFalse(agent["lease"]["live"])

    def test_a_lot_without_app_falls_back_to_the_agent_project(self):
        body = build(lots={})
        agent = body["meshes"][0]["agents"][0]
        self.assertEqual(agent["project"], "ameesh")
        self.assertEqual(agent["lot"]["state"], "build")

    def test_gate_and_absent_compute_lease(self):
        body = build(gate={"state": "draining", "seq": 4, "reason": "user_active"})
        self.assertEqual(body["compute"]["gate"]["state"], "draining")
        self.assertIsNone(body["compute"]["lease"])

    def test_no_measure_is_said_not_invented(self):
        body = build(runner_readings=[{"runner_id": "runner-agent-a@" + HOST}])
        agent = body["meshes"][0]["agents"][0]
        self.assertFalse(agent["sandbox"]["measured"])
        self.assertIsNone(agent["sandbox"]["cpu"])
        self.assertIn("network", body["unmeasured"])


class StopTest(unittest.TestCase):
    def test_strict_stop_refuses_an_unknown_or_foreign_agent(self):
        db = object()
        store = SimpleNamespace(agents=SimpleNamespace(get=lambda _n: None))
        with mock.patch("ameesh.storage.of", return_value=store):
            with self.assertRaises(ValueError):
                D.stop_agent(cfg(), db, "agent-a", strict=True)
        store = SimpleNamespace(agents=SimpleNamespace(get=lambda _n: {"host": "other"}))
        with mock.patch("ameesh.storage.of", return_value=store):
            with self.assertRaises(ValueError):
                D.stop_agent(cfg(), db, "agent-a", strict=True)

    def test_strict_stop_uses_the_same_gesture_as_agent_runner_stop(self):
        db = object()
        store = SimpleNamespace(agents=SimpleNamespace(get=lambda _n: {"host": HOST}))
        with mock.patch("ameesh.storage.of", return_value=store), \
                mock.patch("ameesh.registry.set_status") as set_status, \
                mock.patch("ameesh.work.measurement_actor", return_value="auditeur"), \
                mock.patch("ameesh.work.release_lots", return_value=[{"id": 382}]) as release:
            returned = D.stop_agent(cfg(), db, "agent-a", strict=True)
        self.assertEqual(returned, [{"id": 382}])
        set_status.assert_called_once_with(
            db, "agent-a", "stopped", status_text="stopped by hand", stop_reason="manuel")
        self.assertEqual(release.call_args.kwargs["actor"], "stop")


class SocketTest(unittest.TestCase):
    def setUp(self):
        # les sockets Unix sont bornées à ~108 octets : le dossier d'exécution
        # de l'utilisateur (court), pas $TMPDIR qui peut être profond
        base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
        self.dir = tempfile.mkdtemp(prefix="ameesh-l93-", dir=base)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for name in os.listdir(self.dir):
            try:
                os.unlink(os.path.join(self.dir, name))
            except OSError:
                pass
        try:
            os.rmdir(self.dir)
        except OSError:
            pass

    def test_state_channel_is_read_only_and_private(self):
        path = os.path.join(self.dir, "device.sock")
        channel = D.StateChannel(path, build, cache_s=0)
        channel.start()
        self.addCleanup(channel.stop)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        body = D.query(path)
        self.assertEqual(body["schema"], "ameesh-device/1")
        # un ordre sur le canal d'état ne fait rien : il relit simplement l'état
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(sock.close)
        sock.connect(path)
        sock.sendall(b'{"op":"stop","agent":"agent-a"}\n')
        raw = sock.makefile("rb").readline()
        self.assertEqual(json.loads(raw)["schema"], D.SCHEMA)

    def test_control_channel_accepts_only_a_served_agent(self):
        path = os.path.join(self.dir, "control.sock")
        calls = []
        channel = D.ControlChannel(path, lambda agent: calls.append(agent) or [{"id": 382}],
                                   allowed=["agent-a"])
        channel.start()
        self.addCleanup(channel.stop)
        reply = D.stop(path, "agent-a")
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["returned"], [382])
        self.assertEqual(calls, ["agent-a"])
        refused = D.stop(path, "agent-b")
        self.assertFalse(refused["ok"])
        self.assertIn("not served", refused["error"])
        self.assertEqual(calls, ["agent-a"])

    def test_a_channel_is_removed_on_stop(self):
        path = os.path.join(self.dir, "device.sock")
        channel = D.StateChannel(path, build, cache_s=0).start()
        self.assertTrue(os.path.exists(path))
        channel.stop()
        self.assertFalse(os.path.exists(path))

    def test_a_regular_file_is_never_replaced(self):
        path = os.path.join(self.dir, "device.sock")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("keep me")
        with self.assertRaises(D.DeviceError):
            D.StateChannel(path, build)
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "keep me")

    def test_query_of_a_missing_channel_fails_cleanly(self):
        with self.assertRaises(D.DeviceError):
            D.query(os.path.join(self.dir, "absent.sock"))

    def test_cli_view_and_stop_read_the_channels(self):
        state_path = os.path.join(self.dir, "device.sock")
        control_path = os.path.join(self.dir, "control.sock")
        state = D.StateChannel(state_path, build, cache_s=0).start()
        control = D.ControlChannel(control_path, lambda _a: [{"id": 382}],
                                   allowed=["agent-a"]).start()
        self.addCleanup(state.stop)
        self.addCleanup(control.stop)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = host_cli.main(["device", "view", "--socket", state_path, "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["schema"], D.SCHEMA)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = host_cli.main(["device", "stop", "agent-a", "--socket", control_path, "--json"])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out.getvalue())["ok"])

    def test_merge_reunites_two_runners(self):
        one = build()
        two = build(agents=[row("agent-b")], runner_id="runner-agent-b@" + HOST,
                    socket_path="/tmp/device-b.sock",
                    control_socket="/tmp/control-b.sock",
                    runner_readings=[], now=1310.0)
        merged = D.merge([one, two])
        self.assertEqual(len(merged["runners"]), 2)
        names = [a["name"] for a in merged["meshes"][0]["agents"]]
        self.assertEqual(names, ["agent-a", "agent-b"])

    def test_merge_keeps_one_row_per_agent(self):
        merged = D.merge([build(), build(now=1320.0)])
        self.assertEqual(len(merged["meshes"][0]["agents"]), 1)

    def test_runner_starts_and_stops_its_two_channels(self):
        runner = SimpleNamespace(
            once=False, dry_run=False, mediated=False,
            runner_id="runner-agent-a@" + HOST,
            cfg=SimpleNamespace(host=HOST), db=object(), host_gate=None,
            agents_filter=["agent-a"], device_channels=[])
        state_path, control_path = D.paths(runner.runner_id, self.dir)
        with mock.patch.dict(os.environ, {"AMEESH_DEVICE_DIR": self.dir}), \
                mock.patch.object(D, "snapshot", return_value=build()), \
                mock.patch.object(D, "stop_agent", return_value=[{"id": 382}]):
            Runner.start_device_channels(runner)
            self.assertEqual(len(runner.device_channels), 2)
            self.assertEqual(D.query(state_path)["schema"], D.SCHEMA)
            reply = D.stop(control_path, "agent-a")
            self.assertTrue(reply["ok"])
            refused = D.stop(control_path, "agent-b")
            self.assertFalse(refused["ok"])
            for channel in runner.device_channels:
                channel.stop()
        runner.device_channels = []


if __name__ == "__main__":
    unittest.main()
