# SPDX-License-Identifier: AGPL-3.0-only
"""#2341 : un hôte muet n'était pas signalé.

`hosts.current` rend le dernier relevé, pas un relevé frais : manaty-mesh-1
(sampled_ts du 12/10, 5,3 j) n'élevait aucune alerte et ses 14
`orphan_resource` restaient ouverts à jamais, faute d'exécuteur sur l'hôte
pour les solder (#339). Témoins : `host_unsampled` sur l'âge de `sampled_ts`
pour un hôte dont la fiche admet des agents ; un hôte à `max_agents: 0` est un
retrait volontaire, son relevé n'est plus courant et ses ressources de tour ne
sont plus alertées. Un relevé frais garde les alertes existantes.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import exploitation, notify, resources, storage

HOST = "manaty-mesh-1"
OTHER = "ameesh-agents-1"
NOW = 10_000_000.0
DAY = 86400.0


class HostUnsampledTest(unittest.TestCase):
    def alerts(self, readings: dict, *, max_agents, orphans=(), cfg=None) -> list[dict]:
        """`_host_alerts` sur des relevés `{hôte: champs}` et des fiches dont
        `max_agents` vaut la valeur donnée (un dict par hôte, ou un entier)."""
        store = mock.MagicMock()
        store.hosts.current.return_value = [
            dict({"host": host}, **reading) for host, reading in readings.items()]
        store.hosts.disk_history.return_value = []
        store.hosts.runners.return_value = []
        store.turn_resources.orphans.return_value = list(orphans)
        store.turn_resources.stale_running.return_value = []

        def physical(_canons, host):
            value = max_agents.get(host) if isinstance(max_agents, dict) else max_agents
            return {"limits": resources.thresholds(None), "max_agents": value,
                    "origin": {}, "fiches": []}

        with mock.patch.object(storage, "of", return_value=store), \
                mock.patch("ameesh.canon.load_configured", return_value=[]), \
                mock.patch.object(resources, "host_limits", side_effect=physical), \
                mock.patch.object(exploitation, "_housekeeping_alerts", return_value=[]):
            return exploitation._host_alerts(cfg, None, now=NOW)

    @staticmethod
    def orphan(turn="tour-1", host=HOST) -> dict:
        return {"turn_id": turn, "agent": "deepseek1", "host": host,
                "started_ts": NOW - 2 * DAY, "status": "orphan", "pgid": 42}

    def test_a_stale_reading_of_a_host_with_agents_alerts(self):
        found = self.alerts({HOST: {"sampled_ts": NOW - 2 * DAY}}, max_agents=3,
                            orphans=[self.orphan()])
        # le relevé ancien ne dit plus rien de la pression, mais l'alerte de
        # silence paraît ; les ressources de tour restent signalées tant que
        # l'hôte n'est pas retiré (l'exécuteur les soldera à son retour)
        self.assertEqual([a["type"] for a in found],
                         ["host_unsampled", "orphan_resource"])
        alert = found[0]
        self.assertEqual(
            (alert["host"], alert["max_agents"], alert["value"], alert["threshold"]),
            (HOST, 3, int(2 * DAY), 600))
        self.assertIn("host %s: no reading for 48.0 h" % HOST, alert["detail"])
        self.assertIn("max_agents 3", alert["detail"])
        self.assertIn("max_agents: 0", alert["detail"])

    def test_a_decommissioned_stale_host_is_silent(self):
        # la fiche le retire (max_agents 0) : ni alerte de silence, ni
        # orphan_resource qui ne pourra jamais être soldée
        self.assertEqual(self.alerts({HOST: {"sampled_ts": NOW - 2 * DAY}},
                                     max_agents=0, orphans=[self.orphan()]), [])

    def test_a_stale_reading_no_longer_raises_its_pressure(self):
        found = self.alerts(
            {HOST: {"sampled_ts": NOW - 2 * DAY, "mem_available_bytes": 0,
                    "load1": 99.0}},
            max_agents=3)
        self.assertEqual([a["type"] for a in found], ["host_unsampled"])

    def test_a_fresh_reading_keeps_the_existing_alerts(self):
        found = self.alerts(
            {HOST: {"sampled_ts": NOW - 30, "mem_available_bytes": 0, "load1": 99.0}},
            max_agents=3, orphans=[self.orphan()])
        types = [a["type"] for a in found]
        self.assertNotIn("host_unsampled", types)
        self.assertIn("host_pressure", types)
        self.assertIn("orphan_resource", types)

    def test_only_the_retired_hosts_orphans_are_closed(self):
        rows = {HOST: {"sampled_ts": NOW - 2 * DAY},
                OTHER: {"sampled_ts": NOW - 30}}
        found = self.alerts(rows, max_agents={HOST: 0, OTHER: 3},
                            orphans=[self.orphan("tour-muet", HOST),
                                     self.orphan("tour-vivant", OTHER)])
        self.assertEqual([a["turn"] for a in found if a["type"] == "orphan_resource"],
                         ["tour-vivant"])

    def test_a_retired_hosts_orphans_are_closed_even_on_a_fresh_reading(self):
        # un hôte retiré n'a pas d'exécuteur pour solder : le relevé frais
        # d'un échantillonneur restant ne change rien
        found = self.alerts({HOST: {"sampled_ts": NOW - 30}}, max_agents=0,
                            orphans=[self.orphan()])
        self.assertEqual(found, [])

    def test_a_retired_host_without_a_reading_is_still_recognized(self):
        # au-delà de la rétention de 7 jours le relevé est purgé : la fiche
        # reste la source du retrait, sinon les orphelins reviendraient
        found = self.alerts({}, max_agents={HOST: 0}, orphans=[self.orphan()])
        self.assertEqual(found, [])

    def test_a_host_with_no_reading_and_no_card_keeps_its_orphans(self):
        # sans fiche, on n'invente pas un retrait
        found = self.alerts({}, max_agents={HOST: None}, orphans=[self.orphan()])
        self.assertEqual([a["type"] for a in found], ["orphan_resource"])

    def test_the_threshold_follows_the_sampling_period(self):
        cfg = SimpleNamespace(resource_interval=300.0)
        self.assertEqual(self.alerts({HOST: {"sampled_ts": NOW - 1200}},
                                     max_agents=3, cfg=cfg), [])
        [alert] = self.alerts({HOST: {"sampled_ts": NOW - 3001}},
                              max_agents=3, cfg=cfg)
        self.assertEqual((alert["type"], alert["threshold"]), ("host_unsampled", 3000))

    def test_a_reading_without_a_timestamp_cannot_be_judged(self):
        # on n'invente pas un âge : pas d'alerte ; les autres alertes restent
        found = self.alerts({HOST: {}}, max_agents=3, orphans=[self.orphan()])
        self.assertEqual([a["type"] for a in found], ["orphan_resource"])

    def test_declared_and_pushed_by_default(self):
        self.assertIn("host_unsampled", exploitation.ALERT_TYPES)
        self.assertIn("host_unsampled", notify.TYPE_LABELS)
        self.assertIn("host_unsampled", notify.DEFAULT_TYPES)


if __name__ == "__main__":
    unittest.main()
