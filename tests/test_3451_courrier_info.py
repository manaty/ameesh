# SPDX-License-Identifier: AGPL-3.0-only
"""#3451 — un courrier sans geste attendu n'ouvre plus de tour de pilote.

Constat (24/10 → 25/10, 2 414 tours) : 1 223 tours sans travail (117 USD),
ouverts par des avis de prise automatique L165 copiés aux pilotes, des
comptes rendus « rien à faire » et des tours repartis 2 à 6 s après le
précédent. Désormais :

* une nature passive `info` (avis L165 aux pilotes, `agent-mail send
  --info`) : lue au prochain tour, sans réveil ;
* la fenêtre de regroupement court aussi depuis la fin du dernier tour de
  l'exécuteur (au plus une fenêtre de retard ; l'urgent et l'humain n'attendent
  jamais).
Postgres réel.
"""
from __future__ import annotations

import os
import time
import unittest

from ameesh import adapters, authority, mail, registry, signing
from ameesh.storage.postgres.mailbox import PASSIVE_SQL

from .test_l125_courrier_regroupe import _Base, _ligne


class NatureInfoTest(unittest.TestCase):
    def test_info_est_passive_sauf_urgente(self):
        self.assertEqual(mail.passive_reason(_ligne(payload={"info": True})), "info")
        self.assertEqual(mail.passive_label(_ligne(payload={"info": True})),
                         "information, no action expected")
        self.assertEqual(mail.passive_reason(
            _ligne(payload={"info": True, "urgent": True})), "")
        rendu = mail.prompt_for(None, [_ligne(payload={"info": True})], kind="mail")[0]
        self.assertIn("(information, no action expected)", rendu)

    def test_la_consigne_du_tour_la_nomme(self):
        self.assertIn("--info", adapters.MAIL_FOOTER)


class FenetreDepuisLaFinDuTourTest(_Base):
    def test_un_message_ancien_attend_la_fenetre_apres_le_tour(self):
        _runner, worker = self._worker("orch-f")
        mail.send(self.db, "dev1", "orch-f", "le lot 12 est prêt")
        self._vieillir("orch-f", 3600)  # arrivé pendant un long tour
        worker._turn_end_mono = time.monotonic() - 5  # tour fini il y a 5 s
        self.assertIsNone(worker.pick())
        worker._turn_end_mono = time.monotonic() - 61
        self.assertEqual(worker.pick()["kind"], "mail")

    def test_urgent_et_humain_n_attendent_pas(self):
        _runner, worker = self._worker("orch-u")
        worker._turn_end_mono = time.monotonic()
        mail.send(self.db, "dev1", "orch-u", "la CI est rouge", payload={"urgent": True})
        self.assertEqual(worker.pick()["kind"], "mail")
        worker._turn_end_mono = time.monotonic()
        mail.send(self.db, "human:proprio", "orch-u", "où en est le lot 12 ?")
        self.assertEqual(worker.pick()["kind"], "mail")

    def test_info_seule_n_ouvre_pas_de_tour(self):
        runner, worker = self._worker("orch-i", batch=0.0)
        runner.event_coalesce = 0.0
        mail.send(self.db, "ameesh", "orch-i", "Automatic take (L165): …", kind="event",
                  payload={"lot_take": {"lot": 1, "agent": "dev1", "team": "t"},
                           "info": True})
        self._vieillir("orch-i", 3600)
        self.assertIsNone(worker.pick())
        # même prédicat en base (PASSIVE_SQL, vue 0483) : compté passif
        [row] = self.db.query("SELECT unread, passive_unread FROM agent_mesh_overview"
                              " WHERE name = 'orch-i'")
        self.assertEqual((row["unread"], row["passive_unread"]), (1, 1))
        self.assertEqual(self.db.query(
            "SELECT count(*) AS n FROM agent_mailbox WHERE recipient = 'orch-i'"
            " AND NOT " + PASSIVE_SQL.format(t="agent_mailbox"))[0]["n"], 0)
        # un message ordinaire l'emporte, l'avis part avec lui
        mail.send(self.db, "dev2", "orch-i", "le lot 14 est fini")
        spec = worker.pick()
        self.assertEqual(len(spec["ids"]), 2)
        self.assertIn("(information, no action expected)", spec["prompt"])


class EnvoiInfoTest(_Base):
    def _envoi(self, *args):
        return self.cli("send", *args, env=self.env(AGENT_MAIL_NAME="dev1"))

    def _payload(self, recipient: str) -> dict:
        return mail.message_payload(mail.unread(self.db, recipient)[-1])

    def test_send_info(self):
        for name in ("orch", "dev1"):
            registry.upsert(self.db, name, harness="claude")
        proc = self._envoi("orch", "Lot 12 : rien à faire, état inchangé", "--info")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("information", proc.stdout)
        self.assertTrue(self._payload("orch").get("info"))
        self.assertEqual(mail.passive_reason(mail.unread(self.db, "orch")[-1]), "info")
        for contraire in ("--urgent", "--verdict"):
            proc = self._envoi("orch", "x", "--info", contraire)
            self.assertEqual(proc.returncode, 2, contraire)
            self.assertIn("--info and", proc.stderr)


    def test_envoi_signe_sans_info_reste_actif(self):
        # relecture cx3 : les renseignements de clé (cmd_send) écrasaient le
        # drapeau --info ; tout envoi signé avec clé enregistrée devenait passif
        for name in ("orch", "dev1"):
            registry.upsert(self.db, name, harness="claude")
        seed = signing.backend("pure").generate_seed()
        authority.register_key(self.db, "dev1",
                               signing.backend("pure").public_from_seed(seed))
        key = os.path.join(self.tmp, "dev1.key")
        with open(key, "w", encoding="utf-8") as fh:
            fh.write(signing.encode_private(seed))
        os.chmod(key, 0o600)
        proc = self._envoi("orch", "Décision signée : à appliquer", "--sign", "--key", key)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("info", self._payload("orch"))
        self.assertEqual(mail.passive_reason(mail.unread(self.db, "orch")[-1]), "")
        proc = self._envoi("orch", "Lot 12 : rien à faire", "--sign", "--key", key, "--info")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(self._payload("orch").get("info"))
        self.assertEqual(mail.passive_reason(mail.unread(self.db, "orch")[-1]), "info")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
