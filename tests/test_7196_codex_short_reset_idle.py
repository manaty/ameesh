# SPDX-License-Identifier: AGPL-3.0-only
"""#7196 — après le reset connu de la courte, un compte sans tour redevient éligible.

Nuit du 13/11 : codex/secondaire saturé sur sa fenêtre courte (reset connu à
05:56Z), ameesh-cx1/cx2 en pause. Après le reset, la courte retenue est échue
(#2843 : pas de courte vivante) et le dernier relevé a moins de 5 h (#3044 :
pas au repos). Le compte restait refusé « incomplete reading » jusqu'à 5 h
après son dernier tour ; en pause, il ne pouvait jamais produire la courte
vivante qui lève le refus.

Chaque tour publie un relevé : si aucun relevé lu ni aucun journal non lu
n'est postérieur au reset connu, aucun tour n'a couru depuis, la courte
suivante est vierge. Un relevé ou un journal non lu après ce reset garde le
refus #2843.
"""
from __future__ import annotations

import os
import unittest

from ameesh import accounts, codex_gauges

from .test_cost import Sandbox
from .test_2839_codex_account_gauges import event, journal
from .test_3044_codex_at_rest import DAY, declared_codex, team_event, team_partial

HOUR = 3600.0


class ShortResetIdleTest(Sandbox, unittest.TestCase):
    def _declared(self, plan="team"):
        home = os.path.join(self.tmp_cost, "compte-codex")
        sessions = os.path.join(home, "sessions")
        declared_codex(home, plan)
        profile = accounts.parse(
            {"codex": [{"name": "secondaire", "path": home}]})["codex"][0]
        return sessions, profile

    def _truncate(self, sessions, unread_at):
        """Échantillon tronqué : 32 journaux pro récents, un 33e non lu daté `unread_at`."""
        for i in range(codex_gauges.MAX_FILES - 1):
            journal(sessions, "pro-%d" % i,
                    event(self.now, short=None, week=13, plan="pro"),
                    mtime=self.now + i + 1)
        journal(sessions, "hors-echantillon",
                event(unread_at, short=100, week=90), mtime=unread_at)

    def _saturated(self, sessions, mtime=None):
        # Dernier tour il y a 1 h : courte saturée, reset connu il y a 4 min.
        journal(sessions, "team-sature",
                team_event(self.now - HOUR, short=100, week=50,
                           short_reset=self.now - 4 * 60),
                mtime=self.now - HOUR if mtime is None else mtime)

    def _evaluate(self, profile):
        return accounts.evaluate(self.book(), profile, [profile], self.now,
                                 record=False)

    def test_saturated_short_past_its_reset_without_a_turn_is_eligible(self):
        # Témoin de l'interblocage : sans le correctif, refus jusqu'à now+4 h.
        sessions, profile = self._declared("team")
        self._saturated(sessions)
        self._truncate(sessions, self.now - 30 * DAY)

        result = self._evaluate(profile)

        self.assertTrue(result.ok, result.reason)
        self.assertFalse(result.hard)
        self.assertEqual({g.key: g.used_at(self.now) for g in result.gauges},
                         {"codex-300min": 0.0, "codex-10080min": 0.5})

    def test_a_reading_after_the_reset_keeps_the_refusal(self):
        # Un tour après le reset a publié un relevé sans courte : la courte
        # vivante peut être saturée hors de l'échantillon (#2843).
        sessions, profile = self._declared("team")
        self._saturated(sessions)
        journal(sessions, "team-apres", team_partial(self.now - 60),
                mtime=self.now - 60)
        for i in range(codex_gauges.MAX_FILES - 2):
            journal(sessions, "pro-%d" % i,
                    event(self.now, short=None, week=13, plan="pro"),
                    mtime=self.now + i + 1)
        journal(sessions, "hors-echantillon",
                event(self.now - 30 * DAY, short=100, week=90),
                mtime=self.now - 30 * DAY)

        result = self._evaluate(profile)

        self.assertFalse(result.ok)
        self.assertTrue(result.hard)
        self.assertIn("short window", result.reason)

    def test_an_unread_journal_after_the_reset_keeps_the_refusal(self):
        # Un journal non lu, touché après le reset, a pu observer une courte
        # vivante saturée (#3122). Le journal saturé, touché depuis sans
        # nouveau relevé, reste dans l'échantillon lu.
        sessions, profile = self._declared("team")
        self._saturated(sessions, mtime=self.now)
        self._truncate(sessions, self.now - 60)

        result = self._evaluate(profile)

        self.assertFalse(result.ok)
        self.assertTrue(result.hard)
        self.assertIn("short window", result.reason)


if __name__ == "__main__":
    unittest.main()
