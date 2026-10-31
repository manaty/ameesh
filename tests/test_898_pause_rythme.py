# SPDX-License-Identifier: AGPL-3.0-only
"""#898 : l'échéance d'une pause de rythme est l'heure où le rythme rejoint
l'utilisation, pas la remise à zéro de la fenêtre.

Une fenêtre tenue par le seul rythme se libère d'elle-même : la part écoulée
rattrape l'utilisation (la garde est réévaluée à chaque tour). Annoncer la
remise à zéro faisait lire « en pause jusqu'au 20/10 » pour un forfait utilisé
à 5 %. Seule une utilisation au plafond de rythme (90 %, moins la réserve des
non-critiques) attend la fenêtre suivante.
"""
from __future__ import annotations

import re
import time
from unittest import mock

from ameesh import accounts, cost

from .test_l242b_critical_accounts import _Comptes

JOUR = 86400.0
FENETRE = 7 * JOUR


class PauseDeRythmeTest(_Comptes):
    """Jauge de 7 jours à 5 % de la fenêtre écoulée, sur le secondaire (aucun
    plafond fixe)."""

    def jauge_longue(self, part, ecoule=0.05):
        reset = self.now + (1.0 - ecoule) * FENETRE
        return cost.Gauge("codex", "codex-10080min", part, reset, FENETRE)

    def evaluate(self, jauge, **kw):
        with mock.patch.object(self.book, "codex_gauges", return_value=[jauge]):
            return accounts.evaluate(self.book, self.items[1], self.items, self.now,
                                     record=False, **kw)

    def heure(self, ts):
        return time.strftime(
            "%H:%M" if time.localtime(ts)[:3] == time.localtime(self.now)[:3]
            else "%d/%m %H:%M", time.localtime(ts))

    def test_une_tenue_de_rythme_annonce_l_heure_de_croisement(self):
        # 10 % utilisés, 5 % de la fenêtre écoulée : avec la réserve des
        # non-critiques le rythme permis vaut max(rythme − réserve, marge)
        # = 10 % (PACE_MARGIN, #777), il rejoint 10 % à 10 % de la fenêtre —
        # la reprise est là, pas à la remise à zéro (7 jours).
        ev = self.evaluate(self.jauge_longue(0.10), reserve=True)
        self.assertFalse(ev.hard)
        self.assertEqual(ev.held, ("codex-10080min", 0.10))
        self.assertAlmostEqual(ev.until, self.now + 0.05 * FENETRE, delta=1.0)

    def test_un_agent_critique_se_libere_au_croisement_sans_reserve(self):
        # sans réserve, le rythme permis vaut 10 % + 5 % écoulés : 30 %
        # utilisés sont rejoints à 20 % de la fenêtre.
        ev = self.evaluate(self.jauge_longue(0.30), critical=True)
        self.assertFalse(ev.hard)
        self.assertAlmostEqual(ev.until, self.now + 0.15 * FENETRE, delta=1.0)

    def test_une_utilisation_au_plafond_de_rythme_attend_la_remise_a_zero(self):
        # 85 % utilisés : au-dessus du plafond de rythme (80 % avec la
        # réserve), il ne redescend pas avant la fenêtre suivante.
        jauge = self.jauge_longue(0.85)
        ev = self.evaluate(jauge, reserve=True)
        self.assertAlmostEqual(ev.until, jauge.resets_at, delta=1.0)

    def test_la_pause_nomme_la_fenetre_qui_bloque_le_plus_longtemps(self):
        # une fenêtre courte au plafond (remise dans 30 min) et une longue
        # tenue par le rythme (croisement dans 8 h 24) : c'est la longue.
        court = cost.Gauge("codex", "codex-300min", 0.96, self.now + 1800, 5 * 3600.0)
        long = self.jauge_longue(0.10)
        with mock.patch.object(self.book, "codex_gauges", return_value=[court, long]):
            ev = accounts.evaluate(self.book, self.items[1], self.items, self.now,
                                   record=False, reserve=True)
        self.assertEqual(ev.held[0], "codex-10080min")
        self.assertAlmostEqual(ev.until, self.now + 0.05 * FENETRE, delta=1.0)

    def test_sans_comptes_declares_la_pause_annonce_le_croisement(self):
        # même règle sur le chemin sans comptes déclarés
        # (cost.CostBook.pace_exceeded), qui porte le compte par défaut.
        jauge = self.jauge_longue(0.10)
        with mock.patch.object(self.book, "gauges", return_value=[jauge]), \
                mock.patch.object(self.book, "clock", return_value=self.now):
            raison = self.book.pace_exceeded("codex", accounts.RESERVE)
        # #4781 : le libellé porte l'échéance absolue (`@epoch`), lue à part.
        self.assertTrue(re.sub(r"@\d+;", ";", raison).startswith(
            "pause (account (default), codex-10080min 10 %%) until %s; plan codex"
            % self.heure(self.now + 0.05 * FENETRE)), raison)
        self.assertAlmostEqual(accounts.pause_deadline(raison),
                               self.now + 0.05 * FENETRE, delta=60.0)
