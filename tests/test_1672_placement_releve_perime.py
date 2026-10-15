# SPDX-License-Identifier: AGPL-3.0-only
"""#1672 — un relevé de jauge trop vieux ne place plus de session.

Cas réel (bureau/auditeur, 15/10/2026, compte Claude secondaire) : la bascule
automatique du 15/10 13:09:38Z a pris le secondaire — raison journalisée
« stale reading (window reset since the reading): unverified, last seven_day
30%, tried (no current reading) » — alors que le primaire et le tertiaire
étaient au seuil de rythme. Le relevé datait du 11/10 (quatre jours), la
fenêtre de 7 j courait encore (remise à zéro dans 9 h) et valait en réalité
100 % : le tour de l'agent est parti pour être refusé par la limite
hebdomadaire.

#1561 range le relevé périmé après tout relevé courant mais le laissait
« essayé, pas écarté » (L71). #1672 **borne** cet essai : le relevé n'est
écarté du placement (`rank`) que lorsque son âge dépasse ce qui reste de
**chaque** fenêtre encore ouverte — trop vieux pour engager un tour, par
**prudence sur une information ancienne**, pas parce qu'il prouverait un quota
atteint ; la remise à zéro qui motive la pause le rouvre (L71, 0 %). En deçà,
il reste plaçable après les relevés courants — témoin dev2 de la R1 : un
secondaire lu il y a 6 h, fenêtre 7 j à 4 j de sa remise à zéro, doit encore
recevoir la rotation. Les autres chemins sont inchangés et témoins ici :

* la **continuité** (une session déjà sur le compte y reste : c'est son tour
  qui rapporte la jauge) ;
* le **forçage manuel** (`ameesh accounts use`) ;
* l'**absence totale** de relevé (L117 : un compte jamais utilisé passe en
  premier) ;
* un relevé dont **toutes** les fenêtres datées sont échues reste courant
  (L71, 0 %) et replaçable ;
* un relevé **sans date** (`read_at` absent) n'est pas écarté : une date
  manquante ne prouve aucune péremption de plus.
"""
from __future__ import annotations

import os
import time
import unittest
from unittest import mock

from ameesh import accounts, cost, storage

from .test_l117_account_spread import _codex_weekly
from .test_l30_accounts import _Base

HOUR = 3600.0
DAY = 86400.0
WEEK = 7 * 86400.0


def _age_journal(home: str, when: float) -> None:
    """Vieillit le journal Codex écrit par `_codex_weekly` : le relevé est daté
    par la date du fichier (`cost.CostBook.codex_gauges`), donc `read_at`."""
    path = os.path.join(home, "sessions", "2026", "10", "10", "rollout-test.jsonl")
    os.utime(path, (when, when))


class LectureTropVieilleTest(unittest.TestCase):
    """`cost.reading_outdated` : la borne d'âge, ses frontières, ses cas
    d'exemption ; `rank` replace le relevé périmé après les relevés courants."""

    NOW = 1_800_000_000.0

    def _ev(self, name: str, gauges) -> accounts.Evaluation:
        return accounts.Evaluation(accounts.Profile("claude", name), gauges=gauges)

    def _courant(self, now: float) -> list:
        return [
            cost.Gauge("claude", "five_hour", 0.30, now + 2.5 * HOUR, 5 * HOUR),
            cost.Gauge("claude", "seven_day", 0.50, now + 0.5 * WEEK, WEEK),
        ]

    def _incident(self, now: float) -> list:
        """Le relevé de l'incident : 5 h remise à zéro depuis, 7 j ouverte à
        9 h de sa remise à zéro, relevé vieux de quatre jours."""
        return [
            cost.Gauge("claude", "five_hour", 0.78, now - HOUR, 5 * HOUR,
                       now - 4 * DAY),
            cost.Gauge("claude", "seven_day", 0.30, now + 9 * HOUR, WEEK,
                       now - 4 * DAY),
        ]

    def _temoin_dev2(self, now: float) -> list:
        """Le témoin de la R1 dev2 : secondaire lu il y a 6 h (fenêtre 5 h
        remise à zéro depuis), 7 j à 4 j de sa remise à zéro, 5 % utilisés."""
        return [
            cost.Gauge("claude", "five_hour", 0.05, now - HOUR, 5 * HOUR,
                       now - 6 * HOUR),
            cost.Gauge("claude", "seven_day", 0.05, now + 4 * DAY, WEEK,
                       now - 6 * HOUR),
        ]

    def test_incident_quatre_jours_contre_neuf_heures_ecarte(self):
        now = self.NOW
        perime = self._incident(now)
        self.assertTrue(cost.stale_reading(perime, now))
        self.assertTrue(cost.reading_outdated(perime, now))

    def test_temoin_dev2_six_heures_contre_quatre_jours_place(self):
        """La rotation normale survit : un relevé partiel de 6 h devant une
        fenêtre de 7 j qui court encore 4 j reste informatif."""
        now = self.NOW
        partiel = self._temoin_dev2(now)
        self.assertTrue(cost.stale_reading(partiel, now))
        self.assertFalse(cost.reading_outdated(partiel, now))

    def test_egalite_age_et_reste_ne_ecarte_pas(self):
        """Le bord est strict (`>`), pas `>=` : à âge égal au reste, le relevé
        n'est pas « plus vieux » — et la remise à zéro est imminente."""
        now = self.NOW
        pile = [
            cost.Gauge("claude", "five_hour", 0.10, now - HOUR, 5 * HOUR,
                       now - 6 * HOUR),
            cost.Gauge("claude", "seven_day", 0.30, now + 6 * HOUR, WEEK,
                       now - 6 * HOUR),
        ]
        self.assertTrue(cost.stale_reading(pile, now))
        self.assertFalse(cost.reading_outdated(pile, now))

    def test_plusieurs_fenetres_ouvertes_une_plus_longue_que_l_age_suffit(self):
        """La borne est « plus vieux que **chaque** fenêtre encore ouverte » :
        dès qu'une fenêtre ouverte reste plus longue que l'âge, le relevé garde
        du sens pour elle et le compte reste plaçable."""
        now = self.NOW
        une_longue = [
            cost.Gauge("claude", "five_hour", 0.90, now - HOUR, 5 * HOUR,
                       now - 6 * HOUR),          # échue
            cost.Gauge("claude", "seven_day", 0.30, now + 2 * HOUR, WEEK,
                       now - 6 * HOUR),          # reste 2 h < 6 h
            cost.Gauge("claude", "month", 0.20, now + 10 * HOUR, 30 * DAY,
                       now - 6 * HOUR),          # reste 10 h > 6 h
        ]
        self.assertTrue(cost.stale_reading(une_longue, now))
        self.assertFalse(cost.reading_outdated(une_longue, now))

        toutes_courtes = [
            cost.Gauge("claude", "five_hour", 0.90, now - HOUR, 5 * HOUR,
                       now - 6 * HOUR),
            cost.Gauge("claude", "seven_day", 0.30, now + 2 * HOUR, WEEK,
                       now - 6 * HOUR),
            cost.Gauge("claude", "month", 0.20, now + 4 * HOUR, 30 * DAY,
                       now - 6 * HOUR),
        ]
        self.assertTrue(cost.reading_outdated(toutes_courtes, now))

    def test_releve_sans_date_n_ecarte_pas(self):
        """`read_at` absent : une date manquante ne prouve aucune péremption de
        plus — le relevé partiel reste plaçable, au rang #1561."""
        now = self.NOW
        sans_date = [
            cost.Gauge("claude", "five_hour", 0.78, now - HOUR, 5 * HOUR),
            cost.Gauge("claude", "seven_day", 0.30, now + 9 * HOUR, WEEK),
        ]
        self.assertTrue(cost.stale_reading(sans_date, now))
        self.assertFalse(cost.reading_outdated(sans_date, now))

    def test_releve_courant_ou_toutes_echues_n_ecarte_pas(self):
        """Un relevé courant n'est pas « trop vieux » ; un relevé dont toutes
        les fenêtres datées sont échues est courant aussi (L71, 0 %)."""
        now = self.NOW
        courant = self._courant(now)
        self.assertFalse(cost.reading_outdated(courant, now))
        echu = [cost.Gauge("claude", "seven_day", 0.80, now - 60, WEEK,
                           now - 4 * DAY)]
        self.assertFalse(cost.stale_reading(echu, now))
        self.assertFalse(cost.reading_outdated(echu, now))

    def test_rank_garde_le_temoin_dev2_apres_le_courant(self):
        """#1561 restauré : le relevé partiel plaçable passe **après** tout
        relevé courant, et sa raison dit qu'il est essayé faute de courant."""
        now = self.NOW
        items = [accounts.Profile("claude", n) for n in ("primaire", "secondaire")]
        evaluations = {"primaire": self._ev("primaire", self._courant(now)),
                       "secondaire": self._ev("secondaire", self._temoin_dev2(now))}
        ranked = accounts.rank(items, evaluations, now)
        self.assertEqual([p.name for p in ranked], ["primaire", "secondaire"])
        self.assertEqual(
            accounts.reason_of(ranked, evaluations, now),
            "seven_day 50% used (pace 60%), furthest behind its pace; "
            "ahead of: secondaire stale reading (window reset since the "
            "reading): unverified, last seven_day 5%")

    def test_rank_ecarte_l_incident_et_garde_l_absence_premiere(self):
        """L'incident (4 j > 9 h) est écarté ; l'absence totale reste première
        (L117) et le relevé courant suit."""
        now = self.NOW
        items = [accounts.Profile("claude", n)
                 for n in ("primaire", "secondaire", "tertiaire")]
        evaluations = {"primaire": self._ev("primaire", self._incident(now)),
                       "secondaire": self._ev("secondaire", []),
                       "tertiaire": self._ev("tertiaire", self._courant(now))}
        ranked = accounts.rank(items, evaluations, now)
        self.assertEqual([p.name for p in ranked], ["secondaire", "tertiaire"])


class PlacementChoixTest(_Base):
    """Le choix automatique sur des journaux Codex réels, et ses frontières."""

    def setUp(self) -> None:
        super().setUp()
        reserve = mock.patch.object(accounts, "RESERVE", 0.0)
        reserve.start()
        self.addCleanup(reserve.stop)

    def _items(self):
        self.declared = self._codex_comptes()
        return accounts.parse({"codex": self.declared})["codex"]

    def _journaux_incident(self, items, now: float) -> None:
        """L'incident du 15/10 : primaire au seuil (courant), secondaire à
        30 % sur un relevé vieux de quatre jours dont la fenêtre de 5 h s'est
        remise à zéro, la 7 j à 9 h de sa remise à zéro."""
        _codex_weekly(items[0].path, 95, now + 0.5 * WEEK,
                      short=(30, now + 2.5 * HOUR))
        _codex_weekly(items[1].path, 30, now + 9 * HOUR, short=(78, now - HOUR))
        _age_journal(items[1].path, now - 4 * DAY)

    def test_le_choix_pause_au_lieu_de_prendre_un_releve_trop_vieux(self):
        items = self._items()
        now = float(int(time.time()))
        self._journaux_incident(items, now)
        book = cost.CostBook(state_dir=self.cfg.state_dir, db=self.db)

        choix = accounts.choose(self.db, self.cfg.host, "codex", items, book, now=now)
        self.assertIsNone(choix.profile)
        self.assertIn("no codex account to place", choix.reason)
        self.assertIn("reading older than every open window (secondaire)", choix.reason)
        # aucun placement : pas de bascule journalisée
        self.assertEqual(self._bascules(), [])

        # le rapport ne présente plus le compte écarté comme le prochain choix
        cfg = mock.Mock(host=self.cfg.host, accounts={"codex": self.declared})
        rows = accounts.report(cfg, self.db, book, now=now, record=False)
        perime = [r for r in rows if r["account"] == "secondaire"][0]
        self.assertTrue(perime["stale"])
        self.assertTrue(perime["ok"])          # L71 : le relevé ne bloque pas
        self.assertFalse(perime["next"])       # mais il ne place plus

    def test_la_rotation_place_un_partiel_dans_la_borne(self):
        """Témoin dev2 de la R1 : le primaire est au seuil, le secondaire a un
        relevé partiel **dans la borne** (lu il y a 6 h, 7 j à 4 j de sa remise
        à zéro) — la rotation doit le placer, pas pauser."""
        items = self._items()
        now = float(int(time.time()))
        _codex_weekly(items[0].path, 95, now + 0.5 * WEEK,
                      short=(30, now + 2.5 * HOUR))
        _codex_weekly(items[1].path, 5, now + 4 * DAY, short=(5, now - HOUR))
        _age_journal(items[1].path, now - 6 * HOUR)
        book = cost.CostBook(state_dir=self.cfg.state_dir, db=self.db)

        choix = accounts.choose(self.db, self.cfg.host, "codex", items, book, now=now)
        self.assertEqual(choix.profile.name, "secondaire")
        self.assertIn("tried (no current reading)", choix.why)

    def test_la_continuite_garde_le_compte_a_releve_perime(self):
        """Une session déjà sur le compte y reste : son tour rapporte la
        jauge, c'est le chemin de rafraîchissement du relevé."""
        items = self._items()
        now = float(int(time.time()))
        self._journaux_incident(items, now)
        book = cost.CostBook(state_dir=self.cfg.state_dir, db=self.db)

        choix = accounts.choose(self.db, self.cfg.host, "codex", items, book, now=now,
                                session_account="secondaire")
        self.assertEqual(choix.profile.name, "secondaire")
        self.assertTrue(choix.kept)

    def test_le_forcage_manuel_place_encore_un_releve_perime(self):
        items = self._items()
        now = float(int(time.time()))
        self._journaux_incident(items, now)
        book = cost.CostBook(state_dir=self.cfg.state_dir, db=self.db)
        self.assertTrue(accounts.force(self.db, self.cfg.host, "codex", items,
                                       "secondaire", by="test"))

        choix = accounts.choose(self.db, self.cfg.host, "codex", items, book, now=now)
        self.assertEqual(choix.profile.name, "secondaire")
        self.assertTrue(choix.forced)

        # rendu à l'automate, le compte trop vieux n'est plus choisi
        storage.of(self.db).accounts.set_auto(self.cfg.host, "codex", reason="test")
        choix = accounts.choose(self.db, self.cfg.host, "codex", items, book, now=now)
        self.assertIsNone(choix.profile)

    def test_un_releve_trop_vieux_ne_leve_pas_un_forcage_manuel(self):
        """Un forçage au seuil n'est levé que pour un remplaçant réellement
        plaçable : un compte à relevé trop vieux ne le remplace pas par une
        pause (#1672), le forçage reste posé."""
        items = self._items()
        now = float(int(time.time()))
        self._journaux_incident(items, now)
        book = cost.CostBook(state_dir=self.cfg.state_dir, db=self.db)
        self.assertTrue(accounts.force(self.db, self.cfg.host, "codex", items,
                                       "primaire", by="test"))
        avant = self._bascules()                 # la pose du forçage (manuel)

        choix = accounts.choose(self.db, self.cfg.host, "codex", items, book, now=now)
        self.assertIsNone(choix.profile)         # primaire au seuil : pause
        self.assertTrue(choix.forced)            # mais le forçage tient
        self.assertEqual(self._bascules(), avant)  # aucune levée journalisée
        row = storage.of(self.db).accounts.active(self.cfg.host, "codex")
        self.assertTrue(row.get("forced"))


if __name__ == "__main__":
    unittest.main()
