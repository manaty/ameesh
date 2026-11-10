# SPDX-License-Identifier: AGPL-3.0-only
"""#6642 (décision owner #207135 a, P3) : coût de coordination.

- Un SLO de niveau 1 ordinaire n'est briefé qu'après 5 min de retard, au seul
  débiteur ; les copies partent dès le niveau 2 ; urgent et P1 gardent leur
  envoi immédiat (contre-examen #207148). L'alerte, elle, existe dès le retard.
- `agent-mail send` sans `--lot` rattache le courrier au lot d'un « #NNNN » en
  tête du corps ; un numéro au milieu, ou d'un lot inconnu, ne rattache rien.
"""
from __future__ import annotations

import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import alert_briefs, mail, notify, storage, work
from .test_l118_assignments import _Base


def _row(name, *, team="ameesh"):
    return {"name": name, "team": team, "host": "h1", "lease_live": True,
            "status": "idle", "mode": "execute"}


def _slo(*, level=1, overdue=360, targets=("assignee",), **kw):
    alert = {"type": "slo_breach", "agent": "dev1", "lot": 42, "title": "un lot",
             "lot_state": "build", "value": overdue, "threshold": 1800,
             "step": "build_quiet", "step_label": "build", "level": level,
             "targets": list(targets), "reason": "build_quiet/level-%d" % level,
             "orchestrators": ["orch1", "orch2"], "project": "ameesh",
             "critical": False, "urgent": False}
    alert.update(kw)
    return alert


LISTING = [_row("dev1"), _row("orch1"), _row("orch2"), _row("bureau"),
           _row("auditeur")]


def _briefs(alert, listing=LISTING, covered=()):
    return alert_briefs.briefs(alert, listing, auditor_names=["auditeur"],
                               orchestrator_names=("orch1", "orch2"),
                               covered=covered)


class GraceTest(unittest.TestCase):
    def test_l1_a_3_min_differe_a_6_min_brief(self):
        self.assertTrue(alert_briefs.slo_grace_unbriefed(_slo(overdue=180)))
        self.assertFalse(alert_briefs.slo_grace_unbriefed(_slo(overdue=360)))

    def test_urgent_p1_et_l2_sans_grace(self):
        self.assertFalse(alert_briefs.slo_grace_unbriefed(
            _slo(overdue=60, urgent=True, critical=True)))
        self.assertFalse(alert_briefs.slo_grace_unbriefed(_slo(overdue=60, critical=True)))
        self.assertFalse(alert_briefs.slo_grace_unbriefed(
            _slo(level=2, overdue=60, targets=("pilot",))))
        self.assertFalse(alert_briefs.slo_grace_unbriefed(
            {"type": "stale_lot", "level": 1, "value": 60}))

    def test_l1_au_seul_debiteur(self):
        # Une attente de fusion vise le pilote dès le niveau 1 : un seul
        # orchestrateur la reçoit, et le porteur seul quand il est visé.
        out = _briefs(_slo(targets=("pilot",)))
        self.assertEqual(len(out), 1)
        self.assertIn(next(iter(out)), ("orch1", "orch2"))
        self.assertEqual(out, _briefs(_slo(targets=("pilot",))))  # stable
        self.assertEqual(list(_briefs(_slo(targets=("assignee", "pilot")))), ["dev1"])

    def test_l1_deja_remis_reste_le_seul(self):
        # Relecture mty-amh-docs-01 (b491b7034, réécrit) : le pilote déjà servi
        # garde le courrier, même quand un autre serait choisi à cette passe.
        listing = [_row("dev1"), _row("orch1"),
                   dict(_row("orch2"), lease_live=False), _row("bureau"),
                   _row("auditeur")]
        out = _briefs(_slo(targets=("pilot",)), listing, covered={"orch2"})
        self.assertEqual(list(out), ["orch2"])
        for served in ("orch1", "orch2"):
            self.assertEqual(list(_briefs(_slo(targets=("pilot",)),
                                          covered={served})), [served])

    def test_l2_avec_copies_et_p1_inchange(self):
        out = _briefs(_slo(level=2, targets=("pilot",)))
        self.assertEqual(sorted(out), ["orch1", "orch2"])
        out = _briefs(_slo(targets=("assignee", "bureau", "auditeur"),
                           critical=True, urgent=True, overdue=30))
        self.assertEqual(sorted(out), ["auditeur", "bureau", "dev1"])


class NotifierGraceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = SimpleNamespace(canon=(), state_dir=self.tmp.name)
        self.store = mock.Mock()
        self.store.operations.listing.side_effect = lambda: list(LISTING)
        self.store.operations.open_lots_activity.side_effect = lambda _: [
            {"id": 42, "state": "build", "assignee": "dev1"}]
        for target, attr, value in ((storage, "of", self.store),
                                    (mail, "send", 1),
                                    (notify.fil, "record", "thread-id")):
            patch = mock.patch.object(target, attr, return_value=value)
            started = patch.start()
            self.addCleanup(patch.stop)
            if attr == "send":
                self.send = started
        self.notifier = notify.Notifier(self.cfg, notify.parse_config({"default": []}),
                                        sender=object(), log=lambda text: None)

    def run_pass(self, alert, now):
        self.notifier._brief_agents(object(), [alert], None, now)

    def recipients(self):
        return [call.args[2] for call in self.send.call_args_list]

    def test_l1_differe_puis_un_seul_courrier_par_lot_et_etape(self):
        self.run_pass(_slo(overdue=180), now=10_000)
        self.assertEqual(self.recipients(), [])
        self.run_pass(_slo(overdue=360), now=10_180)
        self.assertEqual(self.recipients(), ["dev1"])
        self.run_pass(_slo(overdue=420), now=10_240)
        self.assertEqual(self.recipients(), ["dev1"])
        meta = self.send.call_args.kwargs["payload"]
        self.assertEqual((meta["lot"], meta["step"], meta["level"]),
                         (42, "build_quiet", 1))
        # pas de `work_item_id` : le courrier compterait comme activité du lot
        self.assertNotIn("work_item_id", self.send.call_args.kwargs)

    def test_l1_pas_de_second_courrier_quand_le_bail_cligne(self):
        # Relecture mty-amh-docs-01 (b491b7034, réécrit) : le pilote servi
        # perd son bail un instant ; l'autre, éveillé, ne reçoit rien.
        alive = [dict(row, lease_owner="runner") for row in LISTING]
        listing = alive
        self.store.operations.listing.side_effect = lambda: listing
        alert = _slo(targets=("pilot",), lot=43, overdue=360)
        self.run_pass(alert, now=10_000)
        first = self.recipients()
        self.assertEqual(len(first), 1)
        listing = [dict(row, lease_live=False, lease_owner=None)
                   if row["name"] == first[0] else row for row in alive]
        self.run_pass(dict(alert, value=420), now=10_060)
        self.assertEqual(self.recipients(), first)

    def test_p1_part_tout_de_suite(self):
        self.run_pass(_slo(overdue=30, critical=True, urgent=True,
                           targets=("assignee", "bureau", "auditeur")), now=10_000)
        self.assertEqual(sorted(self.recipients()), ["auditeur", "bureau", "dev1"])


class HeadLotTest(_Base):
    def setUp(self):
        super().setUp()
        for name in ("orch", "dev1"):
            self.agent(name)
        self.lot = work.add(self.db, title="Un lot", app="ameesh")

    def sent(self, text):
        proc = self.cli("send", "dev1", text, "--from", "orch",
                        env=self.env(AMEESH_ALERT_ORCHESTRATORS="orch"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return self.unread("dev1")[-1]["work_item_id"]

    def test_numero_de_tete_rattache(self):
        self.assertEqual(self.sent("#%d avis rendu, rien à faire." % self.lot["id"]),
                         str(self.lot["id"]))
        self.assertEqual(self.sent("  #%d: suite" % self.lot["id"]), str(self.lot["id"]))
        # un rattachement deviné n'attribue rien
        self.assertIsNone(work.get(self.db, self.lot["id"]).get("assignee"))

    def test_numero_au_milieu_ou_inconnu_ignore(self):
        self.assertIsNone(self.sent("Voir le lot #%d plus tard." % self.lot["id"]))
        self.assertIsNone(self.sent("#999999 n'existe pas."))
        self.assertIsNone(self.sent("#%d-bis n'est pas un numéro." % self.lot["id"]))


if __name__ == "__main__":
    unittest.main()
