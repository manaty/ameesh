# SPDX-License-Identifier: AGPL-3.0-only
"""#3586 — une lecture antérieure au reset de la fenêtre ne condamne plus le compte.

Constat (bureau #140037, vérifié par l'auditeur sur agents-2) : le compte
claude/secondaire n'est jamais choisi. Sa dernière lecture (2 j 16 h) précède
la remise à zéro de sa fenêtre 5 h ; `accounts.list` le classe derrière
claude/primaire avec « stale reading (window reset since the reading):
unverified ». Un compte écarté n'ouvre aucune session, donc n'est jamais relu,
donc reste écarté : famine permanente du secondaire, capacité perdue.

Attendu : quand la fenêtre remise à zéro est vide (0 % partout à `now`), le
relevé ancien ne prouve aucun budget entamé ; le compte s'essaie en premier et
le premier tour relit la jauge (sonde), comme pour un relevé absent (L117).
Un compte réellement au seuil reste écarté, `auth_blocked` et les plafonds durs
restent intacts ; jamais d'overage. L'incident #1672 (relevé ancien sur une
fenêtre encore ouverte avec un usage non nul) reste écarté par
`reading_outdated`.

Témoins : secondaire vide choisi avant le primaire courant (puis relu) ;
lecture après reset au-dessus du seuil écartée ; `auth_blocked` toujours
écarté ; l'incident #1672 et le témoin dev2 #1672 inchangés.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from ameesh import accounts, cost

HOUR = 3600.0
DAY = 86400.0
WEEK = 7 * DAY
NOW = 1_800_000_000.0


def _eval(name: str, gauges, *, priority: int | None = None) -> accounts.Evaluation:
    profile = accounts.Profile("claude", name, priority=priority)
    result = accounts.Evaluation(profile, gauges=list(gauges))
    result.limits = {g.key: (0.90, "pace cap") for g in gauges}
    return result


def _empty(now: float) -> list:
    """Le secondaire réel : relevé d'il y a 2 j 16 h, 5 h remise à zéro
    depuis, 0 % partout (5 h et 7 j)."""
    old = now - 2 * DAY - 16 * HOUR
    return [
        cost.Gauge("claude", "five_hour", 0.0, now - HOUR, 5 * HOUR, old),
        cost.Gauge("claude", "seven_day", 0.0, now + 3 * DAY + 22 * HOUR, WEEK, old),
    ]


def _current(now: float) -> list:
    return [
        cost.Gauge("claude", "five_hour", 0.21, now + HOUR + 44 * 60, 5 * HOUR, now),
        cost.Gauge("claude", "seven_day", 0.36, now + 2 * DAY + 19 * HOUR, WEEK, now),
    ]


def _incident_1672(now: float) -> list:
    """Le relevé de l'incident #1672 : 5 h remise à zéro depuis, 7 j ouverte
    à 9 h de sa remise à zéro, 30 % utilisés, relevé vieux de quatre jours."""
    old = now - 4 * DAY
    return [
        cost.Gauge("claude", "five_hour", 0.78, now - HOUR, 5 * HOUR, old),
        cost.Gauge("claude", "seven_day", 0.30, now + 9 * HOUR, WEEK, old),
    ]


def _temoin_dev2(now: float) -> list:
    """#1672 R1 dev2 : secondaire lu il y a 6 h, 5 h remise à zéro depuis,
    7 j à 4 j de sa remise à zéro, 5 % utilisés — reste plaçable."""
    old = now - 6 * HOUR
    return [
        cost.Gauge("claude", "five_hour", 0.05, now - HOUR, 5 * HOUR, old),
        cost.Gauge("claude", "seven_day", 0.05, now + 4 * DAY, WEEK, old),
    ]


class FamineTest(unittest.TestCase):
    """Le rang : une fenêtre remise à zéro et vide s'essaie en premier."""

    def _ranked(self, now: float):
        secondaire = _eval("secondaire", _empty(now), priority=20)
        primaire = _eval("primaire", _current(now), priority=100)
        evaluations = {"secondaire": secondaire, "primaire": primaire}
        items = [accounts.Profile("claude", "primaire", priority=100),
                 accounts.Profile("claude", "secondaire", priority=20)]
        return accounts.rank(items, evaluations, now), evaluations

    def test_le_secondaire_vide_passe_avant_le_primaire_courant(self):
        ranked, evaluations = self._ranked(NOW)
        self.assertEqual([p.name for p in ranked], ["secondaire", "primaire"])
        why = accounts.reason_of(ranked, evaluations, NOW)
        self.assertIn("0% used, tried first", why)
        self.assertIn("stale reading (window reset since the reading)", why)
        # La sonde du premier tour relit la jauge : la lecture reste périmée
        # (ce n'est pas une mesure courante), c'est le choix qui la vérifie.
        self.assertTrue(cost.stale_reading(evaluations["secondaire"].gauges, NOW))

    def test_deux_comptes_vides_l_ordre_declare_decide(self):
        # 20 (secondaire) avant 100 (primaire), comme la décision #135688 a.
        now = NOW
        late = [cost.Gauge("claude", "five_hour", 0.0, now - HOUR, 5 * HOUR, now - 3 * DAY),
                cost.Gauge("claude", "seven_day", 0.0, now + 4 * DAY, WEEK, now - 3 * DAY)]
        early = [cost.Gauge("claude", "five_hour", 0.0, now - HOUR, 5 * HOUR, now - 3 * DAY),
                 cost.Gauge("claude", "seven_day", 0.0, now + 5 * DAY, WEEK, now - 3 * DAY)]
        evaluations = {"secondaire": _eval("secondaire", late, priority=20),
                       "tertiaire": _eval("tertiaire", early, priority=100)}
        items = [accounts.Profile("claude", "tertiaire", priority=100),
                 accounts.Profile("claude", "secondaire", priority=20)]
        ranked = accounts.rank(items, evaluations, now)
        self.assertEqual([p.name for p in ranked], ["secondaire", "tertiaire"])
        self.assertIn("first in the failover order (priority 20)",
                      accounts.reason_of(ranked, evaluations, now))

    def test_releve_vide_apres_reset_explique_zero(self):
        evaluation = _eval("secondaire", _empty(NOW))
        self.assertEqual(
            accounts.explain(evaluation, NOW),
            "stale reading (window reset since the reading): 0% used, tried first")

    def test_releve_partiel_non_nul_garde_son_rang_1566(self):
        # Régression : le témoin dev2 #1672 (5 h remise à zéro depuis, 7 j
        # ouverte avec 5 % utilisés) n'est pas vide ; il reste derrière un
        # relevé courant (#1561).
        now = NOW
        items = [accounts.Profile("claude", "primaire"),
                 accounts.Profile("claude", "secondaire")]
        evaluations = {"primaire": _eval("primaire", _current(now)),
                       "secondaire": _eval("secondaire", _temoin_dev2(now))}
        self.assertEqual([p.name for p in accounts.rank(items, evaluations, now)],
                         ["primaire", "secondaire"])

    def test_incident_1672_reste_ecarte(self):
        # La fenêtre encore ouverte porte 30 % et le relevé est plus vieux
        # que ce qui lui reste : la prudence #1672 reste entière.
        now = NOW
        incident = _incident_1672(now)
        self.assertTrue(cost.reading_outdated(incident, now))
        items = [accounts.Profile("claude", "secondaire")]
        evaluations = {"secondaire": _eval("secondaire", incident)}
        self.assertEqual(accounts.rank(items, evaluations, now), [])


class SeuilEtAuthTest(unittest.TestCase):
    """Les règles absolues ne bougent pas : seuil et auth_blocked écartent."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-3586-")
        os.chmod(self.tmp, 0o700)
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)

    def _profile(self, name="secondaire", priority=20):
        return accounts.Profile("claude", name, path=self.tmp,
                                check_credentials=False, priority=priority)

    def test_lecture_apres_reset_au_dessus_du_seuil_reste_ecartee(self):
        # Une lecture courante (après le reset) à 95 % dépasse le plafond
        # effectif : le compte est exclu, la fenêtre vide ne le réhabilite pas.
        now = NOW
        gauges = cost.Gauges([
            cost.Gauge("claude", "seven_day", 0.95, now + WEEK, WEEK, now),
        ])
        book = mock.Mock(db=None)
        with mock.patch.object(accounts, "gauges_of", return_value=gauges):
            result = accounts.evaluate(book, self._profile(), [self._profile()],
                                       now, record=False)
        self.assertFalse(result.ok, result.reason)
        items = [self._profile()]
        self.assertEqual(accounts.rank(items, {"secondaire": result}, now), [])

    def test_auth_blocked_reste_hors_des_candidats(self):
        items = [accounts.Profile("claude", "primaire"),
                 accounts.Profile("claude", "secondaire")]
        self.assertEqual(
            [p.name for p in accounts.healthy(items, {"secondaire": {"reason": "auth"}})],
            ["primaire"])


if __name__ == "__main__":
    unittest.main()
