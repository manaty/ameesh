# SPDX-License-Identifier: AGPL-3.0-only
"""#4448 — seuil de rotation de session par harnais, compilé du canon.

Constat : `session_max_tokens` valait 150 000 pour tous les harnais. Un
relecteur Codex dépasse ce seuil dès son premier tour ; `session_tokens`
sommait de surcroît les relevés **cumulés** de `turn.completed` (un fil de
trois tours comptait plusieurs fois son dernier relevé), d'où une rotation à
chaque minimum de trois tours. L'alerte `session_too_big` mesurait la somme des
relectures d'un tour, avec la constante locale 15 M : ni la même grandeur, ni
le même seuil que la rotation.

Ce témoin couvre les quatre attendus : le seuil du harnais vient du canon
compilé, un canon sans bloc retombe sur la valeur commune, l'alerte et la
rotation s'accordent sur la même session, et la garde mécanique refuse une
valeur de harnais codée en dur (voir aussi `test_2449_garde_mecanique`).
Postgres réel ; faux harnais `codex` du banc, jamais `~/.codex`.
"""
from __future__ import annotations

import dataclasses
import json
import os
import unittest
from unittest import mock

from ameesh import exploitation, mechanics, registry, storage
from ameesh.config import Config
from ameesh.runner import AgentWorker, Runner

from .support import FAKEBIN, PgTestCase

FIL = "codex-thread-1"  # le fil du faux harnais Codex


def document(session_block: str = "") -> mechanics.Document:
    """Un document global minimal, avec ou sans bloc `session` (#4448)."""
    return mechanics.compile_document("""
schema: ameesh-mechanics/1
timezone: Europe/Paris
cutoffs: ["11:00", "23:00"]
build: {max_in_build: 2}
ci:
  packet: full
  incident_label: ci-full
  environment_red: does_not_block
  faulty_lot: removed_without_new_review
reviews:
  families: [claude, codex, deepseek]
  exclude_author_family: true
  distinct_families: true
  light_reviews: 1
merge:
  named_fire: false
  repeat_freeze: false
  on_green_packet: true
  post_merge_red: forward_or_revert
""" + session_block)


SEUIL_COMMUN = 150_000
SEUIL_CODEX = 2_000_000
BLOC = "session:\n  max_tokens: 150000\n  max_tokens_by_harness: {codex: 2000000}\n"


class BlocSessionTest(unittest.TestCase):
    """Le lecteur strict du canon et son repli commun."""

    def test_bloc_lu_et_surcharge_du_harnais(self):
        doc = document(BLOC)
        self.assertEqual(doc.session.max_tokens, SEUIL_COMMUN)
        self.assertEqual(doc.session.by_harness, (("codex", SEUIL_CODEX),))
        self.assertEqual(doc.session_max_tokens("codex", 1), SEUIL_CODEX)
        self.assertEqual(doc.session_max_tokens("claude", 1), SEUIL_COMMUN)
        self.assertEqual(doc.session_max_tokens("harnais-inconnu", 1), SEUIL_COMMUN)

    def test_bloc_absent_replie_sur_la_valeur_commune(self):
        doc = document()
        self.assertIsNone(doc.session)
        self.assertEqual(doc.session_max_tokens("codex", SEUIL_COMMUN), SEUIL_COMMUN)
        payload = doc.to_dict()
        self.assertNotIn("session", payload, "un document sans bloc compile à l'identique")

    def test_bloc_present_dans_le_document_compile(self):
        self.assertEqual(document(BLOC).to_dict()["session"],
                         {"max_tokens": SEUIL_COMMUN,
                          "max_tokens_by_harness": {"codex": SEUIL_CODEX}})

    def test_un_harnais_non_declare_refuse(self):
        with self.assertRaises(mechanics.MechanicsError):
            document("session:\n  max_tokens: 150000\n"
                     "  max_tokens_by_harness: {gemini: 2000000}\n")

    def test_valeurs_invalides_refusees(self):
        for bloc in (
                "session:\n  max_tokens: 0\n",
                "session:\n  max_tokens: -1\n",
                "session:\n  max_tokens: 150000\n  max_tokens_by_harness: {codex: 0}\n",
                "session:\n  max_tokens: 150000\n  max_tokens_by_harness: [codex]\n",
                "session:\n  unknown: 1\n"):
            with self.subTest(bloc=bloc):
                with self.assertRaises(mechanics.MechanicsError):
                    document(bloc)

    def test_absent_du_canon_replie_sur_la_valeur_locale(self):
        # aucun canon configuré : le repli local est la seule valeur
        self.assertEqual(mechanics.session_max_tokens(Config(), "codex",
                                                      SEUIL_COMMUN),
                         SEUIL_COMMUN)

    def test_canon_illisible_replie_sur_la_valeur_locale(self):
        cfg = Config()
        with mock.patch.object(mechanics, "configured",
                               side_effect=mechanics.MechanicsError("illisible")):
            self.assertEqual(mechanics.session_max_tokens(cfg, "codex", 123_000),
                             123_000)


class _RotationBase(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute("TRUNCATE turn_costs, spend_pending CASCADE")
        self.cfg = dataclasses.replace(self.cfg, session_min_turns=3,
                                       context_max_tokens=0.0,
                                       session_max_tokens=float(SEUIL_COMMUN))
        self.executeur = Runner(self.cfg, self.db, once=True)
        cwd = os.path.join(self.tmp, "work", "cx-4448")
        os.makedirs(cwd, exist_ok=True)
        self.register("cx-4448", "codex", cwd=cwd)
        lease = registry.claim(self.db, "cx-4448", self.executeur.runner_id, 3600)
        self.assertIsNotNone(lease)
        self.worker = AgentWorker(self.executeur, registry.get(self.db, "cx-4448"),
                                  lease)
        self.addCleanup(self.worker.watchdog_stop.set)

    def tour(self, usage: int, *, exit_code: int = 0) -> bool:
        """Un tour Codex dont `turn.completed` porte l'usage cumulé du fil.

        `exit_code` non nul : le harnais publie son usage puis échoue — le
        tour est compté au grand livre (L13 B1), donc il compte dans la
        session (#4448 B cx3).
        """
        env = {"AMEESH_BIN_DIR": FAKEBIN, "AMEESH_TEST_LOG": self.turns_log,
               "AMEESH_TEST_EXIT": str(exit_code),
               "AMEESH_TEST_CODEX_USAGE": json.dumps({
                   "input_tokens": usage, "cached_input_tokens": usage,
                   "output_tokens": 0})}
        with mock.patch.dict(os.environ, env, clear=False):
            return self.worker.run_turn({"kind": "prompt", "prompt": "relecture",
                                         "ids": []})

    def alertes(self):
        with mock.patch.object(mechanics, "configured", return_value=document(BLOC)):
            return [a for a in exploitation.alerts(self.cfg, self.db)
                    if a["type"] == "session_too_big"]


class SeuilDuHarnaisTest(_RotationBase):
    def test_seuil_du_harnais_lu_du_canon(self):
        with mock.patch.object(mechanics, "configured", return_value=document(BLOC)):
            self.assertEqual(self.worker.session_max_tokens(), SEUIL_CODEX)
        with mock.patch.object(mechanics, "configured", return_value=document()):
            self.assertEqual(self.worker.session_max_tokens(), SEUIL_COMMUN)
        with mock.patch.object(mechanics, "configured",
                               side_effect=mechanics.MechanicsError("illisible")):
            self.assertEqual(self.worker.session_max_tokens(), SEUIL_COMMUN)

    def test_premier_tour_de_relecteur_sous_le_seuil_ne_tourne_pas(self):
        """Le relecteur Codex travaille trois tours : sous 2 M, pas de rotation."""
        with mock.patch.object(mechanics, "configured", return_value=document(BLOC)):
            for usage in (300_000, 600_000, 900_000):
                self.assertTrue(self.tour(usage))
            self.assertEqual(self.worker.session_tokens, 900_000,
                             "le cumul du fil, pas la somme des trois cumuls")
            self.assertGreaterEqual(self.worker.session_turns, 3)
            self.assertFalse(self.worker.rotation_due())

    def test_cumul_codex_n_est_pas_une_somme_de_cumuls(self):
        """Deux relevés cumulés 600 k puis 1,2 M : la session vaut 1,2 M."""
        with mock.patch.object(mechanics, "configured", return_value=document(BLOC)):
            self.assertTrue(self.tour(600_000))
            self.assertTrue(self.tour(1_200_000))
        self.assertEqual(self.worker.session_tokens, 1_200_000)

    def test_au_dela_du_seuil_du_harnais_la_rotation_est_due(self):
        with mock.patch.object(mechanics, "configured", return_value=document(BLOC)):
            for usage in (800_000, 1_600_000, 2_400_000):
                self.assertTrue(self.tour(usage))
            self.assertGreaterEqual(self.worker.session_max_tokens(), SEUIL_CODEX)
            self.assertTrue(self.worker.rotation_due())


class AlerteEtRotationDAccordTest(_RotationBase):
    def test_accord_sur_la_meme_session_et_le_meme_seuil(self):
        with mock.patch.object(mechanics, "configured", return_value=document(BLOC)):
            for usage in (800_000, 1_600_000, 2_400_000):
                self.assertTrue(self.tour(usage))
            self.assertTrue(self.worker.rotation_due())
        alertes = self.alertes()
        self.assertEqual(len(alertes), 1)
        alerte = alertes[0]
        self.assertEqual((alerte["agent"], alerte["session"]), ("cx-4448", FIL))
        self.assertEqual(alerte["value"], 2_400_000)
        self.assertEqual(alerte["threshold"], SEUIL_CODEX)
        self.assertEqual((alerte["turns"], alerte["harness"]), (3, "codex"))

    def test_sous_le_seuil_aucune_alerte(self):
        with mock.patch.object(mechanics, "configured", return_value=document(BLOC)):
            for usage in (300_000, 600_000, 900_000):
                self.assertTrue(self.tour(usage))
            self.assertFalse(self.worker.rotation_due())
        self.assertEqual(self.alertes(), [])

    def test_apres_rotation_l_alerte_se_tait(self):
        with mock.patch.object(mechanics, "configured", return_value=document(BLOC)):
            for usage in (800_000, 1_600_000, 2_400_000):
                self.assertTrue(self.tour(usage))
        self.assertEqual(len(self.alertes()), 1)
        # la rotation a ouvert une autre session : le dernier tour n'est plus
        # celui de la session courante, l'alerte se tait (« ou se tait quand la
        # rotation a déjà eu lieu »).
        registry.set_session(self.db, "cx-4448", "codex-thread-2")
        self.assertEqual(self.alertes(), [])

    def test_worker_relance_reprend_la_mesure_du_grand_livre(self):
        """La session survit au worker : la rotation reprend sa mesure."""
        with mock.patch.object(mechanics, "configured", return_value=document(BLOC)):
            for usage in (800_000, 1_600_000, 2_400_000):
                self.assertTrue(self.tour(usage))
            suivant = AgentWorker(self.executeur, registry.get(self.db, "cx-4448"),
                                  {"lease_epoch": self.worker.epoch})
            self.addCleanup(suivant.watchdog_stop.set)
            self.assertEqual((suivant.session_turns, suivant.session_tokens), (0, 0.0))
            self.assertTrue(suivant.rotation_due())
        self.assertEqual(suivant.session_turns, 3)
        self.assertEqual(suivant.session_tokens, 2_400_000)
        self.assertEqual(len(self.alertes()), 1)

    def test_seuil_absent_du_canon_replie_sur_le_commun(self):
        with mock.patch.object(mechanics, "configured", return_value=document()):
            for usage in (800_000, 1_600_000, 2_400_000):
                self.assertTrue(self.tour(usage))
        with mock.patch.object(mechanics, "configured", return_value=document()):
            alertes = [a for a in exploitation.alerts(self.cfg, self.db)
                       if a["type"] == "session_too_big"]
        self.assertEqual(len(alertes), 1)
        self.assertEqual(alertes[0]["threshold"], SEUIL_COMMUN)


class ToursEchouesTest(_RotationBase):
    """#4448 B cx3 : un tour échoué est compté au grand livre, donc il compte.

    L'alerte et la rotation doivent lire le même nombre de tours avant comme
    après la recréation du worker : plus de rotation qui n'apparaît qu'après un
    redémarrage, plus d'alerte sans geste.
    """

    def test_tours_echoues_accord_alerte_rotation_et_reprise(self):
        with mock.patch.object(mechanics, "configured", return_value=document(BLOC)):
            for usage in (800_000, 1_600_000, 2_400_000):
                self.assertFalse(self.tour(usage, exit_code=1),
                                 "le harnais échoue après avoir publié son usage")
            self.assertEqual(self.worker.session_turns, 3)
            self.assertEqual(self.worker.session_tokens, 2_400_000)
            self.assertTrue(self.worker.rotation_due())
            # la même session reprise par un worker neuf : mêmes compteurs,
            # même décision, même alerte.
            suivant = AgentWorker(self.executeur, registry.get(self.db, "cx-4448"),
                                  {"lease_epoch": self.worker.epoch})
            self.addCleanup(suivant.watchdog_stop.set)
            self.assertEqual((suivant.session_turns, suivant.session_tokens), (0, 0.0))
            self.assertTrue(suivant.rotation_due())
            self.assertEqual((suivant.session_turns, suivant.session_tokens),
                             (3, 2_400_000))
        alertes = self.alertes()
        self.assertEqual(len(alertes), 1)
        self.assertEqual((alertes[0]["value"], alertes[0]["threshold"]),
                         (2_400_000, SEUIL_CODEX))
        self.assertEqual(alertes[0]["turns"], 3)

    def test_tours_echoues_sous_le_seuil_pas_de_rotation_ni_d_alerte(self):
        with mock.patch.object(mechanics, "configured", return_value=document(BLOC)):
            for usage in (100_000, 200_000, 300_000):
                self.assertFalse(self.tour(usage, exit_code=1))
            self.assertEqual(self.worker.session_turns, 3)
            self.assertEqual(self.worker.session_tokens, 300_000)
            self.assertFalse(self.worker.rotation_due())
            suivant = AgentWorker(self.executeur, registry.get(self.db, "cx-4448"),
                                  {"lease_epoch": self.worker.epoch})
            self.addCleanup(suivant.watchdog_stop.set)
            self.assertFalse(suivant.rotation_due())
            self.assertEqual((suivant.session_turns, suivant.session_tokens),
                             (3, 300_000))
        self.assertEqual(self.alertes(), [])


if __name__ == "__main__":
    unittest.main()
