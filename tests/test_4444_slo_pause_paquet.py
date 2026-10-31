# SPDX-License-Identifier: AGPL-3.0-only
"""#4444 : une pause déclarée du paquet horaire n'est pas un retard GO → fusion.

Témoins : un lot OK hors paquet dont le GO tombe pendant une pause
`packet.pause` (#4361) n'escalade pas tant qu'elle court ; levée, l'horloge
repart du premier créneau après elle (ici la levée, sans cadence connue) et la
ligne nomme la pause ; une pause d'un autre projet, ou close avant le GO, ne
change rien ; `windows` coupe une pause à l'événement suivant de sa cible.
"""
from __future__ import annotations

import time
import unittest

from ameesh import packet_pause, storage

from .test_789_slo_alerts import _Base


class _Events:
    def __init__(self, rows):
        self.rows = rows

    def recent_type(self, type, days, limit, object=None):
        return [row for row in reversed(self.rows) if row["type"] == type][:limit]

    def context_type(self, type, days, limit, *, object_prefix):
        return [row for row in reversed(self.rows)
                if row["type"] == type and row["object"].startswith(object_prefix)][:limit]


class _Store:
    def __init__(self, rows):
        self.events = _Events(rows)


def _row(i, ts, data, target="main", project="ameesh"):
    return {"id": i, "type": packet_pause.EVENT, "ts": ts, "project": project,
            "object": packet_pause.key(project, target), "data": data}


class WindowsTest(unittest.TestCase):
    def windows(self, rows, project="ameesh"):
        from unittest import mock
        with mock.patch.object(packet_pause.storage, "of", lambda db: _Store(rows)):
            return packet_pause.windows(object(), project=project)

    def test_levee_coupe_la_fenetre(self):
        got = self.windows([
            _row(1, 1000, {"state": "paused", "until": 5000, "reason": "staging", "by": "b"}),
            _row(2, 3000, {"state": "resumed", "by": "b"}),
        ])
        self.assertEqual([(w["start"], w["end"]) for w in got], [(1000, 3000)])
        self.assertEqual(got[0]["reason"], "staging")

    def test_autre_projet_ignore(self):
        got = self.windows([_row(1, 1000, {"state": "paused", "until": 5000},
                                 project="nexlink", target="develop")])
        self.assertEqual(got, [])

    def test_renouvellement_et_sans_date(self):
        got = self.windows([
            _row(1, 1000, {"state": "paused", "until": 5000}),
            _row(2, 2000, {"state": "paused", "until": 9000}),
            {"id": 3, "type": packet_pause.EVENT, "object": packet_pause.key("ameesh", "main"),
             "data": {"state": "resumed"}},
        ])
        self.assertEqual([(w["start"], w["end"]) for w in got], [(1000, 2000), (2000, 9000)])

    def test_suspended_start(self):
        hourly = lambda at: (int(at) // 3600 + 1) * 3600  # noqa: E731
        # relecture cx3 : pause commencée après le créneau, lot déjà en attente
        self.assertEqual(packet_pause.suspended_start(
            3600, [{"start": 4000, "end": 12000}], hourly, 10000)[0], 10000)
        # levée : 400 s acquis avant la pause, puis depuis le créneau 14400
        start, crossed = packet_pause.suspended_start(
            3600, [{"start": 4000, "end": 12000}], hourly, 15000)
        self.assertEqual(15000 - start, 400 + 600)
        self.assertEqual(crossed["end"], 12000)
        # créneau dans la pause, sans cadence : l'horloge repart de la fin
        start, _ = packet_pause.suspended_start(
            1200, [{"start": 1000, "end": 3000}], lambda at: None, 3500)
        self.assertEqual(start, 3000)
        # pause close avant le départ, ou à venir : rien ne change
        self.assertEqual(packet_pause.suspended_start(
            5000, [{"start": 1000, "end": 3000}, {"start": 9000, "end": 9500}],
            hourly, 8000), (5000.0, None))


class SloPauseTest(_Base):
    """Le journal est en ajout seul : la pause est datée de maintenant (T0) et
    le GO posé juste après, l'horloge de la passe avançant ensuite."""

    def go_after(self, *pauses, other=False, target="main", go_s=60):
        # le journal survit d'un test à l'autre : un projet propre à chacun
        app = "p4444-%s" % self._testMethodName[-12:]
        project = app + "-autre" if other else app
        self.db.execute("UPDATE work_items SET app = %s WHERE id = %s",
                        (app, self.lot["id"]))
        st = storage.of(self.db)
        t0 = time.time()
        for until, reason in pauses:
            st.events.record(packet_pause.EVENT, packet_pause.key(project, target), project,
                             {"state": "paused", "until": int(t0 + until),
                              "reason": reason, "by": "bureau", "target": target})
        self.qa_at(t0 + go_s)
        return t0

    def steps(self, at):
        self.now = at
        return [a["step"] for a in self.slo()]

    def test_pause_active_aucune_escalade(self):
        t0 = self.go_after((9000, "staging agents-1"))
        self.assertEqual(self.steps(t0 + 7300), [])

    def test_announcement_deadline_does_not_restart_slo_clock(self):
        t0 = self.go_after()
        app = self.db.query("SELECT app FROM work_items WHERE id = %s",
                            (self.lot["id"],))[0]["app"]
        storage.of(self.db).events.record(
            packet_pause.EVENT, packet_pause.key(app, "main"), app,
            {"state": "paused", "until": int(t0 + 3600),
             "await_announcement": True, "reason": "coordination", "target": "main"})
        self.assertEqual(self.steps(t0 + 7200), [])
        self.assertEqual(self.steps(t0 + 3 * 86400), [])

    def test_levee_horloge_repart_de_la_levee(self):
        t0 = self.go_after((600, "staging agents-1"))
        self.assertEqual(self.steps(t0 + 720), [])
        self.assertEqual(self.steps(t0 + 2500), ["verdict_merge"])
        alert = self.slo()[0]
        self.assertEqual(alert["packet_pause"]["reason"], "staging agents-1")
        self.assertIn("after the pause of main", alert["detail"])

    def test_pause_commencee_pendant_l_attente(self):
        # relecture cx3 : GO 10 min avant le début de la pause ; acquis 600 s
        t0 = self.go_after((3 * 3600, "staging agents-1"), go_s=-600)
        self.assertEqual(self.steps(t0 + 2 * 3600), [])
        self.assertEqual(self.steps(t0 + 3 * 3600 + 900), [])
        self.assertEqual(self.steps(t0 + 3 * 3600 + 1500), ["verdict_merge"])

    def test_sans_pause_le_retard_reste(self):
        t0 = self.go_after()
        self.assertEqual(self.steps(t0 + 7300), ["verdict_merge"])

    def test_pause_close_avant_le_go(self):
        t0 = self.go_after((30, "ancienne"))
        self.assertEqual(self.steps(t0 + 7300), ["verdict_merge"])

    def test_pause_d_un_autre_projet(self):
        t0 = self.go_after((9000, "autre"), other=True, target="develop")
        self.assertEqual(self.steps(t0 + 7300), ["verdict_merge"])


if __name__ == "__main__":
    unittest.main()
