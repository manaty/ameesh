# SPDX-License-Identifier: AGPL-3.0-only
"""#6317 : corrélation pure A4 d'un signal à la version active par hôte.

Les formats sont ceux du staging existant : `actif-<sha>.json` (ou sa forme
partagée `staging.attested`) pour la preuve active, `staging-<jour>.jsonl`
pour le contrôle de fraîcheur. Aucune base, aucun réseau : la vue active est
injectée, la relation d'ascendance est un prédicat de test, et les deux
lecteurs existants sont exercés sur des fichiers temporaires.
"""
from __future__ import annotations

import datetime
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ameesh import release_cutoff, repair_correlation

from tests import support  # noqa: F401  (L142 : environnement isolé)

OBSERVE = "a" * 40
ACTIVE = "b" * 40
OTHER = "c" * 40
HOST = "ameesh-agents-1"
HOST2 = "ameesh-agents-2"
NOW = datetime.datetime(2026, 11, 8, 12, 0, tzinfo=datetime.timezone.utc).timestamp()


def iso(stamp: float) -> str:
    return datetime.datetime.fromtimestamp(
        stamp, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def signal(**override):
    data = {"environnement": "staging", "hote": HOST, "revision": OBSERVE}
    data.update(override)
    return data


def attestation(sha: str, *, at: float = NOW - 60, host: str = HOST) -> dict:
    return {host: {"sha": sha, "absent": False, "at": at,
                   "attestation": "actif-%s.json" % sha[:12]}}


def ligne(*, sha: str = "", etape: str = "sante", verdict: str = "ok",
          at: float = NOW - 30, precedent: str = "", host: str = HOST) -> dict:
    return {"ts": iso(at), "hote": host, "sha": sha or None,
            "precedent": precedent or None, "etape": etape, "verdict": verdict}


def ascend(observe: str, actif: str) -> bool:
    """Le seul ordre du test : OBSERVE précède ACTIVE."""
    return (observe, actif) == (OBSERVE, ACTIVE)


class CorrelateTest(unittest.TestCase):
    def test_version_anterieure(self):
        """OBSERVE est un ancêtre de la version attestée ACTIVE."""
        report = repair_correlation.correlate(
            signal(), attestation(ACTIVE), is_ancestor=ascend, now=NOW)
        self.assertEqual(report["verdict"],
                         repair_correlation.VERSION_ANTERIEURE)
        self.assertEqual(report["connus"], [HOST])
        self.assertEqual(report["inconnus"], [])
        self.assertEqual(report["hotes"][0]["actif"], ACTIVE)

    def test_version_active(self):
        """La révision observée est la version active de l'hôte."""
        report = repair_correlation.correlate(
            signal(), attestation(OBSERVE), now=NOW)
        self.assertEqual(report["verdict"],
                         repair_correlation.VERSION_ACTIVE)
        self.assertEqual(report["hotes"][0]["verdict"],
                         repair_correlation.VERSION_ACTIVE)

    def test_ascendance_absente(self):
        """Sans prédicat, une paire inégale ne reçoit aucune direction."""
        report = repair_correlation.correlate(
            signal(), attestation(ACTIVE), now=NOW)
        self.assertEqual(report["verdict"],
                         repair_correlation.VERSION_DIFFERENTE)
        self.assertIn("ascendance", report["hotes"][0]["raison"])

    def test_ascendance_non_prouvee(self):
        """Un prédicat qui répond faux laisse la relation indéterminée."""
        report = repair_correlation.correlate(
            signal(), attestation(ACTIVE),
            is_ancestor=lambda observe, actif: False, now=NOW)
        self.assertEqual(report["verdict"],
                         repair_correlation.VERSION_DIFFERENTE)

    def test_ascendance_injectee_recue(self):
        """Le prédicat reçoit (révision observée, version attestée)."""
        seen = []

        def predicate(observe, actif):
            seen.append((observe, actif))
            return True

        report = repair_correlation.correlate(
            signal(), attestation(ACTIVE), is_ancestor=predicate, now=NOW)
        self.assertEqual(report["verdict"],
                         repair_correlation.VERSION_ANTERIEURE)
        self.assertEqual(seen, [(OBSERVE, ACTIVE)])

    def test_ascendance_en_echec(self):
        """Un prédicat qui lève ne prouve pas l'ascendance."""
        def predicate(observe, actif):
            raise RuntimeError("git illisible")

        report = repair_correlation.correlate(
            signal(), attestation(ACTIVE), is_ancestor=predicate, now=NOW)
        self.assertEqual(report["verdict"],
                         repair_correlation.VERSION_DIFFERENTE)

    def test_rollback(self):
        """Rollback : la révision observée est postérieure à la version active."""
        # Le signal vient d'ACTIVE, mais l'hôte est revenu à OBSERVE : le
        # prédicat d'ascendance OBSERVE -> ACTIVE ne s'applique pas.
        report = repair_correlation.correlate(
            signal(revision=ACTIVE), attestation(OBSERVE),
            is_ancestor=ascend, now=NOW)
        self.assertEqual(report["verdict"],
                         repair_correlation.VERSION_DIFFERENTE)
        self.assertEqual(report["hotes"][0]["actif"], OBSERVE)

    def test_flotte_mixte(self):
        active = {**attestation(ACTIVE, host=HOST),
                  **attestation(OBSERVE, host=HOST2)}
        report = repair_correlation.correlate(
            signal(hotes=[HOST, HOST2]), active, is_ancestor=ascend, now=NOW)
        self.assertEqual(report["verdict"], repair_correlation.MIXTE)
        self.assertEqual(report["connus"], [HOST, HOST2])
        by_host = {row["hote"]: row["verdict"] for row in report["hotes"]}
        self.assertEqual(by_host, {
            HOST: repair_correlation.VERSION_ANTERIEURE,
            HOST2: repair_correlation.VERSION_ACTIVE})
        self.assertIn("flotte mixte", report["raison"])

    def test_flotte_bornee_par_l_argument_hosts(self):
        active = {**attestation(ACTIVE, host=HOST),
                  **attestation(OBSERVE, host=HOST2)}
        report = repair_correlation.correlate(
            signal(), active, hosts=[HOST2], now=NOW)
        self.assertEqual(report["verdict"],
                         repair_correlation.VERSION_ACTIVE)
        self.assertEqual([row["hote"] for row in report["hotes"]], [HOST2])

    def test_attestation_absente(self):
        report = repair_correlation.correlate(signal(), {}, now=NOW)
        self.assertEqual(report["verdict"], repair_correlation.INCONNU)
        self.assertEqual(report["inconnus"], [HOST])
        self.assertEqual(report["hotes"][0]["raison"], "attestation absente")

    def test_attestation_perimee(self):
        old = NOW - repair_correlation.DUREE_MAX_ATTESTATION_S - 1
        report = repair_correlation.correlate(
            signal(), attestation(ACTIVE, at=old), now=NOW)
        self.assertEqual(report["verdict"], repair_correlation.INCONNU)
        self.assertIn("périmée", report["hotes"][0]["raison"])
        # Le lecteur peut élargir sa fenêtre ; la relation redevient concluante.
        report = repair_correlation.correlate(
            signal(), attestation(ACTIVE, at=old), is_ancestor=ascend,
            now=NOW,
            max_age_s=repair_correlation.DUREE_MAX_ATTESTATION_S * 2)
        self.assertEqual(report["verdict"],
                         repair_correlation.VERSION_ANTERIEURE)

    def test_journal_posterieur_autre_commit(self):
        """Une preuve antérieure à un autre déploiement ne décrit plus l'actif."""
        report = repair_correlation.correlate(
            signal(), attestation(ACTIVE), is_ancestor=ascend,
            journal=[ligne(sha=OTHER)], now=NOW)
        self.assertEqual(report["verdict"], repair_correlation.INCONNU)
        self.assertIn("journal postérieur", report["hotes"][0]["raison"])
        self.assertEqual(report["hotes"][0]["journal"]["sha"], OTHER)

    def test_journal_posterieur_meme_commit(self):
        report = repair_correlation.correlate(
            signal(), attestation(ACTIVE), is_ancestor=ascend,
            journal=[ligne(sha=ACTIVE)], now=NOW)
        self.assertEqual(report["verdict"],
                         repair_correlation.VERSION_ANTERIEURE)

    def test_journal_retour(self):
        report = repair_correlation.correlate(
            signal(), attestation(ACTIVE), is_ancestor=ascend,
            journal=[ligne(sha=OTHER, etape="retour", precedent=OBSERVE)],
            now=NOW)
        self.assertEqual(report["verdict"], repair_correlation.INCONNU)
        self.assertIn("retour", report["hotes"][0]["raison"])
        # Un retour vers le contenu attesté ne contredit pas l'attestation.
        report = repair_correlation.correlate(
            signal(), attestation(ACTIVE), is_ancestor=ascend,
            journal=[ligne(sha=OTHER, etape="retour", precedent=ACTIVE)],
            now=NOW)
        self.assertEqual(report["verdict"],
                         repair_correlation.VERSION_ANTERIEURE)

    def test_journal_echec(self):
        report = repair_correlation.correlate(
            signal(), attestation(ACTIVE), is_ancestor=ascend,
            journal=[ligne(sha=OTHER, etape="sante", verdict="ko")],
            now=NOW)
        self.assertEqual(report["verdict"], repair_correlation.INCONNU)
        self.assertIn("échec", report["hotes"][0]["raison"])
        # Le passage en échec nomme le contenu précédent resté actif.
        report = repair_correlation.correlate(
            signal(), attestation(ACTIVE), is_ancestor=ascend,
            journal=[ligne(sha=OTHER, etape="sante", verdict="ko",
                           precedent=ACTIVE)],
            now=NOW)
        self.assertEqual(report["verdict"],
                         repair_correlation.VERSION_ANTERIEURE)

    def test_journal_lignes_illisibles_ignorees(self):
        """Une ligne sans instant ou sans hôte ne prouve rien."""
        report = repair_correlation.correlate(
            signal(), attestation(ACTIVE), is_ancestor=ascend,
            journal=[{"hote": HOST, "sha": OTHER},
                     {"ts": iso(NOW), "sha": OTHER}],
            now=NOW)
        self.assertEqual(report["verdict"],
                         repair_correlation.VERSION_ANTERIEURE)

    def test_hote_reporte_absent(self):
        active = {HOST: {"sha": None, "absent": True, "at": NOW - 60,
                         "raison": "poste hors ligne"}}
        report = repair_correlation.correlate(signal(), active, now=NOW)
        self.assertEqual(report["verdict"], repair_correlation.INCONNU)
        self.assertIn("reporté absent", report["hotes"][0]["raison"])
        self.assertIn("poste hors ligne", report["hotes"][0]["raison"])

    def test_environnement_distinct(self):
        report = repair_correlation.correlate(
            signal(environnement="prod"), attestation(ACTIVE), now=NOW)
        self.assertEqual(report["verdict"], repair_correlation.INCONNU)
        self.assertIn("environnement distinct : prod", report["raison"])
        self.assertEqual(report["hotes"], [])

    def test_projet_distinct(self):
        report = repair_correlation.correlate(
            signal(project="nexlink"), attestation(ACTIVE), now=NOW)
        self.assertEqual(report["verdict"], repair_correlation.INCONNU)
        self.assertIn("projet distinct : nexlink", report["raison"])
        self.assertEqual(report["projet"], "nexlink")

    def test_projet_absent_suppose_ameesh(self):
        report = repair_correlation.correlate(
            signal(), attestation(ACTIVE), now=NOW)
        self.assertEqual(report["projet"], "ameesh")

    def test_enveloppe_repair_signal_1(self):
        """Le reçu A1 de U2 (#6315) alimente le lecteur sans adaptation."""
        record = {
            "schema": "repair-signal/1", "id": "rs1_0123456789abcdef",
            "project": "ameesh", "environment": "staging", "source": "alerts",
            "native_id": "a" * 64, "occurrence": 2, "cause_key": "",
            "observed_version": ACTIVE, "observed_at": iso(NOW - 120),
            "received_at": iso(NOW), "evidence_ref": "alerts:" + "a" * 64,
            "outcome": "pending",
        }
        report = repair_correlation.correlate(
            record, attestation(OBSERVE), hosts=[HOST], now=NOW)
        self.assertEqual(report["verdict"],
                         repair_correlation.VERSION_DIFFERENTE)
        self.assertEqual(report["revision"], ACTIVE)

    def test_environnement_absent(self):
        report = repair_correlation.correlate(
            signal(environnement=""), attestation(ACTIVE), now=NOW)
        self.assertEqual(report["verdict"], repair_correlation.INCONNU)
        self.assertIn("environnement absent", report["raison"])

    def test_revision_absente_ou_illisible(self):
        for revision in ("", "pas-un-commit", "xyz1234"):
            with self.subTest(revision=revision):
                report = repair_correlation.correlate(
                    signal(revision=revision), attestation(ACTIVE), now=NOW)
                self.assertEqual(report["verdict"],
                                 repair_correlation.INCONNU)
                self.assertIn("révision", report["raison"])

    def test_aucun_hote(self):
        report = repair_correlation.correlate(
            signal(hote=""), attestation(ACTIVE), now=NOW)
        self.assertEqual(report["verdict"], repair_correlation.INCONNU)
        self.assertIn("aucun hôte", report["raison"])

    def test_prefixe_court(self):
        """L'attestation peut nommer un commit court, comme `staging_pending`."""
        report = repair_correlation.correlate(
            signal(), attestation(OBSERVE[:12]), now=NOW)
        self.assertEqual(report["verdict"],
                         repair_correlation.VERSION_ACTIVE)

    def test_references_bornees(self):
        long_path = "actif-" + "x" * 500
        active = {HOST: {"sha": ACTIVE, "at": NOW - 60,
                         "attestation": long_path}}
        report = repair_correlation.correlate(signal(), active, now=NOW)
        self.assertEqual(report["verdict"],
                         repair_correlation.VERSION_DIFFERENTE)
        self.assertLessEqual(len(report["hotes"][0]["attestation"]), 240)

    def test_entree_illisible_ne_leve_pas(self):
        report = repair_correlation.correlate(None, None, now=NOW)
        self.assertEqual(report["verdict"], repair_correlation.INCONNU)
        self.assertEqual(report["schema"], repair_correlation.SCHEMA)


class FormatTest(unittest.TestCase):
    """Les deux lecteurs existants alimentent la corrélation sans adaptation."""

    def test_vue_active_depuis_actif_json(self):
        with tempfile.TemporaryDirectory() as home:
            folder = Path(home, ".local", "state", "ameesh", "mise-a-jour")
            folder.mkdir(parents=True)
            Path(folder, "actif-%s.json" % OBSERVE[:12]).write_text(json.dumps({
                "sha": OBSERVE, "date": iso(NOW - 60),
                "hotes": {HOST: {"actif": OBSERVE}}}), encoding="utf-8")
            with mock.patch.dict(os.environ, {"HOME": home}):
                active = repair_correlation.active_view()
        self.assertEqual(active[HOST]["sha"], OBSERVE)
        report = repair_correlation.correlate(signal(), active, now=NOW)
        self.assertEqual(report["verdict"],
                         repair_correlation.VERSION_ACTIVE)

    def test_vue_journal_depuis_staging_jsonl(self):
        with tempfile.TemporaryDirectory() as home:
            path = Path(home, "staging.jsonl")
            path.write_text(json.dumps(ligne(sha=ACTIVE)) + "\n",
                            encoding="utf-8")
            with mock.patch.object(release_cutoff, "staging_journal",
                                   return_value=str(path)):
                rows = repair_correlation.journal_view()
        self.assertEqual(len(rows), 1)
        report = repair_correlation.correlate(
            signal(), attestation(ACTIVE), is_ancestor=ascend,
            journal=rows, now=NOW)
        self.assertEqual(report["verdict"],
                         repair_correlation.VERSION_ANTERIEURE)


if __name__ == "__main__":
    unittest.main()
