# SPDX-License-Identifier: AGPL-3.0-only
"""#5035 : `work move` n'avance pas le lot d'un autre.

Le 31/10, `mty-amh-impl-05` a fait passer #5029 d'intake à build juste après le
refus de sa prise : le lot était à `ameesh-dev3`, le journal a dit un démarrage
qui n'en était pas un.
"""
from __future__ import annotations

from ameesh import work

from .support import PgTestCase


class MoveLotOfAnotherTest(PgTestCase):
    def setUp(self):
        super().setUp()
        for name in ("porteur", "tiers", "relecteur", "pilote", "auditeur", "bureau"):
            self.assertEqual(self.register(name, "codex").returncode, 0)
        self.lot = work.add(self.db, title="lot du porteur", assignee="porteur")["id"]

    def _env(self, name=None, **extra):
        env = self.env(**extra)
        env.pop("AGENT_MAIL_NAME", None)
        if name:
            env["AGENT_MAIL_NAME"] = name
        return env

    def move(self, state, *args, session=None, lot=None, **extra):
        return self.mesh("work", "move", str(lot or self.lot), state, *args,
                         env=self._env(session, **extra))

    def lot_state(self, lot=None):
        return work.get(self.db, lot or self.lot)["state"]

    def test_un_tiers_est_refuse_et_rien_ne_change(self):
        proc = self.move("build", session="tiers")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("held by porteur", proc.stderr)
        self.assertEqual(self.lot_state(), "intake")

    def test_actor_ne_prete_pas_l_identite_du_porteur(self):
        for actor in ("porteur", "human:quelqu-un"):
            proc = self.move("build", "--actor", actor, session="tiers")
            self.assertNotEqual(proc.returncode, 0, actor)
        self.assertEqual(self.lot_state(), "intake")

    def test_le_porteur_avance_son_lot(self):
        proc = self.move("build", session="porteur")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.lot_state(), "build")

    def test_humain_et_session_sans_identite_passent(self):
        proc = self.move("build", "--actor", "human:owner")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self.move("qa")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_pilote_bureau_auditeur_passent(self):
        proc = self.move("build", session="pilote",
                         AMEESH_ALERT_ORCHESTRATORS="pilote")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self.move("blocked", session="bureau")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self.move("build", session="auditeur")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_le_relecteur_designe_rend_un_lot_en_revue(self):
        work.move(self.db, self.lot, "build", actor="porteur")
        work.move(self.db, self.lot, "qa", actor="porteur")
        proc = self.move("build", session="relecteur")
        self.assertNotEqual(proc.returncode, 0)
        self.db.execute("UPDATE work_items SET reviewer = 'relecteur', "
                        "reviewers = ARRAY['relecteur'] WHERE id = %s", (self.lot,))
        proc = self.move("build", session="relecteur")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.lot_state(), "build")

    def test_la_porte_du_quorum_garde_son_propre_refus(self):
        work.move(self.db, self.lot, "build", actor="porteur")
        work.move(self.db, self.lot, "qa", actor="porteur")
        proc = self.move("validated", session="tiers")
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("held by", proc.stderr)
        self.assertIn("review quorum", proc.stderr)

    def test_un_lot_sans_porteur_reste_deplacable(self):
        free = work.add(self.db, title="lot libre")["id"]
        proc = self.move("build", session="tiers", lot=free)
        self.assertEqual(proc.returncode, 0, proc.stderr)
