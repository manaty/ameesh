# SPDX-License-Identifier: AGPL-3.0-only
"""#4466 : la veille « créneau de paquet manqué » respecte la pause du paquet.

Alerte #161222 (29/10 03:25Z) : pendant une pause déclarée (`packet.pause`,
#4361, essai 5 de staging), `packet_watch` a crié au créneau manqué et désigné
un composeur. Témoins : pause active (ou échue depuis moins que la grâce) =
`paused`, ni courrier ni créneau consommé ; pause échue ou levée = l'alerte
part comme avant ; un journal illisible ne tait rien.
"""
from __future__ import annotations

import tempfile
import unittest
from unittest import mock

from ameesh import packet_pause, packet_watch
from tests.test_2601_packet_watch import Hourly, at, cfg_with, payload


class FakeEvents:
    def __init__(self):
        self.rows: list[dict] = []

    def record(self, type, object, project, data):
        self.rows.append({"id": len(self.rows) + 1, "type": type, "object": object,
                          "project": project, "data": dict(data)})
        return len(self.rows)

    def recent_type(self, type, days, limit, object=None):
        rows = [row for row in reversed(self.rows)
                if row["type"] == type and (object is None or row["object"] == object)]
        return rows[:limit]


class WatchPause(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cfg = cfg_with(tmp.name)
        self.cfg.packet_watch["projects"]["ameesh"]["target"] = "main"
        self.watch = packet_watch.projects(self.cfg)["ameesh"]
        self.events = FakeEvents()
        self.db = object()
        for patch in (mock.patch.object(packet_watch, "carriers",
                                        return_value={"open": [], "recent": [], "served": []}),
                      mock.patch.object(packet_pause, "_events", lambda db: self.events)):
            patch.start()
            self.addCleanup(patch.stop)
        self.sent = []

    def run_check(self, now, state):
        return packet_watch.check(
            self.cfg, self.db, self.watch, Hourly(), now=now, state=state,
            inventory=lambda: payload(), send=lambda *a: self.sent.append(a))

    def pause(self, until, project="ameesh", target="main"):
        packet_pause.pause(self.db, project=project, target=target, until=until,
                           reason="essai 5 staging", by="bureau", now=at(3, 0))

    def test_active_pause_neither_alerts_nor_consumes_the_slot(self):
        self.pause(at(4, 30))
        state = {}
        result = self.run_check(at(4, 20), state)
        self.assertEqual(result["result"], "paused")
        self.assertEqual(result["pause"]["reason"], "essai 5 staging")
        self.assertEqual(result["pause"]["until"], int(at(4, 30)))
        self.assertEqual(self.sent, [])
        self.assertNotIn("last_alert_slot", state.get("projects", {}).get("ameesh", {}))
        # échue depuis moins que la grâce : le tic n'a pas encore repris
        self.assertEqual(self.run_check(at(4, 40), state)["result"], "paused")
        self.assertEqual(self.sent, [])
        # la grâce passée, le créneau non servi est signalé
        self.assertEqual(self.run_check(at(4, 50), state)["result"], "alerted")

    def test_expired_or_lifted_pause_alerts_again(self):
        self.pause(at(6, 0))
        self.assertEqual(self.run_check(at(6, 20), {})["result"], "alerted")
        self.assertEqual([to for to, _t, _d in self.sent], ["composeur", "pilote"])
        self.sent.clear()
        self.pause(at(9, 0))
        self.assertEqual(self.run_check(at(4, 20), {})["result"], "paused")
        packet_pause.resume(self.db, project="ameesh", target="main", by="bureau")
        self.assertEqual(self.run_check(at(4, 20), {})["result"], "alerted")

    def test_pause_of_another_target_or_project_does_not_silence(self):
        self.pause(at(6, 0), target="develop")
        self.pause(at(6, 0), project="nexlink")
        self.assertEqual(self.run_check(at(4, 20), {})["result"], "alerted")

    def test_unreadable_log_silences_nothing(self):
        with mock.patch.object(packet_pause, "latest", side_effect=RuntimeError("down")):
            result = self.run_check(at(4, 20), {})
        self.assertEqual(result["result"], "alerted")
        self.assertIn("down", result["pause_error"])


if __name__ == "__main__":
    unittest.main()
