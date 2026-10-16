# SPDX-License-Identifier: AGPL-3.0-only
"""#1901 (N3 de la relecture Claude de #1463) : une famille non lue ne résout rien.

La porte d'hôte (`exploitation._gate_alerts`) rend `[]` quand son registre est
présent mais illisible. Cette absence n'est pas une résolution : l'acquittement
d'une alerte de la famille reste valide, et la passe de notification n'annonce
pas une résolution qu'elle n'a pas observée (« absence ≠ résolution », #1473).
"""
from __future__ import annotations

import dataclasses
import json
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import alert_ack, exploitation, notify, storage
from ameesh.mediated_executor import availability

from .support import PgTestCase


def registre(body=None, schema=availability.REGISTRY_SCHEMA):
    return json.dumps({"schema": schema, "hosts": body if body is not None else {}})


class RegistreLocalTest(unittest.TestCase):
    """La lecture du registre dit ce qu'elle n'a pas pu lire."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ameesh-1901-")
        self.cfg = SimpleNamespace(state_dir=self.dir, host="h1")
        self.path = availability.default_path(self.cfg)

    def ecrire(self, contenu):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(contenu)

    def test_registre_absent_n_est_pas_une_lecture_incomplete(self):
        out = []
        self.assertEqual(availability.local_alerts(self.cfg, [], 1000.0, unreadable=out), [])
        self.assertEqual(out, [])

    def test_registre_illisible_nomme_la_famille(self):
        self.ecrire("{ tronqué")
        out = []
        self.assertEqual(availability.local_alerts(self.cfg, [], 1000.0, unreadable=out), [])
        self.assertEqual(out, [availability.GATE_FAMILY])

    def test_schema_inconnu_nomme_la_famille(self):
        self.ecrire(registre(schema="autre-chose"))
        out = []
        self.assertEqual(availability.local_alerts(self.cfg, [], 1000.0, unreadable=out), [])
        self.assertEqual(out, [availability.GATE_FAMILY])

    def test_registre_lisible_n_est_pas_signale(self):
        self.ecrire(registre({"h1": {
            "body": {"state": "stopped", "reason": "unknown", "seq": 1,
                     "available": False, "until_ts": None, "caps": {}},
            "since_ts": 10.0}}))
        out = []
        alerts = availability.local_alerts(self.cfg, [], 1000.0, unreadable=out)
        self.assertEqual([a["type"] for a in alerts], ["host_unavailable"])
        self.assertEqual(out, [])

    def test_gate_alerts_avale_l_erreur_et_nomme_la_famille(self):
        out = []
        with mock.patch.object(availability, "local_alerts", side_effect=OSError("registre")):
            self.assertEqual(exploitation._gate_alerts(self.cfg, [], 1000.0, unreadable=out), [])
        self.assertEqual(out, [availability.GATE_FAMILY])
        # sans liste de sortie, la famille reste silencieuse comme avant
        with mock.patch.object(availability, "local_alerts", side_effect=OSError("registre")):
            self.assertEqual(exploitation._gate_alerts(self.cfg, [], 1000.0), [])


class AcquittementTest(PgTestCase):
    """`reconcile` ne conclut pas « resolved » sur un relevé incomplet."""

    def setUp(self):
        super().setUp()
        self.db.execute("truncate alert_acknowledgements restart identity")
        self.domain = storage.of(self.db).alert_acknowledgements

    def ack(self, alert, observation_host="h1"):
        alert = dict(alert)
        self.domain.acknowledge(alert_ack.key_of(alert), alert_ack.severity(alert),
                                "human:owner", "human:owner",
                                observation_host=observation_host)
        return alert

    def live(self):
        return [r["alert_key"] for r in self.domain.live()]

    def test_releve_incomplet_ne_resout_pas_la_porte(self):
        a = self.ack(dict(type="host_unavailable", host="h1", since=1000.0))
        rows = alert_ack.reconcile(self.db, [], observed_ts=time.time(),
                                   observation_host="h1",
                                   unreadable=(availability.GATE_FAMILY,))
        self.assertEqual(self.live(), [alert_ack.key_of(a)])
        self.assertEqual([r["alert_key"] for r in rows], [alert_ack.key_of(a)])

    def test_releve_complet_resout_la_porte(self):
        self.ack(dict(type="host_unavailable", host="h1", since=1000.0))
        alert_ack.reconcile(self.db, [], observed_ts=time.time(), observation_host="h1")
        self.assertEqual(self.live(), [])

    def test_une_autre_famille_reste_resolue(self):
        self.ack(dict(type="long_turn", agent="a", since=1000.0), observation_host=None)
        alert_ack.reconcile(self.db, [], observed_ts=time.time(), observation_host="h1",
                            unreadable=(availability.GATE_FAMILY,))
        self.assertEqual(self.live(), [])

    def test_un_autre_hote_ne_resout_pas_la_porte_locale(self):
        a = self.ack(dict(type="host_unavailable", host="h1", since=1000.0))
        alert_ack.reconcile(self.db, [], observed_ts=time.time(),
                            observation_host="un-autre-poste")
        self.assertEqual(self.live(), [alert_ack.key_of(a)])


class PasseIncompleteTest(PgTestCase):
    """Bout en bout : une porte illisible ne produit ni résolution ni courrier."""

    def setUp(self):
        super().setUp()
        self.db.execute("truncate alert_acknowledgements restart identity")
        self.cfg = dataclasses.replace(self.cfg, host="h1", notify={
            "default_human": "human:owner", "default": ["desktop"],
            "types": ["host_unavailable"]})
        self.sent = []
        self.now = 10000.0

    def notifier(self, state=None):
        return notify.Notifier(
            self.cfg, notify.parse_config(self.cfg.notify), state,
            sender=type("Sender", (), {"send": lambda _, c, m: self.sent.append(m)})(),
            clock=lambda: self.now, log=lambda _: None)

    def ecrire_registre(self, hosts):
        path = availability.default_path(self.cfg)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(registre(hosts))

    def indisponible(self):
        return {"h1": {
            "body": {"state": "stopped", "reason": "unknown", "seq": 1,
                     "available": False, "until_ts": None, "caps": {}},
            "since_ts": self.now - 3600.0}}

    def entry(self, n):
        return list(n.state["active"].values())

    def test_porte_illisible_ne_resout_pas_et_ne_renotifie_pas(self):
        self.ecrire_registre(self.indisponible())
        n = self.notifier()
        n.run_pass(self.db)
        alerte = self.entry(n)[0]["alert"]
        self.assertEqual(alerte["type"], "host_unavailable")
        self.assertEqual(len(self.sent), 1)
        key = alert_ack.key_of(alerte)
        storage.of(self.db).alert_acknowledgements.acknowledge(
            key, alert_ack.severity(alerte), "human:owner", "human:owner",
            observation_host=self.cfg.host)
        # le registre devient illisible : la famille n'est plus lue du tout
        with open(availability.default_path(self.cfg), "w", encoding="utf-8") as fh:
            fh.write("{ tronqué")
        self.now += 60.0
        n.run_pass(self.db)
        self.assertEqual([e["alert"]["type"] for e in self.entry(n)], ["host_unavailable"])
        self.assertEqual(len(self.sent), 1)          # aucune résolution annoncée
        rows = self.db.query("select invalidation_reason from alert_acknowledgements")
        self.assertEqual([r["invalidation_reason"] for r in rows], [None])
        # le registre se relit, sans l'hôte : la résolution a bien lieu
        self.ecrire_registre({})
        self.now += 60.0
        n.run_pass(self.db)
        self.assertEqual(self.entry(n), [])
        rows = self.db.query("select invalidation_reason from alert_acknowledgements")
        self.assertEqual([r["invalidation_reason"] for r in rows], ["resolved"])
        self.assertEqual(len(self.sent), 1)          # acquittée : pas de courrier
