# SPDX-License-Identifier: AGPL-3.0-only
"""#3973 (panier de #3969) — un agent bloqué par sa configuration de comptes
n'est ni désigné relecteur ni attribuable à distance.

Constat (relecture de #3969, ameesh-dev3, 27/10 13:25) : l'exécuteur écrit
désormais « accounts : no valid … account » (`accounts.CONFIG_BLOCK_PREFIX`)
au lieu d'une pause budget ; la désignation (`review_designation.is_available`,
préfixes `paused`/`budget : `) et l'attribution d'un agent distant
(`account_turn.turn_hold`, qui ne lit que la pause budget annoncée) ne le
voyaient plus : un agent qui ne peut pas tourner restait relecteur désigné et
recevait des lots.

Témoins : le blocage de comptes rend l'agent indisponible à la désignation et
refuse son attribution, nommant la cause ; un agent `idle` dont le texte
commence par hasard comme le préfixe reste disponible ; le littéral de la
désignation suit le préfixe de l'exécuteur.
"""
from __future__ import annotations

from ameesh import accounts, registry, work
from ameesh import review_designation as rd

from .test_l242b_critical_accounts import _Comptes

RAISON = ("no valid claude account: primary: directory not private (755, "
          "expected 700) (fix: chmod 700 /home/ameesh/.claude-primary)")


def row(*, status="blocked", text=accounts.CONFIG_BLOCK_PREFIX + RAISON):
    return {"name": "dev4", "team": "ameesh", "harness": "claude",
            "session_harness": "", "status": status, "mode": "execute",
            "status_text": text, "operator_paused": False}


class DesignationTest(_Comptes):
    def test_prefixe_partage_avec_l_executeur(self):
        self.assertIn(accounts.CONFIG_BLOCK_PREFIX, rd.PAUSE_PREFIXES)

    def test_blocage_de_comptes_indisponible(self):
        self.assertFalse(rd.is_available(row()))

    def test_agent_sain_reste_disponible(self):
        self.assertTrue(rd.is_available(row(status="idle", text="last turn: ok")))


class AttributionDistanteTest(_Comptes):
    def lot(self):
        return work.add(self.db, title="a confier", cfg=self.cfg)

    def _distant(self, status, text):
        registry.upsert(self.db, "distant", harness="claude", host="hote-voisin",
                        cwd=self.tmp)
        self.db.execute(
            "UPDATE agent_registry SET status = %s, status_text = %s WHERE name = %s",
            (status, text, "distant"))

    def test_blocage_de_comptes_distant_refuse(self):
        self._distant("blocked", accounts.CONFIG_BLOCK_PREFIX + RAISON)
        with self.assertRaises(work.WorkError) as ctx:
            work.check_assignee(self.db, "distant", cfg=self.cfg)
        msg = str(ctx.exception)
        self.assertIn("is held —", msg)
        self.assertIn("chmod 700", msg)          # la cause et le remède sont nommés
        lot = self.lot()
        with self.assertRaises(work.WorkError):
            work.assign(self.db, lot["id"], "distant", cfg=self.cfg)
        self.assertIsNone(work.get(self.db, lot["id"])["assignee"])

    def test_texte_hors_blocage_accepte(self):
        # `idle` : le texte n'est pas un blocage annoncé
        self._distant("idle", accounts.CONFIG_BLOCK_PREFIX + RAISON)
        self.assertEqual(work.check_assignee(self.db, "distant", cfg=self.cfg)["assignee"],
                         "distant")
