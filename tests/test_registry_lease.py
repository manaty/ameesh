# SPDX-License-Identifier: AGPL-3.0-only
"""Registre et baux : course, expiration, fencing, consignes en attente."""
from __future__ import annotations

import threading
import time
import unittest

from ameesh import registry

from .support import PgTestCase


class RegistryTest(PgTestCase):
    def test_upsert_partiel_ne_preserve_que_le_fourni(self):
        """Régression : un upsert minimal (envoi de mail, hook) n'écrase rien."""
        registry.upsert(
            self.db, "alpha", chantier="nexlink", harness="claude", host="laptop",
            cwd="/tmp/alpha", session_id="sess-1", status="running",
            status_text="tour en cours", model="opus", budget_usd=12.5,
        )
        registry.upsert(self.db, "alpha", host="laptop")  # upsert minimal
        row = registry.get(self.db, "alpha")
        self.assertEqual(row["chantier"], "nexlink")
        self.assertEqual(row["harness"], "claude")
        self.assertEqual(row["cwd"], "/tmp/alpha")
        self.assertEqual(row["session_id"], "sess-1")
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["status_text"], "tour en cours")
        self.assertEqual(row["model"], "opus")
        self.assertEqual(float(row["budget_usd"]), 12.5)

    def test_upsert_ne_met_pas_le_harnais_a_other(self):
        """Régression : `coalesce(…, 'other')` ne doit pas écraser un harnais connu."""
        registry.upsert(self.db, "alpha", harness="deepseek", host="laptop")
        registry.upsert(self.db, "alpha", host="laptop")
        self.assertEqual(registry.get(self.db, "alpha")["harness"], "deepseek")

    def test_overview_compte_les_non_lus(self):
        from ameesh import mail
        registry.upsert(self.db, "alpha", harness="claude", host="laptop")
        mail.send(self.db, "beta", "alpha", "un")
        mail.send(self.db, "beta", "alpha", "deux")
        row = [r for r in registry.overview(self.db) if r["name"] == "alpha"][0]
        self.assertEqual(int(row["unread"]), 2)

    def test_claim_release(self):
        registry.upsert(self.db, "alpha", harness="claude", host="laptop")
        lease = registry.claim(self.db, "alpha", "runner-a", 60)
        self.assertIsNotNone(lease)
        self.assertEqual(lease["lease_owner"], "runner-a")
        self.assertEqual(int(lease["lease_epoch"]), 1)
        self.assertGreater(lease["lease_expires_ts"], time.time())

        # un autre exécuteur ne peut pas prendre un bail vivant
        self.assertIsNone(registry.claim(self.db, "alpha", "runner-b", 60))
        # le détenteur peut le reprendre (idempotent, epoch +1)
        again = registry.claim(self.db, "alpha", "runner-a", 60)
        self.assertEqual(int(again["lease_epoch"]), 2)

        self.assertTrue(registry.release(self.db, "alpha", "runner-a", 2))
        self.assertIsNone(registry.get(self.db, "alpha")["lease_owner"])
        self.assertIsNotNone(registry.claim(self.db, "alpha", "runner-b", 60))

    def test_course_de_bail_entre_deux_connexions(self):
        """Deux exécuteurs simultanés : un seul gagne le bail."""
        registry.upsert(self.db, "alpha", harness="claude", host="laptop")
        db1, db2 = self.connect(), self.connect()
        barrier = threading.Barrier(2)
        results: list = [None, None]

        def contend(index: int, db) -> None:
            barrier.wait(timeout=10)
            results[index] = registry.claim(db, "alpha", "runner-%d" % index, 60)

        try:
            threads = [
                threading.Thread(target=contend, args=(0, db1)),
                threading.Thread(target=contend, args=(1, db2)),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
            gagnants = [r for r in results if r]
            self.assertEqual(len(gagnants), 1, "baux gagnés : %r" % (results,))
            perdants = [r for r in results if not r]
            self.assertEqual(len(perdants), 1)
            row = registry.get(self.db, "alpha")
            self.assertEqual(row["lease_owner"], gagnants[0]["lease_owner"])
        finally:
            db1.close()
            db2.close()

    def test_course_de_bail_quatre_connexions(self):
        registry.upsert(self.db, "alpha", harness="claude", host="laptop")
        connections = [self.connect() for _ in range(4)]
        barrier = threading.Barrier(4)
        results: list = [None] * 4

        def contend(index: int) -> None:
            barrier.wait(timeout=10)
            results[index] = registry.claim(connections[index], "alpha", "runner-%d" % index, 60)

        try:
            threads = [threading.Thread(target=contend, args=(i,)) for i in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
            self.assertEqual(len([r for r in results if r]), 1)
        finally:
            for connection in connections:
                connection.close()

    def test_expiration_et_fencing(self):
        registry.upsert(self.db, "alpha", harness="claude", host="laptop")
        first = registry.claim(self.db, "alpha", "runner-a", 1.0)
        self.assertIsNotNone(first)
        epoch_a = int(first["lease_epoch"])
        self.assertIsNone(registry.claim(self.db, "alpha", "runner-b", 60))  # encore vivant

        time.sleep(1.3)
        second = registry.claim(self.db, "alpha", "runner-b", 60)
        self.assertIsNotNone(second, "le bail expiré doit être reprenable")
        self.assertEqual(int(second["lease_epoch"]), epoch_a + 1)

        # l'ancien détenteur est fencé : ni renouvellement, ni écriture de tour
        self.assertIsNone(registry.renew(self.db, "alpha", "runner-a", epoch_a, 60))
        self.assertFalse(registry.end_turn(self.db, "alpha", "runner-a", epoch_a, status="idle"))
        self.assertEqual(int(registry.get(self.db, "alpha")["turns"]), 0)

    def test_renew_refuse_un_bail_expire(self):
        registry.upsert(self.db, "alpha", harness="claude", host="laptop")
        lease = registry.claim(self.db, "alpha", "runner-a", 1.0)
        epoch = int(lease["lease_epoch"])
        time.sleep(1.3)
        self.assertIsNone(registry.renew(self.db, "alpha", "runner-a", epoch, 60))
        # le même exécuteur peut reprendre le bail, mais par un nouveau claim
        again = registry.claim(self.db, "alpha", "runner-a", 60)
        self.assertEqual(int(again["lease_epoch"]), epoch + 1)
        self.assertIsNotNone(registry.renew(self.db, "alpha", "runner-a", epoch + 1, 60))

    def test_begin_turn_refuse_un_bail_expire(self):
        registry.upsert(self.db, "alpha", harness="claude", host="laptop")
        registry.set_pending_prompt(self.db, "alpha", "consigne")
        lease = registry.claim(self.db, "alpha", "runner-a", 1.0)
        epoch = int(lease["lease_epoch"])
        time.sleep(1.3)
        self.assertFalse(registry.begin_turn(self.db, "alpha", "runner-a", epoch, "tour"))
        self.assertIsNone(
            registry.take_pending_prompt(self.db, "alpha", "runner-a", epoch))
        self.assertTrue(registry.begin_turn(
            self.db, "alpha", *(lambda l: ("runner-a", int(l["lease_epoch"])))(
                registry.claim(self.db, "alpha", "runner-a", 60)), "tour"))

    def test_consigne_survit_a_la_mort_de_l_executeur(self):
        """R5 : la consigne d'un tour interrompu repart en attente pour le suivant."""
        registry.upsert(self.db, "alpha", harness="claude", host="laptop")
        registry.set_pending_prompt(self.db, "alpha", "travail à ne pas perdre")
        lease = registry.claim(self.db, "alpha", "runner-a", 60)
        epoch = int(lease["lease_epoch"])
        self.assertEqual(
            registry.take_pending_prompt(self.db, "alpha", "runner-a", epoch),
            "travail à ne pas perdre")
        row = registry.get(self.db, "alpha")
        self.assertIsNone(row["pending_prompt"])
        self.assertEqual(row["current_prompt"], "travail à ne pas perdre")

        # l'exécuteur meurt : le bail expire, un autre reprend l'agent
        self.db.execute(
            "UPDATE agent_registry SET lease_expires_at = now() - interval '1 second' "
            "WHERE name = 'alpha'")
        nouveau = registry.claim(self.db, "alpha", "runner-b", 60)
        self.assertIsNotNone(nouveau)
        row = registry.get(self.db, "alpha")
        self.assertEqual(row["pending_prompt"], "travail à ne pas perdre",
                         "la consigne doit être restaurée au changement de main")
        self.assertIsNone(row["current_prompt"])
        self.assertEqual(
            registry.take_pending_prompt(self.db, "alpha", "runner-b", int(nouveau["lease_epoch"])),
            "travail à ne pas perdre")

    def test_reprise_par_le_meme_executeur_restaure_la_consigne(self):
        """B6b : un redémarrage avec le même --runner-id ne perd pas la consigne."""
        registry.upsert(self.db, "alpha", harness="claude", host="laptop")
        registry.set_pending_prompt(self.db, "alpha", "travail interrompu")
        lease = registry.claim(self.db, "alpha", "runner-a", 60)
        epoch = int(lease["lease_epoch"])
        registry.take_pending_prompt(self.db, "alpha", "runner-a", epoch)
        self.db.execute(
            "UPDATE agent_registry SET lease_expires_at = now() - interval '1 second' "
            "WHERE name = 'alpha'")
        again = registry.claim(self.db, "alpha", "runner-a", 60)  # même identifiant
        self.assertIsNotNone(again)
        self.assertEqual(registry.get(self.db, "alpha")["pending_prompt"],
                         "travail interrompu")
        self.assertEqual(
            registry.take_pending_prompt(self.db, "alpha", "runner-a", int(again["lease_epoch"])),
            "travail interrompu")

    def test_restore_prompt_conserve_les_deux_consignes(self):
        """Sonde codex3 (1) : un échec avec current A + pending B ne perd pas A."""
        registry.upsert(self.db, "alpha", harness="claude", host="laptop")
        registry.set_pending_prompt(self.db, "alpha", "A du tour")
        lease = registry.claim(self.db, "alpha", "runner-a", 60)
        epoch = int(lease["lease_epoch"])
        registry.take_pending_prompt(self.db, "alpha", "runner-a", epoch)
        registry.set_pending_prompt(self.db, "alpha", "B arrivée pendant le tour")
        self.assertTrue(registry.restore_prompt(self.db, "alpha", "runner-a", epoch))
        row = registry.get(self.db, "alpha")
        self.assertEqual(row["pending_prompt"], "A du tour\n\nB arrivée pendant le tour")
        self.assertIsNone(row["current_prompt"])

    def test_deux_consignes_conservees_a_la_reprise(self):
        """B6c : current A + pending B → les deux survivent, dans l'ordre."""
        registry.upsert(self.db, "alpha", harness="claude", host="laptop")
        registry.set_pending_prompt(self.db, "alpha", "A interrompue")
        lease = registry.claim(self.db, "alpha", "runner-a", 60)
        registry.take_pending_prompt(self.db, "alpha", "runner-a", int(lease["lease_epoch"]))
        registry.set_pending_prompt(self.db, "alpha", "B en attente")
        self.db.execute(
            "UPDATE agent_registry SET lease_expires_at = now() - interval '1 second' "
            "WHERE name = 'alpha'")
        nouveau = registry.claim(self.db, "alpha", "runner-b", 60)
        row = registry.get(self.db, "alpha")
        self.assertEqual(row["pending_prompt"], "A interrompue\n\nB en attente")
        self.assertIsNone(row["current_prompt"])
        self.assertEqual(
            registry.take_pending_prompt(self.db, "alpha", "runner-b",
                                         int(nouveau["lease_epoch"])),
            "A interrompue\n\nB en attente")

    def test_restore_prompt(self):
        registry.upsert(self.db, "alpha", harness="claude", host="laptop")
        registry.set_pending_prompt(self.db, "alpha", "consigne")
        lease = registry.claim(self.db, "alpha", "runner-a", 60)
        epoch = int(lease["lease_epoch"])
        registry.take_pending_prompt(self.db, "alpha", "runner-a", epoch)
        self.assertTrue(registry.restore_prompt(self.db, "alpha", "runner-a", epoch))
        row = registry.get(self.db, "alpha")
        self.assertEqual(row["pending_prompt"], "consigne")
        self.assertIsNone(row["current_prompt"])
        self.assertFalse(registry.restore_prompt(self.db, "alpha", "runner-a", epoch))

    def test_renew_prolonge(self):
        registry.upsert(self.db, "alpha", harness="claude", host="laptop")
        lease = registry.claim(self.db, "alpha", "runner-a", 5)
        avant = lease["lease_expires_ts"]
        time.sleep(0.2)
        apres = registry.renew(self.db, "alpha", "runner-a", int(lease["lease_epoch"]), 60)
        self.assertIsNotNone(apres)
        self.assertGreater(apres, avant)
        self.assertIsNone(registry.renew(self.db, "alpha", "runner-a", 999, 60))

    def test_consigne_en_attente_consommee_une_fois(self):
        registry.upsert(self.db, "alpha", harness="claude", host="laptop")
        registry.set_pending_prompt(self.db, "alpha", "fais le point")
        self.assertEqual(registry.get(self.db, "alpha")["status"], "queued")
        lease = registry.claim(self.db, "alpha", "runner-a", 60)
        epoch = int(lease["lease_epoch"])

        self.assertEqual(
            registry.take_pending_prompt(self.db, "alpha", "runner-a", epoch), "fais le point"
        )
        self.assertIsNone(
            registry.take_pending_prompt(self.db, "alpha", "runner-a", epoch)
        )
        row = registry.get(self.db, "alpha")
        self.assertEqual(row["status"], "running")
        self.assertIsNone(row["pending_prompt"])
        # un autre exécuteur (mauvais epoch) ne peut rien consommer
        registry.set_pending_prompt(self.db, "alpha", "autre consigne")
        self.assertIsNone(registry.take_pending_prompt(self.db, "alpha", "runner-a", epoch + 99))

    def test_claimable_filtre_hote_et_arret(self):
        registry.upsert(self.db, "alpha", harness="claude", host="laptop")
        registry.upsert(self.db, "beta", harness="codex", host="autre")
        registry.upsert(self.db, "gamma", harness="deepseek", host="laptop", status="stopped")
        registry.upsert(self.db, "delta", harness="claude", host="laptop")
        registry.claim(self.db, "delta", "runner-x", 60)

        noms = [row["name"] for row in registry.claimable(self.db, "laptop")]
        self.assertEqual(noms, ["alpha"])
        self.assertEqual(
            [row["name"] for row in registry.claimable(self.db, "laptop", ["alpha", "delta"])],
            ["alpha"],
        )
        self.assertEqual([row["name"] for row in registry.claimable(self.db, "autre")], ["beta"])

    def test_reap_marque_mort_un_bail_expire(self):
        registry.upsert(self.db, "alpha", harness="claude", host="laptop")
        lease = registry.claim(self.db, "alpha", "runner-a", 1.0)
        registry.begin_turn(self.db, "alpha", "runner-a", int(lease["lease_epoch"]), "tour")
        time.sleep(1.3)
        morts = registry.reap(self.db, "laptop")
        self.assertEqual([row["name"] for row in morts], ["alpha"])
        self.assertEqual(registry.get(self.db, "alpha")["status"], "dead")
        # un agent mort reste réclamable (le nouveau bail le remet en vie)
        nouveau = registry.claim(self.db, "alpha", "runner-b", 60)
        self.assertIsNotNone(nouveau)
        self.assertEqual(registry.get(self.db, "alpha")["status"], "idle")

    def test_set_status_et_session(self):
        registry.upsert(self.db, "alpha", harness="claude", host="laptop")
        registry.set_status(self.db, "alpha", "blocked", status_text="attend une réponse",
                            error="boum")
        row = registry.get(self.db, "alpha")
        self.assertEqual((row["status"], row["status_text"], row["last_error"]),
                         ("blocked", "attend une réponse", "boum"))
        registry.set_session(self.db, "alpha", "sess-42")
        self.assertEqual(registry.get(self.db, "alpha")["session_id"], "sess-42")

    def test_echeance_recontrolee_apres_le_verrou(self):
        """Grille codex3 (L6) : une échéance se recontrôle avec
        `clock_timestamp()` **après** le dernier verrou. Un renouvellement qui
        attend un verrou pendant que le bail expire doit être refusé.
        """
        if self.db.name != "psycopg":
            self.skipTest("verrou tenu entre deux ordres : connexion persistante requise")
        registry.upsert(self.db, "bail", harness="claude", host=self.cfg.host)
        lease = registry.claim(self.db, "bail", "runner-1", 0.4)
        self.assertIsNotNone(lease)
        bloqueur = self.connect()
        resultat: list = []
        try:
            bloqueur.execute("BEGIN")
            bloqueur.execute(
                "UPDATE agent_registry SET updated_at = now() WHERE name = 'bail'")
            thread = threading.Thread(
                target=lambda: resultat.append(registry.renew(
                    self.db, "bail", "runner-1", int(lease["lease_epoch"]), 30)),
                daemon=True)
            thread.start()
            time.sleep(0.8)  # le bail expire pendant que le renouvellement attend
            bloqueur.execute("COMMIT")
            thread.join(timeout=5)
            self.assertEqual(resultat, [None],
                             "un bail expiré pendant l'attente du verrou a été renouvelé")
        finally:
            try:
                bloqueur.execute("ROLLBACK")
            except Exception:
                pass
            bloqueur.close()

    def test_echeance_recontrolee_apres_select_for_update_sans_modification(self):
        """Sonde codex3 L8 B3 : le détenteur ne fait qu'un `SELECT ... FOR UPDATE`
        (aucune modification), donc le `WHERE` d'un UPDATE n'est pas réévalué
        après l'attente. Le verrou explicite puis le recontrôle
        `clock_timestamp()` doivent refuser le renouvellement et la consommation
        de consigne sous un bail expiré.
        """
        if self.db.name != "psycopg":
            self.skipTest("verrou tenu entre deux ordres : connexion persistante requise")
        registry.upsert(self.db, "bail", harness="claude", host=self.cfg.host)
        lease = registry.claim(self.db, "bail", "runner-1", 0.4)
        self.assertIsNotNone(lease)
        registry.set_pending_prompt(self.db, "bail", "consigne en attente")
        bloqueur = self.connect()
        epoch = int(lease["lease_epoch"])
        try:
            resultat: list = []
            bloqueur.execute("BEGIN")
            bloqueur.execute(
                "SELECT name FROM agent_registry WHERE name = 'bail' FOR UPDATE")
            thread = threading.Thread(
                target=lambda: resultat.append(
                    registry.renew(self.db, "bail", "runner-1", epoch, 30)),
                daemon=True)
            thread.start()
            time.sleep(0.8)  # le bail expire pendant que le renouvellement attend
            bloqueur.execute("COMMIT")
            thread.join(timeout=5)
            self.assertEqual(resultat, [None],
                             "un bail expiré sous SELECT FOR UPDATE a été renouvelé")

            resultat2: list = []
            bloqueur.execute("BEGIN")
            bloqueur.execute(
                "SELECT name FROM agent_registry WHERE name = 'bail' FOR UPDATE")
            thread2 = threading.Thread(
                target=lambda: resultat2.append(
                    registry.take_pending_prompt(self.db, "bail", "runner-1", epoch)),
                daemon=True)
            thread2.start()
            time.sleep(0.8)
            bloqueur.execute("COMMIT")
            thread2.join(timeout=5)
            self.assertEqual(resultat2, [None],
                             "une consigne a été consommée sous un bail expiré")
        finally:
            try:
                bloqueur.execute("ROLLBACK")
            except Exception:
                pass
            bloqueur.close()


if __name__ == "__main__":
    unittest.main()
