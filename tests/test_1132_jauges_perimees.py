# SPDX-License-Identifier: AGPL-3.0-only
"""#1132 — un relevé périmé ne remplace plus la lecture fraîche.

Témoins horodatés, sur journaux factices et sur la base jetable :

* 14 → 10 par mtime : un flux SANS marqueur (legacy) est non daté ; le mtime du
  fichier ne le fait pas battre une mesure datée d'un flux marqué ;
* 87 → 67 par marqueur récent : une mesure dont une fenêtre était déjà close
  quand le tour a commencé est un rejeu du harnais — elle ne remplace pas la
  mesure fraîche, même publiée sous un marqueur récent ;
* historique : un relevé périmé réinséré (`observed_at` récent, `produced_at`
  ancien) ne devient pas « le dernier relevé » ; la provenance est écrite ;
* réconciliation : un exécuteur qui ne voit qu'un flux figé reprend la mesure
  plus fraîche publiée par un autre état, jamais l'inverse.

Et les témoins de non-blocage : une remise à zéro réelle (nouvelle fenêtre) et
un ajustement explicite plus récent (baisse dans la même fenêtre) sont acceptés ;
L71 (une fenêtre échue vaut 0 %) est préservé.
"""
from __future__ import annotations

import os
import unittest

from ameesh import cost, storage

from .support import PgTestCase
from .test_cost import Sandbox


def marked(account: str, ts: float) -> dict:
    return {"type": cost.ACCOUNT_MARKER, "harness": "claude", "account": account, "ts": ts}


def windows(**values) -> dict:
    """Un `rate_limit_event` : `cle=(utilisation, resetsAt)`."""
    return {"type": "rate_limit_event", "rate_limit_info": {"unifiedWindows": {
        key: {"utilization": used, "resetsAt": resets}
        for key, (used, resets) in values.items()}}}


def line(harness: str, key: str, used: float, resets_at: float, *,
         account: str, produced_at: float | None, source: str) -> dict:
    return {"harness": harness, "key": key, "used": used, "resets_at": resets_at,
            "window_s": cost.KNOWN_WINDOWS["seven_day"] if key == "seven_day"
            else cost.KNOWN_WINDOWS["five_hour"],
            "account": account, "produced_at": produced_at, "source": source}


class MtimeNeDatePasTest(Sandbox, unittest.TestCase):
    """14 → 10 : le mtime d'un flux non marqué ne date pas sa mesure."""

    def test_un_flux_sans_marqueur_ne_bat_pas_une_mesure_datee(self):
        self.agent("marque", "claude")
        self.agent("legacy", "claude")
        self.events("marque", marked("primaire", self.now - 10),
                    windows(five_hour=(0.14, self.now + 3600)))
        path = os.path.join(self.state, "legacy", "events.jsonl")
        self.events("legacy", windows(five_hour=(0.10, self.now - 86400)))
        # le flux legacy est le plus récemment écrit : c'est tout le piège
        os.utime(path, ns=(int((self.now + 100) * 1e9),) * 2)
        gauges = self.book(db=None).claude_gauges(account="primaire", primary="primaire")
        self.assertAlmostEqual(gauges[0].used, 0.14)
        self.assertEqual(gauges[0].source, "marque")
        self.assertAlmostEqual(gauges[0].produced_at, self.now - 10, places=0)

    def test_un_flux_non_marque_reste_non_date_et_utilisable(self):
        self.agent("legacy", "claude")
        self.events("legacy", windows(five_hour=(0.10, self.now + 3600)))
        gauges = self.book(db=None).claude_gauges(account="primaire", primary="primaire")
        self.assertAlmostEqual(gauges[0].used, 0.10)
        self.assertIsNone(gauges[0].produced_at)
        self.assertEqual(gauges[0].source, "legacy")


class RejeuMarqueurRecentTest(Sandbox, unittest.TestCase):
    """87 → 67 : un rejeu publié sous un marqueur récent ne gagne pas."""

    def _fige(self, agent: str, marker: float) -> None:
        # five_hour close depuis 25 h au moment du marqueur : le harnais rejoue
        # le relevé du 12/10 (75 %/67 %) sous le marqueur du 13/10.
        self.events(agent, marked("secondaire", marker),
                    windows(five_hour=(0.75, marker - 25 * 3600),
                            seven_day=(0.67, self.now + 3 * 86400)))

    def test_la_mesure_fraiche_gagne_meme_marqueur_recent(self):
        self.agent("fige", "claude")
        self.agent("frais", "claude")
        self._fige("fige", self.now - 5)
        self.events("frais", marked("secondaire", self.now - 3600),
                    windows(five_hour=(0.20, self.now + 2 * 3600),
                            seven_day=(0.87, self.now + 3 * 86400)))
        gauges = {g.key: g for g in
                  self.book(db=None).claude_gauges(account="secondaire", primary="primaire")}
        self.assertAlmostEqual(gauges["five_hour"].used, 0.20)
        self.assertAlmostEqual(gauges["seven_day"].used, 0.87)
        self.assertEqual(gauges["seven_day"].source, "frais")

    def test_un_rejeu_seul_reste_utilisable_et_non_date(self):
        self.agent("fige", "claude")
        self._fige("fige", self.now - 5)
        gauges = {g.key: g for g in
                  self.book(db=None).claude_gauges(account="secondaire", primary="primaire")}
        # le rejeu n'est pas daté : il ne pourra pas battre une mesure fraîche
        self.assertIsNone(gauges["seven_day"].produced_at)
        # …mais il reste lu (L71 : la five_hour échue vaut 0, le compte n'est
        # pas bloqué pour autant)
        self.assertEqual(gauges["five_hour"].used_at(self.now), 0.0)
        self.assertAlmostEqual(gauges["seven_day"].used_at(self.now), 0.67)

    def test_une_fenetre_close_juste_avant_le_tour_n_est_pas_un_rejeu(self):
        self.agent("a", "claude")
        self.events("a", marked("secondaire", self.now - 120),
                    windows(five_hour=(0.90, self.now - 180)))   # 60 s avant : toléré
        gauges = self.book(db=None).claude_gauges(account="secondaire", primary="primaire")
        self.assertIsNotNone(gauges[0].produced_at)

    def test_un_rejeu_posterieur_ne_masque_pas_la_mesure_fraiche_du_meme_journal(self):
        # B1 : la mesure fraîche d'abord, puis un rejeu sous un marqueur plus
        # récent — l'ordre de lecture ne doit pas faire gagner le rejeu.
        self.agent("mixte", "claude")
        self.events("mixte", marked("secondaire", self.now - 3600),
                    windows(five_hour=(0.20, self.now + 2 * 3600),
                            seven_day=(0.87, self.now + 3 * 86400)),
                    marked("secondaire", self.now - 600),
                    windows(five_hour=(0.75, self.now - 25 * 3600),
                            seven_day=(0.67, self.now + 3 * 86400)))
        gauges = {g.key: g for g in
                  self.book(db=None).claude_gauges(account="secondaire", primary="primaire")}
        self.assertAlmostEqual(gauges["five_hour"].used, 0.20)
        self.assertAlmostEqual(gauges["seven_day"].used, 0.87)
        self.assertAlmostEqual(gauges["seven_day"].produced_at, self.now - 3600, places=0)

    def test_sans_compte_un_rejeu_posterieur_ne_masque_pas_la_mesure_fraiche(self):
        self.agent("mixte", "claude")
        self.events("mixte", marked("secondaire", self.now - 3600),
                    windows(seven_day=(0.87, self.now + 3 * 86400)),
                    marked("secondaire", self.now - 600),
                    windows(five_hour=(0.75, self.now - 25 * 3600),
                            seven_day=(0.67, self.now + 3 * 86400)))
        gauges = {g.key: g for g in self.book(db=None).claude_gauges()}
        self.assertAlmostEqual(gauges["seven_day"].used, 0.87)
        self.assertAlmostEqual(gauges["seven_day"].produced_at, self.now - 3600, places=0)

    def test_a_production_egale_le_lecteur_garde_la_derniere_valeur(self):
        # Delta auditeur : plusieurs `rate_limit_event` du même tour partagent le
        # ts du marqueur ; à production égale, le lecteur garde le DERNIER.
        self.agent("mixte", "claude")
        self.events("mixte", marked("secondaire", self.now - 3600),
                    windows(seven_day=(0.87, self.now + 3 * 86400)),
                    windows(seven_day=(0.67, self.now + 3 * 86400)),
                    windows(seven_day=(0.87, self.now + 3 * 86400)))
        gauges = {g.key: g for g in
                  self.book(db=None).claude_gauges(account="secondaire", primary="primaire")}
        self.assertAlmostEqual(gauges["seven_day"].used, 0.87)
        self.assertAlmostEqual(gauges["seven_day"].produced_at, self.now - 3600, places=0)

    def test_sans_compte_les_drapeaux_overage_suivent_la_lecture(self):
        # N1 : la lecture sans compte rend un `Gauges`, drapeaux d'overage inclus.
        self.agent("a", "claude")
        event = windows(seven_day=(0.87, self.now + 3 * 86400))
        event["rate_limit_info"].update(overageStatus="allowed", isUsingOverage=True)
        self.events("a", marked("primaire", self.now - 10), event)
        gauges = self.book(db=None).claude_gauges()
        self.assertIsInstance(gauges, cost.Gauges)
        self.assertTrue(gauges.in_overage(self.now))


class _JaugesBase(Sandbox, PgTestCase):
    """Schéma jetable et table des jauges vide : chaque témoin part de zéro."""

    def setUp(self) -> None:
        super().setUp()
        self.db.execute("TRUNCATE quota_gauge_readings RESTART IDENTITY")


class HistoriqueProvenanceTest(_JaugesBase):
    """L'historique garde la provenance et classe par instant de production."""

    def test_un_releve_perime_reinsere_ne_devient_pas_le_dernier(self):
        ops = storage.of(self.db).operations
        fenetre = self.now + 3 * 86400
        ops.record_gauges([line("claude", "seven_day", 0.87, fenetre,
                                account="secondaire", produced_at=self.now - 3600,
                                source="ag-frais")])
        # réinséré plus tard (observed_at = maintenant) mais produit AVANT
        ops.record_gauges([line("claude", "seven_day", 0.67, fenetre,
                                account="secondaire", produced_at=self.now - 7200,
                                source="ag-fige")])
        latest = ops.gauge_latest(harness="claude", account="secondaire")
        self.assertEqual(len(latest), 1)
        self.assertAlmostEqual(latest[0]["used"], 0.87)
        self.assertEqual(latest[0]["source"], "ag-frais")
        self.assertAlmostEqual(latest[0]["produced_ts"], self.now - 3600, places=0)
        rows = ops.latest_gauges(since_s=3600)
        self.assertAlmostEqual(rows[0]["used"], 0.87)
        # l'historique complet garde les deux lignes, avec leur provenance
        hist = ops.gauge_history(since_s=3600, harness="claude", account="secondaire")
        self.assertEqual([r["source"] for r in hist], ["ag-frais", "ag-fige"])
        self.assertAlmostEqual(hist[1]["produced_ts"], self.now - 7200, places=0)

    def test_un_autre_compte_ne_donne_pas_sa_jauge(self):
        ops = storage.of(self.db).operations
        ops.record_gauges([line("claude", "seven_day", 0.87, self.now + 3 * 86400,
                                account="secondaire", produced_at=self.now - 60,
                                source="ag")])
        self.assertEqual(ops.gauge_latest(harness="claude", account="primaire"), [])
        self.assertEqual(ops.gauge_latest(harness="codex", account="secondaire"), [])

    def test_une_production_plus_recente_a_valeur_egale_est_gardee(self):
        # B2 : A(87 @ -7200), B(87 @ -3600) puis C(67 @ -5400). La dédup ne doit
        # pas perdre B (valeur égale) et laisser C devenir « le dernier ».
        ops = storage.of(self.db).operations
        fenetre = self.now + 3 * 86400
        for used, produced, source in ((0.87, self.now - 7200, "ag-a"),
                                       (0.87, self.now - 3600, "ag-b"),
                                       (0.67, self.now - 5400, "ag-c")):
            ops.record_gauges([line("claude", "seven_day", used, fenetre,
                                    account="secondaire", produced_at=produced,
                                    source=source)])
        latest = ops.gauge_latest(harness="claude", account="secondaire")
        self.assertEqual(len(latest), 1)
        self.assertAlmostEqual(latest[0]["used"], 0.87)
        self.assertAlmostEqual(latest[0]["produced_ts"], self.now - 3600, places=0)
        self.assertEqual(latest[0]["source"], "ag-b")

    def test_une_production_datee_remplace_une_ligne_non_datee(self):
        # B2 : une ligne non datée ne bloque pas l'écriture de la même mesure,
        # datée (NULL -> daté).
        ops = storage.of(self.db).operations
        fenetre = self.now + 3 * 86400
        ops.record_gauges([line("claude", "seven_day", 0.67, fenetre,
                                account="secondaire", produced_at=None,
                                source="ag-legacy")])
        ops.record_gauges([line("claude", "seven_day", 0.67, fenetre,
                                account="secondaire", produced_at=self.now - 60,
                                source="ag-date")])
        latest = ops.gauge_latest(harness="claude", account="secondaire")
        self.assertEqual(len(latest), 1)
        self.assertAlmostEqual(latest[0]["produced_ts"], self.now - 60, places=0)
        self.assertEqual(latest[0]["source"], "ag-date")

    def test_une_repetition_identique_ne_se_reinsere_pas(self):
        # B2 (suite) : la dédup reste bornée. Un rejeu identique répété n'écrit
        # pas une ligne de plus, même quand une production plus fraîche (autre
        # valeur) existe : la comparaison porte sur la production, jamais sur la
        # seule dernière insertion.
        ops = storage.of(self.db).operations
        fenetre = self.now + 3 * 86400
        ops.record_gauges([line("claude", "seven_day", 0.67, fenetre,
                                account="secondaire", produced_at=self.now - 3600,
                                source="ag-frais")])
        for _ in range(3):
            ops.record_gauges([line("claude", "seven_day", 0.87, fenetre,
                                    account="secondaire", produced_at=self.now - 7200,
                                    source="ag-fige")])
        hist = ops.gauge_history(since_s=3600, harness="claude", account="secondaire")
        self.assertEqual(sorted(r["source"] for r in hist), ["ag-fige", "ag-frais"])
        latest = ops.gauge_latest(harness="claude", account="secondaire")
        self.assertEqual(len(latest), 1)
        self.assertAlmostEqual(latest[0]["used"], 0.67)

    def test_une_oscillation_a_production_egale_garde_la_derniere_valeur(self):
        # Delta a66546be (auditeur) : un tour publie plusieurs `rate_limit_event`
        # sous le MÊME marqueur (`produced_at` égal). La dédup doit comparer à la
        # DERNIÈRE valeur de ce rang, pas à une ancienne valeur identique : sinon
        # 87 -> 67 -> 87 est refusé et `gauge_latest` garde 67 quand le journal
        # garde 87.
        ops = storage.of(self.db).operations
        P = self.now - 3600
        fenetre = self.now + 3 * 86400
        for used, source in ((0.87, "ag-1"), (0.67, "ag-2"), (0.87, "ag-3")):
            ops.record_gauges([line("claude", "seven_day", used, fenetre,
                                    account="secondaire", produced_at=P, source=source)])
        latest = ops.gauge_latest(harness="claude", account="secondaire")
        self.assertEqual(len(latest), 1)
        self.assertAlmostEqual(latest[0]["used"], 0.87)
        self.assertEqual(latest[0]["source"], "ag-3")
        self.assertAlmostEqual(latest[0]["produced_ts"], P, places=0)
        self.assertAlmostEqual(ops.latest_gauges(since_s=3600)[0]["used"], 0.87)
        # la répétition de la DERNIÈRE valeur de ce rang reste dédupliquée
        ops.record_gauges([line("claude", "seven_day", 0.87, fenetre,
                                account="secondaire", produced_at=P, source="ag-4")])
        hist = ops.gauge_history(since_s=3600, harness="claude", account="secondaire")
        self.assertEqual([r["source"] for r in hist], ["ag-1", "ag-2", "ag-3"])
        self.assertAlmostEqual(
            ops.gauge_latest(harness="claude", account="secondaire")[0]["used"], 0.87)

    def test_une_oscillation_non_datee_garde_la_derniere_valeur(self):
        # Même défaut sans provenance (Codex, flux sans marqueur) : 67 -> 87 -> 67.
        ops = storage.of(self.db).operations
        fenetre = self.now + 3 * 86400
        for used, source in ((0.67, "ag-1"), (0.87, "ag-2"), (0.67, "ag-3")):
            ops.record_gauges([line("claude", "seven_day", used, fenetre,
                                    account="secondaire", produced_at=None, source=source)])
        latest = ops.gauge_latest(harness="claude", account="secondaire")
        self.assertEqual(len(latest), 1)
        self.assertAlmostEqual(latest[0]["used"], 0.67)
        self.assertEqual(latest[0]["source"], "ag-3")
        self.assertIsNone(latest[0]["produced_ts"])


class ReconciliationTest(_JaugesBase):
    """Un état qui ne voit qu'un flux figé reprend la mesure plus fraîche."""

    def test_un_autre_etat_plus_frais_est_repris_fenetre_par_fenetre(self):
        self.agent("fige", "claude")
        self.events("fige", marked("secondaire", self.now - 90_000),
                    windows(five_hour=(0.75, self.now - 90_000 + 7200),
                            seven_day=(0.67, self.now + 3 * 86400)))
        ops = storage.of(self.db).operations
        ops.record_gauges([
            line("claude", "five_hour", 0.16, self.now + 2 * 3600,
                 account="secondaire", produced_at=self.now - 3600, source="ag-frais"),
            line("claude", "seven_day", 0.87, self.now + 3 * 86400,
                 account="secondaire", produced_at=self.now - 3600, source="ag-frais")])
        gauges = {g.key: g for g in
                  self.book().claude_gauges(account="secondaire", primary="primaire")}
        self.assertAlmostEqual(gauges["five_hour"].used, 0.16)
        self.assertAlmostEqual(gauges["seven_day"].used, 0.87)
        self.assertEqual(gauges["seven_day"].source, "ag-frais")

    def test_un_historique_plus_ancien_ne_remplace_pas_la_lecture_fraiche(self):
        self.agent("frais", "claude")
        self.events("frais", marked("secondaire", self.now - 60),
                    windows(seven_day=(0.87, self.now + 3 * 86400)))
        ops = storage.of(self.db).operations
        ops.record_gauges([line("claude", "seven_day", 0.67, self.now + 3 * 86400,
                                account="secondaire", produced_at=self.now - 7200,
                                source="ag-fige")])
        gauges = self.book().claude_gauges(account="secondaire", primary="primaire")
        self.assertAlmostEqual(gauges[0].used, 0.87)

    def test_un_releve_non_date_ne_remplace_pas_l_historique_date(self):
        self.agent("legacy", "claude")
        self.events("legacy", windows(seven_day=(0.67, self.now + 3 * 86400)))
        ops = storage.of(self.db).operations
        ops.record_gauges([line("claude", "seven_day", 0.87, self.now + 3 * 86400,
                                account="primaire", produced_at=self.now - 3600,
                                source="ag-frais")])
        gauges = self.book().claude_gauges(account="primaire", primary="primaire")
        self.assertAlmostEqual(gauges[0].used, 0.87)
        self.assertEqual(gauges[0].source, "ag-frais")


class ResetEtAjustementTest(_JaugesBase):
    """On ne bloque pas une remise à zéro réelle ni un ajustement plus récent."""

    def test_une_baisse_plus_recente_est_acceptee(self):
        ops = storage.of(self.db).operations
        fenetre = self.now + 3 * 86400
        ops.record_gauges([line("claude", "seven_day", 0.87, fenetre,
                                account="secondaire", produced_at=self.now - 3600,
                                source="ag")])
        # ajustement explicite du fournisseur (voucher) : MÊME fenêtre, plus récent
        ops.record_gauges([line("claude", "seven_day", 0.20, fenetre,
                                account="secondaire", produced_at=self.now - 60,
                                source="ag")])
        latest = ops.gauge_latest(harness="claude", account="secondaire")
        self.assertAlmostEqual(latest[0]["used"], 0.20)
        self.assertAlmostEqual(latest[0]["produced_ts"], self.now - 60, places=0)

    def test_une_remise_a_zero_reelle_change_de_fenetre(self):
        ops = storage.of(self.db).operations
        ops.record_gauges([line("claude", "seven_day", 0.87, self.now + 3 * 86400,
                                account="secondaire", produced_at=self.now - 3600,
                                source="ag")])
        nouvelle = self.now + 10 * 86400
        ops.record_gauges([line("claude", "seven_day", 0.05, nouvelle,
                                account="secondaire", produced_at=self.now - 30,
                                source="ag")])
        latest = ops.gauge_latest(harness="claude", account="secondaire")[0]
        self.assertAlmostEqual(latest["used"], 0.05)
        self.assertAlmostEqual(latest["resets_at_ts"], nouvelle, places=0)

    def test_l71_une_fenetre_echue_vaut_zero(self):
        gauge = cost.Gauge("claude", "five_hour", 0.95, self.now - 3600, 5 * 3600,
                           produced_at=self.now - 9000, source="ag")
        self.assertEqual(gauge.used_at(self.now), 0.0)
