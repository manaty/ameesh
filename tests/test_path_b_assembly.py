# SPDX-License-Identifier: AGPL-3.0-only
"""Voie B : branchements entre les lots L108 à L114 (sans base).

* flux d'événements : `agent_lease` sort avec `name` (contrat 1.1) ;
* L110 dans L109 : `device.token_source_for` (fichier, sinon l'identité de
  l'appareil enrôlé ; volume d'un autre serveur refusé) ;
* L114b : SIGTERM engage un retrait de 90 s que la porte ne peut rouvrir ;
  `HTTPS_PROXY` (tunnel CONNECT, `NO_PROXY`, boucle locale) ;
* `GateAck.from_json` ne lève que `ValueError`.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import types
import unittest
from unittest import mock

from ameesh.db import Unavailable
from ameesh.mediated_executor import device
from ameesh.mediated_executor import stream
from ameesh.mediated_executor import gate as P
from ameesh.mediated_executor import host_gate as H
from ameesh.storage import remote
from ameesh.storage.remote import http as remote_http


class FluxTest(unittest.TestCase):
    def test_agent_lease_sort_avec_name(self):
        hub = stream.EventHub(None, stream="ab12")
        event = hub.publish("agent_lease", {"agent": "inge-front", "owner": "o", "epoch": 4,
                                            "status": "running"})
        self.assertEqual(event.data, {"name": "inge-front", "owner": "o", "epoch": 4,
                                      "status": "running"})
        self.assertTrue(stream.visible(event, {"inge-front"}))
        self.assertFalse(stream.visible(event, {"autre"}))
        mail = hub.publish("agent_mail", {"to": "inge-front", "id": 3, "from": "coord"})
        self.assertEqual(mail.data["to"], "inge-front")


class JetonAppareilTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        patcher = mock.patch.dict(os.environ, {"AMEESH_EXEC_HOME": self.dir})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_fichier_d_abord(self):
        path = os.path.join(self.dir, "jeton")
        with open(path, "w") as fh:
            fh.write("amx1.x")
        cfg = types.SimpleNamespace(exec_token_file=path, exec_url="https://m.example")
        self.assertIsInstance(device.token_source_for(cfg), remote_http.FileTokenSource)

    def test_appareil_non_enrole(self):
        cfg = types.SimpleNamespace(exec_token_file="", exec_url="https://m.example")
        with self.assertRaises(Unavailable):
            device.token_source_for(cfg)

    def test_appareil_enrole_et_autre_serveur(self):
        device.load_or_create_key(self.dir)
        device.save_state({"executor_id": "7f3a9c2e4b1d6058", "mesh": "m", "host": "h",
                             "server_url": "https://m.example"}, self.dir)
        cfg = types.SimpleNamespace(exec_token_file="", exec_url="https://m.example/")
        source = device.token_source_for(cfg)
        self.assertIsInstance(source, device.HttpTokenSource)
        self.assertEqual(source.executor_id, "7f3a9c2e4b1d6058")
        cfg.exec_url = "https://autre.example"
        with self.assertRaises(Unavailable):
            device.token_source_for(cfg)

    def test_branchement_dans_le_client(self):
        self.addCleanup(remote.set_token_source_factory, None)
        remote.set_token_source_factory(None)
        device.install_token_source()
        self.assertFalse(remote.token_source_factory_is_default())
        # une fabrique posée par ailleurs (essais) n'est pas remplacée
        mine = lambda cfg: None  # noqa: E731
        remote.set_token_source_factory(mine)
        device.install_token_source()
        self.assertIs(remote._token_source_factory, mine)


class _Gate(P.HostGate):
    def __init__(self, state):
        self.st = state
        self.acks = []

    def state(self):
        return self.st

    def wait_change(self, timeout):
        return self.st

    def acknowledge(self, ack):
        self.acks.append(ack)


class SigtermTest(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.gate = _Gate(P.GateState("available", 5, reason="idle"))
        self.runner = types.SimpleNamespace(lock=threading.Lock(), workers={},
                                            wake_all=threading.Event())
        self.ctl = H.GateController(self.runner, self.gate, drain_s=90.0, sink=mock.Mock(),
                                    clock=lambda: self.now)
        self.ctl.apply(self.gate.st)

    def test_retrait_de_90_s_que_la_porte_ne_rouvre_pas(self):
        forced = self.ctl.terminate()
        self.assertEqual((forced.state, forced.reason), ("draining", "sigterm"))
        self.assertTrue(self.ctl.holds_turns())
        self.assertEqual(self.ctl.deadline, 1090.0)
        # la porte dit toujours available : le retrait tient
        self.ctl.apply(self.ctl._effective(self.gate.state()))
        self.assertTrue(self.ctl.holds_turns())
        # un stopped de la porte l'aggrave
        self.gate.st = P.GateState("stopped", 6, reason="user_active")
        self.ctl.apply(self.ctl._effective(self.gate.state()))
        self.assertEqual(self.ctl.current.state, "stopped")
        self.assertTrue(self.ctl.tick().drained)

    def test_runner_engage_le_retrait_hors_du_gestionnaire(self):
        from ameesh.runner import Runner
        runner = types.SimpleNamespace(mediated=True, once=False, host_gate=self.ctl,
                                       sigterm_requested=False, lock=threading.Lock(),
                                       workers={})
        self.assertTrue(Runner.can_drain_on_sigterm(runner))
        self.assertFalse(Runner._sigterm_drained(runner))
        runner.sigterm_requested = True
        worker = mock.Mock(is_alive=lambda: True)
        runner.workers["a"] = worker
        self.assertFalse(Runner._sigterm_drained(runner))   # un bail encore détenu
        self.assertTrue(self.ctl.terminating)
        worker.is_alive = lambda: False
        self.assertTrue(Runner._sigterm_drained(runner))
        runner.mediated = False
        self.assertFalse(Runner.can_drain_on_sigterm(runner))


class ProxyTest(unittest.TestCase):
    def test_sans_mandataire(self):
        self.assertIsNone(remote_http.https_proxy_for("m.example", {}))

    def test_mandataire_et_exceptions(self):
        env = {"HTTPS_PROXY": "http://alice:s%40cret@proxy.lan:8080",
               "NO_PROXY": "interne.example,.local"}
        host, port, headers = remote_http.https_proxy_for("m.example", env)
        self.assertEqual((host, port), ("proxy.lan", 8080))
        self.assertEqual(headers["Proxy-Authorization"], "Basic YWxpY2U6c0BjcmV0")
        self.assertIsNone(remote_http.https_proxy_for("interne.example", env))
        self.assertIsNone(remote_http.https_proxy_for("api.interne.example", env))
        self.assertIsNone(remote_http.https_proxy_for("x.local", env))
        self.assertIsNone(remote_http.https_proxy_for("127.0.0.1", env))
        self.assertIsNone(remote_http.https_proxy_for("m.example", dict(env, NO_PROXY="*")))
        with self.assertRaises(ValueError):
            remote_http.https_proxy_for("m.example", {"HTTPS_PROXY": "socks5://p:1080"})

    def test_tunnel_connect(self):
        transport = remote_http.HttpTransport("https://m.example:8443")
        with mock.patch.dict(os.environ, {"HTTPS_PROXY": "proxy.lan:3128"}, clear=False):
            os.environ.pop("NO_PROXY", None)
            os.environ.pop("no_proxy", None)
            conn = transport._connection(5.0)
        self.assertEqual((conn.host, conn.port), ("proxy.lan", 3128))
        self.assertEqual((conn._tunnel_host, conn._tunnel_port), ("m.example", 8443))


class GateAckTest(unittest.TestCase):
    def test_corps_faux_valueerror(self):
        good = {"schema": P.SCHEMA_HOST_ACK, "seq": 3, "state": "draining",
                "in_turn": ["a"], "held": ["a"], "drained": False, "ts": 1.5}
        self.assertEqual(P.GateAck.from_json(good).seq, 3)
        for bad in (None, [], {"schema": "x"}, dict(good, seq="3"), dict(good, seq=True),
                    dict(good, state="ouvert"), dict(good, in_turn="a"),
                    dict(good, held=[1]), dict(good, ts="x"), json.loads("{}")):
            with self.assertRaises(ValueError):
                P.GateAck.from_json(bad)

    def test_illisible_vaut_stopped(self):
        self.assertEqual(P.GateState.unreadable().state, "stopped")
        self.assertEqual(P.GateState("available", 1).max_concurrent, 1)


if __name__ == "__main__":
    unittest.main()
