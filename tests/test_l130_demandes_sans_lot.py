# SPDX-License-Identifier: AGPL-3.0-only
"""L130 : toute demande d'un humain devient un lot — `ameesh work unrecorded`
liste les messages d'humains (courrier, réponses aux décisions, chat) qui ne
sont rattachés à aucun lot et n'en citent aucun ; `work add --priority`."""
from __future__ import annotations

import dataclasses
import json
import time
import unittest

from ameesh import mail, registry, storage, unrecorded, work

from .support import PgTestCase


class TextTest(unittest.TestCase):
    """Citations, passages repris, normalisation : sans base."""

    def test_citations_de_lot(self):
        self.assertEqual(
            unrecorded.cited_lots("voir L130, lot #12, lot n° 7, Lot 8 et ameesh-work: 99"),
            {130, 12, 7, 8, 99})
        # ni un mot qui finit par « l » ou « lot », ni un « #n » seul (PR, issue)
        self.assertEqual(unrecorded.cited_lots("HTML5, pilot 3, L2b, la PR #45"), set())

    def test_passage_repris_dans_la_source(self):
        lot = {"source": "Demande de human:alice (chat) : « Ajoute une commande qui liste "
                         "les demandes »", "body": ""}
        message = unrecorded.normalize("Bonjour, ajoute une commande qui liste les "
                                       "demandes.\nMerci !")
        self.assertTrue(unrecorded.quoted_by(message, lot))
        self.assertFalse(unrecorded.quoted_by(unrecorded.normalize("Ajoute autre chose"), lot))
        # le message entier, cité dans le corps du lot
        lot = {"source": "mail", "body": "Le propriétaire a écrit : relance la CI de main, "
                                         "s’il te plaît, puis préviens-moi."}
        self.assertTrue(unrecorded.quoted_by(
            unrecorded.normalize("Relance la CI de main, s'il te plaît"), lot))
        # trop court pour conclure
        self.assertFalse(unrecorded.quoted_by(unrecorded.normalize("oui"),
                                              {"source": "« oui »", "body": "oui"}))

    def test_normalisation(self):
        self.assertEqual(unrecorded.normalize("  « Fais  ÇA »\u00a0maintenant. "),
                         "fais ça maintenant")


class UnrecordedTest(PgTestCase):
    """Le contrôle en base, et la commande."""

    def setUp(self):
        super().setUp()
        storage.of(self.db).decisions.register_chat(
            "chat-alice", human="human:alice", host=self.cfg.host, harness="claude",
            cwd=self.tmp, status_text="conversation de human:alice")

    def send(self, sender, recipient, body, **kw):
        return mail.send(self.db, sender, recipient, body, **kw)

    def human(self, recipient, body, sender="alice", **kw):
        payload = dict(kw.pop("payload", None) or {}, human=True)
        return self.send(sender, recipient, body, payload=payload, **kw)

    def scan(self, cfg=None, since_s=3600.0):
        return unrecorded.scan(cfg or self.cfg, self.db, since_ts=time.time() - since_s)

    def test_messages_enregistres_ou_non(self):
        lot = work.add(self.db, title="frise : corriger l'échelle")
        rattache = self.human("orch", "Corrige l'échelle de la frise, elle déborde.",
                              work_item_id=str(lot["id"]))
        cite = self.human("orch", "Où en est le L%d ? Je n'ai rien vu passer." % lot["id"])
        fantome = self.human("orch", "Où en est le L99999 ? Je n'ai rien vu passer.")
        oublie = self.human("orch", "Ajoute une commande qui liste les demandes sans lot.")
        self.human("orch", "Bien reçu, merci !")                           # accusé : ignoré
        self.send("alice", "dev1", "Ajoute une commande qui liste les demandes sans lot.",
                  kind="event", payload={"cc": "orch"})                   # copie : ignorée
        self.send("orch", "dev1", "Prends la frise, sans lot pour l'instant.")  # agent
        decision = self.send("human:alice", "orch", "Décision #4 : option a.", kind="reply",
                             work_item_id=str(lot["id"]),
                             payload={"human": True, "decision_answer": {"decision": 4}})
        transmis = self.send("chat-alice", "orch",
                             "Demande de human:alice : « Relance la CI de la branche "
                             "principale avant midi »")
        # alice est aussi un humain déclaré : sa copie (--cc) serait examinée
        # sans la règle des copies
        cfg = dataclasses.replace(self.cfg, humans="alice")
        report = self.scan(cfg)
        self.assertEqual(report["schema"], "ameesh-unrecorded/1")
        enregistres = {e["id"]: (e["how"], e["lot"]) for e in report["recorded"]}
        self.assertEqual(enregistres, {rattache: ("attached", lot["id"]),
                                       cite: ("cited", lot["id"]),
                                       decision: ("attached", lot["id"])})
        manquants = {e["id"]: (e["channel"], e["recipients"]) for e in report["unrecorded"]}
        self.assertEqual(manquants, {fantome: ("mail", ["orch"]), oublie: ("mail", ["orch"]),
                                     transmis: ("chat", ["orch"])})
        self.assertEqual(report["examined"], 6)

        # la demande est enregistrée, phrase exacte en source : elle sort de la liste
        work.add(self.db, title="L131 : demandes sans lot", priority=1,
                 source="Demande d'alice (courrier) : « ajoute une commande qui liste les "
                        "demandes sans lot »")
        work.add(self.db, title="CI", source="chat : « Relance la CI de la branche "
                                             "principale avant midi »")
        report = self.scan(cfg)
        self.assertEqual({e["id"] for e in report["unrecorded"]}, {fantome})
        how = {e["id"]: e["how"] for e in report["recorded"]}
        self.assertEqual((how[oublie], how[transmis]), ("source", "source"))

    def test_qui_est_humain(self):
        # avant L125, le courrier d'un humain n'était pas marqué : son nom déclaré suffit
        ancien = self.send("bob", "orch", "Prépare la version 1.8 pour demain matin.")
        # un agent mené qui s'appelle chat-… n'est pas le chat d'un humain
        registry.upsert(self.db, "chat-faux", harness="claude", host=self.cfg.host)
        self.send("chat-faux", "orch", "Demande : un agent mené qui s'appelle chat-faux.")
        self.assertEqual(self.scan()["examined"], 0)
        report = self.scan(dataclasses.replace(self.cfg, humans="bob"))
        self.assertEqual([e["id"] for e in report["unrecorded"]], [ancien])
        # décision répondue sans lot (cas dégradé) : listée, nature « décision »
        reponse = self.send("human:alice", "orch", "Décision #9 : non, pas maintenant.",
                            kind="reply", payload={"human": True,
                                                   "decision_answer": {"decision": 9}})
        canaux = {e["id"]: e["channel"] for e in self.scan()["unrecorded"]}
        self.assertEqual(canaux, {reponse: "decision"})

    def test_periode_et_diffusion(self):
        vieux = self.human("orch", "Une demande d'il y a trois jours, jamais enregistrée.")
        self.db.execute("UPDATE agent_mailbox SET created_at = now() - interval '3 days'"
                        " WHERE id = %s", (vieux,))
        un = self.human("dev1", "Arrêtez tout déploiement jusqu'à ce soir, s'il vous plaît.")
        deux = self.human("dev2", "Arrêtez tout déploiement jusqu'à ce soir, s'il vous plaît.")
        report = self.scan()
        entry, = report["unrecorded"]
        self.assertEqual((entry["id"], entry["ids"], entry["recipients"]),
                         (un, [un, deux], ["dev1", "dev2"]))
        self.assertIn(vieux, [e["id"] for e in self.scan(since_s=7 * 86400)["unrecorded"]])

    def test_commande(self):
        oublie = self.human("orch", "Ajoute une commande qui liste les demandes sans lot.")
        proc = self.mesh("work", "unrecorded", "--since", "2h", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual([e["id"] for e in report["unrecorded"]], [oublie])
        proc = self.mesh("work", "unrecorded")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("n°%d" % oublie, proc.stdout)
        self.assertIn("alice → orch  [courrier]", proc.stdout)
        self.assertIn("ameesh work add", proc.stdout)
        proc = self.mesh("work", "unrecorded", "--since", "hier")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--since illisible", proc.stderr)
        # aucune écriture : la commande ne touche ni au courrier ni aux lots
        self.assertEqual(self.db.query("SELECT count(*)::int AS n FROM work_items")[0]["n"], 0)

    def test_work_add_priorite(self):
        proc = self.mesh("work", "add", "--title", "L131 : demandes sans lot", "--source",
                         "alice : « liste les demandes sans lot »", "--priority", "1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("(priorité haute)", proc.stdout)
        item, = work.list_items(self.db)
        self.assertEqual((item["priority"], item["source"]),
                         (1, "alice : « liste les demandes sans lot »"))
        proc = self.mesh("work", "show", str(item["id"]))
        self.assertIn("priorité : 1 (haute)", proc.stdout)
        proc = self.mesh("work", "add", "--title", "x", "--priority", "5")
        self.assertEqual(proc.returncode, 2)
        with self.assertRaises(work.WorkError):
            work.add(self.db, title="x", priority=0)
        self.assertIsNone(work.add(self.db, title="sans priorité")["priority"])


if __name__ == "__main__":
    unittest.main()
