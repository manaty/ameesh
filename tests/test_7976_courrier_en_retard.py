# SPDX-License-Identifier: AGPL-3.0-only
"""#7976 — le courrier en retard passe avant les tours enchaînés.

Constat (staging 1.66.0, AG1, 20/11) : un agent dsh en tours enchaînés
(suite après plafond, reprise, consigne) a laissé son courrier ordinaire 1 h 40
non remis — le hook n'en remet rien en cours de tour (L514), `_pick` servait la
suite et la consigne avant lui, et la fenêtre de regroupement repartait à
chaque fin de tour (#3451). La sonde staging l'a compté en souffrance.

Désormais un message actif plus vieux que `MAIL_OVERDUE_S` ouvre son tour tout
de suite, avant la suite, la consigne en attente et le réveil programmé ; le
courrier frais et le courrier passif gardent leurs règles. Postgres réel.
"""
from __future__ import annotations

import time
import unittest

from .test_l125_courrier_regroupe import _ligne

from ameesh import mail, registry
from ameesh.runner import AgentWorker

from .test_l125_courrier_regroupe import _Base


class CourrierEnRetardTest(_Base):
    def _en_retard(self, name: str) -> int:
        ident = mail.send(self.db, "dev1", name, "le lot 12 attend ton avis")
        self._vieillir(name, AgentWorker.MAIL_OVERDUE_S + 60)
        return ident

    def test_passe_avant_la_suite_apres_plafond(self):
        _runner, worker = self._worker("dev-s")
        ident = self._en_retard("dev-s")
        worker.suite = "turn ceiling"
        worker._turn_end_mono = time.monotonic()  # tour clos à l'instant
        spec = worker.pick()
        self.assertEqual(spec["kind"], "mail")
        self.assertEqual(spec["ids"], [ident])
        # la suite n'est pas perdue : elle part au tour suivant
        self.assertEqual(worker.suite, "turn ceiling")
        mail.mark_delivered(self.db, [ident])
        self.assertEqual(worker.pick()["kind"], "suite")

    def test_passe_avant_la_consigne_en_attente_sans_la_consommer(self):
        _runner, worker = self._worker("dev-p")
        ident = self._en_retard("dev-p")
        registry.set_pending_prompt(self.db, "dev-p", "reprends le lot 12")
        worker._turn_end_mono = time.monotonic()
        spec = worker.pick()
        self.assertEqual((spec["kind"], spec["ids"]), ("mail", [ident]))
        self.assertEqual(registry.get(self.db, "dev-p")["pending_prompt"],
                         "reprends le lot 12")
        mail.mark_delivered(self.db, [ident])
        self.assertEqual(worker.pick()["kind"], "prompt")

    def test_emporte_le_courrier_frais_du_meme_tour(self):
        _runner, worker = self._worker("dev-m")
        ancien = self._en_retard("dev-m")
        frais = mail.send(self.db, "dev2", "dev-m", "le lot 13 est prêt")
        worker.suite = "turn ceiling"
        spec = worker.pick()
        self.assertEqual(sorted(spec["ids"]), sorted([ancien, frais]))

    def test_le_courrier_frais_laisse_passer_la_suite(self):
        _runner, worker = self._worker("dev-f")
        mail.send(self.db, "dev1", "dev-f", "le lot 12 est prêt")
        self._vieillir("dev-f", AgentWorker.MAIL_OVERDUE_S - 60)
        worker.suite = "turn ceiling"
        self.assertEqual(worker.pick()["kind"], "suite")

    def test_le_courrier_passif_en_retard_n_ouvre_rien(self):
        _runner, worker = self._worker("dev-i")
        mail.send(self.db, "dev1", "dev-i", "rien à faire", payload={"info": True})
        self._vieillir("dev-i", AgentWorker.MAIL_OVERDUE_S * 10)
        worker.suite = "turn ceiling"
        self.assertEqual(worker.pick()["kind"], "suite")

    def test_sans_concurrent_le_retard_ignore_la_fenetre_de_fin_de_tour(self):
        _runner, worker = self._worker("dev-w")
        ident = self._en_retard("dev-w")
        worker._turn_end_mono = time.monotonic() - 1  # #3451 : fenêtre en cours
        self.assertEqual(worker.pick()["ids"], [ident])


    def test_un_arriere_lourd_s_ecoule_en_un_tour(self):
        # 12 messages de 6 Ko, en retard : le budget (20 Ko) n'en tient que 2
        # en entier ; les autres entrent en aperçu, tous dans ce tour.
        _runner, worker = self._worker("dev-a")
        ids = [mail.send(self.db, "dev%d" % i, "dev-a", ("avis %d " % i) + "x" * 6000)
               for i in range(12)]
        self._vieillir("dev-a", AgentWorker.MAIL_OVERDUE_S + 60)
        worker.suite = "turn ceiling"
        spec = worker.pick()
        self.assertEqual((spec["kind"], spec["remaining"]), ("mail", 0))
        self.assertEqual(spec["ids"], ids)
        self.assertLessEqual(mail.octets(spec["prompt"]), worker._budget_courrier())
        self.assertIn("agent-mail show %d" % ids[-1], spec["prompt"])
        # la réservation refait le même choix (run_turn)
        _t, retenus, reste = mail.prompt_for(
            self.db, spec["candidats"], kind="mail", budget=worker._budget_courrier(),
            en_retard_avant=worker._retard_avant())
        self.assertEqual(([int(m["id"]) for m in retenus], reste), (ids, 0))


class ApercuEnRetardTest(unittest.TestCase):
    def _lignes(self, n, created_ts):
        return [dict(_ligne(body="corps %d " % i + "y" * 6000), id=i + 1,
                     created_ts=created_ts) for i in range(n)]

    def test_le_courrier_frais_lourd_attend_le_tour_suivant(self):
        lignes = self._lignes(6, time.time())
        _t, retenus, reste = mail.prompt_for(None, lignes, kind="mail", budget=20000,
                                             en_retard_avant=time.time() - 300)
        self.assertLess(len(retenus), 6)
        self.assertEqual(reste, 6 - len(retenus))

    def test_le_courrier_en_retard_entre_en_apercu(self):
        lignes = self._lignes(6, time.time() - 3600)
        texte, retenus, reste = mail.prompt_for(None, lignes, kind="mail", budget=20000,
                                                en_retard_avant=time.time() - 300)
        self.assertEqual((len(retenus), reste), (6, 0))
        self.assertLessEqual(mail.octets(texte), 20000)
        self.assertIn("agent-mail show 6", texte)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
