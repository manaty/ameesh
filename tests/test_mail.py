# SPDX-License-Identifier: AGPL-3.0-only
"""Boîte aux lettres : dépôt, lecture, remise, LISTEN/NOTIFY."""
from __future__ import annotations

import json
import time
import unittest

from ameesh import mail
from ameesh import registry
from ameesh.backend import PgBackend

from .support import PgTestCase


class MailTest(PgTestCase):
    def _notifications(self, listener, to: str, count: int = 1, mid: int | None = None,
                       timeout: float = 5.0) -> list[dict]:
        """Notifications de CE schéma : le canal LISTEN/NOTIFY est global à la
        base, des suites concurrentes y déposent leurs propres messages — on
        filtre sur le destinataire (et l'id quand il est connu)."""
        trouves: list[dict] = []
        deadline = time.monotonic() + timeout
        while len(trouves) < count and time.monotonic() < deadline:
            item = listener.wait(timeout=1.0)
            if not item or not item.get("channel"):
                continue
            try:
                charge = json.loads(item["payload"])
            except ValueError:
                continue
            if charge.get("to") != to:
                continue
            if mid is not None and int(charge.get("id", 0)) != mid:
                continue
            trouves.append(charge)
        return trouves

    def test_envoi_lecture_remise(self):
        registry.upsert(self.db, "beta", harness="codex", host="autre")
        message_id = mail.send(self.db, "alpha", "beta", "bonjour beta", host="laptop")

        non_lus = mail.unread(self.db, "beta")
        self.assertEqual(len(non_lus), 1)
        self.assertEqual(non_lus[0]["id"], message_id)
        self.assertEqual(non_lus[0]["sender"], "alpha")
        self.assertEqual(non_lus[0]["body"], "bonjour beta")
        self.assertEqual(non_lus[0]["status"], "pending")
        self.assertEqual(non_lus[0]["delivered_ts"], None)
        self.assertEqual(mail.unread_counts(self.db), {"beta": 1})

        self.assertEqual(mail.mark_delivered(self.db, [message_id]), 1)
        self.assertEqual(mail.unread(self.db, "beta"), [])
        self.assertEqual(mail.unread_counts(self.db), {})
        self.assertEqual(mail.mark_delivered(self.db, [message_id]), 0)  # déjà remis
        history = mail.history(self.db, "beta")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["status"], "delivered")
        self.assertIsNotNone(history[0]["delivered_ts"])

    def test_staging_emission_grande_boite(self):
        """#4180 : aucun plafond historique, filtres exacts et rejeu sans doublon."""
        from ameesh import work
        registry.upsert(self.db, "beta", harness="codex", host="autre")
        registry.upsert(self.db, "gamma", harness="codex", host="autre")
        cle = "0123456789ab:beta"
        charge = {"staging_actif": {"emission": cle}}
        # Ces faux positifs ne prouvent pas un dépôt pour beta par le système.
        mail.send(self.db, "alpha", "beta", "autre expéditeur", payload=charge)
        mail.send(self.db, work.SYSTEM_SENDER, "gamma", "autre destinataire", payload=charge)
        mail.send(self.db, work.SYSTEM_SENDER, "beta", "autre clé",
                  payload={"staging_actif": {"emission": cle + "-autre"}})
        self.db.execute(
            "INSERT INTO agent_mailbox (sender, recipient, body, kind)"
            " SELECT 'alpha', 'beta', 'bruit', 'notify'"
            " FROM generate_series(1, 20001)")
        self.assertIsNone(mail.staging_emission(self.db, "beta", work.SYSTEM_SENDER, cle))
        # Exerce le vrai signal et sa reprise après dépôt réussi mais échec
        # de sauvegarde locale : ni recherche ni envoi de courrier substitués.
        import io
        import tempfile
        from pathlib import Path
        from contextlib import redirect_stdout, redirect_stderr
        from unittest import mock
        from . import test_1323_actif as fixture
        actif = fixture.charger_actif()
        with tempfile.TemporaryDirectory() as dossier:
            racine = Path(dossier)
            journal = racine / "journal.jsonl"
            journal.write_text("".join(json.dumps(e) + "\n"
                                       for e in fixture.journal_complet()))
            argv = ["--journal", str(journal), "--sha", fixture.SHA,
                    "--depuis", fixture.DEPUIS, "--etat-dir", str(racine),
                    "--rapporteur", "beta"]
            for hote in fixture.HOTES:
                argv += ["--hote", hote + "=" + fixture.SHA]
            ecrire = actif.ecrire_attestation

            def echec_apres_envoi(chemin, attestation):
                if attestation["notifies"]:
                    raise OSError("sauvegarde après dépôt indisponible")
                return ecrire(chemin, attestation)

            with mock.patch.object(actif.db_mod, "connect", return_value=self.db), \
                 mock.patch.object(self.db, "close"), \
                 mock.patch.object(actif, "verifier_remises"), \
                 mock.patch.object(actif, "enregistrer_attestation"), \
                 redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                with mock.patch.object(actif, "ecrire_attestation",
                                       side_effect=echec_apres_envoi):
                    self.assertEqual(actif.main(argv), actif.CODE_ECHEC)
                premier = mail.staging_emission(self.db, "beta", work.SYSTEM_SENDER, cle)
                self.assertIsNotNone(premier)
                mail.mark_delivered(self.db, [premier])
                self.db.execute(
                    "INSERT INTO agent_mailbox (sender, recipient, body, kind)"
                    " SELECT 'alpha', 'beta', 'bruit', 'notify'"
                    " FROM generate_series(1, 20001)")
                self.assertEqual(actif.main(argv), 0)
                self.assertEqual(actif.main(argv), 0)
            attestation = json.loads((racine / ("actif-" + fixture.SHA[:12] + ".json")).read_text())
            self.assertEqual(attestation["notifies"]["beta"]["message"], premier)
            self.assertTrue(attestation["notifies"]["beta"]["reconcilie"])
        self.assertEqual(self.db.query(
            "SELECT count(*) AS n FROM agent_mailbox WHERE recipient = %s"
            " AND sender = %s AND payload -> 'staging_actif' ->> 'emission' = %s",
            ("beta", work.SYSTEM_SENDER, cle))[0]["n"], 1)

    def test_remise_horodatee_par_message(self):
        """#796 : chaque message remis porte SON instant de remise (l'instant
        réel où il entre dans le contexte), pas l'horodatage de transaction
        partagé par tout un lot — c'est ce qui rend le délai de réaction
        mesurable message par message.

        Tout se lit sur l'horloge de la base, dans UNE transaction : `now()`
        (début de transaction) y est figé pendant que `clock_timestamp()`
        avance. Une remise qui recopierait `now()` — l'ancien défaut, un seul
        instant pour tout un lot — daterait les messages du début de
        transaction ; la remise par message les date après lui. Aucune horloge
        Python, donc aucun écart d'horloge entre la machine de test et la base ;
        le sommeil force 50 ms d'écart, donc aucune dépendance à la résolution
        de l'horloge."""
        registry.upsert(self.db, "beta", harness="codex", host="autre")
        ids = [mail.send(self.db, "alpha", "beta", "message %d" % i, host="laptop")
               for i in range(5)]
        with self.db.transaction() as tx:
            debut = tx.query(
                "select extract(epoch from now())::float8 as at_epoch")[0]["at_epoch"]
            tx.query("select pg_sleep(0.05)")
            self.assertEqual(mail.mark_delivered(tx, ids), len(ids))
            stamps = [row["delivered_ts"] for row in tx.query(
                "select extract(epoch from delivered_at)::float8 as delivered_ts"
                " from agent_mailbox where id in (%s) order by id"
                % ", ".join(str(int(i)) for i in ids))]
            fin = tx.query(
                "select extract(epoch from clock_timestamp())::float8 as at_epoch")[0]["at_epoch"]
        self.assertEqual(len(stamps), len(ids))
        # l'instant de chaque message tombe dans la fenêtre de la remise, lue
        # sur la même horloge que les timbres...
        self.assertTrue(all(debut < s <= fin for s in stamps), (debut, stamps, fin))

    def test_cle_idempotence_rejoue_ou_refuse(self):
        """#3892 : une clé rejouée rend le même message ; réutilisée pour un
        autre corps ou un autre lot, elle est refusée — jamais rendue comme
        envoyée, sinon une demande jamais partie passerait pour livrée."""
        registry.upsert(self.db, "beta", harness="codex", host="autre")
        first = mail.send(self.db, "alpha", "beta", "bonjour", host="laptop",
                          idempotency_key="cle-1")
        self.assertEqual(
            mail.send(self.db, "alpha", "beta", "bonjour", host="laptop",
                      idempotency_key="cle-1"),
            first)
        self.assertEqual(len(mail.unread(self.db, "beta")), 1)
        with self.assertRaises(ValueError):
            mail.send(self.db, "alpha", "beta", "autre texte", host="laptop",
                      idempotency_key="cle-1")
        with self.assertRaises(ValueError):
            mail.send(self.db, "alpha", "beta", "bonjour", host="laptop",
                      work_item_id="42", idempotency_key="cle-1")
        self.assertEqual(len(mail.unread(self.db, "beta")), 1)

    def test_normalize_forme_commune(self):
        mail.send(self.db, "alpha", "beta", "coucou", host="laptop")
        row = mail.normalize(mail.unread(self.db, "beta")[0])
        self.assertEqual(
            set(row) >= {"id", "from", "to", "ts", "text", "kind", "payload", "host"}, True
        )
        self.assertEqual((row["from"], row["to"], row["text"]), ("alpha", "beta", "coucou"))

    def test_cli_send_all_et_liste(self):
        registry.upsert(self.db, "alpha", harness="claude", host="laptop")
        registry.upsert(self.db, "beta", harness="codex", host="laptop")
        registry.upsert(self.db, "gamma", harness="deepseek", host="autre")
        backend = PgBackend(self.cfg, self.db)

        targets = backend.send("alpha", "all", "message collectif", host="laptop")
        self.assertEqual(sorted(targets), ["beta", "gamma"])
        self.assertEqual(len(mail.unread(self.db, "beta")), 1)
        self.assertEqual(len(mail.unread(self.db, "gamma")), 1)
        self.assertEqual(mail.unread(self.db, "alpha"), [])

        noms = {row["name"]: row for row in backend.agents()}
        self.assertEqual(noms["beta"]["unread"], 1)
        self.assertEqual(noms["gamma"]["unread"], 1)
        self.assertEqual(noms["alpha"]["unread"], 0)

    def test_listen_notify_de_bout_en_bout(self):
        listener = self.db.listen(["agent_mail"])
        self.assertIsNotNone(listener)
        try:
            time.sleep(0.6)  # laisser LISTEN s'établir (pilote psql : ~0,5 s)
            mail.send(self.db, "alpha", "beta", "réveille-toi", host="laptop")
            charges = self._notifications(listener, "beta", mid=1)
            self.assertEqual(len(charges), 1, "aucune notification reçue pour beta")
            self.assertEqual(int(charges[0]["id"]), 1)
        finally:
            listener.close()

    def test_notification_par_message(self):
        """Chaque message déposé produit une notification (et pas une par lot)."""
        listener = self.db.listen(["agent_mail"])
        try:
            time.sleep(0.6)
            for index in range(3):
                mail.send(self.db, "alpha", "beta", "message %d" % index)
            charges = self._notifications(listener, "beta", count=3)
            self.assertEqual(len(charges), 3)
        finally:
            listener.close()


if __name__ == "__main__":
    unittest.main()
