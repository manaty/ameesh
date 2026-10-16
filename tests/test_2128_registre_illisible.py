# SPDX-License-Identifier: AGPL-3.0-only
"""#2128/#2127 : un registre de disponibilité illisible ne s'écrase pas et se dit.

#2128 : `FileRegistry.put` relisait le fichier, `_load` rendait `{}` sur un
fichier tronqué ou de schéma inconnu, et la réécriture ne gardait que l'hôte
écrit — les lignes des autres hôtes disparaissaient en silence. `put` refuse
désormais (`RegistryUnreadable`) et le fichier reste intact.

#2127 : ce même registre illisible ne produisait aucune alerte — la famille de
la porte restait active sans résolution (#1901), mais rien ne disait qu'elle
n'était plus lue. `_gate_alerts` émet `host_gate_unreadable`, observée par
l'hôte, pour fermer la boucle.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import exploitation
from ameesh.mediated_executor import availability


def _corps(state="available", seq=1):
    return {"schema": availability.SCHEMA_AVAILABILITY, "available": state == "available",
            "state": state, "seq": seq, "until_ts": None, "caps": {}, "reason": "unknown"}


def _registre(hosts=None, schema=availability.REGISTRY_SCHEMA):
    return json.dumps({"schema": schema, "hosts": hosts if hosts is not None else {}})


class PutRefuseTest(unittest.TestCase):
    """#2128 : `put` ne réécrit jamais un registre illisible."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ameesh-2128-")
        self.path = os.path.join(self.dir, "exec", "availability.json")
        os.makedirs(os.path.dirname(self.path), exist_ok=True)

    def ecrire(self, contenu):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(contenu)

    def lire(self):
        with open(self.path, encoding="utf-8") as fh:
            return fh.read()

    def test_fichier_tronque_refuse_et_reste_intact(self):
        contenu = '{"schema": "%s", "hosts": {"h2": {"body"' % availability.REGISTRY_SCHEMA
        self.ecrire(contenu)
        with self.assertRaises(availability.RegistryUnreadable):
            availability.FileRegistry(self.path).put("h1", _corps())
        self.assertEqual(self.lire(), contenu)

    def test_schema_inconnu_refuse(self):
        self.ecrire(_registre(schema="autre-chose"))
        with self.assertRaises(availability.RegistryUnreadable):
            availability.FileRegistry(self.path).put("h1", _corps())

    def test_hotes_illisibles_refuse(self):
        self.ecrire(_registre(hosts=["h2"]))
        with self.assertRaises(availability.RegistryUnreadable):
            availability.FileRegistry(self.path).put("h1", _corps())

    def test_fichier_absent_cree_le_registre(self):
        registre = availability.FileRegistry(self.path)
        registre.put("h1", _corps())
        self.assertIsNone(registre.error)
        self.assertEqual([r["host"] for r in registre.rows()], ["h1"])

    def test_registre_lisible_garde_les_autres_hotes(self):
        self.ecrire(_registre({"h2": {"body": {"state": "stopped"}, "since_ts": 1.0}}))
        availability.FileRegistry(self.path).put("h1", _corps())
        rows = availability.FileRegistry(self.path).rows()
        self.assertEqual([r["host"] for r in rows], ["h1", "h2"])


class AlertePorteIllisibleTest(unittest.TestCase):
    """#2127 : la porte non lue se dit, observée par l'hôte."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ameesh-2127-")
        self.cfg = SimpleNamespace(state_dir=self.dir, host="h1")
        self.path = availability.default_path(self.cfg)

    def ecrire(self, contenu):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(contenu)

    def test_registre_tronque_emet_l_alerte_dediee(self):
        self.ecrire("{ tronqué")
        out = []
        alerts = exploitation._gate_alerts(self.cfg, [], 1000.0, unreadable=out)
        self.assertEqual([a["type"] for a in alerts], ["host_gate_unreadable"])
        self.assertEqual(alerts[0]["host"], "h1")
        self.assertEqual(alerts[0]["observation_host"], "h1")
        self.assertIn("illisible", alerts[0]["detail"])
        self.assertEqual(out, [availability.GATE_FAMILY])

    def test_schema_inconnu_emet_la_meme_alerte(self):
        self.ecrire(_registre(schema="autre-chose"))
        alerts = exploitation._gate_alerts(self.cfg, [], 1000.0)
        self.assertEqual([a["type"] for a in alerts], ["host_gate_unreadable"])

    def test_registre_absent_ne_dit_rien(self):
        self.assertEqual(exploitation._gate_alerts(self.cfg, [], 1000.0), [])

    def test_registre_lisible_ne_dit_rien_de_plus(self):
        self.ecrire(_registre({"h1": {"body": {"state": "stopped", "reason": "unknown",
                                               "seq": 1, "available": False,
                                               "until_ts": None, "caps": {}},
                               "since_ts": 10.0}}))
        alerts = exploitation._gate_alerts(self.cfg, [], 1000.0)
        self.assertEqual([a["type"] for a in alerts], ["host_unavailable"])

    def test_erreur_d_acces_emet_aussi_l_alerte(self):
        with mock.patch.object(availability, "local_alerts", side_effect=OSError("boom")):
            alerts = exploitation._gate_alerts(self.cfg, [], 1000.0)
        self.assertEqual([a["type"] for a in alerts], ["host_gate_unreadable"])
        self.assertIn("boom", alerts[0]["detail"])

    def test_type_declare(self):
        self.assertIn("host_gate_unreadable", exploitation.ALERT_TYPES)


if __name__ == "__main__":
    unittest.main()
