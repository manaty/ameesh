# SPDX-License-Identifier: AGPL-3.0-only
"""#7006 — un agent critique quitte son compte avant sa saturation.

Le tour de résumé d'une rotation tourne sous l'ANCIEN compte et n'est admis que
si aucune de ses jauges n'est à 100 % (`account_turn.summary_possible`). Un
agent critique peut courir jusqu'à 100 % (`pace_ceiling: 1.0` d'un compte
secondaire, ou relèvement de fin de fenêtre) : la bascule se déclenchait donc
exactement quand le résumé n'était plus possible, et la session repartait du
brief déterministe. La marge de résumé (`accounts.SUMMARY_MARGIN`) fait
basculer le critique avant, ou l'arrête dans la marge si aucun autre compte
n'est plaçable — la session reste alors résumable.
"""
from __future__ import annotations

import os
from unittest import mock

from ameesh import account_turn, accounts, cost

from .test_l30_accounts import _Base as _L30Base, _dossier

HOUR = 3600.0
WEEK = 7 * 24 * HOUR


class _MargeBase(_L30Base):
    """Comptes Claude sans identifiants réels ; jauges données par le test."""

    def setUp(self) -> None:
        super().setUp()
        self.now = 1_800_000_000.0
        self.primaire_dir = _dossier(os.path.join(self.tmp, "claude-primaire"))
        self.secondaire_dir = _dossier(os.path.join(self.tmp, "claude-secondaire"))
        self.items = accounts.parse({"claude": [
            {"name": "primaire", "path": self.primaire_dir, "cap": 0.9,
             "check_credentials": False},
            {"name": "secondaire", "path": self.secondaire_dir, "priority": 20,
             "pace_ceiling": 1.0, "short_window_cap": 0.75,
             "check_credentials": False},
        ]})["claude"]
        self.book = cost.CostBook(state_dir=self.cfg.state_dir, db=self.db,
                                  tools={"critique": "claude"})
        self.gauges = {}
        self._patch_gauges()

    def _patch_gauges(self) -> None:
        def gauges_of(book, profile, items, environ=None, *, model=None, now=None):
            return self.gauges.get(profile.name, cost.Gauges())

        patcher = mock.patch.object(accounts, "gauges_of", gauges_of)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _reading(self, seven: float, five: float = 0.10) -> cost.Gauges:
        # fin de fenêtre : le plafond de rythme atteint son maximum (1.0 sur le
        # secondaire), c'est là que la bascule coïncidait avec la saturation.
        return cost.Gauges([
            cost.Gauge("claude", "five_hour", five, self.now + HOUR, 5 * HOUR),
            cost.Gauge("claude", "seven_day", seven, self.now + 0.05 * WEEK, WEEK),
        ])

    def _choose(self, **kw):
        return accounts.choose(self.db, self.cfg.host, "claude", self.items, self.book,
                               now=self.now, **kw)

    def _summary_possible(self):
        old = accounts.by_name(self.items, "secondaire")
        return account_turn.summary_possible(old, self.book, self.items, now=self.now)


class MargeDeResumeTest(_MargeBase):
    def test_critique_bascule_avant_la_saturation_et_le_resume_reste_possible(self):
        self.gauges = {"primaire": self._reading(0.30),
                       "secondaire": self._reading(0.995)}
        self.assertTrue(self._summary_possible()[0],
                        "témoin de départ : à 99,5 %% le résumé est encore possible")

        choix = self._choose(critical=True, session_account="secondaire")
        self.assertEqual(getattr(choix.profile, "name", None), "primaire")
        self.assertFalse(choix.kept)
        self.assertEqual((choix.switched["from"], choix.switched["to"]),
                         ("secondaire", "primaire"))
        # le motif dit que la marge, pas un seuil ordinaire, a décidé
        self.assertIn("summary margin", choix.why)

        # À 100 %, le même résumé est refusé : c'est ce que la marge évite.
        self.gauges["secondaire"] = self._reading(1.0)
        possible, raison = self._summary_possible()
        self.assertFalse(possible)
        self.assertIn("saturated", raison)

    def test_critique_hors_marge_garde_la_continuite(self):
        self.gauges = {"primaire": self._reading(0.30),
                       "secondaire": self._reading(0.97)}
        choix = self._choose(critical=True, session_account="secondaire")
        self.assertEqual(getattr(choix.profile, "name", None), "secondaire")
        self.assertTrue(choix.kept)
        self.assertIsNone(choix.switched)

    def test_sans_autre_compte_le_critique_s_arrete_dans_la_marge(self):
        # Un seul compte : le quitter n'est pas possible. On s'arrête à 99 %,
        # pas à 100 %, pour garder la session résumable quand un compte se
        # libérera.
        self.items = [accounts.by_name(self.items, "secondaire")]
        self.gauges = {"secondaire": self._reading(0.995)}
        choix = self._choose(critical=True, session_account="secondaire")
        self.assertIsNone(choix.profile)
        self.assertIn("summary margin", choix.reason)
        self.assertIn("99.5", choix.reason)
        self.assertIsNotNone(accounts.pause_deadline(choix.reason))
        # la session n'a pas été consommée : le compte reste sous 100 %
        self.assertTrue(self._summary_possible()[0])

    def test_le_repli_au_dela_du_plafond_du_primaire_reste_atteignable(self):
        # Le seul autre compte est le primaire, au-delà de son plafond implicite
        # de 50 % : le chemin `beyond` de L242b doit encore le trouver dans la
        # marge, sans garder la session sur le secondaire qui sature.
        self.items = accounts.parse({"claude": [
            {"name": "primaire", "check_credentials": False},
            {"name": "secondaire", "path": self.secondaire_dir, "priority": 20,
             "pace_ceiling": 1.0, "short_window_cap": 0.75,
             "check_credentials": False},
        ]})["claude"]
        self.assertTrue(self.items[0].default)
        self.gauges = {"primaire": self._reading(0.60),
                       "secondaire": self._reading(0.995)}
        choix = self._choose(critical=True, session_account="secondaire")
        self.assertEqual(getattr(choix.profile, "name", None), "primaire")
        self.assertIn("beyond its 50% cap", choix.why)
        self.assertIn("summary margin", choix.why)
