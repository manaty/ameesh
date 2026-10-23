# SPDX-License-Identifier: AGPL-3.0-only
"""#3122 — le repos #3044 ne se prouve pas sur les journaux non lus.

#3044 accepte un compte team dont la dernière courte retenue est échue quand
le relevé le plus récent de l'échantillon est plus vieux qu'une fenêtre
courte. Ce raisonnement ne vaut que pour ce qui a été **lu** : un journal
omis du scan borné (33e par mtime) ou tronqué (> TAIL_BYTES), ou un journal
sauté sur OSError, peut porter une courte vivante saturée. Sa date de fichier
borne ce qu'il a pu observer : le repos n'est prouvé que si tout l'inconnu
est plus vieux qu'une fenêtre courte.
"""
from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from ameesh import accounts, codex_gauges

from .test_cost import Sandbox
from .test_2839_codex_account_gauges import event, journal
from .test_3044_codex_at_rest import DAY, declared_codex, team_event

HOUR = 3600.0


class UnreadBoundTest(Sandbox, unittest.TestCase):
    def _declared(self, plan="team"):
        home = os.path.join(self.tmp_cost, "compte-codex")
        sessions = os.path.join(home, "sessions")
        declared_codex(home, plan)
        profile = accounts.parse(
            {"codex": [{"name": "secondaire", "path": home}]})["codex"][0]
        return sessions, profile

    def _evaluate(self, profile):
        return accounts.evaluate(self.book(), profile, [profile], self.now,
                                 record=False)

    def _pro_sample(self, sessions, count=codex_gauges.MAX_FILES - 2):
        """`count` journaux pro sains récents, plus un 33e journal très ancien."""
        for i in range(count):
            journal(sessions, "pro-%d" % i,
                    event(self.now, short=None, week=13, plan="pro"),
                    mtime=self.now + i + 1)
        journal(sessions, "hors-echantillon",
                event(self.now - 30 * DAY, short=100, week=90),
                mtime=self.now - 30 * DAY)

    def test_32_touched_old_journals_cannot_mask_a_hidden_saturated_short(self):
        # Témoin cx1 : la courte saturée observée il y a 30 min est le 33e
        # journal par mtime ; 32 journaux team à l'ancien relevé (courte échue
        # à 6 %) ont été touchés plus récemment sans nouveau relevé. Le 33e
        # prouve pourtant qu'une courte vivante a pu être observée depuis.
        sessions, profile = self._declared("team")
        journal(sessions, "sature",
                team_event(self.now - 30 * 60, short=100, week=50,
                           short_reset=self.now + HOUR),
                mtime=self.now - 30 * 60)
        for i in range(codex_gauges.MAX_FILES):
            journal(sessions, "touche-%d" % i,
                    team_event(self.now - 11 * HOUR, short=6, week=6),
                    mtime=self.now - 20 * 60 + i)

        result = self._evaluate(profile)

        self.assertFalse(result.ok)
        self.assertTrue(result.hard)
        self.assertIn("incomplete Codex account reading", result.reason)
        self.assertIn("short window", result.reason)

    def test_a_live_short_in_an_omitted_prefix_is_not_rest(self):
        # Témoin mesh-design : le préfixe d'un journal > TAIL_BYTES porte une
        # courte vivante saturée (observée il y a 1 h) ; la queue, faite
        # d'écritures sans jauges, ne montre rien. Le repos ne peut être
        # prouvé tant que la date de ce journal est dans la fenêtre courte.
        sessions, profile = self._declared("team")
        journal(sessions, "team-rest",
                team_event(self.now - 11 * HOUR, short=6, week=6),
                mtime=self.now - 11 * HOUR)
        path = journal(sessions, "team-big",
                       team_event(self.now - HOUR, short=100, week=50,
                                  short_reset=self.now + HOUR))
        with open(path, "a", encoding="utf-8") as fh:
            for _ in range(80):
                fh.write(json.dumps({"text": "x" * 4000}) + "\n")
        os.utime(path, (self.now - 20 * 60, self.now - 20 * 60))
        self.assertGreater(os.path.getsize(path), codex_gauges.TAIL_BYTES)

        result = self._evaluate(profile)

        self.assertFalse(result.ok)
        self.assertTrue(result.hard)
        self.assertIn("incomplete Codex account reading", result.reason)
        self.assertIn("short window", result.reason)

    def test_an_unreadable_journal_prevents_rest(self):
        # Le journal est dans l'échantillon mais sa lecture échoue : sa date
        # reste une borne dans la fenêtre courte, le repos n'est pas prouvé.
        sessions, profile = self._declared("team")
        journal(sessions, "team-rest",
                team_event(self.now - 11 * HOUR, short=6, week=6),
                mtime=self.now - 11 * HOUR)
        self._pro_sample(sessions)
        broken = journal(sessions, "illisible",
                         team_event(self.now - 5 * 60, short=100, week=50),
                         mtime=self.now - 5 * 60)

        real = codex_gauges.session_reading

        def reading(path, read_at):
            if path == broken:
                raise OSError("journal illisible")
            return real(path, read_at)

        with mock.patch.object(codex_gauges, "session_reading", side_effect=reading):
            result = self._evaluate(profile)

        self.assertFalse(result.ok)
        self.assertTrue(result.hard)
        self.assertIn("short window", result.reason)

    def test_a_journal_without_a_readable_mtime_prevents_rest(self):
        # Un journal que `recent_files` ne peut pas dater n'a pas de borne :
        # rien ne prouve qu'il est plus vieux qu'une fenêtre courte, le repos
        # est refusé même si le reste de l'échantillon est le cas #3044.
        sessions, profile = self._declared("team")
        journal(sessions, "team-rest",
                team_event(self.now - 11 * HOUR, short=6, week=6),
                mtime=self.now - 11 * HOUR)
        for i in range(codex_gauges.MAX_FILES - 1):
            journal(sessions, "pro-%d" % i,
                    event(self.now, short=None, week=13, plan="pro"),
                    mtime=self.now + i + 1)
        journal(sessions, "hors-echantillon",
                event(self.now - 30 * DAY, short=100, week=90),
                mtime=self.now - 30 * DAY)
        broken = journal(sessions, "sans-date",
                         team_event(self.now - 5 * 60, short=100, week=50),
                         mtime=self.now - 5 * 60)

        real_stat = os.stat

        def stat(path, *args, **kwargs):
            if os.fspath(path) == broken:
                raise OSError("date illisible")
            return real_stat(path, *args, **kwargs)

        with mock.patch.object(codex_gauges.os, "stat", side_effect=stat):
            result = self._evaluate(profile)

        self.assertFalse(result.ok)
        self.assertTrue(result.hard)
        self.assertIn("short window", result.reason)


if __name__ == "__main__":
    unittest.main()
