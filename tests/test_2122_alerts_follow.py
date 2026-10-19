# SPDX-License-Identifier: AGPL-3.0-only
"""#2122 : `alerts --follow` ne résout pas une famille qu'il n'a pas lue.

Quand le registre de la porte d'hôte de ce poste devient illisible,
`exploitation.alerts` ne rend plus les alertes de cette famille (elles portent
`observation_host == cfg.host`, #1901/#2127). Le flux comparait deux relevés
et annonçait une « resolved » jamais observée, puis un « raised » au retour.
Le témoin est hermétique : ni base ni registre réels, `alerts` est simulé et
la famille est nommée par `availability.GATE_FAMILY`.
"""
from __future__ import annotations

import contextlib
import io
import json
import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import alert_ack, canon, exploitation, pgbouncer_local
from ameesh.mediated_executor import availability


class FollowTest(unittest.TestCase):
    def setUp(self):
        self.cfg = SimpleNamespace(host="h1")
        self.args = SimpleNamespace(json=True, follow=True, iterations=3,
                                    interval=0.05, operation=None, key=None,
                                    human=None)
        self.passes: list = []

    def alerts(self, cfg, db, *, unreadable=None, **seuils):
        alerts, families = self.passes.pop(0)
        if unreadable is not None:
            unreadable.extend(families)
        return [dict(a) for a in alerts]

    def run_follow(self):
        db = SimpleNamespace(close=lambda: None)
        out = io.StringIO()
        with mock.patch.object(exploitation.db_mod, "connect", return_value=db), \
                mock.patch.object(exploitation.db_mod, "require_schema",
                                  lambda *a, **k: None), \
                mock.patch.object(exploitation, "thresholds", lambda args: {}), \
                mock.patch.object(exploitation, "alerts", self.alerts), \
                mock.patch.object(alert_ack, "annotate",
                                  lambda cfg, db, alerts: alerts), \
                mock.patch.object(canon, "configured_cache", contextlib.nullcontext):
            with contextlib.redirect_stdout(out):
                rc = exploitation.cmd_alerts(self.cfg, self.args)
        self.assertEqual(rc, 0)
        return [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]

    @staticmethod
    def gate_alert(**extra):
        alert = {"type": "host_unavailable", "agent": None, "host": "h1",
                 "since": 1000.0, "reason": "unknown", "observation_host": "h1"}
        alert.update(extra)
        return alert

    def test_porte_non_lue_ne_resout_pas_et_reporte_l_alerte(self):
        self.passes = [
            ([self.gate_alert()], []),                      # lisible : levée
            ([], [availability.GATE_FAMILY]),               # famille non lue
            ([], []),                                       # lisible : absente
        ]
        events = self.run_follow()
        self.assertEqual([e["event"] for e in events], ["raised", "resolved"])
        self.assertEqual(events[0]["type"], "host_unavailable")
        # la résolution vient de la passe lisible, pas de la passe incomplète
        self.assertEqual(events[1]["type"], "host_unavailable")

    def test_une_autre_famille_est_resolue_pendant_la_porte_non_lue(self):
        self.passes = [
            ([{"type": "long_turn", "agent": "dev1", "since": 1000.0}], []),
            ([], [availability.GATE_FAMILY]),
            ([], []),
        ]
        events = self.run_follow()
        self.assertEqual([e["event"] for e in events], ["raised", "resolved"])
        self.assertEqual(events[1]["type"], "long_turn")

    def test_une_alerte_de_la_porte_d_un_autre_poste_est_resolue(self):
        self.passes = [
            ([self.gate_alert(observation_host="autre")], []),
            ([], [availability.GATE_FAMILY]),
            ([], []),
        ]
        events = self.run_follow()
        self.assertEqual([e["event"] for e in events], ["raised", "resolved"])
        self.assertEqual(events[1]["type"], "host_unavailable")

    def test_une_autre_famille_non_lue_ne_bloque_pas_la_porte(self):
        self.passes = [
            ([self.gate_alert()], []),
            ([], ["autre-famille"]),
            ([], []),
        ]
        events = self.run_follow()
        self.assertEqual([e["event"] for e in events], ["raised", "resolved"])
        self.assertEqual(events[1]["type"], "host_unavailable")

    def test_une_console_muette_ne_resout_pas_la_file(self):
        """#2582 : la console illisible ne dit rien de la file du pool."""
        pool = {"type": "pgbouncer_pool_waiting", "agent": None, "host": "h1",
                "since": None, "value": 3, "threshold": 1, "detail": "queue"}
        console = {"type": "pgbouncer_console_unreadable", "agent": None,
                   "host": "h1", "since": None,
                   "detail": "local PgBouncer console unreadable: no answer"}
        self.passes = [
            ([pool], []),                                         # console lisible
            ([console], [pgbouncer_local.FAMILY]),                # console muette
            ([], []),                                             # lisible, file vide
        ]
        events = self.run_follow()
        # la passe muette ne résout pas la file : elle n'est pas observée
        self.assertEqual([(e["event"], e["type"]) for e in events][:2],
                         [("raised", "pgbouncer_pool_waiting"),
                          ("raised", "pgbouncer_console_unreadable")])
        self.assertEqual([(e["event"], e["type"]) for e in events][2:],
                         [("resolved", "pgbouncer_console_unreadable"),
                          ("resolved", "pgbouncer_pool_waiting")])


class ResolutionUnobservableTest(unittest.TestCase):
    """Le prédicat, isolé : hôte local, famille gate, champ d'observation."""

    def setUp(self):
        self.cfg = SimpleNamespace(host="h1")
        self.alert = {"type": "host_unavailable", "observation_host": "h1"}

    def test_famille_non_lue_et_hote_local(self):
        self.assertTrue(exploitation._resolution_unobservable(
            self.alert, [availability.GATE_FAMILY], self.cfg))

    def test_autre_hote(self):
        self.assertFalse(exploitation._resolution_unobservable(
            dict(self.alert, observation_host="h2"),
            [availability.GATE_FAMILY], self.cfg))

    def test_sans_observation_locale(self):
        self.assertFalse(exploitation._resolution_unobservable(
            {"type": "long_turn"}, [availability.GATE_FAMILY], self.cfg))

    def test_famille_lue(self):
        self.assertFalse(exploitation._resolution_unobservable(
            self.alert, [], self.cfg))

    def test_hote_non_declare(self):
        self.assertFalse(exploitation._resolution_unobservable(
            {"type": "host_unavailable", "observation_host": None},
            [availability.GATE_FAMILY], SimpleNamespace(host=None)))

    def test_console_muette_ne_resout_pas_la_file(self):
        self.assertTrue(exploitation._resolution_unobservable(
            {"type": "pgbouncer_pool_waiting"}, [pgbouncer_local.FAMILY], self.cfg))

    def test_console_muette_ne_bloque_pas_les_autres_types(self):
        self.assertFalse(exploitation._resolution_unobservable(
            {"type": "long_turn"}, [pgbouncer_local.FAMILY], self.cfg))


if __name__ == "__main__":
    unittest.main()
