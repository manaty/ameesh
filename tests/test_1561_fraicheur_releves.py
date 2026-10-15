# SPDX-License-Identifier: AGPL-3.0-only
"""#1561 — un relevé de jauge périmé n'est plus pris pour courant.

Cas réel (bureau, 15/10/2026, compte Claude secondaire) : jauge 7 j à 92 %
le 14/10 à 23:03, relue à 30 % le 15/10 de 06:45 à 08:15 (« five_hour 0 %,
reset passed, last reading 78 % »), puis 93 % à 08:25 — la fenêtre de 7 jours
n'avait pas été remise à zéro. Le relevé à 30 % était un `rate_limit_event`
ancien dont la fenêtre de 5 h s'était remise à zéro depuis : le choix
automatique l'a pris pour une mesure courante et a envoyé la session sur un
compte en réalité au seuil.

Règle (#1561, option (a) du pilote) : un relevé **partiel** — une fenêtre
datée remise à zéro alors qu'une autre court encore, ou un relevé non daté
plus vieux que sa plus courte fenêtre — est **non vérifié** : il ne passe
jamais avant un compte à relevé courant (`rank`), sa raison le dit au lieu
d'annoncer « 30 % utilisé » (`explain`), et il n'annonce ni perte ni échéance
(`_forecast`). Il reste utilisable : sans compte vérifié il est essayé
(L71, « essayé, pas écarté »), et son premier tour rapporte sa jauge.

Un compte **sans aucun relevé** garde la décision owner L117 (amendement 0034
du 2026-10-10) : il compte pour 0 % et passe en premier, pour ne pas laisser
un forfait payé inutilisé — mais sa raison cesse de prétendre à un 0 % mesuré
(« no reading: unverified »). Absence totale et relevé partiel restent deux
témoins distincts.
"""
from __future__ import annotations

import json
import os
import time
import unittest
from unittest import mock

from ameesh import accounts, cost

from .test_l117_account_spread import _codex_weekly
from .test_l30_accounts import _Base, _claude_rate_limit, _codex_rollout

HOUR = 3600.0
WEEK = 7 * 86400.0


class FraicheurTest(unittest.TestCase):
    """`cost.stale_reading`, la raison et le classement, sur des jauges
    construites — le scénario de l'incident, sans journal ni base."""

    NOW = 1_800_000_000.0

    def _ev(self, name: str, gauges) -> accounts.Evaluation:
        return accounts.Evaluation(accounts.Profile("claude", name), gauges=gauges)

    def _courant(self, now: float) -> list:
        """Un relevé courant : 5 h à 30 % et 7 j à 50 %, toutes deux à mi-
        fenêtre (rythme 60 %) — la plus contraignante est la semaine."""
        return [
            cost.Gauge("claude", "five_hour", 0.30, now + 2.5 * HOUR, 5 * HOUR),
            cost.Gauge("claude", "seven_day", 0.50, now + 0.5 * WEEK, WEEK),
        ]

    def _perime(self, now: float) -> list:
        """Le relevé de l'incident : la fenêtre de 5 h est remise à zéro
        depuis, celle de 7 j court encore à 30 % (reset dans 9 h)."""
        return [
            cost.Gauge("claude", "five_hour", 0.78, now - HOUR, 5 * HOUR),
            cost.Gauge("claude", "seven_day", 0.30, now + 9 * HOUR, WEEK),
        ]

    def test_incident_fenetre_7j_non_remise_a_zero(self):
        """Le relevé partiel est non vérifié et ne passe plus avant un relevé
        courant — alors même que son rythme lu (30 %) est plus favorable."""
        now = self.NOW
        perime, courant = self._perime(now), self._courant(now)
        self.assertTrue(cost.stale_reading(perime, now))
        self.assertFalse(cost.stale_reading(courant, now))
        # Le piège d'origine : au rythme seul, le relevé périmé gagne.
        self.assertLess(accounts.lag(perime, now)[0], accounts.lag(courant, now)[0])
        self.assertEqual(
            accounts.explain(self._ev("secondaire", perime), now),
            "stale reading (window reset since the reading): unverified, "
            "last seven_day 30%")

        items = [accounts.Profile("claude", n) for n in ("primaire", "secondaire")]
        evaluations = {"primaire": self._ev("primaire", courant),
                       "secondaire": self._ev("secondaire", perime)}
        ranked = accounts.rank(items, evaluations, now)
        self.assertEqual([p.name for p in ranked], ["primaire", "secondaire"])
        self.assertEqual(
            accounts.reason_of(ranked, evaluations, now),
            "seven_day 50% used (pace 60%), furthest behind its pace; "
            "ahead of: secondaire stale reading (window reset since the "
            "reading): unverified, last seven_day 30%")

    def test_absence_de_releve_reste_premier_l117(self):
        """Décision L117 : un compte jamais utilisé compte pour 0 % et passe
        en premier ; la raison dit « non vérifié », pas « 0 % mesuré »."""
        now = self.NOW
        items = [accounts.Profile("claude", n) for n in ("primaire", "secondaire")]
        evaluations = {"primaire": self._ev("primaire", self._courant(now)),
                       "secondaire": self._ev("secondaire", [])}
        self.assertEqual(accounts.explain(evaluations["secondaire"], now),
                         "no reading: unverified")
        ranked = accounts.rank(items, evaluations, now)
        self.assertEqual([p.name for p in ranked], ["secondaire", "primaire"])
        self.assertEqual(
            accounts.reason_of(ranked, evaluations, now),
            "no reading: unverified, tried first (L117); ahead of: primaire "
            "seven_day 50% used (pace 60%)")

    def test_releve_perime_seul_est_essaye_faute_de_mieux(self):
        """Sans compte à relevé courant, le compte périmé est essayé (L71) :
        la raison ne prétend pas l'avoir choisi sur son rythme."""
        now = self.NOW
        items = [accounts.Profile("claude", "secondaire")]
        evaluations = {"secondaire": self._ev("secondaire", self._perime(now))}
        ranked = accounts.rank(items, evaluations, now)
        self.assertEqual([p.name for p in ranked], ["secondaire"])
        self.assertEqual(
            accounts.reason_of(ranked, evaluations, now),
            "stale reading (window reset since the reading): unverified, "
            "last seven_day 30%, tried (no current reading)")

    def test_fenetres_toutes_remises_a_zero_comptent_pour_zero(self):
        """L71 inchangé : toutes les fenêtres datées sont échues, la suivante
        est vierge — 0 %, pas « périmé »."""
        now = self.NOW
        echu = [cost.Gauge("claude", "seven_day", 0.80, now - 60, WEEK)]
        self.assertFalse(cost.stale_reading(echu, now))
        self.assertEqual(accounts.lag(echu, now)[0], 0.0)
        self.assertEqual(accounts.explain(self._ev("x", echu), now),
                         "stale reading (window reset): 0% used")

    def test_releve_non_date_perime_par_l_age(self):
        """Une source qui ne date pas ses fenêtres (`resets_at` absent) est
        périmée quand le relevé dépasse sa plus courte fenêtre."""
        now = self.NOW
        vieux = [cost.Gauge("claude", "five_hour", 0.30, None, 5 * HOUR, now - 6 * HOUR)]
        recent = [cost.Gauge("claude", "five_hour", 0.30, None, 5 * HOUR, now - HOUR)]
        self.assertTrue(cost.stale_reading(vieux, now))
        self.assertFalse(cost.stale_reading(recent, now))
        self.assertIn("stale reading (window reset since the reading): unverified",
                      accounts.explain(self._ev("x", vieux), now))


class FraicheurSourcesTest(unittest.TestCase):
    """`read_at` : le relevé porte l'horodatage de sa source."""

    def setUp(self) -> None:
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="ameesh-l1561-")

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_read_at_claude_vient_du_marqueur_de_compte(self):
        """Le relevé d'un flux marqué est daté par le marqueur qui le précède
        (le tour qui l'a produit), pas par la date du fichier."""
        etat = os.path.join(self.tmp, "etat")
        os.makedirs(os.path.join(etat, "ag"))
        with open(os.path.join(etat, "ag", "tool"), "w", encoding="utf-8") as fh:
            fh.write("claude\n")
        now = time.time()
        with open(os.path.join(etat, "ag", "events.jsonl"), "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": cost.ACCOUNT_MARKER, "harness": "claude",
                                 "account": "secondaire", "ts": now - 10}) + "\n")
            fh.write(json.dumps(_claude_rate_limit(0.30, now + 3600)) + "\n")
        book = cost.CostBook(state_dir=etat, db=None, codex_sessions=self.tmp)
        gauges = book.claude_gauges("secondaire", "primaire")
        self.assertEqual(len(gauges), 1)
        self.assertEqual(gauges[0].read_at, now - 10)

    def test_read_at_codex_vient_de_la_date_du_journal(self):
        """Un relevé Codex est daté par la date de son journal de session."""
        home = os.path.join(self.tmp, "codex")
        now = time.time()
        _codex_rollout(home, 95, now + 3600)
        path = os.path.join(home, "sessions", "2026", "10", "05", "rollout-test.jsonl")
        book = cost.CostBook(state_dir=self.tmp, db=None, codex_sessions=self.tmp)
        gauges = book.codex_gauges(sessions=os.path.join(home, "sessions"))
        self.assertTrue(gauges)
        self.assertEqual(gauges[0].read_at, os.path.getmtime(path))


class FraicheurChoixTest(_Base):
    """Le choix automatique et le rapport, sur des journaux Codex réels."""

    def setUp(self) -> None:
        super().setUp()
        # ces tests portent sur le classement : la réserve des non-critiques
        # a les siens (test_l242b2_budget)
        reserve = mock.patch.object(accounts, "RESERVE", 0.0)
        reserve.start()
        self.addCleanup(reserve.stop)

    def _items(self):
        self.declared = self._codex_comptes()
        return accounts.parse({"codex": self.declared})["codex"]

    def test_le_choix_ne_suit_plus_un_releve_perime(self):
        """Le cas de l'incident, de bout en bout : le secondaire à 30 % avec
        la fenêtre de 5 h remise à zéro ne prend plus la session ; son rapport
        ne promet ni perte ni échéance, et il reste utilisable."""
        items = self._items()
        now = float(int(time.time()))
        _codex_weekly(items[0].path, 50, now + 0.5 * WEEK,
                      short=(30, now + 2.5 * HOUR))
        _codex_weekly(items[1].path, 30, now + 9 * HOUR, short=(78, now - HOUR))
        book = cost.CostBook(state_dir=self.cfg.state_dir, db=self.db)

        choice = accounts.choose(self.db, self.cfg.host, "codex", items, book, now=now)
        self.assertEqual(choice.profile.name, "primaire")
        self.assertIn("ahead of: secondaire stale reading", choice.why)

        cfg = mock.Mock(host=self.cfg.host, accounts={"codex": self.declared})
        rows = accounts.report(cfg, self.db, book, now=now, record=False)
        perime = [r for r in rows if r["account"] == "secondaire"][0]
        self.assertTrue(perime["stale"])
        self.assertTrue(perime["ok"])            # L71 : essayé, pas écarté
        self.assertEqual(perime["losses"], [])   # rien n'est « perdu » sur un
        self.assertIsNone(perime["expires_in_s"])  # relevé qui n'est qu'une borne
        self.assertTrue(all(g["read_at"] for g in perime["gauges"]))
        courant = [r for r in rows if r["account"] == "primaire"][0]
        self.assertFalse(courant["stale"])
        self.assertTrue(courant["losses"])
        # l'affichage ne présente plus la valeur périmée comme courante
        texte = accounts.format_rows(rows)
        self.assertEqual(
            texte.count("reading not current: window reset since the reading"), 1)


if __name__ == "__main__":
    unittest.main()
