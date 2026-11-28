# SPDX-License-Identifier: AGPL-3.0-only
"""#8468 (manaty/ameesh#6678) : un système non-agent déclaré (`system:<nom>`,
#6091) réserve les lots qu'il crée pour lui-même (`work add --actor
system:<nom> --external-executor system:<nom>`), et seulement ceux-là.

La garde est étroite : un autre exécutant, le lot d'un autre agent et les
autres gestes de réservation restent des gestes de pilote. Une session liée à
un agent ne prend pas l'identité du système, et le journal de création nomme
le système et son responsable humain déclaré.
"""
from __future__ import annotations

from unittest import mock

from ameesh import identity, storage, work

from .test_l119_backlog import _Base


class ReservationSystemeTest(_Base):
    NAME = "system:ima-backlog"
    HUMAN = "human:proprio"

    def declare(self, name=None, responsible=None):
        name = name or self.NAME
        responsible = responsible or self.HUMAN
        return work.declare_external_executor(
            self.db, name, responsible, actor=self.HUMAN, cfg=self.cfg)

    def add(self, *, actor=None, executor=None, title="lot du système", **kw):
        return work.add(self.db, title=title, app="nexlink",
                        external_executor=executor or self.NAME,
                        actor=actor or self.NAME, cfg=self.cfg, **kw)

    def events(self, lot_id):
        return storage.of(self.db).work.events(int(lot_id), None)

    def created(self, lot_id):
        return [event for event in self.events(lot_id)
                if (event["note"] or "").startswith("created")]

    def count_lots(self):
        return int(self.db.query("SELECT count(*) AS n FROM work_items")[0]["n"])

    # -- le chemin ouvert ----------------------------------------------------
    def test_un_systeme_declare_cree_et_reserve_son_propre_lot(self):
        self.declare()
        lot = self.add()
        self.assertEqual(lot["external_executor"], self.NAME)
        self.assertIsNone(lot["assignee"])
        events = self.events(lot["id"])
        created = self.created(lot["id"])
        self.assertEqual(len(created), 1)
        # Le journal nomme le système (acteur) ET son responsable humain.
        self.assertEqual(created[0]["actor"], self.NAME)
        self.assertIn("reserved to external executor %s" % self.NAME,
                      created[0]["note"])
        self.assertIn("responsible human:proprio", created[0]["note"])
        # La note de création reste lisible par la mesure SLO : l'exécutant
        # nommé est le système seul, pas la fin de la ligne.
        from ameesh import slo
        self.assertEqual(
            [period["executor"]
             for period in slo.external_reservation_periods(lot, events)],
            [self.NAME])
        # L165 ne prend toujours pas un lot réservé.
        self.agent("claude1", team="nexlink")
        self.assertNotIn(lot["id"],
                         [item["id"] for item in
                          storage.of(self.db).work.unassigned_lots(10)])
        self.assertEqual(self.take(), [])

    def test_un_pilote_humain_reserve_toujours_pour_le_systeme(self):
        self.declare()
        lot = self.add(actor=self.HUMAN)
        self.assertEqual(lot["external_executor"], self.NAME)
        event = self.created(lot["id"])[0]
        self.assertEqual(event["actor"], self.HUMAN)
        self.assertIn("responsible human:proprio", event["note"])

    def test_le_lot_du_systeme_porte_la_cle_durable_et_la_session(self):
        self.declare()
        lot = self.add(unique_ref="ima-8468", external_session="ima-run-1")
        self.assertEqual(lot["external_executor"], self.NAME)
        self.assertEqual(lot["external_session"], "ima-run-1")
        # Un rejeu rend le même lot (aucune nouvelle écriture).
        again = self.add(unique_ref="ima-8468", external_session="ima-run-1")
        self.assertTrue(again.get("existing"))
        self.assertEqual(again["id"], lot["id"])
        self.assertEqual(self.count_lots(), 1)

    # -- les refus -----------------------------------------------------------
    def test_un_systeme_ne_reserve_pas_pour_un_autre_executant(self):
        self.declare()
        self.declare(name="system:autre")
        with self.assertRaises(work.WorkError) as raised:
            self.add(executor="system:autre")
        self.assertIn("never a lot reserved to system:autre",
                      str(raised.exception))
        # Un humain n'est pas un système : le refus est celui de `corrector`,
        # un système n'ayant aucune autorité de pilote.
        with self.assertRaises(work.WorkError) as raised:
            self.add(executor=self.HUMAN)
        self.assertIn("%s is neither a human" % self.NAME,
                      str(raised.exception))
        self.assertEqual(self.count_lots(), 0)

    def test_un_systeme_inconnu_ou_retire_est_refuse_avant_ecriture(self):
        with self.assertRaises(work.WorkError) as raised:
            self.add()
        self.assertIn("not declared", str(raised.exception))
        self.declare()
        work.retire_external_executor(self.db, self.NAME, actor=self.HUMAN,
                                      cfg=self.cfg)
        with self.assertRaises(work.WorkError) as raised:
            self.add()
        self.assertIn("was retired", str(raised.exception))
        self.assertEqual(self.count_lots(), 0)

    def test_une_session_liee_a_un_agent_ne_prend_pas_l_identite_du_systeme(self):
        self.declare()
        binding = identity.Binding(name="claude1", source="explicit", ok=True)
        with mock.patch.object(identity, "resolve_binding", return_value=binding):
            with self.assertRaises(work.WorkError) as raised:
                self.add()
        self.assertIn("this session belongs to claude1, it does not act on behalf "
                      "of %s" % self.NAME, str(raised.exception))
        self.assertEqual(self.count_lots(), 0)

    def test_un_systeme_ne_prend_pas_le_lot_d_un_agent(self):
        self.declare()
        lot = work.add(self.db, title="lot d'un agent", app="nexlink",
                       actor=self.HUMAN, cfg=self.cfg)
        with self.assertRaises(work.WorkError) as raised:
            work.set_external_executor(self.db, lot["id"], self.NAME,
                                       actor=self.NAME, cfg=self.cfg)
        self.assertIn("%s is neither a human" % self.NAME,
                      str(raised.exception))
        self.assertIsNone(work.get(self.db, lot["id"])["external_executor"])

    # -- CLI -----------------------------------------------------------------
    def test_cli_un_systeme_declare_cree_son_lot(self):
        self.declare()
        proc = self.mesh("work", "add", "--title", "lot IMA", "--app", "agence-x",
                         "--new-project", "--external-executor", self.NAME,
                         "--actor", self.NAME)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("executor : %s (external, reserved; not assigned"
                      % self.NAME, proc.stdout)
        lot_id = int(proc.stdout.split("#")[1].split()[0])
        item = work.get(self.db, lot_id)
        self.assertEqual(item["external_executor"], self.NAME)
        self.assertIsNone(item["assignee"])
        event = self.created(lot_id)[0]
        self.assertEqual(event["actor"], self.NAME)
        self.assertIn("responsible human:proprio", event["note"])

    def test_cli_l_ancienne_identite_nue_reste_refusee(self):
        self.declare()
        self.agent("ima-backlog-1", harness="claude")
        proc = self.mesh("work", "add", "--title", "lot IMA", "--app", "agence-x",
                         "--new-project", "--external-executor", self.NAME,
                         "--actor", "ima-backlog-1", "--estimate", "30m")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("ima-backlog-1 is neither a human", proc.stderr)
        self.assertEqual(self.count_lots(), 0)
