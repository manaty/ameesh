# SPDX-License-Identifier: AGPL-3.0-only
"""#3044 — un compte Codex team au repos reste utilisable (échantillon tronqué).

#2843 exige une fenêtre courte **vivante** dans un échantillon tronqué pour un
forfait non-pro. Un compte laissé sans tour plus longtemps que sa fenêtre
courte n'en produit jamais : sa dernière courte retenue est échue, tandis que
la semaine reste ouverte. Le refus bloque alors le compte pour toujours — il
n'est jamais choisi, donc jamais rafraîchi.

La garde admet ce relevé seulement quand une courte a été observée (même
échue) et que l'observation la plus récente de l'échantillon (après filtrage
du forfait vivant) est plus vieille que la fenêtre courte : aucun relevé de
l'échantillon n'a pu voir une courte vivante. Un relevé récent sans courte
vivante, ou un échantillon sans aucune courte, garde le refus #2843 (une
courte saturée peut se cacher hors de l'échantillon borné).
"""
from __future__ import annotations

import base64
import json
import os
import unittest

from ameesh import accounts, codex_gauges

from .test_cost import Sandbox
from .test_2839_codex_account_gauges import event, journal

SHORT = 5 * 3600.0
DAY = 86400.0


def declared_codex(root, plan):
    """Un compte Codex déclaré : auth.json avec le forfait vivant (JWT)."""
    os.makedirs(root, exist_ok=True)
    os.chmod(root, 0o700)
    node = {}
    if plan is not None:
        claims = {"https://api.openai.com/auth": {"chatgpt_plan_type": plan}}
        payload = base64.urlsafe_b64encode(
            json.dumps(claims).encode()).rstrip(b"=").decode()
        node = {"tokens": {"id_token": "e30.%s.sig" % payload,
                           "refresh_token": "secret-refresh"}}
    path = os.path.join(root, "auth.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(node, fh)
    os.chmod(path, 0o600)
    return root


def team_event(observed, *, short=5, week=11, short_reset=None):
    """Un relevé team réel : courte de 5 h, semaine à 4 jours de sa remise."""
    node = event(observed, short=short, week=week)
    limits = node["payload"]["rate_limits"]
    limits["primary"]["resets_at"] = (observed + SHORT if short_reset is None
                                      else short_reset)
    limits["secondary"]["resets_at"] = observed + 4 * DAY
    return node


def team_partial(observed, *, week=11):
    """Un relevé team partiel : la semaine seule (courte absente)."""
    node = event(observed, short=None, week=week)
    node["payload"]["rate_limits"]["secondary"]["resets_at"] = observed + 4 * DAY
    return node


class AtRestTest(Sandbox, unittest.TestCase):
    def _declared(self, plan="team"):
        home = os.path.join(self.tmp_cost, "compte-codex")
        sessions = os.path.join(home, "sessions")
        declared_codex(home, plan)
        profile = accounts.parse(
            {"codex": [{"name": "secondaire", "path": home}]})["codex"][0]
        return sessions, profile

    def _truncate_with_pro(self, sessions, count=codex_gauges.MAX_FILES - 1):
        """Rend l'échantillon tronqué : `count` journaux pro sains plus récents.

        Le relevé team, plus ancien, reste dans les 32 plus récents ; le pro
        vivant est ignoré (#3034), donc le relevé utile est bien le team au
        repos.
        """
        for i in range(count):
            journal(sessions, "pro-%d" % i,
                    event(self.now, short=None, week=13, plan="pro"),
                    mtime=self.now + i + 1)
        journal(sessions, "hors-echantillon",
                event(self.now - 30 * DAY, short=100, week=90),
                mtime=self.now - 30 * DAY)

    def _evaluate(self, profile):
        return accounts.evaluate(self.book(), profile, [profile], self.now,
                                 record=False)

    def test_a_team_account_at_rest_in_a_truncated_sample_is_usable(self):
        # Cas réel : dernier relevé team ~11 h avant, courte remise à zéro
        # depuis ~6 h, semaine encore ouverte. Le pro, plus récent et sain,
        # est ignoré (#3034) ; le team au repos doit rester choisissable.
        sessions, profile = self._declared("team")
        journal(sessions, "team-rest",
                team_event(self.now - 11 * 3600, short=6, week=6),
                mtime=self.now - 11 * 3600)
        self._truncate_with_pro(sessions)

        result = self._evaluate(profile)

        self.assertTrue(result.ok, result.reason)
        self.assertFalse(result.hard)
        self.assertEqual({g.key: g.used for g in result.gauges},
                         {"codex-300min": 0.06, "codex-10080min": 0.06})
        self.assertEqual(accounts.rank([profile], {"secondaire": result}, self.now),
                         [profile])

    def test_an_at_rest_sample_without_any_short_is_still_refused(self):
        # Sans aucune courte observée, le format n'est pas prouvé : la garde
        # #2843 reste, même si la semaine est vieille. La courte échue doit
        # être dans l'échantillon pour que le repos lève le refus.
        sessions, profile = self._declared("team")
        journal(sessions, "team-week", team_partial(self.now - 11 * 3600),
                mtime=self.now - 11 * 3600)
        self._truncate_with_pro(sessions)

        result = self._evaluate(profile)

        self.assertFalse(result.ok)
        self.assertTrue(result.hard)
        self.assertIn("incomplete Codex account reading", result.reason)
        self.assertIn("short window", result.reason)

    def test_a_recent_partial_reading_still_refuses_an_expired_short(self):
        # Un relevé récent sans courte vivante peut cacher une courte saturée
        # hors de l'échantillon : le refus #2843 reste.
        sessions, profile = self._declared("team")
        journal(sessions, "team-short",
                team_event(self.now - 11 * 3600),
                mtime=self.now - 11 * 3600)
        journal(sessions, "team-recent", team_partial(self.now - 3600),
                mtime=self.now - 3600)
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
        self.assertIn("incomplete Codex account reading", result.reason)
        self.assertIn("short window", result.reason)

    def test_a_reading_younger_than_the_short_window_still_refuses(self):
        # Borne : à 4 h 59 du dernier relevé, une courte vivante a encore pu
        # être observée ; le repos n'est prouvé qu'après la fenêtre courte.
        sessions, profile = self._declared("team")
        journal(sessions, "team-short",
                team_event(self.now - 11 * 3600),
                mtime=self.now - 11 * 3600)
        journal(sessions, "team-recent", team_partial(self.now - SHORT + 60),
                mtime=self.now - SHORT + 60)
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
        self.assertIn("incomplete Codex account reading", result.reason)

    def test_the_at_rest_relaxation_does_not_accept_a_saturated_short(self):
        # Une courte vivante saturée n'est pas « au repos » : elle reste vue,
        # et le compte est refusé en overage au lieu d'être lavé par la garde.
        sessions, profile = self._declared("team")
        journal(sessions, "team-short", team_event(self.now - 11 * 3600),
                mtime=self.now - 11 * 3600)
        journal(sessions, "team-saturated",
                team_event(self.now - 3600, short=100,
                           short_reset=self.now + 3600),
                mtime=self.now - 3600)
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
        self.assertIn("overage", result.reason)


if __name__ == "__main__":
    unittest.main()
