# SPDX-License-Identifier: AGPL-3.0-only
"""#8384 : la garde d'entrée du drainage (release-check) est rejouée par
`host drain`, et la prise automatique (L165) ne confie aucun lot d'équipe à un
agent d'un hôte à adaptateur déclaré ni à la sonde d'un retour fermé.

Incident R1 v3 du 27/11 : release-check vert à 22:49Z, prise automatique de
#8312 par la sonde d'AG3 à 22:57Z, drainage à 23:03Z. Les quatre témoins du lot
sont ici : (a) lot ouvert → drain refusé, lots nommés ; (b) release-check à 0 →
drain accepté ; (c) hôte à hook déclaré / sonde → aucune prise ; (d) autre
hôte → prise inchangée."""
from __future__ import annotations

import dataclasses
import unittest

from ameesh import host_lifecycle, host_release, storage, work

from .test_l165_prise_lots import _Lots


class DrainGuardTest(_Lots):
    def setUp(self):
        super().setUp()
        # Aucun adaptateur : la garde testée est celle des lots, pas la borne
        # de drainage court ni la relecture du hook.
        self.cfg = dataclasses.replace(self.cfg, lifecycle_hooks={})

    def drain(self, host, **kw):
        kw.setdefault("duration", 7200)
        kw.setdefault("actor", "human:test")
        return host_lifecycle.drain(self.cfg, self.db, host, **kw)

    def control(self, host):
        rows = self.db.query(
            "SELECT mode, generation FROM host_lifecycle WHERE host = %s", (host,))
        return rows[0] if rows else None

    def test_a_lot_ouvert_refuse_le_drain_et_nomme_les_lots(self):
        self.agent("worker", team="ameesh", host="vm")
        lot = self.lot("lot encore ouvert", app="ameesh", assignee="worker")

        with self.assertRaises(ValueError) as caught:
            self.drain("vm")

        message = str(caught.exception)
        self.assertIn("#%s" % lot["id"], message)
        self.assertIn("worker", message)
        self.assertIn("avant la vidange", message)
        self.assertIsNone(self.control("vm"))

    def test_b_release_check_a_zero_laisse_passer_le_drain(self):
        self.agent("worker", team="ameesh", host="vm-clean")

        self.drain("vm-clean")

        control = self.control("vm-clean")
        self.assertIsNotNone(control)
        self.assertEqual(control["mode"], "draining")

    def test_force_trace_le_motif_et_ouvre_la_fenetre(self):
        self.agent("worker", team="ameesh", host="vm-forced")
        self.lot("lot à déplacer", app="ameesh", assignee="worker")

        row = self.drain("vm-forced", force_reason="incident, lot déplacé à la main")

        self.assertEqual(row["mode"], "draining")
        events = self.db.query(
            "SELECT data FROM ameesh_events WHERE type = 'host.drain_forced'"
            " AND object = %s", ("host:vm-forced",))
        self.assertEqual(len(events), 1)
        data = events[0]["data"]
        if isinstance(data, str):
            import json
            data = json.loads(data)
        self.assertEqual(data["actor"], "human:test")
        self.assertEqual(data["reason"], "incident, lot déplacé à la main")
        with self.assertRaisesRegex(ValueError, "non-empty reason"):
            self.drain("vm-forced", force_reason="  ")


class AdapterTakeTest(_Lots):
    def setUp(self):
        super().setUp()
        # `hook déclaré` : l'inscription de l'adaptateur est lisible dans la
        # configuration par hôte (même lecture que `host_release.hook_for`).
        self.cfg = dataclasses.replace(
            self.cfg, lifecycle_hooks={"vm-adapter": "ameesh-host-release"})

    def take_agents(self):
        return [row["agent"] for row in self.take()]

    def test_c_hote_a_hook_declare_ne_prend_aucun_lot_equipe(self):
        self.agent("adapter1", team="nexlink", host="vm-adapter")
        self.agent("plain1", team="nexlink", host="vm-plain")
        self.lot("travail", app="nexlink")

        self.assertEqual(self.take_agents(), ["plain1"])

    def test_c_la_sonde_d_un_retour_ferme_ne_prend_aucun_lot(self):
        self.agent("probe1", team="nexlink", host="vm-probe")
        api = storage.of(self.db).host_lifecycle
        api.drain("vm-probe", actor="human:test", close_claims=True)
        self.db.execute(
            "UPDATE host_lifecycle SET mode='returning', claims_open=false,"
            " return_probe='probe1' WHERE host='vm-probe'")
        lot = self.lot("travail pendant le retour", app="nexlink")
        self.assertIn("probe1", host_release.window_probe_agents(self.db))
        self.assertIn("vm-probe", host_release.adapter_lot_hosts(
            self.cfg, self.db, storage.of(self.db).operations.listing(),
            canons=[]))
        self.assertEqual(self.take(), [])
        self.assertIsNone(work.get(self.db, lot["id"])["assignee"])

    def test_d_autre_hote_prend_comme_avant(self):
        self.agent("plain1", team="nexlink", host="vm-plain")
        lot = self.lot("travail", app="nexlink")

        taken = self.take()

        self.assertEqual([row["item"]["id"] for row in taken], [lot["id"]])
        self.assertEqual(taken[0]["agent"], "plain1")


if __name__ == "__main__":
    unittest.main()
