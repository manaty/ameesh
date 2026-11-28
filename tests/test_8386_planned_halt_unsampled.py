# SPDX-License-Identifier: AGPL-3.0-only
"""#8386 : `host_unsampled` levée sur un hôte arrêté par l'adaptateur VM.

À 00:52Z le 28/11, ameesh-agents-3 était drainé (23:03:50Z), son arrêt prouvé
(00:44:48Z), la VM `stopped` chez Scaleway (00:48:58Z), retour daté à
02:03:50Z : l'alerte de silence conseillait de redémarrer son exécuteur.
Témoins : un arrêt piloté avant son retour daté se tait ; passé le retour plus
la borne de démarrage, ou sur un état d'adaptateur illisible, l'alerte revient.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import exploitation, host_release, resources, storage

HOST = "ameesh-agents-3"
NOW = 10_000_000.0
RETURN = NOW + 4200.0
CFG = SimpleNamespace(resource_interval=60.0, host_release_boot_grace=900.0)


def control(**extra) -> dict:
    row = {"host": HOST, "mode": "draining", "release_state": "idle-drained",
           "hook_state": "idle-drained", "claims_open": False,
           "return_ts": RETURN, "return_requested_ts": None,
           "updated_ts": NOW - 600.0}
    row.update(extra)
    return row


class PlannedHaltTest(unittest.TestCase):
    def alerts(self, *, now=NOW, controls=(), provider="stopped",
               orphans=()) -> list[dict]:
        store = mock.MagicMock()
        store.hosts.current.return_value = [
            {"host": HOST, "sampled_ts": NOW - 600.0}]
        store.hosts.disk_history.return_value = []
        store.hosts.runners.return_value = []
        store.turn_resources.orphans.return_value = list(orphans)
        store.turn_resources.stale_running.return_value = []
        if isinstance(controls, Exception):
            store.host_lifecycle.listing.side_effect = controls
        else:
            store.host_lifecycle.listing.return_value = list(controls)

        def physical(_canons, host, *, reading=None):
            return {"limits": resources.thresholds(None), "max_agents": 3,
                    "origin": {}, "fiches": []}

        with mock.patch.object(storage, "of", return_value=store), \
                mock.patch("ameesh.canon.load_configured", return_value=[]), \
                mock.patch.object(resources, "host_limits", side_effect=physical), \
                mock.patch.object(host_release, "_adapter_provider_state",
                                  return_value=provider), \
                mock.patch.object(exploitation, "_housekeeping_alerts", return_value=[]):
            return exploitation._host_alerts(CFG, None, now=now + 120.0)

    def types(self, **kw) -> list[str]:
        return [a["type"] for a in self.alerts(**kw)]

    def test_a_host_halted_by_the_adapter_before_its_return_is_quiet(self):
        self.assertEqual(self.types(controls=[control()]), [])
        self.assertEqual(self.types(controls=[control()], provider="stopped in place"), [])

    def test_its_turn_resources_wait_for_its_return(self):
        orphan = {"turn_id": "t1", "agent": "a", "host": HOST,
                  "started_ts": NOW - 7200.0, "status": "orphan", "pgid": 1}
        self.assertEqual(self.types(controls=[control()], orphans=[orphan]), [])

    def test_the_alert_comes_back_past_the_return_and_the_boot_bound(self):
        self.assertEqual(self.types(now=RETURN + 900.0, controls=[control()]),
                         ["host_unsampled"])

    def test_a_return_under_way_is_quiet_within_the_boot_bound(self):
        back = control(mode="returning", return_ts=None, updated_ts=RETURN)
        self.assertEqual(self.types(now=RETURN + 300.0, controls=[back],
                                    provider="starting"), [])
        self.assertEqual(self.types(now=RETURN + 900.0, controls=[back],
                                    provider="starting"), ["host_unsampled"])

    def test_an_early_wake_is_quiet_within_the_boot_bound(self):
        # retour anticipé demandé avant la date : la VM démarre, en `draining`
        early = control(return_requested_ts=NOW)
        self.assertEqual(self.types(now=NOW + 300.0, controls=[early],
                                    provider="starting"), [])
        self.assertEqual(self.types(now=NOW + 900.0, controls=[early],
                                    provider="running"), ["host_unsampled"])

    def test_a_released_host_booting_after_busy_is_quiet_within_the_bound(self):
        # #4148 B1 : `busy` acquitté, release `busy`, claims encore fermés
        booting = control(release_state="busy", hook_state="busy",
                          released_ts=NOW - 3600.0, updated_ts=NOW)
        self.assertEqual(self.types(now=NOW + 300.0, controls=[booting],
                                    provider="running"), [])
        self.assertEqual(self.types(now=NOW + 900.0, controls=[booting],
                                    provider="running"), ["host_unsampled"])

    def test_an_unreadable_adapter_state_alerts(self):
        self.assertEqual(self.types(controls=[control()], provider=None),
                         ["host_unsampled"])
        self.assertEqual(self.types(controls=[control()], provider="running"),
                         ["host_unsampled"])

    def test_an_unreadable_lifecycle_alerts(self):
        self.assertEqual(self.types(controls=RuntimeError("db")), ["host_unsampled"])

    def test_no_proof_of_a_planned_halt_alerts(self):
        for row in (control(release_state="draining"), control(return_ts=None),
                    control(release_state="busy", hook_state="busy", claims_open=True,
                            released_ts=NOW - 3600.0),
                    control(mode="returning", claims_open=True, return_ts=None)):
            with self.subTest(row=row):
                self.assertEqual(self.types(controls=[row]), ["host_unsampled"])
        self.assertEqual(self.types(controls=[]), ["host_unsampled"])


if __name__ == "__main__":
    unittest.main()
